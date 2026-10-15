import { useEffect, useState } from 'react';
import { get } from '../lib/api';
import type { Trace, TraceEntry } from '../lib/review';
import { Banner, Card, Chip } from './ui';

export interface TraceState {
  trace: Trace | null;
  error: string | null;
}

/** The OpenAI responses captured while the briefing was made. `refreshKey` reloads it (for example when the briefing changes). */
export function useLlmTrace(briefingId: string | undefined, refreshKey?: string): TraceState {
  const [state, setState] = useState<TraceState>({ trace: null, error: null });
  useEffect(() => {
    if (!briefingId) return;
    let live = true;
    get<Trace>(`/briefings/${encodeURIComponent(briefingId)}/llm-trace`)
      .then((trace) => live && setState({ trace, error: null }))
      .catch((e: Error) => live && setState((s) => ({ ...s, error: e.message })));
    return () => {
      live = false;
    };
  }, [briefingId, refreshKey]);
  return state;
}

const STATUS: Record<TraceEntry['status'], { label: string; tone: string }> = {
  ok: { label: 'Valid', tone: 'text-good' },
  validation_failed: { label: 'Validation failed', tone: 'text-notyet' },
  empty: { label: 'Empty response', tone: 'text-notyet' },
  blocked: { label: 'Declined by OpenAI', tone: 'text-notyet' },
  request_failed: { label: 'Request failed', tone: 'text-notyet' },
};

function Pre({ children, label }: { children: string; label: string }) {
  return (
    <pre aria-label={label} className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-surface-2 p-3 text-caption">
      {children}
    </pre>
  );
}

function Entry({ e }: { e: TraceEntry }) {
  const s = STATUS[e.status];
  const tokens = e.usage ? `${e.usage.input ?? '?'} in · ${e.usage.output ?? '?'} out · ${e.usage.total ?? '?'} total` : 'no usage reported';
  return (
    <details className="rounded-xl border border-line px-3 py-2">
      <summary className="flex cursor-pointer flex-wrap items-center gap-x-3 gap-y-1 text-body">
        <span className="font-medium">{e.label}</span>
        {e.segTitle && <span className="text-caption text-ink-3">{e.segTitle}</span>}
        <span className={`text-caption font-semibold ${s.tone}`}>{s.label}</span>
        {e.attempt > 1 && <span className="text-caption text-ink-3">attempt {e.attempt}</span>}
        <span className="text-caption text-ink-3">{tokens}</span>
      </summary>
      <div className="mt-3 space-y-3">
        <p className="text-caption text-ink-2">
          Model <b>{e.model}</b>
          {e.durationMs != null && <> · {e.durationMs} ms</>}
          {e.finishReason && <> · finish: {e.finishReason}</>}
        </p>
        {e.error && <Banner tone="error">{e.error}</Banner>}
        {e.promptPreview != null && (
          <div>
            <p className="mb-1 text-caption font-medium text-ink-3">
              Input (first {e.promptPreview.length.toLocaleString()} of {e.promptChars?.toLocaleString()} characters)
            </p>
            <Pre label="Input preview">{e.promptPreview}</Pre>
          </div>
        )}
        {e.rawResponse != null && (
          <div>
            <p className="mb-1 text-caption font-medium text-ink-3">Raw OpenAI response (exactly as returned)</p>
            <Pre label="Raw response">{e.rawResponse || '(empty)'}</Pre>
          </div>
        )}
        {e.parsed !== undefined && (
          <div>
            <p className="mb-1 text-caption font-medium text-ink-3">Parsed result (after schema validation)</p>
            <Pre label="Parsed response">{JSON.stringify(e.parsed, null, 2)}</Pre>
          </div>
        )}
        {e.appValidation && (
          <p className={`text-caption ${e.appValidation.passed ? 'text-good' : 'text-notyet'}`}>
            Script checks: {e.appValidation.passed ? 'passed' : `rejected: ${e.appValidation.problems.join('; ')}`}
            {e.faithfulness && <> · faithfulness: {e.faithfulness}</>}
          </p>
        )}
      </div>
    </details>
  );
}

/** Every OpenAI response of this briefing, raw and parsed shown separately. Renders nothing unless the briefing was made with review on. */
export function LlmPreview({ trace: { trace, error } }: { trace: TraceState }) {
  if (error && !trace) return <Banner tone="error">{error}</Banner>;
  if (!trace?.enabled) return null;
  const total = trace.entries.reduce((n, e) => n + (e.usage?.total ?? 0), 0);
  return (
    <Card as="section" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-section font-semibold">OpenAI response preview</h2>
        <div className="flex gap-2">
          <Chip>{trace.model}</Chip>
          <Chip>{trace.entries.length} calls</Chip>
          <Chip>{total.toLocaleString()} tokens</Chip>
        </div>
      </div>
      {trace.entries.length === 0 ? (
        <p className="text-body text-ink-2">No OpenAI responses captured yet.</p>
      ) : (
        <div className="space-y-2">
          {trace.entries.map((e) => (
            <Entry key={e.seq} e={e} />
          ))}
        </div>
      )}
    </Card>
  );
}
