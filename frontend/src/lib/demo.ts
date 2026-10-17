/** Demo mode hides every page except the landing page and the TLDR issue page. On unless VITE_DEMO_MODE=false. */
export const DEMO_MODE = import.meta.env.VITE_DEMO_MODE !== 'false';

export const MAX_PREFERENCE_CHARS = 1000; // the backend limit (app/tldr/podcast.py MAX_PREFERENCE_CHARS)

export type Preference = { choice: 'yes' | 'no'; text: string };
const KEY = 'knowly.preference';

export function loadPreference(): Preference | null {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || 'null');
    if (v && (v.choice === 'yes' || v.choice === 'no') && typeof v.text === 'string') return v;
  } catch {
    /* storage can be blocked */
  }
  return null;
}

export function savePreference(p: Preference): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(p));
  } catch {
    /* the preference still applies for this visit */
  }
}

/** The preference the server should use: the trimmed text for "yes", nothing for "no". */
export const preferenceText = (p: Preference | null) => (p?.choice === 'yes' && p.text.trim() ? p.text.trim() : '');

export function firstWords(text: string, n = 14): string {
  const w = text.trim().split(/\s+/);
  return w.length > n ? `${w.slice(0, n).join(' ')}…` : text.trim();
}

/** TLDR headlines start with emojis ("🚀 Tesla Roadster, ..."); the page shows plain text. */
export const stripEmoji = (s: string) => s.replace(/[\p{Extended_Pictographic}️‍]/gu, '').replace(/\s+/g, ' ').trim();

const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** "2026-10-16" -> "Friday 16 Oct 2026" (the issue date is a calendar date, so no time zone is applied). */
export function longDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  return `${DAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}
export function shortDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

export const wordCount = (t: string) => t.trim().split(/\s+/).filter(Boolean).length;
