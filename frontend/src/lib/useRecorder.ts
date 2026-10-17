import { useCallback, useEffect, useRef, useState } from 'react';

export const MAX_RECORDING_SECONDS = 40; // a recording must fit the server's request size limit

export type RecorderState = 'idle' | 'recording';

/** Why recording is unavailable, for the UI. Browsers only expose the microphone on HTTPS or localhost. */
export function recordingUnavailableReason(): string | null {
  if (recordingSupported()) return null;
  if (typeof window !== 'undefined' && window.isSecureContext === false)
    return 'Voice needs a secure connection: open the app on localhost or over HTTPS (it is blocked on http://' + window.location.host + ').';
  return 'Voice recording is not supported in this browser.';
}

const MIME_CANDIDATES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];

export const recordingSupported = () =>
  typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== 'undefined';

function pickMime(): string | undefined {
  return MIME_CANDIDATES.find((m) => MediaRecorder.isTypeSupported?.(m));
}

const message = (e: unknown) => {
  const name = e instanceof DOMException ? e.name : '';
  if (name === 'NotAllowedError' || name === 'SecurityError') return 'Microphone access was denied. Allow it in your browser, or type your question.';
  if (name === 'NotFoundError') return 'No microphone was found. Type your question instead.';
  return 'Recording failed. Try again or type your question.';
};

/** Records one question with MediaRecorder. `onAudio` gets the finished recording; `onError` gets a user-readable reason. */
export function useRecorder(onAudio: (blob: Blob) => void, onError: (message: string) => void) {
  const [state, setState] = useState<RecorderState>('idle');
  const [seconds, setSeconds] = useState(0);
  const rec = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const chunks = useRef<Blob[]>([]);
  const tick = useRef<number>(0);
  const cancelled = useRef(false);

  const cleanup = useCallback(() => {
    window.clearInterval(tick.current);
    stream.current?.getTracks().forEach((t) => t.stop());
    stream.current = null;
    rec.current = null;
    setState('idle');
    setSeconds(0);
  }, []);

  useEffect(() => () => {
    cancelled.current = true;
    if (rec.current && rec.current.state !== 'inactive') rec.current.stop();
    cleanup();
  }, [cleanup]);

  const stop = useCallback(() => {
    if (rec.current && rec.current.state !== 'inactive') rec.current.stop();
  }, []);

  const start = useCallback(async () => {
    if (state !== 'idle') return;
    if (!recordingSupported()) return onError('Voice recording is not supported in this browser. Type your question instead.');
    cancelled.current = false;
    try {
      stream.current = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = pickMime();
      const r = new MediaRecorder(stream.current, mimeType ? { mimeType } : undefined);
      chunks.current = [];
      r.ondataavailable = (e) => e.data.size && chunks.current.push(e.data);
      r.onerror = () => {
        onError('Recording failed. Try again or type your question.');
        cleanup();
      };
      r.onstop = () => {
        const blob = new Blob(chunks.current, { type: r.mimeType || mimeType || 'audio/webm' });
        const discard = cancelled.current;
        cleanup();
        if (!discard) onAudio(blob);
      };
      rec.current = r;
      r.start();
      setState('recording');
      const began = Date.now();
      tick.current = window.setInterval(() => {
        const s = Math.floor((Date.now() - began) / 1000);
        setSeconds(s);
        if (s >= MAX_RECORDING_SECONDS) stop();
      }, 250);
    } catch (e) {
      cleanup();
      onError(message(e));
    }
  }, [state, onAudio, onError, cleanup, stop]);

  return { state, seconds, start, stop };
}
