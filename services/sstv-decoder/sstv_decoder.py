"""
SSTV decoder service.

Monitors a configurable frequency for SSTV signals. When a complete frame
is detected, it is base64-encoded as a PNG and broadcast to all WebSocket
clients.

WebSocket message schema:
  { "type": "frame",  "imageData": "data:image/png;base64,...", "mode": "Robot36", "ts": ISO8601 }
  { "type": "status", "connected": true }
  { "type": "error",  "message": "..." }

SSTV signal flow:
  rtl_tcp → IQ samples → FM demodulate → detect VIS → decode lines → PNG → WS
"""

import asyncio
import base64
import io
import json
import logging
import os
import socket
import struct
from datetime import datetime, timezone

import numpy as np
import websockets
from PIL import Image
from scipy.signal import butter, lfilter, resample_poly

# ── Configuration ─────────────────────────────────────────────────────────────

RTL_HOST = os.environ.get("RTL_HOST", "rtl-bridge")
RTL_PORT = int(os.environ.get("RTL_PORT", "1234"))
WS_PORT = int(os.environ.get("WS_PORT", "8766"))

# SSTV is commonly found on 14.230 MHz USB
SSTV_FREQ_HZ = int(os.environ.get("SSTV_FREQ_HZ", str(14_230_000)))
LO_OFFSET_HZ = int(os.environ.get("LO_OFFSET_HZ", str(125_000_000)))
CENTER_FREQ_HZ = SSTV_FREQ_HZ + LO_OFFSET_HZ

SDR_SAMPLE_RATE = int(os.environ.get("SDR_SAMPLE_RATE", str(240_000)))
AUDIO_SAMPLE_RATE = 48_000  # SSTV decoding target rate
GAIN = int(os.environ.get("GAIN", "250"))

CHUNK_SIZE = 65536  # IQ bytes per read

logging.basicConfig(level=logging.INFO, format="%(asctime)s [SSTV] %(message)s")
log = logging.getLogger(__name__)

# ── SSTV mode definitions ─────────────────────────────────────────────────────
# Frequencies: 1200 Hz sync, 1500-2300 Hz pixel

SSTV_MODES = {
    0x88: {"name": "Robot36",   "lines": 240, "width": 320, "scan_ms": 150.0,  "color": "YUV"},
    0xAC: {"name": "MartinM1",  "lines": 256, "width": 320, "scan_ms": 146.432,"color": "RGB"},
    0x3C: {"name": "ScottieS1", "lines": 256, "width": 320, "scan_ms": 138.24, "color": "RGB"},
    0x5F: {"name": "PD120",     "lines": 496, "width": 640, "scan_ms": 508.48, "color": "PD"},
}

FREQ_SYNC  = 1200.0
FREQ_BLACK = 1500.0
FREQ_WHITE = 2300.0


# ── RTL-TCP helpers ───────────────────────────────────────────────────────────

def rtl_command(sock: socket.socket, cmd: int, param: int) -> None:
    sock.sendall(struct.pack(">BI", cmd, param))


