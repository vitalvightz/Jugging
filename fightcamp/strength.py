import logging
import os
import hashlib
import json
import random
import re
from time import perf_counter
from types import SimpleNamespace
from collections import defaultdict
from .training_context import (
    normalize_athlete_equipment_list,
    normalize_equipment_list,
    allocate_sessions,
    calculate_exercise_numbers,
)
from .bank_schema import NON_EQUIPMENT_TOKENS, is_late_fight_metadata_safe, validate_training_item
from .tagging import normalize_item_tags, normalize_tag, normalize_tags
from .tag_maps import GOAL_PRIORITY_TAG_MAP, STYLE_TAG_MAP, priority_adaptation
# Refactored: Import centralized constants from config
from .config import PHASE_EQUIPMENT_BOOST, PHASE_TAG_BOOST, DATA_DIR, INJURY_GUARD_SHORTLIST
from .injury_filtering import (
    _log_exclusion,
    _log_replacement,
    injury_match_details,
)
# Refactored: Import factory function for guarded decision making
from .injury_guard import Decision, pick_safe_replacement, make_guarded_decision_factory
from .restriction_filtering import evaluate_restriction_impact
from .strength_session_quality import (
    classify_strength_item,
    infer_strength_sessions,
    missing_base_categories,
    score_band_margin,
    support_budget_cost,
    session_starts_with_support_only,
    session_support_count_before_anchor,
    strength_adaptation_relevance,
    strength_quality_adjustment,
)
from .session_restraint import NEAR_EQUAL_SCORE_BAND, sort_weighted_candidates
from .late_selector_windows import (
    CONTROL_D28,
    D21_TO_D14,
    D13_TO_D8,
    D1,
    D4_TO_D2,
    D6_TO_D5,
    D7,
    classify_late_selector_window,
    coerce_days_until_fight,
    is_active_late_selector_window,
)
from .normalization import normalize_fight_format as _normalize_fight_format
from .selection_metadata import build_score_evidence, normalize_selection_metadata
from .weight_cut import compute_cut_severity_score, cut_health_bucket
from .priority_clarification_tags import derive_clarification_tags
from .stage1_fail_safe import bounded_max_iterations, log_fail_safe_degrade
from .priority_profile import (
    PRIMARY_GOAL_WEIGHT,
    PRIMARY_WEAKNESS_WEIGHT,
    build_priority_profile,
    goal_priority_weight,
    is_priority_collision_tag,
    total_strength_collision_safe_priority_bonus,
    weakness_priority_weight,
)

logger = logging.getLogger(__name__)

_exercise_bank_cache = None

# Equivalent catalogue rows should offer alternatives, not consume two slots.
# Keep this deliberately narrow: a shared hinge/squat pattern is not redundancy
# when the dose is unilateral, isometric, ballistic or a contrast pair.
_SELECTION_ALIASES = {
    "Romanian Deadlift (RDL)": "Romanian Deadlift (DB)",
    "Lat Pulldown Machine": "Lat Pulldown",
    "Clinch Knee Strike (Band-Resisted)": "Band-Resisted Clinch Knee Strike",
}


def _selection_identity(exercise: dict) -> tuple[str, str]:
    name = str(exercise.get("name") or "")
    return (_SELECTION_ALIASES.get(name, name), str(exercise.get("method") or "").lower())









SESSION_SUPPORT_CAP_MULTIPLIER = 2
CORE_BALANCE_PRIORITY_TOKENS = {
    "core",
    "core_stability",
    "core_strength",
    "trunk",
    "trunk_strength",
    "anti_rotation",
    "balance",
    "stability",
}
LOWER_BODY_EXPLOSIVE_PRIORITY_TOKENS = {
    "power",
    "explosive_power",
    "rate_of_force",
}


def _has_core_balance_priority(flags: dict, weaknesses) -> bool:
    raw_values: list[str] = []

    def add_values(value) -> None:
        if isinstance(value, (list, tuple, set)):
            raw_values.extend(str(item) for item in value if str(item).strip())
        elif value is not None and str(value).strip():
            raw_values.append(str(value))

    add_values(weaknesses)
    for key in ("weaknesses", "key_goals", "primary_goal", "primary_weak_area"):
        add_values(flags.get(key))

    normalized = {normalize_tag(value) for value in raw_values}
    normalized.discard(None)
    return bool(normalized & CORE_BALANCE_PRIORITY_TOKENS)


def _has_lower_body_explosive_priority(flags: dict, weaknesses) -> bool:
    raw_values: list[str] = []

    def add_values(value) -> None:
        if isinstance(value, (list, tuple, set)):
            raw_values.extend(str(item) for item in value if str(item).strip())
        elif value is not None and str(value).strip():
            raw_values.append(str(value))

    add_values(weaknesses)
    for key in ("weaknesses", "key_goals", "primary_goal", "primary_weak_area"):
        add_values(flags.get(key))

    normalized = {normalize_tag(value) for value in raw_values}
    normalized.discard(None)
    return bool(normalized & LOWER_BODY_EXPLOSIVE_PRIORITY_TOKENS)


FATIGUE_COST_BY_QUALITY_CLASS = {
    "anchor_loaded": 3.0,
    "anchor_power": 2.5,
    "anchor_force_isometric": 2.0,
    "support_isometric": 1.5,
    "support_accessory": 1.0,
    "rehab_support": 0.5,
}

LATE_STRENGTH_DENYLIST = {
    "EMOM: 5 Squat Cleans + 5 Burpees",
    "Jump Lunge (Alternating)",
    "Jumping Lunge",
}
LATE_STRENGTH_SAFE_TAGS = {
    "neural_primer",
    "speed",
    "reactive",
    "low_impact",
    "low_eccentric",
    "rehab_friendly",
    "cns_freshness",
    "sharpness",
    "ballistic_low_volume",
    "sport_specific",
    "late_strength_touch",
    "maximal_strength_maintenance",
}
LATE_STRENGTH_TIGHT_WINDOWS = {D7, D6_TO_D5, D4_TO_D2, D1}
EARLY_TAPER_STRENGTH_WINDOWS = {D21_TO_D14, D13_TO_D8}
STRENGTH_MAINTENANCE_INTENT_TAGS = {
    "strength",
    "maximal_strength",
    "maximal_strength_maintenance",
}
STRENGTH_MAINTENANCE_MATCH_TAGS = {
    "late_strength_touch",
    "maximal_strength_maintenance",
}
PRIMER_ONLY_STRENGTH_TOUCH_TAGS = {
    "neural_primer",
    "speed",
    "reactive",
    "mobility",
    "rehab_support",
    "support_accessory",
}
STRENGTH_MAINTENANCE_QUALITY_CLASSES = {
    "anchor_loaded",
    "anchor_force_isometric",
    "anchor_power",
}
STRENGTH_MAINTENANCE_SUPPORT_TAGS = {
    "low_eccentric",
    "cns_freshness",
    "low_impact",
}
# Safety-critical hard blocks apply across every active late window where their
# condition fires (e.g. D13-D8), not just the tight windows. They must not be
# dropped by the tight-window block filter.
LATE_STRENGTH_SAFETY_CRITICAL_BLOCKS = frozenset(
    {
        "late_strength_block_high_cut_balance_risk",
        "late_strength_block_familiarity_required_late",
    }
)
LATE_STRENGTH_TRUE_CLUSTER_BAND = 0.05
# Bias applied to posterior-chain-specific late-touches when posterior_chain is a
# stated weakness, so they win the posterior-chain slot ahead of quad-dominant
# isometrics that merely also carry the posterior_chain tag.
POSTERIOR_CHAIN_LATE_TOUCH_BIAS = 1.0
LATE_SAFE_STRENGTH_FIELDS = {
    "late_strength_touch",
    "low_eccentric",
    "low_impact",
    "cns_freshness",
}
LATE_MUST_HAVE_BONUS_MULTIPLIER = 0.6
VALID_CUT_BUCKETS = {"none", "low", "moderate", "high", "critical", "extreme"}
STRENGTH_CLARIFICATION_TAG_BONUS = 0.25
STRENGTH_MAX_CLARIFICATION_TAG_BONUS = 0.75
LATE_STRENGTH_HIGH_CUT_BUCKETS = {"high", "critical", "extreme"}
LATE_STRENGTH_CUT_BUCKET_MULTIPLIER = {
    "none": 0.0,
    "low": 0.0,
    "moderate": 0.35,
    "high": 1.0,
    "critical": 1.2,
    "extreme": 1.4,
}

# Trap-bar-vs-back-squat anchor preference.
# When any risk condition fires (a flare in a joint loaded by the squat bar
# position, an active weight cut, moderate/high fatigue, a compressed camp, or
# poor squat tolerance) the trap-bar deadlift is preferred over a barbell
# back-squat-pattern anchor: barbell squat anchors are penalised and the
# trap-bar hinge anchor is boosted. The magnitudes express a *preference* — a
# strong goal/weakness fit can still override it — rather than a hard block.
# Joints loaded by the back-/front-rack bar position that a trap-bar deadlift
# spares; a flare in any of them biases selection toward the trap-bar hinge.
SQUAT_BAR_POSITION_INJURY_REGIONS = ("wrist", "shoulder", "elbow", "neck")
# Weight-cut buckets treated as an "active cut" for the preference rule.
TRAP_BAR_PREFERENCE_ACTIVE_CUT_BUCKETS = {"moderate", "high", "critical", "extreme"}
# Total camp days at or below this are treated as a compressed camp when no
# explicit ``camp_compressed`` flag is supplied (matches the short-notice
# compressed band in ``camp_phases``).
COMPRESSED_CAMP_DAYS_THRESHOLD = 13
# Squat-tolerance levels that count as "poor" when no explicit
# ``poor_squat_tolerance`` boolean is supplied.
POOR_SQUAT_TOLERANCE_LEVELS = {"poor", "low", "limited", "bad"}
TRAP_BAR_PREFERENCE_SQUAT_PENALTY = -0.6
TRAP_BAR_PREFERENCE_HINGE_BOOST = 0.4

# Camp-level rotational-power coverage.
# Rotational power is its own power family, but nothing makes the planner notice
# when a whole camp's development phases have produced no exposure to it. When
# the caller reports that gap, safe rotational-power anchors get a modest
# selection boost — a preference, not a requirement: equipment, injury state,
# fatigue, and a stronger goal/weakness fit can all still win.
ROTATIONAL_POWER_COVERAGE_BOOST = 0.35


def _exercise_fatigue_cost(exercise: dict, quality_profile: dict) -> float:
    """Return a small recovery-cost proxy for near-equal Rule 2 ordering.

    The exercise bank does not currently expose a native ``fatigue_cost`` field, so
    the restraint layer derives a deterministic proxy from existing planner signals
    instead of defaulting every candidate to zero. The proxy stays intentionally
    small and only helps break near-equal score groups; scoring remains primary.
    """
    tags = set(normalize_tags(exercise.get("tags", [])))
    equipment = set(normalize_equipment_list(exercise.get("equipment", [])))
    fatigue_cost = FATIGUE_COST_BY_QUALITY_CLASS.get(quality_profile.get("quality_class"), 1.0)

    if "high_volume" in tags:
        fatigue_cost += 1.0
    if tags & {"eccentric", "plyometric", "contrast_pairing", "triple_extension"}:
        fatigue_cost += 0.5
    if equipment & {"barbell", "trap_bar", "sandbag", "atlas_stone", "log"}:
        fatigue_cost += 0.5

    return round(fatigue_cost, 2)


