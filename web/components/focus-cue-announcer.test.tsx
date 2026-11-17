import assert from "node:assert/strict";
import test from "node:test";
import "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";

import { FocusCueAnnouncer } from "./focus-cue-announcer";
import { FocusCueLine } from "./session-details";
import { ToastProvider } from "./toast-provider";
import { watchNoteFocusCue } from "@/lib/focus-cue-watch";
import { TRAINING_FOCUS_CHANGED_EVENT } from "@/lib/training-focus";

const DELAYS = [5, 5, 5, 5] as const;
const wait = (ms: number) => act(async () => { await new Promise(resolve => setTimeout(resolve, ms)); });

async function mount(answers: Array<Record<string, unknown>>) {
  const original = globalThis.fetch;
  const urls: string[] = [];
  globalThis.fetch = async (input: RequestInfo | URL) => {
    urls.push(String(input));
    const answer = answers.length > 1 ? answers.shift()! : answers[0];
    return new Response(JSON.stringify(answer), { headers: { "Content-Type": "application/json" } });
  };
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<ToastProvider><FocusCueAnnouncer delaysMs={DELAYS} /></ToastProvider>));
  return {
    urls,
    text: () => document.body.textContent ?? "",
    async cleanup() { await act(async () => root.render(null)); container.remove(); globalThis.fetch = original; },
  };
}

test("a saved note is answered with its cue once it is ready", async () => {
  const pending = { status: "pending", id: null, focus: null };
  const app = await mount([pending, pending, { status: "ready", id: "cue-1", focus: "Turn your hips through each hook." }]);
  let refreshed = 0;
  const onRefresh = () => { refreshed++; };
  window.addEventListener(TRAINING_FOCUS_CHANGED_EVENT, onRefresh);
  try {
    await act(async () => watchNoteFocusCue({ token: "t", kind: "sparring_log", sourceId: "log-1" }));
    await wait(80);
    assert.match(app.text(), /Next time: Turn your hips through each hook\./);
    assert.match(app.text(), /UNLXCK COACH/);
    assert.match(app.text(), /Saved to History/);
    assert.equal(document.querySelector('a[href="/history?tab=sparring"]')?.textContent, "View in History →");
    await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Close UNLXCK Coach feedback"]')?.click());
    assert.equal(document.querySelector('[aria-label="UNLXCK Coach feedback"]'), null);
    assert.equal(app.urls.length, 3);
    assert.match(app.urls[0], /\/api\/training\/focus-cues\/sparring_log\/log-1$/);
    assert.equal(refreshed, 1);
  } finally {
    window.removeEventListener(TRAINING_FOCUS_CHANGED_EVENT, onRefresh);
    await app.cleanup();
  }
});

test("a note with no cue stays quiet and stops asking", async () => {
  const app = await mount([{ status: "none", id: null, focus: null }]);
  try {
    await act(async () => watchNoteFocusCue({ token: "t", kind: "manual_takeaway", sourceId: "s-1" }));
    await wait(80);
    assert.doesNotMatch(app.text(), /Next time/);
    assert.equal(app.urls.length, 1);
  } finally { await app.cleanup(); }
});

test("a cue that never arrives gives up after the last poll", async () => {
  const app = await mount([{ status: "pending", id: null, focus: null }]);
  try {
    await act(async () => watchNoteFocusCue({ token: "t", kind: "plan_completion", sourceId: "c-1" }));
    await wait(120);
    assert.equal(app.urls.length, DELAYS.length);
    assert.doesNotMatch(app.text(), /Next time/);
  } finally { await app.cleanup(); }
});

test("History shows a ready cue under its note and nothing otherwise", async () => {
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<FocusCueLine cue="Keep your foot planted." />));
    assert.equal(container.querySelector(".history-row-cue")?.textContent, "UNLXCK CUEKeep your foot planted.");
    await act(async () => root.render(<FocusCueLine cue={null} />));
    assert.equal(container.querySelector(".history-row-cue"), null);
  } finally { await act(async () => root.render(null)); container.remove(); }
});

test("plan and manual training cues link to Sessions history", async () => {
  for (const kind of ["plan_completion", "manual_takeaway"] as const) {
    const app = await mount([{ status: "ready", id: "cue-1", focus: "Check foot pressure as you lower." }]);
    try {
      await act(async () => watchNoteFocusCue({ token: "t", kind, sourceId: "note-1" }));
      await wait(80);
      assert.equal(document.querySelector('a[href="/history?tab=sessions"]')?.textContent, "View in History →");
    } finally { await app.cleanup(); }
  }
});

test("a newly saved note clears older feedback even when no new cue is generated", async () => {
  const app = await mount([
    { status: "ready", id: "old-cue", focus: "Keep your feet steady." },
    { status: "none", id: null, focus: null },
  ]);
  try {
    await act(async () => watchNoteFocusCue({ token: "t", kind: "plan_completion", sourceId: "old" }));
    await wait(35);
    assert.match(app.text(), /Keep your feet steady/);
    await act(async () => watchNoteFocusCue({ token: "t", kind: "plan_completion", sourceId: "new" }));
    assert.equal(document.querySelector('[aria-label="UNLXCK Coach feedback"]'), null);
    await wait(35);
    assert.doesNotMatch(app.text(), /Keep your feet steady/);
    assert.equal(app.urls.length, 2);
  } finally { await app.cleanup(); }
});
