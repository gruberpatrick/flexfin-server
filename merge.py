"""Fold the real Jellyfin library into Tidal results.

server.py routes call in here instead of straight into the Tidal translator. For
a plain browse or search we ask both sources and merge; for a request scoped to
one container (ParentId is an album or artist, a single-item lookup, a playlist)
we route to whichever source owns that id.

Dedupe is deliberately narrow: a Tidal item is dropped in favour of a Jellyfin
one only when the two carry the same real identifier - ISRC for tracks,
barcode/UPC for albums. No identifier, or no match: both entries stay. Fuzzy
matching is a later problem.

Everything degrades to Tidal-only: if the merge is switched off, the session
predates the Jellyfin token, or Jellyfin errors, the Tidal result is returned
unchanged.
"""

import logging
import os

from flask import Response, g

import db
from jellyfin_client import JellyfinClient, JellyfinAuthError, MERGE_ENABLED
from tidal_client import TidalAuthError
from translate.tidal_jellyfin_translator import (
    SERVER_ID, collect_items, collect_playlist_tracks, get_single_item,
    _return_json_dict,
)

log = logging.getLogger(__name__)

# Fixed container ids this proxy invents (server.py builds a single synthetic
# "Music" library). They are not real Jellyfin items, so a request carrying one
# as ParentId is a top-level browse, not a scoped lookup.
LIBRARY_ID = "22222222-2222-2222-2222-222222222222"
ALBUM_ID = "33333333-3333-3333-3333-333333333333"
ARTIST_ID = "44444444-4444-4444-4444-444444444444"
TRACK_ID = "55555555-5555-5555-5555-555555555555"
_SYNTHETIC = {LIBRARY_ID, ALBUM_ID, ARTIST_ID, TRACK_ID}

_KIND_BY_TYPE = {"Audio": "track", "MusicAlbum": "album"}

# Testing aid: when on, every item name is prefixed with its source ("[J] " for
# the real Jellyfin library, "[T] " for Tidal) so it is obvious at a glance what
# the merge pulled from where. Purely cosmetic and off by default.
TAG_SOURCE = os.environ.get("MERGE_TAG_SOURCE", "").strip().lower() \
    in ("1", "true", "yes", "on")
_SOURCE_PREFIX = {"jellyfin": "[J] ", "tidal": "[T] "}


# --------------------------------------------------------------------- dispatch

def _source_of(item_id: str) -> str:
    """Which backend owns this id: 'tidal', 'jellyfin', or 'synthetic'."""
    if not item_id or item_id in _SYNTHETIC:
        return "synthetic"
    if item_id.startswith("tidal_"):
        return "tidal"
    return "jellyfin"


def is_jellyfin_id(item_id: str) -> bool:
    return _source_of(item_id) == "jellyfin"


def _jf_token() -> str | None:
    session = getattr(g, "session", None)
    return session.get("jf_token") if session else None


def _jellyfin_on() -> bool:
    return MERGE_ENABLED and bool(_jf_token())


def _client() -> JellyfinClient:
    return JellyfinClient(_jf_token())


# ---------------------------------------------------------------------- wrapping

def _wrap(items: list[dict], start_index: int, total: int | None = None) -> Response:
    return _return_json_dict({
        "Items": items,
        "TotalRecordCount": len(items) if total is None else total,
        "StartIndex": start_index,
    })


def _stamp(items: list[dict]) -> list[dict]:
    """Present real-Jellyfin items under this proxy's server id, so the client
    sees one server. Image and playback requests still route back here by id."""
    for item in items:
        item["ServerId"] = SERVER_ID
    return _mark(items, "jellyfin")


def _mark(items: list[dict], source: str) -> list[dict]:
    """Prefix each item's name with its source tag, when MERGE_TAG_SOURCE is on."""
    if not TAG_SOURCE:
        return items
    prefix = _SOURCE_PREFIX[source]
    for item in items:
        name = item.get("Name")
        if isinstance(name, str) and not name.startswith(prefix):
            item["Name"] = prefix + name
    return items


# ------------------------------------------------------------------- dedupe keys

def _norm(value) -> str | None:
    return (str(value).strip().upper() or None) if value else None


def _match_key(item: dict, kind: str) -> str | None:
    providers = {k.lower(): v for k, v in (item.get("ProviderIds") or {}).items()}
    if kind == "track":
        return _norm(providers.get("isrc"))
    if kind == "album":
        return _norm(providers.get("barcode") or providers.get("upc")
                     or providers.get("ean"))
    return None


def _merge(jf_items: list[dict], tidal_items: list[dict], kind: str | None) -> list[dict]:
    """Jellyfin items first, then Tidal items, minus any Tidal item that shares
    a real identifier with a Jellyfin one (the local copy wins)."""
    if not jf_items:
        return tidal_items
    if not tidal_items or kind is None:
        return jf_items + tidal_items

    by_key: dict[str, dict] = {}
    for jf in jf_items:
        key = _match_key(jf, kind)
        if key:
            by_key.setdefault(key, jf)

    kept = list(jf_items)
    dropped = 0
    for td in tidal_items:
        key = _match_key(td, kind)
        twin = by_key.get(key) if key else None
        if twin is None:
            kept.append(td)
        else:
            dropped += 1
            db.upsert_source_map(td.get("Id", ""), twin.get("Id", ""), kind, key)
    if dropped:
        log.info("merge: dropped %d Tidal %s(s) with a local twin", dropped, kind)
    return kept


