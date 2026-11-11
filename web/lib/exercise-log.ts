import { cleanText, finitePositiveNumber, isModeLikeReps, isTimeLikeReps } from "@/lib/structured-plan";
import { countOnlySetSummary, readSets, setSummary, type SetDraft } from "./set-entries";
import { formatRunTime, parseRunTime, RUN_TIME_MAX_SECONDS } from "./run-time";
import type {
  ExerciseLogActual,
  ExerciseLogReason,
  ExerciseLogRecord,
  ExerciseLogMeasure,
  ExerciseLogRequest,
  ExerciseLogStatus,
  ExerciseRecentLoad,
  StructuredBlock,
  StructuredSession,
} from "@/lib/types";

/**
 * The model behind in-session exercise logging: which numbers an exercise can
 * be logged with, how what the athlete typed compares with the prescription,
 * and how a saved log reads back. The prescription itself is never edited.
 */

export type LogFieldKey = "sets" | "reps" | "load" | "duration" | "work" | "rounds" | "distance" | "effort";

export type LogField = {
  key: LogFieldKey;
  label: string;
  /** Unit shown beside the input ("kg", "min"); null for bare counts. */
  unit: string | null;
  /** Unit sent to the API for measured fields; null for counts and RPE. */
  apiUnit: string | null;
  /** The prescribed value, or the prescribed range ("8-12" is [8, 12]). Null
   * when the plan gives no number for this field (the weight on the bar). */
  prescribed: [number, number] | null;
  /** Reps only: the server's permitted range above the prescribed number. A
   * count inside it is recorded as done, not as a change to the workout. */
  permitted?: [number, number];
  /** The prescription as written, shown as the input's placeholder. */
  hint: string | null;
  /** Entered and shown as a clock ("24:35"); the value is seconds. */
  clock?: boolean;
};

export const EXERCISE_LOG_REASONS: readonly ExerciseLogReason[] = ["equipment", "fatigue", "pain", "felt_strong"];

// Short answers to "why?", so a chip reads on its own and in a saved log line.
export const EXERCISE_LOG_REASON_LABELS: Record<ExerciseLogReason, string> = {
  equipment: "No kit",
  fatigue: "Too tired",
  pain: "Pain",
  felt_strong: "Felt strong",
};

export const EXERCISE_LOG_STATUS_LABELS: Record<ExerciseLogStatus, string> = {
  as_prescribed: "Done",
  modified: "Changed",
  skipped: "Skipped",
};

/** A value this many times over (or under) the prescription is confirmed before saving. */
const OUTLIER_RATIO = 3;
/** With no prescribed load to compare with, anything past this is confirmed. */
const OUTLIER_LOAD_KG = 400;

// Loads the athlete cannot put a number on.
const UNWEIGHED_LOAD_METHODS = new Set(["bodyweight", "band"]);

function timeUnit(unit: unknown): { unit: string; apiUnit: string } | null {
  const text = cleanText(unit)?.toLowerCase() ?? "";
  if (/^(s|sec|secs|second|seconds)$/.test(text)) return { unit: "sec", apiUnit: "seconds" };
  if (/^(m|min|mins|minute|minutes)$/.test(text)) return { unit: "min", apiUnit: "minutes" };
  return null;
}

function distanceUnit(unit: unknown): { unit: string; apiUnit: string } | null {
  const text = cleanText(unit)?.toLowerCase() ?? "";
  if (/^(m|meter|meters|metre|metres)$/.test(text)) return { unit: "m", apiUnit: "meters" };
  if (/^(km|kilometer|kilometers|kilometre|kilometres)$/.test(text)) return { unit: "km", apiUnit: "km" };
  if (/^(mi|mile|miles)$/.test(text)) return { unit: "mi", apiUnit: "miles" };
  return null;
}

const METERS_PER_UNIT: Record<string, number> = { meters: 1, km: 1000, miles: 1609.344 };
/** Shorter efforts are timed in seconds, not on a run clock. */
const MIN_TIMED_DISTANCE_METERS = 400;

function single(value: number): [number, number] {
  return [value, value];
}

/** "8", "8-12", "5 each side" as a prescribed rep count or range; null for
 * anything that is not a rep count ("30 seconds", "AMRAP"). */
function prescribedReps(reps: StructuredBlock["reps"]): { range: [number, number]; hint: string } | null {
  if (typeof reps === "number") {
    return finitePositiveNumber(reps) ? { range: single(reps), hint: String(reps) } : null;
  }
  const text = cleanText(reps);
  if (!text || isTimeLikeReps(text) || isModeLikeReps(text)) {
    return null;
  }
  const match = /^(\d+)(?:\s*[-–—]\s*(\d+))?(?![\d.])/.exec(text);
  if (!match) {
    return null;
  }
  const low = Number(match[1]);
  const high = match[2] ? Number(match[2]) : low;
  if (!finitePositiveNumber(low) || high < low) {
    return null;
  }
  return { range: [low, high], hint: text.replace(/\s*[-–—]\s*/, "–") };
}

function measured(
  key: LogFieldKey,
  label: string,
  value: StructuredBlock["duration"],
  resolveUnit: (unit: unknown) => { unit: string; apiUnit: string } | null,
): LogField | null {
  const amount = value?.value;
  const unit = resolveUnit(value?.unit);
  if (!finitePositiveNumber(amount) || !unit) {
    return null;
  }
  return { key, label, ...unit, prescribed: single(amount), hint: String(amount) };
}

