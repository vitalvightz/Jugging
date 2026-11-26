import { countNoun, type IntervalItem, type SetsItem, type TimerItem } from "./plan";
import type { SetDraft } from "../set-entries";

/**
 * Session timer state machine.
 *
 * Every phase is anchored to wall-clock timestamps rather than a decrementing
 * counter, so a locked phone, a backgrounded tab or a throttled interval never
 * makes the clock drift: the next `advance` simply replays whatever transitions
 * fell due while nothing was ticking.
 *
 * Phases:
 * - ready: an exercise is queued; the athlete taps Start.
 * - work:  a round, a timed hold, or an untimed set the athlete ends with "Set done".
 * - rest:  between rounds or sets. Untimed only when the plan gave no rest.
 *          A ranged rest (90–120 s) stays in rest until its maximum: at the
 *          minimum it only signals "ready", and the next set begins when the
 *          athlete taps Start set or the maximum runs out, never earlier.
 * - done:  every item finished.
 */
export type TimerPhase = "ready" | "work" | "rest" | "done";

export type TimerState = {
  items: TimerItem[];
  index: number;
  phase: TimerPhase;
  /** 1-based round or set currently in progress (or next up during rest). */
  unit: number;
  /** Rounds / sets completed per item. */
  completed: number[];
  /** Set drafts survive minimising, refresh and the final handoff. */
  setDrafts?: Record<string, SetDraft[]>;
  /** Completed work is counted separately from confirmed measurements. */
  confirmedSetLogs?: Record<string, number[]>;
  /** Explicitly reviewed without reporting measurements. */
  unreportedSetLogs?: Record<string, number[]>;
  pendingSetLog?: { itemId: string; set: number } | null;
  phaseStartedAt: number | null;
  /** Length of a timed phase; null for an untimed one. */
  phaseMs: number | null;
  /** Ranged rest only: when the minimum rest is reached, until announced. */
  readyAt: number | null;
  /** A get-ready countdown (timed as a rest) before a round or hold starts. */
  prep?: boolean;
  pausedAt: number | null;
  startedAt: number | null;
  endedAt: number | null;
};

export type TimerEvent =
  | "round_start"
  | "round_end"
  | "rest_start"
  | "hold_start"
  | "hold_end"
  | "set_start"
  | "rest_ready"
  | "rest_end"
  | "prep_start"
  | "item_complete"
  | "session_complete";

export type TimerStep = { state: TimerState; events: TimerEvent[] };

export function createTimerState(items: TimerItem[]): TimerState {
  return {
    items,
    index: 0,
    phase: items.length ? "ready" : "done",
    unit: 1,
    completed: items.map(() => 0),
    phaseStartedAt: null,
    phaseMs: null,
    readyAt: null,
    pausedAt: null,
    startedAt: null,
    endedAt: null,
  };
}

export function currentItem(state: TimerState): TimerItem | null {
  return state.items[state.index] ?? null;
}

function setPhase(
  state: TimerState,
  phase: TimerPhase,
  at: number,
  phaseMs: number | null,
  patch: Partial<TimerState> = {},
): TimerState {
  return { ...state, phase, phaseStartedAt: at, phaseMs, readyAt: null, prep: false, ...patch };
}

function bumpCompleted(state: TimerState): number[] {
  return state.completed.map((count, index) => (index === state.index ? count + 1 : count));
}

function completeItem(state: TimerState, at: number, events: TimerEvent[]): TimerState {
  if (state.index + 1 < state.items.length) {
    events.push("item_complete");
    return {
      ...state,
      index: state.index + 1,
      phase: "ready",
      unit: 1,
      phaseStartedAt: null,
      phaseMs: null,
      readyAt: null,
    };
  }
  events.push("session_complete");
  return {
    ...state,
    phase: "done",
    phaseStartedAt: null,
    phaseMs: null,
    readyAt: null,
    endedAt: at,
  };
}

function beginWork(state: TimerState, at: number, events: TimerEvent[]): TimerState {
  const item = currentItem(state);
  if (!item) {
    return state;
  }
  if (item.kind === "interval") {
    events.push("round_start");
    return setPhase(state, "work", at, item.workSec * 1000);
  }
  if (item.kind === "sets" && item.holdSec) {
    events.push("hold_start");
    return setPhase(state, "work", at, item.holdSec * 1000);
  }
  events.push("set_start");
  return setPhase(state, "work", at, null);
}

