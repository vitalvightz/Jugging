import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
const markup = execFileSync(process.execPath, ["--import", "tsx", "--import", "./test/register-css-stub.mjs",
  "test/render-mental-training.mjs"], { encoding: "utf8" });
const css = readFileSync("app/globals.css", "utf8");
for (const width of [375, 1280]) {
  test(`mental tasks keep contact and all authored instructions visible at ${width}px`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.setViewportSize({ width, height: 850 });
    await page.setContent(`<html lang="en" data-theme="dark"><head><title>Mental training QA</title></head><body><main style="max-width:800px;margin:auto;padding:16px"><h1>Training</h1><h2>Today</h2>${markup}</main></body></html>`);
    await page.addStyleTag({ content: css });
    await expect(page).toHaveTitle("Mental training QA");
    await expect(page.getByText("Hard sparring", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Fight Visualisation · 5 min", exact: true })).toBeVisible();
    // Tactical analysis/reflection are prompt-driven: legacy synthetic doses
    // must not become timers, and the original session names stay intact.
    await expect(page.getByRole("heading", { name: "Tactical Focus", exact: true })).toBeVisible();
    const reflection = page.locator('[data-mental-category="reflect"] > details');
    await expect(reflection).not.toHaveAttribute("open");
    await expect(reflection.locator("summary")).toContainText("Self-Review Cues");
    await reflection.locator("summary").click();
    await expect(reflection).toHaveAttribute("open");
    await expect(page.getByRole("heading", { name: "Self-Review Cues", exact: true })).toBeVisible();
    for (const category of ["analyse", "reflect"]) {
      const task = page.locator(`[data-mental-category="${category}"]`);
      await expect(task.getByRole("heading")).not.toContainText(/min/);
      if (category === "analyse") await expect(task.locator("details")).toHaveCount(0);
    }
    await expect(page.locator('[data-mental-category="reflect"] li')).toHaveText(
      "Review the last clean technical work. Write three cues only: one entry, one defensive reset, one composure cue.");
    const task = page.locator('[data-mental-category="visualise"]');
    await expect(task.locator("details")).toHaveCount(0);
    await expect(task.getByRole("listitem")).toHaveText("Enter behind your jab, cut off the exit and reset calmly.");
    await expect(page.locator('[data-mental-category="analyse"] li')).toHaveText(
      "Review how your opponent exits after the jab. Choose one adjustment for the next session.");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
    if (process.env.MENTAL_QA_SCREENSHOTS) await page.screenshot({ path: `${process.env.MENTAL_QA_SCREENSHOTS}/mental-${width}.png`, fullPage: true });
  });
}
