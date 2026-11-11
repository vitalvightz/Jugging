import assert from "node:assert/strict";
import test from "node:test";
import { window } from "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { ExerciseLibrary, type ExerciseHistoryCache } from "./exercise-library";
import type { ExerciseLibraryResponse } from "@/lib/api-schema.generated";
import { SHEET_EXIT_MS } from "@/lib/use-sheet-exit";

window.HTMLDialogElement.prototype.showModal ??= function () { this.setAttribute("open", ""); };
window.HTMLDialogElement.prototype.close ??= function () { this.removeAttribute("open"); };
const library = { as_of: "2026-10-19T12:00:00Z", coverage: "retained_logs", exercises: [
  { id: "key:back-squat", name: "Back Squat", identity_kind: "canonical", latest_day: "2026-10-19", latest_values: {}, latest_confirmed_as_prescribed: true, best_loads: [{ value: 100, unit: "kg", basis: "reported" }] },
  { id: "key:old-plank", name: "Old Plank", identity_kind: "canonical", latest_day: "2025-01-01", latest_values: { duration: { value: 120, unit: "seconds" } }, latest_confirmed_as_prescribed: false, best_loads: [] },
  { id: "log:old", name: "Legacy Squat", identity_kind: "legacy", latest_day: "2025-01-01", latest_values: { reps: 10 }, latest_confirmed_as_prescribed: false, best_loads: [] },
] } as unknown as ExerciseLibraryResponse;
const performance = (id: string, day = "2026-10-19") => ({ id, training_day: day, block: { name: "Back Squat", kind: "exercise", status: "modified", prescribed: { sets: 4, reps: 5 }, actual: { sets: 3, load: { value: 80, unit: "kg" } }, responses: [], notes: "Coach cue" } });

test("complete library search, record detail pagination, failure retry and visit cache", async () => {
  const fetch = globalThis.fetch;
  const requests: string[] = [];
  let pageCalls = 0;
  globalThis.fetch = async input => {
    requests.push(String(input)); pageCalls++;
    if (pageCalls === 2) return new Response("unavailable", { status: 503 });
    return new Response(JSON.stringify(pageCalls === 1 ? { performances: [performance("first")], next_cursor: "next" }
      : { performances: [performance("first"), performance("older", "2026-10-12")], next_cursor: null }));
  };
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container); const cache: ExerciseHistoryCache = new Map();
  const button = (text: string) => [...container.querySelectorAll<HTMLButtonElement>("button")].find(item => item.textContent?.includes(text))!;
  try {
    await act(async () => root.render(<ExerciseLibrary library={library} token="token" cache={cache} />));
    assert.equal(requests.length, 0);
    assert.equal(container.querySelectorAll('[aria-label="Your exercises"] > li').length, 2);
    assert.match(container.textContent!, /Unlinked legacy records/);
    const search = container.querySelector<HTMLInputElement>('input[type="search"]')!;
    const set = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
    await act(async () => { set.call(search, "Old Plank"); search.dispatchEvent(new window.Event("input", { bubbles: true })); });
    assert.match(container.textContent!, /2 min/);
    assert.equal(container.textContent?.includes("Back Squat"), false);
    await act(async () => { set.call(search, ""); search.dispatchEvent(new window.Event("input", { bubbles: true })); });
    const trigger = container.querySelector<HTMLButtonElement>('[aria-label="View Back Squat performance history"]')!;
    trigger.focus(); await act(async () => trigger.click());
    assert.match(container.querySelector("dialog")!.textContent!, /80 kg/);
    // History rows start collapsed with a one-line summary; the full record expands on request.
    assert.match(container.querySelector("dialog")!.textContent!, /3 sets/);
    // The weight stands apart from the dose, marked for gold.
    assert.equal(container.querySelector("dialog ol strong")!.textContent, "80 kg");
    assert.equal(container.querySelector("dialog")!.textContent!.includes("Other performed values are unknown"), false);
    const row = container.querySelector<HTMLButtonElement>('dialog ol button[aria-expanded="false"]')!;
    await act(async () => row.click());
    assert.equal(row.getAttribute("aria-expanded"), "true");
    assert.match(container.querySelector("dialog")!.textContent!, /Other performed values are unknown/);
    await act(async () => button("Load earlier performances").click());
    assert.ok(container.querySelector('[role="alert"]'));
    assert.match(container.querySelector("dialog")!.textContent!, /Coach cue/);
    await act(async () => button("Retry").click());
    assert.equal(container.querySelectorAll("dialog ol > li").length, 2);
    assert.equal(pageCalls, 3);
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Close exercise history"]')!.click());
    // The sheet slides away, then focus returns to what opened it.
    await act(async () => { await new Promise(resolve => setTimeout(resolve, SHEET_EXIT_MS + 20)); });
    assert.equal(document.activeElement, trigger);
    await act(async () => trigger.click());
    assert.equal(pageCalls, 3);
    assert.equal(container.querySelectorAll("dialog ol > li").length, 2);
  } finally { act(() => root.unmount()); container.remove(); globalThis.fetch = fetch; }
});

test("initial failures retry without claiming an empty performance record", async () => {
  const fetch = globalThis.fetch; let calls = 0;
  globalThis.fetch = async () => ++calls === 1 ? new Response("unavailable", { status: 503 })
    : new Response(JSON.stringify({ performances: [performance("first")], next_cursor: null }));
  const container = document.createElement("div"); document.body.append(container); const root = createRoot(container);
  try {
    await act(async () => root.render(<ExerciseLibrary library={library} token="token" cache={new Map()} />));
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="View Back Squat performance history"]')!.click());
    assert.ok(container.querySelector('[role="alert"]'));
    await act(async () => [...container.querySelectorAll("button")].find(item => item.textContent === "Retry")!.click());
    assert.equal(calls, 2); assert.equal(container.querySelectorAll("dialog ol > li").length, 1);
  } finally { act(() => root.unmount()); container.remove(); globalThis.fetch = fetch; }
});

test("library cards and details preserve the recorded exercise type", async () => {
  const fetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ performances: [], next_cursor: null }));
  const item = { ...library.exercises[0], latest_values: { sets: 3, reps: 8, load: { value: 120, unit: "kg" } },
    progression_hold: false, allow_progression: true, latest_performance: {
      exercise_key: "back-squat", training_day: "2026-10-01", status: "as_prescribed",
      prescribed: { display_name: "Back Squat", block_type: "plyometric_power", sets: 3, reps: 8 },
      actual: { sets: 3, reps: 8, load: { value: 120, unit: "kg" }, feel: "easy" },
    },
  } as unknown as ExerciseLibraryResponse["exercises"][number];
  const container = document.createElement("div"); document.body.append(container); const root = createRoot(container);
  try {
    await act(async () => root.render(<ExerciseLibrary library={{ ...library, exercises: [item] }} token="token" cache={new Map()} />));
    assert.ok(!container.textContent!.includes("122.5 kg"));
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="View Back Squat performance history"]')!.click());
    assert.ok(!container.querySelector("dialog")!.textContent!.includes("122.5 kg"));
  } finally { act(() => root.unmount()); container.remove(); globalThis.fetch = fetch; }
});
