/**
 * CWLogPanel — persistent CW session log with Fallout-style master-detail layout.
 *
 * Left (35%): scrollable session list (newest first), amber dot for live session.
 * Right (65%): selected session text or live decode stream.
 *
 * Sessions are persisted in IndexedDB (sdr-monitor / cw-sessions).
 * A 30-second inactivity timer marks the end of each session.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { addIQListener } from '../workers/iqWorkerSingleton.js';
import { listCWSessions, saveCWSession } from '../utils/db.js';
import type { CWSession } from '../utils/db.js';

const SESSION_TIMEOUT_MS = 30_000;

function StatusDot({ live }: { live: boolean }) {
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full flex-shrink-0 ${live ? 'bg-amber-400 animate-pulse' : 'bg-white/20'}`}
    />
  );
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function formatFreq(hz: number): string {
  return (hz / 1e6).toFixed(3) + ' MHz';
}

export function CWLogPanel() {
  // ── Persisted sessions ──────────────────────────────────────────────────────
  const [sessions, setSessions] = useState<CWSession[]>([]);
  const [selectedId, setSelectedId] = useState<number | 'live'>('live');

  // ── Live session state ──────────────────────────────────────────────────────
  const [connected, setConnected] = useState(false);
  const [liveText, setLiveText] = useState('');
  const [liveFreq, setLiveFreq] = useState<number>(14_029_000);
  const [liveStartTs, setLiveStartTs] = useState<string>('');

  const liveTextRef  = useRef('');
  const liveFreqRef  = useRef(14_029_000);
  const liveStartRef = useRef('');
  const timerRef     = useRef<ReturnType<typeof setTimeout> | null>(null);
  const bottomRef    = useRef<HTMLDivElement>(null);

  // ── Load persisted sessions on mount ───────────────────────────────────────
  useEffect(() => {
    listCWSessions().then((rows) => {
      // Newest first
      setSessions(rows.sort((a, b) => b.startTs.localeCompare(a.startTs)));
    });
  }, []);

  // ── Session flush ───────────────────────────────────────────────────────────
  const flushSession = useCallback(() => {
    const text = liveTextRef.current.trim();
    if (!text) return;
    const session: CWSession = {
      startTs: liveStartRef.current,
      endTs:   new Date().toISOString(),
      text,
      freqHz:  liveFreqRef.current,
    };
    // Save and prepend to list
    saveCWSession(session).then(() => {
      setSessions((prev) => {
        const withId = { ...session, id: Date.now() };   // temp id until reload
        return [withId, ...prev];
      });
    });
    liveTextRef.current  = '';
    liveStartRef.current = '';
    setLiveText('');
    setLiveStartTs('');
  }, []);

  // ── Reset inactivity timer ──────────────────────────────────────────────────
  const resetTimer = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      flushSession();
    }, SESSION_TIMEOUT_MS);
  }, [flushSession]);

  // ── IQ worker listener ──────────────────────────────────────────────────────
  useEffect(() => {
    const unsub = addIQListener((msg) => {
      if (msg.type === 'status') {
        setConnected(msg.connected);
        liveFreqRef.current = msg.centerFreq;
        setLiveFreq(msg.centerFreq);
      } else if (msg.type === 'cw_char') {
        liveFreqRef.current = msg.freq;
        setLiveFreq(msg.freq);
        if (!liveStartRef.current) {
          liveStartRef.current = msg.ts;
          setLiveStartTs(msg.ts);
        }
        liveTextRef.current += msg.char;
        setLiveText(liveTextRef.current);
        resetTimer();
      } else if (msg.type === 'cw_word_space') {
        liveTextRef.current += ' ';
        setLiveText(liveTextRef.current);
        resetTimer();
      }
    });
    return () => {
      unsub();
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [resetTimer]);

  // Auto-scroll live view
  useEffect(() => {
    if (selectedId === 'live') {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  });

  // ── Render helpers ──────────────────────────────────────────────────────────
  const selectedSession = selectedId !== 'live'
    ? sessions.find((s) => s.id === selectedId) ?? null
    : null;

  const isLive = liveText.length > 0;

  return (
    <div className="glass rounded-2xl overflow-hidden">
      {/* ── Header ── */}
      <div className="flex items-center justify-between px-6 py-4 border-b border-white/10">
        <div>
          <h2 className="text-white text-xl font-semibold tracking-wide">CW — Sessions</h2>
          <p className="text-white/40 text-xs mt-0.5 font-mono">
            <span
              className={`inline-block w-2 h-2 rounded-full mr-1.5 ${connected ? 'bg-emerald-400' : 'bg-red-500'}`}
            />
            {connected
              ? `${formatFreq(liveFreq)} · ${sessions.length} sessions`
              : 'Connecting to IQ stream…'}
          </p>
        </div>
      </div>

      {/* ── Master-detail body ── */}
      <div className="flex" style={{ minHeight: '400px', maxHeight: '60vh' }}>

        {/* ── Left: session list (35%) ── */}
        <div className="w-[35%] border-r border-white/10 overflow-y-auto flex-shrink-0">
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
                <p className="text-[10px] text-white/50 font-mono truncate mt-0.5">{liveText.slice(0, 30)}</p>
              )}
              {!liveText && (
                <p className="text-[10px] text-white/20 italic">Waiting for signal…</p>
              )}
            </div>
          </button>

          {/* Past sessions */}
          {sessions.map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => setSelectedId(s.id!)}
              className={`w-full text-left px-4 py-3 border-b border-white/6 transition-colors flex items-start gap-2
                ${selectedId === s.id ? 'bg-white/8' : 'hover:bg-white/4'}`}
            >
              <StatusDot live={false} />
              <div className="min-w-0">
                <p className="text-xs text-white/70 font-mono">{formatTime(s.startTs)}</p>
                <p className="text-[10px] text-white/30 font-mono">{formatFreq(s.freqHz)}</p>
                <p className="text-[10px] text-white/50 font-mono truncate mt-0.5">{s.text.slice(0, 30)}</p>
              </div>
            </button>
          ))}

          {sessions.length === 0 && !isLive && (
            <p className="text-white/20 text-xs italic p-4">No sessions yet.</p>
          )}
        </div>

        {/* ── Right: content (65%) ── */}
        <div className="flex-1 overflow-y-auto p-6 font-mono">
          {selectedId === 'live' ? (
            <>
              {liveStartTs && (
                <p className="text-white/30 text-xs mb-3">
                  {formatTime(liveStartTs)} · {formatFreq(liveFreq)}
                </p>
              )}
              {liveText ? (
                <p className="text-emerald-300 text-sm leading-relaxed whitespace-pre-wrap">
                  {liveText}
                  <span className="cw-cursor" />
                </p>
              ) : (
                <p className="text-white/20 text-xs italic">
                  {connected ? 'Waiting for CW signal…' : 'Connecting…'}
                </p>
              )}
              <div ref={bottomRef} />
            </>
          ) : selectedSession ? (
            <>
              <p className="text-white/30 text-xs mb-3">
                {formatTime(selectedSession.startTs)}
                {' – '}
                {formatTime(selectedSession.endTs)}
                {' · '}
                {formatFreq(selectedSession.freqHz)}
              </p>
              <p className="text-emerald-300 text-sm leading-relaxed whitespace-pre-wrap">
                {selectedSession.text}
              </p>
            </>
          ) : (
            <p className="text-white/20 text-xs italic">Select a session.</p>
          )}
        </div>
      </div>
    </div>
  );
}
