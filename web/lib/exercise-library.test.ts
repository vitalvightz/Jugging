import assert from "node:assert/strict";
import test from "node:test";
import type { PersonalExercise } from "./api-schema.generated";
import { exerciseResult } from "./exercise-library";

const exercise = (changes: Partial<PersonalExercise> = {}) => ({ id: "key:squat", name: "Squat", identity_kind: "canonical",
  latest_day: "2026-10-19", latest_values: {}, latest_confirmed_as_prescribed: false, best_loads: [], ...changes }) as PersonalExercise;

test("best recorded kg and lb remain separate and legacy results make no cross-date PR claim", () => {
  const best_loads = [{ value: 100, unit: "kg", basis: "reported" }, { value: 200, unit: "lb", basis: "reported" }] as PersonalExercise["best_loads"];
  assert.deepEqual(exerciseResult(exercise({ best_loads })), { label: "Best recorded loads · by unit", value: "100 kg · 200 lb" });
  assert.equal(exerciseResult(exercise({ best_loads, identity_kind: "legacy" })).label, "Recorded load");
});

test("a load inferred from a prescription marked Done is never labelled as a recorded load", () => {
  const kg = { value: 100, unit: "kg", basis: "confirmed_prescription" };
  const lb = { value: 200, unit: "lb", basis: "reported" };
  const loads = (...items: object[]) => exercise({ best_loads: items as PersonalExercise["best_loads"] });
  assert.deepEqual(exerciseResult(loads(kg)), { label: "Best prescribed load · marked Done", value: "100 kg" });
  assert.deepEqual(exerciseResult(loads(lb)), { label: "Best recorded load", value: "200 lb" });
  assert.deepEqual(exerciseResult(loads(kg, lb)),
    { label: "Best loads · by unit", value: "100 kg (prescribed, marked Done) · 200 lb (recorded)" });
  assert.equal(exerciseResult({ ...loads(kg), identity_kind: "legacy" }).label, "Prescribed load · marked Done");
});

test("bodyweight, duration and interval results do not require a weight or fabricate a best", () => {
  assert.equal(exerciseResult(exercise({ latest_values: { sets: 3, reps: 15 } as PersonalExercise["latest_values"] })).value, "3 × 15");
  assert.equal(exerciseResult(exercise({ latest_values: { duration: { value: 120, unit: "seconds" } } as PersonalExercise["latest_values"] })).value, "2 min");
  assert.equal(exerciseResult(exercise({ latest_values: { rounds: 6 } as PersonalExercise["latest_values"] })).value, "6 rounds");
  assert.deepEqual(exerciseResult(exercise({ latest_confirmed_as_prescribed: true })), { label: "Latest confirmed", value: "Completed as prescribed" });
});

test("unreported completed work has a compact label without claiming a load record", () => {
  assert.deepEqual(exerciseResult(exercise({ latest_values: { sets: 4 } as PersonalExercise["latest_values"] })), {
    label: "Measurements unreported", value: "4 sets completed",
  });
});
