self.addEventListener('install',  () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));

const CACHE_NAME    = 'spoofloc-tiles-v1';
const META_URL      = 'https://spoofloc-meta/v1'; // sentinel key inside the same cache
const MAX_BYTES     = 150 * 1024 * 1024;
const EVICT_TARGET  = MAX_BYTES * 0.85;
const CACHEABLE_HOST = 'tiles.openfreemap.org';

// Serialize all metadata reads/writes through a promise chain to avoid races.
let _metaQueue = Promise.resolve();

async function getMeta(cache) {
  const r = await cache.match(META_URL);
  if (!r) return { total: 0, entries: {} };
  try { return await r.json(); } catch { return { total: 0, entries: {} }; }
}

function setMeta(cache, meta) {
  return cache.put(META_URL, new Response(JSON.stringify(meta), {
    headers: { 'Content-Type': 'application/json' },
  }));
}

function enqueueMeta(fn) {
  _metaQueue = _metaQueue.then(fn).catch(() => {});
  return _metaQueue;
}

async function afterCachePut(url, size) {
  enqueueMeta(async () => {
    const cache = await caches.open(CACHE_NAME);
    const meta  = await getMeta(cache);

    const prev = meta.entries[url];
    meta.total += size - (prev?.size ?? 0);
    meta.entries[url] = { size, ts: Date.now() };

    if (meta.total > MAX_BYTES) {
      const sorted = Object.entries(meta.entries).sort((a, b) => a[1].ts - b[1].ts);
      for (const [eUrl, e] of sorted) {
        if (meta.total <= EVICT_TARGET) break;
        if (eUrl === url) continue;
        await cache.delete(eUrl);
        meta.total -= e.size;
        delete meta.entries[eUrl];
      }
    }

    await setMeta(cache, meta);
  });
}

// ── Fetch handler ─────────────────────────────────────────────────────────────

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET')     return;
  if (req.headers.get('range')) return;
  if (!new URL(req.url).hostname.endsWith(CACHEABLE_HOST)) return;

  event.respondWith(
    caches.open(CACHE_NAME).then(async cache => {
      const cached = await cache.match(req, { ignoreVary: true });
      if (cached) return cached;

      const response = await fetch(req);
      if (response.ok && response.status === 200) {
        cache.put(req, response.clone()).then(() => {
          const size = parseInt(response.headers.get('content-length') || '0') || 40000;
          afterCachePut(req.url, size);
        }).catch(() => {});
      }
      return response;
    }).catch(() => fetch(req))
  );
});

// ── Message handler ───────────────────────────────────────────────────────────

self.addEventListener('message', event => {
  const { type } = event.data || {};

  if (type === 'GET_SIZE') {
    caches.open(CACHE_NAME)
      .then(cache => getMeta(cache))
      .then(meta  => event.source.postMessage({ type: 'CACHE_SIZE', bytes: meta.total }))
      .catch(()   => event.source.postMessage({ type: 'CACHE_SIZE', bytes: 0 }));
  }

  if (type === 'PURGE') {
    caches.delete(CACHE_NAME).then(() => {
      _metaQueue = Promise.resolve();
      event.source.postMessage({ type: 'CACHE_SIZE', bytes: 0 });
    }).catch(() => {});
  }
});
