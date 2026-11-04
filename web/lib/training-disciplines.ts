import disciplines from "../../shared/training-disciplines.json";
import type { TrainingHistorySession as ApiTrainingHistorySession } from "./api-schema.generated";
import type { TrainingActivity, TrainingHistorySession } from "./training-history";

/** Optional detail on manual training. `activity` stays the stored high-level kind. */
export const TRAINING_SPORTS = ["boxing", "muay_thai", "kickboxing", "mma", "bjj", "wrestling"] as const;
export type TrainingSport = (typeof TRAINING_SPORTS)[number];
// The API's own unions, generated from shared/training-disciplines.json.
export type TrainingSessionType = NonNullable<ApiTrainingHistorySession["session_type"]>;
type Same<A, B> = [A] extends [B] ? [B] extends [A] ? true : false : false;
// Fails to compile when the sport list here and the API's drift apart.
export const SPORTS_MATCH_API: Same<TrainingSport, NonNullable<ApiTrainingHistorySession["sport"]>> = true;
type SessionTypeEntry = { id: TrainingSessionType; label: string; activity?: string };
type SportEntry = { id: TrainingSport; label: string; activity: TrainingActivity; session_types: SessionTypeEntry[] };

const SPORTS = new Map((disciplines.sports as SportEntry[]).map(sport => [sport.id, sport]));

export type TrainingClassification = {
  activity?: TrainingActivity | null;
  sport?: TrainingSport | null;
  session_type?: TrainingSessionType | null;
};

export function sportLabel(sport: TrainingSport): string {
  return SPORTS.get(sport)?.label ?? sport;
}

export function sessionTypes(sport: TrainingSport | null | undefined): readonly SessionTypeEntry[] {
  return (sport && SPORTS.get(sport)?.session_types) || [];
}

/** The one activity a sport/session-type pair is stored with (the API rejects any other). */
export function activityFor(sport: TrainingSport, sessionType?: TrainingSessionType | null): TrainingActivity {
  const entry = SPORTS.get(sport)!;
  const kind = sessionType ? entry.session_types.find(item => item.id === sessionType) : undefined;
  return (kind?.activity ?? entry.activity) as TrainingActivity;
}

const capitalise = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);
const activityLabel = (activity: TrainingActivity) => activity === "roadwork" ? "Running" : capitalise(activity);

/** Present the existing roadwork activity as Running without changing stored records. */
export function trainingTitle(row: TrainingHistorySession): string {
  return row.source === "external" && row.activity === "roadwork" ? "Running" : row.title;
}

/** "Muay Thai · Clinch"; older records without a sport keep their activity label. */
export function trainingLabel(row: TrainingClassification): string {
  const entry = row.sport ? SPORTS.get(row.sport) : undefined;
  if (!entry) return row.activity ? activityLabel(row.activity) : "Session";
  const kind = row.session_type ? entry.session_types.find(item => item.id === row.session_type) : undefined;
  return kind ? `${entry.label} · ${kind.label}` : entry.label;
}

/** A known sport/session type that maps to this activity; anything else is not sent. */
export function isValidClassification(row: TrainingClassification): boolean {
  if (!row.sport) return !row.session_type;
  if (!SPORTS.has(row.sport)) return false;
  if (row.session_type && !sessionTypes(row.sport).some(item => item.id === row.session_type)) return false;
  return row.activity === activityFor(row.sport, row.session_type);
}

/** The athlete's sport from their profile (chosen at sign-up or in a plan), if it is one we log. */
export function athleteSport(technicalStyle: readonly string[] | null | undefined): TrainingSport | null {
  return (technicalStyle ?? []).find((value): value is TrainingSport => (TRAINING_SPORTS as readonly string[]).includes(value)) ?? null;
}

export type TrainingPick = {
  label: string; activity: TrainingActivity; sport: TrainingSport | null; session_type: TrainingSessionType | null;
};

// Shared by every athlete; stored exactly as before, without a sport.
const GENERAL_PICKS: TrainingPick[] = (["strength", "conditioning", "roadwork", "other"] as const)
  .map(activity => ({ label: activityLabel(activity), activity, sport: null, session_type: null }));
// Athletes without a known sport keep the original six choices.
const LEGACY_PICKS: TrainingPick[] = (["boxing", "sparring", "strength", "conditioning", "roadwork", "other"] as const)
  .map(activity => ({ label: activityLabel(activity), activity, sport: null, session_type: null }));

/** The Activity choices: the sport's own session types, then S&C, roadwork and other. */
export function trainingPicks(sport: TrainingSport | null): TrainingPick[] {
  if (!sport) return LEGACY_PICKS;
  return [...sessionTypes(sport).map(kind => ({ label: kind.label, activity: activityFor(sport, kind.id), sport, session_type: kind.id })),
    ...GENERAL_PICKS];
}

/** Whether a record is one of the choices shown for `sport` (older Boxing/Sparring rows are not). */
export function fitsPicks(row: TrainingClassification, sport: TrainingSport | null): boolean {
  return trainingPicks(sport).some(pick => pick.activity === row.activity
    && pick.sport === (row.sport ?? null) && pick.session_type === (row.session_type ?? null));
}
