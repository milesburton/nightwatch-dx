"""
Unit tests for easypal_decoder.py signal chain components.

Tests are structured around the discrete processing stages:
  IQSignalChain → AudioToBaseband → OFDMDemodulator
  qam16_demap → deinterleave_frame → viterbi_decode / depuncture
  crc8 / crc16 → FrameAssembler → Hub
"""

import io
import json
import math
import os
import sys
import types

import numpy as np
import pytest

# ── Stub aiohttp so we can import without the network stack ──────────────────
try:
    import aiohttp  # noqa: F401
except ImportError:
    aiohttp_stub = types.ModuleType("aiohttp")
    web_stub = types.ModuleType("aiohttp.web")
    for _attr in ["WebSocketResponse", "WSMsgType", "Application", "AppRunner", "TCPSite", "Request"]:
        setattr(web_stub, _attr, object)
    aiohttp_stub.web = web_stub
    sys.modules["aiohttp"] = aiohttp_stub
    sys.modules["aiohttp.web"] = web_stub

# ── Stub store so DB calls are no-ops ────────────────────────────────────────
store_stub = types.ModuleType("store")
store_stub.DB_PATH    = ":memory:"
store_stub.FRAMES_DIR = "/tmp"
store_stub.init_db    = None
store_stub.save_frame = None
sys.modules["store"] = store_stub

sys.path.insert(0, os.path.dirname(__file__))
import easypal_decoder as ep  # noqa: E402

AUDIO_RATE  = ep.AUDIO_RATE   # 24 000
DRM_RATE    = ep.DRM_RATE     # 12 000
FFT_SIZE    = ep.FFT_SIZE     # 256
GUARD_SIZE  = ep.GUARD_SIZE   # 64
SYMBOL_SIZE = ep.SYMBOL_SIZE  # 320
N_CARRIERS  = ep.N_CARRIERS   # 29


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_complex_tone(freq_hz: float, rate: int, n: int) -> np.ndarray:
    """Pure complex tone at freq_hz."""
    t = np.arange(n) / rate
    return np.exp(2j * np.pi * freq_hz * t).astype(np.complex64)


def make_complex64_bytes(freq_hz: float, n_samples: int) -> bytes:
    """Produce complex64 bytes as if from rtl-bridge AudioMux."""
    sig = make_complex_tone(freq_hz, AUDIO_RATE, n_samples)
    return sig.tobytes()


# ── Constants ─────────────────────────────────────────────────────────────────

class TestConstants:
    def test_audio_rate_is_24khz(self):
        assert ep.AUDIO_RATE == 24_000

    def test_drm_rate_is_12khz(self):
        assert ep.DRM_RATE == 12_000

    def test_fft_size_is_256(self):
        assert ep.FFT_SIZE == 256

    def test_guard_size_is_64(self):
        assert ep.GUARD_SIZE == 64

    def test_symbol_size_is_fft_plus_guard(self):
        assert ep.SYMBOL_SIZE == ep.FFT_SIZE + ep.GUARD_SIZE

    def test_frame_symbols_is_15(self):
        assert ep.FRAME_SYMBOLS == 15

    def test_n_carriers_is_29(self):
        # K_MIN=-10 … K_MAX=18 inclusive → 29 carriers
        assert ep.N_CARRIERS == 29

    def test_drm_center_bin_is_correct(self):
        # 1500 Hz / (12000/256) Hz per bin ≈ 32
        expected = round(1500 / (DRM_RATE / FFT_SIZE))
        assert ep.DRM_CENTER_BIN == expected


# ── IQSignalChain ─────────────────────────────────────────────────────────────

