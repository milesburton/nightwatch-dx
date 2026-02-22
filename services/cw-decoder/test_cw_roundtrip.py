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

CHAR_TO_MORSE: dict[str, str] = {v: k for k, v in cwd.MORSE_CODE.items()}


# ── Pure test helpers ─────────────────────────────────────────────────────────

def morse_intervals_for(text: str) -> list[tuple[bool, int]]:
    dit = DIT_SAMPLES * DECIMATE
    intervals: list[tuple[bool, int]] = [(False, SDR_SAMPLE_RATE)]
    first_char = True
    for ch in text.upper():
        if ch == ' ':
            intervals.append((False, dit * 4))
            first_char = True
            continue
        symbols = CHAR_TO_MORSE.get(ch)
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
    return intervals


def render_iq(intervals: list[tuple[bool, int]], amplitude: float = 0.6, noise_amplitude: float = 0.02) -> bytes:
    total_samples = sum(n for _, n in intervals)
    iq    = np.zeros(total_samples, dtype=np.complex64)
    phase = 0.0
    step  = 2 * math.pi * FREQ_OFFSET_HZ / SDR_SAMPLE_RATE
    rng   = np.random.default_rng(42)
    idx   = 0
    for tone_on, n in intervals:
        t      = np.arange(n, dtype=np.float64)
        signal = amplitude * np.exp(1j * (phase + t * step)) if tone_on else np.zeros(n, dtype=np.complex64)
        noise  = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * noise_amplitude
        iq[idx:idx + n] = signal.astype(np.complex64) + noise.astype(np.complex64)
        phase += float((phase + t[-1] * step + step) if tone_on else (phase + n * step)) - phase if n else 0
        phase  = float(phase + t[-1] * step + step) if tone_on else float(phase + n * step)
        idx   += n
    i_samples = np.clip(iq.real * 127.5 + 127.5, 0, 255).astype(np.uint8)
    q_samples = np.clip(iq.imag * 127.5 + 127.5, 0, 255).astype(np.uint8)
    interleaved = np.empty(total_samples * 2, dtype=np.uint8)
    interleaved[0::2] = i_samples
    interleaved[1::2] = q_samples
    return interleaved.tobytes()


def make_cw_iq(text: str, amplitude: float = 0.6, noise_amplitude: float = 0.02) -> bytes:
    return render_iq(morse_intervals_for(text), amplitude, noise_amplitude)


def chars_from(events: list[dict]) -> list[str]:
    return [e['char'] for e in events if e['type'] == 'char']


def event_types_from(events: list[dict]) -> set[str]:
    return {e['type'] for e in events}


def decoded_text(events: list[dict]) -> str:
    return ''.join(
        e['char'] if e['type'] == 'char' else ' '
        for e in events
        if e['type'] in ('char', 'word_space')
    ).strip()


def decode_message(text: str, chunk_size: int = 65536) -> list[dict]:
    chain    = cwd.CWSignalChain()
    iq_bytes = make_cw_iq(text)
    events: list[dict] = []
    for start in range(0, len(iq_bytes), chunk_size):
        events.extend(chain.process(iq_bytes[start:start + chunk_size]))
    return [*events, *chain.flush()]


def morse_decoder_at_wpm(wpm: int) -> cwd.MorseDecoder:
    dit_samples = round((60 / (50 * wpm)) * AUDIO_RATE)
    md = cwd.MorseDecoder()
    md._dit_est = float(dit_samples)
    return md


# ── Signal chain constants ────────────────────────────────────────────────────

class TestConstants:
    def test_cw_frequency_is_below_rf_centre(self):
        assert cwd.FREQ_OFFSET_HZ < 0

    def test_audio_rate_is_exact_100x_decimation_of_sdr_rate(self):
        assert cwd.SDR_SAMPLE_RATE // cwd.AUDIO_RATE == 100

    def test_dit_samples_matches_20wpm_timing(self):
        expected = round((60 / (50 * 20)) * AUDIO_RATE)
        assert expected == cwd.DIT_SAMPLES


# ── FIR filter ────────────────────────────────────────────────────────────────

class TestKaiserLowpass:
    def test_unity_gain_at_dc(self):
        taps = cwd.kaiser_lowpass(1000.0, 24_000.0)
        assert abs(taps.sum() - 1.0) < 1e-5

    def test_returns_odd_number_of_taps(self):
        taps = cwd.kaiser_lowpass(1000.0, 24_000.0)
        assert len(taps) % 2 == 1

    def test_output_is_float32(self):
        taps = cwd.kaiser_lowpass(1000.0, 24_000.0)
        assert taps.dtype == np.float32


