import type { DistanceUnit } from "./training-history";

/** Longest run time accepted, matching the API (10 hours). */
export const RUN_TIME_MAX_SECONDS = 36000;
export const DISTANCE_LIMITS: Record<DistanceUnit, number> = { km: 200, miles: 125 };

/** Parses "24:35" (mm:ss) or "1:02:10" (h:mm:ss) into seconds; null when invalid. */
export function parseRunTime(text: string): number | null {
  const parts = text.trim().split(":");
  if (parts.length < 2 || parts.length > 3 || parts.some(part => !/^\d+$/.test(part))) return null;
  const numbers = parts.map(Number);
  const [hours, minutes, seconds] = numbers.length === 3 ? numbers : [0, ...numbers];
  // Leading field may be any size; the rest are clock fields.
  if (seconds > 59 || (numbers.length === 3 && minutes > 59)) return null;
  const total = hours * 3600 + minutes * 60 + seconds;
  return total >= 1 && total <= RUN_TIME_MAX_SECONDS ? total : null;
}

const pad = (value: number) => String(value).padStart(2, "0");

/** 1475 → "24:35", 3730 → "1:02:10". */
export function formatRunTime(totalSeconds: number): string {
  const seconds = Math.round(totalSeconds);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours}:${pad(minutes)}:${pad(seconds % 60)}` : `${minutes}:${pad(seconds % 60)}`;
}

export function distanceUnitLabel(unit: DistanceUnit): string {
  return unit === "km" ? "km" : "mi";
}

/** 5, "km" → "5 km"; 3.1, "miles" → "3.1 mi". */
export function formatDistance(value: number, unit: DistanceUnit): string {
  return `${Number(value.toFixed(2))} ${distanceUnitLabel(unit)}`;
}

/** Pace per unit, e.g. "4:55/km". */
export function formatPace(seconds: number, distance: number, unit: DistanceUnit): string | null {
  if (!(distance > 0) || !(seconds > 0)) return null;
  return `${formatRunTime(seconds / distance)}/${distanceUnitLabel(unit)}`;
}

type RunDetail = { distance_value?: number | null; distance_unit?: DistanceUnit | null; duration_seconds?: number | null; duration_minutes: number | null };

/** "5 km · 24:35 · 4:55/km", or null when the row has no run detail. */
export function runSummary(row: RunDetail): string | null {
  const distance = row.distance_value && row.distance_unit ? formatDistance(row.distance_value, row.distance_unit) : null;
  const time = row.duration_seconds ? formatRunTime(row.duration_seconds) : null;
  if (!distance && !time) return null;
  const pace = distance && row.duration_seconds ? formatPace(row.duration_seconds, row.distance_value!, row.distance_unit!) : null;
  return [distance, time ?? (row.duration_minutes ? `${row.duration_minutes} min` : null), pace].filter(Boolean).join(" · ");
}
