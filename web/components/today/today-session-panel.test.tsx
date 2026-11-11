import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { window as domWindow } from "../test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";

import { ToastProvider } from "@/components/toast-provider";
import { FadeThroughProvider } from "@/components/fade-through";
import { RoundTimerProvider } from "@/components/session-timer/round-timer-provider";
import { timerAudio } from "@/lib/session-timer/audio";
import { AuthProvider, AppSessionContext } from "@/components/auth-provider";
import { previewUnlockCopy, TodaySessionBlocks, TodaySessionPanel } from "./today-session-panel";
import { resolveCurrentDay, resolveTrainingDay, toISODate } from "@/lib/camp-map";
import type { StructuredPlan, TodayCommandView } from "@/lib/types";
import { withHistoricalTimerLoads } from "@/lib/session-timer/history";
import type { ExerciseLogRecord } from "@/lib/types";
import { buildTimerItems } from "@/lib/session-timer/plan";
import { createTimerState, endSession } from "@/lib/session-timer/engine";
import { SHEET_EXIT_MS } from "@/lib/use-sheet-exit";

test("weekday fallback never presents a stale template date as today", () => {
  const plan = {
    schema_version: "text-adapter.v1",
    plan_metadata: { title: "Open plan", plan_type: "open_ongoing_system" },
    weeks: [
      { week_id: "week-1", week_index: 1, days: [] },
      {
        week_id: "week-2",
        week_index: 2,
        days: [
          {
            date: "2026-06-13",
            weekday: "Sat",
            sessions: [{ session_id: "sat-strength", title: "Saturday strength", blocks: [] }],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const current = resolveCurrentDay(plan, new Date(2026, 6, 18), {
    openWeekNumber: 2,
    allowDatedWeekdayMatch: true,
  });
  const html = renderToStaticMarkup(<TodaySessionBlocks current={current} />);

  assert.equal(current.matchType, "weekday");
  assert.equal(current.trainingDayISO, "2026-07-18");
  // Today's session header owns the date; the numbered sections below it must
  // never bring the template row's stale date back.
  assert.match(html, /data-layout="section"/);
  assert.equal(html.includes("Sat 13 Jun 2026"), false);
  assert.equal(html.includes("2026-06-13"), false);
});

test("Today lays a multi-session day out as numbered parts with every exercise in view", () => {
  const plan = {
    weeks: [{
      week_index: 1,
      days: [{
        date: "2026-10-10",
        weekday: "Sat",
        sessions: [
          {
            session_id: "fight-iq",
            title: "Fight Visualisation",
            session_type: "skill",
            objective: "Rehearse recognising the opponent's action before choosing a response.",
            planned_duration: { value: 12, unit: "min" },
            blocks: [
              { block_id: "read-react", block_type: "mindset", display_name: "Read -> React" },
              { block_id: "watch", block_type: "skill", display_name: "Tactical Watch: Southpaw Entries" },
              { block_id: "cars", block_type: "preparation", display_name: "Hip CARs" },
            ],
          },
          {
            session_id: "strength",
            title: "Controlled Strength",
            session_type: "strength_power",
            objective: "Keep basic pulling strength and single-leg control before weekly sparring.",
            blocks: [
              { block_id: "pull-up", block_type: "strength", display_name: "Slow-Lowered Pull-Up", sets: 2, reps: "6" },
              { block_id: "sl-rdl", block_type: "strength", display_name: "Single-Leg RDL Hold", sets: 2, reps: "20 s" },
            ],
          },
        ],
      }],
    }],
  } as StructuredPlan;
  const current = resolveCurrentDay(plan, new Date(2026, 9, 10));
  const html = renderToStaticMarkup(<TodaySessionBlocks current={current} headline="Strength" />);

  assert.equal(html.match(/data-layout="section"/g)?.length, 2);
  assert.match(html, /<span>01<\/span>/);
  assert.match(html, /<span>02<\/span>/);
  assert.match(html, /~12 min/);
  assert.match(html, /2 exercises/);
  // Every exercise is listed without a Show more / Show less step.
  assert.doesNotMatch(html, /Show more|Show less/);
  for (const name of ["Read -&gt; React", "Tactical Watch: Southpaw Entries", "Slow-Lowered Pull-Up", "Single-Leg RDL Hold"]) {
    assert.match(html, new RegExp(name));
  }
  // No demo video: each row shows an icon for what kind of work it is.
  const glyphs = [...html.matchAll(/class="ex-row-placeholder" aria-hidden="true"><svg[^>]*><path d="([^"]+)"/g)]
    .map((match) => match[1]);
  assert.equal(glyphs.length, 5);
  assert.equal(new Set(glyphs.slice(0, 3)).size, 3, "visualisation, tactical watch and mobility differ");
  assert.equal(glyphs[3], glyphs[4], "both strength rows share the strength icon");
  // The rationale waits behind one disclosure per part, closed by default.
  assert.equal(html.match(/<summary>Why this session<\/summary>/g)?.length, 2);
  assert.doesNotMatch(html, /<details class="sp-section-details" open/);
  assert.ok(html.indexOf("Why this session") < html.indexOf("Keep basic pulling strength"));
});

test("today shows no session blocks while the plan's block has not started", () => {
  const plan = {
    schema_version: "text-adapter.v1",
    plan_metadata: { title: "Open plan", plan_type: "open_ongoing_system" },
    weeks: [
      {
        week_id: "week-1",
        week_index: 1,
        days: [
          {
            date: "2026-08-08",
            weekday: "Sat",
            sessions: [{ session_id: "sat-strength", title: "Saturday strength", blocks: [] }],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  // Saturday 1 August, a week before the block's Saturday. Today is not a plan
  // day yet, so no future session may be presented as today's work.
  const current = resolveCurrentDay(plan, new Date(2026, 7, 1), {
    openWeekNumber: 1,
    allowDatedWeekdayMatch: true,
  });
  const html = renderToStaticMarkup(<TodaySessionBlocks current={current} />);

  assert.equal(current.inRange, false);
  assert.equal(current.matchType, null);
  assert.equal(html.includes("Saturday strength"), false);
});

test("Today uses its parent heading and starts video exercises as compact rows", () => {
  const plan = {
    weeks: [{
      week_index: 1,
      days: [{
        date: "2026-09-29",
        weekday: "Tue",
        sessions: [{
          session_id: "strength-1",
          title: "Lower-body strength",
          session_type: "strength_power",
          blocks: [{ block_id: "rdl-db", block_type: "strength", display_name: "Romanian Deadlift (DB)", sets: 3, reps: "8–12" }],
        }],
      }],
    }],
  } as StructuredPlan;
  const current = resolveCurrentDay(plan, new Date(2026, 8, 29));
  const exerciseMedia = {
    "Romanian Deadlift (DB)": { provider: "youtube" as const, video_id: "hQgFixeXdZo", start_s: 27, end_s: 49, source: "curated" as const },
  };
  const html = renderToStaticMarkup(
    <TodaySessionBlocks current={current} headline="Lower-body strength" exerciseMedia={exerciseMedia} />,
  );

  assert.doesNotMatch(html, /class="sp-session-title"/);
  assert.match(html, /Romanian Deadlift \(DB\)/);
  assert.match(html, /aria-expanded="false"/);
  assert.match(html, /class="ex-row-thumb"/);
  assert.doesNotMatch(html, /class="ex-demo"/);

  const alternateHeadline = renderToStaticMarkup(
    <TodaySessionBlocks current={current} headline="Hard sparring" exerciseMedia={exerciseMedia} />,
  );
  assert.match(alternateHeadline, /class="sp-session-title">Lower-body strength/);
});

test("a light-combat session under Today's Technical Combat heading drops its repeated title", () => {
  const plan = {
    weeks: [{
      week_index: 1,
      days: [{
        date: "2026-09-29",
        weekday: "Tue",
        sessions: [{
          session_id: "light-combat-1",
          title: "Light technical combat",
          session_type: "skill",
          blocks: [],
        }],
      }],
    }],
  } as StructuredPlan;
  const current = resolveCurrentDay(plan, new Date(2026, 8, 29));
  const html = renderToStaticMarkup(
    <TodaySessionBlocks current={current} headline="Technical Combat" />,
  );

  assert.doesNotMatch(html, /class="sp-session-title"/);
  assert.doesNotMatch(html, /light (?:technical )?(?:combat|sparring)/i);
  assert.match(html, /Pads, drills, movement or other lower-intensity combat work\./);
});

test("Today blocks omit a coach title supplied by their parent but keep its context", () => {
  const plan = {
    weeks: [{
      week_index: 1,
      days: [{
        date: "2026-09-29",
        weekday: "Tue",
        today_card: { coach_led_contact: "Light Combat / Technical" },
        sessions: [{ session_id: "throw-1", title: "Med-Ball Rotational Throw", blocks: [] }],
      }],
    }],
  } as StructuredPlan;
  const current = resolveCurrentDay(plan, new Date(2026, 8, 29));
  const html = renderToStaticMarkup(
    <TodaySessionBlocks current={current} headline="Technical Combat" />,
  );

  assert.equal((html.match(/Technical Combat/g) ?? []).length, 0);
  assert.equal(html.match(/Med-Ball Rotational Throw/g)?.length, 1);
  assert.match(html, /Pads, drills, movement or other lower-intensity combat work\./);
});

// Session timing is settled by today_service.build_today_command_view and
// carried in session_scope. These two tests pin that the panel follows it in
// both directions rather than re-deciding the day from calendar_date — the
// second-guessing that let Overview and Today describe the same payload
// differently. The backend refuses to scope a future open-plan row to today
// (tests/test_open_plan_recurring_resolution.py) and rejects a completion
// written against one with a 409, so that guard lives there, once.
test("a session the backend scopes to next stays a locked preview", () => {
  const state: TodayCommandView = {
    active_plan: { id: "plan-1", name: "Open plan", phase: "GPP" },
    today: {
      training_day: "2026-08-01",
      recommendation_state: "not_checked_in",
      decision_tier: "not_checked_in",
      warnings: [],
      next_session: {
        session_id: "2026-08-08",
        title: "Fight-Pace Conditioning and Neural Primer",
        calendar_date: "2026-08-08",
        session_relation: "today",
        effective_load: "technical",
      },
      session_scope: "next",
      session_label: "Next session",
      completion_status: "not_started",
    },
    risk_watch: [],
    open_injuries: [],
    week_summary: {},
    quick_actions: [],
  };

  const html = renderToStaticMarkup(
    <ToastProvider>
      <TodaySessionPanel
        state={state}
        structuredPlan={null}
        token="token"
        onRefresh={async () => {}}
      />
    </ToastProvider>,
  );

  assert.match(html, /Next session/);
  assert.match(html, /Sat 08 Aug 2026/);
  assert.match(html, /Available on the day after check-in\./);
  assert.doesNotMatch(html, />Start session<|>Skip session<|>Mark skipped</);
});

test("today's session actions stay locked until check-in is submitted", () => {
  const state: TodayCommandView = {
    active_plan: { id: "plan-1", name: "Camp", phase: "GPP" },
    today: {
      training_day: "2026-08-01",
      recommendation_state: "not_checked_in",
      decision_tier: "not_checked_in",
      warnings: [],
      next_session: {
        session_id: "session-1",
        title: "Turkish Get-Up Skill Flow",
        session_relation: "today",
        effective_load: "technical",
      },
      session_scope: "today",
      session_label: "Today's session",
      completion_status: "not_started",
    },
    risk_watch: [],
    open_injuries: [],
    week_summary: {},
    quick_actions: [],
  };

  const html = renderToStaticMarkup(
    <ToastProvider>
      <TodaySessionPanel
        state={state}
        structuredPlan={null}
        token="token"
        onRefresh={async () => {}}
      />
    </ToastProvider>,
  );

  assert.match(html, /Submit today&#x27;s check-in to unlock session actions\./);
  assert.doesNotMatch(html, />Start session<|>Skip session<|>Mark skipped</);
});

test("the session header carries the day, summary chips and a compact injury strip", () => {
  const state: TodayCommandView = {
    active_plan: { id: "plan-1", name: "Active fight camp", phase: "SPP", fight_date: "2026-11-05" },
    today: {
      training_day: "2026-10-10",
      recommendation_state: "train_as_planned",
      decision_tier: "green",
      warnings: [],
      next_session: {
        session_id: "2026-10-10-strength",
        title: "Strength",
        calendar_date: "2026-10-10",
        d_day: -26,
        estimated_duration: 32,
        session_relation: "today",
      },
      session_scope: "today",
      session_label: "Today's session",
      completion_status: "not_started",
    },
    risk_watch: [],
    open_injuries: [
      { label: "Right achilles tendonitis", body_area: "achilles" },
      { label: "Chest strain", body_area: "chest" },
    ] as TodayCommandView["open_injuries"],
    week_summary: {},
    quick_actions: [],
  };

  const html = renderPanel(state);

  // The year is dropped and the camp countdown left to the page header.
  assert.match(html, /class="today-session-date">Sat 10 Oct</);
  assert.doesNotMatch(html, /today-session-countdown/);
  assert.match(html, /<\/svg><span><span aria-hidden="true">~<\/span><span class="sr-only">About <\/span>32 min<\/span><\/li>/);
  assert.match(html, /href="#today-injury" class="today-injury-strip"/);
  assert.match(html, /Training around Right achilles tendonitis \+ Chest strain/);
  assert.ok(html.indexOf("today-injury-strip") < html.indexOf(">Start session</"));
});

test("a previewed multi-part day is named by its parts and opens on its own preview screen", async () => {
  const plan = {
    weeks: [{
      week_index: 1,
      days: [{
        date: "2026-10-10",
        weekday: "Sat",
        sessions: [
          { session_id: "viz", title: "Fight IQ", session_type: "skill", planned_duration: { value: 12, unit: "min" },
            blocks: [{ block_id: "read-react", block_type: "mindset", display_name: "Read React",
              coaching_cues: ["Identify the clearest signal before a jab.", "Choose the one trigger you trust most."] }] },
          { session_id: "2026-10-10-strength", title: "Controlled strength", session_type: "strength_power",
            planned_duration: { value: 23, unit: "min" },
            blocks: [{ block_id: "pull", block_type: "strength", display_name: "Slow-Lowered Pull-Up", sets: 2, reps: "6",
              purpose: "Pulling strength for clinch control.", coaching_cues: ["Three seconds down."],
              stop_rules: ["Stop: any sharp chest pain."] }] },
        ],
      }],
    }],
  } as StructuredPlan;
  const state: TodayCommandView = {
    active_plan: { id: "plan-1", name: "Fight camp", phase: "GPP", fight_date: "2026-11-05" },
    today: {
      training_day: "2026-10-09",
      recommendation_state: "train_as_planned",
      decision_tier: "green",
      warnings: [],
      next_session: {
        session_id: "2026-10-10-strength",
        title: "Strength",
        calendar_date: "2026-10-10",
        session_relation: "next",
        effective_load: "technical",
      },
      session_scope: "next",
      session_label: "Next session",
      completion_status: "done",
    },
    risk_watch: [],
    open_injuries: [],
    week_summary: {},
    quick_actions: [],
  };
  const container = domWindow.document.createElement("div");
  domWindow.document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <AuthProvider>
        <ToastProvider>
          <TodaySessionPanel state={state} structuredPlan={plan} token="token" onRefresh={async () => {}} />
        </ToastProvider>
      </AuthProvider>,
    );
  });
  try {
    const heading = container.querySelector("#today-session-heading");
    assert.equal(heading?.textContent, "Strength + Skill");
    assert.equal(container.querySelector(".today-session-parts")?.textContent, "Fight IQ · Controlled strength");
    const chips = [...container.querySelectorAll(".today-session-chips li")].map((chip) => chip.textContent);
    assert.deepEqual(chips, ["~About 35 min", "Technical", "2 exercises"]);
    // The parts are not on Today itself: Preview session opens their screen.
    assert.doesNotMatch(container.textContent ?? "", /Slow-Lowered Pull-Up/);
    const preview = [...container.querySelectorAll("button")].find((item) => item.textContent === "Preview session");
    assert.ok(preview);
    assert.equal(preview.getAttribute("aria-haspopup"), "dialog");
    await act(async () => {
      preview.dispatchEvent(new domWindow.MouseEvent("click", { bubbles: true }));
    });
    const screen = domWindow.document.querySelector('[role="dialog"][data-variant="screen"]');
    assert.ok(screen);
    assert.equal(screen.querySelector("h2")?.textContent, "Strength + Skill");
    assert.match(screen.textContent ?? "", /Slow-Lowered Pull-Up/);
    assert.doesNotMatch(container.textContent ?? "", /Slow-Lowered Pull-Up/);

    // An exercise opens its own sheet over the preview: the dose and stop rule
    // in view, every other detail behind its own closed section.
    const row = [...screen.querySelectorAll<HTMLButtonElement>(".ex-row-toggle")]
      .find((item) => item.textContent?.includes("Slow-Lowered Pull-Up"));
    assert.ok(row);
    assert.equal(row.getAttribute("aria-haspopup"), "dialog");
    await act(async () => {
      row.dispatchEvent(new domWindow.MouseEvent("click", { bubbles: true }));
    });
    const dialogs = domWindow.document.querySelectorAll('[role="dialog"]');
    const sheet = dialogs[dialogs.length - 1];
    assert.notEqual(sheet, screen);
    assert.equal(sheet.querySelector("h2")?.textContent, "Slow-Lowered Pull-Up");
    // Rows leave the stop rule to the sheet, which states it as one plain line.
    assert.equal(screen.querySelector(".ex-row-stop"), null);
    assert.equal(sheet.querySelector(".ex-detail-stop")?.textContent, "Stop if any sharp chest pain.");
    const sections = [...sheet.querySelectorAll<HTMLDetailsElement>("details.ex-detail-section")];
    assert.deepEqual(
      sections.map((section) => section.querySelector("summary")?.textContent),
      ["Why it's here", "Full prescription"],
    );
    assert.ok(sections.every((section) => !section.open));

    // Escape closes only the topmost sheet (after it slides away); Back closes the preview.
    await act(async () => {
      domWindow.document.dispatchEvent(new domWindow.KeyboardEvent("keydown", { key: "Escape" }));
    });
    await act(async () => { await new Promise(resolve => setTimeout(resolve, SHEET_EXIT_MS + 20)); });
    assert.equal(domWindow.document.querySelectorAll('[role="dialog"]').length, 1);

    // A banked mindset drill's instructions read as numbered steps, not one
    // "Cue" label per step.
    const drill = [...screen.querySelectorAll<HTMLButtonElement>(".ex-row-toggle")]
      .find((item) => item.textContent?.includes("Read React"));
    assert.ok(drill);
    await act(async () => {
      drill.dispatchEvent(new domWindow.MouseEvent("click", { bubbles: true }));
    });
    const drillSheets = domWindow.document.querySelectorAll('[role="dialog"]');
    const drillSheet = drillSheets[drillSheets.length - 1];
    assert.deepEqual(
      [...drillSheet.querySelectorAll(".ex-detail-steps li")].map((item) => item.textContent),
      ["Identify the clearest signal before a jab.", "Choose the one trigger you trust most."],
    );
    assert.doesNotMatch(drillSheet.textContent ?? "", /Cue/);
    await act(async () => {
      domWindow.document.dispatchEvent(new domWindow.KeyboardEvent("keydown", { key: "Escape" }));
    });
    const back = screen.querySelector<HTMLButtonElement>('button[aria-label="Back to Today"]');
    assert.ok(back);
    await act(async () => {
      back.dispatchEvent(new domWindow.MouseEvent("click", { bubbles: true }));
    });
    assert.equal(domWindow.document.querySelector('[role="dialog"]'), null);
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});

test("a session the backend scopes to today unlocks on that scope alone", () => {
  const state: TodayCommandView = {
    active_plan: { id: "plan-1", name: "Active fight camp", phase: "GPP" },
    today: {
      training_day: "2026-09-13",
      recommendation_state: "train_as_planned",
      decision_tier: "green",
      warnings: [],
      next_session: {
        session_id: "2026-09-13-strength",
        title: "Strength",
        // Deliberately stamped a week out: the scope decides, not this date.
        calendar_date: "2026-09-20",
        session_relation: "next",
        effective_load: "moderate",
      },
      session_scope: "today",
      session_label: "Today's session",
      completion_status: "not_started",
    },
    risk_watch: [],
    open_injuries: [],
    week_summary: {},
    quick_actions: [],
  };

  const html = renderToStaticMarkup(
    <AuthProvider>
      <ToastProvider>
        <TodaySessionPanel
          state={state}
          structuredPlan={null}
          token="token"
          onRefresh={async () => {}}
        />
      </ToastProvider>
    </AuthProvider>,
  );

  assert.match(html, /Today&#x27;s session/);
  assert.match(html, />Start session</);
  assert.match(html, />Skip session</);
  assert.doesNotMatch(html, /Preview only|after check-in\./);
  assert.match(html, /class="today-session-summary"/);
  assert.ok(html.indexOf(">Start session</") < html.indexOf('class="today-session-summary"'));
});

test("safe replacement renders without blocked terminal or completion controls", () => {
  const state: TodayCommandView = {
    active_plan: { id: "plan-1", name: "Camp", phase: "SPP" },
    today: {
      training_day: "2026-08-01",
      recommendation_state: "not_checked_in",
      decision_tier: "stop",
      warnings: [],
      next_session: {
        session_id: "session-1",
        title: "Hard sparring",
        effective_load: "hard",
      },
      session_scope: "today",
      session_label: "Today",
      completion_status: "not_started",
    },
    risk_watch: [],
    open_injuries: [
      {
        id: "injury-1",
        athlete_id: "athlete-1",
        source: "today",
        body_area: "knee",
        description: "Left knee",
        severity: "severe",
        status: "open",
        created_at: "2026-08-01T08:00:00Z",
        updated_at: "2026-08-01T08:00:00Z",
      },
    ],
    week_summary: {},
    quick_actions: [],
  };
  const html = renderToStaticMarkup(
    <ToastProvider>
      <TodaySessionPanel
        state={state}
        structuredPlan={null}
        token="token"
        onRefresh={async () => {}}
      />
    </ToastProvider>,
  );

  assert.match(html, /Rest and recover/i);
  assert.doesNotMatch(html, /Blocked by an active severe injury/);
  assert.doesNotMatch(html, />Start session<|>Resume session<|Log as|>Mark done<|>Mark modified</);
});

function contactDayState(decisionTier: TodayCommandView["today"]["decision_tier"]): TodayCommandView {
  return {
    active_plan: { id: "plan-1", name: "Camp", phase: "SPP" },
    today: {
      training_day: "2026-09-25",
      recommendation_state: decisionTier === "not_checked_in" ? "not_checked_in" : "train_as_planned",
      decision_tier: decisionTier,
      warnings: [],
      next_session: {
        session_id: "2026-09-25-flush",
        title: "Mobility flush",
        calendar_date: "2026-09-25",
        session_relation: "today",
        effective_load: "low",
        coach_led_contact: "Hard sparring (coach-led)",
      },
      session_scope: "today",
      session_label: "Today's session",
      completion_status: "not_started",
    },
    risk_watch: [],
    open_injuries: [],
    week_summary: {},
    quick_actions: [],
  };
}

test("while the next day's parts load, the preview tray holds its place instead of a stand-in summary", () => {
  const state: TodayCommandView = {
    active_plan: { id: "plan-1", name: "Fight camp", phase: "GPP", fight_date: "2026-11-05" },
    today: {
      training_day: "2026-10-09",
      recommendation_state: "train_as_planned",
      decision_tier: "green",
      warnings: [],
      next_session: { session_id: "2026-10-10-strength", title: "Strength", calendar_date: "2026-10-10",
        session_relation: "next", effective_load: "technical" },
      session_scope: "next",
      session_label: "Next session",
      completion_status: "done",
    },
    risk_watch: [], open_injuries: [], week_summary: {}, quick_actions: [],
  };
  const render = (presentationPending: boolean) => renderToStaticMarkup(
    <AuthProvider>
      <ToastProvider>
        <TodaySessionPanel state={state} structuredPlan={null} token="token" onRefresh={async () => {}}
          presentationPending={presentationPending} />
      </ToastProvider>
    </AuthProvider>,
  );

  const loading = render(true);
  assert.match(loading, /<button type="button" class="cta" aria-haspopup="dialog" disabled="">.*?Preview session<\/button>/);
  assert.doesNotMatch(loading, /today-session-summary/);

  // Once the read is done with no parts for that day, the summary is the answer.
  const settled = render(false);
  assert.doesNotMatch(settled, />Preview session</);
  assert.match(settled, /today-session-summary/);
});

function renderPanel(state: TodayCommandView): string {
  return renderToStaticMarkup(
    <AuthProvider>
      <ToastProvider>
        <TodaySessionPanel state={state} structuredPlan={null} token="token" onRefresh={async () => {}} />
      </ToastProvider>
    </AuthProvider>,
  );
}

for (const level of ["rehab_only", "train_no_contact"] as const) {
  for (const status of ["not_started", "started"] as const) {
    test(`zero-load tactical watch ${status} remains actionable under ${level} while sparring stays locked`, () => {
      const state = contactDayState("green");
      state.today.completion_status = status;
      state.today.next_session = { ...state.today.next_session, title: "Tactical Watch" };
      state.effective_clinician_clearance = { level,
        scopes: level === "rehab_only" ? ["rehab"] : ["rehab", "training"], requires_update: false, limited_by: [] };
      state.live_prescription = { revision: "a".repeat(64), frozen: status === "started", safety_hold: false,
        changes: [{ action: "held", reason: "clinician_clearance_ceiling" }],
        session: { session_id: state.today.next_session.session_id!, session_type: "skill", title: "Tactical Watch",
          blocks: [{ block_id: "watch", block_type: "mindset", display_name: "Sparring exchange review" }] } };
      const html = renderPanel(state);
      assert.match(html, /Sparring exchange review/);
      assert.match(html, /Sparring rounds locked by clinician clearance/);
      // Nothing to time: a started session leads with logging it, and there is
      // no separate done / modified pick.
      assert.match(html, status === "not_started" ? />Start session</ : /class="cta"[^>]*>(<svg[\s\S]*?<\/svg>)?Finish session</);
      assert.doesNotMatch(html, /data-value="done"|data-value="modified"|>Resume session</);
      assert.doesNotMatch(html, />Start hard sparring</);
    });
  }
}

for (const tier of ["green", "modify", "pull_back", "stop", "not_checked_in"] as const) {
  test(`frozen contact renders the backend ${tier} gate despite full clinician clearance`, () => {
    const state = contactDayState(tier);
    state.today.completion_status = "started";
    state.today.next_session = { ...state.today.next_session, title: "Hard sparring", coach_led_contact: "Hard sparring" };
    state.effective_clinician_clearance = { level: "train_contact", scopes: ["rehab", "training", "contact"],
      requires_update: false, limited_by: [] };
    state.live_prescription = { revision: "a".repeat(64), frozen: true,
      safety_hold: false, changes: [],
      session: { session_id: state.today.next_session.session_id!, title: "Hard sparring", session_type: "sparring",
        blocks: [{ block_id: "contact", block_type: "sparring", display_name: "Accepted sparring" }] } };
    const html = renderPanel(state);
    if (tier === "green" || tier === "modify") {
      assert.match(html, />Resume session<|Sparring rounds<\/button>/);
      assert.match(html, />Finish session</);
    } else {
      assert.doesNotMatch(html, />Resume session<|>Start session<|>Start hard sparring<|>Finish session<|Sparring rounds<\/button>/);
    }
    assert.doesNotMatch(html, /locked by clinician clearance/);
  });
}

for (const sessionType of ["sparring", "mindset", "rehab"] as const) {
  for (const tier of ["green", "stop"] as const) {
    test(`medical hold locks ${sessionType} under ${tier} despite full contact clearance`, () => {
      const state = contactDayState(tier);
      state.today.completion_status = "started";
      state.today.recommendation_state = "pull_back";
      state.today.recommendation_reason = "No training today.\nYour current injury report includes dizziness.\nStop training and seek medical advice.";
      state.effective_clinician_clearance = { level: "train_contact", scopes: ["rehab", "training", "contact"],
        requires_update: false, limited_by: [] };
      state.live_prescription = { revision: "a".repeat(64), frozen: true, safety_hold: true,
        safety_hold_reason: state.today.recommendation_reason, changes: [],
        session: { session_id: state.today.next_session!.session_id!, title: "Accepted work", session_type: sessionType,
          blocks: [{ block_id: "work", block_type: sessionType, display_name: "Accepted work" }] } };
      const html = renderPanel(state);
      assert.doesNotMatch(html, />Resume session<|>Start session<|>Start hard sparring<|>Finish session<|Sparring rounds<\/button>/);
      assert.doesNotMatch(html, /locked by clinician clearance/);
      assert.match(html, /dizziness|medical advice/);
    });
  }
}

test("a declared sparring day leads with the sparring, with the app work alongside", () => {
  const html = renderPanel(contactDayState("green"));
  // The athlete's gym day owns the headline; the app session is named under it.
  assert.match(html, /<h2 id="today-session-heading">Hard sparring<\/h2>/);
  assert.match(html, /Also today<\/span> Mobility flush/);
  // The sparring rounds are the primary button, the app session its own start.
  assert.match(
    html,
    /today-action-tray[\s\S]*class="cta"[^>]*>Start hard sparring<[\s\S]*class="secondary-button"[^>]*>Start Mobility flush</,
  );
  // The planned rounds cover timing, so no blank timer beside them.
  assert.doesNotMatch(html, /<\/svg>Free timer<\/button>/);
  // The lead button already starts the rounds, so no duplicate shortcut.
  assert.doesNotMatch(html, /<\/svg>Sparring rounds<\/button>/);
  assert.doesNotMatch(html, /completion is unavailable|nothing to log/);
});

test("a sparring-only day is one session: the lead button starts and logs it", () => {
  const state = contactDayState("green");
  state.today.next_session = {
    ...state.today.next_session,
    session_id: "2026-09-25",
    title: "Hard sparring",
    coach_led_contact: "Hard sparring",
  };
  const html = renderPanel(state);
  assert.match(html, /<h2 id="today-session-heading">Hard sparring<\/h2>/);
  assert.match(html, />Start hard sparring</);
  assert.doesNotMatch(html, /Also today|>Start session<|>Start Hard sparring</);
  assert.match(html, />Skip session</);
});

// A declared Light Combat day only tells the app the slot is technical work; it
// never knows how light the gym runs it, so Today never calls it "light".
const LIGHT_WORDING = /light (?:technical )?(?:combat|sparring)/i;

test("a declared Light Combat day reads as Technical Combat, with the app work alongside", () => {
  const state = contactDayState("green");
  state.today.next_session = { ...state.today.next_session, coach_led_contact: "Light Combat / Technical" };
  const html = renderPanel(state);
  assert.match(html, /<h2 id="today-session-heading">Technical Combat<\/h2>/);
  assert.match(html, /Also today<\/span> Mobility flush/);
  assert.match(html, />Start technical combat</);
  assert.doesNotMatch(html, LIGHT_WORDING);
});

test("a Light Combat-only day is one Technical Combat session", () => {
  const state = contactDayState("green");
  state.today.next_session = {
    ...state.today.next_session,
    session_id: "2026-09-25",
    title: "Light Combat / Technical",
    coach_led_contact: "Light Combat / Technical",
  };
  const html = renderPanel(state);
  assert.match(html, /<h2 id="today-session-heading">Technical Combat<\/h2>/);
  assert.match(html, />Start technical combat</);
  assert.doesNotMatch(html, /Also today|>Start session</);
  assert.doesNotMatch(html, LIGHT_WORDING);
});

test("an app session titled as light combat is headlined Technical Combat", () => {
  const state = contactDayState("green");
  state.today.next_session = {
    ...state.today.next_session,
    title: "Light technical combat",
    coach_led_contact: undefined,
  };
  const html = renderPanel(state);
  assert.match(html, /<h2 id="today-session-heading">Technical Combat<\/h2>/);
  assert.doesNotMatch(html, LIGHT_WORDING);
});

test("an idless day falls back to honest copy instead of a dead end", () => {
  const state = contactDayState("green");
  state.today.next_session = { ...state.today.next_session, session_id: undefined, coach_led_contact: undefined };
  const html = renderPanel(state);
  assert.match(html, /This entry has nothing to log/);
  assert.doesNotMatch(html, /completion is unavailable for this entry/);
});

test("sparring rounds stay locked until check-in, and are never offered under a stop", () => {
  const state = contactDayState("not_checked_in");
  state.effective_clinician_clearance = { level: "train_contact", scopes: ["rehab", "training", "contact"],
    requires_update: false, limited_by: [] };
  const unchecked = renderPanel(state);
  assert.doesNotMatch(unchecked, /Sparring rounds<\/button>/);
  assert.match(unchecked, /Sparring rounds unlock after check-in/);
  assert.doesNotMatch(unchecked, /locked by clinician clearance/);
  // With the planned rounds locked, the blank timer is the way to time anything.
  assert.match(unchecked, /<\/svg>Free timer<\/button>/);

  const stopped = renderPanel(contactDayState("stop"));
  assert.doesNotMatch(stopped, /Sparring rounds<\/button>/);
  assert.doesNotMatch(stopped, /Free timer<\/button>/);
});

for (const level of ["rehab_only", "train_no_contact"] as const) {
  for (const tier of ["not_checked_in", "green"] as const) {
    for (const contactOnly of [false, true]) {
      test(`${level} contact lock copy outranks ${tier} for ${contactOnly ? "contact-owned" : "mixed camp"} sessions`, () => {
        const state = contactDayState(tier);
        if (contactOnly) state.today.next_session = { ...state.today.next_session, title: "Hard sparring", coach_led_contact: "Hard sparring" };
        state.effective_clinician_clearance = { level, scopes: level === "rehab_only" ? ["rehab"] : ["rehab", "training"],
          requires_update: false, limited_by: [{ injury_id: "chest", injury_episode_id: "episode", label: "Chest strain" }] };
        const html = renderPanel(state);
        assert.match(html, /Sparring rounds locked by clinician clearance/);
        assert.doesNotMatch(html, /Sparring rounds unlock after check-in|>Start hard sparring<|Sparring rounds<\/button>/);
      });
    }
  }
}

function nextContactDayState(): TodayCommandView {
  const state = contactDayState("green");
  state.today.next_session = {
    session_id: "2026-09-27-breathing",
    title: "Breathing Reset",
    calendar_date: "2026-09-27",
    session_relation: "next",
    effective_load: "low",
    coach_led_contact: "Hard sparring",
  };
  state.today.session_scope = "next";
  state.today.session_label = "Next session";
  return state;
}

test("a next session on a declared sparring day is headlined by the sparring", () => {
  const html = renderPanel(nextContactDayState());
  assert.match(html, /Next session/);
  assert.match(html, /<h2 id="today-session-heading">Hard sparring<\/h2>/);
  assert.match(html, /Also that day<\/span> Breathing Reset/);
  // Preview only: nothing about tomorrow's contact can be started today.
  assert.doesNotMatch(html, />Start hard sparring<|>Start session</);
});

test("completed training previews the next main session instead of leading optional visualisation", () => {
  const state = nextContactDayState();
  state.today.completion_status = "done";
  state.today.next_session = { session_id: "next-strength", title: "Strength", calendar_date: "2026-09-27",
    session_relation: "next", effective_load: "moderate" };
  const structuredPlan = { weeks: [{ week_index: 1, days: [{ date: "2026-09-27", weekday: "Sun", countdown_label: "D-20",
    today_card: { headline: "Strength" }, sessions: [
      { session_id: "optional-visualisation", title: "Fight Visualisation", optional: true,
        blocks: [{ block_type: "mindset", display_name: "Tactical Picture" }] },
      { session_id: "next-strength", title: "Strength", blocks: [{ block_type: "strength", display_name: "Squat", sets: 3, reps: 5 }] },
    ] }] }] } as StructuredPlan;
  const html = renderToStaticMarkup(<ToastProvider><TodaySessionPanel state={state}
    structuredPlan={structuredPlan} token="token" onRefresh={async () => {}} /></ToastProvider>);
  assert.match(html, /Next session/);
  assert.match(html, /<h2 id="today-session-heading">Strength<\/h2>/);
  // The eyebrow drops the year; the optional visualisation never names the day.
  assert.match(html, /class="today-session-date">Sun 27 Sept?</);
  assert.doesNotMatch(html, /class="today-session-parts"/);
  assert.doesNotMatch(html, />Start session<|>Mark done</);
});

test("a next sparring day read from the plan card is headlined by the sparring", () => {
  const state = nextContactDayState();
  state.today.next_session = { ...state.today.next_session, coach_led_contact: undefined };
  const structuredPlan = {
    weeks: [
      {
        week_index: 1,
        days: [
          {
            date: "2026-09-27",
            weekday: "Sun",
            countdown_label: "D-20",
            day_type: "high",
            today_card: { headline: "Breathing Reset", coach_led_contact: "Hard sparring" },
            sessions: [{ session_id: "2026-09-27-breathing", title: "Breathing Reset", blocks: [] }],
          },
        ],
      },
    ],
  } as unknown as StructuredPlan;
  const html = renderToStaticMarkup(
    <AuthProvider>
      <ToastProvider>
        <TodaySessionPanel state={state} structuredPlan={structuredPlan} token="token" onRefresh={async () => {}} />
      </ToastProvider>
    </AuthProvider>,
  );
  assert.match(html, /<h2 id="today-session-heading">Hard sparring<\/h2>/);
  assert.doesNotMatch(html, /Also that day/);
  assert.equal(html.match(/Hard sparring/g)?.length, 1);
  assert.equal(html.match(/Breathing Reset/g)?.length, 1);
});

test("a pull-back day never headlines the sparring it blocks", () => {
  const html = renderPanel(contactDayState("pull_back"));
  assert.doesNotMatch(html, /<h2 id="today-session-heading">Hard sparring<\/h2>/);
  assert.doesNotMatch(html, />Start hard sparring</);
  assert.match(html, /<h2 id="today-session-heading">Mobility flush<\/h2>/);
});

function reviewedSessionState(held: boolean): TodayCommandView {
  return {
    active_plan: { id: "plan-1", name: "Camp", phase: "SPP" },
    today: {
      training_day: "2026-08-01", recommendation_state: "pull_back", decision_tier: "pull_back", warnings: [],
      next_session: { session_id: "rehab-2026-08-01", title: "Today's rehab", session_relation: "today", effective_load: "technical" },
      session_scope: "today", session_label: "Today's session", completion_status: held ? "started" : "not_started",
    },
    live_prescription: {
      revision: "a".repeat(64), safety_hold: held, frozen: held, rehab_only: true, changes: [{ action: "held" }],
      session: { session_id: "rehab-2026-08-01", title: "Today's rehab", session_type: "rehab",
        blocks: [{ block_id: "reviewed-1", block_type: "rehab", display_name: "Reviewed test rehab" }] },
    },
    risk_watch: [], open_injuries: [], week_summary: {}, quick_actions: [],
  };
}

test("reviewed rehab remains available under reduced readiness and uses server blocks", () => {
  const state = reviewedSessionState(false);
  const html = renderToStaticMarkup(<AuthProvider><ToastProvider><TodaySessionPanel state={state} structuredPlan={null} token="token" onRefresh={async () => {}} /></ToastProvider></AuthProvider>);
  assert.match(html, /Reviewed test rehab/);
  assert.match(html, />Completed</);
  assert.doesNotMatch(html, /Do not start this session/);
});

function guidanceDayState(): TodayCommandView {
  const state = reviewedSessionState(false);
  state.today.recommendation_state = "train_as_planned";
  state.today.decision_tier = "green";
  state.live_prescription!.changes = [];
  state.live_prescription!.session.blocks = [{
    block_id: "rehab:chest", block_type: "rehab", display_name: "Chest strain recovery support", is_loading: false,
    coaching_cues: ["Rest from exercise that loads it. Use comfortable everyday movement."],
    drill_snapshot: { rehab_stage: "calm", function: "recovery_downregulation", dose: {} },
  }];
  return state;
}

test("calm-stage guidance is checked off, never started", () => {
  const html = renderToStaticMarkup(<AuthProvider><ToastProvider><TodaySessionPanel state={guidanceDayState()} structuredPlan={null} token="token" onRefresh={async () => {}} /></ToastProvider></AuthProvider>);
  assert.match(html, /Chest strain recovery support/);
  assert.match(html, /Follow the guidance, then check it off/);
  assert.doesNotMatch(html, /Today.s rehab|No rehab exercises/);
  assert.match(html, />Completed</);
  assert.doesNotMatch(html, />Start session<|>Skip session<|How much rehab did you do/);
});

test("checking off guidance saves it as followed and asks how it felt", async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  const original = globalThis.fetch;
  const calls: Array<Record<string, unknown>> = [];
  globalThis.fetch = (async (_input, init) => {
    if (init?.method === "POST") {
      calls.push(JSON.parse(String(init.body)));
      return new Response(JSON.stringify({ completion: { id: "completion-1", plan_id: "plan-1", session_id: "rehab-2026-08-01", training_day: "2026-08-01" },
        rehab_response_prompts: [{ injury_id: "injury-1", injury_episode_id: "episode-1", injury_label: "Chest", body_region: "chest", side: "unknown",
          during_question: "How did it feel while following the guidance?", during_options: ["better", "same", "worse", "not_sure"],
          limit_question: "Did it make you ease off or stop anything?", limit_options: ["no", "reduced", "stopped"], guidance_only: true }] }), { status: 200 });
    }
    return new Response('{"response_sets":[],"history_truncated":false}', { status: 200 });
  }) as typeof fetch;
  try {
    await act(async () => { root.render(<AuthProvider><ToastProvider><TodaySessionPanel state={guidanceDayState()} structuredPlan={null} token="token" onRefresh={async () => {}} /></ToastProvider></AuthProvider>); });
    const button = Array.from(container.querySelectorAll("button")).find(b => b.textContent?.trim() === "Completed");
    assert.ok(button);
    await act(async () => { button.dispatchEvent(new domWindow.MouseEvent("click", { bubbles: true })); });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].status, "done");
    assert.equal(calls[0].rehab_performance, "done_as_shown");
    assert.doesNotMatch(container.textContent ?? "", /How did it feel today\?|while following the guidance/);
    assert.doesNotMatch(container.textContent ?? "", /How did the rehab work go|during the rehab work/);
  } finally { globalThis.fetch = original; act(() => root.unmount()); container.remove(); }
});

test("guidance wording survives Today moving on to the next session", async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  const original = globalThis.fetch;
  // Today now shows the next, exercise-bearing session; the guidance prompt is
  // rehydrated from the server and must keep its own wording.
  const state = reviewedSessionState(false);
  state.today.recommendation_state = "train_as_planned";
  state.today.decision_tier = "green";
  state.live_prescription!.changes = [];
  globalThis.fetch = (async () => new Response(JSON.stringify({ history_truncated: false, response_sets: [{
    completion_id: "completion-1", plan_id: "plan-1", session_id: "rehab-2026-07-31", training_day: "2026-07-31",
    rehab_response_prompts: [{ injury_id: "injury-1", injury_episode_id: "episode-1", injury_label: "CHEST", body_region: "chest", side: "unknown",
      drill_ids: ["chest_strain_recovery_support"], during_question: "How did it feel while following the guidance?",
      during_options: ["better", "same", "worse", "not_sure"], limit_question: "Did it make you ease off or stop anything?",
      limit_options: ["no", "reduced", "stopped"], guidance_only: true }] }] }), { status: 200 })) as typeof fetch;
  try {
    await act(async () => { root.render(<AuthProvider><ToastProvider><TodaySessionPanel state={state} structuredPlan={null} token="token" onRefresh={async () => {}} /></ToastProvider></AuthProvider>); });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    const text = container.textContent ?? "";
    assert.match(text, /Reviewed test rehab/);
    // The prompt is from the day before, so it names that day.
    assert.doesNotMatch(text, /How did it feel on Friday\?|while following the guidance/);
    assert.doesNotMatch(text, /How did the rehab work go|during the rehab work/);
  } finally { globalThis.fetch = original; act(() => root.unmount()); container.remove(); }
});

test("a new safety hold offers stopped logging without resuming frozen work", () => {
  const state = reviewedSessionState(true);
  const html = renderToStaticMarkup(<AuthProvider><ToastProvider><TodaySessionPanel state={state} structuredPlan={null} token="token" onRefresh={async () => {}} /></ToastProvider></AuthProvider>);
  assert.match(html, /Reviewed test rehab/);
  assert.match(html, />Log stopped session</);
  assert.match(html, />Mark skipped</);
  assert.doesNotMatch(html, />Start session<|>Done<|>Resume session<|>Finish session</);
});

for (const [status, performance, label] of [
  ["done", "done_as_shown", "Completed"], ["modified", "changed", "Modified"], ["skipped", undefined, "Skipped"],
] as const) {
  test(`simple rehab ${label} saves without a questionnaire`, async () => {
    const container = document.createElement("div"); document.body.appendChild(container);
    const root = createRoot(container);
    const state = reviewedSessionState(false);
    state.today.completion_status = "started";
    const original = globalThis.fetch;
    const calls: Array<Record<string, unknown>> = [];
    let refreshes = 0;
    globalThis.fetch = (async (_input, init) => {
      if (init?.method === "POST") calls.push(JSON.parse(String(init.body)));
      return new Response(JSON.stringify({ completion: { id: "completion-1" }, rehab_response_prompts: [] }), { status: 200 });
    }) as typeof fetch;
    try {
      await act(async () => { root.render(<AuthProvider><ToastProvider><TodaySessionPanel state={state} structuredPlan={null} token="token" onRefresh={async () => { refreshes++; }} /></ToastProvider></AuthProvider>); });
      assert.equal(container.querySelectorAll('[aria-label="Did you complete your rehab?"] button').length, 3);
      assert.equal(container.querySelectorAll("textarea, select, input").length, 0);
      const button = Array.from(container.querySelectorAll("button")).find(b => b.textContent?.trim() === label);
      assert.ok(button);
      await act(async () => { button.dispatchEvent(new domWindow.MouseEvent("click", { bubbles: true })); });
      assert.equal(calls.length, 1);
      assert.equal(calls[0].rehab_performance, performance);
      assert.equal(calls[0].status, status);
      assert.equal(calls[0].session_rpe, null);
      assert.equal(refreshes, 1);
    } finally { globalThis.fetch = original; act(() => root.unmount()); container.remove(); }
  });
}

test("a tapped rehab answer is marked at once while the save is still in flight", async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  const state = reviewedSessionState(false);
  state.today.completion_status = "started";
  const original = globalThis.fetch;
  let release: (() => void) | null = null;
  globalThis.fetch = (async (_input, init) => {
    if (init?.method === "POST") await new Promise<void>((resolve) => { release = resolve; });
    return new Response(JSON.stringify({ completion: { id: "completion-1" }, rehab_response_prompts: [] }), { status: 200 });
  }) as typeof fetch;
  try {
    await act(async () => { root.render(<AuthProvider><ToastProvider><TodaySessionPanel state={state} structuredPlan={null} token="token" onRefresh={async () => {}} /></ToastProvider></AuthProvider>); });
    const button = () => Array.from(container.querySelectorAll<HTMLButtonElement>('[aria-label="Did you complete your rehab?"] button'))
      .find(b => b.textContent?.trim() === "Modified")!;
    await act(async () => { button().dispatchEvent(new domWindow.MouseEvent("click", { bubbles: true })); });
    // The POST is genuinely in flight: the mock has not answered yet.
    assert.ok(release, "the completion save was requested");
    assert.equal(button().getAttribute("aria-pressed"), "true");
    assert.match(button().className, /today-segment-chosen/);
    assert.match(container.textContent ?? "", /Saving…/);
    await act(async () => { release!(); await new Promise((resolve) => setTimeout(resolve, 0)); });
    assert.match(container.textContent ?? "", /Saved\./);
  } finally { globalThis.fetch = original; act(() => root.unmount()); container.remove(); }
});

test("a rehab answer is dropped when Today moves on to another session", async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  const state = reviewedSessionState(false);
  state.today.completion_status = "started";
  const original = globalThis.fetch;
  globalThis.fetch = (async (_input, init) => init?.method === "POST"
    ? new Promise<Response>(() => {})
    : new Response('{"response_sets":[],"history_truncated":false}', { status: 200 })) as typeof fetch;
  const render = (next: typeof state) => root.render(<AuthProvider><ToastProvider><TodaySessionPanel state={next} structuredPlan={null} token="token" onRefresh={async () => {}} /></ToastProvider></AuthProvider>);
  const chosen = () => container.querySelector(".today-segment-chosen");
  try {
    await act(async () => { render(state); });
    const modified = Array.from(container.querySelectorAll<HTMLButtonElement>('[aria-label="Did you complete your rehab?"] button'))
      .find(b => b.textContent?.trim() === "Modified")!;
    await act(async () => { modified.dispatchEvent(new domWindow.MouseEvent("click", { bubbles: true })); });
    assert.ok(chosen());
    const next = structuredClone(state);
    next.today.next_session = { ...next.today.next_session, session_id: `${next.today.next_session.session_id}-next` };
    await act(async () => { render(next); });
    assert.equal(chosen(), null);
  } finally { globalThis.fetch = original; act(() => root.unmount()); container.remove(); }
});

test("one live rehab session renders every reviewed bundle drill", () => {
  const state = contactDayState("green");
  state.today.next_session = {
    session_id: "rehab-2026-09-25", title: "Today's rehab", session_relation: "today",
  };
  state.live_prescription = {
    revision: "a".repeat(64),
    safety_hold: false, frozen: false, rehab_only: true, changes: [],
    session: {
      session_id: "rehab-2026-09-25", title: "Today's rehab", session_type: "rehab",
      blocks: [
        { block_id: "rehab:injury-1:balance", block_type: "rehab",
          display_name: "Reviewed supported balance", coaching_cues: ["Use a stable counter."] },
        { block_id: "rehab:injury-1:lowering", block_type: "rehab",
          display_name: "Reviewed heel lowering", coaching_cues: ["Lower within comfort."] },
      ],
    },
  };
  const html = renderPanel(state);
  assert.match(html, /Reviewed supported balance/);
  assert.match(html, /Reviewed heel lowering/);
  assert.equal((html.match(/>Completed</g) ?? []).length, 1);
});

test("a planned mobility session typed rehab does not ask whether rehab was completed", () => {
  // The plan types mobility support (Joint Prep) as "rehab". On a rehab
  // recovery day the live prescription is that planned session, not rehab.
  const state = contactDayState("green");
  state.today.next_session = { session_id: "2026-09-25-joint-prep", title: "Joint Prep", session_relation: "today" };
  state.live_prescription = {
    revision: "a".repeat(64), safety_hold: false, frozen: false, rehab_only: false, changes: [],
    session: { session_id: "2026-09-25-joint-prep", title: "Joint Prep", session_type: "rehab",
      blocks: [{ block_id: "joint-prep", block_type: "mobility_activation", display_name: "Joint Prep" }] },
  };
  const html = renderPanel(state);
  assert.doesNotMatch(html, /Did you complete your rehab/);
  assert.doesNotMatch(html, /Did you follow the guidance/);
});

test("contact actions respect live holds and reviewed rehab replacements", () => {
  for (const rehabOnly of [false, true]) {
    const state = contactDayState("green");
    state.live_prescription = {
      revision: "a".repeat(64), frozen: false, safety_hold: !rehabOnly, rehab_only: rehabOnly,
      changes: [{ action: "held", reason: "clinician_clearance_ceiling" }],
      session: { session_id: rehabOnly ? "rehab-2026-09-25" : "2026-09-25-flush", title: rehabOnly ? "Today's rehab" : "Mobility flush",
        session_type: rehabOnly ? "rehab" : "strength", blocks: [] },
    };
    const html = renderPanel(state);
    assert.doesNotMatch(html, />Start hard sparring<|<h2 id="today-session-heading">Hard sparring<|Sparring rounds<\/button>/);
    if (!rehabOnly) assert.match(html, /Sparring rounds locked today/);
    if (rehabOnly) assert.match(html, />Completed</);
  }
});

for (const level of ["rehab_only", "train_no_contact"] as const) {
  test(`${level} effective clearance locks contact even when the original plan permits it`, () => {
    const state = contactDayState("green");
    state.effective_clinician_clearance = { level, scopes: level === "rehab_only" ? ["rehab"] : ["rehab", "training"],
      requires_update: false, limited_by: [{ injury_id: "chest", injury_episode_id: "chest-episode", label: "Chest strain" }] };
    const html = renderPanel(state);
    assert.doesNotMatch(html, />Start hard sparring<|<h2 id="today-session-heading">Hard sparring<|Sparring rounds<\/button>/);
    assert.match(html, /Sparring rounds locked by clinician clearance/);
  });
}

for (const level of ["rehab_only", "train_no_contact", "train_contact"] as const) {
  for (const status of ["not_started", "started"] as const) {
    test(`sparring-only ${status} actions obey ${level} without losing contact identity`, () => {
      const state = contactDayState("green");
      state.today.next_session = { ...state.today.next_session, session_id: "contact-only",
        title: "Hard sparring", coach_led_contact: "Hard sparring" };
      state.today.completion_status = status;
      state.effective_clinician_clearance = { level,
        scopes: level === "rehab_only" ? ["rehab"] : level === "train_no_contact" ? ["rehab", "training"] : ["rehab", "training", "contact"],
        requires_update: false, limited_by: [{ injury_id: "chest", injury_episode_id: "episode", label: "Chest strain" }] };
      const html = renderPanel(state);
      if (level === "train_contact") {
        assert.match(html, status === "not_started" ? />Start hard sparring</ : />Resume session</);
        if (status === "started") assert.match(html, />Finish session</);
        assert.doesNotMatch(html, /Sparring rounds locked by clinician clearance/);
      } else {
        assert.match(html, /Sparring rounds locked by clinician clearance/);
        assert.doesNotMatch(html, />Start session<|>Start hard sparring<|>Resume session<|>Finish session<|<form/);
      }
    });
  }
}

for (const status of ["not_started", "started"] as const) {
  test(`mixed camp ${status} keeps reviewed non-contact actions under train_no_contact`, () => {
    const state = contactDayState("green");
    state.today.completion_status = status;
    state.effective_clinician_clearance = { level: "train_no_contact", scopes: ["rehab", "training"], requires_update: false,
      limited_by: [{ injury_id: "chest", injury_episode_id: "episode", label: "Chest strain" }] };
    state.live_prescription = { revision: "a".repeat(64), frozen: status === "started", safety_hold: false, changes: [],
      session: { session_id: state.today.next_session.session_id!, session_type: "strength", title: "Mobility flush",
        blocks: [{ block_id: "safe", block_type: "strength", display_name: "Reviewed non-contact work" }] } };
    const html = renderPanel(state);
    assert.match(html, /Reviewed non-contact work/);
    assert.match(html, /Sparring rounds locked by clinician clearance/);
    assert.match(html, status === "not_started" ? />Start session</ : />Resume session</);
    if (status === "started") assert.match(html, />Finish session</);
    assert.doesNotMatch(html, />Start hard sparring</);
  });
}

for (const action of ["Finish session", "Skip session"] as const) {
  test(`lowering contact clearance closes an already open ${action} log`, async () => {
    const container = document.createElement("div"); document.body.appendChild(container);
    const root = createRoot(container);
    const state = contactDayState("green");
    state.today.next_session = { ...state.today.next_session, session_id: "contact-only", title: "Hard sparring", coach_led_contact: "Hard sparring" };
    state.today.completion_status = "started";
    state.effective_clinician_clearance = { level: "train_contact", scopes: ["rehab", "training", "contact"], requires_update: false, limited_by: [] };
    const original = globalThis.fetch;
    const writes: unknown[] = [];
    globalThis.fetch = (async (_input, init) => {
      if (init?.method === "POST") writes.push(init.body);
      return new Response('{"response_sets":[],"history_truncated":false}', { status: 200 });
    }) as typeof fetch;
    const render = () => root.render(<AuthProvider><ToastProvider><TodaySessionPanel state={state} structuredPlan={null} token="token" onRefresh={async () => {}} /></ToastProvider></AuthProvider>);
    try {
      await act(async () => { render(); });
      const choice = Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.trim() === action);
      assert.ok(choice);
      await act(async () => { choice.dispatchEvent(new domWindow.MouseEvent("click", { bubbles: true })); });
      // Finish opens the session's review sheet (portalled to the body); Skip the reason form.
      const openLog = () => document.querySelector(".today-review-root") ?? container.querySelector("form");
      assert.ok(openLog());
      state.effective_clinician_clearance = { ...state.effective_clinician_clearance!, level: "train_no_contact", scopes: ["rehab", "training"] };
      await act(async () => { render(); });
      assert.equal(openLog(), null);
      assert.equal(Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.trim() === "Finish session"), undefined);
      assert.match(container.textContent ?? "", /Sparring rounds locked by clinician clearance/);
      assert.equal(writes.length, 0);
    } finally { globalThis.fetch = original; act(() => root.unmount()); container.remove(); }
  });
}

for (const tier of ["green", "not_checked_in", "pull_back", "stop"] as const) {
for (const title of ["Fight Visualisation", "Neural Visualization"]) {
test(`start ${title} under ${tier} opens the guide then hands on to the timer`, async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  const state = contactDayState(tier);
  state.today.next_session = { ...state.today.next_session, coach_led_contact: undefined, title };
  state.live_prescription = { revision: "a".repeat(64), frozen: false, safety_hold: tier !== "green", changes: [],
    session: { session_id: state.today.next_session.session_id!, session_type: "skill", title,
      blocks: [
        { block_id: "vis-1", block_type: "mindset", display_name: "Tactical Picture",
          coaching_cues: ["See the opponent clearly.", "Cue: Take the space."] },
        { block_id: "rdl", block_type: "strength", display_name: "Romanian Deadlift", sets: 3, reps: "8" },
      ] } };
  const original = globalThis.fetch;
  const posts: Array<Record<string, unknown>> = [];
  globalThis.fetch = (async (_input, init) => {
    if (init?.method === "POST") {
      posts.push(JSON.parse(String(init.body)));
      return new Response(JSON.stringify({ completion: { id: "completion-1" }, rehab_response_prompts: [] }), { status: 200 });
    }
    return new Response('{"response_sets":[],"history_truncated":false,"logs":[]}', { status: 200 });
  }) as typeof fetch;
  const button = (scope: ParentNode, label: string) =>
    Array.from(scope.querySelectorAll("button")).find((b) => b.textContent?.trim() === label);
  const render = () => root.render(<AuthProvider><ToastProvider><TodaySessionPanel state={state} structuredPlan={null} token="token" onRefresh={async () => { state.today.completion_status = "started"; render(); }} /></ToastProvider></AuthProvider>);
  try {
    await act(async () => { render(); });
    const start = button(container, "Start session");
    assert.ok(start);
    await act(async () => { start.dispatchEvent(new domWindow.MouseEvent("click", { bubbles: true })); });
    assert.equal(posts[0]?.status, "started");
    const player = document.querySelector<HTMLElement>('[aria-label="Guided Tactical Picture"]');
    assert.ok(player, "the visualisation opens first");
    assert.equal(document.querySelector(".st-root"), null);
    const skip = button(player, "Skip to training");
    assert.ok(skip);
    await act(async () => { skip.dispatchEvent(new domWindow.MouseEvent("click", { bubbles: true })); });
    assert.equal(document.querySelector('[aria-label="Guided Tactical Picture"]'), null);
    assert.ok(document.querySelector(".st-root"), "then the timer takes over");
    await act(async () => { document.querySelector<HTMLButtonElement>('[aria-label="Minimise timer"]')!.click(); });
    await act(async () => { button(container, "Resume session")!.click(); });
    assert.ok(document.querySelector('.st-root[role="dialog"]'), "resume reopens the session timer");
  } finally { globalThis.fetch = original; act(() => root.unmount()); container.remove(); }
});

}
}

test("handing on from the visualisation unlocks timer audio in the tap, not after the fade", async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  const state = contactDayState("green");
  state.today.next_session = { ...state.today.next_session, coach_led_contact: undefined, title: "Fight Visualisation" };
  state.live_prescription = { revision: "a".repeat(64), frozen: false, safety_hold: false, changes: [],
    session: { session_id: state.today.next_session.session_id!, session_type: "skill", title: "Fight Visualisation",
      blocks: [
        { block_id: "vis-1", block_type: "mindset", display_name: "Tactical Picture",
          coaching_cues: ["See the opponent clearly.", "Cue: Take the space."] },
        { block_id: "rdl", block_type: "strength", display_name: "Romanian Deadlift", sets: 3, reps: "8" },
      ] } };
  const original = globalThis.fetch;
  globalThis.fetch = (async (_input, init) => init?.method === "POST"
    ? new Response(JSON.stringify({ completion: { id: "completion-1" }, rehab_response_prompts: [] }), { status: 200 })
    : new Response('{"response_sets":[],"history_truncated":false,"logs":[]}', { status: 200 })) as typeof fetch;
  const audio = timerAudio();
  const originalUnlock = audio.unlock.bind(audio);
  let unlocks = 0;
  audio.unlock = () => { unlocks += 1; originalUnlock(); };
  const button = (scope: ParentNode, label: string) =>
    Array.from(scope.querySelectorAll("button")).find((b) => b.textContent?.trim() === label);
  const wait = (ms: number) => act(async () => { await new Promise((resolve) => setTimeout(resolve, ms)); });
  try {
    await act(async () => { root.render(<AuthProvider><ToastProvider><FadeThroughProvider>
      <TodaySessionPanel state={state} structuredPlan={null} token="token" onRefresh={async () => {}} />
    </FadeThroughProvider></ToastProvider></AuthProvider>); });
    await act(async () => { button(container, "Start session")!.dispatchEvent(new domWindow.MouseEvent("click", { bubbles: true })); });
    await wait(500);
    const player = document.querySelector<HTMLElement>('[aria-label="Guided Tactical Picture"]');
    assert.ok(player, "the visualisation opens first");

    unlocks = 0;
    act(() => { button(player, "Skip to training")!.dispatchEvent(new domWindow.MouseEvent("click", { bubbles: true })); });
    // Within the tap: unlocked, while the screen is still only going dark.
    assert.equal(unlocks, 1);
    assert.equal(document.querySelector(".st-root"), null);
    await wait(500);
    assert.ok(document.querySelector(".st-root"), "the timer takes over after the fade");
    assert.equal(unlocks, 1, "and nothing unlocks again from inside the fade");
  } finally {
    audio.unlock = originalUnlock;
    globalThis.fetch = original; act(() => root.unmount()); container.remove();
  }
});

