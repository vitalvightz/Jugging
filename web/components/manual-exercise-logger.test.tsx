import assert from "node:assert/strict";
import test from "node:test";
import { window } from "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { ManualExerciseLogger } from "./manual-exercise-logger";
import { SessionDetails, type SessionDetailsCache } from "./session-details";
import type { ManualExerciseEntry } from "@/lib/api-schema.generated";
import type { TrainingHistorySession } from "@/lib/training-history";
import { SHEET_EXIT_MS } from "@/lib/use-sheet-exit";

const proto = window.HTMLDialogElement.prototype;
proto.showModal ??= function () { this.setAttribute("open", ""); };
proto.close ??= function () { this.removeAttribute("open"); };
globalThis.requestAnimationFrame ??= ((callback: FrameRequestCallback) => setTimeout(() => callback(0), 0)) as unknown as typeof requestAnimationFrame;

const session: TrainingHistorySession = { id: "session-1", source: "external", title: "Strength", training_day: "2026-10-19",
  status: "done", activity: "strength", plan_id: null, duration_minutes: 60, effort: "hard" };
const blank = { sets: null, reps: null, rounds: null, load: null, duration: null, work: null, rest: null, distance: null, effort: null, feel: null, set_entries: null };
const squatRecord = { id: "key:back-squat", name: "Back Squat", identity_kind: "canonical", latest_day: "2026-10-12",
  latest_values: { ...blank, sets: 3, reps: 5, load: { value: 75, unit: "kg" } }, latest_confirmed_as_prescribed: false,
  best_loads: [], latest_source: "manual", log_format: "strength" };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

type Call = { url: string; method: string; body: Record<string, unknown> | null };

async function mount({ entries = [] as ManualExerciseEntry[], reference = null as TrainingHistorySession | null,
  respond = undefined as ((call: Call) => Response | undefined) | undefined } = {}) {
  const calls: Call[] = [];
  const originalFetch = globalThis.fetch;
  const originalConfirm = window.confirm;
  const confirms: string[] = [];
  window.confirm = (message?: string) => { confirms.push(String(message)); return true; };
  globalThis.confirm = window.confirm;
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://test");
    const call = { url: url.pathname + url.search, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : null };
    calls.push(call);
    const custom = respond?.(call);
    if (custom) return custom;
    if (call.method === "PUT") {
      const body = call.body as { exercise: Record<string, string>; format: string; actual: Record<string, unknown>; notes: string };
      const name = body.exercise.name ?? (body.exercise.catalogue_id ? "Plank" : "Back Squat");
      return json({ id: `row-${calls.length}`, entry_id: url.pathname.split("/").at(-1), external_session_id: session.id, training_day: session.training_day,
        exercise_id: body.exercise.name ? "custom:new" : "key:back-squat", name, format: body.format, actual: { ...blank, ...body.actual }, notes: body.notes || null,
        revision: Number((call.body as { revision?: number }).revision ?? 0) + 1 });
    }
    if (url.pathname === `/api/training/sessions/${session.id}/exercises`) return json({ entries });
    if (url.pathname === "/api/training/sessions/old-session/exercises") return json({ entries: [{ id: "old", entry_id: "old-entry", external_session_id: "old-session",
      training_day: "2026-10-12", exercise_id: "key:back-squat", name: "Back Squat", format: "strength", actual: { ...blank, sets: 3, reps: 5, load: { value: 75, unit: "kg" } }, notes: null }] });
    if (url.pathname === "/api/history/exercise-library") return json({ exercises: [squatRecord], as_of: "2026-10-19T00:00:00Z", coverage: "retained_logs" });
    if (url.pathname === "/api/training/library") return json({ entries: [{ catalogue_id: "plank-id", name: "Plank", category: "training", exercise_key: "plank", log_format: "isometric" }],
      total: 1, catalogue_total: 1, offset: 0, limit: 6, next_offset: null, revision: "r", facets: {} });
    return json({});
  }) as typeof fetch;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const closed: (ManualExerciseEntry[] | null)[] = [];
  await act(async () => root.render(<ManualExerciseLogger token="token" session={session} reference={reference} onClose={value => closed.push(value)} />));
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
  const button = (text: string | RegExp) => [...container.querySelectorAll("button")].find(item =>
    typeof text === "string" ? item.textContent?.trim() === text || item.getAttribute("aria-label") === text : text.test(item.textContent ?? ""));
  const click = async (text: string | RegExp) => { const target = button(text); assert.ok(target, `Missing ${text}`); await act(async () => target.click()); };
  const type = async (selector: string, value: string) => {
    const input = container.querySelector<HTMLInputElement>(selector);
    assert.ok(input, `Missing ${selector}`);
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
    await act(async () => { setter.call(input, value); input.dispatchEvent(new window.Event("input", { bubbles: true })); });
  };
  const submit = async () => {
    await act(async () => container.querySelector("form")!.requestSubmit());
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
  };
  return { container, calls, closed, confirms, button, click, type, submit,
    cleanup: () => { act(() => root.unmount()); container.remove(); globalThis.fetch = originalFetch; window.confirm = originalConfirm; } };
}

