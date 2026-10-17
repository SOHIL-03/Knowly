import { Loader2, Mic, Send, Square, Volume2 } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../../lib/api';
import { pauseCurrent, playExclusive, claimAudio } from '../../lib/audioFocus';
import { answerAudio, chatHistory, newClientId, sendChat, transcribe, type ChatMessage } from '../../lib/podcastApi';
import { MAX_RECORDING_SECONDS, recordingSupported, recordingUnavailableReason, useRecorder } from '../../lib/useRecorder';
import { Banner, Button } from '../ui';

type Row = ChatMessage & { local?: 'sending' | 'failed'; clientId?: string; error?: string };

const errText = (e: unknown, fallback: string) => (e instanceof ApiError || e instanceof Error ? e.message : fallback);

/** Spoken version of one assistant answer. The text is always shown; audio is made on demand (Gemini TTS) and can be retried. */
function AnswerAudio({ pid, id, auto }: { pid: string; id: string; auto: boolean }) {
  const [state, setState] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle');
  const [url, setUrl] = useState<string | null>(null);
  const el = useRef<HTMLAudioElement>(null);
  const requested = useRef(false);

  const load = useCallback(
    async (play: boolean) => {
      setState('loading');
      try {
        const r = await answerAudio(pid, id);
        setUrl(r.url);
        setState('ready');
        if (play) {
          await new Promise((res) => setTimeout(res, 0)); // let <audio> mount with its src
          // If the browser blocks autoplay, the visible controls are the play button.
          if (el.current) await playExclusive(el.current);
        }
      } catch {
        setState('error');
      }
    },
    [pid, id],
  );

  useEffect(() => {
    if (auto && !requested.current) {
      requested.current = true;
      void load(true);
    }
  }, [auto, load]);

  if (state === 'ready' && url)
    return <audio ref={el} controls preload="auto" src={url} className="mt-2 h-9 w-full" onPlay={(e) => claimAudio(e.currentTarget)} aria-label="Spoken answer" />;
  return (
    <div className="mt-2 flex items-center gap-2 text-caption text-ink-3">
      <Button variant="ghost" className="min-h-9 px-2 text-caption" busy={state === 'loading'} onClick={() => load(true)}>
        <Volume2 className="size-4" aria-hidden /> {state === 'error' ? 'Retry audio' : 'Listen'}
      </Button>
      {state === 'error' && <span role="alert">The spoken answer could not be made. The text above is still here.</span>}
    </div>
  );
}

/**
 * Questions about the podcast, by text or voice. Both end in one backend call (OpenAI answers); voice first goes through
 * Gemini STT and the recognised text is shown for review before it is sent. Answers can be spoken with Gemini TTS.
 */
