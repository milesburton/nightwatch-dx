/**
 * CW (Morse code) decoder.
 *
 * Accepts raw uint8 IQ chunks from the RTL-SDR (via rtl-bridge WebSocket),
 * mixes down to the target CW frequency, decimates 100× in two 10× FIR
 * stages, envelope-detects, and runs the Morse state machine.
 *
 * Reuses Phasor (frequency mixing), KaiserFIR (decimation), Complex (arithmetic).
 *
 * Signal chain (matches cw_decoder.py exactly):
 *   uint8 IQ → complex64 → mix by -freqOffset → 10× FIR → 10× FIR → |z| → LPF → threshold → Morse
 *
 * SDR parameters (must match rtl_tcp startup flags in rtl-bridge Dockerfile):
 *   SDR_SAMPLE_RATE = 2 400 000  sps
 *   SDR_CENTER_HZ   = 139 175 000  (14.175 MHz RF after 125 MHz upconverter)
 *   DECIMATE_FACTOR = 100          (→ 24 000 sps audio)
 */

import { Complex } from './Complex.js';
import { KaiserFIR } from './KaiserFIR.js';
import { Phasor } from './Phasor.js';

// ── Morse code table ──────────────────────────────────────────────────────────

export const MORSE_CODE: Record<string, string> = {
  '.-': 'A', '-...': 'B', '-.-.': 'C', '-..': 'D', '.': 'E',
  '..-.': 'F', '--.': 'G', '....': 'H', '..': 'I', '.---': 'J',
  '-.-': 'K', '.-..': 'L', '--': 'M', '-.': 'N', '---': 'O',
  '.--.': 'P', '--.-': 'Q', '.-.': 'R', '...': 'S', '-': 'T',
  '..-': 'U', '...-': 'V', '.--': 'W', '-..-': 'X', '-.--': 'Y',
  '--..': 'Z',
  '-----': '0', '.----': '1', '..---': '2', '...--': '3', '....-': '4',
  '.....': '5', '-....': '6', '--...': '7', '---..': '8', '----.': '9',
  '.-.-.-': '.', '--..--': ',', '..--..': '?', '.----.': "'",
  '-.-.--': '!', '-..-.': '/', '-.--.': '(', '-.--.-': ')',
  '.-...': '&', '---...': ':', '-.-.-.': ';', '-...-': '=',
  '.-.-.': '+', '-....-': '-', '..--.-': '_', '.-..-.': '"',
  '...-..-': '$', '.--.-.': '@', '...---...': 'SOS',
};

// ── SDR / decode parameters ───────────────────────────────────────────────────

export interface CWDecoderConfig {
  /** SDR sample rate from rtl_tcp (default 2 400 000) */
  sdrSampleRate?: number;
  /** SDR centre frequency after upconverter (default 139 175 000 = 14.175 MHz RF) */
  sdrCenterHz?: number;
  /** HF upconverter LO offset (default 125 000 000) */
  loOffsetHz?: number;
  /** Target CW frequency to decode (default 14 029 000 = 14.029 MHz) */
  cwFreqHz?: number;
  /** Expected speed in WPM (default 20) */
  wpm?: number;
}

// Morse timing constants
const DAH_THRESHOLD = 2.5;   // dah ≥ dit × 2.5
const CHAR_GAP_DITS = 3.0;
const WORD_GAP_DITS = 7.0;

// ── MorseDecoder state machine ────────────────────────────────────────────────

class MorseDecoder {
  private symbols: string[] = [];

  pushTone(duration: number, ditSamples: number): void {
    if (duration < ditSamples * 0.4) return;
    this.symbols.push(duration < ditSamples * DAH_THRESHOLD ? '.' : '-');
  }

  pushGap(duration: number, ditSamples: number, onChar: (c: string | null) => void): void {
    const dits = duration / ditSamples;
    if (dits >= WORD_GAP_DITS) {
      this.flush(onChar);
      onChar(null);   // word space sentinel
    } else if (dits >= CHAR_GAP_DITS) {
      this.flush(onChar);
    }
  }

  flush(onChar: (c: string | null) => void): void {
    if (this.symbols.length === 0) return;
    const code = this.symbols.join('');
    onChar(MORSE_CODE[code] ?? `[${code}]`);
    this.symbols = [];
  }
}

// ── EnvelopeDetector (single-pole IIR LPF on magnitude) ─────────────────────

class EnvelopeDetector {
  private readonly alpha: number;   // LPF coefficient
  private lpfState = 0;
  private readonly windowSize: number;
  private readonly window: number[];
  private windowIdx = 0;
  private windowFull = false;

