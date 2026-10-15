import { Loader2 } from 'lucide-react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger';

const variants: Record<Variant, string> = {
  primary: 'bg-accent text-on-accent hover:bg-accent-hover',
  secondary: 'bg-surface text-ink border border-line hover:bg-surface-2',
  ghost: 'text-ink-2 hover:bg-surface-2',
  danger: 'bg-surface text-notyet border border-notyet/40 hover:bg-notyet-soft',
};

export function Button({
  variant = 'primary',
  busy,
  full,
  className = '',
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; busy?: boolean; full?: boolean }) {
  return (
    <button
      {...rest}
      disabled={rest.disabled || busy}
      className={`inline-flex min-h-11 items-center justify-center gap-2 rounded-xl px-4 text-body font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${variants[variant]} ${full ? 'w-full' : ''} ${className}`}
    >
      {busy && <Loader2 className="size-4 animate-spin" aria-hidden />}
      {children}
    </button>
  );
}

export function Card({ children, className = '', as: As = 'div' }: { children: ReactNode; className?: string; as?: 'div' | 'section' | 'li' | 'article' }) {
  return <As className={`rounded-[var(--radius-card)] border border-line bg-surface p-4 ${className}`}>{children}</As>;
}

export function Chip({
  children,
  active,
  onClick,
  className = '',
  title,
}: { children: ReactNode; active?: boolean; onClick?: () => void; className?: string; title?: string }) {
  const cls = `inline-flex min-h-9 items-center gap-1.5 rounded-full border px-3 text-caption font-medium ${
    active ? 'border-accent bg-accent-soft text-accent' : 'border-line bg-surface text-ink-2'
  } ${onClick ? 'hover:border-accent/60' : ''} ${className}`;
  return onClick ? (
    <button type="button" onClick={onClick} aria-pressed={active} className={cls} title={title}>
      {children}
    </button>
  ) : (
    <span className={cls} title={title}>
      {children}
    </span>
  );
}

export function Banner({ tone = 'info', children, action }: { tone?: 'info' | 'warn' | 'error' | 'good'; children: ReactNode; action?: ReactNode }) {
  const tones = {
    info: 'bg-accent-soft text-ink border-accent/30',
    warn: 'bg-partly-soft text-ink border-partly/40',
    error: 'bg-notyet-soft text-ink border-notyet/40',
    good: 'bg-good-soft text-ink border-good/40',
  };
  return (
    <div role={tone === 'error' ? 'alert' : 'status'} className={`flex flex-wrap items-center justify-between gap-3 rounded-xl border px-4 py-3 text-body ${tones[tone]}`}>
      <div className="min-w-0 flex-1">{children}</div>
      {action}
    </div>
  );
}

export function Spinner({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-ink-3" role="status">
      <Loader2 className="size-5 animate-spin" aria-hidden />
      <span className="text-caption">{label}</span>
    </div>
  );
}

export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`skeleton ${className}`} aria-hidden />;
}

export function MasteryBar({ value, label }: { value: number; label: string }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <div className="flex items-center gap-3">
      <div className="h-2 flex-1 overflow-hidden rounded-full bg-surface-2" role="meter" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label={`${label} mastery`}>
        <div className="h-full rounded-full bg-accent" style={{ width: `${pct}%` }} />
      </div>
      <span className="w-10 text-right text-caption tabular-nums text-ink-2">{pct}%</span>
    </div>
  );
}

export function ScoreRing({ points, total }: { points: number; total: number }) {
  const r = 34;
  const c = 2 * Math.PI * r;
  const frac = total ? points / total : 0;
  return (
    <div className="relative size-24" role="img" aria-label={`Quiz score ${points} of ${total}`}>
      <svg viewBox="0 0 80 80" className="size-24 -rotate-90">
        <circle cx="40" cy="40" r={r} fill="none" stroke="var(--surface-2)" strokeWidth="8" />
        <circle cx="40" cy="40" r={r} fill="none" stroke="var(--accent)" strokeWidth="8" strokeLinecap="round" strokeDasharray={`${c * frac} ${c}`} />
      </svg>
      <span className="absolute inset-0 flex items-center justify-center text-section font-semibold tabular-nums">
        {points}/{total}
      </span>
    </div>
  );
}

export function Toggle({ checked, onChange, label, description }: { checked: boolean; onChange: (v: boolean) => void; label: string; description?: string }) {
  return (
    <label className="flex min-h-11 cursor-pointer items-center justify-between gap-4">
      <span>
        <span className="block text-body">{label}</span>
        {description && <span className="block text-caption text-ink-3">{description}</span>}
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={`relative h-7 w-12 shrink-0 rounded-full transition-colors ${checked ? 'bg-accent' : 'bg-line'}`}
      >
        <span className={`absolute top-1 size-5 rounded-full bg-surface shadow transition-transform ${checked ? 'translate-x-6' : 'translate-x-1'}`} />
      </button>
    </label>
  );
}

export function Segmented<T extends string | number>({
  value,
  options,
  onChange,
  label,
}: { value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; label: string }) {
  return (
    <div role="radiogroup" aria-label={label} className="grid auto-cols-fr grid-flow-col gap-1 rounded-xl bg-surface-2 p-1">
      {options.map((o) => (
        <button
          key={String(o.value)}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          onClick={() => onChange(o.value)}
          className={`min-h-10 rounded-lg px-3 text-body ${value === o.value ? 'bg-surface font-semibold text-ink shadow-sm' : 'text-ink-2'}`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function PageHeader({ title, subtitle, action }: { title: string; subtitle?: ReactNode; action?: ReactNode }) {
  return (
    <header className="mb-6 flex items-start justify-between gap-4">
      <div>
        <h1 className="text-title font-bold">{title}</h1>
        {subtitle && <p className="mt-1 text-body text-ink-2">{subtitle}</p>}
      </div>
      {action}
    </header>
  );
}

export function GradeBadge({ grade }: { grade: 'correct' | 'partly' | 'not_yet' }) {
  const map = {
    correct: ['bg-good-soft text-good', 'Correct'],
    partly: ['bg-partly-soft text-partly', 'Partly correct'],
    not_yet: ['bg-notyet-soft text-notyet', 'Not yet'],
  } as const;
  const [cls, text] = map[grade];
  return <span className={`rounded-full px-3 py-1 text-caption font-semibold ${cls}`}>{text}</span>;
}
