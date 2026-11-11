"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import type { ExerciseLogging } from "@/components/exercise-log";
import { listTodayExerciseLogs, submitExerciseLog, submitExerciseLogs } from "@/lib/api";
import type { ExerciseLogRecord, ExerciseRecentLoad } from "@/lib/types";

/**
 * Today's exercise logs for the active plan, and the save that writes one.
 * Fetches history before Start; canWrite keeps measurement controls closed
 * until the session starts.
 */
export function useTodayExerciseLogs({
  token,
  planId,
  trainingDay,
  enabled,
  canWrite = true,
  lowerLoads = false,
  painReasonAllowed,
  allowProgression = false,
  onError,
}: {
  token: string;
  planId: string;
  /** The server's training day: a new day starts from an empty set of logs. */
  trainingDay: string;
  /** The server only accepts a log for a session started or completed today. */
  enabled: boolean;
  canWrite?: boolean;
  lowerLoads?: boolean;
  painReasonAllowed: boolean;
  allowProgression?: boolean;
  /** Reports a failed one-tap save from a collapsed row. */
  onError?: (message: string) => void;
}): ExerciseLogging | null {
  const scope = `${token}:${planId}:${trainingDay}`;
  const [loaded, setLoaded] = useState<{
    scope: string;
    logs: Record<string, ExerciseLogRecord>;
    recentLoads?: ExerciseRecentLoad[];
    recentPerformances?: ExerciseLogRecord[];
  }>({
    scope,
    logs: {},
  });
  const active = enabled && Boolean(token) && Boolean(planId);

  useEffect(() => {
    if (!active) {
      return;
    }
    let cancelled = false;
    void listTodayExerciseLogs(token, planId)
      .then((response) => {
        if (cancelled) return;
        const fetched = Object.fromEntries(response.logs.map((log) => [log.block_id, log]));
        // A log saved while the list was loading is newer than the list.
        setLoaded((current) => ({
          scope,
          logs: current.scope === scope ? { ...fetched, ...current.logs } : fetched,
          recentLoads: response.recent_loads ?? [],
          recentPerformances: response.recent_performances ?? [],
        }));
      })
      .catch((error: unknown) => {
        // The rows stay loggable; a save still returns the stored log.
        if (process.env.NODE_ENV !== "production") {
          console.error("Exercise logs could not be loaded", error);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [active, token, planId, scope]);

  const save = useCallback<ExerciseLogging["save"]>(
    async (request) => {
      const response = await submitExerciseLog(token, { plan_id: planId, ...request });
      setLoaded((current) => ({
        ...(current.scope === scope ? current : {}),
        scope,
        logs: { ...(current.scope === scope ? current.logs : {}), [response.log.block_id]: response.log },
      }));
    },
    [token, planId, scope],
  );

  const saveMany = useCallback<NonNullable<ExerciseLogging["saveMany"]>>(
    async (requests, options) => {
      if (requests.length === 0) return;
      const response = await submitExerciseLogs(token, {
        plan_id: planId,
        entries: requests,
        ...(options?.keepExisting ? { keep_existing: true } : {}),
      });
      const saved = Object.fromEntries(response.logs.map((log) => [log.block_id, log]));
      setLoaded((current) => ({
        ...(current.scope === scope ? current : {}),
        scope,
        logs: { ...(current.scope === scope ? current.logs : {}), ...saved },
      }));
    },
    [token, planId, scope],
  );

  const logs = loaded.scope === scope ? loaded.logs : EMPTY_LOGS;
  const recentLoads = loaded.scope === scope ? loaded.recentLoads : undefined;
  const recentPerformances = loaded.scope === scope ? loaded.recentPerformances : undefined;
  return useMemo(
    () => (active ? { lowerLoads, canWrite, logs, save, saveMany, recentLoads, recentPerformances, allowProgression, painReasonAllowed, onError } : null),
    [lowerLoads, canWrite, active, logs, save, saveMany, recentLoads, recentPerformances, allowProgression, painReasonAllowed, onError],
  );
}

const EMPTY_LOGS: Record<string, ExerciseLogRecord> = {};
