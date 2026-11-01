import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";

import {
  buildDayTimeline,
  BlockCard,
  ExerciseRationaleProvider,
  ExerciseRow,
  SessionCard,
  SessionlessDayCard,
  TodayCard,
  StructuredPlanRenderer,
  weekStripCenterOffset,
} from "./structured-plan-renderer";
import { openBlockWeekIntent } from "@/lib/open-block";
import { formatPlanLabel } from "@/lib/plan-labels";
import type {
  PlanScheduleContext,
  RehabLabelPolicy,
  StructuredPlan,
  StructuredSession,
} from "@/lib/types";

test("expanded footwork retains cue method and stance after a pinned lead cue", () => {
  const html = renderToStaticMarkup(<BlockCard hideLeadCue block={{ display_name: "Footwork drill",
    coaching_cues: ["Circle off the line.", "Cue Method: Partner feeds a random cue.",
      "Side / Stance: Work both directions from your normal stance."],
    stop_rules: ["Stop if the feet cross."],
  }} />);
  assert.doesNotMatch(html, /Circle off the line/);
  assert.match(html, /Cue Method: Partner feeds a random cue/);
  assert.match(html, /Side \/ Stance: Work both directions from your normal stance/);
  assert.match(html, /Stop if the feet cross/);
});

test("the competition day retains the fight-day protocol instruction", () => {
  const html = renderToStaticMarkup(<SessionlessDayCard day={{countdown_label: "D-0", day_type: "competition",
    today_card: {headline: "Fight day"}, sessions: []}} />);
  assert.match(html, /warm-up and fight protocol; no additional S&amp;C/);
});

function countOccurrences(text: string, needle: string): number {
  return text.split(needle).length - 1;
}

test("open calendar shows all seven weekdays without turning availability into workouts", () => {
  const weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] as const;
  const plan: StructuredPlan = {
    plan_metadata: { title: "Open plan", plan_type: "open_ongoing_system" },
    weeks: [{ week_index: 1, phase_label: "GPP", days: weekdays.map((weekday, index) => ({
      weekday, date: `2026-10-${12 + index}`, day_type: "rest",
      today_card: { headline: weekday === "Wed" || weekday === "Sun" ? "Unavailable" : "No session planned" },
      sessions: [],
    })) }],
  };
  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} openOngoing
    today={new Date("2026-10-14T12:00:00")} currentTrainingDayIso="2026-10-14"
    onLogSession={() => { throw new Error("An empty day cannot log a session"); }} />);
  for (const weekday of weekdays) assert.ok(html.includes(weekday.toUpperCase()), `Missing ${weekday}`);
  assert.equal(countOccurrences(html, '>Unavailable<'), 2);
  assert.ok(html.includes("No session planned"));
  assert.doesNotMatch(html, /Log session|Missed/);
});

for (const openOngoing of [false, true]) {
  test(`${openOngoing ? "open" : "fight-camp"} calendar uses compact neutral rows and preserves recovery/contact`, () => {
    const plan: StructuredPlan = { weeks: [{ week_index: 1, days: [
      { date: "2026-10-26", weekday: "Mon", day_type: "rest", sessions: [],
        today_card: { headline: "No session planned", mindset_anchor: { focus_cue: "Recover well" } } },
      { date: "2026-10-27", weekday: "Tue", day_type: "high", sessions: [],
        today_card: { headline: "Hard sparring", coach_led_contact: "Hard sparring" } },
      { date: "2026-10-28", weekday: "Wed", day_type: "rest", sessions: [], today_card: { headline: "Unavailable" } },
      { date: "2026-10-29", weekday: "Thu", day_type: "recovery", sessions: [],
        today_card: { headline: "Prescribed recovery", mindset_anchor: { focus_cue: "Keep the prescribed mobility dose" } } },
      { date: "2026-10-30", weekday: "Fri", sessions: [{ session_id: "strength", title: "Strength", blocks: [] }] },
      { date: "2026-10-31", weekday: "Sat", day_type: "rest", sessions: [], today_card: { headline: "No session planned" } },
      { date: "2026-11-01", weekday: "Sun", day_type: "rest", sessions: [], today_card: { headline: "Unavailable" } },
    ] }] };
    const original = JSON.stringify(plan);
    const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} openOngoing={openOngoing}
      today={new Date(2026, 9, 26)} focusDay={new Date(2026, 9, 27)} currentDayLabel="Next session" />);
    assert.equal(countOccurrences(html, "cm-rest-day"), 4);
    assert.equal(countOccurrences(html, ">Unavailable<"), 2);
    assert.equal(countOccurrences(html, "cm-day-current"), 1);
    assert.match(html, /cm-day-now">Today/);
    assert.match(html, /Hard sparring/);
    assert.match(html, /Keep the prescribed mobility dose/);
    assert.doesNotMatch(html, /Recover well/);
    assert.equal(JSON.stringify(plan), original);
    if (openOngoing) {
      const withoutTodayResponse = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} openOngoing
        today={new Date(2026, 9, 26)} />);
      assert.equal(countOccurrences(withoutTodayResponse, "cm-day-current"), 1);
      assert.match(withoutTodayResponse, /cm-day-now">Today/);
    }
  });
}

test("owned custom plan rest and recovery days offer Add session without changing AI plans", () => {
  const plan: StructuredPlan = {
    plan_metadata: { title: "Own plan", plan_type: "open_ongoing_system" },
    weeks: [{ week_index: 1, days: [
      { weekday: "Mon", date: "2026-11-02", day_type: "rest", sessions: [], today_card: { headline: "Rest day" } },
      { weekday: "Tue", date: "2026-11-03", day_type: "recovery", sessions: [], today_card: { headline: "Recovery day" } },
      { weekday: "Wed", date: "2026-11-04", day_type: "moderate", sessions: [
        { session_id: "strength", title: "Strength", session_type: "strength_power", blocks: [] },
      ] },
    ] }],
  };
  const own = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} openOngoing customPlanId="draft-1"
    today={new Date("2026-11-02T12:00:00")} />);
  assert.match(own, /href="\/plans\/custom\?plan=draft-1&amp;addSession=Mon"/);
  assert.match(own, /href="\/plans\/custom\?plan=draft-1&amp;addSession=Tue"/);
  assert.equal(countOccurrences(own, "+ Add session"), 2);
  const generated = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} openOngoing
    today={new Date("2026-11-02T12:00:00")} />);
  assert.doesNotMatch(generated, /Add session/);
});

test("custom calendar enables rest selection without disabling planned sessions or AI-plan restrictions", () => {
  const plan: StructuredPlan = { weeks: [{ week_index: 1, days: [
    { weekday: "Mon", date: "2026-11-02", day_type: "rest", sessions: [] },
    { weekday: "Tue", date: "2026-11-03", day_type: "moderate", sessions: [
      { session_id: "training", title: "Training", blocks: [{ block_id: "exercise", display_name: "Squat" }] },
    ] },
  ] }] };
  const own = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} defaultView="calendar" customPlanId="custom-1"
    today={new Date("2026-11-02T12:00:00")} />);
  assert.match(own, /<td data-state="rest"><button/);
  assert.match(own, /<td data-state="planned"><button/);
  const generated = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} defaultView="calendar"
    today={new Date("2026-11-02T12:00:00")} />);
  assert.match(generated, /<td data-state="rest"><span/);
  assert.match(generated, /<td data-state="planned"><button/);
});

test("legacy open neutral rows use the real training date on a recurring weekday match", () => {
  const plan: StructuredPlan = { weeks: [{ days: [{ date: "2026-10-12", weekday: "Mon", day_type: "rest",
    today_card: { headline: "No session planned" }, sessions: [] }] }] };
  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} openOngoing today={new Date(2026, 9, 26)} />);
  assert.match(html, /MON 26 OCT/);
  assert.doesNotMatch(html, /MON 12 OCT/);
  assert.match(html, /cm-day-now">Today/);
});

test("structured renderer uses one session and exposes compact rows inside each day", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "TAPER",
        days: [
          {
            date: "2026-06-15",
            countdown_label: "D-19",
            day_type: "moderate",
            today_card: {
              headline: "Morning intro duplicate",
              readiness_status: "train_as_planned",
              mindset_anchor: {
                intent: "Duplicate intro intent",
                focus_cue: "Duplicate intro focus",
              },
            },
            sessions: [
              {
                session_id: "ses-1",
                session_type: "mixed",
                title: "Freshness Reset",
                objective: "Restore freshness without adding load.",
                mindset_anchor: {
                  intent: "Stay loose",
                  focus_cue: "Clean rhythm",
                  reset_cue: "Breathe and reset",
                  confidence_anchor: "Rounds are banked",
                  context: "Taper freshness day",
                },
                blocks: [{ block_id: "blk-1", display_name: "Breathing reset" }],
              },
            ],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  assert.equal(countOccurrences(html, "Freshness Reset"), 1);
  // A non-current day keeps a neutral countdown (red is reserved for today).
  assert.equal(html.includes('<span class="sp-countdown cm-day-countdown">D-19</span>'), true);
  assert.equal(html.includes("sp-countdown cm-day-countdown sp-accent"), false);
  assert.equal(html.includes('<span class="sp-week-title cm-day-title">Mon</span>'), true);
  // The closed row shows only the completion fraction — no session-count pill.
  assert.equal(html.includes("1 session"), false);
  assert.equal(html.includes("Morning intro duplicate"), false); // headline not shown on session days
  // The more specific session mindset suppresses the broader day fallback.
  assert.equal(html.includes("Duplicate intro intent"), false);
  assert.equal(html.includes("Breathing reset"), true);
  assert.equal(html.includes("MORE"), false);
  assert.equal(html.includes("LESS"), false);
  assert.equal(html.includes("Show more (1 block)"), false);
  assert.equal(html.includes("Context"), true);
  assert.equal(html.includes("Taper freshness day"), true);
  // A session without exercise cues gets one specific coaching instruction.
  assert.equal(html.includes("Clean rhythm"), true);
  assert.equal(html.includes("Breathe and reset"), false);
  assert.equal(html.includes("Rounds are banked"), false);
  assert.equal(html.includes(">Mindset<"), false);
  assert.equal(html.includes(">Intent</span>"), false);
  assert.equal(html.includes(">Anchor</span>"), false);
  assert.equal(html.includes(">Coach cue</span>"), true);
});

test("the current Plan day shows compact exercise rows before a demo is opened", () => {
  const plan = {
    weeks: [{
      week_index: 1,
      days: [{
        date: "2026-09-29",
        weekday: "Tue",
        sessions: [{
          session_id: "strength-1",
          title: "Lower-body strength",
          blocks: [{ block_id: "rdl-db", block_type: "strength", display_name: "Romanian Deadlift (DB)" }],
        }],
      }],
    }],
  } as StructuredPlan;
  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={plan}
      today={new Date(2026, 8, 29)}
      exerciseMedia={{ "Romanian Deadlift (DB)": { provider: "youtube", video_id: "hQgFixeXdZo", start_s: 27, end_s: 49, source: "curated" } }}
    />,
  );

  assert.match(html, /Romanian Deadlift \(DB\)/);
  assert.match(html, /aria-expanded="false"/);
  assert.match(html, /class="ex-row-thumb"/);
  assert.doesNotMatch(html, /class="ex-demo"/);
});

test("a block's demo resolves on its exercise_key, not its display copy", () => {
  const plan = {
    weeks: [{
      week_index: 1,
      days: [{
        date: "2026-09-29",
        weekday: "Tue",
        sessions: [{
          session_id: "shadow-flow",
          title: "Shadowboxing Aerobic Flow",
          blocks: [
            { block_id: "flow", block_type: "conditioning", display_name: "3 x 2 min easy rounds", exercise_key: "tempo-shadowboxing" },
            { block_id: "other", block_type: "conditioning", display_name: "Easy jog", exercise_key: "easy-jog" },
          ],
        }],
      }],
    }],
  } as StructuredPlan;
  const media = { provider: "youtube", video_id: "hQgFixeXdZo", start_s: 0, end_s: null, source: "curated" } as const;
  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={plan}
      today={new Date(2026, 8, 29)}
      // A legacy display_name entry must not leak onto a keyed block.
      exerciseMedia={{ "exercise:tempo-shadowboxing": media, "Easy jog": media }}
    />,
  );

  assert.match(html, /3 x 2 min easy rounds/);
  assert.equal(html.match(/class="ex-row-thumb"/g)?.length, 1);
});

