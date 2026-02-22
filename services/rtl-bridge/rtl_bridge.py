"""
RTL-TCP bridge / multiplexer.

Sits between the physical rtl_tcp process and the decoder services.
Exposes the same rtl_tcp protocol on a configurable port, forwarding
commands upstream and broadcasting IQ data to all connected clients.

Also exposes a WebSocket server (WS_PORT) that streams raw uint8 IQ
bytes to browser clients.  The browser does all waterfall FFT
processing in a Web Worker — no Python decoder services are involved.

Audio ports (AUDIO_CW_PORT, AUDIO_SSTV_PORT, AUDIO_EP_PORT) each run
an AudioMux that decimates the IQ stream 100× to 24 kHz complex64 and
distributes the result to the relevant decoder service.  Decimation is
done once per frequency here rather than redundantly in each service.
"""

import asyncio
import contextlib
import logging
import os
import queue
import socket
import threading
import time

import numpy as np
import websockets
from scipy.signal import cheby1, sosfilt

RTL_TCP_PORT = int(os.environ.get("RTL_TCP_PORT", "1234"))   # upstream (actual device)
MUX_PORT     = int(os.environ.get("MUX_PORT",     "1235"))   # downstream (TCP decoders)
WS_PORT      = int(os.environ.get("WS_PORT",      "1236"))   # browser WebSocket IQ stream
RTL_TCP_HOST = os.environ.get("RTL_TCP_HOST", "127.0.0.1")

AUDIO_CW_PORT   = int(os.environ.get("AUDIO_CW_PORT",   "1237"))
AUDIO_SSTV_PORT = int(os.environ.get("AUDIO_SSTV_PORT", "1238"))
AUDIO_EP_PORT   = int(os.environ.get("AUDIO_EP_PORT",   "1239"))

RF_CENTER_HZ = 14_175_000
CW_FREQ_HZ   = 14_029_000   # offset from RF centre = -146_000
SSTV_FREQ_HZ = 14_230_000   # offset from RF centre = +55_000
EP_FREQ_HZ   = 14_233_000   # offset from RF centre = +58_000

SDR_RATE   = 2_400_000
AUDIO_RATE = 24_000          # 100x decimation (10x x 10x)

MAGIC       = b"RTL0\x00\x00\x00\x00\x00\x00\x00\x00"  # 12-byte header for IQ clients
AUDIO_MAGIC = b"AUD0\x00\x00\x00\x00\x00\x00\x00\x00"  # 12-byte header for audio clients

# Two-stage Chebyshev Type-I IIR antialiasing filters — much faster than Kaiser FIR.
# Each stage removes aliases before 10x decimation.
# Stage 1 cutoff: 0.1 x SDR_RATE  = 240 kHz -> passband to 120 kHz
# Stage 2 cutoff: 0.1 x 240 kHz   = 24 kHz  -> passband to 12 kHz
# 0.05 dB passband ripple, order 8 — gives >60 dB stopband attenuation.
_SOS1 = cheby1(8, 0.05, 0.1, output='sos')
_SOS2 = cheby1(8, 0.05, 0.1, output='sos')

logging.basicConfig(level=logging.INFO, format="%(asctime)s [bridge] %(message)s")
log = logging.getLogger(__name__)


# -- Audio decimator -----------------------------------------------------------

class AudioDecimator:
    """
    Decimates raw uint8 IQ (2.4 Msps) to complex64 at 24 kHz for one target frequency.

    Steps:
      1. Parse uint8 IQ pairs -> complex64 at SDR_RATE.
      2. Mix by -freq_offset_hz to shift target to DC.
      3. Stage-1: Chebyshev IIR LPF + 10x decimate.
      4. Stage-2: Chebyshev IIR LPF + 10x decimate -> 24 kHz complex64.

    IIR filter state (_zi1, _zi2) is preserved across successive calls so
    the output is phase- and amplitude-continuous across chunk boundaries.
    """

    def __init__(self, freq_offset_hz: float) -> None:
        self._step  = 2 * np.pi * freq_offset_hz / SDR_RATE
        self._phase = 0.0
        # sosfilt zi shape: (n_sections, 2) per channel
        n_sec1 = _SOS1.shape[0]
        n_sec2 = _SOS2.shape[0]
        self._zi1_re = np.zeros((n_sec1, 2))
        self._zi1_im = np.zeros((n_sec1, 2))
        self._zi2_re = np.zeros((n_sec2, 2))
        self._zi2_im = np.zeros((n_sec2, 2))

    def process(self, raw: bytes) -> np.ndarray:
        """raw: uint8 IQ bytes from rtl_tcp. Returns complex64 at AUDIO_RATE."""
        u8 = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        if len(u8) & 1:
            u8 = u8[:-1]
        iq = ((u8[0::2] - 127.5) + 1j * (u8[1::2] - 127.5)) / 127.5
        n = len(iq)

        # Mix target frequency to DC
        phases      = self._phase + self._step * np.arange(n, dtype=np.float64)
        self._phase = float(phases[-1] + self._step)
        lo          = np.exp(-1j * phases).astype(np.complex64)
        mixed       = (iq * lo).astype(np.complex64)

        # Stage 1: Chebyshev IIR LPF + 10x decimate
        re1, self._zi1_re = sosfilt(_SOS1, mixed.real, zi=self._zi1_re)
        im1, self._zi1_im = sosfilt(_SOS1, mixed.imag, zi=self._zi1_im)
        stage1 = (re1[9::10] + 1j * im1[9::10]).astype(np.complex64)

        # Stage 2: Chebyshev IIR LPF + 10x decimate -> AUDIO_RATE
        re2, self._zi2_re = sosfilt(_SOS2, stage1.real, zi=self._zi2_re)
        im2, self._zi2_im = sosfilt(_SOS2, stage1.imag, zi=self._zi2_im)
        return (re2[9::10] + 1j * im2[9::10]).astype(np.complex64)


