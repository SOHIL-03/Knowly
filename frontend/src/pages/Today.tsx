import { ArrowDown, ArrowUp, Headphones, Play, RefreshCw, Sparkles, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { AppShell } from '../components/AppShell';
import { Banner, Button, Card, Skeleton } from '../components/ui';
import { ApiError, get, patch, post } from '../lib/api';
import { track } from '../lib/events';
import { useBriefing } from '../lib/live';
import { minutes } from '../lib/transcript';
import type { Briefing, Segment } from '../lib/types';

function greeting() {
  const h = new Date().getHours();
  return h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
}

const audioLabel: Record<Segment['audioStatus'], string> = {
  ok: 'Audio ready',
  pending: 'Audio on its way',
  processing: 'Making audio',
  failed: 'Text only',
};

export function Today() {
  const { user, me } = useAuth();
  const navigate = useNavigate();
  const [latestId, setLatestId] = useState<string | null | undefined>(undefined);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const { briefing, reload } = useBriefing(user?.uid, latestId || undefined);

  useEffect(() => {
    get<Briefing>('/briefings/today')
      .then((b) => setLatestId(b.id))
      .catch((e) => {
        if (e instanceof ApiError && e.status === 404) setLatestId(null);
        else setStartError(e.message);
      });
  }, []);

  const generate = async (kind: 'daily' | 'review' = 'daily', regenerate = false) => {
    setStarting(true);
    setStartError(null);
    try {
      const r = await post<{ briefingId: string }>('/briefings/generate', { kind, regenerate });
      navigate(`/generating/${encodeURIComponent(r.briefingId)}`);
    } catch (e) {
      setStartError(e instanceof Error ? e.message : 'Could not start a briefing');
    } finally {
      setStarting(false);
    }
  };

  const visible = (briefing?.segments || []).filter((s) => !s.removedByUser).sort((a, b) => a.order - b.order);
  const removed = (briefing?.segments || []).filter((s) => s.removedByUser);
  const stories = visible.reduce((n, s) => n + s.itemIds.length, 0);
  const total = visible.reduce((n, s) => n + (s.durationSec || 0), 0);

  const saveOrder = async (ids: string[], removedIds: string[]) => {
    await patch(`/briefings/${encodeURIComponent(briefing!.id)}/segments`, { order: ids, removed: removedIds });
    void reload();
  };
  const move = (i: number, d: -1 | 1) => {
    const ids = visible.map((s) => s.id);
    const j = i + d;
    if (j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    void saveOrder(ids, removed.map((s) => s.id));
  };
  const remove = (s: Segment) => {
    track({ type: 'cardRemoved', briefingId: briefing!.id, segId: s.id, itemId: s.itemIds[0] });
    void saveOrder(visible.filter((x) => x.id !== s.id).map((x) => x.id), [...removed.map((x) => x.id), s.id]);
  };

  const name = me?.anonymous ? '' : (user?.displayName || '').split(' ')[0];
  const date = new Date().toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' });

  return (
    <AppShell>
      <header className="mb-6">
        <h1 className="text-title font-bold">
          {greeting()}
          {name ? `, ${name}` : ''}
        </h1>
        <p className="mt-1 text-body text-ink-2">
          {date}
          {briefing?.status === 'ready' && ` · ${minutes(total)} · ${stories} stories`}
        </p>
      </header>

      {startError && (
        <div className="mb-4">
          <Banner tone="error">{startError}</Banner>
        </div>
      )}

      {latestId === undefined || (latestId && !briefing) ? (
        <div className="space-y-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
      ) : latestId === null ? (
        <Card className="text-center">
          <Headphones className="mx-auto size-10 text-accent" aria-hidden />
          <h2 className="mt-3 text-section font-semibold">No briefing yet</h2>
          <p className="mt-1 text-body text-ink-2">
            {me?.mode === 'sample' ? 'Make one from the three sample newsletters.' : 'Make one from your newsletters now, or wait for your scheduled time.'}
          </p>
          <Button className="mt-4" busy={starting} onClick={() => generate()}>
            Make my briefing
          </Button>
        </Card>
      ) : briefing?.status === 'generating' ? (
        <Banner tone="info" action={<Button variant="secondary" onClick={() => navigate(`/generating/${encodeURIComponent(briefing.id)}`)}>Show progress</Button>}>
          {briefing.progress?.message || 'Your briefing is being made'}
        </Banner>
      ) : briefing?.status === 'failed' ? (
        <Banner tone="error" action={<Button variant="secondary" busy={starting} onClick={() => generate('daily', true)}>Try again</Button>}>
          {briefing.error?.message || 'The briefing could not be made.'}
        </Banner>
      ) : briefing?.status === 'empty' ? (
        <Card>
          <h2 className="text-section font-semibold">No new issues today</h2>
          <p className="mt-1 text-body text-ink-2">{briefing.progress?.message === 'No new issues today' ? "Want a review of this week's saved items?" : briefing.progress?.message}</p>
          <Button className="mt-4" busy={starting} onClick={() => generate('review', true)}>
            Review my saved items
          </Button>
        </Card>
      ) : briefing ? (
        <>
          <ol className="space-y-3">
            {visible.map((s, i) => (
              <li key={s.id}>
                <Card className="flex gap-3">
                  <Link to={`/play/${encodeURIComponent(briefing.id)}?seg=${s.id}`} className="min-w-0 flex-1">
                    <p className="text-section font-semibold">{s.title}</p>
                    <p className="mt-0.5 text-caption text-ink-3">
                      {minutes(s.durationSec)} · {s.type === 'review' ? 'review' : `${s.itemIds.length} ${s.itemIds.length === 1 ? 'story' : 'stories'}`} · {audioLabel[s.audioStatus]}
                    </p>
                    <p className="mt-1 text-caption italic text-accent">{s.whyChosen}</p>
                    {s.wildcardItemId && (
                      <p className="mt-1 inline-flex items-center gap-1 text-caption text-ink-2">
                        <Sparkles className="size-3.5" aria-hidden /> Includes 1 wildcard
                      </p>
                    )}
                  </Link>
                  <div className="flex shrink-0 flex-col items-center">
                    <button onClick={() => move(i, -1)} disabled={i === 0} className="flex size-9 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2 disabled:opacity-30" aria-label={`Move ${s.title} up`}>
                      <ArrowUp className="size-4" />
                    </button>
                    <button onClick={() => remove(s)} className="flex size-9 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2" aria-label={`Remove ${s.title}`}>
                      <X className="size-4" />
                    </button>
                    <button onClick={() => move(i, 1)} disabled={i === visible.length - 1} className="flex size-9 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2 disabled:opacity-30" aria-label={`Move ${s.title} down`}>
                      <ArrowDown className="size-4" />
                    </button>
                  </div>
                </Card>
              </li>
            ))}
          </ol>

          {removed.length > 0 && (
            <p className="mt-3 text-caption text-ink-3">
              {removed.length} removed ·{' '}
              <button className="underline" onClick={() => void saveOrder([...visible, ...removed].map((s) => s.id), [])}>
                restore
              </button>
            </p>
          )}

          {briefing.alsoInInbox && briefing.alsoInInbox.length > 0 && (
            <section className="mt-8">
              <h2 className="text-section font-semibold">Also in your inbox</h2>
              <p className="text-caption text-ink-3">Did not fit in {minutes(total)}. Text only.</p>
              <ul className="mt-3 divide-y divide-line rounded-[var(--radius-card)] border border-line bg-surface">
                {briefing.alsoInInbox.map((a) => (
                  <li key={a.itemId} className="px-4 py-3">
                    {a.url ? (
                      <a href={a.url} target="_blank" rel="noopener noreferrer" className="text-body hover:text-accent" onClick={() => track({ type: 'openSource', briefingId: briefing.id, itemId: a.itemId })}>
                        {a.headline}
                      </a>
                    ) : (
                      <span className="text-body">{a.headline}</span>
                    )}
                    <p className="text-caption text-ink-3">{a.senderName}</p>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <div className="mt-6 flex justify-center">
            <Button variant="ghost" busy={starting} onClick={() => generate('daily', true)}>
              <RefreshCw className="size-4" aria-hidden /> Make a new briefing
            </Button>
          </div>

          <div className="fixed inset-x-0 bottom-16 z-20 px-4 md:static md:mt-6 md:px-0">
            <Button full className="mx-auto max-w-xl shadow-lg md:shadow-none" disabled={!visible.length} onClick={() => navigate(`/play/${encodeURIComponent(briefing.id)}`)}>
              <Play className="size-5" fill="currentColor" aria-hidden /> Play all
            </Button>
          </div>
        </>
      ) : null}
    </AppShell>
  );
}
