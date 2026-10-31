"""Authoritative exercise composition for scheduled physical sessions.

Candidate pools remain planning evidence. This module records the smaller,
deterministic set that the calendar actually assigns to each session; dose
resolution remains downstream of this boundary.

Normal strength composition is deliberately reduce-only:
- Stage 1's selected slots are the only candidates considered.
- session role sets the fresh-state exercise ceiling;
- fatigue, weight-cut severity, and injury state can only tighten that ceiling;
- existing exercise tags/classification prevent redundant same-quality loading;
- Stage 2 receives the final closed membership and never chooses replacements.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .exercise_identity import normalize_exercise_key, stamp_exercise_key
from .normalization import normalize_fatigue_level
from .planner_context import get_planner_athlete_model
from .late_fight_phase_eligibility import scheduled_phase_for_role
from .calendar_context import role_d_day
from .calendar_integrity import relocate_or_suppress_role_for_recovery
from .combat_load_policy import BodyRegion, SessionStress, StressLevel
from .prescription_resolver import (
    _parse_sets_reps,
    _role_kind,
    athlete_dose_state,
    has_verified_low_cost,
)
from .working_dose import working_dose
from .strength_session_quality import classify_strength_item, strength_adaptation_relevance
from .training_context import normalize_equipment_list
from .config import (
    athlete_round_seconds,
    conditioning_effective_dose,
    conditioning_work_is_timed,
    conditioning_round_prescription,
    conditioning_dose_active_work_seconds,
    conditioning_dose_minutes,
    conditioning_phase_workload_envelope as _conditioning_phase_workload_envelope,
)
from .bank_schema import has_meaningful_fulfillment_authority
from .weight_cut import (
    compute_cut_severity_score,
    cut_health_bucket,
    resolve_cut_health_bucket,
)


_NORMAL_STRENGTH_ROLE_CAPS: dict[str, int] = {
    "primary_strength_day": 6,
    "structural_strength_day": 6,
    "secondary_strength_day": 3,
    "neural_plus_strength_day": 3,
    "transfer_strength_day": 3,
    "strength_touch_day": 2,
    "neural_primer_day": 2,
    "small_strength_touch_day": 2,
}
_DEFAULT_NORMAL_STRENGTH_CAP = 3
# Full strength / strength+power roles capped below the preferred session size
# may take ONE extra compatible Stage 1 exercise on a clear day, and only for a
# fresh athlete (pressure 0): any fatigue, cut or injury pressure keeps its own
# reduced cap. Composition only proposes the extra item;
# ``apply_standalone_strength_minimum`` keeps it only when the realised-load
# revalidation outcome and every existing dose are unchanged by it.
_STANDALONE_STRENGTH_MINIMUM = 4
_STANDALONE_STRENGTH_MAX_PRESSURE = 0
_STANDALONE_STRENGTH_MINIMUM_ROLE_KEYS = frozenset(
    {"secondary_strength_day", "neural_plus_strength_day", "transfer_strength_day"}
)

_FATIGUE_PRESSURE = {"low": 0, "moderate": 1, "high": 2}
_CUT_PRESSURE = {
    "none": 0,
    "low": 0,
    "moderate": 1,
    "high": 2,
    "critical": 3,
    "extreme": 3,
}

_STRENGTH_FAMILIES = frozenset({"lower_strength", "upper_strength"})
_POWER_FAMILIES = frozenset({"lower_power", "rotational_power", "upper_power"})

_ADJACENT_STRENGTH_RECOVERY_COST_FIELDS = (
    "movement_cost",
    "impact_cost",
    "eccentric_cost",
    "landing_cost",
    "cns_load",
    "soreness_risk",
)
_ADJACENT_STRENGTH_RECOVERY_REASON = "substantial_repeated_mechanical_loading"

_ROLE_REQUIRED_FAMILY_GROUPS: dict[str, tuple[frozenset[str], ...]] = {
    "primary_strength_day": (_STRENGTH_FAMILIES,),
    "structural_strength_day": (_STRENGTH_FAMILIES,),
    "secondary_strength_day": (_STRENGTH_FAMILIES,),
    "neural_plus_strength_day": (_POWER_FAMILIES, _STRENGTH_FAMILIES),
    "transfer_strength_day": (_POWER_FAMILIES, _STRENGTH_FAMILIES),
}


def assignment_from_slot(phase: str, slot_group: str, slot: dict[str, Any]) -> dict[str, Any] | None:
    selected = slot.get("selected") if isinstance(slot.get("selected"), dict) else {}
    name = str(selected.get("name") or "").strip()
    if not name:
        return None
    assignment = {
        "slot_id": slot.get("slot_id"),
        "name": name,
        "source_phase": phase,
        "slot_group": slot_group,
        "source_session_index": slot.get("session_index"),
    }
    # Identity is decided here, with the selection, before any model renders
    # the plan: Stage 2 may reword the block, never change what it is.
    stamp_exercise_key(assignment)
    for key in ("support_only", "meaningful_stress", "fulfillment_authority"):
        if key in selected:
            assignment[key] = selected[key]
    base_prescription = str(selected.get("prescription") or "").strip()
    if base_prescription:
        assignment["base_prescription"] = base_prescription
    # Authored mechanical tags travel with the assignment. Without them a
    # composed session's realised body-region and impact cost is unknowable
    # downstream: the candidate pool the slot came from is compacted, so the
    # assignment is the only surviving record of what the session mechanically is.
    tags = option_mechanical_risk_tags(selected)
    if tags:
        assignment["mechanical_risk_tags"] = sorted(tags)
    notes = _selected_coaching_notes(selected)
    if notes:
        assignment["coaching_notes"] = notes
    assignment.update(_selected_coaching_fields(selected))
    return assignment


def _selected_coaching_notes(option: dict[str, Any] | None) -> str:
    """Return the authored exercise-bank coaching note for a selected option.

    The note travels with the selected assignment so Stage 2 keeps the bank's
    execution guidance without a separate join back to the candidate pool. It is
    coaching evidence only and never a dose authority.
    """
    if not isinstance(option, dict):
        return ""
    note = option.get("notes")
    if not str(note or "").strip():
        metadata = option.get("selection_metadata")
        if isinstance(metadata, dict):
            note = metadata.get("notes")
    return str(note or "").strip()


def _selected_coaching_fields(option: dict[str, Any]) -> dict[str, Any]:
    """Keep explicit authored instructions on the selected assignment."""
    metadata = option.get("selection_metadata") or {}
    fields = {}
    explanation = (option.get("execution_instructions") or metadata.get("execution_instructions")
                   or option.get("description") or metadata.get("description"))
    if isinstance(explanation, str) and explanation.strip():
        fields["execution_instructions"] = explanation.strip()
    for key in ("coaching_cues", "regression_options", "progression_rule", "stop_rules"):
        value = option.get(key) or metadata.get(key)
        if isinstance(value, list):
            fields[key] = list(value)
        elif isinstance(value, str) and value.strip():
            fields[key] = value
    return fields


def _rehab_assignment_from_slot(phase: str, slot: dict[str, Any]) -> dict[str, Any] | None:
    """Preserve the rehab identity needed by final rendering and completion."""
    assignment = assignment_from_slot(phase, "rehab_slots", slot)
    if assignment is None:
        return None

    selected = slot.get("selected") if isinstance(slot.get("selected"), dict) else {}
    for key in (
        "source",
        "function_class",
        "rehab_function_label",
        "rehab_drill_id",
        "movement_patterns",
        "restriction_tags",
        "mechanical_risk_tags",
    ):
        value = selected.get(key)
        if value not in (None, "", []):
            assignment[key] = value

    purpose = str(selected.get("why") or slot.get("purpose") or "").strip()
    if purpose:
        assignment["purpose"] = purpose
    rehab_role = str(slot.get("role") or "").strip()
    if rehab_role:
        assignment["rehab_role"] = rehab_role
    return assignment


def _bounded_rehab_slots(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Choose at most two selected drills while representing separate injuries first."""
    valid = [
        slot
        for slot in slots
        if isinstance(slot, dict)
        and isinstance(slot.get("selected"), dict)
        and str(slot["selected"].get("name") or "").strip()
    ]
    selected: list[dict[str, Any]] = []
    represented_roles: set[str] = set()

    for slot in valid:
        rehab_role = str(slot.get("role") or slot.get("slot_id") or "").strip()
        if rehab_role in represented_roles:
            continue
        represented_roles.add(rehab_role)
        selected.append(slot)
        if len(selected) == 2:
            return selected

    for slot in valid:
        if slot in selected:
            continue
        selected.append(slot)
        if len(selected) == 2:
            break
    return selected


def _normalized_fatigue(athlete_model: dict[str, Any]) -> str:
    return normalize_fatigue_level(athlete_model)


def _resolved_cut_bucket(athlete_model: dict[str, Any]) -> str:
    """Strain bucket driving session composition pressure (dose, not calendar)."""
    explicit = str(athlete_model.get("cut_health_bucket") or "").strip().lower()
    if explicit in _CUT_PRESSURE:
        return explicit

    flags = {
        str(flag).strip().lower()
        for flag in (athlete_model.get("readiness_flags") or [])
        if str(flag).strip()
    }
    active_cut = bool(athlete_model.get("weight_cut_risk")) or bool(
        flags & {"active_weight_cut", "aggressive_weight_cut", "extreme_weight_cut"}
    )
    if not active_cut:
        # A legacy snapshot may carry a capacity bucket and no activity flags at
        # all. That bucket is the only cut signal it has, so it still stands in
        # for strain here rather than silently reading as "no cut".
        legacy = str(athlete_model.get("cut_severity_bucket") or "").strip().lower()
        return legacy if legacy in _CUT_PRESSURE else "none"

    # Recompute strain from the score before ever reading the capacity bucket:
    # the two disagree by design, so a capacity fallback understates strain.
    return resolve_cut_health_bucket(athlete_model) or "none"


def _injury_restricted(athlete_model: dict[str, Any]) -> bool:
    # One readiness authority for composition and dose resolution, so a stable
    # surface-only injury never tightens one while the other stays open.
    return bool(athlete_dose_state(athlete_model).get("injury_restricted"))


def _pressure_state_from_components(
    *, fatigue: str, cut_bucket: str, injury_restricted: bool
) -> dict[str, Any]:
    fatigue = fatigue if fatigue in _FATIGUE_PRESSURE else "low"
    cut_bucket = cut_bucket if cut_bucket in _CUT_PRESSURE else "none"

    fatigue_pressure = _FATIGUE_PRESSURE[fatigue]
    cut_pressure = _CUT_PRESSURE[cut_bucket]
    injury_pressure = 1 if injury_restricted else 0
    pressures = (fatigue_pressure, cut_pressure, injury_pressure)
    active_stressors = sum(value > 0 for value in pressures)

    pressure = max(pressures, default=0)
    if active_stressors >= 2:
        pressure += 1
    pressure = min(3, pressure)

    # An injury still informs dose, recovery and movement safety, but must not
    # shrink every strength session's exercise count or movement-family budget.
    # Only fatigue and weight-cut strain govern that generic exercise budget.
    exercise_count_pressure = max(fatigue_pressure, cut_pressure)
    if fatigue_pressure > 0 and cut_pressure > 0:
        exercise_count_pressure = min(3, exercise_count_pressure + 1)

    return {
        "pressure": pressure,
        "exercise_count_pressure": exercise_count_pressure,
        "fatigue": fatigue,
        "fatigue_pressure": fatigue_pressure,
        "cut_severity_bucket": cut_bucket,
        "cut_pressure": cut_pressure,
        "injury_restricted": injury_restricted,
        "injury_pressure": injury_pressure,
        "active_stressors": active_stressors,
    }