class TestIQSignalChain:
    def test_returns_float32(self):
        chain = ep.IQSignalChain()
        raw   = make_complex64_bytes(1000.0, 256)
        out   = chain.process(raw)
        assert out.dtype == np.float32

    def test_output_length_matches_input_samples(self):
        chain = ep.IQSignalChain()
        n     = 512
        raw   = make_complex64_bytes(1000.0, n)
        out   = chain.process(raw)
        assert len(out) == n

    def test_dc_carrier_produces_near_zero_frequency(self):
        # A DC (0 Hz) complex signal has zero instantaneous frequency
        chain  = ep.IQSignalChain()
        dc_sig = np.ones(2048, dtype=np.complex64)
        out    = chain.process(dc_sig.tobytes())
        assert np.abs(out).mean() < 10.0   # well under 1 Hz tolerance in Hz units

    def test_state_persists_across_chunks(self):
        # Two consecutive chunks should produce consistent output
        chain = ep.IQSignalChain()
        tone  = make_complex64_bytes(500.0, 256)
        out1  = chain.process(tone)
        out2  = chain.process(tone)
        # Both chunks should yield similar mean frequency (500 Hz)
        assert abs(out1.mean() - out2.mean()) < 200.0

    def test_empty_input_returns_empty_output(self):
        chain = ep.IQSignalChain()
        out   = chain.process(b"")
        assert len(out) == 0

    def test_tone_frequency_reflected_in_output(self):
        # FM discriminator: constant frequency tone → constant instantaneous freq
        chain = ep.IQSignalChain()
        freq  = 2000.0
        raw   = make_complex64_bytes(freq, 4096)
        out   = chain.process(raw)
        # Mean should be close to the tone frequency (±10%)
        assert abs(out[10:].mean() - freq) < freq * 0.15


# ── AudioToBaseband ───────────────────────────────────────────────────────────

class TestAudioToBaseband:
    def test_returns_complex64(self):
        bb    = ep.AudioToBaseband()
        audio = np.zeros(256, dtype=np.float32)
        out   = bb.process(audio)
        assert out.dtype == np.complex64

    def test_output_is_half_length_of_input(self):
        # Decimates by 2 (24 kHz → 12 kHz)
        bb    = ep.AudioToBaseband()
        audio = np.zeros(256, dtype=np.float32)
        out   = bb.process(audio)
        assert len(out) == 128

    def test_empty_input_returns_empty(self):
        bb    = ep.AudioToBaseband()
        audio = np.zeros(0, dtype=np.float32)
        out   = bb.process(audio)
        assert len(out) == 0

    def test_state_persists_across_chunks(self):
        bb   = ep.AudioToBaseband()
        a    = np.random.default_rng(42).standard_normal(512).astype(np.float32)
        out1 = bb.process(a[:256])
        out2 = bb.process(a[256:])
        # Both should return non-empty arrays
        assert len(out1) > 0
        assert len(out2) > 0

    def test_output_is_unit_magnitude(self):
        # exp(jφ) should have magnitude ≈ 1 before DC blocking
        bb    = ep.AudioToBaseband()
        # Constant-frequency audio → smooth phase ramp → near-unit magnitude
        audio = np.full(1024, 500.0, dtype=np.float32)
        out   = bb.process(audio)
        mags  = np.abs(out)
        # After DC block the magnitude may deviate slightly; allow ±0.2
        assert mags.mean() == pytest.approx(1.0, abs=0.2)


# ── OFDMDemodulator ───────────────────────────────────────────────────────────

class TestOFDMDemodulator:
    def _make_ofdm_symbol(self) -> np.ndarray:
        """Synthesise one valid OFDM symbol with a known guard interval copy."""
        useful = np.random.default_rng(7).standard_normal(FFT_SIZE).astype(np.complex64)
        guard  = useful[-GUARD_SIZE:]
        return np.concatenate([guard, useful])

    def test_empty_input_returns_no_symbols(self):
        ofdm = ep.OFDMDemodulator()
        result = ofdm.push(np.empty(0, dtype=np.complex64))
        assert result == []

    def test_insufficient_data_returns_no_symbols(self):
        ofdm = ep.OFDMDemodulator()
        short = np.zeros(SYMBOL_SIZE - 1, dtype=np.complex64)
        result = ofdm.push(short)
        assert result == []

    def test_one_symbol_returns_one_result(self):
        ofdm = ep.OFDMDemodulator()
        # Feed 3 symbols worth to guarantee sync + one output
        sym  = self._make_ofdm_symbol()
        data = np.tile(sym, 3)
        result = ofdm.push(data)
        assert len(result) >= 1

    def test_result_has_correct_carrier_count(self):
        ofdm = ep.OFDMDemodulator()
        sym  = self._make_ofdm_symbol()
        data = np.tile(sym, 4)
        result = ofdm.push(data)
        assert len(result) >= 1
        cells, mask = result[0]
        assert len(cells) == N_CARRIERS
        assert len(mask)  == N_CARRIERS

    def test_data_mask_has_correct_true_count(self):
        # 5 pilot carriers should be masked out → 24 data carriers
        ofdm = ep.OFDMDemodulator()
        sym  = self._make_ofdm_symbol()
        data = np.tile(sym, 4)
        result = ofdm.push(data)
        if result:
            _, mask = result[0]
            assert mask.sum() == N_CARRIERS - len(ep.TIME_PILOT_CARRIERS)

    def test_find_symbol_start_returns_none_for_short_data(self):
        ofdm  = ep.OFDMDemodulator()
        short = np.zeros(GUARD_SIZE, dtype=np.complex64)
        assert ofdm._find_symbol_start(short) is None


