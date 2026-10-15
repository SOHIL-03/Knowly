import { collection, doc, onSnapshot } from 'firebase/firestore';
import { useCallback, useEffect, useRef, useState } from 'react';
import { get } from './api';
import { db } from './firebase';
import type { Briefing } from './types';

/**
 * Briefing view from the API (it adds signed audio URLs and resolved sources), kept fresh by
 * Firestore listeners on the briefing document and its segments. Reads only: the security rules
 * allow a user to read their own subtree and nothing else.
 */
export function useBriefing(uid: string | undefined, briefingId: string | undefined) {
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null);

  const reload = useCallback(async () => {
    if (!briefingId) return;
    try {
      setBriefing(await get<Briefing>(`/briefings/${encodeURIComponent(briefingId)}`));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load the briefing');
    } finally {
      setLoading(false);
    }
  }, [briefingId]);

  useEffect(() => {
    if (!uid || !briefingId) return;
    setLoading(true);
    void reload();
    const schedule = () => {
      if (pending.current) clearTimeout(pending.current);
      pending.current = setTimeout(() => void reload(), 400);
    };
    const ref = doc(db, 'users', uid, 'briefings', briefingId);
    const unsubB = onSnapshot(ref, schedule, () => undefined);
    const unsubS = onSnapshot(collection(ref, 'segments'), schedule, () => undefined);
    return () => {
      unsubB();
      unsubS();
      if (pending.current) clearTimeout(pending.current);
    };
  }, [uid, briefingId, reload]);

  return { briefing, error, loading, reload, setBriefing };
}

export interface LiveProgress {
  status: Briefing['status'];
  progress?: Briefing['progress'];
  error?: { message: string } | null;
}

/** Real-time pipeline progress for the Generating screen (no polling). */
export function useBriefingProgress(uid: string | undefined, briefingId: string | undefined) {
  const [state, setState] = useState<LiveProgress | null>(null);
  useEffect(() => {
    if (!uid || !briefingId) return;
    return onSnapshot(
      doc(db, 'users', uid, 'briefings', briefingId),
      (snap) => setState((snap.data() as LiveProgress) || null),
      () => setState({ status: 'failed', error: { message: 'Lost connection to progress updates' } }),
    );
  }, [uid, briefingId]);
  return state;
}
