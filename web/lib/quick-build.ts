import {
  detectDeviceTimeZone,
  EQUIPMENT_ACCESS_OPTIONS,
  canonicalizeEquipmentAccessValues,
  KEY_GOAL_OPTIONS,
  TACTICAL_STYLE_OPTIONS,
  TECHNICAL_STYLE_OPTIONS,
  TRAINING_AVAILABILITY_OPTIONS,
  WEAK_AREA_OPTIONS,
  retainKnownOptionValues,
} from "@/lib/intake-options";
import { applyNoScheduledFightSnapshot, canonicalizePerformanceFocus, emptyPlanRequest } from "@/lib/onboarding";
import {
  buildDaysOutContext,
  computeDaysUntilFight,
  filterAvailablePerformanceFocusValues,
} from "@/lib/days-out-policy";
import { validatePerformanceFocusSelections } from "@/lib/performance-focus-cap";
import { getSparringConsistency, HARD_SPARRING_DAY_CAP } from "@/lib/training-schedule";
import { buildAthleteInjuryTexts } from "@/lib/guided-injury";
import type { PlanRequest } from "@/lib/types";

import { readInjuryImpact, writeInjuryImpact, type InjuryImpact } from "@/lib/injury-impact";

export const QUICK_BUILD_KEY_GOAL_CAP = 3;
export const QUICK_BUILD_WEAK_AREA_CAP = 2;
const ROUNDS_FORMAT_PATTERN = /^\d+\s*[xX]\s*\d+$/;
const FIGHT_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

export type QuickBuildInput = {
  full_name: string;
  technical_style: string[];
  tactical_style: string[];
  fight_date: string;
  event_name?: string;
  no_scheduled_fight: boolean;
  rounds_format: string;
  weekly_training_frequency: number;
  training_availability: string[];
  hard_sparring_days: string[];
  support_work_days: string[];
  equipment_access: string[];
  key_goals: string[];
  weak_areas: string[];
  injuries: string;
  injury_impact?: InjuryImpact | "";
  guided_injuries?: PlanRequest["guided_injuries"];
};

export function emptyQuickBuildInput(fullName = ""): QuickBuildInput {
  return {
    full_name: fullName,
    technical_style: [],
    tactical_style: [],
    fight_date: "",
    no_scheduled_fight: false,
    rounds_format: "3 x 3",
    weekly_training_frequency: 4,
    training_availability: [],
    hard_sparring_days: [],
    support_work_days: [],
    equipment_access: [],
    key_goals: [],
    weak_areas: [],
    injuries: "",
  };
}

function normalizeTrainingDays(values: string[] | undefined): string[] {
  return (values ?? []).map((day) =>
    TRAINING_AVAILABILITY_OPTIONS.find((option) => option.value.toLowerCase() === day.trim().toLowerCase())?.value,
  ).filter((day): day is string => Boolean(day));
}

export function planRequestToQuickBuildInput(plan: PlanRequest): QuickBuildInput {
  const guidedInjuries = plan.guided_injuries?.length ? plan.guided_injuries : plan.guided_injury ? [plan.guided_injury] : [];
  const trainingAvailability = normalizeTrainingDays(plan.training_availability);
  // Preserve legacy session counts until the athlete explicitly selects days.
  const weeklyFrequency = plan.weekly_training_frequency ?? 4;
  return {
    full_name: plan.athlete?.full_name ?? "",
    technical_style: retainKnownOptionValues(
      plan.athlete?.technical_style ?? [],
      TECHNICAL_STYLE_OPTIONS,
    ).slice(0, 1),
    tactical_style: retainKnownOptionValues(
      plan.athlete?.tactical_style ?? [],
      TACTICAL_STYLE_OPTIONS,
    ).slice(0, 1),
    fight_date: plan.no_scheduled_fight ? "" : (plan.fight_date ?? ""),
    event_name: plan.no_scheduled_fight ? "" : (plan.event_name ?? ""),
    no_scheduled_fight: Boolean(plan.no_scheduled_fight),
    rounds_format: plan.rounds_format || "3 x 3",
    weekly_training_frequency: weeklyFrequency,
    training_availability: trainingAvailability,
    hard_sparring_days: normalizeTrainingDays(plan.hard_sparring_days)
      .filter((day) => trainingAvailability.includes(day))
      .slice(0, HARD_SPARRING_DAY_CAP),
    support_work_days: normalizeTrainingDays(plan.support_work_days)
      .filter((day) => trainingAvailability.includes(day)),
    equipment_access: retainKnownOptionValues(
      canonicalizeEquipmentAccessValues(plan.equipment_access ?? []),
      EQUIPMENT_ACCESS_OPTIONS,
    ),
    key_goals: retainKnownOptionValues(plan.key_goals ?? [], KEY_GOAL_OPTIONS).slice(
      0,
      QUICK_BUILD_KEY_GOAL_CAP,
    ),
    weak_areas: retainKnownOptionValues(plan.weak_areas ?? [], WEAK_AREA_OPTIONS).slice(
      0,
      QUICK_BUILD_WEAK_AREA_CAP,
    ),
    // When the athlete completed the advanced intake, plan.injuries holds the
    // planner's structured comprehension ("Left shoulder is bruised (low,
    // stable). Type: surface_injury. Surface: bruise..."), not their own words.
    // Show what they actually typed; fall back to the free-text field only when
    // there are no structured guided injuries to draw from.
    injuries: writeInjuryImpact(buildAthleteInjuryTexts(guidedInjuries) || (plan.injuries ?? "").trim(), ""),
    injury_impact: guidedInjuries.length
      ? (new Set(guidedInjuries.map(injury => readInjuryImpact(injury.notes ?? "")?.value ?? "")).size === 1
        ? readInjuryImpact(guidedInjuries[0].notes ?? "")?.value ?? "" : "")
      : readInjuryImpact(plan.injuries ?? "")?.value ?? "",
    guided_injuries: guidedInjuries,
  };
}

