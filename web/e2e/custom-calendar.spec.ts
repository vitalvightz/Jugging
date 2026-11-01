import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { setup } from "./custom-plan-fixture";
import { blankDose } from "../lib/custom-plan-dose";
import type { CustomPlan, CustomPlanInput, CustomSession } from "../lib/api-schema.generated";

function demo(): CustomPlan {
  return { id: crypto.randomUUID(), name: "Demo", kind: "self_directed", incomplete: true,
    created_at: "2026-10-12T12:00:00Z", updated_at: "2026-10-12T12:00:00Z",
    sessions: [
      ...["Monday", "Wednesday", "Thursday", "Saturday"].map(weekday => ({
        id: crypto.randomUUID(), name: "Lower Body", weekday: weekday as CustomSession["weekday"], session_type: "strength_power" as const,
        exercises: [{ id: crypto.randomUUID(), origin: "manual" as const, name: "My squat", equipment: "Rack", notes: "Keep my dose",
          media_id: null, community_id: null, community_revision_id: null, incomplete: false,
          coaching_cues: null, stop_rules: null,
          dose: { ...blankDose(), sets: 3, reps: 5, load_or_intensity: "80 kg" } }],
      })),
      { id: crypto.randomUUID(), name: "Training", weekday: "Tuesday", session_type: "recovery", exercises: [] },
      { id: crypto.randomUUID(), name: "Training", weekday: "Friday", session_type: "sparring", exercises: [] },
    ],
  };
}

test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: "wait" }); });

