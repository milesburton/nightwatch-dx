import { useEffect, useRef, useState } from 'react';
import { addIQListener } from '../workers/iqWorkerSingleton.js';

// ── Colour LUT (black → blue → cyan → yellow → white) ───────────────────────

function buildColorLut(): Uint8ClampedArray {
  const lut = new Uint8ClampedArray(256 * 4);
  for (let i = 0; i < 256; i++) {
    let r = 0, g = 0, b = 0;
    if (i < 64) {
      b = Math.round((i / 63) * 200);
    } else if (i < 128) {
      const t = (i - 64) / 63;
      b = Math.round(200 + t * 55);
      g = Math.round(t * 220);
    } else if (i < 192) {
      const t = (i - 128) / 63;
      r = Math.round(t * 255);
      g = Math.round(220 + t * 35);
      b = Math.round(255 * (1 - t));
    } else {
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

const DB_MIN   = -110;
const DB_MAX   = -30;
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
  const loHz = centerHz - sampleRate / 2;
  return ((freqHz - loHz) / sampleRate) * width;
}

const SPECTRUM_HEIGHT = 80;
const WATERFALL_ROWS  = 400;

// ── Component ─────────────────────────────────────────────────────────────────

export function WaterfallPanel() {
  const specCanvasRef = useRef<HTMLCanvasElement>(null);
  const wfCanvasRef   = useRef<HTMLCanvasElement>(null);
  const [connected, setConnected]   = useState(false);
  const [centerFreq, setCenterFreq] = useState<number | null>(null);
  const [sampleRate, setSampleRate] = useState<number | null>(null);
  const wfDataRef = useRef<ImageData | null>(null);

  useEffect(() => {
    const unsub = addIQListener((msg) => {
      if (msg.type === 'status') {
        setConnected(msg.connected);
        setCenterFreq(msg.centerFreq);
        setSampleRate(msg.sampleRate);
        return;
      }
      if (msg.type !== 'fft') return;

      const { bins, centerFreq: cf, sampleRate: sr } = msg;
      setCenterFreq(cf);
      setSampleRate(sr);

      const specCanvas = specCanvasRef.current;
      const wfCanvas   = wfCanvasRef.current;
      if (!specCanvas || !wfCanvas) return;

      // If the container hasn't been measured yet, size the canvases now.
      let W = wfCanvas.width;
      if (W === 0) {
        const containerW = Math.floor(wfCanvas.getBoundingClientRect().width);
        if (containerW === 0) return;
        W = containerW;
        specCanvas.width  = containerW;
        specCanvas.height = SPECTRUM_HEIGHT;
        wfCanvas.width    = containerW;
        wfCanvas.height   = WATERFALL_ROWS;
        wfDataRef.current = null;
      }

      // ── Spectrum (instantaneous) ──────────────────────────────────────────
      if (specCanvas.height !== SPECTRUM_HEIGHT) specCanvas.height = SPECTRUM_HEIGHT;

      const specCtx = specCanvas.getContext('2d');
      if (specCtx) {
        specCtx.clearRect(0, 0, W, SPECTRUM_HEIGHT);

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
      if (wfCanvas.height !== WATERFALL_ROWS) wfCanvas.height = WATERFALL_ROWS;

      const wfCtx = wfCanvas.getContext('2d');
      if (!wfCtx) return;

      if (!wfDataRef.current || wfDataRef.current.width !== W || wfDataRef.current.height !== WATERFALL_ROWS) {
        wfDataRef.current = wfCtx.createImageData(W, WATERFALL_ROWS);
        // Pre-fill alpha to 255 so all rows are opaque black until data arrives.
        // createImageData initialises to all-zero which makes pixels transparent,
        // causing the canvas to show the page background instead of black.
        for (let i = 3; i < wfDataRef.current.data.length; i += 4) {
          wfDataRef.current.data[i] = 255;
        }
      }
      const wfData = wfDataRef.current;

      // Scroll waterfall down one row (copy rows 0‥ROWS-2 → rows 1‥ROWS-1).
      // Must iterate in reverse to avoid overwriting source before it's copied.
      const rowBytes = W * 4;
      for (let row = WATERFALL_ROWS - 1; row > 0; row--) {
        wfData.data.copyWithin(row * rowBytes, (row - 1) * rowBytes, row * rowBytes);
      }

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
    });
    return unsub;
  }, []);

  // Resize canvases on mount
  useEffect(() => {
    const resize = () => {
      const container = specCanvasRef.current?.parentElement;
      if (!container) return;
      const W = container.clientWidth || Math.floor(container.getBoundingClientRect().width);
      if (W === 0) return;
      if (specCanvasRef.current) {
        specCanvasRef.current.width  = W;
        specCanvasRef.current.height = SPECTRUM_HEIGHT;
      }
      if (wfCanvasRef.current) {
        wfCanvasRef.current.width  = W;
        wfCanvasRef.current.height = WATERFALL_ROWS;
      }
      wfDataRef.current = null;
    };
    resize();
    window.addEventListener('resize', resize);
    return () => window.removeEventListener('resize', resize);
  }, []);

  const bwMHz = sampleRate ? (sampleRate / 1e6).toFixed(1) : '—';
  const loMHz = centerFreq && sampleRate ? ((centerFreq - sampleRate / 2) / 1e6).toFixed(3) : '—';
  const hiMHz = centerFreq && sampleRate ? ((centerFreq + sampleRate / 2) / 1e6).toFixed(3) : '—';

  return (
    <div className="glass rounded-2xl p-6">
      <div className="flex items-center gap-2 mb-3">
        <h2 className="text-white text-lg font-semibold tracking-wide flex-1">
          Spectrum &amp; Waterfall
        </h2>
        <span
          className={`inline-block w-2 h-2 rounded-full ${connected ? 'bg-emerald-400' : 'bg-red-500'}`}
          title={connected ? 'Connected' : 'Connecting…'}
        />
        <span className="text-white/40 text-xs font-mono">
          {loMHz} – {hiMHz} MHz &nbsp;|&nbsp; {bwMHz} MHz BW
        </span>
      </div>

      <div className="relative w-full mb-1">
        <canvas
          ref={specCanvasRef}
          className="w-full block rounded-t-lg bg-black/60"
          style={{ height: `${SPECTRUM_HEIGHT}px` }}
        />
        <div className="absolute top-0 right-1 h-full flex flex-col justify-between pointer-events-none">
          {[-50, -62, -75, -87, -100].map((db) => (
            <span key={db} className="text-white/30 text-[9px] font-mono leading-none">{db}</span>
          ))}
        </div>
      </div>

      <canvas
        ref={wfCanvasRef}
        className="w-full block rounded-b-lg"
        style={{ height: `${WATERFALL_ROWS}px`, imageRendering: 'pixelated', minHeight: `${WATERFALL_ROWS}px` }}
      />

      {!connected && (
        <p className="text-white/30 text-xs text-center mt-2 italic">
          Connecting to IQ stream…
        </p>
      )}
    </div>
  );
}
