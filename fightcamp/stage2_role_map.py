"""Week-by-week progression, session role slots, sparring lock, day-hint
assignment, and high-fatigue compression — the second layer of the Stage 2
planning brief.

All public functions here are re-exported from stage2_payload for
backward compatibility.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, NamedTuple

from .normalization import clean_list, normalize_fatigue_level, ordered_weekdays as _ordered_weekdays
from .calendar_context import (
    classify_role,
    normal_week_legality,
    weekday_position,
    week_scope,
)
from .combat_load_policy import (
    CalendarEvent,
    PlacementDirective,
    contact_load_profile,
    evaluate_candidate_at_position,
    placement_rank,
    role_load_profile,
)
from .declared_combat_ownership import (
    LIGHT_COMBAT_ROLE_KEY,
    build_declared_light_combat_role,
    declared_light_combat_weekdays,
)
from .sparring_dose_planner import (
    MAX_REPEATED_WEEKDAY_SPAN,
    compute_hard_sparring_plan,
    repeated_weekday_hard_sparring_entries,
    effective_hard_day_count,
    effective_hard_days,
    sandwiched_training_days,
)
from .stage2_payload_late_fight import (
    _conditioning_limiter_signal,
    _role_anchor,
    compute_bridge_rules,
)
from .stage2_planning_brief import (
    dedupe_preserve_order,
    _is_high_pressure_weight_cut,
    _WEEKLY_STAGE_TEMPLATES,
    PLANNING_DECISION_HIERARCHY,
)
from .gap_fill_inserts import LOW_COST_AEROBIC_INSERTS
from .goal_priority import goal_priority_scores
from .priority_profile import build_priority_profile
from .physical_session_frequency import cadence_windows, requested_training_frequency, dated_appointments
from .weight_cut import (
    compute_cut_severity_score,
    cut_severity_bucket,
    cut_training_compression_points,
)
from .fight_day_override import apply_fight_day_override_to_weekly_role_map, compute_fight_weekday
from .fight_date_utils import build_calendar_days
from .stage2_render_guards import _all_active_injuries_surface_only
from .role_labels import PRIMARY_STRENGTH_ROLE_KEYS, stamp_weekly_role_map_labels
from .allocator_priority import allocation_sort_key


def _rotate_weekdays_from_plan_start(weekdays: list[str], plan_creation_weekday: Any) -> list[str]:
    ordered = _ordered_weekdays(clean_list(weekdays))
    creation_day = str(plan_creation_weekday or "").strip().lower()
    creation_index = _WEEKDAY_ORDER.get(creation_day)
    if creation_index is None or not ordered:
        return ordered
    start_index = (creation_index + 1) % 7

    def relative_position(day: str) -> int:
        day_index = _WEEKDAY_ORDER.get(str(day).strip().lower(), 99)
        if day_index == 99:
            return 99
        return (day_index - start_index) % 7

    return sorted(ordered, key=lambda day: (relative_position(day), str(day).strip().lower()))

def _phase_progression_slot_count(brief: dict) -> int:
    weeks = int(brief.get("weeks") or 0)
    days = int(brief.get("days") or 0)
    if weeks > 0:
        return weeks
    return 1 if days > 0 else 0


def _split_phase_days(days: int, slot_count: int) -> list[int]:
    if slot_count <= 0:
        return []
    if days <= 0:
        return [0] * slot_count
    base, remainder = divmod(days, slot_count)
    return [base + (1 if idx < remainder else 0) for idx in range(slot_count)]


def _progression_templates_for_phase(phase: str, slot_count: int, athlete_model: dict, phase_days: int) -> list[dict]:
    templates = _WEEKLY_STAGE_TEMPLATES[phase]
    readiness_flags = set(clean_list(athlete_model.get("readiness_flags", [])))
    short_notice = bool(athlete_model.get("short_notice"))
    fight_week_like = short_notice or phase_days <= 7 or "fight_week" in readiness_flags

    if phase == "GPP":
        if slot_count <= 1:
            return [templates["single"]]
        if slot_count == 2:
            return [templates["early"], templates["middle"]]
        return [templates["early"]] + [templates["middle"]] * (slot_count - 2) + [templates["late"]]

    if phase == "SPP":
        if slot_count <= 1:
            return [templates["single"]]
        if slot_count == 2:
            return [templates["middle"], templates["late"]]
        return [templates["early"]] + [templates["middle"]] * (slot_count - 2) + [templates["late"]]

    if slot_count <= 1:
        return [templates["late"] if fight_week_like else templates["single"]]
    return [templates["early"]] + [templates["late"]] * (slot_count - 1)


def _build_week_by_week_progression(
    athlete_model: dict,
    phase_briefs: dict[str, dict],
    weekly_stress_map: dict[str, dict],
) -> dict:
    week_entries: list[dict] = []
    week_index = 1

    for phase in ("GPP", "SPP", "TAPER"):
        brief = phase_briefs.get(phase)
        if not brief:
            continue
        slot_count = _phase_progression_slot_count(brief)
        if slot_count <= 0:
            continue

        phase_days = int(brief.get("days") or 0)
        stage_templates = _progression_templates_for_phase(phase, slot_count, athlete_model, phase_days)
        day_spans = _split_phase_days(phase_days, slot_count)
        stress = weekly_stress_map.get(phase, {})
        guardrails = brief.get("selection_guardrails") or {}

        for phase_week_index, stage in enumerate(stage_templates, start=1):
            week_entries.append(
                {
                    "week_index": week_index,
                    "phase": phase,
                    "phase_week_index": phase_week_index,
                    "phase_week_total": slot_count,
                    "span_days": day_spans[phase_week_index - 1] if phase_week_index - 1 < len(day_spans) else 0,
                    "stage_key": stage["key"],
                    "stage_label": stage["label"],
                    "stage_objective": stage["objective"],
                    "load_bias": stage["load_bias"],
                    "session_counts": dict(brief.get("session_counts") or {}),
                    "build": dedupe_preserve_order(clean_list(brief.get("emphasize", [])) + list(stage.get("emphasize", []))),
                    "protect": dedupe_preserve_order(clean_list(brief.get("risk_flags", [])) + list(stage.get("protect", []))),
                    "deprioritize": dedupe_preserve_order(clean_list(brief.get("deprioritize", [])) + list(stage.get("deprioritize", []))),
                    "must_keep": clean_list(guardrails.get("must_keep_if_present", [])),
                    "drop_order_if_thin": clean_list(guardrails.get("conditioning_drop_order_if_thin", [])),
                    "conditioning_sequence": list(stress.get("conditioning_sequence", [])),
                    "highest_neural_day": stress.get("highest_neural_day", ""),
                    "highest_glycolytic_day": stress.get("highest_glycolytic_day", ""),
                    "lowest_load_day": stress.get("lowest_load_day", ""),
                    "protect_first": stress.get("protect_first", ""),
                    "cut_first_when_collisions_rise": stress.get("cut_first_when_collisions_rise", ""),
                    "sport_load_interaction": stress.get("sport_load_interaction", ""),
                    "highest_collision_sport_load": stress.get("highest_collision_sport_load", ""),
                    "resolved_rule_state": dict(stress.get("resolved_rule_state", {})),
                }
            )
            week_index += 1

    return {
        "model": "adaptive_phase_overlay.v1",
        "source_of_truth": [
            "Phase order and duration come from the existing deterministic phase allocation.",
            "Progression jobs compress or expand to fit the active phase duration without rewriting phase boundaries.",
            "Days refine span reporting so short active phases still get one compressed week entry when needed.",
        ],
        "active_week_count": len(week_entries),
        "weeks": week_entries,
    }




def _placement_rule_for_anchor(anchor: str, week_entry: dict) -> str:
    if anchor == "highest_neural_day":
        return week_entry.get("highest_neural_day", "Use this as the week's highest neural slot.")
    if anchor == "highest_glycolytic_day":
        return week_entry.get("highest_glycolytic_day", "Use this as the week's main density slot.")
    if anchor == "lowest_load_day":
        return week_entry.get("lowest_load_day", "Keep this as the lowest-load day of the week.")
    return "Place this away from the highest collision sport load when possible."


def _strength_role_key(phase: str, stage_key: str, limiter_key: str, idx: int) -> str:
    if phase == "GPP":
        if idx == 0:
            return "structural_strength_day" if limiter_key == "tissue_state" else "primary_strength_day"
        return "secondary_strength_day"
    if phase == "SPP":
        if idx == 0:
            return "neural_plus_strength_day"
        if stage_key in {"peak_specificity", "specific_density_to_peak"}:
            return "strength_touch_day"
        return "transfer_strength_day"
    if idx == 0:
        return "neural_primer_day"
    return "small_strength_touch_day"


def _conditioning_role_key(phase: str, system: str, limiter_key: str) -> str:
    if system == "aerobic":
        if phase == "GPP":
            return "aerobic_coordination_day" if limiter_key == "coordination" else "aerobic_base_day"
        if phase == "SPP":
            return "repeatability_support_day" if limiter_key == "aerobic_repeatability" else "aerobic_support_day"
        return "aerobic_flush_day"
    if system == "glycolytic":
        if phase == "TAPER":
            return "light_fight_pace_touch_day"
        if phase == "SPP":
            return "fight_pace_repeatability_day"
        return "controlled_repeatability_day"
    if phase == "TAPER":
        return "alactic_sharpness_day"
    if phase == "SPP":
        return "alactic_speed_day"
    return "alactic_coordination_day" if limiter_key == "coordination" else "alactic_support_day"


def _recovery_role_key(phase: str, stage_key: str, athlete_model: dict) -> str:
    readiness_flags = set(clean_list(athlete_model.get("readiness_flags", [])))
    if phase == "TAPER" or stage_key == "fight_week_survival_rhythm" or "fight_week" in readiness_flags:
        return "fight_week_freshness_day"
    if athlete_model.get("injuries"):
        return "tissue_recovery_day"
    return "recovery_reset_day"


def _role_selection_rule(role_key: str, category: str, system: str | None = None) -> str:
    if category == "strength":
        if role_key in {"primary_strength_day", "structural_strength_day", "neural_plus_strength_day", "neural_primer_day"}:
            return "Use the highest-priority compliant strength slot first."
        return "Use a remaining compliant strength slot with lower interference cost than the main strength day."
    if category == "conditioning":
        if system == "aerobic":
            return "Prefer compliant aerobic or low-damage conditioning slots first."
        if system == "glycolytic":
            return "Prefer compliant glycolytic slots only when phase guardrails still allow density work."
        return "Prefer compliant alactic slots that preserve speed and sharpness."
    return "Use rehab slots first; if rehab is absent, keep this day recovery-only."


def _has_gas_tank_signal(athlete_model: dict) -> bool:
    """Return True when gas tank/conditioning is an explicit goal or weakness."""
    raw_values: list[Any] = []

    for key in (
        "key_goals",
        "goals",
        "performance_goals",
        "weaknesses",
        "weak_areas",
    ):
        raw_values.extend(clean_list(athlete_model.get(key, [])))

    joined = " ".join(str(value).lower().replace("-", "_") for value in raw_values)

    gas_tank_terms = (
        "gas_tank",
        "conditioning",
        "conditioning_endurance",
        "endurance",
        "work_capacity",
        "aerobic",
        "repeatability",
        "late_fight",
        "late_round",
    )

    return any(term in joined for term in gas_tank_terms)


def _calendar_d_day_for_role(week_entry: dict, role: dict) -> int | None:
    """Resolve the D-day countdown for a role's scheduled weekday."""
    scheduled_day = str(role.get("scheduled_day_hint") or "").strip().lower()
    if not scheduled_day:
        return None

    for day in week_entry.get("calendar_days") or []:
        if str(day.get("weekday") or "").strip().lower() == scheduled_day:
            try:
                return int(day.get("d_day"))
            except (TypeError, ValueError):
                return None

    return None


def _low_load_support_profile_for_unused_day(athlete_model: dict) -> dict[str, Any] | None:
    """
    Decide whether an unused recovery/off day should become a low-load support day.

    Priority:
    1. Gas tank / conditioning
    2. Mobility
    3. Recovery / freshness / cut-stress support
    4. Injury prevention / rehab-friendly support

    Only gas tank gets preferred_exercise_names because we specifically want to bias
    Assault Bike / Rower / Nasal Shadowboxing / Nasal Walk.
    """
    raw_values: list[Any] = []

    for key in (
        "key_goals",
        "goals",
        "weaknesses",
        "weak_areas",
        "performance_goals",
        "main_limiter",
        "limiter_key",
    ):
        raw_values.extend(clean_list(athlete_model.get(key, [])))

    tokens = {
        str(value).strip().lower().replace("-", "_").replace(" ", "_")
        for value in raw_values
        if str(value).strip()
    }

    gas_tank_terms = {
        "gas_tank",
        "conditioning",
        "conditioning_endurance",
        "endurance",
        "work_capacity",
        "aerobic",
        "repeatability",
        "late_fight",
        "late_round",
    }

    mobility_terms = {
        "mobility",
        "hip_mobility",
        "shoulder_mobility",
        "range_of_motion",
        "flexibility",
        "movement_quality",
    }

    injury_terms = {
        "injury_prevention",
        "rehab",
        "rehab_friendly",
        "prehab",
        "tissue_quality",
        "ankle",
        "knee",
        "shoulder",
        "hip",
        "back",
    }
    recovery_terms = {
        "recovery",
        "freshness",
        "fatigue_management",
        "active_recovery",
        "restore",
        "regeneration",
        "cut_stress",
        "weight_cut",
    }

    gas_tank_profile_tokens = {
        str(value).strip().lower().replace("-", "_").replace(" ", "_")
        for key in ("key_goals", "goals", "performance_goals", "weaknesses", "weak_areas")
        for value in clean_list(athlete_model.get(key, []))
        if str(value).strip()
    }

    if gas_tank_profile_tokens & gas_tank_terms or _has_gas_tank_signal(athlete_model):
        return {
            "role_key": "converted_low_aerobic_gas_tank_day",
            "athlete_facing_label": "Low aerobic gas-tank support",
            "preferred_system": "aerobic",
            "preferred_tags": ["gas_tank", "aerobic", "low_impact", "low_cns", "recovery"],
            "preferred_exercise_names": [
                "Assault Bike Easy Gas Tank Ride",
                "Rower Nasal Aerobic Base",
                "Nasal Shadowboxing Flow (Gas Tank)",
                "Nasal Walk with Boxing Posture",
            ],
            "reason": (
                "Gas tank/conditioning is a profile goal or weakness, so this unused "
                "day becomes a low-aerobic support touch."
            ),
        }

    if tokens & mobility_terms:
        return {
            "role_key": "converted_mobility_support_day",
            "athlete_facing_label": "Low-load mobility support",
            "preferred_system": "aerobic",
            "preferred_tags": ["mobility", "recovery", "low_impact", "low_cns", "cns_freshness"],
            "reason": (
                "Mobility is a profile goal or weakness, so this unused day becomes "
                "a low-load mobility support touch."
            ),
        }

    if (tokens & recovery_terms) or athlete_model.get("weight_cut_risk"):
        return {
            "role_key": "converted_recovery_flush_day",
            "athlete_facing_label": "Low-load recovery flush",
            "preferred_system": "aerobic",
            "preferred_tags": [
                "recovery",
                "freshness",
                "mobility",
                "low_impact",
                "low_cns",
                "low_lactate",
            ],
            "preferred_exercise_names": [
                "Assault Bike Easy Recovery Flush",
                "Mobility Reset Flow",
                "Breathing Reset",
                "Nasal Shadowboxing Flow",
            ],
            "reason": (
                "Recovery, fatigue management, or active cut stress is present, "
                "so this unused day becomes a low-load recovery flush."
            ),
        }

    if (tokens & injury_terms) or athlete_model.get("injuries"):
        return {
            "role_key": "converted_rehab_friendly_support_day",
            "athlete_facing_label": "Rehab-friendly low-load support",
            "preferred_system": "aerobic",
            "preferred_tags": ["rehab_friendly", "recovery", "low_impact", "low_cns", "mobility"],
            "reason": (
                "Injury prevention or restriction is present, so this unused day becomes "
                "a rehab-friendly low-load support touch."
            ),
        }

    return None


_LOW_AEROBIC_SUPPORT_ROLE_KEYS = {
    "recovery_aerobic_gas_tank_day",
    "converted_low_aerobic_gas_tank_day",
    "aerobic_support_day",
    "aerobic_base_day",
    "aerobic_coordination_day",
    "aerobic_flush_day",
}


def _is_low_aerobic_support_role(role: dict) -> bool:
    """Return True when the role qualifies as a low-aerobic support touch.

    Covers both shapes a low-aerobic touch can take: a conditioning role the
    allocator created, and a low-cost aerobic support insert the filler layer
    placed. Both spend the same weekly allowance, so both are counted here
    rather than by a second frequency authority.
    """
    if not isinstance(role, dict):
        return False
    if str(role.get("role_key") or "").strip() in LOW_COST_AEROBIC_INSERTS:
        return True
    category = str(role.get("category") or "").strip().lower()
    if category != "conditioning":
        return False
    preferred_system = str(role.get("preferred_system") or "").strip().lower()
    if preferred_system in {"glycolytic", "alactic", "atp-pcr", "atp_pcr"}:
        return False
    role_key = str(role.get("role_key") or "").strip()
    if role_key in _LOW_AEROBIC_SUPPORT_ROLE_KEYS:
        return True
    if role_key == "repeatability_support_day" and preferred_system == "aerobic":
        return True
    if preferred_system == "aerobic" and (
        role.get("recovery_compatible")
        or role.get("allowed_on_recovery_day")
        or role.get("gas_tank_recovery_touch")
        or role.get("converted_from_unused_day")
    ):
        return True
    return False


def _count_low_aerobic_support_roles(roles: list[dict]) -> int:
    # Dedicated mobility/rehab support roles are explicitly excluded from the
    # conditioning cap budget — they are protective recovery touches, not
    # production conditioning. Gas-tank low-aerobic support still counts.
    return sum(
        1
        for role in roles
        if _is_low_aerobic_support_role(role)
        and role.get("counts_toward_conditioning_cap") is not False
    )


def _low_aerobic_support_cap_for_week(
    week_entry: dict,
    athlete_model: dict,
    session_roles: list[dict],
    hard_sparring_plan: list[dict] | None = None,
) -> int:
    """Severity-aware cap on low-aerobic support touches for a week.

    Reads cut severity from the deterministic source of truth in
    weight_cut.py (via _resolved_cut_severity_bucket). The cap reflects
    phase, fight-week proximity, hard-sparring load, fatigue, and
    readiness flags. Easy bike, easy row, nasal shadowboxing, short
    Z1/Z2 flushes, and mobility/breathing are not auto-suppressed by a
    cut, but their frequency is reduced when the cut is severe.
    """
    phase = str(week_entry.get("phase") or "").strip().upper()

    min_d_day: int | None = None
    for day in week_entry.get("calendar_days") or []:
        try:
            d = int(day.get("d_day"))
        except (TypeError, ValueError):
            continue
        if min_d_day is None or d < min_d_day:
            min_d_day = d

    readiness_flags = {
        str(flag).strip().lower()
        for flag in clean_list(athlete_model.get("readiness_flags", []))
    }
    is_fight_week = (
        (min_d_day is not None and min_d_day <= 7)
        or "fight_week" in readiness_flags
        or "fight_day_protocol" in readiness_flags
    )

    bucket = _resolved_cut_severity_bucket(athlete_model) or "none"

    fatigue = str(athlete_model.get("fatigue") or "").strip().lower()
    high_fatigue = fatigue == "high"
    red_flag = bool(
        readiness_flags
        & {"severe_injury", "red_flag_injury", "medical_hold"}
    )

    if bucket in {"none", "low"}:
        if is_fight_week:
            return 0 if (high_fatigue or red_flag) else 1
        if phase == "TAPER":
            return 1
        # Frequent low-damage aerobic exposure is the point of the build phases
        # when the athlete has actually stated a conditioning/gas-tank priority.
        # A second easy touch needs no fatigue or red-flag blocker and a week
        # that is not already carrying heavy hard-contact load.
        hard_contact_days = len(
            {
                str(day).strip().lower()
                for day in (week_entry.get("effective_hard_sparring_days") or [])
                if str(day).strip()
            }
            or {
                str(entry.get("day") or "").strip().lower()
                for entry in (hard_sparring_plan or [])
                if isinstance(entry, dict)
                and entry.get("status") == "hard_as_planned"
                and str(entry.get("day") or "").strip()
            }
        )
        if (
            phase in {"GPP", "SPP"}
            and not high_fatigue
            and not red_flag
            and hard_contact_days <= 2
            and goal_priority_scores(athlete_model).get("conditioning", 0) >= 10
        ):
            return 2
        return 1

    if bucket == "moderate":
        if is_fight_week:
            return 0 if (high_fatigue or red_flag) else 1
        if phase == "TAPER":
            return 1
        # GPP / SPP: keep gas-tank support to one easy touch.
        return 1

    # high / critical / extreme: never reopen volume on high fatigue or red flag.
    if high_fatigue or red_flag:
        return 0
    return 1