/** Seconds of get-ready before every isometric hold, so the athlete can set up. */
export const HOLD_PREP_SEC = 5;

function isHold(item: TimerItem | null): boolean {
  return item?.kind === "sets" && !!item.holdSec;
}

/** Lead into a hold with its get-ready countdown. */
function beginHoldPrep(state: TimerState, at: number, events: TimerEvent[]): TimerState {
  events.push("prep_start");
  return setPhase(state, "rest", at, HOLD_PREP_SEC * 1000, { prep: true });
}

/**
 * The ready screen's Start: begins the queued exercise. Rounds can open with a
 * prep countdown (`prepSec`), timed as a rest before round 1 so the 3-2-1 beeps
 * and the round bell lead into it like any other round.
 */
export function startItem(state: TimerState, now: number, options: { prepSec?: number } = {}): TimerStep {
  if (state.phase !== "ready" || state.pausedAt !== null) {
    return { state, events: [] };
  }
  const events: TimerEvent[] = [];
  const started = { ...state, unit: 1, startedAt: state.startedAt ?? now };
  const prepSec = options.prepSec ?? 0;
  if (currentItem(state)?.kind === "interval" && prepSec > 0) {
    events.push("prep_start");
    return { state: setPhase(started, "rest", now, prepSec * 1000, { prep: true }), events };
  }
  if (isHold(currentItem(state))) {
    return { state: beginHoldPrep(started, now, events), events };
  }
  return { state: beginWork(started, now, events), events };
}

/** A get-ready countdown: before round 1 of an interval, or before a hold. */
export function isPrep(state: TimerState): boolean {
  if (state.phase === "rest" && state.prep) return true;
  return (
    state.phase === "rest" &&
    currentItem(state)?.kind === "interval" &&
    state.unit === 1 &&
    (state.completed[state.index] ?? 0) === 0
  );
}

/** A finished set (tapped, or a hold that ran out) moves to rest or the next item. */
function finishSet(state: TimerState, at: number, events: TimerEvent[]): TimerState {
  const item = currentItem(state);
  if (!item || item.kind !== "sets") {
    return state;
  }
  const completed = bumpCompleted(state);
  const done = completed[state.index];
  const withCount = { ...state, completed };
  if (item.sets && done >= item.sets.max) {
    return completeItem(withCount, at, events);
  }
  events.push("rest_start");
  const rest = item.restSec;
  return setPhase(withCount, "rest", at, rest ? rest.max * 1000 : null, {
    unit: done + 1,
    readyAt: rest && rest.max > rest.min ? at + rest.min * 1000 : null,
  });
}

/** One timed phase reaching zero at `at`. */
function expirePhase(state: TimerState, at: number, events: TimerEvent[]): TimerState {
  const item = currentItem(state);
  if (!item) {
    return state;
  }
  if (item.kind === "interval") {
    if (state.phase === "work") {
      const completed = bumpCompleted(state);
      events.push("round_end");
      const withCount = { ...state, completed };
      if (completed[state.index] >= item.rounds) {
        return completeItem(withCount, at, events);
      }
      if (item.restSec > 0) {
        events.push("rest_start");
        return setPhase(withCount, "rest", at, item.restSec * 1000, { unit: state.unit + 1 });
      }
      return beginWork({ ...withCount, unit: state.unit + 1 }, at, events);
    }
    return beginWork(state, at, events);
  }
  if (item.kind === "sets") {
    if (state.phase === "work") {
      events.push("hold_end");
      return finishSet(state, at, events);
    }
    events.push("rest_end");
    return beginWork(state, at, events);
  }
  return state;
}

export function phaseEndsAt(state: TimerState): number | null {
  if (state.phaseStartedAt === null || state.phaseMs === null) {
    return null;
  }
  return state.phaseStartedAt + state.phaseMs;
}

/** Replay every transition due by `now`. A paused timer never advances. */
export function advance(state: TimerState, now: number): TimerStep {
  if (state.pausedAt !== null || state.phase === "ready" || state.phase === "done") {
    return { state, events: [] };
  }
  const events: TimerEvent[] = [];
  let next = state;
  // Bounded so a corrupt persisted state can never spin forever.
  for (let guard = 0; guard < 500; guard += 1) {
    // The minimum of a ranged rest always falls before its maximum.
    if (next.readyAt !== null && now >= next.readyAt) {
      events.push("rest_ready");
      next = { ...next, readyAt: null };
      continue;
    }
    const endsAt = phaseEndsAt(next);
    if (endsAt !== null && now >= endsAt && (next.phase === "work" || next.phase === "rest")) {
      next = expirePhase(next, endsAt, events);
      continue;
    }
    break;
  }
  return { state: next, events };
}

