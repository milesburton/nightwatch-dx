import { useEffect, useRef, useState } from 'react';
import type { SpectrumMessage } from '../types.js';

const WS_PROTO = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const WS_URL = `${WS_PROTO}//${window.location.host}/ws/spectrum`;

// ── Colour LUT (black → blue → cyan → yellow → white) ───────────────────────

function buildColorLut(): Uint8ClampedArray {
  const lut = new Uint8ClampedArray(256 * 4);
  for (let i = 0; i < 256; i++) {
    let r = 0, g = 0, b = 0;
    if (i < 64) {
      // black → blue
      b = Math.round((i / 63) * 200);
    } else if (i < 128) {
      // blue → cyan
      const t = (i - 64) / 63;
      b = Math.round(200 + t * 55);
      g = Math.round(t * 220);
    } else if (i < 192) {
      // cyan → yellow
      const t = (i - 128) / 63;
      r = Math.round(t * 255);
      g = Math.round(220 + t * 35);
      b = Math.round(255 * (1 - t));
    } else {
      // yellow → white
      const t = (i - 192) / 63;
      r = 255;
      g = 255;
      b = Math.round(t * 255);
    }
    lut[i * 4]     = r;
    lut[i * 4 + 1] = g;
    lut[i * 4 + 2] = b;
    lut[i * 4 + 3] = 255;
  }
  return lut;
}

const COLOR_LUT = buildColorLut();

// Measured noise floor ~-44 dBFS, peaks at ~-30 dBFS (15 dB dynamic range).
// Tight 30 dB window: noise sits at dark-blue end, peaks jump to yellow/white.
const DB_MIN = -55;
const DB_MAX = -25;
const DB_RANGE = DB_MAX - DB_MIN;

function dbToLutIndex(db: number): number {
  const clamped = Math.max(DB_MIN, Math.min(DB_MAX, db));
  return Math.round(((clamped - DB_MIN) / DB_RANGE) * 255);
}

// ── 20m band markers ─────────────────────────────────────────────────────────

const BAND_MARKERS = [
  { freqHz: 14_000_000, label: '14.000\nCW' },
  { freqHz: 14_025_000, label: '14.025\nQRP' },
  { freqHz: 14_070_000, label: '14.070\nFT8' },
  { freqHz: 14_100_000, label: '14.100\nBcn' },
  { freqHz: 14_175_000, label: '14.175\nCtr' },
  { freqHz: 14_230_000, label: '14.230\nSSTV' },
];

function freqToX(freqHz: number, centerHz: number, sampleRate: number, width: number): number {
  const bwHz = sampleRate;
  const loHz = centerHz - bwHz / 2;
  return ((freqHz - loHz) / bwHz) * width;
}

// ── Constants ─────────────────────────────────────────────────────────────────

const SPECTRUM_HEIGHT = 80;   // px — instantaneous power spectrum
const WATERFALL_ROWS  = 200;  // scrolling history rows

// ── Component ─────────────────────────────────────────────────────────────────

