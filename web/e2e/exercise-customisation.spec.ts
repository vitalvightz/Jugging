import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { isolateFromNetwork } from "./support";

/**
 * Swap / add / remove from Today's Enhanced Card, end to end against the real
 * API (ranking, override writes, effective plan reads) served over an
 * in-memory store: python tools/e2e_exercise_customisation_server.py
 */
const api = process.env.EXERCISE_TEST_API;
const storageKey = `sb-${new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").hostname.split(".")[0]}-auth-token`;
const shots = process.env.EXERCISE_SCREENSHOTS;

async function setup(page: Page, baseURL: string) {
  await isolateFromNetwork(page, baseURL);
  await page.route("https://fonts.googleapis.com/**", (route) => route.fulfill({ contentType: "text/css", body: "" }));
  await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [storageKey, JSON.stringify({
    access_token: "athlete-token", refresh_token: "test-refresh", token_type: "bearer",
    expires_in: 86400, expires_at: Math.floor(Date.now() / 1000) + 86400,
    user: { id: "athlete-1", email: "ari@example.com", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} },
  })]);
  const writes: { path: string; body: unknown }[] = [];
  await page.route("**/api/**", async (route) => {
    try {
    const request = route.request();
    const url = new URL(request.url());
    const response = await page.request.fetch(`${api}${url.pathname}${url.search}`, {
      method: request.method(),
      headers: { Authorization: "Bearer athlete-token", "Content-Type": "application/json" },
      data: request.postData() ?? undefined,
      failOnStatusCode: false,
    });
    if (request.method() === "POST" && url.pathname.includes("/exercise-changes")) {
      writes.push({ path: url.pathname, body: request.postDataJSON() });
    }
    if (url.pathname === "/api/me" && response.ok()) {
      const me = await response.json();
      // The seeded athlete has completed onboarding and consent.
      Object.assign(me.profile, {
        athlete_timezone: "UTC", technical_style: ["boxing"], date_of_birth: "1995-01-01", meets_minimum_age: true,
        is_minor: false, terms_accepted: true, health_consent_granted: true, private_trial_ack_at: "2026-01-01T12:00:00Z",
      });
      return await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(me) });
    }
    return await route.fulfill({ status: response.status(), contentType: "application/json", body: await response.body() });
    } catch (error) {
      // Navigating/reloading cancels superseded app reads while the real API
      // response is in flight. Only that already-handled route is ignored.
      if (error instanceof Error && /Route is already handled/.test(error.message)) return;
      throw error;
    }
  });
  return writes;
}

/** Let the sheet's enter animation finish before a screenshot. */
async function settle(page: Page) {
  await page.waitForTimeout(300);
}

test.describe.configure({ mode: "serial" });
test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

