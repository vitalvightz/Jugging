import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  buildStructuredPlanFromText,
  canShowContextualPlanFeedback,
  canRebuildEnhancedCard,
  parsePlanText,
  splitLabeledSegments,
  buildReviewSummary,
  describePlanReleaseState,
  canRetryResumeGenerationForPlan,
  getAdminReviewHeading,
  hasBlockedTriageStubText,
  isPlanReleasedToAthlete,
  isProtectedTriageResumePendingState,
  isRecentlyCreatedPlan,
  readInjuryTriage,
  readRawTriageMode,
  readStructuredCardDebug,
  resolveApprovalAfterError,
  resolvePlanActiveState,
  shouldAwaitStructuredPlanUpgrade,
  shouldHoldPlanForEnhancedCard,
  shouldPollForStructuredPlanUpgrade,
  shouldShowProtectedResumeAdminReview,
  shouldUseSavedStructuredPlan,
  EnhancedCardLockInCard,
  StructuredCardStatusChip,
} from "./plan-viewer";
import { ApiError, RETRYABLE_NETWORK_MESSAGE } from "@/lib/api";
import { HARD_STAGE2_BLOCKER_CODES } from "@/lib/stage2-policy";
import type { PlanDetail, StructuredCardState } from "@/lib/types";

const PLAN_VIEWER_SOURCE = readFileSync(new URL("./plan-viewer.tsx", import.meta.url), "utf8");

function makePlan(overrides: { status?: string; planText?: string }): PlanDetail {
  return {
    status: overrides.status ?? "ready",
    outputs: { plan_text: overrides.planText ?? "# Plan body" },
  } as unknown as PlanDetail;
}

test("validated custom cards are released without AI markdown, including archived history", () => {
  const custom = {
    ...makePlan({ planText: "" }), custom_plan_id: "draft",
    structured_card_state: { state: "live", reasons: [] } as StructuredCardState,
    outputs: { plan_text: "", structured_plan: { plan_metadata: { prescription_source: "athlete" as const } } },
  };
  assert.equal(isPlanReleasedToAthlete(custom), true);
  assert.equal(isPlanReleasedToAthlete({ ...custom, status: "archived" }), true);
  assert.equal(isPlanReleasedToAthlete({ ...custom, status: "held_for_review" }), false);
  assert.equal(isPlanReleasedToAthlete({ ...custom, structured_card_state: { state: "failed", reasons: [] } }), false);
  assert.equal(isPlanReleasedToAthlete(makePlan({ planText: "" })), false);
});

test("coach assignments release their custom card without an editable athlete draft or AI finalization", () => {
  const assigned = {
    ...makePlan({ planText: "" }),
    plan_source: "custom", custom_plan_id: null,
    structured_card_state: { state: "live", reasons: [] } as StructuredCardState,
    outputs: { plan_text: "", structured_plan: { plan_metadata: { prescription_source: "athlete" as const } } },
  };
  for (const status of ["ready", "publishable_with_flags", "archived"]) {
    assert.equal(isPlanReleasedToAthlete({ ...assigned, status }), true, status);
  }
  // A compiled assignment awaiting athlete consent must remain inaccessible.
  for (const status of ["review_required", "held_for_review", "generated", "failed"]) {
    assert.equal(isPlanReleasedToAthlete({ ...assigned, status }), false, status);
  }
  assert.equal(isPlanReleasedToAthlete({ ...assigned, outputs: { plan_text: "" } }), false);
  assert.equal(isPlanReleasedToAthlete({ ...assigned, plan_source: "generated" }), false);
});

test("plan feedback is visible to athletes and only the owning admin", () => {
  assert.equal(canShowContextualPlanFeedback("athlete", "athlete-1", "athlete-1"), true);
  assert.equal(canShowContextualPlanFeedback("admin", "admin-1", "admin-1"), true);
  assert.equal(canShowContextualPlanFeedback("admin", "admin-1", "athlete-1"), false);
  assert.equal(canShowContextualPlanFeedback("admin", null, "admin-1"), false);
  assert.equal(canShowContextualPlanFeedback("coach", "coach-1", "coach-1"), false);
});

test("Today can confirm the active plan when the dedicated endpoint fails", () => {
  assert.equal(
    resolvePlanActiveState({
      todayResolved: true,
      todayActivePlanId: "plan-1",
      activeEndpointResolved: false,
    }),
    "plan-1",
  );
  assert.equal(
    resolvePlanActiveState({
      todayResolved: false,
      activeEndpointResolved: false,
    }),
    undefined,
  );
});

test("the dedicated active-plan endpoint takes priority, including an explicit null", () => {
  assert.equal(
    resolvePlanActiveState({
      todayResolved: true,
      todayActivePlanId: "stale-plan",
      activeEndpointResolved: true,
      activeEndpointPlanId: null,
    }),
    null,
  );
  assert.equal(
    resolvePlanActiveState({
      todayResolved: true,
      todayActivePlanId: "stale-plan",
      activeEndpointResolved: true,
      activeEndpointPlanId: "current-plan",
    }),
    "current-plan",
  );
});

const STRUCTURED_CARD_CHIP_CASES: Array<{
  cardState: StructuredCardState;
  label: string;
}> = [
  {
    cardState: { state: "live", reasons: [], schema_version: "structured-plan.v2" },
    label: "Enhanced card live",
  },
  {
    cardState: { state: "building", reasons: [], attempt_started_at: "2026-07-11T10:00:00Z" },
    label: "Enhanced card building",
  },
  {
    cardState: { state: "failed", reasons: ["build did not complete"] },
    label: "Enhanced card failed",
  },
  {
    cardState: { state: "not_attempted", reasons: ["converter unavailable"] },
    label: "Enhanced card not attempted",
  },
  {
    cardState: { state: "none", reasons: [] },
    label: "Enhanced card no record",
  },
];

for (const { cardState, label } of STRUCTURED_CARD_CHIP_CASES) {
  test(`structured-card admin chip renders the ${cardState.state} state`, () => {
    const html = renderToStaticMarkup(
      createElement(StructuredCardStatusChip, { cardState }),
    );

    assert.match(html, new RegExp(`data-state="${cardState.state}"`));
    assert.match(html, new RegExp(label));
    assert.ok(html.includes("structured-card-status-chip"));
  });
}

test("live structured-card chip renders its schema version compactly", () => {
  const html = renderToStaticMarkup(
    createElement(StructuredCardStatusChip, {
      cardState: {
        state: "live",
        reasons: [],
        schema_version: "structured-plan.v2",
      },
    }),
  );

  assert.match(html, /structured-card-schema-version/);
  assert.match(html, /structured-plan\.v2/);
});

test("structured-card chip never renders blank when the lifecycle field is absent", () => {
  const html = renderToStaticMarkup(
    createElement(StructuredCardStatusChip, { cardState: undefined }),
  );

  assert.match(html, /data-state="none"/);
  assert.match(html, /Enhanced card no record/);
});

test("enhanced-card rebuild is enabled only for failed and not-attempted states", () => {
  assert.equal(canRebuildEnhancedCard({ state: "failed", reasons: [] }), true);
  assert.equal(canRebuildEnhancedCard({ state: "not_attempted", reasons: [] }), true);
  assert.equal(canRebuildEnhancedCard({ state: "building", reasons: [] }), false);
  assert.equal(canRebuildEnhancedCard({ state: "live", reasons: [] }), false);
  assert.equal(canRebuildEnhancedCard({ state: "none", reasons: [] }), false);
});

test("triage_resume_approved with empty validator report and restricted rehab stub is not publishable", () => {
  const hasStub = hasBlockedTriageStubText(
    "## Injury Triage: Restricted Rehab Only\nNormal fight-camp planning is intentionally suspended\nClinician clearance is required",
    "",
  );
  assert.equal(hasStub, true);

  const summary = buildReviewSummary({}, "triage_resume_approved", {
    hasBlockedTriageStubText: hasStub,
  });

  assert.equal(summary.isPublishable, false);
  assert.match(summary.headline, /Resume approved, regeneration pending/i);
});

