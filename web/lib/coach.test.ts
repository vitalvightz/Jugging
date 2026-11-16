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
  assert.equal(r.attention, null);
  assert.equal(r.active, false);
  assert.equal(r.weekSessions, 0);
  assert.equal(r.recovery, "Not shared");
});
test("weekly activity starts on Monday", () => {
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
    "Poor sleep reported",
  );
  assert.equal(
    fighterSummary({
      ...blank,
      share_recovery: true,
      recovery: [{ day: "2026-10-30", sleep: "poor", source: "today" }],
    }).attention,
    null,
  );
  assert.equal(
    fighterSummary({ ...blank, share_recovery: true, recovery: [] }).recovery,
    "No check-in",
  );
});
test("injury flags and heavy contact outrank recovery as the reason", () => {
  const injury = {
    id: "i",
    body_area: "knee",
    severity: "moderate",
    status: "open",
    updated_at: "2026-11-15T12:00:00Z",
  };
  const report = {
    id: "s",
    day: "2026-11-14",
    intensity: "hard",
    rounds: 3,
    round_seconds: null,
    head_contact: "heavy",
    rocked: false,
  };
  assert.equal(
    fighterSummary({ ...blank, injuries: [injury], sparring_reports: [report] })
      .attention,
    "1 open injury flag",
  );
  const contact = fighterSummary({ ...blank, sparring_reports: [report] });
  assert.equal(contact.attention, "Contact concern reported");
  assert.equal(contact.active, true);
});
