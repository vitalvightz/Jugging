import {
  canCompleteTodaySession,
  getActiveSevereInjury,
  getInjuryOverrideBanner as getLegacyInjuryOverrideBanner,
  getSessionTitle,
  getTierMeta,
  getTodayDecisionBanner as getLegacyTodayDecisionBanner,
  hasTodaySession,
  isSessionToday,
  type TodayDecisionBanner,
  type TodayDecisionDisplayState,
  type TodayDecisionTier,
  type TodayDecisionTone,
} from "./today";
import type {
  TodayCommandView,
  TodayPrimarySafetyNotice,
  TodayRecommendationState,
  TodaySession,
} from "./types";

export * from "./today";

type BannerOptions = {
  isPreview?: boolean;
};

type AuthoritativeDisplayState = Extract<
  TodayDecisionDisplayState,
  "go" | "adjust" | "stop" | "pull_back" | "preview"
>;

const DISPLAY_BY_DECISION: Partial<
  Record<TodayRecommendationState, Exclude<AuthoritativeDisplayState, "preview">>
> = {
  train_as_planned: "go",
  modify: "adjust",
  pull_back: "pull_back",
};

const CHIP_BY_DISPLAY: Record<
  AuthoritativeDisplayState,
  TodayDecisionBanner["chip"]
> = {
  go: "GO",
  adjust: "ADJUST",
  stop: "STOP",
  pull_back: "PULL BACK",
  preview: "PREVIEW",
};

const TONE_BY_DISPLAY: Record<
  AuthoritativeDisplayState,
  TodayDecisionTone
> = {
  go: "green",
  adjust: "amber",
  stop: "red",
  pull_back: "red",
  preview: "neutral",
};

/**
 * Backend-authoritative Today banner adapter.
 *
 * The legacy formatter is still used for display copy only. This compatibility
 * adapter normalizes state, chip and tone from the structured recommendation;
 * the shared resolver below promotes the backend decision tier over that state.
 */
export function getTodayDecisionBanner(
  state: TodayRecommendationState,
  reason?: string | null,
  options: BannerOptions = {},
): TodayDecisionBanner | null {
  const displayState = options.isPreview ? "preview" : DISPLAY_BY_DECISION[state];
  if (!displayState) {
    return null;
  }

  const copy = getLegacyTodayDecisionBanner(state, reason, options);
  if (!copy) {
    return null;
  }

  return {
    ...copy,
    displayState,
    chip: CHIP_BY_DISPLAY[displayState],
    tone: TONE_BY_DISPLAY[displayState],
  };
}

export type AuthoritativeTodayTier = Exclude<TodayDecisionTier, "preview">;

export type TodaySessionOutcome =
  | "unchanged"
  | "guidance_only"
  | "blocked"
  | "replaced_with_recovery"
  | "replaced_with_rehab"
  | "adjusted_prescription"
  | "preview";

export type ResolvedTodayDecision = {
  recommendationState: TodayRecommendationState;
  authoritativeTier: AuthoritativeTodayTier;
  displayTier: TodayDecisionTier;
  banner: TodayDecisionBanner | null;
  /** Current-day guidance shown separately when the session below is a preview. */
  currentGuidanceBanner: TodayDecisionBanner | null;
  tone: TodayDecisionTone;
  hasSession: boolean;
  sessionIsToday: boolean;
  blocksCurrentSession: boolean;
  severeInjuryBlocksCurrentSession: boolean;
  canCompleteSession: boolean;
  useSafeReplacement: boolean;
  /** Which authority owns the lead message; session behavior remains separate. */
  primaryMessageKind: "decision" | "safety_notice" | "preview" | "none";
  /** Presentation only: describes what Today renders, never why it is safe. */
  sessionOutcome: TodaySessionOutcome;
};

const FALLBACK_TIER_BY_RECOMMENDATION: Record<
  TodayRecommendationState,
  AuthoritativeTodayTier
> = {
  train_as_planned: "green",
  modify: "modify",
  pull_back: "pull_back",
  not_checked_in: "not_checked_in",
};

type ResolvedPresentationTier = Exclude<TodayDecisionTier, "not_checked_in">;

const DISPLAY_BY_TIER: Record<ResolvedPresentationTier, AuthoritativeDisplayState> = {
  stop: "stop",
  pull_back: "pull_back",
  modify: "adjust",
  green: "go",
  preview: "preview",
};

