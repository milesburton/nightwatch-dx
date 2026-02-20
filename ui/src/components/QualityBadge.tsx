interface Props {
  verdict: 'good' | 'warn' | 'bad' | undefined;
}

const VERDICT_MAP = {
  good: { label: 'Good', className: 'bg-emerald-500/14 text-emerald-300 border border-emerald-500/28' },
  warn: { label: 'Warning', className: 'bg-amber-400/14 text-amber-300 border border-amber-400/28' },
  bad: { label: 'Poor', className: 'bg-red-400/14 text-red-300 border border-red-400/28' },
} as const;

export function QualityBadge({ verdict }: Props) {
  if (!verdict) return null;
  const { label, className } = VERDICT_MAP[verdict] ?? VERDICT_MAP.good;
  return (
    <span
      className={`inline-block text-xs font-bold uppercase tracking-wide px-2 py-0.5 rounded-full ml-2 align-middle ${className}`}
    >
      {label}
    </span>
  );
}
