import { useEffect, useRef, useState } from 'react';
import {
  saveAnswer, startAssessment, submitAssessment, type Assessment,
} from '../../lib/podcastApi';
import { CloseIcon, Modal } from './Modal';

export function QuizPrompt({ section, onNo, onYes }: { section: string; onNo: () => void; onYes: () => void }) {
  return (
    <Modal label="Take a quiz" width={480} align="top" paddingTop={120} padding="28px" onClose={onNo}>
      <h2 style={{ fontSize: 24, fontWeight: 800 }}>Want a quick quiz?</h2>
      <p style={{ margin: '10px 0 0', color: 'var(--text-2)', fontSize: 15, lineHeight: 1.55 }}>
        A few questions on {section}, about a minute. Your answers help Knowly pitch your next podcasts at your level.
      </p>
      <div style={{ marginTop: 24, display: 'flex', gap: 10, justifyContent: 'flex-end', flexWrap: 'wrap' }}>
        <button className="btn btn-ghost" onClick={onNo}>Not now</button>
        <button className="btn btn-primary" onClick={onYes}>Quiz me</button>
      </div>
    </Modal>
  );
}

const errText = (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback);

/**
 * The quiz of a podcast, from the existing assessment API. Answers are saved one by one; the whole quiz is graded when
 * the last answer is in, so right/wrong feedback and the learner-model update arrive together on the result screen.
 */
