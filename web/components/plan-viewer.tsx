"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import {
  adminArchivePlan,
  adminPermanentlyDeletePlan,
  approveAndResumeGeneration,
  approvePlanForRelease,
  archivePlan,
  getActivePlan,
  getPlan,
  getPlanCompletions,
  getToday,
  isRetryableApiFailure,
  rebuildStructuredPlan,
  rejectApprovedPlan,
  renamePlan,
  setActivePlan,
  submitManualStage2,
} from "@/lib/api";
import { requestXpRefresh } from "@/lib/xp-events";
import { SHARED_READ_MAX_AGE_MS } from "@/lib/shared-reads";
import {
  type ActivePlanOverlapAction,
  canSetActivePlan,
  isCompletedFightCamp,
  isActivePlanOverlapError,
  isArchivedPlan,
} from "@/lib/plan-active";
import { clearCompletedGenerationForDeletedPlan } from "@/lib/completed-generation";
import { PlanSwitchDialog } from "@/components/plan-switch-dialog";
import { PremiumLoadingScreen } from "@/components/premium-loading-screen";
import { useToast } from "@/components/toast-provider";
import {
  getPushOptInState,
  subscribeToPushNotifications,
  type PushOptInState,
} from "@/lib/push";
import { QuickBuildRefinementBanner } from "@/components/quick-build-refinement-banner";
import { ContextualFeedback } from "@/components/feedback/contextual-feedback";
import { Skeleton } from "@/components/skeleton";
import { StructuredPlanRenderer } from "@/components/structured-plan-renderer";
import { ExerciseCustomisationProvider, type ExerciseCustomisation } from "@/components/exercise-customisation";
import { patchPlanSession } from "@/lib/exercise-customisation";
import { WhyTooltip } from "@/components/why-tooltip";
import { useGenerationController } from "@/lib/generation-controller";
import { canUseAdminPlanControls, isAdminRole } from "@/lib/plan-admin-controls";
import { STAGE2_HARD_BLOCKER_CODE_SET, STAGE2_STAGE1_FALLBACK_STATUS } from "@/lib/stage2-policy";
import {
  buildStructuredPlanFromText,
  humanizeStatus,
  titleizeToken,
} from "@/lib/plan-text-adapter";
import { athleteFacingSessionTitle } from "@/lib/plan-labels";
import { shouldRenderStructuredPlan } from "@/lib/structured-plan";
import { explainRiskBand } from "@/lib/sparring-reason-codes";
import {
  formatAthletePlanStatus,
  formatPlanFightDate,
  formatPlanStatus,
  formatPlanTimestamp,
  getPlanDisplayName,
  isOpenOngoingPlan,
  resolveFiniteWeekNumber,
} from "@/lib/plan-format";
import { describeRelativeDay, formatAppDate } from "@/lib/date-format";
import { contactTimerTarget, contactTitle } from "@/lib/session-timer/contact";
import {
  buildBlockedInjuryContextSummary,
  readGuidedInjurySummary,
  buildBlockedWhy,
  type BlockedInjuryContextSummary,
} from "@/lib/triage-block-reasons";
import type {
  PlanAdvisory,
  PlanDetail,
  PlanCompletionsResponse,
  StructuredBlock,
  StructuredCardLifecycleState,
  StructuredCardState,
  StructuredDay,
  StructuredPlan,
  StructuredSession,
  StructuredWeek,
  TodaySession,
  UserRole,
} from "@/lib/types";
import {
  PROFILE_REFRESH_FAILED_ATHLETE_NOTICE,
  planHasProfileRefreshFailed,
} from "@/lib/profile-refresh-warning";
import { hasTriageResumeApproval, shouldShowTriageBlockedState } from "@/lib/triage-view";


// Re-exported from the extracted adapter so existing imports keep working.
export { buildStructuredPlanFromText, parsePlanText, splitLabeledSegments } from "@/lib/plan-text-adapter";
export type {
  PlanTextBlock,
  PlanTextDetail,
  PlanTextGroup,
  PlanTextNotes,
  PlanTextSession,
  PlanTextWeek,
} from "@/lib/plan-text-adapter";

const TRIAGE_RESUME_FETCH_ATTEMPTS = 5;
const TRIAGE_RESUME_FETCH_DELAY_MS = 800;
const APPROVE_RECOVERY_FETCH_ATTEMPTS = 3;
const APPROVE_RECOVERY_FETCH_DELAY_MS = 800;
const STRUCTURED_PLAN_POLL_INTERVAL_MS = 2500;
// Longest the upgrade poll waits on one plan fetch before moving on. The API
// layer's own timeout can be minutes (retries) or disabled, so the poll bounds it.
const STRUCTURED_PLAN_POLL_FETCH_TIMEOUT_MS = 15_000;
// Background structuring can take up to ~2 minutes for a full camp. We never make
// the athlete wait for it: plan_text is deterministically adapted into the full
// structured renderer immediately. Polling only swaps in the richer saved payload
// when it lands. The window covers the backend conversion timeout plus a buffer.
const STRUCTURED_PLAN_UPGRADE_POLL_WINDOW_MS = 220_000;
const STRUCTURED_PLAN_RECENT_PLAN_THRESHOLD_MS = 5 * 60_000;

const ATHLETE_VISIBLE_STATUSES = new Set(["ready", "publishable_with_flags"]);

/**
 * A plan counts as released to the athlete once it lands in an athlete-visible
 * status with non-empty plan_text. Used both to render the published state and
 * to confirm an approval that may have completed server-side after a network
 * timeout on the approve request.
 */
export function isPlanReleasedToAthlete(
  plan: Pick<PlanDetail, "status" | "outputs"> & Partial<Pick<PlanDetail, "custom_plan_id" | "structured_card_state">>,
): boolean {
  const status = (plan.status || "").trim().toLowerCase();
  const customCard = Boolean(plan.custom_plan_id && plan.structured_card_state?.state === "live"
    && plan.outputs.structured_plan?.plan_metadata?.prescription_source === "athlete");
  return (ATHLETE_VISIBLE_STATUSES.has(status) || (status === "archived" && customCard))
    && (Boolean(plan.outputs.plan_text.trim()) || customCard);
}

/**
 * A saved structured card remains authoritative when only its calendar
 * projection fails, unless it is one of the legacy week-shell payloads with no
 * day rows. Those shells contain no usable session detail, so keep the existing
 * deterministic text-adapter fallback for them.
 */
export function shouldUseSavedStructuredPlan(
  plan: Pick<PlanDetail, "outputs" | "fight_date" | "schedule_context">,
): boolean {
  const structuredPlan = plan.outputs.structured_plan;
  if (!shouldRenderStructuredPlan(plan.outputs) || !structuredPlan) {
    return false;
  }
  const weeks = Array.isArray(structuredPlan.weeks) ? structuredPlan.weeks : [];
  const hasDayRows = weeks.some(
    (week) => Array.isArray(week.days) && week.days.length > 0,
  );
  return !(
    isOpenOngoingPlan(plan.fight_date) &&
    plan.schedule_context?.projection_status === "unavailable" &&
    !hasDayRows
  );
}

export function canShowContextualPlanFeedback(
  viewerRole: UserRole,
  viewerProfileId: string | null,
  planAthleteId: string | null | undefined,
): boolean {
  if (viewerRole === "athlete") return true;
  return Boolean(
    viewerRole === "admin" &&
    viewerProfileId &&
    planAthleteId &&
    viewerProfileId === planAthleteId,
  );
}

/**
 * Decide whether a freshly published plan is still expecting its richer saved
 * structured payload to land in the background. The enhanced renderer is already
 * active from plan_text; polling only adds server-derived metadata when available.
 * We only await an upgrade for plans that can still produce
 * one. We never await for:
 *  - legacy/old plans (created outside the recent-plan window), which may simply
 *    never have a structured_plan,
 *  - plans without an access token (we cannot poll for the structured card),
 *  - triage-blocked plans,
 *  - plans whose background poll window has already elapsed,
 *  - plans that already have a structured_plan.
 */
export function shouldAwaitStructuredPlanUpgrade(params: {
  hasPublishedPlan: boolean;
  hasStructuredPlan: boolean;
  pollWindowExpired: boolean;
  hasAccessToken: boolean;
  isRecentPlan: boolean;
  isTriageBlocked?: boolean;
}): boolean {
  return (
    params.hasPublishedPlan &&
    !params.hasStructuredPlan &&
    !params.pollWindowExpired &&
    params.hasAccessToken &&
    params.isRecentPlan &&
    params.isTriageBlocked !== true
  );
}

/**
 * Whether the athlete-facing view should hold back the deterministic plan
 * fallback and show the "camp is being lxcked in" waiting card instead, so the
 * first plan an athlete ever sees is the enhanced card.
 *
 * The hold only applies while the richer payload can still land: it needs the
 * same await conditions as the background upgrade (recent plan, open poll
 * window, access token, published, not triage-blocked) AND a card lifecycle
 * that has not already terminally failed. Failed / not-attempted / lost-card
 * plans fall back to the deterministic renderer immediately, and the
 * mount-scoped poll window bounds the hold even if a build hangs. Admins are
 * never held — they keep the text view plus diagnostics for review.
 */
export function shouldHoldPlanForEnhancedCard(params: {
  isViewerAdmin: boolean;
  structuredCardLifecycleState: StructuredCardLifecycleState;
  hasPublishedPlan: boolean;
  hasStructuredPlan: boolean;
  pollWindowExpired: boolean;
  hasAccessToken: boolean;
  isRecentPlan: boolean;
  isTriageBlocked?: boolean;
}): boolean {
  if (params.isViewerAdmin) {
    return false;
  }
  // "none" covers the moment right after publish before the lifecycle record
  // lands; "building" is an active server-side conversion. Every other state
  // means no richer payload is coming, so the fallback must show.
  if (
    params.structuredCardLifecycleState !== "building" &&
    params.structuredCardLifecycleState !== "none"
  ) {
    return false;
  }
  return shouldAwaitStructuredPlanUpgrade(params);
}

/**
 * A plan is "recent" if it was created within the recent-plan window. Used to
 * avoid holding the structured-card finalising state for legacy plans that were
 * created long before structured plans existed (or never produced one).
 */
export function isRecentlyCreatedPlan(
  plan: Pick<PlanDetail, "created_at">,
  now: number = Date.now(),
): boolean {
  const createdAt = Date.parse(plan.created_at || "");
  if (Number.isNaN(createdAt)) {
    return false;
  }
  return now - createdAt <= STRUCTURED_PLAN_RECENT_PLAN_THRESHOLD_MS;
}

/**
 * Whether the background upgrade poll should run for this plan.
 *
 * This is deliberately NOT gated on plan recency. A published plan still missing
 * its structured card keeps trying to pick one up whenever its view is open. An
 * admin-held plan also polls while its authoritative server state says building,
 * so retry progress cannot freeze until reload. The mount-scoped poll window
 * still bounds the cost, and triage-blocked plans remain excluded.
 */
export function shouldPollForStructuredPlanUpgrade(params: {
  hasPublishedPlan: boolean;
  hasStructuredPlan: boolean;
  pollWindowExpired: boolean;
  hasAccessToken: boolean;
  isServerBuilding?: boolean;
  isTriageBlocked?: boolean;
}): boolean {
  return (
    (params.hasPublishedPlan || params.isServerBuilding === true) &&
    !params.hasStructuredPlan &&
    !params.pollWindowExpired &&
    params.hasAccessToken &&
    params.isTriageBlocked !== true
  );
}

type VisibilityDocument = Pick<
  Document,
  "visibilityState" | "addEventListener" | "removeEventListener"
>;

/**
 * Run the background upgrade poll until the returned stop function is called.
 *
 * Every tick fetches the whole plan, so a tick is skipped while the previous
 * fetch is still out (slow responses must not stack up) and while the tab is
 * hidden (nobody is there to see the swap). Returning to the tab checks at
 * once. A window that ends while the tab is hidden stays open until a fetch
 * that started after the return has finished, so an upgrade that landed while
 * the athlete was away still shows. A fetch still out from before the tab was
 * hidden does not count: it may predate the upgrade. ``poll`` handles its own
 * failures.
 *
 * Each fetch gets an abort signal, aborted after ``fetchTimeoutMs`` and when
 * the poll stops: a request that never settles would otherwise hold every
 * later tick and keep the window open forever. ``poll`` must settle once its
 * signal aborts (a cancelled fetch rejects), and the next fetch only starts
 * after that, so fetches never overlap and an abandoned request cannot apply a
 * late result. The window closing stops the poll straight away.
 */
export function startStructuredPlanUpgradePoll(params: {
  poll: (signal: AbortSignal) => Promise<void>;
  onWindowExpired: () => void;
  intervalMs: number;
  windowMs: number;
  fetchTimeoutMs: number;
  doc?: VisibilityDocument;
}): () => void {
  const doc = params.doc ?? document;
  let stopped = false;
  let inFlight: Promise<void> | null = null;
  let inFlightController: AbortController | null = null;
  let expireWhenVisible = false;
  const isVisible = () => doc.visibilityState === "visible";
  const startFetch = (): Promise<void> => {
    const controller = new AbortController();
    const deadlineId = setTimeout(() => controller.abort(), params.fetchTimeoutMs);
    const request = params
      .poll(controller.signal)
      .catch(() => undefined)
      .finally(() => {
        clearTimeout(deadlineId);
        if (inFlight === request) {
          inFlight = null;
          inFlightController = null;
        }
      });
    inFlight = request;
    inFlightController = controller;
    return request;
  };
  const tick = async () => {
    if (stopped || inFlight || !isVisible()) {
      return;
    }
    await startFetch();
  };
  /** Resolves true once a fetch begun after the return has finished. */
  const checkAfterReturn = async (): Promise<boolean> => {
    const earlier = inFlight;
    if (earlier) {
      await earlier;
    }
    if (stopped || !isVisible()) {
      return false;
    }
    // A fetch started since the earlier one settled began after the return.
    await (inFlight ?? startFetch());
    // Hidden again before it finished: the next return's check closes the window.
    return !stopped && isVisible();
  };
  let returns = 0;
  const handleVisibilityChange = () => {
    if (!isVisible()) {
      return;
    }
    const thisReturn = ++returns;
    void checkAfterReturn().then((checked) => {
      // A later return runs its own check; that one closes the window.
      if (checked && thisReturn === returns && expireWhenVisible) {
        expire();
      }
    });
  };

  doc.addEventListener("visibilitychange", handleVisibilityChange);
  const intervalId = setInterval(() => void tick(), params.intervalMs);
  const timeoutId = setTimeout(() => {
    if (isVisible()) {
      expire();
    } else {
      expireWhenVisible = true;
    }
  }, params.windowMs);

  function stop() {
    stopped = true;
    inFlightController?.abort();
    doc.removeEventListener("visibilitychange", handleVisibilityChange);
    clearInterval(intervalId);
    clearTimeout(timeoutId);
  }

  function expire() {
    if (stopped) {
      return;
    }
    // Stop first: no tick may start between the window closing and the
    // component's own cleanup.
    stop();
    params.onWindowExpired();
  }

  return stop;
}

