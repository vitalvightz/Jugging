from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from .calendar_context import (
    CalendarLegalityView,
    resolved_contact_offsets,
    sequence_legality,
)
from .camp_phases import calculate_phase_weeks
from .combat_load_policy import PlacementDirective, role_load_profile
from .normalization import WEEKDAY_ORDER, clean_list, normalize_fatigue_level
from .priority_profile import selected_priority_targets
from .conditioning import (
    TECHNICAL_FOOTWORK_GROUP,
    select_technical_footwork_candidates,
    technical_footwork_prescription_fields,
)
from .sports import normalize_sport, planning_format
from .exercise_identity import support_insert_exercise_key
from .late_selector_windows import classify_late_selector_window
from .restriction_filtering import evaluate_restriction_impact
from .stage2_render_guards import _all_active_injuries_surface_only
from .tagging import normalize_tag
from .stage2_payload_late_fight import (
    _countdown_offset,
    _countdown_weekday_map,
    _resolve_plan_creation_weekday,
    can_render_late_taper_day,
    is_low_cost_coexistable_filler,
    resolve_late_fight_contacts,
)
from .fight_visualization_library import (
    ATHLETE_FACING_LABEL as FIGHT_VISUALIZATION_LABEL,
    CAMP_VISUALIZATION_FIRST_DAY,
    CAMP_VISUALIZATION_LAST_DAY,
    ROLE_KEY as FIGHT_VISUALIZATION_ROLE_KEY,
    build_visualization_display_text,
    camp_visualization_role,
    choose_camp_visualization_days,
    select_for_athlete,
    visualization_metadata,
)
from .mental_training_policy import (
    MENTAL_ROLE_KEYS,
    VISUALISATION_MAX_DURATION_MIN,
    calendar_week_bucket,
    govern_mental_roles,
    mental_training_policy,
)
from .tactical_watch_library import (
    TacticalWatchBankExhausted,
    build_watch_display_text,
    extract_tactical_style,
    select_tactical_watch,
    watch_metadata,
)


ZERO_COST_INSERTS = {
    "tactical_watch",
    "fight_visualization",
    "tactical_cue_card",
    "self_review",
    "neural_visualization",
}

LOW_COST_RECOVERY_INSERTS = {
    "recovery_reset",
    "breathing_reset",
    "sleep_downshift",
}

PHYSICAL_INSERTS = {
    "mobility_rehab",
    "movement_quality",
    "technical_shadow_rhythm",
    "footwork_walkthrough",
    "joint_prep",
    "walk_flush",
}

# Genuinely low-risk aerobic-maintenance inserts (RPE 3-4, no soreness target).
# These keep a selected conditioning / gas-tank goal *visible* late in camp when
# the higher-cost physical inserts are correctly stripped for freshness. They are
# bodyweight-only, so they do not depend on bike/rower style equipment, and they
# are only offered when the athlete actually selected a conditioning signal.
LOW_COST_AEROBIC_INSERTS = {
    "aerobic_shadow_flow",
    "aerobic_walk_flush",
    "aerobic_footwork_rhythm",
    "aerobic_skip_flush",
    "aerobic_jog_flush",
}
# Zero/low-impact options that stay safe even with a lower-leg injury or high fatigue.
_ZERO_IMPACT_AEROBIC_INSERTS = {"aerobic_shadow_flow", "aerobic_walk_flush"}
# Higher-impact options gated behind healthy lower legs and low fatigue.
_IMPACT_AEROBIC_INSERTS = {"aerobic_skip_flush", "aerobic_jog_flush"}

GAP_FILL_MIN_DAYS = 3
TWO_INSERT_GAP_MIN_DAYS = 5
# Widest countdown at which the opening days of the window are eligible for a
# gap-fill insert. Inside the taper the session budget is small enough that the
# plan can start days after the athlete does; further out the sessions already
# reach the front of the window on their own.
LEADING_SPAN_MAX_DAYS_UNTIL_FIGHT = 7
MAX_INSERTS_TOTAL_D21_TO_D0 = 6
MAX_PHYSICAL_INSERTS_PER_7_DAY_SEGMENT = 1
# Day-before-fight slots stay restricted to zero/recovery work regardless of goal.
MIN_AEROBIC_MAINTENANCE_OFFSET = 2

_ALL_INSERTS = (
    ZERO_COST_INSERTS
    | LOW_COST_RECOVERY_INSERTS
    | PHYSICAL_INSERTS
    | LOW_COST_AEROBIC_INSERTS
)
TACTICAL_INSERTS = {"tactical_watch", "tactical_cue_card", "self_review"}


def consumes_insert_budget(role_key: Any) -> bool:
    """Whether an insert spends the camp's support-insert allowance.

    Zero-cost inserts (Tactical Watch, cue card, self review, visualization) are
    informational: they carry no physical load, coexist with real sessions and
    do not fill a training day. Counting them against the allowance let a week
    of tactical reading crowd out the physical support the athlete's goals need.
    """
    return str(role_key or "") not in ZERO_COST_INSERTS


def budgeted_insert_count(inserts: list[dict[str, Any]]) -> int:
    return sum(
        1
        for insert in inserts
        if isinstance(insert, dict) and consumes_insert_budget(insert.get("role_key"))
    )
CONDITIONING_MAINTENANCE_INSERTS = LOW_COST_AEROBIC_INSERTS

_LOWER_LEG_LOAD_MARKERS = (
    "achilles",
    "calf",
    "calves",
    "shin",
    "ankle",
    "foot",
    "feet",
    "heel",
    "plantar",
    "knee",
    "lower leg",
    "hamstring",
    "tibia",
    "peroneal",
)


@dataclass(frozen=True)
class TargetCoverageState:
    """Coverage state for one canonical athlete-selected target.

    A selected priority is not automatically a remaining training need. Full
    programme roles can satisfy meaningful coverage; low-cost fillers can only
    satisfy the narrower support opportunity represented by ``remaining_need``.
    """

    target: str
    label: str
    priority_weight: float
    sources: tuple[str, ...]
    meaningful_coverage: bool
    support_coverage: float
    remaining_need: float
    low_cost_addressable: bool


# Canonicalize only structured athlete-priority values. These aliases never run
# over display copy, titles, descriptions, or renderer prose.
_TARGET_ALIASES = {
    "footwork": "footwork",
    "feet": "footwork",
    "stance": "footwork",
    "ringcraft": "footwork",
    "ring_craft": "footwork",
    "angles": "footwork",
    "speed": "speed",
    "explosiveness": "power",
    "explosive_power": "power",
    "power": "power",
    "strength": "strength",
    "max_strength": "strength",
    "conditioning": "conditioning",
    "gas": "conditioning",
    "gas_tank": "conditioning",
    "aerobic": "conditioning",
    "endurance": "conditioning",
    "cardio": "conditioning",
    "work_capacity": "conditioning",
    "engine": "conditioning",
    "stamina": "conditioning",
    "mobility": "mobility",
    "range_of_motion": "mobility",
    "coordination": "coordination",
    "coordination_proprioception": "coordination",
    "proprioception": "coordination",
    "balance": "coordination",
    "recovery": "recovery",
}

# Intentionally small role-identity map. A role receives meaningful target
# credit only where its canonical identity has a reliable semantic basis. Generic
# conditioning role keys are deliberately absent: their real adaptation comes
# from the structured preferred_system stamped by the planner.
_MEANINGFUL_ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "primary_strength": frozenset({"strength"}),
    "primary_strength_day": frozenset({"strength"}),
    "secondary_strength_day": frozenset({"strength"}),
    "structural_strength_day": frozenset({"strength"}),
    "strength_touch_day": frozenset({"strength"}),
    "transfer_strength_day": frozenset({"strength", "power"}),
    "small_strength_touch_day": frozenset({"strength"}),
    "neural_plus_strength_day": frozenset({"strength", "power", "speed"}),
    "neural_primer_day": frozenset({"power", "speed"}),
    "alactic_speed_day": frozenset({"speed"}),
    "alactic_sharpness_day": frozenset({"speed"}),
    "alactic_support_day": frozenset({"speed"}),
    "aerobic_base_day": frozenset({"conditioning"}),
    "aerobic_support_day": frozenset({"conditioning"}),
    "aerobic_flush_day": frozenset({"conditioning"}),
    "repeatability_support_day": frozenset({"conditioning"}),
    "controlled_repeatability_day": frozenset({"conditioning"}),
    "fight_pace_repeatability_day": frozenset({"conditioning"}),
    "light_fight_pace_touch_day": frozenset({"conditioning"}),
    "converted_low_aerobic_gas_tank_day": frozenset({"conditioning"}),
    "recovery_aerobic_gas_tank_day": frozenset({"conditioning", "recovery"}),
    "aerobic_coordination_day": frozenset({"conditioning", "coordination"}),
    "alactic_coordination_day": frozenset({"speed", "coordination"}),
    "converted_mobility_support_day": frozenset({"mobility"}),
    "recovery_reset_day": frozenset({"recovery"}),
    "tissue_recovery_day": frozenset({"recovery"}),
    "converted_recovery_flush_day": frozenset({"recovery"}),
    "fight_week_freshness_day": frozenset({"recovery", "mobility"}),
}

# Capability means honest low-cost support, not a full adaptation. Strength and
# power are deliberately absent: cheap filler must never claim to repair them.
_FILLER_TARGET_CAPABILITY: dict[str, dict[str, float]] = {
    "footwork_walkthrough": {"footwork": 1.0, "coordination": 0.55},
    "technical_shadow_rhythm": {"footwork": 0.7, "coordination": 0.6, "speed": 0.35},
    "aerobic_footwork_rhythm": {"conditioning": 0.75, "footwork": 0.6, "coordination": 0.5},
    "movement_quality": {"coordination": 1.0, "mobility": 0.55, "footwork": 0.35},
    "mobility_rehab": {"mobility": 1.0},
    "joint_prep": {"mobility": 0.75},
    "aerobic_shadow_flow": {"conditioning": 1.0, "speed": 0.2},
    "aerobic_walk_flush": {"conditioning": 0.8, "recovery": 0.45},
    "aerobic_skip_flush": {"conditioning": 0.9, "coordination": 0.35},
    "aerobic_jog_flush": {"conditioning": 0.9},
    "recovery_reset": {"recovery": 1.0, "mobility": 0.35},
    "breathing_reset": {"recovery": 0.7},
    "sleep_downshift": {"recovery": 0.7},
    "walk_flush": {"recovery": 0.55},
    "neural_visualization": {"speed": 0.2},
    # Mental rehearsal only: it trains no physical capability, so it claims
    # none of the athlete's goal-coverage ledger.
    "fight_visualization": {},
    # Normal-camp's established banked coordination role uses this same ledger.
    "coordination_support": {"coordination": 1.0},
}

_STRUCTURED_TARGET_FIELDS = (
    "coverage_targets",
    "selected_targets",
    "matched_goal_tags",
    "matched_weakness_tags",
    "goal_tags",
    "weakness_tags",
)

