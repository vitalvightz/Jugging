import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { isolateFromNetwork } from "./support";
import { asOf, auth, fixtureResponse, performances, sessions } from "./fixtures/training-progress";

async function setup(page: Page, baseURL: string) {
  await isolateFromNetwork(page, baseURL);
  await page.clock.setFixedTime(new Date(asOf));
  const key = `sb-${new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").hostname.split(".")[0]}-auth-token`;
  await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [key, JSON.stringify(auth)]);
  const requests: string[] = [];
  await page.route("**/api/**", route => {
    const url = new URL(route.request().url()); requests.push(url.pathname + url.search);
    const result = fixtureResponse(url);
    return route.fulfill({ status: result.status ?? 200, contentType: "application/json", body: JSON.stringify(result.body) });
  });
  return requests;
}

for (const width of [390, 1280]) {
  test(`Training Rhythm: complete data, drill-down and single-modal focus at ${width}px`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const requests = await setup(page, baseURL!);
    const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
    await page.goto("/history");
    await page.locator("details.history-trends > summary").click();
    const preview = page.getByRole("region", { name: "Training Rhythm preview" });
    await expect(preview.getByText("Select a week to see its training.")).toBeVisible();
    expect(requests.some(url => url.includes("before_day="))).toBe(true);
    await preview.getByRole("button", { name: "View trends" }).click();
    const trends = page.getByRole("dialog", { name: "Training Rhythm" });
    await expect(trends).toBeVisible();
    await trends.getByRole("button", { name: "4 weeks", exact: true }).click();
    await trends.getByRole("button", { name: "Minutes", exact: true }).click();
    await expect(trends).toContainText("Missing durations: 2 last completed week; 2 previous week.");
    const list = trends.getByRole("list", { name: "Sessions in selected week" });
    await expect(list.getByRole("listitem")).toHaveCount(5);
    await expect(list.locator('[data-tone="amber"]')).toHaveCount(2); // row and status badge
    const open = list.getByRole("button").filter({ hasText: "Strength & Power" });
    await open.click();
    const details = page.getByRole("dialog", { name: "Strength & Power" });
    await expect(details).toBeVisible();
    await expect(page.locator("dialog[open]")).toHaveCount(1);
    await expect(trends).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(trends).toBeVisible(); await expect(open).toBeFocused();
    await expect(trends.getByRole("button", { name: "4 weeks", exact: true })).toHaveAttribute("aria-pressed", "true");
    await expect(trends.getByRole("button", { name: "Minutes", exact: true })).toHaveAttribute("aria-pressed", "true");
    if (process.env.PROGRESS_QA_DIR) {
      await trends.getByRole("region", { name: "Training trends", exact: true }).evaluate(element => { element.scrollTop = 0; });
      await page.screenshot({ path: `${process.env.PROGRESS_QA_DIR}/rhythm-${width}.png` });
    }
    await trends.getByLabel("Discipline", { exact: true }).selectOption("boxing");
    await expect(list.getByRole("listitem")).toHaveCount(1);
    expect((await new AxeBuilder({ page }).include("dialog[open]").analyze()).violations).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await page.keyboard.press("Escape"); await expect(preview.getByRole("button", { name: "View trends" })).toBeFocused();
    expect(errors).toEqual([]);
  });

  test(`Exercise Progress: exact records, gaps, earlier pages and sparse states at ${width}px`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 }); await setup(page, baseURL!);
    await page.goto("/history"); await page.getByRole("tab", { name: "Exercises", exact: true }).click();
    await page.getByRole("button", { name: "View Back Squat performance history", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Back Squat", exact: true });
    const progress = dialog.getByRole("region", { name: "Exercise progress", exact: true });
    await expect(progress.getByRole("slider", { name: "Inspect performance" })).toBeVisible();
    await progress.getByRole("group", { name: "Progress measurement" }).getByRole("button", { name: "Reps", exact: true }).click();
    await expect(progress.locator('[aria-label="Selected performance"]')).toContainText("5 reps");
    await progress.getByRole("group", { name: "Progress measurement" }).getByRole("button", { name: "Load · kg" }).click();
    await progress.getByRole("slider", { name: "Inspect performance" }).focus(); await page.keyboard.press("ArrowLeft");
    const selected = progress.locator('[aria-label="Selected performance"]');
    await expect(selected).toContainText("95 kg"); await expect(selected).toContainText("Sat 03 Oct 2026");
    await expect(selected).toContainText("3 ×");
    await selected.getByRole("button", { name: "Show in history" }).click();
    await expect(dialog.locator('[data-performance="p5"]')).toBeFocused();
    await expect(dialog.locator('[data-performance="p5"]').getByRole("button", { expanded: true })).toBeVisible();
    await dialog.getByRole("button", { name: "Load earlier performances" }).click();
    if (width < 1100) await dialog.getByRole("tab", { name: "Progress" }).click();
    await expect(progress).toContainText("6 recorded loads");
    // Records without a load are excluded from the series rather than splitting the line.
    expect(await progress.locator("svg path").last().getAttribute("d")).toMatch(/^M[^M]*$/);
    await progress.getByRole("slider").focus(); await page.keyboard.press("Home");
    await expect(selected).toContainText("80 kg");
    expect((await new AxeBuilder({ page }).include("dialog[open]").analyze()).violations).toEqual([]);
    if (process.env.PROGRESS_QA_DIR) {
      await dialog.getByRole("region", { name: "Exercise performance record", exact: true }).evaluate(element => { element.scrollTop = 0; });
      await page.screenshot({ path: `${process.env.PROGRESS_QA_DIR}/exercise-${width}.png` });
    }
    await page.keyboard.press("Escape");
    for (const [name, count] of [["Plank", 2], ["Push-ups", 1], ["Unmeasured hold", 0]] as const) {
      await page.getByRole("button", { name: `View ${name} performance history`, exact: true }).click();
      const sparse = page.getByRole("dialog", { name, exact: true }).getByRole("region", { name: "Exercise progress", exact: true });
      await expect(sparse).toBeVisible(); await expect(sparse.getByRole("slider")).toHaveCount(0);
      if (count) await expect(sparse).toContainText(`${count} recorded `);
      else await expect(sparse).toContainText("No comparable measurements");
      await page.keyboard.press("Escape");
    }
  });
}