test("rehab replacement drops the held day's strength and combat coaching", async () => {
  const state = reviewedSessionState(false);
  state.today.training_day = toISODate(resolveTrainingDay(new Date()));
  state.live_prescription!.rehab_only = true;
  state.today.next_session!.calendar_date = state.today.training_day;
  const structuredPlan: StructuredPlan = { weeks: [{ week_id: "w", days: [{
    date: state.today.training_day, today_card: {
      headline: "Strength", coach_led_contact: "Hard sparring", primary_warning: "Push today's strength load",
      mindset_anchor: { focus_cue: "Keep a weekly power exposure" }, nutrition_summary: "Keep your normal nutrition plan",
    }, sessions: [{ session_id: "original", title: "Strength", objective: "Build structural strength" }],
  }] }] };
  const container = document.createElement("div");
  const root = createRoot(container);
  const renderPanel = (view: TodayCommandView) => (
    <AppSessionContext.Provider value={{ isReady: true, isMeHydrated: true, hasTransientMeError: false,
      session: null, me: null, previewAppearanceMode: () => {}, refreshMe: async () => {},
      replaceMe: () => {}, signOut: async () => {} }}>
      <ToastProvider><TodaySessionPanel state={view} structuredPlan={structuredPlan} token="" onRefresh={async () => {}} /></ToastProvider>
    </AppSessionContext.Provider>
  );
  try {
    const original = { ...state, live_prescription: undefined, today: { ...state.today,
      recommendation_state: "train_as_planned" as const, decision_tier: "green" as const,
      next_session: { ...state.today.next_session, session_id: "original", title: "Strength" } } };
    await act(async () => { root.render(renderPanel(original)); });
    // Prove the held day's coaching was present before the live replacement.
    assert.match(container.textContent!, /Build structural strength/);
    assert.match(container.textContent!, /weekly power exposure/);
    await act(async () => { root.render(renderPanel(state)); });
    assert.match(container.textContent!, /Reviewed test rehab/);
    assert.doesNotMatch(container.textContent!, /Hard sparring|weekly power exposure|Push today|Build structural strength/);
  } finally {
    await act(async () => { root.unmount(); });
  }
});

