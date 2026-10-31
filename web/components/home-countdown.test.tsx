import assert from "node:assert/strict";
import test from "node:test";
import { window } from "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { AppRouterContext } from "next/dist/shared/lib/app-router-context.shared-runtime";
import { PathnameContext } from "next/dist/shared/lib/hooks-client-context.shared-runtime";
import { AppSessionContext } from "./auth-provider";
import { XpContext } from "./xp-provider";
import HomePage from "../app/page";
import messages from "../messages/en.json";
import { createFreshXpProgress } from "@/lib/xp-progress";
import { clearSharedReads } from "@/lib/shared-reads";
import type { MeResponse } from "@/lib/types";

function pendingResponse() {
  let resolve!: (response: Response) => void;
  return { promise: new Promise<Response>((done) => { resolve = done; }), resolve: (body: unknown, status = 200) => resolve(new Response(JSON.stringify(body), { status })) };
}

const today = (id: string) => ({
  active_plan: { id, name: "Autumn camp", fight_date: "2026-11-14", phase: "GPP", status: "ready" },
  today: { training_day: "2026-10-23", recommendation_state: "train_as_planned", decision_tier: "green", next_session: {}, session_scope: "none", completion_status: "not_started" },
  risk_watch: [], open_injuries: [], week_summary: {}, quick_actions: [],
});
const plan = { outputs: { structured_plan: { weeks: [{ phase_label: "GPP", start_date: "2026-10-19", end_date: "2026-11-14", days: [] }] } } };

async function mountHome(latestId: string, latestFightDate?: string) {
  const originalFrame = globalThis.requestAnimationFrame;
  const originalCancel = globalThis.cancelAnimationFrame;
  globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
  globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
  const host = document.createElement("div"); document.body.appendChild(host);
  const root = createRoot(host);
  const me = { profile: { athlete_id: "athlete-1", role: "athlete", starting_path: "plan_first", technical_style: ["boxing"] },
    latest_plan: { plan_id: latestId, status: "ready", fight_date: latestFightDate }, plan_count: 1 } as unknown as MeResponse;
  const router = { push() {}, replace() {}, refresh() {}, back() {}, forward() {}, prefetch() {}, bfcacheId: "fixture" };
  await act(async () => root.render(
    <NextIntlClientProvider locale="en" timeZone="UTC" messages={messages}>
      <AppRouterContext.Provider value={router}><PathnameContext.Provider value="/">
        <AppSessionContext.Provider value={{ isReady: true, isMeHydrated: true, hasTransientMeError: false, session: { access_token: "token" } as never,
          me, previewAppearanceMode() {}, refreshMe: async () => {}, replaceMe() {}, signOut: async () => {} }}>
          <XpContext.Provider value={{ progress: createFreshXpProgress(), isHydrated: true, isRefreshing: false, error: null, feedback: null, refresh: async () => {}, dismissFeedback() {} }}>
            <HomePage />
          </XpContext.Provider>
        </AppSessionContext.Provider>
      </PathnameContext.Provider></AppRouterContext.Provider>
    </NextIntlClientProvider>,
  ));
  return { host, cleanup: async () => {
    await act(async () => root.unmount()); host.remove();
    // Each test is a new session: one test's plan read must not serve the next.
    clearSharedReads();
    if (originalFrame) globalThis.requestAnimationFrame = originalFrame; else Reflect.deleteProperty(globalThis, "requestAnimationFrame");
    if (originalCancel) globalThis.cancelAnimationFrame = originalCancel; else Reflect.deleteProperty(globalThis, "cancelAnimationFrame");
  } };
}

test("countdown prefetch starts before Today resolves; days stay visible while the calendar loads", async () => {
  const originalFetch = globalThis.fetch;
  const command = pendingResponse(), calendar = pendingResponse();
  const calls: string[] = [];
  globalThis.fetch = (async (input) => {
    const url = String(input); calls.push(url);
    return url.includes("/api/today") ? command.promise : calendar.promise;
  }) as typeof fetch;
  const ui = await mountHome("active", "2026-11-14");
  try {
    assert.ok(ui.host.querySelector(".overview-primary-session .fight-countdown-skeleton"), "show countdown-shaped loading card for a scheduled fight");
    assert.equal(ui.host.querySelector(".overview-primary-session .xp-progress-card-skeleton"), null);
    assert.equal(calls.filter(url => url.includes("/api/plans/active")).length, 1, "warm the calendar without waiting for Today");
    await act(async () => command.resolve(today("active")));
    const number = () => ui.host.querySelector(".fight-countdown-number")?.textContent;
    assert.equal(number(), "22");
    assert.ok(ui.host.querySelector(".overview-primary-session .fight-countdown"), "countdown leads the Home hero");
    assert.equal(ui.host.querySelector(".overview-primary-session .xp-progress-card"), null);
    assert.ok(ui.host.querySelector(".overview-secondary .xp-progress-card"), "compact XP moves into Camp Progress");
    assert.equal(ui.host.querySelector(".overview-secondary .fight-countdown"), null);
    assert.equal(ui.host.querySelector(".fight-countdown-skeleton"), null);
    assert.equal(calls.filter(url => url.includes("/api/plans/active")).length, 1, "reuse the in-flight request");
    await act(async () => calendar.resolve(plan));
    assert.equal(number(), "22", "late calendar must not replay the count from camp day one");
    assert.equal(ui.host.querySelector(".fight-countdown")?.hasAttribute("data-pending"), false);
    assert.equal(ui.host.querySelector('[role="progressbar"][aria-label="Camp progress"]')?.getAttribute("aria-valuenow"), "15");
  } finally { await ui.cleanup(); globalThis.fetch = originalFetch; }
});

