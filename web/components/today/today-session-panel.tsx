"use client";

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from "react";

import { ExerciseMediaProvider } from "@/components/exercise-demo";
import { useFadeThrough } from "@/components/fade-through";
import { Skeleton } from "@/components/skeleton";
import { AutoHeight } from "@/components/today/auto-height";
import { Collapse } from "@/components/today/collapse";
import { PrivateExerciseMediaProvider } from "@/components/private-exercise-video";
import { ExerciseLogProvider, type ExerciseLogging } from "@/components/exercise-log";
import { ExerciseCustomisationProvider, type ExerciseCustomisation } from "@/components/exercise-customisation";
import { SessionFeedbackPrompt } from "@/components/feedback/session-feedback-prompt";
import {
  SessionCompletionForm,
  type CompletionIntent,
} from "@/components/session-completion-form";
import {
  DaySessionContext,
  ExerciseRationaleProvider,
  RehabLabelProvider,
  SessionCard as StructuredSessionCard,
  SessionlessDayCard,
} from "@/components/structured-plan-renderer";
import { formatTrainingDay } from "@/components/today/format";
import { InjuryDetailSheet } from "@/components/today/injury-detail-sheet";
import { GuidedVisualisation } from "@/components/guided-visualisation/guided-visualisation";
import {
  CONTACT_RUN_KEY_PREFIX,
  pruneSavedTodayRuns,
  SESSION_RUN_KEY_PREFIX,
  useRoundTimer,
} from "@/components/session-timer/round-timer-provider";
import { SessionTimer, type SessionTimerSummary } from "@/components/session-timer/session-timer";
import { clearSavedRun, hasSavedRun } from "@/components/session-timer/use-session-timer";
import { SessionReview } from "@/components/today/session-review";
import { SparringLogPrompt, type SparringDraft } from "@/components/today/sparring-log-prompt";
import { useTodayExerciseLogs } from "@/components/today/use-exercise-logs";
import { useTrainingFocus } from "@/components/today/use-training-focus";
import { relevantFocus, upcomingSport } from "@/lib/training-focus";
import { useToast } from "@/components/toast-provider";
import { submitTodaySessionCompletion } from "@/lib/api";
import { offerPushNotifications } from "@/lib/push-prompt";
import { timerAudio } from "@/lib/session-timer/audio";
import {
  CONTACT_FORMAT_MEMORY_KEY,
  contactRoundsItem,
  contactTimerTarget,
  contactTitle,
  savedRoundFormat,
  type ContactTimerTarget,
} from "@/lib/session-timer/contact";
import { isLoggableBlock, withLastLoad } from "@/lib/exercise-log";
import { timerExerciseLogs } from "@/lib/session-timer/logs";
import { cleanText, getBlocks, isZeroLoadSupportSession } from "@/lib/structured-plan";
import { dayTimerItems, timerSessionFor, withoutSparringItems, type TimerItem } from "@/lib/session-timer/plan";
import { fightVisualisationFromSession, firstNameOf } from "@/lib/fight-visualisation/script";
import { fightLevel } from "@/lib/fight-visualisation/crowd";
import {
  primarySessionOf,
  resolveCurrentDay,
  resolveOpenPlanWeekNumber,
  sessionIdentity,
  type CurrentDayResolution,
} from "@/lib/camp-map";
import type { TodayPlanSchedule } from "@/components/today/use-today-command";
import { openBlockWeekIntent, type OpenBlockWeekIntent } from "@/lib/open-block";
import { isOpenOngoingPlan } from "@/lib/plan-format";
import { athleteFacingSessionTitle, formatPlanLabel, humanizeIfRawEnum } from "@/lib/plan-labels";
import { isCalmRehabBlock, resolveSessionTypeLabel } from "@/lib/rehab-label";
import { shouldPromptSessionFeedback } from "@/lib/session-feedback";
import { useTrainingDay } from "@/lib/use-training-day";
import {
  getCompletionLabel,
  getSafeSessionView,
  getSessionFocus,
  getSessionTitle,
  isGuidanceOnlyRehabBlock,
  isHardCombatSession,
  resolveTodayDecision,
  resolveSessionFocusDate,
  type SafeSessionView,
} from "@/lib/today";
import type {
  ExerciseMedia,
  RehabLabelPolicy,
  SparringPlannedIntensity,
  StructuredDay,
  StructuredPlan,
  StructuredSession,
  TodayCommandView,
  TodayCompletionStatus,
  TodaySession,
} from "@/lib/types";
import { XP_TOAST_KEYS } from "@/lib/xp-events";

function formatSessionDate(session: TodaySession): string {
  const dayText = session.weekday_with_label || session.weekday;
  const countdown =
    typeof session.d_day === "number" ? `D-${Math.abs(session.d_day)}` : session.day_label;
  // The canonical date already carries the short weekday, so only fall back to
  // the raw weekday token when there's no calendar date to format.
  const dayPart = session.calendar_date ? formatTrainingDay(session.calendar_date) : dayText;
  const hasCountdownInDayPart = Boolean(dayPart && countdown && dayPart.includes(countdown));
  const parts = [dayPart, hasCountdownInDayPart ? null : countdown].filter(Boolean);
  return parts.length ? parts.join(" / ") : "Athlete-local training day";
}

function getSessionDuration(session: TodaySession): string | null {
  if (typeof session.estimated_duration === "string" && session.estimated_duration.trim()) {
    return session.estimated_duration.trim();
  }
  if (typeof session.estimated_duration === "number" && Number.isFinite(session.estimated_duration)) {
    return `${session.estimated_duration} min`;
  }
  if (typeof session.duration_minutes === "number" && Number.isFinite(session.duration_minutes)) {
    return `${session.duration_minutes} min`;
  }
  if (session.planned_duration?.display) {
    return session.planned_duration.display;
  }
  if (typeof session.planned_duration?.value === "number") {
    return `${session.planned_duration.value} ${session.planned_duration.unit || "min"}`;
  }
  return null;
}

/** `estimate` marks a figure the plan states but nothing measured: shown "~". */
type SummaryChip = { icon: ToolIconName; label: string; estimate?: boolean };

const LOAD_LABELS: Record<string, string> = {
  hard: "Hard",
  technical: "Technical",
  reduced: "Reduced load",
  none: "Zero load",
};

/** At-a-glance chips under the session headline: total time, how hard it
 * loads the body, and the exercise count. Minutes are summed only when every
 * session states them; otherwise the backend duration stands. */
function sessionSummaryChips(
  sessions: StructuredSession[],
  fallbackDuration: string | null,
  effectiveLoad: string | null | undefined,
): SummaryChip[] {
  const minutes = sessions.map((item) => {
    const measured = item.planned_duration;
    return measured && typeof measured.value === "number" && /^min/i.test(measured.unit ?? "")
      ? measured.value
      : null;
  });
  const totalMinutes = sessions.length > 0 && minutes.every((value) => value !== null)
    ? minutes.reduce<number>((sum, value) => sum + (value ?? 0), 0)
    : null;
  const exercises = sessions.reduce((sum, item) => sum + getBlocks(item).length, 0);
  const loadKey = textValue(effectiveLoad).toLowerCase();
  const load = sessions.length > 0 && sessions.every(isZeroLoadSupportSession)
    ? "Zero load"
    : loadKey
      ? LOAD_LABELS[loadKey] ?? formatPlanLabel(loadKey)
      : "";
  // Session time is the plan's own estimate, never derived from the
  // prescription, so it always reads as approximate.
  const time = totalMinutes ? `${totalMinutes} min` : fallbackDuration?.replace(/^~\s*/, "") ?? null;
  const chips: Array<SummaryChip | null> = [
    time ? { icon: "clock", label: time, estimate: true } : null,
    load ? { icon: "load", label: load } : null,
    exercises > 0 ? { icon: "exercises", label: `${exercises} exercise${exercises === 1 ? "" : "s"}` } : null,
  ];
  return chips.filter((chip): chip is SummaryChip => chip !== null);
}

