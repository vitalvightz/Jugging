import test from "node:test";
import assert from "node:assert/strict";

import { filterTakeaways, formatTrainingDuration, summarizeTraining, summaryWindowLoaded } from "./training-summary.ts";
import type { TrainingHistorySession } from "@/lib/training-history";

const TODAY = "2026-10-19";

function row(overrides: Partial<TrainingHistorySession>): TrainingHistorySession {
  return {
    id: Math.random().toString(36), source: "external", training_day: TODAY, status: "done", title: "Boxing",
    activity: "boxing", duration_minutes: 60, effort: "hard", plan_id: null, ...overrides,
  };
}

test("the summary counts done and modified training in the window, by discipline", () => {
  const summary = summarizeTraining([
    row({ activity: "boxing", duration_minutes: 90 }),
    row({ activity: "boxing", duration_minutes: 150, training_day: "2026-10-13" }),
    row({ activity: "sparring", duration_minutes: 135 }),
    row({ activity: "strength", duration_minutes: 60 }),
    row({ activity: "conditioning", duration_minutes: 120, status: "modified" }),
    // Outside the 7-day window, skipped, or an unverified legacy completion.
    row({ activity: "boxing", training_day: "2026-10-12" }),
    row({ source: "unlxck", status: "skipped", completion_origin: "reported" }),
    row({ source: "unlxck", completion_origin: null, activity: null }),
  ], TODAY, 7);
  assert.equal(summary.sessions, 5);
  assert.equal(summary.minutes, 555);
  assert.equal(formatTrainingDuration(summary.minutes), "9h 15m");
  assert.deepEqual(summary.mix.map(item => [item.label, item.minutes]), [["Boxing", 240], ["S&C", 180], ["Sparring", 135]]);
});

test("plan sessions keep their own bucket and missing durations are never estimated", () => {
  const summary = summarizeTraining([
    row({ source: "unlxck", completion_origin: "reported", activity: null, duration_minutes: null, title: "Strength A" }),
    row({ activity: "roadwork", duration_minutes: 30 }),
  ], TODAY, 28);
  assert.equal(summary.sessions, 2);
  assert.equal(summary.minutes, 30);
  assert.equal(summary.untimed, 1);
  assert.deepEqual(summary.mix.map(item => item.label), ["Running", "Unlxck plan"]);
});

test("a recorded sport decides the bucket; older records keep theirs", () => {
  const summary = summarizeTraining([
    row({ sport: "bjj", session_type: "rolling", activity: "other", duration_minutes: 90 }),
    row({ sport: "muay_thai", session_type: "clinch", activity: "other", duration_minutes: 60 }),
    row({ sport: "mma", session_type: "striking", activity: "other", duration_minutes: 60 }),
    row({ sport: "mma", session_type: "wrestling", activity: "other", duration_minutes: 45 }),
    row({ sport: "kickboxing", session_type: "sparring", activity: "sparring", duration_minutes: 30 }),
    row({ sport: "boxing", session_type: "pads", activity: "boxing", duration_minutes: 60 }),
    row({ activity: "boxing", duration_minutes: 30 }),
    row({ activity: "strength", duration_minutes: 40 }),
    row({ activity: "conditioning", duration_minutes: 20 }),
    row({ activity: "sparring", duration_minutes: 50 }),
  ], TODAY, 7);
  assert.equal(summary.sessions, 10);
  assert.equal(summary.minutes, 485);
  assert.deepEqual(summary.mix.map(item => [item.label, item.sessions, item.minutes]), [
    ["MMA", 2, 105], ["Boxing", 2, 90], ["BJJ", 1, 90], ["S&C", 2, 60], ["Muay Thai", 1, 60], ["Sparring", 1, 50], ["Kickboxing", 1, 30],
  ]);
  // Every session sits in exactly one bucket.
  assert.equal(summary.mix.reduce((total, item) => total + item.minutes, 0), summary.minutes);
});

test("the four-week window reaches 28 training days back", () => {
  const rows = [row({ training_day: "2026-09-22" }), row({ training_day: "2026-09-21" })];
  assert.equal(summarizeTraining(rows, TODAY, 28).sessions, 1);
  assert.equal(summarizeTraining([], TODAY, 7).sessions, 0);
});

test("durations format as hours and minutes", () => {
  assert.equal(formatTrainingDuration(0), "0m");
  assert.equal(formatTrainingDuration(45), "45m");
  assert.equal(formatTrainingDuration(120), "2h");
});

test("takeaways are found by their text or session title", () => {
  const rows = [
    row({ id: "a", takeaway: "Right hand up after the jab" }),
    row({ id: "b", title: "Roadwork", takeaway: "Hold pace on the hill" }),
    row({ id: "c", takeaway: "   " }),
    row({ id: "d" }),
  ];
  assert.deepEqual(filterTakeaways(rows, "").map(item => item.id), ["a", "b"]);
  assert.deepEqual(filterTakeaways(rows, "JAB").map(item => item.id), ["a"]);
  assert.deepEqual(filterTakeaways(rows, "roadwork").map(item => item.id), ["b"]);
});

test("a window counts as loaded only when every day in it is fully loaded", () => {
  assert.equal(summaryWindowLoaded(null, TODAY, 28), true);
  // Pages hold whole days: everything on or after the cursor day is loaded.
  assert.equal(summaryWindowLoaded("2026-10-13", TODAY, 7), true);
  assert.equal(summaryWindowLoaded("2026-10-14", TODAY, 7), false);
  assert.equal(summaryWindowLoaded("2026-09-22", TODAY, 28), true);
  assert.equal(summaryWindowLoaded("2026-09-23", TODAY, 28), false);
});
