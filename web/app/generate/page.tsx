"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { offerProgrammeArrival } from "@/lib/programme-arrival";
import { RequireAuth } from "@/components/auth-guard";
import { useAppSession } from "@/components/auth-provider";
import { createGenerationJob, getActiveGenerationJob, getActivePlan } from "@/lib/api";
import { COMPLETED_GENERATION_KEY, parseCompletedGeneration } from "@/lib/completed-generation";
import { useGenerationController } from "@/lib/generation-controller";
import { clearGenerationIntent, hasGenerationIntent } from "@/lib/generation-intent";
import {
  buildGenerationPayloadDebugSummary,
  buildPlanRequestPayloadHash,
  clearPendingGenerationPayload,
  readPendingGenerationPayload,
} from "@/lib/generation-pending-payload";
import {
  resolveGenerateAutoStartDecision,
  resolveMatchingPayloadGenerationAction,
  resolvePostGenerationTarget,
} from "@/lib/generation-status-guards";
import { hydratePlanRequest } from "@/lib/onboarding";
import { validatePerformanceFocusSelections } from "@/lib/performance-focus-cap";
import { stableStringify } from "@/lib/stable-stringify";
import { clearSharedReads } from "@/lib/shared-reads";
import { PremiumLoadingScreen } from "@/components/premium-loading-screen";
import { PublicGenerationScreen } from "@/components/public-generation-screen";
import { useFadeNavigate } from "@/components/fade-through";

const STORAGE_KEY = "unlxck:pending-generation:self";
const ALLOWED_PLAN_SOURCES = new Set(["quick_build", "self_serve"]);

function resolvePlanSource(me: ReturnType<typeof useAppSession>["me"]): string {
  const draft = me?.profile.onboarding_draft as { plan_source?: unknown } | null | undefined;
  const candidate = typeof draft?.plan_source === "string" ? draft.plan_source.trim() : "";
  return ALLOWED_PLAN_SOURCES.has(candidate) ? candidate : "self_serve";
}

function getCompletedGeneration() {
  if (typeof window === "undefined") {
    return null;
  }
  return parseCompletedGeneration(window.localStorage.getItem(COMPLETED_GENERATION_KEY));
}

