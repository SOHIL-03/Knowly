import { api, ApiError, get, post, put } from './api';
import type { PodcastState } from '../components/TldrPodcast';

export type TimedLine = { s: number; e: number };
export type PodcastPage = PodcastState & { podcastId: string; lineTimings?: TimedLine[] | null; conceptsStatus?: string | null };

export type ChatMessage = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  inputMode?: 'text' | 'voice' | null;
  grounded?: boolean | null;
  clarifyingQuestion?: string | null;
  createdAt?: string | null;
  replyTo?: string | null;
};
export type ChatExchange = { user: ChatMessage; assistant: ChatMessage };

export type AssessmentQuestion = { id: string; type: 'mcq' | 'explain' | 'scenario' | 'compare'; prompt: string; options: string[]; conceptNames: string[] };
export type EvalStatus = 'demonstrated' | 'partial' | 'incorrect' | 'insufficient' | 'not_assessed';
export type AssessmentResult = {
  question_id: string;
  answer: string;
  evaluation_status: EvalStatus;
  correctness_score: number;
  feedback: string;
  evidence: string;
  mastery?: Record<string, { after: string | null; reason: string } | null>;
};
export type Assessment = {
  id: string;
  podcastId: string;
  status: 'created' | 'in_progress' | 'submitted' | 'evaluating' | 'completed' | 'failed';
  questions: AssessmentQuestion[];
  answers: Record<string, string>;
  results: AssessmentResult[] | null;
  score: number | null;
  message?: string | null;
  attempt: number;
};
export type MasteryStatus = 'unknown' | 'learning' | 'understood' | 'needs_review';
export type LearningProfile = {
  concepts: { conceptId: string; name: string; status: MasteryStatus; evidenceCount: number; correctEvidenceCount: number; incorrectEvidenceCount: number }[];
  counts: Record<MasteryStatus, number>;
};

/** podcast id = category.date.section[.variant]; the id is all the page needs to find the podcast again. */
export const podcastUrl = (id: string) => `/podcast/${encodeURIComponent(id)}`;

/** The podcast is readable without signing in (shared ones); a signed-in user also sees their private episodes. */
export async function fetchPodcast(pid: string): Promise<PodcastPage> {
  const { auth } = await import('./firebase');
  const user = auth.currentUser;
  const res = await fetch(`/api/podcasts/${encodeURIComponent(pid)}`, { headers: user ? { Authorization: `Bearer ${await user.getIdToken()}` } : {} });
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, body?.error?.code || 'http_error', body?.error?.message || `Request failed (${res.status})`);
  return body as PodcastPage;
}

const base = (pid: string) => `/podcasts/${encodeURIComponent(pid)}`;

export const chatHistory = (pid: string) => get<{ messages: ChatMessage[] }>(`${base(pid)}/chat`).then((r) => r.messages);
export const sendChat = (pid: string, clientId: string, text: string, inputMode: 'text' | 'voice') =>
  post<ChatExchange>(`${base(pid)}/chat`, { clientId, text, inputMode });
export const answerAudio = (pid: string, messageId: string) => post<{ url: string }>(`${base(pid)}/chat/${encodeURIComponent(messageId)}/audio`);
export const transcribe = (pid: string, audio: Blob) =>
  api<{ text: string }>(`${base(pid)}/transcribe`, { method: 'POST', body: audio, headers: { 'Content-Type': audio.type || 'audio/webm' } });

export const listAssessments = (pid: string) => get<{ assessments: Assessment[] }>(`${base(pid)}/assessments`).then((r) => r.assessments);
export const startAssessment = (pid: string, retake = false) => post<Assessment>(`${base(pid)}/assessments`, { retake });
export const saveAnswer = (aid: string, qid: string, answer: string) => put<unknown>(`/assessments/${aid}/answers/${qid}`, { answer });
export const submitAssessment = (aid: string) => post<Assessment>(`/assessments/${aid}/submit`);
export const learningProfile = () => get<LearningProfile>('/learning/profile');

export const newClientId = () =>
  typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID().replace(/-/g, '').slice(0, 32) : `${Date.now()}${Math.random().toString(36).slice(2, 10)}`;