export function WaterfallPanel() {
  const specCanvasRef = useRef<HTMLCanvasElement>(null);
  const wfCanvasRef   = useRef<HTMLCanvasElement>(null);
  const [connected, setConnected]     = useState(false);
  const [centerFreq, setCenterFreq]   = useState<number | null>(null);
  const [sampleRate, setSampleRate]   = useState<number | null>(null);

  // Keep a rolling ImageData for the waterfall
  const wfDataRef = useRef<ImageData | null>(null);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout>;

    const connect = () => {
      ws = new WebSocket(WS_URL);

      ws.onopen = () => setConnected(true);

      ws.onclose = () => {
        setConnected(false);
        reconnectTimer = setTimeout(connect, 3000);
      };

      ws.onerror = () => ws?.close();

      ws.onmessage = (e: MessageEvent<string>) => {
        let msg: SpectrumMessage;
        try { msg = JSON.parse(e.data) as SpectrumMessage; } catch { return; }

        if (msg.type === 'status') {
          setConnected(msg.connected);
          return;
        }
        if (msg.type !== 'fft') return;

        const { bins, centerFreq: cf, sampleRate: sr } = msg;
        setCenterFreq(cf);
        setSampleRate(sr);

        const specCanvas = specCanvasRef.current;
        const wfCanvas   = wfCanvasRef.current;
        if (!specCanvas || !wfCanvas) return;

        const W = specCanvas.width;

        // ── Spectrum (instantaneous) ──────────────────────────────────────────
        const specCtx = specCanvas.getContext('2d');
        if (specCtx) {
          specCtx.clearRect(0, 0, W, SPECTRUM_HEIGHT);

          // Background grid
          specCtx.strokeStyle = 'rgba(255,255,255,0.06)';
          specCtx.lineWidth   = 1;
          for (let db = DB_MIN; db <= DB_MAX; db += 10) {
            const normY = (db - DB_MIN) / DB_RANGE;
            const y = Math.round(normY * SPECTRUM_HEIGHT);
            specCtx.beginPath();
            specCtx.moveTo(0, SPECTRUM_HEIGHT - y);
            specCtx.lineTo(W, SPECTRUM_HEIGHT - y);
            specCtx.stroke();
          }

          // Spectrum line
          specCtx.beginPath();
          specCtx.strokeStyle = '#22d3ee';
          specCtx.lineWidth = 1.5;
          const N = bins.length;
          for (let i = 0; i < N; i++) {
            const x = (i / N) * W;
            const normY = (Math.max(DB_MIN, Math.min(DB_MAX, bins[i])) - DB_MIN) / DB_RANGE;
            const y = SPECTRUM_HEIGHT - normY * SPECTRUM_HEIGHT;
            if (i === 0) specCtx.moveTo(x, y);
            else specCtx.lineTo(x, y);
          }
          specCtx.stroke();

          // Frequency markers
          if (cf && sr) {
            specCtx.font = '9px monospace';
            specCtx.textAlign = 'center';
            for (const m of BAND_MARKERS) {
              const x = freqToX(m.freqHz, cf, sr, W);
              if (x < 0 || x > W) continue;
              specCtx.strokeStyle = 'rgba(255,255,255,0.25)';
              specCtx.lineWidth = 1;
              specCtx.setLineDash([3, 4]);
              specCtx.beginPath();
              specCtx.moveTo(x, 0);
              specCtx.lineTo(x, SPECTRUM_HEIGHT);
              specCtx.stroke();
              specCtx.setLineDash([]);
              specCtx.fillStyle = 'rgba(255,255,255,0.5)';
              const lines = m.label.split('\n');
              lines.forEach((line, li) => {
                specCtx.fillText(line, x, 10 + li * 10);
              });
            }
          }
        }

        // ── Waterfall (scrolling history) ────────────────────────────────────
        const wfCtx = wfCanvas.getContext('2d');
        if (!wfCtx) return;

        if (!wfDataRef.current || wfDataRef.current.width !== W || wfDataRef.current.height !== WATERFALL_ROWS) {
          wfDataRef.current = wfCtx.createImageData(W, WATERFALL_ROWS);
        }
        const wfData = wfDataRef.current;

        // Scroll existing rows down by one
        wfData.data.copyWithin(W * 4, 0, W * (WATERFALL_ROWS - 1) * 4);

        // Write new row at top (pixel 0..W-1 row 0)
        const N = bins.length;
        for (let x = 0; x < W; x++) {
          const binIdx = Math.floor((x / W) * N);
          const lut    = dbToLutIndex(bins[Math.min(binIdx, N - 1)]);
          wfData.data[x * 4]     = COLOR_LUT[lut * 4];
          wfData.data[x * 4 + 1] = COLOR_LUT[lut * 4 + 1];
          wfData.data[x * 4 + 2] = COLOR_LUT[lut * 4 + 2];
          wfData.data[x * 4 + 3] = 255;
        }

        wfCtx.putImageData(wfData, 0, 0);

        // Marker lines on waterfall
        if (cf && sr) {
          wfCtx.strokeStyle = 'rgba(255,255,255,0.2)';
          wfCtx.lineWidth   = 1;
          wfCtx.setLineDash([4, 6]);
          for (const m of BAND_MARKERS) {
            const x = freqToX(m.freqHz, cf, sr, W);
            if (x < 0 || x > W) continue;
            wfCtx.beginPath();
            wfCtx.moveTo(x, 0);
            wfCtx.lineTo(x, WATERFALL_ROWS);
            wfCtx.stroke();
          }
          wfCtx.setLineDash([]);
        }
      };
    };

    connect();
    return () => {
      clearTimeout(reconnectTimer);
      ws?.close();
    };
  }, []);

  // Resize canvases on mount
  useEffect(() => {
    const resize = () => {
      const container = specCanvasRef.current?.parentElement;
      if (!container) return;
      const W = container.clientWidth;
      if (specCanvasRef.current) {
        specCanvasRef.current.width  = W;
        specCanvasRef.current.height = SPECTRUM_HEIGHT;
      }
      if (wfCanvasRef.current) {
        wfCanvasRef.current.width  = W;
        wfCanvasRef.current.height = WATERFALL_ROWS;
      }
      wfDataRef.current = null; // reset on resize
    };
    resize();
    window.addEventListener('resize', resize);
    return () => window.removeEventListener('resize', resize);
  }, []);

  const bwMHz = sampleRate ? (sampleRate / 1e6).toFixed(1) : '—';
  const loMHz = centerFreq && sampleRate
    ? ((centerFreq - sampleRate / 2) / 1e6).toFixed(3)
    : '—';
  const hiMHz = centerFreq && sampleRate
    ? ((centerFreq + sampleRate / 2) / 1e6).toFixed(3)
    : '—';

  return (
    <div className="glass rounded-2xl p-6">
      <div className="flex items-center gap-2 mb-3">
        <h2 className="text-white text-lg font-semibold tracking-wide flex-1">
          Spectrum &amp; Waterfall
        </h2>
        <span
          className={`inline-block w-2 h-2 rounded-full ${connected ? 'bg-emerald-400' : 'bg-red-500'}`}
          title={connected ? 'Connected' : 'Reconnecting…'}
        />
        <span className="text-white/40 text-xs font-mono">
          {loMHz} – {hiMHz} MHz &nbsp;|&nbsp; {bwMHz} MHz BW
        </span>
      </div>

      {/* Spectrum */}
      <div className="relative w-full mb-1">
        <canvas
          ref={specCanvasRef}
          className="w-full block rounded-t-lg bg-black/60"
          style={{ height: `${SPECTRUM_HEIGHT}px` }}
        />
        {/* dB scale labels */}
        <div className="absolute top-0 right-1 h-full flex flex-col justify-between pointer-events-none">
          {[-25, -32, -40, -47, -55].map((db) => (
            <span key={db} className="text-white/30 text-[9px] font-mono leading-none">{db}</span>
          ))}
        </div>
      </div>

      {/* Waterfall */}
      <canvas
        ref={wfCanvasRef}
        className="w-full block rounded-b-lg"
        style={{ height: `${WATERFALL_ROWS}px`, imageRendering: 'pixelated' }}
      />

      {!connected && (
        <p className="text-white/30 text-xs text-center mt-2 italic">
          Connecting to spectrum service…
        </p>
      )}
    </div>
  );
}
