import test from "node:test";
import assert from "node:assert/strict";

import {
  blockToTimerItem,
  buildTimerItems,
  formatClock,
  formatRange,
  measuredSeconds,
  parseCountRange,
  parseDurationRange,
  plannedSparringIntensity,
  dayTimerItems,
  timerSessionFor,
  withoutSparringItems,
  workPhaseLabel,
  type TimerItem,
} from "./plan.ts";
import { advance, completeSet, createTimerState, startItem } from "./engine.ts";
import type { StructuredBlock, StructuredSession } from "../types.ts";

test("measured values convert to seconds and reject unknown units", () => {
  assert.equal(measuredSeconds({ value: 3, unit: "min" }), 180);
  assert.equal(measuredSeconds({ value: 45, unit: "seconds" }), 45);
  assert.equal(measuredSeconds({ value: 90, unit: "s" }), 90);
  assert.equal(measuredSeconds({ value: 400, unit: "m_distance" }), null);
  assert.equal(measuredSeconds({ value: 5, unit: null }), null);
  assert.equal(measuredSeconds({ value: Number.NaN, unit: "s" }), null);
});

test("count and duration ranges parse exact and ranged doses", () => {
  assert.deepEqual(parseCountRange("3-5"), { min: 3, max: 5 });
  assert.deepEqual(parseCountRange("4"), { min: 4, max: 4 });
  assert.equal(parseCountRange("5-3"), null);
  assert.deepEqual(parseDurationRange("90-120 s"), { min: 90, max: 120 });
  assert.deepEqual(parseDurationRange("2-3 min"), { min: 120, max: 180 });
  assert.deepEqual(parseDurationRange("30s"), { min: 30, max: 30 });
  assert.deepEqual(parseDurationRange("1:30"), { min: 90, max: 90 });
  assert.equal(parseDurationRange("easy"), null);
});

test("sparring rounds use the plan's round and rest length", () => {
  const item = blockToTimerItem(
    {
      block_type: "sparring",
      display_name: "Hard sparring",
      rounds: 6,
      work: { value: 3, unit: "min" },
      rest: { value: 60, unit: "seconds" },
    },
    0,
  );
  assert.equal(item?.kind, "interval");
  assert.ok(item?.kind === "interval");
  assert.equal(item.rounds, 6);
  assert.equal(item.workSec, 180);
  assert.equal(item.restSec, 60);
  assert.equal(item.sparring, true);
  assert.equal(item.needsSetup, false);
});

test("a long block duration with rounds is split per round, a short one is per round", () => {
  const total = blockToTimerItem(
    { block_type: "conditioning", display_name: "Bike", rounds: 4, duration: { value: 20, unit: "min" } },
    0,
  );
  assert.ok(total?.kind === "interval");
  assert.equal(total.workSec, 300);
  const perRound = blockToTimerItem(
    { block_type: "conditioning", display_name: "Bag", rounds: 5, duration: { value: 2, unit: "min" } },
    0,
  );
  assert.ok(perRound?.kind === "interval");
  assert.equal(perRound.workSec, 120);
});

test("rounds without a length are flagged for setup instead of silently timed", () => {
  const item = blockToTimerItem({ block_type: "sparring", display_name: "Sparring", rounds: 5 }, 0);
  assert.ok(item?.kind === "interval");
  assert.equal(item.needsSetup, true);
  assert.equal(item.rounds, 5);
});

test("strength sets keep an exact dose and rest", () => {
  const block: StructuredBlock = {
    block_type: "strength",
    display_name: "Trap bar deadlift",
    sets: 4,
    reps: 3,
    rest: { value: 120, unit: "seconds" },
    load: { display: "80% 1RM" },
    effort: { method: "RPE", value: 7 },
  };
  const item = blockToTimerItem(block, 0);
  assert.ok(item?.kind === "sets");
  assert.deepEqual(item.sets, { min: 4, max: 4 });
  assert.deepEqual(item.restSec, { min: 120, max: 120 });
  assert.equal(item.holdSec, null);
  assert.equal(item.detail, "3 reps · 80% 1RM · RPE 7");
});

