import { test } from "node:test";
import assert from "node:assert/strict";
import { fighterSummary, weekStart, type Fighter } from "./coach";
const blank: Fighter = {
  id: "fighter",
  connection_id: "c",
  name: "Fighter",
  avatar_url: null,
  sport: null,
  today: "2026-11-15",
  timezone: "UTC",
  fight_date: null,
  plan_id: null,
  plan_name: null,
  share_recovery: false,
  share_injuries: false,
  schedule: [],
  sessions: [],
  recovery: null,
  injuries: null,
};
test("missing logs do not classify an athlete as needing attention", () => {
  const r = fighterSummary(blank);
  assert.equal(r.attention, false);
  assert.equal(r.active, false);
  assert.equal(r.weekSessions, 0);
  assert.equal(r.recovery, "Not shared");
});
test("seven-day activity and Monday week boundary", () => {
  assert.equal(weekStart("2026-11-15"), "2026-11-09");
  const row = {
    id: "x",
    source: "external",
    type: "strength",
    status: "done",
    recorded_at: "2026-11-09T12:00:00Z",
  };
  assert.equal(
    fighterSummary({ ...blank, sessions: [{ ...row, day: "2026-11-09" }] })
      .active,
    true,
  );
  assert.equal(
    fighterSummary({ ...blank, sessions: [{ ...row, day: "2026-11-08" }] })
      .active,
    false,
  );
  assert.equal(
    fighterSummary({
      ...blank,
      sessions: [{ ...row, day: "2026-11-09", status: "skipped" }],
    }).weekSessions,
    0,
  );
});
test("recovery concern must be recent and explicit", () => {
  assert.equal(
    fighterSummary({
      ...blank,
      share_recovery: true,
      recovery: [{ day: "2026-11-15", sleep: "poor", source: "today" }],
    }).attention,
    true,
  );
  assert.equal(
    fighterSummary({
      ...blank,
      share_recovery: true,
      recovery: [{ day: "2026-10-30", sleep: "poor", source: "today" }],
    }).attention,
    false,
  );
  assert.equal(
    fighterSummary({ ...blank, share_recovery: true, recovery: [] }).recovery,
    "No check-in",
  );
});
