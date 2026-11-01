import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { isolateFromNetwork } from "./support";
import type { TrainingHistorySession } from "../lib/training-history";

async function setup(page: Page, baseURL: string, options: { path?: string | null; failSave?: boolean; failMe?: boolean; failToday?: boolean; terms?: boolean; plan?: boolean; consent?: boolean; rejectConsent?: boolean; failConsent?: boolean; manualHistory?: TrainingHistorySession[]; sharedHistory?: TrainingHistorySession[]; activePlan?: boolean; sports?: string[] } = {}) {
  await isolateFromNetwork(page, baseURL);
  const storageKey = `sb-${new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").hostname.split(".")[0]}-auth-token`;
  await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [storageKey, JSON.stringify({
    access_token: "tracking-test-token", refresh_token: "test-refresh", token_type: "bearer",
    expires_in: 86400, expires_at: Math.floor(Date.now() / 1000) + 86400,
    user: { id: "athlete-1", email: "fighter@example.com", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} },
  })]);
  let path = options.path ?? null;
  let sports = options.sports ?? ["boxing"];
  let healthConsent = options.consent !== false;
  let consentFailed = false;
  let failed = false;
  let sessions: Record<string, unknown>[] = options.sharedHistory ?? [];
  let manualSessions: Record<string, unknown>[] = options.manualHistory ?? [];
  const writes: Record<string, unknown>[] = [];
  let injuries: Record<string, unknown>[] = [];
  const sparringLogs: Record<string, unknown>[] = [];
  let checkin: Record<string, unknown> | null = null;
  await page.route("**/api/**", async route => {
    const request = route.request();
    const endpoint = new URL(request.url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (endpoint === "/api/me" || endpoint === "/api/me/compliance") {
      if (options.failMe) return json({ detail: "Unavailable" }, 503);
      if (endpoint === "/api/me/compliance") {
        writes.push(request.postDataJSON());
        if (options.failConsent && !consentFailed) { consentFailed = true; return json({ detail: "Unavailable" }, 503); }
        healthConsent = request.postDataJSON().health_data_consent === true;
      }
      if (request.method() === "PUT") {
        const body = request.postDataJSON(); writes.push(body);
        if (options.failSave && !failed) { failed = true; return json({ detail: "Unavailable" }, 503); }
        path = body.starting_path;
        if (body.technical_style) sports = body.technical_style;
      }
      return json({ profile: { starting_path: path, athlete_id: "athlete-1", email: "fighter@example.com", full_name: "Alex", role: "athlete", access_status: "approved",
        technical_style: sports, tactical_style: [], stance: "orthodox", professional_status: "amateur", record: "",
        athlete_timezone: "Europe/London", athlete_locale: "en", appearance_mode: "dark", nutrition_profile: {},
        date_of_birth: "1995-01-01", meets_minimum_age: true, is_minor: false, terms_accepted: options.terms !== false, health_consent_granted: healthConsent,
        training_habits: options.manualHistory ? { enabled: true, decisions: [] } : undefined,
      }, plan_count: options.plan ? 1 : 0, latest_plan: options.plan ? { plan_id: "plan-9", status: "ready" } : null, latest_intake: null, username_rate_limit: {} });
    }
    if (endpoint === "/api/today") return options.failToday ? json({ detail: "Unavailable" }, 503) : json({
      active_plan: options.activePlan ? { id: "plan-9", name: "Autumn camp", phase: "GPP", fight_date: "2026-12-12" } : {},
      today: { training_day: "2026-10-18", warnings: [], next_session: {}, completion_status: "not_started", session_scope: "none",
        ...(checkin ? { recommendation_state: checkin.recommendation_state, decision_tier: checkin.decision_tier, recommendation_reason: checkin.recommendation_reason }
          : { recommendation_state: "not_checked_in", decision_tier: "not_checked_in" }) },
      open_injuries: injuries, risk_watch: [], week_summary: {}, quick_actions: [],
      ...(sparringLogs.some(log => log.rocked) ? { today: { training_day: "2026-10-18", recommendation_state: "not_checked_in", decision_tier: "not_checked_in", warnings: [], next_session: {}, completion_status: "not_started", session_scope: "none",
        primary_safety_notice: { code: "head_impact_no_contact", injury_id: "", chip: "CHECK", title: "No more contact today", action: "You reported being rocked or dropped. No more contact today, and tell your coach.", detail: "Get checked if symptoms appear.", tone: "red" } } } : {}),
    });
    if (endpoint === "/api/today/checkin" && request.method() === "POST") {
      const body = request.postDataJSON(); writes.push(body);
      const pulled = body.pain === "high";
      const reason = pulled
        ? "Rehab only today.\nPain is high, so contact and impact are not safe today.\nUse rehab or easy mobility only; skip sparring, pads, bag work, and conditioning."
        : "No warning signs today.\nYour sleep, body, and pain checks are all clear today.\nTrain as your coach plans and stop anything that hurts.\nThis check-in is not medical clearance for sparring or contact.";
      checkin = { recommendation_state: pulled ? "pull_back" : "train_as_planned", decision_tier: pulled ? "pull_back" : "green", recommendation_reason: reason };
      return json({ checkin: { ...body, id: "checkin-1", athlete_id: "athlete-1", plan_id: null, phase: null, training_day: "2026-10-18", ...checkin },
        training_day: "2026-10-18", recommendation_state: checkin.recommendation_state, recommendation_reason: reason, triggers: [], warnings: [] }, 201);
    }
    if (endpoint === "/api/today/sparring-log" && request.method() === "POST") {
      const body = request.postDataJSON(); writes.push(body);
      const replay = sparringLogs.find(log => log.client_request_id === body.client_request_id);
      const log = replay ?? { ...body, id: `spar-${sparringLogs.length}`, athlete_id: "athlete-1", training_day: "2026-10-18", created_at: "" };
      if (!replay) sparringLogs.push(log);
      return json({ log, review_created: Boolean(log.rocked), safety_notice: log.rocked ? "You logged being rocked or dropped. No more contact today." : null }, 201);
    }
    if (endpoint === "/api/today/injury-checkin" && request.method() === "POST") {
      const body = request.postDataJSON(); writes.push(body);
      if (!healthConsent) return json({ detail: { code: "health_data_consent_required", message: "Health-data consent is required." } }, 403);
      for (const item of body.injuries as Record<string, unknown>[]) {
        if (!item.flag_id) injuries = [...injuries, { id: `flag-${injuries.length + 1}`, athlete_id: "athlete-1", source: "checkin", plan_id: null,
          body_area: item.body_area, description: item.description, severity: item.severity, status: "open", latest_reported_status: "ongoing",
          surface_class: "non_surface", created_at: "2026-10-18T09:00:00Z", updated_at: "2026-10-18T09:00:00Z" }];
        else if (item.status === "resolved") injuries = injuries.filter(row => row.id !== item.flag_id);
        else injuries = injuries.map(row => row.id === item.flag_id ? { ...row, latest_reported_status: item.status } : row);
      }
      return json({ open_injuries: injuries }, 201);
    }
    if (endpoint === "/api/training/history") return json({ sessions: new URL(request.url()).searchParams.get("manual_only") === "true" ? manualSessions : sessions });
    if (endpoint === "/api/training/sessions" && request.method() === "POST") {
      const body = request.postDataJSON(); writes.push(body);
      if (!healthConsent || options.rejectConsent) return json({ detail: { code: "health_data_consent_required", message: "Health-data consent is required." } }, 403);
      const row = { ...body, id: `manual-${sessions.length}`, source: "external", training_day: "2026-10-18", title: body.activity === "sparring" ? "Sparring" : "Boxing", status: "done", plan_id: null };
      sessions = [row, ...sessions]; manualSessions = [row, ...manualSessions]; return json(row, 201);
    }
    if (endpoint === "/api/plans/active") return json({ detail: "No active plan" }, 404);
    if (endpoint === "/api/plans") return json([]);
    return json({ notifications: [], unread_count: 0, level: 1, total_xp: 0 });
  });
  return writes;
}

