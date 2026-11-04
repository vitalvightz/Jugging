import test from "node:test";
import assert from "node:assert/strict";

import {
  getReducedContactReason,
  REDUCED_CONTACT_FALLBACK_REASON,
  classifySessionlessDay,
  compactTimeUnits,
  getCoachLedContactView,
  isConvertedTechnicalHeadline,
  cleanText,
  finitePositiveNumber,
  formatBlockLoad,
  formatClusterSummary,
  formatRestNote,
  getBlockComponents,
  formatCountdownLabel,
  formatEffort,
  isNonFiniteNumericToken,
  formatMacroRange,
  formatMeasured,
  formatSessionObjective,
  formatWeightCutBand,
  macroLine,
  getActiveNotesExcludingRedFlags,
  getBlockCoachingDisplay,
  getBlocks,
  getCoachingCues,
  getDays,
  getDeterministicNutritionPhases,
  getDeterministicRecoveryPhases,
  getDisplayableRedFlags,
  getFallbackSafetyNotes,
  getMindsetLines,
  getSessionCoachingLines,
  getPlanNotes,
  getPriorityMicrodose,
  planNoteLabel,
  getSessions,
  getStringList,
  getWeeks,
  hasDeterministicNutrition,
  hasDeterministicRecovery,
  hasNutrition,
  isDeEmphasisedWeightCutSafety,
  isProminentRedFlagSeverity,
  isStopRuleText,
  isTimeLikeReps,
  inferredLateFightWeekContext,
  nutritionPhaseRows,
  progressionRuleLabel,
  recoveryPhaseView,
  redFlagView,
  resolvedWeekPhase,
  selectBlockMetric,
  shouldRenderStructuredPlan,
  shouldShowRest,
  splitMindsetLines,
  weekLabel,
} from "./structured-plan.ts";

test("empty calendar days retain neutral availability labels", () => {
  for (const headline of ["Unavailable", "No session planned"]) {
    const view = classifySessionlessDay({ day_type: "rest", today_card: { headline }, sessions: [] });
    assert.equal(view.kind, "rest");
    assert.equal(view.title, headline);
    assert.equal(view.coachLed, false);
  }
});

test("formatCountdownLabel normalizes legacy event-day labels for display", () => {
  assert.equal(formatCountdownLabel("D0"), "D-0");
  assert.equal(formatCountdownLabel("d0"), "D-0");
  assert.equal(formatCountdownLabel("D-12"), "D-12");
  assert.equal(formatCountdownLabel("  "), null);
});

test("getPriorityMicrodose requires complete planner-owned display fields", () => {
  assert.deepEqual(
    getPriorityMicrodose({
      priority_microdose: { goal: " power ", name: " Med-ball throw ", prescription: " 2 x 3 " },
    }),
    { goal: "power", name: "Med-ball throw", prescription: "2 x 3" },
  );
  assert.equal(getPriorityMicrodose({}), null);
  assert.equal(
    getPriorityMicrodose({ priority_microdose: { goal: "power", name: "Med-ball throw" } }),
    null,
  );
});

// An athlete-safe deterministic_support projection (as the backend emits it,
// with coach_gated already stripped).
function planWithDeterministicSupport() {
  return {
    schema_version: "1.0",
    nutrition: { summary: "Fuel around sessions." },
    weeks: [{ week_id: "wk-1", week_index: 1, days: [] }],
    deterministic_support: {
      schema_version: "athlete_support.v1",
      nutrition: {
        by_phase: {
          TAPER: {
            phase: "TAPER",
            meal_structure: "3 core meals + 2-3 snacks daily",
            protein_g_per_day: { min: 126, max: 175, per_kg: [1.8, 2.5], note: null },
            carbs_g_per_day: { min: null, max: 350, per_kg: [null, 5], note: "reduce before weigh-in" },
            fats_g_per_day: { min: null, max: null, per_kg: null, note: "moderate (~20%)" },
            hydration_ml_per_day: { min: 2100, max: 2800, per_kg_l: [0.03, 0.04] },
            fuel_timing: { pre: "light carbs", intra: "water only", post: "carbs + protein" },
            fatigue_adjustment: "high",
            weight_cut: { active: true, risk_band: "severe", supervision_required: true },
          },
          GPP: {
            phase: "GPP",
            protein_g_per_day: { min: 112, max: 140, per_kg: [1.6, 2.0], note: null },
            hydration_ml_per_day: { min: 2100, max: 2800 },
            weight_cut: { active: false, risk_band: "none", supervision_required: false },
          },
        },
      },
      recovery: {
        by_phase: {
          GPP: {
            phase: "GPP",
            core_strategies: ["Daily breathwork", "8-9 h sleep"],
            sleep_hours_target: [8, 9],
            phase_focus: ["Tissue prep & joint mobility"],
            weight_cut: { active: false, risk_band: "none", supervision_required: false },
          },
          TAPER: {
            phase: "TAPER",
            core_strategies: ["Breathwork"],
            sleep_hours_target: [8, 9],
            fatigue_flags: ["Cut weekly volume by 25-40%"],
            phase_focus: ["Reduce volume to 30-40%"],
            weight_cut: { active: true, risk_band: "severe", supervision_required: true },
          },
        },
      },
    },
  };
}

function validPlan() {
  return {
    schema_version: "1.0",
    plan_metadata: { title: "Fight Camp", sport: "boxing", plan_type: "fight_camp" },
    athlete_context: { sport_profile: "amateur boxer" },
    red_flag_rules: [
      { rule_id: "r1", severity: "red", display_text: "Sharp pain — stop and report.", action: "Notify coach." },
    ],
    nutrition: { summary: "Fuel around sessions.", daily_focus: "Protein + carbs." },
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "SPP",
        week_goal: "Power",
        days: [
          {
            date: "2026-05-29",
            day_type: "high",
            countdown_label: "D-15",
            today_card: {
              headline: "Power day",
              readiness_status: "train_as_planned",
              mindset_anchor: { intent: "Move fast", focus_cue: "Drive the floor", reset_cue: "Breathe out" },
            },
            sessions: [
              {
                session_id: "ses-1",
                session_type: "strength_power",
                title: "Power Transfer",
                objective: "Raise punch speed.",
                mindset_anchor: { intent: "Snap", focus_cue: "Fast hands", reset_cue: "Reset stance" },
                blocks: [
                  {
                    block_id: "blk-1",
                    block_type: "strength",
                    display_name: "Back Squat",
                    sets: 4,
                    reps: "4-6",
                    load: { method: "percentage", value: 85, unit: "percent", display: "85% 1RM" },
                    rest: { value: 180, unit: "seconds" },
                  },
                ],
              },
            ],
          },
        ],
      },
    ],
    raw_markdown_fallback: "# Fight Camp\n...",
  };
}

// --- render decision: structured vs plan_text fallback ----------------------

test("renders plan_text (no structured) when structured_plan is null", () => {
  assert.equal(shouldRenderStructuredPlan({ structured_plan: null }), false);
  assert.equal(shouldRenderStructuredPlan({ structured_plan: undefined }), false);
  assert.equal(shouldRenderStructuredPlan(null), false);
  assert.equal(shouldRenderStructuredPlan(undefined), false);
});

test("renders structured_plan when present with weeks", () => {
  assert.equal(shouldRenderStructuredPlan({ structured_plan: validPlan() }), true);
});

test("falls back to plan_text when structured_plan is malformed", () => {
  // Wrong types / no usable weeks must not render the structured UI.
  assert.equal(shouldRenderStructuredPlan({ structured_plan: {} as never }), false);
  assert.equal(shouldRenderStructuredPlan({ structured_plan: { weeks: [] } as never }), false);
  assert.equal(shouldRenderStructuredPlan({ structured_plan: { weeks: "nope" } as never }), false);
  assert.equal(shouldRenderStructuredPlan({ structured_plan: "raw text" as never }), false);
  assert.equal(shouldRenderStructuredPlan({ structured_plan: [] as never }), false);
});

// --- progression_rule vs stop rule labelling --------------------------------

test("labels a stop rule in progression_rule as Stop rule, not Progress", () => {
  // The conversion model routinely drops per-block stop rules into
  // progression_rule; the renderer must never tell the athlete to ADVANCE on a
  // safety cue.
  for (const stop of [
    "Stop on sharp pain.",
    "Stop the set if ankle pain increases or punch speed drops across the set.",
    "stop if the ankle flares",
    "Stop when burst quality drops 2 reps.",
    "Stop immediately on dizziness.",
  ]) {
    assert.equal(isStopRuleText(stop), true, stop);
    assert.equal(progressionRuleLabel(stop), "Stop rule", stop);
  }
});

