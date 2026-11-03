import { expect, test, type Page } from "@playwright/test";
import { isolateFromNetwork } from "./support";

// Weight-to-target in a camp's final weeks: Today's one-tap weigh-in card, Home's
// purple shortcut status, and adding a target + weigh-in on the tracker. The
// status is the API's (mocked here); the clock is pinned so dates are stable.
// Set WEIGHT_CUT_SCREENSHOTS to a directory to capture the card and shortcut.
const shots = process.env.WEIGHT_CUT_SCREENSHOTS;
const TODAY = "2026-11-03";
const storageKey = `sb-${new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").hostname.split(".")[0]}-auth-token`;

type Options = {
  fightDate?: string;
  isMinor?: boolean;
  theme?: "dark" | "light";
  target?: number | null;
  derived?: Record<string, unknown>;
  afterSave?: Record<string, unknown>;
  /** Extra active-plan fields /api/today sends, e.g. the target fight weight. */
  todayPlan?: Record<string, unknown>;
};

function addDays(iso: string, days: number) {
  const date = new Date(`${iso}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

async function setup(page: Page, baseURL: string, options: Options = {}) {
  const { fightDate = addDays(TODAY, 19), isMinor = false, theme = "dark", target = 71 } = options;
  await isolateFromNetwork(page, baseURL);
  const now = new Date(`${TODAY}T12:00:00Z`);
  await page.clock.setFixedTime(now);
  await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [storageKey, JSON.stringify({
    access_token: "weight-token", refresh_token: "refresh", token_type: "bearer", expires_at: Math.floor(now.getTime() / 1000) + 86400,
    user: { id: "athlete-1", email: "fighter@example.com", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} },
  })]);
  let workspace = {
    athlete_id: "athlete-1", source: "intake", intake_id: null,
    nutrition_profile: { sex: "male", age: 27, height_cm: 178 },
    shared_camp_context: {
      fight_date: fightDate, target_weight_kg: target, current_weight_kg: 73.4, current_weight_source: "latest_bodyweight_log",
      weigh_in_type: target == null ? null : "day_before", weigh_in_time: target == null ? null : "18:00",
      session_types_by_day: {}, hard_sparring_days: [], support_work_days: [], training_availability: [],
    },
    s_and_c_preferences: {}, nutrition_readiness: {},
    nutrition_monitoring: { daily_bodyweight_log: [
      { date: addDays(TODAY, -2), weight_kg: 73.6, time: "07:05", is_fasted: true, notes: null },
      { date: addDays(TODAY, -1), weight_kg: 73.4, time: "07:00", is_fasted: true, notes: null },
    ] },
    nutrition_coach_controls: {},
    derived: {
      days_until_fight: 19, weight_cut_pct: 3.3, weight_cut_risk: true, readiness_flags: [], missing_required_fields: [],
      foundation_status: "complete", weight_cut_risk_band: "moderate", weight_cut_supervision_required: false,
      kg_to_target: target == null ? null : 2.4, weight_cut_status: target == null ? "none" : "on_track", weight_cut_behind_kg: null,
      bodyweight_trend: [
        { date: addDays(TODAY, -2), scale_weight_kg: 73.6, trend_weight_kg: 73.6 },
        { date: addDays(TODAY, -1), scale_weight_kg: 73.4, trend_weight_kg: 73.56 },
      ],
      ...options.derived,
    },
  };
  const nutritionReads: string[] = [];
  const puts: Array<Record<string, any>> = [];
  await page.route("**/api/**", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/api/nutrition/current") {
      if (request.method() === "PUT") {
        const body = request.postDataJSON();
        puts.push(body);
        workspace = {
          ...workspace,
          shared_camp_context: { ...body.shared_camp_context },
          nutrition_monitoring: body.nutrition_monitoring,
          derived: { ...workspace.derived, ...options.afterSave },
        };
      } else {
        nutritionReads.push(page.url());
      }
      return json(workspace);
    }
    if (path === "/api/me") return json({ profile: {
      athlete_id: "athlete-1", full_name: "Alex", role: "athlete", access_status: "approved", starting_path: "plan_first",
      technical_style: ["boxing"], tactical_style: [], athlete_timezone: "UTC", athlete_locale: "en", appearance_mode: theme,
      nutrition_profile: {}, date_of_birth: isMinor ? "2010-01-01" : "1995-01-01", meets_minimum_age: true, is_minor: isMinor,
      terms_accepted: true, health_consent_granted: true, private_trial_ack_at: "2026-01-01T00:00:00Z",
    }, plan_count: 1, latest_plan: { plan_id: "plan-1", status: "ready", fight_date: fightDate }, latest_intake: null, username_rate_limit: {} });
    if (path === "/api/today") return json({
      active_plan: { id: "plan-1", name: "Autumn camp", phase: "SPP", status: "ready", fight_date: fightDate, ...options.todayPlan },
      today: { training_day: TODAY, recommendation_state: "not_checked_in", decision_tier: "not_checked_in", warnings: [],
        next_session: {}, completion_status: "not_started", session_scope: "none", session_label: "No session" },
      open_injuries: [], risk_watch: [], week_summary: {}, quick_actions: [],
    });
    if (path === "/api/plans/plan-1") return json({ outputs: {} });
    if (path === "/api/plans/active") return json({ detail: "No active plan" }, 404);
    if (path === "/api/plans") return json([]);
    if (path === "/api/training/history") return json({ sessions: [] });
    if (path === "/api/today/sparring-logs") return json({ logs: [] });
    return json({ notifications: [], unread_count: 0 });
  });
  return { nutritionReads, puts };
}

async function capture(page: Page, name: string) {
  await page.waitForTimeout(250);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  if (shots) await page.screenshot({ path: `${shots}/${name}.png` });
}

for (const [width, height, theme] of [[390, 844, "dark"], [1280, 800, "dark"], [390, 844, "light"]] as const) {
  test(`Today's weight-cut card: weight-to-target and a one-tap weight entry (${width}px, ${theme})`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height });
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    const { puts } = await setup(page, baseURL!, { theme, afterSave: { kg_to_target: 2.1 } });

    await page.goto("/today");
    const card = page.getByRole("region", { name: "Weight cut" });
    await expect(card).toContainText("Weight cut · 19 days left");
    await expect(card.getByText("2.4", { exact: true })).toBeVisible();
    await expect(card).toContainText("kg to go");
    await expect(card).toContainText("On track");
    await expect(card).toContainText("Starting 73.6 kg");
    await expect(card).toContainText("Target 71.0 kg");
    // (73.6 − 73.4) / (73.6 − 71.0) of the way from the first weight of the final weeks.
    await expect(card.getByRole("progressbar", { name: "Progress to target" })).toHaveAttribute("aria-valuenow", "8");
    await expect(card).toContainText("Current weight 73.4 kg");
    await expect(card).toContainText("Sat 21 Nov · 18:00");
    await expect(card).toContainText("Official weigh-in");
    await card.scrollIntoViewIfNeeded();
    await capture(page, `today-card-${theme}-${width}`);

    await card.getByRole("button", { name: "Log today's weight" }).click();
    const sheet = page.getByRole("dialog", { name: "Log today's weight" });
    await expect(sheet.getByLabel("Weight")).toBeFocused();
    await sheet.getByRole("button", { name: "Decrease by 0.1 kg" }).click();
    await sheet.getByRole("button", { name: "Decrease by 0.1 kg" }).click();
    await sheet.getByRole("button", { name: "Decrease by 0.1 kg" }).click();
    await expect(sheet.getByLabel("Weight")).toHaveValue("73.1");
    await sheet.getByRole("button", { name: "Save weight" }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);

    expect(puts).toHaveLength(1);
    expect(puts[0].nutrition_monitoring.daily_bodyweight_log.at(-1)).toMatchObject({ date: TODAY, weight_kg: 73.1 });
    await expect(card).toContainText("Logged today · 73.1 kg");
    await expect(card.getByText("2.1", { exact: true })).toBeVisible();
    await expect(card.getByRole("button", { name: "Log today's weight" })).toHaveCount(0);
    expect(errors).toEqual([]);
  });
}

