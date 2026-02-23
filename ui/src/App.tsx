import { useEffect, useRef, useState } from 'react';
import { CWLogPanel } from './components/CWLogPanel.js';
import { EasyPalGalleryPanel } from './components/EasyPalGalleryPanel.js';
import { PSK31Panel } from './components/PSK31Panel.js';
import { ServerStatusPanel } from './components/ServerStatusPanel.js';
import { SSTVGalleryPanel } from './components/SSTVGalleryPanel.js';
import { WaterfallPanel } from './components/WaterfallPanel.js';
import type { CWSocketMessage } from './types.js';
import { useVersionPoller } from './utils/useVersionPoller.js';
import { addIQListener } from './workers/iqWorkerSingleton.js';

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
// Hardware is considered down if no FFT data for this long (rtl_tcp crashed/unplugged).
// 15 s gives the iqWorker time to fill its ring buffer and send the first FFT on startup.
const HW_TIMEOUT_MS = 15_000;

function useSignalStatus() {
  const [iqConnected, setIqConnected] = useState(false);
  const [hwDown, setHwDown] = useState(false);
  const [cwActive, setCwActive] = useState(false);
  const [sstActive, setSstActive] = useState(false);
  const [easypalActive, setEasypalActive] = useState(false);
  const [psk31Active, setPsk31Active] = useState(false);

  const cwTimer      = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sstTimer     = useRef<ReturnType<typeof setTimeout> | null>(null);
  const easypalTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const psk31Timer   = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hwTimer      = useRef<ReturnType<typeof setTimeout> | null>(null);

  // IQ / spectrum — track connection AND data flow
  useEffect(() => {
    // Start the hardware watchdog immediately; it resets on every FFT
    hwTimer.current = setTimeout(() => setHwDown(true), HW_TIMEOUT_MS);

    return addIQListener((msg) => {
      if (msg.type === 'status') {
        setIqConnected(msg.connected);
      }
      if (msg.type === 'fft') {
        // Data is flowing — hardware is up
        setHwDown(false);
        if (hwTimer.current) clearTimeout(hwTimer.current);
        hwTimer.current = setTimeout(() => setHwDown(true), HW_TIMEOUT_MS);
      }
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
      } catch {
        /* ignore */
      }
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
      } catch {
        /* ignore */
      }
    };

    return () => {
      ws.close();
      if (sstTimer.current) clearTimeout(sstTimer.current);
    };
  }, []);

  // EasyPal active — listen for frame events
  useEffect(() => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/easypal`);

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data as string) as { type: string };
        if (msg.type === 'frame') {
          setEasypalActive(true);
          if (easypalTimer.current) clearTimeout(easypalTimer.current);
          easypalTimer.current = setTimeout(() => setEasypalActive(false), ACTIVE_TTL_MS);
        }
      } catch {
        /* ignore */
      }
    };

    return () => {
      ws.close();
      if (easypalTimer.current) clearTimeout(easypalTimer.current);
    };
  }, []);

  // PSK31 active — listen for char/word_space events
  useEffect(() => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/psk31`);

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data as string) as CWSocketMessage;
        if (msg.type === 'char' || msg.type === 'word_space') {
          setPsk31Active(true);
          if (psk31Timer.current) clearTimeout(psk31Timer.current);
          psk31Timer.current = setTimeout(() => setPsk31Active(false), ACTIVE_TTL_MS);
        }
      } catch {
        /* ignore */
      }
    };

    return () => {
      ws.close();
      if (psk31Timer.current) clearTimeout(psk31Timer.current);
    };
  }, []);

  return { iqConnected, hwDown, cwActive, sstActive, easypalActive, psk31Active };
}

