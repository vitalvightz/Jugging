import test from "node:test";
import assert from "node:assert/strict";
import { simpleRehabOutcome, usesSimpleRehabCompletion } from "./rehab-completion";
import type { StructuredSession } from "./types";

function rehab(...stages: string[]): StructuredSession {
  return { session_type: "rehab", blocks: stages.map(rehab_stage => ({ block_type: "rehab", drill_snapshot: { rehab_stage } })) };
}

test("Calm and Restore rehab use direct outcome logging", () => {
  for (const session of [rehab("calm"), rehab("restore"), rehab("calm", "restore")]) {
    assert.equal(usesSimpleRehabCompletion(session), true);
  }
});

test("Load, mixed training, empty and unknown sessions retain the training flow", () => {
  for (const session of [undefined, rehab(), rehab("load"), rehab("calm", "load"), rehab("unknown"),
    { ...rehab("calm"), session_type: "strength" },
    { session_type: "rehab", blocks: [{ block_type: "rehab" }] },
    { ...rehab("calm"), blocks: [...rehab("calm").blocks!, { block_type: "strength" }] }]) {
    assert.equal(usesSimpleRehabCompletion(session), false);
  }
});

test("direct outcomes preserve performance without inventing pain, effort or a reason", () => {
  assert.deepEqual(simpleRehabOutcome("done"), { rehabPerformance: "done_as_shown", modificationReason: "" });
  assert.equal(simpleRehabOutcome("modified").rehabPerformance, "changed");
  assert.equal(simpleRehabOutcome("skipped").rehabPerformance, undefined);
  assert.match(simpleRehabOutcome("modified").modificationReason, /no further details supplied/);
  assert.match(simpleRehabOutcome("skipped").modificationReason, /not done/);
});