test("open-plan weekday fallback labels today with the live date, not the stale date", () => {
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

  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={plan}
      today={new Date(2026, 6, 18)}
      openOngoing
      scheduleContext={{
        schedule_mode: "open_recurring",
        projection_status: "projected",
        current_week_number: 2,
      }}
    />,
  );

  assert.equal(html.includes("SAT 18 JUL"), true);
  assert.equal(html.includes("SAT 13 JUN"), false);
});

test("an open plan whose block starts next week marks no day as today", () => {
  // The block anchors to Monday 3 August while the athlete is on Saturday 1
  // August. Every row belongs to a week that has not started, so the timeline
  // reads in date order with no "Today" stamped onto a future row.
  const plan = {
    schema_version: "text-adapter.v1",
    plan_metadata: { title: "Open plan", plan_type: "open_ongoing_system" },
    weeks: [
      {
        week_id: "week-1",
        week_index: 1,
        days: [
          {
            date: "2026-08-06",
            weekday: "Thu",
            sessions: [{ session_id: "thu-strength", title: "Thursday strength", blocks: [] }],
          },
          {
            date: "2026-08-08",
            weekday: "Sat",
            sessions: [{ session_id: "sat-strength", title: "Saturday strength", blocks: [] }],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={plan}
      today={new Date(2026, 7, 1)}
      openOngoing
      scheduleContext={{
        schedule_mode: "open_recurring",
        projection_status: "projected",
        anchor_date: "2026-08-03",
        current_week_number: 1,
      }}
    />,
  );

  assert.equal(html.includes("THU 06 AUG"), true);
  assert.equal(html.includes("SAT 08 AUG"), true);
  assert.equal(html.includes("SAT 01 AUG"), false);
  assert.equal(html.includes('cm-day-now">Today'), false);
  assert.equal(html.includes('cm-day-now">Next session'), true);
  // With no day markable as today, the plan says when it does start — the block
  // anchor (Monday), not the first day that happens to carry a session.
  assert.equal(html.includes("Plan starts Mon 03 Aug 2026"), true);
  assert.equal(html.includes("in 2 days"), true);
  assert.equal(html.includes("Plan starts Thu 06 Aug 2026"), false);
});

test("a not-yet-started plan without an anchor falls back to its first scheduled day", () => {
  const plan = {
    schema_version: "text-adapter.v1",
    plan_metadata: { title: "Open plan", plan_type: "open_ongoing_system" },
    weeks: [
      {
        week_id: "week-1",
        week_index: 1,
        days: [
          {
            date: "2026-08-06",
            weekday: "Thu",
            sessions: [{ session_id: "thu-strength", title: "Thursday strength", blocks: [] }],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={plan}
      today={new Date(2026, 7, 1)}
      openOngoing
      scheduleContext={{
        schedule_mode: "open_recurring",
        projection_status: "projected",
        current_week_number: 1,
      }}
    />,
  );

  assert.equal(html.includes("Plan starts Thu 06 Aug 2026"), true);
  assert.equal(html.includes("in 5 days"), true);
});

test("structured renderer normalizes legacy D0 event-day labels to D-0", () => {
  const plan = {
    schema_version: "1.0",
    event_context: { fight_date: "2026-07-17" },
    weeks: [
      {
        week_id: "fight-week",
        week_index: 1,
        countdown_start: "D0",
        countdown_end: "D0",
        days: [
          {
            date: "2026-07-17",
            countdown_label: "D0",
            day_type: "rest",
            sessions: [],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  assert.equal(html.includes(">D0<"), false);
  assert.equal(html.includes("D-0"), true);
});

test("open ongoing renderer uses renewable block labels instead of fight-camp phases", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Open Plan", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      { week_id: "wk-1", week_index: 1, phase_label: "SPP", week_goal: "Baseline", days: [{ date: "", day_type: "high", sessions: [] }] },
      { week_id: "wk-2", week_index: 2, phase_label: "SPP", week_goal: "Small progression", days: [] },
      { week_id: "wk-3", week_index: 3, phase_label: "SPP", week_goal: "Highest controlled week", days: [] },
      { week_id: "wk-4", week_index: 4, phase_label: "SPP", week_goal: "Deload and reassess", days: [] },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} openOngoing />);

  assert.equal(html.includes('aria-label="Training block weeks"'), true);
  // The squares stay bare (W1 · progress); the selected week's intent names it.
  assert.equal(html.includes("cm-week-pill-phase"), false);
  assert.equal(html.includes('<span class="sp-tag sp-accent">Baseline</span>'), true);
  assert.equal(html.includes("Specific prep"), false);
  assert.equal(html.includes("General prep"), false);
  assert.equal(html.includes(">Taper<"), false);
  assert.equal(html.includes(">Load</span>"), false);
  assert.equal(html.includes("Block 1 · Week 1 of 4"), true);
});

test("open ongoing renderer falls back when schedule and week numbers are non-finite", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Open Plan", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-bad",
        week_index: Number.NaN,
        phase_label: "SPP",
        week_goal: "Baseline",
        days: [],
      },
    ],
  } as unknown as StructuredPlan;
  const scheduleContext: PlanScheduleContext = {
    schedule_mode: "open_recurring",
    projection_status: "projected",
    current_week_number: Number.NaN,
  };

  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={plan}
      openOngoing
      scheduleContext={scheduleContext}
    />,
  );

  assert.equal(html.includes("Week 1 of 4"), true);
  assert.equal(html.includes("NaN"), false);
});

// Builds a day whose day-card mindset and per-session mindsets can be varied
// independently. Each entry in `sessionMindsets` becomes one session; `undefined`
// omits the session's mindset_anchor, an object supplies one. This exercises the
// rule that SESSION mindsets take priority and the DAY mindset is only a
// fallback when no session defines a usable anchor.
function mindsetPlan({
  dayCardMindset,
  sessionMindsets,
}: {
  dayCardMindset?: unknown;
  sessionMindsets: unknown[];
}): StructuredPlan {
  return {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "SPP",
        days: [
          {
            date: "2026-06-15",
            countdown_label: "D-19",
            day_type: "hard_spar",
            today_card: {
              readiness_status: "train_as_planned",
              ...(dayCardMindset !== undefined ? { mindset_anchor: dayCardMindset } : {}),
            },
            sessions: sessionMindsets.map((sessionMindset, index) => ({
              session_id: `ses-${index + 1}`,
              session_type: "sparring",
              title: `Session ${index + 1}`,
              ...(sessionMindset !== undefined ? { mindset_anchor: sessionMindset } : {}),
              blocks: [{ block_id: `blk-${index + 1}`, display_name: "Live rounds" }],
            })),
          },
        ],
      },
    ],
  } as StructuredPlan;
}

// Each rendered MindsetAnchorCard emits exactly one `sp-coaching-list`, so its
// count is the number of distinct compact coaching cards on screen.
function mindsetCardCount(html: string): number {
  return countOccurrences(html, "sp-coaching-list");
}

test("mindset scenario 1: day mindset is the fallback when sessions have none", () => {
  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={mindsetPlan({
        dayCardMindset: { intent: "Day-only intent", focus_cue: "Day-only focus" },
        sessionMindsets: [undefined],
      })}
    />,
  );

  assert.equal(mindsetCardCount(html), 1);
  assert.equal(countOccurrences(html, "Day-only intent"), 0);
  assert.equal(html.includes("Day-only focus"), true);
});

test("mindset scenario 2: session mindset only renders on its session card", () => {
  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={mindsetPlan({
        sessionMindsets: [{ intent: "Session-only intent", focus_cue: "Session-only focus" }],
      })}
    />,
  );

  assert.equal(mindsetCardCount(html), 1);
  assert.equal(html.includes("Session-only intent"), false);
  assert.equal(html.includes("Session-only focus"), true);
});

test("mindset scenario 3: session mindsets suppress the day fallback", () => {
  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={mindsetPlan({
        dayCardMindset: { intent: "Day intent", focus_cue: "Day focus" },
        sessionMindsets: [
          { intent: "Session A intent", focus_cue: "Session A focus" },
          { intent: "Session B intent", focus_cue: "Session B focus" },
        ],
      })}
    />,
  );

  assert.equal(mindsetCardCount(html), 2);
  assert.equal(html.includes("Day intent"), false);
  assert.equal(html.includes("Session A intent"), false);
  assert.equal(html.includes("Session B intent"), false);
});

test("mindset scenario 4: any session mindset suppresses the day fallback", () => {
  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={mindsetPlan({
        dayCardMindset: { intent: "Day intent", focus_cue: "Day focus" },
        sessionMindsets: [
          { intent: "Owning session intent", focus_cue: "Owning session focus" },
          undefined,
        ],
      })}
    />,
  );

  // Keep the specific anchor; the session without one does not repeat the day.
  assert.equal(mindsetCardCount(html), 1);
  assert.equal(html.includes("Day intent"), false);
  assert.equal(html.includes("Owning session intent"), false);
});

test("mindset scenario 5: no mindset anywhere renders no mindset block", () => {
  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={mindsetPlan({ dayCardMindset: {}, sessionMindsets: [{}, undefined] })}
    />,
  );

  assert.equal(mindsetCardCount(html), 0);
  assert.equal(html.includes(">Mindset<"), false);
});

