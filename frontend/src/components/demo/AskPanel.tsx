import { useEffect, useRef, useState } from 'react';
import { useAuth } from '../../auth/AuthContext';
import { chatHistory, newClientId, sendChat, type ChatMessage } from '../../lib/podcastApi';

const MAX_QUESTION = 500;

/** "Ask about this podcast": typed questions answered by the existing podcast chat endpoint. Asking pauses the audio. */
export function AskPanel({ pid, onAsk }: { pid: string; onAsk: () => void }) {
  const { user } = useAuth();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [pending, setPending] = useState<string | null>(null); // the question being answered
  const [error, setError] = useState<string | null>(null);
  const busy = useRef(false);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!user) return;
    let live = true;
    chatHistory(pid).then((m) => live && setMessages(m)).catch(() => {});
    return () => {
      live = false;
    };
  }, [pid, user?.uid]);

  useEffect(() => {
    end.current?.scrollIntoView?.({ block: 'end' });
  }, [messages.length, pending]);

  const send = async () => {
    const text = draft.trim();
    if (!text || busy.current || !user) return;
    busy.current = true;
    onAsk(); // pauses the podcast
    setDraft('');
    setError(null);
    setPending(text);
    try {
      const ex = await sendChat(pid, newClientId(), text, 'text');
      setMessages((m) => [...m, ex.user, ex.assistant]);
    } catch (e) {
      setDraft(text);
      setError(e instanceof Error ? e.message : 'The answer could not be made. Try again.');
    } finally {
      busy.current = false;
      setPending(null);
    }
  };

  const bubble = (m: ChatMessage) =>
    m.role === 'user' ? (
      <p key={m.id} style={{ alignSelf: 'flex-end', maxWidth: '85%', background: 'var(--accent)', color: 'var(--on-accent)', padding: '10px 14px', borderRadius: '14px 14px 4px 14px', fontSize: 14, lineHeight: 1.5, whiteSpace: 'pre-wrap' }}>{m.content}</p>
    ) : (
      <div key={m.id} style={{ alignSelf: 'flex-start', maxWidth: '92%', background: 'var(--surface)', border: '1px solid var(--border)', padding: '10px 14px', borderRadius: '14px 14px 14px 4px', fontSize: 14, lineHeight: 1.55 }}>
        <p style={{ whiteSpace: 'pre-wrap' }}>{m.content}</p>
        {m.grounded === false && <p style={{ margin: '8px 0 0', fontSize: 12, color: 'var(--kn-muted)' }}>This goes beyond the podcast and its sources.</p>}
      </div>
    );

  return (
    <aside style={{ flex: '1 1 340px', minWidth: 0, padding: 24, display: 'flex', flexDirection: 'column', gap: 14, background: 'var(--surface-2)' }}>
      <div>
        <h3 style={{ fontSize: 17, fontWeight: 800 }}>Ask about this podcast</h3>
        <p style={{ margin: '4px 0 0', fontSize: 14, color: 'var(--text-2)' }}>Asking pauses the podcast. Answers come from this section’s stories.</p>
      </div>
      <div role="log" aria-live="polite" style={{ flex: 1, minHeight: 220, maxHeight: 420, display: 'flex', flexDirection: 'column', gap: 12, overflowY: 'auto' }}>
        {messages.map(bubble)}
        {pending && (
          <>
            <p style={{ alignSelf: 'flex-end', maxWidth: '85%', background: 'var(--accent)', color: 'var(--on-accent)', padding: '10px 14px', borderRadius: '14px 14px 4px 14px', fontSize: 14, lineHeight: 1.5 }}>{pending}</p>
            <p style={{ alignSelf: 'flex-start', fontSize: 13, color: 'var(--kn-muted)' }}>Getting an answer…</p>
          </>
        )}
        <div ref={end} />
      </div>
      {error && <p role="alert" style={{ fontSize: 13, color: 'var(--notyet)' }}>{error}</p>}
      {!user && <p role="status" style={{ fontSize: 13, color: 'var(--kn-muted)' }}>Getting the chat ready…</p>}
      <div style={{ display: 'flex', gap: 8 }}>
        <label htmlFor="ask" className="sr-only">Your question</label>
        <input
          id="ask"
          className="field"
          type="text"
          maxLength={MAX_QUESTION}
          placeholder="Type a question…"
          value={draft}
          disabled={!user}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && void send()}
        />
        <button className="btn btn-primary" onClick={() => void send()} disabled={!user || !draft.trim() || !!pending} aria-label="Send question" style={{ flex: 'none' }}>Ask</button>
      </div>
    </aside>
  );
}
