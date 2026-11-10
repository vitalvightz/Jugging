import { test, expect } from "@playwright/test";
import type { TodayCommandView } from "../lib/types";
import fixtures from "./fixtures/rehab-today.json";
import { isolateFromNetwork } from "./support";

for (const training of [false, true]) for (const width of [320, 390, 1280]) test(`programme arrival ${training ? "training" : "rehab"} stays compact at ${width}px`, async ({page, baseURL}) => {
  await page.setViewportSize({width, height: 844});
  await isolateFromNetwork(page, baseURL!);
  const {profile} = structuredClone(fixtures.chest);
  const view = structuredClone(fixtures.chest.view) as TodayCommandView;
  if (training) {
    view.active_plan!.fight_date = "2026-11-16";
    view.open_injuries = []; view.risk_watch = []; view.live_prescription = null;
    view.today = {training_day: "2026-10-05", recommendation_state: "train_as_planned", decision_tier: "green",
      warnings: [], completion_status: "not_started", session_scope: "today", session_label: "Today's session",
      next_session: {session_id: "strength-1", title: "Strength", calendar_date: "2026-10-05", session_relation: "today", effective_load: "low"}};
  }
  const id = view.active_plan.id;
  const key = "sb-stub-auth-token";
  await page.addInitScript(({key, id, profile}) => {
    localStorage.setItem(key, JSON.stringify({access_token: "fixture", refresh_token: "fixture", token_type: "bearer",
      expires_at: Date.now()/1000 + 86400, user: {id: profile.profile.athlete_id, email: "fixture@example.com", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {}}}));
    // Model the same-document generation -> Today navigation once only.
    if (!sessionStorage.getItem("fixture-offered")) {
      sessionStorage.setItem("fixture-offered", "1");
      sessionStorage.setItem("unlxck:programme-arrival", JSON.stringify({planId: id, at: Date.now(), document: performance.timeOrigin}));
    }
  }, {key, id, profile});
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = {};
    if (path === "/api/me") body = profile;
    if (path === "/api/today") body = view;
    if (path === `/api/plans/${id}`) body = {plan_id: id, status: "ready", outputs: {structured_plan: {
      athlete_context: {sport_profile: "Boxing", experience_level: "Intermediate"},
      weeks: Array.from({length: training ? 6 : 1}, (_, index) => ({week_index: index + 1,
        days: [{date: new Date(Date.parse(`${view.today.training_day}T12:00:00Z`) + index * 7 * 86400000).toISOString().slice(0, 10),
          sessions: [view.live_prescription?.session ?? view.today.next_session]}]})),
    }}};
    if (path === "/api/training/history") body = {sessions: []};
    if (/session-completions|sparring-logs|exercise-logs|pending/.test(path)) body = {completions: [], logs: [], response_sets: []};
    await route.fulfill({json: body});
  });
  const errors: string[] = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.goto(`/today?plan=${id}`);
  const summary = page.locator('[class*="programmeArrival"]');
  await expect(summary).toBeVisible();
  await expect(summary).toContainText(training ? "6 weeks. Built for Boxing." : "1 week of training. Built for Boxing.");
  expect((await summary.boundingBox())!.height).toBeLessThan(150);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expect(page.getByRole("link", {name: "View Full Plan", exact: true})).toHaveAttribute("href", `/plans/${id}`);
  await expect(page.getByRole("link", {name: "View plan", exact: true})).toHaveCount(0);
  await page.screenshot({path: `/tmp/programme-arrival-${training ? "six-week-camp" : "rehab"}-${width}.png`});
  if (training) {
    const jump = page.getByRole("link", {name: "Go to Session", exact: true});
    await expect(jump).toBeVisible();
    await jump.click();
    const action = page.locator("#today-session .today-session-actions button.cta").first();
    await expect(action).toBeFocused();
    await expect(action).toBeInViewport();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(action).toContainText("Start");
  } else await expect(page.getByRole("link", {name: "Go to Session", exact: true})).toHaveCount(0);
  await page.getByRole("link", {name: "View Full Plan", exact: true}).click();
  await expect(page).toHaveURL(new RegExp(`/plans/${id}$`));
  await page.goto(`/today?plan=${id}`);
  await expect(page.locator("h1")).toHaveText("Today");
  await expect(summary).toHaveCount(0);
  await page.reload();
  await expect(summary).toHaveCount(0);
  await expect(page.locator("h1")).toHaveText("Today");
  expect(errors).toEqual([]);
});
