"use client";

import { useEffect, useState } from "react";

import { SessionFeedbackPrompt } from "@/components/feedback/session-feedback-prompt";
import {
  SessionCompletionForm,
  type CompletionIntent,
} from "@/components/session-completion-form";
import {
  DaySessionContext,
  RehabLabelProvider,
  SessionCard as StructuredSessionCard,
  SessionlessDayCard,
} from "@/components/structured-plan-renderer";
import { formatTrainingDay } from "@/components/today/format";
import { DelayedRehabResponse, RehabResponsePrompt } from "@/components/today/rehab-response-prompt";
import { TodaySessionGuidance } from "@/components/today/today-session-guidance";
import { useToast } from "@/components/toast-provider";
import { listPendingRehabResponses, submitTodaySessionCompletion } from "@/lib/api";
import {
  resolveCurrentDay,
  resolveOpenPlanWeekNumber,
  sessionIdentity,
  type CurrentDayResolution,
} from "@/lib/camp-map";
import type { TodayPlanSchedule } from "@/components/today/use-today-command";
import { openBlockWeekIntent, type OpenBlockWeekIntent } from "@/lib/open-block";
import { isOpenOngoingPlan } from "@/lib/plan-format";
import { humanizeIfRawEnum } from "@/lib/plan-labels";
import { shouldPromptSessionFeedback } from "@/lib/session-feedback";
import { simpleRehabOutcome, usesSimpleRehabCompletion } from "@/lib/rehab-completion";
import { useTrainingDay } from "@/lib/use-training-day";
import {
  getCompletionLabel,
  getSafeSessionView,
  getSessionFocus,
  getSessionTitle,
  isHardCombatSession,
  resolveTodayDecision,
  resolveSessionFocusDate,
  type SafeSessionView,
} from "@/lib/today";
import type {
  RehabLabelPolicy,
  PendingRehabResponseSet,
  StructuredPlan,
  TodayCommandView,
  TodayCompletionStatus,
  TodaySession,
} from "@/lib/types";

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

function textValue(value: string | null | undefined): string {
  return typeof value === "string" ? value.trim() : "";
}

