const CACHE = 'smallgram-v2';

self.addEventListener('install', e => { self.skipWaiting(); });

self.addEventListener('activate', e =>
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  )
);

// 网络优先：每次打开都拉最新版，离线时降级用缓存
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  if (e.request.url.includes('/api/')) return;

  e.respondWith(
    fetch(e.request)
      .then(res => {
        if (res.ok) {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});

self.addEventListener('push', e => {
  const d = e.data?.json?.() || {};
  e.waitUntil(
    self.registration.showNotification(d.title || 'Ombre', {
      body: d.body || '',
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      data: { url: '/' },
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(
    clients.matchAll({ type: 'window' }).then(list => {
      for (const c of list) {
        if ('focus' in c) return c.focus();
      }
      if (clients.openWindow) return clients.openWindow('/');
    })
  );
});
