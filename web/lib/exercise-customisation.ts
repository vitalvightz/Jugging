import { cleanText } from "@/lib/structured-plan";
import type { StructuredBlock, StructuredPlan, StructuredSession } from "@/lib/types";

/**
 * Athlete edits / swaps / additions / removals on generated and custom plans.
 *
 * The server decides what may change and re-checks every write; these helpers
 * only decide when to OFFER the controls, so a card never shows a menu the
 * server would refuse for an obvious reason (rehab, a past or started day).
 */

/** Block types an athlete may swap or remove (mirrors the server). */
export const EDITABLE_BLOCK_TYPES = new Set([
  "strength",
  "strength_speed",
  "plyometric_power",
  "speed",
  "accessory",
  "conditioning",
  "exercise",
]);

/** Session types an athlete may add an exercise to (mirrors the server). */
export const ADDABLE_SESSION_TYPES = new Set(["strength_power", "conditioning", "mixed", "skill"]);

type ClinicalBlock = StructuredBlock & {
  injury_id?: string | null;
  policy_id?: string | null;
  rehab_drill_id?: string | null;
  clinical_review_pin?: unknown;
};

export function isCustomisableBlock(block: StructuredBlock): boolean {
  const blockType = cleanText(block.block_type) ?? "";
  if (blockType === "rehab" || !cleanText(block.block_id)) return false;
  // Anything the athlete added is theirs to change, whatever its type.
  if (!EDITABLE_BLOCK_TYPES.has(blockType) && !block.athlete_change && block.prescription_source !== "athlete") return false;
  const clinical = block as ClinicalBlock;
  return !(clinical.injury_id || clinical.policy_id || clinical.rehab_drill_id || clinical.clinical_review_pin);
}

export function isCustomisableSession(session: StructuredSession): boolean {
  return Boolean(cleanText(session.session_id));
}

export function canAddToSession(session: StructuredSession): boolean {
  const kind = cleanText(session.session_type) ?? "";
  const title = cleanText(session.title) ?? "";
  return isCustomisableSession(session) && (ADDABLE_SESSION_TYPES.has(kind)
    || (kind === "sparring" && /technical|controlled|light|reduced/i.test(title) && !/hard|full contact/i.test(title)));
}

/** ISO day comparison; an unknown date is never editable. */
export function isEditableDate(date: string | null | undefined, today: string | null | undefined): boolean {
  const day = cleanText(date)?.slice(0, 10);
  const current = cleanText(today)?.slice(0, 10);
  if (!day || !current || !/^\d{4}-\d{2}-\d{2}$/.test(day)) return false;
  return day >= current;
}

/**
 * The plan with one dated session replaced by the server's effective copy.
 * Returns the same object when nothing matched, so a caller can tell.
 */
export function patchPlanSession(
  plan: StructuredPlan | null,
  date: string,
  session: StructuredSession,
): StructuredPlan | null {
  const sessionId = cleanText(session.session_id);
  if (!plan || !sessionId || !Array.isArray(plan.weeks)) return plan;
  let changed = false;
  const weeks = plan.weeks.map((week) => {
    if (!week || !Array.isArray(week.days)) return week;
    let weekChanged = false;
    const days = week.days.map((day) => {
      if (!day || cleanText(day.date)?.slice(0, 10) !== date || !Array.isArray(day.sessions)) return day;
      const index = day.sessions.findIndex((candidate) => cleanText(candidate?.session_id) === sessionId);
      if (index < 0) return day;
      weekChanged = true;
      const sessions = day.sessions.slice();
      // Keep presentation fields the server copy may not repeat.
      sessions[index] = { ...sessions[index], ...session };
      return { ...day, sessions };
    });
    if (!weekChanged) return week;
    changed = true;
    return { ...week, days };
  });
  return changed ? { ...plan, weeks } : plan;
}

export function newClientRequestId(): string {
  const random = globalThis.crypto?.randomUUID?.();
  if (random) return random;
  // RFC 4122 v4 shape for older browsers; uniqueness, not secrecy, is needed.
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (char) => {
    const value = Math.floor(Math.random() * 16);
    return (char === "x" ? value : (value & 0x3) | 0x8).toString(16);
  });
}
