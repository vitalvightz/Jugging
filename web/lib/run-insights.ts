import { distanceUnitLabel, formatDistance, formatRunTime } from "./run-time";
import type { DistanceUnit, RunType, TrainingHistorySession } from "./training-history";

/** What a logged run is called, as runners label them. */
export const RUN_TYPE_LABELS: Record<RunType, string> = {
  easy: "Easy", tempo: "Tempo", intervals: "Intervals", sprints: "Sprints", long: "Long run", race: "Race",
};

const KM_PER_MILE = 1.609344;
/** Standard distances a run counts toward when it lands within 3% of one. */
const RECORD_DISTANCES = [
  { label: "1 mile", km: KM_PER_MILE }, { label: "5k", km: 5 }, { label: "10k", km: 10 },
  { label: "half marathon", km: 21.0975 }, { label: "marathon", km: 42.195 },
];
/** Runs shorter than this never set the fastest-pace record (a few strides are not a pace). */
const PACE_RECORD_MIN_KM = 1;
/** How many earlier runs the pace and distance averages use. */
const RECENT_RUNS = 5;
/** How many runs the distance trend shows, ending at this one. */
const TREND_RUNS = 8;

type RunRow = Pick<TrainingHistorySession, "id" | "training_day" | "activity" | "source" | "distance_value" | "distance_unit"
  | "duration_seconds" | "duration_minutes"> & { created_at?: string | null };

export const isRun = (row: Pick<TrainingHistorySession, "activity" | "source">) => row.source === "external" && row.activity === "roadwork";

export function toKm(value: number, unit: DistanceUnit): number {
  return unit === "km" ? value : value * KM_PER_MILE;
}
const fromKm = (km: number, unit: DistanceUnit) => unit === "km" ? km : km / KM_PER_MILE;

export function runKm(row: Pick<RunRow, "distance_value" | "distance_unit">): number | null {
  return row.distance_value && row.distance_unit ? toKm(row.distance_value, row.distance_unit) : null;
}

/** Seconds per km; only from an exact time, since whole minutes would blur pace. */
export function paceSecondsPerKm(row: Pick<RunRow, "distance_value" | "distance_unit" | "duration_seconds">): number | null {
  const km = runKm(row);
  return km && row.duration_seconds ? row.duration_seconds / km : null;
}

/** Average speed, e.g. "12.2 km/h". */
export function formatSpeed(row: Pick<RunRow, "distance_value" | "distance_unit" | "duration_seconds">): string | null {
  if (!row.distance_value || !row.distance_unit || !row.duration_seconds) return null;
  const perHour = row.distance_value / (row.duration_seconds / 3600);
  return `${perHour.toFixed(1)} ${row.distance_unit === "km" ? "km/h" : "mph"}`;
}

export type RunTrendPoint = { id: string; day: string; km: number; current: boolean };
export type RunInsights = {
  /** Seconds per the run's unit versus the earlier average; negative is faster. */
  paceDelta: { seconds: number; runs: number } | null;
  /** Distance in the run's unit versus the earlier average; positive is further. */
  distanceDelta: { value: number; runs: number } | null;
  /** Bests this run set among the runs logged before it. */
  records: string[];
  /** Monday-to-Sunday week holding this run, in its unit. */
  week: { distance: number; runs: number; timed: number };
  trend: RunTrendPoint[];
};

const order = (a: RunRow, b: RunRow) => a.training_day.localeCompare(b.training_day)
  || String(a.created_at ?? "").localeCompare(String(b.created_at ?? "")) || a.id.localeCompare(b.id);

