// Pure, defensive helpers that decide how a structured_plan renders. The React
// components in components/structured-plan-renderer.tsx are thin wrappers over
// these so the rendering rules stay unit-testable (node:test) and crash-proof
// against malformed/partial payloads.
import { formatPlanLabel, humanizeIfRawEnum, isRawEnumLabel } from "./plan-labels.ts";
import type {
  DeterministicMacroRange,
  DeterministicNutritionPhase,
  DeterministicRecoveryPhase,
  DeterministicWeightCut,
  LoadPrescription,
  MeasuredValue,
  PlanOutputs,
  StructuredBlock,
  StructuredDay,
  StructuredPlan,
  StructuredSession,
  StructuredWeek,
} from "@/lib/types";

type MindsetLine = { label: string; value: string };

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Trimmed non-empty string, or null. Hides null/blank/whitespace fields. */
export function cleanText(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

/** Athlete-facing countdown label. Older structured plans can carry the event
 * day as `D0`; keep the UI contract consistent by always rendering it as `D-0`. */
export function formatCountdownLabel(value: unknown): string | null {
  const text = cleanText(value);
  return text ? text.replace(/^D0\b/i, "D-0") : null;
}

/** A safe array (never throws on null/non-array); empty arrays stay empty. */
export function safeArray<T>(value: T[] | null | undefined): T[] {
  return Array.isArray(value) ? value.filter((item) => item != null) : [];
}

/** Capitalise the first character so a mental cue reads as a sentence
 * ("stop if breathing becomes heavy" -> "Stop if breathing becomes heavy").
 * Only the first character is touched, so acronyms and the rest of the phrase
 * are left exactly as authored. */
function capitalizeFirst(value: string): string {
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : value;
}

/** The session objective — the plan's "Why:" line, shown as the subtitle under
 * the session title. Like the mindset cues it is authored inconsistently (mostly
 * lower-case, occasionally sentence-case), which reads as a typo next to the
 * Title Case session name, so normalise the first letter for display and leave
 * the rest of the phrase exactly as authored. */
// A session objective that still carries its source label ("Why: read the exit
// lane…") would render as "WHY Why: read the exit lane…" once the card adds its
// own kicker. The label is the source grammar, not athlete-facing copy.
const LEADING_WHY_LABEL_RE = /^\s*why(?:\s+today)?\s*:\s*/i;

export function formatSessionObjective(value: unknown): string | null {
  const text = cleanText(value);
  if (!text) {
    return null;
  }
  return capitalizeFirst(text.replace(LEADING_WHY_LABEL_RE, "").trim()) || null;
}

/**
 * Whether to render the structured UI instead of the plan_text fallback.
 * True only when structured_plan is an object with at least one week. Malformed
 * or empty structures fall back to plan_text.
 */
export function shouldRenderStructuredPlan(
  outputs: Pick<PlanOutputs, "structured_plan"> | null | undefined,
): boolean {
  const plan = outputs?.structured_plan;
  if (!isObject(plan)) {
    return false;
  }
  const weeks = (plan as StructuredPlan).weeks;
  return Array.isArray(weeks) && weeks.some((week) => isObject(week));
}

const MEANINGLESS_LOAD = new Set(["", "n/a", "na", "none", "null", "-", "0", "tbd"]);

/**
 * Athlete-facing load string, or null when it should be hidden.
 * Prefers ``load.display``; hides empty/null/meaningless values.
 */
export function formatBlockLoad(load: LoadPrescription | null | undefined): string | null {
  if (!isObject(load)) {
    return null;
  }
  const display = cleanText(load.display);
  if (display && !MEANINGLESS_LOAD.has(display.toLowerCase()) && !isNonFiniteNumericToken(display)) {
    return display;
  }
  if (display) {
    return null; // An explicit but meaningless display hides the load.
  }
  // Cards written before the server stamped `display` still carry the exact
  // machine-readable load; spell only the unambiguous methods.
  const value = load.value;
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    return null;
  }
  const method = cleanText(load.method)?.toLowerCase();
  if (method === "percentage") {
    const ref = cleanText(load.ref);
    return ref ? `${value}% ${ref}` : `${value}%`;
  }
  if (method === "absolute") {
    const unit = cleanText(load.unit);
    return unit ? `${value} ${unit}` : null;
  }
  return null;
}

/** Rest is shown only when it carries a positive value. Hides 0-second rest. */
export function shouldShowRest(rest: MeasuredValue | null | undefined): boolean {
  return isObject(rest) && typeof rest.value === "number" && rest.value > 0;
}

/** A finite, strictly-positive number — the guard for any count/multiplier the
 * renderer prints (sets, reps, rounds). Rejects NaN, ±Infinity, zero, negative
 * and non-numbers so a malformed numeric payload never reaches the UI. */
export function finitePositiveNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

/** A bare non-finite numeric spelling — "NaN", "Infinity", "+Infinity",
 * "-Infinity" (case-insensitive) — that must never render. This targets the
 * STANDALONE token only, so it never touches a valid range like "4-6" or a time
 * string like "30 seconds" (those carry a dash / units and are not bare tokens). */
export function isNonFiniteNumericToken(text: string): boolean {
  return /^[+-]?(nan|infinity)$/i.test(text.trim());
}

/** "180 seconds" / "45 minutes" / null. Rejects non-finite (NaN/Infinity) and
 * negative values — a duration/rest/distance/work measure is never below zero,
 * and a bad number must not leak into the card as "NaN seconds". */
export function formatMeasured(measured: MeasuredValue | null | undefined): string | null {
  if (
    !isObject(measured) ||
    typeof measured.value !== "number" ||
    !Number.isFinite(measured.value) ||
    measured.value < 0
  ) {
    return null;
  }
  // The unit is free text from the model; "sec_per_side" must read as words.
  const unit = cleanText(measured.unit)?.replace(/_+/g, " ").replace(/\s+/g, " ").trim();
  return unit ? `${measured.value} ${unit}` : `${measured.value}`;
}

const TIME_WORDS_RE =
  /(\d+(?:\.\d+)?)(?:\s*([-–—])\s*(\d+(?:\.\d+)?))?\s*(seconds?|secs?|minutes?|mins?)\b/gi;

/**
 * Short time units for the exercise card: "120 seconds" -> "2 min",
 * "90 seconds" -> "90 sec", "2 minutes" -> "2 min", "60-120 seconds" ->
 * "1-2 min". Seconds become minutes only when every number is a whole minute,
 * so the value never changes, only its wording. Everything else in the string
 * ("× 3", "per side") is left as written.
 */
export function compactTimeUnits(text: string): string {
  return text.replace(TIME_WORDS_RE, (_match, low: string, dash: string | undefined, high: string | undefined, unit: string) => {
    const numbers = [low, high].filter((value): value is string => value !== undefined);
    const range = (values: string[]) => values.join(dash ?? "");
    if (/^m/i.test(unit)) {
      return `${range(numbers)} min`;
    }
    const wholeMinutes = numbers.every((value) => Number(value) >= 60 && Number(value) % 60 === 0);
    return wholeMinutes
      ? `${range(numbers.map((value) => String(Number(value) / 60)))} min`
      : `${range(numbers)} sec`;
  });
}

/** A reps value that is really a duration string, e.g. "5-6 min", "30s", "2 min". */
export function isTimeLikeReps(reps: unknown): boolean {
  if (typeof reps !== "string") {
    return false;
  }
  return /\b(s|sec|secs|second|seconds|min|mins|minute|minutes|hr|hrs|hour|hours)\b/i.test(
    reps,
  ) || /\d\s*s\b/i.test(reps) || /\b\d{1,2}:\d{2}\b/.test(reps);
}

/** A prescription mode stored in `reps` by some structured-plan payloads.
 * These values describe how work is performed, not a repetition count. */
export function isModeLikeReps(reps: string): boolean {
  return /\b(continuous|amrap|emom)\b/i.test(reps);
}

function repsEchoesIntervalWork(block: StructuredBlock, repsText: string): boolean {
  const work = block.work;
  if (
    !finitePositiveNumber(block.rounds) ||
    finitePositiveNumber(block.sets) ||
    !isObject(work) ||
    !finitePositiveNumber(work.value) ||
    !/^s(?:ec(?:ond)?s?)?$/i.test(cleanText(work.unit) ?? "")
  ) {
    return false;
  }
  return /^\d+(?:\.\d+)?$/.test(repsText) && Number(repsText) === work.value;
}

export type BlockMetric = { label: string; value: string };

/**
 * The quantitative "how much" lines for a block, in display order.
 *
 * Strength blocks use reps (with optional sets); time-based work uses duration;
 * conditioning blocks may instead (or additionally) use distance and/or rounds.
 * Prefers an explicit duration over reps when reps looks like a time string.
 * Returns an empty array when the block carries no usable metric.
 */
export function selectBlockMetric(block: StructuredBlock | null | undefined): BlockMetric[] {
  if (!isObject(block)) {
    return [];
  }
  const metrics: BlockMetric[] = [];

  const duration = formatMeasured(block.duration);
  // reps as a NUMBER is only usable when finite and positive, so a malformed
  // NaN/Infinity/negative count never renders as "NaN"/"Infinity"/"-5".
  const repsRaw = block.reps;
  let repsText: string | null =
    typeof repsRaw === "number"
      ? finitePositiveNumber(repsRaw)
        ? String(repsRaw)
        : null
      : cleanText(repsRaw as string | null);
  // A reps STRING that is a bare non-positive number ("-5", "0") is also dropped;
  // ranges like "4-6" (not a single negative number) are kept.
  if (repsText !== null && /^-?\d+(?:\.\d+)?$/.test(repsText) && Number(repsText) <= 0) {
    repsText = null;
  }
  // A bare non-finite spelling as text ("NaN", "Infinity", "-Infinity") never
  // renders — a range "4-6" or time "30 seconds" is untouched.
  if (repsText !== null && isNonFiniteNumericToken(repsText)) {
    repsText = null;
  }
  // An interval block (rounds x work) whose bare rep count only repeats the
  // work seconds ("2x3s" read as rounds 2, work 3 sec, reps 3) is an echo, not
  // a volume: "Volume 3" beside "Work 3 seconds" means nothing to the athlete.
  if (repsText !== null && repsEchoesIntervalWork(block, repsText)) {
    repsText = null;
  }
  // The set multiplier must be a finite positive number, or it is omitted.
  const sets = finitePositiveNumber(block.sets) ? (block.sets as number) : null;
  const modeLikeReps = repsText ? isModeLikeReps(repsText) : false;
  const timedHold =
    repsText === "1" && /\b(hold|isometric)\b/i.test(cleanText(block.display_name) ?? "");

  // Whether a metric row already prints the set multiplier. A set count that no
  // row carries is a lost prescription ("2 x 5 sec" rendering as "5 seconds"),
  // so the work/distance rows below pick it up when nothing else did.
  let setsShown = false;
  if ((!repsText || isTimeLikeReps(repsText) || modeLikeReps || timedHold) && duration) {
    // A single hold per set is timed work, not an informative "3 × 1" rep
    // count. Keep its set count alongside the measured, unit-bearing hold.
    metrics.push({ label: "Duration", value: sets && sets > 1 ? `${sets} × ${duration}` : duration });
    setsShown = true;
  } else if (repsText && isTimeLikeReps(repsText)) {
    // The reps value is itself a duration ("30 seconds") and no separate
    // duration exists, so it is time, not a rep count — labelling it "Volume"
    // ("5 × 30 seconds") is semantically wrong. Surface it as Duration, keeping
    // any sets multiplier for the interval count.
    metrics.push({ label: "Duration", value: sets ? `${sets} × ${repsText}` : repsText });
    setsShown = true;
  } else if (repsText && modeLikeReps) {
    // "continuous" / AMRAP / EMOM are execution modes, not volume. When the
    // payload omitted a quantitative duration, keep the card truthful instead
    // of inventing minutes from progression or regression prose.
    metrics.push({
      label: "Mode",
      value: `${repsText.charAt(0).toUpperCase()}${repsText.slice(1)}`,
    });
  } else if (repsText) {
    metrics.push({ label: "Volume", value: sets ? `${sets} × ${repsText}` : repsText });
    setsShown = true;
    if (duration) {
      metrics.push({ label: "Duration", value: duration });
    }
  } else if (duration) {
    metrics.push({ label: "Duration", value: sets && sets > 1 ? `${sets} × ${duration}` : duration });
    setsShown = true;
  }

  const work = formatMeasured(block.work);
  const distance = formatMeasured(block.distance);
  // "1 ×" says nothing, so a single set never needs a carrier.
  const pendingSets = !setsShown && sets && sets > 1 ? sets : null;
  // Timed work per set (sets 2, work 5 sec) prefers the work row; a distance
  // per set (sets 4, distance 200 m) with no work takes it on the distance row.
  const workTakesSets = pendingSets !== null && work !== null;
  const distanceTakesSets = pendingSets !== null && !workTakesSets && distance !== null;

  if (distance) {
    metrics.push({
      label: "Distance",
      value: distanceTakesSets ? `${pendingSets} × ${distance}` : distance,
    });
  }

  if (finitePositiveNumber(block.rounds)) {
    metrics.push({ label: "Rounds", value: String(block.rounds) });
  }

  if (work) {
    metrics.push({ label: "Work", value: workTakesSets ? `${pendingSets} × ${work}` : work });
  }

  return metrics;
}

