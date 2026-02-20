"""
RTL-TCP bridge / multiplexer.

Sits between the physical rtl_tcp process and the decoder services.
Exposes the same rtl_tcp protocol on a configurable port, forwarding
commands upstream and broadcasting IQ data to all connected clients.

This replaces both the old rtl-tcp container and the iq_multiplexer service.
The rtl_tcp binary itself runs in the same container (launched by the
Dockerfile CMD).  This script is the protocol-level multiplexer only.
"""

import asyncio
import logging
import os
import socket
import struct
import threading
import time
from collections import defaultdict

RTL_TCP_PORT = int(os.environ.get("RTL_TCP_PORT", "1234"))   # upstream (actual device)
MUX_PORT     = int(os.environ.get("MUX_PORT",     "1235"))   # downstream (decoders connect here)
RTL_TCP_HOST = os.environ.get("RTL_TCP_HOST", "127.0.0.1")

MAGIC = b"RTL0\x00\x00\x00\x00\x00\x00\x00\x00"  # 12-byte header sent to clients

logging.basicConfig(level=logging.INFO, format="%(asctime)s [bridge] %(message)s")
log = logging.getLogger(__name__)


class Multiplexer:
    def __init__(self) -> None:
        self._clients: dict[socket.socket, None] = {}
        self._lock = threading.Lock()
        self._upstream: socket.socket | None = None

    def add_client(self, sock: socket.socket) -> None:
        with self._lock:
            self._clients[sock] = None
        log.info("Client connected (total: %d)", len(self._clients))

    def remove_client(self, sock: socket.socket) -> None:
        with self._lock:
            self._clients.pop(sock, None)
        log.info("Client disconnected (total: %d)", len(self._clients))

    def broadcast(self, data: bytes) -> None:
        with self._lock:
            dead = []
            for sock in self._clients:
                try:
                    sock.sendall(data)
                except (OSError, BrokenPipeError):
                    dead.append(sock)
            for sock in dead:
                self._clients.pop(sock, None)

    def connect_upstream(self) -> socket.socket:
        delay = 1.0
        while True:
            try:
                sock = socket.create_connection((RTL_TCP_HOST, RTL_TCP_PORT), timeout=5)
                header = sock.recv(12)
                if not header.startswith(b"RTL"):
                    raise ValueError(f"Bad upstream header: {header!r}")
                self._upstream = sock
                log.info("Connected to upstream rtl_tcp at %s:%d", RTL_TCP_HOST, RTL_TCP_PORT)
                return sock
            except (OSError, ValueError) as e:
                log.warning("Upstream connect failed (%s), retry in %.0fs", e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30)

    def upstream_reader(self) -> None:
        """Continuously read from upstream and broadcast to all clients."""
        while True:
            upstream = self.connect_upstream()
            try:
                while True:
                    data = upstream.recv(65536)
                    if not data:
                        break
                    self.broadcast(data)
            except (OSError, ConnectionResetError) as e:
                log.warning("Upstream read error: %s, reconnecting…", e)
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass
            time.sleep(2)

    def handle_client(self, sock: socket.socket, addr) -> None:
        self.add_client(sock)
        # Send the rtl_tcp magic header so clients think they're talking to rtl_tcp
        try:
            sock.sendall(MAGIC)
            while True:
                # Forward any commands from this client to upstream
                cmd = sock.recv(5)
                if not cmd:
                    break
                if self._upstream:
                    try:
                        self._upstream.sendall(cmd)
                    except OSError:
                        pass
        except (OSError, ConnectionResetError):
            pass
        finally:
            self.remove_client(sock)
            sock.close()

    def run(self) -> None:
        # Start upstream reader thread
        reader = threading.Thread(target=self.upstream_reader, daemon=True)
        reader.start()

        # Accept client connections
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", MUX_PORT))
        server.listen(16)
        log.info("Multiplexer listening on :%d", MUX_PORT)

        while True:
            sock, addr = server.accept()
            t = threading.Thread(target=self.handle_client, args=(sock, addr), daemon=True)
            t.start()


if __name__ == "__main__":
    mux = Multiplexer()
    mux.run()
