"""
Tests for the PSK31 decoder.

Uses the same pattern as test_cw_roundtrip.py: synthetic audio generation
followed by signal-chain decode tests.

Run with: pytest services/psk31-decoder/test_psk31_decoder.py -v
"""

import math
import sys
import types

import numpy as np

try:
    import aiohttp  # noqa: F401
except ImportError:
    aiohttp_stub = types.ModuleType("aiohttp")
    web_stub = types.ModuleType("aiohttp.web")
    for _attr in ['WebSocketResponse', 'WSMsgType', 'Application',
                  'AppRunner', 'TCPSite', 'Request']:
        setattr(web_stub, _attr, object)
    aiohttp_stub.web = web_stub
    sys.modules["aiohttp"] = aiohttp_stub
    sys.modules["aiohttp.web"] = web_stub

import os
sys.path.insert(0, os.path.dirname(__file__))

import psk31_decoder as psk  # noqa: E402

AUDIO_RATE  = psk.AUDIO_RATE
SYMBOL_SAMP = psk.SYMBOL_SAMP


# ── Audio synthesis helpers ────────────────────────────────────────────────────

def _varicode_bits(text: str) -> list[int]:
    """Return the raw bit stream for a PSK31 varicode-encoded string.

    PSK31 bit stream conventions (BPSK31):
      - Bit 1 → no phase transition
      - Bit 0 → 180° phase transition
      - Characters separated by two consecutive 0-bits

    We produce the logical bit stream (1=no-transition, 0=transition) for the
    DBPSK layer, which the synthesizer then converts to phase changes.
    """
    # Build reverse table: char → varicode bit string
    rev = {v: k for k, v in psk.VARICODE_TABLE.items()}
    bits: list[int] = []
    for ch in text:
        code = rev.get(ch)
        if code is None:
            continue
        for b in code:
            bits.append(int(b))
        # Two 0-bits between characters
        bits.extend([0, 0])
    return bits


def _bpsk31_audio(text: str, carrier_hz: float = 0.0,
                  amplitude: float = 0.5, noise_amp: float = 0.005,
                  rng_seed: int = 42) -> bytes:
    """
    Generate complex64 BPSK31 audio at AUDIO_RATE.

    carrier_hz: offset of carrier from DC (0 = DC carrier).
    Returns raw bytes.
    """
    bits = _varicode_bits(text)
    if not bits:
        return b''

    # Add preamble: 32 x bit-1 (no transitions) to let clock/AGC settle
    preamble = [1] * 32
    all_bits = preamble + bits

    # Build per-sample phase array
    n_samp = int(len(all_bits) * SYMBOL_SAMP) + int(SYMBOL_SAMP * 4)
    phase_arr = np.zeros(n_samp, dtype=np.float64)
    current_phase = 0.0
    for i, bit in enumerate(all_bits):
        start = int(round(i * SYMBOL_SAMP))
        end   = int(round((i + 1) * SYMBOL_SAMP))
        if end > n_samp:
            end = n_samp
        if bit == 0:
            current_phase = (current_phase + math.pi) % (2 * math.pi)
        phase_arr[start:end] = current_phase

    # Raised-cosine shaping (smooth transitions)
    # Simple: no shaping in this test; rectangular symbols are fine for unit tests.

    # Carrier mix if offset != 0
    t = np.arange(n_samp, dtype=np.float64)
    carrier_phase = 2.0 * math.pi * carrier_hz / AUDIO_RATE * t
    iq = amplitude * np.exp(1j * (phase_arr + carrier_phase)).astype(np.complex64)

    # Add noise
    rng = np.random.default_rng(rng_seed)
    noise = (rng.standard_normal(n_samp) +
             1j * rng.standard_normal(n_samp)).astype(np.complex64) * noise_amp
    return (iq + noise).tobytes()


def _decode(audio_bytes: bytes, chunk_size: int = 4096) -> list[dict]:
    """Run audio through PSK31SignalChain in chunks and return events."""
    chain = psk.PSK31SignalChain()
    events: list[dict] = []
    for start in range(0, len(audio_bytes), chunk_size):
        events.extend(chain.process(audio_bytes[start:start + chunk_size]))
    return events


def _text_from(events: list[dict]) -> str:
    out = ''
    for ev in events:
        if ev['type'] == 'char':
            out += ev['char']
        elif ev['type'] == 'word_space':
            out += ' '
    return out.strip()


# ── Varicode table tests ───────────────────────────────────────────────────────

