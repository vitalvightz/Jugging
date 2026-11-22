/* global self, caches, fetch, URL, Response */

const CACHE_PREFIX = "unlxck-pwa-";
const BUILD_VERSION = (new URL(self.location.href).searchParams.get("build") || "shell")
  .replace(/[^a-z0-9_-]/gi, "")
  .slice(0, 32);
const PRECACHE_NAME = `${CACHE_PREFIX}precache-${BUILD_VERSION}`;
const STATIC_CACHE_NAME = `${CACHE_PREFIX}static-${BUILD_VERSION}`;
const OFFLINE_URL = "/offline.html";
const APP_ICON_192 = "/icons/icon-192x192.png";
const PRECACHE_URLS = [
  OFFLINE_URL,
  APP_ICON_192,
  "/icons/icon-512x512.png",
  "/icons/apple-touch-icon.png",
  "/icons/favicon-32x32.png",
  "/icons/favicon-16x16.png",
  "/favicon.ico",
];
const SAFE_STATIC_PATHS = new Set(PRECACHE_URLS.slice(1));

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(PRECACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS)));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    Promise.all([
      caches.keys().then((keys) =>
        Promise.all(
          keys
            .filter(
              (key) =>
                key.startsWith(CACHE_PREFIX) &&
                key !== PRECACHE_NAME &&
                key !== STATIC_CACHE_NAME,
            )
            .map((key) => caches.delete(key)),
        ),
      ),
      self.clients.claim(),
    ]),
  );
});

async function networkFirstNavigation(request) {
  try {
    return await fetch(request);
  } catch {
    return (await caches.match(OFFLINE_URL)) ?? Response.error();
  }
}

async function staleWhileRevalidate(request, event) {
  const cache = await caches.open(STATIC_CACHE_NAME);
  // Icons are precached at install into PRECACHE_NAME, not the runtime cache.
  // Looking only in the runtime cache meant the offline page's first request
  // for an icon went to the (dead) network and rendered a broken image.
  const cached = (await cache.match(request)) ?? (await caches.match(request));
  const networkRequest = fetch(request)
    .then((response) => {
      if (response.ok && response.type === "basic") {
        return cache.put(request, response.clone()).then(() => response);
      }
      return response;
    })
    .catch(() => null);

  if (cached) {
    event.waitUntil(networkRequest);
    return cached;
  }

  return (await networkRequest) ?? Response.error();
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") {
    return;
  }

  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) {
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(networkFirstNavigation(request));
    return;
  }

  const isVersionedNextAsset = url.pathname.startsWith("/_next/static/");
  if (isVersionedNextAsset || SAFE_STATIC_PATHS.has(url.pathname)) {
    event.respondWith(staleWhileRevalidate(request, event));
  }
});

// ---------------------------------------------------------------------------
// Web push: plan-ready ("your camp is lxcked in") and morning check-in nudges.
// Payloads are JSON {title, body, url, tag} built by the backend push service.
// ---------------------------------------------------------------------------

const DEFAULT_NOTIFICATION = {
  title: "UNLXCK",
  body: "Open the app for an update.",
  url: "/",
  tag: "unlxck",
};

self.addEventListener("push", (event) => {
  let payload = DEFAULT_NOTIFICATION;
  try {
    payload = { ...DEFAULT_NOTIFICATION, ...(event.data ? event.data.json() : {}) };
  } catch {
    // Non-JSON payloads fall back to the default shell notification.
  }

  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      tag: payload.tag,
      icon: APP_ICON_192,
      badge: APP_ICON_192,
      data: { url: payload.url },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl = new URL(
    (event.notification.data && event.notification.data.url) || "/",
    self.location.origin,
  ).href;

  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clientList) => {
        for (const client of clientList) {
          if (client.url === targetUrl && "focus" in client) {
            return client.focus();
          }
        }
        // Prefer refocusing any open app window onto the target instead of
        // opening a duplicate tab/window.
        for (const client of clientList) {
          if ("navigate" in client && "focus" in client) {
            return client.navigate(targetUrl).then((navigated) => (navigated || client).focus());
          }
        }
        return self.clients.openWindow(targetUrl);
      }),
  );
});
