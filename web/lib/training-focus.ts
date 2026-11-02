import { contactTimerTarget } from "./session-timer/contact";
import { TRAINING_SPORTS } from "./training-disciplines";
import type { StructuredDay, StructuredSession, TrainingFocus } from "./types";

/** Dispatched after a sparring report is saved so a shown focus refreshes. */
export const TRAINING_FOCUS_CHANGED_EVENT = "unlxck:training-focus-changed";

export function announceTrainingFocusChanged(): void {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(TRAINING_FOCUS_CHANGED_EVENT));
}

/**
 * Whether a sparring focus belongs before this day's work. Only the app's own
 * classification counts: a contact day (sparring, technical, light combat or
 * coach-led contact, as the round timer reads it) or a session holding sparring
 * blocks. Strength, conditioning, recovery and "skill" sessions (which include
 * mental work like visualisation) do not qualify. When unsure, no reminder.
 */
export function focusFitsDay(day: StructuredDay | null | undefined, sessions: readonly StructuredSession[]): boolean {
  if (contactTimerTarget(day)) return true;
  return sessions.some((session) => (session.blocks ?? []).some((block) => String(block.block_type ?? "").toLowerCase() === "sparring"));
}

/**
 * Whether the focus was recorded for one of the athlete's own sports. Plan and
 * timer sparring carry no sport and follow the athlete's; a manual session's
 * sport must be one the athlete trains (a Muay Thai note is not shown before
 * boxing work).
 */
export function focusFitsSport(focusSport: string | null | undefined, technicalStyle: readonly string[] | null | undefined): boolean {
  if (!focusSport) return true;
  const own = (technicalStyle ?? []).filter((sport) => (TRAINING_SPORTS as readonly string[]).includes(sport));
  return own.includes(focusSport);
}

/** The focus text to show before this day's work, or null. */
export function relevantFocus(
  focus: TrainingFocus | null,
  day: StructuredDay | null | undefined,
  sessions: readonly StructuredSession[],
  technicalStyle: readonly string[] | null | undefined,
): string | null {
  const text = focus?.focus?.trim();
  if (!text) return null;
  return focusFitsSport(focus?.sport, technicalStyle) && focusFitsDay(day, sessions) ? text : null;
}
