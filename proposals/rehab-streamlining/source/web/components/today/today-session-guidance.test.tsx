import test from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { TodaySessionGuidance } from "./today-session-guidance";

test("one guidance block communicates a hold or its rehab replacement", () => {
  const prescription = { revision: "r", frozen: false, session: {}, changes: [], safety_hold: true,
    safety_hold_reason: "Get this injury checked.", rehab_only: true, training_hold_reason: "Training held." };
  const held = renderToStaticMarkup(<TodaySessionGuidance prescription={prescription} />);
  assert.equal((held.match(/role="alert"/g) || []).length, 1);
  assert.match(held, /Get this injury checked/);
  assert.doesNotMatch(held, /Follow only|Training held/);
  const rehab = renderToStaticMarkup(<TodaySessionGuidance prescription={{ ...prescription, safety_hold: false }} />);
  assert.match(rehab, /Training held/);
  assert.match(rehab, /Follow only the rehab/);
  assert.doesNotMatch(rehab, /Get this injury checked/);
  assert.equal(renderToStaticMarkup(<TodaySessionGuidance prescription={null} />), "");
});