test("finished mixed rehab run survives a failed batch and opens populated review only after retry saves", async () => {
  const state = contactDayState("green");
  state.today.completion_status = "started";
  state.today.next_session = { ...state.today.next_session, coach_led_contact: undefined, title: "Strength and rehab" };
  const blocks = [
    { block_id: "rope", block_type: "power", display_name: "Rope", sets: 3, reps: "8" },
    { block_id: "press", block_type: "strength", display_name: "Press", sets: 3, reps: "8" },
    { block_id: "rdl", block_type: "strength", display_name: "RDL", sets: 3, reps: "8" },
    { block_id: "bike", block_type: "conditioning", display_name: "Bike", rounds: 6, work: {value: 30, unit: "seconds"} },
    { block_id: "rehab", block_type: "rehab", display_name: "Rehab hold", sets: 2, reps: "6" },
  ];
  state.live_prescription = {revision: "a".repeat(64), frozen: false, safety_hold: false, changes: [],
    session: {session_id: state.today.next_session.session_id!, session_type: "strength", title: "Strength and rehab", blocks}};
  const items = buildTimerItems([state.live_prescription.session]);
  const rdl = items.find(item => item.blockId === "rdl")!;
  const run = endSession({...createTimerState(items), completed: [3, 0, 1, 0, 2], index: 4,
    setDrafts: { [rdl.id]: [{ reps: "8", load: "115", rpe: "6" }] },
    confirmedSetLogs: { [rdl.id]: [1] } }, Date.now());
  const key = `unlxck.session-timer.run:plan-1:${state.today.next_session.session_id}:${state.today.training_day}`;
  window.localStorage.setItem(key, JSON.stringify({version: 2, key, itemIds: items.map(item => `${item.kind}:${item.id}`), state: run}));
  const originalFetch = globalThis.fetch;
  const batches: Array<{ entries: Array<{ block_id: string; status: string }>; keep_existing: boolean }> = [];
  const pending: Array<(response: Response) => void> = [];
  const manual = {block_id: "press", status: "as_prescribed", actual: {load: {value: 25, unit: "kg"}}};
  globalThis.fetch = ((input: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      assert.match(String(input), /\/api\/today\/exercise-logs$/);
      batches.push(JSON.parse(String(init.body)));
      return new Promise<Response>(resolve => pending.push(resolve));
    }
    return Promise.resolve(new Response(JSON.stringify({logs: [manual], response_sets: [], history_truncated: false}), {status: 200}));
  }) as typeof fetch;
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  try {
    await act(async () => { root.render(<AuthProvider><ToastProvider><TodaySessionPanel state={state}
      structuredPlan={null} token="token" onRefresh={async () => {}} /></ToastProvider></AuthProvider>);
      await new Promise(resolve => setTimeout(resolve, 20)); });
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
    assert.equal(batches.length, 1);
    assert.deepEqual(batches[0].entries.map(entry => [entry.block_id, entry.status]), [
      ["rope", "as_prescribed"], ["rdl", "modified"], ["bike", "skipped"],
    ]);
    assert.equal(batches[0].keep_existing, true);
    assert.deepEqual((batches[0].entries.find(entry => entry.block_id === "rdl") as unknown as
      { actual: { set_entries: { load: { value: number } }[] } }).actual.set_entries[0].load.value, 115);
    assert.match(container.textContent ?? "", /Save timer results/);
    assert.ok(window.localStorage.getItem(key));
    assert.equal(document.querySelector(".today-review-root"), null);
    await act(async () => { pending[0](new Response(JSON.stringify({detail: "Network write failed"}), {status: 409})); });
    assert.ok(window.localStorage.getItem(key));
    assert.equal(document.querySelector(".today-review-root"), null);
    await act(async () => { document.querySelector<HTMLButtonElement>(".st-mini")!.click(); });
    const retry = Array.from(document.querySelectorAll("button")).find(button => button.textContent === "Retry save")!;
    assert.ok(retry);
    await act(async () => { retry.click(); retry.click(); });
    assert.equal(batches.length, 2);
    assert.ok(window.localStorage.getItem(key));
    await act(async () => { pending[1](new Response(JSON.stringify({logs: batches[1].entries}), {status: 200})); });
    assert.equal(window.localStorage.getItem(key), null);
    const savedRdl = batches[1].entries.find(entry => entry.block_id === "rdl")!;
    const nextExposure = withHistoricalTimerLoads(buildTimerItems([{ blocks }]), blocks, [], [
      { ...savedRdl, plan_id: "older-plan", prescribed: blocks[2] } as unknown as ExerciseLogRecord,
    ]);
    const nextRdl = nextExposure.find(item => item.blockId === "rdl");
    assert.ok(nextRdl?.kind === "sets");
    assert.equal(nextRdl.weightRecommendation?.last?.value, 115);
    // Only one of three prescribed sets was completed: Last stays measured,
    // while the next attempt follows the existing reduction rule.
    assert.equal(nextRdl.suggestedLoads?.[0], "102.5");
    assert.equal(document.querySelector(".st-root"), null);
    assert.match(document.querySelector(".today-review-root")?.textContent ?? "", /4 of 4 exercises logged/);
    assert.match(document.querySelector(".today-review-root")?.textContent ?? "", /Did you complete your rehab/);
  } finally {
    await act(async () => { root.unmount(); });
    container.remove();
    globalThis.fetch = originalFetch;
    window.localStorage.removeItem(key);
  }
});

