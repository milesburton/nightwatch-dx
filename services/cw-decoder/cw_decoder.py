"""
CW (Morse code) decoder service.

Connects to the rtl-bridge TCP multiplexer (port 1235, rtl_tcp-compatible
byte stream of uint8 IQ samples at 2.4 Msps) and decodes CW on 14.029 MHz.

Signal chain:
  uint8 IQ → complex64 → mix by -freqOffset → 10× FIR → 10× FIR
           → envelope → adaptive threshold → Morse state machine

Broadcasts JSON messages over WebSocket (aiohttp) on WS_PORT (default 8765).

Outbound message types:
  {"type": "char",       "char": "A", "freq": 14029000, "ts": "..."}
  {"type": "word_space", "ts": "..."}
  {"type": "status",     "connected": true,  "freq": 14029000}
  {"type": "status",     "connected": false, "freq": 14029000}
"""

import asyncio
import json
import logging
import os
import socket
import time
from collections import deque
from datetime import datetime, timezone

import numpy as np
from aiohttp import web

# ── Configuration ──────────────────────────────────────────────────────────────

MUX_HOST = os.environ.get("MUX_HOST", "rtl-bridge")
MUX_PORT = int(os.environ.get("MUX_PORT", "1235"))
WS_PORT  = int(os.environ.get("WS_PORT",  "8765"))

SDR_SAMPLE_RATE = 2_400_000
SDR_CENTER_HZ   = 139_175_000
LO_OFFSET_HZ    = 125_000_000
RF_CENTER_HZ    = SDR_CENTER_HZ - LO_OFFSET_HZ   # 14_175_000
CW_FREQ_HZ      = 14_029_000
FREQ_OFFSET_HZ  = CW_FREQ_HZ - RF_CENTER_HZ       # -146_000

AUDIO_RATE      = SDR_SAMPLE_RATE // 100           # 24_000 Hz
WPM             = 20
DIT_SAMPLES     = round((60 / (50 * WPM)) * AUDIO_RATE)   # 72 samples/dit

DECIMATE1       = 10
DECIMATE2       = 10
INTERMEDIATE    = SDR_SAMPLE_RATE // DECIMATE1     # 240_000 Hz

logging.basicConfig(level=logging.INFO, format="%(asctime)s [cw] %(message)s")
log = logging.getLogger(__name__)

# ── FIR filter builder ─────────────────────────────────────────────────────────

def kaiser_lowpass(cutoff: float, sample_rate: float, duration: float = 0.001, beta: float = 8.0) -> np.ndarray:
    """Builds a Kaiser-windowed low-pass FIR filter (same parameters as KaiserFIR.ts)."""
    num_taps = int(duration * sample_rate) | 1
    center   = (num_taps - 1) / 2
    norm_cut = 2.0 * cutoff / sample_rate
    n        = np.arange(num_taps)
    x        = n - center
    with np.errstate(invalid='ignore', divide='ignore'):
        sinc = np.where(x == 0, norm_cut, np.sin(np.pi * x * norm_cut) / (np.pi * x))
    window   = np.kaiser(num_taps, beta)
    taps     = sinc * window
    taps    /= taps.sum()
    return taps.astype(np.float32)

# Build FIR taps once at startup
_taps1 = kaiser_lowpass(INTERMEDIATE / 2, SDR_SAMPLE_RATE)
_taps2 = kaiser_lowpass(AUDIO_RATE  / 2, INTERMEDIATE)

# ── LO oscillator ─────────────────────────────────────────────────────────────

class LOOscillator:
    """Allocation-free complex LO; advances via angle-addition recursion."""
    def __init__(self, freq_hz: float, sample_rate: float) -> None:
        step           = 2 * np.pi * freq_hz / sample_rate
        self._step_re  = float(np.cos(step))
        self._step_im  = float(-np.sin(step))   # negative = mix-down
        self._re       = 1.0
        self._im       = 0.0
        self._norm_ctr = 0

    def generate(self, n: int) -> np.ndarray:
        """Returns n complex samples of the LO signal."""
        out = np.empty(n, dtype=np.complex64)
        re, im = self._re, self._im
        sr, si  = self._step_re, self._step_im
        for i in range(n):
            out[i] = complex(re, im)
            re, im = re * sr - im * si, re * si + im * sr
            self._norm_ctr += 1
            if self._norm_ctr >= 1000:
                mag = (re * re + im * im) ** 0.5
                re /= mag
                im /= mag
                self._norm_ctr = 0
        self._re, self._im = re, im
        return out

