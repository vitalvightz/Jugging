import test from "node:test";
import assert from "node:assert/strict";

import "./test-dom";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";

import { ExerciseLogProvider, type ExerciseLogging } from "./exercise-log";
import { ExerciseRow } from "./structured-plan-renderer";
import type { ExerciseLogRecord, ExerciseLogRequest, ExerciseRecentLoad, StructuredBlock } from "@/lib/types";

const deadlift: StructuredBlock = {
  block_id: "blk-deadlift",
  block_type: "strength",
  display_name: "Trap Bar Deadlift",
  sets: 4,
  reps: 8,
  effort: { method: "RPE", value: 7 },
  // The server has cleared this lift to progress (see api/rep_range.py).
  progression_hold: false,
};

const plank: StructuredBlock = {
  block_id: "blk-plank",
  block_type: "accessory",
  display_name: "Front Plank",
  duration: { value: 3, unit: "minutes" },
};

function record(block: StructuredBlock, patch: Partial<ExerciseLogRecord> = {}): ExerciseLogRecord {
  return {
    id: "log-1",
    athlete_id: "athlete-1",
    plan_id: "plan-1",
    session_id: "s1",
    block_id: block.block_id ?? "",
    exercise_key: null,
    training_day: "2026-10-12",
    status: "as_prescribed",
    reason: null,
    prescribed: block,
    actual: {},
    notes: "",
    created_at: "",
    updated_at: "",
    ...patch,
  };
}

type Saved = Omit<ExerciseLogRequest, "plan_id">;

/** Mounts one open row with live logging and returns what it saved. */
function mount(
  block: StructuredBlock,
  options: {
    logs?: Record<string, ExerciseLogRecord>;
    painReasonAllowed?: boolean;
    recentPerformances?: ExerciseLogRecord[];
    recentLoads?: ExerciseRecentLoad[];
    allowProgression?: boolean;
    canWrite?: boolean;
    hideWeightGuidance?: boolean;
    fail?: string;
    open?: boolean;
  } = {},
) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const saved: Saved[] = [];
  const errors: string[] = [];
  let toggles = 0;
  let logs = options.logs ?? {};
  let root: Root;
  const render = () => {
    const logging: ExerciseLogging = {
      logs,
      canWrite: options.canWrite,
      hideWeightGuidance: options.hideWeightGuidance,
      painReasonAllowed: options.painReasonAllowed ?? true,
      recentPerformances: options.recentPerformances,
      recentLoads: options.recentLoads,
      allowProgression: options.allowProgression,
      onError: (message) => errors.push(message),
      save: async (request) => {
        if (options.fail) {
          throw new Error(options.fail);
        }
        saved.push(request);
        logs = {
          ...logs,
          [request.block_id]: record(block, {
            status: request.status,
            reason: request.reason ?? null,
            notes: request.notes ?? "",
            actual: request.actual ?? {},
          }),
        };
        render();
      },
    };
    root.render(
      <ExerciseLogProvider logging={logging}>
        <ExerciseRow block={block} open={options.open ?? true} onToggle={() => (toggles += 1)} />
      </ExerciseLogProvider>,
    );
  };
  act(() => {
    root = createRoot(container);
    render();
  });
  const button = (name: string) =>
    Array.from(container.querySelectorAll("button")).find((node) => node.textContent?.trim() === name);
  const press = async (target: Element) => {
    await act(async () => {
      target.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    });
  };
  const click = async (name: string) => {
    const target = button(name);
    assert.ok(target, `button "${name}" not found`);
    await press(target);
  };
  const type = async (label: string, value: string) => {
    const field = Array.from(container.querySelectorAll("label")).find((node) =>
      node.textContent?.startsWith(label),
    );
    const input = field?.querySelector("input");
    assert.ok(input, `input "${label}" not found`);
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
      setter.call(input, value);
      input.dispatchEvent(new window.Event("input", { bubbles: true }));
    });
  };
  return {
    container,
    saved,
    errors,
    toggled: () => toggles,
    button,
    press,
    click,
    type,
    text: () => container.textContent ?? "",
    unmount: () => {
      act(() => root.unmount());
      container.remove();
    },
  };
}