test("a logged day no longer offers its sparring rounds", () => {
  for (const status of ["done", "modified", "skipped"] as const) {
    const state = contactDayState("green");
    state.today.completion_status = status;
    const html = renderPanel(state);
    assert.doesNotMatch(html, /Sparring rounds<\/button>|>Start hard sparring</);
    // With the day's rounds gone, the blank timer is back.
    assert.match(html, /<\/svg>Free timer<\/button>/);
  }
});


test("current rehab uses its canonical YouTube identity through the S&C player", () => {
  const key = "elbow-tendonitis-supported-hand-weight-wrist-extension";
  const plan = { weeks: [{ week_index: 1, days: [{ date: "2026-09-29", weekday: "Tue", sessions: [{
    session_id: "rehab-current", title: "Rehab", blocks: [{ block_id: "elbow-current", block_type: "rehab",
      display_name: "Supported hand-weight wrist extension", exercise_key: key, sets: 1, reps: 10 }],
  }] }] }] } as StructuredPlan;
  const current = resolveCurrentDay(plan, new Date(2026, 8, 29));
  const media = { provider: "youtube", video_id: "hQgFixeXdZo", start_s: 5, end_s: 20, source: "curated" } as const;
  const html = renderToStaticMarkup(<TodaySessionBlocks current={current} exerciseMedia={{ [`exercise:${key}`]: media }} />);
  assert.match(html, /class="ex-row-thumb"/);
  const wrong = renderToStaticMarkup(<TodaySessionBlocks current={current} exerciseMedia={{ "Supported hand-weight wrist extension": media }} />);
  assert.doesNotMatch(wrong, /class="ex-row-thumb"/);
});

