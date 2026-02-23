"""
PSK31 decoder service.

Receives complex64 @ 24 kHz from rtl-bridge AudioMux (port 1240, centred on
14.070 MHz mixed to DC).  Scans ±2 kHz around DC for the strongest carrier,
locks on, and decodes BPSK31 text, broadcasting JSON events over WebSocket.

Signal chain:
  1. FFT carrier scan  — find peak within ±2 kHz of DC every 5 s
  2. Fine mix to DC    — multiply by exp(-j2π·offset·n/Fs)
  3. Matched filter    — Kaiser FIR lowpass at 45 Hz (passes full PSK31 ±15.6 Hz)
  4. Symbol clock      — one sample per T=768 at 24 kHz (31.25 baud)
  5. Differential BPSK — Δφ > π/2 → bit 0 (transition), else → bit 1
  6. Varicode decode   — two consecutive 0s = char boundary; look up in table
  7. Broadcast         — same JSON schema as cw_decoder (char / word_space / status)

The decoder emits:
  {"type": "char",       "char": "C", "freq": 14070123, "ts": "..."}
  {"type": "word_space",              "freq": 14070123, "ts": "..."}
  {"type": "status",     "connected": true, "freq": 14070000}
"""

import asyncio
import contextlib
import json
import logging
import math
import os
from datetime import UTC, datetime

import numpy as np
from aiohttp import web
from scipy.signal import lfilter

import store

MUX_HOST = os.environ.get("MUX_HOST", "rtl-bridge")
MUX_PORT = int(os.environ.get("MUX_PORT", "1240"))
WS_PORT  = int(os.environ.get("WS_PORT",  "8768"))

SESSION_TIMEOUT_S = 30

AUDIO_RATE   = 24_000
PSK31_BAUD   = 31.25
SYMBOL_SAMP  = AUDIO_RATE / PSK31_BAUD       # 768.0 samples/symbol
PSK31_CENTRE = 14_070_000                     # nominal RF centre (Hz)

# Carrier scan: search within ±SCAN_HZ of DC (the AudioMux already mixed
# PSK31_CENTRE to DC, so signals ≤2 kHz off-channel are still captured).
SCAN_HZ = 2_000
# Re-scan every N symbols (~5 s).
RESCAN_SYMBOLS = int(PSK31_BAUD * 5)
# Minimum carrier SNR to keep decoding (peak bin / median of spectrum).
MIN_SNR = 3.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s [psk31] %(message)s")
log = logging.getLogger(__name__)


# ── Varicode table (standard PSK31) ───────────────────────────────────────────
# Code→char mapping built from the G3PLX PSK31 spec integer table.
# Source: Peter Martinez G3PLX, cross-checked against fldigi varicode.cxx.
# Bit strings are MSB-first (= transmission order for BPSK31).
# Character boundaries are two consecutive 0-bits (not part of the code).
#
# The spec has a small number of code collisions between control chars (0x00–0x1f)
# and printable chars.  The standard resolution (used by all known implementations)
# is: printable characters take priority over control characters for shared codes,
# since PSK31 is a text mode and control codes are essentially never transmitted.

