import { Plus, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { Banner, Button, Card, Chip, Segmented, Spinner, Toggle } from '../components/ui';
import { get, post, put } from '../lib/api';
import { enablePush } from '../lib/push';
import type { Prefs } from '../lib/types';

const GOAL_SUGGESTIONS = ['LLM evals', 'RAG', 'vector search', 'agents', 'system design', 'Kubernetes', 'serverless', 'web performance', 'security'];
const MUTE_SUGGESTIONS = ['funding news', 'crypto', 'hiring', 'politics'];
const STEPS = ['Newsletters', 'Goals and project', 'Level check', 'Preferences'];

interface Sender {
  senderId: string;
  name: string;
  address?: string;
  issueCount?: number;
  enabled: boolean;
  isKnownTech?: boolean;
  sample?: boolean;
}

/** Four steps, one question per screen, optional steps skippable, Back always available (section 11.4). */
export function Onboarding() {
  const { me, refreshMe } = useAuth();
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // step 1
  const [senders, setSenders] = useState<Sender[] | null>(null);
  const [manual, setManual] = useState('');
  const [manualList, setManualList] = useState<string[]>([]);
  // step 2
  const [project, setProject] = useState(me?.profile?.project?.text || '');
  const [readme, setReadme] = useState(me?.profile?.project?.readmeUrl || '');
  const [goals, setGoals] = useState<string[]>(me?.profile?.goals?.map((g) => g.topic) || []);
  const [goalInput, setGoalInput] = useState('');
  const [mutes, setMutes] = useState<string[]>(me?.profile?.muteTopics || []);
  // step 3
  const [concepts, setConcepts] = useState<string[] | null>(null);
  const [levels, setLevels] = useState<Record<string, 'new' | 'heard' | 'use'>>({});
  // step 4
  const [prefs, setPrefs] = useState<Prefs>(
    me?.prefs || { lengthMin: 10, style: 'twoHost', speed: 1, schedule: { mode: 'daily', time: '07:00', days: [0, 1, 2, 3, 4, 5, 6] }, notify: 'push', wildcardOn: true, endQuizOn: true },
  );

  const live = me?.mode === 'live' && me.gmail?.status === 'ok';

  useEffect(() => {
    if (step !== 0 || senders) return;
    (live ? get<{ senders: Sender[] }>('/newsletters/detected').then((r) => r.senders) : get<{ newsletters: Sender[] }>('/newsletters').then((r) => r.newsletters))
      .then(setSenders)
      .catch((e: Error) => setError(e.message));
  }, [step, senders, live]);

  useEffect(() => {
    if (step !== 2 || concepts) return;
    get<{ concepts: string[] }>('/profile/levelcheck/suggestions')
      .then((r) => setConcepts(r.concepts))
      .catch(() => setConcepts([]));
  }, [step, concepts]);

  const next = async () => {
    setBusy(true);
    setError(null);
    try {
      if (step === 0 && live && senders) {
        await put('/newsletters', { enabled: senders.filter((s) => s.enabled).map((s) => s.senderId), manual: manualList });
      }
      if (step === 1) {
        await put('/profile', { project: { text: project, readmeUrl: readme || null }, goals, muteTopics: mutes });
      }
      if (step === 2 && Object.keys(levels).length) {
        await post('/profile/levelcheck', { ratings: Object.entries(levels).map(([name, level]) => ({ name, level })) });
      }
      if (step === 3) {
        await put('/prefs', { prefs, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, onboarded: true });
        if (prefs.notify === 'push') await enablePush().catch(() => undefined);
        await refreshMe();
        const r = await post<{ briefingId: string }>('/briefings/generate', { kind: 'daily' });
        navigate(`/generating/${encodeURIComponent(r.briefingId)}`, { replace: true });
        return;
      }
      setStep(step + 1);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save');
    } finally {
      setBusy(false);
    }
  };

  const addGoal = (g: string) => {
    const t = g.trim();
    if (t && !goals.includes(t) && goals.length < 5) setGoals([...goals, t]);
    setGoalInput('');
  };

  return (
    <div className="mx-auto flex min-h-dvh max-w-xl flex-col px-4 py-6">
      <div className="flex items-center gap-2" aria-label={`Step ${step + 1} of 4`}>
        {STEPS.map((s, i) => (
          <div key={s} className={`h-1.5 flex-1 rounded-full ${i <= step ? 'bg-accent' : 'bg-line'}`} />
        ))}
      </div>
      <p className="mt-3 text-caption text-ink-3">
        Step {step + 1} of 4 · {STEPS[step]}
      </p>

      <div className="mt-4 flex-1">
        {step === 0 && (
          <section>
            <h1 className="text-title font-bold">Which newsletters should Knowly read?</h1>
            {!live && (
              <p className="mt-2 text-body text-ink-2">
                You are in sample mode: briefings use three sample newsletters and no inbox access.{' '}
                {!me?.anonymous && (
                  <button className="text-accent underline" onClick={() => navigate('/connect-gmail')}>
                    Connect Gmail instead
                  </button>
                )}
              </p>
            )}
            <div className="mt-4 space-y-2">
              {!senders ? (
                <Spinner label={live ? 'Looking for newsletters in the last 30 days' : 'Loading'} />
              ) : senders.length === 0 ? (
                <Banner tone="info">No newsletters found in the last 30 days. Add a sender by email address below.</Banner>
              ) : (
                senders.map((s, i) => (
                  <Card key={s.senderId} className="py-2">
                    <Toggle
                      label={s.name}
                      description={s.sample ? 'Sample newsletter' : `${s.address} · ${s.issueCount} issues in 30 days${s.isKnownTech ? ' · tech' : ''}`}
                      checked={s.enabled}
                      onChange={(v) => !s.sample && setSenders(senders.map((x, j) => (j === i ? { ...x, enabled: v } : x)))}
                    />
                  </Card>
                ))
              )}
            </div>
            {live && (
              <form
                className="mt-4 flex gap-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (manual.includes('@')) setManualList([...manualList, manual.trim()]);
                  setManual('');
                }}
              >
                <input value={manual} onChange={(e) => setManual(e.target.value)} type="email" placeholder="Add sender by email address" className="min-h-11 flex-1 rounded-xl border border-line bg-surface px-3 text-body" />
                <Button type="submit" variant="secondary">
                  Add
                </Button>
              </form>
            )}
            {manualList.length > 0 && <p className="mt-2 text-caption text-ink-2">Will also follow: {manualList.join(', ')}</p>}
          </section>
        )}

        {step === 1 && (
          <section className="space-y-5">
            <h1 className="text-title font-bold">What are you building and learning?</h1>
            <div>
              <label htmlFor="project" className="text-body font-medium">
                Your current project
              </label>
              <textarea
                id="project"
                value={project}
                onChange={(e) => setProject(e.target.value)}
                rows={3}
                maxLength={4000}
                placeholder="e.g. A RAG chatbot over our docs, FastAPI on Cloud Run, Firestore for storage"
                className="mt-2 w-full rounded-xl border border-line bg-surface p-3 text-body"
              />
              <input value={readme} onChange={(e) => setReadme(e.target.value)} type="url" placeholder="README link (optional, https://…)" className="mt-2 min-h-11 w-full rounded-xl border border-line bg-surface px-3 text-body" />
            </div>
            <div>
              <p className="text-body font-medium">Up to 5 learning goals</p>
              <div className="mt-2 flex flex-wrap gap-2">
                {goals.map((g) => (
                  <Chip key={g} active onClick={() => setGoals(goals.filter((x) => x !== g))}>
                    {g} <X className="size-3.5" aria-label="remove" />
                  </Chip>
                ))}
                {GOAL_SUGGESTIONS.filter((g) => !goals.includes(g)).map((g) => (
                  <Chip key={g} onClick={() => addGoal(g)}>
                    <Plus className="size-3.5" aria-hidden /> {g}
                  </Chip>
                ))}
              </div>
              <form
                className="mt-2 flex gap-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  addGoal(goalInput);
                }}
              >
                <input value={goalInput} onChange={(e) => setGoalInput(e.target.value)} maxLength={80} placeholder="Add your own goal" className="min-h-11 flex-1 rounded-xl border border-line bg-surface px-3 text-body" disabled={goals.length >= 5} />
                <Button type="submit" variant="secondary" disabled={goals.length >= 5}>
                  Add
                </Button>
              </form>
            </div>
            <div>
              <p className="text-body font-medium">Topics to skip</p>
              <div className="mt-2 flex flex-wrap gap-2">
                {MUTE_SUGGESTIONS.map((m) => (
                  <Chip key={m} active={mutes.includes(m)} onClick={() => setMutes(mutes.includes(m) ? mutes.filter((x) => x !== m) : [...mutes, m])}>
                    {m}
                  </Chip>
                ))}
              </div>
            </div>
          </section>
        )}

        {step === 2 && (
          <section>
            <h1 className="text-title font-bold">How well do you know these?</h1>
            <p className="mt-2 text-body text-ink-2">Optional. It sets where your briefings start; quizzes correct it over time.</p>
            <div className="mt-4 space-y-3">
              {!concepts ? (
                <Spinner />
              ) : concepts.length === 0 ? (
                <Banner tone="info">Add goals or a project first, or skip this step.</Banner>
              ) : (
                concepts.map((c) => (
                  <Card key={c} className="py-3">
                    <p className="mb-2 text-body font-medium">{c}</p>
                    <Segmented
                      label={`How well you know ${c}`}
                      value={levels[c] || ('' as never)}
                      onChange={(v) => setLevels({ ...levels, [c]: v })}
                      options={[
                        { value: 'new', label: 'New to me' },
                        { value: 'heard', label: 'Heard of it' },
                        { value: 'use', label: 'I use it' },
                      ]}
                    />
                  </Card>
                ))
              )}
            </div>
          </section>
        )}

        {step === 3 && (
          <section className="space-y-5">
            <h1 className="text-title font-bold">How should your briefing sound?</h1>
            <div>
              <p className="mb-2 text-body font-medium">Length</p>
              <Segmented label="Length" value={prefs.lengthMin} onChange={(v) => setPrefs({ ...prefs, lengthMin: v })} options={[5, 10, 15].map((m) => ({ value: m as 5 | 10 | 15, label: `${m} min` }))} />
            </div>
            <div>
              <p className="mb-2 text-body font-medium">Style</p>
              <Segmented
                label="Style"
                value={prefs.style}
                onChange={(v) => setPrefs({ ...prefs, style: v })}
                options={[
                  { value: 'twoHost', label: 'Two hosts' },
                  { value: 'brief', label: 'One narrator' },
                ]}
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <label className="text-body font-medium">
                Daily at
                <input type="time" value={prefs.schedule.time} onChange={(e) => setPrefs({ ...prefs, schedule: { ...prefs.schedule, time: e.target.value } })} className="mt-2 min-h-11 w-full rounded-xl border border-line bg-surface px-3" />
              </label>
              <label className="text-body font-medium">
                Notify me
                <select value={prefs.notify} onChange={(e) => setPrefs({ ...prefs, notify: e.target.value as Prefs['notify'] })} className="mt-2 min-h-11 w-full rounded-xl border border-line bg-surface px-3">
                  <option value="push">Push</option>
                  <option value="none">No</option>
                </select>
              </label>
            </div>
            <p className="text-caption text-ink-3">Timezone: {Intl.DateTimeFormat().resolvedOptions().timeZone}</p>
          </section>
        )}
      </div>

      {error && <div className="mb-3"><Banner tone="error">{error}</Banner></div>}
      <div className="sticky bottom-0 flex gap-2 bg-bg py-4">
        {step > 0 && (
          <Button variant="secondary" onClick={() => setStep(step - 1)}>
            Back
          </Button>
        )}
        {step === 2 && (
          <Button variant="ghost" onClick={() => setStep(3)}>
            Skip
          </Button>
        )}
        <Button full busy={busy} onClick={next} disabled={step === 0 && !senders}>
          {step === 3 ? 'Make my first briefing' : 'Continue'}
        </Button>
      </div>
    </div>
  );
}