const DEFAULT_COPY_BY_TIER: Record<
  ResolvedPresentationTier,
  { title: string; detail: string; action: string }
> = {
  stop: {
    title: "Stop today",
    detail: "A safety restriction is blocking training today.",
    action: "Today's planned session is blocked.",
  },
  pull_back: {
    title: "Pull back today",
    detail: "Your readiness is too low for hard combat work today.",
    action: "Today's planned session is blocked. Follow today's limits.",
  },
  modify: {
    title: "Follow today's limits",
    detail: "Today's session is unchanged. Follow the limits below and skip extra work.",
    action: "Follow today's limits.",
  },
  green: {
    title: "Session unchanged",
    detail: "Your check-in is clear for today's planned work.",
    action: "Complete today's planned session.",
  },
  preview: {
    title: "Session preview",
    detail: "This session is not open today.",
    action: "Completion opens on the matched training day.",
  },
};

type CanonicalSessionType =
  | "strength_power"
  | "conditioning"
  | "skill"
  | "sparring"
  | "primer"
  | "recovery"
  | "rehab"
  | "fight_or_match"
  | "mixed";

type SupportInsertCategory =
  | "tactical"
  | "mental"
  | "recovery"
  | "mobility"
  | "movement_quality"
  | "technical"
  | "footwork"
  | "recovery_walk"
  | "conditioning_maintenance";

type CanonicalBlockType =
  | "preparation"
  | "mobility_activation"
  | "plyometric_power"
  | "speed"
  | "strength"
  | "strength_speed"
  | "accessory"
  | "conditioning"
  | "skill"
  | "sparring"
  | "cooldown_recovery"
  | "nutrition"
  | "mindset"
  | "rehab";

const PREVIEW_ACTION_BY_SESSION_TYPE = {
  strength_power: "Review the lifts and loading before it opens.",
  conditioning: "Review the intervals and pace before it opens.",
  skill: "Review the drills and technical focus before it opens.",
  sparring: "Review the rounds and contact plan before it opens.",
  primer: "Review the primer and key movement cues before it opens.",
  recovery: "Review the mobility and recovery work before it opens.",
  rehab: "Review the rehab sequence and pain-free ranges before it opens.",
  fight_or_match: "Review the fight-day plan before it opens.",
  mixed: "Review the session blocks and transitions before it opens.",
} satisfies Record<CanonicalSessionType, string>;

const PREVIEW_ACTION_BY_SUPPORT_CATEGORY = {
  tactical: "Review the tactical cues before it opens.",
  mental: "Review the mindset work before it opens.",
  recovery: "Review the recovery work before it opens.",
  mobility: "Review the mobility work and pain-free ranges before it opens.",
  movement_quality: "Review the movement-quality work before it opens.",
  technical: "Review the technical drills before it opens.",
  footwork: "Review the footwork pattern before it opens.",
  recovery_walk: "Review the easy pace and route before it opens.",
  conditioning_maintenance: "Review the easy conditioning pace before it opens.",
} satisfies Record<SupportInsertCategory, string>;

const PREVIEW_ACTION_BY_BLOCK_TYPE = {
  preparation: "Review the preparation sequence before it opens.",
  mobility_activation: "Review the mobility and activation work before it opens.",
  plyometric_power: "Review the explosive work and rest periods before it opens.",
  speed: "Review the speed work and rest periods before it opens.",
  strength: "Review the lifts and loading before it opens.",
  strength_speed: "Review the power lifts and loading before it opens.",
  accessory: "Review the accessory work before it opens.",
  conditioning: "Review the intervals and pace before it opens.",
  skill: "Review the drills and technical focus before it opens.",
  sparring: "Review the rounds and contact plan before it opens.",
  cooldown_recovery: "Review the cooldown and recovery work before it opens.",
  nutrition: "Review the nutrition steps before it opens.",
  mindset: "Review the mindset cues before it opens.",
  rehab: "Review the rehab sequence and pain-free ranges before it opens.",
} satisfies Record<CanonicalBlockType, string>;

function normalizedSessionToken(value: unknown): string {
  return typeof value === "string"
    ? value.trim().toLowerCase().replace(/[\s-]+/g, "_")
    : "";
}