def _seeded_tie_rank(seed, name: str) -> int:
    """Tie order for one exercise under one seed.

    Keyed on the exercise itself rather than its position in a shuffled bank,
    so adding or removing an unrelated row never reorders other tied picks.
    """
    digest = hashlib.sha256(f"{seed}:{name}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _strength_late_window_severity(window: str | None) -> float:
    return {
        "d21_to_d14": 0.55,
        "d13_to_d8": 0.8,
        D7: 1.0,
        D6_TO_D5: 1.15,
        D4_TO_D2: 1.25,
        D1: 1.35,
    }.get(window, 0.0)


def _normalized_metadata_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_values = [part for part in re.split(r"[,\s]+", value) if part]
    elif isinstance(value, (list, tuple, set)):
        raw_values = [str(part) for part in value if str(part).strip()]
    else:
        raw_values = [str(value)]

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        canonical = normalize_tag(raw)
        if not canonical:
            continue
        if canonical in seen:
            continue
        normalized.append(canonical)
        seen.add(canonical)
    return normalized


def _exercise_late_windows(exercise: dict) -> set[str]:
    return set(_normalized_metadata_list(exercise.get("late_windows")))


def _exercise_phase_role(exercise: dict) -> str:
    return normalize_tag(str(exercise.get("phase_role") or ""))


def _exercise_cut_buckets_allowed(exercise: dict) -> set[str]:
    return {
        bucket
        for bucket in _normalized_metadata_list(exercise.get("cut_buckets_allowed"))
        if bucket in VALID_CUT_BUCKETS
    }


def _has_strength_maintenance_intent(*, goals, weaknesses, flags: dict) -> bool:
    raw_values: list[str] = []
    for value in goals or []:
        raw_values.append(str(value))
    for value in weaknesses or []:
        raw_values.append(str(value))
    for value in flags.get("weaknesses") or []:
        raw_values.append(str(value))
    for field in ("primary_goal", "primary_weak_area"):
        value = flags.get(field)
        if value:
            raw_values.append(str(value))

    normalized_values = {normalize_tag(value) for value in raw_values if str(value).strip()}
    normalized_values.discard(None)
    if normalized_values & STRENGTH_MAINTENANCE_INTENT_TAGS:
        return True
    return any('strength' in value for value in normalized_values)


def _strength_maintenance_match_tags(exercise: dict) -> set[str]:
    tags = set(normalize_tags(exercise.get("tags", [])))
    matches = set(tags & STRENGTH_MAINTENANCE_MATCH_TAGS)
    phase_role = _exercise_phase_role(exercise)
    if phase_role in STRENGTH_MAINTENANCE_MATCH_TAGS:
        matches.add(phase_role)
    for field in STRENGTH_MAINTENANCE_MATCH_TAGS:
        if exercise.get(field) is True:
            matches.add(field)
    return matches


def _is_primer_only_strength_touch(exercise: dict, profile: dict) -> bool:
    tags = set(normalize_tags(exercise.get("tags", [])))
    if "maximal_strength_maintenance" in _strength_maintenance_match_tags(exercise):
        return False
    method = normalize_tag(str(exercise.get("method") or ""))
    quality_class = str(profile.get("quality_class") or "")
    has_primer_signal = bool(tags & PRIMER_ONLY_STRENGTH_TOUCH_TAGS)
    has_strength_method = method == "strength"
    has_real_anchor = quality_class in STRENGTH_MAINTENANCE_QUALITY_CLASSES
    return has_primer_signal and not has_strength_method and not (
        has_real_anchor and "late_strength_touch" in tags and "strength" in tags
    )


def _is_real_strength_maintenance_touch(exercise: dict, profile: dict) -> bool:
    if not _strength_maintenance_match_tags(exercise):
        return False
    if _is_primer_only_strength_touch(exercise, profile):
        return False
    if "maximal_strength_maintenance" in _strength_maintenance_match_tags(exercise):
        return True
    method = normalize_tag(str(exercise.get("method") or ""))
    return method == "strength" or profile.get("quality_class") in STRENGTH_MAINTENANCE_QUALITY_CLASSES


def _strength_maintenance_support_score(exercise: dict, profile: dict, *, window: str | None) -> int:
    tags = set(normalize_tags(exercise.get("tags", [])))
    support = len(tags & STRENGTH_MAINTENANCE_SUPPORT_TAGS)
    late_windows = _exercise_late_windows(exercise)
    if window and (window in late_windows or "all" in late_windows):
        support += 1
    if profile.get("quality_class") in STRENGTH_MAINTENANCE_QUALITY_CLASSES:
        support += 1
    if _exercise_phase_role(exercise) == "late_strength_touch":
        support += 1
    if "maximal_strength_maintenance" in _strength_maintenance_match_tags(exercise):
        support += 1
    return support


def _exercise_profile_flag(exercise: dict, tags: set[str], field_name: str, *, tag_name: str | None = None) -> bool:
    if exercise.get(field_name) is True:
        return True
    return (tag_name or field_name) in tags


def _exercise_profile_level(exercise: dict, field_name: str) -> str:
    value = exercise.get(field_name)
    return str(value or "").strip().lower()


def _has_explicit_profile_level(exercise: dict, field_name: str) -> bool:
    value = exercise.get(field_name)
    return value is not None and str(value).strip() != ""


def _low_cost_level(level: str) -> bool:
    return level in {"none", "low"}


def _high_cost_level(level: str) -> bool:
    return level in {"high", "very_high", "max"}


def _strength_text_blob(exercise: dict) -> str:
    fields = (
        exercise.get("name", ""),
        exercise.get("notes", ""),
        exercise.get("method", ""),
        exercise.get("movement", ""),
        exercise.get("type", ""),
        exercise.get("duration", ""),
        exercise.get("timing", ""),
    )
    return " ".join(str(field or "") for field in fields).strip().lower()


def _resolved_cut_severity_bucket(flags: dict) -> str:
    """Strain bucket driving strength dose/exercise caps.

    Reads the strain scale so that recalibrating capacity severity (which
    governs how many sessions a cut may delete) never weakens how much a cut
    trims strength dose.
    """
    explicit_bucket = str(
        flags.get("cut_health_bucket") or flags.get("cut_severity_bucket") or ""
    ).strip().lower()
    if explicit_bucket in VALID_CUT_BUCKETS:
        return explicit_bucket

    try:
        cut_score = float(flags.get("cut_severity_score"))
    except (TypeError, ValueError):
        cut_score = None
    if cut_score is not None:
        return cut_health_bucket(cut_score)

    return cut_health_bucket(
        compute_cut_severity_score(
            flags.get("weight_cut_pct"),
            flags.get("days_until_fight"),
        )
    )


def _camp_is_compressed(flags: dict) -> bool:
    """Return True when the camp is compressed.

    Prefers an explicit ``camp_compressed`` flag; otherwise falls back to the
    total camp length recorded in ``phase_weeks['days']`` and treats a camp at
    or below ``COMPRESSED_CAMP_DAYS_THRESHOLD`` days as compressed. ``days_until_fight``
    is deliberately not used as a proxy — it shrinks for every athlete during the
    taper, compressed camp or not.
    """
    explicit = flags.get("camp_compressed")
    if explicit is not None:
        return bool(explicit)
    phase_weeks = flags.get("phase_weeks")
    if not isinstance(phase_weeks, dict):
        return False
    days = phase_weeks.get("days")
    if not isinstance(days, dict) or not days:
        return False
    total_days = sum(v for v in days.values() if isinstance(v, (int, float)))
    return 0 < total_days <= COMPRESSED_CAMP_DAYS_THRESHOLD


def _squat_tolerance_is_poor(flags: dict) -> bool:
    """Return True when the athlete has poor squat tolerance.

    Optional signal that degrades gracefully: an explicit ``poor_squat_tolerance``
    boolean wins; otherwise a ``squat_tolerance`` level string is read and treated
    as poor when it names a low tier. Absent both keys, returns False.
    """
    explicit = flags.get("poor_squat_tolerance")
    if explicit is not None:
        return bool(explicit)
    level = str(flags.get("squat_tolerance") or "").strip().lower()
    return level in POOR_SQUAT_TOLERANCE_LEVELS


def _trap_bar_preference_context(flags: dict, *, cut_bucket: str) -> tuple[bool, list[str]]:
    """Resolve whether the trap-bar-over-back-squat preference should fire.

    Returns ``(active, reason_codes)`` where ``reason_codes`` names which
    condition(s) triggered it. Computed once per strength block since it depends
    only on the athlete's flags, not the candidate exercise.
    """
    reasons: list[str] = []

    injuries = flags.get("injuries") or []
    injury_blob = " ".join(str(injury) for injury in injuries).lower()
    hit_regions = [
        region
        for region in SQUAT_BAR_POSITION_INJURY_REGIONS
        if region in injury_blob
    ]
    if hit_regions:
        reasons.append("trap_bar_pref_injury:" + "/".join(hit_regions))

    if cut_bucket in TRAP_BAR_PREFERENCE_ACTIVE_CUT_BUCKETS or bool(flags.get("weight_cut_risk")):
        reasons.append("trap_bar_pref_active_cut")

    if str(flags.get("fatigue", "")).strip().lower() in {"moderate", "high"}:
        reasons.append("trap_bar_pref_fatigue")

    if _camp_is_compressed(flags):
        reasons.append("trap_bar_pref_compressed_camp")

    if _squat_tolerance_is_poor(flags):
        reasons.append("trap_bar_pref_poor_squat_tolerance")

    return bool(reasons), reasons


def _trap_bar_anchor_preference_adjustment(
    exercise: dict,
    *,
    active: bool,
    context_reasons: list[str],
) -> tuple[float, list[str]]:
    """Bias a candidate toward the trap-bar deadlift over a barbell squat anchor.

    Only fires when the preference is ``active``. Penalises barbell
    back-/front-rack squat-pattern anchors (which load the spared joints and
    carry more axial/recovery cost) and boosts the trap-bar hinge anchor. Other
    movements — including bodyweight/dumbbell squats and trap-bar jump squats —
    are left untouched.
    """
    if not active:
        return 0.0, []
    equipment = set(normalize_equipment_list(exercise.get("equipment", [])))
    movement = normalize_exercise_movement(exercise)
    if movement == "squat" and "barbell" in equipment and "trap_bar" not in equipment:
        return TRAP_BAR_PREFERENCE_SQUAT_PENALTY, context_reasons + [
            "trap_bar_pref_squat_anchor_penalty"
        ]
    if movement == "hinge" and "trap_bar" in equipment:
        return TRAP_BAR_PREFERENCE_HINGE_BOOST, context_reasons + [
            "trap_bar_pref_trap_bar_anchor_boost"
        ]
    return 0.0, []


def _rotational_power_coverage_adjustment(
    quality_profile: dict,
    *,
    active: bool,
    restricted: bool,
) -> tuple[float, list[str]]:
    """Boost a safe rotational-power anchor when the camp has no exposure yet.

    Only fires when the caller reports the camp-level gap (``active``). A
    candidate qualifies only if it is a real rotational-power anchor and carries
    no restriction match, so injury/equipment-constrained options are never
    pulled forward by the gap.
    """
    if not active or restricted:
        return 0.0, []
    if not quality_profile.get("anchor_capable"):
        return 0.0, []
    if "rotational_power" not in set(quality_profile.get("base_categories") or ()):
        return 0.0, []
    return ROTATIONAL_POWER_COVERAGE_BOOST, ["rotational_power_camp_coverage_boost"]


def _strength_is_lower_body(exercise: dict, tags: set[str]) -> bool:
    movement = normalize_exercise_movement(exercise)
    if movement in {"hinge", "squat", "lunge", "lateral"}:
        return True
    return any(tag.startswith("mech_lower_") for tag in tags)


def _strength_is_unilateral_lower(exercise: dict, tags: set[str]) -> bool:
    if "unilateral" in tags or "mech_lower_lunge" in tags:
        return True
    movement = normalize_exercise_movement(exercise)
    return movement == "lunge" or str(exercise.get("type", "")).strip().lower() == "unilateral"


def _strength_overhead_signal(tags: set[str]) -> bool:
    return "overhead" in tags or "mech_shoulder_overhead" in tags


def _strength_dense_pattern(text: str) -> bool:
    keywords = ("emom", "tabata", "amrap", "for time")
    return any(keyword in text for keyword in keywords)


def _strength_conditioning_density(text: str, tags: set[str]) -> bool:
    if _strength_dense_pattern(text):
        return True
    if any(keyword in text for keyword in ("interval", "round", "burpee")):
        return True
    return bool(tags & {"conditioning", "endurance", "work_capacity", "mech_systemic_fatigue"})


def _strength_contextual_risk_patterns(exercise: dict) -> tuple[list[str], list[str], dict]:
    tags = set(normalize_tags(exercise.get("tags", [])))
    text = _strength_text_blob(exercise)
    name = str(exercise.get("name", "") or "")
    equipment = set(normalize_equipment_list(exercise.get("equipment", [])))
    soreness_risk = _exercise_profile_level(exercise, "soreness_risk")
    impact_cost = _exercise_profile_level(exercise, "impact_cost")
    eccentric_cost = _exercise_profile_level(exercise, "eccentric_cost")
    landing_cost = _exercise_profile_level(exercise, "landing_cost")
    cns_load = _exercise_profile_level(exercise, "cns_load")
    movement_cost = _exercise_profile_level(exercise, "movement_cost")
    lower_body = _strength_is_lower_body(exercise, tags)
    unilateral_lower = _strength_is_unilateral_lower(exercise, tags)
    landing_impact = "mech_landing_impact" in tags or "high_impact_lower" in tags
    if _has_explicit_profile_level(exercise, "landing_cost") or _has_explicit_profile_level(exercise, "impact_cost"):
        explicit_levels = [level for cost_field, level in [("landing_cost", landing_cost), ("impact_cost", impact_cost)] if _has_explicit_profile_level(exercise, cost_field)]
        landing_impact = not all(_low_cost_level(level) for level in explicit_levels)
    overhead = _strength_overhead_signal(tags)
    systemic_fatigue = _strength_conditioning_density(text, tags)
    compound = normalize_exercise_movement(exercise) == "compound" or "compound" in tags
    eccentric = "eccentric" in tags
    if _has_explicit_profile_level(exercise, "eccentric_cost"):
        eccentric = not _low_cost_level(eccentric_cost)
    triple_extension = "triple_extension" in tags
    ballistic = "mech_ballistic" in tags or "explosive" in tags
    dense_emom = _strength_dense_pattern(text)
    heavy_loaded_lower = lower_body and bool(equipment & {"barbell", "trap_bar"})
    heavy_loaded_pattern = bool(re.search(r"@\s*(?:8[0-9]|9[0-9]|100)\b", text)) or "heavy" in text
    if _high_cost_level(cns_load) and heavy_loaded_lower:
        heavy_loaded_pattern = True
    dense_ballistic = ballistic and (dense_emom or systemic_fatigue)
    trap_bar_jump = (
        ("trap bar" in text and "jump" in text)
        or ("trap_bar" in equipment and "mech_lower_jump" in tags and landing_impact)
    )
    aggressive_med_ball_slam = (
        "medicine_ball" in equipment
        and "slam" in text
        and bool(tags & {"mech_ballistic", "mech_trunk_rotation", "anti_rotation"})
    )

    penalties: list[str] = []
    blocks: list[str] = []

    if compound and eccentric:
        penalties.append("late_strength_penalty_compound_eccentric")
    if overhead and systemic_fatigue:
        penalties.append("late_strength_penalty_overhead_density")
    if triple_extension and landing_impact:
        penalties.append("late_strength_penalty_triple_extension_landing")
    if landing_impact and unilateral_lower and ballistic:
        penalties.append("late_strength_penalty_landing_unilateral_power")
    if heavy_loaded_lower and systemic_fatigue:
        penalties.append("late_strength_penalty_loaded_lower_fatigue")
    if str(exercise.get("method", "")).strip().lower() == "conditioning" and systemic_fatigue:
        penalties.append("late_strength_penalty_conditioning_impostor")
    if name in LATE_STRENGTH_DENYLIST:
        penalties.append("late_strength_penalty_known_offender")

    if dense_emom:
        blocks.append("late_strength_block_dense_emom")
    if eccentric and lower_body:
        blocks.append("late_strength_block_eccentric_lower")
    if landing_impact and unilateral_lower and ballistic:
        blocks.append("late_strength_block_landing_unilateral_power")
    if name in LATE_STRENGTH_DENYLIST:
        blocks.append("late_strength_block_known_offender")

    return penalties, blocks, {
        "tags": tags,
        "text": text,
        "lower_body": lower_body,
        "loaded_lower": heavy_loaded_lower,
        "heavy_loaded_pattern": heavy_loaded_pattern,
        "landing_impact": landing_impact,
        "overhead": overhead,
        "systemic_fatigue": systemic_fatigue,
        "soreness_risk": soreness_risk,
        "impact_cost": impact_cost,
        "eccentric_cost": eccentric_cost,
        "landing_cost": landing_cost,
        "cns_load": cns_load,
        "movement_cost": movement_cost,
        "ballistic": ballistic,
        "dense_ballistic": dense_ballistic,
        "trap_bar_jump": trap_bar_jump,
        "aggressive_med_ball_slam": aggressive_med_ball_slam,
    }


def _strength_throw_signal(exercise: dict, equipment: set[str]) -> bool:
    profile = exercise.get("profile") or {}
    text = str(profile.get("text") or _strength_text_blob(exercise)).lower()
    if re.search(r"\b(?:throw|throws|throwing|toss|tosses|tossing)\b", text):
        return True
    if "medicine_ball" in equipment and re.search(r"\b(?:pass|passes|passing)\b", text):
        return True
    return False


def _strength_band_signal(exercise: dict, equipment: set[str]) -> bool:
    if "bands" in equipment:
        return True
    profile = exercise.get("profile") or {}
    text = str(profile.get("text") or _strength_text_blob(exercise)).lower()
    return bool(re.search(r"\b(?:band|bands|banded)\b", text))


def _strength_metadata_score_adjustment(
    exercise: dict,
    *,
    fatigue: str,
    cut_bucket: str,
) -> tuple[float, list[str]]:
    """Score explicit strength metadata before falling back to text/tag heuristics."""
    soreness_risk = _exercise_profile_level(exercise, "soreness_risk")
    impact_cost = _exercise_profile_level(exercise, "impact_cost")
    eccentric_cost = _exercise_profile_level(exercise, "eccentric_cost")
    landing_cost = _exercise_profile_level(exercise, "landing_cost")
    cns_load = _exercise_profile_level(exercise, "cns_load")
    movement_cost = _exercise_profile_level(exercise, "movement_cost")
    cut_buckets_allowed = _exercise_cut_buckets_allowed(exercise)
    explicit_fields = {
        field
        for field in (
            "soreness_risk",
            "impact_cost",
            "eccentric_cost",
            "landing_cost",
            "cns_load",
            "movement_cost",
            "cut_buckets_allowed",
        )
        if exercise.get(field) not in (None, "", [], {})
    }
    if not explicit_fields:
        return 0.0, []

    adjustment = 0.0
    reason_codes: list[str] = []
    fatigue = str(fatigue or "").strip().lower()
    if fatigue == "high":
        if _high_cost_level(cns_load):
            adjustment -= 1.1
            reason_codes.append("strength_penalty_high_fatigue_high_cns_load")
        elif cns_load == "moderate":
            adjustment -= 0.35
            reason_codes.append("strength_penalty_high_fatigue_moderate_cns_load")
        if _high_cost_level(soreness_risk) or _high_cost_level(eccentric_cost):
            adjustment -= 0.45
            reason_codes.append("strength_penalty_high_fatigue_high_soreness_cost")

    if cut_bucket in LATE_STRENGTH_HIGH_CUT_BUCKETS:
        if cut_buckets_allowed and cut_bucket not in cut_buckets_allowed:
            adjustment -= 1.35
            reason_codes.append("strength_penalty_cut_bucket_mismatch")

        cost_levels = [level for level in (impact_cost, eccentric_cost, landing_cost, soreness_risk, cns_load, movement_cost) if level]
        if any(_high_cost_level(level) for level in cost_levels):
            adjustment -= 0.75
            reason_codes.append("strength_penalty_active_cut_high_cost_metadata")
        elif cost_levels and all(_low_cost_level(level) for level in cost_levels):
            adjustment += 0.25
            reason_codes.append("strength_boost_active_cut_low_cost_metadata")

    return round(adjustment, 4), reason_codes


def _evaluate_strength_late_window(
    exercise: dict,
    *,
    window: str | None,
    days_until_fight=None,
    cut_bucket: str = "none",
    source: str = "exercise_bank.json",
    familiar_names: set[str] | frozenset[str] | None = None,
) -> dict:
    if not is_active_late_selector_window(window):
        return {
            "blocked": False,
            "severity": "safe",
            "block_codes": [],
            "reason_codes": [],
            "penalty_codes": [],
            "adjustment": 0.0,
            "ambiguous_gap": None,
        }
    metadata_safety = None
    metadata_penalty_codes: list[str] = []
    metadata_adjustment = 0.0
    if exercise.get("_schema_source") or exercise.get("_schema_issues") or exercise.get("_schema_safety"):
        metadata_source = str(exercise.get("_schema_source") or source)
        metadata_safety = is_late_fight_metadata_safe(exercise, metadata_source, window)
        if metadata_safety.get("severity") == "blocked":
            return {
                "blocked": True,
                "severity": "blocked",
                "block_codes": metadata_safety["block_codes"],
                "reason_codes": metadata_safety["reason_codes"],
                "penalty_codes": metadata_safety.get("penalty_codes", []),
                "adjustment": -1.0,
                "ambiguous_gap": None,
                "unsafe_metadata": metadata_safety["unsafe_metadata"],
            }
        metadata_penalty_codes = list(metadata_safety.get("penalty_codes", []))
        if metadata_penalty_codes:
            metadata_adjustment = max(-0.75, -0.35 * len(metadata_penalty_codes))

    penalties, blocks, profile = _strength_contextual_risk_patterns(exercise)
    tags = profile["tags"]
    equipment = set(normalize_equipment_list(exercise.get("equipment", [])))
    late_windows = _exercise_late_windows(exercise)
    cut_buckets_allowed = _exercise_cut_buckets_allowed(exercise)
    phase_role = normalize_tag(str(exercise.get("phase_role") or ""))
    subfamily = normalize_tag(str(exercise.get("subfamily") or ""))
    soreness_risk = _exercise_profile_level(exercise, "soreness_risk")
    eccentric_cost = _exercise_profile_level(exercise, "eccentric_cost")
    landing_cost = _exercise_profile_level(exercise, "landing_cost")
    cns_load = _exercise_profile_level(exercise, "cns_load")
    low_impact = _exercise_profile_flag(exercise, tags, "low_impact")
    low_eccentric = _exercise_profile_flag(exercise, tags, "low_eccentric")
    neural_primer = _exercise_profile_flag(exercise, tags, "neural_primer")
    cns_freshness = _exercise_profile_flag(exercise, tags, "cns_freshness")
    ballistic_low_volume = _exercise_profile_flag(exercise, tags, "ballistic_low_volume")
    sport_specific = _exercise_profile_flag(exercise, tags, "sport_specific")
    explicit_late_touch = "late_strength_touch" in tags or phase_role == "late_strength_touch"
    explicit_cost_metadata = bool(
        late_windows
        or cut_buckets_allowed
        or phase_role
        or subfamily
        or any(
            exercise.get(field_name) is not None
            for field_name in ("soreness_risk", "eccentric_cost", "landing_cost", "cns_load")
        )
    )
    explicit_late_metadata = bool(late_windows or phase_role or subfamily or explicit_late_touch)
    safe_tags = sorted(tags & LATE_STRENGTH_SAFE_TAGS)
    severity = _strength_late_window_severity(window)
    reason_codes: list[str] = list(metadata_penalty_codes)
    penalty_codes: list[str] = list(metadata_penalty_codes)
    adjustment = metadata_adjustment
    exact_days_until_fight = coerce_days_until_fight(days_until_fight)
    cut_multiplier = LATE_STRENGTH_CUT_BUCKET_MULTIPLIER.get(cut_bucket, 0.0)
    high_cut_window = window in {D13_TO_D8, D7, D6_TO_D5, D4_TO_D2, D1} and cut_bucket in LATE_STRENGTH_HIGH_CUT_BUCKETS
    late_band_lockout_window = window in {D7, D6_TO_D5, D4_TO_D2, D1}
    rehab_mobility_band_ok = bool(
        tags
        & {
            "mobility",
            "recovery",
            "rehab",
            "rehab_friendly",
            "prehab",
            "injury_prevention",
        }
    )
    # The band-work lockout targets loaded/dense band strength work in the final
    # week. Low-dose late-safe primers (neural primers, explicit late-strength
    # touches, ballistic low-volume work) are kept through the earlier final-week
    # windows, but D1 is the strictest day: no band work survives there, so the
    # primer exemption does not apply on D1.
    late_safe_band_primer = (
        window != D1
        and (neural_primer or explicit_late_touch or ballistic_low_volume)
    )
    if (
        late_band_lockout_window
        and _strength_band_signal(exercise, equipment)
        and (window == D1 or not rehab_mobility_band_ok)
        and not late_safe_band_primer
    ):
        blocks.append("late_strength_block_band_work_lockout")
        reason_codes.append("late_strength_penalty_band_work_lockout")
    if exact_days_until_fight == 3 and _strength_throw_signal(exercise, equipment):
        blocks.append("late_strength_block_d3_throw_lockout")
        reason_codes.append("late_strength_penalty_d3_throw_lockout")

    if window == D1 and not ("d1_ok" in tags or "d1_if_familiar" in tags):
        blocks.append("late_strength_block_d1_requires_d1_tags")
    # d1 allows no equipment of any kind; d1_ok tags do not override this.
    if window == D1 and equipment - NON_EQUIPMENT_TOKENS:
        blocks.append("late_strength_block_d1_equipment")
    if window == D4_TO_D2 and "no_d4_to_d1" in tags:
        blocks.append("late_strength_block_no_d4_to_d1")
    if window in {D7, D6_TO_D5, D4_TO_D2, D1} and "no_d7_to_d1" in tags:
        blocks.append("late_strength_block_no_d7_to_d1")
    # Familiarity-gated late work (loaded/technical movements that need prior
    # exposure) is blocked from D13-D8 inward *unless* the athlete has already
    # trained this exact exercise (prev/recent plan exposure). With no
    # familiarity signal the default stays conservative and blocks, so a
    # movement is never introduced cold in the final stretch.
    if window in {D13_TO_D8, D7, D6_TO_D5, D4_TO_D2, D1} and "familiarity_required" in tags:
        exercise_name = str(exercise.get("name") or "")
        athlete_is_familiar = bool(familiar_names) and exercise_name in familiar_names
        if not athlete_is_familiar:
            blocks.append("late_strength_block_familiarity_required_late")
    if cut_bucket in LATE_STRENGTH_HIGH_CUT_BUCKETS and tags & {
        "single_leg", "no_high_cut", "neck_optional", "vestibular_sensitive", "balance_challenge"
    }:
        adjustment -= 0.45 * (1.0 + cut_multiplier)
        reason_codes.append("late_strength_penalty_high_cut_sensitive_tags")
    # Balance is materially compromised during a hard weight cut, so balance-risk
    # work is hard-blocked (not just penalised) in the late window.
    if cut_bucket in LATE_STRENGTH_HIGH_CUT_BUCKETS and tags & {
        "single_leg", "vestibular_sensitive", "balance_challenge"
    }:
        blocks.append("late_strength_block_high_cut_balance_risk")

    if late_windows:
        if window in late_windows:
            adjustment += 0.8 + (0.15 * severity)
            reason_codes.append("late_strength_boost_window_fit")
        else:
            adjustment -= 0.85
            reason_codes.append("late_strength_penalty_outside_window")
            blocks.append("late_strength_block_window_mismatch")

    if cut_buckets_allowed:
        if cut_bucket in cut_buckets_allowed:
            adjustment += 0.2 + (0.15 * cut_multiplier)
            reason_codes.append("late_strength_boost_cut_survivable")
        elif cut_bucket in LATE_STRENGTH_HIGH_CUT_BUCKETS:
            blocks.append("late_strength_block_cut_bucket_mismatch")

    if explicit_late_touch:
        adjustment += 0.45
        reason_codes.append("late_strength_boost_explicit_late_touch")
    if _has_explicit_profile_level(exercise, "eccentric_cost"):
        low_eccentric = _low_cost_level(eccentric_cost)
    impact_cost = _exercise_profile_level(exercise, "impact_cost")
    explicit_impact_levels = [
        level
        for field_name, level in (
            ("landing_cost", landing_cost),
            ("impact_cost", impact_cost),
        )
        if _has_explicit_profile_level(exercise, field_name)
    ]
    if explicit_impact_levels:
        low_impact = all(_low_cost_level(level) for level in explicit_impact_levels)
    if _has_explicit_profile_level(exercise, "cns_load"):
        cns_freshness = _low_cost_level(cns_load)

    if low_eccentric or eccentric_cost in {"none", "low"}:
        adjustment += 0.3
        reason_codes.append("late_strength_boost_low_eccentric")
    if low_impact or landing_cost in {"none", "low"}:
        adjustment += 0.25
        reason_codes.append("late_strength_boost_low_impact")
    if cns_freshness or cns_load == "low":
        adjustment += 0.25
        reason_codes.append("late_strength_boost_cns_freshness")
    if ballistic_low_volume and not profile["landing_impact"]:
        adjustment += 0.25
        reason_codes.append("late_strength_boost_ballistic_low_volume")
    if sport_specific:
        adjustment += 0.2
        reason_codes.append("late_strength_boost_sport_specific")
    if soreness_risk == "low":
        adjustment += 0.2
        reason_codes.append("late_strength_boost_low_soreness")

    if neural_primer:
        adjustment += 0.7 + (0.1 * severity)
        reason_codes.append("late_strength_boost_neural_primer")
    if "speed" in tags:
        adjustment += 0.45
        reason_codes.append("late_strength_boost_speed")
    if "reactive" in tags and not profile["landing_impact"]:
        adjustment += 0.3
        reason_codes.append("late_strength_boost_reactive")
    if "rehab_friendly" in tags:
        adjustment += 0.5
        reason_codes.append("late_strength_boost_rehab_friendly")
    if "low_impact" in tags or "cns_freshness" in tags:
        adjustment += 0.35
        reason_codes.append("late_strength_boost_freshness")
    if (
        profile["ballistic"]
        and not profile["landing_impact"]
        and not profile["systemic_fatigue"]
        # Preserve late-window scoring while movement normalization becomes
        # more specific. Upper-body push families remain governed by their
        # explicit late metadata rather than gaining an incidental score boost.
        and normalize_exercise_movement(exercise) in {"core", "pull", "rotational"}
    ):
        adjustment += 0.25
        reason_codes.append("late_strength_boost_crisp_ballistic")

    for code in penalties:
        if code == "late_strength_penalty_known_offender":
            adjustment -= 1.2 * severity
        elif code == "late_strength_penalty_conditioning_impostor":
            adjustment -= 1.1 * severity
        elif code == "late_strength_penalty_landing_unilateral_power":
            adjustment -= 1.0 * severity
        elif code == "late_strength_penalty_loaded_lower_fatigue":
            adjustment -= 0.85 * severity
        else:
            adjustment -= 0.75 * severity
        reason_codes.append(code)

    if high_cut_window:
        if any(
            _high_cost_level(level)
            for level in (soreness_risk, eccentric_cost, landing_cost, cns_load, _exercise_profile_level(exercise, "impact_cost"))
        ):
            adjustment -= 0.75 * severity * cut_multiplier
            reason_codes.append("late_strength_penalty_cut_pressure_high_cost_metadata")
        if profile["loaded_lower"] and (
            profile["heavy_loaded_pattern"]
            or profile["systemic_fatigue"]
            or "cluster" in tags
            or "mech_cns_high" in tags
        ):
            adjustment -= 0.95 * severity * cut_multiplier
            reason_codes.append("late_strength_penalty_cut_pressure_loaded_lower")
        if profile["landing_impact"] and profile["lower_body"]:
            adjustment -= 0.85 * severity * cut_multiplier
            reason_codes.append("late_strength_penalty_cut_pressure_landing_impact")
        if profile["dense_ballistic"]:
            adjustment -= 0.75 * severity * cut_multiplier
            reason_codes.append("late_strength_penalty_cut_pressure_dense_ballistic")

    if window in {D7, D6_TO_D5, D4_TO_D2, D1} and profile["aggressive_med_ball_slam"]:
        adjustment -= 1.05 * severity
        reason_codes.append("late_strength_penalty_aggressive_med_ball_slam")
    if window in {D21_TO_D14, D13_TO_D8, D7, D6_TO_D5, D4_TO_D2, D1} and profile["trap_bar_jump"]:
        adjustment -= 1.0 * severity
        reason_codes.append("late_strength_penalty_jump_landing")
    if window in {D4_TO_D2, D1} and profile["loaded_lower"] and not explicit_late_metadata and (
        profile["systemic_fatigue"] or "cluster" in tags or "mech_cns_high" in tags
    ):
        blocks.append("late_strength_block_loaded_lower_noise")
    if window == D1 and profile["overhead"] and not explicit_late_metadata and not (low_impact and cns_freshness):
        blocks.append("late_strength_block_overhead_noise")
    if window == D1 and "medicine_ball" in set(normalize_equipment_list(exercise.get("equipment", []))) and not explicit_late_metadata:
        blocks.append("late_strength_block_nonexplicit_ballistic_med_ball")

    if window in LATE_STRENGTH_TIGHT_WINDOWS:
        block_codes = sorted(set(blocks))
    else:
        # Outside the tight final-week windows (D21-D14, D13-D8) the window-specific
        # noise/diversity blocks are dropped, but *authority* blocks always survive:
        # the safety-critical hard blocks and an explicit late_windows mismatch. An
        # exercise that declares late_windows is never surfaced outside them — the
        # bank's declared range is authoritative across the whole late-fight period,
        # not just the tight windows. (Exercises with no late_windows are already
        # hard-blocked everywhere by the missing-late-windows metadata gate.)
        block_codes = sorted(
            b
            for b in set(blocks)
            if b in LATE_STRENGTH_SAFETY_CRITICAL_BLOCKS
            or b == "late_strength_block_window_mismatch"
        )
    if window in LATE_STRENGTH_TIGHT_WINDOWS and high_cut_window:
        if profile["loaded_lower"] and (
            profile["heavy_loaded_pattern"]
            or profile["dense_ballistic"]
            or profile["landing_impact"]
        ):
            block_codes.append("late_strength_block_high_cut_loaded_lower")
    if window in {D21_TO_D14, D13_TO_D8, D7, D6_TO_D5, D4_TO_D2, D1} and profile["trap_bar_jump"]:
        block_codes.append("late_strength_block_trap_bar_jump")
    if window == D1 and profile["aggressive_med_ball_slam"]:
        block_codes.append("late_strength_block_aggressive_med_ball_slam")
    block_codes = sorted(set(block_codes))

    ambiguous_gap = None
    if (
        not block_codes
        and not safe_tags
        and not explicit_cost_metadata
        and (profile["ballistic"] or profile["overhead"] or "mech_reactive" in tags)
        and not profile["landing_impact"]
        and not profile["systemic_fatigue"]
    ):
        ambiguous_gap = {
            "name": exercise.get("name", "<unnamed>"),
            "issue": "late_safe_intent_not_explicit",
            "signals": sorted(
                signal
                for signal in {"explosive", "mech_ballistic", "mech_reactive", "mech_shoulder_overhead"} & tags
            ),
        }

    return {
        "blocked": bool(block_codes),
        "severity": "blocked" if block_codes else "penalty" if penalty_codes else "safe",
        "block_codes": block_codes,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "penalty_codes": list(dict.fromkeys(penalty_codes)),
        "adjustment": round(adjustment, 4),
        "ambiguous_gap": ambiguous_gap,
    }


def _late_strength_family(exercise: dict) -> str:
    subfamily = normalize_tag(str(exercise.get("subfamily") or ""))
    if subfamily:
        return subfamily
    equipment = set(normalize_equipment_list(exercise.get("equipment", [])))
    if "medicine_ball" in equipment:
        return "medicine_ball"
    if "bands" in equipment:
        return "bands"
    if str(exercise.get("method", "")).strip().lower() == "rehab":
        return "rehab_support"
    movement = normalize_exercise_movement(exercise)
    return movement if movement != "unknown" else "general"


def _apply_late_strength_diversity_dampener(
    weighted_exercises: list[tuple[dict, float, dict]],
    *,
    window: str | None,
) -> list[tuple[dict, float, dict]]:
    if not is_active_late_selector_window(window):
        return weighted_exercises

    family_counts: dict[str, int] = defaultdict(int)
    reordered: list[tuple[dict, float, dict]] = []
    idx = 0
    guard = 0
    max_iter = max(len(weighted_exercises) * 4, 8)
    while idx < len(weighted_exercises):
        guard += 1
        if guard > max_iter:
            logger.warning("[stage1] loop_guard_break module=late_strength_dampener_outer")
            break
        leader_score = weighted_exercises[idx][1]
        group = [weighted_exercises[idx]]
        idx += 1
        inner_guard = 0
        while idx < len(weighted_exercises) and leader_score - weighted_exercises[idx][1] <= LATE_STRENGTH_TRUE_CLUSTER_BAND:
            inner_guard += 1
            if inner_guard > max_iter:
                logger.warning("[stage1] loop_guard_break module=late_strength_dampener_inner")
                break
            group.append(weighted_exercises[idx])
            idx += 1
        if len(group) > 1 and len({_late_strength_family(exercise) for exercise, _, _ in group}) > 1:
            indexed_group = list(enumerate(group))
            indexed_group.sort(
                key=lambda item: (
                    family_counts[_late_strength_family(item[1][0])],
                    item[0],
                )
            )
            group = [entry for _, entry in indexed_group]
        for exercise, score, reasons in group:
            reordered.append((exercise, score, reasons))
            family_counts[_late_strength_family(exercise)] += 1
    return reordered


# Running space is a surface, not apparatus, and intake never asks for it: the
# conditioning bank's hill sprints and 80-60-40m ladders require nothing, and
# the editor already treats it as ambient. Requiring it made the strength rows
# that spell it out (Sprint Acceleration, Carioca Sprint, ...) unreachable for
# every athlete. Mats, lines and tape are not covered: their suitability is
# task-specific.
_SPACE_ONLY_EQUIPMENT = frozenset({"open_space"})
# Still unreachable: their injury metadata gates more loosely than the
# conditioning sprints athletes already receive (Backpedal-to-Sprint derives no
# deceleration risk, so a high-severity ankle or knee injury allows it; Single-
# Leg Bounding is allowed with a high-severity Achilles injury). Making them
# reachable waits on that metadata, which this gate does not own.
_SPACE_ONLY_HELD_BACK = frozenset({"Backpedal-to-Sprint Transition", "Single-Leg Bounding"})


def _unrequired_space_tokens(exercise: dict) -> frozenset[str]:
    return frozenset() if exercise.get("name") in _SPACE_ONLY_HELD_BACK else _SPACE_ONLY_EQUIPMENT


def _required_strength_equipment(exercise: dict) -> list[str]:
    exempt = _unrequired_space_tokens(exercise)
    return [token for token in normalize_equipment_list(exercise.get("equipment", [])) if token not in exempt]


def equipment_score_adjust(entry_equip, user_equipment, known_equipment):
    entry_equip_list = normalize_equipment_list(entry_equip)
    user_equipment = normalize_athlete_equipment_list(user_equipment)
    known_equipment = [e.lower() for e in known_equipment]

    if not entry_equip_list or "bodyweight" in entry_equip_list:
        return 0

    for eq in entry_equip_list:
        if eq in known_equipment and eq not in user_equipment:
            return -999

    if any(eq not in known_equipment for eq in entry_equip_list):
        return -1

    return 0


def score_exercise(
    exercise_tags,
    weakness_tags,
    goal_tags,
    style_tags,
    must_have_tags,
    phase_tags,
    current_phase,
    fatigue_level,
    available_equipment,
    required_equipment,
    is_rehab,
    priority_profile=None,
    must_have_bonus_multiplier: float = 1.0,
    derived_clarification_tags=None,
    adaptation_tags=(),
):
    """Return a weighted score and breakdown for a candidate exercise.

    ``adaptation_tags`` is the item's ``strength_adaptation_relevance``. A
    selected priority that names an adaptation (strength, power, speed, trunk
    strength) matches through that evidence; bank tags still match regional
    priorities, but never stand in for an adaptation.
    """
    exercise_tags = normalize_tags(exercise_tags or [])
    weakness_tags = normalize_tags(weakness_tags or [])
    goal_tags = normalize_tags(goal_tags or [])
    style_tags = normalize_tags(style_tags or [])
    must_have_tags = normalize_tags(must_have_tags or [])
    phase_tags = normalize_tags(phase_tags or [])
    derived_clarification_tags = normalize_tags(derived_clarification_tags or [])
    score = 0.0
    reasons = {
        "goal_hits": 0,
        "weakness_hits": 0,
        "style_hits": 0,
        "must_have_hits": 0,
        "must_have_bonus": 0.0,
        "clarification_tag_hits": 0,
        "clarification_bonus": 0.0,
        "phase_hits": 0,
        "load_adjustments": 0.0,
        "equipment_boost": 0.0,
        "penalties": 0.0,
        "reason_codes": [],
    }

    matched_weakness_tags = sorted(set(exercise_tags) & set(weakness_tags))
    matched_goal_tags = sorted(set(exercise_tags) & set(goal_tags))
    if priority_profile is not None and adaptation_tags:
        # The athlete's own priority wording carries the canonical weight, so a
        # match is recorded under that wording.
        matched_goal_tags = sorted(
            set(matched_goal_tags)
            | {goal for goal in priority_profile.all_goals if priority_adaptation(goal) in adaptation_tags}
        )
        matched_weakness_tags = sorted(
            set(matched_weakness_tags)
            | {
                weakness
                for weakness in priority_profile.all_weak_areas
                if priority_adaptation(weakness) in adaptation_tags
            }
        )
    weakness_matches = len(matched_weakness_tags)
    goal_matches = len(matched_goal_tags)
    priority_bonus = (
        total_strength_collision_safe_priority_bonus(
            matched_goal_tags,
            matched_weakness_tags,
            priority_profile,
        )
        if priority_profile is not None
        else weakness_matches * 0.6 + goal_matches * 0.5
    )
    score += priority_bonus
    reasons["weakness_hits"] = weakness_matches
    reasons["goal_hits"] = goal_matches
    # A stated posterior-chain weakness should surface a posterior-chain-specific
    # late-touch (e.g. Isometric Mid-Thigh Pull / Trap-Bar Pin Pull Isometric)
    # rather than letting a quad-dominant isometric stand in as the posterior-chain
    # answer. Bias the specific options up so at least one is preferred where it is
    # safe and available; mixed quad/posterior options stay valid but no longer win
    # the posterior-chain slot by default.
    if (
        "posterior_chain" in weakness_tags
        and "late_strength_touch" in exercise_tags
        and "posterior_chain" in exercise_tags
        and "quad_dominant" not in exercise_tags
    ):
        score += POSTERIOR_CHAIN_LATE_TOUCH_BIAS
        reasons["reason_codes"].append("priority_posterior_chain_late_touch_bias")
    if derived_clarification_tags:
        matched_clarification_tags = sorted(set(exercise_tags) & set(derived_clarification_tags))
        clarification_bonus = min(
            len(matched_clarification_tags) * STRENGTH_CLARIFICATION_TAG_BONUS,
            STRENGTH_MAX_CLARIFICATION_TAG_BONUS,
        )
        if clarification_bonus > 0:
            score += clarification_bonus
            reasons["clarification_tag_hits"] = len(matched_clarification_tags)
            reasons["clarification_bonus"] = round(clarification_bonus, 2)
            for tag in matched_clarification_tags:
                reasons["reason_codes"].append(f"priority_clarification_tag_match:{tag}")

    if priority_profile is not None:
        for tag in matched_goal_tags:
            goal_weight = goal_priority_weight(tag, priority_profile)
            if goal_weight == PRIMARY_GOAL_WEIGHT:
                reasons["reason_codes"].append(f"priority_primary_goal_match:{tag}")
            elif goal_weight > 0:
                reasons["reason_codes"].append(f"priority_secondary_goal_match:{tag}")
        for tag in matched_weakness_tags:
            weakness_weight = weakness_priority_weight(tag, priority_profile)
            if weakness_weight == PRIMARY_WEAKNESS_WEIGHT:
                reasons["reason_codes"].append(f"priority_primary_weakness_match:{tag}")
            elif weakness_weight > 0:
                reasons["reason_codes"].append(f"priority_secondary_weakness_match:{tag}")
        for tag in list(dict.fromkeys(matched_goal_tags + matched_weakness_tags)):
            if is_priority_collision_tag(tag, priority_profile):
                reasons["reason_codes"].append(f"priority_collision_goal_weakness:{tag}")

    matched_style_tags = list(set(exercise_tags) & set(style_tags))
    style_score = len(matched_style_tags) * 0.3
    if len(matched_style_tags) == 2:
        style_score += 0.2
    elif len(matched_style_tags) >= 3:
        style_score += 0.1
    score += style_score
    reasons["style_hits"] = len(matched_style_tags)

    must_have_matches = len(set(exercise_tags) & set(must_have_tags))
    if must_have_matches:
        score += must_have_matches * 0.35
    reasons["must_have_hits"] = must_have_matches
    must_have_bonus_tags = {"compound", "posterior_chain", "unilateral", "rate_of_force", "explosive"}
    must_have_bonus = len(set(exercise_tags) & must_have_bonus_tags) * 0.15 * must_have_bonus_multiplier
    score += must_have_bonus
    reasons["must_have_bonus"] = round(must_have_bonus, 2)

    total_matches = len(
        set(exercise_tags) & set(weakness_tags + goal_tags + style_tags)
    )
    if total_matches >= 3:
        score += 0.2

    phase_matches = len(set(exercise_tags) & set(phase_tags))
    score += phase_matches * 0.4
    reasons["phase_hits"] = phase_matches

    fatigue_penalty = 0.0
    if fatigue_level == "high":
        fatigue_penalty = -0.75
    elif fatigue_level == "moderate":
        fatigue_penalty = -0.35
    score += fatigue_penalty
    reasons["load_adjustments"] = fatigue_penalty

    if not set(required_equipment).issubset(set(available_equipment)):
        return -999, reasons

    phase_boost = PHASE_EQUIPMENT_BOOST.get(current_phase, set())
    equipment_bonus = 0.25 if any(eq in phase_boost for eq in available_equipment) else 0.0
    score += equipment_bonus
    reasons["equipment_boost"] = equipment_bonus

    rehab_penalty = 0.0
    if is_rehab:
        phase_penalties = {"GPP": -0.7, "SPP": -1.0, "TAPER": -0.75}
        rehab_penalty = phase_penalties.get(current_phase, -0.75)
        score += rehab_penalty
    reasons["penalties"] = rehab_penalty

    reasons["deterministic_scoring"] = True
    reasons["final_score"] = round(score, 4)

    return round(score, 4), reasons

def is_banned_exercise(name: str, tags: list[str], fight_format: str, details: str = "") -> bool:
    """Return True if the exercise should be removed for the given sport."""
    name = name.lower()
    tags = normalize_tags(tags)
    details = details.lower()

    grappling_terms = {
        "wrestling",
        "wrestle",
        "wrestler",
        "bjj",
        "grappling",
        "grapple",
        "grappler",
        "sprawl",
        "sprawling",
    }

    if fight_format in {"boxing", "kickboxing"}:
        for term in grappling_terms:
            if term in name or term in tags or term in details:
                return True

    if fight_format == "boxing":
        for term in ["kick", "knee", "clinch knee strike"]:
            if term in name or term in tags or term in details:
                return True

    return False




_SUPRA_MAX_ISO_PATTERN = re.compile(r"(?:11[5-9]|120)%\s*1rm|supra", re.IGNORECASE)
_OVER_100_ISO_PATTERN = re.compile(
    r"(10[1-9]|1[1-9]\d)%\s*(?:1rm|max(?:\s+\w+)?)",
    re.IGNORECASE,
)


def _is_supra_max_isometric(exercise: dict) -> bool:
    name = str(exercise.get("name", ""))
    method = str(exercise.get("method", ""))
    tags = " ".join(normalize_tags(exercise.get("tags", [])))
    text = f"{name} {method} {tags}"
    if "isometric" not in text.lower() and "iso" not in text.lower():
        return False
    return bool(_SUPRA_MAX_ISO_PATTERN.search(text))


def _is_over_100_percent_isometric(exercise: dict) -> bool:
    name = str(exercise.get("name", ""))
    method = str(exercise.get("method", ""))
    tags = " ".join(normalize_tags(exercise.get("tags", [])))
    text = f"{name} {method} {tags}"
    if "isometric" not in text.lower() and "iso" not in text.lower():
        return False
    return bool(_OVER_100_ISO_PATTERN.search(text))


def _has_isometric_setup_equipment(equipment_access: list[str]) -> bool:
    eq = set(normalize_equipment_list(equipment_access))
    valid = {"power_rack", "squat_rack", "rack", "safety_pins", "pins"}
    return bool(eq & valid)

def get_exercise_bank() -> list[dict]:
    global _exercise_bank_cache
    if _exercise_bank_cache is None:
        _exercise_bank_cache = json.loads(
            (DATA_DIR / "exercise_bank.json").read_text(encoding="utf-8")
        )
        for item in _exercise_bank_cache:
            validate_training_item(item, source="exercise_bank.json", require_phases=True, mode="runtime")
            normalize_item_tags(item)
            # Selection writes the canonical movement back onto the row it reads
            # (``normalize_exercise_movement``), and classification reads that
            # movement. Normalizing here means every caller sees the same rows,
            # whether or not ``prime_strength_banks`` ran first.
            normalize_exercise_movement(item)
    return _exercise_bank_cache


def prime_strength_banks() -> None:
    """Load the strength bank once so later runs skip the cold read.

    `_detect_movement_pattern` includes the exercise's existing `movement` value
    in its keyword haystack and writes the result back in-place, so an
    unnormalized bank would classify items differently on the first
    plan-generation call than on later ones. ``get_exercise_bank`` pins the
    canonical movement as it loads, so priming only needs to trigger the load.
    """
    get_exercise_bank()


MOVEMENT_PATTERN_TAGS = {
    "squat": {"squat", "quad_dominant"},
    "hinge": {"hinge", "posterior_chain", "hip_dominant", "deadlift"},
    "push": {"push", "upper_body", "press"},
    "pull": {"pull"},
    "lunge": {"lunge", "unilateral"},
    "rotation": {"rotational", "anti_rotation"},
    "carry": {"carry", "loaded_carry", "grip"},
    "core": {"core"},
    "neck": {"neck"},
}

MOVEMENT_PATTERN_KEYWORDS = {
    "squat": ["squat"],
    "hinge": ["hinge", "deadlift", "rdl", "hip hinge"],
    "push": ["press", "push", "bench", "fly"],
    "pull": ["row", "pull", "chin"],
    "lunge": ["lunge", "split squat", "step-up", "step up"],
    "rotation": ["rotation", "rotational", "anti-rotation", "anti rotation"],
    "carry": ["carry", "farmer", "suitcase"],
    "core": ["core", "trunk", "ab"],
    "neck": ["neck"],
}

EXPLICIT_MOVEMENT_ALIASES = {
    "rotational": "rotation",
}

CANONICAL_EXPLICIT_MOVEMENTS = {
    "vertical_jump",
    "horizontal_jump",
    "lateral_reactive",
    "horizontal_push",
    "vertical_push",
    "olympic",
    "squat",
    "hinge",
    "push",
    "pull",
    "lunge",
    "rotation",
    "carry",
    "core",
    "neck",
}


def _contains_movement_keyword(text: str, keyword: str) -> bool:
    """Match complete movement terms instead of arbitrary substrings."""
    pattern = rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])"
    return re.search(pattern, text) is not None