test("a set and rest range from the source plan stays a range", () => {
  const source = [
    "### D-21",
    "- Trap bar deadlift — 3-5 sets x 3 reps @ RPE 7, rest 90-120 s",
  ].join("\n");
  const item = blockToTimerItem(
    { block_type: "strength", display_name: "Trap bar deadlift", sets: 4, reps: 3 },
    0,
    { sourceText: source, countdown: "D-21" },
  );
  assert.ok(item?.kind === "sets");
  assert.deepEqual(item.sets, { min: 3, max: 5 });
  assert.deepEqual(item.restSec, { min: 90, max: 120 });
});

test("missing sets and rest stay unknown rather than invented", () => {
  const item = blockToTimerItem({ block_type: "accessory", display_name: "Band pull-apart", reps: "15" }, 0);
  assert.ok(item?.kind === "sets");
  assert.equal(item.sets, null);
  assert.equal(item.restSec, null);
});

test("timed sets come from the plan's time fields, not the exercise name", () => {
  const fromDuration = blockToTimerItem(
    {
      block_type: "rehab",
      display_name: "Copenhagen plank hold",
      sets: 3,
      reps: 1,
      duration: { value: 20, unit: "seconds" },
      rest: { value: 45, unit: "seconds" },
    },
    0,
  );
  assert.ok(fromDuration?.kind === "sets");
  assert.equal(fromDuration.holdSec, 20);
  const fromReps = blockToTimerItem(
    { block_type: "rehab", display_name: "Wall sit", sets: 2, reps: "30s" },
    0,
  );
  assert.ok(fromReps?.kind === "sets");
  assert.equal(fromReps.holdSec, 30);
});

test("guidance blocks are skipped and dose-less blocks become tasks", () => {
  assert.equal(blockToTimerItem({ block_type: "nutrition", display_name: "Carbs" }, 0), null);
  assert.equal(blockToTimerItem({ block_type: "mindset", display_name: "Visualise" }, 0), null);
  const task = blockToTimerItem({ block_type: "mobility_activation", display_name: "Hip flow" }, 0);
  assert.equal(task?.kind, "task");
  const timed = blockToTimerItem(
    { block_type: "preparation", display_name: "Warm-up", duration: { value: 10, unit: "min" } },
    0,
  );
  assert.ok(timed?.kind === "interval");
  assert.equal(timed.rounds, 1);
  assert.equal(timed.workSec, 600);
});

test("items from several sessions get unique ids", () => {
  const block: StructuredBlock = { block_id: "b1", block_type: "strength", display_name: "Squat", sets: 3, reps: 5 };
  const sessions = [{ blocks: [block] }, { blocks: [block] }] as StructuredSession[];
  const items = buildTimerItems(sessions);
  assert.equal(items.length, 2);
  assert.notEqual(items[0].id, items[1].id);
});

test("formatting helpers", () => {
  assert.equal(formatClock(185), "3:05");
  assert.equal(formatClock(0), "0:00");
  assert.equal(formatRange({ min: 3, max: 5 }), "3–5");
  assert.equal(formatRange({ min: 4, max: 4 }), "4");
});

const LOWER_POWER: StructuredSession = {
  session_id: "s1",
  title: "Lower power",
  blocks: [{ block_type: "strength", display_name: "Trap bar jump", sets: 4, reps: 3 }],
};
const SPRINT: StructuredSession = {
  session_id: "s2",
  title: "Sprint",
  blocks: [
    {
      block_type: "conditioning",
      display_name: "Hill sprints",
      rounds: 6,
      work: { value: 10, unit: "s" },
      rest: { value: 90, unit: "s" },
    },
  ],
};
const TACTICAL_FOCUS: StructuredSession = {
  session_id: "s3",
  title: "Tactical Focus",
  blocks: [{ block_type: "mindset", display_name: "Pocket Exchange Map" }],
};

test("a zero-load Tactical Focus session has no timer items, never generic rounds", () => {
  assert.deepEqual(buildTimerItems([TACTICAL_FOCUS]), []);
  assert.deepEqual(dayTimerItems([TACTICAL_FOCUS], "s3"), []);
});