for (const width of [320, 390, 1280]) {
  test(`Today: swap, undo, remove and add without leaving Today (${width}px)`, async ({ page, baseURL }) => {
    test.skip(!api, "Run with EXERCISE_TEST_API pointing at tools/e2e_exercise_customisation_server.py");
    expect((await page.request.post(`${api}/__e2e/reset`)).ok()).toBe(true);
    await page.setViewportSize({ width, height: 844 });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const writes = await setup(page, baseURL!);

    await page.goto("/today");
    const squat = page.getByRole("button", { name: "Change Back Squat", exact: true });
    await expect(squat).toBeVisible();
    const startUrl = page.url();
    // No horizontal overflow from the new control at any width.
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

    // Swap: three dots -> Swap -> pick -> updated in place, with Undo.
    await squat.click();
    const menu = page.getByRole("dialog", { name: "Back Squat" });
    await expect(menu.getByRole("group", { name: "Change Back Squat" })).toBeVisible();
    const actions = menu.getByRole("group", { name: "Change Back Squat" }).getByRole("button");
    const boxes = await Promise.all((await actions.all()).map((action) => action.boundingBox()));
    // One horizontal bar: every action shares a row.
    expect(new Set(boxes.map((box) => Math.round(box!.y))).size).toBe(1);
    if (shots) await settle(page), await page.screenshot({ path: `${shots}/menu-${width}.png` });
    await menu.getByRole("button", { name: "Swap", exact: true }).click();
    const sheet = page.getByRole("dialog", { name: "Swap Back Squat" });
    const option = sheet.getByRole("button", { name: /Cossack Squat/ });
    await expect(option).toBeVisible();
    await expect(sheet.getByRole("radio", { name: "Just this workout" })).toHaveAttribute("aria-checked", "true");
    await expect(sheet.getByText("4 × 5 · RPE 7").first()).toBeVisible();
    expect((await new AxeBuilder({ page }).include('[role="dialog"]').analyze()).violations).toEqual([]);
    expect(await sheet.evaluate((panel) => panel.scrollWidth <= panel.clientWidth)).toBe(true);
    if (shots) await settle(page), await page.screenshot({ path: `${shots}/swap-sheet-${width}.png` });
    await option.click();
    await expect(sheet).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Change Cossack Squat", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Change Back Squat", exact: true })).toHaveCount(0);
    await expect(page.getByText("Swapped to Cossack Squat")).toBeVisible();
    if (shots) await settle(page), await page.screenshot({ path: `${shots}/swapped-${width}.png` });

    // Undo restores the prescription.
    await page.getByRole("button", { name: "Undo", exact: true }).click();
    await expect(page.getByRole("button", { name: "Change Back Squat", exact: true })).toBeVisible();

    // Remove an accessory: no confirmation, a short Undo, and Restore on the card.
    await page.getByRole("button", { name: "Change Front Plank", exact: true }).click();
    await page.getByRole("button", { name: "Remove Front Plank", exact: true }).click();
    await expect(page.getByRole("button", { name: "Change Front Plank", exact: true })).toHaveCount(0);
    const restore = page.getByRole("button", { name: "Restore Front Plank", exact: true });
    await expect(restore).toBeVisible();
    // Add and Restore share the session's foot line.
    const [addBox, restoreBox] = await Promise.all([
      page.getByRole("button", { name: "Add exercise" }).boundingBox(), restore.boundingBox(),
    ]);
    expect(Math.abs(addBox!.y - restoreBox!.y)).toBeLessThan(4);
    if (shots) await settle(page), await page.screenshot({ path: `${shots}/removed-${width}.png` });
    await restore.click();
    await expect(page.getByRole("button", { name: "Change Front Plank", exact: true })).toBeVisible();

    // Add: anything from the library, searched inside the sheet.
    await page.getByRole("button", { name: "Add exercise" }).click();
    const add = page.getByRole("dialog", { name: "Add exercise" });
    await expect(add.locator("ul > li").first()).toBeVisible();
    await add.getByRole("searchbox", { name: "Search the exercise library" }).fill("sled push");
    const first = add.locator("ul:not([data-refreshing='true']) > li").filter({ hasText: /sled/i }).first();
    await expect(first).toBeVisible();
    // Nothing inside the sheet runs off its edge, search field included.
    expect(await add.evaluate((panel) => panel.scrollWidth <= panel.clientWidth)).toBe(true);
    if (shots) await settle(page), await page.screenshot({ path: `${shots}/library-search-${width}.png` });
    const name = (await first.locator("button").first().locator("span span").first().textContent())!.trim();
    await first.locator("button").first().click();
    const doseForm = first.locator("form");
    if (await doseForm.count()) {
      if (shots) await settle(page), await page.screenshot({ path: `${shots}/dose-${width}.png` });
      const inputs = doseForm.locator("input");
      await inputs.nth(0).fill("3");
      await inputs.nth(1).fill("20");
      await inputs.nth(2).fill("60");
      await doseForm.getByRole("button", { name: "Add" }).click();
    }
    await expect(add).toHaveCount(0);
    await expect(page.getByRole("button", { name: `Change ${name}`, exact: true })).toBeVisible();
    // One confirmation at a time, never a stack of toasts.
    await expect(page.locator(".toast")).toHaveCount(1);
    if (shots) await settle(page), await page.screenshot({ path: `${shots}/added-${width}.png` });

    expect(page.url()).toBe(startUrl);
    expect(writes.filter((write) => write.path.endsWith("/exercise-changes"))).toHaveLength(3);
    expect(errors).toEqual([]);
  });
}

