import { Complex } from './Complex.js';

export class KaiserFIR {
  private readonly taps: number[];
  private readonly buffer: Complex[];
  private bufferIndex: number = 0;

  constructor(cutoffFreq: number, sampleRate: number, duration: number, beta: number = 8.0) {
    const numTaps = Math.floor(duration * sampleRate) | 1;
    this.taps = new Array<number>(numTaps);

    const normalizedCutoff = (2 * cutoffFreq) / sampleRate;
    const center = (numTaps - 1) / 2;

    for (let i = 0; i < numTaps; i++) {
      const x = i - center;
      let sinc: number;
      if (x === 0) {
        sinc = normalizedCutoff;
      } else {
        const arg = Math.PI * x * normalizedCutoff;
        sinc = Math.sin(arg) / arg;
      }
      this.taps[i] = sinc * this.kaiser(i, numTaps, beta);
    }

    const sum = this.taps.reduce((a, b) => a + b, 0);
    for (let i = 0; i < numTaps; i++) {
      this.taps[i] /= sum;
    }

    this.buffer = Array.from({ length: numTaps }, () => new Complex(0, 0));
  }

  private kaiser(n: number, N: number, beta: number): number {
    const alpha = (N - 1) / 2;
    const arg = beta * Math.sqrt(1 - ((n - alpha) / alpha) ** 2);
    return this.besselI0(arg) / this.besselI0(beta);
  }

  private besselI0(x: number): number {
    let sum = 1.0;
    let term = 1.0;
    const threshold = 1e-12;
    for (let k = 1; k < 50; k++) {
      term *= (x / (2 * k)) * (x / (2 * k));
      sum += term;
      if (term < threshold * sum) break;
    }
    return sum;
  }

  push(sample: Complex): Complex {
    this.buffer[this.bufferIndex] = sample;
    this.bufferIndex = (this.bufferIndex + 1) % this.buffer.length;

    let real = 0;
    let imag = 0;
    for (let i = 0; i < this.taps.length; i++) {
      const bufIdx = (this.bufferIndex + i) % this.buffer.length;
      const bufSample = this.buffer[bufIdx];
      const tap = this.taps[i];
      real += bufSample.real * tap;
      imag += bufSample.imag * tap;
    }
    return new Complex(real, imag);
  }

  reset(): void {
    this.buffer.fill(new Complex(0, 0));
    this.bufferIndex = 0;
  }
}