test("shows rehab and conditioning rows while keeping detailed instructions in sheets", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "GPP",
        days: [
          {
            date: "2026-06-17",
            countdown_label: "D-34",
            day_type: "low",
            sessions: [
              {
                session_id: "ses-1",
                session_type: "conditioning",
                title: "Assault Bike aerobic steady state + rehab",
                blocks: [
                  { block_id: "bike", block_type: "conditioning", display_name: "Easy Assault Bike" },
                  {
                    block_id: "rehab",
                    block_type: "rehab",
                    display_name: "Neutral-Grip Isometric Holds",
                    sets: 2,
                    reps: "12-15 s",
                    coaching_cues: ["Full rest between holds"],
                  },
                ],
              },
            ],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  assert.equal(/class="ex-row-type">Rehab(?:<|\s)/.test(html), true);
  assert.equal(html.includes("Neutral-Grip Isometric Holds"), true);
  assert.equal(html.includes("Full rest between holds"), false);
  assert.equal(html.includes("Easy Assault Bike"), true);
  assert.equal(html.includes("Show more (2 blocks)"), false);
});

// A one-session plan whose only rehab block targets the hamstring. The tag on
// that block is the thing under test in the label cases below.
function hamstringRehabPlan(): StructuredPlan {
  return {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "GPP",
        days: [
          {
            date: "2026-06-17",
            countdown_label: "D-34",
            day_type: "low",
            sessions: [
              {
                session_id: "ses-1",
                session_type: "conditioning",
                title: "Assault Bike aerobic steady state + rehab",
                blocks: [
                  { block_id: "bike", block_type: "conditioning", display_name: "Easy Assault Bike" },
                  {
                    block_id: "rehab",
                    block_type: "rehab",
                    display_name: "Isometric Hamstring Bridge Hold",
                    sets: 2,
                    reps: "12-15 s",
                  },
                ],
              },
            ],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;
}

const HAMSTRING_OPEN: RehabLabelPolicy = {
  default_mode: "prehab",
  active_regions: [{ region: "hamstring", terms: ["hamstring", "hamstrings"] }],
};

const QUAD_OPEN_HAMSTRING_CLEARED: RehabLabelPolicy = {
  default_mode: "prehab",
  active_regions: [{ region: "quads", terms: ["quad", "quads", "quadriceps"] }],
};

test("without a policy every rehab block keeps the Rehab wording", () => {
  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={hamstringRehabPlan()} />);
  assert.equal(/class="ex-row-type">Rehab(?:<|\s)/.test(html), true);
  assert.equal(/class="ex-row-type">Prehab(?:<|\s)/.test(html), false);
});

test("a rehab block whose region is still injured stays Rehab", () => {
  const html = renderToStaticMarkup(
    <StructuredPlanRenderer plan={hamstringRehabPlan()} rehabLabelPolicy={HAMSTRING_OPEN} />,
  );
  assert.equal(/class="ex-row-type">Rehab(?:<|\s)/.test(html), true);
  assert.equal(/class="ex-row-type">Prehab(?:<|\s)/.test(html), false);
});

test("a cleared region reads Prehab even while another region is injured", () => {
  // The regression: one open quad flag used to pin the whole plan to "Rehab",
  // so cleared hamstring work kept reading Rehab.
  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={hamstringRehabPlan()}
      rehabLabelPolicy={QUAD_OPEN_HAMSTRING_CLEARED}
    />,
  );
  assert.equal(/class="ex-row-type">Prehab(?:<|\s)/.test(html), true);
  assert.equal(/class="ex-row-type">Rehab(?:<|\s)/.test(html), false);
});

test("an unlocalizable open injury keeps every rehab block on Rehab", () => {
  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={hamstringRehabPlan()}
      rehabLabelPolicy={{ default_mode: "rehab", active_regions: [] }}
    />,
  );
  assert.equal(/class="ex-row-type">Rehab(?:<|\s)/.test(html), true);
  assert.equal(/class="ex-row-type">Prehab(?:<|\s)/.test(html), false);
});

test("does not duplicate a rehab insert as a summary once the blocks are expanded", () => {
  // With the full blocks open, every rehab insert renders in full below, so the
  // compact Rehab / Mobility summary must not ALSO print — no two cards for one
  // insert.
  const session = {
    session_id: "ses-1",
    session_type: "conditioning",
    title: "Aerobic + rehab",
    blocks: [
      { block_id: "bike", block_type: "conditioning", display_name: "Easy Assault Bike" },
      {
        block_id: "rehab",
        block_type: "rehab",
        display_name: "Neutral-Grip Isometric Holds",
        sets: 2,
        reps: "12-15 s",
        coaching_cues: ["Full rest between holds"],
      },
    ],
  } as unknown as StructuredSession;

  const html = renderToStaticMarkup(<SessionCard session={session} defaultOpenBlocks />);

  // The rehab insert is its own exercise row (name + dose) exactly once; the
  // summary eyebrow is gone. Its cues live behind the row, never duplicated.
  assert.equal(countOccurrences(html, "Neutral-Grip Isometric Holds"), 1);
  assert.equal(html.includes("2 × 12-15 s"), true);
  assert.equal(countOccurrences(html, "Full rest between holds") <= 1, true);
  assert.equal(html.includes("Rehab / Mobility"), false);
});

test("labels the session objective as the Why and sentence-cases it", () => {
  // Objectives arrive from the plan conversion in mixed casing (usually all
  // lower-case), which read as a typo under the Title Case session name — and
  // unlabelled they read as a description of the work rather than its reason.
  const session = {
    session_id: "ses-1",
    session_type: "mixed",
    title: "Neural Visualization",
    objective: "refine motor plan and composure without physical load",
    blocks: [{ block_id: "b1", display_name: "Quiet rehearsal" }],
  } as unknown as StructuredSession;

  const html = renderToStaticMarkup(<SessionCard session={session} />);

  assert.equal(html.includes("Refine motor plan and composure without physical load"), true);
  assert.equal(html.includes("refine motor plan and composure"), false);
  // Mental instructions are directly visible, without nested disclosures.
  assert.equal(html.includes("View steps"), false);
});

test("drops a mindset Context line that only restates the session objective", () => {
  // The objective already prints under the title; a Context anchor that repeats
  // it (bar casing/trailing period) must not print the same sentence twice, but
  // the other mindset lines and a genuinely distinct Context must stay.
  const session = {
    session_id: "ses-1",
    session_type: "skill",
    title: "Technical Shadow Rhythm",
    objective: "Timing and coordination rehearsal without physiological cost",
    mindset_anchor: {
      intent: "Reinforce entries/exits and rhythm",
      focus_cue: "Continuous smooth rounds",
      context: "Timing and coordination rehearsal without physiological cost.",
    },
    blocks: [{ block_id: "b1", display_name: "Shadow rounds" }],
  } as unknown as StructuredSession;

  const html = renderToStaticMarkup(<SessionCard session={session} defaultOpenBlocks />);

  // The objective sentence appears once (the title subtitle), not twice.
  assert.equal(
    countOccurrences(html, "Timing and coordination rehearsal without physiological cost"),
    1,
  );
  // The duplicate Context row is gone; only the specific focus remains.
  assert.equal(html.includes("Context"), false);
  assert.equal(html.includes("Reinforce entries/exits and rhythm"), false);
  assert.equal(html.includes("Continuous smooth rounds"), true);
});

test("keeps a mindset Context line that adds information beyond the objective", () => {
  const session = {
    session_id: "ses-2",
    session_type: "skill",
    title: "Sharp technical work",
    objective: "Sharpen decision rules for opening exchanges",
    mindset_anchor: {
      intent: "Sharpen decisions",
      context: "Pairs with coach-led technical session",
    },
    blocks: [{ block_id: "b1", display_name: "Drill" }],
  } as unknown as StructuredSession;

  const html = renderToStaticMarkup(<SessionCard session={session} defaultOpenBlocks />);

  assert.equal(html.includes("Context"), true);
  assert.equal(html.includes("Pairs with coach-led technical session"), true);
});

test("a malformed numeric payload never renders NaN / Infinity in the card", () => {
  // Every numeric field is deliberately malformed. The block detail is expanded
  // so the block renders in full, and the plan carries malformed macros, sleep
  // hours and week_index too — none of which may reach the DOM as text.
  const session = {
    session_id: "ses-1",
    session_type: "strength_power",
    title: "Malformed metrics",
    blocks: [
      {
        block_id: "b1",
        display_name: "Bad numbers",
        sets: Number.NaN,
        reps: Number.POSITIVE_INFINITY,
        rounds: -3,
        effort: { method: "RPE", value: Number.NaN },
        duration: { value: Number.NaN, unit: "seconds" },
        distance: { value: Number.POSITIVE_INFINITY, unit: "m" },
      },
      {
        // Malformed numeric STRING tokens must be stripped too, not just numbers.
        block_id: "b2",
        display_name: "Bad string numbers",
        sets: 3,
        reps: "Infinity",
        effort: { method: "RPE", value: "NaN" },
        load: { display: "-Infinity" },
      },
    ],
  } as unknown as StructuredSession;
  const sessionHtml = renderToStaticMarkup(<SessionCard session={session} defaultOpenBlocks />);
  assert.equal(sessionHtml.includes("NaN"), false);
  assert.equal(sessionHtml.includes("Infinity"), false);

  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    deterministic_support: {
      nutrition: {
        by_phase: {
          GPP: {
            protein_g_per_day: { min: Number.NaN, max: Number.POSITIVE_INFINITY },
            carbs_g_per_day: { min: -100, max: -50 },
          },
        },
      },
      recovery: { by_phase: { GPP: { sleep_hours_target: [Number.NaN, -2] } } },
    },
    weeks: [
      {
        week_id: "wk-1",
        week_index: Number.NaN,
        phase_label: "GPP",
        days: [{ date: "2026-06-17", day_type: "low", sessions: [session] }],
      },
    ],
  } as unknown as StructuredPlan;
  const planHtml = renderToStaticMarkup(
    <StructuredPlanRenderer plan={plan} today={new Date(2026, 5, 17)} />,
  );
  assert.equal(planHtml.includes("NaN"), false);
  assert.equal(planHtml.includes("Infinity"), false);
  // The week strip falls back to a positional label rather than "WNaN".
  assert.equal(planHtml.includes("WNaN"), false);
  assert.equal(planHtml.includes("W1"), true);
});

test("labels a stop rule stored in progression_rule as Stop rule, not Progress", () => {
  const session = {
    session_id: "ses-1",
    session_type: "strength_power",
    title: "Band-Resisted Straight Punch",
    blocks: [
      {
        block_id: "punch",
        block_type: "power",
        display_name: "Band-Resisted Straight Punch",
        sets: 2,
        reps: "6-8",
        progression_rule: "Stop the set if ankle pain increases or punch speed drops.",
      },
    ],
  } as unknown as StructuredSession;

  const html = renderToStaticMarkup(<SessionCard session={session} defaultOpenBlocks />);

  assert.equal(html.includes("Stop rule"), true);
  assert.equal(html.includes("Stop the set if ankle pain increases"), true);
});

test("cleans malformed Regression and Stop coaching cues at render time", () => {
  const session = {
    session_id: "ses-legacy",
    session_type: "mobility",
    title: "Mobility reset",
    blocks: [
      {
        block_id: "mobility",
        block_type: "mobility",
        display_name: "Mobility Reset Flow",
        coaching_cues: [
          "Regression /",
          "Stay smooth through the range.",
          "Stop: switch to breathing only if shoulder pain rises.",
        ],
        regression_options: ["Use a smaller range."],
      },
    ],
  } as unknown as StructuredSession;

  const html = renderToStaticMarkup(<SessionCard session={session} defaultOpenBlocks />);

  assert.equal(html.includes("Regression /"), false);
  assert.equal(html.includes("Stay smooth through the range."), true);
  assert.equal(html.includes(">Easier</span>"), true);
  assert.equal(html.includes(">Stop rule</span>"), true);
  assert.equal(countOccurrences(html, "switch to breathing only if shoulder pain rises."), 1);
});

test("renders fallback safety card from active notes when red flag rules are absent", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    plan_notes: [
      {
        category: "injury",
        label: "Elbow cut",
        text: "Stop immediately and report if the wound reopens or sharp pain increases.",
      },
    ],
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "GPP",
        days: [],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  assert.equal(html.includes("Safety priority"), true);
  assert.equal(html.includes("Red flags - stop"), true);
  assert.equal(html.includes("Safety note"), true);
  assert.equal(countOccurrences(html, "Stop immediately and report"), 1);
});

test("keeps calm lead notes out of Red Flags while retaining the safety disclaimer", () => {
  const leadText =
    "Target weight is not set; coach owns the final call and programmed support stays active.";
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    plan_notes: [
      {
        category: "weight_cut",
        label: "Lead notes",
        text: leadText,
      },
    ],
    weeks: [{ week_id: "wk-1", week_index: 1, phase_label: "TAPER", days: [] }],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  assert.equal(countOccurrences(html, leadText), 1);
  assert.equal(html.includes("Plan notes"), true);
  assert.equal(html.includes("Red flags - stop"), true);
  assert.equal(countOccurrences(html, 'class="safety-note '), 1);
});

test("renders a coach-led / sparring day with no app blocks as its own card", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "SPP",
        days: [
          {
            date: "2026-06-20",
            countdown_label: "D-16",
            day_type: "high",
            today_card: { headline: "Coach-led boxing — technical only" },
            sessions: [],
          },
          {
            date: "2026-06-21",
            countdown_label: "D-15",
            day_type: "rest",
            today_card: {},
            sessions: [],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  // The contact day surfaces its own compact card instead of collapsing into a
  // rest day. Its athlete copy stays short and never borrows the hard-contact
  // wording.
  assert.equal(html.includes("Technical Combat"), true);
  assert.equal(html.includes(">Low load<"), true);
  assert.equal(html.includes("Technical only, no hard sparring. Stay sharp and leave fresh."), true);
  assert.equal(html.includes("Technical-only contact today"), false);
  assert.equal(html.includes("no hard sparring"), true);
  assert.equal(countOccurrences(html, 'aria-label="Why this changed: Technical Combat"'), 1);
  assert.equal(html.includes("This is your declared hard-sparring/contact day"), false);
  assert.equal(html.includes("sp-day-card-technical"), true);
  // The genuine rest day renders as a single compact, non-expandable rest row.
  assert.equal(countOccurrences(html, "cm-rest-day"), 1);
  assert.equal(html.includes("Rest day."), false);
});

test("renders a planner microdose inside its sessionless technical-combat host day", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [{
      week_id: "wk-1",
      week_index: 1,
      phase_label: "SPP",
      days: [{
        date: "2026-08-15",
        countdown_label: "D-26",
        day_type: "low",
        today_card: { headline: "Light technical combat" },
        sessions: [],
        priority_microdose: {
          goal: "power",
          name: "Med-Ball Rotational Throw",
          prescription: "2 x 3/side @ RPE 7",
        },
      }],
    }],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  assert.equal(countOccurrences(html, "Technical Combat"), 1);
  assert.equal(countOccurrences(html, "Power microdose"), 1);
  assert.equal(countOccurrences(html, "Med-Ball Rotational Throw"), 1);
  assert.equal(countOccurrences(html, "2 x 3/side @ RPE 7"), 1);
  assert.equal(countOccurrences(html, '<article class="sp-session'), 1);
  assert.ok(html.indexOf("Technical Combat") < html.indexOf("Power microdose"));
});

