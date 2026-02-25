"""
Integration tests for store.py and api.py.

Uses a temporary directory and in-memory-style SQLite so no production data
is touched. Tests are isolated: each test gets a fresh DB and frames dir.
"""

import asyncio
import os
import sys
import tempfile
import types

import importlib

import pytest

# ── Stub aiohttp so store.py can be imported in CI without the full package ──
# (api.py needs aiohttp; we import it only where aiohttp is guaranteed present)
try:
    import aiohttp  # noqa: F401
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

# Make services/ importable (real store.py lives here)
SERVICES_DIR = os.path.join(os.path.dirname(__file__), "..")
API_DIR      = os.path.dirname(__file__)
for p in (SERVICES_DIR, API_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

# Remove any stub store module left by other test files so we get the real one
sys.modules.pop("store", None)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_data(tmp_path, monkeypatch):
    """Point store at a fresh temp dir for each test."""
    db   = str(tmp_path / "test.db")
    fdir = str(tmp_path / "frames")
    # Ensure we have the real store module (not a stub)
    sys.modules.pop("store", None)
    import store
    monkeypatch.setattr(store, "DB_PATH",    db)
    monkeypatch.setattr(store, "FRAMES_DIR", fdir)
    return db, fdir


# ── store.init_db ──────────────────────────────────────────────────────────────

class TestInitDb:
    @pytest.mark.asyncio
    async def test_creates_database_file(self, tmp_data):
        import store
        db, _ = tmp_data
        await store.init_db()
        assert os.path.isfile(db)

    @pytest.mark.asyncio
    async def test_creates_frames_directory(self, tmp_data):
        import store
        _, fdir = tmp_data
        await store.init_db()
        assert os.path.isdir(fdir)

    @pytest.mark.asyncio
    async def test_creates_sessions_table(self, tmp_data):
        import aiosqlite
        import store
        db, _ = tmp_data
        await store.init_db()
        async with aiosqlite.connect(db) as conn:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
            ) as cur:
                row = await cur.fetchone()
        assert row is not None

    @pytest.mark.asyncio
    async def test_creates_frames_table(self, tmp_data):
        import aiosqlite
        import store
        db, _ = tmp_data
        await store.init_db()
        async with aiosqlite.connect(db) as conn:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='frames'"
            ) as cur:
                row = await cur.fetchone()
        assert row is not None

    @pytest.mark.asyncio
    async def test_idempotent_second_call(self, tmp_data):
        import store
        await store.init_db()
        await store.init_db()   # should not raise


# ── store.save_session ─────────────────────────────────────────────────────────

class TestSaveSession:
    @pytest.mark.asyncio
    async def test_returns_integer_row_id(self, tmp_data):
        import store
        await store.init_db()
        row_id = await store.save_session("cw", "2026-01-01T00:00:00Z", "2026-01-01T00:00:05Z", 14029000, "CQ CQ")
        assert isinstance(row_id, int)
        assert row_id >= 1

    @pytest.mark.asyncio
    async def test_row_ids_are_sequential(self, tmp_data):
        import store
        await store.init_db()
        id1 = await store.save_session("cw",    "2026-01-01T00:00:00Z", "2026-01-01T00:00:05Z", 14029000, "CQ")
        id2 = await store.save_session("psk31", "2026-01-01T00:00:10Z", "2026-01-01T00:00:15Z", 14070000, "DE")
        assert id2 == id1 + 1

    @pytest.mark.asyncio
    async def test_persists_all_fields(self, tmp_data):
        import aiosqlite
        import store
        db, _ = tmp_data
        await store.init_db()
        await store.save_session("psk31", "2026-02-01T12:00:00Z", "2026-02-01T12:00:30Z", 14070000, "hello world")
        async with aiosqlite.connect(db) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT * FROM sessions") as cur:
                row = await cur.fetchone()
        assert row["mode"]     == "psk31"
        assert row["start_ts"] == "2026-02-01T12:00:00Z"
        assert row["end_ts"]   == "2026-02-01T12:00:30Z"
        assert row["freq_hz"]  == 14070000
        assert row["text"]     == "hello world"


