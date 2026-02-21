/**
 * CWDecoder unit tests.
 *
 * Three test layers:
 *
 * 1. MORSE_CODE table — instant lookups
 * 2. Construction — parameter derivation
 * 3. decodeAudio() end-to-end character decode.
 *
 *    Performance constraints:
 *    - decodeAudio() sorts its adaptive-threshold window every sample → O(n²)
 *    - We use sr=8000, wpm=200, no leading silence to keep n < 1000 per test
 *    - At n=1000 the O(n²) cost is ~1M ops — well under the 5 s timeout
 *    - dit=48 samples > FIR taps (16), so the decimation filter has time to fill
 *
 * 4. pushBytes() smoke tests — pipeline integrity only (IQ at 2.4 MHz is too
 *    slow to run full decode in a unit test)
 */

import { describe, expect, it } from 'vitest';
import { CWDecoder, MORSE_CODE } from './CWDecoder.js';
import type { CWEvent } from './CWDecoder.js';

// ── MORSE_CODE table ──────────────────────────────────────────────────────────

describe('MORSE_CODE table', () => {
  it('contains all 26 letters', () => {
    const chars = Object.values(MORSE_CODE);
    for (const ch of 'ABCDEFGHIJKLMNOPQRSTUVWXYZ') {
      expect(chars).toContain(ch);
    }
  });

  it('contains all 10 digits', () => {
    const chars = Object.values(MORSE_CODE);
    for (const d of '0123456789') {
      expect(chars).toContain(d);
    }
  });

  it('E is a single dot', () => {
    expect(MORSE_CODE['.']).toBe('E');
  });

  it('T is a single dash', () => {
    expect(MORSE_CODE['-']).toBe('T');
  });

  it('SOS is a special sequence', () => {
    expect(MORSE_CODE['...---...']).toBe('SOS');
  });
});

// ── Construction ──────────────────────────────────────────────────────────────

describe('CWDecoder construction', () => {
  const SDR_SAMPLE_RATE = 2_400_000;
  const SDR_CENTER_HZ   = 139_175_000;
  const LO_OFFSET_HZ    = 125_000_000;
  const CW_FREQ_HZ      = 14_029_000;
  const RF_CENTER_HZ    = SDR_CENTER_HZ - LO_OFFSET_HZ;   // 14 175 000

  it('initialises with correct derived parameters', () => {
    const dec = new CWDecoder({
      sdrSampleRate: SDR_SAMPLE_RATE,
      sdrCenterHz:   SDR_CENTER_HZ,
      loOffsetHz:    LO_OFFSET_HZ,
      cwFreqHz:      CW_FREQ_HZ,
      wpm:           20,
    });
    expect(dec.cwFrequencyHz).toBe(CW_FREQ_HZ);
    expect(dec.rfCenterHz).toBe(RF_CENTER_HZ);
    expect(dec.freqOffsetHz).toBe(CW_FREQ_HZ - RF_CENTER_HZ);   // -146 000
    expect(dec.audioSampleRate).toBe(SDR_SAMPLE_RATE / 100);    // 24 000
    // At 20 WPM: dit = 60/(50×20) × 24000 = 1440 samples
    expect(dec.ditSamples).toBe(1440);
  });

  it('uses sensible defaults', () => {
    const dec = new CWDecoder();
    expect(dec.audioSampleRate).toBe(24_000);
    expect(dec.cwFrequencyHz).toBe(14_029_000);
  });
});

// ── Audio-rate decode helpers ─────────────────────────────────────────────────

/**
 * Sample rate for tests: must be >> KaiserFIR cutoff (300 Hz) and envelope
 * LPF cutoff (200 Hz), so 8 kHz is fine (Nyquist = 4 kHz).
 *
 * WPM for tests: 200 WPM gives dit = 60/(50×200) × 8000 = 48 samples.
 * This is long enough for the 16-tap KaiserFIR to fill before the first dit ends,
 * yet short enough that multi-element characters stay under ~1000 total samples,
 * keeping the O(n²) adaptive-threshold sort path well within the 5 s timeout.
 */
const SAMPLE_RATE = 8_000;
const TONE_HZ     = 750;    // typical CW sidetone, well below Nyquist
const WPM         = 200;    // high WPM keeps sample counts small

/** Synthesise a gated CW tone at toneHz using the given segment list. */
function synthAudio(
  toneHz: number,
  sampleRate: number,
  segments: Array<{ tone: boolean; samples: number }>,
): Float32Array {
  const total = segments.reduce((s, p) => s + p.samples, 0);
  const out = new Float32Array(total);
  let idx = 0;
  let phase = 0;
  const angFreq = (2 * Math.PI * toneHz) / sampleRate;
  for (const seg of segments) {
    const amp = seg.tone ? 0.8 : 0;
    for (let s = 0; s < seg.samples; s++) {
      out[idx++] = amp * Math.cos(phase);
      phase += angFreq;
    }
  }
  return out;
}

/**
 * Build Morse segment list for a single character.
 * No leading silence (to keep sample count small).
 * A 1-sample trailing gap triggers the final tone→off transition so the
 * Morse state machine records the last element; decodeAudio() calls flush()
 * afterwards to emit the character regardless.
 */