/** "Set done" on a rep set, or ending a hold early. */
export function completeSet(state: TimerState, now: number): TimerStep {
  const item = currentItem(state);
  if (state.phase !== "work" || state.pausedAt !== null || item?.kind !== "sets") {
    return { state, events: [] };
  }
  const events: TimerEvent[] = [];
  return { state: finishSet(state, now, events), events };
}

/**
 * Start the next set without waiting out the rest (or from an untimed rest).
 * A ranged rest's minimum is binding: before it, the tap is refused.
 */
export function skipRest(state: TimerState, now: number): TimerStep {
  if (
    state.phase !== "rest" ||
    state.pausedAt !== null ||
    (state.readyAt !== null && now < state.readyAt)
  ) {
    return { state, events: [] };
  }
  const events: TimerEvent[] = [];
  // Cutting a rest short still leaves the hold's get-ready countdown.
  if (isHold(currentItem(state)) && !isPrep(state)) {
    return { state: beginHoldPrep(state, now, events), events };
  }
  return { state: beginWork(state, now, events), events };
}

/** End the current round now; it still counts as a round done. */
export function endRound(state: TimerState, now: number): TimerStep {
  const item = currentItem(state);
  if (state.phase !== "work" || state.pausedAt !== null || item?.kind !== "interval") {
    return { state, events: [] };
  }
  const events: TimerEvent[] = [];
  return { state: expirePhase(state, now, events), events };
}

/** Finish the current exercise (task done, set range met, or moving on early). */
export function finishItem(state: TimerState, now: number): TimerStep {
  if (state.phase === "done") {
    return { state, events: [] };
  }
  const item = currentItem(state);
  const events: TimerEvent[] = [];
  let next = { ...state, pausedAt: null };
  if (item?.kind === "task" && next.completed[next.index] === 0) {
    next = { ...next, completed: bumpCompleted(next) };
  }
  return { state: completeItem(next, now, events), events };
}

/**
 * The plan's count for the current exercise: what "done as planned" fills in.
 * The plan's own number when the item carries it, else the timer's target.
 */
export function plannedTarget(item: TimerItem): number {
  if (item.planned) return item.planned.min;
  if (item.kind === "interval") return item.rounds;
  if (item.kind === "sets") return item.sets?.min ?? 1;
  return 1;
}

/**
 * Mark the current exercise done as planned and move on, for work done
 * without running the clock: its count is filled up to the plan.
 */
export function finishItemAsPlanned(state: TimerState, now: number): TimerStep {
  const item = currentItem(state);
  if (!item || state.phase === "done") {
    return { state, events: [] };
  }
  const completed = [...state.completed];
  completed[state.index] = Math.max(completed[state.index] ?? 0, plannedTarget(item));
  const events: TimerEvent[] = [];
  return { state: completeItem({ ...state, completed, pausedAt: null }, now, events), events };
}

/** End the whole session from anywhere. */
export function endSession(state: TimerState, now: number): TimerState {
  return {
    ...state,
    phase: "done",
    phaseStartedAt: null,
    phaseMs: null,
    readyAt: null,
    pausedAt: null,
    endedAt: now,
  };
}

/** Give an untimed rest a length (the rest presets when the plan had none). */
export function setRestLength(state: TimerState, seconds: number, now: number): TimerState {
  if (state.phase !== "rest" || seconds <= 0) {
    return state;
  }
  return { ...state, phaseStartedAt: state.pausedAt ?? now, phaseMs: seconds * 1000 };
}

/** Add (or, negative, take) time from the running phase; at least a second is always left. */
export function addTime(state: TimerState, seconds: number, now: number): TimerState {
  if (state.phaseMs === null || (state.phase !== "work" && state.phase !== "rest")) {
    return state;
  }
  const elapsed = state.phaseStartedAt === null ? 0 : Math.max(0, (state.pausedAt ?? now) - state.phaseStartedAt);
  // Taking time off never adds any back, even with under a second to go.
  const phaseMs = Math.max(state.phaseMs + seconds * 1000, Math.min(state.phaseMs, elapsed + 1000));
  return phaseMs === state.phaseMs ? state : { ...state, phaseMs };
}

