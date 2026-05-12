import logging

from flask import Response

import db
from schema.jellyfin import ResultWrapper, Artist, Album, Track, Playlist, MinimalArtistElements, UserData
from tidal_client import TidalClient

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s",)
log = logging.getLogger("subsonic-proxy")

SERVER_ID = "66666666-6666-6666-6666-666666666666"


def _return_json(model: any) -> Response:
    return Response(
        model.model_dump_json(by_alias=True),
        mimetype="application/json",
    )


def _get_search_params(request):
    search_term = request.args.get("SearchTerm", "").strip()
    include_types = request.args.get("IncludeItemTypes", "")
    limit = int(request.args.get("Limit", "50"))
    start_index = int(request.args.get("StartIndex", "0"))
    return search_term, include_types, limit, start_index


def _strip_prefix(value: str, prefix: str) -> str | None:
    return value[len(prefix):] if value.startswith(prefix) else None


def _artist_name(artist) -> str:
    return artist.name if artist is not None else ""


def _artist_mapping(artists) -> list[MinimalArtistElements]:
    return [
        MinimalArtistElements(id=f"tidal_artist_{a.id}", name=a.name)
        for a in (artists or [])
    ]


def _cache_artist(artist) -> None:
    if artist is None:
        return
    db.upsert_artist(artist.id, artist.name, getattr(artist, "picture", None))


def _cache_album(album) -> None:
    if album is None:
        return
    primary = album.artist
    _cache_artist(primary)
    db.upsert_album(
        tidal_id=album.id,
        title=album.name,
        cover_uuid=album.cover,
        artist_id=primary.id if primary is not None else None,
        artist_name=primary.name if primary is not None else None,
        num_tracks=album.num_tracks,
        year=album.year,
        release_date=album.release_date.isoformat() if album.release_date else None,
    )


def _user_data_for(user_id: str, item_id: str, *, with_play_count: bool = False) -> UserData:
    favorite = db.is_favorite(user_id, item_id)
    if with_play_count:
        play_count = db.get_play_count(user_id, item_id)
        return UserData(is_favorite=favorite, play_count=play_count, played=play_count > 0)
    return UserData(is_favorite=favorite)


def _to_artist_item(artist, user_id: str) -> Artist:
    _cache_artist(artist)
    item_id = f"tidal_artist_{artist.id}"
    return Artist(
        id=item_id,
        name=artist.name,
        server_id=SERVER_ID,
        image_tags={"Primary": artist.picture} if artist.picture else {},
        user_data=_user_data_for(user_id, item_id),
    )


def _to_album_item(album, user_id: str) -> Album:
    _cache_album(album)
    item_id = f"tidal_album_{album.id}"
    return Album.create(
        id=item_id,
        name=album.name,
        server_id=SERVER_ID,
        album_artist=_artist_name(album.artist),
        artist_mapping=_artist_mapping(album.artists),
        child_count=album.num_tracks,
        production_year=album.year,
        premiere_date=album.release_date.isoformat() if album.release_date else "",
        cover_id=album.cover,
        user_data=_user_data_for(user_id, item_id),
    )


def _cache_playlist(playlist) -> None:
    if playlist is None:
        return
    db.upsert_playlist(
        tidal_id=playlist.id,
        name=playlist.name,
        picture_uuid=getattr(playlist, "picture", None),
        description=getattr(playlist, "description", None),
        num_tracks=getattr(playlist, "num_tracks", None),
        duration=getattr(playlist, "duration", None),
    )


def _to_playlist_item(playlist, user_id: str) -> Playlist:
    _cache_playlist(playlist)
    item_id = f"tidal_playlist_{playlist.id}"
    duration = getattr(playlist, "duration", None) or 0
    return Playlist.create(
        id=item_id,
        name=playlist.name,
        server_id=SERVER_ID,
        child_count=getattr(playlist, "num_tracks", 0) or 0,
        overview=getattr(playlist, "description", "") or "",
        run_time_ticks=duration * 10_000_000,
        cover_id=getattr(playlist, "picture", None),
        user_data=_user_data_for(user_id, item_id),
    )


def _to_track_item(track, user_id: str) -> Track:
    _cache_album(track.album)
    primary = track.album.artist if track.album is not None else None
    db.upsert_track(
        tidal_id=track.id,
        title=track.title,
        album_id=track.album.id if track.album is not None else None,
        album_title=track.album.name if track.album is not None else None,
        album_cover_uuid=track.album.cover if track.album is not None else None,
        artist_id=primary.id if primary is not None else None,
        artist_name=primary.name if primary is not None else None,
        duration=track.duration,
        track_num=track.track_num,
        volume_num=track.volume_num,
        year=track.album.year if track.album is not None else None,
    )

    item_id = f"tidal_track_{track.id}"
    user_data = _user_data_for(user_id, item_id, with_play_count=True)

    return Track.create(
        id=item_id,
        name=track.title,
        server_id=SERVER_ID,
        album=track.album.name if track.album is not None else "",
        album_id=f"tidal_album_{track.album.id}" if track.album is not None else "",
        album_artist=_artist_name(primary),
        artist_mapping=_artist_mapping(track.artists),
        production_year=track.album.year if track.album is not None else None,
        run_time_ticks=(track.duration or 0) * 10_000_000,
        cover_id=track.album.cover if track.album is not None else None,
        index_number=track.track_num or 1,
        parent_index_number=track.volume_num or 1,
        user_data=user_data,
    )