test("sparring advisory omits generated why rationale", () => {
  assert.equal(PLAN_VIEWER_SOURCE.includes("Why this flag"), false);
  assert.equal(PLAN_VIEWER_SOURCE.includes("sparring-advisory-why-toggle"), false);
  assert.equal(PLAN_VIEWER_SOURCE.includes("sparring-advisory-reason"), false);
});

test("ready final stage2 status without blocked stub can be publishable", () => {
  const summary = buildReviewSummary({ is_publishable: true }, "stage2_pass", {
    hasBlockedTriageStubText: false,
  });

  assert.equal(summary.isPublishable, true);
});

test("stage2 failed without validator reasons or blockers is still releasable", () => {
  const summary = buildReviewSummary({}, "stage2_failed", {
    hasBlockedTriageStubText: false,
  });

  assert.equal(summary.isPublishable, true);
  assert.equal(summary.hasIssues, false);
  assert.equal(summary.blockingCount, 0);
  assert.match(summary.headline, /ready to release/i);
});

test("a flagged plan is never labelled Held in the admin sidebar", () => {
  // publishable_with_flags means the athlete already has the plan. Deriving this
  // label from the validator summary said "Held" for exactly those plans.
  assert.equal(describePlanReleaseState({ status: "publishable_with_flags" }), "Released with flags");
  assert.equal(describePlanReleaseState({ status: "ready" }), "Released");
});

test("release state still reports genuinely withheld and protected plans", () => {
  assert.equal(describePlanReleaseState({ status: "review_required" }), "Held");
  assert.equal(describePlanReleaseState({ status: "held_for_review" }), "Held");
  assert.equal(describePlanReleaseState({ status: "archived" }), "Archived");
  assert.equal(
    describePlanReleaseState({ status: "ready", isTriageBlocked: true, triageMode: "medical_hold" }),
    "Blocked",
  );
  assert.equal(
    describePlanReleaseState({ status: "ready", isTriageBlocked: true, triageMode: "needs_review" }),
    "Protected",
  );
  assert.equal(
    describePlanReleaseState({ status: "ready", isProtectedTriageResumePending: true }),
    "Blocked / resume pending",
  );
});

test("stage 1 fallback release says the finalizer failed rather than looking routine", () => {
  // The report is clean because the validator never ran against the Stage 1
  // body. Without this branch the admin sees a plain "ready to release" and the
  // technical Stage 2 failure is invisible.
  const summary = buildReviewSummary({}, "stage2_failed_stage1_fallback", {
    hasBlockedTriageStubText: false,
  });

  assert.equal(summary.isPublishable, true);
  assert.equal(summary.hasIssues, false);
  assert.match(summary.headline, /Released from Stage 1/i);
  assert.match(summary.headline, /finalizer pass failed/i);
  assert.match(summary.guidance, /stage2_fallback/);
});

test("flagged blockers are described as flagged, not as holding the plan", () => {
  // Stage 2 findings no longer withhold a plan, so the summary must not tell an
  // admin that something is waiting on them.
  const hardBlockerCode = HARD_STAGE2_BLOCKER_CODES[0];
  const summary = buildReviewSummary(
    { blocking_warnings: [{ code: hardBlockerCode, message: "Hard blocker present." }] },
    "stage2_failed",
    { hasBlockedTriageStubText: false },
  );

  assert.equal(summary.hasIssues, true);
  assert.match(summary.headline, /Flagged on this Stage 2 plan/i);
  assert.doesNotMatch(summary.headline, /holding/i);
  assert.match(summary.guidance, /released to the athlete/i);
});

test("blocked triage stub without validator reasons still explains the hold", () => {
  const summary = buildReviewSummary({}, "stage2_failed", {
    hasBlockedTriageStubText: true,
  });

  assert.equal(summary.isPublishable, false);
  assert.equal(summary.hasIssues, true);
  assert.equal(summary.blockingCount, 0);
  assert.match(summary.headline, /Triage placeholder text/i);
  assert.match(summary.guidance, /cannot be released/i);
});

test("soft review warnings do not block release summary", () => {
  const summary = buildReviewSummary(
    {
      warnings: [{ code: "generic_filler_phrase", message: "Low-trust filler." }],
      review_flags: [{ code: "generic_filler_phrase", message: "Low-trust filler." }],
      review_flag_count: 1,
      is_publishable: false,
    },
    "stage2_pass",
    { hasBlockedTriageStubText: false },
  );

  assert.equal(summary.isPublishable, true);
  assert.equal(summary.hasIssues, false);
  assert.match(summary.headline, /ready to release/i);
});

test("legacy soft blocking warnings do not block release summary", () => {
  const summary = buildReviewSummary(
    {
      warnings: [{ code: "missing_required_element", message: "Missing phase element." }],
      blocking_warnings: [{ code: "missing_required_element", message: "Missing phase element." }],
      is_publishable: false,
    },
    "stage2_pass",
    { hasBlockedTriageStubText: false },
  );

  assert.equal(summary.isPublishable, true);
  assert.equal(summary.blockingCount, 0);
  assert.equal(summary.hasIssues, false);
});

test("hard blocking warnings still hold release summary", () => {
  const hardBlockerCode = HARD_STAGE2_BLOCKER_CODES[0];
  const summary = buildReviewSummary(
    {
      blocking_warnings: [
        {
          code: hardBlockerCode,
          message: "Hard blocker present.",
        },
      ],
    },
    "stage2_failed",
    { hasBlockedTriageStubText: false },
  );

  assert.equal(summary.isPublishable, false);
  assert.equal(summary.blockingCount, 1);
  assert.equal(summary.hasIssues, true);
});

test("triage resume approved state is protected even without explicit stub", () => {
  const protectedState = isProtectedTriageResumePendingState({
    isTriageBlocked: false,
    stage2Status: "triage_resume_approved",
    containsBlockedTriageStub: false,
    athletePlanText: "",
    finalPlanText: "Structured draft waiting for regeneration",
  });

  assert.equal(protectedState, true);
});

test("protected resume-approved state exposes retry action and hides release controls", () => {
  const showProtected = shouldShowProtectedResumeAdminReview({
    isTriageBlocked: false,
    isProtectedTriageResumePending: true,
    hasResumeApproval: true,
  });
  assert.equal(showProtected, true);
  assert.equal(
    getAdminReviewHeading({ showProtectedResumeAdminReview: showProtected, hasResumeApproval: true }),
    "Resume generation required",
  );
});

test("readRawTriageMode keeps original mode after resume approval", () => {
  const plan = {
    status: "triage_resume_approved",
    admin_outputs: {
      stage2_status: "triage_resume_approved",
      why_log: { injury_triage: { mode: "needs_review" } },
    },
  };

  assert.equal(readRawTriageMode(plan as never), "needs_review");
  assert.equal(readInjuryTriage(plan as never)?.mode, "needs_review");
});

test("medical_hold does not allow retry resume", () => {
  const canRetry = canRetryResumeGenerationForPlan({
    isAdmin: true,
    isProtectedTriageResumePending: true,
    injuryTriageMode: "medical_hold",
  });
  assert.equal(canRetry, false);
});

test("medical_hold always blocks retry even when another source looks resumable", () => {
  const canRetry = canRetryResumeGenerationForPlan({
    isAdmin: true,
    isProtectedTriageResumePending: true,
    injuryTriageMode: "needs_review",
    rawTriageMode: "Medical_Hold",
    planStatus: "triage_resume_approved",
  });
  assert.equal(canRetry, false);
});

test("needs_review and restricted_rehab_only allow retry resume", () => {
  assert.equal(
    canRetryResumeGenerationForPlan({
      isAdmin: true,
      isProtectedTriageResumePending: true,
      injuryTriageMode: "needs_review",
    }),
    true,
  );
  assert.equal(
    canRetryResumeGenerationForPlan({
      isAdmin: true,
      isProtectedTriageResumePending: true,
      rawTriageMode: "restricted_rehab_only",
    }),
    true,
  );
});

