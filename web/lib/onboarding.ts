import type { MeResponse, PlanRequest } from "@/lib/types";

import { deriveAgeFromDateOfBirth } from "@/lib/compliance";
import { detectDeviceTimeZone } from "@/lib/intake-options";

function isEmptyValue(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === "string") return value.trim() === "";
  if (Array.isArray(value)) return value.length === 0;
  return false;
}

type DraftWithSource = PlanRequest & { plan_source?: string };

function isQuickBuildDraft(draft: PlanRequest | null): draft is DraftWithSource {
  return (draft as DraftWithSource | null)?.plan_source === "quick_build";
}


// `hard_sparring_days` and `support_work_days` are subordinate to
// `training_availability`: the backend rejects any day in them that is not also
// an available training day. They must track the resolved availability, never
// get resurrected independently from a stale layer.
const AVAILABILITY_BOUND_DAY_FIELDS = ["hard_sparring_days", "support_work_days"] as const;

// Layer `top` on `base` field-by-field, preferring `top` only when it carries a non-empty value.
// Lets a partial onboarding_draft pull untouched fields from latest_intake instead of clobbering them.
function mergeIntakeLayers(base: PlanRequest, top: Partial<PlanRequest>): PlanRequest {
  const merged: Record<string, unknown> = { ...base };

  for (const key of Object.keys(top) as (keyof PlanRequest)[]) {
    if (key === "athlete") continue;

    const topValue = top[key];

    // Undefined means "draft never saved this field".
    // Empty string / empty array means "athlete intentionally cleared this field".
    if (topValue !== undefined) {
      merged[key] = topValue;
    }
  }

  const availability = new Set((merged.training_availability as string[] | undefined) ?? []);

  for (const field of AVAILABILITY_BOUND_DAY_FIELDS) {
    const days = (merged[field] as string[] | undefined) ?? [];
    merged[field] = days.filter((day) => availability.has(day));
  }

  if (top.no_scheduled_fight === true) {
    merged.fight_date = "";
    merged.no_scheduled_fight = true;
  }

  return merged as PlanRequest;
}

export function applyNoScheduledFightSnapshot(form: PlanRequest, noScheduledFight: boolean): PlanRequest {
  if (!noScheduledFight) {
    return {
      ...form,
      no_scheduled_fight: false,
    };
  }
  return {
    ...form,
    no_scheduled_fight: true,
    fight_date: "",
    event_name: "",
  };
}

function canonicalizePrimaryFocusValue(
  selectedValues: string[],
  primaryValue: string | null | undefined,
): string {
  if (selectedValues.length === 1) {
    return selectedValues[0] ?? "";
  }
  if (primaryValue && selectedValues.includes(primaryValue)) {
    return primaryValue;
  }
  return "";
}

export function canonicalizePerformanceFocus(form: PlanRequest): PlanRequest {
  const primaryGoal = canonicalizePrimaryFocusValue(form.key_goals ?? [], form.primary_goal);
  const primaryWeakArea = canonicalizePrimaryFocusValue(form.weak_areas ?? [], form.primary_weak_area);

  if (form.primary_goal === primaryGoal && form.primary_weak_area === primaryWeakArea) {
    return form;
  }

  return {
    ...form,
    primary_goal: primaryGoal,
    primary_weak_area: primaryWeakArea,
  };
}

function mergeAthleteLayers(
  base: PlanRequest["athlete"],
  top?: Partial<PlanRequest["athlete"]> | null,
  mode: "non_empty" | "explicit" = "non_empty",
): PlanRequest["athlete"] {
  if (!top) return base;

  const merged: Record<string, unknown> = { ...base };

  for (const key of Object.keys(top) as (keyof PlanRequest["athlete"])[]) {
    const topValue = top[key];

    if (mode === "explicit") {
      if (topValue !== undefined) {
        merged[key] = topValue;
      }
      continue;
    }

    merged[key] = isEmptyValue(topValue) ? base[key] : topValue;
  }

  return merged as PlanRequest["athlete"];
}

export function emptyPlanRequest(fullName = ""): PlanRequest {
  return {
    athlete: {
      full_name: fullName,
      sex: null,
      age: null,
      weight_kg: null,
      target_weight_kg: null,
      height_cm: null,
      technical_style: [],
      tactical_style: [],
      stance: "",
      professional_status: "",
      record: "",
      athlete_timezone: detectDeviceTimeZone(),
      athlete_locale: "",
    },
    fight_date: "",
    no_scheduled_fight: false,
    rounds_format: "3 x 3",
    weekly_training_frequency: 4,
    fatigue_level: "low",
    equipment_access: [],
    training_availability: [],
    hard_sparring_days: [],
    support_work_days: [],
    injuries: "",
    guided_injury: null,
    guided_injuries: [],
    key_goals: [],
    primary_goal: "",
    weak_areas: [],
    primary_weak_area: "",
    goal_weakness_collision_detail: "",
    goal_weakness_collision_tags: [],
    goal_weakness_collision_details: [],
    training_preference: "",
    mindset_challenges: "",
    notes: "",
  };
}

