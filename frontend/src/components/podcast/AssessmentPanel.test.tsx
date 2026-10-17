import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { button, click, flush, mount, textarea, type } from '../../test-utils/dom';

vi.mock('../../lib/firebase', () => ({ auth: { currentUser: null } }));
const api = vi.hoisted(() => ({
  listAssessments: vi.fn(), startAssessment: vi.fn(), saveAnswer: vi.fn(), submitAssessment: vi.fn(), learningProfile: vi.fn(),
}));
vi.mock('../../lib/podcastApi', async (orig) => ({ ...(await orig<typeof import('../../lib/podcastApi')>()), ...api }));

import type { Assessment } from '../../lib/podcastApi';
import { AssessmentPanel } from './AssessmentPanel';

const PID = 'tech.2026-10-16.big-tech';
const quiz = (over: Partial<Assessment> = {}): Assessment => ({
  id: 'a1', podcastId: PID, status: 'created', attempt: 1, score: null, results: null, answers: {},
  questions: [
    { id: 'q1', type: 'mcq', prompt: 'Which is right?', options: ['Wrong', 'Right', 'Other'], conceptNames: ['vector search'] },
    { id: 'q2', type: 'explain', prompt: 'Explain embeddings.', options: [], conceptNames: ['embeddings'] },
  ],
  ...over,
});
const done = (): Assessment => quiz({
  status: 'completed', score: 0.5, answers: { q1: '1' },
  results: [
    { question_id: 'q1', answer: 'Right', evaluation_status: 'demonstrated', correctness_score: 1, feedback: 'Correct.', evidence: 'Right', mastery: { vector_search: { after: 'learning', reason: 'first evidence' } } },
    { question_id: 'q2', answer: '', evaluation_status: 'not_assessed', correctness_score: 0, feedback: 'You skipped this question, so it does not count.', evidence: '' },
  ],
});

let ui: ReturnType<typeof mount>;
const open = async () => { ui = mount(<AssessmentPanel pid={PID} />); await flush(); };

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset());
  api.listAssessments.mockResolvedValue([]);
  api.saveAnswer.mockResolvedValue({});
  api.learningProfile.mockResolvedValue({ concepts: [{ conceptId: 'vector_search', name: 'vector search', status: 'learning', evidenceCount: 1, correctEvidenceCount: 1, incorrectEvidenceCount: 0 }], counts: {} });
});
afterEach(() => ui?.unmount());

describe('Test Your Understanding', () => {
  it('starts only when the user asks, and never treats listening as understanding', async () => {
    await open();
    expect(api.startAssessment).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain('never counts as understanding');
    api.startAssessment.mockResolvedValue(quiz());
    await click(button('Test Your Understanding'));
    await flush();
    expect(api.startAssessment).toHaveBeenCalledWith(PID);
    expect(document.body.textContent).toContain('0 of 2 answered');
    expect(document.body.textContent).toContain('Which is right?');
  });

  it('saves each answer to the server before anything is graded and shows progress', async () => {
    api.listAssessments.mockResolvedValue([quiz()]);
    await open();
    await click(document.querySelectorAll('input[type=radio]')[1]); // the second option
    expect(api.saveAnswer).toHaveBeenCalledWith('a1', 'q1', '1');
    expect(document.body.textContent).toContain('1 of 2 answered');
    expect(api.submitAssessment).not.toHaveBeenCalled();
  });

  it('restores saved answers after a refresh', async () => {
    api.listAssessments.mockResolvedValue([quiz({ status: 'in_progress', answers: { q2: 'My earlier explanation' } })]);
    await open();
    expect(textarea('Answer to question 2')!.value).toBe('My earlier explanation');
    expect(document.body.textContent).toContain('1 of 2 answered');
  });

  it('flushes unsaved text, submits once, and ignores a second click while grading', async () => {
    api.listAssessments.mockResolvedValue([quiz({ status: 'in_progress', answers: {} })]);
    let finish!: (v: unknown) => void;
    api.submitAssessment.mockReturnValue(new Promise((r) => (finish = r)));
    await open();
    await type(textarea('Answer to question 2')!, 'Embeddings map text to vectors');
    await click(button(/Submit answers/));
    await flush();
    expect(api.saveAnswer).toHaveBeenCalledWith('a1', 'q2', 'Embeddings map text to vectors'); // stored before grading
    expect(api.submitAssessment).toHaveBeenCalledTimes(1);
    expect(document.body.textContent).toContain('Checking your answers');
    await click(button(/Checking your answers/));
    expect(api.submitAssessment).toHaveBeenCalledTimes(1);
    await import('react').then(({ act }) => act(async () => { finish(done()); }));
    expect(document.body.textContent).toContain('Score 50%');
  });

  it('shows feedback, evidence-based labels and saved progress, and says skipped questions do not count', async () => {
    api.listAssessments.mockResolvedValue([done()]);
    await open();
    await flush();
    const text = document.body.textContent ?? '';
    expect(text).toContain('Shows understanding');
    expect(text).toContain('Skipped, does not count');
    expect(text).toContain('You skipped this question, so it does not count.');
    expect(text).toContain('vector search: Learning'); // a correct answer is only "learning" until the policy says otherwise
    expect(text).toContain('Your saved progress');
    expect(text).not.toContain('Understood');
  });

  it('keeps the saved answers and lets the user submit again when evaluation failed', async () => {
    api.listAssessments.mockResolvedValue([quiz({ status: 'failed', answers: { q1: '1' }, message: 'Your answers are saved, but they could not be evaluated yet. Submit again.' })]);
    await open();
    expect(document.body.textContent).toContain('your answers are saved');
    expect(button('Submit again')).toBeTruthy();
    expect(document.body.textContent).toContain('1 of 2 answered');
  });
});
