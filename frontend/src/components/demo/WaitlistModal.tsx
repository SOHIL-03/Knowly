import { useState } from 'react';
import { Link } from 'react-router-dom';
import { joinWaitlist } from '../../lib/tldrApi';
import { CloseIcon, Modal } from './Modal';

const EMAIL = /^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$/;

export function WaitlistModal({ onClose }: { onClose: () => void }) {
  const [email, setEmail] = useState('');
  const [senders, setSenders] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const list = senders.split('\n').map((s) => s.trim()).filter(Boolean);
  const disabled = busy || !EMAIL.test(email.trim()) || list.length === 0;

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await joinWaitlist(email.trim(), list);
      setDone(email.trim());
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not join the waitlist. Try again.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal label="Join the Gmail waitlist" width={560} padding="24px 28px" onClose={onClose}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 }}>
        <div>
          <span className="badge">Coming soon</span>
          <h2 style={{ margin: '12px 0 0', fontSize: 24, fontWeight: 800 }}>Podcasts from your own newsletters</h2>
        </div>
        <button className="btn btn-ghost icon-btn" aria-label="Close" onClick={onClose}><CloseIcon /></button>
      </div>

      {!done ? (
        <>
          <p style={{ margin: '10px 0 0', color: 'var(--text-2)', fontSize: 15, lineHeight: 1.55 }}>
            Gmail connection isn’t open yet. Leave your email and the newsletters you read, and we’ll let you know when Knowly can turn them into podcasts.
          </p>
          <div style={{ marginTop: 20, display: 'flex', flexDirection: 'column', gap: 16 }}>
            <div>
              <label htmlFor="wl-email" style={{ display: 'block', fontWeight: 600, fontSize: 14, marginBottom: 6 }}>Your Gmail address</label>
              <input id="wl-email" className="field" type="email" placeholder="you@gmail.com" value={email} onChange={(e) => setEmail(e.target.value)} />
            </div>
            <div>
              <label htmlFor="wl-senders" style={{ display: 'block', fontWeight: 600, fontSize: 14, marginBottom: 6 }}>Newsletters you want as podcasts</label>
              <textarea id="wl-senders" className="field" rows={2} placeholder="One sender address per line, e.g. dan@tldrnewsletter.com" value={senders} onChange={(e) => setSenders(e.target.value)} style={{ resize: 'vertical' }} />
              <p style={{ margin: '6px 0 0', fontSize: 13, color: 'var(--kn-muted)' }}>The “From” address of each newsletter. We only read these, never other mail.</p>
            </div>
          </div>
          {error && <p role="alert" style={{ margin: '12px 0 0', fontSize: 14, color: 'var(--notyet)' }}>{error}</p>}
          <div style={{ marginTop: 24, display: 'flex', gap: 10, justifyContent: 'flex-end', flexWrap: 'wrap' }}>
            <button className="btn btn-ghost" onClick={onClose}>Not now</button>
            <button className="btn btn-primary" onClick={submit} disabled={disabled}>Join the waitlist</button>
          </div>
        </>
      ) : (
        <>
          <div role="status" style={{ marginTop: 18, padding: 18, borderRadius: 12, background: 'var(--good-soft)', color: 'var(--text)', display: 'flex', gap: 12, alignItems: 'flex-start' }}>
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--good)" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: 'none', marginTop: 2 }}>
              <path d="M20 6 9 17l-5-5" />
            </svg>
            <p style={{ fontSize: 15, lineHeight: 1.55 }}>You’re on the list. We’ll email <strong>{done}</strong> when Gmail connection opens. Meanwhile, try today’s TLDR.</p>
          </div>
          <div style={{ marginTop: 20, display: 'flex', justifyContent: 'flex-end', gap: 10, flexWrap: 'wrap' }}>
            <Link to="/tldr" className="btn btn-primary" onClick={onClose}>Explore today’s TLDR</Link>
          </div>
        </>
      )}
    </Modal>
  );
}