test("Home's Preview session link opens the previewed day's screen once", async () => {
  const plan = {
    weeks: [{
      week_index: 1,
      days: [{
        date: "2026-10-10",
        weekday: "Sat",
        sessions: [{ session_id: "2026-10-10-strength", title: "Controlled strength", session_type: "strength_power",
          blocks: [{ block_id: "pull", block_type: "strength", display_name: "Slow-Lowered Pull-Up", sets: 2, reps: "6" }] }],
      }],
    }],
  } as StructuredPlan;
  const state: TodayCommandView = {
    active_plan: { id: "plan-1", name: "Fight camp", phase: "GPP", fight_date: "2026-11-05" },
    today: {
      training_day: "2026-10-09",
      recommendation_state: "train_as_planned",
      decision_tier: "green",
      warnings: [],
      next_session: { session_id: "2026-10-10-strength", title: "Strength", calendar_date: "2026-10-10", session_relation: "next" },
      session_scope: "next",
      session_label: "Next session",
      completion_status: "done",
    },
    risk_watch: [],
    open_injuries: [],
    week_summary: {},
    quick_actions: [],
  };
  domWindow.history.replaceState(null, "", "/today?preview=1");
  const container = domWindow.document.createElement("div");
  domWindow.document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <AuthProvider>
        <ToastProvider>
          <TodaySessionPanel state={state} structuredPlan={plan} token="token" onRefresh={async () => {}} />
        </ToastProvider>
      </AuthProvider>,
    );
  });
  try {
    const screen = domWindow.document.querySelector('[role="dialog"][data-variant="screen"]');
    assert.ok(screen);
    assert.match(screen.textContent ?? "", /Slow-Lowered Pull-Up/);
    // The param is dropped so a refresh or Back does not reopen it.
    assert.equal(domWindow.location.search, "");
  } finally {
    await act(async () => root.unmount());
    container.remove();
    domWindow.history.replaceState(null, "", "/");
  }
});

