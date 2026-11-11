import type { PersonalExercise } from "./api-schema.generated";

/**
 * The weight field shared by every prescription editor. A weight is stored as
 * "30 kg" / "45 lb": the exact shape the server turns into a numeric load, so
 * what the athlete prescribes is what logging, bests and progress track.
 * Anything else ("light band", "fast") stays free text and is never a number.
 */

export type LoadUnit = "kg" | "lb";

export function parseLoad(text: string | null | undefined): { value: number; unit: LoadUnit } | null {
  // The same pattern the server reads a numeric load from (api/services/custom_plans.py).
  const match = (text ?? "").trim().match(/^(\d+(?:\.\d+)?)\s*(kg|lb|lbs)$/i);
  if (!match) return null;
  const value = Number(match[1]);
  return Number.isFinite(value) && value > 0 ? { value, unit: match[2].toLowerCase() === "kg" ? "kg" : "lb" } : null;
}

export function formatLoad(value: number, unit: LoadUnit): string {
  return `${Math.round(value * 100) / 100} ${unit}`;
}

/** Whether the weight field opens on a number: kept text stays text, and sets × reps work is weighed unless it says otherwise. */
export function startsAsWeight(text: string | null | undefined, weighed: boolean): boolean {
  const value = (text ?? "").trim();
  if (value) return parseLoad(value) !== null;
  return weighed;
}

/**
 * Whether sets × reps work is weighed. A catalogue movement or past exercise
 * may carry a `log_format` ("strength", "bodyweight", …); only an explicit
 * non-strength format turns the default off. Read structurally because not
 * every response carries it.
 */
export function weighedFormat(mode: string, ...sources: (object | null | undefined)[]): boolean {
  if (mode !== "reps") return false;
  const format = sources.map(source => (source as { log_format?: unknown } | null | undefined)?.log_format)
    .find(value => typeof value === "string");
  return format === undefined || format === "strength";
}

type DoseShape = {
  mode: "reps" | "duration" | "intervals" | "distance" | "text";
  sets?: number | null; reps?: number | null; rounds?: number | null;
  work_seconds?: number | null; duration_seconds?: number | null; rest_seconds?: number | null;
  distance?: number | null; distance_unit?: "m" | "km" | "mi" | "yd" | null;
  rpe?: number | null; load_or_intensity?: string | null; prescription?: string | null;
};

const seconds = (measure: { value: number; unit: string } | null | undefined) =>
  measure && measure.value > 0 ? Math.round(measure.value * (/^min/i.test(measure.unit) ? 60 : 1)) : null;
const DISTANCE_UNITS: Record<string, DoseShape["distance_unit"]> = { meters: "m", km: "km", miles: "mi" };
const blank = (value: unknown) => value == null || value === "";

/**
 * Fills the gaps in a new prescription from the last time this exercise was
 * done: a suggested format always wins, the athlete's own numbers only stand
 * in where nothing was suggested. The last weight fills an empty weight.
 */
export function withPastDose<T extends DoseShape>(dose: T, past: PersonalExercise | null | undefined, preserveMode = false): T {
  if (!past) return dose;
  const values = past.latest_values;
  // No suggested format at all: the athlete's last session sets one.
  const unformatted = (["sets", "reps", "rounds", "work_seconds", "duration_seconds", "distance", "prescription"] as const)
    .every(key => blank(dose[key]));
  let mode = dose.mode;
  if (unformatted && !preserveMode) {
    mode = values.sets && values.reps ? "reps" : values.rounds && values.work ? "intervals"
      : values.distance && DISTANCE_UNITS[values.distance.unit] ? "distance" : values.duration ? "duration" : dose.mode;
  }
  const next: T = { ...dose, mode };
  const fill = <K extends keyof DoseShape>(key: K, value: DoseShape[K] | null | undefined) => {
    if (value != null && blank(next[key])) (next as DoseShape)[key] = value;
  };
  if (mode === "reps") { fill("sets", values.sets); fill("reps", values.reps); }
  if (mode === "intervals") { fill("rounds", values.rounds); fill("work_seconds", seconds(values.work)); }
  if (mode === "duration") fill("duration_seconds", seconds(values.duration));
  if (mode === "distance" && values.distance && DISTANCE_UNITS[values.distance.unit] && blank(next.distance)) {
    next.distance = values.distance.value;
    next.distance_unit = DISTANCE_UNITS[values.distance.unit];
  }
  if (unformatted) {
    fill("rest_seconds", seconds(values.rest));
    const target = past.latest_performance?.prescribed.effort as { method?: string; value?: number } | undefined;
    if (target?.method === "RPE") fill("rpe", target.value);
  }
  const load = values.load;
  if (load && load.value > 0 && (load.unit === "kg" || load.unit === "lb")) fill("load_or_intensity", formatLoad(load.value, load.unit));
  return next;
}

/** The last weight for the field's placeholder and default unit. */
export function pastLoad(past: PersonalExercise | null | undefined): { value: number; unit: LoadUnit } | null {
  const load = past?.latest_values.load;
  return load && load.value > 0 && (load.unit === "kg" || load.unit === "lb") ? { value: load.value, unit: load.unit } : null;
}
