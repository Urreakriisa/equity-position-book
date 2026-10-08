/* Service worker for price-alert notifications (ported from Tlalocai).
   It only shows pushes and opens the app when one is tapped; it caches
   nothing, so the page and its data always come fresh from the server. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

self.addEventListener('push', event => {
  let d = {};
  try { d = event.data.json(); } catch (e) {}
  event.waitUntil(self.registration.showNotification(d.title || 'Equity Position Book', {
    body: d.body || '', icon: '/static/icon-192.png', badge: '/static/icon-192.png',
    tag: d.tag || 'price-alert', data: { url: '/' }
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(ws => {
    for (const w of ws) { if ('focus' in w) return w.focus(); }
    return clients.openWindow('/');
  }));
});
