// Service worker CheevoScope.
//
// Важно: это дашборд живых данных (статистика Steam/RA обновляется по
// кнопке), поэтому /api/* здесь НИКОГДА не кэшируется — иначе после
// установки как приложение можно было бы годами видеть одни и те же цифры.
// Кэшируется только статическая оболочка (HTML/иконки/манифест), чтобы
// приложение открывалось мгновенно и не было пустого белого экрана при
// плохой связи.

const CACHE_NAME = "cheevoscope-shell-v1";
const SHELL_URLS = [
  "/",
  "/manifest.json",
  "/static/favicon.svg",
  "/static/icon-192.png",
  "/static/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_URLS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Живые данные — всегда только сеть, без исключений.
  if (url.pathname.startsWith("/api/")) {
    return;
  }

  // Не наш origin (шрифты Google и т.п.) — не вмешиваемся.
  if (url.origin !== self.location.origin) {
    return;
  }

  // Оболочка — stale-while-revalidate: отдаём из кэша мгновенно, в фоне
  // тихо обновляем кэш свежей версией для следующего открытия.
  event.respondWith(
    caches.open(CACHE_NAME).then((cache) =>
      cache.match(event.request).then((cached) => {
        const networkFetch = fetch(event.request)
          .then((response) => {
            if (response && response.ok) {
              cache.put(event.request, response.clone());
            }
            return response;
          })
          .catch(() => cached);
        return cached || networkFetch;
      })
    )
  );
});