test("keeps genuine progression content labelled Progress", () => {
  for (const progress of [
    "Add 5 minutes next week once pain-free.",
    "Progress to single-leg once double-leg is easy.",
    "Increase band tension when speed holds across all sets.",
    "The non-stop shop drill advances by tempo.", // "stop" mid-word must not trip it
  ]) {
    assert.equal(isStopRuleText(progress), false, progress);
    assert.equal(progressionRuleLabel(progress), "Progress", progress);
  }
});

test("stop-rule detection is safe on null / blank input", () => {
  assert.equal(isStopRuleText(null), false);
  assert.equal(isStopRuleText(undefined), false);
  assert.equal(isStopRuleText("   "), false);
  assert.equal(progressionRuleLabel(null), "Progress");
});

test("legacy bare regression labels are dropped and Stop cues are separated", () => {
  const display = getBlockCoachingDisplay({
    coaching_cues: [
      "Regression /",
      "Stay tall and relaxed.",
      "Stop: reduce to breathing only if shoulder pain rises.",
    ],
  } as never);

  assert.deepEqual(display.cues, ["Stay tall and relaxed."]);
  assert.deepEqual(display.stopRules, [
    "Stop: reduce to breathing only if shoulder pain rises.",
  ]);
});

// --- defensive selectors: never crash on partial data -----------------------

test("selectors return safe empties on missing/partial fields", () => {
  assert.deepEqual(getWeeks(null), []);
  assert.deepEqual(getWeeks({} as never), []);
  assert.deepEqual(getDays(undefined), []);
  assert.deepEqual(getSessions({ sessions: null } as never), []);
  assert.deepEqual(getBlocks({} as never), []);
  assert.deepEqual(getCoachingCues({} as never), []);
  assert.deepEqual(getMindsetLines(undefined), []);
  // A week/day/session with null nested arrays does not throw.
  const week = getWeeks({ structured_plan: validPlan() } as never);
  assert.equal(week.length, 0); // wrong-shaped input -> empty, no crash
});

test("getWeeks leaves a single-calendar-week plan untouched", () => {
  // Mon–Fri all in the same calendar week -> one week, unchanged.
  const plan = {
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        days: [
          { date: "2026-06-29", countdown_label: "D-19" }, // Mon
          { date: "2026-07-01", countdown_label: "D-17" }, // Wed
          { date: "2026-07-03", countdown_label: "D-15" }, // Fri
        ],
      },
    ],
  } as never;
  const weeks = getWeeks(plan);
  assert.equal(weeks.length, 1);
  assert.equal(weeks[0].week_id, "wk-1");
});

test("getWeeks does not split a <=7-day week that merely crosses a Monday", () => {
  // Sat 2026-07-04 .. Tue 2026-07-07 crosses a Monday but spans only 3 days, so a
  // normally-structured short week stays intact.
  const plan = {
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        days: [
          { date: "2026-07-04", countdown_label: "D-14" }, // Sat (cw1)
          { date: "2026-07-07", countdown_label: "D-11" }, // Tue (cw2)
        ],
      },
    ],
  } as never;
  assert.equal(getWeeks(plan).length, 1);
});

test("getWeeks splits a multi-calendar-week late-fight block into weeks", () => {
  // One week object spanning three calendar weeks (the late-fight bridge bug):
  // D-18 Tue 2026-06-30 ... D-5 Mon 2026-07-13 must render as W1/W2/W3.
  const plan = {
    weeks: [
      {
        week_id: "wk-1",
        week_index: 1,
        phase_label: "SPP",
        week_goal: "Maintain freshness",
        start_date: "2026-06-30",
        end_date: "2026-07-13",
        countdown_start: "D-18",
        countdown_end: "D-5",
        days: [
          { date: "2026-06-30", countdown_label: "D-18" }, // cw1 Tue
          { date: "2026-07-01", countdown_label: "D-17" }, // cw1 Wed
          { date: "2026-07-04", countdown_label: "D-14" }, // cw1 Sat
          { date: "2026-07-07", countdown_label: "D-11" }, // cw2 Tue
          { date: "2026-07-09", countdown_label: "D-9" }, // cw2 Thu
          { date: "2026-07-13", countdown_label: "D-5" }, // cw3 Mon
        ],
      },
    ],
  } as never;

  const weeks = getWeeks(plan);
  assert.equal(weeks.length, 3);
  assert.deepEqual(
    weeks.map((week) => week.week_index),
    [1, 2, 3],
  );
  // Each sub-week carries only its own days and a recomputed range.
  assert.equal(getDays(weeks[0]).length, 3);
  assert.equal(weeks[0].start_date, "2026-06-30");
  assert.equal(weeks[0].end_date, "2026-07-04");
  assert.equal(weeks[0].countdown_start, "D-18");
  assert.equal(weeks[0].countdown_end, "D-14");
  assert.equal(getDays(weeks[1]).length, 2);
  assert.equal(weeks[1].countdown_start, "D-11");
  assert.equal(getDays(weeks[2]).length, 1);
  assert.equal(weeks[2].countdown_end, "D-5");
  // Metadata is inherited; ids stay unique.
  assert.equal(weeks[0].phase_label, "SPP");
  assert.equal(new Set(weeks.map((week) => week.week_id)).size, 3);
});

test("calendar splitting derives display phase from each group's days", () => {
  const plan = {
    weeks: [
      {
        week_id: "wk-authority",
        week_index: 1,
        phase_label: "TAPER",
        start_date: "2026-08-20",
        end_date: "2026-08-30",
        countdown_start: "D-28",
        countdown_end: "D-18",
        days: [
          { date: "2026-08-20", countdown_label: "D-28", phase_label: "GPP" },
          { date: "2026-08-23", countdown_label: "D-25", phase_label: "GPP" },
          { date: "2026-08-27", countdown_label: "D-21", phase_label: "SPP", planning_week_index: 2 },
          { date: "2026-08-30", countdown_label: "D-18", phase_label: "SPP", planning_week_index: 2 },
        ],
      },
    ],
  } as never;

  const weeks = getWeeks(plan);

  assert.equal(weeks.length, 2);
  assert.equal(weeks[0].phase_label, "GPP");
  assert.deepEqual(weeks[0].phase_coverage, ["GPP"]);
  assert.equal(weeks[1].phase_label, "SPP");
  assert.deepEqual(weeks[1].phase_coverage, ["SPP"]);
  assert.equal(getDays(weeks[1])[0].phase_label, "SPP");
  assert.equal(getDays(weeks[1])[0].planning_week_index, 2);
});

test("calendar splitting re-resolves deterministic late-fight titles but preserves custom legacy goals", () => {
  const sourceWeek = {
    week_id: "wk-late",
    week_index: 1,
    phase_label: "TAPER",
    week_goal: "Compressed Pre-Fight Week",
    start_date: "2026-07-26",
    end_date: "2026-08-05",
    countdown_start: "D-10",
    countdown_end: "D-0",
    days: [
      { date: "2026-07-26", countdown_label: "D-10" },
      { date: "2026-07-27", countdown_label: "D-9" },
      { date: "2026-08-02", countdown_label: "D-3" },
      { date: "2026-08-03", countdown_label: "D-2" },
      { date: "2026-08-05", countdown_label: "D-0" },
    ],
  };

  const deterministic = getWeeks({ weeks: [sourceWeek] } as never);
  assert.deepEqual(
    deterministic.map((week) => week.week_goal),
    ["Compressed Pre-Fight Week", "Compressed Pre-Fight Week", "Sharpness Sessions"],
  );

  const legacy = getWeeks({
    weeks: [{ ...sourceWeek, week_goal: "Power Transfer Touch" }],
  } as never);
  assert.deepEqual(
    legacy.map((week) => week.week_goal),
    ["Power Transfer Touch", "Power Transfer Touch", "Power Transfer Touch"],
  );
});

// --- plan-level active notes ------------------------------------------------

test("getPlanNotes keeps notes with text, drops empties, lowercases category", () => {
  const notes = getPlanNotes({
    plan_notes: [
      { category: "Weight_Cut", label: "Active weight cut", text: "~5.7% target." },
      { category: "injury", text: "Keep the wound covered and dry." },
      { category: "nutrition", text: "   " },
      { text: "Stay disciplined." },
      "not an object" as never,
    ],
  } as never);
  assert.equal(notes.length, 3);
  assert.deepEqual(notes[0], {
    category: "weight_cut",
    label: "Active weight cut",
    text: "~5.7% target.",
  });
  assert.equal(notes[1].category, "injury");
  assert.equal(notes[2].category, "general");
  assert.equal(notes[2].label, null);
});

test("getPlanNotes is safe on missing / malformed plan_notes", () => {
  assert.deepEqual(getPlanNotes(null), []);
  assert.deepEqual(getPlanNotes({} as never), []);
  assert.deepEqual(getPlanNotes({ plan_notes: "nope" } as never), []);
});

