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
import time
from collections import deque
from datetime import UTC, datetime

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
DIT_SAMPLES     = round((60 / (50 * WPM)) * AUDIO_RATE)   # 1440 samples/dit

# How often to recompute the adaptive threshold (every N audio samples)
THRESHOLD_UPDATE_INTERVAL = max(DIT_SAMPLES // 8, 50)   # every 1/8 dit ≈ 7.5 ms

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
        # Envelope smoother: power-law smoothing, τ ≈ 3 ms (much shorter than a dit)
        _tau_sec      = 0.003
        self._env_alpha = float(1 - np.exp(-1 / (_tau_sec * AUDIO_RATE)))
        self._env_state = 0.0
        # Rolling window for adaptive threshold (3 seconds of audio power)
        self._window: deque[float] = deque(maxlen=AUDIO_RATE * 3)
        # Threshold is recomputed periodically (not every sample) to avoid chattering
        self._threshold       = 0.05
        self._threshold_ctr   = 0
        # SNR gate: latch open when signal seen, hold for GATE_HOLD_SAMPLES after last detection
        # Gate starts open so the first few seconds of data are always decoded
        self._signal_present  = True
        self._gate_hold_ctr   = 0        # counts down samples since last high-SNR observation
        # Schmitt-trigger hysteresis: 20% of threshold range
        self._hyst_frac       = 0.20
        # Morse state machine
        self._morse    = MorseDecoder()
        self._tone_on  = False
        self._tone_start = 0
        self._gap_start  = 0
        self._clock      = 0

    # Minimum ratio of p90/p5 required to consider the signal worth decoding.
    # Pure Gaussian noise after FIR decimation has p90/p5 ≈ 1.5–2.
    # A real HF CW carrier via NooElec upconverter at 20 dB gain reaches ~2.5–4.
    MIN_SNR_RATIO = 2.5
    # How long (in audio samples) to keep the gate open after last high-SNR detection.
    # 10 seconds at 24 kHz = 240 000 samples → covers inter-character and word gaps.
    GATE_HOLD_SAMPLES = AUDIO_RATE * 10

    # Log SNR diagnostics once per minute (every N threshold updates)
    _LOG_INTERVAL = int(60 * AUDIO_RATE / max(THRESHOLD_UPDATE_INTERVAL, 1))

    def _update_threshold(self) -> None:
        """Recompute adaptive threshold from recent envelope history."""
        if len(self._window) < 20:
            return
        arr = np.array(self._window)
        p5  = float(np.percentile(arr, 5))
        p90 = float(np.percentile(arr, 90))
        spread = p90 - p5

        # Periodic diagnostic so we can tune SNR parameters
        self._log_ctr = getattr(self, '_log_ctr', 0) + 1
        if self._log_ctr >= self._LOG_INTERVAL:
            self._log_ctr = 0
            snr_diag = p90 / max(p5, 1e-9)
            log.info(
                "SNR diag: p5=%.4f p90=%.4f ratio=%.2f gate=%s thr=%.4f",
                p5, p90, snr_diag, "OPEN" if self._signal_present else "CLOSED",
                self._threshold,
            )

        if spread < 0.01 or p5 < 1e-9:
            # Countdown the hold timer; gate stays open while hold > 0
            if self._gate_hold_ctr > 0:
                self._gate_hold_ctr -= THRESHOLD_UPDATE_INTERVAL
            else:
                self._signal_present = False
            return
        snr = p90 / max(p5, 1e-9)
        if snr >= self.MIN_SNR_RATIO:
            # Signal detected — open gate and reset hold timer
            self._signal_present = True
            self._gate_hold_ctr  = self.GATE_HOLD_SAMPLES
            self._threshold = p5 + spread * 0.5
        else:
            # Below SNR — count down hold timer
            if self._gate_hold_ctr > 0:
                self._gate_hold_ctr -= THRESHOLD_UPDATE_INTERVAL
            else:
                self._signal_present = False

    def process(self, raw: bytes) -> list[dict]:
        """Process one chunk of uint8 IQ bytes. Returns list of CW events."""
        from scipy.signal import lfilter

        samples = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        if len(samples) & 1:
            samples = samples[:-1]
        iq = ((samples[0::2] - 127.5) + 1j * (samples[1::2] - 127.5)) / 127.5
        iq = iq.astype(np.complex64)

        # Mix down to CW frequency
        lo    = self._lo.generate(len(iq))
        mixed = iq * lo

        # First 10× decimation with FIR
        re1, self._zi1_re = lfilter(_taps1, 1.0, mixed.real, zi=self._zi1_re)
        im1, self._zi1_im = lfilter(_taps1, 1.0, mixed.imag, zi=self._zi1_im)
        stage1 = (re1 + 1j * im1)[DECIMATE1 - 1::DECIMATE1]

        # Second 10× decimation with FIR
        re2, self._zi2_re = lfilter(_taps2, 1.0, stage1.real, zi=self._zi2_re)
        im2, self._zi2_im = lfilter(_taps2, 1.0, stage1.imag, zi=self._zi2_im)
        audio = (re2 + 1j * im2)[DECIMATE2 - 1::DECIMATE2]

        # Vectorised envelope: single-pole IIR on |audio|
        mags = np.abs(audio).astype(np.float64)
        alpha = self._env_alpha
        env   = np.empty_like(mags)
        state = self._env_state
        for i, m in enumerate(mags):
            state += alpha * (m - state)
            env[i] = state
        self._env_state = float(state)

        # Extend window with downsampled envelope (every 4 samples) for efficiency
        self._window.extend(env[::4].tolist())

        events: list[dict] = []
        thr = self._threshold
        hyst = thr * self._hyst_frac
        high_thr = thr + hyst
        low_thr  = max(thr - hyst, 0.001)

        for v in env:
            # Recompute threshold periodically
            self._threshold_ctr += 1
            if self._threshold_ctr >= THRESHOLD_UPDATE_INTERVAL:
                self._threshold_ctr = 0
                self._update_threshold()
                thr   = self._threshold
                hyst  = thr * self._hyst_frac
                high_thr = thr + hyst
                low_thr  = max(thr - hyst, 0.001)

            # Schmitt trigger with hysteresis — only when SNR gate is open
            if self._signal_present:
                if not self._tone_on and v > high_thr:
                    if self._gap_start > 0:
                        events.extend(self._morse.push_gap(
                            self._clock - self._gap_start, DIT_SAMPLES
                        ))
                    self._tone_on    = True
                    self._tone_start = self._clock
                elif self._tone_on and v < low_thr:
                    self._morse.push_tone(self._clock - self._tone_start, DIT_SAMPLES)
                    self._tone_on  = False
                    self._gap_start = self._clock
            elif self._tone_on:
                # Signal dropped below SNR gate while tone was active — reset state
                self._tone_on   = False
                self._gap_start = 0
                self._morse     = MorseDecoder()   # discard partial character
            self._clock += 1

        return events

    def flush(self) -> list[dict]:
        """Flush any pending tone/gap that hasn't been closed by a transition.

        Call this at end-of-stream (or periodically during silence) to emit
        the last character and word-space events.
        """
        events: list[dict] = []
        if self._tone_on:
            # Tone was still on when data ended — close it
            self._morse.push_tone(self._clock - self._tone_start, DIT_SAMPLES)
            self._tone_on   = False
            self._gap_start = self._clock
        if self._gap_start > 0:
            # Emit the pending gap as a word-space to flush the last character
            events.extend(self._morse.push_gap(DIT_SAMPLES * 7, DIT_SAMPLES))
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
CHAR_GAP_DITS = 2.5   # 3-dit gaps measure as ~2.95 dits due to envelope latency
WORD_GAP_DITS = 5.5   # 7-dit gaps measure as ~6.0 dits; 1-dit intra-char ~1.0


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
        ts   = datetime.now(UTC).isoformat()
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

_level_map = {'log': log.info, 'info': log.info, 'warn': log.warning, 'error': log.error}


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    hub: Hub = request.app['hub']
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    hub.add(ws)
    await hub.send_status(ws)
    # Rate-limit browser log processing: only emit once per LOG_THROTTLE_SEC per source
    _last_log: dict[str, float] = {}
    LOG_THROTTLE_SEC = 1.0
    try:
        async for msg in ws:
            # Accept inbound log entries from the browser on this same socket.
            if msg.type == web.WSMsgType.TEXT:
                try:
                    entries = json.loads(msg.data)
                    if not isinstance(entries, list):
                        entries = [entries]
                    now = time.monotonic()
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        level  = str(entry.get('level', 'log'))
                        source = str(entry.get('source', 'browser'))
                        text   = str(entry.get('message', ''))
                        # Only ship warn/error at full rate; throttle info/log
                        if level in ('log', 'info'):
                            key = f'{source}:{text[:40]}'
                            if now - _last_log.get(key, 0) < LOG_THROTTLE_SEC:
                                continue
                            _last_log[key] = now
                        _level_map.get(level, log.info)('[%s] %s', source, text)
                except Exception:
                    pass
    finally:
        hub.remove(ws)
    return ws


async def log_ws_handler(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint for browser log shipping.
    Clients send JSON arrays of log entries; we emit them via Python logger.
    Using WebSocket avoids issues with HTTP POST interception on some networks.
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    level_map = {'log': log.info, 'info': log.info, 'warn': log.warning, 'error': log.error}
    async for msg in ws:
        if msg.type == web.WSMsgType.TEXT:
            try:
                entries = json.loads(msg.data)
            except Exception:
                continue
            if not isinstance(entries, list):
                entries = [entries]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                level  = str(entry.get('level', 'log'))
                source = str(entry.get('source', 'browser'))
                text   = str(entry.get('message', ''))
                emit   = level_map.get(level, log.info)
                emit('[%s] %s', source, text)
    return ws


async def supervised_iq_reader(hub: Hub) -> None:
    """Wraps iq_reader so it always restarts if it crashes unexpectedly."""
    while True:
        try:
            await iq_reader(hub)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("iq_reader crashed: %s — restarting in 5s", e)
            await asyncio.sleep(5)


async def main() -> None:
    hub = Hub()
    app = web.Application()
    app['hub'] = hub
    app.router.add_get('/ws/cw', ws_handler)
    app.router.add_get('/ws/logs', log_ws_handler)

    asyncio.create_task(supervised_iq_reader(hub))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', WS_PORT)
    await site.start()
    log.info("CW decoder WebSocket listening on :%d /ws/cw", WS_PORT)
    await asyncio.Event().wait()   # run forever


if __name__ == '__main__':
    asyncio.run(main())
