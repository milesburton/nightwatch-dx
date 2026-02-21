/**
 * IQ Worker — runs in a Web Worker, connects to rtl-bridge WebSocket,
 * and does all signal processing in the background thread:
 *
 *   - CW decode: CWDecoder.ts (mix → decimate → envelope → Morse)
 *   - Waterfall FFT: 1024-pt Hann-windowed, 50-frame averaged
 *
 * Posts IQWorkerMessage to the main thread for UI updates.
 *
 * SDR parameters are read from the worker's URL search params so the
 * main thread can configure them without rebuilding the worker.
 */

import { CWDecoder } from '../utils/CWDecoder.js';
import type { IQWorkerMessage } from '../types.js';

// ── Configuration (matches rtl-bridge Dockerfile / docker-compose.yml) ───────

const SDR_SAMPLE_RATE = 2_400_000;
const SDR_CENTER_HZ   = 139_175_000;   // 14.175 MHz RF after 125 MHz upconverter
const LO_OFFSET_HZ    = 125_000_000;
const CW_FREQ_HZ      = 14_029_000;    // 14.029 MHz target CW frequency
const WPM             = 20;

const RF_CENTER_HZ = SDR_CENTER_HZ - LO_OFFSET_HZ;   // 14.175 MHz

// ── Waterfall FFT state ──────────────────────────────────────────────────────

const FFT_SIZE     = 1024;
const FFT_AVERAGES = 50;

// Pre-compute Hann window
const hannWindow = new Float32Array(FFT_SIZE);
for (let i = 0; i < FFT_SIZE; i++) {
  hannWindow[i] = 0.5 * (1 - Math.cos((2 * Math.PI * i) / (FFT_SIZE - 1)));
}

// Accumulator for FFT averaging
const fftAccum = new Float32Array(FFT_SIZE).fill(0);
let fftFrameCount = 0;

// Circular buffer for raw IQ → wideband FFT (keep last FFT_SIZE samples)
const iqBuf = new Float32Array(FFT_SIZE * 2).fill(0);   // [I0, Q0, I1, Q1, …]
let iqBufIdx = 0;
let iqBufFilled = 0;

/**
 * Minimal in-place radix-2 DIT FFT on interleaved [re, im] Float32Array.
 * Length must be a power of two. Returns power spectrum in dBFS.
 */
function fftPower(re: Float32Array, im: Float32Array): Float32Array {
  const n = re.length;
  // Bit-reversal permutation
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
  // FFT butterfly
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
        re[i + k]           = uRe + vRe;
        im[i + k]           = uIm + vIm;
        re[i + k + len / 2] = uRe - vRe;
        im[i + k + len / 2] = uIm - vIm;
        const nextRe = curRe * wRe - curIm * wIm;
        curIm = curRe * wIm + curIm * wRe;
        curRe = nextRe;
      }
    }
  }
  // Power in dBFS, FFT-shifted (DC at centre)
  const power = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const mag = Math.sqrt(re[i] * re[i] + im[i] * im[i]) / n;
    power[i] = 20 * Math.log10(Math.max(mag, 1e-10));
  }
  // FFT shift: swap halves so DC is in the middle
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

  // Extract ordered samples from circular buffer
  const re = new Float32Array(FFT_SIZE);
  const im = new Float32Array(FFT_SIZE);
  for (let i = 0; i < FFT_SIZE; i++) {
    const idx = (iqBufIdx + i) % FFT_SIZE;
    re[i] = iqBuf[idx * 2]     * hannWindow[i];
    im[i] = iqBuf[idx * 2 + 1] * hannWindow[i];
  }

  const power = fftPower(re, im);

  for (let i = 0; i < FFT_SIZE; i++) {
    fftAccum[i] += power[i];
  }
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

// ── CW decoder instance ───────────────────────────────────────────────────────

const cwDecoder = new CWDecoder({
  sdrSampleRate: SDR_SAMPLE_RATE,
  sdrCenterHz:   SDR_CENTER_HZ,
  loOffsetHz:    LO_OFFSET_HZ,
  cwFreqHz:      CW_FREQ_HZ,
  wpm:           WPM,
});

// ── WebSocket connection to rtl-bridge ───────────────────────────────────────

const WS_PROTO = self.location.protocol === 'https:' ? 'wss:' : 'ws:';
const WS_URL   = `${WS_PROTO}//${self.location.host}/ws/iq`;

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

    // Skip the 12-byte RTL0 magic header on first message
    const data = raw.length === 12 && raw[0] === 82 && raw[1] === 84 && raw[2] === 76
      ? null
      : raw;
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
  };
}

connect();