function isFutureOrToday(value: string, now: Date = new Date()): boolean {
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) return false;
  const [, y, m, d] = match;
  const fightUtc = Date.UTC(Number(y), Number(m) - 1, Number(d));
  const todayUtc = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  return Number.isFinite(fightUtc) && fightUtc >= todayUtc;
}

export type QuickBuildValidationErrors = Partial<Record<keyof QuickBuildInput | "combat_sessions" | "focus_cap", string>>;

export function sanitizeQuickBuildFocusByDaysOut(input: QuickBuildInput, now?: Date): Pick<QuickBuildInput, "key_goals" | "weak_areas"> {
  const daysUntilFight = input.no_scheduled_fight ? null : computeDaysUntilFight(input.fight_date, now);
  const daysOutCtx = buildDaysOutContext(daysUntilFight, {
    hasHardSparring: input.hard_sparring_days.length > 0,
  });
  return {
    key_goals: filterAvailablePerformanceFocusValues(daysOutCtx, "key_goals", input.key_goals),
    weak_areas: filterAvailablePerformanceFocusValues(daysOutCtx, "weak_areas", input.weak_areas),
  };
}

export function validateQuickBuildInput(
  input: QuickBuildInput,
  options?: { now?: Date; timeZone?: string | null },
): QuickBuildValidationErrors {
  const errors: QuickBuildValidationErrors = {};

  if (!input.full_name.trim()) {
    errors.full_name = "Add your full name.";
  }
  if (input.technical_style.length === 0) {
    errors.technical_style = "Choose a combat sport.";
  } else if (input.technical_style.length > 1) {
    errors.technical_style = "Pick only one combat sport.";
  }
  if (input.tactical_style.length > 1) {
    errors.tactical_style = "Pick only one tactical style.";
  }
  if (input.no_scheduled_fight) {
    if (input.fight_date) {
      errors.fight_date = "Clear the fight date or turn off \"no scheduled fight\".";
    }
  } else {
    if (!input.fight_date) {
      errors.fight_date = "Add a fight date or choose open camp.";
    } else if (!FIGHT_DATE_PATTERN.test(input.fight_date)) {
      errors.fight_date = "Fight date must be in YYYY-MM-DD format.";
    } else if (!isFutureOrToday(input.fight_date, options?.now)) {
      errors.fight_date = "Fight date cannot be in the past.";
    }
  }
  if (!ROUNDS_FORMAT_PATTERN.test(input.rounds_format)) {
    errors.rounds_format = "Pick a rounds format.";
  }
  if (!Number.isInteger(input.weekly_training_frequency) || input.weekly_training_frequency < 1 || input.weekly_training_frequency > 7) {
    errors.weekly_training_frequency = "Training days per week must be between 1 and 7.";
  }
  if (input.training_availability.length === 0) {
    errors.training_availability = "Choose at least one training day.";
  } else if (input.weekly_training_frequency > input.training_availability.length) {
    errors.training_availability = "Training days per week cannot exceed selected available days.";
  }
  const combatSchedule = getSparringConsistency(
    input.training_availability,
    input.hard_sparring_days,
    input.support_work_days,
    !input.no_scheduled_fight,
    input.weekly_training_frequency,
  );
  if (combatSchedule.hardError) {
    errors.combat_sessions = combatSchedule.hardError;
  }
  if (input.equipment_access.length === 0) {
    errors.equipment_access = "Choose your equipment.";
  }
  if (input.key_goals.length === 0) {
    errors.key_goals = "Choose at least one focus.";
  } else if (input.key_goals.length > QUICK_BUILD_KEY_GOAL_CAP) {
    errors.key_goals = `Pick at most ${QUICK_BUILD_KEY_GOAL_CAP} goals.`;
  }
  if (input.key_goals.some((goal) => KEY_GOAL_OPTIONS.some((option) => option.value === goal && option.disabled))) {
    errors.key_goals = "Selected goal is not available.";
  }
  if (input.weak_areas.length > QUICK_BUILD_WEAK_AREA_CAP) {
    errors.weak_areas = `Pick at most ${QUICK_BUILD_WEAK_AREA_CAP} weak areas.`;
  }
  const sanitizedFocus = sanitizeQuickBuildFocusByDaysOut(input, options?.now);
  if (sanitizedFocus.key_goals.length !== input.key_goals.length) {
    errors.key_goals = "One or more goals are not available for this fight window.";
  }
  if (sanitizedFocus.weak_areas.length !== input.weak_areas.length) {
    errors.weak_areas = "One or more weak areas are not available for this fight window.";
  }

  if (input.no_scheduled_fight || (input.fight_date && !errors.fight_date)) {
    const focus = validatePerformanceFocusSelections(
      input.no_scheduled_fight ? null : input.fight_date,
      { keyGoals: input.key_goals, weakAreas: input.weak_areas },
      { now: options?.now, timeZone: options?.timeZone },
    );
    if (focus.isOverCap && focus.errorMessage) {
      errors.focus_cap = focus.errorMessage;
    }
  }

  return errors;
}