function SummaryChipList({ chips }: { chips: SummaryChip[] }) {
  return (
    <ul className="today-session-chips today-fade-in" aria-label="Session summary">
      {chips.map((chip) => (
        <li key={chip.label}>
          <ToolIcon name={chip.icon} />
          <span>
            {chip.estimate ? <><span aria-hidden="true">~</span><span className="sr-only">About </span></> : null}
            {chip.label}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** A part's type in a word or two ("Strength & Power" reads "Strength"). */
function shortSessionType(session: StructuredSession, policy: RehabLabelPolicy | null | undefined): string {
  const label = resolveSessionTypeLabel(session, policy) || formatPlanLabel(textValue(session.session_type));
  return label.split(/\s*[&/]\s*/)[0].trim();
}

/** "Right achilles tendonitis + Chest strain": what today's work is built around. */
function injuryStripText(injuries: TodayCommandView["open_injuries"]): string {
  const labels = injuries
    .map((injury) => textValue(injury.label) || textValue(injury.body_area))
    .filter(Boolean);
  return Array.from(new Set(labels)).join(" + ");
}

/** Which timer is running: today's planned session, or today's coach-led contact
 * (sparring the athlete declared, with no app session to start). Plain rounds
 * run on the app-wide round timer. */
type TimerSource = "session" | "contact";

const CONTACT_LOCK_COPY = {
  not_checked_in: "Sparring rounds unlock after check-in",
  clinician_clearance: "Sparring rounds locked by clinician clearance",
  head_impact: "No more contact today after your head-impact report",
  blocked: "Sparring rounds locked today",
};

const PLANNED_SPARRING_INTENSITY: Record<ContactTimerTarget["kind"], SparringPlannedIntensity> = {
  sparring: "hard",
  // A declared Light Combat day is only known to be technical work.
  light_combat: "technical",
  technical: "technical",
  coach_led: "contact",
};

type ToolIconName = "bell" | "timer" | "lock" | "check" | "adjust" | "skip" | "voice" | "play" | "shield" | "clock" | "load" | "exercises";

const TOOL_ICON_PATHS: Record<ToolIconName, ReactNode> = {
  // A ring bell: sparring rounds.
  bell: <path d="M6 16h12M8 16a4 4 0 118 0M12 8V6M10 19h4" />,
  timer: <path d="M12 13V9.5M9.5 3h5M12 21a8 8 0 100-16 8 8 0 000 16zM18.5 6.5l1.5-1.5" />,
  lock: <path d="M7 11V8a5 5 0 0110 0v3M6 11h12v9H6z" />,
  check: <path d="M5 12.5l4.5 4.5L19 7.5" />,
  adjust: <path d="M4 8h9M17 8h3M4 16h3M11 16h9M15 6v4M9 14v4" />,
  skip: <path d="M6 6l12 12M18 6L6 18" />,
  // A speaking head: the voice-guided visualisation.
  voice: <path d="M15 8a5 5 0 00-10 0c0 2 1 3 1 5v3h3l1 2h2v-4M18 9a4 4 0 010 6M20.5 7a7 7 0 010 10" />,
  play: <path d="M12 21a9 9 0 100-18 9 9 0 000 18zM10 8.5v7l5.5-3.5z" />,
  // Injury-aware session: the plan is built around open injuries.
  shield: <path d="M12 3l7 3v5c0 4.5-3 8.2-7 10-4-1.8-7-5.5-7-10V6zM9 12l2 2 4-4" />,
  clock: <path d="M12 21a9 9 0 100-18 9 9 0 000 18zM12 7.5V12l3 2" />,
  // Rising bars: how hard the day loads the body.
  load: <path d="M6 19v-4M12 19V10M18 19V5" />,
  // A dumbbell: the number of exercises.
  exercises: <path d="M3 12h18M6 8v8M9 6v12M15 6v12M18 8v8" />,
};

function ToolIcon({ name }: { name: ToolIconName }) {
  return (
    <svg className="today-tool-icon" viewBox="0 0 24 24" aria-hidden="true">
      <g fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        {TOOL_ICON_PATHS[name]}
      </g>
    </svg>
  );
}

/** Saved timer runs only change through this tab's own actions, which re-render. */
function subscribeToNothing(): () => void {
  return () => undefined;
}

function textValue(value: string | null | undefined): string {
  return typeof value === "string" ? value.trim() : "";
}

function getStructuredTodaySessionTitle(current: CurrentDayResolution, sessionId?: string): string {
  const session = current.sessions.find((entry) => sessionId && entry.session_id === sessionId)
    ?? primarySessionOf(current.day);
  const card = current.day?.today_card;
  return athleteFacingSessionTitle(
    textValue(session?.title) ||
      textValue(card?.headline) ||
      humanizeIfRawEnum(textValue(session?.session_type)),
  );
}

function sameTitle(a: string, b: string): boolean {
  const normalize = (value: string) => value.trim().toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  return Boolean(normalize(a)) && normalize(a) === normalize(b);
}

function getSessionRelationCopy(
  session: TodaySession,
  completionStatus: TodayCompletionStatus,
  sessionIsToday: boolean,
): {
  kicker: string;
  status: string;
  helper: string;
} {
  // The authoritative resolver owns "today vs next". It normally follows the
  // backend relation, but rejects an impossible calendar-date mismatch so a
  // stale payload cannot label or unlock future work as today's session.
  if (!sessionIsToday) {
    const loggedToday =
      completionStatus === "done" ||
      completionStatus === "modified" ||
      completionStatus === "skipped";
    return {
      kicker: "Next session",
      status: "Preview",
      helper: loggedToday
        ? "Today's session is logged, so this shows your next session."
        : "Today has no matched training card, so this shows the next available plan day.",
    };
  }
  return {
    kicker: "Today's session",
    status: "Live today",
    helper: "Matched from the active plan by the athlete-local training day.",
  };
}

/**
 * The recovery/mobility-only card shown in place of the scheduled blocks when
 * today is a STOP — Today never displays hard combat as available under a stop.
 */
function SafeSessionCard({ view }: { view: SafeSessionView }) {
  return (
    <div className="today-safe-session" data-tone="red">
      <p className="today-safe-session-eyebrow">{view.eyebrow}</p>
      <p className="today-safe-session-title">{view.title}</p>
      <p className="today-safe-session-detail">{view.detail}</p>
      <div className="today-safe-session-lists">
        <div className="today-safe-list" data-kind="allowed">
          <p className="today-safe-list-label">Allowed</p>
          <ul>{view.allowed.map((item) => <li key={item}>{item}</li>)}</ul>
        </div>
        <div className="today-safe-list" data-kind="blocked">
          <p className="today-safe-list-label">Blocked</p>
          <ul>{view.blocked.map((item) => <li key={item}>{item}</li>)}</ul>
        </div>
      </div>
    </div>
  );
}

/**
 * Today's exact blocks, resolved from the active plan's structured_plan via the
 * same shared resolver and the same SessionCard component Plan Detail renders.
 * This guarantees Today and Plan Detail agree on the current day, its sessions
 * and their counts — Today simply scopes the view to today's day only (no week
 * strip, no other days, no full camp map).
 */
export function TodaySessionBlocks({
  planId,
  current,
  headline,
  openWeekIntent,
  rehabLabelPolicy,
  exerciseMedia,
  exerciseLogging = null,
  accessToken,
  exerciseCustomisation = null,
}: {
  planId?: string;
  current: CurrentDayResolution;
  /** Swap / add / remove on the shown workout; null where it can't change. */
  exerciseCustomisation?: ExerciseCustomisation | null;
  /** Heading already shown by the surrounding Today card. */
  headline?: string;
  /** Development-block week intent of an open (renewable) plan: headlines where
   * today sits in the block and forwards the per-block directive to the cards. */
  openWeekIntent?: OpenBlockWeekIntent | null;
  /** Per-region Rehab/Prehab policy from the active plan. Today renders
   * SessionCard directly rather than through Plan Detail's full-plan renderer,
   * so it has to mount the provider itself; without it every rehab block on this
   * screen read "Rehab" no matter which injuries had cleared. */
  rehabLabelPolicy?: RehabLabelPolicy | null;
  /** Curated demo videos keyed by block display_name; same reason as above. */
  exerciseMedia?: Record<string, ExerciseMedia> | null;
  /** In-session logging for the exercise rows; null until today's session is
   * started, and whenever the card shows a day other than today. */
  exerciseLogging?: ExerciseLogging | null;
  accessToken?: string | null;
}) {
  if (!current.inRange || !current.day) {
    return null;
  }
  // A renewable plan may resolve today's weekday against a projected template
  // row from another month. Keep that row for plan identity, but never present
  // its projected date as today's calendar date.
  const displayDay =
    current.matchType === "weekday" && current.trainingDayISO
      ? { ...current.day, date: current.trainingDayISO }
      : current.day;
  const weekIntentNote = openWeekIntent ? (
    <p className="today-open-week-note">
      <span className="sp-tag sp-accent">
        Week {openWeekIntent.weekNumber} · {openWeekIntent.label}
      </span>
      {openWeekIntent.summary}
    </p>
  ) : null;
  if (current.sessions.length === 0) {
    return (
      <div className="today-blocks">
        {weekIntentNote}
        <SessionlessDayCard day={displayDay} />
      </div>
    );
  }
  return (
    <RehabLabelProvider policy={rehabLabelPolicy}>
    <PrivateExerciseMediaProvider token={accessToken}>
    <ExerciseMediaProvider media={exerciseMedia}>
    <ExerciseRationaleProvider>
    <ExerciseLogProvider logging={exerciseLogging}>
    <ExerciseCustomisationProvider value={exerciseCustomisation}>
      <div className="today-blocks today-blocks-sessions">
        {weekIntentNote}
        <DaySessionContext day={displayDay} headline={headline} />
        {current.sessions.map((session, index) => (
          <StructuredSessionCard
            key={sessionIdentity({
              planId,
              weekPos: current.weekPos ?? 0,
              dayPos: current.dayPos ?? 0,
              sessionPos: index,
              week: current.week,
              day: current.day,
              session,
            })}
            session={session}
            day={index === 0 ? displayDay : undefined}
            layout="section"
            sectionIndex={index}
            defaultOpenBlocks
            initiallyOpenFirstBlock={false}
            hideTitle={Boolean(headline && sameTitle(headline, athleteFacingSessionTitle(session.title || displayDay.today_card?.headline || "")))}
            showDayContext={false}
            openWeekIntent={openWeekIntent}
            occurrenceDate={displayDay.date}
          />
        ))}
      </div>
    </ExerciseCustomisationProvider>
    </ExerciseLogProvider>
    </ExerciseRationaleProvider>
    </ExerciseMediaProvider>
    </PrivateExerciseMediaProvider>
    </RehabLabelProvider>
  );
}

/**
 * The session card: today's (or the next) session with its structured blocks,
 * the decision banner framing it, and the start/complete lifecycle actions.
 */
export function TodaySessionPanel({
  state,
  structuredPlan,
  planSchedule,
  rehabLabelPolicy,
  exerciseMedia,
  painReasonAllowed = false,
  token,
  onRefresh,
  athleteFullName,
  professionalStatus,
  commandUnavailable = false,
  presentationPending = false,
  onPlanSessionChanged,
  athleteSports,
}: {
  state: TodayCommandView;
  /** The athlete's own sports (profile technical_style): a focus from another sport stays hidden. */
  athleteSports?: readonly string[] | null;
  /** Applies an athlete's exercise change to the shown plan (see useTodayCommand). */
  onPlanSessionChanged?: (date: string, session: StructuredSession | null) => void;
  /** From the athlete's profile: the guided visualisation says their first name. */
  athleteFullName?: string | null;
  /** "amateur" / "professional": picks the guided visualisation's crowd. */
  professionalStatus?: string | null;
  /** Preserve drafts after a failed refresh; require fresh guidance for actions. */
  commandUnavailable?: boolean;
  /** The plan read behind the blocks is in flight: hold their space, don't show a stand-in. */
  presentationPending?: boolean;
  structuredPlan: StructuredPlan | null;
  /** Server-derived per-region Rehab/Prehab policy for the active plan. */
  rehabLabelPolicy?: RehabLabelPolicy | null;
  /** Curated demo videos for the active plan's blocks. */
  exerciseMedia?: Record<string, ExerciseMedia> | null;
  /** Server schedule projection + plan creation date, used to anchor the
   * current week of a weekday-only (open / renewable) plan. */
  planSchedule?: TodayPlanSchedule | null;
  /** Whether the athlete has health-data consent: gates the "Pain" log reason. */
  painReasonAllowed?: boolean;
  token: string;
  onRefresh: () => Promise<void>;
}) {
  const { showToast } = useToast();
  const fadeThrough = useFadeThrough();
  // Sparring focus is health-adjacent: read only with consent (the server checks too).
  const trainingFocus = useTrainingFocus(token, painReasonAllowed);
  const [intent, setIntent] = useState<CompletionIntent>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [rehabCheckOff, setRehabCheckOff] = useState<{ value: "done" | "modified" | "skipped"; label: string; saved?: boolean } | null>(null);
  const [rehabPerformance, setRehabPerformance] = useState<"done_as_shown" | "changed" | "stopped" | "skipped" | undefined>();
  // The session the athlete just logged as trained, captured at write time.
  // The refresh that follows can advance `next_session` to tomorrow's card, so
  // reading the id live would attach the review to the wrong session.
  const [reviewableSession, setReviewableSession] = useState<
    { planId: string; sessionId: string } | null
  >(null);
  // The running timer, full screen or minimised to a bar while it keeps time.
  const [activeTimer, setActiveTimer] = useState<
    { source: TimerSource; mode: "open" | "minimized" } | null
  >(null);
  // What the timer recorded, pre-filled (editable) into the completion notes.
  const [timerNotes, setTimerNotes] = useState("");
  // Finished sparring rounds waiting for the athlete's quick log entry.
  const [sparringDraft, setSparringDraft] = useState<SparringDraft | null>(null);
  // The voice-guided Fight Visualisation player, full screen while open.
  const [guidedOpen, setGuidedOpen] = useState(false);
  // Where a visualisation run started by Start session goes next: the session
  // timer, or straight to the log when the day has nothing to time.
  const [guidedNext, setGuidedNext] = useState<"timer" | "review" | null>(null);
  // The one card the session is logged on (see SessionReview).
  const [reviewOpen, setReviewOpen] = useState(false);
  // Whether the open review was reached from the timer (its ticks came from it).
  const [reviewFromTimer, setReviewFromTimer] = useState(false);
  const [timerHandoffPending, setTimerHandoffPending] = useState(false);
  // A previewed (not yet today's) session opens on its own screen.
  const [previewOpen, setPreviewOpen] = useState(false);
  const session = state.today.next_session;
  const status = state.today.completion_status;
  const duration = getSessionDuration(session);
  const resolvedDecision = resolveTodayDecision(state);
  const hasSession = resolvedDecision.hasSession;
  // Resolve today's day/session from the structured plan through the shared
  // 03:00 rollover, exactly as Plan Detail does. These blocks — not the backend
  // session summary — are the "what exact blocks apply today" answer. The
  // training day comes from the client-mounted hook (SSR-safe, null until mount).
  // The hook checks rollover every minute and on focus/visibility, invalidating
  // the calendar projection when a long-lived tab crosses into a new day.
  const trainingDay = useTrainingDay();
  const activePlanId = state.active_plan.id ?? "";
  // Exercises are logged against a session started (or already completed)
  // today, which is the same condition the server enforces. A card that has
  // moved on to the next session shows another day's blocks, so it stays closed.
  const reportExerciseLogError = useCallback(
    (message: string) => showToast(message, { tone: "error" }),
    [showToast],
  );
  // Swap / add / remove stays available until the shown workout starts. Once
  // it is started or logged it keeps its prescription (the server enforces
  // the same rule); a stale or failed Today never offers it.
  const sessionUnderway = resolvedDecision.sessionIsToday && status !== "not_started";
  const customisationDay = state.today.training_day;
  const exerciseCustomisation: ExerciseCustomisation | null =
    token && activePlanId && onPlanSessionChanged && !commandUnavailable && !sessionUnderway
      ? { token, planId: activePlanId, today: customisationDay, canRepeat: !state.active_plan.fight_date, onSessionChanged: onPlanSessionChanged }
      : null;
  const exerciseLogging = useTodayExerciseLogs({
    token,
    planId: activePlanId,
    trainingDay: state.today.training_day,
    enabled:
      resolvedDecision.sessionIsToday &&
      (status === "started" || status === "done" || status === "modified"),
    painReasonAllowed,
    allowProgression: state.today.decision_tier === "green"
      && state.open_injuries.length === 0
      && /^(GPP|SPP)$/i.test(state.active_plan.phase ?? "")
      && !state.live_prescription?.safety_hold,
    onError: reportExerciseLogError,
  });
  // Center the structured blocks on whatever the backend command view targets:
  // today's day in the normal case, or the NEXT scheduled session's day once
  // today is logged / carries no app card (session_relation === "next"). Resolving
  // by that target day — not the bare calendar day — stops the card from sticking
  // on the finished session while the header has already advanced to "Next
  // session", which is exactly how Overview already behaves.
  const focusDate = useMemo(() => resolveSessionFocusDate(trainingDay, session), [trainingDay, session]);
  // Open (renewable) plans carry weekday-only days with no calendar dates, so
  // the resolver needs to know which week of the block "today" falls in. Dated
  // camps ignore the hint — they still resolve purely by calendar date.
  const openWeekNumber = useMemo(() => resolveOpenPlanWeekNumber(structuredPlan, focusDate, {
    currentWeekNumber: planSchedule?.scheduleContext?.current_week_number,
    anchorDate: planSchedule?.scheduleContext?.anchor_date,
    createdAt: planSchedule?.createdAt,
  }), [structuredPlan, focusDate, planSchedule]);
  const openOngoing = Boolean(
    state.active_plan && isOpenOngoingPlan(state.active_plan.fight_date),
  );
  // Calendar projection can scan the whole plan. Local review/timer controls
  // do not change it; a new plan, focus date or week still resolves afresh.
  const storedCurrent = useMemo(() => resolveCurrentDay(structuredPlan, focusDate, {
    openWeekNumber,
    allowDatedWeekdayMatch: openOngoing,
  }), [structuredPlan, focusDate, openWeekNumber, openOngoing]);
  const livePrescription = state.live_prescription;
  // Calm-stage recovery support is guidance to follow, not rehab to log.
  const sessionHasRehab = Boolean(livePrescription?.session.blocks?.some(block =>
    block.block_type === "rehab" && !isGuidanceOnlyRehabBlock(block)));
  const rehabChoiceRequired = (intent === "done" || reviewOpen) && sessionHasRehab;
  // Only the server's standalone rehab occurrence is a rehab day. A planned
  // mobility session (Joint Prep) is also typed "rehab" but is not rehab.
  const rehabOnlyPrescription = livePrescription?.rehab_only === true;
  const sessionHasGuidanceRehab = Boolean(livePrescription?.session.blocks?.some(isGuidanceOnlyRehabBlock));
  // Followed guidance is all there is to report: no amount to choose.
  const guidanceOnlyRehab = sessionHasGuidanceRehab && !sessionHasRehab;
  // A rehab-only day of guidance has nothing to start: it is checked off.
  const guidanceOnlyRehabDay = rehabOnlyPrescription && guidanceOnlyRehab;
  const liveSessions = useMemo(() => livePrescription?.session.prescription_source === "athlete" && storedCurrent.sessions.length
    ? storedCurrent.sessions.map(s => ({
        ...s, blocks: (livePrescription.session.blocks ?? []).filter(b => b.source_session_id === s.session_id),
      }))
    : livePrescription ? [livePrescription.session] : [], [livePrescription, storedCurrent.sessions]);
  const current: CurrentDayResolution = useMemo(() => livePrescription ? {
    ...storedCurrent, inRange: true,
    day: {
      ...(storedCurrent.day ?? {}), date: state.today.training_day, sessions: liveSessions,
      ...(rehabOnlyPrescription ? {
        day_type: "recovery",
        today_card: {
          headline: livePrescription.session.title,
          nutrition_summary: storedCurrent.day?.today_card?.nutrition_summary,
          weight_cut_warning: storedCurrent.day?.today_card?.weight_cut_warning,
        },
      } : {}),
    },
    sessions: liveSessions,
  } : storedCurrent, [livePrescription, storedCurrent, liveSessions, rehabOnlyPrescription, state.today.training_day]);
  // Where the resolved day sits in the renewable development block (baseline /
  // progress / peak / deload). The resolved week position wins over the bare
  // anchor-derived number so the note always matches the blocks shown below.
  // Dated camps stay null and render unchanged.
  const openWeekIntent = openOngoing && !rehabOnlyPrescription
    ? openBlockWeekIntent(current.weekPos != null ? current.weekPos + 1 : openWeekNumber, current.sessions.some(s => s.prescription_source === "athlete"))
    : null;
  const showStructuredBlocks = current.inRange && Boolean(current.day);
  // The day's blocks by id, to put each timer log back against its block.
  const dayBlocksById = useMemo(() => new Map(
    current.sessions.flatMap((item) => item.blocks ?? []).flatMap((block) => {
      const id = cleanText(block.block_id);
      return id ? [[id, block] as const] : [];
    }),
  ), [current.sessions]);
  const hasResolvedDaySessions = current.inRange && current.sessions.length > 0;
  const isSessionPreview = resolvedDecision.displayTier === "preview" || (hasSession && !resolvedDecision.sessionIsToday);
  // Home's "Preview session" lands here with ?preview=1: open the preview once
  // the previewed day's parts are known, then drop the param so a refresh or
  // the back button does not reopen it.
  const canOpenPreview = isSessionPreview && showStructuredBlocks;
  useEffect(() => {
    if (!canOpenPreview) return;
    const params = new URLSearchParams(window.location.search);
    if (params.get("preview") !== "1") return;
    params.delete("preview");
    const query = params.toString();
    window.history.replaceState(window.history.state, "", `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- one-shot read of the URL on arrival
    setPreviewOpen(true);
  }, [canOpenPreview]);
  const relationCopy = getSessionRelationCopy(
    session,
    status,
    resolvedDecision.sessionIsToday,
  );
  // session_hold already folds in the prescription hold; older payloads lack it.
  const decisionBlocksCurrentSession = resolvedDecision.blocksCurrentSession
    || (!state.session_hold && Boolean(livePrescription?.safety_hold));
  const severeInjuryBlocksCurrentSession =
    resolvedDecision.severeInjuryBlocksCurrentSession;
  // STOP + the scheduled session is today: show the recovery/mobility safe
  // session in place of the real blocks so Today never displays hard combat as
  // available under a stop. Future sessions stay visible but read as pending.
  const safeSession =
    resolvedDecision.useSafeReplacement && !livePrescription && !current.sessions.some(s => s.prescription_source === "athlete")
      ? getSafeSessionView(getSessionTitle(session), state.open_injuries)
      : null;
  const nextIsHardCombat = isHardCombatSession(session);
  // Gate completion on the scope-aware "is this today" check, not just the
  // session_relation stamp: a session that reaches the card without an explicit
  // session_relation but whose scope is not "today" must still read as pending,
  // never completable.
  //
  // Whether today HAS a session is the server's answer, not one re-derived here.
  // An empty sessions array is not the same question: a headline-only support
  // day ("Rhythm flush") carries no session objects and is still prescribed
  // work, so reading rest-ness off the array disabled real sessions. The server
  // resolves the plan card — and rejects completion writes on a rest day — so
  // scope "today" is the single answer both sides use.
  // A training day is one session to the athlete: one start, one RPE, one log,
  // written by the backend to every session the card schedules that day. So the
  // timer runs every timeable block of the day. No timeable blocks means no
  // session timer: a zero-load session such as Tactical Focus is never turned
  // into rounds.
  const timerSourceText = structuredPlan?.raw_markdown_fallback ?? null;
  const timerCountdown = current.day?.countdown_label ?? null;
  // A rocked/dropped report today ends contact for the day: the server's
  // notice is authoritative for every contact action, whatever the tier or
  // clearance says. Non-contact work is untouched.
  const headImpactToday = state.today.primary_safety_notice?.code === "head_impact_no_contact";
  const plannedTimerItems: TimerItem[] = useMemo(() => hasResolvedDaySessions
    ? dayTimerItems(current.sessions, session.session_id, {
        sourceText: timerSourceText,
        countdown: timerCountdown,
      })
    : [], [hasResolvedDaySessions, current.sessions, session.session_id, timerSourceText, timerCountdown]);
  const nonContactItems = useMemo(() => headImpactToday ? withoutSparringItems(plannedTimerItems) : plannedTimerItems,
    [headImpactToday, plannedTimerItems]);
  const contactRemovedFromTimer = nonContactItems.length !== plannedTimerItems.length;
  const timerItems = nonContactItems;
  // The day's Fight Visualisation, offered as a voice-guided run. Only when the
  // session being logged is one of this day's, the same rule as the timer.
  const guidedVisualisation = useMemo(() =>
    hasResolvedDaySessions && timerSessionFor(current.sessions, session.session_id)
      ? current.sessions.map(fightVisualisationFromSession).find((item) => item !== null) ?? null
      : null, [hasResolvedDaySessions, current.sessions, session.session_id]);
  const timerKeys: Record<TimerSource, string> = {
    // A run saved with its sparring rounds is not resumed once they are removed.
    session: `${SESSION_RUN_KEY_PREFIX}${activePlanId}:${session.session_id ?? ""}:${state.today.training_day}${contactRemovedFromTimer ? ":no-contact" : ""}`,
    contact: `${CONTACT_RUN_KEY_PREFIX}${activePlanId}:${state.today.training_day}`,
  };
  const timerStorageKey = timerKeys.session;
  // Coach-led contact is timed from TODAY's plan day, even when the card above
  // has moved on to the next app session (a sparring-only day has none).
  const todayOpenWeekNumber = useMemo(() => resolveOpenPlanWeekNumber(structuredPlan, trainingDay, {
    currentWeekNumber: planSchedule?.scheduleContext?.current_week_number,
    anchorDate: planSchedule?.scheduleContext?.anchor_date,
    createdAt: planSchedule?.createdAt,
  }), [structuredPlan, trainingDay, planSchedule]);
  const todayResolved = useMemo(() =>
    focusDate === trainingDay
      ? current
      : resolveCurrentDay(structuredPlan, trainingDay, {
          openWeekNumber: todayOpenWeekNumber,
          allowDatedWeekdayMatch: openOngoing,
        }), [focusDate, trainingDay, current, structuredPlan, todayOpenWeekNumber, openOngoing]);
  // completion_status always describes today: once today is logged (done,
  // modified or skipped) its contact is over, even while the card has moved on
  // to the next session, so its rounds are no longer offered or re-loggable.
  const todayLogged = status === "done" || status === "modified" || status === "skipped";
  const customOffDayCopy = structuredPlan?.plan_metadata?.prescription_source === "athlete"
    ? todayResolved.day?.day_type === "recovery" ? "Recovery day — No workout completion required."
      : todayResolved.day?.day_type === "rest" ? "Rest day" : null
    : null;
  const contactTarget: ContactTimerTarget | null = todayLogged ? null :
    (todayResolved.inRange ? contactTimerTarget(todayResolved.day) : null) ??
    (resolvedDecision.sessionIsToday && session.coach_led_contact
      ? contactTimerTarget({ today_card: { coach_led_contact: session.coach_led_contact } } as StructuredDay)
      : null);
  // Hard contact follows the same readiness authority as a session: only a
  // go / follow-the-limits day, never under pull back, stop or a severe injury.
  const clearanceAllowsContact = !headImpactToday && (!state.effective_clinician_clearance ||
    (state.effective_clinician_clearance.level === "train_contact" && !state.effective_clinician_clearance.requires_update));
  const contactCleared =
    clearanceAllowsContact &&
    (resolvedDecision.authoritativeTier === "green" ||
      resolvedDecision.authoritativeTier === "modify") &&
    !severeInjuryBlocksCurrentSession &&
    !decisionBlocksCurrentSession &&
    !livePrescription?.rehab_only &&
    !safeSession;
  const contactTimerAvailable = Boolean(contactTarget) && contactCleared;
  // The athlete's declared gym work (hard sparring, technical rounds) is the
  // day's main event: it owns the headline and the primary button, and any app
  // work that day reads as what comes with it. Contact never leads a day the
  // decision blocks (pull back, stop): it is not happening, so it cannot be
  // the day's headline.
  const contactLeads =
    clearanceAllowsContact &&
    Boolean(contactTarget) &&
    resolvedDecision.sessionIsToday &&
    !safeSession &&
    !livePrescription?.rehab_only &&
    !decisionBlocksCurrentSession;
  const contactHeadline = contactTarget ? contactTitle(contactTarget) : "";
  // The same authority holds for the day the card previews: a declared contact
  // day shown as "Next session" is headlined by its contact, not by whichever
  // app session sits first that day. Timer and start stay with today's contact.
  const previewContactTarget =
    resolvedDecision.sessionIsToday || safeSession
      ? null
      : (current.inRange ? contactTimerTarget(current.day) : null) ??
        (session.coach_led_contact
          ? contactTimerTarget({ today_card: { coach_led_contact: session.coach_led_contact } } as StructuredDay)
          : null);
  // Is the session being logged the contact itself? A sparring-only day has no
  // session objects, so the server logs it against the day's headline entry.
  // Identity must survive a restrictive clearance: hiding the contact CTA
  // must not turn the same contact work into a generic completable session.
  const contactIsSession =
    Boolean(contactTarget) && resolvedDecision.sessionIsToday && !livePrescription?.rehab_only &&
    ((current.inRange && Boolean(current.day) && current.sessions.length === 0) ||
      sameTitle(getSessionTitle(session), contactTarget?.headline ?? "") ||
      sameTitle(getSessionTitle(session), contactHeadline));
  const contactClearanceBlocked = contactIsSession && !clearanceAllowsContact;
  const canCompleteSession = resolvedDecision.canCompleteSession
    && !livePrescription?.safety_hold && !contactClearanceBlocked && !guidanceOnlyRehabDay;
  const guidanceCanCheckOff = guidanceOnlyRehabDay && resolvedDecision.canCompleteSession
    && !livePrescription?.safety_hold;
  const timerAvailable =
    canCompleteSession && !safeSession && Boolean(session.session_id) && timerItems.length > 0;
  // A day with nothing to time but a Fight Visualisation: starting the session
  // IS the guided run, and finishing it opens the log.
  // An optional camp session never leads: it rides on the day's real work.
  const guidedLeads =
    Boolean(guidedVisualisation && !guidedVisualisation.optional) &&
    canCompleteSession &&
    !safeSession &&
    !timerAvailable;
  // Something to go back into while the session is started.
  const canResume = timerAvailable || guidedLeads || (contactIsSession && contactTimerAvailable);
  const contactLockCopy =
    headImpactToday
      ? CONTACT_LOCK_COPY.head_impact
      : !clearanceAllowsContact
      ? CONTACT_LOCK_COPY.clinician_clearance
      : resolvedDecision.authoritativeTier === "not_checked_in"
        ? CONTACT_LOCK_COPY.not_checked_in
        : CONTACT_LOCK_COPY.blocked;
  const freeTimerAvailable =
    resolvedDecision.authoritativeTier !== "stop" && !severeInjuryBlocksCurrentSession;
  // A run left in progress (app closed mid-session) comes back as the mini bar.
  // Read from storage on the client only, so server and first client render agree.
  const savedTimerSource = useSyncExternalStore<TimerSource | null>(
    subscribeToNothing,
    () =>
      timerAvailable && status === "started" && hasSavedRun(timerKeys.session)
        ? "session"
        : contactTimerAvailable && hasSavedRun(timerKeys.contact)
          ? "contact"
          : null,
    () => null,
  );
  const shownTimer =
    activeTimer ?? (savedTimerSource ? { source: savedTimerSource, mode: "minimized" as const } : null);
  // The app-wide round timer; while it is up, its mini bar is the way back in.
  const roundTimer = useRoundTimer();
  const anyTimerShown = Boolean(shownTimer) || roundTimer.shown;
  // Drop today's saved runs this Today can no longer resume (the plan or
  // session changed, or the session was logged elsewhere), so they cannot
  // keep blocking the round timer. Only server facts decide: a plan that
  // failed to load leaves the contact run alone rather than guessing.
  const resumableSession = status === "started" ? timerKeys.session : null;
  const resumableContact =
    contactTimerAvailable || (!contactTarget && !structuredPlan) ? timerKeys.contact : null;
  useEffect(() => {
    pruneSavedTodayRuns(
      state.today.training_day,
      [resumableSession, resumableContact].filter((key): key is string => Boolean(key)),
    );
  }, [state.today.training_day, resumableSession, resumableContact]);
  // Tint the session card to match today's decision (green/amber/red) so the page
  // reads at a glance instead of being a wall of identical dark cards. Neutral
  // (not-checked-in) carries no tone — the card stays default until check-in.
  const cardTone =
    resolvedDecision.tone === "green" ||
    resolvedDecision.tone === "amber" ||
    resolvedDecision.tone === "red"
      ? resolvedDecision.tone
      : undefined;
  const terminalStatusCopy = severeInjuryBlocksCurrentSession
    ? "Blocked by an active severe injury."
    : decisionBlocksCurrentSession
      ? "Follow the recommendation above. Do not start this session from Today."
      : contactClearanceBlocked
        ? headImpactToday
          ? "No more contact today: you reported being rocked or dropped."
          : "This contact session is locked by your effective clinician clearance."
        : guidanceCanCheckOff
          ? status === "done" ? "Guidance followed today." : "Follow the guidance, then check it off."
        : resolvedDecision.authoritativeTier === "not_checked_in"
          ? "Submit today's check-in to unlock session actions."
          : "This entry has nothing to log. Follow it as written.";

  async function saveCompletion(
    nextStatus: TodayCompletionStatus,
    details: {
      sessionRpe?: number | null;
      painAfter?: number | null;
      modificationReason?: string;
      notes?: string;
    } = {},
  ): Promise<boolean> {
    if (timerHandoffPending) {
      showToast("Save the timer results before completing this session.", { tone: "error" });
      return false;
    }
    if (commandUnavailable) {
      showToast("Refresh Today before saving this session.", { tone: "error" });
      return false;
    }
    if (!state.active_plan.id || !session.session_id || isSubmitting) {
      return false;
    }
    if (nextStatus === "done" && rehabChoiceRequired && !rehabPerformance && !rehabOnlyPrescription) {
      showToast("Choose Completed, Modified or Skipped for rehab.", { tone: "error" });
      return false;
    }
    setIsSubmitting(true);
    try {
      const completion = await submitTodaySessionCompletion(token, {
        plan_id: state.active_plan.id,
        session_id: session.session_id,
        status: nextStatus,
        session_rpe: details.sessionRpe ?? null,
        pain_after: details.painAfter ?? null,
        modification_reason: details.modificationReason ?? "",
        notes: details.notes ?? "",
        prescription_revision: livePrescription?.revision,
        rehab_tracking: "injury_checkin",
        rehab_performance: nextStatus === "done" ? rehabPerformance ?? (rehabOnlyPrescription || guidanceOnlyRehab ? "done_as_shown" : undefined) : nextStatus === "modified" && livePrescription ? (livePrescription.safety_hold ? "stopped" : rehabPerformance ?? "changed") : undefined,
      });
      setIntent(null);
      if (nextStatus !== "started") {
        clearSavedRun(timerStorageKey);
        setActiveTimer((timer) => (timer?.source === "session" ? null : timer));
        setTimerNotes("");
        // The full-screen review fades through black back to the logged card.
        fadeThrough(() => setReviewOpen(false));
      }
      setRehabPerformance(undefined);
      showToast(getCompletionLabel(nextStatus), { tone: "success", mergeKeys: XP_TOAST_KEYS.session });
      // Order matters: the confirmation toast is already up and the refresh
      // below is what surfaces the XP award, so the review prompt is queued
      // here and only renders once both have landed.
      setReviewableSession(
        shouldPromptSessionFeedback(nextStatus) && !rehabOnlyPrescription
          ? { planId: state.active_plan.id, sessionId: session.session_id }
          : null,
      );
      await onRefresh();
      if (nextStatus === "done" || nextStatus === "modified") offerPushNotifications(token);
      return true;
    } catch (error) {
      showToast(error instanceof Error ? error.message : "Session update failed.", { tone: "error" });
      // A rejected stale revision needs fresh content before another start.
      await onRefresh();
      return false;
    } finally {
      setIsSubmitting(false);
    }
  }

  /** Whether a timer may open, unlocking its audio if so. Call it inside the
   * tap itself, never from a fade or other delayed callback: iOS Safari only
   * unlocks audio during the user's gesture. */
  function readyTimer(): boolean {
    if (commandUnavailable) {
      showToast("Refresh Today before opening a training timer.", { tone: "error" });
      return false;
    }
    // The round timer is up: it keeps the screen until it closes.
    if (roundTimer.shown) return false;
    timerAudio().unlock();
    return true;
  }

  function openTimer(source: TimerSource) {
    if (!readyTimer()) return;
    fadeThrough(() => setActiveTimer({ source, mode: "open" }));
  }

  /** Lead button on a contact day: time the rounds, and when the contact IS
   * today's session, mark it started so it can be logged when the rounds end. */
  function startContact() {
    openTimer("contact");
    if (contactIsSession && canCompleteSession && status === "not_started") {
      void saveCompletion("started");
    }
  }

  /** Start session: the day's visualisation comes first when there is one,
   * then the timer, so the run follows the card from the top. */
  function startSession() {
    // Audio only unlocks inside the tap itself, before any await.
    if (timerAvailable) timerAudio().unlock();
    const visualisationFirst = Boolean(guidedVisualisation);
    void saveCompletion("started").then((started) => {
      if (!started || roundTimer.shown) return;
      if (visualisationFirst) {
        fadeThrough(() => {
          setGuidedNext(timerAvailable ? "timer" : "review");
          setGuidedOpen(true);
        });
        return;
      }
      if (timerAvailable) {
        fadeThrough(() => setActiveTimer({ source: "session", mode: "open" }));
      }
    });
  }

  /** The visualisation run reached its end: that is the exercise done. */
  function logVisualisationDone() {
    const blockId = guidedVisualisation?.blockId;
    if (!blockId || !exerciseLogging || exerciseLogging.logs[blockId]) return;
    void exerciseLogging.save({ block_id: blockId, status: "as_prescribed" }).catch((error: unknown) => {
      showToast(error instanceof Error ? error.message : "Could not log the visualisation.", { tone: "error" });
    });
  }

  /** Leave the visualisation for what comes next in the session. */
  function continueAfterGuided() {
    const next = guidedNext;
    // Checked and unlocked now, in the tap: inside the fade would be too late
    // for iOS audio.
    const toTimer = next === "timer" && timerAvailable && readyTimer();
    // One fade: the visualisation leaves and what follows arrives in the dark.
    fadeThrough(() => {
      setGuidedOpen(false);
      setGuidedNext(null);
      if (toTimer) {
        setActiveTimer({ source: "session", mode: "open" });
      } else if (next === "review" && !reviewOpen) {
        openReview(false);
      }
    });
  }

  function openReview(fromTimer: boolean) {
    setIntent(null);
    setReviewFromTimer(fromTimer);
    setReviewOpen(true);
  }

  // Rehab-only check-off: the tapped answer is marked at once (red outline) and
  // saved straight away, or — while the session timer is running or handing
  // off its results — as soon as the timer is done, so a tap is never lost.
  // shownTimer also covers a session timer resumed from storage after a reload.
  const timerBusy = timerHandoffPending || shownTimer?.source === "session";
  const rehabCheckOffPending = rehabCheckOff !== null && !["done", "modified", "skipped"].includes(status);
  // The answer belongs to this session: Today moving on to another drops it.
  const [rehabCheckOffSession, setRehabCheckOffSession] = useState(session.session_id);
  if (rehabCheckOffSession !== session.session_id) {
    setRehabCheckOffSession(session.session_id);
    setRehabCheckOff(null);
  }
  const rehabCheckOffSaving = useRef(false);
  useEffect(() => {
    if (!rehabCheckOff || rehabCheckOff.saved || rehabCheckOffSaving.current || timerBusy || isSubmitting
      || ["done", "modified", "skipped"].includes(status)) return;
    const { value, label } = rehabCheckOff;
    rehabCheckOffSaving.current = true;
    void saveCompletion(value, { modificationReason: value === "done" ? "" : `Rehab ${label.toLowerCase()}` })
      .then((saved) => {
        rehabCheckOffSaving.current = false;
        // Saved: stays marked until the logged status replaces the prompt.
        // Failed: cleared so the athlete can tap again.
        setRehabCheckOff((current) => current?.value === value ? (saved ? { ...current, saved: true } : null) : current);
      });
    // saveCompletion reads the latest render; re-run whenever the gate changes,
    // including another submission finishing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rehabCheckOff, timerBusy, isSubmitting, status]);

  // Asked once a session with reviewed rehab is logged as done.
  const rehabChoice = (
    <div role="group" aria-label="Did you complete your rehab?" className="today-injury-guidance">
      <p>Did you complete your rehab?</p>
      <div className="today-segment-row">
        {([ ["done_as_shown", "Completed"], ["changed", "Modified"], ["skipped", "Skipped"] ] as const).map(([value, label]) => (
          <button key={value} type="button" className={rehabPerformance === value ? "today-segment today-segment-active" : "today-segment"} aria-pressed={rehabPerformance === value} onClick={() => setRehabPerformance(value)}>{label}</button>
        ))}
      </div>
    </div>
  );

  const sparringPrompt = sparringDraft ? (
    <SparringLogPrompt
      key={`${sparringDraft.source}:${sparringDraft.rounds}:${sparringDraft.title}`}
      token={token}
      draft={sparringDraft}
      onSaved={() => void onRefresh()}
      onDismiss={() => setSparringDraft(null)}
    />
  ) : null;

  // Timer shortcuts shown along the bottom of the action tray. Hidden while a
  // timer is running: its mini bar is the way back in.
  function timerTools(trailing?: ReactNode, options: { contactAsPrimary?: boolean } = {}) {
    const showContact = Boolean(contactTarget) && !options.contactAsPrimary;
    // Leading the tray already, the guided run needs no second button.
    const showGuided =
      Boolean(guidedVisualisation) && !(guidedLeads && (status === "not_started" || status === "started"));
    // A cleared sparring day already offers a round timer loaded with the plan;
    // a blank one beside it reads as a duplicate.
    const showFreeTimer = freeTimerAvailable && !contactTimerAvailable;
    const tools = !anyTimerShown && !guidedOpen && (showContact || showFreeTimer || showGuided);
    if (!tools && !trailing) {
      return null;
    }
    return (
      <>
      {tools ? (
      <div className="today-action-tools">
        {tools && showContact ? (
          contactTimerAvailable ? (
            <button type="button" className="action-utility today-tool-button" data-accent="true" onClick={() => openTimer("contact")}>
              <ToolIcon name="bell" />
              Sparring rounds
            </button>
          ) : (
            <span className="today-tool-lock">
              <ToolIcon name="lock" />
              {contactLockCopy}
            </span>
          )
        ) : null}
        {tools && showGuided ? (
          <button
            type="button"
            className="action-utility today-tool-button"
            aria-label="Guided visualisation"
            data-accent="true"
            disabled={isSubmitting}
            onClick={() => {
              // Doing today's visualisation is doing today's session: start it,
              // so finishing the run logs the exercise.
              if (canCompleteSession && !safeSession && status === "not_started") {
                startSession();
              } else {
                fadeThrough(() => setGuidedOpen(true));
              }
            }}
          >
            <ToolIcon name="voice" />
            Visualisation
          </button>
        ) : null}
        {tools && showFreeTimer ? (
          <button type="button" className="action-utility today-tool-button" onClick={roundTimer.open}>
            <ToolIcon name="timer" />
            Free timer
          </button>
        ) : null}
      </div>
      ) : null}
      {trailing ? <div className="today-action-footer">{trailing}</div> : null}
      </>
    );
  }

  // A sparring-only day has no session actions: its tray leads with the rounds.
  const contactOnlyTray =
    !anyTimerShown && (contactTarget || freeTimerAvailable) ? (
      <div className="today-session-actions today-action-tray">
        {contactTarget && contactTimerAvailable ? (
          <button type="button" className="cta" onClick={() => openTimer("contact")}>
            Start sparring rounds
          </button>
        ) : null}
        {timerTools(undefined, { contactAsPrimary: Boolean(contactTarget && contactTimerAvailable) })}
      </div>
    ) : null;

  function renderTimer(sessionTitle: string) {
    // One timer on screen at a time: while the round timer is up, a saved
    // session or contact run waits and comes back once it closes.
    if (!shownTimer || roundTimer.shown) {
      return null;
    }
    const { source, mode } = shownTimer;
    let items: TimerItem[];
    let title: string;
    if (source === "session") {
      if (!timerAvailable) return null;
      items = timerItems;
      title = sessionTitle;
    } else {
      if (!contactTimerAvailable || !contactTarget) return null;
      items = [contactRoundsItem(contactTarget, savedRoundFormat(CONTACT_FORMAT_MEMORY_KEY))];
      title = items[0].title;
    }
    const storageKey = timerKeys[source];
    return (
      <SessionTimer
        key={storageKey}
        items={items}
        storageKey={storageKey}
        sessionTitle={title}
        visible={mode === "open"}
        finishLabel={source === "session" ? "Finish session" : "Done"}
        handOffWhenDone={source === "session"}
        onMinimize={() => fadeThrough(() => setActiveTimer({ source, mode: "minimized" }))}
        onExpand={() => openTimer(source)}
        onClose={() => fadeThrough(() => {
          clearSavedRun(storageKey);
          setActiveTimer(null);
        })}
        onFinish={async (summary: SessionTimerSummary) => {
          if (source === "session") {
            setTimerHandoffPending(true);
            // Keep the finished run and timer until the atomic batch succeeds.
            // A failure stays on the timer with Retry save; no re-ticking.
            if (!exerciseLogging?.saveMany) {
              throw new Error("Exercise logging is unavailable. Refresh Today and retry saving this run.");
            }
            const entries = timerExerciseLogs(summary.run, exerciseLogging.logs).filter((entry) => {
              // Also check the current block: a resumed older run may carry
              // metadata from before the live rehab prescription changed.
              const block = dayBlocksById.get(entry.block_id);
              return !block || isLoggableBlock(block);
            }).map((entry) => {
              const block = dayBlocksById.get(entry.block_id);
              return block ? { ...entry, ...withLastLoad(block, entry, exerciseLogging.recentLoads, exerciseLogging.recentPerformances) } : entry;
            });
            if (entries.length > 0) {
              await exerciseLogging.saveMany(entries, { keepExisting: true });
            }
            setTimerHandoffPending(false);
          }
          clearSavedRun(storageKey);
          // The timer leaves, and the log or sparring sheet arrives, in one
          // fade: calls made while it is going dark swap together.
          fadeThrough(() => setActiveTimer(null));
          // Any sparring rounds actually done get the quick sparring log.
          if (summary.sparring && summary.sparring.rounds > 0) {
            const draft: SparringDraft = {
              source,
              planId: activePlanId || null,
              sessionId: source === "session" ? session.session_id ?? null : null,
              // The day's deterministic contact classification is authoritative
              // for both contact rounds and sparring inside a planned session;
              // the sparring block's own structured intensity is the fallback.
              plannedIntensity: contactTarget
                ? PLANNED_SPARRING_INTENSITY[contactTarget.kind]
                : summary.sparring.plannedIntensity,
              title: title,
              rounds: summary.sparring.rounds,
              roundSeconds: summary.sparring.roundSeconds,
            };
            fadeThrough(() => setSparringDraft(draft));
          }
          if (source === "session") {
            fadeThrough(() => openReview(true));
            return;
          }
          if (
            source === "contact" &&
            contactIsSession &&
            canCompleteSession &&
            (status === "not_started" || status === "started") &&
            (summary.sparring?.rounds ?? 0) > 0
          ) {
            // The gym sets the round count, so fewer rounds than the timer's
            // default is not a modified session: the rounds done are the log.
            setTimerNotes(summary.notes.slice(0, 2000));
            setIntent("done");
            return;
          }
          const recorded = summary.notes.replace(/^Timer:\s*/, "");
          showToast(recorded || "Timer closed.", { tone: recorded ? "success" : "info" });
        }}
      />
    );
  }

  if (!hasSession) {
    return (
      <section
        id="today-session"
        className="today-card today-session-card"
        data-tone={cardTone}
        aria-labelledby="today-session-heading"
      >
        {customOffDayCopy && !guidanceOnlyRehabDay ? <p className="muted">{customOffDayCopy}</p> : null}
        <div className="today-card-head">
          <div>
            <p className="kicker">Today&apos;s session</p>
            {/* training_day is always present in the initial payload, so headline
                it unconditionally — gating on the async-loaded structuredPlan
                caused a flash from "No session scheduled" to the date on load. */}
            <h2 id="today-session-heading">
              {contactTarget ? contactHeadline : formatTrainingDay(state.today.training_day)}
            </h2>
          </div>
        </div>
        {showStructuredBlocks ? (
          <TodaySessionBlocks
            planId={state.active_plan?.id}
            current={current}
            headline={contactTarget ? contactHeadline : formatTrainingDay(state.today.training_day)}
            openWeekIntent={openWeekIntent}
            rehabLabelPolicy={rehabLabelPolicy}
            exerciseMedia={exerciseMedia}
            exerciseLogging={exerciseLogging}
            accessToken={token}
            exerciseCustomisation={exerciseCustomisation}
          />
        ) : (
          <p className="muted">No plan session today. Use View plan to find the next one.</p>
        )}
        {contactOnlyTray}
        {sparringPrompt}
        {renderTimer(formatTrainingDay(state.today.training_day))}

      </section>
    );
  }

  const sessionTitle = hasResolvedDaySessions
    ? getStructuredTodaySessionTitle(current, session.session_id) || getSessionTitle(session)
    : getSessionTitle(session);
  // Avoid the "Today's session / Today's session" stutter: when the session has
  // no real name and falls back to the generic title that already matches the
  // kicker, headline the training day instead so the eyebrow and heading differ.
  const sessionHeadline =
    sessionTitle.trim().toLowerCase() === "today's session" ||
    sessionTitle.trim().toLowerCase() === relationCopy.kicker.trim().toLowerCase()
      ? formatSessionDate(session)
      : sessionTitle;
  const previewContactHeadline = previewContactTarget ? contactTitle(previewContactTarget) : "";
  const previewContactIsSession =
    Boolean(previewContactTarget) &&
    ((current.inRange && Boolean(current.day) && current.sessions.length === 0) ||
      sameTitle(sessionTitle, previewContactTarget?.headline ?? "") ||
      sameTitle(sessionTitle, previewContactHeadline));
  const headline = contactLeads
    ? contactHeadline
    : previewContactTarget
      ? previewContactHeadline
      : sessionHeadline;
  // The contact rounds lead the tray while no timer is already running.
  const contactCta = contactLeads && contactTimerAvailable && !anyTimerShown;
  // The app work that comes with the day's contact, named under the headline.
  const alongsideTitle =
    (contactLeads && !contactIsSession) || (previewContactTarget && !previewContactIsSession)
      ? sessionTitle.trim()
      : "";
  const alongsideLabel = contactLeads ? "Also today" : "Also that day";
  // Eyebrow: which day this is. The camp countdown lives in the page header.
  const sessionDateLabel = session.calendar_date
    ? formatTrainingDay(session.calendar_date).replace(
        new RegExp(`\\s${session.calendar_date.slice(0, 4)}$`),
        "",
      )
    : textValue(session.weekday_with_label || session.weekday);
  const unlockCopy = previewUnlockCopy(
    session.calendar_date,
    state.today.training_day,
    (state.open_injuries?.length ?? 0) > 0,
  );
  // A day of several parts is named by them, primary first ("Strength +
  // Skill"), with their own names underneath in the order they are trained.
  // Optional extras never name the day, and contact days keep their contact
  // headline.
  const namedParts = showStructuredBlocks && !safeSession
    ? current.sessions.filter((item) => !item.optional)
    : [];
  const multiPart = namedParts.length > 1 && headline === sessionHeadline;
  const primaryPart = multiPart
    ? namedParts.find((item) => item.session_id === session.session_id) ?? namedParts[0]
    : null;
  const partsTitle = primaryPart
    ? Array.from(
        new Set(
          [primaryPart, ...namedParts.filter((item) => item !== primaryPart)]
            .map((item) => shortSessionType(item, rehabLabelPolicy))
            .filter(Boolean),
        ),
      ).join(" + ")
    : "";
  // A preview's parts live on their own screen, so their names show under
  // the headline as the sign of what else the day holds.
  const calmBlocks = livePrescription?.session.blocks ?? session.blocks ?? [];
  const calmOnly = calmBlocks.length > 0 && calmBlocks.every(isCalmRehabBlock);
  const shownTitle = calmOnly ? (partsTitle || headline).replace(/rehab/gi, "guidance") : partsTitle || headline;
  const partsSubtitle = multiPart || isSessionPreview
    ? namedParts
        .map((item) => athleteFacingSessionTitle(textValue(item.title)))
        .filter((title) => title && !sameTitle(title, shownTitle))
        .join(" · ")
    : "";
  const summaryChips = safeSession
    ? []
    : sessionSummaryChips(showStructuredBlocks ? current.sessions : [], duration, session.effective_load);
  const injuryText = injuryStripText(state.open_injuries ?? []);
  // A different session taking the card (today's logged, next one up) fades
  // in as a handoff rather than swapping in one frame.
  const cardKey = `${session.session_id ?? ""}:${session.calendar_date ?? ""}:${isSessionPreview ? "preview" : "today"}`;
  // "Last training focus": before relevant work only, never during or after it.
  // Matched to the session this card shows (not any session that day), or to
  // the declared contact when the card leads with it.
  const focusSession = current.sessions.find((item) => item.session_id === session.session_id)
    ?? { session_type: session.session_type, blocks: calmBlocks };
  const focusText = !safeSession && (isSessionPreview || status === "not_started")
    ? relevantFocus(trainingFocus, {
        session: session.session_id ? focusSession : null,
        contactLeads: contactLeads || Boolean(previewContactTarget),
        sport: upcomingSport(structuredPlan?.plan_metadata?.sport, athleteSports),
      })
    : null;

  return (
    <section
      id="today-session"
      className="today-card today-session-card"
      data-tone={cardTone}
      aria-labelledby="today-session-heading"
    >
      <AutoHeight swapKey={cardKey}>
        {customOffDayCopy && !guidanceOnlyRehabDay ? <p className="muted">{customOffDayCopy}</p> : null}
        <div className="today-card-head today-session-head">
          <div>
            <div className="today-session-eyebrow">
              <p className="kicker">{relationCopy.kicker}</p>
              {sessionDateLabel ? <p className="today-session-date">{sessionDateLabel}</p> : null}
            </div>
            <h2 id="today-session-heading">{shownTitle}</h2>
            {partsSubtitle ? <p className="today-session-parts today-fade-in">{partsSubtitle}</p> : null}
            {alongsideTitle && !showStructuredBlocks ? (
              <p className="today-session-alongside">
                <span className="today-detail-label">{alongsideLabel}</span> {alongsideTitle}
              </p>
            ) : null}
            {summaryChips.length > 0 ? (
              <SummaryChipList chips={summaryChips} />
            ) : null}
            {focusText ? (
              <p className="today-session-focus today-fade-in">
                <span className="today-detail-label">Last training focus</span> {focusText}
              </p>
            ) : null}
          </div>
        </div>
        {injuryText && !safeSession && !isSessionPreview ? (
          <a href="#today-injury" className="today-injury-strip">
            <ToolIcon name="shield" />
            <span className="today-injury-strip-text">
              <span className="today-injury-strip-title">Injury-aware session</span>
              <span className="today-injury-strip-detail">Training around {injuryText}</span>
            </span>
            <span className="today-injury-strip-chevron" aria-hidden="true" />
          </a>
        ) : null}
        {canCompleteSession && !rehabOnlyPrescription && !safeSession && status === "not_started" ? (
          <div className="today-session-actions today-action-tray">
            {contactCta ? (
              <button type="button" className="cta" onClick={startContact} disabled={isSubmitting}>
                Start {contactHeadline.toLowerCase()}
              </button>
            ) : null}
            {contactLeads && contactTimerAvailable && contactIsSession ? null : (
              <button
                type="button"
                className={contactCta ? "secondary-button" : "cta"}
                onClick={startSession}
                disabled={isSubmitting}
              >
                {contactCta ? null : <ToolIcon name="play" />}
                {guidedLeads ? "Start guided visualisation" : alongsideTitle ? `Start ${alongsideTitle}` : "Start session"}
              </button>
            )}
            {timerTools(
              <button
                type="button"
                className="today-tool-link"
                onClick={() => setIntent("skipped")}
                disabled={isSubmitting}
              >
                Skip session
              </button>,
              { contactAsPrimary: contactLeads && contactTimerAvailable },
            )}
          </div>
        ) : null}
        {/* While the day's parts load, the tray already holds its place. */}
        {isSessionPreview && !safeSession && (showStructuredBlocks || presentationPending) ? (
          <div className="today-session-actions today-action-tray">
            <button
              type="button"
              className="cta"
              aria-haspopup="dialog"
              disabled={!showStructuredBlocks}
              onClick={() => fadeThrough(() => setPreviewOpen(true))}
            >
              <ToolIcon name="play" />
              Preview session
            </button>
          </div>
        ) : null}
        {safeSession ? (
          <SafeSessionCard view={safeSession} />
        ) : showStructuredBlocks ? (
          // A previewed day is read on its own screen; today's work stays here.
          isSessionPreview ? (
            previewOpen ? (
              <InjuryDetailSheet
                title={shownTitle}
                variant="screen"
                closeLabel="Back to Today"
                onClose={() => fadeThrough(() => setPreviewOpen(false))}
              >
                <div className="today-preview-screen">
                  <p className="today-preview-meta">
                    <span>{relationCopy.kicker}</span>
                    {sessionDateLabel ? <span>{sessionDateLabel}</span> : null}
                  </p>
                  {summaryChips.length > 0 ? (
                    <SummaryChipList chips={summaryChips} />
                  ) : null}
                  <TodaySessionBlocks
                    planId={state.active_plan?.id}
                    current={current}
                    headline={shownTitle}
                    openWeekIntent={openWeekIntent}
                    rehabLabelPolicy={rehabLabelPolicy}
                    exerciseMedia={exerciseMedia}
                    exerciseLogging={exerciseLogging}
                    accessToken={token}
                    exerciseCustomisation={exerciseCustomisation}
                  />
                  <p className="today-pending-line">
                    <span className="today-pending-pill">Pending</span>
                    {unlockCopy}
                  </p>
                </div>
              </InjuryDetailSheet>
            ) : null
          ) : (
            <TodaySessionBlocks
              planId={state.active_plan?.id}
              current={current}
              headline={shownTitle}
              openWeekIntent={openWeekIntent}
              rehabLabelPolicy={rehabLabelPolicy}
              exerciseMedia={exerciseMedia}
              exerciseLogging={exerciseLogging}
              accessToken={token}
              exerciseCustomisation={exerciseCustomisation}
            />
          )
        ) : presentationPending ? (
          // The plan read decides between the blocks and this summary; showing
          // the summary first made the card change twice.
          isSessionPreview ? null : <Skeleton variant="block" height={140} />
        ) : (
          <div className="today-session-summary">
            <div>
              <p className="today-detail-label">Day</p>
              <p>{formatSessionDate(session)}</p>
            </div>
            <div>
              <p className="today-detail-label">Focus</p>
              <p>{getSessionFocus(session)}</p>
            </div>
            {session.coach_led_contact ? (
              <div>
                <p className="today-detail-label">Coach contact</p>
                <p>{athleteFacingSessionTitle(session.coach_led_contact)}</p>
              </div>
            ) : null}
            {duration ? (
              <div>
                <p className="today-detail-label">Duration</p>
                <p>{duration}</p>
              </div>
            ) : null}
            {!isSessionPreview ? (
              <div>
                <p className="today-detail-label">Status</p>
                <p>{getCompletionLabel(status)}</p>
              </div>
            ) : null}
          </div>
        )}
        {isSessionPreview && !safeSession ? (
          <div className="today-next-planned-note">
            <p className="today-pending-line">
              <span className="today-pending-pill">Pending</span>
              {unlockCopy}
            </p>
            {nextIsHardCombat ? (
              <div className="today-caution-row">
                <span className="today-caution-label">Caution</span>
                <span className="today-caution-text">
                  Combat session next. Re-check fatigue, pain and injury before clearing.
                </span>
              </div>
            ) : null}
          </div>
        ) : null}

        {!canCompleteSession && !safeSession && !isSessionPreview ? (
          <div className="today-terminal-block">
            <p
              className="today-terminal-status"
              data-tone={decisionBlocksCurrentSession || contactClearanceBlocked ? "blocked" : "neutral"}
            >
              {terminalStatusCopy}
            </p>
            {severeInjuryBlocksCurrentSession ? (
              <a href="#today-injury" className="secondary-button today-terminal-action">
                Open injury check-in
              </a>
            ) : null}
          </div>
        ) : null}

        {canCompleteSession && !rehabOnlyPrescription && status === "started" ? (
          <div className="today-session-actions today-action-tray">
            {canResume ? (
            <button
              type="button"
              className="cta"
              onClick={() => {
                if (roundTimer.shown && (timerAvailable || guidedLeads || (contactIsSession && contactTimerAvailable))) {
                  showToast("Close the round timer to resume your session timer.", { tone: "info" });
                  return;
                }
                if (timerAvailable) {
                  openTimer("session");
                  return;
                }
                if (guidedLeads) {
                  fadeThrough(() => {
                    setGuidedNext("review");
                    setGuidedOpen(true);
                  });
                  return;
                }
                if (contactIsSession && contactTimerAvailable) {
                  openTimer("contact");
                  return;
                }
                showToast("Session is in progress.", { tone: "info" });
              }}
              disabled={isSubmitting}
            >
              Resume session
            </button>
            ) : null}
            {/* The session is logged on its own card, from the exercises: there
                is no separate done / modified pick to make and keep in step. */}
            <button
              type="button"
              className={canResume ? "secondary-button" : "cta"}
              onClick={() => timerHandoffPending ? openTimer("session") : openReview(false)}
              disabled={isSubmitting}
            >
              <ToolIcon name="check" />
              {timerHandoffPending ? "Save timer results" : "Finish session"}
            </button>
            {timerTools(
              <button
                type="button"
                className="today-tool-link"
                onClick={() => setIntent("skipped")}
                disabled={isSubmitting}
              >
                Skip session
              </button>,
            )}
          </div>
        ) : null}

        {canCompleteSession && (status === "done" || status === "modified" || status === "skipped") ? (
          <p className="today-terminal-status">{getCompletionLabel(status)}</p>
        ) : null}

        {/* Locked, logged or blocked sessions still get the timer shortcuts. */}
        {!(canCompleteSession && (status === "not_started" || status === "started")) &&
        (contactTarget || freeTimerAvailable) &&
        !anyTimerShown ? (
          <div className="today-session-actions today-action-tray">{timerTools()}</div>
        ) : null}

        {rehabOnlyPrescription && (canCompleteSession || guidanceCanCheckOff) && !["done", "modified", "skipped"].includes(status) ? <div className="today-injury-guidance" role="group" aria-label={calmOnly ? "Did you follow the guidance?" : "Did you complete your rehab?"}>
          <p>{calmOnly ? "Did you follow the guidance?" : "Did you complete your rehab?"}</p>
          <div className="today-segment-row">
            {([["done", "Completed"], ["modified", "Modified"], ["skipped", "Skipped"]] as const).map(([value, label]) => {
              const chosen = rehabCheckOff?.value === value;
              return <button key={value} type="button" className={chosen ? "today-segment today-segment-chosen" : "today-segment"}
                aria-pressed={chosen} disabled={isSubmitting && !chosen}
                onClick={() => { if (!rehabCheckOff?.saved) setRehabCheckOff(chosen && timerBusy ? null : { value, label }); }}>{label}</button>;
            })}
          </div>
          {rehabCheckOffPending ? <p className="today-rehab-checkoff-note" role="status">
            {rehabCheckOff?.saved ? "Saved." : timerBusy ? "Saved when you finish the timer." : "Saving…"}
          </p> : null}
        </div> : null}
        {sparringPrompt}

        {livePrescription?.safety_hold ? <p role="alert">{livePrescription.safety_hold_reason || "This session is on hold. Follow the current injury guidance before training."}</p> : null}
        {livePrescription?.safety_hold && resolvedDecision.sessionIsToday ? <div className="today-session-actions">
          {livePrescription.frozen && status === "started" ? <button type="button" className="secondary-button" disabled={isSubmitting} onClick={() => setIntent("modified")}>Log stopped session</button> : null}
          <button type="button" className="ghost-button" disabled={isSubmitting} onClick={() => setIntent("skipped")}>Mark skipped</button>
        </div> : null}
        {canCompleteSession && !rehabOnlyPrescription && intent === "done" && sessionHasRehab ? rehabChoice : null}

        {(!rehabOnlyPrescription && canCompleteSession) || (livePrescription?.safety_hold && (intent === "skipped" || (intent === "modified" && status === "started"))) ? (
          <SessionCompletionForm
            key={`${intent ?? "closed"}:${timerNotes}`}
            intent={intent}
            initialNotes={timerNotes}
            isSubmitting={isSubmitting}
            submissionBlockedReason={rehabChoiceRequired && !rehabPerformance ? "Choose Completed, Modified or Skipped for rehab." : undefined}
            onCancel={() => setIntent(null)}
            onSubmit={async (nextStatus, details) => {
              await saveCompletion(nextStatus, {
                sessionRpe: details.sessionRpe,
                painAfter: details.painAfter,
                modificationReason: details.modificationReason,
                notes: details.notes,
              });
            }}
          />
        ) : null}

      </AutoHeight>
      {renderTimer(headline)}

      {guidedOpen && guidedVisualisation ? (
        <GuidedVisualisation
          visualisation={guidedVisualisation}
          firstName={firstNameOf(athleteFullName)}
          level={fightLevel(professionalStatus)}
          finishLabel={guidedNext === "timer" ? "Continue to training" : "Finish session"}
          upNext={guidedNext === "timer" ? timerItems[0]?.title ?? null : null}
          skipLabel="Skip to training"
          onClose={() => fadeThrough(() => {
            setGuidedOpen(false);
            setGuidedNext(null);
          })}
          onComplete={logVisualisationDone}
          onFinish={guidedNext ? continueAfterGuided : undefined}
          onSkip={guidedNext === "timer" ? continueAfterGuided : undefined}
        />
      ) : null}

      {reviewOpen && canCompleteSession && status === "started" ? (
        <SessionReview
          title={headline}
          sessions={showStructuredBlocks ? current.sessions : []}
          logging={exerciseLogging}
          hint={
            reviewFromTimer
              ? "Ticked from your timer. Tap an exercise to add weight or edit it."
              : "Tick what you did. Tap an exercise to add weight or edit it."
          }
          blocks={
            showStructuredBlocks ? (
              <TodaySessionBlocks
                planId={state.active_plan?.id}
                current={current}
                headline={headline}
                rehabLabelPolicy={rehabLabelPolicy}
                exerciseMedia={exerciseMedia}
                exerciseLogging={exerciseLogging}
                accessToken={token}
                exerciseCustomisation={exerciseCustomisation}
              />
            ) : null
          }
          canCollectPain={painReasonAllowed}
          extraAction={
            guidedVisualisation?.blockId && exerciseLogging && !exerciseLogging.logs[guidedVisualisation.blockId] ? (
              <button
                type="button"
                className="today-review-chip"
                data-tone="accent"
                onClick={() => {
                  // Played over the review; finishing it ticks the row and
                  // comes straight back here.
                  fadeThrough(() => {
                    setGuidedNext("review");
                    setGuidedOpen(true);
                  });
                }}
              >
                <ToolIcon name="voice" />
                Play {guidedVisualisation.name} now
              </button>
            ) : null
          }
          rehabChoice={sessionHasRehab ? rehabChoice : null}
          doneBlockedReason={
            sessionHasRehab && !rehabPerformance ? "Choose Completed, Modified or Skipped for rehab." : undefined
          }
          isSubmitting={isSubmitting}
          onClose={() => fadeThrough(() => setReviewOpen(false))}
          onSave={(nextStatus, details) => saveCompletion(nextStatus, details)}
        />
      ) : null}

      <Collapse show={Boolean(reviewableSession)}>
        {reviewableSession ? (
          <SessionFeedbackPrompt
            key={reviewableSession.sessionId}
            token={token}
            planId={reviewableSession.planId}
            sessionId={reviewableSession.sessionId}
            onDismiss={() => setReviewableSession(null)}
          />
        ) : null}
      </Collapse>
    </section>
  );
}

/** When a previewed session opens, in the athlete's terms ("tomorrow", not
 * "on the day"), and that an open injury may still change it at check-in. */
export function previewUnlockCopy(
  sessionDate: string | null | undefined,
  trainingDay: string | null | undefined,
  hasOpenInjury: boolean,
): string {
  const days = sessionDate && trainingDay
    ? Math.round((Date.parse(`${sessionDate.slice(0, 10)}T00:00:00Z`) - Date.parse(`${trainingDay.slice(0, 10)}T00:00:00Z`)) / 86_400_000)
    : NaN;
  const when = days === 1
    ? "tomorrow"
    : days > 1 && days < 7
      ? new Date(`${sessionDate!.slice(0, 10)}T00:00:00Z`).toLocaleDateString("en-GB", { weekday: "long", timeZone: "UTC" })
      : "on the day";
  const unlock = `Available ${when} after check-in.`;
  return hasOpenInjury ? `${unlock} It may be adjusted for your injury.` : unlock;
}
