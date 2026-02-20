// ── SSTV types (from sstv-toolkit) ───────────────────────────────────────────

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

// ── CW types ─────────────────────────────────────────────────────────────────

export interface CWMessage {
  /** ISO timestamp from decoder */
  ts: string;
  /** Decoded text so far on this line */
  text: string;
  /** Frequency being decoded (Hz) */
  freq: number;
  /** Signal power (dB) */
  power: number;
}

export type CWSocketMessage =
  | { type: 'char'; ts: string; char: string; freq: number; power: number }
  | { type: 'word_space' }
  | { type: 'status'; connected: boolean; freq: number }
  | { type: 'error'; message: string };

// ── SSTV live types ───────────────────────────────────────────────────────────

export type SSTVSocketMessage =
  | { type: 'frame'; imageData: string; mode: string; ts: string }
  | { type: 'status'; connected: boolean }
  | { type: 'error'; message: string };

// ── Spectrum / waterfall types ────────────────────────────────────────────────

export type SpectrumMessage =
  | { type: 'fft'; bins: number[]; centerFreq: number; sampleRate: number; ts: string }
  | { type: 'status'; connected: boolean }
  | { type: 'error'; message: string };
