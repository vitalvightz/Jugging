import test from "node:test";
import assert from "node:assert/strict";

import {
  buildLogEntry,
  deriveSessionOutcome,
  lastLoadFor,
  lastPerformanceFor,
  performanceSummary,
  progressionLoadFor,
  backoffLoadFor,
  plannedSessionRpe,
  withLastLoad,
  draftFromLog,
  hasLoggableNumbers,
  isLoggableBlock,
  logFieldsForBlock,
  logSummary,
  loggedValues,
} from "./exercise-log";
import type { ExerciseLogRecord, StructuredBlock } from "@/lib/types";

const deadlift: StructuredBlock = {
  block_id: "blk-2026-10-12-trap-bar-deadlift",
  block_type: "strength",
  display_name: "Trap Bar Deadlift",
  sets: 4,
  reps: 8,
  effort: { method: "RPE", value: 7 },
  rest: { value: 120, unit: "seconds" },
};

const plank: StructuredBlock = {
  block_id: "blk-2026-10-12-front-plank",
  block_type: "accessory",
  display_name: "Front Plank",
  duration: { value: 3, unit: "minutes" },
};

function log(block: StructuredBlock, overrides: Partial<ExerciseLogRecord>): ExerciseLogRecord {
  return {
    id: "log-1",
    athlete_id: "athlete-1",
    plan_id: "plan-1",
    session_id: "s1",
    block_id: block.block_id ?? "",
    exercise_key: null,
    training_day: "2026-10-12",
    status: "as_prescribed",
    reason: null,
    prescribed: block,
    actual: {},
    notes: "",
    created_at: "",
    updated_at: "",
    ...overrides,
  };
}

const keys = (block: StructuredBlock) => logFieldsForBlock(block).map((field) => field.key);

test("a lift is logged with sets, reps, the weight used and RPE", () => {
  assert.deepEqual(keys(deadlift), ["sets", "reps", "load", "effort"]);
  const load = logFieldsForBlock(deadlift).find((field) => field.key === "load");
  // The plan gives no absolute load, so there is nothing to compare it with.
  assert.equal(load?.prescribed, null);
  assert.equal(load?.unit, "kg");
});

test("every load type offers its own fields", () => {
  assert.deepEqual(keys(plank), ["duration", "effort"]);
  assert.deepEqual(
    keys({ block_type: "conditioning", rounds: 6, work: { value: 30, unit: "seconds" }, rest: { value: 30, unit: "seconds" } }),
    ["rounds", "work", "effort"],
  );
  // Sets of a sprint are not weighed; bare sets (a sled push) are.
  assert.deepEqual(keys({ block_type: "speed", sets: 4, distance: { value: 20, unit: "meters" } }), [
    "sets",
    "distance",
    "effort",
  ]);
  assert.deepEqual(keys({ block_type: "strength", sets: 3 }), ["sets", "load", "effort"]);
  // A bodyweight or band exercise has no weight to enter.
  assert.deepEqual(keys({ sets: 3, reps: 12, load: { method: "bodyweight", unit: "bodyweight" } }), [
    "sets",
    "reps",
    "effort",
  ]);
  // Nothing measurable: only how it felt.
  assert.deepEqual(keys({ block_type: "skill", display_name: "Shadow rounds" }), ["effort"]);
  assert.equal(hasLoggableNumbers(logFieldsForBlock({ display_name: "Shadow rounds" })), false);
});

test("reps written as a range, with a side note or as time are read correctly", () => {
  const reps = (value: StructuredBlock["reps"]) =>
    logFieldsForBlock({ sets: 3, reps: value }).find((field) => field.key === "reps");
  assert.deepEqual(reps("8-12")?.prescribed, [8, 12]);
  assert.equal(reps("8-12")?.hint, "8–12");
  assert.deepEqual(reps("5 each side")?.prescribed, [5, 5]);
  assert.equal(reps("30 seconds"), undefined);
  assert.equal(reps("AMRAP"), undefined);
  assert.equal(reps(0), undefined);
});

test("a free-text unit the API cannot store is not offered", () => {
  assert.deepEqual(keys({ duration: { value: 20, unit: "sec_per_side" } }), ["effort"]);
});