function Subtitle({
  iqConnected,
  hwDown,
  cwActive,
  sstActive,
  easypalActive,
  psk31Active,
}: {
  iqConnected: boolean;
  hwDown: boolean;
  cwActive: boolean;
  sstActive: boolean;
  easypalActive: boolean;
  psk31Active: boolean;
}) {
  const parts: string[] = [];

  if (hwDown) {
    parts.push('SDR Hardware Offline');
  } else if (!iqConnected) {
    parts.push('Connecting…');
  } else {
    parts.push('Scanning 20m');
    if (cwActive) parts.push('CW Active');
    if (psk31Active) parts.push('PSK31 Active');
    if (sstActive) parts.push('SSTV Active');
    if (easypalActive) parts.push('EasyPal Active');
  }

  const activeLabels = new Set(['CW Active', 'PSK31 Active', 'SSTV Active', 'EasyPal Active']);

  return (
    <p className="text-sm text-white/50 tracking-widest uppercase font-medium">
      {parts.map((part, i) => (
        <span key={part}>
          {i > 0 && <span className="text-white/25"> · </span>}
          <span
            className={
              part === 'SDR Hardware Offline'
                ? 'text-red-400'
                : activeLabels.has(part)
                  ? 'text-emerald-400'
                  : undefined
            }
          >
            {part}
          </span>
        </span>
      ))}
    </p>
  );
}

// ── App ────────────────────────────────────────────────────────────────────────

function useDocumentTitle(hwDown: boolean, iqConnected: boolean, cwActive: boolean, sstActive: boolean, easypalActive: boolean, psk31Active: boolean) {
  useEffect(() => {
    if (hwDown) {
      document.title = '⚠ Offline — dx-watch';
    } else if (!iqConnected) {
      document.title = 'Connecting… — dx-watch';
    } else {
      const active: string[] = [];
      if (cwActive) active.push('CW');
      if (psk31Active) active.push('PSK31');
      if (sstActive) active.push('SSTV');
      if (easypalActive) active.push('EasyPal');
      if (active.length > 0) {
        document.title = `● ${active.join(' + ')} — dx-watch`;
      } else {
        document.title = 'Scanning 20m — dx-watch';
      }
    }
  }, [hwDown, iqConnected, cwActive, sstActive, easypalActive, psk31Active]);
}

export default function App() {
  useStarfield();
  useVersionPoller();
  const { iqConnected, hwDown, cwActive, sstActive, easypalActive, psk31Active } = useSignalStatus();
  useDocumentTitle(hwDown, iqConnected, cwActive, sstActive, easypalActive, psk31Active);

  return (
    <>
      <canvas id="stars-canvas" />

      {/* Full-screen red overlay when SDR hardware is offline */}
      {hwDown && (
        <div
          className="fixed inset-0 z-50 pointer-events-none"
          style={{ background: 'rgba(220,38,38,0.15)', borderTop: '3px solid rgba(220,38,38,0.8)' }}
          role="alert"
          aria-live="assertive"
        >
          <div
            className="flex items-center justify-center gap-2 px-4 py-2"
            style={{ background: 'rgba(220,38,38,0.75)' }}
          >
            <span className="text-white font-bold text-sm tracking-wider uppercase">
              ⚠ SDR Hardware Offline — check RTL-SDR USB connection
            </span>
          </div>
        </div>
      )}

      <div className="w-full max-w-7xl mx-auto px-6 py-10">
        <header className="text-center mb-10">
          <h1 className="text-5xl font-bold mb-3 tracking-tight text-white drop-shadow-lg">
            dx-watch
          </h1>
          <Subtitle
            iqConnected={iqConnected}
            hwDown={hwDown}
            cwActive={cwActive}
            sstActive={sstActive}
            easypalActive={easypalActive}
            psk31Active={psk31Active}
          />
        </header>

        <main className="flex flex-col gap-6">
          <WaterfallPanel />
          <CWLogPanel />
          <PSK31Panel />
          <SSTVGalleryPanel />
          <EasyPalGalleryPanel />
          <ServerStatusPanel />
        </main>

        <footer className="text-center text-white/40 py-6 text-sm mt-6 space-y-1">
          <div className="font-mono text-white/60 text-xs tracking-wider">
            {__APP_VERSION__} &nbsp;·&nbsp; {__BUILD_DATE__}
          </div>
          <div>
            <a
              href="https://github.com/milesburton/dx-watch"
              target="_blank"
              rel="noopener noreferrer"
              className="text-white/40 hover:text-white/70 transition-colors"
            >
              milesburton/dx-watch
            </a>
          </div>
        </footer>
      </div>
    </>
  );
}