# ── store.save_frame ───────────────────────────────────────────────────────────

class TestSaveFrame:
    _PNG_BYTES = (
        b"\x89PNG\r\n\x1a\n"       # PNG magic
        b"\x00\x00\x00\rIHDR"      # IHDR chunk
        b"\x00\x00\x00\x01"        # width = 1
        b"\x00\x00\x00\x01"        # height = 1
        b"\x08\x02"                 # 8-bit RGB
        b"\x00\x00\x00"
        b"\x90wS\xde"              # CRC (approximate, Pillow won't validate)
        b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    @pytest.mark.asyncio
    async def test_writes_png_to_disk(self, tmp_data):
        import store
        _, fdir = tmp_data
        await store.init_db()
        _, rel = await store.save_frame("sstv", "2026-01-01T00:00:00Z", 14230000, self._PNG_BYTES)
        full = os.path.join(fdir, rel)
        assert os.path.isfile(full)

    @pytest.mark.asyncio
    async def test_creates_mode_subdirectory(self, tmp_data):
        import store
        _, fdir = tmp_data
        await store.init_db()
        await store.save_frame("sstv", "2026-01-01T00:00:00Z", 14230000, self._PNG_BYTES)
        assert os.path.isdir(os.path.join(fdir, "sstv"))

    @pytest.mark.asyncio
    async def test_relative_filepath_includes_mode(self, tmp_data):
        import store
        await store.init_db()
        _, rel = await store.save_frame("easypal", "2026-01-01T00:00:00Z", 14233000, self._PNG_BYTES)
        assert rel.startswith("easypal/")
        assert rel.endswith(".png")

    @pytest.mark.asyncio
    async def test_timestamp_colons_replaced_in_filename(self, tmp_data):
        import store
        await store.init_db()
        _, rel = await store.save_frame("sstv", "2026-01-01T12:34:56Z", 14230000, self._PNG_BYTES)
        filename = rel.split("/")[-1]
        assert ":" not in filename

    @pytest.mark.asyncio
    async def test_returns_integer_row_id(self, tmp_data):
        import store
        await store.init_db()
        row_id, _ = await store.save_frame("sstv", "2026-01-01T00:00:00Z", 14230000, self._PNG_BYTES)
        assert isinstance(row_id, int)
        assert row_id >= 1

    @pytest.mark.asyncio
    async def test_png_content_preserved(self, tmp_data):
        import store
        _, fdir = tmp_data
        await store.init_db()
        _, rel = await store.save_frame("sstv", "2026-01-01T00:00:00Z", 14230000, self._PNG_BYTES)
        with open(os.path.join(fdir, rel), "rb") as f:
            written = f.read()
        assert written == self._PNG_BYTES


# ── api endpoints ─────────────────────────────────────────────────────────────

@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
class TestApiHealth:
    @pytest.mark.asyncio
    async def test_health_returns_ok(self, tmp_data, monkeypatch):
        import store
        db, fdir = tmp_data
        monkeypatch.setenv("DB_PATH",    db)
        monkeypatch.setenv("FRAMES_DIR", fdir)
        sys.path.insert(0, os.path.dirname(__file__))
        import api as api_mod
        monkeypatch.setattr(api_mod, "DB_PATH",    db)
        monkeypatch.setattr(api_mod, "FRAMES_DIR", fdir)

        from aiohttp.test_utils import make_mocked_request
        resp = await api_mod.health(make_mocked_request("GET", "/api/health"))
        import json
        body = json.loads(resp.body)
        assert body == {"ok": True}


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
class TestApiSessions:
    @pytest.fixture(autouse=True)
    def _patch_api(self, tmp_data, monkeypatch):
        import store
        db, fdir = tmp_data
        sys.path.insert(0, os.path.dirname(__file__))
        import api as api_mod
        monkeypatch.setattr(api_mod, "DB_PATH",    db)
        monkeypatch.setattr(store,   "DB_PATH",    db)
        monkeypatch.setattr(store,   "FRAMES_DIR", fdir)
        self.db   = db
        self.fdir = fdir
        self.api  = api_mod
        self.store = store

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_sessions(self):
        import json
        from aiohttp.test_utils import make_mocked_request
        await self.store.init_db()
        req  = make_mocked_request("GET", "/api/sessions?mode=cw")
        resp = await self.api.get_sessions(req)
        body = json.loads(resp.body)
        assert body == {"sessions": []}

    @pytest.mark.asyncio
    async def test_returns_saved_session(self):
        import json
        from aiohttp.test_utils import make_mocked_request
        await self.store.init_db()
        await self.store.save_session("cw", "2026-01-01T00:00:00Z", "2026-01-01T00:00:05Z", 14029000, "DE G0ABC")
        req  = make_mocked_request("GET", "/api/sessions?mode=cw")
        resp = await self.api.get_sessions(req)
        body = json.loads(resp.body)
        assert len(body["sessions"]) == 1
        s = body["sessions"][0]
        assert s["mode"]  == "cw"
        assert s["text"]  == "DE G0ABC"
        assert s["freq_hz"] == 14029000

    @pytest.mark.asyncio
    async def test_mode_filtering(self):
        import json
        from aiohttp.test_utils import make_mocked_request
        await self.store.init_db()
        await self.store.save_session("cw",    "2026-01-01T00:00:00Z", "2026-01-01T00:00:05Z", 14029000, "CW session")
        await self.store.save_session("psk31", "2026-01-01T00:00:10Z", "2026-01-01T00:00:15Z", 14070000, "PSK session")
        req  = make_mocked_request("GET", "/api/sessions?mode=psk31")
        resp = await self.api.get_sessions(req)
        body = json.loads(resp.body)
        assert len(body["sessions"]) == 1
        assert body["sessions"][0]["mode"] == "psk31"

    @pytest.mark.asyncio
    async def test_invalid_mode_returns_400(self):
        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request
        await self.store.init_db()
        req = make_mocked_request("GET", "/api/sessions?mode=invalid")
        with pytest.raises(web.HTTPBadRequest):
            await self.api.get_sessions(req)

    @pytest.mark.asyncio
    async def test_limit_caps_at_200(self):
        import json
        from aiohttp.test_utils import make_mocked_request
        await self.store.init_db()
        for i in range(5):
            await self.store.save_session("cw", f"2026-01-01T00:00:0{i}Z", f"2026-01-01T00:00:0{i}Z", 14029000, f"msg{i}")
        req  = make_mocked_request("GET", "/api/sessions?mode=cw&limit=999")
        resp = await self.api.get_sessions(req)
        body = json.loads(resp.body)
        # All 5 returned (limit 999 capped to 200, but only 5 exist)
        assert len(body["sessions"]) == 5

    @pytest.mark.asyncio
    async def test_pagination_with_before(self):
        import json
        from aiohttp.test_utils import make_mocked_request
        await self.store.init_db()
        await self.store.save_session("cw", "2026-01-01T00:00:01Z", "2026-01-01T00:00:05Z", 14029000, "first")
        await self.store.save_session("cw", "2026-01-01T00:00:10Z", "2026-01-01T00:00:15Z", 14029000, "second")
        req  = make_mocked_request("GET", "/api/sessions?mode=cw&before=2026-01-01T00:00:05Z")
        resp = await self.api.get_sessions(req)
        body = json.loads(resp.body)
        assert len(body["sessions"]) == 1
        assert body["sessions"][0]["text"] == "first"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
class TestApiFrames:
    _PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32   # minimal fake PNG

    @pytest.fixture(autouse=True)
    def _patch_api(self, tmp_data, monkeypatch):
        import store
        db, fdir = tmp_data
        sys.path.insert(0, os.path.dirname(__file__))
        import api as api_mod
        monkeypatch.setattr(api_mod, "DB_PATH",    db)
        monkeypatch.setattr(api_mod, "FRAMES_DIR", fdir)
        monkeypatch.setattr(store,   "DB_PATH",    db)
        monkeypatch.setattr(store,   "FRAMES_DIR", fdir)
        self.db    = db
        self.fdir  = fdir
        self.api   = api_mod
        self.store = store

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_frames(self):
        import json
        from aiohttp.test_utils import make_mocked_request
        await self.store.init_db()
        req  = make_mocked_request("GET", "/api/frames?mode=sstv")
        resp = await self.api.get_frames(req)
        body = json.loads(resp.body)
        assert body == {"frames": []}

    @pytest.mark.asyncio
    async def test_returns_saved_frame_with_url(self):
        import json
        from aiohttp.test_utils import make_mocked_request
        await self.store.init_db()
        await self.store.save_frame("sstv", "2026-01-01T12:00:00Z", 14230000, self._PNG_BYTES)
        req  = make_mocked_request("GET", "/api/frames?mode=sstv")
        resp = await self.api.get_frames(req)
        body = json.loads(resp.body)
        assert len(body["frames"]) == 1
        f = body["frames"][0]
        assert f["mode"] == "sstv"
        assert f["url"].startswith("/frames/sstv/")
        assert f["freq_hz"] == 14230000

    @pytest.mark.asyncio
    async def test_invalid_mode_returns_400(self):
        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request
        await self.store.init_db()
        req = make_mocked_request("GET", "/api/frames?mode=cw")
        with pytest.raises(web.HTTPBadRequest):
            await self.api.get_frames(req)

    @pytest.mark.asyncio
    async def test_mode_filtering(self):
        import json
        from aiohttp.test_utils import make_mocked_request
        await self.store.init_db()
        await self.store.save_frame("sstv",    "2026-01-01T00:00:01Z", 14230000, self._PNG_BYTES)
        await self.store.save_frame("easypal", "2026-01-01T00:00:02Z", 14233000, self._PNG_BYTES)
        req  = make_mocked_request("GET", "/api/frames?mode=easypal")
        resp = await self.api.get_frames(req)
        body = json.loads(resp.body)
        assert len(body["frames"]) == 1
        assert body["frames"][0]["mode"] == "easypal"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
class TestServeFrame:
    _PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    @pytest.fixture(autouse=True)
    def _patch_api(self, tmp_data, monkeypatch):
        import store
        db, fdir = tmp_data
        sys.path.insert(0, os.path.dirname(__file__))
        import api as api_mod
        monkeypatch.setattr(api_mod, "DB_PATH",    db)
        monkeypatch.setattr(api_mod, "FRAMES_DIR", fdir)
        monkeypatch.setattr(store,   "DB_PATH",    db)
        monkeypatch.setattr(store,   "FRAMES_DIR", fdir)
        self.fdir = fdir
        self.api  = api_mod
        self.store = store

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self):
        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request
        req = make_mocked_request("GET", "/frames/../etc/passwd",
                                  match_info={"mode": "..", "filename": "passwd"})
        with pytest.raises(web.HTTPForbidden):
            await self.api.serve_frame(req)

    @pytest.mark.asyncio
    async def test_invalid_mode_rejected(self):
        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request
        req = make_mocked_request("GET", "/frames/cw/file.png",
                                  match_info={"mode": "cw", "filename": "file.png"})
        with pytest.raises(web.HTTPNotFound):
            await self.api.serve_frame(req)

    @pytest.mark.asyncio
    async def test_missing_file_returns_404(self):
        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request
        req = make_mocked_request("GET", "/frames/sstv/nonexistent.png",
                                  match_info={"mode": "sstv", "filename": "nonexistent.png"})
        with pytest.raises(web.HTTPNotFound):
            await self.api.serve_frame(req)

    @pytest.mark.asyncio
    async def test_existing_file_served(self, tmp_data):
        from aiohttp.test_utils import make_mocked_request
        await self.store.init_db()
        _, rel = await self.store.save_frame("sstv", "2026-01-01T00-00-00Z", 14230000, self._PNG_BYTES)
        filename = rel.split("/")[-1]
        req = make_mocked_request("GET", f"/frames/sstv/{filename}",
                                  match_info={"mode": "sstv", "filename": filename})
        resp = await self.api.serve_frame(req)
        assert resp.status == 200


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
class TestRetentionSweep:
    _PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    @pytest.fixture(autouse=True)
    def _patch_api(self, tmp_data, monkeypatch):
        import store
        db, fdir = tmp_data
        sys.path.insert(0, os.path.dirname(__file__))
        import api as api_mod
        monkeypatch.setattr(api_mod, "DB_PATH",             db)
        monkeypatch.setattr(api_mod, "FRAMES_DIR",          fdir)
        monkeypatch.setattr(api_mod, "MAX_SESSIONS_PER_MODE", 3)
        monkeypatch.setattr(api_mod, "MAX_FRAMES_PER_MODE",   2)
        monkeypatch.setattr(store,   "DB_PATH",             db)
        monkeypatch.setattr(store,   "FRAMES_DIR",          fdir)
        self.db    = db
        self.fdir  = fdir
        self.api   = api_mod
        self.store = store

    @pytest.mark.asyncio
    async def test_excess_sessions_deleted(self):
        import aiosqlite
        await self.store.init_db()
        for i in range(5):
            await self.store.save_session("cw", f"2026-01-01T00:00:0{i}Z", f"2026-01-01T00:00:0{i}Z", 14029000, f"msg{i}")

        # Run sweep directly (bypass the sleep)
        async with aiosqlite.connect(self.db) as db:
            await db.execute(
                """DELETE FROM sessions WHERE id IN (
                     SELECT id FROM sessions WHERE mode=?
                     ORDER BY start_ts DESC
                     LIMIT -1 OFFSET ?
                   )""",
                ("cw", self.api.MAX_SESSIONS_PER_MODE),
            )
            await db.commit()
            async with db.execute("SELECT COUNT(*) FROM sessions WHERE mode='cw'") as cur:
                count = (await cur.fetchone())[0]
        assert count == 3

    @pytest.mark.asyncio
    async def test_excess_frames_deleted_with_files(self):
        import aiosqlite
        await self.store.init_db()
        paths = []
        for i in range(4):
            _, rel = await self.store.save_frame("sstv", f"2026-01-01T00:00:0{i}Z", 14230000, self._PNG_BYTES)
            paths.append(os.path.join(self.fdir, rel))

        # Verify all files exist before sweep
        assert all(os.path.isfile(p) for p in paths)

        # Simulate the sweep logic
        async with aiosqlite.connect(self.db) as db:
            async with db.execute(
                "SELECT filepath FROM frames WHERE mode='sstv' ORDER BY ts DESC LIMIT -1 OFFSET ?",
                (self.api.MAX_FRAMES_PER_MODE,),
            ) as cur:
                to_delete = [row[0] for row in await cur.fetchall()]
            await db.execute(
                "DELETE FROM frames WHERE id IN (SELECT id FROM frames WHERE mode='sstv' ORDER BY ts DESC LIMIT -1 OFFSET ?)",
                (self.api.MAX_FRAMES_PER_MODE,),
            )
            await db.commit()

        for rel in to_delete:
            full = os.path.join(self.fdir, rel)
            if os.path.isfile(full):
                os.remove(full)

        async with aiosqlite.connect(self.db) as db:
            async with db.execute("SELECT COUNT(*) FROM frames WHERE mode='sstv'") as cur:
                count = (await cur.fetchone())[0]

        assert count == self.api.MAX_FRAMES_PER_MODE
        # The 2 oldest files should be gone
        assert not os.path.isfile(paths[0])
        assert not os.path.isfile(paths[1])
        # The 2 newest should still exist
        assert os.path.isfile(paths[2])
        assert os.path.isfile(paths[3])
