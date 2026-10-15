import { BookMarked, Brain, Home, Settings as Cog } from 'lucide-react';
import type { ReactNode } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { Banner, Button } from './ui';

const tabs = [
  { to: '/today', label: 'Today', icon: Home },
  { to: '/library', label: 'Library', icon: BookMarked },
  { to: '/knowledge', label: 'Knowledge', icon: Brain },
  { to: '/settings', label: 'Settings', icon: Cog },
];

/** Bottom tab bar on mobile, left rail on desktop (section 11.3). */
export function AppShell({ children }: { children: ReactNode }) {
  const { me } = useAuth();
  const navigate = useNavigate();
  return (
    <div className="min-h-dvh bg-bg md:flex">
      <nav aria-label="Main" className="hidden w-56 shrink-0 border-r border-line bg-surface p-4 md:block">
        <div className="mb-8 px-2 text-section font-bold text-accent">Knowly</div>
        <ul className="space-y-1">
          {tabs.map(({ to, label, icon: Icon }) => (
            <li key={to}>
              <NavLink
                to={to}
                className={({ isActive }) =>
                  `flex min-h-11 items-center gap-3 rounded-xl px-3 text-body ${isActive ? 'bg-accent-soft font-semibold text-accent' : 'text-ink-2 hover:bg-surface-2'}`
                }
              >
                <Icon className="size-5" aria-hidden />
                {label}
              </NavLink>
            </li>
          ))}
        </ul>
        {me?.mode === 'sample' && <p className="mt-8 px-3 text-caption text-ink-3">Sample mode: briefings use three sample newsletters, no inbox access.</p>}
      </nav>
      <main className="mx-auto w-full max-w-3xl px-4 pb-28 pt-6 md:px-8 md:pb-10">
        {me?.gmail?.status === 'reauth' && me.mode === 'live' && (
          <div className="mb-4">
            <Banner tone="warn" action={<Button variant="secondary" onClick={() => navigate('/connect-gmail')}>Reconnect</Button>}>
              Reconnect Gmail to keep getting briefings. Your Library is still here.
            </Banner>
          </div>
        )}
        {children}
      </main>
      <nav aria-label="Main" className="fixed inset-x-0 bottom-0 z-30 border-t border-line bg-surface pb-[env(safe-area-inset-bottom)] md:hidden">
        <ul className="grid grid-cols-4">
          {tabs.map(({ to, label, icon: Icon }) => (
            <li key={to}>
              <NavLink
                to={to}
                className={({ isActive }) => `flex min-h-14 flex-col items-center justify-center gap-0.5 text-caption ${isActive ? 'font-semibold text-accent' : 'text-ink-3'}`}
              >
                <Icon className="size-5" aria-hidden />
                {label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
    </div>
  );
}
