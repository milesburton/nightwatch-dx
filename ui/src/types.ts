// ── SSTV types (from sstv-toolkit) ───────────────────────────────────────────

export interface SSTVMode {
  name: string;
  visCode: number;
  scanTime: number;
  lines: number;
  width: number;
  colorScan: boolean;
  syncPulse: number;
  syncPorch: number;
  separatorPulse?: number;
  componentTime?: number;
  colorFormat: 'YUV' | 'RGB' | 'PD';
}

export interface ImageQuality {
  rAvg: number;
  gAvg: number;
  bAvg: number;
  brightness: number;
  verdict: 'good' | 'warn' | 'bad';
  warnings: string[];
}

export interface DecodeDiagnostics {
  mode: string;
  visCode: number | null;
  sampleRate: number;
  fileDuration: string | null;
  freqOffset: number;
  autoCalibrate: boolean;
  visEndPos: number | null;
  decodeTimeMs: number;
  quality: ImageQuality;
}

export interface DecodeState {
  url: string;
  filename: string;
  diagnostics: DecodeDiagnostics;
}

export interface WorkerDecodeRequest {
  type: 'decode';
  samples: Float32Array;
  sampleRate: number;
}

export interface WorkerResultMessage {
  type: 'result';
  pixels: Uint8ClampedArray;
  width: number;
  height: number;
  diagnostics: DecodeDiagnostics;
}

export interface WorkerErrorMessage {
  type: 'error';
  message: string;
}

export type WorkerOutboundMessage = WorkerResultMessage | WorkerErrorMessage;

export interface DecodeResult {
  imageUrl: string;
  diagnostics: DecodeDiagnostics;
}

export interface DecodeImageResult {
  pixels: Uint8ClampedArray;
  width: number;
  height: number;
  diagnostics: DecodeDiagnostics;
}

// ── IQ Worker messages (browser ↔ iqWorker.ts) ───────────────────────────────

/** Messages sent FROM the IQ worker TO the main thread */
export type IQWorkerMessage =
  | { type: 'fft';    bins: number[]; centerFreq: number; sampleRate: number }
  | { type: 'status'; connected: boolean; centerFreq: number; sampleRate: number }
  | { type: 'error';  message: string };

// ── CW types (kept for compatibility) ────────────────────────────────────────

export type CWSocketMessage =
  | { type: 'char'; ts: string; char: string; freq: number; power: number }
  | { type: 'word_space' }
  | { type: 'status'; connected: boolean; freq: number }
  | { type: 'error'; message: string };

// ── Spectrum / waterfall types ────────────────────────────────────────────────

export type SpectrumMessage =
  | { type: 'fft'; bins: number[]; centerFreq: number; sampleRate: number; ts: string }
  | { type: 'status'; connected: boolean }
  | { type: 'error'; message: string };
