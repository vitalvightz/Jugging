import test from "node:test";
import assert from "node:assert/strict";
import "../test-dom";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { RehabFeedbackQuestion } from "./rehab-feedback-question";
import type { RehabDuringResponse } from "../../lib/types";

test("one injury question records the tapped answer without a second confirmation", async () => {
  const container = document.createElement("div");
  const root = createRoot(container);
  const answers: RehabDuringResponse[] = [];
  try {
    await act(async () => root.render(<RehabFeedbackQuestion injury="Left ankle" question="How did it feel during rehab?"
      options={["better", "same", "worse", "not_sure"]} disabled={false} onAnswer={answer => answers.push(answer)} />));
    assert.equal(container.querySelectorAll('[role="group"]').length, 1);
    assert.deepEqual(Array.from(container.querySelectorAll("button"), button => button.textContent), ["Better", "Same", "Worse", "Not sure"]);
    assert.deepEqual(answers, []);
    await act(async () => (container.querySelectorAll("button")[2] as HTMLButtonElement).click());
    assert.deepEqual(answers, ["worse"]);
    assert.doesNotMatch(container.textContent!, /reduce|stop|save|completed/i);
    await act(async () => root.render(<RehabFeedbackQuestion injury="Left ankle" question="Next day?"
      options={["better", "same"]} disabled={true} onAnswer={answer => answers.push(answer)} />));
    assert.equal(container.querySelectorAll("button").length, 2);
    assert.ok(Array.from(container.querySelectorAll("button"), button => button.disabled).every(Boolean));
  } finally { await act(async () => root.unmount()); }
});