/** Labels come from the reconciled display metrics, including exact source ranges.
 * Never reinterpret a duration as reps or a set count as rounds. */
export function readableExerciseMetrics(prescription: {
  metrics: BlockMetric[]; load: string | null; effort: string | null; rest: string | null;
}): string | null {
  const time = (value: string) => compactTimeUnits(value.replace(/(\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?)\s*s\b/g, "$1 seconds"));
  const metrics = prescription.metrics.map(({ label, value }) => {
    if (label === "Volume") {
      const reps = (text: string) => /^(?:\d+(?:\.\d+)?(?:\s*[-–]\s*\d+)?)(?:\s+per\s+.+)?$/.test(text)
        ? text.replace(/^(\d+(?:\.\d+)?(?:\s*[-–]\s*\d+)?)/, "$1 reps") : text;
      const pair = value.match(/^(.+?) × (.+)$/);
      return pair ? pair[1].trim() === "1" ? reps(pair[2]) : `${pair[1]} sets × ${reps(pair[2])}` : reps(value);
    }
    if (label === "Rounds") return `${value} ${value === "1" ? "round" : "rounds"}`;
    const measured = time(value).replace(/^1 × /, "").replace(/^(.+?) × /, "$1 sets × ");
    return label === "Work" ? `${measured} work` : measured;
  });
  return [...metrics, prescription.load, prescription.effort,
    prescription.rest ? `${time(prescription.rest)} rest` : null].filter(Boolean).join(" · ") || null;
}

export type BlockComponentLine = { label: string; name: string; detail: string | null };

/**
 * The movements of a cluster block (a contrast pair), in execution order:
 * "A1 Heavy RDL — 2 reps · 85% 1RM", "A2 Broad Jump — 2 reps · Max intent".
 * Empty unless the server stamped at least two named components, so a single
 * movement never renders as a one-row cluster.
 */
export function getBlockComponents(block: StructuredBlock | null | undefined): BlockComponentLine[] {
  const components = Array.isArray(block?.components) ? block.components : [];
  const lines = components.flatMap((component, index) => {
    if (!isObject(component)) return [];
    const name = cleanText(component.name);
    if (!name) return [];
    const reps = typeof component.reps === "number"
      ? finitePositiveNumber(component.reps) ? `${component.reps} rep${component.reps === 1 ? "" : "s"}` : null
      : cleanText(component.reps);
    const load = formatBlockLoad(component.load);
    const effort = formatEffort({ effort: component.effort } as StructuredBlock);
    const detail = [reps, load, effort].filter(Boolean).join(" · ") || null;
    return [{ label: `A${index + 1}`, name, detail }];
  });
  return lines.length >= 2 ? lines : [];
}

/** Qualitative rest for the Rest stat: "Full rest between holds" -> "Full". */
export function formatRestNote(block: StructuredBlock | null | undefined): string | null {
  const note = cleanText(block?.rest_note);
  if (!note) return null;
  return /^full\s+rest\b/i.test(note) ? "Full" : note;
}

/** Collapsed-row line for a cluster: "3 rounds · rest 4 min", else null. */
export function formatClusterSummary(block: StructuredBlock | null | undefined): string | null {
  if (getBlockComponents(block).length === 0 || !finitePositiveNumber(block?.rounds)) {
    return null;
  }
  const rounds = `${block.rounds} round${block.rounds === 1 ? "" : "s"}`;
  const rest = shouldShowRest(block.rest) ? formatMeasured(block.rest) : null;
  const restText = rest ? `rest ${compactTimeUnits(rest)}` : formatRestNote(block)?.toLowerCase().replace(/^full$/, "full rest");
  return [rounds, restText].filter(Boolean).join(" · ");
}

// Athlete-facing names for an effort method printed without a value.
const EFFORT_METHOD_LABELS: Readonly<Record<string, string>> = {
  rpe: "RPE",
  rir: "RIR",
  intent: "Intent",
  velocity: "Velocity",
  heart_rate_zone: "Heart rate zone",
  pace: "Pace",
  max_effort_percent: "Max effort %",
};

// Intent values that are a bare intensity word read as "Max intent"; anything
// more descriptive ("max_speed", "explosive") already says how to move.
const BARE_INTENT_LEVELS = new Set([
  "max",
  "maximal",
  "maximum",
  "full",
  "high",
  "moderate",
  "medium",
  "low",
  "submaximal",
  "sub-maximal",
]);

const NUMERIC_OR_RANGE_RE = /^\d+(?:\.\d+)?(?:\s*[-–—]\s*\d+(?:\.\d+)?)?$/;

/** Machine spellings ("max_speed", "zone_2") as plain words ("max speed"). An
 * all-caps token ("MAX_SPEED") is lowered so the card does not shout. */
function humanizeEffortToken(text: string): string {
  const spaced = text.replace(/_+/g, " ").replace(/\s+/g, " ").trim();
  return /[a-z]/.test(spaced) || !/[A-Z]{2}/.test(spaced) ? spaced : spaced.toLowerCase();
}

/** "RPE 7" / "7 RPE" as a value under method RPE is just "7": the method is
 * printed once by the caller. */
function stripRepeatedMethod(value: string, method: string): string {
  const escaped = method.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return value
    .replace(new RegExp(`^${escaped}\\s*:?\\s*`, "i"), "")
    .replace(new RegExp(`\\s*${escaped}$`, "i"), "")
    .trim();
}

/** "2", "z2", "zone2", "Zone 2", "z2 z3" -> the zone number(s), else null. */
function heartRateZoneNumbers(value: string): string | null {
  if (NUMERIC_OR_RANGE_RE.test(value)) return value;
  const zones = value.match(/^(?:z|zone)\s*(\d)(?:\s*(?:[-–—]|to|\s)\s*(?:z|zone)?\s*(\d))?$/i);
  if (!zones) return null;
  return zones[2] ? `${zones[1]}-${zones[2]}` : zones[1];
}

function effortWithMethod(methodKey: string, method: string, rawValue: string): string {
  const value =
    methodKey in EFFORT_METHOD_LABELS
      ? stripRepeatedMethod(rawValue, methodKey === "heart_rate_zone" ? "heart rate zone" : methodKey) ||
        rawValue
      : rawValue;
  const lower = value.toLowerCase();
  switch (methodKey) {
    case "rpe":
      return `RPE ${value}`;
    case "rir":
      return `RIR ${value}`;
    case "intent":
      if (BARE_INTENT_LEVELS.has(lower)) return `${capitalizeFirst(lower)} intent`;
      // A bare number has no unit on its own. Up to 1 is a fraction of full
      // intent (the model writes 1 for "full", never meaning 1/10 on a power
      // block), up to 10 a 10-point scale, anything larger a percentage.
      if (/^\d+(?:\.\d+)?$/.test(value)) {
        const intent = Number(value);
        if (intent > 0 && intent <= 1) {
          return intent === 1 ? "Max intent" : `${Math.round(intent * 100)}% intent`;
        }
        return intent <= 10 ? `Intent ${value}/10` : `${value}% intent`;
      }
      if (/^\d+(?:\.\d+)?\s*%$/.test(value)) return `${value} intent`;
      return capitalizeFirst(value);
    case "velocity":
      if (NUMERIC_OR_RANGE_RE.test(value)) return `${value} m/s`;
      return capitalizeFirst(value.replace(/(\d)\s*m\/s\b/i, "$1 m/s"));
    case "heart_rate_zone": {
      const zone = heartRateZoneNumbers(value);
      if (zone) return `Zone ${zone}`;
      return capitalizeFirst(/\bzone\b/i.test(value) ? value : `${value} zone`);
    }
    case "pace":
      if (/\bpace\b/i.test(value)) return capitalizeFirst(value);
      return /\d/.test(value) ? `Pace ${value}` : `${capitalizeFirst(value)} pace`;
    case "max_effort_percent":
      if (/^\d+(?:\.\d+)?$/.test(value) && Number(value) > 0 && Number(value) <= 1) {
        // A fraction (0.8) is 80%, never "0.8% of max".
        return `${Math.round(Number(value) * 100)}% of max`;
      }
      if (NUMERIC_OR_RANGE_RE.test(value)) return `${value}% of max`;
      return /\bmax/i.test(value) ? value : `${value} of max`;
    default:
      return capitalizeFirst(`${humanizeEffortToken(method)} ${value}`);
  }
}

/**
 * Athlete-facing effort: "RPE 7", "Max intent", "Max speed", "Zone 2",
 * "80% of max" or null. The payload's method is an enum and its value is often
 * a machine token from the model ("max_speed"), so neither is printed raw.
 */