def _can_preserve_one_non_gas_low_load_support(
    week_entry: dict,
    athlete_model: dict,
    support_profile: dict[str, Any],
    cap: int,
) -> bool:
    """Allow one non-gas low-load support slot only when no hard safety blockers exist."""
    if support_profile.get("role_key") == "converted_low_aerobic_gas_tank_day":
        return False

    fatigue = str(athlete_model.get("fatigue") or "").strip().lower()
    if fatigue == "high":
        return False

    injury_mode = str(athlete_model.get("injury_mode") or "").strip().lower()
    if injury_mode in {"medical_hold", "restricted_rehab_only"}:
        return False

    readiness_flags = {
        str(flag).strip().lower()
        for flag in clean_list(athlete_model.get("readiness_flags", []))
    }
    if readiness_flags & {
        "red_flag_injury",
        "severe_injury",
        "medical_hold",
        "restricted_rehab_only",
    }:
        return False

    min_d_day: int | None = None
    for day in week_entry.get("calendar_days") or []:
        try:
            d = int(day.get("d_day"))
        except (TypeError, ValueError):
            continue
        if min_d_day is None or d < min_d_day:
            min_d_day = d

    is_fight_week = (
        (min_d_day is not None and 0 <= min_d_day <= 7)
        or "fight_week" in readiness_flags
        or "fight_day_protocol" in readiness_flags
    )
    if is_fight_week and cap == 0:
        return False

    return True


def _upgrade_recovery_days_to_gas_tank(
    week_entry: dict,
    session_roles: list[dict],
    athlete_model: dict,
    hard_sparring_plan: list[dict] | None = None,
) -> list[dict]:
    """
    If gas tank is a goal/weakness, turn eligible recovery roles into
    low-aerobic gas-tank conditioning roles.

    This only applies to gas tank. Other goals/weaknesses are handled by
    _upgrade_unused_days_to_gas_tank().
    """
    if not _has_gas_tank_signal(athlete_model):
        return session_roles

    cap = _low_aerobic_support_cap_for_week(
        week_entry,
        athlete_model,
        session_roles,
        hard_sparring_plan=hard_sparring_plan,
    )
    current_count = _count_low_aerobic_support_roles(session_roles)

    updated: list[dict] = []

    for role in session_roles:
        if role.get("category") != "recovery":
            updated.append(role)
            continue

        scheduled_day = str(role.get("scheduled_day_hint") or "").strip()
        if not scheduled_day:
            updated.append(role)
            continue

        d_day = _calendar_d_day_for_role(week_entry, role)

        # No extra app work on fight day or day before fight.
        if d_day is not None and d_day <= 1:
            updated.append(role)
            continue

        if current_count >= cap:
            updated.append(role)
            continue

        converted = dict(role)
        converted.update(
            {
                "category": "conditioning",
                "role_key": "recovery_aerobic_gas_tank_day",
                "original_role_key": role.get("role_key", ""),
                "original_category": "recovery",
                "athlete_facing_label": "Low aerobic gas-tank flush",
                "preferred_system": "aerobic",
                "preferred_pool": "conditioning_slots",
                "preferred_tags": ["gas_tank", "aerobic", "low_impact", "low_cns", "recovery"],
                "preferred_exercise_names": [
                    "Assault Bike Easy Gas Tank Ride",
                    "Rower Nasal Aerobic Base",
                    "Nasal Shadowboxing Flow (Gas Tank)",
                    "Nasal Walk with Boxing Posture",
                ],
                "selection_rule": (
                    "Use only low-aerobic gas-tank work here: RPE <= 4, "
                    "low impact, low lactate, low CNS. This may sit on a recovery "
                    "day or adjacent to hard sparring because it is a flush/base touch, "
                    "not a hard conditioning session."
                ),
                "anchor": "lowest_load_day",
                "recovery_compatible": True,
                "gas_tank_recovery_touch": True,
                "allowed_on_recovery_day": True,
                "support_kind": "gas_tank",
                "counts_toward_conditioning_cap": True,
                "counts_toward_exercise_cap": False,
                "counts_toward_strength_cap": False,
                "blocked_systems": ["glycolytic", "ATP-PCr"],
                "blocked_intensities": ["high", "max"],
                "blocked_tags": [
                    "mech_cns_high",
                    "high_cns",
                    "sprint",
                    "plyometric",
                    "high_impact_lower",
                    "mech_landing_impact",
                ],
            }
        )

        placement = str(converted.get("placement_rule") or "").strip()
        addendum = (
            "Because gas tank is a profile limiter, this recovery slot may become "
            "a low-aerobic gas-tank session. Do not select hard conditioning."
        )
        converted["placement_rule"] = f"{placement} {addendum}".strip()

        updated.append(converted)
        current_count += 1

    for idx, role in enumerate(updated, start=1):
        role["session_index"] = idx

    return updated


def _upgrade_unused_days_to_low_load_support(
    week_entry: dict,
    session_roles: list[dict],
    athlete_model: dict,
    hard_sparring_plan: list[dict] | None = None,
) -> list[dict]:
    """
    Turn eligible intentionally unused recovery/off days into low-load support work
    when the athlete profile has a matching goal, weakness, limiter, or restriction.

    Gas tank gets preferred exercise names.
    Other qualities rely on tags and normal selector scoring.
    """
    support_profile = _low_load_support_profile_for_unused_day(athlete_model)
    if not support_profile:
        return session_roles

    # Dedicated mobility/rehab support is a protective recovery touch and is
    # explicitly excluded from the conditioning cap budget. Gas-tank low-aerobic
    # support still consumes the cap.
    support_counts_toward_conditioning_cap = (
        support_profile["role_key"] == "converted_low_aerobic_gas_tank_day"
    )

    updated_unused_days: list[dict] = []
    added_roles: list[dict] = []

    phase = str(week_entry.get("phase", "")).strip().upper()
    legacy_phase_ceiling = 2 if phase in {"GPP", "SPP"} else 1
    base_cap = _low_aerobic_support_cap_for_week(
        week_entry,
        athlete_model,
        session_roles,
        hard_sparring_plan=hard_sparring_plan,
    )
    # For non-gas mobility/rehab profiles, _can_preserve_one_non_gas_low_load_support
    # is the hard safety gate (high fatigue, medical_hold, restricted_rehab_only,
    # red_flag_injury, severe_injury, fight-week cap=0). It is computed against
    # the unboosted base cap so the fight-week cap=0 check stays accurate.
    can_preserve_non_gas = _can_preserve_one_non_gas_low_load_support(
        week_entry,
        athlete_model,
        support_profile,
        base_cap,
    )
    cap = max(base_cap, 1) if can_preserve_non_gas else base_cap
    current_count = _count_low_aerobic_support_roles(session_roles)

    existing_days = {
        str(role.get("scheduled_day_hint") or "").strip().lower()
        for role in session_roles
        if str(role.get("scheduled_day_hint") or "").strip()
    }
    support_work_days = {
        str(day).strip().lower()
        for day in clean_list(athlete_model.get("support_work_days") or athlete_model.get("technical_skill_days") or [])
        if str(day).strip()
    }

    unused_day_entries = list(week_entry.get("intentionally_unused_days") or [])
    following_day = ""
    if (
        support_profile["role_key"] == "converted_low_aerobic_gas_tank_day"
    ):
        creation_day = str(athlete_model.get("plan_creation_weekday") or "").strip().lower()
        creation_index = _WEEKDAY_ORDER.get(creation_day)
        if creation_index is None:
            return session_roles
        following_index = (creation_index + 1) % 7
        following_day = next(
            (day for day, index in _WEEKDAY_ORDER.items() if index == following_index),
            "",
        )

    for day_entry in unused_day_entries:
        day = str(day_entry.get("day") or "").strip()
        role = str(day_entry.get("role") or "").strip()

        if not day:
            updated_unused_days.append(day_entry)
            continue

        # Gas-tank support is a single next-day post-generation upgrade, never
        # a general search for another free day later in the week.
        if following_day and day.lower() != following_day:
            updated_unused_days.append(day_entry)
            continue

        # Do not double-book a day that already has a session role.
        if day.lower() in existing_days:
            updated_unused_days.append(day_entry)
            continue

        d_day = None
        for calendar_day in week_entry.get("calendar_days") or []:
            if str(calendar_day.get("weekday") or "").strip().lower() == day.lower():
                try:
                    d_day = int(calendar_day.get("d_day"))
                except (TypeError, ValueError):
                    d_day = None
                break

        # Do not add app support work on D-1 or D-0.
        if d_day is not None and d_day <= 1:
            updated_unused_days.append(day_entry)
            continue

        if role not in {"recovery_only_day", "off_day"}:
            updated_unused_days.append(day_entry)
            continue

        if len(added_roles) >= legacy_phase_ceiling:
            updated_unused_days.append(day_entry)
            continue

        if support_counts_toward_conditioning_cap:
            if current_count >= cap:
                annotated = dict(day_entry)
                annotated["low_aerobic_cap_skipped"] = True
                annotated["low_aerobic_cap_reason"] = (
                    f"Low-aerobic support cap reached ({cap}); cut severity, "
                    f"phase, fatigue, or readiness blocked the upgrade for {day}."
                )
                updated_unused_days.append(annotated)
                continue
        else:
            # Non-gas mobility/rehab support does not consume the conditioning
            # cap, but is still blocked when the safety gate trips (high fatigue,
            # medical_hold, restricted_rehab_only, red_flag_injury, severe_injury,
            # fight-week cap=0).
            if not can_preserve_non_gas:
                annotated = dict(day_entry)
                annotated["low_aerobic_cap_skipped"] = True
                annotated["low_aerobic_cap_reason"] = (
                    "Mobility/rehab support blocked by safety gate "
                    "(fatigue, medical hold, red-flag, or fight-week cap=0)."
                )
                updated_unused_days.append(annotated)
                continue

        role_key = support_profile["role_key"]
        if role_key == "converted_low_aerobic_gas_tank_day" and day.lower() in support_work_days:
            role_key = "recovery_aerobic_gas_tank_day"
        preferred_system = support_profile["preferred_system"]
        preferred_tags = list(support_profile["preferred_tags"])
        preferred_exercise_names = list(support_profile.get("preferred_exercise_names") or [])

        # This day is no longer intentionally unused once we convert it into
        # a concrete low-load support session role. Keep the converted metadata
        # on the added role, and remove the day from intentionally_unused_days
        # to avoid downstream recovery/off flattening.

        added_role = {
            "session_index": 0,
            "category": "conditioning",
            "role_key": role_key,
            "athlete_facing_label": support_profile.get("athlete_facing_label", "Low-load support"),
            "preferred_pool": "conditioning_slots",
            "preferred_system": preferred_system,
            "preferred_tags": preferred_tags,
            "selection_rule": (
                "Use only low-load support work here: RPE <= 4, low impact, "
                "low lactate, low CNS. Do not turn this into hard conditioning."
            ),
            "anchor": "lowest_load_day",
            "placement_rule": (
                "This was an unused recovery/off training day upgraded into low-load support "
                "work because the athlete profile has a matching goal, weakness, "
                "limiter, or restriction. Allowed adjacent to hard sparring only if "
                "it stays low intensity."
            ),
            "governance": {
                "authority": "unused_day_low_load_support_upgrade",
                "execution_only": True,
                "suppression_rules": [
                    "Must remain RPE <= 4.",
                    "Must be low impact, low lactate, and low CNS.",
                    "Must not become glycolytic, sprint, plyometric, or max-intensity work.",
                ],
                "hard_suppression_reasons": [],
            },
            "scheduled_day_hint": day,
            "day_assignment_reason": (
                "Unused recovery/off training day upgraded to low-load support work."
            ),
            "recovery_compatible": True,
            "gas_tank_recovery_touch": role_key == "converted_low_aerobic_gas_tank_day",
            "priority_recovery_touch": role_key != "converted_low_aerobic_gas_tank_day",
            "allowed_on_recovery_day": True,
            "counts_toward_exercise_cap": False,
            "counts_toward_strength_cap": False,
            "blocked_systems": ["glycolytic", "ATP-PCr"],
            "blocked_intensities": ["high", "max"],
            "blocked_tags": [
                "mech_cns_high",
                "high_cns",
                "sprint",
                "plyometric",
                "high_impact_lower",
                "mech_landing_impact",
            ],
        }

        if role_key == "converted_low_aerobic_gas_tank_day":
            added_role["support_kind"] = "gas_tank"
            added_role["counts_toward_conditioning_cap"] = True
        else:
            added_role["counts_toward_conditioning_cap"] = False
            added_role["is_dedicated_recovery_mobility_day"] = True
            if role_key == "converted_mobility_support_day":
                added_role["support_kind"] = "mobility"
            elif role_key == "converted_recovery_flush_day":
                added_role["support_kind"] = "recovery"
            else:
                added_role["support_kind"] = "rehab_friendly"

        if preferred_exercise_names:
            added_role["preferred_exercise_names"] = preferred_exercise_names

        added_roles.append(added_role)
        if (
            _is_low_aerobic_support_role(added_role)
            and added_role.get("counts_toward_conditioning_cap") is not False
        ):
            current_count += 1

    week_entry["intentionally_unused_days"] = updated_unused_days

    result = list(session_roles) + added_roles
    for idx, role in enumerate(result, start=1):
        role["session_index"] = idx

    return result
    
    
def _role_governance(
    week_entry: dict,
    *,
    category: str,
    role_key: str,
    athlete_model: dict,
    system: str | None = None,
    idx: int = 0,
) -> dict:
    phase = str(week_entry.get("phase", "")).upper()
    resolved_rule_state = dict(week_entry.get("resolved_rule_state", {}))
    must_keep = set(clean_list(resolved_rule_state.get("must_keep", week_entry.get("must_keep", []))))
    drop_order = clean_list(resolved_rule_state.get("drop_order_if_thin", week_entry.get("drop_order_if_thin", [])))
    cut_first_text = str(
        resolved_rule_state.get("cut_first_when_collisions_rise", week_entry.get("cut_first_when_collisions_rise", ""))
    ).lower()
    highest_collision_load = str(week_entry.get("highest_collision_sport_load", "")).strip()
    tissue_protection_priority = bool(resolved_rule_state.get("tissue_protection_priority"))
    freshness_priority = bool(resolved_rule_state.get("freshness_priority"))
    sport_load_owns_density = bool(resolved_rule_state.get("sport_load_owns_density"))

    hard_suppression: list[str] = []
    suppression_rules: list[str] = []

    if category == "strength" and phase == "TAPER" and idx > 0:
        hard_suppression.append(
            "Taper survival rules suppress extra strength touches once the primary primer already exists."
        )
    if category == "strength" and role_key == "neural_primer_day" and tissue_protection_priority:
        hard_suppression.append(
            "Safety and readiness prioritize tissue protection, so sharpness-dominant neural primer work is suppressed."
        )

    if category == "conditioning" and system:
        if system in drop_order and system not in must_keep:
            suppression_rules.append(
                f"{system.replace('_', ' ')} work is optional in this week and must drop before must-keep systems if the plan gets thin."
            )
        if role_key == "alactic_sharpness_day" and tissue_protection_priority:
            hard_suppression.append(
                "Safety and readiness prioritize tissue protection, so sharpness-dominant alactic work is suppressed."
            )
        if system == "glycolytic" and system not in must_keep and (
            (phase == "TAPER" and sport_load_owns_density and highest_collision_load) or "glycolytic density" in cut_first_text
        ):
            hard_suppression.append(
                "Taper survival and sport-load rules keep glycolytic density optional once live load already owns density."
            )
        if system == "aerobic" and phase == "TAPER" and system not in must_keep and freshness_priority:
            suppression_rules.append(
                "Optional aerobic work cannot outrank fight-week freshness protection."
            )

    if category == "recovery":
        suppression_rules.append(
            "Recovery roles may replace work, but cannot create extra workload or displace rehab."
        )

    return {
        "authority": "execution_layer_only",
        "execution_only": True,
        "governed_by": [entry["driver"] for entry in PLANNING_DECISION_HIERARCHY],
        "cannot_override": [
            "phase_survival_rules",
            "safety_and_readiness",
            "sport_load_collision_rules",
            "main_limiter",
            "session_counts",
            "must_keep",
            "drop_order_if_thin",
            "conditioning_sequence",
        ],
        "resolved_authority": {
            "protect_first_driver": resolved_rule_state.get("protect_first_driver"),
            "cut_first_driver": resolved_rule_state.get("cut_first_driver"),
            "conditioning_sequence_driver": resolved_rule_state.get("conditioning_sequence_driver"),
        },
        "suppression_rules": suppression_rules,
        "hard_suppression_reasons": hard_suppression,
    }


_WEEKDAY_ORDER = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_CROWDED_LOW_LOAD_SUPPORT_ROLE_KEYS = {
    "recovery_reset_day",
    "tissue_recovery_day",
    "aerobic_support_day",
    "aerobic_base_day",
    "aerobic_coordination_day",
    "repeatability_support_day",
    "controlled_repeatability_day",
    "technical_touch_day",
    "fight_week_freshness_day",
}
_CROWDED_OPTIONAL_ALACTIC_ROLE_KEYS = {
    "alactic_speed_day",
    "alactic_sharpness_day",
    "alactic_coordination_day",
    "alactic_support_day",
}


def _athlete_sport_key(athlete_model: dict) -> str:
    return str(athlete_model.get("sport") or "").strip().lower().replace(" ", "_")




def _declared_day_sets(athlete_model: dict) -> tuple[list[str], set[str], set[str]]:
    training_days = _ordered_weekdays(clean_list(athlete_model.get("training_days", [])))
    hard_sparring = {day for day in _ordered_weekdays(clean_list(athlete_model.get("hard_sparring_days", []))) if day in training_days}
    support_work = {
        day
        for day in _ordered_weekdays(
            clean_list(athlete_model.get("support_work_days") or athlete_model.get("technical_skill_days") or [])
        )
        if day in training_days
    }
    return training_days, hard_sparring, support_work


def _append_day_hint(role: dict, day: str | None, reason: str | None = None) -> None:
    if not day:
        role["scheduled_day_hint"] = ""
        role["day_assignment_reason"] = ""
        return
    role["scheduled_day_hint"] = day
    role["day_assignment_reason"] = reason or ""
    placement = str(role.get("placement_rule", "")).strip()
    extra = f"Prefer {day} for this role."
    if reason:
        extra = f"{extra} {reason}"
    role["placement_rule"] = f"{placement} {extra}".strip() if placement else extra


def _dedupe_clean_strings(values: list[Any]) -> list[str]:
    return dedupe_preserve_order([str(value).strip() for value in values if str(value).strip()])


def _append_week_coach_note_flag(week_entry: dict, flag: str) -> None:
    current_flags = _dedupe_clean_strings(clean_list(week_entry.get("coach_note_flags", [])))
    if flag and flag not in current_flags:
        current_flags.append(flag)
    week_entry["coach_note_flags"] = current_flags


def _hard_sparring_coach_note_flags(plan_entry: dict[str, Any] | None = None) -> list[str]:
    status = str((plan_entry or {}).get("status") or "hard_as_planned").strip() or "hard_as_planned"
    return ["deload hard sparring"] if status != "hard_as_planned" else []


def _is_final_week_capped_sparring_entry(plan_entry: dict[str, Any] | None = None) -> bool:
    if not isinstance(plan_entry, dict):
        return False
    reason_codes = {str(code).strip() for code in clean_list(plan_entry.get("reason_codes")) if str(code).strip()}
    status = str(plan_entry.get("status") or "").strip()
    return "final_week_sparring_cap" in reason_codes and status != "hard_as_planned"


