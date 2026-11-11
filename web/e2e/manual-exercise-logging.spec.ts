import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { isolateFromNetwork } from "./support";

const athlete = "00000000-0000-4000-8000-000000000001";
const day = "2026-10-19";
const shots = process.env.EXERCISE_QA_DIR;
const blank = { sets: null, reps: null, rounds: null, load: null, duration: null, work: null, rest: null, distance: null, effort: null };
type Entry = { id: string; entry_id: string; external_session_id: string; training_day: string; exercise_id: string; name: string; format: string; actual: Record<string, unknown>; notes: string | null; revision: number };
type Session = { id: string; source: "external"; title: string; activity: string; duration_minutes: number; effort: string; status: "done"; training_day: string; plan_id: null; exercise_count: number; takeaway: string };

const catalogue = [
  { catalogue_id: "bank:back-squat", name: "Back Squat", exercise_key: "back-squat", log_format: "strength" },
  { catalogue_id: "bank:rdl", name: "Romanian Deadlift", exercise_key: "romanian-deadlift", log_format: "strength" },
  { catalogue_id: "bank:plank", name: "Plank", exercise_key: "plank", log_format: "isometric" },
].map(entry => ({ ...entry, category: "training", content_type: "strength_power", context: "Strength", focus: [], verified_goals: [], exercise_details: [],
  discovery_match: null, equipment: null, equipment_state: "absent", equipment_requirements: null, media: null, selection_boundary: "x", prescription_allowed: false }));

