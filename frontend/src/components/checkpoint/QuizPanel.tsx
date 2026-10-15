import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, post } from '../../lib/api';
import { useSpeechInput } from '../../lib/speech';
import type { Citation } from '../../lib/types';
import { SourceChip } from '../SourceChip';
import { SpeakButton } from '../SpeakButton';
import { Banner, Button, GradeBadge, MasteryBar, Spinner } from '../ui';

interface Start {
  quizId: string;
  question: string;
  conceptName: string;
}
interface Result {
  grade: 'correct' | 'partly' | 'not_yet';
  feedback: string;
  correctAnswer: string;
  missingPoints: string[];
  citations: Citation[];
  masteryBefore: number;
  masteryAfter: number;
  nextDueAt: string;
  conceptName: string;
}

/** Recall quiz (section 4.4): ask before telling; hint first, answer only after an attempt. */
export function QuizPanel({ briefingId, segId, onDone }: { briefingId: string; segId: string; onDone: (r: Result | null) => void }) {
  const [quiz, setQuiz] = useState<Start | null>(null);
  const [answer, setAnswer] = useState('');
  const [hint, setHint] = useState<string | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const started = useRef(false);
  const onSpeech = useCallback((t: string) => setAnswer(t), []);
  const speech = useSpeechInput(onSpeech);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    post<Start>('/quiz/start', { briefingId, segId })
      .then(setQuiz)
      .catch((e: Error) => setError(e.message));
  }, [briefingId, segId]);

  const submit = async (dontKnow: boolean) => {
    if (!quiz) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await post<Result>('/quiz/answer', { quizId: quiz.quizId, answer: dontKnow ? null : answer, dontKnow }));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not grade your answer');
    } finally {
      setBusy(false);
    }
  };

  if (error && !quiz) return <Banner tone="error">{error}</Banner>;
  if (!quiz) return <Spinner label="Writing a question" />;

  if (result) {
    const tone = result.grade === 'correct' ? 'border-good/40 bg-good-soft' : result.grade === 'partly' ? 'border-partly/40 bg-partly-soft' : 'border-notyet/40 bg-notyet-soft';
    return (
      <div className="space-y-4">
        <div className={`space-y-2 rounded-xl border p-4 ${tone}`}>
          <GradeBadge grade={result.grade} />
          <p className="text-body">{result.feedback}</p>
          <SpeakButton path={`/quiz/${quiz.quizId}/audio`} body={{ part: 'feedback' }} autoPlay label="Listen" />
          {result.grade !== 'correct' && (
            <p className="text-body">
              <span className="font-semibold">Answer: </span>
              {result.correctAnswer}
            </p>
          )}
        </div>
        <div>
          <p className="mb-1 text-caption text-ink-3">What Knowly thinks you know about {result.conceptName}</p>
          <MasteryBar value={result.masteryAfter} label={result.conceptName} />
          <p className="mt-1 text-caption text-ink-3">
            {Math.round(result.masteryBefore * 100)}% → {Math.round(result.masteryAfter * 100)}% · next review {new Date(result.nextDueAt).toLocaleDateString()}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {result.citations.map((c) => (
            <SourceChip key={c.itemId} newsletter={c.newsletter} headline={c.headline} url={c.url} itemId={c.itemId} briefingId={briefingId} segId={segId} />
          ))}
        </div>
        <Button full onClick={() => onDone(result)}>
          Continue
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <p className="text-caption text-ink-3">Quick check · {quiz.conceptName}</p>
      <p className="text-section font-semibold">{quiz.question}</p>
      <SpeakButton path={`/quiz/${quiz.quizId}/audio`} body={{ part: 'question' }} autoPlay label="Hear the question" />
      <label className="sr-only" htmlFor="quiz-answer">
        Your answer
      </label>
      <textarea
        id="quiz-answer"
        value={answer}
        onChange={(e) => setAnswer(e.target.value)}
        rows={3}
        maxLength={2000}
        placeholder="Answer in your own words…"
        className="w-full rounded-xl border border-line bg-surface p-3 text-body outline-none focus:border-accent"
      />
      {hint && <Banner tone="info">Hint: {hint}</Banner>}
      {error && <Banner tone="error">{error}</Banner>}
      <div className="flex flex-wrap gap-2">
        <Button busy={busy} disabled={answer.trim().length < 2} onClick={() => submit(false)}>
          Check my answer
        </Button>
        {speech.supported && (
          <Button variant="secondary" onClick={() => (speech.listening ? speech.stop() : speech.start())}>
            {speech.listening ? 'Stop' : 'Answer by voice'}
          </Button>
        )}
        {!hint && (
          <Button variant="secondary" onClick={async () => setHint((await post<{ hint: string }>('/quiz/hint', { quizId: quiz.quizId })).hint)}>
            Hint
          </Button>
        )}
        <Button variant="ghost" disabled={busy} onClick={() => submit(true)}>
          I don't know
        </Button>
      </div>
    </div>
  );
}