const HEAD_IMPACT_NOTICE: NonNullable<TodayCommandView["today"]["primary_safety_notice"]> = {
  code: "head_impact_no_contact", injury_id: "", chip: "CHECK", title: "No more contact today",
  action: "You reported being rocked or dropped. No more contact today, and tell your coach.",
  detail: "Get checked if symptoms appear.", tone: "red",
};

test("a head-impact report locks contact on a green day but keeps the non-contact session", () => {
  const state = contactDayState("green");
  state.effective_clinician_clearance = { level: "train_contact", scopes: ["rehab", "training", "contact"],
    requires_update: false, limited_by: [] };
  state.today.primary_safety_notice = HEAD_IMPACT_NOTICE;
  const html = renderPanel(state);
  assert.doesNotMatch(html, />Start hard sparring<|Sparring rounds<\/button>/);
  assert.match(html, /No more contact today after your head-impact report/);
  assert.doesNotMatch(html, /locked by clinician clearance/);
  // The mobility flush is not contact, so it stays startable.
  assert.match(html, />Start (session|Mobility flush)</);
});

test("a head-impact report blocks a contact-only session from being started or finished", () => {
  const state = contactDayState("green");
  state.today.next_session = { ...state.today.next_session, session_id: "2026-09-25", title: "Hard sparring",
    coach_led_contact: "Hard sparring" };
  state.today.primary_safety_notice = HEAD_IMPACT_NOTICE;
  const html = renderPanel(state);
  assert.doesNotMatch(html, />Start hard sparring<|>Start session<|>Finish session<|>Mark done</);
  assert.match(html, /No more contact today: you reported being rocked or dropped/);
});

