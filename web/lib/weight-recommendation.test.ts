import test from "node:test";
import assert from "node:assert/strict";
import { compactWeightAge, weightRecommendation } from "./weight-recommendation";
import { withHistoricalTimerLoads } from "./session-timer/history";
import { buildTimerItems } from "./session-timer/plan";
import { logFieldsForBlock } from "./exercise-log";
import type { ExerciseLogRecord, StructuredBlock } from "./types";

const block: StructuredBlock = { block_id: "squat", exercise_key: "squat", block_type: "strength",
  progression_hold: false, display_name: "Back Squat", sets: 3, reps: 8, effort: { method: "RPE", value: 7 } };
const load = (value: number) => ({ value, unit: "kg" });
const previous = (actual: ExerciseLogRecord["actual"]) => ({
  exercise_key: "squat", status: "as_prescribed", prescribed: block, actual,
} as ExerciseLogRecord);
test("shared recommendation separates evidence from earned load, rep and reduction suggestions", () => {
  const context = { allowProgression: true, recentPerformances: [previous({ load: load(120), feel: "easy" })] };
  const earned = weightRecommendation(block, context);
  assert.deepEqual(earned.last, load(120));
  assert.deepEqual(earned.suggested, load(122.5));
  const timer = withHistoricalTimerLoads(buildTimerItems([{ blocks: [block] }]), [block], [], context.recentPerformances, context);
  assert.ok(timer[0].kind === "sets");
  assert.deepEqual(timer[0].weightRecommendation, earned);
  assert.deepEqual(timer[0].suggestedLoads, ["122.5", "122.5", "122.5"]);
  const reps = weightRecommendation({ ...block, rep_range: [8, 12] }, context);
  assert.equal(reps.tryReps, 9);
  assert.deepEqual(reps.suggested, load(120));
  const hold = weightRecommendation(block, { ...context, allowProgression: false });
  assert.deepEqual(hold.suggested, load(120));
  const reduction = weightRecommendation(block, { recentPerformances: [previous({ load: load(120), effort: { method: "RPE", value: 9 } })] });
  assert.deepEqual(reduction.last, load(120));
  assert.ok(reduction.suggested!.value < 120);
});
test("prescription wins, lower-load holds never import history, unknown remains blank", () => {
  const context = { allowProgression: true, recentPerformances: [previous({ load: load(120), feel: "easy" })] };
  assert.deepEqual(weightRecommendation({ ...block, load: load(80) }, context).suggested, load(80));
  assert.deepEqual(weightRecommendation({ ...block, load: load(80) }, context).last, load(120));
  assert.equal(weightRecommendation(block).suggested, null);
  assert.equal(weightRecommendation(block, { ...context, lowerLoads: true }).suggested, null);
  assert.equal(weightRecommendation({ ...block, progression_hold: true }, context).suggested, null);
  assert.equal(weightRecommendation({ ...block, effort: { method: "RPE", value: 5 } }, context).suggested, null);
  assert.equal(weightRecommendation(block, { ...context, recentPerformances: [{ ...context.recentPerformances[0], reason: "pain" }] }).suggested, null);
});

test("per-set progression and reductions use the same engine without flattening measured loads", () => {
  const history = previous({ sets: 3, set_entries: [100, 110, 120].map(value =>
    ({ reps: 8, load: load(value), rpe: 6 })) });
  const earned = weightRecommendation(block, { allowProgression: true, recentPerformances: [history] });
  assert.deepEqual(earned.last, load(120));
  assert.equal(earned.suggested, null);
  assert.deepEqual(earned.sets.map(row => row.suggested?.value), [102.5, 112.5, 122.5]);
  const hard = { ...history, actual: { ...history.actual,
    set_entries: history.actual.set_entries!.map(row => ({ ...row, rpe: 9 })) } };
  const reduced = weightRecommendation(block, { recentPerformances: [hard] });
  assert.deepEqual(reduced.last, load(120));
  assert.equal(reduced.suggested, null);
  assert.ok(reduced.sets.every(row => row.suggested!.value < row.last!.value));
  assert.deepEqual(history.actual.set_entries!.map(row => row.load?.value), [100, 110, 120]);
});

