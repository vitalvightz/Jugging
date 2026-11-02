"use client";

import Link from "next/link";
import { ActionLink } from "@/components/action-link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type CSSProperties, type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import { useAppSession } from "@/components/auth-provider";
import { useConnectivity, useOnReconnect } from "@/components/connection-status-banner";
import { getAuthenticatedLandingHref, needsStartingPath } from "@/lib/auth-routing";
import { EmptyState } from "@/components/empty-state";
import { FightCountdown, FightCountdownSkeleton } from "@/components/fight-countdown";
import { getFightCountdown } from "@/lib/fight-countdown";
import { InstallUnlxck } from "@/components/install-unlxck";
import { LandingFeaturePanel } from "@/components/landing-feature-panel";
import { LandingPreviewCard } from "@/components/landing-preview-card";
import { UnlockGlitchText } from "@/components/unlock-glitch-text";
import { PlansFeaturedSkeleton, Skeleton } from "@/components/skeleton";
import { XpProgressCard, XpProgressCardSkeleton } from "@/components/xp-progress-card";
import { TrainingSummaryCard } from "@/components/training-summary-card";
import { useXp } from "@/components/xp-provider";
import { getPlan, getToday, getTrainingHistory } from "@/lib/api";
import { describeLoadFailure } from "@/lib/connectivity";
import { SHARED_READ_MAX_AGE_MS } from "@/lib/shared-reads";
import { hasHealthDataConsent } from "@/lib/compliance";
import { getTrackingHome } from "@/lib/tracking-home";
import type { TrainingHistorySession } from "@/lib/training-history";
import {
  getOptionLabel,
  PROFESSIONAL_STATUS_OPTIONS,
  STANCE_OPTIONS,
  TACTICAL_STYLE_OPTIONS,
  TECHNICAL_STYLE_OPTIONS,
} from "@/lib/intake-options";
import { humanizeIfRawEnum } from "@/lib/plan-labels";
import { formatPlanFightDate, formatPlanTimestamp, getPlanDisplayName, isOpenOngoingPlan } from "@/lib/plan-format";
import {
  getCampDayLabel,
  getOverviewCommandEyebrow,
  getOverviewPrimaryAction,
  getRiskTimeframeLabel,
  getTierMeta,
  resolveTodayDecision,
} from "@/lib/today";
import type { PlanSummary, StructuredPlan, TodayActivePlan, TodayCommandView } from "@/lib/types";

function formatPlanCount(value: number): string {
  return `${value} saved plan${value === 1 ? "" : "s"}`;
}

function OverviewDetailList({
  items,
}: {
  items: Array<{
    label: string;
    value: string;
    highlight?: boolean;
    badgeText?: string;
    helperText?: string;
    progressValue?: number;
  }>;
}) {
  return (
    <div className="review-detail-list overview-detail-list">
      {items.map((item) => (
        <div
          key={`${item.label}-${item.value}`}
          className={item.highlight ? "review-detail-row overview-detail-row-highlight" : "review-detail-row"}
        >
          <div className={item.highlight ? "overview-detail-heading overview-detail-heading-highlight" : "overview-detail-heading"}>
            <p className="review-detail-label">{item.label}</p>
            {item.badgeText ? <span className="overview-inline-badge">{item.badgeText}</span> : null}
          </div>
          <p className={item.highlight ? "review-detail-value overview-detail-value-strong" : "review-detail-value"}>{item.value}</p>
          {typeof item.progressValue === "number" ? (
            <div className="overview-progress-track" role="presentation" aria-hidden="true">
              <span
                className="overview-progress-fill"
                style={{ width: `${Math.max(0, Math.min(100, item.progressValue))}%` }}
              />
            </div>
          ) : null}
          {item.helperText ? <p className="overview-progress-helper">{item.helperText}</p> : null}
        </div>
      ))}
    </div>
  );
}

function OverviewDetailGrid({
  items,
}: {
  items: Array<{
    label: string;
    value: string;
    highlight?: boolean;
    badgeText?: string;
    helperText?: string;
    progressValue?: number;
  }>;
}) {
  const midpoint = Math.ceil(items.length / 2);
  const columns = [items.slice(0, midpoint), items.slice(midpoint)].filter((column) => column.length);

  return (
    <div className="overview-detail-grid">
      {columns.map((column, index) => (
        <div key={`column-${index + 1}`} className="overview-detail-column">
          <OverviewDetailList items={column} />
        </div>
      ))}
    </div>
  );
}

