import type {
  InjuryFlagRecord,
  InjuryLoadRegion,
  StructuredBlock,
  TodayActiveInjury,
  TodayActivePlan,
  TodayCheckinBody,
  TodayCheckinPain,
  TodayCheckinPhase,
  TodayCheckinRequest,
  TodayCheckinSleep,
  TodayCommandView,
  TodayCompletionStatus,
  TodayPreviousSession,
  TodayRecommendationState,
  TodaySession,
} from "@/lib/types";
import { readInjuryImpact } from "@/lib/injury-impact";
import { athleteFacingSessionTitle } from "@/lib/plan-labels";

type TodaySafetyFlags = {
  sharp_pain: boolean;
  instability: boolean;
  swelling: boolean;
  neurological_symptoms: boolean;
  illness_symptoms: boolean;
  cannot_warm_into_movement: boolean;
  worse_next_day_pain: boolean;
};

export const TODAY_EMPTY_TITLE = "No active plan yet";
export const TODAY_EMPTY_TEXT = "Complete intake to generate your training plan.";

export function hasActivePlan(plan: TodayActivePlan | null | undefined): boolean {
  return Boolean(plan?.id);
}

// The backend appends structured fail-safe signals to Today's warnings for
// machines ("readiness_context_status=…", "reason_code:…"). The fail-safe
// itself is shown through the decision; only prose is for the athlete.
const MACHINE_WARNING_PREFIXES = ["readiness_context_status=", "reason_code:"];

export function getAthleteWarnings(warnings: readonly string[] | null | undefined): string[] {
  return (warnings ?? []).filter(
    (warning) => !MACHINE_WARNING_PREFIXES.some((prefix) => warning.startsWith(prefix)),
  );
}

/** The daily check-in is due for every athlete, with or without a plan. */
export function shouldShowTodayCheckin(state: TodayCommandView): boolean {
  return state.today.recommendation_state === "not_checked_in";
}

export function normalizeTodayPhase(phase: string | null | undefined): TodayCheckinPhase | null {
  if (phase === "GPP" || phase === "SPP" || phase === "TAPER" || phase === "REINTEGRATION") {
    return phase;
  }
  return null;
}

export function getRecommendationCopy(state: TodayRecommendationState): {
  label: string;
  icon: string;
  tone: TodayDecisionTone;
  actionText: string;
} {
  if (state === "train_as_planned") {
    return {
      label: "Train as planned",
      icon: "GO",
      tone: "green",
      actionText: "Start session and keep the work clean.",
    };
  }
  if (state === "modify") {
    return {
      label: "Adjust",
      icon: "ADJUST",
      tone: "amber",
      actionText: "Follow adjusted work and skip extras.",
    };
  }
  if (state === "pull_back") {
    return {
      label: "Pull back",
      icon: "PULL",
      tone: "red",
      actionText: "Skip hard combat work today. Use recovery or light mobility instead.",
    };
  }
  return {
    label: "Not checked in yet",
    icon: "CHK",
    tone: "neutral",
    actionText: "Submit the fast check-in to unlock today's recommendation.",
  };
}

export type TodayDecisionTone = "green" | "amber" | "red" | "neutral";

export type TodayDecisionDisplayState =
  | "go"
  | "adjust"
  | "stop"
  | "pull_back"
  | "rehab_only"
  | "no_training"
  | "injury_blocked"
  | "safety_notice"
  | "preview";

export type TodayDecisionBanner = {
  state: TodayRecommendationState;
  displayState: TodayDecisionDisplayState;
  chip: "GO" | "ADJUST" | "STOP" | "PULL BACK" | "REHAB ONLY" | "NO TRAINING" | "INJURY HOLD" | "SKIN CARE" | "CHECK" | "PREVIEW";
  /** Short coach-card headline, e.g. "Pull back today". */
  title: string;
  /** One clear reason sentence. Prefers the backend reason when present. */
  detail: string;
  /** One clear action sentence. */
  action?: string;
  /** Optional safety sentence, shown only when the backend sends one. */
  safety?: string;
  tone: TodayDecisionTone;
};

const DECISION_BANNERS: Record<
  Exclude<TodayRecommendationState, "not_checked_in">,
  { title: string; detail: string; action: string; tone: TodayDecisionTone }
> = {
  train_as_planned: {
    title: "Sharp work ready",
    detail: "Your check-in is clear for today's combat work.",
    action: "Start session and keep the work clean.",
    tone: "green",
  },
  modify: {
    title: "Session reduced",
    detail: "Hard combat work needs to be controlled today.",
    action: "Follow adjusted work and skip extras.",
    tone: "amber",
  },
  pull_back: {
    title: "Pull back today",
    detail: "Your body is not ready for hard combat work.",
    action: "Skip hard combat work today. Use recovery or light mobility instead.",
    tone: "red",
  },
};

const PREVIEW_BANNER = {
  title: "Session preview",
  detail: "This session is not open today.",
  action: "Completion opens on the matched training day.",
  tone: "neutral" as const,
};

