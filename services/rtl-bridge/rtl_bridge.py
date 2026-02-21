"""
RTL-TCP bridge / multiplexer.

Sits between the physical rtl_tcp process and the decoder services.
Exposes the same rtl_tcp protocol on a configurable port, forwarding
commands upstream and broadcasting IQ data to all connected clients.

Also exposes a WebSocket server (WS_PORT) that streams raw uint8 IQ
bytes to browser clients.  The browser does all signal processing
(CW decode, waterfall FFT) in Web Workers — no Python decoder services
are needed.

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

import websockets

RTL_TCP_PORT = int(os.environ.get("RTL_TCP_PORT", "1234"))   # upstream (actual device)
MUX_PORT     = int(os.environ.get("MUX_PORT",     "1235"))   # downstream (TCP decoders)
WS_PORT      = int(os.environ.get("WS_PORT",      "1236"))   # browser WebSocket IQ stream
RTL_TCP_HOST = os.environ.get("RTL_TCP_HOST", "127.0.0.1")

MAGIC = b"RTL0\x00\x00\x00\x00\x00\x00\x00\x00"  # 12-byte header sent to clients

logging.basicConfig(level=logging.INFO, format="%(asctime)s [bridge] %(message)s")
log = logging.getLogger(__name__)


class Multiplexer:
    def __init__(self) -> None:
        self._clients: dict[socket.socket, None] = {}
        self._lock = threading.Lock()
        self._upstream: socket.socket | None = None
        # WebSocket clients (browser IQ stream)
        self._ws_clients: set = set()
        self._ws_lock = threading.Lock()
        self._ws_loop: asyncio.AbstractEventLoop | None = None

    def add_client(self, sock: socket.socket) -> None:
        with self._lock:
            self._clients[sock] = None
        log.info("TCP client connected (total: %d)", len(self._clients))

    def remove_client(self, sock: socket.socket) -> None:
        with self._lock:
            self._clients.pop(sock, None)
        log.info("TCP client disconnected (total: %d)", len(self._clients))

    def add_ws_client(self, ws) -> None:
        with self._ws_lock:
            self._ws_clients.add(ws)
        log.info("WS client connected (total: %d)", len(self._ws_clients))

    def remove_ws_client(self, ws) -> None:
        with self._ws_lock:
            self._ws_clients.discard(ws)
        log.info("WS client disconnected (total: %d)", len(self._ws_clients))

    def broadcast(self, data: bytes) -> None:
        # TCP clients
        with self._lock:
            dead = []
            for sock in self._clients:
                try:
                    sock.sendall(data)
                except (OSError, BrokenPipeError):
                    dead.append(sock)
            for sock in dead:
                self._clients.pop(sock, None)

        # WebSocket clients — schedule coroutines onto the asyncio event loop
        if self._ws_loop and self._ws_loop.is_running():
            with self._ws_lock:
                clients = list(self._ws_clients)
            if clients:
                asyncio.run_coroutine_threadsafe(
                    self._ws_broadcast(data, clients), self._ws_loop
                )

    async def _ws_broadcast(self, data: bytes, clients: list) -> None:
        dead = []
        for ws in clients:
            try:
                await ws.send(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove_ws_client(ws)

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

    async def ws_handler(self, websocket) -> None:
        """Handle a single browser WebSocket IQ stream connection."""
        self.add_ws_client(websocket)
        try:
            # Keep connection open; browser only receives, never sends
            await websocket.wait_closed()
        finally:
            self.remove_ws_client(websocket)

    async def _run_ws_server(self) -> None:
        self._ws_loop = asyncio.get_running_loop()
        log.info("WebSocket IQ server listening on :%d", WS_PORT)
        async with websockets.serve(self.ws_handler, "0.0.0.0", WS_PORT):
            await asyncio.Future()  # run forever

    def run(self) -> None:
        # Start upstream reader thread
        reader = threading.Thread(target=self.upstream_reader, daemon=True)
        reader.start()

        # Start WebSocket server in a background thread with its own event loop
        ws_thread = threading.Thread(
            target=lambda: asyncio.run(self._run_ws_server()),
            daemon=True, name="ws-iq-server",
        )
        ws_thread.start()

        # Accept TCP client connections (blocking — runs in main thread)
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", MUX_PORT))
        server.listen(16)
        log.info("TCP multiplexer listening on :%d", MUX_PORT)

        while True:
            sock, addr = server.accept()
            t = threading.Thread(target=self.handle_client, args=(sock, addr), daemon=True)
            t.start()


if __name__ == "__main__":
    mux = Multiplexer()
    mux.run()
