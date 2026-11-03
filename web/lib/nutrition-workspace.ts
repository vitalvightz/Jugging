import type {
  NutritionBodyweightLogEntry,
  NutritionProfileInput,
  NutritionWorkspaceState,
  NutritionWorkspaceUpdateRequest,
  WeighInType,
} from "@/lib/types";

export function toNumber(value: string): number | null {
  if (!value.trim()) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function toList(value: string): string[] {
  return value
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
}

export function toCsv(values: string[]): string {
  return values.join(", ");
}

export function localDateTimeValue(): string {
  const now = new Date();
  const adjusted = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return adjusted.toISOString().slice(0, 16);
}

export function localDateValue(): string {
  return localDateTimeValue().slice(0, 10);
}

export function localTimeValue(): string {
  return localDateTimeValue().slice(11, 16);
}

export function defaultNutritionProfile(existing?: NutritionProfileInput): NutritionProfileInput {
  return {
    sex: existing?.sex ?? null,
    age: existing?.age ?? null,
    height_cm: existing?.height_cm ?? null,
    daily_activity_level: existing?.daily_activity_level ?? null,
    dietary_restrictions: existing?.dietary_restrictions ?? [],
    food_preferences: existing?.food_preferences ?? [],
    meals_per_day_preference: existing?.meals_per_day_preference ?? null,
    foods_avoided_pre_session: existing?.foods_avoided_pre_session ?? [],
    foods_avoided_fight_week: existing?.foods_avoided_fight_week ?? [],
    supplement_use: existing?.supplement_use ?? [],
    caffeine_use: existing?.caffeine_use ?? null,
  };
}

export function emptyUpdateRequest(profile?: NutritionProfileInput): NutritionWorkspaceUpdateRequest {
  return {
    nutrition_profile: defaultNutritionProfile(profile),
    shared_camp_context: {
      fight_date: "",
      rounds_format: "3 x 3",
      weigh_in_type: null,
      weigh_in_time: "",
      current_weight_kg: null,
      current_weight_recorded_at: "",
      current_weight_source: null,
      target_weight_kg: null,
      target_weight_range_kg: null,
      phase_override: null,
      fatigue_level: "low",
      weekly_training_frequency: null,
      training_availability: [],
      hard_sparring_days: [],
      support_work_days: [],
      session_types_by_day: {},
      injuries: "",
      guided_injury: null,
      training_restriction_level: null,
    },
    s_and_c_preferences: {
      equipment_access: [],
      key_goals: [],
      weak_areas: [],
      training_preference: "",
      mindset_challenges: "",
      notes: "",
      random_seed: null,
    },
    nutrition_readiness: { sleep_quality: null, appetite_status: null },
    nutrition_monitoring: { daily_bodyweight_log: [] },
    nutrition_coach_controls: {
      coach_override_enabled: false,
      athlete_override_enabled: false,
      do_not_reduce_below_calories: null,
      protein_floor_g_per_kg: null,
      fight_week_manual_mode: false,
      water_cut_locked_to_manual: false,
    },
  };
}

export function toUpdateRequest(workspace: NutritionWorkspaceState): NutritionWorkspaceUpdateRequest {
  return {
    nutrition_profile: defaultNutritionProfile(workspace.nutrition_profile),
    shared_camp_context: {
      ...workspace.shared_camp_context,
      fight_date: workspace.shared_camp_context.fight_date ?? "",
      rounds_format: workspace.shared_camp_context.rounds_format ?? "3 x 3",
      weigh_in_time: workspace.shared_camp_context.weigh_in_time ?? "",
      current_weight_recorded_at: workspace.shared_camp_context.current_weight_recorded_at ?? "",
      injuries: workspace.shared_camp_context.injuries ?? "",
    },
    s_and_c_preferences: workspace.s_and_c_preferences,
    nutrition_readiness: workspace.nutrition_readiness,
    nutrition_monitoring: workspace.nutrition_monitoring,
    nutrition_coach_controls: workspace.nutrition_coach_controls,
  };
}

/** The update that saves a new weigh-in log and leaves everything else as read. */
export function withBodyweightLog(
  workspace: NutritionWorkspaceState,
  entries: NutritionBodyweightLogEntry[],
): NutritionWorkspaceUpdateRequest {
  const payload = toUpdateRequest(workspace);
  payload.nutrition_monitoring = { ...payload.nutrition_monitoring, daily_bodyweight_log: entries };
  return payload;
}

export type TargetAndWeighIn = {
  target_weight_kg: number | null;
  weigh_in_type: WeighInType | null;
  weigh_in_time: string | null;
};

/**
 * The update that adds a target weight and weigh-in from the tracker. The API
 * keeps a target already set in Camp Setup, so this only fills one that is missing.
 */
export function withTargetAndWeighIn(
  workspace: NutritionWorkspaceState,
  values: TargetAndWeighIn,
): NutritionWorkspaceUpdateRequest {
  const payload = toUpdateRequest(workspace);
  payload.shared_camp_context = {
    ...payload.shared_camp_context,
    target_weight_kg: values.target_weight_kg ?? payload.shared_camp_context.target_weight_kg ?? null,
    weigh_in_type: values.weigh_in_type,
    weigh_in_time: values.weigh_in_time ?? "",
  };
  return payload;
}