function parseBackendAdjustment(reason: string | null | undefined): {
  title?: string;
  detail?: string;
  action?: string;
  safety?: string;
} {
  const lines = (reason ?? "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  if (lines.length >= 3) {
    return {
      title: lines[0],
      detail: lines[1],
      action: lines[2],
      safety: lines[3],
    };
  }
  if (lines.length === 2) {
    return {
      title: lines[0],
      detail: lines[1],
    };
  }
  if (lines.length === 1) {
    return {
      detail: lines[0],
    };
  }
  return {};
}

function stripTitleStop(value: string | undefined): string | undefined {
  const text = value?.trim();
  if (!text) {
    return undefined;
  }
  return text.replace(/[.!?]+$/g, "").trim();
}

function normalizeTitleKey(value: string | undefined): string {
  return stripTitleStop(value)?.toLowerCase() ?? "";
}

/**
 * Guard against the title and the body repeating the same command. When the
 * first sentence of `detail` just restates the title — e.g. title "Pull back
 * today" with detail "Pull back today. Several warnings are showing…" — drop
 * that leading sentence so the card never shows the command twice. Pure display
 * cleanup: the meaning is unchanged, and nothing is removed when it would leave
 * the body empty. Applies to every recommendation state.
 */
function stripDuplicateLeadSentence(title: string, detail: string): string {
  const trimmed = detail.trim();
  if (!trimmed) {
    return trimmed;
  }
  const match = trimmed.match(/^([^.!?]*[.!?])\s+([\s\S]+)$/);
  if (!match) {
    return trimmed;
  }
  const [, firstSentence, rest] = match;
  const remainder = rest.trim();
  if (normalizeTitleKey(firstSentence) === normalizeTitleKey(title) && remainder) {
    return remainder;
  }
  return trimmed;
}

function normalizeBackendAdjustment(
  backend: ReturnType<typeof parseBackendAdjustment>,
): ReturnType<typeof parseBackendAdjustment> {
  const title = stripTitleStop(backend.title);
  const detail = backend.detail?.trim();
  const action = backend.action?.trim();
  const safety = backend.safety?.trim();

  if (
    normalizeTitleKey(backend.title) === "sharp work only" &&
    detail === "You are in taper, so sharpness matters more than extra work today." &&
    action === "Keep speed and timing work only; remove tiring rounds."
  ) {
    return {
      title: "Sharp taper work",
      detail: "Taper phase: sharpness over extra rounds.",
      action: "Keep speed and timing clean. Remove tiring rounds.",
      safety,
    };
  }

  return {
    title,
    detail,
    action,
    safety,
  };
}

function getSafetyDisplayState(
  state: TodayRecommendationState,
  backend: ReturnType<typeof parseBackendAdjustment>,
): TodayDecisionDisplayState | null {
  if (state !== "pull_back") {
    return null;
  }

  const titleKey = normalizeTitleKey(backend.title);
  const text = [backend.title, backend.detail, backend.action, backend.safety]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();

  if (
    titleKey === "no training today" ||
    text.includes("red flag") ||
    text.includes("seek medical advice")
  ) {
    return "no_training";
  }

  if (
    titleKey === "rehab only today" ||
    text.includes("injury is worse") ||
    text.includes("pain is high")
  ) {
    return "rehab_only";
  }

  return null;
}

function getDisplayState(
  state: TodayRecommendationState,
  backend: ReturnType<typeof parseBackendAdjustment>,
  isPreview: boolean,
): TodayDecisionDisplayState | null {
  const safetyState = getSafetyDisplayState(state, backend);
  if (safetyState) {
    return safetyState;
  }

  if (isPreview) {
    return "preview";
  }

  if (state === "train_as_planned") {
    return "go";
  }
  if (state === "modify") {
    return "adjust";
  }
  if (state === "pull_back") {
    return "pull_back";
  }
  return null;
}

function getDisplayTone(displayState: TodayDecisionDisplayState): TodayDecisionTone {
  if (displayState === "go") {
    return "green";
  }
  if (displayState === "adjust") {
    return "amber";
  }
  if (displayState === "preview") {
    return "neutral";
  }
  return "red";
}

function getDisplayChip(displayState: TodayDecisionDisplayState): TodayDecisionBanner["chip"] {
  if (displayState === "go") {
    return "GO";
  }
  if (displayState === "adjust") {
    return "ADJUST";
  }
  if (displayState === "stop") {
    return "STOP";
  }
  if (displayState === "pull_back") {
    return "PULL BACK";
  }
  if (displayState === "rehab_only") {
    return "REHAB ONLY";
  }
  if (displayState === "no_training") {
    return "NO TRAINING";
  }
  return "PREVIEW";
}

/**
 * The compact decision banner shown above today's session blocks once the
 * athlete has checked in. Returns null before check-in (no decision yet). The
 * banner title is fixed and command-like; the detail prefers the backend
 * readiness reason and falls back to the canned command copy. This does not
 * mutate the plan — it only frames the original blocks as train/modify/pull-back.
 */
