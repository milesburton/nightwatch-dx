import type { IQWorkerMessage } from '../types.js';
import { initLogShipper } from '../utils/logShipper.js';

initLogShipper('iqWorker');

const SDR_SAMPLE_RATE = 1_200_000;
const SDR_CENTER_HZ = 139_131_000;
const LO_OFFSET_HZ = 125_000_000;

const RF_CENTER_HZ = SDR_CENTER_HZ - LO_OFFSET_HZ;

// ── Waterfall FFT ─────────────────────────────────────────────────────────────

const FFT_SIZE = 4096;
const FFT_AVERAGES = 3;
// Stride: compute a new FFT window every N new IQ samples.
// 4096/4 = 1024 → ~4× overlap
const FFT_STRIDE = FFT_SIZE >> 2;

const hannWindow = new Float32Array(FFT_SIZE);
for (let i = 0; i < FFT_SIZE; i++) {
  hannWindow[i] = 0.5 * (1 - Math.cos((2 * Math.PI * i) / (FFT_SIZE - 1)));
}

const fftAccum = new Float32Array(FFT_SIZE).fill(0);
let fftFrameCount = 0;

// Ring buffer holds FFT_SIZE IQ pairs (2 floats each)
const iqBuf = new Float32Array(FFT_SIZE * 2).fill(0);
let iqBufIdx = 0; // write head (in IQ pairs)
let iqBufFilled = 0; // how many pairs are valid
let strideCount = 0; // samples since last FFT trigger

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
    if (iqBufFilled < FFT_SIZE) continue;

    strideCount++;
    if (strideCount < FFT_STRIDE) continue;
    strideCount = 0;

    const re = new Float32Array(FFT_SIZE);
    const im = new Float32Array(FFT_SIZE);
    for (let k = 0; k < FFT_SIZE; k++) {
      const idx = (iqBufIdx + k) % FFT_SIZE;
      re[k] = iqBuf[idx * 2] * hannWindow[k];
      im[k] = iqBuf[idx * 2 + 1] * hannWindow[k];
    }

    const power = fftPower(re, im);
    for (let k = 0; k < FFT_SIZE; k++) fftAccum[k] += power[k];
    fftFrameCount++;

    if (fftFrameCount >= FFT_AVERAGES) {
      const bins = Array.from(fftAccum, (v) => v / FFT_AVERAGES);
      fftFrameCount = 0;
      fftAccum.fill(0);
      self.postMessage({
        type: 'fft',
        bins,
        centerFreq: RF_CENTER_HZ,
        sampleRate: SDR_SAMPLE_RATE,
      } as IQWorkerMessage);
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
    self.postMessage({
      type: 'status',
      connected: true,
      centerFreq: RF_CENTER_HZ,
      sampleRate: SDR_SAMPLE_RATE,
    } as IQWorkerMessage);
  };

  ws.onclose = () => {
    self.postMessage({
      type: 'status',
      connected: false,
      centerFreq: RF_CENTER_HZ,
      sampleRate: SDR_SAMPLE_RATE,
    } as IQWorkerMessage);
    setTimeout(connect, 3000);
  };

  ws.onerror = () => ws?.close();

  ws.onmessage = (e: MessageEvent<ArrayBuffer>) => {
    if (!(e.data instanceof ArrayBuffer)) return;
    const raw = new Uint8Array(e.data);

    if (raw.length === 12 && raw[0] === 82 && raw[1] === 84 && raw[2] === 76) return;

    processFFT(raw);
  };
}

connect();
