"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";

import { cancelGenerationJob, retryGenerationJob } from "@/lib/api";
import { formatGenerationElapsedLabel, formatJobElapsedLabel } from "@/lib/generation-elapsed";
import { humanizeGenerationError } from "@/lib/generation-failure";
import { useAppSession } from "./auth-provider";
import { useGenerationStatus } from "./generation-status-provider";

const CELEBRATION_DURATION_MS = 1_600;

/**
 * The frozen total a finished build took. Every terminal ribbon renders it —
 * completed, review-required, recovered plan, and failure alike. Dropping it
 * on the terminal branches was what made the ribbon look like it had lost
 * track of time: the number climbed while the build ran and then vanished the
 * instant it mattered, so nothing on screen ever said how long it had taken.
 */
function TotalBuildTime({ label }: { label: string | null }) {
  if (!label) {
    return null;
  }

  return (
    <span className="global-generation-status-elapsed" aria-label={`Total build time ${label}`}>
      {label}
    </span>
  );
}
const RIBBON_DISMISSED_KEY = "unlxck:generation-ribbon-dismissed";
const latestJobDismissKey = (jobId: string) => `${RIBBON_DISMISSED_KEY}:${jobId}`;
// Hiding the ribbon while a plan builds is a separate choice from hiding its
// outcome: closing "Building..." must not also swallow "Your plan is ready".
export const buildingJobDismissKey = (jobId: string) => `${latestJobDismissKey(jobId)}:building`;

const IN_PROGRESS_PHASES = new Set(["submitting", "queued", "running", "reconnecting", "finalizing"]);

export function isRibbonBuildInProgress(
  isActive: boolean,
  phase: string | null,
  latestJob: { status?: string | null } | null,
): boolean {
  return isActive
    ? IN_PROGRESS_PHASES.has(phase ?? "")
    : latestJob?.status === "queued" || latestJob?.status === "running";
}

export function resolveRibbonDismissKey({
  isActive,
  phase,
  jobId,
  latestJob,
}: {
  isActive: boolean;
  phase: string | null;
  jobId: string | null;
  latestJob: { job_id?: string | null; status?: string | null } | null;
}): string {
  const isBuildInProgress = isRibbonBuildInProgress(isActive, phase, latestJob);
  const ribbonJobId = !isActive && latestJob?.job_id ? latestJob.job_id : jobId;
  if (!ribbonJobId) {
    return RIBBON_DISMISSED_KEY;
  }
  return isBuildInProgress ? buildingJobDismissKey(ribbonJobId) : latestJobDismissKey(ribbonJobId);
}

type PassiveLatestJobStatus = "failed" | "review_required" | "completed" | "queued" | "running";

export function isProtectedTriageLatestJob(
  latestJob:
    | { stage2_status?: string | null; requires_admin_resume?: boolean | null }
    | null
    | undefined,
): boolean {
  if (!latestJob) {
    return false;
  }
  if (latestJob.requires_admin_resume === true) {
    return true;
  }
  const stage2 = String(latestJob.stage2_status || "").trim().toLowerCase();
  return stage2 === "triage_blocked";
}

/**
 * Ribbon copy for a build held by protected triage.
 *
 * Only an admin can act on the hold, so only an admin is offered the admin
 * review link. An athlete used to be shown "Open admin review", which both
 * read as their job to do and led to a page they cannot open; they now get a
 * plain waiting notice instead.
 */
export function resolveAdminHoldRibbonCopy(isAdminViewer: boolean, hasAdminTarget = true): {
  message: string;
  ctaLabel: string;
  canOpenAdminReview: boolean;
} {
  if (!isAdminViewer) {
    return {
      message: "Your plan is with an admin for review.",
      ctaLabel: "Awaiting approval",
      canOpenAdminReview: false,
    };
  }

  return {
    message: "Plan is held for admin review.",
    ctaLabel: hasAdminTarget ? "Open admin review" : "Awaiting admin",
    canOpenAdminReview: hasAdminTarget,
  };
}

