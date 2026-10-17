import { useCallback, useEffect, useRef, useState } from 'react';
import { useAuth } from '../../auth/AuthContext';
import { firstWords, shortDate } from '../../lib/demo';
import { claimAudio, releaseAudio } from '../../lib/audioFocus';
import { fetchPodcast, type PodcastPage } from '../../lib/podcastApi';
import { startSectionPodcast, type Issue, type Section } from '../../lib/tldrApi';
import { activeLine, clock } from '../podcast/PodcastPlayer';
import { AskPanel } from './AskPanel';
import { CloseIcon, Modal } from './Modal';

const POLL_MS = 3000;
const GIVE_UP_MS = 15 * 60 * 1000;

const PHASE_TEXT = (phase?: string | null) => (phase === 'audio' ? 'Recording their voices…' : 'Writing Maya and Theo’s script…');

/** Starts the section podcast and follows it until it is ready or failed. `retry` starts it again. */
function usePodcast(issue: Issue, section: Section, preference: string) {
  const { user, loading, signInForSample } = useAuth();
  const [podcast, setPodcast] = useState<PodcastPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const timer = useRef(0);

  // Ask and Quiz need an identity: a private anonymous one is created in the background, with no sign-in step.
  const joining = useRef(false);
  useEffect(() => {
    if (loading || user || joining.current) return;
    joining.current = true;
    signInForSample().catch(() => {});
  }, [loading, user, signInForSample]);

  useEffect(() => {
    if (loading) return;
    let live = true;
    const started = Date.now();
    setPodcast(null);
    setError(null);

    const poll = async (pid: string) => {
      try {
        const p = await fetchPodcast(pid);
        if (!live) return;
        setPodcast(p);
        if (p.status === 'processing') {
          if (Date.now() - started > GIVE_UP_MS) {
            // Show the failure screen (with Try again); setting only `error` was invisible while a script was on screen.
            return setPodcast({ ...p, status: 'failed', message: 'This is taking too long. Try again in a few minutes.' });
          }
          timer.current = window.setTimeout(() => poll(pid), POLL_MS);
        }
      } catch (e) {
        if (!live) return;
        setError(e instanceof Error ? e.message : 'Could not check the podcast');
        timer.current = window.setTimeout(() => poll(pid), POLL_MS * 2);
      }
    };

    (async () => {
      try {
        const s = await startSectionPodcast(issue, section.id, preference);
        if (!live || !s.podcastId) return;
        setPodcast(s as PodcastPage);
        if (s.status === 'processing') timer.current = window.setTimeout(() => poll(s.podcastId!), POLL_MS);
        else if (s.status === 'ready') void poll(s.podcastId); // the POST view has no line timings guarantee; read the page view
      } catch (e) {
        if (live) setError(e instanceof Error ? e.message : 'Could not start the podcast');
      }
    })();
    return () => {
      live = false;
      window.clearTimeout(timer.current);
    };
    // The preference and section are fixed for the life of one player.
  }, [loading, attempt]); // eslint-disable-line react-hooks/exhaustive-deps

  return { podcast, error, retry: () => setAttempt((n) => n + 1) };
}

