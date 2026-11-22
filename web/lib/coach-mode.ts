"use client";

import { useCallback, useEffect, useState } from "react";
import { activityFor, sessionTypes, type TrainingPick } from "./training-disciplines";

export type CoachMode = "technical" | "sc";
export type CoachRelevance = CoachMode | "shared";
export const DEFAULT_COACH_MODE: CoachMode = "sc";
const EVENT = "unlxck-coach-mode";
const memory = new Map<string, CoachMode>();
const key = (userId: string) => `unlxck:coach-mode:${userId}`;

export function validCoachMode(value: unknown): CoachMode {
  return value === "technical" || value === "sc" ? value : DEFAULT_COACH_MODE;
}

function readMode(userId: string): CoachMode {
  try {
    return validCoachMode(window.localStorage.getItem(key(userId)));
  } catch {
    return memory.get(userId) ?? DEFAULT_COACH_MODE;
  }
}

function writeMode(userId: string, value: CoachMode) {
  memory.set(userId, value);
  try { window.localStorage.setItem(key(userId), value); } catch { /* In-memory fallback. */ }
  window.dispatchEvent(new Event(EVENT));
}

/** One account-scoped preference; storage events keep other tabs in sync. */
export function useCoachMode(userId: string | null | undefined) {
  const [snapshot, setSnapshot] = useState<{ userId: string; mode: CoachMode } | null>(null);
  useEffect(() => {
    if (!userId) { setSnapshot(null); return; }
    const sync = () => setSnapshot({ userId, mode: readMode(userId) });
    const onStorage = (event: StorageEvent) => {
      if (event.key === key(userId) || event.key === null) sync();
    };
    sync();
    window.addEventListener(EVENT, sync);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(EVENT, sync);
      window.removeEventListener("storage", onStorage);
    };
  }, [userId]);
  const setMode = useCallback((value: CoachMode) => {
    if (userId) writeMode(userId, validCoachMode(value));
  }, [userId]);
  return {
    coachMode: snapshot?.userId === userId ? snapshot.mode : DEFAULT_COACH_MODE,
    ready: Boolean(userId && snapshot?.userId === userId),
    setMode,
  };
}

/** Structured plan types only. Never infer domain from a free-text name. */
export function planRelevance(type: string | null | undefined): CoachRelevance {
  if (type === "skill" || type === "sparring") return "technical";
  if (type === "strength_power" || type === "conditioning") return "sc";
  return "shared";
}

export function libraryRelevance(contentType: string | null | undefined): CoachRelevance {
  if (contentType === "technical_coordination") return "technical";
  if (contentType === "strength_power" || contentType === "conditioning") return "sc";
  return "shared";
}

/** Sport-specific work must match the shared discipline catalogue to count. */
export function trainingRelevance(pick: TrainingPick): CoachRelevance {
  if (pick.sport) {
    return pick.session_type && sessionTypes(pick.sport).some(item => item.id === pick.session_type)
      && pick.activity === activityFor(pick.sport, pick.session_type) ? "technical" : "shared";
  }
  if (pick.session_type) return "shared";
  if (["strength", "conditioning", "roadwork"].includes(pick.activity)) return "sc";
  if (["boxing", "sparring"].includes(pick.activity)) return "technical";
  return "shared";
}

/** Stable sort: preferred, shared, then the other mode. No records are removed. */
export function prioritiseForMode<T>(items: readonly T[], mode: CoachMode, relevance: (item: T) => CoachRelevance): T[] {
  const rank = (item: T) => relevance(item) === mode ? 0 : relevance(item) === "shared" ? 1 : 2;
  return [...items].sort((a, b) => rank(a) - rank(b));
}
