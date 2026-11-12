import { expect, test, type Locator, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { isolateFromNetwork } from "./support";
import type { TrainingHabitPreferences } from "../lib/training-habits";
import type { TrainingHistorySession } from "../lib/training-history";

const athlete = "00000000-0000-4000-8000-000000000001";
const token = "log-training-test-token";
const storageKey = `sb-${new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").hostname.split(".")[0]}-auth-token`;
const prescribed: TrainingHistorySession = {
  id: "prescribed", source: "unlxck", title: "Strength & Power", status: "done", training_day: "2026-10-10",
  activity: null, duration_minutes: null, effort: null, plan_id: "prior-plan",
  session_rpe: 7, notes: "Coach session",
};

async function setup(page: Page, baseURL: string, lostResponse: boolean | "network" = false, failMutations = false, initialSessions: TrainingHistorySession[] = [prescribed], initialHabits: TrainingHabitPreferences = { enabled: true, decisions: [] }, trainingDay = "2026-10-18") {
  await isolateFromNetwork(page, baseURL);
  const testNow = new Date(`${trainingDay}T12:00:00Z`);
  await page.clock.setFixedTime(testNow);
  await page.addInitScript(([key, value]) => localStorage.setItem(key, value), [storageKey, JSON.stringify({
    access_token: token, refresh_token: "test-refresh", token_type: "bearer",
    expires_in: 86400, expires_at: Math.floor(testNow.getTime() / 1000) + 86400,
    user: { id: athlete, email: "fighter@example.com", aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} },
  })]);
  let sessions = [...initialSessions];
  const created = new Map<string, TrainingHistorySession>();
  const submissions: Record<string, unknown>[] = [];
  let habits = initialHabits;
  let habitRevision = 0;
  let editAttempts = 0;
  let deleteAttempts = 0;
  await page.route("**/api/**", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/api/me" && request.method() === "PUT") {
      const body = request.postDataJSON();
      if (body.training_habits_revision !== habitRevision) return json({ detail: "Routines changed on another device. Refresh and try again." }, 409);
      habits = body.training_habits; habitRevision++;
    }
    if (path === "/api/me") return json({ profile: {
      training_habits: habits, training_habits_revision: habitRevision,
      athlete_id: athlete, email: "fighter@example.com", full_name: "Carlos", role: "athlete", access_status: "approved",
      technical_style: ["boxing"], tactical_style: [], stance: "orthodox", professional_status: "professional", record: "",
      athlete_timezone: "Europe/London", athlete_locale: "en", appearance_mode: "dark", nutrition_profile: {},
      date_of_birth: "1995-01-01", meets_minimum_age: true, is_minor: false, terms_accepted: true,
      health_consent_granted: true, private_trial_ack_at: "2026-01-01T12:00:00Z",
    }, plan_count: 0, latest_plan: null, latest_intake: null, username_rate_limit: {} });
    if (path === "/api/today") return json({
      active_plan: { id: "plan-1", name: "Camp", phase: "SPP" },
      today: { training_day: trainingDay, recommendation_state: "not_checked_in", decision_tier: "not_checked_in",
        warnings: [], next_session: {}, completion_status: "not_started", session_scope: "none", session_label: "No session" },
      open_injuries: [], risk_watch: [], week_summary: {}, quick_actions: [],
    });
    if (path === "/api/plans/plan-1") return json({ outputs: {} });
    if (path === "/api/plans/active") return json({ detail: "No active plan" }, 404);
    if (path === "/api/plans") return json([]);
    if (path === "/api/training/history") return json({ sessions });
    // History reads linked sparring reports (#2935); none exist in these flows.
    if (path === "/api/today/sparring-logs") return json({ logs: [] });
    if (path === "/api/training/sessions" && request.method() === "POST") {
      expect(request.headers().authorization).toBe(`Bearer ${token}`);
      const body = request.postDataJSON();
      submissions.push(body);
      if (!created.has(body.client_request_id)) {
        const row: TrainingHistorySession = {
        id: `manual-${created.size}`, source: "external", training_day: body.training_day ?? trainingDay, status: "done",
        plan_id: null,
        title: body.activity.charAt(0).toUpperCase() + body.activity.slice(1), activity: body.activity, duration_minutes: body.duration_minutes, effort: body.effort,
        sport: body.sport ?? null, session_type: body.session_type ?? null,
      };
        created.set(body.client_request_id, row);
        sessions = [row, ...sessions];
      }
      if (lostResponse === "network" && submissions.length === 1) return route.abort("failed");
      if (lostResponse && submissions.length === 1) return json({ detail: "Saved, but response lost. Retry." }, 503);
      return json(created.get(body.client_request_id), 201);
    }
    if (path.startsWith("/api/training/sessions/") && request.method() === "PATCH") {
      const body = request.postDataJSON();
      editAttempts += 1;
      if (failMutations && editAttempts === 1) return route.fulfill({ status: 502, contentType: "text/html", body: "<html>Bad Gateway</html>" });
      sessions[0] = { ...sessions[0], ...body, title: "Boxing" };
      return json(sessions[0]);
    }
    if (path.startsWith("/api/training/sessions/") && request.method() === "DELETE") {
      deleteAttempts += 1;
      if (failMutations && deleteAttempts === 1) return route.fulfill({ status: 502, contentType: "text/html", body: "<html>Bad Gateway</html>" });
      sessions = sessions.filter(row => row.source !== "external");
      return route.fulfill({ status: 204 });
    }
    return json({ notifications: [], unread_count: 0, level: 1, total_xp: 0 });
  });
  return submissions;
}

/** The optional post-sparring follow-up (#2934) opens after a sparring save. */
async function skipSparringFollowUp(page: Page) {
  const prompt = page.getByRole("dialog", { name: "Log your sparring" });
  await prompt.getByRole("button", { name: "Skip", exact: true }).click();
  await expect(prompt).toHaveCount(0);
}

async function choose(scope: Page | Locator, page: Page, field: string, option: string) {
  const pick = scope.getByRole("group", { name: field, exact: true }).getByRole("button", { name: option, exact: true });
  await pick.click();
  await expect(pick).toHaveAttribute("aria-pressed", "true");
}