test("older evidence may be carried forward but never proves an unmeasured easy session earned a raise", () => {
  const result = weightRecommendation(block, { allowProgression: true,
    recentLoads: [{ exercise_key: "squat", display_name: "Back Squat", load: load(120), training_day: "2026-01-01" }],
    recentPerformances: [previous({ sets: 3, reps: 8, feel: "easy" })] });
  assert.deepEqual(result.last, load(120));
  assert.deepEqual(result.suggested, load(120));
});

test("today's restrictions override reductions from previous struggle for summary and every set", () => {
  const hard = previous({ sets: 3, set_entries: [100, 110, 120].map(value =>
    ({ reps: 8, load: load(value), rpe: 9 })) });
  for (const restricted of [
    { block, context: { lowerLoads: true } },
    { block: { ...block, progression_hold: true }, context: {} },
    { block: { ...block, effort: { method: "RPE", value: 5 } }, context: {} },
  ]) {
    const context = { ...restricted.context, allowProgression: true, recentPerformances: [hard] };
    const result = weightRecommendation(restricted.block, context);
    assert.deepEqual(result.last, load(120));
    assert.equal(result.suggested, null);
    assert.equal(result.tryReps, null);
    assert.ok(result.sets.every(row => row.suggested === null));
    const timer = withHistoricalTimerLoads(buildTimerItems([{ blocks: [restricted.block] }]),
      [restricted.block], [], [hard], context);
    assert.ok(timer[0].kind === "sets");
    assert.deepEqual(timer[0].suggestedLoads, [null, null, null]);
    const prescribed = weightRecommendation({ ...restricted.block, load: load(60) }, context);
    assert.deepEqual(prescribed.suggested, load(60));
    assert.ok(prescribed.sets.every(row => row.suggested?.value === 60));
  }
});

test("confirmed manual history can seed the next planned exposure without granting progression", async () => {
  const { withManualWeightHistory } = await import("./weight-recommendation");
  const manual = { id: "key:squat", name: "Back Squat", latest_day: "2026-10-04", latest_source: "manual",
    latest_confirmed_as_prescribed: false, latest_values: { sets: 3, set_entries: [115, 120, 125].map(value =>
      ({ reps: 8, load: load(value) })) } } as import("./api-schema.generated").PersonalExercise;
  const history = withManualWeightHistory([], new Map([["squat", manual]]), "2026-10-05");
  const result = weightRecommendation(block, { recentPerformances: history, allowProgression: true });
  assert.deepEqual(result.last, load(125));
  assert.deepEqual(result.sets.map(row => row.suggested?.value), [115, 120, 125]);
  assert.equal(result.tryReps, null);
  const older = withManualWeightHistory([{ ...history[0], training_day: "2026-10-06", status: "skipped" }],
    new Map([["squat", manual]]), "2026-10-07");
  assert.equal(weightRecommendation(block, { recentPerformances: older }).suggested, null);
});

test("known incompatible doses retain Last without suggesting or prefilling history", () => {
  const history = previous({ sets: 3, set_entries: [100, 110, 120].map(value =>
    ({ reps: 8, load: load(value), rpe: 9 })) });
  for (const today of [{ ...block, sets: 4 }, { ...block, reps: 10 }, { ...block, reps: "6-8" }]) {
    const context = { recentPerformances: [history], allowProgression: true };
    const result = weightRecommendation(today, context);
    assert.deepEqual(result.last, load(120));
    assert.equal(result.suggested, null);
    assert.equal(result.tryReps, null);
    assert.ok(result.sets.every(row => row.suggested === null));
    assert.deepEqual(result.sets.slice(0, 3).map(row => row.last?.value), [100, 110, 120]);
    const timer = withHistoricalTimerLoads(buildTimerItems([{ blocks: [today] }]), [today], [], [history], context);
    assert.ok(timer[0].kind === "sets");
    assert.ok(timer[0].suggestedLoads?.every(value => value === null));
    const prescribed = weightRecommendation({ ...today, load: load(60) }, context);
    assert.deepEqual(prescribed.suggested, load(60));
    assert.ok(prescribed.sets.every(row => row.suggested?.value === 60));
  }
});

