// ── IQ Worker messages (browser ↔ iqWorker.ts) ───────────────────────────────

/** Messages sent FROM the IQ worker TO the main thread */
export type IQWorkerMessage =
  | { type: 'fft'; bins: number[]; centerFreq: number; sampleRate: number }
  | { type: 'status'; connected: boolean; centerFreq: number; sampleRate: number }
  | { type: 'error'; message: string };

// ── CW WebSocket messages (/ws/cw) ────────────────────────────────────────────

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
