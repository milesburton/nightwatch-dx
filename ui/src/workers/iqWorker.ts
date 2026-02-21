/**
 * IQ Worker — runs in a Web Worker, connects to rtl-bridge WebSocket,
 * and does all signal processing in the background thread:
 *
 *   - CW decode:      CWDecoder.ts (mix → decimate → envelope → Morse)
 *   - Waterfall FFT:  1024-pt Hann-windowed, 50-frame averaged
 *   - SSTV detect:    FM discriminator → SSTVVISDetector → sstv_audio message
 *
 * Posts IQWorkerMessage to the main thread for UI updates.
 *
 * SDR parameters are hard-coded to match the rtl-bridge Dockerfile.
 */

import type { IQWorkerMessage } from '../types.js';
import { Complex } from '../utils/Complex.js';
import { CWDecoder } from '../utils/CWDecoder.js';
import { KaiserFIR } from '../utils/KaiserFIR.js';
import { Phasor } from '../utils/Phasor.js';
import { SSTVVISDetector } from '../utils/SSTVVISDetector.js';

// ── Configuration ─────────────────────────────────────────────────────────────

const SDR_SAMPLE_RATE = 2_400_000;
const SDR_CENTER_HZ = 139_175_000; // 14.175 MHz RF after 125 MHz upconverter
const LO_OFFSET_HZ = 125_000_000;
const CW_FREQ_HZ = 14_029_000;
const WPM = 20;

const RF_CENTER_HZ = SDR_CENTER_HZ - LO_OFFSET_HZ; // 14.175 MHz
const AUDIO_SAMPLE_RATE = SDR_SAMPLE_RATE / 100; // 24 000 Hz

// SSTV is on 14.230 MHz → offset from RF centre 14.175 MHz = +55 kHz
const SSTV_OFFSET_HZ = 14_230_000 - RF_CENTER_HZ; // 55 000

// ── Waterfall FFT state ──────────────────────────────────────────────────────

const FFT_SIZE = 1024;
const FFT_AVERAGES = 50;

const hannWindow = new Float32Array(FFT_SIZE);
for (let i = 0; i < FFT_SIZE; i++) {
  hannWindow[i] = 0.5 * (1 - Math.cos((2 * Math.PI * i) / (FFT_SIZE - 1)));
}

const fftAccum = new Float32Array(FFT_SIZE).fill(0);
let fftFrameCount = 0;

const iqBuf = new Float32Array(FFT_SIZE * 2).fill(0); // [I0, Q0, I1, Q1, …]
let iqBufIdx = 0;
let iqBufFilled = 0;

function fftPower(re: Float32Array, im: Float32Array): Float32Array {
  const n = re.length;
  let j = 0;
  for (let i = 1; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      [re[i], re[j]] = [re[j], re[i]];
      [im[i], im[j]] = [im[j], im[i]];
    }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = (-2 * Math.PI) / len;
    const wRe = Math.cos(ang);
    const wIm = Math.sin(ang);
    for (let i = 0; i < n; i += len) {
      let curRe = 1,
        curIm = 0;
      for (let k = 0; k < len / 2; k++) {
        const uRe = re[i + k];
        const uIm = im[i + k];
        const vRe = re[i + k + len / 2] * curRe - im[i + k + len / 2] * curIm;
        const vIm = re[i + k + len / 2] * curIm + im[i + k + len / 2] * curRe;
        re[i + k] = uRe + vRe;
        im[i + k] = uIm + vIm;
        re[i + k + len / 2] = uRe - vRe;
        im[i + k + len / 2] = uIm - vIm;
        const nextRe = curRe * wRe - curIm * wIm;
        curIm = curRe * wIm + curIm * wRe;
        curRe = nextRe;
      }
    }
  }
  const power = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const mag = Math.sqrt(re[i] * re[i] + im[i] * im[i]) / n;
    power[i] = 20 * Math.log10(Math.max(mag, 1e-10));
  }
  const half = n >> 1;
  const out = new Float32Array(n);
  out.set(power.subarray(half), 0);
  out.set(power.subarray(0, half), half);
  return out;
}

