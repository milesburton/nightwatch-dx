"""
Tests for the RTL-bridge multiplexer.

Tests the Multiplexer class's thread-safe client management and
broadcast logic without requiring actual network connections.
"""

import sys
import os
import socket
import threading
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'services', 'rtl-bridge'))
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
            s1.close(); s2.close()

    def test_remove_client_decrements_count(self):
        mux = bridge.Multiplexer()
        s1, s2 = socket.socketpair()
        try:
            mux.add_client(s1)
            mux.remove_client(s1)
            assert len(mux._clients) == 0
        finally:
            s1.close(); s2.close()

    def test_remove_nonexistent_client_safe(self):
        mux = bridge.Multiplexer()
        s1, s2 = socket.socketpair()
        try:
            # Should not raise
            mux.remove_client(s1)
        finally:
            s1.close(); s2.close()

    def test_multiple_clients(self):
        mux = bridge.Multiplexer()
        pairs = [socket.socketpair() for _ in range(5)]
        try:
            for s, _ in pairs:
                mux.add_client(s)
            assert len(mux._clients) == 5
        finally:
            for s1, s2 in pairs:
                s1.close(); s2.close()

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
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert errors == [], f"Thread safety errors: {errors}"
        for s1, s2 in pairs:
            s1.close(); s2.close()


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
            s1.close(); s2.close()

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

        live_s.close(); live_r.close()


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
            # handle_client blocks reading commands; close after magic received
            try:
                mux.handle_client(client_s, ('127.0.0.1', 9999))
            except Exception:
                pass

        t = threading.Thread(target=run_handler, daemon=True)
        t.start()

        test_r.settimeout(2.0)
        magic = test_r.recv(12)
        assert magic == bridge.MAGIC

        test_r.close()
        t.join(timeout=2)