test("an empty entry is the exercise done as written", () => {
  assert.deepEqual(buildLogEntry(logFieldsForBlock(deadlift), {}), {
    status: "as_prescribed",
    actual: {},
    invalid: [],
    outliers: [],
  });
});

test("only what departs from the prescription is recorded, and it marks the log modified", () => {
  const entry = buildLogEntry(logFieldsForBlock(deadlift), { sets: "3", reps: "8", load: "80" });
  assert.equal(entry.status, "modified");
  assert.deepEqual(entry.actual, { sets: 3, load: { value: 80, unit: "kg" } });
  assert.deepEqual(entry.outliers, []);
});

test("the weight used and the RPE felt add to a log without making it a change", () => {
  const entry = buildLogEntry(logFieldsForBlock(deadlift), { load: "92,5", effort: "7" });
  assert.equal(entry.status, "as_prescribed");
  assert.deepEqual(entry.actual, { load: { value: 92.5, unit: "kg" } });
  // A different RPE from the one prescribed is a change.
  assert.equal(buildLogEntry(logFieldsForBlock(deadlift), { effort: "9" }).status, "modified");
});

test("a count inside a prescribed range is kept but is not a change", () => {
  const fields = logFieldsForBlock({ sets: 3, reps: "8-12" });
  assert.deepEqual(buildLogEntry(fields, { reps: "10" }), {
    status: "as_prescribed",
    actual: { reps: 10 },
    invalid: [],
    outliers: [],
  });
  assert.equal(buildLogEntry(fields, { reps: "6" }).status, "modified");
});

test("an entry far from the prescription is flagged for confirmation", () => {
  const far = buildLogEntry(logFieldsForBlock(plank), { duration: "15" });
  assert.equal(far.status, "modified");
  assert.deepEqual(far.actual, { duration: { value: 15, unit: "minutes" } });
  assert.deepEqual(far.outliers, ["15 min against 3 prescribed"]);
  // A modest change is saved without a second question.
  assert.deepEqual(buildLogEntry(logFieldsForBlock(plank), { duration: "4" }).outliers, []);
  assert.deepEqual(buildLogEntry(logFieldsForBlock(deadlift), { sets: "1" }).outliers, ["1 set against 4 prescribed"]);
  assert.deepEqual(buildLogEntry(logFieldsForBlock(deadlift), { load: "800" }).outliers, ["800 kg is a very heavy load"]);
});

test("entries that are not usable numbers are reported, not sent", () => {
  const entry = buildLogEntry(logFieldsForBlock(deadlift), { sets: "2.5", reps: "ten", load: "-5", effort: "11" });
  assert.deepEqual(entry.invalid, ["sets", "reps", "load", "effort"]);
  assert.deepEqual(entry.actual, {});
});

test("a saved log reads back as its draft, its values and its one-line summary", () => {
  const fields = logFieldsForBlock(deadlift);
  const changed = log(deadlift, {
    status: "modified",
    reason: "equipment",
    actual: { sets: 3, reps: 6, load: { value: 80, unit: "kg" } },
  });
  assert.deepEqual(draftFromLog(fields, changed), { sets: "3", reps: "6", load: "80" });
  assert.deepEqual(loggedValues(fields, changed), [
    { key: "sets", label: "Sets", value: "3", text: "3 sets", insteadOf: "4" },
    { key: "reps", label: "Reps", value: "6", text: "6 reps", insteadOf: "8" },
    { key: "load", label: "Load", value: "80 kg", text: "80 kg", insteadOf: null },
  ]);
  assert.equal(logSummary(fields, changed), "Changed · 3 sets · 6 reps · 80 kg");
  assert.equal(logSummary(fields, log(deadlift, {})), "Done");
  assert.equal(logSummary(fields, log(deadlift, { actual: { load: { value: 100, unit: "kg" } } })), "Done · 100 kg");
  assert.equal(logSummary(fields, log(deadlift, { status: "skipped", reason: "pain" })), "Skipped");
  assert.equal(
    logSummary(logFieldsForBlock(plank), log(plank, { status: "modified", actual: { duration: { value: 15, unit: "minutes" } } })),
    "Changed · 15 min",
  );
});

