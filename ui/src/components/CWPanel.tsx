import { useCallback, useEffect, useRef, useState } from 'react';
import type { CWSocketMessage } from '../types.js';

// nginx proxies /ws/cw → cw-decoder:8765; vite dev proxy also maps this path
const WS_PROTO = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const WS_URL = `${WS_PROTO}//${window.location.host}/ws/cw`;
const MAX_LINES = 200;

interface Line {
  id: number;
  ts: string;
  text: string;
  freq: number;
  power: number;
}

function StatusDot({ connected }: { connected: boolean }) {
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full mr-2 ${connected ? 'bg-emerald-400' : 'bg-red-500'}`}
      title={connected ? 'Connected' : 'Disconnected'}
    />
  );
}

export function CWPanel() {
  const [lines, setLines] = useState<Line[]>([]);
  const [currentLine, setCurrentLine] = useState('');
  const [connected, setConnected] = useState(false);
  const [statusFreq, setStatusFreq] = useState<number | null>(null);
  const [statusPower, setStatusPower] = useState<number | null>(null);
  const lineId = useRef(0);
  const bottomRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const finishLine = useCallback((text: string, ts: string, freq: number, power: number) => {
    if (!text.trim()) return;
    setLines((prev) => {
      const next = [...prev, { id: lineId.current++, ts, text, freq, power }];
      return next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next;
    });
    setCurrentLine('');
  }, []);

  useEffect(() => {
    let reconnectTimer: ReturnType<typeof setTimeout>;

    const connect = () => {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        reconnectTimer = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();

      ws.onmessage = (event: MessageEvent<string>) => {
        let msg: CWSocketMessage;
        try {
          msg = JSON.parse(event.data) as CWSocketMessage;
        } catch {
          return;
        }

        if (msg.type === 'char') {
          setStatusFreq(msg.freq);
          setStatusPower(msg.power);
          setCurrentLine((prev) => prev + msg.char);
        } else if (msg.type === 'word_space') {
          setCurrentLine((prev) => prev + ' ');
        } else if (msg.type === 'status') {
          setConnected(msg.connected);
          setStatusFreq(msg.freq);
        }
      };
    };

    connect();
    return () => {
      clearTimeout(reconnectTimer);
      wsRef.current?.close();
    };
  }, [finishLine]);

  // Auto-scroll to bottom
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [lines, currentLine]);

  const clearAll = () => {
    setLines([]);
    setCurrentLine('');
  };

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between mb-4 pb-4 border-b border-white/10">
        <div>
          <h2 className="text-white text-xl font-semibold tracking-wide">CW — Live Decode</h2>
          <p className="text-white/40 text-xs mt-0.5 font-mono">
            <StatusDot connected={connected} />
            {connected ? (
              <>
                {statusFreq != null && <span>{(statusFreq / 1e6).toFixed(4)} MHz</span>}
                {statusPower != null && (
                  <span className="ml-2 text-white/25">{statusPower.toFixed(1)} dB</span>
                )}
              </>
            ) : (
              <span className="text-red-400/80">Reconnecting…</span>
            )}
          </p>
        </div>
        <button
          onClick={clearAll}
          className="text-xs text-white/30 hover:text-white/60 transition-colors px-2 py-1 rounded border border-white/10 hover:border-white/20"
        >
          Clear
        </button>
      </div>

      {/* Terminal */}
      <div className="flex-1 overflow-y-auto font-mono text-sm rounded-lg bg-black/30 border border-white/8 p-4 min-h-64 max-h-[60vh]">
        {lines.length === 0 && !currentLine && (
          <p className="text-white/20 text-xs italic">Waiting for CW signal…</p>
        )}
        {lines.map((line) => (
          <div key={line.id} className="mb-1">
            <span className="text-white/20 text-xs mr-3 select-none">
              {new Date(line.ts).toLocaleTimeString()}
            </span>
            <span className="text-emerald-300">{line.text}</span>
          </div>
        ))}
        {currentLine && (
          <div className="mb-1">
            <span className="text-white/20 text-xs mr-3 select-none">
              {new Date().toLocaleTimeString()}
            </span>
            <span className="text-emerald-300">{currentLine}</span>
            <span className="cw-cursor" />
          </div>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
