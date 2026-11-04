// Weight-to-target in a camp's final weeks: when Today and Home show it, the
// weigh-in moment, and the athlete-facing status. The status itself comes from
// the API (NutritionDerivedState.weight_cut_status), which reads the plan's own
// weight-cut severity score; nothing here scores a cut. Pure and defensive.
import { formatAppDate } from "./date-format.ts";
import { getFightCountdown } from "./fight-countdown.ts";
import type { NutritionDerivedState, WeighInType } from "@/lib/types";

/** Today and Home show weight-to-target from four weeks out. */
export const FINAL_WEEKS_DAYS = 28;

const MS_PER_DAY = 86_400_000;

export function daysToFight(fightDate: string | null | undefined, trainingDay: string | null | undefined): number | null {
  return getFightCountdown({ fightDate, trainingDay })?.daysOut ?? null;
}

export function inFinalWeeks(fightDate: string | null | undefined, trainingDay: string | null | undefined): boolean {
  const days = daysToFight(fightDate, trainingDay);
  return days !== null && days <= FINAL_WEEKS_DAYS;
}

/** "Fri 13 Nov 2026 · 18:00": the day before or of the fight, as set. */
export function weighInMoment(
  fightDate: string | null | undefined,
  weighInType: WeighInType | null | undefined,
  weighInTime: string | null | undefined,
): string | null {
  const iso = String(fightDate ?? "").trim().slice(0, 10);
  const fight = /^\d{4}-\d{2}-\d{2}$/.test(iso) ? new Date(`${iso}T12:00:00Z`) : null;
  const time = String(weighInTime ?? "").trim() || null;
  if (weighInType === "informal") return ["Informal weigh-in", time].filter(Boolean).join(" · ");
  if (!fight || Number.isNaN(fight.getTime()) || (weighInType !== "day_before" && weighInType !== "same_day")) return null;
  const day = new Date(fight.getTime() - (weighInType === "day_before" ? MS_PER_DAY : 0)).toISOString().slice(0, 10);
  return [formatAppDate(day), time].filter(Boolean).join(" · ");
}

/** "18 days left" for the card's kicker. */
export function daysLeftLabel(days: number): string {
  if (days <= 0) return "Fight day";
  return days === 1 ? "1 day left" : `${days} days left`;
}

/** The weigh-in for the card's row: "Sat 21 Nov · 18:00" and whether it is official. */
export function weighInDetails(
  fightDate: string | null | undefined,
  weighInType: WeighInType | null | undefined,
  weighInTime: string | null | undefined,
): { when: string | null; label: "Official weigh-in" | "Informal weigh-in" } | null {
  const time = String(weighInTime ?? "").trim() || null;
  if (weighInType === "informal") return { when: time, label: "Informal weigh-in" };
  const moment = weighInMoment(fightDate, weighInType, null);
  if (!moment) return null;
  return { when: [moment.replace(/\s\d{4}$/, ""), time].filter(Boolean).join(" · "), label: "Official weigh-in" };
}

/**
 * How far the camp weight has come from where the final weeks started toward
 * the target. The start is the first daily reading in the window, read from the
 * API's one-reading-per-day trend, so the bar fills as each weight is logged.
 * Null without a start, or when the start was already at or under target.
 */
export function cutProgress(input: {
  trend: ReadonlyArray<{ date: string; scale_weight_kg: number }> | null | undefined;
  fightDate: string | null | undefined;
  currentKg: number | null;
  targetKg: number | null;
}): { startKg: number; fraction: number } | null {
  const iso = String(input.fightDate ?? "").trim().slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(iso) || input.currentKg == null || input.targetKg == null) return null;
  const windowStart = new Date(new Date(`${iso}T12:00:00Z`).getTime() - FINAL_WEEKS_DAYS * MS_PER_DAY).toISOString().slice(0, 10);
  const start = (input.trend ?? []).find(point => point.date >= windowStart && point.date <= iso);
  if (!start || start.scale_weight_kg <= input.targetKg) return null;
  const fraction = (start.scale_weight_kg - input.currentKg) / (start.scale_weight_kg - input.targetKg);
  return { startKg: start.scale_weight_kg, fraction: Math.max(0, Math.min(1, fraction)) };
}

export type CutStatusView = { label: string; tone: "green" | "amber" | "red"; detail: string };

const kg = (value: number) => `${value.toFixed(1)} kg`;
const daysOutPhrase = (days: number) => (days === 0 ? "on fight day" : `${days} ${days === 1 ? "day" : "days"} out`);

/** The API's status in words. The copy says who to talk to, never how to cut. */
export function describeCutStatus(derived: NutritionDerivedState | null | undefined): CutStatusView | null {
  if (!derived) return null;
  const toGo = derived.kg_to_target ?? null;
  switch (derived.weight_cut_status) {
    case "on_target":
      return { label: "On target", tone: "green", detail: "You're at or under your target weight." };
    case "on_track":
      return { label: "On track", tone: "green", detail: toGo != null ? `${kg(toGo)} to go.` : "Your cut is on track." };
    case "behind": {
      const behind = derived.weight_cut_behind_kg ?? null;
      const days = derived.days_until_fight;
      return {
        label: behind != null ? `Behind by ${kg(behind)}` : "Behind",
        tone: "amber",
        detail: `${days != null && days >= 0 ? `Steep for ${daysOutPhrase(days)}. ` : ""}Talk to your coach about your plan.`,
      };
    }
    case "too_aggressive":
      return {
        label: "Cut too aggressive",
        tone: "red",
        detail: "Don't attempt this without your coach or a medical professional overseeing it.",
      };
    default:
      return null;
  }
}