test("a row renders no log controls unless a started session provides logging", () => {
  const html = renderToStaticMarkup(<ExerciseRow block={deadlift} open onToggle={() => {}} />);
  assert.equal(html.includes("ex-log"), false);

  const closed = renderToStaticMarkup(
    <ExerciseLogProvider logging={null}>
      <ExerciseRow block={deadlift} open onToggle={() => {}} />
    </ExerciseLogProvider>,
  );
  assert.equal(closed.includes("ex-log"), false);
});

test("rehab blocks and blocks without a server id are never loggable", () => {
  const logging: ExerciseLogging = { logs: {}, painReasonAllowed: true, save: async () => {} };
  const render = (block: StructuredBlock) =>
    renderToStaticMarkup(
      <ExerciseLogProvider logging={logging}>
        <ExerciseRow block={block} open onToggle={() => {}} />
      </ExerciseLogProvider>,
    );
  assert.equal(render({ ...deadlift, block_type: "rehab" }).includes("ex-log"), false);
  assert.equal(render({ ...deadlift, block_id: null }).includes("ex-log"), false);
  assert.equal(render(deadlift).includes("ex-log-tick"), true);
});

test("the prescription stays read-only: the stats are not inputs", () => {
  const view = mount(deadlift);
  assert.equal(view.container.querySelectorAll(".sp-block-stats input, .sp-block-stats button.sp-stat").length, 0);
  view.unmount();
});

test("the tick logs a collapsed exercise as written in one tap", async () => {
  const view = mount(deadlift, { open: false });
  const tick = view.container.querySelector<HTMLButtonElement>(".ex-log-tick");
  assert.equal(tick?.getAttribute("aria-label"), "Mark Trap Bar Deadlift done as written");
  await view.press(tick!);

  assert.deepEqual(view.saved, [{ block_id: "blk-deadlift", status: "as_prescribed" }]);
  const logged = view.container.querySelector(".ex-log-tick");
  assert.equal(logged?.getAttribute("data-status"), "as_prescribed");
  assert.equal(logged?.getAttribute("aria-label"), "Trap Bar Deadlift: done. Change");
  // A plain "done" needs no extra line under the name.
  assert.equal(view.container.querySelector(".ex-row-log"), null);
  view.unmount();
});

test("tapping the tick of a logged exercise opens it instead of saving again", async () => {
  const view = mount(deadlift, { open: false, logs: { "blk-deadlift": record(deadlift) } });
  await view.press(view.container.querySelector<HTMLButtonElement>(".ex-log-tick")!);

  assert.deepEqual(view.saved, []);
  assert.equal(view.toggled(), 1);
  view.unmount();
});

test("a failed one-tap save is reported and leaves the exercise unlogged", async () => {
  const view = mount(deadlift, { open: false, fail: "Start today's session before logging an exercise." });
  await view.press(view.container.querySelector<HTMLButtonElement>(".ex-log-tick")!);

  assert.deepEqual(view.errors, ["Start today's session before logging an exercise."]);
  assert.equal(view.container.querySelector(".ex-log-tick")?.hasAttribute("data-status"), false);
  view.unmount();
});

test("one tap inside the open row logs it as written", async () => {
  const view = mount(deadlift);
  await view.click("Done");

  assert.deepEqual(view.saved, [{ block_id: "blk-deadlift", status: "as_prescribed" }]);
  assert.equal(view.container.querySelector(".ex-log-status")?.textContent, "Done");
  assert.equal(view.container.querySelector(".ex-log-edit")?.getAttribute("aria-label"), "Done. Change");
  view.unmount();
});

test("logging different numbers saves only the change, with the chosen reason", async () => {
  const view = mount(deadlift);
  await view.click("Log numbers");
  // No reason is asked for until something actually departs from the plan.
  assert.equal(view.container.querySelector(".ex-log-reasons"), null);
  await view.type("Sets", "3");
  await view.type("Load", "80");
  assert.ok(view.container.querySelector(".ex-log-reasons"));
  await view.click("No kit");
  await view.click("Save");

  assert.deepEqual(view.saved, [
    {
      block_id: "blk-deadlift",
      status: "modified",
      actual: { sets: 3, load: { value: 80, unit: "kg" } },
      reason: "equipment",
    },
  ]);
  // Prescribed and actual on one line.
  assert.equal(
    view.container.querySelector(".ex-log-status")?.textContent,
    "Changed3 sets not 480 kgNo kit",
  );
  view.unmount();
});