export function pause(state: TimerState, now: number): TimerState {
  if (state.pausedAt !== null || (state.phase !== "work" && state.phase !== "rest")) {
    return state;
  }
  return { ...state, pausedAt: now };
}

export function resume(state: TimerState, now: number): TimerState {
  if (state.pausedAt === null) {
    return state;
  }
  const pausedFor = Math.max(0, now - state.pausedAt);
  return {
    ...state,
    pausedAt: null,
    phaseStartedAt: state.phaseStartedAt === null ? null : state.phaseStartedAt + pausedFor,
    readyAt: state.readyAt === null ? null : state.readyAt + pausedFor,
  };
}

export type ItemAdjustment = {
  rounds?: number;
  workSec?: number;
  /** Rest between rounds, or between sets (a set rest becomes exact). */
  restSec?: number;
  /** Target set count (becomes exact). */
  sets?: number;
};

/**
 * Adjust the current exercise at any point: before it starts, or mid-run when
 * the coach changes the round length, rest or target. A change to the phase
 * that is running applies to it at once.
 *
 * Counts are reconciled with the work already done, so the state never shows
 * an impossible target ("set 3 of 2") and never runs work past the target:
 * - during a round or set, the target cannot drop below the one in progress;
 * - while resting, lowering the target to what is already done completes the
 *   exercise instead of starting another round or set.
 */
export function adjustItem(state: TimerState, patch: ItemAdjustment, now: number): TimerStep {
  const item = currentItem(state);
  if (!item || state.phase === "done" || item.kind === "task") {
    return { state, events: [] };
  }
  const done = state.completed[state.index] ?? 0;
  const inProgress = state.phase === "work" ? state.unit : 0;
  const floor = Math.max(1, done, inProgress);
  const replace = (updated: TimerItem): TimerState => ({
    ...state,
    items: state.items.map((entry, index) => (index === state.index ? updated : entry)),
  });
  const finishIfMet = (next: TimerState, target: number): TimerStep => {
    if (next.phase === "rest" && done >= target) {
      const events: TimerEvent[] = [];
      return { state: completeItem({ ...next, pausedAt: null }, now, events), events };
    }
    return { state: next, events: [] };
  };

  if (item.kind === "interval") {
    const updated: IntervalItem = {
      ...item,
      rounds: Math.min(30, Math.max(floor, patch.rounds ?? item.rounds)),
      workSec: Math.min(3600, Math.max(5, patch.workSec ?? item.workSec)),
      restSec: Math.min(1800, Math.max(0, patch.restSec ?? item.restSec)),
      needsSetup: false,
    };
    let next = replace(updated);
    if (state.phase === "work" && patch.workSec !== undefined) {
      next = { ...next, phaseMs: updated.workSec * 1000 };
    }
    // The prep countdown is not a between-rounds rest: a new rest length leaves it alone.
    if (state.phase === "rest" && patch.restSec !== undefined && !isPrep(state)) {
      next = { ...next, phaseMs: updated.restSec * 1000 };
    }
    return finishIfMet(next, updated.rounds);
  }

  const restSec =
    patch.restSec !== undefined ? Math.min(900, Math.max(15, patch.restSec)) : null;
  const sets = patch.sets !== undefined ? Math.min(20, Math.max(floor, patch.sets)) : null;
  const updated: SetsItem = {
    ...item,
    restSec: restSec !== null ? { min: restSec, max: restSec } : item.restSec,
    sets: sets !== null ? { min: sets, max: sets } : item.sets,
  };
  let next = replace(updated);
  if (state.phase === "rest" && restSec !== null && !isPrep(state)) {
    // The rest is now exact: no separate minimum left to wait for.
    next = { ...next, phaseMs: restSec * 1000, readyAt: null };
  }
  return sets !== null ? finishIfMet(next, sets) : { state: next, events: [] };
}

/** Seconds between the adjust steppers' duration steps. */
export const DURATION_STEP_SEC = 15;

/**
 * The next duration mark up or down, so a step always lands on a round
 * number: 1:05 steps to 1:15 or 1:00, never 1:20 or 0:50.
 */
export function stepDuration(seconds: number, direction: 1 | -1, step: number = DURATION_STEP_SEC): number {
  return direction > 0
    ? Math.floor(seconds / step) * step + step
    : Math.ceil(seconds / step) * step - step;
}

