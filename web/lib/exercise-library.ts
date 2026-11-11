import type { PersonalExercise } from "./api-schema.generated";
import { countOnlySetSummary } from "./set-entries";
import { detailMeasurements } from "./session-details";
import { compactTimeUnits } from "./structured-plan";

export function exerciseResult(exercise: PersonalExercise): { label: string; value: string } {
  if (exercise.best_loads.length) return {
    label: exercise.identity_kind === "legacy" ? "Recorded load" : exercise.best_loads.length > 1 ? "Best recorded loads · by unit" : "Best recorded load",
    value: exercise.best_loads.map(load => `${load.value} ${load.unit}`).join(" · "),
  };
  const values = exercise.latest_values;
  const unreported = countOnlySetSummary(values);
  if (unreported) return { label: "Measurements unreported", value: `${values.sets} ${values.sets === 1 ? "set" : "sets"} completed` };
  const parts: string[] = [];
  if (values.sets != null && values.reps != null) parts.push(`${values.sets} × ${values.reps}`);
  for (const item of detailMeasurements(values)) {
    if ((item.label === "Sets" || item.label === "Reps") && values.sets != null && values.reps != null) continue;
    if (item.label === "Rest" || item.label === "Effort") continue;
    parts.push(item.label === "Rounds" || item.label === "Sets" || item.label === "Reps" ? `${item.value} ${item.label.toLowerCase()}` : compactTimeUnits(item.value));
  }
  // A movement logged by name alone was performed; nothing was measured.
  const empty = exercise.latest_source === "manual" ? "No measurements recorded"
    : exercise.latest_confirmed_as_prescribed ? "Completed as prescribed" : "Modified result";
  return { label: exercise.latest_confirmed_as_prescribed ? "Latest confirmed" : "Latest recorded", value: parts.join(" · ") || empty };
}

// One read of the athlete's movement index serves every editor opened soon after.
const PAST_TTL_MS = 5 * 60_000;
let past: { token: string; at: number; request: Promise<ReadonlyMap<string, PersonalExercise>> } | null = null;

/** The athlete's own exercises by canonical key, for prefilling a new prescription. Never fails: no history is an empty map. */
export function pastExercisesByKey(token: string, read: (token: string) => Promise<{ exercises: PersonalExercise[] }>): Promise<ReadonlyMap<string, PersonalExercise>> {
  if (past && past.token === token && Date.now() - past.at < PAST_TTL_MS) return past.request;
  const request = read(token)
    .then(response => new Map(response.exercises.filter(item => item.identity_kind === "canonical")
      .map(item => [item.id.slice("key:".length), item] as const)))
    .catch(() => {
      if (past?.request === request) past = null;
      return new Map<string, PersonalExercise>();
    });
  past = { token, at: Date.now(), request };
  return request;
}

export const EXERCISE_HISTORY_UPDATED = "unlxck:exercise-history-updated";

/** A confirmed manual result must be visible to the next prescription editor. */
export function invalidatePastExercises(token: string): void {
  if (past?.token === token) past = null;
  if (typeof window !== "undefined") window.dispatchEvent(new window.Event(EXERCISE_HISTORY_UPDATED));
}