def _detect_movement_pattern(exercise: dict) -> str:
    text_fields = [
        exercise.get("name", ""),
        exercise.get("movement", ""),
        exercise.get("category", ""),
        exercise.get("type", ""),
    ]
    haystack = " ".join(str(val) for val in text_fields).lower()
    name = str(exercise.get("name") or "").lower()
    method = str(exercise.get("method") or "").strip().lower()
    tags = set(normalize_tags(exercise.get("tags") or []))
    category = str(exercise.get("category") or "").strip().lower()
    explicit = str(exercise.get("movement") or "").strip().lower().replace(" ", "_")

    # The bank owns movement identity. Preserve meaningful explicit metadata
    # before consulting tags, names or method-level hints.
    explicit = EXPLICIT_MOVEMENT_ALIASES.get(explicit, explicit)
    if explicit in CANONICAL_EXPLICIT_MOVEMENTS:
        return explicit
    if explicit == "vertical" and "mech_lower_jump" in tags:
        return "vertical_jump"
    if explicit == "horizontal" and "mech_lower_jump" in tags:
        return "horizontal_jump"
    if explicit in {"lateral", "frontal"} and "mech_lower_jump" in tags:
        return "lateral_reactive"
    if explicit == "transverse" and ("mech_trunk_rotation" in tags or "rotat" in name):
        return "rotation"

    # Contrast pairs stay attached to the loaded first movement because that
    # movement controls selection and prescription.
    contrast_pair = "→" in name or "->" in name or "contrast_pairing" in tags
    if contrast_pair:
        loaded_name = re.split(r"(?:→|->)", name, maxsplit=1)[0]
        for pattern in ("squat", "hinge", "push", "pull", "lunge"):
            if any(
                _contains_movement_keyword(loaded_name, keyword)
                for keyword in MOVEMENT_PATTERN_KEYWORDS[pattern]
            ):
                return pattern

    # Mechanical metadata comes next. Plyometric is deliberately not treated
    # as a lower-body signal: it describes force production, not body region.
    upper_body_context = (
        category == "upper_body"
        or "upper_body" in tags
        or bool(re.search(r"\b(?:push-up|pushup|press|punch|chest toss|chest throw)\b", name))
    )
    if "mech_upper_press" in tags and "mech_lower_jump" not in tags and upper_body_context:
        return "vertical_push" if "overhead" in name else "horizontal_push"
    if "mech_lower_lateral" in tags and "mech_lower_jump" in tags:
        return "lateral_reactive"
    if "mech_trunk_rotation" in tags and "mech_lower_jump" in tags:
        return "rotation"
    if "mech_lower_jump" in tags:
        if any(hint in name for hint in ("lateral", "skater", "side hop", "zig-zag", "shuffle")):
            return "lateral_reactive"
        if any(hint in name for hint in ("broad jump", "forward hop", "bounding", "bound", "sprint")):
            return "horizontal_jump"
        return "vertical_jump"

    # Directional names can refine otherwise-generic bank metadata. Method is
    # only a weak eligibility hint here and never determines vertical direction.
    lower_body_context = category in {"lower_body", "lateral", "locomotion", "reactive"}
    if lower_body_context or method == "plyometric":
        if any(hint in name for hint in ("lateral", "skater", "side hop", "zig-zag", "shuffle")):
            return "lateral_reactive"
        if any(hint in name for hint in ("broad jump", "forward hop", "bounding", "bound", "sprint")):
            return "horizontal_jump"
        if re.search(r"\b(?:jump|jumps|hop|hops|pogo)\b", name) and category != "upper_body":
            return "vertical_jump"

    for pattern, keywords in MOVEMENT_PATTERN_KEYWORDS.items():
        if any(_contains_movement_keyword(haystack, keyword) for keyword in keywords):
            return pattern
    for pattern, tag_set in MOVEMENT_PATTERN_TAGS.items():
        if tags & tag_set:
            return pattern
    return "unknown"


