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
 * Scans ±2 kHz around 14.070 MHz to lock onto the strongest PSK31 carrier.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { CWSocketMessage } from '../types.js';
import type { ApiSession } from '../utils/api.js';
import { fetchSessions } from '../utils/api.js';
import { useAccordion } from '../utils/useAccordion.js';

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
    return <span className="inline-block w-4" />;
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
      {cursor && <span className="cw-cursor" />}
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
}

function usePSK31State(open: boolean): SessionState {
  const [sessions, setSessions] = useState<ApiSession[]>([]);
  const [selectedId, setSelectedId] = useState<number | 'live'>('live');
  const [connected, setConnected] = useState(false);
  const [liveTokens, setLiveTokens] = useState<PSK31Token[]>([]);
  const [liveText, setLiveText] = useState('');
  const [liveFreq, setLiveFreq] = useState<number>(14_070_000);
  const [liveStartTs, setLiveStartTs] = useState<string>('');

  const liveTextRef  = useRef('');
  const liveFreqRef  = useRef(14_070_000);
  const liveStartRef = useRef('');

  // Load history from REST on panel open
  useEffect(() => {
    if (!open) return;
    fetchSessions('psk31').then((rows) => {
      setSessions(rows);
    });
  }, [open]);

  const resetLive = useCallback(() => {
    liveTextRef.current = '';
    liveStartRef.current = '';
    setLiveText('');
    setLiveTokens([]);
    setLiveStartTs('');
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
          // Server flushed a session — prepend to history and clear live state
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

  return {
    sessions,
    selectedId,
    setSelectedId,
    connected,
    liveTokens,
    liveText,
    liveFreq,
    liveStartTs,
  };
}

// ── Session panel (master-detail list + detail pane) ─────────────────────────

function SessionPanel({ state }: { state: SessionState }) {
  const { sessions, selectedId, setSelectedId, connected, liveTokens,
          liveText, liveFreq, liveStartTs } = state;

  const selectedSession =
    selectedId !== 'live' ? (sessions.find((s) => s.id === selectedId) ?? null) : null;

  const isLive = liveText.length > 0;

  return (
    <div className="flex" style={{ minHeight: '400px', maxHeight: '60vh' }}>
      {/* Left: session list (35%) */}
      <div className="w-[35%] border-r border-white/10 overflow-y-auto shrink-0">
        {/* Live session entry */}
        <button
          type="button"
          onClick={() => setSelectedId('live')}
          className={`w-full text-left px-4 py-3 border-b border-white/6 transition-colors flex items-start gap-2
            ${selectedId === 'live' ? 'bg-white/8' : 'hover:bg-white/4'}`}
        >
          <StatusDot live={isLive} />
          <div className="min-w-0">
            <p className="text-xs text-amber-400 font-semibold">Live</p>
            {liveStartTs && (
              <p className="text-[10px] text-white/30 font-mono">{formatTime(liveStartTs)}</p>
            )}
            {liveText && (
              <p className="text-[10px] text-white/50 font-mono truncate mt-0.5">
                {liveText.slice(0, 30)}
              </p>
            )}
            {!liveText && <p className="text-[10px] text-white/20 italic">Waiting for signal…</p>}
          </div>
        </button>

        {/* Past sessions */}
        {sessions.map((s) => (
          <button
            key={s.id}
            type="button"
            onClick={() => setSelectedId(s.id)}
            className={`w-full text-left px-4 py-3 border-b border-white/6 transition-colors flex items-start gap-2
              ${selectedId === s.id ? 'bg-white/8' : 'hover:bg-white/4'}`}
          >
            <StatusDot live={false} />
            <div className="min-w-0">
              <p className="text-xs text-white/70 font-mono">{formatTime(s.start_ts)}</p>
              <p className="text-[10px] text-white/30 font-mono">{formatFreq(s.freq_hz)}</p>
              <p className="text-[10px] text-white/50 font-mono truncate mt-0.5">
                {s.text.slice(0, 30)}
              </p>
            </div>
          </button>
        ))}

        {sessions.length === 0 && !isLive && (
          <p className="text-white/20 text-xs italic p-4">No sessions yet.</p>
        )}
      </div>

      {/* Right: detail pane (65%) */}
      <div className="flex-1 overflow-y-auto p-6 font-mono">
        {selectedId === 'live' ? (
          <>
            <div className="flex items-center gap-2 mb-3">
              {liveStartTs && (
                <p className="text-white/30 text-xs flex-1">
                  {formatTime(liveStartTs)} · {formatFreq(liveFreq)}
                </p>
              )}
              {liveText && <CopyButton text={liveText} />}
            </div>
            {liveTokens.length > 0 ? (
              <PSK31Text tokens={liveTokens} cursor />
            ) : (
              <p className="text-white/20 text-xs italic">
                {connected ? 'Waiting for PSK31 signal…' : 'Connecting…'}
              </p>
            )}
          </>

        ) : selectedSession ? (
          <>
            <div className="flex items-center gap-2 mb-3">
              <p className="text-white/30 text-xs flex-1">
                {formatTime(selectedSession.start_ts)}
                {' – '}
                {formatTime(selectedSession.end_ts)}
                {' · '}
                {formatFreq(selectedSession.freq_hz)}
              </p>
              <CopyButton text={selectedSession.text} />
            </div>
            <PSK31Text tokens={tokenise(selectedSession.text)} />
          </>
        ) : (
          <p className="text-white/20 text-xs italic">Select a session.</p>
        )}
      </div>
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
          <h2 className="text-white text-xl font-semibold tracking-wide">
            PSK31 — 14.070 ±2kHz
          </h2>
          {open && (
            <p className="text-white/40 text-xs mt-0.5 font-mono">
              <span
                className={`inline-block w-2 h-2 rounded-full mr-1.5 ${state.connected ? 'bg-emerald-400' : 'bg-red-500'}`}
              />
              {state.connected
                ? `${formatFreq(state.liveFreq)} · ${state.sessions.length} sessions`
                : 'Connecting to PSK31 decoder…'}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={toggleOpen}
          className="text-white/40 hover:text-white/80 transition-colors text-xs font-mono px-2 py-0.5 rounded border border-white/10 hover:border-white/30"
          aria-label={open ? 'Collapse PSK31 sessions' : 'Expand PSK31 sessions'}
        >
          {open ? '▲ hide' : '▼ show'}
        </button>
      </div>

      {/* Master-detail body */}
      {open && <SessionPanel state={state} />}
    </div>
  );
}