  constructor(audioSampleRate: number, lpfCutoffHz = 200) {
    // Single-pole IIR: y[n] = α·|x[n]| + (1-α)·y[n-1]
    const rc = 1 / (2 * Math.PI * lpfCutoffHz);
    const dt = 1 / audioSampleRate;
    this.alpha = dt / (rc + dt);
    // 2-second rolling window for adaptive threshold
    this.windowSize = audioSampleRate * 2;
    this.window = new Array(this.windowSize).fill(0);
  }

  process(magnitude: number): { value: number; threshold: number } {
    this.lpfState = this.alpha * magnitude + (1 - this.alpha) * this.lpfState;

    // Update rolling window
    this.window[this.windowIdx] = this.lpfState;
    this.windowIdx = (this.windowIdx + 1) % this.windowSize;
    if (this.windowIdx === 0) this.windowFull = true;

    const samples = this.windowFull
      ? this.window
      : this.window.slice(0, this.windowIdx);

    let threshold = 0.05;
    if (samples.length > 200) {
      const sorted = [...samples].sort((a, b) => a - b);
      const p10 = sorted[Math.floor(sorted.length * 0.10)];
      const p95 = sorted[Math.floor(sorted.length * 0.95)];
      threshold = Math.max(p10 + (p95 - p10) * 0.5, 0.01);
    }

    return { value: this.lpfState, threshold };
  }
}

// ── CW Decoder ────────────────────────────────────────────────────────────────

export interface CWChar {
  type: 'char';
  char: string;
  freq: number;
}

export interface CWWordSpace {
  type: 'word_space';
}

export type CWEvent = CWChar | CWWordSpace;

export class CWDecoder {
  private readonly SDR_SAMPLE_RATE: number;
  private readonly CW_FREQ_HZ: number;
  private readonly RF_CENTER_HZ: number;
  private readonly FREQ_OFFSET_HZ: number;
  private readonly AUDIO_SAMPLE_RATE: number;
  private readonly DIT_SAMPLES: number;

  private readonly mixer: Phasor;
  private readonly fir1: KaiserFIR;   // first 10× decimation
  private readonly fir2: KaiserFIR;   // second 10× decimation
  private readonly envelope: EnvelopeDetector;
  private readonly morse: MorseDecoder;

  // Decimation counters
  private fir1Counter = 0;
  private fir2Counter = 0;

  // Morse state
  private toneOn = false;
  private toneStart = 0;
  private gapStart = 0;
  private sampleClock = 0;

  constructor(config: CWDecoderConfig = {}) {
    this.SDR_SAMPLE_RATE = config.sdrSampleRate ?? 2_400_000;
    this.CW_FREQ_HZ      = config.cwFreqHz      ?? 14_029_000;
    const loOffsetHz     = config.loOffsetHz    ?? 125_000_000;
    this.RF_CENTER_HZ    = (config.sdrCenterHz  ?? 139_175_000) - loOffsetHz;
    this.FREQ_OFFSET_HZ  = this.CW_FREQ_HZ - this.RF_CENTER_HZ;
    this.AUDIO_SAMPLE_RATE = this.SDR_SAMPLE_RATE / 100;  // 24 000 Hz
    const wpm            = config.wpm ?? 20;
    this.DIT_SAMPLES     = Math.round((60 / (50 * wpm)) * this.AUDIO_SAMPLE_RATE);

    // Mixer: shift by -FREQ_OFFSET to bring target to DC
    this.mixer = new Phasor(this.FREQ_OFFSET_HZ, this.SDR_SAMPLE_RATE);

    // Two 10× decimation FIR filters (cutoff = SDR_RATE/20 for first, /2 for second)
    const decimRate1 = 10;
    const audio1Rate = this.SDR_SAMPLE_RATE / decimRate1;  // 240 000 Hz
    // Cutoff at half the output bandwidth to avoid aliasing
    this.fir1 = new KaiserFIR(audio1Rate / 2, this.SDR_SAMPLE_RATE, 0.001, 8);
    this.fir2 = new KaiserFIR(this.AUDIO_SAMPLE_RATE / 2, audio1Rate, 0.001, 8);

    this.envelope = new EnvelopeDetector(this.AUDIO_SAMPLE_RATE);
    this.morse    = new MorseDecoder();
  }