_INSERT_META = {
    "tactical_watch": {
        "label": "Tactical Focus",
        "duration_min": [8, 12],
        "rpe_max": 1,
        "insert_category": "tactical",
        "repeat_allowed": False,
    },
    "tactical_cue_card": {
        "label": "Tactical Cue Card",
        "duration_min": [5, 8],
        "rpe_max": 1,
        "insert_category": "tactical",
        "repeat_allowed": False,
        "display_text": "Write one fight cue only: entry, exit, counter, foot position, or guard reaction. Keep it short enough to recall under pressure.",
    },
    "self_review": {
        "label": "Self-Review Cues",
        "duration_min": [8, 12],
        "rpe_max": 1,
        "insert_category": "tactical",
        "repeat_allowed": False,
        "display_text": "Review the last clean technical work. Write three cues only: one entry, one defensive reset, one composure cue.",
    },
    "fight_visualization": {
        "label": FIGHT_VISUALIZATION_LABEL,
        # Widest span in the bank (D-7). The selected entry's own duration
        # overwrites this when the role is stamped.
        "duration_min": [1, 8],
        "rpe_max": 1,
        "insert_category": "mental",
        "repeat_allowed": True,
    },
    "neural_visualization": {
        "label": "Neural Visualization",
        "duration_min": [5, 8],
        "rpe_max": 1,
        "insert_category": "mental",
        "repeat_allowed": False,
        "display_text": "Quiet visualization only. Rehearse first exchange, best entry, exit/reset, and final-round composure.",
    },
    "recovery_reset": {
        "label": "Recovery Reset",
        "duration_min": [10, 20],
        "rpe_max": 2,
        "insert_category": "recovery",
        "repeat_allowed": False,
        "display_text": "Breathing reset, easy tissue work, and downshift mobility. Keep it restorative.",
    },
    "breathing_reset": {
        "label": "Breathing Reset",
        "duration_min": [3, 6],
        "rpe_max": 1,
        "insert_category": "recovery",
        "repeat_allowed": False,
        "display_text": "Nasal breathing if comfortable. Use a 4 second inhale and 6 second exhale. Finish calmer than you started.",
    },
    "sleep_downshift": {
        "label": "Sleep Downshift",
        "duration_min": [5, 10],
        "rpe_max": 1,
        "insert_category": "recovery",
        "repeat_allowed": False,
        "display_text": "Lights down, phone away, easy breathing, then stretch two tight areas without chasing range.",
    },
    "mobility_rehab": {
        "label": "Mobility/Rehab Reset",
        "duration_min": [8, 15],
        "rpe_max": 2,
        "insert_category": "mobility",
        "repeat_allowed": False,
        "display_text": "Target the flagged restriction with easy range, activation, and pain-free control. Stop well before fatigue.",
    },
    "movement_quality": {
        "label": "Movement Quality Check",
        "duration_min": [8, 15],
        "rpe_max": 2,
        "insert_category": "movement_quality",
        "repeat_allowed": False,
        "display_text": "Low-amplitude stance, posture, breathing, and foot placement quality. No sweat target.",
    },
    "technical_shadow_rhythm": {
        "label": "Technical Shadow Rhythm",
        "duration_min": [8, 15],
        "rpe_max": 3,
        "insert_category": "technical",
        "repeat_allowed": False,
        "display_text": "Light shadow rhythm only. Smooth entries, exits, and reset cues. No bag, bands, bursts, or conditioning intent.",
    },
    "footwork_walkthrough": {
        "label": "Footwork Walkthrough",
        "duration_min": [8, 12],
        "rpe_max": 2,
        "insert_category": "technical_footwork",
        "repeat_allowed": False,
        "display_text": "Controlled stance movement, direction changes, pivots and position resets. Keep the movement slow and technical with no fatigue target.",
    },
    "joint_prep": {
        "label": "Joint Prep",
        "duration_min": [6, 8],
        "rpe_max": 1,
        "insert_category": "mobility",
        "repeat_allowed": False,
        "display_text": "Neck CARs, shoulder CARs, wrist circles, hip circles, and ankle rocks. Stay smooth and pain-free.",
    },
    "walk_flush": {
        "label": "Easy Walk Flush",
        "duration_min": [10, 20],
        "rpe_max": 2,
        "insert_category": "recovery_walk",
        "repeat_allowed": False,
        "display_text": "Nose-breathing pace only. No sweat target. Finish feeling better than when you started.",
    },
    "aerobic_shadow_flow": {
        "label": "Aerobic Movement Flow",
        "duration_min": [8, 12],
        "rpe_max": 4,
        "insert_category": "conditioning_maintenance",
        "repeat_allowed": False,
        "display_text": "4 x 2 min easy solo movement rounds, 60 sec rest. Use smooth sport-specific movement at RPE 3. No contact, no power and no impact. Keep the gas tank ticking over without costing freshness.",
    },
    "aerobic_walk_flush": {
        "label": "Brisk Walk Flush",
        "duration_min": [15, 25],
        "rpe_max": 4,
        "insert_category": "conditioning_maintenance",
        "repeat_allowed": False,
        "display_text": "Brisk or incline walk at a nose-breathing pace, RPE 3. Low-impact aerobic maintenance and recovery support - finish fresher than you started.",
    },
    "aerobic_footwork_rhythm": {
        "label": "Footwork Rhythm Flush",
        "duration_min": [6, 10],
        "rpe_max": 4,
        "insert_category": "conditioning_maintenance",
        "repeat_allowed": False,
        "display_text": "Light in-out steps, pivots, and stance resets, RPE 3. Movement-economy work so you waste less energy in exchanges. No sprinting or sharp cuts.",
    },
    "aerobic_skip_flush": {
        "label": "Light Skipping Flush",
        "duration_min": [6, 10],
        "rpe_max": 4,
        "insert_category": "conditioning_maintenance",
        "repeat_allowed": False,
        "display_text": "30 sec easy skip / 30 sec rest, RPE 3. Keeps rhythm, calf stiffness, and breathing control without hard conditioning stress. Skip only while calves and Achilles are healthy.",
    },
    "aerobic_jog_flush": {
        "label": "Easy Jog Flush",
        "duration_min": [12, 18],
        "rpe_max": 4,
        "insert_category": "conditioning_maintenance",
        "repeat_allowed": False,
        "display_text": "Easy continuous jog or walk-jog, RPE 3. Maintains aerobic rhythm without fatigue. Keep it conversational.",
    },
}


# Mechanical load footprint per insert: the body regions each filler/primer
# actually LOADS enough to aggravate an injury there. Canonical region keys match
# fightcamp.injury_exclusion_rules.INJURY_REGION_KEYWORDS so a downstream safety
# gate can intersect them with an active injury's region. Low load is not zero
# load — even a brisk walk or a low-amplitude foot-placement drill still loads
# the lower chain, so every insert with real bodily movement is mapped here,
# not just the visibly ballistic ones.
#
# mobility_rehab and joint_prep are the deliberate exception, not an oversight:
# both are gentle, pain-free, controlled-range work explicitly TARGETED at the
# athlete's flagged restriction ("Target the flagged restriction with easy
# range... stop well before fatigue" / "Stay smooth and pain-free"). They are
# the designated safe option even at moderate_plus injury (see
# _allowed_inserts), so their regional overlap with an active injury is the
# whole point of prescribing them, not a hazard for the gate to catch. Pure
# mental cue work and breathing/sleep resets are absent for the same
# not-a-hazard reason: a neck strain cannot stop you writing a fight cue.
_INSERT_MECH_LOAD_REGIONS: dict[str, tuple[str, ...]] = {
    # Shadow / punch rhythm loads the striking chain (shoulder, elbow, wrist,
    # chest) AND the entries/exits/stance work that comes with it (ankle, knee).
    "technical_shadow_rhythm": ("shoulder", "elbow", "wrist", "chest", "ankle", "knee"),
    "aerobic_shadow_flow": ("shoulder", "elbow", "wrist", "chest", "ankle", "knee"),
    # Footwork / stance movement loads the lower chain.
    "footwork_walkthrough": ("ankle", "foot", "knee"),
    "aerobic_footwork_rhythm": ("ankle", "foot", "calf", "knee"),
    # Low-amplitude foot-placement / stance-quality work still loads the same
    # lower-leg contact points, just at a smaller range.
    "movement_quality": ("ankle", "foot"),
    # Skipping and jogging add rebound / continuous impact on the lower legs.
    "aerobic_skip_flush": ("calf", "achilles", "ankle", "foot", "shin", "knee"),
    "aerobic_jog_flush": ("calf", "achilles", "ankle", "foot", "shin", "knee", "hamstring"),
    # Walking is genuinely low load, but it is still repetitive weight-bearing
    # gait: an ankle sprain, Achilles flare, foot stress reaction, or irritable
    # knee can all be aggravated by a brisk walk, so it is not zero-cost.
    "walk_flush": ("ankle", "foot", "achilles", "calf", "knee"),
    "aerobic_walk_flush": ("ankle", "foot", "achilles", "calf", "knee"),
}


def insert_mechanical_load_regions(role_key: str) -> tuple[str, ...]:
    """Body regions a gap-fill insert mechanically loads (empty for non-loading work)."""
    return _INSERT_MECH_LOAD_REGIONS.get(role_key, ())


def filler_target_capabilities(role_key: str) -> dict[str, float]:
    """Return the shared low-cost target capability declaration for a filler."""
    return dict(_FILLER_TARGET_CAPABILITY.get(str(role_key or "").strip().lower(), {}))


def _normalised_set(values: Any) -> set[str]:
    return {str(value).strip().lower().replace(" ", "_") for value in clean_list(values) if str(value).strip()}


def _canonical_target(value: Any) -> str:
    normalized = normalize_tag(str(value or "")) or ""
    return _TARGET_ALIASES.get(normalized, normalized)


def _role_has_meaningful_target_coverage(role: dict[str, Any]) -> bool:
    governance = role.get("governance")
    if isinstance(governance, dict) and governance.get("meaningful_stress") is False:
        return False
    if role.get("meaningful_stress") is False:
        return False
    return str(role.get("category") or "").strip().lower() != "support_insert"


def _structured_role_targets(role: dict[str, Any]) -> set[str]:
    targets: set[str] = set()
    sources = [role]
    for nested_key in ("selection_metadata", "metadata"):
        nested = role.get(nested_key)
        if isinstance(nested, dict):
            sources.append(nested)
    for source in sources:
        for field in _STRUCTURED_TARGET_FIELDS:
            for value in clean_list(source.get(field)):
                target = _canonical_target(value)
                if target:
                    targets.add(target)
    return targets


