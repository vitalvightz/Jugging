import test, { afterEach } from "node:test";
import assert from "node:assert/strict";

import "../test-dom";
import { act } from "react";
import { createRoot as createReactRoot, type Root } from "react-dom/client";

import TimerPage from "@/app/timer/page";
import { AuthProvider } from "@/components/auth-provider";
import { TodaySessionPanel } from "@/components/today/today-session-panel";
import { ToastProvider } from "@/components/toast-provider";
import { resolveTrainingDay, toISODate } from "@/lib/camp-map";
import { timerAudio } from "@/lib/session-timer/audio";
import { FREE_FORMAT_MEMORY_KEY } from "@/lib/session-timer/contact";
import type { TodayCommandView } from "@/lib/types";
import {
  CONTACT_RUN_KEY_PREFIX,
  ROUND_TIMER_STORAGE_KEY,
  RoundTimerProvider,
  SESSION_RUN_KEY_PREFIX,
  useRoundTimer,
} from "./round-timer-provider";

const TODAY = toISODate(resolveTrainingDay(new Date()));

// Every root a test mounts is torn down afterwards, even when an assertion
// fails mid-test: a timer left mounted keeps ticking and the run never exits.
const mounted = new Set<Root>();
function createRoot(container: HTMLElement): Root {
  const root = createReactRoot(container);
  const unmount = root.unmount.bind(root);
  root.unmount = () => {
    mounted.delete(root);
    unmount();
  };
  mounted.add(root);
  return root;
}

afterEach(async () => {
  for (const root of [...mounted]) {
    await act(async () => root.unmount());
  }
  document.body.replaceChildren();
  window.localStorage.clear();
});

/** Every timer on screen, full size or minimised. */
function timersOnScreen(): number {
  return document.querySelectorAll(".st-root, .st-mini").length;
}

/** Today with a coach-led contact the athlete can time, cleared to train. */
function contactDay(): TodayCommandView {
  return {
    active_plan: { id: "plan-1", name: "Camp", phase: "SPP" },
    today: {
      training_day: TODAY,
      recommendation_state: "train_as_planned",
      decision_tier: "green",
      warnings: [],
      next_session: {
        session_id: `${TODAY}-flush`,
        title: "Mobility flush",
        calendar_date: TODAY,
        session_relation: "today",
        effective_load: "low",
        coach_led_contact: "Hard sparring (coach-led)",
      },
      session_scope: "today",
      session_label: "Today's session",
      completion_status: "started",
    },
    risk_watch: [],
    open_injuries: [],
    week_summary: {},
    quick_actions: [],
  } as TodayCommandView;
}

function TryRoundTimer() {
  const roundTimer = useRoundTimer();
  return (
    <button type="button" data-testid="try-round" onClick={roundTimer.open}>
      try
    </button>
  );
}

/** The timer screen is loaded on demand (React.lazy): wait for it to mount. */
async function untilTimerMounts() {
  for (let attempt = 0; attempt < 40 && !document.querySelector('.st-root[role="dialog"]'); attempt += 1) {
    await act(async () => new Promise((resolve) => setTimeout(resolve, 25)));
  }
}

function ShownFlag() {
  return <p data-testid="flag">{useRoundTimer().shown ? "shown" : "hidden"}</p>;
}

test("the timer page opens straight into the round timer, which minimises to its mini bar", async () => {
  window.localStorage.clear();
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <RoundTimerProvider>
        <TimerPage />
        <ShownFlag />
      </RoundTimerProvider>,
    );
  });

  await untilTimerMounts();
  // No tap: the timer is up on its setup, ready for Start round 1.
  const flag = () => container.querySelector('[data-testid="flag"]')?.textContent;
  assert.equal(flag(), "shown");
  assert.ok(document.querySelector('.st-root[role="dialog"]'));
  assert.ok([...document.querySelectorAll(".st-primary")].some((b) => b.textContent === "Start round 1"));
  assert.ok([...container.querySelectorAll("button")].some((b) => b.textContent === "Open round timer"));

  const minimise = document.querySelector<HTMLButtonElement>('[aria-label="Minimise timer"]');
  assert.ok(minimise);
  await act(async () => minimise.click());
  assert.equal(document.querySelector(".st-root"), null);
  const mini = document.querySelector<HTMLButtonElement>(".st-mini");
  assert.ok(mini);
  assert.equal(flag(), "shown");
  // The floating bar reserves room under the page so it can't cover controls.
  assert.equal(document.documentElement.classList.contains("has-timer-mini"), true);

  await act(async () => mini.click());
  assert.ok(document.querySelector(".st-root"));
  assert.equal(document.documentElement.classList.contains("has-timer-mini"), false);

  // Closing it leaves the page, which can start it again.
  const close = [...document.querySelectorAll<HTMLButtonElement>(".st-link")].find(
    (b) => b.textContent === "Close timer",
  );
  assert.ok(close);
  await act(async () => close.click());
  assert.equal(flag(), "hidden");
  assert.equal(timersOnScreen(), 0);
  const start = [...container.querySelectorAll("button")].find((b) => b.textContent === "Start round timer");
  assert.ok(start);
  await act(async () => start.click());
  assert.equal(timersOnScreen(), 1);

  await act(async () => root.unmount());
  container.remove();
});