test("adding the weight used does not turn the log into a change", async () => {
  const view = mount(deadlift);
  await view.click("Log numbers");
  await view.type("Load", "100");
  await view.click("Save");

  assert.deepEqual(view.saved, [
    { block_id: "blk-deadlift", status: "as_prescribed", actual: { load: { value: 100, unit: "kg" } }, reason: null },
  ]);
  view.unmount();
});

test("an entry far from the prescription is confirmed before it is saved", async () => {
  const view = mount(plank);
  await view.click("Log numbers");
  await view.type("Duration", "15");
  await view.click("Save");

  assert.deepEqual(view.saved, []);
  assert.equal(view.container.querySelector(".ex-log-confirm")?.textContent, "15 min against 3 prescribed?");

  await view.click("Yes, save");
  assert.deepEqual(view.saved, [
    {
      block_id: "blk-plank",
      status: "modified",
      actual: { duration: { value: 15, unit: "minutes" } },
      reason: null,
    },
  ]);
  view.unmount();
});

test("changing the number after the prompt asks again instead of saving", async () => {
  const view = mount(plank);
  await view.click("Log numbers");
  await view.type("Duration", "15");
  await view.click("Save");
  await view.type("Duration", "150");
  assert.equal(view.container.querySelector(".ex-log-confirm"), null);
  await view.click("Save");

  assert.deepEqual(view.saved, []);
  assert.equal(view.container.querySelector(".ex-log-confirm")?.textContent?.includes("150 min"), true);
  view.unmount();
});

test("an unusable number is pointed at and nothing is saved", async () => {
  const view = mount(deadlift);
  await view.click("Log numbers");
  await view.type("Sets", "abc");
  await view.click("Save");

  assert.deepEqual(view.saved, []);
  assert.equal(view.container.querySelector('[role="alert"]')?.textContent, "Check Sets.");
  assert.equal(view.container.querySelector('input[aria-invalid="true"]') !== null, true);
  view.unmount();
});

test("skipping saves in one tap; the reason is a second, optional tap", async () => {
  const view = mount(deadlift);
  await view.click("Skip");
  assert.deepEqual(view.saved, [{ block_id: "blk-deadlift", status: "skipped" }]);
  // Nobody skips because they felt strong.
  assert.equal(view.button("Felt strong"), undefined);

  await view.click("Too tired");
  assert.deepEqual(view.saved[1], { block_id: "blk-deadlift", status: "skipped", reason: "fatigue" });
  assert.equal(view.button("Too tired")?.getAttribute("aria-pressed"), "true");
  view.unmount();
});

test("the pain reason is only offered with health consent", async () => {
  const withConsent = mount(deadlift);
  await withConsent.click("Skip");
  assert.ok(withConsent.button("Pain"));
  withConsent.unmount();

  const without = mount(deadlift, { painReasonAllowed: false });
  await without.click("Skip");
  assert.equal(without.button("Pain"), undefined);
  assert.ok(without.button("Too tired"));
  without.unmount();
});

test("a saved log can be changed to a different outcome", async () => {
  const view = mount(deadlift, {
    logs: { "blk-deadlift": record(deadlift, { status: "modified", actual: { sets: 3 }, reason: "fatigue" }) },
  });
  assert.equal(view.container.querySelector(".ex-log-status")?.textContent, "Changed3 sets not 4Too tired");
  const edit = () => view.container.querySelector<HTMLButtonElement>(".ex-log-edit")!;
  assert.equal(edit().getAttribute("aria-label"), "Changed, 3 sets not 4, Too tired. Change");
  await view.press(edit());
  await view.click("Log numbers");
  // The editor opens on what was saved.
  const sets = view.container.querySelector<HTMLInputElement>(".ex-log-field input");
  assert.equal(sets?.value, "3");
  await view.click("Cancel");
  await view.press(edit());
  await view.click("Done");

  assert.deepEqual(view.saved, [{ block_id: "blk-deadlift", status: "as_prescribed" }]);
  view.unmount();
});

