import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { sectionId } from './TldrDigest';
import { PodcastView, type PodcastState, type Report } from './TldrPodcast';

const report = (over: Partial<Report> = {}): Report => ({
  status: 'complete', expectedPoints: 6, coveredPoints: 6, missing: [], unsupportedLines: [], structure: [], words: 900, parts: 2,
  llmCalls: 5, ttsCalls: 3, repairs: 1, ...over,
});
const lines = [
  { speaker: 'Maya', role: 'question' as const, text: 'What happened?' },
  { speaker: 'Theo', role: 'answer' as const, text: 'It flew over five cars.' },
];
const ready = (over: Partial<PodcastState> = {}): PodcastState => ({
  status: 'ready', title: 'Roadster', audioUrl: '/api/media?t=abc', durationSec: 612, lines, takeaways: ['It flies'], report: report(), ...over,
});

describe('PodcastView', () => {
  it('plays the whole assembled episode and shows its real length', () => {
    const html = renderToStaticMarkup(<PodcastView state={ready()} />);
    expect(html).toContain('<audio controls="" preload="metadata" src="/api/media?t=abc"');
    expect(html).toContain('Roadster · 10:12'); // 612 seconds, not rounded down to minutes
  });

  it('shows short episodes in seconds, not as 0 min', () => {
    expect(renderToStaticMarkup(<PodcastView state={ready({ durationSec: 45 })} />)).toContain('· 0:45');
  });

  it('shows the script, coverage and call counts', () => {
    const html = renderToStaticMarkup(<PodcastView state={ready()} />);
    expect(html).toContain('Coverage: complete');
    expect(html).toContain('6 of 6 key points');
    expect(html).toContain('900 words');
    expect(html).toContain('5 AI calls');
    expect(html).toContain('3 audio calls');
    expect(html).toContain('What happened?');
    expect(html).toContain('It flew over five cars.');
    expect(html).not.toContain('may not cover everything');
  });

  it('labels a partial podcast and lists what is missing, instead of presenting it as complete', () => {
    const html = renderToStaticMarkup(
      <PodcastView state={ready({ report: report({ status: 'partial', coveredPoints: 4, missing: [{ id: 'k5', topic: 'Pricing', facts: ['Costs $299'] }] }) })} />,
    );
    expect(html).toContain('may not cover everything');
    expect(html).toContain('4 of 6 key points');
    expect(html).toContain('Missing: Pricing (Costs $299)');
  });

  it('shows the script before any audio exists', () => {
    const html = renderToStaticMarkup(<PodcastView state={{ status: 'processing', phase: 'audio', lines, report: report() }} />);
    expect(html).toContain('The script is ready. Now making the audio');
    expect(html).toContain('What happened?');
    expect(html).not.toContain('<audio');
  });

  it('keeps the script visible when audio fails', () => {
    const html = renderToStaticMarkup(<PodcastView state={{ status: 'failed', message: 'Audio part 2 failed', lines, report: report() }} />);
    expect(html).toContain('Audio part 2 failed');
    expect(html).toContain('It flew over five cars.');
  });

  it('lists the stories a section podcast covers, in order', () => {
    const sources = [
      { id: 's1', title: 'Tesla Roadster flies', url: 'https://a.example/1', basis: 'article' as const },
      { id: 's2', title: 'Muse Spark Open', url: 'https://a.example/2', basis: 'article' as const },
    ];
    const html = renderToStaticMarkup(<PodcastView state={ready({ sectionName: 'Big Tech & Startups', basis: 'article', sources })} />);
    expect(html).toContain('Covers 2 stories, in this order:');
    expect(html).toContain('Tesla Roadster flies · Muse Spark Open');
    expect(html).not.toContain('short summar');
  });

  it('names the stories that fell back to the short summary when only some articles could be opened', () => {
    const sources = [
      { id: 's1', title: 'Opened story', url: 'https://a.example/1', basis: 'article' as const },
      { id: 's2', title: 'Paywalled story', url: 'https://a.example/2', basis: 'summary' as const },
    ];
    const html = renderToStaticMarkup(<PodcastView state={ready({ basis: 'mixed', sources })} />);
    expect(html).toContain('use TLDR&#x27;s short summary: Paywalled story.');
    expect(html).not.toContain('Opened story.</');
  });

  it('notes stories left out by the per-section limit', () => {
    const sources = [{ id: 's1', title: 'One', url: 'https://a.example/1', basis: 'article' as const }];
    const html = renderToStaticMarkup(<PodcastView state={ready({ sources, report: report({ omittedStories: 3 }) })} />);
    expect(html).toContain('Covers 1 story');
    expect(html).toContain('3 more not included');
  });
});

describe('sectionId', () => {
  it('matches the server slug for the TLDR section names', () => {
    expect(sectionId({ name: 'Big Tech & Startups' })).toBe('big-tech-startups');
    expect(sectionId({ name: 'Science & Futuristic Technology' })).toBe('science-futuristic-technology');
    expect(sectionId({ name: 'Programming, Design & Data Science' })).toBe('programming-design-data-science');
    expect(sectionId({ name: '???' })).toBe('section');
    expect(sectionId({ name: 'x'.repeat(80) })).toHaveLength(50);
  });

  it('prefers the id the server sent', () => {
    expect(sectionId({ id: 'same-name-2', name: 'Same name' })).toBe('same-name-2');
  });
});