/** Ready-screen round presets and legacy callers: same rules as adjustItem. */
export function updateIntervalItem(
  state: TimerState,
  patch: Partial<Pick<IntervalItem, "rounds" | "workSec" | "restSec">>,
): TimerState {
  // Only used on the ready screen, where nothing is done yet and no clock runs.
  return currentItem(state)?.kind === "interval" ? adjustItem(state, patch, Date.now()).state : state;
}

export type TimerView = {
  /** Time left in a timed phase; null when the phase is untimed. */
  remainingMs: number | null;
  /** Time spent in the current phase. */
  elapsedMs: number;
  /** Ranged rest only: time left until the minimum rest (0 once ready). */
  readyRemainingMs: number | null;
  paused: boolean;
};

export function viewAt(state: TimerState, now: number): TimerView {
  const clock = state.pausedAt ?? now;
  const endsAt = phaseEndsAt(state);
  const item = currentItem(state);
  const rest = item?.kind === "sets" ? item.restSec : null;
  const ranged = state.phase === "rest" && !state.prep && rest !== null && rest.max > rest.min;
  const elapsedMs = state.phaseStartedAt === null ? 0 : Math.max(0, clock - state.phaseStartedAt);
  return {
    remainingMs: endsAt === null ? null : Math.max(0, endsAt - clock),
    elapsedMs,
    readyRemainingMs: ranged && rest ? Math.max(0, rest.min * 1000 - elapsedMs) : null,
    paused: state.pausedAt !== null,
  };
}

/**
 * The countdown the warning cues lead into: the minimum of a ranged rest
 * while it is still ahead (that is when the athlete is ready), otherwise the
 * end of the phase.
 */
export function cueRemainingMs(view: TimerView): number | null {
  return view.readyRemainingMs !== null && view.readyRemainingMs > 0
    ? view.readyRemainingMs
    : view.remainingMs;
}

/**
 * Whether `next` is still the same round, set or rest as `previous` (an edit
 * such as −10s, +10s, pause or a new length, not a transition). Warning cues
 * keep counting across such an edit, so taking time off past a mark still
 * plays that mark's warning; a new phase starts its count fresh.
 */
export function samePhaseRun(previous: TimerState, next: TimerState): boolean {
  return (
    previous.index === next.index &&
    previous.phase === next.phase &&
    previous.unit === next.unit &&
    previous.readyAt === next.readyAt
  );
}

export type TimerCue = "halfway" | "thirty_seconds" | "ten_seconds" | "count_3" | "count_2" | "count_1";

/** Which of the optional round warnings the athlete wants. */
export type WarningOptions = { ten: boolean; thirty: boolean; halfway: boolean };

export const DEFAULT_WARNINGS: WarningOptions = { ten: true, thirty: false, halfway: false };

/**
 * The length a round's warnings are judged by: the round as set, not as
 * trimmed. Taking time off a 3:00 round never makes it a "short round" that
 * loses its warnings.
 */
export function warningLengthMs(state: TimerState): number | null {
  const item = currentItem(state);
  if (state.phaseMs === null) return null;
  return state.phase === "work" && item?.kind === "interval"
    ? Math.max(state.phaseMs, item.workSec * 1000)
    : state.phaseMs;
}

/**
 * Warning cues crossed between two ticks of the same phase. Each round warning
 * only fires on a round long enough for it to be a warning (the ten-second
 * clapper from 30 s, the 30-second and halfway cues from a minute), judged by
 * `lengthMs` (see warningLengthMs), and the 3-2-1 count only leads into work
 * (the end of a rest). Halfway is half of the phase as it now runs.
 */
export function crossedCues(
  phase: TimerPhase,
  phaseMs: number | null,
  previousRemainingMs: number | null,
  remainingMs: number | null,
  warnings: WarningOptions = DEFAULT_WARNINGS,
  lengthMs: number | null = phaseMs,
): TimerCue[] {
  if (previousRemainingMs === null || remainingMs === null || phaseMs === null || lengthMs === null) {
    return [];
  }
  const crossed = (mark: number) => previousRemainingMs > mark && remainingMs <= mark && remainingMs > 0;
  const cues: TimerCue[] = [];
  if (phase === "work" && warnings.halfway && lengthMs >= 60_000 && crossed(phaseMs / 2)) {
    cues.push("halfway");
  }
  if (phase === "work" && warnings.thirty && lengthMs >= 60_000 && crossed(30_000)) {
    cues.push("thirty_seconds");
  }
  if (phase === "work" && warnings.ten && lengthMs >= 30_000 && crossed(10_000)) {
    cues.push("ten_seconds");
  }
  if (phase === "rest") {
    if (crossed(3_000)) cues.push("count_3");
    if (crossed(2_000)) cues.push("count_2");
    if (crossed(1_000)) cues.push("count_1");
  }
  return cues;
}