test("isPlanReleasedToAthlete requires an athlete-visible status with plan text", () => {
  assert.equal(isPlanReleasedToAthlete(makePlan({ status: "ready", planText: "# Plan" })), true);
  assert.equal(
    isPlanReleasedToAthlete(makePlan({ status: "publishable_with_flags", planText: "x" })),
    true,
  );
  // Ready but empty plan text is not yet releasable.
  assert.equal(isPlanReleasedToAthlete(makePlan({ status: "ready", planText: "   " })), false);
  // Non-athlete-visible status is never released, even with plan text.
  assert.equal(
    isPlanReleasedToAthlete(makePlan({ status: "review_required", planText: "# Plan" })),
    false,
  );
});

test("recent published plan without structured card awaits the background upgrade", () => {
  assert.equal(
    shouldAwaitStructuredPlanUpgrade({
      hasPublishedPlan: true,
      hasStructuredPlan: false,
      pollWindowExpired: false,
      hasAccessToken: true,
      isRecentPlan: true,
    }),
    true,
  );
});

test("structured card or an elapsed poll window stops awaiting the upgrade", () => {
  assert.equal(
    shouldAwaitStructuredPlanUpgrade({
      hasPublishedPlan: true,
      hasStructuredPlan: true,
      pollWindowExpired: false,
      hasAccessToken: true,
      isRecentPlan: true,
    }),
    false,
  );
  assert.equal(
    shouldAwaitStructuredPlanUpgrade({
      hasPublishedPlan: true,
      hasStructuredPlan: false,
      pollWindowExpired: true,
      hasAccessToken: true,
      isRecentPlan: true,
    }),
    false,
  );
});

test("triage blocked plans do not await a structured-card upgrade", () => {
  assert.equal(
    shouldAwaitStructuredPlanUpgrade({
      hasPublishedPlan: true,
      hasStructuredPlan: false,
      pollWindowExpired: false,
      hasAccessToken: true,
      isRecentPlan: true,
      isTriageBlocked: true,
    }),
    false,
  );
});

test("legacy plans without a structured card do not await an upgrade", () => {
  assert.equal(
    shouldAwaitStructuredPlanUpgrade({
      hasPublishedPlan: true,
      hasStructuredPlan: false,
      pollWindowExpired: false,
      hasAccessToken: true,
      isRecentPlan: false,
    }),
    false,
  );
});

test("plans without an access token cannot await a structured upgrade", () => {
  assert.equal(
    shouldAwaitStructuredPlanUpgrade({
      hasPublishedPlan: true,
      hasStructuredPlan: false,
      pollWindowExpired: false,
      hasAccessToken: false,
      isRecentPlan: true,
    }),
    false,
  );
});

const LOCKIN_HOLD_BASE = {
  isViewerAdmin: false,
  structuredCardLifecycleState: "building" as const,
  hasPublishedPlan: true,
  hasStructuredPlan: false,
  pollWindowExpired: false,
  hasAccessToken: true,
  isRecentPlan: true,
};

test("athletes are held on the lock-in card while the enhanced card builds", () => {
  assert.equal(shouldHoldPlanForEnhancedCard(LOCKIN_HOLD_BASE), true);
  // "none" covers the gap right after publish before the lifecycle record lands.
  assert.equal(
    shouldHoldPlanForEnhancedCard({
      ...LOCKIN_HOLD_BASE,
      structuredCardLifecycleState: "none",
    }),
    true,
  );
});

test("admins are never held on the lock-in card", () => {
  assert.equal(
    shouldHoldPlanForEnhancedCard({ ...LOCKIN_HOLD_BASE, isViewerAdmin: true }),
    false,
  );
});

test("terminal card states fall back to the deterministic plan instead of holding", () => {
  for (const state of ["failed", "not_attempted", "live"] as const) {
    assert.equal(
      shouldHoldPlanForEnhancedCard({
        ...LOCKIN_HOLD_BASE,
        structuredCardLifecycleState: state,
      }),
      false,
      `state ${state} must not hold the athlete view`,
    );
  }
});

test("the lock-in hold is bounded and only applies to plans that can still upgrade", () => {
  assert.equal(
    shouldHoldPlanForEnhancedCard({ ...LOCKIN_HOLD_BASE, pollWindowExpired: true }),
    false,
  );
  assert.equal(
    shouldHoldPlanForEnhancedCard({ ...LOCKIN_HOLD_BASE, isRecentPlan: false }),
    false,
  );
  assert.equal(
    shouldHoldPlanForEnhancedCard({ ...LOCKIN_HOLD_BASE, hasAccessToken: false }),
    false,
  );
  assert.equal(
    shouldHoldPlanForEnhancedCard({ ...LOCKIN_HOLD_BASE, hasStructuredPlan: true }),
    false,
  );
  assert.equal(
    shouldHoldPlanForEnhancedCard({ ...LOCKIN_HOLD_BASE, hasPublishedPlan: false }),
    false,
  );
  assert.equal(
    shouldHoldPlanForEnhancedCard({ ...LOCKIN_HOLD_BASE, isTriageBlocked: true }),
    false,
  );
});

test("the lock-in card announces the camp is being lxcked in", () => {
  const html = renderToStaticMarkup(createElement(EnhancedCardLockInCard));
  assert.match(html, /YOUR CAMP IS BEING LXCKED IN/);
  assert.match(html, /reviewing and finalising your camp/);
  assert.match(html, /2-5 minutes/);
  assert.match(html, /role="status"/);
});

test("the upgrade poll keeps running for an older published plan still missing its card", () => {
  // The key fix: a plan whose card lands after the 5-minute recency window (e.g.
  // approved several minutes after generation) must still auto-swap on an open
  // view. The poll is NOT recency-gated, unlike the visible hint.
  assert.equal(
    shouldPollForStructuredPlanUpgrade({
      hasPublishedPlan: true,
      hasStructuredPlan: false,
      pollWindowExpired: false,
      hasAccessToken: true,
    }),
    true,
  );
  // But it stops once the card exists, the mount-scoped window elapses, the plan
  // isn't published, the token is missing, or the plan is triage-blocked.
  assert.equal(
    shouldPollForStructuredPlanUpgrade({
      hasPublishedPlan: true,
      hasStructuredPlan: true,
      pollWindowExpired: false,
      hasAccessToken: true,
    }),
    false,
  );
  assert.equal(
    shouldPollForStructuredPlanUpgrade({
      hasPublishedPlan: true,
      hasStructuredPlan: false,
      pollWindowExpired: true,
      hasAccessToken: true,
    }),
    false,
  );
  assert.equal(
    shouldPollForStructuredPlanUpgrade({
      hasPublishedPlan: true,
      hasStructuredPlan: false,
      pollWindowExpired: false,
      hasAccessToken: true,
      isTriageBlocked: true,
    }),
    false,
  );
});

test("an admin-held plan polls while its server-authoritative card state is building", () => {
  assert.equal(
    shouldPollForStructuredPlanUpgrade({
      hasPublishedPlan: false,
      hasStructuredPlan: false,
      pollWindowExpired: false,
      hasAccessToken: true,
      isServerBuilding: true,
    }),
    true,
  );
  assert.equal(
    shouldPollForStructuredPlanUpgrade({
      hasPublishedPlan: false,
      hasStructuredPlan: false,
      pollWindowExpired: false,
      hasAccessToken: true,
      isServerBuilding: true,
      isTriageBlocked: true,
    }),
    false,
  );
});

test("readStructuredCardDebug surfaces every recorded outcome and sanitises reasons", () => {
  const make = (structured: unknown) =>
    ({ admin_outputs: { stage2_validator_report: { structured_plan: structured } } }) as never;

  // A rejected conversion is shown with its drift reasons.
  assert.deepEqual(
    readStructuredCardDebug(
      make({ status: "invalid_fallback_used", errors: ["faithfulness: exercise 'X' not present in source text"] }),
    ),
    { status: "invalid_fallback_used", errors: ["faithfulness: exercise 'X' not present in source text"] },
  );
  // A `valid`/`repair_attempted_valid` status on the fallback IS the signal we
  // want (card built then lost), so it must surface — not be hidden.
  assert.deepEqual(readStructuredCardDebug(make({ status: "valid", errors: [] })), {
    status: "valid",
    errors: [],
  });
  assert.deepEqual(readStructuredCardDebug(make({ status: "repair_attempted_valid" })), {
    status: "repair_attempted_valid",
    errors: [],
  });
  // null/undefined/whitespace-only reasons are dropped, never shown as rubbish.
  assert.deepEqual(
    readStructuredCardDebug(
      make({ status: "invalid_fallback_used", errors: [null, undefined, "   ", "real reason"] }),
    ),
    { status: "invalid_fallback_used", errors: ["real reason"] },
  );
  // Nothing recorded (no debug, blank status, or no admin outputs) → nothing to show.
  assert.equal(readStructuredCardDebug(make(undefined)), null);
  assert.equal(readStructuredCardDebug(make({ status: "   " })), null);
  assert.equal(readStructuredCardDebug({ admin_outputs: null } as never), null);
});