# ── CW signal chain ────────────────────────────────────────────────────────────

class CWSignalChain:
    def __init__(self) -> None:
        self._lo     = LOOscillator(FREQ_OFFSET_HZ, SDR_SAMPLE_RATE)
        # FIR state (zi for lfilter)
        self._zi1_re = np.zeros(len(_taps1) - 1)
        self._zi1_im = np.zeros(len(_taps1) - 1)
        self._zi2_re = np.zeros(len(_taps2) - 1)
        self._zi2_im = np.zeros(len(_taps2) - 1)
        # Envelope LPF state
        self._lpf_alpha = float(1 / (1 + 2 * np.pi * 200 / AUDIO_RATE))
        self._lpf_state = 0.0
        # Rolling window for adaptive threshold (2 seconds)
        self._window: deque[float] = deque(maxlen=AUDIO_RATE * 2)
        # Morse state machine
        self._morse    = MorseDecoder()
        self._tone_on  = False
        self._tone_start = 0
        self._gap_start  = 0
        self._clock      = 0

    def process(self, raw: bytes) -> list[dict]:
        """Process one chunk of uint8 IQ bytes. Returns list of CW events."""
        samples = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        if len(samples) & 1:
            samples = samples[:-1]
        iq = ((samples[0::2] - 127.5) + 1j * (samples[1::2] - 127.5)) / 127.5
        iq = iq.astype(np.complex64)

        # Mix down to CW frequency
        lo  = self._lo.generate(len(iq))
        mixed = iq * lo

        # First 10× decimation with FIR
        from scipy.signal import lfilter
        re1, self._zi1_re = lfilter(_taps1, 1.0, mixed.real, zi=self._zi1_re)
        im1, self._zi1_im = lfilter(_taps1, 1.0, mixed.imag, zi=self._zi1_im)
        stage1 = (re1 + 1j * im1)[DECIMATE1 - 1::DECIMATE1]

        # Second 10× decimation with FIR
        re2, self._zi2_re = lfilter(_taps2, 1.0, stage1.real, zi=self._zi2_re)
        im2, self._zi2_im = lfilter(_taps2, 1.0, stage1.imag, zi=self._zi2_im)
        audio = (re2 + 1j * im2)[DECIMATE2 - 1::DECIMATE2]

        events = []
        alpha = self._lpf_alpha

        for s in audio:
            mag = abs(s)
            self._lpf_state = alpha * mag + (1 - alpha) * self._lpf_state
            v = self._lpf_state
            self._window.append(v)

            # Adaptive threshold: midpoint between 10th and 95th percentile
            threshold = 0.05
            if len(self._window) > 200:
                arr = np.array(self._window)
                p10 = float(np.percentile(arr, 10))
                p95 = float(np.percentile(arr, 95))
                threshold = max(p10 + (p95 - p10) * 0.5, 0.01)

            is_tone = v > threshold
            if is_tone and not self._tone_on:
                if self._gap_start > 0:
                    events.extend(self._morse.push_gap(
                        self._clock - self._gap_start, DIT_SAMPLES
                    ))
                self._tone_on    = True
                self._tone_start = self._clock
            elif not is_tone and self._tone_on:
                self._morse.push_tone(self._clock - self._tone_start, DIT_SAMPLES)
                self._tone_on  = False
                self._gap_start = self._clock
            self._clock += 1

        return events

# ── Morse state machine ────────────────────────────────────────────────────────

MORSE_CODE: dict[str, str] = {
    '.-': 'A', '-...': 'B', '-.-.': 'C', '-..': 'D', '.': 'E',
    '..-.': 'F', '--.': 'G', '....': 'H', '..': 'I', '.---': 'J',
    '-.-': 'K', '.-..': 'L', '--': 'M', '-.': 'N', '---': 'O',
    '.--.': 'P', '--.-': 'Q', '.-.': 'R', '...': 'S', '-': 'T',
    '..-': 'U', '...-': 'V', '.--': 'W', '-..-': 'X', '-.--': 'Y',
    '--..': 'Z',
    '-----': '0', '.----': '1', '..---': '2', '...--': '3', '....-': '4',
    '.....': '5', '-....': '6', '--...': '7', '---..': '8', '----.': '9',
    '.-.-.-': '.', '--..--': ',', '..--..': '?', '.----.': "'",
    '-.-.--': '!', '-..-.': '/', '-.--.': '(', '-.--.-': ')',
    '.-...': '&', '---...': ':', '-.-.-.': ';', '-...-': '=',
    '.-.-.': '+', '-....-': '-', '..--.-': '_', '.-..-.': '"',
    '...-..-': '$', '.--.-.': '@',
}