test("a failed save keeps the entry and shows the server's message", async () => {
  const view = mount(deadlift, { fail: "Start today's session before logging an exercise." });
  await view.click("Log numbers");
  await view.type("Sets", "3");
  await view.click("Save");

  assert.equal(
    view.container.querySelector('[role="alert"]')?.textContent,
    "Start today's session before logging an exercise.",
  );
  assert.equal(view.container.querySelector<HTMLInputElement>(".ex-log-field input")?.value, "3");
  view.unmount();
});

test("a collapsed row carries the numbers that were logged", () => {
  const logging: ExerciseLogging = {
    logs: { "blk-deadlift": record(deadlift, { status: "modified", actual: { sets: 3, load: { value: 80, unit: "kg" } } }) },
    painReasonAllowed: true,
    save: async () => {},
  };
  const html = renderToStaticMarkup(
    <ExerciseLogProvider logging={logging}>
      <ExerciseRow block={deadlift} open={false} onToggle={() => {}} />
      <ExerciseRow block={plank} open={false} onToggle={() => {}} />
    </ExerciseLogProvider>,
  );
  assert.equal(html.includes('class="ex-row-log" data-status="modified">Changed · 3 sets · 80 kg<'), true);
  assert.equal(html.split('class="ex-row-log"').length - 1, 1);
});


test("a struggled weight is offered eased off, at any readiness, and stays editable", async () => {
  const previous = record(deadlift, {
    plan_id: "previous-plan", reason: "fatigue", status: "modified",
    actual: { load: { value: 100, unit: "kg" }, effort: { method: "RPE", value: 9 } },
  });
  const view = mount(deadlift, { recentPerformances: [previous], allowProgression: false });
  assert.match(view.text(), /Suggested 90 kg · Last 100 kg/);
  await view.click("Log numbers");
  const load = view.container.querySelector<HTMLInputElement>('.ex-log-field input[id$="-load"]');
  assert.equal(load?.value, "90");
  // A suggestion is not a confirmed measurement.
  await view.click("Cancel");
  await view.click("Done");
  assert.deepEqual(view.saved[0], { block_id: "blk-deadlift", status: "as_prescribed" });
  view.unmount();
});

test("a stated reason other than fatigue is not read as the weight being too much", () => {
  const short = record(deadlift, { plan_id: "previous-plan", reason: "equipment", status: "modified",
    actual: { sets: 2, load: { value: 100, unit: "kg" } } });
  const view = mount(deadlift, { recentPerformances: [short] });
  assert.doesNotMatch(view.text(), /Ease to/);
  view.unmount();
});

test("last performance is visible collapsed and the suggested load remains editable", async () => {
  const block = { ...deadlift, progression_rule: "Add 2.5 kg when all sets complete." };
  const previous = record(deadlift, { plan_id: "previous-plan", actual: { load: { value: 80, unit: "kg" }, effort: { method: "RPE", value: 6 } } });
  const collapsed = mount(block, { open: false, recentPerformances: [previous], allowProgression: true });
  assert.equal(collapsed.text().includes("Last"), true);
  collapsed.unmount();
  const view = mount(block, { recentPerformances: [previous], allowProgression: true });
  assert.match(view.text(), /Suggested 82.5 kg · Last 80 kg/);
  await view.click("Log numbers");
  const load = view.container.querySelector<HTMLInputElement>('.ex-log-field input[id$="-load"]');
  assert.equal(load?.value, "82.5");
  await view.type("Load", "80");
  await view.click("Save");
  assert.deepEqual(view.saved[0].actual?.load, { value: 80, unit: "kg" });
  view.unmount();
});

test("planned per-set editor exposes optional RPE and keeps it separate from aggregate controls", async () => {
  const view = mount(deadlift);
  await view.click("Log numbers");
  await view.click("Edit sets individually");
  const inputs = view.container.querySelectorAll<HTMLInputElement>('input[aria-label^="RPE, set"]');
  assert.equal(inputs.length, 4);
  assert.ok([...inputs].every(input => input.value === "7"));
  assert.equal(view.container.querySelector('.ex-log-field input[id$="-effort"]'), null);
  view.unmount();
});

