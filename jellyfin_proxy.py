"""
Minimal Jellyfin API proxy for testing HLS playback in Finamp.

Returns a single hardcoded album/track. The track streams as HLS.
Point Finamp at: http://localhost:8096
Login: user=test, password=test (password is ignored)
"""

import uuid
from flask import Flask, jsonify, request, Response, abort, redirect
import logging

from schema.jellyfin import (
    AuthResponse, User, SessionInfo, SystemInfo, Album, Artist, Track, ResultWrapper, UserData, MediaSource
)

import db
from translate.tidal_jellyfin_translator import process_items, get_track_stream, playlist_tracks_response, SERVER_ID

app = Flask(__name__)
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s",)
log = logging.getLogger("subsonic-proxy")

# ---- Fixed IDs so clients can cache consistently ----
USER_ID = "11111111-1111-1111-1111-111111111111"
LIBRARY_ID = "22222222-2222-2222-2222-222222222222"
ALBUM_ID = "33333333-3333-3333-3333-333333333333"
ARTIST_ID = "44444444-4444-4444-4444-444444444444"
TRACK_ID = "55555555-5555-5555-5555-555555555555"


def _return_json(model: any) -> Response:
    return Response(
        model.model_dump_json(by_alias=True),
        mimetype="application/json",
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
    body = request.get_json(force=True, silent=True) or {}
    username = body.get("Username", "test")
    return _return_json(AuthResponse(
        user=User(
            id=USER_ID,
            name=username,
            server_id=SERVER_ID,
        ),
        session_info=SessionInfo(
            id=str(uuid.uuid4()),
            user_id=USER_ID,
            user_name=username,
            server_id=SERVER_ID,
            remote_end_point=request.remote_addr,
        ),
        access_token="test-token",
        server_id=SERVER_ID,
    ))


@app.route("/Users/Me")
@app.route("/Users/<user_id>")
@app.route("/users/me")
@app.route("/users/<user_id>")
def get_user(user_id):
    return _return_json(User(
        id=USER_ID,
        name="test",
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
    Returns albums or tracks depending on IncludeItemTypes.
    """
    return process_items(request, user_id=user_id)


@app.route("/Users/<user_id>/Items/<item_id>")
@app.route("/Items/<item_id>")
def get_item(item_id, user_id=None):
    return process_items(request, user_id=user_id)


@app.route("/Albums/<album_id>/Similar")
@app.route("/Artists/<artist_id>/Similar")
def similar(album_id=None, artist_id=None):
    return jsonify({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


@app.route("/Artists")
@app.route("/Artists/AlbumArtists")
@app.route("/artists/albumArtists")
def album_artists():
    return process_items(request, types=["MusicArtist"])


# ============================================================
# Playback
# ============================================================

@app.route("/Items/<item_id>/PlaybackInfo", methods=["GET", "POST"])
def playback_info(item_id):
    return jsonify({
        "MediaSources": [MediaSource(id=item_id).model_dump(by_alias=True)],
        "PlaySessionId": str(uuid.uuid4()),
    })


@app.route("/Audio/<item_id>/main.m3u8")
@app.route("/Audio/<item_id>/master.m3u8")
@app.route("/Audio/<item_id>/stream.m3u8")
@app.route("/Audio/<item_id>/stream")
@app.route("/Audio/<item_id>/universal")
@app.route("/Items/<item_id>/File")
@app.route("/Items/<item_id>/Download")
def audio_hls(item_id):
    """Return HLS playlist. Redirect to the actual source."""

    if not item_id.startswith("tidal_track_"):
        return Response(status=404)

    track_id = item_id[len("tidal_track_"):]

    stream = get_track_stream(track_id)
    if stream.get("hls") is not None:
        return Response(stream.get("hls"), mimetype="application/vnd.apple.mpegurl")

    return redirect(stream.get("seg_urls")[0])


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
    if image_type != "Primary":
        return Response(_TRANSPARENT_PNG, mimetype="image/png")

    cover = request.args.get("tag") or db.get_cover_uuid(item_id)
    if not cover or "-" not in cover:
        return Response(_TRANSPARENT_PNG, mimetype="image/png")

    size = "320x320" if item_id.startswith("tidal_artist_") else "1280x1280"
    return redirect(f"https://resources.tidal.com/images/{cover.replace('-', '/')}/{size}.jpg")


# ============================================================
# Sessions (scrobbling — accept and ignore)
# ============================================================

@app.route("/Users/<user_id>/FavoriteItems/<item_id>", methods=["POST"])
def add_favorite(user_id, item_id):
    uid = user_id or db.DEFAULT_USER_ID
    db.set_favorite(uid, item_id)
    return _return_json(UserData(is_favorite=True))


@app.route("/Users/<user_id>/FavoriteItems/<item_id>", methods=["DELETE"])
def remove_favorite(user_id, item_id):
    uid = user_id or db.DEFAULT_USER_ID
    db.unset_favorite(uid, item_id)
    return _return_json(UserData(is_favorite=False))


@app.route("/Sessions/Playing", methods=["POST"])
@app.route("/Sessions/Playing/Progress", methods=["POST"])
def session_noop():
    return Response(status=204)


@app.route("/Sessions/Playing/Stopped", methods=["POST"])
def session_stopped():
    body = request.get_json(force=True, silent=True) or {}
    item_id = body.get("ItemId") or ""
    position_ticks = int(body.get("PositionTicks") or 0)
    user_id = request.args.get("UserId") or db.DEFAULT_USER_ID
    if item_id:
        recorded = db.maybe_record_play(user_id, item_id, position_ticks)
        log.info("stop %s user=%s pos=%d recorded=%s", item_id, user_id, position_ticks, recorded)
    return Response(status=204)


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
    return jsonify({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


@app.route("/Playlists/<playlist_id>/Items")
def playlist_items(playlist_id):
    user_id = request.args.get("UserId") or db.DEFAULT_USER_ID
    start_index = int(request.args.get("StartIndex", "0"))
    return playlist_tracks_response(playlist_id, user_id=user_id, start_index=start_index)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8096, debug=True)