test("tracking keeps consent optional and grants it in one explicit tap inside the logger", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const writes = await setup(page, baseURL!, { consent: false });
  await page.goto("/get-started");
  await page.getByRole("button", { name: /I already have a coach/ }).click();
  // Tracking athletes land on Home; logging lives on Today.
  await expect(page).toHaveURL(/\/$/);
  await page.goto("/today");
  await page.getByRole("button", { name: "Log session", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Training logs include health-related information");
  await expect(dialog.getByRole("form")).toHaveCount(0);
  const allow = dialog.getByRole("button", { name: "Allow health-data use and continue", exact: true });
  await expect(allow).toBeFocused();
  expect(writes).toEqual([{ starting_path: "tracking_first" }]);
  expect((await new AxeBuilder({ page }).include("dialog").analyze()).violations).toEqual([]);
  await page.screenshot({ path: test.info().outputPath("contextual-consent.png") });
  await allow.click();
  await expect(page).toHaveURL(/\/today$/);
  await expect(page.getByRole("form", { name: "Log session", exact: true })).toBeVisible();
  // The athlete boxes, so the first Activity choice is a boxing session type.
  await expect(page.getByRole("button", { name: "Pads", exact: true })).toBeFocused();
  expect(writes).toEqual([{ starting_path: "tracking_first" }, { health_data_consent: true }]);
});

test("declining contextual consent keeps tracking and the round timer available", async ({ page, baseURL }) => {
  const writes = await setup(page, baseURL!, { consent: false, path: "tracking_first" });
  await page.goto("/today");
  await page.getByRole("button", { name: "Log session", exact: true }).click();
  await page.getByRole("button", { name: "Not now", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("navigation", { name: "Quick actions" }).getByRole("link", { name: "Round timer" })).toBeVisible();
  expect(writes).toEqual([]);
});

test("failed contextual consent remains optional and can be retried", async ({ page, baseURL }) => {
  const writes = await setup(page, baseURL!, { consent: false, path: "tracking_first", failConsent: true });
  await page.goto("/today");
  await page.getByRole("button", { name: "Log session", exact: true }).click();
  const allow = page.getByRole("button", { name: "Allow health-data use and continue", exact: true });
  await allow.click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText("could not be saved");
  await expect(page.getByRole("form", { name: "Log session", exact: true })).toHaveCount(0);
  await allow.click();
  await expect(page.getByRole("form", { name: "Log session", exact: true })).toBeVisible();
  expect(writes).toEqual([{ health_data_consent: true }, { health_data_consent: true }]);
});

test("server-side consent withdrawal replaces the logger with a recovery route", async ({ page, baseURL }) => {
  await setup(page, baseURL!, { path: "tracking_first", rejectConsent: true });
  await page.goto("/today");
  await page.getByRole("button", { name: "Log session", exact: true }).click();
  const form = page.getByRole("form", { name: "Log session", exact: true });
  await form.getByRole("button", { name: "Pads", exact: true }).click();
  await form.getByRole("button", { name: "45", exact: true }).click();
  await form.getByRole("button", { name: "Moderate", exact: true }).click();
  await form.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("button", { name: "Allow health-data use and continue", exact: true })).toBeVisible();
  await expect(form).toHaveCount(0);
});

for (const failedSource of ["none", "manual", "shared"]) {
  test(`habits use manual-only evidence independently of shared history (${failedSource})`, async ({ page, baseURL }) => {
    await page.clock.setFixedTime(new Date("2026-10-18T12:00:00Z"));
    const manual: TrainingHistorySession[] = ["2026-10-11", "2026-10-04"].map(day => ({
      id: day, source: "external", training_day: day, status: "done", title: "Boxing", activity: "boxing", duration_minutes: 45, effort: "moderate", plan_id: null,
    }));
    const prescribed: TrainingHistorySession[] = Array.from({ length: 30 }, (_, index) => ({
      id: `plan-${index}`, source: "unlxck", training_day: "2026-10-18", status: "done", title: "Prescribed strength", activity: null, duration_minutes: null, effort: null, plan_id: "old-plan",
    }));
    await setup(page, baseURL!, { path: "tracking_first", manualHistory: manual, sharedHistory: prescribed });
    const urls: string[] = [];
    page.on("request", request => { if (request.url().includes("/api/training/history")) urls.push(request.url()); });
    if (failedSource !== "none") await page.route(failedSource === "manual" ? "**/api/training/history?manual_only=true&limit=200" : "**/api/training/history", route => route.fulfill({ status: 503, contentType: "application/json", body: '{}' }));
    await page.goto("/today");
    await expect.poll(() => urls.some(url => url.endsWith("manual_only=true&limit=200"))).toBe(true);
    if (failedSource === "manual") await expect(page.getByText("Boxing every Sunday?", { exact: true })).toHaveCount(0);
    else await expect(page.getByText("Boxing every Sunday?", { exact: true })).toBeVisible();
    // Today carries no recent-history list or habit settings; History owns the record.
    await expect(page.getByRole("region", { name: "Training history", exact: true })).toHaveCount(0);
    await expect(page.getByText("Training habits", { exact: true })).toHaveCount(0);
  });
}

for (const width of [320, 390, 1280]) {
  test(`Tracking choice, log, repeat and plan later (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const errors: string[] = []; page.on("pageerror", e => errors.push(e.message));
    const writes = await setup(page, baseURL!);
    await page.goto("/get-started");
    await expect(page.getByRole("heading", { name: "How would you like to start?" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Open navigation" })).toHaveCount(0);
    await expect(page.locator(".app-sidebar, .mobile-tab-bar")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Sign out", exact: true })).toBeVisible();
    const choices = page.locator('[aria-busy="false"] > button');
    const sizes = await choices.evaluateAll(elements => elements.map(element => element.getBoundingClientRect().height));
    expect(sizes).toHaveLength(2);
    // Building an S&C plan is offered first.
    await expect(choices.first()).toContainText("Generate my S&C plan");
    // Slim list rows, not cards: touch-sized, but no taller than a short two-line row.
    expect(sizes.every(size => size >= 44 && size <= 96)).toBe(true);
    await page.screenshot({ path: test.info().outputPath(`choice-${width}.png`) });
    const tracking = page.getByRole("button", { name: /I already have a coach/ });
    await tracking.focus(); await expect(tracking).toBeFocused();
    expect(await tracking.evaluate(element => getComputedStyle(element).boxShadow)).toBe("none");
    expect((await new AxeBuilder({ page }).include("main").analyze()).violations).toEqual([]);
    await page.screenshot({ path: test.info().outputPath(`choice-focus-${width}.png`) });
    await tracking.press("Enter");
    // Tracking athletes land on Home; logging lives on Today.
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole("link", { name: "Add my plan" })).toHaveAttribute("href", "/plans/custom");
    await page.goto("/today");
    await expect(page.locator("html")).toHaveAttribute("data-app-surface", "workspace");
    await expect(page.getByRole("button", { name: "Repeat last session", exact: true })).toHaveCount(0);
    const quickActions = page.getByRole("navigation", { name: "Quick actions" });
    // Phones hide Quick actions: the tab bar and + Create carry them.
    if (width <= 720) await expect(quickActions).toBeHidden();
    else {
      await expect(quickActions.getByRole("link", { name: "Round timer" })).toBeVisible();
      await expect(quickActions.getByRole("link", { name: "Generate my S&C plan" })).toBeVisible();
      await expect(quickActions.getByRole("link", { name: "Add my plan" })).toHaveAttribute("href", "/plans/custom");
    }
    // The daily check-in is the same as a planned athlete's, sent plan-free.
    await expect(page.getByRole("list", { name: "Today command status" })).toContainText("Check-in");
    await expect(page.getByRole("list", { name: "Today command status" })).toContainText("Due");
    await expect(page.locator("#today-checkin")).toBeVisible();
    await page.getByRole("button", { name: "Log session", exact: true }).click();
    const form = page.getByRole("form", { name: "Log session", exact: true });
    // A boxer's Activity choices are boxing session types.
    await form.getByRole("button", { name: "Pads", exact: true }).click();
    await form.getByRole("button", { name: "45", exact: true }).click();
    await form.getByRole("button", { name: "Moderate", exact: true }).click();
    await form.getByRole("button", { name: "Save Session", exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Log session", exact: true })).toBeFocused();
    await expect(page.getByRole("button", { name: "Repeat last session", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Repeat last session", exact: true })).toContainText("Boxing · Pads");
    // Boxing gets no sparring follow-up; the tile now reports today's log.
    await expect(page.getByRole("dialog", { name: "Log your sparring" })).toHaveCount(0);
    await expect(page.getByRole("list", { name: "Today command status" })).toContainText("Logged");
    await page.reload();
    await expect(page.getByText(/^Your training, your record\./)).toBeVisible();
    await expect(page.getByRole("button", { name: "Repeat last session", exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect((await new AxeBuilder({ page }).include("main").analyze()).violations).toEqual([]);
    await page.screenshot({ path: test.info().outputPath(`tracking-${width}.png`) });
    // Home keeps its layout, filled from what was logged; no redirect to Today.
    await page.goto("/");
    await expect(page).toHaveURL(/\/$/);
    await expect(page.locator("html")).toHaveAttribute("data-app-surface", "workspace");
    // Not checked in yet, so the check-in leads; the logged session fills the strip and summary.
    await expect(page.getByRole("heading", { name: "How are you today?", level: 1 })).toBeVisible();
    await expect(page.getByLabel("Training status")).toContainText("1 session");
    await expect(page.getByRole("region", { name: "Training summary" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    // Measure settled colours after the brand-to-workspace transition. Keep
    // every contrast check; an in-flight fade is not the final reading state.
    await page.evaluate(async () => {
      await Promise.all(document.getAnimations()
        .filter(animation => animation.effect && animation.effect.getTiming().iterations !== Infinity)
        .map(animation => animation.finished.catch(() => {})));
    });
    expect((await new AxeBuilder({ page }).include("main").analyze()).violations).toEqual([]);
    await page.screenshot({ path: test.info().outputPath(`tracking-home-${width}.png`) });
    await page.getByRole("link", { name: "Generate my S&C plan" }).first().click();
    await expect(page).toHaveURL(/\/quick-build$/);
    expect(writes[0]).toEqual({ starting_path: "tracking_first" });
    expect(writes[1]).toMatchObject({ activity: "boxing", duration_minutes: 45, effort: "moderate" });
    expect(writes[1]).not.toHaveProperty("plan_id");
    expect(errors).toEqual([]);
  });
}

test("Saving failure stays on choice and retry saves plan-first", async ({ page, baseURL }) => {
  const writes = await setup(page, baseURL!, { failSave: true });
  await page.goto("/get-started");
  const plan = page.getByRole("button", { name: /Generate my S&C plan/ });
  await plan.click(); await expect(page.getByRole("alert").filter({ hasText: "could not be saved" })).toBeVisible();
  await expect(page).toHaveURL(/\/get-started$/); await expect(plan).toBeEnabled();
  await plan.click(); await expect(page).toHaveURL(/\/quick-build$/);
  expect(writes).toEqual([{ starting_path: "plan_first" }, { starting_path: "plan_first" }]);
});

test("focused entry keeps sign-out available without choosing a path", async ({ page, baseURL }) => {
  const writes = await setup(page, baseURL!);
  await page.route("**/auth/v1/logout**", route => route.fulfill({ status: 204 }));
  await page.goto("/get-started");
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect(writes).toEqual([]);
});

test("Unaccepted terms precede the choice", async ({ page, baseURL }) => {
  await setup(page, baseURL!, { terms: false }); await page.goto("/get-started");
  await expect(page).toHaveURL(/\/consent$/);
  await expect(page.getByRole("button", { name: /I already have a coach/ })).toHaveCount(0);
});

test("Existing athlete with a plan keeps their destination", async ({ page, baseURL }) => {
  await setup(page, baseURL!, { path: "tracking_first", plan: true }); await page.goto("/get-started");
  await expect(page).toHaveURL(/\/plans\/plan-9$/);
});

test("Profile failure never becomes first-use onboarding", async ({ page, baseURL }) => {
  await setup(page, baseURL!, { failMe: true }); await page.goto("/get-started");
  await expect(page.getByRole("heading", { name: "Couldn’t load your account" })).toBeVisible();
  await expect(page.getByRole("button", { name: /I already have a coach/ })).toHaveCount(0);
});

test("Today failure never becomes a no-plan empty state", async ({ page, baseURL }) => {
  await setup(page, baseURL!, { path: "tracking_first", failToday: true }); await page.goto("/today");
  await expect(page.getByRole("heading", { name: "Today is temporarily unavailable" })).toBeVisible();
  await expect(page.getByText("No training logged yet", { exact: true })).toHaveCount(0);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Today is temporarily unavailable" })).toBeVisible();
  await expect(page.getByText("Build your fight camp in minutes.", { exact: true })).toHaveCount(0);
});

for (const width of [320, 390, 1280]) {
  test(`no-plan athlete reports, updates and resolves an injury on the shared Today (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const writes = await setup(page, baseURL!, { path: "tracking_first" });
    await page.goto("/today");
    const manager = page.locator("#today-injury");
    await expect(manager).toContainText("No injuries tracked.");
    // Compact by default: the questionnaire stays closed until asked for.
    await expect(manager.locator("form.today-injury-add")).toBeHidden();
    await manager.getByRole("button", { name: /Add injury/ }).click();
    await manager.getByRole("button", { name: /Enter it manually/ }).click();
    await manager.getByLabel(/Affected area/).fill("left shoulder");
    await manager.getByRole("button", { name: "Soreness", exact: true }).click();
    await manager.getByRole("button", { name: "Not limiting me", exact: true }).click();
    await manager.getByRole("button", { name: "Save injury", exact: true }).click();
    await expect(page.getByRole("list", { name: "Today command status" })).toContainText("1 active");
    expect(writes[0]).toMatchObject({ injuries: [{ body_area: "left shoulder" }] });
    expect(writes[0]).not.toHaveProperty("plan_id");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect((await new AxeBuilder({ page }).include("main").analyze()).violations).toEqual([]);
    await page.screenshot({ path: test.info().outputPath(`no-plan-injury-${width}.png`), fullPage: true });

    await manager.locator(".today-injury-summary-row").click();
    await manager.getByRole("button", { name: "Better", exact: true }).click();
    await expect.poll(() => writes.length).toBe(2);
    expect(writes[1]).toEqual({ injuries: [{ flag_id: "flag-1", status: "improving" }] });
    await manager.getByRole("button", { name: "Mark resolved" }).click();
    await manager.getByRole("button", { name: "Yes, resolve", exact: true }).click();
    await expect(manager).toContainText("No injuries tracked.");
    expect(writes[2]).toEqual({ injuries: [{ flag_id: "flag-1", status: "resolved" }] });
    await expect(page.getByRole("list", { name: "Today command status" })).toContainText("None");
  });
}

test("without health-data consent the injury form is never offered", async ({ page, baseURL }) => {
  const writes = await setup(page, baseURL!, { path: "tracking_first", consent: false });
  await page.goto("/today");
  const injuries = page.locator("#today-injury");
  await expect(injuries).toContainText("needs your health-data consent");
  await expect(injuries.getByRole("button", { name: /Add injury/ })).toHaveCount(0);
  expect(writes).toEqual([]);
});

for (const width of [320, 390, 1280]) {
  test(`a tracking athlete with an active plan keeps the same Today with prescribed content (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    await setup(page, baseURL!, { path: "tracking_first", activePlan: true, plan: true });
    await page.goto("/today");
    await expect(page.getByRole("heading", { name: "Today", level: 1 })).toHaveCount(1);
    await expect(page.getByText("General prep", { exact: true })).toBeVisible();
    const status = page.getByRole("list", { name: "Today command status" });
    await expect(status).toContainText("Check-in");
    await expect(status).not.toContainText("Not assessed");
    await expect(page.locator("#today-checkin")).toBeVisible();
    if (width > 720) await expect(page.getByRole("navigation", { name: "Quick actions" }).getByRole("link", { name: "View plan" })).toHaveAttribute("href", "/plans/plan-9");
    await expect(page.getByRole("navigation", { name: "Quick actions" }).getByRole("link", { name: "Add my plan" })).toHaveCount(0);
    // Shared sections are the same components as the no-plan state.
    await expect(page.locator("#today-injury")).toContainText("No injuries tracked.");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: test.info().outputPath(`planned-today-${width}.png`), fullPage: true });
  });
}

async function logSparring(page: Page, effort: "Easy" | "Moderate" | "Hard") {
  await page.getByRole("button", { name: "Log session", exact: true }).click();
  const form = page.getByRole("form", { name: "Log session", exact: true });
  await form.getByRole("button", { name: "Sparring", exact: true }).click();
  await form.getByRole("button", { name: "45", exact: true }).click();
  await form.getByRole("button", { name: effort, exact: true }).click();
  await form.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Log session", exact: true })).toHaveCount(0);
}

for (const width of [320, 390, 1280]) {
  test(`manual sparring offers the shared follow-up, prefilled but never with invented rounds (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const writes = await setup(page, baseURL!, { path: "tracking_first" });
    await page.goto("/today");
    await logSparring(page, "Hard");
    const prompt = page.getByRole("dialog", { name: "Log your sparring" });
    await expect(prompt).toBeVisible();
    await expect(prompt).toContainText("Session saved. This is optional.");
    // The follow-up is the Log training sheet's size, not a long card on Today.
    expect((await prompt.boundingBox())!.height).toBeLessThanOrEqual(640);
    await expect(prompt.locator(".sparring-log-rounds-value")).toHaveText("–");
    await expect(prompt.getByRole("button", { name: "Hard", exact: true }).first()).toHaveAttribute("aria-pressed", "true");
    await prompt.getByRole("button", { name: "More rounds" }).click();
    await prompt.getByRole("button", { name: "More rounds" }).click();
    await prompt.getByRole("group", { name: "Head contact" }).getByRole("button", { name: "Light", exact: true }).click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect((await new AxeBuilder({ page }).include("main").analyze()).violations).toEqual([]);
    await page.screenshot({ path: test.info().outputPath(`manual-sparring-${width}.png`) });
    await prompt.getByRole("button", { name: "Save", exact: true }).click();
    await expect(prompt).toHaveCount(0);
    const [manual, detail] = writes;
    expect(manual).toMatchObject({ activity: "sparring", effort: "hard" });
    expect(detail).toMatchObject({ source: "free", plan_id: null, planned_intensity: null, external_session_id: "manual-0",
      intensity: "hard", rounds_completed: 2, head_contact: "light", rocked: false });
    expect(detail.client_request_id).toEqual(expect.any(String));
    expect(writes).toHaveLength(2);
  });
}

test("skipping the sparring follow-up keeps the basic training log", async ({ page, baseURL }) => {
  const writes = await setup(page, baseURL!, { path: "tracking_first" });
  await page.goto("/today");
  await logSparring(page, "Easy");
  const prompt = page.getByRole("dialog", { name: "Log your sparring" });
  await expect(prompt.getByRole("group", { name: "How hard was it?" }).getByRole("button", { name: "Light", exact: true })).toHaveAttribute("aria-pressed", "true");
  await prompt.getByRole("button", { name: "Skip", exact: true }).click();
  await expect(prompt).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Repeat last session", exact: true })).toContainText("Sparring");
  expect(writes).toHaveLength(1);
});

test("a rocked report shows the safety notice at once and Today stops leading with anything else", async ({ page, baseURL }) => {
  const writes = await setup(page, baseURL!, { path: "tracking_first" });
  await page.goto("/today");
  await logSparring(page, "Moderate");
  const prompt = page.getByRole("dialog", { name: "Log your sparring" });
  await prompt.getByRole("button", { name: "More rounds" }).click();
  await prompt.getByRole("group", { name: "Head contact" }).getByRole("button", { name: "A lot", exact: true }).click();
  await prompt.getByRole("group", { name: "Got rocked or dropped?" }).getByRole("button", { name: "Yes", exact: true }).click();
  await expect(prompt.getByRole("alert")).toContainText("No more contact today");
  await prompt.getByRole("button", { name: "Save", exact: true }).click();
  const saved = page.getByRole("dialog", { name: "Sparring logged" });
  await expect(saved.getByRole("alert")).toContainText("No more contact today");
  await expect(page.locator("#today-decision")).toContainText("No more contact today");
  expect(writes.filter(row => "head_contact" in row)).toHaveLength(1);
  await page.screenshot({ path: test.info().outputPath("rocked-today.png"), fullPage: true });
});

for (const width of [320, 390, 1280]) {
  test(`a plan-free athlete completes a real daily check-in that survives reload (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const writes = await setup(page, baseURL!, { path: "tracking_first" });
    await page.goto("/today");
    const checkin = page.locator("#today-checkin");
    await expect(checkin).toBeVisible();
    expect((await new AxeBuilder({ page }).include("main").analyze()).violations).toEqual([]);
    await checkin.getByRole("button", { name: "Submit check-in", exact: true }).click();
    await expect(page.locator("#today-decision")).toContainText("Train as your coach plans");
    await expect(page.locator("#today-decision")).toContainText("not medical clearance");
    await expect(page.locator("#today-decision")).not.toContainText(/Session unchanged|planned session/);
    expect(writes[0]).not.toHaveProperty("plan_id");
    expect(writes[0]).not.toHaveProperty("phase");
    await page.reload();
    await expect(page.locator("#today-checkin")).toHaveCount(0);
    await expect(page.getByRole("list", { name: "Today command status" })).toContainText("Done");
    await expect(page.locator("#today-decision")).toContainText("Train as your coach plans");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: test.info().outputPath(`plan-free-checkin-${width}.png`), fullPage: true });
  });
}

test("a plan-free athlete reporting high pain is pulled back, not cleared", async ({ page, baseURL }) => {
  await setup(page, baseURL!, { path: "tracking_first" });
  await page.goto("/today");
  const checkin = page.locator("#today-checkin");
  await checkin.getByRole("group", { name: "Pain", exact: true }).getByRole("button", { name: "High", exact: true }).click();
  await checkin.getByRole("button", { name: "Submit check-in", exact: true }).click();
  await expect(page.locator("#today-decision")).toContainText("Use rehab or easy mobility only");
  await expect(page.locator("#today-decision")).not.toContainText("Train as your coach plans");
});

for (const width of [320, 390, 1280]) {
  test(`sign-up asks the sport first, then the start, and the log shows that sport (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const errors: string[] = []; page.on("pageerror", e => errors.push(e.message));
    const writes = await setup(page, baseURL!, { sports: [] });
    await page.goto("/get-started");
    // One page: a compact sport pill row, then the two ways to start.
    await expect(page.getByRole("heading", { name: "How would you like to start?" })).toBeVisible();
    const sports = page.getByRole("group", { name: "Your sport" });
    await expect(sports.getByRole("button")).toHaveText(["Boxing", "Muay Thai", "Kickboxing", "MMA", "BJJ", "Wrestling", "Other"]);
    await expect(sports.locator('[aria-pressed="true"]')).toHaveCount(0);
    const pill = (await sports.getByRole("button").first().boundingBox())!;
    expect(pill.height).toBeLessThanOrEqual(44);
    // Starting without a sport asks for one and saves nothing.
    await page.getByRole("button", { name: /I already have a coach/ }).click();
    const hint = page.getByRole("alert").filter({ hasText: "Pick your sport first." });
    await expect(hint).toBeVisible();
    await expect(sports.getByRole("button", { name: "Boxing", exact: true })).toBeFocused();
    expect(writes).toHaveLength(0);
    expect((await new AxeBuilder({ page }).include("main").analyze()).violations).toEqual([]);
    await sports.getByRole("button", { name: "BJJ", exact: true }).click();
    await sports.getByRole("button", { name: "Muay Thai", exact: true }).click();
    await expect(sports.locator('[aria-pressed="true"]')).toHaveText(["Muay Thai"]);
    await expect(hint).toHaveCount(0);
    await page.screenshot({ path: test.info().outputPath(`start-${width}.png`) });
    await page.getByRole("button", { name: /I already have a coach/ }).click();
    // Tracking athletes land on Home; logging lives on Today.
    await expect(page).toHaveURL(/\/$/);
    await page.goto("/today");
    expect(writes[0]).toEqual({ starting_path: "tracking_first", technical_style: ["muay_thai"] });
    await page.getByRole("button", { name: "Log session", exact: true }).click();
    const form = page.getByRole("form", { name: "Log session", exact: true });
    const activity = form.getByRole("group", { name: "Activity", exact: true });
    await expect(activity.locator("[data-activity]")).toHaveText(["Pads", "Bag work", "Clinch", "Technical drills", "Sparring", "Strength", "Conditioning", "Roadwork", "Other"]);
    await activity.getByRole("button", { name: "Clinch", exact: true }).click();
    await form.getByRole("button", { name: "60", exact: true }).click();
    await form.getByRole("button", { name: "Hard", exact: true }).click();
    expect((await new AxeBuilder({ page }).include("dialog").analyze()).violations).toEqual([]);
    await page.screenshot({ path: test.info().outputPath(`muay-thai-log-${width}.png`) });
    await form.getByRole("button", { name: "Save Session", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "Log session", exact: true })).toHaveCount(0);
    expect(writes.at(-1)).toMatchObject({ activity: "other", sport: "muay_thai", session_type: "clinch", duration_minutes: 60, effort: "hard" });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect(errors).toEqual([]);
  });
}

