import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { isolateFromNetwork } from "./support";

const day = "2026-10-19";
const rows = [
  { id: "strength", source: "unlxck", history_editable: true, completion_origin: "reported", title: "Strength & Power", status: "modified", training_day: day, plan_id: "old", session_rpe: 7 },
  { id: "rehab", source: "unlxck", title: "Today's rehab", status: "done", training_day: day },
  { id: "manual", source: "external", title: "Boxing", activity: "boxing", duration_minutes: 40, effort: "hard", status: "done", training_day: day },
  { id: "legacy", source: "unlxck", title: "Legacy strength", status: "done", training_day: day },
];
const blocks = [
  { name: "Back squat", kind: "exercise", prescribed: { sets: 4, reps: 6, load: { display: "80 kg" }, rest: { value: 120, unit: "seconds" } }, actual: { sets: 3, reps: 5, load: { value: 75, unit: "kg" }, rest: { value: 150, unit: "seconds" } }, status: "modified", responses: [] },
  { name: "Plank", kind: "exercise", prescribed: { sets: 3, duration: { value: 45, unit: "seconds" } }, actual: {}, status: "as_prescribed", responses: [] },
  { name: "Bike intervals", kind: "exercise", prescribed: { rounds: 5, work: { value: 30, unit: "seconds" }, rest: { value: 90, unit: "seconds" } }, actual: {}, status: "skipped", responses: [] },
];

async function setup(page: Page, baseURL: string) {
  await isolateFromNetwork(page, baseURL);
  const now = new Date(`${day}T12:00:00Z`);
  await page.clock.setFixedTime(now);
  const key = `sb-${new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").hostname.split(".")[0]}-auth-token`;
  await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [key, JSON.stringify({
    access_token: "history-test", refresh_token: "test", token_type: "bearer", expires_at: now.getTime() / 1000 + 86400,
    user: { id: "00000000-0000-4000-8000-000000000001", email: "fighter@example.com", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} },
  })]);
  const requests: string[] = [];
  let fail = false;
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/api/me") return json({ profile: { athlete_id: "00000000-0000-4000-8000-000000000001", email: "fighter@example.com", full_name: "Carlos", role: "athlete", access_status: "approved", technical_style: ["boxing"], tactical_style: [], athlete_timezone: "Europe/London", athlete_locale: "en", appearance_mode: "dark", nutrition_profile: {}, date_of_birth: "1995-01-01", meets_minimum_age: true, is_minor: false, terms_accepted: true, health_consent_granted: true, private_trial_ack_at: `${day}T12:00:00Z` }, plan_count: 0, latest_plan: null, latest_intake: null, username_rate_limit: {} });
    if (path === "/api/training/history") return json({ sessions: rows });
    if (path.startsWith("/api/training/history/")) {
      expect(route.request().method()).toBe("GET");
      requests.push(path);
      if (fail) { fail = false; return json({ detail: "unavailable" }, 503); }
      const id = path.split("/").at(-1);
      return json({ session: rows.find(row => row.id === id), prescription_source: id === "manual" ? "manual" : "snapshot",
        blocks: id === "strength" ? blocks : id === "rehab" ? [
          { name: "Calf raise", kind: "rehab", prescribed: { sets: 3, reps: 10 }, actual: {}, status: "done_as_shown", completed_dose: { sets: 3, reps: 10, completion_state: "quantified" }, responses: [] },
          { name: "Ankle hold", kind: "rehab", prescribed: { duration: { value: 30, unit: "seconds" } }, actual: {}, status: "stopped", completed_dose: { completion_state: "partial_amount_unknown" }, responses: [] },
        ] : [], message: id === "legacy" ? "Detailed exercise records weren't captured for this session." : null });
    }
    if (path === "/api/plans/active") return json({ detail: "No active plan" }, 404);
    if (path === "/api/plans") return json([]);
    return json({ notifications: [], unread_count: 0, level: 1, total_xp: 0, logs: [] });
  });
  return { requests, failNext: () => { fail = true; } };
}

