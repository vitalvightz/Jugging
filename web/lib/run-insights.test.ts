import test from "node:test";
import assert from "node:assert/strict";

import { describeDistanceDelta, describePaceDelta, formatSpeed, paceSecondsPerKm, runInsights } from "./run-insights.ts";
import type { TrainingHistorySession } from "./training-history.ts";

function run(id: string, day: string, km: number | null, seconds: number | null, extra: Partial<TrainingHistorySession> = {}): TrainingHistorySession {
  return {
    id, source: "external", activity: "roadwork", training_day: day, status: "done", title: "Roadwork", plan_id: null, effort: "moderate",
    distance_value: km, distance_unit: km === null ? null : "km", duration_seconds: seconds,
    duration_minutes: seconds ? Math.ceil(seconds / 60) : 30, ...extra,
  };
}

test("pace uses an exact time only and converts miles", () => {
  assert.equal(paceSecondsPerKm(run("a", "2026-11-01", 5, 1500)), 300);
  assert.equal(paceSecondsPerKm(run("a", "2026-11-01", 5, null)), null);
  assert.equal(Math.round(paceSecondsPerKm({ distance_value: 1, distance_unit: "miles", duration_seconds: 480 })!), 298);
  assert.equal(formatSpeed(run("a", "2026-11-01", 10, 3000)), "12.0 km/h");
});

test("compares pace and distance with the earlier runs only", () => {
  const rows = [
    run("a", "2026-10-20", 5, 1500), // 5:00/km
    run("b", "2026-10-22", 5, 1600), // 5:20/km
    run("c", "2026-10-25", 6, 1680), // 4:40/km, this run
    run("d", "2026-10-28", 20, 3000), // later: ignored
  ];
  const insights = runInsights(rows[2], rows);
  assert.equal(Math.round(insights.paceDelta!.seconds), -30);
  assert.equal(insights.paceDelta!.runs, 2);
  assert.equal(insights.distanceDelta!.value, 1);
  assert.deepEqual(insights.records, ["Longest run", "Fastest pace"]);
  assert.deepEqual(insights.trend.map(point => [point.id, point.current]), [["a", false], ["b", false], ["c", true]]);
  assert.equal(describePaceDelta(insights.paceDelta!, "km"), "0:30/km faster than your average over 2 runs");
  assert.equal(describeDistanceDelta(insights.distanceDelta!, "km"), "1 km further than your average over 2 runs");
});

test("a standard distance record needs an earlier run at that distance", () => {
  const first = run("a", "2026-10-20", 5, 1500);
  assert.deepEqual(runInsights(first, [first]).records, []);
  const faster = run("b", "2026-10-27", 5.05, 1450);
  assert.ok(runInsights(faster, [first, faster]).records.includes("Fastest 5k"));
  const slower = run("c", "2026-10-29", 4.98, 1550);
  assert.ok(!runInsights(slower, [first, faster, slower]).records.includes("Fastest 5k"));
});

test("sums the run's Monday-to-Sunday week and ignores other activities", () => {
  const rows = [
    run("sun", "2026-11-01", 4, 1200),
    run("mon", "2026-11-02", 5, 1500),
    run("wed", "2026-11-04", 3, null, { duration_minutes: 20 }),
    { ...run("box", "2026-11-03", null, null), activity: "boxing" as const },
  ];
  const insights = runInsights(rows[1], rows);
  assert.deepEqual(insights.week, { distance: 8, runs: 2, timed: 1500 + 1200 });
  assert.equal(describePaceDelta(insights.paceDelta!, "km"), "Same pace as your previous run");
});

test("a run with no detail still gets a week but no comparisons", () => {
  const bare = run("a", "2026-11-02", null, null);
  const insights = runInsights(bare, [run("b", "2026-11-01", 5, 1500), bare]);
  assert.equal(insights.paceDelta, null);
  assert.equal(insights.distanceDelta, null);
  assert.deepEqual(insights.records, []);
  assert.deepEqual(insights.trend.map(point => point.id), ["b"]);
});
