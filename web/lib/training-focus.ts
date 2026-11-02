import focusPatterns from "../../shared/training-focus-patterns.json";
import type { TrainingFocusItem } from "./api-schema.generated";
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
  blocks?: readonly Pick<StructuredBlock, "block_type" | "display_name" | "exercise_key" | "source_session_id">[] | null;
};

/**
 * The one session a focus is matched against: the session this card shows,
 * never the whole day. Today's live prescription is day-wide (every session's
 * blocks in one list), so it is narrowed to this session's blocks by
 * ``source_session_id``; untagged live blocks belong to this session only when
 * the day plans no other session. Anything unattributable is left out, which
 * hides exercise-specific reminders rather than guessing.
 */
export function focusSessionFor({ sessionId, sessionType, planned, cardBlocks, liveBlocks, plannedSessionCount }: {
  sessionId: string | null | undefined;
  sessionType?: string | null;
  /** This session as the plan stores it (by session_id), if found. */
  planned?: FocusSession | null;
  /** The card's own session blocks, used when there is no live prescription for it. */
  cardBlocks?: FocusSession["blocks"];
  /** Today's live (safety-adjusted) prescription blocks, only when the card shows today. */
  liveBlocks?: FocusSession["blocks"];
  plannedSessionCount: number;
}): FocusSession | null {
  if (!sessionId) return null;
  const base = { session_type: planned?.session_type ?? sessionType ?? null, prescription_source: planned?.prescription_source ?? null };
  if (!liveBlocks) return { ...base, blocks: planned?.blocks ?? cardBlocks ?? [] };
  const tagged = liveBlocks.some((block) => block.source_session_id);
  const blocks = tagged
    ? liveBlocks.filter((block) => block.source_session_id === sessionId)
    : plannedSessionCount <= 1 ? liveBlocks : [];
  return { ...base, blocks };
}

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

/** Lowercase, accent-free words ("Push-Ups" -> "push ups"); same as the API's `words`. */
export function focusWords(value: string | null | undefined): string {
  return (value ?? "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
    .replace(/&/g, " and ").replace(/[^a-z0-9]+/g, " ").trim();
}

function containsWords(haystack: string, needle: string): boolean {
  return Boolean(needle) && ` ${haystack} `.includes(` ${needle} `);
}

/** The first shared pattern (shared/training-focus-patterns.json) a name holds, or null. */
export function movementPattern(name: string | null | undefined): string | null {
  const text = focusWords(name);
  return focusPatterns.patterns.find((pattern) => pattern.phrases.some((phrase) => containsWords(text, phrase)))?.id ?? null;
}

// Warm-ups, cool-downs, rehab and mental work are not where a cue is applied.
const NON_TRAINING_BLOCKS = new Set(["preparation", "mobility_activation", "cooldown_recovery", "warmup", "cooldown",
  "rehab", "prehab", "mindset", "nutrition", "recovery"]);

/** The exercise names this session actually prescribes (name and canonical key). */
function exerciseNames(session: FocusSession | null): string[] {
  return (session?.blocks ?? []).flatMap((block) => {
    const type = String(block.block_type ?? "").toLowerCase();
    // Any rehab-labelled type, as the API rule (incl. "prehab", "rehabilitation").
    if (NON_TRAINING_BLOCKS.has(type) || type.includes("rehab")) return [];
    return [focusWords(block.display_name), focusWords(block.exercise_key)].filter(Boolean);
  });
}

/**
 * Whether an inferred focus fits this card's work, at the level its metadata
 * supports: the exact exercise (or its family, "split squat" in "Bulgarian
 * Split Squat"); a movement pattern only when the inference said the issue
 * transfers there; sport technique only before sparring or technical work.
 * Missing session detail means no.
 */
export function inferredFocusFits(item: TrainingFocusItem, target: FocusTarget): boolean {
  // An inferred technique cue must name its sport and meet that exact sport:
  // a boxing cue is never shown before Muay Thai, or when the sport is unknown.
  if (item.scope === "sport_technique") {
    return Boolean(item.sport) && item.sport === target.sport && focusFitsSession(target);
  }
  const names = exerciseNames(target.session);
  if (item.scope === "specific_exercise") {
    const targets = item.exercise_targets.map(focusWords).filter(Boolean);
    return names.some((name) => targets.some((wanted) => containsWords(name, wanted)));
  }
  if (item.scope === "movement_pattern" || item.scope === "general_quality") {
    return names.some((name) => {
      const pattern = movementPattern(name);
      return pattern !== null && item.movement_patterns.includes(pattern);
    });
  }
  return false;
}

export type ShownFocus = { id: string | null; text: string; origin: "athlete" | "inferred" };

/** The one focus to show before this card's work (newest that fits), or null. */
export function relevantFocusItem(focus: TrainingFocus | null, target: FocusTarget): ShownFocus | null {
  // An older API sends only the newest explicit focus.
  const items: TrainingFocusItem[] = focus?.items ?? (focus?.focus ? [{
    id: "", origin: "athlete", focus: focus.focus, training_day: focus.training_day ?? "", sport: focus.sport,
    scope: null, category: null, exercise_targets: [], movement_patterns: [],
  }] : []);
  for (const item of items) {
    const text = item.focus?.trim();
    if (!text) continue;
    const fits = item.origin === "athlete"
      ? focusFitsSport(item.sport, target.sport) && focusFitsSession(target)
      : inferredFocusFits(item, target);
    if (fits) return { id: item.id || null, text, origin: item.origin };
  }
  return null;
}

/** The focus text to show before this card's work, or null. */
export function relevantFocus(focus: TrainingFocus | null, target: FocusTarget): string | null {
  return relevantFocusItem(focus, target)?.text ?? null;
}
