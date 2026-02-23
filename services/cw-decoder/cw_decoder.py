import asyncio
import contextlib
import json
import logging
import os
import time
from collections import deque
from datetime import UTC, datetime

import psutil

import numpy as np
from aiohttp import web

import store

MUX_HOST = os.environ.get("MUX_HOST", "rtl-bridge")
MUX_PORT = int(os.environ.get("MUX_PORT", "1237"))
WS_PORT  = int(os.environ.get("WS_PORT",  "8765"))

SESSION_TIMEOUT_S = 30

# Audio stream constants -- received pre-decimated at AUDIO_RATE from rtl-bridge AudioMux
AUDIO_RATE  = 24_000
CW_FREQ_HZ  = 14_029_000

WPM         = 12   # starting estimate; adapts in real-time. 20m CW typically 5-25 WPM
DIT_SAMPLES = round((60 / (50 * WPM)) * AUDIO_RATE)

# Update threshold every ~500 ms.  Initial fast-adapt period uses a shorter
# interval (see CWSignalChain.__init__ for _threshold_update_interval).
THRESHOLD_UPDATE_INTERVAL = AUDIO_RATE // 2   # 12 000 samples = 500 ms
THRESHOLD_FAST_INTERVAL   = DIT_SAMPLES * 2   # ~2 dits — fast adapt at startup

# Narrow bandpass half-bandwidth around DC (the CW tone has already been mixed
# to DC by rtl-bridge's AudioDecimator). Keep only +/-BP_HZ.
# 150 Hz is narrow enough to pass a single CW station while rejecting adjacent
# signals. A station within ±150 Hz of 14.029 MHz is solidly in the bandpass.
# The original 500 Hz was designed for the pre-decimated IQ path and is far
# too wide here — adjacent CW and SSB sidebands corrupt the Schmitt trigger.
BP_HZ = 150

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


# Narrow bandpass at audio rate: keep only +/-BP_HZ around DC (the mixed CW tone).
# Longer filter (5 ms x 24 kHz = 120 taps) for steeper skirts.
_taps_bp = kaiser_lowpass(BP_HZ, AUDIO_RATE, duration=0.005, beta=8.0)