test("a first weighted lift with no weight on record asks for it once, after the log is saved", async () => {
  window.localStorage.clear();
  const view = mount(deadlift, { recentLoads: [] });
  assert.doesNotMatch(view.text(), /What weight did you use\?/);
  await view.click("Done");
  // Logging is never held up: the exercise is already saved as done.
  assert.deepEqual(view.saved, [{ block_id: "blk-deadlift", status: "as_prescribed" }]);
  assert.match(view.text(), /What weight did you use\?/);
  await view.type("What weight did you use?", "abc");
  await view.click("Save weight");
  assert.match(view.text(), /Enter the weight as a number\./);
  assert.equal(view.saved.length, 1);
  await view.type("What weight did you use?", "60");
  await view.click("Save weight");
  assert.deepEqual(view.saved[1], {
    block_id: "blk-deadlift", status: "as_prescribed", reason: null, notes: "",
    actual: { load: { value: 60, unit: "kg" } },
  });
  assert.doesNotMatch(view.text(), /What weight did you use\?/);
  view.unmount();
});

test("the first-weight question can be skipped and does not come back for that exercise", async () => {
  window.localStorage.clear();
  const view = mount(deadlift, { recentLoads: [] });
  await view.click("Done");
  await view.click("Skip");
  assert.doesNotMatch(view.text(), /What weight did you use\?/);
  assert.equal(view.saved.length, 1);
  view.unmount();
  // Next session: neither the open row nor the tick raises it again.
  const later = mount(deadlift, { recentLoads: [], logs: { "blk-deadlift": record(deadlift) } });
  assert.doesNotMatch(later.text(), /What weight did you use\?/);
  later.unmount();
  const collapsed = mount(deadlift, { open: false, recentLoads: [] });
  await collapsed.press(collapsed.container.querySelector<HTMLButtonElement>(".ex-log-tick")!);
  assert.equal(collapsed.toggled(), 0);
  collapsed.unmount();
  window.localStorage.clear();
});

test("the tick opens the row for the first weight, and only when there is none to carry", async () => {
  window.localStorage.clear();
  const view = mount(deadlift, { open: false, recentLoads: [] });
  await view.press(view.container.querySelector<HTMLButtonElement>(".ex-log-tick")!);
  assert.deepEqual(view.saved, [{ block_id: "blk-deadlift", status: "as_prescribed" }]);
  assert.equal(view.toggled(), 1);
  view.unmount();
  const known = mount(deadlift, { open: false, recentLoads: [
    { exercise_key: null, display_name: "Trap Bar Deadlift", load: { value: 80, unit: "kg" }, training_day: "2026-10-01" },
  ] });
  await known.press(known.container.querySelector<HTMLButtonElement>(".ex-log-tick")!);
  assert.equal(known.saved[0].actual?.load, undefined);
  assert.equal(known.toggled(), 0);
  known.unmount();
  // Holds and jumps are never asked what they weighed.
  const hold = mount(plank, { open: false, recentLoads: [] });
  await hold.press(hold.container.querySelector<HTMLButtonElement>(".ex-log-tick")!);
  assert.equal(hold.toggled(), 0);
  hold.unmount();
});

test("how it felt is an optional tap on a saved lift and keeps the rest of the log", async () => {
  const saved = record(deadlift, { actual: { load: { value: 80, unit: "kg" }, sets: null } });
  const view = mount(deadlift, { logs: { "blk-deadlift": saved } });
  const group = view.container.querySelector('[aria-label="How did it feel? (optional)"]');
  assert.deepEqual(Array.from(group!.querySelectorAll("button")).map((node) => node.textContent), ["Easy", "Right", "Hard"]);
  await view.click("Easy");
  assert.deepEqual(view.saved[0], {
    block_id: "blk-deadlift", status: "as_prescribed", reason: null, notes: "",
    actual: { load: { value: 80, unit: "kg" }, feel: "easy" },
  });
  assert.equal(view.button("Easy")?.getAttribute("aria-pressed"), "true");
  // Tapping it again clears the answer.
  await view.click("Easy");
  assert.deepEqual(view.saved[1].actual, { load: { value: 80, unit: "kg" } });
  view.unmount();
  // Not offered on a skip or on work with no reps or weight.
  const skipped = mount(deadlift, { logs: { "blk-deadlift": record(deadlift, { status: "skipped" }) } });
  assert.equal(skipped.container.querySelector('[aria-label="How did it feel? (optional)"]'), null);
  skipped.unmount();
  const hold = mount(plank, { logs: { "blk-plank": record(plank) } });
  assert.equal(hold.container.querySelector('[aria-label="How did it feel? (optional)"]'), null);
  hold.unmount();
});