def composition_pressure_state(athlete_model: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve reduce-only session-composition pressure from existing athlete buckets.

    The strongest active signal owns the base pressure. A second independent
    stressor adds one interaction step, capped at preservation mode (3). This
    avoids naively summing three moderate signals while still recognising that
    combined fatigue/cut/injury meaningfully reduces recovery capacity.
    """
    model = athlete_model if isinstance(athlete_model, dict) else {}
    return _pressure_state_from_components(
        fatigue=_normalized_fatigue(model),
        cut_bucket=_resolved_cut_bucket(model),
        injury_restricted=_injury_restricted(model),
    )


def _float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _active_weight_cut(
    athlete_model: dict[str, Any], *, current_bucket: str, cut_pct: float | None
) -> bool:
    flags = {
        str(flag).strip().lower()
        for flag in (athlete_model.get("readiness_flags") or [])
        if str(flag).strip()
    }
    return bool(
        athlete_model.get("weight_cut_risk")
        or flags & {"active_weight_cut", "aggressive_weight_cut", "extreme_weight_cut"}
        or (cut_pct is not None and cut_pct > 0.0)
        or current_bucket not in {"", "none"}
    )


def _composition_context_from_model(athlete_model: dict[str, Any] | None) -> dict[str, Any]:
    model = athlete_model if isinstance(athlete_model, dict) else {}
    current_state = composition_pressure_state(model)
    cut_pct = _float_or_none(model.get("weight_cut_pct"))
    if cut_pct is not None and cut_pct <= 0.0:
        cut_pct = None

    return {
        **current_state,
        "generation_days_until_fight": _int_or_none(model.get("days_until_fight")),
        "weight_cut_pct": cut_pct,
        "active_weight_cut": _active_weight_cut(
            model,
            current_bucket=str(current_state["cut_severity_bucket"]),
            cut_pct=cut_pct,
        ),
    }


def _effective_role_cap(role_key: str, pressure: int) -> tuple[int, int]:
    base_cap = _NORMAL_STRENGTH_ROLE_CAPS.get(role_key, _DEFAULT_NORMAL_STRENGTH_CAP)
    if pressure <= 0:
        return base_cap, base_cap
    if role_key in {"primary_strength_day", "structural_strength_day"}:
        # A six-exercise ceiling tapers gradually with fatigue and cut strain.
        # Authoritative dose, injury and calendar safety rules remain in place.
        return base_cap, max(3, base_cap - pressure)
    if pressure == 1:
        return base_cap, max(2, base_cap - 1)
    if pressure == 2:
        return base_cap, max(2, base_cap - 2)
    return base_cap, 2


def _strength_role_is_standalone(
    weekly_role_map: dict[str, Any], week: dict[str, Any], week_ordinal: int, role: dict[str, Any]
) -> bool:
    """True only when the role's day holds no contact or other meaningful session.

    Fails closed: an undated or unclassifiable role, a same-day contact or
    meaningful strength/conditioning session, or any non-ALLOW verdict from the
    shared calendar policy (e.g. the day before hard contact) is not standalone.
    """
    from .calendar_context import build_events, classify_role, week_scope
    from .combat_load_policy import (
        CONTACT_LOAD_CLASSES,
        LoadClass,
        PlacementDirective,
        evaluate_candidate_at_position,
    )

    d_day = role_d_day(week, role)
    profile = classify_role(role)
    if d_day is None or profile is None:
        return False
    events = build_events(weekly_role_map, exclude_role=role)
    blocking = CONTACT_LOAD_CLASSES | {
        LoadClass.MEANINGFUL_STRENGTH,
        LoadClass.MEANINGFUL_CONDITIONING,
    }
    if any(
        event.position == -d_day and event.profile.load_class in blocking
        for event in events
    ):
        return False
    decision = evaluate_candidate_at_position(
        profile,
        candidate_position=-d_day,
        events=events,
        candidate_scope=week_scope(week, week_ordinal),
    )
    return decision.directive is PlacementDirective.ALLOW


def _slot_selected_item(slot: dict[str, Any]) -> dict[str, Any]:
    selected = slot.get("selected") if isinstance(slot.get("selected"), dict) else {}
    merged = {
        key: value
        for key, value in slot.items()
        if key not in {"selected", "alternates"}
    }
    merged.update(selected)
    return merged


def _composition_families(slot: dict[str, Any]) -> tuple[set[str], dict[str, Any]]:
    """Map existing Stage 1 quality metadata into broad session families.

    Stage 1 already serializes ``base_categories`` and ``support_only`` from the
    exercise classifier. Those fields are preferred as authoritative evidence;
    reclassification is only a fallback for older or hand-built slots. It is not
    unioned in: the serialized slot no longer carries the authored body
    category, so reclassifying it would re-add the "pull"/"press" word hint the
    Stage 1 classification already corrected.
    """
    item = _slot_selected_item(slot)
    profile = classify_strength_item(item)
    serialized = [
        *(slot.get("base_categories") or []),
        *(item.get("base_categories") or []),
    ]
    has_serialized = "base_categories" in slot or "base_categories" in item
    categories = {
        str(value).strip()
        for value in (serialized if has_serialized else profile.get("base_categories") or [])
        if str(value).strip()
    }
    support_only = bool(
        slot.get("support_only")
        or item.get("support_only")
        or profile.get("support_only")
    )

    families: set[str] = set()
    if "lower_body_loaded" in categories:
        families.add("lower_strength")
    if "upper_body_push_pull" in categories:
        families.add("upper_strength")
    if "lower_body_power" in categories:
        families.add("lower_power")
    if "rotational_power" in categories:
        families.add("rotational_power")
    if "upper_body_ballistic" in categories:
        families.add("upper_power")
    if support_only:
        families.add("support")

    effective_profile = dict(profile)
    effective_profile["base_categories"] = sorted(categories)
    effective_profile["support_only"] = support_only
    return families, effective_profile


def _serialized_quality_class(slot: dict[str, Any]) -> str:
    item = _slot_selected_item(slot)
    return str(slot.get("quality_class") or item.get("quality_class") or "").strip()


def _slot_uses_loaded_equipment(slot: dict[str, Any]) -> bool:
    item = _slot_selected_item(slot)
    equipment = normalize_equipment_list(item.get("equipment") or [])
    return bool(
        {
            "barbell",
            "trap_bar",
            "dumbbell",
            "dumbbells",
            "kettlebell",
            "kettlebells",
            "cable",
            "landmine",
            "sandbag",
            "bulgarian_bag",
            "log",
            "atlas_stone",
            "water_jug",
            "weight_vest",
            "plate",
            "partner",
        }
        & set(equipment)
    )


def _slot_has_core_balance_signal(slot: dict[str, Any]) -> bool:
    item = _slot_selected_item(slot)
    tags = {
        re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
        for value in [
            *(slot.get("tags") or []),
            *(item.get("tags") or []),
            *(slot.get("movement_patterns") or []),
            *(item.get("movement_patterns") or []),
            slot.get("movement"),
            item.get("movement"),
        ]
        if str(value).strip()
    }
    return bool(
        tags
        & {
            "core",
            "core_stability",
            "core_strength",
            "trunk",
            "trunk_strength",
            "anti_rotation",
            "balance",
            "stability",
            "proprioception",
        }
    )


def _slot_priority(slot: dict[str, Any], original_index: int) -> tuple[int, int]:
    try:
        priority = int(slot.get("priority"))
    except (TypeError, ValueError):
        priority = 10_000
    return priority, original_index


def option_mechanical_risk_tags(option: dict[str, Any] | None) -> set[str]:
    """Authored ``mech_*`` tags for one selected bank option.

    The bank states them on the item and the selector mirrors them into
    ``selection_metadata``; both shapes occur, so both are read. This is the one
    item-level extraction, shared by the slot-level helper and by the assignment
    builders, so a tag can never survive into one view of a session and be
    missing from another.
    """
    if not isinstance(option, dict):
        return set()
    metadata = option.get("selection_metadata")
    nested_tags = (
        metadata.get("mechanical_risk_tags", []) if isinstance(metadata, dict) else []
    )
    return {
        str(value).strip().lower()
        for value in [*(option.get("mechanical_risk_tags") or []), *nested_tags]
        if str(value).strip().lower().startswith("mech_")
    }


def _slot_mechanical_risk_tags(slot: dict[str, Any]) -> set[str]:
    return {
        str(value).strip().lower()
        for value in (slot.get("mechanical_risk_tags") or [])
        if str(value).strip().lower().startswith("mech_")
    } | option_mechanical_risk_tags(_slot_selected_item(slot))


def slot_adaptations(slot: dict[str, Any]) -> frozenset[str]:
    """The adaptation relevance Stage 1 stamped on this slot's selected item.

    Serialized slots carry no method or authored category, so Stage 1 records
    ``strength_adaptation_relevance`` in the selection metadata. A hand-built
    slot without that stamp is classified directly.
    """
    item = _slot_selected_item(slot)
    metadata = item.get("selection_metadata") if isinstance(item.get("selection_metadata"), dict) else {}
    stamped = {
        str(value).strip()
        for value in [metadata.get("primary_adaptation"), *(metadata.get("secondary_adaptations") or [])]
        if str(value or "").strip() and str(value).strip() != "unknown"
    }
    return frozenset(stamped) if stamped else strength_adaptation_relevance(item)


def _candidate_records(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, slot in enumerate(slots):
        assignment = assignment_from_slot("", "strength_slots", slot)
        if not assignment:
            continue
        families, profile = _composition_families(slot)
        serialized_quality_class = _serialized_quality_class(slot)
        serialized_support_only = serialized_quality_class in {"support_accessory", "support_isometric", "rehab_support"}
        has_major_family = bool(families - {"support"})
        support_only = bool(serialized_support_only or (profile.get("support_only") and not has_major_family))
        item = _slot_selected_item(slot)
        # Authored meaningful stress (a heavy sled, yoke or carry) is never
        # low-cost support, whatever class its name or tags produced.
        governed_meaningful_stress = item.get("meaningful_stress") is True or item.get("support_only") is False
        adaptations = slot_adaptations(slot)
        records.append(
            {
                "slot": slot,
                "name": assignment["name"],
                "families": families,
                "support_only": support_only,
                "core_balance_support": bool(
                    profile.get("core_balance_support")
                    or (
                        serialized_support_only
                        and _slot_has_core_balance_signal(slot)
                        and not governed_meaningful_stress
                    )
                ),
                "adaptations": adaptations,
                "trunk_targeted": "trunk_strength" in adaptations,
                "loaded_pattern": bool(profile.get("loaded_pattern") and (not serialized_support_only or _slot_uses_loaded_equipment(slot))),
                "force_isometric": bool(profile.get("force_isometric")),
                "power_pattern": bool(profile.get("power_pattern")),
                "quality_class": serialized_quality_class or str(profile.get("quality_class") or ""),
                "mechanical_risk_tags": _slot_mechanical_risk_tags(slot),
                "verified_low_recovery_cost": has_verified_low_cost(
                    slot,
                    fields=_ADJACENT_STRENGTH_RECOVERY_COST_FIELDS,
                ),
                "material_movement_cost": not has_verified_low_cost(
                    slot,
                    fields=("movement_cost",),
                ),
                "sort_key": _slot_priority(slot, index),
                "original_index": index,
            }
        )
    return sorted(records, key=lambda item: item["sort_key"])


def _would_exceed_family_limit(
    families: set[str],
    family_counts: dict[str, int],
    family_limit: int,
) -> bool:
    major = families - {"support"}
    return any(family_counts.get(family, 0) >= family_limit for family in major)


def _add_record(
    record: dict[str, Any],
    selected: list[dict[str, Any]],
    family_counts: dict[str, int],
) -> None:
    selected.append(record)
    for family in record["families"]:
        family_counts[family] = family_counts.get(family, 0) + 1


_GROUP_ADAPTATIONS = {_STRENGTH_FAMILIES: "strength", _POWER_FAMILIES: "power"}


def _backs_required_group(record: dict[str, Any], group: frozenset[str]) -> bool:
    adaptation = _GROUP_ADAPTATIONS.get(group)
    return bool(record["families"] & group) and adaptation in record.get("adaptations", ())


def _select_bounded_records(
    records: list[dict[str, Any]],
    *,
    role_key: str,
    cap: int,
    pressure: int,
    preserve_trunk_support: bool = False,
    preserve_adaptation: str = "",
    preserve_weakness: str = "",
    preserve_strength: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    # The merged GPP/SPP pool can contain the same exercise in two slots.
    unique_records = {}
    for record in records:
        unique_records.setdefault(normalize_exercise_key(record["name"]), record)
    records = list(unique_records.values())
    family_limit = 2 if pressure == 0 else 1
    selected: list[dict[str, Any]] = []
    family_counts: dict[str, int] = {}
    dropped: dict[str, str] = {}

    # A family says which region and pattern a lift loads; it does not say the
    # lift develops strength. A plyometric push-up sits in the upper-strength
    # family by its push pattern. When the session's candidates include a record
    # whose own adaptation evidence backs a required group, only such a record
    # satisfies it; otherwise the family match stands, as before.
    backed_groups = {
        group
        for group in _ROLE_REQUIRED_FAMILY_GROUPS.get(role_key, ())
        if any(_backs_required_group(record, group) for record in records)
    }

    def _covers(record: dict[str, Any], group: frozenset[str]) -> bool:
        if not record["families"] & group:
            return False
        return group not in backed_groups or _backs_required_group(record, group)

    unsatisfied_groups = list(_ROLE_REQUIRED_FAMILY_GROUPS.get(role_key, ()))
    # Reserve the primary adaptation before family filling spends the cap.
    # Family balance cannot substitute for the athlete's actual goal.
    primary = next((r for r in records if preserve_adaptation in r.get("adaptations", ())), None) if preserve_adaptation else None
    if primary is not None and cap > 0:
        _add_record(primary, selected, family_counts)
        unsatisfied_groups = [group for group in unsatisfied_groups if not _covers(primary, group)]
    # Complement the primary goal inside the same established exercise cap.
    weakness = next((r for r in records if preserve_weakness in r.get("adaptations", ())), None) if preserve_weakness else None
    if weakness is not None and weakness not in selected and len(selected) < cap:
        _add_record(weakness, selected, family_counts)
        unsatisfied_groups = [group for group in unsatisfied_groups if not _covers(weakness, group)]
    # Spend the existing cap on selected stimuli before filling generic movement
    # families. Power work may share a squat/push family without maintaining
    # strength; bracing during that work is not dedicated trunk training either.
    for adaptation, required in (("strength", preserve_strength), ("trunk_strength", preserve_trunk_support)):
        if not required or len(selected) >= cap or any(adaptation in r["adaptations"] for r in selected):
            continue
        candidate = next((r for r in records if r not in selected
                          and adaptation in r["adaptations"]
                          and not _would_exceed_family_limit(r["families"], family_counts, family_limit)), None)
        if candidate is not None:
            _add_record(candidate, selected, family_counts)
            unsatisfied_groups = [group for group in unsatisfied_groups if not _covers(candidate, group)]
    while unsatisfied_groups:
        if len(selected) >= cap:
            break
        eligible = [
            record
            for record in records
            if record not in selected
            and any(_covers(record, group) for group in unsatisfied_groups)
            and not _would_exceed_family_limit(
                record["families"], family_counts, family_limit
            )
        ]
        if not eligible:
            break
        record = max(
            eligible,
            key=lambda item: sum(
                _covers(item, group) for group in unsatisfied_groups
            ),
        )
        _add_record(record, selected, family_counts)
        unsatisfied_groups = [
            group for group in unsatisfied_groups if not _covers(record, group)
        ]

    # Preserve Stage 1 ranking as the authority. Support/accessory status is only
    # a drop preference when the session actually has to shrink; it is never a
    # global pre-selection ranking penalty.
    fill_records = records
    if len(records) > cap:
        fill_records = sorted(
            records,
            key=lambda item: (
                1 if item["support_only"] else 0,
                item["sort_key"][0],
                item["sort_key"][1],
            ),
        )

    for record in fill_records:
        if len(selected) >= cap:
            break
        if record in selected:
            continue
        if _would_exceed_family_limit(record["families"], family_counts, family_limit):
            dropped[record["name"]] = "redundant_major_family"
            continue
        _add_record(record, selected, family_counts)

    selected_ids = {id(item) for item in selected}
    for record in records:
        if id(record) in selected_ids:
            continue
        dropped.setdefault(record["name"], "role_or_readiness_cap")

    selected.sort(key=lambda item: item["sort_key"])
    return selected, dropped


_PRESSURE_SOURCE_REASONS = (
    ("fatigue_pressure", "high_fatigue"),
    ("cut_pressure", "weight_cut_pressure"),
)


def _trunk_support_outcome(
    records: list[dict[str, Any]],
    available: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    *,
    role_key: str,
    pressure_state: dict[str, Any],
    preserve_adaptation: str = "",
) -> dict[str, Any]:
    """Whether this session carries the selected trunk weakness, and if not why.

    Readiness pressure is cited only when it is causal: the same selector, run
    on the same candidates at pressure 0 and the role's base cap, would have
    kept trunk-targeted work. A gap with no such cause stays uncited, so goal
    preservation reports it rather than excusing it.
    """
    if any(record.get("trunk_targeted") for record in selected):
        return {"delivered": True}
    if not any(record.get("trunk_targeted") for record in records):
        return {"delivered": False, "skip_reason": "no_trunk_candidate_in_session_pool"}
    if not any(record.get("trunk_targeted") for record in available):
        return {"delivered": False, "skip_reason": "adjacent_strength_recovery"}
    sources = [
        reason for key, reason in _PRESSURE_SOURCE_REASONS if int(pressure_state.get(key) or 0) > 0
    ]
    base_cap, _ = _effective_role_cap(role_key, 0)
    unpressured, _ = _select_bounded_records(
        available, role_key=role_key, cap=base_cap, pressure=0, preserve_trunk_support=True,
        preserve_adaptation=preserve_adaptation,
    )
    if sources and any(record.get("trunk_targeted") for record in unpressured):
        return {"delivered": False, "skip_reason": "readiness_capacity", "pressure_sources": sources}
    return {"delivered": False, "skip_reason": "session_capacity"}


def _pressure_context_from_map(weekly_role_map: dict[str, Any]) -> dict[str, Any] | None:
    state = weekly_role_map.get("strength_composition_context")
    if not isinstance(state, dict):
        return None
    try:
        pressure = int(state.get("pressure"))
    except (TypeError, ValueError):
        return None
    if pressure not in {0, 1, 2, 3}:
        return None
    return dict(state)


def _primary_goal_adaptation(athlete_model: dict[str, Any] | None) -> str:
    """The trained adaptation the athlete's primary goal names, if composition
    can deliver it from strength members (strength, power or speed)."""
    if not isinstance(athlete_model, dict):
        return ""
    from .priority_profile import build_priority_profile
    from .tag_maps import priority_adaptation

    adaptation = priority_adaptation(build_priority_profile(athlete_model).primary_goal)
    return adaptation if adaptation in {"strength", "power", "speed"} else ""


def _trunk_strength_selected(athlete_model: dict[str, Any] | None) -> bool:
    model = athlete_model if isinstance(athlete_model, dict) else {}
    values = [
        *(model.get("key_goals") or []),
        *(model.get("goals") or []),
        *(model.get("weaknesses") or []),
        *(model.get("weak_areas") or []),
    ]
    return any(
        re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
        == "trunk_strength"
        for value in values
    )


def _low_load_trunk_support_records(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for record in _candidate_records(slots):
        if (
            record.get("support_only")
            and record.get("core_balance_support")
            and record.get("trunk_targeted")
            and not record.get("loaded_pattern")
            and not record.get("force_isometric")
            and not record.get("power_pattern")
            and record.get("quality_class") in {"support_accessory", "support_isometric"}
        ):
            records.append(record)
    return records


_EMBEDDED_TRUNK_HOLD_PRESCRIPTION = "1-2 controlled sets x 15-20 sec hold; stop before fatigue"
_EMBEDDED_TRUNK_REP_PRESCRIPTION = (
    "1-2 controlled sets x 3-5 slow reps (each side if one-sided); stop before fatigue"
)
_EMBEDDED_TRUNK_CARRY_PRESCRIPTION = (
    "1-2 controlled sets x 15-20 m carry (each side if one-sided); light load, stop before fatigue"
)
_HOLD_NAME_RE = re.compile(r"\b(?:hold|holds|plank|isometric|iso)\b", re.IGNORECASE)
_CARRY_RE = re.compile(r"\b(?:carry|carries|walk)\b", re.IGNORECASE)


def _embedded_trunk_support_prescription(record: dict[str, Any]) -> str:
    """A complete dose for an embedded trunk-support drill.

    A set count alone ("1-2 controlled sets") leaves the athlete with no hold
    time or rep count, so the card renders a hold with nothing to hold for.
    Holds get a hold time, carries a short distance, everything else a small
    rep count: "3-5 slow reps" of a carry is not a dose.
    """
    slot = record.get("slot") if isinstance(record.get("slot"), dict) else {}
    selected = slot.get("selected") if isinstance(slot.get("selected"), dict) else {}
    movement = str(selected.get("movement") or "").strip().lower()
    patterns = [str(value).strip().lower() for value in selected.get("movement_patterns") or []]
    if movement == "carry" or "carry" in patterns[:1] or _CARRY_RE.search(str(record.get("name") or "")):
        return _EMBEDDED_TRUNK_CARRY_PRESCRIPTION
    is_hold = (
        record.get("quality_class") == "support_isometric"
        or movement == "isometric"
        or bool(_HOLD_NAME_RE.search(str(record.get("name") or "")))
    )
    return _EMBEDDED_TRUNK_HOLD_PRESCRIPTION if is_hold else _EMBEDDED_TRUNK_REP_PRESCRIPTION


def _role_days_until_fight(role: dict[str, Any]) -> int | None:
    # The canonical resolver reads final placement (scheduled label, then
    # scheduled_d_day) before any planning-time offset, so a relocated role is
    # judged on the day it is actually trained.
    value = role_d_day({}, role)
    if value is None:
        value = _int_or_none(role.get("scheduled_countdown_offset"))
    return max(0, value) if value is not None else None


def _role_pressure_state(
    context: dict[str, Any], *, role: dict[str, Any], fatigue_applied: bool
) -> dict[str, Any]:
    fatigue = str(context.get("fatigue") or "low").strip().lower() if fatigue_applied else "low"
    if fatigue not in _FATIGUE_PRESSURE:
        fatigue = "low"

    active_cut = bool(context.get("active_weight_cut"))
    cut_bucket = str(context.get("cut_severity_bucket") or "none").strip().lower()
    role_d_day = _role_days_until_fight(role)
    cut_pct = _float_or_none(context.get("weight_cut_pct"))
    if active_cut and cut_pct is not None and cut_pct > 0.0 and role_d_day is not None:
        # Per-role strain recalculation: a role nearer the fight carries more
        # cut strain, which trims its dose. It never removes the role.
        cut_bucket = cut_health_bucket(compute_cut_severity_score(cut_pct, role_d_day))
    elif not active_cut:
        cut_bucket = "none"

    state = _pressure_state_from_components(
        fatigue=fatigue,
        cut_bucket=cut_bucket,
        injury_restricted=bool(context.get("injury_restricted")),
    )
    state["fatigue_applied"] = fatigue_applied
    state["role_days_until_fight"] = role_d_day
    return state


# --- Realised cross-day load -------------------------------------------------
#
# A role's planned load class is a promise made before any exercise exists: an
# ``alactic_speed_day`` is stamped NEURAL_MICRODOSE from its role key alone. Once
# composition has chosen exercises and doses, the session's real cross-day cost
# is knowable, and it may be nothing like the promise. These helpers translate a
# composed session into the canonical ``SessionStress`` vocabulary owned by
# ``combat_load_policy`` so the calendar can be re-judged on what was actually
# composed.
#
# Nothing here decides legality. It only measures, in the shared vocabulary.

# Mechanical tags are an authored bank vocabulary (``mech_*``), so region mapping
# is a prefix rule over that vocabulary rather than a list of exercise names.
_REGION_TAG_PREFIXES = (
    ("mech_lower", BodyRegion.LOWER),
    ("mech_upper", BodyRegion.UPPER),
    ("mech_shoulder", BodyRegion.UPPER),
    ("mech_grip", BodyRegion.UPPER),
    ("mech_trunk", BodyRegion.TRUNK),
    ("mech_hip", BodyRegion.LOWER),
    ("mech_plantarflexion", BodyRegion.LOWER),
)
# Tags that describe how the tissue is loaded rather than where. Impact and
# ballistic work is the mechanical cost a following day has to absorb.
_HIGH_MECHANICAL_TAGS = frozenset(
    {
        "mech_ballistic",
        "mech_landing_impact",
        "mech_reactive",
        "mech_reactive_rebound",
        "mech_max_velocity",
        "mech_deceleration",
        "mech_change_of_direction",
        "mech_acceleration",
        "mech_axial_heavy",
    }
)
_HIGH_NEURAL_TAGS = frozenset({"mech_cns_high"})
_HIGH_SYSTEMIC_TAGS = frozenset({"mech_systemic_fatigue"})

# A microdose is a handful of quality contacts, not a training block. Above this
# many total reps the session is no longer expressing a microdose promise,
# whatever its role key says. Chosen to sit well above a genuine 2x3 primer and
# well below the ~80-rep session that motivated this rule.
_MICRODOSE_REP_CEILING = 30


def _assignment_mechanical_tags(assignment: dict[str, Any]) -> set[str]:
    metadata = assignment.get("selection_metadata")
    nested = metadata.get("mechanical_risk_tags", []) if isinstance(metadata, dict) else []
    return {
        str(value).strip().lower()
        for value in [*(assignment.get("mechanical_risk_tags") or []), *nested]
        if str(value).strip().lower().startswith("mech_")
    }


# A count followed by a time unit (optionally as a range) is a duration.
_DURATION_UNIT_TAIL = re.compile(
    r"\s*(?:[-\u2013]\s*\d+\s*)?(?:s|secs?|seconds?|mins?|minutes?)\b", re.I
)
_DURATION_DOSE = re.compile(r"\d+\s*(?:s|secs?|seconds?|mins?|minutes?)\b", re.I)


def prescription_dose_known(text: str) -> bool:
    """Whether a rendered dose states a measurable amount of work.

    Reps (``4x5``) or a work duration (``2x5s``, ``20 min``). A dose stating
    neither cannot be measured, and an unmeasured dose is never evidence that
    further work fits.
    """
    return _prescription_total_reps(text) > 0 or bool(_DURATION_DOSE.search(str(text or "")))


def _prescription_total_reps(text: str) -> float:
    """Total reps a rendered prescription commits to, ``0.0`` when it states none.

    Handles the authored forms that actually appear: ``4x5``, ``4 x 5/side``,
    ``3 sets x 8 reps``. ``/side`` doubles the count because both limbs pay. A
    duration-only or unparseable dose contributes no rep count; the tag-based
    signals above still apply to it.
    """
    total = 0.0
    for match in re.finditer(
        r"(?<![\d.])(\d+)\s*(?:sets?\s*)?[x\u00d7]\s*(\d+)", str(text or ""), re.I
    ):
        # ``2x5s`` and ``4-6 x 6-10 sec`` are work durations, not repetitions;
        # counting them as reps scored a 10-second primer as a 10-rep set.
        if _DURATION_UNIT_TAIL.match(text[match.end():]):
            continue
        reps = float(match.group(1)) * float(match.group(2))
        tail = text[match.end():match.end() + 12].lower()
        if "side" in tail or "/s" in tail or "each" in tail:
            reps *= 2
        total += reps
    return total


def _authoritative_dose(
    assignment: dict[str, Any], resolved_strength: dict[tuple[str, str], str]
) -> str:
    """The dose this exercise will actually be performed at.

    Strength membership and strength *dose* are separate authorities: a
    ``strength_slots`` assignment carries only the raw bank ``base_prescription``
    until ``prescription_resolver`` resolves its scheduled-day dose into the
    role's ``effective_strength_prescriptions``. Measuring realised load off the
    bank dose would score a capped 2x3 primer as the uncapped 5x5 the bank
    authored, so the resolver's answer wins wherever it exists.
    """
    key = (str(assignment.get("slot_id") or ""), str(assignment.get("name") or ""))
    if resolved := resolved_strength.get(key):
        return resolved
    return str(
        assignment.get("effective_prescription")
        or assignment.get("base_prescription")
        or ""
    )


def _resolved_strength_doses(role: dict[str, Any]) -> dict[tuple[str, str], str]:
    resolved: dict[tuple[str, str], str] = {}
    for item in role.get("effective_strength_prescriptions") or []:
        if not isinstance(item, dict):
            continue
        slot_id = str(item.get("slot_id") or "").strip()
        name = str(item.get("name") or "").strip()
        dose = str(item.get("effective_prescription") or "").strip()
        if slot_id and name and dose:
            resolved[(slot_id, name)] = dose
    return resolved


def realised_role_stress(role: dict[str, Any]) -> SessionStress | None:
    """Measure a composed *role*'s cross-day cost in the canonical vocabulary.

    Role-level rather than assignment-level because the authoritative strength
    dose lives on the role (``effective_strength_prescriptions``), not on the
    assignment it applies to.
    """
    if not isinstance(role, dict):
        return None
    stress = realised_session_stress(
        role.get("selected_exercise_assignments"),
        resolved_strength=_resolved_strength_doses(role),
        resolved_strength_effort={
            (str(p.get("slot_id") or ""), str(p.get("name") or "")): p.get("effective_rpe_cap")
            for p in role.get("effective_strength_prescriptions") or []
            if p.get("dose_authority") == "scheduled_countdown_overlay"
        },
    )
    from .session_workload import role_has_substantial_endurance
    if stress is None and role_has_substantial_endurance(role):
        return SessionStress(systemic=StressLevel.LOW)
    return stress


def realised_session_stress(
    assignments: list[dict[str, Any]] | None,
    *,
    resolved_strength: dict[tuple[str, str], str] | None = None,
    resolved_strength_effort: dict[tuple[str, str], Any] | None = None,
) -> SessionStress | None:
    """Measure a composed session's cross-day cost, or ``None`` if unmeasurable.

    ``None`` means "no evidence" and leaves the role's planned vector in place;
    it never means "free".
    """
    assignments = [item for item in assignments or [] if isinstance(item, dict)]
    resolved_strength = resolved_strength or {}
    resolved_strength_effort = resolved_strength_effort or {}
    if not assignments:
        return None

    tags: set[str] = set()
    regions: set[BodyRegion] = set()
    total_reps = 0.0
    mechanical_reps = 0.0
    systemic = StressLevel.NONE
    for assignment in assignments:
        # An embedded support item is explicitly dose-capped to stay below the
        # session's own cost and must not inflate it.
        if assignment.get("embedded_support"):
            continue
        item_tags = _assignment_mechanical_tags(assignment)
        dose = _authoritative_dose(assignment, resolved_strength)
        item_reps = _prescription_total_reps(dose)
        if item_tags & (_HIGH_MECHANICAL_TAGS | _HIGH_NEURAL_TAGS):
            mechanical_reps += item_reps
        tags |= item_tags
        if item_tags & _HIGH_SYSTEMIC_TAGS:
            # Conditioning carries authored effort alongside its resolved dose.
            # A fatigue tag alone does not turn easy aerobic work into a hard
            # metabolic session. Unknown effort retains the conservative cost.
            rpe = _float_or_none(assignment.get("rpe"))
            lactate = str(assignment.get("lactate_load") or "").strip().lower()
            intensity = str(assignment.get("intensity") or "").strip().lower()
            easy_effort = ((rpe is not None and rpe <= 5)
                           or (intensity in {"zone 2", "zone2", "easy", "recovery"}
                               and (rpe is None or rpe <= 6)))
            easy = (assignment.get("slot_group") == "conditioning_slots"
                    and easy_effort
                    and lactate in {"none", "low"})
            effort = re.search(r"\bRPE\s*[:=]?\s*(\d+(?:\.\d+)?)(?:\s*[-–]\s*(\d+(?:\.\d+)?))?", dose, re.I)
            effort_ceiling = _float_or_none(resolved_strength_effort.get((str(assignment.get("slot_id") or ""), str(assignment.get("name") or ""))))
            if effort is not None:
                effort_ceiling = float(effort[2] or effort[1])
            managed_strength = (assignment.get("slot_group") == "strength_slots"
                                and effort_ceiling is not None and effort_ceiling <= 7
                                and item_reps <= _MICRODOSE_REP_CEILING
                                and dose != str(assignment.get("base_prescription") or "")
                                and bool(resolved_strength))
            systemic = max(systemic, StressLevel.LOW if easy else
                           StressLevel.MODERATE if managed_strength else StressLevel.HIGH)
        for prefix, region in _REGION_TAG_PREFIXES:
            if any(tag.startswith(prefix) for tag in item_tags):
                regions.add(region)
        total_reps += item_reps

    if not tags and not total_reps:
        return None

    high_mechanical = bool(tags & _HIGH_MECHANICAL_TAGS)
    if high_mechanical and mechanical_reps > _MICRODOSE_REP_CEILING:
        neural_mechanical = StressLevel.HIGH
    elif high_mechanical or tags & _HIGH_NEURAL_TAGS or total_reps > _MICRODOSE_REP_CEILING:
        neural_mechanical = StressLevel.MODERATE
    else:
        neural_mechanical = StressLevel.LOW

    return SessionStress(
        systemic=systemic, neural_mechanical=neural_mechanical, regions=frozenset(regions)
    )


def _stress_stamp(stress: SessionStress) -> dict[str, Any]:
    return {
        "systemic": int(stress.systemic),
        "neural_mechanical": int(stress.neural_mechanical),
        "regions": sorted(region.value for region in stress.regions),
        "authority": "realised_session_composition",
    }


def _substantial_mechanical_tags(records: list[dict[str, Any]]) -> set[str]:
    if not records:
        return set()
    return set().union(
        *[
            record["mechanical_risk_tags"]
            for record in records
            if record["material_movement_cost"]
        ]
    )


def _all_mechanical_tags(records: list[dict[str, Any]]) -> set[str]:
    return (
        set().union(*(record["mechanical_risk_tags"] for record in records))
        if records
        else set()
    )


def _assign_selected_records(
    entry: dict[str, Any], selected_records: list[dict[str, Any]]
) -> None:
    assignments = []
    for record in selected_records:
        assignment = (conditioning_assignment_from_slot(entry["phase"], record["slot"], record["slot"]["selected"])
                      if record.get("slot_group") == "conditioning_slots"
                      else assignment_from_slot(entry["phase"], "strength_slots", record["slot"]))
        if assignment:
            assignments.append(assignment)
    entry["role"]["selected_exercise_assignments"] = assignments
    policy = entry["role"]["strength_composition_policy"]
    policy["selected_count"] = len(assignments)
    policy["selected_names"] = [item["name"] for item in assignments]
    entry["selected_records"] = selected_records


def _apply_adjacent_strength_recovery(
    weekly_role_map: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    allow_relocation: bool = True,
) -> None:
    scheduled = [
        entry
        for entry in entries
        if role_d_day(entry["week"], entry["role"]) is not None
        and entry["role"] in (entry["week"].get("session_roles") or [])
    ]
    scheduled.sort(
        key=lambda entry: role_d_day(entry["week"], entry["role"]),
        reverse=True,
    )

    for index, current in enumerate(scheduled):
        current_d_day = role_d_day(current["week"], current["role"])
        if current_d_day is None or current.get("context_only"):
            continue
        previous = [
            entry
            for entry in scheduled[:index]
            if role_d_day(entry["week"], entry["role"]) == current_d_day + 1
            and entry["role"] in (entry["week"].get("session_roles") or [])
        ]
        if not previous:
            continue

        previous_records = [
            record for entry in previous for record in entry["selected_records"]
        ]
        previous_substantial_tags = _substantial_mechanical_tags(previous_records)
        shared_tags = _all_mechanical_tags(previous_records) & _all_mechanical_tags(
            current["selected_records"]
        )
        conflicting_tags = previous_substantial_tags & _substantial_mechanical_tags(
            current["selected_records"]
        )
        evidence = {
            "consecutive": True,
            "previous_d_day": current_d_day + 1,
            "current_d_day": current_d_day,
            "shared_mechanical_tags": sorted(shared_tags),
            "substantial_overlap_tags": sorted(conflicting_tags),
            "previous_material_items": [
                record["name"]
                for record in previous_records
                if record["material_movement_cost"]
            ],
            "current_material_items": [
                record["name"]
                for record in current["selected_records"]
                if record["material_movement_cost"]
            ],
            "current_verified_low_cost_items": [
                record["name"]
                for record in current["selected_records"]
                if record["verified_low_recovery_cost"]
            ],
        }

        if not conflicting_tags:
            evidence["decision"] = "compatible"
            current["role"]["strength_composition_policy"][
                "adjacent_strength_recovery"
            ] = evidence
            continue

        safe_records = [
            record
            for record in current["records"]
            if not record["material_movement_cost"]
            or not (record["mechanical_risk_tags"] & previous_substantial_tags)
        ]
        selected_records, dropped = _select_bounded_records(
            safe_records,
            role_key=current["role_key"],
            cap=current["effective_cap"],
            pressure=current["pressure"],
            preserve_trunk_support=current["preserve_trunk_support"],
            preserve_adaptation=current.get("preserve_adaptation", ""),
            preserve_weakness=current.get("preserve_weakness", ""),
            preserve_strength=current.get("preserve_strength", False),
        )
        if selected_records:
            _assign_selected_records(current, selected_records)
            policy = current["role"]["strength_composition_policy"]
            if current["preserve_trunk_support"]:
                policy["trunk_support"] = _trunk_support_outcome(
                    current["records"], safe_records, selected_records,
                    role_key=current["role_key"], pressure_state=policy,
                    preserve_adaptation=current.get("preserve_adaptation", ""),
                )
            existing_dropped = {
                str(item.get("name") or ""): str(item.get("reason") or "")
                for item in policy.get("dropped") or []
                if isinstance(item, dict) and str(item.get("name") or "")
            }
            existing_dropped.update(dropped)
            for record in current["records"]:
                if record not in safe_records:
                    existing_dropped[record["name"]] = _ADJACENT_STRENGTH_RECOVERY_REASON
            policy["dropped"] = [
                {"name": name, "reason": reason}
                for name, reason in existing_dropped.items()
            ]
            evidence["decision"] = "selection_modified"
            policy["adjacent_strength_recovery"] = evidence
            continue

        if not allow_relocation:
            # The caller has already ruled on placement; composition only
            # fills the day the role is on. The conflict stays on record.
            evidence["decision"] = "relocation_not_permitted"
            current["role"]["strength_composition_policy"]["adjacent_strength_recovery"] = evidence
            continue

        current_substantial_tags = _substantial_mechanical_tags(current["selected_records"])
        excluded_d_days: set[int] = set()
        for other in scheduled:
            if other is current:
                continue
            other_d_day = role_d_day(other["week"], other["role"])
            if other_d_day is None:
                continue
            if current_substantial_tags & _substantial_mechanical_tags(other["selected_records"]):
                excluded_d_days.update({other_d_day - 1, other_d_day, other_d_day + 1})

        action = relocate_or_suppress_role_for_recovery(
            weekly_role_map,
            current["role"],
            excluded_d_days=excluded_d_days,
            reason_code=_ADJACENT_STRENGTH_RECOVERY_REASON,
        )
        evidence["decision"] = (
            str(action.get("action")) if isinstance(action, dict) else "unchanged"
        )
        if isinstance(action, dict) and action.get("to_d_day") is not None:
            evidence["relocated_to_d_day"] = action["to_d_day"]
        current["role"]["strength_composition_policy"][
            "adjacent_strength_recovery"
        ] = evidence


def compose_normal_strength_assignments(
    *,
    weekly_role_map: dict[str, Any],
    candidate_pools: dict[str, Any],
    only_roles: set[int] | None = None,
    allow_relocation: bool = True,
) -> dict[str, Any]:
    """Carry bounded Stage 1 strength composition onto normal planner roles.

    Closed membership remains Stage 1 authority. This function never chooses an
    alternate and never invents an exercise. It only prunes already-selected
    slots using role ceilings, athlete recovery pressure, and existing movement
    tags. Late-fight-owned roles are excluded and keep their dedicated selector.

    Current fatigue is applied only to the first camp week containing a normal
    strength role handled here. Persistent injury state remains active, while
    weight-cut severity is recalculated from each role's D-day when the cut
    percentage is available.

    ``only_roles`` (addressed by ``id()``) recomposes just those roles, for a
    caller that moved them after composition: membership is a function of the
    scheduled day (its phase, per-day cut strain and neighbours), so a moved
    role's membership is stale. Every other strength role takes part only as
    context -- its current membership informs the adjacency check -- and is
    never rewritten. ``allow_relocation=False`` is for a caller that owns
    placement: the adjacent-recovery mover is not run, so membership is always
    composed for the day the role is on.
    """
    athlete_model = get_planner_athlete_model()
    pressure_context = (
        _composition_context_from_model(athlete_model)
        if athlete_model is not None
        else _pressure_context_from_map(weekly_role_map)
    )
    if pressure_context is None:
        pressure_context = _composition_context_from_model(None)
    trunk_strength_selected = _trunk_strength_selected(athlete_model)
    primary_adaptation = _primary_goal_adaptation(athlete_model)
    from .priority_profile import build_priority_profile, selected_priority_targets
    # Other weakness families retain their existing composition owners (for
    # example trunk support); the selected moving-footwork bank is complementary.
    primary_weakness = "footwork" if build_priority_profile(athlete_model or {}).primary_weak_area == "footwork" else ""
    selected_adaptations = {p.target for p in selected_priority_targets(athlete_model or {})}
    strength_selected = "strength" in selected_adaptations

    weekly_role_map["strength_composition_context"] = dict(pressure_context)
    composed_roles: list[dict[str, Any]] = []

    first_strength_week_position = next(
        (
            position
            for position, week in enumerate(weekly_role_map.get("weeks", []) or [])
            if isinstance(week, dict)
            and any(
                isinstance(role, dict)
                and not role.get("late_fight_tail_owned")
                and (
                    str(role.get("category") or "").lower() == "strength"
                    or str(role.get("preferred_pool") or "").lower() == "strength_slots"
                )
                for role in (week.get("session_roles", []) or [])
            )
        ),
        None,
    )

    for week_position, week in enumerate(weekly_role_map.get("weeks", []) or []):
        if not isinstance(week, dict):
            continue
        week_phase = str(week.get("phase") or "").strip().upper()
        strength_index = 0
        for role in week.get("session_roles", []) or []:
            if not isinstance(role, dict):
                continue
            is_strength = (
                str(role.get("category") or "").lower() == "strength"
                or str(role.get("preferred_pool") or "").lower() == "strength_slots"
            )
            if not is_strength:
                continue
            strength_index += 1
            if role.get("late_fight_tail_owned"):
                continue

            # Candidate authority follows the phase that owns this role's D-day,
            # not the weekly container's label, so a week straddling a phase
            # boundary cannot hand a GPP-only exercise to an SPP day.
            #
            # Fails closed, matching ``phase_scoped_candidate_pools``: once the
            # phase resolves, a missing pool for it yields no candidates rather
            # than substituting another phase's — substituting is precisely how
            # a GPP-only exercise reached an SPP day. The week label is the
            # fallback only when the phase does not resolve at all (an undated
            # role, or a D-day outside the camp's allocation), which is also the
            # single-phase camp's path.
            phase = _scheduled_phase_for_role(week, role)
            if not phase:
                phase = week_phase
            pool = candidate_pools.get(phase) if isinstance(candidate_pools, dict) else None
            slots = pool.get("strength_slots", []) if isinstance(pool, dict) else []

            pressure_state = _role_pressure_state(
                pressure_context,
                role=role,
                fatigue_applied=week_position == first_strength_week_position,
            )
            pressure = int(pressure_state["pressure"])
            exercise_count_pressure = int(pressure_state["exercise_count_pressure"])

            session_index = role.get("strength_session_index") or strength_index
            owned_slots = [
                slot
                for slot in slots
                if isinstance(slot, dict) and (slot.get("session_index") or 1) == session_index
            ]
            records = _candidate_records(owned_slots)
            # Selection distributes slots before calendar suppression. The
            # surviving session can use eligible complementary pool members;
            # an alternate remains one substitution for its original slot.
            active_indices = {r.get("strength_session_index") or 1 for r in week.get("session_roles") or []
                              if r is not role and r.get("category") == "strength" and role_d_day(week, r) is not None
                              and not r.get("late_fight_tail_owned")}
            available_slots = [slot for slot in slots if slot in owned_slots
                               or (slot.get("session_index") or 1) not in active_indices]
            priorities = ({primary_adaptation, primary_weakness}
                          | (selected_adaptations & {"strength", "trunk_strength"})) - {""}
            records.extend(r for r in _candidate_records(available_slots)
                           if priorities.intersection(r["adaptations"]) and r["slot"] not in owned_slots)
            if primary_weakness == "footwork":
                for slot in (pool or {}).get("conditioning_slots") or []:
                    if slot.get("role") != "technical_footwork":
                        continue
                    # The conditioning selector already chose and filtered this
                    # physical movement drill. Use its own dose within this
                    # session's cap rather than substituting stance/prep cues.
                    for record in _candidate_records([slot]):
                        record.update(adaptations=frozenset({"footwork"}), slot_group="conditioning_slots")
                        records.append(record)
            if primary_adaptation and not any(primary_adaptation in r["adaptations"] for r in records):
                promoted = False
                for slot in available_slots:
                    if not slot.get("slot_id"):
                        continue  # An alternate needs its original slot's stable identity.
                    for index, alternate in enumerate(slot.get("alternates") or []):
                        alternate_slot = {**slot, "slot_id": f"{slot['slot_id']}:alternate:{index}",
                                          "selected": alternate, "alternates": []}
                        matches = [r for r in _candidate_records([alternate_slot])
                                   if primary_adaptation in r["adaptations"]]
                        if matches:
                            # One substitute for this slot, never the selected
                            # exercise plus its backups as extra circuit members.
                            records = [r for r in records if r["slot"].get("slot_id") != slot.get("slot_id")]
                            records.extend(matches)
                            promoted = True
                            break
                    if promoted:
                        break
            role_key = str(role.get("role_key") or "").strip()
            base_cap, effective_cap = _effective_role_cap(role_key, exercise_count_pressure)
            prior_policy = role.get("strength_composition_policy") or {}
            priced_names = {p.get("name") for p in [*(role.get("effective_strength_prescriptions") or []),
                                                    *(role.get("selected_exercise_assignments") or [])]
                            if p.get("effective_prescription")}
            members = role.get("selected_exercise_assignments") or []
            closed_current = (only_roles is None and bool(members)
                and prior_policy.get("composed_for_d_day") == role_d_day(week, role)
                and all(m.get("name") in priced_names and m.get("source_phase") == phase for m in members))
            if closed_current or (only_roles is not None and id(role) not in only_roles):
                # Already composed and priced for this calendar date is closed
                # work, not another chance to pick a different session. Movers
                # and changed-neighbour callbacks explicitly target only_roles;
                # stale dates/phases still recompose through the same owner.
                held = {
                    str(item.get("name") or "")
                    for item in role.get("selected_exercise_assignments") or []
                    if isinstance(item, dict)
                }
                composed_roles.append(
                    {
                        "week": week,
                        "role": role,
                        "records": records,
                        "selected_records": [r for r in records if r["name"] in held],
                        "context_only": True,
                    }
                )
                continue
            prior_policy = role.get("strength_composition_policy")
            prior_standalone_minimum = (
                prior_policy.get("standalone_minimum")
                if isinstance(prior_policy, dict)
                else None
            )
            microdose_only = role.get("pre_hard_contact_microdose_only") is True
            selected_records, dropped = _select_bounded_records(
                records,
                role_key=role_key,
                cap=effective_cap,
                pressure=exercise_count_pressure,
                preserve_trunk_support=trunk_strength_selected,
                preserve_adaptation=primary_adaptation,
                preserve_weakness=primary_weakness,
                preserve_strength=strength_selected,
            )

            assignments: list[dict[str, Any]] = []
            for record in selected_records:
                assignment = (conditioning_assignment_from_slot(phase, record["slot"], record["slot"]["selected"])
                              if record.get("slot_group") == "conditioning_slots"
                              else assignment_from_slot(phase, "strength_slots", record["slot"]))
                if assignment:
                    assignments.append(assignment)
            role["selected_exercise_assignments"] = assignments
            role["strength_composition_policy"] = {
                **pressure_state,
                "role_key": role_key,
                "base_exercise_cap": base_cap,
                "effective_exercise_cap": effective_cap,
                "pre_hard_contact_microdose_only": microdose_only,
                "standalone_minimum_applied": False,
                # The day this membership was chosen for. Any later move must
                # recompose the role; the final calendar is checked against it.
                "composed_for_d_day": role_d_day(week, role),
                "major_family_limit": 2 if exercise_count_pressure == 0 else 1,
                "selected_count": len(assignments),
                "selected_names": [item["name"] for item in assignments],
                "dropped": [
                    {"name": name, "reason": reason}
                    for name, reason in dropped.items()
                ],
            }
            if trunk_strength_selected:
                role["strength_composition_policy"]["trunk_support"] = _trunk_support_outcome(
                    records, records, selected_records, role_key=role_key, pressure_state=pressure_state,
                    preserve_adaptation=primary_adaptation,
                )
            composed_roles.append(
                {
                    "week": week,
                    "role": role,
                    "phase": phase,
                    "records": records,
                    "selected_records": selected_records,
                    "role_key": role_key,
                    "effective_cap": effective_cap,
                    "pressure": exercise_count_pressure,
                    "preserve_trunk_support": trunk_strength_selected,
                    "preserve_adaptation": primary_adaptation,
                    "preserve_weakness": primary_weakness,
                    "preserve_strength": strength_selected,
                    "week_ordinal": week_position + 1,
                    "standalone_eligible": (
                        not microdose_only
                        and pressure <= _STANDALONE_STRENGTH_MAX_PRESSURE
                        and phase in {"GPP", "SPP"}
                        and role_key in _STANDALONE_STRENGTH_MINIMUM_ROLE_KEYS
                    ),
                    "prior_standalone_minimum": prior_standalone_minimum,
                }
            )
    _apply_adjacent_strength_recovery(weekly_role_map, composed_roles, allow_relocation=allow_relocation)
    _propose_standalone_strength_minimum(weekly_role_map, composed_roles)
    return weekly_role_map


def _normal_strength_roles(weekly_role_map: dict[str, Any]):
    for week in weekly_role_map.get("weeks", []) or []:
        if not isinstance(week, dict):
            continue
        for role in week.get("session_roles", []) or []:
            if (
                isinstance(role, dict)
                and not role.get("late_fight_tail_owned")
                and str(role.get("category") or "").lower() == "strength"
            ):
                yield week, role


def stale_strength_roles(weekly_role_map: dict[str, Any]) -> set[int]:
    """Strength roles (by ``id()``) whose membership was composed for another day."""
    return {
        id(role)
        for week, role in _normal_strength_roles(weekly_role_map)
        if isinstance(role.get("strength_composition_policy"), dict)
        and role["strength_composition_policy"].get("composed_for_d_day") != role_d_day(week, role)
    }


def reconcile_relocated_strength(
    weekly_role_map: dict[str, Any],
    *,
    candidate_pools: dict[str, Any],
    replace_callback: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    """Recompose every strength role that moved after its membership was chosen.

    Any mover can strand membership: composition's own adjacent-recovery
    mover, the calendar governor, the countdown morph's re-run. A role is stale
    when the day it was composed for is not the day it is on. Stale roles are
    recomposed by the same owner; if that relocates anything, placement is
    re-ruled once through ``replace_callback`` (morph + governor). Any role
    still stale after that is composed for the day it is on with relocation
    disabled -- placement has been ruled on twice -- so the pass is bounded and
    always ends with membership matching the calendar.
    """
    stale = stale_strength_roles(weekly_role_map)
    if not stale:
        return weekly_role_map
    placed = {id(role): role_d_day(week, role) for week, role in _normal_strength_roles(weekly_role_map)}
    compose_normal_strength_assignments(
        weekly_role_map=weekly_role_map, candidate_pools=candidate_pools, only_roles=stale,
    )
    if any(role_d_day(week, role) != placed.get(id(role)) for week, role in _normal_strength_roles(weekly_role_map)):
        replace_callback(weekly_role_map)
    stale = stale_strength_roles(weekly_role_map)
    if stale:
        compose_normal_strength_assignments(
            weekly_role_map=weekly_role_map,
            candidate_pools=candidate_pools,
            only_roles=stale,
            allow_relocation=False,
        )
    return weekly_role_map


def _propose_standalone_strength_minimum(
    weekly_role_map: dict[str, Any], entries: list[dict[str, Any]]
) -> None:
    """Record at most one extra compatible exercise per eligible standalone role.

    Runs after the adjacent-strength-recovery pass so it sees final composition
    placement. A new proposal never touches ``selected_exercise_assignments``:
    only ``apply_standalone_strength_minimum`` can confirm it. A decision already
    confirmed or rejected for the same exercise (a goal-repair trial recomposes
    the whole map) is carried forward rather than re-judged, so only an
    already-confirmed extra is re-added here.
    """
    for entry in entries:
        role = entry["role"]
        week = entry["week"]
        policy = role.get("strength_composition_policy")
        if entry.get("context_only") or not entry.get("standalone_eligible") or not isinstance(policy, dict):
            continue
        if role not in (week.get("session_roles") or []):
            continue
        if (policy.get("adjacent_strength_recovery") or {}).get("decision") not in {
            None,
            "compatible",
        }:
            continue
        selected = entry["selected_records"]
        if len(selected) >= _STANDALONE_STRENGTH_MINIMUM:
            continue
        if not _strength_role_is_standalone(
            weekly_role_map, week, entry["week_ordinal"], role
        ):
            continue

        d_day = role_d_day(week, role)
        neighbour_substantial_tags = _substantial_mechanical_tags(
            [
                record
                for other in entries
                if other is not entry
                and other["role"] in (other["week"].get("session_roles") or [])
                and role_d_day(other["week"], other["role"]) in {d_day - 1, d_day + 1}
                for record in other["selected_records"]
            ]
        )
        family_limit = 2 if entry["pressure"] == 0 else 1
        family_counts: dict[str, int] = {}
        for record in selected:
            for family in record["families"]:
                family_counts[family] = family_counts.get(family, 0) + 1
        extra = next(
            (
                record
                for record in entry["records"]
                if record not in selected
                and not _would_exceed_family_limit(
                    record["families"], family_counts, family_limit
                )
                and (
                    not record["material_movement_cost"]
                    or not (record["mechanical_risk_tags"] & neighbour_substantial_tags)
                )
            ),
            None,
        )
        if extra is None:
            continue
        assignment = assignment_from_slot(entry["phase"], "strength_slots", extra["slot"])
        if not assignment:
            continue

        prior = entry.get("prior_standalone_minimum")
        prior = prior if isinstance(prior, dict) else {}
        same_exercise = (
            prior.get("exercise") == assignment["name"]
            and prior.get("slot_id") == assignment.get("slot_id")
        )
        decision = {
            "status": prior.get("status") if same_exercise else "proposed",
            "exercise": assignment["name"],
            "slot_id": assignment.get("slot_id"),
            "source_phase": entry["phase"],
        }
        if same_exercise and prior.get("reason"):
            decision["reason"] = prior["reason"]
        policy["standalone_minimum"] = decision
        if decision["status"] == "applied":
            _assign_selected_records(entry, [*selected, extra])
            policy["standalone_minimum_applied"] = True


def _placement_signature(weekly_role_map: dict[str, Any]) -> tuple:
    weeks = []
    for week in weekly_role_map.get("weeks", []) or []:
        if not isinstance(week, dict):
            continue
        scheduled = sorted(
            tuple(
                str(role.get(key))
                for key in (
                    "role_key",
                    "category",
                    "strength_session_index",
                    "session_index",
                    "scheduled_countdown_label",
                    "scheduled_day_hint",
                )
            )
            for role in week.get("session_roles") or []
            if isinstance(role, dict)
        )
        suppressed = sorted(
            str(role.get("role_key"))
            for role in week.get("suppressed_roles") or []
            if isinstance(role, dict)
        )
        weeks.append((tuple(scheduled), tuple(suppressed)))
    return tuple(weeks)


def _strength_roles_by_position(
    weekly_role_map: dict[str, Any],
) -> dict[tuple[int, int], dict[str, Any]]:
    return {
        (week_index, role_index): role
        for week_index, week in enumerate(weekly_role_map.get("weeks", []) or [])
        if isinstance(week, dict)
        for role_index, role in enumerate(week.get("session_roles") or [])
        if isinstance(role, dict)
        and isinstance(role.get("strength_composition_policy"), dict)
    }


def apply_standalone_strength_minimum(
    weekly_role_map: dict[str, Any],
    *,
    candidate_pools: dict[str, Any],
    redose_callback: Callable[[dict[str, Any]], Any],
    recompose_conditioning_callback: Callable[[dict[str, Any], set[int]], Any] | None = None,
    recompose_strength_callback: Callable[[dict[str, Any], set[int]], Any] | None = None,
) -> dict[str, Any]:
    """Confirm or reject each proposed standalone-strength extra exercise.

    Must run after dose resolution and immediately before
    ``apply_realised_load_calendar_revalidation``. The existing revalidation is
    run on copies of the map, first without any extra exercise (the baseline)
    and then with each proposal added on top of those already accepted. A
    proposal is kept only when the revalidation outcome -- every role's
    placement, every relocation/suppression action, and every residual conflict
    -- is identical to the baseline, and every existing exercise keeps its
    resolved dose. Otherwise the session stays at its original exercise count,
    so this can never alter session placement.
    """
    from copy import deepcopy

    if not isinstance(weekly_role_map, dict):
        return weekly_role_map

    proposals: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for position, role in _strength_roles_by_position(weekly_role_map).items():
        decision = role["strength_composition_policy"].get("standalone_minimum")
        if not isinstance(decision, dict) or decision.get("status") != "proposed":
            continue
        phase = str(decision.get("source_phase") or "")
        pool = candidate_pools.get(phase) if isinstance(candidate_pools, dict) else None
        slot = next(
            (
                slot
                for slot in (pool or {}).get("strength_slots", []) or []
                if isinstance(slot, dict) and slot.get("slot_id") == decision.get("slot_id")
            ),
            None,
        )
        assignment = assignment_from_slot(phase, "strength_slots", slot) if slot else None
        if not assignment or assignment["name"] != decision.get("exercise"):
            decision.update(status="rejected", reason="candidate_unavailable")
            continue
        proposals.append((position, assignment))
    if not proposals:
        return weekly_role_map

    def _add(role_map: dict[str, Any], accepted: list) -> None:
        roles = _strength_roles_by_position(role_map)
        for position, assignment in accepted:
            role = roles[position]
            role["selected_exercise_assignments"] = [
                *(role.get("selected_exercise_assignments") or []),
                deepcopy(assignment),
            ]
            policy = role["strength_composition_policy"]
            policy["selected_count"] = len(role["selected_exercise_assignments"])
            policy["selected_names"] = [
                item["name"] for item in role["selected_exercise_assignments"]
            ]

    def _outcome(role_map: dict[str, Any]) -> tuple:
        apply_realised_load_calendar_revalidation(
            role_map,
            redose_callback=redose_callback,
            recompose_conditioning_callback=recompose_conditioning_callback,
            recompose_strength_callback=recompose_strength_callback,
        )
        return (
            _placement_signature(role_map),
            repr(role_map.get("realised_load_revalidation")),
        )

    original_doses = {
        position: _resolved_strength_doses(role)
        for position, role in _strength_roles_by_position(weekly_role_map).items()
    }
    baseline = _outcome(deepcopy(weekly_role_map))
    accepted: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for proposal in proposals:
        trial = deepcopy(weekly_role_map)
        _add(trial, [*accepted, proposal])
        redose_callback(trial)
        trial_roles = _strength_roles_by_position(trial)
        doses_kept = all(
            _resolved_strength_doses(trial_roles[position]).get(key) == dose
            for position, doses in original_doses.items()
            for key, dose in doses.items()
        )
        position, _assignment = proposal
        decision = _strength_roles_by_position(weekly_role_map)[position][
            "strength_composition_policy"
        ]["standalone_minimum"]
        if not doses_kept:
            decision.update(status="rejected", reason="existing_dose_changed")
        elif _outcome(trial) != baseline:
            decision.update(status="rejected", reason="realised_load_placement_changed")
        else:
            decision["status"] = "applied"
            accepted.append(proposal)

    if accepted:
        _add(weekly_role_map, accepted)
        roles = _strength_roles_by_position(weekly_role_map)
        for position, _assignment in accepted:
            roles[position]["strength_composition_policy"]["standalone_minimum_applied"] = True
        redose_callback(weekly_role_map)
    return weekly_role_map


def _conditioning_duration_minutes(option: dict[str, Any]) -> float | None:
    return conditioning_dose_minutes(_conditioning_effective_dose(option))


def _conditioning_is_high_load(option: dict[str, Any]) -> bool:
    metadata = option.get("selection_metadata") if isinstance(option.get("selection_metadata"), dict) else {}
    rpe = _float_or_none(metadata.get("rpe"))
    load_tokens = " ".join(
        str(metadata.get(key) or "").strip().lower()
        for key in ("load", "intensity", "lactate_load", "movement_cost")
    )
    return bool((rpe is not None and rpe >= 8) or re.search(r"\b(?:high|max|maximal|all[- ]out)\b", load_tokens))


def _conditioning_high_load_dose_known(option: dict[str, Any]) -> bool:
    dose = _conditioning_effective_dose(option)
    return bool(
        (_float_or_none(dose.get("work_sec")) or 0) > 0
        and (_float_or_none(dose.get("rest_sec")) or -1) >= 0
        and _conditioning_rounds(option) is not None
    )


def _conditioning_rounds(option: dict[str, Any]) -> int | None:
    metadata = option.get("selection_metadata") if isinstance(option.get("selection_metadata"), dict) else {}
    rounds = _float_or_none(metadata.get("rounds"))
    if rounds is None or rounds <= 0 or not rounds.is_integer():
        return None
    return int(rounds)


def _conditioning_effective_dose(option: dict[str, Any]) -> dict[str, Any]:
    """Bank dose for this option, resolved to the athlete's own round length."""
    metadata = option.get("selection_metadata") if isinstance(option.get("selection_metadata"), dict) else {}
    athlete_model = get_planner_athlete_model() or {}
    return conditioning_effective_dose(
        metadata, athlete_round_seconds(athlete_model.get("rounds_format"))
    )


def _conditioning_active_work_seconds(option: dict[str, Any]) -> float | None:
    metadata = option.get("selection_metadata") if isinstance(option.get("selection_metadata"), dict) else option
    if not has_meaningful_fulfillment_authority(
        metadata,
        source_kind="conditioning",
        source=str(metadata.get("_schema_source") or ""),
        allow_legacy_unstamped=True,
    ):
        return 0.0
    return conditioning_dose_active_work_seconds(_conditioning_effective_dose(option))


def _conditioning_selected_active_work_seconds(
    selected: list[tuple[dict[str, Any], dict[str, Any], bool]],
    allocated_rounds: dict[str, int],
) -> float:
    """Measure the retained conditioning workload after high-load partitioning."""
    active_work = 0.0
    for _slot, option, _is_selected in selected:
        metadata = option.get("selection_metadata") if isinstance(option.get("selection_metadata"), dict) else option
        if not has_meaningful_fulfillment_authority(
            metadata,
            source_kind="conditioning",
            source=str(metadata.get("_schema_source") or ""),
            allow_legacy_unstamped=True,
        ):
            continue
        dose = _conditioning_effective_dose(option)
        work_sec = _float_or_none(dose.get("work_sec"))
        allocated = allocated_rounds.get(str(option.get("name") or ""))
        if allocated is not None and work_sec is not None and work_sec > 0:
            active_work += work_sec * allocated
        else:
            active_work += _conditioning_active_work_seconds(option) or 0.0
    return active_work


def _conditioning_selected_minutes(
    selected: list[tuple[dict[str, Any], dict[str, Any], bool]],
) -> float:
    return sum(_conditioning_duration_minutes(option) or 0.0 for _, option, _ in selected)


def _conditioning_round_seconds(option: dict[str, Any]) -> float | None:
    """Round duration for a round-based option, owned by the athlete's format.

    Only an option the bank marks ``round_based`` is a fight round, so only
    those re-anchor to the athlete's own "Rounds x Minutes" intake. Every other
    dose — machine threshold blocks, EMOMs, carries, short interval drills —
    keeps the work interval the bank authored. Round count, rest, RPE, exercise
    identity, scoring and mechanical metadata are untouched either way.
    """
    metadata = option.get("selection_metadata") if isinstance(option.get("selection_metadata"), dict) else {}
    if not metadata.get("round_based"):
        return None
    athlete_model = get_planner_athlete_model() or {}
    return athlete_round_seconds(athlete_model.get("rounds_format"))


def _conditioning_prescription(option: dict[str, Any], *, rounds: int | None = None) -> str:
    metadata = option.get("selection_metadata") if isinstance(option.get("selection_metadata"), dict) else {}
    athlete_round_sec = _conditioning_round_seconds(option)
    # A round-based option states its work interval in fight rounds, so it
    # renders at the athlete's own round length even when nothing repartitioned
    # it. The bank keeps the round count, rest and RPE.
    effective_rounds = rounds if rounds is not None else (
        _conditioning_rounds(option) if athlete_round_sec else None
    )
    if effective_rounds is not None:
        rendered = conditioning_round_prescription(
            effective_rounds,
            athlete_round_sec,
            work_sec=_float_or_none(metadata.get("work_sec")),
            rest_sec=_float_or_none(metadata.get("rest_sec")),
            rpe=_float_or_none(metadata.get("rpe")),
        )
        if rendered:
            return rendered
    base = str(option.get("prescription") or metadata.get("timing") or metadata.get("duration") or "").strip()
    if base:
        from .working_dose import working_dose
        return working_dose(base, duration_max=True)
    total_minutes = _float_or_none(metadata.get("total_minutes"))
    return f"{total_minutes:g} min" if total_minutes is not None and total_minutes > 0 else ""


def _conditioning_partition_high_load(
    selected: list[tuple[dict[str, Any], dict[str, Any], bool]],
    *,
    phase: str,
    system: str,
    rounds_format: str | None = None,
) -> tuple[
    list[tuple[dict[str, Any], dict[str, Any], bool]],
    dict[str, int],
    str | None,
    dict[str, float | None],
]:
    """Partition one phase-appropriate workload across known high-load drills."""
    high_load = []
    for item in selected:
        if not _conditioning_is_high_load(item[1]):
            continue
        metadata = item[1].get("selection_metadata") if isinstance(item[1].get("selection_metadata"), dict) else {}
        dose_text = str(metadata.get("duration") or metadata.get("timing") or "").lower()
        if not re.search(r"\b(?:reps?|per[-\s]?side|/side|yds?|yards?|meters?|feet|ft|m)\b", dose_text):
            high_load.append(item)
    if len(high_load) < 2:
        return selected, {}, None, {}

    dose_data: list[tuple[tuple[dict[str, Any], dict[str, Any], bool], float, float, int]] = []
    for item in high_load:
        metadata = _conditioning_effective_dose(item[1])
        work_sec = _float_or_none(metadata.get("work_sec"))
        rest_sec = _float_or_none(metadata.get("rest_sec"))
        rounds = _conditioning_rounds(item[1])
        if (
            work_sec is None
            or work_sec <= 0
            or rest_sec is None
            or rest_sec < 0
            or rounds is None
        ):
            retained = [candidate for candidate in selected if candidate not in high_load]
            return retained, {}, "high_load_dose_unknown", {}
        dose_data.append((item, work_sec, rest_sec, rounds))

    target_active_work, elapsed_cap_minutes = _conditioning_phase_workload_envelope(
        phase=phase, system=system, rounds_format=rounds_format
    )
    if target_active_work is None or elapsed_cap_minutes is None:
        return selected, {}, None, {}

    elapsed_cap_seconds = elapsed_cap_minutes * 60.0
    minimum_required = sum(work_sec for _, work_sec, _, _ in dose_data)
    minimum_elapsed = sum(work_sec for _, work_sec, _, _ in dose_data)
    if minimum_required > target_active_work or minimum_elapsed > elapsed_cap_seconds:
        retained = [candidate for candidate in selected if candidate not in high_load]
        return retained, {}, "high_load_dose_underfilled", {
            "target_active_work_seconds": target_active_work,
            "elapsed_cap_seconds": elapsed_cap_seconds,
        }

    allocated = {str(item[1].get("name") or ""): 1 for item, _, _, _ in dose_data}
    active_work = minimum_required
    elapsed_work = minimum_elapsed
    while True:
        progressed = False
        for item, work_sec, rest_sec, max_rounds in dose_data:
            name = str(item[1].get("name") or "")
            additional_elapsed = work_sec + rest_sec
            if (
                allocated[name] >= max_rounds
                or active_work >= target_active_work
                or elapsed_work + additional_elapsed > elapsed_cap_seconds
            ):
                continue
            allocated[name] += 1
            active_work += work_sec
            elapsed_work += additional_elapsed
            progressed = True
        if not progressed:
            break
    underfill_reason = None if active_work >= target_active_work else "phase_system_dose_capacity_limited"
    return selected, allocated, underfill_reason, {
        "target_active_work_seconds": target_active_work,
        "allocated_active_work_seconds": active_work,
        "elapsed_cap_seconds": elapsed_cap_seconds,
        "allocated_elapsed_seconds": elapsed_work,
    }


def _scheduled_phase_for_role(week: dict[str, Any], role: dict[str, Any]) -> str:
    """Return the Stage 1 phase that owns this role's own scheduled day.

    A calendar week is a container, not a phase: its label is the phase the week
    starts in, and a week can straddle a phase boundary (a week labelled GPP can
    still hold a D-20 role that canonically belongs to SPP). Dated roles must
    resolve their phase from their own D-day, which is exactly what the
    authority validator does -- so both read the same Stage 1
    ``phase_weeks.days`` allocation through ``scheduled_phase_for_role``.

    Returns "" when the role carries a D-day the camp's allocation cannot map
    (an out-of-range day, or a camp with no allocation recorded). That empty
    result is meaningful -- it is the only case in which falling back to the
    week label is safe -- so callers apply their own fallback rather than having
    one baked in here. An undated role still resolves to the week label, via
    ``spec_phase``.
    """
    athlete_model = get_planner_athlete_model() or {}
    return scheduled_phase_for_role(
        role,
        athlete_model=athlete_model,
        spec_phase=week.get("phase"),
    )


def _conditioning_role_is_hard_spar_adjacent(week: dict[str, Any], role: dict[str, Any]) -> bool:
    weekday_order = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    scheduled_day = str(role.get("scheduled_day_hint") or "").strip().lower()
    scheduled_index = weekday_order.get(scheduled_day)
    if scheduled_index is None:
        return False

    hard_days = {
        str(day).strip().lower()
        for day in (week.get("effective_hard_sparring_days") or [])
        if str(day).strip()
    }
    if not hard_days:
        hard_days = {
            str(entry.get("day") or "").strip().lower()
            for entry in (week.get("hard_sparring_plan") or [])
            if isinstance(entry, dict) and entry.get("status") == "hard_as_planned"
        }
    return any(
        ((weekday_order[hard_day] - scheduled_index) % 7) in {1, 6}
        for hard_day in hard_days
        if hard_day in weekday_order
    )


def _option_metadata(option: dict[str, Any]) -> dict[str, Any]:
    metadata = option.get("selection_metadata")
    return metadata if isinstance(metadata, dict) else option


def _conditioning_option_has_authority(option: dict[str, Any]) -> bool:
    """A conditioning option may fill a system role only with fulfilment authority."""
    authority = option.get("fulfillment_authority")
    if authority is not None:
        return authority is True
    metadata = _option_metadata(option)
    return has_meaningful_fulfillment_authority(
        metadata,
        source_kind="conditioning",
        source=str(metadata.get("_schema_source") or ""),
        allow_legacy_unstamped=True,
    )


def _is_low_load_support_role(role: dict[str, Any]) -> bool:
    """An unused day upgraded to low-load support (mobility, recovery flush,
    rehab-friendly or low-aerobic gas tank). Its contract is the role's own
    ``selection_rule``: low impact, low lactate, low CNS, never hard work."""
    return (role.get("governance") or {}).get("authority") == "unused_day_low_load_support_upgrade"


def _meets_low_load_support_contract(role: dict[str, Any], metadata: dict[str, Any]) -> bool:
    """Judge the drill with the bank's own structured freshness profile and the
    role's declared blocked tags, systems and intensities; no new threshold is
    introduced here."""
    from .conditioning import _conditioning_structured_profile

    raw_tags = [*(metadata.get("tags") or []), *(metadata.get("mechanical_risk_tags") or [])]
    tags = {str(tag).strip().lower() for tag in raw_tags}
    system = str(metadata.get("system") or "").strip().lower()
    return bool(
        _conditioning_structured_profile(metadata, system=system)["freshness"]
        and not tags & {str(tag).strip().lower() for tag in role.get("blocked_tags") or []}
        and system not in {str(value).strip().lower() for value in role.get("blocked_systems") or []}
        and str(metadata.get("intensity") or "").strip().lower()
        not in {str(value).strip().lower() for value in role.get("blocked_intensities") or []}
    )


def conditioning_assignment_from_slot(
    phase: str, slot: dict[str, Any], option: dict[str, Any],
    *, is_selected: bool = True, allocated_rounds: int | None = None,
) -> dict[str, Any]:
    """Existing conditioning membership and dose authority, also for complements."""
    name = str(option.get("name") or "")
    assignment = {
        "slot_id": slot.get("slot_id"),
        "name": name,
        "source_phase": phase,
        "slot_group": "conditioning_slots",
        "selected_option": is_selected,
        "base_prescription": _conditioning_prescription(option),
        "effective_prescription": _conditioning_prescription(option, rounds=allocated_rounds),
        "effective_rounds": allocated_rounds,
        "support_only": option.get("support_only") is True,
        "meaningful_stress": option.get("meaningful_stress") is True,
        "fulfillment_authority": option.get("fulfillment_authority") is True,
    }
    stamp_exercise_key(assignment)
    # Carry the canonical structured dose alongside the rendered
    # text. The assignment is what persists; the candidate pool it
    # was resolved from is compacted, so a downstream reader that
    # only sees the assignment had no numeric dose at all and could
    # not tell 8 x 5 sec / 60 sec alactic work from anything else.
    # Structured recognition must never depend on parsing the
    # human-readable prescription back out.
    effective_dose = _conditioning_effective_dose(option)
    metadata = option.get("selection_metadata") or {}
    for field in ("system", "modality", "duration", "total_minutes", "duration_min", "distance_m"):
        value = effective_dose.get(field, metadata.get(field, option.get(field)))
        if value is not None:
            assignment[field] = value
    for field in ("rpe", "lactate_load", "intensity"):
        if metadata.get(field) is not None:
            assignment[field] = metadata[field]
    for field in ("work_sec", "rest_sec", "rounds"):
        value = _float_or_none(effective_dose.get(field))
        if value is not None:
            assignment[field] = value
    # Same reason as the strength path: the realised mechanical
    # identity of the session must survive onto the assignment.
    conditioning_tags = option_mechanical_risk_tags(option)
    if conditioning_tags:
        assignment["mechanical_risk_tags"] = sorted(conditioning_tags)
    notes = _selected_coaching_notes(option)
    if notes:
        assignment["coaching_notes"] = notes
    assignment.update(_selected_coaching_fields(option))
    return assignment


def compose_normal_conditioning_assignments(
    *,
    weekly_role_map: dict[str, Any],
    candidate_pools: dict[str, Any],
    only_roles: set[int] | None = None,
) -> dict[str, Any]:
    """Attach a safe bank-backed minimum composition to normal conditioning roles.

    ``only_roles`` restricts composition to the given roles, addressed by
    ``id()``. It exists so a caller that has invalidated a *specific* role's
    membership can reconcile exactly that role through this authority, rather
    than recomposing every conditioning session in the plan and churning
    membership that is still correct.
    """
    athlete_model = get_planner_athlete_model()
    rounds_format = (
        athlete_model.get("rounds_format") if isinstance(athlete_model, dict) else None
    )
    pressure_context = _composition_context_from_model(athlete_model)
    trunk_strength_selected = _trunk_strength_selected(athlete_model)

    for week in weekly_role_map.get("weeks", []) or []:
        if not isinstance(week, dict):
            continue
        embedded_trunk_support_count = 0
        for role in week.get("session_roles", []) or []:
            if (
                not isinstance(role, dict)
                or role.get("late_fight_tail_owned")
                or str(role.get("category") or "").strip().lower() != "conditioning"
                or (only_roles is not None and id(role) not in only_roles)
            ):
                continue

            # Conditioning keeps the week label as its fallback; that fallback
            # now lives here rather than inside the shared resolver.
            phase = _scheduled_phase_for_role(week, role) or str(
                week.get("phase") or ""
            ).strip().upper()
            pool = candidate_pools.get(phase) if isinstance(candidate_pools, dict) else None
            slots = pool.get("conditioning_slots", []) if isinstance(pool, dict) else []
            strength_slots = pool.get("strength_slots", []) if isinstance(pool, dict) else []

            preferred_system = str(role.get("preferred_system") or "").strip().lower()
            low_load_only = _is_low_load_support_role(role)
            # Membership is one member per Stage 1 slot. A slot's ``alternates``
            # are its same-role substitution reservoir, not additional session
            # members: drawing from them turns one selected drill plus its two
            # backups into a fake three-exercise circuit. A low-load support role
            # may only substitute one alternate when the selected drill breaks
            # the role's low-load contract.
            options: list[tuple[dict[str, Any], dict[str, Any], bool]] = []
            seen_names: set[str] = set()
            for slot in slots:
                if not isinstance(slot, dict) or str(slot.get("role") or "").strip().lower() != preferred_system:
                    continue
                option = slot.get("selected")
                if not isinstance(option, dict) or not _conditioning_option_has_authority(option):
                    continue
                is_selected = True
                if low_load_only and not _meets_low_load_support_contract(role, _option_metadata(option)):
                    # An alternate passes every check the selected drill must.
                    option = next(
                        (
                            alternate
                            for alternate in slot.get("alternates") or []
                            if isinstance(alternate, dict)
                            and _conditioning_option_has_authority(alternate)
                            and _meets_low_load_support_contract(role, _option_metadata(alternate))
                        ),
                        None,
                    )
                    is_selected = False
                    if option is None:
                        continue
                name = str(option.get("name") or "").strip()
                if not name or name in seen_names:
                    continue
                seen_names.add(name)
                options.append((slot, option, is_selected))

            adjacent_hard_spar = _conditioning_role_is_hard_spar_adjacent(week, role)
            target_active_work, _ = _conditioning_phase_workload_envelope(
                phase=phase,
                system=preferred_system,
                rounds_format=rounds_format,
            )
            if options and _option_metadata(options[0][1]).get("standalone_protocol"):
                # Full protocols have their own stimulus/dose. In particular,
                # four 30s maximal sprints are not an underfilled eight-minute
                # circuit that needs more exercises added to it.
                options = options[:1]
                target_active_work = _conditioning_active_work_seconds(options[0][1])
            unit_based_alactic = bool(
                preferred_system == "alactic" and options
                and not conditioning_work_is_timed(_option_metadata(options[0][1]))
            )
            if unit_based_alactic:
                # A selected rep/distance speed dose stays in its own units.
                # Neither pad it with drills to reach fictitious seconds nor
                # fail release for being below a time-only conditioning target.
                options = options[:1]
                target_active_work = None
            distance_only_endurance = bool(
                preferred_system in {"aerobic", "glycolytic"} and options
                and (_float_or_none(_option_metadata(options[0][1]).get("distance_m")) or 0) > 0
                and _conditioning_active_work_seconds(options[0][1]) is None
            )
            if distance_only_endurance:
                # A 5K or measured track repeat has an exact distance but no
                # known completion time. Keep one dose, never invent seconds.
                options = options[:1]
                target_active_work = None
            if not options:
                role["selected_exercise_assignments"] = []
                # A targeted recomposition can follow a countdown morph that
                # deliberately turns a former conditioning role into a light
                # touch with no matching bank membership. Preserve that explicit
                # reduction path instead of converting it into an underfill.
                if only_roles is not None:
                    role.pop("conditioning_composition_policy", None)
                    continue
                role["conditioning_composition_policy"] = {
                    "minimum_exercise_count": None if adjacent_hard_spar else 0,
                    "selected_count": 0,
                    "long_aerobic_session": False,
                    "hard_sparring_adjacent": adjacent_hard_spar,
                    "high_load_workload_envelope": {},
                    "partitioned_high_load_rounds": {},
                    "conditioning_target_active_work_seconds": target_active_work,
                    "conditioning_active_work_seconds": 0.0,
                    "conditioning_workload_met": None if adjacent_hard_spar else False,
                    "underfill_reason": None if adjacent_hard_spar else "phase_system_workload_unmet",
                    "workload_limited": not adjacent_hard_spar,
                    "embedded_trunk_support": False,
                    "embedded_trunk_support_count": 0,
                    "embedded_trunk_support_skip_reason": "conditioning_workload_unmet",
                }
                continue

            # The workload envelope, not the number of selected exercises,
            # closes a normal conditioning role.
            selected: list[tuple[dict[str, Any], dict[str, Any], bool]] = []
            total_minutes = 0.0
            active_work_seconds = 0.0
            high_load_rounds: dict[str, int] = {}
            high_load_budget: dict[str, float | None] = {}
            underfill_reason: str | None = None
            for slot, option, is_selected in options:
                duration = _conditioning_duration_minutes(option)
                if selected and duration is not None and total_minutes + duration > 45:
                    continue
                selected.append((slot, option, is_selected))
                (
                    selected,
                    high_load_rounds,
                    partition_reason,
                    high_load_budget,
                ) = _conditioning_partition_high_load(
                    selected,
                    phase=phase,
                    system=preferred_system,
                    rounds_format=rounds_format,
                )
                # Partitioning can remove high-load candidates. From this point
                # on the retained list owns both elapsed budget and workload.
                total_minutes = _conditioning_selected_minutes(selected)
                active_work_seconds = _conditioning_selected_active_work_seconds(
                    selected, high_load_rounds
                )
                underfill_reason = partition_reason or underfill_reason
                if adjacent_hard_spar or unit_based_alactic or distance_only_endurance:
                    break
                if target_active_work is not None and active_work_seconds >= target_active_work:
                    underfill_reason = None
                    break
            if underfill_reason == "high_load_dose_unknown":
                selected = [
                    item
                    for item in selected
                    if not (
                        _conditioning_is_high_load(item[1])
                        and not _conditioning_high_load_dose_known(item[1])
                    )
                ]
                total_minutes = _conditioning_selected_minutes(selected)
                active_work_seconds = _conditioning_selected_active_work_seconds(
                    selected, high_load_rounds
                )
            long_aerobic = preferred_system == "aerobic" and total_minutes >= 25
            workload_met = None if unit_based_alactic or distance_only_endurance else (
                target_active_work is not None and active_work_seconds >= target_active_work
            )
            if adjacent_hard_spar:
                minimum = None
            elif workload_met:
                minimum = len(selected)
            else:
                minimum = None

            assignments = []
            selected_names: set[str] = set()
            for slot, option, is_selected in selected:
                name = str(option.get("name") or "")
                if name:
                    selected_names.add(name)
                allocated_rounds = high_load_rounds.get(name)
                assignment = conditioning_assignment_from_slot(
                    phase, slot, option, is_selected=is_selected, allocated_rounds=allocated_rounds,
                )
                assignments.append(assignment)

            trunk_support_added = False
            trunk_support_skip_reason = ""
            pressure_state = _role_pressure_state(
                pressure_context,
                role=role,
                fatigue_applied=True,
            )
            if (
                workload_met
                and trunk_strength_selected
                and not adjacent_hard_spar
                and not low_load_only
                and int(pressure_state["pressure"]) < 2
            ):
                trunk_options = [
                    record
                    for record in _low_load_trunk_support_records(strength_slots)
                    if record["name"] not in selected_names
                ]
                if embedded_trunk_support_count >= 2:
                    trunk_support_skip_reason = "weekly_trunk_support_cap"
                elif not trunk_options:
                    # The phase's Stage 1 pool holds no low-load trunk drill to
                    # embed; say so rather than leave an unexplained gap.
                    trunk_support_skip_reason = "no_low_load_trunk_candidate"
                else:
                    record = trunk_options[0]
                    assignment = assignment_from_slot(phase, "strength_slots", record["slot"])
                    if assignment:
                        assignment.update(
                            {
                                "embedded_support": True,
                                "support_dose_category": "low_load_trunk",
                                "effective_prescription": _embedded_trunk_support_prescription(record),
                            }
                        )
                        assignments.append(assignment)
                        selected_names.add(record["name"])
                        embedded_trunk_support_count += 1
                        trunk_support_added = True
            elif trunk_strength_selected:
                # A day upgraded to low-load support keeps that contract: its
                # membership is the conditioning drill that met it, not added
                # S&C work.
                trunk_support_skip_reason = (
                    "hard_sparring_adjacent"
                    if adjacent_hard_spar
                    else "low_load_support_day"
                    if low_load_only
                    else "conditioning_workload_unmet"
                    if not workload_met
                    else "readiness_pressure"
                )

            role["selected_exercise_assignments"] = assignments
            role["conditioning_composition_policy"] = {
                "minimum_exercise_count": minimum,
                "selected_count": len(assignments),
                "long_aerobic_session": long_aerobic,
                "distance_dose_unmeasured": distance_only_endurance,
                "hard_sparring_adjacent": adjacent_hard_spar,
                "high_load_workload_envelope": high_load_budget,
                "partitioned_high_load_rounds": high_load_rounds,
                "conditioning_target_active_work_seconds": target_active_work,
                "conditioning_active_work_seconds": active_work_seconds,
                "conditioning_workload_met": None if adjacent_hard_spar else workload_met,
                "underfill_reason": (
                    None
                    if adjacent_hard_spar or workload_met or unit_based_alactic or distance_only_endurance
                    else underfill_reason or "phase_system_workload_unmet"
                ),
                "workload_limited": not adjacent_hard_spar and not workload_met and not unit_based_alactic and not distance_only_endurance,
                "embedded_trunk_support": trunk_support_added,
                "embedded_trunk_support_count": 1 if trunk_support_added else 0,
                "embedded_trunk_support_skip_reason": trunk_support_skip_reason,
            }

    return weekly_role_map


# A relocation changes the role's scheduled day, which re-morphs and re-doses it,
# which changes its realised load, which can in principle change where it belongs.
# That is a fixpoint search, and it is deliberately not run to convergence: two
# rounds (an initial move plus one verification round that may correct it) is the
# bound. Anything unresolved after that is recorded as a residual conflict rather
# than chased, so this pass can never loop or oscillate.
_MAX_REVALIDATION_ROUNDS = 2


def _refresh_stress_stamps(weekly_role_map: dict[str, Any]) -> list[dict[str, Any]]:
    """Re-stamp every composed role from its *current* authoritative dose.

    Called once per round, and once more after the final round, because a
    relocation re-morphs and re-doses the role it moved: a stamp written before
    that describes the dose the role had on the day it left. The stamp persists
    on the role, so a stale one would be a lie to every later reader, not just to
    this pass.

    Neighbours are re-stamped too, not only the relocated role: a move changes
    which days sit next to which, and the morph owner may re-dose more than the
    role that moved.

    Late-fight tail roles are stamped and judged like every other composed
    session, so a D-14 session is measured against the tail's real load and a
    tail conflict is visible. Their placement stays immutable: the canonical
    mover refuses to relocate a tail role, so a conflict on one is recorded in
    ``residual_conflicts`` rather than moved. The tail's membership owner keeps
    its sessions inside their envelope when it composes them.
    """
    stamped: list[dict[str, Any]] = []
    for week in weekly_role_map.get("weeks", []) or []:
        if not isinstance(week, dict):
            continue
        for role in week.get("session_roles", []) or []:
            if not isinstance(role, dict):
                continue
            stress = realised_role_stress(role)
            if stress is None:
                # Drop only our stale derived measurement. Explicit technical
                # stress remains authoritative even without app-owned exercises.
                stamp = role.get("calendar_stress")
                if isinstance(stamp, dict) and stamp.get("authority") == "realised_session_composition":
                    role.pop("calendar_stress", None)
                continue
            role["calendar_stress"] = _stress_stamp(stress)
            stamped.append(role)
    return stamped


def _physically_occupied_d_days(
    weekly_role_map: dict[str, Any], mover: dict[str, Any]
) -> set[int]:
    """Days already carrying a physical session, excluding the mover's own.

    This pass exists to relieve concentrated load, so it must not relieve an
    adjacency by stacking: moving a substantial session onto a day that already
    holds one concentrates the very load being spread out. The shared policy
    legitimately permits two physical sessions to share a day (strength plus an
    easy aerobic flush, say) and that judgement is not this pass's to change --
    so this constrains only where *this* pass is willing to move a role, via the
    mover's excluded days, and leaves ``_same_day_decision`` untouched.
    """
    from .calendar_context import role_refs
    from .combat_load_policy import DayOccupancy

    compatible_technical_days = {
        ref.d_day for ref in role_refs(weekly_role_map)
        if ref.profile.allows_light_combat_stack
    }
    return {
        ref.d_day
        for ref in role_refs(weekly_role_map)
        if ref.role is not mover
        and ref.d_day not in compatible_technical_days
        and ref.profile.occupancy
        in {DayOccupancy.PHYSICAL, DayOccupancy.EXCLUSIVE_PHYSICAL}
    }


# The fields that decide *what kind of session* a role is, and therefore which
# bank membership is valid for it. The countdown morph can rewrite all three at
# once (``_morph_to_rhythm_touch`` turns a hard glycolytic fight-pace role into an
# aerobic rhythm touch), which strands any membership composed for the old
# identity. Dose fields are deliberately not included: a re-dosed role keeps its
# membership, and only its prescription changes.
_ROLE_IDENTITY_FIELDS = ("role_key", "category", "preferred_system")


def _role_identity(role: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(role.get(field) or "").strip().lower() for field in _ROLE_IDENTITY_FIELDS
    )


def _conditioning_identities(weekly_role_map: dict[str, Any]) -> dict[int, tuple[str, ...]]:
    return {
        id(role): _role_identity(role)
        for week in weekly_role_map.get("weeks", []) or []
        if isinstance(week, dict)
        for role in week.get("session_roles", []) or []
        if isinstance(role, dict)
        and not role.get("late_fight_tail_owned")
        and str(role.get("category") or "").strip().lower() == "conditioning"
    }


def _realised_load_conflicts(
    weekly_role_map: dict[str, Any], roles: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], Any, Any]]:
    """``(role, ref, decision)`` for each role the policy does not ALLOW."""
    from .calendar_context import build_events, role_refs
    from .combat_load_policy import PlacementDirective, evaluate_candidate_at_position

    refs = {id(ref.role): ref for ref in role_refs(weekly_role_map)}
    conflicts = []
    for role in roles:
        ref = refs.get(id(role))
        if ref is None:
            continue
        decision = evaluate_candidate_at_position(
            ref.profile,
            candidate_position=-ref.d_day,
            events=build_events(weekly_role_map, exclude_role=role),
            candidate_scope=ref.scope,
        )
        if decision.directive is not PlacementDirective.ALLOW:
            conflicts.append((role, ref, decision))
    # Preserve the strength anchor when optional aerobic support collides with
    # it. All other conflict ordering and existing training priorities stay put.
    from .session_workload import UNSAFE_STACKING_CODE
    conflicts.sort(key=lambda item: (
        item[2].reason_code == UNSAFE_STACKING_CODE and item[0].get("category") == "strength"
    ))
    return conflicts


