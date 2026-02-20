"""
SSTV decoder service.

Monitors 14.230 MHz for SSTV signals via FM demodulation + VIS header
detection.  Decoded frames are base64-encoded as PNG and broadcast to all
WebSocket clients.

WebSocket message schema:
  { "type": "frame",  "imageData": "data:image/png;base64,...", "mode": "Robot36", "ts": ISO8601 }
  { "type": "status", "connected": true }
  { "type": "error",  "message": "..." }

Async IO: same thread+queue pattern as cw_decoder — blocking recv() in a
dedicated thread, asyncio loop drains via short-timeout run_in_executor.
"""

import asyncio
import base64
import io
import json
import logging
import os
import queue
import socket
import struct
import threading
import time
from datetime import datetime, timezone
from math import gcd

import numpy as np
import websockets
from PIL import Image
from scipy.signal import resample_poly

# ── Configuration ─────────────────────────────────────────────────────────────

RTL_HOST = os.environ.get("RTL_HOST", "rtl-bridge")
RTL_PORT = int(os.environ.get("RTL_PORT", "1235"))
WS_PORT  = int(os.environ.get("WS_PORT", "8766"))

SSTV_FREQ_HZ    = int(os.environ.get("SSTV_FREQ_HZ", str(14_230_000)))
SDR_SAMPLE_RATE = int(os.environ.get("SDR_SAMPLE_RATE", str(2_400_000)))
GAIN            = int(os.environ.get("GAIN", "420"))

AUDIO_SAMPLE_RATE = 48_000
READ_BYTES        = 65536

logging.basicConfig(level=logging.INFO, format="%(asctime)s [SSTV] %(message)s")
log = logging.getLogger(__name__)

# ── SSTV mode definitions ─────────────────────────────────────────────────────

SSTV_MODES = {
    0x88: {"name": "Robot36",   "lines": 240, "width": 320, "scan_ms": 150.0,   "color": "YUV"},
    0xAC: {"name": "MartinM1",  "lines": 256, "width": 320, "scan_ms": 146.432, "color": "RGB"},
    0x3C: {"name": "ScottieS1", "lines": 256, "width": 320, "scan_ms": 138.24,  "color": "RGB"},
    0x5F: {"name": "PD120",     "lines": 496, "width": 640, "scan_ms": 508.48,  "color": "PD"},
}

FREQ_SYNC  = 1200.0
FREQ_BLACK = 1500.0
FREQ_WHITE = 2300.0

# ── RTL-TCP helpers ───────────────────────────────────────────────────────────

def rtl_command(sock: socket.socket, cmd: int, param: int) -> None:
    sock.sendall(struct.pack(">BI", cmd, param))


