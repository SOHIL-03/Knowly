import { useEffect, useState } from 'react';
import { stream, type StreamEvent } from '../../lib/api';
import type { Citation } from '../../lib/types';
import { SourceChip } from '../SourceChip';
import { SpeakButton } from '../SpeakButton';
import { Banner, Button, Chip } from '../ui';

interface Props {
  briefingId: string;
  segId: string;
  question: string;
  onDeeper: () => void;
  onSave: () => void;
  onBack: () => void;
}

/** Answer view: streamed text, then the validated answer with citation chips (section 4.3). */
export function AskPanel({ briefingId, segId, question, onDeeper, onSave, onBack }: Props) {
  const [text, setText] = useState('');
  const [final, setFinal] = useState<StreamEvent | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setText('');
    setFinal(null);
    setError(null);
    const ctrl = new AbortController();
    stream(
      '/ask',
      { briefingId, segId, question },
      (ev) => {
        if (ev.type === 'delta') setText((t) => t + (ev.text || ''));
        else if (ev.type === 'reset') setText(ev.text || '');
        else if (ev.type === 'final') setFinal(ev);
        else if (ev.type === 'error') setError(ev.message || 'Something went wrong');
      },
      ctrl.signal,
    ).catch((e: Error) => {
      if (e.name !== 'AbortError') setError(e.message);
    });
    return () => ctrl.abort();
  }, [briefingId, segId, question]);

  const shown = final?.answer ?? text;
  const citations: Citation[] = final?.citations || [];
  const general = final && !final.grounded;

  return (
    <div className="space-y-4" aria-live="polite">
      <p className="text-caption text-ink-3">You asked</p>
      <p className="text-body font-medium">{question}</p>
      {error ? (
        <Banner tone="error">{error}</Banner>
      ) : (
        <div className={`rounded-xl p-4 text-body leading-relaxed ${general ? 'border border-partly/40 bg-partly-soft' : 'bg-surface-2'}`}>
          {shown ? stripMarkers(shown) : <span className="text-ink-3">Thinking…</span>}
        </div>
      )}
      {general && final?.scope !== 'refused' && (
        <p className="text-caption text-ink-2">This answer is not from your newsletters, so it has no source.</p>
      )}
      {final?.qaId && !error && <SpeakButton path={`/qa/${final.qaId}/audio`} autoPlay label="Listen to the answer" />}
      {citations.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {citations.map((c) => (
            <SourceChip key={c.itemId} newsletter={c.newsletter} headline={c.headline} url={c.url} itemId={c.itemId} briefingId={briefingId} segId={segId} />
          ))}
        </div>
      )}
      {final && (
        <div className="flex flex-wrap gap-2">
          <Chip onClick={onDeeper}>Go deeper</Chip>
          <Chip onClick={onSave}>Save</Chip>
          <Chip onClick={onBack}>Back to briefing</Chip>
        </div>
      )}
      {!final && !error && (
        <Button variant="ghost" onClick={onBack}>
          Back to briefing
        </Button>
      )}
    </div>
  );
}

function stripMarkers(t: string): string {
  return t.replace(/\s?\[it_[A-Za-z0-9_]+\]/g, '');
}
