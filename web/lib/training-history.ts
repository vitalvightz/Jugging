import { isValidClassification, type TrainingSessionType, type TrainingSport } from "./training-disciplines";

export const TRAINING_ACTIVITIES = ["sparring", "boxing", "roadwork", "strength", "conditioning", "other"] as const;
export const TRAINING_EFFORTS = ["easy", "moderate", "hard"] as const;
export type TrainingActivity = (typeof TRAINING_ACTIVITIES)[number];
export type TrainingEffort = (typeof TRAINING_EFFORTS)[number];
export type ExternalTrainingFields = {
  activity: TrainingActivity;
  duration_minutes: number;
  effort: TrainingEffort;
  /** Optional; omitted, never guessed, when the athlete does not choose one. */
  sport?: TrainingSport | null;
  session_type?: TrainingSessionType | null;
  /** Optional run detail, roadwork only. Minutes stay ceil(seconds / 60). */
  distance_value?: number | null;
  distance_unit?: DistanceUnit | null;
  duration_seconds?: number | null;
};
export type DistanceUnit = "km" | "miles";
export type TrainingHistorySession = {
  id: string;
  source: "unlxck" | "external";
  completion_origin?: "reported" | "day_credit" | null;
  training_day: string;
  status: "done" | "modified" | "skipped";
  title: string;
  activity: TrainingActivity | null;
  /** Absent or null on older records and on sessions logged without one. */
  sport?: TrainingSport | null;
  session_type?: TrainingSessionType | null;
  duration_minutes: number | null;
  distance_value?: number | null;
  distance_unit?: DistanceUnit | null;
  duration_seconds?: number | null;
  effort: TrainingEffort | null;
  plan_id: string | null;
  session_rpe?: number | null;
  pain_after?: number | null;
  modification_reason?: string | null;
  notes?: string | null;
  /** A manual session's note on what was learned. */
  takeaway?: string;
  /** Live exercise entries on a manual session; null/absent when not counted. */
  exercise_count?: number | null;
  /** A plan session History may edit (RPE, pain, notes) or remove. */
  history_editable?: boolean;
};

/** One page of whole training days; pass `next_before_day` back for older ones. */
export type TrainingHistoryPage = {
  sessions: TrainingHistorySession[];
  next_before_day?: string | null;
};

export const TAKEAWAY_MAX_CHARS = 280;

export function sortTrainingHistory(rows: TrainingHistorySession[]): TrainingHistorySession[] {
  return [...rows].sort((a, b) => b.training_day.localeCompare(a.training_day));
}

/** Only complete manual session fields can seed a new occurrence. */
export function canRepeatTraining(row: TrainingHistorySession): boolean {
  return row.source === "external" && row.status === "done"
    && TRAINING_ACTIVITIES.includes(row.activity as TrainingActivity)
    && Number.isInteger(row.duration_minutes) && row.duration_minutes! >= 1 && row.duration_minutes! <= 600
    && TRAINING_EFFORTS.includes(row.effort as TrainingEffort)
    && isValidClassification(row);
}

export function latestRepeatableTraining(rows: TrainingHistorySession[]): TrainingHistorySession | undefined {
  // Stable sorting retains the API's creation-time ordering within a day.
  return sortTrainingHistory(rows).find(canRepeatTraining);
}
