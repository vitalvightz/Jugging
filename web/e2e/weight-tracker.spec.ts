import { expect, test, type Page } from "@playwright/test";
import { isolateFromNetwork } from "./support";

// Nutrition is the weight tracker: the old workspace form, its BMI/weight-source
// plumbing and the "Coming soon" gate are gone. Set WEIGHT_TRACKER_SCREENSHOTS
// to a directory to capture the page and logging sheet at phone and desktop widths.
const shots = process.env.WEIGHT_TRACKER_SCREENSHOTS;

// What the API derives from the log: a calendar 7-day average, the weekly pace,
// the smoothed trend and the plan's own weight-cut band. `legacy` drops the
// fields an older API does not send, since the web app can deploy first.
function derived(isMinor: boolean, empty: boolean, legacy: boolean) {
  const base = { days_until_fight: 19, weight_cut_pct: isMinor ? 0 : 4.6, readiness_flags: [], missing_required_fields: [], foundation_status: "ready",
    rolling_7_day_average_weight: empty ? null : 73.7 };
  if (legacy) return base;
  return { ...base, rolling_7_day_weigh_in_days: empty ? 0 : 3, weekly_pace_kg: empty ? null : -0.6,
    weight_cut_risk_band: isMinor ? "none" : "high", weight_cut_supervision_required: !isMinor,
    bodyweight_trend: empty ? [] : [
      { date: "2026-10-21", scale_weight_kg: 74.1, trend_weight_kg: 74.1 },
      { date: "2026-10-23", scale_weight_kg: 73.6, trend_weight_kg: 73.92 },
      { date: "2026-10-26", scale_weight_kg: 73.4, trend_weight_kg: 73.69 },
    ] };
}

function workspace(isMinor: boolean, empty = false, legacy = false) {
  return {
    athlete_id: "athlete-1",
    source: "intake",
    intake_id: null,
    nutrition_profile: { sex: "male", age: isMinor ? 16 : 27, height_cm: 178 },
    shared_camp_context: {
      fight_date: "2026-11-14",
      target_weight_kg: isMinor ? null : 70,
      current_weight_kg: 73.4,
      current_weight_source: "latest_bodyweight_log",
      session_types_by_day: {},
      hard_sparring_days: [],
      support_work_days: [],
      training_availability: [],
    },
    s_and_c_preferences: {},
    nutrition_readiness: {},
    nutrition_monitoring: {
      daily_bodyweight_log: empty ? [] : [
        { date: "2026-10-21", weight_kg: 74.1, time: "07:10", is_fasted: true, notes: null },
        { date: "2026-10-23", weight_kg: 73.6, time: "07:05", is_fasted: true, notes: null },
        { date: "2026-10-26", weight_kg: 73.4, time: "07:00", is_fasted: true, notes: "Morning, after toilet" },
      ],
    },
    nutrition_coach_controls: {},
    derived: derived(isMinor, empty, legacy),
  };
}

async function signInAthlete(page: Page, baseURL: string, { isMinor, empty = false, legacy = false, campWeight }: { isMinor: boolean; empty?: boolean; legacy?: boolean; campWeight?: number }) {
  await isolateFromNetwork(page, baseURL);
  // The fixture's weigh-ins run 21-26 Oct and trend ranges end on today, so pin today.
  await page.clock.setFixedTime(new Date("2026-10-27T12:00:00Z"));
  const key = `sb-${new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").hostname.split(".")[0]}-auth-token`;
  await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [key, JSON.stringify({
    access_token: "fixture-token", refresh_token: "fixture", token_type: "bearer", expires_at: Math.floor(Date.now() / 1000) + 86400,
    user: { id: "athlete-1", email: "fighter@example.com", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} },
  })]);
  const saves: unknown[] = [];
  let current = workspace(isMinor, empty, legacy);
  if (campWeight != null) current = { ...current, shared_camp_context: { ...current.shared_camp_context, current_weight_kg: campWeight } };
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/nutrition/current") {
      if (request.method() === "PUT") {
        const body = request.postDataJSON();
        saves.push(body);
        current = { ...current, nutrition_monitoring: body.nutrition_monitoring };
      }
      return route.fulfill({ json: current });
    }
    if (path === "/api/me") {
      return route.fulfill({ json: {
        profile: { athlete_id: "athlete-1", full_name: "Alex", role: "athlete", access_status: "approved", starting_path: "plan_first",
          technical_style: ["boxing"], tactical_style: [], athlete_timezone: "UTC", athlete_locale: "en", appearance_mode: "dark",
          nutrition_profile: {}, date_of_birth: isMinor ? "2010-01-01" : "1999-01-01", meets_minimum_age: true, is_minor: isMinor,
          terms_accepted: true, health_consent_granted: true, private_trial_ack_at: "2026-01-01T00:00:00Z" },
        plan_count: 0, latest_plan: null, latest_intake: null, username_rate_limit: {},
      } });
    }
    return route.fulfill({ json: path === "/api/plans" ? [] : { notifications: [], unread_count: 0 } });
  });
  return saves;
}

