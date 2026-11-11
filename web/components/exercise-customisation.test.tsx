import assert from "node:assert/strict";
import { test } from "node:test";

import { weekdayName } from "./exercise-customisation";

test("weekdayName reads the ISO date as a calendar day in any timezone", () => {
  assert.equal(weekdayName("2026-10-12"), "Monday");
  assert.equal(weekdayName("2026-10-18"), "Sunday");
  assert.equal(weekdayName("2026-10-12T23:30:00-08:00"), "Monday");
});

import "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { ExerciseActions, AddExerciseButton, ExerciseCustomisationProvider, ExerciseSessionScope } from "./exercise-customisation";
import { ExerciseLogProvider } from "./exercise-log";
import type { StructuredBlock } from "@/lib/types";

test("held swap and add previews suppress remembered weight suggestions", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => Response.json({ alternatives: [{ catalogue_id: "cossack", exercise_key: "cossack-squat",
    name: "Cossack Squat", suggested_dose: { sets: 3, reps: 8 }, dose_mode: "reps", equipment: [] }],
    scopes: ["occurrence"], exercises: [] });
  const node = document.createElement("div");
  document.body.appendChild(node);
  const root = createRoot(node);
  const block: StructuredBlock = { block_id: "squat", display_name: "Back Squat", block_type: "strength", sets: 3, reps: 8 };
  const click = async (label: string) => {
    const button = [...document.querySelectorAll("button")].find(button => button.textContent?.trim() === label || button.getAttribute("aria-label") === label);
    assert.ok(button, label);
    await act(async () => { button.click(); });
  };
  try {
    for (const held of [false, true]) {
      const current = { ...block, progression_hold: held };
      await act(async () => root.render(<ExerciseCustomisationProvider value={{ token: "preview-test", planId: "plan", today: "2026-11-11", onSessionChanged: () => {} }}>
        <ExerciseLogProvider logging={{ canWrite: false, logs: {}, painReasonAllowed: false, save: async () => {},
          recentLoads: [{ exercise_key: "cossack-squat", display_name: "Cossack Squat", load: { value: 20, unit: "kg" }, training_day: "2026-11-10" }] }}>
          <ExerciseSessionScope date="2026-11-11" session={{ session_id: "s", title: "Strength", session_type: "strength_power", blocks: [current] }}>
            <ExerciseActions block={current} title="Back Squat" /><AddExerciseButton />
          </ExerciseSessionScope>
        </ExerciseLogProvider>
      </ExerciseCustomisationProvider>));
      for (const action of ["swap", "add"]) {
        if (action === "swap") { await click("Change Back Squat"); await click("Swap"); }
        else await click("+ Add exercise");
        const dialog = document.querySelector('[role="dialog"]');
        assert.ok(dialog);
        assert.match(dialog.textContent ?? "", /Cossack Squat/);
        assert.equal((dialog.textContent ?? "").includes("Suggested 20 kg"), false);
        assert.match(dialog.textContent ?? "", /Last 20 kg/);
        await click("Close");
        await act(async () => { await new Promise(resolve => setTimeout(resolve, 250)); });
      }
    }
  } finally {
    await act(async () => root.unmount());
    node.remove();
    globalThis.fetch = originalFetch;
  }
});