# -- Audio mux -----------------------------------------------------------------

class AudioMux:
    """
    TCP server that distributes pre-decimated complex64@24kHz audio to one
    decoder service.

    A single background thread runs the decimation loop (consuming raw IQ from
    a bounded queue and broadcasting audio bytes to all connected clients).
    Client threads call handle_client() which holds the connection open while
    the decimation worker sends data.
    """

    def __init__(self, port: int, freq_offset_hz: float) -> None:
        self._port      = port
        self._decimator = AudioDecimator(freq_offset_hz)
        self._clients: dict[socket.socket, None] = {}
        self._lock      = threading.Lock()
        self._raw_queue: queue.Queue[bytes] = queue.Queue(maxsize=8)

    def add_raw(self, data: bytes) -> None:
        """Called from broadcast() to enqueue raw IQ; drops oldest if full."""
        try:
            self._raw_queue.put_nowait(data)
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                self._raw_queue.get_nowait()
            with contextlib.suppress(queue.Full):
                self._raw_queue.put_nowait(data)

    def _decimation_worker(self) -> None:
        """Background thread: decimate IQ chunks and broadcast to audio clients."""
        while True:
            raw     = self._raw_queue.get()
            audio   = self._decimator.process(raw)
            payload = audio.tobytes()
            with self._lock:
                dead = []
                for sock in list(self._clients):
                    try:
                        sock.setblocking(False)
                        sock.sendall(payload)
                        sock.setblocking(True)
                    except (BlockingIOError, OSError, BrokenPipeError):
                        dead.append(sock)
                for sock in dead:
                    self._clients.pop(sock, None)

    def handle_client(self, sock: socket.socket, addr) -> None:
        """Handle one audio decoder client connection."""
        log.info("AudioMux :%d -- client connected from %s", self._port, addr)
        try:
            sock.sendall(AUDIO_MAGIC)
            with self._lock:
                self._clients[sock] = None
            # Hold connection open; the decimation worker sends data asynchronously
            while True:
                data = sock.recv(16)
                if not data:
                    break
        except (OSError, ConnectionResetError):
            pass
        finally:
            with self._lock:
                self._clients.pop(sock, None)
            with contextlib.suppress(Exception):
                sock.close()
            log.info("AudioMux :%d -- client disconnected", self._port)

    def serve(self) -> None:
        """Start decimation worker and accept client connections (blocking)."""
        t = threading.Thread(target=self._decimation_worker, daemon=True,
                             name=f"audio-decimate-{self._port}")
        t.start()

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", self._port))
        server.listen(4)
        log.info("AudioMux listening on :%d", self._port)

        while True:
            sock, addr = server.accept()
            t = threading.Thread(target=self.handle_client, args=(sock, addr),
                                 daemon=True, name=f"audio-client-{self._port}")
            t.start()


# -- IQ multiplexer ------------------------------------------------------------

class Multiplexer:
    def __init__(self) -> None:
        self._clients: dict[socket.socket, None] = {}
        self._lock = threading.Lock()
        self._upstream: socket.socket | None = None
        # WebSocket clients (browser IQ stream)
        self._ws_clients: set = set()
        self._ws_lock = threading.Lock()
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        # Audio muxes (populated in run())
        self._audio_muxes: list[AudioMux] = []

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
        # TCP clients -- non-blocking send: drop the chunk rather than stalling
        # the upstream reader thread.  A stalled upstream reader causes rtl_tcp
        # to overflow its USB ring buffer (the "ll+" messages), corrupting all
        # downstream data.  Decoders that fall behind simply miss some chunks;
        # they still see a continuous, real-time IQ stream when they catch up.
        with self._lock:
            dead = []
            for sock in self._clients:
                try:
                    sock.setblocking(False)
                    sock.sendall(data)
                    sock.setblocking(True)
                except BlockingIOError:
                    pass  # client is slow: drop this chunk, keep connection
                except (OSError, BrokenPipeError):
                    dead.append(sock)
            for sock in dead:
                self._clients.pop(sock, None)

        # Feed audio mux queues for each decoder frequency
        for mux in self._audio_muxes:
            mux.add_raw(data)

        # WebSocket clients -- schedule coroutines onto the asyncio event loop
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
                log.warning("Upstream read error: %s, reconnecting...", e)
            finally:
                with contextlib.suppress(Exception):
                    upstream.close()
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
                    with contextlib.suppress(OSError):
                        self._upstream.sendall(cmd)
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

        # Create and start audio mux instances for each decoder frequency
        cw_mux   = AudioMux(AUDIO_CW_PORT,   CW_FREQ_HZ   - RF_CENTER_HZ)
        sstv_mux = AudioMux(AUDIO_SSTV_PORT, SSTV_FREQ_HZ - RF_CENTER_HZ)
        ep_mux   = AudioMux(AUDIO_EP_PORT,   EP_FREQ_HZ   - RF_CENTER_HZ)
        self._audio_muxes = [cw_mux, sstv_mux, ep_mux]
        for mux in self._audio_muxes:
            t = threading.Thread(target=mux.serve, daemon=True,
                                 name=f"audio-mux-{mux._port}")
            t.start()

        # Accept TCP client connections (blocking -- runs in main thread)
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
