/**
 * SSTVVISDetector unit tests.
 *
 * Requirement under test:
 *   The detector must buffer audio from the START of the leader tone so that
 *   the emitted Float32Array contains the complete VIS preamble + image data.
 *   SSTVDecoder.detectMode() scans ≥250 ms back from the 1200 Hz break to
 *   confirm a 1900 Hz leader, so any buffer that omits the preamble will cause
 *   detectMode() to fail with "Could not detect SSTV mode".
 *
 * Synthetic audio is FM-modulated: frequency maps to instantaneous amplitude
 * since after FM discriminator the signal is already in Hz (the VIS detector
 * receives frequency-domain samples, not time-domain audio).
 *
 * VIS standard preamble (all timings in seconds at 24 000 Hz):
 *   Leader:    ≥ 300 ms of 1900 Hz  (≥10 × 30 ms windows)
 *   Break:       10 ms of 1200 Hz
 *   Start bit:   30 ms of 1900 Hz
 *   VIS bits:  8 × 30 ms  (1100 Hz = '1',  1300 Hz = '0')
 *   Stop bit:    30 ms of 1200 Hz
 *
 * Robot 36 VIS code = 0x08 = 0b00001000  (LSB first, bit7 = even parity = 1)
 *   bit0=0(1300) bit1=0(1300) bit2=0(1300) bit3=1(1100)
 *   bit4=0(1300) bit5=0(1300) bit6=0(1300) bit7=1(1100)
 */

import { describe, expect, it } from 'vitest';
import { SSTVVISDetector } from './SSTVVISDetector.js';

const SAMPLE_RATE = 24_000;

// ── Tone synthesis ─────────────────────────────────────────────────────────────

/** Fill `buf[offset..offset+samples)` with the given constant frequency value. */
function fillTone(buf: Float32Array, offset: number, samples: number, freqHz: number): void {
  for (let i = 0; i < samples; i++) {
    buf[offset + i] = freqHz;
  }
}

/** Duration in samples for a given time in seconds. */
function dur(seconds: number): number {
  return Math.round(seconds * SAMPLE_RATE);
}

// ── Robot 36 VIS preamble builder ─────────────────────────────────────────────

/**
 * Build a synthetic FM-discriminated audio buffer containing the standard
 * Robot 36 VIS preamble (0x08).
 *
 * Robot 36 VIS code = 0x08:
 *   7 data bits (LSB first): 0 0 0 1 0 0 0  → 1300 1300 1300 1100 1300 1300 1300
 *   parity bit (bit 7):       1               → 1100
 */
function buildRobot36Preamble(leaderMs = 350): Float32Array {
  // VIS bit frequencies: 1100=bit1, 1300=bit0
  const visBitFreqs = [
    1300, // bit0 = 0
    1300, // bit1 = 0
    1300, // bit2 = 0
    1100, // bit3 = 1
    1300, // bit4 = 0
    1300, // bit5 = 0
    1300, // bit6 = 0
    1100, // bit7 = parity (even parity of 1 set bit → 1)
  ];

  const leaderSamples    = dur(leaderMs / 1000);
  const breakSamples     = dur(0.010);
  const startBitSamples  = dur(0.030);
  const visBitSamples    = dur(0.030);
  const stopBitSamples   = dur(0.030);

  const totalPreamble =
    leaderSamples +
    breakSamples +
    startBitSamples +
    8 * visBitSamples +
    stopBitSamples;

  const buf = new Float32Array(totalPreamble);
  let pos = 0;

  fillTone(buf, pos, leaderSamples, 1900);   pos += leaderSamples;
  fillTone(buf, pos, breakSamples,  1200);   pos += breakSamples;
  fillTone(buf, pos, startBitSamples, 1900); pos += startBitSamples;
  for (const f of visBitFreqs) {
    fillTone(buf, pos, visBitSamples, f);    pos += visBitSamples;
  }
  fillTone(buf, pos, stopBitSamples, 1200);  pos += stopBitSamples;

  return buf;
}

// ── Tests ──────────────────────────────────────────────────────────────────────

