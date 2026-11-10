"use client";

import { createContext, useContext, useEffect, useRef, useState, useCallback, type ReactNode } from "react";
import { getActivePlan, getActiveGenerationJob, getGenerationJob, getLatestGenerationJob, isRetryableApiFailure } from "@/lib/api";
import {
  isPreStartStaleGenerationJob,
  resolveFailedJobWithSavedPlan,
  resolveTerminalJobPlanId,
} from "@/lib/generation-controller";
import { isExpiredPendingGeneration, isStaleVisibleGenerationJob, normalizeLegacyGenerationJobStatus } from "@/lib/generation-status-guards";
import {
  resolveGenerationEndedAtMs,
  subscribeGenerationTerminalJob,
} from "@/lib/generation-terminal-event";
import { offerProgrammeArrival } from "@/lib/programme-arrival";
import type { GenerationJobResponse, GenerationJobStatus } from "@/lib/types";

export type GlobalGenerationPhase = "queued" | "running" | "finalizing" | "completed" | "failed" | null;
export type GlobalTerminalGenerationStatus = "completed" | "review_required" | null;

export interface GenerationStatusContextValue {
  phase: GlobalGenerationPhase;
  jobId: string | null;
  clientRequestId: string | null;
  planId: string | null;
  athleteId: string | null;
  source: string | null;
  isActive: boolean;
  // True when the phase is "failed" only because the job stopped reporting
  // progress — the server may still be running it. The ribbon offers "Cancel
  // build" here and "Retry" for a genuinely terminal failure, because the
  // cancel endpoint rejects anything that is not queued/running.
  isStalled: boolean;
  statusMessage: string | null;
  terminalStatus: GlobalTerminalGenerationStatus;
  startedAtMs: number | null;
  // Backend-derived instant the job stopped (`completed_at ?? updated_at`).
  // Null while the job is live. The ribbon's elapsed clock reads from this
  // instead of Date.now() as soon as it lands, which is what stops the timer
  // rolling on after a completed / review-required / failed outcome.
  endedAtMs: number | null;
  requiresAdminResume: boolean;
  refreshStatus: () => void;
  latestJob: GenerationJobResponse | null;
}

const GenerationStatusContext = createContext<GenerationStatusContextValue | null>(null);
const PENDING_GENERATION_PREFIX = "unlxck:pending-generation:";
const GLOBAL_STATUS_POLL_MS = 15_000;
// A build held for admin review is terminal as far as the job poll is
// concerned, so nothing was watching it: the athlete's ribbon kept saying
// "held for admin review" long after an admin had approved and resumed it,
// until something else forced a navigation. A held job is polled on its own
// cadence so the approval lands on screen by itself.
const ADMIN_HOLD_POLL_MS = 8_000;
const INITIAL_STATUS_CHECK_DELAY_MS = 900;
// Returning to the tab re-checks generation status. With nothing in flight that
// check is two authenticated reads (active, then latest) that almost always
// report "no build", so it is skipped when the last check finished recently.
// A tracked job, a local pending record or an admin hold always re-checks.
export const PASSIVE_STATUS_RECHECK_MIN_INTERVAL_MS = 60_000;

export function shouldRunPassiveStatusCheck({
  trackedJobId,
  hasPendingRecord,
  adminHold,
  lastCheckedAtMs,
  nowMs = Date.now(),
}: {
  trackedJobId: string | null | undefined;
  hasPendingRecord: boolean;
  adminHold: boolean;
  lastCheckedAtMs: number;
  nowMs?: number;
}): boolean {
  if (shouldPollGenerationStatus(trackedJobId, hasPendingRecord) || adminHold) {
    return true;
  }
  return nowMs - lastCheckedAtMs >= PASSIVE_STATUS_RECHECK_MIN_INTERVAL_MS;
}

interface PendingGenerationState {
  clientRequestId: string;
  jobId?: string | null;
  createdAt: string;
}

type StoredPendingGenerationState = PendingGenerationState & {
  storageKey: string;
  createdAtMs: number;
};

export function shouldUseLocalPendingForRecovery(pending: PendingGenerationState | null): boolean {
  return Boolean(pending?.jobId);
}

