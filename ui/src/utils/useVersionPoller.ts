/**
 * useVersionPoller — polls /version.json every 60 seconds and reloads the
 * page when the deployed version differs from the version baked in at build
 * time (__APP_VERSION__).
 *
 * No-ops in dev (Vite's HMR handles updates there).
 */

import { useEffect } from 'react';

const POLL_INTERVAL_MS = 60_000;

export function useVersionPoller() {
  useEffect(() => {
    // Skip polling in Vite dev server (location is localhost / 127.x)
    const host = self.location.hostname;
    if (host === 'localhost' || host === '127.0.0.1' || host.startsWith('192.168.')) return;

    const check = async () => {
      try {
        const res = await fetch('/version.json', { cache: 'no-store' });
        if (!res.ok) return;
        const { version } = (await res.json()) as { version: string };
        if (version && version !== __APP_VERSION__) {
          window.location.reload();
        }
      } catch {
        // Network error — ignore, try again next interval
      }
    };

    const id = setInterval(() => { void check(); }, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, []);
}
