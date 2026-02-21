import { useEffect, useRef, useState } from 'react';
import { CWLogPanel } from './components/CWLogPanel.js';
import { SSTVGalleryPanel } from './components/SSTVGalleryPanel.js';
import { WaterfallPanel } from './components/WaterfallPanel.js';
import type { CWSocketMessage } from './types.js';
import { addIQListener } from './workers/iqWorkerSingleton.js';
import { useVersionPoller } from './utils/useVersionPoller.js';

// ── Starfield background ───────────────────────────────────────────────────────

function useStarfield() {
  useEffect(() => {
    const canvas = document.getElementById('stars-canvas') as HTMLCanvasElement | null;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    type Star = { x: number; y: number; r: number; speed: number; opacity: number };
    let stars: Star[] = [];
    let animId: number;

    const resize = () => {
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
      stars = Array.from({ length: 220 }, () => ({
        x: Math.random() * canvas.width,
        y: Math.random() * canvas.height,
        r: Math.random() * 1.4 + 0.3,
        speed: Math.random() * 0.15 + 0.04,
        opacity: Math.random() * 0.6 + 0.2,
      }));
    };

    const draw = () => {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      for (const s of stars) {
        s.y += s.speed;
        if (s.y > canvas.height) {
          s.y = 0;
          s.x = Math.random() * canvas.width;
        }
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(200, 215, 255, ${s.opacity})`;
        ctx.fill();
      }
      animId = requestAnimationFrame(draw);
    };

    resize();
    draw();
    window.addEventListener('resize', resize);
    return () => {
      cancelAnimationFrame(animId);
      window.removeEventListener('resize', resize);
    };
  }, []);
}

// ── Dynamic subtitle ───────────────────────────────────────────────────────────

// A signal is considered "active" if we received something from it within
// the last ACTIVE_TTL_MS milliseconds.
const ACTIVE_TTL_MS = 10_000;

function useSignalStatus() {
  const [iqConnected,  setIqConnected]  = useState(false);
  const [cwActive,     setCwActive]     = useState(false);
  const [sstActive,    setSstActive]    = useState(false);

  const cwTimer  = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sstTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // IQ / spectrum connected state
  useEffect(() => {
    return addIQListener((msg) => {
      if (msg.type === 'status') setIqConnected(msg.connected);
    });
  }, []);

  // CW active — listen for char/word_space events
  useEffect(() => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/cw`);

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data as string) as CWSocketMessage;
        if (msg.type === 'char' || msg.type === 'word_space') {
          setCwActive(true);
          if (cwTimer.current) clearTimeout(cwTimer.current);
          cwTimer.current = setTimeout(() => setCwActive(false), ACTIVE_TTL_MS);
        }
      } catch { /* ignore */ }
    };

    return () => {
      ws.close();
      if (cwTimer.current) clearTimeout(cwTimer.current);
    };
  }, []);

  // SSTV active — listen for frame events
  useEffect(() => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/sstv`);

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data as string) as { type: string };
        if (msg.type === 'frame') {
          setSstActive(true);
          if (sstTimer.current) clearTimeout(sstTimer.current);
          sstTimer.current = setTimeout(() => setSstActive(false), ACTIVE_TTL_MS);
        }
      } catch { /* ignore */ }
    };

    return () => {
      ws.close();
      if (sstTimer.current) clearTimeout(sstTimer.current);
    };
  }, []);

  return { iqConnected, cwActive, sstActive };
}

function Subtitle({ iqConnected, cwActive, sstActive }: {
  iqConnected: boolean;
  cwActive: boolean;
  sstActive: boolean;
}) {
  const parts: string[] = [];

  if (!iqConnected) {
    parts.push('Connecting…');
  } else {
    parts.push('Scanning 20m');
    if (cwActive)  parts.push('CW Active');
    if (sstActive) parts.push('SSTV Active');
  }

  return (
    <p className="text-sm text-white/50 tracking-widest uppercase font-medium">
      {parts.map((part, i) => (
        <span key={part}>
          {i > 0 && <span className="text-white/25"> · </span>}
          <span className={
            (part === 'CW Active' || part === 'SSTV Active')
              ? 'text-emerald-400'
              : undefined
          }>
            {part}
          </span>
        </span>
      ))}
    </p>
  );
}

// ── App ────────────────────────────────────────────────────────────────────────

export default function App() {
  useStarfield();
  useVersionPoller();
  const { iqConnected, cwActive, sstActive } = useSignalStatus();

  return (
    <>
      <canvas id="stars-canvas" />
      <div className="w-full max-w-7xl mx-auto px-6 py-10">
        <header className="text-center mb-10">
          <h1 className="text-5xl font-bold mb-3 tracking-tight text-white drop-shadow-lg">
            20m Signal Decoder
          </h1>
          <Subtitle iqConnected={iqConnected} cwActive={cwActive} sstActive={sstActive} />
        </header>

        <main className="flex flex-col gap-6">
          <WaterfallPanel />
          <CWLogPanel />
          <SSTVGalleryPanel />
        </main>

        <footer className="text-center text-white/40 py-6 text-sm mt-6 space-y-1">
          <div className="font-mono text-white/60 text-xs tracking-wider">
            {__APP_VERSION__} &nbsp;·&nbsp; {__BUILD_DATE__}
          </div>
          <div>
            <a
              href="https://github.com/milesburton/gmktec-sdr-project"
              target="_blank"
              rel="noopener noreferrer"
              className="text-white/40 hover:text-white/70 transition-colors"
            >
              milesburton/gmktec-sdr-project
            </a>
          </div>
        </footer>
      </div>
    </>
  );
}