function processFFT(raw: Uint8Array): void {
  const n = raw.length & ~1;
  for (let i = 0; i < n; i += 2) {
    iqBuf[iqBufIdx * 2] = (raw[i] - 127.5) / 127.5;
    iqBuf[iqBufIdx * 2 + 1] = (raw[i + 1] - 127.5) / 127.5;
    iqBufIdx = (iqBufIdx + 1) % FFT_SIZE;
    if (iqBufFilled < FFT_SIZE) iqBufFilled++;
  }

  if (iqBufFilled < FFT_SIZE) return;

  const re = new Float32Array(FFT_SIZE);
  const im = new Float32Array(FFT_SIZE);
  for (let i = 0; i < FFT_SIZE; i++) {
    const idx = (iqBufIdx + i) % FFT_SIZE;
    re[i] = iqBuf[idx * 2] * hannWindow[i];
    im[i] = iqBuf[idx * 2 + 1] * hannWindow[i];
  }

  const power = fftPower(re, im);
  for (let i = 0; i < FFT_SIZE; i++) fftAccum[i] += power[i];
  fftFrameCount++;

  if (fftFrameCount >= FFT_AVERAGES) {
    const bins = Array.from(fftAccum, (v) => v / FFT_AVERAGES);
    fftFrameCount = 0;
    fftAccum.fill(0);
    const msg: IQWorkerMessage = {
      type: 'fft',
      bins,
      centerFreq: RF_CENTER_HZ,
      sampleRate: SDR_SAMPLE_RATE,
    };
    self.postMessage(msg);
  }
}

// ── CW decoder ────────────────────────────────────────────────────────────────

const cwDecoder = new CWDecoder({
  sdrSampleRate: SDR_SAMPLE_RATE,
  sdrCenterHz: SDR_CENTER_HZ,
  loOffsetHz: LO_OFFSET_HZ,
  cwFreqHz: CW_FREQ_HZ,
  wpm: WPM,
});

// ── SSTV signal chain ─────────────────────────────────────────────────────────
// Mix to 14.230 MHz (+55 kHz from RF centre), decimate 100× in two 10× FIR
// stages, then FM-discriminate to recover audio, feed to VIS detector.

// KaiserFIR constructor: (cutoffFreq, sampleRate, duration, beta)
// Cutoff = 0.5 × new Nyquist after decimation = SDR_SAMPLE_RATE/10/2 = 120 000 Hz
const SSTV_FIR_CUTOFF = SDR_SAMPLE_RATE / 10 / 2; // 120 000 Hz
const sstvMixer = new Phasor(SSTV_OFFSET_HZ, SDR_SAMPLE_RATE);
const sstvFir1 = new KaiserFIR(SSTV_FIR_CUTOFF, SDR_SAMPLE_RATE, 0.001);
const sstvFir2 = new KaiserFIR(SSTV_FIR_CUTOFF, SDR_SAMPLE_RATE / 10, 0.001);

const visDetector = new SSTVVISDetector(AUDIO_SAMPLE_RATE);

let sstvPrevI = 0;
let sstvPrevQ = 0;
let sstvDecCount1 = 0;
let sstvDecCount2 = 0;

// Accumulate audio samples in 10 ms chunks before pushing to VIS detector
const SSTV_AUDIO_CHUNK = Math.round(AUDIO_SAMPLE_RATE * 0.01); // 240 samples
const sstvAudioBuf = new Float32Array(SSTV_AUDIO_CHUNK);
let sstvAudioIdx = 0;

