import { getMessaging, getToken, isSupported } from 'firebase/messaging';
import { post } from './api';
import { app, vapidKey } from './firebase';

/**
 * Registers this browser for "briefing ready" pushes through Firebase Cloud Messaging.
 * Returns false (and changes nothing) when FCM is not configured or not supported.
 */
export async function enablePush(): Promise<boolean> {
  if (!vapidKey || !(await isSupported()) || !('serviceWorker' in navigator)) return false;
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') return false;
  const o = app.options;
  const cfg = encodeURIComponent(JSON.stringify({ apiKey: o.apiKey, projectId: o.projectId, appId: o.appId, messagingSenderId: o.messagingSenderId }));
  const reg = await navigator.serviceWorker.register(`/firebase-messaging-sw.js?config=${cfg}`);
  const token = await getToken(getMessaging(app), { vapidKey, serviceWorkerRegistration: reg });
  if (!token) return false;
  await post('/notifications/token', { token });
  return true;
}
