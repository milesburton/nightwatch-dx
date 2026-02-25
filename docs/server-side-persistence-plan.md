# Server-Side Persistence Plan

**Status:** Not started
**Author:** Claude Sonnet 4.6 / Miles Burton
**Created:** 2026-02-23

This document records the agreed design and full implementation plan for moving
all decoded signal storage from the browser (IndexedDB) to the server (SQLite +
filesystem). It is written so that any future session can resume implementation
from exactly the right point without needing to re-derive the design.

---

## Problem

All four decoders currently transmit decoded data (text or base64-encoded PNG)
over WebSocket directly to the browser. The browser stores it in IndexedDB.

**Consequences:**
- If no browser tab is open when a frame arrives, the data is lost forever.
- SSTV and EasyPal frames are stored as base64 data-URLs in IndexedDB — browser
  storage, not backed up, not shared between devices, silently evicted under
  memory pressure.
- CW and PSK31 sessions have the same ephemeral problem.
- No quality filtering — garbage partial decodes are stored alongside clean ones.
- No consistent retention policy — each mode has an ad-hoc cap.

---

## Solution Overview

1. **Each decoder saves to SQLite** via a shared `store.py` module.
   - Text decoders (CW, PSK31): save session records.
   - Image decoders (SSTV, EasyPal): save PNG to `/data/frames/<mode>/` and a
     metadata row to SQLite.
2. **A new `api` service** serves saved data over HTTP REST and static files.
3. **nginx** proxies `/api/` and `/frames/` to the `api` service.
4. **The UI** fetches history from REST on panel open; WebSocket is for live
   notifications only (small JSON metadata, no base64 blobs in the stream).
5. **Retention** runs as a periodic task in the `api` service.

---

## Database Schema

Single SQLite file: `/data/nightwatch-dx.db`
Shared between all containers via a named Docker volume `dx-data`.

```sql
CREATE TABLE IF NOT EXISTS sessions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    mode     TEXT    NOT NULL,          -- 'cw' | 'psk31'
    start_ts TEXT    NOT NULL,          -- ISO 8601
    end_ts   TEXT    NOT NULL,
    freq_hz  INTEGER NOT NULL,
    text     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_mode_ts ON sessions(mode, start_ts DESC);

CREATE TABLE IF NOT EXISTS frames (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    mode     TEXT    NOT NULL,          -- 'sstv' | 'easypal'
    ts       TEXT    NOT NULL,          -- ISO 8601
    freq_hz  INTEGER NOT NULL,
    filepath TEXT    NOT NULL,          -- relative: frames/sstv/2026-02-23T14-32-01Z.png
    quality  REAL    NOT NULL DEFAULT 0 -- decoder-supplied quality score 0..1
);
CREATE INDEX IF NOT EXISTS idx_frames_mode_ts ON frames(mode, ts DESC);
```

---

## New Service: `api`

**Location:** `services/api/api.py`
**Port:** 8900 (internal), nginx proxies `/api/` and `/frames/`
**Dependencies:** aiohttp, aiosqlite

### Responsibilities

- Initialise the database schema on startup.
- Serve REST endpoints (see below).
- Serve static frame images from `/data/frames/` at `/frames/<mode>/<file>`.
- Run retention sweep every hour.

### REST Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/sessions?mode=cw&limit=50&before=<ts>` | List sessions, newest first |
| GET | `/api/frames?mode=sstv&limit=50&before=<ts>` | List frame metadata, newest first |
| GET | `/frames/<mode>/<filename>` | Serve PNG file |
| GET | `/api/health` | `{"ok": true}` |

Response shapes:

```json
// GET /api/sessions
{
  "sessions": [
    { "id": 42, "mode": "cw", "start_ts": "...", "end_ts": "...",
      "freq_hz": 14029000, "text": "CQ CQ DE G4XYZ" }
  ]
}

// GET /api/frames
{
  "frames": [
    { "id": 7, "mode": "sstv", "ts": "...", "freq_hz": 14230000,
      "url": "/frames/sstv/2026-02-23T14-32-01Z.png", "quality": 0.87 }
  ]
}
```

### Retention Policy (hourly sweep)

- Sessions: keep newest 1 000 per mode; delete excess rows.
- Frames: keep newest 500 per mode; delete excess rows **and** the PNG files.
- Quality filter: delete frames where `quality < MIN_QUALITY` (default 0.2).
  Decoders supply a quality score (see below).

---

## Shared Store Module

**Location:** `services/store.py`
**Copied into each decoder image** via Dockerfile.

