import type {
  DecodeDiagnostics,
  DecodeImageResult,
  DecodeResult,
  ImageQuality,
  SSTVMode,
} from '../types.js';
import { SSTV_MODES } from './SSTVEncoder.js';

const FREQ_SYNC = 1200;
const FREQ_BLACK = 1500;
const FREQ_WHITE = 2300;

export interface SSTVDecoderOptions {
  freqOffset?: number;
  autoCalibrate?: boolean;
}

function isWAV(buffer: ArrayBuffer): boolean {
  if (buffer.byteLength < 12) return false;
  const view = new DataView(buffer);
  return view.getUint32(0, false) === 0x52494646 && view.getUint32(8, false) === 0x57415645;
}

function parseWAV(buffer: ArrayBuffer): { samples: Float32Array; sampleRate: number } {
  const view = new DataView(buffer);
  // Verify RIFF/WAVE header
  if (view.getUint32(0, false) !== 0x52494646 || view.getUint32(8, false) !== 0x57415645) {
    throw new Error('Not a valid WAV file');
  }
  const sampleRate = view.getUint32(24, true);
  const bitsPerSample = view.getUint16(34, true);
  const channels = view.getUint16(22, true);
  // Find the data chunk
  let offset = 12;
  while (offset + 8 <= buffer.byteLength) {
    const chunkId = view.getUint32(offset, false);
    const chunkSize = view.getUint32(offset + 4, true);
    if (chunkId === 0x64617461) {
      // 'data'
      offset += 8;
      const numSamples = Math.floor(chunkSize / (bitsPerSample / 8) / channels);
      const samples = new Float32Array(numSamples);
      for (let i = 0; i < numSamples; i++) {
        const byteOffset = offset + i * channels * (bitsPerSample / 8);
        if (bitsPerSample === 16) {
          samples[i] = view.getInt16(byteOffset, true) / 32768;
        } else if (bitsPerSample === 8) {
          samples[i] = (view.getUint8(byteOffset) - 128) / 128;
        } else {
          samples[i] = view.getFloat32(byteOffset, true);
        }
      }
      return { samples, sampleRate };
    }
    offset += 8 + chunkSize;
  }
  throw new Error('No data chunk found in WAV file');
}

export class SSTVDecoder {
  sampleRate: number;
  private mode: SSTVMode | null;
  freqOffset: number;
  autoCalibrate: boolean;
  private visEndPos: number | null = null;
  private lastVisCode: number | null = null;
  private lastVisParityOk: boolean = false;
  private visFreqShift: number = 0;
  private currentChromaType: 'U' | 'V' = 'V';

  constructor(sampleRate: number = 48000, options: SSTVDecoderOptions = {}) {
    this.sampleRate = sampleRate;
    this.mode = null;
    this.freqOffset = options.freqOffset ?? 0;
    this.autoCalibrate = options.autoCalibrate !== false;
  }

  private get currentMode(): SSTVMode {
    if (this.mode === null) throw new Error('SSTV mode not detected');
    return this.mode;
  }

  async decodeAudio(audioFile: { arrayBuffer(): Promise<ArrayBuffer> }): Promise<DecodeResult> {
    const arrayBuffer = await audioFile.arrayBuffer();
    const result = await this.decodeAudioBuffer(arrayBuffer);

    const canvas = document.createElement('canvas');
    canvas.width = result.width;
    canvas.height = result.height;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('Could not get canvas context');
    const imageData = ctx.createImageData(result.width, result.height);
    imageData.data.set(result.pixels);
    ctx.putImageData(imageData, 0, 0);

    return { imageUrl: canvas.toDataURL('image/png'), diagnostics: result.diagnostics };
  }

