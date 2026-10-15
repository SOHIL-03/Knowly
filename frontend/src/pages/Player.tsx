import { ChevronLeft, Pause, Play, RotateCcw, SkipForward, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { AudioPanel } from '../components/AudioPanel';
import { AskPanel } from '../components/checkpoint/AskPanel';
import { CheckpointSheet, type CheckpointAction } from '../components/checkpoint/CheckpointSheet';
import { QuizPanel } from '../components/checkpoint/QuizPanel';
import { ApplyPanel, RephrasePanel } from '../components/checkpoint/SmallPanels';
import { SourceChip } from '../components/SourceChip';
import { Banner, Button, Spinner } from '../components/ui';
import { post } from '../lib/api';
import { flush, track } from '../lib/events';
import { useBriefing } from '../lib/live';
import { formatTime, lineAt } from '../lib/transcript';
import type { Segment } from '../lib/types';

const SPEEDS = [0.75, 1, 1.25, 1.5, 2];

type Panel = { kind: 'ask'; question: string } | { kind: 'quiz' } | { kind: 'simpler' } | { kind: 'apply' } | { kind: 'notice'; text: string };

export function Player() {
  const { briefingId = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const { user, me } = useAuth();
  const { briefing, error, reload } = useBriefing(user?.uid, briefingId);

  const playlist = useMemo(
    () => (briefing?.segments || []).filter((s) => !s.removedByUser && s.status !== 'failed').sort((a, b) => a.order - b.order),
    [briefing],
  );
  const segId = params.get('seg') || playlist[0]?.id;
  const index = Math.max(0, playlist.findIndex((s) => s.id === segId));
  const seg: Segment | undefined = playlist[index];

  const audio = useRef<HTMLAudioElement>(null);
  const prefetch = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [speed, setSpeed] = useState(me?.prefs?.speed || 1);
  const [checkpoint, setCheckpoint] = useState(false);
  const [panel, setPanel] = useState<Panel | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const autoplay = useRef(false);

  const hasAudio = Boolean(seg?.audioUrl && seg.audioStatus === 'ok');
  // Signed audio URLs change on every briefing reload (live updates while other segments finish).
  // Keep the first URL per segment so playback is not restarted; swap only if it expires and fails.
  const stableSrc = useRef<Record<string, string>>({});
  if (seg?.audioUrl && hasAudio && !stableSrc.current[seg.id]) stableSrc.current[seg.id] = seg.audioUrl;
  const audioSrc = seg ? stableSrc.current[seg.id] : undefined;

  // Pre-fetch the next segment's audio so playback starts quickly and survives going offline.
  useEffect(() => {
    const next = playlist[index + 1];
    if (next?.audioUrl) {
      prefetch.current = new Audio();
      prefetch.current.preload = 'auto';
      prefetch.current.src = next.audioUrl;
    }
  }, [playlist, index]);

  useEffect(() => {
    if (audio.current) audio.current.playbackRate = speed;
  }, [speed, seg?.id]);

  useEffect(() => {
    setTime(0);
    setCheckpoint(false);
    setPanel(null);
    if (autoplay.current && audio.current && hasAudio) {
      audio.current.play().catch(() => setPlaying(false));
    }
  }, [seg?.id, hasAudio]);

  useEffect(() => () => void flush(), []);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(t);
  }, [toast]);

  const goTo = useCallback(
    (i: number) => {
      if (i >= playlist.length) {
        void flush();
        navigate(`/recap/${encodeURIComponent(briefingId)}`);
        return;
      }
      setParams({ seg: playlist[i].id }, { replace: true });
    },
    [playlist, briefingId, navigate, setParams],
  );

  const continueNext = useCallback(() => {
    setCheckpoint(false);
    setPanel(null);
    autoplay.current = true;
    goTo(index + 1);
  }, [goTo, index]);

  if (error) {
    return (
      <div className="mx-auto max-w-xl p-6">
        <Banner tone="error">{error}</Banner>
      </div>
    );
  }
  if (!briefing || !seg) {
    return (
      <div className="flex min-h-dvh items-center justify-center">
        <Spinner label="Loading your briefing" />
      </div>
    );
  }

  const ev = (type: Parameters<typeof track>[0]['type'], extra: Record<string, string | number | boolean> = {}, itemId?: string) =>
    track({ type, briefingId, segId: seg.id, itemId: itemId || seg.itemIds[0], conceptId: seg.conceptIds[0], payload: extra });

  const toggle = () => {
    const a = audio.current;
    if (!a) return;
    if (a.paused) {
      autoplay.current = true;
      void a.play();
    } else {
      a.pause();
    }
  };

  const skipNext = () => {
    if (!checkpoint) ev('skip', { listenedSec: Math.round(time) });
    audio.current?.pause();
    autoplay.current = true;
    goTo(index + 1);
  };

  const onAction = async (a: CheckpointAction) => {
    const first = seg.sources[0];
    switch (a) {
      case 'quiz':
        setPanel({ kind: 'quiz' });
        break;
      case 'simpler':
        setPanel({ kind: 'simpler' });
        break;
      case 'apply':
        setPanel({ kind: 'apply' });
        break;
      case 'save':
        if (!first) return;
        await post('/library', { itemId: first.itemId, briefingId, segId: seg.id });
        setToast(`Saved "${first.headline}" to your Library`);
        break;
      case 'deeper': {
        const res = await post<{ status: string; message?: string }>('/deeper', { briefingId, segId: seg.id });
        if (res.status === 'link_only') setPanel({ kind: 'notice', text: res.message || 'Only the newsletter note is available.' });
        else {
          setToast('A deeper segment is being written and will play next');
          void reload();
        }
        break;
      }
      case 'skip':
        continueNext();
        break;
      case 'lessLikeThis':
        ev('lessLikeThis', {}, first?.itemId);
        setToast('Got it, fewer stories like this');
        continueNext();
        break;
      case 'openSource':
        if (first?.url) {
          window.open(first.url, '_blank', 'noopener,noreferrer');
          ev('openSource', {}, first.itemId);
        } else setToast('This story had no link in the newsletter');
        break;
    }
  };

  const currentLine = hasAudio ? lineAt(seg.lineTimings || [], time) : -1;

  return (
    <div className="fixed inset-0 z-40 overflow-y-auto bg-bg">
      <div className="mx-auto flex min-h-dvh max-w-2xl flex-col px-4 pb-40 pt-4">
        <header className="flex items-center justify-between gap-2">
          <button onClick={() => navigate('/today')} className="flex size-11 items-center justify-center rounded-full hover:bg-surface-2" aria-label="Close player">
            <X className="size-5" />
          </button>
          <p className="text-caption text-ink-3">
            Segment {index + 1} of {playlist.length}
          </p>
          <span className="size-11" />
        </header>

        <h1 className="mt-2 text-title font-bold">{seg.title}</h1>
        <p className="mt-1 text-caption text-ink-2">{seg.whyChosen}</p>

        <nav aria-label="Chapters" className="mt-4 flex gap-1">
          {playlist.map((s, i) => (
            <button
              key={s.id}
              onClick={() => {
                if (i === index) {
                  ev('replay');
                  if (audio.current) audio.current.currentTime = 0;
                } else goTo(i);
              }}
              aria-label={`${i === index ? 'Replay' : 'Go to'} ${s.title}`}
              aria-current={i === index}
              className="h-11 flex-1"
            >
              <span className={`block h-1.5 rounded-full ${i < index ? 'bg-accent/60' : i === index ? 'bg-accent' : 'bg-line'}`} />
            </button>
          ))}
        </nav>

        {!hasAudio && (
          <div className="mt-4">
            <AudioPanel briefingId={briefingId} segment={seg} onChanged={reload} />
          </div>
        )}
        {seg.status === 'generating' && <Spinner label="Writing this deeper segment" />}

        <section aria-label="Transcript" className="mt-6 flex-1">
          {!hasAudio && <p className="mb-3 text-caption font-medium text-ink-3">Text version</p>}
          <ol className="space-y-3">
            {(seg.script || []).map((line, i) => (
              <li key={i}>
                <button
                  type="button"
                  disabled={!hasAudio}
                  onClick={() => {
                    const t = seg.lineTimings?.[i];
                    if (audio.current && t) audio.current.currentTime = t.s;
                  }}
                  aria-current={i === currentLine ? 'true' : undefined}
                  className={`block w-full rounded-xl px-3 py-2 text-left transition-colors ${i === currentLine ? 'bg-highlight' : ''} ${hasAudio ? 'cursor-pointer hover:bg-surface-2' : 'cursor-default'}`}
                >
                  <span className={`block text-caption font-semibold ${line.speaker === 'A' ? 'text-accent' : 'text-ink-2'}`}>{line.speakerName}</span>
                  <span className="text-transcript">{line.text}</span>
                </button>
              </li>
            ))}
          </ol>
          {hasAudio && seg.lineTimings && (
            <p className="mt-3 text-caption text-ink-3">Line highlighting follows the audio approximately.</p>
          )}
        </section>

        <div className="mt-6 flex flex-wrap gap-2">
          {seg.sources.map((s) => (
            <SourceChip key={s.itemId} {...s} briefingId={briefingId} segId={seg.id} />
          ))}
        </div>
        {seg.faithfulness === 'review' && <p className="mt-3 text-caption text-ink-3">This script was flagged for review against its sources.</p>}
      </div>

      <div className="fixed inset-x-0 bottom-0 z-40 border-t border-line bg-surface/95 pb-[env(safe-area-inset-bottom)] backdrop-blur">
        <div className="mx-auto max-w-2xl px-4 py-3">
          {hasAudio && (
            <>
              <audio
                ref={audio}
                src={audioSrc}
                onError={() => {
                  // Expired link: switch to the newest URL and resume where we were.
                  if (seg.audioUrl && seg.audioUrl !== stableSrc.current[seg.id] && audio.current) {
                    const at = audio.current.currentTime;
                    stableSrc.current[seg.id] = seg.audioUrl;
                    audio.current.src = seg.audioUrl;
                    audio.current.currentTime = at;
                    void audio.current.play().catch(() => setPlaying(false));
                  }
                }}
                preload="auto"
                onPlay={() => {
                  setPlaying(true);
                  ev('play');
                }}
                onPause={() => {
                  setPlaying(false);
                  if (!audio.current?.ended) ev('pause', { atSec: Math.round(time) });
                }}
                onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
                onLoadedMetadata={(e) => setDuration(e.currentTarget.duration)}
                onEnded={() => {
                  ev('complete', { listenedSec: Math.round(duration) });
                  setCheckpoint(true);
                }}
              />
              <input
                type="range"
                min={0}
                max={duration || 0}
                step={0.5}
                value={time}
                onChange={(e) => {
                  if (audio.current) audio.current.currentTime = Number(e.target.value);
                }}
                aria-label="Seek"
                className="w-full accent-[var(--accent)]"
              />
              <div className="flex justify-between text-caption tabular-nums text-ink-3">
                <span>{formatTime(time)}</span>
                <span>{formatTime(duration)}</span>
              </div>
            </>
          )}
          <div className="mt-1 flex items-center justify-between gap-2">
            <button
              onClick={() => {
                const v = SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.length];
                setSpeed(v);
                ev('speedChange', { speed: v });
              }}
              className="min-h-11 rounded-full border border-line px-3 text-caption font-medium"
              aria-label={`Playback speed ${speed}x`}
              disabled={!hasAudio}
            >
              {speed}x
            </button>
            <div className="flex items-center gap-3">
              <button
                onClick={() => {
                  if (audio.current) audio.current.currentTime = Math.max(0, audio.current.currentTime - 15);
                }}
                disabled={!hasAudio}
                className="flex size-12 items-center justify-center rounded-full border border-line disabled:opacity-40"
                aria-label="Back 15 seconds"
              >
                <RotateCcw className="size-5" />
              </button>
              {hasAudio ? (
                <button onClick={toggle} className="flex size-16 items-center justify-center rounded-full bg-accent text-on-accent" aria-label={playing ? 'Pause' : 'Play'}>
                  {playing ? <Pause className="size-7" fill="currentColor" /> : <Play className="ml-1 size-7" fill="currentColor" />}
                </button>
              ) : (
                <Button onClick={() => setCheckpoint(true)}>Done reading</Button>
              )}
              <button onClick={skipNext} className="flex size-12 items-center justify-center rounded-full border border-line" aria-label="Next segment">
                <SkipForward className="size-5" />
              </button>
            </div>
            <button onClick={() => goTo(Math.max(0, index - 1))} disabled={index === 0} className="flex size-11 items-center justify-center rounded-full disabled:opacity-30" aria-label="Previous segment">
              <ChevronLeft className="size-5" />
            </button>
          </div>
        </div>
      </div>

      {checkpoint && !panel && (
        <CheckpointSheet
          title={seg.title}
          isLast={index === playlist.length - 1}
          hasProject={Boolean(me?.profile?.project?.text)}
          onAsk={(q) => setPanel({ kind: 'ask', question: q })}
          onAction={(a) => void onAction(a).catch((e: Error) => setToast(e.message))}
          onContinue={continueNext}
        />
      )}

      {panel && (
        <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/30" role="presentation">
          <div role="dialog" aria-modal="true" className="animate-sheet max-h-[85dvh] w-full max-w-xl overflow-y-auto rounded-t-[20px] border border-line bg-surface p-5 pb-[max(1.25rem,env(safe-area-inset-bottom))]">
            {panel.kind === 'ask' && (
              <AskPanel
                briefingId={briefingId}
                segId={seg.id}
                question={panel.question}
                onDeeper={() => void onAction('deeper')}
                onSave={() => void onAction('save')}
                onBack={() => setPanel(null)}
              />
            )}
            {panel.kind === 'quiz' && <QuizPanel briefingId={briefingId} segId={seg.id} onDone={() => setPanel(null)} />}
            {panel.kind === 'simpler' && <RephrasePanel briefingId={briefingId} segment={seg} onBack={() => setPanel(null)} />}
            {panel.kind === 'apply' && <ApplyPanel briefingId={briefingId} segId={seg.id} onBack={() => setPanel(null)} />}
            {panel.kind === 'notice' && (
              <div className="space-y-4">
                <p className="text-body">{panel.text}</p>
                <Button variant="secondary" onClick={() => setPanel(null)}>
                  Back
                </Button>
              </div>
            )}
          </div>
        </div>
      )}

      {toast && (
        <div role="status" className="fixed inset-x-4 bottom-36 z-[60] mx-auto max-w-md rounded-xl bg-ink px-4 py-3 text-body text-bg shadow-lg">
          <div className="flex items-center justify-between gap-3">
            <span>{toast}</span>
            <button className="text-caption underline" onClick={() => setToast(null)}>
              Dismiss
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
