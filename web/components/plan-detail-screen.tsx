"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";

import { RequireAuth } from "@/components/auth-guard";
import { useAppSession } from "@/components/auth-provider";
import { useOnReconnect } from "@/components/connection-status-banner";
import { TrainingLibraryLink } from "@/components/training-library-link";
import actionStyles from "@/components/training-action-tile.module.css";
import plansStyles from "@/components/plans/plans.module.css";
import { PlanViewer } from "@/components/plan-viewer";
import { LogTraining } from "@/components/log-training";
import { ApiError, getPlan, trackProductEvent } from "@/lib/api";
import { isNetworkFailure } from "@/lib/connectivity";
import { recallView, SHARED_READ_MAX_AGE_MS } from "@/lib/shared-reads";
import type { PlanDetail } from "@/lib/types";

// Right after generation completes the app redirects straight to
// `/plans/{planId}`, but the saved plan row can briefly lag behind the
// completion event (read-after-write replication). That window surfaces as a
// transient 404 — the plan is genuinely there a moment later (it shows up in
// plan history). `getPlan`'s transient retries deliberately ignore 404s, so we
// re-attempt the initial load here before surfacing the alarming "could not
// restore" card.
const PLAN_LOAD_MAX_ATTEMPTS = 5;
const PLAN_LOAD_RETRY_DELAY_MS = 1500;

/**
 * Whether a failed plan load should be retried instead of surfaced. Only the
 * read-after-write 404 window is retried; genuine errors (403, malformed
 * responses, exhausted gateway/network retries) are shown immediately.
 */