export function hydratePlanRequest(me: MeResponse | null): PlanRequest {
  const fallback = emptyPlanRequest(me?.profile.full_name ?? "");
  if (!me) {
    return fallback;
  }

  const base = me.latest_intake ?? fallback;
  const draftSource = (me.profile.onboarding_draft as PlanRequest | null | undefined) ?? null;
  const normalizedDraft = draftSource
    ? {
        ...draftSource,
        support_work_days: draftSource.support_work_days ?? (draftSource as PlanRequest & { technical_skill_days?: string[] }).technical_skill_days ?? [],
      }
    : null;

  // Layer order: defaults < latest_intake < onboarding_draft (non-empty wins).
  // A partially-completed draft still pulls untouched fields from the last completed intake.
  const layered = normalizedDraft
    ? (isQuickBuildDraft(normalizedDraft) ? normalizedDraft : mergeIntakeLayers(base, normalizedDraft))
    : base;

  // Layer order for athlete fields: defaults < profile-derived < latest_intake.athlete < draft.athlete.
  // Each layer only overrides when its value is non-empty, so a partial draft (e.g. record: "",
  // technical_style: []) never clobbers prior values pulled from the previous intake or the profile.
  // `age` is deliberately absent here. It is not a layer at all any more: the
  // legacy `nutrition_profile.age` is a second, hand-edited copy that can drift
  // from the date of birth, and layering it would only give it a chance to win.
  // The canonical value is applied below, after every layer has been merged.
  const profileAthlete: Partial<PlanRequest["athlete"]> = {
    full_name: me.profile.full_name,
    sex: me.profile.nutrition_profile?.sex ?? null,
    height_cm: me.profile.nutrition_profile?.height_cm ?? null,
    technical_style: me.profile.technical_style,
    tactical_style: me.profile.tactical_style,
    stance: me.profile.stance,
    professional_status: me.profile.professional_status,
    record: me.profile.record,
    athlete_timezone: me.profile.athlete_timezone,
  };

  const withProfile = mergeAthleteLayers(fallback.athlete, profileAthlete);
  const withIntake = mergeAthleteLayers(withProfile, base.athlete);
  const mergedAthlete = mergeAthleteLayers(withIntake, normalizedDraft?.athlete, "explicit");

  // Age is forced *after* every layer, not merged as one of them. Applying it
  // earlier would let `latest_intake.athlete.age`, `onboarding_draft.athlete.age`
  // or the legacy `nutrition_profile.age` overwrite it again — which is exactly
  // how a profile whose date of birth says 15 ended up displaying 25 in Camp
  // Setup. The date of birth is the only source; no date means no age, never a
  // stale one, and the server re-derives it before generation either way.
  const canonicalAge = deriveAgeFromDateOfBirth(me.profile.date_of_birth);

  const finalAthlete: PlanRequest["athlete"] = {
    ...mergedAthlete,
    age: canonicalAge,
    // A profile that is currently a minor has no weight-cut feature, so a target
    // weight left behind by an older adult draft or intake has nothing to serve.
    // The backend strips it from the request too; clearing it here stops it
    // reappearing in a form the athlete can no longer see the field for.
    ...(me.profile.is_minor ? { target_weight_kg: null } : {}),
    // Like age, the saved profile timezone is forced after every layer so an
    // old intake or draft cannot carry a stale zone back into Camp Setup.
    ...(me.profile.athlete_timezone?.trim() ? { athlete_timezone: me.profile.athlete_timezone.trim() } : {}),
  };

  return canonicalizePerformanceFocus({
    ...fallback,
    ...layered,
    athlete: finalAthlete,
  });
}

export function mergeSavedOnboardingDraft(
  me: MeResponse | null,
  draft: Record<string, unknown>,
  athlete: PlanRequest["athlete"],
): MeResponse | null {
  if (!me) {
    return null;
  }

  return {
    ...me,
    profile: {
      ...me.profile,
      full_name: athlete.full_name ?? me.profile.full_name,
      technical_style: athlete.technical_style ?? me.profile.technical_style,
      tactical_style: athlete.tactical_style ?? me.profile.tactical_style,
      stance: athlete.stance ?? me.profile.stance,
      professional_status: athlete.professional_status ?? me.profile.professional_status,
      record: athlete.record ?? me.profile.record,
      athlete_timezone: me.profile.athlete_timezone?.trim() || athlete.athlete_timezone || me.profile.athlete_timezone,
      onboarding_draft: draft,
    },
  };
}

export function mergePlanRequestDraft(
  existingDraft: Record<string, unknown> | null | undefined,
  nextPlanRequest: PlanRequest,
  currentStep: number,
): Record<string, unknown> {
  return {
    ...(existingDraft ?? {}),
    ...nextPlanRequest,
    current_step: currentStep,
  };
}

export function csvToList(value: string): string[] {
  return value
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
}

export function listToCsv(values: string[] | undefined): string {
  return (values ?? []).join(", ");
}
