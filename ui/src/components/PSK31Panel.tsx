/**
 * PSK31Panel — persistent session log for PSK31 transmissions.
 *
 * Receives decoded characters from the psk31-decoder Python backend via
 * WebSocket at /ws/psk31. Session history is loaded from the REST API
 * (/api/sessions?mode=psk31) on panel open. The server flushes sessions
 * after 30 s of inactivity and broadcasts a `session` notification.
 *
 * Layout: Fallout-style master-detail.
 *   Left (35%): scrollable session list (newest first), amber dot for live.
 *   Right (65%): selected session text or live decode stream.
 *
 * Signal validity: live SNR from the carrier scan is shown in the detail pane.
 * SNR ≥ 5× is considered good; sessions shorter than 3 chars are flagged.
 *
 * Scans ±2 kHz around 14.070 MHz to lock onto the strongest PSK31 carrier.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { CWSocketMessage } from '../types.js';
import type { ApiSession } from '../utils/api.js';
import { fetchSessions } from '../utils/api.js';
import { useAccordion } from '../utils/useAccordion.js';

// ── Signal quality helpers ────────────────────────────────────────────────────

type QualityLevel = 'good' | 'fair' | 'poor';

/** Classify a session by text length — very short sessions are likely noise. */
function sessionQuality(text: string): QualityLevel {
  const trimmed = text.trim();
  if (trimmed.length < 3) return 'poor';
  if (trimmed.length < 8) return 'fair';
  return 'good';
}

/** Classify live SNR (ratio, not dB). */
function snrQuality(snr: number): QualityLevel {
  if (snr <= 0) return 'poor';
  if (snr >= 5) return 'good';
  if (snr >= 2) return 'fair';
  return 'poor';
}

const QUALITY_META: Record<QualityLevel, { dot: string; label: string; desc: string }> = {
  good: { dot: 'bg-emerald-400', label: 'Good', desc: 'Signal quality: good' },
  fair: { dot: 'bg-amber-400',   label: 'Fair', desc: 'Signal quality: fair' },
  poor: { dot: 'bg-red-500',     label: 'Poor', desc: 'Signal quality: poor — weak or noisy carrier' },
};

function QualityBadge({ quality }: { quality: QualityLevel }) {
  const { dot, label, desc } = QUALITY_META[quality];
  if (quality === 'good') return null;
  return (
    <span
      className={`inline-flex items-center gap-1 text-[9px] font-mono px-1.5 py-0.5 rounded border border-white/10 ${quality === 'poor' ? 'text-red-400/80' : 'text-amber-400/80'}`}
      title={desc}
      role="img"
      aria-label={desc}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${dot}`} aria-hidden />
      {label}
    </span>
  );
}

// ── Token rendering ───────────────────────────────────────────────────────────

interface PSK31Token {
  id: number;
  char: string;
}

let _tokenId = 0;

function makeToken(ch: string): PSK31Token {
  return { id: _tokenId++, char: ch };
}

function tokenise(text: string): PSK31Token[] {
  return [...text].map(makeToken);
}

function PSK31Char({ token }: { token: PSK31Token }) {
  if (token.char === ' ') {
    return <span className="inline-block w-4" aria-hidden />;
  }
  return (
    <span className="inline-block cursor-default select-none">
      {token.char}
    </span>
  );
}

function PSK31Text({ tokens, cursor }: { tokens: PSK31Token[]; cursor?: boolean }) {
  return (
    <p className="text-emerald-300 text-sm leading-relaxed whitespace-pre-wrap font-mono">
      {tokens.map((tok) => (
        <PSK31Char key={tok.id} token={tok} />
      ))}
      {cursor && <span className="cw-cursor" aria-hidden />}
    </p>
  );
}

// ── Shared UI helpers ─────────────────────────────────────────────────────────

function copyToClipboard(text: string): Promise<void> {
  if (typeof navigator.clipboard?.writeText === 'function') {
    return navigator.clipboard.writeText(text);
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
  return Promise.resolve();
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      aria-label={copied ? 'Copied to clipboard' : 'Copy decoded text to clipboard'}
      onClick={() => {
        copyToClipboard(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
      className="text-white/30 hover:text-white/70 transition-colors text-[10px] font-mono px-1.5 py-0.5 rounded border border-white/10 hover:border-white/30 shrink-0"
    >
      {copied ? '✓ copied' : 'copy'}
    </button>
  );
}

function StatusDot({ live }: { live: boolean }) {
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full shrink-0 ${live ? 'bg-amber-400 animate-pulse' : 'bg-white/20'}`}
      aria-hidden
    />
  );
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function formatFreq(hz: number): string {
  return `${(hz / 1e6).toFixed(3)} MHz`;
}