DAH_THRESHOLD = 2.5
CHAR_GAP_DITS = 3.0
WORD_GAP_DITS = 7.0


class MorseDecoder:
    def __init__(self) -> None:
        self._symbols: list[str] = []

    def push_tone(self, duration: int, dit: int) -> None:
        if duration < dit * 0.4:
            return
        self._symbols.append('.' if duration < dit * DAH_THRESHOLD else '-')

    def push_gap(self, duration: int, dit: int) -> list[dict]:
        events: list[dict] = []
        dits = duration / dit
        ts   = datetime.now(timezone.utc).isoformat()
        if dits >= WORD_GAP_DITS:
            events.extend(self._flush(ts))
            events.append({'type': 'word_space', 'ts': ts})
        elif dits >= CHAR_GAP_DITS:
            events.extend(self._flush(ts))
        return events

    def _flush(self, ts: str) -> list[dict]:
        if not self._symbols:
            return []
        code  = ''.join(self._symbols)
        char  = MORSE_CODE.get(code, f'[{code}]')
        self._symbols = []
        return [{'type': 'char', 'char': char, 'freq': CW_FREQ_HZ, 'ts': ts}]

# ── WebSocket broadcast hub ────────────────────────────────────────────────────

class Hub:
    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()
        self._connected = False

    def add(self, ws: web.WebSocketResponse) -> None:
        self._clients.add(ws)

    def remove(self, ws: web.WebSocketResponse) -> None:
        self._clients.discard(ws)

    async def broadcast(self, msg: dict) -> None:
        text = json.dumps(msg)
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_str(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove(ws)

    async def set_connected(self, connected: bool) -> None:
        self._connected = connected
        await self.broadcast({'type': 'status', 'connected': connected, 'freq': CW_FREQ_HZ})

    async def send_status(self, ws: web.WebSocketResponse) -> None:
        await ws.send_str(json.dumps(
            {'type': 'status', 'connected': self._connected, 'freq': CW_FREQ_HZ}
        ))

# ── IQ reader loop ─────────────────────────────────────────────────────────────

async def iq_reader(hub: Hub) -> None:
    chain = CWSignalChain()
    while True:
        try:
            log.info("Connecting to TCP mux at %s:%d…", MUX_HOST, MUX_PORT)
            reader, writer = await asyncio.open_connection(MUX_HOST, MUX_PORT)
            # Read and discard the 12-byte RTL0 magic header
            header = await reader.readexactly(12)
            if not header.startswith(b"RTL"):
                raise ValueError(f"Unexpected header: {header!r}")
            log.info("Connected to mux. Decoding CW on %.3f MHz…", CW_FREQ_HZ / 1e6)
            await hub.set_connected(True)

            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                events = chain.process(chunk)
                for ev in events:
                    await hub.broadcast(ev)

        except Exception as e:
            log.warning("Mux connection lost: %s, retrying in 5s…", e)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            await hub.set_connected(False)
        await asyncio.sleep(5)

# ── HTTP / WebSocket server ────────────────────────────────────────────────────

async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    hub: Hub = request.app['hub']
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    hub.add(ws)
    await hub.send_status(ws)
    try:
        async for _ in ws:
            pass   # clients only receive
    finally:
        hub.remove(ws)
    return ws


async def main() -> None:
    hub = Hub()
    app = web.Application()
    app['hub'] = hub
    app.router.add_get('/ws/cw', ws_handler)

    asyncio.create_task(iq_reader(hub))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', WS_PORT)
    await site.start()
    log.info("CW decoder WebSocket listening on :%d /ws/cw", WS_PORT)
    await asyncio.Event().wait()   # run forever


if __name__ == '__main__':
    asyncio.run(main())