test("Other sport keeps the original log choices and saves no sport", async ({ page, baseURL }) => {
  const writes = await setup(page, baseURL!, { sports: [] });
  await page.goto("/get-started");
  await page.getByRole("button", { name: "Other", exact: true }).click();
  await page.getByRole("button", { name: /I already have a coach/ }).click();
  // Tracking athletes land on Home; logging lives on Today.
  await expect(page).toHaveURL(/\/$/);
  await page.goto("/today");
  expect(writes[0]).toEqual({ starting_path: "tracking_first" });
  await page.getByRole("button", { name: "Log session", exact: true }).click();
  await expect(page.getByRole("group", { name: "Activity", exact: true }).locator("[data-activity]"))
    .toHaveText(["Boxing", "Sparring", "Strength", "Conditioning", "Roadwork", "Other"]);
});

test("a tracking athlete with nothing logged gets a first-session CTA on Home", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page, baseURL!, { path: "tracking_first" });
  await page.goto("/");
  await expect(page).toHaveURL(/\/$/);
  const hero = page.locator(".overview-decision-lead");
  await expect(hero.getByRole("heading", { level: 1 })).toHaveText("How are you today?");
  await expect(hero.getByRole("link", { name: "Check in" })).toHaveAttribute("href", "/today#today-checkin");
  await expect(page.getByText("Build your fight camp in minutes.")).toHaveCount(0);
  await expect(page.getByText("Fight countdown")).toHaveCount(0);
  await expect(page.getByLabel("Training status")).toContainText("0 sessions");
  await page.screenshot({ path: test.info().outputPath("tracking-home-empty.png") });
});