def apply_realised_load_calendar_revalidation(
    weekly_role_map: dict[str, Any],
    *,
    redose_callback: Callable[[dict[str, Any]], Any] | None = None,
    recompose_conditioning_callback: Callable[[dict[str, Any], set[int]], Any] | None = None,
    recompose_strength_callback: Callable[[dict[str, Any], set[int]], Any] | None = None,
    only_roles: set[int] | None = None,
) -> dict[str, Any]:
    """Re-judge composed sessions on their realised load, not their planned role.

    Placement and the final governor both run before conditioning composition, so
    they judge a role by the promise its role key makes. An
    ``alactic_speed_day`` is stamped ``NEURAL_MICRODOSE`` from its key alone; the
    session that is actually composed onto it may be two substantial unilateral
    plyometric blocks. Nothing downstream ever re-asked the calendar.

    This closes that sequence — role placement, exercise composition, realised
    load classification, calendar revalidation — for composed S&C roles.

    It is representation plus the canonical mover, not a second policy:
    ``realised_role_stress`` measures in ``combat_load_policy``'s vocabulary, the
    stamp goes on the role, and the legality verdict and any relocation come from
    ``combat_load_policy`` through
    ``calendar_integrity.relocate_or_suppress_role_for_recovery`` — the same
    canonical mover the adjacent-strength-recovery pass already uses.

    A graded conflict moves only to a genuinely cleaner slot. A forbidden
    realised dose must relocate or be suppressed through the canonical mover;
    it cannot stay merely because no cleaner destination exists.

    Ordering requirement: this must run after ``prescription_resolver``, because
    a strength assignment carries only the raw bank dose until the resolver has
    written the role's ``effective_strength_prescriptions``. Measuring before
    that scores a capped primer at its uncapped bank dose.

    Relocation invalidates the countdown morph, the membership of a moved
    strength role and the resolved dose, so all three owners are re-run and the
    destination is then re-judged
    against the *refreshed* load. That verification is bounded at
    ``_MAX_REVALIDATION_ROUNDS``; a conflict surviving the bound is recorded in
    ``residual_conflicts`` and the role is left where it is, never chased.
    """
    if not isinstance(weekly_role_map, dict):
        return weekly_role_map

    from .late_camp_role_morph import _apply_late_camp_role_morph_once
    from .combat_load_policy import PlacementDirective

    def _reconcile_changed_conditioning(before: dict[int, tuple[str, ...]]) -> list[dict[str, Any]]:
        """Re-reconcile membership for any conditioning role the morph rewrote.

        The morph can change a role's *identity*, not just its dose: a hard
        glycolytic fight-pace role crossing into the fight-pace morph band comes
        back as an aerobic rhythm touch. Its ``selected_exercise_assignments``
        were composed for the session it used to be, so leaving them is stale
        hard glycolytic work sitting on a low-cost recovery role.

        Only roles whose identity actually changed are reconciled, through the
        existing conditioning composition authority. Every other conditioning
        session keeps its closed membership untouched.

        Stale membership is cleared first and unconditionally, because
        recomposition may legitimately produce nothing: the composer skips a role
        whose pool holds no candidate for its new system. The outcome recorded
        per role is therefore whatever actually resulted — ``recomposed`` only
        when a replacement session exists, ``membership_cleared`` otherwise.
        """
        changed = [
            role
            for week in weekly_role_map.get("weeks", []) or []
            if isinstance(week, dict)
            for role in week.get("session_roles", []) or []
            if isinstance(role, dict)
            and id(role) in before
            and _role_identity(role) != before[id(role)]
            and role.get("selected_exercise_assignments")
        ]
        if not changed:
            return []
        notes = [
            {
                "role_key": role.get("role_key"),
                "previous_role_key": before[id(role)][0],
                "previous_system": before[id(role)][2],
                "d_day": role.get("scheduled_d_day"),
                "stale_membership": [
                    str(item.get("name") or "")
                    for item in role.get("selected_exercise_assignments") or []
                    if isinstance(item, dict)
                ],
            }
            for role in changed
        ]
        # Clear before recomposing, never after. The composition authority skips a
        # role for which no compliant candidate exists, so recomposition is not
        # guaranteed to overwrite anything: without clearing first, a glycolytic
        # session whose pool holds no aerobic option would keep its pre-morph
        # membership on an aerobic role even though reconciliation ran. Clearing
        # here rather than inside the composer also leaves ordinary, non-targeted
        # composition behaviour untouched.
        for role in changed:
            role["selected_exercise_assignments"] = []
            # This block reports selected_count and the composition decisions for
            # membership that no longer exists, so it goes with it.
            role.pop("conditioning_composition_policy", None)
        if recompose_conditioning_callback is not None:
            recompose_conditioning_callback(weekly_role_map, {id(r) for r in changed})
        for note, role in zip(notes, changed):
            membership = [
                str(item.get("name") or "")
                for item in role.get("selected_exercise_assignments") or []
                if isinstance(item, dict)
            ]
            note["membership"] = membership
            # Report what actually happened. "recomposed" claims a replacement
            # session exists; when the pool offered nothing compliant the honest
            # outcome is an explicitly empty closed membership.
            note["action"] = "recomposed" if membership else "membership_cleared"
        return notes

    def _remorph_and_redose(moved_strength: set[int]) -> None:
        # The morph and the strength dose were both resolved for the day the role
        # has just left (strength morphs through D-17, so a D-20 -> D-16 move
        # changes the applicable cap). Both canonical owners are re-run, exactly
        # as the final governor re-runs the morph after it relocates a role. The
        # dose owner needs the candidate pools and athlete model, which are the
        # pipeline's to hold, so it arrives as a callback rather than an import.
        identities_before = _conditioning_identities(weekly_role_map)
        _apply_late_camp_role_morph_once(weekly_role_map)
        # Membership before dose: the resolver and the next stress refresh must
        # both see the session the role actually is now.
        reconciliations.extend(_reconcile_changed_conditioning(identities_before))
        if moved_strength and recompose_strength_callback is not None:
            recompose_strength_callback(weekly_role_map, moved_strength)
        if redose_callback is not None:
            redose_callback(weekly_role_map)

    actions: list[dict[str, Any]] = []
    reconciliations: list[dict[str, Any]] = []
    # Days a role has already been moved off. Excluding them stops a role being
    # sent back to a day it just left when the refreshed dose flips the verdict.
    vacated: dict[int, set[int]] = {}
    rounds_run = 0

    for _round in range(_MAX_REVALIDATION_ROUNDS):
        rounds_run += 1
        # Re-stamp from current doses so each round judges the calendar as it now
        # actually is, not as it was before the previous round's re-dose.
        stamped = _refresh_stress_stamps(weekly_role_map)
        round_actions: list[dict[str, Any]] = []
        moved_strength: set[int] = set()
        for role, _ref, _initial_decision in _realised_load_conflicts(weekly_role_map, stamped):
            # A local repair must not restart bounded searches for unrelated
            # graded conflicts that the completed calendar already recorded.
            # Final forbidden-load checks and residual reporting remain global.
            if only_roles is not None and id(role) not in only_roles:
                continue
            # A preceding move/suppression may have cleared this role's conflict.
            # Never act on a same-day collision captured before that mutation.
            live_conflicts = _realised_load_conflicts(weekly_role_map, [role])
            if not live_conflicts:
                continue
            _, _, decision = live_conflicts[0]
            action = relocate_or_suppress_role_for_recovery(
                weekly_role_map,
                role,
                excluded_d_days=(
                    vacated.get(id(role), set())
                    | _physically_occupied_d_days(weekly_role_map, role)
                ),
                reason_code=decision.reason_code,
                require_clean_destination=decision.directive is PlacementDirective.DEPRIORITIZE,
            )
            if not isinstance(action, dict):
                continue
            if action.get("from_d_day") is not None:
                vacated.setdefault(id(role), set()).add(int(action["from_d_day"]))
            if action.get("action") == "relocated" and str(role.get("category") or "").lower() == "strength":
                moved_strength.add(id(role))
            # Moving or removing a session changes the neighbouring strength
            # composition context too. Rebuild affected survivors so pruning
            # against a vacated neighbour does not become a permanent restriction.
            changed_days = {int(action[key]) for key in ("from_d_day", "to_d_day", "original_d_day")
                            if action.get(key) is not None}
            adjacent_days = {day + offset for day in changed_days for offset in (-1, 0, 1)}
            moved_strength.update(id(neighbour)
                for week in weekly_role_map.get("weeks") or []
                for neighbour in week.get("session_roles") or []
                if neighbour.get("category") == "strength" and not neighbour.get("late_fight_tail_owned")
                and role_d_day(week, neighbour) in adjacent_days)
            round_actions.append(
                {"round": rounds_run, "role_key": role.get("role_key"), **action}
            )
        if not round_actions:
            break
        actions.extend(round_actions)
        _remorph_and_redose(moved_strength)

    # The last round may have re-dosed after its final stamp, so refresh once more:
    # the stamp that persists must describe the dose the role actually ends with.
    final_roles = _refresh_stress_stamps(weekly_role_map)
    # The bounded search may end immediately after a destination is re-dosed.
    # A surviving graded conflict remains visible, but a normal FORBID verdict
    # cannot remain scheduled. Use the canonical suppression path without
    # starting another relocation/recomposition round; finished tails stay owned.
    for role, ref, decision in _realised_load_conflicts(weekly_role_map, final_roles):
        live_conflicts = _realised_load_conflicts(weekly_role_map, [role])
        if not live_conflicts:
            continue
        _, ref, decision = live_conflicts[0]
        if decision.directive is not PlacementDirective.FORBID or ref.d_day <= 13:
            continue
        excluded = {day.get("d_day") for day in ref.week.get("calendar_days") or []
                    if isinstance(day.get("d_day"), int)}
        action = relocate_or_suppress_role_for_recovery(
            weekly_role_map, role, excluded_d_days=excluded,
            reason_code=decision.reason_code,
        )
        if isinstance(action, dict):
            actions.append({"round": rounds_run, "role_key": role.get("role_key"), "bound_reached": True, **action})
    final_roles = _refresh_stress_stamps(weekly_role_map)
    # Every conflict still present on the final calendar is residual. A late
    # session's owner resolves its conflict before this point (relocation or a
    # policy-verified reduction); an attempted fix is never counted as resolved.
    residual = [
        {
            "role_key": role.get("role_key"),
            "d_day": ref.d_day,
            "reason_code": decision.reason_code,
            "directive": decision.directive.value,
        }
        for role, ref, decision in _realised_load_conflicts(weekly_role_map, final_roles)
    ]

    if actions or residual or reconciliations:
        weekly_role_map["realised_load_revalidation"] = {
            "schema_version": "realised_load_revalidation.v3",
            "rounds_run": rounds_run,
            "max_rounds": _MAX_REVALIDATION_ROUNDS,
            "actions": actions,
            # Roles whose canonical identity the morph rewrote, and whose
            # membership therefore had to be reconciled for the session they now
            # are. Empty in the overwhelmingly common case.
            "membership_reconciliations": reconciliations,
            # Legal but not ideal, and deliberately not chased further. Present
            # so a residual conflict is visible rather than silently accepted.
            "residual_conflicts": residual,
        }
    return weekly_role_map


