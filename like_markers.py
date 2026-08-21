"""Marker files for liked items.

Something outside the proxy watches a directory for one small file per liked
item, so every favourite write drops

    LIKED_TRACKS_DIR/track_<tidal id>   containing   track:<tidal id>
    LIKED_TRACKS_DIR/album_<tidal id>   containing   album:<tidal id>

i.e. the equivalent of `echo "track:$id" > "$dir/track_$id"`. Artists and
playlists are ignored - add them to _KINDS if that changes. The directory is
append-only from our side: un-liking something leaves its marker in place. All
of this is best effort, exactly like the sqlite cache, so a filesystem that
isn't mounted must not break the request that triggered the write.
"""

import logging
import os
import re
import tempfile

log = logging.getLogger(__name__)

LIKED_TRACKS_DIR = os.environ.get("LIKED_TRACKS_DIR", "/mnt/Main/Apps/music_files")

# Jellyfin item id prefix -> the name used for both the file and its contents.
_KINDS = {
    "tidal_track_": "track",
    "tidal_album_": "album",
}

# Tidal ids are numeric, but keep this permissive enough for any opaque id while
# still refusing anything that could escape LIKED_TRACKS_DIR.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _parse(item_id: str) -> tuple[str, str] | None:
    """Split a Jellyfin item id into (kind, tidal id), or None if it gets no marker."""
    for prefix, kind in _KINDS.items():
        if not item_id.startswith(prefix):
            continue
        tidal_id = item_id[len(prefix):]
        if not _SAFE_ID.match(tidal_id):
            log.warning("refusing marker file for suspicious item id %r", item_id)
            return None
        return kind, tidal_id
    return None


def write(item_id: str) -> bool:
    """Create the marker for a liked item. Returns True if a file was written."""
    parsed = _parse(item_id)
    if parsed is None:
        return False
    kind, tidal_id = parsed
    path = os.path.join(LIKED_TRACKS_DIR, f"{kind}_{tidal_id}")

    try:
        if not os.path.isdir(LIKED_TRACKS_DIR):
            os.makedirs(LIKED_TRACKS_DIR, exist_ok=True)
            log.info("created liked-items directory %s", LIKED_TRACKS_DIR)
        # Write-then-rename so a watcher never sees a half-written file.
        fd, tmp = tempfile.mkstemp(dir=LIKED_TRACKS_DIR, prefix=f".{kind}_")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(f"{kind}:{tidal_id}\n")
            # mkstemp is 0600; the point of these files is for another process
            # (likely another user) to read them, so match what a shell
            # redirect would have produced.
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    except OSError:
        log.exception("could not write liked-item marker %s", path)
        return False
    return True
