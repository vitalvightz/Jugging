import { test } from "node:test";
import assert from "node:assert/strict";
import {
  coachInviteShare,
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

test("conflicting readings on one date retain their order and are clearly labelled", async () => {
  const { groupCoachCheckIns, coachCheckInValues } = await import("./coach");
  const [day] = groupCoachCheckIns([
    { day: "2026-11-03", source: "today", sleep: "good", recorded_at: "2026-11-03T10:00:00Z" },
    { day: "2026-11-03", source: "daily", sleep: "okay", body: "normal", recorded_at: "2026-11-03T12:00:00Z" },
  ]);
  assert.deepEqual(coachCheckInValues(day.readings).map(value => value.label), ["Sleep: Okay", "Body: Normal", "Earlier sleep: Good"]);
});

import { coachConcerns, coachCutStatus } from "./coach";
import type { CoachFighterWeight } from "./api-schema.generated";
const riskWeight: CoachFighterWeight = { shared: true, entries: [], current_weight_kg: 80, target_weight_kg: 70, current_weight_recorded_at: "2026-11-15", rolling_7_day_average_weight: null, weekly_pace_kg: null,
  cut_assessment: { weight_cut_status: "too_aggressive", weight_cut_risk_band: "severe", kg_to_target: 10, weight_cut_behind_kg: null, days_until_fight: 3 } };
test("severe canonical cut assessment stays red even with old inputs and near-target progress", () => {
  const result = coachCutStatus({ ...riskWeight, current_weight_kg: 70.1, current_weight_recorded_at: "2026-09-01" }, blank.today);
  assert.equal(result?.tone, "red"); assert.equal(result?.label, "Cut too aggressive");
});
test("missing and old weight data cannot say on track", () => {
  const weight = { ...riskWeight, cut_assessment: { ...riskWeight.cut_assessment!, weight_cut_status: "on_track" as const } };
  assert.equal(coachCutStatus({ ...weight, current_weight_recorded_at: null }, blank.today)?.label, "Unknown");
  assert.equal(coachCutStatus({ ...weight, current_weight_recorded_at: "2026-10-01" }, blank.today)?.label, "Unknown");
  assert.equal(coachCutStatus({ ...weight, shared: false }, blank.today), null);
  assert.equal(coachCutStatus({ ...weight, cut_assessment: null }, blank.today), null);
});
test("concerns sort severity first then weight, contact, injury and recovery", () => {
  const f: Fighter = { ...blank, share_weight: true, share_injuries: true, share_recovery: true,
    injuries: [{ id: "mild", body_area: "arm", severity: "mild", status: "open", updated_at: blank.today }, { id: "severe", body_area: "leg", severity: "severe", status: "open", updated_at: blank.today }],
    sparring_reports: [{ id: "rocked", day: "2026-11-12", rounds: 3, intensity: "technical", round_seconds: 180, head_contact: "light", rocked: true }],
    recovery: [{ day: blank.today, source: "today", sleep: "good", body: "normal" }, { day: "2026-11-12", source: "today", sleep: "poor" }] };
  const concerns = coachConcerns(f, riskWeight);
  assert.deepEqual(concerns.map(c => [c.category, c.tone]), [["weight", "concern"], ["contact", "concern"], ["injury", "concern"], ["recovery", "concern"], ["injury", "caution"]]);
  assert.ok(concerns.every(c => c.reason && c.section));
  assert.deepEqual(coachConcerns({ ...f, share_weight: false, share_injuries: false, share_recovery: false }, riskWeight), []);
});
test("hard intensity alone is not a contact warning and normal data hides attention", () => {
  const f: Fighter = { ...blank, share_recovery: true, share_injuries: true, recovery: [{ day: blank.today, sleep: "good", body: "normal", source: "today" }], sparring_reports: [{ id: "hard", day: blank.today, rounds: 3, intensity: "hard", round_seconds: 180, head_contact: "none", rocked: false }] };
  assert.deepEqual(coachConcerns(f, null), []);
  assert.equal(coachConcerns({ ...f, recovery: [] }, null)[0].tone, "unknown");
});
test("severity outranks category and adherence requires an explicitly skipped planned session", () => {
  const f: Fighter = { ...blank, share_weight: true, share_injuries: true,
    schedule: [{ id: "scheduled", day: "2026-11-14", type: "strength_power", title: "Strength" }],
    sessions: [{ id: "skipped", day: "2026-11-14", session_id: "scheduled", plan_id: "plan", source: "unlxck", type: "strength_power", status: "skipped", recorded_at: "2026-11-14" }],
    sparring_reports: [{ id: "contact", day: blank.today, rounds: 3, intensity: "light", round_seconds: 180, head_contact: "heavy", rocked: false }],
  };
  const amber = { ...riskWeight, cut_assessment: { ...riskWeight.cut_assessment!, weight_cut_status: "behind" as const } };
  assert.deepEqual(coachConcerns(f, amber).map(c => c.category), ["contact", "weight", "adherence"]);
  assert.ok(!coachConcerns({ ...f, sessions: [] }, amber).some(c => c.category === "adherence"));
});

test("current contact alerts use the existing seven-day window without hiding a recent warning behind a normal report", () => {
  const report = { id: "recent", day: "2026-11-09", rounds: 3, intensity: "technical", round_seconds: 180, head_contact: "heavy", rocked: true };
  const f: Fighter = { ...blank, share_injuries: true, sparring_reports: [
    { ...report, id: "normal", day: blank.today, head_contact: "none", rocked: false },
    report,
    { ...report, id: "old", day: "2026-11-08" },
    { ...report, id: "future", day: "2026-11-16" },
  ] };
  assert.deepEqual(coachConcerns(f, null).map(concern => concern.id), ["contact:recent"]);
  assert.equal(f.sparring_reports?.length, 4); // Keep all reports available to History.
});

test("failed authorised weight reads produce a grey linked status, but loading and unshared/minor data do not", () => {
  const f: Fighter = { ...blank, age: 25, share_weight: true };
  assert.deepEqual(coachConcerns(f, null, true), [{ id: "weight:unavailable", category: "weight", tone: "unknown", reason: "Weight status unavailable", section: "fight-weight" }]);
  assert.deepEqual(coachConcerns(f, null), []);
  assert.deepEqual(coachConcerns({ ...f, share_weight: false }, null, true), []);
  assert.deepEqual(coachConcerns({ ...f, age: 17 }, null, true), []);
  assert.deepEqual(coachConcerns({ ...f, age: 17 }, riskWeight), []);
  assert.equal(coachConcerns({ ...f, age: 18 }, null, true)[0]?.tone, "unknown");
  assert.equal(coachConcerns({ ...f, age: undefined }, null, true)[0]?.reason, "Weight status unavailable");
});

test("an invite share names the invited email and links to the accept screen", () => {
  const share = coachInviteShare({ coach_name: "Sam Okafor", invited_email: "ari@example.com" }, "https://app.example/");
  assert.equal(share.url, "https://app.example/settings/coach-access");
  assert.match(share.text, /^Sam Okafor invited you/);
  assert.match(share.text, /Sign in with ari@example\.com and tap Accept: https:\/\/app\.example\/settings\/coach-access$/);
  assert.equal(share.whatsapp, `https://wa.me/?text=${encodeURIComponent(share.text)}`);
  assert.match(coachInviteShare({ coach_name: " ", invited_email: "a@b.co" }, "https://x.y").text, /^Your coach invited you/);
});