def redistribute_pre_hard_power_from_main(
    *, weekly_role_map: dict[str, Any], candidate_pools: dict[str, Any],
    athlete_model: dict[str, Any],
) -> dict[str, Any]:
    """Split a genuinely dense session into 2-3 bounded-cost exercises.

    Only EXISTING, selected bank assignments move. Both the source and
    receiving appointments preserve the closed exercise identity/phase.
    This owner never adds weekly exercises or sets; if fewer than two
    compatible moves can be proved, leave the dense day as composed.
    """
    from itertools import combinations
    from .prescription_resolver import _assignment_strength_slot, _pre_hard_transfer_eligible
    from .pre_hard_contact_strength import suppress_unfunded_power_microdose

    if not isinstance(weekly_role_map, dict):
        return weekly_role_map
    for week in weekly_role_map.get("weeks") or []:
        if not isinstance(week, dict):
            continue
        if (week.get("pre_hard_power_redistribution") or {}).get("status") == "transferred":
            continue
        roles = week.get("session_roles") or []
        receivers = [
            role for role in roles if isinstance(role, dict)
            and role.get("pre_hard_contact_microdose_only") is True
            # An already-composed independent second session is not a
            # receiving slot: replacing its exercises would silently lose its
            # own training. Only an empty/provisional receiver is fundable.
            and not role.get("selected_exercise_assignments")
        ]
        for receiver in receivers:
            receiver["dense_day_receiver_provisional"] = True
            receiver.setdefault(
                "dense_day_receiver_token",
                f"{week.get('week_index')}:{receiver.get('scheduled_d_day')}",
            )
            sources = [
                role for role in roles
                if isinstance(role, dict)
                and role is not receiver
                and role.get("category") == "strength"
                and role.get("pre_hard_contact_managed_stress") is not True
                and role.get("strength_session_index") == 1
                and role.get("scheduled_day_hint") != receiver.get("scheduled_day_hint")
            ]
            source = sources[0] if len(sources) == 1 else None
            memberships = [
                item for item in (source or {}).get("selected_exercise_assignments", [])
                if isinstance(item, dict)
            ]
            # Density is the trigger, not the athlete's goal or the name of the
            # weekday. Splitting fewer than five exercises creates needless
            # appointments, and the main session must retain >=3 exercises.
            candidates: list[dict[str, Any]] = []
            if source is not None and len(memberships) >= 5:
                for index, member in enumerate(memberships):
                    if member.get("slot_group") != "strength_slots":
                        continue
                    phase = str(member.get("source_phase") or "").strip().upper()
                    slot = _assignment_strength_slot(candidate_pools, phase, member)
                    # A meaningful power label alone does not mean this
                    # transferred, reduced exercise is unsuitable. Cost, dose
                    # and actual high-load tags are checked by one shared
                    # eligibility rule also used by the prescription resolver.
                    if slot is None or not _pre_hard_transfer_eligible(slot):
                        continue
                    selected = slot.get("selected") or {}
                    kind = _role_kind(slot)
                    dose = working_dose(str(selected.get("prescription") or ""))
                    sets, reps = _parse_sets_reps(dose)
                    if sets is None or reps is None:
                        # An authorised rep alternative written as "2 sets x
                        # 6 reps or 20s tempo" must not be mistaken for an
                        # unpriceable exercise. This selects the bank's rep
                        # variant; it does not invent a new dose.
                        written = re.search(
                            r"\b(\d+)\s+sets?\s*[xX×]\s*(\d+)\s+reps?\b",
                            dose, re.I,
                        )
                        if written:
                            sets, reps = int(written[1]), int(written[2])
                    if not isinstance(sets, int) or not isinstance(reps, int):
                        continue
                    if sets < 1 or reps < 1:
                        continue
                    kind = _role_kind(slot)
                    transfer_sets = min(sets, 2)
                    transfer_reps = min(reps, 2 if kind == "power" else 6)
                    tags = _slot_mechanical_risk_tags(slot)
                    # CNS-heavy and systemic-fatigue work is rejected in the
                    # shared eligibility rule, even if one cost field says low.
                    regions = {
                        region.value for prefix, region in _REGION_TAG_PREFIXES
                        if any(tag.startswith(prefix) for tag in tags)
                    }
                    candidates.append({
                        "member": member, "slot": slot, "index": index,
                        "kind": kind, "regions": regions,
                        "dose": f"{transfer_sets} x {transfer_reps}",
                        "reps": transfer_sets * transfer_reps,
                    })

            chosen = None
            candidate_score = None
            primary_goal = str(athlete_model.get("primary_goal") or "").strip().lower()
            # Prefer three only when enough main work remains, otherwise a
            # paired transfer. Shared region is a preference, never permission
            # to stack high-risk lower-body loading.
            for n in (3, 2):
                if len(memberships) - n < 3:
                    continue
                for group in combinations(candidates, n):
                    moved_ids = {id(item["member"]) for item in group}
                    remaining = [m for m in memberships if id(m) not in moved_ids]
                    if not any(
                        _role_kind(
                            _assignment_strength_slot(
                                candidate_pools,
                                str(m.get("source_phase") or "").strip().upper(),
                                m,
                            ) or {}
                        ) in {"anchor", "hybrid"}
                        for m in remaining if m.get("slot_group") == "strength_slots"
                    ):
                        continue
                    if primary_goal:
                        original_goal = [
                            m for m in memberships if m.get("slot_group") == "strength_slots"
                            and primary_goal in slot_adaptations(
                                _assignment_strength_slot(
                                    candidate_pools,
                                    str(m.get("source_phase") or "").strip().upper(), m,
                                ) or {}
                            )
                        ]
                        if original_goal and not any(id(m) not in moved_ids for m in original_goal):
                            continue
                    # Multiple exercises consume one shared recovery envelope,
                    # not an independent two-set allowance each.
                    if sum(item["reps"] for item in group) > 24:
                        continue
                    simulated = []
                    doses = {}
                    effort = {}
                    for item in group:
                        m = dict(item["member"])
                        simulated.append(m)
                        key = (str(m.get("slot_id") or ""), str(m.get("name") or ""))
                        doses[key] = item["dose"]
                        effort[key] = 6
                    stress = realised_session_stress(
                        simulated, resolved_strength=doses, resolved_strength_effort=effort
                    )
                    if (
                        stress is None
                        or stress.systemic > StressLevel.LOW
                        or stress.neural_mechanical > StressLevel.MODERATE
                    ):
                        continue
                    shared = max((
                        sum(region in item["regions"] for item in group)
                        for region in {"lower", "upper", "trunk"}
                    ), default=0)
                    score = (
                        shared if shared >= 2 else 0,
                        n,
                        -sum(item["reps"] for item in group),
                        -sum(item["index"] for item in group),
                    )
                    if chosen is None or score > candidate_score:
                        chosen, candidate_score = list(group), score

            if chosen is None:
                suppress_unfunded_power_microdose(
                    week, receiver, athlete_model=athlete_model,
                    reason_code="no_safe_two_exercise_redistribution",
                )
                continue

            moved = {id(item["member"]) for item in chosen}
            original_source_assignments = [dict(item) for item in memberships]
            source["selected_exercise_assignments"] = [
                item for item in memberships if id(item) not in moved
            ]
            receiver["selected_exercise_assignments"] = [
                {
                    **item["member"],
                    "redistributed_from_role_key": source.get("role_key"),
                    "redistribution_reason": "pre_hard_session_rebalance",
                }
                for item in chosen
            ]
            # The exact bounded doses evaluated above are authoritative inputs
            # to the existing prescription resolver. No extra membership is
            # generated and no full bank prescription leaks into Monday.
            for assignment, item in zip(receiver["selected_exercise_assignments"], chosen):
                assignment["redistributed_dose_ceiling"] = item["dose"]
            for role in (source, receiver):
                policy = role.get("strength_composition_policy")
                if isinstance(policy, dict):
                    selected = role["selected_exercise_assignments"]
                    policy["selected_count"] = len(selected)
                    policy["selected_names"] = [item.get("name") for item in selected]
                role.pop("effective_strength_prescriptions", None)
                role.pop("effective_strength_envelope", None)

            week["pre_hard_power_redistribution"] = {
                "status": "transferred",
                "reason_code": "dense_day_rebalanced_with_bounded_cost",
                "source_day": source.get("scheduled_day_hint"),
                "target_day": receiver.get("scheduled_day_hint"),
                "exercise_names": [item["member"].get("name") for item in chosen],
                "source_members_before": len(memberships),
                "source_members_after": len(source["selected_exercise_assignments"]),
                "recipient_members": len(chosen),
                "weekly_exercise_count_delta": 0,
                "dose_ceiling_sets": 2,
                # Snapshot for conservation if final realised-load revalidation
                # relocates or suppresses the proposed receiving appointment.
                "source_strength_session_index": source.get("strength_session_index"),
                "receiver_strength_session_index": receiver.get("strength_session_index"),
                "receiver_original_d_day": receiver.get("scheduled_d_day"),
                "receiver_token": receiver.get("dense_day_receiver_token"),
                "original_source_assignments": original_source_assignments,
            }
    return weekly_role_map


