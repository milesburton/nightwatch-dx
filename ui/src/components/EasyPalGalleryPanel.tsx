/**
 * EasyPalGalleryPanel — automatically detects and decodes EasyPal transmissions.
 *
 * Receives pre-decoded PNG frames from the easypal-decoder Python backend via
 * WebSocket at /ws/easypal. Persists frames in IndexedDB (max 100).
 *
 * Gallery: newest first, 3-column grid.
 */

import { useEffect, useState } from 'react';
import type { EasyPalFrame } from '../utils/db.js';
import { listEasyPal, saveEasyPal } from '../utils/db.js';
import { useAccordion } from '../utils/useAccordion.js';

type EasyPalBackendMessage =
  | { type: 'frame'; imageDataUrl: string; ts: string }
  | { type: 'status'; connected: boolean }
  | { type: 'error'; message: string };

export function EasyPalGalleryPanel() {
  const [open, toggleOpen] = useAccordion('easypal-open');
  const [frames, setFrames] = useState<EasyPalFrame[]>([]);
  const [connected, setConnected] = useState(false);

  // Load persisted frames on mount (newest first)
  useEffect(() => {
    listEasyPal().then((rows) => {
      setFrames(rows.sort((a, b) => b.ts.localeCompare(a.ts)));
    });
  }, []);

  // WebSocket to /ws/easypal — only while open
  useEffect(() => {
    if (!open) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let ws: WebSocket | null = null;
    let closed = false;
    let frameCounter = Date.now();

    function connect() {
      if (closed) return;
      ws = new WebSocket(`${proto}//${location.host}/ws/easypal`);
      ws.onopen = () => {};
      ws.onclose = () => {
        setConnected(false);
        if (!closed) setTimeout(connect, 3000);
      };
      ws.onerror = () => ws?.close();
      ws.onmessage = (e: MessageEvent<string>) => {
        let msg: EasyPalBackendMessage;
        try {
          msg = JSON.parse(e.data) as EasyPalBackendMessage;
        } catch {
          return;
        }

        if (msg.type === 'status') {
          setConnected(msg.connected);
        } else if (msg.type === 'frame') {
          const frame: EasyPalFrame = {
            id: frameCounter++,
            ts: msg.ts,
            imageUrl: msg.imageDataUrl,
          };
          void saveEasyPal(frame);
          setFrames((prev) => [frame, ...prev]);
        }
      };
    }

    connect();

    return () => {
      closed = true;
      ws?.close();
      setConnected(false);
    };
  }, [open]);

  return (
    <div className="glass rounded-2xl overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2 px-6 py-4 border-b border-white/10">
        <h2 className="text-white text-xl font-semibold tracking-wide flex-1">
          EasyPal — Auto Detect
        </h2>
        {open && (
          <>
            <span
              className={`inline-block w-2 h-2 rounded-full ${connected ? 'bg-emerald-400' : 'bg-red-500'}`}
              title={connected ? 'Connected' : 'Connecting…'}
            />
            <span className="text-white/40 text-xs font-mono">14.233 MHz</span>
          </>
        )}
        <button
          type="button"
          onClick={toggleOpen}
          className="text-white/40 hover:text-white/80 transition-colors text-xs font-mono px-2 py-0.5 rounded border border-white/10 hover:border-white/30"
          aria-label={open ? 'Collapse EasyPal gallery' : 'Expand EasyPal gallery'}
        >
          {open ? '▲ hide' : '▼ show'}
        </button>
      </div>

      {/* Gallery */}
      {open && (
        <div className="p-6">
          {frames.length === 0 ? (
            <p className="text-white/20 text-xs italic text-center py-8">
              {connected ? 'Listening for EasyPal on 14.233 MHz…' : 'Connecting to EasyPal decoder…'}
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
                    alt="EasyPal frame"
                    className="w-full h-auto block"
                  />
                  <div className="px-2 py-1 flex justify-between text-[10px] text-white/40 font-mono">
                    <span>EasyPal</span>
                    <span>{new Date(frame.ts).toLocaleTimeString()}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
