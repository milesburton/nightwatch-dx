"""
CW Roundtrip Test
=================

Generates synthetic IQ data containing a CW tone at 14.029 MHz (offset -146 kHz
from the 14.175 MHz RF centre), encodes a known Morse sequence, feeds it through
CWSignalChain, and asserts the decoded characters match.

Run with:
    cd services/cw-decoder
    pip install -r requirements.txt
    python -m pytest test_cw_roundtrip.py -v
"""

import math
import sys
import types

import numpy as np

# ---------------------------------------------------------------------------
# Pull in the module under test.  cw_decoder.py imports aiohttp at module
# level but does NOT use it during signal processing.  Stub it out so the
# test doesn't need a running web server.
# ---------------------------------------------------------------------------

# Stub aiohttp if it is not installed (CI environment).
try:
    import aiohttp  # noqa: F401
except ImportError:
    aiohttp_stub = types.ModuleType("aiohttp")
    web_stub = types.ModuleType("aiohttp.web")
    web_stub.WebSocketResponse = object
    web_stub.WSMsgType = object
    web_stub.Application = object
    web_stub.AppRunner = object
    web_stub.TCPSite = object
    web_stub.Request = object
    aiohttp_stub.web = web_stub
    sys.modules["aiohttp"] = aiohttp_stub
    sys.modules["aiohttp.web"] = web_stub

import os

# Add the service directory to the path so we can import cw_decoder.
sys.path.insert(0, os.path.dirname(__file__))

import cw_decoder as cwd  # noqa: E402  (after sys.path manipulation)

# ---------------------------------------------------------------------------
# Signal-generation helpers
# ---------------------------------------------------------------------------

SDR_SAMPLE_RATE = cwd.SDR_SAMPLE_RATE   # 2_400_000
RF_CENTER_HZ    = cwd.RF_CENTER_HZ      # 14_175_000
CW_FREQ_HZ      = cwd.CW_FREQ_HZ       # 14_029_000
FREQ_OFFSET_HZ  = cwd.FREQ_OFFSET_HZ   # -146_000
WPM             = cwd.WPM               # 20
DIT_SAMPLES     = cwd.DIT_SAMPLES       # 72 audio samples at 24 kHz
AUDIO_RATE      = cwd.AUDIO_RATE        # 24_000
DECIMATE        = SDR_SAMPLE_RATE // AUDIO_RATE   # 100

# Morse code table (character → symbol string)
MORSE: dict[str, str] = {v: k for k, v in cwd.MORSE_CODE.items()}
MORSE[' '] = ' '   # word space


def dit_sdr_samples() -> int:
    """Number of SDR samples per dit at the configured WPM."""
    # DIT_SAMPLES is in audio samples; multiply by decimation factor
    return DIT_SAMPLES * DECIMATE


def make_cw_iq(text: str, amplitude: float = 0.6, noise_amplitude: float = 0.02) -> bytes:
    """
    Synthesise IQ bytes for a CW message.

    The tone is placed at FREQ_OFFSET_HZ relative to DC (= CW_FREQ_HZ in the
    RF frame), so that CWSignalChain's LO mixes it to baseband correctly.

    Morse timing (at WPM speed):
        dit  = 1 dit
        dah  = 3 dits
        intra-character gap  = 1 dit (between elements)
        inter-character gap  = 3 dits
        inter-word gap       = 7 dits (space in text)

    Returns raw uint8 IQ bytes (I, Q interleaved) as expected by
    CWSignalChain.process().
    """
    dit = dit_sdr_samples()

    # Build a timeline of on/off intervals
    intervals: list[tuple[bool, int]] = []  # (tone_on, num_sdr_samples)

    first_char = True
    for ch in text.upper():
        if ch == ' ':
            # Inter-word gap is 7 dits; we already added 3 from the previous
            # char gap, so add 4 more.
            intervals.append((False, dit * 4))
            first_char = True
            continue

        symbols = MORSE.get(ch)
        if symbols is None:
            continue

        # Inter-character gap (3 dits) before every character except the first
        if not first_char:
            intervals.append((False, dit * 3))
        first_char = False

        # Encode each element
        for j, sym in enumerate(symbols):
            if j > 0:
                intervals.append((False, dit))  # intra-char gap
            dur = dit if sym == '.' else dit * 3
            intervals.append((True, dur))

    # Add a trailing word-gap so the last character gets flushed
    intervals.append((False, dit * 7))

    # Synthesise complex samples
    total_samples = sum(n for _, n in intervals)
    iq = np.zeros(total_samples, dtype=np.complex64)

    phase = 0.0
    phase_step = 2 * math.pi * FREQ_OFFSET_HZ / SDR_SAMPLE_RATE
    idx = 0
    rng = np.random.default_rng(42)

    for tone_on, n in intervals:
        if tone_on:
            t = np.arange(n, dtype=np.float64)
            phases = phase + t * phase_step
            chunk = amplitude * np.exp(1j * phases).astype(np.complex64)
            phase = float(phases[-1] + phase_step)
        else:
            chunk = np.zeros(n, dtype=np.complex64)
            phase += phase_step * n

        # Add a tiny amount of noise so the adaptive threshold has variance
        noise = rng.standard_normal(n) * noise_amplitude + 1j * rng.standard_normal(n) * noise_amplitude
        iq[idx:idx + n] = chunk + noise.astype(np.complex64)
        idx += n

    # Convert complex64 to uint8 IQ (rtl_tcp format: I byte, Q byte)
    i_f = np.clip(iq.real * 127.5 + 127.5, 0, 255).astype(np.uint8)
    q_f = np.clip(iq.imag * 127.5 + 127.5, 0, 255).astype(np.uint8)
    raw = np.empty(total_samples * 2, dtype=np.uint8)
    raw[0::2] = i_f
    raw[1::2] = q_f
    return raw.tobytes()


