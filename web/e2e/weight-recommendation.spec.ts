import { test, expect } from "@playwright/test";
import fixtures from "./fixtures/rehab-today.json";
import { isolateFromNetwork } from "./support";

for (const width of [390, 1280]) for (const completed of [false, true]) test(`weight recommendation stays consistent in preview and detail at ${width}px (logged: ${completed})`, async ({ page, baseURL }) => {
  await page.setViewportSize({ width, height: 844 });
  await isolateFromNetwork(page, baseURL!);
  await page.clock.setFixedTime(new Date("2026-10-05T12:00:00Z"));
  const { profile } = structuredClone(fixtures.chest);
  const view = structuredClone(fixtures.chest.view);
  const block = { block_id: "squat", exercise_key: "back-squat", block_type: "strength",
    display_name: "Back Squat", sets: 3, reps: 8, effort: { method: "RPE", value: 7 } };
  const session = { session_id: "strength-1", title: "Strength", session_type: "strength_power", blocks: [block] };
  Object.assign(view.active_plan, { phase: "GPP", fight_date: "2027-06-13" });
  Object.assign(view, { open_injuries: [], risk_watch: [],
    live_prescription: { revision: "a".repeat(64), frozen: false, safety_hold: false, changes: [], session } });
  Object.assign(view.today, { training_day: "2026-10-05", recommendation_state: "train_as_planned",
    decision_tier: "green", warnings: [], completion_status: completed ? "started" : "not_started", session_scope: "today",
    session_label: "Today's session", next_session: { ...session, calendar_date: "2026-10-05", session_relation: "today", effective_load: "low" } });
  const id = view.active_plan.id;
  const key = `sb-${new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").hostname.split(".")[0]}-auth-token`;
  await page.addInitScript(({ key, profile }) => localStorage.setItem(key, JSON.stringify({
    access_token: "fixture", refresh_token: "fixture", token_type: "bearer", expires_at: Date.now()/1000 + 86400,
    user: { id: profile.profile.athlete_id, email: "fixture@example.com", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} },
  })), { key, profile });
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = {};
    if (path === "/api/me") body = profile;
    if (path === "/api/today") body = view;
    if (path === `/api/plans/${id}`) body = { plan_id: id, status: "ready", outputs: { structured_plan: {
      weeks: [{ week_index: 1, days: [{ date: "2026-10-05", sessions: [session] }] }],
    } } };
    if (/session-completions|sparring-logs|pending/.test(path)) body = { completions: [], logs: [], response_sets: [] };
    if (path.endsWith("/exercise-changes/additions") || path.endsWith("/exercise-changes/alternatives"))
      body = { scopes: ["occurrence"], alternatives: [{ catalogue_id: "back-squat", exercise_key: "back-squat",
        name: "Back Squat", dose_required: true, dose_mode: "reps", suggested_dose: { sets: 3, reps: 8, rpe: 7 } }] };
    if (path === "/api/history/exercise-library") body = { exercises: [] };
    if (path === "/api/today/exercise-logs") body = { logs: completed ? [{ id: "today", plan_id: id, block_id: "squat", exercise_key: "back-squat",
      status: "as_prescribed", prescribed: block, actual: { sets: 3, reps: 8, load: { value: 110, unit: "kg" } } }] : [], recent_loads: [],
      recent_performances: [{ id: "old", plan_id: "older-plan", exercise_key: "back-squat", status: "as_prescribed",
        prescribed: block, actual: { sets: 3, reps: 8, load: { value: 120, unit: "kg" }, feel: "easy" } }] };
    await route.fulfill({ json: body });
  });
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(`/today?plan=${id}`);
  await expect(page).toHaveTitle(/UNLXCK/i);
  const row = page.locator(".ex-row").filter({ hasText: "Back Squat" }).first();
  if (completed) {
    await expect(row).not.toContainText("Suggested");
  } else {
    await expect(row).toContainText("Suggested 122.5 kg · Last 120 kg");
    await expect(row.getByText(/Suggested/)).toHaveCount(1);
    await expect(row).not.toContainText("Editable");
  }
  await expect(row.locator("input")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await row.scrollIntoViewIfNeeded();
  if (process.env.WEIGHT_QA_DIR) await row.screenshot({ path: `${process.env.WEIGHT_QA_DIR}/weight-preview-${width}${completed ? "-completed" : ""}.png` });
  await row.getByRole("button", { name: "Back Squat", exact: true }).click();
  const detail = page.getByRole("dialog").filter({ hasText: "Back Squat" });
  if (completed) {
    await expect(detail).not.toContainText("Suggested");
  } else {
    await expect(detail).toContainText("Suggested 122.5 kg · Last 120 kg");
    await expect(detail.getByText(/Suggested/)).toHaveCount(1);
    await expect(detail).not.toContainText("Editable");
  }
  await expect(detail.locator('input[aria-label*="Load"]')).toHaveCount(0);
  await detail.scrollIntoViewIfNeeded();
  if (process.env.WEIGHT_QA_DIR) await detail.screenshot({ path: `${process.env.WEIGHT_QA_DIR}/weight-detail-${width}${completed ? "-completed" : ""}.png` });
  if (completed) {
    const actual = detail.getByRole("group", { name: "Today's actual", exact: true });
    await expect(actual).toContainText("110 kg");
    await expect(detail.locator("details").filter({ hasText: "Planned" })).not.toHaveAttribute("open");
    expect(await actual.evaluate(node => {
      const planned = node.parentElement?.querySelector("details");
      return Boolean(planned && (node.compareDocumentPosition(planned) & Node.DOCUMENT_POSITION_FOLLOWING));
    })).toBe(true);
    await expect(actual).not.toContainText("122.5 kg");
    expect(errors).toEqual([]);
    return;
  }
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Add exercise", exact: true }).click();
  const picker = page.getByRole("dialog", { name: "Add exercise" });
  await expect(picker).toContainText("Suggested");
  await expect(picker).toContainText("122.5 kg");
  await expect(picker).toContainText("Last");
  await expect(picker).toContainText("120 kg");
  expect(await picker.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  await picker.getByRole("button", { name: /Back Squat/ }).click();
  await expect(picker.getByRole("spinbutton", { name: "Weight", exact: true })).toBeVisible();
  await expect(picker.getByRole("spinbutton", { name: "Weight", exact: true })).toHaveValue("122.5");
  if (process.env.WEIGHT_QA_DIR) await picker.screenshot({ path: `${process.env.WEIGHT_QA_DIR}/weight-picker-prefilled-${width}.png` });
  await picker.getByRole("spinbutton", { name: "Weight", exact: true }).fill("");
  await expect(picker.getByRole("spinbutton", { name: "Weight", exact: true })).toHaveValue("");
  await expect(picker).toContainText("Last");
  if (process.env.WEIGHT_QA_DIR) await picker.screenshot({ path: `${process.env.WEIGHT_QA_DIR}/weight-picker-${width}.png` });
  await page.keyboard.press("Escape");
  await row.getByRole("button", { name: "Change Back Squat", exact: true }).click();
  await page.getByRole("button", { name: "Swap", exact: true }).click();
  const swap = page.getByRole("dialog", { name: "Swap Back Squat" });
  await expect(swap).toContainText("Suggested");
  await expect(swap).toContainText("122.5 kg");
  await expect(swap).toContainText("Last");
  await expect(swap).toContainText("120 kg");
  expect(errors).toEqual([]);
});
