import { window } from "./test-dom";
import test from "node:test";
import assert from "node:assert/strict";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { CustomDoseEditor } from "./custom-dose-editor";
import { blankDose, blankPrescribedSet } from "@/lib/custom-plan-dose";
import type { CustomDose, PersonalExercise } from "@/lib/api-schema.generated";

test("custom Add and Edit share history, leave edits intact and apply weights only on request", async () => {
  const node = document.createElement("div");
  const actualRoot = createRoot(node);
  const past = { id: "key:back-squat", name: "Back Squat", latest_day: "2026-10-01",
    latest_values: { sets: 3, reps: 8, load: { value: 120, unit: "kg" } },
    latest_confirmed_as_prescribed: true } as PersonalExercise;
  let dose: CustomDose = blankDose();
  let changes = 0;
  const render = (essential: boolean) => actualRoot.render(<CustomDoseEditor dose={dose} past={past} essential={essential}
    onChange={next => { changes++; dose = next; render(essential); }} />);
  try {
    await act(async () => render(true));
    assert.match(node.textContent!, /Last 120 kg/);
    assert.equal(changes, 0);
    assert.equal(dose.sets, null);
    assert.equal(dose.reps, null);
    await act(async () => [...node.querySelectorAll("button")].find(b => b.textContent?.includes("Use last weight"))!.click());
    assert.equal(node.querySelector<HTMLInputElement>('[aria-label="Weight"]')!.value, "120");
    dose = { ...dose, load_or_intensity: "110 kg" };
    await act(async () => render(false));
    assert.match(node.textContent!, /Last 120 kg/);
    assert.equal(node.querySelector<HTMLInputElement>('[aria-label="Weight"]')!.value, "110");
    for (const load_or_intensity of ["110kg", "light band"]) {
      dose = { ...dose, load_or_intensity };
      await act(async () => render(false));
      assert.equal(dose.load_or_intensity, load_or_intensity);
      assert.ok(![...node.querySelectorAll("button")].some(button => /Use (last|suggested) weight/.test(button.textContent ?? "")));
      assert.equal(changes, 1);
    }
  } finally { await act(async () => { actualRoot.unmount(); }); }
});

test("earned custom weights stay optional and editable until explicitly applied", async () => {
  const node = document.createElement("div");
  const root = createRoot(node);
  const past = { id: "custom:bench", name: "Bench Press", latest_day: "2026-10-01",
    latest_values: { sets: 3, reps: 10, load: { value: 80, unit: "kg" } },
    progression_hold: false, allow_progression: true,
    latest_performance: { prescribed: { display_name: "Bench Press" }, status: "modified",
      actual: { sets: 3, reps: 10, load: { value: 80, unit: "kg" }, effort: { method: "RPE", value: 6 } } },
  } as unknown as PersonalExercise;
  let dose: CustomDose = { ...blankDose(), sets: 3, reps: 10, load_or_intensity: "80 kg", rpe: 7 };
  let changes = 0;
  const render = () => root.render(<CustomDoseEditor dose={dose} past={past}
    onChange={next => { changes++; dose = next; render(); }} />);
  try {
    await act(async () => render());
    assert.match(node.textContent!, /Suggested 82.5 kg/);
    assert.equal(dose.load_or_intensity, "80 kg");
    assert.equal(changes, 0);
    await act(async () => [...node.querySelectorAll("button")].find(b => b.textContent?.includes("Use suggested weight"))!.click());
    assert.equal(changes, 1);
    assert.equal(dose.load_or_intensity, "82.5 kg");
    assert.equal(past.latest_values.load!.value, 80);
  } finally { await act(async () => root.unmount()); }
});


test("loading-stage editing and reordering preserve every other prescribed detail", async () => {
  const node = document.createElement("div");
  const root = createRoot(node);
  const source = { ...blankPrescribedSet(), sets: 3, reps: 1, percentage: 95, source_text: "WT: 225",
    rest: { value: 120, unit: "seconds" }, per_side: "E/S", tempo: { eccentric: 0, pause_bottom: null, concentric: "X", pause_top: null } };
  let dose: CustomDose = { ...blankDose("stages"), prescribed_sets: [source, { ...blankPrescribedSet(), reps: "4e" }] };
  const render = () => root.render(<CustomDoseEditor dose={dose} onChange={next => { dose = next; render(); }} />);
  try {
    await act(async () => render());
    const percentage = node.querySelector<HTMLInputElement>('[aria-label="Stage 1 percentage"]')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!.call(percentage, "90");
      percentage.dispatchEvent(new window.Event("input", { bubbles: true }));
    });
    assert.equal(dose.prescribed_sets![0].percentage, 90);
    assert.equal(dose.prescribed_sets![0].source_text, "WT: 225");
    assert.deepEqual(dose.prescribed_sets![0].rest, source.rest);
    assert.deepEqual(dose.prescribed_sets![0].tempo, source.tempo);
    assert.equal(dose.prescribed_sets![0].per_side, "E/S");
    await act(async () => node.querySelector<HTMLButtonElement>('[aria-label="Move stage 1 down"]')!.click());
    assert.equal(dose.prescribed_sets![0].reps, "4e");
    assert.equal(dose.prescribed_sets![1].percentage, 90);
    assert.deepEqual(dose.prescribed_sets![1].rest, source.rest);
    assert.equal(dose.mode, "stages");
    assert.ok(!node.textContent!.includes("Use last weight"));
  } finally { await act(async () => root.unmount()); }
});
