import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { button, click, flush, mount, textarea, type } from '../../test-utils/dom';

vi.mock('../../lib/firebase', () => ({ auth: { currentUser: null } }));

const api = vi.hoisted(() => ({
  chatHistory: vi.fn(),
  sendChat: vi.fn(),
  transcribe: vi.fn(),
  answerAudio: vi.fn(),
}));
vi.mock('../../lib/podcastApi', async (orig) => ({ ...(await orig<typeof import('../../lib/podcastApi')>()), ...api }));

// The recorder is a browser feature: replace it with one that "records" a blob as soon as the mic button is pressed.
const rec = vi.hoisted(() => ({ supported: true, state: 'idle' as 'idle' | 'recording' }));
vi.mock('../../lib/useRecorder', () => ({
  MAX_RECORDING_SECONDS: 40,
  recordingSupported: () => rec.supported,
  recordingUnavailableReason: () => (rec.supported ? null : 'Voice needs a secure connection'),
  useRecorder: (onAudio: (b: Blob) => void, onError: (m: string) => void) => ({
    state: rec.state,
    seconds: 3,
    start: async () => (rec.supported ? onAudio(new Blob(['voice'], { type: 'audio/webm' })) : onError('Microphone access was denied.')),
    stop: () => {},
  }),
}));

import { ChatPanel } from './ChatPanel';

const user = (id: string, content: string, mode: 'text' | 'voice' = 'text') => ({ id: `u_${id}`, role: 'user' as const, content, inputMode: mode });
const bot = (id: string, content: string) => ({ id: `a_${id}`, role: 'assistant' as const, content, grounded: true, replyTo: `u_${id}` });
const exchange = (id: string, q: string, a: string, mode: 'text' | 'voice' = 'text') => ({ user: user(id, q, mode), assistant: bot(id, a) });

let ui: ReturnType<typeof mount>;
const open = async () => {
  ui = mount(<ChatPanel pid="tech.2026-10-16.big-tech" />);
  await flush();
};

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset());
  api.chatHistory.mockResolvedValue([]);
  api.answerAudio.mockResolvedValue({ url: '/media/a.wav' });
  rec.supported = true;
  HTMLMediaElement.prototype.play = vi.fn().mockResolvedValue(undefined);
  HTMLMediaElement.prototype.pause = vi.fn();
});
afterEach(() => ui?.unmount());

const ask = async (text: string) => {
  await type(textarea('Your question')!, text);
  await click(button('Send'));
  await flush();
};

describe('typed questions', () => {
  it('shows the question immediately, then the answer, and never calls speech-to-text', async () => {
    let finish!: (v: unknown) => void;
    api.sendChat.mockReturnValue(new Promise((r) => (finish = r)));
    await open();
    await ask('How does retrieval work?');
    expect(document.body.textContent).toContain('How does retrieval work?'); // shown before the answer arrives
    expect(document.body.textContent).toContain('Getting an answer');
    expect(api.sendChat).toHaveBeenCalledWith('tech.2026-10-16.big-tech', expect.stringMatching(/^[A-Za-z0-9]{8,}/), 'How does retrieval work?', 'text');
    await act(() => finish(exchange('c1', 'How does retrieval work?', 'It finds similar chunks.')));
    expect(document.body.textContent).toContain('It finds similar chunks.');
    expect(api.transcribe).not.toHaveBeenCalled();
  });

  it('aligns user messages right and assistant messages left', async () => {
    api.chatHistory.mockResolvedValue([user('c1', 'Hi there'), bot('c1', 'Hello')]);
    await open();
    const row = (t: string) => Array.from(document.querySelectorAll('[role=log] > div')).find((d) => d.textContent?.includes(t))!;
    expect(row('Hi there').className).toContain('justify-end');
    expect(row('Hello').className).toContain('justify-start');
  });

  it('ignores a second submit while the first is in flight', async () => {
    api.sendChat.mockReturnValue(new Promise(() => {}));
    await open();
    await ask('First question');
    await type(textarea('Your question')!, 'Second question'); // disabled while busy, so typing and sending do nothing
    await click(button('Send'));
    expect(api.sendChat).toHaveBeenCalledTimes(1);
  });

  it('keeps the question when the answer fails and retries with the same request id', async () => {
    api.sendChat.mockRejectedValueOnce(new Error('The assistant is unavailable right now.'));
    await open();
    await ask('What is chunking?');
    expect(document.body.textContent).toContain('The assistant is unavailable');
    const firstId = api.sendChat.mock.calls[0][1];
    api.sendChat.mockResolvedValueOnce(exchange(firstId, 'What is chunking?', 'Splitting documents.'));
    await click(button('Retry'));
    await flush();
    expect(api.sendChat).toHaveBeenCalledTimes(2);
    expect(api.sendChat.mock.calls[1][1]).toBe(firstId); // idempotent: the server will not ask the model twice
    expect(document.body.textContent).toContain('Splitting documents.');
    expect(document.querySelectorAll('[role=log] > div').length).toBeGreaterThanOrEqual(2);
  });

  it('offers a retry for a question that was stored but never answered (refresh mid-request)', async () => {
    api.chatHistory.mockResolvedValue([user('abcdef123456', 'Unanswered one')]);
    await open();
    expect(document.body.textContent).toContain('No answer yet.');
    api.sendChat.mockResolvedValueOnce(exchange('abcdef123456', 'Unanswered one', 'Now answered.'));
    await click(button('Retry'));
    await flush();
    expect(api.sendChat.mock.calls[0][1]).toBe('abcdef123456');
    expect(document.body.textContent).toContain('Now answered.');
  });
});