class CWSignalChain:
    _LOG_INTERVAL_SEC = 15.0   # log signal levels every 15 s for live diagnostics

    def __init__(self) -> None:
        self._zi_bp_re = np.zeros(len(_taps_bp) - 1)
        self._zi_bp_im = np.zeros(len(_taps_bp) - 1)
        self._env_attack  = float(1 - np.exp(-1 / (0.0005 * AUDIO_RATE)))
        self._env_decay   = float(1 - np.exp(-1 / (0.0002 * AUDIO_RATE)))
        self._env_state   = 0.0
        self._window      = deque[float](maxlen=AUDIO_RATE * 3)
        self._env_max     = 0.0    # max envelope value since last log
        self._threshold   = 0.05
        self._hyst_frac   = 0.10
        self._threshold_ctr = 0
        # Fast adapt for the first ~3 seconds, then switch to slow update
        self._fast_adapt_remaining = AUDIO_RATE * 3
        self._last_log_ts = time.monotonic()
        self._morse       = MorseDecoder()
        self._tone_on     = False
        self._tone_start  = 0
        self._gap_start   = 0
        self._clock       = 0

    def _compute_threshold_from_window(self) -> float:
        arr = np.fromiter(self._window, dtype=np.float32, count=len(self._window))
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
        return p10 + (p90 - p10) * 0.5

    def _current_update_interval(self) -> int:
        if self._fast_adapt_remaining > 0:
            return THRESHOLD_FAST_INTERVAL
        return THRESHOLD_UPDATE_INTERVAL

    def _update_threshold(self) -> None:
        if len(self._window) >= 20:
            self._threshold = self._compute_threshold_from_window()

    def _schmitt_thresholds(self) -> tuple[float, float]:
        hyst = self._threshold * self._hyst_frac
        return self._threshold + hyst, max(self._threshold - hyst, 0.001)

    def _apply_envelope(self, mags: np.ndarray) -> np.ndarray:
        from scipy.signal import lfilter

        attack = self._env_attack
        decay  = self._env_decay

        # Fast asymmetric envelope via two single-pole IIR passes:
        #   attack path: y_a[n] = α_a·x[n] + (1-α_a)·y_a[n-1]
        #   decay  path: y_d[n] = α_d·x[n] + (1-α_d)·y_d[n-1]
        # Envelope = element-wise max of both paths, which approximates the
        # behaviour of the original per-sample branch but runs fully in C.
        zi_a = np.array([self._env_state * (1 - attack)])
        zi_d = np.array([self._env_state * (1 - decay)])
        env_a, zi_a_out = lfilter([attack], [1.0, -(1.0 - attack)], mags, zi=zi_a)
        env_d, zi_d_out = lfilter([decay],  [1.0, -(1.0 - decay)],  mags, zi=zi_d)
        env = np.maximum(env_a, env_d)
        # Advance the state using whichever path was higher at the last sample
        self._env_state = float(max(float(env_a[-1]), float(env_d[-1])) if len(env) else self._env_state)
        return env

    def process(self, raw: bytes) -> list[dict]:
        """Process a chunk of complex64 audio bytes (pre-decimated to AUDIO_RATE by rtl-bridge)."""
        from scipy.signal import lfilter

        audio = np.frombuffer(raw, dtype=np.complex64)

        # Narrow bandpass +/-BP_HZ around DC (CW tone already mixed to DC by rtl-bridge)
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
        """Vectorised Schmitt-trigger edge detector.

        Computes a thresholded state array in numpy, then only iterates over
        the (rare) edge transitions in Python — O(edges) instead of O(samples).
        """
        events: list[dict] = []
        n = len(env)
        if n == 0:
            return events

        high_thr, low_thr = self._schmitt_thresholds()

        # Build a boolean "tone active" array using hysteresis:
        # Start from last known state, apply high/low thresholds sample-by-sample
        # via numpy ops — still O(n) but in C, not Python.
        above_high = env > high_thr
        below_low  = env < low_thr

        # Reconstruct the Schmitt output using a prefix-scan approach:
        # At each sample the state can only change if it crosses the relevant threshold.
        # We find all crossing candidates and resolve them left-to-right in a small loop.
        # Crossing indices are rare (one per dit/dah/gap), so the loop is short.
        changes = np.where(above_high | below_low)[0]

        state   = self._tone_on
        clock   = self._clock
        chunk_start = clock  # absolute sample index of env[0]

        for idx in changes:
            v = float(env[idx])
            abs_idx = chunk_start + idx

            # Advance threshold counter to this position
            advance = abs_idx - clock
            self._threshold_ctr += advance
            self._fast_adapt_remaining = max(0, self._fast_adapt_remaining - advance)
            clock = abs_idx
            interval = self._current_update_interval()
            if self._threshold_ctr >= interval:
                self._threshold_ctr %= interval
                self._update_threshold()
                high_thr, low_thr = self._schmitt_thresholds()
                # Re-check thresholds may have changed; recompute crossing arrays
                above_high = env > high_thr
                below_low  = env < low_thr

            if not state and above_high[idx]:
                if self._gap_start > 0:
                    events.extend(self._morse.push_gap(clock - self._gap_start))
                state            = True
                self._tone_start = clock
                log.debug("tone ON  env=%.4f thr=%.4f", v, high_thr)
            elif state and below_low[idx]:
                dur_ms = (clock - self._tone_start) * 1000 // AUDIO_RATE
                self._morse.push_tone(clock - self._tone_start)
                state            = False
                self._gap_start  = clock
                log.debug("tone OFF dur=%dms env=%.4f thr=%.4f", dur_ms, v, low_thr)

        # Advance clock and threshold counter past the end of this chunk
        remaining = (chunk_start + n) - clock
        self._threshold_ctr += remaining
        self._fast_adapt_remaining = max(0, self._fast_adapt_remaining - remaining)
        interval = self._current_update_interval()
        if self._threshold_ctr >= interval:
            self._threshold_ctr %= interval
            self._update_threshold()

        self._clock    = chunk_start + n
        self._tone_on  = state
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

DAH_THRESHOLD = 2.5   # dah = duration > 2.5× dit (ITU standard: dah = 3× dit)
CHAR_GAP_DITS = 2.5   # inter-character gap > 2.5× dit triggers char decode
WORD_GAP_DITS = 5.0   # inter-word gap > 5× dit triggers word space


class MorseDecoder:
    # Asymmetric adaptation: snap quickly toward faster senders (smaller dit),
    # but resist inflation toward slower values caused by false dah readings
    # (e.g. digital modes with brief dips mis-read as CW gaps).
    _DIT_ALPHA_DOWN = 0.20   # observed shorter than estimate → speed up fast
    _DIT_ALPHA_UP   = 0.05   # observed longer than estimate  → slow drift
    _DIT_MIN   = int(AUDIO_RATE * 0.030)
    _DIT_MAX   = int(AUDIO_RATE * 0.480)
    # CW characters have at most 7 elements (e.g. '-------' doesn't exist, but
    # longest real chars like '...-..-' = 7). If we accumulate more than this
    # without a gap, the signal isn't CW — discard the run.
    _MAX_SYMBOLS = 7

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
        # If we've accumulated an impossibly long sequence, it's not CW — reset.
        if len(self._symbols) > self._MAX_SYMBOLS:
            self._symbols = []
            return
        observed = duration if is_dit else duration / 3.0
        alpha = self._DIT_ALPHA_DOWN if observed < self._dit_est else self._DIT_ALPHA_UP
        self._dit_est += alpha * (observed - self._dit_est)

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
        return [{'type': 'char', 'char': char, 'freq': CW_FREQ_HZ, 'ts': ts, 'wpm': wpm}]


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


