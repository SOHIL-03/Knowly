import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flush, mount } from '../test-utils/dom';

vi.mock('../lib/firebase', () => ({ auth: { currentUser: null } }));
const auth = vi.hoisted(() => ({ user: null as null | { uid: string }, loading: false, join: vi.fn().mockResolvedValue(undefined) }));
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({ user: auth.user, loading: auth.loading, signInForSample: auth.join }),
}));
const api = vi.hoisted(() => ({ fetchPodcast: vi.fn() }));
vi.mock('../lib/podcastApi', async (orig) => ({ ...(await orig<typeof import('../lib/podcastApi')>()), ...api }));
vi.mock('../components/podcast/ChatPanel', () => ({ ChatPanel: ({ pid }: { pid: string }) => <div data-testid="chat">chat for {pid}</div> }));
vi.mock('../components/podcast/AssessmentPanel', () => ({ AssessmentPanel: () => <div data-testid="assessment">assessment</div> }));

import { PodcastPage } from './PodcastPage';
import { ApiError } from '../lib/api';

const PID = 'tech.2026-10-16.big-tech';
const ready = {
  status: 'ready', podcastId: PID, title: 'Vectors', audioUrl: '/media/p.wav', durationSec: 20,
  lines: [{ speaker: 'Maya', role: 'question', text: 'What is vector search?' }], lineTimings: [{ s: 0, e: 5 }],
};

let ui: ReturnType<typeof mount>;
const open = async (id = PID) => {
  ui = mount(<MemoryRouter initialEntries={[`/podcast/${id}`]}><Routes><Route path="/podcast/:pid" element={<PodcastPage />} /></Routes></MemoryRouter>);
  await flush();
};

beforeEach(() => {
  api.fetchPodcast.mockReset();
  auth.user = { uid: 'u1' };
  auth.loading = false;
});
afterEach(() => ui?.unmount());

describe('podcast page', () => {
  it('loads everything from the persistent id, so a refresh or later session shows the same podcast', async () => {
    api.fetchPodcast.mockResolvedValue(ready);
    await open();
    expect(api.fetchPodcast).toHaveBeenCalledWith(PID);
    expect(document.body.textContent).toContain('Vectors');
    expect(document.body.textContent).toContain('What is vector search?');
    expect(document.querySelector('audio')?.getAttribute('src')).toBe('/media/p.wav');
    expect(document.querySelector('[data-testid=chat]')?.textContent).toContain(PID);
    expect(document.querySelector('[data-testid=assessment]')).not.toBeNull();
  });

  it('is the loading state while the podcast is being made, and polls until it is ready', async () => {
    vi.useFakeTimers();
    try {
      api.fetchPodcast.mockResolvedValueOnce({ status: 'processing', podcastId: PID }).mockResolvedValue(ready);
      const { act } = await import('react');
      ui = mount(<MemoryRouter initialEntries={[`/podcast/${PID}`]}><Routes><Route path="/podcast/:pid" element={<PodcastPage />} /></Routes></MemoryRouter>);
      await act(async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); }); // microtasks only: timers are faked here
      expect(document.body.textContent).toContain('Making your podcast');
      expect(document.querySelector('audio')).toBeNull();
      await import('react').then(({ act }) => act(async () => { await vi.advanceTimersByTimeAsync(4100); }));
      expect(document.querySelector('audio')).not.toBeNull();
      expect(document.body.textContent).not.toContain('Making your podcast');
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows the failure and a way back when generation failed', async () => {
    api.fetchPodcast.mockResolvedValue({ status: 'failed', podcastId: PID, message: 'The podcast could not be made right now.' });
    await open();
    expect(document.body.textContent).toContain('The podcast could not be made right now.');
    expect(document.body.textContent).toContain('your preference is kept');
    expect(document.querySelector('[data-testid=chat]')).toBeNull();
  });

  it('says a missing or private podcast was not found', async () => {
    api.fetchPodcast.mockRejectedValue(new ApiError(404, 'not_found', 'Podcast not found'));
    await open('tech.2026-10-16.nope');
    expect(document.body.textContent).toContain('not found');
  });

  it('needs no sign-in screen: a visitor is given an anonymous identity so chat works at once', async () => {
    auth.user = null;
    auth.join.mockClear();
    api.fetchPodcast.mockResolvedValue(ready);
    await open();
    expect(auth.join).toHaveBeenCalledTimes(1);
    expect(document.querySelector('audio')).not.toBeNull();
    expect(document.body.textContent).not.toContain('Sign in');
    expect(document.body.textContent).toContain('Getting the chat ready');
  });

  it('shows the chat as soon as the identity exists', async () => {
    api.fetchPodcast.mockResolvedValue(ready);
    await open();
    expect(document.body.textContent).not.toContain('Sign in');
    expect(document.querySelector('[data-testid=chat]')).not.toBeNull();
  });
});
