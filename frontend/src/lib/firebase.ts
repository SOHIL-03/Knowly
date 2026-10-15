import { initializeApp } from 'firebase/app';
import { connectAuthEmulator, getAuth } from 'firebase/auth';
import { connectFirestoreEmulator, getFirestore } from 'firebase/firestore';

const env = import.meta.env;
// Emulators when asked for, or in `npm run dev` when no Firebase web config is set (local default).
export const usingEmulators =
  env.VITE_USE_EMULATORS === 'true' || (env.DEV && env.VITE_USE_EMULATORS !== 'false' && !env.VITE_FIREBASE_API_KEY);

const config = {
  apiKey: env.VITE_FIREBASE_API_KEY || (usingEmulators ? 'demo-key' : ''),
  authDomain: env.VITE_FIREBASE_AUTH_DOMAIN || undefined,
  projectId: env.VITE_FIREBASE_PROJECT_ID || 'demo-knowly',
  appId: env.VITE_FIREBASE_APP_ID || undefined,
  messagingSenderId: env.VITE_FIREBASE_MESSAGING_SENDER_ID || undefined,
};

export const firebaseConfigured = Boolean(config.apiKey);
export const app = initializeApp(config);
export const auth = getAuth(app);
// The browser only reads Firestore (live progress); every write goes through the API.
export const db = getFirestore(app);

if (usingEmulators) {
  // Same machine as the page, so the app also works when opened from another device on the network.
  const host = env.VITE_EMULATOR_HOST || (typeof window !== 'undefined' ? window.location.hostname : '127.0.0.1');
  connectAuthEmulator(auth, env.VITE_AUTH_EMULATOR_URL || `http://${host}:9099`, { disableWarnings: true });
  connectFirestoreEmulator(db, host, Number(env.VITE_FIRESTORE_EMULATOR_PORT || 8080));
}

export const gmailClientId: string = env.VITE_GOOGLE_OAUTH_CLIENT_ID || '';
export const vapidKey: string = env.VITE_FIREBASE_VAPID_KEY || '';
export const sampleAudioUrl: string = env.VITE_SAMPLE_AUDIO_URL || '';
