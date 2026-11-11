import { expect, test, type Page } from "@playwright/test";
import { isolateFromNetwork } from "./support";

/**
 * First weight, how it felt and the next suggestion on Today, end to end
 * against the real API served over an in-memory store:
 *   python tools/e2e_exercise_customisation_server.py --port 8765
 *   EXERCISE_TEST_API=http://127.0.0.1:8765 npx playwright test exercise-progression
 */
const api = process.env.EXERCISE_TEST_API;
const storageKey = `sb-${new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").hostname.split(".")[0]}-auth-token`;
const shots = process.env.EXERCISE_SCREENSHOTS;
const headers = { Authorization: "Bearer athlete-token" };

async function setup(page: Page, baseURL: string) {
  await isolateFromNetwork(page, baseURL);
  await page.route("https://fonts.googleapis.com/**", (route) => route.fulfill({ contentType: "text/css", body: "" }));
  await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [storageKey, JSON.stringify({
    access_token: "athlete-token", refresh_token: "test-refresh", token_type: "bearer",
    expires_in: 86400, expires_at: Math.floor(Date.now() / 1000) + 86400,
    user: { id: "athlete-1", email: "ari@example.com", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} },
  })]);
  await page.route("**/api/**", async (route) => {
    try {
      const request = route.request();
      const url = new URL(request.url());
      const response = await page.request.fetch(`${api}${url.pathname}${url.search}`, {
        method: request.method(),
        headers: { ...headers, "Content-Type": "application/json" },
        data: request.postData() ?? undefined,
        failOnStatusCode: false,
      });
      if (url.pathname === "/api/me" && response.ok()) {
        const me = await response.json();
        Object.assign(me.profile, {
          athlete_timezone: "UTC", technical_style: ["boxing"], date_of_birth: "1995-01-01", meets_minimum_age: true,
          is_minor: false, terms_accepted: true, health_consent_granted: true, private_trial_ack_at: "2026-01-01T12:00:00Z",
        });
        return await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(me) });
      }
      return await route.fulfill({ status: response.status(), contentType: "application/json", body: await response.body() });
    } catch (error) {
      if (error instanceof Error && /Route is already handled/.test(error.message)) return;
      throw error;
    }
  });
}

async function shot(page: Page, name: string) {
  if (shots) await page.screenshot({ path: `${shots}/${name}.png`, fullPage: true });
}

async function todayLog(page: Page, planId: string) {
  const response = await page.request.get(`${api}/api/today/exercise-logs?plan_id=${planId}`, { headers });
  expect(response.ok()).toBe(true);
  const body = await response.json();
  return {
    squat: body.logs.find((log: { exercise_key: string }) => log.exercise_key === "back-squat"),
    recentLoads: body.recent_loads as { exercise_key: string; load: { value: number; unit: string } }[],
  };
}

/** Check in green, open Today and start the session, leaving the exercise rows in view. */
async function startSession(page: Page, planId: string) {
  expect((await page.request.post(`${api}/api/today/checkin`, { headers,
    data: { plan_id: planId, sleep: "good", body: "normal", pain: "none", phase: "GPP" } })).ok()).toBe(true);
  await page.goto("/today");
  await page.getByRole("button", { name: "Start session", exact: true }).click();
  await page.getByRole("button", { name: "Close timer", exact: true }).click();
}

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