export function getTodayDecisionBanner(
  state: TodayRecommendationState,
  reason?: string | null,
  options: { isPreview?: boolean } = {},
): TodayDecisionBanner | null {
  const backend = normalizeBackendAdjustment(parseBackendAdjustment(reason));
  const displayState = getDisplayState(state, backend, Boolean(options.isPreview));

  if (!displayState) {
    return null;
  }

  // Past the preview check, getDisplayState only returns a truthy display
  // state for a checked-in recommendation (not_checked_in yields "preview" or
  // null), so state is guaranteed to be a DECISION_BANNERS key here.
  const banner =
    displayState === "preview"
      ? PREVIEW_BANNER
      : DECISION_BANNERS[state as Exclude<TodayRecommendationState, "not_checked_in">];

  const title = backend.title || banner.title;
  const detail = stripDuplicateLeadSentence(title, backend.detail || banner.detail);

  return {
    state,
    displayState,
    chip: getDisplayChip(displayState),
    title,
    detail,
    action: backend.action || banner.action,
    safety: backend.safety,
    tone: getDisplayTone(displayState),
  };
}

/** Athlete-facing label for an open injury flag. Today's `open_injuries` carry a
 * clean server-computed `label` (built from the shared injury synonym logic);
 * fall back to a lightly capitalized body area only when it is absent. */
function injuryFlagLabel(injury: InjuryFlagRecord): string {
  const serverLabel = injury.label?.trim();
  if (serverLabel) {
    return serverLabel;
  }
  const raw = (injury.body_area?.trim() || injury.description?.trim() || "").replace(/\s+/g, " ");
  if (!raw) {
    return "a severe injury";
  }
  return raw.charAt(0).toUpperCase() + raw.slice(1);
}

/**
 * The highest-priority active injury that must hard-block training today: any
 * non-resolved injury the athlete flagged as SEVERE. The block is driven by
 * severity, not day-status — a severe injury is still severe while it is
 * "easing" (monitoring), so monitoring status must not unblock hard training
 * on the same day. Only resolving it — a deliberate, confirmed action — or
 * downgrading its severity removes the block.
 */
export function getActiveSevereInjury(
  openInjuries: readonly InjuryFlagRecord[] | null | undefined,
): InjuryFlagRecord | null {
  return (
    (openInjuries ?? []).find(
      (injury) =>
        injury.severity === "severe" &&
        (injury.status === "open" || injury.status === "monitoring"),
    ) ?? null
  );
}

/**
 * A severe active injury is the highest-priority constraint for today and
 * supersedes the daily check-in recommendation. When one is present this returns
 * a red, training-blocking banner that names the injury and the scheduled
 * session; otherwise null, so callers fall back to the daily decision banner.
 *
 * This is a display-priority override, not a plan mutation: the stored daily
 * recommendation stays in the command view / history, but the athlete sees the
 * injury stop lead instead of a stale "load reduced" adjustment. Safety never
 * gets weaker — a severe injury can only make today more restrictive.
 */
export function getInjuryOverrideBanner(
  state: TodayCommandView,
  sessionName?: string,
): TodayDecisionBanner | null {
  // A low-cost support / filler session (mental cue, breathing/mobility reset) is
  // exempt from the injury hold — it is the safe work the hold itself prescribes,
  // so a severe injury must not block it. The backend flags this on the command view.
  if (state.today?.injury_hold_exempt) {
    return null;
  }
  const injury = getActiveSevereInjury(state.open_injuries);
  if (!injury) {
    return null;
  }
  const label = injuryFlagLabel(injury);
  const name = (sessionName ?? "").trim();
  const sessionPhrase =
    name && name.toLowerCase() !== "today's session" ? name.toLowerCase() : "this session";
  const recommendationState = state.today?.recommendation_state ?? "not_checked_in";
  return {
    state: recommendationState,
    displayState: "injury_blocked",
    chip: "CHECK",
    title: "Training caution",
    // "Can't train normally" is functional impact on the severe band, not a
    // medical severity; the hold is identical, only the wording differs.
    detail: `${
      readInjuryImpact(injury.description ?? "")?.value === "cant_train"
        ? "Can't train normally"
        : "Active severe injury"
    }: ${label}. Do not complete ${sessionPhrase} until it is cleared.`,
    // Only call out the superseded guidance when a daily recommendation actually
    // exists to supersede (i.e. the athlete has checked in).
    safety:
      recommendationState !== "not_checked_in"
        ? "Previous readiness guidance is superseded by the injury warning."
        : undefined,
    tone: "red",
  };
}

export function getCompletionLabel(status: TodayCompletionStatus): string {
  if (status === "not_started") {
    return "Not started";
  }
  if (status === "started") {
    return "Started";
  }
  if (status === "done") {
    return "Session complete";
  }
  if (status === "modified") {
    return "Session modified";
  }
  return "Session skipped";
}

