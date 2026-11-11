import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { setup } from "./custom-plan-fixture";
import { trainingPlan, completions } from "../test/training-plan-fixture";
import type { PlanDetail } from "../lib/types";

const plan: PlanDetail = {
  plan_id: "premium-plan", athlete_id: "00000000-0000-4000-8000-000000000001", full_name: "Carlos",
  plan_name: "Fight camp", fight_date: "2026-12-21", technical_style: ["boxing"], created_at: "2026-10-01T12:00:00Z",
  status: "ready", activation_state: "eligible", advisories: [], structured_card_state: { state: "live", reasons: [] },
  outputs: { plan_text: "# Fight camp", pdf_url: null, structured_plan: trainingPlan,
    exercise_media: { "Landmine Press → Med Ball Chest Pass": { provider: "youtube", video_id: "hQgFixeXdZo", start_s: 27, end_s: 49, source: "curated" } } },
  schedule_context: { schedule_mode: "event_countdown", projection_status: "not_required", current_training_day: "2026-10-28", plan_start_day: "2026-10-01" },
};

for (const width of [320, 375, 390, 430, 1280]) {
  test(`plan rows, safety, sheets, statuses and week navigation (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await setup(page, baseURL!);
    await page.route("**/api/plans/**", route => {
      const path = new URL(route.request().url()).pathname;
      return path.endsWith("/completions")
        ? route.fulfill({ json: { completions, current_training_day: "2026-10-28" } })
        : route.fulfill({ json: plan });
    });
    await page.route("**/api/today", route => route.fulfill({ status: 404, json: { detail: "No daily check-in in this fixture" } }));
    await page.goto("/plans/premium-plan");
    const today = page.locator('.cm-days [data-day-index="2"]');
    await expect(today).toHaveAttribute("open", "");
    await expect(page.locator('.cm-days details[open]')).toHaveCount(1);
    await expect(today.locator(".ex-row")).toHaveCount(4);
    await expect(today.locator(".sp-session-title")).toHaveCSS("animation-name", "none");
    await expect(today.locator(".sp-week-body")).toHaveCSS("animation-name", "plan-day-reveal");
    await page.emulateMedia({ reducedMotion: "reduce" });
    await expect(today.locator(".sp-session-title")).toHaveCSS("animation-name", "none");
    await expect(today.locator(".sp-week-body")).toHaveCSS("animation-name", "none");
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await expect(today).toContainText("4 rounds · 2 min work · 2 min rest");
    await expect(today).toContainText("3 sets × 8 reps · 40 kg · RPE 6");
    await expect(page.locator('.sp-session-toggle')).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Change Back Squat", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Add exercise", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Jump to Today", exact: true }).click();
    await expect(today.locator("summary").first()).toBeFocused();
    await expect.poll(() => today.locator(".sp-week-body").evaluate(el => el.getAnimations().filter(animation => animation.playState === "running").length)).toBe(0);
    await expect.poll(() => today.evaluate(el => Math.abs(el.getBoundingClientRect().top - parseFloat(getComputedStyle(el).scrollMarginTop)))).toBeLessThan(2);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (width < 720) {
      const [headerBottom, targetTop, background] = await page.evaluate(() => {
        const band = document.querySelector('.phone-top-band')!;
        return [band.getBoundingClientRect().bottom, document.querySelector('.cm-day-current')!.getBoundingClientRect().top,
          getComputedStyle(band).backgroundImage];
      });
      expect(Number(targetTop)).toBeGreaterThanOrEqual(Number(headerBottom));
      expect(background).toBe("none");
    }
    if (process.env.PLAN_SCREENSHOTS) await page.screenshot({ path: `${process.env.PLAN_SCREENSHOTS}/plan-${width}.png` });
    const scrollBefore = await page.evaluate(() => scrollY);
    await today.getByRole("button", { name: /^Landmine Press/ }).click();
    const demo = page.getByRole("dialog", { name: /Landmine Press/ });
    await expect(demo.getByRole("button", { name: /^Play .* demo$/ })).toBeVisible();
    await expect(demo.locator(".ex-detail-stop")).toContainText("sharp shoulder pain");
    await expect(demo.locator("iframe")).toHaveCount(0);
    await expect(demo.getByRole("link", { name: /Watch on YouTube/ })).toHaveAttribute("href", /youtube.com/);
    await demo.press("Escape");
    await expect(demo).toHaveCount(0);
    await expect.poll(async () => Math.abs(await page.evaluate(() => scrollY) - scrollBefore)).toBeLessThan(3);
    await today.getByRole("button", { name: "Alternating Bounds", exact: true }).click();
    const noDemo = page.getByRole("dialog", { name: "Alternating Bounds", exact: true });
    await expect(noDemo).toBeVisible(); await noDemo.getByRole("button", { name: "Close", exact: true }).click();
    await expect(noDemo).toHaveCount(0);
    const sparring = page.locator('.cm-days [data-day-index="3"]');
    await sparring.locator("summary").first().click();
    await expect(sparring).toContainText("Hard sparring"); await expect(sparring).toContainText("No additional contact");
    await expect(sparring.locator(".ex-row")).toHaveCount(0);
    const rehab = page.locator('.cm-days [data-day-index="4"]'); await rehab.locator("summary").first().click();
    await expect(rehab.getByText("No contact training.", { exact: true })).toBeVisible();
    await expect(rehab.getByText("Stop if new pain appears or symptoms worsen.")).toHaveCount(0);
    await expect(today.locator(".ex-row-stop")).toHaveCount(0);
    const mental = page.locator('.cm-days [data-day-index="5"]'); await mental.locator("summary").first().click();
    await expect(mental).toContainText("12 min"); await expect(mental.locator(".sp-mental-steps li")).toHaveCount(4);
    await expect(mental).toContainText("Stop if the rehearsal causes distress");
    await expect(mental.getByRole("button", { name: /View instructions/ })).toHaveCount(0);
    if (process.env.PLAN_SCREENSHOTS) await page.screenshot({ path: `${process.env.PLAN_SCREENSHOTS}/mental-${width}.png` });
    const modified = page.locator('.cm-days [data-day-index="1"]'); await modified.locator("summary").first().click();
    await expect(modified).toContainText("Modified");
    await modified.locator(".sp-modification-detail summary").click();
    await expect(modified.locator(".sp-modification-detail p")).toContainText(completions[1].modification_reason!);
    const past = page.locator('.cm-days [data-day-index="0"]'); await past.locator("summary").first().click();
    await expect(past).toContainText("Done"); await expect(past).toContainText("Missed");
    await expect(page.locator(".cm-rest-day")).toHaveCount(1);
    const axe = await new AxeBuilder({ page }).include(".cm-days").analyze();
    expect(axe.violations).toEqual([]);
    await page.locator('.cm-week-pill[data-week-pos="7"]').click();
    await expect(page.locator('.cm-days details[open]')).toHaveCount(0);
    await page.getByRole("button", { name: "Jump to Today", exact: true }).click();
    await expect(today).toHaveAttribute("open", "");
    await today.locator("summary").first().click(); await expect(today).not.toHaveAttribute("open", "");
    await page.getByRole("button", { name: "Jump to Today", exact: true }).click();
    await expect(today).toHaveAttribute("open", "");
    await expect(page.locator(".cm-week-pill-phase")).toHaveCount(0);
    await expect(page.locator("details.sp-redflags")).not.toHaveAttribute("open", "");
    await page.getByRole("button", { name: "Calendar", exact: true }).click();
    const dayPanel = page.locator(".cm-cal-day");
    await expect(dayPanel).toBeVisible();
    const thursday = page.locator("button.cm-cal-cell").filter({ hasText: "Thu 29 Oct" });
    await thursday.click();
    await expect(page.locator(".cm-calendar")).toBeVisible(); await expect(page.locator(".cm-days")).toHaveCount(0);
    await expect(thursday).toHaveAttribute("aria-pressed", "true");
    await expect(dayPanel.locator(".cm-cal-day-title")).toContainText("Thu 29 Oct");
    await expect(dayPanel.locator(".cm-cal-day-title")).toBeInViewport();
    if (process.env.PLAN_SCREENSHOTS) await page.screenshot({ path: `${process.env.PLAN_SCREENSHOTS}/calendar-${width}.png`, fullPage: true });
    await dayPanel.locator(".cm-cal-day-row").first().click();
    const daySheet = page.getByRole("dialog");
    await expect(daySheet).toBeVisible(); await daySheet.press("Escape"); await expect(daySheet).toHaveCount(0);
    await page.getByRole("button", { name: "List", exact: true }).click();
    await expect(page.locator(".cm-days")).toBeVisible();
    await page.evaluate(() => { document.documentElement.style.fontSize = "20px"; });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
  });
}

for (const width of [390, 1280]) test(`Plan Detail uses historical Back Squat recommendations without enabling logs (${width}px)`, async ({ page, baseURL }) => {
  await page.setViewportSize({ width, height: 900 });
  await setup(page, baseURL!);
  const historicalPlan = structuredClone(plan);
  for (const week of historicalPlan.outputs.structured_plan!.weeks ?? [])
    for (const day of week.days ?? []) for (const session of day.sessions ?? [])
      for (const block of session.blocks ?? []) if (block.display_name === "Back Squat") { block.load = null; block.exercise_key = "back-squat"; }
  await page.route("**/api/plans/**", route => route.fulfill({ json:
    new URL(route.request().url()).pathname.endsWith("/completions")
      ? { completions, current_training_day: "2026-10-28" } : historicalPlan }));
  await page.route("**/api/today", route => route.fulfill({ status: 404, json: { detail: "No current readiness" } }));
  await page.route("**/api/today/exercise-logs?**", route => route.fulfill({ json: {
    logs: [], recent_loads: [], recent_performances: [],
  } }));
  await page.route("**/api/history/exercise-library", route => route.fulfill({ json: { exercises: [{
    id: "key:back-squat", name: "Back Squat", identity_kind: "canonical", latest_day: "2026-10-20",
    latest_source: "manual", latest_confirmed_as_prescribed: false, best_loads: [], log_format: "strength",
    latest_values: { sets: 3, reps: 8, load: { value: 120, unit: "kg" } },
  }] } }));
  await page.goto("/plans/premium-plan");
  const today = page.locator('.cm-days [data-day-index="2"]');
  const row = today.locator(".ex-row").filter({ hasText: "Back Squat" });
  await expect(row).toContainText("Suggested");
  await expect(row).toContainText("Last");
  await expect(row).toContainText("120 kg");
  await expect(row.locator(".ex-log-tick")).toHaveCount(0);
  await row.getByRole("button", { name: "Back Squat", exact: true }).click();
  const detail = page.getByRole("dialog", { name: "Back Squat", exact: true });
  await expect(detail).toContainText("Suggested");
  await expect(detail).toContainText("Last");
  await expect(detail).toContainText("120 kg");
  await expect(detail.getByRole("button", { name: "Done", exact: true })).toHaveCount(0);
  if (process.env.PLAN_SCREENSHOTS) await detail.screenshot({ path: `${process.env.PLAN_SCREENSHOTS}/plan-weight-${width}.png` });
});
