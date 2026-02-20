import type { SSTVMode } from '../types.js';

export const SSTV_MODES: Record<string, SSTVMode> = {
  ROBOT36: {
    name: 'Robot 36',
    visCode: 0x08,
    scanTime: 0.15,
    lines: 240,
    width: 320,
    colorScan: true,
    syncPulse: 0.009,
    syncPorch: 0.003,
    colorFormat: 'YUV',
  },
  PD120: {
    name: 'PD 120',
    visCode: 0x5f,
    scanTime: 0.532,
    lines: 496,
    width: 640,
    colorScan: true,
    syncPulse: 0.02,
    syncPorch: 0.00208,
    componentTime: 0.1216,
    colorFormat: 'PD',
  },
  MARTIN1: {
    name: 'Martin M1',
    visCode: 0x2c,
    scanTime: 0.146,
    lines: 256,
    width: 320,
    colorScan: false,
    syncPulse: 0.004862,
    syncPorch: 0.000572,
    separatorPulse: 0.000572,
    colorFormat: 'RGB',
  },
  SCOTTIE1: {
    name: 'Scottie S1',
    visCode: 0x3c,
    scanTime: 0.138,
    lines: 256,
    width: 320,
    colorScan: false,
    syncPulse: 0.009,
    syncPorch: 0.0015,
    separatorPulse: 0.0015,
    colorFormat: 'RGB',
  },
};

const FREQ_SYNC = 1200;
const FREQ_BLACK = 1500;
const FREQ_WHITE = 2300;
const FREQ_VIS_BIT1 = 1100;
const FREQ_VIS_BIT0 = 1300;
const FREQ_VIS_START = 1900;
const FREQ_VIS_STOP = 1200;

export class SSTVEncoder {
  readonly mode: SSTVMode;
  readonly sampleRate: number;
  private phase: number = 0;

  constructor(mode: string = 'ROBOT36', sampleRate: number = 48000) {
    const m = SSTV_MODES[mode];
    if (!m) throw new Error(`Unknown SSTV mode: ${mode}`);
    this.mode = m;
    this.sampleRate = sampleRate;
  }

  async encodeImage(imageFile: File): Promise<Blob> {
    const img = await this.loadImage(imageFile);
    const canvas = document.createElement('canvas');
    canvas.width = this.mode.width;
    canvas.height = this.mode.lines;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('Could not get canvas context');
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
    return this.generateAudio(imageData);
  }

  private loadImage(file: File): Promise<HTMLImageElement> {
    return new Promise((resolve, reject) => {
      const img = new Image();
      const objectUrl = URL.createObjectURL(file);
      img.onload = () => {
        URL.revokeObjectURL(objectUrl);
        resolve(img);
      };
      img.onerror = (err) => {
        URL.revokeObjectURL(objectUrl);
        reject(err);
      };
      img.src = objectUrl;
    });
  }

  generateAudio(imageData: ImageData): Blob {
    const samples: number[] = [];
    this.addVISCode(samples);
    this.addImageData(samples, imageData);
    return this.createWAV(samples);
  }

  addVISCode(samples: number[]): void {
    this.addTone(samples, 1900, 0.3);
    this.addTone(samples, 1200, 0.01);
    this.addTone(samples, FREQ_VIS_START, 0.03);
    const visCode = this.mode.visCode;
    let parity = 0;
    for (let i = 0; i < 7; i++) {
      const bit = (visCode >> i) & 1;
      parity ^= bit;
      this.addTone(samples, bit ? FREQ_VIS_BIT1 : FREQ_VIS_BIT0, 0.03);
    }
    this.addTone(samples, parity ? FREQ_VIS_BIT1 : FREQ_VIS_BIT0, 0.03);
    this.addTone(samples, FREQ_VIS_STOP, 0.03);
  }

  private addImageData(samples: number[], imageData: ImageData): void {
    const { data, width, height } = imageData;
    if (this.mode.colorFormat === 'PD') {
      for (let y = 0; y < height; y += 2) {
        this.addScanLinePD(samples, data, width, y);
      }
    } else {
      for (let y = 0; y < height; y++) {
        this.addTone(samples, FREQ_SYNC, this.mode.syncPulse);
        this.addTone(samples, FREQ_BLACK, this.mode.syncPorch);
        if (this.mode.colorFormat === 'RGB') {
          this.addScanLine(samples, data, width, y, 1);
          if (this.mode.separatorPulse) this.addTone(samples, FREQ_SYNC, this.mode.separatorPulse);
          this.addScanLine(samples, data, width, y, 2);
          if (this.mode.separatorPulse) this.addTone(samples, FREQ_SYNC, this.mode.separatorPulse);
          this.addScanLine(samples, data, width, y, 0);
        } else {
          this.addScanLineYUV(samples, data, width, y);
        }
      }
    }
  }

  private addScanLine(
    samples: number[],
    data: Uint8ClampedArray,
    width: number,
    y: number,
    channel: number
  ): void {
    const timePerPixel = this.mode.scanTime / width;
    for (let x = 0; x < width; x++) {
      const value = data[(y * width + x) * 4 + channel];
      const freq = FREQ_BLACK + (value / 255) * (FREQ_WHITE - FREQ_BLACK);
      this.addTone(samples, freq, timePerPixel);
    }
  }