test("getActiveNotesExcludingRedFlags drops notes that restate a red flag", () => {
  const plan = {
    red_flag_rules: [
      {
        rule_id: "rf-1",
        display_text:
          "If weight-cut symptoms worsen (lightheadedness, excessive weakness), stop non-essential activity and escalate to coach/medical staff.",
      },
    ],
    plan_notes: [
      // A shorter paraphrase of the red flag (missing the parenthetical) — dropped.
      {
        category: "weight_cut",
        label: "Note",
        text: "If weight-cut symptoms worsen, stop non-essential activity and escalate to coach/medical staff.",
      },
      // Richer context that merely shares a phrase with the flag — kept.
      {
        category: "injury",
        label: "Left shoulder contusion",
        text: "High-severity bruise, stable. Avoid direct contact; stop any drill on sharp pain or new swelling. Rehab drills included each session.",
      },
      // Unrelated context — kept.
      { category: "weight_cut", label: "Active weight cut", text: "Cut ~3.5%; recovery tolerance reduced." },
    ],
  } as never;

  const notes = getActiveNotesExcludingRedFlags(plan);
  assert.equal(notes.length, 2);
  assert.equal(
    notes.some((note) => note.label === "Note"),
    false,
  );
  assert.equal(
    notes.some((note) => note.label === "Left shoulder contusion"),
    true,
  );
  assert.equal(
    notes.some((note) => note.label === "Active weight cut"),
    true,
  );
});

test("getActiveNotesExcludingRedFlags returns all notes when there are no red flags", () => {
  const plan = {
    plan_notes: [{ category: "general", text: "Stay disciplined." }],
  } as never;
  assert.equal(getActiveNotesExcludingRedFlags(plan).length, 1);
});

test("planNoteLabel prefers an explicit label, else a category title", () => {
  assert.equal(planNoteLabel({ category: "weight_cut", label: "Cut", text: "x" }), "Cut");
  assert.equal(planNoteLabel({ category: "injury", label: null, text: "x" }), "Injury");
  assert.equal(planNoteLabel({ category: "unknown", label: null, text: "x" }), "Note");
});

// --- session-less day classification ----------------------------------------

test("classifies coach-led / sparring / technical days from the headline", () => {
  const make = (headline: string, day_type = "moderate") => ({
    day_type,
    today_card: { headline },
  });

  assert.equal(classifySessionlessDay(make("Coach-led boxing session")).kind, "coach_led");
  const lightCombat = classifySessionlessDay(make("Light technical combat"));
  assert.equal(lightCombat.kind, "light_combat");
  assert.equal(lightCombat.tag, "Technical");
  assert.equal(lightCombat.coachLed, false);
  assert.equal(classifySessionlessDay(make("Hard sparring")).kind, "sparring");
  assert.equal(classifySessionlessDay(make("Coach-led sparring")).kind, "sparring");
  assert.equal(classifySessionlessDay(make("Coach-led boxing \u2014 technical only")).kind, "technical");
  // "technical only / no hard sparring" must read as technical, not sparring.
  assert.equal(
    classifySessionlessDay(make("Coach-led boxing — no hard sparring / technical only")).kind,
    "technical",
  );
  // A sparring/technical contact day carries its headline as the card title
  // and flags the coach-neutral "your own hard sparring/contact work" note.
  const sparring = classifySessionlessDay(make("Hard sparring"));
  assert.equal(sparring.title, "Hard sparring");
  assert.equal(sparring.tag, "Sparring");
  assert.equal(sparring.coachLed, true);
});

test("reads coach-led contact that coexists with app sessions", () => {
  // No coach_led_contact field -> null (a plain session day shows no contact).
  assert.equal(getCoachLedContactView({ today_card: { headline: "Lower strength" } }), null);
  assert.equal(getCoachLedContactView(null), null);
  assert.equal(getCoachLedContactView({ today_card: {} }), null);

  // The dedicated field drives the contact view independently of the day headline
  // (which a session day uses for its own session title).
  const spar = getCoachLedContactView({
    today_card: { headline: "Lower strength", coach_led_contact: "Coach-led sparring" },
  });
  assert.equal(spar?.kind, "sparring");
  assert.equal(spar?.title, "Coach-led sparring");
  assert.equal(spar?.tag, "Sparring");

  const technical = getCoachLedContactView({
    today_card: { coach_led_contact: "Coach-led boxing — technical only" },
  });
  assert.equal(technical?.kind, "technical");
  assert.equal(technical?.tag, "Technical");
});

test("explains every reduced-dose sparring day deterministically", () => {
  const stamped = "Reduced dose: It falls back-to-back with another hard sparring day.";
  assert.equal(
    getReducedContactReason({
      today_card: { headline: "Hard sparring — reduced dose", contact_reason: stamped },
    }),
    stamped,
  );
  // Plans saved before the server stamped a reason still get the fixed fallback.
  assert.equal(
    getReducedContactReason({ today_card: { headline: "Hard sparring — reduced dose" } }),
    REDUCED_CONTACT_FALLBACK_REASON,
  );
  assert.equal(
    getReducedContactReason({
      today_card: { headline: "Lower strength", coach_led_contact: "Hard sparring — reduced dose" },
    }),
    REDUCED_CONTACT_FALLBACK_REASON,
  );
  assert.equal(getReducedContactReason({ today_card: { headline: "Hard sparring" } }), null);
  // A stale reason left on a day that is now full hard or technical is ignored.
  assert.equal(
    getReducedContactReason({ today_card: { headline: "Hard sparring", contact_reason: stamped } }),
    null,
  );
  assert.equal(
    getReducedContactReason({
      today_card: { headline: "Lower strength", coach_led_contact: "Technical-only combat", contact_reason: stamped },
    }),
    null,
  );
  assert.equal(getReducedContactReason(null), null);
});

test("classifies a headline-less or rest day as a true rest day", () => {
  assert.equal(classifySessionlessDay({ day_type: "rest", today_card: {} }).kind, "rest");
  assert.equal(classifySessionlessDay({ day_type: "recovery" } as never).kind, "rest");
  assert.equal(classifySessionlessDay(null).kind, "rest");
  assert.equal(classifySessionlessDay({} as never).coachLed, false);
  // A headline with no contact keyword still gets its own card (not "Rest day").
  const scheduled = classifySessionlessDay({ today_card: { headline: "Active recovery flush" } });
  assert.equal(scheduled.kind, "scheduled");
  assert.equal(scheduled.title, "Active recovery flush");
  assert.equal(scheduled.tag, null);
});

// --- field display rules ----------------------------------------------------

test("hides 0-second rest, shows positive rest", () => {
  assert.equal(shouldShowRest({ value: 0, unit: "seconds" }), false);
  assert.equal(shouldShowRest({ value: 180, unit: "seconds" }), true);
  assert.equal(shouldShowRest(null), false);
  assert.equal(shouldShowRest({} as never), false);
});

test("hides null / empty / meaningless loads, keeps real display", () => {
  assert.equal(formatBlockLoad(null), null);
  assert.equal(formatBlockLoad({} as never), null);
  assert.equal(formatBlockLoad({ display: "" }), null);
  assert.equal(formatBlockLoad({ display: "   " }), null);
  assert.equal(formatBlockLoad({ display: "n/a" }), null);
  assert.equal(formatBlockLoad({ display: "0" }), null);
  assert.equal(formatBlockLoad({ method: "percentage", value: 85, unit: "percent", display: "85% 1RM" }), "85% 1RM");
});

test("cleanText hides blank values", () => {
  assert.equal(cleanText(null), null);
  assert.equal(cleanText("  "), null);
  assert.equal(cleanText(42 as never), null);
  assert.equal(cleanText(" squat "), "squat");
});

test("formatMeasured renders value + unit, or null", () => {
  assert.equal(formatMeasured({ value: 45, unit: "minutes" }), "45 minutes");
  assert.equal(formatMeasured({ value: 30, unit: null }), "30");
  assert.equal(formatMeasured(null), null);
  assert.equal(formatMeasured({ unit: "minutes" } as never), null);
});

test("prefers duration over reps when reps looks like a time string", () => {
  assert.equal(isTimeLikeReps("5-6 min"), true);
  assert.equal(isTimeLikeReps("30s"), true);
  assert.equal(isTimeLikeReps("2:00 rounds"), true);
  assert.equal(isTimeLikeReps("4-6"), false);

  const timeReps = selectBlockMetric({
    reps: "5-6 min",
    duration: { value: 6, unit: "minutes" },
  } as never);
  assert.deepEqual(timeReps, [{ label: "Duration", value: "6 minutes" }]);

  const realReps = selectBlockMetric({ sets: 4, reps: "4-6" } as never);
  assert.deepEqual(realReps, [{ label: "Volume", value: "4 × 4-6" }]);

  assert.deepEqual(selectBlockMetric({} as never), []);
  assert.deepEqual(selectBlockMetric(null), []);
});

