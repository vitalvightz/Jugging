import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";
import { WeightRecommendation } from "./weight-recommendation";

const load = (value: number, unit = "kg") => ({ value, unit });
function text(last: ReturnType<typeof load> | null, suggested: ReturnType<typeof load> | null, tryReps: number | null = null) {
  return renderToStaticMarkup(<WeightRecommendation value={{ last, suggested, tryReps, sets: [] }} />).replace(/<[^>]*>/g, "");
}
test("compact guidance keeps one primary weight and secondary history", () => {
  assert.equal(text(load(95), load(95)), "Suggested 95 kg · Same as last");
  assert.equal(text(load(95), load(97.5)), "Suggested 97.5 kg · Last 95 kg");
  assert.equal(text(load(95), load(85)), "Suggested 85 kg · Last 95 kg");
  assert.equal(text(load(95), load(95), 9), "95 kg · Try 9 reps");
});
test("missing suggestions never appear as a completed weight or an invented suggestion", () => {
  assert.equal(text(load(95), null), "Last 95 kg");
  assert.equal(text(null, load(95)), "Suggested 95 kg");
  assert.equal(text(null, null), "");
  assert.equal(text(load(95, "lb"), load(95)), "Suggested 95 kg · Last 95 lb");
});
