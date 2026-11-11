"use client";
import { useContext } from "react";
import { AppSessionContext } from "@/components/auth-provider";
import { trackProductEvent, productEventIdentity } from "@/lib/api";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  loadSoundSettings,
  saveSoundSettings,
  timerAudio,
  type TimerSound,
  type TimerSoundSettings,
} from "@/lib/session-timer/audio";
import {
  advance,
  metPlan,
  calloutForEvents,
  createTimerState,
  crossedCues,
  cueRemainingMs,
  cueSounds,
  pause,
  resume,
  samePhaseRun,
  warningLengthMs,
  soundForEvents,
  viewAt,
  type TimerEvent,
  type TimerState,
  type TimerStep,
} from "@/lib/session-timer/engine";
import type { TimerItem } from "@/lib/session-timer/plan";
import { RoundAudioTrack, type RoundAudioStatus } from "@/lib/session-timer/round-audio";

// 2: ranged rest stays in rest until its maximum (readyAt replaced windowEndsAt).
const RUN_VERSION = 2;
const TICK_MS = 100;
const MAX_CUE_DELAY_MS = 1500;
/** The fight-ring bells still ring while the timer is minimised. */
const MINIMIZED_SOUNDS: ReadonlySet<TimerSound> = new Set(["bell", "triple_bell", "finish"]);

type SavedRun = { version: number; key: string; itemIds: string[]; state: TimerState };

function itemIds(items: TimerItem[]): string[] {
  return items.map((item) => `${item.kind}:${item.id}`);
}

/** The saved run for this session, if it still matches the plan's items. */
export function loadSavedRun(storageKey: string, items: TimerItem[]): TimerState | null {
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) return null;
    const saved = JSON.parse(raw) as SavedRun;
    const ids = itemIds(items);
    if (
      saved?.version !== RUN_VERSION ||
      saved.key !== storageKey ||
      !Array.isArray(saved.itemIds) ||
      saved.itemIds.join("|") !== ids.join("|")
    ) {
      return null;
    }
    return saved.state;
  } catch {
    return null;
  }
}

export function hasSavedRun(storageKey: string): boolean {
  try {
    return Boolean(window.localStorage.getItem(storageKey));
  } catch {
    return false;
  }
}

export function clearSavedRun(storageKey: string): void {
  try {
    window.localStorage.removeItem(storageKey);
  } catch {
    // Nothing to clear.
  }
}

function saveRun(storageKey: string, state: TimerState): void {
  try {
    const run: SavedRun = {
      version: RUN_VERSION,
      key: storageKey,
      itemIds: itemIds(state.items),
      state,
    };
    window.localStorage.setItem(storageKey, JSON.stringify(run));
  } catch {
    // Resume is a convenience; the live timer does not depend on storage.
  }
}

type WakeLockSentinelLike = { released?: boolean; release: () => Promise<void> };
type WakeLockNavigator = Navigator & {
  wakeLock?: { request: (type: "screen") => Promise<WakeLockSentinelLike> };
};

/** Keep the screen on while `active`, re-acquiring after the tab returns. */
function useWakeLock(active: boolean): void {
  useEffect(() => {
    if (!active) return;
    const nav = navigator as WakeLockNavigator;
    if (!nav.wakeLock) return;
    let sentinel: WakeLockSentinelLike | null = null;
    let cancelled = false;
    let pending = false;
    const acquire = () => {
      if (cancelled || pending || (sentinel && !sentinel.released) || document.visibilityState !== "visible") return;
      pending = true;
      nav.wakeLock
        ?.request("screen")
        .then((lock) => {
          if (cancelled) {
            void lock.release();
          } else {
            sentinel = lock;
          }
        })
        .catch(() => {
          // Denied (battery saver, unsupported): the timer still runs.
        }).finally(() => { pending = false; });
    };
    acquire();
    document.addEventListener("visibilitychange", acquire);
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", acquire);
      void sentinel?.release().catch(() => undefined);
    };
  }, [active]);
}

export type SessionTimerController = {
  state: TimerState;
  now: number;
  settings: TimerSoundSettings;
  setSettings: (settings: TimerSoundSettings) => void;
  roundAudioStatus: RoundAudioStatus;
  restoreAudio: () => void;
  /** Apply an engine action that may emit events (plays their sound). */
  run: (action: (state: TimerState, now: number) => TimerStep) => void;
  /** Apply a silent engine update. */
  update: (action: (state: TimerState, now: number) => TimerState) => void;
};

/**
 * Drives the pure timer engine: ticks it, plays sounds for its events and
 * warning cues, keeps the screen awake and saves the run so Resume restores
 * the exact round or set.
 */
