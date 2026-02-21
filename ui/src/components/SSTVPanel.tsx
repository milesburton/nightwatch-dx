import { useCallback, useRef, useState } from 'react';
import type { WorkerDecodeRequest, WorkerOutboundMessage } from '../types.js';
import { DropZone } from './DropZone.js';

interface DecodedFrame {
  id: number;
  imageUrl: string;
  mode: string;
  ts: string;
}

async function decodeAudioBuffer(buffer: ArrayBuffer): Promise<{ samples: Float32Array; sampleRate: number }> {
  const ctx = new AudioContext();
  try {
    const audioBuffer = await ctx.decodeAudioData(buffer);
    return { samples: audioBuffer.getChannelData(0), sampleRate: audioBuffer.sampleRate };
  } finally {
    await ctx.close();
  }
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

export function SSTVPanel() {
  const [frames, setFrames] = useState<DecodedFrame[]>([]);
  const [decoding, setDecoding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const frameId = useRef(0);

  const handleFile = useCallback(async (file: File) => {
    setDecoding(true);
    setError(null);
    try {
      const buffer = await file.arrayBuffer();
      const { samples, sampleRate } = await decodeAudioBuffer(buffer);
      const result = await decodeWithWorker(samples, sampleRate);

      if (result.type === 'error') {
        setError(result.message);
        return;
      }

      // Convert pixel buffer to PNG data URL via canvas
      const canvas = document.createElement('canvas');
      canvas.width  = result.width;
      canvas.height = result.height;
      const ctx = canvas.getContext('2d');
      if (!ctx) throw new Error('No canvas context');
      const imageData = ctx.createImageData(result.width, result.height);
      imageData.data.set(result.pixels);
      ctx.putImageData(imageData, 0, 0);
      const imageUrl = canvas.toDataURL('image/png');

      const frame: DecodedFrame = {
        id:       frameId.current++,
        imageUrl,
        mode:     result.diagnostics.mode,
        ts:       new Date().toISOString(),
      };
      setFrames((prev) => {
        const next = [frame, ...prev];
        return next.length > 20 ? next.slice(0, 20) : next;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Decode failed');
    } finally {
      setDecoding(false);
    }
  }, []);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2 pb-4 border-b border-white/10">
        <h2 className="text-white text-xl font-semibold tracking-wide flex-1">SSTV — Decode</h2>
        {decoding && (
          <span className="text-amber-400 text-xs animate-pulse">Decoding…</span>
        )}
      </div>

      <DropZone
        onFile={handleFile}
        accept="audio/*"
        processing={decoding}
        inputId="sstv-audio-input"
        hint="WAV or MP3 containing an SSTV transmission"
        icon={
          <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
            <path d="M9 13l3-3 3 3M12 10v8M4 16.5A4 4 0 015.5 9H6a6 6 0 0111.94-.6A4.5 4.5 0 1119.5 16.5H4z" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        }
      />

      {error && (
        <p className="text-red-400 text-xs font-mono px-3 py-2 bg-red-900/20 border border-red-800/40 rounded">
          {error}
        </p>
      )}

      {frames.length === 0 && !decoding && !error ? (
        <p className="text-white/20 text-xs italic text-center py-4">
          Decoded frames will appear here.
        </p>
      ) : (
        <div className="grid grid-cols-2 gap-3">
          {frames.map((frame) => (
            <div key={frame.id} className="border border-white/10 rounded-lg overflow-hidden">
              <img src={frame.imageUrl} alt={`SSTV ${frame.mode}`} className="w-full h-auto" />
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