test("the timer runs the whole day, logged as one session", () => {
  const day = [LOWER_POWER, SPRINT, TACTICAL_FOCUS];
  // Starting the day from its primary session times every timeable block.
  const items = dayTimerItems(day, "s1");
  assert.deepEqual(items.map((item) => item.title), ["Trap bar jump", "Hill sprints"]);
  // A session id that is not one of the day's gets no timer.
  assert.deepEqual(dayTimerItems(day, "s9"), []);
});

test("a session that cannot be identified confidently gets no timer", () => {
  assert.equal(timerSessionFor([LOWER_POWER, SPRINT], "missing"), null);
  assert.equal(timerSessionFor([LOWER_POWER, SPRINT], null), null);
  assert.equal(timerSessionFor([LOWER_POWER, { ...SPRINT, session_id: "s1" }], "s1"), null);
  // Several id-less sessions are ambiguous; a lone id-less (legacy) session is not.
  const unnamed = { ...LOWER_POWER, session_id: null };
  assert.equal(timerSessionFor([unnamed, { ...SPRINT, session_id: null }], "2026-09-25"), null);
  assert.equal(timerSessionFor([unnamed], "2026-09-25"), unnamed);
  // A lone session with a different explicit id is not silently adopted.
  assert.equal(timerSessionFor([LOWER_POWER], "s9"), null);
});

test("planned sparring intensity comes only from the block's structured field", () => {
  assert.equal(plannedSparringIntensity({ block_type: "sparring", intensity: "hard" }), "hard");
  assert.equal(plannedSparringIntensity({ block_type: "sparring", intensity: "Light" }), "light");
  assert.equal(plannedSparringIntensity({ block_type: "sparring", intensity: "technical" }), "technical");
  assert.equal(plannedSparringIntensity({ block_type: "sparring", intensity: "moderate" }), "contact");
  // Free text is not guessed at.
  assert.equal(plannedSparringIntensity({ block_type: "sparring", intensity: "go hard-ish today" }), null);
  assert.equal(plannedSparringIntensity({ block_type: "sparring" }), null);
  const item = blockToTimerItem(
    { block_type: "sparring", display_name: "Sparring", rounds: 5, work: { value: 3, unit: "min" }, intensity: "hard" },
    0,
  );
  assert.ok(item?.kind === "interval");
  assert.equal(item.plannedIntensity, "hard");
});

test("distance is shown on every kind of timer item", () => {
  const sled = blockToTimerItem(
    {
      block_type: "strength",
      display_name: "Sled Pull Backward",
      sets: 3,
      distance: { value: 20, unit: "meters" },
      rest: { value: 90, unit: "seconds" },
      effort: { method: "RPE", value: 7 },
    },
    0,
  );
  assert.ok(sled?.kind === "sets");
  assert.equal(sled.detail, "20 m · RPE 7");

  const repeats = blockToTimerItem(
    {
      block_type: "conditioning",
      display_name: "Shuttle runs",
      rounds: 6,
      work: { value: 30, unit: "s" },
      distance: { value: 200, unit: "m" },
    },
    0,
  );
  assert.ok(repeats?.kind === "interval");
  assert.equal(repeats.detail, "200 m");

  const run = blockToTimerItem(
    { block_type: "conditioning", display_name: "Tempo run", duration: { value: 20, unit: "min" }, distance: { value: 3, unit: "km" } },
    0,
  );
  assert.ok(run?.kind === "interval");
  assert.equal(run.detail, "3 km");

  const carry = blockToTimerItem(
    { block_type: "accessory", display_name: "Farmer walk", distance: { value: 40, unit: "yards" }, load: { display: "2 × 32 kg" } },
    0,
  );
  assert.equal(carry?.kind, "task");
  assert.equal(carry?.detail, "40 yd · 2 × 32 kg");

  // A zero or malformed distance is not printed.
  const none = blockToTimerItem(
    { block_type: "strength", display_name: "Squat", sets: 3, reps: 5, distance: { value: 0, unit: "m" } },
    0,
  );
  assert.equal(none?.detail, "5 reps");
});

test("a duration beside reps is kept in the detail instead of dropped", () => {
  const item = blockToTimerItem(
    { block_type: "skill", display_name: "Shadow boxing drill", sets: 2, reps: "10", duration: { value: 5, unit: "minutes" } },
    0,
  );
  assert.ok(item?.kind === "sets");
  assert.equal(item.holdSec, null);
  assert.equal(item.detail, "10 reps · 5 min");
});

