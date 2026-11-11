import "./test-dom";
import test from "node:test";
import assert from "node:assert/strict";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { usePastExercises } from "./load-field";
import { invalidatePastExercises } from "@/lib/exercise-library";

test("confirmed results refresh history already displayed by another editor", async () => {
  const original = globalThis.fetch;
  let load = 115;
  globalThis.fetch = (async () => new Response(JSON.stringify({ exercises: [{
    id: "key:back-squat", identity_kind: "canonical", latest_values: { load: { value: load, unit: "kg" } },
  }] }))) as typeof fetch;
  const node = document.createElement("div");
  const root = createRoot(node);
  function Probe() {
    const history = usePastExercises("history-refresh");
    return <span>{history?.get("back-squat")?.latest_values.load?.value}</span>;
  }
  try {
    await act(async () => root.render(<Probe />));
    assert.equal(node.textContent, "115");
    load = 125;
    await act(async () => invalidatePastExercises("history-refresh"));
    assert.equal(node.textContent, "125");
  } finally {
    await act(async () => root.unmount());
    globalThis.fetch = original;
  }
});
