import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { claimAudio, pauseCurrent, playExclusive } from '../../lib/audioFocus';
import type { PodcastPage } from '../../lib/podcastApi';
import { activeLine, clock, PodcastPlayer } from './PodcastPlayer';

vi.mock('../../lib/firebase', () => ({ auth: { currentUser: null } }));

const podcast = (over: Partial<PodcastPage> = {}): PodcastPage => ({
  status: 'ready', podcastId: 'tech.2026-10-16.big-tech', title: 'Vectors', sectionName: 'Big Tech', audioUrl: '/media/p.wav', durationSec: 20,
  sources: [{ id: 's1', title: 'Story one', url: 'https://a.example', basis: 'article' }],
  lines: [
    { speaker: 'Maya', role: 'question', text: 'What is vector search?' },
    { speaker: 'Theo', role: 'answer', text: 'It finds nearest vectors.' },
  ],
  lineTimings: [{ s: 0, e: 5 }, { s: 5, e: 12 }],
  ...over,
});

describe('PodcastPlayer', () => {
  it('shows the title, the source stories, the audio and the transcript with speaker labels', () => {
    const html = renderToStaticMarkup(<PodcastPlayer podcast={podcast()} />);
    expect(html).toContain('Vectors');
    expect(html).toContain('Story one');
    expect(html).toContain('src="/media/p.wav"');
    expect(html).toContain('aria-label="Play"');
    expect(html).toContain('aria-label="Seek"');
    expect(html).toContain('Maya:');
    expect(html).toContain('Theo:');
    expect(html).toContain('It finds nearest vectors.');
  });

  it('shows the transcript before the audio exists', () => {
    const html = renderToStaticMarkup(<PodcastPlayer podcast={podcast({ status: 'processing', audioUrl: undefined })} />);
    expect(html).not.toContain('<audio');
    expect(html).toContain('What is vector search?');
  });
});

describe('transcript highlighting', () => {
  const timings = [{ s: 0, e: 5 }, { s: 5, e: 12 }, { s: 12, e: 20 }];
  it('finds the line being spoken', () => {
    expect(activeLine(timings, 0)).toBe(0);
    expect(activeLine(timings, 4.9)).toBe(0);
    expect(activeLine(timings, 5)).toBe(1);
    expect(activeLine(timings, 19)).toBe(2);
    expect(activeLine(timings, 99)).toBe(2); // after the end the last line stays current
    expect(activeLine(undefined, 3)).toBe(-1);
  });
  it('formats time', () => {
    expect(clock(612)).toBe('10:12');
    expect(clock(5.9)).toBe('0:05');
  });
});

describe('audio focus: one voice at a time', () => {
  const el = (playing: boolean) => {
    const a = document.createElement('audio');
    Object.defineProperty(a, 'paused', { value: !playing, writable: true });
    a.pause = vi.fn(() => Object.defineProperty(a, 'paused', { value: true }));
    a.play = vi.fn().mockResolvedValue(undefined);
    return a;
  };

  it('starting a new answer pauses the one that was playing', async () => {
    const first = el(true);
    const second = el(false);
    claimAudio(first);
    expect(await playExclusive(second)).toBe(true);
    expect(first.pause).toHaveBeenCalledTimes(1);
    expect(second.play).toHaveBeenCalled();
  });

  it('a new question stops whatever is speaking', () => {
    const speaking = el(true);
    claimAudio(speaking);
    pauseCurrent();
    expect(speaking.pause).toHaveBeenCalled();
  });

  it('reports a blocked autoplay so a play button can be shown', async () => {
    const blocked = el(false);
    blocked.play = vi.fn().mockRejectedValue(new DOMException('blocked', 'NotAllowedError'));
    expect(await playExclusive(blocked)).toBe(false);
  });
});
