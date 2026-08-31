"""
Jellyfin API proxy that serves a merged library: the user's real Jellyfin
library plus Tidal.

All the routes live here; `main.py` is the entrypoint (or `gunicorn server:app`).
Browse and search go through merge.py, which asks both sources and folds them
together. Audio and images never cross this process - a Tidal item redirects to
Tidal's CDN, a local item redirects to the Jellyfin server (JELLYFIN_PUBLIC_URL).

Login is delegated to a real Jellyfin instance - set JELLYFIN_URL and sign in
with those credentials. The access token Jellyfin issues is kept and reused to
read that user's library. Every endpoint except /System/Info, /System/Ping and
/Users/AuthenticateByName requires the token handed out at login.

Environment:
    JELLYFIN_URL            Jellyfin base URL, used for login + library reads
    JELLYFIN_PUBLIC_URL     Jellyfin URL the client uses for audio/images
                            (default: JELLYFIN_URL)
    MERGE_LOCAL_LIBRARY     "false" to serve Tidal only (default true)
    JELLYFIN_API_TIMEOUT    seconds to wait on a library call (default 10)
    PROXY_DB_PATH           sqlite cache + sessions   (default ./data/proxy.db)
    LIKED_TRACKS_DIR        marker files for likes    (default /mnt/Main/Apps/music_files)
    TIDAL_DOWNLOAD_QUALITY  tier used for downloads   (default LOSSLESS)
    PROXY_TOKEN_TTL_DAYS    session lifetime          (default 30)
"""

import json
import os
import re
import threading
import uuid
import requests
from dotenv import load_dotenv
from flask import Flask, g, jsonify, request, Response, abort, redirect, stream_with_context
from flask import Request as FlaskRequest
from werkzeug.datastructures import ImmutableMultiDict
import logging

# Load ./.env before importing anything that reads os.environ at import time
# (auth.py, jellyfin_client.py). Real environment variables always win over the
# file, so this is a convenience for local runs, not an override.
load_dotenv()

from schema.jellyfin import (
    AuthResponse, User, SessionInfo, SystemInfo, Album, Artist, Track, ResultWrapper, UserData, MediaSource
)

import auth
import db
import like_markers
import merge
from jellyfin_client import JellyfinClient
from merge import LIBRARY_ID
from translate.tidal_jellyfin_translator import (
    get_track_stream, get_track_download_url, SERVER_ID,
)

app = Flask(__name__)
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s",)
log = logging.getLogger("subsonic-proxy")


class _CIMultiDict(ImmutableMultiDict):
    """Query/form args looked up without regard to key case.

    Real Jellyfin treats query parameters case-insensitively, and its generated
    SDKs send the camelCase spelling from the OpenAPI spec (`searchTerm`,
    `includeItemTypes`, `parentId`); hand-written clients tend to send
    PascalCase. This proxy reads PascalCase throughout, so without this a
    camelCase client (Jellify, Swiftfin) silently loses its search term and
    type filter and every browse looks like a bare "list everything".
    """

    def __init__(self, mapping=None):
        super().__init__(mapping)
        self._canonical = {}
        for key in super().keys():
            self._canonical.setdefault(key.lower(), key)

    def _key(self, key):
        if isinstance(key, str):
            return self._canonical.get(key.lower(), key)
        return key

    def __getitem__(self, key):
        return super().__getitem__(self._key(key))

    def get(self, key, default=None, type=None):
        return super().get(self._key(key), default, type)

    def getlist(self, key, type=None):
        return super().getlist(self._key(key), type)

    def __contains__(self, key):
        return super().__contains__(self._key(key))


class _CIRequest(FlaskRequest):
    parameter_storage_class = _CIMultiDict


app.request_class = _CIRequest

# Tokens ride in the query string (see MediaSource.hls_for), and both werkzeug
# and gunicorn log the whole request line - so scrub them before anything is
# written, or the access log becomes a list of working credentials.
auth.install_log_redaction()


