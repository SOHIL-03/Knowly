import { RotateCcw } from 'lucide-react';
import { useState } from 'react';
import { post } from '../lib/api';
import type { TraceEntry } from '../lib/review';
import type { Segment } from '../lib/types';
import { ScriptReview } from './ScriptReview';
import { Banner, Button, Spinner } from './ui';

/** Shown while a segment has no playable audio. Audio is made by Gemini TTS from the segment's
 * script; the script is readable below the whole time. */
export function AudioPanel({
  briefingId,
  segment,
  trace = [],
  onChanged,
}: {
  briefingId: string;
  segment: Segment;
  trace?: TraceEntry[];
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const retry = async () => {
    setBusy(true);
    setError(null);
    try {
      await post(`/briefings/${encodeURIComponent(briefingId)}/segments/${segment.id}/audio/retry`);
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Retry failed');
    } finally {
      setBusy(false);
    }
  };

  if (segment.audioStatus === 'review') {
    return <ScriptReview key={segment.id} briefingId={briefingId} segment={segment} trace={trace} onChanged={onChanged} />;
  }
  if (segment.audioStatus === 'failed') {
    return (
      <div className="space-y-2">
        <Banner
          tone="warn"
          action={
            <Button variant="secondary" busy={busy} onClick={retry}>
              <RotateCcw className="size-4" aria-hidden /> Retry audio
            </Button>
          }
        >
          {segment.audioMessage || 'Audio could not be made for this segment. The text version is below.'}
        </Banner>
        {error && <Banner tone="error">{error}</Banner>}
      </div>
    );
  }
  return <Spinner label="Making the audio for this segment. You can read along below meanwhile." />;
}