async function setup(page: Page, baseURL: string, { slowSearch = false, failFirstSave = false } = {}) {
  await isolateFromNetwork(page, baseURL);
  const now = new Date(`${day}T12:00:00Z`);
  await page.clock.setFixedTime(now);
  const key = `sb-${new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").hostname.split(".")[0]}-auth-token`;
  await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [key, JSON.stringify({
    access_token: "manual-exercise-test", refresh_token: "test", token_type: "bearer", expires_at: now.getTime() / 1000 + 86400,
    user: { id: athlete, email: "fighter@example.com", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} },
  })]);
  const sessions: Session[] = [{ id: "earlier", source: "external", title: "Strength", activity: "strength", duration_minutes: 60, effort: "hard",
    status: "done", training_day: "2026-10-12", plan_id: null, exercise_count: 2, takeaway: "" }];
  const entries: Entry[] = [
    { id: "e1", entry_id: "old-1", external_session_id: "earlier", training_day: "2026-10-12", exercise_id: "key:back-squat", name: "Back Squat", format: "strength", actual: { ...blank, sets: 3, reps: 5, load: { value: 75, unit: "kg" } }, notes: null, revision: 1 },
    { id: "e2", entry_id: "old-2", external_session_id: "earlier", training_day: "2026-10-12", exercise_id: "key:plank", name: "Plank", format: "isometric", actual: { ...blank, sets: 3, duration: { value: 40, unit: "seconds" } }, notes: null, revision: 1 },
  ];
  const puts: Record<string, unknown>[] = [];
  let saveAttempts = 0;
  const live = (sessionId: string) => entries.filter(entry => entry.external_session_id === sessionId);
  const library = () => {
    const byId = new Map<string, Entry>();
    for (const entry of [...entries].sort((a, b) => a.training_day.localeCompare(b.training_day))) byId.set(entry.exercise_id, entry);
    return [...byId.values()].map(entry => {
      const loads = entries.filter(item => item.exercise_id === entry.exercise_id && item.actual.load).map(item => ({ ...(item.actual.load as { value: number; unit: string }), training_day: item.training_day }));
      const best = [...new Set(loads.map(load => load.unit))].map(unit => loads.filter(load => load.unit === unit).reduce((a, b) => b.value > a.value ? b : a));
      return { id: entry.exercise_id, name: entry.name, identity_kind: entry.exercise_id.startsWith("custom:") ? "personal" : "canonical", latest_day: entry.training_day,
        latest_values: entry.actual, latest_confirmed_as_prescribed: false, latest_source: "manual", log_format: entry.format,
        best_loads: best.map(load => ({ value: load.value, unit: load.unit, training_day: load.training_day, basis: "reported" })) };
    });
  };
  await page.route("**/api/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/api/me") return json({ profile: { athlete_id: athlete, email: "fighter@example.com", full_name: "Carlos", role: "athlete", access_status: "approved",
      technical_style: [], tactical_style: [], athlete_timezone: "Europe/London", athlete_locale: "en", appearance_mode: "dark", nutrition_profile: {},
      date_of_birth: "1995-01-01", meets_minimum_age: true, is_minor: false, terms_accepted: true, health_consent_granted: true, private_trial_ack_at: `${day}T12:00:00Z`,
      training_habits: { enabled: true, decisions: [] }, training_habits_revision: 0 }, plan_count: 0, latest_plan: null, latest_intake: null, username_rate_limit: {} });
    if (path === "/api/today") return json({ active_plan: { id: "plan-1", name: "Camp", phase: "SPP" }, today: { training_day: day, recommendation_state: "not_checked_in",
      decision_tier: "not_checked_in", warnings: [], next_session: {}, completion_status: "not_started", session_scope: "none", session_label: "No session" },
      open_injuries: [], risk_watch: [], week_summary: {}, quick_actions: [] });
    if (path === "/api/plans/plan-1") return json({ outputs: {} });
    if (path === "/api/plans/active") return json({ detail: "No active plan" }, 404);
    if (path === "/api/plans") return json([]);
    if (path === "/api/today/sparring-logs") return json({ logs: [] });
    if (path === "/api/training/history") return json({ sessions: sessions.map(row => ({ ...row, exercise_count: live(row.id).length })) });
    if (path === "/api/training/sessions" && request.method() === "POST") {
      const body = request.postDataJSON();
      const row: Session = { id: `manual-${sessions.length}`, source: "external", title: body.activity.charAt(0).toUpperCase() + body.activity.slice(1), activity: body.activity,
        duration_minutes: body.duration_minutes, effort: body.effort, status: "done", training_day: day, plan_id: null, exercise_count: 0, takeaway: "" };
      sessions.unshift(row);
      return json(row, 201);
    }
    const exercises = path.match(/^\/api\/training\/sessions\/([^/]+)\/exercises(?:\/([^/]+))?$/);
    if (exercises) {
      const [, sessionId, entryId] = exercises;
      if (request.method() === "GET") return json({ entries: live(sessionId) });
      if (request.method() === "DELETE") { entries.splice(entries.findIndex(entry => entry.entry_id === entryId), 1); return route.fulfill({ status: 204 }); }
      const body = request.postDataJSON();
      puts.push(body);
      if (failFirstSave && ++saveAttempts === 1) return json({ detail: "bad request" }, 400);
      const known = catalogue.find(item => item.catalogue_id === body.exercise.catalogue_id);
      const exerciseId = body.exercise.name ? `custom:${body.exercise.name.toLowerCase()}` : body.exercise.exercise_key ? `key:${body.exercise.exercise_key}`
        : body.exercise.personal_exercise_id ? `custom:${body.exercise.personal_exercise_id}` : `key:${known!.exercise_key}`;
      const name = body.exercise.name ?? known?.name ?? entries.find(item => item.exercise_id === exerciseId)?.name ?? "Exercise";
      const existing = entries.find(entry => entry.entry_id === entryId);
      const row: Entry = { id: existing?.id ?? `row-${entries.length}`, entry_id: entryId, external_session_id: sessionId, training_day: sessions.find(item => item.id === sessionId)!.training_day,
        exercise_id: exerciseId, name, format: body.format, actual: { ...blank, ...body.actual }, notes: body.notes || null, revision: body.revision + 1 };
      if ((existing?.revision ?? 0) !== body.revision) return json({ detail: "stale" }, 409);
      if (existing) Object.assign(existing, row); else entries.push(row);
      return json(row);
    }
    if (path.startsWith("/api/training/history/external/")) {
      const row = sessions.find(item => item.id === path.split("/").at(-1))!;
      return json({ session: { ...row, exercise_count: live(row.id).length }, prescription_source: "manual",
        blocks: live(row.id).map(entry => ({ name: entry.name, kind: "exercise", source: "manual", format: entry.format, prescribed: {}, actual: entry.actual, status: null, notes: entry.notes, responses: [] })) });
    }
    if (path === "/api/history/exercise-library") return json({ exercises: library(), as_of: `${day}T12:00:00Z`, coverage: "retained_logs" });
    if (path === "/api/history/exercise-performances") {
      const id = url.searchParams.get("exercise");
      return json({ performances: entries.filter(entry => entry.exercise_id === id).sort((a, b) => b.training_day.localeCompare(a.training_day)).map(entry => ({ id: entry.id, training_day: entry.training_day,
        block: { name: entry.name, kind: "exercise", source: "manual", format: entry.format, prescribed: {}, actual: entry.actual, status: null, notes: null, responses: [] }, performed_values: entry.actual })), next_cursor: null });
    }
    if (path === "/api/training/library") {
      if (slowSearch) await new Promise(resolve => setTimeout(resolve, 1500));
      const q = (url.searchParams.get("q") ?? "").toLowerCase();
      expect(url.searchParams.get("for_log")).toBe("true");
      const matches = catalogue.filter(item => item.name.toLowerCase().includes(q));
      return json({ entries: matches, total: matches.length, catalogue_total: 3, offset: 0, limit: 6, next_offset: null, revision: "r", facets: {} });
    }
    return json({ notifications: [], unread_count: 0, level: 1, total_xp: 0, logs: [] });
  });
  return { puts, entries };
}

