"""
CW audio file decoder.

Decodes a CW (Morse code) audio file (MP3, WAV, OGG, etc.) using the same
EnvelopeDetector and MorseDecoder logic as the live SDR decoder.

Audio files differ from the live IQ stream:
- Already at baseband — no frequency mixing needed
- Contains an audible CW tone (typically 400–900 Hz)
- May be any sample rate — resampled to 8 kHz internally

Usage:
    python3 decode_file.py <audio_file> [--wpm WPM] [--tone-hz HZ] [--debug]

Examples:
    python3 decode_file.py 250611_15WPM.mp3
    python3 decode_file.py recording.wav --wpm 15 --tone-hz 750
    python3 decode_file.py cw.mp3 --wpm 25 --debug
"""

import argparse
import logging
import subprocess
import sys
import numpy as np
from scipy.signal import butter, lfilter

# ── Constants ─────────────────────────────────────────────────────────────────

SAMPLE_RATE = 8_000   # internal working sample rate (Hz)

# Morse timing
DAH_THRESHOLD = 2.5
CHAR_GAP_DITS = 3.0
WORD_GAP_DITS = 7.0

# ── Morse code dictionary (same as cw_decoder.py) ─────────────────────────────

MORSE_CODE = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E",
    "..-.": "F", "--.": "G", "....": "H", "..": "I", ".---": "J",
    "-.-": "K", ".-..": "L", "--": "M", "-.": "N", "---": "O",
    ".--.": "P", "--.-": "Q", ".-.": "R", "...": "S", "-": "T",
    "..-": "U", "...-": "V", ".--": "W", "-..-": "X", "-.--": "Y",
    "--..": "Z",
    "-----": "0", ".----": "1", "..---": "2", "...--": "3", "....-": "4",
    ".....": "5", "-....": "6", "--...": "7", "---..": "8", "----.": "9",
    ".-.-.-": ".", "--..--": ",", "..--..": "?", ".----.": "'",
    "-.-.--": "!", "-..-.": "/", "-.--.": "(", "-.--.-": ")",
    ".-...": "&", "---...": ":", "-.-.-.": ";", "-...-": "=",
    ".-.-.": "+", "-....-": "-", "..--.-": "_", ".-..-.": '"',
    "...-..-": "$", ".--.-.": "@", "...---...": "SOS",
}

# ── Signal processing ─────────────────────────────────────────────────────────


def detect_tone_hz(audio: np.ndarray, sample_rate: int) -> float:
    """Estimate the dominant CW tone frequency via FFT on first 2 seconds."""
    n = min(len(audio), sample_rate * 2)
    fft = np.abs(np.fft.rfft(audio[:n]))
    freqs = np.fft.rfftfreq(n, 1 / sample_rate)
    # Only look in typical CW sidetone range 300–1500 Hz
    mask = (freqs >= 300) & (freqs <= 1500)
    if not mask.any():
        return 750.0
    peak_idx = np.argmax(fft * mask)
    return float(freqs[peak_idx])


def bandpass_filter(audio: np.ndarray, centre_hz: float, width_hz: float,
                    sample_rate: int) -> np.ndarray:
    """Narrow bandpass around the CW tone frequency."""
    nyq = sample_rate / 2.0
    low = max((centre_hz - width_hz / 2) / nyq, 0.001)
    high = min((centre_hz + width_hz / 2) / nyq, 0.999)
    b, a = butter(4, [low, high], btype="band")
    return lfilter(b, a, audio)


def envelope_detect(audio: np.ndarray, lpf_cutoff_hz: float,
                    sample_rate: int) -> np.ndarray:
    """Rectify and low-pass filter to extract the Morse envelope."""
    nyq = sample_rate / 2.0
    cutoff = min(lpf_cutoff_hz / nyq, 0.99)
    b, a = butter(3, cutoff, btype="low")
    return lfilter(b, a, np.abs(audio))


