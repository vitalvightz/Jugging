import test from "node:test";
import assert from "node:assert/strict";

import {
  athleteTrainingDayISO,
  buildCompletionIndex,
  canRetroLog,
  completionForSession,
  completionSessionId,
  dayCompletion,
  deriveCountdownLabel,
  findDayByISO,
  getReadinessStrip,
  resolveCurrentDay,
  resolveNextPlanFocusDay,
  resolveOpenPlanWeekNumber,
  resolvePlanProgress,
  resolveTrainingDay,
  getSessionDisplayStatus,
  primarySessionOf,
  sessionIdentity,
  toISODate,
  weekCompletion,
  weekLoadProxy,
  weekSessionSummary,
} from "./camp-map.ts";
import type { StructuredPlan, TodaySessionCompletionRecord } from "@/lib/types";

test("next focus needs no Today response and skips neutral days and live completed sessions", () => {
  const plan: StructuredPlan = { weeks: [{ days: [
    { date: "2026-10-26", day_type: "rest", sessions: [] },
    { date: "2026-10-27", sessions: [{ session_id: "done", title: "Strength" }] },
    { date: "2026-10-28", day_type: "rest", sessions: [], today_card: { headline: "Unavailable" } },
    { date: "2026-10-29", sessions: [], today_card: { headline: "Hard sparring", coach_led_contact: "Hard sparring" } },
    { date: "2026-10-30", sessions: [{ session_id: "later", title: "Conditioning" }] },
  ] }] };
  const completionIndex = buildCompletionIndex([{ session_id: "done", training_day: "2026-10-27", status: "done" } as TodaySessionCompletionRecord]);
  assert.equal(toISODate(resolveNextPlanFocusDay(plan, new Date(2026, 9, 26), undefined, { completionIndex })!), "2026-10-29");
  assert.equal(toISODate(resolveNextPlanFocusDay(plan, new Date(2026, 9, 25), undefined)!), "2026-10-27");
  assert.equal(resolveNextPlanFocusDay(plan, new Date(2026, 10, 1), undefined), undefined);
});

function campPlan(): StructuredPlan {
  return {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    event_context: { fight_date: "2026-07-17" },
    red_flag_rules: [
      { rule_id: "rf-1", severity: "red", display_text: "Stop if Achilles pain ≥ 6/10." },
    ],
    plan_notes: [
      { category: "weight_cut", label: "Active weight cut", text: "Cut ~3.5%; recovery tolerance reduced." },
      {
        category: "injury",
        label: "Left shoulder contusion",
        text: "Avoid contact; stop on sharp pain or new swelling.",
      },
    ],
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "SPP",
        week_goal: "Convert strength into speed.",
        days: [
          {
            date: "2026-06-18",
            day_type: "high",
            countdown_label: "D-29",
            sessions: [
              { session_id: "s1", title: "Lower power", completion_status: "done", blocks: [] },
              { session_id: "s2", title: "Sprint", completion_status: "not_started", blocks: [] },
            ],
          },
          {
            date: "2026-06-19",
            day_type: "moderate",
            countdown_label: "D-28",
            today_card: {
              headline: "Speed conversion",
              readiness_status: "train_as_planned",
              primary_warning: "Achilles still tender — keep contacts short.",
            },
            sessions: [
              { session_id: "s3", title: "Upper strength", completion_status: "done", blocks: [] },
            ],
          },
        ],
      },
      {
        week_id: "wk-2",
        week_index: 2,
        phase_label: "TAPER",
        week_goal: "Sharpen and freshen.",
        days: [{ date: "2026-06-25", day_type: "low", sessions: [] }],
      },
    ],
  } satisfies StructuredPlan;
}

test("toISODate formats a Date as a local YYYY-MM-DD string", () => {
  assert.equal(toISODate(new Date(2026, 5, 19)), "2026-06-19");
  assert.equal(toISODate(new Date(2026, 0, 3)), "2026-01-03");
});

test("resolvePlanProgress locates today's week, day and countdown", () => {
  const progress = resolvePlanProgress(campPlan(), new Date(2026, 5, 19));
  assert.equal(progress.weekCount, 2);
  assert.equal(progress.currentWeekPos, 0);
  assert.equal(progress.currentDayDate, "2026-06-19");
  assert.equal(progress.dLabel, "D-28");
});

test("resolvePlanProgress falls back to a derived countdown when out of range", () => {
  const progress = resolvePlanProgress(campPlan(), new Date(2026, 5, 1));
  assert.equal(progress.currentWeekPos, null);
  assert.equal(progress.currentDayDate, null);
  // 2026-07-17 minus 2026-06-01 = 46 days.
  assert.equal(progress.dLabel, "D-46");
});

test("resolvePlanProgress tolerates an empty/missing plan", () => {
  const progress = resolvePlanProgress(undefined, new Date(2026, 5, 19));
  assert.equal(progress.weekCount, 0);
  assert.equal(progress.currentWeekPos, null);
  assert.equal(progress.dLabel, null);
});

test("deriveCountdownLabel returns D-0 on event day and null after it", () => {
  assert.equal(deriveCountdownLabel(campPlan(), new Date(2026, 6, 17)), "D-0");
  assert.equal(deriveCountdownLabel(campPlan(), new Date(2026, 6, 18)), null);
});

test("weekCompletion and dayCompletion count done sessions", () => {
  const plan = campPlan();
  const week1 = plan.weeks![0]!;
  assert.deepEqual(weekCompletion(week1), { done: 2, total: 3 });
  assert.deepEqual(dayCompletion(week1.days![0]!), { done: 1, total: 2 });
  assert.deepEqual(dayCompletion(week1.days![1]!), { done: 1, total: 1 });
});