test("timed holds show their measured seconds instead of a one-rep volume", () => {
  assert.deepEqual(
    selectBlockMetric({
      display_name: "Punch-Specific Max Isometric Hold",
      sets: 3,
      reps: 1,
      duration: { value: 10, unit: "seconds" },
      rest: { value: 180, unit: "seconds" },
    } as never),
    [{ label: "Duration", value: "3 × 10 seconds" }],
  );
  assert.deepEqual(
    selectBlockMetric({
      display_name: "Single-rep strength drill",
      sets: 3,
      reps: 1,
      duration: { value: 10, unit: "seconds" },
    } as never),
    [
      { label: "Volume", value: "3 × 1" },
      { label: "Duration", value: "10 seconds" },
    ],
  );
  assert.deepEqual(
    selectBlockMetric({ sets: 3, duration: { value: 10, unit: "seconds" } } as never),
    [{ label: "Duration", value: "3 × 10 seconds" }],
  );
  assert.deepEqual(
    selectBlockMetric({ sets: 3, reps: 5, duration: { value: 10, unit: "seconds" } } as never),
    [
      { label: "Volume", value: "3 × 5" },
      { label: "Duration", value: "10 seconds" },
    ],
  );
  assert.deepEqual(selectBlockMetric({ rounds: 4, work: { value: 2, unit: "minutes" } } as never), [
    { label: "Rounds", value: "4" },
    { label: "Work", value: "2 minutes" },
  ]);
});

test("selectBlockMetric never drops a set count that only work or distance can carry", () => {
  // "Pull-Counter-Reset: 2 x 5 sec; rest 120 sec" converts to sets 2 + work 5 s
  // with no reps/duration. The card must read "2 × 5 seconds", not "5 seconds".
  assert.deepEqual(
    selectBlockMetric({ sets: 2, work: { value: 5, unit: "seconds" } } as never),
    [{ label: "Work", value: "2 × 5 seconds" }],
  );
  // Sets per distance with no work time: the distance row carries them.
  assert.deepEqual(
    selectBlockMetric({ sets: 4, distance: { value: 200, unit: "meters" } } as never),
    [{ label: "Distance", value: "4 × 200 meters" }],
  );
  // A single set needs no multiplier.
  assert.deepEqual(
    selectBlockMetric({ sets: 1, work: { value: 20, unit: "seconds" } } as never),
    [{ label: "Work", value: "20 seconds" }],
  );
  // Sets already printed on Volume are not repeated on Work.
  assert.deepEqual(
    selectBlockMetric({ sets: 3, reps: 5, work: { value: 20, unit: "seconds" } } as never),
    [
      { label: "Volume", value: "3 × 5" },
      { label: "Work", value: "20 seconds" },
    ],
  );
});

test("only a converted hard-sparring headline is flagged as converted", () => {
  for (const headline of [
    "Technical-only combat",
    "Technical combat",
    "Coach-led boxing — technical only",
    "Controlled fight-speed technical rounds",
    "Technical rhythm only",
    "Technical touch — pads / shadow",
    "Technical activation — no contact",
  ]) {
    assert.equal(isConvertedTechnicalHeadline(headline), true, headline);
  }
  for (const headline of ["Technical sparring", "Pad work", "Shadow boxing and footwork", "Skill drills"]) {
    assert.equal(isConvertedTechnicalHeadline(headline), false, headline);
    const view = classifySessionlessDay({ today_card: { headline }, sessions: [] } as never);
    assert.equal(view.kind, "technical", headline);
    assert.equal(view.converted, false, headline);
  }
});

test("an empty rest day cannot advertise an unscheduled training role", () => {
  assert.deepEqual(
    classifySessionlessDay({
      day_type: "rest",
      today_card: { headline: "Fight-pace conditioning" },
      sessions: [],
    } as never),
    { kind: "rest", title: "No planned session", tag: null, coachLed: false, converted: false },
  );
});

test("continuous prescriptions prefer duration and never render as volume", () => {
  assert.deepEqual(
    selectBlockMetric({
      sets: 1,
      reps: "continuous",
      duration: { value: 20, unit: "minutes" },
    } as never),
    [{ label: "Duration", value: "20 minutes" }],
  );
  assert.deepEqual(selectBlockMetric({ sets: 1, reps: "continuous" } as never), [
    { label: "Mode", value: "Continuous" },
  ]);
});

test("isTimeLikeReps treats bare metres as NOT time (metres/minutes ambiguity)", () => {
  // "5 m" / "5m" reps mean metres, not minutes — must not be coerced to duration.
  assert.equal(isTimeLikeReps("5 m"), false);
  assert.equal(isTimeLikeReps("400m"), false);
  // Unambiguous time units still match.
  assert.equal(isTimeLikeReps("2 min"), true);
  assert.equal(isTimeLikeReps("90 seconds"), true);
});

test("selectBlockMetric renders distance and rounds for conditioning blocks", () => {
  assert.deepEqual(selectBlockMetric({ distance: { value: 400, unit: "meters" } } as never), [
    { label: "Distance", value: "400 meters" },
  ]);
  assert.deepEqual(selectBlockMetric({ rounds: 8 } as never), [
    { label: "Rounds", value: "8" },
  ]);
  // Zero/negative rounds are hidden.
  assert.deepEqual(selectBlockMetric({ rounds: 0 } as never), []);
});

test("selectBlockMetric combines duration, distance, and rounds in order", () => {
  const metrics = selectBlockMetric({
    reps: "30s",
    duration: { value: 5, unit: "minutes" },
    distance: { value: 400, unit: "meters" },
    rounds: 8,
  } as never);
  assert.deepEqual(metrics, [
    { label: "Duration", value: "5 minutes" },
    { label: "Distance", value: "400 meters" },
    { label: "Rounds", value: "8" },
  ]);
});

test("selectBlockMetric drops a rep count that only echoes interval work seconds", () => {
  // "2x3s" converted to rounds 2, work 3 sec and a stray reps 3.
  assert.deepEqual(
    selectBlockMetric({ reps: 3, rounds: 2, work: { value: 3, unit: "seconds" } } as never),
    [
      { label: "Rounds", value: "2" },
      { label: "Work", value: "3 seconds" },
    ],
  );
  // A real per-round rep count that differs from the work seconds stays.
  assert.deepEqual(
    selectBlockMetric({ reps: 5, rounds: 2, work: { value: 20, unit: "seconds" } } as never),
    [
      { label: "Volume", value: "5" },
      { label: "Rounds", value: "2" },
      { label: "Work", value: "20 seconds" },
    ],
  );
  // With sets, the reps are a real sets x reps volume.
  assert.deepEqual(
    selectBlockMetric({ sets: 2, reps: 3, rounds: 2, work: { value: 3, unit: "seconds" } } as never),
    [
      { label: "Volume", value: "2 × 3" },
      { label: "Rounds", value: "2" },
      { label: "Work", value: "3 seconds" },
    ],
  );
});

// --- content presence: blocks / mindset / nutrition / red flags -------------

test("extracts session blocks from a valid plan", () => {
  const plan = validPlan();
  const blocks = getBlocks(getSessions(getDays(getWeeks(plan)[0])[0])[0]);
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].display_name, "Back Squat");
});

test("extracts mindset anchor lines", () => {
  const session = getSessions(getDays(getWeeks(validPlan())[0])[0])[0];
  const lines = getMindsetLines(session.mindset_anchor);
  assert.equal(lines.length, 3);
  assert.deepEqual(
    lines.map((line) => line.label),
    ["Intent", "Focus", "Reset"],
  );
});

test("detects nutrition when present, absent when empty", () => {
  assert.equal(hasNutrition(validPlan()), true);
  assert.equal(hasNutrition({ nutrition: {} } as never), false);
  assert.equal(hasNutrition({} as never), false);
});

test("surfaces displayable red flags only", () => {
  assert.equal(getDisplayableRedFlags(validPlan()).length, 1);
  assert.equal(getDisplayableRedFlags({ red_flag_rules: [{ rule_id: "x" }] } as never).length, 0);
  assert.equal(getDisplayableRedFlags({} as never).length, 0);
});

test("getStringList cleans, drops blanks, and tolerates null", () => {
  assert.deepEqual(getStringList(["Brace hard", "  ", "Knees out"]), ["Brace hard", "Knees out"]);
  assert.deepEqual(getStringList(null), []);
  assert.deepEqual(getStringList(undefined), []);
  assert.deepEqual(getStringList([]), []);
});