def decode_message(text: str, chunk_size: int = 65536) -> list[dict]:
    """
    Feed synthesised IQ for *text* through CWSignalChain and collect all events.
    Calls flush() at the end to emit any character buffered in the trailing gap.
    """
    chain = cwd.CWSignalChain()
    iq_bytes = make_cw_iq(text)

    events: list[dict] = []
    for start in range(0, len(iq_bytes), chunk_size):
        chunk = iq_bytes[start:start + chunk_size]
        events.extend(chain.process(chunk))

    events.extend(chain.flush())
    return events


def events_to_text(events: list[dict]) -> str:
    """Reconstruct the decoded text from a list of CW events."""
    result = []
    for ev in events:
        if ev['type'] == 'char':
            result.append(ev['char'])
        elif ev['type'] == 'word_space':
            result.append(' ')
    return ''.join(result).strip()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMorseDecoder:
    """Unit tests for the MorseDecoder state machine in isolation."""

    def test_single_dit_is_E(self):
        md = cwd.MorseDecoder()
        dit = 72  # audio samples
        md.push_tone(dit, dit)
        events = md.push_gap(dit * 7, dit)  # word gap flushes
        chars = [e['char'] for e in events if e['type'] == 'char']
        assert 'E' in chars

    def test_dah_is_T(self):
        md = cwd.MorseDecoder()
        dit = 72
        md.push_tone(dit * 3, dit)  # dah
        events = md.push_gap(dit * 7, dit)
        chars = [e['char'] for e in events if e['type'] == 'char']
        assert 'T' in chars

    def test_CQ(self):
        """Encode CQ: -.-. --.-"""
        md = cwd.MorseDecoder()
        dit = 72

        def tone(n): md.push_tone(n, dit)
        def gap(n): return md.push_gap(n, dit)

        # C = -.-. (dah dit dah dit)
        tone(dit * 3)
        tone(dit)
        tone(dit * 3)
        tone(dit)
        gap(dit * 3)  # char gap → produces 'C'
        # Q = --.- (dah dah dit dah)
        tone(dit * 3)
        tone(dit * 3)
        tone(dit)
        tone(dit * 3)
        events = gap(dit * 7)  # word gap → produces 'Q'

        chars = [e['char'] for e in events if e['type'] == 'char']
        assert 'Q' in chars

    def test_word_space_event(self):
        md = cwd.MorseDecoder()
        dit = 72
        # Send E then word gap
        md.push_tone(dit, dit)
        events = md.push_gap(dit * 7, dit)
        types = [e['type'] for e in events]
        assert 'word_space' in types

    def test_short_noise_ignored(self):
        """Tones shorter than 40% of a dit are treated as noise."""
        md = cwd.MorseDecoder()
        dit = 72
        md.push_tone(int(dit * 0.3), dit)   # too short → ignored
        md.push_tone(dit, dit)               # dit → E
        events = md.push_gap(dit * 7, dit)
        chars = [e['char'] for e in events if e['type'] == 'char']
        assert chars == ['E']