export function shouldRetryPlanLoad(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

type PlanDetailStateCardProps = {
  phase: "finalizing" | "failed";
  eyebrow: string;
  title: string;
  copy: string;
  railTitle: string;
  railCopy: string;
  statusMessage?: string;
  error?: string | null;
};

function PlanDetailStateCard({
  phase,
  eyebrow,
  title,
  copy,
  railTitle,
  railCopy,
  statusMessage,
  error = null,
}: PlanDetailStateCardProps) {
  return (
    <section className={`panel loading-shell loading-phase-${phase}`}>
      <div className="split-layout">
        <div className="step-main athlete-motion-slot athlete-motion-main">
          <article className="status-card loading-primary-panel loading-context-panel">
            <p className="loading-eyebrow">{eyebrow}</p>
            <h1 className="loading-title">{title}</h1>
            <p className="muted loading-copy">{copy}</p>

            {phase !== "failed" ? (
              <div className="loading-scan-rail" aria-hidden="true">
                <span className="loading-scan-line" />
              </div>
            ) : null}

            {error ? (
              <div className="error-banner">{error}</div>
            ) : (
              <div className="loading-status-strip">{statusMessage}</div>
            )}
          </article>
        </div>

        <aside className="step-aside athlete-motion-slot athlete-motion-rail">
          <div className="support-panel loading-secondary-panel">
            <div className="form-section-header">
              <p className="loading-eyebrow">Workspace state</p>
              <h2 className="form-section-title">{railTitle}</h2>
            </div>
            <p className="muted">{railCopy}</p>
          </div>
        </aside>
      </div>
    </section>
  );
}

export function PlanDetailScreen({ planId }: { planId: string }) {
  const { me, session, refreshMe } = useAppSession();
  const searchParams = useSearchParams();

  // A plan opened earlier this session shows at once while it is read again.
  // The plan document is presentation; completions, activation and Today's
  // decisions are read fresh by the viewer itself. Writes drop it.
  const [plan, setPlan] = useState<PlanDetail | null>(() => session?.access_token
    ? recallView<PlanDetail>(session.access_token, `/api/plans/${encodeURIComponent(planId)}`)
    : null);
  const [error, setError] = useState<string | null>(null);
  const [isConnectionError, setIsConnectionError] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  // A plan that failed to load because the connection dropped reloads itself
  // on reconnect instead of stranding the athlete on an error card.
  useOnReconnect(() => setReloadKey((key) => key + 1), isConnectionError);

  useEffect(() => {
    if (!session?.access_token) {
      return;
    }

    setError(null);
    setIsConnectionError(false);

    let cancelled = false;
    const token = session.access_token;

    const loadPlan = async () => {
      for (let attempt = 1; attempt <= PLAN_LOAD_MAX_ATTEMPTS; attempt += 1) {
        try {
          // Failed reads are never held, so a retry always reaches the server.
          const loaded = await getPlan(token, planId, { reuseForMs: SHARED_READ_MAX_AGE_MS });
          if (!cancelled) {
            setPlan(loaded);
            void trackProductEvent(token, "plan_opened", "plans", undefined, loaded.plan_id);
          }
          return;
        } catch (planError) {
          if (cancelled) {
            return;
          }
          if (attempt === PLAN_LOAD_MAX_ATTEMPTS || !shouldRetryPlanLoad(planError)) {
            setIsConnectionError(isNetworkFailure(planError));
            setError(planError instanceof Error ? planError.message : "Unable to load plan.");
            return;
          }
          await new Promise((resolve) => setTimeout(resolve, PLAN_LOAD_RETRY_DELAY_MS));
        }
      }
    };

    void loadPlan();

    return () => {
      cancelled = true;
    };
  }, [planId, reloadKey, session?.access_token]);

  const recovered = searchParams.get("recovered") === "1";
  const protectedTriage = searchParams.get("protected_triage") === "1";
  const stage2Status = (searchParams.get("stage2_status") || "").trim().toLowerCase();
  const showResumeFailureHint = stage2Status === "triage_resume_approved";
  const resolvedPlanId = plan?.plan_id || planId;
  const isAdminViewer = me?.profile.role === "admin";

  return (
    <RequireAuth>
      {recovered ? (
        <section className="panel loading-card loading-shell loading-phase-finalizing athlete-motion-slot athlete-motion-status">
          <article className="status-card loading-context-panel loading-context-panel-compact">
            <p className="loading-eyebrow">Plan synced</p>
            <div className="loading-status-strip">
              Plan restored after a timeout and synced.
            </div>
          </article>
        </section>
      ) : null}

      {protectedTriage ? (
        <section className="panel loading-card loading-shell loading-phase-finalizing athlete-motion-slot athlete-motion-status">
          <article className="status-card loading-context-panel loading-context-panel-compact">
            <p className="loading-eyebrow">Protected triage plan restored</p>
            <div className="loading-status-strip">
              {isAdminViewer
                ? "Use Admin Review → Resume Generation."
                : "This plan is protected and still requires review before release."}
              {isAdminViewer && showResumeFailureHint
                ? " Previous resume failed or did not complete. Submit a new resume request."
                : ""}
            </div>

            {isAdminViewer && resolvedPlanId ? (
              <a className="button button-secondary" href={`#admin-review-${resolvedPlanId}`}>
                Open Admin Review
              </a>
            ) : null}
          </article>
        </section>
      ) : null}

      {error ? (
        <PlanDetailStateCard
          phase="failed"
          eyebrow="Plan detail"
          title={isConnectionError ? "Connection dropped." : "We couldn't load this plan."}
          copy={
            isConnectionError
              ? "Reloads automatically when you're back online."
              : "Check the error below, then retry from your plans."
          }
          railTitle="Your plan is safe"
          railCopy="Nothing deleted. Reopen it from your plans."
          error={error}
        />
      ) : plan ? (
        <PlanViewer
          plan={plan}
          accessToken={session?.access_token ?? null}
          viewerRole={me?.profile.role ?? "athlete"}
          viewerProfileId={me?.profile.athlete_id ?? null}
          onPlanUpdated={setPlan}
          onPlanDeleted={refreshMe}
        />
      ) : (
        <PlanDetailStateCard
          phase="finalizing"
          eyebrow="Plan detail"
          title="Unlxcking your plan…"
          copy="Rebuilding the saved output and athlete view."
          railTitle="Current action"
          railCopy="Loading the latest saved version."
          statusMessage="Restoring the latest saved plan output now."
        />
      )}

      <section className={plansStyles.tools} aria-label="Training tools">
        <h2 className={plansStyles.sectionTitle}>Training tools</h2>
        <div className={`${actionStyles.actions} ${plansStyles.shortcuts}`}>
          {(!plan || plan.athlete_id === me?.profile.athlete_id) ? <LogTraining accessToken={session?.access_token ?? null} variant="tile" /> : null}
          <TrainingLibraryLink variant="tile" />
        </div>
      </section>
    </RequireAuth>
  );
}