function OverviewDisclosure({
  title,
  summary,
  badge,
  children,
}: {
  title: string;
  summary: string;
  badge?: string;
  children: ReactNode;
}) {
  return (
    <details className="overview-disclosure">
      <summary className="overview-disclosure-summary">
        <div className="overview-disclosure-copy">
          <p className="kicker">{title}</p>
          <p className="overview-disclosure-title">{summary}</p>
        </div>
        <div className="overview-disclosure-meta">
          {badge ? <span className="overview-inline-badge">{badge}</span> : null}
          <span className="overview-disclosure-chevron" aria-hidden="true" />
        </div>
      </summary>
      <div className="overview-disclosure-body">{children}</div>
    </details>
  );
}

function WorkspaceOverviewSkeleton({ showCountdown = false }: { showCountdown?: boolean }) {
  return (
    <>
      <section
        className="hero-panel overview-command-shell overview-command-primary athlete-motion-slot athlete-motion-header"
        aria-busy="true"
      >
        <div className="overview-primary-grid">
          <div className="status-card overview-command-card overview-decision-lead">
            <Skeleton variant="text" width={110} height={12} />
            <Skeleton variant="text" width="70%" height={40} />
            <Skeleton variant="text" width="88%" height={16} />
            <Skeleton variant="text" width="76%" height={16} />
            <div className="plan-summary-actions overview-primary-actions">
              <Skeleton variant="block" width={168} height={44} />
              <Skeleton variant="block" width={140} height={44} />
            </div>
          </div>
          <div className="overview-primary-session">
            {showCountdown ? <FightCountdownSkeleton /> : <XpProgressCardSkeleton />}
          </div>
        </div>
      </section>
      <section className="panel overview-secondary athlete-motion-slot athlete-motion-status" aria-busy="true">
        <div className="overview-operational-strip" aria-label="Workspace status loading">
          {[0, 1, 2, 3].map((index) => (
            <div key={index} className="overview-operational-item">
              <Skeleton variant="text" width={72} height={10} />
              <Skeleton variant="text" width={120} height={16} />
            </div>
          ))}
        </div>
        <PlansFeaturedSkeleton />
      </section>
    </>
  );
}

function enrichConfirmedActivePlan(
  commandPlan: TodayActivePlan | null | undefined,
  latestPlan: PlanSummary | null | undefined,
): TodayActivePlan {
  if (!commandPlan?.id) {
    return commandPlan ?? {};
  }

  const canUseLatestPlanFields = latestPlan?.plan_id === commandPlan.id;
  if (!canUseLatestPlanFields) {
    return commandPlan;
  }

  return {
    ...commandPlan,
    name: commandPlan.name || getPlanDisplayName(latestPlan),
    status: commandPlan.status || latestPlan.status,
    fight_date: commandPlan.fight_date || latestPlan.fight_date,
  };
}

/**
 * Condensed Overview risk index. Today owns the full explanation and action;
 * this card names only the two highest-priority signals and routes overflow to
 * that actionable surface.
 */
function OverviewRiskWatch({ risks = [] }: { risks?: TodayCommandView["risk_watch"] }) {
  if (!risks.length) {
    return (
      <article className="status-card overview-command-card overview-risk-card">
        <p className="status-label">Training alerts</p>
        <p className="muted">No active warnings.</p>
      </article>
    );
  }

  const safeRisks = risks ?? [];
  const visible = safeRisks.slice(0, 2);
  const overflow = safeRisks.length - visible.length;

  return (
    <article className="status-card overview-command-card overview-risk-card">
      <p className="status-label">Training alerts</p>
      <div className="overview-risk-list">
        {visible.map((risk, index) => {
          const timeframe = getRiskTimeframeLabel(risk.timeframe);
          const riskLabel = humanizeIfRawEnum(risk.label) || risk.label;
          return (
            <div key={`${risk.category}-${risk.label}-${index}`} className="overview-risk-row" data-tone={risk.tone}>
              <span className="overview-risk-row-label">{timeframe || riskLabel}</span>
              {timeframe ? <span className="overview-risk-row-text">{riskLabel}</span> : null}
            </div>
          );
        })}
      </div>
      {overflow > 0 ? (
        <Link href="/today" className="overview-risk-more">
          Review {overflow} more on Today
        </Link>
      ) : null}
    </article>
  );
}

// Staggers the landing entrance: each element fades up after its own delay.
const riseDelay = (ms: number) => ({ "--rise-delay": `${ms}ms` }) as CSSProperties;