describe('voice questions', () => {
  it('transcribes with STT, shows the recognised text for review, and only then sends it through the same chat call', async () => {
    api.transcribe.mockResolvedValue({ text: 'what is a vector database' });
    await open();
    await click(button('Record a question'));
    await flush();
    expect(api.transcribe).toHaveBeenCalledTimes(1);
    expect(api.sendChat).not.toHaveBeenCalled(); // nothing is sent silently
    expect(textarea('Your question')!.value).toBe('what is a vector database'); // visible, editable
    expect(document.body.textContent).toContain('This is what we heard');
    await type(textarea('Your question')!, 'What is a vector database?'); // the listener corrects it
    api.sendChat.mockResolvedValue(exchange('c1', 'What is a vector database?', 'A store for embeddings.', 'voice'));
    await click(button('Send'));
    await flush();
    expect(api.sendChat).toHaveBeenCalledWith('tech.2026-10-16.big-tech', expect.any(String), 'What is a vector database?', 'voice');
    expect(document.body.textContent).toContain('Voice question');
    expect(document.body.textContent).toContain('A store for embeddings.');
  });

  it('a failed transcription shows an error and never reaches the language model', async () => {
    api.transcribe.mockRejectedValue(new Error('We could not transcribe that. Try again or type your question.'));
    await open();
    await click(button('Record a question'));
    await flush();
    expect(document.body.textContent).toContain('We could not transcribe that');
    expect(api.sendChat).not.toHaveBeenCalled();
    expect(textarea('Your question')!.value).toBe('');
    await type(textarea('Your question')!, 'Typing works instead');
    expect((button('Send') as HTMLButtonElement).disabled).toBe(false);
  });

  it('reports a denied microphone and an unsupported browser', async () => {
    rec.supported = true;
    await open();
    ui.unmount();
    rec.supported = false;
    await open();
    expect(document.body.textContent).toContain('Voice is not supported here');
    expect(button('Record a question')).toBeUndefined();
    expect(button('Send')).toBeTruthy(); // typing still works
  });
});

describe('spoken answers', () => {
  it('asks Gemini TTS for the answer and plays it; the text is already visible', async () => {
    api.chatHistory.mockResolvedValue([user('c1', 'q'), bot('c1', 'The spoken text')]);
    await open();
    expect(document.body.textContent).toContain('The spoken text');
    await click(button('Listen'));
    await flush();
    expect(api.answerAudio).toHaveBeenCalledWith('tech.2026-10-16.big-tech', 'a_c1');
    expect((document.querySelector('audio[aria-label="Spoken answer"]') as HTMLAudioElement).getAttribute('src')).toBe('/media/a.wav');
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalled();
  });

  it('keeps the text and offers a retry when TTS fails, without asking the model again', async () => {
    api.chatHistory.mockResolvedValue([user('c1', 'q'), bot('c1', 'Still readable')]);
    api.answerAudio.mockRejectedValueOnce(new Error('tts down'));
    await open();
    await click(button('Listen'));
    await flush();
    expect(document.body.textContent).toContain('Still readable');
    expect(document.body.textContent).toContain('The spoken answer could not be made');
    await click(button('Retry audio'));
    await flush();
    expect(api.answerAudio).toHaveBeenCalledTimes(2);
    expect(api.sendChat).not.toHaveBeenCalled();
    expect(document.querySelector('audio')).not.toBeNull();
  });

  it('shows the player controls when the browser blocks autoplay', async () => {
    HTMLMediaElement.prototype.play = vi.fn().mockRejectedValue(new DOMException('blocked', 'NotAllowedError'));
    api.chatHistory.mockResolvedValue([user('c1', 'q'), bot('c1', 'Answer')]);
    await open();
    await click(button('Listen'));
    await flush();
    expect((document.querySelector('audio') as HTMLAudioElement).controls).toBe(true); // the user can press play
  });
});

import { act as reactAct } from 'react';
function act<T>(fn: () => T | Promise<T>) {
  return reactAct(async () => {
    await fn();
  });
}
