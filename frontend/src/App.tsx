import { lazy, Suspense, type ReactNode } from 'react';
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { AuthProvider, useAuth } from './auth/AuthContext';
import { Banner, Spinner } from './components/ui';
import { DEMO_MODE } from './lib/demo';
import { IssuePage } from './pages/IssuePage';
import { Landing } from './pages/Landing';

const ConnectGmail = lazy(() => import('./pages/ConnectGmail').then((m) => ({ default: m.ConnectGmail })));
const Generating = lazy(() => import('./pages/Generating').then((m) => ({ default: m.Generating })));
const Knowledge = lazy(() => import('./pages/Knowledge').then((m) => ({ default: m.Knowledge })));
const Library = lazy(() => import('./pages/Library').then((m) => ({ default: m.Library })));
const Onboarding = lazy(() => import('./pages/Onboarding').then((m) => ({ default: m.Onboarding })));
const PodcastPage = lazy(() => import('./pages/PodcastPage').then((m) => ({ default: m.PodcastPage })));
const Player = lazy(() => import('./pages/Player').then((m) => ({ default: m.Player })));
const Recap = lazy(() => import('./pages/Recap').then((m) => ({ default: m.Recap })));
const Settings = lazy(() => import('./pages/Settings').then((m) => ({ default: m.Settings })));
const Today = lazy(() => import('./pages/Today').then((m) => ({ default: m.Today })));

function Gate({ children, allowUnonboarded }: { children: ReactNode; allowUnonboarded?: boolean }) {
  const { user, me, loading, error } = useAuth();
  const location = useLocation();
  if (loading) {
    return (
      <div className="flex min-h-dvh items-center justify-center">
        <Spinner />
      </div>
    );
  }
  if (!user) return <Navigate to="/" replace state={{ from: location.pathname }} />;
  if (error || !me) {
    return (
      <div className="mx-auto max-w-md p-6">
        <Banner tone="error">{error || 'Could not load your account.'}</Banner>
      </div>
    );
  }
  if (!me.onboarded && !allowUnonboarded) return <Navigate to="/onboarding" replace />;
  return <>{children}</>;
}

function Home() {
  const { user, me, loading } = useAuth();
  if (DEMO_MODE) return <Landing />; // always shown in the demo, even when already signed in
  if (loading) return <div className="flex min-h-dvh items-center justify-center"><Spinner /></div>;
  if (user && me) return <Navigate to={me.onboarded ? '/today' : '/onboarding'} replace />;
  return <Landing />;
}

/** The Gmail briefing pages are hidden in demo mode (redirect to the landing page) but stay in the codebase. */
function Hidden({ children }: { children: ReactNode }) {
  return DEMO_MODE ? <Navigate to="/" replace /> : <>{children}</>;
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Suspense fallback={<div className="flex min-h-dvh items-center justify-center"><Spinner /></div>}>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/onboarding" element={<Hidden><Gate allowUnonboarded><Onboarding /></Gate></Hidden>} />
          <Route path="/connect-gmail" element={<Hidden><Gate allowUnonboarded><ConnectGmail /></Gate></Hidden>} />
          <Route path="/generating/:briefingId" element={<Hidden><Gate><Generating /></Gate></Hidden>} />
          <Route path="/tldr" element={<IssuePage />} />
          <Route path="/tldr/:date" element={<IssuePage />} />
          <Route path="/podcast/:pid" element={<PodcastPage />} />
          <Route path="/today" element={<Hidden><Gate><Today /></Gate></Hidden>} />
          <Route path="/play/:briefingId" element={<Hidden><Gate><Player /></Gate></Hidden>} />
          <Route path="/recap/:briefingId" element={<Hidden><Gate><Recap /></Gate></Hidden>} />
          <Route path="/library" element={<Hidden><Gate><Library /></Gate></Hidden>} />
          <Route path="/knowledge" element={<Hidden><Gate><Knowledge /></Gate></Hidden>} />
          <Route path="/settings" element={<Hidden><Gate><Settings /></Gate></Hidden>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        </Suspense>
      </BrowserRouter>
    </AuthProvider>
  );
}
