import { expect, test } from "@playwright/test";
import { mockAthlete, navigate, PRIMARY, SECONDARY, screens, sessionFor, STORAGE_KEY, todayFor } from "./request-journeys";

test("Home checks admission once and stays gated until it succeeds", async ({ page, baseURL }) => {
  await mockAthlete(page, baseURL!);
  let checks = 0;
  let admit!: () => void;
  const admission = new Promise<void>(resolve => { admit = resolve; });
  await page.route("**/rest/v1/profiles?**", async route => {
    checks++;
    await admission;
    await route.fulfill({ json: [{ access_status: "approved" }] });
  });
  await page.goto("/");
  await expect.poll(() => checks).toBeGreaterThan(0);
  await expect(screens.homeReady(page)).toHaveCount(0);
  admit();
  await expect(screens.homeReady(page)).toBeVisible();
  expect(checks).toBe(1);
});

test("Home reads the plan calendar only for a dated camp's countdown", async ({ page, baseURL }) => {
  const api = await mockAthlete(page, baseURL!);
  const fightDate = new Date(Date.parse(`${todayFor(PRIMARY).today.training_day}T00:00:00Z`) + 30 * 86_400_000).toISOString().slice(0, 10);
  await page.route("**/api/today", route => route.fulfill({ json: { ...todayFor(PRIMARY), active_plan: { ...todayFor(PRIMARY).active_plan, fight_date: fightDate } } }));
  await page.goto("/");
  await expect(page.locator(".fight-countdown:not(.fight-countdown-skeleton)")).toBeVisible();
  await expect.poll(() => api.calls.map(call => call.path)).toContain(`/api/plans/${PRIMARY.planId}`);
  await expect(page.locator('.fight-countdown[aria-busy="true"]')).toHaveCount(0);
});

test("a new account gets its own admission and profile after Home has loaded", async ({ page, baseURL }) => {
  const api = await mockAthlete(page, baseURL!);
  let checks = 0;
  await page.route("**/rest/v1/profiles?**", async route => {
    checks++;
    await route.fulfill({ json: [{ access_status: "approved" }] });
  });
  await page.goto("/");
  await expect(screens.homeReady(page)).toBeVisible();
  api.switchTo(SECONDARY);
  // Supabase delivers cross-tab sign-ins on its auth storage channel.
  await page.evaluate(([key, session]) => {
    const channel = new BroadcastChannel(key);
    channel.postMessage({ event: "SIGNED_IN", session: JSON.parse(session) });
    channel.close();
  }, [STORAGE_KEY, sessionFor(SECONDARY)]);
  await expect(page.locator("main")).toContainText(`${SECONDARY.name} camp`);
  expect(checks).toBe(2);
  expect(api.calls.filter(call => call.path === "/api/me")).toHaveLength(2);
  await expect(page.locator(`main a[href="/plans/${PRIMARY.planId}"]`)).toHaveCount(0);
});

test("Home can open the deferred timer, run it and restore it after navigation", async ({ page, baseURL }) => {
  await mockAthlete(page, baseURL!);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(screens.homeReady(page)).toBeVisible();
  await page.locator('nav a[href="/timer"]:visible').first().click();
  await expect(page.getByRole("button", { name: "Start round 1", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Start round 1", exact: true }).click();
  await page.getByRole("button", { name: "Minimise timer", exact: true }).click();
  await navigate(page, "Home");
  await expect(screens.homeReady(page)).toBeVisible();
  await expect(page.locator(".st-mini")).toBeVisible();
  await page.reload();
  await expect(page.locator(".st-mini")).toBeVisible();
  await page.locator(".st-mini").click();
  await expect(page.locator('.st-root[role="dialog"]')).toBeVisible();
});

// Opt-in, fixture-only measurement. No production account or writes are used.
const benchOut = process.env.HOME_BENCH_OUT;
test("measure Home startup and History returns", async ({ browser, baseURL }) => {
  test.skip(!benchOut, "Set HOME_BENCH_OUT to record a Home benchmark");
  const runs = Number(process.env.HOME_BENCH_RUNS ?? 3);
  test.setTimeout(runs * 60_000);
  const results = [];
  for (let run = 0; run < runs; run++) {
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, reducedMotion: "reduce",
    });
    const page = await context.newPage();
    const cdp = await context.newCDPSession(page);
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
    const api = await mockAthlete(page, baseURL!, { latencyMs: 300 });
    let admissionRequests = 0;
    page.on("request", request => {
      if (new URL(request.url()).pathname === "/rest/v1/profiles") admissionRequests++;
    });
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    const started = Date.now();
    await page.goto("/");
    await expect(screens.homeReady(page)).toBeVisible();
    const readyMs = Date.now() - started;
    await api.settle(1500);
    const returns = [];
    for (let visit = 0; visit < 2; visit++) {
      await navigate(page, "History");
      await expect(screens.history(page)).toBeVisible();
      await api.settle();
      const returning = Date.now();
      await navigate(page, "Home");
      await expect(screens.homeReady(page)).toBeVisible();
      returns.push(Date.now() - returning);
      await api.settle();
    }
    results.push({ readyMs, returns, admissionRequests, errors });
    await context.close();
  }
  const { writeFileSync } = await import("node:fs");
  writeFileSync(benchOut!, JSON.stringify({ source: "mocked API, 4x CPU, 300ms API delay, local bandwidth", results }, null, 2));
});
