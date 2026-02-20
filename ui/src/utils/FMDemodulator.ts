import { Complex } from './Complex.js';
import { KaiserFIR } from './KaiserFIR.js';
import { Phasor } from './Phasor.js';

export class FMDemodulator {
  private readonly phasor: Phasor;
  private readonly lowpass: KaiserFIR;
  private prevPhase: number = 0;
  private readonly scale: number;

  constructor(centerFreq: number, bandwidth: number, sampleRate: number) {
    this.phasor = new Phasor(centerFreq, sampleRate);
    this.lowpass = new KaiserFIR(bandwidth / 2, sampleRate, 0.002, 8.0);
    this.scale = sampleRate / (Math.PI * bandwidth);
  }

  demodulate(sample: number): number {
    const basebandSample = new Complex(sample, 0).mul(this.phasor.rotate());
    const filtered = this.lowpass.push(basebandSample);
    const phase = filtered.arg();
    let delta = phase - this.prevPhase;
    if (delta > Math.PI) delta -= 2 * Math.PI;
    else if (delta < -Math.PI) delta += 2 * Math.PI;
    this.prevPhase = phase;
    return Math.max(-1, Math.min(1, this.scale * delta));
  }

  demodulateAll(samples: Float32Array | number[]): Float32Array {
    const output = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i++) {
      output[i] = this.demodulate(samples[i]);
    }
    return output;
  }

  reset(): void {
    this.phasor.reset();
    this.lowpass.reset();
    this.prevPhase = 0;
  }
}