test("without a head-impact notice the same contact day is unchanged", () => {
  const state = contactDayState("green");
  state.today.primary_safety_notice = { ...HEAD_IMPACT_NOTICE, code: "skin_care", chip: "SKIN CARE", tone: "amber" };
  assert.match(renderPanel(state), />Start hard sparring</);
});

test("local log controls reuse the calendar projection; a new plan refreshes the blocks", async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  const state = contactDayState("green");
  const day = toISODate(resolveTrainingDay(new Date()));
  state.today.training_day = day;
  state.today.next_session = { ...state.today.next_session, calendar_date: day, coach_led_contact: undefined };
  const weeks: StructuredPlan["weeks"] = [{ week_index: 1, days: [{ date: day, sessions: [{
    session_id: state.today.next_session.session_id!, title: "Strength", blocks: [
      { block_id: "squat", block_type: "strength", display_name: "Squat", sets: 3, reps: "5" },
    ],
  }] }] }];
  let calendarReads = 0;
  const plan: StructuredPlan = { get weeks() { calendarReads++; return weeks; } };
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response('{"response_sets":[],"history_truncated":false}')) as typeof fetch;
  const render = (structuredPlan: StructuredPlan) => root.render(
    <AuthProvider><ToastProvider><TodaySessionPanel state={state} structuredPlan={structuredPlan}
      token="token" onRefresh={async () => {}} /></ToastProvider></AuthProvider>,
  );
  try {
    await act(async () => { render(plan); });
    assert.match(container.textContent ?? "", /Squat/);
    const initialReads = calendarReads;
    assert.ok(initialReads > 0);
    const skip = Array.from(container.querySelectorAll("button")).find(b => b.textContent?.trim() === "Skip session");
    assert.ok(skip);
    await act(async () => { skip.click(); });
    assert.ok(container.querySelector("form"), "the skip form opens");
    assert.equal(calendarReads, initialReads, "opening a local form must not rescan the calendar");
    const changedPlan = structuredClone({ weeks });
    changedPlan.weeks![0].days![0].sessions![0].blocks![0].display_name = "Updated squat";
    await act(async () => { render(changedPlan); });
    assert.match(container.textContent ?? "", /Updated squat/);
  } finally {
    await act(async () => { root.unmount(); });
    container.remove(); globalThis.fetch = originalFetch;
  }
});

test("a previewed session says when it opens and that an injury may still change it", () => {
  assert.equal(previewUnlockCopy("2026-10-29", "2026-10-28", false), "Available tomorrow after check-in.");
  assert.equal(previewUnlockCopy("2026-10-31", "2026-10-28", false), "Available Saturday after check-in.");
  assert.equal(previewUnlockCopy("2026-11-08", "2026-10-28", false), "Available on the day after check-in.");
  assert.equal(previewUnlockCopy(undefined, "2026-10-28", false), "Available on the day after check-in.");
  assert.equal(
    previewUnlockCopy("2026-10-29", "2026-10-28", true),
    "Available tomorrow after check-in. It may be adjusted for your injury.",
  );
});

test("YOUR FOCUS stays on the card and each UNLXCK CUE sits under its own exercise", async () => {
  const plan = {
    weeks: [{
      week_index: 1,
      days: [{
        date: "2026-10-10",
        weekday: "Sat",
        sessions: [
          // Athlete-built technical session (manual plan) and generated strength work, same day.
          { session_id: "tech", title: "Technical Combat", session_type: "skill", prescription_source: "athlete", blocks: [] },
          { session_id: "str", title: "Strength", session_type: "strength_power",
            blocks: [
              { block_id: "pistol", block_type: "strength", display_name: "Assisted Pistol Squat", sets: 3, reps: "8" },
              { block_id: "goblet", block_type: "strength", display_name: "Goblet Squat", sets: 3, reps: "8" },
              { block_id: "bench", block_type: "strength", display_name: "Bench Press", sets: 3, reps: "5" },
            ] },
        ],
      }],
    }],
  } as StructuredPlan;
  const state: TodayCommandView = {
    active_plan: { id: "plan-1", name: "Fight camp", phase: "GPP", fight_date: "2026-11-05" },
    today: {
      training_day: "2026-10-09", recommendation_state: "train_as_planned", decision_tier: "green", warnings: [],
      next_session: { session_id: "tech", title: "Technical Combat", calendar_date: "2026-10-10", session_relation: "next" },
      session_scope: "next", session_label: "Next session", completion_status: "done",
    },
    risk_watch: [], open_injuries: [], week_summary: {}, quick_actions: [],
  };
  const item = (fields: Record<string, unknown>) => ({ training_day: "2026-10-08", sport: null, scope: null, category: null,
    exercise_targets: [], movement_patterns: [], ...fields });
  const focus = { focus: "Need to get in pockets", training_day: "2026-10-08", sport: null, items: [
    item({ id: "jab", origin: "inferred", focus: "Return the jab to guard.", scope: "sport_technique", sport: "boxing" }),
    item({ id: "log-1", origin: "athlete", focus: "Need to get in pockets" }),
    item({ id: "pistol-cue", origin: "inferred", focus: "Keep your foot planted and control the descent.",
      scope: "specific_exercise", exercise_targets: ["pistol squat"] }),
    item({ id: "squat-cue", origin: "inferred", focus: "Sit between your heels.", scope: "movement_pattern",
      movement_patterns: ["squat"] }),
  ] };
  const original = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify(focus), { status: 200 })) as typeof fetch;
  const container = domWindow.document.createElement("div");
  domWindow.document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => {
      root.render(<AuthProvider><ToastProvider>
        <TodaySessionPanel state={state} structuredPlan={plan} token="token" painReasonAllowed athleteSports={["boxing"]}
          onRefresh={async () => {}} />
      </ToastProvider></AuthProvider>);
    });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
    // The fighter's words keep the session slot even though a newer cue fits; the technique cue sits beside it.
    const lines = [...container.querySelectorAll(".today-session-focus")].map((line) => line.textContent);
    assert.deepEqual(lines, ["Your focusNeed to get in pockets", "UNLXCK CUEReturn the jab to guard.Dismiss"]);
    assert.ok(container.querySelector(".today-session-focus svg"), "target icon");
    assert.doesNotMatch(container.textContent ?? "", /Keep your foot planted|Last training focus/);

    const preview = [...container.querySelectorAll("button")].find((button) => button.textContent === "Preview session");
    await act(async () => { preview!.dispatchEvent(new domWindow.MouseEvent("click", { bubbles: true })); });
    const screen = domWindow.document.querySelector('[role="dialog"][data-variant="screen"]')!;
    const cueOf = (name: string) => [...screen.querySelectorAll(".ex-row")]
      .find((row) => row.querySelector(".ex-row-title")?.textContent === name)?.querySelector(".ex-row-cue")?.textContent ?? null;
    assert.equal(cueOf("Assisted Pistol Squat"), "UNLXCK CUEKeep your foot planted and control the descent.Dismiss");
    assert.equal(cueOf("Goblet Squat"), "UNLXCK CUESit between your heels.Dismiss");
    assert.equal(cueOf("Bench Press"), null);
    assert.equal(screen.querySelectorAll(".ex-row-cue").length, 2);
  } finally {
    globalThis.fetch = original;
    act(() => root.unmount());
    container.remove();
    domWindow.document.querySelectorAll('[role="dialog"]').forEach((dialog) => dialog.remove());
  }
});