export function formatEffort(block: StructuredBlock | null | undefined): string | null {
  const effort = block?.effort;
  if (!isObject(effort)) {
    return null;
  }
  const method = cleanText(effort.method);
  // A numeric effort (e.g. RPE 7) must be finite — a NaN/Infinity would otherwise
  // print "RPE NaN". Non-numeric effort passes through cleanText, but a bare
  // non-finite spelling as text ("RPE" + "NaN"/"Infinity") is dropped too.
  const rawValue =
    typeof effort.value === "number"
      ? Number.isFinite(effort.value)
        ? String(effort.value)
        : null
      : cleanText(effort.value as string);
  const cleanValue = rawValue !== null && isNonFiniteNumericToken(rawValue) ? null : rawValue;
  const value = cleanValue ? humanizeEffortToken(cleanValue) || null : null;
  const methodKey = method ? method.toLowerCase() : "";
  if (method && value) {
    return effortWithMethod(methodKey, method, value);
  }
  if (method) {
    return EFFORT_METHOD_LABELS[methodKey] ?? capitalizeFirst(humanizeEffortToken(method));
  }
  return value ? capitalizeFirst(value) : null;
}

// --- safe structural selectors (never throw on partial data) ----------------

/** Local ISO date (YYYY-MM-DD) for the Monday of the week containing `dateStr`. */
function calendarWeekMonday(dateStr: string | null): string | null {
  if (!dateStr) {
    return null;
  }
  const parsed = new Date(`${dateStr.slice(0, 10)}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }
  // getDay(): 0=Sun..6=Sat -> offset back to Monday (0=Mon..6=Sun).
  const offsetToMonday = (parsed.getDay() + 6) % 7;
  parsed.setDate(parsed.getDate() - offsetToMonday);
  const y = parsed.getFullYear();
  const m = String(parsed.getMonth() + 1).padStart(2, "0");
  const d = String(parsed.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

/** Whole days between two local ISO date strings, or null if either is unusable. */
function daySpan(startIso: string, endIso: string): number | null {
  const start = new Date(`${startIso.slice(0, 10)}T00:00:00`);
  const end = new Date(`${endIso.slice(0, 10)}T00:00:00`);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
    return null;
  }
  return Math.round((end.getTime() - start.getTime()) / 86_400_000);
}

const PHASE_RANK: Record<string, number> = {
  GPP: 0,
  SPP: 1,
  TAPER: 2,
  FIGHT_WEEK: 3,
  REINTEGRATION: 4,
};

function phaseCoverage(days: StructuredDay[]): string[] {
  const phases: string[] = [];
  for (const day of days) {
    const phase = cleanText(day.phase_label)?.toUpperCase();
    if (phase && !phases.includes(phase)) {
      phases.push(phase);
    }
  }
  return phases;
}

function displayPhaseForDays(days: StructuredDay[], fallback: string | null | undefined): string | null {
  const counts = new Map<string, number>();
  for (const day of days) {
    const phase = cleanText(day.phase_label)?.toUpperCase();
    if (!phase) continue;
    counts.set(phase, (counts.get(phase) ?? 0) + 1);
  }
  if (counts.size === 0) {
    return cleanText(fallback);
  }
  const top = Math.max(...counts.values());
  return [...counts.entries()]
    .filter(([, count]) => count === top)
    .map(([phase]) => phase)
    .sort((a, b) => (PHASE_RANK[b] ?? 0) - (PHASE_RANK[a] ?? 0))[0] ?? cleanText(fallback);
}

/**
 * Split one plan week into per-calendar-week (Mon–Sun) display weeks.
 *
 * Normal camp weeks already fit inside a single calendar week, so they pass
 * through unchanged. A late-fight / bridge plan, however, ships its whole
 * countdown as ONE week object spanning two or three calendar weeks — which the
 * UI would otherwise render as a single "Week 1" listing every day. Splitting it
 * on Monday boundaries restores the week-by-week view. Each sub-week inherits the
 * source week's metadata but gets its own day list and recomputed date/countdown
 * range. Days without a parseable date stay with the preceding sub-week.
 *
 * Guarded to only ever touch a genuinely long block: a week whose dated days
 * span 7 days or fewer is returned untouched even if it happens to cross a
 * Monday, so a normally-structured camp week is never re-cut.
 */
function splitWeekByCalendarWeek(week: StructuredWeek): StructuredWeek[] {
  const days = getDays(week);
  const datedSorted = days
    .map((day) => cleanText(day.date))
    .filter((value): value is string => value !== null)
    .sort();
  if (datedSorted.length > 0) {
    const span = daySpan(datedSorted[0], datedSorted[datedSorted.length - 1]);
    if (span === null || span <= 7) {
      return [week];
    }
  }

  const groups: { monday: string; days: StructuredDay[] }[] = [];
  const leadingUndated: StructuredDay[] = [];

  for (const day of days) {
    const monday = calendarWeekMonday(cleanText(day.date));
    if (monday === null) {
      if (groups.length > 0) {
        groups[groups.length - 1].days.push(day);
      } else {
        leadingUndated.push(day);
      }
      continue;
    }
    const existing = groups.find((group) => group.monday === monday);
    if (existing) {
      existing.days.push(day);
    } else {
      groups.push({ monday, days: [day] });
    }
  }

  // One calendar week (or no dates to split on) -> leave the week untouched.
  if (groups.length <= 1) {
    return [week];
  }

  groups.sort((a, b) => (a.monday < b.monday ? -1 : a.monday > b.monday ? 1 : 0));
  if (leadingUndated.length > 0) {
    groups[0].days = [...leadingUndated, ...groups[0].days];
  }

  return groups.map((group, index) => {
    const dates = group.days
      .map((day) => cleanText(day.date))
      .filter((value): value is string => value !== null)
      .sort();
    const labels = group.days
      .map((day) => formatCountdownLabel(day.countdown_label))
      .filter((value): value is string => value !== null);
    const splitWeek = {
      ...week,
      week_id: `${cleanText(week.week_id) || "week"}-${group.monday}-cw${index + 1}`,

      days: group.days,
      phase_label: displayPhaseForDays(group.days, week.phase_label),
      phase_coverage: phaseCoverage(group.days),
      start_date: dates[0] ?? week.start_date,
      end_date: dates[dates.length - 1] ?? week.end_date,
      // Days run furthest-from-fight first, so the first/last labels bound the range.
      countdown_start: labels[0] ?? week.countdown_start,
      countdown_end: labels[labels.length - 1] ?? week.countdown_end,
    };
    const inheritedGoal = cleanText(week.week_goal);
    const inheritedLateFightGoal = inferredLateFightWeekContext(week).goal;
    if (
      inheritedGoal &&
      inheritedLateFightGoal &&
      inheritedGoal.toLowerCase() === inheritedLateFightGoal.toLowerCase()
    ) {
      return {
        ...splitWeek,
        week_goal: inferredLateFightWeekContext(splitWeek).goal || inheritedGoal,
      };
    }
    return splitWeek;
  });
}

export function getWeeks(plan: StructuredPlan | null | undefined): StructuredWeek[] {
  const raw = safeArray(plan?.weeks).filter(isObject);
  const split = raw.flatMap(splitWeekByCalendarWeek);
  // No week spanned multiple calendar weeks -> identity (zero change for normal
  // camps). Only when a split happened do we renumber week_index so the strip
  // reads Week 1, 2, 3… in order.
  if (split.length === raw.length) {
    return raw;
  }
  return split.map((week, index) => ({ ...week, week_index: index + 1 }));
}

export function getDays(week: StructuredWeek | null | undefined): StructuredDay[] {
  return safeArray(week?.days).filter(isObject);
}

export function getSessions(day: StructuredDay | null | undefined): StructuredSession[] {
  return safeArray(day?.sessions).filter(isObject);
}

// ---------------------------------------------------------------------------
// Zero-load support: scheduled items that must RENDER but must never count as
// physical training. One shared rule, used by the text adapter, the renderer and
// the completion counters, so rendering fidelity and physical-session
// accounting stay separate concerns (the planner owns the latter in
// fightcamp/physical_session_frequency.py and this never changes it).
// ---------------------------------------------------------------------------

/** Session types that are informational by construction (mental / tactical). */
const ZERO_LOAD_SESSION_TYPES = new Set([
  "mindset",
  "mental",
  "tactical",
  "visualisation",
  "visualization",
  "education",
  "protocol",
  "fight_day",
]);

/** Session types that always prescribe real movement; they win over wording. */
const PHYSICAL_SESSION_TYPES = new Set([
  "strength_power",
  "conditioning",
  "sparring",
  "fight_or_match",
  "rehab",
  "mobility",
  "recovery",
]);

/**
 * Content signals for scheduled zero-load work. Deliberately semantic (what the
 * item IS) rather than a list of drill names, so a new tactical or mental insert
 * is classified correctly without another patch. "Technical"/"footwork"/"joint
 * prep" are NOT here: those are physical work even when they are light.
 */
const ZERO_LOAD_CONTENT_RE =
  /\b(?:visuali[sz]\w*|mental rehearsal|tactical (?:watch|focus|review|cue)|cue card|self-?review|film (?:study|review)|breathing (?:reset|work|only)|fight[- ]day protocol|no physical load|zero[- ]load)\b/i;

/**
 * True when this scheduled item is zero-load support (mental, tactical,
 * breathing-only, fight-day protocol). Such a session still renders in full —
 * it just contributes nothing to physical-session counts.
 *
 * Low-cost PHYSICAL support (joint prep, footwork walkthrough, technical shadow
 * rhythm, walk flush) is deliberately not matched here: it is light work, not
 * absent work, and it keeps counting as a physical session.
 */
export function isZeroLoadSupportSession(
  session: StructuredSession | null | undefined,
): boolean {
  if (!isObject(session)) {
    return false;
  }
  // `stress_class: "support"` is NOT consulted. The planner stamps it on joint
  // prep, footwork walkthroughs and technical shadow rhythm as well as on
  // breathing resets: it means "low cost", not "no movement", and
  // fightcamp/physical_session_frequency.py documents that it is insufficient
  // on its own. Zero load is decided by what the item IS, below.
  const sessionType = cleanText(session.session_type)?.toLowerCase().replace(/[\s-]+/g, "_");
  if (sessionType && ZERO_LOAD_SESSION_TYPES.has(sessionType)) {
    return true;
  }
  // The title names what the item IS, so it is read first: a "Breathing Reset"
  // is zero load even though the adapter infers a `recovery` session type from
  // the same words. Block names only get a say when the session type does not
  // already assert real movement, so a strength session whose accessory happens
  // to mention a cue card is never demoted.
  if (ZERO_LOAD_CONTENT_RE.test(cleanText(session.title) || "")) {
    return true;
  }
  if (sessionType && PHYSICAL_SESSION_TYPES.has(sessionType)) {
    return false;
  }
  const blockNames = getBlocks(session)
    .map((block) => cleanText(block.display_name) || "")
    .join(" ");
  return ZERO_LOAD_CONTENT_RE.test(blockNames);
}

/** Only the five established mental identities receive the compact treatment.
 * Physical sessions and passive breathing/sleep support keep their own UI. */
export function mentalSessionCategory(session: StructuredSession): "Visualise" | "Analyse" | "Reflect" | null {
  if (!isZeroLoadSupportSession(session)) return null;
  // Older converter cards sometimes bundle physical exercises under a mental
  // title. Keep that whole card in the normal layout so no movement is hidden.
  const type = cleanText(session.session_type)?.toLowerCase();
  if (type && PHYSICAL_SESSION_TYPES.has(type)) return null;
  if (getBlocks(session).some(block => {
    const blockType = cleanText(block.block_type);
    const name = cleanText(block.display_name) || "";
    // Older valid converter cards tag banked mental rehearsal as "skill" and
    // name it "Tactical Picture". It has no executable exercise key. Treat it
    // as the same zero-load visualisation as today's locked mindset version,
    // without hiding real skill drills or conditioning inside a mental title.
    const mentalSkill = blockType === "skill" && (
      ZERO_LOAD_CONTENT_RE.test(name) ||
      (/^(?:Tactical Picture|Pressure Route Scan|Pocket Exchange Map|First-Round Pressure Script)$/i.test(name)
        && !cleanText(block.exercise_key))
    );
    return blockType && blockType !== "mindset" && !mentalSkill;
  })) return null;
  const identity = `${session.session_id ?? ""} ${session.title ?? ""}`;
  if (/fight[_ -]visuali[sz]|neural[_ -]visuali[sz]/i.test(identity)) return "Visualise";
  if (/tactical[_ -](?:watch|focus|cue[_ -]card)/i.test(identity)) return "Analyse";
  if (/self[_ -]?review/i.test(identity)) return "Reflect";
  return null;
}

/** Compact only standalone mental/breathing tasks, never protocols or movement. */
export function isCompactPlanSupportSession(session: StructuredSession): boolean {
  const type = cleanText(session.session_type);
  if (type && !["mindset", "skill", "recovery"].includes(type)) return false;
  if (getBlocks(session).some(block => getBlockAdjustmentDisplay(block).stopRules.length > 0)) return false;
  if (mentalSessionCategory(session)) return true;
  if (!/^breathing reset$/i.test(cleanText(session.title) || "")) return false;
  if (!["recovery", "mindset", "skill"].includes(cleanText(session.session_type) || "")) return false;
  return getBlocks(session).every(block =>
    !cleanText(block.exercise_key) &&
    (!block.block_type || ["mindset", "recovery"].includes(block.block_type)) &&
    (!cleanText(block.display_name) || /^breathing reset$/i.test(cleanText(block.display_name)!)));
}

/** Preview the selected activity's authored instruction, preserving its full
 * copy in the disclosure. Prefer a complete sentence, then a word boundary. */
export function mentalSessionSummary(session: StructuredSession): string | null {
  const instructions = getBlocks(session).flatMap(block => getStringList(block.coaching_cues));
  const selected = instructions.map(line => line.replace(/^\s*(?:instructions?:|\d+[.)])\s*/i, "").trim())
    .find(line => line && !/^(?:cue|pre-bout|progress|stop|rest)\s*:/i.test(line) &&
      !/^\d+(?:[–-]\d+)?\s*(?:min(?:ute)?s?|sec(?:ond)?s?)\b/i.test(line));
  const full = selected || formatSessionObjective(session.objective);
  if (!full || full.toLowerCase() === cleanText(session.title)?.toLowerCase()) return null;
  if (full.length <= 180) return full;
  const sentence = full.slice(0, 181).match(/^(.+?[.!?])(?:\s|$)/)?.[1];
  if (sentence) return sentence;
  const words = full.slice(0, 177).replace(/\s+\S*$/, "").trimEnd();
  return `${words}…`;
}

/** Preserve every authored action in its original order. */
export function mentalTaskSteps(lines: string[]): string[] {
  const numbered = lines.filter(line => /^\s*Step\s*\d+\s*:/i.test(line));
  const source = numbered.length ? numbered : lines.filter(line =>
    !/^\s*(?:Cue|Intent|Focus|Reset|Anchor|Purpose|Progress|Stop|Pre-bout)\s*:/i.test(line) &&
    !/^\s*\d+(?:[–-]\d+)?\s*(?:min|sec|minutes|seconds)\b/i.test(line));
  const steps = dedupeBlockText(source.map(line => line.replace(/^\s*Step\s*\d+\s*:\s*/i, "")));
  return steps;
}

/** Sessions on this day that count toward physical training frequency. */
export function getPhysicalSessions(
  day: StructuredDay | null | undefined,
): StructuredSession[] {
  return getSessions(day).filter((session) => session.optional !== true && !isZeroLoadSupportSession(session));
}

/** Coach-owned contact kinds that are real physical work the athlete performs. */
const COACH_LED_PHYSICAL_KINDS = new Set<SessionlessDayKind>([
  "coach_led",
  "sparring",
  "technical",
  "light_combat",
]);

/**
 * True when this day's physical training is coach-owned and the app prescribes
 * no physical card of its own: declared hard sparring, and the technical /
 * light-combat days a downgrade produces.
 *
 * The athlete trains physically on such a day, so it is a physical training day
 * even though the app prescribes nothing for it.
 *
 * Two things deliberately do NOT disqualify it:
 *
 *   * zero-load support sharing the day. Production ships "Technical-only
 *     combat + Tactical Focus" on D-11 and "Technical-only combat + Tactical
 *     Cue Card" on D-4; those cards are sessions, but they are not physical
 *     work, so the combat still has to count. Only an app PHYSICAL session on
 *     the same day removes the addition — that day is already counted, and
 *     counting the contact too would double it.
 *   * where the contact is recorded. A day with no app card names it in
 *     `today_card.headline`; a day that also carries app work names it in
 *     `today_card.coach_led_contact` (the deterministic field
 *     `reconcile_coach_led_sparring_days` writes), and the day classifier reads
 *     only the headline. Both are consulted here.
 *
 * Fight day is excluded: D-0 is the competition, not a training session.
 */
export function isCoachLedPhysicalTrainingDay(
  day: StructuredDay | null | undefined,
): boolean {
  if (!isObject(day) || getPhysicalSessions(day).length > 0) {
    return false;
  }
  const countdown = cleanText(day.countdown_label)?.replace(/\s+/g, "").toUpperCase();
  const dayType = cleanText(day.day_type)?.toLowerCase();
  if (countdown === "D-0" || countdown === "D0" || dayType === "competition") {
    return false;
  }
  const contact = getCoachLedContactView(day);
  const kind = contact
    ? contact.kind
    : getSessions(day).length === 0
      ? classifySessionlessDay(day).kind
      : null;
  return kind !== null && COACH_LED_PHYSICAL_KINDS.has(kind);
}

/** Complete planner-owned microdose for this host day; malformed legacy data is hidden. */
export function getPriorityMicrodose(day: StructuredDay | null | undefined) {
  const raw = day?.priority_microdose;
  if (!isObject(raw)) {
    return null;
  }
  const goal = cleanText(raw.goal);
  const name = cleanText(raw.name);
  const prescription = cleanText(raw.prescription);
  return goal && name && prescription ? { goal, name, prescription } : null;
}

export function getBlocks(session: StructuredSession | null | undefined): StructuredBlock[] {
  return safeArray(session?.blocks).filter(isObject);
}

const REHAB_BLOCK_RE = /\b(rehab|prehab|mobility|mobil(?:ity|isation|ization)|isometric|opener)\b/i;

/** Rehab/mobility blocks get a compact always-visible summary on session cards. */
export function isRehabOrMobilityBlock(block: StructuredBlock | null | undefined): boolean {
  if (!isObject(block)) {
    return false;
  }
  return [block.block_type, block.category, block.display_name]
    .map((value) => cleanText(value))
    .some((value) => Boolean(value && REHAB_BLOCK_RE.test(value)));
}

/** Ordered rehab/mobility blocks for a session, empty when absent. */
export function getRehabOrMobilityBlocks(
  session: StructuredSession | null | undefined,
): StructuredBlock[] {
  return getBlocks(session).filter(isRehabOrMobilityBlock);
}

export function getCoachingCues(block: StructuredBlock | null | undefined): string[] {
  return getStringList(block?.coaching_cues);
}

const BARE_ADJUSTMENT_LABEL_RE =
  /^\s*(?:progress(?:ion)?\s*\/\s*)?regress(?:ion)?\s*\/?\s*$/i;
const COACHING_STOP_CUE_RE = /^\s*stop(?:\s+rule)?\s*:/i;

/** Clean legacy converter leakage out of coaching cues while retaining any
 * actionable stop instruction for a labelled Stop rule aside. */
export function getBlockCoachingDisplay(
  block: StructuredBlock | null | undefined,
): { cues: string[]; stopRules: string[] } {
  const cues: string[] = [];
  const stopRules: string[] = [];
  for (const cue of getCoachingCues(block)) {
    if (BARE_ADJUSTMENT_LABEL_RE.test(cue)) {
      continue;
    }
    if (COACHING_STOP_CUE_RE.test(cue)) {
      stopRules.push(cue);
      continue;
    }
    cues.push(cue);
  }
  return { cues, stopRules };
}

const BLOCK_DETAIL_LABEL_RE =
  /^\s*(purpose|why\s+today|easier|regress(?:ion)?|progress(?:ion)?|stop(?:\s+rule)?|swaps?|substitutions?|cue)\s*:\s*(.*)$/i;
const COACHING_PURE_STOP_RE = /^\s*stop(?!-)/i;

function dedupeBlockText(values: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values) {
    const text = cleanText(value);
    if (!text) continue;
    const key = text.toLowerCase().replace(/\s+/g, " ").replace(/[.\s]+$/, "");
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(text);
  }
  return result;
}

/** Athlete-facing execution projection. Rich purpose/why-today reasoning stays
 * on the block object, while labelled legacy prose is routed away from cue bullets. */
export function getBlockExecutionDisplay(
  block: StructuredBlock | null | undefined,
): {
  cues: string[];
  stopRules: string[];
  regressions: string[];
  substitutions: string[];
  progressions: string[];
  prescription: string | null;
} {
  const cues: string[] = [];
  const stopRules: string[] = [];
  const regressions = getStringList(block?.regression_options);
  const substitutions = getStringList(block?.substitutions);
  const progressions: string[] = [];
  const prescriptions: string[] = [];

  // Legacy deterministic cards can put a dose before "Cue: ... Stop ...".
  // Keep one true execution cue and separate the embedded safety stop.
  const registerCue = (value: string) => {
    const text = cleanText(value);
    if (!text) return;
    const embeddedStop = text.match(/^(.*?)\.\s*(Stop\b.+)$/i);
    if (embeddedStop) {
      if (embeddedStop[1].trim()) registerCue(embeddedStop[1].trim());
      stopRules.push(embeddedStop[2].trim());
      return;
    }
    if (/^\d+(?:[–-]\d+|\.\d+)?\s*(?:sets?\s*[x×]|[x×]\s*\d|(?:s|sec|seconds)\s+work\b|(?:min|minutes)\b)/i.test(text)) {
      prescriptions.push(text);
      return;
    }
    if (/^Next D-\d+:\s*repeat\b/i.test(text)) {
      return;
    }
    cues.push(text);
  };

  for (const cue of getCoachingCues(block)) {
    if (BARE_ADJUSTMENT_LABEL_RE.test(cue)) continue;
    const labelled = BLOCK_DETAIL_LABEL_RE.exec(cue);
    if (labelled) {
      const label = labelled[1].toLowerCase().replace(/\s+/g, " ");
      const detail = cleanText(labelled[2]);
      if (!detail) continue;
      if (label === "purpose" || label === "why today") continue;
      if (label === "cue") {
        registerCue(detail);
        continue;
      }
      if (label === "easier" || label === "regress" || label === "regression") {
        regressions.push(detail);
      } else if (label === "progress" || label === "progression") {
        progressions.push(detail);
      } else if (label.startsWith("stop")) {
        stopRules.push(detail);
      } else {
        substitutions.push(detail);
      }
      continue;
    }
    if (COACHING_PURE_STOP_RE.test(cue)) {
      stopRules.push(cue);
      continue;
    }
    registerCue(cue);
  }

  const cueKeys = new Set<string>();
  const cueKey = (cue: string) => cue.toLowerCase().replace(/\band\b/g, " ")
    .replace(/[^\p{L}\p{N}]+/gu, " ").trim();
  const uniqueCues = dedupeBlockText(cues).filter(cue => {
    const key = cueKey(cue);
    if (cueKeys.has(key)) return false;
    cueKeys.add(key);
    return true;
  });
  return {
    cues: uniqueCues.filter(cue => !uniqueCues.some(other =>
      other !== cue && cueKey(other).startsWith(`${cueKey(cue)} `))),
    stopRules: dedupeBlockText(stopRules),
    regressions: dedupeBlockText(regressions),
    substitutions: dedupeBlockText(substitutions),
    progressions: dedupeBlockText(progressions),
    prescription: dedupeBlockText(prescriptions).join(" ") || null,
  };
}

const EMBEDDED_STOP_RULE_LABEL_RE = /\bstop(?:\s+rule)?\s*:\s*/i;
const PROGRAMMING_ONLY_PROGRESSION_RE =
  /^\s*(?:maintain(?:\s+(?:the|this))?\s+dose|keep\s+(?:the\s+)?dose\s+small|do\s+not\s+(?:add|increase)\s+(?:sets?|volume)|no\s+(?:set|volume)\s+increase)\b/i;
const NEGATED_PROGRESSION_RE =
  /\b(?:do\s+not|don't|never)\s+(?:add|increase|progress|advance|raise|build|extend)\b/i;
const POSITIVE_PROGRESSION_RE =
  /\b(?:progress|advance|increase|raise|build|extend|heavier|more\s+resistance|reduce\s+assistance)\b/i;
const TAPER_PROGRAMMING_RE = /\b(?:taper|fight[ -]?week|sharpness|freshness)\b/i;

function stripStopRuleLabel(value: string): string {
  return value.replace(/^\s*stop(?:\s+rule)?\s*:\s*/i, "").trim();
}

// "Progress: none this block." states that nothing advances; it is no rule.
const NULL_PROGRESSION_RE =
  /^\s*(?:none|n\/?a|no(?:ne)?\s+progression|no\s+change|hold|not\s+applicable)(?:\s+(?:this|for\s+this)\s+(?:block|week|phase|camp))?\s*[.!]?\s*$/i;

export function isNullProgression(text: string | null | undefined): boolean {
  return NULL_PROGRESSION_RE.test((text ?? "").replace(/^\s*Progress(?:ion)?:\s*/i, ""));
}

function isProgrammingOnlyProgression(text: string): boolean {
  if (isNullProgression(text)) return true;
  if (NEGATED_PROGRESSION_RE.test(text)) {
    return PROGRAMMING_ONLY_PROGRESSION_RE.test(text) || TAPER_PROGRAMMING_RE.test(text);
  }
  if (POSITIVE_PROGRESSION_RE.test(text)) {
    return false;
  }
  return PROGRAMMING_ONLY_PROGRESSION_RE.test(text);
}

function dedupeStopRules(values: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values) {
    const text = stripStopRuleLabel(value);
    const key = text.toLowerCase().replace(/[.\s]+$/, "");
    if (!text || seen.has(key)) {
      continue;
    }
    seen.add(key);
    result.push(text);
  }
  return result;
}

export function getBlockAdjustmentDisplay(
  block: StructuredBlock | null | undefined,
): { progression: string | null; stopRules: string[] } {
  const explicitStops = getStringList(block?.stop_rules);
  const execution = getBlockExecutionDisplay(block);
  const progressionCandidates = [
    cleanText(block?.progression_rule),
    ...execution.progressions,
  ].filter((value): value is string => value !== null);
  let progression: string | null = null;
  const embeddedStops: string[] = [];

  for (const rawProgression of progressionCandidates) {
    let candidate: string | null = rawProgression.replace(/^Progress(?:ion)?:\s*/i, "");
    const match = EMBEDDED_STOP_RULE_LABEL_RE.exec(rawProgression);
    if (match) {
      candidate = cleanText(rawProgression.slice(0, match.index).replace(/[\s—–\-:;,.]+$/, ""));
      const stop = cleanText(rawProgression.slice(match.index + match[0].length));
      if (stop) embeddedStops.push(stop);
    } else if (isStopRuleText(rawProgression)) {
      candidate = null;
      embeddedStops.push(rawProgression);
    }
    if (candidate && isProgrammingOnlyProgression(candidate)) candidate = null;
    if (!progression && candidate) progression = candidate;
  }

  return {
    progression,
    stopRules: dedupeStopRules([
      ...explicitStops,
      ...execution.stopRules,
      ...embeddedStops,
    ]),
  };
}

// A stop rule reads like "Stop on sharp pain." / "Stop the set if punch speed
// drops" / "stop if the ankle flares". The conversion model frequently drops
// these into a block's `progression_rule` (its only free-text "what to do"
// field), so the renderer must not label a stop rule as "Progress" — that told
// the athlete to ADVANCE on a safety cue. Detect the stop-rule shape so it can
// be labelled correctly.
const STOP_RULE_RE = /^\s*stop(?!-)\b|(?<!\bdo\snot\s|don't\s|never\s)(?<!-)\bstop (?:the set|on|if|when|immediately)\b/i;

/** Whether a `progression_rule` string is really a stop/safety rule. */
export function isStopRuleText(text: string | null | undefined): boolean {
  const clean = cleanText(text);
  return clean !== null && STOP_RULE_RE.test(clean);
}

/** The label a block's `progression_rule` should render under: a stop rule is
 * surfaced as "Stop rule", genuine progression stays "Progress". */
export function progressionRuleLabel(text: string | null | undefined): "Progress" | "Stop rule" {
  return isStopRuleText(text) ? "Stop rule" : "Progress";
}

/** A clean list of non-empty strings from a possibly-null/non-array value. */
export function getStringList(value: string[] | null | undefined): string[] {
  return safeArray(value)
    .map((item) => cleanText(item))
    .filter((item): item is string => item !== null);
}

/** The mindset anchor as label/value lines (Intent, Focus, Reset, Anchor,
 * Context), in that order, skipping any field that is blank. Empty when the
 * anchor has no usable line, so the renderer can hide the card. */
export function getMindsetLines(
  anchor:
    | {
        intent?: string | null;
        focus_cue?: string | null;
        reset_cue?: string | null;
        confidence_anchor?: string | null;
        context?: string | null;
      }
    | null
    | undefined,
): { label: string; value: string }[] {
  if (!isObject(anchor)) {
    return [];
  }
  const lines: { label: string; value: string }[] = [];
  const intent = cleanText(anchor.intent);
  const focus = cleanText(anchor.focus_cue);
  const reset = cleanText(anchor.reset_cue);
  const confidence = cleanText(anchor.confidence_anchor);
  const context = cleanText(anchor.context);
  // Cues are authored inconsistently (some sentence-case, most lower-case); the
  // card reads better when each one starts with a capital, so normalise the first
  // letter for display without touching the rest of the phrase.
  if (intent) lines.push({ label: "Intent", value: capitalizeFirst(intent) });
  if (focus) lines.push({ label: "Focus", value: capitalizeFirst(focus) });
  if (reset) lines.push({ label: "Reset", value: capitalizeFirst(reset) });
  if (confidence) lines.push({ label: "Anchor", value: capitalizeFirst(confidence) });
  if (context) lines.push({ label: "Context", value: capitalizeFirst(context) });
  return lines;
}

const IMPORTANT_SESSION_CONTEXT_RE =
  /\b(?:injur(?:y|ies)|rehab(?:ilitation)?|prehab|pain|sore(?:ness)?|irritation|strain|sprain|tendon|physio(?:therapy)?|medical|restriction|restricted|modified|limit(?:ed|ing)?|avoid(?:ing)?|taper|fight[ -]?(?:week|day)|weigh[ -]?in|weight[ -]?cut|freshness|return(?:ing)? to (?:training|play))\b/i;
const COACH_SESSION_DEPENDENCY_RE =
  /\b(?:coach(?:[- ]?led)?|gym|sparr(?:ing)?|contact)\b.*\b(?:session|work|block|day|training)\b|\b(?:session|work|block|day|training)\b.*\b(?:coach(?:[- ]?led)?|gym|sparr(?:ing)?|contact)\b/i;

function isImportantSessionContext(value: string): boolean {
  return IMPORTANT_SESSION_CONTEXT_RE.test(value) || COACH_SESSION_DEPENDENCY_RE.test(value);
}

function coachCueValue(values: Array<string | null>): string | null {
  const seen = new Set<string>();
  const cues = values.filter((value): value is string => {
    if (!value) {
      return false;
    }
    const normalized = value.toLowerCase().replace(/\s+/g, " ").replace(/[.\s]+$/, "");
    if (seen.has(normalized)) {
      return false;
    }
    seen.add(normalized);
    return true;
  });
  return cues.length > 0 ? cues.map(capitalizeFirst).join(" · ") : null;
}

/**
 * The compact, athlete-facing session coaching layer. The intake-derived mental
 * framing remains intact, but intent and confidence are one optional Coach cue
 * rather than separate labels. Context is reserved for constraints or a
 * coach-session dependency so generic planning rationale does not slow down the
 * in-session scan.
 */
/** Casefold + whitespace/trailing-punctuation normalise for equality checks, so
 * "Mobility and low-noise speed." matches "mobility and low-noise speed". */
export function normalizeForDedupe(value: string | null | undefined): string | null {
  const clean = cleanText(value ?? null);
  if (!clean) {
    return null;
  }
  return clean.toLowerCase().replace(/\s+/g, " ").replace(/[.\s]+$/, "");
}

export function getSessionCoachingLines(
  anchor: Parameters<typeof getMindsetLines>[0],
): MindsetLine[] {
  if (!isObject(anchor)) {
    return [];
  }
  const focus = cleanText(anchor.focus_cue);
  const reset = cleanText(anchor.reset_cue);
  const intent = cleanText(anchor.intent);
  const confidence = cleanText(anchor.confidence_anchor);
  const context = cleanText(anchor.context);
  const coachCue = coachCueValue([intent, confidence]);
  const lines: MindsetLine[] = [];

  // One sentence, one line. A payload that puts the SAME cue in several slots —
  // the Fight Visualisation bank carries a single trusted cue, and older saved
  // cards replicated it across intent/focus/reset — printed it as "Focus",
  // "Reset" and "Coach cue" on one card. The first slot that carries it keeps
  // it; the repeats are dropped rather than restated.
  const seen = new Set<string>();
  const push = (label: string, value: string) => {
    const key = normalizeForDedupe(value);
    if (!key || seen.has(key)) {
      return;
    }
    seen.add(key);
    lines.push({ label, value });
  };

  if (focus) push("Focus", capitalizeFirst(focus));
  if (reset) push("Reset", capitalizeFirst(reset));
  if (coachCue) push("Coach cue", coachCue);
  if (context && isImportantSessionContext(context)) {
    push("Context", capitalizeFirst(context));
  }
  return lines;
}

export function splitMindsetLines(
  anchor: Parameters<typeof getMindsetLines>[0],
): { primary: MindsetLine[]; secondary: MindsetLine[] } {
  const lines = getMindsetLines(anchor);
  return {
    primary: lines,
    secondary: [],
  };
}

export function redFlagView(
  rule:
    | { display_text?: string | null; action?: string | null; severity?: string | null }
    | null
    | undefined,
): { text: string | null; action: string | null; severityLabel: string | null } {
  const text = cleanText(rule?.display_text);
  const rawAction = cleanText(rule?.action);
  const action =
    rawAction && !isRawEnumLabel(rawAction) && rawAction !== text ? rawAction : null;
  const severity = cleanText(rule?.severity);
  const severityLabel = severity ? formatPlanLabel(severity) : null;
  return { text, action, severityLabel };
}

// --- deterministic (Stage 1) athlete-safe nutrition + recovery --------------

const PHASE_ORDER = ["GPP", "SPP", "TAPER", "FIGHT_WEEK", "REINTEGRATION"];

/** Stable key used to attach deterministic support to matching week phases. */
export function normalizeSupportPhaseKey(value: unknown): string | null {
  const token = cleanText(value)
    ?.toLowerCase()
    .replace(/[\s\-/]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_|_$/g, "");
  if (!token) {
    return null;
  }
  if (token === "gpp" || token.includes("general_prep") || token.includes("general_preparation")) {
    return "gpp";
  }
  if (token === "spp" || token.includes("specific_prep") || token.includes("specific_preparation")) {
    return "spp";
  }
  if (token === "taper") {
    return "taper";
  }
  if (token.includes("fight_week") || token.includes("fight_week_taper")) {
    return "fight_week";
  }
  if (token.includes("reintegration")) {
    return "reintegration";
  }
  return token;
}

function orderedPhaseEntries<T>(byPhase: Record<string, T> | null | undefined): {
  phase: string;
  entry: T;
}[] {
  if (!isObject(byPhase)) {
    return [];
  }
  const keys = Object.keys(byPhase);
  keys.sort((a, b) => {
    const ia = PHASE_ORDER.indexOf(a.toUpperCase());
    const ib = PHASE_ORDER.indexOf(b.toUpperCase());
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });
  return keys
    .filter((key) => isObject(byPhase[key]))
    .map((phase) => ({ phase, entry: byPhase[phase] }));
}

/** Ordered athlete-safe deterministic nutrition phases (empty when absent). */
export function getDeterministicNutritionPhases(
  plan: StructuredPlan | null | undefined,
): { phase: string; entry: DeterministicNutritionPhase }[] {
  return orderedPhaseEntries(plan?.deterministic_support?.nutrition?.by_phase);
}

/** Ordered athlete-safe deterministic recovery phases (empty when absent). */
export function getDeterministicRecoveryPhases(
  plan: StructuredPlan | null | undefined,
): { phase: string; entry: DeterministicRecoveryPhase }[] {
  return orderedPhaseEntries(plan?.deterministic_support?.recovery?.by_phase);
}

/** "112–140 g/day" / "up to 350 g/day" / "from 30 g/day" / null. */
export function formatMacroRange(
  range: DeterministicMacroRange | null | undefined,
  unit: string,
): string | null {
  if (!isObject(range)) {
    return null;
  }
  // Reject NaN/Infinity/negative so a malformed macro never prints "NaN g/day"
  // or a nonsensical negative intake. Zero is allowed (a legitimate "0 g" line).
  const finiteNonNegative = (n: unknown): n is number =>
    typeof n === "number" && Number.isFinite(n) && n >= 0;
  const min = finiteNonNegative(range.min) ? range.min : null;
  const max = finiteNonNegative(range.max) ? range.max : null;
  if (min != null && max != null) {
    return `${min}–${max} ${unit}`;
  }
  if (max != null) {
    return `up to ${max} ${unit}`;
  }
  if (min != null) {
    return `from ${min} ${unit}`;
  }
  return null;
}

/** A macro line combining its numeric range and any deterministic note. */
export function macroLine(
  range: DeterministicMacroRange | null | undefined,
  unit: string,
): string | null {
  const numeric = formatMacroRange(range, unit);
  const note = cleanText(range?.note);
  if (numeric && note) return `${numeric} (${note})`;
  return numeric || note || null;
}

/** Athlete-safe weight-cut summary (risk band + supervision) — never dosing. */
export function formatWeightCutBand(
  weightCut: DeterministicWeightCut | null | undefined,
): { band: string; supervisionRequired: boolean } | null {
  if (!isObject(weightCut)) {
    return null;
  }
  const band = cleanText(weightCut.risk_band);
  if (!band || band.toLowerCase() === "none") {
    return null;
  }
  return { band, supervisionRequired: weightCut.supervision_required === true };
}

const WEIGHT_CUT_RISK_RANK: Record<string, number> = {
  none: 0,
  inactive: 0,
  low: 1,
  mild: 1,
  moderate: 2,
  medium: 2,
  amber: 2,
  elevated: 2,
  high: 3,
  severe: 3,
  red: 3,
  critical: 4,
  extreme: 4,
  aggressive: 4,
};

/** The numeric rank of a risk-band token, or null when the token is missing or
 * unrecognised — so callers can tell "explicitly none/low" from "unknown / not
 * provided". Unknown risk is never treated as low. */
function recognizedWeightCutRiskRank(value: unknown): number | null {
  const token = cleanText(value)?.toLowerCase().replace(/[\s-]+/g, "_");
  if (!token) {
    return null;
  }
  if (WEIGHT_CUT_RISK_RANK[token] != null) {
    return WEIGHT_CUT_RISK_RANK[token];
  }
  const matches = Object.entries(WEIGHT_CUT_RISK_RANK)
    .filter(([key]) => token.includes(key))
    .map(([, rank]) => rank);
  return matches.length > 0 ? Math.max(...matches) : null;
}

/** All explicitly-present weight-cut risk-band tokens across the plan's warning
 * and deterministic phases. Empty when the plan states no risk band at all. */
function presentWeightCutRiskTokens(plan: StructuredPlan | null | undefined): string[] {
  const raw: (string | null)[] = [cleanText(plan?.nutrition?.weight_cut_warning?.risk_level)];
  for (const { entry } of getDeterministicNutritionPhases(plan)) {
    raw.push(cleanText(entry.weight_cut?.risk_band));
  }
  for (const { entry } of getDeterministicRecoveryPhases(plan)) {
    raw.push(cleanText(entry.weight_cut?.risk_band));
  }
  return raw.filter((token): token is string => token !== null);
}

/**
 * Whether the plan's weight-cut risk is EXPLICITLY known to be below moderate
 * (low / mild / none-inactive). Returns false when risk is missing, unknown /
 * unrecognised, or moderate-and-above — those must never be de-emphasised. A
 * single unrecognised or at-or-above-moderate band anywhere pins it to false.
 */
function isWeightCutRiskExplicitlyBelowModerate(plan: StructuredPlan | null | undefined): boolean {
  const tokens = presentWeightCutRiskTokens(plan);
  if (tokens.length === 0) {
    return false; // missing / not provided → never de-emphasise
  }
  const ranks = tokens.map(recognizedWeightCutRiskRank);
  if (ranks.some((rank) => rank === null)) {
    return false; // an unknown band is treated as unknown risk → full weight
  }
  const maxRank = Math.max(...(ranks as number[]));
  return maxRank < WEIGHT_CUT_RISK_RANK.moderate;
}

// Red-flag severities that must ALWAYS render at full weight — an explicit
// high/critical safety rule is never faded regardless of computed cut risk.
const PROMINENT_RED_FLAG_SEVERITIES = new Set(["red", "critical", "high", "severe", "extreme"]);

/** True when a red-flag rule's severity must never be visually de-emphasised. */
export function isProminentRedFlagSeverity(severity: string | null | undefined): boolean {
  const token = cleanText(severity)?.toLowerCase();
  return token != null && PROMINENT_RED_FLAG_SEVERITIES.has(token);
}

function isWeightCutSymptomEscalationText(text: string): boolean {
  const normalized = text.toLowerCase();
  return (
    /\bweight[-\s]?cut\b/.test(normalized) &&
    (
      /\bsymptoms?\s+worsen\b/.test(normalized) ||
      /\bworsen(?:s|ed|ing)?\b/.test(normalized) ||
      /\blightheaded(?:ness)?\b/.test(normalized) ||
      /\bdizz(?:y|iness)\b/.test(normalized) ||
      /\bdehydrat(?:ed|ion|e)?\b/.test(normalized)
    )
  );
}

/**
 * Whether a weight-cut symptom safety line should be visually DE-EMPHASISED —
 * shown, but softened — because the athlete's computed cut risk is EXPLICITLY
 * below moderate (low / mild / none). It is never suppressed: a symptom-based
 * stop/escalate rule ("if you feel dizzy/dehydrated, stop and report") is
 * generically safe advice, and a predicted risk band does not make actual
 * symptoms unimportant. De-emphasis is deliberately conservative — it applies
 * ONLY when:
 *   - the text is a weight-cut symptom escalation line, AND
 *   - the plan explicitly states a below-moderate risk band (never on missing,
 *     unknown, moderate, or higher risk), AND
 *   - the rule's own severity is not an explicit high/critical signal
 *     (``severity``, when supplied, overrides any de-emphasis).
 * Everything else renders at full weight, so a genuinely high-risk or unknown
 * situation is never faded.
 */
export function isDeEmphasisedWeightCutSafety(
  plan: StructuredPlan | null | undefined,
  text: string | null | undefined,
  severity?: string | null,
): boolean {
  const clean = cleanText(text);
  if (clean === null || !isWeightCutSymptomEscalationText(clean)) {
    return false;
  }
  if (isProminentRedFlagSeverity(severity)) {
    return false; // an explicit red/critical/high rule is never faded
  }
  return isWeightCutRiskExplicitlyBelowModerate(plan);
}

/** The athlete-safe metric rows for one deterministic nutrition phase. */
export function nutritionPhaseRows(
  entry: DeterministicNutritionPhase | null | undefined,
): { label: string; value: string }[] {
  if (!isObject(entry)) {
    return [];
  }
  const rows: { label: string; value: string }[] = [];
  const push = (label: string, value: string | null) => {
    if (value) rows.push({ label, value });
  };
  push("Protein", macroLine(entry.protein_g_per_day, "g/day"));
  push("Carbs", macroLine(entry.carbs_g_per_day, "g/day"));
  push("Fats", macroLine(entry.fats_g_per_day, "g/day"));
  push("Hydration", macroLine(entry.hydration_ml_per_day, "ml/day"));
  push("Meals", cleanText(entry.meal_structure));
  const fuel = entry.fuel_timing;
  if (isObject(fuel)) {
    push("Fuel (pre)", cleanText(fuel.pre));
    push("Fuel (intra)", cleanText(fuel.intra));
    push("Fuel (post)", cleanText(fuel.post));
  }
  const fatigue = cleanText(entry.fatigue_adjustment);
  if (fatigue) push("Fatigue adjustment", `${fatigue} fatigue support`);
  return rows;
}

/** The athlete-safe recovery view for one deterministic recovery phase. */
export function recoveryPhaseView(entry: DeterministicRecoveryPhase | null | undefined): {
  sleep: string | null;
  coreStrategies: string[];
  phaseFocus: string[];
  fatigue: string[];
  ageAdjustments: string[];
  weightCut: { band: string; supervisionRequired: boolean } | null;
} {
  const e = isObject(entry) ? entry : {};
  // Sleep hours must be finite and positive: typeof alone lets NaN through
  // (NaN is a "number"), which would render as "NaN–9 h/night".
  const sleepRange = safeArray(e.sleep_hours_target).filter(finitePositiveNumber);
  const sleep =
    sleepRange.length === 2
      ? `${sleepRange[0]}–${sleepRange[1]} h/night`
      : sleepRange.length === 1
        ? `${sleepRange[0]} h/night`
        : null;
  return {
    sleep,
    coreStrategies: getStringList(e.core_strategies),
    phaseFocus: getStringList(e.phase_focus),
    // fatigue_flags (high) or fatigue_notes (moderate) — whichever is present.
    fatigue: getStringList(e.fatigue_flags).concat(getStringList(e.fatigue_notes)),
    ageAdjustments: getStringList(e.age_adjustments),
    weightCut: formatWeightCutBand(e.weight_cut),
  };
}

/** True when the plan carries deterministic athlete-safe nutrition data. */
export function hasDeterministicNutrition(plan: StructuredPlan | null | undefined): boolean {
  return getDeterministicNutritionPhases(plan).some(
    ({ entry }) =>
      nutritionPhaseRows(entry).length > 0 || formatWeightCutBand(entry.weight_cut) !== null,
  );
}

/** True when the plan carries deterministic athlete-safe recovery data. */
export function hasDeterministicRecovery(plan: StructuredPlan | null | undefined): boolean {
  return getDeterministicRecoveryPhases(plan).some(({ entry }) => {
    const view = recoveryPhaseView(entry);
    return (
      view.sleep !== null ||
      view.coreStrategies.length > 0 ||
      view.phaseFocus.length > 0 ||
      view.fatigue.length > 0 ||
      view.ageAdjustments.length > 0 ||
      view.weightCut !== null
    );
  });
}

/** True when nutrition carries any displayable text. */
export function hasNutrition(plan: StructuredPlan | null | undefined): boolean {
  const nutrition = plan?.nutrition;
  if (!isObject(nutrition)) {
    return false;
  }
  return Boolean(
    cleanText(nutrition.summary) ||
      cleanText(nutrition.daily_focus) ||
      cleanText(nutrition.training_day_guidance) ||
      cleanText(nutrition.fight_week_guidance) ||
      cleanText(nutrition.weight_cut_warning?.display_text),
  );
}

export type PlanNoteView = { category: string; label: string | null; text: string };

const PLAN_NOTE_CATEGORY_LABELS: Record<string, string> = {
  weight_cut: "Weight cut",
  injury: "Injury",
  nutrition: "Nutrition",
  training: "Training",
  recovery: "Recovery",
  general: "Note",
};

/** Short title for a plan note: its own label, else a category fallback. */
export function planNoteLabel(note: PlanNoteView): string {
  if (note.label) {
    return note.label;
  }
  return PLAN_NOTE_CATEGORY_LABELS[note.category] ?? PLAN_NOTE_CATEGORY_LABELS.general;
}

/** Plan-level "active notes" that carry displayable text (empty when absent). */
export function getPlanNotes(plan: StructuredPlan | null | undefined): PlanNoteView[] {
  return safeArray(plan?.plan_notes)
    .filter(isObject)
    .map((note) => {
      const text = cleanText(note.text);
      if (!text) {
        return null;
      }
      const category = cleanText(note.category)?.toLowerCase().replace(/[-\s]+/g, "_") ?? "general";
      return { category, label: cleanText(note.label), text } satisfies PlanNoteView;
    })
    .filter((note): note is PlanNoteView => note !== null);
}

/** Fallback safety lines from active notes when explicit red-flag rules are
 * absent. A weight-cut symptom safety line is ALWAYS surfaced here (it is a
 * stop/escalate rule); the renderer de-emphasises it below moderate risk rather
 * than hiding it. */
export function getFallbackSafetyNotes(plan: StructuredPlan | null | undefined): PlanNoteView[] {
  const safetyAction =
    /(?:\bstop\s*:|\bstop\s+(?:training|the (?:exercise|session|set)|immediately|if|when|on)\b|\bseek (?:urgent )?medical (?:help|attention|review)\b|\breport (?:to|any|new|worsening)\b|\bdo not train\b|\bno training\b)/i;
  const symptomEscalation =
    /\b(?:dizz(?:y|iness)|faint(?:ing)?|lightheaded(?:ness)?|chest pain|shortness of breath|breathing (?:difficulty|worsens)|bleed(?:ing)?|wound (?:opens|worsens)|pain (?:increases|worsens|above|over)|dehydrat(?:ed|ion)|vomit(?:ing)?|confus(?:ed|ion))\b/i;
  const escalationVerb = /\b(?:stop|report|seek|urgent|worsen|escalat)\b/i;
  return getPlanNotes(plan).filter(
    (note) =>
      safetyAction.test(note.text) ||
      (symptomEscalation.test(note.text) && escalationVerb.test(note.text)),
  );
}

/** Red-flag rules that have something to display. Explicit symptom-based safety
 * rules (incl. weight-cut symptom escalation) are never suppressed by risk band
 * — they always render; the renderer softens the below-moderate ones. */
export function getDisplayableRedFlags(plan: StructuredPlan | null | undefined) {
  return safeArray(plan?.red_flag_rules)
    .filter(isObject)
    .filter((rule) => Boolean(cleanText(rule.display_text)));
}

/** Loose normalization for de-duplicating a note against a red-flag rule:
 *  lowercased, parentheticals dropped, all punctuation/whitespace collapsed to
 *  single spaces. Lets "…worsen, stop…" match "…worsen (lightheadedness), stop…". */
function normalizeForDup(text: string): string {
  return text
    .toLowerCase()
    .replace(/\([^)]*\)/g, " ")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

/**
 * Active notes with any note that merely restates a red-flag rule removed, so
 * the Red Flags card stays the single home for stop/report rules. A note counts
 * as a duplicate only on EXACT normalized equality with a red flag's normalized
 * display_text. Substring ("contains / is contained by") matching is
 * deliberately NOT used here: these are safety notes, and a looser match risks
 * hiding a note that merely shares wording with a flag but adds its own
 * instruction. Over-suppressing safety copy is the more dangerous failure, so we
 * keep the stricter equality check. Notes shorter than the guard length are
 * always kept. The ``normalizeForDup`` helper still absorbs the common
 * parenthetical-only difference, which is the case this dedup targets.
 */
export function getActiveNotesExcludingRedFlags(
  plan: StructuredPlan | null | undefined,
): PlanNoteView[] {
  // No risk-gated hiding of weight-cut symptom notes here: a symptom-based
  // safety line is never suppressed by risk band (see isDeEmphasisedWeightCutSafety).
  // Duplication with the Red Flags card is prevented by the exact-match dedup
  // below, including when a genuine stop/escalation note supplies the fallback
  // Red Flags content because no explicit rule exists.
  const notes = getPlanNotes(plan);
  const explicitFlags = getDisplayableRedFlags(plan);
  const fallbackFlags = explicitFlags.length === 0 ? getFallbackSafetyNotes(plan) : [];
  const flagTexts = [
    ...explicitFlags.map((rule) => cleanText(rule.display_text)),
    ...fallbackFlags.map((note) => note.text),
  ]
    .filter((text): text is string => text !== null)
    .map(normalizeForDup)
    .filter((text) => text.length >= 12);
  if (flagTexts.length === 0) {
    return notes;
  }
  return notes.filter((note) => {
    const noteNorm = normalizeForDup(note.text);
    if (noteNorm.length < 12) {
      return true;
    }
    return !flagTexts.some((flag) => flag === noteNorm);
  });
}

// The week heading must read as a glanceable label, not a paragraph. The LLM is
// told to keep week_goal short, but plans (and older saved plans) can still
// carry a full multi-clause sentence, so we shorten deterministically: keep the
// first clause (up to the first ; or .) when that already fits in 4 words, else
// hard-cap at 4 words with an ellipsis. Goals already short are returned verbatim
// so their punctuation (e.g. a trailing period) is preserved.
const WEEK_GOAL_MAX_WORDS = 4;
function shortenWeekGoal(goal: string): string {
  const words = goal.split(/\s+/).filter(Boolean);
  if (words.length <= WEEK_GOAL_MAX_WORDS) {
    return goal;
  }
  const firstClause = goal.split(/[;.](?=\\s|$)/)[0].trim();
  const clauseWords = firstClause.split(/\s+/).filter(Boolean);
  if (clauseWords.length > 0 && clauseWords.length <= WEEK_GOAL_MAX_WORDS) {
    return firstClause;
  }
  return `${words.slice(0, WEEK_GOAL_MAX_WORDS).join(" ")}…`;
}

function lateFightCountdownStart(week: StructuredWeek | null | undefined): number | null {
  const labels = [
    formatCountdownLabel(week?.countdown_start),
    ...getDays(week).map((day) => formatCountdownLabel(day.countdown_label)),
  ];
  const distances = labels
    .map((label) => {
      const match = label?.match(/\bD-(\d+)\b/i);
      return match ? Number(match[1]) : null;
    })
    .filter((value): value is number => value !== null && Number.isFinite(value));
  return distances.length > 0 ? Math.max(...distances) : null;
}

/** Display-only fallback for countdown-led late-fight payloads saved before
 * week context repair existed. Explicit saved titles/phases always win. */
export function inferredLateFightWeekContext(
  week: StructuredWeek | null | undefined,
): { goal: string | null; phase: string | null } {
  const countdownStart = lateFightCountdownStart(week);
  if (countdownStart === null || countdownStart < 0 || countdownStart > 21) {
    return { goal: null, phase: null };
  }
  if (countdownStart >= 14) {
    return { goal: "Bridge Compression Week", phase: "TAPER" };
  }
  if (countdownStart >= 8) {
    return { goal: "Compressed Pre-Fight Week", phase: "TAPER" };
  }
  if (countdownStart === 7) {
    return { goal: "Sharpness Week", phase: "TAPER" };
  }
  if (countdownStart >= 5) {
    return { goal: "Sharpness & Freshness Window", phase: "TAPER" };
  }
  if (countdownStart >= 2) {
    return { goal: "Sharpness Sessions", phase: "TAPER" };
  }
  if (countdownStart === 1) {
    return { goal: "Primer Day", phase: "TAPER" };
  }
  return { goal: "Fight-Day Protocol", phase: "TAPER" };
}

export function resolvedWeekPhase(week: StructuredWeek | null | undefined): string | null {
  return cleanText(week?.phase_label) || inferredLateFightWeekContext(week).phase;
}

export function weekLabel(week: StructuredWeek | null | undefined): string {
  const goal = cleanText(week?.week_goal) || inferredLateFightWeekContext(week).goal;
  const index =
    typeof week?.week_index === "number" && Number.isFinite(week.week_index)
      ? week.week_index
      : null;
  const base = index != null ? `Week ${index}` : "Week";
  return goal ? `${base}: ${shortenWeekGoal(goal)}` : base;
}

// --- session-less day classification ----------------------------------------
//
// A coach-led / sparring / technical day legitimately carries no app S&C
// blocks (the contact load is owned by the coach), so the converter emits it as
// a day with an empty `sessions` array. Rather than relying on the LLM to build
// a session object for these — which wastes tokens and is a frequent source of
// dropped days — we deterministically derive a self-contained card from the
// day's own `today_card.headline` + `day_type`. Only a genuine rest/recovery
// day (or a truly empty day) falls through to "Rest day.".

const REST_DAY_TYPES = new Set(["rest", "recovery"]);
const REST_HEADLINE_RE = /\b(rest|recovery|off|unavailable|no planned session|no session planned)\b/i;
// `technical` is checked before `sparring` so a "technical only / no hard
// sparring" headline is not mislabelled as a sparring day by the stray
// "sparring" token, and `coach_led` is the catch-all for coach-owned contact.
const LIGHT_COMBAT_RE = /\b(light(?:[\s-]+technical)?[\s-]+combat|support[\s-]+work)\b/i;
const TECHNICAL_RE = /\b(technical|skill|drill|pad\s?work|pads|mitts?|footwork|shadow)/i;
const SPARRING_RE = /\bspar(?:r(?:ing|ed)|s)?\b/i;
const COACH_LED_RE = /\bcoach/i;

// Existing saved plans can still carry the old generic technical-contact title.
// Project that legacy title from the day's D-day at render time so the taper
// ladder applies immediately without requiring plan regeneration. Explicit
// countdown-specific titles from newer plans are preserved verbatim.
const GENERIC_TECHNICAL_CONTACT_TITLES = new Set([
  "technical-only combat",
  "technical combat",
]);

function technicalContactTitleForCountdown(headline: string, countdownLabel: unknown): string {
  if (!GENERIC_TECHNICAL_CONTACT_TITLES.has(headline.toLowerCase())) {
    return headline;
  }
  const countdown = formatCountdownLabel(countdownLabel);
  const match = countdown?.match(/^D-(\d+)$/i);
  const dDay = match ? Number(match[1]) : null;
  if (dDay == null || !Number.isFinite(dDay)) {
    return headline;
  }
  if (dDay >= 8 && dDay <= 17) {
    return "Controlled fight-speed technical rounds";
  }
  if (dDay >= 5 && dDay <= 7) {
    return "Technical rhythm only";
  }
  if (dDay >= 2 && dDay <= 4) {
    return "Technical touch — pads / shadow";
  }
  if (dDay >= 0 && dDay <= 1) {
    return "Technical activation — no contact";
  }
  return headline;
}

// Headlines the planner only ever stamps on a declared hard-sparring day it
// converted to technical work (D-17 inward): the canonical "technical only" /
// "no hard sparring" wording, its legacy spelling, and the countdown ladder above. A technical / light day the athlete
// declared keeps its own wording and must never read as a converted one.
const CONVERTED_TECHNICAL_LADDER_TITLES = new Set([
  "controlled fight-speed technical rounds",
  "technical rhythm only",
  "technical touch — pads / shadow",
  "technical activation — no contact",
]);
const CONVERTED_TECHNICAL_RE = /\btechnical[\s-]+only\b|\bno\s+hard\s+sparring\b/i;

/** Whether a technical contact headline marks a converted hard-sparring day. */
export function isConvertedTechnicalHeadline(headline: string): boolean {
  const normalized = headline.trim().toLowerCase();
  return (
    CONVERTED_TECHNICAL_RE.test(normalized) ||
    GENERIC_TECHNICAL_CONTACT_TITLES.has(normalized) ||
    CONVERTED_TECHNICAL_LADDER_TITLES.has(normalized)
  );
}

export type SessionlessDayKind =
  | "coach_led"
  | "light_combat"
  | "sparring"
  | "technical"
  | "scheduled"
  | "rest";

export type SessionlessDayView = {
  kind: SessionlessDayKind;
  title: string;
  /** Short tag for the day kind, or null when no kind tag should show. */
  tag: string | null;
  /** Whether to surface the "no app S&C — your own hard sparring/contact work" note. */
  coachLed: boolean;
  /** A technical day the planner converted from declared hard sparring. */
  converted: boolean;
};

const SESSIONLESS_DAY_TAGS: Record<SessionlessDayKind, string | null> = {
  coach_led: "Contact",
  light_combat: "Technical",
  sparring: "Sparring",
  technical: "Technical",
  scheduled: null,
  rest: null,
};

/** The day kind for a coach-led/contact headline (scheduled when none matches). */
function coachLedKindFromHeadline(headline: string): SessionlessDayKind {
  if (LIGHT_COMBAT_RE.test(headline)) {
    return "light_combat";
  }
  if (TECHNICAL_RE.test(headline)) {
    return "technical";
  }
  if (SPARRING_RE.test(headline)) {
    return "sparring";
  }
  if (COACH_LED_RE.test(headline)) {
    return "coach_led";
  }
  return "scheduled";
}

/**
 * Deterministically resolve how a day with no app sessions should render.
 *
 * Coach-led/sparring/technical days get their own card titled from the day
 * headline so a mostly-coach-led camp does not collapse into a wall of
 * "Rest day.". A headline-less rest/recovery day (or an otherwise empty day)
 * is the only case that renders as a rest day.
 */
export function classifySessionlessDay(
  day: StructuredDay | null | undefined,
): SessionlessDayView {
  const headline = humanizeIfRawEnum(cleanText(day?.today_card?.headline));
  const dayType = cleanText(day?.day_type)?.toLowerCase() ?? null;

  if (headline) {
    const headlineKind = coachLedKindFromHeadline(headline);
    // A declared light-combat day is never converted sparring, whatever wording
    // an older plan stamped on it (see getCoachLedContactView).
    const kind =
      headlineKind === "technical" && isDeclaredLightCombatOnlyDay(day) ? "light_combat" : headlineKind;
    // A rest/recovery day_type whose headline does not clearly identify
    // coach-led combat / sparring / technical work stays a rest day (e.g.
    // day_type "rest" + "Full rest and mobility"), rather than a generic
    // "scheduled" day. day_type is only allowed to override an unclassified
    // headline — a headline that names real combat/coach work always wins.
    if (kind === "scheduled" && dayType !== null && REST_DAY_TYPES.has(dayType)) {
      return {
        kind: "rest",
        title: REST_HEADLINE_RE.test(headline) ? headline : "No planned session",
        tag: null,
        coachLed: false,
        converted: false,
      };
    }
    return {
      kind,
      title:
        kind === "technical"
          ? technicalContactTitleForCountdown(headline, day?.countdown_label)
          : headline,
      tag: SESSIONLESS_DAY_TAGS[kind],
      coachLed:
        kind === "coach_led" ||
        kind === "sparring" ||
        kind === "technical",
      converted: kind === "technical" && isConvertedTechnicalHeadline(headline),
    };
  }

  // No headline to classify from: fall back to a plain rest day. The converter
  // is instructed to always headline a coach-led/sparring/technical day, so a
  // headline-less session-less day is treated as genuine rest.
  return { kind: "rest", title: "Rest day", tag: null, coachLed: false, converted: false };
}

function isDeclaredLightCombatOnlyDay(day: StructuredDay | null | undefined): boolean {
  const roles = Array.isArray(day?.planning_day_role_keys) ? day.planning_day_role_keys : [];
  return roles.includes("light_combat_day") && !roles.includes("hard_sparring_day");
}

export type CoachLedContactView = {
  kind: SessionlessDayKind;
  title: string;
  tag: string | null;
  /** A technical day the planner converted from declared hard sparring. */
  converted: boolean;
};

const REDUCED_DOSE_RE = /\breduced\s+dose\b/i;
// Mirrors _REDUCED_REASON_FALLBACK in api/structured_plan_sparring_reconcile.py,
// for plans saved before the server stamped today_card.contact_reason.
export const REDUCED_CONTACT_FALLBACK_REASON =
  "Reduced dose: It keeps this week's total sparring load manageable. Spar, but cut hard rounds and keep intensity controlled.";

/**
 * Why a declared hard-sparring day was deloaded, or null when it was not. The
 * server stamps a deterministic ``today_card.contact_reason`` from the
 * planner's reason codes; a reduced-dose card without one gets the fixed
 * fallback so it never renders unexplained. The reason is only trusted while
 * the day's contact still reads as reduced dose.
 */
export function getReducedContactReason(day: StructuredDay | null | undefined): string | null {
  const card = day?.today_card;
  const contactText = `${cleanText(card?.headline) ?? ""} ${cleanText(card?.coach_led_contact) ?? ""}`;
  // The day must still read as reduced: a stale reason on a day that is now
  // full hard or technical contact is never shown.
  if (!REDUCED_DOSE_RE.test(contactText)) {
    return null;
  }
  return cleanText(card?.contact_reason) ?? REDUCED_CONTACT_FALLBACK_REASON;
}

/**
 * Coach-owned contact (declared / downgraded sparring) that coexists with a
 * day's app sessions, or null when none is set. Driven by the deterministic
 * ``today_card.coach_led_contact`` field rather than the day headline (which a
 * session day uses for its own session title), so surfacing the contact never
 * clobbers the app session. The renderer shows it as a context block above the
 * session cards.
 */
export function getCoachLedContactView(
  day: StructuredDay | null | undefined,
): CoachLedContactView | null {
  const headline = humanizeIfRawEnum(cleanText(day?.today_card?.coach_led_contact));
  if (!headline) {
    return null;
  }
  // Plans saved before the reconcile fix stamped a declared light-combat day
  // with the converted-sparring wording ("Technical-only combat", or the taper
  // ladder). The day's role keys are deterministic: a light-combat day that
  // was never a hard-sparring day is the athlete's own technical session.
  if (isDeclaredLightCombatOnlyDay(day)) {
    return { kind: "light_combat", title: headline, tag: SESSIONLESS_DAY_TAGS.light_combat, converted: false };
  }
  const kind = coachLedKindFromHeadline(headline);
  const title =
    kind === "technical"
      ? technicalContactTitleForCountdown(headline, day?.countdown_label)
      : headline;
  return {
    kind,
    title,
    tag: SESSIONLESS_DAY_TAGS[kind],
    converted: kind === "technical" && isConvertedTechnicalHeadline(headline),
  };
}