function getApprovalSuccessMessage(plan: Pick<PlanDetail, "outputs">): string {
  return shouldRenderStructuredPlan(plan.outputs)
    ? "Plan approved and released to the athlete view."
    : "Plan approved and released. The athlete sees their plan now; the full card view follows automatically once it finishes building.";
}

/**
 * Recover from a flaky approve request. The backend commits the approval before
 * any slow post-processing, so a retryable network/timeout error is frequently a
 * false negative — the plan is already released. For those errors only, re-fetch
 * the plan a few times and return it once it reads as released to the athlete.
 * Returns `null` when the error is not retryable or the plan never becomes
 * released within the recovery window, so the caller surfaces the original error.
 */
export async function resolveApprovalAfterError(params: {
  error: unknown;
  fetchPlan: () => Promise<PlanDetail>;
  attempts?: number;
  wait?: (attempt: number) => Promise<void>;
}): Promise<PlanDetail | null> {
  if (!isRetryableApiFailure(params.error)) {
    return null;
  }
  const attempts = params.attempts ?? APPROVE_RECOVERY_FETCH_ATTEMPTS;
  const wait =
    params.wait ?? ((attempt: number) => sleep(APPROVE_RECOVERY_FETCH_DELAY_MS * (attempt + 1)));
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const refreshedPlan = await params.fetchPlan();
      if (isPlanReleasedToAthlete(refreshedPlan)) {
        return refreshedPlan;
      }
    } catch {
      // Ignore transient fetch failures during the recovery window.
    }
    if (attempt < attempts - 1) {
      await wait(attempt);
    }
  }
  return null;
}

async function sleep(ms: number): Promise<void> {
  await new Promise((resolve) => globalThis.setTimeout(resolve, ms));
}

type ValidatorIssue = Record<string, unknown>;
type ReviewIssue = {
  code: string;
  title: string;
  message: string;
  severity: "error" | "warning";
  context?: string;
  snippet?: string;
};

type InjuryTriageView = {
  mode?: string;
  reasons: string[];
  red_flags: string[];
  matched_high_risk_categories: string[];
  routing_reasons: string[];
  urgent_flags: string[];
  sparring_risk_band?: string;
  clinician_clearance_required?: boolean;
};

const FRACTURE_CATEGORY_SET = new Set([
  "fracture",
  "stress_fracture",
  "hairline_fracture",
  "rib_fracture",
  "broken_rib",
]);

type RiskBandTone = "green" | "amber" | "red" | "black";

const BLOCKING_WARNING_CODES = STAGE2_HARD_BLOCKER_CODE_SET;
const NON_PUBLISHABLE_STAGE2_STATUSES = new Set([
  "triage_blocked",
  "triage_resume_approved",
  "medical_hold",
  "restricted_rehab_only",
]);
// STAGE2_STAGE1_FALLBACK_STATUS (shared/stage2-policy.json) is deliberately NOT
// in the set above: such a plan IS released to the athlete.
const TRIAGE_BLOCKED_STUB_MARKERS = [
  "## Injury Triage: Restricted Rehab Only",
  "Normal fight-camp planning is intentionally suspended",
  "Clinician clearance is required",
];


const ISSUE_TITLES: Record<string, string> = {
  restriction_violation: "Restriction violation",
  missing_required_element: "Missing phase-critical element",
  phase_section_missing: "Missing phase section",
  weak_anchor_session: "Weak anchor session",
  support_takeover_before_anchor: "Support work took over too early",
  conditional_conditioning_choice: "Conditioning is still unresolved",
  too_many_fallbacks: "Too many fallback branches",
  unresolved_access_fallback: "Fallback does not match real access needs",
  template_like_session_render: "Session still reads like a template",
  taper_option_overload: "Taper is too noisy",
  equipment_incongruent_selection: "Equipment mismatch",
  missing_week_session_role: "Week structure is missing a session",
  late_camp_session_incomplete: "Late-camp week is incomplete",
  weekly_session_overage: "Too many sessions in a week",
  weekly_rhythm_broken: "Weekly rhythm broke",
  missing_weight_cut_acknowledgement: "Weight-cut stress is missing",
  high_pressure_weight_cut_underaddressed: "High-pressure cut is underaddressed",
  sport_language_leak: "Cross-sport wording leaked in",
  overstyled_drill_name: "Naming still needs cleanup",
  gimmick_name: "Naming still needs cleanup",
};


