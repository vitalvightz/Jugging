import { test } from "node:test";
import assert from "node:assert/strict";
import {
  connectionSharing,
  fighterSummary,
  weekStart,
  type Connection,
  type Fighter,
} from "./coach";
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
  share_weight: false,
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
    fighterSummary({ ...blank, share_injuries: true, injuries: [injury], sparring_reports: [report] })
      .attention,
    "1 open injury flag",
  );
  const contact = fighterSummary({ ...blank, share_injuries: true, sparring_reports: [report] });
  assert.equal(contact.attention, "Contact concern reported");
  assert.equal(contact.active, true);
  assert.equal(fighterSummary({ ...blank, injuries: [injury], sparring_reports: [report], recovery: [{ day: blank.today, source: "today", sleep: "poor" }] }).attention, null);
});

const accepted: Connection = {
  id: "c",
  coach_id: "coach",
  athlete_id: "fighter",
  invited_email: "fighter@example.com",
  coach_name: "Coach",
  status: "accepted",
  share_recovery: true,
  share_injuries: true,
  share_weight: true,
  health_consent: true,
};
test("coach access describes everything only when everything is shared", () => {
  assert.deepEqual(connectionSharing(accepted), {
    label: "Sharing training, weight, recovery and injuries",
    note: null,
    missing: [],
  });
});
test("a connection accepted before weight sharing does not claim weight", () => {
  const legacy = { ...accepted, share_weight: undefined };
  assert.deepEqual(connectionSharing(legacy), {
    label: "Sharing training, recovery and injuries",
    note: "Not sharing weight.",
    missing: ["weight"],
  });
  assert.equal(
    connectionSharing({ ...legacy, share_injuries: false }).label,
    "Sharing training and recovery",
  );
});
test("withdrawn health consent leaves only training shared", () => {
  const sharing = connectionSharing({ ...accepted, health_consent: false });
  assert.equal(sharing.label, "Sharing training");
  assert.equal(
    sharing.note,
    "Weight, recovery and injuries paused while health-data consent is off.",
  );
  assert.deepEqual(sharing.missing, []);
  assert.equal(
    connectionSharing({
      ...accepted,
      health_consent: false,
      share_weight: false,
      share_recovery: false,
      share_injuries: false,
    }).note,
    null,
  );
});

test("check-in dates group sources, retain distinct values and use recorded timestamps", async () => {
  const { groupCoachCheckIns, coachCheckInValues, coachRecoveryTone } = await import("./coach");
  const rows = groupCoachCheckIns([
    { day: "2026-11-15", source: "today", sleep: "good", body: "normal", recorded_at: "2026-11-15T13:00:00Z" },
    { day: "2026-11-15", source: "daily", readiness: 3, fatigue: 4, sleep_quality: 2, recorded_at: "2026-11-15T10:00:00Z" },
    { day: "2026-11-14", source: "today", sleep: "okay" },
    { day: "2026-11-15", source: "today", sleep: "good", body: "normal", recorded_at: "2026-11-15T12:00:00Z" },
  ]);
  assert.deepEqual(rows.map(row => row.day), ["2026-11-15", "2026-11-14"]);
  const values = coachCheckInValues(rows[0].readings);
  assert.equal(values.filter(value => value.label === "Sleep: Good").length, 1);
  assert.ok(values.some(value => value.label === "Readiness: 3/5"));
  assert.ok(values.some(value => value.label === "Fatigue: 4/5"));
  assert.equal(coachRecoveryTone("sleep_quality", 1), "concern");
  assert.equal(coachRecoveryTone("sleep_quality", 2), "concern");
  assert.equal(coachRecoveryTone("sleep_quality", 3), "caution");
  assert.equal(coachRecoveryTone("sleep_quality", 4), "good");
  assert.equal(coachRecoveryTone("sleep_quality", 5), "good");
  assert.equal(coachRecoveryTone("sleep_quality", undefined), "unknown");
  assert.equal(coachRecoveryTone("sleep", "poor"), "concern");
  assert.equal(coachRecoveryTone("injury", "mild"), "caution");
  assert.equal(coachRecoveryTone("injury", "severe"), "concern");
  assert.equal(coachRecoveryTone("body", "flat"), "caution");
  assert.equal(coachRecoveryTone("body", "normal"), "good");
  assert.equal(coachRecoveryTone("readiness", undefined), "unknown");
  assert.equal(coachRecoveryTone("readiness", 99), "unknown");
  assert.equal(coachRecoveryTone("body", "unknown"), "unknown");
});