  /**
   * Feed raw uint8 IQ bytes from rtl-bridge.
   * Returns any CW events (chars / word spaces) decoded from this chunk.
   *
   * Decimation: push every sample through the FIR (to keep filter state correct),
   * then only process the decimated output every Nth sample.
   */
  pushBytes(raw: Uint8Array, onEvent: (ev: CWEvent) => void): void {
    const n = raw.length & ~1;   // even length (I+Q pairs)

    for (let i = 0; i < n; i += 2) {
      const I = (raw[i]     - 127.5) / 127.5;
      const Q = (raw[i + 1] - 127.5) / 127.5;
      const iq = new Complex(I, Q);

      // Mix down to target frequency (continuous phase via Phasor)
      const mixed = iq.mul(this.mixer.rotate());

      // First 10× FIR decimation — push every sample, keep every 10th output
      const stage1 = this.fir1.push(mixed);
      this.fir1Counter++;
      if (this.fir1Counter < 10) continue;
      this.fir1Counter = 0;

      // Second 10× FIR decimation — push every stage1 output, keep every 10th
      const audio = this.fir2.push(stage1);
      this.fir2Counter++;
      if (this.fir2Counter < 10) continue;
      this.fir2Counter = 0;

      // Envelope detection + adaptive threshold
      const { value, threshold } = this.envelope.process(audio.abs());

      // Morse state machine
      const isTone = value > threshold;
      if (isTone && !this.toneOn) {
        if (this.gapStart > 0) {
          this.morse.pushGap(this.sampleClock - this.gapStart, this.DIT_SAMPLES, (c) => {
            if (c === null) onEvent({ type: 'word_space' });
            else onEvent({ type: 'char', char: c, freq: this.CW_FREQ_HZ });
          });
        }
        this.toneOn    = true;
        this.toneStart = this.sampleClock;
      } else if (!isTone && this.toneOn) {
        this.morse.pushTone(this.sampleClock - this.toneStart, this.DIT_SAMPLES);
        this.toneOn   = false;
        this.gapStart = this.sampleClock;
      }
      this.sampleClock++;
    }
  }

  /**
   * Decode CW from a pre-loaded mono audio Float32Array (e.g. from an MP3 file).
   * The audio is assumed to already be at baseband with a CW tone around toneHz.
   * Returns all decoded events synchronously.
   */
  decodeAudio(samples: Float32Array, sampleRate: number, toneHz: number, wpm: number): CWEvent[] {
    const ditSamples = Math.round((60 / (50 * wpm)) * sampleRate);
    const events: CWEvent[] = [];

    // Build a simple single-pole IIR envelope detector for audio files
    const lpfAlpha = (() => {
      const rc = 1 / (2 * Math.PI * 200);
      const dt = 1 / sampleRate;
      return dt / (rc + dt);
    })();

    // Bandpass around toneHz via mixing to DC + LPF (same principle as IQ decode)
    const mixer = new Phasor(toneHz, sampleRate);
    const lpf   = new KaiserFIR(300, sampleRate, 0.002, 8);

    let lpfState = 0;
    const windowSamples = sampleRate * 2;
    const rollingWindow: number[] = [];

    const morse = new MorseDecoder();
    let toneOn = false;
    let toneStart = 0;
    let gapStart = 0;
    let clock = 0;

    for (let i = 0; i < samples.length; i++) {
      const s = new Complex(samples[i], 0);
      const mixed = s.mul(mixer.rotate());
      const filtered = lpf.push(mixed);
      const mag = filtered.abs();

      lpfState = lpfAlpha * mag + (1 - lpfAlpha) * lpfState;
      rollingWindow.push(lpfState);
      if (rollingWindow.length > windowSamples) rollingWindow.shift();

      let threshold = 0.05;
      if (rollingWindow.length > 200) {
        const sorted = [...rollingWindow].sort((a, b) => a - b);
        threshold = Math.max(
          sorted[Math.floor(sorted.length * 0.10)] +
            (sorted[Math.floor(sorted.length * 0.95)] - sorted[Math.floor(sorted.length * 0.10)]) * 0.5,
          0.01
        );
      }

      const isTone = lpfState > threshold;
      if (isTone && !toneOn) {
        if (gapStart > 0) {
          morse.pushGap(clock - gapStart, ditSamples, (c) => {
            if (c === null) events.push({ type: 'word_space' });
            else events.push({ type: 'char', char: c, freq: Math.round(toneHz) });
          });
        }
        toneOn = true;
        toneStart = clock;
      } else if (!isTone && toneOn) {
        morse.pushTone(clock - toneStart, ditSamples);
        toneOn = false;
        gapStart = clock;
      }
      clock++;
    }

    morse.flush((c) => {
      if (c !== null) events.push({ type: 'char', char: c, freq: Math.round(toneHz) });
    });

    return events;
  }

  get cwFrequencyHz(): number { return this.CW_FREQ_HZ; }
  get rfCenterHz(): number    { return this.RF_CENTER_HZ; }
  get freqOffsetHz(): number  { return this.FREQ_OFFSET_HZ; }
  get ditSamples(): number    { return this.DIT_SAMPLES; }
  get audioSampleRate(): number { return this.AUDIO_SAMPLE_RATE; }
}