// ── Session hook ──────────────────────────────────────────────────────────────

interface SessionState {
  sessions: ApiSession[];
  selectedId: number | 'live';
  setSelectedId: (id: number | 'live') => void;
  connected: boolean;
  liveTokens: PSK31Token[];
  liveText: string;
  liveFreq: number;
  liveStartTs: string;
  liveSnr: number;
}

function usePSK31State(open: boolean): SessionState {
  const [sessions, setSessions] = useState<ApiSession[]>([]);
  const [selectedId, setSelectedId] = useState<number | 'live'>('live');
  const [connected, setConnected] = useState(false);
  const [liveTokens, setLiveTokens] = useState<PSK31Token[]>([]);
  const [liveText, setLiveText] = useState('');
  const [liveFreq, setLiveFreq] = useState<number>(14_070_000);
  const [liveStartTs, setLiveStartTs] = useState<string>('');
  const [liveSnr, setLiveSnr] = useState(0);

  const liveTextRef  = useRef('');
  const liveFreqRef  = useRef(14_070_000);
  const liveStartRef = useRef('');

  useEffect(() => {
    if (!open) return;
    fetchSessions('psk31').then((rows) => setSessions(rows));
  }, [open]);

  const resetLive = useCallback(() => {
    liveTextRef.current = '';
    liveStartRef.current = '';
    setLiveText('');
    setLiveTokens([]);
    setLiveStartTs('');
    setLiveSnr(0);
  }, []);

  useEffect(() => {
    if (!open) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let ws: WebSocket | null = null;
    let closed = false;

    function connect() {
      if (closed) return;
      ws = new WebSocket(`${proto}//${location.host}/ws/psk31`);
      ws.onopen = () => {};
      ws.onclose = () => {
        setConnected(false);
        if (!closed) setTimeout(connect, 3000);
      };
      ws.onerror = () => ws?.close();
      ws.onmessage = (e: MessageEvent<string>) => {
        let msg: CWSocketMessage;
        try {
          msg = JSON.parse(e.data) as CWSocketMessage;
        } catch {
          return;
        }

        if (msg.type === 'status') {
          setConnected(msg.connected);
          if ('freq' in msg) {
            liveFreqRef.current = msg.freq;
            setLiveFreq(msg.freq);
          }
        } else if (msg.type === 'char') {
          liveFreqRef.current = msg.freq;
          setLiveFreq(msg.freq);
          if (msg.snr !== undefined) setLiveSnr(msg.snr);
          if (!liveStartRef.current) {
            liveStartRef.current = msg.ts;
            setLiveStartTs(msg.ts);
          }
          liveTextRef.current += msg.char;
          setLiveText(liveTextRef.current);
          setLiveTokens((prev) => [...prev, makeToken(msg.char)]);
        } else if (msg.type === 'word_space') {
          liveTextRef.current += ' ';
          setLiveText(liveTextRef.current);
          setLiveTokens((prev) => [...prev, makeToken(' ')]);
        } else if (msg.type === 'session') {
          const saved: ApiSession = {
            id: msg.id,
            mode: msg.mode,
            start_ts: msg.start_ts,
            end_ts: msg.end_ts,
            freq_hz: msg.freq_hz,
            text: msg.text,
          };
          setSessions((prev) => [saved, ...prev]);
          resetLive();
        }
      };
    }

    connect();

    return () => {
      closed = true;
      ws?.close();
    };
  }, [open, resetLive]);

  return { sessions, selectedId, setSelectedId, connected,
           liveTokens, liveText, liveFreq, liveStartTs, liveSnr };
}