/**
 * Whether the background poll should run this tick.
 *
 * This used to be `Boolean(pendingRecord)` alone, which was the trap: the
 * /generate controller deletes that record the instant it sees a terminal
 * result, and a same-tab localStorage write raises no `storage` event — so the
 * provider's poll switched off before it ever learned the job had ended, and
 * the ribbon's timer ran forever. A job this provider is still tracking keeps
 * the poll alive on its own.
 */
export function shouldPollAdminHoldStatus(
  latestJob: { requires_admin_resume?: boolean | null } | null | undefined,
): boolean {
  return latestJob?.requires_admin_resume === true;
}

export function shouldPollGenerationStatus(
  trackedJobId: string | null | undefined,
  hasPendingRecord: boolean,
): boolean {
  return Boolean(trackedJobId) || hasPendingRecord;
}

function parsePendingGeneration(storageKey: string): StoredPendingGenerationState | null {
  if (typeof window === "undefined") return null;

  const raw = window.localStorage.getItem(storageKey);
  if (!raw) return null;

  try {
    const decoded = JSON.parse(raw) as PendingGenerationState;
    if (!decoded?.clientRequestId) {
      window.localStorage.removeItem(storageKey);
      return null;
    }
    const createdAtMs = Date.parse(decoded.createdAt || "");
    return {
      ...decoded,
      storageKey,
      createdAtMs: Number.isFinite(createdAtMs) ? createdAtMs : 0,
    };
  } catch {
    window.localStorage.removeItem(storageKey);
    return null;
  }
}

function listPendingGenerations(): StoredPendingGenerationState[] {
  if (typeof window === "undefined") return [];

  return Object.keys(window.localStorage)
    .filter((key) => key.startsWith(PENDING_GENERATION_PREFIX))
    .map(parsePendingGeneration)
    .filter((pending): pending is StoredPendingGenerationState => pending !== null)
    .sort((a, b) => b.createdAtMs - a.createdAtMs);
}

function getPendingGeneration(): PendingGenerationState | null {
  const [latest, ...duplicates] = listPendingGenerations();

  duplicates.forEach((pending) => {
    window.localStorage.removeItem(pending.storageKey);
  });

  return latest ?? null;
}

function savePendingGeneration(pending: PendingGenerationState): void {
  if (typeof window === "undefined") return;
  const existing = listPendingGenerations();
  existing.forEach((item) => {
    window.localStorage.removeItem(item.storageKey);
  });
  const key = `${PENDING_GENERATION_PREFIX}self`;
  window.localStorage.setItem(key, JSON.stringify(pending));
}

function clearPendingGenerations(): void {
  if (typeof window === "undefined") return;

  Object.keys(window.localStorage)
    .filter((key) => key.startsWith(PENDING_GENERATION_PREFIX))
    .forEach((key) => window.localStorage.removeItem(key));
}

function isTerminalStatus(status: GenerationJobStatus): boolean {
  return status === "completed" || status === "review_required" || status === "failed";
}

export function shouldRetainLatestJob(
  job: GenerationJobResponse | null | undefined,
  nowMs = Date.now(),
): boolean {
  if (!job) return false;
  const normalizedStatus = normalizeLegacyGenerationJobStatus(job.status) as GenerationJobStatus;
  // A terminal job with no openable plan (no plan_id and no latest_plan_id) has
  // nothing to show or act on, so it must not linger as a passive ribbon —
  // UNLESS it is a protected triage outcome that lives only on the job, in
  // which case the ribbon must keep surfacing the "admin review required"
  // state so the user can see it and an admin can act on it.
  if (isTerminalStatus(normalizedStatus) && !resolveTerminalJobPlanId(job)) {
    if (job.requires_admin_resume === true) {
      return true;
    }
    // A recent failure the backend says is retryable IS actionable. Dropping it
    // meant a failed build vanished without trace the moment the user left
    // /generate: no notice, no retry, nothing in the workspace.
    if (normalizedStatus === "failed" && job.can_retry === true) {
      return isRecentTerminalJob(job, nowMs);
    }
    return false;
  }
  return true;
}

export const MAX_PASSIVE_FAILED_JOB_AGE_MS = 24 * 60 * 60 * 1000;

function isRecentTerminalJob(job: GenerationJobResponse, nowMs: number): boolean {
  const terminalAtMs = Date.parse(job.completed_at || job.updated_at || job.created_at || "");
  if (!Number.isFinite(terminalAtMs)) {
    return false;
  }
  return nowMs - terminalAtMs <= MAX_PASSIVE_FAILED_JOB_AGE_MS;
}