/**
 * Copy for a build an admin has approved and resumed. The athlete's ribbon
 * must say the build is being finished — the generic "Generating plan..."
 * alongside a link into /generate is what made an approved plan look like it
 * had gone back to the start.
 */
export const ADMIN_RESUMED_STATUS_MESSAGE = "Approved - finishing your plan.";

export function resolveAthleteResumeStatusMessage(
  phase: string | null,
  source: string | null,
  fallback: string | null,
): string | null {
  if (source === "admin_triage_resume" && (phase === "queued" || phase === "running" || phase === "finalizing")) {
    return ADMIN_RESUMED_STATUS_MESSAGE;
  }
  return fallback;
}

export function shouldRenderPassiveLatestJobRibbon(
  latestJob:
    | {
        status?: PassiveLatestJobStatus | null;
        plan_id?: string | null;
        latest_plan_id?: string | null;
        stage2_status?: string | null;
        requires_admin_resume?: boolean | null;
      }
    | null
    | undefined,
): boolean {
  if (!latestJob) {
    return false;
  }

  if (latestJob.status === "failed") {
    return true;
  }

  if (latestJob.status === "review_required" && Boolean(latestJob.plan_id)) {
    return true;
  }

  // Triage outcomes have no plan_id and the backend reports them as
  // review_required. Surface them via the protected-triage ribbon copy.
  if (latestJob.status === "review_required" && isProtectedTriageLatestJob(latestJob)) {
    return true;
  }

  if (latestJob.status === "completed" && !latestJob.plan_id) {
    return true;
  }

  if (latestJob.status === "completed" && isProtectedTriageLatestJob(latestJob)) {
    return true;
  }

  return false;
}

export function latestFailedJobHasOpenablePlan(
  latestJob:
    | { status?: string | null; plan_id?: string | null; latest_plan_id?: string | null }
    | null
    | undefined,
): boolean {
  if (!latestJob || latestJob.status !== "failed") {
    return false;
  }

  return Boolean(latestJob.plan_id || latestJob.latest_plan_id);
}

export function latestCompletedJobOpenablePlanId(
  latestJob:
    | { status?: string | null; plan_id?: string | null; latest_plan_id?: string | null }
    | null
    | undefined,
): string | null {
  if (!latestJob || latestJob.status !== "completed" || latestJob.plan_id) {
    return null;
  }

  return latestJob.latest_plan_id || null;
}

export function getPassiveLatestJobPlanTarget(
  latestJob:
    | { status?: string | null; plan_id?: string | null; latest_plan_id?: string | null }
    | null
    | undefined,
): `/plans/${string}` | null {
  if (!latestJob) {
    return null;
  }

  if (latestJob.status === "failed") {
    const planId = latestJob.plan_id || latestJob.latest_plan_id;
    return planId ? `/plans/${planId}` : null;
  }

  if (latestJob.status === "review_required" && latestJob.plan_id) {
    return `/plans/${latestJob.plan_id}`;
  }

  const completedPlanId = latestCompletedJobOpenablePlanId(latestJob);
  return completedPlanId ? `/plans/${completedPlanId}` : null;
}

export function getGenerationStatusTarget(
  phase: string | null,
  planId: string | null,
  terminalStatus: "completed" | "review_required" | null,
  source: string | null,
  athleteId: string | null,
  isAdminViewer = true,
): `/generate` | `/admin/athletes/${string}` | `/plans/${string}` | `/plans/${string}?review_required=1` | null {
  if (phase === "queued" || phase === "running" || phase === "finalizing") {
    // The loading screen recovers the active job, including admin-approved builds.
    return "/generate";
  }

  if (phase === "completed" && planId) {
    if (terminalStatus === "review_required") {
      return `/plans/${planId}?review_required=1`;
    }

    return `/plans/${planId}`;
  }

  // A failed build has no live workspace to open: /generate would mount the
  // build screen ("Saving your intake...") and then bounce back, which reads as
  // if generation restarted. The failed ribbon renders its own explicit
  // actions instead of a navigation target.
  return null;
}

