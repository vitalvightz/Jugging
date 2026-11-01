import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { setup, entries } from "./custom-plan-fixture";
test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: "wait" }); });

for (const width of [320, 390, 1280, 1600]) {
  test(`Saved links, mixed exercises and compact doses (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    const { mutations } = await setup(page, baseURL!);
    await page.goto("/create");
    await page.getByRole("link", { name: /^Build Your Own/ }).click();
    await page.getByLabel("Plan name", { exact: true }).fill(`Weekly strength ${width}`);
    await page.getByRole("button", { name: "+ Add Session", exact: true }).click();
    await page.getByRole("button", { name: "Edit session 1", exact: true }).click();
    await page.getByLabel("Session name", { exact: true }).fill("Strength");
    await page.getByLabel("Day", { exact: true }).selectOption("Monday");
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await page.getByRole("button", { name: "Add exercise to Strength", exact: true }).click();
    await page.getByRole("button", { name: "Add from library", exact: true }).click();
    await page.getByLabel("Search drills", { exact: true }).fill(entries[0].name);
    await page
      .getByRole("list", { name: "Drill results" })
      .getByRole("button")
      .filter({ hasText: entries[0].name })
      .click();
    await page.getByRole("button", { name: "Add to Session", exact: true }).click();
    await expect(page.getByLabel("Sets", { exact: true })).toHaveCount(0);
    if (!process.env.CUSTOM_PLAN_TEST_API) {
      // Adding from the library applies the source's format in the background; wait for it before editing.
      await expect(page.getByRole("region", { name: "Session 1", exact: true })).toContainText("5 × 5 min");
    }
    await page
      .getByRole("button", {
        name: `Edit dose for ${entries[0].name}`,
        exact: true,
      })
      .click();
    if (!process.env.CUSTOM_PLAN_TEST_API) {
      await page.getByRole("button", { name: "Suggested format", exact: true }).click();
      await expect(page.getByRole("button", { name: "Use source suggestion", exact: true })).toBeVisible();
      await expect(page.getByText("Source: conditioning_bank.json", { exact: true })).toHaveCount(0);
      await expect(page.getByText("5x5min, 30s rest", { exact: true })).toBeVisible();
      await expect(page.getByLabel("Rounds", { exact: true })).toHaveValue("5");
      await expect(page.getByLabel("Work duration", { exact: true })).toHaveValue("5");
      await page.getByLabel("Dose format").selectOption("reps");
    }
    await page.getByLabel("Sets", { exact: true }).fill("4");
    await page.getByLabel("Reps", { exact: true }).fill("8");
    await page.getByLabel("Rest (sec)", { exact: true }).fill("120");
    await page.getByLabel("Weight", { exact: true }).fill("80");
    const weight = await page.getByLabel('Weight', { exact: true }).boundingBox();
    const unit = await page.getByLabel('Weight unit', { exact: true }).boundingBox();
    expect(weight!.width).toBeGreaterThan(100);
    expect(weight!.x + weight!.width).toBeLessThanOrEqual(unit!.x + 1);
    if (width >= 900) {
      const dialog = page.getByRole('dialog', { name: entries[0].name, exact: true });
      const bounds = await dialog.boundingBox();
      expect(bounds!.width).toBeGreaterThan(900);
      const context = await dialog.getByRole('region', { name: 'Exercise details', exact: true }).boundingBox();
      const prescription = await dialog.getByRole('region', { name: 'Exercise prescription', exact: true }).boundingBox();
      expect(context!.x + context!.width).toBeLessThan(prescription!.x);
      expect((await new AxeBuilder({ page }).include('[role="dialog"]').analyze()).violations).toEqual([]);
    }
    if (process.env.CUSTOM_QA_DIR) await page.screenshot({ path: `${process.env.CUSTOM_QA_DIR}/custom-dose-${width}.png` });
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await page.getByRole("button", { name: "Add exercise to Strength", exact: true }).click();
    await page.getByRole("button", { name: "Create my exercise", exact: true }).click();
    await page.getByLabel("Exercise name", { exact: true }).fill("My bag rounds");
    await expect(page.getByLabel("equipment (optional)", { exact: true })).toHaveCount(0);
    await expect(page.getByLabel("notes (optional)", { exact: true })).toHaveCount(0);
    await page.getByLabel("Dose format").selectOption("intervals");
    await page.getByLabel("Rounds", { exact: true }).fill("5");
    await page.getByLabel("Work duration unit").selectOption("60");
    await page.getByLabel("Work duration", { exact: true }).fill("5");
    await page.getByLabel("Rest between (sec)").fill("30");
    await page.getByRole("button", { name: "Add exercise", exact: true }).click();
    await page.getByRole("button", { name: "Edit dose for My bag rounds", exact: true }).click();
    await page.getByLabel("Position in session").selectOption("0");
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await page.getByRole("button", { name: "Save draft", exact: true }).click();
    await expect(page).toHaveURL(/custom\?plan=/);
    await expect(page.getByText("Unsaved changes", { exact: true })).toHaveCount(0);
    await page.getByRole("link", { name: /Saved plans/ }).click();
    const card = page
      .getByRole("region", { name: "Saved plans" })
      .getByRole("link")
      .filter({ hasText: `Weekly strength ${width}` });
    await card.scrollIntoViewIfNeeded();
    await card.click();
    await expect(page.getByRole("heading", { level: 1 })).toBeInViewport();
    await expect(page.getByRole("heading", { level: 1 })).toBeFocused();
    await page.reload();
    const exercises = page.getByRole("region", { name: "Session 1", exact: true }).getByRole("listitem");
    await expect(exercises.first()).toContainText("My bag rounds");
    await expect(exercises.last()).toContainText("4 × 8 · 80 kg · rest 120s");
    await expect(page.getByText("Monday", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Edit dose for My bag rounds" }).click();
    await expect(page.getByLabel("Work duration", { exact: true })).toHaveValue("5");
    await page.getByLabel("Exercise name").fill("My renamed rounds");
    page.once("dialog", (dialog) => dialog.dismiss());
    await page.goBack();
    await expect(page.getByLabel("Exercise name")).toHaveValue("My renamed rounds");
    await page.getByRole("button", { name: "Done", exact: true }).click();
    page.once("dialog", (dialog) => dialog.dismiss());
    await page.getByRole("link", { name: /Saved plans/ }).click();
    await expect(page.getByLabel("Plan name")).toBeVisible();
    page.once("dialog", (dialog) => dialog.dismiss());
    await page.goBack();
    await expect(page.getByText("My renamed rounds", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Save draft", exact: true }).click();
    // Wait for the saved result, including release of the unsaved-history guard.
    // "Unsaved changes" is only part of a status line, never an exact text node.
    await expect(page.getByRole("status").filter({ hasText: /Plan saved\.|Draft saved\./ })).toBeVisible();
    await expect(page.getByRole("status").filter({ hasText: "Unsaved changes" })).toHaveCount(0);
    await page.goBack();
    await expect(page).toHaveURL(/\/plans(?:#custom-plans)?$/);
    await page.goForward();
    await expect(page.getByText("My renamed rounds", { exact: true })).toBeVisible();
    await page.screenshot({
      path: test.info().outputPath(`custom-builder-${width}.png`),
      animations: "disabled",
      fullPage: true,
    });
    if (process.env.CUSTOM_QA_DIR) await page.screenshot({ path: `${process.env.CUSTOM_QA_DIR}/custom-builder-${width}.png`, fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (width >= 1280) {
      const bounds = await page.getByRole('region', { name: 'Custom training plan builder', exact: true }).boundingBox();
      expect(bounds!.width).toBeGreaterThan(880);
    }
    const saveBounds = await page.getByRole("button", { name: "Save draft", exact: true }).boundingBox();
    expect(saveBounds!.y + saveBounds!.height).toBeLessThan(width < 768 ? 780 : 844);
    expect(
      (await new AxeBuilder({ page }).include('[aria-label="Custom training plan builder"]').analyze()).violations,
    ).toEqual([]);
    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "Delete plan", exact: true }).click();
    await expect(
      page
        .getByRole("region", { name: "Saved plans" })
        .getByRole("link")
        .filter({ hasText: `Weekly strength ${width}` }),
    ).toHaveCount(0);
    expect(
      mutations.filter((path) => path !== "/api/xp/activity").every((path) => path.startsWith("/api/custom-plans")),
    ).toBe(true);
    expect(errors).toEqual([]);
  });
}

for (const width of [320, 390, 1280, 1600]) {
  test(`Private video clip selection, playback and persistence (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    const { saved } = await setup(page, baseURL!);
    const media = { id: "00000000-0000-4000-8000-000000000099", provider: "youtube", video_id: "abcdefghijk",
      start_s: 10, end_s: 25, asset_id: null, channel_title: "Coach", orientation: "landscape", url: null };
    let verified = false;
    await page.route("**/api/private-media/**", async route => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/settings")) return route.fulfill({ json: { uploads_available: false, youtube_available: true } });
      if (path.endsWith("/youtube")) {
        expect(route.request().postDataJSON()).toEqual({ url: "https://youtube.com/watch?v=abcdefghijk", start_s: 10, end_s: 25 });
        verified = true;
      }
      return route.fulfill({ json: media });
    });
    // Deterministic offline IFrame API: verify the existing player receives the selected segment.
    await page.route("https://www.youtube.com/iframe_api", route => route.fulfill({ contentType: "text/javascript", body: `
      window.YT = { PlayerState: { PLAYING: 1, PAUSED: 2, ENDED: 0 }, Player: function(target, options) {
        window.__clipOptions = options;
        let time = options.playerVars.start;
        this.playVideo = () => options.events.onStateChange({ data: 1, target: this });
        this.pauseVideo = () => options.events.onStateChange({ data: 2, target: this });
        this.seekTo = value => { time = value; window.__clipSeek = value; window.__clipTime = value; };
        this.getCurrentTime = () => window.__clipTime ?? time;
        this.setPlaybackRate = () => {}; this.mute = () => {}; this.unMute = () => {}; this.destroy = () => {};
        setTimeout(() => options.events.onReady({ target: this }), 0);
      }}; window.onYouTubeIframeAPIReady?.();
    ` }));
    await page.goto("/plans/custom");
    await page.getByRole("button", { name: "+ Add Session", exact: true }).click();
    await page.getByRole("button", { name: "Add exercise to Training", exact: true }).click();
    await page.getByRole("button", { name: "Create my exercise", exact: true }).click();
    await page.getByLabel("Exercise name", { exact: true }).fill("My defensive drill");
    await page.getByRole("button", { name: "Add YouTube demo (optional)", exact: true }).click();
    const sheet = page.getByRole("dialog", { name: "Demonstration video", exact: true });
    await expect(sheet.getByRole("button", { name: "Upload video" })).toHaveCount(0);
    await sheet.getByLabel("YouTube link", { exact: true }).fill("https://youtube.com/watch?v=abcdefghijk");
    await sheet.getByLabel("Start (seconds)").fill("10");
    await sheet.getByLabel("End (seconds)").fill("50");
    await sheet.getByRole("button", { name: "Preview clip", exact: true }).click();
    await expect(sheet.getByRole("alert")).toContainText("between 1 and 30 seconds");
    expect(verified).toBe(false);
    await sheet.getByLabel("End (seconds)").fill("25");
    await sheet.getByRole("button", { name: "Preview clip", exact: true }).click();
    await sheet.getByRole("button", { name: "Play My defensive drill demo", exact: true }).click();
    await expect(sheet.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
    expect(await page.evaluate(() => {
      const options = (window as unknown as { __clipOptions: { host: string; playerVars: { start: number; end: number; playsinline: number } } }).__clipOptions;
      return { host: options.host, start: options.playerVars.start, end: options.playerVars.end, inline: options.playerVars.playsinline };
    })).toEqual({ host: "https://www.youtube-nocookie.com", start: 10, end: 25, inline: 1 });
    await sheet.getByRole("button", { name: "Replay", exact: true }).click();
    expect(await page.evaluate(() => (window as unknown as { __clipSeek: number }).__clipSeek)).toBe(10);
    await page.evaluate(() => { (window as unknown as { __clipTime: number }).__clipTime = 25; });
    await expect.poll(() => page.evaluate(() => (window as unknown as { __clipTime: number }).__clipTime)).toBe(10);
    expect(await sheet.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    if (width >= 1024) {
      const settings = await sheet.getByText("Optional · YouTube clip up to 30 seconds · Only you can see it", { exact: true }).boundingBox();
      const preview = await sheet.getByRole("button", { name: "Use video", exact: true }).boundingBox();
      expect(preview!.x).toBeGreaterThan(settings!.x + settings!.width);
      expect((await sheet.boundingBox())!.width).toBe(720);
    }
    await page.screenshot({ path: `${process.env.TEMP ?? "/tmp"}/unlxck-private-video-${width}.png`, fullPage: false });
    await sheet.getByRole("button", { name: "Use video", exact: true }).click();
    await page.getByRole("button", { name: "Add exercise", exact: true }).click();
    if (width >= 1024) {
      await page.getByRole("button", { name: "+ Add Session", exact: true }).click();
      const first = await page.getByRole("region", { name: "Session 1", exact: true }).boundingBox();
      const second = await page.getByRole("region", { name: "Session 2", exact: true }).boundingBox();
      if (width >= 1600) {
        expect(second!.x).toBeGreaterThan(first!.x + first!.width);
        expect(second!.y).toBe(first!.y);
      } else {
        expect(second!.y).toBeGreaterThanOrEqual(first!.y + first!.height);
      }
      await page.screenshot({ path: `${process.env.TEMP ?? "/tmp"}/unlxck-private-builder-${width}.png`, fullPage: true });
    }
    await page.getByRole("button", { name: "Save draft", exact: true }).click();
    await expect(page).toHaveURL(/custom\?plan=/);
    expect([...saved.values()][0].sessions[0].exercises[0]).toMatchObject({ origin: "manual", media_id: media.id });
    await page.reload();
    await page.getByRole("button", { name: "Edit dose for My defensive drill", exact: true }).click();
    await expect(page.getByRole("button", { name: "Replace video", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Remove video", exact: true }).click();
    await expect(page.getByRole("button", { name: "Add YouTube demo (optional)", exact: true })).toBeVisible();
    expect(errors).toEqual([]);
  });
}

test("Saved-plan failures are visible and retryable", async ({ page, baseURL }) => {
  await setup(page, baseURL!);
  const id = "00000000-0000-4000-8000-000000000099";
  await page.route(`**/api/custom-plans/${id}`, (route) =>
    route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Custom plan not found." }),
    }),
  );
  await page.goto(`/plans/custom?plan=${id}`);
  await expect(page.getByRole("alert").filter({ hasText: "Custom plan not found" })).toContainText(
    "Custom plan not found",
  );
  await expect(page.getByRole("button", { name: "Retry opening plan" })).toBeVisible();
  await expect(page.getByLabel("Plan name")).toHaveCount(0);
  await page.route(`**/api/custom-plans/${id}`, (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        id,
        name: "Demo Plan",
        kind: "self_directed",
        sessions: [],
        incomplete: true,
        created_at: "2026-10-11T12:00:00Z",
        updated_at: "2026-10-11T12:00:00Z",
      }),
    }),
  );
  await page.getByRole("button", { name: "Retry opening plan" }).click();
  await expect(page.getByRole("heading", { name: "Demo Plan", exact: true })).toBeVisible();
  await expect(page.getByRole("alert").filter({ hasText: "Custom plan not found" })).toHaveCount(0);
  await page.route("**/api/custom-plans", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Custom plans temporarily unavailable." }),
    }),
  );
  await page.goto("/plans");
  await expect(page.getByRole("region", { name: "Saved plans" }).getByRole("alert")).toContainText(
    "temporarily unavailable",
  );
  await expect(page.getByRole("button", { name: "Retry custom plans" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Log session", exact: true })).toBeVisible();
  await page.route("**/api/custom-plans", (route) => route.fulfill({ contentType: "application/json", body: "[]" }));
  await page.getByRole("button", { name: "Retry custom plans" }).click();
  await expect(page.getByText("No saved plans yet.")).toBeVisible();
});

for (const width of [390, 1280]) {
  test(`Sessions stay in calendar order before save and after reopening (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const { saved } = await setup(page, baseURL!);
    await page.goto('/plans/custom');
    const days = ['Sunday', 'Friday', 'Monday', 'Wednesday', 'Tuesday', 'Thursday', 'Saturday', 'Monday', null];
    for (const [index, day] of days.entries()) {
      await page.getByRole('button', { name: '+ Add Session', exact: true }).click();
      await page.getByRole('button', { name: `Edit session ${index + 1}`, exact: true }).click();
      await page.getByLabel('Session name', { exact: true }).fill(`Day ${index}`);
      await page.getByLabel('Day', { exact: true }).selectOption(day ?? '');
      await page.getByRole('button', { name: 'Done', exact: true }).click();
    }
    const expected = ['Day 2', 'Day 7', 'Day 4', 'Day 3', 'Day 5', 'Day 1', 'Day 6', 'Day 0', 'Day 8'];
    // One day is shown at a time, so check the week through the day strip plus the unassigned group.
    const tiles = ['Monday, Day 2 / Day 7', 'Tuesday, Day 4', 'Wednesday, Day 3', 'Thursday, Day 5', 'Friday, Day 1', 'Saturday, Day 6', 'Sunday, Day 0'];
    const week = page.getByRole('group', { name: 'Weekly training calendar' }).getByRole('button');
    const unassigned = page.getByRole('group', { name: 'Unassigned sessions' }).getByRole('heading', { level: 2 });
    const checkWeek = async () => {
      for (const [index, name] of tiles.entries()) await expect(week.nth(index)).toHaveAccessibleName(name);
      await expect(unassigned).toHaveText(['Day 8']);
    };
    await checkWeek();
    await page.getByRole('button', { name: 'Save draft', exact: true }).click();
    await expect(page).toHaveURL(/custom\?plan=/);
    expect([...saved.values()][0].sessions.map(session => session.name)).toEqual(expected);
    await page.reload();
    await checkWeek();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}


for (const width of [320, 390, 1280]) {
  test(`AI and custom plans share one saved list (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const { saved } = await setup(page, baseURL!);
    const publishedDraft = "00000000-0000-4000-8000-000000000011";
    saved.set(publishedDraft, { id: publishedDraft, name: "Already published", kind: "self_directed",
      incomplete: false, sessions: [], created_at: "2026-10-11T12:00:00Z", updated_at: "2026-10-11T12:00:00Z" });
    const draftId = "00000000-0000-4000-8000-000000000012";
    saved.set(draftId, { id: draftId, name: "My custom strength and conditioning week with a longer title",
      kind: "self_directed", incomplete: true, sessions: [], created_at: "2026-10-12T12:00:00Z",
      updated_at: "2026-10-12T12:00:00Z" });
    const base = { athlete_id: "00000000-0000-4000-8000-000000000001", full_name: "Carlos", fight_date: "",
      technical_style: ["boxing"], created_at: "2026-10-11T12:00:00Z", status: "ready", activation_state: "eligible" };
    const active = { ...base, plan_id: "active-custom", plan_name: "My active week", custom_plan_id: "active-draft" };
    await page.route("**/api/plans", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify([
      active, { ...base, plan_id: "custom-week", plan_name: "Published custom week", custom_plan_id: publishedDraft },
      { ...base, plan_id: "ai-week", plan_name: "Boxing performance block" },
    ]) }));
    await page.route("**/api/plans/active", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify(active) }));
    await page.goto("/plans");
    const list = page.getByRole("region", { name: "Saved plans", exact: true });
    await expect(list.getByRole("listitem")).toHaveCount(3);
    await expect(list.getByText("Custom", { exact: true })).toHaveCount(2);
    await expect(list.getByText("3 saved", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Custom plans", exact: true })).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Edit Plan", exact: true })).toHaveAttribute("href", "/plans/custom?plan=active-draft");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    // Plan has no creation menu or History shortcut of its own: those live in + Create and the tab bar.
    await expect(page.getByRole("link", { name: "View History" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Log session", exact: true })).toBeVisible();
    await list.scrollIntoViewIfNeeded();
    const accessibility = await new AxeBuilder({ page }).include('section[aria-label="Saved plans"]').analyze();
    expect(accessibility.violations).toEqual([]);
    await list.getByRole("link").click();
    await expect(page).toHaveURL(new RegExp(`plans/custom\\?plan=${draftId}`));
    expect(errors).toEqual([]);
  });
}
