/**
 * SSTVGalleryPanel — automatically detects and decodes SSTV transmissions.
 *
 * Frame history is loaded from the REST API (/api/frames?mode=sstv) on panel
 * open. Live frame notifications arrive via WebSocket at /ws/sstv; frames are
 * served as static PNG files from /frames/sstv/...
 *
 * Gallery: newest first, 3-column grid.
 */

import { useEffect, useState } from 'react';
import type { ApiFrame } from '../utils/api.js';
import { fetchFrames } from '../utils/api.js';
import { useAccordion } from '../utils/useAccordion.js';

type SSTVBackendMessage =
  | { type: 'frame'; id: number; mode: string; ts: string; url: string }
  | { type: 'status'; connected: boolean }
  | { type: 'error'; message: string };

export function SSTVGalleryPanel() {
  const [open, toggleOpen] = useAccordion('sstv-open');
  const [frames, setFrames] = useState<ApiFrame[]>([]);
  const [connected, setConnected] = useState(false);

  // Load frame history from REST on panel open
  useEffect(() => {
    if (!open) return;
    fetchFrames('sstv').then((rows) => {
      setFrames(rows);
    });
  }, [open]);

  // WebSocket to /ws/sstv — only while open
  useEffect(() => {
    if (!open) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let ws: WebSocket | null = null;
    let closed = false;

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
        try {
          msg = JSON.parse(e.data) as SSTVBackendMessage;
        } catch {
          return;
        }

        if (msg.type === 'status') {
          setConnected(msg.connected);
        } else if (msg.type === 'frame') {
          const frame: ApiFrame = {
            id: msg.id,
            mode: msg.mode,
            ts: msg.ts,
            freq_hz: 14_230_000,
            url: msg.url,
          };
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
          SSTV — Auto Detect
        </h2>
        {open && (
          <>
            <span
              className={`inline-block w-2 h-2 rounded-full ${connected ? 'bg-emerald-400' : 'bg-red-500'}`}
              title={connected ? 'Connected' : 'Connecting…'}
            />
            <span className="text-white/40 text-xs font-mono">14.230 MHz</span>
          </>
        )}
        <button
          type="button"
          onClick={toggleOpen}
          className="text-white/40 hover:text-white/80 transition-colors text-xs font-mono px-2 py-0.5 rounded border border-white/10 hover:border-white/30"
          aria-label={open ? 'Collapse SSTV gallery' : 'Expand SSTV gallery'}
        >
          {open ? '▲ hide' : '▼ show'}
        </button>
      </div>

      {/* Gallery */}
      {open && (
        <div className="p-6">
          {frames.length === 0 ? (
            <p className="text-white/20 text-xs italic text-center py-8">
              {connected ? 'Listening for SSTV on 14.230 MHz…' : 'Connecting to SSTV decoder…'}
            </p>
          ) : (
            <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">
              {frames.map((frame) => (
                <div
                  key={frame.id}
                  className="border border-white/10 rounded-lg overflow-hidden bg-black/30"
                >
                  <img
                    src={frame.url}
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
      )}
    </div>
  );
}
