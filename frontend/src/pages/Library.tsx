import { Search, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { AppShell } from '../components/AppShell';
import { SourceChip } from '../components/SourceChip';
import { Banner, Card, PageHeader, Segmented, Skeleton } from '../components/ui';
import { del, get } from '../lib/api';
import type { LibraryItem, QaRecord } from '../lib/types';

interface LibraryResponse {
  items: LibraryItem[];
  qa: QaRecord[];
  facets: { topics: string[]; newsletters: { id: string; name: string }[] };
}

export function Library() {
  const [tab, setTab] = useState<'stories' | 'qa'>('stories');
  const [q, setQ] = useState('');
  const [topic, setTopic] = useState('');
  const [newsletter, setNewsletter] = useState('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [data, setData] = useState<LibraryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [facets, setFacets] = useState<LibraryResponse['facets']>({ topics: [], newsletters: [] });

  const load = async () => {
    const params = new URLSearchParams();
    if (q) params.set('q', q);
    if (topic) params.set('topic', topic);
    if (newsletter) params.set('newsletter', newsletter);
    if (from) params.set('from', from);
    if (to) params.set('to', to);
    try {
      const r = await get<LibraryResponse>(`/library?${params}`);
      setData(r);
      if (!q && !topic && !newsletter && !from && !to) setFacets(r.facets);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load your Library');
    }
  };

  useEffect(() => {
    const t = setTimeout(() => void load(), 250);
    return () => clearTimeout(t);
  }, [q, topic, newsletter, from, to]);

  const remove = async (id: string) => {
    await del(`/library/${encodeURIComponent(id)}`);
    void load();
  };

  return (
    <AppShell>
      <PageHeader title="Library" subtitle="Stories you saved and questions you asked" />
      <div className="space-y-3">
        <label className="flex min-h-11 items-center gap-2 rounded-xl border border-line bg-surface px-3">
          <Search className="size-5 text-ink-3" aria-hidden />
          <span className="sr-only">Search</span>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search your Library" className="min-h-11 flex-1 bg-transparent text-body outline-none" />
        </label>
        <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
          <select aria-label="Topic" value={topic} onChange={(e) => setTopic(e.target.value)} className="min-h-11 rounded-xl border border-line bg-surface px-3 text-body">
            <option value="">All topics</option>
            {facets.topics.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
          <select aria-label="Newsletter" value={newsletter} onChange={(e) => setNewsletter(e.target.value)} className="min-h-11 rounded-xl border border-line bg-surface px-3 text-body">
            <option value="">All newsletters</option>
            {facets.newsletters.map((n) => (
              <option key={n.id} value={n.id}>
                {n.name}
              </option>
            ))}
          </select>
          <input aria-label="From date" type="date" value={from} onChange={(e) => setFrom(e.target.value)} className="min-h-11 rounded-xl border border-line bg-surface px-3 text-body" />
          <input aria-label="To date" type="date" value={to} onChange={(e) => setTo(e.target.value)} className="min-h-11 rounded-xl border border-line bg-surface px-3 text-body" />
        </div>
        <Segmented
          label="Library section"
          value={tab}
          onChange={setTab}
          options={[
            { value: 'stories', label: `Stories${data ? ` (${data.items.length})` : ''}` },
            { value: 'qa', label: `Q&A${data ? ` (${data.qa.length})` : ''}` },
          ]}
        />
      </div>

      <div className="mt-4">
        {error && <Banner tone="error">{error}</Banner>}
        {!data && !error && <Skeleton className="h-24" />}
        {data && tab === 'stories' && (
          data.items.length === 0 ? (
            <Card className="text-center text-body text-ink-2">
              Nothing saved yet. Tap <span className="font-semibold">Save</span> at a checkpoint to keep a story here; it comes back as a short review in 1, 3 and 7 days.
            </Card>
          ) : (
            <ul className="space-y-3">
              {data.items.map((it) => (
                <li key={it.id}>
                  <Card>
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="text-body font-semibold">{it.headline}</p>
                        <p className="mt-1 text-caption text-ink-2">{it.blurb}</p>
                        {it.note && <p className="mt-2 rounded-lg bg-surface-2 p-2 text-caption">Note: {it.note}</p>}
                        <p className="mt-2 text-caption text-ink-3">
                          Saved {new Date(it.savedAt).toLocaleDateString()}
                          {it.source === 'deeper' && ' · from Go deeper'}
                          {it.reviewDueAt && ` · review ${new Date(it.reviewDueAt).toLocaleDateString()}`}
                        </p>
                      </div>
                      <button onClick={() => void remove(it.id)} className="flex size-11 shrink-0 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2" aria-label={`Remove ${it.headline}`}>
                        <Trash2 className="size-4" />
                      </button>
                    </div>
                    <div className="mt-2">
                      <SourceChip newsletter={it.senderName} url={it.url} itemId={it.itemId} headline={it.headline} />
                    </div>
                  </Card>
                </li>
              ))}
            </ul>
          )
        )}
        {data && tab === 'qa' && (
          data.qa.length === 0 ? (
            <Card className="text-center text-body text-ink-2">Questions you ask at checkpoints appear here with their sources.</Card>
          ) : (
            <ul className="space-y-3">
              {data.qa.map((x) => (
                <li key={x.id}>
                  <Card>
                    <p className="text-caption text-ink-3">
                      {x.kind === 'apply' ? 'Apply to my project' : 'You asked'} · {new Date(x.ts).toLocaleDateString()}
                    </p>
                    <p className="mt-1 text-body font-semibold">{x.question}</p>
                    {x.kind === 'ask' && <p className="mt-2 text-body text-ink-2">{x.answer.replace(/\s?\[it_[A-Za-z0-9_]+\]/g, '')}</p>}
                    <div className="mt-2 flex flex-wrap gap-2">
                      {x.citations?.map((c) => (
                        <SourceChip key={c.itemId} newsletter={c.newsletter} headline={c.headline} url={c.url} itemId={c.itemId} />
                      ))}
                    </div>
                  </Card>
                </li>
              ))}
            </ul>
          )
        )}
      </div>
    </AppShell>
  );
}
