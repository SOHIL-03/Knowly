import { post } from './api';

export type EventType =
  | 'play' | 'pause' | 'skip' | 'replay' | 'openSource' | 'lessLikeThis' | 'cardRemoved' | 'speedChange' | 'complete';

export interface PlayerEvent {
  type: EventType;
  briefingId?: string;
  segId?: string;
  itemId?: string;
  conceptId?: string;
  payload?: Record<string, string | number | boolean>;
}

let queue: PlayerEvent[] = [];
let timer: ReturnType<typeof setInterval> | null = null;

/** Events are batched and sent every 10 seconds and on pause (Architecture v3 section 4.1). */
export function track(ev: PlayerEvent) {
  queue.push(ev);
  if (!timer) timer = setInterval(() => void flush(), 10_000);
  if (ev.type === 'pause' || ev.type === 'lessLikeThis') void flush();
}

export async function flush() {
  if (!queue.length) return;
  const batch = queue;
  queue = [];
  try {
    await post('/events', { events: batch });
  } catch {
    queue = batch.concat(queue).slice(-200); // keep for the next attempt
  }
}

if (typeof window !== 'undefined') {
  window.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') void flush();
  });
}
