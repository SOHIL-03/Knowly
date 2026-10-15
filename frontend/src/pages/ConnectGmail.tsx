import { Lock, Mail, ShieldCheck } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { Banner, Button } from '../components/ui';
import { post } from '../lib/api';
import { gmailClientId } from '../lib/firebase';

const GMAIL_SCOPE = 'https://www.googleapis.com/auth/gmail.readonly';

type CodeClient = { requestCode: () => void };
declare global {
  interface Window {
    google?: { accounts: { oauth2: { initCodeClient: (cfg: Record<string, unknown>) => CodeClient } } };
  }
}

function loadGis(): Promise<void> {
  if (window.google?.accounts?.oauth2) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = 'https://accounts.google.com/gsi/client';
    s.async = true;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error('Could not load Google sign-in'));
    document.head.appendChild(s);
  });
}

/** Gmail permission explainer: three short lines, one Allow button (section 11.4, screen 2). */
export function ConnectGmail() {
  const { me, refreshMe, linkGoogle } = useAuth();
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const allow = async () => {
    setBusy(true);
    setError(null);
    try {
      await loadGis();
      const code = await new Promise<string>((resolve, reject) => {
        window.google!.accounts.oauth2
          .initCodeClient({
            client_id: gmailClientId,
            scope: GMAIL_SCOPE,
            ux_mode: 'popup',
            callback: (r: { code?: string; error?: string }) => (r.code ? resolve(r.code) : reject(new Error(r.error || 'Permission was not granted'))),
            error_callback: (e: { message?: string }) => reject(new Error(e.message || 'The Google window was closed')),
          })
          .requestCode();
      });
      // The server exchanges the code; the refresh token never reaches the browser.
      await post('/auth/gmail/connect', { code });
      await refreshMe();
      navigate(me?.onboarded ? '/settings' : '/onboarding', { replace: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not connect Gmail');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto flex min-h-dvh max-w-md flex-col justify-center px-4 py-10">
      <h1 className="text-title font-bold">Read your newsletters from Gmail</h1>
      <ul className="mt-6 space-y-4 text-body">
        <li className="flex gap-3">
          <Mail className="mt-0.5 size-5 shrink-0 text-accent" aria-hidden /> Knowly only opens emails from the newsletters you pick, and skips anything that is not a newsletter.
        </li>
        <li className="flex gap-3">
          <Lock className="mt-0.5 size-5 shrink-0 text-accent" aria-hidden /> Access is read-only. Knowly can never send, delete or change your email.
        </li>
        <li className="flex gap-3">
          <ShieldCheck className="mt-0.5 size-5 shrink-0 text-accent" aria-hidden /> Raw emails are deleted after about 7 days. You can disconnect and delete everything in Settings.
        </li>
      </ul>
      <details className="mt-4 text-caption text-ink-2">
        <summary className="min-h-9 cursor-pointer text-accent">What we read and store</summary>
        <p className="mt-2">
          We store the stories found in each newsletter (headline, short note, link), your briefings, and what you did with them. Your Gmail access token is kept in Google Secret Manager on the server and never sent to your browser.
        </p>
      </details>

      <div className="mt-8 space-y-3">
        {!gmailClientId ? (
          <Banner tone="warn">Gmail connection is not configured on this deployment. Sample mode works without it.</Banner>
        ) : me?.anonymous ? (
          <>
            <Banner tone="info">Sign in with Google first so your Gmail connection belongs to your account.</Banner>
            <Button full onClick={() => linkGoogle().catch((e: Error) => setError(e.message))}>
              Sign in with Google
            </Button>
          </>
        ) : (
          <Button full busy={busy} onClick={allow}>
            Allow read-only access
          </Button>
        )}
        {error && <Banner tone="error">{error}</Banner>}
        <Button variant="ghost" full onClick={() => navigate(me?.onboarded ? '/settings' : '/onboarding')}>
          Not now, use sample mode
        </Button>
      </div>
    </div>
  );
}
