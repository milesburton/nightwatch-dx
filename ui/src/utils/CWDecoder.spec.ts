/**
 * CWDecoder unit tests.
 *
 * Three test layers:
 *
 * 1. MORSE_CODE table — instant lookups
 * 2. Construction — parameter derivation
 * 3. decodeAudio() end-to-end decode at 8 000 Hz / high WPM so total
 *    sample count stays small (< 200) and the adaptive-threshold sort
 *    path is never reached, keeping each test fast (< 100 ms).
 * 4. pushBytes() smoke tests — pipeline integrity without full IQ decode
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
 * Build a mono Float32Array with a gated CW tone at `toneHz`.
 * Uses the given segments array (each segment: on/off + sample count).
 */
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
 * Build Morse timing segments for `text`.
 *
 * Key insight for test speed: keeps total samples < 200 so the
 * EnvelopeDetector's adaptive threshold (sort-based) is never triggered,
 * keeping each test fast. Use a small `sampleRate` and high `wpm`.
 */
function morseSegments(
  text: string,
  wpm: number,
  sampleRate: number,
  opts: { leadingSilence?: boolean; trailingSilence?: boolean } = {},
): Array<{ tone: boolean; samples: number }> {
  const { leadingSilence = false, trailingSilence = true } = opts;

  const reverseMorse: Record<string, string> = {};
  for (const [code, char] of Object.entries(MORSE_CODE)) {
    reverseMorse[char] = code;
  }

  const dit = Math.max(1, Math.round((60 / (50 * wpm)) * sampleRate));
  const dah = dit * 3;
  const intraGap = dit;
  const charGap  = dit * 3;
  const wordGap  = dit * 7;

  const segs: Array<{ tone: boolean; samples: number }> = [];
  if (leadingSilence) segs.push({ tone: false, samples: charGap });

  for (let ci = 0; ci < text.length; ci++) {
    const ch = text[ci].toUpperCase();
    if (ch === ' ') {
      segs.push({ tone: false, samples: wordGap - charGap });
      continue;
    }
    const code = reverseMorse[ch];
    if (!code) continue;

    for (let ei = 0; ei < code.length; ei++) {
      if (ei > 0) segs.push({ tone: false, samples: intraGap });
      segs.push({ tone: true, samples: code[ei] === '.' ? dit : dah });
    }
    segs.push({ tone: false, samples: charGap });
  }

  if (trailingSilence) segs.push({ tone: false, samples: wordGap });
  return segs;
}

function decodeChars(events: CWEvent[]): string[] {
  return events
    .filter((e): e is { type: 'char'; char: string; freq: number } => e.type === 'char')
    .map((e) => e.char);
}

/**
 * Test sample rate chosen so total sample count for single-char tests
 * stays well below 200 (the adaptive-threshold trigger) while still
 * giving the LPF time to respond.
 *
 * At SAMPLE_RATE=200, WPM=25:
 *   dit = 60/(50×25) × 200 = 9.6 ≈ 10 samples
 *   charGap = 30, wordGap = 70
 *   'E' total = 10 (dit) + 30 (charGap) + 70 (trailing) = 110 samples ✓
 */
const SAMPLE_RATE = 200;
const TONE_HZ     = 40;    // well below Nyquist (100 Hz)
const WPM         = 25;

// ── decodeAudio end-to-end ────────────────────────────────────────────────────

describe('CWDecoder.decodeAudio — end-to-end character decode', () => {
  it('decodes E (single dit)', () => {
    const dec = new CWDecoder();
    const segs = morseSegments('E', WPM, SAMPLE_RATE);
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, segs);
    const chars = decodeChars(dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toContain('E');
  });

  it('decodes T (single dah)', () => {
    const dec = new CWDecoder();
    const segs = morseSegments('T', WPM, SAMPLE_RATE);
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, segs);
    const chars = decodeChars(dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toContain('T');
  });

  it('decodes M (two dahs)', () => {
    const dec = new CWDecoder();
    const segs = morseSegments('M', WPM, SAMPLE_RATE);
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, segs);
    const chars = decodeChars(dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toContain('M');
  });

  it('decodes S (three dits)', () => {
    const dec = new CWDecoder();
    const segs = morseSegments('S', WPM, SAMPLE_RATE);
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, segs);
    const chars = decodeChars(dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toContain('S');
  });

  it('decodes O (three dahs)', () => {
    const dec = new CWDecoder();
    const segs = morseSegments('O', WPM, SAMPLE_RATE);
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, segs);
    const chars = decodeChars(dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toContain('O');
  });

  it('decodes C (dah-dit-dah-dit)', () => {
    const dec = new CWDecoder();
    const segs = morseSegments('C', WPM, SAMPLE_RATE);
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, segs);
    const chars = decodeChars(dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toContain('C');
  });

  it('decodes digit 5 (five dits)', () => {
    const dec = new CWDecoder();
    const segs = morseSegments('5', WPM, SAMPLE_RATE);
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, segs);
    const chars = decodeChars(dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toContain('5');
  });

  it('returns no chars for silence', () => {
    const dec = new CWDecoder();
    const silence = new Float32Array(50);
    const chars = decodeChars(dec.decodeAudio(silence, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toHaveLength(0);
  });

  it('reports char freq matching toneHz', () => {
    const dec = new CWDecoder();
    const segs = morseSegments('E', WPM, SAMPLE_RATE);
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, segs);
    const events = dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM);
    const charEvt = events.find((e): e is { type: 'char'; char: string; freq: number } =>
      e.type === 'char'
    );
    if (!charEvt) throw new Error('Expected at least one char event');
    expect(charEvt.freq).toBe(Math.round(TONE_HZ));
  });

  it('decodes multiple characters sequentially', () => {
    // Concatenate individual character segments (each already includes charGap)
    const dec = new CWDecoder();
    const allSegs = [
      ...morseSegments('C', WPM, SAMPLE_RATE, { trailingSilence: false }),
      ...morseSegments('Q', WPM, SAMPLE_RATE),
    ];
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, allSegs);
    const chars = decodeChars(dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM));
    expect(chars).toContain('C');
    expect(chars).toContain('Q');
  });

  it('produces a word_space event between words', () => {
    const dec = new CWDecoder();
    const allSegs = [
      ...morseSegments('E', WPM, SAMPLE_RATE, { trailingSilence: false }),
      { tone: false as const, samples: Math.round((60 / (50 * WPM)) * SAMPLE_RATE) * 7 },
      ...morseSegments('T', WPM, SAMPLE_RATE),
    ];
    const samples = synthAudio(TONE_HZ, SAMPLE_RATE, allSegs);
    const events = dec.decodeAudio(samples, SAMPLE_RATE, TONE_HZ, WPM);
    const wordSpaces = events.filter((e) => e.type === 'word_space');
    expect(wordSpaces.length).toBeGreaterThanOrEqual(1);
  });
});

// ── pushBytes smoke tests ─────────────────────────────────────────────────────

describe('CWDecoder.pushBytes — pipeline smoke tests', () => {
  it('accepts silence without error or spurious chars', () => {
    const dec = new CWDecoder();
    // 100 ms of silence at SDR rate = 240 000 IQ bytes
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