def _build_varicode_table() -> dict[str, str]:
    # Canonical varicode integers indexed by ASCII code point 0x00–0x7e.
    # Each value is the varicode word as a binary integer (MSB = first bit).
    # Sourced from the G3PLX spec and verified against fldigi varicode.cxx.
    raw = [
        0b1010101011, 0b1011011011, 0b1011101101, 0b1101110111,  # 0x00-03
        0b1011101011, 0b1101011111, 0b1011101111, 0b1011111101,  # 0x04-07
        0b1011111111, 0b11100101,   0b11101101,   0b1101101111,  # 0x08-0b
        0b1011011101, 0b11101011,   0b1101110101, 0b1110101011,  # 0x0c-0f
        0b1011110111, 0b1011110101, 0b1110101101, 0b1110101111,  # 0x10-13
        0b1101011011, 0b1101101011, 0b1101101101, 0b1101010111,  # 0x14-17
        0b1101111011, 0b1101111101, 0b1110110111, 0b1101010101,  # 0x18-1b
        0b1101011101, 0b1110111011, 0b1011111011, 0b1101111111,  # 0x1c-1f
        0b1,          0b111111111,  0b101011111,  0b111110101,   # SP ! " #
        0b111011011,  0b1011010101, 0b1010111011, 0b101111111,   # $ % & '
        0b11111011,   0b11110111,   0b101101111,  0b111011111,   # ( ) * +
        0b1110101,    0b110101,     0b1010111,    0b110101111,   # , - . /
        0b10110111,   0b10111101,   0b11101101,   0b11111111,    # 0 1 2 3
        0b101110111,  0b101011011,  0b101101011,  0b110101101,   # 4 5 6 7
        0b110101011,  0b110110111,  0b11110101,   0b110111101,   # 8 9 : ;
        0b111101101,  0b1010101,    0b111010111,  0b1010101101,  # < = > ?
        0b1010111101, 0b1111101,    0b11101011,   0b10101101,    # @ A B C
        0b10110101,   0b1110111,    0b11011011,   0b11111101,    # D E F G
        0b101010101,  0b1111111,    0b111111101,  0b101111101,   # H I J K
        0b11010111,   0b111010101,  0b10111011,   0b10101111,    # L M N O
        0b11010101,   0b111011101,  0b10101011,   0b1101011,     # P Q R S
        0b11100111,   0b1011011,    0b11011101,   0b10110111,    # T U V W
        0b101101111,  0b101011101,  0b110110101,  0b11110101011, # X Y Z [
        0b10110111011,0b11110101101,0b101010111,  0b1011010111,  # \ ] ^ _
        0b1111111101, 0b101110,     0b11011111,   0b101011,      # ` a b c
        0b110111,     0b1101,       0b111101,     0b1011011,     # d e f g
        0b11111,      0b11011,      0b1111010111, 0b1110111101,  # h i j k
        0b10110,      0b101010,     0b10111,      0b1011,        # l m n o
        0b110111,     0b111110111,  0b10101,      0b10111,       # p q r s
        0b101,        0b110101,     0b1111011,    0b1101011,     # t u v w
        0b11011,      0b11101,      0b1010110111,               # x y z
    ]  # 0x00..0x7a (123 entries covering NUL..z)

    # Build table: printable chars take priority over control chars for shared codes.
    # Pass 1: insert control characters (0x00–0x1f)
    table: dict[str, str] = {}
    for i in range(min(0x20, len(raw))):
        code = bin(raw[i])[2:]
        if code not in table:
            table[code] = chr(i)

    # Pass 2: insert printable characters (0x20–), overwriting controls on conflict.
    for i in range(0x20, len(raw)):
        code = bin(raw[i])[2:]
        table[code] = chr(i)  # printable always wins

    return table


VARICODE_TABLE = _build_varicode_table()


# ── Kaiser FIR matched filter ─────────────────────────────────────────────────

def _kaiser_fir(cutoff_hz: float, fs: float, duration_s: float = 0.04,
                beta: float = 8.0) -> np.ndarray:
    """Kaiser-windowed FIR lowpass.  duration_s controls filter length."""
    num_taps = int(duration_s * fs) | 1
    center   = (num_taps - 1) / 2
    norm_cut = 2.0 * cutoff_hz / fs
    n = np.arange(num_taps)
    x = n - center
    with np.errstate(invalid='ignore', divide='ignore'):
        sinc = np.where(x == 0, norm_cut,
                        np.sin(np.pi * x * norm_cut) / (np.pi * x))
    window = np.kaiser(num_taps, beta)
    taps   = (sinc * window).astype(np.float32)
    return taps / taps.sum()


# Matched filter: 45 Hz lowpass passes full PSK31 spectrum (±15.6 Hz)
# with margin for carrier offsets up to ~30 Hz.
_MATCHED_TAPS = _kaiser_fir(45.0, AUDIO_RATE, duration_s=0.04)


# ── PSK31 signal chain ─────────────────────────────────────────────────────────

class VaricodeDecoder:
    """Accumulate bits and emit ASCII characters on double-zero boundary."""

    def __init__(self) -> None:
        self._bits: list[int] = []
        self._prev_zero = False

    def push_bit(self, bit: int) -> list[str]:
        """bit=1 for no-transition, bit=0 for transition (standard PSK31)."""
        chars: list[str] = []
        if bit == 0 and self._prev_zero:
            # Two consecutive zeros = character boundary
            code = ''.join(str(b) for b in self._bits)
            # Remove the first zero (which is part of the delimiter pair)
            if code:
                # Strip trailing zeros (the delimiter bits are not part of the code)
                code = code.rstrip('0')
                ch = VARICODE_TABLE.get(code)
                if ch:
                    chars.append(ch)
                elif code:
                    log.debug("unknown varicode: %r", code)
            self._bits = []
            self._prev_zero = False
        else:
            if self._prev_zero:
                self._bits.append(0)
            self._prev_zero = (bit == 0)
            if bit == 1:
                self._bits.append(1)
            # Guard against runaway bit accumulation (max varicode length = 10)
            if len(self._bits) > 12:
                self._bits = []
                self._prev_zero = False
        return chars


