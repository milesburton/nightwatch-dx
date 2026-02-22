import math
import sys
import types

import numpy as np

try:
    import aiohttp  # noqa: F401
except ImportError:
    aiohttp_stub = types.ModuleType("aiohttp")
    web_stub = types.ModuleType("aiohttp.web")
    for _attr in ['WebSocketResponse', 'WSMsgType', 'Application', 'AppRunner', 'TCPSite', 'Request']:
        setattr(web_stub, _attr, object)
    aiohttp_stub.web = web_stub
    sys.modules["aiohttp"] = aiohttp_stub
    sys.modules["aiohttp.web"] = web_stub

import os
sys.path.insert(0, os.path.dirname(__file__))

import cw_decoder as cwd  # noqa: E402

SDR_SAMPLE_RATE = cwd.SDR_SAMPLE_RATE
FREQ_OFFSET_HZ  = cwd.FREQ_OFFSET_HZ
DIT_SAMPLES     = cwd.DIT_SAMPLES
AUDIO_RATE      = cwd.AUDIO_RATE
DECIMATE        = SDR_SAMPLE_RATE // AUDIO_RATE

MORSE: dict[str, str] = {v: k for k, v in cwd.MORSE_CODE.items()}
MORSE[' '] = ' '


def make_cw_iq(text: str, amplitude: float = 0.6, noise_amplitude: float = 0.02) -> bytes:
    dit = DIT_SAMPLES * DECIMATE
    intervals: list[tuple[bool, int]] = [(False, SDR_SAMPLE_RATE)]
    first_char = True
    for ch in text.upper():
        if ch == ' ':
            intervals.append((False, dit * 4))
            first_char = True
            continue
        symbols = MORSE.get(ch)
        if symbols is None:
            continue
        if not first_char:
            intervals.append((False, dit * 3))
        first_char = False
        for j, sym in enumerate(symbols):
            if j > 0:
                intervals.append((False, dit))
            intervals.append((True, dit if sym == '.' else dit * 3))
    intervals.append((False, dit * 7))

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
        noise = rng.standard_normal(n) * noise_amplitude + 1j * rng.standard_normal(n) * noise_amplitude
        iq[idx:idx + n] = chunk + noise.astype(np.complex64)
        idx += n

    i_f = np.clip(iq.real * 127.5 + 127.5, 0, 255).astype(np.uint8)
    q_f = np.clip(iq.imag * 127.5 + 127.5, 0, 255).astype(np.uint8)
    raw = np.empty(total_samples * 2, dtype=np.uint8)
    raw[0::2] = i_f
    raw[1::2] = q_f
    return raw.tobytes()


def decode_message(text: str, chunk_size: int = 65536) -> list[dict]:
    chain = cwd.CWSignalChain()
    iq_bytes = make_cw_iq(text)
    events: list[dict] = []
    for start in range(0, len(iq_bytes), chunk_size):
        events.extend(chain.process(iq_bytes[start:start + chunk_size]))
    events.extend(chain.flush())
    return events


def events_to_text(events: list[dict]) -> str:
    result = []
    for ev in events:
        if ev['type'] == 'char':
            result.append(ev['char'])
        elif ev['type'] == 'word_space':
            result.append(' ')
    return ''.join(result).strip()