function getPreviewAction(session: TodaySession | null | undefined): string {
  const supportCategory = normalizedSessionToken(session?.support_insert_category);
  if (supportCategory in PREVIEW_ACTION_BY_SUPPORT_CATEGORY) {
    return PREVIEW_ACTION_BY_SUPPORT_CATEGORY[
      supportCategory as SupportInsertCategory
    ];
  }

  const sessionType = normalizedSessionToken(session?.session_type);
  if (sessionType in PREVIEW_ACTION_BY_SESSION_TYPE) {
    return PREVIEW_ACTION_BY_SESSION_TYPE[sessionType as CanonicalSessionType];
  }

  const blockActions = (session?.blocks ?? [])
    .map((block) => {
      const blockType = normalizedSessionToken(block.block_type);
      return blockType in PREVIEW_ACTION_BY_BLOCK_TYPE
        ? PREVIEW_ACTION_BY_BLOCK_TYPE[blockType as CanonicalBlockType]
        : null;
    })
    .filter((action): action is string => Boolean(action));
  const uniqueBlockActions = [...new Set(blockActions)];
  if (uniqueBlockActions.length === 1) {
    return uniqueBlockActions[0] ?? DEFAULT_COPY_BY_TIER.preview.action;
  }
  if (uniqueBlockActions.length > 1) {
    return PREVIEW_ACTION_BY_SESSION_TYPE.mixed;
  }

  return DEFAULT_COPY_BY_TIER.preview.action;
}

function getPreviewCopy(
  session: TodaySession | null | undefined,
): { title: string; detail: string; action: string } {
  if (!session || !hasTodaySession(session)) {
    return DEFAULT_COPY_BY_TIER.preview;
  }

  return {
    title: DEFAULT_COPY_BY_TIER.preview.title,
    detail: `${getSessionTitle(session)} is next on your plan.`,
    action: getPreviewAction(session),
  };
}

const HARD_STOP_COPY = [
  /\bno training today\b/i,
  /\btraining is not safe today\b/i,
  /\bstop training and seek medical advice\b/i,
  /\bsession blocked\b/i,
  /\bactive severe injury\b/i,
  /\brehab only today\b/i,
];
const UNRESTRICTED_COPY = /\b(?:train normally|train as planned|everything feels good)\b/i;

function containsHardStopCopy(copy: TodayDecisionBanner): boolean {
  const text = [copy.title, copy.detail, copy.action, copy.safety]
    .filter(Boolean)
    .join(" ");
  return HARD_STOP_COPY.some((pattern) => pattern.test(text));
}

/**
 * Return backend presentation copy only when its structured recommendation can
 * safely sit beneath the authoritative tier. Copy never changes the tier or any
 * session behaviour resolved below.
 */
export function getCompatibleRecommendationCopy(
  recommendationState: TodayRecommendationState,
  authoritativeTier: AuthoritativeTodayTier,
  recommendationReason?: string | null,
): TodayDecisionBanner | null {
  if (!recommendationReason?.trim()) {
    return null;
  }

  const copy = getLegacyTodayDecisionBanner(
    recommendationState,
    recommendationReason,
    { isPreview: false },
  );
  if (!copy) {
    return null;
  }

  const expectedTier = FALLBACK_TIER_BY_RECOMMENDATION[recommendationState];
  if (expectedTier === authoritativeTier) {
    if (
      (authoritativeTier === "green" || authoritativeTier === "modify") &&
      containsHardStopCopy(copy)
    ) {
      return null;
    }
    if (
      authoritativeTier === "pull_back" &&
      UNRESTRICTED_COPY.test(
        [copy.title, copy.detail, copy.action, copy.safety].filter(Boolean).join(" "),
      )
    ) {
      return null;
    }
    return copy;
  }

  // A pull-back recommendation may contain the stronger stop instruction that
  // caused the backend to promote the authoritative tier to STOP.
  if (
    recommendationState === "pull_back" &&
    authoritativeTier === "stop" &&
    containsHardStopCopy(copy)
  ) {
    return copy;
  }
  return null;
}