def role_target_capabilities(role: dict[str, Any]) -> set[str]:
    """Return genuine semantic target coverage for one scheduled programme role.

    Renderer text is intentionally ignored. Coverage comes from canonical role
    identity, structured planner fields such as preferred_system, or explicit
    structured target metadata. A generic role key never makes an alactic speed
    exposure count as full aerobic / gas-tank coverage.
    """

    if not isinstance(role, dict) or not _role_has_meaningful_target_coverage(role):
        return set()
    role_key = str(role.get("role_key") or "").strip().lower()
    capabilities = set(_MEANINGFUL_ROLE_CAPABILITIES.get(role_key, ()))
    category = str(role.get("category") or "").strip().lower()
    preferred_system = (
        str(role.get("preferred_system") or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    if category == "conditioning":
        if preferred_system == "technical_footwork":
            capabilities.add("footwork")
        elif preferred_system in {"aerobic", "glycolytic"}:
            capabilities.add("conditioning")
        elif preferred_system in {"alactic", "atp_pcr"}:
            capabilities.add("speed")
    elif category == "mobility":
        capabilities.add("mobility")
    elif category == "recovery":
        capabilities.add("recovery")
    return capabilities | _structured_role_targets(role)


def build_target_coverage_state(
    athlete_model: dict[str, Any],
    scheduled_roles: list[dict[str, Any]] | None = None,
) -> list[TargetCoverageState]:
    """Build the shared selected-priority and remaining-support ledger.

    Meaningful scheduled work fully covers a target. Existing low-cost fillers
    only cover targets their conservative capability map names; they satisfy a
    support opportunity without being reclassified as meaningful adaptation.
    """

    selected_by_target: dict[str, dict[str, Any]] = {}
    target_order: list[str] = []
    for selected in selected_priority_targets(athlete_model):
        target = _canonical_target(selected.target)
        if not target:
            continue
        current = selected_by_target.get(target)
        if current is None:
            target_order.append(target)
            selected_by_target[target] = {
                "label": selected.label,
                "weight": selected.weight,
                "sources": list(selected.sources),
            }
            continue
        current["weight"] = max(float(current["weight"]), selected.weight)
        current["sources"] = list(dict.fromkeys([*current["sources"], *selected.sources]))

    roles = [role for role in (scheduled_roles or []) if isinstance(role, dict)]
    meaningful_targets = {
        target
        for role in roles
        for target in role_target_capabilities(role)
    }
    support_coverage: dict[str, float] = {}
    for role in roles:
        role_key = str(role.get("role_key") or "").strip().lower()
        for target, capability in _FILLER_TARGET_CAPABILITY.get(role_key, {}).items():
            support_coverage[target] = min(
                1.0,
                support_coverage.get(target, 0.0) + float(capability),
            )

    addressable_targets = {
        target
        for capabilities in _FILLER_TARGET_CAPABILITY.values()
        for target in capabilities
    }
    order_index = {target: index for index, target in enumerate(target_order)}
    states = [
        TargetCoverageState(
            target=target,
            label=str(data["label"]),
            priority_weight=float(data["weight"]),
            sources=tuple(data["sources"]),
            meaningful_coverage=target in meaningful_targets,
            support_coverage=support_coverage.get(target, 0.0),
            remaining_need=(
                0.0
                if target in meaningful_targets
                else max(0.0, 1.0 - support_coverage.get(target, 0.0))
            ),
            low_cost_addressable=target in addressable_targets,
        )
        for target, data in selected_by_target.items()
    ]
    return sorted(
        states,
        key=lambda state: (-state.priority_weight, order_index[state.target]),
    )


def _priority_contribution(
    role_key: str,
    coverage_state: list[TargetCoverageState],
) -> float:
    capabilities = _FILLER_TARGET_CAPABILITY.get(role_key, {})
    return max(
        (
            state.priority_weight * state.remaining_need * float(capabilities.get(state.target, 0.0))
            for state in coverage_state
            if state.low_cost_addressable and state.remaining_need > 0
        ),
        default=0.0,
    )


def _has_selected_target(athlete_model: dict[str, Any], target: str) -> bool:
    canonical = _canonical_target(target)
    return any(
        state.target == canonical
        for state in build_target_coverage_state(athlete_model)
    )


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(_flatten_text(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten_text(item) for item in value)
    return str(value)


def _readiness_flags(athlete_model: dict[str, Any]) -> set[str]:
    return _normalised_set(athlete_model.get("readiness_flags", []))


def _has_active_weight_cut(athlete_model: dict[str, Any]) -> bool:
    flags = _readiness_flags(athlete_model)
    if flags & {"active_weight_cut", "weight_cut_active", "aggressive_weight_cut", "extreme_weight_cut"}:
        return True
    if bool(athlete_model.get("weight_cut_risk")):
        return True
    try:
        return float(athlete_model.get("weight_cut_pct") or 0.0) > 0
    except (TypeError, ValueError):
        return False


def _has_high_fatigue(athlete_model: dict[str, Any]) -> bool:
    return normalize_fatigue_level(athlete_model) == "high" or "high_fatigue" in _readiness_flags(athlete_model)


def _has_mobility_need(athlete_model: dict[str, Any]) -> bool:
    readiness = _normalised_set(athlete_model.get("readiness_flags", []))
    return _has_selected_target(athlete_model, "mobility") or any(
        "mobil" in value or "rehab" in value or "range" in value
        for value in readiness
    )


def _has_conditioning_goal(athlete_model: dict[str, Any]) -> bool:
    return _has_selected_target(athlete_model, "conditioning")


def _has_lower_leg_load_risk(athlete_model: dict[str, Any]) -> bool:
    if _all_active_injuries_surface_only(athlete_model):
        return False
    text = _flatten_text(
        [
            athlete_model.get("parsed_injuries"),
            athlete_model.get("guided_injury"),
            athlete_model.get("injury_restrictions"),
            athlete_model.get("injuries")
            or athlete_model.get("injury")
            or athlete_model.get("injury_notes"),
            athlete_model.get("active_injury"),
            sorted(_readiness_flags(athlete_model)),
        ]
    ).lower().replace("_", " ")
    return any(marker in text for marker in _LOWER_LEG_LOAD_MARKERS)


def _safe_conditioning_maintenance_inserts(
    athlete_model: dict[str, Any],
    insert_offset: int,
    injury_state: str,
    *,
    on_hard_sparring_day: bool,
) -> set[str]:
    """Context-safe aerobic-maintenance options for a conditioning/gas-tank goal.

    Returns an empty set unless the athlete selected a conditioning signal and the
    slot is safe to use (not the day before the fight, not a hard-sparring day).
    Higher-impact options (skip/jog) are withheld when lower legs are loaded,
    fatigue is high, or a hard weight cut is active, so the slot stays low-risk.
    """

    if insert_offset < MIN_AEROBIC_MAINTENANCE_OFFSET or on_hard_sparring_day:
        return set()
    if not _has_conditioning_goal(athlete_model):
        return set()

    # Honour the injury guard: acute/concussion/fracture/medical-hold states
    # (moderate_plus) get only the existing recovery + mobility support and must
    # never be prescribed RPE 3-4 aerobic maintenance.
    if injury_state == "moderate_plus":
        return set()

    # Zero/low-impact aerobic rhythm is always safe to keep the goal visible.
    # Footwork rhythm is low-impact movement economy and stays safe for the
    # remaining none / mild_stable states.
    safe = set(_ZERO_IMPACT_AEROBIC_INSERTS)
    safe.add("aerobic_footwork_rhythm")

    lower_leg_risk = _has_lower_leg_load_risk(athlete_model)
    high_fatigue = _has_high_fatigue(athlete_model)
    active_cut = _has_active_weight_cut(athlete_model)
    fatigue_low = normalize_fatigue_level(athlete_model) == "low"

    # Impact work only when lower legs are healthy and fatigue is not high.
    if not lower_leg_risk and not high_fatigue:
        safe.add("aerobic_skip_flush")
        # Continuous jogging is the highest-cost option: only with genuinely low
        # fatigue and no active weight cut.
        if fatigue_low and not active_cut:
            safe.add("aerobic_jog_flush")

    return safe


def _is_fight_sport(athlete_model: dict[str, Any]) -> bool:
    text = _flatten_text(
        [
            athlete_model.get("sport"),
            athlete_model.get("mapped_format"),
            athlete_model.get("fight_format"),
            athlete_model.get("style"),
            athlete_model.get("style_technical"),
            athlete_model.get("style_tactical"),
        ]
    ).lower()
    return any(
        marker in text
        for marker in {
            "boxing",
            "boxer",
            "combat",
            "fight",
            "fighter",
            "mma",
            "muay",
            "kickbox",
            "grappling",
            "wrestling",
            "jiu",
        }
    )


def _cost_category(role_key: str) -> str:
    if role_key in ZERO_COST_INSERTS:
        return "zero_cost"
    if role_key in LOW_COST_RECOVERY_INSERTS:
        return "low_cost_recovery"
    if role_key in LOW_COST_AEROBIC_INSERTS:
        return "low_cost_aerobic"
    return "physical"


def _insert_category(role_key: str) -> str:
    meta = _INSERT_META.get(role_key) or {}
    return str(meta.get("insert_category") or _cost_category(role_key))


def classify_injury_state(athlete_model: dict[str, Any]) -> Literal["none", "mild_stable", "moderate_plus"]:
    if _all_active_injuries_surface_only(athlete_model):
        return "none"

    parsed = athlete_model.get("parsed_injuries") or []
    guided = athlete_model.get("guided_injury")
    restrictions = athlete_model.get("injury_restrictions") or []
    raw_injuries = athlete_model.get("injuries") or athlete_model.get("injury") or athlete_model.get("injury_notes")
    active_injury = athlete_model.get("active_injury")
    has_active = bool(athlete_model.get("has_active_injury"))

    injury_text = _flatten_text([parsed, guided, restrictions, raw_injuries, active_injury]).lower().replace("_", " ")
    flags_text = " ".join(_readiness_flags(athlete_model)).replace("_", " ")

    empty_markers = {"", "none", "no", "false", "0", "n/a", "na"}
    active_injury_text = str(active_injury or "").strip().lower()
    has_raw_injury = bool(injury_text.strip()) and injury_text.strip() not in empty_markers
    if not parsed and not guided and not restrictions and not has_active and not has_raw_injury:
        return "none"
    if active_injury_text in {"none", "no", "false", "0"} and not parsed and not guided and not restrictions and not raw_injuries:
        return "none"

    combined = f"{injury_text} {flags_text}"
    moderate_markers = {
        "moderate",
        "severe",
        "high",
        "worsening",
        "unstable",
        "instability",
        "daily",
        "red flag",
        "red flags",
        "medical hold",
        "needs review",
        "acute",
        "neurological",
        "concussion",
        "fracture",
        "surgery",
    }
    if any(marker in combined for marker in moderate_markers):
        return "moderate_plus"

    mild_marker = any(marker in combined for marker in {"mild", "low", "minor"})
    stable_marker = any(marker in combined for marker in {"stable", "improving", "settled"})
    if mild_marker and stable_marker:
        return "mild_stable"

    return "moderate_plus"


def _allowed_inserts(
    athlete_model: dict[str, Any],
    insert_offset: int,
    *,
    on_hard_sparring_day: bool = False,
) -> set[str]:
    if insert_offset == 0:
        return set()

    # Fight Visualisation is placed by its own mandatory countdown protocol, not
    # by gap-fill scoring: it must never be chosen to fill a hole in the calendar.
    allowed = set(_ALL_INSERTS) - {FIGHT_VISUALIZATION_ROLE_KEY}
    injury_state = classify_injury_state(athlete_model)

    if _has_active_weight_cut(athlete_model):
        allowed -= {"walk_flush"}
        if injury_state in {"none", "mild_stable"}:
            allowed -= {"technical_shadow_rhythm", "footwork_walkthrough", "movement_quality"}

    if injury_state == "moderate_plus":
        allowed &= ZERO_COST_INSERTS | LOW_COST_RECOVERY_INSERTS | {"mobility_rehab", "joint_prep"}
    elif injury_state == "mild_stable":
        allowed |= {"mobility_rehab", "joint_prep"}

    has_injury = bool(
        athlete_model.get("has_active_injury")
        or athlete_model.get("injuries")
        or athlete_model.get("parsed_injuries")
        or athlete_model.get("guided_injury")
        or athlete_model.get("injury_restrictions")
    )
    if has_injury and _all_active_injuries_surface_only(athlete_model):
        allowed -= {"mobility_rehab", "joint_prep"}

    if "mobility_rehab" in allowed and not (
        _has_mobility_need(athlete_model) or injury_state in {"mild_stable", "moderate_plus"}
    ):
        allowed.remove("mobility_rehab")

    if insert_offset == 1 or _has_high_fatigue(athlete_model):
        allowed &= ZERO_COST_INSERTS | LOW_COST_RECOVERY_INSERTS

    if on_hard_sparring_day:
        allowed -= PHYSICAL_INSERTS
        allowed |= ZERO_COST_INSERTS | LOW_COST_RECOVERY_INSERTS

    # A selected conditioning / gas-tank goal keeps a low-risk aerobic-maintenance
    # slot available even when the higher-cost physical inserts are stripped for
    # freshness, so the goal stays visible instead of vanishing into unrelated
    # tactical/breathing filler. The aerobic inserts never appear without a
    # conditioning signal, so the no-goal contracts above are unchanged.
    allowed -= LOW_COST_AEROBIC_INSERTS
    allowed |= _safe_conditioning_maintenance_inserts(
        athlete_model,
        insert_offset,
        injury_state,
        on_hard_sparring_day=on_hard_sparring_day,
    )

    return allowed


def _phase_allocation_for_watch(athlete_model: dict[str, Any]) -> dict[str, Any]:
    """Return the planner's phase allocation; recompute only for legacy direct callers."""
    phase_weeks = athlete_model.get("phase_weeks")
    if isinstance(phase_weeks, dict) and isinstance(phase_weeks.get("days"), dict):
        return phase_weeks

    raw_days = athlete_model.get("days_until_fight")
    try:
        days_until_fight = int(raw_days) if raw_days is not None else None
    except (TypeError, ValueError):
        days_until_fight = None
    if days_until_fight is None:
        days_until_fight = 7

    try:
        camp_length = int(athlete_model.get("camp_length_weeks") or 0)
    except (TypeError, ValueError):
        camp_length = 0
    if camp_length <= 0:
        camp_length = max(1, round(days_until_fight / 7))

    sport = planning_format(athlete_model.get("sport"))

    return calculate_phase_weeks(
        camp_length,
        sport,
        athlete_model.get("tactical_styles") or athlete_model.get("tactical_style"),
        athlete_model.get("status"),
        athlete_model.get("fatigue"),
        bool(athlete_model.get("weight_cut_risk")),
        athlete_model.get("mental_blocks") or athlete_model.get("mental_block"),
        athlete_model.get("weight_cut_pct"),
        days_until_fight,
    )


def _watch_phase_for_offset(athlete_model: dict[str, Any], insert_offset: int) -> str:
    """Map D-day to the authoritative dynamic GPP/SPP/TAPER allocation."""
    phase_days = (_phase_allocation_for_watch(athlete_model).get("days") or {})
    remaining = max(1, int(insert_offset))
    for phase in ("TAPER", "SPP", "GPP"):
        try:
            days = max(0, int(phase_days.get(phase, 0) or 0))
        except (TypeError, ValueError):
            days = 0
        if days <= 0:
            continue
        if remaining <= days:
            return phase
        remaining -= days
    return next(
        (phase for phase in ("GPP", "SPP", "TAPER") if int(phase_days.get(phase, 0) or 0) > 0),
        "TAPER",
    )


def _apply_bank_watch(
    role: dict[str, Any],
    athlete_model: dict[str, Any],
    *,
    phase: str,
    used_watch_keys: set[str],
) -> bool:
    """Stamp the selected Tactical Watch from the JSON bank onto ``role``.

    Every athlete-facing string comes from ``data/tactical_watch_bank.json``;
    this only formats what the bank already holds. Returns False, recording
    the skip on ``role``, when no eligible watch exists for this phase.
    """
    try:
        watch = select_tactical_watch(
            extract_tactical_style(athlete_model),
            phase,
            used_watch_keys,
            allow_reuse=True,
        )
    except TacticalWatchBankExhausted:
        role["tactical_watch_skipped"] = {"reason_code": "tactical_watch_bank_empty", "phase": phase}
        return False
    metadata = watch_metadata(watch)
    watch_governance = dict(metadata.pop("governance", {}) or {})
    role.update(metadata)
    role["camp_phase"] = phase
    role["governance"] = {
        **dict(role.get("governance") or {}),
        **watch_governance,
    }
    role["display_text"] = build_watch_display_text(watch)
    role["duration_min"] = [watch.duration_minutes, watch.duration_minutes]
    role["prescribed_duration_min"] = watch.duration_minutes
    used_watch_keys.add(watch.key)
    return True

def _apply_bank_visualization(
    role: dict[str, Any], athlete_model: dict[str, Any], countdown_day: int
) -> bool:
    """Stamp the selected Fight Visualisation from the JSON bank onto ``role``.

    Every athlete-facing string comes from ``data/fight_visualization_bank.json``;
    this only formats what the bank already holds. Returns False when the bank
    has nothing for this countdown day, leaving ``role`` untouched.
    """
    entry = select_for_athlete(athlete_model, countdown_day)
    if entry is None:
        return False
    # The selected bank entry already reflects sport, style and countdown day.
    # The rehearsal carries no physical load, so physical fatigue is no reason
    # to cut it short. The dose is the bank's, capped at the mental-training
    # policy's 8 minutes (the D-7 scenario used to run 12).
    prescribed_duration = min(entry.duration_min[1], VISUALISATION_MAX_DURATION_MIN)
    metadata = visualization_metadata(entry, prescribed_duration_min=prescribed_duration)
    governance = dict(metadata.pop("governance", {}) or {})
    role.update(metadata)
    role["governance"] = {**dict(role.get("governance") or {}), **governance}
    role["display_text"] = build_visualization_display_text(
        entry, prescribed_duration_min=prescribed_duration
    )
    role["duration_min"] = [
        min(value, VISUALISATION_MAX_DURATION_MIN) for value in entry.duration_min
    ]
    role["prescribed_duration_min"] = prescribed_duration
    role["athlete_facing_label"] = FIGHT_VISUALIZATION_LABEL
    role["mandatory_fight_visualization"] = True
    role["stress_class"] = "support"
    return True


def _ensure_fight_visualization_days(
    session_sequence: list[dict[str, Any]],
    athlete_model: dict[str, Any],
    countdown_map: dict[str, str],
    days_until_fight: int,
    usage_ledger: dict[str, Any],
) -> list[dict[str, Any]]:
    """Place the Fight Visualisation countdown protocol.

    The days come from :mod:`fightcamp.mental_training_policy`: D-7/D-1/D-0 by
    default (D-7/D-3/D-1/D-0 when mental preparation is prioritised). D-5 and
    D-3 used to be mandatory too; their style-owned scenarios now run in the
    camp block instead of crowding fight week.

    These are countdown interventions, not gap fillers: they are not scored, not
    budgeted and not subject to day-eligibility, calendar legality or training-day
    checks. They carry zero physical load and coexist with whatever the day already
    holds -- hard sparring, technical combat, strength, the fight-day protocol, or
    nothing at all. The only condition is that the countdown day is inside the
    plan's own window.
    """
    if not _is_fight_sport(athlete_model):
        return []

    additions: list[dict[str, Any]] = []
    for countdown_day in mental_training_policy(athlete_model).countdown_visualization_days:
        if countdown_day > days_until_fight:
            continue
        existing = next(
            (
                role
                for role in session_sequence
                if str(role.get("role_key") or "") == FIGHT_VISUALIZATION_ROLE_KEY
                and _role_offset(role) == countdown_day
            ),
            None,
        )
        weekday = str(countdown_map.get(f"D-{countdown_day}") or "").strip() or None
        if existing is not None:
            # Always restamp, never trust what is already on the role. On a
            # regeneration the athlete's sport, tactical style or countdown may
            # have changed, and a role that merely *has* a bank key would
            # otherwise carry the previous camp's content forward.
            _apply_bank_visualization(existing, athlete_model, countdown_day)
            if weekday:
                existing["real_weekday"] = weekday
                existing["scheduled_day_hint"] = weekday
                existing["countdown_display_label"] = (
                    f"D-{countdown_day} ({weekday.title()})"
                )
            continue
        role = _build_insert_role(
            FIGHT_VISUALIZATION_ROLE_KEY,
            athlete_model,
            countdown_day,
            weekday,
            usage_ledger=usage_ledger,
        )
        if not role.get("fight_visualization_key"):
            # No bank content resolved for this athlete/day: never ship an
            # empty governed card.
            continue
        additions.append(role)
        _record_insert_usage(usage_ledger, FIGHT_VISUALIZATION_ROLE_KEY, countdown_day)
    return additions


def camp_visualization_day_tier(day_roles: list[dict[str, Any]]) -> int | None:
    """How well a day suits an optional camp Fight Visualisation.

    1 = everything on it is low-load support, 2 = any other non-contact day.
    None = unsuitable: no real training on it (a rest day stays a rest day, and
    a day holding only mental work is not a training day), hard contact, or any
    mental task already there -- the athlete gets one clear mental task a day.
    """
    keys = {str(role.get("role_key") or "") for role in day_roles}
    if not keys or "hard_sparring_day" in keys or keys & MENTAL_ROLE_KEYS:
        return None
    if all(is_low_cost_coexistable_filler(role) for role in day_roles):
        return 1
    return 2


def _ensure_late_camp_visualizations(
    session_sequence: list[dict[str, Any]],
    athlete_model: dict[str, Any],
    countdown_map: dict[str, str],
    days_until_fight: int,
) -> list[dict[str, Any]]:
    """Optional camp Fight Visualisation on the late-fight path's D-13..D-8.

    The normal-camp planner owns D-14 and further out; this covers the last
    pre-countdown week of the same block so it reaches the countdown without a
    gap.
    """
    if not _is_fight_sport(athlete_model):
        return []
    first = min(days_until_fight, 13, CAMP_VISUALIZATION_FIRST_DAY)
    by_day: dict[int, list[dict[str, Any]]] = {}
    for role in session_sequence:
        offset = _role_offset(role)
        if offset is not None and CAMP_VISUALIZATION_LAST_DAY <= offset <= first:
            by_day.setdefault(offset, []).append(role)
    candidates = {
        offset: tier
        for offset, roles in by_day.items()
        if (tier := camp_visualization_day_tier(roles)) is not None
    }
    additions: list[dict[str, Any]] = []
    limit = mental_training_policy(athlete_model).visualisations_per_week
    for ordinal, offset in enumerate(choose_camp_visualization_days(candidates, limit=limit)):
        weekday = str(countdown_map.get(f"D-{offset}") or "").strip() or None
        # Each week runs picture -> read -> reset, the same order as the camp weeks.
        role = camp_visualization_role(
            athlete_model, d_day=offset, weekday=weekday, ordinal=ordinal
        )
        if role is not None:
            additions.append(role)
    return additions


def _time_band_preferences(insert_offset: int) -> list[str]:
    if insert_offset == 1:
        return [
            "tactical_cue_card",
            "neural_visualization",
            "breathing_reset",
            "sleep_downshift",
            "recovery_reset",
            "tactical_watch",
            "self_review",
        ]
    if 2 <= insert_offset <= 4:
        return [
            "tactical_cue_card",
            "neural_visualization",
            "breathing_reset",
            "mobility_rehab",
            "recovery_reset",
            "sleep_downshift",
        ]
    if 5 <= insert_offset <= 7:
        return [
            "tactical_cue_card",
            "neural_visualization",
            "breathing_reset",
            "mobility_rehab",
            "technical_shadow_rhythm",
            "tactical_watch",
            "recovery_reset",
        ]
    if 8 <= insert_offset <= 13:
        return [
            "tactical_watch",
            "tactical_cue_card",
            "technical_shadow_rhythm",
            "footwork_walkthrough",
            "mobility_rehab",
            "breathing_reset",
            "neural_visualization",
            "recovery_reset",
        ]
    return [
        "tactical_watch",
        "footwork_walkthrough",
        "mobility_rehab",
        "joint_prep",
        "walk_flush",
        "self_review",
        "movement_quality",
        "breathing_reset",
        "recovery_reset",
    ]


def _new_usage_ledger() -> dict[str, Any]:
    return {
        "used_role_keys": set(),
        "used_categories": set(),
        "role_key_offsets": {},
        "category_counts": {},
        "used_tactical_watch_keys": set(),
    }


def _record_insert_usage(ledger: dict[str, Any], role_key: str, offset: int | None) -> None:
    if role_key not in _ALL_INSERTS:
        return
    category = _insert_category(role_key)
    ledger.setdefault("used_role_keys", set()).add(role_key)
    ledger.setdefault("used_categories", set()).add(category)
    category_counts = ledger.setdefault("category_counts", {})
    category_counts[category] = int(category_counts.get(category, 0)) + 1
    if offset is not None:
        ledger.setdefault("role_key_offsets", {}).setdefault(role_key, []).append(offset)


def _usage_ledger_from_sequence(session_sequence: list[dict[str, Any]]) -> dict[str, Any]:
    ledger = _new_usage_ledger()
    for role in session_sequence:
        role_key = str(role.get("role_key") or "")
        _record_insert_usage(ledger, role_key, _role_offset(role))
        watch_key = str(role.get("tactical_watch_key") or "").strip()
        if watch_key:
            ledger["used_tactical_watch_keys"].add(watch_key)
    return ledger


def _role_repeat_blocked(role_key: str, insert_offset: int, usage_ledger: dict[str, Any] | None) -> bool:
    if not usage_ledger:
        return False
    if bool((_INSERT_META.get(role_key) or {}).get("repeat_allowed")):
        return False
    previous_offsets = usage_ledger.get("role_key_offsets", {}).get(role_key, [])
    return any(abs(int(previous) - insert_offset) <= 7 for previous in previous_offsets)


def _base_preference_score(role_key: str, insert_offset: int) -> float:
    preferences = _time_band_preferences(insert_offset)
    if role_key not in preferences:
        return 1.0
    return float((len(preferences) - preferences.index(role_key)) * 4)


def _score_insert_role(
    role_key: str,
    athlete_model: dict[str, Any],
    insert_offset: int,
    *,
    usage_ledger: dict[str, Any] | None = None,
    gap_span: int | None = None,
) -> float:
    score = _base_preference_score(role_key, insert_offset)
    high_fatigue = _has_high_fatigue(athlete_model)
    active_cut = _has_active_weight_cut(athlete_model)
    injury_state = classify_injury_state(athlete_model)
    readiness = _normalised_set(athlete_model.get("readiness_flags", []))
    mobility_safety_need = injury_state in {"mild_stable", "moderate_plus"} or any(
        "mobil" in value or "rehab" in value or "range" in value
        for value in readiness
    )

    if active_cut:
        if role_key in {"tactical_cue_card", "breathing_reset", "sleep_downshift", "recovery_reset"}:
            score += 16
        if role_key == "tactical_watch":
            score += 14
        if role_key in PHYSICAL_INSERTS:
            score -= 20

    if high_fatigue:
        if role_key in {"breathing_reset", "sleep_downshift", "neural_visualization"}:
            score += 16
        elif role_key == "recovery_reset":
            score += 10
        if role_key in PHYSICAL_INSERTS:
            score -= 8

    if mobility_safety_need:
        if injury_state != "none" and active_cut:
            mobility_boost = 55
        elif injury_state != "none" or not active_cut:
            mobility_boost = 28
        else:
            mobility_boost = 8
        if role_key in {"mobility_rehab", "joint_prep"}:
            score += mobility_boost

    if role_key in LOW_COST_AEROBIC_INSERTS:
        if high_fatigue or active_cut:
            if role_key in _ZERO_IMPACT_AEROBIC_INSERTS:
                score += 6
            elif role_key in _IMPACT_AEROBIC_INSERTS:
                score -= 10
        if role_key == "aerobic_shadow_flow" and _is_fight_sport(athlete_model):
            score += 3

    if gap_span is not None and gap_span >= TWO_INSERT_GAP_MIN_DAYS:
        if role_key == "walk_flush":
            score += 8
        elif role_key == "mobility_rehab":
            score += 5
        elif role_key == "tactical_watch":
            score += 5
        elif role_key == "footwork_walkthrough":
            score += 4

    if usage_ledger:
        used_role_keys = usage_ledger.get("used_role_keys", set())
        used_categories = usage_ledger.get("used_categories", set())
        category = _insert_category(role_key)
        score += 2 if role_key not in used_role_keys else -2
        score += 1 if category not in used_categories else -0.75
        score -= min(int(usage_ledger.get("category_counts", {}).get(category, 0)), 3) * 0.25

    return score


def _select_role_key(
    athlete_model: dict[str, Any],
    insert_offset: int,
    allowed: set[str],
    *,
    usage_ledger: dict[str, Any] | None = None,
    gap_span: int | None = None,
    force_tactical: bool = False,
    force_conditioning: bool = False,
    coverage_state: list[TargetCoverageState] | None = None,
) -> str | None:
    candidates = set(allowed)
    coverage_state = coverage_state or []
    if force_tactical:
        candidates &= TACTICAL_INSERTS
    elif force_conditioning:
        # The caller owns the frequency decision: it knows the week, how many
        # aerobic touches it already holds, and whether another is wanted.
        # Coverage state cannot answer that — it reports whether a goal is
        # represented at all, so one conditioning day marks conditioning
        # satisfied for the rest of the week. ``candidates`` only contains
        # aerobic inserts when the athlete actually selected a conditioning
        # signal (see _allowed_inserts), so a non-conditioning athlete falls
        # through untouched.
        aerobic = candidates & LOW_COST_AEROBIC_INSERTS
        if aerobic:
            candidates = aerobic
        # else: no safe aerobic insert for this slot -> fall back to normal
        # selection so the gap still gets a tactical/recovery filler.
    candidates = {
        role_key
        for role_key in candidates
        if not _role_repeat_blocked(role_key, insert_offset, usage_ledger)
    }
    if not candidates:
        return None

    preference_rank = {
        role_key: index
        for index, role_key in enumerate(_time_band_preferences(insert_offset))
    }
    all_inserts_index = {role_key: index for index, role_key in enumerate(sorted(_ALL_INSERTS))}
    return max(
        sorted(candidates),
        key=lambda role_key: (
            _priority_contribution(role_key, coverage_state),
            _score_insert_role(
                role_key,
                athlete_model,
                insert_offset,
                usage_ledger=usage_ledger,
                gap_span=gap_span,
            ),
            -preference_rank.get(role_key, 99),
            -all_inserts_index.get(role_key, 99),
        ),
    )


def _build_insert_role(
    role_key: str,
    athlete_model: dict[str, Any],
    insert_offset: int,
    weekday: str | None = None,
    *,
    usage_ledger: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """None only for a tactical_watch with no eligible bank content."""
    meta = _INSERT_META[role_key]
    label = str(meta["label"])
    if role_key == "aerobic_shadow_flow" and normalize_sport(
        athlete_model.get("sport") or athlete_model.get("fight_format") or ""
    ) == "boxing":
        label = "Shadowboxing Aerobic Flow"
    # tactical_watch carries no static copy: its athlete-facing text is stamped
    # from the JSON drill bank below.
    display_text = (
        ""
        if role_key in {"tactical_watch", FIGHT_VISUALIZATION_ROLE_KEY}
        else str(meta["display_text"])
    )
    lower_dose = _has_high_fatigue(athlete_model) or _has_active_weight_cut(athlete_model)
    if role_key == "aerobic_shadow_flow" and lower_dose:
        display_text = display_text.replace("4 x 2 min", "3 x 2 min")
    prescribed_duration = (
        8 if lower_dose else 11
    ) if role_key == "aerobic_shadow_flow" else int(meta["duration_min"][0 if lower_dose else 1])
    if role_key not in {"tactical_watch", FIGHT_VISUALIZATION_ROLE_KEY, "aerobic_shadow_flow"}:
        display_text = f"- {label}: {prescribed_duration} minutes, RPE {int(meta['rpe_max'])} max.\n{display_text}"
    role: dict[str, Any] = {
        "session_index": None,
        "category": "support_insert",
        "role_key": role_key,
        "scheduled_day_hint": weekday,
        "athlete_facing_label": label,
        "display_text": display_text,
        "duration_min": list(meta["duration_min"]),
        "prescribed_duration_min": prescribed_duration,
        "rpe_max": int(meta["rpe_max"]),
        "support_insert_category": _insert_category(role_key),
        "support_insert_cost_category": _cost_category(role_key),
        "support_target_capabilities": filler_target_capabilities(role_key),
        "mechanical_load_regions": list(insert_mechanical_load_regions(role_key)),
        "countdown_offset": insert_offset,
        "countdown_label": f"D-{insert_offset}",
        "scheduled_countdown_label": f"D-{insert_offset}",
        "stress_class": "support",
        "cost_class": "low",
        "governance": {
            "authority": "gap_fill_support_insert",
            "meaningful_stress": False,
        },
    }
    exercise_key = support_insert_exercise_key(role_key, label)
    if exercise_key:
        role["exercise_key"] = exercise_key
    if role_key == "tactical_watch":
        used_watch_keys = (
            usage_ledger.setdefault("used_tactical_watch_keys", set())
            if usage_ledger is not None
            else set()
        )
        if not _apply_bank_watch(
            role,
            athlete_model,
            phase=_watch_phase_for_offset(athlete_model, insert_offset),
            used_watch_keys=used_watch_keys,
        ):
            return None
    if role_key == FIGHT_VISUALIZATION_ROLE_KEY:
        _apply_bank_visualization(role, athlete_model, insert_offset)
    if weekday:
        role["real_weekday"] = weekday
        role["countdown_display_label"] = f"D-{insert_offset} ({weekday.title()})"
    return role


def _legal_support_keys(
    legality: CalendarLegalityView,
    role_keys: set[str],
    insert_offset: int,
) -> set[str]:
    """Filter candidate filler role-keys through the shared calendar legality.

    Keep every key the policy marks ``ALLOW``; fall back to the ``DEPRIORITIZE``
    keys only when no ``ALLOW`` key survives; never keep a ``FORBID`` key. A key
    the policy cannot classify from its role-key alone is left in (the filler's
    own selection still governs it). The prefer-ALLOW choice lives here in the
    filler layer; ``calendar_context`` only builds the view and answers per-role
    directives, so it stays representation-only.
    """
    allow: set[str] = set()
    deprioritized: set[str] = set()
    for key in role_keys:
        profile = role_load_profile({"role_key": str(key)})
        if profile is None:
            allow.add(key)
            continue
        directive = legality.decision_for_profile(profile, insert_offset).directive
        if directive is PlacementDirective.ALLOW:
            allow.add(key)
        elif directive is PlacementDirective.DEPRIORITIZE:
            deprioritized.add(key)
    return allow or deprioritized


def _select_gap_footwork_drill(
    athlete_model: dict[str, Any],
    insert_offset: int,
    usage_ledger: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Select an existing bank drill for a footwork filler, if one is legal."""
    phase = _watch_phase_for_offset(athlete_model, insert_offset)
    sport = athlete_model.get("sport") or athlete_model.get("fight_format") or ""
    tactical = clean_list(
        athlete_model.get("style_tactical")
        or athlete_model.get("tactical_styles")
        or athlete_model.get("tactical_style")
    )
    technical = clean_list(athlete_model.get("style_technical") or athlete_model.get("technical_style"))
    injury_text = _flatten_text(
        [
            athlete_model.get("parsed_injuries"),
            athlete_model.get("guided_injury"),
            athlete_model.get("injuries") or athlete_model.get("injury"),
            athlete_model.get("injury_notes"),
            athlete_model.get("active_injury"),
        ]
    ).strip()
    injuries = [injury_text] if injury_text else []
    existing_names = set((usage_ledger or {}).get("used_technical_footwork_names", set()))
    flags = {
        **athlete_model,
        "sport": sport,
        "fight_format": sport,
        "phase": phase,
        "style_tactical": tactical,
        "style_technical": technical,
        "weaknesses": clean_list(athlete_model.get("weaknesses")),
        "competitive_maturity": athlete_model.get("competitive_maturity") or "",
        # Reaching this function means the gap-fill engine has already selected
        # `footwork_walkthrough` as the best legal filler. Treat that role choice
        # as contextual footwork relevance for this local bank lookup only, so a
        # fighter does not need to have explicitly selected footwork as a goal or
        # weakness to benefit from the personalised bank. The athlete model is not
        # mutated and the normal training-plan selector remains relevance-gated.
        "key_goals": list(
            dict.fromkeys([*clean_list(athlete_model.get("key_goals")), "footwork"])
        ),
        "equipment": clean_list(
            athlete_model.get("equipment") or athlete_model.get("available_equipment")
        ),
    }
    window = classify_late_selector_window(insert_offset)
    restrictions = athlete_model.get("injury_restrictions") or []
    for drill in select_technical_footwork_candidates(flags, existing_names, injuries):
        eligible_windows = {
            str(value).strip().lower()
            for value in drill.get("late_windows", [])
            if str(value).strip()
        }
        if window and eligible_windows and window not in eligible_windows:
            continue
        restriction = evaluate_restriction_impact(
            restrictions,
            text=" ".join(
                str(drill.get(key) or "")
                for key in ("name", "modality", "notes", "equipment_note")
            ),
            tags=drill.get("tags", []),
            limit_penalty=-0.75,
        )
        if restriction.get("allowed", True):
            return drill
    return None


def _apply_bank_footwork(
    role: dict[str, Any],
    athlete_model: dict[str, Any],
    insert_offset: int,
    usage_ledger: dict[str, Any] | None,
) -> None:
    drill = _select_gap_footwork_drill(athlete_model, insert_offset, usage_ledger)
    role["technical_footwork_channel"] = TECHNICAL_FOOTWORK_GROUP
    if drill is None:
        role["technical_footwork_fallback"] = True
        return
    stance = athlete_model.get("stance")
    fields = technical_footwork_prescription_fields(drill, stance=stance)
    name = fields["name"]
    notes = str(drill.get("notes") or "").strip()
    timing_lower = fields["timing"].lower()
    rest_lower = fields["rest"].lower()
    rest_seconds = re.match(r"(\d+)\s*sec", rest_lower)
    rest_already_stated = rest_lower in timing_lower
    if rest_seconds:
        rest_already_stated = rest_already_stated or bool(
            re.search(
                rf"\b{re.escape(rest_seconds.group(1))}\s*sec(?:ond)?s?\s+(?:rest|reset)\b",
                timing_lower,
            )
        )
    dose = " ".join(
        part
        for part in (
            fields["timing"],
            f"Rest: {fields['rest']}." if fields["rest"] and not rest_already_stated else "",
        )
        if part
    )
    # Same shared "Why: / bulleted activity / indented detail lines" contract as
    # every other gap-fill insert (see build_watch_display_text,
    # build_coordination_display_text) so the renderer treats this as one card,
    # not several stray lines.
    lines = [f"Why: {notes or 'Keep the technical footwork identity sharp in this gap.'}"]
    lines.append(f"- {name}: {dose}" if dose else f"- {name}")
    if fields["cue"]:
        lines.append(f"  Cue: {fields['cue']}")
    if fields["cue_execution"]:
        lines.append(f"  Cue Method: {fields['cue_execution']}")
    if fields["side_instruction"]:
        lines.append(f"  Side / Stance: {fields['side_instruction']}")
    if fields["quality_stop_rule"]:
        lines.append(f"  Quality Stop: {fields['quality_stop_rule']}")
    role.update(
        {
            "athlete_facing_label": name,
            "display_text": "\n".join(lines),
            "technical_footwork_name": name,
            "technical_footwork_source": "technical_footwork_bank.json",
            "technical_footwork_fallback": False,
            "required_equipment": list(drill.get("equipment") or []),
        }
    )
    if usage_ledger is not None:
        usage_ledger.setdefault("used_technical_footwork_names", set()).add(name)


def select_gap_fill_insert(
    athlete_model: dict[str, Any],
    insert_offset: int,
    *,
    on_hard_sparring_day: bool = False,
    usage_ledger: dict[str, Any] | None = None,
    gap_span: int | None = None,
    force_tactical: bool = False,
    force_conditioning: bool = False,
    allow_physical: bool = True,
    legality: CalendarLegalityView | None = None,
    scheduled_roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if insert_offset == 0:
        return None

    allowed = _allowed_inserts(
        athlete_model,
        insert_offset,
        on_hard_sparring_day=on_hard_sparring_day,
    )
    if not allow_physical:
        # Aerobic maintenance also occupies a real training day. Never let it
        # bypass the day quota just because it is low cost.
        allowed -= PHYSICAL_INSERTS | LOW_COST_AEROBIC_INSERTS
    # Shared calendar legality is the authority on physical coexistence: drop any
    # candidate the policy would FORBID at this position and prefer its ALLOW
    # options over DEPRIORITIZE ones, all read from the same canonical calendar
    # the final governor verifies. Filler variety/injury/goal selection below is
    # unchanged; it just chooses from the legal survivors.
    if legality is not None:
        allowed = _legal_support_keys(legality, allowed, insert_offset)
    if not allowed:
        return None

    role_key = _select_role_key(
        athlete_model,
        insert_offset,
        allowed,
        usage_ledger=usage_ledger,
        gap_span=gap_span,
        force_tactical=force_tactical,
        force_conditioning=force_conditioning,
        coverage_state=build_target_coverage_state(
            athlete_model,
            scheduled_roles,
        ),
    )
    if role_key is None:
        return None
    role = _build_insert_role(
        role_key,
        athlete_model,
        insert_offset,
        usage_ledger=usage_ledger,
    )
    if role_key == "footwork_walkthrough":
        _apply_bank_footwork(role, athlete_model, insert_offset, usage_ledger)
    return role


def _role_offset(role: dict[str, Any]) -> int | None:
    value = role.get("countdown_offset")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            pass
    label = str(role.get("scheduled_countdown_label") or role.get("countdown_label") or "")
    return _countdown_offset(label)


def _segment_for_offset(offset: int) -> int:
    return max(0, (offset - 1) // 7)


def _select_non_physical_insert(
    athlete_model: dict[str, Any],
    insert_offset: int,
    *,
    on_hard_sparring_day: bool,
    usage_ledger: dict[str, Any] | None = None,
    gap_span: int | None = None,
    force_tactical: bool = False,
    legality: CalendarLegalityView | None = None,
    scheduled_roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    allowed = _allowed_inserts(
        athlete_model,
        insert_offset,
        on_hard_sparring_day=on_hard_sparring_day,
    ) - PHYSICAL_INSERTS
    if legality is not None:
        allowed = _legal_support_keys(legality, allowed, insert_offset)
    role_key = _select_role_key(
        athlete_model,
        insert_offset,
        allowed,
        usage_ledger=usage_ledger,
        gap_span=gap_span,
        force_tactical=force_tactical,
        coverage_state=build_target_coverage_state(
            athlete_model,
            scheduled_roles,
        ),
    )
    if not role_key:
        return None
    return _build_insert_role(
        role_key,
        athlete_model,
        insert_offset,
        usage_ledger=usage_ledger,
    )


def _nearest_available_offset(target: int, available: list[int], chosen: set[int]) -> int | None:
    candidates = [offset for offset in available if offset not in chosen]
    if not candidates:
        return None
    return min(candidates, key=lambda offset: (abs(offset - target), -offset))


def _gap_candidate_offsets(
    far_offset: int,
    near_offset: int,
    *,
    eligible_offsets: set[int] | None = None,
) -> list[int]:
    gap = far_offset - near_offset
    if gap < GAP_FILL_MIN_DAYS:
        return []
    available = [
        offset
        for offset in range(far_offset - 1, near_offset, -1)
        if offset > 0 and (eligible_offsets is None or offset in eligible_offsets)
    ]
    if not available:
        return []

    target_count = 2 if gap >= TWO_INSERT_GAP_MIN_DAYS else 1
    if target_count == 1:
        targets = [near_offset + max(1, gap // 2)]
    else:
        targets = [
            near_offset + max(1, (gap * 2) // 3),
            near_offset + max(1, gap // 3),
        ]

    chosen: set[int] = set()
    for target in targets:
        selected = _nearest_available_offset(target, available, chosen)
        if selected is not None:
            chosen.add(selected)
    return sorted(chosen, reverse=True)


def _candidate_offsets_from_sequence(
    offsets: list[int],
    days_until_fight: int | None = None,
    *,
    eligible_offsets: set[int] | None = None,
) -> list[tuple[int, int]]:
    candidate_offsets: list[tuple[int, int]] = []

    # Leading span: the days between the start of the window and the FIRST
    # session. Only the gaps *between* sessions and the trailing run down to
    # fight day used to be candidates, so on a short camp whose sessions are
    # back-loaded (D-6 placing both at D-3 and D-1) the opening days were
    # structurally unreachable — no insert could land there however light, and
    # the athlete opened the app to a run of blank days.
    #
    # Scoped to the late-fight taper window: that is where the session budget is
    # tight enough to strand the front of the plan, and it keeps longer camps on
    # their existing shape. _gap_candidate_offsets excludes its far endpoint, so
    # passing days_until_fight + 1 makes the window's own first day eligible.
    if (
        days_until_fight is not None
        and 0 < days_until_fight <= LEADING_SPAN_MAX_DAYS_UNTIL_FIGHT
        and offsets
    ):
        first_offset = max(offsets)
        if days_until_fight > first_offset:
            leading_gap = days_until_fight - first_offset
            for target_offset in _gap_candidate_offsets(days_until_fight + 1, first_offset):
                candidate_offsets.append((target_offset, leading_gap))

    for far_offset, near_offset in zip(offsets, offsets[1:]):
        gap = far_offset - near_offset
        # Between two authoritative sessions, choose targets from days the
        # athlete can actually train. Leading/trailing behaviour is unchanged.
        for target_offset in _gap_candidate_offsets(
            far_offset,
            near_offset,
            eligible_offsets=eligible_offsets,
        ):
            candidate_offsets.append((target_offset, gap))

    trailing_gap = min(offsets)
    for target_offset in _gap_candidate_offsets(trailing_gap, 0):
        candidate_offsets.append((target_offset, trailing_gap))

    return candidate_offsets




def _has_tactical_support(session_sequence: list[dict[str, Any]]) -> bool:
    return any(str(role.get("role_key") or "") in TACTICAL_INSERTS for role in session_sequence)

def _ensure_weekly_tactical_watches(
    session_sequence: list[dict[str, Any]],
    athlete_model: dict[str, Any],
    countdown_map: dict[str, str],
    usage_ledger: dict[str, Any],
) -> list[dict[str, Any]]:
    """Add one banked Tactical Watch per visible seven-day countdown segment.

    The watch always shares a day already present in the countdown sequence, so
    this requirement adds a zero-load card, not a new training day.
    """
    if not _is_fight_sport(athlete_model):
        return []

    additions: list[dict[str, Any]] = []
    segments = sorted(
        {
            _segment_for_offset(offset)
            for role in session_sequence
            if (offset := _role_offset(role)) is not None and 0 < offset <= 21
        }
    )
    used_watch_keys = usage_ledger.setdefault("used_tactical_watch_keys", set())

    for segment in segments:
        segment_roles = [
            role
            for role in session_sequence
            if (offset := _role_offset(role)) is not None
            and offset > 0
            and _segment_for_offset(offset) == segment
        ]
        watch = next(
            (
                role
                for role in segment_roles
                if str(role.get("role_key") or "") == "tactical_watch"
            ),
            None,
        )
        if watch is None:
            # De-dup: anchor the mandatory watch on a day that does not already
            # carry a tactical insert, so it never stacks a second tactical touch
            # onto a day the gap-fill already served with a tactical cue card.
            # Tactical work may still repeat across different days.
            non_tactical_roles = [
                role
                for role in segment_roles
                if str(role.get("role_key") or "") not in TACTICAL_INSERTS
            ]
            anchor_pool = non_tactical_roles or segment_roles
            anchor = max(anchor_pool, key=lambda role: int(_role_offset(role) or 0))
            offset = int(_role_offset(anchor) or 0)
            weekday = str(
                anchor.get("scheduled_day_hint")
                or anchor.get("real_weekday")
                or countdown_map.get(f"D-{offset}")
                or ""
            ).strip() or None
            watch = _build_insert_role(
                "tactical_watch",
                athlete_model,
                offset,
                weekday,
                usage_ledger=usage_ledger,
            )
            if watch is None:
                continue
            additions.append(watch)
            _record_insert_usage(usage_ledger, "tactical_watch", offset)
        elif not watch.get("tactical_watch_key"):
            offset = int(_role_offset(watch) or 0)
            if not _apply_bank_watch(
                watch,
                athlete_model,
                phase=_watch_phase_for_offset(athlete_model, offset),
                used_watch_keys=used_watch_keys,
            ):
                continue

        watch["mandatory_tactical_watch"] = True
        watch["weekly_requirement"] = "fight_tactical_watch"
        watch["governance"] = {
            **dict(watch.get("governance") or {}),
            "mandatory": True,
            "meaningful_stress": False,
        }

    return additions


def _offsets_owning(sequence: list[dict[str, Any]], role_key: str) -> set[int]:
    return {
        offset
        for role in sequence
        if str(role.get("role_key") or "") == role_key
        and (offset := _role_offset(role)) is not None
    }


def _finalize_sequence(combined: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply same-day de-duplication, then order and index the sequence.

    Shared by both exits of ``apply_gap_fill_inserts`` so a plan that returns
    early (no sessions, or no positive countdown offset) still gets the same
    de-duplication and indexing as a fully gap-filled one.
    """
    # A day carrying the mandatory Tactical Watch must not also keep a redundant
    # gap-fill tactical cue card / self review. Single-day countdown windows (for
    # example D-1) can only place both on the one available day, so this pass
    # drops the lower-priority tactical insert there. Tactical work on other days
    # is untouched.
    watch_offsets = _offsets_owning(combined, "tactical_watch")
    redundant_tactical = TACTICAL_INSERTS - {"tactical_watch"}
    combined = [
        role
        for role in combined
        if not (
            str(role.get("role_key") or "") in redundant_tactical
            and _role_offset(role) in watch_offsets
        )
    ]

    # The governed D-x Fight Visualisation wins over the generic mental filler.
    # ``neural_visualization`` stays available on every other day.
    visualization_offsets = _offsets_owning(combined, FIGHT_VISUALIZATION_ROLE_KEY)
    combined = [
        role
        for role in combined
        if not (
            str(role.get("role_key") or "") == "neural_visualization"
            and _role_offset(role) in visualization_offsets
        )
    ]

    final_sequence = sorted(
        combined, key=lambda role: int(_role_offset(role) or 0), reverse=True
    )
    for index, role in enumerate(final_sequence, start=1):
        role["session_index"] = index
    return final_sequence


def apply_gap_fill_inserts(
    session_sequence: list[dict[str, Any]],
    athlete_model: dict[str, Any],
    *,
    resolved_contacts: list[tuple[int, str]] | None = None,
) -> list[dict[str, Any]]:
    ordered = sorted(
        [dict(role) for role in session_sequence],
        key=lambda role: int(_role_offset(role) or 0),
        reverse=True,
    )
    offsets = [offset for role in ordered if (offset := _role_offset(role)) is not None and offset > 0]

    raw_days = athlete_model.get("days_until_fight")
    days_until_fight: int | None = None
    if raw_days is not None and str(raw_days).strip() != "":
        try:
            days_until_fight = int(raw_days)
        except (TypeError, ValueError):
            days_until_fight = None
    if days_until_fight is None:
        days_until_fight = max(offsets) if offsets else None
    if days_until_fight is None:
        # Nothing anchors a countdown, so neither the protocol nor the gap fill
        # has a day to place anything on.
        return _finalize_sequence(govern_sequence_mental_training(
            ordered, athlete_model, countdown_map={},
        ))

    creation_weekday = _resolve_plan_creation_weekday(days_until_fight, athlete_model)
    countdown_map = _countdown_weekday_map(creation_weekday, days_until_fight)
    usage_ledger = _usage_ledger_from_sequence(ordered)

    # The mandatory countdown protocol runs BEFORE the ordinary gap-fill early
    # returns. It is not a filler: "never omitted" has to hold for a plan whose
    # Stage 1 sequence is empty or carries no positive countdown offset (a
    # D-0-only plan), which is exactly where the gap fill has nothing to do.
    mandatory_inserts = _ensure_fight_visualization_days(
        ordered,
        athlete_model,
        countdown_map,
        days_until_fight,
        usage_ledger,
    )

    # Now the ordinary gap fill may bail out: it needs real sessions with real
    # positive offsets to find gaps between.
    if not ordered or not offsets:
        return _finalize_sequence(govern_sequence_mental_training(
            ordered + mandatory_inserts, athlete_model, countdown_map=countdown_map,
        ))

    training_days = clean_list(athlete_model.get("training_days", []))
    # Resolved contact truth (hard vs technical) is OWNED by the late-fight
    # module: gap-fill consumes the resolver's occurrences rather than deriving
    # or re-classifying contact itself. Callers may inject the resolved contacts;
    # otherwise the late-fight owner resolves them for this athlete/window.
    if resolved_contacts is None:
        resolved_contacts = resolve_late_fight_contacts(days_until_fight, athlete_model)
    else:
        resolved_contacts = list(resolved_contacts)
    # Derive existing contact days from the SAME canonical interpretation the
    # shared legality view uses (``calendar_context``): a suppressed / off / none
    # resolved load carries no contact, so it must not become a coach-day tactical
    # target or mark a day as an exclusive hard-sparring day. This keeps the filler
    # pre-check and the final governor reading one existing-contact set.
    contact_offsets = resolved_contact_offsets(resolved_contacts)

    existing_exclusive_offsets = {
        offset
        for role in ordered
        if str(role.get("role_key") or "") != "hard_sparring_day"
        and not is_low_cost_coexistable_filler(role)
        and (offset := _role_offset(role)) is not None
    }
    # Choose between-session gap targets from legal declared training days
    # before nearest-target placement. This prevents an unavailable geometric
    # target from being discarded when another valid day exists in the gap.
    eligible_gap_offsets = {
        offset
        for offset in range(1, max(days_until_fight, max(offsets)) + 1)
        if offset not in existing_exclusive_offsets
        and can_render_late_taper_day(
            countdown_offset=offset,
            weekday=str(countdown_map.get(f"D-{offset}") or ""),
            training_days=training_days,
        )
    }
    candidate_offsets = _candidate_offsets_from_sequence(
        offsets,
        days_until_fight,
        eligible_offsets=eligible_gap_offsets,
    )

    inserts: list[dict[str, Any]] = list(mandatory_inserts)
    physical_segment_counts: dict[int, int] = {}
    for role in ordered:
        role_key = str(role.get("role_key") or "")
        offset = _role_offset(role)
        if role_key in PHYSICAL_INSERTS and offset is not None:
            segment = _segment_for_offset(offset)
            physical_segment_counts[segment] = physical_segment_counts.get(segment, 0) + 1

    tactical_present = _has_tactical_support(ordered)
    # Never stack two tactical support inserts on the SAME day: the mandatory
    # Tactical Watch already occupies its day, so the gap-fill must not add a
    # second tactical insert (e.g. a tactical cue card) onto a day that already
    # carries one. Tactical work may still repeat across different days.
    tactical_offsets = {
        offset
        for role in ordered
        if str(role.get("role_key") or "") in TACTICAL_INSERTS
        and (offset := _role_offset(role)) is not None
        and offset > 0
    }
    tactical_required = _is_fight_sport(athlete_model) and not tactical_present
    # Coach combat days come from resolved contact (hard + technical occurrences),
    # so tactical support still attaches to declared combat days without matching
    # raw weekday names.
    coach_day_candidates = [
        (offset, 0)
        for offset in sorted(contact_offsets, reverse=True)
        if offset not in existing_exclusive_offsets
    ]
    if tactical_required:
        candidate_offsets = coach_day_candidates + candidate_offsets
    else:
        candidate_offsets = candidate_offsets + coach_day_candidates

    for target_offset, gap_span in candidate_offsets:
        if target_offset <= 0 or target_offset in existing_exclusive_offsets:
            continue
        weekday = countdown_map.get(f"D-{target_offset}")
        if not can_render_late_taper_day(
            countdown_offset=target_offset,
            weekday=str(weekday or ""),
            training_days=training_days,
        ):
            continue
        # Same shared calendar the final governor verifies, rebuilt over the
        # sequence plus already-placed inserts and the resolved contact events.
        legality = sequence_legality(
            ordered + inserts,
            resolved_contacts=resolved_contacts,
        )
        # A hard/technical contact owns its day exclusively: derive it from the
        # resolved contact set, not raw declared weekday names.
        on_hard_sparring_day = target_offset in contact_offsets
        force_tactical = tactical_required and not tactical_present
        if budgeted_insert_count(inserts) >= MAX_INSERTS_TOTAL_D21_TO_D0 and not force_tactical:
            break
        insert = select_gap_fill_insert(
            athlete_model,
            target_offset,
            on_hard_sparring_day=on_hard_sparring_day,
            usage_ledger=usage_ledger,
            gap_span=gap_span,
            force_tactical=force_tactical,
            legality=legality,
            scheduled_roles=ordered + inserts,
        )
        if insert is None:
            continue
        # Skip a tactical filler whose day already carries a tactical insert: the
        # mandatory Tactical Watch owns that day, so a second tactical insert on
        # the same day is a duplicate.
        if (
            str(insert.get("role_key") or "") in TACTICAL_INSERTS
            and target_offset in tactical_offsets
        ):
            continue
        if insert["role_key"] in PHYSICAL_INSERTS:
            segment = _segment_for_offset(target_offset)
            if physical_segment_counts.get(segment, 0) >= MAX_PHYSICAL_INSERTS_PER_7_DAY_SEGMENT:
                insert = _select_non_physical_insert(
                    athlete_model,
                    target_offset,
                    on_hard_sparring_day=on_hard_sparring_day,
                    usage_ledger=usage_ledger,
                    gap_span=gap_span,
                    force_tactical=force_tactical,
                    legality=legality,
                    scheduled_roles=ordered + inserts,
                )
                if insert is None:
                    continue
        insert["scheduled_day_hint"] = weekday
        if weekday:
            insert["real_weekday"] = weekday
            insert["countdown_display_label"] = f"D-{target_offset} ({weekday.title()})"

        # A zero-cost insert spends nothing, so it needs no allowance and no
        # reserved slot for the mandatory watches that are themselves zero-cost.
        if consumes_insert_budget(insert.get("role_key")):
            if budgeted_insert_count(inserts) + 1 > MAX_INSERTS_TOTAL_D21_TO_D0:
                continue

        if insert["role_key"] in PHYSICAL_INSERTS:
            segment = _segment_for_offset(target_offset)
            physical_segment_counts[segment] = physical_segment_counts.get(segment, 0) + 1
        inserts.append(insert)
        _record_insert_usage(usage_ledger, str(insert.get("role_key") or ""), target_offset)
        if insert.get("role_key") in TACTICAL_INSERTS:
            tactical_present = True
            tactical_offsets.add(target_offset)
        if not is_low_cost_coexistable_filler(insert):
            existing_exclusive_offsets.add(target_offset)

    inserts.extend(
        _ensure_weekly_tactical_watches(
            ordered + inserts,
            athlete_model,
            countdown_map,
            usage_ledger,
        )
    )
    inserts.extend(
        _ensure_late_camp_visualizations(
            ordered + inserts,
            athlete_model,
            countdown_map,
            days_until_fight,
        )
    )
    # Re-run the protocol over the finished sequence. It already placed every
    # countdown day above; this catches a Stage 1 sequence that carried its own
    # Fight Visualisation role and restamps it from the current athlete.
    inserts.extend(
        _ensure_fight_visualization_days(
            ordered + inserts,
            athlete_model,
            countdown_map,
            days_until_fight,
            usage_ledger,
        )
    )
    combined = govern_sequence_mental_training(
        ordered + inserts,
        athlete_model,
        countdown_map=countdown_map,
        contact_offsets=contact_offsets,
    )
    return _finalize_sequence(combined)


def govern_sequence_mental_training(
    sequence: list[dict[str, Any]],
    athlete_model: dict[str, Any],
    *,
    countdown_map: dict[str, str],
    contact_offsets: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Apply the shared mental-training frequency policy to a flat sequence.

    Runs after every placer in this module (weekly watches, late camp
    visualisations, the countdown protocol and the gap fill), so none of them
    can restore a task the policy removed.
    """
    fight_weekday = WEEKDAY_ORDER.get(str(countdown_map.get("D-0") or "").strip().lower())

    def relocate(role: dict[str, Any], offset: int) -> None:
        _move_role_to_offset(role, offset, countdown_map.get(f"D-{offset}"))

    removed = govern_mental_roles(
        sequence,
        athlete_model,
        offset_of=_role_offset,
        bucket_of=lambda _role, offset: calendar_week_bucket(offset, fight_weekday),
        contact_offsets=contact_offsets or (),
        relocate=relocate,
    )
    return [role for role in sequence if id(role) not in removed]


def _move_role_to_offset(role: dict[str, Any], offset: int, weekday: Any) -> None:
    role["countdown_offset"] = offset
    role["countdown_label"] = f"D-{offset}"
    role["scheduled_countdown_label"] = f"D-{offset}"
    day = str(weekday or "").strip()
    if day:
        role["scheduled_day_hint"] = day
        role["real_weekday"] = day
        role["countdown_display_label"] = f"D-{offset} ({day.title()})"
    else:
        role.pop("countdown_display_label", None)


# --- Physical-occupancy completion -------------------------------------------
#
# ``training_frequency`` is a physical-session count (see
# ``fightcamp.physical_session_frequency``). The gap-fill pass above is driven by
# calendar *geometry* — it closes long empty stretches — and is deliberately
# budgeted (``MAX_INSERTS_TOTAL_D21_TO_D0``,
# ``MAX_PHYSICAL_INSERTS_PER_7_DAY_SEGMENT``) so a normal week is not padded out.
# Occupancy completion answers a different question: a *declared* training day is
# sitting empty while the athlete asked for a session on it. It therefore selects
# from the same deterministic insert banks, but only from options that count as
# physical work, and only for days the athlete actually declared.

#: Inserts that satisfy a physical training session.
PHYSICAL_OCCUPANCY_INSERTS = PHYSICAL_INSERTS | LOW_COST_AEROBIC_INSERTS

#: Low-cost technical/movement work that ``_allowed_inserts`` strips purely as a
#: *freshness preference* while a healthy athlete is cutting weight (the strip is
#: guarded by ``injury_state in {"none", "mild_stable"}``, so it is not an injury
#: gate). A cut is a reason to make a session cheaper, not to delete a declared
#: training day, so occupancy completion may fall back to these when the cut
#: strip is the only thing standing between a declared day and a legal session.
_CUT_PREFERENCE_STRIPPED_INSERTS = {
    "technical_shadow_rhythm",
    "footwork_walkthrough",
    "movement_quality",
}


def _physical_occupancy_candidates(
    athlete_model: dict[str, Any],
    insert_offset: int,
    *,
    on_hard_sparring_day: bool,
) -> tuple[set[str], set[str]]:
    """Return ``(preferred, cut_relaxed)`` physical options for this day."""
    preferred = _allowed_inserts(
        athlete_model,
        insert_offset,
        on_hard_sparring_day=on_hard_sparring_day,
    ) & PHYSICAL_OCCUPANCY_INSERTS
    # Relax only the weight-cut freshness preference, and only while every hard
    # gate still says a low-cost session is fine.
    if (
        not _has_active_weight_cut(athlete_model)
        or _has_high_fatigue(athlete_model)
        or insert_offset <= 1
        or on_hard_sparring_day
        or classify_injury_state(athlete_model) not in {"none", "mild_stable"}
    ):
        return preferred, set()
    return preferred, set(_CUT_PREFERENCE_STRIPPED_INSERTS) - preferred


def select_physical_occupancy_insert(
    athlete_model: dict[str, Any],
    insert_offset: int,
    *,
    on_hard_sparring_day: bool = False,
    usage_ledger: dict[str, Any] | None = None,
    legality: CalendarLegalityView | None = None,
    scheduled_roles: list[dict[str, Any]] | None = None,
    allow_repeat: bool = True,
) -> dict[str, Any] | None:
    """Return one low-cost *physical* insert for a declared day left empty.

    Returns ``None`` whenever no legal physical option exists — the caller then
    records the day in ``intentionally_unused_days`` with a deterministic reason
    rather than inventing filler. D-0 and D-1 are never filled here: fight day is
    not training, and D-1 governance stays with the late-fight safety rules.
    """
    if insert_offset <= 1:
        return None

    preferred, relaxed = _physical_occupancy_candidates(
        athlete_model,
        insert_offset,
        on_hard_sparring_day=on_hard_sparring_day,
    )
    if legality is not None:
        preferred = _legal_support_keys(legality, preferred, insert_offset)
        relaxed = _legal_support_keys(legality, relaxed, insert_offset)
    if not preferred and not relaxed:
        return None

    coverage_state = build_target_coverage_state(athlete_model, scheduled_roles)

    def _pick(candidates: set[str], ledger: dict[str, Any] | None) -> str | None:
        if not candidates:
            return None
        return _select_role_key(
            athlete_model,
            insert_offset,
            candidates,
            usage_ledger=ledger,
            coverage_state=coverage_state,
        )

    # Variety before repetition: an unused option from either tier beats reusing
    # one this window already spent. Repeating is still better than deleting a
    # declared training day, so it is the last resort rather than a refusal.
    role_key = _pick(preferred, usage_ledger) or _pick(relaxed, usage_ledger)
    if role_key is None and allow_repeat:
        role_key = _pick(preferred, None) or _pick(relaxed, None)
    if role_key is None:
        return None

    role = _build_insert_role(role_key, athlete_model, insert_offset, usage_ledger=usage_ledger)
    if role is None:
        return None
    if role_key == "footwork_walkthrough":
        _apply_bank_footwork(role, athlete_model, insert_offset, usage_ledger)
    role["physical_occupancy_fill"] = True
    return role