test("weekSessionSummary separates app sessions from coach-led days", () => {
  const week = {
    days: [
      {
        date: "2026-06-18",
        sessions: [{ session_id: "s1", title: "Lower strength", blocks: [] }],
      },
      {
        date: "2026-06-19",
        today_card: { coach_led_contact: "Coach-led boxing — technical only" },
        sessions: [{ session_id: "s2", title: "Conditioning", blocks: [] }],
      },
      {
        date: "2026-06-20",
        today_card: { headline: "Coach-led boxing session" },
        sessions: [],
      },
      {
        date: "2026-06-21",
        today_card: { headline: "Coach-led sparring" },
        sessions: [],
      },
      {
        date: "2026-06-22",
        day_type: "rest",
        sessions: [],
      },
    ],
  };

  assert.deepEqual(weekSessionSummary(week), {
    trainingDays: 4,
    appSessions: 2,
    supportSessions: 0,
    coachLedSessions: 3,
  });
});

// A "high" day_type badge is not enough on its own: the third day here is
// coach-led technical work, which scores as a light touch regardless of its
// badge, so the week lands Moderate rather than High.
test("weekLoadProxy scores weekly burden instead of the hardest single day", () => {
  const week = {
    days: [
      {
        date: "2026-07-10",
        day_type: "high",
        sessions: [{ session_id: "s1", title: "Power touch", blocks: [] }],
      },
      {
        date: "2026-07-12",
        day_type: "low",
        sessions: [{ session_id: "s2", title: "Mobility and speed reset", blocks: [] }],
      },
      {
        date: "2026-07-14",
        day_type: "high",
        today_card: { headline: "Technical rhythm only — no hard sparring" },
        sessions: [],
      },
    ],
  };

  assert.equal(weekLoadProxy(week), "Moderate");
});

test("weekLoadProxy still returns High for repeated hard weekly stress", () => {
  const week = {
    days: [
      {
        date: "2026-06-18",
        day_type: "high",
        sessions: [{ session_id: "s1", title: "Hard conditioning", blocks: [] }],
      },
      {
        date: "2026-06-20",
        day_type: "high",
        sessions: [{ session_id: "s2", title: "Power and sprint session", blocks: [] }],
      },
      {
        date: "2026-06-22",
        today_card: { headline: "Coach-led sparring" },
        sessions: [],
      },
    ],
  };

  assert.equal(weekLoadProxy(week), "High");
});

test("weekLoadProxy does not count filler or mobility sessions as high-load days", () => {
  const week = {
    phase_label: "SPP",
    days: [
      {
        day_type: "high",
        sessions: [{ session_type: "skill", title: "Mobility and low-noise speed", blocks: [] }],
      },
      {
        day_type: "high",
        sessions: [{ session_type: "skill", title: "Tactical Cue Card", blocks: [] }],
      },
      {
        day_type: "high",
        sessions: [{ session_type: "recovery", title: "Recovery Reset", blocks: [] }],
      },
      {
        day_type: "high",
        sessions: [{ session_type: "primer", title: "Joint Prep", blocks: [] }],
      },
      {
        day_type: "high",
        sessions: [
          {
            session_type: "skill",
            title: "Movement quality",
            blocks: [{ block_type: "mobility_activation", display_name: "Hip flow" }],
          },
        ],
      },
    ],
  };

  assert.equal(weekLoadProxy(week), "Moderate");
});

test("weekLoadProxy keeps loaded blocks load-bearing when a generic session title mentions mobility", () => {
  const week = {
    phase_label: "SPP",
    days: ["2026-07-10", "2026-07-12", "2026-07-14"].map((date, index) => ({
      date,
      day_type: "low",
      sessions: [
        {
          session_id: `loaded-mobility-${index + 1}`,
          session_type: "skill",
          title: "Mobility and acceleration",
          blocks: [{ block_type: "speed_acceleration", display_name: "Acceleration work" }],
        },
      ],
    })),
  };

  assert.equal(weekLoadProxy(week), "Moderate");
});

test("weekLoadProxy caps taper weeks at Moderate", () => {
  const week = {
    phase_label: "TAPER",
    days: [
      {
        day_type: "high",
        sessions: [{ session_type: "conditioning", title: "Hard conditioning", blocks: [] }],
      },
      {
        day_type: "high",
        sessions: [{ session_type: "strength_power", title: "Power and sprint session", blocks: [] }],
      },
      {
        day_type: "high",
        today_card: { headline: "Coach-led sparring" },
        sessions: [],
      },
    ],
  };

  assert.equal(weekLoadProxy(week), "Moderate");
});

test("weekLoadProxy still counts coach-led hard contact when a filler shares the day", () => {
  const week = {
    phase_label: "SPP",
    days: [
      {
        day_type: "high",
        today_card: { coach_led_contact: "Coach-led sparring" },
        sessions: [{ session_type: "recovery", title: "Recovery Reset", blocks: [] }],
      },
      {
        day_type: "high",
        today_card: { coach_led_contact: "Coach-led sparring" },
        sessions: [{ session_type: "skill", title: "Tactical Cue Card", blocks: [] }],
      },
    ],
  };

  assert.equal(weekLoadProxy(week), "High");
});

// Fixtures for dose-aware fatigue: intensity labels stay "high", but fatigue
// follows the prescription (bouts, work time, rest, effort).
const bagBurstSession = (id: string) => ({
  session_id: id,
  session_type: "strength_power",
  title: "Max-intent power bursts",
  blocks: [
    {
      block_type: "power",
      display_name: "Heavy bag bursts",
      sets: 7,
      work: { value: 5, unit: "seconds" },
      rest: { value: 60, unit: "seconds" },
      effort: { method: "RPE", value: 9 },
      intensity: "max",
    },
  ],
});