// Line icons for the three landing benefits: build (calendar), adapt
// (sliders), track (bar chart). Decorative; the headings carry the meaning.
// Their parts carry classes so each draws in once when the list is seen.
const LANDING_BENEFIT_ICONS: ReactNode[] = [
  <svg key="build" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <rect className="icon-draw" pathLength={1} x="3.5" y="5" width="17" height="15" rx="2.5" />
    <path className="icon-draw" pathLength={1} d="M3.5 10h17M8 3v4M16 3v4" />
    <path className="icon-fade" d="M8 14h.01M12 14h.01M16 14h.01M8 17h.01M12 17h.01" strokeWidth="2.2" />
  </svg>,
  <svg key="adapt" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
    <path d="M4 6h16M4 12h16M4 18h16" opacity="0.45" />
    <path className="icon-knob icon-knob-1" d="M15 4v4" />
    <path className="icon-knob icon-knob-2" d="M9 10v4" />
    <path className="icon-knob icon-knob-3" d="M17 16v4" />
  </svg>,
  <svg key="track" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
    <path className="icon-bar icon-bar-1" d="M7 19v-5" />
    <path className="icon-bar icon-bar-2" d="M12 19V9" />
    <path className="icon-bar icon-bar-3" d="M17 19V5" />
  </svg>,
];

// Line icons for the in-your-corner tools: round timer (stopwatch), injury
// (bandage), weight (scale). Same draw-in parts as the benefit icons.
const LANDING_TOOL_ICONS: ReactNode[] = [
  <svg key="timer" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <circle className="icon-draw" pathLength={1} cx="12" cy="13.5" r="7.5" />
    <path className="icon-fade" d="M10 2.5h4M12 2.5V6M12 13.5l3-3M18.5 6.5 20 5" />
  </svg>,
  <svg key="injury" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <rect className="icon-draw" pathLength={1} x="2.5" y="8" width="19" height="8" rx="4" transform="rotate(-45 12 12)" />
    <path className="icon-fade" d="M10.5 10.5h.01M13.5 10.5h.01M10.5 13.5h.01M13.5 13.5h.01" strokeWidth="2.2" />
  </svg>,
  <svg key="weight" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <rect className="icon-draw" pathLength={1} x="3.5" y="3.5" width="17" height="17" rx="4" />
    <path className="icon-fade" d="M8 9.5a5 5 0 0 1 8 0M12 9.5l1.5-2" />
  </svg>,
];

