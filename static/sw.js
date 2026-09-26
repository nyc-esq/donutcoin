// Donut Coin service worker: push notifications, and a small cache so the shell opens instantly.
const VERSION = 'donut-2026091703';
const SHELL = ['/wallet', '/market', '/static/core.js?v=202609162249', '/static/vendor/secp256k1.js', '/static/donut.jpeg', '/static/manifest.webmanifest', '/static/site.css?v=2026091702'];
self.addEventListener('install', e => { e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== VERSION).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.pathname.startsWith('/api/')) return;            // the chain is never cached
  e.respondWith(fetch(e.request).then(r => { const copy = r.clone(); caches.open(VERSION).then(c => c.put(e.request, copy)); return r; }).catch(() => caches.match(e.request)));
});
self.addEventListener('push', e => {
  let d = {}; try { d = e.data.json(); } catch { d = { title: 'Donut Coin', body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || 'Donut Coin', { body: d.body || '', icon: '/static/icons/icon-192.png', badge: '/static/icons/icon-192.png', data: { url: d.url || '/wallet' } }));
});
self.addEventListener('notificationclick', e => {
  e.notification.close(); const url = (e.notification.data && e.notification.data.url) || '/wallet';
  e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(ws => { for (const w of ws) { if ('focus' in w) { w.navigate(url); return w.focus(); } } return clients.openWindow(url); }));
});
