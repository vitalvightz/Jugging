/**
 * Guided Fight Visualisation: turns the plan's visualisation session into a
 * spoken script with timed silence.
 *
 * The prescription itself (why, steps, cue, pre-bout line) comes verbatim from
 * the planner's Fight Visualisation bank via the session card. This module only
 * adds the delivery around it: a short breathing settle, the silence the athlete
 * needs to actually run each picture, and a close. The silence is the point; a
 * voice that talks the whole time does the rehearsal for them.
 */
import { cleanText } from "../structured-plan";
import { measuredSeconds } from "../session-timer/plan";
import type { StructuredBlock, StructuredSession } from "../types";

export type FightVisualisation = {
  /** The bank entry's name, e.g. "Tactical Picture". */
  name: string;
  why: string | null;
  steps: string[];
  cue: string | null;
  preBout: string | null;
  /** The prescribed dose, in seconds. */
  durationSec: number;
  /** A camp-block session the athlete may skip, not the countdown protocol. */
  optional: boolean;
  /** The block the run logs as done when it finishes; null when unidentified. */
  blockId: string | null;
};

export type GuidePhase = "settle" | "frame" | "rehearse" | "anchor" | "close";

export type GuideSegment =
  | { kind: "say"; text: string; phase: GuidePhase; estimateSec: number }
  | { kind: "hold"; seconds: number; phase: GuidePhase };

export type GuideScript = {
  visualisation: FightVisualisation;
  segments: GuideSegment[];
  /** Planned length: spoken estimates plus every hold. */
  totalSec: number;
};

const VISUALISATION_TITLE_RE = /\b(?:fight|neural)\s+visuali[sz]ation\b/i;
const VISUALISATION_ID_RE = /(?:fight|neural)[-_]visuali[sz]ation$/i;
const DEFAULT_DURATION_SEC = 5 * 60;
const OPTIONAL_RE = /^optional[.:]\s*/i;

/** True for the planner's locked Fight Visualisation session. */
export function isFightVisualisationSession(
  session: StructuredSession | null | undefined,
): boolean {
  if (!session) return false;
  return (
    VISUALISATION_TITLE_RE.test(cleanText(session.title) ?? "") ||
    VISUALISATION_ID_RE.test(cleanText(session.session_id) ?? "")
  );
}

function prefixed(line: string, prefix: string): string | null {
  const match = line.match(new RegExp(`^${prefix}\\s*:\\s*(.+)$`, "i"));
  return match ? match[1].trim() : null;
}

/**
 * The visualisation a session prescribes, or null when the session is not a
 * Fight Visualisation or carries no steps to guide. The locked merge writes the
 * bank's instructions into the block's `coaching_cues`, followed by
 * "Cue: ..." and, on fight day, "Pre-bout: ...".
 */
export function fightVisualisationFromSession(
  session: StructuredSession | null | undefined,
): FightVisualisation | null {
  if (!session || !isFightVisualisationSession(session)) return null;
  const block: StructuredBlock | undefined = (session.blocks ?? []).find(
    (item) => (item.coaching_cues ?? []).some((cue) => cleanText(cue)) ||
      cleanText(item.execution_instructions ?? item.instructions ?? item.notes),
  );
  if (!block) return null;

  const steps: string[] = [];
  let cue: string | null = null;
  let preBout: string | null = null;
  for (const raw of block.coaching_cues?.length ? block.coaching_cues : [block.execution_instructions ?? block.instructions ?? block.notes]) {
    const line = cleanText(raw);
    if (!line) continue;
    const cueText = prefixed(line, "cue");
    const preBoutText = prefixed(line, "pre-?bout");
    if (cueText) cue ??= cueText;
    else if (preBoutText) preBout ??= preBoutText;
    else steps.push(line);
  }
  if (steps.length === 0) return null;

  // Server-owned flag; the objective also carries an athlete-facing
  // "Optional." prefix, which is not repeated as the spoken why.
  const objective = cleanText(session.objective);
  const optional = session.optional === true;

  return {
    name: cleanText(block.display_name) || "Fight Visualisation",
    why: cleanText(block.purpose) || (objective ? objective.replace(OPTIONAL_RE, "").trim() || null : null),
    steps,
    cue: cue ?? (cleanText(session.mindset_anchor?.confidence_anchor) || null),
    preBout,
    durationSec:
      measuredSeconds(block.duration) ?? measuredSeconds(session.planned_duration) ?? DEFAULT_DURATION_SEC,
    optional,
    blockId: cleanText(block.block_id) || null,
  };
}

