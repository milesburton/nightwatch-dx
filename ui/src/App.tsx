import { useEffect } from 'react';
import { CWLogPanel } from './components/CWLogPanel.js';
import { SSTVGalleryPanel } from './components/SSTVGalleryPanel.js';
import { WaterfallPanel } from './components/WaterfallPanel.js';
import { useVersionPoller } from './utils/useVersionPoller.js';

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

export default function App() {
  useStarfield();
  useVersionPoller();

  return (
    <>
      <canvas id="stars-canvas" />
      <div className="w-full max-w-7xl mx-auto px-6 py-10">
        <header className="text-center mb-10">
          <h1 className="text-5xl font-bold mb-3 tracking-tight text-white drop-shadow-lg">
            SDR Monitor
          </h1>
          <p className="text-sm text-white/50 tracking-widest uppercase font-medium">
            Live CW &amp; SSTV · 20m · RTL-SDR
          </p>
        </header>

        <main className="flex flex-col gap-6">
          {/* Spectrum / waterfall — full width */}
          <WaterfallPanel />

          {/* CW session log — full width (master-detail needs horizontal space) */}
          <CWLogPanel />

          {/* SSTV auto-detect gallery */}
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
