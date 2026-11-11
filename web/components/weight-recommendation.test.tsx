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

test("rep-only guidance renders without a load", () => {
  assert.equal(text(null, null, 9), "Try 9 reps");
});

test("historical age appears once beside Last, without long labels", () => {
  const day = new Date(Date.now() - 6 * 86_400_000).toISOString().slice(0, 10);
  const html = renderToStaticMarkup(<WeightRecommendation value={{ last: load(80), lastDay: day, suggested: load(82.5), tryReps: null, sets: [] }} />);
  const text = html.replace(/<[^>]*>/g, "");
  assert.equal(text, "Suggested 82.5 kg · Last 80 kg · 6d");
  assert.equal(text.split("6d").length, 2);
  assert.doesNotMatch(html, /title=|days ago|tooltip/);
});