export function getCompletionActions(status: TodayCompletionStatus): string[] {
  if (status === "not_started") {
    return ["Start session", "Mark skipped"];
  }
  if (status === "started") {
    return ["Resume session", "Mark done", "Mark modified", "Mark skipped"];
  }
  if (status === "done") {
    return ["Session complete"];
  }
  if (status === "modified") {
    return ["Session modified"];
  }
  return ["Session skipped"];
}

const SESSION_VALUE_LABELS: Record<string, string> = {
  hard_as_planned: "Hard sparring",
  convert_to_technical_suggested: "Technical sparring",
  deload_suggested: "Reduced sparring",
  technical_skill: "Technical skill",
  no_hard_sparring_day: "No hard sparring",
  missing_effective_sparring_plan: "Plan detail unavailable",
  hard: "Hard session",
  technical: "Technical work",
  reduced: "Reduced work",
  none: "No training load",
};

export function formatSessionValue(value: string | null | undefined): string {
  const raw = value?.trim();
  if (!raw) {
    return "";
  }
  const normalized = raw.toLowerCase();
  if (SESSION_VALUE_LABELS[normalized]) {
    return SESSION_VALUE_LABELS[normalized];
  }
  return raw
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (match) => match.toUpperCase());
}

export function completionRequiresReviewFields(status: TodayCompletionStatus): boolean {
  return status === "done" || status === "modified";
}

export function completionRequiresModificationReason(status: TodayCompletionStatus): boolean {
  return status === "modified" || status === "skipped";
}

export function getCompletionReasonLabel(status: TodayCompletionStatus): string {
  return status === "skipped" ? "Reason for skipping" : "Modification reason";
}

export function getCompletionReasonError(status: TodayCompletionStatus): string {
  return status === "skipped"
    ? "Skipped sessions need a reason."
    : "Modified sessions need a reason.";
}

export function buildTodayCheckinPayload(params: {
  /** Omitted for a plan-free check-in. */
  planId?: string | null;
  phase?: string | null;
  sleep: TodayCheckinSleep;
  body: TodayCheckinBody;
  pain: TodayCheckinPain;
  safetyFlags: TodaySafetyFlags;
}): TodayCheckinRequest {
  // A camp phase only exists with a plan, and is never invented without one.
  const phase = params.planId ? normalizeTodayPhase(params.phase) : null;
  if (params.planId && !phase) {
    throw new Error("Today phase is unavailable. Refresh Today before checking in.");
  }
  return {
    ...(params.planId && phase ? { plan_id: params.planId, phase } : {}),
    sleep: params.sleep,
    body: params.body,
    pain: params.pain,
    // active_injury / previous_session are derived on the server from Today's
    // injuries and logged sessions, so they are not asked or sent.
    ...params.safetyFlags,
  };
}

export function getSessionTitle(session: TodaySession): string {
  return athleteFacingSessionTitle(
    session.title?.trim() ||
      session.label?.trim() ||
      formatSessionValue(session.status) ||
      formatSessionValue(session.effective_load) ||
      "Today's session",
  );
}

/**
 * Human-readable focus line for a session — never a raw backend enum. Prefers
 * the plain-language focus/emphasis fields and only falls back to the humanized
 * load value, then coach copy. Shared by Today and Overview so both agree.
 */
export function getSessionFocus(session: TodaySession): string {
  const focus =
    session.primary_focus?.trim() ||
    session.emphasis?.trim() ||
    formatSessionValue(session.effective_load) ||
    session.reason?.trim() ||
    // Falls back to the session objective ("Why:" line), which is authored
    // lower-case, so capitalise the first letter to match the other sources.
    session.coach_note?.trim() ||
    "Follow the current plan guidance.";
  return focus ? focus.charAt(0).toUpperCase() + focus.slice(1) : focus;
}

/**
 * Short supporting day line, e.g. "Thu · D-17". Keeps the weekday/countdown
 * visible as a sub-label so the session type can headline the card instead.
 */
export function getSessionDayLabel(session: TodaySession): string {
  const dayText = (session.weekday_with_label || session.weekday || "").trim();
  const countdown =
    typeof session.d_day === "number" ? `D-${Math.abs(session.d_day)}` : (session.day_label || "").trim();
  const hasCountdownInDayText = Boolean(dayText && countdown && dayText.includes(countdown));
  const parts = [dayText, hasCountdownInDayText ? "" : countdown].filter(Boolean);
  return parts.join(" · ");
}

export function hasTodaySession(session: TodaySession): boolean {
  return Boolean(session.session_id || session.weekday || session.status || session.title);
}