test("only blocks with a server id are loggable, and rehab never is", () => {
  assert.equal(isLoggableBlock(deadlift), true);
  assert.equal(isLoggableBlock({ ...deadlift, block_id: null }), false);
  assert.equal(isLoggableBlock({ ...deadlift, block_id: "  " }), false);
  assert.equal(isLoggableBlock({ block_id: "rehab:1", block_type: "rehab" }), false);
});

const visualisation: StructuredBlock = {
  block_id: "vis-1",
  block_type: "mindset",
  display_name: "Tactical Picture",
};

const day = [
  { session_id: "vis", optional: true, blocks: [visualisation] },
  { session_id: "s1", blocks: [deadlift, plank, { block_id: "rehab-1", block_type: "rehab", display_name: "Ankle" }] },
];

test("a session is done when every owed exercise was done as written", () => {
  const outcome = deriveSessionOutcome(day, {
    [deadlift.block_id!]: log(deadlift, { status: "as_prescribed" }),
    [plank.block_id!]: log(plank, { status: "as_prescribed" }),
  });
  // Optional visualisation and rehab are not owed.
  assert.deepEqual(
    { status: outcome.status, total: outcome.total, logged: outcome.logged, reason: outcome.reason },
    { status: "done", total: 2, logged: 2, reason: "" },
  );
});

test("changes and skips make it modified, with the reason written out", () => {
  const outcome = deriveSessionOutcome(day, {
    [deadlift.block_id!]: log(deadlift, { status: "modified", actual: { sets: 3, load: { value: 100, unit: "kg" } } }),
  });
  assert.equal(outcome.status, "modified");
  assert.deepEqual(outcome.unlogged.map((block) => block.block_id), [plank.block_id]);
  assert.equal(outcome.reason, "Changed: Trap Bar Deadlift (3 sets · 100 kg). Skipped: Front Plank.");
});

test("nothing done reads as a skipped session", () => {
  const outcome = deriveSessionOutcome(day, {
    [deadlift.block_id!]: log(deadlift, { status: "skipped" }),
  });
  assert.equal(outcome.status, "skipped");
  assert.equal(outcome.reason, "Skipped: Trap Bar Deadlift, Front Plank.");
});

test("a day with nothing to log is done", () => {
  assert.equal(deriveSessionOutcome([{ session_id: "vis", optional: true, blocks: [visualisation] }], {}).status, "done");
});

test("the session effort starts at the plan's middle RPE, on the 1-9 scale", () => {
  const rpe = (value: number): StructuredBlock => ({ ...deadlift, block_id: `b-${value}`, effort: { method: "RPE", value } });
  assert.equal(plannedSessionRpe([{ session_id: "s", blocks: [rpe(6), rpe(8), rpe(7)] }]), 7);
  assert.equal(plannedSessionRpe([{ session_id: "s", blocks: [rpe(10)] }]), 9);
  assert.equal(plannedSessionRpe([{ session_id: "s", blocks: [plank] }]), null);
});

test("last time's weight is carried into a done log, only when the plan names none", () => {
  const recent = [
    { exercise_key: null, display_name: "trap bar deadlift", load: { value: 100, unit: "kg" }, training_day: "2026-10-01" },
  ];
  assert.deepEqual(lastLoadFor(deadlift, recent), { value: 100, unit: "kg" });
  assert.deepEqual(withLastLoad(deadlift, { status: "as_prescribed" }, recent), {
    status: "as_prescribed",
    actual: { load: { value: 100, unit: "kg" } },
  });
  // A weight already given, a skip, a held plank, or a prescribed load is left alone.
  const given = { status: "modified" as const, actual: { load: { value: 90, unit: "kg" } } };
  assert.equal(withLastLoad(deadlift, given, recent), given);
  assert.deepEqual(withLastLoad(deadlift, { status: "skipped" }, recent), { status: "skipped" });
  assert.equal(lastLoadFor(plank, [{ ...recent[0], display_name: "Front Plank" }]), null);
  assert.equal(lastLoadFor({ ...deadlift, load: { value: 80, unit: "kg" } }, recent), null);
  // A different unit is never mixed in.
  assert.equal(lastLoadFor(deadlift, [{ ...recent[0], load: { value: 220, unit: "lb" } }]), null);
});