test("getMindsetLines surfaces the full mindset anchor with an accurate display label", () => {
  const lines = getMindsetLines({
    intent: "Move fast",
    focus_cue: "Drive",
    reset_cue: "Reset",
    confidence_anchor: "Banked the work",
    context: "First hard week",
  });
  assert.deepEqual(
    lines.map((l) => l.label),
    ["Intent", "Focus", "Reset", "Anchor", "Context"],
  );
  assert.deepEqual(
    lines.map((l) => l.value),
    ["Move fast", "Drive", "Reset", "Banked the work", "First hard week"],
  );
  // Empty anchor object yields no lines (renderer hides it).
  assert.deepEqual(getMindsetLines({}), []);
  assert.deepEqual(getMindsetLines(null), []);
});

test("getMindsetLines capitalises the first letter of each cue for display", () => {
  const lines = getMindsetLines({
    intent: "keep it light and fluid",
    focus_cue: "intentional explosive pulls, then full recovery",
    reset_cue: "stop if breathing becomes heavy",
    confidence_anchor: "you are protecting freshness",
    context: "short band work and mobility",
  });
  assert.deepEqual(
    lines.map((l) => l.value),
    [
      "Keep it light and fluid",
      "Intentional explosive pulls, then full recovery",
      "Stop if breathing becomes heavy",
      "You are protecting freshness",
      "Short band work and mobility",
    ],
  );
});

test("getSessionCoachingLines condenses mental framing into one Coach cue", () => {
  const lines = getSessionCoachingLines({
    intent: "stay loose",
    focus_cue: "clean rhythm",
    reset_cue: "breathe and reset",
    confidence_anchor: "rounds are banked",
    context: "Taper freshness day",
  });

  assert.deepEqual(
    lines.map((line) => line.label),
    ["Focus", "Reset", "Coach cue", "Context"],
  );
  assert.deepEqual(
    lines.map((line) => line.value),
    ["Clean rhythm", "Breathe and reset", "Stay loose · Rounds are banked", "Taper freshness day"],
  );
});

test("getSessionCoachingLines hides generic context but keeps coach dependencies", () => {
  const generic = getSessionCoachingLines({
    intent: "stay sharp",
    context: "SPP pocket planning for a brawler",
  });
  assert.deepEqual(generic.map((line) => line.label), ["Coach cue"]);

  const dependency = getSessionCoachingLines({
    context: "Pairs with coach-led technical session",
  });
  assert.deepEqual(dependency, [
    { label: "Context", value: "Pairs with coach-led technical session" },
  ]);

  const injuryConstraint = getSessionCoachingLines({
    context: "Modified for shoulder irritation",
  });
  assert.deepEqual(injuryConstraint, [
    { label: "Context", value: "Modified for shoulder irritation" },
  ]);
});

test("formatSessionObjective capitalises the subtitle without touching the rest", () => {
  assert.equal(
    formatSessionObjective("refine motor plan and composure without physical load"),
    "Refine motor plan and composure without physical load",
  );
  // Already sentence-case / acronym-led text is returned untouched.
  assert.equal(formatSessionObjective("Hold RPE 6 and stay smooth"), "Hold RPE 6 and stay smooth");
  assert.equal(formatSessionObjective("  keep the pace honest  "), "Keep the pace honest");
  // Blank / non-string objectives stay hidden.
  assert.equal(formatSessionObjective("   "), null);
  assert.equal(formatSessionObjective(null), null);
  assert.equal(formatSessionObjective(42), null);
});

// --- deterministic (Stage 1) nutrition + recovery (PR-6) -------------------

test("weekLabel keeps short goals verbatim but caps long ones to a glanceable heading", () => {
  // Short goal: returned untouched, trailing punctuation preserved.
  assert.equal(
    weekLabel({ week_index: 2, week_goal: "Convert strength into speed." } as never),
    "Week 2: Convert strength into speed.",
  );
  assert.equal(
    weekLabel({
      week_index: 1,
      week_goal: "Baseline and technical consistency: establish anchor execution quality",
    } as never),
    "Week 1: Baseline and technical consistency:…",
  );
  // Long multi-clause goal: cap the first clause when it still exceeds four words.
  assert.equal(
    weekLabel({
      week_index: 1,
      week_goal:
        "Build single-leg drive and balance; maintain punch speed and shoulder-friendly maintenance while preserving freshness.",
    } as never),
    "Week 1: Build single-leg drive and…",
  );
  // Goal with decimal numbers: should not split on the decimal point.
  assert.equal(
    weekLabel({
      week_index: 5,
      week_goal:
        "Build 1.5x bodyweight squat and power; maintain punch speed and shoulder-friendly maintenance.",
    } as never),
    "Week 5: Build 1.5x bodyweight squat…",
  );
  // Long single clause with no early break: hard-cap at 4 words with an ellipsis.
  assert.equal(
    weekLabel({
      week_index: 3,
      week_goal: "Sharpen reactive power speed timing and ring distance control",
    } as never),
    "Week 3: Sharpen reactive power speed…",
  );
  // No goal: just the week number.
  assert.equal(weekLabel({ week_index: 4 } as never), "Week 4");
});

test("countdown-led late-fight weeks infer missing titles and phase without overriding legacy values", () => {
  const compressed = {
    week_index: 1,
    countdown_start: "D-10",
    days: [{ countdown_label: "D-10" }, { countdown_label: "D-5" }],
  } as never;
  assert.deepEqual(inferredLateFightWeekContext(compressed), {
    goal: "Compressed Pre-Fight Week",
    phase: "TAPER",
  });
  assert.equal(weekLabel(compressed), "Week 1: Compressed Pre-Fight Week");
  assert.equal(resolvedWeekPhase(compressed), "TAPER");

  const legacy = {
    week_index: 1,
    phase_label: "SPP",
    week_goal: "Power transfer touch",
    countdown_start: "D-10",
  } as never;
  assert.equal(weekLabel(legacy), "Week 1: Power transfer touch");
  assert.equal(resolvedWeekPhase(legacy), "SPP");
});

test("fallback red flags require a real stop or escalation instruction", () => {
  const plan = {
    plan_notes: [
      {
        category: "weight_cut",
        label: "Lead notes",
        text: "Target weight is not set; coach owns the final weight-cut decision.",
      },
      {
        category: "injury",
        label: "Active notes",
        text: "Keep the elbow covered and use the programmed support.",
      },
      {
        category: "injury",
        label: "Safety",
        text: "Stop training and report any worsening wound or dizziness.",
      },
    ],
  } as never;

  assert.deepEqual(
    getFallbackSafetyNotes(plan).map((note) => note.label),
    ["Safety"],
  );
  assert.deepEqual(
    getActiveNotesExcludingRedFlags(plan).map((note) => note.label),
    ["Lead notes", "Active notes"],
  );
});

test("formatMacroRange handles full / max-only / min-only / empty", () => {
  assert.equal(formatMacroRange({ min: 112, max: 140 }, "g/day"), "112–140 g/day");
  assert.equal(formatMacroRange({ min: null, max: 350 }, "g/day"), "up to 350 g/day");
  assert.equal(formatMacroRange({ min: 30, max: null }, "g/day"), "from 30 g/day");
  assert.equal(formatMacroRange({ min: null, max: null, note: "x" }, "g/day"), null);
  assert.equal(formatMacroRange(null, "g/day"), null);
});

test("NutritionCard renders deterministic macros / hydration / fuel timing", () => {
  const plan = planWithDeterministicSupport();
  assert.equal(hasDeterministicNutrition(plan), true);
  const phases = getDeterministicNutritionPhases(plan);
  // Phase order is normalised (GPP before TAPER).
  assert.deepEqual(phases.map((p) => p.phase), ["GPP", "TAPER"]);

  const taper = phases.find((p) => p.phase === "TAPER")!;
  const rows = nutritionPhaseRows(taper.entry);
  const byLabel = Object.fromEntries(rows.map((r) => [r.label, r.value]));
  assert.equal(byLabel["Protein"], "126–175 g/day");
  assert.equal(byLabel["Carbs"], "up to 350 g/day (reduce before weigh-in)");
  assert.equal(byLabel["Fats"], "moderate (~20%)"); // note-only macro
  assert.equal(byLabel["Hydration"], "2100–2800 ml/day");
  assert.equal(byLabel["Meals"], "3 core meals + 2-3 snacks daily");
  assert.equal(byLabel["Fuel (pre)"], "light carbs");
  assert.equal(byLabel["Fatigue adjustment"], "high fatigue support");
  // Athlete-safe weight-cut: risk band + supervision only.
  assert.deepEqual(formatWeightCutBand(taper.entry.weight_cut), {
    band: "severe",
    supervisionRequired: true,
  });
  // No active cut -> no weight-cut line.
  const gpp = phases.find((p) => p.phase === "GPP")!;
  assert.equal(formatWeightCutBand(gpp.entry.weight_cut), null);
});