def normalize_exercise_movement(exercise: dict) -> str:
    """Ensure exercises expose a canonical movement key."""
    movement = _detect_movement_pattern(exercise)
    exercise["movement"] = movement
    return movement


# Loaded carries are dosed in distance or time, never in reps. Identified from
# the bank's own carry semantics plus a narrow carry/sled noun -- deliberately
# NOT the word "walk", which in this bank also names banded lateral walks and a
# tandem-gait balance drill that correctly keep ordinary reps.
_CARRY_TAGS = frozenset({"carry_heavy", "mech_upper_carry"})
_CARRY_NAME_PATTERN = re.compile(r"\b(?:carry|carries|sled|yoke)\b")

# Loaded complete-cycle work: one full ground-to-standing cycle per side is the
# rep, so volume is counted in cycles per side and quality, not fatigue, governs.
# This is a deliberately narrow exception for the get-up family. The bank marks
# the Turkish Get-Up ``movement: "complex"``, but "complex" is also the natural
# label for a barbell complex, which is dosed nothing like a get-up -- so the
# movement value is not used as the discriminator here.
_LOADED_CYCLE_NAME_PATTERN = re.compile(r"\bget[-\s]?ups?\b")

_LOADED_BAR_EQUIPMENT = frozenset({"barbell", "trap_bar"})

# Ballistic bar lifts whose bank ``method`` still reads "strength". Their method
# field also gates strength-maintenance eligibility, so correcting the dose here
# rather than in the bank keeps selection semantics untouched. Gated on bar
# equipment, so the non-bar push-press variants keep their current class.
_POWER_LIFT_NAME_PATTERN = re.compile(
    r"\b(?:thruster|thrusters|push press|clean and jerk|clean & jerk)\b"
)

# Word-boundary med-ball match. A bare ``"med" in name`` substring also matched
# "Glute Medius", routing a static hold to the ballistic speed template.
_MED_BALL_NAME_PATTERN = re.compile(r"\bmed(?:icine)?[-\s]?ball\b")

# "3-Minute Round", "3-Minute Heavy Bag Interval": a fight-length round drill.
_COMBAT_ROUND_NAME_PATTERN = re.compile(r"\b3[-\s]?minute\b")


def _classify_prescription_type(exercise: dict) -> str:
    tags = set(normalize_tags(exercise.get("tags") or []))
    equipment = set(normalize_equipment_list(exercise.get("equipment", [])))
    name = (exercise.get("name") or "").lower()
    method = str(exercise.get("method") or "").strip().lower()

    # --- Archetypes whose dosing UNIT or METHOD the generic templates cannot
    # express. All are resolved BEFORE the equipment branch, because a loaded
    # implement must not override what the movement actually is.

    # A contrast pair carries two doses (heavy + explosive) and must keep both.
    # ``method: "contrast"`` is the bank's own class for exactly these entries.
    if method == "contrast" or "→" in name or "->" in name or "contrast_pairing" in tags:
        return "contrast"

    # Maximal overcoming isometrics (>=105% 1RM, or a max implement) are a few
    # seconds of all-out intent, not a 10-20s submaximal hold and certainly not
    # reps. ``_is_over_100_percent_isometric`` is the bank helper already used
    # for late-camp eligibility.
    if _is_over_100_percent_isometric(exercise):
        return "max_isometric"

    # Isometrics resolve before equipment: a barbell in the rack does not turn a
    # 110% 1RM pin hold into an 8-12 rep tempo lift. The condition itself is
    # unchanged -- only its position moved.
    if "isometric" in tags or "isometric" in name or "iso hold" in name:
        return "isometric"

    if "sled" in name and "speed" in name:
        return "speed_sled"
    if tags & _CARRY_TAGS or _CARRY_NAME_PATTERN.search(name):
        return "carry"

    # The bank's own ``method: "conditioning"`` drills (jump rope, bag rounds,
    # rope waves, shadow boxing) are dosed in work time, never sets x reps:
    # "3 sets x 4 reps" of jump rope is not a prescription. Resolved before the
    # jump pattern, which a rope's ``mech_lower_jump`` tag would otherwise hit.
    # A name that carries its own round structure keeps it.
    if method == "conditioning":
        if "tabata" in name:
            return "tabata"
        if _COMBAT_ROUND_NAME_PATTERN.search(name):
            return "combat_round"
        return "timed_interval"

    if _LOADED_CYCLE_NAME_PATTERN.search(name):
        return "quality_cycle"

    if method == "isometric" or re.search(r"\b(?:hold|holds|plank)(?:\s*\([^)]*\))?$", name):
        return "support_isometric"

    # Jumps, hops and bounds are ballistic power work and must not inherit the
    # barbell %1RM strength template just because they use a loaded implement
    # (e.g. a trap-bar jump).
    is_jump_pattern = (
        method == "plyometric"
        or "mech_lower_jump" in tags
        or bool(re.search(r"\b(?:jump|jumps|bound|bounds|hop|hops|pogo)\b", name))
    )
    if is_jump_pattern:
        return "ballistic"

    # Olympic / loaded-power derivatives (clean, high pull, push press, speed
    # squat) are explosive work and take the ballistic dose rather than a
    # hypertrophy-and-slow-eccentrics barbell one. Gated on the bank's
    # authoritative ``method: "power"``, NOT on tags: heavy strength lifts such
    # as "Cluster Set Trap Bar Deadlift" also carry ``mech_ballistic``.
    if method == "power" or _POWER_LIFT_NAME_PATTERN.search(name):
        return "ballistic"

    if equipment.intersection({"barbell", "trap_bar"}):
        return "barbell"
    if "medicine_ball" in equipment or _MED_BALL_NAME_PATTERN.search(name):
        return "ballistic"
    if tags.intersection({"core", "trunk", "anti_rotation", "stability"}):
        return "core"
    if "deadbug" in name or "dead bug" in name:
        return "core"
    if tags.intersection({"explosive", "rate_of_force", "reactive"}):
        return "ballistic"
    if equipment.intersection({"bands", "medicine_ball", "kettlebell"}) and "sprint" in name:
        return "ballistic"
    return "general"


