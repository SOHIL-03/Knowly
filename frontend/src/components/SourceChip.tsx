import { ExternalLink } from 'lucide-react';
import { track } from '../lib/events';

interface Props {
  newsletter: string;
  headline?: string;
  url: string | null;
  itemId: string;
  briefingId?: string;
  segId?: string;
  linkOnly?: boolean;
}

/** Newsletter name chip; opens the original link in a new tab and logs openSource. */
export function SourceChip({ newsletter, headline, url, itemId, briefingId, segId, linkOnly }: Props) {
  const label = (
    <>
      <span className="truncate">{newsletter}</span>
      {linkOnly && <span className="text-ink-3">· note only</span>}
      {url && <ExternalLink className="size-3.5 shrink-0" aria-hidden />}
    </>
  );
  const cls = 'inline-flex min-h-9 max-w-full items-center gap-1.5 rounded-full border border-line bg-surface px-3 text-caption font-medium text-ink-2';
  if (!url) {
    return (
      <span className={cls} title={headline ? `${headline} (no link in the newsletter)` : 'No link in the newsletter'}>
        {label}
      </span>
    );
  }
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      title={headline}
      className={`${cls} hover:border-accent/60 hover:text-accent`}
      onClick={() => track({ type: 'openSource', itemId, briefingId, segId })}
    >
      {label}
    </a>
  );
}
