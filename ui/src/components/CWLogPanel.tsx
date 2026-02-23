/**
 * CWLogPanel — persistent session log for CW and PSK31 modes.
 *
 * Header tabs switch between CW and PSK31.  Each mode has its own:
 *   - WebSocket connection (/ws/cw or /ws/psk31)
 *   - Live session state and 30-second inactivity timer
 *   - IndexedDB persistence (sdr-monitor / cw-sessions, mode-tagged)
 *
 * Layout: Fallout-style master-detail.
 *   Left (35%): scrollable session list (newest first), amber dot for live.
 *   Right (65%): selected session text or live decode stream.
 *
 * Each decoded character is a hoverable token — plain text visible by default,
 * Morse dots/dashes (CW mode) or varicode bits revealed on hover.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { CWSocketMessage } from '../types.js';
import type { CWSession } from '../utils/db.js';
import { listCWSessions, saveCWSession } from '../utils/db.js';

const SESSION_TIMEOUT_MS = 30_000;

type Mode = 'cw' | 'psk31';

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

interface CWToken {
  id: number;
  char: string;
  morse: string;
}

let _tokenId = 0;

function makeToken(ch: string): CWToken {
  return {
    id: _tokenId++,
    char: ch,
    morse: ch === ' ' ? '' : (CHAR_TO_MORSE[ch.toUpperCase()] ?? '?'),
  };
}

function tokenise(text: string): CWToken[] {
  return [...text].map(makeToken);
}

function CWChar({ token }: { token: CWToken }) {
  if (token.char === ' ') {
    return <span className="inline-block w-4" />;
  }

  return (
    <span className="group relative inline-block cursor-default select-none">
      <span className="group-hover:opacity-0 transition-opacity duration-100">
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
      {cursor && <span className="cw-cursor" />}
    </p>
  );
}

function copyToClipboard(text: string): Promise<void> {
  // navigator.clipboard requires a secure context (HTTPS / localhost).
  // On plain HTTP, navigator.clipboard exists but is undefined — check writeText.
  if (typeof navigator.clipboard?.writeText === 'function') {
    return navigator.clipboard.writeText(text);
  }
  // Legacy execCommand fallback for plain-HTTP home-lab use.
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

// ── Per-mode session hook ─────────────────────────────────────────────────────

interface ModeState {
  sessions: CWSession[];
  selectedId: number | 'live';
  setSelectedId: (id: number | 'live') => void;
  connected: boolean;
  liveTokens: CWToken[];
  liveText: string;
  liveFreq: number;
  liveStartTs: string;
}

function useModeState(mode: Mode, wsPath: string, defaultFreq: number): ModeState {
  const [sessions, setSessions] = useState<CWSession[]>([]);
  const [selectedId, setSelectedId] = useState<number | 'live'>('live');
  const [connected, setConnected] = useState(false);
  const [liveTokens, setLiveTokens] = useState<CWToken[]>([]);
  const [liveText, setLiveText] = useState('');
  const [liveFreq, setLiveFreq] = useState<number>(defaultFreq);
  const [liveStartTs, setLiveStartTs] = useState<string>('');

  const liveTextRef  = useRef('');
  const liveFreqRef  = useRef(defaultFreq);
  const liveStartRef = useRef('');
  const timerRef     = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Load persisted sessions on mount (filter by mode tag)
  useEffect(() => {
    listCWSessions().then((rows) => {
      const filtered = rows.filter((s) => (s.mode ?? 'cw') === mode);
      setSessions(filtered.sort((a, b) => b.startTs.localeCompare(a.startTs)));
    });
  }, [mode]);

  const flushSession = useCallback(() => {
    const text = liveTextRef.current.trim();
    if (!text) return;
    const session: CWSession = {
      startTs: liveStartRef.current,
      endTs: new Date().toISOString(),
      text,
      freqHz: liveFreqRef.current,
      mode,
    };
    saveCWSession(session).then(() => {
      setSessions((prev) => {
        const withId = { ...session, id: Date.now() };
        return [withId, ...prev];
      });
    });
    liveTextRef.current = '';
    liveStartRef.current = '';
    setLiveText('');
    setLiveTokens([]);
    setLiveStartTs('');
  }, [mode]);

  const resetTimer = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      flushSession();
    }, SESSION_TIMEOUT_MS);
  }, [flushSession]);

  useEffect(() => {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let ws: WebSocket | null = null;
    let closed = false;

    function connect() {
      if (closed) return;
      ws = new WebSocket(`${proto}//${location.host}${wsPath}`);
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
          resetTimer();
        } else if (msg.type === 'word_space') {
          liveTextRef.current += ' ';
          setLiveText(liveTextRef.current);
          setLiveTokens((prev) => [...prev, makeToken(' ')]);
          resetTimer();
        }
      };
    }

    connect();

    return () => {
      closed = true;
      ws?.close();
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [wsPath, resetTimer]);

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

function SessionPanel({ state, hintText }: { state: ModeState; hintText: string }) {
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
            onClick={() => {
              if (s.id !== undefined) setSelectedId(s.id);
            }}
            className={`w-full text-left px-4 py-3 border-b border-white/6 transition-colors flex items-start gap-2
              ${selectedId === s.id ? 'bg-white/8' : 'hover:bg-white/4'}`}
          >
            <StatusDot live={false} />
            <div className="min-w-0">
              <p className="text-xs text-white/70 font-mono">{formatTime(s.startTs)}</p>
              <p className="text-[10px] text-white/30 font-mono">{formatFreq(s.freqHz)}</p>
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
              <CWText tokens={liveTokens} cursor />
            ) : (
              <p className="text-white/20 text-xs italic">
                {connected ? `Waiting for ${hintText} signal…` : 'Connecting…'}
              </p>
            )}
          </>

        ) : selectedSession ? (
          <>
            <div className="flex items-center gap-2 mb-3">
              <p className="text-white/30 text-xs flex-1">
                {formatTime(selectedSession.startTs)}
                {' – '}
                {formatTime(selectedSession.endTs)}
                {' · '}
                {formatFreq(selectedSession.freqHz)}
              </p>
              <CopyButton text={selectedSession.text} />
            </div>
            <CWText tokens={tokenise(selectedSession.text)} />
          </>
        ) : (
          <p className="text-white/20 text-xs italic">Select a session.</p>
        )}
      </div>
    </div>
  );
}

// ── Main export ───────────────────────────────────────────────────────────────

export function CWLogPanel() {
  const [mode, setMode] = useState<Mode>('cw');

  const cwState    = useModeState('cw',    '/ws/cw',    14_029_000);
  const psk31State = useModeState('psk31', '/ws/psk31', 14_070_000);

  const active = mode === 'cw' ? cwState : psk31State;

  const freqLabel = mode === 'cw'
    ? formatFreq(active.liveFreq)
    : `14.070 ±2kHz`;

  const sessionCount = active.sessions.length;

  return (
    <div className="glass rounded-2xl overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-6 py-4 border-b border-white/10">
        <div>
          <div className="flex items-center gap-3 mb-0.5">
            <h2 className="text-white text-xl font-semibold tracking-wide">Sessions</h2>
            {/* Mode tabs */}
            <div className="flex gap-1">
              <button
                type="button"
                onClick={() => setMode('cw')}
                className={`text-xs font-mono px-2 py-0.5 rounded transition-colors ${
                  mode === 'cw'
                    ? 'bg-amber-400/20 text-amber-400 border border-amber-400/40'
                    : 'text-white/40 border border-white/10 hover:text-white/70 hover:border-white/30'
                }`}
              >
                CW
              </button>
              <button
                type="button"
                onClick={() => setMode('psk31')}
                className={`text-xs font-mono px-2 py-0.5 rounded transition-colors ${
                  mode === 'psk31'
                    ? 'bg-amber-400/20 text-amber-400 border border-amber-400/40'
                    : 'text-white/40 border border-white/10 hover:text-white/70 hover:border-white/30'
                }`}
              >
                PSK31
              </button>
            </div>
          </div>
          <p className="text-white/40 text-xs mt-0.5 font-mono">
            <span
              className={`inline-block w-2 h-2 rounded-full mr-1.5 ${active.connected ? 'bg-emerald-400' : 'bg-red-500'}`}
            />
            {active.connected
              ? `${freqLabel} · ${sessionCount} sessions`
              : `Connecting to ${mode === 'cw' ? 'CW' : 'PSK31'} decoder…`}
          </p>
        </div>
        <p className="text-white/20 text-[10px] font-mono italic">hover chars for morse</p>
      </div>

      {/* Master-detail body */}
      <SessionPanel
        key={mode}
        state={active}
        hintText={mode === 'cw' ? 'CW' : 'PSK31'}
      />
    </div>
  );
}