test("uses the same microdose card path for every supported planner goal", () => {
  for (const goal of ["power", "speed", "strength", "footwork", "mobility"]) {
    const plan = {
      schema_version: "1.0",
      plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
      weeks: [{
        week_id: "wk-1",
        week_index: 1,
        phase_label: "SPP",
        days: [{
          date: "2026-08-15",
          countdown_label: "D-26",
          day_type: "low",
          today_card: { headline: "Light technical combat" },
          sessions: [],
          priority_microdose: { goal, name: `${goal} drill`, prescription: "2 x 3" },
        }],
      }],
    } satisfies StructuredPlan;

    const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);
    assert.equal(countOccurrences(html, `${formatPlanLabel(goal)} microdose`), 1);
    assert.equal(countOccurrences(html, "sp-priority-microdose"), 3);
  }
});

test("renders a microdose before an app session without suppressing either", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [{
      week_id: "wk-1",
      week_index: 1,
      phase_label: "SPP",
      days: [{
        date: "2026-08-15",
        countdown_label: "D-26",
        day_type: "low",
        today_card: { headline: "Light technical combat" },
        priority_microdose: { goal: "speed", name: "Fast-feet primer", prescription: "2 x 5 sec" },
        sessions: [{ session_id: "s1", session_type: "strength_power", title: "Tactical Watch", blocks: [] }],
      }],
    }],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);
  assert.equal(countOccurrences(html, "Fast-feet primer"), 1);
  assert.equal(countOccurrences(html, "Tactical Watch"), 1);
  assert.ok(html.indexOf("Fast-feet primer") < html.indexOf("Tactical Watch"));
});

