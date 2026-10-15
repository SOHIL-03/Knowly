// Firebase Cloud Messaging background handler. The web config arrives in the registration URL
// (see src/lib/push.ts) so no keys are hard-coded here.
importScripts('https://www.gstatic.com/firebasejs/13.0.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/13.0.0/firebase-messaging-compat.js');

const config = JSON.parse(new URL(self.location).searchParams.get('config') || '{}');
if (config.apiKey) {
  firebase.initializeApp(config);
  firebase.messaging().onBackgroundMessage((payload) => {
    const n = payload.notification || {};
    self.registration.showNotification(n.title || 'Knowly', {
      body: n.body || 'Your briefing is ready',
      icon: '/icon.svg',
      data: { url: '/today' },
    });
  });
}

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(clients.openWindow((event.notification.data && event.notification.data.url) || '/today'));
});