async def _flush_cw_session(hub: Hub, live_text: str, start_ts: str,
                            end_ts: str, freq_hz: int) -> None:
    """Save a completed CW session to SQLite and notify clients."""
    text = live_text.strip()
    if not text:
        return
    try:
        row_id = await store.save_session('cw', start_ts, end_ts, freq_hz, text)
        await hub.broadcast({
            'type': 'session', 'id': row_id, 'mode': 'cw',
            'start_ts': start_ts, 'end_ts': end_ts,
            'freq_hz': freq_hz, 'text': text,
        })
        log.info("CW session saved: id=%d  len=%d chars", row_id, len(text))
    except Exception as e:
        log.error("failed to save CW session: %s", e)


async def iq_reader(hub: Hub) -> None:
    loop = asyncio.get_running_loop()

    while True:
        chain  = CWSignalChain()
        writer = None
        # Session accumulator (30 s inactivity → flush).
        # Stored as a mutable dict so nested callbacks can mutate without nonlocal.
        sess: dict = {'text': '', 'start_ts': '', 'freq_hz': CW_FREQ_HZ, 'timer': None}

        def _reset_flush_timer() -> None:
            if sess['timer'] is not None:
                sess['timer'].cancel()

            def _fire() -> None:
                text     = sess['text']
                start_ts = sess['start_ts']
                freq_hz  = sess['freq_hz']
                # Clear immediately so a double-fire or reconnect doesn't re-save
                sess['text']     = ''
                sess['start_ts'] = ''
                sess['timer']    = None
                asyncio.ensure_future(
                    _flush_cw_session(hub, text, start_ts,
                                      datetime.now(UTC).isoformat(), freq_hz)
                )

            sess['timer'] = loop.call_later(SESSION_TIMEOUT_S, _fire)

        try:
            log.info("connecting to %s:%d...", MUX_HOST, MUX_PORT)
            reader, writer = await asyncio.open_connection(MUX_HOST, MUX_PORT)
            header = await reader.readexactly(12)
            if not header.startswith(b"AUD"):
                raise ValueError(f"unexpected header: {header!r}")
            log.info("connected -- decoding CW on %.3f MHz (+/-%d Hz bandpass)", CW_FREQ_HZ / 1e6, BP_HZ)
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
                    # Accumulate text for server-side session persistence
                    if ev['type'] == 'char':
                        if not sess['start_ts']:
                            sess['start_ts'] = ev['ts']
                        sess['freq_hz']  = ev.get('freq', CW_FREQ_HZ)
                        sess['text']    += ev['char']
                        _reset_flush_timer()
                    elif ev['type'] == 'word_space':
                        if sess['text']:
                            sess['text'] += ' '
                            _reset_flush_timer()

            drain_task.cancel()

        except Exception as e:
            log.warning("mux connection lost: %s, retrying in 5s...", e)
        finally:
            if sess['timer'] is not None:
                sess['timer'].cancel()
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


_status_clients: set[web.WebSocketResponse] = set()


def _host_stats() -> dict:
    cpu   = psutil.cpu_percent(interval=None)
    load  = psutil.getloadavg()
    mem   = psutil.virtual_memory()
    disk  = psutil.disk_usage('/')
    return {
        'type':        'status',
        'cpu_pct':     round(cpu, 1),
        'load_1':      round(load[0], 2),
        'load_5':      round(load[1], 2),
        'load_15':     round(load[2], 2),
        'mem_used_mb': round(mem.used / 1024 / 1024),
        'mem_total_mb': round(mem.total / 1024 / 1024),
        'mem_pct':     round(mem.percent, 1),
        'disk_used_gb': round(disk.used / 1024 ** 3, 1),
        'disk_total_gb': round(disk.total / 1024 ** 3, 1),
        'disk_pct':    round(disk.percent, 1),
        'ts':          datetime.now(UTC).isoformat(),
    }


async def status_ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    _status_clients.add(ws)
    # Send immediately on connect
    with contextlib.suppress(Exception):
        await ws.send_str(json.dumps(_host_stats()))
    try:
        async for _ in ws:
            pass  # clients don't send; just wait for disconnect
    finally:
        _status_clients.discard(ws)
    return ws


async def _status_broadcaster() -> None:
    """Push host stats to all /ws/status clients every 5 seconds."""
    # Warm up psutil cpu_percent (first call always returns 0.0)
    psutil.cpu_percent(interval=None)
    await asyncio.sleep(1)
    while True:
        if _status_clients:
            payload = json.dumps(_host_stats())
            dead = []
            for ws in list(_status_clients):
                try:
                    await ws.send_str(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                _status_clients.discard(ws)
        await asyncio.sleep(5)


async def supervised_iq_reader(hub: Hub) -> None:
    while True:
        try:
            await iq_reader(hub)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("iq_reader crashed: %s -- restarting in 5s", e)
            await asyncio.sleep(5)


async def main() -> None:
    await store.init_db()
    hub = Hub()
    app = web.Application()
    app['hub'] = hub
    app.router.add_get('/ws/cw', ws_handler)
    app.router.add_get('/ws/logs', log_ws_handler)
    app.router.add_get('/ws/status', status_ws_handler)

    asyncio.create_task(supervised_iq_reader(hub))
    asyncio.create_task(_status_broadcaster())

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', WS_PORT)
    await site.start()
    log.info("CW decoder WebSocket on :%d /ws/cw", WS_PORT)
    await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(main())