  async decodeAudioBuffer(arrayBuffer: ArrayBuffer): Promise<DecodeImageResult> {
    const wav = isWAV(arrayBuffer);
    if (typeof AudioContext !== 'undefined' || typeof OfflineAudioContext !== 'undefined') {
      const Ctor = typeof AudioContext !== 'undefined' ? AudioContext : OfflineAudioContext;
      try {
        const ctx =
          Ctor === OfflineAudioContext
            ? new OfflineAudioContext(1, 1, 48000)
            : new (Ctor as typeof AudioContext)({ sampleRate: 48000 });
        const audioBuffer = await ctx.decodeAudioData(arrayBuffer);
        const samples = audioBuffer.getChannelData(0);
        this.sampleRate = audioBuffer.sampleRate;
        return this.decodeSamples(samples);
      } catch (err) {
        if (!wav) throw err;
      }
    }

    if (!wav) {
      throw new Error(
        'Web Audio API is unavailable in this environment and the file is not a WAV. ' +
          'Please use a WAV file.'
      );
    }
    const { samples, sampleRate } = parseWAV(arrayBuffer);
    this.sampleRate = sampleRate;
    return this.decodeSamples(samples);
  }

  decodeSamples(samples: Float32Array): DecodeImageResult {
    this.mode = this.detectMode(samples);

    if (!this.mode) {
      throw new Error('Could not detect SSTV mode. Try manually selecting a mode.');
    }

    return this.decodeImage(samples, {
      sampleRate: this.sampleRate,
      fileDuration: samples.length / this.sampleRate,
    });
  }

  detectMode(samples: Float32Array): SSTVMode {
    const step = Math.floor(this.sampleRate * 0.0005);
    const searchLimit = Math.min(samples.length, this.sampleRate * 60);

    for (let i = 0; i < searchLimit - 1000; i += step) {
      const freq = this.detectFrequency(samples, i, 0.01);

      if (Math.abs(freq - 1200) < 150) {
        const breakFreq = freq;
        const checkStep = Math.floor(0.005 * this.sampleRate);

        if (i < Math.floor(0.25 * this.sampleRate)) continue;
        {
          const expectedLeader = 1900 + (breakFreq - 1200);
          const f1 = this.detectFrequency(
            samples,
            Math.max(0, i - Math.floor(0.2 * this.sampleRate)),
            0.02
          );
          const f2 = this.detectFrequency(
            samples,
            Math.max(0, i - Math.floor(0.1 * this.sampleRate)),
            0.02
          );
          if (Math.abs(f1 - expectedLeader) > 200 || Math.abs(f2 - expectedLeader) > 200) continue;
        }

        let breakStart = i;
        const maxBreakSamples = Math.floor(0.3 * this.sampleRate);
        while (breakStart > 0 && i - breakStart < maxBreakSamples) {
          const prevPos = Math.max(0, breakStart - checkStep);
          if (Math.abs(this.detectFrequency(samples, prevPos, 0.005) - breakFreq) > 80) break;
          breakStart = prevPos;
        }

        let breakEnd = breakStart;
        while (
          breakEnd < samples.length - checkStep &&
          breakEnd - breakStart < Math.floor(0.3 * this.sampleRate)
        ) {
          const checkFreq = this.detectFrequency(samples, breakEnd, 0.005);
          if (Math.abs(checkFreq - breakFreq) > 80) break;
          breakEnd += checkStep;
        }
        const breakDuration = (breakEnd - breakStart) / this.sampleRate;
        if (breakDuration < 0.005) continue;

        const freqShift = breakFreq - 1200;
        let dataBitPos = breakEnd;
        const afterBreakFreq = this.detectFrequency(samples, dataBitPos, 0.03);
        if (Math.abs(afterBreakFreq - (1900 + freqShift)) < 150) {
          dataBitPos += Math.floor(0.03 * this.sampleRate);
        }

        const firstBitFreq = this.detectFrequency(samples, dataBitPos, 0.03);
        if (Math.abs(firstBitFreq - (1900 + freqShift)) < 150) continue;
        if (firstBitFreq < 1000 + freqShift || firstBitFreq > 1500 + freqShift) continue;

        const visCode = this.decodeVIS(samples, dataBitPos, freqShift);
        if (!this.lastVisParityOk) continue;
        this.lastVisCode = visCode;
        this.visFreqShift = freqShift;

        let visEndPos = dataBitPos + 9 * Math.floor(0.03 * this.sampleRate);

        {
          const porchFreq = FREQ_BLACK + freqShift;
          const searchStep = Math.floor(0.002 * this.sampleRate);
          const searchWindow = Math.floor(0.06 * this.sampleRate);
          let porchFound = -1;
          for (
            let p = Math.max(0, visEndPos - searchWindow);
            p < Math.min(samples.length, visEndPos + searchWindow);
            p += searchStep
          ) {
            const f = this.detectFrequency(samples, p, 0.003);
            if (Math.abs(f - porchFreq) < 100) {
              porchFound = p;
              break;
            }
          }
          if (porchFound !== -1) {
            const syncSamples = Math.floor(0.009 * this.sampleRate);
            visEndPos = Math.max(visEndPos - searchWindow, porchFound - syncSamples);
          }
        }

        for (const [_key, mode] of Object.entries(SSTV_MODES)) {
          if (mode.visCode === visCode) {
            this.visEndPos = visEndPos;
            return mode;
          }
        }
      }
    }

    const timingMode = this.detectModeByTiming(samples);
    if (timingMode) return timingMode;

    this.visFreqShift = 0;
    return SSTV_MODES.ROBOT36 as SSTVMode;
  }

