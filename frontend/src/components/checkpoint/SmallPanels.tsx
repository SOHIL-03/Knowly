import { useEffect, useRef, useState } from 'react';
import { post } from '../../lib/api';
import type { Citation, Segment } from '../../lib/types';
import { SourceChip } from '../SourceChip';
import { Banner, Button, Segmented, Spinner } from '../ui';

function useOnce<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const key = useRef<string>('');
  useEffect(() => {
    const k = JSON.stringify(deps);
    if (key.current === k) return;
    key.current = k;
    setData(null);
    setError(null);
    fn().then(setData).catch((e: Error) => setError(e.message));
  }, deps);
  return { data, error };
}

/** Simpler / more technical re-explanation of the segment's last point. */
export function RephrasePanel({ briefingId, segment, onBack }: { briefingId: string; segment: Segment; onBack: () => void }) {
  const [direction, setDirection] = useState<'simpler' | 'technical'>('simpler');
  const cited = (segment.script || []).filter((l) => l.item_ids.length);
  const last = cited[cited.length - 1]?.text || segment.title;
  const { data, error } = useOnce(
    () => post<{ text: string; citations: Citation[] }>('/rephrase', { briefingId, segId: segment.id, text: last, direction }),
    [briefingId, segment.id, direction],
  );
  return (
    <div className="space-y-4">
      <Segmented
        label="Explanation level"
        value={direction}
        onChange={setDirection}
        options={[
          { value: 'simpler', label: 'Simpler' },
          { value: 'technical', label: 'More technical' },
        ]}
      />
      {error ? <Banner tone="error">{error}</Banner> : data ? <p className="rounded-xl bg-surface-2 p-4 text-body leading-relaxed">{data.text}</p> : <Spinner label="Rewriting" />}
      <div className="flex flex-wrap gap-2">
        {data?.citations.map((c) => (
          <SourceChip key={c.itemId} newsletter={c.newsletter} headline={c.headline} url={c.url} itemId={c.itemId} briefingId={briefingId} segId={segment.id} />
        ))}
      </div>
      <Button variant="secondary" onClick={onBack}>
        Back to briefing
      </Button>
    </div>
  );
}

interface Suggestion {
  idea: string;
  why_it_might_help: string;
  check_first: string;
  citations: Citation[];
}

/** Apply to my project: 1-3 grounded suggestions (section 9.6). */
export function ApplyPanel({ briefingId, segId, onBack }: { briefingId: string; segId: string; onBack: () => void }) {
  const { data, error } = useOnce(() => post<{ suggestions: Suggestion[] }>('/apply', { briefingId, segId }), [briefingId, segId]);
  return (
    <div className="space-y-4">
      {error ? (
        <Banner tone="error">{error}</Banner>
      ) : !data ? (
        <Spinner label="Thinking about your project" />
      ) : (
        <ol className="space-y-3">
          {data.suggestions.map((s, i) => (
            <li key={i} className="rounded-xl bg-surface-2 p-4">
              <p className="text-body font-semibold">{s.idea}</p>
              <p className="mt-1 text-body text-ink-2">{s.why_it_might_help}</p>
              <p className="mt-2 text-caption text-ink-2">
                <span className="font-semibold">Check first: </span>
                {s.check_first}
              </p>
              <div className="mt-2 flex flex-wrap gap-2">
                {s.citations.map((c) => (
                  <SourceChip key={c.itemId} newsletter={c.newsletter} headline={c.headline} url={c.url} itemId={c.itemId} briefingId={briefingId} segId={segId} />
                ))}
              </div>
            </li>
          ))}
        </ol>
      )}
      <Button variant="secondary" onClick={onBack}>
        Back to briefing
      </Button>
    </div>
  );
}