/** Rough spoken length at the narrator's slow, calm rate (~140 wpm). */
export function estimateSpeechSec(text: string): number {
  const words = text.trim().split(/\s+/).filter(Boolean).length;
  return Math.max(1.5, Math.round((words / 2.3) * 10) / 10);
}

/** Shortest silence that still lets the athlete see a step once. */
const MIN_STEP_GAP_SEC = 5;
/** Longest single silence: beyond this attention drifts rather than deepens. */
const MAX_STEP_GAP_SEC = 90;
/** At this length and above there is room for a full real-time run-through. */
const RUN_THROUGH_MIN_SEC = 4 * 60;
/** And at this length, a second one: repetitions are what build the skill. */
const SECOND_RUN_THROUGH_MIN_SEC = 10 * 60;
/** Share of the rehearsal time given to the run-throughs. */
const RUN_THROUGH_SHARE = 0.35;
const MAX_RUN_THROUGH_SEC = 150;
/** D-1 / D-0 length and below: familiar and calm rather than fired up. */
const CALM_MAX_SEC = 5 * 60;
/** Silence after the setback line: long enough to see it go wrong and recover. */
const COPING_GAP_SEC = 15;

export type GuideOptions = {
  /** Said first, and once more on the cue: the athlete hears themselves addressed. */
  firstName?: string | null;
  /** Amateur hall or professional arena, for the venue line. */
  level?: "amateur" | "professional" | null;
};