function resolvePresentationBanner(
  recommendationState: TodayRecommendationState,
  displayTier: TodayDecisionTier,
  recommendationReason?: string | null,
  injuryPresentation?: TodayDecisionBanner | null,
  previewSession?: TodaySession | null,
): TodayDecisionBanner | null {
  if (displayTier === "not_checked_in") {
    return null;
  }

  const displayState = DISPLAY_BY_TIER[displayTier];
  const fallback = DEFAULT_COPY_BY_TIER[displayTier];

  // A planning preview never borrows current recommendation copy. Current
  // safety notices are selected separately in resolveTodayDecision before this
  // fallback, which stops session timing from suppressing wound care.
  if (displayTier === "preview") {
    const copy = getPreviewCopy(previewSession);
    return {
      state: recommendationState,
      displayState,
      chip: CHIP_BY_DISPLAY[displayState],
      ...copy,
      tone: TONE_BY_DISPLAY[displayState],
    };
  }

  const recommendationCopy = getCompatibleRecommendationCopy(
    recommendationState,
    displayTier,
    recommendationReason,
  );
  const injuryCopy = displayTier === "stop" ? injuryPresentation ?? null : null;

  return {
    state: recommendationState,
    displayState,
    chip: CHIP_BY_DISPLAY[displayState],
    // The fixed title/chip communicate the tier. Backend prose supplies the
    // useful athlete-specific instruction without becoming a safety authority.
    title: fallback.title,
    detail: injuryCopy?.detail ?? recommendationCopy?.detail ?? fallback.detail,
    action:
      recommendationCopy?.action ?? injuryCopy?.action ?? fallback.action,
    safety: recommendationCopy?.safety ?? injuryCopy?.safety,
    tone: TONE_BY_DISPLAY[displayState],
  };
}

function getPrimarySafetyNoticeBanner(
  recommendationState: TodayRecommendationState,
  notice: TodayPrimarySafetyNotice,
): TodayDecisionBanner {
  return {
    state: recommendationState,
    displayState: "safety_notice",
    chip: notice.chip,
    title: notice.title,
    detail: notice.detail,
    action: notice.action,
    tone: notice.tone,
  };
}

function scopeToCurrentGuidance(banner: TodayDecisionBanner | null): TodayDecisionBanner | null {
  if (!banner) {
    return null;
  }
  // The current decision may carry useful injury care or load instructions.
  // Remove green workout commands and references to a session absent today.
  const action = banner.displayState === "go" ||
    (banner.action && /\bsession\b|\bplanned work\b/i.test(banner.action))
    ? undefined
    : banner.action;
  return { ...banner, action };
}

/**
 * Resolve Today once for both presentation and session safety.
 *
 * The backend tier is authoritative. Older payloads fall back only to the
 * structured recommendation state; rendered titles, reasons and actions are
 * deliberately absent from every safety decision below.
 */