function phaseFromStatus(status: GenerationJobStatus): GlobalGenerationPhase {
  if (status === "queued") return "queued";
  if (status === "running") return "running";
  if (status === "completed" || status === "review_required") return "completed";
  if (status === "failed") return "failed";
  return null;
}

export function statusMessage(
  phase: GlobalGenerationPhase,
  terminalStatus: GlobalTerminalGenerationStatus,
  requiresAdminResume = false,
): string {
  switch (phase) {
    case "queued":
      return "Plan request queued...";
    case "running":
      return "Generating plan...";
    case "finalizing":
      return "Finalizing plan...";
    case "completed":
      // A triage hold has no plan to open — saying "ready for review" reads as
      // if the athlete can act on it. Name the state that actually applies.
      if (requiresAdminResume) {
        return "Admin review required.";
      }
      return terminalStatus === "review_required" ? "Plan ready for review." : "Plan ready!";
    case "failed":
      return "Your plan build stopped.";
    default:
      return "";
  }
}

// Copy for a build the backend marked `failed` that nevertheless saved an
// openable plan. The generate controller already treats this as a success and
// navigates to the plan; the ribbon has to agree, or the user lands on their
// finished plan with a red "your plan build stopped" banner over it.
export const RECOVERED_PLAN_STATUS_MESSAGE = "Your plan is saved and ready.";

export type TerminalGenerationView = {
  phase: GlobalGenerationPhase;
  terminalStatus: GlobalTerminalGenerationStatus;
  planId: string | null;
  athleteId: string | null;
  source: string | null;
  statusMessage: string;
  startedAtMs: number | null;
  endedAtMs: number | null;
  requiresAdminResume: boolean;
  // True when the backend said `failed` but a plan survived. The outcome the
  // user gets is an openable plan, so the ribbon presents it as one.
  recoveredPlanFromFailure: boolean;
};

/**
 * The ribbon state a terminal job produces, derived from the job alone. Both
 * the poll and the in-tab terminal event go through this, so a job that
 * arrives by event lands the provider in exactly the state a poll would have —
 * there is no second, divergent way to become terminal.
 *
 * Returns null when the job is not terminal.
 */
export function resolveTerminalGenerationView(
  job: GenerationJobResponse,
  fallbackEndedAtMs: number | null = null,
): TerminalGenerationView | null {
  const normalizedStatus = normalizeLegacyGenerationJobStatus(job.status) as GenerationJobStatus;
  if (!isTerminalStatus(normalizedStatus)) {
    return null;
  }

  // `failed` with a saved plan (plan_id or latest_plan_id) is not a failure the
  // user can act on as one: the plan exists and the controller opens it. The
  // job status is a build-pipeline detail, not the athlete's outcome.
  const recoveredPlanId = resolveFailedJobWithSavedPlan(job);
  const recoveredPlanFromFailure = Boolean(recoveredPlanId);

  const phase = recoveredPlanFromFailure ? "completed" : phaseFromStatus(normalizedStatus);
  const terminalStatus: GlobalTerminalGenerationStatus = recoveredPlanFromFailure
    ? "completed"
    : normalizedStatus === "completed" || normalizedStatus === "review_required"
      ? normalizedStatus
      : null;
  const requiresAdminResume = job.requires_admin_resume === true;
  const createdAtMs = Date.parse(job.created_at || "");

  return {
    phase,
    terminalStatus,
    planId: recoveredPlanFromFailure ? recoveredPlanId : terminalStatus ? job.plan_id || null : null,
    athleteId: job.athlete_id || null,
    source: job.source || null,
    // A triage hold still outranks the recovered-plan copy: "saved and ready"
    // would promise an outcome the athlete cannot act on yet.
    statusMessage:
      recoveredPlanFromFailure && !requiresAdminResume
        ? RECOVERED_PLAN_STATUS_MESSAGE
        : statusMessage(phase, terminalStatus, requiresAdminResume),
    startedAtMs: Number.isFinite(createdAtMs) ? createdAtMs : null,
    endedAtMs: resolveGenerationEndedAtMs(job, fallbackEndedAtMs),
    requiresAdminResume,
    recoveredPlanFromFailure,
  };
}

