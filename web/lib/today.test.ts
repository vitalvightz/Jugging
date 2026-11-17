import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";

import {
  TODAY_EMPTY_TEXT,
  TODAY_EMPTY_TITLE,
  buildTodayCheckinPayload,
  completionRequiresModificationReason,
  completionRequiresReviewFields,
  canCompleteTodaySession,
  getActiveSevereInjury,
  getAthleteWarnings,
  getCampDayLabel,
  getCompletionActions,
  getDecisionTier,
  getInjuryOverrideBanner,
  getOverviewCommandEyebrow,
  getOverviewPrimaryAction,
  getRecommendationCopy,
  getRiskTimeframeLabel,
  getRiskWatchSummary,
  getSafeSessionView,
  getTierMeta,
  getTodayDecisionBanner,
  getVisibleRiskWatch,
  hasActivePlan,
  hasTodaySession,
  isHardCombatSession,
  isSessionToday,
  resolveDecisionTier,
  shouldShowTodayCheckin,
} from "./today.ts";
import type { TodayDecisionTier } from "./today.ts";
import type { InjuryFlagRecord } from "./types.ts";
import {
  listPendingRehabResponses,
  submitTodayCheckin,
  submitTodaySessionCompletion,
} from "./api.ts";
import type { TodayCommandView, TodaySession } from "./types.ts";

process.env.NEXT_PUBLIC_API_DEBUG = "false";

const BASE_STATE: TodayCommandView = {
  active_plan: { id: "11111111-1111-1111-1111-111111111111", name: "Camp", phase: "SPP" },
  today: {
    training_day: "2026-06-18",
    recommendation_state: "not_checked_in",
    recommendation_reason: null,
    next_session: { session_id: "sess-1", weekday: "Thu", status: "Hard day" },
    session_scope: "today",
    session_label: "Today's session",
    completion_status: "not_started",
  },
  risk_watch: [],
  open_injuries: [],
  week_summary: {},
  quick_actions: [],
};

const TAPER_REASON = [
  "Sharp work only.",
  "You are in taper, so sharpness matters more than extra work today.",
  "Keep speed and timing work only; remove tiring rounds.",
].join("\n");

const MODIFY_REASON = [
  "Session reduced.",
  "Poor sleep before hard combat work raises injury risk today.",
  "Skip sparring, hard rounds, and conditioning finishers.",
].join("\n");

const PULL_BACK_REASON = [
  "Pull back today.",
  "Your readiness is too low for hard combat work today.",
  "Skip hard combat work today. Use recovery or light mobility instead.",
].join("\n");

const INJURY_REASON = [
  "Rehab only today.",
  "The injury is worse, so hard combat work is not safe today.",
  "No sparring, live rounds, clinch work, hard bag work, or conditioning.",
].join("\n");

const RED_FLAG_REASON = [
  "No training today.",
  "You selected a red flag symptom, so training is not safe.",
  "Stop training and seek medical advice.",
].join("\n");

