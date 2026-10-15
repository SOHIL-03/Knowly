import { lazy, Suspense, type ReactNode } from 'react';
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { AuthProvider, useAuth } from './auth/AuthContext';
import { Banner, Spinner } from './components/ui';
import { Landing } from './pages/Landing';

const ConnectGmail = lazy(() => import('./pages/ConnectGmail').then((m) => ({ default: m.ConnectGmail })));
const Generating = lazy(() => import('./pages/Generating').then((m) => ({ default: m.Generating })));
const Knowledge = lazy(() => import('./pages/Knowledge').then((m) => ({ default: m.Knowledge })));
const Library = lazy(() => import('./pages/Library').then((m) => ({ default: m.Library })));
const Onboarding = lazy(() => import('./pages/Onboarding').then((m) => ({ default: m.Onboarding })));
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
  if (loading) return <div className="flex min-h-dvh items-center justify-center"><Spinner /></div>;
  if (user && me) return <Navigate to={me.onboarded ? '/today' : '/onboarding'} replace />;
  return <Landing />;
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Suspense fallback={<div className="flex min-h-dvh items-center justify-center"><Spinner /></div>}>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/onboarding" element={<Gate allowUnonboarded><Onboarding /></Gate>} />
          <Route path="/connect-gmail" element={<Gate allowUnonboarded><ConnectGmail /></Gate>} />
          <Route path="/generating/:briefingId" element={<Gate><Generating /></Gate>} />
          <Route path="/today" element={<Gate><Today /></Gate>} />
          <Route path="/play/:briefingId" element={<Gate><Player /></Gate>} />
          <Route path="/recap/:briefingId" element={<Gate><Recap /></Gate>} />
          <Route path="/library" element={<Gate><Library /></Gate>} />
          <Route path="/knowledge" element={<Gate><Knowledge /></Gate>} />
          <Route path="/settings" element={<Gate><Settings /></Gate>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        </Suspense>
      </BrowserRouter>
    </AuthProvider>
  );
}
