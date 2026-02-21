/**
 * logShipper — intercepts console.log/warn/error and batches them to
 * POST /api/logs so they appear in `docker compose logs cw-decoder`.
 *
 * Call initLogShipper() once from main.tsx (main thread) and once from
 * each Web Worker that should ship logs.
 *
 * The shipper never throws — failures are swallowed so it cannot break
 * the rest of the application.
 */

interface LogEntry {
  level:   'log' | 'info' | 'warn' | 'error';
  source:  string;
  message: string;
  ts:      string;
}

const FLUSH_INTERVAL_MS = 1_000;
const FLUSH_ON_ERROR    = true;
const ENDPOINT          = '/api/logs';

let _buffer:  LogEntry[] = [];
let _source   = 'browser';
let _active   = false;

async function flush(): Promise<void> {
  if (_buffer.length === 0) return;
  const batch = _buffer.splice(0);
  try {
    await fetch(ENDPOINT, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(batch),
      // keepalive so the request survives page unload
      keepalive: true,
    });
  } catch {
    // Network error — drop silently rather than recursive-log
  }
}

function ship(level: LogEntry['level'], args: unknown[]): void {
  const message = args
    .map((a) => {
      if (typeof a === 'string') return a;
      try { return JSON.stringify(a); }
      catch { return String(a); }
    })
    .join(' ');

  _buffer.push({ level, source: _source, message, ts: new Date().toISOString() });

  if (FLUSH_ON_ERROR && (level === 'error' || level === 'warn')) {
    flush();
  }
}

/**
 * Initialise log shipping for the current JS context.
 *
 * @param source  Label added to every entry, e.g. 'iqWorker' or 'browser'.
 *                The prefix already present in messages (e.g. "[iqWorker]")
 *                is kept as-is so filtering by source or by text both work.
 */
export function initLogShipper(source = 'browser'): void {
  if (_active) return;
  _active = true;
  _source = source;

  const orig = {
    log:   console.log.bind(console),
    info:  console.info.bind(console),
    warn:  console.warn.bind(console),
    error: console.error.bind(console),
  };

  console.log   = (...a) => { orig.log(...a);   ship('log',   a); };
  console.info  = (...a) => { orig.info(...a);  ship('info',  a); };
  console.warn  = (...a) => { orig.warn(...a);  ship('warn',  a); };
  console.error = (...a) => { orig.error(...a); ship('error', a); };

  setInterval(flush, FLUSH_INTERVAL_MS);

  // Flush on page/worker close
  if (typeof self !== 'undefined') {
    self.addEventListener('unload', () => flush());
  }
}