const puts = (calls: Call[]) => calls.filter(call => call.method === "PUT");

test("entering a load reveals the unit switch once, and using it retires the tip", async () => {
  window.localStorage.removeItem("discovery-weight-unit-seen");
  const ui = await mount();
  try {
    await ui.click("Back Squat");
    await ui.type('[data-measure="load"]', "80");
    assert.match(ui.container.textContent!, /Tap the load unit to switch between kg and lb/);
    const unit = ui.container.querySelector<HTMLButtonElement>('[aria-label^="Load unit:"]');
    assert.ok(unit);
    await act(async () => unit.click());
    assert.equal(window.localStorage.getItem("discovery-weight-unit-seen"), "1");
    assert.doesNotMatch(ui.container.textContent!, /Tap the load unit to switch between kg and lb/);
  } finally { ui.cleanup(); window.localStorage.removeItem("discovery-weight-unit-seen"); }
});

test("individual-set guidance waits for input, yields to unit guidance and retires on use", async () => {
  const keys = ["discovery-individual-sets-seen", "discovery-weight-unit-seen"];
  keys.forEach(key => window.localStorage.removeItem(key));
  const ui = await mount();
  try {
    await ui.click("Back Squat");
    const hint = () => ui.container.querySelector('[aria-label="Dismiss individual sets tip"]');
    assert.equal(hint(), null);
    await ui.type('[data-measure="sets"]', "3");
    assert.equal(hint(), null, "prefilled weight shows the unit hint first");
    await ui.type('[data-measure="load"]', "80");
    assert.equal(hint(), null, "only the unit tip shows while it has priority");
    await ui.click("Dismiss weight unit tip");
    assert.ok(hint());
    await ui.click("Edit sets individually");
    assert.equal(hint(), null);
    assert.equal(window.localStorage.getItem(keys[0]), "1");
    assert.equal(ui.container.querySelectorAll('ol[aria-label="Set results"] li').length, 3);
  } finally { ui.cleanup(); keys.forEach(key => window.localStorage.removeItem(key)); }
});

test("saved exercise guidance appears after saving and dismissal survives reopening", async () => {
  const key = "discovery-saved-exercise-edit-seen";
  window.localStorage.removeItem(key);
  const ui = await mount();
  const tip = '[aria-label="Dismiss saved exercise edit tip"]';
  try {
    assert.equal(ui.container.querySelector(tip), null);
    await ui.click("Back Squat");
    await ui.submit();
    assert.ok(ui.container.querySelector(tip));
    await ui.click("Dismiss saved exercise edit tip");
    assert.equal(ui.container.querySelector(tip), null);
    assert.equal(window.localStorage.getItem(key), "1");
  } finally { ui.cleanup(); }
  const reopened = await mount();
  try {
    await reopened.click("Back Squat");
    await reopened.submit();
    assert.equal(reopened.container.querySelector(tip), null);
  } finally { reopened.cleanup(); window.localStorage.removeItem(key); }
});

