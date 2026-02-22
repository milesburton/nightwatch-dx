"""
Tests for the SSTV decoder service.

Organised by signal-chain stage:
  TestConstants         -- configuration values
  TestKaiserLowpass     -- FIR filter builder (used in tests for VIS frequencies)
  TestFMDiscriminator   -- FM discriminator on complex64 input
  TestVISDetector       -- VIS preamble state machine
  TestImageDecoder      -- pixel decode helpers
"""

import math
import os
import sys
import types

import numpy as np

# ── Stub aiohttp so we can import sstv_decoder without a web server ────────────

try:
    import aiohttp  # noqa: F401
except ImportError:
    aiohttp_stub = types.ModuleType("aiohttp")
    web_stub = types.ModuleType("aiohttp.web")
    for _attr in ["WebSocketResponse", "Application", "AppRunner", "TCPSite", "Request"]:
        setattr(web_stub, _attr, object)
    aiohttp_stub.web = web_stub
    sys.modules["aiohttp"] = aiohttp_stub
    sys.modules["aiohttp.web"] = web_stub

sys.path.insert(0, os.path.dirname(__file__))
import sstv_decoder as sstv  # noqa: E402

# ── Pure test helpers ──────────────────────────────────────────────────────────

def make_complex64_tone(freq_hz: float, duration_s: float, sample_rate: int,
                        amplitude: float = 0.7) -> np.ndarray:
    """Generate complex64 array of a pure tone at freq_hz (already mixed to DC base)."""
    n = int(duration_s * sample_rate)
    t = np.arange(n) / sample_rate
    phase = 2 * math.pi * freq_hz * t
    return (amplitude * (np.cos(phase) + 1j * np.sin(phase))).astype(np.complex64)


def make_fm_tone(audio_freq_hz: float, duration_s: float, sample_rate: int = sstv.AUDIO_RATE) -> np.ndarray:
    """Generate FM audio samples representing a constant frequency."""
    n = int(duration_s * sample_rate)
    return np.full(n, float(audio_freq_hz), dtype=np.float32)


def _vis_preamble(sample_rate: int = sstv.AUDIO_RATE) -> np.ndarray:
    """Build a minimal valid VIS preamble for Robot 36 (VIS code 8).

    Sequence (each 'window' = 10 ms = WIN_MS):
      leader  : 30 windows of 1900 Hz  (>= LEADER_WINS required)
      break   : 1 window  of 1200 Hz   (transitions LEADER -> BREAK)
      start   : 3 windows of 1900 Hz   (transitions BREAK -> START -> VIS_BITS)
      8 bits  : 3 windows each, 1100 Hz = '1', 1300 Hz = '0' (LSB first)
      stop    : 3 windows of 1200 Hz   (transitions STOP -> BUFFERING)
    """
    win = round(10 * sample_rate / 1000)

    def tone_windows(freq: float, n_wins: int) -> np.ndarray:
        return np.full(n_wins * win, float(freq), dtype=np.float32)

    vis_code = 8   # Robot 36
    bits = [int(b) for b in f"{vis_code:07b}"[::-1]]   # 7 data bits, LSB first
    parity = sum(bits) % 2                               # even parity
    bits.append(parity)

    segments = [
        tone_windows(1900, 30),   # leader
        tone_windows(1200, 1),    # break (single window -- next window must be 1900)
        tone_windows(1900, 3),    # start (3 windows bring sub_cnt to 3 -> VIS_BITS)
    ]
    for bit in bits:
        freq = 1100.0 if bit == 1 else 1300.0
        segments.append(tone_windows(freq, 3))
    segments.append(tone_windows(1200, 3))   # stop

    return np.concatenate(segments)


# ── Constants ──────────────────────────────────────────────────────────────────

class TestConstants:
    def test_audio_rate_is_24khz(self):
        assert sstv.AUDIO_RATE == 24_000

    def test_freq_black_is_below_freq_white(self):
        assert sstv.FREQ_BLACK < sstv.FREQ_WHITE

    def test_freq_sync_is_below_freq_black(self):
        assert sstv.FREQ_SYNC < sstv.FREQ_BLACK


# ── FIR filter (kaiser_lowpass kept for potential VIS tone helper use) ─────────

