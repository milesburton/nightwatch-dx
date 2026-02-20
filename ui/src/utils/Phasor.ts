import { Complex } from './Complex.js';

export class Phasor {
  private phase: number = 0;
  private readonly angularFreq: number;

  constructor(frequency: number, sampleRate: number) {
    this.angularFreq = (2 * Math.PI * frequency) / sampleRate;
  }

  rotate(): Complex {
    const value = new Complex(Math.cos(this.phase), -Math.sin(this.phase));
    this.phase += this.angularFreq;
    if (this.phase > Math.PI) this.phase -= 2 * Math.PI;
    else if (this.phase < -Math.PI) this.phase += 2 * Math.PI;
    return value;
  }

  reset(): void {
    this.phase = 0;
  }
}
