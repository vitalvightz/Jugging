import test from "node:test";
import assert from "node:assert/strict";
import { weightRecommendation } from "./weight-recommendation";
import { withHistoricalTimerLoads } from "./session-timer/history";
import { buildTimerItems } from "./session-timer/plan";
import type { ExerciseLogRecord, StructuredBlock } from "./types";

const block: StructuredBlock = { block_id: "squat", exercise_key: "squat", block_type: "strength",
  display_name: "Back Squat", sets: 3, reps: 8, effort: { method: "RPE", value: 7 } };
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
  assert.deepEqual(earned.suggested, load(122.5));
  assert.deepEqual(earned.sets.map(row => row.suggested?.value), [102.5, 112.5, 122.5]);
  const hard = { ...history, actual: { ...history.actual,
    set_entries: history.actual.set_entries!.map(row => ({ ...row, rpe: 9 })) } };
  const reduced = weightRecommendation(block, { recentPerformances: [hard] });
  assert.deepEqual(reduced.last, load(120));
  assert.ok(reduced.suggested!.value < 120);
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