test("a hard-sparring day carries the hard-sparring note, not the technical note", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "SPP",
        days: [
          {
            date: "2026-06-20",
            countdown_label: "D-20",
            day_type: "high",
            today_card: { headline: "Hard sparring — controlled hard contact" },
            sessions: [],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  assert.match(html, /cm-day-context-contact[\s\S]*?class="sp-session-title">Hard sparring<\/h3>/);
  assert.match(html, /sp-sparring-caution[\s\S]*?Controlled hard contact<\/p>/);
  assert.equal(html.includes("Hard sparring — controlled hard contact"), false);
  assert.equal(html.includes("This is your declared hard-sparring/contact day"), true);
  // A hard-sparring day must not borrow the technical-only "no hard sparring" wording.
  assert.equal(html.includes("Technical-only contact today"), false);
  assert.equal(html.includes("sp-day-card-sparring"), true);
});

test("a plain hard-sparring title stays inside the contact card", () => {
  const html = renderToStaticMarkup(<SessionlessDayCard day={{
    date: "2026-06-21",
    day_type: "high",
    today_card: { headline: "Hard sparring" },
    sessions: [],
  }} showDayLabels={false} />);

  assert.match(html, /cm-day-context-contact[\s\S]*?class="sp-session-title">Hard sparring<\/h3>/);
  assert.equal(html.includes("sp-sparring-caution"), false);
});

test("hidden hard-sparring title does not repeat in the contact card", () => {
  const html = renderToStaticMarkup(<SessionlessDayCard day={{
    date: "2026-06-21",
    day_type: "high",
    today_card: { headline: "Hard sparring — controlled hard contact" },
    sessions: [],
  }} showDayLabels={false} hideTitle />);

  assert.equal(html.includes('class="sp-session-title"'), false);
  assert.equal(html.includes("sp-sparring-caution"), true);
  assert.equal(html.includes("Controlled hard contact"), true);
});

test("open plans keep session categories inside chronological day cards", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Open Plan", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "GPP",
        days: [
          {
            date: "2026-07-13",
            weekday: "Mon",
            day_type: "moderate",
            today_card: { headline: "Support Strength" },
            sessions: [{ session_id: "s1", title: "Support Strength", blocks: [] }],
          },
          {
            date: "2026-07-15",
            weekday: "Wed",
            day_type: "high",
            today_card: { headline: "Coach-led boxing" },
            sessions: [],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={plan}
      openOngoing
      today={new Date(2026, 6, 13)}
      scheduleContext={{
        schedule_mode: "open_recurring",
        projection_status: "projected",
        anchor_date: "2026-07-13",
        current_training_day: "2026-07-13",
        block_number: 1,
        current_week_number: 1,
      }}
    />,
  );

  assert.equal(html.includes('<span class="sp-week-title cm-day-title">MON 13 JUL</span>'), true);
  assert.equal(html.includes('<span class="sp-week-title cm-day-title">WED 15 JUL</span>'), true);
  assert.equal(html.includes('<span class="sp-week-title cm-day-title">Support Strength</span>'), false);
  assert.equal(html.includes("Support Strength"), true);
  assert.equal(html.includes("D-"), false);
  assert.equal(html.includes("Block 1"), true);
});

test("ambiguous open plans fail closed instead of using category names as dates", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Open Plan", sport: "boxing", plan_type: "fight_camp" },
    raw_markdown_fallback: "## Weekly Rhythm\nMonday: Support Strength",
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        days: [
          {
            date: "",
            day_type: "moderate",
            today_card: { headline: "Support Strength" },
            sessions: [{ session_id: "s1", title: "Support Strength", blocks: [] }],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={plan}
      openOngoing
      scheduleContext={{
        schedule_mode: "open_recurring",
        projection_status: "unavailable",
      }}
    />,
  );

  assert.equal(html.includes("Schedule unavailable"), true);
  assert.equal(html.includes('<span class="sp-week-title cm-day-title">Training day 1</span>'), true);
  assert.equal(html.includes('<span class="sp-week-title cm-day-title">Support Strength</span>'), false);
});

test("renders declared light-combat context alongside app sessions in the same day card", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "SPP",
        days: [
          {
            date: "2026-06-20",
            countdown_label: "D-16",
            day_type: "moderate",
            today_card: { headline: "Light technical combat" },
            sessions: [
              { session_id: "s1", session_type: "strength", title: "Lower strength", blocks: [] },
            ],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  assert.equal(html.includes("Light technical combat"), false);
  assert.equal(html.includes("Technical Combat"), true);
  assert.equal(html.includes("Pads, drills, movement or other lower-intensity combat work."), true);
  assert.equal(html.includes(">Technical<"), true);
  assert.doesNotMatch(html, /light (?:technical )?(?:combat|sparring)/i);
  assert.equal(html.includes("Lower strength"), true);
  assert.equal(html.includes("sp-day-card-light_combat"), false);
  assert.ok(html.indexOf("Technical Combat") < html.indexOf("Lower strength"));
});

test("renders declared light-combat contact above app work without hard-sparring copy", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "TAPER",
        days: [
          {
            date: "2026-06-16",
            countdown_label: "D-4",
            day_type: "moderate",
            today_card: {
              headline: "Light technical combat",
              coach_led_contact: "Light Combat / Technical",
            },
            sessions: [
              { session_id: "s1", session_type: "strength", title: "Explosive sharpness primer", blocks: [] },
            ],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  assert.equal(html.includes("Light Combat / Technical"), false);
  assert.equal(html.match(/Technical Combat/g)?.length, 1);
  assert.equal(html.includes("Pads, drills, movement or other lower-intensity combat work."), true);
  assert.equal(html.includes("your declared hard-sparring/contact work today"), false);
  assert.equal(html.includes(">Technical<"), true);
  assert.doesNotMatch(html, /light (?:technical )?(?:combat|sparring)/i);
  assert.ok(html.indexOf("Technical Combat") < html.indexOf("Explosive sharpness primer"));
});

test("normalizes standalone declared light-combat day and session titles", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "SPP",
        days: [
          {
            date: "2026-06-18",
            countdown_label: "D-11",
            day_type: "moderate",
            today_card: { headline: "Light Combat / Technical" },
            sessions: [],
          },
          {
            date: "2026-06-25",
            countdown_label: "D-4",
            day_type: "moderate",
            sessions: [
              {
                session_id: "s1",
                session_type: "skill",
                title: "Light Combat / Technical",
                objective: "Keep technical rhythm and timing without adding fatigue.",
                blocks: [],
              },
            ],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  assert.equal(html.includes("Light Combat / Technical"), false);
  assert.equal(html.match(/Technical Combat/g)?.length, 2);
  assert.equal(html.match(/Pads, drills, movement or other lower-intensity combat work\./g)?.length, 2);
  assert.equal(html.match(/>Technical</g)?.length, 2);
  assert.doesNotMatch(html, /light (?:technical )?(?:combat|sparring)/i);
  assert.equal(html.includes("Keep technical rhythm and timing without adding fatigue."), false);
});

test("a technical day the athlete declared never reads as converted hard sparring", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "SPP",
        days: [
          {
            date: "2026-06-20",
            countdown_label: "D-9",
            day_type: "moderate",
            today_card: { headline: "Technical sparring" },
            sessions: [],
          },
          {
            date: "2026-06-21",
            countdown_label: "D-8",
            day_type: "moderate",
            today_card: {
              headline: "Fight-week freshness",
              coach_led_contact: "Pad work",
            },
            sessions: [
              {
                session_id: "s1",
                session_type: "strength",
                title: "Fight-week freshness",
                blocks: [{
                  block_id: "band-face-pull",
                  block_type: "strength",
                  display_name: "Band face pull, light",
                }],
              },
            ],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  // Both days keep the athlete's own wording and the plain light-combat note:
  // no "hard sparring is reduced" tooltip, no conversion tag or rationale.
  assert.equal(html.includes("Technical sparring"), true);
  assert.equal(html.includes("Pad work"), true);
  assert.equal(countOccurrences(html, "Pads, drills, movement or other lower-intensity combat work."), 2);
  assert.equal(html.includes("Why this changed"), false);
  assert.equal(html.includes("hard sparring is reduced"), false);
  assert.equal(html.includes(">Low load<"), false);
  assert.equal(html.includes("no hard sparring"), false);
});

test("surfaces technical contact alongside prescribed app work in the same day card", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "TAPER",
        days: [
          {
            date: "2026-06-16",
            countdown_label: "D-12",
            day_type: "moderate",
            today_card: {
              headline: "Fight-week freshness",
              coach_led_contact: "Coach-led boxing — technical only",
            },
            sessions: [
              {
                session_id: "s1",
                session_type: "strength",
                title: "Fight-week freshness",
                blocks: [{
                  block_id: "band-face-pull",
                  block_type: "strength",
                  display_name: "Band face pull, light",
                }],
              },
            ],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  // Both the coach-owned contact and the app session render in the one day card,
  // with a compact technical context surfaced above the app work.
  assert.equal(html.includes("Technical Combat"), true);
  assert.equal(html.includes(">Low load<"), true);
  assert.equal(html.includes("Fight-week freshness"), true);
  assert.equal(html.includes("Show more (1 block)"), false);
  assert.equal(countOccurrences(html, 'aria-label="Why this changed: Technical Combat"'), 1);
  assert.equal(html.includes("Technical only, no hard sparring. Stay sharp and leave fresh."), true);
  assert.equal(html.includes("Technical-only contact today"), false);
  assert.equal(html.includes("your declared hard-sparring/contact work today"), false);
  assert.ok(
    html.indexOf("Technical Combat") < html.indexOf(">Fight-week freshness<"),
  );
});

test("renders a technical-only session as the reusable sport-neutral low-load card", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "TAPER",
        days: [
          {
            date: "2026-06-16",
            countdown_label: "D-12",
            day_type: "moderate",
            sessions: [
              {
                session_id: "s1",
                session_type: "skill",
                title: "Technical-only combat",
                objective: "Generated technical-only copy that should not repeat.",
                blocks: [],
              },
            ],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  assert.equal(html.includes("Technical-only combat"), false);
  assert.equal(html.includes("Technical Combat"), true);
  assert.equal(html.includes(">Low load<"), true);
  assert.equal(countOccurrences(html, 'aria-label="Why this changed: Technical Combat"'), 1);
  assert.equal(html.includes("Technical only, no hard sparring. Stay sharp and leave fresh."), true);
  assert.equal(html.includes("Generated technical-only copy that should not repeat."), false);
  assert.equal(html.includes(">Skill<"), false);
});

test("keeps engine rationale out of athlete-facing mindset and block copy", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "TAPER",
        days: [
          {
            date: "2026-06-16",
            day_type: "moderate",
            sessions: [
              {
                session_id: "s1",
                session_type: "skill",
                title: "Pocket exchange map",
                mindset_anchor: {
                  intent: "Choose a clean exit",
                  context: "SPP pocket planning for a brawler.",
                },
                blocks: [
                  {
                    block_id: "pocket-map",
                    display_name: "Pocket exchange map",
                    purpose: "SPP pocket planning for a brawler.",
                  },
                  {
                    block_id: "timing-rule",
                    display_name: "Technical timing",
                    purpose: "From D-17 onward, hard sparring is not scheduled in this window.",
                  },
                  {
                    block_id: "exit-cue",
                    display_name: "Exit cue",
                    purpose: "Stay calm as you exit the exchange.",
                  },
                ],
              },
            ],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;
  const day = plan.weeks[0]!.days[0]!;
  const session = day.sessions![0]!;

  const html = renderToStaticMarkup(
    <SessionCard session={session} day={day} defaultOpenBlocks />,
  );

  assert.equal(html.includes("SPP pocket planning for a brawler."), false);
  assert.equal(html.includes("D-17 onward"), false);
  // Genuine mindset/execution cues remain visible.
  assert.equal(html.includes("Choose a clean exit"), true);

  // Block purpose renders on the open exercise row as the athlete-facing
  // "Builds" line, with the engine's phase prefix and style tail stripped.
  // Checked on the row itself: the mindset anchor also sanitizes to "Pocket
  // planning.", so the session markup alone would pass without the purpose.
  // (Today is where these lines show, so the rows are rendered as Today does.)
  const purposeHtml = renderToStaticMarkup(
    <ExerciseRationaleProvider>
      <ExerciseRow block={session.blocks![0]!} open onToggle={() => {}} />
    </ExerciseRationaleProvider>,
  );
  assert.equal(purposeHtml.includes(">Builds</span>Pocket planning."), true);

  // An engine timing rule is not a reason an athlete can act on: when it is the
  // whole purpose, the open row prints no "Builds" line at all.
  const timingHtml = renderToStaticMarkup(
    <ExerciseRationaleProvider>
      <ExerciseRow block={session.blocks![1]!} open onToggle={() => {}} />
    </ExerciseRationaleProvider>,
  );
  assert.equal(timingHtml.includes("D-17"), false);
  assert.equal(timingHtml.includes(">Builds</span>"), false);
});

test("marks the current day and keeps the camp overview title compact", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    event_context: { fight_date: "2026-07-17" },
    red_flag_rules: [
      { rule_id: "rf-1", severity: "red", display_text: "Stop if Achilles pain is high." },
    ],
    plan_notes: [{ category: "injury", label: "Achilles", text: "Watch Achilles load." }],
    weeks: [
      {
        week_id: "wk-1",
        week_index: 4,
        phase_label: "SPP",
        week_goal: "Convert strength into speed.",
        days: [
          {
            date: "2026-06-19",
            day_type: "high",
            countdown_label: "D-28",
            today_card: { headline: "Speed conversion", readiness_status: "train_as_planned" },
            sessions: [
              { session_id: "s1", title: "Lower power", completion_status: "done", blocks: [] },
            ],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} today={new Date(2026, 5, 19)} />);

  // Countdown + the selected week's dates surface in the overview.
  assert.equal(html.includes("D-28"), true);
  assert.equal(html.includes('<h2 class="cm-week-dates">19 Jun'), true);
  assert.equal(html.includes("Convert strength into speed."), false);
  // The day's readiness_status must never leak the exact train/modify/pull-back
  // call — that stays on Today.
  assert.equal(html.includes("Train as planned"), false);
  assert.equal(html.includes("train_as_planned"), false);
  // Current day is flagged, and its completion shows on the day card as the
  // compact success-toned fraction.
  assert.equal(html.includes("cm-day-current"), true);
  assert.equal(html.includes("cm-day-count-done"), true);
  assert.equal(html.includes("1/1"), true);
});

test("compresses the plan: dedupes safety, folds the disclaimer, trims the week overview", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    red_flag_rules: [
      {
        rule_id: "rf-1",
        severity: "red",
        display_text:
          "If weight-cut symptoms worsen (lightheadedness), stop non-essential activity and escalate to coach/medical staff.",
      },
    ],
    plan_notes: [
      // A paraphrase of the red flag (minus the parenthetical) — dropped from Active notes.
      {
        category: "weight_cut",
        label: "Weight cut",
        text: "If weight-cut symptoms worsen, stop non-essential activity and escalate to coach/medical staff.",
      },
      // Genuine context that only shares a phrase with the flag — kept.
      {
        category: "injury",
        label: "Left shoulder contusion",
        text: "Avoid direct contact; rehab drills included each session.",
      },
    ],
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "SPP",
        week_goal: "Build single-leg drive.",
        days: [
          {
            date: "2026-06-19",
            day_type: "moderate",
            sessions: [{ session_id: "s1", title: "Lower", blocks: [] }],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} today={new Date(2026, 5, 19)} />);
  const count = (needle: string) => html.split(needle).length - 1;

  // Constraints and stop actions must be seen before the training prescription.
  assert.ok(html.indexOf("Plan notes") < html.indexOf(">Lower<"));
  assert.ok(html.indexOf("Red flags - stop") < html.indexOf(">Lower<"));

  // The medical disclaimer is folded into the Red Flags card — exactly one
  // safety-note block, no standalone banner duplicate.
  assert.equal(count('class="safety-note '), 1);
  // The Active note that just restated the red flag is dropped; the contextual
  // injury note stays, and the escalation sentence renders once (in Red Flags).
  assert.equal(html.includes("rehab drills included each session"), true);
  assert.equal(count("stop non-essential activity and escalate"), 1);
  // The week overview no longer prints a Phase stat row (phase lives in the
  // status chips and the week pill).
  assert.equal(html.includes(">Phase</span>"), false);
  // Fight-camp overviews head with the selected week's dates.
  assert.equal(html.includes('<h2 class="cm-week-dates">19 Jun'), true);
  assert.equal(count("Build single-leg drive."), 0);
});

test("shows the raw markdown fallback to admins but hides it from athletes", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [{ week_id: "wk-1", week_index: 1, phase_label: "GPP", days: [] }],
    raw_markdown_fallback: "## Original plan\nLegacy text body.",
  } satisfies StructuredPlan;

  const adminHtml = renderToStaticMarkup(
    <StructuredPlanRenderer plan={plan} today={new Date(2030, 0, 1)} isAdmin />,
  );
  assert.equal(adminHtml.includes("Original plan text"), true);
  assert.equal(adminHtml.includes("Legacy text body."), true);
  assert.equal(adminHtml.includes("cm-raw-fallback"), true);

  // Default (athlete) view: the internal raw dump is gone entirely.
  const athleteHtml = renderToStaticMarkup(
    <StructuredPlanRenderer plan={plan} today={new Date(2030, 0, 1)} />,
  );
  assert.equal(athleteHtml.includes("Original plan text"), false);
  assert.equal(athleteHtml.includes("Legacy text body."), false);
  assert.equal(athleteHtml.includes("cm-raw-fallback"), false);
});

test("keeps the raw fallback for athletes in the fail-closed schedule-unavailable state", () => {
  // The on-screen message tells the athlete to read the original plan below, so
  // the raw text must remain reachable there even without admin.
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Open Plan", sport: "boxing", plan_type: "fight_camp" },
    weeks: [{ week_id: "wk-1", week_index: 1, phase_label: "SPP", days: [] }],
    raw_markdown_fallback: "## Original plan\nLegacy text body.",
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={plan}
      openOngoing
      scheduleContext={{ schedule_mode: "open_recurring", projection_status: "unavailable" }}
    />,
  );

  assert.equal(html.includes("Original plan text"), true);
  assert.equal(html.includes("Legacy text body."), true);
});

// NOTE: the earlier "command header" tests (a header rendering "Camp map",
// plan-status colouring, and a "Generated {date}" line from the createdAt /
// planStatus props) were removed with the camp-map redesign — those props are
// intentionally not rendered on this plan view (see the prop doc-comments in
// structured-plan-renderer.tsx). Their tests were deleted rather than kept
// asserting a removed subsystem.

test("does not leak raw enum tokens for day type or session type", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "SPP",
        days: [
          {
            date: "2026-06-19",
            day_type: "high",
            countdown_label: "D-28",
            today_card: { readiness_status: "train_as_planned" },
            sessions: [
              {
                session_id: "s1",
                session_type: "strength_power",
                title: "Lower power",
                blocks: [],
              },
            ],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} today={new Date(2026, 5, 19)} />);

  assert.equal(html.includes("strength_power"), false);
  assert.equal(html.includes("Strength &amp; power"), true);
  // The readiness/"train as planned" tag was removed as bloat — the app owns
  // that decision (it surfaces on Today), so it must not appear in the map at all.
  assert.equal(html.includes("train_as_planned"), false);
  assert.equal(html.includes("Train as planned"), false);
});

test("does not render day intensity tags on session cards", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "TAPER",
        days: [
          {
            date: "2026-06-26",
            day_type: "high",
            countdown_label: "D-6",
            sessions: [
              {
                session_id: "s1",
                session_type: "mixed",
                title: "Fight-Speed Primer",
                blocks: [],
              },
            ],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} today={new Date(2026, 5, 26)} />);

  assert.equal(html.includes(">High</span>"), false);
  assert.equal(html.includes("Mixed session"), true);
});

// Base plan carrying the weight-cut symptom escalation rule/note. `severity` is
// parameterised so we can prove a prominent severity overrides de-emphasis.
function weightCutPlan(severity: string): StructuredPlan {
  return {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    red_flag_rules: [
      {
        rule_id: "rf-weight",
        severity,
        display_text:
          "If weight-cut symptoms worsen (lightheadedness), stop non-essential activity and escalate to coach/medical staff.",
      },
    ],
    plan_notes: [
      {
        category: "weight_cut",
        label: "Weight cut",
        text: "If weight-cut symptoms worsen, stop non-essential activity and escalate to coach/medical staff.",
      },
    ],
    weeks: [{ week_id: "wk-1", week_index: 1, phase_label: "TAPER", days: [] }],
  } satisfies StructuredPlan;
}

function withRiskBand(plan: StructuredPlan, riskBand: string): StructuredPlan {
  return {
    ...plan,
    deterministic_support: {
      nutrition: { by_phase: { TAPER: { weight_cut: { active: true, risk_band: riskBand } } } },
    },
  } as StructuredPlan;
}

test("weight-cut symptom red flag is de-emphasised ONLY when risk is explicitly below moderate", () => {
  // Low risk + non-prominent severity: shown but softened so it does not lead.
  const low = renderToStaticMarkup(
    <StructuredPlanRenderer plan={withRiskBand(weightCutPlan("amber"), "low")} />,
  );
  assert.equal(low.includes("weight-cut symptoms worsen"), true);
  assert.equal(low.includes("sp-redflag-deemphasised"), true);
  // The redundant duplicate copy in the general Active Notes card stays deduped,
  // so the escalation sentence renders exactly once (in Red Flags).
  assert.equal(countOccurrences(low, "stop non-essential activity"), 1);
});

test("weight-cut symptom red flag is NOT de-emphasised at moderate risk", () => {
  // Moderate is not BELOW moderate — it must render at full weight.
  const moderate = renderToStaticMarkup(
    <StructuredPlanRenderer plan={withRiskBand(weightCutPlan("amber"), "moderate")} />,
  );
  assert.equal(moderate.includes("weight-cut symptoms worsen"), true);
  assert.equal(moderate.includes("sp-redflag-deemphasised"), false);
});

test("weight-cut symptom red flag is NOT de-emphasised when risk data is missing", () => {
  // No deterministic_support at all: risk is unknown, never treated as low.
  const missing = renderToStaticMarkup(
    <StructuredPlanRenderer plan={weightCutPlan("amber")} />,
  );
  assert.equal(missing.includes("weight-cut symptoms worsen"), true);
  assert.equal(missing.includes("sp-redflag-deemphasised"), false);
});

test("an explicit red/critical severity rule is never de-emphasised, even below moderate risk", () => {
  // Even at low risk, an explicit high-severity rule overrides de-emphasis.
  for (const severity of ["red", "critical", "high"]) {
    const html = renderToStaticMarkup(
      <StructuredPlanRenderer plan={withRiskBand(weightCutPlan(severity), "low")} />,
    );
    assert.equal(html.includes("weight-cut symptoms worsen"), true);
    assert.equal(
      html.includes("sp-redflag-deemphasised"),
      false,
      `severity "${severity}" must never be faded`,
    );
  }
});

test("a session-less rest day does not render an awkward '0 sessions' tag", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "GPP",
        days: [
          {
            date: "2026-06-21",
            day_type: "rest",
            today_card: {},
            sessions: [],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} today={new Date(2026, 5, 21)} />);

  assert.equal(html.includes("0 session"), false);
});

test("week overview separates app sessions, coach-led days, and app completion", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "SPP",
        days: [
          {
            date: "2026-06-18",
            day_type: "high",
            sessions: [
              {
                session_id: "s1",
                title: "Lower strength",
                completion_status: "done",
                blocks: [],
              },
            ],
          },
          {
            date: "2026-06-19",
            day_type: "moderate",
            today_card: { coach_led_contact: "Coach-led boxing — technical only" },
            sessions: [{ session_id: "s2", title: "Conditioning", blocks: [] }],
          },
          {
            date: "2026-06-20",
            day_type: "high",
            today_card: { headline: "Coach-led boxing session" },
            sessions: [],
          },
          {
            date: "2026-06-21",
            day_type: "high",
            today_card: { headline: "Coach-led sparring" },
            sessions: [],
          },
          {
            date: "2026-06-22",
            day_type: "rest",
            sessions: [],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} today={new Date(2026, 5, 18)} />);

  assert.equal(html.includes(">2 app sessions</span>"), true);
  assert.equal(html.includes(">3 coach days</span>"), true);
  // App completion lives on the week square only, never repeated in the card.
  assert.equal(html.includes("app done"), false);
  assert.equal(html.includes('1/2<span class="sr-only"> sessions completed'), true);
  assert.equal(html.includes("Coach/gym days are tracked separately."), true);
  // The facts sit on one line, not in a grid of bordered stat tiles.
  assert.equal(html.includes("cm-week-overview-stats"), false);
  assert.equal(html.includes("Week overview"), false);
  assert.equal(html.includes("Load</span>"), false);
  assert.equal(html.includes("Training days</span>"), false);
  assert.equal(html.includes("Plan sessions</span>"), false);
  assert.equal(html.includes("Coach/gym combat sessions</span>"), false);
  assert.equal(html.includes(">Completion</span>"), false);
});

test("week overview hides source counters that are not present", () => {
  const renderDays = (
    days: NonNullable<NonNullable<StructuredPlan["weeks"]>[number]["days"]>,
  ): string => {
    const plan: StructuredPlan = {
      schema_version: "1.0",
      plan_metadata: { title: "Fight Camp", sport: "mma", plan_type: "fight_camp" },
      weeks: [
        {
          week_id: "wk-1",
          week_index: 1,
          phase_label: "SPP",
          days,
        },
      ],
    };

    return renderToStaticMarkup(
      <StructuredPlanRenderer plan={plan} today={new Date(2026, 5, 18)} />,
    );
  };

  const appOnlyHtml = renderDays([
    {
      date: "2026-06-18",
      sessions: [{ session_id: "s1", title: "Strength primer", blocks: [] }],
    },
  ]);
  assert.equal(appOnlyHtml.includes(">1 app session</span>"), true);
  assert.equal(appOnlyHtml.includes("app done"), false);
  assert.equal(appOnlyHtml.includes("coach day"), false);

  const coachOnlyHtml = renderDays([
    {
      date: "2026-06-18",
      today_card: { headline: "Coach-led technical session" },
      sessions: [],
    },
  ]);
  assert.equal(coachOnlyHtml.includes(">1 coach day</span>"), true);
  assert.equal(coachOnlyHtml.includes("app session"), false);
  assert.equal(coachOnlyHtml.includes("app done"), false);

  const emptyHtml = renderDays([
    {
      date: "2026-06-18",
      day_type: "rest",
      sessions: [],
    },
  ]);
  assert.equal(emptyHtml.includes("app session"), false);
  assert.equal(emptyHtml.includes("coach day"), false);
  assert.equal(emptyHtml.includes("app done"), false);
});

test("completed work is tagged with the calm success tone, never the brand red accent", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "SPP",
        days: [
          {
            date: "2026-06-19",
            day_type: "high",
            sessions: [
              { session_id: "s1", title: "Lower power", completion_status: "done", blocks: [] },
            ],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} today={new Date(2026, 5, 19)} />);

  assert.equal(html.includes("cm-day-count-done"), true);
  // The "done" completion fraction must not borrow the red accent class.
  assert.equal(/class="[^"]*sp-accent[^"]*"[^>]*>[^<]*1\/1/.test(html), false);
});

