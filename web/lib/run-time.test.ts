import test from "node:test";
import assert from "node:assert/strict";

import { formatPace, formatRunTime, parseRunTime, runSummary } from "./run-time.ts";

test("parses mm:ss and h:mm:ss run times", () => {
  assert.equal(parseRunTime("24:35"), 1475);
  assert.equal(parseRunTime(" 1:02:10 "), 3730);
  assert.equal(parseRunTime("75:00"), 4500);
});

test("rejects malformed or out-of-range run times", () => {
  for (const text of ["", "24", "24:60", "1:60:00", "a:10", "0:00", "10:00:01", "1:2:3:4"]) assert.equal(parseRunTime(text), null, text);
});

test("formats times and pace", () => {
  assert.equal(formatRunTime(1475), "24:35");
  assert.equal(formatRunTime(3730), "1:02:10");
  assert.equal(formatPace(1475, 5, "km"), "4:55/km");
  assert.equal(formatPace(1475, 0, "km"), null);
});

test("summarises a run row", () => {
  assert.equal(runSummary({ distance_value: 5, distance_unit: "km", duration_seconds: 1475, duration_minutes: 25 }), "5 km · 24:35 · 4:55/km");
  assert.equal(runSummary({ distance_value: 3.1, distance_unit: "miles", duration_seconds: null, duration_minutes: 30 }), "3.1 mi · 30 min");
  assert.equal(runSummary({ duration_minutes: 30 }), null);
});
