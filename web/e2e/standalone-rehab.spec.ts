import { expect, test, type Page } from "@playwright/test";
import path from "node:path";
import fixtures from "./fixtures/standalone-rehab.json";
import type { TodayCommandView } from "../lib/types";
import { isolateFromNetwork } from "./support";

async function setup(page: Page, baseURL: string, variant: keyof typeof fixtures) {
  await isolateFromNetwork(page, baseURL);
  const source = structuredClone(fixtures[variant]);
  const view = source.view as unknown as TodayCommandView;
  const profile = { ...source.profile, profile: { ...source.profile.profile,
    private_trial_ack_at: "2026-10-05T12:00:00Z" } };
  const now = new Date(`${view.today.training_day}T12:05:00Z`);
  await page.clock.setFixedTime(now);
  const key = "sb-stub-auth-token";
  await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [key, JSON.stringify({
    access_token: "athlete-token", refresh_token: "fixture", token_type: "bearer", expires_at: now.getTime() / 1000 + 86400,
    user: { id: profile.profile.athlete_id, email: "thevital1@example.test", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} },
  })]);
  const writes: Record<string, unknown>[] = [];
  await page.route("**/api/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/me") return json(profile);
    if (url.pathname === "/api/today") return json(view);
    if (url.pathname === "/api/today/session-completion") {
      const payload = request.postDataJSON(); writes.push(payload);
      for (const injury of view.open_injuries) {
        if (view.live_prescription?.session.blocks?.some(block => block.injury_id === injury.id) && injury.rehab_decision?.schedule) {
          injury.rehab_decision.schedule = { state: "already_completed", reason: "You have already logged rehab for this injury today.", next_due_day: null };
        }
      }
      view.live_prescription = null;
      return json({ completion: { ...payload, id: "completion", training_day: view.today.training_day }, completion_status: payload.status,
        landing_session_state: "session_done", rehab_response_prompts: [] }, 201);
    }
    if (url.pathname === "/api/today/injury-episode-observation") { writes.push(request.postDataJSON()); return json({}); }
    if (url.pathname === "/api/training/history") return json({ sessions: [] });
    if (/session-completions|sparring-logs|exercise-logs|pending/.test(url.pathname)) return json({ completions: [], logs: [], response_sets: [] });
    if (url.pathname === "/api/plans") return json([]);
    return json({});
  });
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/today");
  await expect(page.getByRole("heading", { name: "Today", exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/today$/);
  await expect(page).not.toHaveTitle("");
  return { view, writes, errors };
}

for (const width of [375, 1280]) {
  test(`tracking-only athlete opens and completes prescribed rehab at ${width}px`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const { writes, errors } = await setup(page, baseURL!, "available");
    await expect(page.locator("#today-session")).toHaveCount(0);
    await expect(page.locator("#today-rehab")).toBeVisible();
    await page.locator(".today-injury-summary-row").first().click();
    const access = page.locator(".today-injury-item").first().getByRole("link", { name: /View rehab/ });
    const target = await access.getAttribute("href");
    expect(target).toBeTruthy();
    await access.click();
    const exercise = page.locator(`[id="${decodeURIComponent(target!.slice(1))}"]`);
    await expect(exercise).toBeVisible();
    await exercise.getByRole("button").first().click();
    await expect(page.getByRole("dialog")).toContainText("How to perform");
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "Complete rehab as shown", exact: true }).click();
    await expect(page.locator("#today-rehab")).toHaveCount(0);
    const summary = page.locator(".today-injury-summary-row").first();
    if (await summary.getAttribute("aria-expanded") !== "true") await summary.click();
    await expect(page.locator(".today-injury-care").first()).toContainText("Done for today");
    expect(writes).toHaveLength(1);
    expect(writes[0]).toMatchObject({ plan_id: null, status: "done", rehab_performance: "done_as_shown", rehab_tracking: "injury_checkin" });
    await expect(page.locator('a[href="#today-session"]')).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (process.env.REHAB_QA_DIR) await page.screenshot({ path: path.join(process.env.REHAB_QA_DIR, `standalone-completed-${width}.png`), fullPage: true });
    expect(errors).toEqual([]);
  });
}

test("thevital1 shoulder sprain and knee swelling show actual stages, permissions and content availability", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 375, height: 844 });
  const { view, writes, errors } = await setup(page, baseURL!, "tracking_only");
  await expect(page.locator(".today-injury-summary-row")).toHaveCount(2);
  for (let index = 0; index < view.open_injuries.length; index++) {
    const injury = view.open_injuries[index];
    await page.locator(".today-injury-summary-row").nth(index).click();
    const card = page.locator(".today-injury-item").nth(index);
    await expect(card).toContainText("CALM → RESTORE → LOAD → DYNAMIC → RETURN");
    await expect(card).toContainText("Contact training");
    if (!injury.rehab_decision?.prescription) {
      await expect(card).toContainText("No rehab exercises available for this stage yet.");
      await expect(card.locator(".injury-rehab-action")).toHaveCount(0);
      await expect(card).not.toContainText("Rehab due today");
    }
    await card.getByRole("button", { name: "Change clearance" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("radio", { name: "Sport-specific rehab", exact: true })).toBeChecked();
    await expect(dialog.getByRole("radio", { name: "Contact training", exact: true })).toBeChecked();
    await dialog.getByRole("button", { name: "Save changes", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    expect(writes.at(-1)).toMatchObject({ injury_id: injury.id, injury_episode_id: injury.episode_id,
      scopes: ["rehab", "training", "contact"], rehabilitation_permission: { schema_version: 1, level: "sport_specific" } });
  }
  await expect(page.locator('a[href="#today-session"]')).toHaveCount(0);
  if (process.env.REHAB_QA_DIR) await page.screenshot({ path: path.join(process.env.REHAB_QA_DIR, "tracking-only-injuries-375.png"), fullPage: true });
  expect(errors).toEqual([]);
});
