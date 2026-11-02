import { TRAINING_SPORTS } from "./training-disciplines";
import type { StructuredBlock, TrainingFocus } from "./types";

/** Dispatched after a sparring report is saved so a shown focus refreshes. */
export const TRAINING_FOCUS_CHANGED_EVENT = "unlxck:training-focus-changed";

export function announceTrainingFocusChanged(): void {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(TRAINING_FOCUS_CHANGED_EVENT));
}

/** The one session a card is about, as the plan stores it. */
export type FocusSession = {
  session_type?: string | null;
  prescription_source?: "athlete" | null;
  blocks?: readonly Pick<StructuredBlock, "block_type">[] | null;
};

export type FocusTarget = {
  /** The session the card shows; null on a day with no app session. */
  session: FocusSession | null;
  /** The day's declared contact (sparring, technical, coach-led) is what the
   * card leads with, as the round timer reads it. */
  contactLeads: boolean;
  /** The upcoming work's sport when reliably known (see `upcomingSport`). */
  sport: string | null;
};

// Athlete-built sessions use their own vocabulary: "Hard Sparring" is
// `sparring` and "Light Technical" is `skill`, often with no exercises at all.
// Generated `skill` sessions also carry mental work (visualisation, tactical
// watch), so a generated session needs real sparring blocks instead.
const ATHLETE_COMBAT_TYPES = new Set(["sparring", "skill"]);

/**
 * Whether a sparring focus belongs before the work this card shows: its
 * declared contact, an athlete-built sparring or technical session, or a
 * session holding sparring blocks. Another session on the same day does not
 * count, and strength, conditioning and recovery never do. Unsure means no.
 */
export function focusFitsSession({ session, contactLeads }: Pick<FocusTarget, "session" | "contactLeads">): boolean {
  if (contactLeads) return true;
  if (!session) return false;
  const type = String(session.session_type ?? "").toLowerCase();
  if (session.prescription_source === "athlete" && ATHLETE_COMBAT_TYPES.has(type)) return true;
  return (session.blocks ?? []).some((block) => String(block.block_type ?? "").toLowerCase() === "sparring");
}

function ownSports(values: readonly string[]): string[] {
  const known = TRAINING_SPORTS as readonly string[];
  return [...new Set(values.map((value) => value.trim().toLowerCase().replace(/[\s-]+/g, "_")).filter((value) => known.includes(value)))];
}

/**
 * The upcoming work's sport, only when it is unambiguous: the plan's single
 * sport, else the athlete's only sport. A plan for "boxing, muay thai" with a
 * two-sport athlete gives null.
 */
export function upcomingSport(planSport: string | null | undefined, athleteSports: readonly string[] | null | undefined): string | null {
  const plan = ownSports((planSport ?? "").split(","));
  if (plan.length === 1) return plan[0];
  const own = ownSports(athleteSports ?? []);
  return plan.length === 0 && own.length === 1 ? own[0] : null;
}

/**
 * Plan and timer sparring carry no sport: they come from the athlete's own
 * plan, so they fit. A manual session's sport must match the upcoming work's
 * known sport; when that is unknown, a sport-specific focus stays hidden.
 */
export function focusFitsSport(focusSport: string | null | undefined, sessionSport: string | null): boolean {
  return !focusSport || focusSport === sessionSport;
}

/** The focus text to show before this card's work, or null. */
export function relevantFocus(focus: TrainingFocus | null, target: FocusTarget): string | null {
  const text = focus?.focus?.trim();
  if (!text) return null;
  return focusFitsSport(focus?.sport, target.sport) && focusFitsSession(target) ? text : null;
}
