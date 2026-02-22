import asyncio
import contextlib
import json
import logging
import os
import time
from collections import deque
from datetime import UTC, datetime

import numpy as np
from aiohttp import web

MUX_HOST = os.environ.get("MUX_HOST", "rtl-bridge")
MUX_PORT = int(os.environ.get("MUX_PORT", "1235"))
WS_PORT  = int(os.environ.get("WS_PORT",  "8765"))

SDR_SAMPLE_RATE = 2_400_000
SDR_CENTER_HZ   = 139_175_000
LO_OFFSET_HZ    = 125_000_000
RF_CENTER_HZ    = SDR_CENTER_HZ - LO_OFFSET_HZ
CW_FREQ_HZ      = 14_029_000
FREQ_OFFSET_HZ  = CW_FREQ_HZ - RF_CENTER_HZ

# Two-stage 100× decimation: 2.4 MHz → 24 kHz
DECIMATE1    = 10
DECIMATE2    = 10
INTERMEDIATE = SDR_SAMPLE_RATE // DECIMATE1   # 240_000
AUDIO_RATE   = INTERMEDIATE // DECIMATE2       # 24_000

WPM         = 20
DIT_SAMPLES = round((60 / (50 * WPM)) * AUDIO_RATE)

THRESHOLD_UPDATE_INTERVAL = max(DIT_SAMPLES // 8, 50)

# Bandpass half-bandwidth around the mixed-to-DC CW tone.
# After mixing CW_FREQ_HZ to DC, keep only ±BP_HZ.
# 500 Hz passes ±50 Hz operator drift and keying artefacts,
# and rejects adjacent SSB / other CW stations.
BP_HZ = 500

logging.basicConfig(level=logging.INFO, format="%(asctime)s [cw] %(message)s")
log = logging.getLogger(__name__)


def kaiser_lowpass(cutoff: float, sample_rate: float, duration: float = 0.001, beta: float = 8.0) -> np.ndarray:
    num_taps = int(duration * sample_rate) | 1
    center   = (num_taps - 1) / 2
    norm_cut = 2.0 * cutoff / sample_rate
    n        = np.arange(num_taps)
    x        = n - center
    with np.errstate(invalid='ignore', divide='ignore'):
        sinc = np.where(x == 0, norm_cut, np.sin(np.pi * x * norm_cut) / (np.pi * x))
    window = np.kaiser(num_taps, beta)
    taps   = sinc * window
    return (taps / taps.sum()).astype(np.float32)


# Stage-1 anti-aliasing LPF (before first decimation)
_taps1 = kaiser_lowpass(INTERMEDIATE / 2, SDR_SAMPLE_RATE)
# Stage-2 anti-aliasing LPF (before second decimation)
_taps2 = kaiser_lowpass(AUDIO_RATE   / 2, INTERMEDIATE)
# Narrow bandpass at audio rate: keep only ±BP_HZ around DC (the mixed CW tone).
# Longer filter (5 ms × 24 kHz = 120 taps) for steeper skirts.
_taps_bp = kaiser_lowpass(BP_HZ, AUDIO_RATE, duration=0.005, beta=8.0)


class LOOscillator:
    def __init__(self, freq_hz: float, sample_rate: float) -> None:
        step          = 2 * np.pi * freq_hz / sample_rate
        self._step_re = float(np.cos(step))
        self._step_im = float(-np.sin(step))
        self._re      = 1.0
        self._im      = 0.0
        self._norm_ctr = 0

    def generate(self, n: int) -> np.ndarray:
        out = np.empty(n, dtype=np.complex64)
        re, im = self._re, self._im
        sr, si = self._step_re, self._step_im
        for i in range(n):
            out[i] = complex(re, im)
            new_re = re * sr - im * si
            new_im = re * si + im * sr
            re, im = new_re, new_im
            self._norm_ctr += 1
            if self._norm_ctr >= 1000:
                mag = (re * re + im * im) ** 0.5
                re /= mag
                im /= mag
                self._norm_ctr = 0
        self._re, self._im = re, im
        return out


class CWSignalChain:
    _LOG_INTERVAL_SEC = 15.0   # log signal levels every 15 s for live diagnostics

    def __init__(self) -> None:
        self._lo       = LOOscillator(FREQ_OFFSET_HZ, SDR_SAMPLE_RATE)
        self._zi1_re   = np.zeros(len(_taps1)   - 1)
        self._zi1_im   = np.zeros(len(_taps1)   - 1)
        self._zi2_re   = np.zeros(len(_taps2)   - 1)
        self._zi2_im   = np.zeros(len(_taps2)   - 1)
        self._zi_bp_re = np.zeros(len(_taps_bp) - 1)
        self._zi_bp_im = np.zeros(len(_taps_bp) - 1)
        self._env_attack  = float(1 - np.exp(-1 / (0.0005 * AUDIO_RATE)))
        self._env_decay   = float(1 - np.exp(-1 / (0.0002 * AUDIO_RATE)))
        self._env_state   = 0.0
        self._window      = deque[float](maxlen=AUDIO_RATE * 3)
        self._env_max     = 0.0    # max envelope value since last log (signal level check)
        self._threshold   = 0.05
        self._hyst_frac   = 0.10
        self._threshold_ctr = 0
        self._last_log_ts = time.monotonic()
        self._morse       = MorseDecoder()
        self._tone_on     = False
        self._tone_start  = 0
        self._gap_start   = 0
        self._clock       = 0

    def _compute_threshold_from_window(self) -> float:
        arr = np.array(self._window)
        p10 = float(np.percentile(arr, 10))
        p90 = float(np.percentile(arr, 90))
        now = time.monotonic()
        if now - self._last_log_ts >= self._LOG_INTERVAL_SEC:
            self._last_log_ts = now
            snr = p90 / p10 if p10 > 1e-9 else 0
            log.info(
                "signal: p10=%.4f p90=%.4f peak=%.4f snr=%.1fx | thr=%.4f (high=%.4f)",
                p10, p90, self._env_max, snr, self._threshold,
                self._threshold * (1 + self._hyst_frac),
            )
            self._env_max = 0.0
        if p10 < 1e-9:
            return self._threshold
        # Midpoint between noise floor (p10) and signal peak (p90).
        # No SNR guard: the narrow bandpass has already rejected off-channel
        # interference, so this ratio reflects real keying excursions.
        return p10 + (p90 - p10) * 0.5

    def _update_threshold(self) -> None:
        if len(self._window) >= 20:
            self._threshold = self._compute_threshold_from_window()

    def _schmitt_thresholds(self) -> tuple[float, float]:
        hyst = self._threshold * self._hyst_frac
        return self._threshold + hyst, max(self._threshold - hyst, 0.001)

    def _apply_envelope(self, mags: np.ndarray) -> np.ndarray:
        attack = self._env_attack
        decay  = self._env_decay
        env    = np.empty_like(mags)
        state  = self._env_state
        for i, m in enumerate(mags):
            alpha  = attack if m > state else decay
            state += alpha * (m - state)
            env[i] = state
        self._env_state = float(state)
        return env

    def process(self, raw: bytes) -> list[dict]:
        from scipy.signal import lfilter

        samples = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        if len(samples) & 1:
            samples = samples[:-1]
        iq    = ((samples[0::2] - 127.5) + 1j * (samples[1::2] - 127.5)) / 127.5
        mixed = iq.astype(np.complex64) * self._lo.generate(len(iq))

        # Stage 1: anti-alias LPF + decimate ×10
        re1, self._zi1_re = lfilter(_taps1, 1.0, mixed.real, zi=self._zi1_re)
        im1, self._zi1_im = lfilter(_taps1, 1.0, mixed.imag, zi=self._zi1_im)
        stage1 = (re1 + 1j * im1)[DECIMATE1 - 1::DECIMATE1]

        # Stage 2: anti-alias LPF + decimate ×10 → 24 kHz
        re2, self._zi2_re = lfilter(_taps2, 1.0, stage1.real, zi=self._zi2_re)
        im2, self._zi2_im = lfilter(_taps2, 1.0, stage1.imag, zi=self._zi2_im)
        audio = (re2 + 1j * im2)[DECIMATE2 - 1::DECIMATE2]

        # Stage 3: narrow bandpass ±BP_HZ — isolates target CW tone, rejects
        # adjacent SSB / other CW stations that passed the wide anti-alias LPF.
        re3, self._zi_bp_re = lfilter(_taps_bp, 1.0, audio.real, zi=self._zi_bp_re)
        im3, self._zi_bp_im = lfilter(_taps_bp, 1.0, audio.imag, zi=self._zi_bp_im)
        narrowband = re3 + 1j * im3

        env = self._apply_envelope(np.abs(narrowband).astype(np.float64))
        self._window.extend(env[::4].tolist())
        peak = float(env.max()) if len(env) else 0.0
        if peak > self._env_max:
            self._env_max = peak

        return self._detect_tones(env)

    def _detect_tones(self, env: np.ndarray) -> list[dict]:
        events: list[dict] = []
        high_thr, low_thr = self._schmitt_thresholds()

        for v in env:
            self._threshold_ctr += 1
            if self._threshold_ctr >= THRESHOLD_UPDATE_INTERVAL:
                self._threshold_ctr = 0
                self._update_threshold()
                high_thr, low_thr = self._schmitt_thresholds()

            if not self._tone_on and v > high_thr:
                if self._gap_start > 0:
                    events.extend(self._morse.push_gap(self._clock - self._gap_start))
                self._tone_on    = True
                self._tone_start = self._clock
                log.debug("tone ON  env=%.4f thr=%.4f", v, high_thr)
            elif self._tone_on and v < low_thr:
                dur_ms = (self._clock - self._tone_start) * 1000 // AUDIO_RATE
                self._morse.push_tone(self._clock - self._tone_start)
                self._tone_on   = False
                self._gap_start = self._clock
                log.debug("tone OFF dur=%dms env=%.4f thr=%.4f", dur_ms, v, low_thr)
            self._clock += 1

        return events

    def flush(self) -> list[dict]:
        if self._tone_on:
            self._morse.push_tone(self._clock - self._tone_start)
            self._tone_on   = False
            self._gap_start = self._clock
        if self._gap_start > 0:
            return self._morse.push_gap(DIT_SAMPLES * 7)
        return []


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

DAH_THRESHOLD = 2.0
CHAR_GAP_DITS = 2.0
WORD_GAP_DITS = 4.5


class MorseDecoder:
    _DIT_ALPHA = 0.15
    _DIT_MIN   = int(AUDIO_RATE * 0.030)
    _DIT_MAX   = int(AUDIO_RATE * 0.480)

    def __init__(self) -> None:
        self._symbols: list[str] = []
        self._dit_est = float(DIT_SAMPLES)

    @property
    def dit(self) -> int:
        return max(self._DIT_MIN, min(self._DIT_MAX, int(self._dit_est)))

    def push_tone(self, duration: int) -> None:
        dit = self.dit
        if duration < dit * 0.4:
            return
        is_dit = duration < dit * DAH_THRESHOLD
        self._symbols.append('.' if is_dit else '-')
        observed = duration if is_dit else duration / 3.0
        self._dit_est += self._DIT_ALPHA * (observed - self._dit_est)

    def push_gap(self, duration: int) -> list[dict]:
        dits = duration / self.dit
        ts   = datetime.now(UTC).isoformat()
        if dits >= WORD_GAP_DITS:
            return [*self._flush(ts), {'type': 'word_space', 'ts': ts}]
        if dits >= CHAR_GAP_DITS:
            return self._flush(ts)
        return []

    def _flush(self, ts: str) -> list[dict]:
        if not self._symbols:
            return []
        code          = ''.join(self._symbols)
        char          = MORSE_CODE.get(code, f'[{code}]')
        self._symbols = []
        wpm = int(round(60 / (50 * (self._dit_est / AUDIO_RATE)))) if self._dit_est > 0 else 0
        log.info("decoded %r from %r  dit_est=%.0f samp (%d WPM)", char, code, self._dit_est, wpm)
        return [{'type': 'char', 'char': char, 'freq': CW_FREQ_HZ, 'ts': ts}]


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
        dead = [ws for ws in list(self._clients) if not await self._try_send(ws, text)]
        for ws in dead:
            self.remove(ws)

    @staticmethod
    async def _try_send(ws: web.WebSocketResponse, text: str) -> bool:
        try:
            await ws.send_str(text)
            return True
        except Exception:
            return False

    async def set_connected(self, connected: bool) -> None:
        self._connected = connected
        await self.broadcast({'type': 'status', 'connected': connected, 'freq': CW_FREQ_HZ})

    async def send_status(self, ws: web.WebSocketResponse) -> None:
        await ws.send_str(json.dumps(
            {'type': 'status', 'connected': self._connected, 'freq': CW_FREQ_HZ}
        ))


async def _drain_tcp(
    reader: asyncio.StreamReader,
    queue: "asyncio.Queue[bytes | None]",
) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            try:
                queue.put_nowait(chunk)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                queue.put_nowait(chunk)
    finally:
        await queue.put(None)


async def iq_reader(hub: Hub) -> None:
    loop = asyncio.get_running_loop()

    while True:
        chain  = CWSignalChain()
        writer = None
        try:
            log.info("connecting to %s:%d…", MUX_HOST, MUX_PORT)
            reader, writer = await asyncio.open_connection(MUX_HOST, MUX_PORT)
            header = await reader.readexactly(12)
            if not header.startswith(b"RTL"):
                raise ValueError(f"unexpected header: {header!r}")
            log.info("connected — decoding CW on %.3f MHz (±%d Hz bandpass)", CW_FREQ_HZ / 1e6, BP_HZ)
            await hub.set_connected(True)

            queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=4)
            drain_task = asyncio.create_task(_drain_tcp(reader, queue))

            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                events = await loop.run_in_executor(None, chain.process, chunk)
                for ev in events:
                    await hub.broadcast(ev)

            drain_task.cancel()

        except Exception as e:
            log.warning("mux connection lost: %s, retrying in 5s…", e)
        finally:
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()
                    await writer.wait_closed()
            await hub.set_connected(False)
        await asyncio.sleep(5)