# -------------------------------------------------------------- public entry pts

def browse(request, user_id: str, types: list[str] | None = None) -> Response:
    start_index = int(request.args.get("StartIndex", "0"))
    limit = int(request.args.get("Limit", "50"))
    if types is None:
        types = [t.strip() for t in request.args.get("IncludeItemTypes", "").split(",")
                 if t.strip()]

    parent_id = request.args.get("ParentId", "")
    parent_src = _source_of(parent_id)

    label = ",".join(types) or "*"

    # A request scoped to a real container belongs entirely to that container's
    # source - never merge an album's tracks with unrelated Tidal results.
    if parent_src == "tidal":
        items = _tidal(request, types, user_id)
        log.info("browse %s parent=tidal:%s -> tidal=%d", label, parent_id, len(items))
        return _wrap(items, start_index)
    if parent_src == "jellyfin":
        items = _stamp(_jellyfin(request, types, user_id, parent_id=parent_id))
        log.info("browse %s parent=jellyfin:%s -> jellyfin=%d", label, parent_id, len(items))
        return _wrap(items, start_index)

    jf_items = _stamp(_jellyfin(request, types, user_id))

    try:
        tidal_items = _mark(collect_items(request, types, user_id), "tidal")
    except TidalAuthError:
        if not jf_items:
            raise
        log.warning("Tidal auth failed; serving Jellyfin-only results")
        tidal_items = []
    except Exception:
        log.exception("tidal browse failed")
        tidal_items = []

    kind = next((_KIND_BY_TYPE[t] for t in types if t in _KIND_BY_TYPE), None)
    merged = _merge(jf_items, tidal_items, kind)
    window = merged[start_index:start_index + limit] if limit else merged[start_index:]
    log.info("browse %s -> jellyfin=%d tidal=%d merged=%d returning=%d",
             label, len(jf_items), len(tidal_items), len(merged), len(window))
    return _wrap(window, start_index, total=len(merged))


def single_item(request, item_id: str, user_id: str) -> Response | None:
    src = _source_of(item_id)
    if src == "tidal":
        item = get_single_item(item_id, user_id)
        log.info("item %s -> tidal %s", item_id, "hit" if item is not None else "miss")
        if item is None:
            return None
        return _return_json_dict(_mark([item], "tidal")[0])
    if src == "jellyfin" and _jellyfin_on():
        try:
            item = _client().get_item(item_id, user_id)
        except JellyfinAuthError as exc:
            log.warning("%s", exc)
            return None
        log.info("item %s -> jellyfin %s", item_id, "hit" if item is not None else "miss")
        if item is not None:
            return _return_json_dict(_stamp([item])[0])
    return None


def playlist_items(request, playlist_id: str, user_id: str) -> Response:
    start_index = int(request.args.get("StartIndex", "0"))
    if is_jellyfin_id(playlist_id) and _jellyfin_on():
        try:
            items = _client().get_playlist_items(playlist_id, request.args, user_id)
        except JellyfinAuthError as exc:
            log.warning("%s", exc)
            items = []
        log.info("playlist %s -> jellyfin=%d", playlist_id, len(items))
        return _wrap(_stamp(items), start_index)
    items = _mark(collect_playlist_tracks(playlist_id, user_id), "tidal")
    log.info("playlist %s -> tidal=%d", playlist_id, len(items))
    return _wrap(items, start_index)


def artists(request, user_id: str) -> Response:
    return browse(request, user_id, types=["MusicArtist"])


# ------------------------------------------------------------------- source legs

def _tidal(request, types, user_id) -> list[dict]:
    try:
        return _mark(collect_items(request, types, user_id), "tidal")
    except TidalAuthError:
        raise
    except Exception:
        log.exception("tidal browse failed")
        return []


def _jellyfin(request, types, user_id, parent_id: str | None = None) -> list[dict]:
    if not _jellyfin_on():
        log.info("jellyfin leg skipped: %s",
                 "MERGE_LOCAL_LIBRARY is off" if not MERGE_ENABLED
                 else "no jf_token on this session (pre-merge login?)")
        return []
    client = _client()
    # A route that pins the type (server.py passes types=[...]) must constrain
    # the Jellyfin call too, since request.args may carry no IncludeItemTypes.
    include_types = ",".join(types) if types else None
    try:
        if "MusicArtist" in (types or []):
            return client.get_artists(request.args, user_id)
        return client.get_items(request.args, user_id, parent_id=parent_id,
                                include_types=include_types)
    except JellyfinAuthError as exc:
        log.warning("%s; Jellyfin library skipped for this request", exc)
        return []
    except Exception:
        log.exception("jellyfin browse failed")
        return []
