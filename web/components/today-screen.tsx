"use client";

import { useSeenOnce } from "@/components/once-hint";
import { Collapse } from "@/components/today/collapse";

import Link from "next/link";
import { TrainingHabitLog } from "@/components/training-habit-log";
import { useEffect, useMemo, useRef, useState } from "react";
import { ARRIVING_PLAN_PARAM } from "@/lib/generation-status-guards";
import { hasProgrammeArrival, consumeProgrammeArrival, PROGRAMME_ARRIVAL_EVENT, programmeArrivalHeadline } from "@/lib/programme-arrival";
import styles from "./today-screen.module.css";

import { useAppSession } from "@/components/auth-provider";
import { EnableRemindersRow } from "@/components/today/enable-reminders-row";
import { useConnectivity, useOnReconnect } from "@/components/connection-status-banner";
import { hasHealthDataConsent } from "@/lib/compliance";
import { ContextualFeedback } from "@/components/feedback/contextual-feedback";
import { Skeleton } from "@/components/skeleton";
import { formatTrainingDay } from "@/components/today/format";
import { AutoHeight } from "@/components/today/auto-height";
import { TodayDecisionPanel } from "@/components/today/today-decision-panel";
import { TodayInjuryManager } from "@/components/today/today-injury-manager";
import { TodayReadinessForm } from "@/components/today/today-readiness-form";
import { TodayRiskWatch } from "@/components/today/today-risk-watch";
import { StandaloneRehab } from "@/components/today/standalone-rehab";
import { TodaySessionPanel } from "@/components/today/today-session-panel";
import { TodayWeighInCard } from "@/components/today/today-weigh-in-card";
import { useTodayCommand } from "@/components/today/use-today-command";
import { resolvePlanProgress } from "@/lib/camp-map";
import { isOpenOngoingPlan } from "@/lib/plan-format";
import { isCalmRehabBlock } from "@/lib/rehab-label";
import { inFinalWeeks } from "@/lib/weight-cut-status";
import { rehabExerciseTarget } from "@/lib/rehab-exercise-guide";
import {
  getCompletionLabel,
  getDistinctTodayRiskWatch,
  getPlanFreeGuidanceBanner,
  getSupplementaryRiskWatch,
  hasActivePlan,
  resolveTodayDecision,
  shouldShowTodayCheckin,
  withoutInjuryTrackingReminder,
} from "@/lib/today-authoritative";
import type { TodayCompletionStatus } from "@/lib/types";

// Same window Plan Detail allows a just-saved plan (5 reads, 1.5s apart).
const ARRIVAL_MAX_REREADS = 4;
const ARRIVAL_REREAD_DELAY_MS = 1500;

function TodayLoadingState() {
  return (
    <div className={`today-page ${styles.loading}`} aria-busy="true" aria-label="Loading Today">
      <section className={`panel today-shell ${styles.command}`} aria-hidden="true">
        <Skeleton variant="text" width={120} height={42} />
        <Skeleton variant="text" width="55%" height={12} />
        <ul className={styles.tiles}>
          {[0, 1, 2].map(index => (
            <li key={index} className={styles.tile}>
              <Skeleton variant="circle" width={30} height={30} />
              <span className={styles.tileText}>
                <Skeleton variant="text" width={48} height={10} />
                <Skeleton variant="text" width={48} height={14} />
              </span>
            </li>
          ))}
        </ul>
      </section>
      <section className={`panel ${styles.loadingSession}`} aria-hidden="true" />
    </div>
  );
}

type TileIconName = "check" | "clock" | "pulse" | "injury" | "plan" | "history" | "timer";

const TILE_ICON_PATHS: Record<TileIconName, string> = {
  check: "M7 12.5l3.2 3.2L17 9",
  clock: "M12 7.5V12l3 2",
  pulse: "M5 12h3l2-4 4 8 2-4h3",
  injury: "M12 7v6M12 16.5v.5",
  plan: "M7 7h10M7 12h10M7 17h6",
  history: "M5 12a7 7 0 107-7 7 7 0 00-5 2.1M5 5v3h3M12 8.5V12l2.5 1.5",
  timer: "M12 13V9.5M9.5 3h5M12 21a8 8 0 100-16 8 8 0 000 16z",
};