test("renders plan-level active notes as a standalone card", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    plan_notes: [
      { category: "weight_cut", label: "Active weight cut", text: "~5.7% target — protect freshness." },
      { category: "injury", text: "Small cut above the elbow — keep covered and dry." },
    ],
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "GPP",
        days: [],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  assert.equal(html.includes("Plan notes"), true);
  assert.equal(html.includes("~5.7% target — protect freshness."), true);
  assert.equal(html.includes("Small cut above the elbow — keep covered and dry."), true);
  // Weight-cut / injury notes get the accent class.
  assert.equal(html.includes("sp-note-weight_cut"), true);
  assert.equal(html.includes("sp-note-injury"), true);
});

// --- open-plan development-block wave ---------------------------------------
//
// Week intent may show an explicitly prescribed deload. Progression advice
// is displayed once in both open-plan and dated-camp exercise cards.

const waveSession = {
  session_id: "ses-wave",
  session_type: "strength_power",
  title: "Support Strength",
  blocks: [
    {
      block_id: "blk-progress",
      display_name: "Trap-bar deadlift",
      progression_rule: "Add 2.5 kg when all sets feel crisp.",
      deload_rule: "Drop to two working sets and keep the bar light.",
    },
    {
      block_id: "blk-stop",
      display_name: "Explosive med-ball throw",
      progression_rule: "Stop when throw speed drops.",
    },
  ],
} satisfies StructuredSession;

test("progression week: block cards display authored progression once and retain stop rules", () => {
  const html = renderToStaticMarkup(
    <SessionCard
      session={waveSession}
      defaultOpenBlocks
      openWeekIntent={openBlockWeekIntent(2)}
    />,
  );

  assert.equal(countOccurrences(html, "This week"), 0);
  assert.equal(countOccurrences(html, "Add 2.5 kg when all sets feel crisp."), 1);
  assert.equal(html.includes(">Progress</span>"), true);
  // The stop-rule block keeps its safety aside and invents no generic bump.
  assert.equal(countOccurrences(html, "Stop when throw speed drops."), 1);
  assert.equal(html.includes(">Stop rule</span>"), true);
  assert.equal(html.includes("only if last week felt controlled"), false);
});

test("deload week: block cards use their own deload rule, never a progression", () => {
  const html = renderToStaticMarkup(
    <SessionCard
      session={waveSession}
      defaultOpenBlocks
      openWeekIntent={openBlockWeekIntent(4)}
    />,
  );

  // Only the block that carries a deload rule gets a week directive; the other
  // block is left alone rather than told to halve sets it does not have.
  assert.equal(countOccurrences(html, "This week"), 1);
  assert.equal(
    countOccurrences(html, "Drop to two working sets and keep the bar light."),
    1,
  );
  assert.equal(html.includes("cut working sets roughly in half"), false);
  assert.equal(html.includes("Add 2.5 kg when all sets feel crisp."), false);
  assert.equal(html.includes(">Stop rule</span>"), true);
});

test("dated camp block cards display authored Progress", () => {
  const html = renderToStaticMarkup(
    <SessionCard session={waveSession} defaultOpenBlocks />,
  );

  assert.equal(html.includes("This week"), false);
  assert.equal(html.includes(">Progress</span>"), true);
  assert.equal(countOccurrences(html, "Add 2.5 kg when all sets feel crisp."), 1);
});

test("open plan week overview headlines the block-week intent", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Open Plan", sport: "boxing", plan_type: "open_ongoing_system" },
    weeks: [
      { week_id: "wk-1", week_index: 1, days: [{ weekday: "Mon", day_type: "moderate", sessions: [] }] },
      { week_id: "wk-2", week_index: 2, days: [] },
      { week_id: "wk-3", week_index: 3, days: [] },
      { week_id: "wk-4", week_index: 4, days: [] },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} openOngoing />);

  // Week 1 is selected by default → baseline intent line in the overview.
  assert.equal(html.includes("Run every dose as written and build consistency."), true);

  const datedHtml = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);
  assert.equal(datedHtml.includes("Run every dose as written"), false);
});

// ---------------------------------------------------------------------------
// Countdown gap fill: missing dates between plan days render as rest rows so
// the D-countdown reads continuous instead of skipping numbers.

function gapFillPlan(
  days: Array<Record<string, unknown>>,
): StructuredPlan {
  return {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [{ week_id: "wk-1", week_index: 1, phase_label: "SPP", days }],
  } as unknown as StructuredPlan;
}

test("buildDayTimeline fills intra-week date holes with countdown rest rows", () => {
  const days = [
    { date: "2026-07-07", countdown_label: "D-10", day_type: "high", sessions: [{ session_id: "s1", blocks: [] }] },
    { date: "2026-07-10", countdown_label: "D-7", day_type: "high", sessions: [{ session_id: "s2", blocks: [] }] },
  ] as unknown as Parameters<typeof buildDayTimeline>[0];

  const timeline = buildDayTimeline(days, true);

  assert.deepEqual(
    timeline.map((entry) => (entry.kind === "gap" ? `${entry.countdown}:${entry.weekday}` : entry.kind)),
    ["day", "D-9:Wed", "D-8:Thu", "day"],
  );
});

test("buildDayTimeline never synthesizes a countdown that contradicts the neighbours", () => {
  // Labels claim a 4-step drop but the dates are only 2 apart → weekday-only rows.
  const days = [
    { date: "2026-07-07", countdown_label: "D-10", day_type: "high", sessions: [] },
    { date: "2026-07-09", countdown_label: "D-6", day_type: "high", sessions: [] },
  ] as unknown as Parameters<typeof buildDayTimeline>[0];

  const timeline = buildDayTimeline(days, true);
  const gaps = timeline.filter((entry) => entry.kind === "gap");

  assert.equal(gaps.length, 1);
  assert.equal(gaps[0].kind === "gap" && gaps[0].countdown, null);
});