export function quickBuildToPlanRequest(
  input: QuickBuildInput,
  options?: { savedTimeZone?: string | null },
): PlanRequest {
  const trimmedName = input.full_name.trim();
  const base = emptyPlanRequest(trimmedName);
  const keyGoals = retainKnownOptionValues(input.key_goals, KEY_GOAL_OPTIONS);
  const weakAreas = retainKnownOptionValues(input.weak_areas, WEAK_AREA_OPTIONS);
  const trainingAvailability = normalizeTrainingDays(input.training_availability);
  const plan: PlanRequest = {
    ...base,
    athlete: {
      ...base.athlete,
      full_name: trimmedName,
      technical_style: retainKnownOptionValues(input.technical_style, TECHNICAL_STYLE_OPTIONS),
      tactical_style: retainKnownOptionValues(input.tactical_style, TACTICAL_STYLE_OPTIONS),
      // A saved profile timezone always wins; the device zone only seeds a first-time setup.
      athlete_timezone: options?.savedTimeZone?.trim() || base.athlete.athlete_timezone || detectDeviceTimeZone(),
    },
    fight_date: input.no_scheduled_fight ? "" : input.fight_date,
    event_name: input.no_scheduled_fight ? "" : (input.event_name ?? "").trim(),
    no_scheduled_fight: input.no_scheduled_fight,
    rounds_format: input.rounds_format || "3 x 3",
    weekly_training_frequency: input.weekly_training_frequency,
    // Quick Build does not ask for fatigue. Do not turn absence into a health
    // self-report; JSON serialization omits this optional field.
    fatigue_level: undefined,
    training_availability: trainingAvailability,
    hard_sparring_days: normalizeTrainingDays(input.hard_sparring_days)
      .filter((day) => trainingAvailability.includes(day))
      .slice(0, HARD_SPARRING_DAY_CAP),
    support_work_days: normalizeTrainingDays(input.support_work_days)
      .filter((day) => trainingAvailability.includes(day)),
    equipment_access: retainKnownOptionValues(
      canonicalizeEquipmentAccessValues(input.equipment_access),
      EQUIPMENT_ACCESS_OPTIONS,
    ),
    injuries: input.injuries.trim() ? writeInjuryImpact(input.injuries.trim(), input.injury_impact ?? "") : "",
    // Retain detailed onboarding injury and clinician fields. Changing impact
    // updates only that report; it never erases the original symptom/avoidance.
    guided_injuries: input.injuries.trim() ? input.guided_injuries?.map(injury => ({
      ...injury,
      notes: input.injury_impact ? writeInjuryImpact(injury.notes ?? "", input.injury_impact) : injury.notes,
    })) : [],
    key_goals: keyGoals,
    primary_goal: keyGoals[0] ?? "",
    weak_areas: weakAreas,
    primary_weak_area: weakAreas[0] ?? "",
    goal_weakness_collision_detail: "",
    goal_weakness_collision_tags: [],
    goal_weakness_collision_details: [],
    training_preference: "",
    mindset_challenges: "",
    notes: "",
  };
  return canonicalizePerformanceFocus(applyNoScheduledFightSnapshot(plan, input.no_scheduled_fight));
}