function formatStructuredValue(value: unknown, fallback: string) {
  if (value == null) {
    return fallback;
  }
  if (typeof value === "string") {
    return value.trim() || fallback;
  }
  if (typeof value === "object") {
    const entries = Array.isArray(value) ? value : Object.keys(value as Record<string, unknown>);
    if (!entries.length) {
      return fallback;
    }
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

function buildArtifactFilename(plan: PlanDetail, suffix: string) {
  const base =
    (plan.full_name || "athlete-plan")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "athlete-plan";
  return `${base}-${suffix}.txt`;
}


function TextStructuredPlanRenderer({
  text,
  fightDate,
  createdAt,
  focusDay,
  currentDayLabel,
  scheduleContext,
  isAdmin = false,
  rehabLabelPolicy,
  exerciseMedia,
}: {
  text: string;
  fightDate?: string | null;
  createdAt?: string | null;
  focusDay?: Date;
  currentDayLabel: string;
  scheduleContext?: PlanDetail["schedule_context"];
  isAdmin?: boolean;
  rehabLabelPolicy?: PlanDetail["rehab_label_policy"] | null;
  exerciseMedia?: PlanDetail["outputs"]["exercise_media"];
}) {
  const adaptedPlan = useMemo(
    () => buildStructuredPlanFromText(text, fightDate),
    [fightDate, text],
  );
  const hasWeekdaySchedule = Boolean(
    adaptedPlan.weeks?.some((week) => week.days?.some((day) => day.weekday)),
  );
  const rendererScheduleContext: PlanDetail["schedule_context"] =
    hasWeekdaySchedule && scheduleContext?.schedule_mode === "open_recurring"
      ? { ...scheduleContext, projection_status: "projected" }
      : scheduleContext;
  return (
    <StructuredPlanRenderer
      plan={adaptedPlan}
      defaultView="calendar"
      openOngoing={isOpenOngoingPlan(fightDate)}
      createdAt={createdAt}
      focusDay={focusDay}
      currentDayLabel={currentDayLabel}
      scheduleContext={rendererScheduleContext}
      isAdmin={isAdmin}
      rehabLabelPolicy={rehabLabelPolicy}
      exerciseMedia={exerciseMedia}
    />
  );
}

/**
 * Athlete-facing hold card shown instead of the deterministic plan fallback
 * while the enhanced card is still building, so the first plan view is always
 * the elite card. The background poll swaps the enhanced card in when it lands.
 *
 * This is also the highest-motivation moment to offer push notifications, so
 * the card carries the opt-in: granted permission here powers both the
 * plan-ready push and the daily morning check-in nudge.
 */
export function EnhancedCardLockInCard({
  accessToken = null,
}: {
  accessToken?: string | null;
}) {
  const [pushState, setPushState] = useState<PushOptInState | "loading" | "enabling">("loading");
  const [pushError, setPushError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getPushOptInState(accessToken)
      .then((state) => {
        if (!cancelled) {
          setPushState(state);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setPushState("unsupported");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [accessToken]);

  async function handleEnableNotifications() {
    if (!accessToken) {
      return;
    }
    setPushState("enabling");
    setPushError(null);
    try {
      await subscribeToPushNotifications(accessToken);
      setPushState("subscribed");
    } catch (error) {
      setPushState("unsubscribed");
      setPushError(
        error instanceof Error ? error.message : "Unable to enable notifications right now.",
      );
    }
  }

  return (
    <section className="support-panel plan-lockin-card" role="status" aria-live="polite">
      <p className="kicker plan-lockin-kicker">Final review</p>
      <h3 className="plan-lockin-title">
        YOUR CAMP IS BEING LXCKED IN
        <span className="loading-title-dots" aria-hidden="true">
          <span />
          <span />
          <span />
        </span>
      </h3>
      <p className="plan-lockin-copy">
        UNLXCK is reviewing and finalising your camp. This takes 2-5 minutes.
        We&rsquo;ll notify you when it&rsquo;s ready.
      </p>
      {accessToken && (pushState === "unsubscribed" || pushState === "enabling") ? (
        <button
          type="button"
          className="cta plan-lockin-notify-button"
          onClick={handleEnableNotifications}
          disabled={pushState === "enabling"}
        >
          {pushState === "enabling" ? "Enabling notifications…" : "Notify me when it's ready"}
        </button>
      ) : null}
      {pushState === "subscribed" ? (
        <p className="plan-lockin-notify-confirmed">
          Notifications on. We&rsquo;ll ping you the moment it&rsquo;s live.
        </p>
      ) : null}
      {pushError ? <p className="plan-lockin-notify-error">{pushError}</p> : null}
    </section>
  );
}

export type StructuredCardDebug = { status: string; errors: string[] };

const STRUCTURED_CARD_LIFECYCLE_STATES = new Set<StructuredCardLifecycleState>([
  "live",
  "building",
  "failed",
  "not_attempted",
  "none",
]);

const STRUCTURED_CARD_STATUS_LABELS: Record<StructuredCardLifecycleState, string> = {
  live: "Enhanced card live",
  building: "Enhanced card building",
  failed: "Enhanced card failed",
  not_attempted: "Enhanced card not attempted",
  none: "Enhanced card no record",
};

/**
 * PlanDetail should always include a server-derived lifecycle record. This
 * defensive boundary keeps the admin chip explicit during rolling deployments
 * or when a malformed legacy response is encountered: it falls back to "none"
 * instead of silently disappearing.
 */
export function normalizeStructuredCardState(
  value: StructuredCardState | null | undefined,
): StructuredCardState {
  const record = value && typeof value === "object" ? value : null;
  const state =
    record && STRUCTURED_CARD_LIFECYCLE_STATES.has(record.state)
      ? record.state
      : "none";
  const reasons = Array.isArray(record?.reasons)
    ? record.reasons
        .map((reason) => (reason != null ? String(reason).trim() : ""))
        .filter(Boolean)
    : [];
  const schemaVersion =
    typeof record?.schema_version === "string" && record.schema_version.trim()
      ? record.schema_version.trim()
      : null;
  const attemptStartedAt =
    typeof record?.attempt_started_at === "string" && record.attempt_started_at.trim()
      ? record.attempt_started_at.trim()
      : null;

  return {
    state,
    reasons,
    schema_version: schemaVersion,
    attempt_started_at: attemptStartedAt,
  };
}

export function canRebuildEnhancedCard(
  cardState: StructuredCardState | null | undefined,
): boolean {
  const normalized = normalizeStructuredCardState(cardState);
  return normalized.state === "failed" || normalized.state === "not_attempted";
}

export function StructuredCardStatusChip({
  cardState,
}: {
  cardState: StructuredCardState | null | undefined;
}) {
  const normalized = normalizeStructuredCardState(cardState);
  const label = STRUCTURED_CARD_STATUS_LABELS[normalized.state];
  const schemaVersion =
    normalized.state === "live" ? normalized.schema_version?.trim() || null : null;
  const accessibleLabel = schemaVersion ? `${label}, ${schemaVersion}` : label;
  const detail = normalized.reasons.length
    ? `${accessibleLabel}: ${normalized.reasons.join("; ")}`
    : accessibleLabel;

  return (
    <span
      className={`badge structured-card-status-chip structured-card-status-${normalized.state}`}
      data-state={normalized.state}
      aria-label={accessibleLabel}
      title={detail}
    >
      <span className="structured-card-status-dot" aria-hidden="true" />
      <span>{label}</span>
      {schemaVersion ? (
        <span className="structured-card-schema-version">{schemaVersion}</span>
      ) : null}
    </span>
  );
}

/**
 * The structured-card conversion outcome recorded on the plan's validator report
 * ({status, errors}; see api/stage2_automation._record_structured_outcome).
 *
 * Returned for any recorded status because this is shown when the richer saved
 * payload is missing, where each status is diagnostic:
 *  - `invalid_fallback_used` → the converted card was rejected (faithfulness /
 *    schema drift) so no card was persisted,
 *  - `valid` / `repair_attempted_valid` → a card WAS built and validated but is
 *    not the one showing, i.e. it was lost on a later write or no longer decodes
 *    at read time (the "card built then lost" signal),
 *  - `not_attempted` → structured generation never ran for this plan.
 * Returns null only when there is no recorded outcome at all.
 */
export function readStructuredCardDebug(
  plan: Pick<PlanDetail, "admin_outputs">,
): StructuredCardDebug | null {
  const report = plan.admin_outputs?.stage2_validator_report;
  const debug =
    report && typeof report === "object"
      ? (report as Record<string, unknown>).structured_plan
      : null;
  if (!debug || typeof debug !== "object") {
    return null;
  }
  const record = debug as Record<string, unknown>;
  const status = typeof record.status === "string" ? record.status.trim() : "";
  if (!status) {
    return null;
  }
  // Coerce defensively: a null/undefined entry must not surface as the literal
  // strings "null"/"undefined", and whitespace-only reasons are dropped.
  const errors = Array.isArray(record.errors)
    ? record.errors.map((entry) => (entry != null ? String(entry).trim() : "")).filter(Boolean)
    : [];
  return { status, errors };
}

/**
 * Admin-only explainer for a missing saved structured payload. The athlete still
 * gets the enhanced renderer through the deterministic plan_text adapter; this
 * diagnostic explains why the richer server payload was unavailable.
 */
function buildStructuredCardDiagnostic(
  cardState: StructuredCardState,
  debug: StructuredCardDebug | null,
): StructuredCardDebug | null {
  if (cardState.state === "building" || cardState.state === "live") {
    return null;
  }
  const hasServerDiagnostic =
    cardState.state === "failed" || cardState.state === "not_attempted";
  if (!debug && !hasServerDiagnostic) {
    return null;
  }
  const errors = Array.from(
    new Set([...(cardState.reasons || []), ...(debug?.errors || [])]),
  );
  return {
    status:
      debug?.status || (cardState.state === "not_attempted" ? "not_attempted" : "failed"),
    errors,
  };
}

function StructuredCardDiagnostic({
  debug,
  hasSavedCard,
}: {
  debug: StructuredCardDebug;
  /** A previously saved card is still rendering (e.g. a failed REBUILD kept the
   * prior good card), so the athlete-facing fallback copy must not claim the
   * text renderer is showing. */
  hasSavedCard?: boolean;
}) {
  const wasBuilt = debug.status === "valid" || debug.status === "repair_attempted_valid";
  const notAttempted = debug.status === "not_attempted";
  const buildDidNotComplete = debug.status === "failed";
  const heading = wasBuilt ? "Saved structured payload not loaded" : "Saved structured payload missing";
  const athleteViewSentence = hasSavedCard
    ? "The athlete still sees the previously saved enhanced card."
    : "The athlete still sees the enhanced renderer built deterministically from the saved plan text.";
  const copy = wasBuilt
    ? `A structured payload was built and validated but is no longer available at read time. ${athleteViewSentence}`
    : notAttempted
      ? `Server-side structured generation never ran for this plan. ${athleteViewSentence}`
      : buildDidNotComplete
        ? `The latest structured-card build did not complete. ${athleteViewSentence} The reasons below explain what stopped a new payload from landing.`
      : `The converted server payload was rejected, so it was not saved. ${athleteViewSentence} The reasons below explain why the richer payload was rejected.`;
  return (
    <section className="support-panel" role="status">
      <div className="form-section-header">
        <p className="kicker">Admin diagnostic</p>
        <h3>
          {heading}: {humanizeStatus(debug.status)}
        </h3>
      </div>
      <p className="muted">{copy}</p>
      {debug.errors.length ? (
        <ul className="summary-list">
          {debug.errors.map((reason, index) => (
            <li key={`${reason}-${index}`}>{reason}</li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function formatRiskBandLabel(riskBand: NonNullable<PlanAdvisory["risk_band"]>) {
  const normalized = humanizeStatus(riskBand || "").trim();
  if (!normalized) {
    return "Unknown";
  }
  return normalized.charAt(0).toUpperCase() + normalized.slice(1);
}

export function readInjuryTriage(plan: PlanDetail): InjuryTriageView | null {
  const whyLogTriage =
    plan.admin_outputs?.why_log && typeof plan.admin_outputs.why_log === "object"
      ? (plan.admin_outputs.why_log as Record<string, unknown>).injury_triage
      : null;
  const adminOutputsRecord = plan.admin_outputs as Record<string, unknown> | undefined;
  const planRecord = plan as Record<string, unknown>;
  const source = whyLogTriage ?? adminOutputsRecord?.injury_triage ?? planRecord.injury_triage;

  if (source && typeof source === "object") {
    const triage = source as Record<string, unknown>;
    const mode = typeof triage.mode === "string" ? triage.mode : undefined;
    const reasons = Array.isArray(triage.reasons) ? triage.reasons.map(String) : [];
    const redFlags = Array.isArray(triage.red_flags) ? triage.red_flags.map(String) : [];
    const matchedCategories = Array.isArray(triage.matched_high_risk_categories)
      ? triage.matched_high_risk_categories.map(String)
      : [];
    const routingReasons = Array.isArray(triage.routing_reasons) ? triage.routing_reasons.map(String) : [];
    const urgentFlags = Array.isArray(triage.urgent_flags) ? triage.urgent_flags.map(String) : [];

    const hasStructuredSignal = Boolean(
      mode || reasons.length || redFlags.length || matchedCategories.length || routingReasons.length || urgentFlags.length,
    );
    if (!hasStructuredSignal) {
      return null;
    }

    return {
      mode,
      reasons,
      red_flags: redFlags,
      matched_high_risk_categories: matchedCategories,
      routing_reasons: routingReasons,
      urgent_flags: urgentFlags,
      sparring_risk_band:
        typeof triage.sparring_risk_band === "string" ? triage.sparring_risk_band : undefined,
      clinician_clearance_required:
        typeof triage.clinician_clearance_required === "boolean"
          ? triage.clinician_clearance_required
          : undefined,
    };
  }

  if (plan.status === "triage_blocked" || plan.admin_outputs?.stage2_status === "triage_blocked") {
    return {
      mode: "needs_review",
      reasons: ["Protected planner state was triggered before finalization."],
      red_flags: [],
      matched_high_risk_categories: [],
      routing_reasons: [],
      urgent_flags: [],
      sparring_risk_band: undefined,
      clinician_clearance_required: undefined,
    };
  }

  return null;
}

export function readRawTriageMode(plan: PlanDetail): string | null {
  const whyLog = plan.admin_outputs?.why_log;
  const whyLogTriage =
    whyLog && typeof whyLog === "object"
      ? (whyLog as Record<string, unknown>).injury_triage
      : null;

  const adminOutputsRecord = plan.admin_outputs as Record<string, unknown> | undefined;
  const planRecord = plan as Record<string, unknown>;

  const sources = [whyLogTriage, adminOutputsRecord?.injury_triage, planRecord.injury_triage];

  for (const source of sources) {
    if (source && typeof source === "object") {
      const mode = (source as Record<string, unknown>).mode;
      if (typeof mode === "string" && mode.trim()) {
        return mode.trim();
      }
    }
  }

  return null;
}


export function shouldShowProtectedResumeAdminReview(input: {
  isTriageBlocked: boolean;
  isProtectedTriageResumePending: boolean;
  hasResumeApproval: boolean;
}): boolean {
  return input.isTriageBlocked || input.isProtectedTriageResumePending || input.hasResumeApproval;
}

export function getAdminReviewHeading(input: {
  showProtectedResumeAdminReview: boolean;
  hasResumeApproval: boolean;
}): string {
  if (!input.showProtectedResumeAdminReview) {
    return "Manual Stage 2 actions";
  }
  return input.hasResumeApproval ? "Resume generation required" : "Planning blocked before Stage 2";
}

export function canRetryResumeGenerationForPlan(input: {
  isAdmin: boolean;
  isProtectedTriageResumePending: boolean;
  injuryTriageMode?: string | null;
  rawTriageMode?: string | null;
  planStatus?: string | null;
}): boolean {
  const normalize = (val?: string | null) => String(val || "").trim().toLowerCase();
  if (
    normalize(input.injuryTriageMode) === "medical_hold" ||
    normalize(input.rawTriageMode) === "medical_hold" ||
    normalize(input.planStatus) === "medical_hold"
  ) {
    return false;
  }

  return (
    input.isAdmin &&
    input.isProtectedTriageResumePending &&
    (isResumableTriageMode(input.injuryTriageMode) ||
      isResumableTriageMode(input.rawTriageMode) ||
      isResumableTriageMode(input.planStatus))
  );
}

function BlockedPlanDecisionCard({
  triage,
  injuryContext,
  isAdmin,
}: {
  triage: InjuryTriageView;
  injuryContext?: BlockedInjuryContextSummary | null;
  isAdmin: boolean;
}) {
  const isMedicalHold = triage.mode === "medical_hold";
  const isRestricted = triage.mode === "restricted_rehab_only";
  const [injuryDetailsOpen, setInjuryDetailsOpen] = useState(false);
  const capturedInjuries = injuryContext?.capturedInjuries ?? [];
  const legacyInjuryText = injuryContext?.legacyInjuryText?.trim() || "";
  const pauseReasons = injuryContext?.pauseReasons ?? [];
  const hasCapturedDetail = capturedInjuries.length > 0 || Boolean(legacyInjuryText);

  const title = isMedicalHold
    ? "Medical hold"
    : isRestricted
      ? "Clearance required"
      : "Planning paused";

  const intro = isMedicalHold
    ? "No training plan was released. This intake contains urgent or medically disqualifying signals that require review before planning can continue."
    : isRestricted
      ? "Normal fight-camp release has been paused. This intake contains structural injury signals that require clinician clearance before loading or sparring resumes."
      : "Normal fight-camp release has been paused. This intake triggered a protected planner state before finalization.";

  const signalTokens = [...triage.matched_high_risk_categories, ...triage.red_flags]
    .map(titleizeToken)
    .filter(Boolean)
    .slice(0, 6);

  const triageRiskBand =
    triage.sparring_risk_band &&
    ["green", "amber", "red", "black"].includes(triage.sparring_risk_band)
      ? (triage.sparring_risk_band as RiskBandTone)
      : null;
  const displayedRiskBand = isMedicalHold
    ? triageRiskBand === "black"
      ? "black"
      : null
    : triageRiskBand;
  const riskBandLabel = displayedRiskBand
    ? formatRiskBandLabel(displayedRiskBand as NonNullable<PlanAdvisory["risk_band"]>)
    : null;

  return (
    <section
      className={`support-panel sparring-advisory-card ${
        isMedicalHold ? "support-panel-alert" : "sparring-advisory-convert"
      }`}
    >
      <div className="plan-header-row">
        <div>
          <p className="kicker">Planner decision</p>
          <h3>{title}</h3>
        </div>
        <div className="sparring-advisory-badges">
          <span className="badge">PROTECTED</span>
          <span className="badge">STAGE 2 SKIPPED</span>
          {riskBandLabel && displayedRiskBand ? (
            <span
              className={`sparring-risk-chip sparring-risk-${displayedRiskBand}`}
              aria-label={`Injury risk ${riskBandLabel}`}
            >
              <span className="sparring-risk-dot" aria-hidden="true" />
              <span>Sparring risk: {riskBandLabel}</span>
              {(() => {
                const riskExplanation = explainRiskBand(displayedRiskBand);
                const blockedExplanation = buildBlockedWhy(triage);
                return (
                  <WhyTooltip
                    title={blockedExplanation.title}
                    body={`${blockedExplanation.body}${riskExplanation ? ` ${riskExplanation.body}` : ""}`}
                  />
                );
              })()}
            </span>
          ) : null}
        </div>
      </div>

      <p>{intro}</p>

      {injuryContext?.capturedInjury ? (
        <div className="blocked-context-line">
          <div>
            <strong>Captured injury:</strong> {injuryContext.capturedInjury}
          </div>
        </div>
      ) : null}

      {injuryContext?.blockedTrigger ? (
        <div className="blocked-context-line">
          <div>
            <strong>Blocked trigger:</strong> {injuryContext.blockedTrigger}
          </div>
        </div>
      ) : null}

      {hasCapturedDetail ? (
        <div className="blocked-context-line">
          <button
            type="button"
            className="ghost-button"
            onClick={() => setInjuryDetailsOpen((open) => !open)}
            aria-expanded={injuryDetailsOpen}
          >
            {injuryDetailsOpen ? "Hide injury details" : "Show injury details"}
          </button>
          {injuryDetailsOpen ? (
            capturedInjuries.length ? (
              <ul className="summary-list">
                {capturedInjuries.map((injury, index) => (
                  <li key={`${injury.headline}-${index}`}>
                    <div>
                      <strong>{injury.headline}</strong>
                    </div>
                    {injury.meta.length ? (
                      <div className="plan-card-meta">
                        {injury.meta.map((entry) => (
                          <span key={entry} className="badge status-badge-neutral">
                            {entry}
                          </span>
                        ))}
                      </div>
                    ) : null}
                    {injury.flags.length ? (
                      <div className="plan-card-meta">
                        {injury.flags.map((flag) => (
                          <span key={flag} className="badge">
                            {flag}
                          </span>
                        ))}
                      </div>
                    ) : null}
                    {injury.notes ? (
                      <div>
                        <em>Athlete notes:</em> {injury.notes}
                      </div>
                    ) : null}
                    {injury.avoid ? (
                      <div>
                        <em>Avoid:</em> {injury.avoid}
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : (
              <div>
                <strong>Captured injury:</strong> {legacyInjuryText}
              </div>
            )
          ) : null}
        </div>
      ) : null}

      {isAdmin && pauseReasons.length ? (
        <div className="blocked-context-line">
          <strong>Why this was paused</strong>
          <ul className="summary-list">
            {pauseReasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {isAdmin && signalTokens.length ? (
        <div className="plan-card-meta">
          {signalTokens.map((token) => (
            <span key={token} className="badge status-badge-neutral">
              {token}
            </span>
          ))}
        </div>
      ) : null}

      <ul className="summary-list">
        <li>Stage 2 was skipped intentionally.</li>
        <li>
          {isMedicalHold
            ? "Medical review is required before any plan can be released."
            : "Only already-approved rehab or clinician-led guidance should continue until clearance."}
        </li>
        {triage.clinician_clearance_required ? (
          <li>Clinician clearance required before loading or sparring.</li>
        ) : null}
      </ul>
    </section>
  );
}

function downloadArtifact(text: string, filename: string) {
  if (!text.trim()) {
    return;
  }
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function safeIssueList(value: unknown): ValidatorIssue[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is ValidatorIssue => Boolean(item) && typeof item === "object");
}

function issueTitle(code: string) {
  return ISSUE_TITLES[code] || humanizeStatus(code || "review issue");
}

function joinContextBits(bits: Array<string | null | undefined>) {
  return bits.filter((bit): bit is string => Boolean(bit && bit.trim())).join(" | ");
}

function normalizeIssueText(value: unknown) {
  return typeof value === "string" && value ? value.replace(/_/g, " ") : null;
}

function formatIssueContext(issue: ValidatorIssue) {
  const equipment =
    Array.isArray(issue.required_equipment) && issue.required_equipment.length
      ? `Needs ${issue.required_equipment.map((item) => String(item).replace(/_/g, " ")).join(", ")}`
      : null;

  return joinContextBits([
    typeof issue.phase === "string" && issue.phase ? issue.phase : null,
    typeof issue.week_index === "number" ? `Week ${issue.week_index}` : null,
    typeof issue.session_index === "number" ? `Session ${issue.session_index}` : null,
    normalizeIssueText(issue.requirement),
    normalizeIssueText(issue.restriction),
    equipment,
  ]);
}

function buildReviewIssue(issue: ValidatorIssue, severity: "error" | "warning"): ReviewIssue {
  const code = typeof issue.code === "string" ? issue.code : "review_issue";
  const message =
    typeof issue.message === "string" && issue.message.trim()
      ? issue.message.trim()
      : issueTitle(code);
  const snippet =
    typeof issue.line === "string" && issue.line.trim() ? issue.line.trim() : undefined;

  return {
    code,
    title: issueTitle(code),
    message,
    severity,
    context: formatIssueContext(issue) || undefined,
    snippet,
  };
}

function resolveWarningBuckets(report: Record<string, unknown> | null | undefined) {
  const warnings = safeIssueList(report?.warnings);
  const explicitBlockingWarnings = safeIssueList(report?.blocking_warnings);

  if (explicitBlockingWarnings.length) {
    return {
      blockingWarnings: explicitBlockingWarnings.filter((issue) =>
        BLOCKING_WARNING_CODES.has(String(issue.code || "")),
      ),
    };
  }

  return {
    blockingWarnings: warnings.filter((issue) =>
      BLOCKING_WARNING_CODES.has(String(issue.code || "")),
    ),
  };
}

function pluralize(count: number, singular: string) {
  return `${count} ${singular}${count === 1 ? "" : "s"}`;
}

export function hasBlockedTriageStubText(...texts: Array<string | null | undefined>): boolean {
  const combined = texts
    .map((text) => (typeof text === "string" ? text.trim() : ""))
    .filter(Boolean)
    .join("\n");
  return TRIAGE_BLOCKED_STUB_MARKERS.some((marker) => combined.includes(marker));
}

export function isProtectedTriageResumePendingState(input: {
  isTriageBlocked: boolean;
  stage2Status?: string | null;
  containsBlockedTriageStub: boolean;
  athletePlanText: string;
  finalPlanText?: string | null;
}): boolean {
  const normalizedStage2Status = (input.stage2Status || "").trim().toLowerCase();
  const hasEmptyAthletePlanWithFinalStub =
    !input.athletePlanText.trim() && hasBlockedTriageStubText(input.finalPlanText);
  return (
    input.isTriageBlocked ||
    normalizedStage2Status === "triage_resume_approved" ||
    normalizedStage2Status === "triage_blocked" ||
    input.containsBlockedTriageStub ||
    hasEmptyAthletePlanWithFinalStub
  );
}

export function isResumableTriageMode(modeOrStatus?: string | null): boolean {
  const normalized = String(modeOrStatus || "").trim().toLowerCase();
  return normalized === "needs_review" || normalized === "restricted_rehab_only";
}

/**
 * The admin sidebar's "Release state" line.
 *
 * Driven by the saved plan status, which is what actually decides whether the
 * athlete can see the plan. It must not be derived from the validator summary:
 * a flagged plan has findings (so `isPublishable` is false) but is already
 * released, and labelling it "Held" contradicts the plan the admin is looking at.
 */
export function describePlanReleaseState(input: {
  status: string | null | undefined;
  isTriageBlocked?: boolean;
  triageMode?: string | null;
  isProtectedTriageResumePending?: boolean;
}): string {
  if (input.isTriageBlocked) {
    return input.triageMode === "medical_hold" ? "Blocked" : "Protected";
  }
  if (input.isProtectedTriageResumePending) {
    return "Blocked / resume pending";
  }
  const status = (input.status || "").trim().toLowerCase();
  if (status === "publishable_with_flags") {
    return "Released with flags";
  }
  if (status === "ready") {
    return "Released";
  }
  if (status === "archived") {
    return "Archived";
  }
  return "Held";
}

export function buildReviewSummary(
  report: Record<string, unknown> | null | undefined,
  stage2Status: string,
  options?: {
    hasBlockedTriageStubText?: boolean;
  },
) {
  const normalizedStage2Status = String(stage2Status || "").trim().toLowerCase();
  const errors = safeIssueList(report?.errors).map((issue) => buildReviewIssue(issue, "error"));
  const { blockingWarnings } = resolveWarningBuckets(report);
  const blocking = blockingWarnings.map((issue) => buildReviewIssue(issue, "warning"));
  const blockingCount = blocking.length;
  const isPublishableFromReport = errors.length === 0 && blocking.length === 0;
  const isExplicitlyNonPublishableStatus = NON_PUBLISHABLE_STAGE2_STATUSES.has(normalizedStage2Status);
  const isBlockedTriageStub = Boolean(options?.hasBlockedTriageStubText);
  const isPublishable =
    !isExplicitlyNonPublishableStatus && !isBlockedTriageStub && isPublishableFromReport;

  const summary = {
    errors,
    blocking,
    blockingCount,
    isPublishable,
  };

  if (isPublishable) {
    // A Stage 1 fallback has a clean report because the validator never ran
    // against it — the AI finalizer failed outright. Say so, or the technical
    // failure is invisible behind a "ready to release" that looks routine.
    if (normalizedStage2Status === STAGE2_STAGE1_FALLBACK_STATUS) {
      return {
        ...summary,
        hasIssues: false,
        headline: "Released from Stage 1 because the AI finalizer pass failed.",
        guidance:
          "Stage 2 never returned a usable plan, so the deterministic Stage 1 plan was released. " +
          "The reason is recorded under stage2_fallback in the validator report below.",
      };
    }
    return {
      ...summary,
      hasIssues: false,
      headline: "This plan is ready to release.",
      guidance: "No hard blockers remain. Approval is now just a release decision.",
    };
  }

  if (errors.length + blocking.length === 0) {
    if (isBlockedTriageStub && normalizedStage2Status !== "triage_resume_approved") {
      return {
        ...summary,
        hasIssues: true,
        headline: "Triage placeholder text is currently holding this Stage 2 plan.",
        guidance: "This plan still contains triage placeholder text and cannot be released to the athlete.",
      };
    }

    return {
      ...summary,
      hasIssues: false,
      headline:
        normalizedStage2Status === "triage_resume_approved"
          ? "Resume approved, regeneration pending. A regenerated final result is required before release."
          : normalizedStage2Status === "stage2_failed"
          ? "Stage 2 validation failed, but no detailed reasons were saved in the report."
          : "No validator issues were saved for this plan.",
      guidance:
        normalizedStage2Status === "triage_resume_approved"
          ? "Keep this plan blocked until Stage 2 regeneration completes and a real final result replaces the triage stub."
          : normalizedStage2Status === "stage2_failed"
          ? "The plan was released to the athlete regardless. Open the latest model output below to see what the validator objected to."
          : "This usually means the plan is held for workflow reasons rather than a specific validator issue.",
    };
  }

  const summaryParts = [
    errors.length ? pluralize(errors.length, "blocking error") : null,
    blockingCount ? pluralize(blockingCount, "blocking issue") : null,
  ].filter((part): part is string => Boolean(part));

  // Stage 2 findings no longer withhold a plan — a flagged plan has already gone
  // to the athlete and these are here for audit. Only a triage stub still blocks,
  // so the "holding" language is reserved for that.
  return {
    ...summary,
    hasIssues: true,
    headline: isBlockedTriageStub
      ? `${summaryParts.join(" and ")} are currently holding this Stage 2 plan.`
      : `Flagged on this Stage 2 plan: ${summaryParts.join(" and ")}.`,
    guidance:
      isBlockedTriageStub
        ? "This plan still contains triage placeholder text and cannot be released to the athlete."
        : "The plan was released to the athlete with these flags recorded. Review them and regenerate if the plan needs correcting.",
  };
}

function ArtifactActions({
  artifactKey,
  text,
  filename,
}: {
  artifactKey: string;
  text: string;
  filename: string;
}) {
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  useEffect(() => {
    if (!copiedKey) {
      return;
    }
    const timeout = window.setTimeout(() => setCopiedKey(null), 1800);
    return () => window.clearTimeout(timeout);
  }, [copiedKey]);

  if (!text.trim()) {
    return null;
  }

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedKey(artifactKey);
    } catch {
      // clipboard write failed; no visible feedback shown
    }
  }

  return (
    <div className="plan-summary-actions">
      <button type="button" className="ghost-button" onClick={handleCopy}>
        {copiedKey === artifactKey ? "Copied" : "Copy text"}
      </button>
      <button type="button" className="ghost-button" onClick={() => downloadArtifact(text, filename)}>
        Download .txt
      </button>
    </div>
  );
}

function QuickCopyButton({ text, artifactKey }: { text: string; artifactKey: string }) {
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  useEffect(() => {
    if (!copiedKey) {
      return;
    }
    const timeout = window.setTimeout(() => setCopiedKey(null), 1800);
    return () => window.clearTimeout(timeout);
  }, [copiedKey]);

  if (!text.trim()) {
    return null;
  }

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedKey(artifactKey);
    } catch {
      // clipboard write failed; no visible feedback shown
    }
  }

  return (
    <button type="button" className="ghost-button" onClick={handleCopy}>
      {copiedKey === artifactKey ? "Copied" : "Copy text"}
    </button>
  );
}

function AdminArtifactSection({
  artifactKey,
  isOpen,
  onToggle,
  kicker,
  title,
  summary,
  description,
  text,
  filename,
}: {
  artifactKey: string;
  isOpen: boolean;
  onToggle: () => void;
  kicker: string;
  title: string;
  summary: string;
  description?: string;
  text: string;
  filename?: string;
}) {
  return (
    <section className={`accordion-item ${isOpen ? "accordion-item-open" : ""}`}>
      <button type="button" className="accordion-trigger" onClick={onToggle} aria-expanded={isOpen}>
        <div className="accordion-trigger-copy">
          <p className="kicker">{kicker}</p>
          <h3>{title}</h3>
          <p className="muted accordion-summary">{summary}</p>
        </div>
        <span className="accordion-chevron" aria-hidden="true">
          {isOpen ? "-" : "+"}
        </span>
      </button>
      {isOpen ? (
        <div className="accordion-panel">
          {description ? <p className="muted">{description}</p> : null}
          {filename ? (
            <ArtifactActions artifactKey={artifactKey} text={text} filename={filename} />
          ) : null}
          <pre className="code-block">{text}</pre>
        </div>
      ) : null}
    </section>
  );
}

export function resolvePlanActiveState(params: {
  todayResolved: boolean;
  todayActivePlanId?: string | null;
  activeEndpointResolved: boolean;
  activeEndpointPlanId?: string | null;
}): string | null | undefined {
  if (params.activeEndpointResolved) {
    return params.activeEndpointPlanId ?? null;
  }
  if (params.todayResolved) {
    return params.todayActivePlanId ?? null;
  }
  return undefined;
}

type PlanOperationalState = {
  planId: string;
  activePlanId: string | null | undefined;
  nextSessionFocusDate?: Date;
  nextSessionAction: TodaySession | null;
  planCompletions: PlanCompletionsResponse | null;
};

export function PlanViewer({
  plan,
  accessToken,
  viewerRole,
  viewerProfileId,
  onPlanUpdated,
  onPlanDeleted,
}: {
  plan: PlanDetail;
  accessToken: string | null;
  viewerRole: UserRole;
  viewerProfileId: string | null;
  onPlanUpdated?: (plan: PlanDetail) => void;
  onPlanDeleted?: () => Promise<void> | void;
}) {
  const router = useRouter();
  const canUseAdminOutputs = canUseAdminPlanControls(viewerRole, Boolean(plan.admin_outputs));
  const isViewerAdmin = isAdminRole(viewerRole);
  const canManagePlan = viewerRole === "admin" || viewerRole === "athlete";
  const canSubmitPlanFeedback = canShowContextualPlanFeedback(
    viewerRole,
    viewerProfileId,
    plan.athlete_id,
  );
  const archivedPreview = isArchivedPlan(plan.status);
  const completedFightCamp = isCompletedFightCamp(plan.activation_state);
  // Only surface an advisory that carries a real injury-risk band; the rest just
  // restate load tweaks the plan already applied, so they are suppressed.

  const athletePlanText = plan.outputs.plan_text.trim();
  const hasPublishedPlan = isPlanReleasedToAthlete(plan);
  const hasStructuredAthletePlan = shouldUseSavedStructuredPlan(plan);
  const structuredCardState = normalizeStructuredCardState(plan.structured_card_state);
  const structuredCardStateKey = JSON.stringify(structuredCardState);
  const canRebuildStructuredCard = canRebuildEnhancedCard(structuredCardState);

  const injuryTriage = readInjuryTriage(plan);
  const rawTriageMode = readRawTriageMode(plan);
  const hasResumeApproval = hasTriageResumeApproval(plan);
  const isTriageBlocked = shouldShowTriageBlockedState(
    plan,
    injuryTriage?.mode || rawTriageMode || undefined,
  );

  const openOngoing = isOpenOngoingPlan(plan.fight_date);
  const useSavedStructuredPlan = hasStructuredAthletePlan;
  const planDetailTitle = getPlanDisplayName(plan);
  // An unnamed fight-camp plan is titled by its fight date, so the meta line
  // would only repeat the heading directly above it.
  const titleIsFightDate = Boolean(plan.fight_date) && planDetailTitle === formatPlanFightDate(plan.fight_date);
  const fightDateLabel =
    plan.fight_date && !titleIsFightDate ? `Fight date ${formatPlanFightDate(plan.fight_date)}` : null;

  const blockedTitle =
    injuryTriage?.mode === "medical_hold"
      ? "Medical hold"
      : injuryTriage?.mode === "restricted_rehab_only"
        ? "Clearance required"
        : "Planning paused";

  const statusLabel = completedFightCamp
    ? "Completed"
    : isTriageBlocked
      ? blockedTitle
      : viewerRole === "athlete"
        ? formatAthletePlanStatus(plan.status || "generated")
        : formatPlanStatus(plan.status || "generated");

  const stage2Status = isTriageBlocked
    ? "Stage 2 skipped intentionally"
    : titleizeToken(plan.admin_outputs?.stage2_status || "legacy");

  const heroSummary = completedFightCamp
    ? "This fight camp has ended and remains available as completed training history."
    : isTriageBlocked
      ? injuryTriage?.mode === "medical_hold"
        ? "The planner intentionally blocked this intake before finalization because it contains urgent or medically disqualifying signals."
        : "The planner intentionally paused normal release because this intake contains structural injury signals that require clearance."
      : hasPublishedPlan
        ? openOngoing
          ? "Your rolling four-week performance block, ready for the next prescribed dose."
          : "Your validated camp plan, ready for the next training decision."
        : "This plan is held back from the athlete view until Stage 2 clears review.";

  const handoffText = plan.admin_outputs?.stage2_handoff_text || "";
  const retryText = plan.admin_outputs?.stage2_retry_text || "";
  const draftText = plan.admin_outputs?.draft_plan_text || "No Stage 1 draft.";
  const latestStage2Text = plan.admin_outputs?.final_plan_text || "No Stage 2 output.";
  const coachNotesText = plan.admin_outputs?.coach_notes || "No internal notes.";
  const validatorText = formatStructuredValue(
    plan.admin_outputs?.stage2_validator_report,
    "No validator report.",
  );
  const validatorReport =
    plan.admin_outputs?.stage2_validator_report &&
    typeof plan.admin_outputs.stage2_validator_report === "object"
      ? plan.admin_outputs.stage2_validator_report
      : {};
  const containsBlockedTriageStub = hasBlockedTriageStubText(
    plan.admin_outputs?.final_plan_text,
    plan.admin_outputs?.draft_plan_text,
  );
  const isProtectedTriageResumePending = isProtectedTriageResumePendingState({
    isTriageBlocked,
    stage2Status: plan.admin_outputs?.stage2_status,
    containsBlockedTriageStub,
    athletePlanText,
    finalPlanText: plan.admin_outputs?.final_plan_text,
  });
  const stage2ReviewSummary = buildReviewSummary(
    validatorReport,
    plan.admin_outputs?.stage2_status || "",
    { hasBlockedTriageStubText: containsBlockedTriageStub },
  );
  const planningBriefText = formatStructuredValue(
    plan.admin_outputs?.planning_brief,
    "No planning brief.",
  );
  const payloadText = formatStructuredValue(
    plan.admin_outputs?.stage2_payload,
    "No Stage 2 payload.",
  );
  const reviewPlanText = (plan.admin_outputs?.final_plan_text || "").trim();
  const approvableText =
    plan.admin_outputs?.final_plan_text?.trim() ||
    plan.admin_outputs?.draft_plan_text?.trim() ||
    athletePlanText ||
    "";
  const canApproveForRelease =
    canUseAdminOutputs && !hasPublishedPlan && Boolean(approvableText) && !isProtectedTriageResumePending;
  const canRetryResumeGeneration = canRetryResumeGenerationForPlan({
    isAdmin: canUseAdminOutputs,
    isProtectedTriageResumePending,
    injuryTriageMode: injuryTriage?.mode,
    rawTriageMode,
    planStatus: plan.status,
  });

  const showProtectedResumeAdminReview = shouldShowProtectedResumeAdminReview({
    isTriageBlocked,
    isProtectedTriageResumePending,
    hasResumeApproval,
  });
  
  const canRejectApproval = canUseAdminOutputs;
  const blockedInjuryContext = injuryTriage
    ? buildBlockedInjuryContextSummary({
        triage: injuryTriage,
        injuriesText: plan.admin_outputs?.intake_injuries?.injuries,
        guidedInjuries: (plan.admin_outputs?.intake_injuries?.guided_injuries ?? []).map(readGuidedInjurySummary),
      })
    : null;
  // Per-region Rehab/Prehab policy, decided server-side from the athlete's live
  // injury flags (resolve_rehab_label_policy), NOT the intake "medically cleared"
  // answer — an athlete can be cleared to train while still rehabbing, and the
  // Today "Cleared" action resolves the injury flag without touching intake. A
  // rehab block reads "Rehab" only while the region it targets is still injured;
  // legacy payloads omit the field and everything stays "Rehab".
  const rehabLabelPolicy = plan.rehab_label_policy ?? null;
  const approveButtonLabel = stage2ReviewSummary.isPublishable
    ? "Approve for athlete view"
    : "Approve anyway";
  const reviewPanelClassName = `support-panel stage2-review-panel ${
    stage2ReviewSummary.isPublishable ? "" : "support-panel-alert"
  }`.trim();
  const approvalSourceLabel = plan.admin_outputs?.final_plan_text?.trim()
    ? "saved Stage 2 final output"
    : plan.admin_outputs?.draft_plan_text?.trim()
      ? "saved Stage 1 draft"
      : "current plan text";

  const [manualPlanText, setManualPlanText] = useState(plan.admin_outputs?.final_plan_text || "");
  const [manualSubmitPending, setManualSubmitPending] = useState(false);
  const [manualSubmitMessage, setManualSubmitMessage] = useState<string | null>(null);
  const [manualSubmitError, setManualSubmitError] = useState<string | null>(null);
  const [approvePending, setApprovePending] = useState(false);
  const [approveMessage, setApproveMessage] = useState<string | null>(null);
  const [approveError, setApproveError] = useState<string | null>(null);
  const [structuredCardRebuildPending, setStructuredCardRebuildPending] = useState(false);
  const [structuredCardRebuildMessage, setStructuredCardRebuildMessage] = useState<string | null>(null);
  const [structuredCardRebuildError, setStructuredCardRebuildError] = useState<string | null>(null);
  const [resumeReason, setResumeReason] = useState("");
  const [resumePending, setResumePending] = useState(false);
  const [resumeMessage, setResumeMessage] = useState<string | null>(null);
  const [resumeError, setResumeError] = useState<string | null>(null);
  const [rejectPending, setRejectPending] = useState(false);
  const [rejectMessage, setRejectMessage] = useState<string | null>(null);
  const [rejectError, setRejectError] = useState<string | null>(null);
  const [archivePending, setArchivePending] = useState(false);
  const [archiveMessage, setArchiveMessage] = useState<string | null>(null);
  const [archiveError, setArchiveError] = useState<string | null>(null);
  const [planOperationalState, setPlanOperationalState] = useState<PlanOperationalState | null>(null);
  const currentOperationalState =
    planOperationalState?.planId === plan.plan_id ? planOperationalState : null;
  // `undefined` means the active-plan sources are still resolving. Keeping that
  // distinct from `null` prevents a false "Set active" state during hydration.
  const activePlanId = currentOperationalState?.activePlanId;
  const nextSessionFocusDate = currentOperationalState?.nextSessionFocusDate;
  const nextSessionAction = currentOperationalState?.nextSessionAction ?? null;
  const planCompletions = currentOperationalState?.planCompletions ?? null;
  const [setActivePending, setSetActivePending] = useState(false);
  const [setActiveError, setSetActiveError] = useState<string | null>(null);
  const [showActiveConflict, setShowActiveConflict] = useState(false);
  const [planActionPending, setPlanActionPending] = useState<
    "rename" | "archive" | "permanent-delete" | null
  >(null);
  const [planActionMessage, setPlanActionMessage] = useState<string | null>(null);
  const [planActionError, setPlanActionError] = useState<string | null>(null);
  // Plans whose background structured-payload poll window has elapsed. The
  // deterministic enhanced renderer remains the final view when polling stops.
  const [pollExpiredPlans, setPollExpiredPlans] = useState<Record<string, boolean>>({});
  const [stage2RetryInProgress, setStage2RetryInProgress] = useState(false);
  const [stage2RetryJustCompleted, setStage2RetryJustCompleted] = useState<"passed" | "failed" | null>(
    null,
  );
  const [openAdminSection, setOpenAdminSection] = useState(() => {
    if (retryText.trim()) {
      return "retry";
    }
    if ((plan.admin_outputs?.final_plan_text || "").trim()) {
      return "final";
    }
    if (handoffText.trim()) {
      return "handoff";
    }
    return "draft";
  });
  const generationController = useGenerationController({
    token: accessToken,
    storageKey: `unlxck:pending-generation:triage-resume:${plan.plan_id}`,
    createJob: async (clientRequestId) => {
      if (!accessToken) {
        throw new Error("Admin session missing. Please sign in again.");
      }
      return approveAndResumeGeneration(
        accessToken,
        plan.plan_id,
        { reason: resumeReason.trim() },
        clientRequestId,
      );
    },
    onComplete: async ({ planId }) => {
      if (!accessToken) {
        return;
      }

      const resolvedPlanId = planId || plan.plan_id;
      let refreshedPlan = await getPlan(accessToken, resolvedPlanId);

      for (let attempt = 1; attempt < TRIAGE_RESUME_FETCH_ATTEMPTS; attempt += 1) {
        const stillBlocked =
          refreshedPlan.status === "triage_blocked" ||
          refreshedPlan.admin_outputs?.stage2_status === "triage_blocked";
        if (!stillBlocked) {
          break;
        }
        await sleep(TRIAGE_RESUME_FETCH_DELAY_MS * attempt);
        try {
          refreshedPlan = await getPlan(accessToken, resolvedPlanId);
        } catch {
          // Ignore transient fetch failures during the polling window
        }
      }

      onPlanUpdated?.(refreshedPlan);
      setResumeMessage("Approved and resumed successfully.");
      router.refresh();
    },
  });
  const { showToast } = useToast();
  // Announce the enhanced card landing while the athlete is on the page: the
  // background poll swaps the renderer silently, so without this the "we'll
  // notify you" promise has no open-tab counterpart. Keyed per plan so
  // switching between plans in one mounted viewer can never false-positive.
  const structuredPlanSeenRef = useRef<{ planId: string; hadStructuredPlan: boolean } | null>(
    null,
  );
  useEffect(() => {
    const previous = structuredPlanSeenRef.current;
    structuredPlanSeenRef.current = {
      planId: plan.plan_id,
      hadStructuredPlan: hasStructuredAthletePlan,
    };
    if (
      previous &&
      previous.planId === plan.plan_id &&
      !previous.hadStructuredPlan &&
      hasStructuredAthletePlan &&
      !isViewerAdmin
    ) {
      showToast("Your final camp is live.", { tone: "success" });
    }
  }, [plan.plan_id, hasStructuredAthletePlan, isViewerAdmin, showToast]);

  const structuredPlanPollExpired = Boolean(pollExpiredPlans[plan.plan_id]);
  // Hold the athlete's first view on the lock-in card until the enhanced card
  // lands. Bounded by the poll window and skipped for terminal card states, so
  // the deterministic fallback still shows when no richer payload is coming.
  const holdPlanForEnhancedCard = shouldHoldPlanForEnhancedCard({
    isViewerAdmin,
    structuredCardLifecycleState: structuredCardState.state,
    hasPublishedPlan,
    hasStructuredPlan: hasStructuredAthletePlan,
    pollWindowExpired: structuredPlanPollExpired,
    hasAccessToken: Boolean(accessToken),
    isRecentPlan: isRecentlyCreatedPlan(plan),
    isTriageBlocked,
  });
  // The server field is authoritative after reload. Old failure details are
  // hidden only while a newer attempt is actively building.
  const structuredCardDebug = buildStructuredCardDiagnostic(
    structuredCardState,
    readStructuredCardDebug(plan),
  );

  useEffect(() => {
    setManualPlanText(plan.admin_outputs?.final_plan_text || "");
  }, [plan.plan_id, plan.admin_outputs?.final_plan_text]);

  // Resolve active state, Today and completion rows in parallel. Today is the
  // server-authoritative operating view, so it can confirm the active plan when
  // the dedicated endpoint is transiently unavailable. We only apply the daily
  // focus when the command view reports that THIS plan is active; admins viewing
  // another athlete's plan get no override.
  useEffect(() => {
    if (!accessToken || !canManagePlan) {
      return;
    }
    let cancelled = false;
    const canLoadAthleteCompletions =
      viewerRole === "athlete" && viewerProfileId === plan.athlete_id;
    const completionsRequest = canLoadAthleteCompletions
      ? getPlanCompletions(accessToken, plan.plan_id)
      : Promise.resolve(null);

    // The active label and "Open Today" link may reuse reads from the screen the
    // athlete came from; Today itself re-reads before any training action.
    Promise.allSettled([
      getActivePlan(accessToken, { reuseForMs: SHARED_READ_MAX_AGE_MS }),
      getToday(accessToken, { reuseForMs: SHARED_READ_MAX_AGE_MS }),
      completionsRequest,
    ]).then(([activeResult, todayResult, completionsResult]) => {
        if (cancelled) {
          return;
        }

        const activeFromToday =
          todayResult.status === "fulfilled" ? todayResult.value.active_plan?.id || null : null;
        const activeFromEndpoint =
          activeResult.status === "fulfilled" ? activeResult.value?.plan_id || null : null;
        const state = todayResult.status === "fulfilled" ? todayResult.value : null;
        const next = state?.today?.next_session;
        const isThisActivePlan = state?.active_plan?.id === plan.plan_id;
        const iso =
          isThisActivePlan && next?.session_relation === "next"
            ? (next.calendar_date || "").slice(0, 10)
            : "";
        // Parse at local noon to dodge any timezone date-shift; an unusable date
        // (undated plans) leaves the calendar "Today" highlight untouched.
        const parsed = iso ? new Date(`${iso}T12:00:00`) : null;
        setPlanOperationalState({
          planId: plan.plan_id,
          activePlanId: resolvePlanActiveState({
            todayResolved: todayResult.status === "fulfilled",
            todayActivePlanId: activeFromToday,
            activeEndpointResolved: activeResult.status === "fulfilled",
            activeEndpointPlanId: activeFromEndpoint,
          }),
          nextSessionAction: isThisActivePlan && next ? next : null,
          nextSessionFocusDate:
            parsed && !Number.isNaN(parsed.getTime()) ? parsed : undefined,
          planCompletions:
            completionsResult.status === "fulfilled" ? completionsResult.value : null,
        });
      });
    return () => {
      cancelled = true;
    };
  }, [
    accessToken,
    canManagePlan,
    plan.athlete_id,
    plan.plan_id,
    viewerProfileId,
    viewerRole,
  ]);

  useEffect(() => {
    setOpenAdminSection(
      retryText.trim()
        ? "retry"
        : (plan.admin_outputs?.final_plan_text || "").trim()
          ? "final"
          : handoffText.trim()
            ? "handoff"
            : "draft",
    );
  }, [plan.plan_id, handoffText, retryText, plan.admin_outputs?.final_plan_text]);

  // Background upgrade poll: the enhanced renderer is already on screen, so this
  // watches both the server lifecycle and the richer saved payload. It never
  // blocks the view. Runs for any open published plan still missing its card, or
  // for an admin-held plan whose server lifecycle says a build is active.
  useEffect(() => {
    if (
      !shouldPollForStructuredPlanUpgrade({
        hasPublishedPlan,
        hasStructuredPlan: hasStructuredAthletePlan,
        pollWindowExpired: structuredPlanPollExpired,
        hasAccessToken: Boolean(accessToken),
        isServerBuilding:
          isViewerAdmin && structuredCardState.state === "building",
        isTriageBlocked,
      })
    ) {
      return;
    }

    let cancelled = false;

    const pollForStructuredPlan = async (signal: AbortSignal) => {
      if (!accessToken) {
        return;
      }
      try {
        const refreshedPlan = await getPlan(accessToken, plan.plan_id, { signal });
        // A response that raced its own abort (deadline or stop) is dropped.
        if (signal.aborted) {
          return;
        }
        const refreshedStateKey = JSON.stringify(
          normalizeStructuredCardState(refreshedPlan.structured_card_state),
        );
        // Keep the chip current when a build fails or completes, and only swap
        // the actual plan renderer once the richer saved payload exists.
        if (
          !cancelled &&
          (refreshedStateKey !== structuredCardStateKey ||
            (shouldRenderStructuredPlan(refreshedPlan.outputs) &&
              Boolean(refreshedPlan.outputs.structured_plan)))
        ) {
          onPlanUpdated?.(refreshedPlan);
        }
      } catch {
        // Transient fetch failure: the deterministic enhanced renderer stays up
        // and the next tick retries until the payload lands or polling expires.
      }
    };

    const stopPolling = startStructuredPlanUpgradePoll({
      poll: pollForStructuredPlan,
      onWindowExpired: () => {
        if (!cancelled) {
          setPollExpiredPlans((prev) => ({ ...prev, [plan.plan_id]: true }));
        }
      },
      intervalMs: STRUCTURED_PLAN_POLL_INTERVAL_MS,
      windowMs: STRUCTURED_PLAN_UPGRADE_POLL_WINDOW_MS,
      fetchTimeoutMs: STRUCTURED_PLAN_POLL_FETCH_TIMEOUT_MS,
    });

    return () => {
      cancelled = true;
      stopPolling();
    };
  }, [
    accessToken,
    hasPublishedPlan,
    hasStructuredAthletePlan,
    isTriageBlocked,
    isViewerAdmin,
    onPlanUpdated,
    plan.plan_id,
    structuredCardState.state,
    structuredCardStateKey,
    structuredPlanPollExpired,
  ]);

  async function handleManualStage2Submit() {
    if (!accessToken) {
      setManualSubmitError("Admin session missing. Please sign in again.");
      return;
    }
    if (!manualPlanText.trim()) {
      setManualSubmitError("Paste the GPT final plan before submitting.");
      return;
    }

    setManualSubmitPending(true);
    setStage2RetryInProgress(true);
    setStage2RetryJustCompleted(null);
    setManualSubmitError(null);
    setManualSubmitMessage(null);

    try {
      const updatedPlan = await submitManualStage2(accessToken, plan.plan_id, {
        final_plan_text: manualPlanText,
      });
      const retryPassed = updatedPlan.status === "ready";
      setStage2RetryJustCompleted(retryPassed ? "passed" : "failed");
      onPlanUpdated?.(updatedPlan);
      setManualSubmitMessage(
        retryPassed
          ? "Manual Stage 2 output passed validation and is now published in the app."
          : "Manual Stage 2 output was saved, but it still needs revision. The retry prompt below is updated.",
      );
    } catch (error) {
      setStage2RetryJustCompleted(null);
      setManualSubmitError(
        error instanceof Error ? error.message : "Unable to submit manual Stage 2 output.",
      );
    } finally {
      setManualSubmitPending(false);
      setStage2RetryInProgress(false);
    }
  }

  /** After an approval whose card is still missing, restart the lifecycle poll.
   * The visible building state itself comes from PlanDetail, never this session. */
  function restartStructuredPlanPolling(updatedPlan: PlanDetail) {
    if (shouldRenderStructuredPlan(updatedPlan.outputs)) {
      return; // card shipped inline with the approval — nothing to await
    }
    setPollExpiredPlans((prev) => {
      if (!prev[plan.plan_id]) {
        return prev;
      }
      const next = { ...prev };
      delete next[plan.plan_id];
      return next;
    });
  }

  async function handleApproveForRelease() {
    if (!accessToken) {
      setApproveError("Admin session missing. Please sign in again.");
      return;
    }
    if (!canApproveForRelease) {
      setApproveError("There is no saved draft or Stage 2 final text available to approve.");
      return;
    }

    setApprovePending(true);
    setApproveError(null);
    setApproveMessage(null);
    setRejectError(null);
    setRejectMessage(null);

    try {
      const updatedPlan = await approvePlanForRelease(accessToken, plan.plan_id);
      onPlanUpdated?.(updatedPlan);
      setApproveMessage(getApprovalSuccessMessage(updatedPlan));
      restartStructuredPlanPolling(updatedPlan);
    } catch (error) {
      // Approval persists server-side before any slow post-processing, so a
      // network/timeout failure is often a false negative: the plan may already
      // be released. Re-fetch a few times and treat a now-ready plan as success.
      const recoveredPlan = await resolveApprovalAfterError({
        error,
        fetchPlan: () => getPlan(accessToken, plan.plan_id),
      });
      if (recoveredPlan) {
        onPlanUpdated?.(recoveredPlan);
        setApproveMessage(getApprovalSuccessMessage(recoveredPlan));
        restartStructuredPlanPolling(recoveredPlan);
        return;
      }
      setApproveError(
        error instanceof Error ? error.message : "Unable to approve this plan for athlete view.",
      );
    } finally {
      setApprovePending(false);
    }
  }

  async function handleRebuildStructuredCard() {
    if (!accessToken) {
      setStructuredCardRebuildError("Admin session missing. Please sign in again.");
      return;
    }
    if (!canRebuildStructuredCard) {
      setStructuredCardRebuildError(
        "Enhanced cards can only be rebuilt after a failed or not-attempted conversion.",
      );
      return;
    }

    setStructuredCardRebuildPending(true);
    setStructuredCardRebuildError(null);
    setStructuredCardRebuildMessage(null);

    try {
      const result = await rebuildStructuredPlan(accessToken, plan.plan_id);
      restartStructuredPlanPolling(plan);

      // The endpoint stamps the server-side attempt marker before returning.
      // Pull it immediately so the chip moves to Building without waiting for
      // the next interval; the existing poll then follows it to Live or Failed.
      try {
        const refreshedPlan = await getPlan(accessToken, result.plan_id || plan.plan_id);
        onPlanUpdated?.(refreshedPlan);
      } catch {
        // The rebuild was accepted even if this immediate refresh is transiently
        // unavailable. The bounded background poll and a route refresh recover it.
      }

      setStructuredCardRebuildMessage(
        result.queued
          ? "Enhanced card rebuild queued. Status updates automatically."
          : "No new rebuild was queued; current server status refreshed.",
      );
      router.refresh();
    } catch (error) {
      setStructuredCardRebuildError(
        error instanceof Error ? error.message : "Unable to rebuild the enhanced card.",
      );
    } finally {
      setStructuredCardRebuildPending(false);
    }
  }

  async function handleApproveAndResumeGeneration() {
    if (!accessToken) {
      setResumeError("Admin session missing. Please sign in again.");
      return;
    }
    if (!canRetryResumeGeneration) {
      setResumeError("This plan cannot be resumed from its current triage state.");
      return;
    }
    if (!resumeReason.trim()) {
      setResumeError("Please enter a short reason before resuming generation.");
      return;
    }
    setResumePending(true);
    generationController.setError(null);
    setResumeError(null);
    setResumeMessage(null);
    try {
      await generationController.startGeneration();
      setResumeReason("");
    } catch (error) {
      setResumeError(error instanceof Error ? error.message : "Unable to approve and resume generation.");
    } finally {
      setResumePending(false);
    }
  }

  useEffect(() => {
    if (generationController.error) {
      setResumeError(generationController.error);
    }
  }, [generationController.error]);

  // Swap / add / remove for the plan's owner, once their completion rows are
  // known: a started or logged workout keeps its prescription.
  const planId = plan.plan_id;
  const exerciseCustomisation = useMemo<ExerciseCustomisation | null>(() => {
    const trainingDay = planCompletions?.current_training_day || plan.schedule_context?.current_training_day;
    if (!accessToken || !planCompletions || !trainingDay || plan.status === "archived") return null;
    const locked = new Set(
      planCompletions.completions
        .filter((row) => row.status !== "not_started")
        .map((row) => `${row.training_day.slice(0, 10)}|${row.session_id}`),
    );
    return {
      token: accessToken,
      planId,
      canRepeat: !plan.fight_date,
      today: trainingDay,
      isLocked: (date, sessionId) => locked.has(`${date}|${sessionId}`),
      onSessionChanged: (date, session) => {
        if (session && plan.outputs.structured_plan) {
          const patched = patchPlanSession(plan.outputs.structured_plan, date, session);
          if (patched && patched !== plan.outputs.structured_plan) {
            onPlanUpdated?.({ ...plan, outputs: { ...plan.outputs, structured_plan: patched } });
          }
        }
        // Then the server's full plan, so recurring changes show on every date.
        void getPlan(accessToken, planId).then((fresh) => onPlanUpdated?.(fresh)).catch(() => undefined);
      },
    };
  }, [accessToken, planCompletions, plan, planId, onPlanUpdated]);

  if (generationController.isGenerating || generationController.hasPendingGeneration) {
    return (
      <PremiumLoadingScreen
        phase={generationController.phase}
        error={generationController.error}
        statusMessage={generationController.statusMessage}
        startedAtMs={generationController.startedAtMs}
        endedAtMs={generationController.endedAtMs}
      />
    );
  }

  async function handleRejectApproval() {
    if (!accessToken) {
      setRejectError("Admin session missing. Please sign in again.");
      return;
    }

    setRejectPending(true);
    setRejectError(null);
    setRejectMessage(null);
    setApproveError(null);
    setApproveMessage(null);

    try {
      const updatedPlan = await rejectApprovedPlan(accessToken, plan.plan_id);
      onPlanUpdated?.(updatedPlan);
      setRejectMessage(hasPublishedPlan ? "Plan rejected and moved back to review." : "Plan rejected.");
    } catch (error) {
      setRejectError(error instanceof Error ? error.message : "Unable to reject this plan.");
    } finally {
      setRejectPending(false);
    }
  }

  async function handleSetActive(overlapAction?: ActivePlanOverlapAction) {
    if (!accessToken) {
      setSetActiveError("Session expired. Sign in again.");
      return;
    }
    if (!canSetActivePlan(plan.activation_state)) {
      setSetActiveError("This plan cannot be set active from its current state.");
      return;
    }
    setSetActivePending(true);
    setSetActiveError(null);
    try {
      const active = await setActivePlan(accessToken, plan.plan_id, { overlapAction });
      setPlanOperationalState((current) => ({
        planId: plan.plan_id,
        activePlanId: active.plan_id,
        nextSessionAction: current?.planId === plan.plan_id ? current.nextSessionAction : null,
        nextSessionFocusDate:
          current?.planId === plan.plan_id ? current.nextSessionFocusDate : undefined,
        planCompletions: current?.planId === plan.plan_id ? current.planCompletions : null,
      }));
      requestXpRefresh();
      setShowActiveConflict(false);
    } catch (error) {
      if (!overlapAction && isActivePlanOverlapError(error)) {
        setShowActiveConflict(true);
        return;
      }
      setSetActiveError(error instanceof Error ? error.message : "Unable to set this plan active.");
    } finally {
      setSetActivePending(false);
    }
  }

  async function handleArchivePlan() {
    if (!accessToken) {
      setArchiveError("Admin session missing. Please sign in again.");
      return;
    }

    const confirmed = window.confirm(`Archive "${planDetailTitle}"?`);
    if (!confirmed) {
      return;
    }

    setArchivePending(true);
    setArchiveError(null);
    setArchiveMessage(null);

    try {
      const updatedPlan = await adminArchivePlan(accessToken, plan.plan_id);
      onPlanUpdated?.(updatedPlan);
      setArchiveMessage("Plan archived.");
    } catch (error) {
      setArchiveError(error instanceof Error ? error.message : "Unable to archive this plan.");
    } finally {
      setArchivePending(false);
    }
  }

  async function handleRenamePlan() {
    if (!accessToken) {
      setPlanActionError("Session expired. Sign in again.");
      return;
    }

    const currentName = plan.plan_name?.trim() || "";
    const nextName = window.prompt("Rename this plan", currentName || plan.fight_date || "");
    if (nextName == null) {
      return;
    }

    const normalizedName = nextName.trim();
    if (!normalizedName) {
      setPlanActionError("Plan name cannot be empty.");
      return;
    }

    setPlanActionPending("rename");
    setPlanActionError(null);
    setPlanActionMessage(null);

    try {
      const updatedPlan = await renamePlan(accessToken, plan.plan_id, normalizedName);
      onPlanUpdated?.(updatedPlan);
      setPlanActionMessage("Plan renamed.");
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "Unable to rename this plan.";
      if (
        errorMessage.includes("Unable to reach the server") ||
        errorMessage.includes("502") ||
        errorMessage.includes("503") ||
        errorMessage.includes("504")
      ) {
        setPlanActionError("Connection issue. Try again in a minute.");
      } else {
        setPlanActionError(errorMessage);
      }
    } finally {
      setPlanActionPending(null);
    }
  }

  async function handleArchiveOwnPlan() {
    if (!accessToken) {
      setPlanActionError("Session expired. Sign in again.");
      return;
    }

    const confirmed = window.confirm(`Archive "${planDetailTitle}"?`);
    if (!confirmed) {
      return;
    }

    setPlanActionPending("archive");
    setPlanActionError(null);
    setPlanActionMessage(null);

    try {
      await archivePlan(accessToken, plan.plan_id);
      clearCompletedGenerationForDeletedPlan(plan.plan_id);
      await onPlanDeleted?.();
      router.push(viewerRole === "admin" ? "/admin" : "/plans");
      router.refresh();
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "Unable to archive this plan.";
      if (
        errorMessage.includes("Unable to reach the server") ||
        errorMessage.includes("502") ||
        errorMessage.includes("503") ||
        errorMessage.includes("504")
      ) {
        setPlanActionError("Connection issue. Try again in a minute.");
      } else {
        setPlanActionError(errorMessage);
      }
    } finally {
      setPlanActionPending(null);
    }
  }

  async function handlePermanentDelete() {
    if (!accessToken) {
      setPlanActionError("Session expired. Sign in again.");
      return;
    }

    const planName = plan.plan_name?.trim() ?? "";
    const isArchived = (plan.status || "").trim().toLowerCase() === "archived";

    // Archived plans are already retired, so skip the type-the-name confirmation
    // and use a single confirm. Live plans still require typing the name.
    if (isArchived) {
      const confirmed = window.confirm(
        `Permanently delete "${planDetailTitle}"? This cannot be undone.`,
      );
      if (!confirmed) {
        return;
      }
    } else {
      if (!planName) {
        setPlanActionError("This plan has no name. Rename it before permanent deletion.");
        return;
      }

      const typed = window.prompt(
        `Permanent delete cannot be undone.\n\nType the plan name to confirm:\n${planName}`,
      );
      if (typed == null) {
        return;
      }
      if (typed.trim() !== planName) {
        setPlanActionError("Confirmation did not match the plan name. Nothing was deleted.");
        return;
      }
    }

    setPlanActionPending("permanent-delete");
    setPlanActionError(null);
    setPlanActionMessage(null);

    try {
      await adminPermanentlyDeletePlan(accessToken, plan.plan_id, isArchived ? undefined : planName);
      clearCompletedGenerationForDeletedPlan(plan.plan_id);
      await onPlanDeleted?.();
      router.push("/admin");
      router.refresh();
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "Unable to delete this plan.";
      if (
        errorMessage.includes("Unable to reach the server") ||
        errorMessage.includes("502") ||
        errorMessage.includes("503") ||
        errorMessage.includes("504")
      ) {
        setPlanActionError("Connection issue. Try again in a minute.");
      } else {
        setPlanActionError(errorMessage);
      }
    } finally {
      setPlanActionPending(null);
    }
  }

  const activePlanStateResolved = activePlanId !== undefined;
  const isCurrentActivePlan = activePlanId === plan.plan_id;
  // A declared contact day is named by its contact, as on Today, not by the
  // app session that happens to ride along with it.
  const nextSessionContact = nextSessionAction?.coach_led_contact
    ? contactTimerTarget({ today_card: { coach_led_contact: nextSessionAction.coach_led_contact } } as StructuredDay)
    : null;
  const nextSessionTitle =
    (nextSessionContact ? contactTitle(nextSessionContact) : "") ||
    athleteFacingSessionTitle(
      nextSessionAction?.title?.trim() || nextSessionAction?.label?.trim() || "Open the next session",
    );
  const nextSessionRelation = nextSessionAction?.session_relation === "next" ? "Next session" : "Today";
  // The app's authoritative "today" — same source the plan renderer uses for its
  // current-day marker — so the countdown is deterministic across server render,
  // browser hydration, and the 03:00 training-day rollover (never the machine clock).
  const currentTrainingDayIso =
    planCompletions?.current_training_day || plan.schedule_context?.current_training_day;
  // For a genuinely-upcoming session, append a "· in N days" countdown so the
  // card reads as time-relative; skip it for the "Today" relation, where the
  // date is already today and a countdown would just echo the label.
  const nextSessionRelativeDay =
    nextSessionAction?.session_relation === "next"
      ? describeRelativeDay(nextSessionAction.calendar_date, currentTrainingDayIso)
      : null;
  const nextSessionDate = nextSessionAction?.calendar_date
    ? [formatAppDate(nextSessionAction.calendar_date), nextSessionRelativeDay]
        .filter(Boolean)
        .join(" · ")
    : null;
  const openSessionLabel = nextSessionAction?.session_relation === "next" ? "Open next session" : "Open Today";
  const openWeekNumber = resolveFiniteWeekNumber(plan.schedule_context?.current_week_number);
  const openBlockLabel = openOngoing
    ? `Block ${plan.schedule_context?.block_number ?? 1} · Week ${openWeekNumber} of 4`
    : null;

  // While the active plan is still unknown, show only SYNCING: a guessed
  // status would widen the badge and reflow the title when it resolves.
  const checkingActivePlan = canManagePlan && !completedFightCamp && !activePlanStateResolved;
  const showStatusLabel =
    (activePlanStateResolved && !isCurrentActivePlan) || completedFightCamp || isTriageBlocked || !hasPublishedPlan;
  // At most one primary action sits under the hero; everything secondary
  // (All plans, Edit Plan, Rename, Archive…) lives in the ••• menu.
  const heroPrimaryAction =
    canManagePlan && isCurrentActivePlan && !nextSessionAction ? (
      <Link href="/today" className="cta">
        Open Today
      </Link>
    ) : canManagePlan && activePlanStateResolved && !isCurrentActivePlan && canSetActivePlan(plan.activation_state) ? (
      <button
        type="button"
        className="cta"
        onClick={() => void handleSetActive()}
        disabled={setActivePending}
      >
        {setActivePending ? "Setting active..." : "Set active"}
      </button>
    ) : archivedPreview && viewerRole === "athlete" ? (
      <Link href="/create" className="ghost-button">
        Create a plan
      </Link>
    ) : null;

  const adminSections = [
    {
      artifactKey: "draft",
      kicker: "Stage 1",
      title: "Draft plan",
      summary: "Original planner output before the final Stage 2 rewrite.",
      text: draftText,
    },
    {
      artifactKey: "final",
      kicker: "Stage 2",
      title: "Latest model output",
      summary: "Most recent saved Stage 2 plan text.",
      text: latestStage2Text,
    },
    {
      artifactKey: "internal-notes",
      kicker: "Internal Notes",
      title: "Coach/internal output",
      summary: "Internal notes saved alongside the current plan.",
      text: coachNotesText,
    },
    {
      artifactKey: "validator",
      kicker: "Validator",
      title: "Latest review report",
      summary: "Structured validator report from the last Stage 2 review.",
      text: validatorText,
    },
    {
      artifactKey: "brief",
      kicker: "Stage 2 Brief",
      title: "Planning brief",
      summary: "Structured brief that Stage 2 used as its planning authority.",
      text: planningBriefText,
    },
    {
      artifactKey: "handoff",
      kicker: "Handoff",
      title: "Stage 2 handoff",
      summary: "Exact handoff prompt generated by the app for manual GPT runs.",
      description:
        "Use this if you want to run a manual Stage 2 pass with the configured finalizer model using the same handoff the app stored.",
      text: handoffText || "No handoff text.",
      filename: buildArtifactFilename(plan, "stage2-handoff"),
    },
    {
      artifactKey: "retry",
      kicker: "Retry",
      title: "Repair prompt",
      summary: "Exact retry prompt to use when the last Stage 2 attempt needs revision.",
      description: "Use this when the validator asked for one more manual repair pass.",
      text: retryText || "No retry prompt.",
      filename: buildArtifactFilename(plan, "stage2-retry"),
    },
    {
      artifactKey: "payload",
      kicker: "Payload",
      title: "Stage 2 package",
      summary: "Internal Stage 2 payload captured for audit and debugging.",
      text: payloadText,
    },
  ];

  return (
    <div className="page">
      <section className="panel plan-detail-hero">
        <QuickBuildRefinementBanner planId={plan.plan_id} planSource={plan.plan_source ?? null} />
        <div className="section-heading">
          <div>
            <p className="kicker">{openOngoing ? "Training plan" : "Fight camp"}</p>
            <h1>{planDetailTitle}</h1>
            {completedFightCamp || isTriageBlocked || !hasPublishedPlan ? (
              <p className="muted">{heroSummary}</p>
            ) : null}
            {fightDateLabel || openBlockLabel ? (
              <p className="plan-detail-meta">{fightDateLabel || openBlockLabel}</p>
            ) : null}
          </div>
          <div className="plan-detail-status">
            <p className="sr-only">Plan status</p>
            <h2 className="plan-summary-title">
              {/* The ACTIVE badge already says a live plan is ready, so the
                  status text only shows when it adds something. */}
              {showStatusLabel ? statusLabel : <span className="sr-only">{statusLabel}</span>}
              {isCurrentActivePlan ? (
                <span className="badge status-badge-success cm-active-badge">ACTIVE</span>
              ) : canManagePlan && !activePlanStateResolved ? (
                <span className="badge status-badge-neutral cm-active-badge">SYNCING</span>
              ) : null}
            </h2>
            {isTriageBlocked ? <p className="muted">Stage 2 was skipped intentionally.</p> : null}
          </div>
          <details className="plan-action-menu plan-hero-menu">
            <summary className="ghost-button" aria-label="Plan options">
              <span aria-hidden="true">•••</span>
            </summary>
            <div className="plan-action-menu-popover">
              <Link href="/plans" className="ghost-button">
                All plans
              </Link>
              {plan.custom_plan_id && plan.athlete_id === viewerProfileId ? (
                <Link className="ghost-button" href={`/plans/custom?plan=${plan.custom_plan_id}`}>Edit Plan</Link>
              ) : null}
              {canManagePlan && !archivedPreview ? (
                <>
                  <button
                    type="button"
                    className="ghost-button"
                    onClick={handleRenamePlan}
                    disabled={planActionPending !== null}
                  >
                    {planActionPending === "rename" ? "Renaming..." : "Rename"}
                  </button>
                  <button
                    type="button"
                    className="ghost-button danger-button"
                    onClick={handleArchiveOwnPlan}
                    disabled={planActionPending !== null}
                  >
                    {planActionPending === "archive" ? "Archiving..." : "Archive"}
                  </button>
                </>
              ) : null}
              {hasPublishedPlan && athletePlanText ? (
                <QuickCopyButton text={athletePlanText} artifactKey="athlete-plan" />
              ) : null}
              {isViewerAdmin ? (
                <button
                  type="button"
                  className="ghost-button danger-button"
                  onClick={handlePermanentDelete}
                  disabled={planActionPending !== null}
                >
                  {planActionPending === "permanent-delete" ? "Deleting..." : "Permanent delete"}
                </button>
              ) : null}
              {isViewerAdmin && plan.athlete_id ? (
                <Link href={`/admin/athletes/${plan.athlete_id}`} className="ghost-button">
                  View athlete profile
                </Link>
              ) : null}
              <p className="plan-created-at muted">Created {formatPlanTimestamp(plan.created_at)}</p>
            </div>
          </details>
        </div>

        {archivedPreview ? (
          <div className="quick-build-refine-banner cm-archived-banner" role="status">
            This plan is archived history. Preview only; it does not affect Today, calendar, streaks, or notifications.
          </div>
        ) : null}

        {completedFightCamp && !archivedPreview ? (
          <div className="quick-build-refine-banner cm-archived-banner" role="status">
            This fight camp has ended. It stays in history and can’t be set active.
          </div>
        ) : null}

        {planHasProfileRefreshFailed(plan) ? (
          <div
            className="quick-build-refine-banner cm-profile-refresh-notice"
            role="status"
            aria-live="polite"
          >
            {PROFILE_REFRESH_FAILED_ATHLETE_NOTICE}
          </div>
        ) : null}

        {/* Hold the next session's space while the active plan resolves, then
            slide the real session in rather than swapping a button for it. */}
        {checkingActivePlan ? (
          <div className="plan-next-session-pending" role="status" aria-label="Checking plan state">
            <div className="plan-next-session-copy">
              <Skeleton variant="text" width={84} height={10} />
              <Skeleton variant="text" width="58%" height={18} />
              <Skeleton variant="text" width="44%" height={12} />
            </div>
            <Skeleton variant="block" width={96} height={44} style={{ borderRadius: 12 }} />
          </div>
        ) : null}

        {isCurrentActivePlan && nextSessionAction ? (
          <div className="plan-next-session plan-hero-arrive" role="region" aria-label={nextSessionRelation}>
            <div className="plan-next-session-copy">
              <p className="label">{nextSessionRelation}</p>
              <h2>{nextSessionTitle}</h2>
              {nextSessionDate ? <p className="muted">{nextSessionDate}</p> : null}
            </div>
            <Link href="/today" className="cta plan-next-session-cta" aria-label={openSessionLabel}>
              Open
            </Link>
          </div>
        ) : null}

        {heroPrimaryAction ? (
          <div className="plan-summary-actions plan-detail-actions plan-hero-arrive">{heroPrimaryAction}</div>
        ) : null}
        {planActionMessage ? <div className="success-banner">{planActionMessage}</div> : null}
        {planActionError ? <div className="error-banner">{planActionError}</div> : null}
        {showActiveConflict ? (
          <PlanSwitchDialog
            plan={plan}
            isPending={setActivePending}
            errorMessage={setActiveError}
            onConfirm={handleSetActive}
            onCancel={() => setShowActiveConflict(false)}
          />
        ) : null}
        {setActiveError ? <div className="error-banner">{setActiveError}</div> : null}
      </section>

      <div className={`plan-detail-layout${canUseAdminOutputs ? "" : " plan-detail-layout-single"}`}>
        {canUseAdminOutputs ? (
          <aside className="plan-summary-stack">
            <section className="plan-summary-card">
              <div className="plan-summary-header">
                <p className="kicker">Stage 2</p>
                <h2 className="plan-summary-title">Automation status</h2>
              </div>
              <div className="plan-meta-grid">
                <article className="plan-meta-item">
                  <p className="plan-meta-label">Stage 2 status</p>
                  <p className="plan-meta-value">{stage2Status}</p>
                </article>
                <article className="plan-meta-item">
                  <p className="plan-meta-label">Attempts</p>
                  <p className="plan-meta-value">{plan.admin_outputs?.stage2_attempt_count || 0}</p>
                </article>
                <article className="plan-meta-item">
                  <p className="plan-meta-label">Release state</p>
                  <p className="plan-meta-value">
                    {describePlanReleaseState({
                      status: plan.status,
                      isTriageBlocked,
                      triageMode: injuryTriage?.mode,
                      isProtectedTriageResumePending,
                    })}
                  </p>
                </article>
                <article className="plan-meta-item">
                  <p className="plan-meta-label">Blocking issues</p>
                  <p className="plan-meta-value">
                    {isTriageBlocked
                      ? "n/a"
                      : stage2ReviewSummary.errors.length + stage2ReviewSummary.blockingCount}
                  </p>
                </article>
              </div>
              {handoffText.trim() ? (
                <>
                  <p className="muted">
                    The Stage 2 handoff is saved, so a manual Stage 2 pass is quick.
                  </p>
                  <ArtifactActions
                    artifactKey="stage2_handoff_text"
                    text={handoffText}
                    filename={buildArtifactFilename(plan, "stage2-handoff")}
                  />
                </>
              ) : null}
              {retryText.trim() ? (
                <>
                  <p className="muted">A repair prompt is ready for a manual retry.</p>
                  <ArtifactActions
                    artifactKey="stage2_retry_text"
                    text={retryText}
                    filename={buildArtifactFilename(plan, "stage2-retry")}
                  />
                </>
              ) : null}
            </section>
          </aside>
        ) : null}

        <section className="plan-text-panel">
          {/* The validation status/badge is operational metadata. Athletes go
              straight into the camp map (which carries its own header); admins
              and any not-yet-published/triage state still see the status. */}
          {isViewerAdmin || canUseAdminOutputs || !hasPublishedPlan || isTriageBlocked ? (
            <div className="plan-header-row">
              <div>
                <p className="kicker">{isViewerAdmin ? "Athlete Plan" : "Your plan"}</p>
                <h2>
                  {isTriageBlocked
                    ? blockedTitle
                    : plan.admin_outputs?.stage2_status === "triage_resume_approved"
                      ? "Resume approved, regeneration pending"
                    : hasPublishedPlan
                      ? "Validated final plan"
                      : "Pending finalization"}
                </h2>
              </div>
              <div className="plan-header-badges">
                <span
                  className={`badge ${
                    isTriageBlocked
                      ? injuryTriage?.mode === "medical_hold"
                        ? "issue-badge-error"
                        : ""
                      : hasPublishedPlan
                        ? "status-badge-success"
                        : "status-badge-neutral"
                  }`}
                >
                  {isTriageBlocked
                    ? blockedTitle
                    : plan.admin_outputs?.stage2_status === "triage_resume_approved"
                      ? "Resume pending"
                    : hasPublishedPlan
                      ? "Validated"
                      : "Review required"}
                </span>
                {isViewerAdmin ? (
                  <StructuredCardStatusChip cardState={structuredCardState} />
                ) : null}
              </div>
            </div>
          ) : null}

          {isViewerAdmin ? (
            <div className="structured-card-admin-controls">
              <button
                type="button"
                className="ghost-button structured-card-rebuild-button"
                onClick={handleRebuildStructuredCard}
                disabled={structuredCardRebuildPending || !canRebuildStructuredCard}
                aria-describedby={`structured-card-rebuild-note-${plan.plan_id}`}
              >
                {structuredCardRebuildPending ? "Rebuilding…" : "Rebuild enhanced card"}
              </button>
              <p
                id={`structured-card-rebuild-note-${plan.plan_id}`}
                className="structured-card-rebuild-note"
              >
                Rebuild reruns conversion and all safety checks. It can’t override a safety block.
              </p>
              {structuredCardRebuildMessage ? (
                <div className="success-banner" role="status">
                  {structuredCardRebuildMessage}
                </div>
              ) : null}
              {structuredCardRebuildError ? (
                <div className="error-banner" role="alert">
                  {structuredCardRebuildError}
                </div>
              ) : null}
            </div>
          ) : null}

          {isTriageBlocked && injuryTriage ? (
            <BlockedPlanDecisionCard
              triage={injuryTriage}
              injuryContext={blockedInjuryContext}
              isAdmin={isViewerAdmin}
            />
          ) : hasPublishedPlan ? (
            <>
              {canRejectApproval || canUseAdminOutputs ? (
                <div className="plan-summary-actions">
                  {canRejectApproval ? (
                    <button
                      type="button"
                      className="ghost-button"
                      onClick={handleRejectApproval}
                      disabled={rejectPending}
                    >
                      {rejectPending ? "Rejecting..." : "Reject approval"}
                    </button>
                  ) : null}
                  {canUseAdminOutputs ? (
                    <button
                      type="button"
                      className="ghost-button"
                      onClick={handleArchivePlan}
                      disabled={archivePending}
                    >
                      {archivePending ? "Archiving..." : "Archive"}
                    </button>
                  ) : null}
                </div>
              ) : null}
              {useSavedStructuredPlan && plan.outputs.structured_plan ? (
                <ExerciseCustomisationProvider value={exerciseCustomisation}>
                <StructuredPlanRenderer
                  plan={plan.outputs.structured_plan}
                  defaultView="calendar"
                  openOngoing={openOngoing}
                  focusDay={nextSessionFocusDate}
                  currentDayLabel={nextSessionFocusDate ? "Next session" : "Today"}
                  createdAt={plan.created_at}
                  planStatus={plan.status}
                  scheduleContext={plan.schedule_context}
                  completions={planCompletions?.completions}
                  currentTrainingDayIso={currentTrainingDayIso}
                  isAdmin={isViewerAdmin}
                  rehabLabelPolicy={rehabLabelPolicy}
                  exerciseMedia={plan.outputs.exercise_media}
                  accessToken={accessToken}
                  customPlanId={viewerRole === "athlete" && plan.athlete_id === viewerProfileId && !archivedPreview ? plan.custom_plan_id : null}
                />
                </ExerciseCustomisationProvider>
              ) : holdPlanForEnhancedCard ? (
                <EnhancedCardLockInCard accessToken={accessToken} />
              ) : (
                <>
                  {isViewerAdmin && structuredCardState.state === "building" ? (
                    <section className="support-panel" role="status">
                      <div className="form-section-header">
                        <p className="kicker">Enhanced card</p>
                        <h3>Building the enhanced card…</h3>
                      </div>
                      <p className="muted">
                        Structured generation is running. The full card appears here when ready; the plan below stays live meanwhile.
                      </p>
                    </section>
                  ) : null}
                  {isViewerAdmin && structuredCardDebug ? (
                    <StructuredCardDiagnostic
                      debug={structuredCardDebug}
                      hasSavedCard={hasStructuredAthletePlan}
                    />
                  ) : null}
                  <TextStructuredPlanRenderer
                    text={athletePlanText}
                    fightDate={plan.fight_date}
                    createdAt={plan.created_at}
                    focusDay={nextSessionFocusDate}
                    currentDayLabel={nextSessionFocusDate ? "Next session" : "Today"}
                    scheduleContext={plan.schedule_context}
                    isAdmin={isViewerAdmin}
                    rehabLabelPolicy={rehabLabelPolicy}
                    exerciseMedia={plan.outputs.exercise_media}
                  />
                </>
              )}
              {!holdPlanForEnhancedCard && canSubmitPlanFeedback && accessToken ? (
                <ContextualFeedback
                  key={`plan-feedback-${plan.plan_id}`}
                  token={accessToken}
                  surface="plan"
                  planId={plan.plan_id}
                />
              ) : null}
              {rejectMessage ? <div className="success-banner">{rejectMessage}</div> : null}
              {rejectError ? <div className="error-banner">{rejectError}</div> : null}
              {archiveMessage ? <div className="success-banner">{archiveMessage}</div> : null}
              {archiveError ? <div className="error-banner">{archiveError}</div> : null}
            </>
          ) : (
            <div className="plan-review-stack">
              {canUseAdminOutputs ? (
                <>
                  {stage2RetryInProgress ? (
                    <section className="support-panel stage2-retry-banner stage2-retry-in-progress">
                      <div className="form-section-header">
                        <p className="kicker">Stage 2 Retry</p>
                        <h3>Retry in progress</h3>
                      </div>
                      <p className="muted">
                        Validating now. Results below are from the previous attempt until this retry completes.
                      </p>
                    </section>
                  ) : null}

                  {stage2RetryJustCompleted ? (
                    <section
                      className={`support-panel stage2-retry-banner ${
                        stage2RetryJustCompleted === "passed"
                          ? "stage2-retry-passed"
                          : "stage2-retry-failed"
                      }`}
                    >
                      <div className="form-section-header">
                        <p className="kicker">
                          Stage 2 Retry (Attempt {plan.admin_outputs?.stage2_attempt_count || 1})
                        </p>
                        <h3>
                          {stage2RetryJustCompleted === "passed"
                            ? "Retry passed, plan published"
                            : "Retry completed. New validation results below."}
                        </h3>
                      </div>
                      <p className="muted">
                        {stage2RetryJustCompleted === "passed"
                          ? "The submitted plan passed validation and has been published to the athlete view."
                          : "The submitted plan was validated. Hard blockers below reflect this latest attempt."}
                      </p>
                    </section>
                  ) : null}

                  <section
                    className={`${reviewPanelClassName}${
                      stage2RetryInProgress ? " stage2-review-panel-stale" : ""
                    }`}
                  >
                    <div className="form-section-header">
                      <p className="kicker">
                        Stage 2 review
                        {plan.admin_outputs?.stage2_attempt_count
                          ? ` (attempt ${plan.admin_outputs.stage2_attempt_count})`
                          : ""}
                        {stage2RetryInProgress ? " (previous attempt)" : ""}
                      </p>
                      <h3>
                        {stage2ReviewSummary.isPublishable
                          ? "Release decision"
                          : "Why this plan is being held"}
                      </h3>
                    </div>

                    <div className="stage2-review-state-row">
                      <span
                        className={`badge ${
                          stage2ReviewSummary.isPublishable
                            ? "status-badge-success"
                            : "issue-badge-error"
                        }`}
                      >
                        {stage2ReviewSummary.isPublishable ? "Ready" : "Held"}
                      </span>
                      <span className="badge issue-badge-error">
                        {stage2ReviewSummary.errors.length + stage2ReviewSummary.blockingCount} blockers
                      </span>
                    </div>

                    <p className="review-summary-text">{stage2ReviewSummary.headline}</p>
                    <p className="muted">{stage2ReviewSummary.guidance}</p>

                    {reviewPlanText ? (
                      <div className="plan-summary-actions">
                        <QuickCopyButton text={reviewPlanText} artifactKey="review-stage2" />
                      </div>
                    ) : null}

                    {stage2ReviewSummary.hasIssues ? (
                      <div className="review-issue-groups">
                        {stage2ReviewSummary.errors.length || stage2ReviewSummary.blocking.length ? (
                          <section className="review-issue-group">
                            <div className="review-issue-group-header">
                              <p className="review-issue-group-title">Blocking issues</p>
                              <span className="badge issue-badge-error">
                                {stage2ReviewSummary.errors.length + stage2ReviewSummary.blockingCount}
                              </span>
                            </div>
                            <div className="review-issue-list">
                              {stage2ReviewSummary.errors.map((issue, index) => (
                                <article key={`${issue.code}-${index}`} className="review-issue-item">
                                  <div className="review-issue-title-row">
                                    <p className="review-issue-title">{issue.title}</p>
                                    <span className="badge issue-badge-error">Error</span>
                                  </div>
                                  <p className="review-issue-message">{issue.message}</p>
                                  {issue.context ? (
                                    <p className="review-issue-context">{issue.context}</p>
                                  ) : null}
                                  {issue.snippet ? (
                                    <p className="review-issue-snippet">Line: {issue.snippet}</p>
                                  ) : null}
                                </article>
                              ))}
                              {stage2ReviewSummary.blocking.map((issue, index) => (
                                <article
                                  key={`${issue.code}-blocking-${index}`}
                                  className="review-issue-item"
                                >
                                  <div className="review-issue-title-row">
                                    <p className="review-issue-title">{issue.title}</p>
                                    <span className="badge issue-badge-error">Blocker</span>
                                  </div>
                                  <p className="review-issue-message">{issue.message}</p>
                                  {issue.context ? (
                                    <p className="review-issue-context">{issue.context}</p>
                                  ) : null}
                                  {issue.snippet ? (
                                    <p className="review-issue-snippet">Line: {issue.snippet}</p>
                                  ) : null}
                                </article>
                              ))}
                            </div>
                          </section>
                        ) : null}

                      </div>
                    ) : null}
                  </section>
                </>
              ) : null}

              <div className="support-panel">
                <div className="form-section-header">
                  <p className="kicker">Publishing hold</p>
                  <h3>Plan not yet released</h3>
                </div>
                <p className="muted">
                  This generated plan needs manual review before the athlete sees it.
                </p>
                {canApproveForRelease ? (
                  <>
                    <p className="muted">
                      Current approval source: {approvalSourceLabel}.{" "}
                      {stage2ReviewSummary.isPublishable
                        ? "Blocking validation is already clear, so approval is just a release decision."
                        : "This plan still has blocking issues, so approval here is an explicit override."}
                    </p>
                    <div className="plan-summary-actions">
                      <button
                        type="button"
                        className={stage2ReviewSummary.isPublishable ? "cta" : "ghost-button"}
                        onClick={handleApproveForRelease}
                        disabled={approvePending}
                      >
                        {approvePending ? "Approving..." : approveButtonLabel}
                      </button>
                      {canUseAdminOutputs ? (
                        <button
                          type="button"
                          className="ghost-button"
                          onClick={handleRejectApproval}
                          disabled={rejectPending}
                        >
                          {rejectPending ? "Rejecting..." : "Reject"}
                        </button>
                      ) : null}
                      {canUseAdminOutputs ? (
                        <button
                          type="button"
                          className="ghost-button"
                          onClick={handleArchivePlan}
                          disabled={archivePending}
                        >
                          {archivePending ? "Archiving..." : "Archive"}
                        </button>
                      ) : null}
                    </div>
                  </>
                ) : null}
                {approveMessage ? <div className="success-banner">{approveMessage}</div> : null}
                {approveError ? <div className="error-banner">{approveError}</div> : null}
                {rejectMessage ? <div className="success-banner">{rejectMessage}</div> : null}
                {rejectError ? <div className="error-banner">{rejectError}</div> : null}
                {archiveMessage ? <div className="success-banner">{archiveMessage}</div> : null}
                {archiveError ? <div className="error-banner">{archiveError}</div> : null}
              </div>
            </div>
          )}
        </section>
      </div>

      {canUseAdminOutputs ? (
        <div id={`admin-review-${plan.plan_id}`} className="admin-review-stack">
          <section className="viewer-panel">
            <div className="form-section-header">
              <p className="kicker">ADMIN REVIEW</p>
              <h3>{getAdminReviewHeading({ showProtectedResumeAdminReview, hasResumeApproval })}</h3>
            </div>
            {showProtectedResumeAdminReview ? (
              <>
                <p className="muted">
                  {injuryTriage?.mode === "restricted_rehab_only"
                    ? "Clinician clearance needed before planning resumes. Stage 2 finalization skipped."
                    : injuryTriage?.mode === "medical_hold"
                      ? "This intake has urgent or medically disqualifying signals. No planning until medical review is complete."
                      : "Planning paused for this intake. Stage 2 skipped until review is complete."}
                </p>
                {canRetryResumeGeneration ? (
                  <div className="support-panel support-panel-alert">
                    <div className="form-section-header">
                      <p className="kicker">Resume generation required</p>
                      <h3>{hasResumeApproval ? "Retry resume generation" : "Approve and resume generation"}</h3>
                    </div>
                
                    <p className="muted">
                      This triage plan can’t be released until admin resume completes and a real final plan replaces the stub.
                    </p>
                
                    <div className="field">
                      <label htmlFor="resume-generation-reason">Reason</label>
                      <input
                        id="resume-generation-reason"
                        type="text"
                        value={resumeReason}
                        onChange={(event) => setResumeReason(event.target.value)}
                        placeholder="Short reason"
                      />
                    </div>
                
                    <div className="plan-summary-actions">
                      <button
                        type="button"
                        className="cta"
                        onClick={handleApproveAndResumeGeneration}
                        disabled={resumePending}
                      >
                        {resumePending ? "Resuming..." : hasResumeApproval ? "Retry resume generation" : "Approve and resume generation"}
                      </button>
                    </div>
                
                    {resumeMessage ? <div className="success-banner">{resumeMessage}</div> : null}
                    {resumeError ? <div className="error-banner">{resumeError}</div> : null}
                  </div>
                ) : hasResumeApproval ? (
                  <div className="support-panel support-panel-alert">
                    <div className="form-section-header">
                      <p className="kicker">Resume unavailable</p>
                      <h3>This plan is not currently resumable</h3>
                    </div>
                    <p className="muted">
                      Resume was approved, but this plan isn’t in a resumable triage mode. Medical holds and unresolved protected states stay blocked until the intake is corrected or reviewed.
                    </p>
                    {resumeError ? <div className="error-banner">{resumeError}</div> : null}
                  </div>
                ) : null}
              </>
            ) : (
              <>
                <p className="muted">
                  Paste a manual Stage 2 final plan. It’s validated, then published if it passes or the retry prompt refreshes.
                </p>

                {canApproveForRelease ? (
                  <div className="support-panel">
                    <div className="form-section-header">
                      <p className="kicker">Quick approval</p>
                      <h3>Release the current saved plan</h3>
                    </div>
                    <p className="muted">
                      If the current saved version is good enough, approve it directly for athlete view without rerunning Stage 2. Source: {approvalSourceLabel}.
                    </p>
                    <div className="plan-summary-actions">
                      <button
                        type="button"
                        className={stage2ReviewSummary.isPublishable ? "cta" : "ghost-button"}
                        onClick={handleApproveForRelease}
                        disabled={approvePending}
                      >
                        {approvePending ? "Approving..." : approveButtonLabel}
                      </button>
                    </div>
                  </div>
                ) : null}

                {/* Approval/reject/archive feedback is rendered once, in the
                    primary "Publishing hold" panel above, to avoid showing the
                    same banner in two approval panels at the same time. */}

                <div className="field">
                  <label htmlFor="manual-stage2-final-plan">Final plan text</label>
                  <textarea
                    id="manual-stage2-final-plan"
                    rows={16}
                    value={manualPlanText}
                    onChange={(event) => setManualPlanText(event.target.value)}
                    placeholder="Paste the manual Stage 2 final plan here"
                  />
                </div>

                <div className="plan-summary-actions">
                  <button
                    type="button"
                    className="cta"
                    onClick={handleManualStage2Submit}
                    disabled={manualSubmitPending}
                  >
                    {manualSubmitPending ? "Submitting..." : "Validate and save"}
                  </button>
                </div>

                {manualSubmitMessage ? <div className="success-banner">{manualSubmitMessage}</div> : null}
                {manualSubmitError ? <div className="error-banner">{manualSubmitError}</div> : null}
              </>
            )}
          </section>

          <section className="viewer-panel">
            <div className="form-section-header">
              <p className="kicker">Stage 2 internals</p>
              <h3>Open one artifact at a time</h3>
            </div>
            <p className="muted">
              Internal notes, artifacts and validator details stay collapsed until opened.
            </p>
            <div className="accordion-list">
              {adminSections.map((section) => (
                <AdminArtifactSection
                  key={section.artifactKey}
                  artifactKey={section.artifactKey}
                  isOpen={openAdminSection === section.artifactKey}
                  onToggle={() =>
                    setOpenAdminSection((current) =>
                      current === section.artifactKey ? "" : section.artifactKey,
                    )
                  }
                  kicker={section.kicker}
                  title={section.title}
                  summary={section.summary}
                  description={section.description}
                  text={section.text}
                  filename={section.filename}
                />
              ))}
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}
