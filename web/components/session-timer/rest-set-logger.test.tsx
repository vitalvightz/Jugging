import test from "node:test";
import assert from "node:assert/strict";
import "../test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { RestSetLogger } from "./rest-set-logger";
import { buildTimerItems } from "@/lib/session-timer/plan";
import { createTimerState } from "@/lib/session-timer/engine";
import type { SessionTimerController } from "./use-session-timer";

const keys = ["discovery-timer-set-entry-seen", "discovery-timer-rpe-seen"];
async function mount(phase: "rest" | "work" = "rest") {
  const items = buildTimerItems([{ blocks: [{ block_id: "squat", block_type: "strength", display_name: "Squat", sets: 3, reps: 5 }] }]);
  let state = { ...createTimerState(items), phase, completed: [1] };
  const node = document.createElement("div"); document.body.append(node);
  const root = createRoot(node);
  const render = () => root.render(<RestSetLogger target={{ itemId: items[0].id, set: 1 }} timer={{ state,
    update: (change: (value: typeof state) => typeof state) => { state = change(state); render(); },
  } as unknown as SessionTimerController} />);
  await act(async () => render());
  return { node, state: () => state, cleanup: async () => { await act(async () => root.unmount()); node.remove(); } };
}

test("rest guidance swaps for RPE help, typing retires it, and confirmation keeps actual results", async () => {
  keys.forEach(key => window.localStorage.removeItem(key));
  const ui = await mount();
  try {
    assert.match(ui.node.querySelector(".st-discovery-hint")?.textContent ?? "", /Check weight and reps/);
    const effort = ui.node.querySelector<HTMLDetailsElement>(".st-rest-effort")!;
    await act(async () => { effort.open = true; effort.dispatchEvent(new Event("toggle", { bubbles: true })); });
    assert.equal(ui.node.querySelectorAll(".st-discovery-hint").length, 1);
    assert.match(ui.node.querySelector(".st-discovery-hint")?.textContent ?? "", /RPE is how hard this set felt/);
    assert.equal(window.localStorage.getItem(keys[0]), "1");
    const input = ui.node.querySelector<HTMLInputElement>('[aria-label="RPE, set 1"]')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!.call(input, "7.5");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    assert.equal(window.localStorage.getItem(keys[1]), "1");
    assert.equal(ui.node.querySelector(".st-discovery-hint"), null);
    assert.equal(ui.state().setDrafts?.[ui.state().items[0].id]?.[0].rpe, "7.5");
    await act(async () => ui.node.querySelector<HTMLButtonElement>(".st-confirm-set")!.click());
    assert.equal(ui.node.querySelector(".st-rest-sheet"), null);
    assert.match(ui.node.textContent ?? "", /Set 1 logged/);
  } finally { await ui.cleanup(); }
  const reopened = await mount();
  try { assert.equal(reopened.node.querySelector(".st-discovery-hint"), null); }
  finally { await reopened.cleanup(); keys.forEach(key => window.localStorage.removeItem(key)); }
});

test("working sets show no discovery prompt and dismissal does not confirm a set", async () => {
  keys.forEach(key => window.localStorage.removeItem(key));
  const working = await mount("work");
  try { assert.equal(working.node.querySelector(".st-discovery-hint"), null); }
  finally { await working.cleanup(); }
  const resting = await mount();
  try {
    await act(async () => resting.node.querySelector<HTMLButtonElement>(".st-discovery-hint")!.click());
    assert.equal(window.localStorage.getItem(keys[0]), "1");
    assert.equal(resting.state().phase, "rest");
    assert.equal(resting.state().confirmedSetLogs?.[resting.state().items[0].id], undefined);
    assert.equal(resting.node.querySelector(".st-discovery-hint"), null);
  } finally { await resting.cleanup(); keys.forEach(key => window.localStorage.removeItem(key)); }
});
