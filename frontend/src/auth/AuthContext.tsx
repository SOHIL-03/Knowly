import {
  GoogleAuthProvider,
  linkWithPopup,
  onAuthStateChanged,
  signInAnonymously,
  signInWithPopup,
  signOut as fbSignOut,
  type User,
} from 'firebase/auth';
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { ApiError, get, post } from '../lib/api';
import { auth } from '../lib/firebase';
import type { Me } from '../lib/types';

interface AuthState {
  user: User | null;
  me: Me | null;
  loading: boolean;
  error: string | null;
  signInWithGoogle: () => Promise<void>;
  signInForSample: () => Promise<void>;
  linkGoogle: () => Promise<void>;
  signOut: () => Promise<void>;
  refreshMe: () => Promise<Me | null>;
}

const Ctx = createContext<AuthState | null>(null);

const timezone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refreshMe = useCallback(async () => {
    try {
      const m = await get<Me>('/me');
      setMe(m);
      return m;
    } catch (e) {
      if (e instanceof ApiError && e.code === 'not_bootstrapped') {
        const m = await post<Me>('/me/bootstrap', { timezone: timezone() });
        setMe(m);
        return m;
      }
      throw e;
    }
  }, []);

  useEffect(
    () =>
      onAuthStateChanged(auth, async (u) => {
        setUser(u);
        setError(null);
        if (!u) {
          setMe(null);
          setLoading(false);
          return;
        }
        try {
          // Bootstrap is idempotent: creates users/{uid} once, then keeps the timezone current.
          setMe(await post<Me>('/me/bootstrap', { timezone: timezone() }));
        } catch (e) {
          setError(e instanceof Error ? e.message : 'Could not load your account');
        } finally {
          setLoading(false);
        }
      }),
    [],
  );

  const value = useMemo<AuthState>(
    () => ({
      user,
      me,
      loading,
      error,
      refreshMe,
      signInWithGoogle: async () => {
        setError(null);
        await signInWithPopup(auth, new GoogleAuthProvider());
      },
      signInForSample: async () => {
        setError(null);
        await signInAnonymously(auth);
      },
      linkGoogle: async () => {
        if (!auth.currentUser) return;
        await linkWithPopup(auth.currentUser, new GoogleAuthProvider());
        await auth.currentUser.getIdToken(true);
        await refreshMe();
      },
      signOut: async () => {
        await fbSignOut(auth);
        setMe(null);
      },
    }),
    [user, me, loading, error, refreshMe],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const v = useContext(Ctx);
  if (!v) throw new Error('useAuth outside AuthProvider');
  return v;
}