class _NormalisePath:
    """Collapse repeated slashes before routing.

    Clients build URLs by concatenating a base address with a path and end up
    posting to things like `http://host:30808//users/<id>/favoriteitems/<id>`.
    Werkzeug answers a doubled slash with a 308 redirect, which not every client
    replays as a POST, so the write silently never happens.
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if "//" in path:
            environ["PATH_INFO"] = re.sub(r"/{2,}", "/", path)
        return self.wsgi_app(environ, start_response)


app.wsgi_app = _NormalisePath(app.wsgi_app)


# Browser-based clients only. A native client has no same-origin policy to
# satisfy, so nothing outside a browser is affected by any of this.
CORS_ALLOW_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "*")

# Every header a Jellyfin client might present a token in. A browser refuses to
# send a header the preflight did not name, so an omission here shows up as a
# 401 rather than as a CORS error, which is a slow thing to debug.
_CORS_ALLOW_HEADERS = ", ".join([
    "Authorization",
    "Content-Type",
    "X-Emby-Authorization",
    "X-Emby-Token",
    "X-MediaBrowser-Token",
])


@app.after_request
def _allow_cross_origin(response):
    """Let a browser-hosted client call this proxy.

    Safe with a wildcard origin because authentication is a bearer token in a
    header, never a cookie: we never set Access-Control-Allow-Credentials, so a
    hostile page can only make unauthenticated calls, which get a 401. Set
    CORS_ALLOW_ORIGIN to pin it to one origin if you would rather be strict.
    """
    response.headers["Access-Control-Allow-Origin"] = CORS_ALLOW_ORIGIN
    response.headers["Access-Control-Allow-Headers"] = _CORS_ALLOW_HEADERS
    response.headers["Access-Control-Allow-Methods"] = \
        "GET, POST, DELETE, OPTIONS"
    # Without this the browser re-preflights every request, which doubles the
    # request count on a library that is mostly small JSON calls.
    response.headers["Access-Control-Max-Age"] = "86400"
    if CORS_ALLOW_ORIGIN != "*":
        # Tell caches the body varies by origin, or a shared cache can serve
        # one origin's response to another.
        response.headers["Vary"] = "Origin"
    return response


def _register_case_insensitive_aliases(flask_app):
    """Mirror every route with its path segments lowercased.

    Flask routing is case-sensitive but Jellyfin clients are not consistent
    about casing: Finamp posts likes to `/users/<id>/favoriteitems/<id>` while
    the API documents `/Users/<id>/FavoriteItems/<id>`. Without an alias that
    404s, which is why favourites and scrobbles stopped being recorded.

    Only literal segments are lowered; `<converter:name>` placeholders are left
    alone so item ids keep their original casing.
    """
    # Keyed by endpoint as well as path: a path can be registered more than
    # once with different methods (FavoriteItems is POST on one view and DELETE
    # on another), and keying on the path alone would alias only the first.
    existing = {(rule.rule, rule.endpoint) for rule in flask_app.url_map.iter_rules()}

    for rule in list(flask_app.url_map.iter_rules()):
        lowered = "/".join(
            segment if segment.startswith("<") else segment.lower()
            for segment in rule.rule.split("/")
        )
        if (lowered, rule.endpoint) in existing:
            continue
        existing.add((lowered, rule.endpoint))
        flask_app.add_url_rule(
            lowered,
            endpoint=rule.endpoint,
            view_func=flask_app.view_functions[rule.endpoint],
            methods=sorted(rule.methods - {"HEAD", "OPTIONS"}),
        )

# ---- Fixed IDs so clients can cache consistently ----
# The user id is no longer among them: it comes from the Jellyfin instance that
# authenticated the request. db.DEFAULT_USER_ID is only the pre-auth placeholder
# that remap_user.py migrates away from. LIBRARY_ID and the other synthetic
# container ids live in merge.py, which needs them to tell a top-level browse
# from a request scoped to a real Jellyfin container.


def _return_json(model: any) -> Response:
    return Response(
        model.model_dump_json(by_alias=True),
        mimetype="application/json",
    )


def _user_id_from(route_user_id=None, body_user_id=None) -> str:
    """Whose data is this request about?

    The authenticated identity wins outright: the user id in the path, query or
    body is client-supplied, and honouring it would let any logged-in user read
    and write another user's favourites. The client-supplied chain survives only
    as a fallback for the unauthenticated case, where the caller can only be
    reaching a public endpoint anyway.
    """
    if (session := getattr(g, "session", None)) is not None:
        return session["user_id"]
    return (route_user_id
            or body_user_id
            or request.args.get("UserId")
            or request.args.get("userId")
            or db.DEFAULT_USER_ID)


@app.before_request
def _require_token():
    """Gate everything that isn't explicitly public.

    Keyed on the Flask endpoint name, so the lowercase route aliases and every
    method variant are covered without being listed again.
    """
    g.session = None
    g.token = None
    if request.method == "OPTIONS" or request.endpoint in auth.PUBLIC_ENDPOINTS:
        return None
    if request.endpoint is None:          # unmatched path; let Flask 404 it
        return None

    token = auth.token_from_request(request)
    session = db.lookup_session(token or "")
    if session is None:
        # Say which failure it was: "client never sent one" and "client sent a
        # stale one" need different fixes, and the path alone can't tell them
        # apart. request.path carries no query string, so no token leaks here.
        log.info("401 %s %s from %s (%s)", request.method, request.path,
                 auth.client_ip(request),
                 "no token presented" if not token
                 else "token not recognised or expired")
        return _unauthorised("Missing or invalid token.")
    g.session = session
    g.token = token
    return None


def _unauthorised(message: str, status: int = 401) -> Response:
    return Response(
        json.dumps({"Message": message}),
        status=status,
        mimetype="application/json",
        headers={"X-Emby-Authentication-Required": "true"},
    )


def _get_search_params():
    """
    GET /Artists/AlbumArtists?ParentId=22222222-2222-2222-2222-222222222222&Recursive=true&SortBy=SortName&SortOrder=Ascending&Fields=ChildCount,DateCreated,DateLastMediaAdded,Etag,Genres,IndexNumber,ParentId,ProviderIds,Tags,albumPrimaryImageTag,parentPrimaryImageItemId,songCount&SearchTerm=never%20gonna%20gove&EnableUserData=true&StartIndex=0&Limit=100&UserId=11111111-1111-1111-1111-111111111111
    """
    search_term = request.args.get("SearchTerm", "").strip()
    include_types = request.args.get("IncludeItemTypes", "")
    limit = int(request.args.get("Limit", "50"))
    start_index = int(request.args.get("StartIndex", "0"))
    # request.args.get("Fields")
    return search_term, include_types, limit, start_index


# ============================================================
# Server discovery / info
# ============================================================

@app.route("/System/Info/Public")
@app.route("/System/Info")
@app.route("/system/info/public")
@app.route("/system/info")
def system_info_public():
    return _return_json(SystemInfo(
        id=SERVER_ID,
        local_address=request.host_url.rstrip("/"),
        server_name="FlexProxy",
        version="10.9.0",
        product_name="Jellyfin Server",
        operating_system="Linux",
        startup_wizard_completed=True
    ))


@app.route("/System/Ping", methods=["GET", "POST"])
@app.route("/system/ping", methods=["GET", "POST"])
def ping():
    return Response("Jellyfin Server", mimetype="text/plain")


# ============================================================
# Auth
# ============================================================

@app.route("/Users/AuthenticateByName", methods=["POST"])
@app.route("/users/authenticatebyname", methods=["POST"])
def authenticate():
    """Verify the credentials against the real Jellyfin, then mint our token.

    We hold no passwords and no user records of our own: Jellyfin decides, and
    all we keep is the token it earned.
    """
    body = request.get_json(force=True, silent=True) or {}
    username = body.get("Username") or body.get("username") or ""
    password = body.get("Pw") or body.get("Password") or ""
    # Not remote_addr: behind cloudflared every remote client arrives from the
    # tunnel, so limiting on it would put the whole internet in one bucket.
    client = auth.client_ip(request)

    if not auth.is_configured():
        log.error("login attempt but JELLYFIN_URL is unset")
        return _unauthorised("This proxy has no Jellyfin configured.", status=503)

    if auth.is_locked_out(client):
        log.warning("login from %s locked out after repeated failures", client)
        return _unauthorised("Too many failed attempts. Try again later.", status=429)

    identity = auth.verify_credentials(username, password,
                                       device_id=f"flexproxy-{client}")
    if identity is None:
        remaining = auth.record_failure(client)
        log.info("login failed for %r from %s (%d attempts left)",
                 username, client, remaining)
        return _unauthorised("Invalid username or password.")

    token = auth.issue_token(identity["user_id"], identity["user_name"],
                             identity.get("jf_token"))
    if token is None:
        return _unauthorised("Could not start a session.", status=500)

    auth.clear_failures(client)
    # Cheap and infrequent: a login is the natural moment to drop rows for
    # tokens nobody came back to use.
    if (purged := db.purge_expired_sessions()):
        log.info("purged %d expired session(s)", purged)
    return _return_json(AuthResponse(
        user=User(
            id=identity["user_id"],
            name=identity["user_name"],
            server_id=SERVER_ID,
        ),
        session_info=SessionInfo(
            id=str(uuid.uuid4()),
            user_id=identity["user_id"],
            user_name=identity["user_name"],
            server_id=SERVER_ID,
            remote_end_point=request.remote_addr,
        ),
        access_token=token,
        server_id=SERVER_ID,
    ))


@app.route("/Sessions/Logout", methods=["POST"])
def logout():
    token = auth.token_from_request(request)
    if token:
        db.delete_session(token)
    return Response(status=204)


@app.route("/Users/Me")
@app.route("/Users/<user_id>")
@app.route("/users/me")
@app.route("/users/<user_id>")
def get_user(user_id=None):
    # user_id is ignored on purpose - a token identifies exactly one user, and
    # the /Users/Me route carries no id at all.
    return _return_json(User(
        id=g.session["user_id"],
        name=g.session["user_name"],
        server_id=SERVER_ID,
    ))


# ============================================================
# Library browsing
# ============================================================

@app.route("/Users/<user_id>/Views")
@app.route("/UserViews")
def user_views(user_id=None):
    return jsonify({
        "Items": [{
            "Name": "Music",
            "ServerId": SERVER_ID,
            "Id": LIBRARY_ID,
            "IsFolder": True,
            "Type": "CollectionFolder",
            "CollectionType": "music",
            "UserData": UserData().model_dump(by_alias=True),
        }],
        "TotalRecordCount": 1,
        "StartIndex": 0,
    })


@app.route("/Users/<user_id>/Items/Root")
def root_items(user_id):
    return jsonify({
        "Name": "Root",
        "ServerId": SERVER_ID,
        "Id": "root",
        "IsFolder": True,
        "Type": "Folder",
    })


@app.route("/Items", methods=["GET"])
@app.route("/Users/<user_id>/Items", methods=["GET"])
@app.route("/items", methods=["GET"])
@app.route("/users/<user_id>/items", methods=["GET"])
def items(user_id=None):
    """
    Handles the big catchall endpoint Finamp uses to browse the library.
    Returns albums or tracks depending on IncludeItemTypes, merged from the
    real Jellyfin library and Tidal.
    """
    return merge.browse(request, _user_id_from(user_id))


@app.route("/Users/<user_id>/Items/<item_id>")
@app.route("/Items/<item_id>")
@app.route("/users/<user_id>/items/<item_id>")
@app.route("/items/<item_id>")
def get_item(item_id, user_id=None):
    uid = _user_id_from(user_id)
    response = merge.single_item(request, item_id, uid)
    if response is not None:
        return response
    return merge.browse(request, uid)


@app.route("/Items/Filters")
@app.route("/items/filters")
def items_filters():
    """Available filter values. We don't track tags/ratings/years, so return empty."""
    return jsonify({
        "Genres": [],
        "Tags": [],
        "OfficialRatings": [],
        "Years": [],
    })


