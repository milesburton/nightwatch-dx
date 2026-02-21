/**
 * logShipper — intercepts console.log/warn/error and batches them over the
 * /ws/cw WebSocket so they appear in `docker compose logs cw-decoder`.
 *
 * Piggybacking on /ws/cw (rather than a dedicated endpoint) works around
 * network intermediaries that intercept POST requests or unknown WS paths.
 * The cw-decoder ws_handler accepts inbound JSON log arrays on that socket.
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

const _buffer: LogEntry[] = [];
let _source  = 'browser';
let _active  = false;
const _conn  = { ws: null as WebSocket | null };

function wsUrl(): string {
  const proto = (typeof location !== 'undefined' && location.protocol === 'https:') ? 'wss' : 'ws';
  const host  = typeof location !== 'undefined' ? location.host : 'localhost:8080';
  return `${proto}://${host}/ws/cw`;
}

function openWs(): void {
  try {
    const ws = new WebSocket(wsUrl());
    ws.onclose = () => { _conn.ws = null; setTimeout(openWs, 5_000); };
    ws.onerror = () => { /* ignore */ };
    _conn.ws = ws;
  } catch {
    _conn.ws = null;
    setTimeout(openWs, 5_000);
  }
}

function flush(): void {
  if (_buffer.length === 0) return;
  if (_conn.ws?.readyState !== WebSocket.OPEN) return;
  const batch = _buffer.splice(0);
  try {
    _conn.ws.send(JSON.stringify(batch));
  } catch {
    // Drop silently — socket may have closed between readyState check and send
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
 */
export function initLogShipper(source = 'browser'): void {
  if (_active) return;
  _active = true;
  _source = source;

  openWs();

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

  if (typeof self !== 'undefined') {
    self.addEventListener('unload', () => flush());
  }
}
