import assert from "node:assert/strict";
import test from "node:test";
import type { CustomDoseSuggestion, PersonalExercise } from "./api-schema.generated";
import { blankDose, libraryPrefill } from "./custom-plan-dose";

const suggestion = (value: Partial<CustomDoseSuggestion>): CustomDoseSuggestion =>
  ({ dose: null, format_dose: null, original: "", ...value }) as CustomDoseSuggestion;
const past = (latest_values: Record<string, unknown>) => ({
  id: "key:back_squat", name: "Back squat", identity_kind: "canonical", latest_day: "2026-10-30",
  latest_values: { sets: null, reps: null, rounds: null, load: null, duration: null, work: null, rest: null, distance: null, effort: null, set_entries: null, ...latest_values },
  latest_confirmed_as_prescribed: true, best_loads: [], latest_source: "plan", log_format: "strength",
}) as PersonalExercise;

test("the library's source dose wins over its format", () => {
  const dose = { ...blankDose("intervals"), rounds: 5, work_seconds: 300, rest_seconds: 30 };
  const result = libraryPrefill(suggestion({ dose, format_dose: { ...blankDose("reps"), sets: 3, reps: 8 } }), null);
  assert.deepEqual(result, { dose, source: "Library format" });
});

test("a text source dose falls back to the verified format", () => {
  const format = { ...blankDose("reps"), sets: 3, reps: 8 };
  const result = libraryPrefill(suggestion({ dose: { ...blankDose("text"), prescription: "Work to a hard set" }, format_dose: format }), null);
  assert.deepEqual(result, { dose: format, source: "Library format" });
});

test("last session fills only what the library leaves open, and says so", () => {
  const result = libraryPrefill(suggestion({ format_dose: { ...blankDose("reps"), sets: 3, reps: 5 } }),
    past({ sets: 5, reps: 3, load: { value: 100, unit: "kg" } }));
  assert.equal(result?.dose.sets, 3, "the library's sets stay");
  assert.equal(result?.dose.reps, 5, "the library's reps stay");
  assert.equal(result?.dose.load_or_intensity, "100 kg");
  assert.equal(result?.source, "Library format · your last session");
});

test("with no library format, last session sets the dose", () => {
  const result = libraryPrefill(null, past({ sets: 4, reps: 6 }));
  assert.equal(result?.dose.sets, 4);
  assert.equal(result?.source, "Your last session");
});

test("a mode-only verified format cannot turn into the previous session's incompatible mode", () => {
  for (const mode of ["duration", "intervals"] as const) {
    const result = libraryPrefill(suggestion({ format_dose: blankDose(mode) }), past({ sets: 4, reps: 6, load: { value: 80, unit: "kg" } }));
    assert.equal(result?.dose.mode, mode);
    assert.equal(result?.dose.sets, null);
    assert.equal(result?.dose.reps, null);
    assert.equal(result?.dose.load_or_intensity, null);
    assert.equal(result?.source, "Library format");
  }
});

test("a mode-only format can reuse matching previous workout values", () => {
  const result = libraryPrefill(suggestion({ format_dose: blankDose("intervals") }),
    past({ rounds: 5, work: { value: 180, unit: "seconds" }, rest: { value: 60, unit: "seconds" } }));
  assert.equal(result?.dose.mode, "intervals");
  assert.equal(result?.dose.rounds, 5);
  assert.equal(result?.dose.work_seconds, 180);
  assert.equal(result?.dose.rest_seconds, 60);
  assert.equal(result?.source, "Library format · your last session");
});

test("empty text formats do not create invented prescriptions", () => {
  for (const prescription of ["", "   ", null]) {
    assert.equal(libraryPrefill(suggestion({ format_dose: { ...blankDose("text"), prescription } }), null), null);
  }
  const authored = { ...blankDose("text"), prescription: "3 coached circuits" };
  assert.deepEqual(libraryPrefill(suggestion({ format_dose: authored }), null), { dose: authored, source: "Library format" });
});

test("no reliable source means no prefill, never an invented dose", () => {
  assert.equal(libraryPrefill(null, null), null);
  assert.equal(libraryPrefill(suggestion({}), null), null);
  assert.equal(libraryPrefill(suggestion({ format_dose: blankDose("reps") }), null), null);
});
