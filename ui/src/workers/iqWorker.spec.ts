/**
 * Tests for the FFT processing logic extracted from iqWorker.ts.
 * Verifies that processFFT emits the expected number of FFT frames per
 * WebSocket chunk and that the waterfall colour-mapping is correct.
 */

import { describe, it, expect } from 'vitest';

// ── Inline the core constants + logic (iqWorker runs as a Worker so we
//    can't import it directly; we duplicate just the bits under test) ─────────

const FFT_SIZE     = 1024;
const FFT_AVERAGES = 3;
const FFT_STRIDE   = FFT_SIZE >> 2; // 256

const hannWindow = new Float32Array(FFT_SIZE);
for (let i = 0; i < FFT_SIZE; i++) {
  hannWindow[i] = 0.5 * (1 - Math.cos((2 * Math.PI * i) / (FFT_SIZE - 1)));
}

function fftPower(re: Float32Array, im: Float32Array): Float32Array {
  const n = re.length;
  let j = 0;
  for (let i = 1; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      [re[i], re[j]] = [re[j], re[i]];
      [im[i], im[j]] = [im[j], im[i]];
    }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = (-2 * Math.PI) / len;
    const wRe = Math.cos(ang);
    const wIm = Math.sin(ang);
    for (let i = 0; i < n; i += len) {
      let curRe = 1, curIm = 0;
      for (let k = 0; k < len / 2; k++) {
        const uRe = re[i + k];
        const uIm = im[i + k];
        const vRe = re[i + k + len / 2] * curRe - im[i + k + len / 2] * curIm;
        const vIm = re[i + k + len / 2] * curIm + im[i + k + len / 2] * curRe;
        re[i + k] = uRe + vRe;
        im[i + k] = uIm + vIm;
        re[i + k + len / 2] = uRe - vRe;
        im[i + k + len / 2] = uIm - vIm;
        const nextRe = curRe * wRe - curIm * wIm;
        curIm = curRe * wIm + curIm * wRe;
        curRe = nextRe;
      }
    }
  }
  const power = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const mag = Math.sqrt(re[i] * re[i] + im[i] * im[i]) / n;
    power[i] = 20 * Math.log10(Math.max(mag, 1e-10));
  }
  const half = n >> 1;
  const out = new Float32Array(n);
  out.set(power.subarray(half), 0);
  out.set(power.subarray(0, half), half);
  return out;
}

/** Simulates the processFFT function and returns the number of FFT frames emitted. */
function simulateProcessFFT(chunks: Uint8Array[]): number {
  const iqBuf = new Float32Array(FFT_SIZE * 2).fill(0);
  const fftAccum = new Float32Array(FFT_SIZE).fill(0);
  let iqBufIdx = 0;
  let iqBufFilled = 0;
  let fftFrameCount = 0;
  let strideCount = 0;
  let emitted = 0;

  for (const raw of chunks) {
    const n = raw.length & ~1;
    for (let i = 0; i < n; i += 2) {
      iqBuf[iqBufIdx * 2]     = (raw[i]     - 127.5) / 127.5;
      iqBuf[iqBufIdx * 2 + 1] = (raw[i + 1] - 127.5) / 127.5;
      iqBufIdx = (iqBufIdx + 1) % FFT_SIZE;
      if (iqBufFilled < FFT_SIZE) iqBufFilled++;
      if (iqBufFilled < FFT_SIZE) continue;

      strideCount++;
      if (strideCount < FFT_STRIDE) continue;
      strideCount = 0;

      const re = new Float32Array(FFT_SIZE);
      const im = new Float32Array(FFT_SIZE);
      for (let k = 0; k < FFT_SIZE; k++) {
        const idx = (iqBufIdx + k) % FFT_SIZE;
        re[k] = iqBuf[idx * 2]     * hannWindow[k];
        im[k] = iqBuf[idx * 2 + 1] * hannWindow[k];
      }

      const power = fftPower(re, im);
      for (let k = 0; k < FFT_SIZE; k++) fftAccum[k] += power[k];
      fftFrameCount++;

      if (fftFrameCount >= FFT_AVERAGES) {
        fftFrameCount = 0;
        fftAccum.fill(0);
        emitted++;
      }
    }
  }
  return emitted;
}

// ── Waterfall colour LUT (duplicated from WaterfallPanel.tsx) ────────────────

const DB_MIN   = -100;
const DB_MAX   = -50;
const DB_RANGE = DB_MAX - DB_MIN;

