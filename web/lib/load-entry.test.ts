import assert from "node:assert/strict";
import test from "node:test";
import type { PersonalExercise } from "./api-schema.generated";
import { blankDose } from "./custom-plan-dose";
import { parseLoad, pastLoad, startsAsWeight, weighedFormat, withPastDose } from "./load-entry";

const past = (latest_values: Partial<PersonalExercise["latest_values"]>) => ({ id: "key:back-squat", name: "Back Squat",
  identity_kind: "canonical", latest_day: "2026-10-12", latest_values, latest_confirmed_as_prescribed: false, best_loads: [] }) as unknown as PersonalExercise;

test("only a number with kg or lb is a weight; other text stays text", () => {
  assert.deepEqual(parseLoad("30kg"), { value: 30, unit: "kg" });
  assert.deepEqual(parseLoad(" 45 lbs "), { value: 45, unit: "lb" });
  assert.equal(parseLoad("light band"), null);
  assert.equal(parseLoad("70%"), null);
  assert.equal(startsAsWeight("", true), true);
  assert.equal(startsAsWeight("", false), false);
  assert.equal(startsAsWeight("Bodyweight", true), false);
  assert.equal(startsAsWeight("20 kg", false), true);
});

test("an exercise with no suggested format starts from what the athlete did last time", () => {
  const dose = withPastDose({ mode: "reps" as const }, past({ sets: 4, reps: 6, load: { value: 80, unit: "kg" }, effort: { method: "RPE", value: 8 } }));
  assert.deepEqual(dose, { mode: "reps", sets: 4, reps: 6, load_or_intensity: "80 kg" });
  const run = withPastDose(blankDose(), past({ distance: { value: 5, unit: "km" } }));
  assert.equal(run.mode, "distance");
  assert.equal(run.distance, 5);
  assert.equal(run.distance_unit, "km");
});

test("a suggested format wins and history only adds the missing weight", () => {
  const dose = withPastDose({ mode: "reps" as const, sets: 3, reps: 10 }, past({ sets: 5, reps: 5, load: { value: 100, unit: "lb" } }));
  assert.deepEqual(dose, { mode: "reps", sets: 3, reps: 10, load_or_intensity: "100 lb" });
  const kept = withPastDose({ mode: "reps" as const, sets: 3, reps: 10, load_or_intensity: "light band" }, past({ load: { value: 20, unit: "kg" } }));
  assert.equal(kept.load_or_intensity, "light band");
  assert.deepEqual(withPastDose({ mode: "reps" as const }, null), { mode: "reps" });
  assert.deepEqual(pastLoad(past({ load: { value: 20, unit: "kg" } })), { value: 20, unit: "kg" });
});

test("sets × reps default to a weight unless the movement's own format says it is not weighed", () => {
  assert.equal(weighedFormat("reps"), true);
  assert.equal(weighedFormat("reps", { log_format: "strength" }), true);
  assert.equal(weighedFormat("reps", { log_format: "bodyweight" }), false);
  assert.equal(weighedFormat("reps", null, { log_format: "bodyweight" }), false);
  assert.equal(weighedFormat("reps", { log_format: null }), true);
  assert.equal(weighedFormat("duration", { log_format: "strength" }), false);
  assert.equal(parseLoad("30 kgs"), null);
});
