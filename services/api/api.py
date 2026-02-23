"""
dx-watch API service.

Serves decoded signal history over REST and static frame images.
Runs a periodic retention sweep to enforce storage limits.

Routes:
  GET /api/health                                  → {"ok": true}
  GET /api/sessions?mode=cw&limit=50&before=<ts>  → {"sessions": [...]}
  GET /api/frames?mode=sstv&limit=50&before=<ts>  → {"frames": [...]}
  GET /frames/<mode>/<filename>                    → static PNG

All session/frame data lives on the dx-data Docker volume shared with decoders.
"""

import asyncio
import logging
import os

import aiosqlite
from aiohttp import web

import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s [api] %(message)s")
log = logging.getLogger(__name__)

API_PORT   = int(os.environ.get("API_PORT",   "8900"))
FRAMES_DIR = os.environ.get("FRAMES_DIR", "/data/frames")
DB_PATH    = store.DB_PATH

# Retention limits
MAX_SESSIONS_PER_MODE = 1_000
MAX_FRAMES_PER_MODE   = 500


# ── REST handlers ─────────────────────────────────────────────────────────────

async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def get_sessions(request: web.Request) -> web.Response:
    mode   = request.query.get("mode", "cw")
    limit  = min(int(request.query.get("limit", "50")), 200)
    before = request.query.get("before")

    if mode not in ("cw", "psk31"):
        raise web.HTTPBadRequest(reason="mode must be 'cw' or 'psk31'")

    if before:
        sql  = ("SELECT id,mode,start_ts,end_ts,freq_hz,text FROM sessions "
                "WHERE mode=? AND start_ts < ? ORDER BY start_ts DESC LIMIT ?")
        args = (mode, before, limit)
    else:
        sql  = ("SELECT id,mode,start_ts,end_ts,freq_hz,text FROM sessions "
                "WHERE mode=? ORDER BY start_ts DESC LIMIT ?")
        args = (mode, limit)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, args) as cur:
            rows = await cur.fetchall()

    sessions = [
        {
            "id":       r["id"],
            "mode":     r["mode"],
            "start_ts": r["start_ts"],
            "end_ts":   r["end_ts"],
            "freq_hz":  r["freq_hz"],
            "text":     r["text"],
        }
        for r in rows
    ]
    return web.json_response({"sessions": sessions})


async def get_frames(request: web.Request) -> web.Response:
    mode   = request.query.get("mode", "sstv")
    limit  = min(int(request.query.get("limit", "50")), 200)
    before = request.query.get("before")

    if mode not in ("sstv", "easypal"):
        raise web.HTTPBadRequest(reason="mode must be 'sstv' or 'easypal'")

    if before:
        sql  = ("SELECT id,mode,ts,freq_hz,filepath FROM frames "
                "WHERE mode=? AND ts < ? ORDER BY ts DESC LIMIT ?")
        args = (mode, before, limit)
    else:
        sql  = ("SELECT id,mode,ts,freq_hz,filepath FROM frames "
                "WHERE mode=? ORDER BY ts DESC LIMIT ?")
        args = (mode, limit)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, args) as cur:
            rows = await cur.fetchall()

    frames = [
        {
            "id":      r["id"],
            "mode":    r["mode"],
            "ts":      r["ts"],
            "freq_hz": r["freq_hz"],
            "url":     f"/frames/{r['filepath']}",
        }
        for r in rows
    ]
    return web.json_response({"frames": frames})


async def serve_frame(request: web.Request) -> web.Response:
    mode     = request.match_info["mode"]
    filename = request.match_info["filename"]

    # Guard against path traversal
    if ".." in mode or ".." in filename or "/" in mode or "/" in filename:
        raise web.HTTPForbidden()
    if mode not in ("sstv", "easypal"):
        raise web.HTTPNotFound()

    path = os.path.join(FRAMES_DIR, mode, filename)
    if not os.path.isfile(path):
        raise web.HTTPNotFound()

    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


# ── Retention sweep ───────────────────────────────────────────────────────────

async def _retention_sweep() -> None:
    """Delete excess sessions and frames (+ orphaned PNG files) each hour."""
    while True:
        await asyncio.sleep(3600)
        log.info("retention sweep starting")
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                for mode in ("cw", "psk31"):
                    await db.execute(
                        """DELETE FROM sessions WHERE id IN (
                             SELECT id FROM sessions WHERE mode=?
                             ORDER BY start_ts DESC
                             LIMIT -1 OFFSET ?
                           )""",
                        (mode, MAX_SESSIONS_PER_MODE),
                    )

                for mode in ("sstv", "easypal"):
                    # Collect filepaths of rows that will be deleted
                    async with db.execute(
                        """SELECT filepath FROM frames WHERE mode=?
                           ORDER BY ts DESC
                           LIMIT -1 OFFSET ?""",
                        (mode, MAX_FRAMES_PER_MODE),
                    ) as cur:
                        to_delete = [row[0] for row in await cur.fetchall()]

                    if to_delete:
                        await db.execute(
                            """DELETE FROM frames WHERE id IN (
                                 SELECT id FROM frames WHERE mode=?
                                 ORDER BY ts DESC
                                 LIMIT -1 OFFSET ?
                               )""",
                            (mode, MAX_FRAMES_PER_MODE),
                        )
                        for rel in to_delete:
                            full = os.path.join(FRAMES_DIR, rel)
                            try:
                                os.remove(full)
                            except OSError:
                                pass

                await db.commit()
            log.info("retention sweep done")
        except Exception as e:
            log.error("retention sweep error: %s", e)


# ── App setup ─────────────────────────────────────────────────────────────────

async def main() -> None:
    await store.init_db()
    log.info("database initialised at %s", DB_PATH)

    app = web.Application()
    app.router.add_get("/api/health",   health)
    app.router.add_get("/api/sessions", get_sessions)
    app.router.add_get("/api/frames",   get_frames)
    app.router.add_get("/frames/{mode}/{filename}", serve_frame)

    asyncio.create_task(_retention_sweep())

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()
    log.info("API service listening on :%d", API_PORT)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