function installFetchMock(responseBody: unknown) {
  const calls: Array<{ input: string; init?: RequestInit }> = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ input: String(input), init });
    return new Response(JSON.stringify(responseBody), {
      status: 201,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;
  return {
    calls,
    restore: () => {
      globalThis.fetch = originalFetch;
    },
  };
}

test("fail-safe machine signals never reach the athlete as warning text", () => {
  const sameDay = "You already completed a check-in today. This response applies to the current active plan only.";
  assert.deepEqual(
    getAthleteWarnings([
      "readiness_context_status=degraded; reason_codes=completions_unavailable",
      "reason_code:completions_unavailable",
      sameDay,
    ]),
    [sameDay],
  );
  assert.deepEqual(getAthleteWarnings(undefined), []);
});

test("no active plan state uses intake copy and no plan identity", () => {
  assert.equal(TODAY_EMPTY_TITLE, "No active plan yet");
  assert.equal(TODAY_EMPTY_TEXT, "Complete intake to generate your training plan.");
  assert.equal(hasActivePlan({}), false);
});

test("active plan without check-in shows the check-in module rule", () => {
  assert.equal(shouldShowTodayCheckin(BASE_STATE), true);
  assert.equal(
    shouldShowTodayCheckin({
      ...BASE_STATE,
      today: { ...BASE_STATE.today, recommendation_state: "modify" },
    }),
    false,
  );
});

test("decision banner is hidden before check-in", () => {
  assert.equal(getTodayDecisionBanner("not_checked_in"), null);
});

test("preview session shows PREVIEW instead of GO and stays neutral", () => {
  const banner = getTodayDecisionBanner("train_as_planned", TAPER_REASON, { isPreview: true });
  const uncheckedPreview = getTodayDecisionBanner("not_checked_in", null, { isPreview: true });

  assert.equal(banner?.chip, "PREVIEW");
  assert.notEqual(banner?.chip, "GO");
  assert.equal(banner?.displayState, "preview");
  assert.equal(banner?.tone, "neutral");
  assert.equal(uncheckedPreview?.chip, "PREVIEW");
  assert.equal(uncheckedPreview?.title, "Session preview");
  assert.equal(uncheckedPreview?.detail.includes("check-in is clear"), false);
});

test("actionable recommendation states show the correct coach chips", () => {
  const go = getTodayDecisionBanner("train_as_planned", TAPER_REASON);
  const adjust = getTodayDecisionBanner("modify", MODIFY_REASON);
  const pullBack = getTodayDecisionBanner("pull_back", PULL_BACK_REASON);

  assert.equal(go?.chip, "GO");
  assert.equal(go?.tone, "green");
  assert.equal(adjust?.chip, "ADJUST");
  assert.equal(adjust?.tone, "amber");
  assert.equal(pullBack?.chip, "PULL BACK");
  assert.equal(pullBack?.tone, "red");
});

test("safety pull-back copy maps to rehab-only or no-training chips", () => {
  const injury = getTodayDecisionBanner("pull_back", INJURY_REASON);
  const highPain = getTodayDecisionBanner(
    "pull_back",
    [
      "Rehab only today.",
      "Pain is high, so contact and impact are not safe today.",
      "Use rehab or easy mobility only; skip sparring, pads, bag work, and conditioning.",
    ].join("\n"),
  );
  const redFlag = getTodayDecisionBanner("pull_back", RED_FLAG_REASON);

  assert.equal(injury?.chip, "REHAB ONLY");
  assert.equal(injury?.tone, "red");
  assert.equal(highPain?.chip, "REHAB ONLY");
  assert.equal(redFlag?.chip, "NO TRAINING");
  assert.equal(redFlag?.title, "No training today");
});

test("decision banner removes trailing title stops and shortens taper display copy", () => {
  const taper = getTodayDecisionBanner("train_as_planned", TAPER_REASON);
  const modified = getTodayDecisionBanner("modify", MODIFY_REASON);

  assert.equal(taper?.title, "Sharp taper work");
  assert.equal(taper?.detail, "Taper phase: sharpness over extra rounds.");
  assert.equal(taper?.action, "Keep speed and timing clean. Remove tiring rounds.");
  assert.equal(modified?.title, "Session reduced");
});

test("check-in payload does not include a frontend recommendation", () => {
  const payload = buildTodayCheckinPayload({
    planId: "11111111-1111-1111-1111-111111111111",
    phase: "TAPER",
    sleep: "poor",
    body: "flat",
    pain: "manageable",
    safetyFlags: {
      sharp_pain: false,
      instability: false,
      swelling: false,
      neurological_symptoms: false,
      illness_symptoms: false,
      cannot_warm_into_movement: false,
      worse_next_day_pain: false,
    },
  });

  assert.equal(payload.phase, "TAPER");
  assert.equal("recommendation_state" in payload, false);
});

test("active plan in TAPER sends TAPER in the check-in payload", () => {
  const state: TodayCommandView = {
    ...BASE_STATE,
    active_plan: { ...BASE_STATE.active_plan, phase: "TAPER" },
  };
  const payload = buildTodayCheckinPayload({
    planId: state.active_plan.id ?? "",
    phase: state.active_plan.phase,
    sleep: "poor",
    body: "flat",
    pain: "manageable",
    safetyFlags: {
      sharp_pain: false,
      instability: false,
      swelling: false,
      neurological_symptoms: false,
      illness_symptoms: false,
      cannot_warm_into_movement: false,
      worse_next_day_pain: false,
    },
  });

  assert.equal(payload.phase, "TAPER");
});

test("missing phase does not silently default an active plan check-in to GPP", () => {
  assert.throws(
    () =>
      buildTodayCheckinPayload({
        planId: "11111111-1111-1111-1111-111111111111",
        phase: undefined,
        sleep: "poor",
        body: "flat",
        pain: "manageable",
        safetyFlags: {
          sharp_pain: false,
          instability: false,
          swelling: false,
          neurological_symptoms: false,
          illness_symptoms: false,
          cannot_warm_into_movement: false,
          worse_next_day_pain: false,
        },
      }),
    /phase is unavailable/,
  );
});

test("recommendation copy maps valid backend states", () => {
  assert.equal(getRecommendationCopy("train_as_planned").label, "Train as planned");
  assert.equal(getRecommendationCopy("modify").label, "Adjust");
  assert.equal(getRecommendationCopy("pull_back").label, "Pull back");
  assert.match(getRecommendationCopy("pull_back").actionText, /light mobility/);
});

test("session empty and completion action states are mapped", () => {
  assert.equal(hasTodaySession({}), false);
  assert.deepEqual(getCompletionActions("not_started"), ["Start session", "Mark skipped"]);
  assert.deepEqual(getCompletionActions("started"), ["Resume session", "Mark done", "Mark modified", "Mark skipped"]);
});

test("session details without session_id are visible but not completable", () => {
  const session = { weekday: "Thu", status: "Hard day" };
  assert.equal(hasTodaySession(session), true);
  assert.equal(canCompleteTodaySession(session), false);
  assert.equal(canCompleteTodaySession({ ...session, session_id: "sess-1" }), true);
});

test("off-day entries with no load are not completable even with a session_id", () => {
  const offDay = { session_id: "2026-06-18", weekday: "Thu", effective_load: "none" };
  assert.equal(hasTodaySession(offDay), true);
  assert.equal(canCompleteTodaySession(offDay), false);
  // A real training load keeps the session completable.
  assert.equal(canCompleteTodaySession({ ...offDay, effective_load: "hard" }), true);
});

test("check-in payload never asks injury or previous-session (server derives both)", () => {
  const payload = buildTodayCheckinPayload({
    planId: "11111111-1111-1111-1111-111111111111",
    phase: "GPP",
    sleep: "good",
    body: "normal",
    pain: "none",
    safetyFlags: {
      sharp_pain: false,
      instability: false,
      swelling: false,
      neurological_symptoms: false,
      illness_symptoms: false,
      cannot_warm_into_movement: false,
      worse_next_day_pain: false,
    },
  });

  assert.equal("active_injury" in payload, false);
  assert.equal("previous_session" in payload, false);
});

test("modified requires a reason and done/modified require review fields", () => {
  assert.equal(completionRequiresModificationReason("modified"), true);
  assert.equal(completionRequiresModificationReason("done"), false);
  assert.equal(completionRequiresReviewFields("done"), true);
  assert.equal(completionRequiresReviewFields("modified"), true);
  assert.equal(completionRequiresReviewFields("skipped"), false);
});

test("risk watch shows icon label text records with overflow count", () => {
  const { visible, overflow } = getVisibleRiskWatch([
    { category: "stop_red_flag", priority: 1, icon: "octagon-x", label: "Stop", text: "Pull back.", tone: "stop" },
    { category: "fatigue", priority: 6, icon: "battery-low", label: "Fatigue", text: "Poor sleep.", tone: "caution" },
    { category: "high_pain", priority: 3, icon: "alert-triangle", label: "High pain", text: "Pain high.", tone: "warning" },
  ]);

  assert.equal(visible.length, 2);
  assert.equal(visible[0].icon.length > 0, true);
  assert.equal(visible[0].label.length > 0, true);
  assert.equal(visible[0].text.length > 0, true);
  assert.equal(overflow, 1);
});

test("risk timeframe labels are explicit and legacy risks stay unlabeled", () => {
  assert.equal(getRiskTimeframeLabel("today"), "Today");
  assert.equal(getRiskTimeframeLabel("last_session"), "Last session");
  assert.equal(getRiskTimeframeLabel("recent_sessions"), "Recent sessions");
  assert.equal(getRiskTimeframeLabel("active"), "Active");
  assert.equal(getRiskTimeframeLabel(), "");
});

function makeInjury(overrides: Partial<InjuryFlagRecord> = {}): InjuryFlagRecord {
  // Legacy tests pre-date the structured Today payload. Production never
  // classifies injury text in the browser; this helper only supplies the
  // backend fields those existing fixtures would receive.
  const bodyArea = overrides.body_area ?? "chest";
  const description = overrides.description ?? "chest bruise";
  const text = `${bodyArea} ${description}`.toLowerCase();
  const hasAny = (terms: readonly string[]) => terms.some((term) => text.includes(term));
  const bodyRegion =
    overrides.load_region ??
    (hasAny(["back of knee", "calf", "achilles", "ankle", "knee", "shin", "hip", "quad", "hamstring", "leg"])
      ? "lower_limb"
      : hasAny(["bicep", "wrist", "arm", "shoulder", "elbow", "hand"])
        ? "upper_limb"
        : hasAny(["concussion", "head", "neck", "cervical", "brain", "skull"])
          ? "head_neck"
          : hasAny(["lower back", "upper back", "spine", "spinal", "rib", "chest", "sternum", "back"])
            ? "trunk_spine"
            : "unknown");
  const deniedStructural = hasAny([
    "no fracture",
    "no fracture or tear",
    "ruled out rupture",
    "nothing is ruptured",
  ]);
  const consequence =
    overrides.consequence !== undefined
      ? overrides.consequence
      : hasAny(["concussion", "concussed"])
        ? "neuro"
        : !deniedStructural && hasAny([
            "fracture",
            "rupture",
            "tear",
            "torn",
            "dislocation",
            "avulsion",
          ])
          ? "structural"
          : null;
  return {
    id: "inj-1",
    athlete_id: "ath-1",
    source: "checkin",
    body_area: bodyArea,
    description,
    label: "Chest bruise",
    load_region: bodyRegion,
    consequence,
    severity: "severe",
    status: "open",
    created_at: "2026-07-06T00:00:00Z",
    updated_at: "2026-07-06T00:00:00Z",
    ...overrides,
  };
}

function stateWithInjuries(
  injuries: InjuryFlagRecord[],
  recommendation: TodayCommandView["today"]["recommendation_state"] = "modify",
): TodayCommandView {
  return {
    ...BASE_STATE,
    today: { ...BASE_STATE.today, recommendation_state: recommendation, recommendation_reason: MODIFY_REASON },
    open_injuries: injuries,
  };
}

test("every severity x status combo blocks iff an active severe injury exists", () => {
  // Exhaustive matrix so no combo can silently open a bypass. The block is
  // severity-driven: a SEVERE injury blocks while open or monitoring/easing.
  // Moderate/mild never hard-block, and a resolved injury clears.
  const severities = ["mild", "moderate", "severe"] as const;
  const statuses = ["open", "monitoring", "resolved"] as const;
  for (const severity of severities) {
    for (const status of statuses) {
      const blocks = severity === "severe" && (status === "open" || status === "monitoring");
      const result = getActiveSevereInjury([makeInjury({ severity, status })]);
      assert.equal(Boolean(result), blocks, `${severity}/${status} should ${blocks ? "block" : "not block"}`);
    }
  }
  assert.equal(getActiveSevereInjury([]), null);
  assert.equal(getActiveSevereInjury(undefined), null);
});

test("a severe injury still blocks when a mild injury is open alongside it", () => {
  const injuries = [
    makeInjury({ id: "mild-1", severity: "mild", status: "open" }),
    makeInjury({ id: "sev-1", severity: "severe", status: "monitoring" }),
  ];
  assert.equal(getActiveSevereInjury(injuries)?.id, "sev-1");
});

test("severe injury override supersedes the daily recommendation banner", () => {
  const banner = getInjuryOverrideBanner(stateWithInjuries([makeInjury()]), "Hard sparring");

  assert.ok(banner);
  assert.equal(banner?.chip, "CHECK");
  assert.equal(banner?.title, "Training caution");
  assert.equal(banner?.displayState, "injury_blocked");
  assert.equal(banner?.tone, "red");
  assert.match(banner?.detail ?? "", /Active severe injury: Chest bruise/);
  assert.match(banner?.detail ?? "", /hard sparring/);
  assert.match(banner?.detail ?? "", /until it is cleared\./);
  assert.match(banner?.safety ?? "", /superseded by the injury warning/);
});

test("can't-train-normally keeps the same hold without a severe medical label", () => {
  const injury = makeInjury({ description: "chest bruise [training_impact:cant_train]" });
  const banner = getInjuryOverrideBanner(stateWithInjuries([injury]), "Hard sparring");

  assert.equal(banner?.chip, "CHECK");
  assert.equal(banner?.displayState, "injury_blocked");
  assert.match(banner?.detail ?? "", /^Can't train normally: /);
  assert.doesNotMatch(banner?.detail ?? "", /severe/i);
});

test("severe injury override remains active while monitoring", () => {
  const easing = getInjuryOverrideBanner(
    stateWithInjuries([makeInjury({ status: "monitoring" })]),
    "Hard sparring",
  );
  assert.ok(easing, "an easing severe injury must still block");
  assert.equal(easing?.displayState, "injury_blocked");
  // Resolving the injury is the only state that removes the override.
  assert.equal(getInjuryOverrideBanner(stateWithInjuries([makeInjury({ status: "resolved" })]), "Hard sparring"), null);
});

test("no injury override without a severe active injury", () => {
  assert.equal(
    getInjuryOverrideBanner(stateWithInjuries([makeInjury({ severity: "moderate" })]), "Hard sparring"),
    null,
  );
  assert.equal(getInjuryOverrideBanner(BASE_STATE, "Hard sparring"), null);
});

test("override falls back to 'this session' and drops the superseded line before check-in", () => {
  const banner = getInjuryOverrideBanner(stateWithInjuries([makeInjury()], "not_checked_in"), "Today's session");
  assert.match(banner?.detail ?? "", /Do not complete this session/);
  assert.equal(banner?.safety, undefined);
});

test("submit check-in calls the Today check-in endpoint", async () => {
  const mock = installFetchMock({
    training_day: "2026-06-18",
    recommendation_state: "modify",
    recommendation_reason: "Poor sleep.",
    triggers: ["poor_sleep"],
  });

  try {
    await submitTodayCheckin("token", {
      plan_id: "11111111-1111-1111-1111-111111111111",
      sleep: "poor",
      body: "normal",
      pain: "none",
      phase: "GPP",
    });

    assert.equal(mock.calls.length, 1);
    assert.equal(mock.calls[0].input.endsWith("/api/today/checkin"), true);
    assert.equal(mock.calls[0].init?.method, "POST");
  } finally {
    mock.restore();
  }
});

test("submit completion calls the Today completion endpoint", async () => {
  const mock = installFetchMock({
    completion_status: "done",
    landing_session_state: "completed",
  });

  try {
    await submitTodaySessionCompletion("token", {
      plan_id: "11111111-1111-1111-1111-111111111111",
      session_id: "sess-1",
      status: "done",
      session_rpe: 7,
      pain_after: 2,
    });

    assert.equal(mock.calls.length, 1);
    assert.equal(mock.calls[0].input.endsWith("/api/today/session-completion"), true);
    assert.equal(mock.calls[0].init?.method, "POST");
  } finally {
    mock.restore();
  }
});

test("pending rehab responses are read across the exact active plan", async () => {
  const mock = installFetchMock({ response_sets: [], history_truncated: false });

  try {
    await listPendingRehabResponses(
      "token",
      "11111111-1111-1111-1111-111111111111",
    );

    assert.equal(mock.calls.length, 1);
    const url = new URL(mock.calls[0].input);
    assert.equal(url.pathname, "/api/today/rehab-responses/pending");
    assert.equal(url.searchParams.get("plan_id"), "11111111-1111-1111-1111-111111111111");
    assert.equal(url.searchParams.has("training_day"), false);
    assert.equal(mock.calls[0].init?.method, undefined);
  } finally {
    mock.restore();
  }
});

test("Today session card uses short preview wording and Next session label", () => {
  const source = readFileSync(new URL("../components/today/today-session-panel.tsx", import.meta.url), "utf8");

  assert.equal(source.includes('kicker: "Next session"'), true);
  assert.equal(source.includes('kicker: "Next scheduled session"'), false);
  assert.equal(source.includes("Available ${when} after check-in."), true);

});

test("Today recommendation styles keep preview neutral, modify amber, and pull-back red", () => {
  const css = readFileSync(new URL("../app/globals.css", import.meta.url), "utf8");
  const baseBlock = css.match(/\.today-decision-banner\s*{[^}]+}/)?.[0] ?? "";
  const neutralBlock = css.match(/\.today-decision-banner\[data-tone="neutral"\]\s*{[^}]+}/)?.[0] ?? "";
  const amberBlock = css.match(/\.today-decision-banner\[data-tone="amber"\]\s*{[^}]+}/)?.[0] ?? "";
  const redBlock = css.match(/\.today-decision-banner\[data-tone="red"\]\s*{[^}]+}/)?.[0] ?? "";

  assert.match(baseBlock, /inset 3px 0 0 var\(--decision-accent\)/);
  assert.match(neutralBlock, /--decision-accent/);
  assert.doesNotMatch(neutralBlock, /#ff6b75|#e23a4c|219,\s*47,\s*64/);
  assert.match(amberBlock, /#f1bd61|214,\s*175,\s*106/);
  assert.doesNotMatch(amberBlock, /#ff6b75|#e23a4c|219,\s*47,\s*64/);
  assert.match(redBlock, /#ff6b75|219,\s*47,\s*64/);
});

test("readiness display messages do not include banned old wording", () => {
  const banners = [
    getTodayDecisionBanner("train_as_planned", TAPER_REASON),
    getTodayDecisionBanner("modify", MODIFY_REASON),
    getTodayDecisionBanner("pull_back", PULL_BACK_REASON),
    getTodayDecisionBanner("pull_back", INJURY_REASON),
    getTodayDecisionBanner("pull_back", RED_FLAG_REASON),
  ];
  const text = banners
    .map((banner) => [banner?.title, banner?.detail, banner?.action, banner?.safety].filter(Boolean).join(" "))
    .join(" ")
    .toLowerCase();

  for (const banned of [
    "prescribed dose",
    "readiness state",
    "modify session",
    "tissue margin",
    "recovery margin",
    "fatigue-heavy accessories",
    "max-effort",
    "sprinting",
    "plyos",
    "heavy lower-body",
    "remove 1 set",
  ]) {
    assert.equal(text.includes(banned), false, banned);
  }
});

test("Today resolves today's blocks from the shared current-day resolver", () => {
  // Today renders today's exact blocks from the active plan's structured_plan,
  // resolved through the SAME shared resolver Plan Detail uses (resolveCurrentDay
  // + the client-mounted 03:00 training-day hook) so the two screens can never
  // disagree on the current day/session.
  const source = readFileSync(new URL("../components/today/today-session-panel.tsx", import.meta.url), "utf8");
  assert.equal(source.includes("resolveCurrentDay"), true);
  assert.equal(source.includes("useTrainingDay"), true);
});

test("Today uses structured titles only for actual structured today sessions", () => {
  const source = readFileSync(new URL("../components/today/today-session-panel.tsx", import.meta.url), "utf8");
  assert.equal(
    source.includes("const sessionTitle = hasResolvedDaySessions"),
    true,
  );
  assert.equal(
    source.includes(": getSessionTitle(session);"),
    true,
  );
});

test("injury extra detail is stale only when rewriting to a different type", () => {
  const source = readFileSync(new URL("../components/guided-injury-card.tsx", import.meta.url), "utf8");
  const nullBranch = source.slice(
    source.indexOf("if (!opt) {"),
    source.indexOf("const isSame ="),
  );
  const sameBranch = source.slice(
    source.indexOf("if (isSame) {"),
    source.indexOf("const currentType = injury.injury_type;"),
  );
  const rewriteBranch = source.slice(
    source.indexOf("const currentType = injury.injury_type;"),
    source.indexOf("clearTypeSpecificFields(onUpdate);", source.indexOf("const currentType = injury.injury_type;")),
  );

  assert.equal(nullBranch.includes("flagStaleExtraDetail()"), false);
  assert.equal(sameBranch.includes("flagStaleExtraDetail()"), false);
  assert.equal(rewriteBranch.includes("flagStaleExtraDetail()"), true);
});

test("Today renders only today's session, never the full camp map", () => {
  // Today must scope to today's day only. It reuses the per-session camp-map
  // cards but must NOT mount the full StructuredPlanRenderer (command header,
  // week strip, every day) — that belongs to Plan Detail (/plans/[planId]).
  const todayDir = new URL("../components/today/", import.meta.url);
  const sources = [
    readFileSync(new URL("../components/today-screen.tsx", import.meta.url), "utf8"),
    ...readdirSync(todayDir).map((name) => readFileSync(new URL(name, todayDir), "utf8")),
  ].join("\n");
  assert.equal(sources.includes("StructuredPlanRenderer"), false);
  assert.equal(sources.includes("WeekStrip"), false);
});

test("Today renders one recommendation and feedback prompt in the required DOM order", () => {
  const screen = readFileSync(
    new URL("../components/today-screen.tsx", import.meta.url),
    "utf8",
  );
  const sessionPanel = readFileSync(
    new URL("../components/today/today-session-panel.tsx", import.meta.url),
    "utf8",
  );
  const orderedMarkers = [
    "<TodayReadinessStrip",
    "<TodayDecisionPanel",
    // Compact risks belong to the command surface, immediately after the
    // decision, rather than beneath the injury manager.
    "<TodayRiskWatch",
    // Feedback sits with the guidance it asks about, inside the command card.
    'surface="daily_recommendation"',
    // Session renders via the {sessionPanel} slot so its safe-replacement
    // ordering with the readiness form stays intact.
    "{sessionPanel}",
    "<TodayInjuryManager",
  ];
  const positions = orderedMarkers.map((marker) => screen.indexOf(marker));

  assert.ok(positions.every((position) => position >= 0));
  assert.deepEqual([...positions].sort((left, right) => left - right), positions);
  assert.equal((screen.match(/<TodayDecisionPanel/g) ?? []).length, 1);
  assert.equal((screen.match(/<TodayRiskWatch/g) ?? []).length, 1);
  assert.equal((screen.match(/<TodayReadinessForm/g) ?? []).length, 1);
  assert.equal((screen.match(/<TodaySessionPanel/g) ?? []).length, 1);
  assert.equal((screen.match(/surface="daily_recommendation"/g) ?? []).length, 1);
  assert.ok(screen.includes("resolvedDecision.useSafeReplacement"));
  const normalizedScreen = screen.replace(/\r\n/g, "\n");
  // Safe replacement puts the check-in after the session; the session keeps
  // one slot either way so a reorder never remounts it.
  assert.ok(normalizedScreen.indexOf(
    "{resolvedDecision.useSafeReplacement ? null : readinessForm}\n      {sessionPanel}\n      {resolvedDecision.useSafeReplacement ? readinessForm : null}",
  ) >= 0);
  assert.equal(sessionPanel.includes("TodayDecisionPanel"), false);
  assert.equal(sessionPanel.includes("ContextualFeedback"), false);
  assert.ok(screen.includes('resolvedDecision.displayTier === "preview"'));
});

test("Today's View full plan action routes to the plan detail camp map", () => {
  const source = readFileSync(new URL("../components/today-screen.tsx", import.meta.url), "utf8");
  assert.equal(source.includes("/plans/${activePlan.id}"), true);
});

test("fatigue level control is backed by a range input for drag interaction", () => {
  const source = readFileSync(new URL("../components/rating-controls.tsx", import.meta.url), "utf8");
  assert.equal(source.includes('className="level-slider-input"'), true);
  assert.equal(source.includes('type="range"'), true);
  assert.equal(source.includes("onPointerDown={selectLevelFromPointer}"), true);
});

test("decision tiers map 1:1 with display-states", () => {
  const green = getTodayDecisionBanner("train_as_planned", TAPER_REASON);
  const modify = getTodayDecisionBanner("modify", MODIFY_REASON);
  const pullBack = getTodayDecisionBanner("pull_back", PULL_BACK_REASON);
  const rehab = getTodayDecisionBanner("pull_back", INJURY_REASON);
  const noTraining = getTodayDecisionBanner("pull_back", RED_FLAG_REASON);
  const injury = getInjuryOverrideBanner(stateWithInjuries([makeInjury()]), "Hard sparring");
  const preview = getTodayDecisionBanner("train_as_planned", TAPER_REASON, { isPreview: true });

  assert.equal(getDecisionTier(green), "green");
  assert.equal(getDecisionTier(modify), "modify");
  assert.equal(getDecisionTier(pullBack), "pull_back");
  assert.equal(getDecisionTier(rehab), "stop");
  assert.equal(getDecisionTier(noTraining), "stop");
  assert.equal(getDecisionTier(injury), "stop");
  assert.equal(getDecisionTier(preview), "preview");
  assert.equal(getDecisionTier(null), "not_checked_in");
});

test("tier meta gives the coach-facing labels and tones", () => {
  assert.equal(getTierMeta("stop").label, "Stop today");
  assert.equal(getTierMeta("stop").tone, "red");
  assert.equal(getTierMeta("pull_back").label, "Pull back today");
  assert.equal(getTierMeta("modify").label, "Modify today");
  assert.equal(getTierMeta("green").label, "Green light");
  assert.equal(getTierMeta("stop").eyebrow, "Today's action");
});

test("isSessionToday reads the backend scope, never its own date maths", () => {
  assert.equal(isSessionToday({}, "today"), true);
  assert.equal(isSessionToday({}, "next"), false);
  assert.equal(isSessionToday({}, "none"), false);
  // The backend owns session timing (today_service.build_today_command_view).
  // A "next" scope stays "next" even when the session is stamped with today's
  // date — re-deriving the day here is what let Overview and Today disagree.
  assert.equal(
    isSessionToday({ session_relation: "today" }, "next"),
    false,
  );
  // Only a payload predating session_scope falls back to the relation stamp.
  assert.equal(isSessionToday({ session_relation: "today" }), true);
  assert.equal(isSessionToday({ session_relation: "next" }), false);
  assert.equal(isSessionToday(null), false);
});

test("isHardCombatSession reads load + status, not technical/rest", () => {
  assert.equal(isHardCombatSession({ effective_load: "hard" }), true);
  assert.equal(isHardCombatSession({ status: "hard_as_planned" }), true);
  assert.equal(isHardCombatSession({ effective_load: "technical" }), false);
  assert.equal(isHardCombatSession({ effective_load: "none" }), false);
  assert.equal(isHardCombatSession({}), false);
});

test("safe session names the blocked work and lists allowed/blocked", () => {
  const view = getSafeSessionView("Technical sparring");
  assert.match(view.detail, /Training caution: reconsider technical sparring today/);
  assert.equal(view.title, "Recovery / mobility only");
  assert.equal(view.allowed.length, 5);
  assert.equal(view.allowed.includes("Light bike or walk"), true);
  assert.equal(view.blocked.includes("Sparring"), true);
  assert.equal(view.blocked.includes("Heavy lifting"), true);
});

test("safe session keeps bike/walk for an upper-body or minor lower-leg injury", () => {
  // A bicep tear (upper body) leaves the legs free — the screenshot case.
  const bicep = getSafeSessionView("Technical sparring", [
    makeInjury({ body_area: "left bicep", description: "left bicep tear", label: "Left bicep tear", severity: "moderate", load_region: "upper_limb", consequence: "structural" }),
  ]);
  assert.equal(bicep.allowed.includes("Light bike or walk"), true);
  assert.equal(bicep.allowed.length, 5);

  // A minor lower-leg niggle (not structural, not severe) still tolerates a spin.
  const tightCalf = getSafeSessionView("Technical sparring", [
    makeInjury({ body_area: "calf", description: "tight calf", label: "Calf tightness", severity: "mild", load_region: "lower_limb", consequence: "load_sensitive" }),
  ]);
  assert.equal(tightCalf.allowed.includes("Light bike or walk"), true);

  // A resolved lower-leg tear no longer constrains today.
  const resolved = getSafeSessionView("Technical sparring", [
    makeInjury({ body_area: "calf", description: "calf tear", label: "Calf tear", severity: "severe", status: "resolved", load_region: "lower_limb", consequence: "structural" }),
  ]);
  assert.equal(resolved.allowed.includes("Light bike or walk"), true);
});

test("safe session swaps bike/walk for seated upper-body cardio on a load-intolerant lower-leg injury", () => {
  // Structural lower-leg injuries: gait / pedal load is out, but symptom-gated
  // seated upper-body cardio still keeps the athlete moving.
  const substitute = "Seated upper-body cardio, only if pain-free and available";
  for (const injury of [
    makeInjury({ body_area: "left calf", description: "calf tear", label: "Left calf tear", severity: "moderate", load_region: "lower_limb", consequence: "structural" }),
    makeInjury({ body_area: "achilles", description: "achilles rupture", label: "Achilles rupture", severity: "moderate", load_region: "lower_limb", consequence: "structural" }),
    makeInjury({ body_area: "ankle", description: "ankle fracture", label: "Ankle fracture", severity: "moderate", load_region: "lower_limb", consequence: "structural" }),
    makeInjury({ body_area: "knee", description: "acl tear", label: "ACL tear", severity: "moderate", load_region: "lower_limb", consequence: "structural" }),
    // Severe lower-leg injury of any type also disqualifies gait/pedal load.
    makeInjury({ body_area: "shin", description: "shin pain", label: "Shin pain", severity: "severe", load_region: "lower_limb", consequence: "load_sensitive" }),
  ]) {
    const view = getSafeSessionView("Technical sparring", [injury]);
    assert.equal(view.allowed.includes("Light bike or walk"), false, injury.label);
    assert.equal(view.allowed.includes(substitute), true, injury.label);
    assert.equal(view.allowed.length, 5, injury.label);
  }
});

test("safe session drops all cardio when both legs and arms cannot take load", () => {
  const view = getSafeSessionView("Technical sparring", [
    makeInjury({ id: "a", body_area: "ankle", description: "ankle fracture", label: "Ankle fracture", severity: "moderate", load_region: "lower_limb", consequence: "structural" }),
    makeInjury({ id: "b", body_area: "wrist", description: "wrist fracture", label: "Wrist fracture", severity: "moderate", load_region: "upper_limb", consequence: "structural" }),
  ]);
  assert.equal(view.allowed.some((item) => item.toLowerCase().includes("cardio")), false);
  assert.equal(view.allowed.includes("Light bike or walk"), false);
  // Mobility, breathing, activation and coach-led rehab remain.
  assert.deepEqual(view.allowed, ["Easy mobility", "Breathing reset", "Gentle activation", "Coach-approved rehab"]);
});

test("safe session recognizes generic arm injuries before offering upper-body cardio", () => {
  for (const bodyArea of ["arm", "arms", "upper arm"]) {
    const view = getSafeSessionView("Technical sparring", [
      makeInjury({ id: "leg", body_area: "ankle", description: "ankle fracture", label: "Ankle fracture", severity: "moderate", load_region: "lower_limb", consequence: "structural" }),
      makeInjury({ id: "arm", body_area: bodyArea, description: `${bodyArea} fracture`, label: `${bodyArea} fracture`, severity: "moderate", load_region: "upper_limb", consequence: "structural" }),
    ]);
    assert.equal(view.allowed.some((item) => item.toLowerCase().includes("cardio")), false, bodyArea);
  }
});

test("safe session recognizes plural structural terms and canonical lower-limb locations", () => {
  const cases = [
    ["calf", "calf tears"],
    ["ankle", "ankle fractures"],
    ["achilles", "achilles ruptures"],
    ["hip", "hip dislocations"],
    ["quads", "quad tear"],
    ["hamstrings", "hamstring tear"],
  ] as const;
  for (const [bodyArea, description] of cases) {
    const view = getSafeSessionView("Technical sparring", [
      makeInjury({ body_area: bodyArea, description, label: "Structural injury", severity: "moderate", load_region: "lower_limb", consequence: "structural" }),
    ]);
    assert.equal(view.allowed.includes("Light bike or walk"), false, `${bodyArea}: ${description}`);
  }
});

test("safe session ignores negated structural terms on a mild lower-limb injury", () => {
  for (const description of [
    "no fracture, mild ankle soreness",
    "no fracture or tear, mild ankle soreness",
    "doctor ruled out rupture; mild ankle soreness",
    "nothing is ruptured, mild ankle soreness",
  ]) {
    const view = getSafeSessionView("Technical sparring", [
      makeInjury({ body_area: "ankle", description, label: "Ankle soreness", severity: "mild", load_region: "lower_limb", consequence: null }),
    ]);
    assert.equal(view.allowed.includes("Light bike or walk"), true, description);
  }
});

test("safe session downregulates to rest work for a concussion or head injury", () => {
  const view = getSafeSessionView("Technical sparring", [
    makeInjury({ body_area: "head", description: "concussion", label: "Concussion", severity: "moderate", load_region: "head_neck", consequence: "neuro" }),
  ]);
  // No aerobic push and no activation — only calm, non-provocative work.
  assert.deepEqual(view.allowed, ["Easy mobility", "Breathing reset", "Clinician-approved rehab"]);

  // A generic severe head injury is caught too (not just the word "concussion").
  const head = getSafeSessionView("Technical sparring", [
    makeInjury({ body_area: "head", description: "head injury", label: "Head injury", severity: "severe", load_region: "head_neck", consequence: null }),
  ]);
  assert.deepEqual(head.allowed, ["Easy mobility", "Breathing reset", "Clinician-approved rehab"]);

  // A severe neck injury downregulates the same way…
  const neck = getSafeSessionView("Technical sparring", [
    makeInjury({ body_area: "neck", description: "cervical fracture", label: "Neck fracture", severity: "severe", load_region: "head_neck", consequence: "structural" }),
  ]);
  assert.equal(neck.allowed.includes("Light bike or walk"), false);
  assert.equal(neck.allowed.includes("Gentle activation"), false);

  // …but a mild stiff neck keeps the standard menu.
  const stiffNeck = getSafeSessionView("Technical sparring", [
    makeInjury({ body_area: "neck", description: "stiff neck", label: "Neck stiffness", severity: "mild", load_region: "head_neck", consequence: "load_sensitive" }),
  ]);
  assert.equal(stiffNeck.allowed.includes("Light bike or walk"), true);
  assert.equal(stiffNeck.allowed.includes("Gentle activation"), true);
});

test("safe session restricts a structural or severe spine/back/rib injury to rest work", () => {
  // A structural/severe trunk injury gets no movement advice — only breathing and
  // clinician-led rehab.
  for (const injury of [
    makeInjury({ body_area: "lower back", description: "lumbar disc injury", label: "Lower back injury", severity: "severe", load_region: "trunk_spine", consequence: "load_sensitive" }),
    makeInjury({ body_area: "back", description: "spinal fracture", label: "Spinal fracture", severity: "moderate", load_region: "trunk_spine", consequence: "structural" }),
    makeInjury({ body_area: "ribs", description: "rib fracture", label: "Rib fracture", severity: "moderate", load_region: "trunk_spine", consequence: "structural" }),
    makeInjury({ body_area: "chest", description: "severe chest injury", label: "Chest injury", severity: "severe", load_region: "trunk_spine", consequence: "load_sensitive" }),
  ]) {
    const view = getSafeSessionView("Technical sparring", [injury]);
    assert.deepEqual(view.allowed, ["Breathing reset", "Clinician-approved rehab"], injury.label);
  }

  // A mild back niggle still keeps the standard menu…
  const mildBack = getSafeSessionView("Technical sparring", [
    makeInjury({ body_area: "lower back", description: "tight lower back", label: "Lower back tightness", severity: "mild", load_region: "trunk_spine", consequence: "load_sensitive" }),
  ]);
  assert.equal(mildBack.allowed.includes("Light bike or walk"), true);

  // …and "back of knee" is a lower-limb injury, not a spine one.
  const backOfKnee = getSafeSessionView("Technical sparring", [
    makeInjury({ body_area: "back of knee", description: "back of knee tear", label: "Knee tear", severity: "moderate", load_region: "lower_limb", consequence: "structural" }),
  ]);
  assert.equal(backOfKnee.allowed.includes("Breathing reset"), true);
  assert.equal(
    backOfKnee.allowed.includes("Seated upper-body cardio, only if pain-free and available"),
    true,
  );
});

test("safe session blocks the explosive work of the region actually at risk", () => {
  // No injury (or a lower-limb one): lower-body explosive work is the default hazard.
  assert.equal(
    getSafeSessionView("Technical sparring").blocked.includes("Plyos or explosive lower-body work"),
    true,
  );
  const lower = getSafeSessionView("Technical sparring", [
    makeInjury({ body_area: "ankle", description: "ankle fracture", label: "Ankle fracture", severity: "moderate", load_region: "lower_limb", consequence: "structural" }),
  ]);
  assert.equal(lower.blocked.includes("Plyos or explosive lower-body work"), true);

  // Upper-limb injury: explosive UPPER-body work is the hazard, plus overhead/pressing.
  const upper = getSafeSessionView("Technical sparring", [
    makeInjury({ body_area: "left bicep", description: "left bicep tear", label: "Left bicep tear", severity: "moderate", load_region: "upper_limb", consequence: "structural" }),
  ]);
  assert.equal(upper.blocked.includes("Plyos or explosive upper-body work"), true);
  assert.equal(upper.blocked.includes("Plyos or explosive lower-body work"), false);
  assert.equal(upper.blocked.includes("Overhead or pressing work"), true);

  // Both limbs injured: block explosive work outright, unqualified.
  const both = getSafeSessionView("Technical sparring", [
    makeInjury({ id: "a", body_area: "ankle", description: "ankle fracture", label: "Ankle fracture", severity: "moderate", load_region: "lower_limb", consequence: "structural" }),
    makeInjury({ id: "b", body_area: "wrist", description: "wrist fracture", label: "Wrist fracture", severity: "moderate", load_region: "upper_limb", consequence: "structural" }),
  ]);
  assert.equal(both.blocked.includes("Plyos or explosive work"), true);

  // The universal stop-day blocks are never dropped.
  for (const view of [lower, upper, both]) {
    for (const item of ["Sparring", "Hard pads", "HIIT", "Heavy lifting"]) {
      assert.equal(view.blocked.includes(item), true, item);
    }
  }
});

test("safe session blocks the loading pattern each injured region cannot take", () => {
  // Trunk/spine: loaded rotation and bracing (valsalva) are the specific danger.
  const trunk = getSafeSessionView("Technical sparring", [
    makeInjury({ body_area: "ribs", description: "rib fracture", label: "Rib fracture", severity: "moderate", load_region: "trunk_spine", consequence: "structural" }),
  ]);
  assert.equal(trunk.blocked.includes("Loaded rotation or bracing"), true);

  // Concussion: head impact is called out explicitly, beyond blanket sparring.
  const head = getSafeSessionView("Technical sparring", [
    makeInjury({ body_area: "head", description: "concussion", label: "Concussion", severity: "moderate", load_region: "head_neck", consequence: "neuro" }),
  ]);
  assert.equal(head.blocked.includes("Head impact or contact drills"), true);

  // A minor injury adds no extra restriction.
  const mild = getSafeSessionView("Technical sparring", [
    makeInjury({ body_area: "left bicep", description: "sore bicep", label: "Bicep soreness", severity: "mild", load_region: "upper_limb", consequence: "load_sensitive" }),
  ]);
  assert.equal(mild.blocked.length, 5);
});

test("safe session copy never contradicts the menu it shows", () => {
  // Rest-only: no movement is offered, so the card must not say "keep the body moving".
  const restOnly = getSafeSessionView("Technical sparring", [
    makeInjury({ body_area: "back", description: "spinal fracture", label: "Spinal fracture", severity: "moderate", load_region: "trunk_spine", consequence: "structural" }),
  ]);
  assert.deepEqual(restOnly.allowed, ["Breathing reset", "Clinician-approved rehab"]);
  assert.equal(restOnly.title, "Rest and recover");
  assert.equal(/keep the body moving/.test(restOnly.detail), false);
  assert.match(restOnly.detail, /no loaded movement today/i);

  // Downregulate still offers mobility, so "mobility only" remains accurate.
  const neuro = getSafeSessionView("Technical sparring", [
    makeInjury({ body_area: "head", description: "concussion", label: "Concussion", severity: "moderate", load_region: "head_neck", consequence: "neuro" }),
  ]);
  assert.equal(neuro.title, "Recovery / mobility only");
  assert.equal(/keep the body moving/.test(neuro.detail), false);
  assert.match(neuro.detail, /symptom-free/);

  // The standard stop day keeps the original coach copy.
  const standard = getSafeSessionView("Technical sparring");
  assert.match(standard.detail, /Training caution: reconsider technical sparring today/);
  assert.match(standard.detail, /keep the body moving without adding stress/);
});

test("safe session does not misread anatomical head/back terms as head or spine injuries", () => {
  // "long head of biceps" is an arm injury — legs are free, so bike/walk stays.
  const bicepHead = getSafeSessionView("Technical sparring", [
    makeInjury({ body_area: "left bicep", description: "long head of biceps tear", label: "Left bicep tear", severity: "moderate", load_region: "upper_limb", consequence: "structural" }),
  ]);
  assert.equal(bicepHead.allowed.includes("Light bike or walk"), true);
  assert.equal(bicepHead.allowed.length, 5);
});

test("camp day counts down from training day to fight date", () => {
  assert.equal(getCampDayLabel("2026-07-06", "2026-07-23"), "D-17");
  assert.equal(getCampDayLabel("2026-07-23", "2026-07-23"), "D-0");
  assert.equal(getCampDayLabel("", "2026-07-23"), "");
  assert.equal(getCampDayLabel("2026-07-24", "2026-07-23"), ""); // past fight
});

test("risk watch summary reports count and the strongest signal", () => {
  assert.deepEqual(getRiskWatchSummary([]), { count: 0, strongestLabel: "" });
  const summary = getRiskWatchSummary([
    { category: "stop_red_flag", priority: 1, icon: "octagon-x", label: "Stop", text: "x", tone: "stop" },
    { category: "fatigue", priority: 6, icon: "battery-low", label: "Fatigue", text: "y", tone: "caution" },
  ]);
  assert.equal(summary.count, 2);
  assert.equal(summary.strongestLabel, "STOP");
});

test("getOverviewPrimaryAction resolves one dominant CTA per athlete state", () => {
  const base = {
    hasActivePlan: true,
    planCount: 1,
    hasInjuryOverride: false,
    recommendation: "train_as_planned" as TodayCommandView["today"]["recommendation_state"],
    decisionTier: "green" as TodayDecisionTier,
    hasSafeSession: false,
    sessionIsToday: false,
  };

  // No plans at all -> build the first one.
  assert.deepEqual(getOverviewPrimaryAction({ ...base, hasActivePlan: false, planCount: 0 }), {
    href: "/quick-build",
    label: "Build your plan",
  });
  // Plans exist but none active -> select one on /plans (the beta test account).
  assert.deepEqual(getOverviewPrimaryAction({ ...base, hasActivePlan: false, planCount: 4 }), {
    href: "/plans",
    label: "Select active plan",
  });
  // Severe-injury override outranks the daily decision (even when not checked in).
  assert.deepEqual(
    getOverviewPrimaryAction({
      ...base,
      hasInjuryOverride: true,
      recommendation: "not_checked_in",
      decisionTier: "stop",
    }),
    { href: "/today#today-injury", label: "Open injury check-in" },
  );
  // Not checked in yet -> check in.
  assert.deepEqual(
    getOverviewPrimaryAction({ ...base, recommendation: "not_checked_in", decisionTier: "not_checked_in" }),
    { href: "/today#today-checkin", label: "Check in now" },
  );
  // STOP with a safe replacement -> route to it.
  assert.deepEqual(getOverviewPrimaryAction({ ...base, decisionTier: "stop", hasSafeSession: true }), {
    href: "/today#today-session",
    label: "View safe session",
  });
  // STOP without a safe session must NOT fall through to a train label.
  assert.deepEqual(getOverviewPrimaryAction({ ...base, decisionTier: "stop", hasSafeSession: false }), {
    href: "/today#today-session",
    label: "Review stop guidance",
  });
  // A future session must not be presented as today's session.
  assert.deepEqual(getOverviewPrimaryAction(base), {
    href: "/today?preview=1",
    label: "Preview session",
  });
  // A session matched to the training day keeps the today's-session label.
  assert.deepEqual(getOverviewPrimaryAction({ ...base, sessionIsToday: true }), {
    href: "/today#today-session",
    label: "Today's session",
  });
});

test("Overview reserves Today's command for current-day decisions", () => {
  assert.equal(getOverviewCommandEyebrow("preview"), "Next up");
  for (const tier of ["stop", "pull_back", "modify", "green", "not_checked_in"] as const) {
    assert.equal(getOverviewCommandEyebrow(tier), "Today's command");
  }
});

test("Overview consumes the shared authoritative resolver", () => {
  const source = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");

  assert.equal(source.includes("resolveTodayDecision(commandState)"), true);
  assert.equal(source.includes("resolvedDecision?.severeInjuryBlocksCurrentSession"), true);
  assert.equal(source.includes("getInjuryOverrideBanner(commandState"), false);
  assert.equal(source.includes("resolveDecisionTier(commandState?.today"), false);
  assert.equal(source.includes("getOverviewCommandEyebrow(decisionTier)"), true);
  assert.equal(source.includes("overview-risk-footer"), false);
  assert.equal(source.includes("getRiskWatchText(risk)"), false);
  assert.equal(source.includes("Review {overflow} more on Today"), true);
});

// ---------------------------------------------------------------------------
// Decision-tier unification: the banner and the risk-watch footer both render
// from one authoritative tier, so they can never contradict.
// ---------------------------------------------------------------------------

function stateWithTier(
  decision_tier: TodayCommandView["today"]["decision_tier"],
  recommendation_state: TodayCommandView["today"]["recommendation_state"] = "pull_back",
  recommendation_reason: string | null = PULL_BACK_REASON,
): TodayCommandView {
  return {
    ...BASE_STATE,
    today: { ...BASE_STATE.today, recommendation_state, recommendation_reason, decision_tier },
  };
}

test("resolveDecisionTier prefers the authoritative backend tier over the banner parse", () => {
  const state = stateWithTier("pull_back");
  const banner = getTodayDecisionBanner(
    state.today.recommendation_state,
    state.today.recommendation_reason,
  );
  assert.equal(resolveDecisionTier(state.today, banner), "pull_back");

  const stopState = stateWithTier("stop", "pull_back", INJURY_REASON);
  const stopBanner = getTodayDecisionBanner(
    stopState.today.recommendation_state,
    stopState.today.recommendation_reason,
  );
  assert.equal(resolveDecisionTier(stopState.today, stopBanner), "stop");
});

test("resolveDecisionTier keeps the preview framing for a next-session card", () => {
  const banner = getTodayDecisionBanner("not_checked_in", null, { isPreview: true });
  assert.equal(resolveDecisionTier({ decision_tier: "green" }, banner), "preview");
});

test("risk footer never shouts louder than the decision tier", () => {
  // A plain PULL BACK day still carries a stop_red_flag risk (it echoes the
  // recommendation); the footer must read PULL BACK, not STOP.
  const risks: TodayCommandView["risk_watch"] = [
    { category: "stop_red_flag", priority: 1, icon: "octagon-x", label: "Stop", text: "Pull back.", tone: "stop" },
  ];
  assert.equal(getRiskWatchSummary(risks, "pull_back").strongestLabel, "PULL BACK");
  assert.equal(getRiskWatchSummary(risks, "modify").strongestLabel, "MODIFY");
  // A genuine STOP day keeps the STOP signal.
  assert.equal(getRiskWatchSummary(risks, "stop").strongestLabel, "STOP");
  // Without a tier the raw signal is returned (back-compat).
  assert.equal(getRiskWatchSummary(risks).strongestLabel, "STOP");
});

test("tier and footer agree across the decision hierarchy", () => {
  const injuryRisk: TodayCommandView["risk_watch"] = [
    { category: "active_injury_worse", priority: 2, icon: "bandage", label: "Injury worsening", text: "", tone: "stop" },
  ];
  // Injury signal is clamped to the tier: MODIFY tier → footer cannot read INJURY-as-stop louder than modify.
  assert.equal(getRiskWatchSummary(injuryRisk, "modify").strongestLabel, "MODIFY");
  assert.equal(getRiskWatchSummary(injuryRisk, "stop").strongestLabel, "INJURY");
});

test("a next session that is not today is never completable even without a session_relation", () => {
  // session_relation is absent, but session_scope is not "today": the session
  // must read as pending (isSessionToday === false), so the completion gate
  // (canCompleteTodaySession && sessionIsToday) can never open.
  const session: TodaySession = { session_id: "sess-9", effective_load: "hard" };
  assert.equal(isSessionToday(session, "next"), false);
  assert.equal(isSessionToday(session, "none"), false);
  assert.equal(canCompleteTodaySession(session) && isSessionToday(session, "next"), false);
});

test("a severe injury does not block a safe filler session (injury_hold_exempt)", () => {
  const severe = makeInjury({ severity: "severe", status: "open" });
  const normalState: TodayCommandView = { ...BASE_STATE, open_injuries: [severe] };
  // Normally a severe injury forces the INJURY HOLD override banner.
  assert.equal(getInjuryOverrideBanner(normalState)?.chip, "CHECK");
  // On a filler day the backend exempts the hold, so no override banner shows.
  const fillerState: TodayCommandView = {
    ...BASE_STATE,
    open_injuries: [severe],
    today: { ...BASE_STATE.today, injury_hold_exempt: true },
  };
  assert.equal(getInjuryOverrideBanner(fillerState), null);
});