for (const width of [390, 1280]) {
  test(`post-session notification prompt is focused and dismisses once (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await setup(page, baseURL!);
    await page.addInitScript((interruptFirst) => {
      const originalOpen = HTMLDialogElement.prototype.showModal;
      let interruptNextOffer = interruptFirst;
      HTMLDialogElement.prototype.showModal = function () {
        if (this.getAttribute("aria-labelledby") === "push-prompt-title") {
          document.documentElement.dataset.pushSeenBeforeOpen = localStorage.getItem("unlxck:push-prompt:v1:00000000-0000-4000-8000-000000000001") ?? "unseen";
          if (interruptNextOffer) {
            interruptNextOffer = false;
            document.documentElement.dataset.pushOpenInterrupted = "true";
            throw new DOMException("Navigation interrupted opening", "InvalidStateError");
          }
        }
        originalOpen.call(this);
      };
      Object.defineProperty(window, "PushManager", { configurable: true, value: function () {} });
      Object.defineProperty(window, "Notification", { configurable: true, value: {
        permission: "default", requestPermission: async () => "granted",
      } });
      Object.defineProperty(navigator.serviceWorker, "getRegistration", { configurable: true, value: async () => ({
        active: {}, pushManager: { getSubscription: async () => null,
          subscribe: async () => ({ toJSON: () => ({ endpoint: "https://push.example.test/device", keys: { p256dh: "key", auth: "auth" } }) }) },
      }) });
    }, width === 390);
    await page.route("**/api/push/settings", route => route.fulfill({ json: {
      enabled: true, public_key: "AQAB", preferences: { push_enabled: true }, subscription_endpoints: [],
    } }));
    await page.goto("/plan");
    await expect(page).toHaveTitle(/UNLXCK/i);
    const prompt = page.getByRole("dialog", { name: "UNLXCK your next move." });
    await expect(prompt).toHaveCount(0);
    await page.getByRole("button", { name: "Log session", exact: true }).click();
    const form = page.getByRole("form", { name: "Log session", exact: true });
    await choose(form, page, "Activity", "Running");
    await choose(form, page, "Duration", "30");
    await choose(form, page, "Effort", "Moderate");
    await form.getByRole("button", { name: "Save Session", exact: true }).click();
    const takeaway = page.getByRole("dialog", { name: "Session saved", exact: true });
    await expect(takeaway).toBeVisible();
    await expect(prompt).toHaveCount(0);
    await takeaway.getByRole("button", { name: "Optional — skip anytime" }).click();
    if (width === 390) {
      await expect(page.locator("html")).toHaveAttribute("data-push-open-interrupted", "true");
      await expect(prompt).toBeHidden();
      expect(await page.evaluate(() => localStorage.getItem("unlxck:push-prompt:v1:00000000-0000-4000-8000-000000000001"))).toBeNull();
      expect(await page.evaluate(() => document.body.style.position)).not.toBe("fixed");
      await page.evaluate(value => window.dispatchEvent(new CustomEvent("unlxck:session-saved", { detail: { token: value } })), token);
    }
    await expect(prompt).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("data-push-seen-before-open", "unseen");
    expect(await page.evaluate(() => localStorage.getItem("unlxck:push-prompt:v1:00000000-0000-4000-8000-000000000001"))).toBe("seen");
    await expect(prompt.getByRole("button", { name: "Enable reminders" })).toBeFocused();
    expect(await prompt.evaluate(node => getComputedStyle(node, "::backdrop").backdropFilter)).toBe("blur(12px)");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect((await new AxeBuilder({ page }).include("dialog[open]").analyze()).violations).toEqual([]);
    await page.screenshot({ path: test.info().outputPath(`push-prompt-${width}.png`), animations: "disabled" });
    await page.emulateMedia({ reducedMotion: "reduce" });
    expect(await prompt.getByRole("button", { name: "Enable reminders" }).evaluate(node => getComputedStyle(node, "::after").animationName)).toBe("none");
    if (width === 390) {
      await prompt.getByRole("button", { name: "Not now", exact: true }).click();
    } else {
      const saved = page.waitForRequest(request => request.url().endsWith("/api/push/subscriptions") && request.method() === "POST");
      await prompt.getByRole("button", { name: "Enable reminders" }).click();
      expect((await saved).postDataJSON()).toMatchObject({ endpoint: "https://push.example.test/device" });
    }
    await expect(prompt).toHaveCount(0);
    await page.reload();
    // A fresh session-save event cannot re-open the one-time prompt.
    await page.evaluate(value => window.dispatchEvent(new CustomEvent("unlxck:session-saved", { detail: { token: value } })), token);
    await expect(prompt).toHaveCount(0);
    expect(errors).toEqual([]);
  });
}

for (const width of [390, 1280]) {
  test(`Plan logs work; History edits and deletes it (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    const submissions = await setup(page, baseURL!, false, true);
    await page.goto("/plan");
    await page.getByRole("button", { name: "Log session", exact: true }).click();
    const form = page.getByRole("form", { name: "Log session", exact: true });
    await expect(form.getByRole("combobox")).toHaveCount(0);
    // The athlete boxes: the same single grid holds boxing session types, then S&C, roadwork and other.
    await expect(form.getByRole("group", { name: "Activity", exact: true }).locator("[data-activity]")).toHaveText(
      ["Pads", "Bag work", "Shadowboxing", "Technical drills", "Sparring", "Strength", "Conditioning", "Running", "Other"]);
    await expect(form.getByLabel("Training date (optional)")).toBeHidden();
    await expect(form.getByRole("button", { name: "Pads", exact: true })).toBeFocused();
    await choose(form, page, "Activity", "Sparring");
    await choose(form, page, "Duration", "60");
    await choose(form, page, "Effort", "Hard");
    // Compare rendered styling with the existing Plan CTA, catching unstyled
    // default buttons even if saving still works functionally.
    const ctaAppearance = (element: Element) => {
      const css = getComputedStyle(element);
      return [css.backgroundColor, css.backgroundImage, css.borderRadius, css.color];
    };
    expect(await form.getByRole("button", { name: "Save Session", exact: true }).evaluate(ctaAppearance))
      .toEqual(await page.getByRole("link", { name: "Create a plan" }).evaluate(ctaAppearance));
    await page.screenshot({ animations: "disabled", path: test.info().outputPath("log-training-form.png") });
    expect((await new AxeBuilder({ page }).include('dialog').analyze()).violations).toEqual([]);
    await form.getByRole("button", { name: "Save Session", exact: true }).click();
    await skipSparringFollowUp(page);
    await expect(page.getByRole("region", { name: "Log session", exact: true }).getByRole("status")).toContainText("logged successfully");
    await expect(page.locator(".history-list")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Edit Sparring" })).toHaveCount(0);
    expect(submissions[0]).toMatchObject({ activity: "sparring", duration_minutes: 60, effort: "hard" });
    expect(Object.keys(submissions[0]).sort()).toEqual(["activity", "client_request_id", "duration_minutes", "effort", "session_type", "sport"]);
    expect(submissions[0]).toMatchObject({ sport: "boxing", session_type: "sparring" });
    await page.reload();
    await expect(page.getByRole("button", { name: "Edit Sparring" })).toHaveCount(0);
    // Phones reach History from the bottom bar; desktop from the sidebar.
    await page.getByRole("link", { name: /^History/ }).filter({ visible: true }).first().click();
    const history = page.locator(".history-list");
    await expect(history).toContainText("Sparring");
    await expect(history.getByRole("button", { name: "Edit Strength & Power" })).toHaveCount(0);
    const editButton = history.getByRole("button", { name: "Edit Sparring", exact: true });
    const deleteButton = history.getByRole("button", { name: "Delete Sparring", exact: true });
    const editBox = await editButton.boundingBox();
    const deleteBox = await deleteButton.boundingBox();
    expect(Math.abs(editBox!.y - deleteBox!.y)).toBeLessThanOrEqual(2);
    expect(editBox!.width).toBeLessThanOrEqual(44);
    expect(deleteBox!.width).toBeLessThan(120);
    expect(deleteBox!.x).toBeGreaterThan(editBox!.x);
    await page.screenshot({ animations: "disabled", path: test.info().outputPath("history-compact-controls.png") });
    expect((await new AxeBuilder({ page }).include(".history-list").analyze()).violations).toEqual([]);
    await editButton.click();
    await choose(page, page, "Activity", "Pads");
    await choose(page, page, "Duration", "45");
    await choose(page, page, "Effort", "Moderate");
    await page.getByRole("button", { name: "Save Session", exact: true }).click();
    await expect(history).toContainText("45 min");
    await expect(history).toContainText("Moderate");
    await page.goto("/history");
    await expect(page.locator(".history-list")).toContainText("Boxing");
    await expect(page.locator(".history-list")).toContainText("Added by you");
    await expect(page.locator(".history-list")).toContainText("45 min");
    await expect(page.locator(".history-list")).toContainText("RPE 7/10");
    await expect(page.locator(".history-list")).toContainText("Coach session");
    await expect(page.locator(".history-list > li")).toHaveCount(2);
    await page.getByRole("button", { name: "Delete Boxing", exact: true }).click();
    await expect(history.getByRole("listitem")).toHaveCount(1);
    await page.reload();
    await expect(history).not.toContainText("Boxing");
    await page.goto("/plan");
    await expect(page.locator(".history-list")).toHaveCount(0);
    await page.getByRole("button", { name: "Log session", exact: true }).click();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Log session", exact: true })).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
  });
}

test("retry after a lost save response keeps the same request ID", async ({ page, baseURL }) => {
  const submissions = await setup(page, baseURL!, true);
  await page.goto("/plans");
  await page.getByRole("button", { name: "Log session", exact: true }).click();
  await choose(page, page, "Activity", "Sparring");
  await choose(page, page, "Duration", "60");
  await choose(page, page, "Effort", "Hard");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText("response lost");
  // Re-selecting the same quick pick must still retry the same payload safely.
  await choose(page, page, "Effort", "Hard");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("region", { name: "Log session", exact: true }).getByRole("status")).toContainText("logged successfully");
  expect(submissions).toHaveLength(2);
  expect(submissions[1].client_request_id).toBe(submissions[0].client_request_id);
});

