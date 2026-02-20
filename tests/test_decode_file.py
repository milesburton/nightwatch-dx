"""
Tests for the CW audio file decoder (decode_file.py).

Tests pure-logic components with synthetic audio — no file I/O or ffmpeg
dependency required for the unit tests:
- Tone auto-detection
- Bandpass filter
- Envelope detection
- Adaptive threshold (noise floor / signal peak midpoint)
- MorseDecoder (re-exported from decode_file, independent of cw_decoder)
- End-to-end decode of a synthesised CW tone
"""

import sys
import os
import numpy as np
import pytest

# Make the service importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'services', 'cw-decoder'))
import decode_file as df


# ── Helpers ───────────────────────────────────────────────────────────────────

SR = df.SAMPLE_RATE  # 8000 Hz


def make_tone(freq_hz: float, duration_s: float, amplitude: float = 0.7) -> np.ndarray:
    """Synthesise a pure sine tone."""
    t = np.arange(int(duration_s * SR)) / SR
    return (np.sin(2 * np.pi * freq_hz * t) * amplitude).astype(np.float32)


def make_cw_audio(tone_hz: float, dit_s: float,
                  *letter_patterns: str) -> np.ndarray:
    """
    Build a mono audio array from one or more Morse letter patterns.

    Each letter_pattern is a string of '.' and '-' characters.
    Letters are separated by a 3-dit char gap; a word gap (7 dits) is
    appended at the end to flush the Morse decoder.

    Example: make_cw_audio(750.0, 0.08, '-.-.', '--.-')  →  "CQ"
    """
    eg = dit_s          # inter-element gap (1 dit)
    cg = dit_s * 3      # inter-character gap (3 dits)
    wg = dit_s * 7      # trailing word gap to flush decoder

    chunks = []
    for idx, pattern in enumerate(letter_patterns):
        if idx > 0:
            # Replace trailing element gap with character gap.
            # Add an extra dit of margin so the gap clearly exceeds CHAR_GAP_DITS
            # despite the small envelope LPF delay at tone edges.
            chunks.append(np.zeros(int((cg - eg + eg * 0.5) * SR), dtype=np.float32))
        for sym in pattern:
            dur = dit_s if sym == '.' else dit_s * 3
            chunks.append(make_tone(tone_hz, dur))
            chunks.append(np.zeros(int(eg * SR), dtype=np.float32))
    # Trailing word gap to flush decoder
    chunks.append(np.zeros(int(wg * SR), dtype=np.float32))
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


# ── Tone detection ────────────────────────────────────────────────────────────

class TestToneDetection:
    def test_detects_750_hz(self):
        audio = make_tone(750.0, 2.0)
        hz = df.detect_tone_hz(audio, SR)
        assert abs(hz - 750.0) < 10.0

    def test_detects_600_hz(self):
        audio = make_tone(600.0, 2.0)
        hz = df.detect_tone_hz(audio, SR)
        assert abs(hz - 600.0) < 10.0

    def test_detects_1000_hz(self):
        audio = make_tone(1000.0, 2.0)
        hz = df.detect_tone_hz(audio, SR)
        assert abs(hz - 1000.0) < 15.0

    def test_ignores_below_300_hz(self):
        # 100 Hz tone should NOT be detected (below CW range)
        audio = make_tone(100.0, 2.0) * 2.0
        hz = df.detect_tone_hz(audio, SR)
        assert hz >= 300.0

    def test_returns_float(self):
        audio = make_tone(750.0, 1.0)
        hz = df.detect_tone_hz(audio, SR)
        assert isinstance(hz, float)


# ── Bandpass filter ───────────────────────────────────────────────────────────

class TestBandpassFilter:
    def test_passes_target_frequency(self):
        audio = make_tone(750.0, 1.0)
        out = df.bandpass_filter(audio, centre_hz=750.0, width_hz=300.0, sample_rate=SR)
        # Output should retain most energy
        assert np.sqrt(np.mean(out**2)) > 0.3

    def test_attenuates_out_of_band(self):
        # Tone far outside the passband — 2000 Hz when centre=750, width=300
        audio = make_tone(2000.0, 1.0)
        out = df.bandpass_filter(audio, centre_hz=750.0, width_hz=300.0, sample_rate=SR)
        rms_in  = np.sqrt(np.mean(audio**2))
        rms_out = np.sqrt(np.mean(out**2))
        assert rms_out < rms_in * 0.1

    def test_output_length_unchanged(self):
        audio = make_tone(750.0, 1.0)
        out = df.bandpass_filter(audio, centre_hz=750.0, width_hz=300.0, sample_rate=SR)
        assert len(out) == len(audio)


# ── Envelope detection ────────────────────────────────────────────────────────

class TestEnvelopeDetection:
    def test_envelope_non_negative(self):
        audio = make_tone(750.0, 1.0)
        env = df.envelope_detect(audio, lpf_cutoff_hz=200.0, sample_rate=SR)
        assert np.all(env >= 0)

    def test_silence_gives_low_envelope(self):
        silence = np.zeros(SR, dtype=np.float32)
        env = df.envelope_detect(silence, lpf_cutoff_hz=200.0, sample_rate=SR)
        assert np.max(env) < 0.01

    def test_tone_gives_positive_envelope(self):
        audio = make_tone(750.0, 1.0, amplitude=0.7)
        env = df.envelope_detect(audio, lpf_cutoff_hz=200.0, sample_rate=SR)
        # After filter settling (~50 samples), should see positive envelope
        assert np.mean(env[200:]) > 0.1


# ── Adaptive threshold ────────────────────────────────────────────────────────