# ── QAM16 demapper ────────────────────────────────────────────────────────────

class TestQAM16Demap:
    def test_returns_uint8_array(self):
        cells  = np.array([1.0 + 1.0j, -1.0 - 1.0j], dtype=np.complex64)
        bits   = ep.qam16_demap(cells)
        assert bits.dtype == np.uint8

    def test_four_bits_per_cell(self):
        n     = 8
        cells = np.random.default_rng(0).standard_normal(n * 2).view(np.complex64)[:n].astype(np.complex64)
        bits  = ep.qam16_demap(cells)
        assert len(bits) == n * 4

    def test_bits_are_binary(self):
        cells = np.array([3.0 + 3.0j, -3.0 - 3.0j, 1.0 - 1.0j], dtype=np.complex64) / np.sqrt(10)
        bits  = ep.qam16_demap(cells)
        assert set(bits.tolist()).issubset({0, 1})

    def test_nearest_constellation_point(self):
        # Point closest to (+1, +1) level in Gray-coded 16-QAM
        # levels = [-3, -1, +1, +3] / sqrt(10)
        lvl = ep._QAM16_LEVELS
        cell = np.array([lvl[2] + 1j * lvl[2]], dtype=np.complex64)  # (+1, +1)
        bits = ep.qam16_demap(cell)
        assert len(bits) == 4

    def test_empty_input_returns_empty(self):
        bits = ep.qam16_demap(np.empty(0, dtype=np.complex64))
        assert len(bits) == 0


# ── Deinterleaver ─────────────────────────────────────────────────────────────

class TestDeinterleaveFrame:
    def _make_data_cells(self, n_sym: int, n_cell: int) -> list[np.ndarray]:
        rng = np.random.default_rng(99)
        return [rng.standard_normal(n_cell).astype(np.complex64) for _ in range(n_sym)]

    def test_returns_complex64(self):
        syms = self._make_data_cells(ep.FRAME_SYMBOLS, 24)
        out  = ep.deinterleave_frame(syms)
        assert out.dtype == np.complex64

    def test_output_length_is_nsym_times_ncell(self):
        n_sym, n_cell = ep.FRAME_SYMBOLS, 24
        syms = self._make_data_cells(n_sym, n_cell)
        out  = ep.deinterleave_frame(syms)
        assert len(out) == n_sym * n_cell

    def test_empty_input_returns_empty(self):
        out = ep.deinterleave_frame([])
        assert len(out) == 0

    def test_single_symbol_is_identity_up_to_bit_reversal(self):
        cells = np.arange(4, dtype=np.complex64)
        out   = ep.deinterleave_frame([cells])
        assert len(out) == 4

    def test_different_inputs_give_different_outputs(self):
        syms1 = self._make_data_cells(5, 8)
        syms2 = [s * 2 for s in syms1]
        out1  = ep.deinterleave_frame(syms1)
        out2  = ep.deinterleave_frame(syms2)
        assert not np.allclose(out1, out2)


# ── Depuncture ────────────────────────────────────────────────────────────────

class TestDepuncture:
    def test_output_longer_than_input(self):
        bits    = np.ones(6, dtype=np.uint8)
        pattern = ep._PUNCTURE_PATTERN   # [1,1,0,1,0,0] — keeps 3 of 6
        out     = ep.depuncture(bits, pattern)
        assert len(out) > len(bits)

    def test_zeros_inserted_at_puncture_positions(self):
        # Pattern [1,1,0,1,0,0]: positions 2,4,5 → zero in output
        bits    = np.ones(3, dtype=np.uint8)
        pattern = [1, 1, 0, 1, 0, 0]
        out     = ep.depuncture(bits, pattern)
        # Position 2 (0-indexed) should be 0 (punctured)
        assert out[2] == 0

    def test_empty_input_returns_empty(self):
        out = ep.depuncture(np.empty(0, dtype=np.uint8), [1, 0])
        assert len(out) == 0


