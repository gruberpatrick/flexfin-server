"""Read the user's real Jellyfin library through the token their login earned.

auth.py verifies a password against JELLYFIN_URL and keeps the AccessToken
Jellyfin minted (sessions.jf_token). This module reuses that token to call the
same Jellyfin instance as that user, so the merge layer can fold the local
library in with Tidal results.

Every call is best effort. A Jellyfin that is unreachable, slow, or has revoked
the token must not fail the request: failures log and return None / [], and the
caller falls back to Tidal only. The one exception is a 401, raised as
JellyfinAuthError so the caller can tell "token dead" from "library empty".

Audio and images are never proxied. The client is handed a URL on the Jellyfin
server itself (JELLYFIN_PUBLIC_URL) and fetches the bytes directly, exactly as
Tidal playback redirects to Tidal's CDN.
"""

import logging
import os
import urllib.parse

import requests

from auth import JELLYFIN_URL

log = logging.getLogger(__name__)

# Base URL a *client* uses to reach Jellyfin for audio and images. Defaults to
# the same address used for the API; set it when clients live outside the
# Jellyfin LAN and need a publicly reachable host for the stream/image redirects.
JELLYFIN_PUBLIC_URL = os.environ.get("JELLYFIN_PUBLIC_URL", JELLYFIN_URL).rstrip("/")
API_TIMEOUT = float(os.environ.get("JELLYFIN_API_TIMEOUT", "10"))

# Master switch. MERGE_LOCAL_LIBRARY=false reverts to Tidal-only behaviour.
MERGE_ENABLED = os.environ.get("MERGE_LOCAL_LIBRARY", "true").strip().lower() \
    not in ("0", "false", "no", "off")

_AUTH_HEADER = (
    'MediaBrowser Client="FlexProxy", Device="FlexProxy", '
    'DeviceId="flexproxy-merge", Version="10.9.0"'
)

# Query parameters forwarded verbatim to Jellyfin. Whatever the client sent that
# Jellyfin understands - sort, fields, paging, filters, and SearchTerm so search
# hits the real library too - rides along.
_FORWARD = (
    "IncludeItemTypes", "SearchTerm", "SortBy", "SortOrder", "Fields",
    "StartIndex", "Limit", "ArtistIds", "AlbumArtistIds", "Genres", "GenreIds",
    "Years", "NameStartsWith", "Filters", "IsFavorite",
)


class JellyfinAuthError(RuntimeError):
    """The stored Jellyfin token was rejected (401)."""


class JellyfinClient:
    """Thin wrapper over the real Jellyfin HTTP API for one user's session."""

    def __init__(self, token: str | None):
        self.token = token

    # ---------------------------------------------------------------- low level

    def _headers(self) -> dict:
        header = _AUTH_HEADER
        if self.token:
            header += f', Token="{self.token}"'
        return {"Authorization": header, "Accept": "application/json"}

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        if not self.token:
            return None
        log.debug("Jellyfin GET %s params=%s", path, params)
        try:
            resp = requests.get(f"{JELLYFIN_URL}{path}", params=params or {},
                                headers=self._headers(), timeout=API_TIMEOUT)
        except requests.RequestException as exc:
            log.warning("Jellyfin GET %s params=%s failed: %s", path, params, exc)
            return None
        if resp.status_code == 401:
            raise JellyfinAuthError(f"Jellyfin rejected the stored token on {path}")
        if not resp.ok:
            log.warning("Jellyfin GET %s -> %s", path, resp.status_code)
            return None
        try:
            data = resp.json()
        except ValueError:
            log.warning("Jellyfin GET %s returned non-JSON", path)
            return None
        return data if isinstance(data, dict) else None

    def _forwarded(self, args, **overrides) -> dict:
        params = {k: args.get(k) for k in _FORWARD if args.get(k) not in (None, "")}
        params["Recursive"] = "true"
        params.update(overrides)
        return params

    @staticmethod
    def _items(data: dict | None) -> list[dict]:
        return data.get("Items", []) if isinstance(data, dict) else []

    # ------------------------------------------------------------ library reads

    # This proxy is a music server. A recursive /Items with no type filter and
    # no container to scope to enumerates the user's whole Jellyfin library -
    # movies and TV included - which on a large library is slow enough to hit
    # API_TIMEOUT (Jellify's home view sends exactly this). Keep such calls to
    # music. An explicit filter, from the route or the client, is left alone.
    _MUSIC_TYPES = "MusicAlbum,MusicArtist,Audio,MusicVideo"

    def get_items(self, args, user_id: str, parent_id: str | None = None,
                  include_types: str | None = None) -> list[dict]:
        params = self._forwarded(args, UserId=user_id)
        if parent_id:
            params["ParentId"] = parent_id
        # An explicit type filter from the route (e.g. /Playlists) wins over
        # whatever the client happened to send.
        if include_types:
            params["IncludeItemTypes"] = include_types
        if not params.get("IncludeItemTypes") and not parent_id:
            params["IncludeItemTypes"] = self._MUSIC_TYPES
        return self._items(self._get("/Items", params))

    def get_item(self, item_id: str, user_id: str) -> dict | None:
        return self._get(f"/Users/{user_id}/Items/{item_id}")

    def get_artists(self, args, user_id: str) -> list[dict]:
        # /Artists only ever returns artists; forwarding IncludeItemTypes to it
        # (the client sends IncludeItemTypes=MusicArtist) makes Jellyfin filter
        # every row out and return nothing.
        params = self._forwarded(args, UserId=user_id)
        params.pop("IncludeItemTypes", None)
        return self._items(self._get("/Artists", params))

    def get_playlist_items(self, playlist_id: str, args, user_id: str) -> list[dict]:
        params = self._forwarded(args, UserId=user_id)
        params.pop("Recursive", None)
        return self._items(self._get(f"/Playlists/{playlist_id}/Items", params))

    # -------------------------------------------- client-facing URLs (no proxy)

    def stream_url(self, item_id: str) -> str:
        # Jellyfin's direct-stream endpoint: the original file, seekable (Range),
        # correctly typed, and - unlike /Items/{id}/Download - with no attachment
        # header. NOT /Audio/{id}/universal: that needs a full PlaybackInfo
        # handshake (PlaySessionId + DeviceProfile) this proxy never sends, and
        # returns an empty 200 body without one. We do not transcode, so the
        # original file is the stream; a browser <audio> and mpv both play it.
        query = urllib.parse.urlencode({"api_key": self.token or "", "static": "true"})
        return f"{JELLYFIN_PUBLIC_URL}/Audio/{item_id}/stream?{query}"

    def download_url(self, item_id: str) -> str:
        query = urllib.parse.urlencode({"api_key": self.token or ""})
        return f"{JELLYFIN_PUBLIC_URL}/Items/{item_id}/Download?{query}"

    @staticmethod
    def image_url(item_id: str, image_type: str, args) -> str:
        keep = {k: args.get(k) for k in
                ("tag", "maxWidth", "maxHeight", "quality", "fillWidth", "fillHeight")
                if args.get(k)}
        query = f"?{urllib.parse.urlencode(keep)}" if keep else ""
        return f"{JELLYFIN_PUBLIC_URL}/Items/{item_id}/Images/{image_type}{query}"
