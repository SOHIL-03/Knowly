import { ThumbsDown, ThumbsUp } from 'lucide-react';
import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { QuizPanel } from '../components/checkpoint/QuizPanel';
import { Banner, Button, Card, ScoreRing, Spinner } from '../components/ui';
import { post } from '../lib/api';
import { useBriefing } from '../lib/live';

/** End-of-briefing recap: takeaways, quiz score, saved items, rating, optional 2-question closing round. */
export function Recap() {
  const { briefingId = '' } = useParams();
  const { user, me } = useAuth();
  const navigate = useNavigate();
  const { briefing, reload } = useBriefing(user?.uid, briefingId);
  const [rating, setRating] = useState<'up' | 'down' | null>(null);
  const [reason, setReason] = useState('');
  const [sent, setSent] = useState(false);
  const [round, setRound] = useState<number | null>(null);

  if (!briefing) return <div className="flex min-h-dvh items-center justify-center"><Spinner /></div>;

  const recap = briefing.recap;
  const segs = briefing.segments.filter((s) => !s.removedByUser && s.type !== 'review').slice(0, 2);

  const rate = async (r: 'up' | 'down', why?: string) => {
    setRating(r);
    await post(`/briefings/${encodeURIComponent(briefingId)}/rating`, { rating: r, reason: why || null });
    if (r === 'up' || why) setSent(true);
  };

  return (
    <div className="mx-auto max-w-xl px-4 py-8">
      <h1 className="text-title font-bold">That's your briefing</h1>

      {round !== null && segs[round] ? (
        <Card className="mt-6">
          <p className="mb-3 text-caption text-ink-3">Closing round · question {round + 1} of {segs.length}</p>
          <QuizPanel
            key={segs[round].id}
            briefingId={briefingId}
            segId={segs[round].id}
            onDone={() => {
              void reload();
              setRound(round + 1 < segs.length ? round + 1 : null);
            }}
          />
        </Card>
      ) : (
        <>
          <Card className="mt-6">
            <h2 className="text-section font-semibold">Three takeaways</h2>
            <ol className="mt-3 list-decimal space-y-2 pl-5 text-body">
              {(recap?.takeaways || []).map((t, i) => (
                <li key={i}>{t}</li>
              ))}
            </ol>
          </Card>

          <div className="mt-4 grid grid-cols-2 gap-3">
            <Card className="flex flex-col items-center">
              {recap?.quizScore ? <ScoreRing points={recap.quizScore.points} total={recap.quizScore.total} /> : <p className="py-6 text-center text-caption text-ink-3">No quiz yet</p>}
              <p className="mt-2 text-caption text-ink-2">Quiz score</p>
            </Card>
            <Card className="flex flex-col items-center justify-center">
              <p className="text-title font-bold tabular-nums">{recap?.savedCount ?? 0}</p>
              <p className="text-caption text-ink-2">saved</p>
              <Link to="/library" className="mt-1 text-caption text-accent underline">
                Open Library
              </Link>
            </Card>
          </div>

          {me?.prefs?.endQuizOn && segs.length > 0 && (
            <Button variant="secondary" full className="mt-4" onClick={() => setRound(0)}>
              Take the 2-question closing round
            </Button>
          )}
        </>
      )}

      <Card className="mt-6">
        <p className="text-body font-medium">How was today's briefing?</p>
        <div className="mt-3 flex gap-2">
          <Button variant={rating === 'up' ? 'primary' : 'secondary'} onClick={() => void rate('up')} aria-pressed={rating === 'up'}>
            <ThumbsUp className="size-4" aria-hidden /> Good
          </Button>
          <Button variant={rating === 'down' ? 'primary' : 'secondary'} onClick={() => void rate('down')} aria-pressed={rating === 'down'}>
            <ThumbsDown className="size-4" aria-hidden /> Not great
          </Button>
        </div>
        {rating === 'down' && !sent && (
          <form
            className="mt-3 flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              void rate('down', reason);
            }}
          >
            <input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} placeholder="What should change? (optional)" className="min-h-11 flex-1 rounded-xl border border-line bg-surface px-3 text-body" />
            <Button type="submit">Send</Button>
          </form>
        )}
        {sent && <div className="mt-3"><Banner tone="good">Thanks. This shapes tomorrow's briefing.</Banner></div>}
      </Card>

      <Button full className="mt-6" onClick={() => navigate('/today')}>
        Back to Today
      </Button>
    </div>
  );
}