export function PlayerModal({
  issue, section, preference, onClose, onQuiz,
}: { issue: Issue; section: Section; preference: string; onClose: (pid: string | null) => void; onQuiz: (pid: string) => void }) {
  const { podcast, error, retry } = usePodcast(issue, section, preference);
  const audio = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [tip, setTip] = useState(false);
  const rows = useRef<(HTMLDivElement | null)[]>([]);

  const pid = podcast?.podcastId ?? null;
  const ready = podcast?.status === 'ready' && !!podcast.audioUrl;
  const failed = podcast?.status === 'failed';
  const lines = podcast?.lines ?? [];
  const current = ready ? activeLine(podcast?.lineTimings, time) : -1;
  const total = duration || podcast?.durationSec || 0;

  const pause = useCallback(() => audio.current?.pause(), []);
  const close = () => {
    pause();
    onClose(pid);
  };
  const quiz = () => {
    pause();
    if (pid) onQuiz(pid);
  };

  useEffect(() => {
    const el = audio.current;
    return () => {
      if (el) releaseAudio(el);
    };
  }, [ready]);

  useEffect(() => {
    if (playing && current >= 0) rows.current[current]?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
  }, [current, playing]);

  const toggle = async () => {
    const el = audio.current;
    if (!el) return;
    if (el.paused) {
      claimAudio(el);
      try {
        await el.play();
      } catch {
        /* autoplay blocked: the button stays a play button */
      }
    } else el.pause();
  };
  const seek = (t: number) => {
    if (audio.current) audio.current.currentTime = Math.max(0, Math.min(total || t, t));
  };

  return (
    <Modal label="Podcast player" width={1180} align="top" paddingTop={32} onClose={close}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap', padding: '18px 24px', borderBottom: '1px solid var(--border)' }}>
        <div style={{ minWidth: 0 }}>
          <p style={{ fontSize: 13, fontWeight: 700, color: 'var(--kn-muted)' }}>TLDR Tech · {shortDate(issue.date)} · Maya and Theo</p>
          <h2 style={{ margin: '4px 0 0', fontSize: 22, fontWeight: 800 }}>{section.name}</h2>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <div style={{ position: 'relative' }}>
            <button className="btn" onClick={quiz} disabled={!ready} onMouseEnter={() => setTip(true)} onMouseLeave={() => setTip(false)} onFocus={() => setTip(true)} onBlur={() => setTip(false)} aria-describedby="quiz-tip">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <circle cx="12" cy="12" r="10" />
                <path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3" />
                <path d="M12 17h.01" />
              </svg>
              Quiz me
            </button>
            {tip && (
              <div id="quiz-tip" role="tooltip" style={{ position: 'absolute', right: 0, top: 52, width: 280, padding: '12px 14px', borderRadius: 10, background: '#15181d', color: '#fff', fontSize: 13, lineHeight: 1.5, zIndex: 5, boxShadow: '0 8px 24px rgba(0,0,0,0.2)' }}>
                A few quick questions. Your answers tell Knowly how much you already know, so your next podcasts fit you better.
              </div>
            )}
          </div>
          <button className="btn btn-ghost icon-btn" aria-label="Close podcast" onClick={close}><CloseIcon size={22} /></button>
        </div>
      </div>

      {failed || (error && !podcast) ? (
        <div role="alert" style={{ padding: '80px 24px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 16, textAlign: 'center' }}>
          <p style={{ fontSize: 18, fontWeight: 700 }}>The podcast could not be made</p>
          <p style={{ fontSize: 15, color: 'var(--text-2)', maxWidth: 460 }}>{(failed && podcast?.message) || error || 'Something went wrong.'}</p>
          <button className="btn btn-primary" onClick={retry}>Try again</button>
        </div>
      ) : !podcast || (podcast.status === 'processing' && !lines.length) ? (
        <div role="status" style={{ padding: '80px 24px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 16, textAlign: 'center' }}>
          <div className="spin" style={{ width: 56, height: 56, borderRadius: 999, border: '4px solid var(--accent-soft)', borderTopColor: 'var(--accent)' }} />
          <p style={{ fontSize: 18, fontWeight: 700 }}>Creating your podcast</p>
          <p style={{ fontSize: 15, color: 'var(--text-2)', maxWidth: 420 }}>{PHASE_TEXT(podcast?.phase)}</p>
        </div>
      ) : (
        <div style={{ display: 'flex', flexWrap: 'wrap' }}>
          <div style={{ flex: '999 1 600px', minWidth: 0, padding: 24, borderRight: '1px solid var(--border)' }}>
            {preference && (
              <p style={{ margin: '0 0 16px', fontSize: 14, color: 'var(--text-2)', background: 'var(--accent-soft)', padding: '10px 14px', borderRadius: 10 }}>
                <strong style={{ color: 'var(--text)' }}>Maya is asking with your preference in mind:</strong> {firstWords(preference)}
              </p>
            )}
            {ready ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
                <audio
                  ref={audio}
                  src={podcast.audioUrl}
                  preload="metadata"
                  onLoadedMetadata={(e) => setDuration(e.currentTarget.duration || 0)}
                  onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
                  onPlay={(e) => {
                    claimAudio(e.currentTarget);
                    setPlaying(true);
                  }}
                  onPause={() => setPlaying(false)}
                  onEnded={() => setPlaying(false)}
                />
                <button className="btn btn-ghost icon-btn" aria-label="Back 15 seconds" onClick={() => seek(time - 15)}>
                  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7L3 8" /><path d="M3 3v5h5" /></svg>
                </button>
                <button className="btn btn-primary" aria-label={playing ? 'Pause' : 'Play'} onClick={toggle} style={{ width: 64, height: 64, padding: 0, borderRadius: 999 }}>
                  {playing ? (
                    <svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="5" width="4" height="14" rx="1" /><rect x="14" y="5" width="4" height="14" rx="1" /></svg>
                  ) : (
                    <svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13a1 1 0 0 0 1.5.9l10-6.5a1 1 0 0 0 0-1.8l-10-6.5A1 1 0 0 0 8 5.5z" /></svg>
                  )}
                </button>
                <button className="btn btn-ghost icon-btn" aria-label="Forward 15 seconds" onClick={() => seek(time + 15)}>
                  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-3-6.7L21 8" /><path d="M21 3v5h-5" /></svg>
                </button>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ height: 8, borderRadius: 999, background: 'var(--border)', overflow: 'hidden' }}>
                    <div style={{ height: '100%', background: 'var(--accent)', width: `${total ? Math.min(100, Math.round((time / total) * 100)) : 0}%` }} />
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6, fontSize: 13, color: 'var(--kn-muted)' }}>
                    <span>{clock(time)}</span>
                    <span>{total ? clock(total) : '--:--'}</span>
                  </div>
                </div>
              </div>
            ) : (
              <p role="status" style={{ fontSize: 15, color: 'var(--text-2)' }}>The script is ready. {PHASE_TEXT(podcast.phase)}</p>
            )}
            <p style={{ margin: '22px 0 8px', fontSize: 13, fontWeight: 700, color: 'var(--kn-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Transcript</p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 2, maxHeight: 420, overflowY: 'auto' }}>
              {lines.map((l, i) => (
                <div
                  key={i}
                  ref={(el) => {
                    rows.current[i] = el;
                  }}
                  className={i === current ? 'line now' : 'line'}
                  aria-current={i === current ? 'true' : undefined}
                >
                  <span style={{ flex: 'none', width: 48, fontWeight: 800, color: l.role === 'question' ? 'var(--accent)' : 'var(--text)' }}>{l.speaker}</span>
                  <span>{l.text}</span>
                </div>
              ))}
            </div>
          </div>
          {ready && pid && <AskPanel pid={pid} onAsk={pause} />}
        </div>
      )}
    </Modal>
  );
}