test("NutritionCard falls back to string fields when deterministic data is missing", () => {
  const plan = { nutrition: { summary: "Fuel around sessions." }, weeks: [] };
  assert.equal(hasDeterministicNutrition(plan), false);
  assert.deepEqual(getDeterministicNutritionPhases(plan), []);
  // Legacy prose still detected so the card still renders via fallback.
  assert.equal(hasNutrition(plan), true);
});

test("RecoveryCard renders deterministic sleep / fatigue / phase focus / core actions", () => {
  const plan = planWithDeterministicSupport();
  assert.equal(hasDeterministicRecovery(plan), true);
  const phases = getDeterministicRecoveryPhases(plan);
  assert.deepEqual(phases.map((p) => p.phase), ["GPP", "TAPER"]);

  const taper = recoveryPhaseView(phases.find((p) => p.phase === "TAPER")!.entry);
  assert.equal(taper.sleep, "8–9 h/night");
  assert.deepEqual(taper.coreStrategies, ["Breathwork"]);
  assert.deepEqual(taper.phaseFocus, ["Reduce volume to 30-40%"]);
  assert.deepEqual(taper.fatigue, ["Cut weekly volume by 25-40%"]);
  assert.deepEqual(taper.weightCut, { band: "severe", supervisionRequired: true });
});

test("RecoveryCard / NutritionCard never surface coach_gated even if present", () => {
  // The backend strips coach_gated, but the helpers must also only read known
  // athlete-safe fields — never echo an unexpected coach_gated payload.
  const entry = {
    phase: "TAPER",
    protein_g_per_day: { min: 126, max: 175 },
    core_strategies: ["Breathwork"],
    sleep_hours_target: [8, 9],
    weight_cut: { active: true, risk_band: "severe", supervision_required: true },
    coach_gated: { acute_cut_protocol: { bicarbonate_g_per_kg: "~0.3 g/kg" } },
  };
  const nutritionBlob = JSON.stringify(nutritionPhaseRows(entry));
  const recoveryBlob = JSON.stringify(recoveryPhaseView(entry));
  for (const blob of [nutritionBlob, recoveryBlob]) {
    assert.equal(blob.includes("bicarbonate"), false);
    assert.equal(blob.includes("coach_gated"), false);
    assert.equal(blob.includes("g/kg"), false);
  }
});

test("hasDeterministicRecovery is true when only age adjustments are present", () => {
  const plan = {
    weeks: [],
    deterministic_support: {
      recovery: {
        by_phase: {
          GPP: { phase: "GPP", age_adjustments: ["72h muscle-group rotation"] },
        },
      },
    },
  };
  // A phase with only age adjustments must still surface the RecoveryCard.
  assert.equal(hasDeterministicRecovery(plan), true);
  const view = recoveryPhaseView(getDeterministicRecoveryPhases(plan)[0]!.entry);
  assert.deepEqual(view.ageAdjustments, ["72h muscle-group rotation"]);
});

test("recovery/nutrition helpers tolerate missing / partial data", () => {
  assert.deepEqual(getDeterministicNutritionPhases(null), []);
  assert.deepEqual(getDeterministicRecoveryPhases({}), []);
  assert.deepEqual(nutritionPhaseRows(null), []);
  const view = recoveryPhaseView(null);
  assert.equal(view.sleep, null);
  assert.deepEqual(view.coreStrategies, []);
  assert.equal(view.weightCut, null);
});

test("redFlagView hides raw action enum but keeps a human action sentence", () => {
  const rawAction = redFlagView({
    display_text: "Sharp or progressive triceps pain.",
    action: "stop_and_report",
    severity: "red",
  });
  assert.equal(rawAction.text, "Sharp or progressive triceps pain.");
  assert.equal(rawAction.action, null);
  assert.equal(rawAction.severityLabel, "Red");

  const humanAction = redFlagView({
    display_text: "Sharp or progressive triceps pain.",
    action: "Stop training and report to medical staff.",
    severity: "amber",
  });
  assert.equal(humanAction.action, "Stop training and report to medical staff.");
  assert.equal(humanAction.severityLabel, "Amber");
});

test("redFlagView drops an action that merely repeats the display text", () => {
  const view = redFlagView({
    display_text: "Stop and report dizziness.",
    action: "Stop and report dizziness.",
    severity: "red",
  });
  assert.equal(view.action, null);
});

test("redFlagView tolerates missing fields", () => {
  const view = redFlagView({ display_text: "Something" });
  assert.equal(view.text, "Something");
  assert.equal(view.action, null);
  assert.equal(view.severityLabel, null);
  const empty = redFlagView(null);
  assert.equal(empty.text, null);
  assert.equal(empty.severityLabel, null);
});

test("splitMindsetLines keeps all mindset lines primary", () => {
  const { primary, secondary } = splitMindsetLines({
    intent: "Stay sharp",
    focus_cue: "Hands up",
    reset_cue: "Breathe",
    confidence_anchor: "You've done the rounds",
    context: "Final hard week",
  });
  assert.deepEqual(
    primary.map((line) => line.label),
    ["Intent", "Focus", "Reset", "Anchor", "Context"],
  );
  assert.deepEqual(secondary, []);
});

test("splitMindsetLines returns empty secondary for simplified mindset", () => {
  const { primary, secondary } = splitMindsetLines({ intent: "Go" });
  assert.deepEqual(primary.map((line) => line.label), ["Intent"]);
  assert.deepEqual(secondary, []);
});

// --- audit fixes: rest-day classification, measures, weight-cut de-emphasis ---

test("a rest/recovery day_type with an unclassified headline stays a rest day", () => {
  // "Full rest and mobility" carries no combat/coach vocabulary, so a rest
  // day_type keeps it a rest day rather than a generic "scheduled" day.
  const rest = classifySessionlessDay({
    day_type: "rest",
    today_card: { headline: "Full rest and mobility" },
  } as never);
  assert.equal(rest.kind, "rest");
  assert.equal(rest.title, "Full rest and mobility");

  const recovery = classifySessionlessDay({
    day_type: "recovery",
    today_card: { headline: "Easy reset and stretch" },
  } as never);
  assert.equal(recovery.kind, "rest");
});

test("a headline naming real coach/combat work overrides a rest day_type", () => {
  // day_type only rescues an UNCLASSIFIED headline; explicit combat wins.
  assert.equal(
    classifySessionlessDay({ day_type: "rest", today_card: { headline: "Hard sparring" } } as never)
      .kind,
    "sparring",
  );
  assert.equal(
    classifySessionlessDay({
      day_type: "recovery",
      today_card: { headline: "Coach-led boxing session" },
    } as never).kind,
    "coach_led",
  );
});

test("formatMeasured rejects non-finite and negative values", () => {
  assert.equal(formatMeasured({ value: Number.NaN, unit: "seconds" } as never), null);
  assert.equal(formatMeasured({ value: Number.POSITIVE_INFINITY, unit: "seconds" } as never), null);
  assert.equal(formatMeasured({ value: -30, unit: "seconds" } as never), null);
  assert.equal(formatMeasured({ value: 30, unit: "seconds" }), "30 seconds");
  assert.equal(formatMeasured({ value: 0, unit: "seconds" }), "0 seconds");
});

test("time-like reps without a separate duration are labelled Duration, not Volume", () => {
  const timeReps = selectBlockMetric({ display_name: "Hold", sets: 5, reps: "30 seconds" } as never);
  assert.deepEqual(timeReps[0], { label: "Duration", value: "5 × 30 seconds" });
  const roundTime = selectBlockMetric({ display_name: "Bag", sets: 5, reps: "2:00 rounds" } as never);
  assert.deepEqual(roundTime[0], { label: "Duration", value: "5 × 2:00 rounds" });
  // A genuine rep count still reads as Volume.
  const repCount = selectBlockMetric({ display_name: "Squat", sets: 3, reps: "8" } as never);
  assert.deepEqual(repCount[0], { label: "Volume", value: "3 × 8" });
});

// --- numeric validation: no NaN / Infinity / negative ever reaches the UI ----

test("finitePositiveNumber accepts only finite positive numbers", () => {
  for (const good of [1, 0.5, 12, 1e6]) {
    assert.equal(finitePositiveNumber(good), true, `${good} should pass`);
  }
  for (const bad of [0, -1, -0.5, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, "3", null, undefined, {}]) {
    assert.equal(finitePositiveNumber(bad as never), false, `${String(bad)} should fail`);
  }
});

