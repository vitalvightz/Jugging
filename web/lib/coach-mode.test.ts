import { test } from "node:test";
import assert from "node:assert/strict";
import { libraryRelevance, planRelevance, prioritiseForMode, trainingRelevance, validCoachMode } from "./coach-mode";
import type { TrainingPick } from "./training-disciplines";

test("coach modes default safely", () => {
  assert.equal(validCoachMode(null), "sc");
  assert.equal(validCoachMode("invalid"), "sc");
  assert.equal(validCoachMode("technical"), "technical");
});

test("plan type classification keeps mixed, recovery and legacy shared", () => {
  for (const type of ["skill", "sparring"]) assert.equal(planRelevance(type), "technical");
  for (const type of ["strength_power", "conditioning"]) assert.equal(planRelevance(type), "sc");
  for (const type of ["mixed", "recovery", "rest", "", "unknown", null]) assert.equal(planRelevance(type), "shared");
});

test("library classification is metadata only", () => {
  assert.equal(libraryRelevance("technical_coordination"), "technical");
  assert.equal(libraryRelevance("strength_power"), "sc");
  assert.equal(libraryRelevance("conditioning"), "sc");
  assert.equal(libraryRelevance("tactical_educational"), "shared");
});

test("logged work respects structured discipline classifications", () => {
  const pick = (activity: string, sport: string | null = null, session_type: string | null = null) =>
    ({ activity, sport, session_type } as TrainingPick);
  assert.equal(trainingRelevance(pick("boxing", "boxing", "pads")), "technical");
  assert.equal(trainingRelevance(pick("other", "bjj", "rolling")), "technical");
  assert.equal(trainingRelevance(pick("other", "mma", "grappling")), "technical");
  assert.equal(trainingRelevance(pick("other", "wrestling", "live")), "technical");
  assert.equal(trainingRelevance(pick("other", "muay_thai", "clinch")), "technical");
  assert.equal(trainingRelevance(pick("strength")), "sc");
  assert.equal(trainingRelevance(pick("roadwork")), "sc");
  assert.equal(trainingRelevance(pick("boxing", "bjj", "rolling")), "shared");
  assert.equal(trainingRelevance(pick("other")), "shared");
});

test("relevance order is stable and never drops opposite-mode items", () => {
  const rows = ["strength_power", "skill", "mixed", "sparring", "conditioning", "rest"];
  assert.deepEqual(prioritiseForMode(rows, "technical", planRelevance), ["skill", "sparring", "mixed", "rest", "strength_power", "conditioning"]);
  assert.deepEqual(prioritiseForMode(rows, "sc", planRelevance), ["strength_power", "conditioning", "mixed", "rest", "skill", "sparring"]);
});