test("isRecentlyCreatedPlan honours the recent-plan threshold", () => {
  const now = Date.parse("2026-06-22T12:00:00.000Z");
  assert.equal(
    isRecentlyCreatedPlan({ created_at: "2026-06-22T11:58:00.000Z" }, now),
    true,
  );
  assert.equal(
    isRecentlyCreatedPlan({ created_at: "2026-06-22T11:50:00.000Z" }, now),
    false,
  );
  assert.equal(isRecentlyCreatedPlan({ created_at: "" }, now), false);
});

test("resolveApprovalAfterError treats a retryable timeout as approved when getPlan reads ready", async () => {
  const readyPlan = makePlan({ status: "ready", planText: "# Released" });
  let calls = 0;
  const recovered = await resolveApprovalAfterError({
    error: new Error(RETRYABLE_NETWORK_MESSAGE),
    fetchPlan: async () => {
      calls += 1;
      return readyPlan;
    },
    wait: async () => {},
  });

  assert.equal(recovered, readyPlan);
  assert.equal(calls, 1);
});

test("resolveApprovalAfterError ignores non-retryable errors without refetching", async () => {
  let calls = 0;
  const recovered = await resolveApprovalAfterError({
    error: new ApiError("bad request", 400),
    fetchPlan: async () => {
      calls += 1;
      return makePlan({ status: "ready", planText: "# Released" });
    },
    wait: async () => {},
  });

  assert.equal(recovered, null);
  assert.equal(calls, 0);
});

test("resolveApprovalAfterError gives up after exhausting attempts when never released", async () => {
  let calls = 0;
  const recovered = await resolveApprovalAfterError({
    error: new Error(RETRYABLE_NETWORK_MESSAGE),
    attempts: 3,
    fetchPlan: async () => {
      calls += 1;
      return makePlan({ status: "review_required", planText: "" });
    },
    wait: async () => {},
  });

  assert.equal(recovered, null);
  assert.equal(calls, 3);
});

test("resolveApprovalAfterError retries past a transient getPlan failure", async () => {
  const readyPlan = makePlan({ status: "ready", planText: "# Released" });
  let calls = 0;
  const recovered = await resolveApprovalAfterError({
    error: new Error(RETRYABLE_NETWORK_MESSAGE),
    attempts: 3,
    fetchPlan: async () => {
      calls += 1;
      if (calls === 1) {
        throw new Error("temporary fetch failure");
      }
      return readyPlan;
    },
    wait: async () => {},
  });

  assert.equal(recovered, readyPlan);
  assert.equal(calls, 2);
});

test("admin review anchor id format remains stable", () => {
  const planId = "plan_123";
  const anchorId = `admin-review-${planId}`;
  assert.equal(anchorId, "admin-review-plan_123");
});

test("splitLabeledSegments breaks a packed body line into labelled details", () => {
  const segments = splitLabeledSegments(
    "Purpose: raise the base. Progress/regress: add 5 min. Stop rule: stop if dizzy.",
  );

  assert.deepEqual(segments, [
    { label: "Purpose", text: "raise the base." },
    { label: "Progress", text: "add 5 min." },
    { label: "Stop", text: "stop if dizzy." },
  ]);
});

test("splitLabeledSegments returns one unlabelled segment when no labels are present", () => {
  assert.deepEqual(splitLabeledSegments("Easy Assault Bike — 25 min Zone 2."), [
    { label: null, text: "Easy Assault Bike — 25 min Zone 2." },
  ]);
});

test("legacy plan text parses into notes, week and session groups", () => {
  const groups = parsePlanText(
    [
      "Lead notes",
      "- Injury: keep cut covered.",
      "",
      "GPP — Week 1 (D-33 to D-27) — Build aerobic base",
      "D-33 (Wednesday) — Aerobic support",
      "Why: restore repeatability.",
      "Easy Assault Bike — 25 min Zone 2. Keep nasal breathing.",
      "Purpose: raise the base. Progress/regress: add 5 min. Stop rule: stop if dizzy.",
      "",
      "Final notes",
      "Stop on dizziness.",
    ].join("\n"),
  );

  assert.deepEqual(
    groups.map((group) => group.kind),
    ["notes", "week", "notes"],
  );

  const [lead, week, final] = groups;
  assert.equal(lead.kind === "notes" && lead.title, "Lead notes");
  assert.deepEqual(lead.kind === "notes" ? lead.lines : null, ["Injury: keep cut covered."]);
  assert.equal(final.kind === "notes" && final.title, "Final notes");

  assert.ok(week.kind === "week");
  assert.equal(week.phase, "GPP");
  assert.equal(week.title, "Week 1 (D-33 to D-27) — Build aerobic base");
  assert.equal(week.sessions.length, 1);

  const session = week.sessions[0];
  assert.equal(session.countdown, "D-33");
  assert.equal(session.weekday, "Wednesday");
  assert.equal(session.title, "Aerobic support");
  assert.equal(session.objective, "restore repeatability.");
  assert.equal(session.blocks.length, 1);
  assert.equal(session.blocks[0].name, "Easy Assault Bike");
  assert.equal(session.blocks[0].dose, "25 min Zone 2. Keep nasal breathing.");
  assert.deepEqual(session.blocks[0].details, [
    { label: "Purpose", text: "raise the base." },
    { label: "Progress", text: "add 5 min." },
    { label: "Stop", text: "stop if dizzy." },
  ]);
});

test("session headers parse in both countdown-first and weekday-first forms with any separator", () => {
  const groups = parsePlanText(
    [
      "GPP — Week 1 (D-33 to D-27) — Build aerobic base",
      "D-33 (Wednesday) — Aerobic support",
      "Why: easy day.",
      "Wednesday (D-32) - Strength",
      "Why: build force.",
      "D-0 (Saturday): Fight day",
      "Why: compete.",
    ].join("\n"),
  );

  const week = groups[0];
  assert.ok(week.kind === "week");
  assert.equal(week.phase, "GPP");
  assert.deepEqual(
    week.sessions.map((session) => [session.countdown, session.weekday, session.title]),
    [
      ["D-33", "Wednesday", "Aerobic support"],
      ["D-32", "Wednesday", "Strength"],
      ["D-0", "Saturday", "Fight day"],
    ],
  );
});

test("inline Why/coach metadata on a run-on session heading is split into body, not the title", () => {
  const groups = parsePlanText(
    "GPP — Week 1 (D-33 to D-27) — Build base D-33 (Wednesday) — Aerobic support Why: restore repeatability. D-32 (Thursday) — Coach-led boxing session No app S&C today. Keep freshness.",
  );

  const week = groups[0];
  assert.ok(week.kind === "week");
  const [aerobic, coach] = week.sessions;
  assert.equal(aerobic.title, "Aerobic support");
  assert.equal(aerobic.objective, "restore repeatability.");
  assert.equal(coach.title, "Coach-led boxing session");
  assert.match(coach.coachNote ?? "", /No app S&C today/);
  assert.equal(coach.blocks.length, 0);
});

test("labeled session-level notes keep their label", () => {
  const groups = parsePlanText(
    ["D-20 (Tuesday) — Conditioning", "Note: keep it light today."].join("\n"),
  );

  const session = groups[0];
  assert.ok(session.kind === "session");
  assert.deepEqual(session.notes, ["Note: keep it light today."]);
});