def _get_playlists(search_term: str | None, limit: int = 100):
    if not search_term:
        return []
    return TidalClient().search(search_term, limit=limit, search_type="PLAYLIST").get("playlists") or []


def _get_tracks(search_term: str | None, limit: int = 100):
    if not search_term:
        return TidalClient().new_tracks(limit=limit)
    return TidalClient().search(search_term, limit=limit, search_type="TRACK").get("tracks") or []


def _get_artists(search_term: str | None, limit: int = 100):
    if not search_term:
        return TidalClient().featured_artists(limit=limit)
    return TidalClient().search(search_term, limit=limit, search_type="ARTIST").get("artists") or []


def _get_albums(search_term: str | None, limit: int = 100):
    if not search_term:
        return TidalClient().new_albums(limit=limit)
    return TidalClient().search(search_term, limit=limit, search_type="ALBUM").get("albums") or []


def get_track_stream(id):
    return TidalClient().stream_track(id)


def playlist_tracks_response(playlist_id: str, user_id: str, start_index: int = 0) -> Response:
    """Return the Audio items for a Tidal playlist, in the Jellyfin Items shape."""
    tidal_id = _strip_prefix(playlist_id, "tidal_playlist_") or playlist_id
    tracks = TidalClient().tracks_for_playlist(tidal_id)
    return _wrap([_to_track_item(t, user_id) for t in tracks], start_index)


def _wrap(items: list, start_index: int) -> Response:
    return _return_json(ResultWrapper(
        total_record_count=len(items),
        start_index=start_index,
        items=items,
    ))


def _albums_for_artists(artist_ids: list[str], limit: int):
    client = TidalClient()
    out = []
    for ext_id in artist_ids:
        tidal_id = _strip_prefix(ext_id, "tidal_artist_")
        if not tidal_id:
            continue
        out.extend(client.albums_for_artist(tidal_id, limit=limit))
    return out


def _tracks_for_artists(artist_ids: list[str], limit: int):
    client = TidalClient()
    out = []
    for ext_id in artist_ids:
        tidal_id = _strip_prefix(ext_id, "tidal_artist_")
        if not tidal_id:
            continue
        out.extend(client.top_tracks_for_artist(tidal_id, limit=limit))
    return out


def process_items(request, types: list[str] = None, user_id: str | None = None):

    search_term, include_types, limit, start_index = _get_search_params(request)
    if types is None:
        types = [t.strip() for t in include_types.split(",") if t.strip()]

    user_id = user_id or request.args.get("UserId") or db.DEFAULT_USER_ID

    parent_id = request.args.get("ParentId", "")
    artist_ids_param = request.args.get("AlbumArtistIds") or request.args.get("ArtistIds") or ""
    artist_ids = [a.strip() for a in artist_ids_param.split(",") if a.strip()]

    log.info("items: types=%s search=%r parent=%r artists=%s user=%s limit=%d start=%d",
             types, search_term, parent_id, artist_ids, user_id, limit, start_index)

    if "Playlist" in types:
        playlists = _get_playlists(search_term, limit)
        return _wrap([_to_playlist_item(p, user_id) for p in playlists], start_index)

    if "MusicArtist" in types:
        artists = _get_artists(search_term, limit)
        return _wrap([_to_artist_item(a, user_id) for a in artists], start_index)

    if "MusicAlbum" in types:
        if artist_ids:
            albums = _albums_for_artists(artist_ids, limit)
        elif (artist_tidal_id := _strip_prefix(parent_id, "tidal_artist_")) is not None:
            albums = TidalClient().albums_for_artist(artist_tidal_id, limit=limit)
        else:
            albums = _get_albums(search_term, limit)
        return _wrap([_to_album_item(a, user_id) for a in albums], start_index)

    if "Audio" in types:
        if (album_tidal_id := _strip_prefix(parent_id, "tidal_album_")) is not None:
            tracks = TidalClient().tracks_for_album(album_tidal_id)
        elif (playlist_tidal_id := _strip_prefix(parent_id, "tidal_playlist_")) is not None:
            tracks = TidalClient().tracks_for_playlist(playlist_tidal_id)
        elif artist_ids:
            tracks = _tracks_for_artists(artist_ids, limit)
        elif (artist_tidal_id := _strip_prefix(parent_id, "tidal_artist_")) is not None:
            tracks = TidalClient().top_tracks_for_artist(artist_tidal_id, limit=limit)
        else:
            tracks = _get_tracks(search_term, limit)
        return _wrap([_to_track_item(t, user_id) for t in tracks], start_index)

    return _wrap([], start_index)