  private detectModeByTiming(samples: Float32Array): SSTVMode | null {
    const scanLimit = Math.min(samples.length, this.sampleRate * 60);
    const chunkSamples = Math.floor(0.05 * this.sampleRate);
    const minLeaderChunks = 4;

    let leaderEnd = -1;
    let runCount = 0;
    for (let i = 0; i < scanLimit; i += chunkSamples) {
      const f = this.detectFrequency(samples, i, 0.05);
      if (Math.abs(f - 1900) < 150) {
        runCount++;
        if (runCount >= minLeaderChunks) leaderEnd = i + chunkSamples;
      } else {
        if (runCount >= minLeaderChunks && leaderEnd > 0) break;
        runCount = 0;
      }
    }
    if (leaderEnd === -1) return null;

    const visSkip = Math.floor(0.5 * this.sampleRate);
    const step = Math.floor(this.sampleRate * 0.002);
    const searchStart = leaderEnd + visSkip;
    const maxSearch = Math.min(samples.length, searchStart + Math.floor(3 * this.sampleRate));

    const syncs: number[] = [];
    let lastSync = -1;
    for (let i = searchStart; i < maxSearch; i += step) {
      const f = this.detectFrequency(samples, i, 0.005);
      if (Math.abs(f - 1200) < 100) {
        if (lastSync === -1 || i - lastSync > Math.floor(0.05 * this.sampleRate)) {
          syncs.push(i);
          lastSync = i;
          if (syncs.length >= 3) break;
        }
      }
    }
    if (syncs.length < 2) return null;

    const lastSyncPos = syncs[syncs.length - 1] ?? 0;
    const firstSyncPos = syncs[0] ?? 0;
    const period = (lastSyncPos - firstSyncPos) / ((syncs.length - 1) * this.sampleRate);

    for (const [, mode] of Object.entries(SSTV_MODES)) {
      const expected =
        mode.colorFormat === 'PD'
          ? (mode.componentTime ?? 0) * 4 + mode.syncPulse + mode.syncPorch
          : mode.scanTime;
      if (Math.abs(period - expected) / expected < 0.1) {
        this.visEndPos = syncs[0] ?? 0;
        this.visFreqShift = 0;
        return mode;
      }
    }
    return null;
  }

  decodeVIS(samples: Float32Array, startIdx: number, freqShift: number = 0): number {
    let idx = startIdx;
    let visCode = 0;
    let ones = 0;

    for (let bit = 0; bit < 7; bit++) {
      const freq = this.detectFrequency(samples, idx, 0.03);
      if (freq < 1200 + freqShift) {
        visCode |= 1 << bit;
        ones++;
      }
      idx += Math.floor(0.03 * this.sampleRate);
    }

    const parityFreq = this.detectFrequency(samples, idx, 0.03);
    const parityBit = parityFreq < 1200 + freqShift ? 1 : 0;
    this.lastVisParityOk = ones % 2 === parityBit;

    return visCode;
  }

