import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { mockAthlete, PRIMARY, screens, TODAY_BLOCK } from "./request-journeys";

/**
 * Opt-in navigation benchmark on a phone profile, following the 5 Nov 00:52
 * iPhone recording: Today → Home → Today → Training plan → History → Timer → Home.
 *
 *   NAV_BENCH_OUT=out.json [NAV_BENCH_RUNS=5] npx playwright test e2e/navigation-bench.spec.ts
 *
 * Real: the production build, 390×844 touch viewport, 4× CPU throttling,
 * client-side navigation through the app nav, React rendering. Simulated: each
 * API response waits NAV_BENCH_SERVER[endpoint] (server time; defaults from
 * the 00:52 production logs and the API replay) plus NAV_BENCH_RTT_MS.
 *
 * Per step, measured in the page from the tap: the URL change, the screen's
 * first real content (not a placeholder), the screen being usable, the last
 * placeholder disappearing, and the API requests the step caused.
 */
const out = process.env.NAV_BENCH_OUT;
const runs = Number(process.env.NAV_BENCH_RUNS ?? 5);
const rttMs = Number(process.env.NAV_BENCH_RTT_MS ?? 150);
// Route payloads (`?_rsc=`) come from the Next server. In production that is a
// Vercel function in iad1: 0.18–0.40 s TTFB measured from the US east coast,
// before a UK phone's extra transatlantic and mobile round trips.
const rscMs = Number(process.env.NAV_BENCH_RSC_MS ?? 0);
const server: Record<string, number> = {
  me: 150, today: 900, plan: 400, plans: 250, history: 350, xp: 500, completions: 150, other: 100,
  ...JSON.parse(process.env.NAV_BENCH_SERVER ?? "{}"),
};

function serverMs(path: string): number {
  if (path === "/api/me") return server.me;
  if (path === "/api/today") return server.today;
  if (path === "/api/xp/progress") return server.xp;
  if (path === "/api/plans") return server.plans;
  if (path.endsWith("/completions")) return server.completions;
  if (/^\/api\/plans\/[0-9a-f-]{36}$/.test(path)) return server.plan;
  if (path.startsWith("/api/training/history")) return server.history;
  return server.other;
}

// CSS selectors, or "text:<words>" for a button containing those words, so the
// page itself can poll them every frame.
type Screen = { content: string; usable: string };
const SCREENS: Record<string, Screen> = {
  home: { content: ".overview-operational-value", usable: ".overview-primary-action" },
  today: { content: `text:${TODAY_BLOCK}`, usable: `text:${TODAY_BLOCK}` },
  plans: { content: `main a[href="/plans/${PRIMARY.planId}"]`, usable: `main a[href="/plans/${PRIMARY.planId}"]` },
  history: { content: "main ul.history-list li", usable: "main ul.history-list button[aria-label^='View']" },
  timer: { content: ".st-root", usable: ".st-root button" },
};

const PLACEHOLDERS = "main [aria-busy='true'], main .skeleton, main .loading-card";

async function measure(page: Page, screen: Screen, tap: () => Promise<unknown>) {
  await page.evaluate(() => { (window as unknown as { __tap: number }).__tap = performance.now(); });
  await tap();
  const marks = await page.evaluate(async ({ content, usable, placeholders }) => {
    const start = (window as unknown as { __tap: number }).__tap;
    const startPath = location.pathname;
    const seen: Record<string, number> = {};
    const shown = (el: HTMLElement) => el.getClientRects().length > 0;
    const visible = (selector: string) => selector.startsWith("text:")
      ? [...document.querySelectorAll<HTMLElement>("main button")].some(el => shown(el) && (el.textContent ?? "").includes(selector.slice(5)))
      : [...document.querySelectorAll<HTMLElement>(selector)].some(shown);
    return await new Promise<Record<string, number>>(resolve => {
      const check = () => {
        const now = performance.now() - start;
        if (seen.route === undefined && location.pathname !== startPath) seen.route = now;
        if (seen.content === undefined && visible(content)) seen.content = now;
        if (seen.usable === undefined && visible(usable)) seen.usable = now;
        const settledPlaceholders = !visible(placeholders);
        if (seen.content !== undefined && seen.usable !== undefined && settledPlaceholders) {
          seen.noPlaceholders = now;
          resolve(seen);
          return;
        }
        if (now > 20_000) { resolve({ ...seen, timeout: now }); return; }
        requestAnimationFrame(check);
      };
      requestAnimationFrame(check);
    });
  }, { content: screen.content, usable: screen.usable, placeholders: PLACEHOLDERS });
  return marks;
}

