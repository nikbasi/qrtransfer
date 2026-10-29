// Offline support: serve from cache, refresh the cache in the background.
// Bump VERSION when deploying so phones pick up the new files.
const VERSION = "qrscan-v1";
const FILES = [
  "./", "index.html", "style.css", "icon.svg", "manifest.webmanifest",
  "js/app.js", "js/format.js", "js/worker.js",
  "vendor/fzstd.mjs", "vendor/zxing/es/reader/index.js", "vendor/zxing/es/share.js",
  "vendor/zxing/reader/zxing_reader.wasm",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET" || new URL(e.request.url).origin !== location.origin) return;
  e.respondWith(
    caches.open(VERSION).then(async (cache) => {
      const cached = await cache.match(e.request, { ignoreSearch: true });
      const fresh = fetch(e.request)
        .then((res) => {
          if (res.ok) cache.put(e.request, res.clone());
          return res;
        })
        .catch(() => cached);
      return cached || fresh;
    }),
  );
});
