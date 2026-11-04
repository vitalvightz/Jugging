import test from "node:test";
import assert from "node:assert/strict";

import {
  XP_ACTIONS,
  XP_LEVELS,
  parseXpAwardResponse,
  resolveXpLevel,
} from "./xp";

const apiAward = (overrides: Record<string, unknown> = {}) => ({
  id: "11111111-1111-1111-1111-111111111111",
  action: "readiness_checkin_completed",
  amount: 10,
  awarded_at: "2026-08-03T12:00:00+00:00",
  ...overrides,
});

test("XP ledger values cover every backend action", () => {
  assert.deepEqual(
    Object.fromEntries(Object.entries(XP_ACTIONS).map(([action, config]) => [action, config.xp])),
    {
      daily_login: 10,
      training_logged: 25,
      planned_session_completed: 50,
      recommended_fighter_content_watched: 10,
      full_training_week_completed: 100,
      profile_completed: 25,
      first_intake_completed: 50,
      first_plan_ready: 100,
      first_checkin_completed: 25,
      readiness_checkin_completed: 10,
      injury_update_completed: 10,
      stop_decision_followed: 15,
      feedback_submitted: 1,
      feedback_with_comment: 3,
      first_plan_completed: 250,
      phase_completed: 200,
    camp_completed: 500,
    manual_training_logged: 25,
    first_session_logged: 50,
    first_exercise_logged: 25,
    training_days_10: 100,
    training_days_25: 150,
    training_days_50: 250,
    training_days_100: 500,
    exercise_contribution_approved: 100,
    },
  );
});

test("XP history labels are short, accurate and free of em dashes", () => {
  assert.equal(XP_ACTIONS.readiness_checkin_completed.label, "Check-in complete");
  assert.equal(XP_ACTIONS.injury_update_completed.label, "Injury update saved");
  assert.equal(XP_ACTIONS.planned_session_completed.label, "Planned session complete");
  assert.equal(XP_ACTIONS.full_training_week_completed.label, "Training week complete");
  assert.equal(XP_ACTIONS.phase_completed.label, "Training phase complete");
  assert.equal(XP_ACTIONS.camp_completed.label, "Fight camp complete");
  assert.equal(XP_ACTIONS.training_days_10.label, "10 training days down");
  for (const { label } of Object.values(XP_ACTIONS)) assert.doesNotMatch(label, /—/);
});

test("shared level contract uses the long-term progression curve", () => {
  assert.deepEqual(XP_LEVELS, [
    { level: 1, title: "Rookie", threshold: 0 },
    { level: 2, title: "Prospect", threshold: 250 },
    { level: 3, title: "Amateur", threshold: 750 },
    { level: 4, title: "Challenger", threshold: 1_500 },
    { level: 5, title: "Ranked", threshold: 2_750 },
    { level: 6, title: "Contender", threshold: 4_500 },
    { level: 7, title: "Elite", threshold: 7_000 },
    { level: 8, title: "Champion", threshold: 10_000 },
  ]);
});

test("level resolution is correct immediately below and at every threshold", () => {
  XP_LEVELS.forEach((level, index) => {
    assert.equal(resolveXpLevel(level.threshold).currentLevel.level, level.level);
    if (index > 0) {
      assert.equal(resolveXpLevel(level.threshold - 1).currentLevel.level, XP_LEVELS[index - 1].level);
    }
  });
});

test("level resolution reports within-level percentage and remaining XP", () => {
  const progress = resolveXpLevel(3_625);
  assert.equal(progress.currentLevel.level, 5);
  assert.equal(progress.currentLevel.title, "Ranked");
  assert.equal(progress.nextLevel?.level, 6);
  assert.equal(progress.xpWithinLevel, 875);
  assert.equal(progress.xpForNextLevel, 1_750);
  assert.equal(progress.xpRemaining, 875);
  assert.equal(progress.percentage, 50);
});

test("maximum level stays full and tolerates XP beyond its threshold", () => {
  const progress = resolveXpLevel(12_000);
  assert.equal(progress.currentLevel.level, 8);
  assert.equal(progress.nextLevel, null);
  assert.equal(progress.percentage, 100);
  assert.equal(progress.xpRemaining, 0);
});

test("invalid XP totals resolve safely to a fresh Level 1 state", () => {
  for (const invalid of [-1, Number.NaN, Number.POSITIVE_INFINITY, "750", null]) {
    const progress = resolveXpLevel(invalid);
    assert.equal(progress.totalXp, 0);
    assert.equal(progress.currentLevel.level, 1);
    assert.equal(progress.percentage, 0);
  }
});

test("new check-in, feedback and camp awards map to presentation state", () => {
  const awards = [
    apiAward(),
    apiAward({
      id: "22222222-2222-2222-2222-222222222222",
      action: "feedback_with_comment",
      amount: 3,
    }),
    apiAward({
      id: "33333333-3333-3333-3333-333333333333",
      action: "camp_completed",
      amount: 500,
    }),
  ];
  const result = parseXpAwardResponse({
    state: {
      total_xp: 513,
      last_daily_login_date: null,
      recent_awards: awards,
    },
    previous_total_xp: 13,
    awarded: true,
    award: awards[2],
  });

  assert.equal(result.state.totalXp, 513);
  assert.deepEqual(
    result.state.recentAwards.map((award) => [award.action, award.amount]),
    [
      ["readiness_checkin_completed", 10],
      ["feedback_with_comment", 3],
      ["camp_completed", 500],
    ],
  );
  assert.equal(result.award?.action, "camp_completed");
});

test("legacy daily-login ledger rows remain readable", () => {
  const award = apiAward({
    action: "daily_login",
    amount: 10,
    calendar_date: "2026-08-01",
  });
  const result = parseXpAwardResponse({
    state: {
      total_xp: 10,
      last_daily_login_date: "2026-08-01",
      recent_awards: [award],
    },
    previous_total_xp: 10,
    awarded: false,
    award: null,
  });

  assert.equal(result.state.recentAwards[0]?.action, "daily_login");
  assert.equal(result.state.recentAwards[0]?.calendarDate, "2026-08-01");
});

test("malformed, unknown, or reward-mismatched server data fails closed", () => {
  const invalidResponses = [
    null,
    {},
    {
      state: { total_xp: -1, last_daily_login_date: null, recent_awards: [] },
      previous_total_xp: 0,
      awarded: false,
      award: null,
    },
    {
      state: { total_xp: 10, last_daily_login_date: "2026-02-30", recent_awards: [] },
      previous_total_xp: 0,
      awarded: true,
      award: apiAward({ amount: 999 }),
    },
    {
      state: { total_xp: 10, last_daily_login_date: null, recent_awards: [apiAward({ action: "coins" })] },
      previous_total_xp: 0,
      awarded: false,
      award: null,
    },
  ];

  for (const response of invalidResponses) {
    assert.throws(() => parseXpAwardResponse(response), /invalid XP data/);
  }
});