interface GenerationStatusProviderProps {
  children: ReactNode;
  token: string | null;
}

export function GenerationStatusProvider({ children, token }: GenerationStatusProviderProps) {
  const [phase, setPhase] = useState<GlobalGenerationPhase>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [clientRequestId, setClientRequestId] = useState<string | null>(null);
  const [planId, setPlanId] = useState<string | null>(null);
  const [athleteId, setAthleteId] = useState<string | null>(null);
  const [source, setSource] = useState<string | null>(null);
  const [statusMessageText, setStatusMessageText] = useState<string | null>(null);
  const [terminalStatus, setTerminalStatus] = useState<GlobalTerminalGenerationStatus>(null);
  const [startedAtMs, setStartedAtMs] = useState<number | null>(null);
  const [endedAtMs, setEndedAtMs] = useState<number | null>(null);
  const [requiresAdminResume, setRequiresAdminResume] = useState(false);
  const [latestJob, setLatestJob] = useState<GenerationJobResponse | null>(null);
  const [isStalled, setIsStalled] = useState(false);

  // Track the clear timeout so we can cancel it on unmount — prevents
  // state updates on an unmounted component
  const clearTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isCheckingRef = useRef(false);
  const wasAuthenticatedRef = useRef(Boolean(token));
  const latestTokenRef = useRef(token);
  const checkSequenceRef = useRef(0);
  // Lets the terminal-state clear timer re-run the check without making
  // checkStatus depend on itself.
  const checkStatusRef = useRef<(() => Promise<void>) | null>(null);
  // The job this provider is following, retained independently of
  // localStorage. The /generate controller clears the shared pending record as
  // soon as it sees a terminal result, and a same-tab localStorage write fires
  // no `storage` event — so a poll gated purely on that record could stop
  // before this provider ever learned the job had finished, leaving the ribbon
  // stuck on "running" with its timer climbing forever. The provider now keeps
  // checking the exact job until the backend confirms a terminal status (or an
  // in-tab terminal event delivers it).
  const trackedJobRef = useRef<PendingGenerationState | null>(null);
  const jobIdRef = useRef<string | null>(null);
  const adminHoldRef = useRef(false);
  const lastCheckedAtRef = useRef(0);

  const setTrackedJob = useCallback((pending: PendingGenerationState | null) => {
    trackedJobRef.current = pending?.jobId ? pending : null;
  }, []);

  // Drops everything about the job currently on the ribbon, including the
  // retained tracking record — the single place that decides what "no active
  // job" means, so no exit path can leave half of it behind.
  const clearActiveJobState = useCallback(() => {
    trackedJobRef.current = null;
    jobIdRef.current = null;
    setPhase(null);
    setJobId(null);
    setClientRequestId(null);
    setPlanId(null);
    setAthleteId(null);
    setSource(null);
    setStatusMessageText(null);
    setTerminalStatus(null);
    setStartedAtMs(null);
    setEndedAtMs(null);
    setRequiresAdminResume(false);
    setIsStalled(false);
  }, []);

  const resetGenerationState = useCallback(() => {
    clearActiveJobState();
    setLatestJob(null);
  }, [clearActiveJobState]);

  const setActiveJobId = useCallback((nextJobId: string | null) => {
    jobIdRef.current = nextJobId;
    setJobId(nextJobId);
  }, []);

  useEffect(() => {
    latestTokenRef.current = token;
    checkSequenceRef.current++;
    isCheckingRef.current = false;
    if (clearTimerRef.current !== null) {
      clearTimeout(clearTimerRef.current);
      clearTimerRef.current = null;
    }
    resetGenerationState();
    // Clear local pending state on every auth change so the previous session's
    // global key cannot leak into the next authenticated user; backend
    // active/latest endpoints restore the current user's real state after login.
    clearPendingGenerations();
  }, [token, resetGenerationState]);

  // Cancel any pending clear timer when the component unmounts
  useEffect(() => {
    return () => {
      if (clearTimerRef.current !== null) {
        clearTimeout(clearTimerRef.current);
      }
    };
  }, []);

  const checkStatus = useCallback(async () => {
    if (token && isCheckingRef.current) return;
    const sequence = ++checkSequenceRef.current;

    if (!token) {
      checkSequenceRef.current++;
      if (wasAuthenticatedRef.current) {
        clearPendingGenerations();
      }
      resetGenerationState();
      wasAuthenticatedRef.current = false;
      isCheckingRef.current = false;
      return;
    }

    wasAuthenticatedRef.current = true;

    if (isCheckingRef.current) {
      return;
    }

    isCheckingRef.current = true;

    try {
      let activePending: PendingGenerationState | null = null;
      try {
        const activeJob = await getActiveGenerationJob(token);
        if (sequence !== checkSequenceRef.current || !latestTokenRef.current) {
          return;
        }
        if (activeJob?.client_request_id && activeJob.created_at) {
          activePending = {
            clientRequestId: activeJob.client_request_id,
            jobId: activeJob.job_id,
            createdAt: activeJob.created_at,
          };
          setLatestJob(null);
          savePendingGeneration(activePending);
        }
      } catch {
        // Active endpoint unavailable: fall back to local pending recovery.
      }
      if (!activePending) {
        const pending = getPendingGeneration();
        if (pending && isExpiredPendingGeneration(pending.createdAt)) {
          clearPendingGenerations();
        } else if (shouldUseLocalPendingForRecovery(pending)) {
          activePending = pending;
        } else {
          clearPendingGenerations();
        }
      }
      if (!activePending && trackedJobRef.current) {
        // The pending record can disappear underneath us: the /generate
        // controller clears it the moment it sees a terminal result, and a
        // same-tab localStorage write raises no `storage` event. Keep checking
        // the exact job we were already following until the backend confirms
        // it is over — otherwise the ribbon keeps rendering "running" against
        // a job nobody is watching any more.
        activePending = trackedJobRef.current;
      }
      if (!activePending) {
        try {
          const latest = await getLatestGenerationJob(token);
          if (sequence !== checkSequenceRef.current || !latestTokenRef.current) return;
          // A reload may land after the job finished, with no active job left.
          // Only recover a recent completion, never introduce historical plans.
          const terminal = latest ? resolveTerminalGenerationView(latest, Date.now()) : null;
          const completionPlanId = latest ? resolveTerminalJobPlanId(latest) : null;
          const completedAt = Date.parse(latest?.completed_at || latest?.updated_at || "");
          const age = Number.isFinite(completedAt) ? Date.now() - completedAt : Infinity;
          if (terminal?.terminalStatus === "completed" && completionPlanId && !terminal.requiresAdminResume
            && age >= 0 && age < 5 * 60 * 1000) {
            const active = await getActivePlan(token);
            if (sequence !== checkSequenceRef.current || !latestTokenRef.current) return;
            if (active?.plan_id === completionPlanId) offerProgrammeArrival(completionPlanId);
          }
          setLatestJob(shouldRetainLatestJob(latest) ? latest : null);
        } catch {
          setLatestJob(null);
        }
        clearActiveJobState();
        return;
      }

      setClientRequestId(activePending.clientRequestId);
      const pendingCreatedAt = Date.parse(activePending.createdAt || "");
      setStartedAtMs(Number.isFinite(pendingCreatedAt) ? pendingCreatedAt : null);
      setLatestJob(null);
      setTrackedJob(activePending);

      if (activePending.jobId && token) {
        try {
          const job: GenerationJobResponse = await getGenerationJob(token, activePending.jobId);
          if (sequence !== checkSequenceRef.current || !latestTokenRef.current) {
            return;
          }
          const stalledBeforeStart = isPreStartStaleGenerationJob(job);
          const staleVisibleJob = isStaleVisibleGenerationJob(job);
          const normalizedStatus = normalizeLegacyGenerationJobStatus(job.status) as GenerationJobStatus;
          const newPhase = stalledBeforeStart || staleVisibleJob ? "failed" : phaseFromStatus(normalizedStatus);
          const newTerminalStatus = normalizedStatus === "completed" || normalizedStatus === "review_required" ? normalizedStatus : null;
          const isStalledJob = stalledBeforeStart || staleVisibleJob;
          const terminalView = isStalledJob ? null : resolveTerminalGenerationView(job, Date.now());
          // The view wins whenever it exists: it is the one place that knows a
          // `failed` job carrying a saved plan is really a completed one, and
          // the poll must not reach a different verdict than the event path.
          setPhase(terminalView?.phase ?? newPhase);
          setActiveJobId(activePending.jobId);
          setTerminalStatus(terminalView?.terminalStatus ?? newTerminalStatus);
          setIsStalled(isStalledJob);
          setRequiresAdminResume(terminalView?.requiresAdminResume ?? false);
          setStatusMessageText(
            isStalledJob
              ? "This build stopped responding."
              : terminalView?.statusMessage ?? statusMessage(newPhase, newTerminalStatus),
          );

          if (isStalledJob) {
            // A stalled build is over as far as this screen is concerned;
            // freeze the clock where it was first detected rather than
            // re-freezing it a second later on every poll.
            setEndedAtMs((previous) => previous ?? Date.now());
          } else {
            // Null for a live job — the clock runs off Date.now() again if a
            // job we had written off resumes reporting progress.
            setEndedAtMs(terminalView?.endedAtMs ?? null);
          }

          // Null for a live or stalled job; for a terminal one this carries the
          // recovered plan of a `failed` build that still saved something.
          setPlanId(terminalView?.planId ?? null);
          setAthleteId(job.athlete_id || null);
          setSource(job.source || null);

          if (stalledBeforeStart || staleVisibleJob) {
            // Deliberately leave the pending-generation record in place and
            // keep polling this job at the normal cadence, re-setting the
            // same "Build stalled" state each time. Auto-hiding it after a
            // few seconds (the old behaviour) only for the next poll —
            // interval tick or tab refocus — to rediscover the same
            // still-"running" job and flash the banner right back is what
            // made the ribbon appear to flicker every few seconds. It now
            // stays put until the user dismisses it, retries, or cancels the
            // job (which resolves the job server-side so the next poll picks
            // up the real terminal status instead).
          } else if (isTerminalStatus(normalizedStatus)) {
            // The backend confirmed a terminal status, so this is the one
            // moment the retained job may be released: nothing else needs to
            // be polled for it.
            trackedJobRef.current = null;
            clearPendingGenerations();

            // Schedule the status clear — cancel any previous pending clear first
            if (clearTimerRef.current !== null) {
              clearTimeout(clearTimerRef.current);
            }
            // A recovered plan reads as a success, so it gets the longer
            // success dwell rather than the short failure one.
            const delay = terminalView?.phase === "failed" ? 3000 : 5000;
            clearTimerRef.current = setTimeout(() => {
              clearTimerRef.current = null;
              clearActiveJobState();
              // Hand off to the passive latest-job ribbon (a failed build the
              // user can still retry). Clearing the pending record just
              // stopped the interval poll, so without this re-check the
              // failure would stay invisible until the next navigation.
              void checkStatusRef.current?.();
            }, delay);
          }
        } catch (pollError) {
          // A connection drop says nothing about the build itself: keep the
          // pending record and the ribbon as-is so the next poll (or the
          // reconnect) re-attaches to the same job instead of forgetting it.
          if (isRetryableApiFailure(pollError)) {
            return;
          }
          clearPendingGenerations();
          clearActiveJobState();
          setLatestJob(null);
        }
      } else {
        clearPendingGenerations();
        clearActiveJobState();
      }
    } finally {
      isCheckingRef.current = false;
      lastCheckedAtRef.current = Date.now();
    }
  }, [clearActiveJobState, resetGenerationState, setActiveJobId, setTrackedJob, token]);

  useEffect(() => {
    checkStatusRef.current = checkStatus;
  }, [checkStatus]);

  // Terminal state, delivered in-tab by whichever component saw it first
  // (today: the /generate controller). Without this the ribbon had to wait for
  // its own poll — and that poll was gated on a localStorage record the
  // controller had already deleted, so it never came.
  const applyTerminalJob = useCallback(
    (job: GenerationJobResponse) => {
      // A signed-out provider owns no job state; the auth effect has already
      // reset it and must not be undone by a late event.
      if (!latestTokenRef.current) {
        return;
      }

      const tracked = trackedJobRef.current;
      const isOurJob =
        (tracked?.jobId && tracked.jobId === job.job_id) || jobIdRef.current === job.job_id;

      const view = resolveTerminalGenerationView(job, Date.now());
      if (!view) {
        // Not actually terminal — reconcile against the backend rather than
        // trusting the payload.
        void checkStatusRef.current?.();
        return;
      }

      if (!isOurJob && (tracked !== null || jobIdRef.current !== null)) {
        // A terminal event for some other job while we are following one:
        // let the poll decide what the ribbon should show.
        void checkStatusRef.current?.();
        return;
      }

      // The controller has already dropped the shared pending record; make
      // sure nothing can resurrect this job as "in flight".
      trackedJobRef.current = null;
      clearPendingGenerations();

      if (clearTimerRef.current !== null) {
        clearTimeout(clearTimerRef.current);
        clearTimerRef.current = null;
      }

      setActiveJobId(job.job_id);
      setClientRequestId(job.client_request_id || null);
      setPhase(view.phase);
      setTerminalStatus(view.terminalStatus);
      setPlanId(view.planId);
      setAthleteId(view.athleteId);
      setSource(view.source);
      setStatusMessageText(view.statusMessage);
      setRequiresAdminResume(view.requiresAdminResume);
      setIsStalled(false);
      setLatestJob(null);
      if (view.startedAtMs !== null) {
        setStartedAtMs(view.startedAtMs);
      }
      // The freeze. From here the elapsed label is a subtraction of two fixed
      // backend timestamps, so it cannot advance again for this job.
      setEndedAtMs(view.endedAtMs);

      const delay = view.phase === "failed" ? 3000 : 5000;
      clearTimerRef.current = setTimeout(() => {
        clearTimerRef.current = null;
        clearActiveJobState();
        void checkStatusRef.current?.();
      }, delay);
    },
    [clearActiveJobState, setActiveJobId],
  );

  useEffect(() => subscribeGenerationTerminalJob(applyTerminalJob), [applyTerminalJob]);

  useEffect(() => {
    const initialCheckTimer = window.setTimeout(() => {
      void checkStatus();
    }, token ? INITIAL_STATUS_CHECK_DELAY_MS : 0);

    const interval = setInterval(() => {
      // Retained job first: local pending state is not the source of truth for
      // "is a build still in flight", it is only one way to discover one.
      if (
        shouldPollGenerationStatus(trackedJobRef.current?.jobId, Boolean(getPendingGeneration())) ||
        latestJob?.requires_admin_resume === true
      ) {
        void checkStatus();
      }
    }, GLOBAL_STATUS_POLL_MS);

    const adminHoldInterval = setInterval(() => {
      if (adminHoldRef.current) {
        void checkStatus();
      }
    }, ADMIN_HOLD_POLL_MS);

    const handleStorageChange = () => {
      void checkStatus();
    };

    const handleVisibilityChange = () => {
      if (
        document.visibilityState === "visible" &&
        shouldRunPassiveStatusCheck({
          trackedJobId: trackedJobRef.current?.jobId,
          hasPendingRecord: Boolean(getPendingGeneration()),
          adminHold: adminHoldRef.current,
          lastCheckedAtMs: lastCheckedAtRef.current,
        })
      ) {
        void checkStatus();
      }
    };

    // Back online: re-attach to whatever was building while we were away.
    const handleOnline = () => {
      void checkStatus();
    };

    window.addEventListener("storage", handleStorageChange);
    window.addEventListener("online", handleOnline);
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      window.clearTimeout(initialCheckTimer);
      clearInterval(interval);
      clearInterval(adminHoldInterval);
      window.removeEventListener("storage", handleStorageChange);
      window.removeEventListener("online", handleOnline);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [checkStatus, token, latestJob?.requires_admin_resume]);

  useEffect(() => {
    adminHoldRef.current = shouldPollAdminHoldStatus(latestJob);
  }, [latestJob]);

  const value: GenerationStatusContextValue = {
    phase,
    jobId,
    clientRequestId,
    planId,
    athleteId,
    source,
    isActive: phase !== null,
    isStalled,
    statusMessage: statusMessageText,
    terminalStatus,
    startedAtMs,
    endedAtMs,
    requiresAdminResume,
    refreshStatus: checkStatus,
    latestJob,
  };

  return (
    <GenerationStatusContext.Provider value={value}>
      {children}
    </GenerationStatusContext.Provider>
  );
}

export function useGenerationStatus(): GenerationStatusContextValue {
  const context = useContext(GenerationStatusContext);
  if (!context) {
    throw new Error("useGenerationStatus must be used within GenerationStatusProvider");
  }
  return context;
}
