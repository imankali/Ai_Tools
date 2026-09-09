/* Service worker اپ‌شِل: فایل‌های UI را cache می‌کند تا صفحه روی موبایل
   نصب‌شده سریع باز شود؛ هرگز چیزی زیر /api یا /ws را cache نمی‌کند. */
const VERSION = "agent-hub-v2";
const SHELL = ["./", "index.html", "styles.css", "app.js", "manifest.webmanifest", "icon.svg", "offline.html"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(VERSION)
      .then((cache) => cache.addAll(SHELL))
      .catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.endsWith("/ws") || url.pathname.endsWith("/sw.js")) return;

  // فایل‌های استاتیک: cache-first با refresh در پس‌زمینه
  event.respondWith(
    caches.match(request).then((cached) => {
      const network = fetch(request)
        .then((response) => {
          if (response && response.ok) {
            const copy = response.clone();
            caches.open(VERSION).then((cache) => cache.put(request, copy)).catch(() => {});
          }
          return response;
        })
        .catch(() => null);
      if (cached) {
        network.catch(() => {});
        return cached;
      }
      return network.then(
        (response) =>
          response ||
          caches.match("offline.html").then((page) => page || new Response("offline", { status: 503, headers: { "Content-Type": "text/plain" } }))
      );
    })
  );
});
