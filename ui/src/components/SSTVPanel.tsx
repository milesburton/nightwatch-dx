import { useEffect, useRef, useState } from 'react';
import type { SSTVSocketMessage } from '../types.js';

const WS_PROTO = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const WS_URL = `${WS_PROTO}//${window.location.host}/ws/sstv`;

interface LiveFrame {
  id: number;
  imageData: string;
  mode: string;
  ts: string;
}

function StatusDot({ connected }: { connected: boolean }) {
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full mr-2 ${connected ? 'bg-emerald-400' : 'bg-red-500'}`}
      title={connected ? 'Connected' : 'Disconnected'}
    />
  );
}

export function SSTVPanel() {
  const [liveFrames, setLiveFrames] = useState<LiveFrame[]>([]);
  const [liveConnected, setLiveConnected] = useState(false);
  const frameId = useRef(0);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let reconnectTimer: ReturnType<typeof setTimeout>;

    const connect = () => {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;
      ws.onopen = () => setLiveConnected(true);
      ws.onclose = () => {
        setLiveConnected(false);
        reconnectTimer = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (e: MessageEvent<string>) => {
        let msg: SSTVSocketMessage;
        try { msg = JSON.parse(e.data) as SSTVSocketMessage; } catch { return; }
        if (msg.type === 'frame') {
          setLiveFrames((prev) => {
            const next = [...prev, { id: frameId.current++, imageData: msg.imageData, mode: msg.mode, ts: msg.ts }];
            return next.length > 20 ? next.slice(next.length - 20) : next;
          });
        } else if (msg.type === 'status') {
          setLiveConnected(msg.connected);
        }
      };
    };

    connect();
    return () => {
      clearTimeout(reconnectTimer);
      wsRef.current?.close();
    };
  }, []);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2 pb-4 border-b border-white/10">
        <h2 className="text-white text-xl font-semibold tracking-wide flex-1">SSTV — Live Feed</h2>
        <StatusDot connected={liveConnected} />
        <span className="text-white/40 text-xs">
          {liveConnected ? 'SDR connected' : 'Reconnecting…'}
        </span>
      </div>

      {liveFrames.length === 0 ? (
        <p className="text-white/20 text-xs italic text-center py-6">
          No SSTV frames received yet. Waiting for signal…
        </p>
      ) : (
        <div className="grid grid-cols-2 gap-3">
          {[...liveFrames].reverse().map((frame) => (
            <div key={frame.id} className="border border-white/10 rounded-lg overflow-hidden">
              <img src={frame.imageData} alt={`SSTV ${frame.mode}`} className="w-full h-auto" />
              <div className="px-2 py-1 bg-black/30 text-xs text-white/40 flex justify-between">
                <span className="font-mono">{frame.mode}</span>
                <span>{new Date(frame.ts).toLocaleTimeString()}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
