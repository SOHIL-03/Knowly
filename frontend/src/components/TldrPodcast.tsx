import { Headphones } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { podcastUrl } from '../lib/podcastApi';
import { Banner, Button } from './ui';

type Line = { speaker: string; role: 'question' | 'answer'; text: string };
export type Report = {
  status: 'complete' | 'partial';
  expectedPoints: number;
  coveredPoints: number;
  missing: { id: string; topic: string; facts: string[] }[];
  unsupportedLines: number[];
  structure: string[];
  words: number;
  parts: number;
  llmCalls: number;
  ttsCalls: number;
  repairs: number;
  audioChunks?: number;
  audioSeconds?: number;
  stories?: number;
  omittedStories?: number;
};
export type Source = { id: string; title: string; url: string; basis: 'article' | 'summary' };
export type PodcastState = {
  status: 'none' | 'processing' | 'ready' | 'failed';
  podcastId?: string; // persistent id, the page is /podcast/{podcastId}
  variant?: string | null; // set for a personalized podcast; sent back when polling it
  phase?: string | null;
  message?: string | null;
  title?: string | null;
  sectionName?: string | null;
  basis?: 'article' | 'summary' | 'mixed' | null;
  sources?: Source[] | null;
  audioUrl?: string;
  durationSec?: number | null;
  lines?: Line[] | null;
  takeaways?: string[] | null;
  report?: Report | null;
};

export const MAX_PREFERENCE_CHARS = 1000; // keep in step with the backend limit
const POLL_MS = 4000;
const GIVE_UP_MS = 15 * 60 * 1000;

const clock = (sec: number) => `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, '0')}`;

