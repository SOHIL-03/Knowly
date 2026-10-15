import { Mic, MicOff, Send } from 'lucide-react';
import { useCallback, useState } from 'react';
import { useSpeechInput } from '../lib/speech';

/** Text input with optional browser speech recognition. Recognised text stays editable before sending. */
export function AskBox({
  placeholder,
  onSubmit,
  onActivity,
  busy,
  autoFocus,
}: { placeholder: string; onSubmit: (text: string) => void; onActivity?: () => void; busy?: boolean; autoFocus?: boolean }) {
  const [text, setText] = useState('');
  const onSpeech = useCallback((t: string) => setText(t), []);
  const speech = useSpeechInput(onSpeech);

  const submit = () => {
    const t = text.trim();
    if (t.length < 2 || busy) return;
    onSubmit(t);
    setText('');
  };

  return (
    <div>
      <form
        className="flex items-center gap-2 rounded-xl border border-line bg-surface px-2"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <label className="sr-only" htmlFor="ask-input">
          {placeholder}
        </label>
        <input
          id="ask-input"
          autoFocus={autoFocus}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            onActivity?.();
          }}
          onFocus={onActivity}
          placeholder={placeholder}
          maxLength={1000}
          className="min-h-11 flex-1 bg-transparent px-2 text-body outline-none placeholder:text-ink-3"
        />
        {speech.supported && (
          <button
            type="button"
            onClick={() => {
              onActivity?.();
              if (speech.listening) speech.stop();
              else speech.start();
            }}
            aria-label={speech.listening ? 'Stop listening' : 'Speak your question'}
            className={`flex size-11 items-center justify-center rounded-lg ${speech.listening ? 'bg-notyet-soft text-notyet' : 'text-ink-2 hover:bg-surface-2'}`}
          >
            {speech.listening ? <MicOff className="size-5" /> : <Mic className="size-5" />}
          </button>
        )}
        <button
          type="submit"
          disabled={busy || text.trim().length < 2}
          aria-label="Send"
          className="flex size-11 items-center justify-center rounded-lg text-accent hover:bg-accent-soft disabled:opacity-40"
        >
          <Send className="size-5" />
        </button>
      </form>
      {speech.listening && <p className="mt-1 text-caption text-ink-3">Listening… speak, then edit if needed.</p>}
      {speech.error && <p className="mt-1 text-caption text-notyet">{speech.error}</p>}
    </div>
  );
}