  decodeImage(
    samples: Float32Array,
    audioMeta: { sampleRate?: number; fileDuration?: number } = {}
  ): DecodeImageResult {
    const decodeStart = Date.now();
    if (!this.mode) throw new Error('No SSTV mode set');
    const mode = this.mode;

    const imageData = {
      data: new Uint8ClampedArray(mode.width * mode.lines * 4),
      width: mode.width,
      height: mode.lines,
    } as ImageData;

    for (let i = 0; i < imageData.data.length; i += 4) {
      imageData.data[i] = 0;
      imageData.data[i + 1] = 0;
      imageData.data[i + 2] = 0;
      imageData.data[i + 3] = 255;
    }

    const needsChroma = mode.colorFormat === 'YUV' || mode.colorFormat === 'PD';
    const chromaU: number[] = needsChroma
      ? new Array<number>(mode.width * mode.lines).fill(128)
      : [];
    const chromaV: number[] = needsChroma
      ? new Array<number>(mode.width * mode.lines).fill(128)
      : [];

    const visEnd = this.visEndPos ?? Math.floor(0.61 * this.sampleRate);
    const lineSamplesInit = Math.floor((mode.scanTime || 0.15) * this.sampleRate);
    let position = this.findSyncPulse(samples, visEnd, visEnd + lineSamplesInit);

    if (position === -1) {
      position = this.findSyncPulse(samples, visEnd, visEnd + lineSamplesInit * 3);
    }

    if (position === -1) {
      position = this.findSyncPulse(samples, 0);
    }

    if (position === -1) {
      throw new Error('Could not find sync pulse. Make sure this is a valid SSTV transmission.');
    }

    if (this.autoCalibrate) {
      this.freqOffset = this.visFreqShift || 0;
      const refined = this.estimateFreqOffset(samples, position);
      if (refined !== 0) this.freqOffset = refined;
    }

    if (mode.colorFormat === 'PD') {
      for (let y = 0; y < mode.lines; y += 2) {
        position += Math.floor((mode.syncPulse + mode.syncPorch) * this.sampleRate);
        position = this.decodeScanLinePD(samples, position, imageData, chromaU, chromaV, y);

        if (this.autoCalibrate && y + 2 < mode.lines) {
          const lineSamples = Math.floor((mode.componentTime ?? 0) * 4 * this.sampleRate);
          const tolerance = Math.floor(lineSamples * 0.1);
          const nextSync = this.findSyncPulse(samples, position - tolerance, position + tolerance);
          if (nextSync !== -1) position = nextSync;
        }
      }
    } else {
      for (let y = 0; y < mode.lines && position < samples.length; y++) {
        position += Math.floor((mode.syncPulse + mode.syncPorch) * this.sampleRate);

        if (mode.colorFormat === 'RGB') {
          position = this.decodeScanLine(samples, position, imageData, y, 1);
          if (mode.separatorPulse) position += Math.floor(mode.separatorPulse * this.sampleRate);
          position = this.decodeScanLine(samples, position, imageData, y, 2);
          if (mode.separatorPulse) position += Math.floor(mode.separatorPulse * this.sampleRate);
          position = this.decodeScanLine(samples, position, imageData, y, 0);
        } else {
          position = this.decodeScanLineYUV(samples, position, imageData, y);

          if (position < samples.length) {
            const isEvenLine = y % 2 === 0;
            this.currentChromaType = isEvenLine ? 'V' : 'U';
            position += Math.floor(0.0045 * this.sampleRate);
            position += Math.floor(0.0015 * this.sampleRate);

            if (position < samples.length) {
              position = this.decodeScanLineChroma(
                samples,
                position,
                chromaU,
                chromaV,
                y,
                this.currentChromaType
              );
            }
          }
        }

        if (this.autoCalibrate) {
          const lineSamples = Math.floor(mode.scanTime * this.sampleRate);
          const tolerance = Math.floor(lineSamples * 0.1);
          const nextSync = this.findSyncPulse(samples, position - tolerance, position + tolerance);
          if (nextSync !== -1) {
            position = nextSync;
          }
        }
      }
    }

    if (mode.colorFormat === 'YUV') {
      this.convertYUVtoRGB(imageData, chromaU, chromaV);
    } else if (mode.colorFormat === 'PD') {
      this.convertPDtoRGB(imageData, chromaU, chromaV);
    }

    const quality = this.analyzeImageQuality(imageData.data, mode.width, mode.lines);

    const diagnostics: DecodeDiagnostics = {
      mode: mode.name,
      visCode: this.lastVisCode,
      sampleRate: audioMeta.sampleRate ?? this.sampleRate,
      fileDuration: audioMeta.fileDuration ? `${audioMeta.fileDuration.toFixed(2)}s` : null,
      freqOffset: this.freqOffset,
      autoCalibrate: this.autoCalibrate,
      visEndPos: this.visEndPos,
      decodeTimeMs: Date.now() - decodeStart,
      quality,
    };

    return { pixels: imageData.data, width: mode.width, height: mode.lines, diagnostics };
  }