/**
 * What a tick's warning cues sound like. Each round warning is its tone plus
 * a spoken callout in the same voice as "Round 3" / "Last round" ("Halfway",
 * "30 seconds", "10 seconds"; spoken only when voice callouts are on). The
 * warnings ring and speak whether or not the timer is on screen; only the
 * 3-2-1 beeps are on-screen only (`onScreen`). A one-minute round's halfway is
 * its thirty-second mark: one cue, not two, and one callout per tick.
 */
export function cueSounds(
  cues: TimerCue[],
  onScreen: boolean,
): { sounds: Array<"clapper" | "double_beep" | "halfway" | "beep">; callout: string | null } {
  const sounds: Array<"clapper" | "double_beep" | "halfway" | "beep"> = [];
  let callout: string | null = null;
  if (cues.includes("thirty_seconds")) {
    sounds.push("double_beep");
    callout = "30 seconds";
  } else if (cues.includes("halfway")) {
    sounds.push("halfway");
    callout = "Halfway";
  }
  if (cues.includes("ten_seconds")) {
    sounds.push("clapper");
    callout = "10 seconds";
  }
  if (onScreen && cues.some((cue) => cue.startsWith("count_"))) sounds.push("beep");
  return { sounds, callout };
}

/** True when every item reached its planned minimum. */
export function metPlan(state: TimerState): boolean {
  return state.items.every((item, index) => {
    const done = state.completed[index] ?? 0;
    if (item.kind === "interval") return done >= item.rounds;
    if (item.kind === "sets") return item.sets ? done >= item.sets.min : done > 0;
    return done > 0;
  });
}

/** A short plain-text record of what the timer logged, for the session notes. */
export function summarizeRun(state: TimerState): string {
  const lines = state.items.flatMap((item, index) => {
    const done = state.completed[index] ?? 0;
    if (item.kind === "interval") {
      return done > 0 ? [`${item.title}: ${done}/${item.rounds} rounds`] : [];
    }
    if (item.kind === "sets") {
      if (done === 0) return [];
      const noun = countNoun(item);
      if (item.sets && item.sets.min === item.sets.max) {
        return [`${item.title}: ${done}/${item.sets.min} ${noun}s`];
      }
      const plan = item.sets ? ` (plan ${item.sets.min}–${item.sets.max})` : "";
      return [`${item.title}: ${done} ${done === 1 ? noun : `${noun}s`}${plan}`];
    }
    return done > 0 ? [`${item.title}: done`] : [];
  });
  return lines.length ? `Timer: ${lines.join(" · ")}` : "";
}

/**
 * The one sound for a batch of events from a single tick. A tick can carry
 * several transitions (a round ending straight into rest, or a phone waking
 * after minutes locked), and stacking their sounds would be noise: the most
 * significant one wins.
 */
export function soundForEvents(
  events: TimerEvent[],
): "finish" | "triple_bell" | "round_start" | "go" | "chime" | "soft_chime" | null {
  if (events.includes("session_complete")) return "finish";
  if (events.includes("round_end")) return "triple_bell";
  if (events.includes("round_start")) return "round_start";
  if (events.includes("rest_end") || events.includes("hold_start")) return "go";
  if (events.includes("hold_end") || events.includes("item_complete")) return "chime";
  if (events.includes("rest_ready")) return "soft_chime";
  return null;
}

/** Optional spoken callout for the same batch of events. */
export function calloutForEvents(events: TimerEvent[], state: TimerState): string | null {
  const item = currentItem(state);
  if (events.includes("session_complete")) return "Session complete";
  if (events.includes("prep_start")) return "Get ready";
  if (events.includes("round_start") && item?.kind === "interval") {
    return state.unit >= item.rounds && item.rounds > 1 ? "Last round" : `Round ${state.unit}`;
  }
  if (events.includes("rest_start")) return "Rest";
  if (events.includes("rest_ready")) return "Ready";
  if (events.includes("item_complete") && item) return `Next: ${item.title}`;
  if (events.includes("rest_end") && item?.kind === "sets") {
    return `${countNoun(item) === "round" ? "Round" : "Set"} ${state.unit}`;
  }
  return null;
}
