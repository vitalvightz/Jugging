import test from "node:test";
import assert from "node:assert/strict";
import "../test-dom";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { SessionReview } from "./session-review";
import type { StructuredSession } from "@/lib/types";

test("review locks the entire save, including the batch write, and releases after failure", async () => {
  const root = createRoot(document.createElement("div"));
  const sessions: StructuredSession[] = [{ session_id: "s1", title: "Strength", blocks: [
    { block_id: "b1", block_type: "strength", display_name: "Deadlift", sets: 3, reps: 5 },
  ] }];
  let writes = 0;
  let completions = 0;
  let rejectWrite: (error: Error) => void = () => {};
  try {
    await act(async () => { root.render(<SessionReview title="Review" sessions={sessions}
      logging={{logs: {}, painReasonAllowed: false, save: async () => {}, saveMany: () => {
        writes++;
        return new Promise<void>((_, reject) => { rejectWrite = reject; });
      }}} blocks={<p>Deadlift</p>} hint="Review" canCollectPain={false} isSubmitting={false}
      onClose={() => { throw new Error("Cannot close during a write"); }}
      onSave={async () => { completions++; return true; }} />); });
    const save = Array.from(document.querySelectorAll("button")).find(button => button.textContent === "Save as skipped")!;
    assert.ok(save);
    await act(async () => { save.click(); save.click(); });
    assert.equal(writes, 1);
    assert.equal(save.disabled, true);
    assert.equal(document.querySelector<HTMLButtonElement>('[aria-label="Back to Today"]')?.disabled, true);
    await act(async () => { rejectWrite(new Error("Network failed")); });
    assert.equal(completions, 0);
    assert.equal(save.disabled, false);
    assert.match(document.body.textContent ?? "", /Network failed/);
  } finally {
    await act(async () => { root.unmount(); });
  }
});

test("historical weight flows through rendered timer confirmation, Session Review and next workout", async () => {
  const { ExerciseLogProvider } = await import("../exercise-log");
  const { ExerciseRow } = await import("../structured-plan-renderer");
  const { RestSetLogger } = await import("../session-timer/rest-set-logger");
  const { withHistoricalTimerLoads } = await import("@/lib/session-timer/history");
  const { buildTimerItems } = await import("@/lib/session-timer/plan");
  const { createTimerState, endSession } = await import("@/lib/session-timer/engine");
  const { timerExerciseLogs } = await import("@/lib/session-timer/logs");
  const block = { block_id: "squat", exercise_key: "back-squat", block_type: "strength",
    display_name: "Back Squat", sets: 1, reps: 8, rep_range: [8, 12], effort: { method: "RPE", value: 7 } };
  const previous = { plan_id: "older", exercise_key: "back-squat", prescribed: block,
    status: "as_prescribed", actual: { load: { value: 115, unit: "kg" }, reps: 8, feel: "easy" } } as unknown as import("@/lib/types").ExerciseLogRecord;
  const items = withHistoricalTimerLoads(buildTimerItems([{ blocks: [block] }]), [block], [], [previous], { allowProgression: true });
  let state = { ...createTimerState(items), completed: [1] };
  const root = createRoot(document.createElement("div"));
  const node = document.createElement("div");
  document.body.appendChild(node);
  // The logger uses the same state updates as the running timer; confirmation
  // is deliberately clicked before its actuals are handed to Session Review.
  const loggerRoot = createRoot(node);
  const renderLogger = () => loggerRoot.render(<RestSetLogger target={{ itemId: items[0].id, set: 1 }}
    timer={{ state, update: (change: (value: typeof state) => typeof state) => {
      state = change(state); renderLogger();
    } } as unknown as import("../session-timer/use-session-timer").SessionTimerController} />);
  try {
    await act(async () => renderLogger());
    assert.equal(node.querySelector<HTMLInputElement>('[aria-label="Load kg, set 1"]')?.value, "115");
    assert.match(node.textContent ?? "", /115 kg · Try 9 reps/);
    assert.equal(state.confirmedSetLogs?.[items[0].id], undefined);
    await act(async () => Array.from(node.querySelectorAll("button")).find(b => b.textContent?.includes("Confirm set"))!.click());
    const entry = timerExerciseLogs(endSession(state, 0))[0];
    const saved = { ...previous, ...entry, prescribed: block, actual: entry.actual! };
    assert.equal(saved.actual.set_entries?.[0].load?.value, 115);
    let completed = false;
    await act(async () => root.render(<ExerciseLogProvider logging={{ logs: { squat: saved }, recentLoads: [],
      painReasonAllowed: false, save: async () => {} }}>
      <SessionReview title="Strength" sessions={[{ blocks: [block] }]} logging={{ logs: { squat: saved }, painReasonAllowed: false, save: async () => {} }}
        blocks={<ExerciseRow block={block} open={false} onToggle={() => {}} />} hint="Timer results"
        canCollectPain={false} isSubmitting={false} onClose={() => {}}
        onSave={async () => { completed = true; return true; }} />
    </ExerciseLogProvider>));
    assert.match(document.querySelector(".today-review-root")?.textContent ?? "", /115 kg/);
    const save = Array.from(document.querySelectorAll<HTMLButtonElement>(".today-review-root button"))
      .find(button => /Save session/.test(button.textContent ?? ""))!;
    assert.ok(save);
    await act(async () => save.click());
    assert.equal(completed, true);
    await act(async () => root.render(<ExerciseLogProvider logging={{ canWrite: false, logs: {}, recentLoads: [],
      recentPerformances: [saved], painReasonAllowed: false, allowProgression: true, save: async () => {} }}>
      <ExerciseRow block={block} open={false} onToggle={() => {}} />
    </ExerciseLogProvider>));
    const next = withHistoricalTimerLoads(buildTimerItems([{ blocks: [block] }]), [block], [], [saved]);
    assert.ok(next[0].kind === "sets");
    assert.deepEqual(next[0].suggestedLoads, ["115"]);
  } finally {
    await act(async () => { root.unmount(); loggerRoot.unmount(); });
    node.remove();
  }
});