  analyzeImageQuality(data: Uint8ClampedArray, width: number, height: number): ImageQuality {
    let rSum = 0;
    let gSum = 0;
    let bSum = 0;
    const pixels = width * height;

    for (let i = 0; i < data.length; i += 4) {
      rSum += data[i] ?? 0;
      gSum += data[i + 1] ?? 0;
      bSum += data[i + 2] ?? 0;
    }

    const rAvg = rSum / pixels;
    const gAvg = gSum / pixels;
    const bAvg = bSum / pixels;
    const brightness = (rAvg + gAvg + bAvg) / 3;
    const greenDominance = gAvg - (rAvg + bAvg) / 2;
    const colorImbalance = Math.max(rAvg, gAvg, bAvg) - Math.min(rAvg, gAvg, bAvg);

    let verdict: 'good' | 'warn' | 'bad' = 'good';
    const warnings: string[] = [];

    if (brightness < 10) {
      verdict = 'bad';
      warnings.push('Image is almost entirely black — sync or timing issue');
    } else if (greenDominance > 40) {
      verdict = 'bad';
      warnings.push(
        `Heavy green tint (G dominates by ${greenDominance.toFixed(0)}) — chroma decode error`
      );
    } else if (colorImbalance > 80 && brightness < 40) {
      verdict = 'warn';
      warnings.push('Unusual color balance — possible frequency offset');
    } else if (colorImbalance > 120) {
      verdict = 'warn';
      warnings.push('High color imbalance — possible chroma misalignment');
    }

    return {
      rAvg: Math.round(rAvg),
      gAvg: Math.round(gAvg),
      bAvg: Math.round(bAvg),
      brightness: Math.round(brightness),
      verdict,
      warnings,
    };
  }

  decodeScanLine(
    samples: Float32Array,
    startPos: number,
    imageData: ImageData,
    y: number,
    channel: number
  ): number {
    const mode = this.currentMode;
    const totalSamples = Math.floor(mode.scanTime * this.sampleRate);

    for (let x = 0; x < mode.width; x++) {
      const startSample = Math.floor((x / mode.width) * totalSamples);
      const endSample = Math.floor(((x + 1) / mode.width) * totalSamples);
      const pos = startPos + startSample;
      const duration = (endSample - startSample) / this.sampleRate;

      if (pos >= samples.length) break;

      const freq = this.detectFrequencyRange(samples, pos, duration);
      let value = ((freq - FREQ_BLACK) / (FREQ_WHITE - FREQ_BLACK)) * 255;
      value = Math.max(0, Math.min(255, Math.round(value)));

      const idx = (y * mode.width + x) * 4;
      imageData.data[idx + channel] = value;
      imageData.data[idx + 3] = 255;
    }

    return startPos + totalSamples;
  }

  detectFrequencyRange(samples: Float32Array, startIdx: number, duration: number): number {
    const numSamples = Math.floor(duration * this.sampleRate);
    const endIdx = Math.min(startIdx + numSamples, samples.length);

    if (endIdx - startIdx < 10) return FREQ_BLACK;

    let maxMag = 0;
    let detectedFreq = FREQ_BLACK;

    for (let freq = FREQ_SYNC - 100; freq <= FREQ_WHITE + 200; freq += 25) {
      const magnitude = this.goertzel(samples, startIdx, endIdx, freq);
      if (magnitude > maxMag) {
        maxMag = magnitude;
        detectedFreq = freq;
      }
    }

    const fineStart = Math.max(FREQ_SYNC - 100, detectedFreq - 30);
    const fineEnd = Math.min(FREQ_WHITE + 200, detectedFreq + 30);

    for (let freq = fineStart; freq <= fineEnd; freq += 1) {
      const magnitude = this.goertzel(samples, startIdx, endIdx, freq);
      if (magnitude > maxMag) {
        maxMag = magnitude;
        detectedFreq = freq;
      }
    }

    return detectedFreq;
  }