test("normalized ranges and legacy fixed prescriptions remain compatible", () => {
  for (const [today, before] of [
    [{ ...block, reps: "8–12" }, { ...block, reps: "8-12" }],
    [{ ...block, rep_range: [8, 12] }, block],
  ] as [StructuredBlock, StructuredBlock][]) {
    const result = weightRecommendation(today, { recentPerformances: [
      { ...previous({ load: load(120), feel: "right" }), prescribed: before }], allowProgression: true });
    assert.deepEqual(result.suggested, load(120));
  }
});

test("hard complete work eases five percent, missed work or fatigue ten, preserving set variation", () => {
  const history = previous({ sets: 3, set_entries: [100, 110, 120].map(value =>
    ({ reps: 8, load: load(value), rpe: 9 })) });
  const result = weightRecommendation(block, { recentPerformances: [history] });
  assert.deepEqual(result.sets.map(row => row.suggested?.value), [95, 105, 115]);
  for (const missed of [
    { ...history, reason: "fatigue" as const },
    { ...history, actual: { ...history.actual, sets: 2 } },
    { ...history, actual: { ...history.actual, set_entries: history.actual.set_entries!.map(row => ({ ...row, reps: 7 })) } },
  ]) {
    assert.deepEqual(weightRecommendation(block, { recentPerformances: [missed] }).sets.map(row => row.suggested?.value), [90, 100, 107.5]);
  }
});

test("missing prior prescription or performance preserves measured carry-forward without earned progression", () => {
  for (const prescribed of [{ display_name: "Back Squat" }, { ...block, reps: undefined }, { ...block, sets: undefined }]) {
    const result = weightRecommendation(block, { allowProgression: true, recentPerformances: [
      { ...previous({ load: load(120), feel: "easy" }), prescribed }] });
    assert.deepEqual(result.suggested, load(120));
    assert.equal(result.tryReps, null);
  }
  assert.deepEqual(weightRecommendation(block, { recentLoads: [{ exercise_key: "squat", display_name: "Back Squat",
    load: load(120), training_day: "2026-01-01" }], allowProgression: true }).suggested, load(120));
});

test("Hard feedback alone reduces complete work five percent, with missed work still ten", () => {
  const history = previous({ sets: 3, feel: "hard", set_entries: [100, 110, 120].map(value =>
    ({ reps: 8, load: load(value) })) });
  const context = { recentPerformances: [history], allowProgression: true };
  const result = weightRecommendation(block, context);
  assert.deepEqual(result.last, load(120));
  assert.equal(result.suggested, null);
  assert.deepEqual(result.sets.map(row => row.suggested?.value), [95, 105, 115]);
  const timer = withHistoricalTimerLoads(buildTimerItems([{ blocks: [block] }]), [block], [], [history], context);
  assert.ok(timer[0].kind === "sets");
  assert.deepEqual(timer[0].suggestedLoads, ["95", "105", "115"]);
  for (const patch of [{ reason: "fatigue" as const }, { actual: { ...history.actual, sets: 2 } }]) {
    assert.deepEqual(weightRecommendation(block, { recentPerformances: [{ ...history, ...patch }] }).sets.map(row => row.suggested?.value), [90, 100, 107.5]);
  }
  for (const guarded of [
    { ...context, lowerLoads: true },
    { ...context, recentPerformances: [{ ...history, reason: "pain" as const }] },
    { ...context, recentPerformances: [{ ...history, status: "skipped" as const }] },
  ]) {
    const held = weightRecommendation(block, guarded);
    assert.equal(held.suggested, null);
    assert.ok(held.sets.every(row => row.suggested === null));
    assert.deepEqual(weightRecommendation({ ...block, load: load(60) }, guarded).suggested, load(60));
  }
});

