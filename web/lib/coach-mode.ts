"use client";

import { useCallback, useSyncExternalStore } from "react";
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
  if (memory.has(userId)) return memory.get(userId)!;
  try {
    return validCoachMode(window.localStorage.getItem(key(userId)));
  } catch {
    return memory.get(userId) ?? DEFAULT_COACH_MODE;
  }
}

function writeMode(userId: string, value: CoachMode) {
  try {
    window.localStorage.setItem(key(userId), value);
    memory.delete(userId);
  } catch { memory.set(userId, value); }
  window.dispatchEvent(new Event(EVENT));
}

/** One account-scoped preference; storage events keep other tabs in sync. */
export function useCoachMode(userId: string | null | undefined) {
  const subscribe = useCallback((sync: () => void) => {
    if (!userId) return () => {};
    const onStorage = (event: StorageEvent) => {
      if (event.key === key(userId) || event.key === null) {
        memory.delete(userId);
        sync();
      }
    };
    window.addEventListener(EVENT, sync);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(EVENT, sync);
      window.removeEventListener("storage", onStorage);
    };
  }, [userId]);
  const mode = useSyncExternalStore(subscribe, () => userId ? readMode(userId) : null, () => null);
  const setMode = useCallback((value: CoachMode) => {
    if (userId) writeMode(userId, validCoachMode(value));
  }, [userId]);
  return {
    coachMode: mode ?? DEFAULT_COACH_MODE,
    ready: mode !== null,
    setMode,
  };
}

/** Structured plan types only. Never infer domain from a free-text name. */
export function planRelevance(type: string | null | undefined): CoachRelevance {
  if (type === "skill" || type === "sparring") return "technical";
  if (type === "strength_power" || type === "conditioning") return "sc";
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

/** Stable sort: preferred choices first; preserve the remaining choices' original order. No records are removed. */
export function prioritiseForMode<T>(items: readonly T[], mode: CoachMode | undefined, relevance: (item: T) => CoachRelevance): readonly T[] {
  if (!mode) return items;
  const rank = (item: T) => relevance(item) === mode ? 0 : 1;
  return [...items].sort((a, b) => rank(a) - rank(b));
}
