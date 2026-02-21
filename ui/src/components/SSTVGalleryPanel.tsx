/**
 * SSTVGalleryPanel — automatically detects and decodes SSTV transmissions.
 *
 * Receives pre-decoded PNG frames from the sstv-decoder Python backend via
 * WebSocket at /ws/sstv. Persists frames in IndexedDB (max 100).
 *
 * Gallery: newest first, 3-column grid.
 */

import { useEffect, useState } from 'react';
import type { SSTVFrame } from '../utils/db.js';
import { listSSTV, saveSSTV } from '../utils/db.js';

type SSTVBackendMessage =
  | { type: 'frame'; imageDataUrl: string; mode: string; ts: string }
  | { type: 'status'; connected: boolean }
  | { type: 'error'; message: string };

export function SSTVGalleryPanel() {
  const [frames, setFrames]       = useState<SSTVFrame[]>([]);
  const [connected, setConnected] = useState(false);

  // Load persisted frames on mount (newest first)
  useEffect(() => {
    listSSTV().then((rows) => {
      setFrames(rows.sort((a, b) => b.ts.localeCompare(a.ts)));
    });
  }, []);

  // WebSocket to /ws/sstv
  useEffect(() => {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let ws: WebSocket | null = null;
    let closed = false;
    let frameCounter = Date.now();

    function connect() {
      if (closed) return;
      ws = new WebSocket(`${proto}//${location.host}/ws/sstv`);
      ws.onopen = () => {};
      ws.onclose = () => {
        setConnected(false);
        if (!closed) setTimeout(connect, 3000);
      };
      ws.onerror = () => ws?.close();
      ws.onmessage = (e: MessageEvent<string>) => {
        let msg: SSTVBackendMessage;
        try { msg = JSON.parse(e.data) as SSTVBackendMessage; }
        catch { return; }

        if (msg.type === 'status') {
          setConnected(msg.connected);
        } else if (msg.type === 'frame') {
          const frame: SSTVFrame = {
            id:       frameCounter++,
            ts:       msg.ts,
            imageUrl: msg.imageDataUrl,
            mode:     msg.mode,
          };
          void saveSSTV(frame);
          setFrames((prev) => [frame, ...prev]);
        }
      };
    }

    connect();

    return () => {
      closed = true;
      ws?.close();
    };
  }, []);

  return (
    <div className="glass rounded-2xl p-6 flex flex-col gap-4">
      {/* Header */}
      <div className="flex items-center gap-2 pb-4 border-b border-white/10">
        <h2 className="text-white text-xl font-semibold tracking-wide flex-1">
          SSTV — Auto Detect
        </h2>
        <span
          className={`inline-block w-2 h-2 rounded-full ${connected ? 'bg-emerald-400' : 'bg-red-500'}`}
          title={connected ? 'Connected' : 'Connecting…'}
        />
        <span className="text-white/40 text-xs font-mono">14.230 MHz</span>
      </div>

      {/* Gallery */}
      {frames.length === 0 ? (
        <p className="text-white/20 text-xs italic text-center py-8">
          {connected
            ? 'Listening for SSTV on 14.230 MHz…'
            : 'Connecting to SSTV decoder…'}
        </p>
      ) : (
        <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">
          {frames.map((frame) => (
            <div
              key={frame.id}
              className="border border-white/10 rounded-lg overflow-hidden bg-black/30"
            >
              <img
                src={frame.imageUrl}
                alt={`SSTV ${frame.mode}`}
                className="w-full h-auto block"
              />
              <div className="px-2 py-1 flex justify-between text-[10px] text-white/40 font-mono">
                <span>{frame.mode}</span>
                <span>{new Date(frame.ts).toLocaleTimeString()}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
