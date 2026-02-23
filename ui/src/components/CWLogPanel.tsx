/**
 * CWLogPanel — persistent session log for CW (Morse code) transmissions.
 *
 * Receives decoded characters from the cw-decoder Python backend via
 * WebSocket at /ws/cw. Session history is loaded from the REST API
 * (/api/sessions?mode=cw) on panel open. The server flushes sessions
 * after 30 s of inactivity and broadcasts a `session` notification.
 *
 * Layout: Fallout-style master-detail.
 *   Left (35%): scrollable session list (newest first), amber dot for live.
 *   Right (65%): selected session text or live decode stream.
 *
 * Signal validity: garbled characters appear as [dit-dah sequences] in square
 * brackets. Sessions with >30% garbled chars are flagged as poor quality.
 * Live WPM is shown in the detail pane header.
 *
 * Each recognised character is a hoverable token — hover reveals Morse notation.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { CWSocketMessage } from '../types.js';
import type { ApiSession } from '../utils/api.js';
import { fetchSessions } from '../utils/api.js';
import { useAccordion } from '../utils/useAccordion.js';

const CHAR_TO_MORSE: Record<string, string> = {
  A: '.-',    B: '-...',  C: '-.-.',  D: '-..',   E: '.',
  F: '..-.',  G: '--.',   H: '....',  I: '..',    J: '.---',
  K: '-.-',   L: '.-..',  M: '--',    N: '-.',    O: '---',
  P: '.--.',  Q: '--.-',  R: '.-.',   S: '...',   T: '-',
  U: '..-',   V: '...-',  W: '.--',   X: '-..-',  Y: '-.--',
  Z: '--..',
  '0': '-----', '1': '.----', '2': '..---', '3': '...--', '4': '....-',
  '5': '.....', '6': '-....', '7': '--...', '8': '---..', '9': '----.',
  '.': '.-.-.-', ',': '--..--', '?': '..--..', "'": '.----.',
  '!': '-.-.--', '/': '-..-.', '(': '-.--.', ')': '-.--.-',
  '&': '.-...', ':': '---...', ';': '-.-.-.', '=': '-...-',
  '+': '.-.-.', '-': '-....-', '_': '..--.-', '"': '.-..-.',
  '$': '...-..-', '@': '.--.-.',
};

/** A character is garbled when the decoder couldn't match a Morse sequence,
 *  producing a bracketed fallback like [.-..-.] */
function isGarbled(ch: string): boolean {
  return ch.startsWith('[') && ch.endsWith(']');
}

/** 0–1 fraction of garbled tokens in text (each bracket group = 1 token). */
function garbledRatio(text: string): number {
  if (!text) return 0;
  const tokens = text.split(/(\[[^\]]+\])/g).filter(Boolean);
  const garbledCount = tokens.filter((t) => t.startsWith('[') && t.endsWith(']')).length;
  const total = tokens.reduce((acc, t) => acc + (t.startsWith('[') ? 1 : t.length), 0);
  return total === 0 ? 0 : garbledCount / total;
}

type QualityLevel = 'good' | 'fair' | 'poor';

function sessionQuality(text: string): QualityLevel {
  const ratio = garbledRatio(text);
  if (ratio > 0.35) return 'poor';
  if (ratio > 0.12) return 'fair';
  return 'good';
}

const QUALITY_META: Record<QualityLevel, { dot: string; label: string; desc: string }> = {
  good: { dot: 'bg-emerald-400', label: 'Good', desc: 'Signal quality: good — most characters recognised' },
  fair: { dot: 'bg-amber-400',   label: 'Fair', desc: 'Signal quality: fair — some unrecognised Morse sequences' },
  poor: { dot: 'bg-red-500',     label: 'Poor', desc: 'Signal quality: poor — high proportion of unrecognised Morse sequences' },
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

interface CWToken {
  id: number;
  char: string;
  morse: string;
  garbled: boolean;
}

let _tokenId = 0;

function makeToken(ch: string): CWToken {
  const g = isGarbled(ch);
  return {
    id: _tokenId++,
    char: ch,
    morse: g ? ch : (ch === ' ' ? '' : (CHAR_TO_MORSE[ch.toUpperCase()] ?? '?')),
    garbled: g,
  };
}

function tokenise(text: string): CWToken[] {
  return text.split(/(\[[^\]]+\])/g).filter(Boolean).flatMap((seg) => {
    if (seg.startsWith('[') && seg.endsWith(']')) return [makeToken(seg)];
    return [...seg].map(makeToken);
  });
}

function CWChar({ token }: { token: CWToken }) {
  if (token.char === ' ') {
    return <span className="inline-block w-4" aria-hidden />;
  }

  if (token.garbled) {
    return (
      <span
        className="inline-block font-mono text-red-400/70 text-[0.7em] align-middle px-0.5"
        title={`Unrecognised Morse sequence: ${token.char}`}
        role="img"
        aria-label={`Unrecognised Morse sequence ${token.char}`}
      >
        {token.char}
      </span>
    );
  }

  return (
    <span
      className="group relative inline-block cursor-default select-none"
      title={`${token.char} — Morse: ${token.morse}`}
    >
      <span className="group-hover:opacity-0 transition-opacity duration-100" aria-hidden>
        {token.char}
      </span>
      <span
        className="absolute inset-0 flex items-center justify-center text-[0.65em] tracking-widest text-amber-400 whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity duration-100"
        aria-hidden
      >
        {token.morse}
      </span>
    </span>
  );
}