/**
 * The calendar day the structured-plan view (Today's blocks, Plan's "Today"
 * highlight) should center on. Normally the athlete-local `trainingDay`, but once
 * the backend command view has advanced past today's session — today logged as
 * done/modified/skipped, or a coach-led / rest day that carries no app card — the
 * `next_session` it returns carries `session_relation: "next"` plus the next
 * scheduled session's `calendar_date`. Centering the structured resolver on that
 * day keeps Today and Plan aligned with Overview's "Next session" instead of
 * sticking on the completed/empty current day (the original bug: the header
 * advanced to "Next scheduled session" while the card body kept the finished
 * day). Falls back to `trainingDay` whenever the next session has no usable
 * calendar date (e.g. weekday-only undated plans).
 */
export function resolveSessionFocusDate(
  trainingDay: Date | null,
  session: TodaySession | null | undefined,
): Date | null {
  if (session?.session_relation === "next") {
    const iso = (session.calendar_date || "").slice(0, 10);
    if (iso) {
      const focus = new Date(`${iso}T12:00:00`);
      if (!Number.isNaN(focus.getTime())) {
        return focus;
      }
    }
  }
  return trainingDay;
}

export function canCompleteTodaySession(session: TodaySession): boolean {
  if (!session.session_id) {
    return false;
  }
  // An off/rest day carries a session_id (keyed on the calendar date) but no
  // real load. Never let it be started/completed — that would persist a false
  // session state. Missing load is allowed (older payloads omit the field).
  return session.effective_load?.trim().toLowerCase() !== "none";
}

export function getVisibleRiskWatch(risks: TodayCommandView["risk_watch"]): {
  visible: TodayCommandView["risk_watch"];
  overflow: number;
} {
  const visible = risks.slice(0, 2);
  return { visible, overflow: Math.max(0, risks.length - visible.length) };
}

const RISK_TIMEFRAME_LABELS: Record<
  NonNullable<TodayCommandView["risk_watch"][number]["timeframe"]>,
  string
> = {
  today: "Today",
  last_session: "Last session",
  recent_sessions: "Recent sessions",
  active: "Active",
};

/** Athlete-facing timing label for a structured risk signal. */
export function getRiskTimeframeLabel(
  timeframe?: TodayCommandView["risk_watch"][number]["timeframe"],
): string {
  return timeframe ? RISK_TIMEFRAME_LABELS[timeframe] : "";
}

/**
 * Display copy for a risk-watch row. Keyed on the backend category so the row
 * never parrots the main recommendation word-for-word ("Recommendation: pull
 * back today.") — it names the consequence instead ("Hard combat work is
 * blocked today."). Falls back to the backend text for categories without an
 * override. Pure display transform — the stored risk data is untouched.
 */
const RISK_WATCH_TEXT_OVERRIDES: Record<string, string> = {
  stop_red_flag: "Training caution: avoid hard combat work today.",
};

export function getRiskWatchText(risk: { category?: string | null; text?: string | null }): string {
  const key = (risk.category ?? "").trim();
  return RISK_WATCH_TEXT_OVERRIDES[key] || risk.text?.trim() || "Monitor this before training.";
}

// ---------------------------------------------------------------------------
// Decision hierarchy (STOP / PULL BACK / MODIFY / GREEN LIGHT)
//
// A single tier abstraction the Overview and Today screens both consume so the
// two can never disagree on the strongest decision. The tier is derived from the
// display-state the readiness engine + injury override already produce — no new
// backend signal. STOP is the hard-block tier (severe injury / red flag / rehab
// only) and always leads the page.
// ---------------------------------------------------------------------------

export type TodayDecisionTier =
  | "stop"
  | "pull_back"
  | "modify"
  | "green"
  | "preview"
  | "not_checked_in";

/** Map a resolved decision banner to its tier. Null banner (not checked in) is
 *  its own tier so the UI can prompt a check-in rather than imply a decision. */
export function getDecisionTier(banner: TodayDecisionBanner | null): TodayDecisionTier {
  if (!banner) {
    return "not_checked_in";
  }
  switch (banner.displayState) {
    case "stop":
    case "injury_blocked":
    case "no_training":
    case "rehab_only":
      return "stop";
    case "pull_back":
      return "pull_back";
    case "adjust":
      return "modify";
    case "go":
      return "green";
    case "preview":
      return "preview";
    default:
      return "not_checked_in";
  }
}

/**
 * The authoritative decision tier the whole Today UI should render from. Prefers
 * the backend-computed `today.decision_tier` (the single source of truth that
 * keeps the banner and the risk-watch footer in agreement) and only falls back to
 * the banner-derived tier for the preview framing (a client-only display concern)
 * or for older payloads that predate the field.
 */
export function resolveDecisionTier(
  today: Pick<TodayCommandView["today"], "decision_tier"> | null | undefined,
  banner: TodayDecisionBanner | null,
): TodayDecisionTier {
  const bannerTier = getDecisionTier(banner);
  // "preview" is a display framing for a future/next session, not a decision the
  // backend tier models — keep it.
  if (bannerTier === "preview") {
    return "preview";
  }
  const backendTier = today?.decision_tier;
  if (backendTier) {
    return backendTier;
  }
  return bannerTier;
}