const conditioningRoundsSession = (id: string) => ({
  session_id: id,
  session_type: "conditioning",
  title: "Hard conditioning rounds",
  blocks: [
    {
      block_type: "conditioning",
      display_name: "Bag conditioning rounds",
      rounds: 4,
      work: { value: 3, unit: "minutes" },
      rest: { value: 1, unit: "minutes" },
      effort: { method: "RPE", value: 8 },
    },
  ],
});

const hardSparringSession = (id: string) => ({
  session_id: id,
  session_type: "sparring",
  title: "Hard sparring",
  blocks: [
    { block_type: "sparring", display_name: "Sparring", rounds: 6, work: { value: 3, unit: "min" }, intensity: "hard" },
  ],
});

const easyBikeSession = (id: string) => ({
  session_id: id,
  session_type: "conditioning",
  title: "Aerobic bike",
  blocks: [
    {
      block_type: "conditioning",
      display_name: "Easy bike",
      duration: { value: 30, unit: "minutes" },
      effort: { method: "RPE", value: 4 },
    },
  ],
});

test("weekLoadProxy does not treat short explosive bursts like prolonged conditioning", () => {
  const burstWeek = {
    phase_label: "SPP",
    days: [
      { day_type: "high", sessions: [bagBurstSession("burst-1")] },
      { day_type: "high", sessions: [bagBurstSession("burst-2")] },
    ],
  };
  const conditioningWeek = {
    phase_label: "SPP",
    days: [
      { day_type: "high", sessions: [conditioningRoundsSession("cond-1")] },
      { day_type: "high", sessions: [conditioningRoundsSession("cond-2")] },
    ],
  };

  // Same "high" labels and same number of days; only the dose differs.
  assert.equal(weekLoadProxy(burstWeek), "Low");
  assert.equal(weekLoadProxy(conditioningWeek), "High");
});

test("weekLoadProxy does not assume unstated rest is generous recovery", () => {
  const burstsWithoutRest = (id: string) => {
    const session = bagBurstSession(id);
    const { rest: _rest, ...block } = session.blocks[0]!;
    return { ...session, blocks: [block] };
  };
  const week = {
    phase_label: "SPP",
    days: [
      { day_type: "high", sessions: [burstsWithoutRest("burst-1")] },
      { day_type: "high", sessions: [burstsWithoutRest("burst-2")] },
    ],
  };

  // With 60 s rest stated this week is Low; unknown rest scores each day
  // moderate instead of low.
  assert.equal(weekLoadProxy(week), "Moderate");
});

test("weekLoadProxy reads a long block duration as the whole block, not per round", () => {
  const roundsOverBlockDuration = (id: string) => ({
    session_id: id,
    session_type: "conditioning",
    title: "Bag rounds",
    blocks: [
      {
        block_type: "conditioning",
        display_name: "Bag rounds",
        rounds: 3,
        duration: { value: 330, unit: "seconds" },
        effort: { method: "RPE", value: 8 },
      },
    ],
  });
  const week = {
    phase_label: "SPP",
    days: [
      { day_type: "moderate", sessions: [roundsOverBlockDuration("rounds-1")] },
      { day_type: "moderate", sessions: [roundsOverBlockDuration("rounds-2")] },
    ],
  };

  // 5.5 minutes of RPE 8 work in total, not 3 x 5.5 = 16.5 minutes. Read per
  // round it would score both days high and the week High.
  assert.equal(weekLoadProxy(week), "Moderate");
});

test("weekLoadProxy reads RIR 0 as maximal effort", () => {
  const roundsAtRir = (id: string, rir: number) => ({
    session_id: id,
    session_type: "conditioning",
    title: "Bag rounds",
    blocks: [
      {
        block_type: "conditioning",
        display_name: "Bag rounds",
        rounds: 4,
        work: { value: 90, unit: "seconds" },
        rest: { value: 60, unit: "seconds" },
        effort: { method: "RIR", value: rir },
      },
    ],
  });
  const week = (rir: number) => ({
    phase_label: "SPP",
    days: [
      { day_type: "high", sessions: [roundsAtRir("rir-1", rir)] },
      { day_type: "high", sessions: [roundsAtRir("rir-2", rir)] },
    ],
  });

  // 6 minutes of work per day: at RIR 0 (RPE 10) both days are high-fatigue,
  // at RIR 4 (RPE 6) they are moderate. RIR 0 must not read as "unstated".
  assert.equal(weekLoadProxy(week(0)), "High");
  assert.equal(weekLoadProxy(week(4)), "Moderate");
});

test("weekLoadProxy does not read pace or heart-rate numbers as RPE", () => {
  const paceRounds = (id: string) => ({
    session_id: id,
    session_type: "conditioning",
    title: "Run intervals",
    blocks: [
      {
        block_type: "conditioning",
        display_name: "Threshold-plus run intervals",
        rounds: 4,
        work: { value: 3, unit: "minutes" },
        rest: { value: 1, unit: "minutes" },
        effort: { method: "pace", value: 4 },
        intensity: "hard",
      },
    ],
  });
  const week = {
    phase_label: "SPP",
    days: [
      { day_type: "high", sessions: [paceRounds("pace-1")] },
      { day_type: "high", sessions: [paceRounds("pace-2")] },
    ],
  };

  // A 4 min/km pace is not RPE 4: the "hard" intensity decides the effort,
  // so 4 x 3 min reads as hard conditioning.
  assert.equal(weekLoadProxy(week), "High");
});

test("weekLoadProxy no longer uses a high day_type as a fatigue floor", () => {
  const week = {
    phase_label: "SPP",
    days: [
      { day_type: "high", sessions: [easyBikeSession("bike-1")] },
      { day_type: "high", sessions: [easyBikeSession("bike-2")] },
    ],
  };

  assert.equal(weekLoadProxy(week), "Low");
});

