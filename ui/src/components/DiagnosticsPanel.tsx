import { useState } from 'react';
import type { DecodeDiagnostics } from '../types.js';
import { QualityBadge } from './QualityBadge.js';

interface Props {
  diagnostics: DecodeDiagnostics;
}

export function DiagnosticsPanel({ diagnostics }: Props) {
  const [open, setOpen] = useState(true);
  const { mode, visCode, sampleRate, fileDuration, freqOffset, autoCalibrate, visEndPos, decodeTimeMs, quality } =
    diagnostics;

  return (
    <div className="mt-4 border border-white/10 rounded-lg overflow-hidden text-xs">
      <button
        className="w-full bg-white/[0.04] hover:bg-white/[0.07] border-none px-3 py-2.5 text-left text-xs font-semibold text-white/40 flex justify-between uppercase tracking-wider transition-colors"
        onClick={() => setOpen((o) => !o)}
      >
        <span>Diagnostics</span>
        <span>{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <div className="p-3">
          <div className="diag-grid">
            <span className="text-white/35 font-medium">Mode</span>
            <span className="font-mono text-white/65 break-all">{mode ?? '—'}</span>
            <span className="text-white/35 font-medium">VIS code</span>
            <span className="font-mono text-white/65">
              {visCode != null ? `0x${visCode.toString(16).toUpperCase().padStart(2, '0')} (${visCode})` : '—'}
            </span>
            <span className="text-white/35 font-medium">Sample rate</span>
            <span className="font-mono text-white/65">{sampleRate ? `${sampleRate} Hz` : '—'}</span>
            <span className="text-white/35 font-medium">File duration</span>
            <span className="font-mono text-white/65">{fileDuration ?? '—'}</span>
            <span className="text-white/35 font-medium">Freq offset</span>
            <span className={`font-mono ${Math.abs(freqOffset) > 50 ? 'text-amber-400 font-semibold' : 'text-white/65'}`}>
              {freqOffset != null ? `${freqOffset > 0 ? '+' : ''}${freqOffset} Hz` : '—'}
            </span>
            <span className="text-white/35 font-medium">Auto-calibrate</span>
            <span className="font-mono text-white/65">{autoCalibrate ? 'on' : 'off'}</span>
            <span className="text-white/35 font-medium">VIS end pos</span>
            <span className="font-mono text-white/65">{visEndPos != null ? `${visEndPos} samples` : '—'}</span>
            <span className="text-white/35 font-medium">Decode time</span>
            <span className="font-mono text-white/65">{decodeTimeMs != null ? `${decodeTimeMs} ms` : '—'}</span>
          </div>
          {quality && (
            <>
              <div className="font-semibold text-white/35 mt-3 mb-1 text-xs uppercase tracking-wider">
                Image Quality
              </div>
              <div className="diag-grid">
                <span className="text-white/35 font-medium">Avg R/G/B</span>
                <span className="font-mono flex gap-2 items-center">
                  <span style={{ color: '#f87171' }}>R:{quality.rAvg}</span>
                  <span style={{ color: '#4ade80' }}>G:{quality.gAvg}</span>
                  <span style={{ color: '#60a5fa' }}>B:{quality.bAvg}</span>
                </span>
                <span className="text-white/35 font-medium">Brightness</span>
                <span className="font-mono text-white/65">{quality.brightness}</span>
                <span className="text-white/35 font-medium">Verdict</span>
                <span className="font-mono">
                  <QualityBadge verdict={quality.verdict} />
                </span>
              </div>
              {quality.warnings.length > 0 && (
                <ul className="mt-2 p-2 bg-amber-500/10 border border-amber-500/20 rounded list-none">
                  {quality.warnings.map((w, i) => (
                    <li key={i} className="text-xs text-amber-300/80 leading-relaxed">
                      {w}
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
