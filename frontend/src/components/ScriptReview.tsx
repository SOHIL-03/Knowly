import { Copy, Pencil, RotateCcw, Volume2 } from 'lucide-react';
import { useState } from 'react';
import { post } from '../lib/api';
import { isModified, prettyJson, scriptEntry, scriptToText, type TraceEntry } from '../lib/review';
import type { Segment } from '../lib/types';
import { Banner, Button, Chip } from './ui';

/** The generated script, held before text-to-speech. The text shown (and edited) here is exactly what Generate Audio sends. */
export function ScriptReview({
  briefingId,
  segment,
  trace,
  onChanged,
}: {
  briefingId: string;
  segment: Segment;
  trace: TraceEntry[];
  onChanged: () => void;
}) {
  const lines = segment.script || [];
  const [texts, setTexts] = useState(() => lines.map((l) => l.text));
  const [editing, setEditing] = useState(false);
  const [view, setView] = useState<'readable' | 'raw'>('readable');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const modified = isModified(lines, texts);
  const raw = scriptEntry(trace, segment.id);
  const fallback = segment.scriptMode === 'fallback';

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError('Copy is not available in this browser');
    }
  };

  const generate = async () => {
    setBusy(true);
    setError(null);
    try {
      await post(`/briefings/${encodeURIComponent(briefingId)}/segments/${segment.id}/audio/generate`, modified ? { texts } : {});
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not start the audio');
      setBusy(false);
    }
  };

  return (
    <section aria-label="Script review" className="space-y-3 rounded-[var(--radius-card)] border border-accent/40 bg-surface p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-section font-semibold">Review the script</h2>
        <div className="flex flex-wrap gap-2">
          <Chip>{lines.length} lines</Chip>
          {segment.faithfulness && <Chip>faithfulness: {segment.faithfulness}</Chip>}
          {modified && <Chip active>Modified by you</Chip>}
        </div>
      </div>
      <p className="text-caption text-ink-2">Audio has not been made yet. Check the script below, then press Generate Audio.</p>
      {fallback ? (
        <Banner tone="warn">
          The model&apos;s script was rejected by the app&apos;s checks, so this text was built from the newsletter notes instead. The rejected model output is in the OpenAI response preview.
        </Banner>
      ) : (
        !raw && <Banner tone="info">The raw model response for this script is not available.</Banner>
      )}

      <div className="flex flex-wrap gap-2">
        <Chip active={view === 'readable'} onClick={() => setView('readable')}>
          Readable
        </Chip>
        <Chip active={view === 'raw'} onClick={() => setView('raw')}>
          Raw JSON
        </Chip>
      </div>

      {view === 'readable' ? (
        <ol className="max-h-[50dvh] space-y-3 overflow-y-auto rounded-lg bg-surface-2 p-3" aria-label="Generated script">
          {lines.map((line, i) => (
            <li key={i}>
              <span className={`block text-caption font-semibold ${line.speaker === 'A' ? 'text-accent' : 'text-ink-2'}`}>{line.speakerName}</span>
              {editing ? (
                <textarea
                  value={texts[i]}
                  onChange={(e) => setTexts((t) => t.map((v, j) => (j === i ? e.target.value : v)))}
                  rows={Math.max(2, Math.ceil(texts[i].length / 70))}
                  maxLength={700}
                  aria-label={`${line.speakerName} line ${i + 1}`}
                  className="mt-1 w-full rounded-lg border border-line bg-surface p-2 text-transcript"
                />
              ) : (
                <p className="whitespace-pre-wrap text-transcript">{texts[i]}</p>
              )}
            </li>
          ))}
        </ol>
      ) : raw?.rawResponse ? (
        <pre aria-label="Raw script JSON" className="max-h-[50dvh] overflow-auto whitespace-pre-wrap break-words rounded-lg bg-surface-2 p-3 text-caption">
          {prettyJson(raw.rawResponse)}
        </pre>
      ) : (
        <p className="text-body text-ink-2">No raw response is available for this script.</p>
      )}
      {view === 'raw' && modified && <p className="text-caption text-ink-3">This is the original model response. Your edits are only in the readable view.</p>}

      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" onClick={() => void copy(view === 'raw' && raw?.rawResponse ? raw.rawResponse : scriptToText(lines, texts))}>
          <Copy className="size-4" aria-hidden /> {copied ? 'Copied' : view === 'raw' ? 'Copy raw JSON' : 'Copy script'}
        </Button>
        <Button variant="secondary" disabled={busy} onClick={() => setEditing((v) => !v)}>
          <Pencil className="size-4" aria-hidden /> {editing ? 'Done editing' : 'Edit text'}
        </Button>
        {modified && (
          <Button variant="ghost" disabled={busy} onClick={() => setTexts(lines.map((l) => l.text))}>
            <RotateCcw className="size-4" aria-hidden /> Reset to generated
          </Button>
        )}
        <Button busy={busy} disabled={texts.some((t) => !t.trim())} onClick={() => void generate()}>
          <Volume2 className="size-4" aria-hidden /> Generate Audio
        </Button>
      </div>
      {error && <Banner tone="error">{error}</Banner>}
    </section>
  );
}