function TileIcon({ name, ring = false }: { name: TileIconName; ring?: boolean }) {
  return (
    <svg className={styles.icon} viewBox="0 0 24 24" aria-hidden="true">
      <g fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        {ring ? <circle cx="12" cy="12" r="10" /> : null}
        <path d={TILE_ICON_PATHS[name]} />
      </g>
    </svg>
  );
}

const SESSION_TILE_VALUE: Record<TodayCompletionStatus, string> = {
  not_started: "Not started",
  started: "In progress",
  done: "Complete",
  // "modified" is a logged completion (done with changes), not an adjusted plan.
  modified: "Completed",
  skipped: "Skipped",
};

// One status tile. With a same-page target the whole tile is the jump-link to
// the section that resolves it; without one it is a plain readout.
function StatusTile({
  label,
  value,
  tone,
  icon,
  href,
  actionLabel,
  modified = false,
}: {
  label: string;
  value: string;
  tone?: "pending" | "clear" | "risk";
  icon: TileIconName;
  href?: string;
  actionLabel: string;
  modified?: boolean;
}) {
  const body = (
    <>
      <span className={styles.tileIcon}>
        <TileIcon name={icon} ring />
      </span>
      <span className={styles.tileText}>
        <span className={styles.tileLabel}>{label}</span>
        <span className={styles.tileValue}>{value}</span>
        {modified ? <span className={styles.tileModifier}>Modified</span> : null}
      </span>
      {href ? <span className={styles.chevron} aria-hidden="true" /> : null}
    </>
  );
  return (
    <li className={styles.tile} data-tone={tone}>
      {href ? (
        <a href={href} className={styles.tileLink}>
          {body}
          <span className="sr-only">, {actionLabel}</span>
        </a>
      ) : (
        body
      )}
    </li>
  );
}

function TodayReadinessStrip({
  hasPlan,
  canCheckIn,
  loggedToday,
  needsCheckin,
  openInjuryCount,
  completionStatus,
  checkinHref,
  injuriesHref,
  sessionHref,
}: {
  hasPlan: boolean;
  canCheckIn: boolean;
  loggedToday: boolean | null;
  needsCheckin: boolean;
  openInjuryCount: number;
  completionStatus: TodayCompletionStatus;
  checkinHref?: string;
  injuriesHref?: string;
  sessionHref?: string;
}) {
  // Tones: pending (amber) = needs the athlete's action, clear (green) =
  // handled, risk (red) = open injuries. The session reads pending while in
  // progress and clear once any completion is logged.
  const sessionLogged =
    completionStatus === "done" ||
    completionStatus === "modified" ||
    completionStatus === "skipped";
  const sessionTone = sessionLogged ? "clear" : completionStatus === "started" ? "pending" : undefined;

  return (
    <ul className={styles.tiles} aria-label="Today command status">
      {/* The check-in is the same with or without a plan. Without health-data
          consent there is none, so it must not read as due or as done. */}
      {canCheckIn ? (
        <StatusTile
          label="Check-in"
          value={needsCheckin ? "Due" : "Done"}
          tone={needsCheckin ? "pending" : "clear"}
          icon={needsCheckin ? "clock" : "check"}
          href={checkinHref}
          actionLabel="Go to today's check-in"
        />
      ) : (
        <StatusTile label="Readiness" value="Not assessed" icon="pulse" actionLabel="" />
      )}
      {hasPlan ? (
        <StatusTile
          label="Session"
          value={SESSION_TILE_VALUE[completionStatus] ?? getCompletionLabel(completionStatus)}
          modified={completionStatus === "modified"}
          tone={sessionTone}
          icon={sessionLogged ? "check" : completionStatus === "started" ? "pulse" : "clock"}
          href={sessionHref}
          actionLabel="Go to today's session"
        />
      ) : (
        <StatusTile
          label="Training"
          value={loggedToday ? "Logged" : "Log session"}
          tone={loggedToday ? "clear" : undefined}
          icon={loggedToday ? "check" : "history"}
          href="#today-training-log"
          actionLabel="Go to the training log"
        />
      )}
      <StatusTile
        label="Injuries"
        value={openInjuryCount ? `${openInjuryCount} active` : "None"}
        tone={openInjuryCount ? "risk" : "clear"}
        icon={openInjuryCount ? "injury" : "check"}
        href={injuriesHref}
        actionLabel="Go to injury manager"
      />
    </ul>
  );
}

