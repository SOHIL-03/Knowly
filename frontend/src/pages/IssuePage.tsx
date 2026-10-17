import { useCallback, useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { Header, HeadphonesIcon } from '../components/demo/Header';
import { PlayerModal } from '../components/demo/PlayerModal';
import { PreferenceModal } from '../components/demo/PreferenceModal';
import { QuizFlow, QuizPrompt } from '../components/demo/QuizModals';
import { WaitlistModal } from '../components/demo/WaitlistModal';
import { firstWords, loadPreference, longDate, preferenceText, savePreference, stripEmoji, type Preference } from '../lib/demo';
import { fetchIssue, sectionPodcastStatus, type Issue, type Section } from '../lib/tldrApi';
import '../demo.css';

type Modal =
  | { kind: 'pref'; section: Section | null } // null = editing the preference only
  | { kind: 'player'; section: Section; preference: string }
  | { kind: 'quizPrompt'; section: Section; pid: string }
  | { kind: 'quiz'; section: Section; pid: string }
  | { kind: 'waitlist' };

const podcastMinutes = (s: Section) => (s.stories.length >= 5 ? 5 : 3);

export function IssuePage() {
  const { date } = useParams();
  const [issue, setIssue] = useState<Issue | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pref, setPref] = useState<Preference | null>(() => loadPreference());
  const [modal, setModal] = useState<Modal | null>(null);
  const [ready, setReady] = useState<Record<string, true>>({}); // `${preference}\n${sectionId}` -> a podcast exists for it
  const [attempt, setAttempt] = useState(0);

  const prefText = preferenceText(pref);
  const readyKey = (sectionId: string, text = prefText) => `${text}\n${sectionId}`;

  useEffect(() => {
    let live = true;
    setIssue(null);
    setError(null);
    fetchIssue(date)
      .then((i) => live && setIssue(i))
      .catch((e) => live && setError(e instanceof Error ? e.message : 'Could not load the issue'));
    return () => {
      live = false;
    };
  }, [date, attempt]);

  // The "Ready" badge for the general podcasts that already exist (made ahead with `python -m app.tldr.warm`).
  useEffect(() => {
    if (!issue) return;
    let live = true;
    issue.sections.forEach((s) =>
      sectionPodcastStatus(issue, s.id)
        .then((p) => live && p.status === 'ready' && setReady((r) => ({ ...r, [readyKey(s.id, '')]: true })))
        .catch(() => {}),
    );
    return () => {
      live = false;
    };
  }, [issue]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    document.title = 'Today’s TLDR · Knowly';
  }, []);

  const closeModal = useCallback(() => setModal(null), []);
  const openCard = (s: Section) => (ready[readyKey(s.id)] ? setModal({ kind: 'player', section: s, preference: prefText }) : setModal({ kind: 'pref', section: s }));

  const onPrefDone = (p: Preference, section: Section | null) => {
    setPref(p);
    savePreference(p);
    setModal(section ? { kind: 'player', section, preference: preferenceText(p) } : null);
  };

  const headline = issue ? stripEmoji(issue.headline) : '';

  return (
    <div className="kn">
      <Header showHome onGmail={() => setModal({ kind: 'waitlist' })} />
      <main style={{ maxWidth: 'var(--kn-max)', margin: '0 auto', padding: '40px clamp(16px, 4vw, 96px) 64px' }}>
        {error && (
          <div role="alert" className="card" style={{ padding: 28, textAlign: 'center' }}>
            <p style={{ fontSize: 18, fontWeight: 700 }}>Today’s TLDR could not be loaded</p>
            <p style={{ margin: '8px 0 18px', fontSize: 15, color: 'var(--text-2)' }}>{error}</p>
            <button className="btn btn-primary" onClick={() => setAttempt((n) => n + 1)}>Try again</button>
          </div>
        )}
        {!issue && !error && (
          <div role="status" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 12, padding: '96px 0', color: 'var(--text-2)' }}>
            <div className="spin" style={{ width: 28, height: 28, borderRadius: 999, border: '3px solid var(--accent-soft)', borderTopColor: 'var(--accent)' }} />
            Loading the latest issue…
          </div>
        )}

        {issue && (
          <>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 24, alignItems: 'flex-end', justifyContent: 'space-between' }}>
              <div style={{ flex: '999 1 560px', minWidth: 0 }}>
                <p style={{ fontSize: 14, fontWeight: 600, color: 'var(--kn-muted)' }}>Sample newsletter · TLDR Tech · {longDate(issue.date)}{date ? '' : ' (latest issue)'}</p>
                <h1 style={{ margin: '10px 0 0', fontSize: 'clamp(30px, 3.4vw, 64px)', lineHeight: 1.1, fontWeight: 800, letterSpacing: '-0.02em' }}>Today’s TLDR</h1>
                {headline && <p style={{ margin: '8px 0 0', fontSize: 18, fontWeight: 600, color: 'var(--text-2)' }}>{headline}</p>}
                <p style={{ margin: '14px 0 0', fontSize: 16, lineHeight: 1.6, color: 'var(--text-2)', maxWidth: 680 }}>
                  Choose a section and Knowly turns it into a short conversation between Maya and Theo. You can pause to ask questions, and take a quick quiz at the end.
                </p>
              </div>
              <a className="btn" href={issue.url} target="_blank" rel="noopener noreferrer">
                Read the full issue
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M7 17 17 7" /><path d="M8 7h9v9" /></svg>
              </a>
            </div>

            <div className="card" style={{ marginTop: 24, padding: '14px 18px', display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'center', justifyContent: 'space-between' }}>
              <p style={{ fontSize: 15, color: 'var(--text-2)', minWidth: 0, flex: '1 1 360px' }}>
                <strong style={{ color: 'var(--text)' }}>Your preference:</strong>{' '}
                {prefText ? `“${firstWords(prefText)}”` : 'None yet. You’ll be asked when you create your first podcast.'}
              </p>
              <button className="btn btn-ghost" onClick={() => setModal({ kind: 'pref', section: null })} style={{ minHeight: 36, color: 'var(--accent)' }}>
                {prefText ? 'Edit' : 'Add one now'}
              </button>
            </div>

            <div style={{ marginTop: 24, display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(min(clamp(340px, 22vw, 480px), 100%), 1fr))', gap: 20 }}>
              {issue.sections.map((s, i) => {
                const done = !!ready[readyKey(s.id)];
                const more = s.stories.length - 3;
                return (
                  <article key={s.id} className="card" style={{ padding: 22, display: 'flex', flexDirection: 'column', gap: 14 }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}>
                      <div>
                        <p style={{ fontSize: 12, fontWeight: 700, color: 'var(--kn-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Section {i + 1} of {issue.sections.length}</p>
                        <h2 style={{ margin: '6px 0 0', fontSize: 20, fontWeight: 800, letterSpacing: '-0.01em' }}>{s.name}</h2>
                      </div>
                      {done && <span className="badge badge-good">Ready</span>}
                    </div>
                    <p style={{ fontSize: 14, color: 'var(--kn-muted)' }}>{s.stories.length} {s.stories.length === 1 ? 'story' : 'stories'} · about {podcastMinutes(s)} min podcast</p>
                    <ul style={{ display: 'flex', flexDirection: 'column', gap: 10, flex: 1 }}>
                      {s.stories.slice(0, 3).map((st) => (
                        <li key={st.id} style={{ fontSize: 15, lineHeight: 1.45, paddingLeft: 14, borderLeft: '2px solid var(--border)' }}>
                          {st.title}
                          {st.read_minutes ? <span style={{ color: 'var(--kn-muted)', fontSize: 13 }}> · {st.read_minutes} min</span> : null}
                        </li>
                      ))}
                      {more > 0 && <li style={{ fontSize: 14, color: 'var(--kn-muted)', paddingLeft: 14 }}>+{more} more {more === 1 ? 'story' : 'stories'}</li>}
                    </ul>
                    <button className="btn btn-primary" onClick={() => openCard(s)} style={{ width: '100%' }}>
                      <HeadphonesIcon />
                      {done ? 'Play podcast' : 'Create podcast'}
                    </button>
                  </article>
                );
              })}
            </div>
            <p style={{ margin: '20px 0 0', fontSize: 13, color: 'var(--kn-muted)' }}>Sponsored items are left out.</p>
          </>
        )}
      </main>

      {modal?.kind === 'waitlist' && <WaitlistModal onClose={closeModal} />}
      {modal?.kind === 'pref' && (
        <PreferenceModal
          section={modal.section?.name ?? 'Listening preference'}
          initial={pref}
          submitLabel={modal.section ? 'Create podcast' : 'Save preference'}
          onCancel={closeModal}
          onCreate={(p) => onPrefDone(p, modal.section)}
        />
      )}
      {modal?.kind === 'player' && issue && (
        <PlayerModal
          key={`${modal.section.id}\n${modal.preference}`}
          issue={issue}
          section={modal.section}
          preference={modal.preference}
          onClose={(pid) => {
            if (!pid) return setModal(null);
            setReady((r) => ({ ...r, [readyKey(modal.section.id, modal.preference)]: true }));
            setModal({ kind: 'quizPrompt', section: modal.section, pid });
          }}
          onQuiz={(pid) => setModal({ kind: 'quiz', section: modal.section, pid })}
        />
      )}
      {modal?.kind === 'quizPrompt' && (
        <QuizPrompt section={modal.section.name} onNo={closeModal} onYes={() => setModal({ kind: 'quiz', section: modal.section, pid: modal.pid })} />
      )}
      {modal?.kind === 'quiz' && <QuizFlow pid={modal.pid} section={modal.section.name} onClose={closeModal} />}
    </div>
  );
}
