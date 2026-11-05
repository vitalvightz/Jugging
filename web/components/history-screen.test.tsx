import assert from "node:assert/strict";
import test, { beforeEach } from "node:test";
import "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { AppSessionContext } from "./auth-provider";
import { HistoryScreen } from "./history-screen";
import { athleteTrainingDayISO } from "@/lib/camp-map";
import { clearSharedReads } from "@/lib/shared-reads";

// History remembers its last page per session; each test is a fresh session.
beforeEach(() => clearSharedReads());

test("legacy completion records stay visible without claiming independent workouts in the summary", async () => {
  const originalFetch = globalThis.fetch;
  const training_day = athleteTrainingDayISO(new Date(), undefined);
  globalThis.fetch = async () => new Response(JSON.stringify({ sessions: [
    { id: "reported", source: "unlxck", completion_origin: "reported", title: "Reported workout", status: "done", training_day },
    { id: "legacy", source: "unlxck", completion_origin: null, title: "Legacy record", status: "done", training_day },
  ] }), { status: 200 });
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<AppSessionContext.Provider value={{
      session: { access_token: "token" }, me: null, isReady: true, isMeHydrated: true,
      hasTransientMeError: false, refreshMe: async () => {}, signOut: async () => {},
      replaceMe: () => {}, previewAppearanceMode: () => {},
    }}><HistoryScreen /></AppSessionContext.Provider>));
    assert.match(container.textContent ?? "", /Legacy record/);
    assert.match(container.textContent ?? "", /workout identity unverified/);
    const done = [...container.querySelectorAll("dt")].find(item => item.textContent === "Done");
    assert.equal(done?.nextElementSibling?.textContent, "1");
  } finally {
    act(() => root.unmount());
    container.remove();
    globalThis.fetch = originalFetch;
  }
});

test("failed History tabs can be retried without losing the Sessions cache", async () => {
  process.env.NEXT_PUBLIC_API_DEBUG = "false";
  const originalFetch = globalThis.fetch;
  const requests: string[] = [];
  let fail = true;
  globalThis.fetch = async (input, init) => {
    const url = String(input);
    requests.push(url);
    assert.equal(new Headers(init?.headers).get("authorization"), "Bearer token");
    const isSessions = url.includes("training/history");
    const failed = !isSessions && fail;
    const body = failed ? { detail: "invalid authentication token" }
      : isSessions ? { sessions: [] } : url.includes("sparring-logs") ? { logs: [] } : url.includes("history/exercise-library") ? { exercises: [], as_of: "2026-10-19T12:00:00Z" } : [];
    return new Response(JSON.stringify(body), {
      status: failed ? 401 : 200,
      headers: { "content-type": "application/json" },
    });
  };
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  const click = async (label: string) => {
    const button = Array.from(container.querySelectorAll("button")).find((item) => item.textContent === label);
    assert.ok(button, `Missing ${label} button`);
    await act(async () => button.click());
  };
  try {
    await act(async () => root.render(
      <AppSessionContext.Provider value={{
        session: { access_token: "token" }, me: null, isReady: true, isMeHydrated: true,
        hasTransientMeError: false, refreshMe: async () => {}, signOut: async () => {},
        replaceMe: () => {}, previewAppearanceMode: () => {},
      }}>
        <HistoryScreen />
      </AppSessionContext.Provider>,
    ));
    assert.match(container.textContent ?? "", /No sessions logged yet/);
    const initialSessionRequests = requests.filter((url) => url.includes("training/history")).length;
    for (const tab of ["Exercises", "Sparring", "Check-ins", "Injuries"]) {
      fail = true;
      await click(tab);
      assert.match(container.querySelector('[role="alert"]')?.textContent ?? "", /invalid authentication token/);
      fail = false;
      await click("Retry");
      assert.equal(container.querySelector('[role="alert"]'), null);
      assert.ok(container.querySelector('[role="status"]'));
    }
    await click("Sessions");
    assert.equal(requests.filter((url) => url.includes("training/history")).length, initialSessionRequests);
    assert.ok(requests.includes("/api/injury-flags?include_resolved=true"));
  } finally {
    act(() => root.unmount());
    container.remove();
    globalThis.fetch = originalFetch;
  }
});


