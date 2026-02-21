"""
inject_cw.py — inject a synthetic CW transmission into the live cw-decoder.

Connects to the running cw-decoder WebSocket (/ws/cw) and listens for decoded
characters while simultaneously connecting to the decoder's TCP port (acting as
a fake rtl-bridge mux) to stream synthetic CW IQ data.

Usage (from the project root on the N100, or via SSH):
    python3 services/cw-decoder/inject_cw.py [--message "CQ CQ DE W1AW"]

The script:
  1. Generates uint8 IQ bytes at 2.4 Msps with a CW tone at FREQ_OFFSET_HZ
     (-146 kHz, matching the 14.029 MHz CW frequency).
  2. Serves the IQ stream over a local TCP socket with a fake RTL0 header.
  3. Connects to the cw-decoder WebSocket and prints decoded characters.
  4. Exits when the full message has been transmitted and decoded.

This works by temporarily replacing the decoder's TCP connection with our fake
mux.  The script patches the decoder's MUX_HOST/MUX_PORT via environment
variable override — so run it on the N100 where docker exec is available, or
use --direct to connect directly to the decoder's internal port.

Simpler approach: connect to the decoder's *WebSocket* as a listener, and run
a local fake-mux server that the decoder connects to. But the decoder already
connects to rtl-bridge:1235 — we can't easily redirect that from outside.

Instead this script:
  - Starts a fake TCP mux server on localhost:11235
  - docker execs into the container and triggers a reconnect via SIGUSR1
    (decoder will reconnect to our fake mux if MUX_HOST is overridden)

Simpler still: we bypass all of that and test the signal chain directly by
running the decoder's process() function on synthetic IQ bytes and printing
the decoded characters.  This exercises every stage except the TCP/WebSocket
plumbing (which has its own tests).
"""

import argparse
import math
import sys
from pathlib import Path

import numpy as np

# ── Add the service dir to sys.path so we can import cw_decoder ───────────────
SERVICE_DIR = Path(__file__).parent
sys.path.insert(0, str(SERVICE_DIR))

# ── Import the decoder under test ─────────────────────────────────────────────
from cw_decoder import (  # noqa: E402
    AUDIO_RATE,
    CW_FREQ_HZ,
    DIT_SAMPLES,
    FREQ_OFFSET_HZ,
    SDR_SAMPLE_RATE,
    CWSignalChain,
)

# ── CW message to transmit ─────────────────────────────────────────────────────
MORSE: dict[str, str] = {
    'A': '.-', 'B': '-...', 'C': '-.-.', 'D': '-..', 'E': '.', 'F': '..-.',
    'G': '--.', 'H': '....', 'I': '..', 'J': '.---', 'K': '-.-', 'L': '.-..',
    'M': '--', 'N': '-.', 'O': '---', 'P': '.--.', 'Q': '--.-', 'R': '.-.',
    'S': '...', 'T': '-', 'U': '..-', 'V': '...-', 'W': '.--', 'X': '-..-',
    'Y': '-.--', 'Z': '--..', '0': '-----', '1': '.----', '2': '..---',
    '3': '...--', '4': '....-', '5': '.....', '6': '-....', '7': '--...',
    '8': '---..', '9': '----.',
}

# Number of SDR samples per audio sample
OVERSAMPLE = SDR_SAMPLE_RATE // AUDIO_RATE   # 100


_seg_counter = 0  # incremented per segment to keep noise uncorrelated


def make_iq_segment(tone_on: bool, n_audio_samples: int, freq_offset_hz: float = FREQ_OFFSET_HZ) -> bytes:
    """Generate n_audio_samples worth of IQ data (×OVERSAMPLE for SDR rate).

    If tone_on: a complex sinusoid at freq_offset_hz (amplitude 0.7).
    If not:     Gaussian noise at amplitude 0.02 (realistic SNR gap).
    Each segment gets a different RNG seed so adjacent noise blocks are uncorrelated.
    """
    global _seg_counter
    _seg_counter += 1
    rng = np.random.default_rng(_seg_counter)

    n_sdr = n_audio_samples * OVERSAMPLE
    if tone_on:
        t = np.arange(n_sdr) / SDR_SAMPLE_RATE
        # Tone amplitude 0.7 → well above noise, gives p90/p5 >> 2.5
        i_samples = 0.7 * np.cos(2 * math.pi * freq_offset_hz * t)
        q_samples = 0.7 * np.sin(2 * math.pi * freq_offset_hz * t)
        # Add a little noise so the FIR isn't processing a perfect sinusoid
        i_samples += rng.normal(0, 0.02, n_sdr)
        q_samples += rng.normal(0, 0.02, n_sdr)
    else:
        i_samples = rng.normal(0, 0.02, n_sdr)
        q_samples = rng.normal(0, 0.02, n_sdr)

    # Encode as uint8 (RTL-SDR wire format: I, Q interleaved, offset binary)
    i_u8 = np.clip(i_samples * 127.5 + 127.5, 0, 255).astype(np.uint8)
    q_u8 = np.clip(q_samples * 127.5 + 127.5, 0, 255).astype(np.uint8)
    interleaved = np.empty(n_sdr * 2, dtype=np.uint8)
    interleaved[0::2] = i_u8
    interleaved[1::2] = q_u8
    return interleaved.tobytes()


