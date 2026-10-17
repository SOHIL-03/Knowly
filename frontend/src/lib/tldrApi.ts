import type { PodcastState } from '../components/TldrPodcast';
import { ApiError } from './api';

export type Story = { id: string; title: string; url: string; summary: string; read_minutes: number | null; sponsored: boolean };
export type Section = { id: string; name: string; emoji: string; stories: Story[] };
export type Issue = { category: string; date: string; url: string; headline: string; sections: Section[] };

// The TLDR endpoints are public (plain fetch). A bearer token is added when someone is signed in, so a signed-in
// listener's learning profile can shape the episode.
async function bearer(): Promise<Record<string, string>> {
  try {
    const { auth } = await import('./firebase');
    const token = await auth.currentUser?.getIdToken();
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch {
    return {};
  }
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, { ...init, headers: { ...(await bearer()), ...(init.headers as Record<string, string>) } });
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, body?.error?.code || 'http_error', body?.error?.message || `Request failed (${res.status})`);
  return body as T;
}

/** Sponsored stories never reach the page, and a section left with no stories is hidden. */
function withoutSponsors(issue: Issue): Issue {
  const sections = issue.sections
    .map((s) => ({ ...s, stories: s.stories.filter((x) => !x.sponsored) }))
    .filter((s) => s.stories.length > 0);
  return { ...issue, sections };
}

export const fetchIssue = (date?: string, category = 'tech') =>
  call<Issue>(date ? `/tldr/issues/${encodeURIComponent(date)}?category=${category}` : `/tldr/latest?category=${category}`).then(withoutSponsors);

const sectionQuery = (issue: Pick<Issue, 'category' | 'date'>, section: string, variant?: string | null) =>
  `?category=${encodeURIComponent(issue.category)}&date=${encodeURIComponent(issue.date)}&section=${encodeURIComponent(section)}` +
  (variant ? `&variant=${encodeURIComponent(variant)}` : '');

/** Starts (or finds) the podcast of one section. `preference` empty = the general conversation. */
export const startSectionPodcast = (issue: Pick<Issue, 'category' | 'date'>, section: string, preference: string) =>
  call<PodcastState>('/tldr/podcast', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ category: issue.category, date: issue.date, section, ...(preference ? { user_preferences: preference } : {}) }),
  });

/** Status of the general (no preference) podcast of a section; used for the "Ready" badge on the cards. */
export const sectionPodcastStatus = (issue: Pick<Issue, 'category' | 'date'>, section: string) =>
  call<PodcastState>(`/tldr/podcast${sectionQuery(issue, section)}`);

export const joinWaitlist = (email: string, senders: string[]) =>
  call<{ ok: true }>('/waitlist', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, senders }) });
