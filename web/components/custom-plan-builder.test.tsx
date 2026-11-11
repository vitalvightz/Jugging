import assert from "node:assert/strict";
import test from "node:test";
import { window } from "./test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { AppRouterContext } from "next/dist/shared/lib/app-router-context.shared-runtime";
import { ToastProvider } from "./toast-provider";
import { CustomPlanBuilder } from "./custom-plan-builder";
import type { CustomPlan, LibraryEntry } from "@/lib/api-schema.generated";

test("Token refresh keeps an opened plan and unsaved edits", async () => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  const id = "00000000-0000-4000-8000-000000000099";
  globalThis.fetch = (async (input) => {
    calls.push(String(input));
    return new Response(
      JSON.stringify({
        id,
        name: "Demo Plan",
        kind: "self_directed",
        sessions: [],
        incomplete: true,
        created_at: "2026-10-11T12:00:00Z",
        updated_at: "2026-10-11T12:00:00Z",
      }),
    );
  }) as typeof fetch;
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const router = {
    push() {},
    replace() {},
    refresh() {},
    back() {},
    forward() {},
    prefetch() {},
    bfcacheId: "fixture",
  };
  const render = (token: string) => (
    <AppRouterContext.Provider value={router}>
      <ToastProvider>
        <CustomPlanBuilder planId={id} accessToken={token} />
      </ToastProvider>
    </AppRouterContext.Provider>
  );
  try {
    await act(async () => root.render(render("first-token")));
    const input = host.querySelector<HTMLInputElement>("input")!;
    assert.equal(input.value, "Demo Plan");
    await act(async () => {
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!.call(input, "My edited plan");
      input.dispatchEvent(new window.Event("input", { bubbles: true }));
    });
    await act(async () => root.render(render("refreshed-token")));
    assert.equal(input.value, "My edited plan");
    assert.match(host.textContent!, /Unsaved changes/);
    // Only the plan read counts; the editor-opened analytics event is not a reload.
    assert.equal(calls.filter(call => !call.includes("/api/analytics/")).length, 1);
  } finally {
    await act(async () => root.unmount());
    host.remove();
    globalThis.fetch = originalFetch;
  }
});

test("rest days offer a pencil control that opens a new session", async () => {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const router = { push() {}, replace() {}, refresh() {}, back() {}, forward() {}, prefetch() {}, bfcacheId: "fixture" };
  try {
    await act(async () => root.render(
      <AppRouterContext.Provider value={router}>
        <ToastProvider><CustomPlanBuilder accessToken="test-token" /></ToastProvider>
      </AppRouterContext.Provider>,
    ));
    assert.match(host.textContent!, /Keep this day for recovery\./);
    const addSession = host.querySelector<HTMLButtonElement>('[aria-label="Add session on Monday"]')!;
    await act(async () => addSession.click());
    assert.equal(document.querySelector<HTMLInputElement>('input[value="Training"]')?.value, "Training");
    assert.match(document.body.textContent!, /New session/);
  } finally {
    await act(async () => root.unmount());
    host.remove();
  }
});

test("a new session only joins the plan once confirmed, and the bin removes it", async () => {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const router = { push() {}, replace() {}, refresh() {}, back() {}, forward() {}, prefetch() {}, bfcacheId: "fixture" };
  const confirm = window.confirm;
  window.confirm = () => true;
  const sheetButton = (label: string) => [...document.querySelectorAll<HTMLButtonElement>(".injury-sheet button")].find(b => b.textContent?.trim() === label);
  try {
    await act(async () => root.render(
      <AppRouterContext.Provider value={router}>
        <ToastProvider><CustomPlanBuilder accessToken="test-token" /></ToastProvider>
      </AppRouterContext.Provider>,
    ));
    assert.doesNotMatch(host.textContent!, /Unassigned|No day set/);
    await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Add session on Monday"]')!.click());
    await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Close"], .injury-sheet-close')?.click());
    assert.match(host.textContent!, /Rest day/, "closing a new session's sheet adds nothing");

    await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Add session on Monday"]')!.click());
    await act(async () => sheetButton("Add session")!.closest("form")!.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })));
    assert.equal(host.querySelectorAll('[aria-label^="Session "]').length, 1);
    assert.doesNotMatch(host.textContent!, /Unassigned|No day set/);
    assert.ok(host.querySelector('[aria-label="Add another session on Monday"]'));

    await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Remove Training"]')!.click());
    assert.equal(host.querySelectorAll('[aria-label^="Session "]').length, 0);
    assert.match(host.textContent!, /Rest day/);
  } finally {
    window.confirm = confirm;
    await act(async () => root.unmount());
    host.remove();
  }
});