  decodeScanLineYUV(
    samples: Float32Array,
    startPos: number,
    imageData: ImageData,
    y: number
  ): number {
    const mode = this.currentMode;
    const Y_SCAN_TIME = 0.088;
    const totalSamples = Math.floor(Y_SCAN_TIME * this.sampleRate);
    const minWindowSamples = Math.max(96, Math.floor(totalSamples / mode.width) * 4);

    for (let x = 0; x < mode.width; x++) {
      const startSample = Math.floor((x / mode.width) * totalSamples);
      const endSample = Math.floor(((x + 1) / mode.width) * totalSamples);
      const pos = startPos + startSample;
      const windowEnd = Math.min(
        startPos + totalSamples,
        pos + Math.max(endSample - startSample, minWindowSamples)
      );
      const duration = (windowEnd - pos) / this.sampleRate;

      if (pos >= samples.length) break;

      const freq = this.detectFrequencyRange(samples, pos, duration);
      const freqBlack = FREQ_BLACK + this.freqOffset;
      const freqWhite = FREQ_WHITE + this.freqOffset;
      const value = Math.max(
        0,
        Math.min(255, Math.round(((freq - freqBlack) / (freqWhite - freqBlack)) * 255))
      );

      const pixelIdx = (y * mode.width + x) * 4;
      imageData.data[pixelIdx] = value;
      imageData.data[pixelIdx + 1] = value;
      imageData.data[pixelIdx + 2] = value;
      imageData.data[pixelIdx + 3] = 255;
    }

    return startPos + totalSamples;
  }

  decodeScanLineChroma(
    samples: Float32Array,
    startPos: number,
    chromaU: number[],
    chromaV: number[],
    y: number,
    componentType: 'U' | 'V'
  ): number {
    const mode = this.currentMode;
    const CHROMA_SCAN_TIME = 0.044;
    const halfWidth = Math.floor(mode.width / 2);
    const totalSamples = Math.floor(CHROMA_SCAN_TIME * this.sampleRate);
    const minWindowSamples = Math.max(96, Math.floor(totalSamples / halfWidth) * 4);

    const frequencies: number[] = [];
    for (let x = 0; x < halfWidth; x++) {
      const startSample = Math.floor((x / halfWidth) * totalSamples);
      const endSample = Math.floor(((x + 1) / halfWidth) * totalSamples);
      const pixelPos = startPos + startSample;
      const windowEnd = Math.min(
        startPos + totalSamples,
        pixelPos + Math.max(endSample - startSample, minWindowSamples)
      );
      const duration = (windowEnd - pixelPos) / this.sampleRate;

      if (pixelPos + (endSample - startSample) >= samples.length) {
        frequencies.push(frequencies[frequencies.length - 1] ?? 1900);
        continue;
      }

      frequencies.push(this.detectFrequencyRange(samples, pixelPos, duration));
    }

    for (let x = 0; x < halfWidth; x++) {
      let freq = frequencies[x] ?? 0;

      if (x >= 2 && x < halfWidth - 2) {
        freq =
          [
            frequencies[x - 2] ?? 0,
            frequencies[x - 1] ?? 0,
            frequencies[x] ?? 0,
            frequencies[x + 1] ?? 0,
            frequencies[x + 2] ?? 0,
          ].sort((a, b) => a - b)[2] ?? 0;
      }

      const freqBlack = this.freqOffset ? FREQ_BLACK + this.freqOffset : FREQ_BLACK;
      const freqWhite = this.freqOffset ? FREQ_WHITE + this.freqOffset : FREQ_WHITE;
      const value = Math.max(
        0,
        Math.min(255, Math.round(((freq - freqBlack) / (freqWhite - freqBlack)) * 255))
      );

      const idx1 = y * mode.width + x * 2;
      const idx2 = y * mode.width + x * 2 + 1;

      if (componentType === 'U') {
        chromaU[idx1] = value;
        if (idx2 < mode.width * mode.lines) chromaU[idx2] = value;
      } else if (componentType === 'V') {
        chromaV[idx1] = value;
        if (idx2 < mode.width * mode.lines) chromaV[idx2] = value;
      }
    }

    return startPos + totalSamples;
  }