test("buildDayTimeline fails closed on missing dates, huge gaps, and disabled mode", () => {
  const noDates = [
    { weekday: "Mon", day_type: "high", sessions: [] },
    { weekday: "Fri", day_type: "high", sessions: [] },
  ] as unknown as Parameters<typeof buildDayTimeline>[0];
  assert.equal(buildDayTimeline(noDates, true).every((entry) => entry.kind === "day"), true);

  const hugeGap = [
    { date: "2026-07-01", countdown_label: "D-20", day_type: "high", sessions: [] },
    { date: "2026-07-11", countdown_label: "D-10", day_type: "high", sessions: [] },
  ] as unknown as Parameters<typeof buildDayTimeline>[0];
  assert.equal(buildDayTimeline(hugeGap, true).every((entry) => entry.kind === "day"), true);

  const dated = [
    { date: "2026-07-07", countdown_label: "D-10", day_type: "high", sessions: [] },
    { date: "2026-07-10", countdown_label: "D-7", day_type: "high", sessions: [] },
  ] as unknown as Parameters<typeof buildDayTimeline>[0];
  assert.equal(buildDayTimeline(dated, false).every((entry) => entry.kind === "day"), true);
});

test("renderer shows synthesized rest rows as inert rows, not accordions", () => {
  const plan = gapFillPlan([
    {
      date: "2026-07-07",
      countdown_label: "D-10",
      day_type: "high",
      sessions: [{ session_id: "s1", title: "Power", blocks: [] }],
    },
    {
      date: "2026-07-10",
      countdown_label: "D-7",
      day_type: "high",
      sessions: [{ session_id: "s2", title: "Conditioning", blocks: [] }],
    },
  ]);

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} today={new Date(2026, 6, 7)} />);

  assert.equal(countOccurrences(html, "cm-rest-day"), 2);
  assert.equal(html.includes("D-9"), true);
  assert.equal(html.includes("D-8"), true);
  // Rest rows are plain divs — the only <details> day rows are the two real days.
  assert.equal(countOccurrences(html, "cm-day-summary"), 2);
  // A synthesized gap has no backend signal, so it must NOT assert "Rest" — it
  // reads the honest neutral label instead.
  assert.equal(html.includes("No planned session"), true);
  assert.equal(html.includes(">Rest</span>"), false);
});

test("an empty day inside fight week reads as deliberate taper rest, not a blank", () => {
  // Inside fight week the session budget caps active work on purpose, so a day
  // with nothing scheduled is planned rest. Showing "No planned session" there
  // made a correct taper look like missing data — the athlete opening the app on
  // that day sees a hole where the reason should be.
  const plan = gapFillPlan([
    {
      date: "2026-07-07",
      countdown_label: "D-5",
      day_type: "moderate",
      sessions: [{ session_id: "s1", title: "Sharpen", blocks: [] }],
    },
    {
      date: "2026-07-10",
      countdown_label: "D-2",
      day_type: "low",
      sessions: [{ session_id: "s2", title: "Primer", blocks: [] }],
    },
  ]);

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} today={new Date(2026, 6, 7)} />);

  // D-4 and D-3 are unprogrammed taper days: named, and given the reason.
  assert.equal(countOccurrences(html, "cm-rest-day"), 2);
  assert.equal(html.includes("D-4"), true);
  assert.equal(html.includes("D-3"), true);
  assert.equal(countOccurrences(html, "Taper rest"), 2);
  assert.equal(html.includes("Planned rest to protect freshness for fight day."), true);
  assert.equal(html.includes("No planned session"), false);
});

test("an empty day outside fight week keeps the honest neutral label", () => {
  // Further out a hole could be rest, coach-led work, or undocumented, so the
  // renderer must still not assert a reason it cannot back.
  const plan = gapFillPlan([
    {
      date: "2026-07-07",
      countdown_label: "D-14",
      day_type: "high",
      sessions: [{ session_id: "s1", title: "Power", blocks: [] }],
    },
    {
      date: "2026-07-09",
      countdown_label: "D-12",
      day_type: "moderate",
      sessions: [{ session_id: "s2", title: "Skill", blocks: [] }],
    },
  ]);

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} today={new Date(2026, 6, 7)} />);

  assert.equal(html.includes("No planned session"), true);
  assert.equal(html.includes("Taper rest"), false);
});

test("a backend-classified rest day keeps the definite 'Rest' label", () => {
  const plan = gapFillPlan([
    {
      date: "2026-07-07",
      countdown_label: "D-10",
      day_type: "high",
      sessions: [{ session_id: "s1", title: "Power", blocks: [] }],
    },
    {
      date: "2026-07-08",
      countdown_label: "D-9",
      day_type: "rest",
      today_card: {},
      sessions: [],
    },
  ]);

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} today={new Date(2026, 6, 7)} />);

  // The explicit rest day (day_type "rest") is a compact rest row labelled "Rest".
  assert.equal(html.includes(">Rest</span>"), true);
  assert.equal(html.includes("No planned session"), false);
});

test("today remains highlighted when next-session focus points to a future day", () => {
  const plan = gapFillPlan([
    {
      date: "2026-07-07",
      countdown_label: "D-10",
      day_type: "high",
      sessions: [{ session_id: "s1", title: "Power", blocks: [] }],
    },
    {
      date: "2026-07-10",
      countdown_label: "D-7",
      day_type: "high",
      sessions: [{ session_id: "s2", title: "Conditioning", blocks: [] }],
    },
  ]);

  // Today (the 8th) is a gap day, but the view has advanced to a future session
  // (focusDay + "Next session"). The future session card owns the marker; the
  // gap row must stay plain rather than stamping today with "Next session".
  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={plan}
      today={new Date(2026, 6, 8)}
      currentTrainingDayIso="2026-07-08"
      focusDay={new Date(2026, 6, 10)}
      currentDayLabel="Next session"
    />,
  );

  // Today remains the calendar anchor even when Today has no physical work.
  assert.equal(html.includes("cm-rest-day cm-day-current"), true);
});

test("a synthesized rest row on the athlete's current day is highlighted", () => {
  const plan = gapFillPlan([
    {
      date: "2026-07-07",
      countdown_label: "D-10",
      day_type: "high",
      sessions: [{ session_id: "s1", title: "Power", blocks: [] }],
    },
    {
      date: "2026-07-10",
      countdown_label: "D-7",
      day_type: "high",
      sessions: [{ session_id: "s2", title: "Conditioning", blocks: [] }],
    },
  ]);

  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={plan}
      today={new Date(2026, 6, 8)}
      currentTrainingDayIso="2026-07-08"
    />,
  );

  assert.equal(html.includes("cm-rest-day cm-day-current"), true);
});

// ---------------------------------------------------------------------------
// Week progression rail: equal-fill for short plans, readable scroll for long
// ones, plus the pure centring math for bringing the active week into view.

function weeksPlan(count: number, firstPhase?: string): StructuredPlan {
  return {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: Array.from({ length: count }, (_, i) => ({
      week_id: `wk-${i + 1}`,
      week_index: i + 1,
      phase_label: i === 0 ? firstPhase ?? "GPP" : i === count - 1 ? "TAPER" : "SPP",
      days: [],
    })),
  } as unknown as StructuredPlan;
}

test("week rail fills the width without scrolling for one to four weeks", () => {
  for (const count of [1, 2, 3, 4]) {
    const html = renderToStaticMarkup(<StructuredPlanRenderer plan={weeksPlan(count)} />);
    assert.equal(
      countOccurrences(html, 'data-week-pos="'),
      count,
      `expected ${count} week cards`,
    );
    assert.equal(
      html.includes('data-scroll="true"'),
      false,
      `${count} weeks should share the width, not scroll`,
    );
  }
});

test("week rail scrolls once there are more than four weeks", () => {
  for (const count of [5, 8, 16]) {
    const html = renderToStaticMarkup(<StructuredPlanRenderer plan={weeksPlan(count)} />);
    assert.equal(
      countOccurrences(html, 'data-week-pos="'),
      count,
      `expected ${count} week cards`,
    );
    assert.equal(
      html.includes('data-scroll="true"'),
      true,
      `${count} weeks should scroll`,
    );
  }
});

test("D-10 countdown plans render taper mini-titles with a compact week overview", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Late Fight", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        countdown_start: "D-10",
        countdown_end: "D-5",
        days: [{ date: "2026-07-27", countdown_label: "D-10", sessions: [] }],
      },
      {
        week_id: "wk-2",
        week_index: 2,
        countdown_start: "D-4",
        countdown_end: "D-0",
        days: [{ date: "2026-08-02", countdown_label: "D-4", sessions: [] }],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(
    <StructuredPlanRenderer plan={plan} today={new Date(2026, 6, 27)} />,
  );

  // The squares stay bare; the selected week's overview names the phase once.
  assert.equal(countOccurrences(html, 'class="cm-week-pill-phase"'), 0);
  assert.equal(countOccurrences(html, 'title="Taper"'), 1);
  assert.equal(html.includes('<h2 class="cm-week-dates">27 Jul'), true);
  assert.equal(html.includes("Week 1: Compressed Pre-Fight Week"), false);
});

test("next-session focus marks the active week and day before a dated camp starts", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Late Fight", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "SPP",
        days: [
          {
            date: "2026-07-27",
            countdown_label: "D-10",
            sessions: [{ session_id: "s1", title: "Power Transfer Touch", blocks: [] }],
          },
        ],
      },
      {
        week_id: "wk-2",
        week_index: 2,
        phase_label: "TAPER",
        days: [
          {
            date: "2026-08-03",
            countdown_label: "D-3",
            sessions: [{ session_id: "s2", title: "Primer", blocks: [] }],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={plan}
      today={new Date(2026, 6, 26)}
      focusDay={new Date(2026, 6, 27)}
      currentDayLabel="Next session"
    />,
  );

  assert.equal(countOccurrences(html, "cm-week-pill-current"), 1);
  assert.equal(countOccurrences(html, "cm-week-pill-dot"), 1);
  assert.equal(countOccurrences(html, "cm-day-current"), 1);
  assert.equal(countOccurrences(html, "cm-day-now"), 1);
  assert.equal(
    html.includes("cm-week-pill cm-week-pill-selected cm-week-pill-current"),
    true,
  );
});

test("a long phase label retains its full text and accessible title", () => {
  const longPhase = "Accumulation Overreaching Realisation Block";
  const plan = weeksPlan(3, longPhase);
  const expected = formatPlanLabel(longPhase);

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  // Wrapping is handled by CSS; the full label remains available in both the
  // text node and the title attribute.
  assert.equal(html.includes(`title="${expected}"`), true);
  assert.equal(html.includes(`<span class="sp-tag sp-accent" title="${expected}">${expected}</span>`), true);
});

test("weekStripCenterOffset centres the active card and clamps at the start", () => {
  // Mid-rail card is centred within the viewport width.
  assert.equal(weekStripCenterOffset(300, 500, 80), 500 - (300 - 80) / 2);
  // A card near the start never produces a negative scroll offset.
  assert.equal(weekStripCenterOffset(300, 10, 80), 0);
  // The very first card sits flush at 0.
  assert.equal(weekStripCenterOffset(300, 0, 80), 0);
});

// --- jargon glossary "i" affordances ---------------------------------------

/** The aria-labels of every glossary "i" in a rendered markup string. */
function glossaryTerms(html: string): string[] {
  return Array.from(html.matchAll(/aria-label="What ([^"]+) means"/g)).map((match) => match[1]);
}

/** A one-block session whose ONLY glossable stat is the effort prescription, so
 * the assertions below read the effort tooltip and nothing else. */
function effortSession(effort: unknown): StructuredSession {
  return {
    session_id: "ses-effort",
    session_type: "strength_power",
    title: "Trap Bar Jump",
    blocks: [
      {
        block_id: "jump",
        block_type: "power",
        display_name: "Trap Bar Jump",
        effort,
      },
    ],
  } as unknown as StructuredSession;
}