// ---- Adding exercises inside the builder -------------------------------------------------

const libraryEntry = (catalogue_id: string, name: string, exercise_key: string | null = null): LibraryEntry => ({
  catalogue_id, name, content_type: "strength_power", category: "training", context: "Gym", focus: [], verified_goals: [],
  exercise_details: [], discovery_match: null, equipment: ["barbell"], equipment_state: "available", equipment_requirements: null,
  exercise_key, media: null, selection_boundary: "", prescription_allowed: false, log_format: null,
});
const bike = libraryEntry("cat-bike", "Bike sprints");
const squat = libraryEntry("cat-squat", "Back squat", "back_squat");
const blank = { mode: "reps", sets: null, reps: null, duration_seconds: null, rest_seconds: null, load_or_intensity: null, rounds: null,
  work_seconds: null, rpe: null, tempo: null, per_side: null, distance: null, distance_unit: null, prescription: null };
const formats: Record<string, unknown> = {
  "cat-bike": { dose: { ...blank, mode: "intervals", rounds: 5, work_seconds: 300, rest_seconds: 30 }, format_dose: null, original: "5x5min, 30s rest" },
  "cat-squat": { dose: null, format_dose: { ...blank, sets: 3, reps: 5 }, original: "" },
};
const squatHistory = { exercises: [{ id: "key:back_squat", name: "Back squat", identity_kind: "canonical", latest_day: "2026-10-30",
  latest_values: { sets: 5, reps: 3, rounds: null, load: { value: 100, unit: "kg" }, duration: null, work: null, rest: null, distance: null, effort: null, set_entries: null },
  latest_confirmed_as_prescribed: true, best_loads: [], latest_source: "plan", log_format: "strength" }] };

type Route = (url: URL, init: RequestInit | undefined) => unknown;
/** Answers the builder's reads by path; anything unlisted is an empty object. */
function mockApi(routes: Record<string, Route>) {
  const original = globalThis.fetch;
  const requests: Array<{ path: string; method: string; body: unknown }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    requests.push({ path: url.pathname, method: init?.method ?? "GET", body: init?.body ? JSON.parse(String(init.body)) : null });
    const handler = routes[url.pathname];
    if (!handler && url.pathname !== "/api/analytics/events") throw new Error(`Unexpected API request: ${url.pathname}`);
    const value = handler ? await handler(url, init) : { accepted: true };
    return new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
  return { requests, restore: () => { globalThis.fetch = original; } };
}
const library: Record<string, Route> = {
  "/api/training/library/entry": url => ({ ...[bike, squat].find(e => e.catalogue_id === url.searchParams.get("catalogue_id")),
    source: "bank", source_revision: "r", row_revision: "r", guidance: { Cues: ["Stay controlled."] }, metadata_projection: [], equipment_source: {} }),
  "/api/training/library": url => {
    const q = url.searchParams.get("q")?.toLowerCase() ?? "";
    const entries = [bike, squat].filter(e => e.name.toLowerCase().includes(q));
    return { entries, total: entries.length, catalogue_total: 2, offset: 0, limit: 24, next_offset: null, revision: "r", facets: {} };
  },
  "/api/custom-plans/source-suggestion": url => formats[url.searchParams.get("catalogue_id")!] ?? { dose: null, format_dose: null, original: "" },
  "/api/history/exercise-library": () => squatHistory,
};

const wait = (ms = 0) => act(async () => { await new Promise(resolve => setTimeout(resolve, ms)); });
const topSheet = () => [...document.querySelectorAll<HTMLElement>('[role="dialog"]')].at(-1)!;
function type(element: Element, value: string) {
  const proto = element instanceof window.HTMLSelectElement ? window.HTMLSelectElement.prototype
    : element instanceof window.HTMLTextAreaElement ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(element, value);
  element.dispatchEvent(new window.Event(element instanceof window.HTMLSelectElement ? "change" : "input", { bubbles: true }));
}
const field = (scope: ParentNode, label: string) => scope.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
const button = (scope: ParentNode, text: string) => [...scope.querySelectorAll<HTMLButtonElement>("button")].find(b => b.textContent?.trim() === text)!;
const submit = (form: HTMLFormElement) => form.dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));