for (const [width, height] of [[390, 844], [1280, 800]] as const) {
  test(`Weight tracker: summary, trend, list and logging sheet (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const saves = await signInAthlete(page, baseURL!, { isMinor: false });
    const shot = async (name: string, fullPage = false) => {
      await page.waitForTimeout(250); // let button colour transitions settle
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      if (shots) await page.screenshot({ path: `${shots}/${name}-${width}.png`, fullPage });
    };

    await page.goto("/nutrition/bodyweight-log");
    await expect(page).toHaveURL(/\/nutrition$/);
    await expect(page.getByRole("heading", { level: 1, name: "Weight tracker" })).toBeVisible();
    const summary = page.getByRole("region", { name: "Weight summary" });
    await expect(summary.getByText("73.4", { exact: true })).toBeVisible();
    await expect(summary.getByText("3.4 kg", { exact: true })).toBeVisible();
    await expect(summary.getByText("Above target")).toBeVisible();
    await expect(summary.getByText("19", { exact: true })).toBeVisible();
    await expect(summary.getByText("7-day average")).toBeVisible();
    await expect(summary.getByText("73.7", { exact: true })).toBeVisible();
    await expect(summary.getByText("Per week")).toBeVisible();
    await expect(summary.getByText("−0.6 kg", { exact: true })).toBeVisible();
    await expect(page.getByText(/^High-strain weight cut: 4\.6% of your bodyweight to target with 19 days to go\./)).toBeVisible();
    for (const removed of ["BMI", "Weight source", "Nutrition workspace", "COMING SOON"]) {
      await expect(page.getByText(removed, { exact: true })).toHaveCount(0);
    }

    // The headline is always the latest weigh-in; the chart is inspected separately.
    const trend = page.getByRole("region", { name: "Weight trend" });
    const latest = trend.getByLabel("Latest weight");
    await expect(latest).toContainText("73.4 kg");
    await expect(latest).toContainText("Mon 26 Oct 2026");
    await expect(latest).toContainText("−0.2 kg vs previous");
    // Sparse recent data opens on 7 days, ending today.
    await expect(trend.getByRole("button", { name: "7 days" })).toHaveAttribute("aria-pressed", "true");
    await expect(latest).toContainText("3 entries · Down 0.7 kg");
    // Only logged weights are drawn: no smoothed estimate, no shaded area.
    await expect(trend.getByText("Trend (estimate)")).toHaveCount(0);
    await expect(latest).not.toContainText("Trend");
    const slider = trend.getByRole("slider", { name: "Inspect logged weight" });
    await expect(slider.locator("circle")).toHaveCount(3);
    await expect(slider.locator("path")).toHaveCount(1);
    // The 70 kg target sits below the weights: named, not plotted.
    await expect(trend.getByText("Target 70.0 kg · below chart")).toBeVisible();
    expect((await slider.boundingBox())!.height).toBeLessThanOrEqual(170);
    // 30 days is a calendar window ending today: six days of weigh-ins use a fifth of it.
    await trend.getByRole("button", { name: "30 days" }).click();
    const [first, last] = await slider.locator("circle").evaluateAll(circles => [circles[0], circles[2]].map(c => Number(c.getAttribute("cx"))));
    const plotWidth = await slider.evaluate(element => element.getBoundingClientRect().width);
    expect((last - first) / plotWidth).toBeLessThan(0.25);
    // Inspecting shows a tooltip only while it lasts, and never moves the headline.
    await slider.focus();
    await page.keyboard.press("ArrowLeft");
    await expect(slider).toContainText("73.6 kg");
    await expect(latest).toContainText("73.4 kg");
    await expect(trend.getByRole("button", { name: /Edit 23 Oct weight/ })).toBeVisible();
    await shot("weight-trend-inspect");
    await page.keyboard.press("Tab");
    await expect(slider).not.toContainText("73.6 kg");
    await trend.getByRole("button", { name: "7 days" }).click();
    await shot("weight-tracker", true);
    await trend.scrollIntoViewIfNeeded();
    await shot("weight-trend");

    // Rows open the same sheet in edit mode.
    await page.getByRole("region", { name: "Logged weights" }).getByRole("button", { name: /Fri 23 Oct 2026/ }).click();
    await expect(page.getByRole("dialog", { name: "Edit weight" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);

    await summary.getByRole("button", { name: "Log today's weight" }).click();
    const dialog = page.getByRole("dialog", { name: "Log today's weight" });
    await expect(dialog.getByLabel("Weight")).toBeFocused();
    await dialog.getByRole("button", { name: "Decrease by 0.1 kg" }).click();
    await expect(dialog.getByLabel("Weight")).toHaveValue("73.3");
    await dialog.getByRole("button", { name: "Fasted" }).click();
    await expect(dialog.getByRole("button", { name: "Fasted" })).toHaveAttribute("aria-pressed", "true");
    await shot("weight-tracker-sheet");
    await dialog.getByRole("button", { name: "Save weight" }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(saves).toHaveLength(1);
    await expect(summary.getByText("73.3", { exact: true })).toBeVisible();

    if (width === 1280) {
      const navLink = page.getByRole("link", { name: /Weight tracker/ }).first();
      await expect(navLink).toHaveAttribute("href", "/nutrition");
    }
    expect(errors).toEqual([]);
  });
}

test("under-18 athletes see no target weight or gap", async ({ page, baseURL }) => {
  await signInAthlete(page, baseURL!, { isMinor: true });
  await page.goto("/nutrition");
  await expect(page.getByRole("heading", { level: 1, name: "Weight tracker" })).toBeVisible();
  await expect(page.getByText("7-day average", { exact: true })).toBeVisible();
  await expect(page.locator(".history-screen").getByText(/target/i)).toHaveCount(0);
  await expect(page.getByText(/weight cut:/i)).toHaveCount(0);
});

test("an older API without trend fields still renders the tracker", async ({ page, baseURL }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await signInAthlete(page, baseURL!, { isMinor: false, legacy: true });
  await page.goto("/nutrition");
  const summary = page.getByRole("region", { name: "Weight summary" });
  await expect(summary.getByText("73.4", { exact: true })).toBeVisible();
  await expect(summary.getByText("Per week")).toHaveCount(0);
  await expect(page.getByText(/weight cut:/i)).toHaveCount(0);
  const trend = page.getByRole("region", { name: "Weight trend" });
  await expect(trend.getByRole("slider", { name: "Inspect logged weight" })).toBeVisible();
  await expect(trend.getByText("Trend (estimate)")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("a first weight starts from an empty state", async ({ page, baseURL }) => {
  await signInAthlete(page, baseURL!, { isMinor: false, empty: true });
  await page.goto("/nutrition");
  await expect(page.getByRole("heading", { name: "No weights logged yet" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Weight trend" })).toHaveCount(0);
  await page.getByRole("button", { name: "Log today's weight" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Save weight" }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toHaveText("Enter your weight in kg.");
});

test("camp weight set later in Camp Setup drives the gap, and the tracker says so", async ({ page, baseURL }) => {
  await signInAthlete(page, baseURL!, { isMinor: false, campWeight: 72.9 });
  await page.goto("/nutrition");
  const summary = page.getByRole("region", { name: "Weight summary" });
  // The headline stays the latest logged weigh-in...
  await expect(summary.getByText("73.4", { exact: true })).toBeVisible();
  // ...while the gap uses the camp weight the plan and cut band use.
  await expect(summary.getByText("2.9 kg", { exact: true })).toBeVisible();
  await expect(summary.getByText(/Your camp weight is 72\.9 kg, set in Camp Setup\./)).toBeVisible();
});

test("a weight can't be dated in the future", async ({ page, baseURL }) => {
  const saves = await signInAthlete(page, baseURL!, { isMinor: false });
  await page.goto("/nutrition");
  await page.getByRole("region", { name: "Weight summary" }).getByRole("button", { name: "Log today's weight" }).click();
  const dialog = page.getByRole("dialog", { name: "Log today's weight" });
  const date = dialog.getByLabel("Date");
  const today = await page.evaluate(() => {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  });
  await expect(date).toHaveAttribute("max", today);
  const future = new Date(`${today}T12:00:00`);
  future.setDate(future.getDate() + 3);
  await date.fill(future.toISOString().slice(0, 10));
  await dialog.getByLabel("Weight").fill("72.0");
  await dialog.getByRole("button", { name: "Save weight" }).click();
  await expect(dialog.getByRole("alert")).toHaveText("A weight can't be dated in the future.");
  expect(saves).toHaveLength(0);
});