test("known mismatches block carry-forward even when the other dose component is missing", () => {
  for (const [today, before] of [
    [{ ...block, sets: 4 }, { ...block, reps: undefined }],
    [{ ...block, sets: 4, reps: undefined }, block],
    [{ ...block, reps: 10 }, { ...block, sets: undefined }],
    [{ ...block, sets: undefined, reps: 10 }, block],
  ] as [StructuredBlock, StructuredBlock][]) {
    const history = { ...previous({ load: load(120), feel: "easy" }), prescribed: before };
    const context = { recentPerformances: [history], allowProgression: true };
    const result = weightRecommendation(today, context);
    assert.deepEqual(result.last, load(120));
    assert.equal(result.suggested, null);
    assert.equal(result.tryReps, null);
    assert.ok(result.sets.every(row => row.suggested === null));
    const timer = withHistoricalTimerLoads(buildTimerItems([{ blocks: [today] }]), [today], [], [history], context);
    assert.ok(timer[0].kind === "sets");
    assert.ok(!timer[0].suggestedLoads || timer[0].suggestedLoads.every(value => value === null));
    assert.deepEqual(weightRecommendation({ ...today, load: load(60) }, context).suggested, load(60));
  }
});

test("bodyweight reps need no load evidence, while weighted raises and unknown authority still do", () => {
  const bodyweight = { ...block, load: { method: "bodyweight" }, rep_range: [8, 12] };
  const history = { ...previous({ feel: "easy", reps: 8 }), prescribed: bodyweight };
  assert.equal(weightRecommendation(bodyweight, { allowProgression: true, recentPerformances: [history] }).tryReps, 9);
  assert.equal(weightRecommendation(bodyweight, { allowProgression: true, recentPerformances: [
    { ...history, actual: { feel: "easy", reps: 12 } }] }).suggested, null);
  for (const guarded of [ { ...bodyweight, progression_hold: undefined }, { ...bodyweight, progression_hold: true },
    { ...bodyweight, block_type: "plyometric_power" } ]) {
    assert.equal(weightRecommendation(guarded, { allowProgression: true, recentPerformances: [history] }).tryReps, null);
  }
});

test("history dates follow the measured weight, including an older fallback after unmeasured Done", () => {
  const measured = { ...previous({ load: load(80) }), training_day: "2026-11-01" };
  assert.equal(weightRecommendation(block, { recentPerformances: [measured] }).lastDay, "2026-11-01");
  assert.equal(weightRecommendation(block, { recentPerformances: [{ ...measured, training_day: "2026-11-10", actual: {} }],
    recentLoads: [{ exercise_key: "squat", display_name: "Back Squat", load: load(80), training_day: "2026-11-01" }] }).lastDay, "2026-11-01");
});


test("compact weight age uses only d/w/mo and hides missing, invalid or future dates", () => {
  const now = new Date("2026-11-11T20:30:00Z");
  for (const [day, expected] of [["2026-11-05", "6d"], ["2026-10-28", "2w"], ["2026-08-13", "3mo"],
    ["2026-11-11", "0d"], [null, null], [undefined, null], ["bad", null], ["2026-02-30", null],
    ["2026-11-12", null]] as const) assert.equal(compactWeightAge(day, now), expected);
});

test("generated bodyweight blocks progress by reps inside authority, never by external load", async () => {
  const fixture = (await import("../test/fixtures/generated-bodyweight.json")).default;
  const blocks = fixture.blocks as StructuredBlock[];
  assert.ok(blocks.some(block => block.display_name === "Feet-Elevated Push-Up"));
  assert.ok(blocks.some(block => block.display_name === "Slow-Lowered Pull-Up"));
  for (const block of blocks) {
    const reps = Number(block.reps);
    const history = { ...previous({ sets: Number(block.sets), reps, feel: "easy" }), exercise_key: block.exercise_key ?? null, prescribed: block };
    const result = weightRecommendation(block, { recentPerformances: [history], allowProgression: true });
    assert.equal(result.suggested, null);
    if (block.progression_hold !== false || !block.rep_range) { assert.equal(result.tryReps, null); continue; }
    assert.equal(result.tryReps, reps + 1);
    assert.equal(block.load, undefined);
    assert.equal(logFieldsForBlock(block).some(field => field.key === "load"), false);
    const measuredSets = Array.from({ length: Number(block.sets) }, () => ({ reps, load: null }));
    assert.equal(weightRecommendation(block, { recentPerformances: [{ ...history, actual: {
      sets: Number(block.sets), set_entries: measuredSets, feel: "easy" } }], allowProgression: true }).tryReps, reps + 1);
    for (const actual of [{ sets: Number(block.sets) - 1, reps, feel: "easy" as const },
      { sets: Number(block.sets), set_entries: Array.from({ length: Number(block.sets) }, (_, at) => ({ reps: at ? reps : reps - 1, load: null })), feel: "easy" as const },
      { sets: Number(block.sets), reps: block.rep_range[1], feel: "easy" as const }]) {
      assert.equal(weightRecommendation(block, { recentPerformances: [{ ...history, actual }], allowProgression: true }).tryReps, null);
    }
    assert.equal(weightRecommendation({ ...block, progression_hold: undefined }, { recentPerformances: [history], allowProgression: true }).tryReps, null);
  }
});