def _prescription_templates(phase: str) -> dict[str, str]:
    phase = phase.upper()
    barbell = {
        "GPP": "3x8-12 @ 60-75% 1RM with slow eccentrics, tempo 3-1-1.",
        "SPP": "3–5x3–5 @ 85–90% 1RM with contrast training (pair with explosive move).",
        "TAPER": "2–3x3–5 @ 80–85%, cluster sets, minimal eccentric load.",
    }
    ballistic = {
        "GPP": "3–5x4–6 reps (or 6–10 throws) at crisp intent; rest 60–90s.",
        "SPP": "4–6x2–5 reps at max speed; full rest 60–120s.",
        "TAPER": "3–5x2–4 reps/throws at max speed; full rest 60–120s.",
    }
    speed_sled = {
        "GPP": "4 x 5s acceleration with a light sled; full rest 90s; stop if speed drops.",
        "SPP": "6 x 5s acceleration with a light sled; full rest 90s; stop if speed drops.",
        "TAPER": "2 x 3s familiar light-sled acceleration; full rest 90s; stop if speed drops.",
    }
    # A contrast pair keeps BOTH components. Written without a bare "NxM" token
    # so the countdown overlay in ``prescription_resolver`` preserves the pair
    # verbatim instead of parsing the first pair of numbers and replacing the
    # whole string -- which would silently delete the explosive half.
    contrast = {
        "GPP": (
            "3–5 rounds: heavy movement 3–5 reps @ 70–80% 1RM "
            "→ explosive movement 3–5 reps at max intent; rest 2–4 min per round."
        ),
        "SPP": (
            "3–4 rounds: heavy movement 2–3 reps @ 85–92% 1RM "
            "→ explosive movement 2–3 reps at max intent; rest 2–4 min per round."
        ),
        "TAPER": (
            "1–3 rounds: heavy movement 1–2 reps @ 80–85% 1RM "
            "→ explosive movement 1–2 reps at max intent; full rest, stop on any speed drop."
        ),
    }
    # Carries are dosed in distance or time. Also deliberately free of a bare
    # "NxM" token: "3 x 20 m" would be parsed as 20 *reps* by the countdown
    # overlay, so the unit is spelled out instead.
    carry = {
        "GPP": "3–5 carries over 20–40 m (or 20–40s), heavy but unbroken; rest 60–90s.",
        "SPP": "2–4 carries over 15–30 m (or 15–30s), heavier load, posture first; full rest.",
        "TAPER": "1–2 carries over 10–20 m (or 10–20s), moderate load; stop well before grip failure.",
    }
    # One full cycle per side is the rep; quality and control govern, not
    # fatigue. The ceiling is stated in the dose itself so it survives rendering.
    quality_cycle = {
        "GPP": (
            "1–2 sets of 1–2 reps per side (hard ceiling 2 sets of 2 per side); "
            "full quality cycles, never to fatigue."
        ),
        "SPP": (
            "1–2 sets of 1–2 reps per side; heavier implement, perfect positions, "
            "stop on any wobble."
        ),
        "TAPER": "1 set of 1 rep per side; movement quality only, no fatigue.",
    }
    # Conditioning drills from the exercise bank are dosed in work time. Like
    # the carry dose these avoid a bare "NxM" token, so the countdown overlay
    # reduces the leading round count and keeps the seconds.
    timed_interval = {
        "GPP": "3–5 rounds of 60–90s work at a steady, repeatable rhythm; rest 30–60s.",
        "SPP": "4–6 rounds of 20–30s hard work; rest 40–60s.",
        "TAPER": "2–3 rounds of 20–30s sharp work; full rest 60s, never to fatigue.",
    }
    combat_round = {
        "GPP": "3–4 rounds of 3 min at a controlled pace; rest 60s between rounds.",
        "SPP": "3–5 rounds of 3 min at fight pace; rest 60s between rounds.",
        "TAPER": "1–2 rounds of 3 min, crisp and relaxed; rest 60–90s.",
    }
    tabata = {
        "GPP": "6–8 rounds of 20s hard work / 10s rest; 1–2 blocks, 2–3 min between blocks.",
        "SPP": "8 rounds of 20s all-out work / 10s rest; 1–2 blocks, 2–3 min between blocks.",
        "TAPER": "4–6 rounds of 20s fast work / 10s rest; one block only.",
    }
    return {
        "barbell": barbell.get(phase, barbell["GPP"]),
        "timed_interval": timed_interval.get(phase, timed_interval["GPP"]),
        "combat_round": combat_round.get(phase, combat_round["GPP"]),
        "tabata": tabata.get(phase, tabata["GPP"]),
        "contrast": contrast.get(phase, contrast["GPP"]),
        "ballistic": ballistic.get(phase, ballistic["GPP"]),
        "quality_cycle": quality_cycle.get(phase, quality_cycle["GPP"]),
        "carry": carry.get(phase, carry["GPP"]),
        "speed_sled": speed_sled.get(phase, speed_sled["GPP"]),
        "max_isometric": (
            "3–5 efforts of 3–6s all-out against pins or an immovable load; "
            "2–3 min rest. Maximal intent, never dosed as reps."
        ),
        "isometric": "3–5 holds x 10–20s @ 7–9/10 effort; full rest between holds.",
        "support_isometric": "2 x 20s hold @ RPE 6; rest 60s." if phase != "TAPER" else "1 x 10s hold @ RPE 5; rest 60s.",
        "core": "2-4 sets x 6-10 reps or 20-40s tempo (3-1-3), RPE 6-8.",
        "general": "2–3x6–10 @ RPE 6–7, keep reps crisp.",
    }


def _strength_explanation(reasons: dict) -> str:
    parts = []
    if reasons.get("goal_hits"):
        parts.append(f"{reasons['goal_hits']} goal match")
    if reasons.get("weakness_hits"):
        parts.append(f"{reasons['weakness_hits']} weakness tag")
    if reasons.get("style_hits"):
        parts.append(f"{reasons['style_hits']} style tag")
    if reasons.get("phase_hits"):
        parts.append(f"{reasons['phase_hits']} phase tag")
    if reasons.get("equipment_boost"):
        parts.append("equipment boost")
    if reasons.get("load_adjustments"):
        parts.append("fatigue adjustment")
    return ", ".join(parts) if parts else "balanced selection"


def _build_strength_candidate_reservoir(
    weighted_exercises: list[tuple[dict, float, dict]],
    *,
    limit_per_role: int = 4,
) -> dict[str, list[dict]]:
    reservoirs: dict[str, list[dict]] = defaultdict(list)
    seen_by_role: dict[str, set[str]] = defaultdict(set)

    for exercise, score, reasons in weighted_exercises:
        name = exercise.get("name")
        if not name:
            continue
        exercise_copy = exercise.copy()
        movement = normalize_exercise_movement(exercise_copy)
        role = movement if movement != "unknown" else "strength_support"
        if name in seen_by_role[role]:
            continue
        if len(reservoirs[role]) >= limit_per_role:
            continue
        reservoirs[role].append(
            {
                "exercise": exercise_copy,
                "score": score,
                "reasons": (reasons or {}).copy(),
                "explanation": _strength_explanation(reasons or {}),
                "score_evidence": build_score_evidence(score=score, reasons=reasons or {}),
                "metadata": normalize_selection_metadata(exercise_copy),
            }
        )
        seen_by_role[role].add(name)

    return dict(reservoirs)


def format_strength_block(phase: str, fatigue: str, exercises: list[dict]) -> str:
    """Return the formatted strength block for the given phase."""
    phase = phase.upper()
    weekly_progression = {
        "GPP": "Add 1 set or ~5–10% load weekly; deload final week by ~20%.",
        "SPP": "Hold volume, increase intensity or bar speed weekly; deload final week by ~20%.",
        "TAPER": "Cut total volume 40–60%, keep intensity crisp; last 3–5 days very light.",
    }
    time_short_note = {
        "GPP": "Keep top 2 lifts + 1 trunk/neck drill.",
        "SPP": "Keep heavy lift + paired explosive + trunk.",
        "TAPER": "Keep 1 neural primer + 1 trunk/neck drill.",
    }

    fatigue_note = ""
    if fatigue == "high":
        fatigue_note = "⚠️ High fatigue → reduce volume by 30-40%, drop last set per lift."
    elif fatigue == "moderate":
        fatigue_note = "⚠️ Moderate fatigue → reduce 1 set if performance drops."

    strength_output = [
        f"**Phase:** {phase}",
        f"**Weekly Progression:** {weekly_progression.get(phase, 'Progress weekly with small load jumps.')}",
        f"**If Time Short:** {time_short_note.get(phase, 'Keep top 2 lifts.')}",
        "",
    ]

    top_exercises = "; ".join(ex["name"] for ex in exercises)
    strength_output.append(f"**Top Exercises:** {top_exercises}")

    prescriptions = _prescription_templates(phase)
    ordered_types = [
        "barbell",
        "contrast",
        "ballistic",
        "quality_cycle",
        "carry",
        "max_isometric",
        "isometric",
        "core",
        "timed_interval",
        "combat_round",
        "tabata",
        "general",
    ]
    present_types = []
    for exercise in exercises:
        ex_type = _classify_prescription_type(exercise)
        if ex_type not in present_types:
            present_types.append(ex_type)

    strength_output += ["", "**Prescriptions by Exercise Type:**"]
    for ex_type in ordered_types:
        if ex_type not in present_types:
            continue
        label = {
            "barbell": "Barbell Strength",
            "contrast": "Contrast Pairs (Heavy → Explosive)",
            "ballistic": "Ballistics (Med Ball / Speed / Power)",
            "quality_cycle": "Loaded Complete-Cycle (Per Side)",
            "carry": "Loaded Carries (Distance / Time)",
            "max_isometric": "Maximal Overcoming Isometrics",
            "isometric": "Isometrics",
            "core": "Core Control",
            "timed_interval": "Conditioning Drills (Timed Rounds)",
            "combat_round": "Fight-Length Rounds",
            "tabata": "Tabata Intervals",
            "general": "General Strength",
        }[ex_type]
        strength_output.append(f"- **{label}:** {prescriptions[ex_type]}")

    if fatigue_note:
        strength_output += [
            "",
            f"**Adjustment:** {fatigue_note}",
        ]

    return "\n".join(strength_output)