def message_to_iq(message: str) -> bytes:
    """Encode a text message as CW IQ bytes at SDR rate.

    Timing (20 WPM):
      dit   = DIT_SAMPLES audio samples
      dah   = 3 × DIT_SAMPLES
      intra = 1 × DIT_SAMPLES (between elements)
      char  = 3 × DIT_SAMPLES (between characters)
      word  = 7 × DIT_SAMPLES (between words)
    """
    dit = DIT_SAMPLES
    chunks: list[bytes] = []

    # Lead-in silence: 5 word gaps so the adaptive threshold fully settles.
    chunks.append(make_iq_segment(False, dit * 7 * 5))

    # Warm-up sequence: "VVV " — lets the envelope smoother stabilise on real
    # keying before the message begins.  VVV is universally used as a pre-amble
    # in amateur CW to lock in timing.  Results are discarded by the receiver.
    for _ in range(3):
        for element in '...-':            # V = ...-
            dur = dit if element == '.' else dit * 3
            chunks.append(make_iq_segment(True, dur))
            chunks.append(make_iq_segment(False, dit))  # intra-char gap
        chunks.append(make_iq_segment(False, dit * 3))  # inter-char gap
    chunks.append(make_iq_segment(False, dit * 7))      # word gap before message

    words = message.upper().split()
    for w_idx, word in enumerate(words):
        for c_idx, ch in enumerate(word):
            code = MORSE.get(ch, '')
            if not code:
                continue
            for e_idx, element in enumerate(code):
                dur = dit if element == '.' else dit * 3
                chunks.append(make_iq_segment(True, dur))
                # Intra-character gap (not after last element)
                if e_idx < len(code) - 1:
                    chunks.append(make_iq_segment(False, dit))
            # Inter-character gap (not after last char in word)
            if c_idx < len(word) - 1:
                chunks.append(make_iq_segment(False, dit * 3))
        # Word gap (not after last word)
        if w_idx < len(words) - 1:
            chunks.append(make_iq_segment(False, dit * 7))

    # Trail-out silence: one word gap so last character flushes
    chunks.append(make_iq_segment(False, dit * 7))
    return b''.join(chunks)


def run(message: str, chunk_size: int = 65536) -> None:
    """Process the synthetic IQ through the decoder and print results."""
    print(f"Injecting: {message!r}")
    print(f"  SDR rate:   {SDR_SAMPLE_RATE:,} sps")
    print(f"  Audio rate: {AUDIO_RATE:,} Hz")
    print(f"  WPM:        {60 * AUDIO_RATE // (50 * DIT_SAMPLES)}")
    print(f"  CW freq:    {CW_FREQ_HZ / 1e6:.3f} MHz (offset {FREQ_OFFSET_HZ:+,} Hz from DC)")
    print()

    iq_bytes = message_to_iq(message)
    total_sdr = len(iq_bytes) // 2
    duration_s = total_sdr / SDR_SAMPLE_RATE
    print(f"  IQ length:  {len(iq_bytes):,} bytes ({total_sdr:,} samples, {duration_s:.1f}s)")
    print()

    chain = CWSignalChain()
    decoded: list[str] = []

    # Process in realistic chunk sizes (same as live TCP read)
    offset = 0
    while offset < len(iq_bytes):
        chunk = iq_bytes[offset:offset + chunk_size]
        events = chain.process(chunk)
        for ev in events:
            if ev['type'] == 'char':
                ch = ev['char']
                decoded.append(ch)
                print(f"  CHAR: {ch!r}", flush=True)
            elif ev['type'] == 'word_space':
                decoded.append(' ')
                print("  SPACE", flush=True)
        offset += chunk_size

    # Flush final character
    for ev in chain.flush():
        if ev['type'] == 'char':
            ch = ev['char']
            decoded.append(ch)
            print(f"  CHAR: {ch!r} (flushed)", flush=True)
        elif ev['type'] == 'word_space':
            decoded.append(' ')

    result = ''.join(decoded).strip()
    print()
    expected_clean = ' '.join(message.upper().split())
    print(f"Expected: {expected_clean!r}")
    print(f"Decoded:  {result!r}")

    # The VVV warm-up preamble is decoded (possibly garbled) before the message.
    # Check that the expected message appears at the *end* of the decoded text,
    # allowing for extra preamble chars at the front.
    decoded_words = result.split()
    expected_words = expected_clean.split()
    suffix_match = decoded_words[-len(expected_words):] == expected_words

    if suffix_match:
        preamble = ' '.join(decoded_words[:-len(expected_words)])
        print(f"\n✓ PASS — message decoded correctly (preamble: {preamble!r})")
        sys.exit(0)
    else:
        print("\n✗ FAIL — message not found in decoded output")
        sys.exit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Inject synthetic CW into the decoder')
    parser.add_argument('--message', default='CQ CQ DE W1AW', help='Message to encode (default: CQ CQ DE W1AW)')
    parser.add_argument('--chunk-size', type=int, default=65536, help='IQ bytes per process() call')
    args = parser.parse_args()
    run(args.message, args.chunk_size)