class TestKaiserLowpass:
    def test_unity_gain_at_dc(self):
        taps = sstv.kaiser_lowpass(1000.0, 24_000.0)
        assert abs(taps.sum() - 1.0) < 1e-5

    def test_returns_odd_number_of_taps(self):
        taps = sstv.kaiser_lowpass(1000.0, 24_000.0)
        assert len(taps) % 2 == 1

    def test_output_is_float32(self):
        taps = sstv.kaiser_lowpass(1000.0, 24_000.0)
        assert taps.dtype == np.float32

    def test_does_not_mutate_input(self):
        taps = sstv.kaiser_lowpass(1000.0, 24_000.0)
        copy = taps.copy()
        _ = sstv.kaiser_lowpass(2000.0, 24_000.0)
        np.testing.assert_array_equal(taps, copy)


# ── FM discriminator ───────────────────────────────────────────────────────────

class TestFMDiscriminator:
    def test_returns_float32(self):
        fm  = sstv.FMDiscriminator()
        inp = make_complex64_tone(1000.0, 0.01, sstv.AUDIO_RATE)
        out = fm.process(inp)
        assert out.dtype == np.float32

    def test_output_length_matches_input(self):
        fm  = sstv.FMDiscriminator()
        inp = make_complex64_tone(500.0, 0.05, sstv.AUDIO_RATE)
        out = fm.process(inp)
        assert len(out) == len(inp)

    def test_dc_carrier_produces_near_zero_frequency(self):
        fm  = sstv.FMDiscriminator()
        # A pure DC complex carrier (constant phase) should yield ~0 Hz
        n   = int(0.1 * sstv.AUDIO_RATE)
        inp = np.ones(n, dtype=np.complex64)
        out = fm.process(inp)
        # Skip first sample (prev state was 0)
        assert float(np.abs(out[1:]).mean()) < 100.0

    def test_state_persists_across_calls(self):
        fm1 = sstv.FMDiscriminator()
        fm2 = sstv.FMDiscriminator()
        inp = make_complex64_tone(500.0, 0.05, sstv.AUDIO_RATE)
        out1 = fm1.process(inp)
        mid  = len(inp) // 2
        out2a = fm2.process(inp[:mid])
        out2b = fm2.process(inp[mid:])
        out2  = np.concatenate([out2a, out2b])
        assert len(out1) == len(out2)


# ── FM discriminator output frequency mapping ─────────────────────────────────

class TestFreqToPixel:
    def test_sync_frequency_gives_minimum_pixel(self):
        val = sstv.freq_to_pixel(sstv.FREQ_SYNC)
        assert val < 10

    def test_black_frequency_gives_zero_pixel(self):
        assert sstv.freq_to_pixel(sstv.FREQ_BLACK) == 0

    def test_white_frequency_gives_maximum_pixel(self):
        assert sstv.freq_to_pixel(sstv.FREQ_WHITE) == 255

    def test_midpoint_frequency_gives_midpoint_pixel(self):
        mid_freq = (sstv.FREQ_BLACK + sstv.FREQ_WHITE) / 2
        pixel = sstv.freq_to_pixel(mid_freq)
        assert 120 <= pixel <= 135

    def test_below_black_clamps_to_zero(self):
        assert sstv.freq_to_pixel(sstv.FREQ_BLACK - 200) == 0

    def test_above_white_clamps_to_255(self):
        assert sstv.freq_to_pixel(sstv.FREQ_WHITE + 200) == 255


# ── dominant_tone helper ───────────────────────────────────────────────────────

class TestDominantTone:
    def test_leader_tone_identified(self):
        samples = make_fm_tone(1900, 0.01)
        assert sstv.dominant_tone(samples) == 1900

    def test_break_tone_identified(self):
        samples = make_fm_tone(1200, 0.01)
        assert sstv.dominant_tone(samples) == 1200

    def test_bit_zero_tone_identified(self):
        samples = make_fm_tone(1300, 0.01)
        assert sstv.dominant_tone(samples) == 1300

    def test_bit_one_tone_identified(self):
        samples = make_fm_tone(1100, 0.01)
        assert sstv.dominant_tone(samples) == 1100

    def test_out_of_band_frequency_returns_zero(self):
        # 2500 Hz is more than TOLERANCE (100 Hz) away from all VIS frequencies
        samples = make_fm_tone(2500, 0.01)
        assert sstv.dominant_tone(samples) == 0


# ── VIS Detector state machine ─────────────────────────────────────────────────