test("a recent exercise becomes a compact saved row and the logger is ready for the next", async () => {
  const ui = await mount();
  try {
    await ui.click("Back Squat");
    assert.match(ui.container.textContent!, /Suggested 75 kg · Same as last/);
    assert.doesNotMatch(ui.container.textContent!, /Last time/);
    // Historical weight is an editable draft, not a recorded measurement.
    assert.equal(ui.container.querySelector<HTMLInputElement>('[data-measure="load"]')!.value, "75");
    await ui.type('[data-measure="sets"]', "3");
    await ui.type('[data-measure="reps"]', "5");
    await ui.type('[data-measure="load"]', "80");
    await ui.submit();
    const [put] = puts(ui.calls);
    assert.deepEqual(put.body, { exercise: { exercise_key: "back-squat" }, format: "strength", actual: { sets: 3, reps: 5, load: { value: 80, unit: "kg" } }, notes: "", revision: 0 });
    const row = ui.container.querySelector('ul[aria-label="Exercises in this session"] li')!;
    assert.match(row.textContent!, /Back Squat3 × 5 · 80 kg/);
    assert.ok(ui.container.querySelector('input[type="search"]'), "editor resets to search");
  } finally { ui.cleanup(); }
});

test("a new name alone is enough, with an explicit no-measurements row, and Done saves it", async () => {
  const ui = await mount();
  try {
    await ui.type('input[type="search"]', "Landmine   rotation");
    await ui.click(/Add “Landmine rotation”/);
    assert.ok(ui.container.querySelector('[role="group"][aria-label="How to record it"]'), "an unknown movement asks for a format");
    await ui.click("Bodyweight");
    await ui.click("Done");
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.deepEqual(puts(ui.calls)[0].body, { exercise: { name: "Landmine rotation" }, format: "bodyweight", actual: {}, notes: "", revision: 0 });
    // The sheet slides away before it closes.
    await act(async () => { await new Promise(resolve => setTimeout(resolve, SHEET_EXIT_MS + 20)); });
    assert.equal(ui.closed.length, 1);
    assert.equal(ui.closed[0]!.length, 1);
  } finally { ui.cleanup(); }
});

test("catalogue picks preselect a trusted format and invalid values stay beside their field", async () => {
  const ui = await mount();
  try {
    await ui.type('input[type="search"]', "plank");
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 300)); });
    await ui.click(/^PlankLibrary$/);
    assert.ok(ui.button(/Hold · Change/), "isometric layout chosen from the catalogue");
    await ui.type('[data-measure="sets"]', "0");
    await ui.submit();
    assert.equal(puts(ui.calls).length, 0);
    const field = ui.container.querySelector('[data-measure="sets"]')!;
    assert.equal(field.getAttribute("aria-invalid"), "true");
    assert.match(ui.container.querySelector(`#${field.getAttribute("aria-describedby")}`)!.textContent!, /At least 1/);
  } finally { ui.cleanup(); }
});

test("a failed save keeps the draft and retries with the same entry ID", async () => {
  let attempts = 0;
  const ui = await mount({ respond: call => call.method === "PUT" && ++attempts === 1 ? json({ detail: "bad request" }, 400) : undefined });
  try {
    await ui.click("Back Squat");
    await ui.type('[data-measure="sets"]', "3");
    await ui.submit();
    assert.match(ui.container.querySelector('[role="alert"]')!.textContent!, /Not saved/);
    assert.equal(ui.container.querySelector<HTMLInputElement>('[data-measure="sets"]')!.value, "3");
    await ui.submit();
    const [first, second] = puts(ui.calls);
    assert.equal(first.url, second.url);
    assert.equal(ui.container.querySelectorAll('ul[aria-label="Exercises in this session"] li').length, 1);
  } finally { ui.cleanup(); }
});

