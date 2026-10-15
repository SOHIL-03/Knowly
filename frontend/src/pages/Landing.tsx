import { Headphones } from 'lucide-react';
import { useState } from 'react';
import { useAuth } from '../auth/AuthContext';
import { Banner, Button, Card } from '../components/ui';
import { firebaseConfigured, sampleAudioUrl } from '../lib/firebase';

export function Landing() {
  const { signInWithGoogle, signInForSample, error } = useAuth();
  const [busy, setBusy] = useState<'google' | 'sample' | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  const run = async (kind: 'google' | 'sample') => {
    setBusy(kind);
    setLocalError(null);
    try {
      await (kind === 'google' ? signInWithGoogle() : signInForSample());
    } catch (e) {
      setLocalError(e instanceof Error ? e.message : 'Sign-in failed');
      setBusy(null);
    }
  };

  return (
    <div className="mx-auto flex min-h-dvh max-w-lg flex-col justify-center px-4 py-10">
      <p className="text-section font-bold text-accent">Knowly</p>
      <h1 className="mt-4 text-[32px] font-bold leading-tight">Your newsletters, as a short spoken briefing that checks what you learned.</h1>
      <p className="mt-3 text-body text-ink-2">
        Knowly regroups the stories around what you are building, reads them to you in a few minutes, then asks one good question after each part. It remembers what you know, so tomorrow is pitched at the right level.
      </p>

      <Card className="mt-6">
        {sampleAudioUrl ? (
          <>
            <p className="mb-2 flex items-center gap-2 text-caption text-ink-3">
              <Headphones className="size-4" aria-hidden /> 20-second sample
            </p>
            <audio controls preload="none" src={sampleAudioUrl} className="w-full" />
          </>
        ) : (
          <>
            <p className="mb-2 flex items-center gap-2 text-caption text-ink-3">
              <Headphones className="size-4" aria-hidden /> What a segment looks like
            </p>
            <p className="text-section font-semibold">For your RAG project</p>
            <p className="mt-1 text-caption italic text-accent">Mentions vector search, which your project uses</p>
            <p className="mt-3 text-body text-ink-2"><span className="font-semibold text-accent">Maya:</span> Two of your newsletters covered vector search in Firestore this week.</p>
            <p className="mt-1 text-body text-ink-2"><span className="font-semibold">Theo:</span> And it matters for your project… then a checkpoint: Ask, Go deeper or Quiz me.</p>
          </>
        )}
      </Card>

      {!firebaseConfigured && <div className="mt-4"><Banner tone="error">Firebase is not configured for this build (VITE_FIREBASE_API_KEY).</Banner></div>}
      {(error || localError) && <div className="mt-4"><Banner tone="error">{error || localError}</Banner></div>}

      <div className="mt-8 space-y-3">
        <Button full busy={busy === 'google'} onClick={() => run('google')}>
          Continue with Google
        </Button>
        <button onClick={() => run('sample')} disabled={busy !== null} className="min-h-11 w-full text-body font-medium text-accent underline-offset-4 hover:underline disabled:opacity-50">
          {busy === 'sample' ? 'Starting…' : 'Try with a sample newsletter'}
        </button>
        <p className="text-center text-caption text-ink-3">The sample needs no inbox access.</p>
      </div>
    </div>
  );
}
