"""
Tests for the CW decoder service.

Tests the pure-logic components that have no hardware or network dependencies:
- Morse code dictionary completeness
- MorseDecoder state machine (dit/dah, char gaps, word gaps)
- EnvelopeDetector basic operation
- IQ → complex conversion and even-length truncation
- Configuration constants (sample rates, LPF cutoff)
"""

import sys
import os
import struct
import numpy as np
import pytest

# Make the service importable without running it
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'services', 'cw-decoder'))
import cw_decoder as cw


# ── Morse dictionary ──────────────────────────────────────────────────────────

class TestMorseDictionary:
    def test_all_letters_present(self):
        chars = set(cw.MORSE_CODE.values())
        for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
            assert letter in chars, f"Missing letter: {letter}"

    def test_all_digits_present(self):
        chars = set(cw.MORSE_CODE.values())
        for digit in '0123456789':
            assert digit in chars, f"Missing digit: {digit}"

    def test_known_codes(self):
        assert cw.MORSE_CODE['.-']   == 'A'
        assert cw.MORSE_CODE['-...'] == 'B'
        assert cw.MORSE_CODE['.']    == 'E'
        assert cw.MORSE_CODE['-']    == 'T'
        assert cw.MORSE_CODE['...']  == 'S'
        assert cw.MORSE_CODE['---']  == 'O'

    def test_sos(self):
        assert cw.MORSE_CODE['...---...'] == 'SOS'

    def test_no_empty_codes(self):
        for code, char in cw.MORSE_CODE.items():
            assert code.strip(), f"Empty code for char {char!r}"
            assert char.strip(), f"Empty char for code {code!r}"


# ── MorseDecoder state machine ────────────────────────────────────────────────

class TestMorseDecoder:
    def _decode_sequence(self, tones, gaps):
        """Helper: alternate tone/gap durations → list of decoded chars."""
        decoder = cw.MorseDecoder()
        results = []
        callback = results.append

        for i, tone_dur in enumerate(tones):
            decoder.push_tone(tone_dur)
            if i < len(gaps):
                decoder.push_gap(gaps[i], callback)
        # flush remaining symbol
        decoder.push_gap(int(cw.DIT_SAMPLES * cw.WORD_GAP_DITS + 1), callback)
        return results

    def test_dit_produces_dot(self):
        decoder = cw.MorseDecoder()
        decoder.push_tone(cw.DIT_SAMPLES)
        assert decoder._symbols == ['.']

    def test_dah_produces_dash(self):
        decoder = cw.MorseDecoder()
        decoder.push_tone(int(cw.DIT_SAMPLES * cw.DAH_THRESHOLD))
        assert decoder._symbols == ['-']

    def test_too_short_tone_ignored(self):
        decoder = cw.MorseDecoder()
        decoder.push_tone(int(cw.DIT_SAMPLES * 0.3))
        assert decoder._symbols == []

    def test_decode_e(self):
        # E = single dit
        results = self._decode_sequence(
            [cw.DIT_SAMPLES],
            [int(cw.DIT_SAMPLES * cw.WORD_GAP_DITS + 1)]
        )
        assert 'E' in results

    def test_decode_t(self):
        # T = single dah
        results = self._decode_sequence(
            [int(cw.DIT_SAMPLES * cw.DAH_THRESHOLD)],
            [int(cw.DIT_SAMPLES * cw.WORD_GAP_DITS + 1)]
        )
        assert 'T' in results

    def test_word_gap_triggers_word_space(self):
        decoder = cw.MorseDecoder()
        results = []
        decoder.push_tone(cw.DIT_SAMPLES)
        decoder.push_gap(int(cw.DIT_SAMPLES * cw.WORD_GAP_DITS + 1), results.append)
        assert None in results  # None = word_space sentinel

    def test_char_gap_flushes_without_word_space(self):
        decoder = cw.MorseDecoder()
        results = []
        decoder.push_tone(cw.DIT_SAMPLES)
        decoder.push_gap(int(cw.DIT_SAMPLES * cw.CHAR_GAP_DITS + 1), results.append)
        # Should have flushed a char but not a word space
        assert None not in results
        assert len(results) == 1

    def test_flush_empty_symbols_no_callback(self):
        """_flush on empty symbols should not call callback."""
        decoder = cw.MorseDecoder()
        called = []
        decoder._flush(called.append)
        assert called == []

    def test_unknown_code_produces_bracketed(self):
        """An unrecognised pattern returns [<code>]."""
        decoder = cw.MorseDecoder()
        results = []
        # Push a pattern that isn't in MORSE_CODE
        decoder._symbols = ['.', '-', '.', '-', '.', '-', '.', '-']
        decoder._flush(results.append)
        assert results[0].startswith('[')
        assert results[0].endswith(']')