test("the timer page opens on the athlete's last round setup", async () => {
  window.localStorage.clear();
  window.localStorage.setItem(FREE_FORMAT_MEMORY_KEY, JSON.stringify({ rounds: 6, workSec: 120, restSec: 30 }));
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <RoundTimerProvider>
        <TimerPage />
      </RoundTimerProvider>,
    );
  });
  const summary = document.querySelector(".st-plan-summary");
  assert.equal(document.querySelector(".st-board-format")?.textContent, "6 rounds");
  assert.equal(summary?.textContent, "2:00 work · 0:30 rest");

  await act(async () => root.unmount());
  container.remove();
});

/** A button in the timer overlay by its visible text or aria-label. */
function timerButton(label: string): HTMLButtonElement | undefined {
  return [...document.querySelectorAll<HTMLButtonElement>(".st-root button")].find(
    (b) => b.textContent?.trim() === label || b.getAttribute("aria-label") === label,
  );
}

test("Start counts down to round 1, remembers the setup, and gives −10s / +10s", async () => {
  window.localStorage.clear();
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <RoundTimerProvider>
        <TimerPage />
      </RoundTimerProvider>,
    );
  });

  // The Amateur preset, then Start: that setup is what /timer opens on next time.
  await act(async () => timerButton("Amateur 2/1")?.click());
  await act(async () => timerButton("Start round 1")?.click());
  assert.deepEqual(JSON.parse(window.localStorage.getItem(FREE_FORMAT_MEMORY_KEY) ?? "null"), {
    rounds: 3,
    workSec: 120,
    restSec: 60,
  });

  // The default 10-second get-ready countdown leads into round 1.
  assert.match(document.querySelector(".st-phase")?.textContent ?? "", /Get ready/);
  assert.equal(document.querySelector(".st-clock-digits")?.textContent, "0:10");
  assert.equal(document.querySelector(".st-clock-caption")?.textContent, "Round 1 / 3");
  assert.ok(timerButton("Take 10 seconds off"));
  assert.ok(timerButton("Add 10 seconds"));

  await act(async () => timerButton("Start now")?.click());
  assert.match(document.querySelector(".st-phase")?.textContent ?? "", /Work/);
  assert.equal(document.querySelector(".st-clock-digits")?.textContent, "2:00");

  await act(async () => timerButton("Add 10 seconds")?.click());
  assert.equal(document.querySelector(".st-clock-digits")?.textContent, "2:10");
  await act(async () => timerButton("Take 10 seconds off")?.click());
  await act(async () => timerButton("Take 10 seconds off")?.click());
  assert.equal(document.querySelector(".st-clock-digits")?.textContent, "1:50");

  await act(async () => root.unmount());
  container.remove();
});

test("the settings sheet, behind the gear, sets the round warnings and the get-ready countdown", async () => {
  window.localStorage.clear();
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <RoundTimerProvider>
        <TimerPage />
      </RoundTimerProvider>,
    );
  });

  await act(async () => timerButton("Timer settings")?.click());
  const alert = (label: string) => timerButton(label)?.getAttribute("aria-pressed");
  assert.equal(alert("10 seconds left"), "true");
  assert.equal(alert("30 seconds left"), "false");
  assert.equal(alert("Halfway"), "false");
  await act(async () => timerButton("30 seconds left")?.click());
  assert.equal(alert("30 seconds left"), "true");
  await act(async () => timerButton("Off")?.click());
  const saved = JSON.parse(window.localStorage.getItem("unlxck.session-timer.settings") ?? "{}");
  assert.equal(saved.warnThirty, true);
  assert.equal(saved.prepSec, 0);

  // With the countdown off, Start rings round 1 straight away.
  await act(async () => timerButton("Close settings")?.click());
  await act(async () => timerButton("Start round 1")?.click());
  assert.match(document.querySelector(".st-phase")?.textContent ?? "", /Work/);

  await act(async () => root.unmount());
  container.remove();
});

