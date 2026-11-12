import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { isolateFromNetwork } from "./support";
import { emptyPlanRequest } from "../lib/onboarding";

const shots = process.env.HOME_FIX_SCREENSHOTS;
const calendar = { outputs: { structured_plan: { weeks: [
  { week_index: 1, phase_label: "GPP", start_date: "2026-10-19", end_date: "2026-11-14", days: [] },
] } } };
const today = {
  active_plan: { id: "camp-a", name: "Autumn camp", fight_date: "2026-11-14", phase: "GPP", status: "ready" },
  today: { training_day: "2026-10-23", recommendation_state: "not_checked_in", decision_tier: "not_checked_in", next_session: {}, session_scope: "none", completion_status: "not_started",
    primary_safety_notice: { code: "skin_care_active", chip: "CARE", title: "Skin care active", action: "Keep the left bicep cut clean and protected.", detail: "Stop if it opens, reopens or bleeds.", tone: "amber" } },
  risk_watch: [], open_injuries: [], week_summary: {}, quick_actions: [],
};

for (const [width, height, theme] of [
  [320, 568, "dark"], [390, 844, "dark"], [430, 932, "dark"], [1280, 800, "dark"], [1440, 900, "dark"],
  [390, 844, "light"], [1280, 800, "light"],
] as const) {
  test(`Home loads countdown immediately and focus hints update (${width}px, ${theme})`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height });
    await isolateFromNetwork(page, baseURL!);
    const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
    const key = `sb-${new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").hostname.split(".")[0]}-auth-token`;
    await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [key, JSON.stringify({ access_token: "fixture-token", refresh_token: "fixture", token_type: "bearer",
      expires_at: Math.floor(Date.now() / 1000) + 86400, user: { id: "athlete-1", email: "fighter@example.com", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} } })]);
    let releaseToday!: () => void, releaseCalendar!: () => void;
    const todayReady = new Promise<void>(resolve => { releaseToday = resolve; });
    const calendarReady = new Promise<void>(resolve => { releaseCalendar = resolve; });
    let calendarReads = 0;
    const draft = { ...emptyPlanRequest("Alex"), current_step: 4, no_scheduled_fight: true, key_goals: ["conditioning"], primary_goal: "conditioning" };
    await page.route("**/api/**", async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/api/today") { await todayReady; return route.fulfill({ json: today }); }
      if (path === "/api/plans/camp-a/calendar") {
        calendarReads++; await calendarReady;
        return route.fulfill({ json: { plan_id: "camp-a", structured_plan: calendar.outputs.structured_plan } });
      }
      return route.fulfill({ json: path === "/api/me" ? {
        profile: { athlete_id: "athlete-1", full_name: "Alex", role: "athlete", access_status: "approved", starting_path: "plan_first", technical_style: ["boxing"], tactical_style: [],
          athlete_timezone: "UTC", athlete_locale: "en", appearance_mode: theme, nutrition_profile: {}, date_of_birth: "1995-01-01", meets_minimum_age: true, is_minor: false,
          terms_accepted: true, health_consent_granted: true, private_trial_ack_at: "2026-01-01T00:00:00Z", onboarding_draft: draft },
        plan_count: 1, latest_plan: { plan_id: "camp-a", status: "ready", fight_date: "2026-11-14" }, latest_intake: null, username_rate_limit: {},
      } : path === "/api/plans" ? [] : { notifications: [], unread_count: 0 } });
    });
    const capture = async (state: string) => {
      await page.mouse.move(0, 0); await page.waitForTimeout(250);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      if (shots) await page.screenshot({ path: `${shots}/${state}-${theme}-${width}.png` });
    };
    try {
      await page.goto("/", { waitUntil: "domcontentloaded" }); await expect(page).toHaveTitle(/UNLXCK/i);
      await expect.poll(() => calendarReads).toBe(1); // Today is still blocked: no waterfall.
      releaseToday();
      const countdown = page.getByRole("region", { name: "Fight countdown: 22 days to fight night", exact: true });
      await expect(countdown.locator(".fight-countdown-number")).toHaveText("22");
      await countdown.scrollIntoViewIfNeeded();
      await expect(countdown.locator(".fight-countdown-number")).toBeVisible();
      await expect(page.getByLabel("Fight countdown loading")).toHaveCount(0);
      await capture("countdown-loading-calendar");
      releaseCalendar();
      await expect(countdown.getByRole("progressbar", { name: "Camp progress" })).toHaveAttribute("aria-valuenow", "15");
      await expect(countdown.locator(".fight-countdown-number")).toHaveText("22");
      await expect(countdown).not.toHaveAttribute("data-pending", "true");
      expect(calendarReads).toBe(1);
      await capture("countdown-loaded");
      const hero = page.locator(".overview-command-primary"); await hero.scrollIntoViewIfNeeded();
      const lead = page.locator(".overview-decision-lead");
      expect(await lead.evaluate(el => ({ image: getComputedStyle(el).backgroundImage, shadow: getComputedStyle(el).boxShadow, overlay: getComputedStyle(el, "::before").content })))
        .toEqual({ image: "none", shadow: "none", overlay: "none" });
      await expect(hero).toHaveAttribute("data-tone", "amber");
      await capture("home-fade");

      for (const quick of [false, true]) {
        await page.goto(quick ? "/quick-build" : "/onboarding");
        const group = quick ? page.locator(".field").filter({ has: page.locator(".checkbox-group-label").filter({ hasText: /^Weak areas/ }) })
          : page.getByRole("group", { name: "Weak Areas", exact: true });
        await expect(group).toBeVisible();
        const strength = group.getByRole("checkbox", { name: /^Strength/ });
        const balance = group.getByRole("checkbox", { name: /^Balance/ });
        const gasTank = group.getByRole("checkbox", { name: /^Gas Tank/ });
        await strength.check(); await expect(gasTank).toBeEnabled();
        await balance.check(); await expect(gasTank).toBeDisabled();
        const why = group.getByRole("button", { name: "Why Gas Tank is unavailable", exact: true });
        await expect(why).toBeVisible(); await expect(why).toBeEnabled(); await why.click();
        const bubble = page.getByRole("tooltip"); await expect(bubble).toContainText("You can pick up to 2 weak areas");
        await expect(bubble).toBeInViewport({ ratio: 1 });
        if (width === 390 || width === 1280) expect((await new AxeBuilder({ page }).include(quick ? ".quick-build-form" : ".onboarding-panel").withRules(["nested-interactive"]).analyze()).violations).toEqual([]);
        await capture(quick ? "quick-build-limit" : "camp-setup-limit");
        await page.keyboard.press("Escape"); await balance.uncheck();
        await expect(gasTank).toBeEnabled(); await expect(why).toHaveCount(0);
        await balance.check(); await expect(why).toBeVisible();
        const goals = quick ? page.locator(".field").filter({ has: page.locator(".checkbox-group-label").filter({ hasText: /^Key goals/ }) })
          : page.getByRole("group", { name: "Key Goals", exact: true });
        await goals.getByRole("checkbox", { name: /^Power/ }).check();
        await expect(goals.getByRole("checkbox", { name: /^Mobility/ })).toBeDisabled();
        if (quick) await expect(goals).toContainText("shared cap: 4");
        else await expect(page.locator(".focus-cap-badge")).toHaveText("4/4");
        const recovery = goals.getByRole("checkbox", { name: /^Recovery/ });
        const conditioning = goals.getByRole("checkbox", { name: /^Conditioning/ });
        await expect(recovery).toBeDisabled(); await expect(conditioning).toBeEnabled();
        await conditioning.uncheck(); await expect(recovery).toBeEnabled();
        await recovery.check(); await expect(conditioning).toBeDisabled();
      }
      expect(errors).toEqual([]);
    } finally {
      releaseToday(); releaseCalendar();
    }
  });
}