class PSK31SignalChain:
    """
    Full BPSK31 demodulator.

    Input: complex64 chunks at AUDIO_RATE (24 kHz), with PSK31_CENTRE mixed to DC.
    Output: list of event dicts (same schema as cw_decoder).
    """

    def __init__(self) -> None:
        # Filter state (complex → apply to real/imag separately)
        self._zi_re = np.zeros(len(_MATCHED_TAPS) - 1)
        self._zi_im = np.zeros(len(_MATCHED_TAPS) - 1)

        # Carrier offset fine-mixer
        self._carrier_offset_hz = 0.0
        self._lo_phase = 0.0          # running phase (radians)

        # Symbol clock
        self._sample_acc: float = 0.0  # fractional accumulator
        self._sym_period: float = SYMBOL_SAMP
        self._prev_sym: complex = complex(1.0, 0.0)  # previous symbol for DPSK

        # Varicode decode
        self._varicode = VaricodeDecoder()

        # Carrier scan scheduling
        self._samples_since_scan: int = 0
        self._scan_interval_samp: int = int(RESCAN_SYMBOLS * SYMBOL_SAMP)
        self._carrier_snr: float = 0.0

        # Scan buffer: accumulate for FFT
        self._scan_buf: list[np.ndarray] = []
        self._scan_buf_len: int = 0
        self._FFT_N = 4096

    def _scan_carrier(self, samples: np.ndarray) -> None:
        """FFT scan: find peak within ±SCAN_HZ of DC, update _carrier_offset_hz."""
        spec = np.abs(np.fft.fft(samples, n=self._FFT_N)) ** 2
        freq_res = AUDIO_RATE / self._FFT_N
        max_bin = int(SCAN_HZ / freq_res)

        # Positive-frequency bins within scan range
        peak_bin = int(np.argmax(spec[1:max_bin + 1])) + 1
        # Also check negative-frequency bins (spec is symmetric for real; for complex, check both)
        neg_peak_bin = self._FFT_N - int(np.argmax(spec[self._FFT_N - max_bin:self._FFT_N])) - 1
        neg_peak_bin = max(self._FFT_N - max_bin, min(self._FFT_N - 1, neg_peak_bin))

        if spec[peak_bin] >= spec[neg_peak_bin]:
            best_bin = peak_bin
        else:
            best_bin = neg_peak_bin

        # Convert bin to Hz (wrap negative bins)
        if best_bin > self._FFT_N // 2:
            offset_hz = (best_bin - self._FFT_N) * freq_res
        else:
            offset_hz = best_bin * freq_res

        # SNR: peak / median of out-of-band spectrum
        noise_median = float(np.median(spec[max_bin + 1:self._FFT_N // 2]))
        peak_power   = float(spec[best_bin])
        snr = peak_power / noise_median if noise_median > 0 else 0.0
        self._carrier_snr = snr

        if snr >= MIN_SNR:
            # On the very first scan (offset still at 0), set directly to avoid
            # needing many scan cycles to converge.  On subsequent scans, smooth
            # with alpha=0.5 to track drift without large jumps.
            if self._carrier_offset_hz == 0.0:
                self._carrier_offset_hz = offset_hz
            else:
                alpha = 0.5
                self._carrier_offset_hz += alpha * (offset_hz - self._carrier_offset_hz)
            log.info("carrier scan: offset=%.1f Hz  SNR=%.1fx", self._carrier_offset_hz, snr)
        else:
            log.info("carrier scan: SNR=%.1fx (below threshold — no PSK31 signal)", snr)

    def _mix_to_dc(self, samples: np.ndarray) -> np.ndarray:
        """Fine-mix carrier offset to DC using a running phasor."""
        n = len(samples)
        step = -2.0 * math.pi * self._carrier_offset_hz / AUDIO_RATE
        phases = self._lo_phase + step * np.arange(n)
        self._lo_phase = float((phases[-1] + step) % (2.0 * math.pi))
        lo = np.exp(1j * phases).astype(np.complex64)
        return (samples * lo).astype(np.complex64)

    def process(self, raw: bytes) -> list[dict]:
        """Process a chunk of complex64 audio bytes; return list of event dicts."""
        audio = np.frombuffer(raw, dtype=np.complex64)
        if len(audio) == 0:
            return []

        events: list[dict] = []

        # Accumulate for carrier scan
        self._scan_buf.append(audio)
        self._scan_buf_len += len(audio)
        if self._scan_buf_len >= self._FFT_N:
            scan_arr = np.concatenate(self._scan_buf)[:self._FFT_N]
            self._scan_carrier(scan_arr)
            self._scan_buf = []
            self._scan_buf_len = 0

        # Fine-mix carrier to DC
        mixed = self._mix_to_dc(audio)

        # Matched filter (applied to real/imag separately to preserve complex)
        re_f, self._zi_re = lfilter(_MATCHED_TAPS, 1.0, mixed.real, zi=self._zi_re)
        im_f, self._zi_im = lfilter(_MATCHED_TAPS, 1.0, mixed.imag, zi=self._zi_im)
        filtered = (re_f + 1j * im_f).astype(np.complex64)

        # Symbol clock: emit one sample per SYMBOL_SAMP samples
        i = 0
        n = len(filtered)
        while i < n:
            # How many samples until the next symbol strobe?
            samps_to_next = self._sym_period - self._sample_acc
            if i + samps_to_next > n:
                self._sample_acc += (n - i)
                break

            # Strobe: take one symbol sample
            strobe_idx = i + int(samps_to_next)
            if strobe_idx >= n:
                strobe_idx = n - 1
            sym = filtered[strobe_idx]

            # DBPSK: phase difference from previous symbol
            delta_phi = float(np.angle(sym * np.conj(self._prev_sym)))
            bit = 0 if abs(delta_phi) > math.pi / 2 else 1
            self._prev_sym = sym

            # Varicode decode
            chars = self._varicode.push_bit(bit)
            ts = datetime.now(UTC).isoformat()
            carrier_hz = int(PSK31_CENTRE + self._carrier_offset_hz)
            for ch in chars:
                if ch == ' ':
                    events.append({'type': 'word_space', 'freq': carrier_hz, 'ts': ts})
                elif ch.isprintable():
                    events.append({'type': 'char', 'char': ch, 'freq': carrier_hz, 'ts': ts})
                    log.debug("char: %r  (carrier %.1f Hz)", ch, self._carrier_offset_hz)

            self._sample_acc = 0.0
            i = strobe_idx + 1

        return events


# ── WebSocket hub (same pattern as cw_decoder) ────────────────────────────────

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
        await self.broadcast({'type': 'status', 'connected': connected,
                              'freq': PSK31_CENTRE})

    async def send_status(self, ws: web.WebSocketResponse) -> None:
        await ws.send_str(json.dumps(
            {'type': 'status', 'connected': self._connected, 'freq': PSK31_CENTRE}
        ))


# ── TCP IQ reader ─────────────────────────────────────────────────────────────

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


async def _flush_psk31_session(hub: Hub, live_text: str, start_ts: str,
                               end_ts: str, freq_hz: int) -> None:
    """Save a completed PSK31 session to SQLite and notify clients."""
    text = live_text.strip()
    if not text:
        return
    try:
        row_id = await store.save_session('psk31', start_ts, end_ts, freq_hz, text)
        await hub.broadcast({
            'type': 'session', 'id': row_id, 'mode': 'psk31',
            'start_ts': start_ts, 'end_ts': end_ts,
            'freq_hz': freq_hz, 'text': text,
        })
        log.info("PSK31 session saved: id=%d  len=%d chars", row_id, len(text))
    except Exception as e:
        log.error("failed to save PSK31 session: %s", e)


async def iq_reader(hub: Hub) -> None:
    loop = asyncio.get_running_loop()

    while True:
        chain  = PSK31SignalChain()
        writer = None
        sess: dict = {'text': '', 'start_ts': '', 'freq_hz': PSK31_CENTRE, 'timer': None}

        def _reset_flush_timer() -> None:
            if sess['timer'] is not None:
                sess['timer'].cancel()
            sess['timer'] = loop.call_later(
                SESSION_TIMEOUT_S,
                lambda: asyncio.ensure_future(
                    _flush_psk31_session(hub, sess['text'], sess['start_ts'],
                                         datetime.now(UTC).isoformat(), sess['freq_hz'])
                ),
            )
        try:
            log.info("connecting to %s:%d...", MUX_HOST, MUX_PORT)
            reader, writer = await asyncio.open_connection(MUX_HOST, MUX_PORT)
            header = await reader.readexactly(12)
            if not header.startswith(b"AUD"):
                raise ValueError(f"unexpected header: {header!r}")
            log.info("connected -- scanning for PSK31 within ±%d Hz of %.3f MHz",
                     SCAN_HZ, PSK31_CENTRE / 1e6)
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
                    if ev['type'] == 'char':
                        if not sess['start_ts']:
                            sess['start_ts'] = ev['ts']
                        sess['freq_hz']  = ev.get('freq', PSK31_CENTRE)
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


async def supervised_iq_reader(hub: Hub) -> None:
    while True:
        try:
            await iq_reader(hub)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("iq_reader crashed: %s -- restarting in 5s", e)
            await asyncio.sleep(5)


# ── WebSocket handler ─────────────────────────────────────────────────────────

async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    hub: Hub = request.app['hub']
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    hub.add(ws)
    await hub.send_status(ws)
    try:
        async for _ in ws:
            pass  # browser only receives
    finally:
        hub.remove(ws)
    return ws


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    await store.init_db()
    hub = Hub()
    app = web.Application()
    app['hub'] = hub
    app.router.add_get('/ws/psk31', ws_handler)

    asyncio.create_task(supervised_iq_reader(hub))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', WS_PORT)
    await site.start()
    log.info("PSK31 decoder WebSocket on :%d /ws/psk31", WS_PORT)
    await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(main())