test("closing with an unfinished exercise asks first", async () => {
  const ui = await mount();
  try {
    await ui.click("Back Squat");
    await ui.click("Close exercises");
    assert.deepEqual(ui.confirms, ["Discard this exercise?"]);
    await act(async () => { await new Promise(resolve => setTimeout(resolve, SHEET_EXIT_MS + 20)); });
    assert.equal(ui.closed.length, 1);
  } finally { ui.cleanup(); }
});

test("repeating suggests recorded weights without saving until Add confirms them", async () => {
  const reference = { ...session, id: "old-session", training_day: "2026-10-12", exercise_count: 1 };
  const ui = await mount({ reference });
  try {
    await ui.click(/Reuse exercises from/);
    await ui.click("Log Back Squat again");
    assert.match(ui.container.textContent!, /Suggested 75 kg · Same as last/);
    assert.doesNotMatch(ui.container.textContent!, /Last time/);
    assert.equal(ui.container.querySelector<HTMLInputElement>('[data-measure="sets"]')!.value, "");
    await ui.submit();
    assert.deepEqual(puts(ui.calls)[0].body!.actual, { load: { value: 75, unit: "kg" } });
  } finally { ui.cleanup(); }
});

test("editing a saved row updates it in place and removal confirms", async () => {
  const saved = { id: "row", entry_id: "entry-1", external_session_id: session.id, training_day: session.training_day, exercise_id: "key:back-squat",
    name: "Back Squat", format: "strength" as const, actual: { ...blank, sets: 3, reps: 5, load: { value: 80, unit: "lb" as const } }, notes: null, revision: 3 };
  const ui = await mount({ entries: [saved] });
  try {
    await ui.click("Edit Back Squat");
    assert.equal(ui.container.querySelector<HTMLInputElement>('[data-measure="load"]')!.value, "80");
    assert.ok(ui.button("Load unit: lb. Change unit"), "stored unit is kept");
    await ui.type('[data-measure="load"]', "85");
    await ui.submit();
    assert.match(puts(ui.calls)[0].url, /entry-1$/);
    // The edit names the revision it was based on.
    assert.equal(puts(ui.calls)[0].body!.revision, 3);
    assert.equal(ui.container.querySelectorAll('ul[aria-label="Exercises in this session"] li').length, 1);
    assert.match(ui.container.textContent!, /3 × 5 · 85 lb/);
    await ui.click("Remove Back Squat");
    assert.deepEqual(ui.confirms, ["Remove Back Squat?"]);
    assert.equal(ui.calls.at(-1)!.method, "DELETE");
  } finally { ui.cleanup(); }
});

test("an edit refused as stale reloads the stored values instead of overwriting them", async () => {
  const saved = { id: "row", entry_id: "entry-1", external_session_id: session.id, training_day: session.training_day, exercise_id: "key:back-squat",
    name: "Back Squat", format: "strength" as const, actual: { ...blank, sets: 3, reps: 5, load: { value: 80, unit: "kg" as const } }, notes: null, revision: 1 };
  const latest = { ...saved, actual: { ...saved.actual, load: { value: 90, unit: "kg" as const } }, revision: 2 };
  let lists = 0;
  const ui = await mount({ respond: call => {
    if (call.method === "PUT") return json({ detail: "This exercise changed since you opened it. Reload to see the latest values." }, 409);
    if (call.url === `/api/training/sessions/${session.id}/exercises`) return json({ entries: [++lists === 1 ? saved : latest] });
    return undefined;
  } });
  try {
    await ui.click("Edit Back Squat");
    await ui.type('[data-measure="load"]', "85");
    await ui.submit();
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
    assert.match(ui.container.querySelector('[role="alert"]')!.textContent!, /changed since you opened it/);
    assert.match(ui.container.textContent!, /3 × 5 · 90 kg/);
    assert.ok(ui.container.querySelector('input[type="search"]'), "the stale edit is closed");
    assert.equal(puts(ui.calls).length, 1);
  } finally { ui.cleanup(); }
});

