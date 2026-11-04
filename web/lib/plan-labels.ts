const LABEL_OVERRIDES: Record<string, string> = {
  train_as_planned: "Train as planned",
  modify: "Modify",
  pull_back: "Pull back",
  unavailable: "Unavailable",
  stop_and_report: "Stop and report",

  generated: "Processing",
  publishable_with_flags: "Ready, with review notes",
  ready: "Ready",
  review_required: "Awaiting review",
  held_for_review: "Awaiting review",
  triage_blocked: "Paused for safety review",
  archived: "Archived",

  gpp: "General prep",
  spp: "Specific prep",
  taper: "Taper",
  fight_week: "Fight week",
  reintegration: "Reintegration",

  strength_power: "Strength & power",
  conditioning: "Conditioning",
  skill: "Skill",
  sparring: "Sparring",
  recovery: "Recovery",
  mixed: "Mixed session",
  primer: "Primer",
  fight_or_match: "Fight day",
  technical_combat: "Technical Combat",

  preparation: "Preparation",
  mobility_activation: "Mobility",
  plyometric_power: "Power",
  strength: "Strength",
  accessory: "Accessory",
  cooldown_recovery: "Cooldown",

  red: "Red",
  amber: "Amber",
  green: "Green",

  morning_check_in: "Morning check-in",
};

function normalizeToken(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[\s\-/]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_|_$/g, "");
}

function titleizeLabel(value: string): string {
  return value
    .replace(/[_\-/]+/g, " ")
    .trim()
    .replace(/\s+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

export function formatPlanLabel(value: unknown): string {
  if (typeof value !== "string") {
    return "";
  }
  const trimmed = value.trim();
  if (!trimmed) {
    return "";
  }
  const override = LABEL_OVERRIDES[normalizeToken(trimmed)];
  return override ?? titleizeLabel(trimmed);
}

export function isRawEnumLabel(value: unknown): boolean {
  if (typeof value !== "string") {
    return false;
  }
  const trimmed = value.trim();
  if (!trimmed || /\s/.test(trimmed)) {
    return false;
  }
  if (/^[A-Z][a-z]+$/.test(trimmed)) {
    return false;
  }
  return /^[a-z0-9]+(_[a-z0-9]+)*$/i.test(trimmed);
}

/**
 * Humanize a value only when it looks like a raw backend enum (a single
 * snake/kebab token). Already-human strings like "High pain" are returned
 * untouched so we never re-titlecase prose. Safe to apply defensively at render
 * sites where a field might be either human copy or a leaked enum.
 */
export function humanizeIfRawEnum(value: unknown): string {
  if (typeof value !== "string") {
    return "";
  }
  return isRawEnumLabel(value) ? formatPlanLabel(value) : value;
}

/**
 * A declared Light Combat day only tells the planner the slot is coach-owned
 * technical work. The app never knows how light the gym runs it, so no
 * athlete-facing title calls it "light" anything: it reads as Technical Combat.
 */
export const DECLARED_LIGHT_COMBAT_TITLE = "Technical Combat";

export function isDeclaredLightCombatTitle(title: string): boolean {
  return /\blight\s+(?:technical\s+)?combat\b/i.test(title);
}

const LEADING_WEEKDAY =
  /^\s*(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b\s*[.:,\-\u2013\u2014|]\s*/i;

/**
 * The day card already names the day, so a generated title that opens with its
 * weekday ("Monday. Strength anchor") repeats it. Only a weekday followed by a
 * separator is removed, and never when nothing would be left.
 */
export function stripLeadingWeekday(title: string): string {
  const stripped = title.replace(LEADING_WEEKDAY, "");
  if (!stripped.trim()) {
    return title;
  }
  return stripped.charAt(0).toUpperCase() + stripped.slice(1);
}

/** A session or day title as the athlete sees it. */
export function athleteFacingSessionTitle(title: string): string {
  return isDeclaredLightCombatTitle(title) ? DECLARED_LIGHT_COMBAT_TITLE : stripLeadingWeekday(humanizeIfRawEnum(title));
}