test("weekLoadProxy keeps hard sparring high even next to short explosive work", () => {
  const week = {
    phase_label: "SPP",
    days: [
      { day_type: "high", sessions: [hardSparringSession("spar-1")] },
      { day_type: "high", sessions: [bagBurstSession("burst-1")] },
      { day_type: "high", sessions: [hardSparringSession("spar-2")] },
    ],
  };

  assert.equal(weekLoadProxy(week), "High");
});

test("weekLoadProxy keeps a genuinely demanding mixed week High", () => {
  const week = {
    phase_label: "SPP",
    days: [
      { day_type: "high", sessions: [conditioningRoundsSession("cond-1")] },
      { day_type: "moderate", sessions: [easyBikeSession("bike-1")] },
      { day_type: "high", sessions: [hardSparringSession("spar-1")] },
      { day_type: "high", sessions: [bagBurstSession("burst-1")] },
    ],
  };

  assert.equal(weekLoadProxy(week), "High");
});

test("weekLoadProxy still reads undosed sessions conservatively from their words", () => {
  const week = {
    phase_label: "SPP",
    days: [
      { day_type: "high", sessions: [{ session_type: "strength_power", title: "Power and sprint session", blocks: [] }] },
      { day_type: "high", sessions: [{ session_type: "strength_power", title: "Max speed work", blocks: [] }] },
    ],
  };

  assert.equal(weekLoadProxy(week), "High");
});

test("weekLoadProxy handles low, rest and empty weeks", () => {
  const plan = campPlan();

  assert.equal(weekLoadProxy(plan.weeks![1]!), "Low");
  assert.equal(weekLoadProxy({ days: [{ day_type: "rest", sessions: [] }] }), "Rest");
  assert.equal(weekLoadProxy({ days: [] }), null);
});

test("findDayByISO returns the matching day or null", () => {
  const plan = campPlan();
  assert.equal(findDayByISO(plan, "2026-06-19")?.day_type, "moderate");
  assert.equal(findDayByISO(plan, "2026-12-25"), null);
  assert.equal(findDayByISO(plan, null), null);
});

test("getReadinessStrip surfaces focus, risk and load (never the today call)", () => {
  const plan = campPlan();
  const currentDay = findDayByISO(plan, "2026-06-19");
  const strip = getReadinessStrip(plan, currentDay, plan.weeks![0]);
  // Focus falls back to the day's headline; injury watch is a SHORT cue built
  // from the injury / weight-cut note labels (never the full stop sentence — that
  // lives in the Red Flags card), with a pointer to it; load is the week proxy.
  // The exact "train / modify / pull back" call is owned by Today; phase is left
  // to the CampStatusLine.
  assert.equal(strip.focus, "Speed conversion");
  assert.equal(strip.risk, "Active weight cut · Left shoulder contusion (see red flags)");
  // One hard day plus one moderate day is a Moderate week under weekLoadProxy.
  assert.equal(strip.load, "Moderate");
  assert.equal("todayCall" in strip, false);
  assert.equal("phase" in strip, false);
});

test("getReadinessStrip prefers an explicit readiness_snapshot over derived values", () => {
  const plan: StructuredPlan = {
    ...campPlan(),
    // today_call is ignored on the plan page (it belongs to Today); the strip
    // reads only focus / injury_watch / weekly_load from the snapshot.
    readiness_snapshot: {
      today_call: "Pull back",
      focus: "Tendon capacity",
      injury_watch: "Achilles flaring — cap plyo volume.",
      weekly_load: "Moderate-high",
    },
  };
  const currentDay = findDayByISO(plan, "2026-06-19");
  const strip = getReadinessStrip(plan, currentDay, plan.weeks![0]);
  assert.equal(strip.focus, "Tendon capacity");
  assert.equal(strip.risk, "Achilles flaring — cap plyo volume.");
  assert.equal(strip.load, "Moderate-high");
});

test("getReadinessStrip degrades gracefully with no current day", () => {
  const plan = campPlan();
  // No current day: focus has nothing to derive from. Injury watch is plan-level
  // (the note labels), so it still resolves; load comes from the passed week proxy.
  const strip = getReadinessStrip(plan, null, plan.weeks![1]);
  assert.equal(strip.focus, null);
  assert.equal(strip.risk, "Active weight cut · Left shoulder contusion (see red flags)");
  assert.equal(strip.load, "Low");
});

test("getReadinessStrip injury cue omits the red-flag pointer when there are no flags", () => {
  const plan: StructuredPlan = { ...campPlan(), red_flag_rules: [] };
  const currentDay = findDayByISO(plan, "2026-06-19");
  const strip = getReadinessStrip(plan, currentDay, plan.weeks![0]);
  assert.equal(strip.risk, "Active weight cut · Left shoulder contusion");
});

test("resolveTrainingDay applies the 03:00 athlete-local rollover", () => {
  // Before 03:00 the training day has not advanced yet — it is still yesterday.
  assert.equal(toISODate(resolveTrainingDay(new Date(2026, 5, 19, 2, 30))), "2026-06-18");
  // At/after 03:00 the training day is today.
  assert.equal(toISODate(resolveTrainingDay(new Date(2026, 5, 19, 3, 0))), "2026-06-19");
  assert.equal(toISODate(resolveTrainingDay(new Date(2026, 5, 19, 23, 59))), "2026-06-19");
});

test("resolveTrainingDay returns local midnight of the resolved day", () => {
  const resolved = resolveTrainingDay(new Date(2026, 5, 19, 15, 42, 13));
  assert.equal(resolved.getHours(), 0);
  assert.equal(resolved.getMinutes(), 0);
  assert.equal(resolved.getSeconds(), 0);
});