def adaptive_threshold(envelope: np.ndarray, window_samples: int) -> np.ndarray:
    """
    Adaptive threshold for audio file CW detection.

    Audio files have two distinct amplitude states — near-silence and tone.
    We use the midpoint between the noise floor (p10) and signal peak (p95)
    computed over non-overlapping blocks, updated ~4× per window.

    This is O(n) and produces a threshold that tracks level changes without
    being fooled by the duty cycle (unlike p75 * 1.8 which fires above max
    when >~55% of samples are in the tone state).
    """
    n = len(envelope)
    thresh = np.full(n, 0.05)

    step = max(window_samples // 4, 1)   # update every 0.5 s at 8 kHz
    prev_t = 0.05
    for start in range(0, n, step):
        look_start = max(0, start - window_samples)
        block = envelope[look_start:start + step]
        if len(block) > 200:
            noise = float(np.percentile(block, 10))
            peak  = float(np.percentile(block, 95))
            prev_t = noise + (peak - noise) * 0.5   # midpoint
            prev_t = max(prev_t, 0.01)
        end = min(start + step, n)
        thresh[start:end] = prev_t

    return thresh


# ── Morse decoder ─────────────────────────────────────────────────────────────


class MorseDecoder:
    def __init__(self) -> None:
        self._symbols: list[str] = []

    def push_tone(self, duration: int, dit_samples: int) -> None:
        if duration < dit_samples * 0.4:
            return
        self._symbols.append("." if duration < dit_samples * DAH_THRESHOLD else "-")

    def push_gap(self, duration: int, dit_samples: int, callback) -> None:
        gap_dits = duration / dit_samples
        if gap_dits >= WORD_GAP_DITS:
            self._flush(callback)
            callback(None)  # word space sentinel
        elif gap_dits >= CHAR_GAP_DITS:
            self._flush(callback)

    def _flush(self, callback) -> None:
        if self._symbols:
            code = "".join(self._symbols)
            callback(MORSE_CODE.get(code, f"[{code}]"))
            self._symbols.clear()


# ── Main decode function ──────────────────────────────────────────────────────


def decode_audio(audio: np.ndarray, sample_rate: int, wpm: float,
                 tone_hz: float | None = None, debug: bool = False) -> str:
    """
    Decode CW from a mono audio array.

    Returns the decoded text string.
    """
    log = logging.getLogger("decode_file")

    # Auto-detect tone frequency if not specified
    if tone_hz is None:
        tone_hz = detect_tone_hz(audio, sample_rate)
        log.info("Auto-detected CW tone: %.0f Hz", tone_hz)
    else:
        log.info("Using specified CW tone: %.0f Hz", tone_hz)

    # Bandpass around the CW tone (±150 Hz window)
    filtered = bandpass_filter(audio, tone_hz, width_hz=300.0, sample_rate=sample_rate)

    # Envelope detection — LPF at ~200 Hz to capture Morse on/off transitions
    envelope = envelope_detect(filtered, lpf_cutoff_hz=200.0, sample_rate=sample_rate)

    # Adaptive threshold — midpoint between noise floor and signal peak
    window_samples = sample_rate * 2   # 2-second rolling window
    thresh = adaptive_threshold(envelope, window_samples)

    # Morse timing
    dit_samples = int((60.0 / (50 * wpm)) * sample_rate)
    log.info("WPM=%.0f, dit=%d samples @ %d Hz, tone=%.0f Hz",
             wpm, dit_samples, sample_rate, tone_hz)

    # Decode state machine
    decoder = MorseDecoder()
    chars: list[str] = []

    def on_char(c) -> None:
        chars.append(c)
        if debug:
            sym = " " if c is None else c
            log.debug("  → %r", sym)

    tone_on = False
    tone_start = 0
    gap_start = 0

    for i, (sample, thr) in enumerate(zip(envelope, thresh)):
        is_tone = float(sample) > float(thr)
        if is_tone and not tone_on:
            if gap_start > 0:
                decoder.push_gap(i - gap_start, dit_samples, on_char)
            tone_on = True
            tone_start = i
        elif not is_tone and tone_on:
            decoder.push_tone(i - tone_start, dit_samples)
            tone_on = False
            gap_start = i

    # Flush any remaining symbol at end of file
    decoder.push_gap(int(dit_samples * WORD_GAP_DITS + 1), dit_samples, on_char)

    # Build output text (None = word space)
    parts = []
    for c in chars:
        if c is None:
            parts.append(" ")
        else:
            parts.append(c)

    return "".join(parts).strip()


def load_audio_ffmpeg(path: str, sample_rate: int) -> np.ndarray:
    """Use ffmpeg to decode any audio format to raw float32 mono."""
    proc = subprocess.run(
        ["ffmpeg", "-i", path, "-ar", str(sample_rate), "-ac", "1",
         "-f", "f32le", "pipe:1"],
        capture_output=True,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace")
        raise RuntimeError(f"ffmpeg failed: {stderr[-500:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Decode CW from an audio file")
    parser.add_argument("file", help="Audio file (MP3, WAV, OGG, …)")
    parser.add_argument("--wpm", type=float, default=15.0,
                        help="Expected WPM speed (default: 15)")
    parser.add_argument("--tone-hz", type=float, default=None,
                        help="CW tone frequency in Hz (default: auto-detect)")
    parser.add_argument("--debug", action="store_true",
                        help="Show per-character debug output")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(message)s")
    log = logging.getLogger("decode_file")

    log.info("Loading %s …", args.file)
    audio = load_audio_ffmpeg(args.file, SAMPLE_RATE)
    log.info("Loaded %.1f seconds of audio (%d samples @ %d Hz)",
             len(audio) / SAMPLE_RATE, len(audio), SAMPLE_RATE)

    text = decode_audio(audio, SAMPLE_RATE, wpm=args.wpm,
                        tone_hz=args.tone_hz, debug=args.debug)

    print("\n── Decoded text ─────────────────────────────────────────")
    print(text if text else "(nothing decoded)")
    print("─────────────────────────────────────────────────────────\n")


if __name__ == "__main__":
    main()
