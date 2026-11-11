import { backoffLoadFor, lastLoadFor, lastPerformanceFor, logFieldsForBlock, nextStepFor, progressionOpen, progressionRepRange } from "./exercise-log";
import type { ExerciseLogMeasure, ExerciseLogRecord, ExerciseRecentLoad, StructuredBlock } from "./types";

export type WeightContext = {
  recentLoads?: readonly ExerciseRecentLoad[];
  recentPerformances?: readonly ExerciseLogRecord[];
  allowProgression?: boolean;
  lowerLoads?: boolean;
};
export type WeightRecommendation = {
  last: ExerciseLogMeasure | null;
  lastDay?: string | null;
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
  const key = block.exercise_key?.trim();
  const fallbackRecord = (key ? context.recentLoads?.find(row => row.exercise_key === key) : undefined)
    ?? context.recentLoads?.find(row => !row.exercise_key && row.display_name.trim().toLowerCase() === block.display_name?.trim().toLowerCase());
  const lastDay = measured || entries?.some(row => valid(row.load)) ? previous?.training_day : fallbackRecord?.training_day;
  const prescription = field?.prescribed?.[0];
  const prescribed = prescription && field?.apiUnit ? { value: prescription, unit: field.apiUnit } : null;
  const before = previous?.prescribed as StructuredBlock | undefined;
  const todayReps = progressionRepRange(block);
  const beforeReps = before && progressionRepRange(before);
  const legacy = beforeReps != null && todayReps != null && before?.rep_range == null
    && beforeReps[0] === beforeReps[1] && beforeReps[0] === todayReps[0]
    && beforeReps[0] <= todayReps[1];
  // Compare known components independently; absent manual prescriptions still carry forward.
  const incompatible = (typeof block.sets === "number" && typeof before?.sets === "number"
    && block.sets !== before.sets)
    || (todayReps != null && beforeReps != null && !legacy && todayReps.join() !== beforeReps.join());
  const oldLoad = before?.load?.value ?? previous?.actual.load?.value;
  const oldUnit = before?.load?.unit ?? previous?.actual.load?.unit;
  const changedLoad = prescription != null && oldLoad != null && (prescription !== oldLoad || field?.apiUnit !== oldUnit);
  const blocked = incompatible || changedLoad || previous?.status === "skipped" || previous?.reason === "pain";
  const beforeEffort = (previous?.prescribed as StructuredBlock | undefined)?.effort;
  const reducedEffort = block.effort?.method === "RPE" && beforeEffort?.method === "RPE"
    && Number(block.effort.value) < Number(beforeEffort.value);
  const lower = context.lowerLoads || block.progression_hold === true || reducedEffort;
  const performanceAt = (load: ExerciseLogMeasure | null) => previous && load
    ? { ...previous, actual: { ...previous.actual, load } } : previous;
  const measuredWork = Boolean(measured || (entries?.length && entries.every(row => valid(row.load))));
  const canProgress = context.allowProgression === true && !lower && (measuredWork || block.load?.method === "bodyweight" || (!block.load && block.bodyweight_progression === true));
  const next = nextStepFor(block, performanceAt(last), canProgress);
  const reduced = backoffLoadFor(block, performanceAt(last));
  // A varied or partially measured workout has no reliable straight-set draft.
  const individualLoads = Boolean(entries?.length && (entries.length < (Number(block.sets) || 1) || !entries.every(row => valid(row.load))
    || new Set(entries.map(row => row.load?.value)).size > 1));
  const suggested = blocked || lower || individualLoads ? prescribed : reduced ?? (next?.kind === "load" ? next.load : prescribed ?? last);
  const sets = Array.from({ length: Math.min(50, Math.max(1, Number(block.sets) || 1)) }, (_, at) => {
    const row = entries?.find((row, index) => (row.set_number ?? index + 1) === at + 1);
    const lastSet = valid(row?.load) ?? measured ?? (entries?.length ? null : valid(fallback));
    const setPerformance = performanceAt(lastSet);
    const setNext = nextStepFor(block, setPerformance, canProgress);
    const setReduction = backoffLoadFor(block, setPerformance);
    return { last: lastSet, suggested: blocked || lower || !lastSet ? prescribed :
      setReduction ?? (setNext?.kind === "load" ? setNext.load : prescribed ?? lastSet) };
  });
  return { last, lastDay, suggested, tryReps: !blocked && !lower && next?.kind === "reps" ? next.reps : null, sets };
}

