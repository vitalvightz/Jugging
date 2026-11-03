import { formatAppDate } from "@/lib/date-format";
import type { NutritionBodyweightLogEntry, NutritionWorkspaceState } from "@/lib/types";

export type BodyweightRange = "7D" | "30D" | "All";

export type IndexedBodyweightEntry = NutritionBodyweightLogEntry & {
  sourceIndex: number;
};

/** Calendar days each range covers, ending on the latest weigh-in's day. */
export const RANGE_DAY_WINDOW: Record<Exclude<BodyweightRange, "All">, number> = {
  "7D": 7,
  "30D": 30,
};

export function getBodyweightTimestamp(entry: NutritionBodyweightLogEntry): number {
  const normalizedDate = String(entry.date || "").trim();
  if (!normalizedDate) return Number.NEGATIVE_INFINITY;
  const normalizedTime = String(entry.time || "").trim() || "00:00";
  const parsed = Date.parse(`${normalizedDate}T${normalizedTime}`);
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

function normalizeWeight(value: number | null | undefined): number | null {
  if (value == null || !Number.isFinite(value)) return null;
  return Math.round(value * 10) / 10;
}

function roundToOne(value: number): number {
  return Math.round(value * 10) / 10;
}

/** Newest first, each entry keeping its index in the saved log. */
export function getBodyweightEntriesWithIndex(entries: NutritionBodyweightLogEntry[]): IndexedBodyweightEntry[] {
  return entries
    .map((entry, sourceIndex) => ({ ...entry, sourceIndex }))
    .sort((left, right) => getBodyweightTimestamp(right) - getBodyweightTimestamp(left));
}

/** A `YYYY-MM-DD` date shifted by whole local calendar days. */
export function shiftDate(date: string, days: number): string {
  const [year, month, day] = date.split("-").map(Number);
  const shifted = new Date(year, month - 1, day + days);
  return `${shifted.getFullYear()}-${String(shifted.getMonth() + 1).padStart(2, "0")}-${String(shifted.getDate()).padStart(2, "0")}`;
}

/**
 * Weigh-ins that can be plotted for the range, oldest first. Ranges count back
 * whole calendar days from the latest weigh-in's day, so a gap in logging
 * never leaves an empty chart.
 */
export function getBodyweightTrend(entries: NutritionBodyweightLogEntry[], range: BodyweightRange): IndexedBodyweightEntry[] {
  const plottable = getBodyweightEntriesWithIndex(entries)
    .filter((entry) => Number.isFinite(getBodyweightTimestamp(entry)) && Number.isFinite(entry.weight_kg))
    .reverse();
  const latest = plottable.at(-1);
  if (range === "All" || !latest) return plottable;
  const firstDay = shiftDate(latest.date, 1 - RANGE_DAY_WINDOW[range]);
  return plottable.filter((entry) => entry.date >= firstDay);
}

/** Change from the first to the last weigh-in in a trend, or null with fewer than two. */
export function getTrendChange(trend: readonly NutritionBodyweightLogEntry[]): number | null {
  if (trend.length < 2) return null;
  return roundToOne(trend[trend.length - 1].weight_kg - trend[0].weight_kg);
}

/**
 * The camp's current weight: what the API's cut maths, Camp Setup and the plan
 * use. It follows the newest weigh-in unless a later weight was set in Camp
 * Setup, so it can differ from the latest logged weigh-in.
 */
export function getCampWeight(workspace: NutritionWorkspaceState): number | null {
  return normalizeWeight(workspace.shared_camp_context.current_weight_kg ?? null);
}

export function getTargetGap(weightKg: number | null, targetWeightKg: number | null | undefined): number | null {
  if (weightKg == null || targetWeightKg == null || !Number.isFinite(targetWeightKg)) return null;
  return roundToOne(weightKg - targetWeightKg);
}

export function formatWeight(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "--";
  return `${value.toFixed(1)} kg`;
}

export function formatWeightDelta(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "--";
  if (value === 0) return "Even";
  return `${value > 0 ? "+" : "−"}${Math.abs(value).toFixed(1)} kg`;
}

export function formatBodyweightDate(value: string | null | undefined): string {
  const normalized = String(value || "").trim();
  if (!normalized) return "No entry yet";
  return formatAppDate(normalized);
}

export function formatFastedState(value: boolean | null | undefined): string {
  if (value === true) return "Fasted";
  if (value === false) return "Fed";
  return "Not set";
}

export function createBodyweightEntryKey(entry: NutritionBodyweightLogEntry, sourceIndex: number): string {
  return [entry.date, entry.time ?? "", entry.weight_kg, entry.notes ?? "", entry.is_fasted ?? "", sourceIndex].join("|");
}
