export class Complex {
  constructor(
    public real: number = 0,
    public imag: number = 0
  ) {}

  mul(other: Complex): Complex {
    return new Complex(
      this.real * other.real - this.imag * other.imag,
      this.real * other.imag + this.imag * other.real
    );
  }

  add(other: Complex): Complex {
    return new Complex(this.real + other.real, this.imag + other.imag);
  }

  scale(scalar: number): Complex {
    return new Complex(this.real * scalar, this.imag * scalar);
  }

  arg(): number {
    return Math.atan2(this.imag, this.real);
  }

  abs(): number {
    return Math.sqrt(this.real * this.real + this.imag * this.imag);
  }

  static fromPolar(magnitude: number, phase: number): Complex {
    return new Complex(magnitude * Math.cos(phase), magnitude * Math.sin(phase));
  }
}