function weekStart(day: string): string {
  const date = new Date(`${day}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() - ((date.getUTCDay() + 6) % 7));
  return date.toISOString().slice(0, 10);
}

/**
 * How one run sits among the athlete's other logged runs: pace and distance
 * against their recent average, any bests it set, its week's volume, and the
 * distance trend. Only runs on or before it count, so an old run keeps the
 * records it set at the time.
 */
export function runInsights(run: RunRow, rows: readonly RunRow[]): RunInsights {
  const unit = run.distance_unit ?? "km";
  const runs = [...new Map([...rows.filter(isRun), run].map(row => [row.id, row])).values()].sort(order);
  const index = runs.findIndex(row => row.id === run.id);
  const earlier = runs.slice(0, index).filter(row => row.training_day <= run.training_day);
  const km = runKm(run);
  const pace = paceSecondsPerKm(run);

  const paced = earlier.filter(row => paceSecondsPerKm(row) !== null).slice(-RECENT_RUNS);
  const paceDelta = pace !== null && paced.length
    ? { seconds: fromKmPace(pace - paced.reduce((sum, row) => sum + paceSecondsPerKm(row)!, 0) / paced.length, unit), runs: paced.length } : null;
  const measured = earlier.filter(row => runKm(row) !== null).slice(-RECENT_RUNS);
  const distanceDelta = km !== null && measured.length
    ? { value: fromKm(km - measured.reduce((sum, row) => sum + runKm(row)!, 0) / measured.length, unit), runs: measured.length } : null;

  const records: string[] = [];
  const previousKm = earlier.map(runKm).filter((value): value is number => value !== null);
  if (km !== null && previousKm.length && km > Math.max(...previousKm)) records.push("Longest run");
  if (pace !== null && km !== null && km >= PACE_RECORD_MIN_KM) {
    const previousPace = earlier.filter(row => (runKm(row) ?? 0) >= PACE_RECORD_MIN_KM).map(paceSecondsPerKm).filter((value): value is number => value !== null);
    if (previousPace.length && pace < Math.min(...previousPace)) records.push("Fastest pace");
  }
  for (const target of RECORD_DISTANCES) {
    const near = (value: number | null) => value !== null && Math.abs(value - target.km) / target.km <= 0.03;
    if (!near(km) || !run.duration_seconds) continue;
    const times = earlier.filter(row => near(runKm(row)) && row.duration_seconds).map(row => row.duration_seconds!);
    if (times.length && run.duration_seconds < Math.min(...times)) records.push(`Fastest ${target.label}`);
  }

  const start = weekStart(run.training_day);
  const inWeek = runs.filter(row => weekStart(row.training_day) === start);
  const week = {
    distance: fromKm(inWeek.reduce((sum, row) => sum + (runKm(row) ?? 0), 0), unit),
    runs: inWeek.length,
    timed: inWeek.reduce((sum, row) => sum + (row.duration_seconds ?? (row.duration_minutes ?? 0) * 60), 0),
  };

  const trend = runs.slice(0, index + 1).filter(row => runKm(row) !== null).slice(-TREND_RUNS)
    .map(row => ({ id: row.id, day: row.training_day, km: runKm(row)!, current: row.id === run.id }));
  return { paceDelta, distanceDelta, records, week, trend };
}

/** A per-km pace difference in seconds per the run's own unit. */
function fromKmPace(secondsPerKm: number, unit: DistanceUnit): number {
  return unit === "km" ? secondsPerKm : secondsPerKm * KM_PER_MILE;
}

/** "0:12/km faster than your average over 5 runs", or "Same pace as…" within a second. */
export function describePaceDelta(delta: NonNullable<RunInsights["paceDelta"]>, unit: DistanceUnit): string {
  const rounded = Math.round(delta.seconds);
  const runs = delta.runs === 1 ? "your previous run" : `your average over ${delta.runs} runs`;
  if (rounded === 0) return `Same pace as ${runs}`;
  return `${formatRunTime(Math.abs(rounded))}/${distanceUnitLabel(unit)} ${rounded < 0 ? "faster" : "slower"} than ${runs}`;
}

/** "1.2 km further than your average over 5 runs", or "About the same distance as…". */
export function describeDistanceDelta(delta: NonNullable<RunInsights["distanceDelta"]>, unit: DistanceUnit): string {
  const runs = delta.runs === 1 ? "your previous run" : `your average over ${delta.runs} runs`;
  if (Math.abs(delta.value) < 0.05) return `About the same distance as ${runs}`;
  return `${formatDistance(Math.abs(delta.value), unit)} ${delta.value > 0 ? "further" : "shorter"} than ${runs}`;
}
