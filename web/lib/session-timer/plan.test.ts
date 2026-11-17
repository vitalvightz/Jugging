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

test("rehab holds become timed sets", () => {
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