test("continuous / AMRAP work is timed from its duration, not counted as sets", () => {
  const steady = blockToTimerItem(
    { block_type: "conditioning", display_name: "Zone 2 bike", reps: "continuous", duration: { value: 30, unit: "minutes" } },
    0,
  );
  assert.ok(steady?.kind === "interval");
  assert.equal(steady.workSec, 1800);
  assert.equal(steady.needsSetup, false);
  assert.equal(steady.detail, "continuous");

  const amrap = blockToTimerItem(
    { block_type: "strength", display_name: "Circuit", reps: "AMRAP", duration: { value: 8, unit: "min" } },
    0,
  );
  assert.ok(amrap?.kind === "interval");
  assert.equal(amrap.workSec, 480);
  assert.equal(amrap.detail, "AMRAP");

  // With sets, the mode and its per-set time are both shown; it is not a hold.
  const sets = blockToTimerItem(
    { block_type: "strength", display_name: "Push-ups", sets: 3, reps: "AMRAP", duration: { value: 60, unit: "seconds" } },
    0,
  );
  assert.ok(sets?.kind === "sets");
  assert.equal(sets.holdSec, null);
  assert.equal(sets.detail, "AMRAP · 60 s");
});

test("rounds of a distance with no time are tapped done, not given a made-up round clock", () => {
  const item = blockToTimerItem(
    {
      block_type: "conditioning",
      display_name: "Shuttle sprints",
      rounds: 6,
      distance: { value: 200, unit: "meters" },
      rest: { value: 90, unit: "seconds" },
      effort: { method: "RPE", value: 8 },
    },
    0,
  );
  assert.ok(item?.kind === "sets");
  assert.equal(item.unit, "round");
  assert.deepEqual(item.sets, { min: 6, max: 6 });
  assert.deepEqual(item.restSec, { min: 90, max: 90 });
  assert.equal(item.holdSec, null);
  assert.equal(item.detail, "200 m · RPE 8");
  // A timed round is still a round clock, and sparring always is.
  const timed = blockToTimerItem(
    { block_type: "conditioning", display_name: "Row", rounds: 4, work: { value: 60, unit: "s" }, distance: { value: 250, unit: "m" } },
    0,
  );
  assert.equal(timed?.kind, "interval");
  const spar = blockToTimerItem(
    { block_type: "sparring", display_name: "Sparring", rounds: 5, distance: { value: 1, unit: "m" } },
    0,
  );
  assert.equal(spar?.kind, "interval");
});

test("the dose is also carried as chips: the target first, then load and effort", () => {
  const item = blockToTimerItem(
    {
      block_type: "strength",
      display_name: "Trap bar deadlift",
      sets: 4,
      reps: "3-5",
      load: { display: "80% 1RM" },
      effort: { method: "RPE", value: 7 },
    },
    0,
  );
  assert.deepEqual(item?.stats, [
    { kind: "target", value: "3–5 reps" },
    { kind: "load", value: "80% 1RM" },
    { kind: "effort", value: "RPE 7" },
  ]);
  const mode = blockToTimerItem(
    { block_type: "conditioning", display_name: "Bike", reps: "continuous", duration: { value: 20, unit: "min" } },
    0,
  );
  assert.deepEqual(mode?.stats, [{ kind: "target", value: "Continuous" }]);
  // A hold's time is on the clock, so it is not repeated as a chip.
  const hold = blockToTimerItem({ block_type: "rehab", display_name: "Wall sit", sets: 2, reps: "30s" }, 0);
  assert.deepEqual(hold?.stats, []);
});

test("after a head impact the day's timer keeps every block except its sparring rounds", () => {
  const interval = (id: string, sparring: boolean): TimerItem => ({ kind: "interval", id, title: id, detail: null,
    blockType: sparring ? "sparring" : "conditioning", rounds: 3, workSec: 120, restSec: 60, sparring, needsSetup: false });
  const items = [interval("spar", true), interval("bag", false)];
  assert.deepEqual(withoutSparringItems(items).map((item) => item.id), ["bag"]);
  assert.deepEqual(withoutSparringItems([interval("bag", false)]).map((item) => item.id), ["bag"]);
});