export function resolveTodayDecision(state: TodayCommandView): ResolvedTodayDecision {
  const recommendationState = state.today.recommendation_state;
  const authoritativeTier =
    state.today.decision_tier ?? FALLBACK_TIER_BY_RECOMMENDATION[recommendationState];
  const hasSession = hasTodaySession(state.today.next_session);
  const sessionIsToday = isSessionToday(state.today.next_session, state.today.session_scope);
  const isPreview = !hasSession || !sessionIsToday;
  const displayTier: TodayDecisionTier = isPreview ? "preview" : authoritativeTier;
  // Injury data can make a backend-authoritative STOP more specific, but it
  // never creates the STOP. This preserves truthful injury presentation while
  // keeping the server decision tier as the sole safety authority.
  const injuryPresentation =
    authoritativeTier === "stop" && !isPreview
      ? getLegacyInjuryOverrideBanner(
          state,
          hasSession ? getSessionTitle(state.today.next_session) : undefined,
        )
      : null;
  const prescription = sessionIsToday ? state.live_prescription : null;
  const reviewedRehabAllowed = prescription?.rehab_only === true && !prescription.safety_hold
    && authoritativeTier !== "stop" && authoritativeTier !== "not_checked_in";
  const useSafeReplacement =
    authoritativeTier === "stop" && hasSession && sessionIsToday && !prescription;
  const sessionOutcome: TodaySessionOutcome = isPreview
    ? "preview"
    : prescription?.safety_hold
      ? "blocked"
      : prescription?.rehab_only
        ? "replaced_with_rehab"
        : prescription?.changes.length
          ? "adjusted_prescription"
    : authoritativeTier === "green" || authoritativeTier === "not_checked_in"
      ? "unchanged"
      : authoritativeTier === "modify"
        ? "guidance_only"
        : authoritativeTier === "stop" && useSafeReplacement
          ? "replaced_with_recovery"
          : "blocked";
  // A current wound instruction outranks planning-only preview copy. It also
  // replaces a green/no-check-in lead because those carry no more restrictive
  // session command. Modify/pull-back/stop still lead for today's session, with
  // the backend recommendation retaining any additive wound restriction.
  const safetyNoticeLeads = Boolean(state.today.primary_safety_notice) &&
    (isPreview || authoritativeTier === "green" || authoritativeTier === "not_checked_in");
  const banner = safetyNoticeLeads && state.today.primary_safety_notice
    ? getPrimarySafetyNoticeBanner(recommendationState, state.today.primary_safety_notice)
    : resolvePresentationBanner(
        recommendationState,
        displayTier,
        state.today.recommendation_reason,
        injuryPresentation,
        state.today.next_session,
      );
  // A future workout must stay a neutral preview. The current-day decision can
  // still explain the athlete's check-in or injury hold in its own scoped panel.
  const currentGuidanceBanner = isPreview && !safetyNoticeLeads &&
    authoritativeTier !== "not_checked_in"
    ? scopeToCurrentGuidance(resolvePresentationBanner(
        recommendationState,
        authoritativeTier,
        state.today.recommendation_reason,
        authoritativeTier === "stop"
          ? getLegacyInjuryOverrideBanner(state, "today's training")
          : null,
      ))
    : null;
  const primaryMessageKind: ResolvedTodayDecision["primaryMessageKind"] = banner
    ? banner.displayState === "safety_notice"
      ? "safety_notice"
      : banner.displayState === "preview"
        ? "preview"
        : "decision"
    : "none";
  const tone = banner?.tone ?? getTierMeta(displayTier).tone;
  // The backend decides once (session_hold). The local rule is only a fallback
  // for an older API response without that field.
  const blocksCurrentSession =
    sessionIsToday &&
    // A missing check-in is "check in first", never a block; canCompleteSession gates it.
    (state.session_hold
      ? state.session_hold.held && state.session_hold.source !== "check_in"
      : Boolean(prescription?.safety_hold) || authoritativeTier === "stop" || (authoritativeTier === "pull_back" && !reviewedRehabAllowed));
  const severeInjuryBlocksCurrentSession =
    blocksCurrentSession &&
    !state.today.injury_hold_exempt &&
    Boolean(getActiveSevereInjury(state.open_injuries));
  // session_hold answers check-in too: it is only required when it can be given (consent).
  const canCompleteSession =
    canCompleteTodaySession(state.today.next_session) &&
    sessionIsToday &&
    (state.session_hold
      ? !state.session_hold.held
      : authoritativeTier !== "not_checked_in" && !blocksCurrentSession);

  return {
    recommendationState,
    authoritativeTier,
    displayTier,
    banner,
    currentGuidanceBanner,
    tone,
    hasSession,
    sessionIsToday,
    blocksCurrentSession,
    severeInjuryBlocksCurrentSession,
    canCompleteSession,
    useSafeReplacement,
    primaryMessageKind,
    sessionOutcome,
  };
}

/** Remove only warnings already expressed by today's authoritative main card. */
export function getSupplementaryRiskWatch(
  risks: TodayCommandView["risk_watch"] | null | undefined,
  decision: ResolvedTodayDecision,
): TodayCommandView["risk_watch"] {
  const severeInjuryStop =
    decision.authoritativeTier === "stop" &&
    decision.sessionIsToday &&
    decision.severeInjuryBlocksCurrentSession;
  const mainDecisionAlreadyShowsBlock =
    decision.sessionIsToday &&
    (decision.authoritativeTier === "pull_back" ||
      decision.authoritativeTier === "stop");
  return (risks ?? []).filter(
    (risk) =>
      !(risk.category === "stop_red_flag" && mainDecisionAlreadyShowsBlock) &&
      !(risk.category === "active_injury_worse" && severeInjuryStop),
  );
}

