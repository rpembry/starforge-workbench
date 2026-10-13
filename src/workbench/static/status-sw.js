/* Status shell only. Sensitive API responses are never intercepted or cached. */
const CACHE_PREFIX = 'workbench-status-shell-';
const CACHE_NAME = `${CACHE_PREFIX}v5`;
const SHELL = '/status/';
const STATIC = new Set([SHELL, '/status/manifest.webmanifest', '/assets/status.js',
  '/status/icon-192.png', '/status/icon-512.png']);

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll([...STATIC])));
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin || !STATIC.has(url.pathname) || url.search) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    const hit = await cache.match(request);
    if (hit) return hit;
    const response = await fetch(request);
    if (response.ok && response.type === 'basic') await cache.put(request, response.clone());
    return response;
  })());
});

self.addEventListener('message', event => {
  if (event.data !== 'ACTIVATE_UPDATE') return;
  try {
    const source = new URL(event.source.url);
    if (source.origin === self.location.origin && source.pathname.startsWith('/status/')) self.skipWaiting();
  } catch (_) { /* Unrecognized client cannot activate this worker. */ }
});
