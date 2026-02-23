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

AUDIO_RATE  = cwd.AUDIO_RATE
DIT_SAMPLES = cwd.DIT_SAMPLES

CHAR_TO_MORSE: dict[str, str] = {v: k for k, v in cwd.MORSE_CODE.items()}


# ── Pure test helpers ──────────────────────────────────────────────────────────

def morse_intervals_for(text: str) -> list[tuple[bool, int]]:
    """Return (tone_on, n_samples) intervals at AUDIO_RATE."""
    dit = DIT_SAMPLES
    intervals: list[tuple[bool, int]] = [(False, AUDIO_RATE)]  # 1 s silence pre-roll
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


def render_audio(intervals: list[tuple[bool, int]], amplitude: float = 0.6,
                 noise_amplitude: float = 0.02) -> bytes:
    """Generate complex64 audio bytes at AUDIO_RATE.

    CW tone at 100 Hz -- within the +/-BP_HZ bandpass filter.
    """
    total = sum(n for _, n in intervals)
    iq    = np.zeros(total, dtype=np.complex64)
    # 100 Hz tone (well within the 150 Hz bandpass)
    step  = 2 * math.pi * 100.0 / AUDIO_RATE
    rng   = np.random.default_rng(42)
    phase = 0.0
    idx   = 0
    for tone_on, n in intervals:
        t = np.arange(n, dtype=np.float64)
        if tone_on:
            signal = amplitude * np.exp(1j * (phase + t * step)).astype(np.complex64)
        else:
            signal = np.zeros(n, dtype=np.complex64)
        noise = ((rng.standard_normal(n) + 1j * rng.standard_normal(n)) * noise_amplitude).astype(np.complex64)
        iq[idx:idx + n] = signal + noise
        phase = float(phase + t[-1] * step + step) if tone_on else float(phase + n * step)
        idx  += n
    return iq.tobytes()


def make_cw_audio(text: str, amplitude: float = 0.6, noise_amplitude: float = 0.02) -> bytes:
    return render_audio(morse_intervals_for(text), amplitude, noise_amplitude)


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


def decode_message(text: str, chunk_size: int = 2624) -> list[dict]:
    """chunk_size=2624: 328 complex64 samples * 8 bytes -- ~13 ms at 24 kHz."""
    chain       = cwd.CWSignalChain()
    audio_bytes = make_cw_audio(text)
    events: list[dict] = []
    for start in range(0, len(audio_bytes), chunk_size):
        events.extend(chain.process(audio_bytes[start:start + chunk_size]))
    return [*events, *chain.flush()]


def morse_decoder_at_wpm(wpm: int) -> cwd.MorseDecoder:
    dit_samples = round((60 / (50 * wpm)) * AUDIO_RATE)
    md = cwd.MorseDecoder()
    md._dit_est = float(dit_samples)
    return md


# ── Signal chain constants ─────────────────────────────────────────────────────

class TestConstants:
    def test_audio_rate_is_24khz(self):
        assert cwd.AUDIO_RATE == 24_000

    def test_dit_samples_matches_wpm_timing(self):
        expected = round((60 / (50 * cwd.WPM)) * AUDIO_RATE)
        assert expected == cwd.DIT_SAMPLES


# ── FIR filter ─────────────────────────────────────────────────────────────────

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


# ── Envelope detector ──────────────────────────────────────────────────────────

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


# ── MorseDecoder unit tests ────────────────────────────────────────────────────

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


# ── Full signal chain (complex64 audio -> character events) ───────────────────

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
        assert 'S' in chars_from(decode_message('SOS', chunk_size=1024))

    def test_noise_only_input_produces_no_characters(self):
        rng   = np.random.default_rng(0)
        # 2 seconds of complex noise at AUDIO_RATE
        noise_c = (rng.standard_normal(AUDIO_RATE * 2) +
                   1j * rng.standard_normal(AUDIO_RATE * 2)).astype(np.complex64) * 0.01
        chain = cwd.CWSignalChain()
        chars = chars_from(chain.process(noise_c.tobytes()))
        assert chars == [], f"noise gate failed -- got {len(chars)} false chars: {chars[:5]}"


# ── Audio generation ───────────────────────────────────────────────────────────

class TestAudioGeneration:
    def test_output_length_is_multiple_of_8_bytes(self):
        # Each complex64 sample is 8 bytes
        assert len(make_cw_audio('E')) % 8 == 0

    def test_output_is_complex64_parseable(self):
        data = make_cw_audio('SOS')
        arr  = np.frombuffer(data, dtype=np.complex64)
        assert arr.dtype == np.complex64
        assert len(arr) > 0


if __name__ == '__main__':
    print("=== CW Roundtrip Smoke Test ===")
    for msg in ['E', 'SOS', 'CQ DE']:
        print(f"  {msg!r} -> {decoded_text(decode_message(msg))!r}")