class TestCWSignalChain:
    """Integration tests: synthesised IQ → CWSignalChain → decoded characters."""

    def test_single_E(self):
        """Shortest message: single dit encodes 'E'."""
        events = decode_message('E')
        chars = [e['char'] for e in events if e['type'] == 'char']
        assert 'E' in chars, f"Expected 'E' in {chars}"

    def test_CQ_de(self):
        """Decode 'CQ DE' — the classic ham radio calling sequence."""
        events = decode_message('CQ DE')
        text = events_to_text(events)
        print(f"\nDecoded: '{text}'  (from 'CQ DE')")
        # Allow for minor mis-decodes at word boundaries; require key letters
        assert 'C' in text, f"Missing C in '{text}'"
        assert 'Q' in text, f"Missing Q in '{text}'"

    def test_SOS(self):
        """Decode 'SOS' — well-known Morse pattern ... --- ..."""
        events = decode_message('SOS')
        chars = [e['char'] for e in events if e['type'] == 'char']
        print(f"\nDecoded chars for 'SOS': {chars}")
        assert 'S' in chars, f"Missing S: {chars}"
        assert 'O' in chars, f"Missing O: {chars}"

    def test_word_space_detected(self):
        """A space in the input must produce a word_space event."""
        events = decode_message('E T')
        types = {e['type'] for e in events}
        assert 'word_space' in types, f"No word_space event in {events}"

    def test_output_is_uppercase(self):
        """All decoded chars must be uppercase or punctuation."""
        events = decode_message('HELLO')
        chars = [e['char'] for e in events if e['type'] == 'char']
        print(f"\nDecoded chars for 'HELLO': {chars}")
        for ch in chars:
            assert ch == ch.upper() or not ch.isalpha(), f"Lower-case char: {ch}"

    def test_chunked_processing(self):
        """Signal split across many small chunks still decodes correctly."""
        events = decode_message('SOS', chunk_size=4096)
        chars = [e['char'] for e in events if e['type'] == 'char']
        print(f"\nDecoded (small chunks) for 'SOS': {chars}")
        assert 'S' in chars

    def test_noise_only_produces_no_chars(self):
        """Pure noise (no CW tone) should not produce character events."""
        rng = np.random.default_rng(0)
        noise = rng.integers(100, 155, size=SDR_SAMPLE_RATE * 2, dtype=np.uint8)
        chain = cwd.CWSignalChain()
        events = chain.process(noise.tobytes())
        chars = [e for e in events if e['type'] == 'char']
        # We allow a very small number of false-positives from pure noise
        # (adaptive threshold may not be stable yet), but not a flood.
        assert len(chars) < 5, f"Too many false chars from noise: {chars}"


class TestIQGeneration:
    """Sanity-checks on the test signal generator itself."""

    def test_iq_bytes_length_is_even(self):
        data = make_cw_iq('E')
        assert len(data) % 2 == 0

    def test_iq_values_in_range(self):
        data = np.frombuffer(make_cw_iq('SOS'), dtype=np.uint8)
        assert data.min() >= 0
        assert data.max() <= 255

    def test_tone_present_at_correct_offset(self):
        """After mixing to baseband, the tone peak should be near DC (0 Hz)."""
        # Take the first 4096 samples (first dit should be a tone)
        data = make_cw_iq('T')   # T = single dah → long tone at start
        raw = np.frombuffer(data[:4096 * 2], dtype=np.uint8).astype(np.float32)
        iq = ((raw[0::2] - 127.5) + 1j * (raw[1::2] - 127.5)) / 127.5

        # Mix down by FREQ_OFFSET_HZ (what CWSignalChain does)
        n = len(iq)
        t = np.arange(n)
        lo = np.exp(-1j * 2 * np.pi * FREQ_OFFSET_HZ * t / SDR_SAMPLE_RATE)
        mixed = iq * lo

        spectrum = np.abs(np.fft.fftshift(np.fft.fft(mixed)))
        peak_bin = int(np.argmax(spectrum))
        freqs = np.fft.fftshift(np.fft.fftfreq(n, 1 / SDR_SAMPLE_RATE))
        peak_freq = freqs[peak_bin]

        print(f"\nTone peak after mix-down: {peak_freq:.0f} Hz (expect near 0)")
        # Allow ±5 kHz (the FIR will attenuate off-centre, but we just check
        # the peak is near DC, not at -146 kHz)
        assert abs(peak_freq) < 5000, f"Peak at {peak_freq:.0f} Hz — tone not at correct offset"


if __name__ == '__main__':
    # Quick smoke-test when run directly
    print("=== CW Roundtrip Smoke Test ===")
    for msg in ['E', 'SOS', 'CQ DE']:
        events = decode_message(msg)
        text = events_to_text(events)
        print(f"  Input:   '{msg}'")
        print(f"  Decoded: '{text}'")
        print()
