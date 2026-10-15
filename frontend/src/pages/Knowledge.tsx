import { useEffect, useState } from 'react';
import { AppShell } from '../components/AppShell';
import { Banner, Card, MasteryBar, PageHeader, Segmented, Skeleton } from '../components/ui';
import { get, patch } from '../lib/api';
import type { Concept } from '../lib/types';

const bandText = {
  define: 'Briefings define this with an analogy',
  remind: 'Briefings use it with a short reminder',
  skip: 'Briefings skip the basics',
};

/** What Knowly thinks you know (section 11.4, screen 12). Corrections always win over inference. */
export function Knowledge() {
  const [concepts, setConcepts] = useState<Concept[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);

  const load = () =>
    get<{ concepts: Concept[] }>('/knowledge')
      .then((r) => setConcepts(r.concepts))
      .catch((e: Error) => setError(e.message));

  useEffect(() => {
    void load();
  }, []);

  const correct = async (id: string, level: 'new' | 'heard' | 'use') => {
    await patch(`/knowledge/${encodeURIComponent(id)}`, { level });
    setEditing(null);
    void load();
  };

  return (
    <AppShell>
      <PageHeader title="What Knowly thinks you know" subtitle="Estimated from your level check, quizzes and questions. Knowly uses it to decide what to explain and what to skip." />
      {error && <Banner tone="error">{error}</Banner>}
      {!concepts && !error && <Skeleton className="h-40" />}
      {concepts?.length === 0 && <Card className="text-body text-ink-2">Nothing yet. Concepts appear after your first briefing or level check.</Card>}
      <ul className="space-y-3">
        {concepts?.map((c) => (
          <li key={c.id}>
            <Card>
              <div className="flex items-baseline justify-between gap-3">
                <p className="text-body font-semibold">{c.name}</p>
                <button className="min-h-9 text-caption text-accent underline-offset-2 hover:underline" onClick={() => setEditing(editing === c.id ? null : c.id)}>
                  {editing === c.id ? 'Cancel' : 'Correct this'}
                </button>
              </div>
              <div className="mt-2">
                <MasteryBar value={c.mastery} label={c.name} />
              </div>
              <p className="mt-2 text-caption text-ink-3">
                {c.evidenceCount} {c.evidenceCount === 1 ? 'signal' : 'signals'} · {bandText[c.band]}
                {c.nextDueAt && ` · next review ${new Date(c.nextDueAt).toLocaleDateString()}`}
                {c.userCorrected && ' · set by you'}
              </p>
              {editing === c.id && (
                <div className="mt-3">
                  <Segmented
                    label={`Your level for ${c.name}`}
                    value={(c.selfRating || '') as 'new'}
                    onChange={(v) => void correct(c.id, v)}
                    options={[
                      { value: 'new', label: 'New to me' },
                      { value: 'heard', label: 'Heard of it' },
                      { value: 'use', label: 'I use it' },
                    ]}
                  />
                </div>
              )}
            </Card>
          </li>
        ))}
      </ul>
    </AppShell>
  );
}
