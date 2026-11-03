import assert from "node:assert/strict";
import test from "node:test";

import {
  formatWeightDelta,
  getBodyweightEntriesWithIndex,
  getBodyweightTrend,
  getCampWeight,
  getTargetGap,
  getTrendChange,
  shiftDate,
} from "./nutrition-bodyweight.ts";

const entries = [
  { date: "2026-04-01", time: "07:10", weight_kg: 74.6, is_fasted: true, notes: "" },
  { date: "2026-04-08", time: "07:05", weight_kg: 73.4, is_fasted: true, notes: "" },
  { date: "2026-04-03", time: "07:15", weight_kg: 74.1, is_fasted: true, notes: "" },
  { date: "2026-04-06", time: "07:20", weight_kg: 73.8, is_fasted: false, notes: "" },
].map((entry) => ({ ...entry }));

test("lists weigh-ins newest first and keeps each saved-log index", () => {
  assert.deepStrictEqual(
    getBodyweightEntriesWithIndex(entries).map((entry) => [entry.date, entry.sourceIndex]),
    [["2026-04-08", 1], ["2026-04-06", 3], ["2026-04-03", 2], ["2026-04-01", 0]],
  );
});

test("trend is oldest first and ranges count back from the latest weigh-in", () => {
  assert.deepStrictEqual(getBodyweightTrend(entries, "7D").map((entry) => entry.date), ["2026-04-03", "2026-04-06", "2026-04-08"]);
  assert.deepStrictEqual(getBodyweightTrend(entries, "30D").map((entry) => entry.date), ["2026-04-01", "2026-04-03", "2026-04-06", "2026-04-08"]);
  assert.deepStrictEqual(getBodyweightTrend([], "All"), []);
});

test("ranges are whole calendar days, whatever the time of day", () => {
  // 7 days ending Wed 08 Apr starts Thu 02 Apr, even before the latest weigh-in's time.
  const early = [...entries, { date: "2026-04-02", time: "06:00", weight_kg: 74.3, is_fasted: true, notes: "" }];
  assert.deepStrictEqual(getBodyweightTrend(early, "7D").map((entry) => entry.date), ["2026-04-02", "2026-04-03", "2026-04-06", "2026-04-08"]);
  assert.equal(shiftDate("2026-03-01", -1), "2026-02-28");
  assert.equal(shiftDate("2026-10-25", 1), "2026-10-26");
});

test("trend skips entries that cannot be plotted", () => {
  const broken = [...entries, { date: "", time: null, weight_kg: 80, is_fasted: null, notes: null }];
  assert.equal(getBodyweightTrend(broken, "All").length, 4);
});

test("trend change runs from the first to the last weigh-in in range", () => {
  assert.equal(getTrendChange(getBodyweightTrend(entries, "All")), -1.2);
  assert.equal(getTrendChange(getBodyweightTrend(entries, "7D")), -0.7);
  assert.equal(getTrendChange(entries.slice(0, 1)), null);
});

test("formats weight changes with a sign", () => {
  assert.equal(formatWeightDelta(-0.4), "−0.4 kg");
  assert.equal(formatWeightDelta(0.3), "+0.3 kg");
  assert.equal(formatWeightDelta(0), "Even");
  assert.equal(formatWeightDelta(null), "--");
});

test("calculates target gap", () => {
  assert.equal(getTargetGap(73.4, 70), 3.4);
  assert.equal(getTargetGap(null, 70), null);
});

test("camp weight is the workspace's current weight, not the latest weigh-in", () => {
  const workspace = {
    shared_camp_context: { current_weight_kg: 70.94 },
    nutrition_monitoring: { daily_bodyweight_log: entries },
  } as unknown as Parameters<typeof getCampWeight>[0];
  assert.equal(getCampWeight(workspace), 70.9);
});