test("Today's card shows the API's behind and too-aggressive statuses", async ({ page, baseURL }) => {
  await setup(page, baseURL!, { derived: { weight_cut_status: "behind", weight_cut_behind_kg: 0.8, kg_to_target: 3.3, days_until_fight: 19 } });
  await page.goto("/today");
  const card = page.getByRole("region", { name: "Weight cut" });
  await expect(card).toContainText("Behind by 0.8 kg");
  await expect(card).toContainText("Steep for 19 days out. Talk to your coach about your plan.");
  await card.scrollIntoViewIfNeeded();
  await capture(page, "today-card-behind");

  await page.unrouteAll({ behavior: "ignoreErrors" });
  await setup(page, baseURL!, { derived: { weight_cut_status: "too_aggressive", kg_to_target: 6, weight_cut_risk_band: "severe" } });
  await page.goto("/today");
  await expect(card).toContainText("Cut too aggressive");
  await expect(card).toContainText("Don't attempt this without your coach or a medical professional overseeing it.");
});

test("outside the final weeks, or for an under-18, Today shows no card and reads no nutrition", async ({ page, baseURL }) => {
  const far = await setup(page, baseURL!, { fightDate: addDays(TODAY, 40) });
  await page.goto("/today");
  await expect(page.getByRole("heading", { name: "Today", level: 1 })).toBeVisible();
  await expect(page.getByRole("region", { name: "Weight cut" })).toHaveCount(0);
  expect(far.nutritionReads).toEqual([]);

  await page.unrouteAll({ behavior: "ignoreErrors" });
  const minor = await setup(page, baseURL!, { isMinor: true });
  await page.goto("/today");
  await expect(page.getByRole("heading", { name: "Today", level: 1 })).toBeVisible();
  await expect(page.getByRole("region", { name: "Weight cut" })).toHaveCount(0);
  expect(minor.nutritionReads).toEqual([]);
});

