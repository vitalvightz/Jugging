import assert from "node:assert/strict";
import test from "node:test";
import { window } from "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { SessionDetails, type SessionDetailsCache } from "./session-details";
import type { TrainingHistorySession } from "@/lib/training-history";
import { SHEET_EXIT_MS } from "@/lib/use-sheet-exit";

const proto = window.HTMLDialogElement.prototype;
proto.showModal ??= function () { this.setAttribute("open", ""); };
proto.close ??= function () { this.removeAttribute("open"); };
const row: TrainingHistorySession = { id: "one", source: "unlxck", title: "Strength", training_day: "2026-10-10",
  status: "done", activity: null, plan_id: "old-plan", duration_minutes: null, effort: null };
const payload = { session: row, prescription_source: "snapshot", blocks: [
  { name: "Squat", kind: "exercise", prescribed: { sets: 4, reps: 6 }, actual: {}, status: null, responses: [] },
  { name: "Calf raise", kind: "rehab", prescribed: { sets: 3, reps: 10 }, actual: {}, status: "stopped", responses: [], completed_dose: { completion_state: "partial_amount_unknown" } },
] };

test("details distinguish unknown actuals, reuse visit cache, and dismiss with Escape", async () => {
  const fetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => { calls++; return new Response(JSON.stringify(payload)); };
  const cache: SessionDetailsCache = new Map();
  const container = document.createElement("div"); document.body.append(container);
  const trigger = document.createElement("button"); document.body.append(trigger); trigger.focus();
  const root = createRoot(container); let dismissed = false;
  const render = () => root.render(<SessionDetails row={row} token="token" cache={cache} onDismiss={() => { dismissed = true; }} />);
  try {
    await act(async () => render());
    for (const button of container.querySelectorAll<HTMLButtonElement>("ol button[aria-expanded]")) await act(async () => button.click());
    assert.match(container.textContent!, /Performed values unknown/);
    assert.match(container.textContent!, /Completed amount unknown/);
    assert.equal(container.querySelectorAll("ol > li").length, 2);
    const dialog = container.querySelector("dialog")!;
    await act(async () => dialog.dispatchEvent(new window.Event("cancel", { bubbles: true, cancelable: true })));
    // The sheet slides away before it is dismissed.
    await act(async () => { await new Promise(resolve => setTimeout(resolve, SHEET_EXIT_MS + 20)); });
    assert.ok(dismissed);
    await act(async () => root.render(null));
    assert.equal(document.activeElement, trigger);
    await act(async () => render());
    assert.equal(calls, 1);
  } finally { act(() => root.unmount()); container.remove(); trigger.remove(); globalThis.fetch = fetch; }
});

test("failed details have a working retry and never cache the failure", async () => {
  const fetch = globalThis.fetch; let calls = 0;
  globalThis.fetch = async () => ++calls === 1 ? new Response("unavailable", { status: 503 }) : new Response(JSON.stringify(payload));
  const cache: SessionDetailsCache = new Map();
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<SessionDetails row={row} token="token" cache={cache} onDismiss={() => {}} />));
    assert.match(container.querySelector('[role="alert"]')!.textContent!, /Unable to load/);
    assert.equal(cache.size, 0);
    await act(async () => container.querySelector<HTMLButtonElement>(".secondary-button")!.click());
    assert.equal(calls, 2);
    assert.match(container.textContent!, /Squat/);
  } finally { act(() => root.unmount()); container.remove(); globalThis.fetch = fetch; }
});

test("completed-as-prescribed blocks start collapsed and never repeat matching reported values", async () => {
  const fetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ ...payload, blocks: [
    { name: "Bound", kind: "exercise", prescribed: { sets: 4, reps: 2 }, actual: { sets: 4, set_entries: [{ reps: 2 }, { reps: 2 }, { reps: 2 }, { reps: 2 }] }, status: "as_prescribed", responses: [] },
    { name: "Press", kind: "exercise", prescribed: { sets: 3, reps: 5 }, actual: { sets: 2, reps: 5 }, status: "modified", responses: [] },
  ] }));
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<SessionDetails row={row} token="token" cache={new Map()} onDismiss={() => {}} />));
    const toggles = [...container.querySelectorAll<HTMLButtonElement>("ol button[aria-expanded]")];
    assert.deepEqual(toggles.map(button => button.getAttribute("aria-expanded")), ["false", "false"]);
    await act(async () => toggles[1].click());
    assert.match(container.textContent!, /4 × 2/);
    const press = container.querySelector('[aria-label="Performance: Press"]')!;
    // Done beside planned on one row, with the changed value marked.
    assert.match(press.textContent!, /Sets2 \/ 3/);
    assert.ok(press.querySelector("[data-changed]"));
    await act(async () => toggles[0].click());
    assert.equal(container.querySelector('[aria-label="Performance: Bound"]'), null);
    assert.match(container.textContent!, /Completed as prescribed · reported values match/);
  } finally { act(() => root.unmount()); container.remove(); globalThis.fetch = fetch; }
});