async function mountBuilder(props: { accessToken: string; planId?: string }) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const router = { push() {}, replace() {}, refresh() {}, back() {}, forward() {}, prefetch() {}, bfcacheId: "fixture" };
  await act(async () => root.render(
    <AppRouterContext.Provider value={router}>
      <ToastProvider><CustomPlanBuilder {...props} /></ToastProvider>
    </AppRouterContext.Provider>,
  ));
  return { host, unmount: async () => { await act(async () => root.unmount()); host.remove(); } };
}
/** New session on `day` through the session sheet, which hands straight on to the add sheet. */
async function addSession(host: HTMLElement, day: string, type?: string) {
  await act(async () => host.querySelector<HTMLButtonElement>(`[aria-label^="${day},"]`)!.click());
  await act(async () => host.querySelector<HTMLButtonElement>(`[aria-label="Add session on ${day}"]`)!.click());
  const sheet = topSheet();
  assert.equal(field(sheet, "Day").value, day, "a new session starts on the day being viewed");
  if (type) await act(async () => button(sheet, type).click());
  await act(async () => submit(sheet.querySelector("form")!));
}
async function search(text: string) {
  await act(async () => type(topSheet().querySelector('input[type="search"]')!, text));
  await wait(260);
  await wait();
}

test("several library exercises go in from one sheet, each starting from its verified format", async () => {
  let stored: unknown;
  const api = mockApi({
    ...library,
    "/api/custom-plans/plan-1": () => stored,
    "/api/custom-plans": (url, init) => {
      const body = JSON.parse(String(init!.body));
      return stored = { id: "plan-1", kind: "self_directed", incomplete: false, created_at: "2026-11-02T00:00:00Z", updated_at: "2026-11-02T00:00:00Z", ...body,
        sessions: body.sessions.map((s: CustomPlan["sessions"][number]) => ({ ...s, exercises: s.exercises.map(e => e.origin === "catalogue"
          ? { ...e, snapshot: [bike, squat].find(entry => entry.catalogue_id === e.catalogue_id) } : e) })) };
    },
  });
  const ui = await mountBuilder({ accessToken: "multi-add" });
  try {
    await addSession(ui.host, "Wednesday", "Strength / power");
    assert.equal(topSheet().querySelector("h2")!.textContent, "Add to Strength & Power", "the name follows the type and adding opens straight away");
    await search("s");
    await act(async () => topSheet().querySelector<HTMLButtonElement>('[aria-label="Add Bike sprints"]')!.click());
    await act(async () => topSheet().querySelector<HTMLButtonElement>('[aria-label="Add Back squat"]')!.click());
    await wait(); await wait();
    const sheet = topSheet();
    assert.ok(sheet.querySelector('input[type="search"]'), "the add sheet stays open for the next exercise");
    assert.doesNotMatch(document.body.textContent!, /Find a Drill/, "no trip to the full library screen");
    // Each added exercise is one line; only the newest is open for its dose.
    const row = (name: string) => sheet.querySelector<HTMLElement>(`[role="group"][aria-label="${name}"]`)!;
    assert.equal(row("Bike sprints").querySelector("button[aria-expanded]")!.getAttribute("aria-expanded"), "false");
    assert.equal(field(row("Bike sprints"), "Rounds"), null, "a collapsed row shows only its summary");
    assert.match(row("Bike sprints").textContent!, /5 × 5 min · rest 30s/);
    const squatRow = row("Back squat");
    assert.equal(field(squatRow, "Sets").value, "3", "the library's sets, not last session's");
    assert.equal(field(squatRow, "Reps").value, "5");
    assert.match(squatRow.textContent!, /3 × 5 · 100 kg[\s\S]*Library format · your last session/);
    await act(async () => row("Bike sprints").querySelector<HTMLButtonElement>("button[aria-expanded]")!.click());
    assert.equal(field(row("Bike sprints"), "Rounds").value, "5");
    assert.match(row("Bike sprints").textContent!, /Library format/);
    assert.equal(field(row("Back squat"), "Sets"), null, "opening one closes the other");
    assert.doesNotMatch(sheet.textContent!, /to finish/, "both doses are complete");

    await act(async () => button(sheet, "Done").click());
    await wait(250);
    const session = ui.host.querySelector<HTMLElement>('[aria-label="Session 1"]')!;
    assert.match(session.textContent!, /Wednesday/);
    assert.equal(session.querySelectorAll("li").length, 2);

    await act(async () => button(ui.host, "Save draft").click());
    await wait();
    const saved = api.requests.find(r => r.path === "/api/custom-plans" && r.method === "POST")!.body as CustomPlan;
    assert.equal(saved.sessions[0].weekday, "Wednesday");
    assert.equal(saved.sessions[0].session_type, "strength_power");
    assert.deepEqual(saved.sessions[0].exercises.map(e => e.dose.rounds ?? e.dose.sets), [5, 3]);
    assert.equal(saved.sessions[0].exercises[1].dose.load_or_intensity, "100 kg");
    assert.deepEqual(saved.sessions[0].exercises[0].coaching_cues, ["Stay controlled."], "search results gain the library's cues");
    await ui.unmount();

    // Reopening shows exactly what was saved, on the same day.
    const reopened = await mountBuilder({ accessToken: "multi-add", planId: "plan-1" });
    try {
      await wait();
      const session = reopened.host.querySelector<HTMLElement>('[aria-label="Session 1"]')!;
      assert.match(session.textContent!, /Wednesday/);
      assert.match(session.textContent!, /Bike sprints[\s\S]*5 × 5 min · rest 30s/);
      assert.match(session.textContent!, /Back squat[\s\S]*3 × 5Suggested 100 kg/);
      assert.match(reopened.host.textContent!, /Saved/);
    } finally { await reopened.unmount(); }
  } finally {
    api.restore();
  }
});