export type TodayTierMeta = {
  /** Uppercase headline, e.g. "STOP TODAY". */
  label: string;
  /** Eyebrow above the headline. */
  eyebrow: string;
  tone: TodayDecisionTone;
};

const TIER_META: Record<TodayDecisionTier, TodayTierMeta> = {
  stop: { label: "Stop today", eyebrow: "Today's action", tone: "red" },
  pull_back: { label: "Pull back today", eyebrow: "Today's action", tone: "red" },
  modify: { label: "Modify today", eyebrow: "Today's action", tone: "amber" },
  green: { label: "Green light", eyebrow: "Today's action", tone: "green" },
  preview: { label: "Session preview", eyebrow: "Next session", tone: "neutral" },
  not_checked_in: { label: "Check in required", eyebrow: "Today's action", tone: "neutral" },
};

export function getTierMeta(tier: TodayDecisionTier): TodayTierMeta {
  return TIER_META[tier];
}

/** Reserve Today's command for a command that applies to the current day. */
export function getOverviewCommandEyebrow(tier: TodayDecisionTier): string {
  return tier === "preview" ? "Next up" : "Today's command";
}

export type OverviewPrimaryAction = { href: string; label: string };

/**
 * The single dominant CTA for the Overview command area, resolved from the
 * whole athlete state so the button can never contradict the rest of the card.
 * Priority: no active plan (build vs select) → severe-injury override → not yet
 * checked in → STOP → everything else. STOP never falls through to a "train"
 * label: with a safe replacement it routes to it, otherwise it points at the
 * stop guidance. Pure so it can be unit-tested across every state.
 */
export function getOverviewPrimaryAction(params: {
  hasActivePlan: boolean;
  planCount: number;
  hasInjuryOverride: boolean;
  recommendation: TodayRecommendationState;
  decisionTier: TodayDecisionTier;
  hasSafeSession: boolean;
  sessionIsToday: boolean;
}): OverviewPrimaryAction {
  const {
    hasActivePlan,
    planCount,
    hasInjuryOverride,
    recommendation,
    decisionTier,
    hasSafeSession,
    sessionIsToday,
  } = params;

  if (!hasActivePlan) {
    return planCount > 0
      ? { href: "/plans", label: "Select active plan" }
      : { href: "/quick-build", label: "Build your plan" };
  }
  if (hasInjuryOverride) {
    return { href: "/today#today-injury", label: "Open injury check-in" };
  }
  if (recommendation === "not_checked_in") {
    return { href: "/today#today-checkin", label: "Check in now" };
  }
  if (decisionTier === "stop") {
    return hasSafeSession
      ? { href: "/today#today-session", label: "View safe session" }
      : { href: "/today#today-session", label: "Review stop guidance" };
  }
  // A future session opens its preview on Today.
  return sessionIsToday
    ? { href: "/today#today-session", label: "Today's session" }
    : { href: "/today?preview=1", label: "Preview session" };
}

/**
 * Whether the scheduled session is TODAY (vs a future planned day).
 *
 * `session_scope` is the answer, not an input to one. The backend
 * (today_service.build_today_command_view) is the single place session timing
 * is derived — it owns rest days, multi-session days, completed sessions and
 * the roll forward to the next training day — so this reads its result rather
 * than re-deriving the day from calendar dates. When surfaces did their own
 * date maths, Overview and Today could disagree about the same payload.
 */
export function isSessionToday(
  session: Pick<TodaySession, "session_relation"> | null | undefined,
  sessionScope?: TodayCommandView["today"]["session_scope"] | null,
): boolean {
  if (sessionScope) {
    return sessionScope === "today";
  }
  // Payloads that predate session_scope still carry the per-session relation.
  return session?.session_relation === "today";
}

/**
 * Whether a session is hard combat / high-risk work (sparring, hard pads, hard
 * rounds). There is no dedicated flag, so this reads the load + status enums the
 * plan already carries. Technical / reduced / rest sessions are NOT hard combat.
 */
export function isHardCombatSession(
  session: Pick<TodaySession, "effective_load" | "status"> | null | undefined,
): boolean {
  const load = (session?.effective_load ?? "").trim().toLowerCase();
  if (load === "hard") {
    return true;
  }
  const status = (session?.status ?? "").trim().toLowerCase();
  return status === "hard_as_planned" || status === "hard";
}

export type SafeSessionView = {
  eyebrow: string;
  title: string;
  detail: string;
  allowed: string[];
  blocked: string[];
};

// ── Safe-session activity gating ─────────────────────────────────────────────
// Anatomy and injury consequence are resolved by the backend's shared injury
// system. This module consumes structured fields only; it never carries a second
// body-part synonym list or re-parses athlete-entered injury text.

type InjuryRegion = Exclude<InjuryLoadRegion, "unknown">;

function isActiveInjury(injury: InjuryFlagRecord): boolean {
  return injury.status === "open" || injury.status === "monitoring";
}