test("an easy session below the top of the rep range suggests a rep, not more weight", () => {
  const squat: StructuredBlock = { ...deadlift, sets: 3, reps: "6-8" };
  const previous = record(squat, { actual: { load: { value: 40, unit: "kg" }, reps: 6, feel: "easy" } });
  const view = mount(squat, { recentPerformances: [previous], allowProgression: true });
  assert.match(view.text(), /Try 7 reps/);
  view.unmount();
  // The same performance under a restriction suggests nothing.
  const held = mount(squat, { recentPerformances: [previous], allowProgression: false });
  assert.doesNotMatch(held.text(), /Try/);
  held.unmount();
});

test("tapping a suggestion sets the numbers up; nothing is recorded until the athlete saves", async () => {
  const squat: StructuredBlock = { ...deadlift, sets: 3, reps: 8, rep_range: [8, 12] };
  const previous = record(squat, { actual: { load: { value: 60, unit: "kg" }, reps: 9, feel: "easy" } });
  const recentLoads = [{ exercise_key: null, display_name: "Trap Bar Deadlift", load: { value: 60, unit: "kg" }, training_day: "2026-10-05" }];
  const view = mount(squat, { recentPerformances: [previous], recentLoads, allowProgression: true });
  await view.press(view.container.querySelector<HTMLButtonElement>(".ex-log-suggest")!);
  assert.deepEqual(view.saved, []);
  assert.equal(view.container.querySelector<HTMLInputElement>('.ex-log-field input[id$="-reps"]')?.value, "10");
  assert.equal(view.container.querySelector<HTMLInputElement>('.ex-log-field input[id$="-load"]')?.value, "60");
  // Backing out records nothing.
  await view.click("Cancel");
  assert.deepEqual(view.saved, []);
  await view.press(view.container.querySelector<HTMLButtonElement>(".ex-log-suggest")!);
  await view.click("Save");
  // Inside the permitted range it is the workout done, with no "why?" asked.
  assert.deepEqual(view.saved, [{ block_id: "blk-deadlift", status: "as_prescribed", reason: null,
    actual: { reps: 10, load: { value: 60, unit: "kg" } } }]);
  view.unmount();
});

test("done records the prescription, then offers last time's reps to confirm", async () => {
  const squat: StructuredBlock = { ...deadlift, sets: 3, reps: 8, rep_range: [8, 12] };
  const previous = record(squat, { actual: { reps: 9 } });
  const view = mount(squat, { recentPerformances: [previous] });
  await view.click("Done");
  assert.deepEqual(view.saved, [{ block_id: "blk-deadlift", status: "as_prescribed" }]);
  assert.match(view.text(), /Last time you reached 9 reps\./);
  await view.click("I did 9 again");
  assert.deepEqual(view.saved[1], { block_id: "blk-deadlift", status: "as_prescribed", reason: null, notes: "", actual: { reps: 9 } });
  // Confirmed: it is not asked again.
  assert.doesNotMatch(view.text(), /Last time you reached/);
  view.unmount();
  // The tick saves as written and opens the row for the same confirmation.
  const collapsed = mount(squat, { open: false, recentPerformances: [previous] });
  await collapsed.press(collapsed.container.querySelector<HTMLButtonElement>(".ex-log-tick")!);
  assert.deepEqual(collapsed.saved, [{ block_id: "blk-deadlift", status: "as_prescribed" }]);
  assert.equal(collapsed.toggled(), 1);
  collapsed.unmount();
  // A held block, or a reduced prescription, is never offered it.
  for (const block of [{ ...squat, progression_hold: true }, { ...squat, sets: 2 }]) {
    const held = mount(block, { recentPerformances: [previous] });
    await held.click("Done");
    assert.doesNotMatch(held.text(), /Last time you reached/);
    held.unmount();
  }
});

