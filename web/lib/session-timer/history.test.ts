import test from "node:test";
import assert from "node:assert/strict";
import { withHistoricalTimerLoads } from "./history";
import { buildTimerItems } from "./plan";
import { createTimerState, endSession, finishItemAsPlanned } from "./engine";
import { timerSetDrafts, confirmSetLog } from "./sets";
import { timerExerciseLogs } from "./logs";
import type { ExerciseLogRecord, StructuredBlock } from "../types";

const block: StructuredBlock = { block_id: "squat", exercise_key: "back-squat",
  block_type: "strength", display_name: "Back Squat", sets: 2, reps: 8 };
test("older-plan history prefills distinct sets without recording them; confirmation survives completion", () => {
  const previous = { plan_id: "older-plan", exercise_key: "back-squat", prescribed: block,
    status: "as_prescribed", actual: { sets: 2, set_entries: [
      { reps: 8, load: { value: 115, unit: "kg" } },
      { reps: 8, load: { value: 125, unit: "kg" } },
    ] } } as ExerciseLogRecord;
  const items = withHistoricalTimerLoads(buildTimerItems([{ blocks: [block] }]), [block], [], [previous]);
  let state = { ...createTimerState(items), completed: [2] };
  const drafts = timerSetDrafts(state)!;
  assert.deepEqual(drafts.map(row => row.load), ["115", "125"]);
  assert.equal(timerExerciseLogs(endSession(state, 0))[0].actual?.load, undefined);
  state = confirmSetLog(state, items[0].id, 1);
  state = confirmSetLog(state, items[0].id, 2);
  state = finishItemAsPlanned(state, 0).state;
  const actual = timerExerciseLogs(endSession(state, 0))[0].actual!;
  assert.deepEqual(actual.set_entries?.map(row => row.load?.value), [115, 125]);
  assert.equal(actual.load, null);
});
test("recent load fallback uses exercise identity and leaves unknown weights blank", () => {
  const base = buildTimerItems([{ blocks: [block] }]);
  const items = withHistoricalTimerLoads(base, [block], [{
    exercise_key: "back-squat", display_name: "Old name", load: { value: 120, unit: "kg" }, training_day: "2026-01-01",
  }]);
  assert.deepEqual(timerSetDrafts(createTimerState(items))?.map(row => row.load), ["120", "120"]);
  assert.deepEqual(timerSetDrafts(createTimerState(base))?.map(row => row.load), ["", ""]);
});
