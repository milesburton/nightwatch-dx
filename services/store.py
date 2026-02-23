"""
Shared async SQLite helpers for dx-watch decoders and API service.

Copied into each decoder image and the api image via Dockerfile.
All paths are configurable via environment variables so they work both
in Docker (mounted volume at /data) and in local development.
"""

import os

import aiosqlite

DB_PATH    = os.environ.get("DB_PATH",    "/data/dx-watch.db")
FRAMES_DIR = os.environ.get("FRAMES_DIR", "/data/frames")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    mode     TEXT    NOT NULL,
    start_ts TEXT    NOT NULL,
    end_ts   TEXT    NOT NULL,
    freq_hz  INTEGER NOT NULL,
    text     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_mode_ts ON sessions(mode, start_ts DESC);

CREATE TABLE IF NOT EXISTS frames (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    mode     TEXT    NOT NULL,
    ts       TEXT    NOT NULL,
    freq_hz  INTEGER NOT NULL,
    filepath TEXT    NOT NULL,
    quality  REAL    NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_frames_mode_ts ON frames(mode, ts DESC);
"""


async def init_db() -> None:
    """Create tables and frame directories if they don't exist."""
    os.makedirs(FRAMES_DIR, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def save_session(
    mode: str,
    start_ts: str,
    end_ts: str,
    freq_hz: int,
    text: str,
) -> int:
    """Insert a session record. Returns the new row id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO sessions(mode,start_ts,end_ts,freq_hz,text) VALUES(?,?,?,?,?)",
            (mode, start_ts, end_ts, freq_hz, text),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]


async def save_frame(
    mode: str,
    ts: str,
    freq_hz: int,
    png_bytes: bytes,
    quality: float = 1.0,
) -> tuple[int, str]:
    """
    Write PNG bytes to disk and record metadata in the DB.

    Returns (row_id, relative_filepath) where relative_filepath is e.g.
    'sstv/2026-02-23T14-32-01Z.png' — suitable for constructing /frames/ URLs.
    """
    # Make timestamp safe for use as a filename (replace : and . with -)
    safe_ts  = ts.replace(':', '-').replace('.', '-')
    subdir   = os.path.join(FRAMES_DIR, mode)
    os.makedirs(subdir, exist_ok=True)
    filename = f"{safe_ts}.png"
    filepath = os.path.join(subdir, filename)
    with open(filepath, 'wb') as f:
        f.write(png_bytes)
    rel = f"{mode}/{filename}"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO frames(mode,ts,freq_hz,filepath,quality) VALUES(?,?,?,?,?)",
            (mode, ts, freq_hz, rel, quality),
        )
        await db.commit()
        return cur.lastrowid, rel  # type: ignore[return-value]