function injuryIsLoadIntolerant(injury: InjuryFlagRecord): boolean {
  return (
    injury.severity === "severe" ||
    injury.consequence === "structural" ||
    injury.consequence === "neuro"
  );
}

function hasLoadIntolerantInjuryInRegion(
  openInjuries: readonly InjuryFlagRecord[] | null | undefined,
  region: InjuryRegion,
): boolean {
  return (openInjuries ?? []).some(
    (injury) =>
      isActiveInjury(injury) &&
      injury.load_region === region &&
      injuryIsLoadIntolerant(injury),
  );
}

function hasUnclassifiedActiveInjury(
  openInjuries: readonly InjuryFlagRecord[] | null | undefined,
): boolean {
  return (openInjuries ?? []).some((injury) => {
    const region = injury.load_region;
    return isActiveInjury(injury) && (!region || region === "unknown");
  });
}

/** Whether an active lower-limb injury cannot take gait or pedal load. */
export function hasLoadIntolerantLowerLegInjury(
  openInjuries: readonly InjuryFlagRecord[] | null | undefined,
): boolean {
  return hasLoadIntolerantInjuryInRegion(openInjuries, "lower_limb");
}

function hasNeuroDownregulationInjury(
  openInjuries: readonly InjuryFlagRecord[] | null | undefined,
): boolean {
  return (openInjuries ?? []).some(
    (injury) =>
      isActiveInjury(injury) &&
      injury.load_region === "head_neck" &&
      injuryIsLoadIntolerant(injury),
  );
}

type SafeSessionPosture = "rest_only" | "downregulate" | "standard";

function resolveSafeSessionPosture(
  openInjuries?: readonly InjuryFlagRecord[] | null,
): SafeSessionPosture {
  // A structural/severe injury whose anatomy could not be classified is never a
  // green light for generic movement. Fail closed rather than guessing a limb.
  if (
    hasUnclassifiedActiveInjury(openInjuries) ||
    hasLoadIntolerantInjuryInRegion(openInjuries, "trunk_spine")
  ) {
    return "rest_only";
  }
  if (hasNeuroDownregulationInjury(openInjuries)) {
    return "downregulate";
  }
  return "standard";
}

export function resolveSafeSessionAllowed(
  openInjuries?: readonly InjuryFlagRecord[] | null,
): string[] {
  const posture = resolveSafeSessionPosture(openInjuries);
  if (posture === "rest_only") {
    return ["Breathing reset", "Clinician-approved rehab"];
  }
  if (posture === "downregulate") {
    return ["Easy mobility", "Breathing reset", "Clinician-approved rehab"];
  }

  const lowerBlocked = hasLoadIntolerantLowerLegInjury(openInjuries);
  const upperBlocked = hasLoadIntolerantInjuryInRegion(openInjuries, "upper_limb");

  let conditioning: string | null;
  if (lowerBlocked && upperBlocked) {
    conditioning = null;
  } else if (lowerBlocked) {
    conditioning = "Seated upper-body cardio, only if pain-free and available";
  } else {
    conditioning = "Light bike or walk";
  }

  const allowed = ["Easy mobility"];
  if (conditioning) {
    allowed.push(conditioning);
  }
  allowed.push("Breathing reset", "Gentle activation", "Coach-approved rehab");
  return allowed;
}

export function resolveSafeSessionBlocked(
  openInjuries?: readonly InjuryFlagRecord[] | null,
): string[] {
  const lowerBlocked = hasLoadIntolerantLowerLegInjury(openInjuries);
  const upperBlocked = hasLoadIntolerantInjuryInRegion(openInjuries, "upper_limb");
  const trunkBlocked = hasLoadIntolerantInjuryInRegion(openInjuries, "trunk_spine");
  const neuro = hasNeuroDownregulationInjury(openInjuries);
  const unclassified = hasUnclassifiedActiveInjury(openInjuries);

  let explosive: string;
  if (unclassified || (upperBlocked && lowerBlocked)) {
    explosive = "Plyos or explosive work";
  } else if (upperBlocked) {
    explosive = "Plyos or explosive upper-body work";
  } else {
    explosive = "Plyos or explosive lower-body work";
  }

  const blocked = ["Sparring", "Hard pads", "HIIT", "Heavy lifting", explosive];
  if (upperBlocked) {
    blocked.push("Overhead or pressing work");
  }
  if (trunkBlocked) {
    blocked.push("Loaded rotation or bracing");
  }
  if (neuro) {
    blocked.push("Head impact or contact drills");
  }
  if (unclassified) {
    blocked.push("Loaded movement");
  }
  return blocked;
}

const SAFE_SESSION_POSTURE_DETAIL: Record<SafeSessionPosture, string> = {
  rest_only:
    "Protect the injured area and let it settle. No loaded movement today, and follow your clinician on what is safe.",
  downregulate:
    "Keep everything calm and symptom-free today. No exertion, and follow your clinician before adding work back.",
  standard: "Protect freshness, reduce risk, and keep the body moving without adding stress.",
};