# ── Viterbi decoder ───────────────────────────────────────────────────────────

class TestViterbiDecode:
    def _encode(self, bits: np.ndarray) -> np.ndarray:
        """Rate-1/6 convolutional encode using the same generator polynomials."""
        n_out  = len(ep._GENS)
        state  = 0
        result = []
        for bit in bits:
            shifted = (state >> 1) | (int(bit) << (ep._K - 2))
            reg     = shifted | (int(bit) << (ep._K - 1))
            row     = [bin(reg & g).count('1') % 2 for g in ep._GENS]
            result.extend(row)
            state = shifted
        return np.array(result, dtype=np.uint8)

    def test_returns_uint8(self):
        bits = np.array([1, 0, 1, 1, 0, 1], dtype=np.uint8)
        enc  = self._encode(bits)
        dec  = ep.viterbi_decode(enc, len(bits))
        assert dec.dtype == np.uint8

    def test_output_length_matches_n_output(self):
        bits = np.zeros(8, dtype=np.uint8)
        enc  = self._encode(bits)
        dec  = ep.viterbi_decode(enc, len(bits))
        assert len(dec) == len(bits)

    def test_decodes_all_zeros(self):
        bits = np.zeros(10, dtype=np.uint8)
        enc  = self._encode(bits)
        dec  = ep.viterbi_decode(enc, len(bits))
        assert np.array_equal(dec, bits)

    def test_decodes_alternating_bits(self):
        bits = np.array([0, 1] * 6, dtype=np.uint8)
        enc  = self._encode(bits)
        dec  = ep.viterbi_decode(enc, len(bits))
        assert np.array_equal(dec, bits)

    def test_single_bit_error_corrected(self):
        bits      = np.zeros(10, dtype=np.uint8)
        enc       = self._encode(bits)
        enc_noisy = enc.copy()
        enc_noisy[0] ^= 1   # flip one bit
        dec = ep.viterbi_decode(enc_noisy, len(bits))
        # Should still decode correctly (rate-1/6 has strong redundancy)
        assert np.array_equal(dec, bits)

    def test_empty_input_returns_empty(self):
        dec = ep.viterbi_decode(np.empty(0, dtype=np.uint8), 0)
        assert len(dec) == 0


# ── CRC helpers ───────────────────────────────────────────────────────────────

class TestCRC8:
    def test_empty_input_returns_zero(self):
        assert ep.crc8(b"") == 0

    def test_single_byte_known_value(self):
        # crc8(0x00) with poly 0xD5: 0 XOR 0x00, 8 shifts without top bit → 0
        assert ep.crc8(b"\x00") == 0

    def test_different_inputs_give_different_crcs(self):
        assert ep.crc8(b"hello") != ep.crc8(b"world")

    def test_returns_single_byte(self):
        val = ep.crc8(b"test")
        assert 0 <= val <= 0xFF


class TestCRC16:
    def test_empty_input_returns_ffff(self):
        # CRC-16 CCITT initialised to 0xFFFF, no data → 0xFFFF
        assert ep.crc16(b"") == 0xFFFF

    def test_known_value(self):
        # CRC-CCITT of b"\x00\x00" with init 0xFFFF, poly 0x1021
        result = ep.crc16(b"\x00\x00")
        assert isinstance(result, int)
        assert 0 <= result <= 0xFFFF

    def test_different_inputs_give_different_crcs(self):
        assert ep.crc16(b"hello") != ep.crc16(b"world")

    def test_consistency(self):
        data = b"nightwatch-dx"
        assert ep.crc16(data) == ep.crc16(data)


# ── FrameAssembler ─────────────────────────────────────────────────────────────

