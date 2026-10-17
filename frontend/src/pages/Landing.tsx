import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Header, MailIcon } from '../components/demo/Header';
import { WaitlistModal } from '../components/demo/WaitlistModal';
import '../demo.css';

const STEPS = [
  ['Pick a section', 'Each section of the issue becomes its own short podcast.'],
  ['Tell Maya what you care about', 'Optional. Skip it for a general tech conversation.'],
  ['Listen, ask, get quizzed', 'Pause to ask a question. A short quiz tunes your next podcasts.'],
];

export function Landing() {
  const [waitlist, setWaitlist] = useState(false);

  return (
    <div className="kn">
      <Header onGmail={() => setWaitlist(true)} />
      <main style={{ maxWidth: 'var(--kn-max)', margin: '0 auto', padding: 'clamp(32px, 6vw, 80px) clamp(16px, 4vw, 96px) 64px' }}>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 48, alignItems: 'center' }}>
          <div style={{ flex: '999 1 520px', minWidth: 0 }}>
            <p style={{ fontSize: 14, fontWeight: 700, color: 'var(--accent)', letterSpacing: '0.04em', textTransform: 'uppercase' }}>Newsletters you can listen to</p>
            <h1 style={{ margin: '16px 0 0', fontSize: 'clamp(36px, 4.4vw, 80px)', lineHeight: 1.08, fontWeight: 800, letterSpacing: '-0.02em' }}>
              Turn any newsletter section into a two-host podcast that asks your questions.
            </h1>
            <p style={{ margin: '20px 0 0', fontSize: 'clamp(18px, 1.3vw, 26px)', lineHeight: 1.6, color: 'var(--text-2)', maxWidth: 'min(62ch, 100%)' }}>
              Maya asks, Theo explains. Tell Maya what you care about and she asks the questions you would. Pause anytime to ask your own, then take a quick quiz so the next podcast fits what you already know.
            </p>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, marginTop: 32, alignItems: 'center' }}>
              <Link to="/tldr" className="btn btn-primary btn-lg">
                Explore today’s TLDR
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M5 12h14" /><path d="m13 6 6 6-6 6" /></svg>
              </Link>
              <span style={{ fontSize: 14, color: 'var(--kn-muted)' }}>Sample newsletter. No sign-in or inbox needed.</span>
            </div>
          </div>
          <div className="card" style={{ flex: '1 1 380px', minWidth: 0, padding: 28 }}>
            <p style={{ margin: '0 0 18px', fontSize: 13, fontWeight: 700, color: 'var(--kn-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>How it works</p>
            <ol style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
              {STEPS.map(([title, body], i) => (
                <li key={title} style={{ display: 'flex', gap: 14 }}>
                  <span style={{ flex: 'none', width: 32, height: 32, borderRadius: 999, background: 'var(--accent-soft)', color: 'var(--accent)', fontWeight: 800, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>{i + 1}</span>
                  <div>
                    <p style={{ fontWeight: 700 }}>{title}</p>
                    <p style={{ margin: '4px 0 0', color: 'var(--text-2)', fontSize: 15, lineHeight: 1.5 }}>{body}</p>
                  </div>
                </li>
              ))}
            </ol>
          </div>
        </div>

        <div className="card" style={{ marginTop: 48, padding: '24px 28px', display: 'flex', flexWrap: 'wrap', gap: 20, alignItems: 'center', justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', gap: 16, alignItems: 'center', flex: '1 1 420px', minWidth: 0 }}>
            <span style={{ flex: 'none', width: 48, height: 48, borderRadius: 12, background: 'var(--surface-2)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-2)' }}>
              <MailIcon size={24} />
            </span>
            <div style={{ minWidth: 0 }}>
              <p style={{ fontWeight: 700, fontSize: 17, display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
                Your own newsletters, from Gmail <span className="badge">Coming soon</span>
              </p>
              <p style={{ margin: '4px 0 0', color: 'var(--text-2)', fontSize: 15 }}>Tell us which newsletters you get. We’ll turn them into podcasts when Gmail connection opens.</p>
            </div>
          </div>
          <button className="btn" onClick={() => setWaitlist(true)}>Join the waitlist</button>
        </div>
      </main>
      {waitlist && <WaitlistModal onClose={() => setWaitlist(false)} />}
    </div>
  );
}
