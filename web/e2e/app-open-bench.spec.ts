import { expect, test } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { mockAthlete, PRIMARY, screens } from "./request-journeys";

/**
 * Opt-in app-open benchmark on a phone profile. Not a production trace.
 *
 *   APP_OPEN_BENCH_OUT=out.json APP_OPEN_BENCH_SERVER='{"me":161,"today":1483,...}' \
 *     npx playwright test e2e/app-open-bench.spec.ts
 *
 * Browser side is real: the production build, 390×844 touch viewport, 4× CPU
 * throttling, first JavaScript, Supabase auth from storage, the admission
 * request (always before API reads), React rendering. Server time per endpoint comes from
 * APP_OPEN_BENCH_SERVER (milliseconds, measured with scripts/replay_app_open.py
 * on the real API with a production-sized plan) plus APP_OPEN_BENCH_RTT_MS of
 * phone ↔ API round trip. Admission takes APP_OPEN_BENCH_ADMISSION_MS
 * (Supabase origin time from edge logs plus phone round trip).
 */
const out = process.env.APP_OPEN_BENCH_OUT;
const server: Record<string, number> = JSON.parse(process.env.APP_OPEN_BENCH_SERVER ?? "{}");
const rttMs = Number(process.env.APP_OPEN_BENCH_RTT_MS ?? 120);
const admissionMs = Number(process.env.APP_OPEN_BENCH_ADMISSION_MS ?? 250);
const runs = Number(process.env.APP_OPEN_BENCH_RUNS ?? 5);

function serverMs(method: string, path: string): number {
  if (path === "/api/me") return server.me ?? 0;
  if (path === "/api/today") return server.today ?? 0;
  if (path === "/api/xp/progress") return server.xp_progress ?? 0;
  if (path === "/api/xp/activity") return server.xp_activity ?? 0;
  if (/^\/api\/plans\/[0-9a-f-]{36}$/.test(path)) return server.plan ?? 0;
  return server.other ?? 40;
}

test("measure app open on a throttled phone", async ({ browser, baseURL }) => {
  test.skip(!out, "Set APP_OPEN_BENCH_OUT to record an app-open benchmark");
  test.setTimeout(runs * 60_000);
  const results: Array<{ marks: Record<string, number>; timeline: Array<{ what: string; start: number; end?: number }> }> = [];
  for (let run = 0; run < runs; run++) {
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, reducedMotion: "reduce",
    });
    const page = await context.newPage();
    const cdp = await context.newCDPSession(page);
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
    const api = await mockAthlete(page, baseURL!, { latencyFor: (method, path) => serverMs(method, path) + rttMs });
    const t0 = Date.now();
    const timeline: Array<{ what: string; start: number; end?: number }> = [];
    await page.route("**/rest/v1/profiles?**", async route => {
      const entry = { what: "admission", start: Date.now() - t0 } as { what: string; start: number; end?: number };
      timeline.push(entry);
      await new Promise(resolve => setTimeout(resolve, admissionMs));
      await route.fulfill({ json: [{ access_status: "approved" }] });
      entry.end = Date.now() - t0;
    });
    page.on("request", request => {
      const url = new URL(request.url());
      if (url.pathname.startsWith("/api/")) timeline.push({ what: `${request.method()} ${url.pathname}`, start: Date.now() - t0 });
    });
    page.on("requestfinished", request => {
      const url = new URL(request.url());
      const what = `${request.method()} ${url.pathname}`;
      const entry = timeline.find(item => item.what === what && item.end === undefined);
      if (entry) entry.end = Date.now() - t0;
    });
    await page.goto("/", { waitUntil: "commit" });
    const marks: Record<string, number> = {};
    await page.waitForFunction(() => document.readyState !== "loading");
    marks.domReady = Date.now() - t0;
    await expect(screens.homeReady(page)).toBeVisible({ timeout: 30_000 });
    marks.homePrimary = Date.now() - t0;
    await expect(page.locator(".xp-overview-level")).toBeVisible({ timeout: 30_000 });
    marks.xpCard = Date.now() - t0;
    await expect(page.locator('main [aria-busy="true"]')).toHaveCount(0, { timeout: 30_000 });
    marks.noSkeletons = Date.now() - t0;
    await api.settle(800);
    const firstApi = timeline.filter(item => item.what.includes("/api/")).map(item => item.start);
    marks.firstApiRequest = Math.min(...firstApi);
    results.push({ marks, timeline: timeline.sort((a, b) => a.start - b.start) });
    await context.close();
  }
  const median = (values: number[]) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];
  const keys = Object.keys(results[0].marks);
  const summary = Object.fromEntries(keys.map(key => [key, median(results.map(result => result.marks[key]))]));
  writeFileSync(out!, JSON.stringify({
    conditions: { cpuThrottle: 4, viewport: "390x844", rttMs, admissionMs, server, runs },
    median: summary, results,
  }, null, 2));
});