const shoot = async (page: Page, name: string, width: number) => { if (shots) await page.screenshot({ path: `${shots}/${name}-${width}.png` }); };

for (const width of [390, 1280]) {
  test(`log training fast, then optionally add exercises (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
    const { puts } = await setup(page, baseURL!);
    await page.goto("/plan");
    await page.getByRole("button", { name: "Log session", exact: true }).click();
    const form = page.getByRole("form", { name: "Log session", exact: true });
    // The existing three steps still save immediately.
    await form.getByRole("group", { name: "Activity", exact: true }).getByRole("button", { name: "Strength", exact: true }).click();
    await form.getByRole("group", { name: "Duration", exact: true }).getByRole("button", { name: "60", exact: true }).click();
    await form.getByRole("group", { name: "Effort", exact: true }).getByRole("button", { name: "Hard", exact: true }).click();
    await form.getByRole("button", { name: "Save Session" }).click();
    await expect(page.getByText("Session saved")).toBeVisible();
    const offer = page.getByRole("button", { name: /Add exercises/ });
    await expect(offer).toBeVisible();
    await shoot(page, "01-saved-offer", width);
    await offer.click();

    const sheet = page.getByRole("dialog", { name: "Add exercises" });
    const search = sheet.getByRole("searchbox", { name: "Exercise" });
    await expect(search).toBeFocused();
    await expect(sheet.getByRole("group", { name: "Recent exercises" }).getByRole("button")).toHaveText(["Back Squat", "Plank"]);
    await shoot(page, "02-empty-logger", width);
    expect((await new AxeBuilder({ page }).include("dialog").analyze()).violations).toEqual([]);

    await search.fill("squat");
    await expect(sheet.getByRole("list", { name: "Matching exercises" }).getByRole("button")).toHaveCount(2);
    await expect(sheet.getByRole("button", { name: /Add “squat”.*only you see it/ })).toBeVisible();
    await expect(sheet.getByRole("button", { name: /Back Squat.*Yours/ })).toBeVisible();
    await shoot(page, "03-search", width);
    await sheet.getByRole("button", { name: /Back Squat.*Yours/ }).click();
    await expect(sheet.getByText("Last 75 kg")).toBeVisible();
    await expect(sheet.getByLabel("Sets", { exact: true })).toBeFocused();
    await expect(sheet.getByLabel("Load", { exact: true })).toHaveValue("75");
    await expect(sheet).not.toContainText("Suggested");
    await expect(sheet).toContainText("Last 75 kg");
    expect(puts).toHaveLength(0);
    await sheet.getByLabel("Sets", { exact: true }).fill("3");
    await expect(sheet.getByRole("button", { name: "Dismiss weight unit tip" })).toBeVisible();
    await sheet.getByLabel("Reps", { exact: true }).fill("5");
    await sheet.getByLabel("Load", { exact: true }).fill("80");
    await expect(sheet.getByRole("button", { name: "Dismiss individual sets tip" })).toHaveCount(0);
    await shoot(page, "04-selected", width);
    await page.keyboard.press("Enter");
    const rows = sheet.getByRole("list", { name: "Exercises in this session" }).getByRole("listitem");
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText("Back Squat3 × 5 · 80 kg");
    await expect(search).toBeFocused();
    await expect(sheet.getByRole("button", { name: "Dismiss saved exercise edit tip" })).toBeVisible();
    await shoot(page, "05-one-added", width);

    // A library movement with a known format, then optional details.
    await search.fill("Romanian");
    await sheet.getByRole("button", { name: /Romanian Deadlift.*Library/ }).click();
    await sheet.getByLabel("Sets", { exact: true }).fill("3");
    await sheet.getByLabel("Reps", { exact: true }).fill("8");
    await sheet.getByLabel("Load", { exact: true }).fill("70");
    await sheet.getByText("More details").click();
    await sheet.getByLabel("RPE", { exact: true }).fill("8");
    await sheet.getByLabel("Rest", { exact: true }).fill("90");
    await shoot(page, "06-more-details", width);
    expect((await new AxeBuilder({ page }).include("dialog").analyze()).violations).toEqual([]);
    await sheet.getByRole("button", { name: "Add exercise" }).click();
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(1)).toContainText("3 × 8 · 70 kg · 90 sec rest · RPE 8");

    // Plank auto-selects the hold layout; a new name asks for one.
    await search.fill("Plank");
    await sheet.getByRole("button", { name: /^Plank.*Yours/ }).click();
    await expect(sheet.getByRole("button", { name: /Currently Hold/ })).toBeVisible();
    await sheet.getByLabel("Sets", { exact: true }).fill("3");
    await sheet.getByLabel("Hold", { exact: true }).fill("45");
    await sheet.getByRole("button", { name: "Add exercise" }).click();
    await search.fill("Landmine rotation");
    await sheet.getByRole("button", { name: /Add “Landmine rotation”/ }).click();
    await sheet.getByRole("group", { name: "How to record it" }).getByRole("button", { name: "Bodyweight" }).click();
    await sheet.getByLabel("Sets", { exact: true }).fill("0");
    await sheet.getByRole("button", { name: "Add exercise" }).click();
    await expect(sheet.getByText("At least 1")).toBeVisible();
    await shoot(page, "07-validation", width);
    await sheet.getByLabel("Sets", { exact: true }).fill("");
    // Done keeps the name-only exercise rather than dropping it.
    await sheet.getByRole("button", { name: "Done" }).click();
    await expect(sheet).toHaveCount(0);
    expect(puts.at(-1)).toEqual({ exercise: { name: "Landmine rotation" }, format: "bodyweight", actual: {}, notes: "", revision: 0 });
    await expect(page.getByRole("button", { name: /4 exercises added/ })).toBeVisible();
    await shoot(page, "08-saved-with-exercises", width);
    expect(errors).toEqual([]);
  });

  test(`History: add later, Session Details and Exercise Library (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
    const { puts } = await setup(page, baseURL!, { slowSearch: true, failFirstSave: true });
    await page.goto("/history");
    await expect(page.locator(".history-session-row").first()).toContainText("2 exercises");
    await page.getByRole("button", { name: /View Strength details/ }).click();
    const details = page.getByRole("dialog", { name: "Strength" });
    await expect(details.locator("ol li")).toHaveText(["Back Squat3 × 5 · 75 kg", "Plank3 × 40 sec"]);
    await expect(details).not.toContainText("Prescribed");
    await shoot(page, "09-session-details", width);
    await details.getByRole("button", { name: "Edit exercises" }).click();
    await expect(details).toHaveCount(0);
    const sheet = page.getByRole("dialog", { name: "Add exercises" });
    await expect(sheet.getByRole("list", { name: "Exercises in this session" }).getByRole("listitem")).toHaveCount(2);
    await sheet.getByRole("searchbox", { name: "Exercise" }).fill("front");
    await expect(sheet.getByText("Searching library…")).toBeVisible();
    await shoot(page, "10-loading", width);
    await sheet.getByRole("button", { name: /Add “front”/ }).waitFor();
    await sheet.getByRole("searchbox", { name: "Exercise" }).fill("Back");
    await sheet.getByRole("button", { name: /Back Squat.*Yours/ }).click();
    await sheet.getByLabel("Sets", { exact: true }).fill("3");
    await sheet.getByLabel("Reps", { exact: true }).fill("3");
    await sheet.getByLabel("Load", { exact: true }).fill("90");
    await sheet.getByRole("button", { name: "Add exercise" }).click();
    await expect(sheet.getByRole("alert")).toContainText("Not saved");
    await shoot(page, "11-save-error", width);
    await sheet.getByRole("button", { name: "Add exercise" }).click();
    await expect(sheet.getByRole("list", { name: "Exercises in this session" }).getByRole("listitem")).toHaveCount(3);
    expect(puts).toHaveLength(2);
    await shoot(page, "12-multiple", width);
    await sheet.getByRole("button", { name: "Done" }).click();
    // Session Details reopens with the new exercise.
    const reopened = page.getByRole("dialog", { name: "Strength" });
    await expect(reopened.locator("ol li")).toHaveCount(3);
    await reopened.getByRole("button", { name: "Close session details" }).click();
    await expect(page.getByText("3 exercises")).toBeVisible();

    await page.getByRole("tab", { name: "Exercises", exact: true }).click();
    const squat = page.getByRole("button", { name: "View Back Squat performance history", exact: true });
    await expect(squat).toContainText("90 kg");
    await shoot(page, "13-exercise-library", width);
    await squat.click();
    const record = page.getByRole("dialog", { name: "Back Squat" });
    await expect(record).toContainText("Saved results from your plans and the training you logged");
    await expect(record.getByRole("region", { name: "Exercise performance record", exact: true })).toContainText("90 kg");
    await shoot(page, "14-exercise-progress", width);
    expect((await new AxeBuilder({ page }).include("dialog").analyze()).violations).toEqual([]);
    expect(errors).toEqual([]);
  });
}

