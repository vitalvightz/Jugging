import { weightRecommendation, type WeightContext } from "../weight-recommendation";
import { lastPerformanceFor, performanceSummary } from "../exercise-log";
import type { ExerciseLogRecord, ExerciseRecentLoad, StructuredBlock } from "../types";
import type { TimerItem } from "./plan";

/** The shared recommendation supplies drafts, never confirmation markers. */
export function withHistoricalTimerLoads(items: TimerItem[], blocks: StructuredBlock[],
  recentLoads?: readonly ExerciseRecentLoad[], history?: readonly ExerciseLogRecord[],
  context: Pick<WeightContext, "allowProgression" | "lowerLoads"> = {}): TimerItem[] {
  return items.map(item => {
    const block = blocks.find(block => block.block_id === item.blockId);
    if (item.kind !== "sets" || !block) return item;
    // The last time this exercise was logged, shown on Up next; never a skipped one.
    const previous = lastPerformanceFor(block, history);
    const lastPerformance = previous && previous.status !== "skipped" ? performanceSummary(previous) || null : null;
    const withLast = lastPerformance ? { ...item, lastPerformance } : item;
    if (!item.setFields) return withLast;
    const recommendation = weightRecommendation(block, { ...context, recentLoads, recentPerformances: history });
    return { ...withLast, weightRecommendation: recommendation,
      suggestedLoads: recommendation.sets.map(row => row.suggested ? String(row.suggested.value) : null) };
  });
}
