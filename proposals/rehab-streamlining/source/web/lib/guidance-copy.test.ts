import test from "node:test";
import assert from "node:assert/strict";
import { distinctGuidanceLines } from "./guidance-copy";

test("repeated caution copy appears once, distinct instructions remain", () => {
  assert.deepEqual(distinctGuidanceLines(" Keep it covered. ", "keep it  covered!", "Avoid contact.", "", undefined),
    ["Keep it covered.", "Avoid contact."]);
  assert.deepEqual(distinctGuidanceLines("No contact.", "Contact allowed after review."),
    ["No contact.", "Contact allowed after review."]);
});