test("contrast clusters are tapped rounds with movement prescriptions and between-round rest", () => {
  const block: StructuredBlock = {
    block_id: "contrast", block_type: "strength", display_name: "Heavy RDL → Broad Jump",
    rounds: 3, rest: { value: 4, unit: "min" },
    components: [
      { name: "Heavy RDL", reps: 2, load: { display: "85% 1RM" } },
      { name: "Broad Jump", reps: 2, effort: { method: "intent", value: "max" } },
    ],
  };
  const item = blockToTimerItem(block, 0);
  assert.equal(item?.kind, "sets");
  if (item?.kind !== "sets") return;
  assert.deepEqual(item.sets, { min: 3, max: 3 });
  assert.deepEqual(item.planned, { field: "rounds", min: 3 });
  assert.equal(item.unit, "round");
  assert.equal(item.holdSec, null);
  assert.deepEqual(item.restSec, { min: 240, max: 240 });
  assert.equal(item.components?.[0].detail, "2 reps · 85% 1RM");
  assert.equal(item.components?.[1].detail, "2 reps · Max intent");
  assert.equal(item.setFields, undefined);
  let state = startItem(createTimerState([item]), 1000).state;
  assert.equal(state.phaseMs, null); // No invented work clock.
  state = advance(state, 181000).state;
  assert.equal(state.phase, "work");
  for (let round = 1; round <= 3; round++) {
    state = completeSet(state, 181000 + (round - 1) * 240000).state;
    assert.equal(state.completed[0], round);
    assert.equal(state.phase, round === 3 ? "done" : "rest");
    if (round < 3) state = advance(state, 181000 + round * 240000).state;
  }

  const noCount = blockToTimerItem({ ...block, rounds: null, rest: null }, 0);
  assert.ok(noCount?.kind === "sets");
  assert.equal(noCount.sets, null);
  assert.equal(noCount.restSec, null);
  const sparring = blockToTimerItem({ ...block, block_type: "sparring" }, 0);
  assert.equal(sparring?.kind, "interval");
});

// Every block type the planners, builders and importers write, plus unknown,
// empty and missing ones. Coach-created, AI-generated, imported and manual
// programmes all reach the timer through blockToTimerItem.
const EVERY_BLOCK_TYPE: Array<string | null | undefined> = [
  "strength", "strength_speed", "plyometric_power", "speed", "accessory", "conditioning",
  "rehab", "preparation", "mobility_activation", "cooldown_recovery", "skill", "sparring",
  "exercise", "training", "power", "not_a_real_type", "", null, undefined,
];
const EVERY_EXERCISE_TYPE = ["strength", "bodyweight", "power", "other", null, undefined] as const;
// Names that the old keyword rules read as holds, lifts, rounds or activities.
const KEYWORD_NAMES = [
  "Copenhagen plank hold", "Farmer carry", "Isometric mid-thigh pull", "Deadlift", "Power clean",
  "Hard sparring rounds", "Pad work", "Shadow boxing 3 x 3 min", "Bike sprint 6 x 30s", "Roadwork 5 km",
  "Bear crawl", "",
];
const DOSES: Array<{ name: string; dose: Partial<StructuredBlock> }> = [
  { name: "sets of reps", dose: { sets: 3, reps: 5, rest: { value: 90, unit: "seconds" } } },
  { name: "sets of a duration", dose: { sets: 3, duration: { value: 30, unit: "seconds" } } },
  { name: "one rep with a duration", dose: { sets: 3, reps: 1, duration: { value: 20, unit: "seconds" } } },
  { name: "reps with a duration", dose: { sets: 2, reps: 8, duration: { value: 40, unit: "seconds" } } },
  { name: "reps written as time", dose: { sets: 2, reps: "45s" } },
  { name: "rounds of work", dose: { rounds: 4, work: { value: 2, unit: "min" }, rest: { value: 1, unit: "min" } } },
  { name: "rounds of a distance", dose: { rounds: 6, distance: { value: 200, unit: "m" } } },
  { name: "a duration alone", dose: { duration: { value: 10, unit: "min" } } },
  { name: "no dose", dose: {} },
];

function withoutTitle(item: TimerItem | null) {
  if (!item) return item;
  const { title: _title, ...rest } = item;
  return rest;
}

