import { isWithinLastDays, trainingDaysAgo } from "@/lib/history";
import type { TrainingActivity, TrainingHistorySession } from "@/lib/training-history";
import { sportLabel, type TrainingSport } from "@/lib/training-disciplines";

/**
 * One discipline's share of the window. A recorded sport decides the bucket;
 * other records keep their activity's bucket. Strength and conditioning are one S&C bucket.
 */
export type TrainingMixRow = { key: string; label: string; sessions: number; minutes: number };

export type TrainingSummary = {
  sessions: number;
  minutes: number;
  /** Counted sessions with no recorded duration: never estimated into `minutes`. */
  untimed: number;
  mix: TrainingMixRow[];
};

const DISCIPLINE: Record<TrainingActivity, { key: string; label: string }> = {
  boxing: { key: "boxing", label: "Boxing" },
  sparring: { key: "sparring", label: "Sparring" },
  strength: { key: "sc", label: "S&C" },
  conditioning: { key: "sc", label: "S&C" },
  roadwork: { key: "roadwork", label: "Running" },
  other: { key: "other", label: "Other" },
};
// Boxing shares the activity bucket above; it is the same discipline.
const sportDiscipline = (sport: TrainingSport) => sport === "boxing" ? DISCIPLINE.boxing : { key: sport, label: sportLabel(sport) };
// Prescribed sessions keep their own bucket rather than a discipline guessed from a title.
const PLAN = { key: "unlxck", label: "Unlxck plan" };

export function isRecordedTraining(row: TrainingHistorySession): boolean {
  return (row.status === "done" || row.status === "modified")
    && (row.source === "external" || row.completion_origin === "reported");
}

export function trainingDiscipline(row: TrainingHistorySession): { key: string; label: string } {
  return row.source !== "external" ? PLAN : row.sport ? sportDiscipline(row.sport)
    : row.activity ? DISCIPLINE[row.activity] ?? DISCIPLINE.other : PLAN;
}

/**
 * Completed training over the last `days` athlete-local days, from records
 * already on the page. Same rows as the status counts: manual sessions and
 * reported Unlxck completions, done or modified.
 */
export function summarizeTraining(rows: readonly TrainingHistorySession[], today: string, days: number): TrainingSummary {
  const mix = new Map<string, TrainingMixRow>();
  let sessions = 0;
  let minutes = 0;
  let untimed = 0;
  for (const row of rows) {
    if (!isRecordedTraining(row) || !isWithinLastDays(row.training_day, today, days)) continue;
    const discipline = trainingDiscipline(row);
    const entry = mix.get(discipline.key) ?? { ...discipline, sessions: 0, minutes: 0 };
    const duration = row.duration_minutes && row.duration_minutes > 0 ? row.duration_minutes : 0;
    entry.sessions += 1;
    entry.minutes += duration;
    mix.set(discipline.key, entry);
    sessions += 1;
    minutes += duration;
    if (!duration) untimed += 1;
  }
  return {
    sessions, minutes, untimed,
    mix: [...mix.values()].sort((a, b) => b.minutes - a.minutes || b.sessions - a.sessions),
  };
}

/** "9h 15m", "45m", "2h". */
export function formatTrainingDuration(minutes: number): string {
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (!hours) return `${rest}m`;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

/** Rows whose takeaway matches `query` (case-insensitive), newest first as given. */
export function filterTakeaways(rows: readonly TrainingHistorySession[], query: string): TrainingHistorySession[] {
  const needle = query.trim().toLowerCase();
  return rows.filter(row => row.takeaway?.trim() && (!needle || row.takeaway.toLowerCase().includes(needle)
    || row.title.toLowerCase().includes(needle)));
}

/**
 * True when every session of the last `days` training days is loaded. History
 * pages hold whole days, so all days on or after `loadedFrom` are complete;
 * null means nothing older remains to load.
 */
export function summaryWindowLoaded(loadedFrom: string | null, today: string, days: number): boolean {
  if (loadedFrom === null) return true;
  const ago = trainingDaysAgo(loadedFrom, today);
  return ago !== null && ago >= days - 1;
}