_LOG_LEVEL_MAP = {'log': log.info, 'info': log.info, 'warn': log.warning, 'error': log.error}


def _log_browser_entries(entries: object) -> None:
    if not isinstance(entries, list):
        entries = [entries]
    for entry in entries:
        if isinstance(entry, dict):
            _LOG_LEVEL_MAP.get(str(entry.get('level', 'log')), log.info)(
                '[%s] %s', entry.get('source', 'browser'), entry.get('message', '')
            )


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    hub: Hub = request.app['hub']
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    hub.add(ws)
    await hub.send_status(ws)
    _last_log: dict[str, float] = {}
    LOG_THROTTLE_SEC = 1.0
    try:
        async for msg in ws:
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
                        if level in ('log', 'info'):
                            key = f'{source}:{text[:40]}'
                            if now - _last_log.get(key, 0) < LOG_THROTTLE_SEC:
                                continue
                            _last_log[key] = now
                        _LOG_LEVEL_MAP.get(level, log.info)('[%s] %s', source, text)
                except Exception:
                    pass
    finally:
        hub.remove(ws)
    return ws


async def log_ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async for msg in ws:
        if msg.type == web.WSMsgType.TEXT:
            with contextlib.suppress(Exception):
                _log_browser_entries(json.loads(msg.data))
    return ws


async def supervised_iq_reader(hub: Hub) -> None:
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
    log.info("CW decoder WebSocket on :%d /ws/cw", WS_PORT)
    await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(main())