const CURRENT_TRIGGER_LABELS_BY_RISK: Partial<Record<string, ReadonlySet<string>>> = {
  high_pain: new Set(["high pain"]),
  fatigue: new Set([
    "poor sleep",
    "poor sleep for 3 days",
    "feeling flat",
    "feeling flat for 3 days",
    "low readiness lately",
  ]),
  active_injury_worse: new Set(["injury getting worse", "active injury"]),
};

/**
 * Remove a current-day risk row only when the visible decision trigger already
 * says the same thing. Historical/active rows stay because their timing, safety
 * guidance, or injury-manager action adds information the trigger does not.
 */
export function getDistinctTodayRiskWatch(
  risks: TodayCommandView["risk_watch"] | null | undefined,
  visibleTriggerLabels: string[] | null | undefined,
): TodayCommandView["risk_watch"] {
  const visibleTriggers = new Set(
    (visibleTriggerLabels ?? []).map((label) => label.trim().toLowerCase()).filter(Boolean),
  );
  if (!visibleTriggers.size) {
    return risks ?? [];
  }
  return (risks ?? []).filter((risk) => {
    if (risk.timeframe !== "today") {
      return true;
    }
    const equivalentTriggers = CURRENT_TRIGGER_LABELS_BY_RISK[risk.category];
    if (!equivalentTriggers) {
      return true;
    }
    return !Array.from(equivalentTriggers).some((label) => visibleTriggers.has(label));
  });
}

/**
 * The standing "Tracking 1 open injury: …" reminder repeats what the Injuries
 * tile and the injury card below already say, so Today drops it whenever those
 * are on screen. Only that soft reminder goes: a worsening or severe injury
 * (`active_injury_worse`), the skin-care reminder ("keep it clean and
 * covered") and every other reminder stay.
 */
export function withoutInjuryTrackingReminder(
  risks: TodayCommandView["risk_watch"] | null | undefined,
  openInjuryCount: number,
): TodayCommandView["risk_watch"] {
  if (openInjuryCount <= 0) {
    return risks ?? [];
  }
  return (risks ?? []).filter(
    (risk) =>
      !(
        risk.category === "reminder" &&
        risk.timeframe === "active" &&
        /^tracking \d+ open injur/i.test(risk.text?.trim() ?? "")
      ),
  );
}

/**
 * Preserve the legacy presentation helper for consumers outside the shared
 * resolver. Its result is display data only and must never determine session
 * safety; resolveTodayDecision keeps that authority with decision_tier.
 */
export function getInjuryOverrideBanner(
  state: TodayCommandView,
  sessionName?: string,
): TodayDecisionBanner | null {
  return getLegacyInjuryOverrideBanner(state, sessionName);
}

// Fallback titles when the stored copy cannot sit under the authoritative tier.
// None of them refers to a session, a plan or a clearance.
const PLAN_FREE_TITLE_BY_TIER: Partial<Record<AuthoritativeTodayTier, string>> = {
  green: "No warning signs today",
  modify: "Ease off today",
  pull_back: "Pull back today",
};

/**
 * Today's lead message for an athlete without a programme.
 *
 * Presentation only: the tier, chip and tone stay exactly as
 * resolveTodayDecision set them, and a safety notice or STOP keeps its own
 * copy. Below that, the session-based fixed titles ("Session unchanged") are
 * replaced by the backend's own plan-free wording, when it is compatible with
 * the tier, so nothing describes a prescribed session that does not exist.
 */
export function getPlanFreeGuidanceBanner(
  state: TodayCommandView,
  decision: ResolvedTodayDecision,
): TodayDecisionBanner | null {
  if (decision.primaryMessageKind === "safety_notice") {
    return decision.banner;
  }
  const banner = decision.currentGuidanceBanner;
  if (!banner || decision.authoritativeTier === "stop") {
    return banner;
  }
  const copy = getCompatibleRecommendationCopy(
    state.today.recommendation_state,
    decision.authoritativeTier,
    state.today.recommendation_reason,
  );
  const action = copy?.action && !/\bsession\b|\bplanned work\b/i.test(copy.action) ? copy.action : undefined;
  return {
    ...banner,
    title: copy?.title || PLAN_FREE_TITLE_BY_TIER[decision.authoritativeTier] || banner.title,
    detail: copy?.detail || banner.detail,
    action,
    safety: copy?.safety ?? banner.safety,
  };
}
