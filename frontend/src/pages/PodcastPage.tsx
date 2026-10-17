import { useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext';
import { AssessmentPanel } from '../components/podcast/AssessmentPanel';
import { ChatPanel } from '../components/podcast/ChatPanel';
import { PodcastPlayer } from '../components/podcast/PodcastPlayer';
import { Banner, Button, Card, Spinner } from '../components/ui';
import { ApiError } from '../lib/api';
import { fetchPodcast, type PodcastPage as Podcast } from '../lib/podcastApi';

const POLL_MS = 4000;

/**
 * /podcast/:id, the persistent page of one podcast. Everything is loaded from the server by id, so a refresh,
 * a later session or another device shows the same podcast, transcript and chat. While the podcast is still being made
 * this page is the loading state, and it turns into the player by itself.
 */
export function PodcastPage() {
  const { pid = '' } = useParams();
  const { user, loading: authLoading, signInForSample } = useAuth();
  const [podcast, setPodcast] = useState<Podcast | null>(null);
  const [error, setError] = useState<{ status: number; message: string } | null>(null);
  const timer = useRef(0);
  const [identityError, setIdentityError] = useState<string | null>(null);
  const joining = useRef(false);

  // No sign-in screen: a visitor gets a private anonymous identity automatically, so chat, assessments and learning
  // progress are saved for them (in this browser) and the server still derives who they are from a verified token.
  useEffect(() => {
    if (authLoading || user || joining.current) return;
    joining.current = true;
    signInForSample().catch((e) => {
      joining.current = false;
      setIdentityError(e instanceof Error ? e.message : 'Could not get ready');
    });
  }, [authLoading, user, signInForSample]);

  useEffect(() => {
    if (authLoading) return; // the signed-in user decides whether a private episode is visible
    let live = true;
    const load = async () => {
      try {
        const p = await fetchPodcast(pid);
        if (!live) return;
        setPodcast(p);
        setError(null);
        if (p.status === 'processing') timer.current = window.setTimeout(load, POLL_MS);
      } catch (e) {
        if (!live) return;
        setError({ status: e instanceof ApiError ? e.status : 0, message: e instanceof Error ? e.message : 'Could not load the podcast' });
        if (!(e instanceof ApiError && e.status === 404)) timer.current = window.setTimeout(load, POLL_MS * 2);
      }
    };
    void load();
    return () => {
      live = false;
      window.clearTimeout(timer.current);
    };
  }, [pid, authLoading, user?.uid]);

  useEffect(() => {
    document.title = podcast?.title ? `${podcast.title} · Knowly` : 'Podcast · Knowly';
  }, [podcast?.title]);

  const ready = podcast?.status === 'ready';
  const hasScript = !!podcast?.lines?.length;

  return (
    <main className="mx-auto max-w-2xl space-y-4 p-4 pb-16">
      {!podcast && !error && (
        <div className="flex justify-center py-16"><Spinner label="Loading the podcast" /></div>
      )}
      {error?.status === 404 && (
        <Banner tone="error">This podcast was not found. If it is a private one, sign in with the account that made it.</Banner>
      )}
      {error && error.status !== 404 && <Banner tone="error">{error.message} Trying again…</Banner>}

      {podcast?.status === 'processing' && (
        <Card>
          <div className="flex items-center gap-3">
            <Spinner label="Making your podcast" />
            <div>
              <p className="text-body font-medium">Making your podcast…</p>
              <p className="text-caption text-ink-3">
                {podcast.phase === 'audio' ? 'The script is ready. Now making the audio.' : 'This takes a few minutes. You can keep this tab open; it updates by itself.'}
              </p>
            </div>
          </div>
        </Card>
      )}
      {podcast?.status === 'failed' && (
        <Banner tone="error" action={<Button variant="secondary" onClick={() => window.location.reload()}>Check again</Button>}>
          {podcast.message || 'The podcast could not be made.'} Go back to the digest to try again; your preference is kept there.
        </Banner>
      )}
      {podcast?.status === 'none' && <Banner tone="error">This podcast was not found.</Banner>}

      {podcast && (ready || (hasScript && podcast.status === 'processing')) && <PodcastPlayer podcast={podcast} />}

      {ready && !user && (
        <Card>
          {identityError ? (
            <Banner tone="error">Chat is unavailable right now: {identityError}</Banner>
          ) : (
            <p className="text-caption text-ink-3" role="status">Getting the chat ready…</p>
          )}
        </Card>
      )}
      {ready && user && (
        <>
          <ChatPanel pid={pid} />
          <AssessmentPanel pid={pid} />
        </>
      )}
    </main>
  );
}