test("resolveCurrentDay locates today's week, day, sessions and countdown", () => {
  const current = resolveCurrentDay(campPlan(), new Date(2026, 5, 19));
  assert.equal(current.inRange, true);
  assert.equal(current.weekPos, 0);
  assert.equal(current.dayPos, 1);
  assert.equal(current.sessions.length, 1);
  assert.equal(current.sessions[0]?.title, "Upper strength");
  assert.equal(current.dLabel, "D-28");
  assert.equal(current.matchType, "calendar");
});

test("resolveCurrentDay reports an in-range off day with no sessions", () => {
  const current = resolveCurrentDay(campPlan(), new Date(2026, 5, 25));
  assert.equal(current.inRange, true);
  assert.equal(current.weekPos, 1);
  assert.equal(current.dayPos, 0);
  assert.equal(current.sessions.length, 0);
});

test("resolveCurrentDay is out of range when today maps to no day", () => {
  const current = resolveCurrentDay(campPlan(), new Date(2026, 5, 1));
  assert.equal(current.inRange, false);
  assert.equal(current.weekPos, null);
  assert.equal(current.dayPos, null);
  assert.equal(current.sessions.length, 0);
  // Still derives a countdown from the event date when out of range.
  assert.equal(current.dLabel, "D-46");
});

test("resolveCurrentDay agrees with resolvePlanProgress on the current day", () => {
  const plan = campPlan();
  const today = new Date(2026, 5, 19);
  const progress = resolvePlanProgress(plan, today);
  const current = resolveCurrentDay(plan, today);
  assert.equal(current.weekPos, progress.currentWeekPos);
  assert.equal(current.trainingDayISO, progress.currentDayDate);
  assert.equal(current.dLabel, progress.dLabel);
});

test("resolveCurrentDay treats a null today as no current day (SSR-safe)", () => {
  // Before the client mounts the training day is null; this must resolve to
  // "no current day" rather than matching a day with a missing date.
  const current = resolveCurrentDay(campPlan(), null);
  assert.equal(current.inRange, false);
  assert.equal(current.trainingDayISO, null);
  assert.equal(current.weekPos, null);
  assert.equal(current.sessions.length, 0);
  assert.equal(current.dLabel, null);
});

// A renewable open plan: four identical weekday-only weeks (no fight date, no
// calendar dates on any day) — the "WEEK 2 · SAT" shape the plan view renders.
function openPlan(): StructuredPlan {
  const days = (): NonNullable<StructuredPlan["weeks"]>[number]["days"] => [
    {
      weekday: "Mon",
      day_type: "moderate",
      sessions: [{ session_id: "open-mon", title: "Support Strength + Coordination", blocks: [] }],
    },
    {
      weekday: "Wed",
      day_type: "sparring",
      today_card: { headline: "Coach-led boxing — hard sparring" },
      sessions: [],
    },
    {
      weekday: "Sat",
      day_type: "moderate",
      sessions: [
        { session_id: "open-sat", title: "Primary Strength + Fight-pace Conditioning", blocks: [] },
      ],
    },
  ];
  return {
    schema_version: "text-adapter.v1",
    plan_metadata: { title: "Open training plan", plan_type: "open_ongoing_system" },
    event_context: null,
    weeks: [1, 2, 3, 4].map((index) => ({
      week_id: `text-week-${index}`,
      week_index: index,
      days: days(),
    })),
  } satisfies StructuredPlan;
}

test("resolveCurrentDay matches a weekday-only open plan by today's weekday", () => {
  // 2026-07-18 is a Saturday; the hint scopes the match to week 2 of the block.
  const current = resolveCurrentDay(openPlan(), new Date(2026, 6, 18), { openWeekNumber: 2 });
  assert.equal(current.inRange, true);
  assert.equal(current.weekPos, 1);
  assert.equal(current.dayPos, 2);
  assert.equal(current.sessions[0]?.title, "Primary Strength + Fight-pace Conditioning");
  assert.equal(current.dLabel, null);
  assert.equal(current.matchType, "weekday");
});

test("resolveCurrentDay matches a coach-led weekday-only day with no sessions", () => {
  // 2026-07-15 is a Wednesday — coach-led sparring day, sessionless but in range.
  const current = resolveCurrentDay(openPlan(), new Date(2026, 6, 15), { openWeekNumber: 2 });
  assert.equal(current.inRange, true);
  assert.equal(current.weekPos, 1);
  assert.equal(current.dayPos, 1);
  assert.equal(current.sessions.length, 0);
});

/** The open plan with every row dated, `monthDay` being week 1's Monday. */
function projectedOpenPlan(month: number, monthDay: number): StructuredPlan {
  const plan = openPlan();
  const weekdayOffsets = [0, 2, 5];
  for (const [weekPos, week] of (plan.weeks ?? []).entries()) {
    for (const [dayPos, day] of (week.days ?? []).entries()) {
      const date = new Date(2026, month, monthDay + weekPos * 7 + weekdayOffsets[dayPos]!);
      day.date = toISODate(date);
    }
  }
  return plan;
}

test("a stale projection still matches the live recurring weekday", () => {
  // Rows projected for a block that has already run out (it ends 28 June) while
  // the athlete is on Saturday 18 July: the block rolled over but the payload's
  // dates did not, and the weekly rhythm still holds.
  const plan = projectedOpenPlan(5, 1);
  const current = resolveCurrentDay(plan, new Date(2026, 6, 18), {
    openWeekNumber: 2,
    allowDatedWeekdayMatch: true,
  });
  const progress = resolvePlanProgress(plan, new Date(2026, 6, 18), {
    openWeekNumber: 2,
    allowDatedWeekdayMatch: true,
  });

  assert.equal(current.inRange, true);
  assert.equal(current.weekPos, 1);
  assert.equal(current.dayPos, 2);
  assert.equal(current.trainingDayISO, "2026-07-18");
  assert.equal(current.matchType, "weekday");
  assert.equal(progress.currentWeekPos, 1);
  assert.equal(progress.currentDayPos, 2);
  assert.equal(progress.currentDayDate, null);
  assert.equal(progress.trainingDayISO, "2026-07-18");
  assert.equal(progress.matchType, "weekday");
});