class TestMorseDecoder:
    @staticmethod
    def make_md(dit: int) -> cwd.MorseDecoder:
        md = cwd.MorseDecoder()
        md._dit_est = float(dit)
        return md

    def test_dit_decodes_as_E(self):
        dit = 720
        md = self.make_md(dit)
        md.push_tone(dit, dit)
        chars = [e['char'] for e in md.push_gap(dit * 7, dit) if e['type'] == 'char']
        assert 'E' in chars

    def test_dah_decodes_as_T(self):
        dit = 720
        md = self.make_md(dit)
        md.push_tone(dit * 3, dit)
        chars = [e['char'] for e in md.push_gap(dit * 7, dit) if e['type'] == 'char']
        assert 'T' in chars

    def test_CQ_sequence(self):
        dit = 720
        md = self.make_md(dit)

        def tone(n): md.push_tone(n, dit)
        def gap(n): return md.push_gap(n, dit)

        for sym in '-.-.' : tone(dit * 3 if sym == '-' else dit); gap(dit)  # noqa
        gap(dit * 3)
        for sym in '--.-': tone(dit * 3 if sym == '-' else dit); gap(dit)   # noqa
        chars = [e['char'] for e in gap(dit * 7) if e['type'] == 'char']
        assert 'Q' in chars

    def test_word_gap_emits_word_space_event(self):
        dit = 720
        md = self.make_md(dit)
        md.push_tone(dit, dit)
        types = [e['type'] for e in md.push_gap(dit * 7, dit)]
        assert 'word_space' in types

    def test_sub_40pct_dit_tone_is_ignored_as_noise(self):
        dit = 720
        md = self.make_md(dit)
        md.push_tone(int(dit * 0.3), dit)
        md.push_tone(dit, dit)
        chars = [e['char'] for e in md.push_gap(dit * 7, dit) if e['type'] == 'char']
        assert chars == ['E']


class TestCWSignalChain:
    def test_single_dit_decodes_as_E(self):
        chars = [e['char'] for e in decode_message('E') if e['type'] == 'char']
        assert 'E' in chars

    def test_CQ_de_contains_Q_D_E(self):
        text = events_to_text(decode_message('CQ DE'))
        assert 'Q' in text, f"Missing Q in {text!r}"
        assert 'D' in text, f"Missing D in {text!r}"
        assert 'E' in text, f"Missing E in {text!r}"

    def test_SOS_contains_S_and_O(self):
        chars = [e['char'] for e in decode_message('SOS') if e['type'] == 'char']
        assert 'S' in chars
        assert 'O' in chars

    def test_word_space_emitted_for_space_in_input(self):
        types = {e['type'] for e in decode_message('E T')}
        assert 'word_space' in types

    def test_all_decoded_chars_are_uppercase(self):
        chars = [e['char'] for e in decode_message('HELLO') if e['type'] == 'char']
        for ch in chars:
            assert ch == ch.upper() or not ch.isalpha()

    def test_small_chunk_size_still_decodes_SOS(self):
        chars = [e['char'] for e in decode_message('SOS', chunk_size=4096) if e['type'] == 'char']
        assert 'S' in chars

    def test_noise_only_produces_no_chars(self):
        rng = np.random.default_rng(0)
        noise = rng.integers(100, 155, size=SDR_SAMPLE_RATE * 2, dtype=np.uint8)
        chain = cwd.CWSignalChain()
        chars = [e for e in chain.process(noise.tobytes()) if e['type'] == 'char']
        assert len(chars) == 0, f"noise gate failed — got {len(chars)} false chars: {chars[:5]}"


class TestIQGeneration:
    def test_output_byte_count_is_even(self):
        assert len(make_cw_iq('E')) % 2 == 0

    def test_all_byte_values_are_valid_uint8(self):
        data = np.frombuffer(make_cw_iq('SOS'), dtype=np.uint8)
        assert data.min() >= 0
        assert data.max() <= 255

    def test_tone_peak_is_near_dc_after_mixing(self):
        data = make_cw_iq('T')
        preroll = SDR_SAMPLE_RATE * 2
        raw  = np.frombuffer(data[preroll:preroll + 4096 * 2], dtype=np.uint8).astype(np.float32)
        iq   = ((raw[0::2] - 127.5) + 1j * (raw[1::2] - 127.5)) / 127.5
        n    = len(iq)
        lo   = np.exp(-1j * 2 * np.pi * FREQ_OFFSET_HZ * np.arange(n) / SDR_SAMPLE_RATE)
        spectrum  = np.abs(np.fft.fftshift(np.fft.fft(iq * lo)))
        peak_freq = np.fft.fftshift(np.fft.fftfreq(n, 1 / SDR_SAMPLE_RATE))[int(np.argmax(spectrum))]
        assert abs(peak_freq) < 5000


if __name__ == '__main__':
    print("=== CW Roundtrip Smoke Test ===")
    for msg in ['E', 'SOS', 'CQ DE']:
        text = events_to_text(decode_message(msg))
        print(f"  {msg!r} → {text!r}")