def rollback_invalid_dense_day_redistributions(
    weekly_role_map: dict[str, Any],
    *, athlete_model: dict[str, Any],
) -> bool:
    """Restore donor work if canonical calendar verification rejects its receiver.

    This checks membership conservation *after* the existing realised-load
    governor. It does not add any new placement or load-veto policy.
    """
    from .calendar_context import role_d_day
    from .pre_hard_contact_strength import suppress_unfunded_power_microdose

    restored = False
    for week in weekly_role_map.get("weeks") or []:
        if not isinstance(week, dict):
            continue
        audit = week.get("pre_hard_power_redistribution") or {}
        if audit.get("status") != "transferred":
            continue
        roles = week.get("session_roles") or []
        source = next((
            role for role in roles
            if role.get("category") == "strength"
            and role.get("strength_session_index") == audit.get("source_strength_session_index")
        ), None)
        receiver_location = next((
            (located_week, role)
            for located_week in weekly_role_map.get("weeks") or []
            for role in located_week.get("session_roles") or []
            if role.get("dense_day_receiver_provisional")
            and role.get("dense_day_receiver_token") == audit.get("receiver_token")
        ), None)
        receiver_week, receiver = receiver_location if receiver_location else (None, None)
        expected_names = set(audit.get("exercise_names") or [])
        received_names = {
            str(item.get("name") or "")
            for item in (receiver or {}).get("selected_exercise_assignments") or []
            if isinstance(item, dict)
        }
        valid = (
            source is not None and receiver is not None
            and receiver_week is week
            and role_d_day(receiver_week, receiver) == audit.get("receiver_original_d_day")
            and received_names == expected_names
            and 2 <= len(receiver.get("selected_exercise_assignments") or []) <= 3
        )
        if valid:
            continue
        if source is not None:
            source["selected_exercise_assignments"] = [
                dict(item) for item in audit.get("original_source_assignments") or []
            ]
            policy = source.get("strength_composition_policy")
            if isinstance(policy, dict):
                policy["selected_count"] = len(source["selected_exercise_assignments"])
                policy["selected_names"] = [
                    item.get("name") for item in source["selected_exercise_assignments"]
                ]
            source.pop("effective_strength_prescriptions", None)
            source.pop("effective_strength_envelope", None)
        if receiver is not None and receiver_week is not None:
            suppress_unfunded_power_microdose(
                receiver_week, receiver, athlete_model=athlete_model,
                reason_code="redistribution_failed_final_calendar_review",
            )
        week["pre_hard_power_redistribution"] = {
            "status": "rolled_back",
            "reason_code": "redistribution_failed_final_calendar_review",
            "weekly_exercise_count_delta": 0,
        }
        restored = True
    return restored


