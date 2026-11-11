import type { CustomDose, CustomDoseSuggestion, PersonalExercise } from "./api-schema.generated";
import { personalWeightRecommendation } from "./weight-recommendation";
import { parseLoad } from "./load-entry";
import { withPastDose } from "./load-entry";
export const blankDose = (mode: CustomDose["mode"] = "reps"): CustomDose => ({
  mode,
  sets: null,
  reps: null,
  duration_seconds: null,
  rest_seconds: null,
  load_or_intensity: null,
  rounds: null,
  work_seconds: null,
  rpe: null,
  tempo: null,
  per_side: null,
  distance: null,
  distance_unit: null,
  prescription: null,
});
export function doseSummary(d: CustomDose) {
  const time = (seconds: number) => seconds % 60 === 0 ? `${seconds / 60} min` : `${seconds}s`;
  const parts = [
    d.mode === "reps"
      ? d.sets && d.reps
        ? `${d.sets} × ${d.reps}`
        : "Set dose"
      : d.mode === "duration"
        ? d.duration_seconds
          ? `${d.sets ? `${d.sets} × ` : ""}${time(d.duration_seconds)}`
          : "Set duration"
        : d.mode === "intervals"
          ? d.rounds && d.work_seconds
            ? `${d.rounds} × ${time(d.work_seconds)}`
            : "Set intervals"
          : d.mode === "distance"
            ? d.distance ? `${d.sets ? `${d.sets} × ` : ""}${d.distance} ${d.distance_unit ?? ""}` : "Set distance"
            : d.prescription || "Set prescription",
  ];
  if (d.distance && d.mode !== "distance") parts.push(`${d.distance} ${d.distance_unit ?? ""}`);
  if (d.load_or_intensity) parts.push(d.load_or_intensity);
  if (d.rest_seconds != null) parts.push(`rest ${d.rest_seconds}s`);
  if (d.rpe) parts.push(`RPE ${d.rpe}`);
  if (d.tempo) parts.push(`tempo ${d.tempo}`);
  if (d.per_side) parts.push("per side");
  return parts.join(" · ");
}

export function doseIncomplete(d: CustomDose): boolean {
  if (d.mode === "distance") return !(d.distance && d.distance_unit);
  if (d.mode === "reps") return !(d.sets && d.reps);
  if (d.mode === "duration") return !d.duration_seconds || Boolean(d.distance) !== Boolean(d.distance_unit);
  if (d.mode === "intervals") return !(d.rounds && d.work_seconds) || d.rest_seconds == null;
  return !d.prescription?.trim();
}

const filled = (d: CustomDose) => d.mode === "text" ? Boolean(d.prescription?.trim())
  : Object.entries(d).some(([key, value]) => key !== "mode" && value != null && value !== "");

/**
 * The dose a newly added library exercise starts with: the library's source
 * dose, else its verified format, with the athlete's last session filling what
 * those leave open. Null when no source has anything to say, so nothing is
 * invented. `source` names where the values came from for the athlete.
 */
export function libraryPrefill(
  suggestion: CustomDoseSuggestion | null | undefined,
  past: PersonalExercise | null | undefined,
): { dose: CustomDose; source: string } | null {
  const candidate = (suggestion?.dose && suggestion.dose.mode !== "text" ? suggestion.dose : suggestion?.format_dose) ?? null;
  const format = candidate?.mode === "text" && !filled(candidate) ? null : candidate;
  const start = format ?? blankDose();
  // A verified format owns its mode; only use past values from the same kind of session.
  const compatiblePast = format && !filled(format) && withPastDose(blankDose(), past).mode !== format.mode ? null : past;
  const filledDose = withPastDose(start, compatiblePast, Boolean(format));
  const recommendation = personalWeightRecommendation({
    display_name: past?.name ?? "Exercise", block_type: "strength", sets: filledDose.sets, reps: filledDose.reps,
    load: parseLoad(start.load_or_intensity),
  }, compatiblePast);
  const dose = { ...filledDose, load_or_intensity: recommendation.suggested
    ? `${recommendation.suggested.value} ${recommendation.suggested.unit}`
    : start.load_or_intensity || null };
  if (!filled(dose) && (dose.mode === "reps" || dose.mode === "text")) return null;
  const fromLibrary = Boolean(format && (filled(format) || (format.mode !== "reps" && format.mode !== "text")));
  const fromPast = JSON.stringify(dose) !== JSON.stringify(start);
  return { dose, source: fromLibrary && fromPast ? "Library format · your last session" : fromLibrary ? "Library format" : "Your last session" };
}