test("log yesterday's sparring with the optional date and preserve it after editing", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const submissions = await setup(page, baseURL!);
  await page.goto("/plans");
  await page.getByRole("button", { name: "Log session", exact: true }).click();
  await choose(page, page, "Activity", "Sparring");
  await choose(page, page, "Duration", "60");
  await choose(page, page, "Effort", "Hard");
  await page.locator("dialog summary").click();
  await page.getByLabel("Training date (optional)").fill("2026-10-09");
  await page.screenshot({ animations: "disabled", path: test.info().outputPath("backdated-training-form.png") });
  expect((await new AxeBuilder({ page }).include('dialog').analyze()).violations).toEqual([]);
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  expect(submissions[0]).toMatchObject({ activity: "sparring", duration_minutes: 60, effort: "hard", training_day: "2026-10-09" });
  await page.goto("/history");
  const history = page.locator(".history-list");
  await expect(history).toContainText("Fri 09 Oct 2026");
  await page.reload();
  await page.getByRole("button", { name: "Edit Sparring", exact: true }).click();
  await choose(page, page, "Duration", "45");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(history).toContainText("Fri 09 Oct 2026");
  await expect(history).toContainText("45 min");
  await expect(history).toContainText("Hard");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});


test("failed edits and deletes preserve the session and allow another attempt", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page, baseURL!);
  await page.goto("/plans");
  await page.getByRole("button", { name: "Log session", exact: true }).click();
  await choose(page, page, "Activity", "Sparring");
  await choose(page, page, "Duration", "60");
  await choose(page, page, "Effort", "Hard");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await skipSparringFollowUp(page);
  await page.goto("/history");
  let fail = true;
  const attempts: string[] = [];
  await page.route("**/api/training/sessions/*", async route => {
    attempts.push(route.request().method());
    if (fail && route.request().method() === "DELETE") return route.fulfill({ status: 502, contentType: "text/html", body: "<html>Bad Gateway</html>" });
    if (fail) return route.fulfill({ status: 403, contentType: "application/json", body: JSON.stringify({ detail: "Session could not be changed." }) });
    await route.fallback();
  });
  const history = page.locator(".history-list");
  await page.getByRole("button", { name: "Edit Sparring", exact: true }).click();
  await choose(page, page, "Duration", "45");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText("Session could not be changed");
  await expect(page.getByRole("dialog").getByRole("button", { name: "45", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(history).toContainText("60 min");
  expect(attempts).toEqual(["PATCH"]);
  fail = false;
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(history).toContainText("45 min");
  fail = true;
  await page.getByRole("button", { name: "Delete Boxing", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "The training service is temporarily unavailable" })).toBeVisible();
  await expect(history).toContainText("Boxing");
  await expect(page.getByRole("button", { name: "Delete Boxing", exact: true })).toBeEnabled();
  expect(attempts).toEqual(["PATCH", "PATCH", "DELETE", "DELETE", "DELETE"]);
  fail = false;
  await page.getByRole("button", { name: "Delete Boxing", exact: true }).click();
  await expect(history).not.toContainText("Boxing");
  await page.reload();
  await expect(history.getByRole("listitem")).toHaveCount(1);
});

const manual: TrainingHistorySession = {
  id: "backdated-boxing", source: "external", title: "Boxing", status: "done", training_day: "2026-10-09",
  activity: "boxing", duration_minutes: 60, effort: "hard", plan_id: null,
};