def compose_normal_rehab_assignments(
    *, weekly_role_map: dict[str, Any], candidate_pools: dict[str, Any]
) -> dict[str, Any]:
    """Attach selected Stage 1 rehab drills to normal rehab/recovery roles."""
    for week in weekly_role_map.get("weeks", []) or []:
        if not isinstance(week, dict):
            continue

        phase = str(week.get("phase") or "").strip().upper()
        pool = candidate_pools.get(phase) if isinstance(candidate_pools, dict) else None
        rehab_slots = pool.get("rehab_slots", []) if isinstance(pool, dict) else []
        if not rehab_slots:
            continue

        remaining_slots = [slot for slot in rehab_slots if isinstance(slot, dict)]

        for role in week.get("session_roles", []) or []:
            if not isinstance(role, dict):
                continue

            role_key = str(role.get("role_key") or "").strip().lower()
            category = str(role.get("category") or "").strip().lower()
            preferred_pool = str(role.get("preferred_pool") or "").strip().lower()

            is_rehab_role = role_key == "mobility_rehab" or (
                category == "recovery"
                and preferred_pool == "rehab_slots_or_recovery_only"
            )

            if role.get("late_fight_tail_owned") or not is_rehab_role:
                continue

            owned_slots = _bounded_rehab_slots(remaining_slots)
            assignments = [
                assignment
                for slot in owned_slots
                if (assignment := _rehab_assignment_from_slot(phase, slot)) is not None
            ]

            role["selected_exercise_assignments"] = assignments

            used = {id(slot) for slot in owned_slots}
            remaining_slots = [
                slot for slot in remaining_slots if id(slot) not in used
            ]

    return weekly_role_map


