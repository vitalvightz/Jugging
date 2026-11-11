import { test, expect } from "@playwright/test";
import fixtures from "./fixtures/rehab-today.json";
import { isolateFromNetwork } from "./support";
import { buildTimerItems } from "../lib/session-timer/plan";
import { createTimerState, startItem } from "../lib/session-timer/engine";

for (const width of [320, 390, 1280]) test(`historical Back Squat → preview → planned timer → confirmed sets → review → next exposure (${width}px)`, async ({ page, baseURL }) => {
  await page.setViewportSize({ width, height: width < 400 ? 844 : 900 });
  await isolateFromNetwork(page, baseURL!);
  let now = Date.parse("2026-10-05T12:00:00Z");
  await page.clock.setFixedTime(new Date(now));
  const { profile } = structuredClone(fixtures.chest);
  const view = structuredClone(fixtures.chest.view);
  const block = { block_id: "squat", exercise_key: "back-squat", block_type: "strength",
    display_name: "Back Squat", sets: 3, reps: 8 };
  const session = { session_id: "strength-1", title: "Strength", session_type: "strength_power", blocks: [block] };
  Object.assign(view.active_plan, { phase: "GPP", fight_date: "2027-06-13" });
  Object.assign(view, { open_injuries: [], risk_watch: [],
    live_prescription: { revision: "a".repeat(64), frozen: false, safety_hold: false, changes: [], session } });
  Object.assign(view.today, { training_day: "2026-10-05", recommendation_state: "train_as_planned",
    decision_tier: "green", warnings: [], completion_status: "started", session_scope: "today",
    session_label: "Today's session", next_session: { ...session, calendar_date: "2026-10-05", session_relation: "today", effective_load: "low" } });
  const id = view.active_plan.id;
  const items = buildTimerItems([session]);
  const run = startItem(createTimerState(items), now, { prepSec: 0 }).state;
  const runKey = (day: string) => `unlxck.session-timer.run:${id}:strength-1:${day}`;
  const savedRun = JSON.stringify({ version: 2, key: runKey(view.today.training_day),
    itemIds: items.map(item => `${item.kind}:${item.id}`), state: run });
  const key = `sb-${new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").hostname.split(".")[0]}-auth-token`;
  await page.addInitScript(({ key, profile, savedRun, runKey }) => {
    localStorage.setItem(key, JSON.stringify({ access_token: "fixture", refresh_token: "fixture", token_type: "bearer",
      expires_at: Date.now()/1000 + 86400, user: { id: profile.profile.athlete_id, email: "fixture@example.com",
        aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} } }));
    if (!sessionStorage.getItem("weight-run-seeded")) {
      sessionStorage.setItem("weight-run-seeded", "1");
      localStorage.setItem(runKey, savedRun);
    }
  }, { key, profile, savedRun, runKey: runKey(view.today.training_day) });
  let history: unknown[] = [];
  let sessionSaved = false;
  const batches: { entries: { block_id: string; actual: { set_entries: { load: { value: number } }[] } }[] }[] = [];
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = {};
    if (path === "/api/me") body = profile;
    if (path === "/api/today") body = view;
    if (path === `/api/plans/${id}`) body = { plan_id: id, status: "ready", outputs: { structured_plan: {
      weeks: [{ week_index: 1, days: [{ date: view.today.training_day, sessions: [session] }] }],
    } } };
    if (/session-completions|sparring-logs|pending/.test(path)) body = { completions: [], logs: [], response_sets: [] };
    if (path === "/api/history/exercise-library") body = { exercises: [{
      id: "key:back-squat", name: "Back Squat", identity_kind: "canonical", latest_day: "2026-10-04",
      latest_source: "manual", latest_confirmed_as_prescribed: false, best_loads: [], log_format: "strength",
      latest_values: { sets: 3, set_entries: [115, 120, 125].map(value => ({ reps: 8, load: { value, unit: "kg" } })) },
    }] };
    if (path === "/api/today/session-completion" && route.request().method() === "POST") {
      sessionSaved = true;
      view.today.completion_status = "done";
      body = { completion: { ...route.request().postDataJSON(), id: "complete" },
        completion_status: "done", landing_session_state: "completed" };
    }
    if (path === "/api/today/exercise-logs") {
      if (route.request().method() === "POST") {
        const batch = route.request().postDataJSON();
        batches.push(batch);
        history = batch.entries.map((entry: object) => ({ ...entry, id: "confirmed", training_day: "2026-10-05", exercise_key: "back-squat", prescribed: block }));
        body = { logs: history };
      } else body = { logs: [], recent_loads: [], recent_performances: history };
    }
    await route.fulfill({ json: body });
  });
  await page.goto(`/today?plan=${id}`);
  await expect(page.locator(".ex-row").filter({ hasText: "Back Squat" })).toContainText("Same as last");
  await page.locator(".st-mini").click();
  const timer = page.locator(".st-root");
  for (const [index, expected] of [115, 120, 125].entries()) {
    await timer.getByRole("button", { name: `Set ${index + 1} done`, exact: true }).click();
    const field = timer.getByLabel(`Load kg, set ${index + 1}`, { exact: true });
    await expect(field).toHaveValue(String(expected));
    await expect(timer.locator(".st-rest-effort")).not.toHaveAttribute("open");
    await expect(timer.getByLabel(`RPE, set ${index + 1}`, { exact: true })).not.toBeVisible();
    if (index === 0) {
      await expect(timer.locator(".st-discovery-hint")).toHaveText(/Check weight and reps/);
      await expect(timer.locator(".st-hint")).toHaveCount(0);
      await expect(timer.locator(".st-clock-digits")).toBeVisible();
      expect(await timer.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
      if (process.env.WEIGHT_QA_DIR) await timer.screenshot({ path: `${process.env.WEIGHT_QA_DIR}/between-sets-${width}.png` });
      await timer.locator(".st-rest-effort summary").click();
      await expect(timer.locator(".st-discovery-hint")).toHaveText(/RPE is how hard this set felt/);
      if (process.env.WEIGHT_QA_DIR) await timer.screenshot({ path: `${process.env.WEIGHT_QA_DIR}/rpe-help-${width}.png`, animations: "disabled" });
      const before = await timer.getByRole("button", {name: /Confirm set/}).boundingBox();
      await timer.getByLabel("RPE, set 1", { exact: true }).fill("7.5");
      await expect(timer.locator(".st-discovery-hint")).toHaveCount(0);
      const after = await timer.getByRole("button", {name: /Confirm set/}).boundingBox();
      expect(Math.abs(after!.y - before!.y)).toBeLessThan(2);
    }
    if (index === 1) {
      await expect(timer.locator(".st-discovery-hint")).toHaveCount(0);
      await field.fill("110");
    }
    expect(batches).toHaveLength(0);
    await timer.getByRole("button", { name: /Confirm set/ }).click();
    if (index < 2) {
      now += 180_000;
      await page.clock.setFixedTime(new Date(now));
      await timer.getByRole("button", { name: `Start set ${index + 2}`, exact: true }).click();
    }
  }
  await expect(page.locator(".today-review-root")).toBeVisible();
  expect(batches).toHaveLength(1);
  expect(batches[0].entries[0].actual.set_entries.map(row => row.load.value)).toEqual([115, 110, 125]);
  expect((batches[0].entries[0].actual.set_entries[0] as { rpe?: number }).rpe).toBe(7.5);
  const review = page.locator(".today-review-root");
  await expect(review).toContainText("110–125 kg");
  await expect(review.locator(".today-review-discovery")).toHaveText(/Your results guide next time/);
  await expect(review.locator(".today-review-hint")).toHaveCount(0);
  await expect(review.getByText("How hard was the whole session?", {exact: true})).toBeVisible();
  if (process.env.WEIGHT_QA_DIR) await review.screenshot({ path: `${process.env.WEIGHT_QA_DIR}/review-help-${width}.png`, animations: "disabled" });
  await review.getByRole("button", { name: "Back Squat", exact: true }).click();
  const detail = page.getByRole("dialog", { name: "Back Squat", exact: true });
  await expect(detail.getByRole("group", { name: "Today's actual" })).toContainText("115");
  await detail.getByRole("button", { name: "Close", exact: true }).click();
  await expect(detail).toHaveCount(0);
  await review.getByRole("slider", { name: "Session effort" }).press("ArrowRight");
  await review.getByRole("radio", { name: "None", exact: true }).click();
  await review.getByRole("button", { name: "Save session", exact: true }).click();
  await expect(review).toHaveCount(0);
  expect(sessionSaved).toBe(true);
  expect(await page.evaluate(() => localStorage.getItem("discovery-session-review-seen"))).toBe("1");
  view.today.completion_status = "started";
  view.today.training_day = "2026-10-06";
  view.today.next_session.calendar_date = "2026-10-06";
  const next = { version: 2, key: runKey(view.today.training_day), itemIds: items.map(item => `${item.kind}:${item.id}`),
    state: startItem(createTimerState(items), now, { prepSec: 0 }).state };
  await page.evaluate(({ key, value }) => localStorage.setItem(key, value),
    { key: runKey(view.today.training_day), value: JSON.stringify(next) });
  await page.reload();
  await page.locator(".st-mini").click();
  await page.locator(".st-root").getByRole("button", { name: "Set 1 done", exact: true }).click();
  await expect(page.locator(".st-root").getByLabel("Load kg, set 1", { exact: true })).toHaveValue("115");
  expect(batches).toHaveLength(1);
  await page.locator(".st-root").getByRole("button", { name: /Confirm set/ }).click();
  now += 180_000;
  await page.clock.setFixedTime(new Date(now));
  await page.locator(".st-root").getByRole("button", { name: "Start set 2", exact: true }).click();
  await page.locator(".st-root").getByRole("button", { name: "Set 2 done", exact: true }).click();
  await expect(page.locator(".st-root").getByLabel("Load kg, set 2", { exact: true })).toHaveValue("110");
  expect(batches).toHaveLength(1);
  await page.goto("/timer");
  await expect(page.getByRole("button", { name: "Start round 1", exact: true })).toBeVisible();
  await expect(page.getByText("Suggested", { exact: true })).toHaveCount(0);
  await expect(page.locator(".st-rest-fields")).toHaveCount(0);
});