test("exercise history follows a stable key across plans and rejects keyed variants", () => {
  const movement = { ...deadlift, exercise_key: "trap-bar-deadlift" };
  const previous = log(deadlift, { plan_id: "old-plan", exercise_key: "trap-bar-deadlift",
    prescribed: deadlift, actual: { sets: 3, load: { value: 80, unit: "kg" } } });
  assert.equal(lastPerformanceFor(movement, [previous]), previous);
  assert.equal(lastPerformanceFor({ ...movement, exercise_key: "barbell-deadlift" }, [previous]), null);
  assert.equal(lastLoadFor({ ...movement, exercise_key: "barbell-deadlift" }, [{
    exercise_key: "trap-bar-deadlift", display_name: movement.display_name!, load: { value: 80, unit: "kg" }, training_day: "2026-10-01",
  }]), null);
  assert.match(performanceSummary(previous), /80 kg.*3 × 8/);
});

test("load suggestions require explicit progression, complete dose and reported effort", () => {
  const movement = { ...deadlift, progression_rule: "Add 2.5 kg when all sets complete." };
  const previous = log(deadlift, { prescribed: deadlift, actual: { load: { value: 80, unit: "kg" }, effort: { method: "RPE", value: 6 } } });
  assert.deepEqual(progressionLoadFor(movement, previous, true), { value: 82.5, unit: "kg" });
  assert.equal(progressionLoadFor(movement, previous, false), null);
  for (const patch of [
    { status: "skipped" as const }, { reason: "pain" as const }, { reason: "fatigue" as const },
    { actual: { ...previous.actual, sets: 3 } }, { actual: { ...previous.actual, effort: null } },
    { actual: { ...previous.actual, effort: { method: "RPE" as const, value: 9 } } },
  ]) assert.equal(progressionLoadFor(movement, { ...previous, ...patch }, true), null);
  for (const patch of [{ reps: 10 }, { load: { value: 60, unit: "kg" } }, { progression_rule: "Increase if comfortable" }]) {
    assert.equal(progressionLoadFor({ ...movement, ...patch }, previous, true), null);
  }
});

test("a struggled weight suggests easing off, never a raise", () => {
  const struggled = (patch: Partial<ExerciseLogRecord>) =>
    log(deadlift, { prescribed: deadlift, ...patch, actual: { load: { value: 80, unit: "kg" }, ...patch.actual } });
  // Fatigue, a missed set or rep, or an RPE two over the plan's each ease 10% onto a plate step.
  for (const patch of [
    { reason: "fatigue" as const, status: "modified" as const },
    { actual: { sets: 3 } },
    { actual: { reps: 6 } },
    { actual: { effort: { method: "RPE" as const, value: 9 } } },
  ]) assert.deepEqual(backoffLoadFor(deadlift, struggled(patch)), { value: 72.5, unit: "kg" });
  // Light dumbbell work eases in finer steps.
  assert.deepEqual(backoffLoadFor(deadlift, struggled({ reason: "fatigue", actual: { load: { value: 12, unit: "kg" } } })), { value: 11, unit: "kg" });
  // A session that went to plan, or ran at the planned effort, keeps last time's weight.
  for (const patch of [{}, { actual: { effort: { method: "RPE" as const, value: 8 } } }, { reason: "felt_strong" as const }]) {
    assert.equal(backoffLoadFor(deadlift, struggled(patch)), null);
  }
  // A stated reason outranks the numbers: missing kit or a strong day is not the weight being too much.
  assert.equal(backoffLoadFor(deadlift, struggled({ reason: "equipment", status: "modified", actual: { sets: 2 } })), null);
  assert.equal(backoffLoadFor(deadlift, struggled({ reason: "felt_strong", status: "modified", actual: { effort: { method: "RPE", value: 9 } } })), null);
  // "Done" records the eased weight the row offered, not last time's.
  const recent = [{ exercise_key: null, display_name: deadlift.display_name!, load: { value: 80, unit: "kg" }, training_day: "2026-10-01" }];
  assert.deepEqual(withLastLoad(deadlift, { status: "as_prescribed" }, recent, [struggled({ reason: "fatigue", status: "modified" })]),
    { status: "as_prescribed", actual: { load: { value: 72.5, unit: "kg" } } });
  assert.deepEqual(withLastLoad(deadlift, { status: "as_prescribed" }, recent, [struggled({})]),
    { status: "as_prescribed", actual: { load: { value: 80, unit: "kg" } } });
  // Pain and skips are handled by not carrying the weight over at all.
  for (const patch of [{ reason: "pain" as const }, { status: "skipped" as const }]) {
    assert.equal(backoffLoadFor(deadlift, struggled({ ...patch, actual: { sets: 2 } })), null);
  }
  // A changed dose, a plan-set weight or a different unit is not second-guessed.
  for (const block of [{ ...deadlift, reps: 5 }, { ...deadlift, sets: 3 }, { ...deadlift, load: { value: 60, unit: "kg" } }]) {
    assert.equal(backoffLoadFor(block, struggled({ reason: "fatigue" })), null);
  }
  assert.equal(backoffLoadFor({ ...deadlift, load: { method: "absolute", unit: "lb" } }, struggled({ reason: "fatigue" })), null);
  assert.equal(backoffLoadFor(deadlift, null), null);
});

