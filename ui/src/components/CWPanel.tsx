import { useCallback, useEffect, useRef, useState } from 'react';
import type { IQWorkerMessage } from '../types.js';

const MAX_LINES = 200;

interface Line {
  id: number;
  ts: string;
  text: string;
  freq: number;
}

function StatusDot({ connected }: { connected: boolean }) {
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full mr-2 ${connected ? 'bg-emerald-400' : 'bg-red-500'}`}
      title={connected ? 'Connected' : 'Disconnected'}
    />
  );
}

// IQ worker singleton shared across CWPanel and WaterfallPanel mounts
// (exported so WaterfallPanel can attach its own listener)
let _worker: Worker | null = null;
const _listeners = new Set<(msg: IQWorkerMessage) => void>();

function getIQWorker(): Worker {
  if (!_worker) {
    _worker = new Worker(new URL('../workers/iqWorker.ts', import.meta.url), { type: 'module' });
    _worker.onmessage = (e: MessageEvent<IQWorkerMessage>) => {
      for (const fn of _listeners) fn(e.data);
    };
  }
  return _worker;
}

export function addIQListener(fn: (msg: IQWorkerMessage) => void): () => void {
  getIQWorker();   // ensure worker is started
  _listeners.add(fn);
  return () => _listeners.delete(fn);
}

export function CWPanel() {
  const [lines, setLines] = useState<Line[]>([]);
  const [currentLine, setCurrentLine] = useState('');
  const [connected, setConnected] = useState(false);
  const [statusFreq, setStatusFreq] = useState<number | null>(null);
  const lineId = useRef(0);
  const bottomRef = useRef<HTMLDivElement>(null);
  const currentLineRef = useRef('');
  const currentTsRef = useRef('');

  const flushLine = useCallback(() => {
    const text = currentLineRef.current;
    const ts   = currentTsRef.current;
    if (!text.trim()) return;
    setLines((prev) => {
      const next = [...prev, { id: lineId.current++, ts, text, freq: 0 }];
      return next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next;
    });
    currentLineRef.current = '';
    currentTsRef.current   = '';
    setCurrentLine('');
  }, []);

  useEffect(() => {
    const unsub = addIQListener((msg) => {
      if (msg.type === 'status') {
        setConnected(msg.connected);
        setStatusFreq(msg.centerFreq);
      } else if (msg.type === 'cw_char') {
        setStatusFreq(msg.freq);
        if (!currentTsRef.current) currentTsRef.current = msg.ts;
        currentLineRef.current += msg.char;
        setCurrentLine(currentLineRef.current);
      } else if (msg.type === 'cw_word_space') {
        currentLineRef.current += ' ';
        setCurrentLine(currentLineRef.current);
        // Flush to a completed line after a word boundary
        if (currentLineRef.current.trim().length > 60) flushLine();
      }
    });
    return unsub;
  }, [flushLine]);

  // Auto-scroll to bottom whenever output changes
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  });

  const clearAll = () => {
    setLines([]);
    setCurrentLine('');
    currentLineRef.current = '';
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
              statusFreq != null && <span>{(statusFreq / 1e6).toFixed(4)} MHz</span>
            ) : (
              <span className="text-red-400/80">Connecting to IQ stream…</span>
            )}
          </p>
        </div>
        <button
          type="button"
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
