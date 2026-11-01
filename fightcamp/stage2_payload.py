"""Stage 2 payload assembly, candidate pools, finalizer prompt, and handoff text.

Internal implementation is split across:
  - stage2_planning_brief  — athlete model, phases, limiter, sport load
  - stage2_role_map        — week progression, role slots, compression

Everything is re-exported here so external callers don't need to change
their import paths.
"""
from __future__ import annotations

import json
import re
import sys
from copy import deepcopy
from typing import Any

from .exercise_identity import normalize_exercise_key, stamp_exercise_key
from . import stage2_planning_brief as stage2_planning_brief_module
from .stage2_finalizer_packet import build_stage2_finalizer_packet
from .stage2_llm_boundary import build_stage2_llm_planning_brief
from .stage2_payload_open_ongoing import (
    _uses_open_ongoing_payload,
    build_open_ongoing_payload,
)
from . import stage2_role_map as stage2_role_map_module
from .render_authority import (
    AUTHORITY_CLOSED_SELECTED_ASSIGNMENTS,
    authoritative_render_for_role,
)
from .stage2_payload_late_fight import (  # noqa: F401  (re-exported for tests/back-compat)
    CANONICAL_HARD_SPARRING_BAN_LABEL,
    CANONICAL_HARD_SPARRING_LABEL,
    CANONICAL_HARD_SPARRING_NOTE,
    CANONICAL_TECHNICAL_ONLY_NOTE,
    _build_late_fight_plan_spec,
    _build_late_fight_session_sequence,
    _build_late_fight_week_by_week_progression,
    _build_late_fight_weekly_role_map,
    _camp_downstream_countdown_continuation,
    _days_out_payload_block,
    _days_out_payload_mode,
    _fight_week_override_payload,
    _handoff_mode_instructions,
    _is_app_owned_visible_role,
    _late_fight_permissions,
    _late_fight_rendering_rules,
    _visible_calendar_session_sequence,
    _resolve_late_fight_phase,
    _uses_late_fight_stage2_payload,
    ensure_declared_coach_combat_spine,
    is_low_cost_coexistable_filler,
)
from .gap_fill_inserts import apply_gap_fill_inserts
from .physical_occupancy_fill import complete_late_fight_sequence_occupancy
from .conditioning import athlete_facing_system_label, technical_footwork_prescription_fields
from .fight_day_override import apply_fight_day_override_to_weekly_role_map
from .role_labels import PRIMARY_STRENGTH_ROLE_KEYS, stamp_weekly_role_map_labels
from .camp_week_fillers import apply_camp_week_fillers
from .bank_authority import original_bank_entries
from .bank_schema import has_meaningful_fulfillment_authority
from .late_camp_role_morph import apply_late_camp_role_morph
from .prescription_resolver import apply_effective_strength_prescriptions
from .session_composition import (
    _conditioning_prescription,
    _refresh_stress_stamps,
    _prescription_total_reps,
    _stress_stamp,
    option_mechanical_risk_tags,
    prescription_dose_known,
    realised_session_stress,
    _selected_coaching_notes,
    _selected_coaching_fields,
    attach_late_fight_assignments,
    apply_realised_load_calendar_revalidation,
    apply_standalone_strength_minimum,
    compose_normal_conditioning_assignments,
    compose_normal_rehab_assignments,
    compose_normal_strength_assignments,
    reconcile_relocated_strength,
    redistribute_pre_hard_power_from_main,
    rollback_invalid_dense_day_redistributions,
)
from .normal_calendar_placement import fill_missing_session_days
from .late_fight_phase_eligibility import (
    governed_build_planning_brief,
    governed_late_fight_allowed_exercises_by_day,
)
from .late_selector_windows import (
    _normalise_late_window_tokens,
    classify_late_selector_window,
    late_window_allowed,
    late_windows_spanned,
)
from .normalization import (  # noqa: F401  (phrase_in_text re-exported for back-compat)
    clean_list,
    dedupe_preserve_order,
    normalize_fatigue_level,
    normalize_text,
    phrase_in_text,
    slugify,
)
from .rehab_protocols import (
    _FUNCTION_LABELS,
    _is_surface_type,
    classify_drill_function,
    rehab_drill_options_for_phase,
)
from .restriction_parsing import CANONICAL_RESTRICTIONS  # noqa: F401  (re-exported for back-compat)
from .priority_profile import build_priority_profile, describe_priority_focus
from .selection_metadata import build_score_evidence, normalize_selection_metadata
from .stage2_render_guards import (  # noqa: F401  (re-exported for backwards compatibility)
    _NO_ACTIVE_INJURY_MARKERS,
    _all_active_injuries_surface_only,
    _append_render_guard_writing_rules,
    _has_active_injury_from_athlete_model,
    _has_active_injury_from_training_context,
    _meaningful_injury_values,
    _render_guard_flags,
)
from .strength_session_quality import (
    classify_strength_item,
    infer_strength_sessions,
    strength_adaptation_relevance,
)
from .training_context import TrainingContext, allocate_sessions
from .nutrition import compute_nutrition_targets
from .mindset_module import compute_mindset_plan
from .injury_awareness import build_injury_awareness_support
from .weight_cut import (  # noqa: F401  (re-exported for back-compat)
    compute_cut_severity_score,
    cut_severity_bucket,
)

# Re-export from sub-modules for backward compatibility. Names that are also
# defined locally further down this file are intentionally omitted here because
# the local definitions are the canonical ones — they shadow the imports at
# execution time, so listing them under "re-export" only triggers F811 noise.
from .stage2_planning_brief import (  # noqa: F401
    CONDITIONING_ROLE_PURPOSES,
    PHASE_CONDITIONING_PRIORITY,
    PHASE_DEPRIORITIZE,
    PHASE_EMPHASIS,
    PHASE_OBJECTIVES,
    PHASE_SELECTION_GUARDRAILS,
    PLANNING_DECISION_HIERARCHY,
    RESTRICTION_PATTERN_HINTS,
    _MECHANICAL_TAG_PREFIXES,
    _MECHANICAL_TAGS,
    _RESTRICTION_CANONICAL_KEYS,
    _LIMITER_PROFILES,
    _SPORT_LOAD_PROFILES,
    _UNKNOWN_COMPETITIVE_MATURITY,
    _WEEKLY_STAGE_TEMPLATES,
    _build_athlete_model,
    _build_limiter_profile,
    _build_phase_selection_guardrails,
    _build_sport_load_profile,
    _build_weekly_stress_map,
    _conditioning_slot_priority,
    _derive_athlete_archetype,
    _derive_competitive_maturity,
    _derive_main_limiter,
    _derive_main_risks,
    _derive_readiness_flags,
    _downgrade_priority,
    _extract_mechanical_risk_tags,
    _extract_restriction_tags,
    _is_high_pressure_weight_cut,
    _normalize_limiter_tokens,
    _parse_record,
    _primary_sport_load_key,
    _priority_bucket,
    _priority_bucket_labels,
    _priority_value,
    _resolve_phase_rule_state,
    _serialize_restrictions,
    _strength_slot_priority,
)
from .stage2_role_map import (  # noqa: F401
    enforce_total_session_budget,
    _append_day_hint,
    _athlete_sport_key,
    _apply_short_camp_role_compression,
    _build_week_by_week_progression,
    _compression_floor_value,
    _compression_summary,
    _compute_intentionally_unused_days,
    _final_week_sparring_cap_summary,
    _hard_sparring_coach_note_flags,
    _hard_sparring_role,
    _high_fatigue_compression_reason_codes,
    _make_compression_suppression,
    _make_final_week_sparring_cap_suppression,
    _make_hard_sparring_lock_suppression,
    _next_training_days_after_effective_hard_spar,
    _phase_progression_slot_count,
    _placement_rule_for_anchor,
    _preferred_boxer_conditioning_sequence,
    _progression_templates_for_phase,
    _replaceable_role_priority,
    _resequence_session_roles,
    _role_governance,
    _rotate_weekdays_from_plan_start,
    _short_camp_priority_catalog,
    _strength_role_key,
    _conditioning_role_key,
    _split_phase_days,
)







def _clean_list(values) -> list[str]:
    if values is None:
        return []
    if isinstance(values, list):
        return [str(value).strip() for value in values if str(value).strip()]
    if isinstance(values, str):
        return [values.strip()] if values.strip() else []
    return [str(values).strip()]


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result





def _compress_short_camp_priorities(athlete_model: dict) -> dict:
    return stage2_planning_brief_module._compress_short_camp_priorities(athlete_model)


def late_fight_sequence_budget(
    session_sequence: list[dict], base_role_budget: dict | None
) -> dict[str, Any]:
    """Session counts a late-fight spec derives from its visible sequence.

    One owner for the builder and for any later pass that removes roles, so the
    counts can never describe sessions the sequence no longer holds.
    """
    app_sequence = [
        role
        for role in session_sequence
        if isinstance(role, dict) and _is_app_owned_visible_role(role.get("role_key"))
    ]
    return {
        "role_budget": {
            **dict(base_role_budget or {}),
            "selected_active_roles": sum(
                1 for role in app_sequence if not is_low_cost_coexistable_filler(role)
            ),
            "selected_meaningful_stress_exposures": sum(
                1 for role in app_sequence if role.get("stress_class") == "meaningful_stress"
            ),
            "selected_support_roles": sum(
                1
                for role in app_sequence
                if role.get("stress_class") == "support"
                and not is_low_cost_coexistable_filler(role)
            ),
        },
        "visible_session_cap": len(app_sequence),
        "max_active_roles": len(app_sequence),
        "visible_session_roles": [role.get("role_key") for role in app_sequence],
    }


def _record_late_fight_unused_days(
    weekly_role_map: dict, unused_days: list[dict]
) -> None:
    """Carry occupancy-completion reasons onto the late-fight week they belong to."""
    if not unused_days or not isinstance(weekly_role_map, dict):
        return
    weeks = [week for week in weekly_role_map.get("weeks", []) or [] if isinstance(week, dict)]
    if not weeks:
        return
    for record in unused_days:
        offset = record.get("countdown_offset")
        target = weeks[-1]
        for week in weeks:
            span = week.get("countdown_span")
            if not isinstance(span, dict) or not isinstance(offset, int):
                continue
            start, end = span.get("start_day"), span.get("end_day")
            if isinstance(start, int) and isinstance(end, int) and end <= offset <= start:
                target = week
                break
        target.setdefault("intentionally_unused_days", []).append(record)


def _build_phase_briefs(training_context: TrainingContext, phase_weeks: dict) -> dict[str, dict]:
    briefs: dict[str, dict] = {}
    for phase in ("GPP", "SPP", "TAPER"):
        if phase_weeks.get(phase, 0) <= 0 and phase_weeks.get("days", {}).get(phase, 0) < 1:
            continue
        # One capping authority for both phase-brief builders: ``allocate_sessions``
        # returns candidate role capacity (frequency + 1), which must never reach
        # the weekly calendar as a physical-session count.
        session_counts = stage2_planning_brief_module._cap_session_counts_to_frequency(
            allocate_sessions(training_context.training_frequency, phase),
            training_context,
        )
        risk_flags: list[str] = []
        if training_context.injuries:
            risk_flags.append("respect injury guardrails")
        if training_context.weight_cut_risk:
            risk_flags.append("manage cut stress")
        if training_context.fatigue in {"moderate", "high"}:
            risk_flags.append("manage accumulated fatigue")
        briefs[phase] = {
            "objective": PHASE_OBJECTIVES.get(phase, ""),
            "emphasize": PHASE_EMPHASIS.get(phase, []),
            "deprioritize": PHASE_DEPRIORITIZE.get(phase, []),
            "risk_flags": _dedupe_preserve_order(risk_flags),
            "session_counts": session_counts,
            "selection_guardrails": _build_phase_selection_guardrails(phase, training_context),
            "weeks": phase_weeks.get(phase, 0),
            "days": phase_weeks.get("days", {}).get(phase, 0),
        }
    return briefs