test("Today without manual history offers the simple form", async ({ page, baseURL }) => {
  await setup(page, baseURL!, false, false, []);
  await page.goto("/today");
  await expect(page.getByRole("button", { name: "Log session", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Repeat last session" })).toHaveCount(0);
  await page.getByRole("button", { name: "Log session", exact: true }).click();
  await expect(page.getByRole("group", { name: "Activity", exact: true }).locator('[aria-pressed="true"]')).toHaveCount(0);
  await expect(page.getByRole("group", { name: "Duration", exact: true }).locator('[aria-pressed="true"]')).toHaveCount(0);
  await expect(page.getByRole("group", { name: "Effort", exact: true }).locator('[aria-pressed="true"]')).toHaveCount(0);
});

for (const width of [390, 1280]) {
test(`repeat on Today needs only effort and save; a second session stays separate (${width}px)`, async ({ page, baseURL }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => {
    // isolateFromNetwork deliberately aborts external Supabase/Sentry traffic.
    if (message.type() === "error" && message.text() !== "Failed to load resource: net::ERR_FAILED") errors.push(message.text());
  });
  page.on("requestfailed", request => {
    // Next cancels in-flight route prefetches when this test changes pages.
    if (new URL(request.url()).origin === new URL(baseURL!).origin && request.failure()?.errorText !== "net::ERR_ABORTED") {
      errors.push(`App resource failed: ${request.url()} (${request.failure()?.errorText})`);
    }
  });
  await page.setViewportSize({ width, height: 844 });
  const submissions = await setup(page, baseURL!, false, false, [prescribed, manual]);
  let todayReads = 0;
  page.on("request", request => { if (new URL(request.url()).pathname === "/api/today") todayReads++; });
  await page.goto("/today");
  const repeat = page.getByRole("button", { name: "Repeat last session", exact: true });
  await expect(repeat).toContainText("Boxing · 60 min");
  await expect(repeat).not.toContainText("Last recorded");
  await expect(repeat).not.toContainText("Review and log");
  expect((await repeat.boundingBox())!.height).toBeLessThanOrEqual(80);
  const entryLink = page.getByRole("link", { name: "Log session", exact: true });
  await expect(entryLink).toBeInViewport();
  await page.screenshot({ animations: "disabled", path: "/tmp/quick-log-today-first-viewport.png" });
  await entryLink.click();
  await expect(repeat).toBeInViewport();
  await page.screenshot({ animations: "disabled", path: "/tmp/quick-log-compact-row.png" });
  await repeat.focus();
  await page.keyboard.press("Enter");
  const effort = page.getByRole("group", { name: "Effort", exact: true }).getByRole("button", { name: "Easy", exact: true });
  await expect(effort).toBeFocused();
  await expect(page.getByRole("dialog")).not.toContainText("Last effort:");
  await expect(effort).toHaveAttribute("aria-pressed", "false");
  await expect(page.getByRole("dialog")).toContainText("60 min · From previous session");
  await expect(page.getByRole("group", { name: "Activity", exact: true })).toBeHidden();
  expect(submissions).toHaveLength(0);
  await page.screenshot({ animations: "disabled", path: "/tmp/quick-log-review-mobile.png" });
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText("Select an activity and effort");
  expect(submissions).toHaveLength(0);
  await effort.press("Enter");
  await expect(effort).toHaveAttribute("aria-pressed", "true");
  expect((await new AxeBuilder({ page }).include("dialog").analyze()).violations).toEqual([]);
  // Hold the authoritative refresh after persistence: old guidance must yield
  // to the pending state without unmounting the save acknowledgement.
  let releaseToday!: () => void;
  const pendingToday = new Promise<void>(resolve => { releaseToday = resolve; });
  await page.route("**/api/today", async route => {
    await pendingToday;
    await route.fallback();
  });
  // Two synchronous submits must produce only one request, even before React renders busy state.
  await page.getByRole("form", { name: "Log session", exact: true }).evaluate((form: HTMLFormElement) => {
    form.requestSubmit(); form.requestSubmit();
  });
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(submissions).toHaveLength(1);
  expect(submissions[0]).toMatchObject({ activity: "boxing", duration_minutes: 60, effort: "easy" });
  expect(submissions[0]).not.toHaveProperty("training_day");
  await expect.poll(() => todayReads).toBeGreaterThan(1);
  await expect(page.getByRole("status").filter({ hasText: "Updating Today" })).toBeVisible();
  await page.getByText("Updating Today…", { exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ animations: "disabled", path: `/tmp/source-neutral-readiness-pending-${width}.png` });
  releaseToday();
  await expect(page.getByText("Updating Today…", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("status").filter({ hasText: "logged successfully" })).toBeVisible();
  await page.screenshot({ animations: "disabled", path: test.info().outputPath("today-recent-shortcut.png") });
  await page.getByRole("button", { name: "Log session", exact: true }).click();
  await choose(page, page, "Activity", "Strength");
  await choose(page, page, "Duration", "45");
  await choose(page, page, "Effort", "Moderate");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(submissions).toHaveLength(2);
  expect(submissions[1].client_request_id).not.toBe(submissions[0].client_request_id);
  await page.goto("/history");
  const rows = page.locator(".history-list > li");
  await expect(rows).toHaveCount(4);
  await expect(rows.filter({ hasText: "Fri 09 Oct 2026" })).toContainText("Hard");
  await expect(rows.filter({ hasText: "Sun 18 Oct 2026" })).toHaveCount(2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});
}

test("History repeat retries the same occurrence, preserves the source and creates fresh repeats", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const submissions = await setup(page, baseURL!, true, false, [prescribed, manual]);
  await page.goto("/history");
  await expect(page.getByRole("button", { name: "Repeat Strength & Power", exact: true })).toHaveCount(0);
  const source = page.locator(".history-list > li").filter({ hasText: "Fri 09 Oct 2026" });
  await source.getByRole("button", { name: "Repeat Boxing", exact: true }).click();
  await page.keyboard.press("Escape");
  expect(submissions).toHaveLength(0);
  await expect(source.getByRole("button", { name: "Repeat Boxing", exact: true })).toBeFocused();
  await source.getByRole("button", { name: "Repeat Boxing", exact: true }).click();
  await choose(page, page, "Effort", "Moderate");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText("response lost");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(submissions).toHaveLength(2);
  expect(submissions[1]).toEqual(submissions[0]);
  await expect(page.locator(".history-list > li")).toHaveCount(3);
  await expect(page.getByRole("status").filter({ hasText: "logged successfully" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expect(source).toContainText("60 min");
  await expect(source).toContainText("Hard");
  await source.getByRole("button", { name: "Repeat Boxing", exact: true }).click();
  await expect(page.getByRole("group", { name: "Effort", exact: true }).locator('[aria-pressed="true"]')).toHaveCount(0);
  await choose(page, page, "Effort", "Easy");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(submissions[2].client_request_id).not.toBe(submissions[0].client_request_id);
  await page.reload();
  await expect(page.locator(".history-list > li")).toHaveCount(4);
  await expect(source).toContainText("Hard");
});

test("expired save reports sign-in requirement without changing history", async ({ page, baseURL }) => {
  await setup(page, baseURL!, false, false, [manual]);
  await page.route("**/api/training/sessions", route => route.fulfill({ status: 401, contentType: "application/json", body: JSON.stringify({ detail: "Unauthorized" }) }));
  await page.goto("/history");
  await page.getByRole("button", { name: "Repeat Boxing", exact: true }).click();
  await choose(page, page, "Effort", "Easy");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText("Your session expired. Sign in again");
  await expect(page.getByRole("button", { name: "Save Session", exact: true })).toBeEnabled();
  await expect(page.locator(".history-list > li")).toHaveCount(1);
});


test("a dropped network response can retry without creating another session", async ({ page, baseURL }) => {
  const submissions = await setup(page, baseURL!, "network", false, [manual]);
  await page.goto("/history");
  await page.getByRole("button", { name: "Repeat Boxing", exact: true }).click();
  await choose(page, page, "Effort", "Easy");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toBeVisible();
  await expect(page.getByRole("dialog")).toContainText("60 min ·");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(submissions).toHaveLength(2);
  expect(submissions[1]).toEqual(submissions[0]);
  await expect(page.locator(".history-list > li")).toHaveCount(2);
});

test("Today logging remains available without an active plan", async ({ page, baseURL }) => {
  await setup(page, baseURL!, false, false, []);
  await page.route("**/api/today", route => route.fulfill({ contentType: "application/json", body: JSON.stringify({
    // The shape the API serialises for an athlete with no programme.
    active_plan: {}, today: { training_day: "2026-10-18", recommendation_state: "not_checked_in", decision_tier: "not_checked_in", warnings: [], next_session: {}, completion_status: "not_started", session_scope: "none" },
    open_injuries: [], risk_watch: [],
  }) }));
  const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
  await page.goto("/today");
  await expect(page.getByRole("navigation", { name: "Quick actions" }).getByRole("link", { name: "Generate my S&C plan" })).toBeVisible();
  await page.getByRole("button", { name: "Log session", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(errors).toEqual([]);
});


const mondayHistory: TrainingHistorySession[] = ["2026-10-12", "2026-10-05"].map(day => ({ ...manual, id: day, training_day: day }));

async function habitSetup(page: Page, baseURL: string, rows = mondayHistory, preferences?: TrainingHabitPreferences) {
  return setup(page, baseURL, false, false, rows, preferences, "2026-10-19");
}

test("one week yields a tentative suggestion with blank effort; no routine is created", async ({ page, baseURL }) => {
  const submissions = await habitSetup(page, baseURL!, mondayHistory.slice(0, 1));
  await page.goto("/today");
  const suggestion = page.getByRole("button", { name: "Suggested today", exact: true });
  await expect(suggestion).toContainText("Boxing · 60 min");
  await expect(suggestion).toHaveAccessibleDescription("Boxing · 60 min");
  await expect(page.getByText("Boxing every Monday?", { exact: true })).toHaveCount(0);
  await suggestion.click();
  await expect(page.getByRole("group", { name: "Effort", exact: true }).locator('[aria-pressed="true"]')).toHaveCount(0);
  expect(submissions).toHaveLength(0);
});

test("confirm a pattern in one tap, persist it and log through the same form", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const submissions = await habitSetup(page, baseURL!);
  await page.goto("/today");
  await expect(page.getByText("Boxing every Monday?", { exact: true })).toBeVisible();
  await expect(page.getByText("Logged two weeks in a row.", { exact: true })).toBeVisible();
  expect((await new AxeBuilder({ page }).include("#today-training-log").analyze()).violations).toEqual([]);
  await page.locator("#today-training-log").scrollIntoViewIfNeeded();
  await page.screenshot({ animations: "disabled", path: "/tmp/habit-pattern-mobile.png" });
  await page.getByRole("button", { name: "Yes, usually", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Routine saved." })).toBeVisible();
  expect(submissions).toHaveLength(0);
  await expect(page.getByRole("button", { name: "Your usual Monday", exact: true })).toContainText("60 min");
  await page.reload();
  await page.getByRole("button", { name: "Your usual Monday", exact: true }).click();
  await expect(page.getByRole("group", { name: "Effort", exact: true }).locator('[aria-pressed="true"]')).toHaveCount(0);
  await choose(page, page, "Effort", "Easy");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(submissions).toHaveLength(1);
  expect(submissions[0]).toMatchObject({ activity: "boxing", duration_minutes: 60, effort: "easy" });
  expect(submissions[0]).not.toHaveProperty("plan_id");
  await expect(page.getByRole("button", { name: "Your usual Monday", exact: true })).toHaveCount(0);
});

test("Not always is remembered; Today has no habit settings to manage", async ({ page, baseURL }) => {
  await habitSetup(page, baseURL!);
  await page.goto("/today");
  await expect(page.getByText("Training habits", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Training history", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Not always", exact: true }).click();
  await page.reload();
  await expect(page.getByText("Boxing every Monday?", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Repeat last session", exact: true })).toBeVisible();
});

test("preference errors do not pretend to confirm a routine", async ({ page, baseURL }) => {
  await habitSetup(page, baseURL!);
  await page.route("**/api/me", async route => {
    if (route.request().method() === "PUT") return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Couldn’t save. Try again." }) });
    await route.fallback();
  });
  await page.goto("/today");
  await page.getByRole("button", { name: "Yes, usually", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Couldn’t save" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Yes, usually", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Your usual Monday", exact: true })).toHaveCount(0);
});

test("sparring history does not predict contact permission", async ({ page, baseURL }) => {
  await habitSetup(page, baseURL!, mondayHistory.map(row => ({ ...row, activity: "sparring", title: "Sparring" })));
  await page.goto("/today");
  await expect(page.getByRole("button", { name: "Repeat last session", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Suggested today", exact: true })).toHaveCount(0);
  await expect(page.getByText("Sparring every Monday?", { exact: true })).toHaveCount(0);
});

test("changed routines need confirmation; one-off changes do not overwrite them", async ({ page, baseURL }) => {
  const preferences: TrainingHabitPreferences = { enabled: true, decisions: [{ weekday: 0, activity: "boxing", duration_minutes: 60, state: "confirmed", decided_on: "2026-09-20" }] };
  const rows = mondayHistory.map(row => ({ ...row, activity: "strength" as const, title: "Strength", duration_minutes: 45 }));
  await habitSetup(page, baseURL!, rows, preferences);
  await page.goto("/today");
  await expect(page.getByRole("button", { name: "Your usual Monday", exact: true })).toContainText("Boxing");
  await expect(page.getByText("Switch Monday to Strength?", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Yes, update", exact: true }).click();
  await expect(page.getByRole("button", { name: "Your usual Monday", exact: true })).toContainText("Strength · 45 min");
  await page.reload();
  await expect(page.getByRole("button", { name: "Your usual Monday", exact: true })).toContainText("Strength · 45 min");
});

for (const mode of ["coach", "injury"] as const) {
  test(`${mode} context outranks learned logging suggestions`, async ({ page, baseURL }) => {
    await habitSetup(page, baseURL!);
    await page.route("**/api/today", route => route.fulfill({ contentType: "application/json", body: JSON.stringify({
      active_plan: { id: "plan-1", name: "Camp", phase: "SPP" },
      today: { training_day: "2026-10-19", recommendation_state: mode === "injury" ? "pull_back" : "train_as_planned",
        decision_tier: mode === "injury" ? "stop" : "green", next_session: { coach_led_contact: mode === "coach" ? "sparring" : undefined },
        session_scope: "today", completion_status: "not_started", session_label: "Today’s session" },
      open_injuries: [], risk_watch: [], week_summary: {}, quick_actions: [],
    }) }));
    await page.goto("/today");
    await expect(page.getByRole("button", { name: "Repeat last session", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Suggested today", exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Yes, usually", exact: true })).toHaveCount(0);
  });
}

test("retrying a lost routine response recovers the saved intention", async ({ page, baseURL }) => {
  const submissions = await habitSetup(page, baseURL!);
  const initialRead = page.waitForResponse(response => new URL(response.url()).pathname === "/api/me" && response.request().method() === "GET");
  await page.goto("/today");
  const initial = await (await initialRead).json();
  let saved: TrainingHabitPreferences | undefined;
  await page.route("**/api/me", async route => {
    if (route.request().method() === "PUT") {
      if (!saved) {
        saved = route.request().postDataJSON().training_habits;
        return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Response lost. Retry." }) });
      }
      return route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "Routines changed on another device. Refresh and try again." }) });
    }
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...initial, profile: {
      ...initial.profile, training_habits: saved ?? initial.profile.training_habits, training_habits_revision: saved ? 1 : 0,
    } }) });
  });
  await page.getByRole("button", { name: "Yes, usually", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Response lost" })).toBeVisible();
  await page.getByRole("button", { name: "Yes, usually", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Routine saved." })).toBeVisible();
  await expect(page.getByRole("button", { name: "Your usual Monday", exact: true })).toBeVisible();
  expect(submissions).toHaveLength(0);
});

for (const requiresUpdate of [false, true]) {
test(`sparring suggestion requires current contact clearance (update pending: ${requiresUpdate})`, async ({ page, baseURL }) => {
  const submissions = await habitSetup(page, baseURL!, mondayHistory.map(row => ({ ...row, activity: "sparring", title: "Sparring" })));
  await page.route("**/api/today", route => route.fulfill({ contentType: "application/json", body: JSON.stringify({
    active_plan: { id: "plan-1", name: "Camp", phase: "SPP" },
    today: { training_day: "2026-10-19", recommendation_state: "train_as_planned", decision_tier: "green",
      next_session: {}, session_scope: "none", completion_status: "not_started", session_label: "No session" },
    effective_clinician_clearance: { level: "train_contact", scopes: [], requires_update: requiresUpdate, limited_by: [] },
    open_injuries: [], risk_watch: [], week_summary: {}, quick_actions: [],
  }) }));
  await page.goto("/today");
  const suggestion = page.getByRole("button", { name: "Suggested today", exact: true });
  if (requiresUpdate) {
    await expect(page.getByRole("button", { name: "Repeat last session", exact: true })).toBeVisible();
    await expect(suggestion).toHaveCount(0);
    return;
  }
  await expect(suggestion).toContainText("Sparring · 60 min");
  await suggestion.click();
  await expect(page.getByRole("group", { name: "Effort", exact: true }).locator('[aria-pressed="true"]')).toHaveCount(0);
  expect(submissions).toHaveLength(0);
});

}

test("two confirmed weekday routines remain separate logging shortcuts", async ({ page, baseURL }) => {
  const preferences: TrainingHabitPreferences = { enabled: true, decisions: [
    { weekday: 0, activity: "boxing", duration_minutes: 60, state: "confirmed", decided_on: "2026-09-20" },
    { weekday: 0, activity: "strength", duration_minutes: 45, state: "confirmed", decided_on: "2026-09-20" },
  ] };
  const submissions = await habitSetup(page, baseURL!, mondayHistory, preferences);
  await page.goto("/today");
  const usual = page.getByRole("button", { name: "Your usual Monday", exact: true });
  await expect(usual).toContainText("Boxing · 60 min");
  await usual.click();
  await choose(page, page, "Effort", "Easy");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(usual).toContainText("Strength · 45 min");
  await usual.click();
  await expect(page.getByRole("dialog")).toContainText("45 min ·");
  await expect(page.getByRole("group", { name: "Effort", exact: true }).locator('[aria-pressed="true"]')).toHaveCount(0);
  expect(submissions).toHaveLength(1);
});

test("confirmed routines survive a failed history read without detecting new patterns", async ({ page, baseURL }) => {
  const preferences: TrainingHabitPreferences = { enabled: true, decisions: [
    { weekday: 0, activity: "boxing", duration_minutes: 50, state: "confirmed", decided_on: "2026-09-20" },
  ] };
  const submissions = await habitSetup(page, baseURL!, mondayHistory, preferences);
  await page.route("**/api/training/history**", route => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "History unavailable." }) }));
  await page.goto("/today");
  const usual = page.getByRole("button", { name: "Your usual Monday", exact: true });
  await expect(usual).toContainText("Boxing · 50 min");
  await expect(page.getByRole("button", { name: "Yes, usually", exact: true })).toHaveCount(0);
  await usual.click();
  await expect(page.getByRole("dialog")).toContainText("50 min ·");
  await expect(page.getByRole("group", { name: "Effort", exact: true }).locator('[aria-pressed="true"]')).toHaveCount(0);
  await expect(page.getByRole("dialog")).not.toContainText("Last effort:");
  expect(submissions).toHaveLength(0);
  await choose(page, page, "Effort", "Easy");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(usual).toHaveCount(0); // The locally saved session remains known during the outage.
  expect(submissions).toHaveLength(1);
});

test("failed history leaves simple logging available without inferred suggestions", async ({ page, baseURL }) => {
  await habitSetup(page, baseURL!);
  await page.route("**/api/training/history**", route => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "History unavailable." }) }));
  await page.goto("/today");
  await expect(page.getByRole("button", { name: "Suggested today", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Yes, usually", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Log session", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
});

test("history failure never restores a confirmed shortcut blocked by readiness", async ({ page, baseURL }) => {
  const preferences: TrainingHabitPreferences = { enabled: true, decisions: [
    { weekday: 0, activity: "boxing", duration_minutes: 50, state: "confirmed", decided_on: "2026-09-20" },
  ] };
  await habitSetup(page, baseURL!, mondayHistory, preferences);
  await page.route("**/api/training/history**", route => route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "History unavailable." }) }));
  await page.route("**/api/today", route => route.fulfill({ contentType: "application/json", body: JSON.stringify({
    active_plan: { id: "plan-1", name: "Camp", phase: "SPP" },
    today: { training_day: "2026-10-19", recommendation_state: "pull_back", decision_tier: "stop",
      next_session: {}, session_scope: "none", completion_status: "not_started", session_label: "No session" },
    open_injuries: [], risk_watch: [], week_summary: {}, quick_actions: [],
  }) }));
  await page.goto("/today");
  await expect(page.getByRole("button", { name: "Log session", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Your usual Monday", exact: true })).toHaveCount(0);
});

for (const width of [375, 390, 430]) {
  for (const mode of ["new", "repeat", "suggested"] as const) {
    test(`quick log ${mode} fits ${width}px and saves with quick picks`, async ({ page, baseURL }) => {
      await page.setViewportSize({ width, height: 844 });
      const errors: string[] = [];
      page.on("pageerror", error => errors.push(error.message));
      const submissions = mode === "suggested"
        ? await habitSetup(page, baseURL!)
        : await setup(page, baseURL!, false, false, mode === "repeat" ? [manual] : []);
      await page.goto("/today");
      await expect(page).toHaveURL(/\/today$/);
      await expect(page).toHaveTitle(/Unlxck/i);
      const trigger = page.getByRole("button", { name: mode === "new" ? "Log session" : mode === "repeat" ? "Repeat last session" : "Suggested today", exact: true });
      await trigger.click();
      const sheet = page.getByRole("dialog");
      await expect(sheet).toBeVisible();
      if (mode === "new") {
        await expect(sheet.getByRole("group", { name: "Duration", exact: true }).locator('[aria-pressed="true"]')).toHaveCount(0);
        await choose(sheet, page, "Activity", "Pads");
        await choose(sheet, page, "Duration", "90");
      } else {
        await expect(sheet).toContainText(mode === "repeat" ? "From previous session" : "Suggested today");
        await expect(sheet.getByRole("group", { name: "Activity", exact: true })).toBeHidden();
      }
      await expect(sheet.getByRole("group", { name: "Effort", exact: true }).locator('[aria-pressed="true"]')).toHaveCount(0);
      // A save without a fresh effort must never create an occurrence.
      await sheet.getByRole("button", { name: "Save Session", exact: true }).click();
      await expect(sheet.getByRole("alert")).toBeVisible();
      expect(submissions).toHaveLength(0);
      await choose(sheet, page, "Effort", "Moderate");
      const box = (await sheet.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(width);
      await expect(sheet.getByRole("button", { name: "Save Session", exact: true })).toBeInViewport();
      const targets = await sheet.locator('button:visible, summary:visible').evaluateAll(elements => elements.map(el => el.getBoundingClientRect().height));
      // Fractional sheet positions can produce a subpixel rounding difference.
      expect(targets.every(height => height >= 43.99)).toBe(true);
      expect(await sheet.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
      if (width === 390) {
        // Clear the validation message for comparable, resting-state screenshots.
        await sheet.getByRole("button", { name: "Close training log" }).click();
        await trigger.click();
        await page.screenshot({ animations: "disabled", path: `/tmp/quick-log-after-${mode}.png` });
        const compactBox = (await sheet.boundingBox())!;
        // A boxer's nine session-type choices take one more chip row than the original six.
        expect(compactBox.height).toBeLessThan(mode === "new" ? 530 : 290);
        console.log(`390px ${mode}:`, compactBox);
        if (mode === "new") {
          await choose(sheet, page, "Activity", "Pads");
          await choose(sheet, page, "Duration", "90");
        }
        await choose(sheet, page, "Effort", "Moderate");
        await page.screenshot({ animations: "disabled", path: `/tmp/quick-log-selected-${mode}.png` });
        expect((await new AxeBuilder({ page }).include('dialog').analyze()).violations).toEqual([]);
      }
      await sheet.getByRole("button", { name: "Save Session", exact: true }).click();
      await expect(sheet).toHaveCount(0);
      expect(submissions).toHaveLength(1);
      expect(submissions[0]).toMatchObject({ activity: "boxing", duration_minutes: mode === "new" ? 90 : 60, effort: "moderate" });
      expect(submissions[0].client_request_id).toMatch(/^[0-9a-f-]{36}$/);
      expect(submissions[0]).not.toHaveProperty("training_day");
      expect(errors).toEqual([]);
    });
  }
}

test("repeat preserves 75 minutes, can edit details, and supports the full custom duration range", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const submissions = await setup(page, baseURL!, false, false, [{ ...manual, duration_minutes: 75 }]);
  await page.goto("/today");
  await page.getByRole("button", { name: "Repeat last session" }).click();
  await expect(page.getByRole("dialog")).toContainText("75 min · From previous session");
  await page.getByRole("button", { name: "Edit details" }).click();
  await expect(page.getByLabel("Duration (minutes)")).toHaveValue("75");
  await choose(page, page, "Effort", "Easy");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(submissions[0].duration_minutes).toBe(75);
  await page.getByRole("button", { name: "Log session", exact: true }).click();
  await choose(page, page, "Activity", "Conditioning");
  await page.getByRole("button", { name: "Custom", exact: true }).click();
  const duration = page.getByLabel("Duration (minutes)");
  await expect(duration).toBeFocused();
  await duration.fill("600");
  await choose(page, page, "Effort", "Hard");
  // Approximate the reduced visual viewport available above an on-screen keyboard.
  await page.setViewportSize({ width: 390, height: 380 });
  await expect(page.getByRole("button", { name: "Save Session", exact: true })).toBeInViewport();
  await page.screenshot({ animations: "disabled", path: "/tmp/quick-log-keyboard-height.png" });
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(submissions[1]).toMatchObject({ activity: "conditioning", duration_minutes: 600, effort: "hard" });
});

test("date summary uses the athlete timezone and 03:00 rollover without sending an override", async ({ page, baseURL }) => {
  await setup(page, baseURL!, false, false, []);
  // London is UTC+1 in October: 01:30 UTC is still the previous training day.
  await page.clock.setFixedTime(new Date("2026-10-18T01:30:00Z"));
  await page.goto("/today");
  await page.getByRole("button", { name: "Log session", exact: true }).click();
  await expect(page.locator("dialog summary")).toContainText("Sat 17 Oct 2026");
  await page.locator("dialog summary").click();
  await expect(page.getByLabel("Training date (optional)")).toHaveAttribute("max", "2026-10-17");
  await expect(page.getByLabel("Training date (optional)")).toHaveValue("");
  await page.getByRole("button", { name: "Close training log" }).click();
  await expect(page.getByRole("button", { name: "Log session", exact: true })).toBeFocused();
});

test("History edit retains effort and record identity; repeat details can change without changing its source", async ({ page, baseURL }) => {
  const submissions = await setup(page, baseURL!, false, false, [{ ...manual, duration_minutes: 75 }]);
  const edits: { url: string; body: unknown }[] = [];
  page.on("request", request => {
    if (request.method() === "PATCH") edits.push({ url: new URL(request.url()).pathname, body: request.postDataJSON() });
  });
  await page.goto("/history");
  await page.getByRole("button", { name: "Edit Boxing", exact: true }).click();
  await expect(page.getByLabel("Duration (minutes)")).toHaveValue("75");
  await expect(page.getByRole("dialog").getByRole("button", { name: "Hard", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.getByLabel("Duration (minutes)").fill("1");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(edits).toEqual([{ url: `/api/training/sessions/${manual.id}`, body: { activity: "boxing", duration_minutes: 1, effort: "hard" } }]);
  expect(submissions).toHaveLength(0);
  await expect(page.locator(".history-list > li")).toHaveCount(1);
  await page.getByRole("button", { name: "Repeat Boxing", exact: true }).click();
  await page.getByRole("button", { name: "Edit details" }).click();
  await choose(page, page, "Activity", "Strength");
  await choose(page, page, "Duration", "45");
  await page.locator("dialog summary").click();
  await page.getByLabel("Training date (optional)").fill("2026-10-10");
  await choose(page, page, "Effort", "Easy");
  await page.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(submissions[0]).toMatchObject({ activity: "strength", duration_minutes: 45, effort: "easy", training_day: "2026-10-10" });
  const source = page.locator(".history-list > li").filter({ hasText: "Fri 09 Oct 2026" });
  await expect(source).toContainText("1 min");
  await expect(source).toContainText("Hard");
  await expect(page.locator(".history-list > li")).toHaveCount(2);
});

test("quick picks support keyboard selection, modal focus containment and close restoration", async ({ page, baseURL }) => {
  await setup(page, baseURL!, false, false, []);
  await page.goto("/plan");
  const trigger = page.getByRole("button", { name: "Log session", exact: true });
  await trigger.focus();
  await page.keyboard.press("Enter");
  const sheet = page.getByRole("dialog");
  await expect(sheet.getByRole("button", { name: "Pads", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  const bag = sheet.getByRole("button", { name: "Bag work", exact: true });
  await expect(bag).toBeFocused();
  await page.keyboard.press("Space");
  await expect(bag).toHaveAttribute("aria-pressed", "true");
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Shift+Tab");
  // The optional per-session sport switch sits on the Activity label line.
  await expect(sheet.getByRole("button", { name: /^Change sport for this session/ })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(sheet.getByRole("button", { name: "Close training log" })).toBeFocused();
  // Native showModal makes the background inert, including programmatic focus.
  await trigger.evaluate(element => element.focus());
  await expect(sheet.getByRole("button", { name: "Close training log" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(sheet).toHaveCount(0);
  await expect(trigger).toBeFocused();
});

test("Change sport is a quiet, optional per-session switch (390px)", async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const submissions = await setup(page, baseURL!, false, false, []);
  await page.goto("/today");
  await page.getByRole("button", { name: "Log session", exact: true }).click();
  const sheet = page.getByRole("dialog");
  const toggle = sheet.getByRole("button", { name: /^Change sport for this session/ });
  await expect(toggle).toHaveText("Boxing · Change");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await toggle.click();
  const menu = sheet.getByRole("group", { name: "Sport for this session" });
  await expect(menu.getByRole("button", { name: "Boxing", exact: true })).toHaveAttribute("aria-pressed", "true");
  expect((await new AxeBuilder({ page }).include("dialog").analyze()).violations).toEqual([]);
  expect(await sheet.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  await page.screenshot({ animations: "disabled", path: test.info().outputPath("change-sport-open.png") });
  await menu.getByRole("button", { name: "MMA", exact: true }).click();
  await expect(menu).toHaveCount(0);
  await expect(toggle).toHaveText("MMA · Change");
  await choose(sheet, page, "Activity", "Grappling");
  await choose(sheet, page, "Duration", "60");
  await choose(sheet, page, "Effort", "Hard");
  await sheet.getByRole("button", { name: "Save Session", exact: true }).click();
  await expect(sheet).toHaveCount(0);
  expect(submissions[0]).toMatchObject({ activity: "other", sport: "mma", session_type: "grappling" });
  await page.getByRole("button", { name: "Log another session", exact: true }).or(page.getByRole("button", { name: "Log session", exact: true })).first().click();
  await expect(page.getByRole("dialog").getByRole("button", { name: /^Change sport for this session/ })).toHaveText("Boxing · Change");
});


for (const width of [390, 1280]) {
  test(`History adds a second session to a previous day (${width}px)`, async ({ page, baseURL }) => {
    await page.setViewportSize({ width, height: 844 });
    const submissions = await setup(page, baseURL!);
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto("/history");
    await page.getByRole("button", { name: "Add Session", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Log session", exact: true });
    await dialog.locator("summary").click();
    await expect(dialog.getByLabel("Training date (optional)")).toHaveValue(prescribed.training_day);
    await choose(dialog, page, "Activity", "Strength");
    await choose(dialog, page, "Duration", "45");
    await choose(dialog, page, "Effort", "Easy");
    await dialog.getByRole("button", { name: "Save Session", exact: true }).click();
    expect(submissions).toHaveLength(1);
    expect(submissions[0]).toMatchObject({ training_day: prescribed.training_day, activity: "strength" });
    const history = page.locator(".history-list");
    await expect(history.locator(".history-session-row")).toHaveCount(2);
    await expect(history).toContainText("Strength & Power");
    await expect(history).toContainText("45 min");
    await page.reload();
    await expect(history.locator(".history-session-row")).toHaveCount(2);
    await expect(history.getByText("Sat 10 Oct 2026", { exact: false })).toHaveCount(2);
    await expect(history).toContainText("Strength & Power");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
    await page.screenshot({ path: `/tmp/history-add-session-${width}.png`, fullPage: true });
  });
}
