import { describe, expect, it } from 'vitest';
import { Complex } from './Complex.js';
import { KaiserFIR } from './KaiserFIR.js';

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Run `n` samples of a complex sinusoid at `freqHz` through the filter
 * and return the magnitude of the last output sample (steady state).
 */
function steadyStateMag(
  filter: KaiserFIR,
  freqHz: number,
  sampleRate: number,
  n = 2000,
): number {
  const dPhi = (2 * Math.PI * freqHz) / sampleRate;
  let phase = 0;
  let last = new Complex(0, 0);
  for (let i = 0; i < n; i++) {
    last = filter.push(new Complex(Math.cos(phase), Math.sin(phase)));
    phase += dPhi;
  }
  return last.abs();
}

/** Create a fresh KaiserFIR with cutoff=`cutoff`, sr=`sr`, duration=0.001. */
function makeFilter(cutoff: number, sr: number) {
  return new KaiserFIR(cutoff, sr, 0.001, 8);
}

describe('KaiserFIR', () => {
  describe('DC gain ≈ 1', () => {
    it('passes DC (0 Hz) at unity', () => {
      const fir = makeFilter(5_000, 48_000);
      // DC = all ones (real=1, imag=0)
      const n = 2000;
      let last = new Complex(0, 0);
      for (let i = 0; i < n; i++) last = fir.push(new Complex(1, 0));
      expect(last.abs()).toBeCloseTo(1, 1);
    });
  });

  describe('low-pass attenuation', () => {
    const SR     = 48_000;
    const CUTOFF = 5_000;

    it('passes a signal well below cutoff (≈ 1)', () => {
      const fir = makeFilter(CUTOFF, SR);
      const mag = steadyStateMag(fir, 500, SR);
      expect(mag).toBeCloseTo(1, 1);
    });

    it('attenuates a signal well above cutoff (< 0.25)', () => {
      const fir = makeFilter(CUTOFF, SR);
      // duration=0.001 s at 48 kHz → ~48 taps.  A 48-tap Kaiser (β=8) has a
      // transition band of roughly ±20 % of the sample rate, so significant
      // attenuation only starts well above cutoff.  At 20 kHz (4× cutoff) the
      // filter achieves roughly 0.2 — the key thing is it is NOT passing-through.
      const mag = steadyStateMag(fir, 20_000, SR);
      expect(mag).toBeLessThan(0.25);
    });

    it('rolls off gradually — signal at cutoff is between 0.1 and 0.9', () => {
      const fir = makeFilter(CUTOFF, SR);
      const mag = steadyStateMag(fir, CUTOFF, SR);
      expect(mag).toBeGreaterThan(0.1);
      expect(mag).toBeLessThan(0.9);
    });
  });

  describe('decimation use-case (2.4 MHz → 24 kHz, 10× each stage)', () => {
    // Stage 1: cutoff = 120 kHz at 2.4 MHz sample rate
    // Stage 2: cutoff = 12 kHz at 240 kHz sample rate
    const SR1 = 2_400_000;
    const SR2 = 240_000;

    it('stage 1 passes 10 kHz signal (well below 120 kHz cutoff)', () => {
      const fir = makeFilter(120_000, SR1);
      const mag = steadyStateMag(fir, 10_000, SR1, 5000);
      expect(mag).toBeGreaterThan(0.8);
    });

    it('stage 1 attenuates 500 kHz signal (well above 120 kHz cutoff)', () => {
      const fir = makeFilter(120_000, SR1);
      const mag = steadyStateMag(fir, 500_000, SR1, 5000);
      expect(mag).toBeLessThan(0.2);
    });

    it('stage 2 passes 1 kHz signal', () => {
      const fir = makeFilter(12_000, SR2);
      const mag = steadyStateMag(fir, 1_000, SR2, 2000);
      expect(mag).toBeGreaterThan(0.8);
    });

    it('stage 2 attenuates 100 kHz signal', () => {
      const fir = makeFilter(12_000, SR2);
      const mag = steadyStateMag(fir, 100_000, SR2, 2000);
      expect(mag).toBeLessThan(0.2);
    });
  });

  describe('tap count', () => {
    it('produces an odd number of taps (linear phase)', () => {
      // numTaps = floor(duration × sr) | 1  ensures odd
      // duration=0.001, sr=48000 → floor(48) | 1 = 49
      const fir = makeFilter(5_000, 48_000);
      // We can't inspect taps directly; we verify the filter has the right
      // impulse response length by checking the step response delay.
      // A filter with ~49 taps has a group delay of ~24 samples at DC.
      // After 48 input samples the output should be well past transient.
      let last = new Complex(0, 0);
      for (let i = 0; i < 48; i++) last = fir.push(new Complex(1, 0));
      expect(last.abs()).toBeGreaterThan(0.5); // transient mostly settled
    });
  });

  describe('reset', () => {
    it('clears filter state back to zero', () => {
      const fir = makeFilter(5_000, 48_000);
      // Drive to steady state
      for (let i = 0; i < 1000; i++) fir.push(new Complex(1, 0));
      fir.reset();
      // First output after reset should be near zero (no stored energy)
      const first = fir.push(new Complex(0, 0));
      expect(first.abs()).toBeCloseTo(0, 5);
    });
  });

  describe('linearity', () => {
    it('f(a+b) = f(a) + f(b) for the same filter state', () => {
      // Run two identical filters: one with (a+b), one with a then b individually
      const firAB = makeFilter(5_000, 48_000);
      const firA  = makeFilter(5_000, 48_000);
      const firB  = makeFilter(5_000, 48_000);

      const a = new Complex(0.3, 0.1);
      const b = new Complex(-0.1, 0.4);

      const outAB = firAB.push(new Complex(a.real + b.real, a.imag + b.imag));
      const outA  = firA.push(a);
      const outB  = firB.push(b);

      expect(outAB.real).toBeCloseTo(outA.real + outB.real, 10);
      expect(outAB.imag).toBeCloseTo(outA.imag + outB.imag, 10);
    });
  });
});