test("UNLXCK CUE rows render only where Today provides cues", () => {
  const plan = { weeks: [{ week_index: 1, days: [{ date: "2026-06-13", weekday: "Sat", sessions: [{ session_id: "s", title: "Strength",
    blocks: [{ block_id: "pistol", block_type: "strength", display_name: "Assisted Pistol Squat", sets: 3, reps: "8" }] }] }] }] } as StructuredPlan;
  const current = resolveCurrentDay(plan, new Date(2026, 5, 13));
  const cue = { id: "c", text: "Keep your foot planted and control the descent.", origin: "inferred" as const };
  assert.match(renderToStaticMarkup(<TodaySessionBlocks current={current} exerciseCues={{ cues: { pistol: cue } }} />),
    /class="ex-row-cue"><span class="sp-stat-label">UNLXCK CUE<\/span>Keep your foot planted and control the descent.<\/p>/);
  assert.doesNotMatch(renderToStaticMarkup(<TodaySessionBlocks current={current} />), /ex-row-cue|UNLXCK CUE/);
});

test("UNLXCK CUE reaches Today's exercises when no next_session is reported, and leaves once logged", async () => {
  // The panel resolves the athlete's training day (03:00 rollover), so the plan is dated to it.
  const day = toISODate(resolveTrainingDay(new Date()));
  const plan = { weeks: [{ week_index: 1, days: [{ date: day, sessions: [{ session_id: "str", title: "Strength",
    session_type: "strength_power", blocks: [
      { block_id: "pistol", block_type: "strength", display_name: "Assisted Pistol Squat", sets: 3, reps: "8" },
      { block_id: "bench", block_type: "strength", display_name: "Bench Press", sets: 3, reps: "5" },
    ] }] }] }] } as StructuredPlan;
  const stateFor = (completion_status: TodayCommandView["today"]["completion_status"]): TodayCommandView => ({
    active_plan: { id: "plan-1", name: "Fight camp", phase: "GPP", fight_date: "2026-11-05" },
    today: { training_day: day, recommendation_state: "train_as_planned", decision_tier: "green", warnings: [],
      next_session: {}, session_scope: "today", session_label: "Today's session", completion_status },
    risk_watch: [], open_injuries: [], week_summary: {}, quick_actions: [],
  } as TodayCommandView);
  const focus = { focus: null, training_day: null, sport: null, items: [{ id: "pistol-cue", origin: "inferred",
    focus: "Keep your foot planted and control the descent.", training_day: "2026-10-08", sport: null,
    scope: "specific_exercise", category: "strength", exercise_targets: ["pistol squat"], movement_patterns: [] }] };
  const original = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify(focus), { status: 200 })) as typeof fetch;
  const container = domWindow.document.createElement("div");
  domWindow.document.body.append(container);
  const root = createRoot(container);
  const render = async (state: TodayCommandView) => {
    await act(async () => {
      root.render(<AuthProvider><ToastProvider>
        <TodaySessionPanel state={state} structuredPlan={plan} token="token" painReasonAllowed onRefresh={async () => {}} />
      </ToastProvider></AuthProvider>);
    });
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  };
  const cueOf = (name: string) => [...container.querySelectorAll(".ex-row")]
    .find((row) => row.querySelector(".ex-row-title")?.textContent === name)?.querySelector(".ex-row-cue")?.textContent ?? null;
  try {
    await render(stateFor("not_started"));
    // The no-session branch: the date headline, the day's exercises, no session focus line.
    assert.equal(container.querySelector(".today-session-head"), null);
    assert.ok(container.querySelector(".ex-row"), "today's exercises are listed");
    assert.equal(cueOf("Assisted Pistol Squat"), "UNLXCK CUEKeep your foot planted and control the descent.Dismiss");
    assert.equal(cueOf("Bench Press"), null);
    assert.equal(container.querySelector(".today-session-focus"), null);
    await render(stateFor("done"));
    assert.equal(container.querySelectorAll(".ex-row-cue").length, 0);
  } finally {
    globalThis.fetch = original;
    act(() => root.unmount());
    container.remove();
  }
});

for (const tier of ["not_checked_in", "pull_back", "stop"] as const) {
  test(`advisory guidance allows session start and sparring timer under ${tier}`, () => {
    const state = contactDayState(tier);
    state.effective_clinician_clearance = { level: "rehab_only", scopes: ["rehab"], requires_update: true, limited_by: [] };
    const html = renderPanel(state);
    assert.match(html, />Start session</);
    assert.match(html, /Sparring rounds<\/button>/);
  });
}

test("advisory concussion warning keeps Start and both timers accessible", () => {
  const state = contactDayState("green");
  state.today.primary_safety_notice = HEAD_IMPACT_NOTICE;
  const html = renderPanel(state);
  assert.match(html, /<strong role="alert">Stop training and seek medical clearance\.<\/strong>/);
  assert.match(html, />Start session</);
  assert.match(html, /Sparring rounds<\/button>/);
  assert.match(html, /Free timer<\/button>/);
  assert.doesNotMatch(html, /locked|unlock|Do not start this session/);
});

test("advisory clinician warning gives a reason without locking actions", () => {
  const state = contactDayState("green");
  state.effective_clinician_clearance = { level: "train_no_contact", scopes: ["rehab", "training"], requires_update: false, limited_by: [] };
  const html = renderPanel(state);
  assert.match(html, /Training caution: follow your clinician&#x27;s contact restrictions\./);
  assert.match(html, />Start session</);
  assert.match(html, /Sparring rounds<\/button>/);
  assert.match(html, /Free timer<\/button>/);
  assert.doesNotMatch(html, /locked|unlock|Do not start this session/);
});


test("Start opens the normal round timer when a held session has no timed blocks", async () => {
  const container = document.createElement("div"); document.body.appendChild(container);
  const root = createRoot(container);
  const state = contactDayState("stop");
  state.today.next_session = { ...state.today.next_session, coach_led_contact: undefined };
  const original = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({ completion: { id: "c1" }, rehab_response_prompts: [], logs: [], response_sets: [] }), { status: 200 })) as typeof fetch;
  const render = () => root.render(<AuthProvider><ToastProvider><RoundTimerProvider><TodaySessionPanel state={state} structuredPlan={null} token="token" onRefresh={async () => { state.today.completion_status = "started"; render(); }} /></RoundTimerProvider></ToastProvider></AuthProvider>);
  try {
    domWindow.localStorage.clear();
    await act(async () => { render(); });
    const start = Array.from(container.querySelectorAll("button")).find(b => b.textContent?.trim() === "Start session");
    assert.ok(start);
    await act(async () => { start.click(); });
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
    assert.ok(document.querySelector('.st-root[role="dialog"]'), "the round timer opens rather than only updating status");
  } finally { globalThis.fetch = original; act(() => root.unmount()); container.remove(); domWindow.localStorage.clear(); }
});

for (const saved of [null, "07:30"] as const) {
  test(`the training time row ${saved ? "stays hidden once a time is saved" : "saves a time without promising a reminder push can't deliver"}`, async () => {
    const originalFetch = globalThis.fetch;
    const puts: unknown[] = [];
    globalThis.fetch = (async (input: string, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/push/settings")) {
        return new Response(JSON.stringify({enabled: false, public_key: "", preferences: {preferred_training_time: saved}}));
      }
      if (url.endsWith("/api/push/preferences")) {
        puts.push(JSON.parse(String(init?.body)));
        return new Response(JSON.stringify({preferred_training_time: "06:45"}));
      }
      return new Response(JSON.stringify({logs: [], pending: [], rehab_response_prompts: []}));
    }) as typeof fetch;
    const state: TodayCommandView = {
      active_plan: {id: "plan-1", name: "Camp", phase: "GPP"},
      today: {training_day: "2026-08-01", recommendation_state: "train_as_planned", decision_tier: "green", warnings: [],
        next_session: {session_id: "session-1", title: "Strength", session_relation: "today", effective_load: "moderate"},
        session_scope: "today", session_label: "Today's session", completion_status: "not_started"},
      risk_watch: [], open_injuries: [], week_summary: {}, quick_actions: []};
    try { domWindow.localStorage.removeItem("unlxck:training-time-row:dismissed"); } catch { /* optional */ }
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    try {
      await act(async () => {
        root.render(<AppSessionContext.Provider value={{isReady: true, isMeHydrated: true, hasTransientMeError: false,
          session: {access_token: "token"} as never, me: null, previewAppearanceMode: () => {}, refreshMe: async () => {},
          replaceMe: () => {}, signOut: async () => {}}}>
          <ToastProvider><FadeThroughProvider><RoundTimerProvider>
            <TodaySessionPanel state={state} structuredPlan={null} token="token" onRefresh={async () => {}} />
          </RoundTimerProvider></FadeThroughProvider></ToastProvider>
        </AppSessionContext.Provider>);
      });
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
      const row = container.querySelector<HTMLFormElement>('form[aria-label="Training time"]');
      if (saved) {
        assert.equal(row, null);
        return;
      }
      assert.ok(row);
      // Push is off on the server here: the row never promises a reminder.
      const submit = row.querySelector('button[type="submit"]');
      assert.equal(submit?.textContent, "Save");
      assert.equal(submit?.getAttribute("aria-label"), "Save time");
      assert.doesNotMatch(row.textContent ?? "", /Remind me|\bRemind\b/);
      assert.match(row.textContent ?? "", /Reminders need notifications on this device\./);
      const input = row.querySelector<HTMLInputElement>('input[type="time"]')!;
      await act(async () => {
        Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), "value")!.set!.call(input, "06:45");
        input.dispatchEvent(new Event("input", {bubbles: true}));
      });
      await act(async () => { row.requestSubmit(); await new Promise(resolve => setTimeout(resolve, 0)); });
      assert.deepEqual(puts, [{preferred_training_time: "06:45"}]);
      assert.equal(container.querySelector('form[aria-label="Training time"]'), null);
      assert.match(document.body.textContent ?? "", /Training time saved: 06:45/);
    } finally {
      await act(async () => { root.unmount(); });
      container.remove();
      globalThis.fetch = originalFetch;
    }
  });
}