/**
 * The recovery-only session shown in place of scheduled work when today is a STOP.
 * The backend classifies each active injury; this display only applies the supplied
 * broad region and consequence fields.
 */
export function getSafeSessionView(
  blockedSessionName?: string,
  openInjuries?: readonly InjuryFlagRecord[] | null,
): SafeSessionView {
  const name = (blockedSessionName ?? "").trim();
  const blockedLead =
    name && name.toLowerCase() !== "today's session"
      ? `Training caution: reconsider ${name.toLowerCase()} today.`
      : "Training caution: avoid hard combat work today.";
  const posture = resolveSafeSessionPosture(openInjuries);
  return {
    eyebrow: "Today's safe session",
    title: posture === "rest_only" ? "Rest and recover" : "Recovery / mobility only",
    detail: `${blockedLead} ${SAFE_SESSION_POSTURE_DETAIL[posture]}`,
    allowed: resolveSafeSessionAllowed(openInjuries),
    blocked: resolveSafeSessionBlocked(openInjuries),
  };
}

/**
 * Today's countdown to the fight ("D-17"), computed from the training day and
 * fight date. Returns "" when either is missing/unparseable. Fight day is D-0.
 */
export function getCampDayLabel(
  trainingDay?: string | null,
  fightDate?: string | null,
): string {
  const toUtcNoon = (value?: string | null): number | null => {
    const iso = (value ?? "").trim().slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(iso)) {
      return null;
    }
    const ms = new Date(`${iso}T12:00:00Z`).getTime();
    return Number.isNaN(ms) ? null : ms;
  };
  const today = toUtcNoon(trainingDay);
  const fight = toUtcNoon(fightDate);
  if (today === null || fight === null) {
    return "";
  }
  const days = Math.round((fight - today) / 86_400_000);
  if (days < 0) {
    return "";
  }
  return `D-${days}`;
}

// Short "strongest signal" name per risk category, for the risk-watch footer.
const RISK_SIGNAL_LABELS: Record<string, string> = {
  stop_red_flag: "STOP",
  active_injury_worse: "INJURY",
  high_pain: "PAIN",
  weight_cut: "WEIGHT",
  fatigue: "FATIGUE",
  reminder: "REMINDER",
};

// How loud each footer signal is, so it can be clamped to never shout louder than
// the day's decision tier (a plain PULL BACK day carries a `stop_red_flag` risk,
// but its footer must not read "STOP").
const RISK_SIGNAL_STRENGTH: Record<string, number> = {
  STOP: 3,
  "PULL BACK": 2,
  INJURY: 2,
  PAIN: 2,
};
const TIER_SIGNAL_LABEL: Record<TodayDecisionTier, string> = {
  stop: "STOP",
  pull_back: "PULL BACK",
  modify: "MODIFY",
  green: "GREEN",
  preview: "",
  not_checked_in: "",
};
const TIER_SIGNAL_STRENGTH: Record<TodayDecisionTier, number> = {
  stop: 3,
  pull_back: 2,
  modify: 1,
  green: 0,
  preview: 0,
  not_checked_in: 0,
};

/**
 * Summary line for the risk-watch card. Risks arrive pre-sorted by priority, so
 * the first is the strongest signal. Returns the count and a short label for it.
 *
 * When a decision tier is supplied, the strongest-signal label is clamped so it
 * can never read louder than the tier — the footer and the banner are two views
 * of the same decision and must not contradict.
 */
export function getRiskWatchSummary(
  risks: TodayCommandView["risk_watch"] | null | undefined,
  tier?: TodayDecisionTier,
): {
  count: number;
  strongestLabel: string;
} {
  const safeRisks = risks ?? [];
  const count = safeRisks.length;
  if (!count) {
    return { count: 0, strongestLabel: "" };
  }
  const category = (safeRisks[0]?.category ?? "").trim();
  let strongestLabel =
    RISK_SIGNAL_LABELS[category] || (safeRisks[0]?.label ?? "").trim().toUpperCase() || "SIGNAL";
  if (tier) {
    const signalStrength = RISK_SIGNAL_STRENGTH[strongestLabel] ?? 0;
    if (signalStrength > TIER_SIGNAL_STRENGTH[tier]) {
      // The risk is louder than the decision — downgrade its label to the tier's
      // word (e.g. STOP → PULL BACK) so the two surfaces agree.
      strongestLabel = TIER_SIGNAL_LABEL[tier] || strongestLabel;
    }
  }
  return { count, strongestLabel };
}

/** Calm-stage recovery support ("rest from loading, move comfortably") has no
 * exercises: it is checked off as followed, never started or measured. */
export function isGuidanceOnlyRehabBlock(block: StructuredBlock | null | undefined): boolean {
  const drill = block?.drill_snapshot;
  return block?.block_type === "rehab" && !block.is_loading && drill?.rehab_stage === "calm"
    && drill.function === "recovery_downregulation" && !(drill.dose && Object.keys(drill.dose).length);
}