/**
 * The numbers this exercise can be logged with, in display order. A field is
 * offered when the prescription carries it; the weight used is offered for
 * rep-based work, because the plan rarely prescribes one; RPE is always offered.
 */
export function logFieldsForBlock(block: StructuredBlock): LogField[] {
  const fields: LogField[] = [];
  const sets = finitePositiveNumber(block.sets) ? block.sets : null;
  const reps = prescribedReps(block.reps);
  if (sets !== null) {
    fields.push({ key: "sets", label: "Sets", unit: null, apiUnit: null, prescribed: single(sets), hint: String(sets) });
  }
  if (reps) {
    const permitted = progressionRepRange(block);
    fields.push({ key: "reps", label: "Reps", unit: null, apiUnit: null, prescribed: reps.range, hint: reps.hint,
      ...(permitted && permitted.join() !== reps.range.join() ? { permitted } : {}) });
  }
  const timed = [
    measured("duration", "Duration", block.duration, timeUnit),
    measured("work", "Work", block.work, timeUnit),
    measured("distance", "Distance", block.distance, distanceUnit),
  ].filter((field): field is LogField => field !== null);
  const loadMethod = !block.load && block.bodyweight_progression === true
    ? "bodyweight" : cleanText(block.load?.method)?.toLowerCase() ?? "";
  // Sets of a sprint or a hold are not weighed; sets of reps (or bare sets) are.
  const weighed = Boolean(reps) || (sets !== null && timed.length === 0);
  if (weighed && !UNWEIGHED_LOAD_METHODS.has(loadMethod)) {
    const loadUnit = cleanText(block.load?.unit)?.toLowerCase();
    const prescribedLoad =
      (loadUnit === "kg" || loadUnit === "lb") && finitePositiveNumber(block.load?.value)
        ? block.load.value
        : null;
    fields.push({
      key: "load",
      label: "Load",
      unit: loadUnit === "lb" ? "lb" : "kg",
      apiUnit: loadUnit === "lb" ? "lb" : "kg",
      prescribed: prescribedLoad === null ? null : single(prescribedLoad),
      hint: prescribedLoad === null ? null : String(prescribedLoad),
    });
  }
  const rounds = finitePositiveNumber(block.rounds) ? block.rounds : null;
  if (rounds !== null) {
    fields.push({ key: "rounds", label: "Rounds", unit: null, apiUnit: null, prescribed: single(rounds), hint: String(rounds) });
  }
  fields.push(...timed);
  // One continuous distance with no prescribed time (a 5k run) can still record
  // how long it took, the way load is offered for rep work: added, never a
  // change. Repeated or short efforts (4 × 20 m sprints) are left as they are.
  const distance = timed.find((field) => field.key === "distance");
  const meters = distance?.prescribed ? distance.prescribed[0] * (METERS_PER_UNIT[distance.apiUnit ?? ""] ?? 0) : 0;
  const singleEffort = (sets === null || sets === 1) && !finitePositiveNumber(block.rounds);
  if (singleEffort && meters >= MIN_TIMED_DISTANCE_METERS && !timed.some((field) => field.key === "duration" || field.key === "work")) {
    fields.push({ key: "duration", label: "Time", unit: null, apiUnit: "seconds", prescribed: null, hint: "mm:ss", clock: true });
  }

  const effort = block.effort;
  const prescribedRpe =
    cleanText(effort?.method)?.toUpperCase() === "RPE" && typeof effort?.value === "number" && finitePositiveNumber(effort.value)
      ? effort.value
      : null;
  fields.push({
    key: "effort",
    label: "RPE",
    unit: null,
    apiUnit: null,
    prescribed: prescribedRpe === null ? null : single(prescribedRpe),
    hint: prescribedRpe === null ? "1–10" : String(prescribedRpe),
  });
  return fields;
}

/** Whether the exercise has anything beyond RPE to put a different number on. */
export function hasLoggableNumbers(fields: LogField[]): boolean {
  return fields.some((field) => field.key !== "effort");
}

export type LogDraft = Partial<Record<LogFieldKey, string>>;

function parseEntry(text: string | undefined): number | null {
  const cleaned = (text ?? "").trim().replace(",", ".");
  if (!/^\d+(?:\.\d+)?$/.test(cleaned)) {
    return null;
  }
  const value = Number(cleaned);
  return Number.isFinite(value) ? value : null;
}

function formatNumber(value: number): string {
  return String(Math.round(value * 100) / 100);
}

function formatPrescribed(field: LogField): string {
  if (!field.prescribed) return "";
  const [low, high] = field.prescribed;
  return low === high ? formatNumber(low) : `${formatNumber(low)}–${formatNumber(high)}`;
}

/** "3 sets", "80 kg", "15 min", "RPE 8". */
export function formatLogValue(field: LogField, value: number): string {
  if (field.clock) return formatRunTime(value);
  const number = formatNumber(value);
  if (field.key === "effort") return `RPE ${number}`;
  if (field.unit) return `${number} ${field.unit}`;
  const noun = field.label.toLowerCase();
  return `${number} ${value === 1 ? noun.replace(/s$/, "") : noun}`;
}