test("selectBlockMetric drops malformed numeric sets / reps / rounds", () => {
  // Malformed reps-as-number never render as "NaN"/"Infinity"/"-5".
  for (const reps of [Number.NaN, Number.POSITIVE_INFINITY, -5, 0]) {
    const m = selectBlockMetric({ display_name: "Squat", sets: 3, reps } as never);
    assert.equal(
      m.some((x) => x.label === "Volume"),
      false,
      `reps ${String(reps)} should not produce a Volume line`,
    );
  }
  // Bare non-positive numeric-string reps are dropped too; a range survives.
  assert.deepEqual(selectBlockMetric({ display_name: "Squat", sets: 3, reps: "-5" } as never), []);
  assert.deepEqual(selectBlockMetric({ display_name: "Squat", sets: 3, reps: "0" } as never), []);
  assert.deepEqual(selectBlockMetric({ display_name: "Squat", reps: "4-6" } as never)[0], {
    label: "Volume",
    value: "4-6",
  });
  // A malformed sets multiplier is omitted, but a valid rep count still shows.
  const badSets = selectBlockMetric({ display_name: "Squat", sets: Number.NaN, reps: "8" } as never);
  assert.deepEqual(badSets[0], { label: "Volume", value: "8" });
  const infSets = selectBlockMetric({
    display_name: "Squat",
    sets: Number.POSITIVE_INFINITY,
    reps: "8",
  } as never);
  assert.deepEqual(infSets[0], { label: "Volume", value: "8" });
  // Malformed rounds never render a Rounds line.
  for (const rounds of [Number.NaN, Number.POSITIVE_INFINITY, -3, 0]) {
    const m = selectBlockMetric({ display_name: "Circuit", rounds } as never);
    assert.equal(m.some((x) => x.label === "Rounds"), false, `rounds ${String(rounds)} should be dropped`);
  }
  assert.deepEqual(selectBlockMetric({ display_name: "Circuit", rounds: 4 } as never), [
    { label: "Rounds", value: "4" },
  ]);
});

test("formatMeasured and formatEffort reject non-finite numbers", () => {
  assert.equal(formatMeasured({ value: Number.NaN, unit: "m" } as never), null);
  assert.equal(formatMeasured({ value: Number.POSITIVE_INFINITY, unit: "m" } as never), null);
  assert.equal(formatMeasured({ value: -1, unit: "m" } as never), null);
  // Effort: a non-finite numeric value is dropped rather than printed as "RPE NaN".
  assert.equal(formatEffort({ effort: { method: "RPE", value: Number.NaN } } as never), "RPE");
  assert.equal(formatEffort({ effort: { method: "RPE", value: Number.POSITIVE_INFINITY } } as never), "RPE");
  assert.equal(formatEffort({ effort: { method: "RPE", value: 7 } } as never), "RPE 7");
  // A non-numeric effort value still passes through, in plain words.
  assert.equal(formatEffort({ effort: { method: "Intent", value: "max" } } as never), "Max intent");
});

test("formatEffort never prints a raw enum or machine token", () => {
  const effort = (method: unknown, value?: unknown) =>
    formatEffort({ effort: { method, value } } as never);
  // The reported bug: the card read "intent max_speed".
  assert.equal(effort("intent", "max_speed"), "Max speed");
  assert.equal(effort("intent", "max"), "Max intent");
  assert.equal(effort("intent", "explosive"), "Explosive");
  assert.equal(effort("RIR", 2), "RIR 2");
  assert.equal(effort("velocity", 0.8), "0.8 m/s");
  assert.equal(effort("heart_rate_zone", 2), "Zone 2");
  assert.equal(effort("heart_rate_zone", "2-3"), "Zone 2-3");
  assert.equal(effort("heart_rate_zone", "zone_2"), "Zone 2");
  assert.equal(effort("pace", "conversational"), "Conversational pace");
  assert.equal(effort("pace", "5:00/km"), "Pace 5:00/km");
  assert.equal(effort("max_effort_percent", 80), "80% of max");
  // A method with no usable value names the scale, not the enum.
  assert.equal(effort("heart_rate_zone"), "Heart rate zone");
  assert.equal(effort("max_effort_percent"), "Max effort %");
  // Unknown methods and bare values are de-underscored too.
  assert.equal(effort("bar_speed", "fast"), "Bar speed fast");
  assert.equal(effort("", "max_speed"), "Max speed");
  // Messy model spellings of each scale.
  assert.equal(effort("intent", "MAX_SPEED"), "Max speed");
  assert.equal(effort("intent", 10), "Intent 10/10");
  // 1 is "full intent" from the model, never 1/10 on a power block.
  assert.equal(effort("intent", 1), "Max intent");
  assert.equal(effort("intent", "1"), "Max intent");
  assert.equal(effort("intent", 0.8), "80% intent");
  assert.equal(effort("intent", 8), "Intent 8/10");
  assert.equal(effort("intent", "100%"), "100% intent");
  assert.equal(effort("RPE", "RPE 7"), "RPE 7");
  assert.equal(effort("RPE", "rpe_7"), "RPE 7");
  assert.equal(effort("RIR", "2 RIR"), "RIR 2");
  assert.equal(effort("velocity", "0.8m/s"), "0.8 m/s");
  assert.equal(effort("heart_rate_zone", "Z2"), "Zone 2");
  assert.equal(effort("heart_rate_zone", "zone2"), "Zone 2");
  assert.equal(effort("heart_rate_zone", "z2_z3"), "Zone 2-3");
  assert.equal(effort("heart_rate_zone", "easy_aerobic"), "Easy aerobic zone");
  assert.equal(effort("max_effort_percent", 0.8), "80% of max");
  assert.equal(effort("max_effort_percent", "80-85%"), "80-85% of max");
  assert.equal(effort("intent", "_"), "Intent");
  assert.equal(formatMeasured({ value: 30, unit: "sec_per_side" } as never), "30 sec per side");
  for (const [method, value] of [
    ["intent", "max_speed"],
    ["heart_rate_zone", 2],
    ["max_effort_percent", 80],
  ]) {
    assert.equal(/_/.test(effort(method, value) ?? ""), false);
  }
});

test("macro ranges reject NaN / Infinity / negative values", () => {
  assert.equal(formatMacroRange({ min: Number.NaN, max: 140 } as never, "g/day"), "up to 140 g/day");
  assert.equal(
    formatMacroRange({ min: 100, max: Number.POSITIVE_INFINITY } as never, "g/day"),
    "from 100 g/day",
  );
  assert.equal(formatMacroRange({ min: -50, max: -10 } as never, "g/day"), null);
  assert.equal(formatMacroRange({ min: Number.NaN, max: Number.NaN } as never, "g/day"), null);
  assert.equal(formatMacroRange({ min: 112, max: 140 } as never, "g/day"), "112–140 g/day");
  // macroLine keeps the note when the numeric range is dropped as malformed.
  assert.equal(macroLine({ min: Number.NaN, note: "steady" } as never, "g/day"), "steady");
});

test("recovery sleep hours reject NaN and non-positive values", () => {
  assert.equal(recoveryPhaseView({ sleep_hours_target: [Number.NaN, 9] } as never).sleep, "9 h/night");
  assert.equal(recoveryPhaseView({ sleep_hours_target: [Number.NaN, Number.NaN] } as never).sleep, null);
  assert.equal(recoveryPhaseView({ sleep_hours_target: [0, -2] } as never).sleep, null);
  assert.equal(recoveryPhaseView({ sleep_hours_target: [8, 9] } as never).sleep, "8–9 h/night");
});

test("weekLabel ignores a non-finite week_index", () => {
  assert.equal(weekLabel({ week_index: Number.NaN } as never), "Week");
  assert.equal(weekLabel({ week_index: Number.POSITIVE_INFINITY } as never), "Week");
  assert.equal(weekLabel({ week_index: 3 } as never), "Week 3");
});

test("isNonFiniteNumericToken flags bare non-finite spellings, not ranges or times", () => {
  for (const token of [
    "NaN",
    "nan",
    "Infinity",
    "infinity",
    "+Infinity",
    "-Infinity",
    "+NaN",
    "-nan",
    "  Infinity  ",
  ]) {
    assert.equal(isNonFiniteNumericToken(token), true, `${token} should be flagged`);
  }
  for (const ok of ["4-6", "8", "30 seconds", "30s", "12-15 s", "Infinity reps", "RPE 7", "", "0"]) {
    assert.equal(isNonFiniteNumericToken(ok), false, `${ok} should NOT be flagged`);
  }
});

test("malformed numeric string tokens never render as reps / effort / load", () => {
  // reps as a non-finite string token → no Volume line at all.
  for (const reps of ["NaN", "Infinity", "+Infinity", "-Infinity"]) {
    const m = selectBlockMetric({ display_name: "Squat", sets: 3, reps } as never);
    assert.equal(
      m.some((x) => x.label === "Volume"),
      false,
      `reps "${reps}" should not render a Volume line`,
    );
  }
  // Valid ranges and time strings are preserved.
  assert.deepEqual(selectBlockMetric({ display_name: "Squat", sets: 3, reps: "4-6" } as never)[0], {
    label: "Volume",
    value: "3 × 4-6",
  });
  assert.deepEqual(selectBlockMetric({ display_name: "Hold", sets: 5, reps: "30 seconds" } as never)[0], {
    label: "Duration",
    value: "5 × 30 seconds",
  });
  // effort value as a non-finite string token → method only, never "RPE NaN".
  for (const value of ["NaN", "Infinity", "+Infinity", "-Infinity"]) {
    assert.equal(formatEffort({ effort: { method: "RPE", value } } as never), "RPE");
  }
  assert.equal(formatEffort({ effort: { method: "RPE", value: "6-7" } } as never), "RPE 6-7");
  // load display as a non-finite string token → hidden.
  for (const display of ["NaN", "Infinity", "-Infinity"]) {
    assert.equal(formatBlockLoad({ display } as never), null);
  }
  assert.equal(formatBlockLoad({ display: "70% 1RM" } as never), "70% 1RM");
});

