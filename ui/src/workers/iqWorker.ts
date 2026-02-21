import type { IQWorkerMessage } from '../types.js';
import { CWDecoder } from '../utils/CWDecoder.js';
import { SSTVVISDetector } from '../utils/SSTVVISDetector.js';

const SDR_SAMPLE_RATE = 2_400_000;
const SDR_CENTER_HZ = 139_175_000;
const LO_OFFSET_HZ = 125_000_000;
const CW_FREQ_HZ = 14_029_000;
const WPM = 20;

const RF_CENTER_HZ = SDR_CENTER_HZ - LO_OFFSET_HZ;
const AUDIO_SAMPLE_RATE = SDR_SAMPLE_RATE / 100;

const SSTV_OFFSET_HZ = 14_230_000 - RF_CENTER_HZ;

// ── Waterfall FFT ─────────────────────────────────────────────────────────────

const FFT_SIZE = 1024;
const FFT_AVERAGES = 50;

const hannWindow = new Float32Array(FFT_SIZE);
for (let i = 0; i < FFT_SIZE; i++) {
  hannWindow[i] = 0.5 * (1 - Math.cos((2 * Math.PI * i) / (FFT_SIZE - 1)));
}

const fftAccum = new Float32Array(FFT_SIZE).fill(0);
let fftFrameCount = 0;

const iqBuf = new Float32Array(FFT_SIZE * 2).fill(0);
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
      let curRe = 1, curIm = 0;
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
    iqBuf[iqBufIdx * 2]     = (raw[i]     - 127.5) / 127.5;
    iqBuf[iqBufIdx * 2 + 1] = (raw[i + 1] - 127.5) / 127.5;
    iqBufIdx = (iqBufIdx + 1) % FFT_SIZE;
    if (iqBufFilled < FFT_SIZE) iqBufFilled++;
  }

  if (iqBufFilled < FFT_SIZE) return;

  const re = new Float32Array(FFT_SIZE);
  const im = new Float32Array(FFT_SIZE);
  for (let i = 0; i < FFT_SIZE; i++) {
    const idx = (iqBufIdx + i) % FFT_SIZE;
    re[i] = iqBuf[idx * 2]     * hannWindow[i];
    im[i] = iqBuf[idx * 2 + 1] * hannWindow[i];
  }

  const power = fftPower(re, im);
  for (let i = 0; i < FFT_SIZE; i++) fftAccum[i] += power[i];
  fftFrameCount++;

  if (fftFrameCount >= FFT_AVERAGES) {
    const bins = Array.from(fftAccum, (v) => v / FFT_AVERAGES);
    fftFrameCount = 0;
    fftAccum.fill(0);
    self.postMessage({ type: 'fft', bins, centerFreq: RF_CENTER_HZ, sampleRate: SDR_SAMPLE_RATE } as IQWorkerMessage);
  }
}

// ── CW decoder ────────────────────────────────────────────────────────────────

const cwDecoder = new CWDecoder({
  sdrSampleRate: SDR_SAMPLE_RATE,
  sdrCenterHz:   SDR_CENTER_HZ,
  loOffsetHz:    LO_OFFSET_HZ,
  cwFreqHz:      CW_FREQ_HZ,
  wpm:           WPM,
});

// ── SSTV signal chain — allocation-free hot path ──────────────────────────────
//
// KaiserFIR.push() allocates a Complex object on every call (2.4 M/s), which
// causes GC pressure that stalls the entire worker and freezes the waterfall.
// Instead we inline the FIR convolution directly with Float32Arrays so no heap
// objects are allocated in the per-sample loop.

function makeKaiserTaps(cutoffFreq: number, sampleRate: number, duration: number, beta = 8): Float32Array {
  const numTaps = Math.floor(duration * sampleRate) | 1;
  const taps = new Float32Array(numTaps);
  const normalizedCutoff = (2 * cutoffFreq) / sampleRate;
  const center = (numTaps - 1) / 2;

  function besselI0(x: number): number {
    let sum = 1, term = 1;
    for (let k = 1; k < 50; k++) {
      term *= (x / (2 * k)) * (x / (2 * k));
      sum += term;
      if (term < 1e-12 * sum) break;
    }
    return sum;
  }

  const ibeta = besselI0(beta);
  for (let i = 0; i < numTaps; i++) {
    const x = i - center;
    const sinc = x === 0 ? normalizedCutoff : Math.sin(Math.PI * x * normalizedCutoff) / (Math.PI * x);
    const alpha = (numTaps - 1) / 2;
    const kaiserArg = beta * Math.sqrt(1 - ((i - alpha) / alpha) ** 2);
    taps[i] = sinc * (besselI0(kaiserArg) / ibeta);
  }

  let sum = 0;
  for (let i = 0; i < numTaps; i++) sum += taps[i];
  for (let i = 0; i < numTaps; i++) taps[i] /= sum;

  return taps;
}

const SSTV_FIR_DURATION = 0.001;
const SSTV_FIR_CUTOFF1 = SDR_SAMPLE_RATE / 10 / 2;
const SSTV_FIR_CUTOFF2 = SDR_SAMPLE_RATE / 100 / 2;

const sstvTaps1 = makeKaiserTaps(SSTV_FIR_CUTOFF1, SDR_SAMPLE_RATE,       SSTV_FIR_DURATION);
const sstvTaps2 = makeKaiserTaps(SSTV_FIR_CUTOFF2, SDR_SAMPLE_RATE / 10,  SSTV_FIR_DURATION);

const N1 = sstvTaps1.length;
const N2 = sstvTaps2.length;

