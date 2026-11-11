import test from "node:test";
import assert from "node:assert/strict";

import {
  buildLogEntry,
  deriveSessionOutcome,
  lastLoadFor,
  lastPerformanceFor,
  performanceSummary,
  progressionLoadFor,
  progressionOpen,
  progressionRepRange,
  nextStepFor,
  nextStepLabel,
  needsFirstLoad,
  asksForFirstLoad,
  amendedLog,
  exerciseIdentity,
  feelOf,
  backoffLoadFor,
  plannedSessionRpe,
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
  // The server has cleared this lift to progress (see api/rep_range.py).
  progression_hold: false,
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
  assert.deepEqual(entry.actual, { load: { value: 92.5, unit: "kg" }, effort: { method: "RPE", value: 7 } });
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

test("last weight is available as a suggestion but never added by completion", () => {
  const recent = [
    { exercise_key: null, display_name: "trap bar deadlift", load: { value: 100, unit: "kg" }, training_day: "2026-10-01" },
  ];
  assert.deepEqual(lastLoadFor(deadlift, recent), { value: 100, unit: "kg" });
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

test("a raise is earned by the work done, not by the wording of a progression rule", () => {
  const done = (actual: ExerciseLogRecord["actual"], patch: Partial<ExerciseLogRecord> = {}) =>
    log(deadlift, { prescribed: deadlift, ...patch, actual: { load: { value: 80, unit: "kg" }, ...actual } });
  // Every set and rep done and it felt easy: a small plate-friendly raise, with no rule on the block.
  assert.deepEqual(nextStepFor(deadlift, done({ feel: "easy" }), true), { kind: "load", load: { value: 82.5, unit: "kg" } });
  assert.deepEqual(progressionLoadFor(deadlift, done({ feel: "easy" }), true), { value: 82.5, unit: "kg" });
  // A low reported RPE or "felt strong" reads as easy when the athlete gave no answer.
  assert.equal(nextStepFor(deadlift, done({ effort: { method: "RPE", value: 6 } }), true)?.kind, "load");
  assert.equal(nextStepFor(deadlift, done({ reps: 10 }, { status: "modified", reason: "felt_strong" }), true)?.kind, "load");
  // A bare "done" is never taken as easy; "right" holds; "hard" is not a raise.
  for (const actual of [{}, { feel: "right" as const }, { feel: "hard" as const }, { effort: { method: "RPE" as const, value: 8 } }]) {
    assert.equal(nextStepFor(deadlift, done(actual), true), null);
  }
  // The athlete's own answer outranks the RPE they typed.
  assert.equal(nextStepFor(deadlift, done({ feel: "right", effort: { method: "RPE", value: 5 } }), true), null);
  // Restrictions close the engine itself, whatever the performance.
  assert.equal(nextStepFor(deadlift, done({ feel: "easy" }), false), null);
  // Missed work, a deviation, a skip, or no weight on record earns nothing.
  for (const previous of [
    done({ feel: "easy", sets: 3 }), done({ feel: "easy", reps: 6 }),
    done({ feel: "easy" }, { status: "skipped" }), done({ feel: "easy" }, { reason: "pain" }),
    done({ feel: "easy" }, { reason: "fatigue" }), done({ feel: "easy" }, { reason: "equipment" }),
    log(deadlift, { prescribed: deadlift, actual: { feel: "easy" } }),
  ]) assert.equal(nextStepFor(deadlift, previous, true), null);
  // A changed dose, a lower planned effort, a plan-set weight or a hold instruction is respected.
  for (const patch of [
    { reps: 10 }, { sets: 3 }, { effort: { method: "RPE", value: 5 } }, { load: { value: 60, unit: "kg" } },
    { progression_rule: "Keep the same dose and make each rep sharp." }, { progression_rule: "none this block" },
    { progression_rule: "Maintain the listed working dose." }, { progression_rule: "Do not add load this week." },
  ]) assert.equal(nextStepFor({ ...deadlift, ...patch }, done({ feel: "easy" }), true), null);
  // Other coaching copy does not gate it.
  assert.equal(nextStepFor({ ...deadlift, progression_rule: "Increase if comfortable" }, done({ feel: "easy" }), true)?.kind, "load");
  // About 3%, never less than one plate step; light dumbbells move in finer steps.
  const raised = (value: number, unit = "kg") => progressionLoadFor(deadlift, done({ feel: "easy", load: { value, unit } }), true)?.value;
  assert.deepEqual([40, 100, 140, 200, 12].map((value) => raised(value)), [42.5, 102.5, 145, 205, 12.5]);
  assert.equal(progressionLoadFor({ ...deadlift, load: { method: "absolute", unit: "lb" } },
    done({ feel: "easy", load: { value: 135, unit: "lb" } }), true)?.value, 140);
});

test("inside a rep range reps come first, and a one-tap done counts as the bottom of it", () => {
  const squat: StructuredBlock = { ...deadlift, sets: 3, reps: "6-8" };
  const done = (actual: ExerciseLogRecord["actual"]) =>
    log(squat, { prescribed: squat, actual: { load: { value: 40, unit: "kg" }, ...actual } });
  // 6 reps, easy: try 7. A bare done with "easy" is the prescribed floor, not the top.
  assert.deepEqual(nextStepFor(squat, done({ reps: 6, feel: "easy" }), true), { kind: "reps", reps: 7 });
  assert.deepEqual(nextStepFor(squat, done({ feel: "easy" }), true), { kind: "reps", reps: 7 });
  assert.equal(nextStepLabel({ kind: "reps", reps: 7 }), "Try 7 reps");
  // 8 on every set, easy: a little more weight.
  assert.deepEqual(nextStepFor(squat, done({ reps: 8, feel: "easy" }), true), { kind: "load", load: { value: 42.5, unit: "kg" } });
  assert.equal(nextStepLabel({ kind: "load", load: { value: 42.5, unit: "kg" } }), "Try 42.5 kg");
  // 7 reps, right: maintain and build towards 8. Top of the range but only "right": hold.
  assert.equal(nextStepFor(squat, done({ reps: 7, feel: "right" }), true), null);
  assert.equal(nextStepFor(squat, done({ reps: 8, feel: "right" }), true), null);
  // Sets logged one by one: the weakest set decides, and an uncounted set earns nothing.
  const rows = (reps: (number | null)[]) => done({ feel: "easy", sets: reps.length,
    set_entries: reps.map((count, index) => ({ set_number: index + 1, reps: count, load: { value: 40, unit: "kg" } })) });
  assert.deepEqual(nextStepFor(squat, rows([8, 8, 7]), true), { kind: "reps", reps: 8 });
  assert.equal(nextStepFor(squat, rows([8, 8, 8]), true)?.kind, "load");
  assert.equal(nextStepFor(squat, rows([8, null, 8]), true), null);
  // Rep steps need no weight on record (bodyweight work).
  assert.deepEqual(nextStepFor(squat, log(squat, { prescribed: squat, actual: { reps: 6, feel: "easy" } }), true), { kind: "reps", reps: 7 });
});

test("feedback is interpreted consistently with recorded RPE against the plan", () => {
  const felt = (patch: Partial<ExerciseLogRecord>) => feelOf(log(deadlift, { prescribed: deadlift, ...patch }));
  assert.equal(felt({ actual: { feel: "hard", effort: { method: "RPE", value: 5 } } }), "right");
  assert.equal(felt({ actual: { effort: { method: "RPE", value: 7 } } }), "easy");
  assert.equal(felt({ actual: { effort: { method: "RPE", value: 8 } } }), "right");
  assert.equal(felt({ actual: { effort: { method: "RPE", value: 9 } } }), "hard");
  assert.equal(felt({ reason: "felt_strong", status: "modified", actual: { reps: 10 } }), "easy");
  assert.equal(felt({}), null);
});

test("a green day never outranks taper, the final fortnight, an injury or a hold", () => {
  const open = { decisionTier: "green", openInjuries: 0, phase: "SPP", safetyHold: false, fightDate: "2026-12-20", trainingDay: "2026-11-10" };
  assert.equal(progressionOpen(open), true);
  // An open plan has no fight date.
  assert.equal(progressionOpen({ ...open, phase: "GPP", fightDate: undefined }), true);
  for (const patch of [
    { decisionTier: "modify" }, { decisionTier: "not_checked_in" }, { decisionTier: undefined },
    { openInjuries: 1 }, { phase: "TAPER" }, { phase: "" }, { safetyHold: true },
    { fightDate: "2026-11-24" }, { fightDate: "2026-11-12" }, { fightDate: "not a date" }, { trainingDay: "" },
  ]) assert.equal(progressionOpen({ ...open, ...patch }), false, JSON.stringify(patch));
  assert.equal(progressionOpen({ ...open, fightDate: "2026-11-25" }), true);
});

test("the first weight is asked once for weighted strength work with none on record", () => {
  const done = log(deadlift, {});
  const recent = [{ exercise_key: null, display_name: "trap bar deadlift", load: { value: 100, unit: "kg" }, training_day: "2026-10-01" }];
  assert.equal(needsFirstLoad(deadlift, []), true);
  assert.equal(asksForFirstLoad(deadlift, done, []), true);
  // Not before the history has loaded, not with a weight on record, not before the exercise is logged.
  assert.equal(needsFirstLoad(deadlift, undefined), false);
  assert.equal(needsFirstLoad(deadlift, recent), false);
  assert.equal(asksForFirstLoad(deadlift, done, recent), false);
  assert.equal(asksForFirstLoad(deadlift, done, undefined), false);
  assert.equal(asksForFirstLoad(deadlift, done, [{ ...recent[0], display_name: "Back Squat" }]), true);
  assert.equal(asksForFirstLoad(deadlift, null, []), false);
  // A skip, a weight already given, or sets logged one by one are left alone.
  assert.equal(asksForFirstLoad(deadlift, log(deadlift, { status: "skipped" }), []), false);
  assert.equal(asksForFirstLoad(deadlift, log(deadlift, { actual: { load: { value: 60, unit: "kg" } } }), []), false);
  assert.equal(asksForFirstLoad(deadlift, log(deadlift, { actual: { sets: 1, set_entries: [{ reps: 8 }] } }), []), true);
  // Jumps, holds, bodyweight or band work, a plan-set weight and the athlete's own exercises are never asked.
  for (const block of [
    { ...deadlift, block_type: "plyometric_power" }, plank, { ...deadlift, load: { method: "bodyweight" } },
    { ...deadlift, load: { method: "band" } }, { ...deadlift, load: { value: 80, unit: "kg" } },
    { ...deadlift, prescription_source: "athlete" as const },
  ]) assert.equal(needsFirstLoad(block, []), false);
  // A percentage of an unknown max is not a weight: the real one is still asked for.
  assert.equal(needsFirstLoad({ ...deadlift, load: { method: "percentage", value: 70, unit: "percent" } }, []), true);
  assert.equal(exerciseIdentity({ ...deadlift, exercise_key: "trap-bar-deadlift" }), "trap-bar-deadlift");
  assert.equal(exerciseIdentity(deadlift), "trap bar deadlift");
});

test("a saved log is amended in place, keeping everything else as stored", () => {
  const saved = log(deadlift, { status: "modified", reason: "equipment", notes: "short bar",
    actual: { sets: 3, reps: null, load: null, effort: { method: "RPE", value: 7 }, feel: "right" } });
  assert.deepEqual(amendedLog(saved, { load: { value: 60, unit: "kg" } }), {
    status: "modified", reason: "equipment", notes: "short bar",
    actual: { sets: 3, effort: { method: "RPE", value: 7 }, feel: "right", load: { value: 60, unit: "kg" } },
  });
  // Tapping the chosen feel again clears it.
  assert.deepEqual(amendedLog(saved, { feel: null }).actual, { sets: 3, effort: { method: "RPE", value: 7 } });
});

test("a struggled weight suggests easing off, never a raise", () => {
  const struggled = (patch: Partial<ExerciseLogRecord>) =>
    log(deadlift, { prescribed: deadlift, ...patch, actual: { load: { value: 80, unit: "kg" }, ...patch.actual } });
  // Fatigue and missed sets or reps ease 10% onto a plate step.
  for (const patch of [
    { reason: "fatigue" as const, status: "modified" as const },
    { actual: { sets: 3 } },
    { actual: { reps: 6 } },
  ]) assert.deepEqual(backoffLoadFor(deadlift, struggled(patch)), { value: 72.5, unit: "kg" });
  assert.deepEqual(backoffLoadFor(deadlift, struggled({ actual: { effort: { method: "RPE", value: 9 } } })), { value: 75, unit: "kg" });
  // Light dumbbell work eases in finer steps.
  assert.deepEqual(backoffLoadFor(deadlift, struggled({ reason: "fatigue", actual: { load: { value: 12, unit: "kg" } } })), { value: 11, unit: "kg" });
  // A session that went to plan, or ran at the planned effort, keeps last time's weight.
  for (const patch of [{}, { actual: { effort: { method: "RPE" as const, value: 8 } } }, { reason: "felt_strong" as const }]) {
    assert.equal(backoffLoadFor(deadlift, struggled(patch)), null);
  }
  // A stated reason outranks the numbers: missing kit or a strong day is not the weight being too much.
  assert.equal(backoffLoadFor(deadlift, struggled({ reason: "equipment", status: "modified", actual: { sets: 2 } })), null);
  assert.equal(backoffLoadFor(deadlift, struggled({ reason: "felt_strong", status: "modified", actual: { effort: { method: "RPE", value: 9 } } })), null);
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

test("a fixed working number progresses inside the server's permitted range", () => {
  // As generated: reps stays 8 (the prescription); rep_range is the bank's 8-12.
  const squat: StructuredBlock = { ...deadlift, sets: 3, reps: 8, rep_range: [8, 12] };
  assert.deepEqual(progressionRepRange(squat), [8, 12]);
  const done = (actual: ExerciseLogRecord["actual"]) =>
    log(squat, { prescribed: squat, actual: { load: { value: 60, unit: "kg" }, ...actual } });
  // A one-tap done counts as the prescribed 8, never the top.
  assert.deepEqual(nextStepFor(squat, done({ feel: "easy" }), true), { kind: "reps", reps: 9 });
  assert.deepEqual(nextStepFor(squat, done({ reps: 11, feel: "easy" }), true), { kind: "reps", reps: 12 });
  assert.equal(nextStepFor(squat, done({ reps: 12, feel: "easy" }), true)?.kind, "load");
  // An explicit answer outranks the RPE; with no answer the RPE still counts.
  assert.equal(nextStepFor(squat, done({ reps: 9, feel: "right", effort: { method: "RPE", value: 5 } }), true), null);
  assert.deepEqual(nextStepFor(squat, done({ reps: 9, effort: { method: "RPE", value: 6 } }), true), { kind: "reps", reps: 10 });
  // A range the prescription sits outside, or a malformed one, is ignored.
  assert.deepEqual(progressionRepRange({ ...squat, rep_range: [10, 12] }), [8, 8]);
  assert.deepEqual(progressionRepRange({ ...squat, rep_range: [12, 8] }), [8, 8]);
  // Legacy fixed reps can be compared with the current permitted range.
  const legacy = log(squat, { prescribed: { ...squat, rep_range: null }, actual: { feel: "easy" } });
  assert.deepEqual(nextStepFor(squat, legacy, true), { kind: "reps", reps: 9 });
});

test("the server's hold stops every suggestion, and only strength work progresses this way", () => {
  const squat: StructuredBlock = { ...deadlift, sets: 3, reps: 8, rep_range: [8, 12] };
  const done = log(squat, { prescribed: squat, actual: { load: { value: 60, unit: "kg" }, feel: "easy" } });
  assert.equal(nextStepFor({ ...squat, progression_hold: true }, done, true), null);
  // Unknown authority never earns an automatic increase.
  assert.equal(nextStepFor({ ...squat, progression_hold: undefined }, done, true), null);
  assert.equal(nextStepFor({ ...squat, progression_hold: null }, done, true), null);
  for (const block_type of ["plyometric_power", "speed", "conditioning", "strength_speed"]) {
    assert.equal(nextStepFor({ ...squat, block_type }, { ...done, prescribed: { ...squat, block_type } }, true), null);
  }
});

test("reps inside the permitted range are the workout done, not a change", () => {
  const squat: StructuredBlock = { ...deadlift, sets: 3, reps: 8, rep_range: [8, 12] };
  const fields = logFieldsForBlock(squat);
  assert.deepEqual(fields.find((field) => field.key === "reps")?.permitted, [8, 12]);
  const nine = buildLogEntry(fields, { reps: "9" });
  assert.equal(nine.status, "as_prescribed");
  assert.equal(nine.actual.reps, 9);
  // Outside the range, either way, is still a change.
  assert.equal(buildLogEntry(fields, { reps: "13" }).status, "modified");
  assert.equal(buildLogEntry(fields, { reps: "7" }).status, "modified");
  // Without a permitted range nothing changes: 9 against a fixed 8 is a change.
  assert.equal(buildLogEntry(logFieldsForBlock(deadlift), { reps: "9" }).status, "modified");
  assert.equal(logFieldsForBlock(deadlift).find((field) => field.key === "reps")?.permitted, undefined);
  // And it reads back without "not 8".
  const saved = log(squat, { actual: { reps: 9 } });
  assert.equal(loggedValues(fields, saved).find((value) => value.key === "reps")?.insteadOf, null);
});


test("per-set progression needs every prescribed set, valid effort and known weights", () => {
  const block = { ...deadlift, sets: 3, reps: 8, rep_range: [8, 12] };
  const previous = log(block, { prescribed: { ...block, rep_range: null },
    actual: { sets: 3, set_entries: [115, 120, 125].map(value => ({
      reps: 12, load: { value, unit: "kg" }, rpe: 6,
    })) } });
  assert.equal(nextStepFor(block, previous, true)?.kind, "load");
  assert.equal(nextStepFor(block, { ...previous, actual: { ...previous.actual,
    set_entries: previous.actual.set_entries!.slice(0, 2) } }, true), null);
  for (const rpe of [0, -1, 11, NaN]) {
    assert.equal(nextStepFor(block, { ...previous, actual: { ...previous.actual,
      effort: { method: "RPE", value: rpe },
      set_entries: previous.actual.set_entries!.map(row => ({ ...row, rpe })) } }, true), null);
  }
  assert.equal(nextStepFor(block, { ...previous, actual: { ...previous.actual,
    set_entries: previous.actual.set_entries!.map(row => ({ ...row, rpe: undefined })) } }, true), null);
});

test("completed planned-hard work holds while excessive effort backs off despite conflicting feedback", () => {
  const block = { ...deadlift, effort: { method: "RPE", value: 9 } };
  const completed = log(block, { prescribed: block, actual: { load: { value: 100, unit: "kg" },
    effort: { method: "RPE", value: 9 }, feel: "hard" } });
  assert.equal(feelOf(completed), "right");
  assert.equal(backoffLoadFor(block, completed), null);
  const maxTarget = { ...block, effort: { method: "RPE", value: 10 } };
  assert.equal(backoffLoadFor(maxTarget, { ...completed, prescribed: maxTarget,
    actual: { ...completed.actual, effort: { method: "RPE", value: 10 } } }), null);
  assert.equal(feelOf({ ...completed, actual: { ...completed.actual, feel: "easy" } }), "right");
  for (const feel of ["easy", "right", "hard"] as const) {
    const excessive = log(deadlift, { actual: { load: { value: 100, unit: "kg" },
      effort: { method: "RPE", value: 9 }, feel } });
    assert.equal(feelOf(excessive), "hard");
    assert.equal(nextStepFor(deadlift, excessive, true), null);
    assert.deepEqual(backoffLoadFor(deadlift, excessive), { value: 95, unit: "kg" });
  }
  assert.deepEqual(buildLogEntry(logFieldsForBlock(block), { effort: "9" }).actual.effort, { method: "RPE", value: 9 });
  assert.equal(buildLogEntry(logFieldsForBlock(block), { effort: "" }).actual.effort, undefined);
});

test("only confirmed missed sets trigger a reduction; sparse measurements do not prove missed work", () => {
  const actual = { load: { value: 100, unit: "kg" }, set_entries: [
    { set_number: 2, reps: 8, load: { value: 100, unit: "kg" } },
  ] };
  for (const sets of [undefined, deadlift.sets]) {
    assert.equal(backoffLoadFor(deadlift, log(deadlift, { actual: { ...actual, sets } })), null);
  }
  assert.deepEqual(backoffLoadFor(deadlift, log(deadlift, { actual: { ...actual, sets: 2 } })), { value: 90, unit: "kg" });
});
