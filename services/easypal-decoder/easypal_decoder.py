"""
EasyPal decoder service.

Connects to the rtl-bridge TCP multiplexer (port 1235) and decodes EasyPal
(DRM Mode B, SO_0) digital image transmissions on 14.233 MHz.

Signal chain:
  uint8 IQ → complex64 → mix by +58kHz → 10× FIR decimate → 10× FIR decimate
           → FM discriminator → resample to 12 kHz → DC block
           → guard-interval sync → 256-pt FFT OFDM demod
           → pilot-based channel equalisation → 16-QAM demapping
           → time/frequency deinterleave → rate-1/2 Viterbi FEC decode
           → CRC-8 FAC parse → CRC-16 MSC reassemble → JPEG → PNG → WebSocket

DRM physical layer (Mode B, SO_0):
  FFT size: 256,  Guard: 64,  Symbol: 320 samples at 12 kHz
  Carrier spacing: 46.875 Hz,  Centre: 1500 Hz
  Active carriers: k = -10 … +18  (29 carriers)
  MSC: 16-QAM, FAC: QPSK, SDC: QPSK

Broadcasts JSON messages over WebSocket (aiohttp) on WS_PORT (default 8767).

Outbound message types:
  {"type": "frame", "imageDataUrl": "data:image/png;base64,...",
   "ts": "2025-01-01T12:00:00Z"}
  {"type": "status", "connected": true}
  {"type": "status", "connected": false}
"""

import asyncio
import base64
import io
import json
import logging
import os
from datetime import UTC, datetime

import numpy as np
from aiohttp import web
from PIL import Image

# ── Configuration ──────────────────────────────────────────────────────────────

MUX_HOST = os.environ.get("MUX_HOST", "rtl-bridge")
MUX_PORT = int(os.environ.get("MUX_PORT", "1235"))
WS_PORT  = int(os.environ.get("WS_PORT",  "8767"))

SDR_SAMPLE_RATE   = 2_400_000
SDR_CENTER_HZ     = 139_175_000
LO_OFFSET_HZ      = 125_000_000
RF_CENTER_HZ      = SDR_CENTER_HZ - LO_OFFSET_HZ    # 14_175_000
EASYPAL_FREQ_HZ   = 14_233_000
EASYPAL_OFFSET_HZ = EASYPAL_FREQ_HZ - RF_CENTER_HZ  # +58_000

# Two-stage decimation: 2_400_000 → 240_000 → 24_000
DECIMATE1    = 10
DECIMATE2    = 10
INTERMEDIATE = SDR_SAMPLE_RATE // DECIMATE1   # 240_000
AUDIO_RATE   = INTERMEDIATE // DECIMATE2       # 24_000

# DRM Mode B physical layer constants
DRM_RATE      = 12_000    # internal DRM sample rate
FFT_SIZE      = 256
GUARD_SIZE    = 64
SYMBOL_SIZE   = FFT_SIZE + GUARD_SIZE          # 320 samples at 12 kHz
FRAME_SYMBOLS = 15
FRAME_SAMPLES = FRAME_SYMBOLS * SYMBOL_SIZE    # 4800 samples
CARRIER_SPACING = DRM_RATE / FFT_SIZE          # 46.875 Hz
DRM_CENTER_BIN = round(1500 / CARRIER_SPACING) # bin 32 = 1500 Hz
K_MIN = -10
K_MAX = 18
N_CARRIERS = K_MAX - K_MIN + 1                # 29 active carriers

# Pilot carriers (time pilots) — known BPSK reference cells
TIME_PILOT_CARRIERS = [-9, -3, 4, 8, 12]
# Reference BPSK values for time pilots (all +1 in DRM Mode B frame 0)
TIME_PILOT_REFS = {k: 1.0 + 0j for k in TIME_PILOT_CARRIERS}

# MSC carries 16-QAM: N_CARRIERS minus pilot carriers per symbol
# = 29 - 5 pilot positions = 24 data carriers per symbol
# Each frame has 15 symbols, so 15 × 24 = 360 QAM cells
# After removing FAC/SDC overhead (~8 cells), ~352 MSC cells/frame
MSC_CELLS_PER_FRAME = 352