test("a projected block that has not started yet stays out of range", () => {
  // Week 1 starts Monday 3 August; the athlete is on Saturday 1 August. Matching
  // today's weekday onto the row dated 8 August would mark a future day "Today"
  // and stamp it with today's date, dropping 01 Aug below 05 Aug in the plan
  // timeline.
  const plan = projectedOpenPlan(7, 3);
  const current = resolveCurrentDay(plan, new Date(2026, 7, 1), {
    openWeekNumber: 1,
    allowDatedWeekdayMatch: true,
  });
  const progress = resolvePlanProgress(plan, new Date(2026, 7, 1), {
    openWeekNumber: 1,
    allowDatedWeekdayMatch: true,
  });

  assert.equal(current.inRange, false);
  assert.equal(current.weekPos, null);
  assert.equal(current.dayPos, null);
  assert.equal(current.matchType, null);
  assert.equal(progress.currentWeekPos, null);
  assert.equal(progress.currentDayPos, null);
  assert.equal(progress.matchType, null);
});

test("resolveCurrentDay falls back to the first matching week without a week hint", () => {
  const current = resolveCurrentDay(openPlan(), new Date(2026, 6, 18));
  assert.equal(current.inRange, true);
  assert.equal(current.weekPos, 0);
  assert.equal(current.dayPos, 2);
});

test("resolveCurrentDay leaves a weekday-only non-training day out of range", () => {
  // 2026-07-14 is a Tuesday — the open plan schedules nothing on Tuesdays.
  const current = resolveCurrentDay(openPlan(), new Date(2026, 6, 14), { openWeekNumber: 2 });
  assert.equal(current.inRange, false);
  assert.equal(current.weekPos, null);
  assert.equal(current.matchType, null);
});

test("resolveCurrentDay never matches a dated camp by weekday", () => {
  // 2026-06-26 is a Friday, same weekday as the camp day 2026-06-19 — but dated
  // days must only ever match on their calendar date.
  const plan = campPlan();
  for (const week of plan.weeks ?? []) {
    for (const day of week.days ?? []) {
      day.weekday = "Fri";
    }
  }
  const current = resolveCurrentDay(plan, new Date(2026, 5, 26), { openWeekNumber: 1 });
  assert.equal(current.inRange, false);
});

test("resolvePlanProgress marks the weekday-only current week and day position", () => {
  const progress = resolvePlanProgress(openPlan(), new Date(2026, 6, 18), { openWeekNumber: 2 });
  assert.equal(progress.currentWeekPos, 1);
  assert.equal(progress.currentDayPos, 2);
  // Weekday-only matches carry no calendar date.
  assert.equal(progress.currentDayDate, null);
  assert.equal(progress.trainingDayISO, "2026-07-18");
  assert.equal(progress.matchType, "weekday");
});

test("resolveOpenPlanWeekNumber prefers the server-computed week number", () => {
  const week = resolveOpenPlanWeekNumber(openPlan(), new Date(2026, 6, 18), {
    currentWeekNumber: 3,
    createdAt: "2026-06-30T09:00:00Z",
  });
  assert.equal(week, 3);
});

test("resolveOpenPlanWeekNumber derives the week from the plan-creation anchor", () => {
  // Created Tuesday 2026-06-30 -> anchor Monday 2026-06-29: a Mon-Thu plan joins
  // the week it was created in (mirroring the backend timeline).
  const hints = { createdAt: "2026-06-30T09:00:00Z" };
  assert.equal(resolveOpenPlanWeekNumber(openPlan(), new Date(2026, 6, 1), hints), 1);
  assert.equal(resolveOpenPlanWeekNumber(openPlan(), new Date(2026, 6, 8), hints), 2);
  assert.equal(resolveOpenPlanWeekNumber(openPlan(), new Date(2026, 6, 18), hints), 3);
  // The block renews: 4 weeks after the anchor it is week 1 again.
  assert.equal(resolveOpenPlanWeekNumber(openPlan(), new Date(2026, 6, 27), hints), 1);
});

test("resolveOpenPlanWeekNumber starts a late-week plan on the coming Monday", () => {
  // Created Friday 2026-07-31 -> anchor Monday 2026-08-03; the rest of that week
  // is too short to join.
  const hints = { createdAt: "2026-07-31T09:00:00Z" };
  // Days before the anchor belong to week 1.
  assert.equal(resolveOpenPlanWeekNumber(openPlan(), new Date(2026, 6, 31), hints), 1);
  assert.equal(resolveOpenPlanWeekNumber(openPlan(), new Date(2026, 7, 10), hints), 2);
});

test("resolveOpenPlanWeekNumber returns null without an anchor or today", () => {
  assert.equal(resolveOpenPlanWeekNumber(openPlan(), new Date(2026, 6, 18), {}), null);
  assert.equal(resolveOpenPlanWeekNumber(openPlan(), null, { createdAt: "2026-06-30" }), null);
  assert.equal(resolveOpenPlanWeekNumber(undefined, new Date(2026, 6, 18), { createdAt: "2026-06-30" }), null);
});