test("switching accounts drops history before the next account finishes loading", async () => {
  const originalFetch = globalThis.fetch;
  let finishSecond: ((response: Response) => void) | undefined;
  globalThis.fetch = async (_input, init) => {
    if (new Headers(init?.headers).get("authorization") === "Bearer first") {
      return new Response(JSON.stringify({ sessions: [{ id: "old", source: "unlxck", title: "First account session", status: "done", training_day: "2026-10-07" }] }), { status: 200 });
    }
    return new Promise<Response>((resolve) => { finishSecond = resolve; });
  };
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  const render = (token: string) => root.render(<AppSessionContext.Provider value={{
    session: { access_token: token }, me: null, isReady: true, isMeHydrated: true,
    hasTransientMeError: false, refreshMe: async () => {}, signOut: async () => {},
    replaceMe: () => {}, previewAppearanceMode: () => {},
  }}><HistoryScreen /></AppSessionContext.Provider>);
  try {
    await act(async () => render("first"));
    assert.match(container.textContent ?? "", /First account session/);
    await act(async () => render("second"));
    assert.equal(container.textContent?.includes("First account session"), false);
    await act(async () => finishSecond?.(new Response('{"sessions":[]}', { status: 200 })));
    assert.match(container.textContent ?? "", /No sessions logged yet/);
  } finally {
    act(() => root.unmount());
    container.remove();
    globalThis.fetch = originalFetch;
  }
});

test("Sessions shows the training summary, takeaways, linked sparring reports and pages back", async () => {
  const originalFetch = globalThis.fetch;
  const today = athleteTrainingDayISO(new Date(), undefined);
  const requests: string[] = [];
  globalThis.fetch = async (input) => {
    const url = String(input);
    requests.push(url);
    const body = url.includes("before_day=2026-01-10")
      ? { sessions: [{ id: "old", source: "external", title: "Roadwork", activity: "roadwork", duration_minutes: 40,
          effort: "easy", status: "done", training_day: "2026-01-09", plan_id: null }], next_before_day: null }
      : url.includes("training/history")
        ? { sessions: [
            { id: "box", source: "external", title: "Boxing", activity: "boxing", duration_minutes: 60, effort: "hard",
              status: "done", training_day: today, plan_id: null, takeaway: "Right hand up after the jab" },
            { id: "spar", source: "external", title: "Sparring", activity: "sparring", duration_minutes: 45, effort: "hard",
              status: "done", training_day: today, plan_id: null },
            { id: "jan", source: "external", title: "Boxing", activity: "boxing", duration_minutes: 30, effort: "easy",
              status: "done", training_day: "2026-01-10", plan_id: null },
          ], next_before_day: "2026-01-10" }
        : { logs: [{ id: "log-1", athlete_id: "a", plan_id: null, session_id: null, training_day: today, source: "free",
            external_session_id: "spar", planned_intensity: null, intensity: "hard", rounds_completed: 6, round_seconds: null,
            head_contact: "light", rocked: false, notes: "", created_at: "", detail_revision: 1 }],
          current_training_day: today,
          last_7_days: { days: 7, sessions: 1, rounds: 6, hard_rounds: 6, hard_days: 1, heavy_head_contact_sessions: 0, rocked_count: 0 },
          last_28_days: { days: 28, sessions: 1, rounds: 6, hard_rounds: 6, hard_days: 1, heavy_head_contact_sessions: 0, rocked_count: 0 },
          last_hard_day: today, last_rocked_day: null };
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  };
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  const button = (text: string) => Array.from(container.querySelectorAll("button")).find(item => item.textContent?.trim() === text);
  try {
    await act(async () => root.render(<AppSessionContext.Provider value={{
      session: { access_token: "token" }, me: null, isReady: true, isMeHydrated: true,
      hasTransientMeError: false, refreshMe: async () => {}, signOut: async () => {},
      replaceMe: () => {}, previewAppearanceMode: () => {},
    }}><HistoryScreen /></AppSessionContext.Provider>));
    const summary = container.querySelector('[aria-label="Training summary"]');
    assert.ok(summary);
    const stat = (label: string) => [...summary.querySelectorAll("dt")].find(item => item.textContent === label)?.nextElementSibling?.textContent;
    assert.equal(stat("Sessions"), "2");
    assert.equal(stat("Training"), "1h 45m");
    assert.match(summary.textContent ?? "", /Boxing.*60m/);
    assert.match(summary.textContent ?? "", /Sparring.*45m/);

    assert.match(container.textContent ?? "", /Right hand up after the jab/);
    assert.ok(button("Edit takeaway"));
    // The manual sparring session shows its linked report, loaded alongside.
    assert.ok(requests.some(url => url.includes("sparring-logs")));
    assert.match(container.textContent ?? "", /6 rounds · Light head contact/);
    assert.ok(button("Update report"));

    await act(async () => button("Takeaways · 1")!.click());
    assert.equal(container.querySelectorAll(".history-row").length, 1);
    await act(async () => button("Takeaways · 1")!.click());

    await act(async () => button("Load earlier sessions")!.click());
    assert.ok(requests.some(url => url.includes("before_day=2026-01-10")));
    assert.match(container.textContent ?? "", /Running/);
    assert.equal(button("Load earlier sessions"), undefined);
  } finally {
    act(() => root.unmount());
    container.remove();
    globalThis.fetch = originalFetch;
  }
});


