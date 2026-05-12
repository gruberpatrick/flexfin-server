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
from typing import Any

log = logging.getLogger(__name__)

DB_PATH = os.environ.get("PROXY_DB_PATH", "./data/proxy.db")
PLAY_THRESHOLD = 0.60
DEFAULT_USER_ID = "11111111-1111-1111-1111-111111111111"

# Each thread gets its own sqlite connection. Python's sqlite3 connection isn't
# thread-safe even with check_same_thread=False; concurrent use surfaces as
# "bad parameter or other API misuse". WAL mode (set per-connection) lets
# multiple connections read/write the same file with sane concurrency.
_local = threading.local()
_init_lock = threading.Lock()
_initialized = False


def _now() -> str:
    return datetime.datetime.utcnow().isoformat()


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
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")

    if not _initialized:
        with _init_lock:
            if not _initialized:
                _init_schema(conn)
                _initialized = True

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
    """)


def _safe_execute(sql: str, params: tuple, op: str) -> None:
    try:
        get_conn().execute(sql, params)
    except Exception:
        log.exception("%s failed; params=%s types=%s",
                      op, params, [type(p).__name__ for p in params])


def upsert_artist(tidal_id: Any, name: str, picture_uuid: str | None) -> None:
    _safe_execute(
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
) -> None:
    _safe_execute(
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
) -> None:
    _safe_execute(
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
) -> None:
    _safe_execute(
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


def record_play(user_id: str, item_id: str) -> None:
    tid = _strip(item_id, "tidal_track_")
    if tid is None:
        return
    get_conn().execute(
        "INSERT INTO play_counts(user_id, track_id, count, last_played_at) "
        "VALUES (?, ?, 1, ?) "
        "ON CONFLICT(user_id, track_id) DO UPDATE SET "
        "count = count + 1, last_played_at = excluded.last_played_at",
        (user_id, tid, _now()),
    )


def maybe_record_play(user_id: str, item_id: str, position_ticks: int) -> bool:
    """Record a play if the listener got past PLAY_THRESHOLD of the track."""
    duration = get_track_duration(item_id)
    if not duration:
        return False
    runtime_ticks = duration * 10_000_000
    if position_ticks / runtime_ticks >= PLAY_THRESHOLD:
        record_play(user_id, item_id)
        return True
    return False


def set_favorite(user_id: str, item_id: str) -> None:
    _safe_execute(
        "INSERT INTO favorites(user_id, item_id, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, item_id) DO NOTHING",
        (user_id, item_id, _now()),
        op="set_favorite",
    )


def unset_favorite(user_id: str, item_id: str) -> None:
    _safe_execute(
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
