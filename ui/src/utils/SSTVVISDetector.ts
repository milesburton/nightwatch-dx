/**
 * SSTVVISDetector — Goertzel-based VIS code detector for SSTV auto-detection.
 *
 * Detects the standard SSTV VIS preamble in a demodulated audio stream
 * (output of FM discriminator on the IQ worker's SSTV signal chain):
 *
 *   Leader   : 300 ms of 1900 Hz tone
 *   Break    :  10 ms of 1200 Hz
 *   Start bit:  30 ms of 1900 Hz
 *   VIS bits : 8 × 30 ms (bit1 = 1100 Hz, bit0 = 1300 Hz)
 *   Stop bit :  30 ms of 1200 Hz
 *
 * Once the stop bit is confirmed, the detector switches to BUFFERING mode and
 * accumulates exactly `frameSamples` more samples, then emits the complete
 * Float32Array for decoding.
 *
 * If the VIS code is not in the known-modes table, the detector resets.
 *
 * Audio sample rate assumed: 24 000 Hz (audio output of the SSTV signal chain).
 */

// Known SSTV modes: visCode → total frame duration in seconds
// Durations are conservative upper bounds (sync + image) so we capture the full frame.
const VIS_DURATIONS: Record<number, number> = {
  0x08: 240 * (0.009 + 0.003 + 0.15),    // Robot 36   ~38.9 s
  0x5f: 496 * (0.02  + 0.00208 + 0.532), // PD 120    ~274 s
  0x2c: 256 * (0.004862 + 0.000572 + 3 * 0.146 + 2 * 0.000572), // Martin M1 ~116 s
  0x3c: 256 * (0.009 + 0.0015 + 3 * 0.138 + 0.0015),            // Scottie S1 ~108 s
};

// ── Goertzel algorithm ────────────────────────────────────────────────────────

/**
 * Compute the Goertzel power at `targetHz` for a block of `samples`.
 * Returns normalised magnitude squared (not dB).
 */
function goertzelPower(samples: Float32Array, sampleRate: number, targetHz: number): number {
  const k     = Math.round(samples.length * targetHz / sampleRate);
  const omega = (2 * Math.PI * k) / samples.length;
  const coeff = 2 * Math.cos(omega);
  let s0 = 0, s1 = 0, s2 = 0;
  for (const x of samples) {
    s0 = x + coeff * s1 - s2;
    s2 = s1;
    s1 = s0;
  }
  const power = s1 * s1 + s2 * s2 - coeff * s1 * s2;
  return power / (samples.length * samples.length);
}

/** Classify a 30ms window as the dominant tone (1100, 1200, 1300, or 1900 Hz). */
function dominantTone(samples: Float32Array, sampleRate: number): number {
  const freqs  = [1100, 1200, 1300, 1900];
  let bestFreq = 0;
  let bestPow  = -1;
  for (const f of freqs) {
    const p = goertzelPower(samples, sampleRate, f);
    if (p > bestPow) { bestPow = p; bestFreq = f; }
  }
  return bestFreq;
}

// ── State machine ─────────────────────────────────────────────────────────────

type State = 'IDLE' | 'LEADER' | 'BREAK' | 'START' | 'VIS_BITS' | 'STOP' | 'BUFFERING';

export class SSTVVISDetector {
  private readonly sampleRate: number;

  // Window size: 30 ms for VIS bits (standard), also used for break/start
  private readonly winSize: number;

  // Leader detection: we need ≥ 10 consecutive 1900 Hz windows (~300 ms)
  private readonly leaderRequired = 10;

  private state: State = 'IDLE';
  private leaderCount  = 0;
  private visBits: number[] = [];
  private frameBuffer: Float32Array | null = null;
  private frameSamplesRequired = 0;
  private frameSamplesCollected = 0;

  // Sliding window accumulator
  private readonly windowBuf: Float32Array;
  private windowFill = 0;

  constructor(sampleRate = 24_000) {
    this.sampleRate = sampleRate;
    this.winSize    = Math.round(0.030 * sampleRate);   // 30 ms window
    this.windowBuf  = new Float32Array(this.winSize);
  }

  /**
   * Push audio samples into the detector.
   * Returns a complete Float32Array frame when one is detected, otherwise null.
   */
  push(samples: Float32Array): Float32Array | null {
    for (let i = 0; i < samples.length; i++) {
      if (this.state === 'BUFFERING') {
        this.frameBuffer![this.frameSamplesCollected++] = samples[i];
        if (this.frameSamplesCollected >= this.frameSamplesRequired) {
          const out = this.frameBuffer!;
          this.reset();
          return out;
        }
        continue;
      }

      // Fill the sliding window
      this.windowBuf[this.windowFill++] = samples[i];
      if (this.windowFill < this.winSize) continue;
      this.windowFill = 0;

      // We have a complete window — classify it
      this.processWindow();
    }
    return null;
  }

  private processWindow(): void {
    const tone = dominantTone(this.windowBuf, this.sampleRate);

    switch (this.state) {
      case 'IDLE':
        if (tone === 1900) {
          this.leaderCount = 1;
          this.state = 'LEADER';
        }
        break;

      case 'LEADER':
        if (tone === 1900) {
          this.leaderCount++;
        } else if (tone === 1200 && this.leaderCount >= this.leaderRequired) {
          this.state = 'BREAK';
        } else {
          this.reset();
        }
        break;

      case 'BREAK':
        if (tone === 1900) {
          this.state = 'START';
        } else {
          this.reset();
        }
        break;

      case 'START':
        // Start bit seen; now collect 8 VIS data bits
        this.visBits = [];
        this.state = 'VIS_BITS';
        this.processVISBit(tone);
        break;

      case 'VIS_BITS':
        this.processVISBit(tone);
        break;

      case 'STOP':
        if (tone === 1200) {
          // Stop bit confirmed — parse VIS code
          const visCode = this.parseVISCode();
          const duration = VIS_DURATIONS[visCode];
          if (duration !== undefined) {
            const needed = Math.ceil(duration * this.sampleRate);
            this.frameBuffer = new Float32Array(needed);
            this.frameSamplesRequired  = needed;
            this.frameSamplesCollected = 0;
            this.state = 'BUFFERING';
          } else {
            this.reset();
          }
        } else {
          this.reset();
        }
        break;

      case 'BUFFERING':
        // handled in push() directly
        break;
    }
  }

  private processVISBit(tone: number): void {
    // VIS bits: 1100 Hz = '1', 1300 Hz = '0'
    if (tone === 1100) {
      this.visBits.push(1);
    } else if (tone === 1300) {
      this.visBits.push(0);
    } else if (tone === 1200 && this.visBits.length === 8) {
      // Got stop bit while expecting more — treat as early STOP
      this.state = 'STOP';
      this.processWindow();
      return;
    } else {
      this.reset();
      return;
    }

    if (this.visBits.length === 8) {
      this.state = 'STOP';
    }
  }

  /** Parse 7 data bits + 1 parity bit into VIS code (LSB first). */
  private parseVISCode(): number {
    let code = 0;
    for (let i = 0; i < 7; i++) {
      code |= (this.visBits[i] << i);
    }
    return code;
  }

  private reset(): void {
    this.state        = 'IDLE';
    this.leaderCount  = 0;
    this.visBits      = [];
    this.frameBuffer  = null;
    this.frameSamplesRequired  = 0;
    this.frameSamplesCollected = 0;
    this.windowFill   = 0;
  }
}