for (const width of [390, 1280]) {
  test(`History session sheet: evidence, focus, cache and actions at ${width}px`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const { requests, failNext } = await setup(page, baseURL!);
    const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
    await page.goto("/history");
    const trigger = page.getByRole("button", { name: /View Strength & Power details/ });
    await expect(trigger).toBeVisible();
    expect(requests).toHaveLength(0);
    if (process.env.HISTORY_QA_DIR) await page.screenshot({ path: `${process.env.HISTORY_QA_DIR}/history-${width}.png` });
    await trigger.focus(); await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: "Strength & Power" });
    await expect(dialog.getByRole("heading", { name: "Back squat" })).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Back squat", exact: true })).toHaveAttribute("aria-expanded", "false");
    await dialog.getByRole("button", { name: "Back squat", exact: true }).click();
    await expect(dialog.getByRole("region", { name: "Performance: Back squat" })).toContainText("75 kg");
    // Completed-as-prescribed blocks start collapsed and show no duplicate reported section.
    const plank = dialog.getByRole("button", { name: "Plank", exact: true });
    await expect(plank).toHaveAttribute("aria-expanded", "false"); await expect(dialog).toContainText("3 sets · 45 sec");
    await plank.click(); await expect(plank).toHaveAttribute("aria-expanded", "true");
    await expect(dialog).toContainText("Completed as prescribed"); await expect(dialog.getByRole("region", { name: "Performance: Plank" })).toHaveCount(0);
    await dialog.getByRole("button", { name: "Bike intervals", exact: true }).click();
    await expect(dialog).toContainText("Skipped. No performance values logged.");
    await plank.click(); await dialog.getByRole("button", { name: "Close session details" }).focus();
    await expect(dialog.getByRole("button", { name: "Close session details" })).toBeFocused();
    await page.keyboard.press("Tab");
    expect(await dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
    const box = await dialog.boundingBox(); expect(box!.width).toBeLessThanOrEqual(width); expect(box!.x).toBeGreaterThanOrEqual(0);
    await expect(dialog.locator("header")).toContainText("RPE 7/10 · UNLXCK");
    await expect(dialog.getByRole("region", { name: "Session overview" })).toHaveCount(0);
    expect((await new AxeBuilder({ page }).include("dialog").analyze()).violations).toEqual([]);
    await page.keyboard.press("Escape"); await expect(dialog).toHaveCount(0); await expect(trigger).toBeFocused();
    await trigger.click(); await expect(dialog.getByText("Back squat", { exact: true })).toBeVisible();
    if (process.env.HISTORY_QA_DIR) await page.screenshot({ path: `${process.env.HISTORY_QA_DIR}/session-details-${width}.png` });
    expect(requests).toHaveLength(1);
    await dialog.getByRole("button", { name: "Close session details" }).click();
    await page.getByRole("button", { name: /View Today's rehab details/ }).click();
    const rehab = page.getByRole("dialog", { name: "Today's rehab" });
    await rehab.getByRole("button", { name: "Ankle hold", exact: true }).click();
    await expect(rehab).toContainText("Completed amount unknown.");
    await rehab.getByRole("button", { name: "Calf raise", exact: true }).click();
    await expect(rehab.getByRole("region", { name: "Performance: Calf raise" })).toContainText("10");
    await page.keyboard.press("Escape");
    failNext(); await page.getByRole("button", { name: /View Legacy strength details/ }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Retry" }).click();
    await expect(page.getByRole("dialog")).toContainText("Detailed exercise records weren't captured");
    await page.keyboard.press("Escape");
    const manual = page.getByRole("button", { name: /View Boxing details/ });
    await manual.click(); await expect(page.getByRole("dialog")).toContainText("40 min");
    await expect(page.getByRole("dialog")).toContainText("No exercises recorded.");
    await expect(page.getByRole("dialog").locator("ol")).toHaveCount(0);
    await page.getByRole("button", { name: "Edit Boxing", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "Edit session" })).toBeVisible();
    await expect(page.getByRole("dialog", { name: "Edit session", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    expect(errors).toEqual([]);
  });
}

for (const width of [390, 1280]) {
  for (const source of ["external", "unlxck"] as const) {
    test(`Journal details keep recorded evidence for ${source} at ${width}px`, async ({ page, baseURL }) => {
      await page.setViewportSize({ width, height: 844 });
      await setup(page, baseURL!);
      const id = source === "external" ? "manual" : "strength";
      const session = { ...rows.find(row => row.id === id)!, status: "modified", pain_after: 3, modification_reason: "Reduced load for comfort", notes: "Coach cue: steady tempo" };
      const block = { name: "Back squat", kind: "exercise", source: source === "external" ? "manual" : "plan", format: "strength", status: "modified",
        prescribed: source === "external" ? {} : { sets: 4, reps: 6, load: { value: 95, unit: "kg" }, rest: { value: 120, unit: "seconds" }, intensity: "RPE 7" },
        actual: { sets: 3, rest: { value: 150, unit: "seconds" }, intensity: "RPE 8", set_entries: [
          { set_number: 1, reps: 6, load: { value: 75, unit: "kg" }, rpe: 7 },
          { set_number: 2, reps: 5, load: { value: 80, unit: "kg" }, rpe: 8 },
          { set_number: 3, reps: 4, load: { value: 85, unit: "kg" }, rpe: 9 },
        ] }, reason: "load_reduced", notes: "Stopped short of fatigue", responses: [] };
      await page.route(`**/api/training/history/${source}/${id}`, route => route.fulfill({ json: {
        session, prescription_source: source === "external" ? "manual" : "snapshot", blocks: [block],
      } }));
      const writes: string[] = [];
      page.on("request", request => { if (["PUT", "PATCH", "DELETE"].includes(request.method())) writes.push(request.url()); });
      const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
      await page.goto("/history");
      await page.getByRole("button", { name: source === "external" ? /View Boxing details/ : /View Strength & Power details/ }).click();
      const dialog = page.getByRole("dialog", { name: session.title, exact: true });
      await expect(dialog.locator("header")).toContainText(source === "external" ? "40 min · Hard · Added by you" : "RPE 7/10 · UNLXCK");
      await expect(dialog).toContainText("Pain after: 3/10");
      await expect(dialog).toContainText("Reduced load for comfort");
      await expect(dialog.getByRole("region", { name: "Session overview" })).toHaveCount(0);
      await expect(dialog.getByRole("button", { name: "Add Session", exact: true })).toHaveCount(0);
      await expect(dialog.getByRole("button", { name: "Delete session", exact: true })).toBeHidden();
      await dialog.getByText("···", { exact: true }).click();
      await expect(dialog.getByRole("button", { name: "Delete session", exact: true })).toBeVisible();
      await dialog.getByText("···", { exact: true }).click();
      const toggle = dialog.getByRole("button", { name: "Back squat", exact: true });
      await expect(toggle).toHaveAttribute("aria-expanded", "false");
      await expect(dialog).toContainText("75–85 kg");
      await page.screenshot({ path: `/tmp/journal-${source}-${width}-collapsed.png` });
      await toggle.click();
      const measurements = dialog.getByRole("region", { name: source === "external" ? "Recorded: Back squat" : "Performance: Back squat", exact: true });
      await expect(measurements).toContainText("75–85 kg");
      if (source === "unlxck") await expect(measurements).toContainText("95 kg");
      await expect(measurements).toContainText("Rest");
      await expect(measurements).toContainText("Intensity");
      await expect(dialog).toContainText("Stopped short of fatigue");
      const results = dialog.locator("li details");
      await results.locator("summary").click();
      const sets = results.getByRole("list", { name: "Recorded sets" });
      await expect(sets.getByRole("listitem")).toHaveCount(3);
      await expect(sets.getByRole("listitem").nth(0)).toContainText("75");
      await expect(sets.getByRole("listitem").nth(1)).toContainText("80");
      await expect(sets.getByRole("listitem").nth(2)).toContainText("85");
      expect(await dialog.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
      expect((await new AxeBuilder({ page }).include("dialog").analyze()).violations).toEqual([]);
      await page.screenshot({ path: `/tmp/journal-${source}-${width}-expanded.png` });
      await toggle.click();
      await page.keyboard.press("Escape");
      await page.reload();
      await page.getByRole("button", { name: source === "external" ? /View Boxing details/ : /View Strength & Power details/ }).click();
      await expect(page.getByRole("dialog", { name: session.title, exact: true })).toContainText("75–85 kg");
      expect(writes).toEqual([]);
      expect(errors).toEqual([]);
    });
  }
}
