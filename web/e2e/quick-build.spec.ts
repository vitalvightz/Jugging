import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { isolateFromNetwork } from "./support";

async function setup(page: Page, baseURL: string) {
  await isolateFromNetwork(page, baseURL);
  const storageKey = `sb-${new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").hostname.split(".")[0]}-auth-token`;
  await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [storageKey, JSON.stringify({
    access_token: "quick-build-test-token", refresh_token: "fixture", token_type: "bearer",
    expires_at: Math.floor(Date.now() / 1000) + 86400,
    user: { id: "athlete-1", email: "fighter@example.com", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} },
  })]);
  const writes: Record<string, unknown>[] = [];
  await page.route("**/api/**", async route => {
    const endpoint = new URL(route.request().url()).pathname;
    if (endpoint === "/api/onboarding/draft") writes.push(route.request().postDataJSON());
    await route.fulfill({ json: endpoint === "/api/me" ? {
      profile: { athlete_id: "athlete-1", full_name: "Alex", role: "athlete", access_status: "approved",
        technical_style: ["boxing"], tactical_style: [], athlete_timezone: "Europe/London", athlete_locale: "en",
        appearance_mode: "dark", nutrition_profile: {}, date_of_birth: "1995-01-01", meets_minimum_age: true,
        is_minor: false, terms_accepted: true, health_consent_granted: true },
      plan_count: 0, latest_plan: null, latest_intake: null, username_rate_limit: {},
    } : { notifications: [], unread_count: 0 } });
  });
  page.on("dialog", dialog => dialog.accept());
  await page.goto("/quick-build");
  await expect(page.getByRole("heading", { name: /^Generate your plan in/ })).toBeVisible();
  return writes;
}

async function expectFits(page: Page, width: number) {
  // iOS expands innerWidth along with the layout viewport when content is too
  // wide. Compare to the requested device width, not just innerWidth.
  const sizes = await page.evaluate(() => ({
    viewport: window.innerWidth, document: document.documentElement.scrollWidth,
    form: document.querySelector(".quick-build-form")!.getBoundingClientRect().width,
  }));
  expect(sizes.viewport).toBeLessThanOrEqual(width);
  expect(sizes.document).toBeLessThanOrEqual(width);
  expect(sizes.form).toBeLessThanOrEqual(width);
}

for (const width of [320, 390, 430, 1280]) {
  test(`Quick Build presets keep their layout and selections at ${width}px`, async ({ browser, baseURL }) => {
    const context = await browser.newContext({ viewport: { width, height: 844 }, isMobile: width < 720, hasTouch: width < 720 });
    const page = await context.newPage();
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    try {
      const writes = await setup(page, baseURL!);
      await expect(page.locator("#qb-weekly-frequency")).toHaveText("Choose training days first");
      await expect(page.locator("#qb-weekly-frequency")).toBeDisabled();
      await expectFits(page, width);
      for (const [name, frequency, equipment] of [
        ["Power build", "5", "Full equipment access"], ["Balanced camp", "4", "Basic gym"],
        ["Speed camp", "4", "Basic gym"], ["Home control", "3", "Home kit"],
      ]) {
        const starter = page.getByRole("button", { name: new RegExp(`^${name}`) });
        await starter.click();
        await expect(starter).toHaveAttribute("aria-pressed", "true");
        await expect(page.locator("#qb-weekly-frequency")).toHaveText(frequency);
        await expect(page.locator("#qb-equipment-preset")).toContainText(equipment);
        await expectFits(page, width);
      }
      // Clearing a starter clears the schedule and no longer displays a stale count.
      await page.getByRole("button", { name: /^Home control/ }).click();
      await expect(page.locator("#qb-weekly-frequency")).toHaveText("Choose training days first");
      await page.getByRole("button", { name: /^Power build/ }).click();
      // Exercise each picker, including full descriptions in the mobile sheet.
      for (const [id, option, selected] of [
        ["qb-training-preset", /^3 days/, /3 days/],
        ["qb-equipment-preset", /^Home kit/, /Home kit/],
        ["qb-equipment-preset", /^Full equipment access/, /Full equipment access/],
        ["qb-focus-preset", /^Gas tank/, /Gas tank/],
      ]) {
        await page.locator(`#${id}`).click();
        await expectFits(page, width);
        await page.getByRole("option", { name: option as RegExp }).click();
        await expect(page.getByRole("listbox")).toHaveCount(0);
        await expect(page.locator(`#${id}`)).toContainText(selected as RegExp);
        await expect(page.locator(`#${id}`)).toBeFocused();
        await expectFits(page, width);
        await expect.poll(() => page.evaluate(() => document.body.style.position)).toBe("");
      }
      await page.getByRole("button", { name: /^Power build/ }).click();
      await page.getByRole("checkbox", { name: /No scheduled fight/ }).check();
      await page.locator(".quick-build-equipment-details summary").click();
      await expectFits(page, width);
      await page.locator("#qb-injuries").fill("Sometimes my left knee hurts");
      const impact = page.getByRole("group", { name: "How much is it affecting you?" });
      await expect(impact.getByRole("button")).toHaveCount(3);
      await impact.getByRole("button", { name: "Not limiting me", exact: true }).click();
      await expect(impact.getByRole("button", { name: "Not limiting me", exact: true })).toHaveAttribute("aria-pressed", "true");
      await impact.getByRole("button", { name: "Limiting me", exact: true }).focus();
      await page.keyboard.press("Space");
      await expect(impact.getByRole("button", { name: "Limiting me", exact: true })).toHaveAttribute("aria-pressed", "true");
      await impact.getByRole("button", { name: "Not limiting me", exact: true }).click();
      const accessibility = await new AxeBuilder({ page }).include(".injury-severity-chips").analyze();
      expect(accessibility.violations).toEqual([]);
      await impact.screenshot({ path: `/tmp/quick-build-impact-${width}.png` });
      await expectFits(page, width);
      await page.screenshot({ path: `/tmp/quick-build-${width}.png`, fullPage: true });
      await page.getByRole("button", { name: "Generate Plan", exact: true }).click();
      await expect(page).toHaveURL(/\/generate$/);
      expect(writes).toHaveLength(1);
      expect(writes[0].onboarding_draft).toMatchObject({ plan_source: "quick_build", equipment_preset: "full_gym", training_preset: "five_days", focus_preset: "explosive_power", weekly_training_frequency: 5 });
      expect(writes[0].onboarding_draft).toMatchObject({ injuries: "Sometimes my left knee hurts [training_impact:not_limiting]" });
      expect(errors).toEqual([]);
    } finally {
      await context.close();
    }
  });
}
