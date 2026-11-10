import { lastLoadFor, lastPerformanceFor } from "../exercise-log";
import type { ExerciseLogRecord, ExerciseRecentLoad, StructuredBlock } from "../types";
import type { TimerItem } from "./plan";

/** Suggestions only; the timer retains its separate confirmation markers. */
export function withHistoricalTimerLoads(items: TimerItem[], blocks: StructuredBlock[],
  recentLoads?: readonly ExerciseRecentLoad[], history?: readonly ExerciseLogRecord[]): TimerItem[] {
  return items.map(item => {
    if (item.kind !== "sets" || !item.setFields) return item;
    const block = blocks.find(block => block.block_id === item.blockId);
    if (!block || block.prescription_source === "athlete") return item;
    const previous = lastPerformanceFor(block, history);
    if (previous?.status === "skipped" || previous?.reason === "pain") return item;
    const field = item.setFields.find(field => field.key === "load");
    if (!field || field.prescribed) return item;
    const unit = field.apiUnit;
    const fallback = lastLoadFor(block, recentLoads);
    return { ...item, suggestedLoads: Array.from({ length: item.sets?.max ?? 1 }, (_, at) => {
      const entries = previous?.actual.set_entries;
      const load = entries?.find((row, index) => (row.set_number ?? index + 1) === at + 1)?.load
        ?? previous?.actual.load ?? (entries?.length ? null : fallback);
      return load && load.unit === unit && load.value > 0 ? String(load.value) : null;
    }) };
  });
}
