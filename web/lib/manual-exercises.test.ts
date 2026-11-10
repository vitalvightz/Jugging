import assert from "node:assert/strict";
import test from "node:test";
import type { LibraryEntry, PersonalExercise } from "./api-schema.generated";
import { blankDraft, draftFromEntry, optionChoice, optionFormat, readDraft, searchOptions, summarise } from "./manual-exercises";

const exercise = (id: string, name: string, latest_day = "2026-10-01", overrides: Partial<PersonalExercise> = {}): PersonalExercise => ({
  id, name, identity_kind: id.startsWith("custom:") ? "personal" : id.startsWith("log:") ? "legacy" : "canonical",
  latest_day, latest_values: { sets: null, reps: null, rounds: null, load: null, duration: null, work: null, rest: null, distance: null, effort: null, feel: null, set_entries: null },
  latest_confirmed_as_prescribed: false, best_loads: [], latest_source: "manual", log_format: "strength", ...overrides,
});
const entry = (name: string, exercise_key: string, log_format: LibraryEntry["log_format"] = "strength") =>
  ({ catalogue_id: `cat-${exercise_key}`, name, category: "training", exercise_key, log_format }) as LibraryEntry;

test("summaries read like a training log and never invent values", () => {
  assert.equal(summarise({ sets: 3, reps: 5, load: { value: 80, unit: "kg" } }, "strength"), "3 × 5 · 80 kg");
  assert.equal(summarise({ sets: 3, duration: { value: 45, unit: "seconds" } }, "isometric"), "3 × 45 sec");
  assert.equal(summarise({ sets: 3, reps: 8, load: { value: 10, unit: "kg" } }, "bodyweight"), "3 × 8 · +10 kg");
  assert.equal(summarise({ rounds: 6, work: { value: 3, unit: "minutes" }, rest: { value: 60, unit: "seconds" } }, "intervals"), "6 × 3 min · 60 sec rest");
  assert.equal(summarise({ distance: { value: 5, unit: "km" }, duration: { value: 28, unit: "minutes" } }, "distance"), "5 km · 28 min");
  assert.equal(summarise({ sets: 3 }, "strength"), "3 sets completed · measurements unreported");
  assert.equal(summarise({ load: { value: 200, unit: "lb" }, effort: { method: "RPE", value: 8.5 } }, "strength"), "200 lb · RPE 8.5");
  assert.equal(summarise({}, "strength"), "");
});

test("drafts send only filled, valid fields with the units the athlete chose", () => {
  const draft = blankDraft("strength", "lb");
  draft.values = { sets: "3", reps: "5", load: "185.5", rest: "", effort: "8" };
  assert.deepEqual(readDraft("strength", draft), { actual: { sets: 3, reps: 5, load: { value: 185.5, unit: "lb" }, effort: { method: "RPE", value: 8 } }, errors: {} });
  // Values for fields the format doesn't record are never sent.
  draft.values = { sets: "3", distance: "5" };
  assert.deepEqual(readDraft("strength", draft).actual, { sets: 3 });
  assert.deepEqual(readDraft("strength", blankDraft("strength")).actual, {});
});

test("each invalid value has a message beside its own field", () => {
  const draft = blankDraft("strength");
  draft.values = { sets: "0", reps: "2.5", load: "1200", effort: "7.3" };
  assert.deepEqual(Object.keys(readDraft("strength", draft).errors).sort(), ["effort", "load", "reps", "sets"]);
  draft.values = { load: "0" };
  assert.equal(readDraft("strength", draft).errors.load, "More than 0");
  const rest = blankDraft("intervals");
  rest.values = { rest: "0", work: "abc" };
  assert.deepEqual(readDraft("intervals", rest), { actual: { rest: { value: 0, unit: "seconds" } }, errors: { work: "Enter a number" } });
  const pounds = blankDraft("strength", "lb");
  pounds.values = { load: "2000" };
  assert.deepEqual(readDraft("strength", pounds).errors, {});
});

test("editing restores stored values and units exactly", () => {
  const draft = draftFromEntry({ format: "intervals", notes: "Felt sharp", actual: {
    sets: null, reps: null, rounds: 5, load: null, duration: null, work: { value: 2, unit: "minutes" }, rest: { value: 30, unit: "seconds" }, distance: null, effort: null, feel: null, set_entries: null } });
  assert.deepEqual(draft.values, { rounds: "5", work: "2", rest: "30" });
  assert.equal(draft.units.work, "minutes");
  assert.equal(draft.notes, "Felt sharp");
});

test("search ranks recorded exercises, then library, then a new name only when it is new", () => {
  const recorded = [exercise("key:back-squat", "Back Squat", "2026-10-10"), exercise("custom:1b4e28ba-2fa1-11d2-883f-0016d3cca427", "Banded squat walk"),
    exercise("log:5", "Squat legacy")];
  const catalogue = [entry("Back Squat", "back-squat"), entry("Front Squat", "front-squat"), entry("Squat Jump", "squat-jump", null)];
  const options = searchOptions(recorded, catalogue, "squat");
  assert.deepEqual(options.map(option => option.kind === "new" ? `new:${option.name}` : `${option.kind}:${"exercise" in option ? option.exercise.name : option.entry.name}`), [
    "recorded:Back Squat", "recorded:Banded squat walk", "catalogue:Front Squat", "catalogue:Squat Jump", "new:squat"]);
  assert.equal(searchOptions(recorded, catalogue, "back squat").some(option => option.kind === "new"), false);
  assert.equal(searchOptions(recorded, catalogue, "  BACK   squat ").some(option => option.kind === "new"), false);
  const created = searchOptions(recorded, catalogue, "  Sled   drag ").at(-1)!;
  assert.deepEqual(optionChoice(created), { name: "Sled drag" });
});

test("choices keep stable identities and only trusted formats are preselected", () => {
  assert.deepEqual(optionChoice({ kind: "recorded", exercise: exercise("key:back-squat", "Back Squat") }), { exercise_key: "back-squat" });
  assert.deepEqual(optionChoice({ kind: "recorded", exercise: exercise("custom:abc", "Mine") }), { personal_exercise_id: "abc" });
  assert.deepEqual(optionChoice({ kind: "catalogue", entry: entry("Plank", "plank", "isometric") }), { catalogue_id: "cat-plank" });
  assert.equal(optionFormat({ kind: "catalogue", entry: entry("Plank", "plank", "isometric") }), "isometric");
  assert.equal(optionFormat({ kind: "catalogue", entry: entry("Complex", "complex", null) }), null);
  assert.equal(optionFormat({ kind: "new", name: "Anything" }), null);
});

test("library cards say a name-only manual record was performed without measurements", async () => {
  const { exerciseResult } = await import("./exercise-library");
  assert.deepEqual(exerciseResult(exercise("custom:abc", "Landmine rotation")), { label: "Latest recorded", value: "No measurements recorded" });
  assert.deepEqual(exerciseResult(exercise("key:back-squat", "Back Squat", "2026-10-19", {
    best_loads: [{ value: 90, unit: "kg", training_day: "2026-10-19", basis: "reported" }, { value: 200, unit: "lb", training_day: "2026-10-01", basis: "reported" }] })),
    { label: "Best recorded loads · by unit", value: "90 kg · 200 lb" });
});