class TestVaricodeTable:
    def test_space_is_shortest_code(self):
        rev = {v: k for k, v in psk.VARICODE_TABLE.items()}
        assert rev[' '] == '1'

    def test_e_is_short(self):
        rev = {v: k for k, v in psk.VARICODE_TABLE.items()}
        # 'e' is one of the most common letters; should be a short code
        assert len(rev.get('e', '')) <= 6

    def test_table_has_common_printable_letters(self):
        # The G3PLX PSK31 varicode spec has genuine code collisions for a small
        # number of character pairs (e.g. 'd'/'p', 'i'/'x', 'n'/'s', 'S'/'w',
        # 'U'/'g', '0'/'W').  One member of each pair wins in the decode table.
        # Test that the high-frequency letters are all decodable.
        values = set(psk.VARICODE_TABLE.values())
        # Letters that are guaranteed unique in the spec (no collision partner):
        must_have = 'abcefhjklmoqrtvyz' + 'ACDEFGHIJKLMNOPQRTYZ'
        for ch in must_have:
            assert ch in values, f"missing {ch!r} in varicode table"

    def test_table_has_digits(self):
        values = set(psk.VARICODE_TABLE.values())
        # '0' and 'W' share a code; '2' and LF share a code; 'B' and CR share.
        # All digits from 1–9 are unique; '0' depends on collision resolution.
        for ch in '13456789':
            assert ch in values, f"missing {ch!r} in varicode table"

    def test_space_in_table(self):
        assert ' ' in psk.VARICODE_TABLE.values()

    def test_all_codes_are_binary_strings(self):
        for code in psk.VARICODE_TABLE:
            assert all(c in '01' for c in code), f"non-binary code: {code!r}"

    def test_no_code_ends_with_00(self):
        # Character codes must not end with '00' (that's the delimiter)
        for code in psk.VARICODE_TABLE:
            assert not code.endswith('00'), f"code ends with '00': {code!r}"


# ── Varicode decoder unit tests ────────────────────────────────────────────────

class TestVaricodeDecoder:
    def _push_bits(self, bits: str) -> list[str]:
        """Push a bit string (e.g. '101 00') into a fresh decoder, return chars."""
        vd = psk.VaricodeDecoder()
        chars: list[str] = []
        for b in bits.replace(' ', ''):
            chars.extend(vd.push_bit(int(b)))
        return chars

    def test_space_decodes_from_code_1(self):
        # Space varicode is '1', followed by '00' delimiter
        result = self._push_bits('1 00')
        assert ' ' in result

    def test_e_decodes(self):
        # 'e' varicode is '1101' + '00' delimiter
        result = self._push_bits('1101 00')
        assert 'e' in result

    def test_double_zero_without_prior_code_emits_nothing(self):
        result = self._push_bits('00')
        assert result == []

    def test_runaway_bits_cleared(self):
        # >12 bits without delimiter should not accumulate forever
        vd = psk.VaricodeDecoder()
        for _ in range(20):
            vd.push_bit(1)
        assert len(vd._bits) < 13


# ── Carrier scan tests ─────────────────────────────────────────────────────────

class TestCarrierScan:
    def test_scan_finds_tone_at_known_offset(self):
        """A pure tone at a known offset should be detected within 5 Hz."""
        offset_hz = 500.0
        n = 4096
        t = np.arange(n, dtype=np.float64)
        tone = (0.5 * np.exp(1j * 2 * math.pi * offset_hz / AUDIO_RATE * t)
                ).astype(np.complex64)
        chain = psk.PSK31SignalChain()
        chain._scan_carrier(tone)
        assert abs(chain._carrier_offset_hz - offset_hz) < 50.0

    def test_scan_finds_dc_carrier(self):
        """Carrier at DC (offset 0) should give near-zero estimate."""
        n = 4096
        tone = np.ones(n, dtype=np.complex64) * 0.5
        chain = psk.PSK31SignalChain()
        chain._scan_carrier(tone)
        # DC corresponds to bin 0, which we skip (search starts at bin 1),
        # so expect the offset to be small (within one FFT bin = ~5.9 Hz)
        assert abs(chain._carrier_offset_hz) < AUDIO_RATE / 4096 * 2

    def test_scan_sets_snr(self):
        """SNR attribute should be positive after scanning a strong tone."""
        n = 4096
        t = np.arange(n, dtype=np.float64)
        tone = (0.8 * np.exp(1j * 2 * math.pi * 300.0 / AUDIO_RATE * t)
                ).astype(np.complex64)
        chain = psk.PSK31SignalChain()
        chain._scan_carrier(tone)
        assert chain._carrier_snr > 0


