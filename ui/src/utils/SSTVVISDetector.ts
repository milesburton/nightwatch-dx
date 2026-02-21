/**
 * SSTVVISDetector — Goertzel-based VIS code detector for SSTV auto-detection.
 *
 * Detects the standard SSTV VIS preamble in a demodulated audio stream
 * (output of FM discriminator on the IQ worker's SSTV signal chain):
 *
 *   Leader   : 300 ms of 1900 Hz tone  (= 30 × 10 ms windows)
 *   Break    :  10 ms of 1200 Hz       (=  1 × 10 ms window)
 *   Start bit:  30 ms of 1900 Hz       (=  3 × 10 ms windows)
 *   VIS bits : 8 × 30 ms               (=  3 × 10 ms windows each)
 *   Stop bit :  30 ms of 1200 Hz       (=  3 × 10 ms windows)
 *
 * Window size is 10 ms — the GCD of all VIS segment durations — so every
 * segment boundary falls on a clean window edge.
 *
 * Once the stop bit is confirmed the detector switches to BUFFERING mode.
 * The emitted Float32Array contains the FULL transmission from the start of
 * the leader tone, so that SSTVDecoder.detectMode() can find the preamble.
 *
 * Audio sample rate assumed: 24 000 Hz (audio output of the SSTV signal chain).
 */

// Known SSTV modes: visCode → total frame duration in seconds (image only, after VIS)
const VIS_DURATIONS: Record<number, number> = {
  8:  240 * (0.009 + 0.003 + 0.15),    // Robot 36   (0x08) ~38.9 s
  95: 496 * (0.02  + 0.00208 + 0.532), // PD 120     (0x5f) ~274 s
  44: 256 * (0.004862 + 0.000572 + 3 * 0.146 + 2 * 0.000572), // Martin M1  (0x2c) ~116 s
  60: 256 * (0.009 + 0.0015 + 3 * 0.138 + 0.0015),            // Scottie S1 (0x3c) ~108 s
};

// ── Frequency classification ──────────────────────────────────────────────────
//
// The input to this detector is FM-discriminated audio: each sample is already
// an instantaneous frequency value in Hz (output of atan2 discriminator).
// We classify each 10ms window by averaging its samples and finding the closest
// VIS reference frequency.  No Goertzel needed — the discriminator has already
// done the frequency-domain work.

const VIS_FREQS = [1100, 1200, 1300, 1900] as const;
const TOLERANCE = 100; // Hz — accept ±100 Hz from each reference

function dominantTone(samples: Float32Array): number {
  let sum = 0;
  for (const x of samples) sum += x;
  const mean = sum / samples.length;

  let bestFreq = 0;
  let bestDist = Number.POSITIVE_INFINITY;
  for (const f of VIS_FREQS) {
    const dist = Math.abs(mean - f);
    if (dist < bestDist) { bestDist = dist; bestFreq = f; }
  }
  return bestDist <= TOLERANCE ? bestFreq : 0;
}

// ── State machine ─────────────────────────────────────────────────────────────

type State = 'IDLE' | 'LEADER' | 'BREAK' | 'START' | 'VIS_BITS' | 'STOP' | 'BUFFERING';

// Max preamble: 5 s leader + break + start + 8 VIS bits + stop ≈ 5.3 s
// Max frame: PD-120 ≈ 274 s. Total cap: ~280 s.
const MAX_TOTAL_SECONDS = 5.5 + 280;

export class SSTVVISDetector {
  private readonly sampleRate: number;
  private readonly winSize: number;        // 10 ms = 240 samples
  private readonly leaderRequired = 30;   // 30 × 10ms = 300 ms

  private state: State = 'IDLE';
  private leaderCount  = 0;
  private visBits: number[] = [];

  // 3-sub-window counter for 30ms segments (start bit, VIS bits, stop bit)
  private subCount = 0;
  private subTone  = 0;

  // Single large pre-allocated output buffer. We write into it continuously
  // from the start of the leader. On emit we slice out the filled portion.
  private readonly outBuf: Float32Array;
  private outIdx = 0;             // write cursor
  private leaderStartIdx  = 0;   // outIdx when leader first detected
  private preambleEndIdx  = 0;   // outIdx when BUFFERING starts
  private frameSamplesNeeded = 0;