def connect_rtl_sync() -> socket.socket:
    delay = 1.0
    while True:
        try:
            sock = socket.create_connection((RTL_HOST, RTL_PORT), timeout=10)
            header = sock.recv(12)
            if not header.startswith(b"RTL"):
                raise ValueError(f"Bad header: {header!r}")
            rtl_command(sock, 0x02, SDR_SAMPLE_RATE)
            rtl_command(sock, 0x04, GAIN)
            rtl_command(sock, 0x03, 1)
            log.info("Connected %s:%d SDR %d ksps", RTL_HOST, RTL_PORT, SDR_SAMPLE_RATE // 1000)
            return sock
        except (OSError, ValueError) as e:
            log.warning("Connect failed (%s), retry in %.0fs", e, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30)


# ── Reader thread ─────────────────────────────────────────────────────────────

def iq_reader_thread(raw_q: queue.Queue, stop: threading.Event) -> None:
    while not stop.is_set():
        sock = connect_rtl_sync()
        try:
            while not stop.is_set():
                data = sock.recv(READ_BYTES)
                if not data:
                    log.warning("EOF, reconnecting…")
                    break
                try:
                    raw_q.put(data, timeout=2.0)
                except queue.Full:
                    log.debug("IQ queue full — dropping")
        except (OSError, ConnectionResetError) as e:
            log.warning("Read error: %s, reconnecting…", e)
        finally:
            try:
                sock.close()
            except Exception:
                pass
        if not stop.is_set():
            time.sleep(2)


# ── FM demodulation + resample ────────────────────────────────────────────────

_UP   = AUDIO_SAMPLE_RATE // gcd(AUDIO_SAMPLE_RATE, SDR_SAMPLE_RATE)
_DOWN = SDR_SAMPLE_RATE   // gcd(AUDIO_SAMPLE_RATE, SDR_SAMPLE_RATE)


def iq_to_audio(raw: bytes) -> np.ndarray:
    u8 = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
    i  = (u8[0::2] - 127.5) / 127.5
    q  = (u8[1::2] - 127.5) / 127.5
    iq = i + 1j * q
    # FM discriminator
    conj   = iq[:-1] * np.conj(iq[1:])
    fm     = np.angle(conj) * (SDR_SAMPLE_RATE / (2 * np.pi))
    return resample_poly(fm, _UP, _DOWN).astype(np.float32)


# ── VIS detection + frame decode ──────────────────────────────────────────────

def goertzel(samples: np.ndarray, target_freq: float, sample_rate: int) -> float:
    n = len(samples)
    k = round(n * target_freq / sample_rate)
    w = 2 * np.pi * k / n
    coeff = 2 * np.cos(w)
    s1, s2 = 0.0, 0.0
    for s in samples:
        s0 = s + coeff * s1 - s2
        s2, s1 = s1, s0
    return s2 ** 2 + s1 ** 2 - coeff * s1 * s2


class VISDetector:
    LEADER_MS = 300
    BIT_MS    = 30
    F_LEADER  = 1900.0
    F_START   = 1200.0
    F_ZERO    = 1300.0
    F_ONE     = 1100.0

    def __init__(self, sr: int) -> None:
        self._sr     = sr
        self._buf: list[float] = []
        self._leader = int(self.LEADER_MS * sr / 1000)
        self._bit    = int(self.BIT_MS    * sr / 1000)

    def feed(self, audio: np.ndarray):
        self._buf.extend(audio.tolist())
        min_len = self._leader + self._bit * 11
        if len(self._buf) < min_len:
            return None
        step = max(1, self._bit // 2)
        for i in range(0, len(self._buf) - min_len, step):
            seg = np.array(self._buf[i : i + self._leader])
            if goertzel(seg, self.F_LEADER, self._sr) < \
               goertzel(seg, self.F_START, self._sr) * 2:
                continue
            vs = i + self._leader + int(10 * self._sr / 1000)
            seg_s = np.array(self._buf[vs : vs + self._bit])
            if len(seg_s) < self._bit:
                continue
            bits = []
            for b in range(8):
                off = vs + self._bit * (1 + b)
                seg_b = np.array(self._buf[off : off + self._bit])
                if len(seg_b) < self._bit:
                    break
                bits.append(0 if goertzel(seg_b, self.F_ZERO, self._sr) >
                                 goertzel(seg_b, self.F_ONE, self._sr) else 1)
            if len(bits) < 8:
                continue
            vis = sum(bits[b] << b for b in range(7))
            if vis in SSTV_MODES:
                end = vs + self._bit * 9
                self._buf = self._buf[end:]
                return vis
        if len(self._buf) > min_len * 4:
            self._buf = self._buf[-min_len * 2:]
        return None


class SSTVFrameDecoder:
    def __init__(self, mode: dict, sr: int) -> None:
        self._mode = mode
        self._sr   = sr

    def decode(self, audio: np.ndarray):
        mode        = self._mode
        lines       = mode["lines"]
        width       = mode["width"]
        scan_samps  = int(mode["scan_ms"] * self._sr / 1000)
        needed      = scan_samps * lines

        if len(audio) < needed:
            return None

        pixels = np.zeros((lines, width, 3), dtype=np.uint8)
        for line in range(lines):
            seg = audio[line * scan_samps : (line + 1) * scan_samps]
            for x in range(width):
                idx   = int(x * scan_samps / width)
                chunk = seg[max(0, idx - 2) : idx + 3]
                if len(chunk) == 0:
                    continue
                freq  = float(np.mean(chunk)) + 1900.0
                pv    = int(np.clip(
                    (freq - FREQ_BLACK) / (FREQ_WHITE - FREQ_BLACK) * 255,
                    0, 255,
                ))
                pixels[line, x] = [pv, pv, pv]
        return Image.fromarray(pixels, "RGB")


def image_to_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ── WebSocket server ──────────────────────────────────────────────────────────

CLIENTS: set = set()


async def broadcast(msg: dict) -> None:
    if not CLIENTS:
        return
    data = json.dumps(msg)
    await asyncio.gather(*[ws.send(data) for ws in list(CLIENTS)], return_exceptions=True)


async def ws_handler(websocket) -> None:
    CLIENTS.add(websocket)
    log.info("WS client connected (total: %d)", len(CLIENTS))
    try:
        await websocket.send(json.dumps({"type": "status", "connected": True}))
        await websocket.wait_closed()
    finally:
        CLIENTS.discard(websocket)
        log.info("WS client disconnected (total: %d)", len(CLIENTS))


# ── Decode loop ───────────────────────────────────────────────────────────────

async def decode_loop(raw_q: queue.Queue) -> None:
    loop         = asyncio.get_running_loop()
    vis_detector = VISDetector(AUDIO_SAMPLE_RATE)
    audio_buf    = np.array([], dtype=np.float32)
    active_mode  = None
    needed_audio = 0

    log.info("Monitoring %.4f MHz for SSTV", SSTV_FREQ_HZ / 1e6)

    while True:
        try:
            raw = await loop.run_in_executor(None, lambda: raw_q.get(timeout=1.0))
        except queue.Empty:
            continue

        audio    = iq_to_audio(raw)
        audio_buf = np.concatenate([audio_buf, audio])

        if active_mode is None:
            result = vis_detector.feed(audio)
            if result is not None:
                active_mode  = SSTV_MODES[result]
                needed_audio = int(
                    active_mode["scan_ms"] * AUDIO_SAMPLE_RATE / 1000
                ) * active_mode["lines"]
                log.info("VIS 0x%02X (%s) — collecting %.1fs of audio",
                         result, active_mode["name"], needed_audio / AUDIO_SAMPLE_RATE)
                audio_buf = np.array([], dtype=np.float32)
        else:
            if len(audio_buf) >= needed_audio:
                frame_audio = audio_buf[:needed_audio]
                audio_buf   = audio_buf[needed_audio:]
                img = SSTVFrameDecoder(active_mode, AUDIO_SAMPLE_RATE).decode(frame_audio)
                if img is not None:
                    data_url = image_to_data_url(img)
                    ts = datetime.now(timezone.utc).isoformat()
                    await broadcast({
                        "type": "frame",
                        "imageData": data_url,
                        "mode": active_mode["name"],
                        "ts": ts,
                    })
                    log.info("Frame decoded: %s at %s", active_mode["name"], ts)
                active_mode  = None
                vis_detector = VISDetector(AUDIO_SAMPLE_RATE)

        # Cap audio buffer
        if len(audio_buf) > AUDIO_SAMPLE_RATE * 60:
            audio_buf = audio_buf[-AUDIO_SAMPLE_RATE * 30:]


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    log.info("SSTV decoder — WS :%d, %.4f MHz, %d ksps",
             WS_PORT, SSTV_FREQ_HZ / 1e6, SDR_SAMPLE_RATE // 1000)

    raw_q = queue.Queue(maxsize=64)
    stop  = threading.Event()
    threading.Thread(
        target=iq_reader_thread, args=(raw_q, stop),
        daemon=True, name="sstv-iq-reader",
    ).start()

    ws_server = await websockets.serve(ws_handler, "0.0.0.0", WS_PORT)
    log.info("WebSocket on :%d", WS_PORT)

    try:
        await asyncio.gather(ws_server.serve_forever(), decode_loop(raw_q))
    finally:
        stop.set()


if __name__ == "__main__":
    asyncio.run(main())