@app.route("/Albums/<album_id>/Similar")
@app.route("/Artists/<artist_id>/Similar")
def similar(album_id=None, artist_id=None):
    return jsonify({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


@app.route("/Artists")
@app.route("/Artists/AlbumArtists")
@app.route("/artists/albumArtists")
def album_artists():
    return merge.artists(request, _user_id_from())


# ============================================================
# Playback
# ============================================================

def _jellyfin_stream_client() -> JellyfinClient:
    return JellyfinClient(g.session["jf_token"] if getattr(g, "session", None) else None)


@app.route("/Items/<item_id>/PlaybackInfo", methods=["GET", "POST"])
def playback_info(item_id):
    # A local (real-Jellyfin) item: point the client straight at the Jellyfin
    # server for the bytes, with our own /Audio route as the fallback for
    # clients that ignore the MediaSource URL. Runtime is already on the item
    # itself from the browse response, so it isn't repeated here.
    if merge.is_jellyfin_id(item_id):
        client = _jellyfin_stream_client()
        # Progressive, not HLS: just_audio_web has no hls.js and Chrome has no
        # native HLS, so a playlist URL fails on web. Our /Audio/<id>/universal
        # route 302s to Jellyfin's direct-stream (a seekable single file), which
        # a browser <audio> and mpv both play.
        transcoding_url = f"/Audio/{item_id}/universal?MediaSourceId={item_id}"
        if g.token:
            transcoding_url += f"&api_key={g.token}"
        source = MediaSource(
            id=item_id,
            path=client.stream_url(item_id),
            protocol="Http",
            is_remote=True,
            supports_direct_stream=True,
            supports_direct_play=True,
            transcoding_url=transcoding_url,
            transcoding_sub_protocol="http",
        )
        return jsonify({
            "MediaSources": [source.model_dump(by_alias=True)],
            "PlaySessionId": str(uuid.uuid4()),
        })

    # Duration was previously reported as 0, which is enough on its own to make
    # some clients refuse a track. We know it for anything the translator has
    # surfaced, so use it.
    duration = db.get_track_duration(item_id) or 0

    # A real call to Tidal, so the client is told the truth about whether this
    # track is actually segmented before it commits to an HLS player for it.
    kind = "hls"
    if item_id.startswith("tidal_track_"):
        stream = get_track_stream(item_id[len("tidal_track_"):])
        kind = stream["kind"]

    source = MediaSource.hls_for(item_id, run_time_ticks=duration * 10_000_000,
                                 api_key=g.token, kind=kind)
    return jsonify({
        "MediaSources": [source.model_dump(by_alias=True)],
        "PlaySessionId": str(uuid.uuid4()),
    })


def _resolve_stream(item_id):
    if not item_id.startswith("tidal_track_"):
        return None
    return get_track_stream(item_id[len("tidal_track_"):])


def _stream_response(stream):
    """Serve whatever /PlaybackInfo promised: a real HLS playlist, or a plain
    redirect for tracks Tidal never segmented in the first place."""
    if stream["kind"] == "direct":
        return redirect(stream["url"])
    return Response(stream["hls"], mimetype="application/vnd.apple.mpegurl")


@app.route("/Audio/<item_id>/main.m3u8")
@app.route("/Audio/<item_id>/master.m3u8")
@app.route("/Audio/<item_id>/stream.m3u8")
def audio_hls(item_id):
    """HLS playlist. Segment URLs point straight at Tidal's CDN. A local item
    is a 302 to the Jellyfin server, which owns the bytes."""
    if merge.is_jellyfin_id(item_id):
        return redirect(_jellyfin_stream_client().stream_url(item_id))
    stream = _resolve_stream(item_id)
    if stream is None:
        return Response(status=404)
    return _stream_response(stream)


@app.route("/Audio/<item_id>/stream.mp4")
@app.route("/Audio/<item_id>/stream.m4a")
@app.route("/Audio/<item_id>/stream")
@app.route("/Audio/<item_id>/universal")
def audio_direct(item_id):
    """Progressive playback for tracks with no segmented rendition. Also
    accepts the two generic Jellyfin routes, for clients that hit those
    without checking /PlaybackInfo first. A local item is a 302 to Jellyfin."""
    if merge.is_jellyfin_id(item_id):
        return redirect(_jellyfin_stream_client().stream_url(item_id))
    stream = _resolve_stream(item_id)
    if stream is None:
        return Response(status=404)
    return _stream_response(stream)


@app.route("/Items/<item_id>/File")
@app.route("/Items/<item_id>/Download")
def audio_download(item_id):
    """Hand the client a URL on Tidal's CDN and stay out of the byte path.

    The CDN honours Range and sends Content-Length, which is what download
    managers need. Concatenating DASH segments through here instead produced a
    chunked 200 with neither, which Finamp rejects outright.

    Note this deliberately does NOT return the HLS playlist that playback uses,
    even though that would keep the audio lossless. Finamp's downloader wants a
    single seekable file and will not assemble segments itself, so it needs one
    URL it can range-request. The cost is real: only the lossy tier is served
    as a single file (HI_RES_LOSSLESS is DASH only), so offline copies are AAC
    while streaming stays 24-bit FLAC.

    A client that can fetch and stitch the segments itself could be handed the
    m3u8 here instead and keep lossless offline - the intended direction once
    the Flexfin client can do that. Serving a local copy with send_file is the
    other way to close the same gap; both belong on this branch.

    A local (real-Jellyfin) item is a 302 to the Jellyfin server's own download
    URL, which already gives the range support and Content-Length a download
    manager needs.
    """
    if merge.is_jellyfin_id(item_id):
        return redirect(_jellyfin_stream_client().download_url(item_id))
    if not item_id.startswith("tidal_track_"):
        return Response(status=404)
    track_id = item_id[len("tidal_track_"):]

    url = get_track_download_url(track_id)
    if url is None:
        # Tidal has no single-file rendition for this track. Fall back to
        # stitching the segments so the download isn't a hard failure, but this
        # response has no length and no Range support - see the warning logged
        # by download_url().
        seg_urls = get_track_stream(track_id)["seg_urls"]

        def gen():
            for seg_url in seg_urls:
                with requests.get(seg_url, stream=True, timeout=30) as r:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            yield chunk

        return Response(stream_with_context(gen()), mimetype="audio/mp4")

    return redirect(url)


# ============================================================
# Images (return a transparent 1x1 so clients don't crash)
# ============================================================

_TRANSPARENT_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c626001000000050001a5f645400000000049454e44ae426082"
)


@app.route("/Items/<item_id>/Images/<image_type>")
@app.route("/Items/<item_id>/Images/<image_type>/<int:image_index>")
def item_image(item_id, image_type, image_index=0):
    # A local (real-Jellyfin) item: 302 to the Jellyfin server, which serves its
    # own art anonymously. Needs JELLYFIN_PUBLIC_URL to be reachable by the
    # client - see the README.
    if merge.is_jellyfin_id(item_id):
        return redirect(JellyfinClient.image_url(item_id, image_type, request.args))

    # image_type comes out of the path, so it arrives lowercased via the
    # lowercase route aliases as well as capitalised.
    if image_type.lower() != "primary":
        return Response(_TRANSPARENT_PNG, mimetype="image/png")

    cover = request.args.get("tag") or db.get_cover_uuid(item_id)
    if not cover or "-" not in cover:
        return Response(_TRANSPARENT_PNG, mimetype="image/png")

    size = "320x320" if item_id.startswith("tidal_artist_") else "1280x1280"
    return redirect(f"https://resources.tidal.com/images/{cover.replace('-', '/')}/{size}.jpg")


# ============================================================
# Sessions (scrobbling — accept and ignore)
# ============================================================

def _user_data(uid: str, item_id: str) -> UserData:
    count = db.get_play_count(uid, item_id)
    return UserData(
        is_favorite=db.is_favorite(uid, item_id),
        play_count=count,
        played=count > 0,
        key=item_id,
    )


@app.route("/Users/<user_id>/FavoriteItems/<item_id>", methods=["POST"])
@app.route("/UserFavoriteItems/<item_id>", methods=["POST"])
def add_favorite(item_id, user_id=None):
    uid = _user_id_from(user_id)
    ok = db.set_favorite(uid, item_id)
    marked = like_markers.write(item_id)
    log.info("favorite+ %s user=%s written=%s marker=%s", item_id, uid, ok, marked)
    return _return_json(_user_data(uid, item_id))


@app.route("/Users/<user_id>/FavoriteItems/<item_id>", methods=["DELETE"])
@app.route("/UserFavoriteItems/<item_id>", methods=["DELETE"])
def remove_favorite(item_id, user_id=None):
    uid = _user_id_from(user_id)
    ok = db.unset_favorite(uid, item_id)
    log.info("favorite- %s user=%s written=%s", item_id, uid, ok)
    return _return_json(_user_data(uid, item_id))


@app.route("/Users/<user_id>/PlayedItems/<item_id>", methods=["POST", "DELETE"])
@app.route("/UserPlayedItems/<item_id>", methods=["POST", "DELETE"])
def played_item(item_id, user_id=None):
    """Clients let you mark a track played by hand, separately from scrobbling."""
    uid = _user_id_from(user_id)
    if request.method == "DELETE":
        ok = db.clear_play(uid, item_id)
    else:
        ok = db.record_play(uid, item_id)
    log.info("played%s %s user=%s written=%s",
             "-" if request.method == "DELETE" else "+", item_id, uid, ok)
    return _return_json(_user_data(uid, item_id))


# Finamp reports progress while a track plays, but the final "Stopped" report
# can arrive with PositionTicks=0 because the player has already reset. Keep the
# furthest position seen per track so the stop handler can still score it.
_progress_lock = threading.Lock()
_furthest_position: dict[tuple[str, str], int] = {}
_MAX_TRACKED = 512


def _playback_report():
    body = request.get_json(force=True, silent=True) or {}
    return (
        body.get("ItemId") or body.get("itemId") or "",
        int(body.get("PositionTicks") or 0),
        int(body.get("RunTimeTicks") or 0) or None,
        _user_id_from(body_user_id=body.get("UserId") or body.get("userId")),
    )


@app.route("/Sessions/Playing", methods=["POST"])
@app.route("/Sessions/Playing/Progress", methods=["POST"])
def session_progress():
    item_id, position_ticks, _runtime_ticks, user_id = _playback_report()
    if item_id:
        with _progress_lock:
            key = (user_id, item_id)
            _furthest_position[key] = max(position_ticks,
                                          _furthest_position.get(key, 0))
            while len(_furthest_position) > _MAX_TRACKED:
                _furthest_position.pop(next(iter(_furthest_position)))
    return Response(status=204)


@app.route("/Sessions/Playing/Stopped", methods=["POST"])
def session_stopped():
    item_id, position_ticks, runtime_ticks, user_id = _playback_report()
    if not item_id:
        return Response(status=204)
    with _progress_lock:
        furthest = max(position_ticks,
                       _furthest_position.pop((user_id, item_id), 0))
    recorded = db.maybe_record_play(user_id, item_id, furthest, runtime_ticks)
    log.info("stop %s user=%s pos=%d furthest=%d runtime=%s recorded=%s",
             item_id, user_id, position_ticks, furthest, runtime_ticks, recorded)
    return Response(status=204)


@app.route("/Proxy/Stats")
def proxy_stats():
    """Row counts, last write per table, and the dropped-write counter."""
    return jsonify(db.stats())


# ============================================================
# Misc endpoints clients probe
# ============================================================

@app.route("/DisplayPreferences/<pref_id>")
def display_prefs(pref_id):
    return jsonify({
        "Id": pref_id,
        "SortBy": "SortName",
        "RememberIndexing": False,
        "PrimaryImageHeight": 250,
        "PrimaryImageWidth": 250,
        "CustomPrefs": {},
        "ScrollDirection": "Horizontal",
        "ShowBackdrop": True,
        "RememberSorting": False,
        "SortOrder": "Ascending",
        "ShowSidebar": False,
        "Client": "emby",
    })


@app.route("/Genres")
@app.route("/MusicGenres")
def genres():
    log.info("genres called with %s ", _get_search_params())
    return jsonify({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


@app.route("/Playlists")
def playlists():
    # Tidal only surfaces playlists for a search term; the real Jellyfin lists
    # the user's own, so this endpoint is now useful even with no SearchTerm.
    return merge.browse(request, _user_id_from(), types=["Playlist"])


@app.route("/Playlists/<playlist_id>/Items")
def playlist_items(playlist_id):
    return merge.playlist_items(request, playlist_id, _user_id_from())


# Must come last: it mirrors whatever is registered above.
_register_case_insensitive_aliases(app)


if __name__ == "__main__":
    # Prefer `python main.py` (it does a couple of startup checks first); this
    # stays so `python server.py` still works.
    app.run(host="0.0.0.0", port=8096, debug=True)
