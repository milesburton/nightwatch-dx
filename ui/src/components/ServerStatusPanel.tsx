/**
 * ServerStatusPanel — live host metrics streamed from /ws/status.
 *
 * The cw-decoder service exposes a /ws/status WebSocket that pushes
 * CPU, load average, RAM, and disk stats every 5 seconds via psutil.
 */

import { useEffect, useState } from 'react';

interface StatusMessage {
  type: 'status';
  cpu_pct: number;
  load_1: number;
  load_5: number;
  load_15: number;
  mem_used_mb: number;
  mem_total_mb: number;
  mem_pct: number;
  disk_used_gb: number;
  disk_total_gb: number;
  disk_pct: number;
  ts: string;
}

function Bar({ pct, warn = 70, danger = 90 }: { pct: number; warn?: number; danger?: number }) {
  const colour =
    pct >= danger ? 'bg-red-500' : pct >= warn ? 'bg-amber-400' : 'bg-emerald-400';
  return (
    <div className="w-full bg-white/10 rounded-full h-1.5 overflow-hidden">
      <div
        className={`h-full rounded-full transition-all duration-500 ${colour}`}
        style={{ width: `${Math.min(pct, 100)}%` }}
      />
    </div>
  );
}

function Stat({
  label,
  value,
  sub,
  pct,
}: {
  label: string;
  value: string;
  sub?: string;
  pct?: number;
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex justify-between items-baseline">
        <span className="text-white/50 text-xs uppercase tracking-wider">{label}</span>
        <span className="text-white font-mono text-sm">{value}</span>
      </div>
      {pct !== undefined && <Bar pct={pct} />}
      {sub && <span className="text-white/30 text-[10px] font-mono">{sub}</span>}
    </div>
  );
}

export function ServerStatusPanel() {
  const [stats, setStats] = useState<StatusMessage | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let ws: WebSocket | null = null;
    let closed = false;

    function connect() {
      if (closed) return;
      ws = new WebSocket(`${proto}//${location.host}/ws/status`);
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!closed) setTimeout(connect, 5000);
      };
      ws.onerror = () => ws?.close();
      ws.onmessage = (e: MessageEvent<string>) => {
        try {
          const msg = JSON.parse(e.data) as StatusMessage;
          if (msg.type === 'status') setStats(msg);
        } catch {
          /* ignore */
        }
      };
    }

    connect();
    return () => {
      closed = true;
      ws?.close();
    };
  }, []);

  return (
    <div className="glass rounded-2xl p-6 flex flex-col gap-4">
      <div className="flex items-center gap-2 pb-4 border-b border-white/10">
        <h2 className="text-white text-xl font-semibold tracking-wide flex-1">Server Status</h2>
        <span
          className={`inline-block w-2 h-2 rounded-full ${connected ? 'bg-emerald-400' : 'bg-red-500'}`}
          title={connected ? 'Connected' : 'Connecting…'}
        />
      </div>

      {!stats ? (
        <p className="text-white/20 text-xs italic text-center py-4">
          {connected ? 'Waiting for data…' : 'Connecting…'}
        </p>
      ) : (
        <div className="flex flex-col gap-4">
          <Stat
            label="CPU"
            value={`${stats.cpu_pct}%`}
            sub={`load ${stats.load_1} / ${stats.load_5} / ${stats.load_15}`}
            pct={stats.cpu_pct}
          />
          <Stat
            label="RAM"
            value={`${stats.mem_pct}%`}
            sub={`${stats.mem_used_mb} / ${stats.mem_total_mb} MB`}
            pct={stats.mem_pct}
          />
          <Stat
            label="Disk"
            value={`${stats.disk_pct}%`}
            sub={`${stats.disk_used_gb} / ${stats.disk_total_gb} GB`}
            pct={stats.disk_pct}
          />
        </div>
      )}
    </div>
  );
}
