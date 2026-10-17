import { useEffect, useState } from 'react';
import { TldrPodcast } from './TldrPodcast';

type Story = { id: string; title: string; url: string; summary: string; read_minutes: number | null; sponsored: boolean };
type Section = { id?: string; name: string; emoji: string; stories: Story[] };
type Issue = { category: string; date: string; url: string; headline: string; sections: Section[] };
type Digest = { scrapedAt: string; windowHours: number; issues: Issue[] };

const DAY_MS = 24 * 60 * 60 * 1000;

/** Same rule as the server's slugify: "Programming, Design & Data Science" -> "programming-design-data-science". */
export const sectionId = (sec: Pick<Section, 'id' | 'name'>) =>
  sec.id || sec.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 50).replace(/-+$/, '') || 'section';

/** Latest TLDR stories from a static file written by `python -m app.tldr.export`; no API call. */
export function TldrDigest() {
  const [digest, setDigest] = useState<Digest | null>(null);

  useEffect(() => {
    let live = true;
    fetch('/tldr-latest.json', { cache: 'no-cache' })
      .then((r) => (r.ok && (r.headers.get('content-type') || '').includes('json') ? (r.json() as Promise<Digest>) : null))
      .then((d) => {
        if (live && d && Date.now() - new Date(d.scrapedAt).getTime() < DAY_MS && d.issues.length) setDigest(d);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  if (!digest) return null;
  return (
    <section className="mt-8" aria-label="TLDR newsletters from the last 24 hours">
      <h2 className="text-section font-semibold">TLDR, last 24 hours</h2>
      {digest.issues.map((issue) => (
        <div key={issue.url} className="mt-4">
          <a href={issue.url} target="_blank" rel="noopener noreferrer" className="text-body font-medium text-accent hover:underline">
            {issue.date}: {issue.headline}
          </a>
          {issue.sections.map((sec) => {
            const stories = sec.stories.filter((s) => !s.sponsored);
            if (!stories.length) return null;
            return (
              <div key={sec.name} className="mt-3">
                <p className="text-caption font-semibold text-ink-3">
                  {sec.emoji} {sec.name}
                </p>
                <ul className="mt-1 space-y-2">
                  {stories.map((s) => (
                    <li key={s.url}>
                      <a href={s.url} target="_blank" rel="noopener noreferrer" className="text-body font-medium hover:underline">
                        {s.title}
                      </a>
                      {s.read_minutes ? <span className="text-caption text-ink-3"> · {s.read_minutes} min</span> : null}
                      <p className="text-caption text-ink-2">{s.summary.length > 220 ? `${s.summary.slice(0, 220)}…` : s.summary}</p>
                    </li>
                  ))}
                </ul>
                <TldrPodcast category={issue.category} date={issue.date} section={sectionId(sec)} stories={stories.length} />
              </div>
            );
          })}
        </div>
      ))}
    </section>
  );
}
