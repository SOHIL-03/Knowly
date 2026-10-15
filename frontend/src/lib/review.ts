import type { ScriptLine } from './types';

const KEY = 'knowly.reviewBeforeAudio';

/** The user's choice for "review the script before audio": true/false, or undefined to use the server default. */
export function reviewPref(): boolean | undefined {
  try {
    const v = localStorage.getItem(KEY);
    return v === 'on' ? true : v === 'off' ? false : undefined;
  } catch {
    return undefined;
  }
}

export function setReviewPref(on: boolean) {
  try {
    localStorage.setItem(KEY, on ? 'on' : 'off');
  } catch {
    /* storage unavailable: the choice just is not remembered */
  }
}

/** True when any line text differs from the generated script. */
export function isModified(lines: ScriptLine[], texts: string[]): boolean {
  return lines.length !== texts.length || lines.some((l, i) => l.text !== texts[i]);
}

/** Plain-text script with speaker labels and a blank line between turns (what the Copy button copies). */
export function scriptToText(lines: ScriptLine[], texts: string[] = lines.map((l) => l.text)): string {
  return lines.map((l, i) => `${l.speakerName}: ${texts[i] ?? l.text}`).join('\n\n');
}

export interface TraceEntry {
  seq: number;
  step: string;
  label: string;
  segId?: string;
  segTitle?: string;
  model: string;
  attempt: number;
  status: 'ok' | 'validation_failed' | 'empty' | 'blocked' | 'request_failed';
  promptPreview?: string;
  promptChars?: number;
  rawResponse?: string | null;
  parsed?: unknown;
  error?: string;
  category?: string;
  finishReason?: string | null;
  durationMs?: number;
  usage?: { input: number | null; output: number | null; total: number | null } | null;
  appValidation?: { passed: boolean; problems: string[] };
  faithfulness?: string;
}

export interface Trace {
  enabled: boolean;
  model: string;
  entries: TraceEntry[];
}

/** The model response behind a segment's script: the latest script attempt that passed validation. */
export function scriptEntry(entries: TraceEntry[], segId: string): TraceEntry | undefined {
  const own = entries.filter((e) => e.step === 'PodcastScript' && e.segId === segId && e.status === 'ok');
  return [...own].reverse().find((e) => e.appValidation?.passed) ?? undefined;
}

/** Pretty-prints raw JSON for display only; text that is not valid JSON is shown unchanged. */
export function prettyJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}