test("a non-RPE effort method is glossed as itself, never as the RPE scale", () => {
  // The blocker: EffortMethod also covers RIR, intent, velocity, heart_rate_zone,
  // pace and max_effort_percent. A fixed RPE tooltip would have told an athlete
  // that "Max intent" is a 10-out-of-10 perceived-exertion score.
  const html = renderToStaticMarkup(
    <SessionCard session={effortSession({ method: "intent", value: "max" })} defaultOpenBlocks />,
  );

  assert.equal(html.includes("Max intent"), true);
  assert.deepEqual(glossaryTerms(html), ["Intent"]);
  assert.equal(html.includes("hard but controlled"), false);
});

test("every EffortMethod in the schema is glossed with its own definition", () => {
  // Mirrors EffortMethod in api/structured_plan_models.py. If the backend adds a
  // method, this fails until the glossary covers it.
  const expected: Array<[string, string]> = [
    ["RPE", "RPE"],
    ["RIR", "RIR"],
    ["intent", "Intent"],
    ["velocity", "Velocity"],
    ["heart_rate_zone", "Heart rate zone"],
    ["pace", "Pace"],
    ["max_effort_percent", "Max effort %"],
  ];

  for (const [method, term] of expected) {
    const html = renderToStaticMarkup(
      <SessionCard session={effortSession({ method, value: 5 })} defaultOpenBlocks />,
    );
    assert.deepEqual(glossaryTerms(html), [term], `${method} should gloss as ${term}`);
  }
});

test("the compact coach cue stays unglossed, so it cannot borrow an effort definition", () => {
  // The coaching card deliberately renders no "i" at all; this pins that,
  // because glossing it from the label would explain a mental cue as bar speed.
  const session = {
    session_id: "ses-mindset",
    session_type: "mobility",
    title: "Mobility Reset Flow",
    mindset_anchor: {
      intent: "relax the system and move gently",
      focus_cue: "slow thoracic mobility",
    },
    blocks: [],
  } as unknown as StructuredSession;

  const html = renderToStaticMarkup(<SessionCard session={session} defaultOpenBlocks />);

  assert.equal(html.includes(">Coach cue</span>"), true);
  assert.deepEqual(glossaryTerms(html), []);
});

test("an unrecognised effort method shows no tooltip rather than a wrong one", () => {
  // Fail-safe: the renderer is defensive about payloads, so an unknown method
  // (or a missing one) must stay silent instead of guessing a scale.
  for (const effort of [
    { method: "vibes", value: 5 },
    { method: "", value: 5 },
    { value: "max" },
  ]) {
    const html = renderToStaticMarkup(
      <SessionCard session={effortSession(effort)} defaultOpenBlocks />,
    );
    assert.deepEqual(glossaryTerms(html), [], `${JSON.stringify(effort)} should gloss nothing`);
  }
});

test("the effort stat carries an i that explains the RPE scale", () => {
  // The card prints "RPE 1.5" with no scale attached — the number is meaningless
  // to an athlete who has never met the RPE scale.
  const session = {
    session_id: "ses-mobility",
    session_type: "mobility",
    title: "Mobility Reset Flow",
    blocks: [
      {
        block_id: "mobility",
        block_type: "mobility",
        display_name: "Mobility Reset Flow",
        duration: { value: 8, unit: "minutes" },
        effort: { method: "RPE", value: 1.5 },
      },
    ],
  } as unknown as StructuredSession;

  const html = renderToStaticMarkup(<SessionCard session={session} defaultOpenBlocks />);

  assert.equal(html.includes("RPE 1.5"), true);
  assert.equal(glossaryTerms(html).includes("RPE"), true);
});

test("plain-English stat labels get no ?, so the ones that matter stand out", () => {
  const session = {
    session_id: "ses-cond",
    session_type: "conditioning",
    title: "Bike Intervals",
    blocks: [
      {
        block_id: "bike",
        block_type: "conditioning",
        display_name: "Bike Intervals",
        duration: { value: 20, unit: "minutes" },
        distance: { value: 5, unit: "km" },
        rounds: 6,
        work: { value: 30, unit: "seconds" },
        rest: { value: 90, unit: "seconds" },
      },
    ],
  } as unknown as StructuredSession;

  const html = renderToStaticMarkup(<SessionCard session={session} defaultOpenBlocks />);

  // Duration / Distance / Rounds / Work / Rest all render, none is glossed.
  assert.equal(html.includes(">Rounds</span>"), true);
  assert.equal(html.includes(">Rest</span>"), true);
  assert.deepEqual(glossaryTerms(html), []);
});

test("volume, load and a rehab tag are glossed on the block card", () => {
  const session = {
    session_id: "ses-strength",
    session_type: "strength_power",
    title: "Rear-Foot Elevated Split Squat",
    blocks: [
      {
        block_id: "rfess",
        block_type: "rehab",
        display_name: "Rear-Foot Elevated Split Squat",
        sets: 3,
        reps: "8",
        load: { display: "60% 1RM" },
        effort: { method: "RPE", value: 7 },
        progression_rule: "Stop the set if knee pain rises above 3/10.",
      },
    ],
  } as unknown as StructuredSession;

  const html = renderToStaticMarkup(<SessionCard session={session} defaultOpenBlocks />);
  const terms = glossaryTerms(html);

  assert.equal(terms.includes("Volume"), true);
  assert.equal(terms.includes("Load"), true);
  assert.equal(terms.includes("RPE"), true);
  assert.equal(terms.includes("Rehab"), true);
  assert.equal(terms.includes("Stop rule"), true);
});

test("AMRAP-style modes are glossed rather than left as bare acronyms", () => {
  const session = {
    session_id: "ses-amrap",
    session_type: "conditioning",
    title: "Circuit",
    blocks: [
      {
        block_id: "circuit",
        block_type: "conditioning",
        display_name: "Circuit",
        reps: "AMRAP",
      },
    ],
  } as unknown as StructuredSession;

  const html = renderToStaticMarkup(<SessionCard session={session} defaultOpenBlocks />);

  assert.equal(html.includes(">Mode</span>"), true);
  assert.equal(glossaryTerms(html).includes("Mode"), true);
});

test("a cleared region's Prehab summary is glossed with the prehab definition", () => {
  const html = renderToStaticMarkup(
    <StructuredPlanRenderer
      plan={hamstringRehabPlan()}
      rehabLabelPolicy={QUAD_OPEN_HAMSTRING_CLEARED}
    />,
  );

  assert.equal(/class="ex-row-type">Prehab(?:<|\s)/.test(html), true);
  assert.equal(/class="ex-row-type">Prehab(?:<|\s)/.test(html), true);
});

test("the week overview explains the selected week's camp phase", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "SPP",
        days: [
          {
            date: "2026-06-20",
            countdown_label: "D-20",
            day_type: "rest",
            today_card: {},
            sessions: [],
          },
        ],
      },
    ],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  assert.equal(html.includes("cm-week-phase"), true);
  assert.equal(glossaryTerms(html).includes("SPP"), true);
});

test("the effort stat reads in plain words, never as a raw enum token", () => {
  // Reported on the plan card: "intent max_speed" under Effort.
  const html = renderToStaticMarkup(
    <SessionCard session={effortSession({ method: "intent", value: "max_speed" })} defaultOpenBlocks />,
  );
  assert.equal(html.includes("Max speed"), true);
  assert.equal(html.includes("max_speed"), false);
  assert.equal(html.includes("intent max"), false);
  assert.deepEqual(glossaryTerms(html), ["Intent"]);
});

test("plan page shows no recovery or nutrition section, even for legacy plans", () => {
  const plan = {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    nutrition: {
      summary: "Eat to fuel training.",
      daily_focus: "Protein at every meal.",
      training_day_guidance: "",
      fight_week_guidance: "",
    },
    deterministic_support: {
      nutrition: { by_phase: { GPP: { protein_g_per_day: { min: 140, max: 170 } } } },
      recovery: {
        by_phase: {
          GPP: {
            sleep_hours_target: [8, 9],
            core_strategies: ["Daily breathwork (5-10 min post-session)"],
          },
        },
      },
    },
    weeks: [{ week_id: "wk-1", week_index: 1, phase_label: "GPP", days: [] }],
  } satisfies StructuredPlan;

  const html = renderToStaticMarkup(<StructuredPlanRenderer plan={plan} />);

  assert.equal(html.includes("Daily breathwork"), false);
  assert.equal(html.includes("Eat to fuel training."), false);
  assert.equal(html.includes("sp-recovery"), false);
  assert.equal(html.includes("sp-nutrition"), false);
});


test("technical drills explain their sequence separately from the one coach cue", () => {
  const instruction = "One feint at range, one rapid entry, one shot, then clean exit - no second entry.";
  const html = renderToStaticMarkup(<ExerciseRow block={{
    display_name: "Range Feint-Entry-Exit", block_type: "skill",
    execution_instructions: instruction,
    coaching_cues: ["Keep punches balanced; return to guard"],
    rounds: 1, work: {value: 5, unit: "sec"}, rest: {value: 120, unit: "sec"},
  }} open onToggle={() => {}} />);
  assert.equal(countOccurrences(html, instruction), 1);
  assert.equal(countOccurrences(html, "Keep punches balanced; return to guard"), 1);
  assert.equal(html.includes(">How</span>"), true);
  assert.equal(html.includes("View steps"), false);
  assert.equal(html.includes(">Progress</span>"), false);
});

test("a written-out 'none this block' Progress and a dose-only How are not shown", () => {
  const html = renderToStaticMarkup(<BlockCard block={{
    display_name: "Lateral Bound Intervals", block_type: "conditioning",
    sets: 6, duration: { value: 20, unit: "seconds" }, rest: { value: 100, unit: "seconds" },
    execution_instructions: "6 x 20 sec work", progression_rule: "none this block.",
    coaching_cues: ["Soft landing, quick reset."], stop_rules: ["Stop if knee gives out."],
  }} />);
  assert.equal(html.includes(">Progress</span>"), false);
  assert.equal(html.includes(">How</span>"), false);
  assert.doesNotMatch(html, /none this block/);
  const drill = renderToStaticMarkup(<BlockCard block={{
    display_name: "Pull-Counter-Reset", block_type: "skill", sets: 1, duration: { value: 5, unit: "seconds" },
    execution_instructions: "1x5s with 120s rest\nDraw one attack, take a small pull, then restore stance.",
  }} />);
  assert.match(drill, />How<\/span>Draw one attack/);
  assert.doesNotMatch(drill, /1x5s with 120s rest/);
});

// The same instruction component serves expanded cards and exercise sheets.
test("How is shown only when the exercise has no demo video", async () => {
  const { ExerciseMediaProvider } = await import("./exercise-demo");
  const block = { block_type: "strength", display_name: "Squat", exercise_key: "squat",
    execution_instructions: "Brace and lower under control." };
  const plain = renderToStaticMarkup(<BlockCard block={block} />);
  assert.match(plain, /Brace and lower under control/);
  const curated = renderToStaticMarkup(
    <ExerciseMediaProvider media={{ "exercise:squat": { provider: "youtube", video_id: "hQgFixeXdZo", start_s: 0, end_s: null, source: "curated" } }}>
      <BlockCard block={block} />
    </ExerciseMediaProvider>,
  );
  assert.doesNotMatch(curated, /ex-execution-instructions/);
  for (const video of [{ private_media_id: "private-video" }, { community_revision_id: "community-video" }]) {
    const html = renderToStaticMarkup(<BlockCard block={{ ...block, ...video }} />);
    assert.doesNotMatch(html, /ex-execution-instructions/);
  }
});

test("legacy raw day and session labels do not reach the athlete renderer", () => {
  const day = { today_card: { headline: "technical_combat" }, sessions: [] };
  const headline = renderToStaticMarkup(<TodayCard day={day as never} />);
  assert.match(headline, /Technical Combat/);
  assert.doesNotMatch(headline, /technical_combat/);

  const session = renderToStaticMarkup(<SessionCard session={{
    session_id: "legacy", title: "technical_combat", session_type: "skill", blocks: [],
  } as never} />);
  assert.match(session, /Technical Combat/);
  assert.doesNotMatch(session, /technical_combat/);
});
