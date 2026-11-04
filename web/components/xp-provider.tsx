"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { useAppSession } from "@/components/auth-provider";
import { XP_ACTIONS, resolveXpLevel, type XpAction, type XpAwardRecord } from "@/lib/xp";
import { getXpProgress, recordAppActivity } from "@/lib/xp-api";
import { XP_REFRESH_EVENT } from "@/lib/xp-events";
import { createFreshXpProgress, type XpProgress } from "@/lib/xp-progress";

export const XP_PROGRESS_POLL_MS = 60_000;
// Passive refreshes (tab focus, visibility, the poll) are skipped when progress
// was loaded this recently. Returning to the tab fires both `focus` and
// `visibilitychange`; without this each return cost two back-to-back progress
// reads. Explicit refreshes (XP_REFRESH_EVENT after a write) always run.
export const XP_PASSIVE_REFRESH_MIN_INTERVAL_MS = 30_000;

export type XpFeedbackEvent =
  | {
      kind: "routine";
      amount: number;
      label: string;
      awardIds: string[];
      actions: XpAction[];
    }
  | {
      kind: "level_up";
      level: number;
      title: string;
      message: string;
      awardIds: string[];
    };

type XpContextValue = {
  progress: XpProgress;
  isHydrated: boolean;
  isRefreshing: boolean;
  error: string | null;
  feedback: XpFeedbackEvent | null;
  refresh: () => Promise<void>;
  dismissFeedback: () => void;
};

type XpViewState = Omit<XpContextValue, "refresh" | "dismissFeedback"> & {
  athleteId: string | null;
};

const initialViewState = (): XpViewState => ({
  athleteId: null,
  progress: createFreshXpProgress(),
  isHydrated: false,
  isRefreshing: false,
  error: null,
  feedback: null,
});

function feedbackLabel(awards: XpAwardRecord[]): string {
  const actions = new Set(awards.map((award) => award.action));

  // A final session can trigger several lifecycle rewards at once. Name the
  // most important milestone in the batch rather than reducing it to the
  // ordinary session action that happened to trigger reconciliation.
  if (actions.has("camp_completed")) {
    return "Fight camp complete";
  }
  if (actions.has("first_plan_completed")) {
    return "Training plan complete";
  }
  if (actions.has("phase_completed")) {
    return "Training phase complete";
  }
  if (actions.has("full_training_week_completed")) {
    return "Training week complete";
  }
  for (const action of ["exercise_contribution_approved", "training_days_100", "training_days_50", "training_days_25", "training_days_10", "first_session_logged", "first_exercise_logged"] as const) {
    if (actions.has(action)) return XP_ACTIONS[action].label;
  }
  if (
    actions.has("training_logged") ||
    actions.has("planned_session_completed")
  ) {
    return "Session logged. Work done.";
  }
  if (
    actions.has("first_checkin_completed") ||
    actions.has("readiness_checkin_completed")
  ) {
    return "Check-in complete";
  }
  if (actions.has("injury_update_completed")) {
    return "Injury update saved";
  }
  if (awards.length === 1) {
    return XP_ACTIONS[awards[0].action].label;
  }
  return "Progress recorded";
}

export const XpContext = createContext<XpContextValue | undefined>(undefined);

