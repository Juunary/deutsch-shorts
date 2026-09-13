// App-shell service worker. Caches ONLY the shell (network-first, cache as offline fallback);
// /api/* and cross-origin (YouTube) are never touched.
const VERSION = 'v3';
const CACHE = `deutsch-shorts-${VERSION}`;
const SHELL = [
  '/',
  '/static/styles.css',
  '/static/js/main.js', '/static/js/api.js', '/static/js/state.js', '/static/js/player.js',
  '/static/js/subtitles.js', '/static/js/gloss.js', '/static/js/modes.js', '/static/js/feed.js',
  '/static/js/vocab.js', '/static/js/settings.js', '/static/js/onboarding.js', '/static/js/i18n.ko.js',
  '/static/js/util.js',
  '/manifest.webmanifest', '/static/icons/icon.svg', '/static/icons/icon-maskable.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;          // YouTube etc. pass through untouched
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/spikes/')) return;
  event.respondWith(
    fetch(req).then((res) => {                              // online: always the fresh file, refresh the cache
      if (res.ok && SHELL.includes(url.pathname)) {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
      }
      return res;
    }).catch(() => caches.match(req))                       // offline: the cached shell
  );
});