// The TLDR podcast endpoints are public, so these use plain fetch (the shared api() helper needs a signed-in user).
async function bearer(): Promise<Record<string, string>> {
  // A signed-in user is recognised (their verified learning profile shapes the podcast); everyone else stays anonymous.
  try {
    const { auth } = await import('../lib/firebase');
    const token = await auth.currentUser?.getIdToken();
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch {
    return {};
  }
}

async function call(path: string, init?: RequestInit): Promise<PodcastState> {
  const res = await fetch(`/api/tldr/podcast${path}`, { ...init, headers: { ...(await bearer()), ...(init?.headers as Record<string, string>) } });
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new Error(body?.error?.message || `Request failed (${res.status})`);
  return body as PodcastState;
}

/** Script, completeness and call counts, available as soon as the script exists (before audio is made). */
function ScriptPanel({ state }: { state: PodcastState }) {
  const r = state.report;
  return (
    <details className="mt-2">
      <summary className="cursor-pointer text-caption font-medium text-accent">Script and coverage</summary>
      {r && (
        <div className="mt-2 text-caption text-ink-2">
          <p>
            Coverage: {r.status === 'complete' ? 'complete' : 'partial'} · {r.coveredPoints} of {r.expectedPoints} key points · {r.words} words
            · {r.parts} part{r.parts === 1 ? '' : 's'} · {r.llmCalls} AI calls · {r.ttsCalls} audio calls · {r.repairs} repair
            {r.repairs === 1 ? '' : 's'}
          </p>
          {r.missing.length > 0 && (
            <ul className="mt-1 list-disc pl-5">
              {r.missing.map((m) => (
                <li key={m.id}>
                  Missing: {m.topic} ({m.facts.join('; ')})
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      <div className="mt-2 space-y-2">
        {state.lines?.map((l, i) => (
          <p key={i} className="text-caption text-ink-2">
            <span className={l.role === 'question' ? 'font-semibold text-accent' : 'font-semibold'}>{l.speaker}:</span> {l.text}
          </p>
        ))}
        {state.takeaways?.length ? (
          <ul className="list-disc pl-5 text-caption text-ink-2">
            {state.takeaways.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
        ) : null}
      </div>
    </details>
  );
}

/** What the user sees for one podcast state; separate from the polling so it can be tested directly. */
export function PodcastView({ state }: { state: PodcastState }) {
  const hasScript = !!state.lines?.length;
  return (
    <>
      {state.status === 'processing' && state.phase === 'audio' && hasScript && (
        <p className="mt-2 text-caption text-ink-3">The script is ready. Now making the audio…</p>
      )}
      {state.status === 'failed' && (
        <div className="mt-2">
          <Banner tone="error">{state.message || 'The podcast could not be made.'}</Banner>
        </div>
      )}
      {state.status === 'ready' && state.audioUrl && (
        <div className="mt-2">
          <p className="text-caption font-semibold text-ink-3">
            {state.title}
            {state.durationSec ? ` · ${clock(state.durationSec)}` : ''}
          </p>
          {state.sources && state.sources.length > 0 && (
            <p className="mt-1 text-caption text-ink-3">
              Covers {state.sources.length === 1 ? '1 story' : `${state.sources.length} stories`}, in this order:{' '}
              {state.sources.map((x) => x.title).join(' · ')}
              {state.report?.omittedStories ? ` (${state.report.omittedStories} more not included)` : ''}
            </p>
          )}
          {state.basis === 'summary' && (
            <p className="mt-1 text-caption text-ink-3">The full articles were not available, so this uses TLDR's short summaries.</p>
          )}
          {state.basis === 'mixed' && (
            <p className="mt-1 text-caption text-ink-3">
              These articles could not be opened, so they use TLDR's short summary:{' '}
              {state.sources?.filter((x) => x.basis === 'summary').map((x) => x.title).join(' · ')}.
            </p>
          )}
          {state.report?.status === 'partial' && (
            <div className="mt-2">
              <Banner tone="warn">
                This podcast may not cover everything: {state.report.coveredPoints} of {state.report.expectedPoints} key points were
                confirmed. See the script and coverage below.
              </Banner>
            </div>
          )}
          <audio controls preload="metadata" src={state.audioUrl} className="mt-1 w-full" />
        </div>
      )}
      {hasScript && <ScriptPanel state={state} />}
    </>
  );
}

type Step = 'ask' | 'describe';

/** Opens the blank tab synchronously, inside the click, so browsers do not block it after the async work. */
function openBlankTab(): Window | null {
  try {
    const w = window.open('', '_blank');
    if (!w) return null;
    try {
      w.opener = null;
      w.document.title = 'Making your podcast…';
      w.document.body.textContent = 'Making your podcast… this page opens it as soon as it is ready.';
    } catch {
      /* a cross-origin or restricted tab is still usable for navigation */
    }
    return w;
  } catch {
    return null;
  }
}

/** Asks whether the listener wants a personalized podcast; YES reveals the preference text area. */
export function PreferenceDialog({
  step, value, busy, error, onChange, onYes, onNo, onSubmit, onClose,
}: {
  step: Step;
  value: string;
  busy: boolean;
  error: string | null;
  onChange: (v: string) => void;
  onYes: () => void;
  onNo: () => void;
  onSubmit: () => void;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && !busy && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [busy, onClose]);

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/30 sm:items-center sm:p-4" onClick={() => !busy && onClose()} role="presentation">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="podcast-pref-title"
        onClick={(e) => e.stopPropagation()}
        className="animate-sheet max-h-[90dvh] w-full max-w-lg overflow-y-auto rounded-t-[20px] border border-line bg-surface p-5 pb-[max(1.25rem,env(safe-area-inset-bottom))] sm:rounded-[20px]"
      >
        <h2 id="podcast-pref-title" className="text-section font-semibold">
          Would you like to go with some preference?
        </h2>
        {step === 'ask' ? (
          <>
            <div className="mt-4 flex gap-2">
              <Button onClick={onYes} className="flex-1">YES</Button>
              <Button variant="secondary" onClick={onNo} className="flex-1">NO</Button>
            </div>
            <div className="mt-2 flex justify-end">
              <Button variant="ghost" onClick={onClose} className="min-h-9">Cancel</Button>
            </div>
          </>
        ) : (
          <form
            className="mt-3"
            onSubmit={(e) => {
              e.preventDefault();
              onSubmit();
            }}
          >
            <label htmlFor="podcast-pref" className="text-caption font-semibold text-ink-2">Describe your preference</label>
            <textarea
              id="podcast-pref"
              value={value}
              onChange={(e) => onChange(e.target.value)}
              rows={5}
              maxLength={MAX_PREFERENCE_CHARS}
              disabled={busy}
              autoFocus
              placeholder="Tell us what you want to learn, which topics interest you, what questions you have, or which aspects of the subject you want the podcast to focus on..."
              className="mt-2 w-full rounded-xl border border-line bg-surface p-3 text-body"
            />
            <p className="mt-1 text-caption text-ink-3">{value.length}/{MAX_PREFERENCE_CHARS}</p>
            {error && (
              <div className="mt-2">
                <Banner tone="error">{error}</Banner>
              </div>
            )}
            <div className="mt-3 flex gap-2">
              <Button type="submit" busy={busy} className="flex-1">{busy ? 'Starting…' : 'Submit'}</Button>
              <Button type="button" variant="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

/** Button under a section: the AI reads every linked article in it and turns them into one question-and-answer podcast.
 *  The button only opens the preference dialog; generation starts on NO (default podcast) or on a valid Submit. */
export function TldrPodcast({ category, date, section, stories }: { category: string; date: string; section: string; stories?: number }) {
  const [state, setState] = useState<PodcastState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [step, setStep] = useState<Step | null>(null); // null = dialog closed
  const [prefs, setPrefs] = useState(''); // kept across failures so the listener can retry
  const [posting, setPosting] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [podcastId, setPodcastId] = useState<string | null>(null);
  const [tabOpened, setTabOpened] = useState(true); // false when the browser blocked the new tab: then a link is shown
  const timer = useRef<number>(0);
  const started = useRef(0);
  const variant = useRef<string | null>(null);
  const inFlight = useRef(false);
  const base = `?category=${encodeURIComponent(category)}&date=${encodeURIComponent(date)}&section=${encodeURIComponent(section)}`;
  const query = () => (variant.current ? `${base}&variant=${encodeURIComponent(variant.current)}` : base);

  const stop = () => window.clearTimeout(timer.current);
  useEffect(() => stop, []);

  const poll = async () => {
    try {
      const s = await call(query());
      setState(s);
      if (s.status === 'processing') {
        if (Date.now() - started.current > GIVE_UP_MS) return setError('This is taking too long. Try again in a few minutes.');
        timer.current = window.setTimeout(poll, POLL_MS);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not check the podcast');
    }
  };

  /** Starts generation; `preferences` null = the default podcast. `tab` is the blank tab opened inside the click: it is
   *  pointed at the persistent podcast page once the server accepted the request (that page shows progress, then plays). */
  const start = async (preferences: string | null, tab: Window | null): Promise<boolean> => {
    if (inFlight.current) return false; // no duplicate submissions
    inFlight.current = true;
    stop();
    setError(null);
    setDialogError(null);
    setPosting(true);
    started.current = Date.now();
    setTabOpened(!!tab);
    try {
      const s = await call('', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(preferences ? { category, date, section, user_preferences: preferences } : { category, date, section }),
      });
      variant.current = s.variant ?? null;
      setPodcastId(s.podcastId ?? null);
      if (tab && s.podcastId) {
        try {
          tab.location.href = podcastUrl(s.podcastId);
        } catch {
          setTabOpened(false);
        }
      }
      setState(s);
      if (s.status === 'processing') timer.current = window.setTimeout(poll, POLL_MS);
      return true;
    } catch (e) {
      try {
        tab?.close();
      } catch {
        /* already closed */
      }
      const msg = e instanceof Error ? e.message : 'Could not start the podcast';
      if (preferences) setDialogError(msg);
      else setError(msg);
      return false;
    } finally {
      inFlight.current = false;
      setPosting(false);
    }
  };

  const chooseNo = async () => {
    const tab = openBlankTab(); // synchronously, still inside the click
    setStep(null);
    await start(null, tab);
  };

  const submit = async () => {
    const text = prefs.trim();
    if (!text) return setDialogError('Please describe your preference, or choose No for the standard podcast.');
    const tab = openBlankTab(); // synchronously, still inside the click
    if (await start(text, tab)) setStep(null); // on failure the dialog stays open with the text intact
  };

  const close = () => {
    setStep(null);
    setDialogError(null);
  };

  const busy = posting || state?.status === 'processing';
  return (
    <div className="mt-2">
      {state?.status !== 'ready' && (
        <Button variant="secondary" busy={busy} onClick={() => setStep('ask')} className="w-full sm:w-auto">
          <Headphones className="size-4" aria-hidden />
          {busy ? 'Making your podcast… this takes a few minutes' : stories && stories > 1 ? `Make a podcast of this section (${stories} stories)` : 'Make a podcast of this section'}
        </Button>
      )}
      {step && (
        <PreferenceDialog
          step={step}
          value={prefs}
          busy={posting}
          error={dialogError}
          onChange={(v) => {
            setPrefs(v);
            setDialogError(null);
          }}
          onYes={() => setStep('describe')}
          onNo={chooseNo}
          onSubmit={submit}
          onClose={close}
        />
      )}
      {error && (
        <div className="mt-2">
          <Banner tone="error">{error}</Banner>
        </div>
      )}
      {podcastId && state && state.status !== 'none' && (!tabOpened || state.status === 'ready') && (
        <p className="mt-2 text-caption">
          <a href={podcastUrl(podcastId)} target="_blank" rel="noopener noreferrer" className="font-medium text-accent hover:underline">
            {state.status === 'ready' ? 'Open the podcast page' : 'Open the podcast page in a new tab'}
          </a>{' '}
          to listen, ask questions and test your understanding.
        </p>
      )}
      {state && <PodcastView state={state} />}
    </div>
  );
}
