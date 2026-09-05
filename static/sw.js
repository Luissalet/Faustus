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
const CACHE_NAME = 'faustus-v400-studio';

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
