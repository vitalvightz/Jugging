import { expect, test } from "@playwright/test";

import { isolateFromNetwork, PUBLIC_ROUTES } from "./support";

const BASE_URL = "http://127.0.0.1:3100";

test.describe("public routes load without crashing", () => {
  for (const route of PUBLIC_ROUTES) {
    test(`GET ${route} renders the app shell`, async ({ page, baseURL }) => {
      await isolateFromNetwork(page, baseURL ?? BASE_URL);

      const uncaughtErrors: string[] = [];
      page.on("pageerror", (error) => uncaughtErrors.push(error.message));

      const response = await page.goto(route, { waitUntil: "domcontentloaded" });

      // No server error page on primary routes.
      expect(response, `expected a response for ${route}`).not.toBeNull();
      expect(response!.status(), `unexpected status for ${route}`).toBeLessThan(500);

      // Document has a non-empty title (basic a11y / SEO sanity).
      await expect(page).toHaveTitle(/.+/);

      // App shell renders its main landmark from the root layout.
      await expect(page.locator("main")).toBeVisible();

      // No console-breaking uncaught exceptions during initial render.
      expect(uncaughtErrors, `uncaught errors on ${route}: ${uncaughtErrors.join("; ")}`).toEqual([]);
    });
  }
});

test("public entry stays on the brand shell while the anonymous session resolves", async ({ page, baseURL }) => {
  await isolateFromNetwork(page, baseURL ?? BASE_URL);
  await page.goto("/", { waitUntil: "domcontentloaded" });

  // The server commits the public shell before hydration, so workspace chrome
  // cannot flash while the client checks whether a session exists.
  await expect(page.locator("html")).toHaveAttribute("data-app-surface", "brand");
  await expect(page.locator("#app-sidebar")).toBeHidden();
  await expect(page.getByRole("button", { name: "Open navigation" })).toBeHidden();
  await expect(page.getByText("Loading your athlete workspace")).toBeHidden();

  // Once the anonymous session resolves, the public account navigation appears
  // and the page remains on the brand surface.
  await expect(page.getByLabel("UNLXCK entry navigation")).toBeVisible();
  await expect(page.getByLabel("Account access").getByRole("link", { name: /log in/i })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("data-app-surface", "brand");

  // Log in lives only in the top bar, and the language selector sits beside it
  // instead of floating over the page.
  await expect(page.getByRole("link", { name: /log in/i })).toHaveCount(1);
  await expect(page.getByLabel("Account access").locator(".unlxck-language-switcher-trigger")).toBeVisible();
  await expect(page.locator(".unlxck-language-switcher-trigger")).toHaveCount(1);
});

test("protected route redirects unauthenticated users to login", async ({ page, baseURL }) => {
  await isolateFromNetwork(page, baseURL ?? BASE_URL);

  await page.goto("/generate", { waitUntil: "domcontentloaded" });

  // RequireAuth performs a client-side replace to /login when no session exists.
  await page.waitForURL("**/login", { timeout: 15_000 });
  expect(page.url()).toContain("/login");
});

test("emailed plan links keep their destination through login", async ({ page, baseURL }) => {
  await isolateFromNetwork(page, baseURL ?? BASE_URL);
  const planId = "11111111-2222-4333-8444-555555555555";
  await page.goto(`/plans/${planId}`, { waitUntil: "domcontentloaded" });
  await page.waitForURL((url) =>
    url.pathname === "/login" && url.searchParams.get("next") === `/plans/${planId}`,
    { timeout: 15_000 },
  );
});
