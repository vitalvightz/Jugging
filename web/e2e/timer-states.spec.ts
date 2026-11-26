import { expect, test, type Page } from "@playwright/test";
import fixtures from "./fixtures/rehab-today.json";
import { isolateFromNetwork } from "./support";
import { buildTimerItems, type TimerItem } from "../lib/session-timer/plan";
import {
  completeSet,
  createTimerState,
  endRound,
  endSession,
  finishItem,
  pause,
  startItem,
  type TimerState,
} from "../lib/session-timer/engine";

/**
 * Every timer in the app is the one SessionTimer, so every state must read as
 * the same gym clock: the standalone round timer and each kind of exercise in
 * a planned session (rounds, strength sets with rest logging, timed holds,
 * tap-to-finish rounds, and dose-less tasks). Each state is seeded as a saved
 * run under a frozen clock, then checked at phone and desktop widths.
 * TIMER_SHOTS=<dir> also saves a screenshot of each.
 */
const shots = process.env.TIMER_SHOTS;
const NOW = Date.parse("2026-10-05T12:00:00Z");
const VIEWPORTS = [
  { width: 320, height: 640 },
  { width: 390, height: 844 },
  { width: 1280, height: 800 },
] as const;

const SESSION = {
  session_id: "fight-pace",
  title: "Fight-pace day",
  session_type: "conditioning",
  blocks: [
    { block_id: "spar", block_type: "sparring", display_name: "Hard sparring", rounds: 3,
      duration: { value: 3, unit: "minutes" }, rest: { value: 60, unit: "seconds" } },
    { block_id: "squat", exercise_key: "back-squat", block_type: "strength", display_name: "Back Squat",
      sets: 4, reps: 5, rest: { value: 90, unit: "seconds" }, load: { display: "100 kg" }, effort: { method: "RPE", value: 7 } },
    { block_id: "plank", block_type: "rehab", display_name: "Copenhagen plank hold", sets: 2,
      duration: { value: 20, unit: "seconds" }, rest: { value: 30, unit: "seconds" } },
    { block_id: "shuttles", block_type: "conditioning", display_name: "Shuttle sprints", rounds: 6,
      distance: { value: 200, unit: "meters" }, rest: { value: 90, unit: "seconds" }, effort: { method: "RPE", value: 8 } },
    { block_id: "flow", block_type: "mobility_activation", display_name: "Hip flow" },
  ],
};

const FREE_ROUNDS: TimerItem = {
  kind: "interval", id: "free-rounds", title: "Rounds", detail: null, blockType: null,
  rounds: 3, workSec: 180, restSec: 60, sparring: false, needsSetup: false, presets: true,
  formatMemoryKey: "unlxck.session-timer.free-format",
};

const at = (seconds: number) => NOW - seconds * 1000;
const next = (state: TimerState, seconds: number) => finishItem(state, at(seconds)).state;

type Seeded = { name: string; state: TimerState; running: boolean };

function sessionStates(): Seeded[] {
  const items = buildTimerItems([SESSION] as never);
  const fresh = createTimerState(items);
  const sparring = startItem(fresh, at(40), { prepSec: 0 }).state;
  const squatReady = next(startItem(fresh, at(900), { prepSec: 0 }).state, 600);
  const squatLift = startItem(squatReady, at(25)).state;
  const squatRested = completeSet(startItem(squatReady, at(60)).state, at(30)).state;
  // The set just finished asks for its reps and load during the rest.
  const squatRest = { ...squatRested, pendingSetLog: { itemId: squatRested.items[1].id, set: 1 } };
  const plankReady = next(squatReady, 400);
  const plankHold = startItem(plankReady, at(6)).state;
  const plankRest = completeSet(startItem(plankReady, at(28)).state, at(8)).state;
  const shuttles = startItem(next(plankReady, 300), at(14)).state;
  const shuttleRest = completeSet(startItem(next(plankReady, 300), at(50)).state, at(20)).state;
  const flow = startItem(next(next(plankReady, 300), 200), at(95)).state;
  return [
    { name: "session-1-rounds-ready", state: fresh, running: false },
    { name: "session-2-rounds-work", state: sparring, running: true },
    { name: "session-3-rounds-paused", state: pause(sparring, NOW), running: true },
    { name: "session-4-rounds-rest", state: endRound(sparring, at(5)).state, running: true },
    { name: "session-5-rounds-final-10s", state: startItem(fresh, at(173), { prepSec: 0 }).state, running: true },
    { name: "session-6-sets-ready", state: squatReady, running: false },
    { name: "session-7-sets-lift", state: squatLift, running: true },
    { name: "session-8-sets-rest-logging", state: squatRest, running: true },
    { name: "session-9-hold-work", state: plankHold, running: true },
    { name: "session-10-hold-rest", state: plankRest, running: true },
    { name: "session-11-manual-rounds-work", state: shuttles, running: true },
    { name: "session-12-manual-rounds-rest", state: shuttleRest, running: true },
    { name: "session-13-task-elapsed", state: flow, running: true },
  ];
}