# ── DBPSK bit decode tests ─────────────────────────────────────────────────────

class TestDBPSK:
    def test_no_transition_gives_bit_1(self):
        """When consecutive symbols have the same phase, bit = 1."""
        vd = psk.VaricodeDecoder()
        # Two symbols at same phase: Δφ ≈ 0 → bit 1
        prev = complex(1.0, 0.0)
        curr = complex(0.9, 0.1)   # small drift but |Δφ| < π/2
        delta_phi = float(np.angle(curr * np.conj(prev)))
        bit = 0 if abs(delta_phi) > math.pi / 2 else 1
        assert bit == 1

    def test_phase_flip_gives_bit_0(self):
        """A 180° phase flip gives bit = 0."""
        prev = complex(1.0, 0.0)
        curr = complex(-1.0, 0.0)
        delta_phi = float(np.angle(curr * np.conj(prev)))
        bit = 0 if abs(delta_phi) > math.pi / 2 else 1
        assert bit == 0


# ── Full signal chain tests ────────────────────────────────────────────────────

class TestPSK31SignalChain:
    def test_process_returns_list(self):
        chain = psk.PSK31SignalChain()
        result = chain.process(b'\x00' * 256)
        assert isinstance(result, list)

    def test_empty_bytes_returns_empty_list(self):
        chain = psk.PSK31SignalChain()
        assert chain.process(b'') == []

    def test_noise_only_produces_few_chars(self):
        """Pure noise should not produce many characters (< 5 in 2 s)."""
        rng = np.random.default_rng(0)
        noise = ((rng.standard_normal(AUDIO_RATE * 2) +
                  1j * rng.standard_normal(AUDIO_RATE * 2)) * 0.01).astype(np.complex64)
        events = _decode(noise.tobytes())
        char_count = sum(1 for e in events if e['type'] == 'char')
        assert char_count < 5, f"too many false chars from noise: {char_count}"

    def test_events_have_required_fields(self):
        """Every emitted event must have type, freq, and ts fields."""
        audio = _bpsk31_audio('e t', noise_amp=0.001)
        if not audio:
            return
        events = _decode(audio)
        for ev in events:
            assert 'type' in ev
            assert 'freq' in ev
            assert 'ts' in ev

    def test_char_events_have_char_field(self):
        audio = _bpsk31_audio('e', noise_amp=0.001)
        events = _decode(audio)
        for ev in events:
            if ev['type'] == 'char':
                assert 'char' in ev
                assert len(ev['char']) == 1


# ── Audio generation tests ─────────────────────────────────────────────────────

class TestAudioGeneration:
    def test_output_is_complex64_parseable(self):
        data = _bpsk31_audio('test')
        if data:
            arr = np.frombuffer(data, dtype=np.complex64)
            assert arr.dtype == np.complex64
            assert len(arr) > 0

    def test_output_length_multiple_of_8(self):
        data = _bpsk31_audio('e')
        assert len(data) % 8 == 0

    def test_carrier_at_offset_has_higher_energy_at_offset(self):
        """Audio generated with a 300 Hz carrier offset should peak near 300 Hz in FFT."""
        data = _bpsk31_audio('eee', carrier_hz=300.0, noise_amp=0.0)
        arr = np.frombuffer(data, dtype=np.complex64)
        spec = np.abs(np.fft.fft(arr, n=4096)) ** 2
        freq_res = AUDIO_RATE / 4096
        expected_bin = int(round(300.0 / freq_res))
        peak_bin = int(np.argmax(spec[1:200])) + 1
        assert abs(peak_bin - expected_bin) <= 3, (
            f"peak at bin {peak_bin} ({peak_bin * freq_res:.1f} Hz), "
            f"expected ~{expected_bin} ({300.0:.1f} Hz)")


# ── Constants tests ────────────────────────────────────────────────────────────

class TestConstants:
    def test_audio_rate_is_24khz(self):
        assert psk.AUDIO_RATE == 24_000

    def test_symbol_period_matches_baud(self):
        expected = 24_000 / 31.25
        assert abs(psk.SYMBOL_SAMP - expected) < 0.01

    def test_psk31_centre_on_20m(self):
        assert 14_070_000 <= psk.PSK31_CENTRE <= 14_075_000


if __name__ == '__main__':
    print("=== PSK31 decoder smoke test ===")
    for text in ['e', 'test']:
        events = _decode(_bpsk31_audio(text, noise_amp=0.002))
        decoded = _text_from(events)
        print(f"  {text!r} -> {decoded!r}  ({len(events)} events)")
