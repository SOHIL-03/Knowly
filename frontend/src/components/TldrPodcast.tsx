import { Headphones } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
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
};
export type PodcastState = {
  status: 'none' | 'processing' | 'ready' | 'failed';
  phase?: string | null;
  message?: string | null;
  title?: string | null;
  basis?: 'article' | 'summary' | null;
  audioUrl?: string;
  durationSec?: number | null;
  lines?: Line[] | null;
  takeaways?: string[] | null;
  report?: Report | null;
};

const POLL_MS = 4000;
const GIVE_UP_MS = 15 * 60 * 1000;

const clock = (sec: number) => `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, '0')}`;

// The TLDR podcast endpoints are public, so these use plain fetch (the shared api() helper needs a signed-in user).
async function call(path: string, init?: RequestInit): Promise<PodcastState> {
  const res = await fetch(`/api/tldr/podcast${path}`, init);
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
          {state.basis === 'summary' && (
            <p className="mt-1 text-caption text-ink-3">The full article was not available, so this uses TLDR's short summary.</p>
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

/** Button under a story: the AI reads the whole linked article and turns it into a question-and-answer podcast. */
export function TldrPodcast({ category, date, story }: { category: string; date: string; story: string }) {
  const [state, setState] = useState<PodcastState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number>(0);
  const started = useRef(0);
  const query = `?category=${encodeURIComponent(category)}&date=${encodeURIComponent(date)}&story=${encodeURIComponent(story)}`;

  const stop = () => window.clearTimeout(timer.current);
  useEffect(() => stop, []);

  const poll = async () => {
    try {
      const s = await call(query);
      setState(s);
      if (s.status === 'processing') {
        if (Date.now() - started.current > GIVE_UP_MS) return setError('This is taking too long. Try again in a few minutes.');
        timer.current = window.setTimeout(poll, POLL_MS);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not check the podcast');
    }
  };

  const start = async () => {
    stop();
    setError(null);
    started.current = Date.now();
    setState({ status: 'processing' });
    try {
      const s = await call('', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category, date, story }),
      });
      setState(s);
      if (s.status === 'processing') timer.current = window.setTimeout(poll, POLL_MS);
    } catch (e) {
      setState(null);
      setError(e instanceof Error ? e.message : 'Could not start the podcast');
    }
  };

  const busy = state?.status === 'processing';
  return (
    <div className="mt-2">
      {state?.status !== 'ready' && (
        <Button variant="secondary" busy={busy} onClick={start} className="w-full sm:w-auto">
          <Headphones className="size-4" aria-hidden />
          {busy ? 'Making your podcast… this takes a few minutes' : 'Make a podcast of this article'}
        </Button>
      )}
      {error && (
        <div className="mt-2">
          <Banner tone="error">{error}</Banner>
        </div>
      )}
      {state && <PodcastView state={state} />}
    </div>
  );
}