test("resolveNextPlanFocusDay prefers the next unfinished app card before a later coach-led day", () => {
  const plan = {
    weeks: [
      {
        days: [
          {
            date: "2026-06-23",
            countdown_label: "D-9",
            sessions: [{ session_id: "tue", title: "Single-leg power", completion_status: "done" }],
          },
          {
            date: "2026-06-26",
            countdown_label: "D-6",
            sessions: [{ session_id: "fri", title: "Friday app session", completion_status: "not_started" }],
          },
          {
            date: "2026-06-27",
            countdown_label: "D-5",
            today_card: { headline: "Technical work" },
            sessions: [],
          },
        ],
      },
    ],
  } as StructuredPlan;

  const focus = resolveNextPlanFocusDay(plan, new Date(2026, 5, 23), new Date(2026, 5, 27));

  assert.equal(focus && toISODate(focus), "2026-06-26");
});

test("resolveNextPlanFocusDay falls through to coach-led day after app cards are terminal", () => {
  const plan = {
    weeks: [
      {
        days: [
          {
            date: "2026-06-23",
            sessions: [{ session_id: "tue", completion_status: "done" }],
          },
          {
            date: "2026-06-26",
            sessions: [{ session_id: "fri", completion_status: "skipped" }],
          },
          {
            date: "2026-06-27",
            today_card: { headline: "Technical work" },
            sessions: [],
          },
        ],
      },
    ],
  } as StructuredPlan;

  const focus = resolveNextPlanFocusDay(plan, new Date(2026, 5, 23), new Date(2026, 5, 27));

  assert.equal(focus && toISODate(focus), "2026-06-27");
});

test("resolvePlanProgress treats a null today as out of range (SSR-safe)", () => {
  const progress = resolvePlanProgress(campPlan(), null);
  assert.equal(progress.currentWeekPos, null);
  assert.equal(progress.currentDayDate, null);
  assert.equal(progress.dLabel, null);
});

test("sessionIdentity prefers plan + day + session_id", () => {
  const plan = campPlan();
  const day = plan.weeks![0]!.days![1]!;
  const session = day.sessions![0]!;
  assert.equal(
    sessionIdentity({ planId: "plan-1", weekPos: 0, dayPos: 1, sessionPos: 0, day, session }),
    "plan-1|2026-06-19|s3",
  );
});

test("sessionIdentity falls back to plan + week/day/session indices", () => {
  // No session_id and no day date -> stable index-based identity.
  assert.equal(
    sessionIdentity({
      planId: "plan-1",
      weekPos: 2,
      dayPos: 3,
      sessionPos: 1,
      day: { sessions: [] },
      session: {},
    }),
    "plan-1|w2|d3|s1",
  );
});

test("sessionIdentity keys the day portion on the parent day's date", () => {
  // The parent day owns the date; identity must not key on anything else.
  const day = { date: "2026-06-19", sessions: [] };
  assert.equal(
    sessionIdentity({ planId: "plan-1", weekPos: 0, dayPos: 0, sessionPos: 0, day, session: {} }),
    "plan-1|w0|d0|s0",
  );
  assert.equal(
    sessionIdentity({
      planId: "plan-1",
      weekPos: 0,
      dayPos: 0,
      sessionPos: 0,
      day,
      session: { session_id: "abc" },
    }),
    "plan-1|2026-06-19|abc",
  );
});

// ---------------------------------------------------------------------------
// Live completion merge (plan-card status from /api/plans/{id}/completions)
// ---------------------------------------------------------------------------

function completionRow(overrides: Partial<TodaySessionCompletionRecord> = {}): TodaySessionCompletionRecord {
  return {
    id: "cmp-1",
    athlete_id: "a1",
    plan_id: "p1",
    session_id: "s1",
    training_day: "2026-06-18",
    status: "done",
    ...overrides,
  };
}

test("buildCompletionIndex keys rows by training_day|session_id", () => {
  const index = buildCompletionIndex([
    completionRow(),
    completionRow({ id: "cmp-2", session_id: "s2", status: "skipped" }),
  ]);
  const day = campPlan().weeks![0]!.days![0]!;
  assert.equal(completionForSession(index, day, day.sessions![0]!)?.status, "done");
  assert.equal(completionForSession(index, day, day.sessions![1]!)?.status, "skipped");
});

test("completionForSession does not treat a date as an id for an id-less session", () => {
  const day = {
    date: "2026-06-20",
    sessions: [{ title: "Untitled primary", blocks: [] }, { title: "Secondary, also id-less" }],
  };
  const index = buildCompletionIndex([
    completionRow({ session_id: "2026-06-20", training_day: "2026-06-20" }),
  ]);
  assert.equal(completionForSession(index, day, day.sessions![0]!), undefined);
  assert.equal(completionForSession(index, day, day.sessions![1]!), undefined);
});

test("primary session skips support work that leads the day in execution order", () => {
  const day = {
    date: "2026-06-20",
    sessions: [
      { title: "Joint Prep", session_type: "rehab", blocks: [{ block_type: "mobility_activation" }] },
      { title: "Tactical Focus", session_type: "skill", blocks: [{ block_type: "mindset" }] },
      { title: "Strength", session_type: "strength_power", blocks: [{ block_type: "strength" }] },
      { title: "Breathing Reset", session_type: "recovery", blocks: [{ block_type: "cooldown_recovery" }] },
    ],
  };
  assert.equal(primarySessionOf(day)?.title, "Strength");
  const supportOnly = {
    date: "2026-06-21",
    sessions: [
      { title: "Joint Prep", session_type: "rehab", blocks: [{ block_type: "mobility_activation" }] },
      { title: "Breathing Reset", session_type: "recovery", blocks: [{ block_type: "cooldown_recovery" }] },
    ],
  };
  assert.equal(primarySessionOf(supportOnly)?.title, "Joint Prep");
});