for (const width of [390, 1280]) {
  test(`Sport-aware hill runs can be added, edited and retained after reload (${width}px)`, async ({ page, baseURL }) => {
    test.skip(!api, "Run with EXERCISE_TEST_API pointing at the real seeded API");
    await page.request.post(`${api}/__e2e/reset`);
    await page.setViewportSize({ width, height: 844 });
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    const writes = await setup(page, baseURL!);
    await page.goto("/today");
    await page.getByRole("button", { name: "Add exercise", exact: true }).click();
    const add = page.getByRole("dialog", { name: "Add exercise" });
    const search = add.getByRole("searchbox", { name: "Search the exercise library" });
    await search.fill("teep");
    await expect(add.getByText("No library exercise matches that search.")).toBeVisible();
    await expect(add.locator("ul:not([data-refreshing='true']) > li")).toHaveCount(0);
    await search.fill("Tempo Hill Runs");
    const option = add.locator("ul:not([data-refreshing='true']) > li").filter({ hasText: "Tempo Hill Runs" }).first();
    await option.locator("button").first().click();
    await expect(option.getByLabel("Dose format")).toHaveValue("intervals");
    await expect(option.getByLabel("Rounds", { exact: true })).toHaveValue("6");
    await expect(option.getByLabel("Work", { exact: true })).toHaveValue("20");
    await expect(option.getByLabel("Reps", { exact: true })).toHaveCount(0);
    await option.getByLabel("Rest", { exact: true }).fill("60");
    if (shots) await settle(page), await page.screenshot({ path: `${shots}/hill-dose-${width}.png` });
    await option.getByRole("button", { name: "Add", exact: true }).click();
    await expect(add).toHaveCount(0);
    await page.getByRole("button", { name: "Change Tempo Hill Runs", exact: true }).click();
    await page.getByRole("dialog", { name: "Tempo Hill Runs", exact: true }).getByRole("button", { name: "Edit", exact: true }).click();
    const edit = page.getByRole("dialog", { name: "Edit Tempo Hill Runs", exact: true });
    await edit.getByLabel("Rounds", { exact: true }).fill("5");
    await edit.getByLabel("Work", { exact: true }).fill("25");
    await edit.getByLabel("Rest", { exact: true }).fill("75");
    await expect(edit.locator("details")).not.toHaveAttribute("open");
    await edit.locator("summary").click();
    await edit.getByLabel("Intensity", { exact: true }).fill("Easy uphill pace");
    await edit.getByLabel("RPE (optional)", { exact: true }).fill("6.5");
    await edit.getByLabel("Cues", { exact: true }).fill("Run tall; keep the shoulders relaxed.");
    await edit.getByLabel("Stop rules", { exact: true }).fill("Stop for sharp pain.");
    expect(await edit.evaluate(panel => panel.scrollWidth <= panel.clientWidth)).toBe(true);
    if (shots) await settle(page), await page.screenshot({ path: `${shots}/hill-edit-${width}.png` });
    await edit.getByRole("button", { name: "Save changes" }).click();
    await expect(edit).toHaveCount(0);
    await page.reload();
    const changed = page.locator(".ex-row").filter({ has: page.getByRole("button", { name: "Change Tempo Hill Runs", exact: true }) });
    await expect(changed).toContainText("RPE 6.5");
    await changed.getByRole("button", { name: "Tempo Hill Runs", exact: true }).click();
    await page.getByRole("dialog", { name: "Tempo Hill Runs", exact: true }).locator("summary").filter({ hasText: "Coaching cues" }).click();
    await expect(page.getByText("Run tall; keep the shoulders relaxed.", { exact: true })).toBeVisible();
    await expect(page.getByText(/sharp pain\./).first()).toBeVisible();
    if (shots) await settle(page), await page.screenshot({ path: `${shots}/hill-rendered-${width}.png` });
    expect(writes.filter(write => write.path.endsWith("/exercise-changes"))).toHaveLength(2);
    expect(errors).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

for (const technical of ["session", "sessionless"]) {
  test(`Technical day supports adding and editing its first drill (${technical})`, async ({ page, baseURL }) => {
    test.skip(!api, "Run with the seeded API");
    await page.request.post(`${api}/__e2e/reset?technical=${technical}`);
    await page.setViewportSize({ width: 390, height: 844 });
    await setup(page, baseURL!);
    await page.goto("/plans/44444444-4444-4444-4444-444444444444");
    const addButton = page.getByRole("button", { name: "Add exercise", exact: true }).first();
    await expect(addButton).toBeVisible();
    await addButton.click();
    const add = page.getByRole("dialog", { name: "Add exercise" });
    await add.getByRole("searchbox").fill("parry cross pivot");
    const option = add.locator("ul:not([data-refreshing='true']) > li").filter({ hasText: "Parry-Cross-Pivot" }).first();
    await option.locator("button").first().click();
    await expect(option.getByLabel("Dose format")).toHaveValue("duration");
    await option.getByLabel("Duration", { exact: true }).fill("120");
    await option.getByRole("button", { name: "Add", exact: true }).click();
    await expect(add).toHaveCount(0);
    const expand = page.getByRole("button", { name: /Show more session detail/ }).first();
    if (await expand.isVisible()) await expand.click();
    await page.getByRole("button", { name: "Change Parry-Cross-Pivot", exact: true }).first().click();
    await page.getByRole("dialog", { name: "Parry-Cross-Pivot", exact: true }).getByRole("button", { name: "Edit", exact: true }).click();
    const edit = page.getByRole("dialog", { name: "Edit Parry-Cross-Pivot" });
    await edit.getByLabel("Duration unit").selectOption("60");
    await edit.getByLabel("Duration", { exact: true }).fill("3");
    await edit.locator("summary").click();
    await edit.getByLabel("Cues", { exact: true }).fill("Light feet, relaxed shoulders.");
    await edit.getByRole("button", { name: "Save changes" }).click();
    await expect(edit).toHaveCount(0);
    if (shots) await page.screenshot({ path: `${shots}/technical-${technical}-390.png` });
    await page.reload();
    await expect(page.getByText("3 min").first()).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test("Custom drafts and published plans use the same editable exercise cards", async ({ page, baseURL }) => {
  test.skip(!api, "Run with the seeded API");
  await page.request.post(`${api}/__e2e/reset`);
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page, baseURL!);
  const headers = { Authorization: "Bearer athlete-token" };
  const state = await (await page.request.get(`${api}/api/today`, { headers })).json();
  const day = state.today.training_day;
  const weekday = new Intl.DateTimeFormat("en", { weekday: "long", timeZone: "UTC" }).format(new Date(`${day}T12:00:00Z`));
  const create = await page.request.post(`${api}/api/custom-plans`, { headers, data: {
    name: "My boxing plan", sessions: [{ id: crypto.randomUUID(), name: "Conditioning", weekday,
      session_type: "conditioning", exercises: [{ id: crypto.randomUUID(), origin: "manual", name: "My shadowboxing",
        dose: { mode: "duration", duration_seconds: 600, rpe: 4 },
        coaching_cues: ["Stay relaxed"], stop_rules: ["Stop for sharp pain"] }],
    }],
  } });
  expect(create.ok()).toBe(true);
  const draft = await create.json();
  await page.goto(`/plans/custom?plan=${draft.id}`);
  await expect(page.locator(".ex-row")).toContainText("My shadowboxing");
  await page.getByRole("button", { name: "Edit dose for My shadowboxing", exact: true }).click();
  const editor = page.getByRole("dialog", { name: "My shadowboxing", exact: true });
  await editor.getByLabel("RPE (optional)", { exact: true }).fill("5");
  await editor.getByLabel("Cues", { exact: true }).fill("Light feet, steady breathing.");
  await editor.getByRole("button", { name: "Done", exact: true }).click();
  await expect(page.locator(".ex-row")).toContainText("RPE 5");
  const saved = page.waitForResponse(r => r.url().endsWith(`/api/custom-plans/${draft.id}`) && r.request().method() === "PUT");
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  const savedResponse = await saved;
  expect(savedResponse.ok()).toBe(true);
  const savedDraft = await savedResponse.json();
  expect(savedDraft.sessions[0].exercises[0].coaching_cues).toEqual(["Light feet, steady breathing."]);
  expect(savedDraft.sessions[0].exercises[0].stop_rules).toEqual(["Stop for sharp pain"]);
  const draftCard = page.locator(".ex-row").filter({ hasText: "My shadowboxing" });
  await draftCard.getByRole("button", { name: "My shadowboxing", exact: true }).click();
  await expect(draftCard).toContainText("Light feet, steady breathing.");
  await draftCard.scrollIntoViewIfNeeded();
  if (shots) await page.screenshot({ path: `${shots}/custom-draft-390.png` });
  const publish = await page.request.post(`${api}/api/custom-plans/${draft.id}/open-plan`, { headers });
  expect(publish.ok()).toBe(true);
  const published = await publish.json();
  await page.goto(`/plans/${published.plan_id}`);
  await page.getByRole("button", { name: "Change My shadowboxing", exact: true }).first().click();
  await page.getByRole("dialog", { name: "My shadowboxing", exact: true }).getByRole("button", { name: "Edit", exact: true }).click();
  const edit = page.getByRole("dialog", { name: "Edit My shadowboxing" });
  await edit.getByLabel("Duration", { exact: true }).fill("15");
  await edit.getByLabel("Rest", { exact: true }).fill("45");
  await edit.getByRole("button", { name: "Save changes" }).click();
  await expect(edit).toHaveCount(0);
  await page.reload();
  await expect(page.getByText("15 min").first()).toBeVisible();
  const publishedCard = page.locator(".ex-row").filter({ hasText: "My shadowboxing" }).first();
  await publishedCard.getByRole("button", { name: "My shadowboxing", exact: true }).click();
  await publishedCard.scrollIntoViewIfNeeded();
  if (shots) await page.screenshot({ path: `${shots}/custom-published-390.png` });
});


test("Cue-only editing preserves an existing RIR prescription after reload", async ({ page, baseURL }) => {
  test.skip(!api, "Run with the seeded API");
  await page.request.post(`${api}/__e2e/reset?rir=true`);
  await page.setViewportSize({ width: 390, height: 844 });
  const writes = await setup(page, baseURL!);
  await page.goto("/today");
  await page.getByRole("button", { name: "Change Back Squat", exact: true }).click();
  await page.getByRole("dialog", { name: "Back Squat", exact: true }).getByRole("button", { name: "Edit", exact: true }).click();
  const edit = page.getByRole("dialog", { name: "Edit Back Squat", exact: true });
  const coaching = edit.locator("details").filter({ has: page.getByLabel("Cues", { exact: true }) });
  await expect(coaching).not.toHaveAttribute("open");
  await coaching.locator("summary").click();
  await edit.getByLabel("Cues", { exact: true }).fill("Brace before moving");
  await edit.getByRole("button", { name: "Save changes" }).click();
  await expect(edit).toHaveCount(0);
  const change = writes.at(-1)!.body as { action: string; dose?: unknown };
  expect(change.action).toBe("edit");
  expect(change.dose).toBeUndefined();
  const headers = { Authorization: "Bearer athlete-token" };
  const state = await (await page.request.get(`${api}/api/today`, { headers })).json();
  const squat = state.today.next_session.blocks.find((block: { exercise_key: string }) => block.exercise_key === "back-squat");
  expect(squat.effort).toMatchObject({ method: "RIR", value: 3 });
  expect(squat.coaching_cues).toEqual(["Brace before moving"]);
  await page.reload();
  await expect(page.locator(".ex-row").filter({ hasText: "Back Squat" })).toContainText("RIR 3");
  await page.getByRole("button", { name: "Change Back Squat", exact: true }).click();
  await page.getByRole("dialog", { name: "Back Squat", exact: true }).getByRole("button", { name: "Edit", exact: true }).click();
  await coaching.locator("summary").click();
  await expect(edit.getByLabel("Cues", { exact: true })).toHaveValue("Brace before moving");
});

test("strength timer logs during rest, restores drafts and reviews actual history", async ({ page, baseURL }) => {
  test.skip(!api, "Run with EXERCISE_TEST_API pointing at the seeded local API");
  test.setTimeout(120_000);
  await page.request.post(`${api}/__e2e/reset`);
  const headers = { Authorization: "Bearer athlete-token" };
  const initial = await (await page.request.get(`${api}/api/today`, { headers })).json();
  const planId = initial.active_plan.id;
  expect((await page.request.post(`${api}/api/today/checkin`, { headers,
    data: { plan_id: planId, sleep: "good", body: "normal", pain: "none", phase: "GPP" } })).ok()).toBe(true);
  const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page, baseURL!);
  await page.goto("/today");
  await page.getByRole("button", { name: "Start session", exact: true }).click();
  const timer = page.locator(".st-root");
  const capture = async (state: string, action: string) => {
    for (const [width, height] of [[320, 568], [390, 844], [430, 932], [1280, 800], [1440, 900]]) {
      await page.setViewportSize({ width, height });
      await expect(timer.getByRole("button", { name: action, exact: true })).toBeInViewport({ ratio: 1 });
      expect(await timer.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
      if (["work", "rest", "confirmed"].includes(state)) {
        await expect(timer.getByRole("timer")).toBeInViewport({ ratio: 1 });
        await expect(timer.getByRole("heading", { name: "Back Squat", exact: true })).toBeInViewport({ ratio: 1 });
        await expect(timer.locator(".st-phase")).toBeInViewport({ ratio: 1 });
      }
      if (["work", "confirmed"].includes(state)) {
        const stats = timer.locator(".st-stats");
        await expect(stats).toBeInViewport({ ratio: 1 });
        const cells = await stats.locator(":scope > div").evaluateAll(elements => elements.map(el => el.getBoundingClientRect().y));
        expect(new Set(cells.map(Math.round)).size).toBe(1);
      }
      if (shots) { await settle(page); await page.screenshot({ path: `${shots}/timer-${state}-${width}.png` }); }
    }
    await page.setViewportSize({ width: 390, height: 844 });
  };
  await timer.getByRole("button", { name: "Start", exact: true }).click();
  await timer.locator(".st-hint").click();
  await expect(timer.getByLabel("Load kg, set 1")).toHaveCount(0);
  await expect(timer).toContainText("100 kg");
  await expect(timer.locator('.st-stats [data-kind="effort"] dt')).toHaveText("RPE");
  await expect(timer.locator('.st-stats [data-kind="effort"] dd')).toHaveText("7");
  await capture("work", "Set 1 done");
  expect((await new AxeBuilder({ page }).include(".st-root").analyze()).violations).toEqual([]);
  await timer.getByRole("button", { name: "Set 1 done", exact: true }).click();
  await expect(timer.getByLabel("Load kg, set 1")).toHaveValue("100");
  await expect(timer.getByLabel("Reps, set 1")).toHaveValue("5");
  await expect(timer.getByLabel("RPE, set 1")).toHaveValue("7");
  await capture("rest", "✓ Confirm set");
  expect((await new AxeBuilder({ page }).include(".st-root").analyze()).violations).toEqual([]);
  await timer.getByLabel("Load kg, set 1").fill("105");
  await timer.getByLabel("RPE, set 1").fill("8");
  const clock = await timer.getByRole("timer").textContent();
  await expect.poll(() => timer.getByRole("timer").textContent()).not.toBe(clock);
  await timer.getByRole("button", { name: "✓ Confirm set", exact: true }).click();
  await expect(timer.getByRole("status")).toContainText("Set 1 logged");
  await capture("confirmed", "Start set 2");
  await timer.getByRole("button", { name: "Edit set 1", exact: true }).click();
  await expect(timer).toHaveAttribute("data-rest-log", "open");
  await expect(timer.getByLabel("Load kg, set 1")).toHaveValue("105");
  await timer.getByRole("button", { name: "✓ Confirm set", exact: true }).click();
  await timer.getByRole("button", { name: "Start set 2", exact: true }).click();
  await expect(timer.getByRole("region", { name: "Log completed set" })).toHaveCount(0);
  await expect(timer.getByLabel("Load kg, set 1")).toHaveCount(0);
  await timer.getByRole("button", { name: "Set 2 done", exact: true }).click();
  await timer.getByLabel("Load kg, set 2").fill("110");
  await timer.getByLabel("RPE, set 2").fill("8.5");
  // Reloading preserves a running rest countdown and the unconfirmed draft.
  await page.reload();
  await page.locator(".st-mini").click();
  await expect(timer.getByLabel("Load kg, set 2")).toHaveValue("110");
  await expect(timer.getByLabel("RPE, set 2")).toHaveValue("8.5");
  await timer.getByRole("button", { name: "✓ Confirm set", exact: true }).click();
  await timer.getByRole("button", { name: "Start set 3", exact: true }).click();
  await timer.getByRole("button", { name: "Set 3 done", exact: true }).click();
  await timer.getByRole("button", { name: "✓ Confirm set", exact: true }).click();
  await timer.getByRole("button", { name: "Start set 4", exact: true }).click();
  await timer.getByRole("button", { name: "Set 4 done", exact: true }).click();
  await expect(timer.getByRole("heading", { name: "Front Plank", exact: true })).toBeVisible();
  await expect(timer.getByLabel("Load kg, set 4")).toHaveValue("100");
  await timer.getByLabel("Load kg, set 4").fill("102.5");
  await capture("transition", "✓ Confirm set");
  expect((await new AxeBuilder({ page }).include(".st-root").analyze()).violations).toEqual([]);
  await timer.getByRole("button", { name: "✓ Confirm set", exact: true }).click();
  await expect(timer.getByRole("status")).toContainText("Back Squat");
  await timer.getByRole("button", { name: "Start", exact: true }).click();
  await expect(timer.getByRole("region", { name: "Log completed set" })).toHaveCount(0);
  await expect(timer.getByRole("timer")).toBeVisible();
  await timer.getByRole("button", { name: "End session", exact: true }).click();
  await page.getByRole("alertdialog", { name: "End session", exact: true }).getByRole("button", { name: "End", exact: true }).click();
  await expect(timer.getByRole("heading", { name: "Review your sets" })).toHaveCount(0);
  await expect(timer).toHaveCount(0);
  const response = await page.request.get(`${api}/api/today/exercise-logs?plan_id=${planId}`, { headers });
  expect(response.ok()).toBe(true);
  const logs = (await response.json()).logs;
  const squat = logs.find((log: { exercise_key: string }) => log.exercise_key === "back-squat");
  expect(squat.actual.set_entries.map((entry: { load: { value: number } }) => entry.load.value)).toEqual([105, 110, 100, 102.5]);
  expect(squat.actual.set_entries.map((entry: { rpe: number }) => entry.rpe)).toEqual([8, 8.5, 7, 7]);
  expect(squat.actual.effort).toBeNull();
  expect(squat.status).toBe("modified");
  expect(squat.actual.load).toBeNull();
  expect(errors).toEqual([]);
});

for (const width of [390, 1280]) for (const partial of [false, true]) {
  test(`timer leaves unknown measurements unreported through real history (${width}px, partial=${partial})`, async ({ page, baseURL }) => {
    test.skip(!api, "Run with EXERCISE_TEST_API pointing at the seeded local API");
    test.setTimeout(60_000);
    await page.request.post(`${api}/__e2e/reset`);
    const headers = { Authorization: "Bearer athlete-token" };
    const initial = await (await page.request.get(`${api}/api/today`, { headers })).json();
    const planId = initial.active_plan.id;
    await page.request.post(`${api}/api/today/checkin`, { headers,
      data: { plan_id: planId, sleep: "good", body: "normal", pain: "none", phase: "GPP" } });
    await page.setViewportSize({ width, height: 844 });
    await setup(page, baseURL!);
    await page.goto("/today");
    await page.getByRole("button", { name: "Start session", exact: true }).click();
    const timer = page.locator(".st-root");
    await timer.getByRole("button", { name: "Start", exact: true }).click();
    for (const set of [1, 2, 3, 4]) {
      await timer.getByRole("button", { name: `Set ${set} done`, exact: true }).click();
      if (set < 4) {
        if (partial && set !== 2) {
          await timer.getByLabel(`Load kg, set ${set}`).fill(set === 1 ? "105" : "110");
          await timer.getByRole("button", { name: "✓ Confirm set", exact: true }).click();
        } else if (set === 2) await timer.getByLabel("Load kg, set 2").fill("invalid");
        await timer.getByRole("button", { name: `Start set ${set + 1}`, exact: true }).click();
      }
    }
    await timer.getByRole("button", { name: "End session", exact: true }).click();
    await page.getByRole("alertdialog", { name: "End session", exact: true }).getByRole("button", { name: "End", exact: true }).click();
    await expect(timer.getByRole("region", { name: "Log completed set" })).toBeVisible();
    await timer.getByRole("button", { name: "Leave unreported", exact: true }).click();
    await expect(timer.getByRole("button", { name: "Leave remaining unreported", exact: true })).toBeInViewport({ ratio: 1 });
    if (partial) await timer.getByRole("button", { name: "Leave set 2 unreported", exact: true }).click();
    else await timer.getByRole("button", { name: "Leave remaining unreported", exact: true }).click();
    await expect(timer).toHaveCount(0);
    // These reads use the real API/store, then render the same records in History.
    const logs = await (await page.request.get(`${api}/api/today/exercise-logs?plan_id=${planId}`, { headers })).json();
    const squat = logs.logs.find((log: { exercise_key: string }) => log.exercise_key === "back-squat");
    expect(squat.status).toBe("modified");
    expect(squat.actual.sets).toBe(4);
    expect(squat.actual.reps).toBeNull(); expect(squat.actual.load).toBeNull(); expect(squat.actual.effort).toBeNull();
    expect((squat.actual.set_entries ?? []).map((entry: { set_number: number }) => entry.set_number)).toEqual(partial ? [1, 3] : []);
    const library = await (await page.request.get(`${api}/api/history/exercise-library`, { headers })).json();
    const record = library.exercises.find((entry: { id: string }) => entry.id === "key:back-squat");
    expect(record.best_loads.map((load: { value: number }) => load.value)).toEqual(partial ? [110] : []);
    await page.goto("/history");
    await page.getByRole("tab", { name: "Exercises", exact: true }).click();
    await page.getByRole("button", { name: "View Back Squat performance history", exact: true }).click();
    const history = page.getByRole("dialog", { name: "Back Squat", exact: true });
    if (partial) {
      await expect(history.getByLabel("Selected performance")).toContainText("Reported");
      await expect(history.getByLabel("Selected performance")).not.toContainText("Confirmed as prescribed");
      const summary = history.locator("details").filter({ hasText: "2 logged · 2 unreported" });
      await summary.locator("summary").click();
      const rows = summary.getByRole("list", { name: "Recorded sets" }).getByRole("listitem");
      await expect(rows).toHaveCount(2);
      await expect(rows.nth(0).locator(":scope > span").first()).toHaveText("1");
      await expect(rows.nth(1).locator(":scope > span").first()).toHaveText("3");
      await expect(summary).toContainText("105"); await expect(summary).toContainText("110");
    } else {
      await expect(history).toContainText("4 sets completed · measurements unreported");
      await expect(history.getByRole("list", { name: "Recorded sets" })).toHaveCount(0);
    }
    if (shots) await page.screenshot({ path: `${shots}/history-unreported-${width}-${partial ? "partial" : "all"}.png` });
  });
}

for (const width of [390, 1280]) for (const reportFinal of [true, false]) {
  test(`session-final set logs inline without a review page (${width}px, report=${reportFinal})`, async ({ page, baseURL }) => {
    test.skip(!api, "Run with EXERCISE_TEST_API pointing at the seeded local API");
    test.setTimeout(60_000);
    await page.request.post(`${api}/__e2e/reset?strength_only=true`);
    const headers = { Authorization: "Bearer athlete-token" };
    const initial = await (await page.request.get(`${api}/api/today`, { headers })).json();
    const planId = initial.active_plan.id;
    await page.request.post(`${api}/api/today/checkin`, { headers,
      data: { plan_id: planId, sleep: "good", body: "normal", pain: "none", phase: "GPP" } });
    await page.setViewportSize({ width, height: 844 });
    await setup(page, baseURL!);
    await page.goto("/today");
    await page.getByRole("button", { name: "Start session", exact: true }).click();
    const timer = page.locator(".st-root");
    await timer.getByRole("button", { name: "Start", exact: true }).click();
    for (const set of [1, 2, 3, 4]) {
      await timer.getByRole("button", { name: `Set ${set} done`, exact: true }).click();
      if (set < 4) {
        await timer.getByRole("button", { name: "✓ Confirm set", exact: true }).click();
        await timer.getByRole("button", { name: `Start set ${set + 1}`, exact: true }).click();
      }
    }
    await expect(timer).toHaveAttribute("data-set-log-context", "complete");
    await expect(timer.getByRole("region", { name: "Log completed set" })).toBeVisible();
    await expect(timer.getByRole("heading", { name: "Review your sets" })).toHaveCount(0);
    await expect(timer.getByLabel("Load kg, set 4")).toHaveValue("100");
    if (reportFinal) {
      await timer.getByLabel("Load kg, set 4").fill("107.5");
      await timer.getByLabel("RPE, set 4").fill("8.5");
      // Even after the usual auto-save handoff delay, the final draft remains available.
      await page.reload();
      await page.waitForTimeout(1100);
      await page.locator(".st-mini").click();
      await expect(timer.getByLabel("Load kg, set 4")).toHaveValue("107.5");
      await expect(timer.getByLabel("RPE, set 4")).toHaveValue("8.5");
      if (width === 390) for (const [w, h] of [[320, 568], [390, 844], [430, 932], [1280, 800], [1440, 900]]) {
        await page.setViewportSize({ width: w, height: h });
        await expect(timer.getByRole("button", { name: "✓ Confirm set", exact: true })).toBeInViewport({ ratio: 1 });
        await expect(timer.getByRole("button", { name: "Leave unreported", exact: true })).toBeInViewport({ ratio: 1 });
        expect(await timer.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
        if (shots) { await settle(page); await page.screenshot({ path: `${shots}/timer-final-set-${w}.png` }); }
      }
      await page.setViewportSize({ width, height: 844 });
      expect((await new AxeBuilder({ page }).include(".st-root").analyze()).violations).toEqual([]);
      await timer.getByRole("button", { name: "✓ Confirm set", exact: true }).click();
    } else await timer.getByRole("button", { name: "Leave unreported", exact: true }).click();
    await expect(timer).toHaveCount(0);
    const response = await page.request.get(`${api}/api/today/exercise-logs?plan_id=${planId}`, { headers });
    const squat = (await response.json()).logs.find((log: { exercise_key: string }) => log.exercise_key === "back-squat");
    expect(squat.actual.sets).toBe(4);
    expect(squat.actual.set_entries.map((entry: { set_number: number }) => entry.set_number)).toEqual(reportFinal ? [1, 2, 3, 4] : [1, 2, 3]);
    if (reportFinal) expect(squat.actual.set_entries[3]).toMatchObject({ load: { value: 107.5, unit: "kg" }, rpe: 8.5 });
    else expect(squat.actual.load).toBeNull();
  });
}

for (const width of [320, 390]) test(`Plan preserves written weights without calling them suggestions (${width}px)`, async ({ page, baseURL }) => {
  test.skip(!api, "Run with the seeded API");
  test.setTimeout(120_000);
  await page.request.post(`${api}/__e2e/reset?progression=true&strength_only=true`);
  const headers = { Authorization: "Bearer athlete-token" };
  const initial = await (await page.request.get(`${api}/api/today`, { headers })).json();
  const planId = initial.active_plan.id;
  await page.setViewportSize({ width, height: 844 });
  const writes = await setup(page, baseURL!);
  await page.request.post(`${api}/api/today/checkin`, { headers,
    data: { plan_id: planId, sleep: "good", body: "normal", pain: "none", phase: "GPP" } });
  await page.goto("/today");
  await page.getByRole("button", { name: "Start session", exact: true }).click();
  await page.getByRole("button", { name: "Close timer", exact: true }).click();
  await page.getByRole("button", { name: "Mark Back Squat done as written", exact: true }).click();
  await page.getByLabel(/What weight did you use\?/).fill("125");
  await page.getByRole("button", { name: "Save weight", exact: true }).click();
  await page.getByRole("button", { name: "Right", exact: true }).click();
  await expect(page.getByRole("button", { name: "Right", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.request.post(`${api}/__e2e/next_exposure`);
  await page.addInitScript(() => localStorage.setItem("unlxck.plan-view", "list"));
  await page.goto(`/plans/${planId}`);
  await expect(page.getByRole("button", { name: "Change Back Squat", exact: true }).first()).toBeVisible();
  for (const weight of [100, 1]) {
    await page.getByRole("button", { name: "Change Back Squat", exact: true }).first().click();
    await page.getByRole("dialog", { name: "Back Squat", exact: true }).getByRole("button", { name: "Edit", exact: true }).click();
    const edit = page.getByRole("dialog", { name: "Edit Back Squat", exact: true });
    await edit.getByLabel("Weight", { exact: true }).fill(String(weight));
    await edit.getByRole("button", { name: "Save changes", exact: true }).click();
    await expect(edit).toHaveCount(0);
    await page.reload();
    const row = page.locator(".ex-row").filter({ has: page.getByRole("button", { name: "Change Back Squat", exact: true }) }).first();
    await expect(row).toContainText(`${weight} kg`);
    await expect(row).toContainText("Last 125 kg");
    await expect(row).not.toContainText("Suggested");
    await row.getByRole("button", { name: "Change Back Squat", exact: true }).click();
    await page.getByRole("dialog", { name: "Back Squat", exact: true }).getByRole("button", { name: "Edit", exact: true }).click();
    await expect(edit.getByLabel("Weight", { exact: true })).toHaveValue(String(weight));
    await edit.getByRole("button", { name: "Close", exact: true }).click();
    const logs = await (await page.request.get(`${api}/api/today/exercise-logs?plan_id=${planId}`, { headers })).json();
    expect(logs.logs).toEqual([]);
    expect(logs.recent_performances[0].actual.load).toEqual({ value: 125, unit: "kg" });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  expect(writes.filter(write => write.path.endsWith("/exercise-changes"))).toHaveLength(2);
});