// "intensity:" is a recognised session label, so it becomes its own labelled
// detail rather than staying buried in the dose — the same treatment Purpose /
// Why / Progress get, which is what makes the block render consistently. The
// dose keeps only the sets/reps/recovery, with no dangling separator.
test("compact labelled late-camp output parses into clean session blocks", () => {
  const groups = parsePlanText(
    [
      "D-18 (Wednesday) — Power Transfer Touch",
      "Why: one meaningful strength touch early in the bridge window.",
      "- Movement prep (4 min): ankle/hip swings, 2 min easy shadow jab-cross with rhythm.",
      "- Band-Resisted Jab-Cross Primer — 3 x 4-6 reps per side; full recovery 90-120 s; intensity: RPE 6-7.",
      "Purpose: preserve punch speed and transfer strength with minimal metabolic cost.",
      "Why today: one meaningful strength touch early in the bridge window.",
      "Progression/regression/stop: reduce band tension if technique breaks.",
      "- Reset (2-3 min): slow mobility flow for hips and thoracic rotation.",
    ].join("\n"),
  );

  const session = groups[0];
  assert.ok(session.kind === "session");
  assert.equal(session.title, "Power Transfer Touch");
  assert.equal(session.objective, "one meaningful strength touch early in the bridge window.");
  assert.deepEqual(
    session.blocks.map((block) => [block.name, block.dose]),
    [
      [
        "Movement prep (4 min)",
        "ankle/hip swings, 2 min easy shadow jab-cross with rhythm.",
      ],
      [
        "Band-Resisted Jab-Cross Primer",
        "3 x 4-6 reps per side; full recovery 90-120 s",
      ],
      ["Reset (2-3 min)", "slow mobility flow for hips and thoracic rotation."],
    ],
  );
  assert.deepEqual(session.blocks[1].details, [
    { label: "Intensity", text: "RPE 6-7." },
    { label: "Purpose", text: "preserve punch speed and transfer strength with minimal metabolic cost." },
    { label: "Why", text: "one meaningful strength touch early in the bridge window." },
    { label: "Progress", text: "reduce band tension if technique breaks." },
  ]);
});

test("markdown section headers (## Nutrition) become their own context cards", () => {
  const groups = parsePlanText(["## Nutrition", "Eat to support training.", "## Recovery", "Sleep 8h."].join("\n"));

  assert.deepEqual(
    groups.map((group) => [group.kind, group.kind === "notes" ? group.title : null]),
    [
      ["notes", "Nutrition"],
      ["notes", "Recovery"],
    ],
  );
});

test("coach-led session keeps its freshness note instead of a block", () => {
  const groups = parsePlanText(
    [
      "GPP — Week 1 (D-33 to D-27) — Build aerobic base",
      "D-32 (Thursday) — Coach-led boxing session",
      "Coach-led boxing session No app S&C today. Keep freshness priority.",
    ].join("\n"),
  );

  const week = groups[0];
  assert.ok(week.kind === "week");
  const session = week.sessions[0];
  assert.equal(session.title, "Coach-led boxing session");
  assert.equal(session.blocks.length, 0);
  assert.match(session.coachNote ?? "", /No app S&C today/);
});

test("rehab bullet items parse into clean blocks tagged by their sub-heading", () => {
  const groups = parsePlanText(
    [
      "D-44 (Friday) — Soft work",
      "Rehab",
      "• Soft-tissue ball on anterior/lateral deltoid — 2 x 60s (gentle)",
      "Purpose: local tissue desensitisation for the bruise area.",
      "• Banded IR/ER light pulses — 2 x 10 each side, very light band",
      "Purpose: reintroduce gentle rotator control with minimal load.",
    ].join("\n"),
  );

  const session = groups[0];
  assert.ok(session.kind === "session");
  // The bare "Rehab" sub-heading becomes a block tag, never a stray note.
  assert.ok(!session.notes.includes("Rehab"));
  assert.deepEqual(
    session.blocks.map((block) => [block.name, block.dose, block.tag]),
    [
      ["Soft-tissue ball on anterior/lateral deltoid", "2 x 60s (gentle)", "Rehab"],
      ["Banded IR/ER light pulses", "2 x 10 each side, very light band", "Rehab"],
    ],
  );
  // The "•" glyph never leaks into an exercise title.
  assert.ok(session.blocks.every((block) => !block.name.includes("•")));
  // Labelled detail still attaches to its block.
  assert.deepEqual(session.blocks[0].details, [
    { label: "Purpose", text: "local tissue desensitisation for the bruise area." },
  ]);
});

test("a block-group sub-heading tags only its own session and does not bleed into the next", () => {
  const groups = parsePlanText(
    [
      "D-43 (Saturday) — Easy day",
      "Mobility",
      "• Thoracic opener — 2 x 8",
      "D-42 (Sunday) — Bike",
      "Easy Assault Bike — 20 min Zone 2",
    ].join("\n"),
  );

  const [first, second] = groups;
  assert.ok(first.kind === "session" && second.kind === "session");
  assert.equal(first.blocks[0].name, "Thoracic opener");
  assert.equal(first.blocks[0].tag, "Mobility");
  // The next session starts with no inherited tag.
  assert.equal(second.blocks[0].name, "Easy Assault Bike");
  assert.equal(second.blocks[0].tag, null);
});

test("missing saved structure is adapted into the full structured renderer contract", () => {
  const plan = buildStructuredPlanFromText(
    [
      "Lead notes",
      "- Protect freshness through the bridge window.",
      "",
      "TAPER - Week 1 (D-21 to D-15) - Sharpen",
      "D-21 (Thursday) - Power Transfer Touch",
      "Why: preserve punch speed without soreness.",
      "Band-Resisted Jab-Cross Primer - 4 x 4 reps; RPE 7.",
      "Purpose: transfer force into the jab-cross. Progress: add one set. Stop: stop if technique breaks.",
    ].join("\n"),
    "2026-07-23",
  );

  assert.equal(plan.schema_version, "text-adapter.v1");
  assert.equal(plan.raw_markdown_fallback?.includes("Power Transfer Touch"), true);
  assert.equal(plan.plan_notes?.[0]?.text, "Protect freshness through the bridge window.");
  assert.equal(plan.weeks?.length, 1);

  const week = plan.weeks?.[0];
  assert.equal(week?.phase_label, "TAPER");
  assert.equal(week?.week_index, 1);
  assert.equal(week?.week_goal, "Sharpen");
  assert.equal(week?.days?.[0]?.date, "2026-07-02");
  assert.equal(week?.days?.[0]?.countdown_label, "D-21");

  const session = week?.days?.[0]?.sessions?.[0];
  assert.equal(session?.title, "Power Transfer Touch");
  assert.equal(session?.objective, "preserve punch speed without soreness.");
  assert.equal(session?.session_type, "strength_power");
  assert.equal(session?.blocks?.[0]?.display_name, "Band-Resisted Jab-Cross Primer");
  assert.equal(session?.blocks?.[0]?.load?.display, "4 x 4 reps; RPE 7.");
  assert.equal(session?.blocks?.[0]?.progression_rule, "add one set.");
  // Purpose stays a labelled coaching cue rather than collapsing into an
  // unlabelled `purpose` paragraph, so the fallback renders the same shape a
  // real structured card does.
  assert.equal(session?.blocks?.[0]?.purpose, null);
  assert.deepEqual(session?.blocks?.[0]?.coaching_cues, [
    "Purpose: transfer force into the jab-cross.",
    "Stop: stop if technique breaks.",
  ]);
});