export default function HomePage() {
  const publicT = useTranslations("PublicHome");
  const landingBenefits = [1, 2, 3].map((index) => ({
    title: publicT(`benefit${index}Title`),
    body: publicT(`benefit${index}Body`),
    icon: LANDING_BENEFIT_ICONS[index - 1],
  }));
  const landingTools = [1, 2, 3].map((index) => ({
    title: publicT(`tool${index}Title`),
    body: publicT(`tool${index}Body`),
    icon: LANDING_TOOL_ICONS[index - 1],
  }));
  const heroLine2 = publicT("heroLine2");
  // English "Lxcked in." glitches its X once, echoing the motto; other
  // locales' lines have no X and render plainly.
  const heroX = heroLine2.indexOf("x");
  const { isReady, isMeHydrated, hasTransientMeError, session, me, signOut, refreshMe } = useAppSession();
  const router = useRouter();
  const [commandState, setCommandState] = useState<TodayCommandView | null>(null);
  const [commandError, setCommandError] = useState<string | null>(null);
  const [calendar, setCalendar] = useState<{ token: string; id: string; plan: StructuredPlan | null } | null>(null);
  const calendarRequest = useRef<{ token: string; id: string; promise: ReturnType<typeof getPlan> } | null>(null);

  useEffect(() => {
    if (isReady && session && isMeHydrated && !me) {
      router.replace("/login");
    }
  }, [isReady, isMeHydrated, me, router, session]);

  useEffect(() => {
    if (isReady && session && isMeHydrated && !hasTransientMeError && needsStartingPath(me)) {
      router.replace(getAuthenticatedLandingHref(me));
    }
  }, [isReady, session, isMeHydrated, hasTransientMeError, me, router]);

  // Tracking athletes without a programme keep the same Home; its plan cards
  // are filled from what they have logged. Only a loaded feed can establish
  // "no active plan", and only then is their training history read.
  const tracksWithoutPlan = Boolean(
    me?.profile.starting_path === "tracking_first" && commandState && !commandState.active_plan?.id,
  );
  const { progress: xpProgress } = useXp();
  // History is tagged with the athlete it was read for, and only that athlete's
  // rows are ever shown: after an account switch nothing from the previous
  // account renders, even before the new read returns.
  const historyOwner = me?.profile.athlete_id ?? "";
  const [trainingHistory, setTrainingHistory] = useState<
    { owner: string; rows: TrainingHistorySession[]; cursor: string | null } | null
  >(null);
  const ownHistory = trainingHistory && trainingHistory.owner === historyOwner ? trainingHistory : null;
  const trainingRows = ownHistory?.rows ?? null;
  const trainingCursor = ownHistory?.cursor ?? null;
  const [trainingMore, setTrainingMore] = useState<{ owner: string; loading: boolean; error: string | null }>(
    { owner: "", loading: false, error: null },
  );
  const trainingMoreLoading = trainingMore.owner === historyOwner && trainingMore.loading;
  const trainingMoreError = trainingMore.owner === historyOwner ? trainingMore.error : null;
  useEffect(() => {
    const token = session?.access_token;
    if (!token || !historyOwner || !tracksWithoutPlan) return;
    let cancelled = false;
    getTrainingHistory(token, true)
      .then((page) => {
        if (cancelled) return;
        setTrainingHistory({ owner: historyOwner, rows: page.sessions, cursor: page.next_before_day ?? null });
      })
      .catch(() => { /* History stays unknown; Home claims nothing about it. */ });
    return () => { cancelled = true; };
  }, [session?.access_token, historyOwner, tracksWithoutPlan]);
  const loadMoreTraining = useCallback(async () => {
    const token = session?.access_token;
    const owner = historyOwner;
    const before = trainingCursor;
    if (!token || !owner || !before) return;
    setTrainingMore({ owner, loading: true, error: null });
    try {
      const page = await getTrainingHistory(token, true, before);
      // A page read for another account (or an older cursor) is dropped.
      setTrainingHistory((current) => {
        if (!current || current.owner !== owner || current.cursor !== before) return current;
        const seen = new Set(current.rows.map((row) => `${row.source}-${row.id}`));
        const older = page.sessions.filter((row) => !seen.has(`${row.source}-${row.id}`));
        return { owner, rows: [...current.rows, ...older], cursor: page.next_before_day ?? null };
      });
      setTrainingMore((current) => (current.owner === owner ? { owner, loading: false, error: null } : current));
    } catch {
      setTrainingMore((current) => (current.owner === owner
        ? { owner, loading: false, error: "Couldn’t load older sessions." } : current));
    }
  }, [session?.access_token, historyOwner, trainingCursor]);

  const [isReloadingCommand, setIsReloadingCommand] = useState(false);
  const { offline: isOffline } = useConnectivity();
  const latestTokenRef = useRef(session?.access_token);

  useEffect(() => {
    latestTokenRef.current = session?.access_token;
  }, [session?.access_token]);

  const loadCommandState = useCallback(async () => {
    const token = session?.access_token;
    if (!token) {
      setCommandState(null);
      return;
    }
    setIsReloadingCommand(true);
    setCommandError(null);
    try {
      const state = await getToday(token);
      // Ignore results from a request the current session has moved past.
      if (latestTokenRef.current !== token) {
        return;
      }
      setCommandState(state);
      setCommandError(null);
    } catch (error) {
      if (latestTokenRef.current !== token) {
        return;
      }
      setCommandError(describeLoadFailure(error, "We couldn't load your camp status. Please try again."));
    } finally {
      if (latestTokenRef.current === token) {
        setIsReloadingCommand(false);
      }
    }
  }, [session?.access_token]);

  // Sitting on a load error when the connection comes back: refetch quietly.
  useOnReconnect(() => void loadCommandState(), Boolean(commandError));

  useEffect(() => {
    let active = true;
    if (!session?.access_token) {
      setCommandState(null);
      return () => {
        active = false;
      };
    }
    void getToday(session.access_token)
      .then((state) => {
        if (!active) return;
        setCommandState(state);
        setCommandError(null);
      })
      .catch((error) => {
        if (!active) return;
        setCommandError(describeLoadFailure(error, "We couldn't load your camp status. Please try again."));
      });
    return () => {
      active = false;
    };
  }, [session?.access_token]);

  // Warm the known plan while Today loads. Only the confirmed active plan can
  // supply the countdown's calendar; a different latest plan is never shown.
  const activePlanId = commandState?.active_plan?.id;
  const calendarPlanId = activePlanId || me?.latest_plan?.plan_id;
  useEffect(() => {
    const token = session?.access_token;
    if (!token || !calendarPlanId) {
      setCalendar(null);
      return;
    }
    let cancelled = false;
    let request = calendarRequest.current;
    if (!request || request.token !== token || request.id !== calendarPlanId) {
      request = { token, id: calendarPlanId, promise: getPlan(token, calendarPlanId, { reuseForMs: SHARED_READ_MAX_AGE_MS }) };
      calendarRequest.current = request;
    }
    request.promise
      .then((detail) => {
        if (!cancelled && calendarPlanId === activePlanId) {
          setCalendar({ token, id: activePlanId, plan: detail.outputs?.structured_plan ?? null });
        }
      })
      .catch(() => {
        if (calendarRequest.current === request) calendarRequest.current = null;
        if (!cancelled && calendarPlanId === activePlanId) {
          setCalendar({ token, id: activePlanId, plan: null });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [session?.access_token, activePlanId, calendarPlanId]);
  const structuredPlan = calendar?.token === session?.access_token && calendar?.id === activePlanId ? calendar?.plan : null;

  if (session && hasTransientMeError) {
    return (
      <section className="panel loading-card">
        <p className="kicker">{isOffline ? "Connection offline" : "Home"}</p>
        <h1>{isOffline ? "You’re offline." : "Workspace temporarily unavailable"}</h1>
        <p className="muted">
          {isOffline
            ? "Your camp is safe. We’ll reload your workspace the moment you reconnect."
            : "We couldn’t load your athlete profile. Please try again."}
        </p>
        <div className="hero-actions">
          <button type="button" className="cta" onClick={() => void refreshMe()}>
            Retry
          </button>
          {/* Signing out offline strands the athlete: they could not sign back in. */}
          {isOffline ? null : (
            <button type="button" className="secondary-button" onClick={() => void signOut()}>
              Sign out
            </button>
          )}
        </div>
      </section>
    );
  }

  if (!isReady) {
    return (
      <section className="panel loading-card">
        <p className="kicker">Unlxcking…</p>
        <h1>Loading your athlete workspace</h1>
        <p className="muted">Checking saved intake and plan history.</p>
      </section>
    );
  }

  if (session && !isMeHydrated) {
    return <WorkspaceOverviewSkeleton />;
  }

  if (session && isMeHydrated && !me) {
    return (
      <section className="panel loading-card">
        <p className="kicker">Home</p>
        <h1>Redirecting to login</h1>
        <p className="muted">Session expired. Sign in again.</p>
      </section>
    );
  }

  if (session && me) {
    if (needsStartingPath(me)) return <p role="status">Opening your starting options…</p>;
    if (me.profile.starting_path === "tracking_first") {
      if (!commandState && !commandError) return <WorkspaceOverviewSkeleton />;
      if (!commandState && commandError) return <section className="panel"><h1>Today is temporarily unavailable</h1><p role="alert">Couldn’t load your training workspace.</p><button type="button" className="secondary-button" onClick={() => void loadCommandState()}>Retry Today</button></section>;
    }
    const latestPlan = me.latest_plan ?? null;
    const draft = (me.profile.onboarding_draft as { current_step?: number } | null) ?? null;
    const latestIntake = me.latest_intake;
    const hasMeaningfulDraft = Boolean(
      draft && typeof draft === "object" && Object.keys(draft as Record<string, unknown>).length > 0,
    );
    // A tracking athlete has chosen not to build a camp: no plan welcome.
    const isFirstTimeUser = me.profile.starting_path !== "tracking_first"
      && me.plan_count === 0 && !latestPlan && !latestIntake && !hasMeaningfulDraft;

    if (isFirstTimeUser) {
      return (
        <section className="hero-panel welcome-panel athlete-motion-slot athlete-motion-header">
          <div className="hero-panel-copy welcome-copy">
            <p className="eyebrow">Welcome to UNLXCK</p>
            <h1 className="hero-title">Build your fight camp in minutes.</h1>
            <p className="overview-command-summary">
              Set goals, get your plan, start training. About two minutes.
            </p>
            <div className="hero-actions welcome-actions">
              <Link href="/quick-build" className="cta">
                Build my plan
              </Link>
              <Link href="/demo-plan" className="ghost-button">
                View Demo Plan
              </Link>
            </div>
            <p className="muted welcome-context">
              Want more control? <Link href="/onboarding" className="auth-text-link">Use Camp Setup</Link>.
            </p>
            <InstallUnlxck variant="inline" />
          </div>
        </section>
      );
    }

    if (!commandState && !commandError) {
      return <WorkspaceOverviewSkeleton showCountdown={Boolean(latestPlan?.fight_date)} />;
    }

    if (!commandState && commandError) {
      return (
        <section className="panel loading-card">
          <p className="kicker">Home</p>
          <h1>Camp command view unavailable</h1>
          <p className="muted">{commandError}</p>
          <div className="hero-actions">
            {/* In-place refetch: a full reload while offline lands on the offline shell. */}
            <button
              type="button"
              className="cta"
              onClick={() => void loadCommandState()}
              disabled={isReloadingCommand}
            >
              {isReloadingCommand ? "Unlxcking…" : "Retry"}
            </button>
            <Link href="/plans" className="secondary-button">
              View plans
            </Link>
          </div>
        </section>
      );
    }

    const activePlan = enrichConfirmedActivePlan(commandState?.active_plan, latestPlan);
    const hasActivePlan = Boolean(activePlan.id);
    // "No active plan" splits into two states the whole primary area must agree
    // on: saved plans exist (pick one) vs no plans at all (build the first).
    const hasSavedPlans = (me.plan_count ?? 0) > 0;
    const resolvedDecision = commandState ? resolveTodayDecision(commandState) : null;
    const risks = commandState?.risk_watch ?? [];
    const recommendation = commandState?.today?.recommendation_state ?? "not_checked_in";
    // Overview consumes the same authoritative resolver as Today. The backend
    // tier controls safety; structured severe-injury data only supplies truthful
    // STOP presentation and the injury check-in action.
    const decisionBanner = resolvedDecision?.banner ?? null;
    const decisionTier = resolvedDecision?.displayTier ?? "not_checked_in";
    const tierMeta = getTierMeta(decisionTier);
    const safetyNoticeLeads = resolvedDecision?.primaryMessageKind === "safety_notice";
    const decisionTitle = safetyNoticeLeads
      ? decisionBanner?.title ?? "Current safety"
      : tierMeta.label;
    const overviewEyebrow = safetyNoticeLeads
      ? "Current safety"
      : getOverviewCommandEyebrow(decisionTier);
    const decisionLines = decisionBanner
      ? (safetyNoticeLeads
          ? [decisionBanner.action, decisionBanner.detail]
          : [decisionBanner.detail, decisionBanner.action]
        ).filter((line): line is string => Boolean(line))
      : ["Submit today's fast check-in to unlock your training decision."];
    const decisionSafety = decisionBanner?.safety;
    // With no active plan there is no training decision to render, so the whole
    // primary area (headline + body) is overridden to match the CTA instead of
    // showing a stale "CHECK IN REQUIRED" that the athlete cannot act on.
    const overviewTitle = !hasActivePlan
      ? hasSavedPlans
        ? "Select active plan"
        : "Build your plan"
      : decisionTitle;
    const overviewLines = !hasActivePlan
      ? [
          hasSavedPlans
            ? "Choose which saved plan should control Today, check-ins and session tracking."
            : "Create your first plan to unlock Today and session tracking.",
        ]
      : decisionLines;
    // Safety copy only belongs to a real decision — never on the no-plan states.
    const overviewSafety = hasActivePlan ? decisionSafety : undefined;
    // Today's countdown to the fight, and whether the scheduled session is today
    // (vs a future planned day that must read as pending, not cleared).
    const campDay = getCampDayLabel(commandState?.today?.training_day, String(activePlan.fight_date || ""));
    const openOngoing = hasActivePlan && isOpenOngoingPlan(activePlan.fight_date);
    const sessionIsToday = resolvedDecision?.sessionIsToday ?? false;
    // Decision tone drives the colour accents on the decision card (matches
    // Today). Neutral/preview carries no accent — the next-session preview stays
    // grey and is never tinted red just because today is a pull-back. The
    // exception is a severe injury: it blocks the scheduled session, so its card
    // IS the blocked one and correctly reads red.
    const decisionTone =
      decisionBanner && decisionBanner.tone !== "neutral" ? decisionBanner.tone : undefined;
    // One dominant next action, resolved from the whole state by a pure helper so
    // the button can never contradict the headline/body (and is unit-tested per
    // state). STOP never falls through to a "train" label — see
    // getOverviewPrimaryAction.
    const primaryAction = getOverviewPrimaryAction({
      hasActivePlan,
      planCount: me.plan_count ?? 0,
      hasInjuryOverride: Boolean(resolvedDecision?.severeInjuryBlocksCurrentSession),
      recommendation,
      decisionTier,
      hasSafeSession: Boolean(resolvedDecision?.useSafeReplacement),
      sessionIsToday,
    });
    const primaryHref = primaryAction.href;
    const primaryLabel = primaryAction.label;
    const trainingDay = commandState?.today?.training_day ?? "";
    const tracking = tracksWithoutPlan && commandState
      ? getTrackingHome({
        state: commandState, rows: trainingRows, loadedFrom: trainingCursor, today: trainingDay,
        canCheckIn: hasHealthDataConsent(me), habits: me.profile.training_habits,
      })
      : null;
    const countdownProps = {
      fightDate: activePlan.fight_date ? String(activePlan.fight_date) : null,
      trainingDay: commandState?.today?.training_day,
      phase: activePlan.phase ? String(activePlan.phase) : null,
      plan: structuredPlan,
      eventName: activePlan.event_name ?? null,
      // Target fight weight from the plan's intake, never current bodyweight.
      // Under-18s have no weight-cut feature, so it never shows for them.
      targetWeightKg: me.profile.is_minor ? null : (activePlan.target_weight_kg ?? null),
      // The calendar read for this plan hasn't settled yet (success or failure).
      calendarPending: Boolean(activePlanId) && !(calendar?.token === session?.access_token && calendar?.id === activePlanId),
    };
    const showCountdown = !tracking && getFightCountdown(countdownProps) !== null;
    const trainingStreak = xpProgress.streaks.adherence.current;
    const trackingSlot = tracking ? tracking.slot ?? (trainingStreak > 0 ? "habits" : null) : null;
    const hero = tracking
      ? { ...tracking.hero, primary: tracking.hero.primary }
      : { eyebrow: overviewEyebrow, title: overviewTitle, lines: overviewLines, safety: overviewSafety, tone: decisionTone,
        primary: { href: primaryHref, label: primaryLabel } };

    return (
      <>
        {/* Primary command area — today's decision, the session it affects, and
            one dominant action lead the first viewport. */}
        <section className="hero-panel overview-command-shell overview-command-primary athlete-motion-slot athlete-motion-header" data-tone={hero.tone}>
          <div className="overview-primary-grid">
            <div className="overview-decision-lead">
              <p className="eyebrow">{hero.eyebrow}</p>
              <h1 className="hero-title overview-decision-headline">{hero.title}</h1>
              <div className="overview-decision-copy">
                {hero.lines.map((line, index) => (
                  <p key={index} className="muted">{line}</p>
                ))}
                {hero.safety ? <p className="muted overview-decision-safety">{hero.safety}</p> : null}
              </div>
              <div className="action-stack overview-primary-actions">
                <Link href={hero.primary.href} className="cta overview-primary-action">{hero.primary.label}</Link>
                {hasActivePlan ? (
                  <ActionLink href={`/plans/${activePlan.id}`}>{openOngoing ? "Training plan" : "Fight camp"}</ActionLink>
                ) : tracking ? (
                  <>
                    <ActionLink href="/quick-build">Generate my S&amp;C plan</ActionLink>
                    <ActionLink href="/plans/custom">Add my plan</ActionLink>
                  </>
                ) : (
                  <ActionLink href="/quick-build">Quick Build</ActionLink>
                )}
              </div>
            </div>
            <div className="overview-primary-session">
              {showCountdown ? <FightCountdown key={activePlanId} {...countdownProps} /> : <XpProgressCard />}
            </div>
          </div>
          {commandError ? (
            <div className="error-banner" role="alert">
              <span>{commandError}</span>
              <button
                type="button"
                className="error-banner-retry"
                onClick={() => void loadCommandState()}
                disabled={isReloadingCommand}
              >
                {isReloadingCommand ? "Unlxcking…" : "Retry"}
              </button>
            </div>
          ) : null}
        </section>

        {/* Secondary — camp context, progress, full risk watch, disclaimer.
            Available but visually reduced so it never competes with the command. */}
        <section className="panel overview-secondary athlete-motion-slot athlete-motion-status">
          {tracking ? (
            <>
              <p className="kicker overview-secondary-eyebrow">Training</p>
              <div className="overview-operational-strip" aria-label="Training status">
                {tracking.strip.map((item) => (
                  <div key={item.label} className="overview-operational-item"><span className="overview-operational-label">{item.label}</span><span className="overview-operational-value">{item.value}</span></div>
                ))}
              </div>
              {trackingSlot === "summary" && trainingRows ? (
                <TrainingSummaryCard rows={trainingRows} today={trainingDay} loadedFrom={trainingCursor}
                  loading={trainingMoreLoading} error={trainingMoreError} onLoadMore={loadMoreTraining} />
              ) : trackingSlot === "readiness" && tracking.readiness ? (
                <section className="fight-countdown" aria-label="Daily readiness">
                  <div className="fight-countdown-head"><div>
                    <p className="fight-countdown-kicker">Daily readiness</p>
                    <p className="fight-countdown-days">{tracking.readiness.title}</p>
                    <p className="fight-countdown-date">{tracking.readiness.detail}</p>
                  </div></div>
                  <p className="fight-countdown-next"><Link href="/today" className="fight-countdown-next-label">Open Today</Link></p>
                </section>
              ) : trackingSlot === "habits" ? (
                <section className="fight-countdown" aria-label="Your routine">
                  <div className="fight-countdown-head"><div>
                    <p className="fight-countdown-kicker">Your routine</p>
                    <p className="fight-countdown-days">
                      <span className="fight-countdown-number">{trainingStreak}</span> day training streak
                    </p>
                  </div></div>
                  {tracking.routines.map((routine) => (
                    <p key={routine} className="fight-countdown-next">{routine}</p>
                  ))}
                </section>
              ) : null}
            </>
          ) : (
            <>
          <p className="kicker overview-secondary-eyebrow">{openOngoing ? "Block progress" : "Camp progress"}</p>
              <div className="overview-operational-strip" aria-label={openOngoing ? "Training status" : "Camp status"}>
                <div className="overview-operational-item"><span className="overview-operational-label">Plan</span><span className="overview-operational-value">{String(activePlan.name || (hasActivePlan ? "Active plan" : "No active plan"))}</span></div>
                <div className="overview-operational-item"><span className="overview-operational-label">{openOngoing ? "Cycle" : "Camp day"}</span><span className="overview-operational-value">{openOngoing ? "Renewable 4-week block" : campDay || "Not set"}</span></div>
                <div className="overview-operational-item"><span className="overview-operational-label">{openOngoing ? "Mode" : "Phase"}</span><span className="overview-operational-value">{openOngoing ? "Ongoing" : humanizeIfRawEnum(activePlan.phase) || "Not set"}</span></div>
                <div className="overview-operational-item"><span className="overview-operational-label">Fight date</span><span className="overview-operational-value">{openOngoing ? "Not scheduled" : formatPlanFightDate(String(activePlan.fight_date || ""))}</span></div>
              </div>
              {showCountdown ? <XpProgressCard /> : null}
            </>
          )}
          <Link href="/nutrition" className="overview-risk-more overview-weigh-in">Weight tracker: log a weigh-in</Link>
          <OverviewRiskWatch risks={risks} />
        </section>
      </>
    );
  }

  const getStartedLink = (
    <Link href="/signup" className="cta public-hero-cta">
      <span>{publicT("getStarted")}</span>
      <svg className="public-cta-arrow" viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
        <path d="M4 10h11m-4.5-4.5L15 10l-4.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </Link>
  );

  return (
    <div className="public-landing">
      <section className="public-hero" aria-labelledby="public-hero-heading">
        <p className="public-hero-motto public-rise" style={riseDelay(0)}>
          <span className="sr-only">UNLXCK {publicT("mottoTagline")}</span>
          <UnlockGlitchText text={`UNLXCK ${publicT("mottoTagline")}`} accentLength={6} delayMs={150} durationMs={1100} />
        </p>
        <h1
          id="public-hero-heading"
          className="hero-title public-hero-title public-rise"
          style={riseDelay(70)}
          aria-label={publicT("heroTitle")}
        >
          <span>{publicT("heroLine1")}</span>
          <span>
            {heroX < 0 ? (
              heroLine2
            ) : (
              <>
                {heroLine2.slice(0, heroX)}
                <span className="public-hero-x" data-char={heroLine2[heroX]}>
                  {heroLine2[heroX]}
                </span>
                {heroLine2.slice(heroX + 1)}
              </>
            )}
          </span>
        </h1>
        <p className="public-hero-lead public-rise" style={riseDelay(140)}>
          {publicT("lead")}
        </p>
        <div className="public-hero-actions public-rise" style={riseDelay(210)}>
          {getStartedLink}
        </div>
      </section>

      <section className="public-preview" aria-labelledby="public-preview-heading">
        <div className="public-section-head public-rise" style={riseDelay(260)}>
          <p className="kicker">{publicT("previewKicker")}</p>
          <h2 id="public-preview-heading">{publicT("previewTitle")}</h2>
        </div>
        <LandingPreviewCard className="public-rise" style={riseDelay(330)} />
      </section>

      <LandingFeaturePanel
        id="public-flex"
        kicker={publicT("flexKicker")}
        title={publicT("flexTitle")}
        items={landingBenefits}
      />

      <LandingFeaturePanel
        id="public-tools"
        kicker={publicT("toolsKicker")}
        title={publicT("toolsTitle")}
        items={landingTools}
      />

      <section className="public-final-cta public-reveal" aria-labelledby="public-final-cta-heading">
        <h2 id="public-final-cta-heading">{publicT("finalTitle")}</h2>
        {getStartedLink}
      </section>
    </div>
  );
}