export function isGenerationRibbonTargetRedundant(pathname: string | null, target: string | null): boolean {
  if (!pathname || !target) {
    return false;
  }

  const targetPath = target.split("?", 1)[0];
  return pathname === targetPath || (pathname === "/plans" && targetPath.startsWith("/plans/"));
}

// While a build is running, the build screen already shows it in full; a
// ribbon pointing back at it only covers the bottom of that screen (on phones,
// where the tips sit). Outcomes such as an admin-review hold still show there.
export function isActiveBuildScreen(pathname: string | null, isBuildInProgress: boolean): boolean {
  return isBuildInProgress && pathname === "/generate";
}

export function isGenerationRibbonAcknowledgedRoute(
  pathname: string | null,
  target: string | null,
): boolean {
  if (!pathname || !target) {
    return false;
  }

  return pathname === target.split("?", 1)[0];
}

export function GlobalGenerationStatus() {
  const { session, me } = useAppSession();
  const isAdminViewer = me?.profile.role === "admin";
  const pathname = usePathname();
  const {
    isActive,
    isStalled,
    statusMessage,
    phase,
    jobId,
    planId,
    terminalStatus,
    startedAtMs,
    endedAtMs,
    refreshStatus,
    latestJob,
    source,
    athleteId,
  } = useGenerationStatus();

  const [now, setNow] = useState(() => Date.now());
  const [isCelebrating, setIsCelebrating] = useState(false);
  const [isDismissed, setIsDismissed] = useState(false);
  const [resolvedDismissKey, setResolvedDismissKey] = useState<string | null>(null);
  const [isRetryingLatest, setIsRetryingLatest] = useState(false);
  const [retryLatestError, setRetryLatestError] = useState<string | null>(null);
  const [reopenPos, setReopenPos] = useState<{ x: number; y: number } | null>(null);
  const [isCancelling, setIsCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);

  const previousPhaseRef = useRef(phase);
  const previousGenerationKeyRef = useRef<string | null>(null);
  const reopenRef = useRef<HTMLButtonElement>(null);
  const reopenDragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
    moved: boolean;
  } | null>(null);
  const reopenJustDraggedRef = useRef(false);

  // Keep the draggable "Show plan build" pill on-screen if the viewport resizes.
  useEffect(() => {
    if (!reopenPos) {
      return;
    }

    const clamp = () => {
      const el = reopenRef.current;
      const width = el?.offsetWidth ?? 0;
      const height = el?.offsetHeight ?? 0;
      const maxX = Math.max(8, window.innerWidth - width - 8);
      const maxY = Math.max(8, window.innerHeight - height - 8);

      setReopenPos((prev) =>
        prev
          ? {
              x: Math.min(Math.max(8, prev.x), maxX),
              y: Math.min(Math.max(8, prev.y), maxY),
            }
          : prev,
      );
    };

    window.addEventListener("resize", clamp);
    return () => window.removeEventListener("resize", clamp);
  }, [reopenPos]);

  const handleReopenPointerDown = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const el = reopenRef.current;
    if (!el) {
      return;
    }

    const rect = el.getBoundingClientRect();
    reopenDragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: rect.left,
      originY: rect.top,
      moved: false,
    };

    try {
      el.setPointerCapture(event.pointerId);
    } catch {
      // Pointer capture unsupported; dragging still works via window coords.
    }
  };

  const handleReopenPointerMove = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = reopenDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) {
      return;
    }

    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;

    // Ignore micro-movements so a tap still registers as a click.
    if (!drag.moved && Math.hypot(dx, dy) < 6) {
      return;
    }

    drag.moved = true;

    const el = reopenRef.current;
    const width = el?.offsetWidth ?? 0;
    const height = el?.offsetHeight ?? 0;
    const maxX = Math.max(8, window.innerWidth - width - 8);
    const maxY = Math.max(8, window.innerHeight - height - 8);

    setReopenPos({
      x: Math.min(Math.max(8, drag.originX + dx), maxX),
      y: Math.min(Math.max(8, drag.originY + dy), maxY),
    });
  };

  const handleReopenPointerUp = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = reopenDragRef.current;
    if (!drag) {
      return;
    }

    reopenJustDraggedRef.current = drag.moved;
    reopenDragRef.current = null;

    try {
      reopenRef.current?.releasePointerCapture(event.pointerId);
    } catch {
      // Ignore release failures.
    }
  };

  const handleReopenClick = () => {
    // Suppress the click that ends a drag so the ribbon doesn't reopen on drop.
    if (reopenJustDraggedRef.current) {
      reopenJustDraggedRef.current = false;
      return;
    }

    reopenCurrentBanner();
  };

  const isFailed = phase === "failed";
  const isCompleted = phase === "completed";
  const navigationTarget = getGenerationStatusTarget(
    phase,
    planId,
    terminalStatus,
    source,
    athleteId,
    isAdminViewer,
  );
  const passivePlanTarget = !isActive ? getPassiveLatestJobPlanTarget(latestJob) : null;
  const acknowledgementTarget = isCompleted ? navigationTarget : passivePlanTarget;
  const isRedundantRoute =
    isActiveBuildScreen(pathname, isRibbonBuildInProgress(isActive, phase, latestJob)) ||
    isGenerationRibbonTargetRedundant(pathname, acknowledgementTarget);
  const isAcknowledgementRoute = isGenerationRibbonAcknowledgedRoute(pathname, acknowledgementTarget);
  const ctaLabel = isCompleted && planId ? "View" : navigationTarget ? "Open" : "Refresh";
  // The elapsed reading survives into the terminal states instead of vanishing
  // — a finished build should say how long it took. It stops moving because
  // `endedAtMs` replaces `now` in the subtraction, not because it is hidden.
  const showElapsed = isActive && startedAtMs !== null;
  const displayStatusMessage = resolveAthleteResumeStatusMessage(phase, source, statusMessage);
  const isElapsedRunning = showElapsed && endedAtMs === null;

  // The passive ribbon has no live timer to read, so its total comes from the
  // job row's own timestamps — the same window, recovered from the backend
  // values, so the number does not change across the handoff.
  const passiveTotalLabel = !isActive ? formatJobElapsedLabel(latestJob) : null;

  const dismissKey = resolveRibbonDismissKey({ isActive, phase, jobId, latestJob });

  useEffect(() => {
    if (!isElapsedRunning) {
      return;
    }

    setNow(Date.now());

    const interval = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [isElapsedRunning]);

  // Install guidance is offered after the first saved session (see
  // offerPushNotifications), not when a plan finishes, so it never stands
  // between a new plan and its first session.

  useEffect(() => {
    if (previousPhaseRef.current !== "completed" && phase === "completed") {
      setIsCelebrating(true);

      const reduceMotion =
        typeof window !== "undefined" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;

      if (!reduceMotion && typeof navigator !== "undefined" && typeof navigator.vibrate === "function") {
        try {
          navigator.vibrate(20);
        } catch {
          // Vibration unavailable.
        }
      }

      const timer = window.setTimeout(() => setIsCelebrating(false), CELEBRATION_DURATION_MS);
      previousPhaseRef.current = phase;

      return () => window.clearTimeout(timer);
    }

    previousPhaseRef.current = phase;
  }, [phase]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      try {
        setIsDismissed(window.localStorage.getItem(dismissKey) === "1");
      } catch {
        setIsDismissed(false);
      }
      setResolvedDismissKey(dismissKey);
    }, 0);

    return () => window.clearTimeout(timer);
  }, [dismissKey]);

  useEffect(() => {
    if (!isAcknowledgementRoute) {
      return;
    }

    try {
      window.localStorage.setItem(dismissKey, "1");
    } catch {}

    const timer = window.setTimeout(() => {
      setIsDismissed(true);
      setResolvedDismissKey(dismissKey);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [dismissKey, isAcknowledgementRoute]);

  useEffect(() => {
    if (!isActive) {
      previousGenerationKeyRef.current = null;
      return;
    }

    const generationKey = jobId && startedAtMs ? `${jobId}:${startedAtMs}` : null;

    if (!generationKey) {
      return;
    }

    if (previousGenerationKeyRef.current && previousGenerationKeyRef.current !== generationKey) {
      setIsDismissed(false);

      try {
        window.localStorage.removeItem(RIBBON_DISMISSED_KEY);
        if (jobId) {
          window.localStorage.removeItem(latestJobDismissKey(jobId));
          window.localStorage.removeItem(buildingJobDismissKey(jobId));
        }
      } catch {}
    }

    previousGenerationKeyRef.current = generationKey;
  }, [isActive, jobId, startedAtMs]);

  if (!isActive && !latestJob) {
    return null;
  }

  const dismissCurrentBanner = () => {
    setIsDismissed(true);
    setResolvedDismissKey(dismissKey);

    try {
      window.localStorage.setItem(dismissKey, "1");
    } catch {}
  };

  const reopenCurrentBanner = () => {
    setIsDismissed(false);
    setResolvedDismissKey(dismissKey);

    try {
      window.localStorage.removeItem(dismissKey);
    } catch {}
  };

  if (resolvedDismissKey !== dismissKey || isRedundantRoute) {
    return null;
  }

  const reopenPill = (label: string) => (
    <button
      ref={reopenRef}
      type="button"
      className="global-generation-status-reopen"
      aria-label="Show generation ribbon"
      style={
        reopenPos
          ? { left: reopenPos.x, top: reopenPos.y, right: "auto", bottom: "auto" }
          : undefined
      }
      onPointerDown={handleReopenPointerDown}
      onPointerMove={handleReopenPointerMove}
      onPointerUp={handleReopenPointerUp}
      onPointerCancel={handleReopenPointerUp}
      onClick={handleReopenClick}
    >
      {label}
    </button>
  );

  if (!isActive && latestJob) {
    if (!shouldRenderPassiveLatestJobRibbon(latestJob)) {
      return null;
    }

    if (isDismissed) {
      // A hold or a failure still needs the athlete's attention, so closing it
      // leaves the small reopen pill rather than erasing every trace of the
      // build. A finished plan the athlete has seen needs nothing more.
      if (latestJob.status === "failed") {
        return reopenPill("Show build error");
      }
      return isProtectedTriageLatestJob(latestJob) ? reopenPill("Show plan build") : null;
    }

    if (latestJob.status === "failed") {
      const failedPlanId = latestJob.plan_id || latestJob.latest_plan_id || null;

      if (latestFailedJobHasOpenablePlan(latestJob) && failedPlanId) {
        const target = getPassiveLatestJobPlanTarget(latestJob) || `/plans/${failedPlanId}`;
        if (isGenerationRibbonTargetRedundant(pathname, target)) {
          return null;
        }

        const isProtectedTriage = isProtectedTriageLatestJob(latestJob);
        return (
          <div className="global-generation-status global-generation-status-completed">
            <Link href={target} className="global-generation-status-main" onClick={dismissCurrentBanner}>
              <span className="global-generation-status-text">
                <span className="global-generation-status-message">
                  {isProtectedTriage
                    ? resolveAdminHoldRibbonCopy(isAdminViewer).message
                    : "Your plan is saved and ready."}
                </span>
                <TotalBuildTime label={passiveTotalLabel} />
              </span>
              <span className="global-generation-status-cta-label">
                {isProtectedTriage
                  ? resolveAdminHoldRibbonCopy(isAdminViewer).ctaLabel
                  : "Open plan"}
              </span>
            </Link>

            <button
              type="button"
              className="global-generation-status-dismiss"
              aria-label="Hide generation ribbon"
              onClick={dismissCurrentBanner}
            >
              ×
            </button>
          </div>
        );
      }

      return (
        <div className="global-generation-status global-generation-status-failed">
          <div className="global-generation-status-main">
            <div className="global-generation-status-content">
              <span className="global-generation-status-text">
                <span className="global-generation-status-message">
                  {humanizeGenerationError(latestJob.error)}
                </span>

                <TotalBuildTime label={passiveTotalLabel} />

                {latestJob.completed_at ? (
                  <span className="global-generation-status-elapsed" suppressHydrationWarning>
                    Completed {new Date(latestJob.completed_at).toLocaleString()}
                  </span>
                ) : null}
              </span>

              {latestJob.can_retry ? (
                <button
                  type="button"
                  className="global-generation-status-cta-label"
                  disabled={isRetryingLatest}
                  onClick={() => {
                    if (!session?.access_token || isRetryingLatest) {
                      return;
                    }

                    setRetryLatestError(null);
                    setIsRetryingLatest(true);

                    void retryGenerationJob(session.access_token, latestJob.job_id)
                      .then(() => refreshStatus())
                      .catch(() => setRetryLatestError("Retry failed. Open Generate and try again."))
                      .finally(() => setIsRetryingLatest(false));
                  }}
                >
                  {isRetryingLatest ? "Retrying..." : "Retry"}
                </button>
              ) : (
                <Link href="/plans" className="global-generation-status-cta-label">
                  Open plan history
                </Link>
              )}
            </div>

            {retryLatestError ? (
              <div className="global-generation-status-message">{retryLatestError}</div>
            ) : null}
          </div>

          <button
            type="button"
            className="global-generation-status-dismiss"
            aria-label="Hide generation ribbon"
            onClick={dismissCurrentBanner}
          >
            ×
          </button>
        </div>
      );
    }

    if (latestJob.status === "review_required" && latestJob.plan_id) {
      const target = getPassiveLatestJobPlanTarget(latestJob) || `/plans/${latestJob.plan_id}`;
      if (isGenerationRibbonTargetRedundant(pathname, target)) {
        return null;
      }

      return (
        <div className="global-generation-status global-generation-status-completed">
          <Link href={target} className="global-generation-status-main" onClick={dismissCurrentBanner}>
            <span className="global-generation-status-text">
              <span className="global-generation-status-message">Review saved plan</span>
              <TotalBuildTime label={passiveTotalLabel} />
            </span>
            <span className="global-generation-status-cta-label">Open plan</span>
          </Link>

          <button
            type="button"
            className="global-generation-status-dismiss"
            aria-label="Hide generation ribbon"
            onClick={dismissCurrentBanner}
          >
            ×
          </button>
        </div>
      );
    }

    // Protected triage outcomes that live only on the job (no plan_id) —
    // backend reports these as review_required + requires_admin_resume.
    // The user cannot open a plan; the ribbon links to the admin profile
    // for admins, or stays as a static notice otherwise.
    if (
      latestJob.status === "review_required"
      && !latestJob.plan_id
      && isProtectedTriageLatestJob(latestJob)
    ) {
      const holdTarget = latestJob.athlete_id ? `/admin/athletes/${latestJob.athlete_id}` : null;
      const holdCopy = resolveAdminHoldRibbonCopy(isAdminViewer, Boolean(holdTarget));
      const adminTarget = holdCopy.canOpenAdminReview ? holdTarget : null;
      const content = (
        <>
          <span className="global-generation-status-text">
            <span className="global-generation-status-message">{holdCopy.message}</span>
            <TotalBuildTime label={passiveTotalLabel} />
          </span>
          <span className="global-generation-status-cta-label">{holdCopy.ctaLabel}</span>
        </>
      );
      return (
        <div className="global-generation-status global-generation-status-completed">
          {adminTarget ? (
            <Link href={adminTarget} className="global-generation-status-main">
              {content}
            </Link>
          ) : (
            <div className="global-generation-status-main">{content}</div>
          )}

          <button
            type="button"
            className="global-generation-status-dismiss"
            aria-label="Hide generation ribbon"
            onClick={dismissCurrentBanner}
          >
            ×
          </button>
        </div>
      );
    }

    if (latestJob.status === "completed" && !latestJob.plan_id) {
      const completedPlanId = latestCompletedJobOpenablePlanId(latestJob);

      if (completedPlanId) {
        const target = `/plans/${completedPlanId}`;
        if (isGenerationRibbonTargetRedundant(pathname, target)) {
          return null;
        }

        const isProtectedTriage = isProtectedTriageLatestJob(latestJob);
        return (
          <div className="global-generation-status global-generation-status-completed">
            <Link href={target} className="global-generation-status-main" onClick={dismissCurrentBanner}>
              <span className="global-generation-status-text">
                <span className="global-generation-status-message">
                  {isProtectedTriage
                    ? resolveAdminHoldRibbonCopy(isAdminViewer).message
                    : "Your plan is saved and ready."}
                </span>
                <TotalBuildTime label={passiveTotalLabel} />
              </span>
              <span className="global-generation-status-cta-label">
                {isProtectedTriage
                  ? resolveAdminHoldRibbonCopy(isAdminViewer).ctaLabel
                  : "Open plan"}
              </span>
            </Link>

            <button
              type="button"
              className="global-generation-status-dismiss"
              aria-label="Hide generation ribbon"
              onClick={dismissCurrentBanner}
            >
              ×
            </button>
          </div>
        );
      }

      return (
        <div className="global-generation-status global-generation-status-failed">
          <div className="global-generation-status-main">
            <div className="global-generation-status-content">
              <span className="global-generation-status-text">
                <span className="global-generation-status-message">
                  Plan finished but couldn’t open. Support can recover it.
                </span>
                <TotalBuildTime label={passiveTotalLabel} />
              </span>

              <Link href="/plans" className="global-generation-status-cta-label">
                Open plan history
              </Link>
            </div>
          </div>

          <button
            type="button"
            className="global-generation-status-dismiss"
            aria-label="Hide generation ribbon"
            onClick={dismissCurrentBanner}
          >
            ×
          </button>
        </div>
      );
    }
  }

  const canNavigateToPlan = Boolean(navigationTarget);
  const elapsedLabel = showElapsed
    ? formatGenerationElapsedLabel({ startedAtMs, endedAtMs, nowMs: now })
    : null;
  const className = [
    "global-generation-status",
    isFailed ? "global-generation-status-failed" : "",
    isCompleted ? "global-generation-status-completed" : "",
    isCelebrating ? "global-generation-status-celebrating" : "",
  ]
    .filter(Boolean)
    .join(" ");

  if (!statusMessage && !phase && !navigationTarget) {
    return null;
  }

  if (isFailed && !isDismissed) {
    // The /generate screen already owns the full failure story; a second
    // ribbon on top of it is noise.
    if (pathname === "/generate") {
      return null;
    }

    return (
      <div className="global-generation-status global-generation-status-failed">
        <div className="global-generation-status-main">
          <div className="global-generation-status-content">
            <span className="global-generation-status-text">
              <span className="global-generation-status-message">{statusMessage}</span>
              <TotalBuildTime label={elapsedLabel} />
            </span>

            {/*
              A stalled build is still queued/running server-side, so cancelling
              it is the action that changes something — the cancel endpoint
              rejects an already-terminal job. A genuinely failed job gets a
              retry instead. Neither state offers a "tap to refresh": re-reading
              a dead job cannot revive it, and swapping the ribbon back to a
              live-looking status is exactly what made a failure look like a
              build in progress.
            */}
            {jobId && isStalled ? (
              <button
                type="button"
                className="global-generation-status-cta-label"
                disabled={isCancelling}
                onClick={() => {
                  if (!session?.access_token || isCancelling) {
                    return;
                  }

                  setCancelError(null);
                  setIsCancelling(true);

                  void cancelGenerationJob(session.access_token, jobId)
                    .then(() => refreshStatus())
                    .catch(() => setCancelError("Cancel failed. Try again."))
                    .finally(() => setIsCancelling(false));
                }}
              >
                {isCancelling ? "Stopping..." : "Stop build"}
              </button>
            ) : null}

            {jobId && !isStalled ? (
              <button
                type="button"
                className="global-generation-status-cta-label"
                disabled={isRetryingLatest}
                onClick={() => {
                  if (!session?.access_token || isRetryingLatest) {
                    return;
                  }

                  setRetryLatestError(null);
                  setIsRetryingLatest(true);

                  void retryGenerationJob(session.access_token, jobId)
                    .then(() => refreshStatus())
                    .catch(() => setRetryLatestError("Retry failed. Open Generate and try again."))
                    .finally(() => setIsRetryingLatest(false));
                }}
              >
                {isRetryingLatest ? "Retrying..." : "Retry"}
              </button>
            ) : null}
          </div>

          {cancelError ? <div className="global-generation-status-message">{cancelError}</div> : null}
          {retryLatestError ? (
            <div className="global-generation-status-message">{retryLatestError}</div>
          ) : null}
        </div>

        <button
          type="button"
          className="global-generation-status-dismiss"
          aria-label="Hide generation ribbon"
          onClick={dismissCurrentBanner}
        >
          ×
        </button>
      </div>
    );
  }

  if (isDismissed) {
    return reopenPill(isFailed ? "Show build error" : "Show plan build");
  }

  const content = (
    <>
      <div className="global-generation-status-content">
        <span className="global-generation-status-indicator" aria-hidden="true">
          {isCompleted ? (
            <span className="global-generation-status-check">&#10003;</span>
          ) : (
            <span className="global-generation-status-pulse" />
          )}
        </span>

        <span className="global-generation-status-text">
          <span className="global-generation-status-message">{displayStatusMessage}</span>

          {elapsedLabel ? (
            <span
              className="global-generation-status-elapsed"
              aria-label={`${isElapsedRunning ? "Elapsed time" : "Total build time"} ${elapsedLabel}`}
            >
              {elapsedLabel}
            </span>
          ) : null}
        </span>

        {isCompleted ? (
          <span className="global-generation-status-cta">
            <span className="global-generation-status-cta-label">{ctaLabel}</span>
            <span className="global-generation-status-arrow" aria-hidden="true">
              →
            </span>
          </span>
        ) : null}
      </div>

      {!isFailed && !isCompleted ? (
        <div className="global-generation-status-rail" aria-hidden="true">
          <span className="global-generation-status-line" />
        </div>
      ) : null}

      {isCelebrating ? (
        <span className="global-generation-status-celebrate-glow" aria-hidden="true" />
      ) : null}
    </>
  );

  return (
    <div className={className}>
      {canNavigateToPlan && navigationTarget ? (
        <Link
          href={navigationTarget}
          className="global-generation-status-main"
          onClick={isCompleted ? dismissCurrentBanner : undefined}
          aria-label={
            isCompleted
              ? "Plan ready. Tap to view."
              : "Generating. Tap for status."
          }
        >
          {content}
        </Link>
      ) : (
        <button
          type="button"
          className="global-generation-status-main"
          aria-label={
            isCompleted
              ? "Plan completed. Tap to refresh status."
              : "Generation in progress. Tap to refresh status."
          }
          onClick={() => {
            refreshStatus();
          }}
        >
          {content}
        </button>
      )}

      <button
        type="button"
        className="global-generation-status-dismiss"
        aria-label="Hide generation ribbon"
        onClick={dismissCurrentBanner}
      >
        ×
      </button>
    </div>
  );
}
