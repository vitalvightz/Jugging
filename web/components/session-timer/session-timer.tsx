"use client";

import {
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { OnceHint, useSeenOnce } from "@/components/once-hint";
import { RestSetLogger, TimerSetReview } from "./rest-set-logger";
import { leaveSetUnreported, visibleSetLogTarget, pendingSetLogs, completeSetWithResults, endSessionWithResults, finishItemWithResults, finishItemAsPlannedWithResults } from "@/lib/session-timer/sets";
import { lockOverlayScroll } from "@/lib/overlay-scroll-lock";

import { useSessionTimer, type SessionTimerController } from "@/components/session-timer/use-session-timer";
import { PREP_OPTIONS, timerAudio } from "@/lib/session-timer/audio";
import {
  addTime,
  adjustItem,
  currentItem,
  endRound,
  finishItem,
  isPrep,
  metPlan,
  plannedTarget,
  pause,
  resume,
  setRestLength,
  skipRest,
  startItem,
  stepDuration,
  summarizeRun,
  updateIntervalItem,
  viewAt,
  type ItemAdjustment,
  type TimerState,
  type TimerView,
} from "@/lib/session-timer/engine";
import { rememberRoundFormat, ROUND_PRESETS } from "@/lib/session-timer/contact";
import type { SparringPlannedIntensity } from "@/lib/types";
import {
  countNoun,
  formatClock,
  formatRange,
  formatShortDuration,
  type SetsItem,
  type TimerItem,
  type TimerStat,
} from "@/lib/session-timer/plan";

export type SessionTimerSummary = {
  /** Every item reached its planned minimum. */
  complete: boolean;
  /** Plain-text record of the run for the session notes. */
  notes: string;
  /** Sparring rounds the run completed (for the sparring log), or null. */
  sparring: {
    rounds: number;
    roundSeconds: number | null;
    /** From the first sparring block's structured intensity, when stated. */
    plannedIntensity: SparringPlannedIntensity | null;
  } | null;
  /** The finished run itself, so the owner can log each exercise from it. */
  run: TimerState;
};

/** How long "Session complete" holds before the run hands off to its log. */
const HANDOFF_MS = 900;

/** Completed sparring rounds across the run's sparring items. */
export function sparringSummary(state: TimerState): SessionTimerSummary["sparring"] {
  let rounds = 0;
  let roundSeconds: number | null = null;
  let plannedIntensity: SparringPlannedIntensity | null = null;
  let hasSparring = false;
  state.items.forEach((item, index) => {
    if (item.kind === "interval" && item.sparring) {
      hasSparring = true;
      rounds += state.completed[index] ?? 0;
      roundSeconds ??= item.workSec;
      plannedIntensity ??= item.plannedIntensity ?? null;
    }
  });
  return hasSparring ? { rounds, roundSeconds, plannedIntensity } : null;
}

const REST_PRESETS = [60, 90, 120, 180];
/** The last stretch of a timed phase, when the screen turns amber. */
const WARN_MS = 10_000;

type Tone = "ready" | "work" | "rest" | "warn" | "paused" | "done" | "open";

function toneFor(state: TimerState, view: TimerView): Tone {
  if (state.phase === "done") return "done";
  if (state.phase === "ready") return "ready";
  if (view.paused) return "paused";
  if (view.remainingMs !== null && state.phaseMs !== null && state.phaseMs > WARN_MS * 2 && view.remainingMs <= WARN_MS) {
    return "warn";
  }
  if (state.phase === "rest") return "rest";
  return view.remainingMs === null ? "open" : "work";
}

function capitalize(text: string): string {
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}`;
}

/** "Set" / "Round", for a sets item's labels. */
function unitWord(item: SetsItem): string {
  return capitalize(countNoun(item));
}

/**
 * What each set or round asks for: its hold, or every target the plan gives
 * (reps / mode, time, distance), e.g. "AMRAP, 60 s". Comma-joined so the
 * parts read as one unit beside the " · " separated rest.
 */
function perUnitTarget(item: SetsItem): string | null {
  if (item.holdSec) return `${formatShortDuration(item.holdSec)} hold`;
  const targets = (item.stats ?? []).filter((stat) => stat.kind === "target").map((stat) => stat.value);
  return targets.length ? targets.join(", ") : null;
}

/** One line for an exercise's plan, e.g. "4 × 5 reps · 2 min rest" or "6 × 3:00 · 1:00 rest". */
function describeItem(item: TimerItem): string {
  if (item.kind === "interval") {
    if (item.rounds <= 1) return formatClock(item.workSec);
    const round = `${item.rounds} × ${formatClock(item.workSec)}`;
    return item.restSec > 0 ? `${round} · ${formatClock(item.restSec)} rest` : round;
  }
  if (item.kind === "sets") {
    const noun = countNoun(item);
    const target = perUnitTarget(item);
    const count = item.sets
      ? target
        ? `${formatRange(item.sets)} × ${target}`
        : `${formatRange(item.sets)} ${noun}s`
      : [target, `${capitalize(noun)}s as needed`].filter(Boolean).join(" · ");
    const rest = item.restSec ? `${formatRange(item.restSec, formatShortDuration)} rest` : null;
    return [count, rest].filter(Boolean).join(" · ");
  }
  return "Tap done when finished";
}

/** Keep saved prescriptions intact; separate familiar units only for display. */
function doseStatDisplay(stat: TimerStat): { label: string; value: string } {
  if (stat.kind === "effort") {
    const effort = /^(RPE|RIR)\s+(.+)$/i.exec(stat.value);
    return effort ? { label: effort[1].toUpperCase(), value: effort[2] } : { label: "Effort", value: stat.value };
  }
  if (stat.kind === "load") {
    return { label: /\b(?:kg|lbs?)\b/i.test(stat.value) ? "Weight" : "Load", value: stat.value };
  }
  const reps = /^(.+?)\s+reps?$/i.exec(stat.value);
  if (reps) return { label: "Reps", value: reps[1] };
  const label = /\s(?:m|km|mi|yd)\b/i.test(stat.value) ? "Distance"
    : /\s(?:s|sec|min)\b/i.test(stat.value) ? "Time" : "Target";
  return { label, value: stat.value };
}

/** One quiet prescription row; older saved runs retain their one-line detail. */
function DoseStats({ item }: { item: TimerItem }) {
  if (!item.stats) return item.detail ? <p className="st-detail">{item.detail}</p> : null;
  // A hold's time is on the clock; everything else stays in view.
  if (item.stats.length === 0) return null;
  return (
    <dl className="st-stats" aria-label="Prescription">
      {item.stats.map((stat, index) => {
        const { label, value } = doseStatDisplay(stat);
        return (
          <div key={`${stat.kind}-${index}`} data-kind={stat.kind}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        );
      })}
    </dl>
  );
}

function phaseLabel(state: TimerState, item: TimerItem | null, view: TimerView): string {
  if (state.phase === "done") return metPlan(state) ? "Session complete" : "Session ended";
  if (state.phase === "ready") return "Up next";
  if (view.paused) return "Paused";
  if (isPrep(state)) return "Get ready";
  if (state.phase === "rest") return view.readyRemainingMs === 0 ? "Ready" : "Rest";
  if (item?.kind === "interval") return item.sparring ? "Spar" : "Work";
  if (item?.kind === "sets") return item.holdSec ? "Hold" : countNoun(item) === "round" ? "Work" : "Lift";
  return "Go";
}

function counterLabel(state: TimerState, item: TimerItem | null): string | null {
  if (!item || state.phase === "ready" || state.phase === "done") return null;
  if (item.kind === "interval") {
    if (item.rounds <= 1) return null;
    const label = state.phase === "rest" && !isPrep(state) ? "Next: round" : "Round";
    return `${label} ${state.unit} / ${item.rounds}`;
  }
  if (item.kind === "sets") {
    const target = item.sets
      ? item.sets.min === item.sets.max
        ? ` of ${item.sets.min}`
        : ` · target ${formatRange(item.sets)}`
      : "";
    const noun = countNoun(item);
    return state.phase === "rest" ? `Next: ${noun} ${state.unit}${target}` : `${unitWord(item)} ${state.unit}${target}`;
  }
  return null;
}

function doneLabel(item: TimerItem, done: number): string {
  if (done === 0) return "Skipped";
  if (item.kind === "interval") return `${done}/${item.rounds} rounds`;
  if (item.kind === "sets") {
    const noun = countNoun(item);
    if (item.sets && item.sets.min === item.sets.max) return `${done}/${item.sets.min} ${noun}s`;
    const count = `${done} ${done === 1 ? noun : `${noun}s`}`;
    return item.sets ? `${count} · plan ${formatRange(item.sets)}` : count;
  }
  return "Done";
}

function Progress({ state, item }: { state: TimerState; item: TimerItem | null }) {
  if (!item || item.kind === "task") return null;
  const total = item.kind === "interval" ? item.rounds : item.sets?.max ?? null;
  if (!total || total > 20) return null;
  const done = state.completed[state.index] ?? 0;
  const min = item.kind === "sets" && item.sets ? item.sets.min : total;
  return (
    <ol className="st-segments" aria-hidden="true">
      {Array.from({ length: total }, (_, index) => (
        <li
          key={index}
          data-state={index < done ? "done" : index === done && state.phase !== "ready" ? "current" : "todo"}
          data-optional={index >= min ? "true" : undefined}
        />
      ))}
    </ol>
  );
}

/** How far through the current countdown the ring is (0..1), or null when untimed. */
function ringProgress(state: TimerState, item: TimerItem | null, view: TimerView): number | null {
  if (view.remainingMs === null || state.phaseMs === null || state.phaseMs <= 0) return null;
  const rest = item?.kind === "sets" ? item.restSec : null;
  if (view.readyRemainingMs !== null && rest && rest.max > rest.min) {
    // A ranged rest fills once to its minimum, then again across the optional window.
    const minMs = rest.min * 1000;
    return view.readyRemainingMs > 0
      ? 1 - view.readyRemainingMs / minMs
      : 1 - (view.remainingMs ?? 0) / (rest.max * 1000 - minMs);
  }
  return 1 - view.remainingMs / state.phaseMs;
}

function Ring({ progress, children }: { progress: number | null; children: ReactNode }) {
  const clamped = progress === null ? 1 : Math.min(1, Math.max(0, progress));
  return (
    <div className="st-ring" data-timed={progress === null ? "false" : "true"}>
      <svg viewBox="0 0 120 120" aria-hidden="true">
        <circle className="st-ring-track" cx="60" cy="60" r="55" />
        <circle
          className="st-ring-fill"
          cx="60"
          cy="60"
          r="55"
          pathLength={100}
          strokeDasharray="100"
          strokeDashoffset={100 - clamped * 100}
          transform="rotate(-90 60 60)"
        />
        {/* A lit head riding the arc's leading edge, turned with the same
            easing as the arc so the two never part. */}
        {progress !== null && clamped > 0 && clamped < 1 ? (
          <g className="st-ring-head" style={{ transform: `rotate(${clamped * 360}deg)` }}>
            <circle cx="60" cy="5" r="2.9" />
          </g>
        ) : null}
      </svg>
      <div className="st-ring-inner">{children}</div>
    </div>
  );
}

type IconName = "pause" | "play" | "plus" | "minus" | "skip" | "next" | "flag" | "chevron" | "settings" | "check" | "sliders";

const ICON_PATHS: Record<IconName, ReactNode> = {
  pause: <path d="M8 5v14M16 5v14" />,
  play: <path d="M7 5l12 7-12 7z" fill="currentColor" stroke="none" />,
  plus: <path d="M12 6v12M6 12h12" />,
  minus: <path d="M6 12h12" />,
  skip: <path d="M6 5l9 7-9 7zM18 5v14" />,
  next: <path d="M9 6l6 6-6 6" />,
  flag: <path d="M6 20V5M6 5h11l-2 4 2 4H6" />,
  chevron: <path d="M6 9l6 6 6-6" />,
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </>
  ),
  check: <path d="M5 12.5l4.5 4.5L19 7.5" />,
  sliders: <path d="M4 8h9M17 8h3M4 16h3M11 16h9M15 6v4M9 14v4" />,
};

function Icon({ name }: { name: IconName }) {
  return (
    <svg className="st-glyph" viewBox="0 0 24 24" aria-hidden="true">
      <g fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
        {ICON_PATHS[name]}
      </g>
    </svg>
  );
}

/** Exercise progress across the session, under the header. */
function StepBar({ state }: { state: TimerState }) {
  if (state.items.length < 2) return null;
  return (
    <ol className="st-steps" aria-hidden="true">
      {state.items.map((entry, index) => (
        <li
          key={entry.id}
          data-state={
            state.phase === "done" || index < state.index ? "done" : index === state.index ? "current" : "todo"
          }
        />
      ))}
    </ol>
  );
}

/** A typed-in field: whole, non-negative numbers only; empty reads as 0. */
function wholeNumber(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return 0;
  return /^\d+$/.test(trimmed) ? Number(trimmed) : null;
}

/**
 * Type a value instead of stepping to it. A duration takes minutes and
 * seconds in two fields (a phone's number pad has no ":" key); a count takes
 * one. Enter, the tick or tapping away applies it; Escape leaves it unchanged.
 * The engine still clamps whatever is typed to what it allows.
 */
function StepperEditor({
  label,
  kind,
  initial,
  onSet,
  onDone,
}: {
  label: string;
  kind: "count" | "duration";
  initial: number;
  onSet: (value: number) => void;
  onDone: () => void;
}) {
  const [minutes, setMinutes] = useState(String(Math.floor(initial / 60)));
  const [seconds, setSeconds] = useState(String(initial % 60).padStart(2, "0"));
  const [count, setCount] = useState(String(initial));
  const name = label.toLowerCase();

  const apply = () => {
    if (kind === "count") {
      const value = wholeNumber(count);
      if (value !== null) onSet(value);
    } else {
      const mins = wholeNumber(minutes);
      const secs = wholeNumber(seconds);
      if (mins !== null && secs !== null) onSet(mins * 60 + secs);
    }
    onDone();
  };

  return (
    <form
      className="st-stepper-edit"
      // Our own parsing rejects bad input; the browser's pattern check would
      // only block the submit and leave the editor stuck open.
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        apply();
      }}
      onBlur={(event) => {
        // Moving between the minutes and seconds fields is not leaving.
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) apply();
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          // The sheet's own Escape handling would close the timer view.
          event.stopPropagation();
          onDone();
        }
      }}
    >
      {kind === "count" ? (
        <input
          type="text"
          inputMode="numeric"
          pattern="[0-9]*"
          autoFocus
          aria-label={name}
          value={count}
          onChange={(event) => setCount(event.target.value)}
          onFocus={(event) => event.target.select()}
        />
      ) : (
        <>
          <input
            type="text"
            inputMode="numeric"
            pattern="[0-9]*"
            autoFocus
            aria-label={`${name} minutes`}
            value={minutes}
            onChange={(event) => setMinutes(event.target.value)}
            onFocus={(event) => event.target.select()}
          />
          <span aria-hidden="true">:</span>
          <input
            type="text"
            inputMode="numeric"
            pattern="[0-9]*"
            aria-label={`${name} seconds`}
            value={seconds}
            onChange={(event) => setSeconds(event.target.value)}
            onFocus={(event) => event.target.select()}
          />
        </>
      )}
      <button type="submit" aria-label={`Set ${name}`}>
        ✓
      </button>
    </form>
  );
}

function Stepper({
  label,
  value,
  onDown,
  onUp,
  edit,
}: {
  label: string;
  value: string;
  onDown: () => void;
  onUp: () => void;
  /** Tap the value to type it: the starting number and how to apply it. */
  edit?: { kind: "count" | "duration"; initial: number; onSet: (value: number) => void };
}) {
  const [editing, setEditing] = useState(false);
  return (
    <div className="st-stepper">
      <span className="st-stepper-label">{label}</span>
      {editing && edit ? (
        <StepperEditor label={label} {...edit} onDone={() => setEditing(false)} />
      ) : (
        <div className="st-stepper-control">
          <button type="button" onClick={onDown} aria-label={`Decrease ${label.toLowerCase()}`}>
            −
          </button>
          {edit ? (
            <button
              type="button"
              className="st-stepper-value"
              onClick={() => setEditing(true)}
              aria-label={`${label} ${value}, tap to type`}
            >
              {value}
            </button>
          ) : (
            <span className="st-stepper-value">{value}</span>
          )}
          <button type="button" onClick={onUp} aria-label={`Increase ${label.toLowerCase()}`}>
            +
          </button>
        </div>
      )}
    </div>
  );
}

function ReadyPanel({
  timer,
  item,
  onAdjust,
}: {
  timer: SessionTimerController;
  item: TimerItem;
  onAdjust: () => void;
}) {
  if (item.kind === "task") {
    return <p className="st-plan-line">{describeItem(item)}</p>;
  }
  if (item.kind === "sets") {
    const noun = countNoun(item);
    return (
      <div className="st-setup">
        <button
          type="button"
          className="st-plan-summary"
          data-size={
            (item.sets && item.sets.min !== item.sets.max) ||
            (item.restSec && item.restSec.min !== item.restSec.max) ||
            (item.holdSec && item.restSec)
              ? "long"
              : undefined
          }
          onClick={onAdjust}
          aria-label={`Edit ${noun}s and rest`}
        >
          {/* A no-break space keeps each number with its unit when the line wraps. */}
          {item.sets ? (
            <>
              <span>{formatRange(item.sets)}</span>
              {`\u00a0${item.sets.max === 1 ? noun : `${noun}s`}`}
            </>
          ) : (
            <>{capitalize(noun)}s as needed</>
          )}
          {item.holdSec ? (
            <>
              {" "}
              · <span>{formatClock(item.holdSec)}</span>
              {"\u00a0hold"}
            </>
          ) : null}
          {item.restSec ? (
            <>
              {" "}
              · <span>{formatRange(item.restSec, formatClock)}</span>
              {"\u00a0rest"}
            </>
          ) : null}
        </button>
      </div>
    );
  }
  // One line, not a wall of controls: the plan's format, plus quick presets
  // when the plan left it open. Everything else lives behind Adjust.
  return (
    <div className="st-setup">
      <button type="button" className="st-plan-summary" onClick={onAdjust} aria-label="Edit rounds and timing">
        {item.rounds > 1 ? (
          <>
            <span>{item.rounds}</span> ×{" "}
          </>
        ) : null}
        <span>{formatClock(item.workSec)}</span>
        {item.restSec > 0 ? (
          <>
            {" "}
            · <span>{formatClock(item.restSec)}</span> rest
          </>
        ) : null}
      </button>
      {item.needsSetup ? (
        <p className="st-setup-note">
          {item.setupNote || "Round length isn't set in your plan. Pick a format."}
        </p>
      ) : null}
      {item.presets ? (
        <div className="st-round-presets" role="group" aria-label="Round format">
          {ROUND_PRESETS.map((preset) => {
            const active = preset.workSec === item.workSec && preset.restSec === item.restSec;
            return (
              <button
                key={preset.label}
                type="button"
                className="action-choice"
                aria-pressed={active}
                onClick={() => {
                  rememberRoundFormat(item.formatMemoryKey, {
                    rounds: item.rounds,
                    workSec: preset.workSec,
                    restSec: preset.restSec,
                  });
                  timer.update((s) =>
                    updateIntervalItem(s, { workSec: preset.workSec, restSec: preset.restSec }),
                  );
                }}
              >
                {preset.label}
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

/** The current exercise's numbers, adjustable at any point (behind the header icon). */
function AdjustSheet({ timer, onClose }: { timer: SessionTimerController; onClose: () => void }) {
  const item = currentItem(timer.state);
  if (!item || item.kind === "task") return null;
  // run(), not update(): lowering a target to what is done completes the
  // exercise, and that transition plays its sound like any other.
  const adjust = (patch: ItemAdjustment) => timer.run((s, at) => adjustItem(s, patch, at));
  return (
    <div className="st-sheet st-adjust" role="group" aria-label="Adjust timer">
      <div className="st-settings-head">
        <p className="st-settings-title">
          {item.kind === "interval" ? "Rounds & timing" : `${capitalize(countNoun(item))}s & rest`}
        </p>
        <button type="button" className="st-settings-close" onClick={onClose} aria-label="Close adjust">
          <span aria-hidden="true">×</span>
        </button>
      </div>
      <div className="st-steppers">
        {item.kind === "interval" ? (
          <>
            <Stepper
              label="Rounds"
              value={String(item.rounds)}
              onDown={() => adjust({ rounds: item.rounds - 1 })}
              onUp={() => adjust({ rounds: item.rounds + 1 })}
              edit={{ kind: "count", initial: item.rounds, onSet: (rounds) => adjust({ rounds }) }}
            />
            <Stepper
              label="Round length"
              value={formatClock(item.workSec)}
              onDown={() => adjust({ workSec: stepDuration(item.workSec, -1) })}
              onUp={() => adjust({ workSec: stepDuration(item.workSec, 1) })}
              edit={{ kind: "duration", initial: item.workSec, onSet: (workSec) => adjust({ workSec }) }}
            />
            <Stepper
              label="Rest"
              value={formatClock(item.restSec)}
              onDown={() => adjust({ restSec: stepDuration(item.restSec, -1) })}
              onUp={() => adjust({ restSec: stepDuration(item.restSec, 1) })}
              edit={{ kind: "duration", initial: item.restSec, onSet: (restSec) => adjust({ restSec }) }}
            />
          </>
        ) : (
          <>
            <Stepper
              label={countNoun(item) === "round" ? "Rounds" : "Sets"}
              value={item.sets ? formatRange(item.sets) : "–"}
              onDown={() => adjust({ sets: (item.sets?.min ?? 2) - 1 })}
              onUp={() => adjust({ sets: (item.sets?.max ?? 0) + 1 })}
              edit={{ kind: "count", initial: item.sets?.max ?? 3, onSet: (sets) => adjust({ sets }) }}
            />
            <Stepper
              label="Rest"
              value={item.restSec ? formatRange(item.restSec, formatClock) : "–"}
              onDown={() => adjust({ restSec: stepDuration(item.restSec?.min ?? 90, -1) })}
              onUp={() => adjust({ restSec: stepDuration(item.restSec?.max ?? 75, 1) })}
              edit={{ kind: "duration", initial: item.restSec?.min ?? 90, onSet: (restSec) => adjust({ restSec }) }}
            />
          </>
        )}
      </div>
    </div>
  );
}

const ADJUST_HINT_KEY = "st-adjust-hint-seen";

/** Whether the one-time "you can edit these" hint should show. */
function useAdjustHint(): [boolean, () => void] {
  return useSeenOnce(ADJUST_HINT_KEY);
}

const ROUND_ALERTS = [
  { key: "warnHalfway", label: "Halfway", aria: "Halfway" },
  { key: "warnThirty", label: "30s", aria: "30 seconds left" },
  { key: "warnTen", label: "10s", aria: "10 seconds left" },
] as const;

function SettingsSheet({ timer, onClose }: { timer: SessionTimerController; onClose: () => void }) {
  const { settings, setSettings } = timer;
  // Vibration only works where the browser supports it (not iOS Safari).
  const canVibrate = typeof navigator !== "undefined" && typeof navigator.vibrate === "function";
  return (
    <div className="st-sheet st-settings" role="group" aria-label="Timer settings">
      <div className="st-settings-head">
        <p className="st-settings-title">Timer settings</p>
        <button type="button" className="st-settings-close" onClick={onClose} aria-label="Close settings">
          <span aria-hidden="true">×</span>
        </button>
      </div>
      <div className="st-settings-row">
        <label className="st-switch">
          <span>Sound</span>
          <input
            type="checkbox"
            role="switch"
            checked={settings.sound}
            onChange={(event) => setSettings({ ...settings, sound: event.target.checked })}
          />
        </label>
        <label className="st-range">
          <span className="sr-only">Volume</span>
          <svg className="st-glyph" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M11 5 6 9H3v6h3l5 4V5Z" />
            <path d="M15.5 9.5a4 4 0 0 1 0 5" />
          </svg>
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={settings.volume}
            disabled={!settings.sound}
            onChange={(event) => setSettings({ ...settings, volume: Number(event.target.value) })}
            onPointerUp={() => timerAudio().play("bell")}
          />
        </label>
      </div>
      <div className="st-settings-row">
        <span className="st-settings-label" id="st-alerts-label">Round alerts</span>
        <div className="st-chips" role="group" aria-labelledby="st-alerts-label">
          {ROUND_ALERTS.map((alert) => (
            <button
              key={alert.key}
              type="button"
              className="action-choice"
              aria-pressed={settings[alert.key]}
              aria-label={alert.aria}
              onClick={() => setSettings({ ...settings, [alert.key]: !settings[alert.key] })}
            >
              {alert.label}
            </button>
          ))}
        </div>
      </div>
      <div className="st-settings-row st-choice" role="radiogroup" aria-label="Countdown before round 1">
        <span className="st-settings-label">Get ready</span>
        <div className="st-choice-options">
          {PREP_OPTIONS.map((seconds) => (
            <button
              key={seconds}
              type="button"
              className="action-choice"
              role="radio"
              aria-checked={settings.prepSec === seconds}
              onClick={() => setSettings({ ...settings, prepSec: seconds })}
            >
              {seconds === 0 ? "Off" : `${seconds}s`}
            </button>
          ))}
        </div>
      </div>
      <details className="st-advanced">
        <summary>Advanced options</summary>
        <label className="st-switch">
          <span>Voice callouts</span>
          <input
            type="checkbox"
            role="switch"
            checked={settings.voice}
            onChange={(event) => setSettings({ ...settings, voice: event.target.checked })}
          />
        </label>
        {canVibrate ? (
          <label className="st-switch">
            <span>Vibrate</span>
            <input
              type="checkbox"
              role="switch"
              checked={settings.vibrate}
              onChange={(event) => setSettings({ ...settings, vibrate: event.target.checked })}
            />
          </label>
        ) : null}
        <p className="st-sheet-note">
          Bells play in the background. Voice and vibration may stop; calls or closing the browser can cut audio.
        </p>
      </details>
    </div>
  );
}

function PrimaryAction({
  timer,
  item,
  canFinishItem,
  readyRemainingMs,
}: {
  timer: SessionTimerController;
  item: TimerItem | null;
  canFinishItem: boolean;
  /** Ranged rest only: time until the minimum rest, when the next set unlocks. */
  readyRemainingMs: number | null;
}) {
  const { state } = timer;
  if (!item || state.phase === "done") return null;
  const unlockThen = (fn: () => void) => () => {
    timerAudio().unlock();
    fn();
  };
  if (state.phase === "ready") {
    const start = () => {
      // What they start with is what the timer opens on next time.
      if (item.kind === "interval" && item.formatMemoryKey) {
        rememberRoundFormat(item.formatMemoryKey, {
          rounds: item.rounds,
          workSec: item.workSec,
          restSec: item.restSec,
        });
      }
      timer.run((s, at) => startItem(s, at, { prepSec: timer.settings.prepSec }));
    };
    return (
      <button type="button" className="st-primary" onClick={unlockThen(start)}>
        {item.kind === "interval" && item.rounds > 1 ? "Start round 1" : "Start"}
      </button>
    );
  }
  if (state.pausedAt !== null) {
    return (
      <button type="button" className="st-primary" onClick={unlockThen(() => timer.update(resume))}>
        Resume
      </button>
    );
  }
  if (state.phase === "work" && item.kind === "sets") {
    return (
      <button type="button" className="st-primary" onClick={() => timer.run(completeSetWithResults)}>
        {item.holdSec ? "End hold" : `${unitWord(item)} ${state.unit} done`}
      </button>
    );
  }
  if (state.phase === "work" && item.kind === "task") {
    return (
      <button type="button" className="st-primary" onClick={() => timer.run(finishItem)}>
        Complete
      </button>
    );
  }
  if (state.phase === "rest" && readyRemainingMs !== null && readyRemainingMs > 0) {
    return (
      <button type="button" className="st-primary" data-variant="ghost" disabled>
        {item.kind === "sets" ? unitWord(item) : "Set"} {state.unit} unlocks in{" "}
        {formatClock(Math.ceil(readyRemainingMs / 1000))}
      </button>
    );
  }
  if (state.phase === "rest") {
    return (
      <button type="button" className="st-primary" data-variant="ghost" onClick={() => timer.run(skipRest)}>
        {isPrep(state)
          ? "Start now"
          : item.kind === "sets"
            ? `Start ${countNoun(item)} ${state.unit}`
            : "Skip rest"}
      </button>
    );
  }
  return (
    <button type="button" className="st-primary" data-variant="ghost" onClick={() => timer.update(pause)}>
      Pause
    </button>
  );
}

/** A routine audio note: one quiet line, the detail behind a tap. */
function AudioInfo({ label, detail }: { label: string; detail: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className="st-audio-info" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span className="st-audio-dot" aria-hidden="true" />
        {label}.
        <span className="st-audio-i" aria-hidden="true">i</span>
      </button>
      {open ? <span className="st-audio-detail"> {detail}</span> : null}
    </>
  );
}

type MiniPos = { x: number; y: number; width: number };

/**
 * Lets the minimised timer bar be dragged anywhere on screen, the same way the
 * "Show plan build" pill works: a small movement threshold keeps a tap a tap,
 * the click that ends a drag is swallowed, and the bar is clamped on-screen
 * (also on resize). The width is pinned on the first drag so the bar keeps its
 * shape once it stops being stretched between the page gutters.
 */
function useDraggableMini(onTap: () => void) {
  const [pos, setPos] = useState<MiniPos | null>(null);
  const ref = useRef<HTMLButtonElement>(null);
  const dragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
    width: number;
    moved: boolean;
  } | null>(null);
  const justDraggedRef = useRef(false);

  const clampTo = (x: number, y: number, width: number): MiniPos => {
    const w = Math.min(width, Math.max(0, window.innerWidth - 16));
    const height = ref.current?.offsetHeight ?? 0;
    const maxX = Math.max(8, window.innerWidth - w - 8);
    const maxY = Math.max(8, window.innerHeight - height - 8);
    return { x: Math.min(Math.max(8, x), maxX), y: Math.min(Math.max(8, y), maxY), width: w };
  };

  useEffect(() => {
    if (!pos) return;
    const clamp = () => setPos((prev) => (prev ? clampTo(prev.x, prev.y, prev.width) : prev));
    window.addEventListener("resize", clamp);
    return () => window.removeEventListener("resize", clamp);
  }, [pos]);

  const onPointerDown = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: rect.left,
      originY: rect.top,
      width: rect.width,
      moved: false,
    };
    try {
      el.setPointerCapture(event.pointerId);
    } catch {
      // Pointer capture unsupported; dragging still works via window coords.
    }
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    // Ignore micro-movements so a tap still opens the timer.
    if (!drag.moved && Math.hypot(dx, dy) < 6) return;
    drag.moved = true;
    setPos(clampTo(drag.originX + dx, drag.originY + dy, drag.width));
  };

  const onPointerUp = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    justDraggedRef.current = drag.moved;
    dragRef.current = null;
    try {
      ref.current?.releasePointerCapture(event.pointerId);
    } catch {
      // Ignore release failures.
    }
  };

  const onClick = () => {
    // Suppress the click that ends a drag so the timer doesn't open on drop.
    if (justDraggedRef.current) {
      justDraggedRef.current = false;
      return;
    }
    onTap();
  };

  return {
    ref,
    style: pos
      ? { left: pos.x, top: pos.y, right: "auto", bottom: "auto", width: pos.width, margin: 0 }
      : undefined,
    onPointerDown,
    onPointerMove,
    onPointerUp,
    onPointerCancel: onPointerUp,
    onClick,
  };
}

/** Counted so two timers (round timer + a session timer) never clear each
 * other's reservation. Returns the release function. */
let miniSpaceHolders = 0;
function reserveMiniSpace(): () => void {
  miniSpaceHolders += 1;
  document.documentElement.classList.add("has-timer-mini");
  return () => {
    miniSpaceHolders = Math.max(0, miniSpaceHolders - 1);
    if (miniSpaceHolders === 0) document.documentElement.classList.remove("has-timer-mini");
  };
}

/** Portal to <body> so a transformed ancestor card can never trap the
 * fixed-position overlay; server rendering (tests) renders in place. */
function ToBody({ children }: { children: ReactNode }) {
  return typeof document === "undefined" ? <>{children}</> : createPortal(children, document.body);
}

/**
 * Full-screen session timer. Rounds for sparring / conditioning, sets with
 * auto rest for strength, holds and rest for rehab. Minimising keeps the clock
 * running (the round bells and warnings still ring) and shows a mini bar; closing only ever minimises, so an
 * accidental tap never loses a session.
 */
export function SessionTimer({
  items,
  storageKey,
  sessionTitle,
  visible,
  finishLabel = "Finish session",
  handOffWhenDone = false,
  onMinimize,
  onExpand,
  onFinish,
  onClose,
}: {
  items: TimerItem[];
  storageKey: string;
  sessionTitle: string;
  visible: boolean;
  /** The done screen's button: logging a planned session, or just closing. */
  finishLabel?: string;
  /**
   * Skip the per-exercise done list: show "Session complete" briefly, then
   * finish by itself. The owner has already logged each exercise from the run
   * and takes the athlete straight to the one card to check.
   */
  handOffWhenDone?: boolean;
  onMinimize: () => void;
  onExpand: () => void;
  onFinish: (summary: SessionTimerSummary) => void | Promise<void>;
  /** Closes a timer that was never started, without logging anything. */
  onClose: () => void;
}) {
  const timer = useSessionTimer({ items, storageKey, audible: visible, keepFinishedRun: handOffWhenDone });
  const { state, now } = timer;
  const [sheet, setSheet] = useState<"settings" | "adjust" | null>(null);
  const [confirmEnd, setConfirmEnd] = useState(false);
  const [finishing, setFinishing] = useState(false);
  const [finishError, setFinishError] = useState<string | null>(null);
  const finishInFlight = useRef(false);
  const autoFinishAttempted = useRef(false);
  const [adjustHint, dismissAdjustHint] = useAdjustHint();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const miniDrag = useDraggableMini(onExpand);
  const item = currentItem(state);
  const view = viewAt(state, now);
  const tone = toneFor(state, view);
  const nextItem = state.items[state.index + 1] ?? null;
  const done = state.completed[state.index] ?? 0;
  const canFinishItem =
    item?.kind === "sets" ? (item.sets ? done >= item.sets.min : done > 0) : false;
  const timed = view.remainingMs !== null;
  // A ranged rest counts down to its minimum first, then to its maximum.
  const countdownMs =
    view.readyRemainingMs !== null && view.readyRemainingMs > 0 ? view.readyRemainingMs : view.remainingMs;
  const clockSeconds = timed ? Math.ceil((countdownMs ?? 0) / 1000) : Math.floor(view.elapsedMs / 1000);

  useEffect(() => {
    if (!visible) return;
    const releaseScroll = lockOverlayScroll();
    dialogRef.current?.focus({ preventScroll: true });
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onMinimize();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      releaseScroll();
      window.removeEventListener("keydown", onKey);
    };
  }, [visible, onMinimize]);

  // While the mini bar floats, reserve room under the page so it never sits on
  // top of the last thing the athlete needs to tap (see .has-timer-mini).
  const showsMini = !visible && (state.phase !== "done" || handOffWhenDone || pendingSetLogs(state).length > 0);
  useEffect(() => {
    if (!showsMini) return;
    return reserveMiniSpace();
  }, [showsMini]);

  const finish = async () => {
    if (finishInFlight.current) return;
    finishInFlight.current = true;
    setFinishing(true);
    setFinishError(null);
    try {
      await onFinish({ complete: metPlan(state), notes: summarizeRun(state), sparring: sparringSummary(state), run: state });
    } catch (error) {
      setFinishError(error instanceof Error ? error.message : "Could not save the timer results. Retry save.");
    } finally {
      finishInFlight.current = false;
      setFinishing(false);
    }
  };
  // The latest finish for the hand-off timeout below, without restarting it.
  const finishRef = useRef(finish);
  useEffect(() => {
    finishRef.current = finish;
  });
  // Also when the run comes back already finished (the app closed between the
  // last set and the hand-off): it is handed on, never left hidden.
  const restLogTarget = visibleSetLogTarget(state);
  const finalSetLog = state.phase === "done" && restLogTarget !== null;
  const needsSetReview = state.phase === "done" && !finalSetLog && pendingSetLogs(state).length > 0;
  const restLogConfirmed = restLogTarget ? (state.confirmedSetLogs?.[restLogTarget.itemId]?.includes(restLogTarget.set) || state.unreportedSetLogs?.[restLogTarget.itemId]?.includes(restLogTarget.set)) : false;
  const strengthLogging = Boolean((item?.kind === "sets" && item.setFields) || restLogTarget || needsSetReview);
  const handingOff = handOffWhenDone && state.phase === "done" && !needsSetReview && !finalSetLog && !finishError;
  useEffect(() => {
    if (!handingOff || autoFinishAttempted.current) return;
    const timeout = window.setTimeout(() => {
      // A light tap as the card takes over, felt with the phone in a pocket.
      timerAudio().buzz([40]);
      autoFinishAttempted.current = true;
      void finishRef.current();
    }, visible ? HANDOFF_MS : 0);
    return () => window.clearTimeout(timeout);
  }, [handingOff, visible]);

  if (!visible) {
    if (!showsMini) return null;
    return (
      <ToBody>
        <button type="button" className="st-mini" data-tone={tone} {...miniDrag}>
          <span className="st-mini-phase">{phaseLabel(state, item, view)}</span>
          <span className="st-mini-title">{item?.title}</span>
          <span className="st-mini-clock">{state.phase === "done" ? (finishError ? "Retry save" : finishing ? "Saving…" : "Finish session") : state.phase === "ready" ? "Open" : formatClock(clockSeconds)}</span>
        </button>
      </ToBody>
    );
  }

  const canAdjust = state.phase !== "done" && !!item && item.kind !== "task";
  const toggleAdjust = () => {
    dismissAdjustHint();
    setSheet((open) => (open === "adjust" ? null : "adjust"));
  };
  // Never started: ending just closes, there is nothing to log or call complete.
  const started = state.startedAt !== null;

  const togglePause = () => {
    if (state.phase !== "work" && state.phase !== "rest") return;
    timerAudio().unlock();
    timer.update(state.pausedAt === null ? pause : resume);
  };

  const phaseText = phaseLabel(state, item, view);
  const counter = counterLabel(state, item);
  const sessionElapsed =
    state.startedAt !== null ? formatClock(Math.floor(((state.endedAt ?? now) - state.startedAt) / 1000)) : null;
  const restNote =
    view.readyRemainingMs !== null && item?.kind === "sets" && item.restSec
      ? view.readyRemainingMs > 0
        ? `Minimum rest. Up to ${formatShortDuration(item.restSec.max - item.restSec.min)} more is allowed after this.`
        : `Go when ready. ${unitWord(item)} ${state.unit} starts itself in ${formatClock(Math.ceil((view.remainingMs ?? 0) / 1000))}.`
      : canFinishItem && item?.kind === "sets" && item.sets && item.sets.min !== item.sets.max && state.phase === "rest"
        ? `Minimum hit. Only add ${countNoun(item)}s if you're moving well.`
        : null;

  const nudges = timed && !view.paused;
  // Pause is the primary action during a round, and Resume is primary
  // whenever paused, so it only repeats here for sets and rest. The ring
  // pauses too, so a bare icon is enough.
  const showsPause =
    (state.phase === "rest" || (state.phase === "work" && item?.kind !== "interval")) && !view.paused;
  const endsRound = state.phase === "work" && item?.kind === "interval" && !view.paused;
  // Work done without the clock: count it as planned in one tap. A quiet link,
  // so it never competes with the timer controls.
  const canMarkPlanned = !!item && state.phase !== "ready" && done < plannedTarget(item) && !(item.kind === "task" && state.phase === "work");
  const hasTools = nudges || showsPause || endsRound;
  const nextInSession = state.index + 1 < state.items.length;

  return (
    <ToBody>
    <div
      ref={dialogRef}
      className="st-root"
      data-tone={tone}
      data-strength-logging={strengthLogging || undefined}
      data-set-log-context={restLogTarget ? finalSetLog ? "complete" : state.phase === "ready" ? "transition" : "rest" : undefined}
      data-rest-log={restLogTarget ? restLogConfirmed ? "confirmed" : "open" : undefined}
      role="dialog"
      aria-modal="true"
      aria-label={`${sessionTitle} timer`}
      tabIndex={-1}
    >
      <div className="st-glow" aria-hidden="true" />
      <header className="st-top">
        <button type="button" className="st-icon" onClick={onMinimize} aria-label="Minimise timer">
          <Icon name="chevron" />
        </button>
        <div className="st-heading">
          <p className="st-session">{sessionTitle}</p>
          <p className="st-meta">
            {state.items.length > 1 && state.phase !== "done"
              ? `Exercise ${state.index + 1} of ${state.items.length}`
              : null}
            {state.items.length > 1 && state.phase !== "done" && sessionElapsed ? " · " : null}
            {sessionElapsed ? <span className="st-meta-clock">{sessionElapsed}</span> : null}
          </p>
        </div>
        <div className="st-top-actions">
          {canAdjust ? (
            <button
              type="button"
              className="st-icon"
              onClick={toggleAdjust}
              aria-label="Adjust timer"
              aria-expanded={sheet === "adjust"}
              data-active={sheet === "adjust" ? "true" : undefined}
            >
              <Icon name="sliders" />
            </button>
          ) : null}
          <button
            type="button"
            className="st-icon"
            onClick={() => setSheet((open) => (open === "settings" ? null : "settings"))}
            aria-label="Timer settings"
            aria-expanded={sheet === "settings"}
            data-active={sheet === "settings" ? "true" : undefined}
          >
            <Icon name="settings" />
          </button>
        </div>
        {canAdjust && adjustHint && sheet === null ? (
          <OnceHint className="st-hint" onDismiss={dismissAdjustHint}
            label={item?.kind === "sets" ? `Tap to edit ${countNoun(item)}s & rest` : "Tap to edit rounds & timing"} />
        ) : null}
      </header>
      <StepBar state={state} />

      {sheet === "settings" ? <SettingsSheet timer={timer} onClose={() => setSheet(null)} /> : null}
      {sheet === "adjust" ? <AdjustSheet timer={timer} onClose={() => setSheet(null)} /> : null}
      {/* Tapping anywhere off an open sheet closes it. */}
      {sheet !== null ? <div className="st-scrim" aria-hidden="true" onClick={() => setSheet(null)} /> : null}

      {needsSetReview ? <TimerSetReview timer={timer} /> : (handingOff || finalSetLog) && visible ? (
        <div className="st-main st-done st-handoff">
          <div className="st-done-badge" aria-hidden="true">
            <Icon name="check" />
          </div>
          <p className="st-done-title" aria-live="polite">
            {metPlan(state) ? "Session complete" : "Session ended"}
          </p>
          {sessionElapsed ? <p className="st-done-time">{sessionElapsed} total</p> : null}
          {!finalSetLog ? <button type="button" className="st-link" onClick={() => void finish()} disabled={finishing}>
            {finishing ? "Saving exercises…" : finishLabel}
          </button> : null}
        </div>
      ) : state.phase === "done" ? (
        <div className="st-main st-done">
          <div className="st-done-badge" aria-hidden="true">
            <Icon name="check" />
          </div>
          <p className="st-done-title">{metPlan(state) ? "Session complete" : "Session ended"}</p>
          {sessionElapsed ? <p className="st-done-time">{sessionElapsed} total</p> : null}
          <ul className="st-summary">
            {state.items.map((entry, index) => (
              <li key={entry.id} data-skipped={(state.completed[index] ?? 0) === 0 ? "true" : undefined}>
                <span>{entry.title}</span>
                <span>{doneLabel(entry, state.completed[index] ?? 0)}</span>
              </li>
            ))}
          </ul>
          <div className="st-actions">
            {finishError ? <p className="st-note" role="alert">{finishError} Your timer results are kept. Retry saving.</p> : null}
            <button type="button" className="st-primary" onClick={() => void finish()} disabled={finishing}>
              {finishing ? "Saving exercises…" : finishError ? "Retry save" : finishLabel}
            </button>
          </div>
        </div>
      ) : (
        <div className="st-main" data-phase={state.phase}>
          {state.phase === "ready" && state.items.length === 1 ? null : (
            <p className="st-phase" aria-live="polite">
              <span className="st-phase-dot" aria-hidden="true" />
              {phaseText}
            </p>
          )}
          <h2 className="st-title">{item?.title}</h2>
          {item ? <DoseStats item={item} /> : null}

          {state.phase === "ready" && item ? (
            <ReadyPanel
              timer={timer}
              item={item}
              onAdjust={() => {
                dismissAdjustHint();
                setSheet("adjust");
              }}
            />
          ) : (
            <>
              <button
                type="button"
                className="st-clock"
                onClick={togglePause}
                aria-label={view.paused ? "Resume timer" : "Pause timer"}
              >
                <Ring progress={ringProgress(state, item, view)}>
                  <span className="st-clock-digits" role="timer">
                    {formatClock(clockSeconds)}
                  </span>
                  <span className="st-clock-caption">
                    {view.paused ? "Tap to resume" : counter ?? (timed ? "remaining" : "elapsed")}
                  </span>
                </Ring>
              </button>
              {restNote ? <p className="st-note">{restNote}</p> : null}
              {state.phase === "rest" && state.phaseMs === null && !view.paused ? (
                <div className="st-presets" role="group" aria-label="Rest length">
                  <span>No rest in your plan. Pick one:</span>
                  {REST_PRESETS.map((seconds) => (
                    <button
                      key={seconds}
                      type="button"
                      className="action-choice"
                      onClick={() => timer.update((s, at) => setRestLength(s, seconds, at))}
                    >
                      {formatShortDuration(seconds)}
                    </button>
                  ))}
                </div>
              ) : null}
            </>
          )}
          {state.phase !== "ready" ? <Progress state={state} item={item} /> : null}
          {item?.kind === "interval" ? (
            <div className="st-audio-status" role="status">
              {view.paused ? "Round timer paused." : timer.roundAudioStatus === "preparing" ? "Preparing round audio… Keep this screen open." :
                timer.roundAudioStatus === "unavailable" || timer.roundAudioStatus === "interrupted" ? (
                  <>Round audio {timer.roundAudioStatus === "interrupted" ? "was interrupted" : "is unavailable"}. Keep this screen open.
                    <button type="button" className="st-link" onClick={timer.restoreAudio}>Enable round audio</button>
                  </>
                ) : timer.roundAudioStatus === "playing" ? (
                  <AudioInfo label="Round audio playing" detail="Calls or closing the browser can interrupt bells." />
                ) : !timer.settings.sound || timer.settings.volume === 0 ? "Round sounds are off. Keep the timer in view." : (
                  <AudioInfo label="Bells on" detail="Keep the timer visible for reliable bells. Check your volume before training." />
                )}
            </div>
          ) : null}
        </div>
      )}

      {state.phase !== "done" || finalSetLog ? (
        <div className="st-controls" role="group" aria-label="Timer controls">
          {restLogTarget ? <RestSetLogger key={`${restLogTarget.itemId}:${restLogTarget.set}`} timer={timer} target={restLogTarget} /> : null}
          {/* One light row of tools above the one main action: ±10s either side,
              pause (or End round during a round) in the middle. */}
          {hasTools ? (
            <div className="st-tools">
              {nudges ? (
                <button
                  type="button"
                  className="st-tool"
                  onClick={() => timer.update((s, at) => addTime(s, -10, at))}
                  aria-label="Take 10 seconds off"
                >
                  <span aria-hidden="true">−</span> 10 sec
                </button>
              ) : <span />}
              {endsRound ? (
                <button type="button" className="st-tool" onClick={() => timer.run(endRound)}>
                  <Icon name="skip" />
                  End round
                </button>
              ) : showsPause ? (
                <button type="button" className="st-tool st-tool-icon" onClick={togglePause} aria-label="Pause">
                  <Icon name="pause" />
                </button>
              ) : <span />}
              {nudges ? (
                <button
                  type="button"
                  className="st-tool"
                  onClick={() => timer.update((s, at) => addTime(s, 10, at))}
                  aria-label="Add 10 seconds"
                >
                  10 sec <span aria-hidden="true">+</span>
                </button>
              ) : <span />}
            </div>
          ) : null}
          <PrimaryAction
            timer={timer}
            item={item}
            canFinishItem={canFinishItem}
            readyRemainingMs={view.readyRemainingMs}
          />
          {/* Only ever moves to the next exercise; nothing here ends the
              session. Highlighted once a set range's minimum is met. */}
          {nextInSession && nextItem ? (
            <button
              type="button"
              className="st-next"
              data-emphasis={canFinishItem ? "true" : undefined}
              onClick={() => timer.run(finishItemWithResults)}
              aria-label={`Next exercise: ${nextItem.title}`}
            >
              <span className="st-next-label">Next</span>
              <span className="st-next-title">{nextItem.title}</span>
              <span className="st-next-plan">{describeItem(nextItem)}</span>
              <span className="st-next-arrow" aria-hidden="true"><Icon name="next" /></span>
            </button>
          ) : null}
          {finalSetLog && restLogTarget ? (
            <div className="st-links"><button type="button" className="st-link" onClick={() => timer.update(s => leaveSetUnreported(s, restLogTarget.itemId, restLogTarget.set))}>Leave unreported</button></div>
          ) : confirmEnd ? (
            <div className="st-confirm" role="alertdialog" aria-label="End session">
              <span>End the session now?</span>
              <button
                type="button"
                data-danger="true"
                onClick={() => {
                  setConfirmEnd(false);
                  timer.update(endSessionWithResults);
                }}
              >
                End
              </button>
              <button type="button" onClick={() => setConfirmEnd(false)}>
                Keep going
              </button>
            </div>
          ) : (
            <div className="st-links">
              {canMarkPlanned && !restLogTarget ? (
                <button type="button" className="st-link" onClick={() => timer.run(finishItemAsPlannedWithResults)}>
                  Done as planned
                </button>
              ) : null}
              <button
                type="button"
                className="st-link"
                onClick={() => (started ? setConfirmEnd(true) : onClose())}
              >
                {started ? "End session" : "Close timer"}
              </button>
            </div>
          )}
        </div>
      ) : null}
    </div>
    </ToBody>
  );
}