export function TodayScreen() {
  const [, dismissPlanHint] = useSeenOnce("discovery-full-plan-seen");
  const { session, me } = useAppSession();
  const token = session?.access_token ?? null;
  const {
    state,
    structuredPlan,
    planSchedule,
    rehabLabelPolicy,
    exerciseMedia,
    isLoading,
    isRefreshing,
    presentationPending,
    error,
    refresh,
    patchSession,
  } = useTodayCommand(token);
  const { offline: isOffline } = useConnectivity();
  useOnReconnect(() => void refresh(), Boolean(error));

  const activePlan = state?.active_plan ?? {};
  const openOngoing = isOpenOngoingPlan(activePlan.fight_date);
  const planTitle = activePlan.name?.trim() || (openOngoing ? "Open training plan" : "Active fight camp");
  const hasPlan = hasActivePlan(activePlan);
  const trainingDate = state?.today.training_day;
  const day = trainingDate && /^\d{4}-\d{2}-\d{2}$/.test(trainingDate) ? new Date(`${trainingDate}T12:00:00`) : null;
  const progress = !openOngoing && day && !Number.isNaN(day.getTime()) ? resolvePlanProgress(structuredPlan, day) : null;
  const weekLabel = progress?.matchType === "calendar" && progress.currentWeekPos !== null
    ? ` · Week ${progress.currentWeekPos + 1} of ${progress.weekCount}` : "";
  // Readiness answers are health data. Without a plan the check-in is only
  // offered with consent; the planned check-in keeps its existing behaviour
  // (the server enforces consent for both).
  const canCheckIn = hasPlan || hasHealthDataConsent(me);
  const showCheckin = state && canCheckIn ? shouldShowTodayCheckin(state) : false;
  // Weight-to-target in a dated camp's final weeks. Weigh-ins are health data
  // and under-18s have no weight-cut feature, so both gate it.
  const showWeighIn = Boolean(token && hasPlan && !openOngoing && activePlan.fight_date && trainingDate
    && inFinalWeeks(activePlan.fight_date, trainingDate) && me && !me.profile.is_minor && hasHealthDataConsent(me));
  // Unknown (null) until shared history confirms it; never assumed.
  const [loggedToday, setLoggedToday] = useState<boolean | null>(null);
  const trainingDayLabel = useMemo(
    () => formatTrainingDay(state?.today.training_day),
    [state?.today.training_day],
  );

  const mergedExerciseMedia = useMemo(
    () => ({ ...exerciseMedia, ...state?.exercise_media }),
    [exerciseMedia, state?.exercise_media],
  );

  // Arriving from a just-built plan (?plan=<id>): the saved plan can briefly
  // lag its completion event. Re-read a few times instead of showing a
  // plan-free Today, then fall back to the plan page, which has its own retry.
  const [arrivingPlanId] = useState<string | null>(() =>
    typeof window === "undefined" ? null : new URLSearchParams(window.location.search).get(ARRIVING_PLAN_PARAM));
  const [offeredPlanId, setOfferedPlanId] = useState<string | null>(null);
  const activeArrivalId = state?.active_plan?.id ?? null;
  useEffect(() => {
    const check = () => {
      if (activeArrivalId && hasProgrammeArrival(activeArrivalId)) setOfferedPlanId(activeArrivalId);
    };
    const onCompletion = () => { check(); void refresh(); };
    check();
    window.addEventListener(PROGRAMME_ARRIVAL_EVENT, onCompletion);
    return () => window.removeEventListener(PROGRAMME_ARRIVAL_EVENT, onCompletion);
  }, [activeArrivalId, refresh]);
  const arrivalConfirmed = useRef<string | null>(null);
  const [arrivalAction, setArrivalAction] = useState<HTMLButtonElement | null>(null);
  const arrivalWeeks = structuredPlan?.weeks?.length ?? 0;
  const arrivalMetadata = programmeArrivalHeadline(arrivalWeeks, openOngoing,
    structuredPlan?.plan_metadata?.sport, structuredPlan?.athlete_context?.sport_profile);
  const showProgrammeArrival = Boolean(offeredPlanId && offeredPlanId === state?.active_plan?.id
    && !isLoading && !error && !presentationPending && structuredPlan && arrivalMetadata);
  useEffect(() => {
    if (!showProgrammeArrival || !offeredPlanId) return;
    // Effects run after the matching plan's summary is committed to the DOM.
    if (arrivalConfirmed.current !== offeredPlanId && consumeProgrammeArrival(offeredPlanId)) arrivalConfirmed.current = offeredPlanId;
    const sessionPanel = document.getElementById("today-session");
    const updateAction = () => {
      const action = Array.from(sessionPanel?.querySelectorAll<HTMLButtonElement>(".today-session-actions button.cta") ?? [])
        .find(button => !button.disabled && /^(start|resume)\b/i.test(button.textContent?.trim() ?? ""));
      setArrivalAction(action ?? null);
    };
    // The session owns its controls (including local timer/resume state).
    const observer = new window.MutationObserver(updateAction);
    if (sessionPanel) observer.observe(sessionPanel, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"] });
    const frame = window.requestAnimationFrame(updateAction);
    return () => { observer.disconnect(); window.cancelAnimationFrame(frame); };
  }, [showProgrammeArrival, offeredPlanId, state, presentationPending]);
  const arrivalReads = useRef(0);
  const awaitingArrivingPlan = Boolean(arrivingPlanId && state && state.active_plan?.id !== arrivingPlanId);
  useEffect(() => {
    if (!arrivingPlanId || !state || isRefreshing) return;
    if (state.active_plan?.id === arrivingPlanId) {
      const params = new URLSearchParams(window.location.search);
      if (params.has(ARRIVING_PLAN_PARAM)) {
        params.delete(ARRIVING_PLAN_PARAM);
        const query = params.toString();
        window.history.replaceState(window.history.state, "", `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`);
      }
      return;
    }
    if (arrivalReads.current >= ARRIVAL_MAX_REREADS) {
      // Rare fallback; a full load keeps Today free of a router dependency.
      window.location.replace(`/plans/${encodeURIComponent(arrivingPlanId)}`);
      return;
    }
    arrivalReads.current += 1;
    const timer = window.setTimeout(() => void refresh(), ARRIVAL_REREAD_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [arrivingPlanId, state, isRefreshing, refresh]);

  if (isLoading || awaitingArrivingPlan) {
    return <TodayLoadingState />;
  }
  if (error && (!state || /unauthorized|forbidden|not authenticated/i.test(error))) {
    const isAccessIssue = /unauthorized|forbidden|not authenticated/i.test(error);
    return (
      <section className="panel today-shell today-error-state">
        <div className="today-hero-copy">
          <p className="kicker">Today command feed</p>
          <h1>
            {isAccessIssue
              ? "Access is locked"
              : isOffline
                ? "You’re offline."
                : "Today is temporarily unavailable"}
          </h1>
          <p className="muted" role="alert">
            {isAccessIssue
              ? "Sign in with an active athlete account to unlock Today."
              : isOffline
                ? "Your saved training hasn’t changed. Today reloads the moment you reconnect."
                : "The live Today feed did not respond. Your saved training has not changed."}
          </p>
          {process.env.NODE_ENV !== "production" ? (
            <p className="today-error-detail">Technical detail: {error}</p>
          ) : null}
        </div>
        <div className="today-action-row">
          <button type="button" className="cta" onClick={() => void refresh()}>
            Retry Today
          </button>
          <Link href="/plans" className="secondary-button">
            Open Plans
          </Link>
          <Link href="/timer" className="secondary-button">
            Round timer
          </Link>
          <Link href="/" className="ghost-button">
            Overview
          </Link>
        </div>
      </section>
    );
  }

  // A missing feed is never read as "no plan" or "no injuries".
  if (!state) {
    return <TodayLoadingState />;
  }
  const resolvedDecision = resolveTodayDecision(state);
  const availableRehabInjuries = new Set((state.open_injuries ?? []).filter(injury => {
    const decision = injury.rehab_decision;
    return decision?.outcome === "prescribed_rehab" && (decision.schedule?.state === "due"
      || (state.live_prescription?.session.completion_status ?? state.today.completion_status) === "started" && decision.schedule?.state === "already_completed"
        && /started session/i.test(decision.schedule.reason));
  }).map(injury => injury.id));
  // Plain anchors, so they stay put through a refresh rather than blinking out.
  const rehabBlocks = (hasPlan ? resolvedDecision.sessionIsToday : Boolean(state.live_prescription?.rehab_only)) && !error
    && !state.live_prescription?.safety_hold
    && !["done", "modified", "skipped"].includes(state.live_prescription?.session.completion_status ?? state.today.completion_status)
    ? (state.live_prescription?.session.blocks ?? []).filter(block => rehabExerciseTarget(block)
      && availableRehabInjuries.has(block.injury_id ?? "")) : [];
  const supplementaryRisks = getSupplementaryRiskWatch(
    state.risk_watch,
    resolvedDecision,
  );
  const visibleTriggerLabels =
    resolvedDecision.displayTier === "preview" && !resolvedDecision.currentGuidanceBanner
      ? []
      : state.today.recommendation_trigger_labels;
  const openInjuryCount = state.open_injuries?.length ?? 0;
  const commandRisks = withoutInjuryTrackingReminder(
    getDistinctTodayRiskWatch(supplementaryRisks, visibleTriggerLabels),
    openInjuryCount,
  );
  // With no programme there is no session to preview: the lead is today's
  // readiness, an injury hold or a safety notice, worded without a session.
  const decisionBanner = hasPlan
    ? resolvedDecision.currentGuidanceBanner ?? resolvedDecision.banner
    : getPlanFreeGuidanceBanner(state, resolvedDecision);
  // Once checked in, the form eases shut instead of dropping out from under
  // the sections below it.
  const readinessForm = (
    <Collapse show={showCheckin}>
      {showCheckin ? (
        <TodayReadinessForm
          plan={hasPlan ? activePlan : undefined}
          token={token ?? ""}
          warnings={state.today.warnings}
          onRefresh={refresh}
        />
      ) : null}
    </Collapse>
  );
  const sessionPanel = hasPlan ? (
    <TodaySessionPanel
      state={state}
      structuredPlan={structuredPlan}
      rehabLabelPolicy={rehabLabelPolicy}
      exerciseMedia={mergedExerciseMedia}
      planSchedule={planSchedule}
      painReasonAllowed={hasHealthDataConsent(me)}
      athleteSports={me?.profile.technical_style}
      token={token ?? ""}
      onRefresh={refresh}
      athleteFullName={me?.profile.full_name}
      commandUnavailable={Boolean(error)}
      presentationPending={presentationPending}
      onPlanSessionChanged={patchSession}
      professionalStatus={me?.profile.professional_status}
    />
  ) : <StandaloneRehab key={state.live_prescription?.session.session_id ?? "rehab"} state={state} token={token ?? ""} unavailable={Boolean(error)} refreshing={isRefreshing} onRefresh={refresh} />;

  return (
    <div className="today-page">
      {error ? (
        <section className="panel" role="alert">
          <p>Today couldn’t refresh. Your entries are safe. Refresh before starting or saving a session.</p>
          <button type="button" className="secondary-button" onClick={() => void refresh()}>Retry Today</button>
        </section>
      ) : null}
      <section className={`panel today-shell ${styles.command} ${styles.contentReveal}`}>
        <div className={styles.hero}>
          <div className="today-hero-copy">
            {showProgrammeArrival ? null : <h1>Today</h1>}
            {hasPlan ? (
              showProgrammeArrival ? (
                <div className={styles.programmeArrival}>
                  <p className={styles.arrivalTitle}><TileIcon name="check" ring />{openOngoing ? "Programme ready" : "Your camp is ready"}</p>
                  <h1 className={styles.arrivalHeadline}>{arrivalMetadata}</h1>
                  <div className={styles.arrivalLinks}>
                    {arrivalAction ? <a className={`cta ${styles.arrivalStart}`} href="#today-session" onClick={event => {
                      event.preventDefault();
                      arrivalAction.scrollIntoView({ block: "center", behavior: "auto" });
                      arrivalAction.focus({ preventScroll: true });
                    }}>Go to Session</a> : null}
                    <Link href={`/plans/${activePlan.id}`} onClick={dismissPlanHint}>View Full Plan <span aria-hidden="true">→</span></Link>
                  </div>
                </div>
              ) : <p className={styles.heroPhase}>{planTitle}{weekLabel}</p>
            ) : (
              <p className={styles.heroIntro}>Your training, your record. Log coach-led or solo sessions.</p>
            )}
          </div>
          {trainingDayLabel !== "Today" ? <p className={styles.heroDate}>{trainingDayLabel.replace(/\s\d{4}$/, "")}</p> : null}
        </div>
        <TodayReadinessStrip
          hasPlan={hasPlan}
          canCheckIn={canCheckIn}
          loggedToday={loggedToday}
          needsCheckin={showCheckin}
          openInjuryCount={state.open_injuries?.length ?? 0}
          completionStatus={state.today.completion_status}
          checkinHref={showCheckin ? "#today-checkin" : undefined}
          injuriesHref={token ? "#today-injury" : undefined}
          sessionHref={resolvedDecision.displayTier === "preview" ? undefined : "#today-session"}
        />
        {/* A refresh marks the guidance stale in place; swapping it for a
            one-line notice collapsed the page and pulled everything below up. */}
        {error ? null : <AutoHeight className={styles.decision}>
          <div className={styles.decisionBody} data-refreshing={isRefreshing ? "true" : undefined} aria-busy={isRefreshing || undefined}>
            {isRefreshing ? <p role="status" className={styles.refreshBadge}>Updating Today…</p> : null}
            <TodayDecisionPanel
              banner={decisionBanner && state.live_prescription?.session.blocks?.length && state.live_prescription.session.blocks.every(isCalmRehabBlock)
                ? { ...decisionBanner, action: decisionBanner.action?.replace(/rehab/gi, "guidance"), detail: decisionBanner.detail.replace(/rehab/gi, "guidance") }
                : decisionBanner}
              compactPreview={resolvedDecision.hasSession && !resolvedDecision.currentGuidanceBanner}
              tier={resolvedDecision.currentGuidanceBanner ? resolvedDecision.authoritativeTier : resolvedDecision.displayTier}
              triggers={state.today.recommendation_trigger_labels}
              safetyChecks={state.today.recommendation_safety_checks}
              context={state.today.recommendation_context_labels}
              sources={resolvedDecision.currentGuidanceBanner
                ? state.today.recommendation_sources?.filter((source) => source !== "today's planned session")
                : state.today.recommendation_sources}
              confidenceNote={state.today.recommendation_confidence_note}
            />
          </div>
        </AutoHeight>}
        <TodayRiskWatch
          risks={commandRisks}
          hasActiveInjury={openInjuryCount > 0}
        />
        {/* Asked beside the guidance it is about, once a check-in has given
            the athlete something to judge, rather than at the foot of the page. */}
        {hasPlan && !error && decisionBanner && resolvedDecision.recommendationState !== "not_checked_in" ? (
          <details className="today-feedback-disclosure">
            <summary>Does today&apos;s guidance fit how you feel?</summary>
            <ContextualFeedback
              key={`daily-feedback-${state.active_plan?.id ?? "none"}-${state.today.training_day}`}
              token={token ?? ""}
              surface="daily_recommendation"
              className="today-feedback-card"
            />
          </details>
        ) : null}
      </section>

      {showWeighIn ? <TodayWeighInCard token={token!} fightDate={String(activePlan.fight_date)} trainingDay={trainingDate!} /> : null}

      {/* The session keeps one slot, so a change of order never remounts it
          (and loses a running timer); only the check-in moves around it. */}
      {resolvedDecision.useSafeReplacement ? null : readinessForm}
      {sessionPanel}
      {resolvedDecision.useSafeReplacement ? readinessForm : null}
      {token && me?.profile.role === "athlete"
        ? <EnableRemindersRow key={`${me.profile.athlete_id}:${token}`} token={token} athleteId={me.profile.athlete_id} /> : null}

      <div id="today-training-log" className={styles.trainingLog} tabIndex={-1}>
        <TrainingHabitLog
          accessToken={token}
          state={state}
          variant={hasPlan ? "compact" : "primary"}
          onLoggedToday={setLoggedToday}
          onSaved={() => void refresh()}
        />
      </div>

      {/* Injury reports are health data: the server rejects them without
          consent, so a new report is never offered until it is granted. Open
          injuries stay visible regardless, with their restrictions. */}
      {token && (hasHealthDataConsent(me) || (state.open_injuries?.length ?? 0) > 0) ? (
        <TodayInjuryManager
          openInjuries={state.open_injuries ?? []}
          rehabTargets={Object.fromEntries(rehabBlocks.map(block => [block.injury_id!, `#${encodeURIComponent(rehabExerciseTarget(block)!)}`]))}
          effectiveClearance={state.effective_clinician_clearance}
          delayedPrompts={state.delayed_rehab_prompts}
          todaySafetyNoticeInjuryId={state.today.primary_safety_notice?.injury_id ?? null}
          token={token}
          onRefresh={refresh}
        />
      ) : token ? (
        <section id="today-injury" className={`panel ${styles.injuryConsent}`} aria-labelledby="today-injury-consent-heading">
          <h2 id="today-injury-consent-heading">Injuries</h2>
          <p className="muted">Reporting an injury needs health-data consent.</p>
          <Link href="/settings#privacy" className="secondary-button">Review consent</Link>
        </section>
      ) : null}

      <nav className={styles.quickActions} aria-label="Quick actions">
        <p className={styles.quickTitle}>Quick actions</p>
        <div className={hasPlan ? styles.quickRow : `${styles.quickRow} ${styles.quickRowFour}`}>
          {/* Without a plan the round timer stays: the check-in has its own tile. */}
          {showCheckin && hasPlan ? (
            <a href="#today-checkin" className={styles.quickAction}>
              <TileIcon name="clock" />
              Check in
              <span className={styles.chevron} aria-hidden="true" />
            </a>
          ) : (
            <Link href="/timer" className={styles.quickAction}>
              <TileIcon name="timer" />
              Round timer
              <span className={styles.chevron} aria-hidden="true" />
            </Link>
          )}
          {hasPlan ? (showProgrammeArrival ? null :
            <Link href={`/plans/${activePlan.id}`} className={styles.quickAction} onClick={dismissPlanHint}>
              <TileIcon name="plan" />
              View plan
              <span className={styles.chevron} aria-hidden="true" />
            </Link>
          ) : (me?.plan_count ?? 0) > 0 ? (
            <Link href="/plans" className={styles.quickAction}>
              <TileIcon name="plan" />
              Choose a plan
              <span className={styles.chevron} aria-hidden="true" />
            </Link>
          ) : (
            <Link href="/quick-build" className={styles.quickAction}>
              <TileIcon name="plan" />
              Generate my S&amp;C plan
              <span className={styles.chevron} aria-hidden="true" />
            </Link>
          )}
          {hasPlan ? null : (
            // Coach-led athletes put their coach's sessions into a plan of their own.
            <Link href="/plans/custom" className={styles.quickAction}>
              <TileIcon name="plan" />
              Add my plan
              <span className={styles.chevron} aria-hidden="true" />
            </Link>
          )}
          <Link href="/history" className={styles.quickAction}>
            <TileIcon name="history" />
            History
            <span className={styles.chevron} aria-hidden="true" />
          </Link>
        </div>
      </nav>

    </div>
  );
}
