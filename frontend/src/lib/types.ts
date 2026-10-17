export type SegmentType = 'project' | 'learning' | 'quickhits' | 'review' | 'deeper';
/** 'review': the script is written and waits for the user to press Generate Audio. */
export type AudioStatus = 'review' | 'pending' | 'processing' | 'ok' | 'failed';

export interface Source {
  itemId: string;
  headline: string;
  newsletter: string;
  url: string | null;
  linkOnly: boolean;
  reason?: string | null;
  /** The declared interest that matched this story (project, goal, ...), when semantic ranking chose it. */
  matchedFacet?: { id: string; type: string | null; text: string | null } | null;
}

export interface ScriptLine {
  speaker: 'A' | 'B';
  speakerName: string;
  text: string;
  style?: string | null;
  item_ids: string[];
}

export interface Segment {
  id: string;
  order: number;
  type: SegmentType;
  title: string;
  whyChosen: string;
  itemIds: string[];
  conceptIds: string[];
  script?: ScriptLine[];
  lineTimings?: { s: number; e: number }[];
  takeaways?: string[];
  openingQuestion?: string;
  style?: 'twoHost' | 'brief';
  voices?: Record<string, string>;
  durationSec: number;
  audioStatus: AudioStatus;
  audioMessage?: string | null;
  audioUrl: string | null;
  faithfulness?: 'pass' | 'review' | 'fail' | 'unchecked' | null;
  scriptMode?: 'model' | 'fallback';
  scriptEdited?: boolean;
  removedByUser?: boolean;
  wildcardItemId?: string | null;
  status?: 'generating' | 'ready' | 'failed';
  sources: Source[];
}

export interface Progress {
  stage: string;
  message: string;
  issues?: number;
  storiesFound?: number;
  storiesPicked?: number;
}

export interface Briefing {
  id: string;
  date: string;
  kind: 'daily' | 'review' | 'onArrival';
  mode: 'sample' | 'live';
  status: 'generating' | 'ready' | 'failed' | 'empty';
  progress?: Progress;
  error?: { message: string } | null;
  totalDurationSec?: number;
  itemIds?: string[];
  alsoInInbox?: { itemId: string; headline: string; url: string | null; senderName: string }[];
  recap?: { takeaways: string[]; quizScore: { points: number; total: number } | null; savedCount: number };
  rating?: 'up' | 'down';
  reviewBeforeAudio?: boolean;
  segments: Segment[];
}

export interface Prefs {
  lengthMin: 5 | 10 | 15;
  style: 'twoHost' | 'brief';
  speed: number;
  schedule: { mode: 'daily' | 'onArrival'; time: string; days: number[] };
  notify: 'push' | 'email' | 'none';
  wildcardOn: boolean;
  endQuizOn: boolean;
}

export interface Me {
  uid: string;
  anonymous: boolean;
  email?: string | null;
  mode: 'sample' | 'live';
  timezone: string;
  onboarded: boolean;
  prefs: Prefs;
  gmail: { status: 'ok' | 'reauth' | 'disconnected'; email?: string | null };
  profile: {
    project?: { text: string; readmeUrl?: string | null; summary?: string | null };
    goals?: { topic: string }[];
    muteTopics?: string[];
  };
}

export interface Citation {
  itemId: string;
  newsletter: string;
  headline: string;
  url: string | null;
}

export interface Concept {
  id: string;
  name: string;
  mastery: number;
  evidenceCount: number;
  selfRating: 'new' | 'heard' | 'use' | null;
  nextDueAt: string | null;
  lastReviewedAt: string | null;
  userCorrected: boolean;
  band: 'define' | 'remind' | 'skip';
}

export interface LibraryItem {
  id: string;
  itemId: string;
  headline: string;
  blurb: string;
  url: string | null;
  senderId: string;
  senderName: string;
  note: string;
  tags: string[];
  savedAt: string;
  reviewDueAt: string | null;
  source: 'save' | 'deeper' | 'qa';
}

export interface QaRecord {
  id: string;
  question: string;
  answer: string;
  citations: Citation[];
  scope: string;
  kind: 'ask' | 'apply';
  ts: string;
}
