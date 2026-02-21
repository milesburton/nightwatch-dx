import { describe, expect, it } from 'vitest';
import { Phasor } from './Phasor.js';

describe('Phasor', () => {
  describe('unity magnitude', () => {
    it('every sample has magnitude ≈ 1', () => {
      const p = new Phasor(1000, 24_000);
      for (let i = 0; i < 100; i++) {
        const v = p.rotate();
        expect(v.abs()).toBeCloseTo(1, 10);
      }
    });

    it('holds unity after many rotations (phase wrap)', () => {
      const p = new Phasor(100, 1000); // 10 cycles per 100 samples
      for (let i = 0; i < 10_000; i++) {
        const v = p.rotate();
        expect(v.abs()).toBeCloseTo(1, 5);
      }
    });
  });

  describe('phase progression', () => {
    it('advances by angularFreq each call', () => {
      const freq = 1000;
      const sr   = 24_000;
      const dPhi = (2 * Math.PI * freq) / sr;

      const p = new Phasor(freq, sr);
      const v0 = p.rotate(); // phase was 0, returns cos(0) + i·(-sin(0))
      const v1 = p.rotate(); // phase was dPhi

      // The angle between consecutive samples equals dPhi
      const angle0 = Math.atan2(-v0.imag, v0.real); // v0 is (cos(0), -sin(0)) → arg = 0
      const angle1 = Math.atan2(-v1.imag, v1.real);
      expect(angle1 - angle0).toBeCloseTo(dPhi);
    });

    it('completes exactly one cycle for f=sr samples', () => {
      // 100 Hz at sr=100 → 1 full cycle in 100 samples
      const p = new Phasor(100, 100);
      const v0 = p.rotate();
      for (let i = 1; i < 100; i++) p.rotate();
      const v100 = p.rotate();
      // After 100 steps we're back to the same phase
      expect(v100.real).toBeCloseTo(v0.real, 5);
      expect(v100.imag).toBeCloseTo(v0.imag, 5);
    });

    it('zero frequency returns (1,0) every time', () => {
      const p = new Phasor(0, 24_000);
      for (let i = 0; i < 10; i++) {
        const v = p.rotate();
        expect(v.real).toBeCloseTo(1);
        expect(v.imag).toBeCloseTo(0);
      }
    });
  });

  describe('reset', () => {
    it('restores initial phase after many rotations', () => {
      const p = new Phasor(1000, 24_000);
      const v0 = p.rotate();
      for (let i = 0; i < 500; i++) p.rotate();
      p.reset();
      const vR = p.rotate();
      expect(vR.real).toBeCloseTo(v0.real, 10);
      expect(vR.imag).toBeCloseTo(v0.imag, 10);
    });
  });

  describe('mixing property', () => {
    // Phasor(f) returns e^(-jωt). Mixing an input at +f Hz (e^(+jωt))
    // with the Phasor output gives e^(-jωt) · e^(+jωt) = 1 (DC).
    it('mixing an input tone at f with Phasor(f) yields DC', () => {
      const SR   = 48_000;
      const FREQ = 1_000;
      const dPhi = (2 * Math.PI * FREQ) / SR;
      const pLO  = new Phasor(FREQ, SR);

      let sumReal = 0;
      let sumImag = 0;
      const N = 480; // 10 ms
      for (let i = 0; i < N; i++) {
        // Synthesise e^(+jωt) input
        const sigReal = Math.cos(i * dPhi);
        const sigImag = Math.sin(i * dPhi);
        const lo      = pLO.rotate();          // e^(-jωt)
        // (a + jb)(c + jd) with signal = (sigReal, sigImag), lo = (lo.real, lo.imag)
        sumReal += sigReal * lo.real - sigImag * lo.imag;
        sumImag += sigReal * lo.imag + sigImag * lo.real;
      }
      // Average should be ≈ (1, 0) — pure DC
      expect(sumReal / N).toBeCloseTo(1, 1);
      expect(sumImag / N).toBeCloseTo(0, 1);
    });
  });
});
