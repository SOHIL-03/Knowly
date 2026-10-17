import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TldrPodcast } from './TldrPodcast';

vi.mock('../lib/firebase', () => ({ auth: { currentUser: null } }));

const fakeTab = () => ({ location: { href: '' }, close: vi.fn(), document: { title: '', body: { textContent: '' } }, opener: {} });
let openMock: ReturnType<typeof vi.fn>;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root;
let host: HTMLDivElement;
let fetchMock: ReturnType<typeof vi.fn>;

const reply = (body: unknown, ok = true, status = 200) => ({ ok, status, json: async () => body });
const flush = () => act(async () => { await Promise.resolve(); });
const buttons = () => Array.from(host.ownerDocument.querySelectorAll('button'));
const button = (label: string | RegExp) => buttons().find((b) => (typeof label === 'string' ? b.textContent === label : label.test(b.textContent ?? '')))!;
const click = (el: Element) => act(async () => { el.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
const textarea = () => document.querySelector('textarea') as HTMLTextAreaElement | null;
const type = (value: string) =>
  act(async () => {
    const el = textarea()!;
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });
const posts = () => fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST');
const postBody = (n = 0) => JSON.parse(posts()[n][1].body as string);

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  openMock = vi.fn(() => fakeTab());
  vi.stubGlobal('open', openMock);
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root.render(<TldrPodcast category="tech" date="2026-10-16" section="big-tech" stories={3} />));
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

const openDialog = () => click(button(/Make a podcast/));

describe('podcast preference dialog', () => {
  it('opens the question instead of generating when the button is clicked', async () => {
    await openDialog();
    expect(document.querySelector('[role=dialog]')?.textContent).toContain('Would you like to go with some preference?');
    expect(button('YES')).toBeTruthy();
    expect(button('NO')).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('YES shows the labelled textarea and Submit, still without generating', async () => {
    await openDialog();
    await click(button('YES'));
    expect(document.body.textContent).toContain('Describe your preference');
    expect(textarea()?.placeholder).toMatch(/^Tell us what you want to learn/);
    expect(button('Submit')).toBeTruthy();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('rejects an empty or whitespace-only preference', async () => {
    await openDialog();
    await click(button('YES'));
    await click(button('Submit'));
    expect(document.body.textContent).toContain('Please describe your preference');
    await type('   \n ');
    await click(button('Submit'));
    expect(document.body.textContent).toContain('Please describe your preference');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('submits valid preferences to the existing endpoint and closes the dialog', async () => {
    fetchMock.mockResolvedValue(reply({ status: 'processing', variant: 'pabc1234567' }));
    await openDialog();
    await click(button('YES'));
    await type('  Focus on practical examples  ');
    await click(button('Submit'));
    expect(posts()).toHaveLength(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/tldr/podcast');
    expect(postBody()).toEqual({ category: 'tech', date: '2026-10-16', section: 'big-tech', user_preferences: 'Focus on practical examples' });
    expect(document.querySelector('[role=dialog]')).toBeNull();
    expect(document.body.textContent).toContain('Making your podcast');
  });

  it('NO starts the default podcast with no preferences field', async () => {
    fetchMock.mockResolvedValue(reply({ status: 'processing' }));
    await openDialog();
    await click(button('NO'));
    expect(posts()).toHaveLength(1);
    expect(postBody()).toEqual({ category: 'tech', date: '2026-10-16', section: 'big-tech' });
    expect(document.querySelector('[role=dialog]')).toBeNull();
  });

  it('cancelling closes the dialog without generating', async () => {
    await openDialog();
    await click(button('Cancel'));
    expect(document.querySelector('[role=dialog]')).toBeNull();
    await openDialog();
    await click(button('YES'));
    await act(async () => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); });
    expect(document.querySelector('[role=dialog]')).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('shows a loading state and ignores a second submit while the request is in flight', async () => {
    let finish!: (v: unknown) => void;
    fetchMock.mockReturnValue(new Promise((r) => { finish = r; }));
    await openDialog();
    await click(button('YES'));
    await type('Explain the architecture');
    await click(button('Submit'));
    expect(button(/Starting/).disabled).toBe(true);
    expect(textarea()!.disabled).toBe(true);
    await click(button(/Starting/));
    act(() => { textarea()!.form!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); });
    expect(posts()).toHaveLength(1);
    await act(async () => { finish(reply({ status: 'processing', variant: 'pabc1234567' })); });
    expect(posts()).toHaveLength(1);
  });

  it('keeps the typed preferences when the request fails, and a retry succeeds', async () => {
    fetchMock.mockResolvedValueOnce(reply({ error: { message: 'Server is busy' } }, false, 500));
    await openDialog();
    await click(button('YES'));
    await type('Cover vector databases');
    await click(button('Submit'));
    expect(document.querySelector('[role=dialog]')).not.toBeNull();
    expect(document.body.textContent).toContain('Server is busy');
    expect(textarea()!.value).toBe('Cover vector databases');

    fetchMock.mockResolvedValueOnce(reply({ status: 'processing', variant: 'pabc1234567' }));
    await click(button('Submit'));
    expect(posts()).toHaveLength(2);
    expect(postBody(1).user_preferences).toBe('Cover vector databases');
    expect(document.querySelector('[role=dialog]')).toBeNull();
  });

  it('polls a personalized podcast with its variant', async () => {
    vi.useFakeTimers();
    try {
      fetchMock.mockResolvedValueOnce(reply({ status: 'processing', variant: 'pabc1234567' }));
      fetchMock.mockResolvedValue(reply({ status: 'processing' }));
      await openDialog();
      await click(button('YES'));
      await type('Business perspective');
      await click(button('Submit'));
      await act(async () => { await vi.advanceTimersByTimeAsync(4100); });
      const poll = fetchMock.mock.calls.find(([, init]) => init?.method !== 'POST')!;
      expect(poll[0]).toContain('&variant=pabc1234567');
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps the preferences after a generation failure so the dialog can be reopened and retried', async () => {
    fetchMock.mockResolvedValueOnce(reply({ status: 'failed', message: 'The podcast could not be made.', variant: 'pabc1234567' }));
    await openDialog();
    await click(button('YES'));
    await type('Deep dive on embeddings');
    await click(button('Submit'));
    await flush();
    expect(document.body.textContent).toContain('The podcast could not be made.');
    await openDialog();
    await click(button('YES'));
    expect(textarea()!.value).toBe('Deep dive on embeddings');
  });
});

describe('opening the podcast in a new tab', () => {
  const PID = 'tech.2026-10-16.big-tech';

  it('opens the blank tab inside the click, before the request finishes, then sends it to the persistent page', async () => {
    let finish!: (v: unknown) => void;
    fetchMock.mockReturnValue(new Promise((r) => { finish = r; }));
    const tab = fakeTab();
    openMock.mockReturnValue(tab);
    await openDialog();
    await click(button('NO'));
    expect(openMock).toHaveBeenCalledWith('', '_blank'); // synchronous: the request has not even resolved yet
    expect(tab.location.href).toBe('');
    expect(tab.document.body.textContent).toContain('Making your podcast');
    await act(async () => { finish(reply({ status: 'processing', podcastId: PID })); });
    expect(tab.location.href).toBe(`/podcast/${PID}`);
    expect(tab.opener).toBeNull();
  });

  it('points the tab at the personalized podcast id too', async () => {
    const tab = fakeTab();
    openMock.mockReturnValue(tab);
    fetchMock.mockResolvedValue(reply({ status: 'processing', variant: 'pabc1234567', podcastId: `${PID}.pabc1234567` }));
    await openDialog();
    await click(button('YES'));
    await type('Focus on practical examples');
    await click(button('Submit'));
    expect(tab.location.href).toBe(`/podcast/${PID}.pabc1234567`);
  });

  it('shows an explicit link when the browser blocks the new tab', async () => {
    openMock.mockReturnValue(null);
    fetchMock.mockResolvedValue(reply({ status: 'processing', podcastId: PID }));
    await openDialog();
    await click(button('NO'));
    const link = Array.from(document.querySelectorAll('a')).find((a) => /Open the podcast page/.test(a.textContent ?? ''))!;
    expect(link.getAttribute('href')).toBe(`/podcast/${PID}`);
    expect(link.getAttribute('target')).toBe('_blank');
  });

  it('closes the blank tab and keeps the preference when the request fails, so the user can retry', async () => {
    const tab = fakeTab();
    openMock.mockReturnValue(tab);
    fetchMock.mockResolvedValueOnce(reply({ error: { message: 'Server is busy' } }, false, 500));
    await openDialog();
    await click(button('YES'));
    await type('Cover reranking');
    await click(button('Submit'));
    expect(tab.close).toHaveBeenCalled();
    expect(textarea()!.value).toBe('Cover reranking');
    expect(document.body.textContent).toContain('Server is busy');
  });

  it('opens no tab for an empty preference or when the dialog is cancelled', async () => {
    await openDialog();
    await click(button('YES'));
    await click(button('Submit'));
    await click(button('Cancel'));
    expect(openMock).not.toHaveBeenCalled();
  });

  it('does not open a second tab for a double click on NO', async () => {
    fetchMock.mockReturnValue(new Promise(() => {}));
    await openDialog();
    await click(button('NO'));
    expect((button(/Making your podcast/) as HTMLButtonElement).disabled).toBe(true); // busy: cannot start another podcast
    expect(openMock).toHaveBeenCalledTimes(1);
    expect(posts()).toHaveLength(1);
  });
});