export function QuizFlow({ pid, section, onClose }: { pid: string; section: string; onClose: () => void }) {
  const [quiz, setQuiz] = useState<Assessment | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [index, setIndex] = useState(0);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const busyRef = useRef(false);

  useEffect(() => {
    let live = true;
    setError(null);
    startAssessment(pid)
      .then((a) => {
        if (!live) return;
        setQuiz(a);
        setAnswers(a.answers || {});
        setIndex(0);
      })
      .catch((e) => live && setError(errText(e, 'Could not make the quiz')));
    return () => {
      live = false;
    };
  }, [pid, attempt]);

  const retry = () => setAttempt((n) => n + 1);

  if (error || !quiz) {
    return (
      <Modal label="Quiz" width={640} align="top" paddingTop={64} padding="28px" onClose={onClose}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
          <p style={{ fontSize: 13, fontWeight: 700, color: 'var(--kn-muted)' }}>{section} · Quiz</p>
          <button className="btn btn-ghost icon-btn" aria-label="Close quiz" onClick={onClose}><CloseIcon /></button>
        </div>
        {error ? (
          <div role="alert" style={{ marginTop: 18 }}>
            <p style={{ fontSize: 15, color: 'var(--text-2)' }}>{error}</p>
            <div style={{ marginTop: 18, display: 'flex', justifyContent: 'flex-end' }}><button className="btn btn-primary" onClick={retry}>Try again</button></div>
          </div>
        ) : (
          <div role="status" style={{ padding: '48px 0', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 14 }}>
            <div className="spin" style={{ width: 40, height: 40, borderRadius: 999, border: '4px solid var(--accent-soft)', borderTopColor: 'var(--accent)' }} />
            <p style={{ fontSize: 15, color: 'var(--text-2)' }}>Writing your questions…</p>
          </div>
        )}
      </Modal>
    );
  }

  if (quiz.status === 'completed' && quiz.results) return <QuizResult quiz={quiz} section={section} onClose={onClose} />;

  const total = quiz.questions.length;
  const q = quiz.questions[index];
  const mcq = q.type === 'mcq';
  const value = answers[q.id] ?? '';
  const last = index === total - 1;

  const next = async () => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      await saveAnswer(quiz.id, q.id, value);
      if (!last) setIndex(index + 1);
      else setQuiz(await submitAssessment(quiz.id));
    } catch (e) {
      setError(errText(e, 'Something went wrong. Try again.'));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  return (
    <Modal label="Quiz" width={640} align="top" paddingTop={64} padding="28px" onClose={onClose}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
        <p style={{ fontSize: 13, fontWeight: 700, color: 'var(--kn-muted)' }}>{section} · Question {index + 1} of {total}</p>
        <button className="btn btn-ghost icon-btn" aria-label="Close quiz" onClick={onClose}><CloseIcon /></button>
      </div>
      <div style={{ display: 'flex', gap: 6, marginTop: 10 }}>
        {quiz.questions.map((x, i) => (
          <span key={x.id} style={{ flex: 1, height: 6, borderRadius: 999, background: i === index ? 'var(--accent)' : i < index ? 'var(--kn-accent-line)' : 'var(--border)' }} />
        ))}
      </div>
      <h2 style={{ margin: '18px 0 0', fontSize: 21, fontWeight: 800, lineHeight: 1.35 }}>{q.prompt}</h2>
      {mcq ? (
        <div role="radiogroup" aria-label="Answers" style={{ marginTop: 18, display: 'flex', flexDirection: 'column', gap: 10 }}>
          {q.options.map((o, i) => (
            <button key={o} className={value === String(i) ? 'opt picked' : 'opt'} role="radio" aria-checked={value === String(i)} disabled={busy} onClick={() => setAnswers({ ...answers, [q.id]: String(i) })}>
              <span style={{ flex: 'none', width: 28, height: 28, borderRadius: 999, border: '1.5px solid var(--kn-field-line)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 13, fontWeight: 700, background: 'var(--surface)' }}>{'ABCD'[i]}</span>
              <span style={{ flex: 1 }}>{o}</span>
            </button>
          ))}
        </div>
      ) : (
        <textarea className="field" rows={4} maxLength={2000} aria-label={`Answer to question ${index + 1}`} placeholder="Explain in your own words…" disabled={busy} value={value} onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })} style={{ marginTop: 18, resize: 'vertical' }} />
      )}
      {error && <p role="alert" style={{ margin: '14px 0 0', fontSize: 14, color: 'var(--notyet)' }}>{error}</p>}
      <div style={{ marginTop: 22, display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
        <button className="btn btn-primary" onClick={next} disabled={busy || !value.trim()}>
          {busy ? (last ? 'Checking your answers…' : 'Saving…') : last ? 'See my result' : 'Next question'}
        </button>
      </div>
    </Modal>
  );
}

function QuizResult({ quiz, section, onClose }: { quiz: Assessment; section: string; onClose: () => void }) {
  const results = quiz.results ?? [];
  const right = results.filter((r) => r.evaluation_status === 'demonstrated').length;
  return (
    <Modal label="Quiz result" width={560} align="top" paddingTop={64} padding="28px" onClose={onClose}>
      <p style={{ fontSize: 13, fontWeight: 700, color: 'var(--kn-muted)' }}>{section} · Quiz result</p>
      <h2 style={{ margin: '8px 0 0', fontSize: 28, fontWeight: 800 }}>{right} of {quiz.questions.length} right</h2>
      <p style={{ margin: '8px 0 0', color: 'var(--text-2)', fontSize: 15, lineHeight: 1.55 }}>
        Saved. Your next podcasts will skip what you already know and spend more time on the rest.
      </p>
      <div style={{ marginTop: 20, display: 'flex', flexDirection: 'column', gap: 8 }}>
        {quiz.questions.map((q) => {
          const r = results.find((x) => x.question_id === q.id);
          const known = r?.evaluation_status === 'demonstrated';
          return (
            <div key={q.id} style={{ padding: '12px 14px', border: '1px solid var(--border)', borderRadius: 12 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                <span style={{ fontSize: 15, fontWeight: 600 }}>{q.conceptNames.join(', ') || q.prompt}</span>
                <span style={{ flex: 'none', fontSize: 13, fontWeight: 700, padding: '4px 10px', borderRadius: 999, background: known ? 'var(--good-soft)' : 'var(--partly-soft)', color: known ? 'var(--good)' : 'var(--partly)' }}>
                  {known ? 'You know this' : 'Still learning'}
                </span>
              </div>
              {r?.feedback && <p style={{ margin: '6px 0 0', fontSize: 13, lineHeight: 1.5, color: 'var(--text-2)' }}>{r.feedback}</p>}
            </div>
          );
        })}
      </div>
      <div style={{ marginTop: 24, display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
        <button className="btn btn-primary" onClick={onClose}>Back to today’s TLDR</button>
      </div>
    </Modal>
  );
}
