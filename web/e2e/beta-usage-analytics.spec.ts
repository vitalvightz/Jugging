import { test, expect } from "@playwright/test";
import { isolateFromNetwork } from "./support";

test("a beta athlete sends visits and page events without any preference request", async ({ page, baseURL }) => {
  await isolateFromNetwork(page, baseURL!);
  await page.addInitScript(() => localStorage.setItem("sb-stub-auth-token", JSON.stringify({
    access_token: "beta-test", refresh_token: "test", token_type: "bearer",
    expires_in: 86400, expires_at: Math.floor(Date.now() / 1000) + 86400,
    user: { id: "beta-1", email: "beta@example.test", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} },
  })));
  const events: Record<string, unknown>[] = [];
  const choices: string[] = [];
  await page.route("**/api/**", async route => {
    const endpoint = new URL(route.request().url()).pathname;
    if (endpoint === "/api/me") return route.fulfill({ json: {
      profile: { athlete_id: "beta-1", role: "athlete", access_status: "approved", starting_path: null,
        email: "beta@example.test", technical_style: ["boxing"], tactical_style: [],
        meets_minimum_age: true, terms_accepted: true, is_minor: false, health_consent_granted: true,
        date_of_birth: "1990-01-01", onboarding_draft: {}, nutrition_profile: {} },
      plan_count: 0, latest_plan: null, latest_intake: null,
    } });
    if (endpoint === "/api/analytics/events") {
      events.push(route.request().postDataJSON());
      return route.fulfill({ status: 202, json: { accepted: true } });
    }
    if (endpoint === "/api/analytics/preference") choices.push(route.request().method());
    return route.fulfill({ json: {} });
  });
  await page.goto("/get-started");
  await expect(page.getByRole("heading", { name: "Private beta usage analytics" })).toHaveCount(0);
  await expect(page.getByText("Start with")).toBeVisible();
  await expect.poll(() => events.some(e => e.event_name === "app_opened")).toBe(true);
  await expect.poll(() => events.some(e => e.event_name === "page_viewed")).toBe(true);
  expect(choices).toEqual([]);
  for (const e of events) {
    expect(Object.keys(e).sort()).toEqual(["event_name", "feature", "request_id"]);
    expect(JSON.stringify(e)).not.toContain("beta@example.test");
  }
  await expect(page.getByText("Allow optional feature analytics")).toHaveCount(0);
});
