import { describe, expect, it } from 'vitest';
import { Complex } from './Complex.js';


describe('Complex', () => {
  describe('construction', () => {
    it('defaults to zero', () => {
      const z = new Complex();
      expect(z.real).toBe(0);
      expect(z.imag).toBe(0);
    });

    it('stores given real and imag', () => {
      const z = new Complex(3, -4);
      expect(z.real).toBe(3);
      expect(z.imag).toBe(-4);
    });
  });

  describe('mul', () => {
    it('(1+0i) × (1+0i) = 1', () => {
      const r = new Complex(1, 0).mul(new Complex(1, 0));
      expect(r.real).toBeCloseTo(1);
      expect(r.imag).toBeCloseTo(0);
    });

    it('(1+0i) × (0+1i) = 0+1i', () => {
      const r = new Complex(1, 0).mul(new Complex(0, 1));
      expect(r.real).toBeCloseTo(0);
      expect(r.imag).toBeCloseTo(1);
    });

    it('(0+1i) × (0+1i) = -1', () => {
      const r = new Complex(0, 1).mul(new Complex(0, 1));
      expect(r.real).toBeCloseTo(-1);
      expect(r.imag).toBeCloseTo(0);
    });

    it('(3+4i) × (1-2i) = 11-2i', () => {
      // (3+4i)(1-2i) = 3-6i+4i-8i² = 3-2i+8 = 11-2i
      const r = new Complex(3, 4).mul(new Complex(1, -2));
      expect(r.real).toBeCloseTo(11);
      expect(r.imag).toBeCloseTo(-2);
    });

    it('is not commutative in general (but magnitudes equal)', () => {
      const a = new Complex(3, 4);
      const b = new Complex(1, -2);
      const ab = a.mul(b);
      const ba = b.mul(a);
      expect(ab.abs()).toBeCloseTo(ba.abs());
    });
  });

  describe('add', () => {
    it('(1+2i) + (3+4i) = 4+6i', () => {
      const r = new Complex(1, 2).add(new Complex(3, 4));
      expect(r.real).toBe(4);
      expect(r.imag).toBe(6);
    });

    it('adding zero returns same value', () => {
      const z = new Complex(5, -3);
      const r = z.add(new Complex(0, 0));
      expect(r.real).toBe(5);
      expect(r.imag).toBe(-3);
    });
  });

  describe('scale', () => {
    it('scale by 0 gives zero', () => {
      const r = new Complex(3, 4).scale(0);
      expect(r.real).toBe(0);
      expect(r.imag).toBe(0);
    });

    it('scale by 1 is identity', () => {
      const r = new Complex(3, -4).scale(1);
      expect(r.real).toBe(3);
      expect(r.imag).toBe(-4);
    });

    it('scale by -1 negates both parts', () => {
      const r = new Complex(3, -4).scale(-1);
      expect(r.real).toBe(-3);
      expect(r.imag).toBe(4);
    });

    it('scale by 2 doubles magnitude', () => {
      const z = new Complex(3, 4);
      expect(z.scale(2).abs()).toBeCloseTo(z.abs() * 2);
    });
  });

  describe('abs', () => {
    it('|0+0i| = 0', () => expect(new Complex(0, 0).abs()).toBe(0));
    it('|3+4i| = 5', () => expect(new Complex(3, 4).abs()).toBeCloseTo(5));
    it('|1+0i| = 1', () => expect(new Complex(1, 0).abs()).toBeCloseTo(1));
    it('|0+1i| = 1', () => expect(new Complex(0, 1).abs()).toBeCloseTo(1));
    it('|-3-4i| = 5', () => expect(new Complex(-3, -4).abs()).toBeCloseTo(5));
  });

  describe('arg', () => {
    it('arg of real positive = 0', () => expect(new Complex(1, 0).arg()).toBeCloseTo(0));
    it('arg of positive imaginary = π/2', () => expect(new Complex(0, 1).arg()).toBeCloseTo(Math.PI / 2));
    it('arg of negative real = ±π', () => {
      const a = new Complex(-1, 0).arg();
      expect(Math.abs(a)).toBeCloseTo(Math.PI);
    });
    it('arg of negative imaginary = -π/2', () => expect(new Complex(0, -1).arg()).toBeCloseTo(-Math.PI / 2));
  });

  describe('fromPolar', () => {
    it('mag=1 phase=0 → (1, 0)', () => {
      const z = Complex.fromPolar(1, 0);
      expect(z.real).toBeCloseTo(1);
      expect(z.imag).toBeCloseTo(0);
    });

    it('mag=1 phase=π/2 → (0, 1)', () => {
      const z = Complex.fromPolar(1, Math.PI / 2);
      expect(z.real).toBeCloseTo(0);
      expect(z.imag).toBeCloseTo(1);
    });

    it('mag=5 phase=arg(3+4i) round-trips', () => {
      const orig = new Complex(3, 4);
      const z = Complex.fromPolar(orig.abs(), orig.arg());
      expect(z.real).toBeCloseTo(orig.real);
      expect(z.imag).toBeCloseTo(orig.imag);
    });

    it('preserves magnitude', () => {
      const z = Complex.fromPolar(7, 1.23);
      expect(z.abs()).toBeCloseTo(7);
    });
  });

  describe('mul preserves magnitude', () => {
    it('|a × b| = |a| × |b|', () => {
      const a = new Complex(3, 4);   // |a| = 5
      const b = new Complex(1, -2); // |b| = √5
      expect(a.mul(b).abs()).toBeCloseTo(a.abs() * b.abs());
    });
  });
});