# ── Configuration constants ───────────────────────────────────────────────────

class TestConfiguration:
    def test_audio_sample_rate(self):
        assert cw.AUDIO_SAMPLE_RATE == cw.SDR_SAMPLE_RATE // cw.DECIMATE_FACTOR

    def test_decimate_factor(self):
        assert cw.DECIMATE_FACTOR == 10

    def test_lpf_cutoff_range(self):
        # Should be a small fraction of Nyquist (< 0.01 for 300Hz at 240kHz)
        assert 0 < cw.ENVELOPE_LPF_CUTOFF < 0.01

    def test_lpf_cutoff_value(self):
        nyquist = cw.AUDIO_SAMPLE_RATE / 2.0
        expected = 300.0 / nyquist
        assert abs(cw.ENVELOPE_LPF_CUTOFF - expected) < 1e-9

    def test_dit_samples_reasonable(self):
        # At 20 WPM, 240kHz: dit = (60/(50*20)) * 240000 = 14400 samples
        assert 1000 < cw.DIT_SAMPLES < 100_000

    def test_gain_tenths_of_db(self):
        # GAIN=420 means 42.0 dB
        assert cw.GAIN == 420


# ── IQ conversion ─────────────────────────────────────────────────────────────

class TestIQConversion:
    def _u8_to_iq(self, raw: bytes) -> np.ndarray:
        """Replicate the service's IQ conversion including even-length truncation."""
        u8 = np.frombuffer(raw, dtype=np.uint8)[:len(raw) & ~1].astype(np.float32)
        return ((u8[0::2] - 127.5) + 1j * (u8[1::2] - 127.5)) / 127.5

    def test_dc_offset_maps_to_zero(self):
        # 127, 127 → (0+0j)
        raw = bytes([127, 127] * 16)
        iq = self._u8_to_iq(raw)
        assert np.allclose(iq, 0, atol=0.01)

    def test_max_value_maps_near_one(self):
        raw = bytes([255, 255] * 16)
        iq = self._u8_to_iq(raw)
        assert np.all(iq.real > 0.99)
        assert np.all(iq.imag > 0.99)

    def test_min_value_maps_near_minus_one(self):
        raw = bytes([0, 0] * 16)
        iq = self._u8_to_iq(raw)
        assert np.all(iq.real < -0.99)
        assert np.all(iq.imag < -0.99)

    def test_odd_length_truncated(self):
        # 33 bytes → 32 bytes → 16 IQ samples
        raw = bytes(33)
        iq = self._u8_to_iq(raw)
        assert len(iq) == 16

    def test_even_length_unchanged(self):
        raw = bytes(64)
        iq = self._u8_to_iq(raw)
        assert len(iq) == 32

    def test_output_dtype_complex(self):
        raw = bytes([127, 200] * 8)
        iq = self._u8_to_iq(raw)
        assert np.iscomplexobj(iq)


# ── EnvelopeDetector ─────────────────────────────────────────────────────────

class TestEnvelopeDetector:
    def test_produces_non_negative_envelope(self):
        detector = cw.EnvelopeDetector()
        iq = np.random.randn(1024) + 1j * np.random.randn(1024)
        iq = iq.astype(np.complex64)
        filtered, thresh = detector.process(iq)
        assert np.all(filtered >= 0)

    def test_threshold_is_positive(self):
        detector = cw.EnvelopeDetector()
        # Feed enough samples to populate the recent buffer
        iq = (np.random.randn(10000) + 1j * np.random.randn(10000)).astype(np.complex64)
        _, thresh = detector.process(iq)
        assert thresh > 0

    def test_strong_signal_exceeds_threshold(self):
        detector = cw.EnvelopeDetector()
        # Warm up with noise
        noise = (np.random.randn(5000) * 0.01).astype(np.complex64)
        detector.process(noise)
        # Now send a strong tone
        t = np.arange(1024) / cw.AUDIO_SAMPLE_RATE
        tone = (np.sin(2 * np.pi * 800 * t) * 0.9).astype(np.complex64)
        filtered, thresh = detector.process(tone)
        assert np.max(filtered) > thresh