def _make_final_week_sparring_cap_suppression(
    day: str,
    plan_entry: dict[str, Any] | None = None,
    replaced_role: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reason = str((plan_entry or {}).get("reason") or "").strip()
    if not reason:
        reason = (
            "Final taper week sparring cap allows only one effective hard sparring day; "
            "this declared hard day must not render as sparring."
        )
    return {
        "category": "sparring",
        "role_key": "hard_sparring_day",
        "preferred_pool": "declared_hard_sparring_days",
        "reasons": [reason],
        "governance": dict((replaced_role or {}).get("governance", {})),
        "locked_day": day,
        "scheduled_day_hint": day,
        "replacement_role_key": "no_hard_sparring_day",
        "downgraded_from_role_key": "hard_sparring_day",
        "hard_sparring_status": str((plan_entry or {}).get("status") or "deload_suggested"),
        "hard_sparring_reason_codes": clean_list((plan_entry or {}).get("reason_codes")),
        "hard_sparring_reason": reason,
        "coach_note": str((plan_entry or {}).get("coach_note") or ""),
    }


def _final_week_sparring_cap_summary(
    hard_sparring_plan: list[dict] | None,
    effective_days: list[str],
) -> dict[str, Any]:
    capped_days = [
        str(entry.get("day") or "").strip()
        for entry in (hard_sparring_plan or [])
        if _is_final_week_capped_sparring_entry(entry) and str(entry.get("day") or "").strip()
    ]
    return {
        "active": bool(capped_days),
        "max_effective_hard_sparring_days": 1 if capped_days else None,
        "effective_hard_sparring_days": list(effective_days),
        "capped_declared_hard_sparring_days": capped_days,
        "instruction": (
            "Final taper week cap overrides declared hard sparring days: render at most one "
            "effective hard sparring day and do not present capped days as sparring."
            if capped_days
            else ""
        ),
    }


def _hard_sparring_role(week_entry: dict, day: str, plan_entry: dict[str, Any] | None = None) -> dict[str, Any]:
    status = str((plan_entry or {}).get("status") or "hard_as_planned").strip() or "hard_as_planned"
    hard_sparring_class = str((plan_entry or {}).get("hard_day_class") or "").strip() or (
        "managed_hard" if status != "hard_as_planned" else "primary_hard"
    )
    reason_codes = list((plan_entry or {}).get("reason_codes") or [])
    coach_note_flags = _hard_sparring_coach_note_flags(plan_entry)
    role: dict[str, Any] = {
        "category": "sparring",
        "role_key": "hard_sparring_day",
        "preferred_pool": "declared_hard_sparring_days",
        "selection_rule": "Keep the declared hard sparring slot fixed. If readiness is compromised, deload the sparring dose instead of replacing the day role.",
        "anchor": "highest_collision_sport_load",
        "placement_rule": "Keep this declared hard sparring slot fixed on the athlete's stated day.",
        "governance": {
            "authority": "declared_schedule_lock",
            "execution_only": False,
            "governed_by": [entry["driver"] for entry in PLANNING_DECISION_HIERARCHY],
            "cannot_override": [
                "declared_hard_sparring_days",
                "weekly_role_map",
                "session_counts",
                "resequence",
                "compression",
                "repair",
            ],
            "resolved_authority": {
                "protect_first_driver": (week_entry.get("resolved_rule_state") or {}).get("protect_first_driver"),
                "cut_first_driver": (week_entry.get("resolved_rule_state") or {}).get("cut_first_driver"),
                "conditioning_sequence_driver": (week_entry.get("resolved_rule_state") or {}).get("conditioning_sequence_driver"),
            },
            "suppression_rules": ["Declared hard sparring days are immutable weekly role locks."],
            "hard_suppression_reasons": [],
            "locked_day": day,
        },
        "scheduled_day_hint": day,
        "day_assignment_reason": "Declared hard sparring day is fixed in the weekly role map.",
        "hard_sparring_status": status,
        "hard_sparring_class": hard_sparring_class,
        "hard_sparring_reason_codes": reason_codes,
        "hard_sparring_reason": str((plan_entry or {}).get("reason") or ""),
        "coach_note_flags": coach_note_flags,
    }
    if role["coach_note_flags"]:
        role["placement_rule"] += " Deload the sparring dose instead of changing the slot."
    return role


def _repeated_weekday_hard_sparring_roles(
    week_entry: dict, entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Visible roles for declared hard days that repeat inside the planner week.

    Each role is pinned to its own countdown day, so the weekday-to-date mapping
    (which resolves a weekday to its occurrence nearest the fight) cannot fold it
    onto the later occurrence.
    """
    roles: list[dict[str, Any]] = []
    for entry in entries:
        d_day = entry.get("scheduled_d_day")
        day = str(entry.get("day") or "").strip()
        if not day or not isinstance(d_day, int) or entry.get("effective_load") == "none":
            continue
        role = _hard_sparring_role(week_entry, day, entry)
        role.update(
            scheduled_d_day=d_day,
            countdown_offset=d_day,
            countdown_label=f"D-{d_day}",
            scheduled_countdown_label=f"D-{d_day}",
            repeated_weekday_occurrence=True,
            day_assignment_reason=(
                f"Declared hard sparring day; {day} repeats in this planner week, so "
                f"D-{d_day} is its own session."
            ),
        )
        roles.append(role)
    return roles


def _repeated_weekday_light_combat_roles(
    week_entry: dict, athlete_model: dict
) -> list[dict[str, Any]]:
    """Light-combat locks for declared light days that repeat inside the week.

    ``_lock_declared_light_combat_roles`` places one lock per weekday, which the
    weekday-to-date mapping resolves to the occurrence nearest the fight. In an
    eight-day planner week the earlier occurrence (the generation day, D-22 in a
    23-day camp) was left unlocked and later filled with app S&C. Each earlier
    occurrence gets its own lock, pinned to its countdown day.
    """
    training_days = [
        normalised
        for day in _ordered_weekdays(clean_list(athlete_model.get("training_days", [])))
        if (normalised := str(day or "").strip().lower())
    ]
    declared = set(
        declared_light_combat_weekdays(
            athlete_model, training_days=training_days, exclude_hard_sparring=True
        )
    )
    calendar_days = week_entry.get("calendar_days") or []
    if not declared or len(calendar_days) > MAX_REPEATED_WEEKDAY_SPAN:
        return []
    occurrences: dict[str, list[int]] = {}
    for calendar_day in calendar_days:
        weekday = str(calendar_day.get("weekday") or "").strip().lower()
        d_day = calendar_day.get("d_day")
        if weekday in declared and isinstance(d_day, int) and d_day > 0:
            occurrences.setdefault(weekday, []).append(d_day)
    roles: list[dict[str, Any]] = []
    for weekday, d_days in occurrences.items():
        # The weekday lock already owns the occurrence nearest the fight.
        for d_day in sorted(d_days, reverse=True)[:-1]:
            role = build_declared_light_combat_role(weekday)
            role.update(
                scheduled_d_day=d_day,
                countdown_offset=d_day,
                countdown_label=f"D-{d_day}",
                scheduled_countdown_label=f"D-{d_day}",
                repeated_weekday_occurrence=True,
            )
            roles.append(role)
    return roles


def _make_hard_sparring_lock_suppression(role: dict, day: str) -> dict[str, Any]:
    return {
        "category": role.get("category"),
        "role_key": role.get("role_key"),
        "preferred_system": role.get("preferred_system", ""),
        "reasons": [f"Declared hard sparring locks {day} as hard_sparring_day in the weekly role map."],
        "governance": dict(role.get("governance", {})),
        "locked_day": day,
        "replacement_role_key": "hard_sparring_day",
    }


def _make_light_combat_lock_suppression(role: dict, day: str) -> dict[str, Any]:
    return {
        "category": role.get("category"),
        "role_key": role.get("role_key"),
        "preferred_system": role.get("preferred_system", ""),
        "reasons": [
            f"Declared light combat locks {day} as a coach-owned light_combat_day; the "
            "combat-load policy forbids stacking this session and no legal free day remained "
            "to move it to."
        ],
        "governance": dict(role.get("governance", {})),
        "locked_day": day,
        "replacement_role_key": LIGHT_COMBAT_ROLE_KEY,
    }


def _lock_declared_light_combat_roles(
    week_entry: dict,
    session_roles: list[dict],
    suppressed_roles: list[dict],
    athlete_model: dict,
    *,
    hard_sparring_plan: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Make every declared light-combat day a coach-owned immutable lock.

    Declared light-combat / technical days (``support_work_days``) are coach-owned
    calendar context, exactly like declared hard-sparring days. This is the
    normal-camp counterpart of :func:`_lock_declared_hard_sparring_roles`: it
    consumes the shared declared-combat ownership helper so normal camp and the
    D-13 inward spine agree on what a declared light-combat day is and what
    coach-owned role sits on it.

    Run after placement is final. For each declared light-combat weekday it:

    1. guarantees a coach-owned ``light_combat_day`` role on that exact day — it
       is never dropped, replaced, or moved; then
    2. reconciles anything the allocator co-located on that day against the shared
       ``combat_load_policy``. A candidate the policy lets share a light-combat day
       (low-load / true-microdose S&C — see ``_LIGHT_COMBAT_COEXIST_LOADS``) STAYS
       and stacks; a candidate the policy forbids (meaningful strength/conditioning
       or a second contact) is dropped into ``suppressed_roles``.

    It does **no** cross-week relocation. The allocator already chose every day
    with full context; this lock only reconciles the light-combat day, so it never
    invents a placement or expands the week onto an extra training day (a displaced
    S&C stacks in place when legal, else yields). Placement stays owned by
    ``_assign_declared_day_hints``.
    """
    training_days = [
        normalised
        for day in _ordered_weekdays(clean_list(athlete_model.get("training_days", [])))
        if (normalised := str(day or "").strip().lower())
    ]
    declared_light_days = declared_light_combat_weekdays(
        athlete_model, training_days=training_days, exclude_hard_sparring=True
    )
    if not declared_light_days:
        return session_roles, suppressed_roles

    light_day_set = set(declared_light_days)
    updated_roles = list(session_roles)
    updated_suppressed = list(suppressed_roles)

    scope = week_scope(week_entry) if isinstance(week_entry, dict) else ("normal_week", None)
    # The exclusive technical-contact profile the light-combat slot owns, used to
    # ask the shared policy whether a same-day app role may legally coexist.
    light_profile = role_load_profile({"role_key": LIGHT_COMBAT_ROLE_KEY})

    # 1. Guarantee the coach-owned light-combat lock on each declared weekday.
    for day in declared_light_days:
        already_locked = any(
            str(role.get("role_key") or "").strip().lower() == LIGHT_COMBAT_ROLE_KEY
            and str(role.get("scheduled_day_hint") or "").strip().lower() == day
            for role in updated_roles
        )
        if not already_locked:
            updated_roles.append(build_declared_light_combat_role(day))

    # 2. Reconcile same-day app roles against the exclusive light-combat owner.
    kept_roles: list[dict] = []
    for role in updated_roles:
        role_key = str(role.get("role_key") or "").strip().lower()
        day = str(role.get("scheduled_day_hint") or "").strip().lower()
        if role_key == LIGHT_COMBAT_ROLE_KEY or day not in light_day_set:
            kept_roles.append(role)
            continue

        profile = classify_role(role)
        if profile is None or light_profile is None:
            # Unclassifiable / coexistable support (tactical cue cards, labels):
            # legality is not this owner's to invent, so let it stack.
            kept_roles.append(role)
            continue

        position = weekday_position(day)
        candidate_position = position if position is not None else 0
        decision = evaluate_candidate_at_position(
            profile,
            candidate_position=candidate_position,
            events=[CalendarEvent(candidate_position, light_profile, scope)],
            candidate_scope=scope,
        )
        if decision.directive is not PlacementDirective.FORBID:
            # LEGAL -> stack with the light-combat slot on the same day. This uses
            # no extra training day, so it can never over-allocate the week.
            kept_roles.append(role)
        else:
            # ILLEGAL (meaningful S&C / second contact) -> the S&C yields; the
            # light-combat slot always remains. Not relocated: moving it to a fresh
            # day would expand the athlete's planned weekly training exposure.
            updated_suppressed.append(_make_light_combat_lock_suppression(role, day))

    for idx, role in enumerate(kept_roles, start=1):
        role["session_index"] = idx
    return kept_roles, updated_suppressed


def _replaceable_role_priority(role: dict, *, day: str) -> tuple[int, int]:
    scheduled_day = str(role.get("scheduled_day_hint") or "").strip()
    if scheduled_day == day:
        return (-1, 0)
    category = str(role.get("category") or "").strip()
    role_key = str(role.get("role_key") or "").strip()
    if category == "conditioning":
        if role.get("gas_tank_recovery_touch") or role.get("allowed_on_recovery_day"):
            return (3, 3)
        return (0 if role.get("preferred_system") == "glycolytic" else 1, 1)
    if category == "strength" and role_key not in PRIMARY_STRENGTH_ROLE_KEYS:
        return (2, 2)
    if category == "recovery":
        return (3, 3)
    if category == "strength":
        return (4, 4)
    return (5, 5)


def _stacks_with_resolved_contact(role: dict, day: str, plan_entry: dict | None, week_entry: dict) -> bool:
    """Whether the shared policy lets ``role`` share this day's resolved contact."""
    try:
        contact = contact_load_profile(plan_entry) if plan_entry else None
    except ValueError:  # unresolved contact keeps the strict displacement
        return False
    profile = classify_role(role)
    if contact is None or profile is None:
        return False
    scope = week_scope(week_entry) if isinstance(week_entry, dict) else ("normal_week", None)
    position = weekday_position(day.lower())
    position = position if position is not None else 0
    decision = evaluate_candidate_at_position(
        profile,
        candidate_position=position,
        events=[CalendarEvent(position, contact, scope)],
        candidate_scope=scope,
    )
    return decision.directive is not PlacementDirective.FORBID


def _lock_declared_hard_sparring_roles(
    week_entry: dict,
    session_roles: list[dict],
    suppressed_roles: list[dict],
    athlete_model: dict,
    *,
    hard_sparring_plan: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    declared_hard_days = _ordered_weekdays(
        clean_list(week_entry.get("declared_hard_sparring_days") or athlete_model.get("hard_sparring_days", []))
    )
    if not declared_hard_days:
        return session_roles, suppressed_roles

    updated_roles = list(session_roles)
    updated_suppressed = list(suppressed_roles)
    plan_by_day = {
        str(entry.get("day") or "").strip(): entry
        for entry in (hard_sparring_plan or [])
        if str(entry.get("day") or "").strip()
    }
    used_indices: set[int] = set()

    for day in declared_hard_days:
        plan_entry = plan_by_day.get(day)
        if plan_entry and plan_entry.get("effective_load") == "none":
            updated_roles = [role for role in updated_roles if not (
                role.get("role_key") == "hard_sparring_day"
                and str(role.get("scheduled_day_hint") or "").strip() == day
            )]
            if not any(item.get("locked_day") == day and item.get("hard_sparring_status") == "blocked" for item in updated_suppressed):
                updated_suppressed.append(_make_final_week_sparring_cap_suppression(day, plan_entry))
            continue
        if _is_final_week_capped_sparring_entry(plan_entry):
            existing_idx = next(
                (
                    idx for idx, role in enumerate(updated_roles)
                    if role.get("role_key") == "hard_sparring_day" and str(role.get("scheduled_day_hint") or "").strip() == day
                ),
                None,
            )
            replaced_role = None
            if existing_idx is not None:
                replaced_role = updated_roles.pop(existing_idx)
                used_indices = {idx - 1 if idx > existing_idx else idx for idx in used_indices if idx != existing_idx}
            if not any(
                item.get("locked_day") == day
                and "final_week_sparring_cap" in clean_list(item.get("hard_sparring_reason_codes"))
                for item in updated_suppressed
            ):
                updated_suppressed.append(_make_final_week_sparring_cap_suppression(day, plan_entry, replaced_role))
            _append_week_coach_note_flag(week_entry, "final week sparring cap")
            continue

        replacement = _hard_sparring_role(week_entry, day, plan_by_day.get(day))
        existing_idx = next(
            (
                idx for idx, role in enumerate(updated_roles)
                if role.get("role_key") == "hard_sparring_day" and str(role.get("scheduled_day_hint") or "").strip() == day
            ),
            None,
        )
        if existing_idx is not None:
            updated_roles[existing_idx] = replacement
            used_indices.add(existing_idx)
            continue

        # Prefer displacing a role already scheduled on this hard-sparring day
        # that clashes with its resolved contact. Only fall back to
        # cross-day poaching when the spar-day slot is otherwise empty AND
        # there are no spare training days; otherwise add the hard-sparring
        # role on its declared day without evicting non-conflicting roles.
        # Work the shared policy lets share a technical-only day stays, like
        # the light-combat lock; downstream compression trims by priority.
        same_day_idx = next(
            (
                idx
                for idx, role in enumerate(updated_roles)
                if idx not in used_indices
                and role.get("role_key") != "hard_sparring_day"
                and str(role.get("scheduled_day_hint") or "").strip().lower() == day.lower()
                and not _stacks_with_resolved_contact(role, day, plan_entry, week_entry)
            ),
            None,
        )
        if same_day_idx is not None:
            updated_suppressed.append(_make_hard_sparring_lock_suppression(updated_roles[same_day_idx], day))
            updated_roles[same_day_idx] = replacement
            used_indices.add(same_day_idx)
            continue

        # No role currently occupies this hard-sparring day — just append the
        # hard-sparring role. ``_apply_high_fatigue_week_compression`` runs
        # downstream and trims any genuine over-allocation through readiness /
        # compression signals instead of silent cross-day poaching here.
        updated_roles.append(replacement)
        used_indices.add(len(updated_roles) - 1)

    if any(role.get("coach_note_flags") for role in updated_roles if role.get("role_key") == "hard_sparring_day"):
        _append_week_coach_note_flag(week_entry, "deload hard sparring")

    for idx, role in enumerate(updated_roles, start=1):
        role["session_index"] = idx
    return updated_roles, updated_suppressed


def _assign_declared_day_hints(
    ordered: list[dict],
    athlete_model: dict,
    *,
    hard_sparring_plan: list[dict] | None = None,
    week_entry: dict | None = None,
) -> list[dict]:
    if not ordered:
        return ordered

    training_days, hard_sparring_days, support_work_days = _declared_day_sets(athlete_model)
    if not training_days:
        return ordered

    day_assignments: dict[int, str] = {}
    used_days: set[str] = set()
    # Resolver authority: only when no plan was supplied (resolver has not run) do
    # declared hard days stand in as effective-hard. A supplied plan — even one where
    # every declared day resolved to technical/reduced/off — is authoritative and is
    # never overridden back to hard. This governs the sandwiched_days *preference*;
    # the canonical legality view below applies the same rule for the FORBID gate.
    if hard_sparring_plan is None:
        effective_hard_days_set = set(hard_sparring_days)
    else:
        effective_hard_days_set = set(effective_hard_days(hard_sparring_plan))
    sandwiched_days = set(sandwiched_training_days(training_days, effective_hard_days_set))

    # Step 9B: the placement owner consults the shared combat_load_policy for every
    # physical candidate day rather than deciding same-day-exclusivity /
    # between-hard-contact legality from its own weekday membership sets. Candidate
    # generation, preference order, anchors, and deterministic tie-breaking stay
    # here; ALLOW/DEPRIORITIZE/FORBID come from the policy (via the canonical
    # calendar_context adapter). Contact events use the resolved hard_sparring_plan
    # so a downgraded declared day is reduced/technical contact, not hard.
    scope = week_scope(week_entry) if isinstance(week_entry, dict) else ("normal_week", None)
    _legality = normal_week_legality(
        hard_sparring_plan, hard_sparring_days, scope=scope,
        declared_light_days=declared_light_combat_weekdays(athlete_model, training_days=training_days),
    )
    _profile_cache: dict[int, Any] = {}

    def _role_profile(idx: int):
        if idx not in _profile_cache:
            _profile_cache[idx] = classify_role(ordered[idx])
        return _profile_cache[idx]

    def _pick_legal_day(idx: int, candidate_days: list[str], unclassified_fallback: str | None) -> str | None:
        """Best legal day for role ``idx`` among ``candidate_days`` (owner order).

        ALLOW is preferred over DEPRIORITIZE; owner order breaks ties within a
        tier. Returns ``None`` when every candidate is FORBID — FORBID means the
        day is *unavailable*, so the owner leaves the role for its existing
        dayless/unresolved handling instead of committing a forbidden slot (a
        forbidden placement can never be overridden back in by local preference).
        ``unclassified_fallback`` is used only when the role cannot be classified,
        where legality is not this owner's to decide.
        """
        profile = _role_profile(idx)
        if profile is None:
            return unclassified_fallback
        return _legality.best_legal_weekday(profile, candidate_days)

    def _directive_rank(idx: int, day: str) -> int:
        """Canonical legality tier for role ``idx`` on ``day``.

        0=ALLOW, 1=DEPRIORITIZE, 2=FORBID. An unclassifiable role or unmappable
        day yields 0 so the owner keeps its own anchor preference there.
        """
        profile = _role_profile(idx)
        position = weekday_position(day)
        if profile is None or position is None:
            return 0
        return placement_rank(_legality.decision_at_position(profile, position))

    # Preserve explicit scheduled days for locked roles.
    # Hard sparring days are coach-owned anchors and must never move.
    # Generated low-aerobic recovery touches are also emitted with explicit
    # day hints after compression and should survive this assignment pass.
    for idx, role in enumerate(ordered):
        locked_day = str(role.get("scheduled_day_hint") or "").strip()
        if not locked_day or locked_day not in training_days:
            continue
        if role.get("role_key") == "hard_sparring_day":
            if locked_day in used_days:
                continue
            day_assignments[idx] = locked_day
            used_days.add(locked_day)
            continue
        if (
            role.get("gas_tank_recovery_touch")
            or role.get("converted_from_unused_day")
            or role.get("allowed_on_recovery_day")
        ):
            day_assignments[idx] = locked_day
            used_days.add(locked_day)
            continue
        if locked_day in used_days:
            continue

    recovery_idx = next((idx for idx, role in enumerate(ordered) if role.get("category") == "recovery"), None)
    primary_idx = next(
        (idx for idx, role in enumerate(ordered) if role.get("category") == "strength" and role.get("role_key") in PRIMARY_STRENGTH_ROLE_KEYS),
        None,
    )
    glycolytic_idx = next(
        (
            idx
            for idx, role in enumerate(ordered)
            if role.get("category") == "conditioning" and role.get("preferred_system") == "glycolytic"
        ),
        None,
    )
    aerobic_idx = next(
        (
            idx
            for idx, role in enumerate(ordered)
            if role.get("category") == "conditioning" and role.get("preferred_system") == "aerobic"
        ),
        None,
    )
    alactic_idx = next((idx for idx, role in enumerate(ordered)
                        if role.get("category") == "conditioning" and role.get("preferred_system") == "alactic"), None)
    priorities = goal_priority_scores(athlete_model)
    strength_is_build_priority = build_priority_profile(athlete_model).primary_weak_area == "strength"
    priority_conditioning_idx = (
        alactic_idx if not strength_is_build_priority and priorities["speed"] >= 30 and priorities["speed"] > priorities["strength"]
        else glycolytic_idx if not strength_is_build_priority and priorities["conditioning"] >= 30 and priorities["conditioning"] > priorities["strength"]
        else None
    )
    if priority_conditioning_idx is not None:
        priority_day = _pick_legal_day(priority_conditioning_idx, [day for day in training_days if day not in used_days], None)
        if priority_day:
            day_assignments[priority_conditioning_idx] = priority_day
            used_days.add(priority_day)
    if recovery_idx is not None and primary_idx is not None and len(training_days) >= 2:
        middle = max(0, len(training_days) // 2)
        best_pair: tuple[int, int] | None = None
        best_score: int | None = None
        # The recovery -> primary adjacency, the mid-week target, and the recovery
        # support/sandwiched preferences are the owner's anchor logic. Legality is
        # the policy's: a FORBID day for either anchor is skipped (a forbidden
        # anchor is never committed on preference), and a DEPRIORITIZE day is a
        # legal fallback that ranks strictly below an ALLOW day.
        for idx in range(len(training_days) - 1):
            recovery_day = training_days[idx]
            primary_day = training_days[idx + 1]
            if recovery_day in used_days or primary_day in used_days:
                continue
            primary_rank = _directive_rank(primary_idx, primary_day)
            recovery_rank = _directive_rank(recovery_idx, recovery_day)
            if primary_rank == 2 or recovery_rank == 2:
                continue
            score = 100
            score -= (primary_rank + recovery_rank) * 1000
            if recovery_day in support_work_days:
                score += 4
            if recovery_day in sandwiched_days:
                score += 5
            score -= abs((idx + 1) - middle)
            if best_score is None or score > best_score:
                best_score = score
                best_pair = (idx, idx + 1)

        if best_pair is not None:
            recovery_day = training_days[best_pair[0]]
            primary_day = training_days[best_pair[1]]
            day_assignments[recovery_idx] = recovery_day
            day_assignments[primary_idx] = primary_day
            used_days.update({recovery_day, primary_day})
        else:
            # No legal adjacent pair. Anchor the primary strength role on its best
            # legal free day (ALLOW before DEPRIORITIZE). If every free day is
            # FORBID it stays dayless — the owner's existing unresolved handling —
            # rather than taking a forbidden slot that a later pass could keep.
            primary_anchor_day = _pick_legal_day(
                primary_idx,
                [day for day in training_days if day not in used_days],
                None,
            )
            if primary_anchor_day:
                day_assignments[primary_idx] = primary_anchor_day
                used_days.add(primary_anchor_day)
        # If neither anchor committed here, the per-role fallback below still runs
        # through the same canonical gate.

    # A recovery slot may already have become a gas-tank flush. That conversion
    # must not erase the primary strength anchor and let lower-priority support
    # consume every compatible day before strength gets considered.
    if primary_idx is not None and primary_idx not in day_assignments:
        primary_day = _pick_legal_day(primary_idx, [day for day in training_days if day not in used_days], None)
        if primary_day:
            day_assignments[primary_idx] = primary_day
            used_days.add(primary_day)

    if glycolytic_idx is not None and glycolytic_idx not in day_assignments:
        # Owner preference: latest declared training day. Legality (same-day
        # contact / between-hard) is the policy's; the raw fallback preserves the
        # prior "latest non-spar, else latest" pick for the no-legal case (where
        # the structural between-hard suppression then owns the outcome).
        glycolytic_candidates = [day for day in reversed(training_days) if day not in used_days]
        raw_glycolytic_day = next(
            (day for day in reversed(training_days) if day not in hard_sparring_days and day not in used_days),
            None,
        ) or next((day for day in reversed(training_days) if day not in used_days), None)
        preferred_glycolytic_day = _pick_legal_day(
            glycolytic_idx, glycolytic_candidates, raw_glycolytic_day
        )
        if preferred_glycolytic_day:
            day_assignments[glycolytic_idx] = preferred_glycolytic_day
            used_days.add(preferred_glycolytic_day)

    if aerobic_idx is not None:
        # Owner preference: declared Support Work Days, in weekday order.
        aerobic_candidates = [
            day for day in training_days if day in support_work_days and day not in used_days
        ]
        raw_aerobic_day = aerobic_candidates[0] if aerobic_candidates else None
        preferred_aerobic_day = _pick_legal_day(aerobic_idx, aerobic_candidates, raw_aerobic_day)
        if preferred_aerobic_day:
            day_assignments[aerobic_idx] = preferred_aerobic_day
            used_days.add(preferred_aerobic_day)

    for idx, day in day_assignments.items():
        role = ordered[idx]
        reason = ""
        if role.get("role_key") == "hard_sparring_day":
            reason = "Declared hard sparring days stay locked in the weekly role map; only the sparring dose may deload."
        elif idx == primary_idx:
            reason = "Keep the main neural-strength slot away from declared hard sparring and immediately after the recovery day when possible."
        elif idx == recovery_idx:
            reason = "Use the lowest-load day immediately before the primary strength anchor when possible."
        elif idx == glycolytic_idx and day in hard_sparring_days:
            reason = "Let declared hard sparring own the main collision-heavy combat load when it already exists."
        elif idx == aerobic_idx and day in support_work_days:
            reason = "Use declared Support Work Days (Light Combat days / S&C-compatible slots) for lower-noise support work when possible."
        _append_day_hint(role, day, reason)
    
    for idx, role in enumerate(ordered):
        if idx in day_assignments:
            continue

        is_recovery_compatible = bool(
            role.get("recovery_compatible")
            or role.get("allowed_on_recovery_day")
            or role.get("gas_tank_recovery_touch")
            or role.get("priority_recovery_touch")
            or role.get("category") == "recovery"
            or (role.get("category") == "conditioning" and role.get("preferred_system") == "aerobic")
        )

        if is_recovery_compatible:
            # Owner preference: between-hard (sandwiched) days first for low-load
            # recovery-compatible support, then the remaining free training days.
            recovery_candidates = [
                day for day in training_days if day in sandwiched_days and day not in used_days
            ] + [
                day for day in training_days if day not in sandwiched_days and day not in used_days
            ]
            raw_recovery_fallback = (
                next((day for day in training_days if day in sandwiched_days and day not in used_days), None)
                or next((day for day in training_days if day not in used_days and day not in hard_sparring_days), None)
                or next((day for day in training_days if day not in used_days), None)
            )
            fallback_day = _pick_legal_day(idx, recovery_candidates, raw_recovery_fallback)
            if fallback_day:
                day_assignments[idx] = fallback_day
                used_days.add(fallback_day)
                _append_day_hint(
                    role,
                    fallback_day,
                    "Use sandwiched days for low-load recovery-compatible support first, then fill unused Light Combat days.",
                )
            continue

        # Owner preference: keep non-recovery stress on a clean (non-spar,
        # non-sandwiched) day first; the remaining free days are the fallback tail.
        stressor_candidates = [
            day
            for day in training_days
            if day not in used_days and day not in hard_sparring_days and day not in sandwiched_days
        ] + [
            day
            for day in training_days
            if day not in used_days and (day in hard_sparring_days or day in sandwiched_days)
        ]
        raw_stressor_fallback = (
            next(
                (
                    day
                    for day in training_days
                    if day not in used_days and day not in hard_sparring_days and day not in sandwiched_days
                ),
                None,
            )
            or next((day for day in training_days if day not in used_days and day not in hard_sparring_days), None)
            or next((day for day in training_days if day not in used_days), None)
        )
        fallback_day = _pick_legal_day(idx, stressor_candidates, raw_stressor_fallback)
        if fallback_day:
            day_assignments[idx] = fallback_day
            used_days.add(fallback_day)
            _append_day_hint(
                role,
                fallback_day,
                "Keep non-recovery-compatible stress away from sandwiched hard-sparring days when a cleaner slot exists.",
            )
    for idx, role in enumerate(ordered):
        if idx not in day_assignments:
            _append_day_hint(role, "")

    return ordered


def _preferred_boxer_conditioning_sequence(phase: str, conditioning_sequence: list[str]) -> list[str]:
    """Order the systems the athlete's limiter left unranked, boxer-first.

    The limiter profile is the authority on relative priority: it already encodes
    what this athlete most needs. Prepending a boxer preference overrode it, so a
    boxer with only two conditioning slots in GPP lost the fight-pace exposure
    their own conditioning limiter had ranked second and received an alactic day
    instead - the 0.2 system displacing the 0.3 system for no safety or contact
    reason. The preference now only orders systems the limiter did not rank, and
    never demotes one it did.
    """
    phase = str(phase or "").upper()
    if phase == "GPP":
        preferred = ["aerobic", "alactic", "glycolytic"]
    elif phase == "SPP":
        preferred = ["aerobic", "glycolytic", "alactic"]
    else:
        preferred = ["alactic", "aerobic", "glycolytic"]

    limiter_order = dedupe_preserve_order([str(s) for s in (conditioning_sequence or []) if s])
    merged = dedupe_preserve_order(preferred + limiter_order)

    # The only place the preference inverts the phase ratios is GPP, where it
    # ranks alactic (0.2) above glycolytic (0.3). When the limiter itself ranked
    # glycolytic higher, that inversion costs a two-slot week its fight-pace day,
    # so the limiter's relative order wins. Every other ordering the preference
    # expresses - including leading SPP with aerobic - is left alone.
    if "glycolytic" in limiter_order and "alactic" in limiter_order:
        if limiter_order.index("glycolytic") < limiter_order.index("alactic"):
            merged = [system for system in merged if system != "glycolytic"]
            merged.insert(merged.index("alactic"), "glycolytic")
    return merged


def _resequence_session_roles(
    week_entry: dict,
    session_roles: list[dict],
    athlete_model: dict,
    *,
    hard_sparring_plan: list[dict] | None = None,
) -> list[dict]:
    if len(session_roles) <= 1:
        return session_roles

    ordered = list(session_roles)
    sport_key = _athlete_sport_key(athlete_model)
    phase = str(week_entry.get("phase", "")).upper()

    def _is_primary_strength(role: dict) -> bool:
        return role.get("category") == "strength" and role.get("role_key") in PRIMARY_STRENGTH_ROLE_KEYS

    def _is_support_strength(role: dict) -> bool:
        return role.get("category") == "strength" and not _is_primary_strength(role)

    def _is_low_damage_conditioning(role: dict) -> bool:
        if role.get("category") != "conditioning":
            return False
        if role.get("preferred_system") == "aerobic":
            return True
        return role.get("role_key") in {"repeatability_support_day", "controlled_repeatability_day"}

    def _take_first(predicate, used: set[int], result: list[dict]) -> None:
        for idx, role in enumerate(ordered):
            if idx in used:
                continue
            if predicate(role):
                used.add(idx)
                result.append(role)
                return

    if sport_key == "boxing" and phase in {"GPP", "SPP"}:
        used: set[int] = set()
        result: list[dict] = []
        _take_first(_is_support_strength, used, result)
        _take_first(_is_low_damage_conditioning, used, result)
        _take_first(lambda role: role.get("category") == "recovery", used, result)
        _take_first(_is_primary_strength, used, result)
        for idx, role in enumerate(ordered):
            if idx in used:
                continue
            if role.get("category") == "conditioning":
                used.add(idx)
                result.append(role)
        for idx, role in enumerate(ordered):
            if idx in used:
                continue
            result.append(role)
        ordered = result
    else:
        recovery_idx = next((idx for idx, role in enumerate(ordered) if role.get("category") == "recovery"), None)
        primary_idx = next((idx for idx, role in enumerate(ordered) if _is_primary_strength(role)), None)
        if recovery_idx is not None and primary_idx is not None and primary_idx != recovery_idx + 1:
            primary_role = ordered.pop(primary_idx)
            if primary_idx < recovery_idx:
                recovery_idx -= 1
            ordered.insert(recovery_idx + 1, primary_role)

    for idx, role in enumerate(ordered, start=1):
        role["session_index"] = idx
    ordered = _assign_declared_day_hints(
        ordered, athlete_model, hard_sparring_plan=hard_sparring_plan, week_entry=week_entry
    )
    return ordered


def _short_camp_priority_catalog(compressed: dict) -> dict[str, str]:
    label_by_kind: dict[str, str] = {}
    for bucket in ("primary_targets", "maintenance_targets", "embedded_support", "deferred"):
        for entry in compressed.get(bucket, []) or []:
            kind = str((entry or {}).get("kind", "")).strip()
            label = str((entry or {}).get("label", "")).strip()
            if kind and label and kind not in label_by_kind:
                label_by_kind[kind] = label
    return label_by_kind


def _compressed_priority_for_role(role: dict, athlete_model: dict) -> tuple[str, str]:
    compressed = athlete_model.get("compressed_priorities") or {}
    label_by_kind = _short_camp_priority_catalog(compressed)
    if not compressed.get("is_short_camp"):
        return "", ""

    role_key = str(role.get("role_key", "")).strip()
    category = str(role.get("category", "")).strip()
    system = str(role.get("preferred_system", "")).strip()

    if category == "recovery":
        if label_by_kind.get("freshness_protection"):
            return label_by_kind["freshness_protection"], "primary_target"
        return "embedded recovery support", "embedded_support"

    if category == "conditioning" and system == "aerobic" and label_by_kind.get("conditioning_maintenance"):
        return label_by_kind["conditioning_maintenance"], "maintenance_target"

    if category == "conditioning" and system == "glycolytic" and label_by_kind.get("conditioning_maintenance"):
        return label_by_kind["conditioning_maintenance"], "maintenance_target"

    if (
        category == "conditioning"
        and system == "alactic"
        and label_by_kind.get("power_expression")
    ):
        return label_by_kind["power_expression"], "primary_target"

    if role_key in {
        "aerobic_coordination_day",
        "repeatability_support_day",
        "aerobic_support_day",
        "controlled_repeatability_day",
        "fight_pace_repeatability_day",
        "light_fight_pace_touch_day",
    } and label_by_kind.get("technical_sharpness"):
        return label_by_kind["technical_sharpness"], "primary_target"

    if role_key in {"primary_strength_day", "neural_plus_strength_day", "neural_primer_day", "alactic_sharpness_day", "alactic_speed_day"}:
        if label_by_kind.get("power_expression"):
            return label_by_kind["power_expression"], "primary_target"
        if label_by_kind.get("technical_sharpness"):
            return label_by_kind["technical_sharpness"], "primary_target"

    if role_key in {"strength_touch_day", "transfer_strength_day", "small_strength_touch_day"}:
        if label_by_kind.get("power_expression"):
            return label_by_kind["power_expression"], "primary_target"

    return "", ""


def _apply_short_camp_role_compression(
    week_entry: dict,
    session_roles: list[dict],
    suppressed_roles: list[dict],
    athlete_model: dict,
) -> tuple[list[dict], list[dict]]:
    compressed = athlete_model.get("compressed_priorities") or {}
    if not compressed.get("is_short_camp"):
        return session_roles, suppressed_roles

    kept_roles: list[dict] = []
    updated_suppressed = list(suppressed_roles)

    for role in session_roles:
        label, bucket = _compressed_priority_for_role(role, athlete_model)
        if label:
            role["compressed_priority_label"] = label
            role["compressed_priority_bucket"] = bucket
            kept_roles.append(role)
            continue
        if role.get("category") == "recovery":
            role["compressed_priority_label"] = "embedded recovery support"
            role["compressed_priority_bucket"] = "embedded_support"
            kept_roles.append(role)
            continue
        updated_suppressed.append(
            {
                "category": role.get("category"),
                "role_key": role.get("role_key"),
                "preferred_system": role.get("preferred_system", ""),
                "reasons": [
                    "Short-camp compression removed this standalone session purpose because it did not map to a compressed week-level priority."
                ],
                "governance": dict(role.get("governance", {})),
            }
        )

    for idx, role in enumerate(kept_roles, start=1):
        role["session_index"] = idx
    return kept_roles, updated_suppressed


def _intentional_compression_stub() -> dict[str, Any]:
    return {
        "active": False,
        "reason_codes": [],
        "reason": "",
        "summary": "",
    }


def _high_fatigue_compression_reason_codes(
    athlete_model: dict,
    *,
    effective_hard_spar_count: int | None = None,
) -> list[str]:
    fatigue = str(athlete_model.get("fatigue", "")).strip().lower()
    readiness_flags = set(clean_list(athlete_model.get("readiness_flags", [])))
    if fatigue != "high" and "high_fatigue" not in readiness_flags:
        return []

    reason_codes = ["high_fatigue"]
    hard_spar_count = effective_hard_spar_count
    if hard_spar_count is None:
        hard_spar_count = len(clean_list(athlete_model.get("hard_sparring_days", [])))
    if hard_spar_count >= 2:
        reason_codes.append("two_hard_spar_days")
    if _is_high_pressure_weight_cut(athlete_model=athlete_model):
        reason_codes.append("high_pressure_weight_cut")
    elif athlete_model.get("weight_cut_risk") or readiness_flags & {"active_weight_cut", "aggressive_weight_cut"}:
        reason_codes.append("active_weight_cut")
    if _active_injury_is_moderate_plus(athlete_model):
        reason_codes.append("injury_management")
    return reason_codes


def _compression_summary(reason_codes: list[str]) -> str:
    if not reason_codes:
        return ""
    label = ", ".join(code.replace("_", " ") for code in reason_codes)
    return f"Keep the smaller week on purpose to protect freshness under {label}."


def _next_training_days_after_effective_hard_spar(
    training_days: list[str],
    effective_hard_days_list: set[str],
) -> set[str]:
    if not training_days or not effective_hard_days_list:
        return set()

    next_days: set[str] = set()
    ordered_training_days = _ordered_weekdays(training_days)
    for hard_day in effective_hard_days_list:
        hard_day_index = _WEEKDAY_ORDER.get(str(hard_day).strip().lower(), -1)
        if hard_day_index < 0:
            continue
        next_day = next(
            (
                day
                for day in ordered_training_days
                if _WEEKDAY_ORDER.get(str(day).strip().lower(), -1) > hard_day_index
            ),
            None,
        )
        if next_day:
            next_days.add(next_day)
    return next_days


def _make_compression_suppression(role: dict, reason_codes: list[str], summary: str) -> dict[str, Any]:
    return {
        "category": role.get("category"),
        "role_key": role.get("role_key"),
        "preferred_system": role.get("preferred_system", ""),
        "reasons": [summary],
        "governance": dict(role.get("governance", {})),
        "intentional_compression": True,
        "reason_codes": list(reason_codes),
        "compression_reason_codes": list(reason_codes),
        "compression_summary": summary,
    }


def _active_weight_cut_is_meaningful(athlete_model: dict) -> bool:
    """True when the athlete has a non-trivial target-weight constraint."""
    cut_bucket = _resolved_cut_severity_bucket(athlete_model)
    if cut_bucket is not None:
        return cut_bucket in {"moderate", "high", "critical", "extreme"}
    if athlete_model.get("weight_cut_risk"):
        return True
    readiness_flags = set(clean_list(athlete_model.get("readiness_flags", [])))
    return bool(readiness_flags & {"active_weight_cut", "aggressive_weight_cut"})


def _active_weight_cut_is_declared(athlete_model: dict) -> bool:
    """Whether the athlete declared a cut at all, regardless of its severity."""
    if athlete_model.get("weight_cut_risk"):
        return True
    readiness_flags = set(clean_list(athlete_model.get("readiness_flags", [])))
    return bool(readiness_flags & {"active_weight_cut", "aggressive_weight_cut"})


def _resolved_cut_severity_bucket(athlete_model: dict) -> str | None:
    """Resolve cut severity bucket from numeric truth when available."""
    explicit_bucket = str(athlete_model.get("cut_severity_bucket") or "").strip().lower()
    if explicit_bucket in {"none", "low", "moderate", "high", "critical", "extreme"}:
        return explicit_bucket

    try:
        cut_score = float(athlete_model.get("cut_severity_score"))
    except (TypeError, ValueError):
        cut_score = None
    if cut_score is not None:
        return cut_severity_bucket(cut_score)

    try:
        float(athlete_model.get("weight_cut_pct"))
    except (TypeError, ValueError):
        return None

    return cut_severity_bucket(
        compute_cut_severity_score(
            athlete_model.get("weight_cut_pct"),
            athlete_model.get("days_until_fight"),
        )
    )


def _cut_severity_compression_points(athlete_model: dict) -> int:
    """Weekly slots this cut may remove — delegated to the canonical authority.

    This is the ONLY place a weight cut subtracts weekly training capacity. The
    mapping itself lives in ``weight_cut.cut_training_compression_points`` so no
    module can drift into a second opinion about what a cut costs.

    An unresolvable bucket contributes nothing: "an active cut exists" is not by
    itself a reason to delete a session. It used to add a point, which meant a
    cut of unknown magnitude was punished like a real one.
    """
    cut_bucket = _resolved_cut_severity_bucket(athlete_model)
    if cut_bucket is None:
        return 0
    return cut_training_compression_points(cut_bucket)


def _active_injury_is_moderate_plus(athlete_model: dict) -> bool:
    """Symptoms alone do not establish a whole-programme load limitation.

    Use the existing severity-aware policy; exercise-specific restrictions
    remain authoritative in candidate selection and calendar legality.
    """
    return _boxing_crowded_week_injury_is_moderate_plus(athlete_model)


def _boxing_crowded_week_injury_is_moderate_plus(athlete_model: dict) -> bool:
    """Shared severity-aware signal for normal role-budget policies."""
    if athlete_model.get("injury_mode") in {"medical_hold", "restricted_rehab_only"}:
        return True
    readiness_flags = set(clean_list(athlete_model.get("readiness_flags", [])))
    if readiness_flags & {
        "high_injury",
        "significant_injury",
        "severe_injury",
        "red_flag_injury",
    }:
        return True
    # A contact-readiness flag alone is not medical severity. Only current,
    # sourced capacity evidence may promote it to global compression.
    if "moderate_injury" in readiness_flags and any(
        item.get("flag") == "moderate_injury" and item.get("scope") == "training_capacity"
        for item in athlete_model.get("readiness_evidence") or []
    ):
        return True
    if _all_active_injuries_surface_only(athlete_model):
        return False

    structured_entries: list[dict] = []
    parsed_injuries = athlete_model.get("parsed_injuries")
    if isinstance(parsed_injuries, (list, tuple)):
        structured_entries.extend(
            entry for entry in parsed_injuries if isinstance(entry, dict)
        )
    guided_injury = athlete_model.get("guided_injury")
    if isinstance(guided_injury, dict):
        structured_entries.append(guided_injury)
    injury_restrictions = athlete_model.get("injury_restrictions")
    if isinstance(injury_restrictions, (list, tuple)):
        structured_entries.extend(
            entry for entry in injury_restrictions if isinstance(entry, dict)
        )

    from .injury_awareness import (
        CANT_TRAIN, LIMITING, has_independent_medical_override,
        reported_training_impact, is_awareness_only,
    )
    if any(has_independent_medical_override(entry) for entry in structured_entries):
        return True
    structured_entries = [entry for entry in structured_entries
                          if not is_awareness_only(entry)]
    if any(reported_training_impact(entry) in {LIMITING, CANT_TRAIN} for entry in structured_entries):
        return True
    structured_severities = {
        str(entry.get("severity") or "").strip().lower()
        for entry in structured_entries
        # A parser fallback is unknown impact, not evidence of global load loss.
        if str(entry.get("severity") or "").strip()
        and entry.get("severity_source") != "fallback_default"
    }
    if structured_severities:
        return bool(
            structured_severities
            & {
                "moderate",
                "high",
                "severe",
                "major",
                "significant",
                "critical",
                "grade 2",
                "grade ii",
                "grade 3",
                "grade iii",
            }
        )

    # Legacy athlete snapshots may predate structured severity fields. Keep the
    # raw-text fallback, but do not treat the generic ``injury_management`` flag
    # as severity: the canonical builder emits it for mild injuries too.
    for entry in clean_list(athlete_model.get("injuries", [])):
        lowered = entry.lower()
        if any(
            token in lowered
            for token in (
                "moderate",
                "severe",
                "major",
                "significant",
                "grade 2",
                "grade ii",
                "grade 3",
                "grade iii",
            )
        ):
            return True
    return False


def _compute_readiness_compression(athlete_model: dict) -> int:
    """
    Compute readiness compression score (0–5) based on:
    - High fatigue (+1)
    - Active cut severity (+0/+1/+2)
    - Active injury/restriction at moderate or greater severity (+1)
    - Proximity to fight (≤17 days) (+1), only as an amplifier of another
      generic readiness signal — never on its own
    """
    return _readiness_compression_components(athlete_model).total


class _CompressionComponents(NamedTuple):
    """Readiness compression split by origin.

    ``cut`` is an exact slot count promised by the canonical cut authority;
    ``generic`` is a points score that passes through the non-linear floor
    curve. They are kept apart because folding the cut into the generic score
    silently discarded part of what the cut authority promised: a critical cut
    contributes 2 points, but 2 generic points map to only *one* removed slot.
    """

    generic: int
    cut: int

    @property
    def total(self) -> int:
        return self.generic + self.cut


def _readiness_compression_components(athlete_model: dict) -> _CompressionComponents:
    compression = 0
    fatigue = str(athlete_model.get("fatigue", "")).strip().lower()
    if fatigue == "high":
        compression += 1
    cut_points = _cut_severity_compression_points(athlete_model)
    if _active_injury_is_moderate_plus(athlete_model):
        compression += 1
    if compression and _proximity_amplifies_compression(athlete_model, cut_points=cut_points):
        compression += 1
    return _CompressionComponents(generic=compression, cut=cut_points)


def _proximity_amplifies_compression(athlete_model: dict, *, cut_points: int) -> bool:
    """Whether fight proximity adds a readiness-compression point.

    Proximity is never a session-removal signal on its own. It is already
    charged where it belongs: the phase/taper allocation, the scheduled-day
    countdown dose bands (``late_camp_role_morph``, D-17 inward), the late-fight
    budget (D-13 inward) and the shared calendar legality. Removing a whole
    session for a fresh, uninjured, non-cutting athlete because the fight is
    17 days away charged the same countdown a second time, so proximity only
    amplifies a genuine generic readiness signal (high fatigue, a moderate or
    worse injury). The cut authority already folds days-out into its severity
    score (exp(-days/15)), so proximity never stacks on a cut point either.
    """
    days_to_fight = athlete_model.get("days_until_fight")
    return cut_points == 0 and isinstance(days_to_fight, int) and 0 <= days_to_fight <= 17


def _compression_floor_value(compression: int) -> int:
    """Convert a *generic* readiness score to removed non-spar slots.

    Cut severity is deliberately NOT routed through this curve — see
    :func:`_readiness_compression_floor`. This curve is lossy by design (1 and 2
    points both mean one slot), which is fine for soft signals but would break
    the exact slot count the cut authority promises.
    """
    if compression == 0:
        return 0
    if compression <= 2:
        return 1
    return 2  # compression >= 3


def _readiness_compression_floor(athlete_model: dict) -> int:
    """Non-spar slots to remove, honouring the cut authority's exact promise.

    The cut's charge is added rather than blended, so
    ``cut_training_compression_points`` means what it says at the allocator:
    moderate removes 0, high removes 1, critical and extreme remove 2.

    This is the *raw* readiness demand. It is deliberately not bounded here: a
    fixed ceiling cannot know the week it is being applied to, so the real
    protection is capacity-aware and lives in
    :func:`_effective_compression_floor`, which is what the allocator uses.
    """
    components = _readiness_compression_components(athlete_model)
    return _compression_floor_value(components.generic) + components.cut


def _zero_physical_week_is_authorised(athlete_model: dict) -> bool:
    """Whether an explicit safety authority may take the week to no physical work.

    Readiness compression is not such an authority. Only a medical hold, a
    red-flag injury, or fight day itself may legitimately leave an athlete with
    nothing physical scheduled.
    """
    mode = str(athlete_model.get("injury_mode", "")).strip().lower()
    if mode in {"medical_hold", "restricted_rehab_only"}:
        return True
    readiness = {
        str(flag).strip().lower()
        for flag in clean_list(athlete_model.get("readiness_flags", []))
    }
    if {"severe_injury", "red_flag_injury"} & readiness:
        return True
    days = athlete_model.get("days_until_fight")
    return isinstance(days, int) and days <= 0


def _minimum_required_non_spar_exposures(
    athlete_model: dict, *, combat_role_count: int
) -> int:
    """Non-spar sessions that readiness compression may never remove.

    The week still needs *some* physical work. A combat session is physical
    work, so when one is present the non-spar count may legitimately fall to
    zero late in taper — we do not manufacture filler just to hit a frequency
    number. With no combat session in the week, one non-spar exposure must
    survive, because otherwise a declared two-session week silently becomes a
    zero-session week.

    ``combat_role_count`` must be a count of *actual combat roles in the week*,
    never of declared hard-sparring days. A declared day whose combat role was
    never built or has since been suppressed is not physical work, and treating
    the declaration as the exemption let compression strip every remaining
    non-spar role and leave nothing at all.
    """
    if _zero_physical_week_is_authorised(athlete_model):
        return 0
    if combat_role_count > 0:
        return 0
    return 1


def _effective_compression_floor(
    athlete_model: dict, *, non_spar_cap: int, combat_role_count: int
) -> int:
    """Readiness compression bounded by what the week can actually afford.

    A fixed ceiling (this used to be a flat 3) cannot protect a low-frequency
    week: for a two-session athlete no constant is small enough to guarantee
    survival, and in TAPER ``min_non_spar_active`` is 0, so a critical cut could
    take a declared 2-session week to 0/2. Clamping against the real capacity is
    the stronger invariant.
    """
    minimum_required = _minimum_required_non_spar_exposures(
        athlete_model, combat_role_count=combat_role_count
    )
    max_removable = max(0, int(non_spar_cap) - minimum_required)
    return min(_readiness_compression_floor(athlete_model), max_removable)


def _can_keep_low_noise_conditioning(athlete_model: dict) -> bool:
    fatigue = str(athlete_model.get("fatigue", "")).strip().lower()
    if fatigue == "high":
        return False
    mode = str(athlete_model.get("injury_mode", "")).strip().lower()
    if mode in {"medical_hold", "restricted_rehab_only"}:
        return False
    readiness = {str(v).strip().lower() for v in clean_list(athlete_model.get("readiness_flags", []))}
    if {"severe_injury", "red_flag_injury"} & readiness:
        return False
    days = athlete_model.get("days_until_fight")
    if isinstance(days, int) and days <= 0:
        return False
    return True


def _is_low_noise_conditioning_role(role: dict, athlete_model: dict) -> bool:
    if str(role.get("category") or "") != "conditioning":
        return False
    system = str(role.get("preferred_system") or "").strip().lower()
    if system in {"aerobic", "alactic"}:
        return True
    equipment = {str(v).strip().lower().replace(" ", "_") for v in clean_list(athlete_model.get("equipment", []))}
    return bool(equipment & {"assault_bike", "air_bike", "rower", "stationary_bike", "bike"})


def _non_spar_role_priority_rank(
    role: dict,
    phase: str,
    is_hard_spar_week: bool,
    is_meaningful_cut: bool,
    must_keep: set[str] | None = None,
    athlete_model: dict | None = None,
) -> int:
    """
    Return a priority rank for a non-sparring role.
    Higher rank = higher priority (kept when budget is tight).
    Must-keep roles receive the highest rank (100).
    """
    if must_keep is None:
        must_keep = set()

    role_key = str(role.get("role_key") or "").strip()
    preferred_system = str(role.get("preferred_system") or "").strip()
    category = str(role.get("category") or "").strip()

    # Must-keep roles always survive compression
    if (preferred_system in must_keep or role_key in must_keep
            or ("primary_strength" in must_keep and role_key in PRIMARY_STRENGTH_ROLE_KEYS
                and role.get("category") == "strength")):
        return 100

    # Dedicated recovery/mobility support is a protected low-load touch. It sits
    # below primary strength / hard sparring / must-keep / hard safety locks, but
    # above generic accessories and non-essential extra conditioning. Creation-time
    # guards (_can_preserve_one_non_gas_low_load_support, _low_aerobic_support_cap_for_week,
    # the D-1/D-0 block) already prevent it from existing under red-flag injury,
    # medical hold, fight-week cap=0, or severe-cut + high-fatigue conditions.
    if role.get("is_dedicated_recovery_mobility_day") is True:
        return 3

    athlete_model = athlete_model or {}
    preserve_low_noise = _conditioning_limiter_signal(athlete_model) and _can_keep_low_noise_conditioning(athlete_model)
    demote_glycolytic = is_hard_spar_week or is_meaningful_cut or _active_injury_is_moderate_plus(athlete_model)

    if phase == "GPP":
        # GPP priority (highest → lowest): primary_strength > aerobic > secondary_strength > recovery
        if role_key in {"primary_strength_day", "structural_strength_day"}:
            return 4
        if category == "conditioning" and (preferred_system == "alactic" or "alactic" in role_key) and goal_priority_scores(athlete_model)["speed"] >= 30:
            # Match placement's existing primary-speed reservation when the
            # total appointment budget is tighter than the available days.
            return 3
        if category == "conditioning" and preferred_system == "aerobic":
            return 3
        if role_key in {"aerobic_support_day", "aerobic_base_day", "aerobic_coordination_day"}:
            return 3
        if category == "strength":
            return 2
        if category == "recovery":
            return 1
        return 2  # other roles default to secondary strength level

    if phase == "SPP":
        # SPP priority (highest → lowest, normal): neural_plus > repeatability > fight_pace > recovery
        # With demote_glycolytic: fight_pace demoted to first-cut (rank 1), recovery promoted to rank 2
        if role_key == "neural_plus_strength_day":
            return 4
        if role_key == "fight_pace_repeatability_day" or (category == "conditioning" and preferred_system == "glycolytic"):
            return 1 if demote_glycolytic else 4
        if category == "conditioning" and preferred_system == "alactic":
            return 3
        if role_key == "repeatability_support_day" or (category == "conditioning" and preferred_system == "aerobic"):
            if demote_glycolytic and category == "conditioning" and preserve_low_noise and _is_low_noise_conditioning_role(role, athlete_model):
                return 4
            return 3
        if category == "recovery":
            return 2 if demote_glycolytic else 1
        if category == "strength":
            return 2  # secondary strength in SPP
        return 2  # other roles default

    # TAPER: alactic sharpness > aerobic support > glycolytic > recovery
    if category == "conditioning" and preferred_system == "alactic":
        return 4
    if category == "conditioning" and preferred_system == "aerobic":
        return 4 if preserve_low_noise and _is_low_noise_conditioning_role(role, athlete_model) else 3
    if category == "conditioning" and preferred_system == "glycolytic":
        return 1 if demote_glycolytic else 2
    if category == "recovery":
        return 1
    return 2


def reserve_dense_day_receiver(
    role_map: dict[str, Any], athlete_model: dict[str, Any]
) -> dict[str, Any]:
    """Offer a *provisional* free pre-contact appointment after main composition.

    Role survival initially happens before exercise selection. The post-selection
    budget owner may now see an actually dense strength day and offer a receiving
    slot it could not justify earlier. It never resurrects a suppressed role,
    increases the weekly appointment cap, or displaces another session.
    Composition must fund it with >=2 moved exercises or withdraw it.
    """
    from .calendar_context import contact_refs, role_d_day, build_events
    from .combat_load_policy import LoadClass
    from .physical_session_frequency import (
        requested_training_frequency, would_add_excess_training_day,
    )
    from .pre_hard_contact_strength import _power_microdose_eligible

    if not isinstance(role_map, dict) or not _power_microdose_eligible(athlete_model):
        return role_map
    if athlete_model.get("short_notice") or (
        athlete_model.get("compressed_priorities") or {}
    ).get("is_short_camp"):
        return role_map
    frequency = requested_training_frequency(athlete_model)
    if not frequency:
        return role_map
    allowed = {d.lower() for d in clean_list(athlete_model.get("training_days"))}
    if not allowed:
        return role_map
    contact_days = {
        ref.d_day for ref in contact_refs(role_map)
        if ref.profile.load_class is LoadClass.HARD_CONTACT
    }
    if not contact_days:
        return role_map

    for ordinal, week in enumerate(role_map.get("weeks") or [], start=1):
        if not isinstance(week, dict):
            continue
        if str(week.get("phase") or "").upper() not in {"GPP", "SPP"}:
            continue
        roles = week.get("session_roles") or []
        # No third strength exposure, no receiver if another strength appointment
        # survived the first role budget, and no overlapping proposed receivers.
        strengths = [r for r in roles if r.get("category") == "strength"]
        if len(strengths) != 1 or strengths[0].get("pre_hard_contact_managed_stress"):
            continue
        donor = strengths[0]
        if donor.get("late_fight_tail_owned"):
            continue
        members = donor.get("selected_exercise_assignments") or []
        if len(members) < 5:
            continue
        source_d = role_d_day(week, donor)
        if source_d is None or source_d < 14:
            continue
        days = {
            d["d_day"] for d in week.get("calendar_days") or []
            if isinstance(d, dict) and isinstance(d.get("d_day"), int)
        }
        occupied = {
            role_d_day(week, role) for role in roles
            if role_d_day(week, role) is not None
        }
        occupied.update(
            ref.d_day for ref in contact_refs(role_map) if ref.d_day in days
        )
        for entry in sorted(
            (d for d in week.get("calendar_days") or [] if isinstance(d, dict)),
            key=lambda d: -int(d.get("d_day") or 0),
        ):
            d_day = entry.get("d_day")
            day = str(entry.get("weekday") or "").strip()
            if (
                not isinstance(d_day, int) or d_day < 14
                or day.lower() not in allowed or d_day in occupied
                or d_day - 1 not in contact_days or d_day == source_d
            ):
                continue
            if would_add_excess_training_day(role_map, frequency, d_day):
                continue
            receiver = {
                "category": "strength",
                "role_key": "secondary_strength_day",
                "preferred_pool": "strength_slots",
                "strength_session_index": 2,
                "session_index": max(
                    (int(role.get("session_index") or 0) for role in roles), default=0
                ) + 1,
                "scheduled_day_hint": day,
                "scheduled_d_day": d_day,
                "scheduled_countdown_label": f"D-{d_day}",
                "countdown_label": f"D-{d_day}",
                "selected_exercise_assignments": [],
                "dense_day_receiver_provisional": True,
                "dense_day_receiver_token": f"{week.get('week_index')}:{d_day}",
            }
            # Shared policy owns the collision veto; the day-before-hard
            # DEPRIORITIZE tier remains conditional on the actual low-cost
            # composition and final realised-load revalidation.
            decision = evaluate_candidate_at_position(
                role_load_profile(receiver),
                candidate_position=-d_day,
                events=build_events(role_map),
                candidate_scope=week_scope(week, ordinal),
            )
            if decision.directive is PlacementDirective.FORBID:
                continue
            roles.append(receiver)
            week["intentionally_unused_days"] = [
                item for item in week.get("intentionally_unused_days") or []
                if str(item.get("day") or "").strip().lower() != day.lower()
            ]
            week["dense_day_receiver_proposal"] = {
                "source_day": donor.get("scheduled_day_hint"),
                "target_day": day,
                "source_selected_exercise_count": len(members),
                "collision_directive": decision.directive.value,
                "reason_code": "selected_main_session_density",
            }
            break
    return role_map


def enforce_total_session_budget(role_map: dict, athlete_model: dict, candidate_pools: dict | None = None) -> dict:
    """Report distinct training-day overruns without deleting selected work.

    Compatible double-session days count once. Calendar compatibility, dosage,
    and safety owners remain authoritative; any genuine excess day is reported
    rather than silently deleting selected or coach-owned work.
    """
    # This pass can run before and after repairs. Its notes describe the current
    # effective schedule; earlier budget findings must not survive a valid repair.
    for week in role_map.get("weeks") or []:
        if "coach_note_flags" in week:
            week["coach_note_flags"] = [
                flag for flag in week["coach_note_flags"]
                if not str(flag).startswith("Training day count discrepancy ")
            ]
    frequency = requested_training_frequency(athlete_model)
    if not frequency:
        return role_map
    slots = [(d["d_day"], d.get("weekday", ""))
             for w in role_map.get("weeks") or [] for d in w.get("calendar_days") or []]
    appointments = dated_appointments(role_map)
    for days in cadence_windows(slots):
        offsets = {d for d, _ in days}
        scheduled = [r for r in appointments if r["countdown_offset"] in offsets]
        training_days = {r["countdown_offset"] for r in scheduled}
        if len(training_days) > frequency:
            _append_week_coach_note_flag(scheduled[0]["_source_week"],
                f"Training day count discrepancy D-{max(offsets)}..D-{min(offsets)}: "
                f"{len(training_days)} physical training days versus {frequency} requested; "
                "compatible selected work retained, requested day count exceeded.")
    return role_map


def _build_spar_allocation_reason_codes(
    athlete_model: dict,
    compression: int,
    is_hard_spar_week: bool,
    is_meaningful_cut: bool,
) -> list[str]:
    reason_codes: list[str] = []
    fatigue = str(athlete_model.get("fatigue", "")).strip().lower()
    if fatigue == "high":
        reason_codes.append("high_fatigue")
    if is_hard_spar_week:
        reason_codes.append("two_hard_spar_days")
    if is_meaningful_cut:
        reason_codes.append("active_weight_cut")
    if _active_injury_is_moderate_plus(athlete_model):
        reason_codes.append("injury_management")
    if (fatigue == "high" or _active_injury_is_moderate_plus(athlete_model)) and (
        _proximity_amplifies_compression(
            athlete_model, cut_points=_cut_severity_compression_points(athlete_model)
        )
    ):
        reason_codes.append("proximity_to_fight")
    return reason_codes


def _is_boxing_crowded_anchor_role(role: dict[str, Any]) -> bool:
    return role.get("category") == "strength" and role.get("role_key") in PRIMARY_STRENGTH_ROLE_KEYS


def _is_boxing_crowded_low_load_support_role(role: dict[str, Any]) -> bool:
    if role.get("category") == "recovery":
        return True
    if role.get("category") == "conditioning" and role.get("preferred_system") == "aerobic":
        return True
    return str(role.get("role_key") or "").strip() in _CROWDED_LOW_LOAD_SUPPORT_ROLE_KEYS


def _boxing_crowded_week_policy_state(week_entry: dict, athlete_model: dict) -> dict[str, Any]:
    """Canonical boxing crowded-week role-budget policy state.

    This policy used to live in ``stage2_payload``. Step 10 moves it into the
    normal role-budget owner so payload post-processing can remain decoration-only.
    """
    declared_hard_days = _ordered_weekdays(clean_list(
        week_entry["effective_hard_sparring_days"]
        if "effective_hard_sparring_days" in week_entry
        else week_entry.get("declared_hard_sparring_days") or athlete_model.get("hard_sparring_days", [])
    ))
    training_days = _ordered_weekdays(clean_list(athlete_model.get("training_days", [])))
    fatigue = normalize_fatigue_level(athlete_model)
    meaningful_cut = _active_weight_cut_is_meaningful(athlete_model)
    injury_management = _boxing_crowded_week_injury_is_moderate_plus(athlete_model)
    days_until_fight = athlete_model.get("days_until_fight")

    risk_signals: list[str] = []
    if meaningful_cut:
        risk_signals.append("meaningful_weight_cut")
    if len(declared_hard_days) >= 3:
        risk_signals.append("high_spar_load")
    if injury_management:
        risk_signals.append("injury_management")
    if fatigue in {"moderate", "high"}:
        risk_signals.append(f"{fatigue}_fatigue")
    if len(training_days) <= 4 and len(declared_hard_days) >= 2:
        risk_signals.append("low_session_budget_high_combat_load")

    override_reason = ""
    if len(declared_hard_days) >= 4:
        override_reason = "four_hard_spar_days"
    elif fatigue == "high" and _active_weight_cut_is_declared(athlete_model):
        # Compound signal, not a cut penalty: high fatigue *together with* an
        # active cut earns crowded-week handling even when the cut on its own is
        # routine. Deliberately keyed on "a cut is declared" rather than on the
        # severity bucket, because the severity bucket governs how much capacity
        # a cut may remove by itself and must not be re-read as a second charge.
        override_reason = "high_fatigue_active_cut"

    is_boxing = _athlete_sport_key(athlete_model) == "boxing"
    late_fight_locked = isinstance(days_until_fight, int) and 0 <= days_until_fight <= 13
    short_notice_locked = bool(athlete_model.get("short_notice")) or bool(
        (athlete_model.get("compressed_priorities") or {}).get("is_short_camp")
    )
    active = is_boxing and not late_fight_locked and not short_notice_locked and (
        bool(override_reason) or len(risk_signals) >= 2
    )
    reason_codes = list(risk_signals)
    if override_reason:
        reason_codes.append(override_reason)

    return {
        "active": active,
        "policy": "boxing_crowded_week" if active else "",
        "risk_signals": risk_signals,
        "override_reason": override_reason,
        "reason_codes": reason_codes,
        "meaningful_cut": meaningful_cut,
        "fatigue": fatigue,
        "hard_spar_count": len(declared_hard_days),
        "training_day_count": len(training_days),
        "max_non_spar_roles": 2,
        "max_support_roles": 1,
        "standalone_glycolytic_allowed": False,
    }


def _boxing_crowded_week_summary(policy_state: dict[str, Any]) -> str:
    labels = [code.replace("_", " ") for code in policy_state.get("reason_codes", [])]
    context = ", ".join(labels) if labels else "crowded boxing week"
    return (
        f"Keep the week ruthlessly compressed under {context}: hard sparring owns the week, "
        "then one anchor, then one low-load support day max."
    )


def _boxing_crowded_role_priority(role: dict[str, Any], must_keep: set[str]) -> int:
    role_key = str(role.get("role_key") or "").strip()
    preferred_system = str(role.get("preferred_system") or "").strip()
    category = str(role.get("category") or "").strip()

    if (preferred_system in must_keep or role_key in must_keep
            or ("primary_strength" in must_keep and role_key in PRIMARY_STRENGTH_ROLE_KEYS
                and role.get("category") == "strength")):
        return 100
    if _is_boxing_crowded_anchor_role(role):
        return 5
    if _is_boxing_crowded_low_load_support_role(role):
        return 4
    if category == "conditioning" and preferred_system == "alactic":
        return 3
    if role_key in _CROWDED_OPTIONAL_ALACTIC_ROLE_KEYS:
        return 3
    if role_key == "fight_pace_repeatability_day" or (
        category == "conditioning" and preferred_system == "glycolytic"
    ):
        return 2
    if category == "strength":
        return 1
    return 2


def _select_boxing_crowded_week_non_spar_roles(
    non_spar_roles: list[dict[str, Any]],
    *,
    allowed_non_spar: int,
    must_keep: set[str],
) -> list[dict[str, Any]]:
    if allowed_non_spar <= 0 or not non_spar_roles:
        return []

    indexed_roles = list(enumerate(non_spar_roles))

    def _priority(item: tuple[int, dict[str, Any]]) -> tuple[int, int]:
        index, role = item
        return (_boxing_crowded_role_priority(role, must_keep), -index)

    selected: list[dict[str, Any]] = []
    anchor_candidates = [item for item in indexed_roles if _is_boxing_crowded_anchor_role(item[1])]
    support_candidates = [item for item in indexed_roles if _is_boxing_crowded_low_load_support_role(item[1])]

    if anchor_candidates:
        selected.append(max(anchor_candidates, key=_priority)[1])
        if allowed_non_spar > 1 and support_candidates:
            remaining_support = [item for item in support_candidates if item[1] not in selected]
            if remaining_support:
                selected.append(max(remaining_support, key=_priority)[1])
    elif support_candidates:
        selected.append(max(support_candidates, key=_priority)[1])

    return selected


def _apply_boxing_crowded_week_compression(
    week_entry: dict,
    session_roles: list[dict],
    suppressed_roles: list[dict],
    athlete_model: dict,
) -> tuple[list[dict], list[dict]]:
    """Apply the canonical boxing crowded-week role budget inside role-map ownership."""
    policy_state = _boxing_crowded_week_policy_state(week_entry, athlete_model)
    if not policy_state["active"]:
        return session_roles, suppressed_roles

    training_days = _ordered_weekdays(clean_list(athlete_model.get("training_days", [])))
    sessions_per_week = int(athlete_model.get("training_frequency", len(training_days) or len(session_roles)))
    weekly_cap = sessions_per_week

    spar_roles = [role for role in session_roles if role.get("role_key") == "hard_sparring_day"]
    non_spar_roles = [role for role in session_roles if role.get("role_key") != "hard_sparring_day"]
    non_spar_cap = max(0, weekly_cap - len(spar_roles))
    allowed_non_spar = min(non_spar_cap, policy_state["max_non_spar_roles"])

    resolved_rule_state = dict(week_entry.get("resolved_rule_state") or {})
    must_keep = set(clean_list(resolved_rule_state.get("must_keep", week_entry.get("must_keep", []))))

    kept_non_spar = _select_boxing_crowded_week_non_spar_roles(
        non_spar_roles,
        allowed_non_spar=allowed_non_spar,
        must_keep=must_keep,
    )

    kept_roles = spar_roles + kept_non_spar
    updated_suppressed = list(suppressed_roles)
    summary = _boxing_crowded_week_summary(policy_state)

    for role in non_spar_roles:
        if role in kept_non_spar:
            continue
        updated_suppressed.append(
            _make_compression_suppression(role, policy_state["reason_codes"], summary)
        )

    if not any(_is_boxing_crowded_anchor_role(role) for role in kept_non_spar):
        _append_week_coach_note_flag(week_entry, "anchor limited by constraints")

    has_recovery_in_kept = any(role.get("category") == "recovery" for role in kept_non_spar)
    week_entry["intentionally_unused_days"] = _compute_intentionally_unused_days(
        training_days,
        kept_roles,
        has_recovery_role=has_recovery_in_kept,
    )
    week_entry["intentional_compression"] = {
        "active": True,
        "policy": policy_state["policy"],
        "risk_signals": list(policy_state["risk_signals"]),
        "reason_codes": list(policy_state["reason_codes"]),
        "reason": ", ".join(policy_state["reason_codes"]),
        "summary": summary,
        "max_non_spar_roles": policy_state["max_non_spar_roles"],
        "max_support_roles": policy_state["max_support_roles"],
        "standalone_glycolytic_allowed": policy_state["standalone_glycolytic_allowed"],
    }
    return kept_roles, updated_suppressed


def _apply_high_fatigue_week_compression(
    week_entry: dict,
    session_roles: list[dict],
    suppressed_roles: list[dict],
    athlete_model: dict,
    *,
    hard_sparring_plan: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Spar-first weekly allocation:
    1. Count sparring against the weekly cap
    2. Apply readiness compression (fatigue, weight cut, injury, proximity) to non-sparring slots only
    3. Select only the highest-priority non-sparring roles up to non_spar_target
    4. Suppress excess roles and mark intentionally unused training days
    """
    week_entry["intentional_compression"] = _intentional_compression_stub()
    if not session_roles:
        return session_roles, suppressed_roles

    compressed = athlete_model.get("compressed_priorities") or {}
    if compressed.get("is_short_camp"):
        return session_roles, suppressed_roles

    training_days = _ordered_weekdays(clean_list(athlete_model.get("training_days", [])))
    if not training_days:
        # Without declared training days we cannot enforce the spar-first cap;
        # fall back to legacy single-role high-fatigue compression.
        return _apply_legacy_high_fatigue_compression(
            week_entry,
            session_roles,
            suppressed_roles,
            athlete_model,
            hard_sparring_plan=hard_sparring_plan,
        )

    # Structural rule: suppress a glycolytic conditioning session that has no legal
    # day in this week's contact structure — i.e. it is boxed in between two
    # effective hard spar days with no escape. This fires unconditionally — it is
    # not gated on fatigue or compression signals.
    #
    # Step 9B: the legality is the shared combat_load_policy's (queried through the
    # canonical calendar_context adapter on resolved contact state), not a duplicate
    # local ``sandwiched_training_days`` verdict. Because placement now leaves a
    # forbidden glycolytic role dayless rather than committing it to a between-hard
    # day, this checks whether *any* declared training day is legal for the role;
    # when none is, the owner suppresses it (its no-legal-slot contract). The owner
    # keeps only the role-budget scope — glycolytic conditioning outside
    # ``must_keep`` — and the suppression action.
    _effective_spar_days = set(effective_hard_days(hard_sparring_plan or []))
    if len(_effective_spar_days) >= 2:
        _resolved = dict(week_entry.get("resolved_rule_state") or {})
        _must_keep_early = set(clean_list(_resolved.get("must_keep", week_entry.get("must_keep", []))))
        _legality = normal_week_legality(
            hard_sparring_plan,
            clean_list(athlete_model.get("hard_sparring_days", [])),
            scope=week_scope(week_entry),
        )
        _training_days = _ordered_weekdays(clean_list(athlete_model.get("training_days", [])))

        def _has_no_legal_day(role: dict) -> bool:
            profile = classify_role(role)
            if profile is None:
                return False
            return _legality.best_legal_weekday(profile, _training_days) is None

        _kept: list[dict] = []
        for _role in session_roles:
            if (
                _role.get("category") == "conditioning"
                and _role.get("preferred_system") == "glycolytic"
                and _role.get("preferred_system") not in _must_keep_early
                and _has_no_legal_day(_role)
            ):
                suppressed_roles = list(suppressed_roles) + [
                    _make_compression_suppression(
                        _role,
                        ["sandwiched_hard_days"],
                        "Glycolytic session falls between two hard sparring days, so it is suppressed to protect recovery between hard contacts.",
                    )
                ]
            else:
                _kept.append(_role)
        session_roles = _kept

    # Step 10 ownership closure: the boxing crowded-week role budget is applied
    # here, inside the canonical normal role-budget owner, before generic weekly
    # compression. Payload post-processing may decorate the surviving roles but
    # cannot re-run or alter this decision.
    boxing_policy_state = _boxing_crowded_week_policy_state(week_entry, athlete_model)
    if boxing_policy_state["active"]:
        return _apply_boxing_crowded_week_compression(
            week_entry,
            session_roles,
            suppressed_roles,
            athlete_model,
        )

    # Step 1: Count sparring against the weekly cap
    hard_sparring_days_set = set(_ordered_weekdays(clean_list(athlete_model.get("hard_sparring_days", []))))
    sessions_per_week = int(athlete_model.get("training_frequency") or len(training_days))
    weekly_cap = sessions_per_week
    locked_spar_days = {day for day in training_days if day in hard_sparring_days_set}
    spar_count = len(locked_spar_days)
    non_spar_cap = max(0, weekly_cap - spar_count)

    # Separate sparring and non-sparring roles up front: the zero-non-spar
    # exemption below must be decided on combat work that actually exists in
    # this week, not on the intake declaration. Weekly capacity still follows
    # the declared lock, which is what reserves the day.
    spar_roles = [r for r in session_roles if r.get("role_key") == "hard_sparring_day"]
    non_spar_roles = [r for r in session_roles if r.get("role_key") != "hard_sparring_day"]

    # Step 2: Compute readiness compression score (applied to non-sparring slots only)
    fatigue = str(athlete_model.get("fatigue", "")).strip().lower()
    compression = _compute_readiness_compression(athlete_model)
    # No late-cut overlay here: ``_cut_severity_compression_points`` is the sole
    # cut-driven capacity charge. The former ``aggressive_cut_extra_compression``
    # re-read the same severity bucket and removed a *second* slot for it.
    #
    # Bounded against this week's actual capacity rather than a fixed ceiling,
    # so readiness compression can never take a declared training week to zero
    # physical sessions. Only a medical/fight-day authority may do that.
    compression_floor = _effective_compression_floor(
        athlete_model, non_spar_cap=non_spar_cap, combat_role_count=len(spar_roles)
    )

    # Step 3: Compute target number of non-sparring active sessions
    phase = str(week_entry.get("phase", "")).strip().upper()
    if phase in {"GPP", "SPP"}:
        min_non_spar_active = 1
    else:  # TAPER
        min_non_spar_active = 0

    if fatigue == "moderate":
        non_spar_target = non_spar_cap
    else:
        non_spar_target = max(min_non_spar_active, non_spar_cap - compression_floor)
    # Never exceed the available non-spar capacity
    non_spar_target = min(non_spar_target, non_spar_cap)

    conditioning_roles = [r for r in non_spar_roles if r.get("category") == "conditioning"]

    # Guardrail: if conditioning is a goal/weakness, keep space for it inside non-spar allocation.
    if conditioning_roles and _conditioning_limiter_signal(athlete_model):
        protected_conditioning_slots = 2 if sessions_per_week >= 5 else 1
        non_spar_target = max(non_spar_target, min(non_spar_cap, protected_conditioning_slots))

    current_non_spar_count = len(non_spar_roles)
    if current_non_spar_count <= non_spar_target:
        # Already within budget – populate intentionally unused days and return
        week_entry["intentionally_unused_days"] = _compute_intentionally_unused_days(
            training_days, session_roles, has_recovery_role=any(r.get("category") == "recovery" for r in non_spar_roles),
        )
        return session_roles, suppressed_roles

    # Step 4: Pick only the highest-priority non-sparring roles
    is_hard_spar_week = len(_effective_spar_days) >= 2
    is_meaningful_cut = _active_weight_cut_is_meaningful(athlete_model)

    resolved_rule_state = dict(week_entry.get("resolved_rule_state") or {})
    must_keep = set(clean_list(resolved_rule_state.get("must_keep", week_entry.get("must_keep", []))))

    def _base_rank(role: dict) -> int:
        return _non_spar_role_priority_rank(
            role,
            phase,
            is_hard_spar_week,
            is_meaningful_cut,
            must_keep,
            athlete_model,
        )

    ranked_roles = sorted(
        non_spar_roles,
        key=lambda r: allocation_sort_key(
            base_rank=_base_rank(r),
            role=r,
            athlete_model=athlete_model,
            dedicated_recovery=r.get("is_dedicated_recovery_mobility_day") is True,
        ),
        reverse=True,  # highest priority first
    )

    kept_non_spar = ranked_roles[:non_spar_target]
    dropped_non_spar = ranked_roles[non_spar_target:]

    reason_codes = _build_spar_allocation_reason_codes(athlete_model, compression, is_hard_spar_week, is_meaningful_cut)
    if not reason_codes:
        reason_codes = ["spar_first_cap"]
    summary = _compression_summary(reason_codes)

    kept_roles = spar_roles + kept_non_spar
    updated_suppressed = list(suppressed_roles)
    # Budget-driven suppressions record the reason but do not mark the
    # entry as ``intentional_compression``; that flag is reserved for
    # policy-driven compression (boxing crowded-week, short-camp,
    # fight-week override). See ``_make_compression_suppression`` for the
    # policy-flagged variant.
    for role in dropped_non_spar:
        updated_suppressed.append(
            {
                "category": role.get("category"),
                "role_key": role.get("role_key"),
                "preferred_system": role.get("preferred_system", ""),
                "reasons": [summary],
                "governance": dict(role.get("governance", {})),
                "reason_codes": list(reason_codes),
                "compression_reason_codes": list(reason_codes),
                "compression_summary": summary,
            }
        )

    # Step 5: Identify intentionally unused training days
    has_recovery_in_kept = any(r.get("category") == "recovery" for r in kept_non_spar)
    week_entry["intentionally_unused_days"] = _compute_intentionally_unused_days(
        training_days, kept_roles, has_recovery_role=has_recovery_in_kept,
    )

    # ``intentional_compression.active`` is reserved for policy-driven
    # compression (boxing crowded-week, short-camp, fight-week override).
    # Routine spar-first budget enforcement always trims the non-spar pool
    # to the weekly cap and would otherwise set the flag for every
    # over-allocated camp week — which is the default, not an intentional
    # compression. Record the suppression reason on the dropped roles
    # without flagging the week as intentionally compressed.
    week_entry["intentional_compression"] = {
        "active": False,
        "reason_codes": list(reason_codes),
        "reason": ", ".join(reason_codes),
        "summary": summary,
    }
    return kept_roles, updated_suppressed


def _compute_intentionally_unused_days(
    training_days: list[str],
    kept_roles: list[dict],
    *,
    has_recovery_role: bool,
) -> list[dict[str, str]]:
    """
    Return the training days that are not assigned to any kept role.
    Unused days become recovery_only_day if the week has no recovery bias yet,
    otherwise off_day.
    """
    used_days: set[str] = set()
    for role in kept_roles:
        day = str(role.get("scheduled_day_hint") or "").strip()
        if day:
            used_days.add(day)
    result = []
    for day in training_days:
        if day not in used_days:
            result.append({
                "day": day,
                "role": "off_day" if has_recovery_role else "recovery_only_day",
            })
    return result


def _apply_legacy_high_fatigue_compression(
    week_entry: dict,
    session_roles: list[dict],
    suppressed_roles: list[dict],
    athlete_model: dict,
    *,
    hard_sparring_plan: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Legacy single-role compression used when no declared training days are available."""
    effective_hard_count = effective_hard_day_count(hard_sparring_plan or []) if hard_sparring_plan else None
    reason_codes = _high_fatigue_compression_reason_codes(
        athlete_model,
        effective_hard_spar_count=effective_hard_count,
    )
    if not reason_codes:
        return session_roles, suppressed_roles

    declared_hard_days = _ordered_weekdays(
        clean_list(week_entry.get("declared_hard_sparring_days") or athlete_model.get("hard_sparring_days"))
    )
    resolved_rule_state = dict(week_entry.get("resolved_rule_state") or {})
    must_keep = set(clean_list(resolved_rule_state.get("must_keep", week_entry.get("must_keep", []))))
    training_days = _ordered_weekdays(clean_list(athlete_model.get("training_days", [])))
    effective_days = set(effective_hard_days(hard_sparring_plan or []))
    has_downgraded_declared_day = bool(declared_hard_days) and len(effective_days) < len(declared_hard_days)
    blocked_follow_on_days = _next_training_days_after_effective_hard_spar(training_days, effective_days)
    summary = _compression_summary(reason_codes)

    kept_roles = list(session_roles)
    updated_suppressed = list(suppressed_roles)

    if has_downgraded_declared_day:
        _append_week_coach_note_flag(week_entry, "deload hard sparring")

    sandwiched_days = sandwiched_training_days(training_days, effective_days)
    removable_role: dict[str, Any] | None = None
    glycolytic_role = next(
        (
            role for role in kept_roles
            if role.get("category") == "conditioning" and role.get("preferred_system") == "glycolytic"
        ),
        None,
    )
    if glycolytic_role is not None and glycolytic_role.get("preferred_system") not in must_keep:
        glycolytic_day = str(glycolytic_role.get("scheduled_day_hint") or "").strip()
        on_follow_on = glycolytic_day in blocked_follow_on_days and has_downgraded_declared_day
        on_sandwiched = glycolytic_day in sandwiched_days
        if on_follow_on or on_sandwiched:
            removable_role = glycolytic_role

    if removable_role is None:
        removable_role = next(
            (
                role for role in kept_roles
                if role.get("category") == "strength" and role.get("role_key") not in PRIMARY_STRENGTH_ROLE_KEYS
            ),
            None,
        )
    if removable_role is None:
        removable_role = next(
            (
                role for role in kept_roles
                if role.get("category") == "conditioning"
                and role.get("preferred_system") != "glycolytic"
                and role.get("preferred_system") not in must_keep
                and role.get("is_dedicated_recovery_mobility_day") is not True
            ),
            None,
        )
    if removable_role is None:
        removable_role = next(
            (
                role for role in kept_roles
                if role.get("category") == "conditioning"
                and role.get("preferred_system") not in must_keep
                and role.get("is_dedicated_recovery_mobility_day") is not True
            ),
            None,
        )
    if removable_role is None:
        recovery_roles = [role for role in kept_roles if role.get("category") == "recovery"]
        if len(recovery_roles) > 1:
            removable_role = recovery_roles[-1]

    if removable_role is None:
        return kept_roles, updated_suppressed

    kept_roles.remove(removable_role)
    updated_suppressed.append(_make_compression_suppression(removable_role, reason_codes, summary))

    week_entry["intentional_compression"] = {
        "active": True,
        "reason_codes": list(reason_codes),
        "reason": ", ".join(reason_codes),
        "summary": summary,
    }
    return kept_roles, updated_suppressed


# ---------------------------------------------------------------------------
# Combat pressure conditioning floor
# ---------------------------------------------------------------------------
#
# A proper fight camp needs at least one controlled hard combat-pressure
# conditioning exposure in safe build weeks. GPP is not just easy aerobic work
# and SPP is not just technical rhythm — a fighter has to touch discomfort
# before fight week. The floor GUARANTEES a single controlled hard exposure in
# safe GPP/SPP weeks, and blocks it whenever a real safety rule says the athlete
# should stay fresh (taper, D-14 conditioning lock, D-7/fight week, high
# fatigue, high+ cut, medical hold, restricted rehab, needs review, active
# injury, compression, bridge suppression).
#
# This is an execution-layer guarantee: it never overrides a baseline
# suppression. It only fills the gap when the athlete is safe to receive
# controlled suffering but the deterministic sequence left them with soft work.

_COMBAT_FLOOR_UNSAFE_FATIGUE = {"high", "critical", "extreme", "unsafe"}
_COMBAT_FLOOR_UNSAFE_CUT = {"high", "critical", "extreme", "unsafe"}
_COMBAT_FLOOR_BLOCKING_INJURY_MODES = {
    "medical_hold",
    "restricted_rehab_only",
    "needs_review",
}
# Conditioning roles that already represent a hard fight-pace / combat-pressure
# exposure. ``light_fight_pace_touch_day`` is intentionally excluded — it is the
# taper rhythm touch, not a hard exposure.
_COMBAT_FLOOR_HARD_PRESSURE_ROLE_KEYS = {
    "fight_pace_repeatability_day",
    "main_fight_pace_day",
    "highest_glycolytic_day",
    "controlled_repeatability_day",
}


def _week_min_d_day(week_entry: dict, athlete_model: dict) -> int | None:
    """Smallest (closest-to-fight) D-day covered by the week, if known."""
    d_days = [
        int(day.get("d_day"))
        for day in (week_entry.get("calendar_days") or [])
        if isinstance(day.get("d_day"), int)
    ]
    if d_days:
        return min(d_days)
    days = athlete_model.get("days_until_fight")
    return days if isinstance(days, int) else None


def _is_hard_pressure_conditioning_role(role: dict) -> bool:
    if str(role.get("category") or "") != "conditioning":
        return False
    if role.get("gas_tank_recovery_touch") or role.get("allowed_on_recovery_day"):
        # Recovery-day gas-tank flushes are low-noise, not a hard exposure.
        return False
    if str(role.get("preferred_system") or "").strip().lower() == "glycolytic":
        return True
    return str(role.get("role_key") or "") in _COMBAT_FLOOR_HARD_PRESSURE_ROLE_KEYS


def _bridge_allows_pressure_touch(athlete_model: dict, days: int) -> bool:
    """Defer to the bridge/late-taper rule set inside the D-21..D-14 window.

    If the baseline bridge rules already allow a single glycolytic touch (and
    the plan is not blocked) the floor may use a controlled pressure touch. If
    the bridge suppresses glycolytic work, the floor must not override it.
    """
    rules = compute_bridge_rules(
        days_until_fight=days,
        sport=athlete_model.get("sport", ""),
        style=athlete_model.get("style") or athlete_model.get("styles"),
        fatigue=athlete_model.get("fatigue") or athlete_model.get("fatigue_level") or "low",
        weight_cut_bucket=_resolved_cut_severity_bucket(athlete_model) or "none",
        injury_mode=athlete_model.get("injury_mode", "full_plan"),
        hard_sparring_days_declared=len(clean_list(athlete_model.get("hard_sparring_days", []))),
        athlete_model=athlete_model,
    )
    if rules.get("block_full_plan"):
        return False
    return int(rules.get("glycolytic_touch_max") or 0) >= 1


def _combat_pressure_floor_blockers(week_entry: dict, athlete_model: dict) -> list[str]:
    """Return the reason codes that block a hard combat-pressure exposure.

    An empty list means the athlete is safe to receive a controlled hard
    exposure this week.
    """
    phase = str(week_entry.get("phase", "")).upper()
    if phase not in {"GPP", "SPP"}:
        return ["floor_only_in_build_phase"]

    reasons: list[str] = []
    readiness = {
        str(flag).strip().lower().replace(" ", "_")
        for flag in clean_list(athlete_model.get("readiness_flags", []))
    }

    # Medical hold / restricted rehab / needs review — never force hard work.
    mode = str(athlete_model.get("injury_mode", "")).strip().lower()
    if mode in _COMBAT_FLOOR_BLOCKING_INJURY_MODES:
        reasons.append(f"injury_mode_{mode}")
    if readiness & {"medical_hold", "needs_review", "restricted_rehab", "restricted_rehab_only"}:
        reasons.append("injury_hold_flag")

    # High / critical / extreme fatigue — protect recovery, not punishment.
    fatigue = str(athlete_model.get("fatigue") or athlete_model.get("fatigue_level") or "").strip().lower()
    if fatigue in _COMBAT_FLOOR_UNSAFE_FATIGUE:
        reasons.append("high_fatigue")
    if readiness & {"high_fatigue", "critical_fatigue", "extreme_fatigue"}:
        reasons.append("high_fatigue_flag")

    # High+ weight cut suppresses hard density. Moderate/low/none does NOT.
    cut_bucket = _resolved_cut_severity_bucket(athlete_model)
    if cut_bucket in _COMBAT_FLOOR_UNSAFE_CUT:
        reasons.append(f"weight_cut_{cut_bucket}")
    if "aggressive_weight_cut" in readiness:
        reasons.append("aggressive_weight_cut")
    if athlete_model.get("unsafe_weight_flag"):
        reasons.append("unsafe_weight_flag")

    # Active injury that blocks hard work.
    if _active_injury_is_moderate_plus(athlete_model):
        reasons.append("active_injury_blocks_hard_work")

    compression = week_entry.get("intentional_compression") or {}
    if isinstance(compression, dict) and compression.get("active"):
        reasons.append("intentional_compression_blocks_hard_conditioning_floor")

    # Fight-week / taper freshness flags.
    if readiness & {"fight_week", "fight_day_protocol"}:
        reasons.append("fight_week_flag")

    # Countdown proximity: D-14 and closer blocks hard conditioning floor
    # fulfilment, while the global progression/regression lock remains D-10.
    # D-21..D-18 defers to the bridge glycolytic allowance, with extra
    # readiness gates for active cuts.
    min_d = _week_min_d_day(week_entry, athlete_model)
    if isinstance(min_d, int) and min_d >= 0:
        if min_d <= 14:
            reasons.append("late_conditioning_lock_d14")
            if min_d <= 13:
                reasons.append("late_taper_or_fight_week")
        elif 15 <= min_d <= 17:
            reasons.append("late_bridge_glycolytic_lock_d17_to_d15")
        elif 18 <= min_d <= 21:
            active_cut = bool(athlete_model.get("weight_cut_risk")) or "active_weight_cut" in readiness
            try:
                active_cut = active_cut or float(athlete_model.get("weight_cut_pct") or 0.0) > 0.0
            except (TypeError, ValueError):
                active_cut = True
            hard_sparring_declared = bool(clean_list(athlete_model.get("hard_sparring_days", [])))
            if cut_bucket in {"moderate", "high", "critical", "extreme"}:
                reasons.append("bridge_suppresses_glycolytic")
            elif fatigue in {"moderate", "high", "critical", "extreme"}:
                reasons.append("bridge_suppresses_glycolytic")
            elif hard_sparring_declared:
                reasons.append("bridge_suppresses_glycolytic")
            elif active_cut:
                reasons.append("active_cut_blocks_extra_conditioning_floor")
            elif not _bridge_allows_pressure_touch(athlete_model, min_d):
                reasons.append("bridge_suppresses_glycolytic")

    return dedupe_preserve_order(reasons)


def _combat_pressure_floor_metadata(phase: str) -> dict[str, Any]:
    """Coach-language dose/purpose/stop-rule for the hard exposure."""
    if str(phase).upper() == "SPP":
        return {
            "combat_pressure_floor": True,
            "mandatory_hard_conditioning_exposure": True,
            "prescribed_intensity_rpe": "8-9",
            "prescribed_dose": "4-6 x 2-3 min fight-pace on / 60 sec off @ RPE 8-9",
            "floor_purpose": (
                "Controlled fight-pace pressure exposure: repeat high output under "
                "fatigue, recover between rounds, tolerate lactate and decision "
                "pressure, and hold technique while breathing hard."
            ),
            "floor_stop_rule": (
                "Hard enough to breathe, not sloppy. Stop the round when output or "
                "technique clearly drops. This is pressure tolerance, not collapse."
            ),
        }
    return {
        "combat_pressure_floor": True,
        "mandatory_hard_conditioning_exposure": True,
        "prescribed_intensity_rpe": "8",
        "prescribed_dose": "6-8 x 60 sec hard / 60-90 sec easy @ RPE 8",
        "floor_purpose": (
            "Gas tank / repeatability touch: one controlled hard pressure exposure "
            "to build work capacity and pressure tolerance without sloppy collapse."
        ),
        "floor_stop_rule": (
            "Hard enough to breathe, not sloppy. Stop when output or technique "
            "drops. Controlled discomfort, not punishment."
        ),
    }


def _stamp_combat_pressure_floor(role: dict, phase: str) -> None:
    role.update(_combat_pressure_floor_metadata(phase))


def _pick_combat_floor_upgrade_target(
    session_roles: list[dict], must_keep: set[str]
) -> dict | None:
    """Choose a developmental conditioning role to make hard.

    The floor never removes a ``must_keep`` system's last instance (so a
    protected aerobic base survives), and it never hijacks a protective slot
    (recovery-day gas-tank flushes, converted low-load support, rehab-friendly
    touches). It prefers a spare aerobic slot, then an alactic slot, so a
    gas-tank / fight pace exposure can be added without dropping the base the
    plan wants to keep. When a must-keep system has more than one conditioning
    slot, a spare slot is still convertible because a protected instance remains.
    """
    system_counts: dict[str, int] = {}
    for role in session_roles:
        if str(role.get("category") or "") != "conditioning":
            continue
        system = str(role.get("preferred_system") or "").strip().lower()
        if system:
            system_counts[system] = system_counts.get(system, 0) + 1

    aerobic_targets: list[dict] = []
    alactic_targets: list[dict] = []
    for role in session_roles:
        if str(role.get("category") or "") != "conditioning":
            continue
        system = str(role.get("preferred_system") or "").strip().lower()
        role_key = str(role.get("role_key") or "")
        if role.get("gas_tank_recovery_touch") or role.get("allowed_on_recovery_day"):
            continue
        if "converted" in role_key or "recovery" in role_key or "mobility" in role_key:
            continue
        if system == "glycolytic":
            continue
        # Never convert the last instance of a must-keep system, but a spare
        # slot is fine when a protected instance of that system remains.
        if system in must_keep and system_counts.get(system, 0) <= 1:
            continue
        if system == "aerobic":
            aerobic_targets.append(role)
        elif system == "alactic":
            alactic_targets.append(role)
    if aerobic_targets:
        return aerobic_targets[0]
    if alactic_targets:
        return alactic_targets[0]
    return None


def _convert_role_to_combat_pressure(role: dict, phase: str) -> None:
    new_key = "fight_pace_repeatability_day" if str(phase).upper() == "SPP" else "controlled_repeatability_day"
    role["role_key"] = new_key
    role["preferred_system"] = "glycolytic"
    role["preferred_pool"] = "conditioning_slots"
    role["preferred_tags"] = dedupe_preserve_order(
        clean_list(role.get("preferred_tags", [])) + ["glycolytic", "fight_pace", "repeatability", "gas_tank"]
    )
    role["selection_rule"] = _role_selection_rule(new_key, "conditioning", "glycolytic")
    role["upgraded_from_combat_pressure_floor"] = True
    # Drop the soft aerobic label so the final label stamp resolves the
    # fight-pace label for the new role_key.
    role.pop("athlete_facing_label", None)
    _stamp_combat_pressure_floor(role, phase)


def _enforce_combat_pressure_floor(
    week_entry: dict,
    session_roles: list[dict],
    suppressed_roles: list[dict],
    athlete_model: dict,
) -> list[dict]:
    """Guarantee one controlled hard combat-pressure exposure in safe build weeks."""
    phase = str(week_entry.get("phase", "")).upper()
    from .conditioning_boxing import conditioning_load_context
    week_entry["conditioning_load_context"] = conditioning_load_context(
        athlete_model, {**week_entry, "session_roles": session_roles}
    )
    blockers = _combat_pressure_floor_blockers(week_entry, athlete_model)
    if blockers:
        week_entry["combat_pressure_floor"] = {"active": False, "reason_codes": blockers}
        return session_roles

    # Already have a hard exposure? Stamp it so the plan clearly shows the dose,
    # purpose, and stop rule — do not add a second one (dose stays controlled).
    existing = next(
        (role for role in session_roles if _is_hard_pressure_conditioning_role(role)),
        None,
    )
    if existing is not None:
        _stamp_combat_pressure_floor(existing, phase)
        week_entry["combat_pressure_floor"] = {
            "active": True,
            "source": "existing_role",
            "role_key": existing.get("role_key"),
        }
        return session_roles

    # A glycolytic role that the baseline hard-suppressed must stay suppressed —
    # the floor never overrides a baseline suppression.
    if any(
        str(entry.get("preferred_system") or "").strip().lower() == "glycolytic"
        or str(entry.get("role_key") or "") in _COMBAT_FLOOR_HARD_PRESSURE_ROLE_KEYS
        for entry in suppressed_roles
    ):
        week_entry["combat_pressure_floor"] = {
            "active": False,
            "reason_codes": ["baseline_suppresses_glycolytic"],
        }
        return session_roles

    # Keep the exposure separated from stacked collisions: if the week already
    # carries two or more effective hard-sparring days, skip the *added*
    # exposure rather than pile a hard session onto a saturated week.
    effective_hard = clean_list(week_entry.get("effective_hard_sparring_days", []))
    if len(effective_hard) >= 2:
        week_entry["combat_pressure_floor"] = {
            "active": False,
            "reason_codes": ["collision_saturated_week"],
        }
        return session_roles

    resolved_rule_state = dict(week_entry.get("resolved_rule_state", {}))
    must_keep = {
        str(token).strip().lower()
        for token in clean_list(
            resolved_rule_state.get("must_keep", week_entry.get("must_keep", []))
        )
    }
    target = _pick_combat_floor_upgrade_target(session_roles, must_keep)
    if target is None:
        week_entry["combat_pressure_floor"] = {
            "active": False,
            "reason_codes": ["no_convertible_slot_without_breaking_must_keep"],
        }
        return session_roles

    _convert_role_to_combat_pressure(target, phase)
    week_entry["combat_pressure_floor"] = {
        "active": True,
        "source": "upgraded_conditioning_slot",
        "role_key": target.get("role_key"),
    }
    return session_roles


def _build_weekly_role_map(
    athlete_model: dict,
    week_by_week_progression: dict,
    limiter_profile: dict,
    fight_week_override: dict[str, Any] | None = None,
) -> dict:
    weeks: list[dict] = []
    limiter_key = limiter_profile.get("key", "general_fight_readiness")
    progression_weeks = list(week_by_week_progression.get("weeks", []))
    projected_days_until_fight_start: list[int] = [0] * len(progression_weeks)
    projected_days_until_fight_end: list[int] = [0] * len(progression_weeks)
    week_span_days: list[int] = [0] * len(progression_weeks)
    # An open (undated) development block never ends on a fight: its calendar is
    # placed this many days clear of one, so no countdown band, fight-week rule
    # or fight-day override can ever apply to it. Dated camps carry 0.
    running_days = max(0, int(athlete_model.get("open_block_horizon_days") or 0))
    for idx in range(len(progression_weeks) - 1, -1, -1):
        span = max(0, int(progression_weeks[idx].get("span_days") or 0))
        week_span_days[idx] = span
        running_days += span
        # Anchor the camp so its final week ends ON the fight day (D-0), not the
        # day before it. ``running_days`` counts days from this week's start up to
        # and including the fight; the latest (smallest-d_day) day of the week is
        # therefore ``running_days - span`` and the earliest is ``running_days - 1``.
        # The previous ``+1`` offset ended the camp at D-1, which pushed the fight
        # weekday to D-7 in the final week and left no D-0 calendar day for the
        # fight-day override to clamp.
        projected_days_until_fight_start[idx] = max(0, running_days - 1)
        projected_days_until_fight_end[idx] = max(0, running_days - span) if span > 0 else 0
        fight_weekday = compute_fight_weekday(athlete_model)

    for week_idx, week_entry in enumerate(progression_weeks):
        calendar_days = build_calendar_days(
            fight_weekday=fight_weekday,
            projected_days_until_fight_end=projected_days_until_fight_end[week_idx],
            span_days=week_span_days[week_idx],
        )
        week_entry["calendar_days"] = calendar_days

        session_counts = dict(week_entry.get("session_counts") or {})
        conditioning_sequence = list(week_entry.get("conditioning_sequence", [])) or ["aerobic", "glycolytic", "alactic"]
        sport_key = _athlete_sport_key(athlete_model)
        if sport_key == "boxing" and week_entry.get("phase", "").upper() in {"GPP", "SPP"} and int(session_counts.get("conditioning", 0) or 0) >= 2:
            conditioning_sequence = _preferred_boxer_conditioning_sequence(
                week_entry.get("phase", ""),
                conditioning_sequence,
            )
        session_roles: list[dict] = []
        suppressed_roles: list[dict] = []
        session_index = 1

        for idx in range(max(0, int(session_counts.get("strength", 0)))):
            role_key = _strength_role_key(
                week_entry.get("phase", ""),
                week_entry.get("stage_key", ""),
                limiter_key,
                idx,
            )
            anchor = _role_anchor(role_key)
            governance = _role_governance(
                week_entry,
                category="strength",
                role_key=role_key,
                athlete_model=athlete_model,
                idx=idx,
            )
            if governance["hard_suppression_reasons"]:
                suppressed_roles.append(
                    {
                        "category": "strength",
                        "role_key": role_key,
                        "reasons": governance["hard_suppression_reasons"],
                        "governance": governance,
                    }
                )
                continue
            session_roles.append(
                {
                    "session_index": session_index,
                    "strength_session_index": idx + 1,
                    "category": "strength",
                    "role_key": role_key,
                    "preferred_pool": "strength_slots",
                    "selection_rule": _role_selection_rule(role_key, "strength"),
                    "anchor": anchor,
                    "placement_rule": _placement_rule_for_anchor(anchor, week_entry),
                    "governance": governance,
                }
            )
            session_index += 1

        conditioning_count = max(0, int(session_counts.get("conditioning", 0)))
        # When conditioning is a profile limiter but the brief has zero
        # conditioning sessions, add a single low-noise aerobic touch — unless
        # there is already a recovery slot that ``_upgrade_recovery_days_to_gas_tank``
        # can convert into the same gas-tank touch. Auto-adding on top of the
        # existing recovery slot would consume the weekly low-aerobic cap and
        # silently block the recovery-day upgrade the brief was actually
        # designed around.
        recovery_count = max(0, int(session_counts.get("recovery", 0)))
        if (
            conditioning_count == 0
            and recovery_count == 0
            and _conditioning_limiter_signal(athlete_model)
            and _can_keep_low_noise_conditioning(athlete_model)
        ):
            conditioning_count = 1
            conditioning_sequence = ["aerobic"] + [s for s in conditioning_sequence if s != "aerobic"]
        for idx in range(conditioning_count):
            system = conditioning_sequence[idx] if idx < len(conditioning_sequence) else conditioning_sequence[-1]
            role_key = _conditioning_role_key(week_entry.get("phase", ""), system, limiter_key)
            anchor = _role_anchor(role_key)
            governance = _role_governance(
                week_entry,
                category="conditioning",
                role_key=role_key,
                athlete_model=athlete_model,
                system=system,
                idx=idx,
            )
            if governance["hard_suppression_reasons"]:
                suppressed_roles.append(
                    {
                        "category": "conditioning",
                        "role_key": role_key,
                        "preferred_system": system,
                        "reasons": governance["hard_suppression_reasons"],
                        "governance": governance,
                    }
                )
                continue
            session_roles.append(
                {
                    "session_index": session_index,
                    "category": "conditioning",
                    "role_key": role_key,
                    "preferred_pool": "conditioning_slots",
                    "preferred_system": system,
                    "selection_rule": _role_selection_rule(role_key, "conditioning", system),
                    "anchor": anchor,
                    "placement_rule": _placement_rule_for_anchor(anchor, week_entry),
                    "governance": governance,
                }
            )
            session_index += 1

        for idx in range(max(0, int(session_counts.get("recovery", 0)))):
            role_key = _recovery_role_key(
                week_entry.get("phase", ""),
                week_entry.get("stage_key", ""),
                athlete_model,
            )
            anchor = _role_anchor(role_key)
            governance = _role_governance(
                week_entry,
                category="recovery",
                role_key=role_key,
                athlete_model=athlete_model,
                idx=idx,
            )
            session_roles.append(
                {
                    "session_index": session_index,
                    "category": "recovery",
                    "role_key": role_key,
                    "preferred_pool": "rehab_slots_or_recovery_only",
                    "selection_rule": _role_selection_rule(role_key, "recovery"),
                    "anchor": anchor,
                    "placement_rule": _placement_rule_for_anchor(anchor, week_entry),
                    "governance": governance,
                }
            )
            session_index += 1

        # Keep only planner-approved candidates for post-placement goal repair.
        # Hard-suppressed roles never enter this reservoir.
        goal_repair_candidates = deepcopy(session_roles)
        session_roles, suppressed_roles = _apply_short_camp_role_compression(
            week_entry,
            session_roles,
            suppressed_roles,
            athlete_model,
        )
        sparring_week = {
            "phase": week_entry.get("phase"),
            "stage_key": week_entry.get("stage_key"),
            "week_index": week_entry.get("week_index"),
            "phase_week_index": week_entry.get("phase_week_index"),
            "phase_week_total": week_entry.get("phase_week_total"),
            "projected_days_until_fight_start": projected_days_until_fight_start[week_idx],
            "projected_days_until_fight_end": projected_days_until_fight_end[week_idx],
            "span_days": week_span_days[week_idx],
            "fight_weekday": fight_weekday,
            "declared_hard_sparring_days": _ordered_weekdays(clean_list(athlete_model.get("hard_sparring_days", []))),
            "session_roles": session_roles,
        }
        hard_sparring_plan = compute_hard_sparring_plan(
            week=sparring_week,
            athlete_snapshot=athlete_model,
        )
        repeated_hard_sparring = repeated_weekday_hard_sparring_entries(
            week=sparring_week,
            athlete_snapshot=athlete_model,
        )
        effective_days = effective_hard_days(hard_sparring_plan)
        week_entry["hard_sparring_plan"] = hard_sparring_plan
        week_entry["effective_hard_sparring_days"] = list(effective_days)
        week_entry["intentional_compression"] = _intentional_compression_stub()
        week_entry["coach_note_flags"] = _dedupe_clean_strings(
            [
                flag
                for entry in hard_sparring_plan
                for flag in _hard_sparring_coach_note_flags(entry)
            ]
        )

        session_roles = _resequence_session_roles(
            week_entry,
            session_roles,
            athlete_model,
            hard_sparring_plan=hard_sparring_plan,
        )

        session_roles, suppressed_roles = _lock_declared_hard_sparring_roles(
            week_entry,
            session_roles,
            suppressed_roles,
            athlete_model,
            hard_sparring_plan=hard_sparring_plan,
        )

        # Let the budget owner see recovery slots that actually serve the
        # athlete's conditioning goal before it chooses the surviving roles.
        # The later pass still handles days freed by compression.
        session_roles = _upgrade_recovery_days_to_gas_tank(
            week_entry, session_roles, athlete_model, hard_sparring_plan=hard_sparring_plan,
        )
        session_roles, suppressed_roles = _apply_high_fatigue_week_compression(
            week_entry,
            session_roles,
            suppressed_roles,
            athlete_model,
            hard_sparring_plan=hard_sparring_plan,
        )
        
        # Gas-tank recovery/off-day upgrade must happen after compression,
        # because compression creates intentionally_unused_days.
        # It must happen before final locking/resequencing,
        # so the new aerobic roles are assigned and indexed properly.
        session_roles = _upgrade_recovery_days_to_gas_tank(
            week_entry,
            session_roles,
            athlete_model,
            hard_sparring_plan=hard_sparring_plan,
        )

        session_roles = _upgrade_unused_days_to_low_load_support(
            week_entry,
            session_roles,
            athlete_model,
            hard_sparring_plan=hard_sparring_plan,
        )

        # Combat pressure conditioning floor: guarantee one controlled hard
        # exposure in safe GPP/SPP build weeks. Runs after the soft-work
        # upgrades so it can see the final conditioning slots, and before the
        # final lock/resequence so the upgraded role is placed correctly.
        session_roles = _enforce_combat_pressure_floor(
            week_entry,
            session_roles,
            suppressed_roles,
            athlete_model,
        )

        session_roles, suppressed_roles = _lock_declared_hard_sparring_roles(
            week_entry,
            session_roles,
            suppressed_roles,
            athlete_model,
            hard_sparring_plan=hard_sparring_plan,
        )
        
        session_roles = _resequence_session_roles(
            week_entry,
            session_roles,
            athlete_model,
            hard_sparring_plan=hard_sparring_plan,
        )

        # Declared light-combat days are coach-owned immutable locks, just like
        # declared hard-sparring days. This runs after placement is final so it
        # reconciles whatever the allocator (including the gas-tank upgrades above)
        # left on a declared light-combat day: the light_combat_day slot survives,
        # and any app-owned S&C the combat-load policy forbids stacking there is
        # moved to a legal free day or dropped. No further resequence runs after
        # this, so nothing re-places an app role back onto the coach-owned day.
        session_roles, suppressed_roles = _lock_declared_light_combat_roles(
            week_entry,
            session_roles,
            suppressed_roles,
            athlete_model,
            hard_sparring_plan=hard_sparring_plan,
        )
        session_roles = session_roles + _repeated_weekday_hard_sparring_roles(
            week_entry, repeated_hard_sparring
        )
        session_roles = session_roles + _repeated_weekday_light_combat_roles(
            week_entry, athlete_model
        )

        calendar_days = list(week_entry.get("calendar_days") or [])
        d_day_by_weekday = {
            str(day.get("weekday") or "").strip().lower(): int(day.get("d_day"))
            for day in calendar_days
            if str(day.get("weekday") or "").strip() and isinstance(day.get("d_day"), int)
        }
        declared_support_days = {
            str(day).strip().lower()
            for day in clean_list(athlete_model.get("support_work_days") or athlete_model.get("technical_skill_days") or [])
            if str(day).strip()
        }
        for role in session_roles:
            weekday = str(role.get("scheduled_day_hint") or "").strip().lower()
            if (
                role.get("role_key") == "converted_low_aerobic_gas_tank_day"
                and weekday in declared_support_days
            ):
                role["role_key"] = "recovery_aerobic_gas_tank_day"
                role["gas_tank_recovery_touch"] = True
                role["priority_recovery_touch"] = True
            if not weekday or weekday not in d_day_by_weekday:
                continue
            # A role pinned to an earlier occurrence of a repeated weekday keeps
            # its date; the weekday map only knows the occurrence nearest the fight.
            if isinstance(role.get("scheduled_d_day"), int):
                continue
            d_day = d_day_by_weekday[weekday]
            role["scheduled_countdown_label"] = f"D-{d_day}"
            role["countdown_label"] = f"D-{d_day}"
        countdown_range = (
            [calendar_days[0]["d_day"], calendar_days[-1]["d_day"]] if calendar_days else []
        )
        weeks.append(
            {
                "week_index": week_entry.get("week_index"),
                "phase": week_entry.get("phase"),
                "stage_key": week_entry.get("stage_key"),
                "phase_week_index": week_entry.get("phase_week_index"),
                "phase_week_total": week_entry.get("phase_week_total"),
                "projected_days_until_fight_start": projected_days_until_fight_start[week_idx],
                "projected_days_until_fight_end": projected_days_until_fight_end[week_idx],
                "countdown_range": countdown_range,
                "calendar_days": calendar_days,
                "declared_training_days": _rotate_weekdays_from_plan_start(
                    clean_list(athlete_model.get("training_days", [])),
                    athlete_model.get("plan_creation_weekday"),
                ),
                "declared_hard_sparring_days": _ordered_weekdays(clean_list(athlete_model.get("hard_sparring_days", []))),
                "declared_support_work_days": _ordered_weekdays(clean_list(athlete_model.get("support_work_days", athlete_model.get("technical_skill_days", [])))),
                "declared_technical_skill_days": _ordered_weekdays(clean_list(athlete_model.get("technical_skill_days", []))),
                "hard_sparring_plan": hard_sparring_plan,
                "repeated_weekday_hard_sparring": repeated_hard_sparring,
                "effective_hard_sparring_days": list(effective_days),
                "final_week_sparring_cap": _final_week_sparring_cap_summary(hard_sparring_plan, list(effective_days)),
                "coach_note_flags": _dedupe_clean_strings(clean_list(week_entry.get("coach_note_flags", []))),
                "intentional_compression": dict(week_entry.get("intentional_compression") or _intentional_compression_stub()),
                "intentionally_unused_days": list(week_entry.get("intentionally_unused_days") or []),
                "combat_pressure_floor": dict(week_entry.get("combat_pressure_floor") or {"active": False}),
                "session_roles": session_roles,
                "suppressed_roles": suppressed_roles,
                "goal_repair_candidates": goal_repair_candidates,
            }
        )

    # Legacy fight_week_override compatibility now patches the relevant late week
    # instead of replacing the whole multi-week map.
    if fight_week_override and fight_week_override.get("active") and weeks:
        band = str(fight_week_override.get("band") or "")
        target_index = len(weeks) - 1
        week = dict(weeks[target_index])
        if band == "final_day_protocol":
            filtered_roles = []
        else:
            allowed_roles = set(clean_list(fight_week_override.get("allowed_session_roles", [])))
            max_sessions = int(fight_week_override.get("max_sessions") or 0)
            roles = list(week.get("session_roles") or [])
            filtered_roles = [role for role in roles if role.get("role_key") in allowed_roles]
            if max_sessions > 0:
                filtered_roles = filtered_roles[:max_sessions]
        week["session_roles"] = filtered_roles
        active_spar_days = {
            str(role.get("scheduled_day_hint") or "").strip().lower()
            for role in filtered_roles
            if role.get("role_key") == "hard_sparring_day" and str(role.get("scheduled_day_hint") or "").strip()
        }
        updated_hard_sparring_plan = []
        for entry in list(week.get("hard_sparring_plan") or []):
            day_key = str(entry.get("day") or "").strip().lower()
            if day_key in active_spar_days:
                updated_hard_sparring_plan.append(entry)
                continue
            updated_entry = dict(entry)
            reason_codes = list(clean_list(updated_entry.get("reason_codes", [])))
            if "fight_week_override" not in reason_codes:
                reason_codes.append("fight_week_override")
            reason = str(updated_entry.get("reason") or "").strip()
            override_reason = str(fight_week_override.get("coach_note") or "fight-week override active")
            updated_entry.update(
                {
                    "status": "suppressed",
                    "effective_load": "none",
                    "reason_codes": reason_codes,
                    "reason": f"{reason}; {override_reason}" if reason else override_reason,
                }
            )
            updated_hard_sparring_plan.append(updated_entry)
        week["hard_sparring_plan"] = updated_hard_sparring_plan
        week["effective_hard_sparring_days"] = effective_hard_days(updated_hard_sparring_plan)
        suppressed_roles = list(week.get("suppressed_roles") or [])
        suppressed_roles.append(
            {
                "category": "plan",
                "role_key": "fight_week_override",
                "reasons": [str(fight_week_override.get("coach_note") or "fight-week override active")],
            }
        )
        week["suppressed_roles"] = suppressed_roles
        week["coach_note_flags"] = _dedupe_clean_strings(
            clean_list(week.get("coach_note_flags", [])) + ["fight-week override active"]
        )
        week["intentional_compression"] = {
            "active": True,
            "reason_codes": ["fight_week_override"],
            "reason": "fight_week_override",
            "summary": str(fight_week_override.get("coach_note") or "fight-week override active"),
        }
        weeks[target_index] = week

    weekly_role_map = {
        "model": "session_role_overlay.v1",
        "source_of_truth": [
            "Session roles inherit week-by-week progression rather than replacing phase logic.",
            "Session counts come from existing deterministic phase session allocation.",
            "Anchors inherit the weekly stress map so phase guardrails, safety, and sport-load rules keep priority.",
            "Weekly roles are an execution layer only and cannot overrule the planning hierarchy.",
        ],
        "fight_week_override": fight_week_override or {"active": False},
        "weeks": weeks,
    }
    weekly_role_map = apply_fight_day_override_to_weekly_role_map(weekly_role_map, athlete_model)
    # Stamp deterministic athlete-facing labels so Stage 1 owns the session
    # titles instead of leaving them for the Stage 2 LLM to invent. Run last so
    # roles injected by the fight-day override are labelled too.
    return stamp_weekly_role_map_labels(weekly_role_map)
