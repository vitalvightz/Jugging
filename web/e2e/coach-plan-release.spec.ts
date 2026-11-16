import { expect, test } from "@playwright/test";
import { setup } from "./custom-plan-fixture";
import { trainingPlan } from "../test/training-plan-fixture";
import type { PlanDetail } from "../lib/types";

for (const width of [390, 1280]) {
  test(`accepted coach plan opens without finalization (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await setup(page, baseURL!);
    const structured = structuredClone(trainingPlan);
    structured.plan_metadata = { ...structured.plan_metadata, prescription_source: "athlete" };
    const plan: PlanDetail = {
      plan_id: "coach-plan", athlete_id: "00000000-0000-4000-8000-000000000001",
      full_name: "Fighter", plan_name: "Coach maintenance", fight_date: "",
      technical_style: ["boxing"], created_at: "2026-10-01T12:00:00Z",
      status: "ready", activation_state: "eligible", advisories: [],
      plan_source: "custom", custom_plan_id: null,
      structured_card_state: { state: "live", reasons: [] },
      outputs: { plan_text: "", structured_plan: structured },
      schedule_context: { schedule_mode: "open_recurring", projection_status: "projected",
        current_training_day: "2026-10-28", plan_start_day: "2026-10-01" },
    };
    await page.route("**/api/plans/**", route => {
      const path = new URL(route.request().url()).pathname;
      return route.fulfill({ json: path.endsWith("/completions")
        ? { completions: [], current_training_day: "2026-10-28" } : plan });
    });
    await page.route("**/api/today/exercise-logs?**", route => route.fulfill({ json: { logs: [], recent_loads: [] } }));
    // Plan content remains available before a daily check-in exists.
    await page.route("**/api/today", route => route.fulfill({ status: 404, json: { detail: "No check-in" } }));
    await page.goto("/plans/coach-plan");
    await expect(page.getByRole("heading", { name: "Coach maintenance", exact: true })).toBeVisible();
    await expect(page.locator(".cm-days")).toBeVisible();
    await expect(page.getByText("Pending finalization", { exact: true })).toHaveCount(0);
    await expect(page.getByText("Plan not yet released", { exact: true })).toHaveCount(0);
    await expect(page.getByText(/held back from the athlete view/)).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Edit Plan", exact: true })).toHaveCount(0);
    await expect(page.locator(".cm-active-badge")).toHaveText("ACTIVE");
    if (process.env.COACH_PLAN_SCREENSHOTS) {
      await page.screenshot({ path: `${process.env.COACH_PLAN_SCREENSHOTS}/coach-plan-${width}.png` });
    }
    const today = page.locator('.cm-days [data-day-index="2"]');
    await expect(today.locator(".ex-row")).toHaveCount(4);
    await today.getByRole("button", { name: /^Landmine Press/ }).click();
    const exercise = page.getByRole("dialog", { name: /Landmine Press/ });
    await expect(exercise).toBeVisible();
    await exercise.press("Escape");
    await expect(exercise).toHaveCount(0);
    expect(errors).toEqual([]);
  });
}