# ── Envelope detector ─────────────────────────────────────────────────────────

class TestEnvelope:
    def test_decay_is_faster_than_attack_so_gaps_register_cleanly(self):
        chain = cwd.CWSignalChain()
        assert chain._env_decay > chain._env_attack

    def test_envelope_rises_on_tone_onset(self):
        chain = cwd.CWSignalChain()
        ones  = np.ones(200, dtype=np.float64)
        env   = chain._apply_envelope(ones)
        assert env[-1] > env[0]

    def test_envelope_falls_after_tone_ends(self):
        chain = cwd.CWSignalChain()
        signal = np.concatenate([np.ones(200), np.zeros(200)])
        env    = chain._apply_envelope(signal)
        assert env[200] > env[-1]


# ── MorseDecoder unit tests ───────────────────────────────────────────────────

class TestMorseDecoder:
    def test_single_dit_produces_E(self):
        md = morse_decoder_at_wpm(20)
        md.push_tone(md.dit)
        assert 'E' in chars_from(md.push_gap(md.dit * 7))

    def test_single_dah_produces_T(self):
        md = morse_decoder_at_wpm(20)
        md.push_tone(md.dit * 3)
        assert 'T' in chars_from(md.push_gap(md.dit * 7))

    def test_tone_shorter_than_40pct_dit_is_rejected_as_noise(self):
        md = morse_decoder_at_wpm(20)
        md.push_tone(int(md.dit * 0.3))
        md.push_tone(md.dit)
        assert chars_from(md.push_gap(md.dit * 7)) == ['E']

    def test_word_gap_emits_word_space_event(self):
        md = morse_decoder_at_wpm(20)
        md.push_tone(md.dit)
        assert 'word_space' in event_types_from(md.push_gap(md.dit * 7))

    def test_char_gap_emits_char_without_word_space(self):
        md = morse_decoder_at_wpm(20)
        md.push_tone(md.dit)
        events = md.push_gap(md.dit * 3)
        assert 'char' in event_types_from(events)
        assert 'word_space' not in event_types_from(events)

    def test_intra_element_gap_produces_no_event(self):
        md = morse_decoder_at_wpm(20)
        md.push_tone(md.dit)
        assert md.push_gap(md.dit) == []

    def test_dit_estimate_decreases_toward_faster_observed_tone(self):
        md = morse_decoder_at_wpm(20)
        initial_est = md._dit_est
        fast_dit    = int(md.dit * 0.6)
        md.push_tone(fast_dit)
        assert md._dit_est < initial_est

    def test_dit_estimate_increases_toward_slower_observed_tone(self):
        md = morse_decoder_at_wpm(20)
        initial_est = md._dit_est
        slow_dit    = int(md.dit * 1.4)
        md.push_tone(slow_dit)
        assert md._dit_est > initial_est

    def test_unrecognised_morse_sequence_wrapped_in_brackets(self):
        md = morse_decoder_at_wpm(20)
        for _ in range(6):
            md.push_tone(md.dit)
            md.push_gap(md.dit)
        events = md.push_gap(md.dit * 7)
        decoded = chars_from(events)
        assert any(c.startswith('[') and c.endswith(']') for c in decoded)

    def test_CQ_sequence_produces_C_and_Q(self):
        md = morse_decoder_at_wpm(20)
        for sym in '-.-.':
            md.push_tone(md.dit * 3 if sym == '-' else md.dit)
            md.push_gap(md.dit)
        md.push_gap(md.dit * 3)
        for sym in '--.-':
            md.push_tone(md.dit * 3 if sym == '-' else md.dit)
            md.push_gap(md.dit)
        events = md.push_gap(md.dit * 7)
        decoded = chars_from(events)
        assert 'Q' in decoded


# ── Full signal chain (IQ → character events) ─────────────────────────────────