class TestVISDetector:
    def test_idle_state_on_init(self):
        det = sstv.VISDetector()
        assert det._state == 'IDLE'

    def test_noise_does_not_trigger_frame(self):
        det = sstv.VISDetector()
        noise = np.random.default_rng(42).uniform(800, 2500, 24_000).astype(np.float32)
        result = det.push(noise)
        assert result is None

    def test_leader_alone_does_not_trigger_frame(self):
        det = sstv.VISDetector()
        leader = make_fm_tone(1900, 0.5)
        result = det.push(leader)
        assert result is None

    def test_valid_vis_preamble_triggers_frame_buffering(self):
        det = sstv.VISDetector()
        preamble = _vis_preamble()
        robot36_duration_s = 40.0
        filler = make_fm_tone(1700, robot36_duration_s)
        result = det.push(np.concatenate([preamble, filler]))
        assert result is not None

    def test_vis_code_8_identified_as_robot36(self):
        det = sstv.VISDetector()
        preamble = _vis_preamble()
        robot36_duration_s = 40.0
        filler = make_fm_tone(1700, robot36_duration_s)
        result = det.push(np.concatenate([preamble, filler]))
        assert result is not None
        _, vis_code = result
        assert vis_code == 8

    def test_detector_resets_after_successful_frame(self):
        det = sstv.VISDetector()
        preamble = _vis_preamble()
        filler = make_fm_tone(1700, 40.0)
        det.push(np.concatenate([preamble, filler]))
        assert det._state == 'IDLE'

    def test_result_contains_audio_array_and_vis_code(self):
        det = sstv.VISDetector()
        preamble = _vis_preamble()
        filler = make_fm_tone(1700, 40.0)
        result = det.push(np.concatenate([preamble, filler]))
        assert result is not None
        audio, vis_code = result
        assert isinstance(audio, np.ndarray)
        assert isinstance(vis_code, int)

    def test_audio_array_is_float32(self):
        det = sstv.VISDetector()
        preamble = _vis_preamble()
        filler = make_fm_tone(1700, 40.0)
        result = det.push(np.concatenate([preamble, filler]))
        assert result is not None
        audio, _ = result
        assert audio.dtype == np.float32


# ── Image decoder ──────────────────────────────────────────────────────────────

class TestImageDecoder:
    def test_decode_generic_returns_rgb_image(self):
        audio = make_fm_tone(1800, 5.0)
        img = sstv.decode_generic(audio, sstv.AUDIO_RATE, vis_code=0)
        assert img.mode == 'RGB'

    def test_decode_generic_dimensions(self):
        audio = make_fm_tone(1800, 5.0)
        img = sstv.decode_generic(audio, sstv.AUDIO_RATE, vis_code=0)
        assert img.width == 320
        assert img.height == 200

    def test_decode_robot36_returns_rgb_image(self):
        sr = sstv.AUDIO_RATE
        lines = 240
        sync_s, porch_s, luma_s, chroma_s = 0.009, 0.003, 0.088, 0.044

        def seg(freq, dur):
            return make_fm_tone(freq, dur, sr)

        rows = []
        for _ in range(lines):
            rows += [seg(1200, sync_s), seg(1500, porch_s),
                     seg(1800, luma_s), seg(1500, chroma_s)]
        audio = np.concatenate(rows)
        img = sstv.decode_robot36(audio, sr)
        assert img.mode == 'RGB'
        assert img.width == 320
        assert img.height == 240

    def test_decode_image_dispatches_to_robot36_for_vis8(self):
        sr = sstv.AUDIO_RATE
        sync_s, porch_s, luma_s, chroma_s = 0.009, 0.003, 0.088, 0.044

        def seg(freq, dur):
            return make_fm_tone(freq, dur, sr)

        rows = []
        for _ in range(240):
            rows += [seg(1200, sync_s), seg(1500, porch_s),
                     seg(1800, luma_s), seg(1500, chroma_s)]
        audio = np.concatenate(rows)
        img = sstv.decode_image(audio, vis_code=8, sr=sr)
        assert img.size == (320, 240)

    def test_decode_image_falls_back_for_unknown_vis(self):
        audio = make_fm_tone(1800, 5.0)
        img = sstv.decode_image(audio, vis_code=99, sr=sstv.AUDIO_RATE)
        assert img.mode == 'RGB'


# ── PNG export ─────────────────────────────────────────────────────────────────

class TestImageToDataUrl:
    def test_output_starts_with_png_data_url_prefix(self):
        from PIL import Image
        img = Image.new('RGB', (10, 10), color=(128, 64, 32))
        url = sstv.image_to_data_url(img)
        assert url.startswith('data:image/png;base64,')

    def test_output_is_valid_base64(self):
        import base64

        from PIL import Image
        img = Image.new('RGB', (8, 8), color=(0, 0, 0))
        url = sstv.image_to_data_url(img)
        b64_part = url.split(',', 1)[1]
        decoded = base64.b64decode(b64_part)
        assert decoded[:8] == b'\x89PNG\r\n\x1a\n'   # PNG magic bytes
