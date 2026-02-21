/**
 * SSTVGalleryPanel — automatically detects and decodes SSTV transmissions
 * from the live IQ stream.
 *
 * The IQ worker runs an FM discriminator + SSTVVISDetector on 14.230 MHz.
 * When it accumulates a full frame it posts an `sstv_audio` message with the
 * raw demodulated Float32Array. This panel receives that, dispatches it to
 * the existing decoderWorker (same one SSTVPanel used), renders the result
 * to a canvas, and persists it in IndexedDB.
 *
 * Gallery: newest first, 3-column grid, max 100 frames (purged in db.ts).
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { WorkerDecodeRequest, WorkerOutboundMessage } from '../types.js';
import { listSSTV, saveSSTV } from '../utils/db.js';
import type { SSTVFrame } from '../utils/db.js';
import { addIQListener } from '../workers/iqWorkerSingleton.js';

function pixelsToDataUrl(pixels: Uint8ClampedArray, width: number, height: number): string {
  const canvas = document.createElement('canvas');
  canvas.width  = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  if (!ctx) return '';
  const imageData = ctx.createImageData(width, height);
  imageData.data.set(pixels);
  ctx.putImageData(imageData, 0, 0);
  return canvas.toDataURL('image/png');
}

function decodeWithWorker(samples: Float32Array, sampleRate: number): Promise<WorkerOutboundMessage> {
  return new Promise((resolve) => {
    const worker = new Worker(new URL('../workers/decoderWorker.ts', import.meta.url), { type: 'module' });
    worker.onmessage = (e: MessageEvent<WorkerOutboundMessage>) => {
      worker.terminate();
      resolve(e.data);
    };
    worker.onerror = (e: ErrorEvent) => {
      worker.terminate();
      resolve({ type: 'error', message: e.message ?? 'Worker error' });
    };
    const req: WorkerDecodeRequest = { type: 'decode', samples, sampleRate };
    worker.postMessage(req, [samples.buffer]);
  });
}

export function SSTVGalleryPanel() {
  const [frames, setFrames]       = useState<SSTVFrame[]>([]);
  const [connected, setConnected] = useState(false);
  const [decoding, setDecoding]   = useState(false);
  const frameIdRef = useRef(0);

  // Load persisted frames on mount (newest first)
  useEffect(() => {
    listSSTV().then((rows) => {
      setFrames(rows.sort((a, b) => b.ts.localeCompare(a.ts)));
    });
  }, []);

  // Handle incoming decoded SSTV audio frames
  const handleSSTVAudio = useCallback(async (samples: Float32Array, sampleRate: number, ts: string) => {
    setDecoding(true);
    try {
      const result = await decodeWithWorker(samples, sampleRate);
      if (result.type === 'error') return;
      const imageUrl = pixelsToDataUrl(result.pixels, result.width, result.height);
      if (!imageUrl) return;
      const frame: SSTVFrame = {
        id: frameIdRef.current++,
        ts,
        imageUrl,
        mode: result.diagnostics.mode,
      };
      await saveSSTV(frame);
      setFrames((prev) => [frame, ...prev]);
    } finally {
      setDecoding(false);
    }
  }, []);

  // Subscribe to IQ worker messages
  useEffect(() => {
    const unsub = addIQListener((msg) => {
      if (msg.type === 'status') {
        setConnected(msg.connected);
      } else if (msg.type === 'sstv_audio') {
        void handleSSTVAudio(msg.samples, msg.sampleRate, msg.ts);
      }
    });
    return unsub;
  }, [handleSSTVAudio]);

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
        {decoding && (
          <span className="text-amber-400 text-xs animate-pulse ml-2">Decoding…</span>
        )}
      </div>

      {/* Gallery */}
      {frames.length === 0 ? (
        <p className="text-white/20 text-xs italic text-center py-8">
          {connected
            ? 'Listening for SSTV on 14.230 MHz…'
            : 'Connecting to IQ stream…'}
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
