import { expect, test } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { CORE_JOURNEY, PRIMARY, duplicateGets, mockAthlete, navigate, runJourney, screens, type StepResult } from "./request-journeys";

const plan = `/api/plans/${PRIMARY.planId}`;
const paths = (step: StepResult) => step.requests.map(({ method, path }) => `${method} ${path}`);
const byJourney = (steps: StepResult[]) => Object.fromEntries(steps.map(step => [step.journey, paths(step)]));

// Entrance motion is irrelevant to request counts and makes clicks wait.
test.use({ contextOptions: { reducedMotion: "reduce" } });

test.describe("frontend request budget", () => {
  test("central athlete journey reuses fresh reads and repeats none", async ({ page, baseURL }) => {
    test.setTimeout(90_000);
    const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
    const api = await mockAthlete(page, baseURL!);
    const steps = byJourney(await runJourney(page, api));

    // Startup still reads everything Home shows, exactly once. This athlete's
    // plan is open (no fight date), so Home shows no countdown and never reads
    // the plan detail that only the countdown's calendar would use.
    expect(steps["Login → Home"]).toEqual(expect.arrayContaining(["GET /api/me", "GET /api/today", "GET /api/xp/progress"]));
    expect(steps["Login → Home"]).not.toContain(`GET ${plan}`);
    for (const [journey, calls] of Object.entries(steps)) {
      expect(duplicateGets(calls.map(call => ({ method: call.split(" ")[0], path: call.split(" ")[1], bytes: 0 }))), journey).toEqual([]);
    }
    // Today and Home always re-read the authoritative command view…
    for (const journey of ["Home → Today", "Viewer → Today", "Today → Home", "History → Home (return)"]) {
      expect(steps[journey], journey).toContain("GET /api/today");
    }
    // …but the plan detail just read by another screen is not fetched again,
    // and opening Today is not a reward-affecting action.
    for (const journey of ["Today → Viewer", "Viewer → Today", "Today → Home", "Plans → Viewer"]) {
      expect(steps[journey], journey).not.toContain(`GET ${plan}`);
    }
    expect(steps["Home → Today"]).not.toContain("GET /api/xp/progress");
    // Presentation-only consumers reuse a Today view from this navigation.
    expect(steps["Home → Plans"]).not.toContain("GET /api/today");
    expect(steps["Today → Viewer"]).not.toEqual(expect.arrayContaining(["GET /api/today"]));
    expect(steps["Plans → Viewer"]).not.toEqual(expect.arrayContaining(["GET /api/today", "GET /api/plans/active"]));
    // Completion rows stay fresh on every Viewer visit.
    for (const journey of ["Today → Viewer", "Plans → Viewer"]) expect(steps[journey], journey).toContain(`GET ${plan}/completions`);
    expect(errors).toEqual([]);
  });

  test("a rename invalidates every reused plan read", async ({ page, baseURL }) => {
    test.setTimeout(60_000);
    const api = await mockAthlete(page, baseURL!);
    await page.goto("/");
    await expect(screens.home(page)).toBeVisible();
    await navigate(page, "Plans");
    await page.getByRole("button", { name: /Manage/ }).first().click();
    await page.getByRole("button", { name: "Rename", exact: true }).click();
    await page.getByLabel("Plan name").fill("Renamed camp");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByText("Plan renamed.")).toBeVisible();
    const first = api.calls.length;
    await page.keyboard.press("Escape");
    await page.locator(`main a[href="${plan.replace("/api", "")}"]`).first().click();
    await expect(screens.viewer(page)).toHaveText("Renamed camp");
    await expect(screens.viewerReady(page)).toBeVisible();
    await api.settle();
    // The write made every earlier read unusable: the Viewer reads them again.
    expect(api.calls.slice(first).map(({ method, path }) => `${method} ${path}`))
      .toEqual(expect.arrayContaining([`GET ${plan}`, "GET /api/today", "GET /api/plans/active"]));
  });

  test("a failed shared read is retried rather than reused", async ({ page, baseURL }) => {
    test.setTimeout(60_000);
    const api = await mockAthlete(page, baseURL!);
    api.failNextRequest(plan);
    await page.goto("/");
    await expect(screens.home(page)).toBeVisible();
    await navigate(page, "Today");
    await expect(screens.today(page)).toBeVisible();
    // The gateway failure is retried (Today joins that retry rather than
    // adding a parallel read) and the failed response is never reused.
    const planReads = () => api.calls.filter(call => call.method === "GET" && call.path === plan).map(call => call.status);
    await expect.poll(planReads, { timeout: 15_000 }).toEqual([504, 200]);
  });
});

// Opt-in benchmark: FRONTEND_BENCH_OUT=<file> [FRONTEND_BENCH_RUNS=15]
// [FRONTEND_BENCH_LATENCY_MS=150]. Mocked browser runs, not production data.
const benchOut = process.env.FRONTEND_BENCH_OUT;
test.describe("frontend request benchmark", () => {
  test.skip(!benchOut, "Set FRONTEND_BENCH_OUT to record a benchmark");
  test("record repeated journeys", async ({ browser, baseURL }) => {
    const runs = Number(process.env.FRONTEND_BENCH_RUNS ?? 15);
    const latencyMs = Number(process.env.FRONTEND_BENCH_LATENCY_MS ?? 150);
    test.setTimeout(runs * 120_000);
    const results: StepResult[][] = [];
    for (let run = 0; run < runs; run += 1) {
      const context = await browser.newContext({ reducedMotion: "reduce" });
      const page = await context.newPage();
      const api = await mockAthlete(page, baseURL!, { latencyMs });
      results.push(await runJourney(page, api, CORE_JOURNEY));
      await context.close();
    }
    writeFileSync(benchOut!, JSON.stringify({ latencyMs, runs, results }));
  });
});