test("a short-camp rehab line keeps its exercise name and both labelled rationales", () => {
  // Real short-camp (D-3) output. Two defects used to land here together: the
  // "Rehab -" group prefix was taken as the exercise NAME (burying the real
  // drill at the head of the dose), and Purpose + Why today were joined into a
  // single unlabelled paragraph.
  const plan = buildStructuredPlanFromText(
    [
      "D-3 (Monday): Freshness Reset",
      "Why: preserve mobility and timing without creating fatigue.",
      "- Movement prep - 5 minutes total. Arm swings 60 sec. RPE 1-2.",
      "  Purpose: re-establish shoulder-friendly movement.",
      "  Why today: pre-session prep for coach-led technical work.",
      "- Rehab - YTW Raise Sequence (light DBs) - 2 sets x 8 reps per letter, light DBs (2-4 kg).",
      "  Purpose: reinforce scapular upward rotation.",
      "  Why today: pre-activity activation.",
    ].join("\n"),
  );

  const blocks = plan.weeks?.[0]?.days?.[0]?.sessions?.[0]?.blocks ?? [];

  // "Movement prep" is also a group label, but here it is the exercise itself —
  // only a label followed by BOTH a name and a dose is treated as a group.
  assert.equal(blocks[0]?.display_name, "Movement prep");
  assert.equal(blocks[0]?.load?.display, "5 minutes total. Arm swings 60 sec. RPE 1-2.");
  assert.deepEqual(blocks[0]?.coaching_cues, [
    "Purpose: re-establish shoulder-friendly movement.",
    "Why today: pre-session prep for coach-led technical work.",
  ]);

  // The inline "Rehab -" prefix is the block's group, not its name.
  assert.equal(blocks[1]?.display_name, "YTW Raise Sequence (light DBs)");
  assert.equal(
    blocks[1]?.load?.display,
    "2 sets x 8 reps per letter, light DBs (2-4 kg).",
  );
  assert.equal(blocks[1]?.block_type, "rehab");
  assert.deepEqual(blocks[1]?.coaching_cues, [
    "Purpose: reinforce scapular upward rotation.",
    "Why today: pre-activity activation.",
  ]);
});

test("late-fight raw output becomes clear notes, contact cards, and exercise cards", () => {
  const plan = buildStructuredPlanFromText(
    [
      "- Injury: Left shoulder skin irritation, moderate and stable. One hygiene note only: keep the area clean and covered during sessions and contact, report any increased redness or bleeding. Train normally around this; no program-level session cuts.",
      "- Missing target weight: no target weight set. Add your fight weight to receive specific cut guidance.",
      "- Sparring note: declared hard-sparring days (Tuesday and Friday) convert to technical-only combat in this window. No hard sparring allowed, keep freshness priority.",
      "- Week shape: this is a bridge compression week. Rendered fewer sessions to preserve sharpness while applying the D-17+ technical-only rule and keeping declared contact work as your responsibility.",
      "",
      "D-12 (Monday) — Neural speed touch",
      "Why: preserve force and rate of force development without creating fatigue.",
      "- Movement Prep: 6 min. Shoulder-safe mobility, band pull-aparts 2 x 10.",
      "- Trap bar deadlift, neural touch: 2-3 sets x 3 reps, RPE 6-7, full recovery 3-4 min.",
      "- Easier: 2 sets x 3 with lighter load.",
      "- Stop: any sharp shoulder pain or new wound bleeding.",
      "",
      "D-11 (Tuesday) — Technical-only combat",
      "Technical-only contact today — no hard sparring and no extra S&C. Keep freshness priority.",
      "",
      "D-11 (Tuesday) — Tactical Focus",
      "Why: keep pocket exchanges planned rather than chaotic.",
      "- Pocket Exchange Map: 10 minutes, tactical review only. No physical load.",
      "  Step 1: Identify the opponent's most common pocket sequence.",
      "  Intent: Win the second decision inside the pocket.",
      "  Focus: Watch the opponent's response after the first two punches.",
      "  Reset: Smother or leave instead of trading blindly.",
      "  Anchor: Know the next beat.",
      "  Purpose: SPP pocket planning for a brawler.",
      "  Progress: Rehearse the chosen exchange ending.",
      "",
      "D-10 (Wednesday) — Light Combat / Technical",
      "Why: keep technical rhythm and timing without adding fatigue.",
      "- Movement Prep: 6 min. Light mobility and gentle shoulder circles.",
      "- Technical shadowboxing: 5 x 2 min rounds, RPE 4-5, 60-90 sec rest.",
      "- Easier: reduce shadowboxing to 3 x 2 min.",
      "- Stop: increase in shoulder irritation or bleeding.",
      "",
      "D-8 (Friday) — Fight-week freshness",
      "Why: preserve readiness and upper-back posture, low noise load.",
      "- Movement Prep: 5 min easy shoulder and thoracic mobility.",
      "- Band face pull, light: 2 sets x 12 reps, RPE 3-4.",
      "- Easier: skip the band and do mobility only.",
      "- Stop: increase in shoulder skin irritation.",
      "",
      "D-5 (Monday) — Fight-Speed Primer",
      "Why: sharpen punch speed and timing without creating soreness.",
      "- Movement Prep: 5 min light shoulder swings and easy shadowboxing.",
      "- Explosive Boxing Burst Intervals: 2-3 x 5-6 sec fast relaxed bursts, RPE 6, full recovery 90-120 sec.",
      "- Easier: 1-2 x 5 sec bursts with full recovery.",
      "- Stop: when technique or speed collapses.",
      "",
      "D-5 (Monday) — Tactical Focus",
      "Why: confirm controlled opening pressure without a reckless start.",
      "- First-Round Pressure Script: 8 minutes, tactical review only. No physical load.",
      "  Step 1: Choose the opening pressure action you trust.",
      "  Intent: Take space calmly.",
      "  Focus: Use only rehearsed pressure actions.",
      "  Reset: Rebuild the ring cut before throwing again.",
      "  Anchor: Close calmly.",
      "  Purpose: Fight-week opening script for a brawler.",
      "  Progress: Rehearse the script once at low intensity.",
      "",
      "D-4 (Tuesday) — Technical-only combat",
      "Technical-only contact today — no hard sparring and no extra S&C. Keep freshness priority.",
      "",
      "D-4 (Tuesday) — Neural Visualization",
      "Quiet visualization only. Rehearse first exchange, best entry, exit/reset, and final-round composure.",
      "- Duration: 5-8 min, RPE 1. Purpose: mental rehearsal of execution and calm under pressure.",
      "- Easier: 3 min focused breathing.",
      "- Stop: switch to breathing-only if visualization increases anxiety.",
      "",
      "D-3 (Wednesday) — Fight-week freshness",
      "Why: low-noise reset and posture work before the final window.",
      "- Movement Prep: 5 min gentle mobility and breathing reset.",
      "- Band pull-apart, low volume: 1-2 sets x 12-15 reps, RPE 2-3.",
      "- Easier: breathing and walking only.",
      "- Stop: any increase in shoulder skin irritation.",
      "",
      "D-3 (Wednesday) — Light Combat / Technical",
      "Why: maintain timing and confidence with minimal physical cost.",
      "- Movement Prep: 4 min light mobility and shadow rhythm.",
      "- Technical shadowboxing tempo: 4 x 2 min, RPE 3-4, 60 sec rest.",
      "- Easier: 2 x 2 min solo shadowboxing.",
      "- Stop: shoulder irritation increases or technique degrades.",
      "",
      "D-1 (Friday) — Technical-only combat",
      "Technical-only contact today — no hard sparring and no extra S&C. Keep freshness priority.",
      "",
      "D-1 (Friday) — Tactical Cue Card",
      "Write one fight cue only: entry, exit, counter, foot position, or guard reaction. Keep it short enough to recall under pressure.",
      "",
      "D-0 (Saturday) — Fight day protocol",
      "Fight day protocol — follow coach warm-up and fight protocol; no additional S&C.",
    ].join("\n"),
    "2026-08-22",
  );

  assert.deepEqual(
    plan.plan_notes?.map((note) => note.label),
    ["Injury", "Missing target weight", "Sparring note", "Week shape"],
  );

  const days = plan.weeks?.[0]?.days ?? [];
  assert.deepEqual(days.map((day) => day.countdown_label), [
    "D-12", "D-11", "D-10", "D-8", "D-5", "D-4", "D-3", "D-1", "D-0",
  ]);
  assert.equal(days[1]?.today_card?.coach_led_contact, "Technical-only contact today — no hard sparring and no extra S&C. Keep freshness priority.");
  assert.equal(days[1]?.sessions?.[0]?.title, "Tactical Focus");
  assert.equal(days[1]?.sessions?.[0]?.blocks?.[0]?.display_name, "Pocket Exchange Map");
  assert.deepEqual(days[1]?.sessions?.[0]?.blocks?.[0]?.coaching_cues, [
    "Purpose: SPP pocket planning for a brawler.",
    "Step 1: Identify the opponent's most common pocket sequence.",
    "Intent: Win the second decision inside the pocket.",
    "Focus: Watch the opponent's response after the first two punches.",
    "Reset: Smother or leave instead of trading blindly.",
    "Anchor: Know the next beat.",
  ]);
  assert.equal(days[1]?.sessions?.[0]?.blocks?.[0]?.progression_rule, "Rehearse the chosen exchange ending.");

  const neuralBlocks = days[0]?.sessions?.[0]?.blocks ?? [];
  assert.equal(neuralBlocks[0]?.display_name, "Movement Prep");
  assert.equal(neuralBlocks[1]?.display_name, "Trap bar deadlift, neural touch");
  assert.equal(neuralBlocks[1]?.load?.display, "2-3 sets x 3 reps, RPE 6-7, full recovery 3-4 min.");
  assert.deepEqual(neuralBlocks[1]?.regression_options, ["2 sets x 3 with lighter load."]);
  assert.equal(neuralBlocks[1]?.coaching_cues?.[0], "Stop: any sharp shoulder pain or new wound bleeding.");

  assert.deepEqual(days[4]?.sessions?.map((session) => session.title), ["Fight-Speed Primer", "Tactical Focus"]);
  assert.deepEqual(days[6]?.sessions?.map((session) => session.title), ["Fight-week freshness", "Light Combat / Technical"]);
  assert.equal(days[7]?.today_card?.coach_led_contact?.startsWith("Technical-only contact today"), true);
  assert.equal(days[7]?.sessions?.[0]?.title, "Tactical Cue Card");
  assert.equal(days[8]?.sessions?.[0]?.title, "Fight day protocol");
  assert.equal(days[8]?.sessions?.[0]?.blocks?.[0]?.display_name, "Fight day protocol");
});

