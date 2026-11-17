import assert from "node:assert/strict";
import test from "node:test";
import "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";

import { SessionTakeawayPrompt } from "./session-takeaway";
import { ToastProvider } from "./toast-provider";
import type { TrainingHistorySession } from "@/lib/training-history";

const ROW: TrainingHistorySession = {
  id: "box-1", source: "external", training_day: "2026-10-19", status: "done", title: "Boxing",
  activity: "boxing", duration_minutes: 60, effort: "hard", plan_id: null,
};

async function mount(onSaved: (row: TrainingHistorySession) => void, onDismiss: () => void) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => root.render(<ToastProvider>
    <SessionTakeawayPrompt token="t" row={ROW} onSaved={onSaved} onDismiss={onDismiss} />
  </ToastProvider>));
  const button = (text: string) => Array.from(container.querySelectorAll("button")).find(item => item.textContent?.trim() === text)!;
  return { container, button, cleanup: () => { act(() => root.unmount()); container.remove(); } };
}

test("the post-save prompt saves a trimmed takeaway on its own endpoint", async () => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; method?: string; body: unknown }> = [];
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    calls.push({ url: String(input), method: init?.method, body: JSON.parse(String(init?.body ?? "{}")) });
    return new Response(JSON.stringify({ ...ROW, takeaway: "Right hand up" }), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  let saved: TrainingHistorySession | null = null;
  const ui = await mount(row => { saved = row; }, () => assert.fail("not dismissed"));
  try {
    assert.match(ui.container.textContent ?? "", /Session saved/);
    assert.match(ui.container.textContent ?? "", /Boxing · 60 minutes · Hard/);
    assert.match(ui.container.textContent ?? "", /Optional — skip anytime/);
    assert.equal(ui.button("Add session notes →").disabled, true);
    const input = ui.container.querySelector("input")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!.call(input, "  Right hand up  ");
      input.dispatchEvent(new window.Event("input", { bubbles: true }));
    });
    await act(async () => ui.button("Add session notes →").click());
    await act(async () => { await Promise.resolve(); });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "/api/training/sessions/box-1/takeaway");
    assert.equal(calls[0].method, "PUT");
    assert.deepEqual(calls[0].body, { takeaway: "Right hand up" });
    assert.equal(saved!.takeaway, "Right hand up");
  } finally {
    ui.cleanup();
    globalThis.fetch = originalFetch;
  }
});

test("skipping the prompt sends nothing", async () => {
  const originalFetch = globalThis.fetch;
  let requests = 0;
  globalThis.fetch = (async () => { requests += 1; return new Response("{}"); }) as typeof fetch;
  let dismissed = 0;
  const ui = await mount(() => assert.fail("not saved"), () => { dismissed += 1; });
  try {
    await act(async () => ui.button("Optional — skip anytime").click());
    assert.equal(dismissed, 1);
    assert.equal(requests, 0);
  } finally {
    ui.cleanup();
    globalThis.fetch = originalFetch;
  }
});