test("every exercise type: the name never changes the timer, and is kept as written", () => {
  for (const block_type of EVERY_BLOCK_TYPE) {
    for (const exercise_type of EVERY_EXERCISE_TYPE) {
      for (const { name: doseName, dose } of DOSES) {
        const base = { block_id: "b", block_type, exercise_type, ...dose } as StructuredBlock;
        const neutral = blockToTimerItem({ ...base, display_name: "Exercise A" }, 0);
        for (const display_name of KEYWORD_NAMES) {
          const item = blockToTimerItem({ ...base, display_name }, 0);
          const where = `${String(block_type)} / ${String(exercise_type)} / ${doseName} / "${display_name}"`;
          assert.deepEqual(withoutTitle(item), withoutTitle(neutral), where);
          if (!item) continue;
          // The original name, untouched; only a missing one gets the placeholder.
          assert.equal(item.title, display_name || "Block", where);
          assert.ok(["Spar", "Work"].includes(workPhaseLabel(item)), where);
        }
      }
    }
  }
});

test("every exercise type: Spar only for sparring structure, Work otherwise", () => {
  for (const block_type of EVERY_BLOCK_TYPE) {
    const where = String(block_type);
    const lift = blockToTimerItem({ block_type, display_name: "Deadlift", sets: 3, reps: 5 }, 0);
    // Sets of reps are never called a lift, whatever the block type.
    assert.equal(workPhaseLabel(lift!), "Work", where);
    const hold = blockToTimerItem({ block_type, display_name: "Plank hold", sets: 3, duration: { value: 30, unit: "s" } }, 0);
    if (block_type === "sparring") assert.equal(workPhaseLabel(hold!), "Spar", where);
    else if (["conditioning", "skill"].includes(block_type ?? "")) assert.equal(workPhaseLabel(hold!), "Work", where);
    else assert.equal(workPhaseLabel(hold!), "Work", where);
    const rounds = blockToTimerItem({ block_type, display_name: "Pad work", rounds: 4, work: { value: 2, unit: "min" } }, 0);
    assert.equal(workPhaseLabel(rounds!), block_type === "sparring" ? "Spar" : "Work", where);
    const untimed = blockToTimerItem({ block_type, display_name: "Hard sparring" }, 0);
    if (untimed) {
      assert.equal(workPhaseLabel(untimed), block_type === "sparring" ? "Spar" : "Work", where);
    }
  }
});

test("no dose in the plan: nothing is read from the name, and nothing is invented", () => {
  for (const block_type of EVERY_BLOCK_TYPE) {
    for (const display_name of KEYWORD_NAMES) {
      const item = blockToTimerItem({ block_type, display_name }, 0);
      const where = `${String(block_type)} / "${display_name}"`;
      if (!item) continue;
      if (item.kind === "interval") {
        // Only a sparring block is a round clock without a dose, and it asks for the length.
        assert.equal(block_type, "sparring", where);
        assert.equal(item.needsSetup, true, where);
        assert.equal(item.planned?.min, 1, where);
      } else {
        assert.equal(item.kind, "task", where);
        assert.deepEqual(item.stats, [], where);
        assert.equal(item.planned, null, where);
      }
    }
  }
});

test("a one-rep set with a duration is timed by its fields alone, and never called a hold", () => {
  for (const display_name of ["Copenhagen plank hold", "Sled push", "Single-rep strength drill"]) {
    const item = blockToTimerItem({ block_type: "rehab", display_name, sets: 3, reps: 1, duration: { value: 20, unit: "seconds" } }, 0);
    assert.ok(item?.kind === "sets");
    assert.equal(item.holdSec, 20, display_name);
    assert.equal(workPhaseLabel(item), "Work", display_name);
  }
  // More than one rep: a counted set, with the duration kept as a stat.
  const counted = blockToTimerItem({ display_name: "Plank hold", sets: 3, reps: 8, duration: { value: 20, unit: "seconds" } }, 0);
  assert.ok(counted?.kind === "sets");
  assert.equal(counted.holdSec, null);
  assert.deepEqual(counted.stats, [{ kind: "target", value: "8 reps" }, { kind: "target", value: "20 s" }]);
});