test("equivalent generated, custom and unplanned completed work shares earned progression", () => {
  const target = { ...block, sets: 3, reps: 10, load: load(80), effort: { method: "RPE" as const, value: 7 } };
  const actual = { sets: 3, reps: 10, load: load(80), effort: { method: "RPE" as const, value: 6 } };
  for (const source of [undefined, "athlete"] as const) {
    const today = { ...target, prescription_source: source };
    for (const prescribed of [today, {}]) {
      const history = { ...previous(actual), prescribed };
      const result = weightRecommendation(today, { allowProgression: true, recentPerformances: [history] });
      assert.deepEqual(result.suggested, load(82.5));
      assert.deepEqual(today.load, load(80));
      assert.deepEqual(history.prescribed, prescribed);
    }
  }
});

test("unplanned evidence needs complete counts and genuine effort; matching a target is Right", () => {
  const target = { ...block, sets: 3, reps: 10, load: load(80), effort: { method: "RPE" as const, value: 7 } };
  for (const actual of [{ load: load(80) }, { sets: 3, reps: 10, load: load(80) },
    { sets: 3, reps: 10, load: load(80), effort: { method: "RPE" as const, value: 7 } },
    { sets: 2, reps: 10, load: load(80), effort: { method: "RPE" as const, value: 6 } }]) {
    assert.deepEqual(weightRecommendation(target, { allowProgression: true, recentPerformances: [
      { ...previous(actual), prescribed: {} }] }).suggested, load(80));
  }
  assert.equal(weightRecommendation({ ...target, progression_hold: true }, { allowProgression: true,
    recentPerformances: [{ ...previous({ sets: 3, reps: 10, load: load(80), feel: "easy" }), prescribed: {} }] }).tryReps, null);
});

test("a library snapshot cannot replace a newer context skip or pain result", async () => {
  const { personalWeightRecommendation } = await import("./weight-recommendation");
  const old = { ...previous({ sets: 3, reps: 8, load: load(120), feel: "easy" }), training_day: "2026-10-01" };
  const past = { id: "key:squat", name: block.display_name, latest_day: old.training_day,
    latest_values: old.actual, latest_performance: old, progression_hold: false, allow_progression: true,
  } as unknown as import("./api-schema.generated").PersonalExercise;
  for (const change of [{ status: "skipped" as const }, { reason: "pain" as const }]) {
    const latest = { ...old, ...change, training_day: "2026-10-02" };
    const result = personalWeightRecommendation(block, past, { allowProgression: true, recentPerformances: [latest] });
    assert.equal(result.suggested, null);
    assert.ok(result.sets.every(row => row.suggested === null));
  }
});

test("reusing a protected movement as strength does not earn generic progression or backoff", () => {
  for (const feel of ["easy", "hard"] as const) {
    const history = { ...previous({ sets: 3, reps: 8, load: load(120), feel }),
      prescribed: { ...block, block_type: "plyometric_power" } };
    assert.deepEqual(weightRecommendation(block, { allowProgression: true, recentPerformances: [history] }).suggested, load(120));
  }
});