test("Home's purple shortcut shows weight-to-target only in the final weeks", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page, baseURL!);
  await page.goto("/");
  const shortcut = page.getByRole("link", { name: "Weight · 2.4 kg to go · On track" });
  await expect(shortcut).toHaveAttribute("href", "/nutrition");
  await shortcut.scrollIntoViewIfNeeded();
  await capture(page, "home-shortcut-final-weeks");

  await page.unrouteAll({ behavior: "ignoreErrors" });
  const far = await setup(page, baseURL!, { fightDate: addDays(TODAY, 40) });
  await page.goto("/");
  await expect(page.getByRole("link", { name: "Weight tracker: log today's weight" })).toBeVisible();
  expect(far.nutritionReads).toEqual([]);
});

test("Home shows the fight weight on the countdown, and on its own with no fight scheduled", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page, baseURL!, { todayPlan: { target_weight_kg: 90.7 } });
  await page.goto("/");
  const countdown = page.getByRole("region", { name: /Fight countdown/ });
  await expect(countdown.getByText("Fight weight", { exact: true })).toBeVisible();
  await expect(countdown.getByText("90.7 kg", { exact: true })).toBeVisible();

  await page.unrouteAll({ behavior: "ignoreErrors" });
  await setup(page, baseURL!, { fightDate: "", todayPlan: { target_weight_kg: 90.7 } });
  await page.goto("/");
  const card = page.getByRole("region", { name: "Fight weight 90.7 kg" });
  await expect(card).toBeVisible();
  await expect(card.getByRole("link", { name: "Weight tracker" })).toHaveAttribute("href", "/nutrition");
  await expect(page.getByRole("region", { name: /Fight countdown/ })).toHaveCount(0);
  await card.scrollIntoViewIfNeeded();
  await capture(page, "home-fight-weight-open-plan");
});

test("the tracker replaces an existing target with the newest one", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { puts } = await setup(page, baseURL!);
  await page.goto("/nutrition");
  const summary = page.getByRole("region", { name: "Weight summary" });
  await summary.getByRole("button", { name: "Change" }).click();
  const sheet = page.getByRole("dialog", { name: "Target and weigh-in" });
  await expect(sheet.getByLabel("Target weight")).toHaveValue("71");
  await sheet.getByLabel("Target weight").fill("69.5");
  await sheet.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(puts[0].shared_camp_context).toMatchObject({ target_weight_kg: 69.5 });
  await expect(summary).toContainText("Target 69.5 kg");
});

test("the tracker adds a missing target weight and weigh-in", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { puts } = await setup(page, baseURL!, { target: null });
  await page.goto("/nutrition");
  const summary = page.getByRole("region", { name: "Weight summary" });
  await expect(summary).toContainText("No target weight yet.");
  await summary.getByRole("button", { name: "Add target weight" }).click();

  const sheet = page.getByRole("dialog", { name: "Add target weight" });
  await expect(sheet.getByLabel("Target weight")).toBeFocused();
  await sheet.getByRole("button", { name: "Save" }).click();
  await expect(sheet.getByRole("alert")).toHaveText("Enter your target weight in kg.");
  await sheet.getByLabel("Target weight").fill("70.5");
  await sheet.getByRole("button", { name: "Day before" }).click();
  await sheet.getByLabel(/Weigh-in time/).fill("18:00");
  await expect(sheet).toContainText("Weigh-in Sat 21 Nov 2026 · 18:00");
  await capture(page, "tracker-target-sheet");
  await sheet.getByRole("button", { name: "Save" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);

  expect(puts).toHaveLength(1);
  expect(puts[0].shared_camp_context).toMatchObject({ target_weight_kg: 70.5, weigh_in_type: "day_before", weigh_in_time: "18:00" });
  await expect(summary).toContainText("Target 70.5 kg · Weigh-in Sat 21 Nov 2026 · 18:00");
  await expect(summary.getByRole("button", { name: "Add target weight" })).toHaveCount(0);
});