logging.basicConfig(level=logging.INFO, format="%(asctime)s [easypal] %(message)s")
log = logging.getLogger(__name__)

# ── FIR filter builder ─────────────────────────────────────────────────────────

def kaiser_lowpass(cutoff: float, sample_rate: float, duration: float = 0.001, beta: float = 8.0) -> np.ndarray:
    num_taps = int(duration * sample_rate) | 1
    center   = (num_taps - 1) / 2
    norm_cut = 2.0 * cutoff / sample_rate
    x        = np.arange(num_taps) - center
    with np.errstate(invalid='ignore', divide='ignore'):
        sinc = np.where(x == 0, norm_cut, np.sin(np.pi * x * norm_cut) / (np.pi * x))
    taps = sinc * np.kaiser(num_taps, beta)
    return (taps / taps.sum()).astype(np.float32)


_taps1 = kaiser_lowpass(INTERMEDIATE / 2, SDR_SAMPLE_RATE)
_taps2 = kaiser_lowpass(AUDIO_RATE   / 2, INTERMEDIATE)

# ── LO oscillator ─────────────────────────────────────────────────────────────

class LOOscillator:
    """Phase-continuous complex LO for frequency downconversion."""

    def __init__(self, freq_hz: float, sample_rate: float) -> None:
        self._step  = 2 * np.pi * freq_hz / sample_rate
        self._phase = 0.0

    def generate(self, n: int) -> np.ndarray:
        phases      = self._phase + self._step * np.arange(n)
        self._phase = float(phases[-1] + self._step) % (2 * np.pi)
        return np.exp(-1j * phases).astype(np.complex64)

# ── IQ → audio signal chain ────────────────────────────────────────────────────

class IQSignalChain:
    """Decimates 2.4 MHz IQ to 24 kHz FM-demodulated audio."""

    def __init__(self) -> None:
        self._lo      = LOOscillator(EASYPAL_OFFSET_HZ, SDR_SAMPLE_RATE)
        self._zi1_re  = np.zeros(len(_taps1) - 1)
        self._zi1_im  = np.zeros(len(_taps1) - 1)
        self._zi2_re  = np.zeros(len(_taps2) - 1)
        self._zi2_im  = np.zeros(len(_taps2) - 1)
        self._prev_re = 0.0
        self._prev_im = 0.0

    def process(self, raw: bytes) -> np.ndarray:
        """Returns FM-demodulated audio at 24 kHz (instantaneous freq in Hz)."""
        from scipy.signal import lfilter

        samples = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        if len(samples) & 1:
            samples = samples[:-1]
        iq = ((samples[0::2] - 127.5) + 1j * (samples[1::2] - 127.5)) / 127.5
        iq = iq.astype(np.complex64)

        lo    = self._lo.generate(len(iq))
        mixed = iq * lo

        re1, self._zi1_re = lfilter(_taps1, 1.0, mixed.real, zi=self._zi1_re)
        im1, self._zi1_im = lfilter(_taps1, 1.0, mixed.imag, zi=self._zi1_im)
        stage1 = (re1 + 1j * im1)[DECIMATE1 - 1::DECIMATE1]

        re2, self._zi2_re = lfilter(_taps2, 1.0, stage1.real, zi=self._zi2_re)
        im2, self._zi2_im = lfilter(_taps2, 1.0, stage1.imag, zi=self._zi2_im)
        audio = (re2 + 1j * im2)[DECIMATE2 - 1::DECIMATE2]

        # FM discriminator
        i_arr, q_arr = audio.real, audio.imag
        prev_i       = np.empty(len(audio))
        prev_q       = np.empty(len(audio))
        prev_i[0]    = self._prev_re
        prev_q[0]    = self._prev_im
        prev_i[1:]   = i_arr[:-1]
        prev_q[1:]   = q_arr[:-1]
        if len(i_arr):
            self._prev_re = float(i_arr[-1])
            self._prev_im = float(q_arr[-1])

        cross = q_arr * prev_i - i_arr * prev_q
        dot   = i_arr * prev_i + q_arr * prev_q
        return (np.arctan2(cross, dot) * AUDIO_RATE / (2 * np.pi)).astype(np.float32)