function withValue(actual: ExerciseLogActual, field: LogField, value: number): void {
  switch (field.key) {
    case "sets":
    case "reps":
    case "rounds":
      actual[field.key] = value;
      break;
    case "effort":
      actual.effort = { method: "RPE", value };
      break;
    default:
      actual[field.key] = { value, unit: field.apiUnit ?? "" };
  }
}

export type LogEntryResult = {
  status: Exclude<ExerciseLogStatus, "skipped">;
  /** Only what differs from, or adds to, the prescription. */
  actual: ExerciseLogActual;
  /** Fields that could not be read as a number, or are out of range. */
  invalid: LogFieldKey[];
  /** Entries far enough from the prescription to be worth a second look. */
  outliers: string[];
};

const FIELD_LIMITS: Record<LogFieldKey, [number, number, boolean]> = {
  // [min, max, whole numbers only]
  sets: [0, 50, true],
  reps: [0, 1000, true],
  rounds: [0, 100, true],
  load: [0, 1000, false],
  duration: [0, 600, false],
  work: [0, 600, false],
  distance: [0, 100000, false],
  effort: [1, 10, false],
};

/**
 * Turn what the athlete typed into a log. An empty field means "as written".
 * A number that departs from the prescription makes the log "modified"; a
 * number the plan never gave (the weight used, the RPE felt) is recorded
 * without counting as a change.
 */
export function buildLogEntry(fields: LogField[], draft: LogDraft): LogEntryResult {
  const actual: ExerciseLogActual = {};
  const invalid: LogFieldKey[] = [];
  const outliers: string[] = [];
  let modified = false;
  for (const field of fields) {
    const text = (draft[field.key] ?? "").trim();
    if (!text) {
      continue;
    }
    const value = field.clock ? parseRunTime(text) : parseEntry(text);
    const [min, max, whole] = field.clock ? [1, RUN_TIME_MAX_SECONDS, true] as const : FIELD_LIMITS[field.key];
    if (value === null || value < min || value > max || (whole && !Number.isInteger(value))) {
      invalid.push(field.key);
      continue;
    }
    if (!field.prescribed) {
      withValue(actual, field, value);
      if (field.key === "load" && value > (field.unit === "lb" ? OUTLIER_LOAD_KG * 2.2 : OUTLIER_LOAD_KG)) {
        outliers.push(`${formatLogValue(field, value)} is a very heavy load`);
      }
      continue;
    }
    const [low, high] = field.prescribed;
    if (value >= low && value <= high) {
      // Inside a prescribed range, the exact count is still worth keeping.
      if (low !== high || field.key === "effort" || field.key === "load") {
        withValue(actual, field, value);
      }
      continue;
    }
    // Reps progressed inside the permitted range are the workout, done.
    if (field.permitted && value >= field.permitted[0] && value <= field.permitted[1]) {
      withValue(actual, field, value);
      continue;
    }
    modified = true;
    withValue(actual, field, value);
    if (value > high * OUTLIER_RATIO || value < low / OUTLIER_RATIO) {
      outliers.push(`${formatLogValue(field, value)} against ${formatPrescribed(field)} prescribed`);
    }
  }
  return { status: modified ? "modified" : "as_prescribed", actual, invalid, outliers };
}

/** Seed every set from the current draft or an exact prescription. */
export function setDraftsForFields(fields: LogField[], draft: LogDraft = {}, count?: number): SetDraft[] {
  const value = (key: LogFieldKey) => {
    if (draft[key]?.trim()) return draft[key]!;
    const prescribed = fields.find(field => field.key === key)?.prescribed;
    return prescribed && prescribed[0] === prescribed[1] ? String(prescribed[0]) : "";
  };
  const sets = count ?? Number(value("sets") || 1);
  return Array.from({ length: Math.min(50, Math.max(1, Number.isFinite(sets) ? Math.floor(sets) : 1)) }, () => ({ reps: value("reps"), load: value("load"), ...(value("effort") ? { rpe: value("effort") } : {}) }));
}

export function buildSetLogEntry(fields: LogField[], rows: SetDraft[], unit: string, draft: LogDraft = {}): LogEntryResult {
  const entry = buildLogEntry(fields.filter(field => !["sets", "reps", "load", "effort"].includes(field.key)), draft);
  const read = readSets(rows, unit);
  const count = buildLogEntry(fields.filter(field => field.key === "sets"), { sets: String(rows.length) });
  const loadField = fields.find(field => field.key === "load");
  const differentUnit = Boolean(loadField?.prescribed && loadField.apiUnit !== unit && rows.some(row => row.load.trim()));
  let changed = (read.actual.set_entries?.length ?? 0) < rows.length || entry.status === "modified" || count.status === "modified" || differentUnit;
  const invalid = [...entry.invalid, ...(read.invalid.length ? ["reps" as const] : [])];
  const outliers = [...entry.outliers, ...count.outliers];
  for (const [index, row] of rows.entries()) {
    const result = buildLogEntry(fields.filter(field => field.key === "reps" || field.key === "load" || (field.key === "effort" && row.rpe !== undefined))
      .map(field => field.key === "load" && field.apiUnit !== unit ? { ...field, prescribed: null, unit, apiUnit: unit } : field), { ...row, effort: row.rpe });
    changed ||= result.status === "modified";
    outliers.push(...result.outliers.map(message => `Set ${index + 1}: ${message}`));
  }
  return { actual: { ...entry.actual, ...read.actual }, status: changed ? "modified" : "as_prescribed", invalid, outliers };
}