function dbToLutIndex(db: number): number {
  const clamped = Math.max(DB_MIN, Math.min(DB_MAX, db));
  return Math.round(((clamped - DB_MIN) / DB_RANGE) * 255);
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('FFT processing rate', () => {
  it('emits multiple FFT frames from a single 65536-byte chunk', () => {
    // Simulate one typical WebSocket message: 65536 bytes of noise
    const chunk = new Uint8Array(65536);
    for (let i = 0; i < chunk.length; i++) chunk[i] = 128 + Math.round((Math.random() - 0.5) * 40);

    const frames = simulateProcessFFT([chunk]);

    // 65536 bytes = 32768 IQ pairs.
    // After filling 1024-pair buffer, we have 31744 pairs driving FFTs.
    // One FFT every FFT_STRIDE=256 pairs → 31744/256 = ~124 FFT windows.
    // Grouped by FFT_AVERAGES=3 → ~41 emitted frames.
    // We want at least 10 frames from a single chunk.
    expect(frames).toBeGreaterThan(10);
    console.log(`FFT frames emitted from one 65536-byte chunk: ${frames}`);
  });

  it('fills 400 waterfall rows in under 5 seconds of simulated data at 2.4 Msps', { timeout: 30_000 }, () => {
    // At 2.4 Msps, 65536 bytes = 32768 IQ pairs ≈ 13.6 ms of data.
    // 5 seconds = 5000 / 13.6 ≈ 367 chunks.
    const CHUNK_SIZE = 65536;
    const CHUNKS_FOR_5_SEC = Math.ceil(5000 / (CHUNK_SIZE / 2 / 2_400_000 * 1000));

    const chunks: Uint8Array[] = [];
    for (let c = 0; c < CHUNKS_FOR_5_SEC; c++) {
      const chunk = new Uint8Array(CHUNK_SIZE);
      for (let i = 0; i < CHUNK_SIZE; i++) chunk[i] = 128;
      chunks.push(chunk);
    }

    const frames = simulateProcessFFT(chunks);
    console.log(`Frames in 5s of simulated data (${CHUNKS_FOR_5_SEC} chunks): ${frames}`);
    // 400 rows needed to fill waterfall
    expect(frames).toBeGreaterThan(400);
  });
});

describe('Waterfall dB → colour mapping', () => {
  it('maps hardware noise floor (-84 dB) to a visible non-zero colour index', () => {
    const idx = dbToLutIndex(-84);
    // -84 is within [-100, -50], so index = round((-84 - -100) / 50 * 255) = round(16/50*255) = 82
    expect(idx).toBe(82);
    expect(idx).toBeGreaterThan(0);  // not black
  });

  it('maps strong signal (-64 dB) to a bright colour index', () => {
    const idx = dbToLutIndex(-64);
    // round((-64 - -100) / 50 * 255) = round(36/50*255) = round(183.6) = 184
    expect(idx).toBe(184);
    expect(idx).toBeGreaterThan(128);  // bright, above midpoint
  });

  it('clamps values below DB_MIN to index 0', () => {
    expect(dbToLutIndex(-120)).toBe(0);
  });

  it('clamps values above DB_MAX to index 255', () => {
    expect(dbToLutIndex(-30)).toBe(255);
  });

  it('noise floor at -84 dB produces opaque blue (not black)', () => {
    // Build the LUT inline
    const lut = new Uint8ClampedArray(256 * 4);
    for (let i = 0; i < 256; i++) {
      let r = 0, g = 0, b = 0;
      if (i < 64) {
        b = Math.round((i / 63) * 200);
      } else if (i < 128) {
        const t = (i - 64) / 63;
        b = Math.round(200 + t * 55);
        g = Math.round(t * 220);
      } else if (i < 192) {
        const t = (i - 128) / 63;
        r = Math.round(t * 255);
        g = Math.round(220 + t * 35);
        b = Math.round(255 * (1 - t));
      } else {
        const t = (i - 192) / 63;
        r = 255;
        g = 255;
        b = Math.round(t * 255);
      }
      lut[i * 4]     = r;
      lut[i * 4 + 1] = g;
      lut[i * 4 + 2] = b;
      lut[i * 4 + 3] = 255;
    }

    const idx = dbToLutIndex(-84);  // 82
    const r = lut[idx * 4];
    const g = lut[idx * 4 + 1];
    const bVal = lut[idx * 4 + 2];
    const a = lut[idx * 4 + 3];

    // Index 82 is in the 64-127 band: blue+green ramp (cyan transition).
    // Key invariants: opaque, has blue component, no red yet.
    expect(a).toBe(255);             // opaque
    expect(bVal).toBeGreaterThan(0); // has blue
    expect(r).toBe(0);               // no red below index 128
    console.log(`-84 dB → LUT[${idx}] = rgba(${r},${g},${bVal},${a})`);
  });
});
