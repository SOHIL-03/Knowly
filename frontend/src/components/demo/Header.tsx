import { Link } from 'react-router-dom';

export function MailIcon({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="2" y="4" width="20" height="16" rx="2" />
      <path d="m22 7-10 6L2 7" />
    </svg>
  );
}

export function HeadphonesIcon({ size = 18, color = 'currentColor' }: { size?: number; color?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 14v-3a9 9 0 0 1 18 0v3" />
      <path d="M21 15a2 2 0 0 1-2 2h-1v-6h1a2 2 0 0 1 2 2z" />
      <path d="M3 15a2 2 0 0 0 2 2h1v-6H5a2 2 0 0 0-2 2z" />
    </svg>
  );
}

/** Logo on the left; Home (issue page only) and Connect Gmail (Soon) on the right. */
export function Header({ showHome, onGmail }: { showHome?: boolean; onGmail: () => void }) {
  return (
    <header style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap', padding: '18px clamp(16px, 4vw, 96px)', borderBottom: '1px solid var(--border)', background: 'var(--surface)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <HeadphonesIcon size={28} color="var(--accent)" />
        <span style={{ fontSize: 20, fontWeight: 800, letterSpacing: '-0.01em' }}>Knowly</span>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        {showHome && <Link to="/" className="btn btn-ghost">Home</Link>}
        <button className="btn" onClick={onGmail}>
          <MailIcon />
          Connect Gmail
          <span className="badge">Soon</span>
        </button>
      </div>
    </header>
  );
}
