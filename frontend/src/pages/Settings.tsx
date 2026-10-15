import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { AppShell } from '../components/AppShell';
import { Banner, Button, Card, PageHeader, Segmented, Toggle } from '../components/ui';
import { del, download, get, post, put } from '../lib/api';
import { enablePush } from '../lib/push';
import type { Prefs } from '../lib/types';

type Theme = 'system' | 'light' | 'dark';

export function applyTheme(t: Theme) {
  const root = document.documentElement;
  if (t === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', t);
  try {
    localStorage.setItem('knowly-theme', t);
  } catch {
    /* storage may be unavailable; theme still applies for this visit */
  }
}

interface Sub {
  senderId: string;
  name: string;
  enabled: boolean;
  sample?: boolean;
}

export function Settings() {
  const { me, refreshMe, signOut, linkGoogle } = useAuth();
  const navigate = useNavigate();
  const [project, setProject] = useState(me?.profile?.project?.text || '');
  const [readme, setReadme] = useState(me?.profile?.project?.readmeUrl || '');
  const [goals, setGoals] = useState((me?.profile?.goals || []).map((g) => g.topic).join(', '));
  const [mutes, setMutes] = useState((me?.profile?.muteTopics || []).join(', '));
  const [prefs, setPrefs] = useState<Prefs | undefined>(me?.prefs);
  const [subs, setSubs] = useState<Sub[]>([]);
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return (localStorage.getItem('knowly-theme') as Theme) || 'system';
    } catch {
      return 'system';
    }
  });
  const [msg, setMsg] = useState<{ tone: 'good' | 'error'; text: string } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState('');

  useEffect(() => {
    get<{ newsletters: Sub[] }>('/newsletters')
      .then((r) => setSubs(r.newsletters))
      .catch(() => undefined);
  }, []);

  const run = async (key: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(key);
    setMsg(null);
    try {
      await fn();
      setMsg({ tone: 'good', text: ok });
    } catch (e) {
      setMsg({ tone: 'error', text: e instanceof Error ? e.message : 'Something went wrong' });
    } finally {
      setBusy(null);
    }
  };

  const split = (s: string) => s.split(',').map((x) => x.trim()).filter(Boolean);

  return (
    <AppShell>
      <PageHeader title="Settings" />
      {msg && <div className="mb-4"><Banner tone={msg.tone}>{msg.text}</Banner></div>}

      <div className="space-y-4">
        <Card as="section">
          <h2 className="text-section font-semibold">Project and goals</h2>
          <label className="mt-3 block text-caption font-medium text-ink-2" htmlFor="s-project">
            Current project
          </label>
          <textarea id="s-project" value={project} onChange={(e) => setProject(e.target.value)} rows={3} className="mt-1 w-full rounded-xl border border-line bg-surface p-3 text-body" />
          <input value={readme} onChange={(e) => setReadme(e.target.value)} placeholder="README link (optional)" className="mt-2 min-h-11 w-full rounded-xl border border-line bg-surface px-3 text-body" />
          <label className="mt-3 block text-caption font-medium text-ink-2" htmlFor="s-goals">
            Goals (up to 5, comma separated)
          </label>
          <input id="s-goals" value={goals} onChange={(e) => setGoals(e.target.value)} className="mt-1 min-h-11 w-full rounded-xl border border-line bg-surface px-3 text-body" />
          <label className="mt-3 block text-caption font-medium text-ink-2" htmlFor="s-mutes">
            Topics to skip (comma separated)
          </label>
          <input id="s-mutes" value={mutes} onChange={(e) => setMutes(e.target.value)} className="mt-1 min-h-11 w-full rounded-xl border border-line bg-surface px-3 text-body" />
          <Button
            className="mt-3"
            busy={busy === 'profile'}
            onClick={() =>
              run(
                'profile',
                async () => {
                  await put('/profile', { project: { text: project, readmeUrl: readme || null }, goals: split(goals).slice(0, 5), muteTopics: split(mutes) });
                  await refreshMe();
                },
                'Saved. Tomorrow’s briefing will use this.',
              )
            }
          >
            Save
          </Button>
        </Card>

        <Card as="section">
          <h2 className="text-section font-semibold">Newsletters</h2>
          {me?.mode === 'sample' ? (
            <p className="mt-2 text-body text-ink-2">Sample mode uses three sample newsletters.</p>
          ) : (
            <div className="mt-2">
              {subs.map((s, i) => (
                <Toggle key={s.senderId} label={s.name} checked={s.enabled} onChange={(v) => setSubs(subs.map((x, j) => (j === i ? { ...x, enabled: v } : x)))} />
              ))}
              <Button
                variant="secondary"
                className="mt-2"
                busy={busy === 'subs'}
                onClick={() => run('subs', () => put('/newsletters', { enabled: subs.filter((s) => s.enabled).map((s) => s.senderId) }), 'Newsletters updated')}
              >
                Save newsletters
              </Button>
            </div>
          )}
        </Card>

        {prefs && (
          <Card as="section" className="space-y-4">
            <h2 className="text-section font-semibold">Briefing</h2>
            <Segmented label="Length" value={prefs.lengthMin} onChange={(v) => setPrefs({ ...prefs, lengthMin: v })} options={[5, 10, 15].map((m) => ({ value: m as 5 | 10 | 15, label: `${m} min` }))} />
            <Segmented
              label="Style"
              value={prefs.style}
              onChange={(v) => setPrefs({ ...prefs, style: v })}
              options={[
                { value: 'twoHost', label: 'Two hosts' },
                { value: 'brief', label: 'One narrator' },
              ]}
            />
            <Segmented
              label="Schedule"
              value={prefs.schedule.mode}
              onChange={(v) => setPrefs({ ...prefs, schedule: { ...prefs.schedule, mode: v } })}
              options={[
                { value: 'daily', label: 'Daily' },
                { value: 'onArrival', label: 'When newsletters arrive' },
              ]}
            />
            <label className="block text-body">
              Time
              <input type="time" value={prefs.schedule.time} onChange={(e) => setPrefs({ ...prefs, schedule: { ...prefs.schedule, time: e.target.value } })} className="ml-3 min-h-11 rounded-xl border border-line bg-surface px-3" />
            </label>
            <Toggle label="Include a wildcard story" description="One story from outside your usual topics" checked={prefs.wildcardOn} onChange={(v) => setPrefs({ ...prefs, wildcardOn: v })} />
            <Toggle label="Closing quiz round" description="Two questions at the end" checked={prefs.endQuizOn} onChange={(v) => setPrefs({ ...prefs, endQuizOn: v })} />
            <Toggle
              label="Push notification when ready"
              checked={prefs.notify === 'push'}
              onChange={(v) => {
                setPrefs({ ...prefs, notify: v ? 'push' : 'none' });
                if (v) void enablePush().then((ok) => !ok && setMsg({ tone: 'error', text: 'Push notifications are not available in this browser or deployment.' }));
              }}
            />
            <Button busy={busy === 'prefs'} onClick={() => run('prefs', async () => { await put('/prefs', { prefs }); await refreshMe(); }, 'Preferences saved')}>
              Save preferences
            </Button>
          </Card>
        )}

        <Card as="section">
          <h2 className="text-section font-semibold">Appearance</h2>
          <div className="mt-3">
            <Segmented
              label="Theme"
              value={theme}
              onChange={(t) => {
                setTheme(t);
                applyTheme(t);
              }}
              options={[
                { value: 'system', label: 'System' },
                { value: 'light', label: 'Light' },
                { value: 'dark', label: 'Dark' },
              ]}
            />
          </div>
        </Card>

        <Card as="section" className="space-y-3">
          <h2 className="text-section font-semibold">Account and privacy</h2>
          {me?.anonymous && (
            <div>
              <p className="text-body text-ink-2">You are using a sample account. Sign in with Google to keep your progress across devices.</p>
              <Button variant="secondary" className="mt-2" onClick={() => run('link', linkGoogle, 'Signed in with Google; your data is kept')}>
                Sign in with Google
              </Button>
            </div>
          )}
          <div>
            <p className="text-body">
              Gmail: <span className="font-medium">{me?.gmail?.status === 'ok' ? `connected${me.gmail.email ? ` (${me.gmail.email})` : ''}` : me?.gmail?.status === 'reauth' ? 'needs reconnecting' : 'not connected'}</span>
            </p>
            {me?.gmail?.status === 'ok' ? (
              <Button variant="secondary" className="mt-2" busy={busy === 'gmail'} onClick={() => run('gmail', async () => { await post('/auth/gmail/disconnect'); await refreshMe(); }, 'Gmail disconnected and its token deleted. Your Library is kept.')}>
                Disconnect Gmail
              </Button>
            ) : (
              <Button variant="secondary" className="mt-2" onClick={() => navigate('/connect-gmail')}>
                Connect Gmail
              </Button>
            )}
          </div>
          <div>
            <Button variant="secondary" busy={busy === 'export'} onClick={() => run('export', () => download('/export', 'knowly-export.json'), 'Your data was downloaded')}>
              Download my data
            </Button>
          </div>
          <div className="rounded-xl border border-notyet/40 p-3">
            <p className="text-body font-medium">Delete all my data</p>
            <p className="mt-1 text-caption text-ink-2">Removes your briefings, audio, Library, learner model, Gmail connection and account. This cannot be undone. Type DELETE to confirm.</p>
            <div className="mt-2 flex gap-2">
              <input value={confirmDelete} onChange={(e) => setConfirmDelete(e.target.value)} aria-label="Type DELETE to confirm" className="min-h-11 w-32 rounded-xl border border-line bg-surface px-3 text-body" />
              <Button
                variant="danger"
                disabled={confirmDelete !== 'DELETE'}
                busy={busy === 'delete'}
                onClick={() =>
                  run(
                    'delete',
                    async () => {
                      await del('/account');
                      await signOut();
                      navigate('/', { replace: true });
                    },
                    'All your data was deleted',
                  )
                }
              >
                Delete everything
              </Button>
            </div>
          </div>
          <Button variant="ghost" onClick={() => void signOut().then(() => navigate('/', { replace: true }))}>
            Sign out
          </Button>
        </Card>
      </div>
    </AppShell>
  );
}
