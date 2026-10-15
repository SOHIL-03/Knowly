import { Check, Circle, Loader2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { Banner, Button, Card } from '../components/ui';
import { post } from '../lib/api';
import { useBriefingProgress } from '../lib/live';

const ORDER = ['queued', 'fetch', 'parse', 'enrich', 'rank', 'plan', 'brief', 'ready'];

/** Live checklist from the briefing document's progress map (Firestore listener, no polling). */
export function Generating() {
  const { briefingId = '' } = useParams();
  const { user } = useAuth();
  const navigate = useNavigate();
  const live = useBriefingProgress(user?.uid, briefingId);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (live?.status === 'ready') {
      const t = setTimeout(() => navigate(`/today`), 1200);
      return () => clearTimeout(t);
    }
  }, [live?.status, navigate]);

  const p = live?.progress;
  const stage = ORDER.indexOf(p?.stage || 'queued');
  const steps = [
    { at: 'fetch', label: p?.issues != null ? `Reading ${p.issues} newsletters` : 'Reading your newsletters' },
    { at: 'parse', label: p?.storiesFound != null ? `Found ${p.storiesFound} stories` : 'Finding the stories' },
    { at: 'plan', label: p?.storiesPicked != null ? `Picked ${p.storiesPicked} for you` : 'Picking what matters to you' },
    { at: 'brief', label: 'Writing your briefing' },
  ];

  const retry = async (kind: 'daily' | 'review') => {
    setBusy(true);
    try {
      const r = await post<{ briefingId: string }>('/briefings/generate', { kind, regenerate: true });
      navigate(`/generating/${encodeURIComponent(r.briefingId)}`, { replace: true });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto flex min-h-dvh max-w-md flex-col justify-center px-4">
      <h1 className="text-title font-bold">Making your briefing</h1>
      <p className="mt-1 text-body text-ink-2">The text is ready first; audio follows per segment.</p>
      <Card className="mt-6">
        <ol className="space-y-4" aria-live="polite">
          {steps.map((s) => {
            const idx = ORDER.indexOf(s.at);
            const done = live?.status === 'ready' || stage > idx;
            const active = !done && stage >= idx - 1 && live?.status === 'generating';
            return (
              <li key={s.at} className="flex items-center gap-3">
                {done ? (
                  <Check className="size-5 text-good" aria-label="done" />
                ) : active ? (
                  <Loader2 className="size-5 animate-spin text-accent" aria-label="in progress" />
                ) : (
                  <Circle className="size-5 text-line" aria-label="waiting" />
                )}
                <span className={`text-body ${done || active ? 'text-ink' : 'text-ink-3'}`}>{s.label}</span>
              </li>
            );
          })}
        </ol>
      </Card>
      <div className="mt-6">
        {live?.status === 'ready' && <Banner tone="good">Your briefing is ready.</Banner>}
        {live?.status === 'failed' && (
          <Banner tone="error" action={<Button variant="secondary" busy={busy} onClick={() => retry('daily')}>Try again</Button>}>
            {live.error?.message || 'Something went wrong.'}
          </Banner>
        )}
        {live?.status === 'empty' && (
          <Card>
            <p className="text-body">{p?.message === 'No new issues today' ? "No new issues today. Want a review of this week's saved items?" : p?.message}</p>
            <div className="mt-3 flex gap-2">
              <Button busy={busy} onClick={() => retry('review')}>
                Review saved items
              </Button>
              <Button variant="secondary" onClick={() => navigate('/today')}>
                Not now
              </Button>
            </div>
          </Card>
        )}
      </div>
    </div>
  );
}