```python
# services/store.py
import aiosqlite, os, asyncio
from datetime import UTC, datetime

DB_PATH = os.environ.get("DB_PATH", "/data/nightwatch-dx.db")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions ( ... );
            CREATE TABLE IF NOT EXISTS frames ( ... );
        """)
        await db.commit()

async def save_session(mode, start_ts, end_ts, freq_hz, text):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sessions(mode,start_ts,end_ts,freq_hz,text) VALUES(?,?,?,?,?)",
            (mode, start_ts, end_ts, freq_hz, text))
        await db.commit()

async def save_frame(mode, ts, freq_hz, filepath, quality):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO frames(mode,ts,freq_hz,filepath,quality) VALUES(?,?,?,?,?)",
            (mode, ts, freq_hz, filepath, quality))
        await db.commit()
```

---

## Decoder Changes

### Quality Scores

Each decoder computes and exposes a quality score (0.0–1.0):

| Decoder | Quality metric |
|---------|---------------|
| CW | SNR of detected tone at flush time (already computed) |
| PSK31 | Peak carrier SNR from the FFT scan |
| SSTV | Sync confidence score (ratio of sync pulses detected vs expected) |
| EasyPal | FEC success rate (blocks decoded without Viterbi errors / total) |

### CW decoder (`cw_decoder.py`)

- On session flush: call `await store.save_session('cw', start_ts, end_ts, freq_hz, text)`.
- Remove WebSocket broadcast of full text from session-flush path — only
  broadcast `{"type": "session", "id": <id>, "start_ts": ..., "text": ...}`
  (small notification; UI re-fetches from REST).
- Keep live char/word_space events on WebSocket for the live stream display.

### PSK31 decoder (`psk31_decoder.py`)

- Same pattern as CW decoder above, mode = `'psk31'`.

### SSTV decoder (`sstv_decoder.py`)

- On successful frame decode: save PNG to `/data/frames/sstv/<ts>.png`.
- Call `await store.save_frame('sstv', ts, freq_hz, filepath, quality)`.
- WebSocket: broadcast `{"type": "frame", "id": <id>, "ts": ..., "mode": ...}`
  (no base64 — UI fetches image via `/frames/sstv/<file>`).

### EasyPal decoder (`easypal_decoder.py`)

- Same pattern as SSTV, mode = `'easypal'`, path `/data/frames/easypal/`.

---

## New Files

| Path | Description |
|------|-------------|
| `services/store.py` | Shared SQLite helpers (copied into each image) |
| `services/api/api.py` | REST + static file service |
| `services/api/requirements.txt` | `aiohttp>=3.9`, `aiosqlite>=0.19` |
| `docker/api/Dockerfile` | Minimal Python image |

---

## Modified Files

| Path | Change |
|------|--------|
| `services/cw-decoder/cw_decoder.py` | Add `store.save_session` on flush, import store |
| `services/cw-decoder/requirements.txt` | Add `aiosqlite` |
| `services/psk31-decoder/psk31_decoder.py` | Same as CW |
| `services/psk31-decoder/requirements.txt` | Add `aiosqlite` |
| `services/sstv-decoder/sstv_decoder.py` | Save PNG + `store.save_frame`, WS notification only |
| `services/sstv-decoder/requirements.txt` | Add `aiosqlite` |
| `services/easypal-decoder/easypal_decoder.py` | Save PNG + `store.save_frame`, WS notification only |
| `services/easypal-decoder/requirements.txt` | Add `aiosqlite` |
| `docker/ui/nginx.conf` | Add `/api/` and `/frames/` proxy blocks |
| `docker-compose.yml` | Add `api` service, `dx-data` volume, mount volume into all decoders |
| `.github/workflows/ci.yml` | Add `api` build+push step |

---

## Docker / Infrastructure

### Named volume

```yaml
volumes:
  dx-data:
```

Mounted at `/data` in: `api`, `cw-decoder`, `psk31-decoder`, `sstv-decoder`,
`easypal-decoder`.

### api service (docker-compose.yml)

```yaml
api:
  image: ghcr.io/milesburton/nightwatch-dx/api:latest
  container_name: nightwatch-dx-api
  restart: unless-stopped
  volumes:
    - dx-data:/data
  environment:
    DB_PATH: /data/nightwatch-dx.db
    FRAMES_DIR: /data/frames
    WS_PORT: "8900"
  depends_on:
    - cw-decoder
    - psk31-decoder
    - sstv-decoder
    - easypal-decoder
```

### nginx additions