test("rest-day Add session shortcut opens the existing editor on the selected weekday", async ({ page, baseURL }) => {
  test.skip(Boolean(process.env.CUSTOM_PLAN_TEST_API), "Uses a saved draft fixture.");
  await page.setViewportSize({ width: 390, height: 844 });
  const { saved } = await setup(page, baseURL!);
  const original = demo();
  saved.set(original.id, original);
  await page.goto(`/plans/custom?plan=${original.id}&addSession=Sun`);
  const sheet = page.getByRole("dialog", { name: "New session" });
  await expect(sheet).toBeVisible();
  await expect(sheet.getByLabel("Day", { exact: true })).toHaveValue("Sunday");
  await sheet.getByLabel("Session name", { exact: true }).fill("Sunday conditioning");
  await sheet.getByRole("button", { name: "Add session", exact: true }).click();
  await expect(page.getByRole("button", { name: /^Sunday,/ })).toContainText("Sunday conditioning");
  await expect(page.getByRole("button", { name: "Save draft", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  expect(saved.get(original.id)!.sessions.filter(s => s.weekday === "Sunday").map(s => s.name)).toEqual(["Sunday conditioning"]);
});
for (const width of [320, 390, 1280]) {
  test(`Repair Demo, editable rounds, incomplete workout route and footer (${width}px)`, async ({ page, baseURL }) => {
    test.skip(Boolean(process.env.CUSTOM_PLAN_TEST_API), "This case exercises the offline saved-draft fixture.");
    await page.setViewportSize({ width, height: 844 });
    const errors: string[] = [];
    page.on("pageerror", e => errors.push(e.message));
    const { saved } = await setup(page, baseURL!);
    const original = demo(); saved.set(original.id, original);
    await page.goto(`/plans/custom?plan=${original.id}`);
    await expect(page.getByRole("heading", { name: "Demo", exact: true })).toBeVisible();
    // The builder opens on the first scheduled day and shows one day at a time.
    await expect(page.getByRole("button", { name: "Monday, Lower Body", exact: true })).toHaveAttribute("aria-pressed", "true");
    await page.getByRole("button", { name: "Tuesday, Recovery", exact: true }).click();
    await expect(page.getByText("Keep this day for recovery.", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Sunday, Rest", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Save & view plan", exact: true })).toBeEnabled();
    await page.getByRole("button", { name: "Friday, Hard Sparring", exact: true }).click();
    await page.getByRole("button", { name: "Edit dose for Hard Sparring rounds", exact: true }).click();
    await expect(page.getByLabel("Rounds", { exact: true })).toHaveValue("3");
    await expect(page.getByLabel("Work duration", { exact: true })).toHaveValue("3");
    await page.getByLabel("Rounds", { exact: true }).fill("4");
    await page.getByLabel("Work duration", { exact: true }).fill("2");
    await page.getByLabel("Rest between (sec)").fill("45");
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await page.getByRole("button", { name: "Save draft", exact: true }).click();
    await expect(page.getByText("Unsaved changes", { exact: false })).toHaveCount(0);
    await page.reload();
    await page.getByRole("button", { name: "Friday, Hard Sparring", exact: true }).click();
    await expect(page.getByText("4 × 2 min · rest 45s", { exact: false })).toBeVisible();
    await page.getByRole("button", { name: "Save draft", exact: true }).click();
    await expect(page.getByText("Unsaved changes", { exact: false })).toHaveCount(0);
    expect(saved.get(original.id)!.sessions.find(s => s.session_type === "sparring")!.exercises).toHaveLength(1);
    expect(saved.get(original.id)!.sessions.find(s => s.weekday === "Monday")!.exercises).toEqual(original.sessions[0].exercises);
    await page.getByRole("button", { name: /^Thursday,/ }).click();
    await page.getByRole("button", { name: "Edit session 4", exact: true }).click();
    await page.getByLabel("Session type").selectOption("conditioning");
    await expect(page.getByLabel("Session name", { exact: true })).toHaveValue("Lower Body");
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await page.getByRole("button", { name: "Sunday, Rest", exact: true }).click();
    await page.getByRole("button", { name: "Add session on Sunday", exact: true }).click();
    await page.getByLabel("Session type").selectOption("strength_power");
    await expect(page.getByLabel("Session name", { exact: true })).toHaveValue("Strength & Power");
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await expect(page.getByRole("button", { name: "Save & view plan", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Save draft", exact: true })).toBeEnabled();
    await page.getByRole("button", { name: "Edit Sunday", exact: true }).click();
    await expect(page.getByLabel("Session type")).toHaveValue("strength_power");
    await page.getByLabel("Session type").selectOption("rest");
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await expect(page.getByRole("button", { name: "Save & view plan", exact: true })).toBeEnabled();
    await page.getByRole("button", { name: /^Sunday,/ }).click();
    await page.getByRole("button", { name: "Edit session 7", exact: true }).click();
    await page.getByLabel("Session type").selectOption("skill");
    await expect(page.getByLabel("Session name", { exact: true })).toHaveValue("Light Technical");
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await expect(page.getByRole("button", { name: "Edit dose for Light Technical rounds" })).toBeVisible();
    const lastControl = page.getByRole("button", { name: /^Add exercise to / }).last();
    await lastControl.scrollIntoViewIfNeeded();
    const cardBox = (await lastControl.boundingBox())!;
    const footer = (await page.getByRole("button", { name: "Save draft", exact: true }).locator("xpath=../..").boundingBox())!;
    expect(cardBox.y + cardBox.height).toBeLessThanOrEqual(footer.y + 1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect((await new AxeBuilder({ page }).include('[aria-label="Custom training plan builder"]').analyze()).violations).toEqual([]);
    await page.screenshot({ path: `${process.env.TEMP ?? "/tmp"}/unlxck-calendar-${width}.png`, fullPage: false });
    expect(errors).toEqual([]);
  });
}

test("Save → View → Activate → Today against the real local API", async ({ page, baseURL }) => {
  test.skip(!process.env.CUSTOM_PLAN_TEST_API, "Run with the existing CUSTOM_PLAN_TEST_API fixture server.");
  await page.setViewportSize({ width: 390, height: 844 });
  const errors: string[] = []; page.on("pageerror", e => errors.push(e.message));
  await setup(page, baseURL!);
  const api = process.env.CUSTOM_PLAN_TEST_API!;
  const headers = { Authorization: "Bearer athlete-token" };
  const source = demo();
  const value: CustomPlanInput = { name: source.name, sessions: source.sessions.map(s => ({ ...s, exercises: s.exercises.map(e => {
    const { incomplete, ...input } = e; void incomplete; return input;
  }) })) };
  const created = await page.request.post(`${api}/api/custom-plans`, { headers, data: value });
  expect(created.ok()).toBe(true); const draft = await created.json();
  await page.goto(`/plans/custom?plan=${draft.id}`);
  await expect(page.getByRole("button", { name: "Save & view plan", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Save & view plan", exact: true }).click();
  await expect(page).toHaveURL(/\/plans\/[a-f0-9-]+$/);
  await expect(page.getByRole("heading", { name: "Demo", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Set active", exact: true })).toBeVisible();
  expect((await page.request.get(`${api}/api/plans/active`, { headers })).status()).toBe(404);
  await page.getByRole("button", { name: "Set active", exact: true }).click();
  await expect(page.getByRole("button", { name: "Set active", exact: true })).toHaveCount(0);
  expect((await page.request.get(`${api}/api/plans/active`, { headers })).ok()).toBe(true);
  await page.goto("/today");
  await expect(page.locator("#today-session")).toBeVisible();
  await expect(page.locator("#today-session")).toContainText(/Lower Body|Rest day|Recovery day/);
  await page.screenshot({ path: `${process.env.TEMP ?? "/tmp"}/unlxck-calendar-today.png`, fullPage: false });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});
