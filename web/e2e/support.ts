import type { Page } from "@playwright/test";

import { SIGNUP_ACCESS_COOKIE, signupAccessToken } from "../lib/signup-access";

/**
 * Make a page deterministic by cutting off every dependency on external
 * services. Same-origin assets (the app's own JS/CSS) load normally; anything
 * cross-origin (Supabase, Sentry) is aborted, and same-origin `/api/*` proxy
 * calls are stubbed with 401 so an unauthenticated session never blocks on a
 * real backend. This keeps smoke/a11y runs fast and free of network flakiness.
 */
export async function isolateFromNetwork(page: Page, baseURL: string): Promise<void> {
  const appOrigin = new URL(baseURL).origin;

  await page.route("**/*", async (route) => {
    const url = route.request().url();

    // Authenticated test fixtures represent existing members unless overridden.
    if (url.includes("/rest/v1/profiles?")) {
      await route.fulfill({ contentType: "application/json", body: '[{"access_status":"approved"}]' });
      return;
    }
    // Stub the same-origin API proxy so unauthenticated calls resolve instantly.
    if (url.startsWith(`${appOrigin}/api/`)) {
      await route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({ detail: "unauthenticated (smoke test stub)" }),
      });
      return;
    }

    // Let the app serve its own origin (pages, chunks, fonts proxied via app).
    if (url.startsWith(appOrigin) || url.startsWith("data:") || url.startsWith("blob:")) {
      await route.continue();
      return;
    }

    // Everything else (Supabase, Sentry, Google Fonts CDN, etc.) is blocked so
    // tests never reach the public internet.
    await route.abort();
  });
}

/** Routes that should render for an anonymous visitor without crashing. */
export const PUBLIC_ROUTES = ["/", "/login", "/signup", "/forgot-password", "/reset-password"] as const;

/** Signup is invite-only; tests about the signup form itself start unlocked. */
export async function grantSignupAccess(page: Page, baseURL: string): Promise<void> {
  await page.context().addCookies([{ name: SIGNUP_ACCESS_COOKIE, value: signupAccessToken(), url: new URL(baseURL).origin }]);
}