/** Route confirmed library evidence and current clearance through the same engine. */
export function personalWeightRecommendation(
  block: StructuredBlock,
  past?: import("./api-schema.generated").PersonalExercise | null,
  context: WeightContext = {},
): WeightRecommendation {
  // A placeholder exposes historical load on unfinished editors; it never earns progression.
  const completeTarget = block.sets != null && block.reps != null;
  return weightRecommendation({ ...block, reps: block.reps ?? 1, prescription_source: "athlete",
    exercise_key: block.exercise_key ?? (past?.id.startsWith("key:") ? past.id.slice(4) : null),
    progression_hold: block.progression_hold === true ? true : past?.progression_hold ?? block.progression_hold,
    bodyweight_progression: past?.bodyweight_progression ?? block.bodyweight_progression },
    { ...context, lowerLoads: context.lowerLoads || past?.allow_progression === false,
      recentPerformances: [...(context.recentPerformances ?? []), ...(past ? [personalPerformance(past, block.exercise_key)] : [])]
        .toSorted((a, b) => (b.training_day ?? "").localeCompare(a.training_day ?? "")),
      recentLoads: [...(context.recentLoads ?? []), ...(past?.latest_values.load ? [{ exercise_key: block.exercise_key ?? (past.id.startsWith("key:") ? past.id.slice(4) : null),
        display_name: past.name, load: past.latest_values.load, training_day: past.latest_day }] : [])],
      allowProgression: completeTarget && (past ? Boolean(past.latest_performance) && past.allow_progression === true && context.allowProgression !== false
        : context.allowProgression === true) });
}


/** Preserve authentic snapshots; never reconstruct a historical workload from today's dose. */
export function personalPerformance(past: import("./api-schema.generated").PersonalExercise, key?: string | null): ExerciseLogRecord {
  if (past.latest_performance) return past.latest_performance;
  return {
    id: past.id, athlete_id: "", plan_id: null, session_id: null, block_id: "",
    training_day: past.latest_day, reason: null, notes: "", created_at: "", updated_at: "",
    exercise_key: key ?? (past.id.startsWith("key:") ? past.id.slice(4) : null),
    prescribed: { display_name: past.name }, actual: past.latest_values,
    status: past.latest_confirmed_as_prescribed ? "as_prescribed" : "modified",
  };
}

/** Manual results join plan history only as measured evidence, never inferred prescriptions. */
export function withManualWeightHistory(history: readonly ExerciseLogRecord[] | undefined,
  library: ReadonlyMap<string, import("./api-schema.generated").PersonalExercise> | null, beforeDay: string) {
  const records = [...(history ?? [])];
  for (const past of library?.values() ?? []) {
    if (past.latest_source !== "manual" || !beforeDay || past.latest_day > beforeDay) continue;
    const manual = personalPerformance(past);
    if (manual.training_day > beforeDay) continue;
    const previous = lastPerformanceFor({ exercise_key: manual.exercise_key,
      display_name: String(manual.prescribed.display_name ?? past.name) }, records);
    if (!previous || manual.training_day > previous.training_day) records.unshift(manual);
  }
  return records;
}

/** Shared readiness policy for Today and Plan Detail; no extra safety inference. */
export function weightContextForToday(state: import("./types").TodayCommandView): Pick<WeightContext, "allowProgression" | "lowerLoads"> {
  return {
    allowProgression: progressionOpen({
      decisionTier: state.today.decision_tier, openInjuries: state.open_injuries.length,
      phase: state.active_plan.phase, safetyHold: state.live_prescription?.safety_hold,
      fightDate: state.active_plan.fight_date, trainingDay: state.today.training_day,
    }),
    lowerLoads: state.live_prescription?.safety_hold === true
      || state.today.decision_tier === "pull_back" || state.today.decision_tier === "stop"
      || /taper|weight.?cut|fight.?week/i.test(state.active_plan.phase ?? ""),
  };
}

/** Calendar-day age for display only; never an expiry or progression input. */
export function compactWeightAge(day?: string | null, now = new Date()): string | null {
  if (!day || !/^\d{4}-\d{2}-\d{2}$/.test(day) || !Number.isFinite(now.getTime())) return null;
  const at = Date.parse(`${day}T00:00:00Z`);
  if (!Number.isFinite(at) || new Date(at).toISOString().slice(0, 10) !== day) return null;
  const days = Math.floor((Date.parse(now.toISOString().slice(0, 10)) - at) / 86_400_000);
  if (days < 0) return null;
  return days < 7 ? `${days}d` : days < 30 ? `${Math.floor(days / 7)}w` : `${Math.floor(days / 30)}mo`;
}
