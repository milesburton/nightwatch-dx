"""
CW (Morse code) decoder service.

Connects to rtl-bridge multiplexer, decimates the wideband IQ stream,
performs envelope detection + Butterworth LPF, decodes Morse code in real
time, and broadcasts characters to WebSocket clients.

WebSocket message schema:
  { "type": "char",    "ts": ISO8601, "char": "A", "freq": 14000000, "power": -12.3 }
  { "type": "word_space" }
  { "type": "status",  "connected": true, "freq": 14000000 }

Async IO design:
  A dedicated daemon thread handles all blocking sock.recv() calls and pushes
  raw IQ byte chunks into a thread-safe queue.Queue.  The asyncio event loop
  drains that queue using short (1-second timeout) run_in_executor calls so
  the event loop is never blocked.
"""

import asyncio
import json
import logging
import os
import queue
import socket
import struct
import threading
import time
from collections import deque
from datetime import datetime, timezone

import numpy as np
import websockets
from scipy.signal import butter, decimate, lfilter

# ── Configuration ─────────────────────────────────────────────────────────────

RTL_HOST = os.environ.get("RTL_HOST", "rtl-bridge")
RTL_PORT = int(os.environ.get("RTL_PORT", "1235"))
WS_PORT  = int(os.environ.get("WS_PORT", "8765"))

CW_FREQ_HZ      = int(os.environ.get("CW_FREQ_HZ", str(14_000_000)))
SDR_CENTER_HZ   = int(os.environ.get("SDR_CENTER_HZ", str(139_175_000)))
LO_OFFSET_HZ    = int(os.environ.get("LO_OFFSET_HZ",  str(125_000_000)))
RF_CENTER_HZ    = SDR_CENTER_HZ - LO_OFFSET_HZ        # 14.175 MHz
SDR_SAMPLE_RATE = int(os.environ.get("SAMPLE_RATE", str(2_400_000)))
GAIN            = int(os.environ.get("GAIN", "420"))   # tenths of dB

# Frequency offset of target CW frequency from SDR centre (Hz)
# Positive = target is above centre, negative = below
FREQ_OFFSET_HZ  = CW_FREQ_HZ - RF_CENTER_HZ           # e.g. -175000 for 14.000 MHz

# Decimate wideband IQ to ~24 kHz (100x) for narrow CW band
DECIMATE_FACTOR   = 100
AUDIO_SAMPLE_RATE = SDR_SAMPLE_RATE // DECIMATE_FACTOR   # 24 000 Hz

WPM           = float(os.environ.get("WPM", "20"))
DIT_SAMPLES   = int((60.0 / (50 * WPM)) * AUDIO_SAMPLE_RATE)
DAH_THRESHOLD = 2.5
CHAR_GAP_DITS = 3.0
WORD_GAP_DITS = 7.0

# LPF cutoff: ~200 Hz as fraction of Nyquist at AUDIO_SAMPLE_RATE (24 kHz)
ENVELOPE_LPF_CUTOFF = 200.0 / (AUDIO_SAMPLE_RATE / 2.0)

READ_BYTES = 65536

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

# ── RTL-TCP helpers ───────────────────────────────────────────────────────────

def rtl_command(sock: socket.socket, cmd: int, param: int) -> None:
    sock.sendall(struct.pack(">BI", cmd, param))


