import "./test-dom";
import test from "node:test";
import assert from "node:assert/strict";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { CustomDoseEditor } from "./custom-dose-editor";
import { blankDose } from "@/lib/custom-plan-dose";
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
    assert.match(node.textContent!, /Suggested120 kgLast120 kg/);
    assert.equal(changes, 0);
    assert.equal(dose.sets, null);
    assert.equal(dose.reps, null);
    await act(async () => [...node.querySelectorAll("button")].find(b => b.textContent?.includes("Use suggested weight"))!.click());
    assert.equal(node.querySelector<HTMLInputElement>('[aria-label="Weight"]')!.value, "120");
    dose = { ...dose, load_or_intensity: "110 kg" };
    await act(async () => render(false));
    assert.match(node.textContent!, /Suggested110 kgLast120 kg/);
    assert.equal(node.querySelector<HTMLInputElement>('[aria-label="Weight"]')!.value, "110");
  } finally { await act(async () => { actualRoot.unmount(); }); }
});