/** First name only, from a profile full name. */
export function firstNameOf(fullName: string | null | undefined): string | null {
  const first = (fullName ?? "").trim().split(/\s+/)[0] ?? "";
  // A handle or an email is not something to say out loud.
  return first && /^[\p{L}][\p{L}'’-]*$/u.test(first) ? first : null;
}

export function isCalmVisualisation(visualisation: FightVisualisation): boolean {
  return visualisation.durationSec <= CALM_MAX_SEC;
}

function sentence(text: string): string {
  return /[.!?]$/.test(text) ? text : `${text}.`;
}

/**
 * The spoken session. Built on PETTLEP and layered stimulus-response imagery:
 * first person through the athlete's own eyes, the venue they will fight in,
 * how the body feels, the bank's steps verbatim with silence to run each one,
 * a setback they recover from, and real-time run-throughs on longer doses.
 */
export function buildGuideScript(
  visualisation: FightVisualisation,
  options: GuideOptions = {},
): GuideScript {
  const name = options.firstName?.trim() || null;
  const short = visualisation.durationSec < 3 * 60;
  const calm = isCalmVisualisation(visualisation);
  const runs =
    visualisation.durationSec >= SECOND_RUN_THROUGH_MIN_SEC
      ? 2
      : visualisation.durationSec >= RUN_THROUGH_MIN_SEC
        ? 1
        : 0;
  const segments: GuideSegment[] = [];
  const say = (text: string, phase: GuidePhase) =>
    segments.push({ kind: "say", text, phase, estimateSec: estimateSpeechSec(text) });
  const hold = (seconds: number, phase: GuidePhase) =>
    segments.push({ kind: "hold", seconds: Math.max(1, Math.round(seconds)), phase });

  // Settle: the breath slows the athlete down before the picture starts.
  say(`${name ? `${name}. ` : ""}Find somewhere quiet. Sit or lie down, and close your eyes.`, "settle");
  hold(3, "settle");
  say("Breathe in through your nose.", "settle");
  hold(4, "settle");
  say("And out, slowly.", "settle");
  hold(6, "settle");
  if (!short) {
    say("Again. In.", "settle");
    hold(4, "settle");
    say("And out.", "settle");
    hold(6, "settle");
  }

  // Frame: perspective, environment and the body before the first step.
  say("See it through your own eyes, as if you're there right now.", "frame");
  hold(2, "frame");
  say(
    options.level === "professional"
      ? "Hear the arena. The crowd, your corner, the referee."
      : "Hear the hall. Your corner, the crowd, the referee.",
    "frame",
  );
  hold(3, "frame");
  say(
    calm
      ? "Feel your heart rate lift, then settle. You've been here before."
      : "Feel your heart rate lift. That's your body getting ready.",
    "frame",
  );
  hold(2, "frame");
  if (visualisation.why) {
    say(sentence(visualisation.why), "frame");
    hold(2, "frame");
  }

  // Everything except the rehearsal gaps is fixed; the rest of the dose is
  // silence for the athlete to run the picture.
  const copingLine = short ? null : "If something goes wrong in the picture, see yourself reset and keep working.";
  const runLines = [
    "Now run it all again, start to finish, in real time.",
    "Once more. A different round, the same calm. Start to finish.",
  ].slice(0, runs);
  const cueLines = visualisation.cue
    ? [`${name ? `${name}, your` : "Your"} cue. ${sentence(visualisation.cue)}`, sentence(visualisation.cue)]
    : [];
  const closing = [
    ...cueLines,
    ...(visualisation.preBout ? [`Before you walk out. ${sentence(visualisation.preBout)}`] : []),
    "Let the picture go. When you're ready, open your eyes.",
  ];
  const sum = (items: GuideSegment[]) =>
    items.reduce((total, item) => total + (item.kind === "say" ? item.estimateSec : item.seconds), 0);
  const speech = (lines: string[]) => lines.reduce((total, line) => total + estimateSpeechSec(line), 0);
  const fixedSec =
    sum(segments) +
    speech(visualisation.steps) +
    (copingLine ? estimateSpeechSec(copingLine) + COPING_GAP_SEC : 0) +
    speech(runLines) +
    speech(closing) +
    3 * closing.length;
  const stepCount = visualisation.steps.length;
  const pool = Math.max(visualisation.durationSec - fixedSec, stepCount * MIN_STEP_GAP_SEC);
  const runCap = runs * MAX_RUN_THROUGH_SEC;
  let runTotal = Math.min(pool * RUN_THROUGH_SHARE * runs, runCap);
  let stepGap = (pool - runTotal) / stepCount;
  if (stepGap > MAX_STEP_GAP_SEC) {
    // Overflow goes to the run-throughs; past their cap the session simply
    // ends a little early rather than leaving minutes of dead air.
    runTotal = Math.min(runTotal + (stepGap - MAX_STEP_GAP_SEC) * stepCount, runCap);
    stepGap = MAX_STEP_GAP_SEC;
  }

  for (const step of visualisation.steps) {
    say(sentence(step), "rehearse");
    hold(Math.max(stepGap, MIN_STEP_GAP_SEC), "rehearse");
  }
  if (copingLine) {
    say(copingLine, "rehearse");
    hold(COPING_GAP_SEC, "rehearse");
  }
  for (const line of runLines) {
    say(line, "rehearse");
    hold(runTotal / runs, "rehearse");
  }

  closing.forEach((line, index) => {
    const phase: GuidePhase = index === closing.length - 1 ? "close" : "anchor";
    say(line, phase);
    hold(3, phase);
  });

  return { visualisation, segments, totalSec: Math.round(sum(segments)) };
}

/** Planned seconds elapsed before segment `index` starts. */
export function elapsedBefore(script: GuideScript, index: number): number {
  return script.segments
    .slice(0, index)
    .reduce((sum, item) => sum + (item.kind === "say" ? item.estimateSec : item.seconds), 0);
}
