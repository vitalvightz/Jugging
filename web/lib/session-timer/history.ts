import { weightRecommendation, type WeightContext } from "../weight-recommendation";
import type { ExerciseLogRecord, ExerciseRecentLoad, StructuredBlock } from "../types";
import type { TimerItem } from "./plan";

/** The shared recommendation supplies drafts, never confirmation markers. */
export function withHistoricalTimerLoads(items: TimerItem[], blocks: StructuredBlock[],
  recentLoads?: readonly ExerciseRecentLoad[], history?: readonly ExerciseLogRecord[],
  context: Pick<WeightContext, "allowProgression" | "lowerLoads"> = {}): TimerItem[] {
  return items.map(item => {
    const block = blocks.find(block => block.block_id === item.blockId);
    if (item.kind !== "sets" || !item.setFields || !block) return item;
    const recommendation = weightRecommendation(block, { ...context, recentLoads, recentPerformances: history });
    return { ...item, weightRecommendation: recommendation,
      suggestedLoads: recommendation.sets.map(row => row.suggested ? String(row.suggested.value) : null) };
  });
}