function CWText({ tokens, cursor }: { tokens: CWToken[]; cursor?: boolean }) {
  return (
    <p className="text-emerald-300 text-sm leading-relaxed whitespace-pre-wrap font-mono">
      {tokens.map((tok) => (
        <CWChar key={tok.id} token={tok} />
      ))}
      {cursor && <span className="cw-cursor" aria-hidden />}
    </p>
  );
}

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
  liveTokens: CWToken[];
  liveText: string;
  liveFreq: number;
  liveStartTs: string;
  liveWpm: number;
}

function useCWState(open: boolean): SessionState {
  const [sessions, setSessions] = useState<ApiSession[]>([]);
  const [selectedId, setSelectedId] = useState<number | 'live'>('live');
  const [connected, setConnected] = useState(false);
  const [liveTokens, setLiveTokens] = useState<CWToken[]>([]);
  const [liveText, setLiveText] = useState('');
  const [liveFreq, setLiveFreq] = useState<number>(14_029_000);
  const [liveStartTs, setLiveStartTs] = useState<string>('');
  const [liveWpm, setLiveWpm] = useState(0);

  const liveTextRef  = useRef('');
  const liveFreqRef  = useRef(14_029_000);
  const liveStartRef = useRef('');

  useEffect(() => {
    if (!open) return;
    fetchSessions('cw').then((rows) => setSessions(rows));
  }, [open]);

  const resetLive = useCallback(() => {
    liveTextRef.current = '';
    liveStartRef.current = '';
    setLiveText('');
    setLiveTokens([]);
    setLiveStartTs('');
    setLiveWpm(0);
  }, []);

  useEffect(() => {
    if (!open) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let ws: WebSocket | null = null;
    let closed = false;

    function connect() {
      if (closed) return;
      ws = new WebSocket(`${proto}//${location.host}/ws/cw`);
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
          if (msg.wpm) setLiveWpm(msg.wpm);
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
           liveTokens, liveText, liveFreq, liveStartTs, liveWpm };
}

// ── Session panel (master-detail list + detail pane) ─────────────────────────

function SessionPanel({ state }: { state: SessionState }) {
  const { sessions, selectedId, setSelectedId, connected, liveTokens,
          liveText, liveFreq, liveStartTs, liveWpm } = state;

  const selectedSession =
    selectedId !== 'live' ? (sessions.find((s) => s.id === selectedId) ?? null) : null;

  const isLive = liveText.length > 0;

  return (
    <div className="flex" style={{ minHeight: '400px', maxHeight: '60vh' }}>
      {/* Left: session list (35%) */}
      <nav
        className="w-[35%] border-r border-white/10 overflow-y-auto shrink-0"
        aria-label="CW session list"
      >
        {/* Live session entry */}
        <button
          type="button"
          onClick={() => setSelectedId('live')}
          aria-pressed={selectedId === 'live'}
          aria-label={isLive
            ? `Live — receiving CW at ${formatFreq(liveFreq)}${liveWpm ? `, ${liveWpm} WPM` : ''}`
            : 'Live — waiting for CW signal'}
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
            ? 'Live CW decode'
            : selectedSession
            ? `CW session from ${formatTime(selectedSession.start_ts)}`
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
                {liveWpm > 0 && (
                  <span className="ml-2 text-amber-400/70">
                    {liveWpm} WPM
                  </span>
                )}
              </p>
              {liveText && <CopyButton text={liveText} />}
            </div>
            {liveTokens.length > 0 ? (
              <CWText tokens={liveTokens} cursor />
            ) : (
              <output className="block text-white/20 text-xs italic">
                {connected ? 'Waiting for CW signal…' : 'Connecting to CW decoder…'}
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
            <CWText tokens={tokenise(selectedSession.text)} />
          </>
        ) : (
          <output className="block text-white/20 text-xs italic">Select a session from the list.</output>
        )}
      </section>
    </div>
  );
}

// ── Main export ───────────────────────────────────────────────────────────────

export function CWLogPanel() {
  const [open, toggleOpen] = useAccordion('sessions-open');
  const state = useCWState(open);

  return (
    <div className="glass rounded-2xl overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2 px-6 py-4 border-b border-white/10">
        <div className="flex-1">
          <h2 className="text-white text-xl font-semibold tracking-wide" id="cw-panel-heading">
            CW — 14.029 MHz
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
                : 'Connecting to CW decoder…'}
            </p>
          )}
        </div>
        {open && (
          <p className="text-white/20 text-[10px] font-mono italic" aria-hidden>
            hover chars for morse
          </p>
        )}
        <button
          type="button"
          onClick={toggleOpen}
          className="text-white/40 hover:text-white/80 transition-colors text-xs font-mono px-2 py-0.5 rounded border border-white/10 hover:border-white/30"
          aria-label={open ? 'Collapse CW sessions panel' : 'Expand CW sessions panel'}
          aria-expanded={open}
          aria-controls="cw-panel-body"
        >
          {open ? '▲ hide' : '▼ show'}
        </button>
      </div>

      {open && (
        <div id="cw-panel-body">
          <SessionPanel state={state} />
        </div>
      )}
    </div>
  );
}
