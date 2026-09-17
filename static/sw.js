// static/sw.js — Faustus PWA service worker
//
// The interface is one shell (static/index.html) plus one entry
// (/static/studio/studio.js) that pulls content-hashed chunks as screens are
// opened. That shape decides the whole strategy:
//
//   - The precache list is TWO entries, not fifty. Chunk names change with
//     every build, so a hardcoded list of them would be stale the moment it
//     shipped; they are picked up by the rules below the first time a screen
//     is opened, which is also when they start mattering offline.
//   - Navigation (any route, not just "/"): the shell from cache first, so a
//     reload on /calendar opens instantly and works offline. The router does
//     the rest client-side.
//   - JS/CSS: network-first, so a new build shows up on a normal reload
//     without a manual cache clear.
//   - Everything else under /static: cache-first with a background refresh.
//   - /api and non-GET: never cached.
//
// Bump CACHE_NAME whenever this file's logic changes.
const CACHE_NAME = 'faustus-v401-push';

// The app shell and its entry. Everything else arrives on demand.
const PRECACHE = [
  '/',
  '/static/studio/studio.js',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then(cache =>
      // addAll is atomic — if any item fails, none are cached. Individual puts
      // so a single 404 cannot block the whole install.
      Promise.all(
        PRECACHE.map(url =>
          fetch(url, { cache: 'reload' })
            .then(res => res.ok ? cache.put(url, res) : null)
            .catch(() => null)
        )
      )
    )
  );
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // Never touch API calls or non-GET.
  if (url.pathname.startsWith('/api/') || e.request.method !== 'GET') return;

  // Navigation: the shell, stale-while-revalidate. Every app route is served
  // the same HTML, so one cached copy answers all of them — a reload deep in
  // the app opens from cache and the router takes it from there. A request for
  // a real file under /static (the login page, say) falls through to the rules
  // below, which is what stops the shell replacing the page actually asked for.
  if (e.request.mode === 'navigate' && !url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.open(CACHE_NAME).then(async cache => {
        const cached = await cache.match('/');
        const network = fetch(e.request).then(res => {
          if (res && res.ok) cache.put('/', res.clone());
          return res;
        }).catch(() => cached);
        return cached || network;
      })
    );
    return;
  }

  // JS/CSS: network-first — a new build shows up on a normal reload; the cache
  // answers only when the network does not.
  if (url.pathname.startsWith('/static/') && /\.(js|css)(\?|$)/.test(url.pathname + url.search)) {
    e.respondWith(
      fetch(e.request).then(res => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(e.request, copy));
        }
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  // Other static assets (images, fonts, the vendored parsers): cache-first
  // with a background refresh.
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.open(CACHE_NAME).then(async cache => {
        const cached = await cache.match(e.request);
        const fetching = fetch(e.request).then(res => {
          if (res && res.ok) cache.put(e.request, res.clone());
          return res;
        }).catch(() => cached);
        return cached || fetching;
      })
    );
    return;
  }
});

// ─────────────────────────────────────────────────────────────────────────
// Web Push (src/push.py, /api/push/*, docs/api/mobile.md).
//
// The payload the server encrypts and sends is always
// `{title, body, url, kind, id}` (see `src/push.py::event_to_push_payload`).
// A notification that fails to parse still shows something rather than
// silently dropping — a push the OS woke the worker for and then showed
// nothing for reads, to the person, as "notifications are broken".
// ─────────────────────────────────────────────────────────────────────────

self.addEventListener('push', (e) => {
  let data = { title: 'Faustus', body: '', url: '/', kind: '', id: null };
  try {
    if (e.data) data = Object.assign(data, e.data.json());
  } catch (err) {
    try { data.body = e.data ? e.data.text() : ''; } catch (err2) { /* nothing usable */ }
  }

  const tag = data.kind ? `${data.kind}:${data.id ?? ''}` : undefined;

  e.waitUntil(
    self.registration.showNotification(data.title || 'Faustus', {
      body: data.body || '',
      icon: '/static/pwa/icon-192.png',
      badge: '/static/pwa/icon-192.png',
      tag,
      // Re-showing the same kind+id (e.g. a resolved approval updating its
      // own pending notification) should replace it quietly, not buzz again.
      renotify: false,
      data: { url: data.url || '/' },
    })
  );
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';

  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      const target = new URL(url, self.registration.scope).href;
      for (const client of list) {
        // Any open Faustus tab/window is reused and navigated, rather than
        // piling up a new one every time a notification is tapped.
        if (client.url && new URL(client.url).origin === new URL(target).origin) {
          return client.navigate(target).then((c) => c && c.focus());
        }
      }
      return clients.openWindow(target);
    })
  );
});

// A push service can rotate a subscription's keys out from under the
// browser (key expiry, a service-side rotation) without any code here ever
// running `subscribe()` again — this event is the only signal that
// happened. Re-subscribing and re-registering with the server keeps
// notifications flowing instead of silently going dead.
self.addEventListener('pushsubscriptionchange', (e) => {
  e.waitUntil(
    (async () => {
      try {
        const keyRes = await fetch('/api/push/vapid-key', { credentials: 'include' });
        if (!keyRes.ok) return;
        const { key } = await keyRes.json();
        const applicationServerKey = urlBase64ToUint8Array(key);
        const subscription = await self.registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey,
        });
        await fetch('/api/push/subscribe', {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ subscription: subscription.toJSON() }),
        });
      } catch (err) {
        // Best-effort: the next foreground visit's adapter call
        // (studio/src/adapters/push.ts) will notice it is unsubscribed and
        // let the person re-enable it from Settings.
      }
    })()
  );
});

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; ++i) output[i] = raw.charCodeAt(i);
  return output;
}