test("a hand-logged session shows effort and duration worded like an Unlxck one", async () => {
  const fetch = globalThis.fetch;
  const manual: TrainingHistorySession = { ...row, id: "manual", source: "external", activity: "boxing", duration_minutes: 45, effort: "easy" };
  globalThis.fetch = async () => new Response(JSON.stringify({ ...payload, session: manual, blocks: [] }));
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<SessionDetails row={manual} token="token" cache={new Map()} onDismiss={() => {}} />));
    assert.match(container.querySelector("header")!.textContent!, /45 min · Easy · Added by you/);
    assert.equal(container.querySelector('[aria-label="Session overview"]'), null);
    assert.doesNotMatch(container.textContent!, /min recorded/);
  } finally { act(() => root.unmount()); container.remove(); globalThis.fetch = fetch; }
});

test("a run reads as distance, time, pace and effort, compared with earlier runs", async () => {
  const fetch = globalThis.fetch;
  const earlier: TrainingHistorySession = { ...row, id: "earlier", source: "external", activity: "roadwork", training_day: "2026-10-05",
    duration_minutes: 25, duration_seconds: 1500, distance_value: 5, distance_unit: "km", effort: "easy" };
  const run: TrainingHistorySession = { ...earlier, id: "run", training_day: "2026-10-10", duration_minutes: 28, duration_seconds: 1650,
    distance_value: 6, distance_unit: "km", effort: "moderate", run_type: "tempo" };
  globalThis.fetch = async () => new Response(JSON.stringify({ ...payload, session: run, blocks: [], runs: [earlier, run] }));
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<SessionDetails row={run} token="token" cache={new Map()} onDismiss={() => {}} />));
    const facts = [...container.querySelectorAll("dl > div")].map(fact => `${fact.querySelector("dt")?.textContent}: ${fact.querySelector("dd")?.textContent}`);
    assert.deepEqual(facts, ["Time: 27:30", "Pace: 4:35/km", "Speed: 13.1km/h", "Runs: 2", "Distance: 11km", "Time: 52:30"]);
    const text = container.textContent!;
    assert.match(container.querySelector('[aria-label="Run summary"]')!.textContent!, /6km.*Tempo · Moderate/);
    assert.match(text, /Longest run/);
    const button = (label: string) => [...container.querySelectorAll("button")].find(item => item.textContent === label)!;
    // Bars of distance first.
    assert.equal(button("Bars").getAttribute("aria-pressed"), "true");
    assert.match(text, /1 km further than your previous run/);
    let slider = container.querySelector('[role="slider"]')!;
    assert.equal(container.querySelectorAll('[role="slider"] > div').length, 2);
    assert.match(slider.getAttribute("aria-valuetext")!, /6 km \(this run\)/);
    await act(async () => slider.dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true })));
    assert.match(container.textContent!, /1 km shorter than this run/);
    await act(async () => button("Pace").click());
    assert.match(container.textContent!, /0:25\/km faster than your previous run/);
    await act(async () => button("Line").click());
    slider = container.querySelector('[role="slider"]')!;
    assert.ok(slider.querySelector("svg"));
    assert.match(slider.getAttribute("aria-valuetext")!, /4:35\/km \(this run\)/);
    assert.doesNotMatch(text, /No exercises recorded/);
  } finally { act(() => root.unmount()); container.remove(); globalThis.fetch = fetch; }
});

test("a run without distance or time asks for them and hands off to its edit sheet", async () => {
  const fetch = globalThis.fetch;
  const run: TrainingHistorySession = { ...row, id: "bare", source: "external", activity: "roadwork", duration_minutes: 26, effort: "moderate" };
  globalThis.fetch = async () => new Response(JSON.stringify({ ...payload, session: run, blocks: [] }));
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  let edited: TrainingHistorySession | null = null;
  try {
    await act(async () => root.render(<SessionDetails row={run} token="token" cache={new Map()} onDismiss={() => {}} onEditRun={value => { edited = value; }} />));
    assert.match(container.textContent!, /Add your distance and exact time/);
    const button = [...container.querySelectorAll("button")].find(item => item.textContent === "Add run details")!;
    await act(async () => button.click());
    assert.equal(edited!.id, "bare");
  } finally { act(() => root.unmount()); container.remove(); globalThis.fetch = fetch; }
});

test("bests come from the server's complete run list, not the loaded History", async () => {
  const fetch = globalThis.fetch;
  const base: TrainingHistorySession = { ...row, source: "external", activity: "roadwork", effort: "easy", duration_minutes: 50 };
  const old = { ...base, id: "old", training_day: "2021-03-01", distance_value: 30, distance_unit: "km" as const };
  const run = { ...base, id: "run", training_day: "2026-10-10", distance_value: 12, distance_unit: "km" as const };
  for (const [runs, longest] of [[[old, run], false], [[run], false], [null, false]] as const) {
    globalThis.fetch = async () => new Response(JSON.stringify({ ...payload, session: run, blocks: [], runs }));
    const container = document.createElement("div"); document.body.append(container);
    const root = createRoot(container);
    try {
      await act(async () => root.render(<SessionDetails row={run} token="token" cache={new Map()} onDismiss={() => {}} />));
      assert.equal(/Longest run/.test(container.textContent!), longest);
    } finally { act(() => root.unmount()); container.remove(); globalThis.fetch = fetch; }
  }
});
