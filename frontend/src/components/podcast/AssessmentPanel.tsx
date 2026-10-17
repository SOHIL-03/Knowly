import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../../lib/api';
import {
  learningProfile, listAssessments, saveAnswer, startAssessment, submitAssessment,
  type Assessment, type EvalStatus, type LearningProfile, type MasteryStatus,
} from '../../lib/podcastApi';
import { Banner, Button, Card } from '../ui';

const errText = (e: unknown, fallback: string) => (e instanceof ApiError || e instanceof Error ? e.message : fallback);

export const EVAL_LABEL: Record<EvalStatus, string> = {
  demonstrated: 'Shows understanding',
  partial: 'Partly there',
  incorrect: 'Not yet correct',
  insufficient: 'Not enough to tell',
  not_assessed: 'Skipped, does not count',
};
export const MASTERY_LABEL: Record<MasteryStatus, string> = {
  unknown: 'Not yet shown',
  learning: 'Learning',
  understood: 'Understood',
  needs_review: 'Needs review',
};

/** Test Your Understanding: only your answers here change what the app believes you understand. */
export function AssessmentPanel({ pid }: { pid: string }) {
  const [a, setA] = useState<Assessment | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [profile, setProfile] = useState<LearningProfile | null>(null);
  const timers = useRef<Record<string, number>>({});
  const pending = useRef<Set<Promise<unknown>>>(new Set());
  const inFlight = useRef(false);

  const adopt = useCallback((next: Assessment) => {
    setA(next);
    setAnswers(next.answers || {});
  }, []);

  useEffect(() => {
    let live = true;
    listAssessments(pid)
      .then((all) => live && all[0] && adopt(all[0]))
      .catch((e) => live && setError(errText(e, 'Could not load your assessment')))
      .finally(() => live && setLoaded(true));
    return () => {
      live = false;
      Object.values(timers.current).forEach((t) => window.clearTimeout(t));
    };
  }, [pid, adopt]);

  useEffect(() => {
    if (a?.status === 'completed') learningProfile().then(setProfile).catch(() => {});
  }, [a?.status, a?.id]);

  const run = async (fn: () => Promise<Assessment>) => {
    if (inFlight.current) return; // no duplicate requests
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      adopt(await fn());
    } catch (e) {
      setError(errText(e, 'Something went wrong. Try again.'));
      if (a) listAssessments(pid).then((all) => all[0] && adopt(all[0])).catch(() => {}); // e.g. a failed evaluation leaves the saved answers
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  const save = (qid: string, text: string) => {
    if (!a) return;
    const p = saveAnswer(a.id, qid, text).catch((e) => setError(errText(e, 'Your answer could not be saved.')));
    pending.current.add(p);
    void p.finally(() => pending.current.delete(p));
  };

  const change = (qid: string, text: string, immediate: boolean) => {
    setAnswers((s) => ({ ...s, [qid]: text }));
    window.clearTimeout(timers.current[qid]);
    if (immediate) save(qid, text);
    else timers.current[qid] = window.setTimeout(() => save(qid, text), 700);
  };

  const submit = () =>
    run(async () => {
      for (const [qid, t] of Object.entries(timers.current)) {
        window.clearTimeout(t);
        delete timers.current[qid];
        save(qid, answers[qid] ?? ''); // make sure the latest text is stored before it is graded
      }
      await Promise.all([...pending.current]);
      return submitAssessment(a!.id);
    });

  if (!loaded) return <Card><p className="text-caption text-ink-3">Loading…</p></Card>;

  const answered = a ? a.questions.filter((q) => (answers[q.id] ?? '').trim()).length : 0;
  const open = a && a.status !== 'completed';

  return (
    <Card as="section">
      <h2 className="text-section font-semibold">Test Your Understanding</h2>
      <p className="mt-1 text-caption text-ink-3">
        Listening, reading or asking questions never counts as understanding. Only your answers here do, and skipped questions count for nothing.
      </p>
      {error && <div className="mt-3"><Banner tone="error">{error}</Banner></div>}

      {!a && (
        <Button className="mt-3" busy={busy} onClick={() => run(() => startAssessment(pid))}>
          Test Your Understanding
        </Button>
      )}

      {a && open && (
        <div className="mt-3 space-y-4">
          <p className="text-caption text-ink-2" role="status">
            {answered} of {a.questions.length} answered{a.status === 'failed' ? ' · your answers are saved' : ''}. You can stop and come back later.
          </p>
          {a.status === 'failed' && a.message && <Banner tone="warn">{a.message}</Banner>}
          {a.questions.map((q, i) => (
            <fieldset key={q.id} className="space-y-2">
              <legend className="text-body font-medium">{i + 1}. {q.prompt}</legend>
              <p className="text-caption text-ink-3">{q.conceptNames.join(', ')}</p>
              {q.type === 'mcq' ? (
                q.options.map((o, n) => (
                  <label key={o} className="flex items-center gap-2 text-body">
                    <input type="radio" name={q.id} checked={answers[q.id] === String(n)} disabled={busy} onChange={() => change(q.id, String(n), true)} /> {o}
                  </label>
                ))
              ) : (
                <textarea
                  aria-label={`Answer to question ${i + 1}`}
                  rows={3}
                  maxLength={2000}
                  value={answers[q.id] ?? ''}
                  disabled={busy}
                  onChange={(e) => change(q.id, e.target.value, false)}
                  placeholder="Explain in your own words…"
                  className="w-full rounded-xl border border-line bg-surface p-2 text-body"
                />
              )}
            </fieldset>
          ))}
          <Button busy={busy} disabled={answered === 0} onClick={submit}>
            {busy ? 'Checking your answers…' : a.status === 'failed' ? 'Submit again' : 'Submit answers'}
          </Button>
        </div>
      )}

      {a?.status === 'completed' && a.results && (
        <div className="mt-3 space-y-4">
          <p className="text-body font-medium" role="status">
            Score {Math.round((a.score ?? 0) * 100)}% · attempt {a.attempt}
          </p>
          {a.questions.map((q, i) => {
            const r = a.results!.find((x) => x.question_id === q.id);
            if (!r) return null;
            return (
              <div key={q.id} className="rounded-xl border border-line p-3">
                <p className="text-body font-medium">{i + 1}. {q.prompt}</p>
                <p className="mt-1 text-caption font-semibold text-accent">{EVAL_LABEL[r.evaluation_status]}</p>
                {r.answer && <p className="mt-1 text-caption text-ink-2">Your answer: {r.answer}</p>}
                <p className="mt-1 text-caption text-ink-2">{r.feedback}</p>
                {Object.values(r.mastery ?? {}).some((m) => m?.after) && (
                  <p className="mt-1 text-caption text-ink-3">
                    {q.conceptNames.map((n, k) => {
                      const m = r.mastery?.[Object.keys(r.mastery ?? {})[k]];
                      return m?.after ? `${n}: ${MASTERY_LABEL[m.after as MasteryStatus] ?? m.after}` : null;
                    }).filter(Boolean).join(' · ')}
                  </p>
                )}
              </div>
            );
          })}
          {profile && profile.concepts.length > 0 && (
            <div>
              <h3 className="text-body font-semibold">Your saved progress</h3>
              <ul className="mt-1 space-y-1 text-caption text-ink-2">
                {profile.concepts.map((c) => (
                  <li key={c.conceptId}>{c.name}: {MASTERY_LABEL[c.status]} ({c.correctEvidenceCount} shown, {c.incorrectEvidenceCount} missed)</li>
                ))}
              </ul>
            </div>
          )}
          <Button variant="secondary" busy={busy} onClick={() => run(() => startAssessment(pid, true))}>Try again with new questions</Button>
        </div>
      )}
    </Card>
  );
}
