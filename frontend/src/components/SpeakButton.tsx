import { Loader2, Pause, Volume2 } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { post } from '../lib/api';

/**
 * Plays a spoken version of a server-stored answer (Gemini single-speaker TTS).
 * `path` is the API endpoint that returns {url}; audio is made once and cached on the server.
 * With autoPlay it starts as soon as the audio is ready; if the browser blocks autoplay the
 * button stays available. The text is always on screen, so audio is never required.
 */
export function SpeakButton({ path, body, autoPlay, label = 'Listen' }: { path: string; body?: unknown; autoPlay?: boolean; label?: string }) {
  const [state, setState] = useState<'idle' | 'loading' | 'playing' | 'ready' | 'error'>('idle');
  const audio = useRef<HTMLAudioElement | null>(null);
  const started = useRef(false);

  const load = async (): Promise<HTMLAudioElement | null> => {
    if (audio.current) return audio.current;
    setState('loading');
    try {
      const { url } = await post<{ url: string }>(path, body ?? {});
      const a = new Audio(url);
      a.onended = () => setState('ready');
      a.onpause = () => setState((s) => (s === 'playing' ? 'ready' : s));
      a.onplay = () => setState('playing');
      audio.current = a;
      setState('ready');
      return a;
    } catch {
      setState('error');
      return null;
    }
  };

  const play = async () => {
    const a = await load();
    if (!a) return;
    if (!a.paused) {
      a.pause();
      return;
    }
    a.currentTime = state === 'ready' && a.ended ? 0 : a.currentTime;
    await a.play().catch(() => setState('ready'));
  };

  useEffect(() => {
    if (autoPlay && !started.current) {
      started.current = true;
      void play();
    }
    return () => audio.current?.pause();
  }, [path]);

  if (state === 'error') return <span className="text-caption text-ink-3">Audio unavailable; the text is above.</span>;
  return (
    <button
      type="button"
      onClick={() => void play()}
      className="inline-flex min-h-9 items-center gap-1.5 rounded-full border border-line bg-surface px-3 text-caption font-medium text-ink-2 hover:border-accent/60 hover:text-accent"
      aria-label={state === 'playing' ? 'Pause spoken answer' : `${label} (spoken)`}
    >
      {state === 'loading' ? <Loader2 className="size-4 animate-spin" aria-hidden /> : state === 'playing' ? <Pause className="size-4" aria-hidden /> : <Volume2 className="size-4" aria-hidden />}
      {state === 'playing' ? 'Pause' : label}
    </button>
  );
}