function standaloneStates(): Seeded[] {
  const fresh = createTimerState([FREE_ROUNDS]);
  const work = startItem(fresh, at(40), { prepSec: 0 }).state;
  return [
    { name: "standalone-1-ready", state: fresh, running: false },
    { name: "standalone-2-work", state: work, running: true },
    { name: "standalone-3-paused", state: pause(work, NOW), running: true },
    { name: "standalone-4-rest", state: endRound(work, at(5)).state, running: true },
    { name: "standalone-5-final-10s", state: startItem(fresh, at(173), { prepSec: 0 }).state, running: true },
    { name: "standalone-6-done", state: endSession(work, at(1)), running: false },
  ];
}

function savedRun(key: string, state: TimerState) {
  return JSON.stringify({ version: 2, key, itemIds: state.items.map(item => `${item.kind}:${item.id}`), state });
}

/** The clock and every control fit, nothing scrolls sideways, and labels stay on one line. */
async function checkFits(page: Page, running: boolean) {
  const timer = page.locator(".st-root");
  expect(await timer.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  await expect(timer.locator(".st-glow, .st-phase-dot, .st-ring, .st-segments")).toHaveCount(0);
  const primary = timer.locator(".st-primary").first();
  if (await primary.count()) await expect(primary).toBeInViewport({ ratio: 1 });
  if (!running) return;
  await expect(timer.getByRole("timer")).toBeInViewport({ ratio: 1 });
  const header = timer.locator(".st-board-header");
  await expect(header).toBeInViewport({ ratio: 1 });
  // Phase and count share one line: they stay apart, inside the header, and the count is never cut short.
  const fit = await header.evaluate(el => {
    const [phase, count] = [...el.children].map(child => child.getBoundingClientRect());
    const box = el.getBoundingClientRect();
    const caption = el.querySelector(".st-clock-caption");
    return {
      inside: phase.left >= box.left - 1 && (!count || count.right <= box.right + 1),
      apart: !count || phase.right <= count.left,
      oneLine: phase.height <= box.height + 1 && (!count || count.height <= box.height + 1),
      uncut: !caption || caption.scrollWidth <= caption.clientWidth,
    };
  });
  expect(fit).toEqual({ inside: true, apart: true, oneLine: true, uncut: true });
}

async function capture(page: Page, name: string, running: boolean) {
  for (const viewport of VIEWPORTS) {
    await page.setViewportSize(viewport);
    await checkFits(page, running);
    if (shots) {
      await page.waitForTimeout(400);
      await page.screenshot({ path: `${shots}/${name}-${viewport.width}.png` });
    }
  }
}

test.beforeEach(async ({ page, baseURL }) => {
  await isolateFromNetwork(page, baseURL!);
  await page.clock.setFixedTime(new Date(NOW));
  await page.emulateMedia({ reducedMotion: "reduce" });
});

for (const seeded of standaloneStates()) {
  test(`standalone round timer: ${seeded.name}`, async ({ page }) => {
    const key = "unlxck.session-timer.free";
    await page.addInitScript(({ key, run }) => {
      localStorage.setItem("st-settings-hint-seen", "1");
      localStorage.setItem("st-minimize-hint-seen", "1");
      localStorage.setItem("unlxck.session-timer.settings", JSON.stringify({ prepSec: 0, voice: false }));
      localStorage.setItem(key, run);
    }, { key, run: savedRun(key, seeded.state) });
    await page.goto("/timer");
    await expect(page.locator(".st-root[role='dialog']")).toBeVisible();
    await capture(page, seeded.name, seeded.running);
    if (seeded.name.endsWith("rounds-work")) {
      // Minimised, the same run floats as the mini bar over Today.
      await page.getByRole("button", { name: "Minimise timer" }).click();
      for (const viewport of VIEWPORTS) {
        await page.setViewportSize(viewport);
        await expect(page.locator(".st-mini")).toBeInViewport({ ratio: 1 });
        if (shots) await page.screenshot({ path: `${shots}/session-14-minimised-${viewport.width}.png` });
      }
    }
  });
}

for (const seeded of sessionStates()) {
  test(`planned session timer: ${seeded.name}`, async ({ page }) => {
    const { profile } = structuredClone(fixtures.chest);
    const view = structuredClone(fixtures.chest.view);
    const day = "2026-10-05";
    Object.assign(view.active_plan, { phase: "GPP", fight_date: "2027-06-13" });
    Object.assign(view, { open_injuries: [], risk_watch: [],
      live_prescription: { revision: "a".repeat(64), frozen: false, safety_hold: false, changes: [], session: SESSION } });
    Object.assign(view.today, { training_day: day, recommendation_state: "train_as_planned",
      decision_tier: "green", warnings: [], completion_status: "started", session_scope: "today",
      session_label: "Today's session", next_session: { ...SESSION, calendar_date: day, session_relation: "today", effective_load: "high" } });
    const id = view.active_plan.id;
    const runKey = `unlxck.session-timer.run:${id}:${SESSION.session_id}:${day}`;
    const authKey = `sb-${new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://stub.supabase.co").hostname.split(".")[0]}-auth-token`;
    await page.addInitScript(({ authKey, profile, run, runKey }) => {
      localStorage.setItem(authKey, JSON.stringify({ access_token: "fixture", refresh_token: "fixture", token_type: "bearer",
        expires_at: Date.now() / 1000 + 86400, user: { id: profile.profile.athlete_id, email: "fixture@example.com",
          aud: "authenticated", role: "authenticated", app_metadata: {}, user_metadata: {} } }));
      localStorage.setItem("st-settings-hint-seen", "1");
      localStorage.setItem("st-minimize-hint-seen", "1");
      localStorage.setItem("st-adjust-hint-seen", "1");
      localStorage.setItem(runKey, run);
    }, { authKey, profile, run: savedRun(runKey, seeded.state), runKey });
    await page.route("**/api/**", async route => {
      const path = new URL(route.request().url()).pathname;
      let body: unknown = {};
      if (path === "/api/me") body = profile;
      if (path === "/api/today") body = view;
      if (path === `/api/plans/${id}`) body = { plan_id: id, status: "ready", outputs: { structured_plan: {
        weeks: [{ week_index: 1, days: [{ date: day, sessions: [SESSION] }] }],
      } } };
      if (/session-completions|sparring-logs|pending/.test(path)) body = { completions: [], logs: [], response_sets: [] };
      if (path === "/api/history/exercise-library") body = { exercises: [] };
      if (path === "/api/today/exercise-logs") body = { logs: [], recent_loads: [], recent_performances: [] };
      await route.fulfill({ json: body });
    });
    await page.goto(`/today?plan=${id}`);
    await page.locator(".st-mini").click();
    await expect(page.locator(".st-root[role='dialog']")).toBeVisible();
    await capture(page, seeded.name, seeded.running);
    if (seeded.name.endsWith("rounds-work")) {
      // Minimised, the same run floats as the mini bar over Today.
      await page.getByRole("button", { name: "Minimise timer" }).click();
      for (const viewport of VIEWPORTS) {
        await page.setViewportSize(viewport);
        await expect(page.locator(".st-mini")).toBeInViewport({ ratio: 1 });
        if (shots) await page.screenshot({ path: `${shots}/session-14-minimised-${viewport.width}.png` });
      }
    }
  });
}
