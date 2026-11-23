import { expect, test } from "@playwright/test";

import { isolateFromNetwork } from "./support";

const BASE_URL = "http://127.0.0.1:3100";
const PRECACHED_STATIC_PATHS = new Set([
  "/offline.html",
  "/brand/unlxck-one-angle-48.png",
  "/icons/icon-192x192.png",
  "/icons/icon-512x512.png",
  "/icons/apple-touch-icon.png",
  "/icons/favicon-32x32.png",
  "/icons/favicon-16x16.png",
  "/favicon.ico",
]);

test("manifest, service worker, icon, and offline assets are production-ready", async ({ request }) => {
  const manifestResponse = await request.get("/manifest.webmanifest");
  expect(manifestResponse.status()).toBe(200);
  expect(manifestResponse.headers()["content-type"]).toContain("application/manifest+json");
  const manifest = await manifestResponse.json();
  expect(manifest).toMatchObject({
    id: "/",
    name: "UNLXCK",
    short_name: "UNLXCK",
    start_url: "/dashboard?source=pwa",
    scope: "/",
    display: "standalone",
  });

  const workerResponse = await request.get("/sw.js?build=headers-check");
  expect(workerResponse.status()).toBe(200);
  expect(workerResponse.headers()["content-type"]).toContain("application/javascript");
  expect(workerResponse.headers()["cache-control"]).toContain("no-cache");
  expect(workerResponse.headers()["cache-control"]).toContain("no-store");
  expect(workerResponse.headers()["cache-control"]).toContain("must-revalidate");
  expect(workerResponse.headers()["service-worker-allowed"]).toBe("/");
  expect(workerResponse.headers()["content-security-policy"]).toBe(
    "default-src 'self'; base-uri 'none'; connect-src 'self'; object-src 'none'; script-src 'self'",
  );

  const rootResponse = await request.get("/");
  const rootCsp = rootResponse.headers()["content-security-policy"] ?? "";
  expect(rootCsp).toMatch(/script-src 'self' 'nonce-[^']+' 'strict-dynamic'/);
  expect(rootCsp).toContain("worker-src 'self' blob:");
  expect(rootCsp).toContain("manifest-src 'self'");
  const connections = rootCsp.match(/(?:^|;)\s*connect-src\s+([^;]+)/)?.[1].split(/\s+/) ?? [];
  expect(connections).toContain("https://fonts.googleapis.com");
  expect(connections).not.toContain("*");

  for (const path of [
    "/icons/icon-192x192.png",
    "/icons/icon-512x512.png",
    "/icons/icon-maskable-512x512.png",
    "/icons/apple-touch-icon.png",
    "/offline.html",
  ]) {
    const response = await request.get(path);
    expect(response.status(), path).toBe(200);
  }
});

test("root-scoped worker caches only the offline shell and static assets", async ({
  page,
  baseURL,
}) => {
  await isolateFromNetwork(page, baseURL ?? BASE_URL);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  const registration = await page.evaluate(async () => {
    const ready = await navigator.serviceWorker.ready;
    await fetch("/api/health").catch(() => null);
    return {
      scope: ready.scope,
      scriptURL: ready.active?.scriptURL ?? ready.waiting?.scriptURL ?? "",
    };
  });

  expect(registration.scope).toBe(`${new URL(baseURL ?? BASE_URL).origin}/`);
  expect(registration.scriptURL).toContain("/sw.js?build=");

  const cachedUrls = await page.evaluate(async () => {
    const names = await caches.keys();
    const urls = await Promise.all(
      names.map(async (name) => (await (await caches.open(name)).keys()).map((request) => request.url)),
    );
    return urls.flat();
  });

  expect(cachedUrls.length).toBeGreaterThan(0);
  for (const value of cachedUrls) {
    const url = new URL(value);
    expect(
      PRECACHED_STATIC_PATHS.has(url.pathname) || url.pathname.startsWith("/_next/static/"),
      value,
    ).toBe(true);
  }
  for (const forbidden of [
    "/api/",
    "/plans",
    "/profiles",
    "generation_jobs",
    "/check-ins",
    "/nutrition",
    "/admin",
  ]) {
    expect(cachedUrls.some((value) => value.includes(forbidden)), forbidden).toBe(false);
  }
});