function processSSTVSample(rawI: number, rawQ: number): void {
  // 1. Frequency mix to SSTV channel
  const lo = sstvMixer.rotate();
  const mixedI = rawI * lo.real - rawQ * lo.imag;
  const mixedQ = rawI * lo.imag + rawQ * lo.real;

  // 2. First 10× FIR decimation — push every sample, take output every 10
  const fir1Out = sstvFir1.push(new Complex(mixedI, mixedQ));
  sstvDecCount1++;
  if (sstvDecCount1 < 10) return;
  sstvDecCount1 = 0;

  // 3. Second 10× FIR decimation
  const fir2Out = sstvFir2.push(fir1Out);
  sstvDecCount2++;
  if (sstvDecCount2 < 10) return;
  sstvDecCount2 = 0;

  // 4. FM discriminator: instantaneous frequency via cross-product
  const I = fir2Out.real;
  const Q = fir2Out.imag;
  const cross = Q * sstvPrevI - I * sstvPrevQ;
  const dot = I * sstvPrevI + Q * sstvPrevQ;
  const instFreq = (Math.atan2(cross, dot) * AUDIO_SAMPLE_RATE) / (2 * Math.PI);
  sstvPrevI = I;
  sstvPrevQ = Q;

  // 5. Accumulate then push to VIS detector
  sstvAudioBuf[sstvAudioIdx++] = instFreq;
  if (sstvAudioIdx >= SSTV_AUDIO_CHUNK) {
    sstvAudioIdx = 0;
    const frame = visDetector.push(sstvAudioBuf);
    if (frame) {
      const msg: IQWorkerMessage = {
        type: 'sstv_audio',
        samples: frame,
        sampleRate: AUDIO_SAMPLE_RATE,
        ts: new Date().toISOString(),
      };
      self.postMessage(msg, { transfer: [frame.buffer] });
    }
  }
}

// ── WebSocket connection ───────────────────────────────────────────────────────

const WS_PROTO = self.location.protocol === 'https:' ? 'wss:' : 'ws:';
const WS_URL = `${WS_PROTO}//${self.location.host}/ws/iq`;

let ws: WebSocket | null = null;

function connect(): void {
  ws = new WebSocket(WS_URL);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    const msg: IQWorkerMessage = {
      type: 'status',
      connected: true,
      centerFreq: RF_CENTER_HZ,
      sampleRate: SDR_SAMPLE_RATE,
    };
    self.postMessage(msg);
  };

  ws.onclose = () => {
    const msg: IQWorkerMessage = {
      type: 'status',
      connected: false,
      centerFreq: RF_CENTER_HZ,
      sampleRate: SDR_SAMPLE_RATE,
    };
    self.postMessage(msg);
    setTimeout(connect, 3000);
  };

  ws.onerror = () => ws?.close();

  ws.onmessage = (e: MessageEvent<ArrayBuffer>) => {
    if (!(e.data instanceof ArrayBuffer)) return;
    const raw = new Uint8Array(e.data);

    // Skip 12-byte RTL0 magic header
    const data = raw.length === 12 && raw[0] === 82 && raw[1] === 84 && raw[2] === 76 ? null : raw;
    if (!data) return;

    // CW decode
    cwDecoder.pushBytes(data, (ev) => {
      if (ev.type === 'char') {
        const msg: IQWorkerMessage = {
          type: 'cw_char',
          char: ev.char,
          freq: ev.freq,
          ts: new Date().toISOString(),
        };
        self.postMessage(msg);
      } else {
        const msg: IQWorkerMessage = { type: 'cw_word_space' };
        self.postMessage(msg);
      }
    });

    // Waterfall FFT
    processFFT(data);

    // SSTV signal chain (sample-by-sample at SDR rate)
    const n = data.length & ~1;
    for (let i = 0; i < n; i += 2) {
      const rawI = (data[i] - 127.5) / 127.5;
      const rawQ = (data[i + 1] - 127.5) / 127.5;
      processSSTVSample(rawI, rawQ);
    }
  };
}

connect();