test("keeps a two-day sparring note when only Tuesday has a dated contact card", () => {
  const plan = buildStructuredPlanFromText(
    [
      "- Sparring note: declared hard-sparring days (Tuesday and Friday) convert to technical-only combat.",
      "D-11 (Tuesday) — Technical-only combat",
      "Technical-only contact today — no hard sparring and no extra S&C. Keep freshness priority.",
    ].join("\n"),
  );

  assert.equal(plan.plan_notes?.[0]?.label, "Sparring note");
  assert.equal(plan.plan_notes?.[0]?.text?.includes("Tuesday and Friday"), true);
});

test("does not swallow dashed exercise prose that mentions no extra S&C", () => {
  const plan = buildStructuredPlanFromText(
    [
      "D-10 (Wednesday) — Recovery reset",
      "Recovery reset — no extra S&C after this drill.",
    ].join("\n"),
  );

  const day = plan.weeks?.[0]?.days?.[0];
  assert.equal(day?.today_card, null);
  assert.equal(day?.sessions?.[0]?.blocks?.[0]?.display_name, "Recovery reset");
});

test("a Tactical Focus day renders as one drill block, not one block per step", () => {
  // Regression for the shredded Tactical Watch card. The watch used to ship its
  // own layout — a bare drill-name line, `Duration:` / `Prescription:` headers,
  // then one bullet per instruction — and every one of those peer-level lines
  // parsed as a separate exercise: the name, duration and the bare
  // "Prescription:" header were glued into the session objective, each
  // instruction became its own load-less block, and the whole mindset stack hung
  // off the last one. fightcamp/tactical_watch_library.build_watch_display_text
  // now emits the shared session-body contract this asserts.
  const plan = buildStructuredPlanFromText(
    [
      "D-11 (Tuesday) — Tactical Focus",
      "Why: Know what happens after the first punches so pocket exchanges stay planned rather than chaotic.",
      "- Pocket Exchange Map: 10 minutes, tactical review only. No physical load.",
      "  Step 1: Identify the opponent's most common pocket sequence.",
      "  Step 2: Choose your answer to that sequence.",
      "  Step 3: Choose the finishing shot that best fits the opening.",
      "  Step 4: Decide whether that exchange should end with an exit or a smother.",
      "  Intent: Win the second decision inside the pocket.",
      "  Focus: Watch the opponent's response after the first two punches.",
      "  Reset: If the exchange loses shape, smother or leave instead of trading blindly.",
      "  Anchor: Know the next beat.",
      "  Purpose: SPP pocket planning for a brawler.",
      "  Progress: Rehearse the chosen exchange ending, not just the opening combination.",
    ].join("\n"),
  );

  const session = plan.weeks?.[0]?.days?.[0]?.sessions?.[0];
  assert.equal(session?.title, "Tactical Focus");
  // The objective is the day's own Why — never the drill name or its duration.
  assert.equal(
    session?.objective,
    "Know what happens after the first punches so pocket exchanges stay planned rather than chaotic.",
  );

  const blocks = session?.blocks ?? [];
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0]?.display_name, "Pocket Exchange Map");
  assert.equal(blocks[0]?.load?.display, "10 minutes, tactical review only. No physical load.");
  // A 10-minute tactical review is skill work, not the generic "training" chip.
  assert.equal(blocks[0]?.block_type, "skill");
  assert.equal(session?.session_type, "skill");
  assert.equal(
    blocks[0]?.progression_rule,
    "Rehearse the chosen exchange ending, not just the opening combination.",
  );
  assert.deepEqual(blocks[0]?.coaching_cues, [
    "Purpose: SPP pocket planning for a brawler.",
    "Step 1: Identify the opponent's most common pocket sequence.",
    "Step 2: Choose your answer to that sequence.",
    "Step 3: Choose the finishing shot that best fits the opening.",
    "Step 4: Decide whether that exchange should end with an exit or a smother.",
    "Intent: Win the second decision inside the pocket.",
    "Focus: Watch the opponent's response after the first two punches.",
    "Reset: If the exchange loses shape, smother or leave instead of trading blindly.",
    "Anchor: Know the next beat.",
  ]);
});

test("open-plan text uses its explicit weekday rhythm instead of an unavailable legacy card", () => {
  const plan = buildStructuredPlanFromText(
    [
      "Weekly Rhythm",
      "- Monday - Support Strength (programmed)",
      "- Wednesday - Coach-led boxing (coach-owned)",
      "  Coach-owned combat session. Keep freshness priority.",
      "- Friday - Coach-led boxing (coach-owned)",
      "  Coach-owned combat session. Keep freshness priority.",
      "- Saturday - Power & Coordination (programmed)",
      "- Tuesday - Optional technical/light shadow session (athlete/coach choice).",
      "",
      "Session Cards",
      "(Format per session: Objective - Main work - Coach note)",
      "Monday - Support Strength",
      "Why: build posterior-chain strength.",
      "- Main work: Trap Bar Deadlift - 4 x 5 @ RPE 7",
      "Wednesday - Coach-led boxing - hard sparring",
      "Coach-owned combat session. Keep freshness priority.",
      "Friday - Coach-led boxing - hard sparring",
      "Coach-owned combat session. Keep freshness priority.",
      "Saturday - Power & Coordination",
      "Why: add low-damage power.",
      "- Main work: Medicine Ball Scoop Toss - 4 x 4",
      "",
      "4-Week Development Block",
      "Week 1 - Baseline and technical consistency",
      "Week 2 - Small progression",
      "Week 3 - Highest controlled week",
      "Week 4 - Deload and reassess",
    ].join("\n"),
  );

  assert.equal(plan.plan_metadata?.plan_type, "open_ongoing_system");
  assert.equal(plan.weeks?.length, 4);
  for (const week of plan.weeks ?? []) {
    assert.deepEqual(
      week.days?.map((day) => day.weekday),
      ["Mon", "Tue", "Wed", "Fri", "Sat"],
    );
    assert.ok(week.days?.every((day) => !day.countdown_label));
  }
  assert.equal(plan.weeks?.[0]?.days?.[0]?.sessions?.[0]?.title, "Support Strength");
  assert.equal(
    plan.weeks?.[0]?.days?.[1]?.sessions?.[0]?.title,
    "Optional technical/light shadow session (athlete/coach choice).",
  );
  assert.deepEqual(plan.weeks?.[0]?.days?.[2]?.sessions, []);
  assert.equal(plan.weeks?.[0]?.days?.[2]?.today_card?.headline, "Coach-led boxing - hard sparring");
});