const WEIGHT_CUT_TEXT = "If weight-cut symptoms worsen (dizziness), stop and escalate to medical.";

function planWithRiskBand(riskBand: string) {
  return {
    deterministic_support: {
      nutrition: { by_phase: { TAPER: { weight_cut: { risk_band: riskBand } } } },
    },
  } as never;
}

test("weight-cut symptom safety is de-emphasised ONLY when risk is explicitly below moderate", () => {
  // Explicitly below moderate → shown but softened.
  for (const band of ["low", "mild", "none", "inactive"]) {
    assert.equal(
      isDeEmphasisedWeightCutSafety(planWithRiskBand(band), WEIGHT_CUT_TEXT),
      true,
      `risk "${band}" should de-emphasise`,
    );
  }
  // Moderate and every synonym at that rank must render at full weight.
  for (const band of ["moderate", "medium", "amber", "elevated"]) {
    assert.equal(
      isDeEmphasisedWeightCutSafety(planWithRiskBand(band), WEIGHT_CUT_TEXT),
      false,
      `risk "${band}" must not de-emphasise`,
    );
  }
  // Above moderate is always full weight.
  for (const band of ["high", "severe", "red", "critical", "extreme", "aggressive"]) {
    assert.equal(
      isDeEmphasisedWeightCutSafety(planWithRiskBand(band), WEIGHT_CUT_TEXT),
      false,
      `risk "${band}" must not de-emphasise`,
    );
  }
});

test("weight-cut de-emphasis never fires on missing or unknown risk data", () => {
  // No plan / no deterministic support → risk unknown → never faded.
  assert.equal(isDeEmphasisedWeightCutSafety(null, WEIGHT_CUT_TEXT), false);
  assert.equal(isDeEmphasisedWeightCutSafety({} as never, WEIGHT_CUT_TEXT), false);
  assert.equal(isDeEmphasisedWeightCutSafety(planWithRiskBand(""), WEIGHT_CUT_TEXT), false);
  // An unrecognised band token is treated as unknown, not as low.
  assert.equal(isDeEmphasisedWeightCutSafety(planWithRiskBand("banana"), WEIGHT_CUT_TEXT), false);
  // A single at-or-above-moderate band anywhere pins the whole plan to full weight,
  // even when another phase reads low.
  const mixed = {
    deterministic_support: {
      nutrition: {
        by_phase: {
          GPP: { weight_cut: { risk_band: "low" } },
          TAPER: { weight_cut: { risk_band: "high" } },
        },
      },
    },
  } as never;
  assert.equal(isDeEmphasisedWeightCutSafety(mixed, WEIGHT_CUT_TEXT), false);
});

test("an explicit prominent severity overrides weight-cut de-emphasis even below moderate", () => {
  const low = planWithRiskBand("low");
  for (const severity of ["red", "critical", "high", "severe", "extreme"]) {
    assert.equal(
      isDeEmphasisedWeightCutSafety(low, WEIGHT_CUT_TEXT, severity),
      false,
      `severity "${severity}" must override de-emphasis`,
    );
    assert.equal(isProminentRedFlagSeverity(severity), true);
  }
  // A non-prominent (or absent) severity leaves the low-risk de-emphasis intact.
  assert.equal(isDeEmphasisedWeightCutSafety(low, WEIGHT_CUT_TEXT, "amber"), true);
  assert.equal(isDeEmphasisedWeightCutSafety(low, WEIGHT_CUT_TEXT, undefined), true);
  assert.equal(isProminentRedFlagSeverity("amber"), false);
  assert.equal(isProminentRedFlagSeverity(null), false);
});

test("weight-cut de-emphasis only applies to weight-cut symptom lines", () => {
  const low = planWithRiskBand("low");
  // Not a weight-cut symptom line at all → never de-emphasised.
  assert.equal(isDeEmphasisedWeightCutSafety(low, "Stop on sharp knee pain."), false);
  assert.equal(isDeEmphasisedWeightCutSafety(low, null), false);
  assert.equal(isDeEmphasisedWeightCutSafety(low, "   "), false);
});

test("compactTimeUnits shortens time words without changing the value", () => {
  assert.equal(compactTimeUnits("120 seconds"), "2 min");
  assert.equal(compactTimeUnits("180 seconds"), "3 min");
  assert.equal(compactTimeUnits("90 seconds"), "90 sec");
  assert.equal(compactTimeUnits("3 seconds"), "3 sec");
  assert.equal(compactTimeUnits("2 minutes"), "2 min");
  assert.equal(compactTimeUnits("1.5 minutes"), "1.5 min");
  assert.equal(compactTimeUnits("1 minute"), "1 min");
  // A range converts only when both ends are whole minutes.
  assert.equal(compactTimeUnits("60-120 seconds"), "1-2 min");
  assert.equal(compactTimeUnits("60-90 seconds"), "60-90 sec");
  // Everything around the time is left as written.
  assert.equal(compactTimeUnits("3 × 10 seconds"), "3 × 10 sec");
  assert.equal(compactTimeUnits("45 seconds per side"), "45 sec per side");
  assert.equal(compactTimeUnits("RPE 7"), "RPE 7");
  assert.equal(compactTimeUnits("20 m"), "20 m");
  assert.equal(compactTimeUnits("4 per side"), "4 per side");
});

test("formatBlockLoad spells a percentage or absolute load stored without display", () => {
  assert.equal(formatBlockLoad({ method: "percentage", value: 85, unit: "percent", ref: "1RM" }), "85% 1RM");
  assert.equal(formatBlockLoad({ method: "absolute", value: 100, unit: "kg" }), "100 kg");
  assert.equal(formatBlockLoad({ method: "rpe", value: 7 }), null);
  assert.equal(formatBlockLoad({ method: "percentage", value: Number.NaN, ref: "1RM" }), null);
  assert.equal(formatBlockLoad({ method: "percentage", value: 85, display: "n/a" }), null);
});

test("cluster helpers need two named components and a round count", () => {
  const pair = {
    rounds: 3,
    rest: { value: 240, unit: "seconds" },
    components: [{ name: "Heavy RDL", reps: 2 }, { name: "Broad Jump", reps: "2" }],
  };
  assert.deepEqual(getBlockComponents(pair).map((line) => [line.label, line.name, line.detail]),
    [["A1", "Heavy RDL", "2 reps"], ["A2", "Broad Jump", "2"]]);
  assert.equal(formatClusterSummary(pair), "3 rounds · rest 4 min");
  assert.equal(formatClusterSummary({ ...pair, rest: null, rest_note: "Full rest" }), "3 rounds · full rest");
  assert.deepEqual(getBlockComponents({ components: [{ name: "Heavy RDL", reps: 2 }] }), []);
  assert.equal(formatClusterSummary({ ...pair, rounds: null }), null);
  assert.equal(formatRestNote({ rest_note: "Full rest between holds" }), "Full");
  assert.equal(formatRestNote({ rest_note: "Walk back" }), "Walk back");
});

test("legacy raw contact label renders without altering converted-contact semantics", () => {
  const day = {
    countdown_label: "D-6",
    today_card: { headline: "Lower strength", coach_led_contact: "technical_combat" },
    sessions: [{ session_id: "strength", title: "Lower strength", blocks: [] }],
  };
  const contact = getCoachLedContactView(day);
  assert.equal(contact?.kind, "technical");
  assert.equal(contact?.title, "Technical rhythm only");
  assert.equal(contact?.converted, true);
  assert.equal(day.today_card.coach_led_contact, "technical_combat");

  const sessionless = classifySessionlessDay({
    countdown_label: "D-6", today_card: { headline: "technical_combat" }, sessions: [],
  });
  assert.equal(sessionless.kind, "technical");
  assert.equal(sessionless.title, "Technical rhythm only");

  const declared = getCoachLedContactView({
    planning_day_role_keys: ["light_combat_day"],
    today_card: { coach_led_contact: "technical_combat" },
  });
  assert.equal(declared?.kind, "light_combat");
  assert.equal(declared?.title, "Technical Combat");
  assert.equal(declared?.converted, false);
});