test("incomplete weekly reads retry without showing partial trends or inventing zero minutes", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 844 }); await setup(page, baseURL!);
  let fail = true;
  await page.route("**/api/training/history?**", route => {
    const url = new URL(route.request().url());
    if (url.searchParams.has("before_day") && fail) { fail = false; return route.fulfill({ status: 503, json: { detail: "unavailable" } }); }
    return route.fulfill({ json: fixtureResponse(url).body });
  });
  await page.goto("/history");
  await page.locator("details.history-trends > summary").click();
  const preview = page.getByRole("region", { name: "Training Rhythm preview" });
  await expect(preview.getByRole("alert")).toBeVisible();
  await expect(preview.getByText("Select a week to see its training.")).toHaveCount(0);
  await preview.getByRole("button", { name: "Retry trends" }).click();
  await expect(preview.getByText("Select a week to see its training.")).toBeVisible();
  await preview.getByRole("button", { name: "View trends" }).click();
  const dialog = page.getByRole("dialog", { name: "Training Rhythm" });
  await dialog.getByRole("button", { name: "Minutes", exact: true }).click();
  await dialog.getByRole("combobox", { name: "Discipline", exact: true }).selectOption("unlxck");
  await expect(dialog).toContainText("Recorded time unavailable");
  await expect(dialog).toContainText("Training time comparison unavailable. Neither week has recorded durations.");
  await expect(dialog).not.toContainText("No change in recorded minutes");
  if (process.env.PROGRESS_QA_DIR) await page.screenshot({ path: `${process.env.PROGRESS_QA_DIR}/untimed-390.png` });
});

test("exercise units never combine and point selection survives an earlier-page failure", async ({ page, baseURL }) => {
  await setup(page, baseURL!);
  let fail = true;
  const poundValues = { sets: 3, reps: 5, load: { value: 175, unit: "lb" } };
  const pound = { ...performances[0], id: "pounds", training_day: "2026-08-01", performed_values: poundValues, block: { ...performances[0].block, actual: poundValues } };
  await page.route("**/api/history/exercise-performances?**", route => {
    const url = new URL(route.request().url());
    if (url.searchParams.has("cursor")) {
      if (fail) { fail = false; return route.fulfill({ status: 503, json: { detail: "unavailable" } }); }
      return route.fulfill({ json: { performances: [pound], next_cursor: null } });
    }
    return route.fulfill({ json: fixtureResponse(url).body });
  });
  await page.goto("/history"); await page.getByRole("tab", { name: "Exercises", exact: true }).click();
  await page.getByRole("button", { name: "View Back Squat performance history", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Back Squat", exact: true });
  const progress = dialog.getByRole("region", { name: "Exercise progress", exact: true });
  await progress.getByRole("slider", { name: "Inspect performance" }).focus(); await page.keyboard.press("ArrowLeft");
  await dialog.getByRole("button", { name: "Load earlier performances" }).click();
  await expect(dialog.getByRole("alert")).toBeVisible();
  await expect(progress.locator('[aria-label="Selected performance"]')).toContainText("95 kg");
  await dialog.getByRole("button", { name: "Retry", exact: true }).click();
  await progress.getByRole("group", { name: "Progress measurement" }).getByRole("button", { name: "Load · lb" }).click();
  await expect(progress.locator('[aria-label="Selected performance"]')).toContainText("175 lb");
  await expect(progress.getByRole("slider")).toHaveCount(0);
});

test("an empty recorded-training window has a factual empty state", async ({ page, baseURL }) => {
  await setup(page, baseURL!);
  await page.route("**/api/training/history?**", route => route.fulfill({ json: { sessions: sessions.slice(0, 1).map(row => ({ ...row, training_day: "2025-01-01" })), next_before_day: null } }));
  await page.goto("/history");
  await page.locator("details.history-trends > summary").click();
  const preview = page.getByRole("region", { name: "Training Rhythm preview" });
  await expect(preview).toContainText("No sessions recorded in this period.");
  await preview.getByRole("button", { name: "View trends" }).click();
  const dialog = page.getByRole("dialog", { name: "Training Rhythm" });
  await expect(dialog).toContainText("No sessions recorded in this period.");
  await expect(dialog.getByRole("button", { name: /sessions;|sessions$/ })).toHaveCount(0);
  if (process.env.PROGRESS_QA_DIR) await page.screenshot({ path: `${process.env.PROGRESS_QA_DIR}/empty-rhythm-1280.png` });
});