export function ChatPanel({ pid }: { pid: string }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [draft, setDraft] = useState('');
  const [fromVoice, setFromVoice] = useState(false);
  const [busy, setBusy] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [speak, setSpeak] = useState(true);
  const [autoId, setAutoId] = useState<string | null>(null); // the one new answer that may start playing by itself
  const inFlight = useRef(false);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let live = true;
    chatHistory(pid)
      .then((m) => {
        if (!live) return;
        const answered = new Set(m.filter((x) => x.role === 'assistant').map((x) => x.replyTo));
        // A question that was stored but never answered (page refreshed mid-request) can be retried with the same id.
        setRows(m.map((x) => (x.role === 'user' && !answered.has(x.id) ? { ...x, local: 'failed' as const, clientId: x.id.replace(/^u_/, ''), error: 'No answer yet.' } : x)));
      })
      .catch((e) => live && setError(errText(e, 'Could not load the conversation')))
      .finally(() => live && setLoaded(true));
    return () => {
      live = false;
    };
  }, [pid]);

  useEffect(() => {
    // Block body on purpose: an effect must not return what scrollIntoView returns (a Promise in current Chromium).
    end.current?.scrollIntoView?.({ block: 'end' });
  }, [rows.length, busy]);

  const submit = async (text: string, clientId: string, mode: 'text' | 'voice') => {
    if (inFlight.current) return; // no duplicate submissions
    inFlight.current = true;
    setBusy(true);
    setError(null);
    pauseCurrent(); // a new question interrupts any answer that is still playing
    setRows((r) => [
      ...r.filter((x) => x.clientId !== clientId),
      { id: `u_${clientId}`, role: 'user', content: text, inputMode: mode, local: 'sending', clientId },
    ]);
    try {
      const ex = await sendChat(pid, clientId, text, mode);
      setRows((r) => [...r.filter((x) => x.clientId !== clientId), ex.user, ex.assistant]);
      setAutoId(speak ? ex.assistant.id : null);
    } catch (e) {
      setRows((r) => r.map((x) => (x.clientId === clientId ? { ...x, local: 'failed' as const, error: errText(e, 'The answer could not be made.') } : x)));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  const send = () => {
    const text = draft.trim();
    if (!text || busy || transcribing) return;
    const mode = fromVoice ? 'voice' : 'text';
    setDraft('');
    setFromVoice(false);
    void submit(text, newClientId(), mode);
  };

  const onAudio = useCallback(
    async (blob: Blob) => {
      setTranscribing(true);
      setError(null);
      try {
        const { text } = await transcribe(pid, blob);
        setDraft(text);
        setFromVoice(true);
      } catch (e) {
        setError(errText(e, 'We could not transcribe that. Try again or type your question.'));
      } finally {
        setTranscribing(false);
      }
    },
    [pid],
  );
  const recorder = useRecorder(onAudio, setError);
  const canRecord = recordingSupported();
  const disabled = busy || transcribing;

  return (
    <section aria-label="Ask about this podcast" className="flex flex-col rounded-[var(--radius-card)] border border-line bg-surface">
      <div className="flex items-center justify-between border-b border-line px-4 py-3">
        <h2 className="text-section font-semibold">Ask a question</h2>
        <label className="flex items-center gap-2 text-caption text-ink-2">
          <input type="checkbox" checked={speak} onChange={(e) => setSpeak(e.target.checked)} /> Speak answers
        </label>
      </div>

      <div className="max-h-[28rem] min-h-40 space-y-3 overflow-y-auto px-4 py-3" role="log" aria-live="polite">
        {!loaded && <p className="text-caption text-ink-3">Loading the conversation…</p>}
        {loaded && rows.length === 0 && <p className="text-caption text-ink-3">Ask anything about what you just heard. You can type, or tap the microphone.</p>}
        {rows.map((m) => (
          <div key={m.id} className={m.role === 'user' ? 'flex justify-end' : 'flex justify-start'}>
            <div className={`max-w-[85%] rounded-2xl px-3 py-2 text-body ${m.role === 'user' ? 'bg-accent text-on-accent' : 'bg-surface-2 text-ink'}`}>
              {m.role === 'user' && m.inputMode === 'voice' && <p className="mb-0.5 flex items-center gap-1 text-caption opacity-80"><Mic className="size-3" aria-hidden /> Voice question</p>}
              <p className="whitespace-pre-wrap">{m.content}</p>
              {m.local === 'sending' && <p className="mt-1 flex items-center gap-1 text-caption opacity-80"><Loader2 className="size-3 animate-spin" aria-hidden /> Getting an answer…</p>}
              {m.local === 'failed' && (
                <p className="mt-1 text-caption" role="alert">
                  {m.error}{' '}
                  <button type="button" className="underline" disabled={busy} onClick={() => submit(m.content, m.clientId!, m.inputMode === 'voice' ? 'voice' : 'text')}>
                    Retry
                  </button>
                </p>
              )}
              {m.role === 'assistant' && (
                <>
                  {m.grounded === false && <p className="mt-1 text-caption text-ink-3">This goes beyond the podcast and its sources.</p>}
                  <AnswerAudio pid={pid} id={m.id} auto={autoId === m.id} />
                </>
              )}
            </div>
          </div>
        ))}
        <div ref={end} />
      </div>

      {error && (
        <div className="px-4 pb-2">
          <Banner tone="error">{error}</Banner>
        </div>
      )}
      {fromVoice && draft && <p className="px-4 pb-1 text-caption text-ink-3">This is what we heard. Edit it if needed, then press Send.</p>}
      {!canRecord && <p className="px-4 pb-1 text-caption text-ink-3">{recordingUnavailableReason()}</p>}
      {transcribing && <p className="px-4 pb-1 text-caption text-ink-3" role="status">Transcribing your question…</p>}
      {recorder.state === 'recording' && (
        <p className="px-4 pb-1 text-caption text-notyet" role="status">
          ● Recording {recorder.seconds}s of {MAX_RECORDING_SECONDS}s. Press stop when you are done.
        </p>
      )}

      <form
        className="flex items-end gap-2 border-t border-line p-3"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <textarea
          aria-label="Your question"
          value={draft}
          rows={2}
          maxLength={1000}
          disabled={disabled || recorder.state === 'recording'}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          placeholder="Ask about this podcast…"
          className="min-h-11 flex-1 resize-none rounded-xl border border-line bg-surface p-2 text-body"
        />
        {canRecord ? (
          <Button
            type="button"
            variant={recorder.state === 'recording' ? 'danger' : 'secondary'}
            aria-label={recorder.state === 'recording' ? 'Stop recording' : 'Record a question'}
            disabled={disabled}
            onClick={() => (recorder.state === 'recording' ? recorder.stop() : void recorder.start())}
          >
            {recorder.state === 'recording' ? <Square className="size-4" aria-hidden /> : <Mic className="size-4" aria-hidden />}
          </Button>
        ) : (
          <span className="max-w-40 self-center text-caption text-ink-3" title={recordingUnavailableReason() ?? undefined}>Voice is not supported here</span>
        )}
        <Button type="submit" aria-label="Send" busy={busy} disabled={!draft.trim() || disabled}>
          <Send className="size-4" aria-hidden />
        </Button>
      </form>
    </section>
  );
}
