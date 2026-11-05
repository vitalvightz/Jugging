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

// Seeds "admitted here before" once per tab, as an earlier visit would have.
const rememberAdmitted = (athleteId: string) =>
  `if (!sessionStorage.getItem("seeded")) { sessionStorage.setItem("seeded", "1"); localStorage.setItem("unlxck-admitted:${athleteId}", "1"); }`;

test("an athlete admitted here before starts Home's reads during admission, still gated on it", async ({ page, baseURL }) => {
  // Today outlasts admission, as in production (≈1–2 s vs ≈0.3 s).
  const api = await mockAthlete(page, baseURL!, { latencyFor: (_method, path) => (path === "/api/today" ? 1_500 : 0) });
  await page.addInitScript(rememberAdmitted(PRIMARY.athleteId));
  let checks = 0;
  let admit!: () => void;
  const admission = new Promise<void>(resolve => { admit = resolve; });
  await page.route("**/rest/v1/profiles?**", async route => {
    checks++;
    await admission;
    await route.fulfill({ json: [{ access_status: "approved" }] });
  });
  const started: string[] = [];
  page.on("request", request => { if (request.url().includes("/api/")) started.push(new URL(request.url()).pathname); });
  await page.goto("/");
  // Profile and Today are already on their way while admission is pending…
  await expect.poll(() => started).toEqual(expect.arrayContaining(["/api/me", "/api/today"]));
  // …but nothing of the workspace is shown until admission passes.
  await page.waitForTimeout(300);
  await expect(screens.homeReady(page)).toHaveCount(0);
  admit();
  await expect(screens.homeReady(page)).toBeVisible();
  await api.settle();
  expect(checks).toBe(1);
  // Home's own reads joined the early ones: each was made once.
  expect(api.calls.filter(call => call.path === "/api/me")).toHaveLength(1);
  expect(api.calls.filter(call => call.path === "/api/today")).toHaveLength(1);
});

test("an athlete no longer admitted never sees Home, even after reads started early", async ({ page, baseURL }) => {
  await mockAthlete(page, baseURL!);
  await page.addInitScript(rememberAdmitted(PRIMARY.athleteId));
  await page.route("**/rest/v1/profiles?**", route => route.fulfill({ json: [{ access_status: "pending" }] }));
  await page.goto("/");
  await expect(page).toHaveURL(/\/signup\?invite=required/);
  await expect(screens.homeReady(page)).toHaveCount(0);
  // The hint is dropped, so the next visit checks admission before any read.
  expect(await page.evaluate(id => localStorage.getItem(`unlxck-admitted:${id}`), PRIMARY.athleteId)).toBeNull();
});

test("a first visit checks admission before reading the workspace", async ({ page, baseURL }) => {
  const api = await mockAthlete(page, baseURL!);
  let admit!: () => void;
  const admission = new Promise<void>(resolve => { admit = resolve; });
  await page.route("**/rest/v1/profiles?**", async route => {
    await admission;
    await route.fulfill({ json: [{ access_status: "approved" }] });
  });
  await page.goto("/");
  await page.waitForTimeout(500);
  expect(api.calls.map(call => call.path)).not.toContain("/api/me");
  admit();
  await expect(screens.homeReady(page)).toBeVisible();
  expect(await page.evaluate(id => localStorage.getItem(`unlxck-admitted:${id}`), PRIMARY.athleteId)).toBe("1");
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
