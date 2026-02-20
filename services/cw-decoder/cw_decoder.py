"""
CW (Morse code) decoder service.

Connects to rtl_tcp, tunes to a configurable CW frequency, performs envelope
detection + Butterworth lowpass filtering, then decodes Morse code in real time.
Decoded characters are broadcast to all connected WebSocket clients as JSON.

WebSocket message schema:
  { "type": "char",       "ts": ISO8601, "char": "A", "freq": 14000000, "power": -12.3 }
  { "type": "word_space" }
  { "type": "status",    "connected": true, "freq": 14000000 }
  { "type": "error",     "message": "..." }
"""

import asyncio
import json
import logging
import os
import socket
import struct
import time
from collections import deque
from datetime import datetime, timezone

import numpy as np
import websockets
from scipy.signal import butter, lfilter

# ── Configuration ─────────────────────────────────────────────────────────────

RTL_HOST = os.environ.get("RTL_HOST", "rtl-bridge")
RTL_PORT = int(os.environ.get("RTL_PORT", "1234"))
WS_PORT = int(os.environ.get("WS_PORT", "8765"))

# CW frequency to decode (Hz)
CW_FREQ_HZ = int(os.environ.get("CW_FREQ_HZ", str(14_000_000)))
# Upconverter LO offset (Hz). Set 0 if no upconverter.
LO_OFFSET_HZ = int(os.environ.get("LO_OFFSET_HZ", str(125_000_000)))
# SDR center frequency (what we ask rtl_tcp to tune to)
CENTER_FREQ_HZ = CW_FREQ_HZ + LO_OFFSET_HZ

SAMPLE_RATE = int(os.environ.get("SAMPLE_RATE", str(240_000)))
GAIN = int(os.environ.get("GAIN", "250"))  # tenths of dB (250 = 25.0 dB)

# Morse timing
WPM = float(os.environ.get("WPM", "20"))
DIT_SAMPLES = int((60.0 / (50 * WPM)) * SAMPLE_RATE)  # samples per dit
DAH_THRESHOLD = 2.5   # dah/dit ratio threshold
CHAR_GAP_DITS = 3.0   # gap ≥ 3 dits → char boundary
WORD_GAP_DITS = 7.0   # gap ≥ 7 dits → word boundary

# Detection
ENVELOPE_LPF_CUTOFF = 0.05   # fraction of Nyquist
DETECTION_THRESHOLD = 0.3     # fraction of envelope max (adaptive)
CHUNK_SAMPLES = 2048

logging.basicConfig(level=logging.INFO, format="%(asctime)s [CW] %(message)s")
log = logging.getLogger(__name__)

# ── Morse code dictionary ─────────────────────────────────────────────────────

MORSE_CODE = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E",
    "..-.": "F", "--.": "G", "....": "H", "..": "I", ".---": "J",
    "-.-": "K", ".-..": "L", "--": "M", "-.": "N", "---": "O",
    ".--.": "P", "--.-": "Q", ".-.": "R", "...": "S", "-": "T",
    "..-": "U", "...-": "V", ".--": "W", "-..-": "X", "-.--": "Y",
    "--..": "Z",
    "-----": "0", ".----": "1", "..---": "2", "...--": "3", "....-": "4",
    ".....": "5", "-....": "6", "--...": "7", "---..": "8", "----.": "9",
    ".-.-.-": ".", "--..--": ",", "..--..": "?", ".----.": "'",
    "-.-.--": "!", "-..-.": "/", "-.--.": "(", "-.--.-": ")",
    ".-...": "&", "---...": ":", "-.-.-.": ";", "-...-": "=",
    ".-.-.": "+", "-....-": "-", "..--.-": "_", ".-..-.": '"',
    "...-..-": "$", ".--.-.": "@", "...---...": "SOS",
}

# ── RTL-TCP client ────────────────────────────────────────────────────────────

def rtl_command(sock: socket.socket, cmd: int, param: int) -> None:
    """Send a 5-byte command to rtl_tcp."""
    sock.sendall(struct.pack(">BI", cmd, param))