// ── Session panel (master-detail list + detail pane) ─────────────────────────

function SessionPanel({ state }: { state: SessionState }) {
  const { sessions, selectedId, setSelectedId, connected, liveTokens,
          liveText, liveFreq, liveStartTs, liveSnr } = state;

  const selectedSession =
    selectedId !== 'live' ? (sessions.find((s) => s.id === selectedId) ?? null) : null;

  const isLive = liveText.length > 0;
  const liveQuality = liveSnr > 0 ? snrQuality(liveSnr) : undefined;

  return (
    <div className="flex" style={{ minHeight: '400px', maxHeight: '60vh' }}>
      {/* Left: session list (35%) */}
      <nav
        className="w-[35%] border-r border-white/10 overflow-y-auto shrink-0"
        aria-label="PSK31 session list"
      >
        {/* Live session entry */}
        <button
          type="button"
          onClick={() => setSelectedId('live')}
          aria-pressed={selectedId === 'live'}
          aria-label={isLive
            ? `Live — receiving PSK31 at ${formatFreq(liveFreq)}${liveSnr > 0 ? `, SNR ${liveSnr.toFixed(1)}×` : ''}`
            : 'Live — waiting for PSK31 signal'}
          className={`w-full text-left px-4 py-3 border-b border-white/6 transition-colors flex items-start gap-2
            ${selectedId === 'live' ? 'bg-white/8' : 'hover:bg-white/4'}`}
        >
          <StatusDot live={isLive} />
          <div className="min-w-0">
            <p className="text-xs text-amber-400 font-semibold">Live</p>
            {liveStartTs && (
              <p className="text-[10px] text-white/30 font-mono">
                <time dateTime={liveStartTs}>{formatTime(liveStartTs)}</time>
              </p>
            )}
            {liveText ? (
              <p className="text-[10px] text-white/50 font-mono truncate mt-0.5" aria-hidden>
                {liveText.slice(0, 30)}
              </p>
            ) : (
              <p className="text-[10px] text-white/20 italic">Waiting for signal…</p>
            )}
          </div>
        </button>

        {/* Past sessions */}
        {sessions.map((s) => {
          const quality = sessionQuality(s.text);
          return (
            <button
              key={s.id}
              type="button"
              onClick={() => setSelectedId(s.id)}
              aria-pressed={selectedId === s.id}
              aria-label={`Session at ${formatTime(s.start_ts)}, ${formatFreq(s.freq_hz)}, signal quality ${QUALITY_META[quality].label}`}
              className={`w-full text-left px-4 py-3 border-b border-white/6 transition-colors flex items-start gap-2
                ${selectedId === s.id ? 'bg-white/8' : 'hover:bg-white/4'}`}
            >
              <StatusDot live={false} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5 flex-wrap">
                  <p className="text-xs text-white/70 font-mono">
                    <time dateTime={s.start_ts}>{formatTime(s.start_ts)}</time>
                  </p>
                  <QualityBadge quality={quality} />
                </div>
                <p className="text-[10px] text-white/30 font-mono">{formatFreq(s.freq_hz)}</p>
                <p className="text-[10px] text-white/50 font-mono truncate mt-0.5" aria-hidden>
                  {s.text.slice(0, 30)}
                </p>
              </div>
            </button>
          );
        })}

        {sessions.length === 0 && !isLive && (
          <output className="block text-white/20 text-xs italic p-4">No sessions yet.</output>
        )}
      </nav>

      {/* Right: detail pane (65%) */}
      <section
        className="flex-1 overflow-y-auto p-6 font-mono"
        aria-label={
          selectedId === 'live'
            ? 'Live PSK31 decode'
            : selectedSession
            ? `PSK31 session from ${formatTime(selectedSession.start_ts)}`
            : 'No session selected'
        }
        aria-live={selectedId === 'live' ? 'polite' : undefined}
        aria-atomic={false}
      >
        {selectedId === 'live' ? (
          <>
            <div className="flex items-center gap-2 mb-3">
              <p className="text-white/30 text-xs flex-1 font-mono">
                {liveStartTs && (
                  <>
                    <time dateTime={liveStartTs}>{formatTime(liveStartTs)}</time>
                    {' · '}
                  </>
                )}
                {formatFreq(liveFreq)}
                {liveSnr > 0 && (
                  <span className={`ml-2 ${liveQuality === 'poor' ? 'text-red-400/70' : liveQuality === 'fair' ? 'text-amber-400/70' : 'text-emerald-400/70'}`}>
                    SNR {liveSnr.toFixed(1)}×
                  </span>
                )}
              </p>
              {liveText && <CopyButton text={liveText} />}
            </div>
            {liveTokens.length > 0 ? (
              <PSK31Text tokens={liveTokens} cursor />
            ) : (
              <output className="block text-white/20 text-xs italic">
                {connected ? 'Waiting for PSK31 signal…' : 'Connecting to PSK31 decoder…'}
              </output>
            )}
          </>
        ) : selectedSession ? (
          <>
            <div className="flex items-center gap-2 mb-3">
              <div className="flex-1 flex items-center gap-2 flex-wrap">
                <p className="text-white/30 text-xs font-mono">
                  <time dateTime={selectedSession.start_ts}>{formatTime(selectedSession.start_ts)}</time>
                  {' – '}
                  <time dateTime={selectedSession.end_ts}>{formatTime(selectedSession.end_ts)}</time>
                  {' · '}
                  {formatFreq(selectedSession.freq_hz)}
                </p>
                <QualityBadge quality={sessionQuality(selectedSession.text)} />
              </div>
              <CopyButton text={selectedSession.text} />
            </div>
            <PSK31Text tokens={tokenise(selectedSession.text)} />
          </>
        ) : (
          <output className="block text-white/20 text-xs italic">Select a session from the list.</output>
        )}
      </section>
    </div>
  );
}