test("a saved plan reopens with its days, sessions and doses", async () => {
  window.history.replaceState(null, "", "/plans/custom");
  const plan = { id: "plan-2", name: "Fight camp", kind: "self_directed", incomplete: false, created_at: "2026-11-02T00:00:00Z", updated_at: "2026-11-02T00:00:00Z",
    sessions: [{ id: "s1", name: "Strength & Power", weekday: "Wednesday", session_type: "strength_power", exercises: [
      { id: "e1", origin: "catalogue", catalogue_id: "cat-squat", snapshot: squat, incomplete: false, coaching_cues: null, stop_rules: null,
        dose: { ...blank, sets: 3, reps: 5, load_or_intensity: "100 kg" } }] }] };
  const api = mockApi({ ...library, "/api/custom-plans/plan-2": () => plan });
  const ui = await mountBuilder({ accessToken: "reopen", planId: "plan-2" });
  try {
    await wait();
    const session = ui.host.querySelector<HTMLElement>('[aria-label="Session 1"]')!;
    assert.match(session.textContent!, /Wednesday/);
    assert.match(session.textContent!, /Back squat/);
    assert.match(session.textContent!, /3 × 5Suggested 100 kg/);
    assert.match(ui.host.textContent!, /Saved/);
  } finally {
    await ui.unmount();
    api.restore();
  }
});

test("a late suggestion never overwrites a dose the athlete already typed", async () => {
  let answer!: (value: unknown) => void;
  const api = mockApi({ ...library, "/api/custom-plans/source-suggestion": () => new Promise(resolve => { answer = resolve; }) });
  const ui = await mountBuilder({ accessToken: "late-suggestion" });
  try {
    await addSession(ui.host, "Monday");
    await search("squat");
    await act(async () => topSheet().querySelector<HTMLButtonElement>('[aria-label="Add Back squat"]')!.click());
    await wait();
    const row = () => topSheet().querySelector<HTMLElement>('[role="group"][aria-label="Back squat"]')!;
    assert.match(row().textContent!, /Checking the library and your history/);
    assert.equal(row().dataset.attention, "true", "an unset dose stands out even when collapsed");
    assert.match(topSheet().textContent!, /Added · 1 · 1 to finish/);
    await act(async () => type(field(row(), "Sets"), "4"));
    await act(async () => answer(formats["cat-squat"]));
    await wait(); await wait();
    assert.equal(field(row(), "Sets").value, "4", "the athlete's value stays");
    assert.equal(field(row(), "Reps").value, "", "nothing is filled in around it either");
    assert.match(row().textContent!, /Your dose/);
    await act(async () => button(row(), "More").click());
    assert.equal(field(topSheet(), "Sets").value, "4", "the full editor shows the same dose");
  } finally {
    await ui.unmount();
    api.restore();
  }
});

