import assert from "node:assert/strict";
import test from "node:test";
import { doseFromBlock, finishedDose, validDose } from "./exercise-dose";
import { blankDose, doseSummary } from "./custom-plan-dose";

test("short holds and interval work stay in seconds on custom cards", () => {
  assert.equal(doseSummary({ ...blankDose("duration"), sets: 3, duration_seconds: 12 }), "3 × 12s");
  assert.equal(doseSummary({ ...blankDose("intervals"), rounds: 6, work_seconds: 20 }), "6 × 20s");
});

test("a partly written dose shows what is there and names what's missing", () => {
  assert.equal(doseSummary({ ...blankDose("intervals"), rounds: 3 }), "3 rounds · duration unspecified");
  assert.equal(doseSummary({ ...blankDose("intervals"), rounds: 1, load_or_intensity: "light" }), "1 round · duration unspecified · light");
  assert.equal(doseSummary({ ...blankDose("intervals"), work_seconds: 180, rest_seconds: 60 }), "3 min rounds · number unspecified · rest 60s");
  assert.equal(doseSummary({ ...blankDose("reps"), sets: 3 }), "3 sets · reps unspecified");
  assert.equal(doseSummary({ ...blankDose("reps"), reps: 8, per_side: true }), "8 reps · sets unspecified · per side");
  assert.equal(doseSummary({ ...blankDose("duration"), sets: 4 }), "4 sets · duration unspecified");
  assert.equal(doseSummary({ ...blankDose("distance"), sets: 6 }), "6 sets · distance unspecified");
  assert.equal(doseSummary({ ...blankDose("distance"), distance: 5 }), "5 · unit unspecified");
  // Nothing written at all still asks for it.
  assert.equal(doseSummary(blankDose("intervals")), "Set intervals");
});

test("editing a timed drill reads minutes as seconds and preserves its effort", () => {
  const dose = doseFromBlock({ duration: { value: 25, unit: "minutes" }, effort: { method: "RPE", value: 4 } });
  assert.equal(dose.mode, "duration");
  assert.equal(dose.duration_seconds, 1500);
  assert.equal(dose.rpe, 4);
  assert.equal(validDose(dose), true);
});

test("an athlete's exact dose survives editing instead of generic defaults", () => {
  const dose = doseFromBlock({ athlete_dose: { mode: "reps", sets: 3, reps: 5, load_or_intensity: "80 kg", per_side: true } });
  assert.equal(dose.load_or_intensity, "80 kg");
  assert.equal(finishedDose(dose).per_side, true);
});

test("hill intervals require a stated rest, including a valid zero", () => {
  const draft = { mode: "intervals" as const, rounds: 6, work_seconds: 20 };
  assert.equal(validDose(draft), false);
  assert.equal(validDose({ ...draft, rest_seconds: 0 }), true);
  assert.equal(validDose({ ...draft, rest_seconds: -1 }), false);
});

test("distance carries accept decimal distances and no fabricated duration", () => {
  const draft = { mode: "distance" as const, distance: 12.5, distance_unit: "m" as const, sets: 4, load_or_intensity: "20 kg" };
  assert.equal(validDose(draft), true);
  assert.equal("duration_seconds" in finishedDose(draft), false);
});

test("switching format only saves fields for the selected format", () => {
  const dose = finishedDose({ mode: "duration", duration_seconds: 30, reps: 10, rounds: 4, distance: 50, rpe: 5 });
  assert.equal("reps" in dose, false);
  assert.equal("rounds" in dose, false);
  assert.equal("distance" in dose, false);
  assert.equal(validDose({ mode: "reps", sets: 3, reps: 8, rpe: 11 }), false);
});