test("taking time off past a warning mark still sounds that warning", async () => {
  window.localStorage.clear();
  window.localStorage.setItem(FREE_FORMAT_MEMORY_KEY, JSON.stringify({ rounds: 3, workSec: 90, restSec: 60 }));
  window.localStorage.setItem(
    "unlxck.session-timer.settings",
    JSON.stringify({ warnTen: true, warnThirty: true, warnHalfway: false, prepSec: 0 }),
  );
  const audio = timerAudio();
  const played: string[] = [];
  const realPlay = audio.play;
  audio.play = (sound) => void played.push(sound);
  try {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
      root.render(
        <RoundTimerProvider>
          <TimerPage />
        </RoundTimerProvider>,
      );
    });
    await act(async () => timerButton("Start round 1")?.click());
    // A tick runs every 100 ms; give one the chance to see each jump.
    const takeTen = async () => {
      await act(async () => timerButton("Take 10 seconds off")?.click());
      await act(async () => new Promise((resolve) => setTimeout(resolve, 150)));
    };
    for (let tap = 0; tap < 5; tap += 1) await takeTen(); // 1:30 → 0:40
    assert.ok(!played.includes("double_beep"));
    await takeTen(); // → 0:30
    assert.equal(played.filter((sound) => sound === "double_beep").length, 1);
    await takeTen(); // → 0:20
    await takeTen(); // → 0:10
    assert.equal(played.filter((sound) => sound === "clapper").length, 1);

    await act(async () => root.unmount());
    container.remove();
  } finally {
    audio.play = realPlay;
  }
});

test("without a provider the round timer reads as closed", async () => {
  const container = document.createElement("div");
  const root = createRoot(container);
  await act(async () => root.render(<ShownFlag />));
  assert.equal(container.textContent, "hidden");
  await act(async () => root.unmount());
});

test("a saved planned run blocks the round timer: it is resumed on Today, and only one timer ever shows", async () => {
  window.localStorage.clear();
  // 1-2. A planned session timer was started on Today, then the athlete left Today.
  window.localStorage.setItem(`${SESSION_RUN_KEY_PREFIX}plan-1:${TODAY}-flush:${TODAY}`, "{}");
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <RoundTimerProvider>
        <TimerPage />
        <TryRoundTimer />
      </RoundTimerProvider>,
    );
  });

  // 3. The timer page points back to the running session instead of starting rounds.
  assert.ok(![...container.querySelectorAll("button")].some((b) => b.textContent === "Start round timer"));
  const resume = container.querySelector<HTMLAnchorElement>('a[href="/today#today-session"]');
  assert.equal(resume?.textContent, "Resume on Today");

  // Any other way in is refused too.
  await act(async () => container.querySelector<HTMLButtonElement>('[data-testid="try-round"]')?.click());
  assert.equal(timersOnScreen(), 0);
  assert.equal(window.localStorage.getItem(ROUND_TIMER_STORAGE_KEY), null);

  await act(async () => root.unmount());
  container.remove();
  window.localStorage.clear();
});

test("returning to Today with a saved contact run shows that one timer, never a round timer beside it", async () => {
  window.localStorage.clear();
  window.localStorage.setItem(`${CONTACT_RUN_KEY_PREFIX}plan-1:${TODAY}`, "{}");
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <AuthProvider>
        <ToastProvider>
          <RoundTimerProvider>
            <TryRoundTimer />
            <TodaySessionPanel state={contactDay()} structuredPlan={null} token="" onRefresh={async () => {}} />
          </RoundTimerProvider>
        </ToastProvider>
      </AuthProvider>,
    );
  });
  assert.equal(timersOnScreen(), 1);
  await act(async () => container.querySelector<HTMLButtonElement>('[data-testid="try-round"]')?.click());
  assert.equal(timersOnScreen(), 1);
  assert.equal(window.localStorage.getItem(ROUND_TIMER_STORAGE_KEY), null);

  await act(async () => root.unmount());
  container.remove();
  window.localStorage.clear();
});

test("a round timer already up keeps the screen: Today's saved run waits instead of stacking", async () => {
  window.localStorage.clear();
  window.localStorage.setItem(ROUND_TIMER_STORAGE_KEY, "{}");
  window.localStorage.setItem(`${CONTACT_RUN_KEY_PREFIX}plan-1:${TODAY}`, "{}");
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <AuthProvider>
        <ToastProvider>
          <RoundTimerProvider>
            <TodaySessionPanel state={contactDay()} structuredPlan={null} token="" onRefresh={async () => {}} />
          </RoundTimerProvider>
        </ToastProvider>
      </AuthProvider>,
    );
  });
  assert.equal(timersOnScreen(), 1);
  assert.match(document.querySelector(".st-mini")?.textContent ?? "", /Rounds/);

  await act(async () => root.unmount());
  container.remove();
  window.localStorage.clear();
});