_LOW_LOAD_SUPPORT_ROLE_KEYS = {
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
_CROWDED_ANCHOR_FORBIDDEN_TOKENS = [
    "standalone_glycolytic",
    "hinge_transfer",
    "contrast_work",
    "jumps",
    "sharpness_touch",
    "hard_sparring",
]
_CROWDED_SUPPORT_FORBIDDEN_TOKENS = [
    "primary_strength_anchor",
    "standalone_glycolytic",
    "hinge_transfer",
    "contrast_work",
    "jumps",
    "sharpness_touch",
    "hard_sparring",
]
def _normalized_fatigue_level(athlete_model: dict) -> str:
    return normalize_fatigue_level(athlete_model)


def _is_anchor_role(role: dict[str, Any]) -> bool:
    return role.get("category") == "strength" and role.get("role_key") in PRIMARY_STRENGTH_ROLE_KEYS


def _is_low_load_support_role(role: dict[str, Any]) -> bool:
    if role.get("category") == "recovery":
        return True
    if role.get("category") == "conditioning" and role.get("preferred_system") == "aerobic":
        return True
    return str(role.get("role_key") or "").strip() in _LOW_LOAD_SUPPORT_ROLE_KEYS


def _main_job_for_role(role: dict[str, Any]) -> str:
    role_key = str(role.get("role_key") or "").strip()
    if role_key == "hard_sparring_day":
        return "hard_sparring"
    if _is_anchor_role(role):
        return "anchor"
    if _is_low_load_support_role(role):
        return "support_recovery"
    if role.get("category") == "conditioning":
        return "conditioning"
    return role_key or str(role.get("category") or "").strip()


def _apply_day_identity_governance(role: dict[str, Any], *, crowded_week_active: bool) -> None:
    governance = dict(role.get("governance") or {})
    main_job = _main_job_for_role(role)
    governance["main_job"] = main_job

    if crowded_week_active and main_job == "anchor":
        governance["support_cap"] = "light_only"
        governance["forbidden_secondary_stressors"] = list(_CROWDED_ANCHOR_FORBIDDEN_TOKENS)
    elif crowded_week_active and main_job == "support_recovery":
        governance["support_cap"] = "light_only"
        governance["forbidden_secondary_stressors"] = list(_CROWDED_SUPPORT_FORBIDDEN_TOKENS)
    else:
        governance.setdefault("support_cap", "")
        governance.setdefault("forbidden_secondary_stressors", [])

    role["governance"] = governance




def _is_meaningful_stressor(role: dict[str, Any]) -> bool:
    role_key = str(role.get("role_key") or "").strip()
    if role_key in {"main_conditioning_stressor", "fight_pace_block", "full_neural_session"}:
        return True
    main_job = _main_job_for_role(role)
    if main_job in {"hard_sparring", "anchor"}:
        return True
    if main_job == "conditioning":
        system = str(role.get("preferred_system") or "").strip().lower()
        if system == "glycolytic":
            return True
        if role_key in {
            "fight_pace_repeatability_day",
            "light_fight_pace_touch_day",
        }:
            return True
    load_tokens = {
        str(token).strip().lower()
        for token in (
            list(role.get("stress_flags") or [])
            + list(role.get("load_flags") or [])
            + list(role.get("tags") or [])
            + list((role.get("governance") or {}).get("load_flags") or [])
        )
        if str(token).strip()
    }
    if {"high_cns", "high_neural", "high_metabolic", "high_load", "main_conditioning_stressor"} & load_tokens:
        return True
    return False




def _active_injury_affects_generic_compression(athlete_model: dict) -> bool:
    """True when the generic readiness layer should count injury pressure."""
    # A stable surface/skin-only injury is a hygiene note, not injured tissue —
    # it must not add compression pressure.
    if _all_active_injuries_surface_only(athlete_model):
        return False
    if athlete_model.get("injuries"):
        return True
    readiness_flags = set(_clean_list(athlete_model.get("readiness_flags", [])))
    return "injury_management" in readiness_flags


def _active_injury_is_moderate_plus(athlete_model: dict) -> bool:
    """True when the boxing crowded-week trigger sees moderate+ injury pressure."""
    # A stable surface/skin-only injury never counts as moderate+ tissue pressure.
    if _all_active_injuries_surface_only(athlete_model):
        return False
    injuries = _clean_list(athlete_model.get("injuries", []))
    readiness_flags = set(_clean_list(athlete_model.get("readiness_flags", [])))
    if readiness_flags & {"injury_management", "moderate_injury", "significant_injury", "severe_injury"}:
        return True
    for entry in injuries:
        lowered = entry.lower()
        if any(token in lowered for token in ("moderate", "severe", "major", "significant", "grade 2", "grade ii", "grade 3", "grade iii")):
            return True
    return False


def _build_weekly_role_map(
    athlete_model: dict,
    week_by_week_progression: dict,
    limiter_profile: dict,
    fight_week_override: dict[str, Any] | None = None,
) -> dict:
    """Compatibility wrapper for the single weekly role-map implementation."""
    return stage2_role_map_module._build_weekly_role_map(
        athlete_model,
        week_by_week_progression,
        limiter_profile,
        fight_week_override=fight_week_override,
    )


def _derive_global_priorities(
    athlete_model: dict,
    phase_briefs: dict[str, dict],
    candidate_pools: dict[str, dict],
) -> dict[str, list[str]]:
    preserve: list[str] = []
    push: list[str] = []
    avoid: list[str] = []

    injuries = _clean_list(athlete_model.get("injuries", []))
    goals = _clean_list(athlete_model.get("key_goals", []))
    hard_sparring_days = _clean_list(athlete_model.get("hard_sparring_days", []))
    support_work_days = _clean_list(
        athlete_model.get("support_work_days", athlete_model.get("technical_skill_days", []))
    )
    high_pressure_cut = _is_high_pressure_weight_cut(athlete_model=athlete_model)
    compressed = athlete_model.get("compressed_priorities") or {}
    primary_labels = _priority_bucket_labels(compressed.get("primary_targets", []))
    maintenance_labels = _priority_bucket_labels(compressed.get("maintenance_targets", []))
    embedded_labels = _priority_bucket_labels(compressed.get("embedded_support", []))
    deferred_labels = _priority_bucket_labels(compressed.get("deferred", []))

    if injuries:
        preserve.append("Keep rehab continuity and remove only clearly conflicting work.")
        avoid.append("Do not keep drills that mechanically overlap the injured pattern just because they sound different.")
    if athlete_model.get("weight_cut_risk"):
        preserve.append("Keep recovery spacing and low-damage conditioning alive while cut stress is active.")
        preserve.append("Protect strength and speed quality by keeping fueling support around key sessions.")
        avoid.append("Avoid unnecessary soreness-heavy conditioning, glycolytic density, or accessory volume during the cut.")
        if high_pressure_cut:
            preserve.append("Preserve freshness first when cut pressure is high.")
            avoid.append("Do not spend cut margin on optional fatigue that does not directly support the fight.")
    if "conditioning" in goals:
        push.append("Prioritize conditioning slots that match the phase objective before extra accessories.")
    if "power" in goals:
        push.append("Preserve explosive and alactic work if compliant options remain.")
    if athlete_model.get("weight_cut_risk"):
        push.append("Choose the crispest high-value work and trim optional fatigue before it blunts strength expression or conditioning tolerance.")
    if hard_sparring_days:
        preserve.append("Let declared hard sparring own the highest collision combat load before adding extra glycolytic stress.")
        push.append("Keep the primary neural strength day away from declared hard sparring when a cleaner weekly placement exists.")
        avoid.append("Do not stack the main glycolytic stressor directly beside declared hard sparring unless the schedule truly forces it.")
    if support_work_days:
        preserve.append("Use declared support work days for lower-noise support work when the weekly rhythm needs a lighter combat touch.")
    if compressed.get("is_short_camp"):
        preserve.append(
            f"Keep the week selective by driving sessions from {', '.join(primary_labels)} and at most one maintenance target."
        )
        avoid.append("Do not turn every selected goal or weakness into its own session objective inside a short camp.")
        if maintenance_labels:
            push.append(f"Keep {maintenance_labels[0]} to one small exposure instead of a full extra emphasis day.")
        if embedded_labels:
            avoid.append(f"Treat {', '.join(embedded_labels)} as embedded support through warm-up, recovery, or drill selection.")
        if deferred_labels:
            avoid.append(f"Defer {', '.join(deferred_labels)} as standalone objectives in this short window.")

    for phase, brief in phase_briefs.items():
        guardrails = brief.get("selection_guardrails", {})
        for item in guardrails.get("must_keep_if_present", []):
            label = str(item).replace("_", " ")
            preserve.append(f"In {phase}, keep {label} work if a compliant version exists.")
        for note in guardrails.get("notes", []):
            avoid.append(str(note))

    conditioning_roles = {
        slot.get("role")
        for pool in candidate_pools.values()
        for slot in pool.get("conditioning_slots", [])
        if slot.get("role")
    }
    if "aerobic" in conditioning_roles and "conditioning" in goals:
        push.append("Use aerobic work to support recovery and repeatability, not just to add volume.")
    if "alactic" in conditioning_roles:
        push.append("Keep at least one neural-speed option when the phase or taper calls for sharpness.")

    return {
        "preserve": dedupe_preserve_order(preserve) or ["Preserve the main phase objectives and any active rehab work."],
        "push": dedupe_preserve_order(push) or ["Push the highest-priority phase qualities first."],
        "avoid": dedupe_preserve_order(avoid) or ["Avoid changes that break the phase intent or restriction logic."],
    }


def _resolve_visible_phase_framing(phase: str, brief: dict, week_by_week_progression: dict) -> dict[str, str]:
    weeks = [
        week
        for week in (week_by_week_progression.get("weeks", []) or [])
        if week.get("phase") == phase
    ]
    if len(weeks) != 1:
        return {
            "label": phase,
            "objective": brief.get("objective", ""),
        }

    week = weeks[0]
    return {
        "label": week.get("stage_label") or phase,
        "objective": week.get("stage_objective") or brief.get("objective", ""),
    }


def _build_phase_strategy(
    phase_briefs: dict[str, dict],
    candidate_pools: dict[str, dict],
    week_by_week_progression: dict,
) -> dict[str, dict]:
    strategy: dict[str, dict] = {}
    for phase, brief in phase_briefs.items():
        pool = candidate_pools.get(phase, {})
        visible_framing = _resolve_visible_phase_framing(phase, brief, week_by_week_progression)
        strategy[phase] = {
            "objective": brief.get("objective", ""),
            "visible_label": visible_framing["label"],
            "visible_objective": visible_framing["objective"],
            "build": clean_list(brief.get("emphasize", [])),
            "protect": clean_list(brief.get("risk_flags", [])),
            "deprioritize": clean_list(brief.get("deprioritize", [])),
            "must_keep": clean_list((brief.get("selection_guardrails") or {}).get("must_keep_if_present", [])),
            "drop_order_if_thin": clean_list((brief.get("selection_guardrails") or {}).get("conditioning_drop_order_if_thin", [])),
            "slot_counts": {
                "strength": len(pool.get("strength_slots", [])),
                "conditioning": len(pool.get("conditioning_slots", [])),
                "rehab": len(pool.get("rehab_slots", [])),
            },
        }
    return strategy


def _slot_exercise_name(slot: dict[str, Any]) -> str:
    for key in ("selected", "primary", "exercise", "drill"):
        value = slot.get(key)
        if isinstance(value, dict):
            name = str(value.get("name") or value.get("exercise_name") or value.get("drill_name") or "").strip()
            if name:
                return name
    return str(slot.get("name") or slot.get("exercise_name") or slot.get("drill_name") or "").strip()


def _slot_selected_option(slot: dict[str, Any]) -> dict[str, Any]:
    selected = slot.get("selected")
    return selected if isinstance(selected, dict) else {}


def _slot_text(slot: dict[str, Any]) -> str:
    selected = _slot_selected_option(slot)
    parts = [
        str(slot.get("role") or ""),
        str(slot.get("purpose") or ""),
        str(slot.get("quality_class") or ""),
        str(selected.get("quality_class") or ""),
        str(selected.get("prescription") or ""),
        " ".join(clean_list(selected.get("movement_patterns"))),
        " ".join(clean_list(selected.get("restriction_tags"))),
        " ".join(clean_list(selected.get("mechanical_risk_tags"))),
    ]
    return " ".join(part for part in parts if part).lower()


def _slot_countdown_labels(slot: dict[str, Any]) -> list[str]:
    selected = _slot_selected_option(slot)
    labels: list[str] = []
    for source in (slot, selected):
        for key in ("scheduled_countdown_label", "countdown_label", "days_out_bucket"):
            value = source.get(key)
            if value:
                labels.append(str(value))
        labels.extend(clean_list(source.get("allowed_countdown_labels")))
        labels.extend(clean_list(source.get("countdown_labels")))
    return dedupe_preserve_order(labels)


def _slot_support_only(slot: dict[str, Any]) -> bool:
    selected = _slot_selected_option(slot)
    metadata = selected.get("selection_metadata") if isinstance(selected.get("selection_metadata"), dict) else {}
    return bool(slot.get("support_only") or selected.get("support_only") or metadata.get("support_only"))


def _slot_has_meaningful_authority(slot: dict[str, Any], *, source_kind: str) -> bool:
    selected = _slot_selected_option(slot)
    metadata = selected.get("selection_metadata") if isinstance(selected.get("selection_metadata"), dict) else selected
    return has_meaningful_fulfillment_authority(
        metadata,
        source_kind=source_kind,
        source=str(metadata.get("_schema_source") or ""),
        allow_legacy_unstamped=True,
    )


def _slot_anchor_capable(slot: dict[str, Any]) -> bool:
    selected = _slot_selected_option(slot)
    return bool(
        (slot.get("anchor_capable") or selected.get("anchor_capable"))
        and _slot_has_meaningful_authority(slot, source_kind="strength")
    )


def _slot_is_low_load_reset(slot: dict[str, Any]) -> bool:
    text = _slot_text(slot)
    return _slot_support_only(slot) or any(
        phrase in text
        for phrase in ("rehab", "prehab", "mobility", "breathing", "reset", "recovery")
    )


def _slot_is_style_taper_neural_primer(
    slot: dict[str, Any], role: dict[str, Any], *, source_phase: str = ""
) -> bool:
    """Return whether a conditioning slot is the narrow Style Taper primer exception.

    Only the late-fight selector asks this, for a scheduled countdown role, so a
    direct D-13 countdown and a long camp's spliced tail are treated alike: both
    are the same late-fight membership owner. (The exception used to require
    ``late_fight_tail_owned``, which left a direct countdown's neural primer with
    no legal member and removed the session even though the same primers were
    legal for it.)
    """
    selected = _slot_selected_option(slot)
    metadata = selected.get("selection_metadata") if isinstance(selected, dict) else {}
    return bool(
        str(source_phase or role.get("phase") or "").upper() in {"SPP", "TAPER"}
        and _candidate_system(slot) == "alactic"
        and selected.get("source") == "style_taper"
        and isinstance(metadata, dict)
        and metadata.get("support_only") is True
        and metadata.get("meaningful_stress") is False
        and str(metadata.get("lactate_load") or "").lower() in {"low", "none"}
    )


def _candidate_system(slot: dict[str, Any]) -> str:
    selected = _slot_selected_option(slot)
    metadata = selected.get("selection_metadata") or {}
    system = str(selected.get("system") or selected.get("role") or metadata.get("system") or slot.get("role") or "").lower()
    return "alactic" if system in {"atp-pcr", "atp_pcr"} else system


def _slot_matches_late_fight_role(
    slot: dict[str, Any], slot_group: str, role: dict[str, Any], *, source_phase: str = ""
) -> bool:
    role_key = str(role.get("role_key") or "").strip()
    preferred_system = str(role.get("preferred_system") or "").strip().lower()
    slot_role = _candidate_system(slot)
    text = _slot_text(slot)

    if slot_group == "rehab_slots":
        return role_key in {"fight_week_freshness_day", "technical_touch_day"}
    if slot_group == "conditioning_slots":
        if role_key in {"strength_touch_day", "neural_primer_day", "alactic_sharpness_day"}:
            if _slot_is_style_taper_neural_primer(slot, role, source_phase=source_phase):
                return True
            if role_key in {"strength_touch_day", "neural_primer_day"}:
                return False
            return slot_role == "alactic" and _slot_selected_option(slot).get("source") != "style_taper"
        if preferred_system:
            return slot_role == preferred_system and _slot_has_meaningful_authority(
                slot, source_kind="conditioning"
            )
        return role_key in {"alactic_sharpness_day", "light_fight_pace_touch_day", "technical_touch_day"}
    if slot_group != "strength_slots":
        return False

    if role_key == "strength_touch_day":
        return _slot_anchor_capable(slot) and not _slot_is_low_load_reset(slot)
    if role_key == "neural_primer_day":
        return _slot_anchor_capable(slot) and any(
            phrase in text
            for phrase in ("isometric", "neural", "primer", "speed", "rate_of_force", "coordination")
        )
    if role_key == "technical_touch_day":
        return any(
            phrase in text
            for phrase in ("shadowboxing", "shadow boxing", "technical", "footwork", "skill_refinement", "coordination")
        ) and not any(phrase in text for phrase in ("loaded", "heavy", "trap_bar", "deadlift"))
    if role_key == "fight_week_freshness_day":
        return _slot_is_low_load_reset(slot)
    return False


def _scheduled_late_fight_roles(spec: dict[str, Any]) -> list[dict[str, Any]]:
    roles = spec.get("visible_session_sequence") or spec.get("session_sequence") or []
    return [role for role in roles if isinstance(role, dict) and str(role.get("scheduled_countdown_label") or "")]


def _candidate_slots_for_role(candidate_pools: dict[str, dict], role: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    matched: list[tuple[str, str, dict[str, Any]]] = []
    for phase, pool in (candidate_pools or {}).items():
        if not isinstance(pool, dict):
            continue
        for slot_group in ("strength_slots", "conditioning_slots", "rehab_slots"):
            slots = list(pool.get(slot_group, []) or [])
            if slot_group == "conditioning_slots":
                slots.extend(pool.get("late_tail_candidates", []) or [])
            for slot in slots:
                if not isinstance(slot, dict) or not _slot_exercise_name(slot):
                    continue
                if _slot_matches_late_fight_role(slot, slot_group, role, source_phase=str(phase)):
                    matched.append((str(phase), slot_group, slot))
                # Alternates remain Stage 1 candidate authority. Expose them to
                # the dated selector so a phase can use different legal drills
                # in different late windows instead of treating one selected
                # phase drill as universal.
                for index, alternate in enumerate(slot.get("alternates", []) or []):
                    if not isinstance(alternate, dict) or not alternate.get("name"):
                        continue
                    alternate_slot = {
                        "slot_id": f"{slot.get('slot_id') or 'slot'}:alternate:{index}",
                        "selected": alternate,
                        "role": alternate.get("system") or alternate.get("role") or (alternate.get("selection_metadata") or {}).get("system", ""),
                        "alternates": [],
                    }
                    if _slot_matches_late_fight_role(
                        alternate_slot, slot_group, role, source_phase=str(phase)
                    ):
                        matched.append((str(phase), slot_group, alternate_slot))
    return matched


def _slot_sport_allowed(slot: dict[str, Any], spec: dict[str, Any]) -> bool:
    from .planner_context import get_planner_athlete_model
    from .style_taper_governance import SPORT_TAGS
    athlete = get_planner_athlete_model() or spec.get("athlete_model") or {}
    sport = str(athlete.get("sport") or athlete.get("fight_format") or "").lower()
    if not sport:
        return True  # Legacy contextless tooling; production supplies the athlete.
    option = _slot_selected_option(slot)
    metadata = option.get("selection_metadata") or {}
    tags = set(metadata.get("tags") or option.get("movement_patterns") or [])
    explicit_sports = tags & SPORT_TAGS
    return not explicit_sports or sport in explicit_sports or (
        explicit_sports == {"grappling"} and sport in {"mma", "wrestling", "bjj"}
    )


# Mirror of the Stage 2 validator's D-1 safety rule
# (stage2_validator: ``dangerous_late_fight_strength_or_conditioning``). Any
# loaded strength or conditioning exposure rendered on D-1 is a hard blocker, so
# the allocator must never assign such an exercise to that day — D-1 stays a
# breathing / mobility / technical-cue primer only.
_LATE_FIGHT_D1_UNSAFE_NAME = re.compile(
    r"\b(strength|conditioning|sprints?|interval|heavy|loaded|deadlift|squat|trap[-_ ]bar|barbell"
    r"|band(?:s|ed)?|dumbbells?|kettlebells?|med(?:icine)?[-_ ]ball|slam[-_ ]ball|sled|sandbag"
    r"|cable|landmine|weight[-_ ]vest|pull[-_ ]?up)\b",
    re.IGNORECASE,
)

# Lazily built name -> requires-equipment map covering the strength,
# conditioning, and coordination banks. D-1 allows no equipment of any kind,
# so any bank exercise that needs equipment must never be assigned there.
_D1_EQUIPMENT_BY_NAME: dict[str, bool] | None = None


def _bank_requires_equipment(name: str) -> bool:
    global _D1_EQUIPMENT_BY_NAME
    if _D1_EQUIPMENT_BY_NAME is None:
        from .bank_schema import requires_equipment
        from .conditioning import get_conditioning_bank, get_coordination_bank
        from .strength import get_exercise_bank

        requirements: dict[str, bool] = {}
        for bank in (get_exercise_bank(), get_conditioning_bank(), get_coordination_bank()):
            for item in bank:
                item_name = str(item.get("name") or "").strip().lower()
                if item_name:
                    # A name duplicated across banks stays equipment-required
                    # if any version of it requires equipment.
                    requirements[item_name] = requirements.get(item_name, False) or requires_equipment(item)
        _D1_EQUIPMENT_BY_NAME = requirements
    return _D1_EQUIPMENT_BY_NAME.get(str(name or "").strip().lower(), False)


def _late_fight_assignment_is_unsafe(day_label: str, name: str) -> bool:
    """Return True when ``name`` must not be assigned to ``day_label``.

    Today this only guards D-1, matching the validator's D-1 blocker exactly:
    loaded/strength/conditioning name signals plus any bank exercise that
    requires equipment (D-1 is equipment-free).
    """
    if str(day_label or "").strip().upper() != "D-1":
        return False
    if _LATE_FIGHT_D1_UNSAFE_NAME.search(str(name or "")):
        return True
    return _bank_requires_equipment(name)


def _slot_late_window_allowed(
    phase: str, slot_group: str, slot: dict[str, Any], *, offset: int
) -> bool:
    """Check original bank authority (with serialized metadata as fallback)."""
    selected = _slot_selected_option(slot)
    entries = original_bank_entries(
        {
            "name": _slot_exercise_name(slot),
            "slot_group": slot_group,
            "source_phase": phase,
        }
    )
    if entries:
        entries = [entry for entry in entries if phase in entry.get("phases", [])]
        return bool(entries) and late_window_allowed(entries, offset=offset)

    metadata = selected.get("selection_metadata")
    if not isinstance(metadata, dict):
        # Non-bank synthetic/legacy slots retain their established behaviour.
        return True
    return late_window_allowed([metadata], offset=offset)


class StyleTaperLateTailUsage:
    """Style Taper primer usage across one athlete's whole finished late tail.

    The late tail is allocated in phase-scoped batches
    (``late_fight_phase_eligibility`` splits the roles by the Stage 1 phase that
    owns each D-day), so this state is owned by the caller and handed to every
    batch. That is what stops the sequence resetting at the SPP -> TAPER
    boundary, at a week boundary, or when the role key changes from
    ``neural_primer_day`` to ``alactic_sharpness_day``.

    Only Style Taper primers are recorded. Normal-camp conditioning consumption
    stays owned by ``consumed_slot_ids`` and is untouched.
    """

    __slots__ = ("name_uses", "family_uses")

    def __init__(self) -> None:
        self.name_uses: dict[str, int] = {}
        self.family_uses: dict[str, int] = {}

    def record(self, option: dict[str, Any]) -> None:
        if not isinstance(option, dict) or option.get("source") != "style_taper":
            return
        name = _style_taper_usage_name(option)
        if name:
            self.name_uses[name] = self.name_uses.get(name, 0) + 1
        family = _style_taper_option_family(option)
        self.family_uses[family] = self.family_uses.get(family, 0) + 1


def _style_taper_usage_name(option: dict[str, Any]) -> str:
    return str(option.get("name") or "").strip().lower()


def _style_taper_option_family(option: dict[str, Any]) -> str:
    from .style_taper_governance import style_taper_primer_family

    metadata = option.get("selection_metadata")
    return style_taper_primer_family(metadata if isinstance(metadata, dict) else option)


def _style_taper_relevance_key(option: dict[str, Any]) -> tuple[float, float, float, int]:
    """The existing Style Taper relevance order, unchanged.

    Style specificity, then declared goal/weakness coverage, then the Stage 1
    score, then bank order as the stable final tie-break.
    """
    relevance = option.get("relevance") or {}

    def hits(key: str) -> float:
        return float(relevance.get(key, 0) or 0)

    return (
        -hits("style_hits"),
        -(hits("goal_hits") + hits("weakness_hits")),
        -float(option.get("score", 0) or 0),
        # Bank order, with the bank's first entry (order 0) treated as first
        # rather than as missing metadata.
        int(option.get("bank_order") if option.get("bank_order") is not None else 10000),
    )


def _style_taper_relevance_band(option: dict[str, Any]) -> tuple[float, int]:
    """The relevance class inside which primers count as comparably appropriate.

    Two axes, both drawn from the relevance the Stage 1 reservoir already
    carries: how well the primer matches the athlete's tactical style, and
    whether it addresses any declared goal or weakness at all. A primer that
    matches the style less well, or that speaks to none of the athlete's
    declared targets, is *materially* less relevant and never displaces a
    repeat. A primer inside the band that overlaps one fewer goal tag is not:
    tag-overlap counts are a coarse relevance signal, not a dose-response
    measure, so a one-tag difference is a preference (kept, further down
    ``_style_taper_relevance_key``) rather than a different class.
    """
    relevance = option.get("relevance") or {}

    def hits(key: str) -> float:
        return float(relevance.get(key, 0) or 0)

    targets = hits("goal_hits") + hits("weakness_hits")
    return (hits("style_hits"), 1 if targets > 0 else 0)


def _rank_style_taper_primer_matches(
    matches: list[tuple[str, str, dict[str, Any]]],
    usage: "StyleTaperLateTailUsage",
) -> list[tuple[str, str, dict[str, Any]]]:
    """Order eligible Style Taper primers for one late-tail day.

    Every candidate reaching here has already passed the hard gates (sport,
    style, phase, late window, contact cap, equipment, injury/restriction,
    mechanical risk, dose and day safety) - this only decides preference among
    survivors, and can never admit something the gates rejected.

    Relevance leads, exactly as before: the relevance leader defines the band of
    comparably appropriate primers. Inside that band, and only among primers
    that cost the athlete no more than the leader on every taper-cost axis, an
    unused drill is preferred to a repeat, and a less-used primer family to a
    much-used one. Everything outside the band keeps the existing relevance
    order behind it, so when the band is exhausted - or holds a single option -
    the leader is still selected and repetition stays legal.

    Deterministic throughout: the counters are plan state, every tie-break is
    bank metadata, and no randomness, clock or hash enters the order.
    """
    from .style_taper_governance import style_taper_cost_is_not_higher

    ordered = sorted(matches, key=lambda match: _style_taper_relevance_key(_slot_selected_option(match[2])))
    if len(ordered) < 2:
        return ordered

    leader = _slot_selected_option(ordered[0][2])
    leader_band = _style_taper_relevance_band(leader)
    leader_metadata = leader.get("selection_metadata")
    leader_metadata = leader_metadata if isinstance(leader_metadata, dict) else {}

    def in_band(option: dict[str, Any]) -> bool:
        if _style_taper_relevance_band(option) != leader_band:
            return False
        metadata = option.get("selection_metadata")
        # Novelty must not buy a more elaborate taper session: a candidate is
        # only comparable when it is no more expensive than the leader.
        return style_taper_cost_is_not_higher(
            metadata if isinstance(metadata, dict) else {}, leader_metadata
        )

    def novelty_key(match: tuple[str, str, dict[str, Any]]):
        option = _slot_selected_option(match[2])
        # Exact-drill reuse is discouraged more strongly than family reuse
        # purely by position in this key: an unused drill outranks a used one
        # whatever their families, and family use only separates drills that
        # have been used equally often.
        return (
            usage.name_uses.get(_style_taper_usage_name(option), 0),
            usage.family_uses.get(_style_taper_option_family(option), 0),
            *_style_taper_relevance_key(option),
        )

    band_positions = {
        position
        for position, match in enumerate(ordered)
        if in_band(_slot_selected_option(match[2]))
    }
    if not band_positions:
        return ordered
    band = sorted((ordered[position] for position in sorted(band_positions)), key=novelty_key)
    return band + [
        match for position, match in enumerate(ordered) if position not in band_positions
    ]


# The countdown neural / speed / power sessions. Each is one session composed
# from Stage 1's legal candidates for its scheduled day.
LATE_PRIMER_ROLE_KEYS = frozenset(
    {"strength_touch_day", "neural_primer_day", "alactic_sharpness_day"}
)
# Product rule: a neural primer holds at most three exercises. Fewer is normal;
# this is a ceiling, never a target, and never permission to triple the work.
LATE_PRIMER_MAX_MEMBERS = 3


def _late_primer_candidate_order(
    matches: list[tuple[str, str, dict[str, Any]]],
    usage: "StyleTaperLateTailUsage",
) -> list[tuple[str, str, dict[str, Any]]]:
    """Order a primer day's legal candidates: meaningful work leads.

    Every candidate here already passed the hard gates (phase, late window,
    sport, day safety). Meaningful Stage 1 work comes first in Stage 1's own
    order, then Stage 1 support work, then Style Taper primers in their existing
    relevance/novelty order. A support-only primer therefore complements a
    meaningful anchor; it no longer displaces one merely because it was the
    preferred fallback.
    """
    unique: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[str] = set()
    for match in matches:
        name = _slot_exercise_name(match[2]).casefold()
        if name and name not in seen:
            seen.add(name)
            unique.append(match)

    def is_style_taper(match: tuple[str, str, dict[str, Any]]) -> bool:
        return _slot_selected_option(match[2]).get("source") == "style_taper"

    meaningful = [m for m in unique if not is_style_taper(m) and not _slot_support_only(m[2])]
    support = [m for m in unique if not is_style_taper(m) and _slot_support_only(m[2])
               and not _slot_selected_option(m[2]).get("generic_fallback")]
    fallback = [m for m in unique if not is_style_taper(m) and _slot_support_only(m[2])
                and _slot_selected_option(m[2]).get("generic_fallback")]
    style_taper = _rank_style_taper_primer_matches(
        [m for m in unique if is_style_taper(m)], usage
    )
    # Relevance scoring has no sport-specificity term, so drills tagged for
    # every striking sport outrank the athlete's own weapons (kicks, knees,
    # clinch) everywhere. The least-used sport-specific drill therefore leads
    # the Style Taper primers, ahead of the generic relevance leader, so a
    # kickboxer's primer is not a boxer's primer.
    specific = [m for m in style_taper if _style_taper_sport_specific(_slot_selected_option(m[2]))]
    if specific:
        promoted = min(
            specific,
            key=lambda m: (usage.name_uses.get(_style_taper_usage_name(_slot_selected_option(m[2])), 0),
                           style_taper.index(m)),
        )
        style_taper = [promoted, *(m for m in style_taper if m is not promoted)]
    # A generic, non-fulfilling maintenance fallback must not displace an
    # eligible bank drill with an actual full-rest physical prescription.
    return meaningful + support + style_taper + fallback


def _style_taper_sport_specific(option: dict[str, Any]) -> bool:
    """Whether a Style Taper drill trains a weapon specific to the athlete's sport.

    Tagged for the athlete's sport but not for boxing: the drill works what a
    boxer does not have (kicks, knees, clinch, takedowns). A boxer's drills are
    all boxing drills, so nothing is promoted for a boxer.
    """
    from .planner_context import get_planner_athlete_model

    athlete = get_planner_athlete_model() or {}
    sport = str(athlete.get("sport") or athlete.get("fight_format") or "").strip().lower().replace(" ", "_")
    if not sport or sport == "boxing":
        return False
    metadata = option.get("selection_metadata") if isinstance(option.get("selection_metadata"), dict) else {}
    tags = {str(tag).strip().lower() for tag in metadata.get("tags") or option.get("movement_patterns") or []}
    return sport in tags and "boxing" not in tags


def _late_primer_stimulus(match: tuple[str, str, dict[str, Any]]) -> tuple[str, str]:
    """The kind of stimulus a candidate adds, so a session is not one drill thrice."""
    _phase, slot_group, slot = match
    option = _slot_selected_option(slot)
    if option.get("source") == "style_taper":
        # A sport-specific weapon drill is a different stimulus from a generic
        # hand drill of the same primer family.
        family = _style_taper_option_family(option)
        return ("style_taper", f"{family}:sport_specific" if _style_taper_sport_specific(option) else family)
    if slot_group == "strength_slots":
        return ("strength", str(slot.get("quality_class") or option.get("quality_class") or "").lower())
    metadata = option.get("selection_metadata") if isinstance(option.get("selection_metadata"), dict) else {}
    return ("conditioning", str(metadata.get("modality") or _candidate_system(slot)).lower())


def _late_primer_is_loaded(match: tuple[str, str, dict[str, Any]]) -> bool:
    from .prescription_resolver import _role_kind

    return match[1] == "strength_slots" and _role_kind(match[2]) in {"anchor", "secondary", "hybrid"}


def _late_primer_member_dose(
    role: dict[str, Any], slot_group: str, slot: dict[str, Any], *, complement: bool
) -> str:
    """The dose a member will be performed at, from the canonical dose owners.

    Strength comes from ``prescription_resolver`` at the role's scheduled-day
    cap; athlete readiness state can only lower it further later, so this is an
    upper bound. A complementary member is a single set (or round): it adds a
    different stimulus, not more volume.
    """
    if slot_group == "strength_slots":
        from .prescription_resolver import resolve_strength_slot_prescription

        target = {**slot, "primer_complement": True} if complement else slot
        return str(
            resolve_strength_slot_prescription(role=role, slot=target).get("effective_prescription") or ""
        )
    from .prescription_resolver import resolve_late_primer_conditioning_dose

    return resolve_late_primer_conditioning_dose(
        _conditioning_prescription(_slot_selected_option(slot)),
        d_day=sequence_role_offset_of(role),
        alactic=_candidate_system(slot) == "alactic",
        complement=complement,
    )


def _late_primer_member_stress(
    role: dict[str, Any], slot_group: str, slot: dict[str, Any], *, complement: bool
) -> tuple[bool, list[dict[str, Any]]]:
    """``(known, measurement_input)`` for one member at its real dose.

    A member is measurable when its dose states an amount of work and it
    carries cost evidence: authored mechanical tags, a rep count, or -- for a
    tagless timed drill -- an authored low/no lactate load. Anything else is
    unknown, and unknown is never evidence that more work fits.
    """
    option = _slot_selected_option(slot)
    tags = sorted(option_mechanical_risk_tags(option))
    dose = _late_primer_member_dose(role, slot_group, slot, complement=complement)
    metadata = option.get("selection_metadata") if isinstance(option.get("selection_metadata"), dict) else {}
    lactate = str(metadata.get("lactate_load") or option.get("lactate_load") or "").strip().lower()
    known = prescription_dose_known(dose) and bool(
        tags or _prescription_total_reps(dose) or lactate in {"low", "none"}
    )
    return known, [{"mechanical_risk_tags": tags, "effective_prescription": dose}]


def _late_primer_session_stress(
    role: dict[str, Any],
    matches: list[tuple[str, str, dict[str, Any]]],
    complements: set[int],
):
    """Realised cross-day cost of the whole composed session, or ``None`` if unknown.

    ``None`` means some member could not be measured. A fully measured session
    whose members author no mechanical or metabolic cost is a known low-load
    session, not an unknown one.
    """
    from .combat_load_policy import SessionStress, StressLevel

    measured: list[dict[str, Any]] = []
    for index, (_phase, slot_group, slot) in enumerate(matches):
        known, item = _late_primer_member_stress(
            role, slot_group, slot, complement=index in complements
        )
        if not known:
            return None
        measured.extend(item)
    stress = realised_session_stress(measured)
    return stress if stress is not None else SessionStress(neural_mechanical=StressLevel.LOW)


def _late_primer_planned_profile(role: dict[str, Any]):
    """The load profile placement judged this role by (its planned promise)."""
    from .calendar_context import classify_role

    return classify_role({k: v for k, v in role.items() if k != "calendar_stress"})


def _late_primer_dominating(stress) -> bool:
    from .combat_load_policy import _DOMINATING_STRESS

    return stress is not None and (
        stress.systemic >= _DOMINATING_STRESS or stress.neural_mechanical >= _DOMINATING_STRESS
    )


def _late_primer_within_envelope(role: dict[str, Any], lead_stress, combined) -> bool:
    """A complement may not raise the session above its own load envelope.

    The envelope is the role's planned vector (what placement and legality
    already cleared) or, where the lead exercise alone already measures above
    it, that single-exercise session. Measured on both canonical axes. A
    complement never produces, or adds to, a session at the policy's dominating
    tier. No new score: the measurement is ``realised_session_stress`` and the
    ceilings are ``combat_load_policy``'s own planned vector and tiers.
    """
    from .combat_load_policy import session_stress

    if combined is None or lead_stress is None:
        return False
    if _late_primer_dominating(combined):
        return False
    from .combat_load_policy import _MEANINGFUL_STRESS, SessionStress

    profile = _late_primer_planned_profile(role)
    ceilings = [
        stress
        for stress in (session_stress(profile) if profile is not None else None, lead_stress)
        if stress is not None
    ]
    if str(role.get("stress_class") or "").strip().lower() == "meaningful_stress":
        # A role that promises meaningful stress may carry meaningful (never
        # dominating) neural load whatever its class default says -- an alactic
        # conditioning day's planned vector names only its systemic cost. The
        # systemic axis stays capped by the plan; cross-day legality is
        # checked separately.
        ceilings.append(SessionStress(neural_mechanical=_MEANINGFUL_STRESS))
    if not ceilings:
        return False
    return combined.systemic <= max(s.systemic for s in ceilings) and (
        combined.neural_mechanical <= max(s.neural_mechanical for s in ceilings)
    )


def _late_primer_decision(
    role: dict[str, Any], stress, sequence: list[dict[str, Any]], *, offset: int | None = None
):
    """Canonical legality of this session, composed at ``stress``, on a day.

    The role is classified as it would be stamped, so the policy judges the
    session it actually is (a measured microdose is classified as one).
    ``offset`` asks about another countdown day without moving the role.
    """
    from .calendar_context import LATE_FIGHT_SCOPE, classify_role, sequence_events, sequence_role_offset
    from .combat_load_policy import evaluate_candidate_at_position

    probe = {k: v for k, v in role.items() if k != "calendar_stress"}
    if stress is not None:
        probe["calendar_stress"] = _stress_stamp(stress)
    offset = offset if offset is not None else sequence_role_offset(role)
    profile = classify_role(probe)
    if offset is None or profile is None:
        return None
    return evaluate_candidate_at_position(
        profile,
        candidate_position=-offset,
        events=sequence_events([item for item in sequence if item is not role]),
        candidate_scope=LATE_FIGHT_SCOPE,
    )


def _late_primer_neighbour_rank(role: dict[str, Any], stress, sequence: list[dict[str, Any]]) -> int:
    """Canonical legality tier of this session at ``stress``."""
    from .combat_load_policy import placement_rank

    decision = _late_primer_decision(role, stress, sequence)
    return placement_rank(decision) if decision is not None else 0


def _late_primer_coexisting_microdose(
    role: dict[str, Any],
    ordered: list[tuple[str, str, dict[str, Any]]],
) -> tuple[str, str, dict[str, Any]] | None:
    """The single support member that can share a day the policy will not give up.

    Late-fight placement is immutable, so a primer whose day the policy forbids
    (an exclusive stressor stacked on declared light combat, say) cannot move.
    Its prescription is reduced instead: one support-only drill, one round,
    measured no higher than the policy's own neural-microdose vector -- the
    load class the policy lets coexist with declared light combat.
    """
    from .combat_load_policy import (
        CalendarLoadProfile, DayOccupancy, LoadClass, session_stress,
    )

    ceiling = session_stress(CalendarLoadProfile(LoadClass.NEURAL_MICRODOSE, DayOccupancy.PHYSICAL))
    for match in ordered:
        if not _slot_support_only(match[2]) or _late_primer_is_loaded(match):
            continue
        stress = _late_primer_session_stress(role, [match], {0})
        if (
            stress is not None
            and stress.systemic <= ceiling.systemic
            and stress.neural_mechanical <= ceiling.neural_mechanical
        ):
            return match
    return None


def sequence_role_offset_of(role: dict[str, Any]) -> int | None:
    from .calendar_context import sequence_role_offset

    return sequence_role_offset(role)


def _is_displaceable_occupancy_filler(role: dict[str, Any]) -> bool:
    """A gap-fill insert that only occupies an otherwise empty day.

    Fillers are subordinate to planner roles: one placed to occupy a free day
    yields that day to a real session the calendar needs to move there.
    """
    governance = role.get("governance") if isinstance(role.get("governance"), dict) else {}
    return bool(
        role.get("physical_occupancy_fill")
        and str(role.get("category") or "").strip().lower() == "support_insert"
        and governance.get("meaningful_stress") is not True
    )


def drop_displaced_occupancy_fillers(roles: list[Any]) -> None:
    """Remove, in place, fillers a relocated late primer took the day from."""
    roles[:] = [
        role for role in roles
        if not (isinstance(role, dict) and role.get("displaced_by_late_primer"))
    ]


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _late_primer_relocate(
    role: dict[str, Any],
    chosen: list[tuple[str, str, dict[str, Any]]],
    complements: set[int],
    sequence: list[dict[str, Any]],
) -> tuple[list[tuple[str, str, dict[str, Any]]], set[int], int] | None:
    """Move a forbidden primer to an adjacent day the policy allows, if one exists.

    A primer is a small stimulus; a conflict with one day's other work is not
    a reason it cannot happen at all. Only the days either side are tried, only
    a declared training day inside D-13..D-2, only members still bank-legal for
    that day's late window and day safety, and only where the policy's verdict
    for the composed session is not FORBID (ALLOW first, then the earlier
    day). Nothing else moves.
    """
    from .combat_load_policy import PlacementDirective, placement_rank
    from .planner_context import get_planner_athlete_model

    offset = sequence_role_offset_of(role)
    weekday = str(role.get("scheduled_day_hint") or role.get("real_weekday") or "").strip().lower()
    if offset is None or weekday not in _WEEKDAYS:
        return None
    athlete = get_planner_athlete_model() or {}
    training_days = {str(day).strip().lower() for day in athlete.get("training_days") or []}
    options = []
    for target in (offset + 1, offset - 1):
        if not 2 <= target <= 13:
            continue
        target_weekday = _WEEKDAYS[(_WEEKDAYS.index(weekday) + (offset - target)) % 7]
        if training_days and target_weekday not in training_days:
            continue
        label = f"D-{target}"
        kept = [
            (index, match)
            for index, match in enumerate(chosen)
            if _slot_late_window_allowed(match[0], match[1], match[2], offset=target)
            and not _late_fight_assignment_is_unsafe(label, _slot_exercise_name(match[2]))
        ]
        if not kept:
            continue
        members = [match for _index, match in kept]
        kept_complements = set(range(1, len(members)))
        stress = _late_primer_session_stress(role, members, kept_complements)
        fillers = [
            item for item in sequence
            if item is not role and sequence_role_offset_of(item) == target
            and _is_displaceable_occupancy_filler(item)
        ]
        verdict = _late_primer_decision(
            role, stress, [item for item in sequence if not any(item is f for f in fillers)],
            offset=target,
        )
        if verdict is None or verdict.directive is PlacementDirective.FORBID:
            continue
        options.append(
            (placement_rank(verdict), -target, target, target_weekday, members, kept_complements, fillers)
        )
    if not options:
        return None
    _rank, _order, target, target_weekday, members, kept_complements, fillers = min(
        options, key=lambda option: option[:2]
    )
    for filler in fillers:
        filler["displaced_by_late_primer"] = str(role.get("role_key") or "")
    label = f"D-{target}"
    for key in ("scheduled_countdown_label", "countdown_label"):
        role[key] = label
    role["countdown_offset"] = target
    role["scheduled_d_day"] = target
    for key in ("scheduled_day_hint", "real_weekday", "countdown_weekday"):
        if key in role or key == "scheduled_day_hint":
            role[key] = target_weekday
    role["countdown_display_label"] = f"{label} ({target_weekday.title()})"
    role["day_assignment_reason"] = (
        f"Moved from D-{offset} to {label}: the composed primer was forbidden on its original day "
        "and this adjacent training day is legal for it."
    )
    return members, kept_complements, target


def _select_late_primer_members(
    matches: list[tuple[str, str, dict[str, Any]]],
    role: dict[str, Any],
    usage: "StyleTaperLateTailUsage",
    *,
    sequence: list[dict[str, Any]],
) -> tuple[list[tuple[str, str, dict[str, Any]]], set[int]]:
    """Compose a primer session of up to ``LATE_PRIMER_MAX_MEMBERS`` exercises.

    The lead is the best legal candidate. Each further candidate is admitted
    only when it adds a different stimulus, keeps at most one loaded lift in the
    session, keeps the combined session inside its load envelope, and does not
    make the session's cross-day legality worse than the lead alone. Returns the
    members and the indices of the complementary (single-set) ones.
    """
    ordered = _late_primer_candidate_order(matches, usage)
    if role.get("role_key") == "alactic_sharpness_day":
        # Select the alactic appointment from actual full-rest speed doses.
        # A support label/priority must not put a short-rest accessory ahead
        # of an eligible bank speed drill, then reduce that drill to one round.
        from .conditioning import _conditioning_structured_profile
        speed = [match for match in ordered if match[1] == "conditioning_slots"
                 and _conditioning_structured_profile(
                     _slot_selected_option(match[2]).get("selection_metadata") or _slot_selected_option(match[2]),
                     system="alactic")["speed_development"]
                 and not _slot_selected_option(match[2]).get("generic_fallback")]
        speed.sort(key=lambda match: usage.name_uses.get(_style_taper_usage_name(_slot_selected_option(match[2])), 0))
        ordered = speed + [match for match in ordered if match not in speed]
    if not ordered:
        return [], set()
    # The lead is the best candidate whose own session stays below the
    # policy's dominating tier; only when none does is the best one kept as is.
    def fits(match) -> bool:
        stress = _late_primer_session_stress(role, [match], set())
        return stress is not None and not _late_primer_dominating(stress)

    lead = next((match for match in ordered if fits(match)), ordered[0])
    ordered = [lead, *(match for match in ordered if match is not lead)]
    # Countdown and readiness already limit each member through the dose owner.
    member_limit = LATE_PRIMER_MAX_MEMBERS
    chosen = [ordered[0]]
    stimuli = {_late_primer_stimulus(ordered[0])}
    loaded_used = _late_primer_is_loaded(ordered[0])
    lead_stress = _late_primer_session_stress(role, chosen, set())
    lead_rank = _late_primer_neighbour_rank(role, lead_stress, sequence)
    for match in ordered[1:] if lead_stress is not None else ():
        if len(chosen) >= member_limit:
            break
        stimulus = _late_primer_stimulus(match)
        loaded = _late_primer_is_loaded(match)
        if stimulus in stimuli or (loaded and loaded_used):
            continue
        trial = [*chosen, match]
        stress = _late_primer_session_stress(role, trial, set(range(1, len(trial))))
        if not _late_primer_within_envelope(role, lead_stress, stress):
            continue
        if _late_primer_neighbour_rank(role, stress, sequence) > lead_rank:
            continue
        chosen.append(match)
        stimuli.add(stimulus)
        loaded_used = loaded_used or loaded
    complements = set(range(1, len(chosen)))

    from .combat_load_policy import PlacementDirective

    decision = _late_primer_decision(
        role, _late_primer_session_stress(role, chosen, complements), sequence
    )
    if decision is None or decision.directive is not PlacementDirective.FORBID:
        return chosen, complements
    # The policy forbids this session on its day. Resolve it, in order: move the
    # composed session to an adjacent training day the policy allows; reduce it
    # to a coexisting microdose the policy re-verifies; only then remove it.
    resolution = {
        "authority": "late_fight_assignment_builder",
        "conflict_reason_code": decision.reason_code,
        "from_d_day": sequence_role_offset_of(role),
        "composed_members": [_slot_exercise_name(match[2]) for match in chosen],
    }
    moved = _late_primer_relocate(role, chosen, complements, sequence)
    if moved is not None:
        members, kept, offset = moved
        role["late_load_resolution"] = {
            **resolution, "action": "relocated_to_adjacent_day", "to_d_day": offset,
            "displaced_fillers": [
                str(item.get("role_key") or "") for item in sequence
                if item.get("displaced_by_late_primer") and sequence_role_offset_of(item) == offset
            ],
        }
        return members, kept
    reduced = _late_primer_coexisting_microdose(role, ordered)
    if reduced is not None:
        verdict = _late_primer_decision(
            role, _late_primer_session_stress(role, [reduced], {0}), sequence
        )
        if verdict is not None and verdict.directive is not PlacementDirective.FORBID:
            role["late_load_resolution"] = {
                **resolution, "action": "reduced_to_coexisting_microdose",
                "verified_directive": verdict.directive.value,
            }
            return [reduced], {0}
    role["late_load_resolution"] = {**resolution, "action": "suppressed_no_safe_prescription"}
    return [], set()


def _build_late_fight_allowed_exercises_by_day(
    *,
    spec: dict[str, Any],
    candidate_pools: dict[str, dict],
    style_taper_usage: "StyleTaperLateTailUsage | None" = None,
) -> tuple[dict[str, list[str]], dict[str, list[dict[str, Any]]]]:
    allowed_by_day: dict[str, list[str]] = {}
    assignments_by_day: dict[str, list[dict[str, Any]]] = {}
    consumed_slot_ids: set[str] = set()
    # Style Taper primers stay deliberately exempt from consumed_slot_ids (a
    # single surviving safe option must still be able to fill a later day), so
    # the sequence is remembered here instead of banning reuse. Passed in by the
    # phase-scoped caller when one plan is allocated in several batches.
    style_taper_usage = style_taper_usage if style_taper_usage is not None else StyleTaperLateTailUsage()

    for role in _scheduled_late_fight_roles(spec):
        day_label = str(role.get("scheduled_countdown_label") or role.get("countdown_label") or "").strip()
        if not day_label:
            continue
        try:
            scheduled_offset = int(day_label.upper().removeprefix("D-"))
        except ValueError:
            scheduled_offset = -1
        allowed_by_day.setdefault(day_label, [])
        assignments_by_day.setdefault(day_label, [])

        explicit_matches: list[tuple[str, str, dict[str, Any]]] = []
        fallback_matches: list[tuple[str, str, dict[str, Any]]] = []
        diagnostics = {"phase_window_rejected": 0, "sport_rejected": 0, "day_safety_rejected": 0}
        role["late_assignment_diagnostics"] = diagnostics
        for phase, slot_group, slot in _candidate_slots_for_role(candidate_pools, role):
            # Bank late-window permission is a hard eligibility gate. Apply it
            # before explicit/fallback ranking so the next legal candidate can
            # win instead of committing a contradiction for Stage 2 to catch.
            if scheduled_offset >= 0 and not _slot_late_window_allowed(
                phase, slot_group, slot, offset=scheduled_offset
            ):
                diagnostics["phase_window_rejected"] += 1
                continue
            if not _slot_sport_allowed(slot, spec):
                diagnostics["sport_rejected"] += 1
                continue
            # Drop day-unsafe candidates (e.g. loaded work on D-1) before
            # selection so a safe explicit match or fallback can still be used
            # instead of leaving the day empty.
            if _late_fight_assignment_is_unsafe(day_label, _slot_exercise_name(slot)):
                diagnostics["day_safety_rejected"] += 1
                continue
            slot_id = str(slot.get("slot_id") or f"{phase}:{slot_group}:{_slot_exercise_name(slot)}")
            labels = _slot_countdown_labels(slot)
            if labels:
                if day_label in labels:
                    explicit_matches.append((phase, slot_group, slot))
                continue
            if slot_id not in consumed_slot_ids or _slot_selected_option(slot).get("source") == "style_taper":
                fallback_matches.append((phase, slot_group, slot))

        complements: set[int] = set()
        if str(role.get("role_key") or "") in LATE_PRIMER_ROLE_KEYS:
            # Explicit day labels keep their authority over the candidate set;
            # the session itself is composed the same way either way.
            selected_matches, complements = _select_late_primer_members(
                explicit_matches or fallback_matches,
                role,
                style_taper_usage,
                sequence=list(spec.get("calendar_sequence") or _scheduled_late_fight_roles(spec)),
            )
            # A forbidden primer may have been moved to an adjacent legal day.
            day_label = str(role.get("scheduled_countdown_label") or day_label)
            allowed_by_day.setdefault(day_label, [])
            assignments_by_day.setdefault(day_label, [])
            # Record what was legal, so a support-only session is visibly
            # justified by the absence of a legal meaningful candidate.
            candidates = _late_primer_candidate_order(
                explicit_matches or fallback_matches, StyleTaperLateTailUsage()
            )
            diagnostics["legal_candidates"] = len(candidates)
            diagnostics["meaningful_legal_candidates"] = sum(
                1
                for match in candidates
                if not _slot_support_only(match[2])
                and _slot_selected_option(match[2]).get("source") != "style_taper"
            )
            # A support-only session fills the day but does not discharge the
            # strength/power obligation the role represents, so record that the
            # role's meaningful exposure is still outstanding.
            if selected_matches and all(
                _slot_support_only(match[2]) for match in selected_matches
            ):
                role["support_only_fill"] = True
                role["meaningful_stress_fulfilled"] = False
        else:
            selected_matches = explicit_matches or fallback_matches[:1]
        for index, (phase, slot_group, slot) in enumerate(selected_matches):
            name = _slot_exercise_name(slot)
            if not name:
                continue
            slot_id = str(slot.get("slot_id") or f"{phase}:{slot_group}:{name}")
            if not _slot_countdown_labels(slot):
                consumed_slot_ids.add(slot_id)
            allowed_by_day[day_label].append(name)
            option = _slot_selected_option(slot)
            # Record the exposure whichever path selected it, so a primer placed
            # by an explicit day label or by the generic fallback still counts as
            # used when the next late-tail day is allocated.
            style_taper_usage.record(option)
            assignment = {
                "name": name,
                "role_key": role.get("role_key"),
                "scheduled_countdown_label": day_label,
                "slot_id": slot.get("slot_id"),
                "slot_group": slot_group,
                "phase": phase,
            }
            # The assignment is the only surviving record of the authored
            # mechanical vocabulary, which realised-load measurement reads.
            mechanical_tags = sorted(option_mechanical_risk_tags(option))
            if mechanical_tags:
                assignment["mechanical_risk_tags"] = mechanical_tags
            if index in complements:
                assignment["primer_complement"] = True
            stamp_exercise_key(assignment)
            # Carry the selected bank dose forward for conditioning tail work so
            # Stage 2 renders it directly, mirroring normal conditioning.  Loaded
            # strength tail doses stay owned by the scheduled-day resolver
            # (effective_strength_prescriptions) so a taper cap is never bypassed.
            if slot_group == "strength_slots" and role.get("category") != "strength":
                # A retained recovery/support appointment may contain an eligible
                # bank strength-support member. Price it with the same scheduled
                # countdown and readiness owner instead of leaving an AI-dose gap.
                from .late_camp_role_morph import late_fight_strength_dose_cap
                from .prescription_resolver import resolve_strength_slot_prescription, athlete_dose_state
                from .planner_context import get_planner_athlete_model
                probe = dict(role)
                cap = late_fight_strength_dose_cap(scheduled_offset)
                if cap:
                    probe.update(strength_dose_cap={key: cap[key] for key in ("max_sets", "max_reps", "loaded_allowed")},
                                 rpe_cap=cap.get("rpe_cap"))
                resolved = resolve_strength_slot_prescription(
                    role=probe, slot=slot, athlete_state=athlete_dose_state(get_planner_athlete_model()),
                )
                assignment.update(base_prescription=option.get("prescription"),
                                  effective_prescription=resolved.get("effective_prescription"))
            if slot_group == "conditioning_slots":
                assignment["system"] = _candidate_system(slot)
                prescription = _conditioning_prescription(option)
                if prescription:
                    assignment["base_prescription"] = prescription
                    assignment["effective_prescription"] = _late_primer_member_dose(
                        role, slot_group, slot, complement=index in complements
                    ) or prescription
                    # Persist the dose the countdown owner actually authorised,
                    # including a one-effort complement. Otherwise goal evidence
                    # falls back to the bank's unreduced two/three efforts.
                    from .exercise_progression import measured_dose
                    from .config import conditioning_work_is_timed
                    measured = measured_dose(assignment["effective_prescription"])
                    if assignment["system"] == "alactic":
                        # Keep structured recovery aligned with the resolved rest,
                        # including rep-based throws; do not alter source RPE.
                        if measured.get("rest") is not None:
                            assignment["rest_sec"] = measured["rest"]
                    count = measured.get("rounds") or measured.get("sets")
                    work = measured.get("work", measured.get("duration"))
                    if (count and work and measured.get("rest") is not None
                            and conditioning_work_is_timed({"duration": assignment["effective_prescription"]})):
                        assignment.update(rounds=count, effective_rounds=count,
                                          work_sec=work, rest_sec=measured["rest"])
                assignment.update(
                    {
                        "support_only": option.get("support_only") is True,
                        "meaningful_stress": option.get("meaningful_stress") is True,
                        "fulfillment_authority": option.get("fulfillment_authority") is True,
                    }
                )
            notes = _selected_coaching_notes(option)
            if notes:
                assignment["coaching_notes"] = notes
            assignment.update(_selected_coaching_fields(option))
            assignments_by_day[day_label].append(assignment)
        if str(role.get("role_key") or "") in LATE_PRIMER_ROLE_KEYS and selected_matches:
            # Stamp the composed session's realised load in the canonical
            # vocabulary so the days after it are judged against what was
            # actually composed here, not the promise of its role key.
            stress = _late_primer_session_stress(role, selected_matches, complements)
            if stress is not None:
                role["calendar_stress"] = _stress_stamp(stress)

    return (
        {day: dedupe_preserve_order(names) for day, names in allowed_by_day.items()},
        assignments_by_day,
    )


def _late_fight_countdown_days_from_spec(days_until_fight: Any, spec: dict[str, Any]) -> list[int]:
    days: list[int] = []
    for segment in spec.get("countdown_mode_sequence", []) or []:
        if not isinstance(segment, dict):
            continue
        start_day = segment.get("start_day")
        end_day = segment.get("end_day")
        if isinstance(start_day, int) and isinstance(end_day, int):
            days.extend(range(start_day, end_day - 1, -1))
    if days:
        return dedupe_preserve_order(days)
    try:
        day = int(days_until_fight)
    except (TypeError, ValueError):
        return []
    return [day] if day >= 0 else []


def _with_late_fight_allowed_exercises(
    *,
    spec: dict[str, Any],
    candidate_pools: dict[str, dict],
    days_until_fight: Any,
) -> dict[str, Any]:
    allowed_by_day, assignments_by_day = _build_late_fight_allowed_exercises_by_day(
        spec=spec,
        candidate_pools=candidate_pools,
    )
    for day in _late_fight_countdown_days_from_spec(days_until_fight, spec):
        allowed_by_day.setdefault(f"D-{day}", [])
    roles = spec.get("visible_session_sequence") or spec.get("session_sequence") or []
    drop_displaced_occupancy_fillers(roles)
    attach_late_fight_assignments(roles, assignments_by_day)
    return {
        **spec,
        "allowed_exercises_by_day": allowed_by_day,
        "allowed_exercise_assignments_by_day": assignments_by_day,
    }


def _apply_boxing_crowded_week_post_processing(
    weekly_role_map: dict[str, Any],
    *,
    athlete_model: dict[str, Any],
) -> None:
    """Decorate roles using crowded-week truth already owned by the role map.

    ``athlete_model`` remains in the compatibility signature, but this payload
    layer must not use it to re-evaluate compression or role survival.
    """
    del athlete_model
    weeks = weekly_role_map.get("weeks")
    if not isinstance(weeks, list):
        return
    for week_entry in weeks:
        if not isinstance(week_entry, dict):
            continue
        compression = week_entry.get("intentional_compression")
        crowded_week_active = (
            isinstance(compression, dict)
            and compression.get("policy") == "boxing_crowded_week"
        )
        for role in week_entry.get("session_roles") or []:
            if isinstance(role, dict):
                _apply_day_identity_governance(role, crowded_week_active=crowded_week_active)


def build_computed_support(*, flags: dict, phases: list[str] | None = None) -> dict:
    """Bundle Stage 1's own computed nutrition/mindset numbers.

    Carries the deterministic Stage 1 support data into the planning brief as
    structured (not prose) blocks. Nutrition stays Stage 1-only: the safety
    audit checks Stage 2 output against it, but it is never handed to Stage 2
    (see :func:`strip_stage1_nutrition_section`). ``coach_gated`` sub-sections
    inside nutrition hold exact
    acute weight-cut and supplement dosing and must never be surfaced directly
    to athletes (coach/medical-gated only).
    """
    active_phases = [str(p).upper() for p in (phases or ["GPP", "SPP", "TAPER"])]
    # De-dup while preserving order.
    ordered_phases = list(dict.fromkeys(active_phases))

    nutrition_by_phase = {
        phase: compute_nutrition_targets(flags={**flags, "phase": phase})
        for phase in ordered_phases
    }
    support = {
        "schema_version": "computed_support.v1",
        "source": "stage1_deterministic",
        "athlete_facing_note": (
            "coach_gated sub-sections hold acute weight-cut and supplement "
            "dosing — never surface them directly to athletes."
        ),
        "nutrition": {"by_phase": nutrition_by_phase},
        "mindset": compute_mindset_plan(flags),
        # Deterministic safeguard state travels with the deterministic numbers.
        # The structured-plan safety audit reads this to decide whether Stage 2
        # wording is allowed to mention a cut at all, which is how "AI is
        # subordinate to the deterministic safety rules" is actually enforced
        # rather than merely asserted.
        "safeguards": {"is_minor": bool(flags.get("is_minor", False))},
    }
    # Display-only reminder for injuries marked not limiting. Merged onto the
    # finished structured plan after the safety audit; stripped from the
    # conversion prompt so it can never become a restriction.
    awareness = build_injury_awareness_support(flags.get("non_limiting_injuries"))
    if awareness:
        support["injury_awareness"] = awareness
    return support


# An open plan is a renewable four-week development block. Its calendar is
# placed this far from any fight so that no countdown band (D-17 inward), late
# path (D-13 inward), fight-week rule or fight-day override can reach it.
OPEN_BLOCK_WEEKS = 4
OPEN_BLOCK_HORIZON_DAYS = 84
# The block is planned as one development phase. Without periodisation it
# trains both general and specific qualities, so it draws on Stage 1's general
# (GPP) and specific (SPP) selections together; taper work is countdown-only.
OPEN_BLOCK_PHASE = "GPP"
_OPEN_BLOCK_SOURCE_PHASES = ("GPP", "SPP")
_OPEN_BLOCK_SLOT_GROUPS = ("strength_slots", "conditioning_slots", "rehab_slots")
_OPEN_BLOCK_SERVER_OWNED = frozenset({"tactical_watch", "fight_visualization"})
_WEEKDAY_ORDER = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _open_block_athlete_view(athlete_model: dict) -> dict:
    days = 7 * OPEN_BLOCK_WEEKS
    return {
        **athlete_model,
        "days_until_fight": OPEN_BLOCK_HORIZON_DAYS + days,
        "open_block_horizon_days": OPEN_BLOCK_HORIZON_DAYS,
        "short_notice": False,
        "phase_weeks": {
            OPEN_BLOCK_PHASE: OPEN_BLOCK_WEEKS,
            "days": {phase: (days if phase == OPEN_BLOCK_PHASE else 0) for phase in ("GPP", "SPP", "TAPER")},
        },
    }


def _open_block_session(role: dict[str, Any]) -> dict[str, Any] | None:
    role_key = str(role.get("role_key") or "").strip().lower()
    if role_key in _OPEN_BLOCK_SERVER_OWNED:
        return None
    weekday = str(role.get("scheduled_day_hint") or role.get("real_weekday") or "").strip().lower()
    if weekday not in _WEEKDAY_ORDER:
        return None
    doses = {
        str(item.get("name") or ""): str(item.get("effective_prescription") or "")
        for item in role.get("effective_strength_prescriptions") or []
        if isinstance(item, dict)
    }
    exercises = [
        {
            "name": str(member.get("name") or ""),
            "prescription": doses.get(str(member.get("name") or ""))
            or str(member.get("effective_prescription") or member.get("base_prescription") or ""),
        }
        for member in role.get("selected_exercise_assignments") or []
        if isinstance(member, dict) and member.get("name")
    ]
    session = {
        "weekday": weekday.title(),
        "role_key": role_key,
        "label": role.get("athlete_facing_label") or role_key.replace("_", " ").capitalize(),
        "category": role.get("category"),
        "execution_order": role.get("execution_order"),
        "coach_owned": bool(role.get("coach_owned")),
        "display_text": role.get("display_text"),
        "support_insert_category": role.get("support_insert_category"),
        "support_insert_cost_category": role.get("support_insert_cost_category"),
        "exercises": exercises,
    }
    return {key: value for key, value in session.items() if value not in (None, "", [])}


def _project_open_development_block(block: dict[str, Any]) -> dict[str, Any]:
    """The block's final sessions by week and weekday -- no D-days, no phases."""
    weeks = []
    for week in (block.get("weekly_role_map") or {}).get("weeks") or []:
        if not isinstance(week, dict):
            continue
        sessions = [
            session
            for role in week.get("session_roles") or []
            if isinstance(role, dict) and (session := _open_block_session(role))
        ]
        sessions.sort(key=lambda item: (
            _WEEKDAY_ORDER.index(item["weekday"].lower()), item.get("execution_order") or 0,
        ))
        weeks.append({
            "week": len(weeks) + 1,
            "focus": week.get("stage_label") or "",
            "sessions": sessions,
            "removed_sessions": [
                {"role_key": row.get("role_key"), "reason_code": row.get("reason_code")
                 or (row.get("reason_codes") or row.get("compression_reason_codes") or ["unspecified_suppression"])[0],
                 "reason_codes": row.get("reason_codes") or row.get("compression_reason_codes") or [],
                 "reasons": row.get("reasons") or []}
                for row in week.get("suppressed_roles") or []
                if isinstance(row, dict) and row.get("role_key") not in _OPEN_BLOCK_SERVER_OWNED
            ],
        })
    return {"authority": "deterministic_open_development_block", "weeks": weeks}


def _project_open_goal_coverage(block: dict[str, Any]) -> list[dict[str, Any]]:
    """Goal coverage from the block's final membership, by week and weekday."""
    weekday_by_d_day = {
        day.get("d_day"): str(day.get("weekday") or "").title()
        for week in (block.get("weekly_role_map") or {}).get("weeks") or []
        for day in week.get("calendar_days") or []
        if isinstance(day, dict)
    }
    week_by_d_day = {
        day.get("d_day"): index + 1
        for index, week in enumerate((block.get("weekly_role_map") or {}).get("weeks") or [])
        for day in week.get("calendar_days") or []
        if isinstance(day, dict)
    }
    from .goal_preservation import validate_goal_preservation
    contract_errors = validate_goal_preservation(block)
    coverage = []
    for entry in block.get("goal_preservation") or []:
        if not isinstance(entry, dict):
            continue
        coverage.append({
            "goal": entry.get("goal"),
            "priority": entry.get("priority"),
            "state": entry.get("state"),
            "satisfied": entry.get("satisfied"),
            "contract_errors": [error for error in contract_errors if error.get("goal") in {None, entry.get("goal")}],
            "reason_codes": entry.get("reason_codes") or [],
            "constraints": entry.get("constraints") or [],
            "evidence": [
                {"name": item.get("name"), "week": week_by_d_day.get(item.get("d_day")),
                 "weekday": weekday_by_d_day.get(item.get("d_day"))}
                for item in entry.get("evidence") or []
                if isinstance(item, dict) and item.get("name")
            ],
        })
    return coverage


_OPEN_BLOCK_RENDER_RULES = [
    "open_plan_spec.weekly_session_plan is the deterministic planner's session plan. Build the Weekly Rhythm and Session Cards from it: each session's weekday, purpose and order are fixed.",
    "Each session's exercises list is closed membership with its authoritative prescription: render every listed exercise with that dose (one exact value inside any range), and never add, replace or substitute an exercise. A session with no exercises is coach-owned or support work: keep it as written in its label.",
    "Use weekly_session_plan weeks 2-4 for the 4-Week Development Block: describe what changes week to week from those sessions, never by inventing new ones.",
    "open_plan_spec.goal_coverage is the planner's verdict on the athlete's goals: explain it faithfully and never claim a goal is trained by an exercise not listed in its evidence.",
]


def _attach_open_development_block(
    brief: dict, *, athlete_model: dict, restrictions: list[dict], phase_briefs: dict[str, dict],
    candidate_pools: dict[str, dict], omission_ledger: dict[str, dict], rewrite_guidance: dict,
    plan_input: Any | None, computed_support: dict | None,
) -> dict:
    """Give an open plan the dated planner's intelligence without its periodisation.

    One four-week development block runs through the canonical owners -- role
    budget, placement and legality, closed membership, effective dose,
    realised-load revalidation, goal preservation and empty-session suppression
    -- on a calendar placed far from any fight. Only its weekday projection
    reaches the open payload; no D-day, phase or countdown concept does.
    """
    from .goal_preservation import reconcile_goal_preservation, refresh_goal_evidence_after_composition
    from .session_composition import consolidate_compatible_physical_sessions, suppress_unfilled_physical_roles
    from .session_sequencing import stamp_role_execution_order

    phase_brief = phase_briefs.get(OPEN_BLOCK_PHASE)
    sources = [candidate_pools.get(phase) for phase in _OPEN_BLOCK_SOURCE_PHASES]
    sources = [pool for pool in sources if isinstance(pool, dict)]
    if not sources or not isinstance(phase_brief, dict):
        return brief
    def merged(group: str) -> list[dict[str, Any]]:
        # Both pools were scored by the same Stage 1 function, so the merged
        # list follows that score; otherwise every general slot would outrank
        # every specific one purely by concatenation order.
        slots = [slot for pool in sources for slot in pool.get(group) or [] if isinstance(slot, dict)]
        ranked = sorted(slots, key=lambda slot: -float(_slot_selected_option(slot).get("score") or 0))
        if group != "strength_slots":
            return ranked
        unique = {}
        # Reserve every original selection before filling a losing duplicate's
        # slot, so its backup cannot displace a different selected exercise.
        selected_names = {
            normalize_exercise_key(_slot_selected_option(slot).get("name"))
            for slot in ranked
        }
        for slot in ranked:
            key = normalize_exercise_key(_slot_selected_option(slot).get("name")) or slot.get("slot_id") or id(slot)
            if key in unique:
                if slot.get("replace_with_same_role") is not True:
                    continue
                replacement = next((
                    option for option in sorted(
                        (option for option in slot.get("alternates") or [] if isinstance(option, dict)),
                        key=lambda option: -float(option.get("score") or 0),
                    )
                    if normalize_exercise_key(option.get("name"))
                    and normalize_exercise_key(option["name"]) not in selected_names
                    and normalize_exercise_key(option["name"]) not in unique
                    and str(option.get("prescription") or "").strip()
                ), None)
                if replacement is None:
                    continue
                slot = {
                    **slot,
                    **{field: replacement[field] for field in (
                        "quality_class", "anchor_capable", "support_only",
                        "meaningful_stress", "fulfillment_authority", "base_categories",
                    ) if field in replacement},
                    "selected": replacement,
                    "purpose": replacement.get("why") or slot.get("purpose"),
                    "alternates": [option for option in slot.get("alternates") or []
                                   if option is not replacement],
                }
                key = normalize_exercise_key(replacement["name"])
            unique[key] = slot
        return sorted(unique.values(), key=lambda slot: -float(_slot_selected_option(slot).get("score") or 0))

    block_pools = {OPEN_BLOCK_PHASE: {group: merged(group) for group in _OPEN_BLOCK_SLOT_GROUPS}}
    block = _build_planning_brief(
        athlete_model=_open_block_athlete_view(athlete_model),
        restrictions=restrictions,
        phase_briefs={OPEN_BLOCK_PHASE: {**phase_brief, "days": 7 * OPEN_BLOCK_WEEKS, "weeks": OPEN_BLOCK_WEEKS}},
        candidate_pools=block_pools, omission_ledger=omission_ledger,
        rewrite_guidance=rewrite_guidance, plan_input=plan_input, computed_support=computed_support,
    )
    if "weekly_role_map" not in block:
        return brief
    block = reconcile_goal_preservation(block)
    role_map = block["weekly_role_map"]
    consolidate_compatible_physical_sessions(role_map, athlete_model=_open_block_athlete_view(athlete_model))
    if role_map.get("session_consolidation_audit"):
        refresh_goal_evidence_after_composition(block)
    block = stamp_role_execution_order(block)
    suppress_unfilled_physical_roles(block)
    spec = dict(brief.get("open_plan_spec") or {})
    spec["weekly_session_plan"] = _project_open_development_block(block)
    spec["goal_coverage"] = _project_open_goal_coverage(block)
    spec["render_rules"] = [*(spec.get("render_rules") or []), *_OPEN_BLOCK_RENDER_RULES]
    return {**brief, "open_plan_spec": spec, "candidate_pools": block_pools}


def build_planning_brief(
    *, athlete_model: dict, restrictions: list[dict], phase_briefs: dict[str, dict],
    candidate_pools: dict[str, dict], omission_ledger: dict[str, dict],
    rewrite_guidance: dict, plan_input: Any | None = None,
    computed_support: dict | None = None,
) -> dict:
    from .goal_preservation import reconcile_goal_preservation
    from .session_sequencing import stamp_role_execution_order

    brief = _build_planning_brief(
        athlete_model=athlete_model, restrictions=restrictions, phase_briefs=phase_briefs,
        candidate_pools=candidate_pools, omission_ledger=omission_ledger,
        rewrite_guidance=rewrite_guidance, plan_input=plan_input, computed_support=computed_support,
    )
    # An open (undated) plan has no fight and no periodisation, but its sessions
    # are still decided by the deterministic planner: one development block is
    # built through the same owners as a dated camp and projected by weekday.
    if brief.get("payload_variant") == "open_ongoing_stage2_payload":
        return _attach_open_development_block(
            brief,
            athlete_model=athlete_model, restrictions=restrictions, phase_briefs=phase_briefs,
            candidate_pools=candidate_pools, omission_ledger=omission_ledger,
            rewrite_guidance=rewrite_guidance, plan_input=plan_input, computed_support=computed_support,
        )
    if "late_fight_session_sequence" in brief:
        from .goal_preservation import _effective_map
        # Resolve the actual final sequence, not the earlier weekly mirror.
        role_map = _effective_map(brief)
        apply_effective_strength_prescriptions(
            weekly_role_map=role_map, candidate_pools=candidate_pools, athlete_model=athlete_model,
        )
        # Re-stamp each composed countdown session from its resolved dose, the
        # same realised-load measurement the long-camp path applies.
        _refresh_stress_stamps(role_map)
    # Within-day execution order is decided once the calendar and membership are
    # final; it reads the finished days and writes only ``execution_order``.
    brief = reconcile_goal_preservation(brief)
    from .goal_preservation import _effective_map, refresh_goal_evidence_after_composition
    from .session_composition import consolidate_compatible_physical_sessions
    role_map = _effective_map(brief)
    consolidate_compatible_physical_sessions(role_map, athlete_model=athlete_model)
    if role_map.get("session_consolidation_audit"):
        refresh_goal_evidence_after_composition(brief)
    # Goal repair and consolidation can move the physical session a mental
    # task was attached to; govern mental support again on the final calendar.
    from .camp_week_fillers_impl import govern_weekly_mental_training
    govern_weekly_mental_training(role_map, athlete_model)
    enforce_total_session_budget(role_map, athlete_model, candidate_pools)
    if "late_fight_session_sequence" in brief:
        # _effective_map wraps the authoritative sequence in a temporary week;
        # removals rebind that week's list, so write the final roles back.
        final = [role for week in role_map.get("weeks") or [] for role in week.get("session_roles") or []]
        sequence = brief["late_fight_session_sequence"]
        sequence[:] = final
        spec = brief.get("late_fight_plan_spec")
        if isinstance(spec, dict) and spec.get("visible_session_sequence") is not sequence:
            spec["visible_session_sequence"] = list(final)
    return stamp_role_execution_order(brief)


def _build_planning_brief(
    *,
    athlete_model: dict,
    restrictions: list[dict],
    phase_briefs: dict[str, dict],
    candidate_pools: dict[str, dict],
    omission_ledger: dict[str, dict],
    rewrite_guidance: dict,
    plan_input: Any | None = None,
    computed_support: dict | None = None,
) -> dict:
    athlete_model = dict(athlete_model)
    # Persist the explicit resolved primary goal (from plan_input when present)
    # onto the athlete model before classification. Open/ongoing payloads return
    # before reconciliation, so compressed_priorities.goal_preservation is their
    # only goal-state authority; without this, classification would assume the
    # first key_goals entry is primary whenever athlete_model omits primary_goal.
    _resolved_primary = build_priority_profile(
        plan_input if plan_input is not None else athlete_model
    ).primary_goal
    if _resolved_primary:
        athlete_model["primary_goal"] = _resolved_primary
    # Compute short-camp compression up front so downstream consumers (role
    # map, weekly stress map, planning brief output) see a real
    # ``compressed_priorities`` dict. The helper is otherwise dead code, which
    # leaves the field empty whenever a caller skips ``build_stage2_payload``
    # and goes straight to ``build_planning_brief``.
    if not athlete_model.get("compressed_priorities"):
        athlete_model["compressed_priorities"] = _compress_short_camp_priorities(athlete_model)
    rewrite_guidance = _append_render_guard_writing_rules(rewrite_guidance, athlete_model=athlete_model, days_until_fight=athlete_model.get("days_until_fight"))
    days_until_fight = athlete_model.get("days_until_fight")
    priority_source = plan_input if plan_input is not None else athlete_model
    priority_profile = build_priority_profile(priority_source)
    collision_details = getattr(plan_input, "goal_weakness_collision_details", None) if plan_input is not None else None
    primary_collision_detail = ""
    if isinstance(collision_details, list) and collision_details:
        first = collision_details[0]
        if isinstance(first, dict):
            primary_collision_detail = str(first.get("detail", "") or "")
    priority_focus = describe_priority_focus(
        priority_profile,
        collision_detail=primary_collision_detail or (getattr(plan_input, "goal_weakness_collision_detail", "") if plan_input is not None else ""),
        collision_tags=getattr(plan_input, "goal_weakness_collision_tags", None) if plan_input is not None else None,
        collision_details=collision_details if isinstance(collision_details, list) else None,
    )

    if _uses_open_ongoing_payload(athlete_model):
        open_payload = build_open_ongoing_payload(athlete_model=athlete_model)
        return {
            "schema_version": "planning_brief.v1",
            "generator_mode": "deterministic_open_ongoing_planner_plus_ai_finalizer",
            "payload_variant": "open_ongoing_stage2_payload",
            "payload_mode": open_payload.get("payload_mode"),
            "render_mode": open_payload.get("render_mode"),
            "athlete_snapshot": athlete_model,
            "open_plan_spec": open_payload.get("open_plan_spec") or {},
            "priority_focus": priority_focus,
            "restrictions": restrictions,
            "candidate_pools": candidate_pools,
            "omission_ledger": omission_ledger,
            "decision_rules": rewrite_guidance,
            "computed_support": computed_support or {},
        }

    limiter_profile = _build_limiter_profile(athlete_model, restrictions)
    sport_load_profile = _build_sport_load_profile(athlete_model)
    weekly_stress_map = _build_weekly_stress_map(
        athlete_model,
        phase_briefs,
        limiter_profile,
        sport_load_profile,
    )
    week_by_week_progression = _build_week_by_week_progression(
        athlete_model,
        phase_briefs,
        weekly_stress_map,
    )

    if _uses_late_fight_stage2_payload(days_until_fight):
        fight_week_override = _fight_week_override_payload(days_until_fight)
        days_out_payload = _days_out_payload_block(days_until_fight, athlete_model)
        late_fight_phase = _resolve_late_fight_phase(phase_briefs)
        late_fight_progression = _build_late_fight_week_by_week_progression(days_until_fight, athlete_model, phase_briefs)
        weekly_role_map = _build_late_fight_weekly_role_map(
            days_until_fight,
            athlete_model,
            fight_week_override,
            phase=late_fight_phase,
        )
        weekly_role_map = apply_fight_day_override_to_weekly_role_map(weekly_role_map, athlete_model)
        weekly_role_map = stamp_weekly_role_map_labels(weekly_role_map)
        base_late_fight_plan_spec = _build_late_fight_plan_spec(
            days_until_fight,
            athlete_model,
        )

        pre_gap_sequence = ensure_declared_coach_combat_spine(
            list(
                base_late_fight_plan_spec.get("session_sequence")
                or base_late_fight_plan_spec.get("visible_session_sequence")
                or _build_late_fight_session_sequence(days_until_fight, athlete_model)
            ),
            athlete_model,
            dict(base_late_fight_plan_spec.get("countdown_weekday_map", {})),
        )
        session_sequence, _occupancy_unused = complete_late_fight_sequence_occupancy(
            _visible_calendar_session_sequence(
                apply_gap_fill_inserts(pre_gap_sequence, athlete_model)
            ),
            athlete_model,
            countdown_weekday_map=dict(
                base_late_fight_plan_spec.get("countdown_weekday_map", {})
            ),
        )
        _record_late_fight_unused_days(weekly_role_map, _occupancy_unused)

        late_fight_plan_spec = _with_late_fight_allowed_exercises(
            spec={
                **base_late_fight_plan_spec,
                # Carry the authoritative late-fight phase so goal-preservation's
                # _effective_map resolves the effective dose against the pool that
                # actually produced this sequence, not an arbitrary first pool.
                "phase": late_fight_phase,
                "visible_session_sequence": session_sequence,
                **late_fight_sequence_budget(
                    session_sequence, base_late_fight_plan_spec.get("role_budget")
                ),
            },
            candidate_pools=candidate_pools,
            days_until_fight=days_until_fight,
        )

        return {
            "schema_version": "planning_brief.v1",
            "generator_mode": "deterministic_late_fight_planner_plus_ai_finalizer",
            "payload_variant": "late_fight_stage2_payload",
            "athlete_snapshot": athlete_model,
            "days_out_payload": days_out_payload,
            "late_fight_plan_spec": late_fight_plan_spec,
            "late_fight_session_sequence": session_sequence,
            "fight_demands": {
                "sport": athlete_model.get("sport"),
                "status": athlete_model.get("status"),
                "rounds_format": athlete_model.get("rounds_format"),
                "camp_length_weeks": athlete_model.get("camp_length_weeks"),
                "days_until_fight": days_until_fight,
                "short_notice": athlete_model.get("short_notice"),
            },
            "archetype_summary": _derive_athlete_archetype(athlete_model),
            "main_limiter": _derive_main_limiter(athlete_model),
            "compressed_priorities": athlete_model.get("compressed_priorities", {}),
            "priority_focus": priority_focus,
            "limiter_profile": limiter_profile,
            "sport_load_profile": sport_load_profile,
            "decision_hierarchy": PLANNING_DECISION_HIERARCHY,
            "main_risks": _derive_main_risks(athlete_model, restrictions),
            "global_priorities": _derive_global_priorities(athlete_model, phase_briefs, candidate_pools),
            "phase_strategy": _build_phase_strategy(phase_briefs, candidate_pools, late_fight_progression),
            "weekly_stress_map": weekly_stress_map,
            "week_by_week_progression": late_fight_progression,
            "fight_week_override": fight_week_override or {"active": False},
            "weekly_role_map": weekly_role_map,
            "rendering_rules": days_out_payload.get("rendering_rules", {}),
            "restrictions": restrictions,
            "candidate_pools": candidate_pools,
            "omission_ledger": omission_ledger,
            "decision_rules": rewrite_guidance,
            "computed_support": computed_support or {},
        }

    fight_week_override = _fight_week_override_payload(days_until_fight)
    weekly_role_map = stage2_role_map_module._build_weekly_role_map(
        athlete_model,
        week_by_week_progression,
        limiter_profile,
        fight_week_override=fight_week_override,
    )
    # The role map owns the crowded-week policy and every role-survival choice.
    # Payload only decorates its already-final decision with day-identity fields.
    _apply_boxing_crowded_week_post_processing(
        weekly_role_map,
        athlete_model=athlete_model,
    )
    # Deterministically place any session role the planner left dayless, then
    # stamp labels. Post-processing can append suppressed/omitted roles after the
    # inner builder ran, so do both here for full coverage.
    fill_missing_session_days(weekly_role_map)
    # Add low-cost support fillers to SPP/TAPER weeks (free days first, then at
    # most one shared day) using the same insert policy as the late-fight path.
    apply_camp_week_fillers(weekly_role_map, athlete_model)
    compose_normal_strength_assignments(
        weekly_role_map=weekly_role_map, candidate_pools=candidate_pools,
    )
    # Late-camp role morph: hard fight-pace/glycolytic conditioning scheduled at
    # D-13 or closer softens to a low-cost rhythm touch. Runs last so no quota
    # or protected-slot rule can preserve hard glycolytic work inside D-13; the
    # D-21→D-18 combat-pressure floor is untouched by construction.
    apply_late_camp_role_morph(weekly_role_map, athlete_model)
    # Composition runs first because its own recovery mover's relocations must
    # be morphed and verified by the governor. Any role moved after its
    # membership was chosen -- by that mover or by the governor -- is recomposed
    # for the day it is on, through the same owners and with a fixed bound.
    reconcile_relocated_strength(
        weekly_role_map,
        candidate_pools=candidate_pools,
        replace_callback=lambda role_map: apply_late_camp_role_morph(role_map, athlete_model),
    )
    # The initial role budget ran before selected exercise density was known.
    # Its own post-composition reservation offers an unused pre-contact day
    # only when a surviving main strength session is genuinely crowded; this
    # cannot displace conditioning, increase appointment capacity, or create a
    # second strength role without a real >=2-member transfer later.
    stage2_role_map_module.reserve_dense_day_receiver(
        weekly_role_map, athlete_model
    )
    from .pre_hard_contact_strength import apply_pre_hard_contact_strength_exposure_cap
    apply_pre_hard_contact_strength_exposure_cap(weekly_role_map, athlete_model)
    compose_normal_conditioning_assignments(
        weekly_role_map=weekly_role_map, candidate_pools=candidate_pools,
    )
    compose_normal_rehab_assignments(
        weekly_role_map=weekly_role_map, candidate_pools=candidate_pools,
    )
    # The long-camp splice owns D-13 inward, but uses the same selector and
    # assignment schema as direct late-fight generation.
    tail_roles = [
        role for week in weekly_role_map.get("weeks", []) or []
        for role in week.get("session_roles", []) or []
        if isinstance(role, dict) and role.get("late_fight_tail_owned")
    ]
    if tail_roles:
        _, tail_assignments = _build_late_fight_allowed_exercises_by_day(
            spec={"visible_session_sequence": tail_roles}, candidate_pools=candidate_pools,
        )
        for week in weekly_role_map.get("weeks", []) or []:
            drop_displaced_occupancy_fillers(week.get("session_roles") or [])
        attach_late_fight_assignments(tail_roles, tail_assignments)
    # A provisional receiver is legal only when existing source exercises
    # actually move. If the selected bank cannot fund at least two low-cost
    # members, the role-budget owner cancels the receiver without adding work.
    redistribute_pre_hard_power_from_main(
        weekly_role_map=weekly_role_map,
        candidate_pools=candidate_pools,
        athlete_model=athlete_model,
    )
    # Deterministic prescription resolution: now that every strength role has its
    # scheduled D-day and final countdown dose envelope, resolve the single
    # authoritative effective prescription per selected strength exercise so
    # Stage 2 never has to reconcile the exercise-bank dose against the role caps.
    # Runs AFTER the morph (which owns dose shaping) and BEFORE label stamping.
    def _resolve_strength_doses(role_map: dict) -> dict:
        return apply_effective_strength_prescriptions(
            weekly_role_map=role_map,
            candidate_pools=candidate_pools,
            athlete_model=athlete_model,
        )

    _resolve_strength_doses(weekly_role_map)
    # Composition plus dose resolution is the first point at which a session's
    # real cross-day cost is knowable. Until here the calendar has only judged
    # roles by the promise their role key makes, and a strength assignment
    # carried only its raw bank dose. Re-ask the canonical policy now that both
    # exercises and authoritative doses exist. Relocation only, to a strictly
    # cleaner slot; never a forced rest day. A relocated role is re-morphed and
    # re-dosed for the day it lands on.
    def _recompose_conditioning(role_map: dict, only_roles: set) -> dict:
        return compose_normal_conditioning_assignments(
            weekly_role_map=role_map,
            candidate_pools=candidate_pools,
            only_roles=only_roles,
        )

    def _recompose_strength(role_map: dict, only_roles: set) -> dict:
        # The revalidation owns placement here; composition only fills the
        # destination day.
        return compose_normal_strength_assignments(
            weekly_role_map=role_map,
            candidate_pools=candidate_pools,
            only_roles=only_roles,
            allow_relocation=False,
        )

    # A proposed extra standalone-strength exercise is kept only when the
    # revalidation below would place every session exactly as it does without it.
    apply_standalone_strength_minimum(
        weekly_role_map,
        candidate_pools=candidate_pools,
        redose_callback=_resolve_strength_doses,
        recompose_conditioning_callback=_recompose_conditioning,
        recompose_strength_callback=_recompose_strength,
    )
    apply_realised_load_calendar_revalidation(
        weekly_role_map,
        redose_callback=_resolve_strength_doses,
        recompose_conditioning_callback=_recompose_conditioning,
        recompose_strength_callback=_recompose_strength,
    )
    # Do not leave the source session partially stripped if canonical calendar
    # governance relocates/suppresses the receiving day or replaces its members.
    # Restore exact original selected work and re-dose through the same owner.
    if rollback_invalid_dense_day_redistributions(
        weekly_role_map, athlete_model=athlete_model
    ):
        _resolve_strength_doses(weekly_role_map)
        _refresh_stress_stamps(weekly_role_map)
    else:
        _resolve_strength_doses(weekly_role_map)
    # Physical placement and budgets are final: mental support must still be
    # attached to a surviving appointment, including across the tail splice.
    from .camp_week_fillers_impl import govern_weekly_mental_training
    govern_weekly_mental_training(weekly_role_map, athlete_model)
    weekly_role_map = stamp_weekly_role_map_labels(weekly_role_map)
    return {
        "schema_version": "planning_brief.v1",
        "generator_mode": "deterministic_planner_plus_ai_finalizer",
        "athlete_snapshot": athlete_model,
        "fight_demands": {
            "sport": athlete_model.get("sport"),
            "status": athlete_model.get("status"),
            "rounds_format": athlete_model.get("rounds_format"),
            "camp_length_weeks": athlete_model.get("camp_length_weeks"),
            "days_until_fight": days_until_fight,
            "short_notice": athlete_model.get("short_notice"),
        },
        "archetype_summary": _derive_athlete_archetype(athlete_model),
        "main_limiter": _derive_main_limiter(athlete_model),
        "compressed_priorities": athlete_model.get("compressed_priorities", {}),
        "priority_focus": priority_focus,
        "limiter_profile": limiter_profile,
        "sport_load_profile": sport_load_profile,
        "decision_hierarchy": PLANNING_DECISION_HIERARCHY,
        "main_risks": _derive_main_risks(athlete_model, restrictions),
        "global_priorities": _derive_global_priorities(athlete_model, phase_briefs, candidate_pools),
        "phase_strategy": _build_phase_strategy(phase_briefs, candidate_pools, week_by_week_progression),
        "weekly_stress_map": weekly_stress_map,
        "week_by_week_progression": _with_final_compression(week_by_week_progression, weekly_role_map),
        "fight_week_override": fight_week_override or {"active": False},
        "weekly_role_map": weekly_role_map,
        "restrictions": restrictions,
        "candidate_pools": candidate_pools,
        "omission_ledger": omission_ledger,
        "decision_rules": rewrite_guidance,
        "computed_support": computed_support or {},
    }

def _with_selection_evidence(option: dict, item: dict, score_evidence: dict | None = None) -> dict:
    evidence = build_score_evidence(score_evidence=score_evidence)
    option.update(
        {
            "score": evidence["score"],
            "reason_codes": evidence["reason_codes"],
            "penalties": evidence["penalties"],
            "restriction_hits": evidence["restriction_hits"],
            "late_window_adjustment": evidence["late_window_adjustment"],
            "score_evidence": evidence,
            "selection_metadata": normalize_selection_metadata(item),
        }
    )
    return option


def _why_log_score_evidence(why_entry: dict) -> dict:
    reasons = why_entry.get("reasons", {}) if isinstance(why_entry, dict) else {}
    explanation = why_entry.get("explanation") if isinstance(why_entry, dict) else None
    return build_score_evidence(reasons=reasons, explanation=explanation)


_ADAPTATION_ORDER = ("strength", "power", "speed", "trunk_strength")


def _serialize_strength_option(exercise: dict, why: str, score_evidence: dict | None = None, *, phase: str | None = None) -> dict:
    movement = str(exercise.get("movement", "")).strip().lower().replace(" ", "_")
    movement_patterns = [movement] if movement else []
    movement_patterns.extend(clean_list(exercise.get("tags", [])))
    quality_profile = classify_strength_item(exercise)
    meaningful_authority = has_meaningful_fulfillment_authority(
        exercise,
        source_kind="strength",
        source=str(exercise.get("_schema_source") or ""),
        allow_legacy_unstamped=True,
    )
    required_equipment = clean_list(exercise.get("required_equipment") or exercise.get("equipment", []))
    # The serialized slot keeps no method or authored category, so the item's
    # adaptation relevance travels in the existing selection-metadata fields.
    # Nothing found stays "unknown": unreviewed is not negative evidence.
    from .strength import _classify_prescription_type, _prescription_templates, _unrequired_space_tokens
    adaptations = [
        adaptation
        for adaptation in _ADAPTATION_ORDER
        if adaptation in strength_adaptation_relevance(exercise, prescription_type=_classify_prescription_type(exercise))
    ]
    if adaptations:
        exercise = {
            **exercise,
            "primary_adaptation": adaptations[0],
            "secondary_adaptations": adaptations[1:],
        }
    prescription = exercise.get("prescription") or ""
    if not prescription and phase:
        # Carry the same actual bank-template dose that Stage 1 renders. A method
        # token such as "strength" or "power" is not an effective prescription.
        prescription = _prescription_templates(phase)[_classify_prescription_type(exercise)]
    return _with_selection_evidence({
        "name": exercise.get("name", "Unnamed"),
        "source": "exercise_bank",
        "movement_patterns": dedupe_preserve_order(movement_patterns),
        "restriction_tags": _extract_restriction_tags(exercise),
        "mechanical_risk_tags": _extract_mechanical_risk_tags(exercise),
        "prescription": prescription or exercise.get("method") or "",
        "notes": str(exercise.get("notes") or "").strip(),
        "real_strength_maintenance": exercise.get("real_strength_maintenance") is True,
        "why": why or "balanced selection",
        "quality_class": quality_profile["quality_class"],
        "anchor_capable": quality_profile["anchor_capable"] and meaningful_authority,
        "support_only": exercise.get("support_only") if isinstance(exercise.get("support_only"), bool) else quality_profile["support_only"],
        "meaningful_stress": exercise.get("meaningful_stress"),
        "fulfillment_authority": meaningful_authority,
        "base_categories": quality_profile["base_categories"],
        "required_equipment": required_equipment,
        # Running space is described, not owned: Stage 1 selection does not require it.
        "universally_available": not required_equipment
        or set(required_equipment).issubset({"bodyweight", *_unrequired_space_tokens(exercise)}),
        "generic_fallback": bool(exercise.get("generic_fallback")),
    }, exercise, score_evidence)


def _with_final_compression(progression: dict, role_map: dict) -> dict:
    """Mirror the role map's final compression onto the progression copy.

    Post-allocation passes (e.g. the pre-hard-contact strength cap) update only
    the role map. The progression must not disagree with it, because the
    Stage 2 packet drops this duplicate on the strength of their parity.
    """
    final = {week.get("week_index"): week for week in (role_map or {}).get("weeks") or []
             if isinstance(week, dict)}
    weeks = []
    for week in (progression or {}).get("weeks") or []:
        source = final.get(week.get("week_index")) if isinstance(week, dict) else None
        if source is not None and "intentional_compression" in source:
            week = {**week, "intentional_compression": deepcopy(source["intentional_compression"])}
        elif source is not None and "intentional_compression" in week:
            week = {key: value for key, value in week.items() if key != "intentional_compression"}
        weeks.append(week)
    return {**progression, "weeks": weeks} if isinstance(progression, dict) and "weeks" in progression else progression


def _serialize_conditioning_option(
    drill: dict,
    system: str,
    why: str,
    *,
    late_window: str | None = None,
    score_evidence: dict | None = None,
    stance: str | None = None,
) -> dict:
    tags = clean_list(drill.get("tags", []))
    required_equipment = clean_list(drill.get("required_equipment") or drill.get("equipment", []))
    option = {
        "name": drill.get("name", "Unnamed"),
        "source": (
            "style_taper"
            if str(drill.get("_schema_source") or "").endswith("style_taper_conditioning.json")
            else "conditioning_bank"
        ),
        "movement_patterns": dedupe_preserve_order([system] + tags),
        "restriction_tags": _extract_restriction_tags(drill),
        "mechanical_risk_tags": _extract_mechanical_risk_tags(drill),
        "notes": str(drill.get("notes") or "").strip(),
        # Conditioning drills author their dose in ``duration`` ("4x5/side, 2min
        # rest", "45s work / 45s rest x 6 rounds"); only a minority also carry
        # timing/rest/load. Reading just the latter three serialized EVERY
        # conditioning entry in the banks to an empty prescription, so Stage 2
        # was handed a scheduled drill with no dose and had to invent one — the
        # deterministic dose the planner already held never reached the
        # finalizer, and the invented phrasing then failed render matching.
        "prescription": " | ".join(
            part
            for part in [drill.get("timing"), drill.get("rest"), drill.get("load")]
            if part
        ) or str(drill.get("duration") or "").strip(),
        "why": why or "balanced selection",
        "required_equipment": required_equipment,
        "universally_available": not required_equipment or set(required_equipment).issubset({"bodyweight"}),
        "generic_fallback": bool(drill.get("generic_fallback")),
        "availability_contingency_reason": drill.get("availability_contingency_reason") or "",
        "session_index": drill.get("session_index"),
        "athlete_facing_system_label": athlete_facing_system_label(drill, late_window=late_window),
        "support_only": drill.get("support_only") is True,
        "meaningful_stress": drill.get("meaningful_stress") is True,
        "fulfillment_authority": has_meaningful_fulfillment_authority(
            drill,
            source_kind="conditioning",
            source=str(drill.get("_schema_source") or ""),
            allow_legacy_unstamped=True,
        ),
    }
    if drill.get("modality") == "technical_footwork":
        prescription_fields = technical_footwork_prescription_fields(drill, stance=stance)
        option["prescription"] = " | ".join(
            part
            for part in [
                prescription_fields["timing"],
                prescription_fields["rest"],
                drill.get("load") or drill.get("intensity"),
            ]
            if part
        )
        option["technical_footwork_prescription"] = {
            key: drill[key]
            for key in (
                "cue_source",
                "cue",
                "cue_execution",
                "side_rule",
                "sets",
                "reps",
                "reps_per_side",
                "rest_sec",
                "quality_stop_rule",
            )
            if key in drill
        }
        # Resolved athlete-facing side/stance instruction, not just the raw
        # side_rule enum, so the finalizer never has to re-derive it from
        # athlete_snapshot.stance on its own.
        option["technical_footwork_prescription"]["side_instruction"] = prescription_fields[
            "side_instruction"
        ]
    option = _with_selection_evidence(option, drill, score_evidence)
    option["system"] = drill.get("system") or system
    option["bank_order"] = drill.get("bank_order")
    # Retain original phase, sport, style and safety authority on each option.
    option["selection_metadata"] = {**deepcopy(drill), **option["selection_metadata"]}
    return option


def _build_late_tail_candidates(
    phase_block: dict | None,
    phase: str,
    *,
    stance=None,
    required_windows: set[str] | None = None,
) -> list[dict]:
    """Serialize the existing qualified reservoir without phase-slot truncation.

    Style-taper drills are always carried. Beyond those, a reservoir candidate is
    also carried when it is legally usable in a late window this camp actually
    reaches: Stage 1 had already qualified and scored it, but the handoff used to
    drop it, so the dated selector could find no legal candidate and left the
    session empty. Nothing new is selected here - this only stops the handoff
    discarding coverage Stage 1 already produced.
    """
    required_windows = {w for w in (required_windows or set()) if w}
    slots = []
    seen: set[str] = set()
    for system, candidates in ((phase_block or {}).get("candidate_reservoir") or {}).items():
        if str(system).startswith("__"):
            continue
        for candidate in candidates:
            drill = candidate.get("drill") or {}
            name = str(drill.get("name") or "").strip()
            if not name or name in seen:
                continue
            is_style_taper = str(drill.get("_schema_source") or "").endswith(
                "style_taper_conditioning.json"
            )
            if is_style_taper:
                if system not in {"alactic", "aerobic"}:
                    continue
            else:
                drill_windows = _normalise_late_window_tokens(drill.get("late_windows"))
                if "all" not in drill_windows and not (drill_windows & required_windows):
                    continue
            seen.add(name)
            option = _serialize_conditioning_option(
                drill, system, candidate.get("explanation", ""),
                score_evidence=candidate.get("score_evidence"), stance=stance,
            )
            option["relevance"] = {key: (candidate.get("reasons") or {}).get(key, 0)
                                   for key in ("style_hits", "goal_hits", "weakness_hits")}
            slots.append({"slot_id": f"{phase.lower()}_late_{slugify(name)}",
                          "role": option["system"], "selected": option, "alternates": []})
    return slots


def _serialize_rehab_option(
    prescription: str,
    *,
    role: str,
    source: str,
    why: str,
    function_class: str = "",
    drill_id: str = "",
) -> dict:
    name = re.split(r"\s+(?:[\u2013-]|\u00e2\u20ac\u201c)\s+", prescription, maxsplit=1)[0].strip()
    # Strip any inline [Function: X] tag from the display name
    name = re.sub(r"\s*\[Function:[^\]]*\]", "", name).strip()
    fc = function_class or classify_drill_function(name, prescription)
    function_label = _FUNCTION_LABELS.get(fc, fc.replace("_", " ").title())
    return {
        "name": name or "Rehab Drill",
        "source": source,
        "movement_patterns": [role],
        "restriction_tags": ["rehab", role],
        "mechanical_risk_tags": ["rehab", role],
        "prescription": prescription,
        "why": why,
        "function_class": fc,
        "rehab_function_label": function_label,
        # The canonical rehab-bank id, carried so a completed drill can be
        # attributed to an injury without matching on the display name (which
        # Stage 2 rewrites). Empty when the option did not come from the bank.
        "rehab_drill_id": drill_id,
    }


def _build_strength_alternates(
    strength_block: dict,
    *,
    role: str,
    selected_names: set[str],
    current_name: str,
    phase: str | None = None,
) -> list[dict]:
    alternates: list[dict] = []
    seen: set[str] = set()
    for candidate in (strength_block.get("candidate_reservoir") or {}).get(role, []):
        exercise = candidate.get("exercise", {})
        name = exercise.get("name")
        if not name or name == current_name or name in selected_names or name in seen:
            continue
        alternates.append(
            _serialize_strength_option(
                exercise,
                candidate.get("explanation", "balanced selection"),
                candidate.get("score_evidence")
                or build_score_evidence(
                    score=candidate.get("score"),
                    reasons=candidate.get("reasons") or {},
                    explanation=candidate.get("explanation"),
                ),
                # An alternate promoted via replace_with_same_role must carry the
                # same phase-specific bank-template dose authority as the selected
                # option, never a bare "strength"/"power" method token.
                phase=phase,
            )
        )
        seen.add(name)
        if len(alternates) >= 2:
            break
    return alternates


def _build_conditioning_alternates(
    phase_block: dict,
    *,
    system: str,
    selected_names: set[str],
    current_name: str,
    late_window: str | None = None,
    stance: str | None = None,
) -> list[dict]:
    alternates: list[dict] = []
    seen: set[str] = set()
    for candidate in (phase_block.get("candidate_reservoir") or {}).get(system, []):
        drill = candidate.get("drill", {})
        name = drill.get("name")
        if not name or name == current_name or name in selected_names or name in seen:
            continue
        # A same-system alternate can replace a mandatory system slot only when
        # it carries the same explicit fulfilment authority. Support work stays
        # available in its own support slot; it is not a hidden fallback for a
        # developmental aerobic/glycolytic/alactic assignment.
        if system in {"aerobic", "glycolytic", "alactic"} and not has_meaningful_fulfillment_authority(
            drill,
            source_kind="conditioning",
            source=str(drill.get("_schema_source") or ""),
            allow_legacy_unstamped=True,
        ):
            continue
        alternates.append(
            _serialize_conditioning_option(
                drill,
                system,
                candidate.get("explanation", "balanced selection"),
                late_window=late_window,
                score_evidence=candidate.get("score_evidence")
                or build_score_evidence(
                    score=candidate.get("score"),
                    reasons=candidate.get("reasons") or {},
                    explanation=candidate.get("explanation"),
                ),
                stance=stance,
            )
        )
        seen.add(name)
        if len(alternates) >= 2:
            break
    return alternates


def _parse_rehab_groups(rehab_block: str) -> list[dict]:
    groups: list[dict] = []
    current: dict | None = None

    for raw_line in rehab_block.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        header_match = re.match(r"^-\s+([^()]+?)\s*\(([^)]+)\):\s*$", stripped)
        if header_match:
            current = {
                "location": header_match.group(1).strip(),
                "injury_type": header_match.group(2).strip(),
                "drills": [],
            }
            groups.append(current)
            continue
        bullet_match = re.match(r"^(?:[-*]|[\u2022]|\u00e2\u20ac\u00a2)\s+(.+)$", stripped)
        is_indented = raw_line[:1].isspace()
        if current is not None and bullet_match and (is_indented or stripped.startswith(("\u00e2\u20ac\u00a2", "\u2022", "*"))):
            current["drills"].append(bullet_match.group(1).strip())

    return groups


def _build_strength_slots(strength_block: dict | None, phase: str) -> list[dict]:
    if not strength_block:
        return []
    reason_lookup = {
        entry.get("name"): entry
        for entry in strength_block.get("why_log", [])
        if entry.get("name")
    }
    exercises = list(strength_block.get("exercises", []))
    selected_names = {
        exercise.get("name")
        for exercise in exercises
        if exercise.get("name")
    }
    sessions = infer_strength_sessions(exercises, strength_block.get("num_sessions", 1))
    position_to_session: dict[int, int] = {}
    for session in sessions:
        for position in session.get("positions", []):
            position_to_session[position] = session.get("session_index", 1)
    slots: list[dict] = []
    for idx, exercise in enumerate(exercises, start=1):
        name = exercise.get("name")
        if not name:
            continue
        reasons = reason_lookup.get(name, {})
        movement = str(exercise.get("movement", "")).strip().lower().replace(" ", "_")
        # Mirror the reservoir role key: unclassified movements bucket under
        # "strength_support" there, so the slot must use the same key or the
        # alternate lookup silently returns nothing for jumps/bounds/hops whose
        # movement resolves to "unknown".
        role = movement if movement and movement != "unknown" else "strength_support"
        quality_profile = classify_strength_item(exercise)
        meaningful_authority = has_meaningful_fulfillment_authority(
            exercise,
            source_kind="strength",
            source=str(exercise.get("_schema_source") or ""),
            allow_legacy_unstamped=True,
        )
        slots.append(
            {
                "slot_id": f"{phase.lower()}_strength_{idx}_{slugify(name)}",
                "role": role,
                "purpose": reasons.get("explanation", "balanced selection"),
                "selected": _serialize_strength_option(
                    exercise,
                    reasons.get("explanation", "balanced selection"),
                    _why_log_score_evidence(reasons),
                    phase=phase,
                ),
                "alternates": _build_strength_alternates(
                    strength_block,
                    role=role,
                    selected_names=selected_names,
                    current_name=name,
                    phase=phase,
                ),
                "replace_with_same_role": True,
                "priority": _strength_slot_priority(phase, role, idx),
                "session_index": position_to_session.get(idx - 1, 1),
                "quality_class": quality_profile["quality_class"],
                "anchor_capable": quality_profile["anchor_capable"] and meaningful_authority,
                "support_only": exercise.get("support_only") if isinstance(exercise.get("support_only"), bool) else quality_profile["support_only"],
                "meaningful_stress": exercise.get("meaningful_stress"),
                "fulfillment_authority": meaningful_authority,
                "base_categories": quality_profile["base_categories"],
            }
        )
    return slots


def _build_conditioning_slots(
    phase_block: dict | None,
    phase: str,
    *,
    late_window: str | None = None,
    stance: str | None = None,
) -> list[dict]:
    if not phase_block:
        return []
    reason_lookup = {
        entry.get("name"): entry
        for entry in phase_block.get("why_log", [])
        if entry.get("name")
    }
    selected_names = {
        drill.get("name")
        for drills in (phase_block.get("grouped_drills") or {}).values()
        for drill in drills
        if drill.get("name")
    }
    slots: list[dict] = []
    for system, drills in (phase_block.get("grouped_drills") or {}).items():
        for idx, drill in enumerate(drills, start=1):
            name = drill.get("name")
            if not name:
                continue
            reasons = reason_lookup.get(name, {})
            slots.append(
                {
                    "slot_id": f"{phase.lower()}_{system}_{idx}_{slugify(name)}",
                    "role": system,
                    "purpose": CONDITIONING_ROLE_PURPOSES.get(system, reasons.get("explanation", "balanced selection")),
                    "selected": _serialize_conditioning_option(
                        drill,
                        system,
                        reasons.get("explanation", "balanced selection"),
                        late_window=late_window,
                        score_evidence=_why_log_score_evidence(reasons),
                        stance=stance,
                    ),
                    "alternates": _build_conditioning_alternates(
                        phase_block,
                        system=system,
                        selected_names=selected_names,
                        current_name=name,
                        late_window=late_window,
                        stance=stance,
                    ),
                    "replace_with_same_role": True,
                    "priority": _conditioning_slot_priority(phase, system, idx),
                    "session_index": int(drill.get("session_index", idx) or idx),
                }
            )
    return slots


def _build_rehab_slots(rehab_block: str, phase: str) -> list[dict]:
    if not rehab_block or rehab_block.strip().startswith("**Red Flag Detected**"):
        return []
    slots: list[dict] = []
    for group in _parse_rehab_groups(rehab_block):
        location = group.get("location", "Unspecified")
        injury_type = group.get("injury_type", "unspecified")
        # Surface/skin injuries are wound-care guidance, not prescriptive
        # loading slots. Promoting them to rehab slots makes the finalizer
        # render wound-care "stop rules" as plan content, which then collides
        # with any friction/contact restriction the athlete set on that same
        # wound and falsely holds the plan. Keep them advisory only.
        if _is_surface_type(injury_type):
            continue
        role = f"rehab_{slugify(location)}_{slugify(injury_type)}"
        from .rehab_protocols import is_rehab_guidance_name
        selected_lines = list(dict.fromkeys(line for line in group.get("drills", [])
                                           if line and not is_rehab_guidance_name(line)))
        from .rehab_protocols import reviewed_rehab_drill_id_for_line
        if phase.upper() == "TAPER":
            selected_lines = [line for line in selected_lines if "nordic" not in line.lower()
                              or reviewed_rehab_drill_id_for_line(line, injury_type.lower(), location.lower().replace(" ", "_"))]
            if not selected_lines:
                continue
        selected_set = set(selected_lines)
        rehab_option_records = rehab_drill_options_for_phase(
            injury_type.lower(),
            location.lower().replace(" ", "_"),
            phase,
            limit=6,
        )
        rehab_options = [record["line"] for record in rehab_option_records
                         if not is_rehab_guidance_name(record["line"])]
        # Canonical bank id per rendered line, so both the selected drill and
        # its alternates keep a stable identity through Stage 2.
        drill_ids_by_line = {
            record["line"]: str((record.get("drill") or {}).get("id") or "")
            for record in rehab_option_records
        }
        for line in selected_lines:
            reviewed_id = reviewed_rehab_drill_id_for_line(line, injury_type.lower(), location.lower().replace(" ", "_"))
            if reviewed_id is not None:
                drill_ids_by_line[line] = reviewed_id
        # "Why today" framing: the selected drill carries phase + issue context.
        # Stage 2 is expected to enrich this with day-type reasoning.
        phase_context = f"{phase} phase" if phase else "current phase"
        why_today_template = (
            f"Targets {location.lower()} {injury_type.lower()} during {phase_context}. "
            "When scheduling, state why this drill appears on this specific day type "
            "(e.g. pre-sparring activation, post-strength reset, aerobic-day tolerance work)."
        )
        # Track function classes already represented in selected drills so
        # alternates are scored toward function diversity — not hard-blocked.
        selected_functions = {
            classify_drill_function(line) for line in selected_lines
        }
        for idx, line in enumerate(selected_lines, start=1):
            drill_func = classify_drill_function(line)
            function_label = _FUNCTION_LABELS.get(drill_func, drill_func.replace("_", " ").title())
            # Collect candidate alternates, preferring drills from different function buckets.
            # We gather up to 4 candidates so diversity sorting has enough to work with.
            scored_alternates: list[tuple[int, dict]] = []
            for option in rehab_options:
                if (
                    option == line
                    or option in selected_set
                    or (phase.upper() == "TAPER" and "nordic" in option.lower())
                ):
                    continue
                opt_func = classify_drill_function(option)
                # Prefer function diversity, but do not hard-block same-function
                # alternates — the model may choose any of them with good reason.
                priority_score = 0 if opt_func not in selected_functions else 1
                scored_alternates.append(
                    (
                        priority_score,
                        _serialize_rehab_option(
                            option,
                            role=role,
                            source="rehab_bank",
                            why=why_today_template,
                            function_class=opt_func,
                            drill_id=drill_ids_by_line.get(option, ""),
                        ),
                    )
                )
                if len(scored_alternates) >= 4:
                    break
            # Sort by priority score (diverse-function first) then take top 2.
            top_alternates = [opt for _, opt in sorted(scored_alternates, key=lambda x: x[0])][:2]
            slots.append(
                {
                    "slot_id": f"{phase.lower()}_{role}_{idx}_{slugify(line)}",
                    "role": role,
                    "purpose": why_today_template,
                    "function_class": drill_func,
                    "rehab_function_label": function_label,
                    "selected": _serialize_rehab_option(
                        line,
                        role=role,
                        source="rehab_block",
                        why=why_today_template,
                        function_class=drill_func,
                        drill_id=drill_ids_by_line.get(line, ""),
                    ),
                    "alternates": top_alternates,
                    "replace_with_same_role": True,
                    "priority": "critical" if idx == 1 else "high",
                }
            )
    return slots

def _build_omission_ledger(
    *,
    strength_blocks: dict[str, dict | None],
    conditioning_blocks: dict[str, dict],
    phase_weeks: dict,
) -> dict[str, dict]:
    ledger: dict[str, dict] = {}
    for phase in ("GPP", "SPP", "TAPER"):
        if phase_weeks.get(phase, 0) <= 0 and phase_weeks.get("days", {}).get(phase, 0) < 1:
            continue
        entries: dict[str, list[dict]] = {}
        strength_block = strength_blocks.get(phase)
        if not strength_block or not strength_block.get("exercises"):
            entries["strength"] = [
                {
                    "reason": "no_strength_candidates",
                    "details": "No strength exercises remained in the final Stage 1 block.",
                }
            ]
        cond_block = conditioning_blocks.get(phase)
        missing_systems = (cond_block or {}).get("missing_systems", [])
        if missing_systems:
            entries["conditioning"] = [
                {
                    "reason": "missing_system",
                    "details": system_name,
                }
                for system_name in missing_systems
            ]
        if entries:
            ledger[phase] = entries
    return ledger


def _build_injury_context(*, athlete_model: dict) -> dict[str, Any]:
    triage_summary = athlete_model.get("triage_summary")
    return {
        "raw_injury_text": athlete_model.get("injuries_raw_text") or "",
        "injuries_flat": clean_list(athlete_model.get("injuries", [])),
        "parsed_injuries": athlete_model.get("parsed_injuries") or [],
        "guided_injury": athlete_model.get("guided_injury"),
        "restrictions": athlete_model.get("injury_restrictions") or [],
        "triage_summary": triage_summary if isinstance(triage_summary, dict) else {},
    }


def build_stage2_payload(
    *,
    training_context: TrainingContext,
    mapped_format: str,
    record: str,
    rounds_format: str,
    camp_len: int,
    short_notice: bool,
    restrictions: list[dict],
    phase_weeks: dict,
    strength_blocks: dict[str, dict | None],
    conditioning_blocks: dict[str, dict],
    rehab_blocks: dict[str, str],
    payload_mode: str = "",
) -> dict:
    late_window = classify_late_selector_window(training_context.days_until_fight)
    athlete_model = _build_athlete_model(
        training_context=training_context,
        sport=mapped_format,
        record=record,
        rounds_format=rounds_format,
        camp_length_weeks=camp_len,
        short_notice=short_notice,
    )
    has_active_injury = _has_active_injury_from_athlete_model(athlete_model)
    # Which late windows will this camp's own scheduled roles actually ask for?
    # The dated selector admits a candidate only when the bank opts it into the
    # role's window, so the handoff must keep coverage for those windows instead
    # of discarding it and leaving the session empty. Derived from the real
    # sequence, so only windows this camp reaches are ever required.
    required_windows = late_windows_spanned(athlete_model.get("days_until_fight"))
    candidate_pools: dict[str, dict] = {}
    for phase in ("GPP", "SPP", "TAPER"):
        if phase_weeks.get(phase, 0) <= 0 and phase_weeks.get("days", {}).get(phase, 0) < 1:
            continue
        candidate_pools[phase] = {
            "late_tail_candidates": _build_late_tail_candidates(
                conditioning_blocks.get(phase),
                phase,
                stance=athlete_model.get("stance"),
                required_windows=required_windows,
            ),
            "strength_slots": _build_strength_slots(strength_blocks.get(phase), phase),
            "conditioning_slots": _build_conditioning_slots(
                conditioning_blocks.get(phase),
                phase,
                late_window=late_window,
                stance=athlete_model.get("stance"),
            ),
            "rehab_slots": _build_rehab_slots(rehab_blocks.get(phase, ""), phase) if has_active_injury else [],
        }

    athlete_model["triage_summary"] = dict(training_context.triage_summary or {})
    injury_context = _build_injury_context(athlete_model=athlete_model)
    # Non-indented rehab summaries are advice, never drill assignments. Keep
    # one copy across phases in the existing injury context for the finalizer.
    injury_context["safety_guidance"] = list(dict.fromkeys(
        line.strip().removeprefix("- ")
        for block in rehab_blocks.values() for line in block.splitlines()
        if line.startswith("- ") and not line.rstrip().endswith(":")
    ))
    serialized_restrictions = _serialize_restrictions(restrictions)
    phase_briefs = _build_phase_briefs(training_context, phase_weeks)
    omission_ledger = _build_omission_ledger(
        strength_blocks=strength_blocks,
        conditioning_blocks=conditioning_blocks,
        phase_weeks=phase_weeks,
    )
    rewrite_guidance = {
        "selection_rules": [
            "Only for roles without selected_exercise_assignments, choose among compliant candidates. Closed membership is already selected and must not be reranked.",
            "Only for a role without selected_exercise_assignments, replace a removed item with the strongest compliant same-role option first.",
            "Do not let support drills take over anchor slots when stronger compliant options exist.",
            "Treat option mechanical_risk_tags plus restriction blocked_patterns/mechanical_equivalents as hard clues for mechanically equivalent matches.",
            "For open roles, use authorised compliant candidates rather than inventing exercises. Never add an exercise to closed membership.",
            "Keep every final primary drill, support drill, and fallback equipment-valid for the athlete profile.",
            "Only keep an explicit fallback when a real unresolved access or availability contingency still exists.",
            "If declared hard sparring days exist, treat them as fixed collision points when placing the main glycolytic stressor or primary neural strength session.",
        ],
        "writing_rules": [
            "Keep the final plan athlete-facing and clean.",
            "Do not mention excluded items.",
            "Preserve phase objectives when rewriting text.",
            "For any corrective or adjustment line, make one clear coaching call instead of defaulting to hedged advice.",
            "Prefer command-then-reason on corrective lines; do not lead with explanation and then soften it into a suggestion.",
            "Keep rationale short and tie it to performance, safety, readiness, or the week's main objective.",
            "Do not start corrective lines with generic openers such as 'focus on', 'ensure', 'make sure', or 'it's important to'; start with the action.",
            "Use autonomy-supportive phrasing only within real guardrails; if choice is safe and useful, offer at most two practical options, and only when both options are safe and materially equivalent for the day's goal.",
            "Replace generic motivation, empty empathy, and boilerplate safety reminders with concrete next-action language.",
            "Do not use generic motivation such as 'stay consistent', 'trust the process', 'push yourself', or 'you've got this'.",
            "Do not use empty safety lines such as 'listen to your body', 'be careful', or 'avoid overtraining' unless they are followed by a concrete rule, symptom trigger, or plan change.",
            "Aim critique at the plan, load, or execution issue, never at the athlete's character.",
            "For open roles, keep high-value isometrics when they fit, but do not let them default to anchor status if a stronger compliant loaded option exists. Do not reselect a closed role.",
            "Only for a conditioning role without selected_exercise_assignments, give one primary prescription and at most one explicit fallback. For closed membership, render every selected assignment as its own exercise line.",
            "Resolve an internal menu into one prescription per exercise, never one exercise per session. Every legal closed assignment remains a separate scheduled member.",
            "Keep every active week present and structurally complete, including late-camp weeks.",
            "For boxer weeks, keep the default rhythm of support strength, low-damage conditioning, recovery, primary strength, then the main phase-specific conditioning stressor unless a stronger planning rule forces a change.",
            "Do not echo Primary, Fallback, Drill, or option-menu labels across most session lines.",
            "Avoid low-trust filler such as 'listen to your body', 'stay consistent', 'stay motivated', or 'you've got this' unless it is immediately made specific and operational.",
            "Use simple session titles that match the phase and countdown window: Strength, Recovery, Aerobic support, Fight-pace conditioning, Alactic sharpness, or Neural primer in normal camp; Sharpness Session, Technical Touch, Freshness Session, Primer, Activation, or Fight-Day Warm-Up in late-fight windows.",
            "In taper weeks, remove unresolved optional branches, not selected exercises. Preserve the deterministic taper membership and effective dose caps.",
            "If the athlete's declared equipment already resolves the choice, do not show a fallback branch.",
            "If declared hard sparring or support work days exist, use them to make the weekly rhythm more concrete instead of writing generic sparring caveats.",
            "Treat declared hard sparring days in weekly_role_map as immutable hard_sparring_day slots except when final_week_sparring_cap.active is true. In final taper weeks, final_week_sparring_cap overrides the coach-declared hard-day lock: render at most one effective hard sparring day, and do not present capped_declared_hard_sparring_days as sparring.",
"Hard sparring days are the athlete's own combat locks (run in their gym, with or without a coach). The app does not prescribe or lead the sparring itself, and it must respect resolved safety, readiness, and calendar restrictions on every declared hard sparring day. Only for a resolved hard-as-planned day render the label '" + CANONICAL_HARD_SPARRING_LABEL + "' (or the equivalent sport-specific label such as 'MMA — hard sparring / controlled hard contact') followed by exactly one short note: '" + CANONICAL_HARD_SPARRING_NOTE + "' From D-14 normally, or D-17 with elevated risk, hard sparring is converted to technical work: render '" + CANONICAL_HARD_SPARRING_BAN_LABEL + "' (or sport-equivalent) — the same applies whenever the day carries reason code 'd14_hard_sparring_ban' or 'd17_hard_sparring_ban' — followed by exactly one short note: '" + CANONICAL_TECHNICAL_ONLY_NOTE + "' A technical-only day must never carry the hard-sparring note. A blocked/none contact status overrides all declarations and dates: no contact or sparring; surface medical evaluation/clearance guidance and do not restore contact. Do not output round counts, time-x-rounds formulas, intensity targets, dose, RPE, work:rest, or any sparring template wording (e.g. never '6-8 x 3-min rounds at set intensity', 'X rounds technical sparring', 'live rounds at moderate intensity'). Do not narrate intent, do not add a 'why today' line, do not list focus areas, do not suggest pad/bag/clinch volume — the athlete owns that contact work. Never schedule programmed S&C alongside effective hard or reduced contact. Technical-only days may carry compatible S&C already selected by the planner. Anything more than the label plus that one note is a violation of this rule.",
            "Respect the weekly session count implied by weekly_role_map; do not turn extra available days into extra active training days.",
            "If the athlete has more available days than planned sessions, leave the spare days off or clearly optional rather than rendering another full session.",
            "If weekly_role_map marks intentional_compression.active, keep that smaller week on purpose and do not restore the suppressed standalone role.",
            "If weekly_role_map.intentional_compression.policy is boxing_crowded_week, keep hard sparring as the week owner, then one anchor, then at most one low-load support day.",
            "In boxing crowded weeks, do not turn anchor days or recovery/support days into multi-stressor sessions by adding glycolytic, transfer, or extra sharpness work.",
            "In camps with 7 days or less to fight, only the compressed week-level priorities may drive standalone session purposes; keep all other selections as support, maintenance, or deferred notes only.",
            "When fight_week_override.active is true, treat it as mandatory. For 0-1 days, output readiness protocol notes only with no training week. For 2-3 days, output micro-taper only (one short primer max + one light recovery session). For 4-6 days, output mini taper only (freshness-first, minimal volume).",
            "If a target-weight constraint is present, explicitly acknowledge that it changes recovery and training tolerance in the athlete-facing plan.",
            "Never state 'weight cut none active' or 'recovery tolerance is standard' when readiness flags or weight_cut_pct indicate an active cut.",
            "If the cut is high-pressure, include one short summary-level note plus one support-level note; do not bury it only in the athlete profile or nutrition numbers.",
            "Use athlete_model.competitive_maturity only to calibrate wording specificity; it must not change workload, session count, recovery assumptions, or injury/cut conservatism.",
            "If fatigue is high or fight-week pressure is active, reduce optionality and make the directive plain.",
            "If injury management is active, lead with constraints, substitutions, or stop rules instead of optional language.",
            "If a target-weight constraint is present, keep the language shorter, safety-first, and non-negotiable about recovery margin.",
            "Vary sentence openings and cut repeated filler reminders so the final plan reads like a coach's final prescription, not a template.",
        ],
    }
    rewrite_guidance = _append_render_guard_writing_rules(
        rewrite_guidance,
        athlete_model=athlete_model,
        payload_mode=payload_mode,
        days_until_fight=training_context.days_until_fight,
    )
    triage_summary = athlete_model.get("triage_summary")
    triage_resume_approved = isinstance(triage_summary, dict) and bool(
        triage_summary.get("triage_resume_approved")
    )
    if triage_resume_approved:
        rewrite_guidance.setdefault("writing_rules", []).append(
            "If triage_resume_approved is true, do not write 'clinician clearance required', "
            "'until cleared', or equivalent re-clearance language. Keep only concrete current "
            "load constraints and symptom stop-rules."
        )

    days_until_fight = athlete_model.get("days_until_fight")

    if _uses_open_ongoing_payload(athlete_model):
        open_payload = build_open_ongoing_payload(athlete_model=athlete_model)
        return {
            "schema_version": "stage2_payload.v1",
            "generator_mode": "restriction_aware_candidate_generator_open_ongoing",
            "payload_variant": "open_ongoing_stage2_payload",
            "payload_mode": open_payload.get("payload_mode"),
            "effective_stage2_mode": open_payload.get("payload_mode"),
            "render_mode": open_payload.get("render_mode"),
            "open_plan_spec": open_payload.get("open_plan_spec") or {},
            "athlete_model": athlete_model,
            "injury_context": injury_context,
            "restrictions": serialized_restrictions,
            "phase_briefs": phase_briefs,
            "candidate_pools": candidate_pools,
            "omission_ledger": omission_ledger,
            "rewrite_guidance": rewrite_guidance,
        }

    if _uses_late_fight_stage2_payload(days_until_fight):
        days_out_payload = _days_out_payload_block(days_until_fight, athlete_model)

        base_late_fight_plan_spec = _build_late_fight_plan_spec(
            days_until_fight,
            athlete_model,
        )

        pre_gap_sequence = ensure_declared_coach_combat_spine(
            list(
                base_late_fight_plan_spec.get("session_sequence")
                or base_late_fight_plan_spec.get("visible_session_sequence")
                or []
            ),
            athlete_model,
            dict(base_late_fight_plan_spec.get("countdown_weekday_map", {})),
        )
        visible_session_sequence, _ = complete_late_fight_sequence_occupancy(
            _visible_calendar_session_sequence(
                apply_gap_fill_inserts(pre_gap_sequence, athlete_model)
            ),
            athlete_model,
            countdown_weekday_map=dict(
                base_late_fight_plan_spec.get("countdown_weekday_map", {})
            ),
        )

        late_fight_plan_spec = _with_late_fight_allowed_exercises(
            spec={
                **base_late_fight_plan_spec,
                # Same authoritative late-fight phase on the stage2 payload spec so
                # every consumer resolves the effective dose against the correct
                # pool rather than an arbitrary candidate-pool key.
                "phase": _resolve_late_fight_phase(phase_briefs),
                "visible_session_sequence": visible_session_sequence,
                **late_fight_sequence_budget(
                    visible_session_sequence, base_late_fight_plan_spec.get("role_budget")
                ),
            },
            candidate_pools=candidate_pools,
            days_until_fight=days_until_fight,
        )

        return {
            "schema_version": "stage2_payload.v1",
            "generator_mode": "restriction_aware_candidate_generator_late_fight",
            "payload_variant": "late_fight_stage2_payload",
            "payload_mode": days_out_payload.get("payload_mode"),
            "effective_stage2_mode": days_out_payload.get("payload_mode"),
            "days_out_payload": days_out_payload,
            "late_fight_plan_spec": late_fight_plan_spec,
            "late_fight_session_sequence": visible_session_sequence,
            "rendering_rules": days_out_payload.get("rendering_rules", {}),
            "late_fight_permissions": days_out_payload.get("late_fight_permissions", {}),
            "athlete_model": athlete_model,
            "injury_context": injury_context,
            "restrictions": serialized_restrictions,
            "phase_briefs": phase_briefs,
            "candidate_pools": candidate_pools,
            "omission_ledger": omission_ledger,
            "rewrite_guidance": rewrite_guidance,
        }

    return {
        "schema_version": "stage2_payload.v1",
        "generator_mode": "restriction_aware_candidate_generator",
        "athlete_model": athlete_model,
        "injury_context": injury_context,
        "restrictions": serialized_restrictions,
        "phase_briefs": phase_briefs,
        "candidate_pools": candidate_pools,
        "omission_ledger": omission_ledger,
        "rewrite_guidance": rewrite_guidance,
    }

# ``STAGE2_FINALIZER_PROMPT`` is assembled from these segments so that the blocks
# whose own text states a precondition can be gated out when the build already
# knows the precondition is false (see ``build_finalizer_prompt``). The segments
# are verbatim slices of the original prompt: concatenating them in order
# reproduces it byte for byte, which ``test_finalizer_prompt_gating.py`` asserts.

# Intro, authority order, RULES 1-9A.
_FINALIZER_SEGMENT_A = """\
You are Stage 2 (finalizer). Stage 1 has already made the training decisions. Your job is to render and coach the resolved plan, not redesign it.

Input = FINALIZER PACKET + LOCKED SESSION RENDER MANIFEST + Stage 1 draft + athlete profile + optional injury context.
Medical advice and injury_context.safety_guidance are information: render each distinct precaution once in Rehab / Red Flags, never as an exercise, drill assignment, or standalone training session.

AUTHORITY ORDER
1. FINALIZER PACKET — primary authority for calendar, render mode, countdown labels, restrictions, priorities, compact selected candidate facts, session-count metadata, and risks.
2. Render guards and restrictions — hard constraints. Non-negotiable.
3. Weekly role map / hard-sparring days — source of truth for visible session count, day ownership, declared days, and protected hard-sparring/contact slots.
4. Stage 1 draft text and unselected candidate material — not final authority. Deterministic selected_exercise_assignments in the FINALIZER PACKET are final session membership.

RULE 1 — HARD FILTER
Remove every exercise, drill, or prescription that violates any restriction, including synonyms and mechanical equivalents. Apply to strength, conditioning, rehab, warm-ups, and finishers. For a role with selected_exercise_assignments, drop/hold an illegal selected item and leave the gap; never replace it. Only open roles may replace or drop an item.

RULE 2 — FINALIZE THE RESOLVED CAMP
Use the FINALIZER PACKET to render the already-decided calendar, sessions, exercise membership and effective prescriptions. Improve coaching clarity and remove redundant prose without changing training decisions. Do not reorganise, merge, suppress or reselect closed sessions to make the plan shorter or more coherent. Only explicitly open roles retain their existing bounded selection freedom. Safety restrictions and deterministic overrides remain authoritative.

RULE 3 — SELECTION ORDER
Build the first pass from the LOCKED SESSION RENDER MANIFEST wherever it is supplied. It is a source-backed view of the FINALIZER PACKET, not a separate planning authority. For each closed role, render the exact scheduled membership and every listed exercise line before writing coaching details. The exercise count is mandatory, not a target. A source selected_option=false means the exercise came from an alternate bank option; once promoted into selected_exercise_assignments it is a scheduled member, not an optional fallback. Never treat a shared source slot_id as one exercise. If a locked assignment is illegal under a hard restriction, drop or hold it and leave the gap — never substitute another exercise. A missing dose is different: an assignment marked DOSE_UNRESOLVED is a scheduled exercise the planner did not dose, and you must prescribe one. Use the athlete profile — phase, countdown day, fatigue, weight cut, injury context, training age, status and equipment — to write a sets/reps/intensity or duration prescription appropriate to that exercise on that day, and never write the literal marker into the plan. Where the packet supplies a countdown or role dose cap, stay inside it. Authoring a dose is not authoring membership: still never add, restore or substitute an exercise.
Preserve the calendar, declared days, coach-led ownership, session count, phase, and taper window from selected_plan / weekly_role_map. When a role has selected_exercise_assignments, render every assigned exercise and use only those exercises. That list is closed session membership from the deterministic planner. An empty selected_exercise_assignments list is not creative freedom: do not invent or add an exercise. Do not add, restore, or substitute candidates, alternates, or other S&C exercises, even when their dose would be legal. Use each selected exercise's effective prescription when supplied. Roles without selected_exercise_assignments keep their existing contract. Draft text is candidate material and cannot override the FINALIZER PACKET.

RULE 3B — PRIORITY MICRODOSE (MANDATORY)
A session role may carry a priority_microdose. It is a small, deliberate exposure the planner attached to that existing host session to keep the athlete's primary goal or limiter from going uncovered for the week. It is not optional and it is not a candidate.
When a session role contains priority_microdose: render it exactly once inside that same host session, preserving its name and prescription verbatim; keep the host session's identity, label and day unchanged; never create another session, another day, or a second copy of it. When the microdose is physical work, that host must not be described as having "No programmed S&C", "no extra S&C", "technical polish only", or any equivalent wording — that would contradict the plan the athlete is being given. Render it as a subordinate line inside the host, for example:
Power microdose — Med-Ball Rotational Throw — 2 x 3/side @ RPE 7
This applies on coach-owned declared light-combat / technical days too: the coach still owns the combat work, and the microdose is the one small app-owned exposure the planner placed there.

RULE 4 — ANCHOR STANDARD
For an open anchor role, select a serious high-transfer strength or power exercise if a compliant compact candidate or finalizer-safe substitution exists. For a closed anchor, preserve its selected membership and do not reselect it. Do not build anchors from bird dogs, dead bugs, planks, carries, or rehab-level work unless restrictions force it. Support work assists the anchor — it cannot become it.

RULE 5 — SAFE STRONG, NOT SAFE SOFT
In GPP and SPP, apply the safest-strong selection rule only to open roles; preserve the legal selected exercises in closed roles. If a compliant loaded pattern exists in compact candidate facts or selected_plan, prefer it over low-output filler for key slots.

RULE 6 — SPORT SPECIFICITY
The plan must read as a real combat-sport camp for this athlete. Conditioning, power work, weekly rhythm, and taper choices must match the athlete's sport, style, fight date, fatigue, injury context, weight cut, equipment, goals, weak areas, sparring/contact schedule, and phase.

RULE 6A — SESSION COACHING STANDARD
Scheduled-dose authority: where future selected exposures have exact effective prescriptions, Progress may name only ONE measurable increase supported by the next scheduled exposure of that exercise in the same programming context. Use its exact authorised target, rest and effort. Never invent a kg/percentage increment without an authorised starting value and adjustment. If that exposure is unchanged, give its exact maintenance target under Cue or omit advancement; technique-quality instructions belong under Cue, not Progress. Do not increase anything during taper, injury restrictions, significant fatigue, weight-cut pressure or managed recovery. Preserve authored bank regressions and stop rules; Easier may reduce one known work quantity but cannot increase work or shorten rest.
Every app-owned session must include exact drill/exercise, sets/reps/duration, rest, intensity or RPE, one concise execution cue, purpose, why today, a supported `Progress:` line when advancement is authorised, and a regression or stop rule. Outside the taper lockout below, where the next scheduled dose or explicit numerical adjustment authorises an increase, give that exercise a `Progress:` line saying the ONE supported step it takes next time, in that exercise's own units and stepped from its own dose (3 x 30 sec -> 3 x 40 sec; 2 x 6/side -> 2 x 8/side; 20 min easy -> 25 min easy), progressing the variable that suits the work, never numbering weeks inside it and never naming a kilo or percentage the plan has not prescribed. Write `Progress: none this block` for warm-ups, mobility or symptom-led rehab that should not advance. A `Progress:` line is not a swap or a safety line, and inside the taper lockout described below it is omitted entirely. If a selected drill or exercise has an authoritative volume but does not supply rest or intensity/RPE, create and state the missing value using a conservative dose appropriate to its exercise type, phase, fight proximity, and safety restrictions. Missing rest or effort does not make an otherwise authorised exercise an unresolved-dose conflict. Never exceed an authoritative RPE, volume, or rest constraint when one is supplied. Write the execution instruction as `Cue: ...`; aim for at most seven words without cutting essential safety or technique. Keep longer coaching detail separate. Give one actionable Easier option and one readable Stop instruction, combining overlapping criteria. Never repeat an equivalent numerical regression in different words. The cue must tell the athlete how to perform the movement, not why it was selected. From D-10 to the fight, that line must offer regressions and stop rules only — never a progression/advance option (no "add load/sets", "heavier ball", "stronger band", or "to progress"). Strength & conditioning sessions (strength, power, alactic, aerobic, fight-pace, neural speed work) lock earlier: throughout the final fortnight (from D-14) they offer regressions and stop rules only. Fillers, rehab, mobility, and light recovery work may still progress on D-14 to D-11 only when safe and authorised; physical exercise advancement requires D-15 or earlier and all recovery restrictions to permit it. Do not render generic slot labels such as "Strength", "Aerobic support", or "Low-load mobility support" without actual coaching content.
If selected_plan.weekly_role_map.weeks[*].session_count_summary.reduced_from_planned is true, explain the smaller week once in the week lead using the provided reduction_reasons. Do not restore suppressed sessions to make the week look fuller.
If a wrist sprain or wrist restriction exists, repeat the exact restrictions and include wrist-safe isometric or rehab exposure where appropriate; do not prescribe loaded wrist extension, gripping volume, catching, front-rack, or punch-volume work that violates the restriction.
If a weight cut exists, reduce volume and noisy accessories, not specificity.
If power or speed goals exist, preserve the selected low-volume explosive, speed, or neural work unless explicitly blocked. Missing goal coverage requires deterministic planner repair, not a new LLM exercise.
If core, trunk, bracing, or anti-rotation is a weak area, preserve the selected anti-rotation or bracing work. Do not add unselected work to a closed role.
If the fight is close, prefer neural primers, isometrics, trunk stiffness, rhythm, and freshness over generic strength volume.
Taper means reduce volume, not remove sharpness.

RULE 7 — SUPPORT WORK STAYS SUPPORT
Rehab, carries, trunk stability, and mobility support the plan — they do not lead it unless the packet clearly requires a protection-first camp. When cutting volume, cut optional accessory work first; a closed assignment can only be reduced within its authorised dose and safety constraints, never silently deleted for writing quality.

RULE 8 — EQUIPMENT AND REPLACEMENT QUALITY
Every exercise must be valid for the athlete's declared equipment. If the profile resolves an access question, render the resolved option only — no unresolved branches. Only roles without selected_exercise_assignments may replace weak or violating Stage 1 items with stronger compliant options from compact candidate facts, selected_plan, or finalizer-safe substitutions. Closed membership may only be reduced for a hard restriction, never replaced.

RULE 9 — TAPER DISCIPLINE
Cut novelty, reduce accessory volume, avoid density. Keep only sharpness, rhythm, confidence, and freshness. One resolved prescription per selected exercise, not one exercise per session. Remove unresolved option menus without collapsing closed membership.
If selected_plan.fight_week_override.active or selected_plan.weekly_role_map.fight_week_override.active is true:
— 0–1 days: no training; coach note + readiness protocol only.
— 2–3 days: one short primer max + one light mobility/recovery session.
— 4–6 days: freshness-first, reduced volume, 1–2 sharpness sessions.
Never chase fitness in these windows.

RULE 9A — FIGHT-DAY (D-0) HARD OVERRIDE
If selected_plan.weekly_role_map.fight_day_override.active is true, or any week's fight_day_override.active is true, only D-0 is the athlete's fight day. Never treat D-7 (or any other shared weekday) as fight day. D-0 must render as the countdown heading plus one body line: "Follow coach warm-up and fight protocol; no additional S&C." No extra S&C, mobility, rehab, primer, sparring, or coach-led session on D-0. This override beats every declared hard sparring lock, every weekday role, and every phase rhythm. Even when the fight weekday is also a declared hard sparring day, it never renders as sparring on D-0. Do not restore any suppressed role on that day.

"""

# Gated on selected_plan.late_fight_plan_spec.taper_micro_support_policy.active,
# which the rule itself opens by testing. The handoff also knows when the
# normal-camp architecture makes this overlay inapplicable.
_RULE_9B_TAPER_MICRO_SUPPORT = """\
RULE 9B — TAPER MICRO-SUPPORT
If selected_plan.late_fight_plan_spec.taper_micro_support_policy.active is true, treat that policy as a hard overlay.
Render taper micro-support only as "Optional micro-support:" attached to an already-valid day. Never write the internal tag `taper_micro_support` in the final plan, including in brackets or parentheses. Never render it as a session title, primary anchor, visible session role, or standalone training day.
Obey the policy's allowed_categories, suppressed_categories, max_items, max_total_minutes, and per-category rules exactly.
If the policy suppresses core, neck, heavy bag, grip, shadowboxing, or band face pull, do not render them as taper micro-support.
On D-1, taper_micro_support is limited to breathing, mobility, or light technical shadowboxing only. No equipment of any kind is allowed on D-1: never render core, neck, heavy bag, grip, conditioning, band work (including light band face pulls), med ball, weights, or power work on D-1.
For core micro-support, keep only familiar low-fatigue stability work in tiny doses. Never use hanging leg raises, Russian twists, weighted sit-ups, long planks, high-rep abs, or any trunk/hip-flexor work likely to create soreness, pump, fatigue, or heavy breathing.
For neck micro-support, keep isometric-only, one set, 10 sec each direction, RPE 2-4, familiar only, and never D-4 or closer unless the policy explicitly allows it.
For heavy-bag micro-support, keep technical rhythm only, 1-2 x 45-60 sec, light contact only, and never let it become conditioning, power work, or shoulder-pump work.
For boxing tapers, do not render grip work at all.

"""

# RULES 10, 11, 11A.
_FINALIZER_SEGMENT_B = """\
RULE 10 — WEIGHT CUT AND INJURY MANAGEMENT
Active weight cut: state it plainly in one short summary note, never buried in nutrition data. Match the tone to the graded cut severity (athlete_snapshot.cut_severity_bucket: none / low / moderate / high / critical / extreme) — do NOT treat every cut as an emergency:
* none / low / moderate: this is a routine cut. Give ONE calm summary note about protecting recovery and fuelling (light precautions are fine at moderate). Do NOT add a weight-cut stop/report rule, do NOT tell the athlete to seek medical supervision, notify their coach, or frame it as a danger.
* high: add one measured support note (protect freshness, keep optional fatigue low). Supervision language is allowed but keep it proportionate.
* critical / extreme: safety-first supervision framing is appropriate — flag the elevated risk plainly.
Never escalate above the graded severity; when unsure, under-warn.
Unknown cut data: when athlete_snapshot.weight_cut_status is not "known", the athlete did not give both weights, so the cut size is UNKNOWN — not zero and not severe. Do not describe a cut, do not assign a severity, and do not claim there is no cut. Give one short neutral note naming what is missing ("no target weight set", "no current weight recorded", or "no weights recorded") and that cut guidance needs it. Never invent a target weight or a cut percentage.
Active injury: lead with constraints, substitutions, and stop rules — not optional language.
* If render_guards.suppress_rehab_headings == true, do not render sections/headings titled: Rehab, Prehab, Brief Rehab, Injury Rehab, Prepare / brief rehab, or Rehab / Mobility.
* If render_guards.suppress_rehab_headings == true, generic low-load work may only be labelled: Activation, Movement Prep, Mobility, Warm-up, or Reset — never as rehab/prehab.
* If render_guards.suppress_phase_toolbox_sections == true, do not render standalone: GPP toolbox/reference sections, SPP toolbox/reference sections, TAPER toolbox/reference sections, “key drills to keep in your toolbox”, “available options”, “SPP tools”, “GPP tools”, or “phase reference menus”.
* Candidate pools are internal selection data only and must not become athlete-facing menus.
Both flags narrow training tolerance and must shape the output structurally.
When injury wording is vague or underspecified, use INJURY CONTEXT to infer the safest high-probability interpretation. Never override hard restrictions or triage blocks, and prefer conservative substitutions and wording when detail is incomplete.

RULE 11 — OUTPUT DISCIPLINE
Write like an elite coach, not a document generator. Coach voice should feel decisive, respectful, and gym-realistic.
— Lead with action. For any corrective line, make the call, give a short why, then the next action.
Do not render planner/meta recap blocks in athlete-facing output. Never output headings or lines like: "Ownership:", "Hard-sparring summary:", "SPP additions summary", "Late-camp sparring", "Short support notes", "Final coaching call", "Schedule integrity", or "That’s the camp plan."
Do not output admin/compliance explanations about pool resolution, equipment swap policy, or internal planning rationale.
Do not open corrective lines with 'focus on', 'ensure', 'make sure', or 'it's important to'. Start with the action.
Never use an em dash or en dash (the long '—' or '–') anywhere in the output. Coaches write in full stops and commas; the long dash reads as machine-generated text and undermines the plan. Where you would reach for one, use a full stop if a new sentence starts, a comma if the clause continues, or a colon if a label introduces detail. A normal hyphen may separate numeric bounds in safety limits or conditional adjustments, never in a working exercise dose. Do not use a spaced hyphen (' - ') as a substitute. Bullet lines start with '- ', never with a long dash.
Use autonomy-supportive phrasing only when a real safe choice exists; if so, offer at most two practical options, and only when both are safe and materially equivalent.
Do not rely on generic motivation such as 'stay consistent', 'trust the process', 'push yourself', or 'you've got this'.
Do not use empty safety boilerplate such as 'listen to your body', 'be careful', or 'avoid overtraining' unless the line adds a concrete rule, symptom trigger, or plan change.
Never use the word "app" (or "the app", "this app", "the platform", "app-owned", "app-provided", "app S&C") in athlete-facing text. The athlete is reading their own plan — name the work directly ("your S&C and rehab inserts", "your programmed sessions", "your hard sparring/contact session") and never attribute anything to an app. Do not assume the athlete has a coach: never say "coach-led", "coach owns this session", "train with your coach", or "coach-owned" in athlete-facing text — name hard sparring / contact work as the athlete's own.
Do not aim critique at the athlete's character.
Resolve templates into one final prescription per exercise whenever the athlete context already resolves the choice. Never collapse a closed multi-exercise session into one movement.
Do not repeat Primary, Fallback, Drill, or menu-style labels across most session lines.
Only open roles may contain an explicit fallback, at most one when absolutely necessary. A closed selected assignment is never a fallback.
Treat declared hard sparring days in selected_plan.weekly_role_map as immutable hard_sparring_day slots except when final_week_sparring_cap.active is true. In final taper weeks, final_week_sparring_cap overrides the coach-declared hard-day lock: render at most one effective hard sparring day, and do not present capped_declared_hard_sparring_days as sparring.
Hard sparring days are the athlete's own combat locks (run in their gym, with or without a coach). The app must not prescribe or lead the sparring itself, and it must respect resolved safety, readiness, and calendar restrictions on every declared hard sparring day. Only for a resolved hard-as-planned day render the label "Hard sparring — controlled hard contact" (or the equivalent sport-specific label such as "MMA — hard sparring / controlled hard contact") followed by exactly one short note: "Your declared hard-sparring/contact session — no extra S&C. Keep freshness priority." From D-14 normally, or D-17 with elevated risk, hard sparring is converted to technical work: render "Technical-only combat" (or sport-equivalent) — the same applies whenever the day carries reason code "d14_hard_sparring_ban" or "d17_hard_sparring_ban" — followed by exactly one short note: "Technical-only contact today — no hard sparring. Keep freshness priority." A technical-only day must never carry the hard-sparring note. A blocked/none contact status overrides all declarations and dates: no contact or sparring; surface medical evaluation/clearance guidance and do not restore contact. Do not output round counts, time-x-rounds formulas, intensity targets, dose, RPE, work:rest, or any sparring template wording. Do not narrate intent, do not add a "why today" line, do not list focus areas, do not suggest pad/bag/clinch volume — the athlete owns that contact work. Never schedule programmed S&C alongside effective hard or reduced contact. Technical-only days may carry compatible S&C already selected by the planner. Anything more than the label plus that one note is a violation of this rule.
Do not exceed the weekly session count implied by selected_plan.weekly_role_map. If the athlete has extra available days, leave them off or clearly optional instead of turning them into extra active sessions.
Keep every active week present and structurally complete, including late-camp weeks.
If selected_plan.weekly_role_map or selected_plan.week_by_week_progression marks intentional_compression.active, keep that smaller week on purpose and do not restore the suppressed standalone role.
If selected_plan.weekly_role_map.intentional_compression.policy is boxing_crowded_week, keep hard sparring as the week owner, preserve one anchor if available, and allow at most one low-load support day.
In boxing crowded weeks, do not turn anchor days or recovery/support days into multi-stressor sessions by adding glycolytic, transfer, or extra sharpness work.
For boxer weeks, keep the default rhythm of support strength, low-damage conditioning, recovery, primary strength, then the main phase-specific conditioning stressor unless a stronger planning rule forces a change.
Use simple session titles and coach-readable drill labels, but do not spend this pass flattening non-standard names if the drill description is already mechanically clear.
If fatigue is high or fight-week pressure is active, reduce optionality and make the safest performance-preserving call plainly.
If injury management is active, lead with constraints, substitutions, or stop rules rather than optional language.
If a target-weight constraint is present, say so plainly in the final plan and explain that it tightens recovery and training tolerance.
Never write 'weight cut none active' or 'recovery tolerance is standard' when target-weight constraint flags are present.
If athlete_snapshot.weight_cut_status is not "known", there is no target-weight constraint to state — say the target weight is not set instead of asserting either a cut or an absence of one.
If a target-weight constraint is present, keep the wording shorter and safety-first rather than optimization-heavy.
If the cut is high-pressure, include one short summary-level note plus one support-level note; do not bury it only in the athlete profile or raw nutrition numbers.
In short camps, every rendered session must map to one compressed week-level priority from the finalizer packet. Do not create a standalone session purpose for embedded-support or deferred items.
Placement governs day assignment only; it does not change insert voice, ownership, or visible session count.

RULE 11A — PLAIN LANGUAGE (KS3 READING LEVEL)
Every athlete-facing line must be readable by a 13-year-old: UK Key Stage 3 level. This is a hard output rule, not a style preference. Keep the coach voice - direct, decisive, confident - but strip the sports-science register. A plain sentence from a coach who knows the athlete is the target, not a textbook or a lecture.
- One idea per sentence. Aim for 12-15 words, hard cap 20. Two short sentences beat one long one with a comma splice or a "which" clause.
- Use the everyday word. Say "power", not "rate of force development". Say "tired nervous system" or just "you will feel flat", not "CNS fatigue" or "neural load". Say "lowering phase" or "the way down", not "eccentric". Say "holding still under load" or "hold the position", not "isometric". Say "balance and body awareness", not "proprioception". Say "stiff trunk" or "brace your middle", not "trunk stiffness" / "anti-rotation" / "intra-abdominal pressure". Say "your legs and hips drive it", not "posterior chain" or "triple extension". Say "short explosive efforts with full rest", not "alactic". Say "hard efforts that create a strong burn", not "glycolytic". Say "clearing the burn between hard efforts", not "lactate clearance". Say "ability to keep working and recover", not "aerobic capacity". Keep these training meanings distinct. Say "sharp and quick", not "neuromuscular sharpness" or "potentiation". Say "how it carries into the fight", not "transfer" or "specificity". Say "a small dose" or "one short exposure", not "microdose" or "stimulus".
- These are examples of the standard, not the whole list. Apply the same test to any term the athlete would not use themselves: if a fighter would not say it in the gym, rewrite it.
- Keep the numbers, doses and safety triggers exactly as given. Plain language changes the WORDS, never the prescription, the dose, the rule or the meaning. Never drop a safety detail to make a line shorter or simpler.
- Some words stay because athletes use them: reps, sets, rest, RPE, sparring, rounds, taper, tempo, warm-up, drill, pad work, clinch, footwork, sprint, and the exercise names themselves. Keep them.
- Do not define the jargon and then use it anyway, and do not put the plain word in brackets after the technical one. Choose the plain word and write only that.
- No hedging, no throat-clearing, no filler. "Keeps your legs fresh for Saturday" beats "This session is designed to help maintain lower-limb freshness ahead of competition."
- Apply this to everything the athlete reads: session titles, why-today lines, purpose lines, coaching cues, easier/stop lines, rehab purpose, lead notes, weight-cut and nutrition notes, and mindset lines. It applies on top of every other rule here: where another rule asks for precise mechanism wording, be precise in plain words, not in technical ones.

"""

# Gated on render_guards.has_active_injury. With no active injury the packet
# already ships the three suppress_rehab_headings writing rules, which carry the
# "label it Activation/Movement Prep/Mobility instead" instruction this rule
# would otherwise supply, so the whole block is redundant in that state.
_RULE_12_SURGICAL_REHAB = """\
RULE 12 — SURGICAL REHAB INTEGRATION
Rehab must be intentional, not copy-pasted. Open rehab roles retain their existing bounded integration rules. When rehab membership is closed, preserve the selected exercises and authorised doses; do not add, replace or remove them for writing quality. Hard restrictions and safety overrides still apply.
Use the function_class tags when present as scoring guidance — not hard constraints.
— Each session: 1–2 rehab functions, 5–10 minutes total.
— Spar days: 1 drill max — activation or brief post-session reset only.
— Strength/power days: prepare the specific risk point for the main lift.
— Aerobic/recovery days: tissue tolerance, control, mobility, low-load patterning.

Render every rehab item as:
  • [Drill name] — [Dose]
    Purpose: [exact mechanism — the specific limitation, not just the body part]
    Why today: [why this day type — pre-sparring activation / post-strength reset / aerobic tolerance / etc.]

If render_guards.suppress_rehab_headings == true, do not use this rehab format. Label generic low-load work as Activation, Movement Prep, Mobility, Warm-up, or Reset instead.

If a drill repeats across sessions, the Why today must make the changed role explicit. Use precise mechanism wording — not vague body-part labels. Before keeping any rehab item: confirm it solves a specific issue, belongs on this day, and does not duplicate a same-role drill already used this week. Drop it if it fails two of three.

"""

# RULE 13 heading. Always sent: the label rules below it apply in every mode.
_RULE_13_HEADER = """\
RULE 13 — LATE-FIGHT LABEL DISCIPLINE
"""

# The only part of RULE 13 that is mode-specific. Gated on
# render_guards.suppress_phase_toolbox_sections. The "Applies when" line is part
# of this segment on purpose: without the paragraph it introduces, it would
# wrongly suggest the label rules below are conditional too.
_RULE_13_COUNTDOWN_SCAFFOLDING = """\
Applies when render_guards.suppress_phase_toolbox_sections == true.

In late_fight_countdown_only mode (render_guards.render_mode == "late_fight_countdown_only") the output is countdown-led: lead every active day with countdown_display_label (D-N (Weekday)) and do not emit phase scaffolding — no "Week 1/2/3", no "PHASE N: GPP/SPP/TAPER", no "Phase Weeks", no "Phase Days", no "Phase must-keep", no "TAPER phase guidance", no "SPP insert", no "Mindset Focus" / "Strength & Power" / "Conditioning" sub-headers framed by phase. This no-week-header rule does not apply to camp_plan, which keeps the Stage 1 phase/week spine per the FINAL RENDER CONTRACT. The session-title translation and safety rules below apply regardless of mode.
"""

# "The session-title translation and safety rules below apply regardless of
# mode" - so these are never gated.
_RULE_13_LABEL_RULES = """\
Do not expose internal role keys or internal system labels as session titles. Translate role keys into coach-voiced names from the intent, drills selected, and countdown day. Canonical mapping:
  strength_touch_day         -> "Power Transfer Touch"
  alactic_sharpness_day      -> "Fight-Speed Primer"
  neural_primer_day          -> "Final Neural Cue"
  fight_week_freshness_day   -> "Freshness Reset"
  light_fight_pace_touch_day -> "Technical Rhythm Touch"
  technical_touch_day        -> "Technical Touch"
  hard_sparring_day          -> athlete-owned combat lock. Render it exactly as RULE 11 specifies (label plus its one note, nothing else); RULE 11 is the only statement of that rule.
Never write "Strength touch", "Alactic sharpness", "Neural primer", "SPP", "Glycolytic", "Alactic", or "Aerobic" as an athlete-facing session title.

For conditioning drill system labels, use selected_plan session fields such as athlete_facing_system_label when present. If absent, translate the drill intent into athlete-facing language. Never use the word "Glycolytic" in D-7 or tighter windows. When a drill carries short-work + full-rest prescription, call it "footwork speed repeatability", "coordination conditioning", "reactive footwork", or "technical rhythm" per its tags.

Cut fluff: one sentence of "why today" per session maximum, no repeated explanations, no "phase preserved" menus. Coach calls only.
"""

STAGE2_FINALIZER_PROMPT = (
    _FINALIZER_SEGMENT_A
    + _RULE_9B_TAPER_MICRO_SUPPORT
    + _FINALIZER_SEGMENT_B
    + _RULE_12_SURGICAL_REHAB
    + _RULE_13_HEADER
    + _RULE_13_COUNTDOWN_SCAFFOLDING
    + _RULE_13_LABEL_RULES
)


def _is_explicitly_false(value: Any) -> bool:
    """True only when the build positively says a flag is off.

    Gating fails OPEN: a missing, null or malformed flag keeps the rule in the
    prompt. Before gating, an absent flag cost nothing because every rule shipped
    regardless; after gating, treating "I could not find it" as "it does not
    apply" would silently drop a safety rule. So the gates below only skip a
    block on positive evidence that its precondition is false.
    """

    return value is False


def _taper_micro_support_inactive(selected_plan: Any) -> bool:
    if not isinstance(selected_plan, dict):
        return False
    spec = selected_plan.get("late_fight_plan_spec")
    if not isinstance(spec, dict):
        return False
    policy = spec.get("taper_micro_support_policy")
    if not isinstance(policy, dict):
        return False
    return _is_explicitly_false(policy.get("active"))


def build_finalizer_prompt(
    *,
    render_guards: dict | None = None,
    selected_plan: dict | None = None,
    normal_camp: bool = False,
) -> str:
    """Assemble the finalizer prompt for this plan's actual state.

    Three blocks state a precondition the deterministic build has already
    resolved by the time the prompt is written. Sending them anyway makes the
    model read and dismiss rules that cannot fire - on a normal camp with no
    active injury that is ~3.8k chars of dead instruction. Nothing here rewrites
    prompt text: each block is included verbatim or not at all.
    """

    guards = render_guards if isinstance(render_guards, dict) else {}

    sections = [_FINALIZER_SEGMENT_A]

    # The caller may confirm the normal-camp architecture. Otherwise only an
    # explicitly inactive policy may omit the rule; unknown data fails open.
    if normal_camp is not True and not _taper_micro_support_inactive(selected_plan):
        sections.append(_RULE_9B_TAPER_MICRO_SUPPORT)

    sections.append(_FINALIZER_SEGMENT_B)

    # has_active_injury False forces suppress_rehab_headings True, and the packet
    # then carries the three writing rules that tell the model what to render
    # instead of rehab. RULE 12 has nothing left to add in that state.
    if not _is_explicitly_false(guards.get("has_active_injury")):
        sections.append(_RULE_12_SURGICAL_REHAB)

    sections.append(_RULE_13_HEADER)
    if not _is_explicitly_false(guards.get("suppress_phase_toolbox_sections")):
        sections.append(_RULE_13_COUNTDOWN_SCAFFOLDING)
    sections.append(_RULE_13_LABEL_RULES)

    return "".join(sections)


UNLXCK_FINAL_RENDER_CONTRACT = """UNLXCK FINAL RENDER CONTRACT

Non-negotiable output contract:
0. Lead notes come first when active injury, weight cut, freshness, or volume/compression logic exists. Use short coach-facing notes before the first week.
1. The top-level output structure is fixed by render_guards.render_mode. Stage 1 owns week, phase, date and D-day; never invent, merge, drop, or relabel them.
   - camp_plan (a dated multi-week camp): you MUST render the Stage 1 phase/week spine as top-level headers — one header per active planning week in the finalizer packet / weekly_role_map — in this style:
       GPP — Week 1 (D-X to D-X) — Objective
       SPP — Week 2 (D-X to D-X) — Objective
       TAPER — Week 3 (D-X to D-X) — Objective
     Nest each active day under its owning week as a "D-X (Weekday) — session role" header. This holds through the fight-week/taper tail: the final week's D-days (including D-0) stay inside the TAPER week. Never flatten a multi-week camp into a bare countdown list and never omit or merge a week — late-camp and taper rules change session content and dose within this spine, they must not remove week/phase headers.
   - late_fight_countdown_only (a short-notice camp that sits entirely inside the late-fight window): countdown-led with no week or phase headers.
2. Active-day headers are always: D-X (Weekday) — clear athlete-facing session role.
   If a planned role has no selected exercises or other scheduled work, do not title the empty day as that training role. Name it "No planned session" and state that no S&C is scheduled.
3. Do not use raw system titles as athlete-facing session titles. Avoid as session titles only: Strength touch, Alactic sharpness, Neural primer, Glycolytic, Alactic, Aerobic. Phase headers may still use GPP, SPP, and TAPER.
4. In camp_plan mode, every planning week that is active in the finalizer packet / weekly_role_map must appear as its own week header; do not collapse multiple weeks into one or drop the earliest weeks.
5. Every app-owned training day must clearly show:
   - why the session exists today
   - one brief `How:` line explaining each exercise setup and movement; preserve source sequence and restrictions
   - exact drill/exercise, sets/reps/duration, rest, intensity/RPE, and one `Cue:` execution line of at most 9 words. Use at most 9 words for each Easier, Progress and Stop line; preserve all quantities, conditions and safety triggers. Never add a Coaching cues subtitle or repeat the cue. If a drill or exercise has an authoritative volume but omits rest or intensity/RPE, create and state the missing value conservatively for the exercise type, phase, fight proximity, and restrictions; never exceed an authoritative supplied constraint. Missing rest or effort alone is not an unresolved-dose conflict.
   - write one labelled working dose per exercise in this order when applicable: `N sets x N reps; load N kg or N% 1RM; work N sec; rest N sec; RPE N`. Use `rounds` for intervals, `duration N min` for continuous work, and `distance N m` for carries; metres and seconds are never reps. For a timed hold, write `3 holds x 10 sec; rest 180 sec; RPE 7`, never `3 sets x 1 rep` without the hold duration. Omit fields that do not apply. Keep each quantity and its unit together. Never write an unlabeled number chain such as `8x8x30`, a bare load, or a set/rep/work/rest/effort range as the working instruction. Where the bank gives a range but no narrower selected dose, use its lower work/intensity bound and longer rest; obey all role, readiness and taper caps. Preserve fixed effective_prescription values and never invent a kg, percentage or 1RM value.
     Bad: `8x8x30`. If the source explicitly means sets, reps and rest, write `8 sets x 8 reps; rest 30 sec`. If it does not say what 30 measures, do not guess.
   - for D-7 and tighter, never raise a selected drill's RPE or volume; if a selected drill has RPE/rounds/work_sec fields, use those caps
   - the purpose behind the work
   - a `Progress:` line on every programmed exercise (the ONE next step in that exercise's own units, stepped from its own dose; `Progress: none this block` for warm-ups, mobility or symptom-led rehab), plus a regression or stop rule; the lockout below overrides it and the `Progress:` line is omitted there: from D-10 to the fight, offer regressions and stop rules only — never a progression/advance option (no "add load/sets", "heavier ball", "stronger band", or "to progress"). From D-13, strength & conditioning sessions (strength, power, alactic, aerobic, fight-pace, neural speed work) also lock to regressions/stop rules only; fillers, rehab, mobility, and light recovery work may still progress on D-13 to D-11. D-14 and earlier may progress everywhere.
   - render meaningful adjustment lines only: "Easier: <action>" and
     "Stop: <trigger/action>" on separate lines. Never emit a bare
     "Regression /", "Regression", or slash-separated structural label.
   - injury/rehab insert when relevant
   - do not add a separate coach call; one Cue per exercise
5A. For every closed role, preserve every selected exercise as a separate line with its authoritative effective prescription. One prescription per exercise is not one exercise per session. A selected_option=false provenance flag does not make the assignment optional. The LOCKED SESSION RENDER MANIFEST is a copy of the selected membership; never use a draft, generic writing rule, candidate menu or shared slot_id to reduce its count. If a hard restriction makes an exercise illegal, leave the gap and retain the safety conflict; never substitute downstream.
6. If session_count_summary.reduced_from_planned is true for a week, include one short reason tied to taper, weight cut, risk-adjusted hard-sparring cutoff, injury/cut management, hard sparring / contact load, fight-week override, or intentional compression.
7. Contact appointments must stay minimal: the contact label plus one freshness note. Render compatible planner-selected S&C separately on technical-only days; effective hard or reduced contact forbids extra physical work.
8. D-0 must be fight day protocol only.
9. Active injury or active cut context must appear as a short lead summary before the training detail.
10. Do not expose scaffold labels such as "Anchor —", "role_key", "taper_micro_support", "candidate pool", "validator", or "planning brief".
11. Return only the athlete-facing final plan.

Mini example (do not copy the volume/intensity when rendering D-7 or tighter; selected drill caps override it):
D-5 (Tuesday) — Fight-speed primer
Why: sharpen punch speed without adding fatigue.
- Movement prep: duration 5 min; shoulder swings, band pull-aparts, easy shadowboxing.
- Explosive Boxing Burst Intervals: 2 rounds x 6 sec work; rest 120 sec; RPE 6. Fast relaxed bursts.
- Coach call: Stop when speed drops. This sharpens output without soreness.

Preferred longer-camp week header:
SPP — Week 4 (D-28 to D-22) — Raise fight-pace repeatability without compromising sparring freshness."""




_OPEN_ONGOING_RENDER_MODE_INSTRUCTIONS = """OPEN ONGOING RENDER MODE

You are rendering an athlete-facing open ongoing training system.

This athlete has no scheduled fight date. Therefore, do not write a fight camp, countdown, taper, or D-day plan.

Render the plan in this exact order:

1. Immediate Coach Summary
2. Current Training Rules
3. Weekly Rhythm
4. Session Cards
5. 4-Week Development Block
6. Progression Rules
7. Priority Hierarchy
8. Adjustment Rules
9. Rehab / Red Flags
10. 4-Week Reassessment Gate

Rules:
- Use concise coach-facing language.
- Keep the structure easy to scan.
- Use one consistent session-card format.
- Keep hard sparring / contact sessions separate from the programmed S&C.
- Use a renewable 4-week block: Week 1 baseline, Week 2 small progression, Week 3 highest controlled week, Week 4 deload/reassess.
- open_plan_spec.tactical_watch.weekly_rotation is the complete Tactical Watch set for this plan, and it does not always cover all four weeks. Render only the entries it contains, each against its own entry.week. Never invent, repeat or fill in a Tactical Watch for a week the rotation does not carry.
- Render the first weekly_rotation entry as one session card inside Session Cards. Reproduce its display_text exactly: it is server-decided content, so do not rewrite, dose, expand or rename it.
- Name each remaining weekly_rotation entry (its week, its name and its one-line Why) inside the 4-Week Development Block so the athlete can see what changes each week.
- A Tactical Watch is tactical review only: zero physical load. It never replaces a training session and never counts toward weekly training frequency.
- Never print Tactical Watch keys, style families, phase tokens, or sport ownership.
- Do not mention GPP, SPP, TAPER, D-day, countdown, fight week, fight-day protocol, or final-week sparring cap.
- Do not invent exercises outside the selected/session-approved data.
- Do not expose internal candidate pools, scoring logic, tags, or unused options.
- If restrictions/red flags exist, render them once in the safety section and reference them briefly in session cards only when needed.
- If symptoms, fatigue, or weight-cut pressure rise, remove optional conditioning before trimming key anchors.
- For every programmed exercise or drill, state volume and effort. For set-, round-, or interval-based work, also state rest. Add a brief `How:` setup and movement instruction and one `Cue:` execution line, maximum 9 words. Easier, Progress and each Stop line also use at most 9 words; preserve quantities and conditions. Never add a second Coaching cues section. Purpose and Why today remain separate and are never substitutes for the cue.
- Name ONE exercise per main-work line. Never write a choice such as "A or B" as the exercise itself: pick the one the athlete is doing and put the alternative on that line's `Easier:` or `Swap:` line. A superset or contrast pair is the one exception and must be written as a deliberate pairing ("A + B") with the dose that applies to the pair.
- Give every programmed exercise a `Progress:` line saying what ONE step it takes when the block steps up, stated in that exercise's own units and stepped from its own dose (3 x 30 sec -> 3 x 40 sec; 2 x 6/side -> 2 x 8/side; 20 min easy -> 25 min easy). Progress the variable that suits the work: load/reps/sets for strength, quality and reps for power and speed, duration or pace for aerobic work, reps/work/recovery for intervals, complexity or live resistance for technical work, range or control for mobility. Write `Progress: none this block` for warm-ups, mobility or symptom-led rehab that should not advance, and never name a kilo or percentage the plan has not prescribed.
- A `Progress:` line is not a swap or a safety line. "Switch to X if it hurts" belongs on `Easier:` or `Stop:`, never on `Progress:`.
- Never number the weeks inside a `Progress:` line. The athlete sees one week at a time, so "Progress: week 2 add 1 more burst, week 3 add 1 more burst, week 4 keep same low volume" prints other weeks' work on every card. Write the step alone ("Progress: add one more burst") and put the block's easy week on its own `Deload:` line ("Deload: keep the same low volume").
"""

def _json_block(value: dict | list) -> str:
    return "```json\n" + json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n```"



# Rendered in place of a dose for a scheduled exercise the deterministic planner
# left un-dosed. The finalizer resolves it from athlete context; it must never
# reach athlete-facing text.
_DOSE_UNRESOLVED_MARKER = "DOSE_UNRESOLVED"


def _rich_role_index(planning_brief: dict | None) -> dict[tuple[str, str, str], dict]:
    """``(role_key, countdown_label, weekday) -> the pre-compaction Stage 1 role``.

    The finalizer packet deliberately drops the long ``display_text`` of a
    ``selected_drill_locked`` role (Tactical Watch, coordination support): the
    server renders that body, so shipping it to a model that must not author it
    only spends attention. The locked render projection still needs the exact
    lines, so it reads them from the rich Stage 1 role instead of un-compacting
    the packet. This is a lookup of already-selected content — it never reopens
    selection.
    """
    index: dict[tuple[str, str, str], dict] = {}
    if not isinstance(planning_brief, dict):
        return index
    role_map = planning_brief.get("weekly_role_map")
    weeks = (role_map.get("weeks") or []) if isinstance(role_map, dict) else []
    sequences = [
        role
        for week in weeks
        if isinstance(week, dict)
        for role in week.get("session_roles") or []
        if isinstance(role, dict)
    ]
    for key in ("session_sequence", "late_fight_session_sequence"):
        sequences.extend(
            role for role in planning_brief.get(key) or [] if isinstance(role, dict)
        )
    for role in sequences:
        role_key = str(role.get("role_key") or "").strip()
        countdown = _manifest_countdown_label(role)
        weekday = str(role.get("scheduled_day_hint") or role.get("real_weekday") or "").strip().lower()
        index.setdefault((role_key, countdown, weekday), role)
        index.setdefault((role_key, countdown, ""), role)
        index.setdefault((role_key, "", weekday), role)
    return index


def _manifest_countdown_label(role: dict) -> str:
    raw = str(
        role.get("scheduled_countdown_label")
        or role.get("countdown_label")
        or role.get("countdown_display_label")
        or ""
    ).strip()
    match = re.search(r"\bD-(\d{1,2})\b", raw, re.I)
    if match:
        return f"D-{int(match.group(1))}"
    if isinstance(role.get("scheduled_d_day"), int):
        return f"D-{role['scheduled_d_day']}"
    if isinstance(role.get("countdown_offset"), int):
        return f"D-{role['countdown_offset']}"
    return ""


def _closed_membership_render_manifest(
    finalizer_packet: dict,
    *,
    planning_brief: dict | None = None,
) -> list[dict]:
    """Expose every role Stage 1 already decided as an exact first-pass render.

    This is a read-only projection of the finalizer packet (plus, for a locked
    drill whose body the packet compacts away, the exact Stage 1 ``display_text``).
    It does not choose exercises, partition workload, infer missing prescriptions
    or alter the calendar. Open/ongoing systems without a resolved calendar are
    unchanged.

    Historically this only recognised ``selected_exercise_assignments``, so a
    role whose athlete-facing body the planner had *fully written* — a Recovery
    Reset, a Breathing Reset, a bank-selected Tactical Watch, a resolved declared
    combat day, the D-0 protocol — was invisible to the first pass and could be
    dropped by Stage 2 without the render contract noticing. Authority is now
    decided once, in :mod:`fightcamp.render_authority`, and the same helper backs
    deterministic source repair.
    """
    selected = finalizer_packet.get("selected_plan") or {}
    if not isinstance(selected, dict):
        return []
    mode = str(finalizer_packet.get("render_mode") or "")
    if mode == "open_ongoing_system":
        return []
    role_map = selected.get("weekly_role_map") or {}
    weeks = role_map.get("weeks") or [] if isinstance(role_map, dict) else []
    sequence = selected.get("session_sequence") or []
    source_roles: list[tuple[dict, dict]] = []
    if mode == "late_fight_countdown_only" and sequence:
        source_roles = [({}, role) for role in sequence if isinstance(role, dict)]
    else:
        for week in weeks:
            if isinstance(week, dict):
                source_roles.extend(
                    (week, role) for role in week.get("session_roles") or []
                    if isinstance(role, dict)
                )
        if not source_roles:
            source_roles = [({}, role) for role in sequence if isinstance(role, dict)]

    rich_index = _rich_role_index(planning_brief)
    athlete_snapshot = _manifest_athlete_snapshot(finalizer_packet, planning_brief)

    manifest: list[dict] = []
    for week, role in source_roles:
        if role.get("render_mandatory") is False:
            continue
        label = _manifest_countdown_label(role)
        if not label:
            weekday = str(role.get("scheduled_day_hint") or "").strip().lower()
            for day in week.get("calendar_days") or []:
                if (isinstance(day, dict)
                    and str(day.get("weekday") or "").strip().lower() == weekday
                    and isinstance(day.get("d_day"), int)):
                    label = f"D-{day['d_day']}"
                    break
        d_day_match = re.search(r"\bD-(\d{1,2})\b", label, re.I)
        d_day = int(d_day_match.group(1)) if d_day_match else None

        entry = {
            "week_index": week.get("week_index"),
            "phase": week.get("phase"),
            "scheduled_countdown_label": label,
            "scheduled_day_hint": role.get("scheduled_day_hint"),
            "role_key": role.get("role_key"),
            "category": role.get("category"),
        }

        assignments = role.get("selected_exercise_assignments")
        has_membership_key = "selected_exercise_assignments" in role
        priced_membership = (
            isinstance(assignments, list) and bool(assignments)
        )

        if priced_membership:
            lines, unresolved = _closed_membership_exercise_lines(role, assignments)
            entry.update({
                "authority": AUTHORITY_CLOSED_SELECTED_ASSIGNMENTS,
                "selected_count": len(assignments),
                "exercise_lines": lines,
                "unresolved": unresolved,
            })
            _attach_manifest_exercise_keys(entry, assignments)
            _attach_manifest_label_and_microdose(entry, role)
            manifest.append(entry)
            continue

        # Deterministic authority is checked BEFORE the closed-membership branch
        # for a role with no priced members. ``selected_exercise_assignments=[]``
        # is a deliberate closed-membership sentinel that the packet preserves
        # (see _compact_role), and in production it sits on exactly the roles
        # whose bodies the planner already wrote — hard_sparring_day,
        # tactical_watch, breathing_reset, tactical_cue_card. Testing the key
        # first classified them as zero-exercise closed roles, so the resolver
        # never ran and their decided content never reached the first pass.
        #
        # The packet also compacts roles for the model (it strips the long body of
        # a selected_drill_locked role, and internal governance keys), so
        # authority is decided against the pre-compaction Stage 1 role where one
        # exists: compaction can never make decided content look undecided.
        rich_role = _manifest_rich_role(role, label, rich_index)
        render = authoritative_render_for_role(
            rich_role if rich_role is not None else role,
            week=week,
            athlete_snapshot=athlete_snapshot,
            d_day=d_day,
            display_text_fallback=role.get("display_text"),
        )
        if render is not None and render["authority"] != AUTHORITY_CLOSED_SELECTED_ASSIGNMENTS:
            entry.update({
                "authority": render["authority"],
                "exact_body_lines": list(render.get("body_lines") or []),
                "unresolved": [],
            })
            if render.get("athlete_facing_label"):
                entry["athlete_facing_label"] = render["athlete_facing_label"]
            if render.get("contact_load"):
                entry["contact_load"] = render["contact_load"]
            if render.get("zero_physical_load"):
                entry["zero_physical_load"] = True
            if render.get("priority_microdose"):
                entry["priority_microdose"] = render["priority_microdose"]
            if isinstance(assignments, list) and not assignments:
                # The sentinel keeps its meaning alongside the exact body: the
                # session carries zero exercises and membership is still closed,
                # so Stage 2 may not open it up and select work for the day.
                entry["selected_count"] = 0
                entry["exercise_lines"] = []
            manifest.append(entry)
            continue

        if has_membership_key:
            if not isinstance(assignments, list):
                continue
            lines, unresolved = _closed_membership_exercise_lines(role, assignments)
            entry.update({
                "authority": AUTHORITY_CLOSED_SELECTED_ASSIGNMENTS,
                "selected_count": len(assignments),
                "exercise_lines": lines,
                "unresolved": unresolved,
            })
            _attach_manifest_exercise_keys(entry, assignments)
            _attach_manifest_label_and_microdose(entry, role)
            manifest.append(entry)
            continue

        # Genuinely open role: Stage 2 still authors it under the existing
        # bounded rules. Inventing a body here would be a second planner.
    return manifest


def _manifest_athlete_snapshot(
    finalizer_packet: dict, planning_brief: dict | None
) -> dict:
    for source in (
        finalizer_packet.get("athlete_snapshot"),
        (planning_brief or {}).get("athlete_snapshot"),
        (planning_brief or {}).get("athlete_model"),
    ):
        if isinstance(source, dict):
            return source
    return {}


def _manifest_rich_role(
    role: dict, label: str, rich_index: dict[tuple[str, str, str], dict]
) -> dict | None:
    """The pre-compaction Stage 1 role behind a packet role, when there is one."""
    role_key = str(role.get("role_key") or "").strip()
    weekday = str(role.get("scheduled_day_hint") or role.get("real_weekday") or "").strip().lower()
    for key in (
        (role_key, label, weekday),
        (role_key, label, ""),
        (role_key, "", weekday),
    ):
        found = rich_index.get(key)
        if isinstance(found, dict):
            return found
    return None


def _attach_manifest_exercise_keys(entry: dict, assignments: list) -> None:
    """Planner-owned identity, one per member in membership order.

    The server re-stamps it onto the rendered blocks after conversion; the model
    is never trusted to carry it.
    """
    keys = [
        str(assignment.get("exercise_key") or "")
        for assignment in assignments
        if isinstance(assignment, dict)
    ]
    if any(keys):
        entry["exercise_keys"] = keys


def _attach_manifest_label_and_microdose(entry: dict, role: dict) -> None:
    label = str(role.get("athlete_facing_label") or role.get("label") or "").strip()
    if label:
        entry["athlete_facing_label"] = label
    microdose = role.get("priority_microdose")
    if isinstance(microdose, dict) and str(microdose.get("name") or "").strip():
        entry["priority_microdose"] = microdose


def _closed_membership_exercise_lines(
    role: dict, assignments: list
) -> tuple[list[str], list[dict]]:
    lines: list[str] = []
    unresolved: list[dict] = []
    # Strength composition records selected membership separately from the
    # scheduled-day dose resolver.  The resolver's records are authoritative
    # and identify a source member by its slot id plus selected name.
    strength_doses: dict[tuple[str, str], list[Any]] = {}
    for resolved in role.get("effective_strength_prescriptions") or []:
        if not isinstance(resolved, dict):
            continue
        slot_id = str(resolved.get("slot_id") or "").strip()
        resolved_name = str(resolved.get("name") or "").strip()
        if slot_id and resolved_name:
            strength_doses.setdefault((slot_id, resolved_name), []).append(
                resolved.get("effective_prescription")
            )
    for index, assignment in enumerate(assignments):
        if not isinstance(assignment, dict):
            unresolved.append({"index": index, "reason": "invalid_assignment"})
            continue
        name = str(assignment.get("name") or assignment.get("exercise_name") or "").strip()
        prescription = assignment.get("effective_prescription")
        if not prescription:
            source_key = (str(assignment.get("slot_id") or "").strip(), name)
            resolved_doses = strength_doses.get(source_key) or []
            if resolved_doses:
                prescription = resolved_doses.pop(0)
            elif not isinstance(role.get("strength_dose_cap"), dict):
                prescription = assignment.get("base_prescription")
        if isinstance(prescription, dict):
            prescription = (
                prescription.get("display") or prescription.get("dose")
                or prescription.get("text")
            )
        prescription = str(prescription or "").strip()
        if not name:
            unresolved.append({
                "index": index, "name": name, "reason": "missing_name",
            })
            continue
        if not prescription:
            # Membership is closed; dosing is not. Dropping the line here
            # silently deleted a scheduled exercise from the render while
            # selected_count still demanded it. The member is rendered and
            # the finalizer authors a dose from athlete context instead.
            # Deterministic caps still bind: where a countdown envelope
            # exists the validator enforces it against the authored dose.
            lines.append(f"- {name}: {_DOSE_UNRESOLVED_MARKER}")
            unresolved.append({
                "index": index, "name": name,
                "reason": "missing_effective_prescription",
                "action": "finalizer_prescribes_from_athlete_context",
            })
            continue
        lines.append(f"- {name}: {prescription}")
    return lines, unresolved


def _athlete_profile_block(planning_brief: dict | None, stage2_payload: dict) -> dict:
    if isinstance(planning_brief, dict):
        athlete_snapshot = planning_brief.get("athlete_snapshot")
        if isinstance(athlete_snapshot, dict):
            return athlete_snapshot
        athlete_model = planning_brief.get("athlete_model")
        if isinstance(athlete_model, dict):
            return athlete_model
    athlete_model = stage2_payload.get("athlete_model")
    return athlete_model if isinstance(athlete_model, dict) else {}


def _continuation_days_until_fight(
    finalizer_packet: dict,
    planning_brief: dict | None,
) -> Any:
    """Best-effort ``days_until_fight`` for the countdown-continuation fallback."""
    for source in (finalizer_packet, planning_brief):
        if not isinstance(source, dict):
            continue
        for model_key in ("athlete_model", "athlete_snapshot"):
            model = source.get(model_key)
            if isinstance(model, dict) and model.get("days_until_fight") is not None:
                return model.get("days_until_fight")
        fight_demands = source.get("fight_demands")
        if isinstance(fight_demands, dict) and fight_demands.get("days_until_fight") is not None:
            return fight_demands.get("days_until_fight")
    return None


def _countdown_continuation_map_from_packet(
    finalizer_packet: dict,
    planning_brief: dict | None,
) -> list[dict]:
    selected_plan = finalizer_packet.get("selected_plan", {})
    if isinstance(selected_plan, dict):
        late_fight_plan_spec = selected_plan.get("late_fight_plan_spec", {}) or {}
        if isinstance(late_fight_plan_spec, dict):
            continuation_map = list(late_fight_plan_spec.get("countdown_mode_sequence", []) or [])
            if continuation_map:
                return continuation_map

        days_out_payload = selected_plan.get("days_out_payload", {}) or {}
        if isinstance(days_out_payload, dict):
            continuation_map = list(days_out_payload.get("countdown_mode_sequence", []) or [])
            if continuation_map:
                return continuation_map

    if isinstance(planning_brief, dict):
        continuation_map = list(
            (
                planning_brief.get("late_fight_plan_spec", {}) or {}
            ).get("countdown_mode_sequence", [])
        )
        if continuation_map:
            return continuation_map
        continuation_map = list(
            (planning_brief.get("days_out_payload", {}) or {}).get("countdown_mode_sequence", [])
        )
        if continuation_map:
            return continuation_map

    # Fallback for normal-camp (D-14+) plans. Their planning brief carries no
    # late_fight_plan_spec / days_out_payload (D-14+ stays on the normal camp
    # planner), but the plan's continuous calendar still reaches the D-13 -> D-0
    # fight-week tail. Derive the downstream continuation from days_until_fight so
    # those scheduled days inherit the existing late-fight mode contracts instead
    # of silently rendering as normal-camp work into fight week. Empty for late
    # plans (already handled above) and for undated / open plans.
    return _camp_downstream_countdown_continuation(
        _continuation_days_until_fight(finalizer_packet, planning_brief)
    )


def _append_countdown_continuation_instructions(
    *,
    mode_instructions: str,
    payload_mode: str,
    continuation_map: list[dict],
) -> str:
    if not continuation_map:
        return mode_instructions

    if len(continuation_map) <= 1:
        return mode_instructions

    # A late-fight/fight-week top mode carries non-empty mode instructions; a
    # normal-camp plan (camp_payload / camp_plan bucket) carries none. Use that to
    # pick the intro: the late-fight continuation-start plan continues its own
    # active countdown, whereas a camp plan only hands its D-13 -> D-0 tail to the
    # fight-week contracts and must NOT reframe its earlier camp days.
    is_camp_top_mode = not str(mode_instructions or "").strip()
    if is_camp_top_mode:
        intro = (
            "This plan stays on the normal camp planner from its generation day through "
            "D-14. From D-13 onward its scheduled countdown days follow the existing "
            "fight-week contracts mapped below — apply each mapped payload mode to its own "
            "D-day window, in descending order through D-0. Do not convert the earlier camp "
            "days into a late-fight countdown."
        )
    else:
        intro = (
            "Continue the active late-fight countdown from this start window through D-0 "
            "exactly as mapped below."
        )

    continuation_lines = ["COUNTDOWN CONTINUATION MAP", intro]

    for segment in continuation_map:
        stage_key = str(segment.get("stage_key") or "").strip()
        segment_mode = str(segment.get("payload_mode") or "").strip()
        start_day = segment.get("start_day")
        end_day = segment.get("end_day")
        if stage_key and segment_mode and isinstance(start_day, int) and isinstance(end_day, int):
            continuation_lines.append(
                f"- {stage_key}: {segment_mode} (D-{start_day} to D-{end_day})"
            )

    if len(continuation_lines) <= 2:
        return mode_instructions

    continuation_block = "\n".join(continuation_lines)
    if mode_instructions:
        return mode_instructions + "\n\n" + continuation_block
    return continuation_block


_STAGE1_NUTRITION_SECTION_RE = re.compile(
    r"^## Nutrition[ \t]*\n.*?(?=^#{1,2} |\Z)", re.MULTILINE | re.DOTALL
)


def strip_stage1_nutrition_section(plan_text: str) -> str:
    """Drop the Stage 1 ``## Nutrition`` section; nutrition is Stage 1-only."""
    return _STAGE1_NUTRITION_SECTION_RE.sub("", plan_text or "")


def build_stage2_handoff_text(
    *,
    stage2_payload: dict,
    plan_text: str,
    coach_notes: str = "",
    planning_brief: dict | None = None,
) -> str:
    planning_brief = build_stage2_llm_planning_brief(planning_brief)
    finalizer_packet = build_stage2_finalizer_packet(
        stage2_payload=stage2_payload,
        planning_brief=planning_brief,
    )

    athlete_profile = _athlete_profile_block(planning_brief, stage2_payload)
    render_mode = str(finalizer_packet.get("render_mode") or "").strip()
    # ``render_mode`` is the abstract bucket ("camp_plan",
    # "late_fight_countdown_only", "open_ongoing_system") used for dispatching
    # rendering rules. ``_handoff_mode_instructions`` keys on the specific
    # payload mode (e.g. ``pre_fight_compressed_payload``), so we must look
    # that up directly — using ``render_mode`` here drops mode instructions
    # entirely for every late-fight day.
    payload_mode = (
        stage2_payload.get("payload_mode")
        or stage2_payload.get("effective_stage2_mode")
        or render_mode
        or "camp_payload"
    )

    # ── Payload-mode-sensitive hard instructions ──────────────────
    mode_instructions = _handoff_mode_instructions(payload_mode)

    continuation_map = _countdown_continuation_map_from_packet(
        finalizer_packet=finalizer_packet,
        planning_brief=planning_brief,
    )
    mode_instructions = _append_countdown_continuation_instructions(
        mode_instructions=mode_instructions,
        payload_mode=payload_mode,
        continuation_map=continuation_map,
    )
    # Priority-hierarchy guidance is NOT restated here: the finalizer packet's
    # hard_rules already carry the full priority_focus doctrine (preserve
    # hierarchy, honour collisions via priority_focus.collision_detail /
    # collision_details, treat derived_clarification_tags as internal-only), and
    # the packet's selected_plan.priority_focus block supplies the underlying
    # values. A parallel prose section here only duplicated those rules.
    # Normal camps deliberately have no late-fight spec. Identify them from the
    # existing planner mode, never from missing policy data alone. Any late-fight
    # marker (even malformed) prevents this exemption and keeps the gate open.
    brief = planning_brief or {}
    normal_camp = (
        (brief.get("generator_mode") or stage2_payload.get("generator_mode"))
        in {"deterministic_planner_plus_ai_finalizer", "restriction_aware_candidate_generator"}
        and all(
            key not in source
            for source in (brief, stage2_payload)
            for key in ("payload_variant", "days_out_payload", "late_fight_plan_spec")
        )
    )
    sections = [
        build_finalizer_prompt(
            render_guards=finalizer_packet.get("render_guards"),
            selected_plan=finalizer_packet.get("selected_plan"),
            normal_camp=normal_camp,
        ).strip(),
        UNLXCK_FINAL_RENDER_CONTRACT.strip(),
    ]

    if mode_instructions:
        sections.append("PAYLOAD MODE INSTRUCTIONS\n" + mode_instructions)
    if render_mode == "open_ongoing_system":
        sections.append(_OPEN_ONGOING_RENDER_MODE_INSTRUCTIONS.strip())

    locked_manifest = _closed_membership_render_manifest(
        finalizer_packet, planning_brief=planning_brief
    )
    if locked_manifest:
        sections.append(
            "LOCKED SESSION RENDER MANIFEST\n"
            "This is the content Stage 1 has already decided for the first pass. "
            "Every entry must appear once, under its own day and role: these lines are "
            "decided, so render them - never rewrite, omit, merge, replace or reinterpret "
            "them. An entry's authority says what kind of decided content it is: "
            "closed_selected_assignments carries exact selected exercise membership in "
            "exercise_lines; deterministic_display_text, canonical_combat and "
            "fight_day_protocol carry the exact athlete-facing body in exact_body_lines, "
            "which you reproduce verbatim and do not expand, dose or embellish. "
            "athlete_facing_label is the day's session name. A priority_microdose belongs "
            "inside that same host session exactly once - never as its own session or day. "
            "An entry marked zero_physical_load is review/support only and adds no physical "
            "training load. An entry whose selected_count is 0 carries no exercises and its "
            "membership is still closed: render its body and add no exercises to that day. "
            "Render every exercise_lines entry once under its owning day and role, "
            "then add coaching details. selected_count is the required membership count. "
            "Do not promote one member to primary and discard the others. "
            "An entry whose dose reads DOSE_UNRESOLVED is a scheduled exercise the "
            "planner did not dose: prescribe an appropriate dose for it from the athlete "
            "profile and the scheduled day, stay inside any dose cap the packet supplies, "
            "and never render the marker itself. This licenses dosing only — never adding, "
            "restoring or substituting an exercise. Hard safety restrictions remain "
            "authoritative.\n"
            + _json_block(locked_manifest)
        )

    sections.append("FINALIZER PACKET\n" + _json_block(finalizer_packet))
    sections.append("ATHLETE PROFILE\n" + _json_block(athlete_profile))

    injury_context = stage2_payload.get("injury_context")
    if isinstance(injury_context, dict):
        sections.append("INJURY CONTEXT\n" + _json_block(injury_context))

    cleaned_notes = (coach_notes or "").strip()
    if cleaned_notes:
        sections.append("COACH NOTES\n" + cleaned_notes)

    sections.append("STAGE 1 DRAFT PLAN\n" + strip_stage1_nutrition_section(plan_text).strip())

    if locked_manifest:
        sections.append(
            "FIRST-PASS COMPLETENESS CHECK\n"
            "Before returning the plan, compare every manifest entry against your output: "
            "each one must exist on its own day, with its label and its exact decided "
            "lines. A manifest entry with no exercise_lines is not optional - a support "
            "insert, a resolved combat day and the fight-day protocol are decided sessions "
            "exactly like a closed exercise role. "
            "Compare each closed role against the locked "
            "manifest. Preserve the required exercise count, exact names, authorised "
            "effective doses, and day ownership. Do not remove a legal member to "
            "shorten prose, satisfy an open-role fallback rule, or improve perceived "
            "session balance. If source or safety conflicts remain, do not invent "
            "replacement work or claim missing goal coverage."
        )

    return "\n\n---\n\n".join(section for section in sections if section.strip())


# Late-fight phase eligibility (fightcamp.late_fight_phase_eligibility), applied
# where these functions are defined so every importer gets the governed version.
build_planning_brief = governed_build_planning_brief(build_planning_brief)
_build_late_fight_allowed_exercises_by_day = governed_late_fight_allowed_exercises_by_day(
    _build_late_fight_allowed_exercises_by_day,
    payload=sys.modules[__name__],
)