test("a late suggestion fills an open, untouched editor instead of being saved over", async () => {
  let answer!: (value: unknown) => void;
  const api = mockApi({ ...library, "/api/custom-plans/source-suggestion": () => new Promise(resolve => { answer = resolve; }) });
  const ui = await mountBuilder({ accessToken: "late-editor" });
  try {
    await addSession(ui.host, "Monday");
    await search("squat");
    await act(async () => topSheet().querySelector<HTMLButtonElement>('[aria-label="Add Back squat"]')!.click());
    await wait();
    await act(async () => button(topSheet().querySelector('[role="group"][aria-label="Back squat"]')!, "More").click());
    await act(async () => answer(formats["cat-squat"]));
    await wait(); await wait();
    const editor = topSheet();
    assert.equal(field(editor, "Sets").value, "3");
    await act(async () => submit(editor.querySelector("form")!));
    await wait(250);
    assert.equal(field(topSheet().querySelector('[role="group"][aria-label="Back squat"]')!, "Sets").value, "3");
  } finally {
    await ui.unmount();
    api.restore();
  }
});

test("a failed search never leaves old exercises available to add", async () => {
  const api = mockApi({ ...library, "/api/training/library": url => {
    if (url.searchParams.get("q") === "broken") throw new Error("Search failed");
    return library["/api/training/library"](url, undefined);
  } });
  const ui = await mountBuilder({ accessToken: "search-failure" });
  try {
    await addSession(ui.host, "Monday");
    await search("squat");
    assert.ok(topSheet().querySelector('[aria-label="Add Back squat"]'));
    await search("broken");
    assert.match(topSheet().textContent!, /Library search unavailable/);
    assert.equal(topSheet().querySelector('[aria-label="Add Back squat"]'), null);
    assert.equal(topSheet().querySelector('[aria-label="Library results"]'), null);
  } finally {
    await ui.unmount();
    api.restore();
  }
});

test("returning from full library keeps the exercise search", async () => {
  const api = mockApi(library);
  const ui = await mountBuilder({ accessToken: "search-return" });
  try {
    await addSession(ui.host, "Monday");
    await search("squat");
    await act(async () => button(topSheet(), "Filters & community").click());
    assert.match(document.body.textContent!, /Find a Drill/);
    await act(async () => button(ui.host, "← Back to plan").click());
    assert.equal((topSheet().querySelector('input[type="search"]') as HTMLInputElement).value, "squat");
  } finally {
    await ui.unmount();
    api.restore();
  }
});

test("a personal exercise starts from the search text, with extras folded away", async () => {
  const api = mockApi(library);
  const ui = await mountBuilder({ accessToken: "manual" });
  try {
    await addSession(ui.host, "Monday");
    await search("Bag rounds");
    assert.match(topSheet().textContent!, /No library match/);
    await act(async () => button(topSheet(), "+ Create “Bag rounds”").click());
    const editor = topSheet();
    assert.equal(editor.querySelector("h2")!.textContent, "Create my exercise");
    assert.equal(field(editor, "Sets").closest("details"), null, "the dose itself is never folded");
    assert.ok(!button(editor, "Add YouTube demo (optional)").closest("details")!.open, "video and sharing start folded");
    assert.ok(!field(editor, "Cues").closest("details")!.open, "cues and stop rules start folded");
    assert.ok(!field(editor, "RPE (optional)").closest("details")!.open, "effort and tempo start folded");
    assert.equal((editor.querySelector("input[required]") as HTMLInputElement).value, "Bag rounds");
    await act(async () => type(field(editor, "Sets"), "3"));
    await act(async () => type(field(editor, "Reps"), "10"));
    await act(async () => submit(editor.querySelector("form")!));
    await wait(250);
    const row = topSheet().querySelector<HTMLElement>('[role="group"][aria-label="Bag rounds"]')!;
    assert.match(row.textContent!, /3 × 10[\s\S]*Your exercise/);
    assert.equal(field(row, "Sets").value, "3");
  } finally {
    await ui.unmount();
    api.restore();
  }
});