def generate_strength_block(*, flags: dict, weaknesses=None):
    strength_started_at = perf_counter()
    substep_callback = flags.get("strength_substep_callback")
    logger = logging.getLogger(__name__)

    def _run_substep(step_name: str, fn):
        started_code = f"stage1_strength_{step_name}_started"
        finished_code = f"stage1_strength_{step_name}_finished"
        if callable(substep_callback):
            substep_callback(started_code, f"Stage 1 strength {step_name} started")
        started_at = perf_counter()
        try:
            result = fn()
        except Exception:
            elapsed = perf_counter() - started_at
            logger.info("[stage1] strength_substep_elapsed step=%s elapsed=%.2f", step_name, elapsed)
            if elapsed > 5.0:
                logger.warning("[stage1] slow_strength_substep step=%s elapsed=%.2f", step_name, elapsed)
            raise
        elapsed = perf_counter() - started_at
        logger.info("[stage1] strength_substep_elapsed step=%s elapsed=%.2f", step_name, elapsed)
        if elapsed > 5.0:
            logger.warning("[stage1] slow_strength_substep step=%s elapsed=%.2f", step_name, elapsed)
        if callable(substep_callback):
            substep_callback(finished_code, f"Stage 1 strength {step_name} finished")
        return result

    def _run_real_poststep(step_name: str, fn):
        started_at = perf_counter()
        result = _run_substep(step_name, fn)
        elapsed = perf_counter() - started_at
        if elapsed > 5.0:
            logger.warning("[stage1] slow_strength_poststep step=%s phase=%s elapsed=%.2f", step_name, phase, elapsed)
        return result

    phase = flags.get("phase", "GPP").upper()
    seed = flags.get("random_seed")
    rng = random.Random(seed) if seed is not None else None
    injuries = flags.get("injuries", [])
    restrictions = flags.get("restrictions")
    ignore_restrictions = bool(flags.get("ignore_restrictions", False))
    injury_trace = os.environ.get("INJURY_TRACE", "0") == "1"
    fatigue = flags.get("fatigue", "low")
    equipment_access = normalize_athlete_equipment_list(flags.get("equipment", []))
    tested_1rm_available = bool(flags.get("tested_1rm_available", False))
    has_isometric_setup = _has_isometric_setup_equipment(equipment_access)
    fight_format = _normalize_fight_format(flags.get("fight_format", "mma"))
    style_input = flags.get("style_tactical", [])
    if isinstance(style_input, str):
        raw_style_list = [style_input]
    elif isinstance(style_input, list):
        raw_style_list = list(style_input)
    else:
        raw_style_list = []
    style_list = [
        canonical
        for style in raw_style_list
        if (canonical := normalize_tag(str(style or "")))
    ]
    goals = flags.get("key_goals", [])
    core_balance_bonus = 1 if _has_core_balance_priority(flags, weaknesses) else 0
    # Camp-level rotational coverage gap, reported by the phase loop. Held to
    # the development phases and to non-high fatigue: a taper or a fried athlete
    # is no place to chase missing exposure.
    rotational_power_coverage_gap = (
        bool(flags.get("rotational_power_camp_gap"))
        and phase in {"GPP", "SPP"}
        and str(fatigue or "").strip().lower() != "high"
    )
    require_lower_body_explosive_anchor = (
        phase in {"GPP", "SPP"}
        and str(fatigue or "").strip().lower() != "high"
        and _has_lower_body_explosive_priority(flags, weaknesses)
    )
    priority_profile = build_priority_profile(
        SimpleNamespace(
            key_goals=goals,
            primary_goal=flags.get("primary_goal", ""),
            weak_areas=weaknesses or [],
            primary_weak_area=flags.get("primary_weak_area", ""),
        )
    )
    strength_priority_selected = any(
        priority_adaptation(value) == "strength"
        for value in [*priority_profile.all_goals, *priority_profile.all_weak_areas]
    )
    training_days = flags.get("training_days", [])
    training_frequency = flags.get(
        "training_frequency", flags.get("days_available", len(training_days))
    )
    days_until_fight = flags.get("days_until_fight")
    num_strength_sessions = allocate_sessions(training_frequency, phase).get("strength", 2)
    exercise_counts = calculate_exercise_numbers(training_frequency, phase)
    target_exercises = exercise_counts.get("strength", 0)
    prev_exercises = flags.get("prev_exercises", [])
    recent_movements = set(flags.get("recent_exercises", []))
    # Names the athlete has already trained (prior plan + recent exposure). Used
    # so familiarity-gated loaded/technical work can survive the late windows
    # only for fighters who already use the movement.
    familiar_exercise_names = frozenset(
        str(name) for name in list(prev_exercises) + list(recent_movements) if str(name).strip()
    )
    cornerstone_terms = {"squat", "deadlift", "bench", "pull-up", "pullup"}
    # Bracket the candidate-pool source fetch with substep milestones so a
    # failure loading the strength bank stays visible (the "*_started" event is
    # emitted before the fetch) and the warm/cold prime paths report the same
    # substep.
    exercise_bank = _run_substep("candidate_pool", get_exercise_bank)
    source_candidate_count = len(exercise_bank)

    # When a seed is provided, vary the iteration order of the source banks so
    # that score-tied candidates fall on different sides of the order-index
    # tiebreaker (see ``_late_safe_candidate_priority``). Use the seeded ``rng``
    # — never the global ``random`` module — so determinism is per-call and no
    # state leaks across plan generations.
    if rng is not None:
        exercise_bank = list(exercise_bank)
        rng.shuffle(exercise_bank)

    style_tags = [t for s in style_list for t in STYLE_TAG_MAP.get(s, [])]
    goal_tags = [tag for g in goals for tag in GOAL_PRIORITY_TAG_MAP.get(g, [])]
    must_have_by_phase = {
        "GPP": ["compound", "posterior_chain", "unilateral", "push", "pull"],
        "SPP": ["compound", "posterior_chain", "unilateral", "explosive", "rate_of_force"],
        "TAPER": ["late_strength_touch", "maximal_strength_maintenance", "neural_primer", "speed"],
    }
    must_have_tags = must_have_by_phase.get(phase, [])
    phase_dict = PHASE_TAG_BOOST.get(phase, {})
    phase_tags = list(phase_dict.keys()) if isinstance(phase_dict, dict) else []

    priority_focus = flags.get("priority_focus") if isinstance(flags.get("priority_focus"), dict) else {}
    derived_clarification_tags = normalize_tags(priority_focus.get("derived_clarification_tags", []))
    if not derived_clarification_tags:
        derived_clarification_tags = derive_clarification_tags(flags.get("goal_weakness_collision_details"))

    weighted_exercises = []
    late_window = classify_late_selector_window(days_until_fight, include_control=True)
    active_late_window = is_active_late_selector_window(late_window)
    cut_bucket = _resolved_cut_severity_bucket(flags)
    trap_bar_pref_active, trap_bar_pref_reasons = _trap_bar_preference_context(
        flags, cut_bucket=cut_bucket
    )
    high_pressure_late_cut = active_late_window and cut_bucket in LATE_STRENGTH_HIGH_CUT_BUCKETS
    must_have_bonus_multiplier = (
        LATE_MUST_HAVE_BONUS_MULTIPLIER if high_pressure_late_cut else 1.0
    )
    legacy_taper_gate = phase == "TAPER" and late_window in {None, CONTROL_D28}
    taper_allowed = {"neural_primer", "speed", "cluster", "explosive", "low_impact", "reactive", "rehab_friendly"}
    taper_banned = {
        "eccentric",
        "lunge_pattern",
        "compound",
        "horizontal_power",
        "triple_extension",
        "overhead",
        "contrast_pairing",
        "rate_of_force",
        "plyometric",
        "elastic",
        "mental_toughness",
        "posterior_chain",
        "high_volume",
        "barbell",
        "trap_bar",
    }
    restriction_candidates = 0
    restriction_blocked = 0
    restriction_reason_counts: dict[str, int] = defaultdict(int)
    restriction_warning_counts: dict[str, int] = defaultdict(int)
    restriction_blocked_items: list[dict] = []
    late_window_blocked: list[dict] = []
    late_window_penalized: list[dict] = []
    late_window_ambiguous: dict[str, dict] = {}
    post_score_late_eval_cache: dict[str, dict] = {}

    def _record_late_block(exercise: dict, score: float, reason_codes: list[str]) -> None:
        if not active_late_window:
            return
        late_window_blocked.append(
            {
                "name": exercise.get("name", "<unnamed>"),
                "score": round(float(score), 4),
                "reason_codes": list(reason_codes),
            }
        )

    def _record_late_penalty(exercise: dict, score: float, penalty_codes: list[str]) -> None:
        if not active_late_window or not penalty_codes:
            return
        late_window_penalized.append(
            {
                "name": exercise.get("name", "<unnamed>"),
                "score": round(float(score), 4),
                "penalty_codes": list(penalty_codes),
            }
        )

    def _record_ambiguous_gap(ambiguous_gap: dict | None) -> None:
        if not active_late_window or not ambiguous_gap:
            return
        name = str(ambiguous_gap.get("name") or "").strip()
        if not name:
            return
        late_window_ambiguous[name] = ambiguous_gap

    def _late_eval_cache_key(exercise: dict) -> str:
        name = str(exercise.get("name") or "").strip()
        return name if name else f"anon:{id(exercise)}"

    def _get_post_score_late_eval(
        exercise: dict,
        *,
        fallback_score: float = 0.0,
    ) -> dict:
        if not active_late_window:
            return {
                "blocked": False,
                "severity": "safe",
                "block_codes": [],
                "reason_codes": [],
                "penalty_codes": [],
                "adjustment": 0.0,
                "ambiguous_gap": None,
            }
        cache_key = _late_eval_cache_key(exercise)
        if cache_key not in post_score_late_eval_cache:
            late_eval = _evaluate_strength_late_window(
                exercise,
                window=late_window,
                days_until_fight=days_until_fight,
                cut_bucket=cut_bucket,
                familiar_names=familiar_exercise_names,
            )
            post_score_late_eval_cache[cache_key] = late_eval
            _record_ambiguous_gap(late_eval.get("ambiguous_gap"))
            if late_eval["blocked"]:
                _record_late_block(exercise, fallback_score, late_eval["block_codes"])
            elif late_eval.get("penalty_codes"):
                _record_late_penalty(exercise, fallback_score, late_eval["penalty_codes"])
        return post_score_late_eval_cache[cache_key]

    def _record_prefilter_late_eval(exercise: dict) -> None:
        if not active_late_window:
            return
        _get_post_score_late_eval(exercise, fallback_score=0.0)

    def _late_safe_marker_profile(
        exercise: dict,
        *,
        profile: dict | None = None,
    ) -> dict:
        tags = set(normalize_tags(exercise.get("tags", [])))
        late_windows = _exercise_late_windows(exercise)
        cut_buckets_allowed = _exercise_cut_buckets_allowed(exercise)
        resolved_profile = profile or classify_strength_item(exercise)
        markers: set[str] = set()

        if "late_strength_touch" in tags or bool(exercise.get("late_strength_touch")):
            markers.add("late_strength_touch")
        if late_window and (late_window in late_windows or "all" in late_windows):
            markers.add("late_windows")
        if cut_bucket and (
            cut_bucket in cut_buckets_allowed
            or "all" in cut_buckets_allowed
        ):
            markers.add("cut_buckets_allowed")
        for field in LATE_SAFE_STRENGTH_FIELDS - {"late_strength_touch"}:
            if field in tags or bool(exercise.get(field)):
                markers.add(field)
        if resolved_profile.get("quality_class") == "anchor_force_isometric":
            markers.add("anchor_force_isometric")

        return {
            "explicit": bool(markers),
            "markers": sorted(markers),
            "marker_count": len(markers),
        }

    def _merge_post_score_reasons(
        reasons: dict,
        *,
        profile: dict,
        late_eval: dict,
        late_safe_profile: dict,
    ) -> dict:
        merged = dict(reasons or {})
        if late_eval.get("reason_codes"):
            existing_codes = [str(code) for code in merged.get("reason_codes", []) if str(code).strip()]
            merged["reason_codes"] = list(dict.fromkeys(existing_codes + list(late_eval["reason_codes"])))
        if late_eval.get("penalty_codes"):
            existing_penalties = [str(code) for code in merged.get("penalty_codes", []) if str(code).strip()]
            merged["penalty_codes"] = list(dict.fromkeys(existing_penalties + list(late_eval["penalty_codes"])))
        if active_late_window:
            merged.setdefault("late_window_adjustment", late_eval.get("adjustment", 0.0))
        merged.setdefault("quality_class", profile.get("quality_class"))
        merged.setdefault("anchor_capable", profile.get("anchor_capable"))
        merged.setdefault("support_only", profile.get("support_only"))
        merged.setdefault("base_categories", profile.get("base_categories"))
        if late_safe_profile["markers"]:
            merged.setdefault("late_safe_markers", list(late_safe_profile["markers"]))
        return merged

    def _generic_loaded_anchor(profile: dict, late_safe_profile: dict) -> bool:
        return bool(
            high_pressure_late_cut
            and profile.get("loaded_pattern")
            and not late_safe_profile["explicit"]
        )

    def _late_safe_candidate_priority(
        cand_score: float,
        *,
        profile: dict,
        late_safe_profile: dict,
        order_idx: int,
    ) -> tuple:
        if not high_pressure_late_cut:
            return (round(float(cand_score), 6), -order_idx)
        return (
            1 if late_safe_profile["explicit"] else 0,
            1 if profile.get("force_isometric") else 0,
            late_safe_profile["marker_count"],
            round(float(cand_score), 6),
            -order_idx,
        )

    for ex in exercise_bank:
        tags = ex.get("tags", [])
        tags_lower = set(normalize_tags(tags))
        if _exercise_late_windows(ex) and not active_late_window:
            continue
        details = " ".join(
            [
                ex.get("notes", ""),
                ex.get("method", ""),
                ex.get("movement", ""),
            ]
        )
        restriction_text = " ".join(
            [
                ex.get("name", ""),
                ex.get("movement", ""),
                ex.get("method", ""),
                ex.get("notes", ""),
            ]
        )
        if is_banned_exercise(ex.get("name", ""), tags, fight_format, details):
            continue
        ex_equipment = _required_strength_equipment(ex)
        if legacy_taper_gate:
            if any(t in taper_banned for t in tags_lower) or any(eq in {"barbell", "trap_bar"} for eq in ex_equipment):
                _record_prefilter_late_eval(ex)
                continue
            if not any(t in taper_allowed for t in tags_lower):
                _record_prefilter_late_eval(ex)
                continue
        if phase not in ex.get("phases", []):
            if _exercise_late_windows(ex):
                _record_prefilter_late_eval(ex)
            continue
        if phase in {"SPP", "TAPER"} and _is_over_100_percent_isometric(ex):
            continue
        if _is_supra_max_isometric(ex) and not (tested_1rm_available and has_isometric_setup):
            continue

        method = ex.get("method", "").lower()

        restriction_candidates += 1
        restriction_result = evaluate_restriction_impact(
            restrictions,
            text=restriction_text,
            tags=tags,
            limit_penalty=-0.75,
        )
        restriction_penalty = restriction_result.get("penalty", 0.0)
        matched_restrictions = restriction_result.get("matched", [])
        if not ignore_restrictions and not restriction_result.get("allowed", True):
            restriction_blocked += 1
            for match in matched_restrictions:
                restriction_reason_counts[match.get("restriction", "generic_constraint")] += 1
            if matched_restrictions:
                top_match = max(matched_restrictions, key=lambda m: m.get("confidence", 0))
                restriction_blocked_items.append(
                    {
                        "name": ex.get("name", "<unnamed>"),
                        "match": top_match,
                        "risk": restriction_result.get("risk", 0.0),
                    }
                )
            if injury_trace:
                print(
                    "[guard-block] strength:%s name=%s matched=%s risk=%.2f"
                    % (
                        phase,
                        ex.get("name", "<unnamed>"),
                        matched_restrictions,
                        restriction_result.get("risk", 0.0),
                    )
                )
            continue
        if restriction_result.get("no_match_hints"):
            for hint in restriction_result.get("no_match_hints", []):
                restriction_warning_counts[hint] += 1

        score, breakdown = score_exercise(
            exercise_tags=tags,
            weakness_tags=weaknesses or [],
            goal_tags=goal_tags,
            style_tags=style_tags,
            must_have_tags=must_have_tags,
            phase_tags=phase_tags,
            current_phase=phase,
            fatigue_level=fatigue,
            available_equipment=equipment_access,
            required_equipment=ex_equipment,
            is_rehab=method == "rehab",
            priority_profile=priority_profile,
            must_have_bonus_multiplier=must_have_bonus_multiplier,
            derived_clarification_tags=derived_clarification_tags,
            adaptation_tags=strength_adaptation_relevance(ex, prescription_type=_classify_prescription_type(ex)),
        )
        if score == -999:
            continue
        quality_adjustment, quality_profile = strength_quality_adjustment(ex, phase=phase)
        score += quality_adjustment
        breakdown["quality_class"] = quality_profile["quality_class"]
        breakdown["quality_adjustment"] = round(quality_adjustment, 2)
        breakdown["anchor_capable"] = quality_profile["anchor_capable"]
        breakdown["support_only"] = quality_profile["support_only"]
        breakdown["base_categories"] = quality_profile["base_categories"]
        breakdown["fatigue_cost"] = _exercise_fatigue_cost(ex, quality_profile)
        breakdown["reason_codes"] = list(breakdown.get("reason_codes", []))
        metadata_adjustment, metadata_reason_codes = _strength_metadata_score_adjustment(
            ex,
            fatigue=fatigue,
            cut_bucket=cut_bucket,
        )
        if metadata_adjustment:
            score += metadata_adjustment
            breakdown["metadata_adjustment"] = metadata_adjustment
        if metadata_reason_codes:
            breakdown["reason_codes"] = list(
                dict.fromkeys(list(breakdown.get("reason_codes", [])) + list(metadata_reason_codes))
            )
        trap_bar_adjustment, trap_bar_reason_codes = _trap_bar_anchor_preference_adjustment(
            ex,
            active=trap_bar_pref_active,
            context_reasons=trap_bar_pref_reasons,
        )
        if trap_bar_adjustment:
            score += trap_bar_adjustment
            breakdown["trap_bar_preference_adjustment"] = trap_bar_adjustment
        if trap_bar_reason_codes:
            breakdown["reason_codes"] = list(
                dict.fromkeys(list(breakdown.get("reason_codes", [])) + list(trap_bar_reason_codes))
            )
        rotational_adjustment, rotational_reason_codes = _rotational_power_coverage_adjustment(
            quality_profile,
            active=rotational_power_coverage_gap,
            restricted=bool(matched_restrictions),
        )
        if rotational_adjustment:
            score += rotational_adjustment
            breakdown["rotational_power_coverage_adjustment"] = rotational_adjustment
        if rotational_reason_codes:
            breakdown["reason_codes"] = list(
                dict.fromkeys(list(breakdown.get("reason_codes", [])) + list(rotational_reason_codes))
            )
        if not ignore_restrictions and restriction_penalty:
            score += restriction_penalty
            breakdown["penalties"] = round(breakdown.get("penalties", 0.0) + restriction_penalty, 2)
            breakdown["restriction_hits"] = len(matched_restrictions)
        late_eval = _get_post_score_late_eval(ex, fallback_score=score)
        if late_eval["blocked"]:
            continue
        if late_eval["adjustment"]:
            score += late_eval["adjustment"]
        if late_eval["reason_codes"]:
            breakdown["reason_codes"] = list(
                dict.fromkeys(list(breakdown.get("reason_codes", [])) + list(late_eval["reason_codes"]))
            )
        if late_eval.get("penalty_codes"):
            breakdown["penalty_codes"] = list(
                dict.fromkeys(list(breakdown.get("penalty_codes", [])) + list(late_eval["penalty_codes"]))
            )
        breakdown["late_window_adjustment"] = late_eval["adjustment"]
        breakdown["final_score"] = round(score, 4)

        # Phase-based novelty enforcement with exemptions
        if prev_exercises and ex.get("name") in prev_exercises:
            if not (
                any(
                    term in ex.get("name", "").lower() or term in tags_lower
                    for term in cornerstone_terms
                )
                or (
                    active_late_window and tags_lower & {"neural_primer", "speed", "late_strength_touch", "maximal_strength_maintenance"}
                )
            ):
                continue

        # No additional fatigue or equipment adjustments; handled in score_exercise

        if score >= 0:
            weighted_exercises.append((ex, score, breakdown))

    weighted_candidates = [
        {
            "name": ex.get("name", ""),
            "score": score,
            "fatigue_cost": breakdown.get("fatigue_cost", ex.get("fatigue_cost", 0.0)),
            # Seeded runs break ties by a per-exercise seeded rank; unseeded
            # runs keep the deterministic name fallback.
            "tie_rank": _seeded_tie_rank(seed, ex.get("name", "")) if rng is not None else None,
            "payload": (ex, score, breakdown),
        }
        for ex, score, breakdown in weighted_exercises
    ]
    # Keep score-driven ordering primary while only allowing local fatigue-cost
    # reordering inside leader-anchored near-equal groups.
    weighted_exercises = [
        candidate["payload"]
        for candidate in sort_weighted_candidates(
            weighted_candidates,
            near_equal_score_band=NEAR_EQUAL_SCORE_BAND,
        )
    ]
    weighted_exercises = _apply_late_strength_diversity_dampener(
        weighted_exercises,
        window=late_window,
    )
    days_count = len(training_days) if isinstance(training_days, list) else training_days
    if not isinstance(days_count, int):
        days_count = 3
    # Target exercise count determined by phase multipliers

    def _fill_fallback_candidates() -> None:
        nonlocal weighted_exercises
        if len(weighted_exercises) >= target_exercises:
            return
        fallback_exercises = []
        for ex in exercise_bank:
            if ex in [we[0] for we in weighted_exercises]:
                continue
            if _exercise_late_windows(ex) and not active_late_window:
                continue
            if phase not in ex.get("phases", []):
                continue
            if phase in {"SPP", "TAPER"} and _is_over_100_percent_isometric(ex):
                continue
            if _is_supra_max_isometric(ex) and not (tested_1rm_available and has_isometric_setup):
                continue
            ex_equipment = _required_strength_equipment(ex)
            if not set(ex_equipment).issubset(set(equipment_access)):
                continue
            tags = ex.get("tags", [])
            if not ignore_restrictions and not evaluate_restriction_impact(
                restrictions,
                text=" ".join(str(ex.get(field, "") or "") for field in ("name", "movement", "method", "notes")),
                tags=tags,
                limit_penalty=-0.75,
            ).get("allowed", True):
                continue
            tags_lower = set(normalize_tags(tags))
            details = " ".join(
                [
                    ex.get("notes", ""),
                    ex.get("method", ""),
                    ex.get("movement", ""),
                ]
            )
            if is_banned_exercise(ex.get("name", ""), tags, fight_format, details):
                continue
            if prev_exercises and ex.get("name") in prev_exercises:
                if not (
                    any(
                        term in ex.get("name", "").lower() or term in tags_lower
                        for term in cornerstone_terms
                    )
                    or (
                        active_late_window and tags_lower & {"neural_primer", "speed", "late_strength_touch", "maximal_strength_maintenance"}
                    )
                ):
                    continue
            if legacy_taper_gate:
                if any(t in taper_banned for t in tags_lower) or any(eq in {"barbell", "trap_bar"} for eq in ex_equipment):
                    continue
                if not any(t in taper_allowed for t in tags_lower):
                    continue
            late_eval = _get_post_score_late_eval(ex, fallback_score=0.0)
            if late_eval["blocked"]:
                continue
            fallback_exercises.append(ex)
            if len(fallback_exercises) >= target_exercises - len(weighted_exercises):
                break
        weighted_exercises += [(ex, 0, {}) for ex in fallback_exercises]
    _run_real_poststep("top_selection", _fill_fallback_candidates)

    # Keep score pairs for later lookups
    score_lookup = {ex["name"]: score for ex, score, _ in weighted_exercises}
    reason_lookup = {ex["name"]: reasons for ex, _, reasons in weighted_exercises}
    excluded_by_injury: list[dict] = []

    guard_pairs = weighted_exercises[:INJURY_GUARD_SHORTLIST]
    guard_exercises = [ex for ex, _, _ in guard_pairs]
    guard_names = {ex.get("name") for ex in guard_exercises if ex.get("name")}

    # Refactored: Use factory function instead of local duplicate implementation
    _guarded_injury_decision = make_guarded_decision_factory(
        injuries,
        phase,
        fatigue,
        guard_names,
        guard_exercises,
        restrictions=restrictions,
        ignore_restrictions=ignore_restrictions,
    )
    def _exercise_key(exercise: dict) -> str:
        return str(exercise.get("id") or exercise.get("name") or id(exercise))

    profile_cache: dict[str, dict] = {}
    movement_cache: dict[str, str] = {}
    tags_cache: dict[str, set[str]] = {}
    equipment_cache: dict[str, set[str]] = {}
    guarded_decision_cache: dict[tuple[str, str], Decision] = {}
    injury_match_cache: dict[tuple[str, tuple[str, ...], tuple[str, ...]], bool] = {}
    late_safe_profile_cache: dict[str, dict] = {}

    def _cached_classify(exercise: dict) -> dict:
        key = _exercise_key(exercise)
        if key not in profile_cache:
            profile_cache[key] = classify_strength_item(exercise)
        return profile_cache[key]

    def _cached_movement(exercise: dict) -> str:
        key = _exercise_key(exercise)
        if key not in movement_cache:
            movement_cache[key] = normalize_exercise_movement(exercise)
        return movement_cache[key]

    def _cached_tags(exercise: dict) -> set[str]:
        key = _exercise_key(exercise)
        if key not in tags_cache:
            tags_cache[key] = set(normalize_tags(exercise.get("tags", [])))
        return tags_cache[key]

    def _cached_equipment(exercise: dict) -> set[str]:
        key = _exercise_key(exercise)
        if key not in equipment_cache:
            equipment_cache[key] = set(normalize_equipment_list(exercise.get("equipment", [])))
        return equipment_cache[key]

    def _cached_guarded_decision(exercise: dict) -> Decision:
        key = (_exercise_key(exercise), phase)
        if key not in guarded_decision_cache:
            guarded_decision_cache[key] = _guarded_injury_decision(exercise)
        return guarded_decision_cache[key]

    def _cached_injury_match(exercise: dict, fields: tuple[str, ...], risk_levels: tuple[str, ...]) -> bool:
        key = (_exercise_key(exercise), fields, risk_levels)
        if key not in injury_match_cache:
            injury_match_cache[key] = bool(injury_match_details(exercise, injuries, fields=fields, risk_levels=risk_levels))
        return injury_match_cache[key]

    def _cached_late_safe_profile(exercise: dict, profile: dict | None = None) -> dict:
        # The marker profile is derived from the exercise (plus the request-level
        # late window and cut bucket, both fixed for this call), so the exercise
        # identity is the whole cache key.
        key = _exercise_key(exercise)
        if key not in late_safe_profile_cache:
            late_safe_profile_cache[key] = _late_safe_marker_profile(exercise, profile=profile)
        return late_safe_profile_cache[key]

    def _selected_names(exercises: list[dict]) -> set[str]:
        return {ex.get("name") for ex in exercises if ex.get("name")}

    identities_by_name = {ex.get("name"): _selection_identity(ex) for ex in exercise_bank}

    def _excluded_identities(names: set[str]) -> set[tuple[str, str]]:
        return {identities_by_name[name] for name in names if name in identities_by_name}

    def _matching_candidates(
        predicate,
        *,
        exclude_names: set[str] | None = None,
    ) -> list[tuple[int, dict, float, dict, dict, dict]]:
        blocked_names = set(exclude_names or set())
        blocked_identities = _excluded_identities(blocked_names)
        matches: list[tuple[int, dict, float, dict, dict, dict]] = []
        for order_idx, (cand, cand_score, cand_reasons) in enumerate(weighted_exercises):
            cand_name = cand.get("name")
            if not cand_name or cand_name in blocked_names or _selection_identity(cand) in blocked_identities:
                continue
            profile = _cached_classify(cand)
            late_eval = _get_post_score_late_eval(cand, fallback_score=cand_score)
            if active_late_window and late_eval["blocked"]:
                continue
            late_safe_profile = _cached_late_safe_profile(cand, profile=profile)
            merged_reasons = _merge_post_score_reasons(
                cand_reasons,
                profile=profile,
                late_eval=late_eval,
                late_safe_profile=late_safe_profile,
            )
            if predicate(cand, cand_score, merged_reasons, profile):
                matches.append(
                    (
                        order_idx,
                        cand,
                        cand_score,
                        merged_reasons,
                        profile,
                        late_safe_profile,
                    )
                )
        return matches

    def _best_candidate(
        predicate,
        *,
        exclude_names: set[str] | None = None,
    ) -> tuple[dict, float, dict, dict, dict] | None:
        matches = _matching_candidates(predicate, exclude_names=exclude_names)
        if not matches:
            return None
        order_idx, cand, cand_score, cand_reasons, profile, late_safe_profile = max(
            matches,
            key=lambda entry: _late_safe_candidate_priority(
                entry[2],
                profile=entry[4],
                late_safe_profile=entry[5],
                order_idx=entry[0],
            ),
        )
        _ = order_idx
        return cand, cand_score, cand_reasons, profile, late_safe_profile

    def _replace_exercise(
        exercises: list[dict],
        *,
        index: int,
        replacement: dict,
        replacement_score: float,
        replacement_reasons: dict,
    ) -> None:
        exercises[index] = replacement
        replacement_name = replacement.get("name")
        if replacement_name:
            score_lookup[replacement_name] = replacement_score
            reason_lookup[replacement_name] = replacement_reasons

    def _support_replacement_index(
        exercises: list[dict],
        *,
        replacement_profile: dict | None = None,
        replacement_late_safe_profile: dict | None = None,
        eligible_indices: set[int] | None = None,
    ) -> int | None:
        eligible = [idx for idx in range(len(exercises))
                    if eligible_indices is None or idx in eligible_indices]
        support_positions = [
            idx
            for idx in eligible
            if _cached_classify(exercises[idx])["support_only"]
        ]
        candidate_indices = support_positions or eligible
        if not candidate_indices:
            return None

        if _generic_loaded_anchor(
            replacement_profile or {},
            replacement_late_safe_profile or {"explicit": False},
        ):
            non_explicit_late_safe_indices = [
                idx
                for idx in candidate_indices
                if not _cached_late_safe_profile(exercises[idx])["explicit"]
            ]
            if non_explicit_late_safe_indices:
                candidate_indices = non_explicit_late_safe_indices
            else:
                return None

        return min(
            candidate_indices,
            key=lambda idx: score_lookup.get(exercises[idx].get("name"), 0.0),
        )

    def _sorted_external_candidates(
        candidates: list[dict],
        *,
        exclude_names: set[str] | None = None,
        base_reasons: dict[str, dict] | None = None,
    ) -> list[tuple[dict, dict, dict, dict]]:
        blocked_names = set(exclude_names or set())
        blocked_identities = _excluded_identities(blocked_names)
        ordered_candidates: list[tuple[tuple, dict, dict, dict, dict]] = []
        reasons_by_name = base_reasons or {}
        for order_idx, cand in enumerate(candidates):
            cand_name = cand.get("name")
            if not cand_name or cand_name in blocked_names or _selection_identity(cand) in blocked_identities:
                continue
            profile = _cached_classify(cand)
            late_eval = _get_post_score_late_eval(
                cand,
                fallback_score=score_lookup.get(cand_name, 0.0),
            )
            if active_late_window and late_eval["blocked"]:
                continue
            late_safe_profile = _cached_late_safe_profile(cand, profile=profile)
            merged_reasons = _merge_post_score_reasons(
                reasons_by_name.get(cand_name, {}),
                profile=profile,
                late_eval=late_eval,
                late_safe_profile=late_safe_profile,
            )
            ordered_candidates.append(
                (
                    _late_safe_candidate_priority(
                        score_lookup.get(cand_name, 0.0),
                        profile=profile,
                        late_safe_profile=late_safe_profile,
                        order_idx=order_idx,
                    ),
                    cand,
                    merged_reasons,
                    profile,
                    late_safe_profile,
                )
            )
        ordered_candidates.sort(key=lambda entry: entry[0], reverse=True)
        return [
            (cand, merged_reasons, profile, late_safe_profile)
            for _, cand, merged_reasons, profile, late_safe_profile in ordered_candidates
        ]

    def _required_base_categories(exercises: list[dict]) -> list[str]:
        return missing_base_categories(
            exercises,
            require_lower_body_explosive_anchor=require_lower_body_explosive_anchor,
        )

    def _promote_base_categories(exercises: list[dict]) -> list[dict]:
        updated = list(exercises)
        targets = {priority_adaptation(value) for value in
                   [*priority_profile.all_goals, *priority_profile.all_weak_areas]} - {""}
        for category in _required_base_categories(updated):
            # Earlier promotions may already have supplied more than one role.
            missing = set(_required_base_categories(updated))
            if category not in missing:
                continue
            adaptation_sets = [strength_adaptation_relevance(
                ex, prescription_type=_classify_prescription_type(ex)) & targets for ex in updated]
            adaptations = set().union(*adaptation_sets)
            retain_rotation = rotational_power_coverage_gap and any(
                _cached_classify(ex)["anchor_capable"] and "rotational_power" in _cached_classify(ex)["base_categories"]
                for ex in updated)
            remaining_options = []
            for idx in range(len(updated)):
                remaining = updated[:idx] + updated[idx + 1:]
                remaining_adaptations = set().union(*(values for j, values in enumerate(adaptation_sets) if j != idx))
                remaining_missing = set(_required_base_categories(remaining))
                # A contrast pair supplies a specific heavy + explosive pattern.
                # Other strength/power witnesses do not replace that pairing.
                removed = updated[idx]
                if _classify_prescription_type(removed) == "contrast" and not any(
                        _classify_prescription_type(ex) == "contrast"
                        and _cached_movement(ex) == _cached_movement(removed) for ex in remaining):
                    remaining_missing.add("contrast:" + _cached_movement(removed))
                if retain_rotation and not any(_cached_classify(ex)["anchor_capable"]
                        and "rotational_power" in _cached_classify(ex)["base_categories"] for ex in remaining):
                    remaining_missing.add("rotational_power")
                remaining_options.append((idx, remaining, remaining_missing, remaining_adaptations))
            replacement_indices: dict[str, int] = {}

            def can_promote(cand, _score, _reasons, profile):
                if not profile["anchor_capable"] or category not in profile["base_categories"]:
                    return False
                if _cached_guarded_decision(cand).action == "exclude":
                    return False
                movement = _cached_movement(cand)
                candidate_adaptations = strength_adaptation_relevance(cand, prescription_type=_classify_prescription_type(cand))
                eligible = set()
                for idx, remaining, remaining_missing, remaining_adaptations in remaining_options:
                    # A third movement would be discarded by the next cap pass.
                    if movement != "unknown" and sum(_cached_movement(ex) == movement for ex in remaining) >= 2:
                        continue
                    supplied = set(profile["base_categories"])
                    if _classify_prescription_type(cand) == "contrast":
                        supplied.add("contrast:" + movement)
                    if remaining_missing - supplied - missing:
                        continue
                    # Coverage must not displace a selected training stimulus.
                    if not adaptations <= remaining_adaptations | candidate_adaptations:
                        continue
                    eligible.add(idx)
                index = _support_replacement_index(
                    updated, replacement_profile=profile,
                    replacement_late_safe_profile=_cached_late_safe_profile(cand, profile),
                    eligible_indices=eligible,
                )
                if index is None:
                    return False
                replacement_indices[cand["name"]] = index
                return True

            selected_names = _selected_names(updated)
            replacement_entry = _best_candidate(
                can_promote,
                exclude_names=selected_names,
            )
            if not replacement_entry:
                continue
            replacement, replacement_score, replacement_reasons, _replacement_profile, _late_safe_profile = replacement_entry
            replace_index = replacement_indices[replacement["name"]]
            _replace_exercise(
                updated,
                index=replace_index,
                replacement=replacement,
                replacement_score=replacement_score,
                replacement_reasons=replacement_reasons,
            )
        return updated

    def _maybe_add_force_isometric(exercises: list[dict]) -> list[dict]:
        if phase not in {"GPP", "SPP"}:
            return exercises
        selected_profiles = [_cached_classify(ex) for ex in exercises]
        if any(profile["force_isometric"] for profile in selected_profiles):
            return exercises
        protective_context = bool(injuries or restrictions or fatigue in {"moderate", "high"})
        if not protective_context and any(profile["anchor_capable"] for profile in selected_profiles):
            return exercises
        selected_scores = [
            score_lookup.get(ex.get("name"), 0.0)
            for ex in exercises
            if ex.get("name") is not None
        ]
        cutoff_score = min(selected_scores) if selected_scores else 0.0
        margin = score_band_margin(selected_scores, phase=phase)
        selected_names = _selected_names(exercises)
        replacement_entry = _best_candidate(
            lambda cand, cand_score, _reasons, profile: profile["force_isometric"]
            and (protective_context or cand_score >= cutoff_score - margin),
            exclude_names=selected_names,
        )
        if not replacement_entry:
            return exercises
        replacement, replacement_score, replacement_reasons, replacement_profile, late_safe_profile = replacement_entry
        replace_index = _support_replacement_index(
            exercises,
            replacement_profile=replacement_profile,
            replacement_late_safe_profile=late_safe_profile,
        )
        if replace_index is None:
            return exercises
        updated = list(exercises)
        _replace_exercise(
            updated,
            index=replace_index,
            replacement=replacement,
            replacement_score=replacement_score,
            replacement_reasons=replacement_reasons,
        )
        return updated

    def _enforce_session_quality(exercises: list[dict]) -> list[dict]:
        updated = list(exercises)
        support_cap = max(num_strength_sessions * SESSION_SUPPORT_CAP_MULTIPLIER, 2)

        def current_support_cost() -> int:
            return support_budget_cost(
                updated,
                core_balance_bonus=core_balance_bonus,
            )

        guard = 0
        while current_support_cost() > support_cap:
            guard += 1
            max_iter = bounded_max_iterations(len(updated))
            if guard > max_iter:
                log_fail_safe_degrade(module="strength", phase=phase, reason="session_quality_guard", target=support_cap, actual=current_support_cost())
                break
            selected_names = _selected_names(updated)
            replacement_entry = _best_candidate(
                lambda cand, _score, _reasons, profile: profile["anchor_capable"],
                exclude_names=selected_names,
            )
            replacement = replacement_score = replacement_reasons = replacement_profile = late_safe_profile = None
            if replacement_entry:
                (
                    replacement,
                    replacement_score,
                    replacement_reasons,
                    replacement_profile,
                    late_safe_profile,
                ) = replacement_entry
            replace_index = _support_replacement_index(
                updated,
                replacement_profile=replacement_profile,
                replacement_late_safe_profile=late_safe_profile,
            )
            if not replacement_entry or replace_index is None:
                log_fail_safe_degrade(module="strength", phase=phase, reason="session_quality_no_replacement", target=support_cap, actual=current_support_cost())
                break
            _replace_exercise(
                updated,
                index=replace_index,
                replacement=replacement,
                replacement_score=replacement_score,
                replacement_reasons=replacement_reasons,
            )

        sessions = infer_strength_sessions(updated, num_strength_sessions)
        for session in sessions:
            items = session.get("items", [])
            positions = session.get("positions", [])
            if not items or not positions:
                continue
            has_anchor = any(_cached_classify(ex)["anchor_capable"] for ex in items)
            if not has_anchor:
                selected_names = _selected_names(updated)
                replacement_entry = _best_candidate(
                    lambda cand, _score, _reasons, profile: profile["anchor_capable"],
                    exclude_names=selected_names,
                )
                if replacement_entry:
                    replacement, replacement_score, replacement_reasons, replacement_profile, late_safe_profile = replacement_entry
                    local_candidates = [
                        idx
                        for idx, exercise in enumerate(items)
                        if _cached_classify(exercise)["support_only"]
                    ] or list(range(len(items)))
                    if _generic_loaded_anchor(replacement_profile, late_safe_profile):
                        local_candidates = [
                            idx
                            for idx in local_candidates
                            if not _cached_late_safe_profile(items[idx])["explicit"]
                        ]
                    if not local_candidates:
                        continue
                    local_support = min(
                        local_candidates,
                        key=lambda idx: score_lookup.get(items[idx].get("name"), 0.0),
                    )
                    _replace_exercise(
                        updated,
                        index=positions[local_support],
                        replacement=replacement,
                        replacement_score=replacement_score,
                        replacement_reasons=replacement_reasons,
                    )

        sessions = infer_strength_sessions(updated, num_strength_sessions)
        for session in sessions:
            items = session.get("items", [])
            positions = session.get("positions", [])
            if not items or not positions:
                continue
            anchor_local_index = next(
                (
                    idx
                    for idx, exercise in enumerate(items)
                    if _cached_classify(exercise)["anchor_capable"]
                ),
                None,
            )
            if anchor_local_index is None:
                continue
            if session_support_count_before_anchor(items) > 1 or session_starts_with_support_only(items):
                first_position = positions[0]
                anchor_position = positions[anchor_local_index]
                updated[first_position], updated[anchor_position] = updated[anchor_position], updated[first_position]

        # Sessions are dealt round-robin by position, so where a scarce strength
        # lift lands is otherwise an accident of rank. When strength is a
        # selected priority, the phase's first weekly session (primary /
        # neural-plus) holds one if any selected exercise develops strength.
        # Positions are swapped; membership is unchanged.
        if strength_priority_selected and len(sessions := infer_strength_sessions(updated, num_strength_sessions)) > 1:
            first = sessions[0]
            if not any("strength" in strength_adaptation_relevance(ex, prescription_type=_classify_prescription_type(ex)) for ex in first.get("items", [])):
                donor_position = next(
                    (
                        position
                        for session in sessions[1:]
                        for exercise, position in zip(session.get("items", []), session.get("positions", []))
                        if "strength" in strength_adaptation_relevance(exercise, prescription_type=_classify_prescription_type(exercise))
                    ),
                    None,
                )
                # The donor receives an anchor in return, so it keeps the
                # anchor the pass above guaranteed every session.
                swap_local = next(
                    (
                        idx
                        for idx, exercise in reversed(list(enumerate(first.get("items", []))))
                        if _cached_classify(exercise)["anchor_capable"]
                    ),
                    None,
                )
                if donor_position is not None and swap_local is not None:
                    first_position = first["positions"][swap_local]
                    updated[first_position], updated[donor_position] = updated[donor_position], updated[first_position]
        return updated

    strength_maintenance_intent = _has_strength_maintenance_intent(
        goals=goals,
        weaknesses=weaknesses,
        flags=flags,
    )

    def _ensure_selected_strength_exposure(exercises: list[dict]) -> list[dict]:
        normal_strength = (phase in {"GPP", "SPP"} and strength_priority_selected
                           and (not isinstance(days_until_fight, int) or days_until_fight > 13))
        early_taper = (phase == "TAPER" and late_window in EARLY_TAPER_STRENGTH_WINDOWS
                       and strength_maintenance_intent)
        if not exercises or not (normal_strength or early_taper):
            return exercises

        def qualifies(exercise: dict) -> bool:
            return ("strength" in strength_adaptation_relevance(exercise, prescription_type=_classify_prescription_type(exercise)) if normal_strength
                    else _is_real_strength_maintenance_touch(exercise, _cached_classify(exercise)))

        if any(qualifies(exercise) for exercise in exercises):
            return exercises

        selected_names = _selected_names(exercises)
        replacement_entry = _best_candidate(
            lambda cand, _score, _reasons, profile: qualifies(cand)
            and _cached_guarded_decision(cand).action != "exclude",
            exclude_names=selected_names,
        )
        if not replacement_entry:
            return exercises

        replacement, replacement_score, replacement_reasons, _replacement_profile, _late_safe_profile = replacement_entry
        replacement_name = replacement.get("name")
        if not replacement_name:
            return exercises

        updated = list(exercises)
        primer_only_indices = [
            idx
            for idx, exercise in enumerate(updated)
            if _is_primer_only_strength_touch(exercise, _cached_classify(exercise))
        ]
        candidate_indices = primer_only_indices or list(range(len(updated)))
        if normal_strength:
            # Preserve other actual selected stimuli; trade a redundant or
            # unrequested member, never the only primary power/speed witness.
            targets = {priority_adaptation(value) for value in
                       [*priority_profile.all_goals, *priority_profile.all_weak_areas]} - {""}
            current = set().union(*(strength_adaptation_relevance(ex, prescription_type=_classify_prescription_type(ex)) for ex in updated)) & targets
            candidate_indices = [i for i in candidate_indices
                if current <= set().union(strength_adaptation_relevance(replacement, prescription_type=_classify_prescription_type(replacement)),
                    *(strength_adaptation_relevance(ex, prescription_type=_classify_prescription_type(ex)) for j, ex in enumerate(updated) if j != i))]
            if not candidate_indices:
                return exercises
        replace_index = min(
            candidate_indices,
            key=lambda idx: (
                score_lookup.get(updated[idx].get("name"), 0.0),
                -_strength_maintenance_support_score(
                    updated[idx],
                    _cached_classify(updated[idx]),
                    window=late_window,
                ),
                -idx,
            ),
        )
        replacement_reasons.setdefault("reason_codes", [])
        replacement_reasons["reason_codes"] = list(
            dict.fromkeys(
                list(replacement_reasons.get("reason_codes", []))
                + ["selected_strength_exposure_retained" if normal_strength else "early_taper_strength_maintenance_selected"]
            )
        )
        _replace_exercise(
            updated,
            index=replace_index,
            replacement=replacement,
            replacement_score=replacement_score,
            replacement_reasons=replacement_reasons,
        )
        return updated

    def _append_core_balance_bonus(exercises: list[dict]) -> list[dict]:
        if core_balance_bonus <= 0 or target_exercises <= 0 or num_strength_sessions <= 0:
            return exercises

        # Keep this deliberately small: at most two dedicated core/balance
        # support exercises can survive, and only one can be added beyond the
        # ordinary selector budget.
        current_core_balance = sum(
            1
            for exercise in exercises
            if _cached_classify(exercise).get("core_balance_support")
        )
        if current_core_balance >= 2 or len(exercises) >= target_exercises + 1:
            return exercises

        movement_counts: dict[str, int] = {}
        for exercise in exercises:
            movement = _cached_movement(exercise)
            if movement != "unknown":
                movement_counts[movement] = movement_counts.get(movement, 0) + 1

        selected_names = _selected_names(exercises)
        bonus_entry = _best_candidate(
            lambda cand, _score, _reasons, profile: (
                bool(profile.get("core_balance_support"))
                and _cached_guarded_decision(cand).action != "exclude"
                and (
                    _cached_movement(cand) == "unknown"
                    or movement_counts.get(_cached_movement(cand), 0) < 2
                )
                and (
                    not injuries
                    or not _cached_injury_match(cand, ("name", "movement", "method"), ("exclude",))
                )
            ),
            exclude_names=selected_names,
        )
        if not bonus_entry:
            return exercises

        bonus, bonus_score, bonus_reasons, _profile, _late_safe_profile = bonus_entry
        bonus_name = bonus.get("name")
        if not bonus_name:
            return exercises
        bonus_reasons.setdefault("reason_codes", [])
        bonus_reasons["reason_codes"] = list(
            dict.fromkeys(
                list(bonus_reasons.get("reason_codes", []))
                + ["core_balance_low_cost_bonus"]
            )
        )
        score_lookup[bonus_name] = bonus_score
        reason_lookup[bonus_name] = bonus_reasons
        return [*exercises, bonus]

    candidate_metadata: dict[str, dict[str, object]] = {}
    for ex, score, reasons in weighted_exercises:
        name = ex.get("name")
        if not name:
            continue
        profile = _cached_classify(ex)
        late_eval = _get_post_score_late_eval(ex, fallback_score=score)
        candidate_metadata[name] = {
            "exercise": ex,
            "score": score,
            "reasons": reasons,
            "profile": profile,
            "movement": _cached_movement(ex),
            "tags": _cached_tags(ex),
            "equipment": _cached_equipment(ex),
            "late_eval": late_eval,
            "late_safe_profile": _cached_late_safe_profile(ex, profile),
        }

    top_pairs = weighted_exercises[:target_exercises]
    top_exercises = [ex for ex, _, _ in top_pairs]
    # Remove any duplicate exercise names that slipped through scoring
    seen_exercises: set[str] = set()
    unique_top: list[dict] = []
    movement_counts: dict[str, int] = {}
    for ex in top_exercises:
        name = ex.get("name")
        if name not in seen_exercises:
            movement = _cached_movement(ex)
            if movement != "unknown" and movement_counts.get(movement, 0) >= 2:
                continue
            seen_exercises.add(name)
            movement_counts[movement] = movement_counts.get(movement, 0) + 1
            unique_top.append(ex)
    top_exercises = unique_top

    base_exercises = top_exercises
    # Final safety deduplication in case database contained repeats
    seen_names: set[str] = set()
    unique_base: list[dict] = []
    for ex in base_exercises:
        name = ex.get("name")
        if name not in seen_names:
            seen_names.add(name)
            unique_base.append(ex)
    base_exercises = unique_base

    def _apply_movement_caps(exercises: list[dict]) -> list[dict]:
        movement_counts: dict[str, int] = {}
        identities: set[tuple[str, str]] = set()
        capped: list[dict] = []
        for ex in exercises:
            identity = _selection_identity(ex)
            if identity in identities:
                continue
            movement = _cached_movement(ex)
            if movement != "unknown" and movement_counts.get(movement, 0) >= 2:
                continue
            movement_counts[movement] = movement_counts.get(movement, 0) + 1
            identities.add(identity)
            capped.append(ex)

        if len(capped) < target_exercises:
            guard = 0
            while len(capped) < target_exercises:
                guard += 1
                max_iter = bounded_max_iterations(target_exercises)
                if guard > max_iter:
                    log_fail_safe_degrade(module="strength", phase=phase, reason="movement_caps_guard", target=target_exercises, actual=len(capped))
                    break
                selected_names = _selected_names(capped)
                replacement_entry = _best_candidate(
                    lambda cand, _score, _reasons, _profile: (
                        _cached_movement(cand) == "unknown"
                        or movement_counts.get(_cached_movement(cand), 0) < 2
                    ),
                    exclude_names=selected_names,
                )
                if not replacement_entry:
                    log_fail_safe_degrade(module="strength", phase=phase, reason="movement_caps_no_replacement", target=target_exercises, actual=len(capped))
                    break
                cand, _cand_score, cand_reasons, _profile, _late_safe_profile = replacement_entry
                movement = _cached_movement(cand)
                if movement != "unknown" and movement_counts.get(movement, 0) >= 2:
                    log_fail_safe_degrade(module="strength", phase=phase, reason="movement_cap_blocked", target=target_exercises, actual=len(capped))
                    break
                movement_counts[movement] = movement_counts.get(movement, 0) + 1
                capped.append(cand)
                reason_lookup.setdefault(cand.get("name"), cand_reasons)
        return capped

    base_exercises = _run_real_poststep("movement_caps_pass_1", lambda: _apply_movement_caps(base_exercises))
    base_exercises = _run_real_poststep("base_category_promotion", lambda: _promote_base_categories(base_exercises))
    base_exercises = _run_real_poststep("movement_caps_pass_2", lambda: _apply_movement_caps(base_exercises))
    base_exercises = _run_real_poststep("force_isometric", lambda: _maybe_add_force_isometric(base_exercises))
    base_exercises = _run_real_poststep("movement_caps_pass_3", lambda: _apply_movement_caps(base_exercises))
    base_exercises = _run_real_poststep("session_quality", lambda: _enforce_session_quality(base_exercises))

    # ------ CONFLICT GUARD: heavy RDL with med-ball rotation ------
    def _enforce_conflicts(ex_list):
        has_med_ball_rot = any(
            "medicine_ball" in _cached_equipment(ex)
            and "rotational" in _cached_tags(ex)
            for ex in ex_list
        )
        if not has_med_ball_rot:
            return
        for idx, ex in enumerate(ex_list):
            name_lower = ex.get("name", "").lower()
            if "heavy rdl" in name_lower or ("rdl" in name_lower and "heavy" in name_lower):
                replacement_entry = _best_candidate(
                    lambda cand, _score, _reasons, _profile: (
                        cand.get("name", "").lower() != name_lower
                        and "heavy rdl" not in cand.get("name", "").lower()
                        and not (
                            "medicine_ball" in _cached_equipment(cand)
                            and "rotational" in _cached_tags(cand)
                        )
                    ),
                    exclude_names=_selected_names(ex_list) - {ex.get("name")},
                )
                if replacement_entry:
                    cand, _cand_score, cand_reasons, _profile, _late_safe_profile = replacement_entry
                    ex_list[idx] = cand
                    reason_lookup[cand.get("name")] = cand_reasons
                    return

    _run_real_poststep("conflict_guard", lambda: _enforce_conflicts(base_exercises))

    def _finalize_injury_safe_exercises(ex_list: list[dict]) -> list[dict]:
        used_names = {ex.get("name") for ex in ex_list if ex.get("name")}
        updated: list[dict | None] = []
        injuries_ctx = {"injuries": injuries, "phase": phase, "fatigue": fatigue}
        def _record_exclusion(exercise: dict, decision: Decision) -> None:
            reason = decision.reason if isinstance(decision.reason, dict) else {}
            excluded_by_injury.append({
                "name": exercise.get("name", "<unnamed>"),
                "score": float(score_lookup.get(exercise.get("name"), 0.0)),
                "region": reason.get("region"),
                "severity": reason.get("severity"),
                "bucket": reason.get("bucket"),
                "matched_tags": list(decision.matched_tags or []),
            })
        max_scan = bounded_max_iterations(len(ex_list))
        for scan_count, ex in enumerate(ex_list, start=1):
            if scan_count > max_scan:
                log_fail_safe_degrade(module="strength", phase=phase, reason="injury_finalize_scan_guard", target=len(ex_list), actual=len(updated))
                break
            decision = _cached_guarded_decision(ex)
            if decision.action != "exclude":
                updated.append(ex)
                continue
            _record_exclusion(ex, decision)
            # Log exclusion using new helper
            _log_exclusion(f"strength:{phase}", ex, decision)
            replacement = None
            replacement_decision = None
            candidate_pool: list[dict] = []
            safe_reasons: dict[str, dict] = {}
            for cand, cand_reasons, _profile, _late_safe_profile in _sorted_external_candidates(
                [cand for cand, _, _ in guard_pairs],
                exclude_names=used_names - {ex.get("name")},
                base_reasons={cand.get("name"): cand_reasons for cand, _, cand_reasons in guard_pairs if cand.get("name")},
            ):
                cand_name = cand.get("name")
                if not cand_name:
                    continue
                candidate_pool.append(cand)
                safe_reasons[cand_name] = cand_reasons
                if len(candidate_pool) >= bounded_max_iterations(len(ex_list), multiplier=6, floor=16):
                    break

            if candidate_pool:
                # Reuse the per-run guarded decision cache: the same
                # exercise/injury pairs are otherwise re-evaluated from
                # scratch on every exclusion, and an unguarded verdict here
                # can pick a candidate the terminal re-check below excludes.
                replacement, replacement_decision = pick_safe_replacement(
                    ex,
                    candidate_pool,
                    injuries_ctx,
                    decide=_cached_guarded_decision,
                )
            if replacement and replacement_decision:
                rep_name = replacement.get("name")
                if rep_name:
                    reason_lookup[rep_name] = safe_reasons.get(rep_name, {})
                    used_names.add(rep_name)
                # Log replacement when INJURY_DEBUG is enabled
                _log_replacement(f"strength:{phase}", ex.get("name", "<unnamed>"), rep_name or "<unnamed>")
                updated.append(replacement)
            else:
                log_fail_safe_degrade(module="strength", phase=phase, reason="injury_finalize_no_replacement", target=len(ex_list), actual=len(updated))
                updated.append(None)
        finalized: list[dict] = []
        for ex in updated:
            if not ex:
                continue
            final_decision = _cached_guarded_decision(ex)
            if final_decision.action == "exclude":
                _record_exclusion(ex, final_decision)
                # Log exclusion using new helper
                _log_exclusion(f"strength:{phase}", ex, final_decision)
                continue
            finalized.append(ex)
        return finalized

    base_exercises = _finalize_injury_safe_exercises(base_exercises)

    def _final_keyword_guard(ex_list: list[dict]) -> list[dict]:
        if not injuries:
            return ex_list
        used_names = {ex.get("name") for ex in ex_list if ex.get("name")}
        updated: list[dict] = []
        max_scan = bounded_max_iterations(len(ex_list))
        for scan_count, ex in enumerate(ex_list, start=1):
            if scan_count > max_scan:
                log_fail_safe_degrade(module="strength", phase=phase, reason="keyword_guard_scan_guard", target=len(ex_list), actual=len(updated))
                break
            if not _cached_injury_match(ex, ("name", "movement", "method"), ("exclude",)):
                updated.append(ex)
                continue
            replacement = None
            replacement_entry = _best_candidate(
                lambda cand, _score, _reasons, _profile: not _cached_injury_match(cand, ("name", "movement", "method"), ("exclude",))
                and _cached_guarded_decision(cand).action != "exclude",
                exclude_names=used_names - {ex.get("name")},
            )
            if replacement_entry:
                cand, _cand_score, cand_reasons, _profile, _late_safe_profile = replacement_entry
                cand_name = cand.get("name")
                if cand_name:
                    reason_lookup[cand_name] = cand_reasons
                    used_names.add(cand_name)
                replacement = cand
            if replacement:
                updated.append(replacement)
            else:
                log_fail_safe_degrade(module="strength", phase=phase, reason="keyword_guard_no_replacement", target=len(ex_list), actual=len(updated))
        return updated

    base_exercises = _run_real_poststep("injury_safe_finalize_1", lambda: _finalize_injury_safe_exercises(base_exercises))
    base_exercises = _run_real_poststep("session_quality_final", lambda: _enforce_session_quality(base_exercises))
    base_exercises = _run_real_poststep("movement_caps_final", lambda: _apply_movement_caps(base_exercises))
    base_exercises = _run_real_poststep("injury_safe_finalize_2", lambda: _finalize_injury_safe_exercises(base_exercises))
    base_exercises = _run_real_poststep("keyword_guard", lambda: _final_keyword_guard(base_exercises))
    base_exercises = _run_real_poststep(
        "selected_strength_exposure_final",
        lambda: _ensure_selected_strength_exposure(base_exercises),
    )
    base_exercises = _run_real_poststep(
        "core_balance_low_cost_bonus",
        lambda: _append_core_balance_bonus(base_exercises),
    )
    base_exercises = _run_real_poststep(
        "movement_caps_after_core_balance_bonus",
        lambda: _apply_movement_caps(base_exercises),
    )
    # Safety net: the core-balance bonus and the final movement-cap fill add
    # exercises AFTER the last injury pass and do not injury-check what they add,
    # so an injury-excluded movement could re-enter the block when the safe pool
    # is exhausted. Re-run the injury-safe finalizer as the terminal step so no
    # exercise the guard classifies as "exclude" can survive — safety beats
    # exercise count, and the finalizer already degrades quantity (and logs the
    # degradation) when no safe replacement exists.
    base_exercises = _run_real_poststep(
        "injury_safe_finalize_terminal",
        lambda: _finalize_injury_safe_exercises(base_exercises),
    )

    _run_real_poststep("movement_normalization", lambda: [_cached_movement(ex) for ex in base_exercises])

    if injury_trace and restrictions:
        active_restrictions = sorted({r.get("restriction", "generic_constraint") for r in restrictions})
        top_blocks = sorted(
            restriction_blocked_items,
            key=lambda item: item.get("risk", 0.0),
            reverse=True,
        )[:5]
        formatted_blocks = [
            {
                "name": item.get("name"),
                "rule": item.get("match", {}).get("restriction"),
                "method": item.get("match", {}).get("method"),
                "confidence": item.get("match", {}).get("confidence"),
                "risk": item.get("risk"),
            }
            for item in top_blocks
        ]
        logger.info(
            "[guard-report] strength:%s restrictions=%s candidates=%d blocked=%d reasons=%s",
            phase,
            active_restrictions,
            restriction_candidates,
            restriction_blocked,
            dict(restriction_reason_counts),
        )
        logger.info(
            "[guard-report] strength:%s top_blocks=%s",
            phase,
            formatted_blocks,
        )
        logger.info(
            "[guard-report] strength:%s warnings=%s",
            phase,
            dict(restriction_warning_counts),
        )

    used_days = training_days[:num_strength_sessions]

    strength_output = _run_real_poststep("format_strength_block", lambda: format_strength_block(phase, fatigue, base_exercises))
    def _build_capped_candidate_reservoir():
        # Alternates become dated assignments downstream. Reuse the settled
        # verdict so an authored prohibition cannot re-enter through that pool.
        metadata_safe = [entry for entry in weighted_exercises
                         if not (entry[0].get("avoid_if") or entry[0].get("contraindications"))
                         or _cached_guarded_decision(entry[0]).reason.get("bucket") != "safety_metadata"]
        capped_weighted = metadata_safe[:500]
        if len(metadata_safe) > len(capped_weighted):
            log_fail_safe_degrade(module="strength", phase=phase, reason="candidate_reservoir_capped", target=len(metadata_safe), actual=len(capped_weighted))
        elif source_candidate_count > 500:
            log_fail_safe_degrade(module="strength", phase=phase, reason="candidate_reservoir_capped", target=source_candidate_count, actual=len(capped_weighted))
        return _build_strength_candidate_reservoir(capped_weighted)
    candidate_reservoir = _run_real_poststep("candidate_reservoir_build", _build_capped_candidate_reservoir)
    deduped_late_blocks = {
        (
            entry.get("name", ""),
            tuple(entry.get("reason_codes", [])),
        ): entry
        for entry in late_window_blocked
    }
    deduped_late_penalties = {
        (
            entry.get("name", ""),
            tuple(entry.get("penalty_codes", [])),
        ): entry
        for entry in late_window_penalized
    }
    candidate_reservoir["__late_window__"] = {
        "window": late_window,
        "blocked": sorted(
            deduped_late_blocks.values(),
            key=lambda entry: (entry.get("name", ""), entry.get("score", 0.0)),
        ),
        "penalized": sorted(
            deduped_late_penalties.values(),
            key=lambda entry: (entry.get("name", ""), entry.get("score", 0.0)),
        ),
        "ambiguous_tag_gaps": [
            late_window_ambiguous[name]
            for name in sorted(late_window_ambiguous)
        ][:300],
    }

    all_tags = []
    for ex in base_exercises:
        all_tags.extend(ex.get("tags", []))

    def _build_why_log():
        entries = []
        for ex in base_exercises:
            name = ex.get("name")
            reasons = reason_lookup.get(name, {}).copy()
            reasons.setdefault("final_score", score_lookup.get(name, 0))
            explanation = _strength_explanation(reasons)
            entries.append({"name": name, "reasons": reasons, "explanation": explanation})
        return entries
    why_log = _run_real_poststep("why_log_build", _build_why_log)

    total_elapsed = perf_counter() - strength_started_at
    logger.info("[stage1] strength_phase_elapsed phase=%s elapsed=%.2f", phase, total_elapsed)
    if total_elapsed > 10.0:
        logger.warning("[stage1] slow_strength_phase phase=%s elapsed=%.2f", phase, total_elapsed)
    if total_elapsed > 30.0:
        logger.warning("[stage1] slow_strength_total elapsed=%.2f", total_elapsed)

    return {
        "block": strength_output,
        "num_sessions": len(used_days),
        "preferred_tags": list(dict.fromkeys(all_tags)),
        "exercises": base_exercises,
        "why_log": why_log,
        "candidate_reservoir": candidate_reservoir,
        "late_window_diagnostics": candidate_reservoir.get("__late_window__", {}),
    }
    