def connect_rtl_sync() -> socket.socket:
    """Blocking connect — runs inside the reader thread, not the event loop."""
    delay = 1.0
    while True:
        try:
            sock = socket.create_connection((RTL_HOST, RTL_PORT), timeout=10)
            header = sock.recv(12)
            if not header.startswith(b"RTL"):
                raise ValueError(f"Bad header: {header!r}")
            # Frequency set by rtl_tcp -f at startup; only override rate and gain
            rtl_command(sock, 0x02, SDR_SAMPLE_RATE)
            rtl_command(sock, 0x04, GAIN)
            rtl_command(sock, 0x03, 1)
            log.info("Connected %s:%d SDR %d ksps gain=%.1f dB",
                     RTL_HOST, RTL_PORT, SDR_SAMPLE_RATE // 1000, GAIN / 10.0)
            return sock
        except (OSError, ValueError) as e:
            log.warning("Connect failed (%s), retry in %.0fs", e, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30)


# ── Reader thread ─────────────────────────────────────────────────────────────

def iq_reader_thread(raw_q: queue.Queue, stop: threading.Event) -> None:
    """All blocking recv() calls live here, never in the asyncio event loop."""
    while not stop.is_set():
        sock = connect_rtl_sync()
        try:
            while not stop.is_set():
                data = sock.recv(READ_BYTES)
                if not data:
                    log.warning("EOF from rtl-bridge, reconnecting…")
                    break
                try:
                    raw_q.put(data, timeout=2.0)
                except queue.Full:
                    log.debug("IQ queue full — dropping chunk")
        except (OSError, ConnectionResetError) as e:
            log.warning("Read error: %s, reconnecting…", e)
        finally:
            try:
                sock.close()
            except Exception:
                pass
        if not stop.is_set():
            time.sleep(2)


# ── Signal processing ─────────────────────────────────────────────────────────

class EnvelopeDetector:
    def __init__(self) -> None:
        b, a = butter(3, ENVELOPE_LPF_CUTOFF, btype="low")
        self._b, self._a = b, a
        self._zi = np.zeros(max(len(b), len(a)) - 1)
        self._recent: deque = deque(maxlen=AUDIO_SAMPLE_RATE * 2)

    def process(self, iq: np.ndarray):
        envelope = np.abs(iq)
        filtered, self._zi = lfilter(self._b, self._a, envelope, zi=self._zi)
        self._recent.extend(filtered.tolist())
        thresh = float(np.percentile(list(self._recent), 75)) * 1.8 \
                 if len(self._recent) > 200 else 0.05
        return filtered, thresh


class MorseDecoder:
    def __init__(self) -> None:
        self._symbols: list[str] = []

    def push_tone(self, duration: int) -> None:
        if duration < DIT_SAMPLES * 0.4:
            return
        self._symbols.append("." if duration < DIT_SAMPLES * DAH_THRESHOLD else "-")

    def push_gap(self, duration: int, callback) -> None:
        gap_dits = duration / DIT_SAMPLES
        if gap_dits >= WORD_GAP_DITS:
            self._flush(callback)
            callback(None)
        elif gap_dits >= CHAR_GAP_DITS:
            self._flush(callback)

    def _flush(self, callback) -> None:
        if self._symbols:
            code = "".join(self._symbols)
            callback(MORSE_CODE.get(code, f"[{code}]"))
            self._symbols.clear()


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
        await websocket.send(json.dumps({
            "type": "status", "connected": True, "freq": CW_FREQ_HZ,
        }))
        await websocket.wait_closed()
    finally:
        CLIENTS.discard(websocket)
        log.info("WS client disconnected (total: %d)", len(CLIENTS))


# ── Decode loop ───────────────────────────────────────────────────────────────

async def decode_loop(raw_q: queue.Queue) -> None:
    loop     = asyncio.get_running_loop()
    detector = EnvelopeDetector()
    morse    = MorseDecoder()
    chars_q: asyncio.Queue = asyncio.Queue()

    tone_on           = False
    tone_start        = 0
    gap_start         = 0
    sample_clock      = 0   # counts decimated samples — used for Morse timing
    wideband_clock    = 0   # counts wideband samples — used for freq-mix phase
    # Accumulate IQ until we have a multiple of DECIMATE_FACTOR
    iq_acc = np.zeros(0, dtype=np.complex64)

    def on_char(c) -> None:
        chars_q.put_nowait(c)

    async def char_sender() -> None:
        while True:
            c = await chars_q.get()
            now = datetime.now(timezone.utc).isoformat()
            if c is None:
                await broadcast({"type": "word_space"})
            else:
                await broadcast({
                    "type": "char", "ts": now, "char": c,
                    "freq": CW_FREQ_HZ, "power": 0.0,
                })
            chars_q.task_done()

    asyncio.ensure_future(char_sender())
    log.info("Decode loop — %.0f WPM, dit=%d samples @ %d Hz, target %.4f MHz (offset %+.1f kHz)",
             WPM, DIT_SAMPLES, AUDIO_SAMPLE_RATE,
             CW_FREQ_HZ / 1e6, FREQ_OFFSET_HZ / 1e3)

    while True:
        try:
            raw = await loop.run_in_executor(None, lambda: raw_q.get(timeout=1.0))
        except queue.Empty:
            continue

        # uint8 interleaved IQ → complex64 (truncate to even length)
        u8  = np.frombuffer(raw, dtype=np.uint8)[:len(raw) & ~1].astype(np.float32)
        iq  = ((u8[0::2] - 127.5) + 1j * (u8[1::2] - 127.5)) / 127.5

        iq_acc = np.concatenate([iq_acc, iq.astype(np.complex64)])
        trim   = len(iq_acc) - (len(iq_acc) % DECIMATE_FACTOR)
        if trim == 0:
            continue
        iq_in  = iq_acc[:trim]
        iq_acc = iq_acc[trim:].copy()

        # Mix down to CW target frequency (shift by -FREQ_OFFSET_HZ)
        t = (wideband_clock + np.arange(len(iq_in))) / SDR_SAMPLE_RATE
        iq_shifted = iq_in * np.exp(-2j * np.pi * FREQ_OFFSET_HZ * t).astype(np.complex64)
        wideband_clock += len(iq_in)

        # Decimate 100x in two 10x stages (scipy decimate requires real input)
        i_dec = decimate(decimate(iq_shifted.real, 10, ftype='fir', zero_phase=True),
                         10, ftype='fir', zero_phase=True)
        q_dec = decimate(decimate(iq_shifted.imag, 10, ftype='fir', zero_phase=True),
                         10, ftype='fir', zero_phase=True)
        iq_dec = (i_dec + 1j * q_dec).astype(np.complex64)

        envelope, threshold = detector.process(iq_dec)

        for sample in envelope:
            is_tone = float(sample) > threshold
            if is_tone and not tone_on:
                if gap_start > 0:
                    morse.push_gap(sample_clock - gap_start, on_char)
                tone_on    = True
                tone_start = sample_clock
            elif not is_tone and tone_on:
                morse.push_tone(sample_clock - tone_start)
                tone_on   = False
                gap_start = sample_clock
            sample_clock += 1


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    log.info("CW decoder — WS :%d, %.4f MHz, %d ksps",
             WS_PORT, CW_FREQ_HZ / 1e6, SDR_SAMPLE_RATE // 1000)

    raw_q = queue.Queue(maxsize=128)
    stop  = threading.Event()
    threading.Thread(
        target=iq_reader_thread, args=(raw_q, stop),
        daemon=True, name="cw-iq-reader",
    ).start()

    ws_server = await websockets.serve(ws_handler, "0.0.0.0", WS_PORT)
    log.info("WebSocket on :%d", WS_PORT)

    try:
        await asyncio.gather(ws_server.serve_forever(), decode_loop(raw_q))
    finally:
        stop.set()


if __name__ == "__main__":
    asyncio.run(main())