test("a held block is never offered a next step; one with no verdict is left as it was", () => {
  const previous = record(deadlift, { actual: { load: { value: 80, unit: "kg" }, feel: "easy" } });
  const recentLoads = [{ exercise_key: null, display_name: "Trap Bar Deadlift", load: { value: 80, unit: "kg" }, training_day: "2026-10-05" }];
  const held = mount({ ...deadlift, progression_hold: true }, { recentPerformances: [previous], recentLoads, allowProgression: true });
  assert.equal(held.container.querySelector(".ex-log-suggest"), null);
  held.unmount();
  const unknown = mount({ ...deadlift, progression_hold: undefined }, { recentPerformances: [previous], recentLoads, allowProgression: true });
  assert.match(unknown.container.querySelector(".ex-log-suggest")?.textContent ?? "", /Try 82\.5 kg/);
  unknown.unmount();
});

test("review weight confirmation fills only missing set weights and reaches next exposure", async () => {
  const existing = record(deadlift, { actual: { sets: 2, set_entries: [
    { set_number: 1, reps: 8, load: { value: 115, unit: "kg" }, rpe: 6 },
    { set_number: 2, reps: 7, load: null, rpe: 7 },
  ] } });
  const view = mount(deadlift, { logs: { [deadlift.block_id!]: existing }, recentLoads: [] });
  assert.match(view.text(), /Weight used for sets with missing weights/);
  await view.type("Weight used for sets with missing weights", "125");
  await view.click("Save weight");
  const actual = view.saved[0].actual!;
  assert.deepEqual(actual.set_entries, [
    existing.actual.set_entries![0],
    { ...existing.actual.set_entries![1], load: { value: 125, unit: "kg" } },
  ]);
  const { withHistoricalTimerLoads } = await import("@/lib/session-timer/history");
  const { buildTimerItems } = await import("@/lib/session-timer/plan");
  const items = withHistoricalTimerLoads(buildTimerItems([{ blocks: [deadlift] }]), [deadlift], [],
    [{ ...existing, actual }]);
  assert.ok(items[0].kind === "sets");
  assert.deepEqual(items[0].weightRecommendation?.sets.map(row => row.last?.value ?? null), [115, 125, null, null]);
  // This historical session missed prescribed sets/reps: the shared engine eases off.
  assert.deepEqual(items[0].suggestedLoads, ["102.5", "112.5", null, null]);
  view.unmount();
});

test("completed details show actuals without pre-workout guidance", () => {
  const today = record(deadlift, { actual: { sets: 4, reps: 8, load: { value: 75, unit: "kg" } } });
  const historical = record(deadlift, { plan_id: "older-plan",
    actual: { sets: 4, reps: 8, load: { value: 80, unit: "kg" }, feel: "easy" } });
  const view = mount(deadlift, { logs: { "blk-deadlift": today },
    recentPerformances: [historical], allowProgression: true });
  assert.doesNotMatch(view.text(), /Suggested|Last 80 kg/);
  const actual = view.container.querySelector('[aria-label="Today\'s actual"]')!;
  assert.match(actual.textContent ?? "", /75 kg/);
  assert.doesNotMatch(actual.textContent ?? "", /82.5 kg|Last80 kg/);
  assert.equal(view.saved.length, 0);
  assert.equal(today.actual.load?.value, 75);
  view.unmount();
});


test("review suppresses guidance even for exercises with unreported measurements", () => {
  const view = mount(deadlift, { open: false, hideWeightGuidance: true,
    recentLoads: [{ exercise_key: null, display_name: deadlift.display_name, load: { value: 80, unit: "kg" } } as ExerciseRecentLoad] });
  assert.doesNotMatch(view.text(), /Suggested|Same as last/);
  assert.equal(view.saved.length, 0);
  view.unmount();
});

test("collapsed prescription and guidance show a numeric weight only once", () => {
  const view = mount({ ...deadlift, load: { value: 95, unit: "kg" },
    prescription_text: "4 × 8 · 95 kg · rest 90s" }, { open: false, recentLoads: [] });
  assert.equal(view.text().split("95 kg").length - 1, 1);
  assert.doesNotMatch(view.text(), /Editable|4 sets × 8 reps/);
  view.unmount();
});