# ── Audio → baseband complex signal ───────────────────────────────────────────

class AudioToBaseband:
    """
    Converts FM-demodulated audio (instantaneous frequency in Hz) to a complex
    baseband signal centred at 0 Hz for OFDM demodulation.

    Steps:
    1. Resample from AUDIO_RATE to DRM_RATE (24 kHz → 12 kHz, factor 1/2).
    2. Mix by -1500 Hz to shift DRM centre tone to DC.
    3. Apply DC blocking IIR.
    4. Re-modulate: audio frequency deviation → complex carrier
       (integrate instantaneous frequency to get phase, then exp(j·phase))
    """

    def __init__(self) -> None:
        self._lo       = LOOscillator(1500.0, DRM_RATE)
        self._dc_state = 0.0
        self._buf: list[float] = []
        # Resample 24k→12k: keep every 2nd sample (already low-pass filtered)
        self._decimate = AUDIO_RATE // DRM_RATE   # = 2
        self._phase    = 0.0

    def process(self, audio: np.ndarray) -> np.ndarray:
        """Returns complex baseband samples at DRM_RATE."""
        # Decimate (audio was already low-pass filtered to AUDIO_RATE/2 = 12 kHz)
        decimated = audio[self._decimate - 1::self._decimate].astype(np.float64)
        n = len(decimated)
        if n == 0:
            return np.empty(0, dtype=np.complex64)

        # Convert instantaneous frequency (Hz) → phase (rad) by integration
        # phase[i] = phase_prev + 2π * freq[i] / DRM_RATE
        # This reconstructs the original analytic signal before FM discrimination.
        phases      = np.cumsum(2 * np.pi * decimated / DRM_RATE)
        phases     += self._phase
        self._phase  = float(phases[-1])

        # Re-create complex analytic signal
        sig = np.exp(1j * phases).astype(np.complex64)

        # Mix down to centre EasyPal at DC
        lo  = self._lo.generate(n)
        sig = sig * lo

        # DC block (leaky integrator)
        out = np.empty(n, dtype=np.complex64)
        dc  = self._dc_state
        alpha = 0.9999
        for i in range(n):
            dc        = alpha * dc + (1 - alpha) * sig[i].real
            out[i]    = complex(sig[i].real - dc, sig[i].imag)
        self._dc_state = float(dc)

        return out

# ── OFDM demodulator ───────────────────────────────────────────────────────────

