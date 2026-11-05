import { expect, test } from "@playwright/test";
import { mockAthlete } from "./request-journeys";

test.use({ contextOptions: { reducedMotion: "reduce" } });

for (const width of [390, 1280]) {
  test(`loading geometry at ${width}px`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 900 });
    await mockAthlete(page, baseURL!, { latencyFor: (_method, path) => path === "/api/today" ? 1800 : 0 });
    await page.goto("/today");
    await expect(page.getByRole("heading", { name: "Today", exact: true })).toBeVisible();
    await expect(page.getByLabel("Loading Today")).toHaveCount(0);
    await page.locator('nav a[href="/"]:visible').first().click();
    const pending = page.getByText("Checking today’s training decision…");
    await expect(pending).toBeVisible();
    const companion = page.locator(".overview-primary-session");
    // Let the existing 560ms page entrance finish before measuring the swap.
    await page.waitForTimeout(650);
    const before = await companion.boundingBox();
    await page.screenshot({ path: `/tmp/home-loading-${width}.png` });
    await expect(page.locator(".overview-primary-action")).toBeVisible();
    const after = await companion.boundingBox();
    expect(Math.abs(after!.y - before!.y)).toBeLessThan(2);
    await page.screenshot({ path: `/tmp/home-loaded-${width}.png` });
    await page.goto("/today");
    const loading = page.getByLabel("Loading Today");
    await expect(loading).toBeVisible();
    await page.screenshot({ path: `/tmp/today-loading-${width}.png` });
    await expect(loading).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "Today", exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.waitForTimeout(220); // Capture after the opacity reveal.
    await page.screenshot({ path: `/tmp/today-loaded-${width}.png` });
  });
}