const nav = (page: Page, href: string) => page.locator(`nav a[href="${href}"]:visible`).first().click();

test("measure repeat navigation on a throttled phone", async ({ browser, baseURL }) => {
  test.skip(!out, "Set NAV_BENCH_OUT to record a navigation benchmark");
  test.setTimeout(runs * 240_000);
  const results: Array<Record<string, unknown>> = [];
  for (let run = 0; run < runs; run++) {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, reducedMotion: "reduce" });
    const page = await context.newPage();
    const cdp = await context.newCDPSession(page);
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
    const api = await mockAthlete(page, baseURL!, { latencyFor: (_method, path) => serverMs(path) + rttMs });
    let rscRequests = 0;
    if (rscMs) {
      await page.route(url => url.searchParams.has("_rsc"), async route => {
        rscRequests++;
        await new Promise(resolve => setTimeout(resolve, rscMs));
        await route.fallback();
      });
    }
    // Cold start lands on Today, as in the recording.
    await page.goto("/today");
    await expect(screens.today(page)).toBeVisible({ timeout: 30_000 });
    await api.settle(1_000);
    const steps: Array<{ step: string; marks: Record<string, number>; requests: string[] }> = [];
    const step = async (name: string, screen: Screen, tap: () => Promise<unknown>) => {
      const first = api.calls.length;
      const rscBefore = rscRequests;
      const marks = await measure(page, screen, tap);
      await api.settle(600);
      const requests = api.calls.slice(first).map(call => `${call.method} ${call.path}`);
      for (let index = rscBefore; index < rscRequests; index++) requests.push("GET <route payload ?_rsc>");
      steps.push({ step: name, marks, requests });
    };
    await step("Today → Home", SCREENS.home, () => nav(page, "/"));
    await step("Home → Today", SCREENS.today, () => nav(page, "/today"));
    await step("Today → Training plan", SCREENS.plans, () => nav(page, "/plans"));
    await step("Training plan → History", SCREENS.history, () => nav(page, "/history"));
    await step("History → Timer", SCREENS.timer, () => nav(page, "/timer"));
    await page.getByRole("button", { name: "Minimise timer", exact: true }).click();
    await step("Timer → Home", SCREENS.home, () => nav(page, "/"));
    await step("Home → History", SCREENS.history, () => nav(page, "/history"));
    await step("History → Home", SCREENS.home, () => nav(page, "/"));
    results.push({ steps });
    await context.close();
  }
  const names = (results[0].steps as Array<{ step: string }>).map(entry => entry.step);
  const median = (values: number[]) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];
  const summary = Object.fromEntries(names.map(name => {
    const all = results.map(result => (result.steps as Array<{ step: string; marks: Record<string, number>; requests: string[] }>).find(entry => entry.step === name)!);
    const keys = ["route", "content", "usable", "noPlaceholders"];
    return [name, { ...Object.fromEntries(keys.map(key => [key, Math.round(median(all.map(entry => entry.marks[key] ?? NaN)))])), requests: all[0].requests }];
  }));
  writeFileSync(out!, JSON.stringify({ conditions: { cpuThrottle: 4, viewport: "390x844", rttMs, rscMs, server, runs }, median: summary, results }, null, 2));
});