export function useSessionTimer({
  items,
  storageKey,
  audible,
  keepFinishedRun = false,
}: {
  items: TimerItem[];
  storageKey: string;
  /** False while the timer is minimised: the clock keeps running, and only the round bells, warnings and voice sound. */
  audible: boolean;
  /**
   * Keep a finished run saved until its owner clears it (after logging what it
   * counted), so closing the app in between never loses the work.
   */
  keepFinishedRun?: boolean;
}): SessionTimerController {
  const analyticsSession = useContext(AppSessionContext);
  const analyticsToken = analyticsSession?.session?.access_token;
  const [state, setState] = useState<TimerState>(
    () => loadSavedRun(storageKey, items) ?? createTimerState(items),
  );
  const [now, setNow] = useState(() => Date.now());
  const [settings, setSettingsState] = useState<TimerSoundSettings>(() => loadSoundSettings());
  const [roundAudioStatus, setRoundAudioStatus] = useState<RoundAudioStatus>("idle");
  const [roundAudio] = useState(() => new RoundAudioTrack(setRoundAudioStatus));
  const stateRef = useRef(state);
  const audibleRef = useRef(audible);
  const settingsRef = useRef(settings);
  const lastRemainingRef = useRef<number | null>(null);
  const lastTickAtRef = useRef<number | null>(null);

  useEffect(() => {
    roundAudio.sync(stateRef.current, Date.now(), settingsRef.current);
    return () => roundAudio.stop();
  }, [roundAudio]);

  useEffect(() => {
    audibleRef.current = audible;
  }, [audible]);

  useEffect(() => {
    settingsRef.current = settings;
    timerAudio().configure(settings);
  }, [settings]);

  const announce = useCallback((events: TimerEvent[], next: TimerState, scheduled = false) => {
    if (events.length === 0) return;
    const sound = soundForEvents(events);
    // Minimised, the round bells still ring and the voice (when it is on)
    // still calls the rounds, so the athlete never has to look; the other
    // beeps and chimes stay quiet.
    if (sound && (audibleRef.current || MINIMIZED_SOUNDS.has(sound))) {
      const at = Date.now();
      if (scheduled && roundAudio.coversCue(sound, at)) {
        timerAudio().buzzSound(sound);
      } else {
        if (scheduled) roundAudio.abandonStalledCue(sound, at);
        timerAudio().play(sound);
      }
    }
    const callout = calloutForEvents(events, next);
    if (callout) timerAudio().say(callout);
  }, [roundAudio]);

  const commit = useCallback(
    (next: TimerState) => {
      // Within the same round or rest (−10s, +10s, a new length) the next tick
      // compares against the time before the edit, so a jump past a warning
      // mark still sounds it. A new phase starts counting fresh.
      if (!samePhaseRun(stateRef.current, next)) {
        lastRemainingRef.current = cueRemainingMs(viewAt(next, Date.now()));
      }
      const previous = stateRef.current;
      if (storageKey === "unlxck.session-timer.free" && analyticsToken && next.startedAt !== null) {
        const emit = async (name: string, suffix: string) => {
          const flow = await productEventIdentity(`${storageKey}:${next.startedAt}`);
          await trackProductEvent(analyticsToken, name, "timer", await productEventIdentity(`${flow}:${suffix}`), flow);
        };
        if (previous.startedAt === null) void emit("timer_started", "start").catch(() => {});
        next.completed.forEach((count, index) => {
          for (let round = (previous.completed[index] ?? 0) + 1; round <= count; round++) {
            void emit("timer_round_completed", `${index}:${round}`).catch(() => {});
          }
        });
        if (next.endedAt !== null && previous.endedAt === null) {
          void emit(metPlan(next) ? "timer_completed" : "timer_exited", "end").catch(() => {});
        }
      }
      stateRef.current = next;
      setState(next);
      if (next.phase === "done" && next.endedAt !== null && !keepFinishedRun) {
        clearSavedRun(storageKey);
      } else {
        saveRun(storageKey, next);
      }
    },
    [storageKey, keepFinishedRun, analyticsToken],
  );

  // History can arrive after opening or resuming the timer. Refresh suggestions
  // only: recorded drafts, confirmations and adjusted timer targets stay intact.
  useEffect(() => {
    const current = stateRef.current;
    let changed = false;
    const refreshed = current.items.map(item => {
      if (item.kind !== "sets") return item;
      const incoming = items.find(candidate => candidate.id === item.id);
      if (incoming?.kind !== "sets" || JSON.stringify([incoming.suggestedLoads, incoming.weightRecommendation]) === JSON.stringify([item.suggestedLoads, item.weightRecommendation])) return item;
      changed = true;
      return { ...item, suggestedLoads: incoming.suggestedLoads, weightRecommendation: incoming.weightRecommendation };
    });
    if (changed) commit({ ...current, items: refreshed });
  }, [items, commit]);

  const run = useCallback(
    (action: (current: TimerState, at: number) => TimerStep) => {
      const at = Date.now();
      const { state: next, events } = action(stateRef.current, at);
      if (next === stateRef.current) return;
      announce(events, next);
      commit(next);
      roundAudio.sync(next, at, settingsRef.current);
      setNow(at);
    },
    [announce, commit, roundAudio],
  );

  const update = useCallback(
    (action: (current: TimerState, at: number) => TimerState) => {
      const at = Date.now();
      const next = action(stateRef.current, at);
      if (next === stateRef.current) return;
      commit(next);
      roundAudio.sync(next, at, settingsRef.current);
      setNow(at);
    },
    [commit, roundAudio],
  );

  // Connect native audio pause/play events after render, without accessing a
  // React ref in render or changing the timer during a React state update.
  useEffect(() => {
    roundAudio.setMediaActions(() => update(pause), () => update(resume));
    return () => roundAudio.setMediaActions();
  }, [roundAudio, update]);

  const running =
    (state.phase === "work" || state.phase === "rest") && state.pausedAt === null;

  useEffect(() => {
    if (!("mediaSession" in navigator) || (state.phase !== "work" && state.phase !== "rest")) return;
    const mediaSession = navigator.mediaSession;
    if (typeof MediaMetadata !== "undefined") {
      mediaSession.metadata = new MediaMetadata({ title: "UNLXCK Round Timer", artist: "UNLXCK" });
    }
    const actions: Array<["pause" | "play", () => void]> = [
      ["pause", () => update(pause)],
      ["play", () => update(resume)],
    ];
    for (const [name, handler] of actions) {
      try { mediaSession.setActionHandler(name, handler); } catch { /* Unsupported action */ }
    }
    return () => {
      for (const [name] of actions) {
        try { mediaSession.setActionHandler(name, null); } catch { /* Unsupported action */ }
      }
      mediaSession.playbackState = "none";
    };
  }, [state.phase, update]);

  useEffect(() => {
    if (!("mediaSession" in navigator)) return;
    if (state.phase === "work" || state.phase === "rest") {
      navigator.mediaSession.playbackState = state.pausedAt === null ? "playing" : "paused";
    }
  }, [state.phase, state.pausedAt]);

  useEffect(() => {
    if (!running) return;
    const tick = () => {
      const at = Date.now();
      const delayed = lastTickAtRef.current !== null && at - lastTickAtRef.current > MAX_CUE_DELAY_MS;
      lastTickAtRef.current = at;
      const current = stateRef.current;
      const { state: next, events } = advance(current, at);
      if (next !== current) {
        // Never play a batch of old bells/callouts after the browser wakes.
        const transitionAt = next.endedAt ?? next.phaseStartedAt;
        if (transitionAt !== null && at - transitionAt < MAX_CUE_DELAY_MS) announce(events, next, true);
        commit(next);
      } else {
        const remaining = cueRemainingMs(viewAt(current, at));
        // A long callback gap must not replay old warnings, voice or vibration
        // within the same round. Limit that catch-up window to recent marks;
        // normal ticks still honour deliberate time edits that cross a mark.
        const previous = lastRemainingRef.current;
        const cueBaseline = delayed && previous !== null && remaining !== null
          ? Math.min(previous, remaining + MAX_CUE_DELAY_MS)
          : previous;
        const { warnTen, warnThirty, warnHalfway } = settingsRef.current;
        const cues = crossedCues(
          current.phase,
          current.phaseMs,
          cueBaseline,
          remaining,
          { ten: warnTen, thirty: warnThirty, halfway: warnHalfway },
          warningLengthMs(current),
        );
        lastRemainingRef.current = remaining;
        // The round warnings ring and speak minimised too: the athlete is
        // training, not looking at the phone. Only the 3-2-1 beeps stay on screen.
        const { sounds, callout } = cueSounds(cues, audibleRef.current);
        const audio = timerAudio();
        for (const sound of sounds) {
          if (sound !== "beep" && roundAudio.coversCue(sound, at)) {
            audio.buzzSound(sound);
          } else {
            if (sound !== "beep") roundAudio.abandonStalledCue(sound, at);
            audio.play(sound);
          }
        }
        if (callout) audio.say(callout);
      }
      setNow(at);
    };
    tick();
    const interval = window.setInterval(tick, TICK_MS);
    // A returning tab catches up at once instead of waiting for the next tick.
    document.addEventListener("visibilitychange", tick);
    window.addEventListener("pageshow", tick);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", tick);
      window.removeEventListener("pageshow", tick);
    };
  }, [running, announce, commit, roundAudio]);

  // In-app minimising must not let a running round's screen go to sleep.
  useWakeLock(running || (audible && state.phase !== "done"));

  const setSettings = useCallback((next: TimerSoundSettings) => {
    settingsRef.current = next;
    timerAudio().configure(next);
    setSettingsState(next);
    saveSoundSettings(next);
    roundAudio.sync(stateRef.current, Date.now(), next);
  }, [roundAudio]);

  const restoreAudio = useCallback(() => {
    timerAudio().unlock();
    roundAudio.sync(stateRef.current, Date.now(), settingsRef.current);
  }, [roundAudio]);

  return { state, now, settings, setSettings, roundAudioStatus, restoreAudio, run, update };
}