test("session details list manual exercises with only recorded values", async () => {
  const fetch = globalThis.fetch;
  globalThis.fetch = async () => json({ session: { ...session, exercise_count: 2 }, prescription_source: "manual", blocks: [
    { name: "Back Squat", kind: "exercise", source: "manual", format: "strength", prescribed: {}, actual: { ...blank, sets: 3, reps: 5, load: { value: 80, unit: "kg" } }, status: null, responses: [] },
    { name: "Landmine rotation", kind: "exercise", source: "manual", format: "bodyweight", prescribed: {}, actual: blank, status: null, responses: [] },
  ] });
  const container = document.createElement("div"); document.body.append(container);
  const root = createRoot(container);
  const edits: TrainingHistorySession[] = [];
  try {
    await act(async () => root.render(<SessionDetails row={session} token="token" cache={new Map() as SessionDetailsCache} onDismiss={() => {}} onEditExercises={row => edits.push(row)} />));
    const items = [...container.querySelectorAll("ol li")].map(item => item.textContent);
    assert.deepEqual(items, ["Back Squat3 × 5 · 80 kg", "Landmine rotationPerformed · no measurements recorded"]);
    assert.doesNotMatch(container.textContent!, /Prescribed/);
    await act(async () => [...container.querySelectorAll("button")].find(item => item.textContent === "Edit exercises")!.click());
    assert.equal(edits[0].id, session.id);
  } finally { act(() => root.unmount()); container.remove(); globalThis.fetch = fetch; }
});

test("Back Squat suggestion is editable, unconfirmed and updated only after Add", async () => {
  const ui = await mount();
  try {
    await ui.click("Back Squat");
    assert.match(ui.container.textContent!, /Suggested 75 kg · Same as last/);
    assert.equal(puts(ui.calls).length, 0);
    await ui.type('[data-measure="load"]', "80");
    assert.equal(puts(ui.calls).length, 0);
    await ui.submit();
    assert.deepEqual((puts(ui.calls)[0].body!.actual as { load: unknown }).load, { value: 80, unit: "kg" });
    await ui.click("Back Squat");
    assert.match(ui.container.textContent!, /Suggested 80 kg · Same as last/);
    assert.equal(ui.container.querySelector<HTMLInputElement>('[data-measure="load"]')!.value, "80");
    await ui.type('[data-measure="load"]', "");
    await ui.submit();
    assert.deepEqual(puts(ui.calls)[1].body!.actual, {});
  } finally { ui.cleanup(); }
});

test("manual set suggestions keep distinct historical weights and never overwrite an edited measurement", async () => {
  const ui = await mount({ respond: call => call.url.startsWith("/api/history/exercise-library") ? json({ exercises: [{
    ...squatRecord, latest_values: { ...blank, sets: 2, set_entries: [
      { reps: 8, load: { value: 115, unit: "kg" } }, { reps: 8, load: { value: 125, unit: "kg" } },
    ] },
  }] }) : undefined });
  try {
    await ui.click("Back Squat");
    const inputs = [...ui.container.querySelectorAll<HTMLInputElement>('input[aria-label^="Load kg, set"]')];
    assert.deepEqual(inputs.map(input => input.value), ["115", "125"]);
    assert.equal(puts(ui.calls).length, 0);
    await ui.type('[aria-label="Load kg, set 1"]', "110");
    await ui.submit();
    const actual = puts(ui.calls)[0].body!.actual as { set_entries: { load: { value: number } }[] };
    assert.deepEqual(actual.set_entries.map(row => row.load.value), [110, 125]);
  } finally { ui.cleanup(); }
});