class OFDMDemodulator:
    """
    Extracts complex QAM cells from a DRM Mode B baseband stream.

    Guard-interval correlation detects symbol boundaries.  Each symbol is
    256-point FFT'd, then active carriers k = K_MIN … K_MAX are extracted.
    Time pilots are used for channel estimation (per-carrier amplitude/phase).
    """

    def __init__(self) -> None:
        self._buf      = np.empty(0, dtype=np.complex64)
        self._synced   = False
        self._sym_off  = 0            # current symbol start within _buf

    # ── Guard-interval (GI) correlation ───────────────────────────────────────

    def _find_symbol_start(self, data: np.ndarray) -> int | None:
        """
        Slide a window looking for the peak GI correlation:
        corr[i] = |sum(data[i:i+GUARD_SIZE] · conj(data[i+FFT_SIZE:i+FFT_SIZE+GUARD_SIZE]))|
        """
        need = SYMBOL_SIZE + GUARD_SIZE
        if len(data) < need:
            return None
        best_pos, best_val = 0, -1.0
        search = min(len(data) - need, SYMBOL_SIZE)
        for i in range(search):
            a   = data[i: i + GUARD_SIZE]
            b   = data[i + FFT_SIZE: i + FFT_SIZE + GUARD_SIZE]
            val = float(np.abs(np.dot(a, b.conj())))
            if val > best_val:
                best_val, best_pos = val, i
        return best_pos

    # ── Channel estimation from time pilots ───────────────────────────────────

    @staticmethod
    def _estimate_channel(bins: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """
        Returns a per-carrier complex channel estimate H[k] for k = K_MIN…K_MAX.
        Time pilots (known BPSK = +1) give H at 5 positions; interpolate linearly.
        """
        pilot_k    = np.array(TIME_PILOT_CARRIERS)
        pilot_vals = np.array([TIME_PILOT_REFS[k] for k in pilot_k])
        # Convert relative carrier index to absolute FFT bin
        pilot_bins = pilot_k + DRM_CENTER_BIN

        # Measured channel at pilot positions
        h_measured = bins[pilot_bins] / pilot_vals.astype(np.complex64)

        # Interpolate across active carrier range
        all_k = np.arange(K_MIN, K_MAX + 1)

        h_re = np.interp(all_k, pilot_k, h_measured.real)
        h_im = np.interp(all_k, pilot_k, h_measured.imag)
        h_interp = (h_re + 1j * h_im).astype(np.complex64)

        # Replace zero/near-zero estimates with 1 to avoid divide-by-zero
        mag = np.abs(h_interp)
        h_interp = np.where(mag > 0.01, h_interp, np.complex64(1.0))

        # Suppress pilot carriers in output (they carry no data)
        pilot_positions = (pilot_k - K_MIN).tolist()
        mask = np.ones(N_CARRIERS, dtype=bool)
        for p in pilot_positions:
            mask[p] = False

        return h_interp, mask

    # ── Process one FFT symbol ─────────────────────────────────────────────────

    def _demod_symbol(self, symbol_samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """
        FFT the guard-stripped useful part, extract active carriers, equalise.
        Returns (cells, data_mask) where cells[i] are complex QAM values and
        data_mask[i] is True for data carriers (False for pilots).
        """
        useful   = symbol_samples[GUARD_SIZE:]          # strip guard
        spectrum = np.fft.fft(useful)                   # 256-pt FFT

        # Extract active carrier bins (DRM_CENTER_BIN + k for k in K_MIN..K_MAX)
        active_bins = np.arange(K_MIN, K_MAX + 1) + DRM_CENTER_BIN
        cells       = spectrum[active_bins].astype(np.complex64)

        # Channel equalisation
        h_est, data_mask = self._estimate_channel(spectrum)
        cells_eq = cells / h_est

        return cells_eq, data_mask

    # ── Public interface ───────────────────────────────────────────────────────

    def push(self, baseband: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
        """
        Feed complex baseband samples.  Returns list of (cells, data_mask) tuples,
        one per complete OFDM symbol found.
        """
        self._buf = np.concatenate([self._buf, baseband])
        symbols: list[tuple[np.ndarray, np.ndarray]] = []

        while True:
            if not self._synced:
                # Need at least 2 symbols to find GI correlation
                if len(self._buf) < 2 * SYMBOL_SIZE:
                    break
                off = self._find_symbol_start(self._buf)
                if off is None:
                    break
                # Align buffer to symbol start
                self._buf    = self._buf[off:]
                self._synced = True
                self._sym_off = 0
                log.debug("OFDM synced at buffer offset %d", off)

            if len(self._buf) < SYMBOL_SIZE:
                break

            sym    = self._buf[:SYMBOL_SIZE]
            cells, mask = self._demod_symbol(sym)
            symbols.append((cells, mask))
            self._buf = self._buf[SYMBOL_SIZE:]

            # Re-sync periodically using correlation (every 15 symbols = 1 frame)
            self._sym_off += 1
            if self._sym_off >= FRAME_SYMBOLS:
                self._sym_off = 0
                self._synced  = False   # re-verify sync at frame boundary

        return symbols

# ── 16-QAM demapper ───────────────────────────────────────────────────────────

# Gray-coded 16-QAM constellation: (I, Q) → 4 bits
# DRM uses Gray-coded 16-QAM with unit average energy
_QAM16_LEVELS = np.array([-3.0, -1.0, 1.0, 3.0]) / np.sqrt(10.0)
_QAM16_BITS   = np.array([0, 0, 0, 1, 1, 1, 1, 0], dtype=np.uint8)  # Gray 2-bit mapping
# Two bits per axis: I=b0b1, Q=b2b3
_GRAY2_TABLE  = np.array([0b00, 0b01, 0b11, 0b10], dtype=np.uint8)   # Gray(0..3)


def qam16_demap(cells: np.ndarray) -> np.ndarray:
    """Hard-decision 16-QAM demapper. Returns flat uint8 bit array (4 bits/cell)."""
    bits = np.empty(len(cells) * 4, dtype=np.uint8)
    levels = _QAM16_LEVELS
    for idx, c in enumerate(cells):
        # Find nearest I and Q levels
        i_idx = int(np.argmin(np.abs(c.real - levels)))
        q_idx = int(np.argmin(np.abs(c.imag - levels)))
        gray_i = _GRAY2_TABLE[i_idx]
        gray_q = _GRAY2_TABLE[q_idx]
        # b0 b1 = I bits (MSB first), b2 b3 = Q bits
        bits[idx * 4 + 0] = (gray_i >> 1) & 1
        bits[idx * 4 + 1] =  gray_i       & 1
        bits[idx * 4 + 2] = (gray_q >> 1) & 1
        bits[idx * 4 + 3] =  gray_q       & 1
    return bits

# ── Deinterleaver ─────────────────────────────────────────────────────────────

def deinterleave_frame(cells_per_frame: list[np.ndarray]) -> np.ndarray:
    """
    Undo DRM frequency + time interleaving.

    Frequency interleaving: bit-reversal permutation within each symbol.
    Time interleaving:      1-frame depth — rows = cells, columns = symbols
                            output is row-major read of column-major write.
    """
    n_sym  = len(cells_per_frame)
    n_cell = len(cells_per_frame[0]) if cells_per_frame else 0
    if n_sym == 0 or n_cell == 0:
        return np.empty(0, dtype=np.complex64)

    # Frequency deinterleave: bit-reversal of cell indices within each symbol
    bits_needed = int(np.ceil(np.log2(n_cell))) if n_cell > 1 else 1
    freq_perm = np.array(
        [int(f'{i:0{bits_needed}b}'[::-1], 2) % n_cell for i in range(n_cell)],
        dtype=int,
    )

    matrix = np.zeros((n_cell, n_sym), dtype=np.complex64)
    for sym_i, cells in enumerate(cells_per_frame):
        if len(cells) == n_cell:
            matrix[:, sym_i] = cells[freq_perm]

    # Time deinterleave: read out row-major (cells vary fastest)
    return matrix.flatten(order='F')   # column-major → effectively transposes

# ── Viterbi FEC decoder ───────────────────────────────────────────────────────

# Rate-1/6 convolutional code, K=7, octal generators 133/171/145/165/117/135
_K   = 7
_N_STATES = 1 << (_K - 1)   # 64 states

# Generator polynomials in binary (octal equivalents: 133/171/145/165/117/135)
_GENS = [
    0b1011011,   # 133 oct
    0b1111001,   # 171 oct
    0b1100101,   # 145 oct
    0b1110101,   # 165 oct
    0b1001111,   # 117 oct
    0b1011101,   # 135 oct
]

# Precompute output bits for each (state, input_bit) pair
def _build_tables():
    n_out = len(_GENS)
    out_table   = np.zeros((_N_STATES, 2, n_out), dtype=np.uint8)
    next_table  = np.zeros((_N_STATES, 2),         dtype=np.int32)
    for state in range(_N_STATES):
        for bit in range(2):
            shifted = (state >> 1) | (bit << (_K - 2))
            reg     = shifted | (bit << (_K - 1))
            next_table[state, bit] = shifted
            for g_i, gen in enumerate(_GENS):
                out_table[state, bit, g_i] = bin(reg & gen).count('1') % 2
    return out_table, next_table

_OUT_TABLE, _NEXT_TABLE = _build_tables()


def viterbi_decode(received_bits: np.ndarray, n_output: int) -> np.ndarray:
    """
    Hard-decision Viterbi decoder for rate-1/6 convolutional code (K=7).

    received_bits: flat uint8 array of received (possibly punctured) bits,
                   grouped in sets of 6 per output bit.
    n_output: expected number of decoded bits.
    Returns decoded uint8 bit array.
    """
    n_out = len(_GENS)
    n_steps = min(len(received_bits) // n_out, n_output + _K)

    INF = 1 << 30
    path_metric = np.full(_N_STATES, INF, dtype=np.int64)
    path_metric[0] = 0
    survivor = np.zeros((_N_STATES, n_steps + 1), dtype=np.int32)

    for t in range(n_steps):
        rx    = received_bits[t * n_out: (t + 1) * n_out]
        if len(rx) < n_out:
            break
        new_pm = np.full(_N_STATES, INF, dtype=np.int64)
        for state in range(_N_STATES):
            if path_metric[state] == INF:
                continue
            for bit in range(2):
                enc   = _OUT_TABLE[state, bit]
                hd    = int(np.sum(enc != rx))   # Hamming distance
                cost  = path_metric[state] + hd
                ns    = _NEXT_TABLE[state, bit]
                if cost < new_pm[ns]:
                    new_pm[ns] = cost
                    survivor[ns, t + 1] = state
        path_metric = new_pm

    # Traceback from best state
    best_state = int(np.argmin(path_metric))
    bits_out   = np.zeros(n_steps, dtype=np.uint8)
    state      = best_state
    for t in range(n_steps - 1, -1, -1):
        prev = survivor[state, t + 1]
        bits_out[t] = (state >> (_K - 2)) & 1
        state = prev

    return bits_out[:n_output]


def depuncture(bits: np.ndarray, pattern: list[int]) -> np.ndarray:
    """
    Undo puncturing: insert zeros (erasures) where puncture pattern = 0.
    DRM MSC puncture pattern [1,1,0,1,0,0] (of 6 output bits → keep 3, rate 1/2).
    """
    n_in   = len(bits)
    n_kept = sum(pattern)
    n_full = (n_in // n_kept) * len(pattern)
    out    = np.zeros(n_full, dtype=np.uint8)
    src, dst = 0, 0
    while src < n_in and dst < n_full:
        for p in pattern:
            if dst >= n_full:
                break
            if p and src < n_in:
                out[dst] = bits[src]
                src += 1
            dst += 1
    return out

# DRM MSC puncture pattern: rate 1/6 → rate 1/2 puncturing
_PUNCTURE_PATTERN = [1, 1, 0, 1, 0, 0]

# ── CRC helpers ───────────────────────────────────────────────────────────────

def crc8(data: bytes, poly: int = 0xD5) -> int:
    """CRC-8 with polynomial 0xD5 (DRM FAC)."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def crc16(data: bytes, poly: int = 0x1021) -> int:
    """CRC-16 CCITT with polynomial 0x1021 (DRM MSC/SDC)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc

# ── Frame assembler ───────────────────────────────────────────────────────────

class FrameAssembler:
    """
    Reassembles JPEG image from DRM MSC segments.

    Each segment has a 4-byte header: [seg_num_hi, seg_num_lo, total_segs_hi, total_segs_lo]
    followed by up to 796 bytes of JPEG data and a 2-byte CRC-16.
    When all segments are received, decode JPEG → PNG data URL.
    """

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._segments: dict[int, bytes] = {}
        self._total_segs: int | None     = None

    def feed_segment(self, data: bytes) -> Image.Image | None:
        """Feed raw MSC payload bytes. Returns PIL image when all segments received."""
        if len(data) < 6:   # 4-byte header + 2-byte CRC minimum
            return None

        seg_num   = (data[0] << 8) | data[1]
        total     = (data[2] << 8) | data[3]
        payload   = data[4:-2]
        recv_crc  = (data[-2] << 8) | data[-1]

        calc_crc  = crc16(data[:-2])
        if calc_crc != recv_crc:
            log.debug("MSC segment %d/%d CRC mismatch (got %04x, exp %04x)", seg_num, total, recv_crc, calc_crc)
            return None

        if self._total_segs is None:
            self._total_segs = total
        elif total != self._total_segs:
            self._reset()
            self._total_segs = total

        self._segments[seg_num] = payload

        if len(self._segments) == self._total_segs:
            jpeg_bytes = b''.join(self._segments[i] for i in range(self._total_segs))
            self._reset()
            try:
                img = Image.open(io.BytesIO(jpeg_bytes))
                img.load()
                return img
            except Exception as e:
                log.warning("JPEG decode failed: %s", e)
                return None
        return None

# ── DRM frame processor ────────────────────────────────────────────────────────

class DRMProcessor:
    """
    Accumulates OFDM symbols into frames, runs FEC decode, feeds FrameAssembler.
    Returns PIL images when complete frames are decoded.
    """

    def __init__(self) -> None:
        self._sym_buf: list[np.ndarray] = []   # data cells per symbol
        self._assembler = FrameAssembler()

    def feed_symbol(self, cells: np.ndarray, data_mask: np.ndarray) -> Image.Image | None:
        """Feed one demodulated symbol's data cells. Returns image if frame complete."""
        data_cells = cells[data_mask]
        self._sym_buf.append(data_cells)

        if len(self._sym_buf) < FRAME_SYMBOLS:
            return None

        frame_syms       = self._sym_buf[:FRAME_SYMBOLS]
        self._sym_buf    = self._sym_buf[FRAME_SYMBOLS:]

        return self._decode_frame(frame_syms)

    def _decode_frame(self, frame_syms: list[np.ndarray]) -> Image.Image | None:
        # Deinterleave across frequency and time
        cells_flat = deinterleave_frame(frame_syms)

        if len(cells_flat) == 0:
            return None

        # 16-QAM demapping → bit stream
        bits = qam16_demap(cells_flat)

        # Depuncture + Viterbi FEC decode
        bits_depunct = depuncture(bits, _PUNCTURE_PATTERN)
        n_decoded    = len(bits_depunct) // len(_GENS)
        if n_decoded == 0:
            return None
        decoded_bits = viterbi_decode(bits_depunct, n_decoded)

        if len(decoded_bits) < 8:
            return None

        # Pack bits into bytes (MSB first)
        n_bytes  = len(decoded_bits) // 8
        raw_bytes = np.packbits(decoded_bits[:n_bytes * 8]).tobytes()

        # Skip first 9 bytes (FAC header) and try to feed as MSC segment
        if len(raw_bytes) > 9:
            return self._assembler.feed_segment(raw_bytes[9:])
        return None

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
        await self.broadcast({'type': 'status', 'connected': connected})

    async def send_status(self, ws: web.WebSocketResponse) -> None:
        await ws.send_str(json.dumps({'type': 'status', 'connected': self._connected}))

# ── IQ reader loop ─────────────────────────────────────────────────────────────

def image_to_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.convert('RGB').save(buf, format='PNG')
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


async def iq_reader(hub: Hub) -> None:
    chain     = IQSignalChain()
    baseband  = AudioToBaseband()
    ofdm      = OFDMDemodulator()
    processor = DRMProcessor()

    while True:
        try:
            log.info("Connecting to TCP mux at %s:%d…", MUX_HOST, MUX_PORT)
            reader, writer = await asyncio.open_connection(MUX_HOST, MUX_PORT)
            header = await reader.readexactly(12)
            if not header.startswith(b"RTL"):
                raise ValueError(f"Unexpected header: {header!r}")
            log.info("Connected. Listening for EasyPal on %.3f MHz…", EASYPAL_FREQ_HZ / 1e6)
            await hub.set_connected(True)

            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break

                # IQ → FM audio at 24 kHz
                audio = chain.process(chunk)

                # Audio → complex baseband at 12 kHz
                bb = baseband.process(audio)

                # Demodulate OFDM symbols
                symbols = ofdm.push(bb)

                # Process each symbol through FEC + frame assembler
                for cells, mask in symbols:
                    img = processor.feed_symbol(cells, mask)
                    if img is not None:
                        log.info("EasyPal frame decoded (%dx%d)", img.width, img.height)
                        loop     = asyncio.get_event_loop()
                        data_url = await loop.run_in_executor(None, image_to_data_url, img)
                        ts       = datetime.now(UTC).isoformat()
                        await hub.broadcast({
                            'type':         'frame',
                            'imageDataUrl': data_url,
                            'ts':           ts,
                        })

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
            pass
    finally:
        hub.remove(ws)
    return ws


async def main() -> None:
    hub = Hub()
    app = web.Application()
    app['hub'] = hub
    app.router.add_get('/ws/easypal', ws_handler)

    asyncio.create_task(iq_reader(hub))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', WS_PORT)
    await site.start()
    log.info("EasyPal decoder WebSocket listening on :%d /ws/easypal", WS_PORT)
    await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(main())