function getStructuredTodaySessionTitle(current: CurrentDayResolution): string {
  const session = current.sessions[0];
  const card = current.day?.today_card;
  return (
    textValue(session?.title) ||
    textValue(card?.headline) ||
    humanizeIfRawEnum(textValue(session?.session_type))
  );
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
  openWeekIntent,
  rehabLabelPolicy,
}: {
  planId?: string;
  current: CurrentDayResolution;
  /** Development-block week intent of an open (renewable) plan: headlines where
   * today sits in the block and forwards the per-block directive to the cards. */
  openWeekIntent?: OpenBlockWeekIntent | null;
  /** Per-region Rehab/Prehab policy from the active plan. Today renders
   * SessionCard directly rather than through Plan Detail's full-plan renderer,
   * so it has to mount the provider itself; without it every rehab block on this
   * screen read "Rehab" no matter which injuries had cleared. */
  rehabLabelPolicy?: RehabLabelPolicy | null;
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
      <div className="today-blocks">
        {weekIntentNote}
        <DaySessionContext day={displayDay} />
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
            defaultOpenBlocks
            showDayContext={false}
            openWeekIntent={openWeekIntent}
          />
        ))}
      </div>
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
  token,
  onRefresh,
}: {
  state: TodayCommandView;
  structuredPlan: StructuredPlan | null;
  /** Server-derived per-region Rehab/Prehab policy for the active plan. */
  rehabLabelPolicy?: RehabLabelPolicy | null;
  /** Server schedule projection + plan creation date, used to anchor the
   * current week of a weekday-only (open / renewable) plan. */
  planSchedule?: TodayPlanSchedule | null;
  token: string;
  onRefresh: () => Promise<void>;
}) {
  const { showToast } = useToast();
  const [intent, setIntent] = useState<CompletionIntent>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [rehabPerformance, setRehabPerformance] = useState<"done_as_shown" | "changed" | "stopped" | undefined>();
  // The session the athlete just logged as trained, captured at write time.
  // The refresh that follows can advance `next_session` to tomorrow's card, so
  // reading the id live would attach the review to the wrong session.
  const [reviewableSession, setReviewableSession] = useState<
    { planId: string; sessionId: string } | null
  >(null);
  // Interaction stays local, but existence comes from durable server context.
  // This is an array so two completed sessions on one day cannot hide each
  // other's independently pending injury response.
  const [rehabResponses, setRehabResponses] = useState<PendingRehabResponseSet[]>([]);
  const session = state.today.next_session;
  const status = state.today.completion_status;
  const duration = getSessionDuration(session);
  const resolvedDecision = resolveTodayDecision(state);
  const hasSession = resolvedDecision.hasSession;
  // Resolve today's day/session from the structured plan through the shared
  // 03:00 rollover, exactly as Plan Detail does. These blocks — not the backend
  // session summary — are the "what exact blocks apply today" answer. The
  // training day comes from the client-mounted hook (SSR-safe, null until mount)
  // and is resolved on every render so a long-lived tab follows the rollover
  // instead of sticking on a memoized day.
  const trainingDay = useTrainingDay();
  const activePlanId = state.active_plan.id ?? "";
  useEffect(() => {
    if (!token || !activePlanId) {
      return;
    }
    let cancelled = false;
    void listPendingRehabResponses(token, activePlanId)
      .then((pending) => {
        if (!cancelled) {
          setRehabResponses(pending.response_sets);
          if (pending.history_truncated && process.env.NODE_ENV !== "production") {
            console.warn("Pending rehab response history was truncated");
          }
        }
      })
      .catch((error: unknown) => {
        // A read failure must not fabricate an answered state. Keep any prompt
        // already in memory; a remount/retry can retrieve the durable context.
        if (process.env.NODE_ENV !== "production") {
          console.error("Pending rehab responses could not be loaded", error);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [activePlanId, token]);
  // Center the structured blocks on whatever the backend command view targets:
  // today's day in the normal case, or the NEXT scheduled session's day once
  // today is logged / carries no app card (session_relation === "next"). Resolving
  // by that target day — not the bare calendar day — stops the card from sticking
  // on the finished session while the header has already advanced to "Next
  // session", which is exactly how Overview already behaves.
  const focusDate = resolveSessionFocusDate(trainingDay, session);
  // Open (renewable) plans carry weekday-only days with no calendar dates, so
  // the resolver needs to know which week of the block "today" falls in. Dated
  // camps ignore the hint — they still resolve purely by calendar date.
  const openWeekNumber = resolveOpenPlanWeekNumber(structuredPlan, focusDate, {
    currentWeekNumber: planSchedule?.scheduleContext?.current_week_number,
    anchorDate: planSchedule?.scheduleContext?.anchor_date,
    createdAt: planSchedule?.createdAt,
  });
  const openOngoing = Boolean(
    state.active_plan && isOpenOngoingPlan(state.active_plan.fight_date),
  );
  const storedCurrent = resolveCurrentDay(structuredPlan, focusDate, {
    openWeekNumber,
    allowDatedWeekdayMatch: openOngoing,
  });
  const livePrescription = state.live_prescription;
  const rehabOnlyPrescription = livePrescription?.session.session_type === "rehab";
  const simpleRehab = usesSimpleRehabCompletion(livePrescription?.session);
  const current: CurrentDayResolution = livePrescription ? {
    ...storedCurrent, inRange: true,
    day: {
      ...(storedCurrent.day ?? {}), date: state.today.training_day, sessions: [livePrescription.session],
      ...(rehabOnlyPrescription ? {
        day_type: "recovery",
        today_card: {
          headline: livePrescription.session.title,
          nutrition_summary: storedCurrent.day?.today_card?.nutrition_summary,
          weight_cut_warning: storedCurrent.day?.today_card?.weight_cut_warning,
        },
      } : livePrescription.changes.some(change => change.action === "contact_held") ? {
        today_card: { ...storedCurrent.day?.today_card, coach_led_contact: null },
      } : {}),
    },
    sessions: [livePrescription.session],
  } : storedCurrent;
  // Where the resolved day sits in the renewable development block (baseline /
  // progress / peak / deload). The resolved week position wins over the bare
  // anchor-derived number so the note always matches the blocks shown below.
  // Dated camps stay null and render unchanged.
  const openWeekIntent = openOngoing && !rehabOnlyPrescription
    ? openBlockWeekIntent(current.weekPos != null ? current.weekPos + 1 : openWeekNumber)
    : null;
  const showStructuredBlocks = current.inRange && Boolean(current.day);
  const hasResolvedDaySessions = current.inRange && current.sessions.length > 0;
  const isSessionPreview = resolvedDecision.displayTier === "preview" || (hasSession && !resolvedDecision.sessionIsToday);
  const relationCopy = getSessionRelationCopy(
    session,
    status,
    resolvedDecision.sessionIsToday,
  );
  const decisionBlocksCurrentSession = resolvedDecision.blocksCurrentSession || Boolean(livePrescription?.safety_hold);
  const severeInjuryBlocksCurrentSession =
    resolvedDecision.severeInjuryBlocksCurrentSession;
  // STOP + the scheduled session is today: show the recovery/mobility safe
  // session in place of the real blocks so Today never displays hard combat as
  // available under a stop. Future sessions stay visible but read as pending.
  const safeSession =
    resolvedDecision.useSafeReplacement && !livePrescription
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
  const canCompleteSession = resolvedDecision.canCompleteSession;
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
      : isSessionPreview
      ? "Preview only. Completion opens on the matched training day."
      : resolvedDecision.authoritativeTier === "not_checked_in"
        ? "Submit today's check-in to unlock session actions."
      : "Session details available, but completion is unavailable for this entry.";

  async function saveCompletion(
    nextStatus: TodayCompletionStatus,
    details: {
      sessionRpe?: number | null;
      painAfter?: number | null;
      modificationReason?: string;
      notes?: string;
    } = {},
  ) {
    if (!state.active_plan.id || !session.session_id || isSubmitting) {
      return;
    }
    setIsSubmitting(true);
    try {
      const simpleOutcome = simpleRehab && (nextStatus === "done" || nextStatus === "modified" || nextStatus === "skipped")
        ? simpleRehabOutcome(nextStatus) : null;
      const completion = await submitTodaySessionCompletion(token, {
        plan_id: state.active_plan.id,
        session_id: session.session_id,
        status: nextStatus,
        session_rpe: details.sessionRpe ?? null,
        pain_after: details.painAfter ?? null,
        modification_reason: details.modificationReason ?? simpleOutcome?.modificationReason ?? "",
        notes: details.notes ?? "",
        prescription_revision: livePrescription?.revision,
        rehab_performance: nextStatus === "done" ? (simpleOutcome?.rehabPerformance ?? rehabPerformance) : nextStatus === "modified" && livePrescription ? (livePrescription.safety_hold ? "stopped" : "changed") : undefined,
      });
      setIntent(null);
      setRehabPerformance(undefined);
      // Non-empty only when the server established that this session contained
      // rehab attributable to a known injury, so a normal session never shows
      // this block.
      const prompts = completion.rehab_response_prompts ?? [];
      setRehabResponses((current) => {
        const others = current.filter(
          (item) => item.completion_id !== completion.completion.id,
        );
        if (prompts.length === 0) {
          return others;
        }
        return [
          ...others,
          {
            completion_id: completion.completion.id,
            plan_id: completion.completion.plan_id,
            session_id: completion.completion.session_id,
            training_day: completion.completion.training_day,
            rehab_response_prompts: prompts,
          },
        ];
      });
      showToast(getCompletionLabel(nextStatus), { tone: "success" });
      // Order matters: the confirmation toast is already up and the refresh
      // below is what surfaces the XP award, so the review prompt is queued
      // here and only renders once both have landed.
      setReviewableSession(
        !simpleRehab && shouldPromptSessionFeedback(nextStatus)
          ? { planId: state.active_plan.id, sessionId: session.session_id }
          : null,
      );
      await onRefresh();
    } catch (error) {
      showToast(error instanceof Error ? error.message : "Session update failed.", { tone: "error" });
      // A rejected stale revision needs fresh content before another start.
      await onRefresh();
    } finally {
      setIsSubmitting(false);
    }
  }

  const rehabFeedback = <>
    {(state.delayed_rehab_prompts ?? []).map(prompt => <DelayedRehabResponse key={prompt.exposure_id} prompt={prompt} token={token} onRefresh={onRefresh} />)}
    {rehabResponses.map(pending => <RehabResponsePrompt key={pending.completion_id}
      token={token} planId={pending.plan_id} sessionId={pending.session_id} trainingDay={pending.training_day}
      prompts={pending.rehab_response_prompts} onRefresh={onRefresh}
      onDismiss={() => setRehabResponses(current => current.filter(item => item.completion_id !== pending.completion_id))} />)}
  </>;

  if (!hasSession) {
    return (
      <section
        id="today-session"
        className="today-card today-session-card"
        data-tone={cardTone}
        aria-labelledby="today-session-heading"
      >
        <div className="today-card-head">
          <div>
            <p className="kicker">Today&apos;s session</p>
            {/* training_day is always present in the initial payload, so headline
                it unconditionally — gating on the async-loaded structuredPlan
                caused a flash from "No session scheduled" to the date on load. */}
            <h2 id="today-session-heading">
              {formatTrainingDay(state.today.training_day)}
            </h2>
          </div>
        </div>
        {showStructuredBlocks ? (
          <TodaySessionBlocks
            planId={state.active_plan?.id}
            current={current}
            openWeekIntent={openWeekIntent}
            rehabLabelPolicy={rehabLabelPolicy}
          />
        ) : (
          <p className="muted">No active plan card matched today. Use Open camp plan to find the next training target.</p>
        )}
        {rehabFeedback}
      </section>
    );
  }

  const sessionTitle = hasResolvedDaySessions
    ? getStructuredTodaySessionTitle(current) || getSessionTitle(session)
    : getSessionTitle(session);
  // Avoid the "Today's session / Today's session" stutter: when the session has
  // no real name and falls back to the generic title that already matches the
  // kicker, headline the training day instead so the eyebrow and heading differ.
  const headline =
    sessionTitle.trim().toLowerCase() === "today's session" ||
    sessionTitle.trim().toLowerCase() === relationCopy.kicker.trim().toLowerCase()
      ? formatSessionDate(session)
      : sessionTitle;

  return (
    <section
      id="today-session"
      className="today-card today-session-card"
      data-tone={cardTone}
      aria-labelledby="today-session-heading"
    >
      <div className="today-card-head">
        <div>
          <p className="kicker">{relationCopy.kicker}</p>
          <h2 id="today-session-heading">{headline}</h2>
        </div>
      </div>
      <TodaySessionGuidance prescription={livePrescription} />
      {safeSession ? (
        <SafeSessionCard view={safeSession} />
      ) : showStructuredBlocks ? (
        <TodaySessionBlocks
          planId={state.active_plan?.id}
          current={current}
          openWeekIntent={openWeekIntent}
          rehabLabelPolicy={rehabLabelPolicy}
        />
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
              <p>{session.coach_led_contact}</p>
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
            Check in on the day to unlock this session.
          </p>
          {nextIsHardCombat ? (
            <div className="today-caution-row">
              <span className="today-caution-label">Caution</span>
              <span className="today-caution-text">
                Combat session planned next. Re-check fatigue, pain, and injury status before clearing.
              </span>
            </div>
          ) : null}
        </div>
      ) : null}

      {!canCompleteSession && !safeSession && !livePrescription?.safety_hold ? (
        <div className="today-terminal-block">
          <p
            className="today-terminal-status"
            data-tone={decisionBlocksCurrentSession ? "blocked" : "neutral"}
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

      {canCompleteSession && simpleRehab && (status === "not_started" || status === "started") ? (
        <div className="today-action-row today-sticky-actions" role="group" aria-label="Rehab outcome">
          <button type="button" className="cta" disabled={isSubmitting} onClick={() => void saveCompletion("done")}>Completed</button>
          <button type="button" className="secondary-button" disabled={isSubmitting} onClick={() => void saveCompletion("modified")}>Modified</button>
          <button type="button" className="ghost-button" disabled={isSubmitting} onClick={() => void saveCompletion("skipped")}>Not done</button>
        </div>
      ) : null}

      {canCompleteSession && !simpleRehab && status === "not_started" ? (
        <div className="today-action-row today-sticky-actions">
          <button type="button" className="cta" onClick={() => void saveCompletion("started")} disabled={isSubmitting}>
            Start session
          </button>
          <button type="button" className="ghost-button" onClick={() => setIntent("skipped")} disabled={isSubmitting}>
            Mark skipped
          </button>
        </div>
      ) : null}

      {canCompleteSession && !simpleRehab && status === "started" ? (
        <div className="today-action-row today-sticky-actions">
          <button
            type="button"
            className="cta"
            onClick={() => showToast("Session is in progress.", { tone: "info" })}
            disabled={isSubmitting}
          >
            Resume session
          </button>
          <button type="button" className="secondary-button" onClick={() => setIntent("done")} disabled={isSubmitting}>
            Mark done
          </button>
          <button type="button" className="secondary-button" onClick={() => setIntent("modified")} disabled={isSubmitting}>
            Mark modified
          </button>
          <button type="button" className="ghost-button" onClick={() => setIntent("skipped")} disabled={isSubmitting}>
            Mark skipped
          </button>
        </div>
      ) : null}

      {canCompleteSession && (status === "done" || status === "modified" || status === "skipped") ? (
        <p className="today-terminal-status">{getCompletionLabel(status)}</p>
      ) : null}

      {livePrescription?.safety_hold && resolvedDecision.sessionIsToday ? <div className="today-session-actions">
        {livePrescription.frozen && status === "started" ? <button type="button" className="secondary-button" disabled={isSubmitting} onClick={() => setIntent("modified")}>Log stopped session</button> : null}
        <button type="button" className="ghost-button" disabled={isSubmitting} onClick={() => setIntent("skipped")}>Mark skipped</button>
      </div> : null}
      {canCompleteSession && !simpleRehab && intent === "done" && livePrescription?.session.blocks?.some(block => block.block_type === "rehab") ? (
        <div role="group" aria-label="How much rehab did you do?" className="today-injury-guidance">
          <p>How much rehab did you do?</p>
          <div className="today-segment-row">
            {([ ["done_as_shown", "Done as shown"], ["changed", "Changed it"], ["stopped", "Stopped early"] ] as const).map(([value, label]) => (
              <button key={value} type="button" aria-pressed={rehabPerformance === value} onClick={() => setRehabPerformance(value)}>{label}</button>
            ))}
          </div>
        </div>
      ) : null}
      {canCompleteSession || (livePrescription?.safety_hold && (intent === "skipped" || (intent === "modified" && status === "started"))) ? (
        <SessionCompletionForm
          key={intent ?? "closed"}
          intent={intent}
          isSubmitting={isSubmitting}
          onCancel={() => setIntent(null)}
          onSubmit={(nextStatus, details) =>
            saveCompletion(nextStatus, {
              sessionRpe: details.sessionRpe,
              painAfter: details.painAfter,
              modificationReason: details.modificationReason,
              notes: details.notes,
            })
          }
        />
      ) : null}

      {/* Above the session review on purpose: an injury observation is the more
          time-sensitive of the two, and the athlete should not have to get past
          a programming survey to report that something hurt. */}
      {rehabFeedback}

      {reviewableSession ? (
        <SessionFeedbackPrompt
          key={reviewableSession.sessionId}
          token={token}
          planId={reviewableSession.planId}
          sessionId={reviewableSession.sessionId}
          onDismiss={() => setReviewableSession(null)}
        />
      ) : null}
    </section>
  );
}
