/* HenZion service worker. Rendered by Django so the cache name changes whenever the front-end files change. */
const CACHE = 'henzion-{{ cache_version }}';
const OFFLINE_URL = '{{ offline_url }}';
const PRECACHE = [OFFLINE_URL, {% for u in precache %}'{{ u }}'{% if not forloop.last %}, {% endif %}{% endfor %}];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith('henzion-') && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;           // sales, payments, approvals: always straight to the server
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Pages: always live data. Only if the network is down, show the offline page.
  // (Pages are never stored: they contain sales and customer data and may be used on a shared computer.)
  if (req.mode === 'navigate') {
    event.respondWith(fetch(req).catch(() => caches.match(OFFLINE_URL)));
    return;
  }

  // Static files (css, js, images): serve the saved copy instantly and refresh it in the background.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.open(CACHE).then((cache) =>
        cache.match(req).then((hit) => {
          const fresh = fetch(req).then((res) => {
            if (res && res.ok) cache.put(req, res.clone());
            return res;
          }).catch(() => hit);
          return hit || fresh;
        })
      )
    );
  }
  // Everything else (JSON status checks, etc.) is not intercepted.
});

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { body: event.data && event.data.text() }; }
  const title = data.title || 'HenZion';
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || '',
    icon: '{{ icon }}',
    badge: '{{ badge }}',
    tag: data.tag || 'henzion',
    renotify: true,
    requireInteraction: true,
    data: { url: data.url || '/' },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = new URL((event.notification.data && event.notification.data.url) || '/', self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        if ('focus' in c && 'navigate' in c) return c.focus().then(() => c.navigate(target));
      }
      return self.clients.openWindow(target);
    })
  );
});