test("a prefetched latest plan never supplies a different active plan's calendar", async () => {
  const originalFetch = globalThis.fetch;
  const command = pendingResponse(), active = pendingResponse();
  const calls: string[] = [];
  globalThis.fetch = (async (input) => {
    const url = String(input); calls.push(url);
    if (url.includes("/api/today")) return command.promise;
    if (url.includes("/api/plans/latest")) return new Response(JSON.stringify({ outputs: { structured_plan: { weeks: [{ start_date: "2000-01-01", end_date: "2026-11-14" }] } } }));
    return active.promise;
  }) as typeof fetch;
  const ui = await mountHome("latest");
  try {
    await act(async () => command.resolve(today("active")));
    assert.equal(ui.host.querySelector(".fight-countdown-number")?.textContent, "22");
    assert.equal(ui.host.querySelector('[role="progressbar"][aria-label="Camp progress"]'), null);
    assert.equal(calls.filter(url => url.includes("/api/plans/active")).length, 1);
    await act(async () => active.resolve(plan));
    assert.equal(ui.host.querySelector('[role="progressbar"][aria-label="Camp progress"]')?.getAttribute("aria-valuenow"), "15");
  } finally { await ui.cleanup(); globalThis.fetch = originalFetch; }
});

test("without a fight date, XP remains visible in the Home hero", async () => {
  const originalFetch = globalThis.fetch;
  const command = pendingResponse(), calendar = pendingResponse();
  globalThis.fetch = (async (input) =>
    String(input).includes("/api/today") ? command.promise : calendar.promise
  ) as typeof fetch;
  const ui = await mountHome("active");
  try {
    assert.ok(ui.host.querySelector(".overview-primary-session .xp-progress-card-skeleton"), "open plans load with the compact XP skeleton");
    await act(async () => command.resolve({
      ...today("active"),
      active_plan: { ...today("active").active_plan, fight_date: null },
    }));
    assert.ok(ui.host.querySelector(".overview-primary-session .xp-progress-card"));
    assert.equal(ui.host.querySelector(".overview-primary-session .fight-countdown"), null);
    assert.equal(ui.host.querySelector(".overview-secondary .xp-progress-card"), null);
  } finally { await ui.cleanup(); globalThis.fetch = originalFetch; }
});

test("after fight night, XP stays in the Home hero", async () => {
  const originalFetch = globalThis.fetch;
  const command = pendingResponse(), calendar = pendingResponse();
  globalThis.fetch = (async (input) =>
    String(input).includes("/api/today") ? command.promise : calendar.promise
  ) as typeof fetch;
  const ui = await mountHome("active");
  try {
    await act(async () => command.resolve({
      ...today("active"),
      active_plan: { ...today("active").active_plan, fight_date: "2026-10-22" },
    }));
    assert.ok(ui.host.querySelector(".overview-primary-session .xp-progress-card"));
    assert.equal(ui.host.querySelector(".overview-primary-session .fight-countdown"), null);
    assert.equal(ui.host.querySelector(".overview-secondary .xp-progress-card"), null);
  } finally { await ui.cleanup(); globalThis.fetch = originalFetch; }
});

test("an unnamed active plan never displays 'No active plan'", async () => {
  const originalFetch = globalThis.fetch;
  const command = pendingResponse(), calendar = pendingResponse();
  globalThis.fetch = (async (input) =>
    String(input).includes("/api/today") ? command.promise : calendar.promise
  ) as typeof fetch;
  const ui = await mountHome("latest");
  try {
    await act(async () => command.resolve({
      ...today("active"),
      active_plan: { ...today("active").active_plan, name: "" },
    }));
    const plan = [...ui.host.querySelectorAll(".overview-operational-item")].find(
      item => item.querySelector(".overview-operational-label")?.textContent === "Plan",
    );
    assert.equal(plan?.querySelector(".overview-operational-value")?.textContent, "Active plan");
  } finally { await ui.cleanup(); globalThis.fetch = originalFetch; }
});