export function XpProvider({ children }: Readonly<{ children: ReactNode }>) {
  const { me, session } = useAppSession();
  const athleteId = me?.profile.role === "athlete" ? me.profile.athlete_id.trim() : "";
  const accessToken = session?.access_token ?? "";
  const identityKey = athleteId && accessToken ? `${athleteId}:${accessToken}` : "";
  const [view, setView] = useState<XpViewState>(() => ({
    ...initialViewState(),
    isHydrated: !athleteId,
  }));
  const activeIdentityRef = useRef(identityKey);
  const activeAthleteRef = useRef(athleteId);
  const progressRef = useRef(createFreshXpProgress());
  const baselineReadyRef = useRef(false);
  const seenAwardIdsRef = useRef<Set<string>>(new Set());
  const inFlightRef = useRef<Promise<void> | null>(null);
  const trailingRefreshRef = useRef(false);
  const lastLoadedAtRef = useRef(0);

  useEffect(() => {
    activeIdentityRef.current = identityKey;
    if (activeAthleteRef.current === athleteId) return;

    activeAthleteRef.current = athleteId;
    progressRef.current = createFreshXpProgress();
    baselineReadyRef.current = false;
    seenAwardIdsRef.current = new Set();
    inFlightRef.current = null;
    trailingRefreshRef.current = false;
    lastLoadedAtRef.current = 0;
    setView({
      ...initialViewState(),
      athleteId: athleteId || null,
      isHydrated: !athleteId,
    });
  }, [athleteId, identityKey]);

  const applyProgress = useCallback(
    (next: XpProgress, targetAthleteId: string, targetIdentityKey: string) => {
      if (activeIdentityRef.current !== targetIdentityKey) return;

      const previous = progressRef.current;
      let feedback: XpFeedbackEvent | null = null;
      const currentIds = new Set(next.state.recentAwards.map((award) => award.id));

      if (baselineReadyRef.current) {
        const newAwards = next.state.recentAwards.filter(
          (award) => !seenAwardIdsRef.current.has(award.id),
        );
        const previousLevel = resolveXpLevel(previous.state.totalXp).currentLevel;
        const currentLevel = resolveXpLevel(next.state.totalXp).currentLevel;
        const awardIds = newAwards.map((award) => award.id);

        if (currentLevel.level > previousLevel.level) {
          feedback = {
            kind: "level_up",
            level: currentLevel.level,
            title: currentLevel.title,
            message: "Built through consistent work.",
            awardIds,
          };
        } else if (newAwards.length > 0) {
          feedback = {
            kind: "routine",
            amount: newAwards.reduce((total, award) => total + award.amount, 0),
            label: feedbackLabel(newAwards),
            awardIds,
            actions: newAwards.map((award) => award.action),
          };
        }
      }

      baselineReadyRef.current = true;
      seenAwardIdsRef.current = currentIds;
      progressRef.current = next;
      setView({
        athleteId: targetAthleteId,
        progress: next,
        isHydrated: true,
        isRefreshing: false,
        error: null,
        feedback,
      });
    },
    [],
  );

  const load = useCallback(async () => {
    if (!athleteId || !accessToken || !identityKey) return;
    if (inFlightRef.current) {
      trailingRefreshRef.current = true;
      await inFlightRef.current;
      return;
    }

    const targetAthleteId = athleteId;
    const targetIdentityKey = identityKey;
    setView((current) => ({
      ...current,
      athleteId: targetAthleteId,
      isRefreshing: current.isHydrated,
    }));

    const request = (async () => {
      do {
        trailingRefreshRef.current = false;
        try {
          const progress = await getXpProgress(accessToken);
          applyProgress(progress, targetAthleteId, targetIdentityKey);
          lastLoadedAtRef.current = Date.now();
        } catch {
          if (activeIdentityRef.current !== targetIdentityKey) return;
          setView((current) => ({
            ...current,
            athleteId: targetAthleteId,
            isHydrated: true,
            isRefreshing: false,
            error: "XP progress is temporarily unavailable.",
          }));
        }
      } while (
        trailingRefreshRef.current &&
        activeIdentityRef.current === targetIdentityKey
      );
    })();

    inFlightRef.current = request;
    try {
      await request;
    } finally {
      if (inFlightRef.current === request) inFlightRef.current = null;
    }
  }, [accessToken, applyProgress, athleteId, identityKey]);

  useEffect(() => {
    if (!athleteId || !accessToken) return;
    // Only the explicit authenticated app/session start records activity.
    // Progress polling, focus refreshes and widgets remain read-only.
    void recordAppActivity(accessToken).then(load, load);

    const refresh = () => void load();
    const passiveRefresh = () => {
      if (document.visibilityState !== "visible") return;
      if (inFlightRef.current) return;
      if (Date.now() - lastLoadedAtRef.current < XP_PASSIVE_REFRESH_MIN_INTERVAL_MS) return;
      void load();
    };
    const handleVisibility = () => passiveRefresh();
    window.addEventListener(XP_REFRESH_EVENT, refresh);
    window.addEventListener("focus", passiveRefresh);
    document.addEventListener("visibilitychange", handleVisibility);
    const interval = window.setInterval(passiveRefresh, XP_PROGRESS_POLL_MS);

    return () => {
      window.removeEventListener(XP_REFRESH_EVENT, refresh);
      window.removeEventListener("focus", passiveRefresh);
      document.removeEventListener("visibilitychange", handleVisibility);
      window.clearInterval(interval);
    };
  }, [accessToken, athleteId, load]);

  const dismissFeedback = useCallback(() => {
    setView((current) => ({ ...current, feedback: null }));
  }, []);

  const visibleView =
    view.athleteId === (athleteId || null)
      ? view
      : { ...initialViewState(), athleteId: athleteId || null, isHydrated: !athleteId };

  return (
    <XpContext.Provider
      value={{
        progress: visibleView.progress,
        isHydrated: visibleView.isHydrated,
        isRefreshing: visibleView.isRefreshing,
        error: visibleView.error,
        feedback: visibleView.feedback,
        refresh: load,
        dismissFeedback,
      }}
    >
      {children}
    </XpContext.Provider>
  );
}

export function useXp(): XpContextValue {
  const context = useContext(XpContext);
  if (!context) throw new Error("useXp must be used within XpProvider.");
  return context;
}
