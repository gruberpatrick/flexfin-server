import json
import logging
import os
import pathlib
import requests
import subprocess
import shutil

import tidalapi
from mutagen.flac import FLAC

logging.basicConfig()
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

MEDIA_PATH = "./"
CREDS_FILE = pathlib.Path("tidal_creds.json")

_session: tidalapi.Session | None = None
_last_saved_token: str | None = None


def _save_creds(session: tidalapi.Session) -> None:
    global _last_saved_token
    CREDS_FILE.write_text(json.dumps({
        "token_type": session.token_type,
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "expiry_time": session.expiry_time.isoformat(),
    }))
    _last_saved_token = session.access_token


def _persist_if_refreshed(session: tidalapi.Session) -> None:
    # tidalapi auto-refreshes the access token in-memory on 401 but never writes it
    # back to disk. Save when we notice the token has changed.
    if session.access_token != _last_saved_token:
        _save_creds(session)


class TidalClient:

    def _get_session(self):
        global _session, _last_saved_token

        if _session is not None:
            _persist_if_refreshed(_session)
            return _session

        session = tidalapi.Session()
        session.audio_quality = tidalapi.Quality.hi_res_lossless

        if CREDS_FILE.exists():
            session.load_oauth_session(**json.loads(CREDS_FILE.read_text()))
            _last_saved_token = session.access_token
        else:
            session.login_oauth_simple()
            _save_creds(session)

        _session = session
        return _session

    def _tag_audio_file(self, file, album, track):
        try:
            audio = FLAC(file)
            audio["id"] = str(track.id)
            audio["album_id"] = str(album.id)
            audio["title"] = track.name
            audio["artist"] = [artist.name for artist in track.artists]
            audio["album"] = album.name
            audio["date"] = str(album.year or "")
            audio["ORIGINALDATE"] = str(album.year or "")
            audio["tracknumber"] = str(track.track_num)
            audio["discnumber"] = str(track.volume_num)
            audio["totaldiscs"] = str(album.num_volumes)
            audio["DISCTOTAL"] = str(album.num_volumes)
            audio.save()
        except Exception as e:
            logger.error("Unable to tag file: %s", e)

    def _process_dash_file(self, combined_file, out_file):
        try:
            logger.info("Combining segment files into single file...")

            ffmpeg_bin = "ffmpeg"
            cmd = [ffmpeg_bin, "-hide_banner", "-y", "-v", "error", "-xerror",
                "-i", combined_file,
                "-map", "0:a:0",
                "-vn", "-sn", "-c:a",
                "copy", out_file,
            ]
            subprocess.run(cmd, check=True)
            os.unlink(combined_file)
            return out_file

        except Exception as e:
            logger.exception("ERROR: %s", e)

    def get_manifest(self, track_id):
        track = self._get_session().track(track_id)
        stream = track.get_stream()
        manifest = stream.get_stream_manifest()
        audio_resolution = stream.get_audio_resolution()
        return manifest.get_urls()

    def _get_urls(self, track):

        logger.info(f"{track.id}: '{track.artist.name}' - '{track.name}'")
        stream = track.get_stream()
        logger.info(f"mime_type: {stream.manifest_mime_type}")
        manifest = stream.get_stream_manifest()
        audio_resolution = stream.get_audio_resolution()
        codec = manifest.get_codecs().lower()
        logger.info(f"track:{track.id}, (quality:{stream.audio_quality}, codec:{codec}, {audio_resolution[0]}bit/{audio_resolution[1]}Hz)")

        final_path = f"./cache/{track.id}"
        filename = "file"
        os.makedirs(pathlib.Path(final_path), exist_ok=True)
        logger.info(pathlib.Path(final_path))
        final_file_name = f"{filename}.{codec}"

        if os.path.exists(os.path.join(final_path, final_file_name)):
            logger.info("SKIPPING, file already exists...")
            return

        urls = []
        is_dash = False
        if stream.is_mpd:  # application/dash+xml
            urls = manifest.get_urls()
            is_dash = True
        elif stream.is_bts:  # other normal ones
            urls = manifest.get_urls()

        return urls, codec, final_path, final_file_name, is_dash

    def _stream_track(self, track):
        stream = track.get_stream()
        manifest = stream.get_stream_manifest()

        CODEC_MAP = {
            "FLAC": "fLaC",
            "MP4A": "mp4a.40.2",
            "AACLC": "mp4a.40.2",
            "HEAACV1": "mp4a.40.5",
        }
        codec_enum = manifest.codecs
        codec_name = str(codec_enum).split(".")[-1].upper()
        codec = CODEC_MAP.get(codec_name, "mp4a.40.2")

        seg_urls = manifest.get_urls()
        try:
            hls = manifest.get_hls()
        except Exception:
            # BTS streams are single-file (AAC/MP4) — tidalapi's get_hls() only
            # handles MPD. Wrap the lone URL in a 1-segment HLS manifest so
            # clients can always use the HLS playback path.
            duration = max(int(track.duration or 1), 1)
            hls = (
                "#EXTM3U\n"
                "#EXT-X-VERSION:3\n"
                f"#EXT-X-TARGETDURATION:{duration + 1}\n"
                "#EXT-X-MEDIA-SEQUENCE:0\n"
                "#EXT-X-PLAYLIST-TYPE:VOD\n"
                f"#EXTINF:{float(track.duration or duration):.3f},\n"
                f"{seg_urls[0]}\n"
                "#EXT-X-ENDLIST\n"
            )

        return {
            "mime": manifest.manifest_mime_type,
            "codec": codec,
            "seg_urls": seg_urls,
            "duration": track.duration,
            "hls": hls,
        }

    def _download_track(self, album, track):

        urls, codec, final_path, final_file_name, is_dash = self._get_urls(album, track)

        # download all files
        all_files = []
        for url in urls:

            file_name = url[url.rfind("/") + 1:url.find("?")]
            out_path = pathlib.Path(final_path, file_name)
            all_files.append(out_path)

            # skip if downloaded
            if out_path.exists():
                continue

            logger.info(f"Download {url} to {out_path}")
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:  # skip keep-alive chunks
                            f.write(chunk)

        # combine the files
        with open(final_file_name + ".mp4", "wb") as fh:
            for file in all_files:
                fh.write(open(file, "rb").read())
                os.unlink(file)

        if is_dash:
            self._process_dash_file(final_file_name + ".mp4", final_file_name)
        else:
            shutil.move(final_file_name + ".mp4", final_file_name)

        self._tag_audio_file(final_file_name, album, track)
    
    def _serialize_image(self, uuid, size: str = "1280x1280"):
        if uuid is None:
            return ""
        return f"https://resources.tidal.com/images/{uuid.replace('-', '/')}/{size}.jpg"
    
    def _serialize_artist(self, artist, with_albums=False):
        albums = []
        if with_albums:
            try:
                albums = [self._serialize_album(album) for album in artist.get_albums()]
            except Exception:
                pass
        return {
            "id": artist.id,
            "name": artist.name,
            "picture": self._serialize_image(artist.picture, size="320x320"),
            "picture_raw": artist.picture,
            "albums": albums,
        }

    def _serialize_album(self, album, with_tracks=False):
        tracks = []
        if with_tracks:
            try:
                tracks = [self._serialize_track(track) for track in album.tracks()]
            except Exception:
                pass
        return {
            "id": album.id,
            "title": album.name,
            "cover": self._serialize_image(album.cover),
            "cover_raw": album.cover,
            "duration": album.duration,
            "allow_streaming": album.allow_streaming,
            "num_tracks": album.num_tracks,
            "num_volumes": album.num_volumes,
            "release_date": album.release_date.isoformat() if album.release_date else "",
            "year": album.year if album.year else "",
            "popularity": album.popularity,
            "audio_quality": album.audio_quality,
            "artist": self._serialize_artist(album.artist),
            "artists": [self._serialize_artist(artist) for artist in album.artists],
            "tracks": tracks,
        }

    def _serialize_track(self, track):
        return {
            "id": track.id,
            "title": track.title,
            "duration": track.duration,
            "popularity": track.popularity,
            "allow_streaming": track.allow_streaming,
            "audio_quality": track.audio_quality,
            "bpm": track.bpm,
            "key": track.key,
            "key_scale": track.key_scale,
            "peak": track.peak,
            "replay_gain": track.replay_gain,
            "track_num": track.track_num,
            "volume_num": track.volume_num,
            "artist": self._serialize_artist(track.artist),
            "artists": [self._serialize_artist(artist) for artist in track.artists],
            "album": self._serialize_album(track.album)
        }
    
    def _serialize_playlist(self, playlist, with_tracks=False):
        tracks = []
        if with_tracks:
            try:
                tracks = [self._serialize_track(track) for track in playlist.tracks()]
            except Exception:
                pass
        return {
            "id": playlist.id,
            "name": playlist.name,
            "num_tracks": playlist.num_tracks,
            "description": playlist.description,
            "duration": playlist.duration,
            "popularity": playlist.popularity,
            "picture": self._serialize_image(playlist.picture),
            "tracks": tracks,
        }

    def _get_page(self, page: str):
        return self._get_session().page.get(page)

    def download_album(self, album_id):
        album = self._get_session().album(album_id)
        tracks = album.tracks()
        for track in tracks:
            logger.info(track)
            self._download_track(album, track)

    def download_track(self, track_id):
        track = self._get_session().track(track_id)
        album = self._get_session().album(track.album.id)
        self._download_track(album, track)

    def stream_track(self, track_id):
        track = self._get_session().track(track_id)
        return self._stream_track(track)

    def raw_track(self, track_id):
        return self._get_session().track(track_id)

    def raw_album(self, album_id):
        return self._get_session().album(album_id)

    def raw_artist(self, artist_id):
        return self._get_session().artist(artist_id)

    def raw_playlist(self, playlist_id):
        return self._get_session().playlist(playlist_id)

    # Higher rank = better quality. Tidal lists the same release once per
    # quality tier (and again for Dolby Atmos), so we collapse them.
    _QUALITY_RANK = {
        "HI_RES_LOSSLESS": 4,
        "LOSSLESS": 3,
        "HIGH": 2,
        "LOW": 1,
    }

    def albums_for_artist(self, artist_id, limit=None):
        # Tidal splits LPs and EPs/singles across two endpoints; modern artists
        # often release only singles, so we merge both.
        artist = self._get_session().artist(artist_id)
        albums = list(artist.get_albums(limit=limit) or [])
        singles = list(artist.get_ep_singles(limit=limit) or [])

        best: dict = {}
        for a in albums + singles:
            primary = a.artist.name if a.artist is not None else ""
            title = (a.name or "").strip().lower()
            date = a.release_date.toordinal() if a.release_date else 0
            key = (title, primary, date)
            rank = self._QUALITY_RANK.get(a.audio_quality or "", 0)
            if key not in best or rank > self._QUALITY_RANK.get(best[key].audio_quality or "", 0):
                best[key] = a

        merged = list(best.values())
        merged.sort(
            key=lambda a: a.release_date.toordinal() if a.release_date else 0,
            reverse=True,
        )
        return merged[:limit] if limit else merged

    def top_tracks_for_artist(self, artist_id, limit=50):
        return self._get_session().artist(artist_id).get_top_tracks(limit=limit)

    def tracks_for_album(self, album_id):
        return self._get_session().album(album_id).tracks()

    def get_artist(self, artist_id):
        return self._serialize_artist(self._get_session().artist(artist_id), with_albums=True)

    def get_album(self, album_id):
        return self._serialize_album(self._get_session().album(album_id), with_tracks=True)

    def get_track(self, track_id):
        return self._serialize_track(self._get_session().track(track_id))

    def get_playlist(self, playlist_id):
        return self._serialize_playlist(self._get_session().playlist(playlist_id), with_tracks=True)
    
    def get_artist_picture(self, uuid):
        return self._serialize_image(uuid, size="320x320")

    def get_art(self, uuid):
        return self._serialize_image(uuid)

    def for_you(self):
        return self._get_session().for_you()

    def new_track_suggestions(self):
        return self._get_page("pages/NEW_TRACK_SUGGESTIONS/view-all")

    def _drain_page(self, page_iterable, target_cls, limit, out, seen):
        """Append items of `target_cls` from `page_iterable` into `out` (deduped via `seen`)."""
        try:
            for item in page_iterable:
                if not isinstance(item, target_cls):
                    continue
                if item.id in seen:
                    continue
                seen.add(item.id)
                out.append(item)
                if len(out) >= limit:
                    return
        except Exception:
            logger.exception("page iteration failed for %s", target_cls.__name__)

    def _items_with_fallback(self, target_cls, limit: int, fresh_slugs: list[str]):
        """Pull items of one type, preferring Tidal's editorial 'new' pages first
        and topping up from home() recommendations."""
        out = []
        seen = set()
        session = self._get_session()
        for slug in fresh_slugs:
            try:
                page = session.page.get(slug)
            except Exception:
                logger.warning("page %s unavailable for %s", slug, target_cls.__name__)
                continue
            self._drain_page(page, target_cls, limit, out, seen)
            if len(out) >= limit:
                return out
        try:
            self._drain_page(session.home(), target_cls, limit, out, seen)
        except Exception:
            logger.exception("home() failed")
        return out

    NEW_RELEASES_SLUG = "pages/explore_new_music"
    TOP_MUSIC_SLUG = "pages/explore_top_music"

    def new_albums(self, limit: int = 100):
        return self._items_with_fallback(
            tidalapi.album.Album, limit,
            fresh_slugs=[self.NEW_RELEASES_SLUG, self.TOP_MUSIC_SLUG],
        )

    def featured_artists(self, limit: int = 100):
        # No dedicated new-artist surface; pull from top music, then fall back to home.
        return self._items_with_fallback(
            tidalapi.artist.Artist, limit,
            fresh_slugs=[self.TOP_MUSIC_SLUG],
        )

    def new_tracks(self, limit: int = 100):
        out = []
        seen = set()
        session = self._get_session()
        for slug in (self.NEW_RELEASES_SLUG, "pages/NEW_TRACK_SUGGESTIONS/view-all", self.TOP_MUSIC_SLUG):
            try:
                page = session.page.get(slug)
            except Exception:
                logger.warning("page %s unavailable for Track", slug)
                continue
            self._drain_page(page, tidalapi.media.Track, limit, out, seen)
            if len(out) >= limit:
                return out
        try:
            self._drain_page(session.home(), tidalapi.media.Track, limit, out, seen)
        except Exception:
            logger.exception("home() fallback for tracks failed")
        return out

    def search(self, query, limit=300, search_type: str | None = None):
        parsed_type = None
        if search_type == "ARTIST":
            parsed_type = tidalapi.artist.Artist
        elif search_type == "TRACK":
            parsed_type = tidalapi.media.Track
        elif search_type == "ALBUM":
            parsed_type = tidalapi.album.Album
        elif search_type == "PLAYLIST":
            parsed_type = tidalapi.playlist.Playlist
        return self._get_session().search(query, limit=limit, models=[parsed_type])

    def tracks_for_playlist(self, playlist_id):
        return self._get_session().playlist(playlist_id).tracks()

if __name__ == "__main__":
    # album_id = "63261249"
    # album_id = "506895498"
    # TidalClient().download_album("141969000")
    # TidalClient().download_track("45157363")
    # print(TidalClient().search("Gigi D'agostino"))
    # TidalClient().stream_track(514837)
    # print(TidalClient().get_track("250603599"))
    res = TidalClient().new_track_suggestions()
    for item in res:
        print(item.artist.name, "-", item.title)
    # for item in res.next():
        # print(item.artist.name, "-", item.title)
