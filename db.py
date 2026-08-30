"""
SQLite-backed cache for Tidal items + per-user play counts.

The translator upserts every item it surfaces; lookup helpers feed the image
endpoint and per-track UserData. There is one shared connection in WAL mode.
"""

import datetime
import logging
import os
import sqlite3
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

# Resolved eagerly so the log line below names the file we actually opened.
# The default is relative, so a service started from an unexpected working
# directory would otherwise create a second, invisible database.
DB_PATH = os.path.abspath(os.environ.get("PROXY_DB_PATH", "./data/proxy.db"))
PLAY_THRESHOLD = 0.60
DEFAULT_USER_ID = "11111111-1111-1111-1111-111111111111"

# SQLite permits one writer at a time and every upsert here is its own
# autocommit transaction, so concurrent requests do collide. Wait for the lock
# instead of dropping the write.
BUSY_TIMEOUT_MS = 15_000
WRITE_RETRIES = 3

# Each thread gets its own sqlite connection. Python's sqlite3 connection isn't
# thread-safe even with check_same_thread=False; concurrent use surfaces as
# "bad parameter or other API misuse". WAL mode (set per-connection) lets
# multiple connections read/write the same file with sane concurrency.
_local = threading.local()
_init_lock = threading.Lock()
_initialized = False

# Writes are best-effort: a failed cache upsert must not break the request that
# triggered it. That makes a broken database invisible, so count the failures
# and expose them (see stats()) instead of only logging them.
write_failures = 0


def _now() -> str:
    # Naive UTC, matching the timestamps already stored.
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()


def _strip(value: str, prefix: str) -> str | None:
    return value[len(prefix):] if value.startswith(prefix) else None


