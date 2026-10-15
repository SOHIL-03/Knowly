import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import type { TraceEntry } from '../lib/review';
import type { Segment } from '../lib/types';

vi.mock('../lib/api', () => ({ get: vi.fn(), post: vi.fn() }));

const { LlmPreview } = await import('./LlmPreview');
const { ScriptReview } = await import('./ScriptReview');

const RAW = '{"title":"T","lines":[{"speaker":"A","text":"Hi <b>there</b> & welcome"}]}';
const entry = (over: Partial<TraceEntry>): TraceEntry => ({
  seq: 1, step: 'PodcastScript', label: 'Podcast script', segId: 's1', segTitle: 'For your project', model: 'gpt-4o-mini',
  attempt: 1, status: 'ok', promptPreview: 'source text', promptChars: 4200, rawResponse: RAW, parsed: { title: 'T' },
  usage: { input: 120, output: 80, total: 200 }, durationMs: 900, appValidation: { passed: true, problems: [] }, ...over,
});

describe('LlmPreview', () => {
  it('renders nothing when review is off', () => {
    expect(renderToStaticMarkup(<LlmPreview trace={{ trace: { enabled: false, model: 'gpt-4o-mini', entries: [] }, error: null }} />)).toBe('');
  });

  it('shows model, tokens, raw and parsed separately, escaping the raw text', () => {
    const html = renderToStaticMarkup(<LlmPreview trace={{ trace: { enabled: true, model: 'gpt-4o-mini', entries: [entry({})] }, error: null }} />);
    expect(html).toContain('OpenAI response preview');
    expect(html).toContain('gpt-4o-mini');
    expect(html).toContain('120 in · 80 out · 200 total');
    expect(html).toContain('Raw OpenAI response (exactly as returned)');
    expect(html).toContain('Parsed result (after schema validation)');
    expect(html).toContain('Hi &lt;b&gt;there&lt;/b&gt;'); // shown as text, never injected as HTML
    expect(html).not.toContain('<b>there</b>');
    expect(html).toContain('Input (first 11 of 4,200 characters)');
    expect(html).toContain('Script checks: passed');
  });

  it('shows failures and validation errors without a parsed block', () => {
    const html = renderToStaticMarkup(
      <LlmPreview
        trace={{
          trace: {
            enabled: true,
            model: 'gpt-4o-mini',
            entries: [
              entry({ seq: 1, status: 'validation_failed', parsed: undefined, error: 'lines: too short' }),
              entry({ seq: 2, status: 'request_failed', rawResponse: undefined, parsed: undefined, usage: null, error: 'OpenAI rejected the API key or permissions (check OPENAI_API_KEY).' }),
            ],
          },
          error: null,
        }}
      />,
    );
    expect(html).toContain('Validation failed');
    expect(html).toContain('lines: too short');
    expect(html).toContain('Request failed');
    expect(html).toContain('no usage reported');
    expect(html).not.toContain('Parsed result');
  });
});

const lines = Array.from({ length: 60 }, (_, i) => ({
  speaker: (i % 2 ? 'B' : 'A') as 'A' | 'B',
  speakerName: i % 2 ? 'Theo' : 'Maya',
  text: `Line number ${i + 1}.`,
  item_ids: [],
}));
const segment = { id: 's1', script: lines, scriptMode: 'model', audioStatus: 'review', faithfulness: 'pass' } as unknown as Segment;

describe('ScriptReview', () => {
  it('lists the complete script with speaker labels and a Generate Audio button', () => {
    const html = renderToStaticMarkup(<ScriptReview briefingId="b" segment={segment} trace={[entry({})]} onChanged={() => undefined} />);
    expect(html).toContain('Review the script');
    for (let i = 1; i <= 60; i++) expect(html).toContain(`Line number ${i}.`);
    expect(html).toContain('Maya');
    expect(html).toContain('Theo');
    expect(html).toContain('Generate Audio');
    expect(html).toContain('Copy script');
    expect(html).toContain('Raw JSON');
    expect(html).toContain('max-h-[50dvh]'); // long scripts scroll instead of growing the page
    expect(html).not.toContain('Modified by you');
  });

  it('says so when the script is the fallback, not the model output', () => {
    const html = renderToStaticMarkup(
      <ScriptReview briefingId="b" segment={{ ...segment, scriptMode: 'fallback' } as Segment} trace={[]} onChanged={() => undefined} />,
    );
    expect(html).toContain('built from the newsletter notes');
  });
});