  addScanLineYUV(samples: number[], data: Uint8ClampedArray, width: number, y: number): void {
    const Y_SCAN_TIME = 0.088;
    const SEPARATOR_TIME = 0.0045;
    const PORCH_TIME = 0.0015;
    const CHROMA_SCAN_TIME = 0.044;
    const yScanSamples = Math.floor(Y_SCAN_TIME * this.sampleRate);

    for (let x = 0; x < width; x++) {
      const idx = (y * width + x) * 4;
      const Y = 0.299 * data[idx] + 0.587 * data[idx + 1] + 0.114 * data[idx + 2];
      const freq =
        FREQ_BLACK + (Math.max(0, Math.min(255, Math.round(Y))) / 255) * (FREQ_WHITE - FREQ_BLACK);
      const start = Math.floor((x / width) * yScanSamples);
      const end = Math.floor(((x + 1) / width) * yScanSamples);
      this.addTone(samples, freq, (end - start) / this.sampleRate);
    }

    this.addTone(samples, y % 2 === 0 ? FREQ_BLACK : FREQ_WHITE, SEPARATOR_TIME);
    this.addTone(samples, FREQ_BLACK, PORCH_TIME);

    const isVLine = y % 2 === 0;
    const chromaScanSamples = Math.floor(CHROMA_SCAN_TIME * this.sampleRate);
    const halfWidth = width / 2;

    for (let x = 0; x < width; x += 2) {
      const chromaIndex = x / 2;
      const idx1 = (y * width + x) * 4;
      const idx2 = (y * width + Math.min(x + 1, width - 1)) * 4;
      const r = (data[idx1] + data[idx2]) / 2;
      const g = (data[idx1 + 1] + data[idx2 + 1]) / 2;
      const b = (data[idx1 + 2] + data[idx2 + 2]) / 2;
      const U = 128 + (-0.14713 * r - 0.28886 * g + 0.436 * b);
      const V = 128 + (0.615 * r - 0.51499 * g - 0.10001 * b);
      const chromaValue = Math.max(0, Math.min(255, Math.round(isVLine ? V : U)));
      const freq = FREQ_BLACK + (chromaValue / 255) * (FREQ_WHITE - FREQ_BLACK);
      const start = Math.floor((chromaIndex / halfWidth) * chromaScanSamples);
      const end = Math.floor(((chromaIndex + 1) / halfWidth) * chromaScanSamples);
      this.addTone(samples, freq, (end - start) / this.sampleRate);
    }
  }

  private addScanLinePD(
    samples: number[],
    data: Uint8ClampedArray,
    width: number,
    y: number
  ): void {
    const COMPONENT_TIME = this.mode.componentTime ?? 0;
    const componentSamples = Math.floor(COMPONENT_TIME * this.sampleRate);
    this.addTone(samples, FREQ_SYNC, this.mode.syncPulse);
    this.addTone(samples, FREQ_BLACK, this.mode.syncPorch);
    const encodeComponent = (getValue: (x: number) => number): void => {
      for (let x = 0; x < width; x++) {
        const value = Math.max(0, Math.min(255, Math.round(getValue(x))));
        const freq = FREQ_BLACK + (value / 255) * (FREQ_WHITE - FREQ_BLACK);
        const start = Math.floor((x / width) * componentSamples);
        const end = Math.floor(((x + 1) / width) * componentSamples);
        this.addTone(samples, freq, (end - start) / this.sampleRate);
      }
    };
    const y1 = Math.min(y + 1, this.mode.lines - 1);
    encodeComponent((x) => {
      const idx = (y * width + x) * 4;
      return 0.299 * data[idx] + 0.587 * data[idx + 1] + 0.114 * data[idx + 2];
    });
    encodeComponent((x) => {
      const i0 = (y * width + x) * 4;
      const i1 = (y1 * width + x) * 4;
      const r = (data[i0] + data[i1]) / 2;
      const g = (data[i0 + 1] + data[i1 + 1]) / 2;
      const b = (data[i0 + 2] + data[i1 + 2]) / 2;
      const Y = 0.299 * r + 0.587 * g + 0.114 * b;
      return 128 + 0.701 * (r - Y);
    });
    encodeComponent((x) => {
      const i0 = (y * width + x) * 4;
      const i1 = (y1 * width + x) * 4;
      const r = (data[i0] + data[i1]) / 2;
      const g = (data[i0 + 1] + data[i1 + 1]) / 2;
      const b = (data[i0 + 2] + data[i1 + 2]) / 2;
      const Y = 0.299 * r + 0.587 * g + 0.114 * b;
      return 128 + 0.886 * (b - Y);
    });
    encodeComponent((x) => {
      const idx = (y1 * width + x) * 4;
      return 0.299 * data[idx] + 0.587 * data[idx + 1] + 0.114 * data[idx + 2];
    });
  }

  addTone(samples: number[], frequency: number, duration: number): void {
    const numSamples = Math.floor(duration * this.sampleRate);
    const phaseIncrement = (2 * Math.PI * frequency) / this.sampleRate;
    for (let i = 0; i < numSamples; i++) {
      samples.push(Math.sin(this.phase));
      this.phase += phaseIncrement;
    }
    this.phase %= 2 * Math.PI;
  }

  createWAV(samples: number[]): Blob {
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    this.writeString(view, 0, 'RIFF');
    view.setUint32(4, 36 + samples.length * 2, true);
    this.writeString(view, 8, 'WAVE');
    this.writeString(view, 12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, this.sampleRate, true);
    view.setUint32(28, this.sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    this.writeString(view, 36, 'data');
    view.setUint32(40, samples.length * 2, true);
    let offset = 44;
    for (let i = 0; i < samples.length; i++) {
      view.setInt16(offset, Math.max(-1, Math.min(1, samples[i])) * 0x7fff, true);
      offset += 2;
    }
    return new Blob([buffer], { type: 'audio/wav' });
  }

  private writeString(view: DataView, offset: number, string: string): void {
    for (let i = 0; i < string.length; i++) {
      view.setUint8(offset + i, string.charCodeAt(i));
    }
  }
}
