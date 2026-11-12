import { expect, test, type Page } from "@playwright/test";
import { mockAthlete, navigate, PRIMARY, screens } from "./request-journeys";

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, contextOptions: { reducedMotion: "reduce" } });

const tab = (page: Page, href: string) => page.locator(`nav a[href="${href}"]:visible`).first().click();
const planStrip = (page: Page) => page.locator(".overview-operational-value").first();
const decisionAction = (page: Page) => page.locator(".overview-primary-action");

/** Serves Today slowly from the given visit on, so a revisit can be observed mid-read. */
function slowTodayFrom(visit: number, ms: number) {
  let todayReads = 0;
  return (_method: string, path: string) => {
    if (path !== "/api/today") return 0;
    todayReads++;
    return todayReads >= visit ? ms : 0;
  };
}

test("a revisited Home shows plan context at once but no decision until Today is read fresh", async ({ page, baseURL }) => {
  const api = await mockAthlete(page, baseURL!, { latencyFor: slowTodayFrom(3, 2_500) });
  await page.goto("/");
  await expect(decisionAction(page)).toBeVisible();
  await tab(page, "/today");
  await expect(screens.today(page)).toBeVisible();
  const reads = api.calls.filter(call => call.path === "/api/today").length;
  await tab(page, "/");
  // Presentation from this session's last read shows immediately…
  await expect(planStrip(page)).toBeVisible({ timeout: 1_000 });
  // …while the decision, its action and the training alerts wait for the fresh read.
  await expect(page.getByText("Checking today’s training decision…")).toBeAttached();
  await expect(decisionAction(page)).toHaveCount(0);
  await expect(page.locator(".overview-risk-card[aria-busy='true']")).toBeVisible();
  await expect(decisionAction(page)).toBeVisible({ timeout: 5_000 });
  await expect(page.getByText("Checking today’s training decision…")).toHaveCount(0);
  // Home still read Today again on this visit.
  expect(api.calls.filter(call => call.path === "/api/today").length).toBe(reads + 1);
});

test("a failed fresh read leaves Home without a decision and offers a retry", async ({ page, baseURL }) => {
  const api = await mockAthlete(page, baseURL!);
  await page.goto("/");
  await expect(decisionAction(page)).toBeVisible();
  await tab(page, "/today");
  await expect(screens.today(page)).toBeVisible();
  await api.settle();
  // The gateway keeps failing, past the client's transient retries.
  let failing = true;
  await page.route("**/api/today", route => (failing
    ? route.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"unavailable"}' })
    : route.fallback()));
  await tab(page, "/");
  await expect(planStrip(page)).toBeVisible();
  await expect(page.getByText("Today's status unavailable")).toBeVisible({ timeout: 15_000 });
  await expect(decisionAction(page)).toHaveCount(0);
  await expect(page.locator(".overview-risk-card")).toContainText("Unavailable until today’s status loads.");
  failing = false;
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(decisionAction(page)).toBeVisible();
});

test("after a write, Home does not show the earlier view at all", async ({ page, baseURL }) => {
  await mockAthlete(page, baseURL!, { latencyFor: slowTodayFrom(2, 2_500) });
  await page.goto("/plans");
  await expect(page.locator(`main a[href="/plans/${PRIMARY.planId}"]`).first()).toBeVisible();
  await page.getByRole("button", { name: /Manage/ }).first().click();
  await page.getByRole("button", { name: "Rename", exact: true }).click();
  await page.getByLabel("Plan name").fill("Renamed camp");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Plan renamed.")).toBeVisible();
  await page.keyboard.press("Escape");
  await tab(page, "/");
  await page.waitForTimeout(800);
  await expect(planStrip(page)).toHaveCount(0);
  await expect(decisionAction(page)).toBeVisible({ timeout: 6_000 });
});