function charSegments(
  ch: string,
  wpm: number,
  sampleRate: number,
): Array<{ tone: boolean; samples: number }> {
  const reverseMorse: Record<string, string> = {};
  for (const [code, char] of Object.entries(MORSE_CODE)) {
    reverseMorse[char] = code;
  }

  const dit = Math.max(1, Math.round((60 / (50 * wpm)) * sampleRate));
  const dah = dit * 3;
  const intra = dit;

  const code = reverseMorse[ch.toUpperCase()];
  if (!code) throw new Error(`No Morse code for '${ch}'`);

  const charGap = dit * 3;
  const segs: Array<{ tone: boolean; samples: number }> = [];
  for (let ei = 0; ei < code.length; ei++) {
    if (ei > 0) segs.push({ tone: false, samples: intra });
    segs.push({ tone: true, samples: code[ei] === '.' ? dit : dah });
  }
  // Trailing silence long enough for the IIR envelope to fall below threshold
  // before flush() is called.  At alpha≈0.136 (sr=8000, cutoff=200Hz),
  // the envelope decays below 0.05 in ~15 samples; charGap >> 15.
  segs.push({ tone: false, samples: charGap });
  return segs;
}

function decodeChars(events: CWEvent[]): string[] {
  return events
    .filter((e): e is { type: 'char'; char: string; freq: number } => e.type === 'char')
    .map((e) => e.char);
}

// ── decodeAudio end-to-end ────────────────────────────────────────────────────

describe('CWDecoder.decodeAudio — end-to-end character decode', () => {
  it('decodes E (single dit)', () => {
    const dec = new CWDecoder();
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, charSegments('E', WPM, SAMPLE_RATE));
    const chars = decodeChars(dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toContain('E');
  });

  it('decodes T (single dah)', () => {
    const dec = new CWDecoder();
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, charSegments('T', WPM, SAMPLE_RATE));
    const chars = decodeChars(dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toContain('T');
  });

  it('decodes M (two dahs)', () => {
    const dec = new CWDecoder();
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, charSegments('M', WPM, SAMPLE_RATE));
    const chars = decodeChars(dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toContain('M');
  });

  it('decodes S (three dits)', () => {
    const dec = new CWDecoder();
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, charSegments('S', WPM, SAMPLE_RATE));
    const chars = decodeChars(dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toContain('S');
  });

  it('decodes O (three dahs)', () => {
    const dec = new CWDecoder();
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, charSegments('O', WPM, SAMPLE_RATE));
    const chars = decodeChars(dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toContain('O');
  });

  it('decodes C (dah-dit-dah-dit)', () => {
    const dec = new CWDecoder();
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, charSegments('C', WPM, SAMPLE_RATE));
    const chars = decodeChars(dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toContain('C');
  });

  it('decodes digit 5 (five dits)', () => {
    const dec = new CWDecoder();
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, charSegments('5', WPM, SAMPLE_RATE));
    const chars = decodeChars(dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toContain('5');
  });

  it('returns no chars for silence', () => {
    const dec = new CWDecoder();
    const silence = new Float32Array(100);   // well below 200-sample sort trigger
    const chars = decodeChars(dec.decodeAudio(silence, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toHaveLength(0);
  });

  it('reports char freq matching toneHz', () => {
    const dec = new CWDecoder();
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, charSegments('E', WPM, SAMPLE_RATE));
    const events = dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM);
    const charEvt = events.find((e): e is { type: 'char'; char: string; freq: number } =>
      e.type === 'char'
    );
    if (!charEvt) throw new Error('Expected at least one char event');
    expect(charEvt.freq).toBe(Math.round(TONE_HZ));
  });

  it('decodes multiple characters sequentially (CQ)', () => {
    // Concatenate segments for C then Q, separated by a char-gap
    const dit = Math.round((60 / (50 * WPM)) * SAMPLE_RATE);
    const charGap = dit * 3;

    const cSegs = charSegments('C', WPM, SAMPLE_RATE).slice(0, -1);  // drop trailing 1-sample gap
    const qSegs = charSegments('Q', WPM, SAMPLE_RATE);
    const allSegs = [
      ...cSegs,
      { tone: false as const, samples: charGap },
      ...qSegs,
    ];

    const dec = new CWDecoder();
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, allSegs);
    const chars = decodeChars(dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toContain('C');
    expect(chars).toContain('Q');
  });
});

// ── pushBytes smoke tests ─────────────────────────────────────────────────────

describe('CWDecoder.pushBytes — pipeline smoke tests', () => {
  it('accepts silence without error or spurious chars', () => {
    const dec = new CWDecoder();
    // 100 ms of silence at SDR rate (I=127, Q=127 → both ≈0)
    const silent = new Uint8Array(240_000).fill(127);
    const events: CWEvent[] = [];
    expect(() => dec.pushBytes(silent, (ev) => events.push(ev))).not.toThrow();
    expect(decodeChars(events)).toHaveLength(0);
  });

  it('accepts odd-length arrays (truncates trailing byte)', () => {
    const dec = new CWDecoder();
    expect(() => dec.pushBytes(new Uint8Array(513).fill(127), () => {})).not.toThrow();
  });

  it('accepts empty array without error', () => {
    const dec = new CWDecoder();
    expect(() => dec.pushBytes(new Uint8Array(0), () => {})).not.toThrow();
  });
});