  // 10ms sliding window accumulator
  private readonly winBuf: Float32Array;
  private winFill = 0;

  constructor(sampleRate = 24_000) {
    this.sampleRate = sampleRate;
    this.winSize    = Math.round(0.010 * sampleRate);
    this.winBuf     = new Float32Array(this.winSize);
    this.outBuf     = new Float32Array(Math.ceil(MAX_TOTAL_SECONDS * sampleRate));
  }

  push(samples: Float32Array): Float32Array | null {
    for (let i = 0; i < samples.length; i++) {
      const s = samples[i];

      if (this.state === 'BUFFERING') {
        // Write image data directly after preamble
        if (this.outIdx < this.outBuf.length) {
          this.outBuf[this.outIdx++] = s;
        }
        if (this.outIdx - this.preambleEndIdx >= this.frameSamplesNeeded) {
          const out = this.outBuf.slice(this.leaderStartIdx, this.outIdx);
          this.reset();
          return out;
        }
        continue;
      }

      // Always write into outBuf so we have the full history
      if (this.outIdx < this.outBuf.length) {
        this.outBuf[this.outIdx++] = s;
      }

      // Accumulate 10ms window
      this.winBuf[this.winFill++] = s;
      if (this.winFill < this.winSize) continue;
      this.winFill = 0;

      this.processWindow();
    }
    return null;
  }

  private processWindow(): void {
    const tone = dominantTone(this.winBuf);

    switch (this.state) {
      case 'IDLE':
        if (tone === 1900) {
          // Record where the leader starts in outBuf
          this.leaderStartIdx = this.outIdx - this.winSize;
          this.leaderCount    = 1;
          this.state          = 'LEADER';
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
          this.subCount = 1;
          this.subTone  = 1900;
          this.state    = 'START';
        } else {
          this.reset();
        }
        break;

      case 'START':
        if (tone === this.subTone) {
          this.subCount++;
          if (this.subCount >= 3) {
            this.visBits  = [];
            this.subCount = 0;
            this.subTone  = 0;
            this.state    = 'VIS_BITS';
          }
        } else {
          this.reset();
        }
        break;

      case 'VIS_BITS':
        this.processVISSubWindow(tone);
        break;

      case 'STOP':
        if (tone === 1200) {
          this.subCount++;
          if (this.subCount >= 3) {
            const visCode  = this.parseVISCode();
            const duration = VIS_DURATIONS[visCode];
            if (duration !== undefined) {
              this.preambleEndIdx    = this.outIdx;
              this.frameSamplesNeeded = Math.ceil(duration * this.sampleRate);
              this.state = 'BUFFERING';
            } else {
              this.reset();
            }
          }
        } else {
          this.reset();
        }
        break;

      case 'BUFFERING':
        break;
    }
  }

  private processVISSubWindow(tone: number): void {
    if (this.subCount === 0) {
      if (tone === 1100 || tone === 1300) {
        this.subTone  = tone;
        this.subCount = 1;
      } else if (tone === 1200 && this.visBits.length === 8) {
        this.subTone  = 1200;
        this.subCount = 1;
        this.state    = 'STOP';
      } else {
        this.reset();
      }
    } else {
      if (tone === this.subTone) {
        this.subCount++;
        if (this.subCount >= 3) {
          this.visBits.push(this.subTone === 1100 ? 1 : 0);
          this.subCount = 0;
          this.subTone  = 0;
          if (this.visBits.length === 8) {
            this.state    = 'STOP';
            this.subCount = 0;
          }
        }
      } else {
        this.reset();
      }
    }
  }

  private parseVISCode(): number {
    let code = 0;
    for (let i = 0; i < 7; i++) {
      code |= (this.visBits[i] << i);
    }
    return code;
  }

  private reset(): void {
    this.state              = 'IDLE';
    this.leaderCount        = 0;
    this.visBits            = [];
    this.subCount           = 0;
    this.subTone            = 0;
    this.outIdx             = 0;
    this.leaderStartIdx     = 0;
    this.preambleEndIdx     = 0;
    this.frameSamplesNeeded = 0;
    this.winFill            = 0;
  }
}