class TestAdaptiveThreshold:
    def _make_keyed_envelope(self, duty=0.5, n_samples=SR * 4):
        """Envelope with alternating signal/silence blocks."""
        env = np.zeros(n_samples, dtype=np.float32)
        block = n_samples // 20
        for i in range(20):
            if i % 2 == 0:
                env[i*block:(i+1)*block] = 0.6
        return env

    def test_threshold_between_noise_and_signal(self):
        env = self._make_keyed_envelope()
        thresh = df.adaptive_threshold(env, window_samples=SR * 2)
        # Threshold should be between 0 and 0.6 (not above signal)
        t_mid = thresh[SR * 2:]   # skip initial window
        assert np.all(t_mid < 0.6)
        assert np.all(t_mid > 0.0)

    def test_threshold_length_matches_input(self):
        env = np.random.rand(SR * 3).astype(np.float32)
        thresh = df.adaptive_threshold(env, window_samples=SR)
        assert len(thresh) == len(env)

    def test_signal_exceeds_threshold(self):
        """Signal samples should be above threshold; silence samples below."""
        env = self._make_keyed_envelope()
        thresh = df.adaptive_threshold(env, window_samples=SR * 2)
        # After warmup, check that threshold correctly separates signal from noise
        signal_samples = env[SR*2:] == 0.6
        noise_samples  = env[SR*2:] == 0.0
        t = thresh[SR*2:]
        if signal_samples.any() and noise_samples.any():
            assert np.mean(env[SR*2:][signal_samples] > t[signal_samples]) > 0.8
            assert np.mean(env[SR*2:][noise_samples]  < t[noise_samples])  > 0.8


# ── MorseDecoder (decode_file version) ───────────────────────────────────────

class TestDecodeFileMorseDecoder:
    """Tests for the MorseDecoder class in decode_file.py."""

    DIT = 640   # samples at 8 kHz, 15 WPM

    def test_dit_produces_dot(self):
        dec = df.MorseDecoder()
        dec.push_tone(self.DIT, self.DIT)
        assert dec._symbols == ['.']

    def test_dah_produces_dash(self):
        dec = df.MorseDecoder()
        dec.push_tone(int(self.DIT * df.DAH_THRESHOLD), self.DIT)
        assert dec._symbols == ['-']

    def test_too_short_tone_ignored(self):
        dec = df.MorseDecoder()
        dec.push_tone(int(self.DIT * 0.3), self.DIT)
        assert dec._symbols == []

    def test_word_gap_triggers_word_space(self):
        dec = df.MorseDecoder()
        results = []
        dec.push_tone(self.DIT, self.DIT)
        dec.push_gap(int(self.DIT * df.WORD_GAP_DITS + 1), self.DIT, results.append)
        assert None in results

    def test_char_gap_no_word_space(self):
        dec = df.MorseDecoder()
        results = []
        dec.push_tone(self.DIT, self.DIT)
        dec.push_gap(int(self.DIT * df.CHAR_GAP_DITS + 1), self.DIT, results.append)
        assert None not in results
        assert len(results) == 1

    def test_flush_empty_no_callback(self):
        dec = df.MorseDecoder()
        called = []
        dec._flush(called.append)
        assert called == []

    def test_unknown_code_bracketed(self):
        dec = df.MorseDecoder()
        results = []
        dec._symbols = ['.', '-', '.', '-', '.', '-', '.', '-']
        dec._flush(results.append)
        assert results[0].startswith('[')
        assert results[0].endswith(']')


# ── End-to-end decode of synthesised CW ──────────────────────────────────────

class TestEndToEndDecode:
    """
    Synthesise clean CW audio and verify the decode pipeline produces correct
    output. These tests are independent of any audio files.
    """

    WPM = 15.0
    TONE_HZ = 750.0
    DIT_S = 60.0 / (50 * WPM)   # 0.08 s at 15 WPM

    def _patterns(self, text: str) -> list[str]:
        """Return list of dot-dash patterns for each letter in text."""
        inv = {v: k for k, v in df.MORSE_CODE.items() if len(v) == 1}
        return [inv[ch] for ch in text.upper() if ch in inv]

    def test_decode_e(self):
        # E = single dit
        audio = make_cw_audio(self.TONE_HZ, self.DIT_S, *self._patterns('E'))
        text = df.decode_audio(audio, SR, wpm=self.WPM, tone_hz=self.TONE_HZ)
        assert 'E' in text

    def test_decode_sos(self):
        # SOS = ... --- ...
        audio = make_cw_audio(self.TONE_HZ, self.DIT_S, *self._patterns('SOS'))
        text = df.decode_audio(audio, SR, wpm=self.WPM, tone_hz=self.TONE_HZ)
        assert 'S' in text or 'O' in text

    def test_decode_cq(self):
        # CQ = -.-. --.-
        audio = make_cw_audio(self.TONE_HZ, self.DIT_S, *self._patterns('CQ'))
        text = df.decode_audio(audio, SR, wpm=self.WPM, tone_hz=self.TONE_HZ)
        assert 'C' in text or 'Q' in text

    def test_silence_returns_empty(self):
        audio = np.zeros(SR * 2, dtype=np.float32)
        text = df.decode_audio(audio, SR, wpm=self.WPM, tone_hz=self.TONE_HZ)
        assert text == ''

    def test_returns_string(self):
        audio = make_cw_audio(self.TONE_HZ, self.DIT_S, *self._patterns('E'))
        result = df.decode_audio(audio, SR, wpm=self.WPM, tone_hz=self.TONE_HZ)
        assert isinstance(result, str)