test("a set extra keeps its fold open", async () => {
  const plan = { id: "plan-3", name: "Camp", kind: "self_directed", incomplete: false, created_at: "2026-11-02T00:00:00Z", updated_at: "2026-11-02T00:00:00Z",
    sessions: [{ id: "s1", name: "Training", weekday: "Monday", session_type: "mixed", exercises: [
      { id: "e1", origin: "catalogue", catalogue_id: "cat-squat", snapshot: squat, incomplete: false, coaching_cues: null, stop_rules: null,
        dose: { ...blank, sets: 3, reps: 5, rpe: 8 } }] }] };
  const api = mockApi({ ...library, "/api/custom-plans/plan-3": () => plan });
  const ui = await mountBuilder({ accessToken: "fold", planId: "plan-3" });
  try {
    await wait();
    await act(async () => ui.host.querySelector<HTMLButtonElement>('[aria-label="Edit dose for Back squat"]')!.click());
    const details = field(topSheet(), "RPE (optional)").closest("details")!;
    assert.ok(details.open);
    assert.match(details.querySelector("summary")!.textContent!, /RPE 8/);
  } finally {
    await ui.unmount();
    api.restore();
  }
});

test("Back closes the builder's own sheets first; only leaving the plan asks to discard", async () => {
  const api = mockApi(library);
  window.history.replaceState(null, "", "/plans/custom");
  const ui = await mountBuilder({ accessToken: "guard" });
  const confirm = window.confirm, pushState = window.history.pushState;
  const asked: string[] = [];
  let pushes = 0;
  window.confirm = (message?: string) => { asked.push(String(message)); return false; };
  window.history.pushState = function (...args: Parameters<History["pushState"]>) { pushes++; return pushState.apply(this, args); };
  try {
    await act(async () => ui.host.querySelector<HTMLButtonElement>('[aria-label="Add session on Monday"]')!.click());
    assert.equal(pushes, 0, "an untouched new-session sheet is not an unsaved change");
    await act(async () => submit(topSheet().querySelector("form")!));
    assert.equal(pushes, 1, "the first real change arms the guard once");
    assert.match(topSheet().querySelector("h2")!.textContent!, /^Add to /);

    // Filters & community opens the full library over the add sheet; Back returns to the sheet.
    await act(async () => button(topSheet(), "Filters & community").click());
    assert.match(document.body.textContent!, /Find a Drill/);
    await act(async () => window.dispatchEvent(new window.PopStateEvent("popstate")));
    assert.doesNotMatch(document.body.textContent!, /Find a Drill/);
    assert.match(topSheet().querySelector("h2")!.textContent!, /^Add to /, "back on the add sheet, not the plan");

    await act(async () => window.dispatchEvent(new window.PopStateEvent("popstate")));
    assert.deepEqual(asked, [], "Back from the add sheet is not leaving the plan");
    assert.equal(document.querySelector('[role="dialog"]'), null, "it closes the sheet");

    await act(async () => window.dispatchEvent(new window.PopStateEvent("popstate")));
    assert.deepEqual(asked, ["Discard unsaved changes?"], "Back from the plan itself still protects the draft");
  } finally {
    window.confirm = confirm;
    window.history.pushState = pushState;
    await ui.unmount();
    api.restore();
  }
});

test("editing and reverting a sheet never stacks extra Back entries", async () => {
  const plan = { id: "plan-4", name: "Camp", kind: "self_directed", incomplete: false, created_at: "2026-11-02T00:00:00Z", updated_at: "2026-11-02T00:00:00Z",
    sessions: [{ id: "s1", name: "Training", weekday: "Monday", session_type: "mixed", exercises: [] }] };
  const api = mockApi({ "/api/custom-plans/plan-4": () => plan });
  window.history.replaceState(null, "", "/plans/custom");
  const pushState = window.history.pushState;
  let pushes = 0;
  window.history.pushState = function (...args: Parameters<History["pushState"]>) { pushes++; return pushState.apply(this, args); };
  const ui = await mountBuilder({ accessToken: "stack", planId: "plan-4" });
  try {
    await wait();
    await act(async () => ui.host.querySelector<HTMLButtonElement>('[aria-label="Edit session 1"]')!.click());
    const name = topSheet().querySelector<HTMLInputElement>("input[required]")!;
    for (const value of ["Pads", "Training", "Pads again"]) await act(async () => type(name, value));
    assert.equal(pushes, 1);
  } finally {
    window.history.pushState = pushState;
    await ui.unmount();
    api.restore();
  }
});