function actualNumber(actual: ExerciseLogActual | null | undefined, key: LogFieldKey): number | null {
  const value = key === "effort" ? actual?.effort?.value : actual?.[key];
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (value && typeof value === "object" && typeof value.value === "number") {
    return Number.isFinite(value.value) ? value.value : null;
  }
  return null;
}

/** A saved value in the field's own terms: a clock field reads seconds. */
function fieldNumber(actual: ExerciseLogActual | null | undefined, field: LogField): number | null {
  const value = actualNumber(actual, field.key);
  if (value === null || !field.clock) return value;
  return actual?.duration?.unit === "minutes" ? value * 60 : value;
}

/** The draft that reproduces a saved log, for editing it. */
export function draftFromLog(fields: LogField[], log: ExerciseLogRecord | null | undefined): LogDraft {
  const draft: LogDraft = {};
  for (const field of fields) {
    const value = fieldNumber(log?.actual, field);
    if (value !== null) {
      draft[field.key] = field.clock ? formatRunTime(value) : formatNumber(value);
    }
  }
  return draft;
}

export type LoggedValue = {
  key: LogFieldKey;
  label: string;
  /** What was done, e.g. "3" or "80 kg". */
  value: string;
  /** The same with its noun: "3 sets", "80 kg", "RPE 8". */
  text: string;
  /** The prescription it replaced; null when it only adds to it. */
  insteadOf: string | null;
};

/** What a saved log recorded, field by field, for showing beside the prescription. */
export function loggedValues(fields: LogField[], log: ExerciseLogRecord): LoggedValue[] {
  const values: LoggedValue[] = [];
  for (const field of fields) {
    const value = fieldNumber(log.actual, field);
    if (value === null) {
      continue;
    }
    const within = field.permitted ?? field.prescribed;
    const departs = Boolean(within) && (value < within![0] || value > within![1]);
    const number = formatNumber(value);
    values.push({
      key: field.key,
      label: field.label,
      value: field.clock ? formatRunTime(value) : field.unit ? `${number} ${field.unit}` : number,
      text: formatLogValue(field, value),
      insteadOf: departs ? `${formatPrescribed(field)}${field.unit ? ` ${field.unit}` : ""}` : null,
    });
  }
  return values;
}

/** One line for the collapsed row: "Done", "Done · 80 kg", "Changed · 3 sets · 6 reps", "Skipped". */
export function logSummary(fields: LogField[], log: ExerciseLogRecord): string {
  const label = EXERCISE_LOG_STATUS_LABELS[log.status];
  if (log.status === "skipped") {
    return label;
  }
  if (log.actual?.set_entries?.length) return `${label} · ${setSummary(log.actual.set_entries, log.actual.sets ?? undefined)}`;
  if (countOnlySetSummary(log.actual)) return `${label} · ${countOnlySetSummary(log.actual)}`;
  const parts = fields.flatMap((field) => {
    const value = fieldNumber(log.actual, field);
    return value === null ? [] : [formatLogValue(field, value)];
  });
  return [label, ...parts].join(" · ");
}

/** Whether a block can be logged at all: it needs its server id, and rehab is
 * logged with the session. */
export function isLoggableBlock(block: StructuredBlock): boolean {
  return Boolean(cleanText(block.block_id)) && cleanText(block.block_type) !== "rehab";
}

export type SessionOutcome = {
  /** The session status the exercise logs add up to. */
  status: "done" | "modified" | "skipped";
  /** Exercises the session owes (optional work and rehab are not counted). */
  total: number;
  logged: number;
  /** Owed exercises with no log yet; they are saved as skipped. */
  unlogged: StructuredBlock[];
  /** What changed, in words, for the session's modification reason. "" when done. */
  reason: string;
};

/**
 * The session's outcome, read off its exercise logs, so the athlete logs each
 * exercise once and never states the session result separately:
 * every owed exercise done as written → done; nothing done → skipped; anything
 * else → modified, with the changes spelled out as the reason. An exercise with
 * no log counts as skipped. Optional sessions never decide the outcome.
 */
export function deriveSessionOutcome(
  sessions: readonly StructuredSession[],
  logs: Readonly<Record<string, ExerciseLogRecord>>,
): SessionOutcome {
  const owed = sessions
    .filter((session) => session.optional !== true)
    .flatMap((session) => session.blocks ?? [])
    .filter(isLoggableBlock);
  const changed: string[] = [];
  const skipped: string[] = [];
  const unlogged: StructuredBlock[] = [];
  let logged = 0;
  for (const block of owed) {
    const name = cleanText(block.display_name) || "Exercise";
    const log = logs[cleanText(block.block_id) ?? ""];
    if (!log) {
      unlogged.push(block);
      skipped.push(name);
      continue;
    }
    logged += 1;
    if (log.status === "skipped") {
      skipped.push(name);
    } else if (log.status === "modified") {
      const values = loggedValues(logFieldsForBlock(block), log).map((value) => value.text);
      changed.push(values.length ? `${name} (${values.join(" · ")})` : name);
    }
  }
  const status =
    owed.length > 0 && skipped.length === owed.length
      ? "skipped"
      : changed.length === 0 && skipped.length === 0
        ? "done"
        : "modified";
  const reason = [
    changed.length ? `Changed: ${changed.join(", ")}.` : "",
    skipped.length ? `Skipped: ${skipped.join(", ")}.` : "",
  ]
    .filter(Boolean)
    .join(" ");
  return { status, total: owed.length, logged, unlogged, reason: status === "done" ? "" : reason };
}