test("open-plan system sections route to structured homes instead of note dumps", () => {
  const plan = buildStructuredPlanFromText(
    [
      "Immediate Coach Summary",
      "Plan: 4 sessions/week. Two coach-led boxing days (Wednesday, Friday).",
      "Current Training Rules",
      "Weekly volume: 4 visible sessions. Do not add more programmed sessions.",
      "Weekly Rhythm",
      "Monday - Support Strength (programmed)",
      "Wednesday - Coach-led boxing (coach-owned)",
      "Coach-owned combat session. Keep freshness priority.",
      "Session Cards",
      "Monday - Support Strength",
      "Why: build posterior-chain strength.",
      "- Main work: Trap Bar Deadlift - 4 x 5 @ RPE 7",
      "4-Week Development Block",
      "Week 1 - Baseline and technical consistency",
      "Week 2 - Small progression",
      "Increase either 1 set on anchor or increase contrast intent.",
      "Week 3 - Highest controlled week",
      "Week 4 - Deload and reassess",
      "Reduce programmed session volume 30-40%; keep intensity sharp but short.",
      "Progression Rules",
      "Anchor progression: add volume by +1 set only when quality is kept.",
      "Adjustment Rules",
      "If symptoms or fatigue rise, remove optional conditioning first.",
      "Red-flag triggers (stop and report): new sharp joint pain >3/10, dizziness, or persistent sharp shoulder pain after 24h. Stop training and contact coach/medical if these occur. No rehab headings will be used; support work appears as Activation or Mobility in sessions.",
      "4-Week Reassessment Gate",
      "Reassess at the end of each 4-week block.",
      "End notes (coach-facing)",
      "Preserve the two coach-led sparring days as gym-owned.",
    ].join("\n"),
  );

  assert.equal(plan.plan_metadata?.plan_type, "open_ongoing_system");
  // The system/rule sections never surface as active notes.
  assert.deepEqual(plan.plan_notes, []);
  assert.equal(
    plan.progression_notes,
    "Anchor progression: add volume by +1 set only when quality is kept.",
  );
  assert.equal(plan.red_flag_rules?.length, 1);
  const flag = plan.red_flag_rules?.[0]?.display_text ?? "";
  assert.equal(flag.startsWith("new sharp joint pain >3/10"), true);
  assert.equal(flag.includes("Stop training and contact coach/medical"), true);
  assert.equal(flag.includes("No rehab headings"), false);
});

test("legacy run-on open-plan prose still yields a red-flag rule and no note dump", () => {
  const plan = buildStructuredPlanFromText(
    [
      "Weekly Rhythm",
      "Monday - Support Strength (programmed)",
      "Session Cards",
      "Monday - Support Strength",
      "- Main work: Trap Bar Deadlift - 4 x 5",
      "4-Week Development Block",
      "Week 1 - Baseline",
      "Adjustment Rules If symptoms rise remove conditioning first. Red-flag triggers (stop and report): dizziness or sharp joint pain. Stop training and contact coach if these occur. No rehab headings will be used. 4-Week Reassessment Gate Reassess at the end of each block.",
    ].join("\n"),
  );

  assert.equal(plan.plan_metadata?.plan_type, "open_ongoing_system");
  assert.deepEqual(plan.plan_notes, []);
  assert.equal(
    plan.red_flag_rules?.[0]?.display_text,
    "dizziness or sharp joint pain. Stop training and contact coach if these occur.",
  );
});

test("fallback week goals omit duplicated week and countdown metadata", () => {
  const explicit = buildStructuredPlanFromText(
    [
      "GPP — Week 1 (D-53 to D-47) — Restore structural tolerance and rhythm",
      "D-53 (Monday) — Aerobic support",
    ].join("\n"),
  );
  assert.equal(explicit.weeks?.[0]?.week_goal, "Restore structural tolerance and rhythm");

  const synthetic = buildStructuredPlanFromText("D-20 (Tuesday) — Conditioning");
  assert.equal(synthetic.plan_metadata?.plan_type, "fight_camp");
  assert.equal(synthetic.weeks?.length, 1);
  assert.equal(synthetic.weeks?.[0]?.week_index, 1);
  assert.equal(synthetic.weeks?.[0]?.week_goal, null);
});

test("session-type inference keeps 'Combat conditioning' as conditioning and 'Technical-only combat' as skill", () => {
  const plan = buildStructuredPlanFromText(
    [
      "SPP - Week 1 (D-30 to D-24) - Build",
      "D-30 (Monday) - Combat conditioning",
      "Why: raise repeatable output.",
      "Assault Bike Intervals - 6 x 40s; RPE 8.",
      "D-28 (Wednesday) - Technical-only combat",
      "Why: sharpen timing.",
      "Tactical Cue-Card Review - 3 x 2 min.",
    ].join("\n"),
    "2026-07-23",
  );

  const days = plan.weeks?.[0]?.days ?? [];
  const combatConditioning = days.find((d) => d.sessions?.[0]?.title === "Combat conditioning");
  const technicalCombat = days.find((d) => d.sessions?.[0]?.title === "Technical-only combat");

  // Regression: the generic word "combat" must not force a conditioning session
  // into the skill bucket.
  assert.equal(combatConditioning?.sessions?.[0]?.session_type, "conditioning");
  // "Technical-only combat" still classifies as a skill session (via "technical").
  assert.equal(technicalCombat?.sessions?.[0]?.session_type, "skill");
});

test("coach-only plan text remains a visible enhanced day card", () => {
  const plan = buildStructuredPlanFromText(
    [
      "SPP - Week 2",
      "D-16 (Tuesday) - Coach-led boxing - technical only",
      "No app S&C today. Keep freshness priority.",
    ].join("\n"),
    "2026-07-23",
  );

  const day = plan.weeks?.[0]?.days?.[0];
  assert.equal(day?.date, "2026-07-07");
  assert.equal(day?.today_card?.headline, "Coach-led boxing - technical only");
  assert.deepEqual(day?.sessions, []);
});

test("the no-payload branch mounts StructuredPlanRenderer instead of legacy cards", () => {
  const adapterComponent = PLAN_VIEWER_SOURCE.slice(
    PLAN_VIEWER_SOURCE.indexOf("function TextStructuredPlanRenderer"),
    PLAN_VIEWER_SOURCE.indexOf("export type StructuredCardDebug"),
  );

  assert.equal(adapterComponent.includes("<StructuredPlanRenderer"), true);
  assert.equal(adapterComponent.includes("legacy-plan-root"), false);
  assert.equal(adapterComponent.includes("legacy-plan-card-stack"), false);
});

test("projection failure preserves a non-empty saved structured card", () => {
  const plan = {
    outputs: {
      plan_text: "## Raw fallback",
      structured_plan: {
        schema_version: "1.0",
        plan_metadata: { title: "Open plan", sport: "boxing", plan_type: "fight_camp" },
        weeks: [{ week_id: "w1", week_index: 1, days: [{ weekday: "Tue" }] }],
      },
    },
    schedule_context: {
      schedule_mode: "open_recurring",
      projection_status: "unavailable",
      projection_reason: "hard_day_not_coach_led",
    },
  } as unknown as PlanDetail;

  assert.equal(shouldUseSavedStructuredPlan(plan), true);
});

test("projection failure keeps the text-adapter fallback for legacy empty week shells", () => {
  const plan = {
    fight_date: null,
    outputs: {
      plan_text: "## Weekly Rhythm\nTuesday — strength",
      structured_plan: {
        schema_version: "1.0",
        plan_metadata: { title: "Open plan", sport: "boxing", plan_type: "fight_camp" },
        weeks: [
          { week_id: "w1", week_index: 1, days: [] },
          { week_id: "w2", week_index: 2, days: [] },
          { week_id: "w3", week_index: 3, days: [] },
          { week_id: "w4", week_index: 4, days: [] },
        ],
      },
    },
    schedule_context: {
      schedule_mode: "open_recurring",
      projection_status: "unavailable",
      projection_reason: "day_shape_unresolved",
    },
  } as unknown as PlanDetail;

  assert.equal(shouldUseSavedStructuredPlan(plan), false);
});