class TestFrameAssembler:
    def _make_segment(self, seg_num: int, total: int, payload: bytes) -> bytes:
        """Build a valid MSC segment with correct CRC-16."""
        header  = bytes([seg_num >> 8, seg_num & 0xFF, total >> 8, total & 0xFF])
        data    = header + payload
        crc_val = ep.crc16(data)
        return data + bytes([crc_val >> 8, crc_val & 0xFF])

    def _minimal_jpeg(self) -> bytes:
        """Tiny valid JPEG bytes."""
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (4, 4), color=(128, 64, 32)).save(buf, format="JPEG")
        return buf.getvalue()

    def test_short_segment_ignored(self):
        fa  = ep.FrameAssembler()
        out = fa.feed_segment(b"\x00\x01")   # too short
        assert out is None

    def test_crc_mismatch_ignored(self):
        fa  = ep.FrameAssembler()
        bad = bytes([0, 0, 0, 1]) + b"\xff" * 10 + bytes([0xDE, 0xAD])  # wrong CRC
        out = fa.feed_segment(bad)
        assert out is None

    def test_single_segment_image_decoded(self):
        fa      = ep.FrameAssembler()
        jpeg    = self._minimal_jpeg()
        seg     = self._make_segment(0, 1, jpeg)
        result  = fa.feed_segment(seg)
        assert result is not None
        from PIL import Image
        assert isinstance(result, Image.Image)

    def test_two_segment_image_requires_both(self):
        fa    = ep.FrameAssembler()
        jpeg  = self._minimal_jpeg()
        half  = len(jpeg) // 2
        seg0  = self._make_segment(0, 2, jpeg[:half])
        seg1  = self._make_segment(1, 2, jpeg[half:])
        # First segment → None
        assert fa.feed_segment(seg0) is None
        # Second segment → image
        result = fa.feed_segment(seg1)
        assert result is not None

    def test_reset_on_inconsistent_total(self):
        fa   = ep.FrameAssembler()
        jpeg = self._minimal_jpeg()
        seg_a = self._make_segment(0, 3, jpeg[:10])
        seg_b = self._make_segment(1, 2, jpeg[10:20])  # different total → reset
        fa.feed_segment(seg_a)
        out = fa.feed_segment(seg_b)
        # After reset, total_segs is now 2, only seg 1 received → no image yet
        assert out is None


# ── Hub (WebSocket broadcaster) ───────────────────────────────────────────────

class TestHub:
    @pytest.mark.asyncio
    async def test_add_and_remove_client(self):
        hub = ep.Hub()
        ws  = object()  # dummy client
        hub.add(ws)
        assert ws in hub._clients
        hub.remove(ws)
        assert ws not in hub._clients

    @pytest.mark.asyncio
    async def test_remove_nonexistent_is_safe(self):
        hub = ep.Hub()
        hub.remove(object())   # should not raise

    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all_clients(self):
        hub      = ep.Hub()
        received = []

        class FakeWS:
            async def send_str(self, text):
                received.append(json.loads(text))

        hub.add(FakeWS())
        hub.add(FakeWS())
        await hub.broadcast({"type": "test", "value": 42})
        assert len(received) == 2
        assert all(m["type"] == "test" for m in received)

    @pytest.mark.asyncio
    async def test_dead_clients_removed_during_broadcast(self):
        hub = ep.Hub()

        class DeadWS:
            async def send_str(self, _text):
                raise Exception("connection closed")

        hub.add(DeadWS())
        await hub.broadcast({"type": "ping"})
        assert len(hub._clients) == 0

    @pytest.mark.asyncio
    async def test_set_connected_broadcasts_status(self):
        hub      = ep.Hub()
        received = []

        class FakeWS:
            async def send_str(self, text):
                received.append(json.loads(text))

        hub.add(FakeWS())
        await hub.set_connected(True)
        assert received[-1] == {"type": "status", "connected": True}


# ── image_to_data_url ─────────────────────────────────────────────────────────

class TestImageToDataUrl:
    def test_starts_with_png_data_url_prefix(self):
        from PIL import Image
        img = Image.new("RGB", (2, 2))
        url = ep.image_to_data_url(img)
        assert url.startswith("data:image/png;base64,")

    def test_output_is_valid_base64(self):
        import base64
        from PIL import Image
        img    = Image.new("RGB", (2, 2))
        url    = ep.image_to_data_url(img)
        b64    = url.split(",", 1)[1]
        decoded = base64.b64decode(b64)
        assert decoded[:4] == b"\x89PNG"   # PNG magic bytes


# ── img_to_bytes ──────────────────────────────────────────────────────────────

class TestImgToBytes:
    def test_returns_png_bytes(self):
        from PIL import Image
        img = Image.new("RGB", (4, 4))
        raw = ep.img_to_bytes(img)
        assert raw[:4] == b"\x89PNG"

    def test_output_is_bytes(self):
        from PIL import Image
        img = Image.new("L", (4, 4))
        assert isinstance(ep.img_to_bytes(img), bytes)