async def connect_rtl_tcp() -> socket.socket:
    """Connect to rtl_tcp with retry logic."""
    delay = 1.0
    while True:
        try:
            sock = socket.create_connection((RTL_HOST, RTL_PORT), timeout=5)
            # Read the 12-byte magic header
            header = sock.recv(12)
            if not header.startswith(b"RTL"):
                raise ValueError(f"Bad rtl_tcp header: {header!r}")
            rtl_command(sock, 0x01, CENTER_FREQ_HZ)   # set frequency
            rtl_command(sock, 0x02, SAMPLE_RATE)       # set sample rate
            rtl_command(sock, 0x04, GAIN)              # set gain
            rtl_command(sock, 0x03, 1)                 # manual gain mode
            log.info("Connected to rtl_tcp at %s:%d, tuned to %.4f MHz", RTL_HOST, RTL_PORT, CENTER_FREQ_HZ / 1e6)
            return sock
        except (OSError, ValueError) as e:
            log.warning("rtl_tcp connect failed (%s), retry in %.0fs", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


# ── Signal processing ─────────────────────────────────────────────────────────

def make_lpf(cutoff: float, order: int = 3):
    b, a = butter(order, cutoff, btype="low")
    return b, a


class MorseDecoder:
    """State machine that turns a stream of on/off durations into characters."""

    def __init__(self) -> None:
        self._symbols: list[str] = []
        self._pending_gap: int = 0  # gap length in samples since last tone

    def push_tone(self, duration: int) -> None:
        if duration < DIT_SAMPLES * 0.5:
            return  # too short, ignore
        symbol = "." if duration < DIT_SAMPLES * DAH_THRESHOLD else "-"
        self._symbols.append(symbol)

    def push_gap(self, duration: int, callback) -> None:
        """Called when a gap is detected. callback(char_or_space)."""
        gap_dits = duration / DIT_SAMPLES
        if gap_dits >= WORD_GAP_DITS:
            self._flush_char(callback)
            callback(None)  # None → word space
        elif gap_dits >= CHAR_GAP_DITS:
            self._flush_char(callback)

    def _flush_char(self, callback) -> None:
        if self._symbols:
            code = "".join(self._symbols)
            char = MORSE_CODE.get(code, f"[{code}]")
            callback(char)
            self._symbols.clear()


class EnvelopeDetector:
    def __init__(self, sample_rate: int) -> None:
        self._b, self._a = make_lpf(ENVELOPE_LPF_CUTOFF)
        self._zi = np.zeros(max(len(self._b), len(self._a)) - 1)
        self._sr = sample_rate
        # Rolling window for adaptive threshold
        self._recent = deque(maxlen=sample_rate * 2)  # 2 s history

    def process(self, iq: np.ndarray):
        """
        iq: complex64 samples
        Returns: (envelope float32 array, threshold float)
        """
        envelope = np.abs(iq)
        filtered, self._zi = lfilter(self._b, self._a, envelope, zi=self._zi)
        self._recent.extend(filtered.tolist())
        thresh = np.percentile(list(self._recent), 70) * 1.5 if len(self._recent) > 100 else 0.1
        return filtered, thresh


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
        await websocket.send(json.dumps({"type": "status", "connected": True, "freq": CW_FREQ_HZ}))
        await websocket.wait_closed()
    finally:
        CLIENTS.discard(websocket)
        log.info("WS client disconnected (total: %d)", len(CLIENTS))


# ── Main decode loop ──────────────────────────────────────────────────────────

async def decode_loop() -> None:
    detector = EnvelopeDetector(SAMPLE_RATE)
    morse = MorseDecoder()
    b_lpf, a_lpf = make_lpf(0.1)

    tone_on = False
    tone_start = 0
    gap_start = 0
    sample_clock = 0

    chars_queued: asyncio.Queue = asyncio.Queue()

    def on_char(c) -> None:
        chars_queued.put_nowait(c)

    async def char_sender():
        while True:
            c = await chars_queued.get()
            now = datetime.now(timezone.utc).isoformat()
            if c is None:
                await broadcast({"type": "word_space"})
            else:
                await broadcast({"type": "char", "ts": now, "char": c, "freq": CW_FREQ_HZ, "power": 0.0})
            chars_queued.task_done()

    asyncio.ensure_future(char_sender())

    while True:
        try:
            sock = await connect_rtl_tcp()
        except Exception as e:
            log.error("Cannot connect to rtl_tcp: %s", e)
            await asyncio.sleep(5)
            continue

        log.info("Starting decode loop at %.4f MHz, %d WPM", CW_FREQ_HZ / 1e6, int(WPM))
        buf = b""

        try:
            while True:
                chunk = await asyncio.get_event_loop().run_in_executor(None, sock.recv, CHUNK_SAMPLES * 2)
                if not chunk:
                    break
                buf += chunk

                # Consume complete chunks
                needed = CHUNK_SAMPLES * 2
                while len(buf) >= needed:
                    raw = buf[:needed]
                    buf = buf[needed:]

                    # Convert uint8 I/Q to complex64
                    samples_u8 = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
                    i = (samples_u8[0::2] - 127.5) / 127.5
                    q = (samples_u8[1::2] - 127.5) / 127.5
                    iq = i + 1j * q

                    envelope, threshold = detector.process(iq)

                    for sample in envelope:
                        is_tone = sample > threshold
                        if is_tone and not tone_on:
                            # Rising edge
                            if gap_start > 0:
                                gap_len = sample_clock - gap_start
                                morse.push_gap(gap_len, on_char)
                            tone_on = True
                            tone_start = sample_clock
                        elif not is_tone and tone_on:
                            # Falling edge
                            tone_len = sample_clock - tone_start
                            morse.push_tone(tone_len)
                            tone_on = False
                            gap_start = sample_clock
                        sample_clock += 1

        except (OSError, ConnectionResetError) as e:
            log.warning("rtl_tcp connection lost: %s, reconnecting…", e)
            try:
                sock.close()
            except Exception:
                pass
            await asyncio.sleep(2)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    log.info("CW decoder starting — WebSocket port %d, CW freq %.4f MHz", WS_PORT, CW_FREQ_HZ / 1e6)

    ws_server = await websockets.serve(ws_handler, "0.0.0.0", WS_PORT)
    log.info("WebSocket server listening on :%d", WS_PORT)

    await asyncio.gather(
        ws_server.serve_forever(),
        decode_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