// ── Main export ───────────────────────────────────────────────────────────────

export function PSK31Panel() {
  const [open, toggleOpen] = useAccordion('psk31-open');
  const state = usePSK31State(open);

  return (
    <div className="glass rounded-2xl overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2 px-6 py-4 border-b border-white/10">
        <div className="flex-1">
          <h2 className="text-white text-xl font-semibold tracking-wide" id="psk31-panel-heading">
            PSK31 — 14.070 ±2kHz
          </h2>
          {open && (
            <p className="text-white/40 text-xs mt-0.5 font-mono" aria-live="polite">
              <span
                className={`inline-block w-2 h-2 rounded-full mr-1.5 ${state.connected ? 'bg-emerald-400' : 'bg-red-500'}`}
                aria-hidden
              />
              <span className="sr-only">{state.connected ? 'Connected.' : 'Not connected.'}</span>
              {state.connected
                ? `${formatFreq(state.liveFreq)} · ${state.sessions.length} session${state.sessions.length === 1 ? '' : 's'}`
                : 'Connecting to PSK31 decoder…'}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={toggleOpen}
          className="text-white/40 hover:text-white/80 transition-colors text-xs font-mono px-2 py-0.5 rounded border border-white/10 hover:border-white/30"
          aria-label={open ? 'Collapse PSK31 sessions panel' : 'Expand PSK31 sessions panel'}
          aria-expanded={open}
          aria-controls="psk31-panel-body"
        >
          {open ? '▲ hide' : '▼ show'}
        </button>
      </div>

      {open && (
        <div id="psk31-panel-body">
          <SessionPanel state={state} />
        </div>
      )}
    </div>
  );
}
