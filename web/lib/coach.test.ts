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

import { coachConcerns, coachCutStatus, concernsForMode, dayOffset } from "./coach";
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
  assert.deepEqual(concerns.map(c => c.reason), ["Cut too aggressive · 3 days to fight", "Rocked in sparring · 3 days ago", "Leg injury · Severe", "Poor sleep · 3 days ago", "Arm injury · Mild"]);
  assert.deepEqual(coachConcerns({ ...f, share_weight: false, share_injuries: false, share_recovery: false }, riskWeight), []);
});
test("neutral check-in values are never a concern", () => {
  const f: Fighter = { ...blank, share_recovery: true, recovery: [
    { day: blank.today, source: "today", sleep: "okay", body: "flat" },
    { day: "2026-11-13", source: "daily", readiness: 3, fatigue: 3, sleep_quality: 3 },
  ] };
  assert.deepEqual(coachConcerns(f, null), []);
  // Each concern-level value on its own is flagged, with a relative day.
  for (const [reading, reason] of [
    [{ sleep: "poor" }, "Poor sleep · yesterday"],
    [{ sleep_quality: 2 }, "Sleep 2/5 · yesterday"],
    [{ readiness: 1 }, "Readiness 1/5 · yesterday"],
    [{ fatigue: 4 }, "Fatigue 4/5 · yesterday"],
  ] as const) {
    assert.deepEqual(coachConcerns({ ...f, recovery: [{ day: "2026-11-14", source: "today", ...reading }] }, null).map(c => c.reason), [reason]);
  }
});
test("three low check-ins in a week add a trend without hiding the critical reading", () => {
  const f: Fighter = { ...blank, share_recovery: true, recovery: [
    { day: blank.today, source: "today", sleep: "okay", body: "flat" },
    { day: "2026-11-14", source: "today", sleep: "poor", body: "normal" },
    { day: "2026-11-13", source: "today", sleep: "good", body: "flat" },
    { day: "2026-11-12", source: "today", sleep: "good", body: "normal" },
  ] };
  assert.deepEqual(coachConcerns(f, null).map(c => [c.id, c.reason]), [["recovery:trend", "Low recovery · 3 of 4 check-ins"], ["recovery:2026-11-14", "Poor sleep · yesterday"]]);
});
test("no recent check-in matters only in the final weeks before a fight", () => {
  const f: Fighter = { ...blank, share_recovery: true, share_injuries: true, recovery: [] };
  assert.deepEqual(coachConcerns(f, null), []);
  assert.deepEqual(coachConcerns({ ...f, fight_date: "2026-12-20" }, null), []);
  assert.deepEqual(coachConcerns({ ...f, fight_date: "2026-12-01" }, null).map(c => [c.reason, c.tone]), [["No check-in this week", "unknown"]]);
});
test("hard intensity alone is not a contact warning and normal data hides attention", () => {
  const f: Fighter = { ...blank, share_recovery: true, share_injuries: true, recovery: [{ day: blank.today, sleep: "good", body: "normal", source: "today" }], sparring_reports: [{ id: "hard", day: blank.today, rounds: 3, intensity: "hard", round_seconds: 180, head_contact: "none", rocked: false }] };
  assert.deepEqual(coachConcerns(f, null), []);
});
const session = (id: string, day: string, status = "done") => ({ id, day, session_id: id, plan_id: "plan", source: "unlxck", type: "strength_power", status, recorded_at: day });
test("one skipped session is not a concern; two in a week are, for S&C only", () => {
  const f: Fighter = { ...blank, plan_id: "plan", sessions: [session("a", "2026-11-14", "skipped")] };
  assert.deepEqual(coachConcerns(f, null), []);
  const two = coachConcerns({ ...f, sessions: [...f.sessions, session("b", "2026-11-10", "skipped"), session("old", "2026-11-08", "skipped")] }, null);
  assert.deepEqual(two.map(c => [c.category, c.reason]), [["adherence", "2 planned sessions skipped"]]);
  assert.deepEqual(concernsForMode(two, "technical"), []);
  assert.equal(concernsForMode(two, "sc").length, 1);
  // Skips must belong to the active plan and, when the schedule is loaded, match it.
  const skips = [session("a", "2026-11-14", "skipped"), session("b", "2026-11-10", "skipped")];
  assert.deepEqual(coachConcerns({ ...f, plan_id: "other", sessions: skips }, null), []);
  assert.deepEqual(coachConcerns({ ...f, sessions: skips.map(s => ({ ...s, session_id: undefined })) }, null), []);
  assert.deepEqual(coachConcerns({ ...f, sessions: skips, schedule: [{ id: "a", day: "2026-11-14", type: "strength_power", title: "S" }] }, null), []);
  assert.equal(coachConcerns({ ...f, sessions: skips, schedule: [{ id: "a", day: "2026-11-14", type: "strength_power", title: "S" }, { id: "b", day: "2026-11-10", type: "strength_power", title: "S" }] }, null).length, 1);
});
test("a training frequency spike needs four weeks of history and a clear jump", () => {
  const usual = (n: number) => [1, 2, 3, 4].flatMap(w => Array.from({ length: n }, (_, i) => session(`w${w}-${i}`, dayOffset("2026-11-09", -7 * w + i))));
  const thisWeek = (n: number) => Array.from({ length: n }, (_, i) => session(`now-${i}`, dayOffset("2026-11-09", i)));
  assert.deepEqual(coachConcerns({ ...blank, sessions: [...usual(3), ...thisWeek(6)] }, null).map(c => c.reason), ["Training frequency spike · 6 sessions, usually 3"]);
  assert.deepEqual(coachConcerns({ ...blank, sessions: [...usual(3), ...thisWeek(4)] }, null), []);
  assert.deepEqual(coachConcerns({ ...blank, sessions: thisWeek(6) }, null), []);
  assert.deepEqual(coachConcerns({ ...blank, sessions: [...usual(1), ...thisWeek(4)] }, null), []);
});
test("each view leads with its own concerns", () => {
  const f: Fighter = { ...blank, share_weight: true, share_injuries: true,
    sparring_reports: [{ id: "contact", day: blank.today, rounds: 3, intensity: "light", round_seconds: 180, head_contact: "heavy", rocked: false }] };
  const all = coachConcerns(f, riskWeight);
  assert.deepEqual(concernsForMode(all, "sc").map(c => c.category), ["weight", "contact"]);
  assert.deepEqual(concernsForMode(all, "technical").map(c => c.category), ["contact", "weight"]);
  assert.deepEqual(coachConcerns(f, riskWeight).find(c => c.category === "contact")?.reason, "Heavy head contact · today");
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

test("weight: a failed read is not a fighter concern, and unknown cut safety only counts near a fight", () => {
  const f: Fighter = { ...blank, age: 25, share_weight: true };
  assert.deepEqual(coachConcerns(f, null), []);
  assert.deepEqual(coachConcerns({ ...f, age: 17 }, riskWeight), []);
  const stale = { ...riskWeight, current_weight_recorded_at: "2026-10-01", cut_assessment: { ...riskWeight.cut_assessment!, weight_cut_status: "on_track" as const, days_until_fight: 40 } };
  assert.deepEqual(coachConcerns(f, stale), []);
  const near = { ...stale, cut_assessment: { ...stale.cut_assessment, days_until_fight: 12 } };
  assert.deepEqual(coachConcerns(f, near).map(c => [c.reason, c.tone]), [["Weight needs updating", "unknown"]]);
});

test("an invite share names the invited email and links to the accept screen", () => {
  const share = coachInviteShare({ coach_name: "Sam Okafor", invited_email: "ari@example.com" }, "https://app.example/");
  assert.equal(share.url, "https://app.example/settings/coach-access");
  assert.match(share.text, /^Sam Okafor invited you/);
  assert.match(share.text, /Sign in with ari@example\.com and tap Accept: https:\/\/app\.example\/settings\/coach-access$/);
  assert.equal(share.whatsapp, `https://wa.me/?text=${encodeURIComponent(share.text)}`);
  assert.match(coachInviteShare({ coach_name: " ", invited_email: "a@b.co" }, "https://x.y").text, /^Your coach invited you/);
});
test("a flat day or okay sleep alone does not flag the fighter summary", () => {
  const f = { ...blank, share_recovery: true };
  assert.equal(fighterSummary({ ...f, recovery: [{ day: blank.today, sleep: "okay", body: "flat", source: "today" }] }).attention, null);
  assert.equal(fighterSummary({ ...f, recovery: [{ day: blank.today, sleep: "poor", body: "flat", source: "today" }] }).attention, "Poor sleep and fatigue reported");
});
test("a severe injury stays a concern while monitoring", () => {
  const f: Fighter = { ...blank, share_injuries: true, injuries: [{ id: "k", body_area: "knee", severity: "severe", status: "monitoring", updated_at: blank.today }] };
  assert.deepEqual(coachConcerns(f, null).map(c => [c.tone, c.reason]), [["concern", "Knee injury · Severe · Monitoring"]]);
});