/**
 * The weight this exercise was last logged with, to carry into today's log, or
 * null. Only for weighed work whose plan names no weight (when it does, "done
 * as written" already means that weight), and only in the same unit.
 */
export function lastLoadFor(
  block: StructuredBlock,
  recentLoads: readonly ExerciseRecentLoad[] | undefined,
): ExerciseLogMeasure | null {
  if (!recentLoads?.length) return null;
  const field = logFieldsForBlock(block).find((item) => item.key === "load");
  if (!field || field.prescribed) return null;
  const key = cleanText(block.exercise_key);
  const name = cleanText(block.display_name)?.toLowerCase() ?? "";
  const match =
    (key ? recentLoads.find((item) => item.exercise_key === key) : undefined) ??
    (name ? recentLoads.find((item) => !item.exercise_key && item.display_name.trim().toLowerCase() === name) : undefined);
  if (!match || match.load.unit !== field.apiUnit || !finitePositiveNumber(match.load.value)) return null;
  return { value: match.load.value, unit: match.load.unit };
}

/**
 * The effort the session was planned at: the middle RPE of the exercises the
 * session owes, rounded onto the 1-9 session effort scale. Null when the plan
 * gives no RPE. The review starts
 * there so a session that went to plan is one tap to confirm.
 */
export function plannedSessionRpe(sessions: readonly StructuredSession[]): number | null {
  const values = sessions
    .filter((session) => session.optional !== true)
    .flatMap((session) => session.blocks ?? [])
    .filter(isLoggableBlock)
    .flatMap((block) => {
      const effort = block.effort;
      const value = typeof effort?.value === "number" ? effort.value : Number(effort?.value);
      return cleanText(effort?.method)?.toUpperCase() === "RPE" && Number.isFinite(value) && value >= 1 && value <= 10
        ? [value]
        : [];
    })
    .sort((a, b) => a - b);
  if (values.length === 0) return null;
  const middle = values.length / 2;
  const median = values.length % 2 ? values[Math.floor(middle)] : (values[middle - 1] + values[middle]) / 2;
  // The session effort scale stops at 9 (Max Effort).
  return Math.min(9, Math.round(median));
}

/** A planner key is authoritative; names only match legacy records without keys. */
export function lastPerformanceFor(block: StructuredBlock, history?: readonly ExerciseLogRecord[]): ExerciseLogRecord | null {
  const key = cleanText(block.exercise_key);
  const name = cleanText(block.display_name)?.toLowerCase();
  return history?.find((row) => key && row.exercise_key
    ? row.exercise_key === key
    : !row.exercise_key && name && String(row.prescribed.display_name ?? "").trim().toLowerCase() === name) ?? null;
}

export function performanceSummary(log: ExerciseLogRecord): string {
  if (log.status === "skipped") return "Skipped";
  if (log.actual?.set_entries?.length) return setSummary(log.actual.set_entries, log.actual.sets ?? undefined);
  if (countOnlySetSummary(log.actual)) return countOnlySetSummary(log.actual)!;
  const block: StructuredBlock = {
    ...log.prescribed,
    // A recorded run time with no prescribed one stays a clock time, not a duration dose.
    ...Object.fromEntries((["load", "duration", "work", "distance"] as const)
      .flatMap((key) => log.actual[key] && (key !== "duration" || log.prescribed.duration) ? [[key, log.actual[key]]] : [])),
  };
  const actual = { ...log.actual };
  // Blank actual fields mean the saved prescription was followed, not today's dose.
  for (const key of ["sets", "reps", "rounds"] as const) {
    if (actual[key] == null && typeof block[key] === "number") actual[key] = block[key] as number;
  }
  const fields = logFieldsForBlock(block);
  const values = loggedValues(fields, { ...log, actual });
  const sets = fields.find((field) => field.key === "sets");
  const reps = fields.find((field) => field.key === "reps");
  const count = (field: LogField | undefined) => {
    if (!field) return null;
    const value = actualNumber(actual, field.key);
    return value !== null ? formatNumber(value) : field.prescribed ? formatPrescribed(field) : null;
  };
  const setCount = count(sets);
  const repCount = count(reps);
  const compactDose = setCount !== null && repCount !== null ? `${setCount} × ${repCount}` : null;
  const parts = [...fields].sort((a, b) => (a.key === "load" ? -1 : b.key === "load" ? 1 : 0)).flatMap((field) => {
    if (compactDose && field.key === "sets") return [compactDose];
    if (compactDose && field.key === "reps") return [];
    const recorded = values.find((value) => value.key === field.key);
    if (recorded) return [recorded.text];
    if (field.key === "effort" || !field.prescribed) return [];
    return [`${formatPrescribed(field)}${field.unit ? ` ${field.unit}` : ` ${field.label.toLowerCase()}`}`];
  });
  return parts.join(" · ") || EXERCISE_LOG_STATUS_LABELS[log.status];
}

