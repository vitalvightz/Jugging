import assert from "node:assert/strict";
import test from "node:test";
import { sessionActivity } from "./session-activity";
import { blankDose, doseSummary, exerciseIncomplete } from "./custom-plan-dose";

test("gym session entries have their activity label and need no prescription", () => {
  for (const [name, label] of [
    ["Boxing session", "Boxing"], ["Technical/pads session", "Pads"],
    ["Sparring/technical rounds as scheduled", "Light technical"], ["Skill session", "Light technical"],
    ["Light technical", "Light technical"], ["Light sparring", "Light technical"], ["Roadwork", "Roadwork"],
  ]) {
    assert.equal(sessionActivity(name), label);
    assert.equal(exerciseIncomplete({ name, dose: blankDose() }), false);
    assert.equal(exerciseIncomplete({ snapshot: { name }, dose: { ...blankDose("intervals"), rounds: 3 } }), false);
    assert.equal(doseSummary(blankDose(), name), "");
  }
  assert.equal(sessionActivity("Box squat"), null);
  assert.equal(exerciseIncomplete({ name: "Back squat", dose: blankDose() }), true);
});