  decodeScanLinePD(
    samples: Float32Array,
    startPos: number,
    imageData: ImageData,
    chromaU: number[],
    chromaV: number[],
    y: number
  ): number {
    const mode = this.currentMode;
    const COMPONENT_TIME = mode.componentTime ?? 0;
    const width = mode.width;
    const lines = mode.lines;
    const y1 = Math.min(y + 1, lines - 1);
    const totalSamples = Math.floor(COMPONENT_TIME * this.sampleRate);

    const pixelSamples = totalSamples / width;
    const minWindowSamples = Math.max(96, Math.floor(pixelSamples) * 4);

    const decodeComponent = (baseOffset: number): number[] => {
      const result: number[] = [];
      const componentStart = startPos + baseOffset;
      for (let x = 0; x < width; x++) {
        const startSample = Math.floor((x / width) * totalSamples);
        const endSample = Math.floor(((x + 1) / width) * totalSamples);
        const pos = componentStart + startSample;
        const windowEnd = Math.min(
          componentStart + totalSamples,
          pos + Math.max(endSample - startSample, minWindowSamples)
        );
        const duration = (windowEnd - pos) / this.sampleRate;
        const freq = this.detectFrequencyRange(samples, pos, duration);
        const normalized = (freq - FREQ_BLACK) / (FREQ_WHITE - FREQ_BLACK);
        result.push(Math.max(0, Math.min(255, Math.round(normalized * 255))));
      }
      return result;
    };

    const y0values = decodeComponent(0);
    const ryValues = decodeComponent(totalSamples);
    const byValues = decodeComponent(totalSamples * 2);
    const y1values = decodeComponent(totalSamples * 3);

    for (let x = 0; x < width; x++) {
      const idx0 = (y * width + x) * 4;
      imageData.data[idx0] = y0values[x] ?? 0;
      imageData.data[idx0 + 1] = y0values[x] ?? 0;
      imageData.data[idx0 + 2] = y0values[x] ?? 0;
      imageData.data[idx0 + 3] = 255;

      const idx1 = (y1 * width + x) * 4;
      imageData.data[idx1] = y1values[x] ?? 0;
      imageData.data[idx1 + 1] = y1values[x] ?? 0;
      imageData.data[idx1 + 2] = y1values[x] ?? 0;
      imageData.data[idx1 + 3] = 255;

      chromaV[y * width + x] = ryValues[x] ?? 0;
      chromaV[y1 * width + x] = ryValues[x] ?? 0;
      chromaU[y * width + x] = byValues[x] ?? 0;
      chromaU[y1 * width + x] = byValues[x] ?? 0;
    }

    return startPos + totalSamples * 4;
  }

  convertYUVtoRGB(imageData: ImageData, chromaU: number[], chromaV: number[]): void {
    const mode = this.currentMode;
    for (let y = 0; y < mode.lines; y += 2) {
      const evenLine = y;
      const oddLine = Math.min(y + 1, mode.lines - 1);

      for (let x = 0; x < mode.width; x++) {
        const V = chromaV[evenLine * mode.width + x] || 128;
        const U = chromaU[oddLine * mode.width + x] || 128;

        for (let ly = evenLine; ly <= oddLine && ly < mode.lines; ly++) {
          const idx = (ly * mode.width + x) * 4;
          const Y = imageData.data[idx] ?? 0;

          imageData.data[idx] = Math.max(0, Math.min(255, Math.round(Y + 1.402 * (V - 128))));
          imageData.data[idx + 1] = Math.max(
            0,
            Math.min(255, Math.round(Y - 0.344136 * (U - 128) - 0.714136 * (V - 128)))
          );
          imageData.data[idx + 2] = Math.max(0, Math.min(255, Math.round(Y + 1.772 * (U - 128))));
        }
      }
    }
  }

  convertPDtoRGB(imageData: ImageData, chromaU: number[], chromaV: number[]): void {
    const mode = this.currentMode;
    const width = mode.width;
    const lines = mode.lines;

    for (let y = 0; y < lines; y++) {
      for (let x = 0; x < width; x++) {
        const idx = (y * width + x) * 4;
        const Y = imageData.data[idx] ?? 0;
        const RYadj = (chromaV[y * width + x] || 128) - 128;
        const BYadj = (chromaU[y * width + x] || 128) - 128;

        imageData.data[idx] = Math.max(0, Math.min(255, Math.round(Y + RYadj)));
        imageData.data[idx + 1] = Math.max(
          0,
          Math.min(255, Math.round(Y - 0.194 * BYadj - 0.509 * RYadj))
        );
        imageData.data[idx + 2] = Math.max(0, Math.min(255, Math.round(Y + BYadj)));
      }
    }
  }