for (const width of [390, 1280]) {
  test(`different loads per set stay compact and reopen in history (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
    const { puts } = await setup(page, baseURL!);
    await page.goto("/history");
    await expect(page).toHaveTitle(/UNLXCK/i);
    await page.getByRole("button", { name: /View Strength details/ }).click();
    await page.getByRole("button", { name: "Edit exercises", exact: true }).click();
    const sheet = page.getByRole("dialog", { name: "Add exercises" });
    await sheet.getByRole("button", { name: "Edit Back Squat", exact: true }).click();
    await sheet.getByRole("button", { name: "Edit sets individually" }).click();
    await expect(sheet.getByLabel("Load kg, set 1", { exact: true })).toHaveValue("75");
    await sheet.getByLabel("Load kg, set 2", { exact: true }).fill("80");
    await expect(sheet.getByLabel("Load kg, set 1", { exact: true })).toHaveValue("75");
    await expect(sheet.getByLabel("Load kg, set 3", { exact: true })).toHaveValue("75");
    await sheet.getByRole("button", { name: "Apply to remaining sets" }).click();
    await expect(sheet.getByLabel("Load kg, set 3", { exact: true })).toHaveValue("80");
    await expect(sheet.getByLabel("RPE, set 1")).toHaveValue("");
    await sheet.getByLabel("RPE, set 1").fill("7");
    await sheet.getByLabel("RPE, set 2").fill("8.5");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect((await new AxeBuilder({ page }).include("dialog").analyze()).violations).toEqual([]);
    await shoot(page, "per-set-editor", width);
    await sheet.getByRole("button", { name: "Save changes", exact: true }).click();
    const actual = puts.at(-1)!.actual as { set_entries: { load: { value: number } }[] };
    expect(actual.set_entries.map(row => row.load.value)).toEqual([75, 80, 80]);
    expect((actual.set_entries as { rpe?: number }[]).map(row => row.rpe ?? null)).toEqual([7, 8.5, null]);
    await sheet.getByRole("button", { name: "Done", exact: true }).click();
    const details = page.getByRole("dialog", { name: "Strength", exact: true });
    const results = details.locator("details").filter({ hasText: "3 × 5 · 75–80 kg" });
    await expect(results).toBeVisible();
    await results.locator("summary").click();
    await expect(results.getByRole("listitem")).toHaveCount(3);
    await expect(results.getByRole("listitem").nth(1)).toContainText("80");
    const cells = results.getByRole("listitem").first().locator(":scope > span");
    const boxes = await Promise.all((await cells.all()).map(cell => cell.boundingBox()));
    const centers = boxes.map(box => box!.y + box!.height / 2);
    expect(Math.abs(centers[0] - centers[1])).toBeLessThan(2);
    expect(Math.abs(centers[1] - centers[2])).toBeLessThan(2);
    expect((await results.getByRole("listitem").first().boundingBox())!.height).toBeLessThanOrEqual(44);
    await shoot(page, "per-set-history", width);
    expect(errors).toEqual([]);
  });
}

for (const width of [390, 1280]) test(`historical Back Squat → editable manual suggestion → confirmation → next exposure (${width}px)`, async ({ page, baseURL }) => {
  await page.setViewportSize({ width, height: 900 });
  const { puts } = await setup(page, baseURL!);
  async function openEntry() {
    await page.goto("/plan");
    await page.getByRole("button", { name: "Log session", exact: true }).click();
    const form = page.getByRole("form", { name: "Log session", exact: true });
    await form.getByRole("group", { name: "Activity", exact: true }).getByRole("button", { name: "Strength", exact: true }).click();
    await form.getByRole("group", { name: "Duration", exact: true }).getByRole("button", { name: "60", exact: true }).click();
    await form.getByRole("group", { name: "Effort", exact: true }).getByRole("button", { name: "Hard", exact: true }).click();
    await form.getByRole("button", { name: "Save Session" }).click();
    await page.getByRole("button", { name: /Add exercises/ }).click();
    const sheet = page.getByRole("dialog", { name: "Add exercises", exact: true });
    await sheet.getByRole("button", { name: "Back Squat", exact: true }).click();
    return sheet;
  }
  let entry = await openEntry();
  await expect(entry).not.toContainText("Suggested");
  await expect(entry).toContainText("Last 75 kg");
  await expect(entry.getByLabel("Load", { exact: true })).toHaveValue("75");
  expect(puts).toHaveLength(0);
  await entry.getByLabel("Sets", { exact: true }).fill("3");
  await entry.getByLabel("Reps", { exact: true }).fill("8");
  await entry.getByLabel("Load", { exact: true }).fill("80");
  expect(puts).toHaveLength(0);
  await entry.getByRole("button", { name: "Add exercise", exact: true }).click();
  await expect(entry.getByRole("list", { name: "Exercises in this session" })).toContainText("80 kg");
  expect((puts[0].actual as { load: { value: number } }).load.value).toBe(80);
  await entry.getByRole("button", { name: "Done", exact: true }).click();
  entry = await openEntry();
  await expect(entry.getByLabel("Load", { exact: true })).toHaveValue("80");
  await expect(entry).toContainText("Last 80 kg");
  await entry.getByLabel("Load", { exact: true }).fill("");
  await entry.getByRole("button", { name: "Add exercise", exact: true }).click();
  expect(puts[1].actual).toEqual({});
});