test("a revisited History shows its rows at once and still reads them again", async ({ page, baseURL }) => {
  let historyReads = 0;
  const api = await mockAthlete(page, baseURL!, {
    latencyFor: (_method, path) => (path.startsWith("/api/training/history") && ++historyReads >= 2 ? 2_500 : 0),
  });
  await page.goto("/");
  await expect(decisionAction(page)).toBeVisible();
  await tab(page, "/history");
  await expect(screens.history(page)).toBeVisible();
  await tab(page, "/");
  await expect(planStrip(page)).toBeVisible();
  await tab(page, "/history");
  await expect(screens.history(page)).toBeVisible({ timeout: 1_000 });
  await api.settle();
  expect(api.calls.filter(call => call.path.startsWith("/api/training/history?include_skipped")).length).toBe(2);
});

test("tab taps do not wait on a route payload from the server", async ({ page, baseURL }) => {
  await mockAthlete(page, baseURL!);
  const blocking: string[] = [];
  page.on("request", request => {
    const url = new URL(request.url());
    if (url.searchParams.has("_rsc") && !request.headers()["next-router-prefetch"]) blocking.push(url.pathname);
  });
  await page.goto("/");
  await expect(decisionAction(page)).toBeVisible();
  await page.waitForTimeout(1_000); // idle prefetch of the tab routes
  blocking.length = 0;
  for (const href of ["/today", "/history", "/", "/today", "/"]) {
    await tab(page, href);
    await expect(page).toHaveURL(new RegExp(`${href === "/" ? "/$" : href}$`));
  }
  expect(blocking).toEqual([]);
});

test("speed diagnostics stay off by default, record navigations when on, and leak no ids", async ({ page, baseURL }) => {
  await mockAthlete(page, baseURL!);
  await page.goto("/");
  await expect(decisionAction(page)).toBeVisible();
  await expect(page.locator("[data-perf-panel]")).toHaveCount(0);

  await page.goto("/?perf=1");
  await expect(page.locator("[data-perf-panel]")).toBeVisible();
  await navigate(page, "Today");
  await expect(screens.today(page)).toBeVisible();
  await navigate(page, "Home");
  await expect(decisionAction(page)).toBeVisible();
  await expect(page.locator("[data-perf-panel] button").first()).toHaveText(/⏱ [12]/, { timeout: 10_000 });
  await page.locator("[data-perf-panel] button").first().click();
  await page.getByText("Show report").click();
  const report = JSON.parse(await page.locator("[data-perf-panel] pre").textContent() ?? "{}");
  expect(report.navigations.length).toBeGreaterThan(0);
  const visit = report.navigations.find((entry: { to: string }) => entry.to === "/today");
  expect(visit.routeChanged).not.toBeNull();
  expect(visit.via).toBe("nav");
  expect(visit.href).toBe("/today");
  for (const request of visit.requests.filter((entry: { kind: string }) => entry.kind === "route")) {
    expect(typeof request.prefetch).toBe("boolean");
  }
  expect(visit.requests.some((request: { path: string }) => request.path === "/api/today")).toBe(true);
  const text = JSON.stringify(report);
  expect(text).not.toContain(PRIMARY.token);
  expect(text).not.toContain(PRIMARY.athleteId);
  expect(text).not.toContain(PRIMARY.planId);

  await page.getByRole("button", { name: "Turn off" }).click();
  await expect(page.locator("[data-perf-panel]")).toHaveCount(0);
  expect(await page.evaluate(() => [localStorage.getItem("unlxck-perf-diagnostics"), localStorage.getItem("unlxck-perf-diagnostics-log")])).toEqual([null, null]);
});

test("a revisited Plans list shows at once and is still read again", async ({ page, baseURL }) => {
  let listReads = 0;
  const api = await mockAthlete(page, baseURL!, {
    latencyFor: (method, path) => (method === "GET" && path === "/api/plans" && ++listReads >= 2 ? 2_500 : 0),
  });
  await page.goto("/plans");
  const planLink = page.locator(`main a[href="/plans/${PRIMARY.planId}"]`).first();
  await expect(planLink).toBeVisible();
  await api.settle();
  await tab(page, "/");
  await expect(planStrip(page)).toBeVisible();
  await tab(page, "/plans");
  // The list this session last read, with no placeholder card, while it is read again.
  await expect(planLink).toBeVisible({ timeout: 1_000 });
  await expect(page.locator("main .skeleton")).toHaveCount(0);
  await api.settle();
  expect(api.calls.filter(call => call.method === "GET" && call.path === "/api/plans").length).toBe(2);
});
