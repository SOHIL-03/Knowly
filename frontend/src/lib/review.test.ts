import { describe, expect, it } from 'vitest';
import { isModified, prettyJson, scriptEntry, scriptToText, type TraceEntry } from './review';
import type { ScriptLine } from './types';

const lines: ScriptLine[] = [
  { speaker: 'A', speakerName: 'Maya', text: 'Hello there.', item_ids: [] },
  { speaker: 'B', speakerName: 'Theo', text: 'Hi Maya.', item_ids: [] },
];

describe('script review helpers', () => {
  it('keeps speaker labels and paragraph breaks when copying', () => {
    expect(scriptToText(lines)).toBe('Maya: Hello there.\n\nTheo: Hi Maya.');
  });
  it('copies edited text with the original labels', () => {
    expect(scriptToText(lines, ['Hello there.', 'Edited.'])).toBe('Maya: Hello there.\n\nTheo: Edited.');
  });
  it('detects edits', () => {
    expect(isModified(lines, ['Hello there.', 'Hi Maya.'])).toBe(false);
    expect(isModified(lines, ['Hello there.', 'Hi Maya!'])).toBe(true);
    expect(isModified(lines, ['Hello there.'])).toBe(true);
  });
  it('pretty prints JSON for display but leaves other text unchanged', () => {
    expect(prettyJson('{"a":1}')).toBe('{\n  "a": 1\n}');
    expect(prettyJson('not json {')).toBe('not json {');
  });
  it('picks the script attempt that passed validation for the segment', () => {
    const e = (seq: number, segId: string, ok: boolean, status: TraceEntry['status'] = 'ok'): TraceEntry => ({
      seq, step: 'PodcastScript', label: 'Podcast script', segId, model: 'gpt-4o-mini', attempt: 1, status,
      appValidation: { passed: ok, problems: [] },
    });
    const entries = [e(1, 's1', false), e(2, 's1', true), e(3, 's2', true), e(4, 's1', false, 'validation_failed')];
    expect(scriptEntry(entries, 's1')?.seq).toBe(2);
    expect(scriptEntry(entries, 's9')).toBeUndefined();
  });
});