test("a run saved on an earlier training day never blocks the round timer", async () => {
  window.localStorage.clear();
  window.localStorage.setItem(`${CONTACT_RUN_KEY_PREFIX}plan-1:2000-01-01`, "{}");
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <RoundTimerProvider>
        <TimerPage />
      </RoundTimerProvider>,
    );
  });
  assert.equal(timersOnScreen(), 1);

  await act(async () => root.unmount());
  container.remove();
  window.localStorage.clear();
});

function renderTodayThenTimerPage(state: TodayCommandView) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  const renderToday = () =>
    root.render(
      <AuthProvider>
        <ToastProvider>
          <RoundTimerProvider>
            <TodaySessionPanel state={state} structuredPlan={null} token="" onRefresh={async () => {}} />
          </RoundTimerProvider>
        </ToastProvider>
      </AuthProvider>,
    );
  const renderTimerPage = () =>
    root.render(
      <AuthProvider>
        <ToastProvider>
          <RoundTimerProvider>
            <TimerPage />
          </RoundTimerProvider>
        </ToastProvider>
      </AuthProvider>,
    );
  return { container, root, renderToday, renderTimerPage };
}

test("a run left by a session that changed is dropped on Today, so the round timer is not stuck", async () => {
  window.localStorage.clear();
  // Started under the old session; the plan changed later that day.
  const staleKey = `${SESSION_RUN_KEY_PREFIX}plan-1:${TODAY}-old-session:${TODAY}`;
  window.localStorage.setItem(staleKey, "{}");
  const view = renderTodayThenTimerPage(contactDay());

  // /timer first points back to Today...
  await act(async () => view.renderTimerPage());
  assert.ok(view.container.querySelector('a[href="/today#today-session"]'));

  // ...Today has nothing to resume for that key and drops it...
  await act(async () => view.renderToday());
  assert.equal(window.localStorage.getItem(staleKey), null);

  // ...so the round timer starts, and it is the only timer.
  await act(async () => view.root.unmount());
  view.container.remove();
  const again = renderTodayThenTimerPage(contactDay());
  await act(async () => again.renderTimerPage());
  assert.equal(timersOnScreen(), 1);

  await act(async () => again.root.unmount());
  again.container.remove();
  window.localStorage.clear();
});

test("Today keeps the run it can resume and drops the current session's run once it is logged", async () => {
  window.localStorage.clear();
  const sessionKey = `${SESSION_RUN_KEY_PREFIX}plan-1:${TODAY}-flush:${TODAY}`;
  const contactKey = `${CONTACT_RUN_KEY_PREFIX}plan-1:${TODAY}`;
  const otherDayKey = `${SESSION_RUN_KEY_PREFIX}plan-1:old:2000-01-01`;
  window.localStorage.setItem(sessionKey, "{}");
  window.localStorage.setItem(contactKey, "{}");
  window.localStorage.setItem(otherDayKey, "{}");

  // Started session: both runs are resumable and stay.
  const started = renderTodayThenTimerPage(contactDay());
  await act(async () => started.renderToday());
  assert.ok(window.localStorage.getItem(sessionKey));
  assert.ok(window.localStorage.getItem(contactKey));
  await act(async () => started.root.unmount());
  started.container.remove();

  // Logged on another device: the session run can no longer be resumed.
  const loggedState = contactDay();
  loggedState.today.completion_status = "done";
  const logged = renderTodayThenTimerPage(loggedState);
  await act(async () => logged.renderToday());
  assert.equal(window.localStorage.getItem(sessionKey), null);
  assert.ok(window.localStorage.getItem(contactKey));
  // Other days are not Today's to prune (and never block the round timer).
  assert.ok(window.localStorage.getItem(otherDayKey));

  await act(async () => logged.root.unmount());
  logged.container.remove();
  window.localStorage.clear();
});

test("a plan that failed to load leaves the contact run alone instead of guessing", async () => {
  window.localStorage.clear();
  const contactKey = `${CONTACT_RUN_KEY_PREFIX}plan-1:${TODAY}`;
  window.localStorage.setItem(contactKey, "{}");
  // No contact in the command view and no structured plan: unknown, so kept.
  const state = contactDay();
  state.today.next_session = { ...state.today.next_session, coach_led_contact: undefined };
  const view = renderTodayThenTimerPage(state);
  await act(async () => view.renderToday());
  assert.ok(window.localStorage.getItem(contactKey));

  await act(async () => view.root.unmount());
  view.container.remove();
  window.localStorage.clear();
});