const sstvBuf1Re = new Float32Array(N1);
const sstvBuf1Im = new Float32Array(N1);
const sstvBuf2Re = new Float32Array(N2);
const sstvBuf2Im = new Float32Array(N2);
let sstvBuf1Idx = 0;
let sstvBuf2Idx = 0;

const sstvLOStep = (2 * Math.PI * SSTV_OFFSET_HZ) / SDR_SAMPLE_RATE;
let sstvLORe = 1;
let sstvLOIm = 0;
const sstvLOStepRe = Math.cos(sstvLOStep);
const sstvLOStepIm = -Math.sin(sstvLOStep);
let sstvLONorm = 0;

let sstvDecCount1 = 0;
let sstvDecCount2 = 0;
let sstvPrevI = 0;
let sstvPrevQ = 0;

const SSTV_AUDIO_CHUNK = Math.round(AUDIO_SAMPLE_RATE * 0.01);
const sstvAudioBuf = new Float32Array(SSTV_AUDIO_CHUNK);
let sstvAudioIdx = 0;

const visDetector = new SSTVVISDetector(AUDIO_SAMPLE_RATE);

function processSSTVSample(rawI: number, rawQ: number): void {
  const loRe = sstvLORe;
  const loIm = sstvLOIm;
  const nextRe = loRe * sstvLOStepRe - loIm * sstvLOStepIm;
  const nextIm = loRe * sstvLOStepIm + loIm * sstvLOStepRe;
  sstvLORe = nextRe;
  sstvLOIm = nextIm;
  if (++sstvLONorm >= 1000) {
    const mag = Math.sqrt(sstvLORe * sstvLORe + sstvLOIm * sstvLOIm);
    sstvLORe /= mag;
    sstvLOIm /= mag;
    sstvLONorm = 0;
  }

  const mixI = rawI * loRe - rawQ * loIm;
  const mixQ = rawI * loIm + rawQ * loRe;

  sstvBuf1Re[sstvBuf1Idx] = mixI;
  sstvBuf1Im[sstvBuf1Idx] = mixQ;
  sstvBuf1Idx = (sstvBuf1Idx + 1) % N1;

  if (++sstvDecCount1 < 10) return;
  sstvDecCount1 = 0;

  let outRe = 0, outIm = 0;
  for (let k = 0; k < N1; k++) {
    const idx = (sstvBuf1Idx + k) % N1;
    outRe += sstvBuf1Re[idx] * sstvTaps1[k];
    outIm += sstvBuf1Im[idx] * sstvTaps1[k];
  }

  sstvBuf2Re[sstvBuf2Idx] = outRe;
  sstvBuf2Im[sstvBuf2Idx] = outIm;
  sstvBuf2Idx = (sstvBuf2Idx + 1) % N2;

  if (++sstvDecCount2 < 10) return;
  sstvDecCount2 = 0;

  let audioRe = 0, audioIm = 0;
  for (let k = 0; k < N2; k++) {
    const idx = (sstvBuf2Idx + k) % N2;
    audioRe += sstvBuf2Re[idx] * sstvTaps2[k];
    audioIm += sstvBuf2Im[idx] * sstvTaps2[k];
  }

  const cross = audioIm * sstvPrevI - audioRe * sstvPrevQ;
  const dot   = audioRe * sstvPrevI + audioIm * sstvPrevQ;
  sstvPrevI = audioRe;
  sstvPrevQ = audioIm;
  const instFreq = (Math.atan2(cross, dot) * AUDIO_SAMPLE_RATE) / (2 * Math.PI);

  sstvAudioBuf[sstvAudioIdx++] = instFreq;
  if (sstvAudioIdx >= SSTV_AUDIO_CHUNK) {
    sstvAudioIdx = 0;
    const frame = visDetector.push(sstvAudioBuf);
    if (frame) {
      self.postMessage(
        { type: 'sstv_audio', samples: frame, sampleRate: AUDIO_SAMPLE_RATE, ts: new Date().toISOString() } as IQWorkerMessage,
        { transfer: [frame.buffer] },
      );
    }
  }
}

// ── WebSocket connection ───────────────────────────────────────────────────────

const WS_PROTO = self.location.protocol === 'https:' ? 'wss:' : 'ws:';
const WS_URL   = `${WS_PROTO}//${self.location.host}/ws/iq`;

let ws: WebSocket | null = null;

function connect(): void {
  ws = new WebSocket(WS_URL);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    self.postMessage({ type: 'status', connected: true,  centerFreq: RF_CENTER_HZ, sampleRate: SDR_SAMPLE_RATE } as IQWorkerMessage);
  };

  ws.onclose = () => {
    self.postMessage({ type: 'status', connected: false, centerFreq: RF_CENTER_HZ, sampleRate: SDR_SAMPLE_RATE } as IQWorkerMessage);
    setTimeout(connect, 3000);
  };

  ws.onerror = () => ws?.close();

  ws.onmessage = (e: MessageEvent<ArrayBuffer>) => {
    if (!(e.data instanceof ArrayBuffer)) return;
    const raw = new Uint8Array(e.data);

    if (raw.length === 12 && raw[0] === 82 && raw[1] === 84 && raw[2] === 76) return;

    cwDecoder.pushBytes(raw, (ev) => {
      if (ev.type === 'char') {
        self.postMessage({ type: 'cw_char', char: ev.char, freq: ev.freq, ts: new Date().toISOString() } as IQWorkerMessage);
      } else {
        self.postMessage({ type: 'cw_word_space' } as IQWorkerMessage);
      }
    });

    processFFT(raw);

    const n = raw.length & ~1;
    for (let i = 0; i < n; i += 2) {
      processSSTVSample((raw[i] - 127.5) / 127.5, (raw[i + 1] - 127.5) / 127.5);
    }
  };
}

connect();
