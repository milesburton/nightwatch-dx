"""
Tests for the RTL-bridge multiplexer.

Tests the Multiplexer class's thread-safe client management and
broadcast logic without requiring actual network connections.

Also tests AudioDecimator: correct output dtype, length, and state
continuity across chunk boundaries.
"""

import contextlib
import os
import socket
import sys
import threading

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import rtl_bridge as bridge

# ── Multiplexer client management ─────────────────────────────────────────────

class TestMultiplexerClientManagement:
    def test_add_client_increments_count(self):
        mux = bridge.Multiplexer()
        s1, s2 = socket.socketpair()
        try:
            mux.add_client(s1)
            assert len(mux._clients) == 1
        finally:
            s1.close()
            s2.close()

    def test_remove_client_decrements_count(self):
        mux = bridge.Multiplexer()
        s1, s2 = socket.socketpair()
        try:
            mux.add_client(s1)
            mux.remove_client(s1)
            assert len(mux._clients) == 0
        finally:
            s1.close()
            s2.close()

    def test_remove_nonexistent_client_safe(self):
        mux = bridge.Multiplexer()
        s1, s2 = socket.socketpair()
        try:
            # Should not raise
            mux.remove_client(s1)
        finally:
            s1.close()
            s2.close()

    def test_multiple_clients(self):
        mux = bridge.Multiplexer()
        pairs = [socket.socketpair() for _ in range(5)]
        try:
            for s, _ in pairs:
                mux.add_client(s)
            assert len(mux._clients) == 5
        finally:
            for s1, s2 in pairs:
                s1.close()
                s2.close()

    def test_thread_safe_add_remove(self):
        """Concurrent adds/removes should not corrupt state."""
        mux = bridge.Multiplexer()
        pairs = [socket.socketpair() for _ in range(20)]
        errors = []

        def add_all():
            try:
                for s, _ in pairs:
                    mux.add_client(s)
            except Exception as e:
                errors.append(e)

        def remove_all():
            try:
                for s, _ in pairs:
                    mux.remove_client(s)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=add_all)
        t2 = threading.Thread(target=remove_all)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert errors == [], f"Thread safety errors: {errors}"
        for s1, s2 in pairs:
            s1.close()
            s2.close()


# ── Broadcast ─────────────────────────────────────────────────────────────────

class TestMultiplexerBroadcast:
    def test_broadcast_reaches_all_clients(self):
        mux = bridge.Multiplexer()
        pairs = [socket.socketpair() for _ in range(3)]
        # pairs[i][0] = "server side" added as client, pairs[i][1] = read side
        for s, _ in pairs:
            mux.add_client(s)

        payload = b"IQ_DATA_CHUNK"
        mux.broadcast(payload)

        for _, reader in pairs:
            reader.settimeout(1.0)
            received = reader.recv(len(payload))
            assert received == payload

        for s1, s2 in pairs:
            s1.close()
            s2.close()

    def test_broadcast_removes_dead_clients(self):
        mux = bridge.Multiplexer()
        s1, s2 = socket.socketpair()
        mux.add_client(s1)
        # Close the read end so writing will fail
        s2.close()
        s1.close()
        # Broadcast should silently drop the dead client
        mux.broadcast(b"data")
        assert len(mux._clients) == 0

    def test_broadcast_empty_clients_noop(self):
        mux = bridge.Multiplexer()
        # Should not raise with no clients
        mux.broadcast(b"data")

    def test_broadcast_partial_failure(self):
        """One dead client should not prevent delivery to live clients."""
        mux = bridge.Multiplexer()
        # Live client
        live_s, live_r = socket.socketpair()
        # Dead client
        dead_s, dead_r = socket.socketpair()
        dead_r.close()
        dead_s.close()

        mux.add_client(live_s)
        mux.add_client(dead_s)  # already closed — will fail on sendall

        payload = b"TEST"
        mux.broadcast(payload)

        live_r.settimeout(1.0)
        received = live_r.recv(len(payload))
        assert received == payload

        live_s.close()
        live_r.close()


