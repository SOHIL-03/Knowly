import { describe, expect, it } from 'vitest';
import { formatTime, lineAt, minutes } from './transcript';

const lines = [
  { s: 0, e: 2.5 },
  { s: 2.5, e: 6 },
  { s: 6, e: 7.5 },
  { s: 7.5, e: 9 },
];

describe('lineAt', () => {
  it('finds the line being spoken', () => {
    expect(lineAt(lines, 0)).toBe(0);
    expect(lineAt(lines, 3)).toBe(1);
    expect(lineAt(lines, 8)).toBe(3);
    expect(lineAt(lines, 99)).toBe(3);
  });
  it('returns -1 before the first line or for no lines', () => {
    expect(lineAt(lines, -1)).toBe(-1);
    expect(lineAt([], 1)).toBe(-1);
  });
});

describe('formatting', () => {
  it('formats clock time and minutes', () => {
    expect(formatTime(75)).toBe('1:15');
    expect(formatTime(NaN)).toBe('0:00');
    expect(minutes(172)).toBe('3 min');
    expect(minutes(20)).toBe('1 min');
  });
});
