import { test, expect } from "@playwright/test";
import { isolateFromNetwork } from "./support";
import fixture from "./fixtures/product-analytics-populated.json";

// Non-sensitive synthetic report from the Python calculator, fixed at 25 Oct 2026.
// These are regression examples, never production performance claims.
for (const width of [390, 1280]) {
  test(`populated analytics identities, coverage, plan funnel and pagination (${width})`, async ({
    page,
    baseURL,
  }) => {
    await isolateFromNetwork(page, baseURL!);
    await page.setViewportSize({ width, height: 950 });
    await page.addInitScript(() =>
      localStorage.setItem(
        "sb-stub-auth-token",
        JSON.stringify({
          access_token: "admin-token",
          refresh_token: "local-test",
          token_type: "bearer",
          expires_in: 86400,
          expires_at: Math.floor(Date.now() / 1000) + 86400,
          user: {
            id: "admin-1",
            email: "admin@example.test",
            aud: "authenticated",
            role: "authenticated",
            app_metadata: {},
            user_metadata: {},
          },
        }),
      ),
    );
    const requests: string[] = [];
    let partial = false;
    await page.route("**/api/**", async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/me")
        return route.fulfill({
          json: {
            profile: {
              athlete_id: "admin-1",
              email: "admin@example.test",
              full_name: "Verification Admin",
              role: "admin",
              access_status: "approved",
              starting_path: "tracking_first",
              health_consent_granted: true,
            },
            compliance: {
              current_versions: {},
              accepted_versions: {},
              requires_acceptance: false,
            },
          },
        });
      if (url.pathname === "/api/admin/product-analytics") {
        requests.push(url.search);
        const data = { ...structuredClone(fixture), tracking_health: { healthy: true }, tracking_since: "2026-10-30T00:00:00Z", visitors_today: 2, visitors_7d: 3, visitors_30d: 4,
          latest_browser_event: "2026-11-04T16:00:00Z", browser_reporting_athletes: 3, seen_recently: 1, no_visit_7d: 24,
          athletes: fixture.athletes.map((p, i) => ({ ...p, last_seen: i === 0 ? "2026-11-04T16:00:00Z" : null,
            latest_action: i === 0 ? { feature: "history", action: "page_viewed" } : null,
            visit_days_7: i === 0 ? 2 : 0, visit_days_30: i === 0 ? 3 : 0 })),
        };
        const offset = Number(url.searchParams.get("offset") || 0);
        const query = url.searchParams.get("athlete_search")?.toLowerCase() ?? "";
        const matches = data.athletes.filter(p => (!query || `${p.full_name} ${p.email}`.toLowerCase().includes(query))
          && (url.searchParams.get("visit_filter") !== "never_seen" || !p.last_seen));
        data.athlete_total = matches.length;
        data.athletes = matches.slice(offset, offset + 25);
        data.offset = offset;
        if (partial) {
          data.coverage = {
            status: "partial",
            measurable_athletes: 1,
            eligible_athletes: 27,
            fully_measured_athletes: 0,
          };
          for (const feature of data.features) {
            if (feature.coverage) feature.coverage = data.coverage;
            if (["timer", "nutrition"].includes(feature.feature))
              Object.assign(feature, {
                athletes: 0,
                uses: 0,
                percent: 0,
                repeat_athletes: 0,
              });
          }
        }
        return route.fulfill({ json: data });
      }
      return route.fulfill({ json: [] });
    });
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto("/admin/analytics");
    await expect(
      page.getByRole("heading", { name: "Product Analytics", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("link", { name: "Example Athlete", exact: true }),
    ).toHaveAttribute("href", /000000000001$/);
    await expect(
      page.getByRole("link", { name: "fallback@example.test", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("link", { name: "abcdef00", exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Tracking collector ready", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Visited today", exact: true })).toBeVisible();
    await expect(page.locator("#analytics-4")).toContainText("history · Viewed a page");
    const history = page
      .locator("#analytics-2 details")
      .filter({ has: page.locator("summary span", { hasText: /^history$/ }) });
    await history.locator("summary").click();
    await expect(history).toContainText(
      "Training logs do not establish a History visit",
    );
    const timer = page
      .locator("#analytics-2 details")
      .filter({ has: page.locator("summary span", { hasText: /^timer$/ }) });
    await expect(timer.locator("summary")).toContainText("Not measured");
    await expect(timer.locator("summary")).not.toContainText(
      "0 recorded athletes",
    );
    await expect(page.locator("#analytics-3")).toContainText(
      "1 / 1 mature usable plans",
    );
    await expect(page.locator("#analytics-3")).toContainText(
      "1 usable plans awaiting seven full days",
    );
    await expect(page.locator("#analytics-0")).toContainText(
      "Manual training logged",
    );
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const shots = process.env.ANALYTICS_SCREENSHOTS;
    if (shots) {
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({ path: `${shots}/overview-${width}.png` });
      for (const [id, name] of [
        ["4", "athlete-directory"],
        ["2", "feature-adoption"],
        ["3", "plan-conversion"],
      ]) {
        await page
          .locator(`#analytics-${id}`)
          .screenshot({ path: `${shots}/${name}-${width}.png` });
      }
    }
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await expect.poll(() => requests.at(-1)).toContain("offset=25");
    await expect(
      page.getByRole("link", { name: "fallback@example.test", exact: true }),
    ).toHaveCount(0);
    partial = true;
    await page
      .getByRole("combobox", { name: "Period", exact: true })
      .selectOption("7");
    await expect.poll(() => requests.at(-1)).toContain("offset=0");
    await expect(timer.locator("summary")).toContainText("0 recorded athletes");
    await expect(timer.locator("summary")).toContainText("Partial coverage");
    await expect(page.locator("#analytics-2")).toContainText("1 / 27 athletes");
    await page.getByRole("searchbox", { name: "Find an athlete" }).fill("fallback@example.test");
    await page.getByRole("button", { name: "Search athletes", exact: true }).click();
    await expect.poll(() => requests.at(-1)).toContain("athlete_search=fallback%40example.test");
    await expect(page.getByRole("link", { name: "fallback@example.test", exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "Example Athlete", exact: true })).toHaveCount(0);
    await page.getByRole("combobox", { name: "Visit history" }).selectOption("never_seen");
    await expect.poll(() => requests.at(-1)).toContain("visit_filter=never_seen");
    expect(errors).toEqual([]);
  });
}