export default function GeneratePage() {
  const router = useRouter();
  const fadeNavigate = useFadeNavigate();
  const { me, session } = useAppSession();
  const autoStartRef = useRef(false);
  const [pendingGeneration] = useState(() => readPendingGenerationPayload());
  // Memoize so a re-render with the same session doesn't rebuild a fresh
  // payload object on every pass (which would churn the start effect below).
  const payload = useMemo(
    () => pendingGeneration?.payload ?? (me ? hydratePlanRequest(me) : null),
    [me, pendingGeneration],
  );
  // Canonical hash so re-ordered-but-identical payloads are treated as equal.
  const payloadHash = useMemo(() => stableStringify(payload), [payload]);
  const performanceFocusValidation = useMemo(
    () =>
      payload
        ? validatePerformanceFocusSelections(
            payload.fight_date,
            {
              keyGoals: payload.key_goals,
              weakAreas: payload.weak_areas,
            },
            {
              timeZone: payload.athlete.athlete_timezone,
            },
          )
        : null,
    [payload],
  );

  const controller = useGenerationController({
    token: session?.access_token ?? null,
    storageKey: STORAGE_KEY,
    createJob: async (clientRequestId) => {
      if (!session?.access_token || !payload) {
        throw new Error("Couldn't start generation. Reload and try again.");
      }
      const planSource = pendingGeneration?.planSource ?? resolvePlanSource(me);
      if (process.env.NODE_ENV !== "production") {
        console.info("[generation] create_job:payload_summary", {
          client_request_id: clientRequestId,
          payload_hash: pendingGeneration?.payloadHash ?? buildPlanRequestPayloadHash(payload),
          plan_source: planSource,
          ...buildGenerationPayloadDebugSummary(payload),
        });
      }
      const job = await createGenerationJob(session.access_token, payload, clientRequestId, planSource);
      if (pendingGeneration) {
        clearPendingGenerationPayload();
      }
      return job;
    },
    recoverActiveJob: async () => {
      if (!session?.access_token) {
        return null;
      }
      return getActiveGenerationJob(session.access_token);
    },
    onComplete: ({ planId, status, recovered, requiresAdminResume, stage2Status }) => {
      const isProtectedTriageOutcome =
        requiresAdminResume === true || (stage2Status || "").toLowerCase() === "triage_blocked";
      if (payload && typeof window !== "undefined" && !isProtectedTriageOutcome && planId) {
        window.localStorage.setItem(
          COMPLETED_GENERATION_KEY,
          JSON.stringify({ payloadHash, planId, completedAt: new Date().toISOString() }),
        );
      }
      // Protected triage outcomes have no plan row. Stay on the generate
      // screen — the controller's `review_paused` phase renders the paused
      // card with admin-review copy and a stopped elapsed timer.
      if (!planId) {
        return;
      }
      // Install guidance waits for the first saved session (offerPushNotifications
      // offers it first), so it never sits between the new plan and training.
      const token = session?.access_token ?? null;
      void (async () => {
        // A first plan is activated as it is published. Read past any held
        // result: only a fresh answer may send the athlete to Today.
        let activePlanId: string | null = null;
        if (token && !isProtectedTriageOutcome) {
          try {
            activePlanId = (await getActivePlan(token))?.plan_id ?? null;
          } catch {
            // Unknown active plan: the plan page is the safe destination.
          }
        }
        // Today must not show a view read before this plan existed.
        clearSharedReads();
        const target = resolvePostGenerationTarget({
          planId, activePlanId, status, recovered, requiresAdminResume, stage2Status,
        });
        if (target.startsWith("/today?")) offerProgrammeArrival(planId);
        router.replace(target);
      })();
    },
  });

  useEffect(() => {
    if (!session?.access_token || !payload || autoStartRef.current || controller.hasPendingGeneration) {
      return;
    }
    const matchingPayloadAction = resolveMatchingPayloadGenerationAction(
      payloadHash,
      getCompletedGeneration(),
    );
    if (matchingPayloadAction.type === "redirect") {
      // A matching plan already exists — open it. Any stale intent is moot.
      clearGenerationIntent();
      if (pendingGeneration) {
        clearPendingGenerationPayload();
      }
      fadeNavigate(() => router.replace(`/plans/${matchingPayloadAction.planId}`));
      return;
    }

    if ((!payload.fight_date && !payload.no_scheduled_fight) || !payload.athlete.technical_style.length) {
      clearGenerationIntent();
      if (pendingGeneration) {
        clearPendingGenerationPayload();
      }
      router.replace("/onboarding");
      return;
    }
    if (performanceFocusValidation?.isOverCap) {
      clearGenerationIntent();
      if (pendingGeneration) {
        clearPendingGenerationPayload();
      }
      router.replace("/onboarding?issue=focus-cap&step=performance");
      return;
    }

    autoStartRef.current = true;
    let cancelled = false;
    const activeToken = session.access_token;

    void (async () => {
      let activeJob: Awaited<ReturnType<typeof getActiveGenerationJob>> = null;
      try {
        activeJob = await getActiveGenerationJob(activeToken);
        if (cancelled) return;
      } catch {
        if (cancelled) return;
        // Active lookup unavailable; fall through to the intent-gated decision.
      }

      const decision = resolveGenerateAutoStartDecision({
        hasActiveJob: Boolean(activeJob),
        hasIntent: hasGenerationIntent() || Boolean(pendingGeneration),
      });

      if (decision === "recover" && activeJob) {
        clearGenerationIntent();
        if (pendingGeneration) {
          clearPendingGenerationPayload();
        }
        await controller.startGeneration({
          clientRequestId: activeJob.client_request_id,
          recovered: true,
          existingJob: activeJob,
        });
        return;
      }

      if (decision === "redirect") {
        // The page mounted without an explicit request to generate (e.g. a
        // reopened or reloaded tab) and there is nothing to reconnect to. Do
        // not silently start a new build — return the user to the workspace.
        if (pendingGeneration) {
          clearPendingGenerationPayload();
        }
        router.replace("/");
        return;
      }

      clearGenerationIntent();
      await controller.startGeneration();
    })();

    return () => {
      cancelled = true;
    };
  }, [controller, fadeNavigate, payload, payloadHash, pendingGeneration, performanceFocusValidation?.isOverCap, router, session?.access_token]);

  return (
    <RequireAuth>
      {me?.effective_admin === true ? (
        <PremiumLoadingScreen
          phase={controller.phase}
          error={controller.error}
          statusMessage={controller.statusMessage}
          startedAtMs={controller.startedAtMs}
          endedAtMs={controller.endedAtMs}
          milestones={controller.milestones}
          intake={payload}
          failureKind={controller.failureKind}
          onRetry={() => {
            void controller.retryGeneration();
          }}
          canRetry={controller.canRetry}
          onOpenPlanHistory={() => router.push("/plans")}
          onReturnToWorkspace={() => router.push("/")}
          onRefineIntake={() => router.push("/onboarding")}
        />
      ) : (
        <PublicGenerationScreen
          phase={controller.phase}
          error={controller.error}
          startedAtMs={controller.startedAtMs}
          milestones={controller.milestones}
          jobId={controller.currentJobId}
          readyToOpen={controller.readyToOpen}
          intake={payload}
          failureKind={controller.failureKind}
          onRetry={() => {
            void controller.retryGeneration();
          }}
          canRetry={controller.canRetry}
          onReturnToWorkspace={() => router.push("/")}
          onRefineIntake={() => router.push("/onboarding")}
        />
      )}
    </RequireAuth>
  );
}