function shiftDay(day: string, offset: number): string {
  return new Date(Date.parse(`${day}T12:00:00Z`) + offset * 86_400_000).toISOString().slice(0, 10);
}

async function renderSessions(fetcher: typeof fetch) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = fetcher;
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => root.render(<AppSessionContext.Provider value={{
    session: { access_token: "token" }, me: null, isReady: true, isMeHydrated: true,
    hasTransientMeError: false, refreshMe: async () => {}, signOut: async () => {},
    replaceMe: () => {}, previewAppearanceMode: () => {},
  }}><HistoryScreen /></AppSessionContext.Provider>));
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
  return {
    container,
    cleanup: () => { act(() => root.unmount()); container.remove(); globalThis.fetch = originalFetch; },
  };
}

test("the summary loads every page in its window before showing totals", async () => {
  const today = athleteTrainingDayISO(new Date(), undefined);
  const session = (id: string, day: string, minutes: number) => ({ id, source: "external", title: "Boxing", activity: "boxing",
    duration_minutes: minutes, effort: "hard", status: "done", training_day: day, plan_id: null });
  const requests: string[] = [];
  const ui = await renderSessions((async (input: unknown) => {
    const url = String(input);
    requests.push(url);
    const body = url.includes(`before_day=${shiftDay(today, -1)}`)
      ? { sessions: [session("b", shiftDay(today, -3), 30)], next_before_day: null }
      : { sessions: [session("a", today, 60), session("c", shiftDay(today, -1), 45)], next_before_day: shiftDay(today, -1) };
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch);
  try {
    const summary = ui.container.querySelector('[aria-label="Training summary"]')!;
    const stat = (label: string) => [...summary.querySelectorAll("dt")].find(item => item.textContent === label)?.nextElementSibling?.textContent;
    assert.ok(requests.some(url => url.includes(`before_day=${shiftDay(today, -1)}`)));
    assert.equal(stat("Sessions"), "3");
    assert.equal(stat("Training"), "2h 15m");
  } finally { ui.cleanup(); }
});

test("a window that cannot be completed shows no totals rather than an undercount", async () => {
  const today = athleteTrainingDayISO(new Date(), undefined);
  const ui = await renderSessions((async (input: unknown) => {
    const url = String(input);
    if (url.includes("before_day=")) {
      return new Response(JSON.stringify({ detail: "Service unavailable" }), { status: 503, headers: { "content-type": "application/json" } });
    }
    return new Response(JSON.stringify({ sessions: [{ id: "a", source: "external", title: "Boxing", activity: "boxing",
      duration_minutes: 60, effort: "hard", status: "done", training_day: today, plan_id: null }], next_before_day: today }),
    { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch);
  try {
    const summary = ui.container.querySelector('[aria-label="Training summary"]')!;
    assert.equal(summary.querySelector("dl"), null);
    assert.match(summary.textContent ?? "", /totals aren.t shown/);
    assert.ok([...summary.querySelectorAll("button")].some(button => button.textContent === "Retry"));
  } finally { ui.cleanup(); }
});
