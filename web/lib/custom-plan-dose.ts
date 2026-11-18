import type { CustomDose, CustomDoseSuggestion, PersonalExercise } from "./api-schema.generated";
import { personalWeightRecommendation } from "./weight-recommendation";
import { formatExercisePrescription } from "./structured-plan";
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
/** The dose as written. A partly filled dose shows what is there and names
 * what's missing ("3 rounds · duration unspecified"), never a bare "Set …". */
export function doseSummary(d: CustomDose, name?: string | null) {
  if (d.mode === "stages") return [
    ...(d.prescribed_sets ?? []).map(formatExercisePrescription), d.load_or_intensity,
    d.rest_seconds != null ? `rest ${d.rest_seconds}s` : null,
    d.rpe != null ? `RPE ${d.rpe}` : null, d.tempo ? `tempo ${d.tempo}` : null,
  ].filter(Boolean).join("; ");
  if (doseOptional(name) && doseIncomplete(d) && !filled(d)) return "";
  const time = (seconds: number) => seconds % 60 === 0 ? `${seconds / 60} min` : `${seconds}s`;
  const count = (value: number, word: string) => `${value} ${word}${value === 1 ? "" : "s"}`;
  const sets = d.sets ? `${d.sets} × ` : "";
  const parts = [
    d.mode === "reps"
      ? d.sets && d.reps ? `${d.sets} × ${d.reps}`
        : d.sets ? `${count(d.sets, "set")} · reps unspecified`
        : d.reps ? `${count(d.reps, "rep")} · sets unspecified` : "Set dose"
      : d.mode === "duration"
        ? d.duration_seconds ? `${sets}${time(d.duration_seconds)}`
          : d.sets ? `${count(d.sets, "set")} · duration unspecified` : "Set duration"
        : d.mode === "intervals"
          ? d.rounds && d.work_seconds ? `${d.rounds} × ${time(d.work_seconds)}`
            : d.rounds ? `${count(d.rounds, "round")} · duration unspecified`
            : d.work_seconds ? `${time(d.work_seconds)} rounds · number unspecified` : "Set intervals"
          : d.mode === "distance"
            ? d.distance ? `${sets}${d.distance} ${d.distance_unit ?? "· unit unspecified"}`.trim()
              : d.sets ? `${count(d.sets, "set")} · distance unspecified` : "Set distance"
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

/** Warm-ups, cooldowns, stretching and mobility are done, not dosed: they never need a prescription. */
const DOSE_OPTIONAL = /\b(warm[\s-]?(?:up|down)s?|cool[\s-]?downs?|stretch(?:es|ing)?|mobility|flexibility|foam[\s-]?roll(?:ing|er)?|breath(?:ing|work)|yoga)\b/i;
export const doseOptional = (name: string | null | undefined) => DOSE_OPTIONAL.test(name ?? "");

type Dosed = { name?: string | null; snapshot?: { name?: string | null } | null; dose?: CustomDose | null };
/** The exercise still needs its dose finished. Warm-ups, cooldowns, stretching and
 * mobility need none while nothing is written; a started dose ("3 sets") is checked like any other. */
export function exerciseIncomplete(e: Dosed): boolean {
  if (doseOptional(e.name ?? e.snapshot?.name) && (!e.dose || !filled(e.dose))) return false;
  return !e.dose || doseIncomplete(e.dose);
}

export function doseIncomplete(d: CustomDose): boolean {
  if (d.mode === "stages") return !d.prescribed_sets?.length || d.prescribed_sets.some(stage => {
    // Sets, loading and rest describe work, but don't prescribe the work itself.
    const positive = (value: unknown) => typeof value === "number" && Number.isFinite(value) && value > 0;
    const reps = typeof stage.reps === "string" ? stage.reps.trim() : stage.reps;
    const hasReps = typeof reps === "string" ? Boolean(reps) && !/^(?:nan|[-+]?infinity)$/i.test(reps) && (Number.isNaN(Number(reps)) || positive(Number(reps))) : positive(reps);
    return !(hasReps || [stage.duration, stage.work, stage.distance].some(value => positive(value?.value)) || stage.source_text?.trim());
  });
  if (d.mode === "distance") return !(d.distance && d.distance_unit);
  if (d.mode === "reps") return !(d.sets && d.reps);
  if (d.mode === "duration") return !d.duration_seconds || Boolean(d.distance) !== Boolean(d.distance_unit);
  if (d.mode === "intervals") return !(d.rounds && d.work_seconds) || d.rest_seconds == null;
  return !d.prescription?.trim();
}

const filled = (d: CustomDose) => d.mode === "text" ? Boolean(d.prescription?.trim())
  : Object.entries(d).some(([key, value]) => key !== "mode" && value != null && value !== false && !(typeof value === "string" && !value.trim()));

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
  const dose = { ...filledDose, load_or_intensity: start.load_or_intensity || (recommendation.suggested
    ? `${recommendation.suggested.value} ${recommendation.suggested.unit}` : null) };
  if (!filled(dose) && (dose.mode === "reps" || dose.mode === "text")) return null;
  const fromLibrary = Boolean(format && (filled(format) || (format.mode !== "reps" && format.mode !== "text")));
  const fromPast = JSON.stringify(dose) !== JSON.stringify(start);
  return { dose, source: fromLibrary && fromPast ? "Library format · your last session" : fromLibrary ? "Library format" : "Your last session" };
}


export const blankPrescribedSet = (): NonNullable<CustomDose["prescribed_sets"]>[number] => ({
  sets: null, reps: null, percentage: null, weight: null, load: null,
  duration: null, rest: null, effort: null, tempo: null, work: null,
  distance: null, rounds: null, intensity: null, per_side: null, source_text: null,
});