test("last painful or skipped occurrence cannot resurrect an older load", () => {
  const recent = [{ exercise_key: null, display_name: deadlift.display_name!, load: { value: 80, unit: "kg" }, training_day: "2026-10-01" }];
  for (const patch of [{ reason: "pain" as const }, { status: "skipped" as const }]) {
    assert.deepEqual(withLastLoad(deadlift, { status: "as_prescribed" }, recent, [log(deadlift, patch)]), { status: "as_prescribed" });
  }
});


test("history displays recorded measurement units without relabelling them", () => {
  const previous = log(deadlift, { actual: { load: { value: 175, unit: "lb" } }, prescribed: deadlift });
  assert.match(performanceSummary(previous), /175 lb/);
  assert.equal(performanceSummary(previous).includes("175 kg"), false);
});

const fiveK: StructuredBlock = { block_id: "run-1", block_type: "conditioning", display_name: "Easy run", distance: { value: 5, unit: "km" } };

test("a continuous run offers its distance and an optional clock time", () => {
  assert.deepEqual(keys(fiveK), ["distance", "duration", "effort"]);
  const time = logFieldsForBlock(fiveK).find((field) => field.key === "duration")!;
  assert.equal(time.label, "Time");
  assert.equal(time.clock, true);
  // Sprints, intervals and runs with a prescribed time are unchanged.
  assert.deepEqual(keys({ block_type: "speed", sets: 4, distance: { value: 20, unit: "meters" } }), ["sets", "distance", "effort"]);
  assert.deepEqual(keys({ block_type: "conditioning", distance: { value: 200, unit: "meters" } }), ["distance", "effort"]);
  assert.deepEqual(keys({ ...fiveK, duration: { value: 30, unit: "minutes" } }), ["duration", "distance", "effort"]);
});

test("a run time is recorded in seconds without counting as a change", () => {
  const fields = logFieldsForBlock(fiveK);
  const entry = buildLogEntry(fields, { duration: "24:35" });
  assert.equal(entry.status, "as_prescribed");
  assert.deepEqual(entry.actual, { duration: { value: 1475, unit: "seconds" } });
  assert.deepEqual(buildLogEntry(fields, { duration: "24.35" }).actual, { duration: { value: 1475, unit: "seconds" } });
  assert.deepEqual(buildLogEntry(fields, { duration: "24.5" }).invalid, ["duration"]);
  // A changed distance is a change, as for any exercise.
  assert.equal(buildLogEntry(fields, { distance: "6", duration: "30:10" }).status, "modified");
});

test("a saved run time reads back as a clock", () => {
  const saved = log(fiveK, { actual: { distance: { value: 5, unit: "km" }, duration: { value: 1475, unit: "seconds" } } });
  const fields = logFieldsForBlock(fiveK);
  assert.equal(draftFromLog(fields, saved).duration, "24:35");
  assert.equal(logSummary(fields, saved), "Done · 5 km · 24:35");
  assert.equal(performanceSummary(saved), "5 km · 24:35");
});
