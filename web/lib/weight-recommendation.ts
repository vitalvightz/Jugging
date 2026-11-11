import { backoffLoadFor, lastLoadFor, lastPerformanceFor, logFieldsForBlock, nextStepFor } from "./exercise-log";
import type { ExerciseLogMeasure, ExerciseLogRecord, ExerciseRecentLoad, StructuredBlock } from "./types";

export type WeightContext = {
  recentLoads?: readonly ExerciseRecentLoad[];
  recentPerformances?: readonly ExerciseLogRecord[];
  allowProgression?: boolean;
  lowerLoads?: boolean;
};
export type WeightRecommendation = {
  last: ExerciseLogMeasure | null;
  suggested: ExerciseLogMeasure | null;
  tryReps: number | null;
  sets: { last: ExerciseLogMeasure | null; suggested: ExerciseLogMeasure | null }[];
};

/** One adapter around the existing progression engine. Suggestions are never actuals. */
export function weightRecommendation(block: StructuredBlock, context: WeightContext = {}): WeightRecommendation {
  const previous = lastPerformanceFor(block, context.recentPerformances);
  const field = logFieldsForBlock(block).find(field => field.key === "load");
  const valid = (load?: ExerciseLogMeasure | null) =>
    load && Number.isFinite(load.value) && load.value > 0 && load.unit === field?.apiUnit ? load : null;
  const entries = previous?.actual.set_entries;
  const fallback = lastLoadFor({ ...block, load: block.load ? { ...block.load, value: undefined } : undefined }, context.recentLoads);
  const measured = valid(previous?.actual.load);
  const last = measured ?? valid(entries?.findLast(row => valid(row.load))?.load) ?? valid(fallback);
  const prescription = field?.prescribed?.[0];
  const prescribed = prescription && field?.apiUnit ? { value: prescription, unit: field.apiUnit } : null;
  const blocked = block.prescription_source === "athlete" || previous?.status === "skipped" || previous?.reason === "pain";
  const beforeEffort = (previous?.prescribed as StructuredBlock | undefined)?.effort;
  const reducedEffort = block.effort?.method === "RPE" && beforeEffort?.method === "RPE"
    && Number(block.effort.value) < Number(beforeEffort.value);
  const lower = context.lowerLoads || block.progression_hold === true || reducedEffort;
  const performanceAt = (load: ExerciseLogMeasure | null) => previous && load
    ? { ...previous, actual: { ...previous.actual, load } } : previous;
  const measuredWork = Boolean(measured || (entries?.length && entries.every(row => valid(row.load))));
  const canProgress = context.allowProgression === true && !lower && measuredWork;
  const next = nextStepFor(block, performanceAt(last), canProgress);
  const reduced = backoffLoadFor(block, performanceAt(last));
  const suggested = prescribed ?? (blocked ? null : reduced ?? (lower ? null : next?.kind === "load" ? next.load : last));
  const sets = Array.from({ length: Math.min(50, Math.max(1, Number(block.sets) || 1)) }, (_, at) => {
    const row = entries?.find((row, index) => (row.set_number ?? index + 1) === at + 1);
    const lastSet = valid(row?.load) ?? measured ?? (entries?.length ? null : valid(fallback));
    const setPerformance = performanceAt(lastSet);
    const setNext = nextStepFor(block, setPerformance, canProgress);
    const setReduction = backoffLoadFor(block, setPerformance);
    return { last: lastSet, suggested: prescribed ?? (blocked || !lastSet ? null :
      setReduction ?? (lower ? null : setNext?.kind === "load" ? setNext.load : lastSet)) };
  });
  return { last, suggested, tryReps: !prescribed && !lower && next?.kind === "reps" ? next.reps : null, sets };
}
