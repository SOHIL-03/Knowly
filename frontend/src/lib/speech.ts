import { useCallback, useEffect, useRef, useState } from 'react';

// Chrome's Web Speech API (a browser feature). When it is missing the mic button is hidden and
// typing still works (section 12).
type Recognition = {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  start(): void;
  stop(): void;
  onresult: ((e: { results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> }) => void) | null;
  onend: (() => void) | null;
  onerror: ((e: { error: string }) => void) | null;
};

function ctor(): (new () => Recognition) | null {
  const w = window as unknown as { SpeechRecognition?: new () => Recognition; webkitSpeechRecognition?: new () => Recognition };
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}

export function useSpeechInput(onText: (text: string) => void) {
  const [listening, setListening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rec = useRef<Recognition | null>(null);
  const supported = typeof window !== 'undefined' && ctor() !== null;

  const stop = useCallback(() => rec.current?.stop(), []);

  const start = useCallback(() => {
    const C = ctor();
    if (!C) return;
    const r = new C();
    r.lang = 'en-US';
    r.interimResults = true;
    r.continuous = false;
    r.onresult = (e) => {
      let text = '';
      for (let i = 0; i < e.results.length; i++) text += e.results[i][0].transcript;
      onText(text);
    };
    r.onend = () => setListening(false);
    r.onerror = (e) => {
      setError(e.error === 'not-allowed' ? 'Microphone permission was denied. You can type instead.' : null);
      setListening(false);
    };
    rec.current = r;
    setError(null);
    setListening(true);
    r.start();
  }, [onText]);

  useEffect(() => () => rec.current?.stop(), []);
  return { supported, listening, start, stop, error };
}
