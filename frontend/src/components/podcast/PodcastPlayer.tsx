import { Pause, Play } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { claimAudio, releaseAudio } from '../../lib/audioFocus';
import type { PodcastPage } from '../../lib/podcastApi';
import { Banner, Button, Card } from '../ui';

export const clock = (sec: number) => `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, '0')}`;

/** Index of the transcript line being spoken at time t, from the per-line timings the server stored. */
export function activeLine(timings: { s: number; e: number }[] | null | undefined, t: number): number {
  if (!timings?.length) return -1;
  const i = timings.findIndex((x) => t >= x.s && t < x.e);
  return i >= 0 ? i : t >= timings[timings.length - 1].e ? timings.length - 1 : -1;
}

/** The podcast audio with play/pause, seeking and duration, and the transcript with the current line highlighted. */
export function PodcastPlayer({ podcast }: { podcast: PodcastPage }) {
  const audio = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(podcast.durationSec ?? 0);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const current = activeLine(podcast.lineTimings, time);
  const rows = useRef<(HTMLLIElement | null)[]>([]);

  useEffect(() => {
    const el = audio.current;
    return () => {
      if (el) releaseAudio(el);
    };
  }, []);

  useEffect(() => {
    if (playing && current >= 0) rows.current[current]?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
  }, [current, playing]);

  const toggle = async () => {
    const el = audio.current;
    if (!el) return;
    if (el.paused) {
      claimAudio(el); // pauses any spoken answer
      try {
        await el.play();
      } catch {
        setFailed(true);
      }
    } else el.pause();
  };

  const seek = (t: number) => {
    if (audio.current) audio.current.currentTime = t;
    setTime(t);
  };

  return (
    <Card as="section">
      <h1 className="text-section font-semibold">{podcast.title || podcast.sectionName}</h1>
      {podcast.sources && podcast.sources.length > 0 && (
        <p className="mt-1 text-caption text-ink-3">
          {podcast.sectionName ? `${podcast.sectionName}: ` : ''}
          {podcast.sources.map((s) => s.title).join(' · ')}
        </p>
      )}
      {podcast.status === 'ready' && podcast.audioUrl && (
        <div className="mt-3">
          <audio
            ref={audio}
            src={podcast.audioUrl}
            preload="metadata"
            onLoadedMetadata={(e) => {
              setDuration(e.currentTarget.duration || podcast.durationSec || 0);
              setLoading(false);
            }}
            onCanPlay={() => setLoading(false)}
            onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
            onPlay={(e) => {
              claimAudio(e.currentTarget);
              setPlaying(true);
            }}
            onPause={() => setPlaying(false)}
            onEnded={() => setPlaying(false)}
            onError={() => setFailed(true)}
          />
          {failed ? (
            <Banner tone="error">The audio could not be played. Reload the page to try again; the transcript is below.</Banner>
          ) : (
            <div className="flex items-center gap-3">
              <Button aria-label={playing ? 'Pause' : 'Play'} onClick={toggle} disabled={loading && !duration} className="size-11 shrink-0 rounded-full px-0">
                {playing ? <Pause className="size-5" aria-hidden /> : <Play className="size-5" aria-hidden />}
              </Button>
              <input
                type="range"
                aria-label="Seek"
                className="flex-1"
                min={0}
                max={Math.max(1, Math.floor(duration))}
                value={Math.min(time, duration || time)}
                onChange={(e) => seek(Number(e.target.value))}
              />
              <span className="w-24 text-right text-caption tabular-nums text-ink-3">{clock(time)} / {duration ? clock(duration) : '--:--'}</span>
            </div>
          )}
          {loading && !failed && <p className="mt-1 text-caption text-ink-3">Loading audio…</p>}
        </div>
      )}
      {podcast.lines && podcast.lines.length > 0 && (
        <details open className="mt-4">
          <summary className="cursor-pointer text-caption font-medium text-accent">Transcript</summary>
          <ol className="mt-2 max-h-80 space-y-2 overflow-y-auto" aria-label="Transcript">
            {podcast.lines.map((l, i) => (
              <li
                key={i}
                ref={(el) => {
                  rows.current[i] = el;
                }}
                aria-current={i === current ? 'true' : undefined}
                className={`rounded-lg px-2 py-1 text-body ${i === current ? 'bg-accent-soft' : ''}`}
              >
                <button
                  type="button"
                  className="text-left"
                  disabled={!podcast.lineTimings?.[i] || podcast.status !== 'ready'}
                  onClick={() => podcast.lineTimings?.[i] && seek(podcast.lineTimings[i].s)}
                >
                  <span className={l.role === 'question' ? 'font-semibold text-accent' : 'font-semibold'}>{l.speaker}:</span> {l.text}
                </button>
              </li>
            ))}
          </ol>
        </details>
      )}
    </Card>
  );
}