/** Only a literal, supported progression rule can propose an increase. */
export type ExerciseFeel = "easy" | "right" | "hard";

/** What to attempt next time: one more rep inside the range, or a little more weight. */
export type NextStep = { kind: "reps"; reps: number } | { kind: "load"; load: ExerciseLogMeasure };

/** A raise is this share of the last weight, and never less than one plate step. */
const PROGRESSION_RATIO = 0.03;
/** No raise is offered inside this many days of the fight, whatever the phase label. */
const PROGRESSION_FINAL_DAYS = 14;

// The plan's own instruction to stay put ("Keep the same dose and make each rep
// sharp", "none this block") outranks what the numbers suggest.
const HOLD_RULE = /\b(?:same (?:dose|load|weight)|maintain|hold (?:the |this )?(?:dose|load|weight)|(?:do not|don't|no) (?:add|increase|progress)|no progression)\b|^none\b/i;

/** Plate-friendly steps for bar work, finer ones for light dumbbells. */
function plateStep(load: ExerciseLogMeasure): number {
  const heavy = load.unit === "lb" ? load.value >= 55 : load.value >= 25;
  return load.unit === "lb" ? (heavy ? 5 : 1) : heavy ? 2.5 : 0.5;
}

function plannedRpe(block: StructuredBlock): number | null {
  const effort = block.effort;
  return cleanText(effort?.method)?.toUpperCase() === "RPE" && typeof effort?.value === "number" && finitePositiveNumber(effort.value)
    ? effort.value
    : null;
}

/** An RPE at least two over the plan's (9+ when the plan gave none) was too much. */
function rpeCeiling(planned: number | null): number {
  return planned === null ? 9 : Math.min(planned + 2, 10);
}

/**
 * How the logged work compared with the plan. Recorded excessive RPE prevents
 * an Easy tap earning an increase; matching target RPE prevents Hard alone
 * causing a reduction. Otherwise the answer is used, or a
 * reported RPE is read against the plan's, and "felt strong" counts as easy.
 * Null when nothing was said: a bare "done" is never taken as easy.
 */
export function feelOf(log: ExerciseLogRecord, currentTarget?: number | null): ExerciseFeel | null {
  const rows = log.actual.set_entries;
  const measuredRpe = rows?.length && rows.every(row => typeof row.rpe === "number" && row.rpe >= 1 && row.rpe <= 10)
    ? Math.max(...rows.map(row => row.rpe!)) : null;
  const effort = log.actual.effort ?? (measuredRpe === null ? null : { method: "RPE", value: measuredRpe });
  if (effort && typeof effort.value === "number" && effort.value >= 1 && effort.value <= 10 && (effort.method ?? "RPE") === "RPE") {
    const before = log.prescribed as StructuredBlock;
    const planned = plannedRpe(before) ?? currentTarget ?? null;
    if (effort.value >= rpeCeiling(planned) && (planned === null || effort.value > planned)) return "hard";
    // Recorded effort prevents an Easy/Hard tap contradicting the target.
    if (log.actual.feel === "hard") return planned !== null && effort.value <= planned ? "right" : "hard";
    if (log.actual.feel === "right") return "right";
    if (log.actual.feel === "easy") return planned !== null && effort.value >= planned ? "right" : "easy";
    return effort.value <= 7 && (planned === null || effort.value < planned) ? "easy" : "right";
  }
  return log.actual.feel ?? (log.reason === "felt_strong" ? "easy" : null);
}

/**
 * The rep range progression may move within: one the prescription writes
 * ("6-8"), else the server's permitted range around a fixed working number.
 * A permitted range is only used while `reps` sits inside it.
 */
export function progressionRepRange(block: StructuredBlock): [number, number] | null {
  const written = prescribedReps(block.reps)?.range;
  if (!written) return null;
  const permitted = block.rep_range;
  if (written[0] === written[1] && Array.isArray(permitted) && permitted.length === 2) {
    const [low, high] = permitted;
    if (Number.isInteger(low) && Number.isInteger(high) && low < high && low <= written[0] && written[0] <= high) {
      return [written[0], high];
    }
  }
  return written;
}

/**
 * The reps every set reached. Blank means the prescription was followed, which
 * counts as the bottom of a range, never its top. Null when sets were logged
 * one by one and any of them has no count.
 */
function repsReached(log: ExerciseLogRecord, planned: [number, number]): number | null {
  const rows = log.actual.set_entries;
  if (rows?.length) {
    const counts = rows.map((row) => row.reps);
    if (rows.length < Math.max(log.actual.sets ?? 0, Number(log.prescribed.sets) || 0) || counts.some((count) => typeof count !== "number")) return null;
    return Math.min(...(counts as number[]));
  }
  return typeof log.actual.reps === "number" ? log.actual.reps : planned[0];
}

/**
 * Whether a raise may be offered at all today. Readiness is one condition among
 * several: a green day never outranks taper, the final fortnight, an open
 * injury or a safety hold.
 */
export function progressionOpen(context: {
  decisionTier?: string | null;
  openInjuries: number;
  phase?: string | null;
  safetyHold?: boolean;
  fightDate?: string | null;
  trainingDay?: string | null;
}): boolean {
  if (context.decisionTier !== "green" || context.openInjuries > 0 || context.safetyHold) return false;
  if (!/^(GPP|SPP)$/i.test(context.phase ?? "")) return false;
  if (context.fightDate) {
    const days = (Date.parse(context.fightDate.slice(0, 10)) - Date.parse((context.trainingDay ?? "").slice(0, 10))) / 86_400_000;
    // An unreadable date is treated as close to the fight.
    if (!Number.isFinite(days) || days <= PROGRESSION_FINAL_DAYS) return false;
  }
  return true;
}

/**
 * The rep range both prescriptions share, when today's asks for the same work
 * as the one logged: same sets, same permitted reps, and no lower effort
 * target. Null when today's dose was changed, which is the plan's call.
 */
function sameWorkload(block: StructuredBlock, before: StructuredBlock, actual?: ExerciseLogActual): [number, number] | null {
  const today = progressionRepRange(block);
  const planned = progressionRepRange(before);
  if (!today || (before.block_type && !FIRST_LOAD_BLOCK_TYPES.has(before.block_type.toLowerCase()))) return null;
  const oldLoad = before.load?.value ?? actual?.load?.value;
  const oldUnit = before.load?.unit ?? actual?.load?.unit;
  if (block.load?.value != null && oldLoad != null && (block.load.value !== oldLoad || block.load.unit !== oldUnit)) return null;
  // Unplanned work can establish a measured workload, never a fictional prescription.
  if (!planned && before.sets == null && before.reps == null && actual && typeof actual.sets === "number" && actual.sets > 0 && actual.sets === block.sets) {
    const rows = actual.set_entries;
    const reached = actual.reps ?? (rows?.length && rows.length === actual.sets && rows.every(row => typeof row.reps === "number")
      ? Math.min(...rows.map(row => row.reps!)) : null);
    return reached != null && reached >= today[0] && reached <= today[1] ? today : null;
  }
  if (!planned || block.sets !== before.sets) return null;
  // A log saved before ranges existed froze only the working number.
  const legacy = before.rep_range == null && planned[0] === planned[1]
    && planned[0] === today[0] && planned[0] <= today[1];
  if (!legacy && today.join() !== planned.join()) return null;
  const rpeToday = plannedRpe(block);
  const rpeBefore = plannedRpe(before);
  if (rpeToday !== null && rpeBefore !== null && rpeToday < rpeBefore) return null;
  return legacy ? today : planned;
}

/**
 * What to try next, read off what was actually done last time rather than off
 * the wording of a progression rule. Only ever a suggestion.
 *
 * Earned in order: every set and rep done, and it felt easy. Below the top of a
 * rep range that earns one more rep; at the top on every set it earns a small
 * raise in weight. "Right" holds the dose, "hard" is left to the ease-off.
 * Nothing is offered when today's dose differs from the one logged, when the
 * plan asks for less effort than last time, or when the plan says to hold.
 */
export function nextStepFor(block: StructuredBlock, log: ExerciseLogRecord | null, allowed: boolean): NextStep | null {
  if (!allowed || !log || log.status === "skipped" || (log.reason != null && log.reason !== "felt_strong")) return null;
  // The server's hold (taper, final fortnight, injury, weight cut, capped dose,
  // jumping or ballistic work) and the plan's own instruction to stay put. An
  // exercise without positive server clearance cannot earn an increase.
  if (block.progression_hold !== false || HOLD_RULE.test(cleanText(block.progression_rule) ?? "")) return null;
  // Strength work only: jumps, sprints and holds progress on quality, not reps or weight.
  if (!FIRST_LOAD_BLOCK_TYPES.has(cleanText(block.block_type)?.toLowerCase() ?? "")) return null;
  const comparable = sameWorkload(block, log.prescribed as StructuredBlock, log.actual);
  if (!comparable || !finitePositiveNumber(block.sets)) return null;
  if ((log.actual.sets ?? block.sets) < block.sets) return null;
  const reps = repsReached(log, comparable);
  if (reps === null || reps < comparable[0] || feelOf(log, plannedRpe(block)) !== "easy") return null;
  if (reps < comparable[1]) return { kind: "reps", reps: reps + 1 };

  const field = logFieldsForBlock(block).find((item) => item.key === "load");
  const entries = log.actual.set_entries;
  const perSetLoads = entries?.map(row => row.load);
  const load = log.actual.load ?? (perSetLoads?.length && perSetLoads.every(value =>
    value && value.unit === perSetLoads[0]?.unit && finitePositiveNumber(value.value))
    ? { value: Math.min(...perSetLoads.map(value => value!.value)), unit: perSetLoads[0]!.unit } : null);
  // Numeric targets are optional drafts; holds, changed targets and intensity methods still win.
  if (block.load?.method && !["absolute", "bodyweight"].includes(block.load.method)) return null;
  if (!field || !load || load.unit !== field.apiUnit || !finitePositiveNumber(load.value)) return null;
  const step = plateStep(load);
  const raise = Math.max(1, Math.round((load.value * PROGRESSION_RATIO) / step)) * step;
  return { kind: "load", load: { value: Math.round((load.value + raise) * 100) / 100, unit: load.unit } };
}

/** The raised weight to offer, when the next step is more weight. */
export function progressionLoadFor(block: StructuredBlock, log: ExerciseLogRecord | null, allowed: boolean): ExerciseLogMeasure | null {
  const step = nextStepFor(block, log, allowed);
  return step?.kind === "load" ? step.load : null;
}

/** "Try 7 reps", "Try 82.5 kg". */
export function nextStepLabel(step: NextStep): string {
  return step.kind === "reps" ? `Try ${step.reps} reps` : `Try ${step.load.value} ${step.load.unit}`;
}

// Weighted work the first-weight question is asked for. Jumps, throws and
// sprints also log reps, and nobody should be asked what a box jump weighed.
const FIRST_LOAD_BLOCK_TYPES = new Set(["strength", "accessory"]);

/** Stable name for an exercise across plans: the planner key, else its name. */
export function exerciseIdentity(block: StructuredBlock): string {
  return cleanText(block.exercise_key) ?? cleanText(block.display_name)?.toLowerCase() ?? "";
}

/**
 * Whether this exercise has no weight on record to carry forward: weighted
 * strength work, no weight named by the plan, none logged before. False until
 * the history has loaded, so a slow read never asks needlessly.
 */
export function needsFirstLoad(block: StructuredBlock, recentLoads: readonly ExerciseRecentLoad[] | undefined): boolean {
  if (!recentLoads || block.prescription_source === "athlete") return false;
  if (!FIRST_LOAD_BLOCK_TYPES.has(cleanText(block.block_type)?.toLowerCase() ?? "")) return false;
  const field = logFieldsForBlock(block).find((item) => item.key === "load");
  return Boolean(field) && !field!.prescribed && lastLoadFor(block, recentLoads) === null;
}

/** Ask for a first weight only while this exercise has none on record. */
export function asksForFirstLoad(
  block: StructuredBlock,
  log: ExerciseLogRecord | null,
  recentLoads: readonly ExerciseRecentLoad[] | undefined,
): boolean {
  if (!log || log.status === "skipped" || log.actual?.load || (log.actual?.set_entries?.length && log.actual.set_entries.every(row => row.load != null))) return false;
  return recentLoads !== undefined && block.prescription_source !== "athlete"
    && FIRST_LOAD_BLOCK_TYPES.has(cleanText(block.block_type)?.toLowerCase() ?? "")
    && logFieldsForBlock(block).some(field => field.key === "load")
    && lastLoadFor(block, recentLoads) === null;
}

/** A saved log resent with one thing added or changed; everything else as stored. */
export function amendedLog(
  log: ExerciseLogRecord,
  patch: Pick<ExerciseLogActual, "load" | "feel" | "reps">,
): Omit<ExerciseLogRequest, "plan_id" | "block_id"> {
  const actual = Object.fromEntries(
    Object.entries({ ...log.actual, ...patch }).filter(([, value]) => value !== null && value !== undefined),
  ) as ExerciseLogActual;
  return { status: log.status, reason: log.reason, notes: log.notes, actual };
}

/** Last time's weight is eased by this share when the athlete struggled with it. */
const BACKOFF_RATIO = 0.9;
const HARD_COMPLETE_BACKOFF_RATIO = 0.95;

/**
 * A lighter weight to offer when last time's was too much: the athlete flagged
 * fatigue, fell short of the sets or reps, or reported an RPE at least two over
 * the plan's (9+ when the plan gave none), or explicitly marked the work hard.
 * Only a suggestion, never applied on
 * its own, and only while today's dose matches the one that was struggled with.
 * Pain and skips are left out: those already stop the old weight carrying over.
 * A stated reason outranks what the numbers suggest, so short sets for want of
 * kit, or a high RPE on a strong day, are not read as the weight being too much.
 */
export function backoffLoadFor(block: StructuredBlock, log: ExerciseLogRecord | null): ExerciseLogMeasure | null {
  if (!log || log.status === "skipped" || log.reason === "pain") return null;
  if (log.reason === "equipment" || log.reason === "felt_strong") return null;
  const field = logFieldsForBlock(block).find((item) => item.key === "load");
  const load = log.actual.load;
  if (!field || !load || load.unit !== field.apiUnit || !finitePositiveNumber(load.value)) return null;
  // The block as it was shown last time.
  const before = log.prescribed as StructuredBlock;
  const plannedReps = prescribedReps(before.reps)?.range;
  if (!sameWorkload(block, before, log.actual)) return null;

  const plannedSets = finitePositiveNumber(before.sets) ? before.sets : null;
  const doneSets = log.actual.sets;
  const rows = log.actual.set_entries;
  const recordedReps = rows?.every(row => typeof row.reps === "number")
    ? Math.min(...rows.map(row => row.reps!)) : null;
  const doneReps = log.actual.reps ?? recordedReps;
  const missedDose =
    (plannedSets !== null && typeof doneSets === "number" && doneSets < plannedSets) ||
    (plannedReps !== undefined && typeof doneReps === "number" && doneReps < plannedReps[0]);
  if (log.reason !== "fatigue" && !missedDose && feelOf(log, plannedRpe(block)) !== "hard") return null;

  const step = plateStep(load);
  const eased = Math.round((load.value * (log.reason === "fatigue" || missedDose ? BACKOFF_RATIO : HARD_COMPLETE_BACKOFF_RATIO)) / step) * step;
  if (!(eased > 0 && eased < load.value)) return null;
  return { value: Math.round(eased * 100) / 100, unit: load.unit };
}