test("registered worker serves the honest offline fallback and recovers", async ({ page, context, baseURL }) => {
  await isolateFromNetwork(page, baseURL ?? BASE_URL);
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
    if (!navigator.serviceWorker.controller) {
      await new Promise<void>((resolve) =>
        navigator.serviceWorker.addEventListener("controllerchange", () => resolve(), { once: true }),
      );
    }
  });

  let apiHealthy = false;
  await page.route("**/api/health", (route) => route.fulfill({
    status: apiHealthy ? 200 : 503,
    contentType: "application/json",
    body: JSON.stringify({ ok: apiHealthy, app: "unlxck-fight-camp-api" }),
  }));
  const destination = "/signup?offline-test=1#original-destination";
  await context.setOffline(true);
  await page.goto(destination, { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "UNLXCK your connection." })).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry connection" })).toBeVisible();
  await expect(page.locator("img")).toHaveJSProperty("naturalWidth", 48);

  await context.setOffline(false);
  await expect(page.getByRole("status")).toContainText("Still can’t reach UNLXCK");
  expect(new URL(page.url()).pathname + new URL(page.url()).search + new URL(page.url()).hash).toBe(destination);
  await page.getByRole("button", { name: "Retry connection" }).click();
  await expect(page.getByRole("status")).toContainText("Still can’t reach UNLXCK");
  apiHealthy = true;
  await page.getByRole("button", { name: "Retry connection" }).click();
  await expect(page.getByRole("heading", { name: "UNLXCK your connection." })).toHaveCount(0);
  expect(new URL(page.url()).pathname + new URL(page.url()).search + new URL(page.url()).hash).toBe(destination);
});


test("connection card waits for API confirmation, ignores unrelated success, and recovers once", async ({ page, context, baseURL }) => {
  await isolateFromNetwork(page, baseURL ?? BASE_URL);
  let healthy = false;
  let checks = 0;
  await page.route("**/api/health", (route) => {
    checks += 1;
    return route.fulfill({
      status: healthy ? 200 : 503,
      contentType: "application/json",
      body: JSON.stringify({ ok: healthy, app: "unlxck-fight-camp-api" }),
    });
  });
  await page.route("**/api/unrelated", (route) => route.fulfill({ json: { ok: true } }));
  await page.goto("/login", { waitUntil: "networkidle" });
  await page.evaluate(() => {
    (window as unknown as { recoveries: number }).recoveries = 0;
    window.addEventListener("unlxck:reconnected", () => { (window as unknown as { recoveries: number }).recoveries += 1; });
  });
  await context.setOffline(true);
  await expect(page.getByRole("status")).toContainText("You’re offline.");
  await context.setOffline(false);
  await expect(page.getByRole("status")).toContainText("Can’t reach UNLXCK.");
  await expect.poll(() => checks).toBeGreaterThan(0);
  await page.evaluate(async () => { await fetch("/api/unrelated"); });
  await expect(page.getByRole("status")).not.toContainText("Back online");
  expect(await page.evaluate(() => (window as unknown as { recoveries: number }).recoveries)).toBe(0);
  healthy = true;
  await page.getByRole("button", { name: "Retry connection" }).click();
  await expect(page.getByRole("status")).toContainText("Back online.");
  expect(await page.evaluate(() => (window as unknown as { recoveries: number }).recoveries)).toBe(1);
});


test("API recovery cannot repeatedly reload a destination that is still unavailable", async ({ page, context, baseURL }) => {
  await isolateFromNetwork(page, baseURL ?? BASE_URL);
  await page.goto("/login", { waitUntil: "networkidle" });
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
    if (!navigator.serviceWorker.controller) {
      await new Promise<void>(resolve => navigator.serviceWorker.addEventListener("controllerchange", () => resolve(), { once: true }));
    }
  });
  let checks = 0;
  await page.route("**/api/health", route => {
    checks += 1;
    return route.fulfill({ json: { ok: true, app: "unlxck-fight-camp-api" } });
  });
  await context.route("**/signup?reload-test=1", route => route.abort());
  await context.setOffline(true);
  await page.goto("/signup?reload-test=1", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "UNLXCK your connection." })).toBeVisible();
  const reloaded = page.waitForEvent("framenavigated", frame => frame === page.mainFrame());
  await context.setOffline(false);
  await reloaded;
  await expect(page.getByRole("button", { name: "Retry connection" })).toBeEnabled();
  expect(checks).toBe(1);
  await page.evaluate(() => {
    window.dispatchEvent(new Event("online"));
    window.dispatchEvent(new Event("online"));
  });
  expect(checks).toBe(1);
  const manualReload = page.waitForEvent("framenavigated", frame => frame === page.mainFrame());
  await page.getByRole("button", { name: "Retry connection" }).click();
  await manualReload;
  await expect(page.getByRole("button", { name: "Retry connection" })).toBeEnabled();
  expect(checks).toBe(2);
});

for (const width of [390, 1280]) {
  test(`connection card leaves loaded page controls reachable (${width}px)`, async ({ page, context, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    await isolateFromNetwork(page, baseURL ?? BASE_URL);
    await page.route("**/api/health", route => route.fulfill({ status: 503, json: { ok: false } }));
    await page.goto("/login", { waitUntil: "networkidle" });
    await context.setOffline(true);
    await expect(page.getByRole("status")).toContainText("You’re offline.");
    await context.setOffline(false);
    await expect(page.getByRole("status")).toContainText("Can’t reach UNLXCK.");
    await page.getByRole("button", { name: "Continue with email", exact: true }).click();
    await expect(page.getByRole("textbox", { name: /email/i })).toBeVisible();
    await expect(page.getByRole("status")).toContainText("Can’t reach UNLXCK.");
  });
}