  estimateFreqOffset(samples: Float32Array, firstSyncPos: number): number {
    const mode = this.currentMode;
    const lineTime = mode.syncPulse + mode.syncPorch + 0.088 + 0.0045 + 0.0015 + 0.044;
    const lineSamples = Math.floor(lineTime * this.sampleRate);
    const tolerance = Math.floor(lineSamples * 0.05);

    const offsets: number[] = [];
    let pos = firstSyncPos;

    for (let line = 0; line < 20; line++) {
      const syncPos = this.findSyncPulse(samples, pos - tolerance, pos + tolerance);
      if (syncPos === -1) break;

      const measuredFreq = this.detectFrequency(samples, syncPos, mode.syncPulse);
      offsets.push(measuredFreq - FREQ_SYNC);
      pos = syncPos + lineSamples;
    }

    if (offsets.length < 5) return 0;

    offsets.sort((a, b) => a - b);
    const medianOffset = offsets[Math.floor(offsets.length / 2)] ?? 0;

    return Math.abs(medianOffset) > 50 ? Math.round(medianOffset) : 0;
  }

  findSyncPulse(samples: Float32Array, startPos: number, endPos: number = samples.length): number {
    const mode = this.mode;
    const syncDuration = Math.max(0.004, mode?.syncPulse ?? 0.005);
    const samplesPerCheck = Math.floor(this.sampleRate * 0.0002);
    const searchEnd = Math.min(endPos, samples.length - Math.floor(syncDuration * this.sampleRate));
    const expectedSync = FREQ_SYNC + (this.visFreqShift || 0);

    for (let i = startPos; i < searchEnd; i += samplesPerCheck) {
      const freq = this.detectFrequency(samples, i, syncDuration);

      if (Math.abs(freq - expectedSync) < 200) {
        let syncValid = true;
        for (let j = 0; j < 3; j++) {
          const checkPos = i + Math.floor((syncDuration * this.sampleRate * j) / 3);
          if (
            Math.abs(this.detectFrequency(samples, checkPos, syncDuration / 3) - expectedSync) > 200
          ) {
            syncValid = false;
            break;
          }
        }
        if (syncValid) return i;
      }
    }

    return -1;
  }

  detectFrequency(samples: Float32Array, startIdx: number, duration: number): number {
    const numSamples = Math.floor(duration * this.sampleRate);
    const endIdx = Math.min(startIdx + numSamples, samples.length);

    if (endIdx - startIdx < 10) return 0;

    const testFreqs = [
      1100, 1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900, 2000, 2100, 2200, 2300,
    ];
    let maxMag = 0;
    let detectedFreq = 1500;

    for (const freq of testFreqs) {
      const magnitude = this.goertzel(samples, startIdx, endIdx, freq);
      if (magnitude > maxMag) {
        maxMag = magnitude;
        detectedFreq = freq;
      }
    }

    if (maxMag > 0.05) {
      for (let f = detectedFreq - 100; f <= detectedFreq + 100; f += 10) {
        const magnitude = this.goertzel(samples, startIdx, endIdx, f);
        if (magnitude > maxMag) {
          maxMag = magnitude;
          detectedFreq = f;
        }
      }
    }

    return detectedFreq;
  }

  goertzel(samples: Float32Array, startIdx: number, endIdx: number, targetFreq: number): number {
    const N = endIdx - startIdx;
    const k = (N * targetFreq) / this.sampleRate;
    const omega = (2 * Math.PI * k) / N;
    const coeff = 2 * Math.cos(omega);

    let s1 = 0;
    let s2 = 0;

    for (let i = startIdx; i < endIdx; i++) {
      const s0 = (samples[i] ?? 0) + coeff * s1 - s2;
      s2 = s1;
      s1 = s0;
    }

    const realPart = s1 - s2 * Math.cos(omega);
    const imagPart = s2 * Math.sin(omega);

    return Math.sqrt(realPart * realPart + imagPart * imagPart) / N;
  }
}