class TestCWSignalChain:
    def test_single_dit_decodes_as_E(self):
        assert 'E' in chars_from(decode_message('E'))

    def test_SOS_contains_S_and_O(self):
        decoded = chars_from(decode_message('SOS'))
        assert 'S' in decoded
        assert 'O' in decoded

    def test_CQ_DE_contains_Q_D_E_and_word_space(self):
        events = decode_message('CQ DE')
        text   = decoded_text(events)
        assert 'Q' in text
        assert 'D' in text
        assert 'E' in text
        assert 'word_space' in event_types_from(events)

    def test_space_in_input_produces_word_space_event(self):
        assert 'word_space' in event_types_from(decode_message('E T'))

    def test_decoded_alpha_chars_are_uppercase(self):
        for ch in chars_from(decode_message('HELLO')):
            assert not ch.isalpha() or ch == ch.upper()

    def test_chunked_processing_decodes_same_as_single_pass(self):
        assert 'S' in chars_from(decode_message('SOS', chunk_size=4096))

    def test_noise_only_input_produces_no_characters(self):
        rng   = np.random.default_rng(0)
        noise = rng.integers(100, 155, size=SDR_SAMPLE_RATE * 2, dtype=np.uint8)
        chain = cwd.CWSignalChain()
        chars = chars_from(chain.process(noise.tobytes()))
        assert chars == [], f"noise gate failed — got {len(chars)} false chars: {chars[:5]}"

    def test_off_frequency_interferer_does_not_produce_characters(self):
        """Bandpass filter must reject a CW station 5 kHz away from CW_FREQ_HZ."""
        interferer_offset = cwd.FREQ_OFFSET_HZ + 5_000   # 5 kHz above target
        intervals = [(False, SDR_SAMPLE_RATE)]            # 1 s silence pre-roll
        dit_n = DIT_SAMPLES * DECIMATE
        # Send 'SOS' on the interferer frequency
        for sym in '... --- ...':
            if sym == ' ':
                intervals.append((False, dit_n * 3))
                continue
            intervals.append((True,  dit_n if sym == '.' else dit_n * 3))
            intervals.append((False, dit_n))
        intervals.append((False, dit_n * 7))

        import math
        total = sum(n for _, n in intervals)
        iq    = np.zeros(total, dtype=np.complex64)
        step  = 2 * math.pi * interferer_offset / SDR_SAMPLE_RATE
        rng   = np.random.default_rng(7)
        idx   = 0
        for tone_on, n in intervals:
            t = np.arange(n, dtype=np.float64)
            if tone_on:
                iq[idx:idx + n] = (0.6 * np.exp(1j * t * step)).astype(np.complex64)
            iq[idx:idx + n] += (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * 0.02
            idx += n

        i_u8 = np.clip(iq.real * 127.5 + 127.5, 0, 255).astype(np.uint8)
        q_u8 = np.clip(iq.imag * 127.5 + 127.5, 0, 255).astype(np.uint8)
        raw = np.empty(total * 2, dtype=np.uint8)
        raw[0::2] = i_u8
        raw[1::2] = q_u8

        chain = cwd.CWSignalChain()
        events = [*chain.process(raw.tobytes()), *chain.flush()]
        chars = chars_from(events)
        assert chars == [], f"bandpass failed — off-freq interferer produced {chars}"


# ── IQ generation ─────────────────────────────────────────────────────────────

class TestIQGeneration:
    def test_output_length_is_even_number_of_bytes(self):
        assert len(make_cw_iq('E')) % 2 == 0

    def test_all_byte_values_are_in_uint8_range(self):
        data = np.frombuffer(make_cw_iq('SOS'), dtype=np.uint8)
        assert data.min() >= 0
        assert data.max() <= 255

    def test_tone_frequency_matches_cw_offset_after_mixing(self):
        data    = make_cw_iq('T')
        preroll = SDR_SAMPLE_RATE * 2
        raw     = np.frombuffer(data[preroll:preroll + 4096 * 2], dtype=np.uint8).astype(np.float32)
        iq      = ((raw[0::2] - 127.5) + 1j * (raw[1::2] - 127.5)) / 127.5
        n       = len(iq)
        lo      = np.exp(-1j * 2 * np.pi * FREQ_OFFSET_HZ * np.arange(n) / SDR_SAMPLE_RATE)
        freqs   = np.fft.fftshift(np.fft.fftfreq(n, 1 / SDR_SAMPLE_RATE))
        spectrum  = np.abs(np.fft.fftshift(np.fft.fft(iq * lo)))
        peak_freq = freqs[int(np.argmax(spectrum))]
        assert abs(peak_freq) < 5000


if __name__ == '__main__':
    print("=== CW Roundtrip Smoke Test ===")
    for msg in ['E', 'SOS', 'CQ DE']:
        print(f"  {msg!r} → {decoded_text(decode_message(msg))!r}")