# ── Magic header ──────────────────────────────────────────────────────────────

class TestMagicHeader:
    def test_magic_is_12_bytes(self):
        assert len(bridge.MAGIC) == 12

    def test_magic_starts_with_rtl(self):
        assert bridge.MAGIC.startswith(b"RTL")

    def test_handle_client_sends_magic(self):
        """handle_client should immediately send the MAGIC header."""
        mux = bridge.Multiplexer()
        client_s, test_r = socket.socketpair()

        def run_handler():
            with contextlib.suppress(Exception):
                mux.handle_client(client_s, ('127.0.0.1', 9999))

        t = threading.Thread(target=run_handler, daemon=True)
        t.start()

        test_r.settimeout(2.0)
        magic = test_r.recv(12)
        assert magic == bridge.MAGIC

        test_r.close()
        t.join(timeout=2)


# ── AudioDecimator ─────────────────────────────────────────────────────────────

class TestAudioDecimator:
    def _make_raw(self, n_bytes: int = 65536) -> bytes:
        rng = np.random.default_rng(7)
        return rng.integers(0, 256, n_bytes, dtype=np.uint8).tobytes()

    @staticmethod
    def _raw_to_iq(raw: bytes) -> np.ndarray:
        """Replicate the IQ parse done in Multiplexer.broadcast()."""
        u8 = np.frombuffer(raw, dtype=np.uint8)
        if len(u8) & 1:
            u8 = u8[:-1]
        u8f = u8.astype(np.float32)
        iq = ((u8f[0::2] - 127.5) + 1j * (u8f[1::2] - 127.5)).astype(np.complex64)
        iq /= 127.5
        return iq

    def test_output_is_complex64(self):
        ad  = bridge.AudioDecimator(-146_000)
        iq  = self._raw_to_iq(self._make_raw())
        out = ad.process_iq(iq)
        assert out.dtype == np.complex64

    def test_output_length_is_100x_less_than_input_iq_pairs(self):
        ad  = bridge.AudioDecimator(55_000)
        iq  = self._raw_to_iq(self._make_raw(65536))
        out = ad.process_iq(iq)
        # 65536 bytes = 32768 IQ pairs -> ~327-328 output samples (100x decimation)
        assert 310 <= len(out) <= 340

    def test_state_persists_across_chunks(self):
        ad1 = bridge.AudioDecimator(-146_000)
        ad2 = bridge.AudioDecimator(-146_000)
        raw = self._make_raw(131072)
        iq  = self._raw_to_iq(raw)
        out1  = ad1.process_iq(iq)
        out2a = ad2.process_iq(self._raw_to_iq(raw[:65536]))
        out2b = ad2.process_iq(self._raw_to_iq(raw[65536:]))
        out2  = np.concatenate([out2a, out2b])
        assert abs(len(out1) - len(out2)) <= 1

    def test_different_freq_offsets_produce_different_output(self):
        iq = self._raw_to_iq(self._make_raw())
        out_cw   = bridge.AudioDecimator(-146_000).process_iq(iq)
        out_sstv = bridge.AudioDecimator(55_000).process_iq(iq)
        # Outputs should differ (different LO mix)
        assert not np.allclose(out_cw, out_sstv, atol=1e-3)

    def test_odd_byte_input_handled_safely(self):
        ad  = bridge.AudioDecimator(58_000)
        iq  = self._raw_to_iq(self._make_raw(65537))   # odd byte count handled in _raw_to_iq
        out = ad.process_iq(iq)
        assert out.dtype == np.complex64


# ── AudioMux magic header ──────────────────────────────────────────────────────

class TestAudioMagic:
    def test_audio_magic_is_12_bytes(self):
        assert len(bridge.AUDIO_MAGIC) == 12

    def test_audio_magic_starts_with_AUD(self):
        assert bridge.AUDIO_MAGIC.startswith(b"AUD")

    def test_audio_magic_differs_from_rtl_magic(self):
        assert bridge.AUDIO_MAGIC != bridge.MAGIC