async def connect_rtl() -> socket.socket:
    delay = 1.0
    while True:
        try:
            sock = socket.create_connection((RTL_HOST, RTL_PORT), timeout=5)
            header = sock.recv(12)
            if not header.startswith(b"RTL"):
                raise ValueError(f"Bad header: {header!r}")
            rtl_command(sock, 0x01, CENTER_FREQ_HZ)
            rtl_command(sock, 0x02, SDR_SAMPLE_RATE)
            rtl_command(sock, 0x04, GAIN)
            rtl_command(sock, 0x03, 1)
            log.info("Connected to rtl_tcp, tuned to %.4f MHz", CENTER_FREQ_HZ / 1e6)
            return sock
        except (OSError, ValueError) as e:
            log.warning("rtl_tcp connect failed (%s), retrying in %.0fs", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


# ── FM demodulation ───────────────────────────────────────────────────────────

def fm_demodulate(iq: np.ndarray) -> np.ndarray:
    """Discriminator-based FM demod → instantaneous frequency deviation."""
    conj = iq[:-1] * np.conj(iq[1:])
    return np.angle(conj) * (SDR_SAMPLE_RATE / (2 * np.pi))


def iq_to_audio(raw_bytes: bytes) -> np.ndarray:
    """Convert raw rtl_tcp uint8 IQ bytes → audio float32 at AUDIO_SAMPLE_RATE."""
    u8 = np.frombuffer(raw_bytes, dtype=np.uint8).astype(np.float32)
    i = (u8[0::2] - 127.5) / 127.5
    q = (u8[1::2] - 127.5) / 127.5
    iq = i + 1j * q
    audio = fm_demodulate(iq)
    # Resample from SDR_SAMPLE_RATE → AUDIO_SAMPLE_RATE
    from math import gcd
    g = gcd(AUDIO_SAMPLE_RATE, SDR_SAMPLE_RATE)
    up = AUDIO_SAMPLE_RATE // g
    down = SDR_SAMPLE_RATE // g
    return resample_poly(audio, up, down).astype(np.float32)


# ── SSTV detection & decoding ─────────────────────────────────────────────────

def goertzel(samples: np.ndarray, target_freq: float, sample_rate: int) -> float:
    """Goertzel algorithm — power at target_freq."""
    n = len(samples)
    k = round(n * target_freq / sample_rate)
    w = 2 * np.pi * k / n
    coeff = 2 * np.cos(w)
    s_prev, s_prev2 = 0.0, 0.0
    for sample in samples:
        s = sample + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    power = s_prev2 ** 2 + s_prev ** 2 - coeff * s_prev * s_prev2
    return power


class VISDetector:
    """Detects the SSTV VIS header in a stream of audio samples."""

    VIS_LEADER_MS  = 300   # leader tone at 1900 Hz
    VIS_BREAK_MS   = 10
    VIS_START_MS   = 300   # start tone at 1200 Hz
    VIS_BIT_MS     = 30

    FREQ_LEADER = 1900.0
    FREQ_START  = 1200.0
    FREQ_ONE    = 1100.0
    FREQ_ZERO   = 1300.0

    def __init__(self, sample_rate: int) -> None:
        self._sr = sample_rate
        self._buf: list[float] = []
        self._state = "idle"
        self._bits: list[int] = []
        self._leader_samples = int(self.VIS_LEADER_MS * sample_rate / 1000)
        self._bit_samples = int(self.VIS_BIT_MS * sample_rate / 1000)

    def feed(self, audio: np.ndarray):
        """Returns vis_code (int) or None if no VIS detected yet."""
        self._buf.extend(audio.tolist())
        return self._scan()

    def _scan(self):
        sr = self._sr
        buf = self._buf

        # Need at least leader + start + 8 bits + parity + stop
        min_samples = self._leader_samples + self._bit_samples * 11
        if len(buf) < min_samples:
            return None

        window = int(self._bit_samples * 0.9)
        for i in range(0, len(buf) - min_samples, window // 2):
            segment = np.array(buf[i : i + self._leader_samples])
            p_leader = goertzel(segment, self.FREQ_LEADER, sr)
            p_sync   = goertzel(segment, self.FREQ_START,  sr)
            if p_leader < p_sync * 2:
                continue
            # Found leader candidate — try to read VIS bits
            vis_start = i + self._leader_samples + int(self.VIS_BREAK_MS * sr / 1000)
            # Start bit at 1200 Hz
            seg_start = np.array(buf[vis_start : vis_start + self._bit_samples])
            if len(seg_start) < self._bit_samples:
                continue
            p_start = goertzel(seg_start, self.FREQ_START, sr)
            if p_start < 1e-6:
                continue
            # Read 7 data bits + 1 parity
            bits = []
            for b in range(8):
                offset = vis_start + self._bit_samples * (1 + b)
                seg = np.array(buf[offset : offset + self._bit_samples])
                if len(seg) < self._bit_samples:
                    break
                p0 = goertzel(seg, self.FREQ_ZERO, sr)
                p1 = goertzel(seg, self.FREQ_ONE,  sr)
                bits.append(0 if p0 > p1 else 1)
            if len(bits) < 8:
                continue
            vis_code = sum(bits[b] << b for b in range(7))
            if vis_code in SSTV_MODES:
                frame_end = vis_start + self._bit_samples * 9
                # Trim buffer to just after VIS header
                self._buf = buf[frame_end:]
                return vis_code, frame_end

        # Trim old data to avoid unbounded growth
        if len(buf) > min_samples * 4:
            self._buf = buf[-min_samples * 2:]
        return None


class SSTVFrameDecoder:
    """Decodes a single SSTV frame from audio samples."""

    def __init__(self, mode: dict, sample_rate: int) -> None:
        self._mode = mode
        self._sr = sample_rate

    def decode(self, audio: np.ndarray) -> Image.Image | None:
        """Returns a PIL Image or None if not enough data."""
        mode = self._mode
        lines = mode["lines"]
        width = mode["width"]
        scan_samples = int(mode["scan_ms"] * self._sr / 1000)
        total_samples = scan_samples * lines

        if len(audio) < total_samples:
            return None

        pixels = np.zeros((lines, width, 3), dtype=np.uint8)

        for line in range(lines):
            line_audio = audio[line * scan_samples : (line + 1) * scan_samples]
            # Map frequency → pixel value
            # FREQ_BLACK=1500 → 0, FREQ_WHITE=2300 → 255
            for x in range(width):
                sample_idx = int(x * scan_samples / width)
                chunk = line_audio[max(0, sample_idx - 2) : sample_idx + 3]
                if len(chunk) == 0:
                    continue
                # Rough instantaneous frequency via zero-crossing rate
                # For proper decoding use Goertzel at each pixel — simplified here
                freq = np.mean(chunk) + 1900.0  # bias from FM demod
                pv = int(np.clip((freq - FREQ_BLACK) / (FREQ_WHITE - FREQ_BLACK) * 255, 0, 255))

                if mode["color"] == "RGB":
                    # R/G/B encoded in 3 separate passes per line — simplified: grayscale
                    pixels[line, x] = [pv, pv, pv]
                else:
                    pixels[line, x] = [pv, pv, pv]

        return Image.fromarray(pixels, "RGB")


def image_to_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


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


# ── Main decode loop ──────────────────────────────────────────────────────────

async def decode_loop() -> None:
    vis_detector = VISDetector(AUDIO_SAMPLE_RATE)
    audio_buf = np.array([], dtype=np.float32)
    decoding_mode: dict | None = None
    frame_audio_needed = 0

    while True:
        try:
            sock = await connect_rtl()
        except Exception as e:
            log.error("Cannot connect rtl_tcp: %s", e)
            await asyncio.sleep(5)
            continue

        log.info("SSTV monitoring %.4f MHz", SSTV_FREQ_HZ / 1e6)
        raw_buf = b""

        try:
            while True:
                chunk = await asyncio.get_event_loop().run_in_executor(
                    None, sock.recv, CHUNK_SIZE
                )
                if not chunk:
                    break
                raw_buf += chunk

                # Process in complete blocks
                block_size = CHUNK_SIZE
                while len(raw_buf) >= block_size:
                    raw = raw_buf[:block_size]
                    raw_buf = raw_buf[block_size:]
                    audio = iq_to_audio(raw)
                    audio_buf = np.concatenate([audio_buf, audio])

                    if decoding_mode is None:
                        result = vis_detector.feed(audio)
                        if result is not None:
                            vis_code, _ = result
                            decoding_mode = SSTV_MODES[vis_code]
                            frame_audio_needed = int(
                                decoding_mode["scan_ms"] * AUDIO_SAMPLE_RATE / 1000
                            ) * decoding_mode["lines"]
                            log.info("VIS detected: 0x%02X (%s) — collecting frame", vis_code, decoding_mode["name"])
                            audio_buf = np.array([], dtype=np.float32)
                    else:
                        if len(audio_buf) >= frame_audio_needed:
                            frame_audio = audio_buf[:frame_audio_needed]
                            audio_buf = audio_buf[frame_audio_needed:]

                            decoder = SSTVFrameDecoder(decoding_mode, AUDIO_SAMPLE_RATE)
                            img = decoder.decode(frame_audio)
                            if img is not None:
                                data_url = image_to_data_url(img)
                                ts = datetime.now(timezone.utc).isoformat()
                                await broadcast({
                                    "type": "frame",
                                    "imageData": data_url,
                                    "mode": decoding_mode["name"],
                                    "ts": ts,
                                })
                                log.info("SSTV frame decoded: %s at %s", decoding_mode["name"], ts)

                            decoding_mode = None
                            vis_detector = VISDetector(AUDIO_SAMPLE_RATE)

                    # Keep audio buffer bounded
                    max_audio = AUDIO_SAMPLE_RATE * 30
                    if len(audio_buf) > max_audio:
                        audio_buf = audio_buf[-max_audio:]

        except (OSError, ConnectionResetError) as e:
            log.warning("rtl_tcp lost: %s, reconnecting…", e)
            try:
                sock.close()
            except Exception:
                pass
            await asyncio.sleep(2)
            vis_detector = VISDetector(AUDIO_SAMPLE_RATE)
            audio_buf = np.array([], dtype=np.float32)
            decoding_mode = None


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    log.info("SSTV decoder starting — WebSocket port %d, SSTV freq %.4f MHz", WS_PORT, SSTV_FREQ_HZ / 1e6)
    ws_server = await websockets.serve(ws_handler, "0.0.0.0", WS_PORT)
    log.info("WebSocket server listening on :%d", WS_PORT)
    await asyncio.gather(ws_server.serve_forever(), decode_loop())


if __name__ == "__main__":
    asyncio.run(main())