test("Today: first weight is asked once, persists, and progression is confirmed by the athlete", async ({ page, baseURL }) => {
  test.skip(!api, "Run with EXERCISE_TEST_API pointing at tools/e2e_exercise_customisation_server.py");
  test.setTimeout(120_000);
  expect((await page.request.post(`${api}/__e2e/reset?progression=true`)).ok()).toBe(true);
  const planId = (await (await page.request.get(`${api}/api/today`, { headers })).json()).active_plan.id;
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page, baseURL!);

  // Exposure 1: no weight on record. One tap logs it, then the weight is asked for.
  await startSession(page, planId);
  await page.getByRole("button", { name: "Mark Back Squat done as written", exact: true }).click();
  const question = page.getByLabel(/What weight did you use\?/);
  await expect(question).toBeVisible();
  // Logging was not held up by the question.
  expect((await todayLog(page, planId)).squat.status).toBe("as_prescribed");
  await shot(page, "1-first-weight-question");
  await question.fill("60");
  await page.getByRole("button", { name: "Save weight", exact: true }).click();
  await expect(question).toHaveCount(0);
  await page.getByRole("button", { name: "Easy", exact: true }).click();
  await expect(page.getByRole("button", { name: "Easy", exact: true })).toHaveAttribute("aria-pressed", "true");
  await shot(page, "2-weight-and-feel-saved");
  let saved = (await todayLog(page, planId)).squat;
  expect(saved.actual.load).toEqual({ value: 60, unit: "kg" });
  expect(saved.actual.feel).toBe("easy");
  expect(saved.status).toBe("as_prescribed");
  expect(saved.prescribed.rep_range).toEqual([8, 12]);

  // Exposure 2: the weight is remembered and a rep is suggested, not recorded.
  expect((await page.request.post(`${api}/__e2e/next_exposure`)).ok()).toBe(true);
  expect((await todayLog(page, planId)).recentLoads).toEqual([expect.objectContaining({ exercise_key: "back-squat", load: { value: 60, unit: "kg" } })]);
  await startSession(page, planId);
  await page.getByRole("button", { name: "Back Squat", exact: true }).click();
  // The remembered weight is shown on the row's recommendation.
  await expect(page.getByText("60 kg").first()).toBeVisible();
  const suggestion = page.getByRole("button", { name: /Try 9 reps/ });
  await expect(suggestion).toBeVisible();
  await shot(page, "3-next-exposure-suggestion");
  await suggestion.click();
  await expect(page.getByLabel("Reps", { exact: true })).toHaveValue("9");
  await expect(page.getByLabel(/^Load/)).toHaveValue("60");
  // Tapping the suggestion only set the numbers up.
  expect((await todayLog(page, planId)).squat).toBeUndefined();
  await shot(page, "4-suggestion-prepared-not-saved");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".ex-log-status")).toContainText("9 reps");
  saved = (await todayLog(page, planId)).squat;
  expect(saved.actual.reps).toBe(9);
  expect(saved.actual.load).toEqual({ value: 60, unit: "kg" });
  // Inside the permitted range it is the workout done, not a modification.
  expect(saved.status).toBe("as_prescribed");

  // Exposure 3: one tap records the prescription only. Neither the remembered
  // weight nor last time's 9 reps is assumed.
  expect((await page.request.post(`${api}/__e2e/next_exposure`)).ok()).toBe(true);
  await startSession(page, planId);
  await page.getByRole("button", { name: "Mark Back Squat done as written", exact: true }).click();
  await expect(page.getByRole("button", { name: "I did 9 again", exact: true })).toHaveCount(0);
  saved = (await todayLog(page, planId)).squat;
  expect(saved.actual.reps).toBeNull();
  expect(saved.actual.load).toBeNull();
  // The weight is still on record for this lift.
  expect((await todayLog(page, planId)).recentLoads).toEqual([expect.objectContaining({ exercise_key: "back-squat", load: { value: 60, unit: "kg" } })]);
  // Done leaves the row collapsed; the athlete opens the existing logger.
  await expect(page.getByRole("button", { name: "Log numbers", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Back Squat", exact: true }).click();
  // A remembered weight suppresses the first-weight question, even when opened.
  await expect(page.getByLabel(/What weight did you use\?/)).toHaveCount(0);
  await page.locator(".ex-log-edit").click();
  await page.getByRole("button", { name: "Log numbers", exact: true }).click();
  await page.getByLabel("Reps", { exact: true }).fill("9");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".ex-log-status")).toContainText("9 reps");
  saved = (await todayLog(page, planId)).squat;
  expect(saved.actual.reps).toBe(9);
  expect(saved.status).toBe("as_prescribed");
  await shot(page, "5-additional-reps-saved-in-logger");
  expect(errors).toEqual([]);
});
