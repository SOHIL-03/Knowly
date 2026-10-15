export interface LineTiming {
  s: number;
  e: number;
}

/** Index of the script line being spoken at time t (binary search over start times), or -1. */
export function lineAt(timings: LineTiming[], t: number): number {
  let lo = 0;
  let hi = timings.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (timings[mid].s <= t) {
      ans = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return ans;
}

export function formatTime(secs: number): string {
  if (!Number.isFinite(secs) || secs < 0) secs = 0;
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

export function minutes(secs?: number): string {
  if (!secs) return '';
  return `${Math.max(1, Math.round(secs / 60))} min`;
}
