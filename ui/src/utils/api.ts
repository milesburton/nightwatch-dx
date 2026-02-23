export interface ApiSession {
  id: number;
  mode: string;
  start_ts: string;
  end_ts: string;
  freq_hz: number;
  text: string;
}

export interface ApiFrame {
  id: number;
  mode: string;
  ts: string;
  freq_hz: number;
  url: string;
}

export async function fetchSessions(
  mode: 'cw' | 'psk31',
  limit = 50,
  before?: string,
): Promise<ApiSession[]> {
  const params = new URLSearchParams({ mode, limit: String(limit) });
  if (before) params.set('before', before);
  const res = await fetch(`/api/sessions?${params}`);
  if (!res.ok) return [];
  const data = (await res.json()) as { sessions: ApiSession[] };
  return data.sessions;
}

export async function fetchFrames(
  mode: 'sstv' | 'easypal',
  limit = 50,
  before?: string,
): Promise<ApiFrame[]> {
  const params = new URLSearchParams({ mode, limit: String(limit) });
  if (before) params.set('before', before);
  const res = await fetch(`/api/frames?${params}`);
  if (!res.ok) return [];
  const data = (await res.json()) as { frames: ApiFrame[] };
  return data.frames;
}