```nginx
# REST API
location /api/ {
    proxy_pass http://api:8900/api/;
    proxy_set_header Host $host;
}

# Static frame images
location /frames/ {
    proxy_pass http://api:8900/frames/;
    proxy_set_header Host $host;
}
```

---

## UI Changes

### Remove

- `ui/src/utils/db.ts` — entire file (IndexedDB layer, no longer needed)
- All `saveSSTV`, `listSSTV`, `saveEasyPal`, `listEasyPal`, `saveCWSession`,
  `listCWSessions` call sites in panel components.

### Replace

**`ui/src/utils/api.ts`** (new) — typed REST client:

```typescript
export async function fetchSessions(mode: 'cw' | 'psk31', limit = 50) { ... }
export async function fetchFrames(mode: 'sstv' | 'easypal', limit = 50) { ... }
```

**Panel components** — on `open` becoming true, call REST to load history.
WebSocket messages drive live updates (new char events for text panels, `"frame"`
notification for image panels which then re-fetches the frame list or appends
the new frame metadata from the notification).

### SSTVGalleryPanel / EasyPalGalleryPanel

- `imageUrl` changes from `data:image/png;base64,...` to `/frames/sstv/<file>`.
- `img src` works unchanged — just a URL instead of a data URL.
- On WebSocket `"frame"` message: append the metadata to the local list; no
  need to re-fetch the full list.

### CWLogPanel / PSK31Panel

- On open: `fetchSessions(mode)` to populate the session list.
- Live char/word_space WebSocket events still drive the live stream display.
- On WebSocket `"session"` notification: add the new session to the top of the list.

---

## Implementation Order

Work through these steps in sequence. Each step should pass `npx tsc --noEmit`
(UI) or `pytest services/ -v` (Python) before moving to the next.

1. **`services/store.py`** — write the shared SQLite module (init_db, save_session, save_frame).
2. **`services/api/api.py`** + **`services/api/requirements.txt`** — REST service with health, sessions, frames endpoints and retention sweep.
3. **`docker/api/Dockerfile`** — build the api image.
4. **`docker-compose.yml`** — add `dx-data` volume, mount into decoders and api, add `api` service.
5. **`docker/ui/nginx.conf`** — add `/api/` and `/frames/` proxy blocks.
6. **`services/cw-decoder/cw_decoder.py`** — import store, save session on flush, change WS session message to notification only. Add `aiosqlite` to requirements.
7. **`services/psk31-decoder/psk31_decoder.py`** — same as step 6.
8. **`services/sstv-decoder/sstv_decoder.py`** — save PNG to volume, save frame metadata, change WS frame message to notification (no base64). Add `aiosqlite` to requirements.
9. **`services/easypal-decoder/easypal_decoder.py`** — same as step 8.
10. **`ui/src/utils/api.ts`** — new REST client module.
11. **`ui/src/components/CWLogPanel.tsx`** — replace IndexedDB calls with REST fetch.
12. **`ui/src/components/PSK31Panel.tsx`** — same as step 11.
13. **`ui/src/components/SSTVGalleryPanel.tsx`** — replace IndexedDB + base64 with REST + image URL.
14. **`ui/src/components/EasyPalGalleryPanel.tsx`** — same as step 13.
15. **`ui/src/utils/db.ts`** — delete (or keep temporarily for migration, then delete).
16. **`.github/workflows/ci.yml`** — add `api` build+push step.
17. **Commit and push** — CI builds and deploys.

---

## Verification Checklist

- [ ] `pytest services/ -v` — all Python tests pass
- [ ] `cd ui && npx tsc --noEmit` — no TypeScript errors
- [ ] `docker compose build` — all images build without error
- [ ] `GET /api/health` returns `{"ok": true}`
- [ ] `GET /api/sessions?mode=cw&limit=10` returns JSON (empty array on fresh deploy)
- [ ] `GET /api/frames?mode=sstv&limit=10` returns JSON
- [ ] CW panel: sessions appear in list after 30 s inactivity timer fires
- [ ] PSK31 panel: same
- [ ] SSTV panel: frame appears in gallery when a frame is decoded; image loads from `/frames/sstv/`
- [ ] EasyPal panel: same
- [ ] Reload the page: all history is still visible (was fetched from server, not IndexedDB)
- [ ] Close browser, reopen: history still present
- [ ] `docker logs nightwatch-dx-api` — retention sweep runs without errors after 1 hour

---

## Session History

| Date | What happened |
|------|---------------|
| 2026-02-23 | Design agreed. Plan written. Implementation not yet started. |