describe('SSTVVISDetector', () => {
  describe('state machine — does not trigger on noise', () => {
    it('returns null for silence', () => {
      const det = new SSTVVISDetector(SAMPLE_RATE);
      const silence = new Float32Array(dur(1)).fill(0);
      expect(det.push(silence)).toBeNull();
    });

    it('returns null for a short 1900 Hz burst (< 300 ms leader)', () => {
      const det = new SSTVVISDetector(SAMPLE_RATE);
      // Only 5 windows × 30ms = 150 ms — not enough for ≥10 windows
      const short = new Float32Array(dur(0.15)).fill(1900);
      expect(det.push(short)).toBeNull();
    });

    it('returns null for preamble with unknown VIS code', () => {
      // Use VIS code 0x7F which is not in VIS_DURATIONS
      const preamble = buildRobot36Preamble();
      // Corrupt the VIS bits so the code becomes unknown (all 1100 Hz → code 0x7F)
      const leaderSamples = dur(0.350);
      const breakSamples  = dur(0.010);
      const startSamples  = dur(0.030);
      const visBitSamples = dur(0.030);
      for (let b = 0; b < 7; b++) {
        const start = leaderSamples + breakSamples + startSamples + b * visBitSamples;
        for (let i = 0; i < visBitSamples; i++) preamble[start + i] = 1100;
      }
      const det = new SSTVVISDetector(SAMPLE_RATE);
      expect(det.push(preamble)).toBeNull();
    });
  });

  describe('preamble detection — Robot 36 (VIS 0x08)', () => {
    it('returns null before the frame is complete (preamble-only input)', () => {
      const det = new SSTVVISDetector(SAMPLE_RATE);
      const preamble = buildRobot36Preamble();
      // No image data yet — detector should enter BUFFERING but not emit
      expect(det.push(preamble)).toBeNull();
    });

    it('emits a Float32Array once the full frame duration has been received', { timeout: 30_000 }, () => {
      const det = new SSTVVISDetector(SAMPLE_RATE);
      const preamble = buildRobot36Preamble();

      // Robot 36 frame duration ≈ 72 s — that's too long for a unit test.
      // We just need to confirm the detector emits *something* after
      // the preamble + enough padding to satisfy frameSamplesRequired.
      // Feed preamble first to enter BUFFERING, then query how many more
      // samples are needed via a large silent pad.
      det.push(preamble);

      // Feed enough silence to complete the frame buffer
      // (Robot 36 ≈ 240 * (0.009 + 0.003 + 0.15) s ≈ 38.9 s → ~934k samples)
      // We chunk it to avoid OOM in tests
      const CHUNK = 10_000;
      const MAX_CHUNKS = 5000; // ~50M samples upper bound, well above 934k
      let result: Float32Array | null = null;
      for (let c = 0; c < MAX_CHUNKS && result === null; c++) {
        result = det.push(new Float32Array(CHUNK).fill(1500));
      }
      expect(result).not.toBeNull();
      expect(result).toBeInstanceOf(Float32Array);
    });

    /**
     * KEY REQUIREMENT: the emitted buffer must contain the preamble.
     *
     * SSTVDecoder.detectMode() requires ≥250 ms of 1900 Hz leader before
     * the 1200 Hz break, and scans up to 200 ms before the break position.
     * If the detector strips the preamble and only emits image audio, every
     * call to SSTVDecoder.decodeSamples() will throw "Could not detect SSTV mode".
     */
    it('emitted buffer contains the 1900 Hz leader tone at its start', { timeout: 30_000 }, () => {
      const det = new SSTVVISDetector(SAMPLE_RATE);
      const preamble = buildRobot36Preamble(350); // 350 ms leader

      det.push(preamble);

      let result: Float32Array | null = null;
      const CHUNK = 10_000;
      for (let c = 0; c < 5000 && result === null; c++) {
        result = det.push(new Float32Array(CHUNK).fill(1500));
      }
      if (!result) throw new Error('Detector never emitted a frame');

      // The first 300 ms (7200 samples) of the output must be ~1900 Hz
      // (the leader tone we wrote into the preamble).
      const leaderCheckSamples = dur(0.3); // 7200
      let sumLeader = 0;
      for (let i = 0; i < leaderCheckSamples; i++) sumLeader += result[i];
      const avgLeader = sumLeader / leaderCheckSamples;

      expect(avgLeader).toBeCloseTo(1900, -1); // within ±10 Hz of 1900
    });

    it('emitted buffer contains the 1200 Hz break after the leader', { timeout: 30_000 }, () => {
      const det = new SSTVVISDetector(SAMPLE_RATE);
      det.push(buildRobot36Preamble(350));

      let result: Float32Array | null = null;
      for (let c = 0; c < 5000 && result === null; c++) {
        result = det.push(new Float32Array(10_000).fill(1500));
      }
      if (!result) throw new Error('Detector never emitted a frame');

      // Break starts at offset = leaderSamples = 350ms × 24000 = 8400
      const breakStart = dur(0.350);
      const breakLen   = dur(0.010); // 240 samples
      let sumBreak = 0;
      for (let i = 0; i < breakLen; i++) sumBreak += result[breakStart + i];
      const avgBreak = sumBreak / breakLen;

      expect(avgBreak).toBeCloseTo(1200, -1);
    });

    it('emitted buffer length matches the expected frame size', { timeout: 30_000 }, () => {
      const det = new SSTVVISDetector(SAMPLE_RATE);
      det.push(buildRobot36Preamble(350));

      let result: Float32Array | null = null;
      for (let c = 0; c < 5000 && result === null; c++) {
        result = det.push(new Float32Array(10_000).fill(1500));
      }
      if (!result) throw new Error('Detector never emitted a frame');

      // The emitted buffer = preamble (leader→stop bit) + image data.
      // Preamble: 350ms leader + 10ms break + 30ms start + 8×30ms VIS + 30ms stop = 650ms
      const preambleDuration = 0.350 + 0.010 + 0.030 + 8 * 0.030 + 0.030; // 0.660 s
      const preambleSamples  = Math.round(preambleDuration * SAMPLE_RATE);

      // Robot 36 image duration: VIS_DURATIONS[8] = 240 * (0.009 + 0.003 + 0.15) ≈ 38.88 s
      const imageDuration = 240 * (0.009 + 0.003 + 0.15);
      const imageSamples  = Math.ceil(imageDuration * SAMPLE_RATE);

      const expectedMin = preambleSamples + imageSamples - 1;
      const expectedMax = preambleSamples + imageSamples + SAMPLE_RATE; // +1s slack for window rounding

      expect(result.length).toBeGreaterThanOrEqual(expectedMin);
      expect(result.length).toBeLessThanOrEqual(expectedMax);
    });
  });

  describe('reset behaviour', () => {
    it('resets and re-detects after emitting a frame', { timeout: 60_000 }, () => {
      const det = new SSTVVISDetector(SAMPLE_RATE);
      det.push(buildRobot36Preamble(350));

      // Complete first frame
      let result: Float32Array | null = null;
      for (let c = 0; c < 5000 && result === null; c++) {
        result = det.push(new Float32Array(10_000).fill(1500));
      }
      expect(result).not.toBeNull();

      // Detector should have reset — a second preamble + data should emit again
      det.push(buildRobot36Preamble(350));
      let result2: Float32Array | null = null;
      for (let c = 0; c < 5000 && result2 === null; c++) {
        result2 = det.push(new Float32Array(10_000).fill(1500));
      }
      expect(result2).not.toBeNull();
    });
  });

  describe('chunked input', () => {
    it('detects preamble when data arrives in small chunks (240 samples = 10 ms)', { timeout: 30_000 }, () => {
      const det = new SSTVVISDetector(SAMPLE_RATE);
      const preamble = buildRobot36Preamble(350);

      // Feed preamble in 240-sample (10 ms) chunks
      const CHUNK = 240;
      for (let i = 0; i < preamble.length; i += CHUNK) {
        det.push(preamble.subarray(i, Math.min(i + CHUNK, preamble.length)));
      }

      // Complete the frame
      let result: Float32Array | null = null;
      for (let c = 0; c < 5000 && result === null; c++) {
        result = det.push(new Float32Array(CHUNK).fill(1500));
      }
      expect(result).not.toBeNull();

      // Preamble should still be present
      const leaderCheckSamples = dur(0.3);
      let sum = 0;
      if (!result) throw new Error('Detector never emitted a frame');
      for (let i = 0; i < leaderCheckSamples; i++) sum += result[i];
      expect(sum / leaderCheckSamples).toBeCloseTo(1900, -1);
    });
  });
});