test("optional visualisation never owns a required support day's completion", () => {
  const optional = { title: "Fight Visualisation", session_id: "optional", optional: true,
    blocks: [{ block_type: "mindset" }] };
  const required = { title: "Breathing Reset", session_id: "required",
    blocks: [{ block_type: "cooldown_recovery" }] };
  const day = { date: "2026-10-07", sessions: [optional, required] };
  assert.equal(primarySessionOf(day)?.session_id, "required");
  const index = buildCompletionIndex([completionRow({ session_id: "required", training_day: day.date })]);
  assert.equal(completionForSession(index, day, primarySessionOf(day))?.status, "done");
  assert.equal(primarySessionOf({ ...day, sessions: [optional] }), null);
});

test("primary session is the first session with executable blocks", () => {
  const day = {
    date: "2026-06-20",
    sessions: [
      { title: "Coach-led contact" },
      { title: "App session", blocks: [{ block_id: "b1" }] },
    ],
  };
  assert.equal(primarySessionOf(day)?.title, "App session");
  assert.equal(completionSessionId(day, day.sessions![1]!), null);
  assert.equal(completionSessionId(day, day.sessions![0]!), null);
});

test("getSessionDisplayStatus maps logged statuses to the tone contract", () => {
  assert.deepEqual(getSessionDisplayStatus(completionRow(), "2026-06-18", "2026-06-19"), {
    state: "done",
    tone: "green",
    label: "Done",
  });
  assert.equal(getSessionDisplayStatus(completionRow({ status: "modified" }), "2026-06-18", "2026-06-19").tone, "amber");
  assert.equal(getSessionDisplayStatus(completionRow({ status: "skipped" }), "2026-06-18", "2026-06-19").tone, "red");
});

test("a past day with no terminal log reads as Missed — including started-only", () => {
  assert.deepEqual(getSessionDisplayStatus(undefined, "2026-06-18", "2026-06-19"), {
    state: "missed",
    tone: "red",
    label: "Missed",
  });
  assert.equal(
    getSessionDisplayStatus(completionRow({ status: "started" }), "2026-06-18", "2026-06-19").state,
    "missed",
  );
});

test("today and future days without logs stay neutral", () => {
  assert.equal(getSessionDisplayStatus(undefined, "2026-06-19", "2026-06-19").state, "pending");
  assert.equal(getSessionDisplayStatus(undefined, "2026-06-20", "2026-06-19").state, "upcoming");
  assert.equal(getSessionDisplayStatus(undefined, "2026-06-20", "2026-06-19").tone, "neutral");
  // Without a server day nothing can be called missed.
  assert.equal(getSessionDisplayStatus(undefined, "2026-06-18", null).state, "upcoming");
});

test("canRetroLog allows past days inside the 7-day window only", () => {
  assert.equal(canRetroLog("2026-06-18", "2026-06-19"), true);
  assert.equal(canRetroLog("2026-06-12", "2026-06-19"), true);
  assert.equal(canRetroLog("2026-06-11", "2026-06-19"), false);
  assert.equal(canRetroLog("2026-06-19", "2026-06-19"), false);
  assert.equal(canRetroLog("2026-06-20", "2026-06-19"), false);
  assert.equal(canRetroLog(null, "2026-06-19"), false);
});

test("dayCompletion counts live done and modified rows when an index is supplied", () => {
  const plan = campPlan();
  const day = plan.weeks![0]!.days![0]!;
  const index = buildCompletionIndex([
    completionRow({ session_id: "s1", status: "modified" }),
    completionRow({ id: "cmp-2", session_id: "s2", status: "skipped" }),
  ]);
  assert.deepEqual(dayCompletion(day, index), { done: 1, total: 2 });
  // Static plan JSON still works without an index (generation-time statuses).
  assert.deepEqual(dayCompletion(day), { done: 1, total: 2 });
});

test("athleteTrainingDayISO uses the athlete's timezone and the 03:00 rollover", () => {
  // 01:30 UTC on 2 Oct is 21:30 on 1 Oct in New York and 10:30 on 2 Oct in Tokyo.
  const now = new Date("2026-10-02T01:30:00Z");
  assert.equal(athleteTrainingDayISO(now, "America/New_York"), "2026-10-01");
  assert.equal(athleteTrainingDayISO(now, "Asia/Tokyo"), "2026-10-02");
  // 02:00 in London (BST) is before the rollover, so it is still the previous day.
  assert.equal(athleteTrainingDayISO(new Date("2026-10-02T01:00:00Z"), "Europe/London"), "2026-10-01");
  assert.equal(athleteTrainingDayISO(new Date("2026-10-02T02:30:00Z"), "Europe/London"), "2026-10-02");
  // An unknown zone falls back to the device day instead of throwing.
  assert.match(athleteTrainingDayISO(now, "Not/AZone"), /^\d{4}-\d{2}-\d{2}$/);
});

test("a day before the plan existed is never Missed or retro-loggable", () => {
  // Plan generated on Tuesday 13 Oct: Monday's session was never available.
  assert.deepEqual(getSessionDisplayStatus(undefined, "2026-10-12", "2026-10-14", "2026-10-13"), {
    state: "before_plan",
    tone: "neutral",
    label: "",
  });
  assert.equal(getSessionDisplayStatus(undefined, "2026-10-13", "2026-10-14", "2026-10-13").state, "missed");
  assert.equal(canRetroLog("2026-10-12", "2026-10-14", "2026-10-13"), false);
  assert.equal(canRetroLog("2026-10-13", "2026-10-14", "2026-10-13"), true);
  // A logged session keeps its real status whatever the plan start.
  assert.equal(getSessionDisplayStatus(completionRow(), "2026-10-12", "2026-10-14", "2026-10-13").state, "done");
});
