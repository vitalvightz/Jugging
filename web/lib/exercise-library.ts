import type { PersonalExercise } from "./api-schema.generated";
import { countOnlySetSummary } from "./set-entries";
import { detailMeasurements } from "./session-details";
import { compactTimeUnits } from "./structured-plan";

export function exerciseResult(exercise: PersonalExercise): { label: string; value: string } {
  if (exercise.best_loads.length) {
    // A weight the athlete entered is "recorded"; one inferred from marking a prescribed
    // workout Done still counts as a best, but is named as prescribed, never as recorded.
    const inferred = exercise.best_loads.filter(load => load.basis === "confirmed_prescription").length;
    const many = exercise.best_loads.length > 1;
    const mixed = inferred > 0 && inferred < exercise.best_loads.length;
    const kind = mixed ? "" : inferred ? "prescribed " : "recorded ";
    const label = exercise.identity_kind === "legacy" ? (mixed ? "Loads" : inferred ? "Prescribed load" : "Recorded load")
      : `Best ${kind}load${many ? "s · by unit" : ""}`;
    return {
      label: inferred && !mixed ? `${label} · marked Done` : label,
      value: exercise.best_loads.map(load => `${load.value} ${load.unit}${mixed
        ? load.basis === "confirmed_prescription" ? " (prescribed, marked Done)" : " (recorded)" : ""}`).join(" · "),
    };
  }
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

// Share concurrent reads; a newly opened editor refreshes current restrictions.

let past: { token: string; request: Promise<ReadonlyMap<string, PersonalExercise>> } | null = null;

/** Canonical keys and exact private names; an unavailable history stays empty. */
export function pastExercisesByKey(token: string, read: (token: string) => Promise<{ exercises: PersonalExercise[] }>): Promise<ReadonlyMap<string, PersonalExercise>> {
  if (past && past.token === token) return past.request;
  const request = read(token)
    .then(response => new Map(response.exercises.toSorted((a, b) => a.latest_day.localeCompare(b.latest_day)).filter(item => item.identity_kind === "canonical" || item.identity_kind === "personal"
      || item.latest_performance?.prescribed.prescription_source === "athlete")
      .map(item => [item.identity_kind === "canonical" ? item.id.slice("key:".length)
        : `custom-name:${item.name.trim().toLowerCase().replace(/\s+/g, " ")}`, item] as const)))
    .catch(() => new Map<string, PersonalExercise>()).finally(() => { if (past?.request === request) past = null; });
  past = { token, request };
  return request;
}

export const EXERCISE_HISTORY_UPDATED = "unlxck:exercise-history-updated";

/** A confirmed manual result must be visible to the next prescription editor. */
export function invalidatePastExercises(token: string): void {
  if (past?.token === token) past = null;
  if (typeof window !== "undefined") window.dispatchEvent(new window.Event(EXERCISE_HISTORY_UPDATED));
}
