import { useCallback, useEffect, useRef, useState } from 'react';
import type { DecodeState, SSTVSocketMessage, WorkerOutboundMessage } from '../types.js';
import { DiagnosticsPanel } from './DiagnosticsPanel.js';
import { DropZone } from './DropZone.js';
import { QualityBadge } from './QualityBadge.js';

const WS_PROTO = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const WS_URL = `${WS_PROTO}//${window.location.host}/ws/sstv`;

interface LiveFrame {
  id: number;
  imageData: string;
  mode: string;
  ts: string;
}

const AudioIcon = () => (
  <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.25">
    <title>Audio file</title>
    <path d="M9 18V5l12-2v13" />
    <circle cx="6" cy="18" r="3" />
    <circle cx="18" cy="16" r="3" />
  </svg>
);

function StatusDot({ connected }: { connected: boolean }) {
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full mr-2 ${connected ? 'bg-emerald-400' : 'bg-red-500'}`}
      title={connected ? 'Connected' : 'Disconnected'}
    />
  );
}

async function decodeAudioBuffer(buffer: ArrayBuffer): Promise<{ samples: Float32Array; sampleRate: number }> {
  const AudioCtx = window.AudioContext ?? (window as unknown as Record<string, typeof AudioContext>).webkitAudioContext;
  if (!AudioCtx) throw new Error('Web Audio API not available');
  const ctx = new AudioCtx();
  try {
    const ab = await ctx.decodeAudioData(buffer);
    return { samples: ab.getChannelData(0), sampleRate: ab.sampleRate };
  } finally {
    await ctx.close();
  }
}

function decodeWithWorker(samples: Float32Array, sampleRate: number): Promise<WorkerOutboundMessage> {
  return new Promise((resolve) => {
    const worker = new Worker(new URL('../workers/decoderWorker.ts', import.meta.url), { type: 'module' });
    worker.onmessage = (e: MessageEvent<WorkerOutboundMessage>) => { worker.terminate(); resolve(e.data); };
    worker.onerror = (e: ErrorEvent) => { worker.terminate(); resolve({ type: 'error', message: e.message ?? 'Worker error' }); };
    worker.postMessage({ type: 'decode', samples, sampleRate }, [samples.buffer]);
  });
}

function pixelsToDataUrl(pixels: Uint8ClampedArray, width: number, height: number): string {
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('No canvas context');
  const img = ctx.createImageData(width, height);
  img.data.set(pixels);
  ctx.putImageData(img, 0, 0);
  return canvas.toDataURL('image/png');
}

export function SSTVPanel() {
  const [liveFrames, setLiveFrames] = useState<LiveFrame[]>([]);
  const [liveConnected, setLiveConnected] = useState(false);
  const frameId = useRef(0);
  const wsRef = useRef<WebSocket | null>(null);

  // File decode state
  const [fileResult, setFileResult] = useState<DecodeState | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [processing, setProcessing] = useState(false);

  // WebSocket for live SSTV from SDR
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

  const runFileDecode = useCallback(async (file: File) => {
    setProcessing(true);
    setFileResult(null);
    setFileError(null);
    try {
      const buffer = await file.arrayBuffer();
      const { samples, sampleRate } = await decodeAudioBuffer(buffer);
      const msg = await decodeWithWorker(samples, sampleRate);
      if (msg.type === 'error') {
        setFileError(msg.message);
      } else {
        const url = pixelsToDataUrl(msg.pixels, msg.width, msg.height);
        setFileResult({ url, filename: `sstv_${Date.now()}.png`, diagnostics: msg.diagnostics });
      }
    } catch (err) {
      setFileError(err instanceof Error ? err.message : 'Decode failed');
    } finally {
      setProcessing(false);
    }
  }, []);

  const handleFile = (file: File) => {
    if (!file.type.startsWith('audio/')) {
      setFileError('Please select an audio file (WAV, MP3, etc.)');
      return;
    }
    runFileDecode(file);
  };

  const downloadFile = () => {
    if (!fileResult) return;
    const a = document.createElement('a');
    a.href = fileResult.url;
    a.download = fileResult.filename;
    a.click();
  };

  const verdict = fileResult?.diagnostics?.quality?.verdict;

  return (
    <div className="flex flex-col gap-8">
      {/* Live SDR feed */}
      <div>
        <div className="flex items-center gap-2 mb-4 pb-4 border-b border-white/10">
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

      {/* File decode */}
      <div>
        <div className="mb-4 pb-4 border-b border-white/10">
          <h2 className="text-white text-xl font-semibold tracking-wide">SSTV — Decode File</h2>
          <p className="text-white/40 text-xs mt-0.5">Automatic mode detection via VIS code</p>
        </div>

        <DropZone
          accept="audio/*"
          onFile={handleFile}
          processing={processing}
          icon={<AudioIcon />}
          hint="WAV, MP3, OGG supported"
          inputId="sstv-decode-input"
        />

        {fileError && (
          <div className="border border-red-500/30 bg-red-500/10 rounded-lg p-3 text-red-400 text-center text-sm">
            {fileError}
          </div>
        )}

        {fileResult && (
          <>
            <h3 className="mb-3 text-sm font-semibold text-center uppercase tracking-wider">
              <span className={verdict === 'bad' ? 'text-red-400' : 'text-emerald-400'}>
                {verdict === 'bad' ? 'Decoded (quality issues)' : 'Decoded successfully'}
              </span>
              <QualityBadge verdict={verdict} />
            </h3>
            <img
              src={fileResult.url}
              alt="Decoded SSTV"
              className="max-w-full h-auto rounded-lg block mx-auto mb-4 opacity-95"
            />
            <div className="flex gap-3 justify-center mb-4">
              <button
                onClick={downloadFile}
                className="px-5 py-2 text-sm font-semibold bg-emerald-600 text-white rounded-lg hover:bg-emerald-500 hover:-translate-y-0.5 transition-all"
              >
                Download PNG
              </button>
              <button
                onClick={() => { setFileResult(null); setFileError(null); }}
                className="px-5 py-2 text-sm font-semibold bg-white/10 text-white/70 rounded-lg hover:bg-white/15 transition-all"
              >
                Clear
              </button>
            </div>
            {fileResult.diagnostics && <DiagnosticsPanel diagnostics={fileResult.diagnostics} />}
          </>
        )}
      </div>
    </div>
  );
}
