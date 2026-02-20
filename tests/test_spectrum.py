"""
Tests for the spectrum/waterfall service.

Tests pure-logic components:
- IQ conversion and even-length truncation
- FFT windowing and power calculation
- dBFS conversion
- Configuration constants
"""

import sys
import os
import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'services', 'spectrum'))
import spectrum_service as spec


# ── Configuration ─────────────────────────────────────────────────────────────

class TestConfiguration:
    def test_fft_size_power_of_two(self):
        assert spec.FFT_SIZE > 0
        assert (spec.FFT_SIZE & (spec.FFT_SIZE - 1)) == 0  # power of 2

    def test_fft_averages_positive(self):
        assert spec.FFT_AVERAGES > 0

    def test_window_length(self):
        assert len(spec._WINDOW) == spec.FFT_SIZE

    def test_window_power_positive(self):
        assert spec._WINDOW_POWER > 0

    def test_rf_center_is_14mhz(self):
        # RF centre should be in the 20m band (14.0–14.35 MHz)
        assert 14_000_000 <= spec.RF_CENTER_HZ <= 14_350_000

    def test_sample_rate(self):
        assert spec.SAMPLE_RATE == 2_400_000


# ── IQ conversion ─────────────────────────────────────────────────────────────

class TestIQConversion:
    def _u8_to_iq(self, raw: bytes) -> np.ndarray:
        u8 = np.frombuffer(raw, dtype=np.uint8)[:len(raw) & ~1].astype(np.float32)
        return ((u8[0::2] - 127.5) + 1j * (u8[1::2] - 127.5)) / 127.5

    def test_dc_offset(self):
        raw = bytes([127, 127] * 64)
        iq = self._u8_to_iq(raw)
        assert np.allclose(iq, 0, atol=0.01)

    def test_odd_length_truncated(self):
        raw = bytes(65)
        iq = self._u8_to_iq(raw)
        assert len(iq) == 32

    def test_amplitude_bounded(self):
        raw = bytes(range(256)) * 4
        iq = self._u8_to_iq(raw)
        assert np.all(np.abs(iq.real) <= 1.01)
        assert np.all(np.abs(iq.imag) <= 1.01)


# ── FFT / power spectrum ──────────────────────────────────────────────────────

class TestFFTPowerSpectrum:
    def _compute_spectrum(self, iq_segment: np.ndarray) -> np.ndarray:
        """Replicate spectrum_service FFT path."""
        windowed = iq_segment[:spec.FFT_SIZE] * spec._WINDOW
        spectrum = np.fft.fftshift(np.fft.fft(windowed, spec.FFT_SIZE))
        power = (np.abs(spectrum) ** 2) / spec._WINDOW_POWER
        return 10.0 * np.log10(np.maximum(power, 1e-12))

    def test_output_length(self):
        iq = np.zeros(spec.FFT_SIZE, dtype=np.complex64)
        db = self._compute_spectrum(iq)
        assert len(db) == spec.FFT_SIZE

    def test_silence_gives_very_low_db(self):
        iq = np.zeros(spec.FFT_SIZE, dtype=np.complex64)
        db = self._compute_spectrum(iq)
        assert np.all(db < -100)

    def test_full_scale_tone_near_zero_db(self):
        t = np.arange(spec.FFT_SIZE)
        # Full-scale tone at bin 100
        iq = np.exp(2j * np.pi * 100 * t / spec.FFT_SIZE).astype(np.complex64)
        db = self._compute_spectrum(iq)
        # Peak should be near 0 dBFS (allow for windowing loss ~1.5 dB)
        assert np.max(db) > -3.0

    def test_tone_at_correct_bin(self):
        t = np.arange(spec.FFT_SIZE)
        bin_target = 200
        iq = np.exp(2j * np.pi * bin_target * t / spec.FFT_SIZE).astype(np.complex64)
        db = self._compute_spectrum(iq)
        # fftshift moves DC to centre; positive freq bins are in the right half
        peak_bin = np.argmax(db)
        # Allow a few bins of tolerance due to windowing
        expected_bin = spec.FFT_SIZE // 2 + bin_target
        assert abs(peak_bin - expected_bin) <= 2

    def test_db_values_are_finite(self):
        iq = (np.random.randn(spec.FFT_SIZE) + 1j * np.random.randn(spec.FFT_SIZE)).astype(np.complex64)
        db = self._compute_spectrum(iq)
        assert np.all(np.isfinite(db))

    def test_db_floor_clamp(self):
        # The max(power, 1e-12) clamp means min dB = 10*log10(1e-12) = -120
        iq = np.zeros(spec.FFT_SIZE, dtype=np.complex64)
        db = self._compute_spectrum(iq)
        assert np.all(db >= -125)  # slight tolerance

    def test_averaging_reduces_noise_variance(self):
        """Averaging N spectra should reduce variance by ~1/N."""
        n = 20
        spectra = []
        for _ in range(n):
            iq = (np.random.randn(spec.FFT_SIZE) + 1j * np.random.randn(spec.FFT_SIZE)).astype(np.complex64)
            windowed = iq * spec._WINDOW
            s = np.fft.fftshift(np.fft.fft(windowed, spec.FFT_SIZE))
            spectra.append((np.abs(s) ** 2) / spec._WINDOW_POWER)

        single_var = np.var(spectra[0])
        avg_power = np.mean(spectra, axis=0)
        avg_var = np.var(avg_power)
        # Averaged variance should be substantially less than single-spectrum variance
        assert avg_var < single_var * 0.5


# ── Hann window properties ────────────────────────────────────────────────────

class TestHannWindow:
    def test_starts_and_ends_near_zero(self):
        w = spec._WINDOW
        assert abs(w[0]) < 0.01
        assert abs(w[-1]) < 0.01

    def test_peaks_near_one(self):
        assert np.max(spec._WINDOW) > 0.99

    def test_window_power_correct(self):
        expected = float(np.sum(spec._WINDOW ** 2))
        assert abs(spec._WINDOW_POWER - expected) < 1e-6