def attach_late_fight_assignments(
    roles: list[dict[str, Any]], assignments_by_day: dict[str, list[dict[str, Any]]]
) -> None:
    """Attach the shared late-fight selector result to its scheduled roles."""
    for role in roles:
        if not isinstance(role, dict):
            continue
        label = str(role.get("scheduled_countdown_label") or role.get("countdown_label") or "").strip()
        selected = assignments_by_day.get(label, [])
        role["selected_exercise_assignments"] = [
            stamp_exercise_key(
                {**assignment, "source_phase": assignment.get("source_phase") or assignment.get("phase")}
            )
            for assignment in selected
            if assignment.get("role_key") == role.get("role_key")
        ]


def consolidate_compatible_physical_sessions(
    weekly_role_map: dict[str, Any], *, athlete_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose eligible short speed or footwork doses into an existing Strength workout.

    Works on closed, fully prescribed normal-camp sessions only. Each source
    member retains its exact Stage 1 identity and dose; its original standalone
    role is removed only when a trial of the combined calendar is no worse
    under the canonical combat-load policy. This is not appointment relabelling,
    a new role selector, or a workout-frequency veto.
    """
    from copy import deepcopy

    from .calendar_context import (
        build_events, classify_role, contact_d_day, role_d_day, week_scope,
    )
    from .combat_load_policy import evaluate_candidate_at_position, placement_rank
    from .physical_session_frequency import (
        audit_physical_session_frequency, cadence_windows, is_physical_session_role,
    )

    if not isinstance(weekly_role_map, dict):
        return weekly_role_map

    # Compare role identities in each trial rather than Python ids (deepcopies
    # deliberately change ids). Each week has planner-assigned session indices.
    def _grades(map_: dict[str, Any]) -> dict[tuple[int, int, str], int]:
        grades = {}
        for ordinal, week in enumerate(map_.get("weeks") or []):
            for role in week.get("session_roles") or []:
                if not is_physical_session_role(role):
                    continue
                d_day = role_d_day(week, role)
                if not isinstance(d_day, int) or d_day <= 13 or role.get("late_fight_tail_owned"):
                    continue
                profile = classify_role(role)
                if profile is None:
                    continue
                decision = evaluate_candidate_at_position(
                    profile, candidate_position=-d_day,
                    events=build_events(map_, exclude_role=role),
                    candidate_scope=week_scope(week, ordinal + 1),
                )
                grades[(ordinal, int(role.get("session_index") or 0),
                        str(role.get("role_key") or ""))] = placement_rank(decision)
        return grades

    slots = [
        (day["d_day"], day.get("weekday", ""))
        for week in weekly_role_map.get("weeks") or []
        for day in week.get("calendar_days") or []
        if isinstance(day, dict) and isinstance(day.get("d_day"), int)
    ]
    cadence_sets = [{day for day, _ in days} for days in cadence_windows(slots)]

    for week_index, week in enumerate(weekly_role_map.get("weeks") or []):
        if str(week.get("phase") or "").upper() not in {"GPP", "SPP"}:
            continue
        for source in list(week.get("session_roles") or []):
            if (source.get("category") != "conditioning"
                    or source.get("coach_owned")
                    or source.get("late_fight_tail_owned")
                    or source.get("goal_preservation_repair")):
                continue
            source_day = role_d_day(week, source)
            members = source.get("selected_exercise_assignments")
            if not isinstance(source_day, int) or source_day <= 13 or not isinstance(members, list) or len(members) != 1:
                continue
            member = members[0]
            if not isinstance(member, dict) or member.get("slot_group") != "conditioning_slots":
                continue
            work, rounds, rest = (
                member.get("work_sec"), member.get("effective_rounds") or member.get("rounds"),
                member.get("rest_sec"),
            )
            try:
                work, rounds, rest = float(work), int(rounds), float(rest)
            except (TypeError, ValueError):
                continue
            slot_id = str(member.get("slot_id") or "").lower()
            source_system = str(source.get("preferred_system") or "").lower()
            try:
                effort = float(member.get("rpe") or 0)
            except (TypeError, ValueError):
                effort = 0
            if source_system == "alactic":
                # Short full-rest speed / reactive-power / coordination work.
                # Never treat long fight-pace rounds as an accessory.
                kind = "speed"
                if not (0 < work <= 6 and 2 <= rounds <= 8
                        and work * rounds <= 40 and rest >= 60):
                    continue
            elif ("_technical_footwork_" in slot_id
                  or source_system == "technical_footwork"):
                # The dedicated technical bank is distinct from aerobic
                # footwork conditioning. Require a truly small, low-effort
                # prescribed technical exposure before embedding it.
                kind = "footwork"
                if not (0 < work <= 90 and 1 <= rounds <= 3
                        and work * rounds <= 270 and 30 <= rest <= 120
                        and 0 < effort <= 5):
                    continue
            else:
                continue
            if not str(member.get("effective_prescription") or "").strip():
                continue

            hosts = []
            for host in week.get("session_roles") or []:
                if (host is source or host.get("category") != "strength"
                        or host.get("coach_owned") or host.get("late_fight_tail_owned")):
                    continue
                host_day = role_d_day(week, host)
                policy = host.get("strength_composition_policy")
                host_members = host.get("selected_exercise_assignments")
                if (not isinstance(host_day, int) or host_day <= 13
                        or abs(host_day - source_day) > 3
                        or not any(source_day in days and host_day in days for days in cadence_sets)
                        or not isinstance(policy, dict) or int(policy.get("pressure") or 0) != 0
                        or not isinstance(host_members, list) or not host_members
                        or not host.get("effective_strength_prescriptions")):
                    continue
                cap = policy.get("effective_exercise_cap")
                if not isinstance(cap, int) or len(host_members) + 1 > cap:
                    continue
                if any(m.get("slot_group") != "strength_slots" for m in host_members):
                    continue
                if any((m.get("exercise_key") or m.get("name")) == (member.get("exercise_key") or member.get("name")) for m in host_members):
                    continue
                # The host's explicit exercise allow-list must not hide the
                # transferred source member from final goal evidence.
                preferred = host.get("preferred_exercise_names") or []
                if preferred and member.get("name") not in preferred:
                    continue
                # Never move a physical addition onto the day immediately
                # before or after hard sparring. Resolved contact stays authoritative.
                hard_days = [
                    contact_d_day(week, item)
                    for item in week.get("hard_sparring_plan") or []
                    if str(item.get("effective_load") or "").lower() == "hard"
                ]
                if any(isinstance(day, int) and abs(day - host_day) <= 1 for day in hard_days):
                    continue
                hosts.append((host_day != source_day, abs(host_day - source_day), len(host_members), host))
            hosts.sort(key=lambda item: item[:3])

            for _different_day, _distance, _size, host in hosts:
                trial = deepcopy(weekly_role_map)
                trial_week = trial["weeks"][week_index]
                roles = trial_week["session_roles"]
                original_index = next((i for i, r in enumerate(week["session_roles"]) if r is source), None)
                host_index = next((i for i, r in enumerate(week["session_roles"]) if r is host), None)
                if original_index is None or host_index is None:
                    continue
                trial_source, trial_host = roles[original_index], roles[host_index]
                carried = deepcopy(trial_source["selected_exercise_assignments"][0])
                carried["consolidated_from_role_key"] = trial_source.get("role_key")
                carried["consolidated_from_system"] = (
                    "technical_footwork" if kind == "footwork" else "alactic"
                )
                # Technical footwork / speed primer first; unchanged Strength doses follow.
                trial_host["selected_exercise_assignments"] = [
                    carried, *trial_host["selected_exercise_assignments"]
                ]
                composition = trial_host["strength_composition_policy"]
                composition["selected_count"] = len(trial_host["selected_exercise_assignments"])
                composition["selected_names"] = [
                    item["name"] for item in trial_host["selected_exercise_assignments"]
                ]
                trial_host["athlete_facing_label"] = (
                    "Strength + footwork" if kind == "footwork" else "Strength + speed"
                )
                trial_host.setdefault("consolidated_session_roles", []).append({
                    "role_key": trial_source.get("role_key"),
                    "source_d_day": source_day,
                    "slot_id": carried.get("slot_id"),
                })
                roles.pop(original_index)
                stamp = realised_role_stress(trial_host)
                if stamp is None:
                    continue
                trial_host["calendar_stress"] = _stress_stamp(stamp)
                baseline_grades = _grades(weekly_role_map)
                proposed_grades = _grades(trial)
                # No new policy forbids, and no previously retained physical
                # appointment becomes more deprioritized due to consolidation.
                if any(grade == 2 or grade > baseline_grades.get(key, grade)
                       for key, grade in proposed_grades.items()):
                    continue
                weekly_role_map["weeks"] = trial["weeks"]
                weekly_role_map.setdefault("session_consolidation_audit", []).append({
                    "week_index": week.get("week_index"),
                    "source_d_day": source_day,
                    "host_d_day": role_d_day(trial_week, trial_host),
                    "source_role_key": source.get("role_key"),
                    "host_role_key": host.get("role_key"),
                    "exercise": carried.get("name"),
                    "embedded_quality": kind,
                    "decision": "combined_preserving_dose",
                })
                if isinstance(weekly_role_map.get("physical_session_frequency_audit"), dict):
                    weekly_role_map["physical_session_frequency_audit"]["warnings"] = (
                        audit_physical_session_frequency(weekly_role_map, athlete_model or {})
                    )
                break
            # The accepted trial replaced this week's object. Do not iterate
            # stale role identities; a later consolidation gets a fresh map.
            if weekly_role_map["weeks"][week_index] is not week:
                break
    return weekly_role_map


EMPTY_PHYSICAL_SESSION_REASON = "no_eligible_exercise_membership"
_MEMBERSHIP_REQUIRED_CATEGORIES = frozenset({"strength", "conditioning"})


def _is_unfilled_physical_role(role: Any) -> bool:
    return (
        isinstance(role, dict)
        and str(role.get("category") or "").strip().lower() in _MEMBERSHIP_REQUIRED_CATEGORIES
        and not role.get("selected_exercise_assignments")
    )


LATE_LOAD_CONFLICT_REASON = "late_load_conflict_no_safe_prescription"


def _unfilled_role_suppression(role: dict[str, Any], d_day: int | None) -> dict[str, Any]:
    resolution = role.get("late_load_resolution")
    if isinstance(resolution, dict) and resolution.get("action") == "suppressed_no_safe_prescription":
        # The membership owner emptied this session on purpose: its fixed day is
        # forbidden at any composed load and no coexisting microdose was legal.
        return {
            "category": role.get("category"),
            "role_key": role.get("role_key"),
            "preferred_system": role.get("preferred_system", ""),
            "strength_session_index": role.get("strength_session_index"),
            "scheduled_day_hint": role.get("scheduled_day_hint"),
            "original_d_day": d_day,
            "reason_code": LATE_LOAD_CONFLICT_REASON,
            "reason_codes": [LATE_LOAD_CONFLICT_REASON, str(resolution.get("conflict_reason_code") or "")],
            "reason": "The scheduled day forbids this session at any composed load and no coexisting "
                      "microdose was legal, so it is removed rather than shipped unsafe.",
            "authority": "late_fight_assignment_builder",
        }
    return {
        "category": role.get("category"),
        "role_key": role.get("role_key"),
        "preferred_system": role.get("preferred_system", ""),
        "strength_session_index": role.get("strength_session_index"),
        "scheduled_day_hint": role.get("scheduled_day_hint"),
        "original_d_day": d_day,
        "reason_code": EMPTY_PHYSICAL_SESSION_REASON,
        "reason_codes": [EMPTY_PHYSICAL_SESSION_REASON],
        "reason": "No eligible bank exercise survived selection for this session, so it is removed rather than shipped empty or filled.",
        "authority": "session_composition",
    }


def suppress_unfilled_physical_roles(brief: dict[str, Any]) -> dict[str, Any]:
    """Remove strength/conditioning sessions that composition left with no member.

    Runs on the finished Stage 1 brief, once every membership owner
    (composition, late-fight selector, dose resolution, goal preservation) has
    finished; empty roles never carried goal evidence, so coverage is unchanged.
    A session with nothing eligible is recorded as a suppression with its
    reason; it is never filled here, never left for Stage 2 to fill, and never
    counted as delivered training. Recovery, sparring and zero-load support
    roles are untouched.
    """
    from .physical_session_frequency import (
        audit_physical_session_frequency,
        is_physical_session_role,
        role_countdown_offset,
        unused_day_record,
    )

    def _split(roles: list[Any], week: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Suppression rows, plus an unused-day record for every day left empty."""
        removed = [role for role in roles if _is_unfilled_physical_role(role)]
        roles[:] = [role for role in roles if not _is_unfilled_physical_role(role)]
        occupied = {role_countdown_offset(role) for role in roles if is_physical_session_role(role)}
        rows, unused = [], []
        for role in removed:
            d_day = role_d_day(week, role)
            rows.append(_unfilled_role_suppression(role, d_day))
            weekday = role.get("scheduled_day_hint") or role.get("real_weekday") or role.get("countdown_weekday")
            if isinstance(d_day, int) and weekday and d_day not in occupied:
                occupied.add(d_day)
                unused.append(unused_day_record(weekday, d_day, EMPTY_PHYSICAL_SESSION_REASON))
        return rows, unused

    role_map = brief.get("weekly_role_map") or {}
    sequence = brief.get("late_fight_session_sequence")
    if isinstance(sequence, list):
        # A late-fight brief's calendar authority is its session sequence; its
        # weekly_role_map is an earlier allocation the selector never fills.
        if not any(_is_unfilled_physical_role(role) for role in sequence):
            return brief
        from .stage2_payload import _record_late_fight_unused_days, late_fight_sequence_budget

        rows, unused = _split(sequence, {})
        spec = brief["late_fight_plan_spec"]
        spec["suppressed_roles"] = [*(spec.get("suppressed_roles") or []), *rows]
        # The spec's counts and caps describe the sequence; recount them from
        # the same owner the builder uses.
        spec.update(late_fight_sequence_budget(sequence, spec.get("role_budget")))
        _record_late_fight_unused_days(role_map, unused)
        return brief

    from .camp_week_fillers_impl import _merge_unused_day_records

    for week in role_map.get("weeks") or []:
        roles = week.get("session_roles") if isinstance(week, dict) else None
        if not isinstance(roles, list) or not any(_is_unfilled_physical_role(role) for role in roles):
            continue
        rows, unused = _split(roles, week)
        week.setdefault("suppressed_roles", []).extend(rows)
        _merge_unused_day_records(week, unused)
    if isinstance(role_map.get("physical_session_frequency_audit"), dict):
        # The frequency audit ran inside calendar integrity, before membership
        # existed; recount so a removed session is never reported as trained.
        role_map["physical_session_frequency_audit"]["warnings"] = audit_physical_session_frequency(
            role_map, brief.get("athlete_snapshot") or {}
        )
    return brief