def _i(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _s(v) -> str | None:
    return None if v is None else str(v)


def get_conn() -> sqlite3.Connection:
    global _initialized

    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn

    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None,
                           timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS};")
    # WAL + NORMAL doesn't fsync on every commit. A crash can lose the last few
    # upserts but never corrupts the file, and browsing one album is ~300
    # autocommit transactions - at one fsync each that dominated the request.
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")

    if not _initialized:
        with _init_lock:
            if not _initialized:
                _init_schema(conn)
                _initialized = True
                log.info("sqlite ready at %s", DB_PATH)

    _local.conn = conn
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tidal_artists (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            picture_uuid TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tidal_albums (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            cover_uuid TEXT,
            artist_id TEXT,
            artist_name TEXT,
            num_tracks INTEGER,
            year INTEGER,
            release_date TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tidal_tracks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            album_id TEXT,
            album_title TEXT,
            album_cover_uuid TEXT,
            artist_id TEXT,
            artist_name TEXT,
            duration INTEGER,
            track_num INTEGER,
            volume_num INTEGER,
            year INTEGER,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tidal_playlists (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            picture_uuid TEXT,
            description TEXT,
            num_tracks INTEGER,
            duration INTEGER,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS play_counts (
            user_id TEXT NOT NULL,
            track_id TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            last_played_at TEXT,
            PRIMARY KEY (user_id, track_id)
        );
        CREATE TABLE IF NOT EXISTS favorites (
            user_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, item_id)
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            user_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            jf_token TEXT
        );
        CREATE INDEX IF NOT EXISTS sessions_expires_at ON sessions(expires_at);

        -- Records that a Tidal item and a Jellyfin library item are the same
        -- release, matched on a real identifier (ISRC for tracks, barcode/UPC
        -- for albums). The merge layer writes a row whenever it collapses a
        -- Tidal result into its local twin; nothing reads it yet, but it is the
        -- store the future "keep the Tidal id, play the local file" work needs.
        CREATE TABLE IF NOT EXISTS source_map (
            tidal_id TEXT NOT NULL,
            jellyfin_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            match_key TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (tidal_id, jellyfin_id)
        );
    """)

    # jf_token was added after the first release. A database created before then
    # has the sessions table without it, and CREATE TABLE IF NOT EXISTS above
    # won't touch it, so patch it in once.
    have = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "jf_token" not in have:
        conn.execute("ALTER TABLE sessions ADD COLUMN jf_token TEXT")
        log.info("added sessions.jf_token to %s", DB_PATH)


def _safe_execute(sql: str, params: tuple, op: str) -> bool:
    """Run one write, retrying while the database is merely locked.

    Returns True when the row landed. Never raises: a failed cache upsert must
    not break the request that triggered it.
    """
    global write_failures

    last_error: Exception | None = None
    for attempt in range(1, WRITE_RETRIES + 1):
        try:
            get_conn().execute(sql, params)
            return True
        except Exception as exc:
            last_error = exc
            message = str(exc).lower()
            retryable = isinstance(exc, sqlite3.OperationalError) and (
                "locked" in message or "busy" in message)
            if not retryable or attempt == WRITE_RETRIES:
                break
            time.sleep(0.05 * attempt)

    write_failures += 1
    log.error("%s failed after %d attempt(s): %s [db=%s write_failures=%d] "
              "params=%s types=%s",
              op, attempt, last_error, DB_PATH, write_failures, params,
              [type(p).__name__ for p in params])
    return False


def upsert_artist(tidal_id: Any, name: str, picture_uuid: str | None) -> bool:
    return _safe_execute(
        "INSERT INTO tidal_artists(id, name, picture_uuid, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "name=excluded.name, picture_uuid=excluded.picture_uuid, updated_at=excluded.updated_at",
        (_s(tidal_id), _s(name), _s(picture_uuid), _now()),
        op="upsert_artist",
    )


def upsert_album(
    tidal_id: Any,
    title: str,
    cover_uuid: str | None,
    artist_id: Any,
    artist_name: str | None,
    num_tracks: int | None,
    year: int | None,
    release_date: str | None,
) -> bool:
    return _safe_execute(
        "INSERT INTO tidal_albums(id, title, cover_uuid, artist_id, artist_name, num_tracks, year, release_date, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "title=excluded.title, cover_uuid=excluded.cover_uuid, artist_id=excluded.artist_id, "
        "artist_name=excluded.artist_name, num_tracks=excluded.num_tracks, year=excluded.year, "
        "release_date=excluded.release_date, updated_at=excluded.updated_at",
        (_s(tidal_id), _s(title), _s(cover_uuid),
         _s(artist_id), _s(artist_name),
         _i(num_tracks), _i(year), _s(release_date), _now()),
        op="upsert_album",
    )


def upsert_playlist(
    tidal_id: Any,
    name: str,
    picture_uuid: str | None,
    description: str | None,
    num_tracks: int | None,
    duration: int | None,
) -> bool:
    return _safe_execute(
        "INSERT INTO tidal_playlists(id, name, picture_uuid, description, num_tracks, duration, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "name=excluded.name, picture_uuid=excluded.picture_uuid, description=excluded.description, "
        "num_tracks=excluded.num_tracks, duration=excluded.duration, updated_at=excluded.updated_at",
        (_s(tidal_id), _s(name), _s(picture_uuid), _s(description),
         _i(num_tracks), _i(duration), _now()),
        op="upsert_playlist",
    )


def upsert_track(
    tidal_id: Any,
    title: str,
    album_id: Any,
    album_title: str | None,
    album_cover_uuid: str | None,
    artist_id: Any,
    artist_name: str | None,
    duration: int | None,
    track_num: int | None,
    volume_num: int | None,
    year: int | None,
) -> bool:
    return _safe_execute(
        "INSERT INTO tidal_tracks(id, title, album_id, album_title, album_cover_uuid, artist_id, artist_name, duration, track_num, volume_num, year, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "title=excluded.title, album_id=excluded.album_id, album_title=excluded.album_title, "
        "album_cover_uuid=excluded.album_cover_uuid, artist_id=excluded.artist_id, "
        "artist_name=excluded.artist_name, duration=excluded.duration, track_num=excluded.track_num, "
        "volume_num=excluded.volume_num, year=excluded.year, updated_at=excluded.updated_at",
        (_s(tidal_id), _s(title),
         _s(album_id), _s(album_title), _s(album_cover_uuid),
         _s(artist_id), _s(artist_name),
         _i(duration), _i(track_num), _i(volume_num), _i(year), _now()),
        op="upsert_track",
    )


def upsert_source_map(tidal_id: str, jellyfin_id: str, kind: str,
                      match_key: str | None) -> bool:
    """Note that a Tidal item and a Jellyfin library item are the same release.

    Written by the merge layer when it collapses a Tidal result into its local
    twin. Best effort, like every other write here.
    """
    return _safe_execute(
        "INSERT INTO source_map(tidal_id, jellyfin_id, kind, match_key, created_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(tidal_id, jellyfin_id) DO UPDATE SET "
        "kind=excluded.kind, match_key=excluded.match_key",
        (_s(tidal_id), _s(jellyfin_id), _s(kind), _s(match_key), _now()),
        op="upsert_source_map",
    )


def get_cover_uuid(item_id: str) -> str | None:
    """Look up the cover/picture UUID for any cached item id."""
    if (tid := _strip(item_id, "tidal_artist_")) is not None:
        row = get_conn().execute(
            "SELECT picture_uuid FROM tidal_artists WHERE id=?", (tid,)
        ).fetchone()
        return row["picture_uuid"] if row else None
    if (tid := _strip(item_id, "tidal_album_")) is not None:
        row = get_conn().execute(
            "SELECT cover_uuid FROM tidal_albums WHERE id=?", (tid,)
        ).fetchone()
        return row["cover_uuid"] if row else None
    if (tid := _strip(item_id, "tidal_track_")) is not None:
        row = get_conn().execute(
            "SELECT album_cover_uuid FROM tidal_tracks WHERE id=?", (tid,)
        ).fetchone()
        return row["album_cover_uuid"] if row else None
    if (tid := _strip(item_id, "tidal_playlist_")) is not None:
        row = get_conn().execute(
            "SELECT picture_uuid FROM tidal_playlists WHERE id=?", (tid,)
        ).fetchone()
        return row["picture_uuid"] if row else None
    return None


def get_track_duration(item_id: str) -> int | None:
    tid = _strip(item_id, "tidal_track_")
    if tid is None:
        return None
    row = get_conn().execute(
        "SELECT duration FROM tidal_tracks WHERE id=?", (tid,)
    ).fetchone()
    return row["duration"] if row and row["duration"] is not None else None


def get_play_count(user_id: str, item_id: str) -> int:
    tid = _strip(item_id, "tidal_track_")
    if tid is None:
        return 0
    row = get_conn().execute(
        "SELECT count FROM play_counts WHERE user_id=? AND track_id=?",
        (user_id, tid),
    ).fetchone()
    return row["count"] if row else 0


def record_play(user_id: str, item_id: str) -> bool:
    tid = _strip(item_id, "tidal_track_")
    if tid is None:
        return False
    return _safe_execute(
        "INSERT INTO play_counts(user_id, track_id, count, last_played_at) "
        "VALUES (?, ?, 1, ?) "
        "ON CONFLICT(user_id, track_id) DO UPDATE SET "
        "count = count + 1, last_played_at = excluded.last_played_at",
        (user_id, tid, _now()),
        op="record_play",
    )


def clear_play(user_id: str, item_id: str) -> bool:
    tid = _strip(item_id, "tidal_track_")
    if tid is None:
        return False
    return _safe_execute(
        "DELETE FROM play_counts WHERE user_id=? AND track_id=?",
        (user_id, tid),
        op="clear_play",
    )


def maybe_record_play(user_id: str, item_id: str, position_ticks: int,
                      runtime_ticks: int | None = None) -> bool:
    """Record a play if the listener got past PLAY_THRESHOLD of the track.

    Prefers our cached duration but falls back to the runtime the client
    reported: a track played from the client's own cache may never have passed
    through the translator, and dropping the play for that is wrong.
    """
    duration = get_track_duration(item_id)
    total_ticks = (duration or 0) * 10_000_000 or (runtime_ticks or 0)
    if total_ticks <= 0:
        log.info("no duration for %s (cached=%s reported=%s); play not recorded",
                 item_id, duration, runtime_ticks)
        return False
    if position_ticks / total_ticks < PLAY_THRESHOLD:
        return False
    return record_play(user_id, item_id)


def set_favorite(user_id: str, item_id: str) -> bool:
    return _safe_execute(
        "INSERT INTO favorites(user_id, item_id, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, item_id) DO NOTHING",
        (user_id, item_id, _now()),
        op="set_favorite",
    )


def unset_favorite(user_id: str, item_id: str) -> bool:
    return _safe_execute(
        "DELETE FROM favorites WHERE user_id=? AND item_id=?",
        (user_id, item_id),
        op="unset_favorite",
    )


def is_favorite(user_id: str, item_id: str) -> bool:
    row = get_conn().execute(
        "SELECT 1 FROM favorites WHERE user_id=? AND item_id=? LIMIT 1",
        (user_id, item_id),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------- sessions
# Login is delegated to a real Jellyfin instance (see auth.py); we only keep the
# token it earned so every later request can be checked with one indexed lookup
# instead of another round trip to Jellyfin.

def create_session(token: str, user_id: str, user_name: str, ttl_days: int,
                   jf_token: str | None = None) -> bool:
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    expires = now + datetime.timedelta(days=ttl_days)
    return _safe_execute(
        "INSERT INTO sessions(token, user_id, user_name, created_at, expires_at, jf_token) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(token) DO UPDATE SET "
        "user_id=excluded.user_id, user_name=excluded.user_name, "
        "created_at=excluded.created_at, expires_at=excluded.expires_at, "
        "jf_token=excluded.jf_token",
        (token, user_id, user_name, now.isoformat(), expires.isoformat(), jf_token),
        op="create_session",
    )


def lookup_session(token: str) -> dict[str, str | None] | None:
    """Return the session for a token, or None if unknown or expired.

    jf_token is the access token the real Jellyfin issued at login; the merge
    layer calls Jellyfin as this user with it. It is None for sessions created
    before that was captured - those users get Tidal-only results until they
    log in again.
    """
    if not token:
        return None
    row = get_conn().execute(
        "SELECT user_id, user_name, expires_at, jf_token FROM sessions WHERE token=?",
        (token,),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= _now():
        log.info("token for user %s expired at %s", row["user_id"], row["expires_at"])
        delete_session(token)
        return None
    return {"user_id": row["user_id"], "user_name": row["user_name"],
            "jf_token": row["jf_token"]}


def delete_session(token: str) -> bool:
    return _safe_execute("DELETE FROM sessions WHERE token=?", (token,),
                         op="delete_session")


def purge_expired_sessions() -> int:
    """Drop expired rows. Returns how many went; safe to call at any time."""
    conn = get_conn()
    before = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    if not _safe_execute("DELETE FROM sessions WHERE expires_at <= ?", (_now(),),
                         op="purge_expired_sessions"):
        return 0
    after = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    return before - after


def remap_user_id(old_user_id: str, new_user_id: str) -> dict[str, int]:
    """Move a user's rows onto a different id.

    Needed once, when login stops handing out the placeholder DEFAULT_USER_ID
    and starts returning real Jellyfin user ids - otherwise every existing
    favourite and play count is orphaned. See remap_user.py.
    """
    conn = get_conn()
    moved = {}
    for table, column in (("favorites", "item_id"), ("play_counts", "track_id")):
        # Drop rows that would collide with something already under the new id,
        # otherwise the UPDATE fails on the composite primary key.
        conn.execute(
            f"DELETE FROM {table} WHERE user_id=? AND {column} IN "
            f"(SELECT {column} FROM {table} WHERE user_id=?)",
            (old_user_id, new_user_id),
        )
        cur = conn.execute(f"UPDATE {table} SET user_id=? WHERE user_id=?",
                           (new_user_id, old_user_id))
        moved[table] = cur.rowcount
    return moved


# table -> the column that records when a row was last written
TABLES = {
    "tidal_artists": "updated_at",
    "tidal_albums": "updated_at",
    "tidal_tracks": "updated_at",
    "tidal_playlists": "updated_at",
    "play_counts": "last_played_at",
    "favorites": "created_at",
    "sessions": "created_at",
    "source_map": "created_at",
}


def stats() -> dict[str, Any]:
    """Which file we're writing to, what's in it, and how many writes were
    dropped. Served at /Proxy/Stats so a database that has quietly stopped
    taking rows stops being invisible."""
    conn = get_conn()
    out: dict[str, Any] = {"db_path": DB_PATH, "write_failures": write_failures}
    for table, stamp in TABLES.items():
        try:
            row = conn.execute(
                f"SELECT COUNT(*) AS rows, MAX({stamp}) AS last_write FROM {table}"
            ).fetchone()
            out[table] = {"rows": row["rows"], "last_write": row["last_write"]}
        except sqlite3.Error as exc:
            out[table] = {"error": str(exc)}
    return out
