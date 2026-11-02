from __future__ import annotations

import re
from typing import Any

from .tagging import normalize_tags
from .training_context import normalize_equipment_list

ANCHOR_CAPABLE_CLASSES = {"anchor_loaded", "anchor_power", "anchor_force_isometric"}
SUPPORT_ONLY_CLASSES = {"support_isometric", "support_accessory", "rehab_support"}

STRENGTH_QUALITY_WEIGHTS = {
    "anchor_loaded": {"GPP": 1.0, "SPP": 1.0, "TAPER": 0.75},
    "anchor_power": {"GPP": 0.7, "SPP": 0.9, "TAPER": 0.6},
    "anchor_force_isometric": {"GPP": 0.45, "SPP": 0.35, "TAPER": 0.75},
    "support_only_penalty": {"GPP": -0.7, "SPP": -0.8, "TAPER": -0.45},
    "rehab_support_penalty": {"GPP": -0.9, "SPP": -1.0, "TAPER": -0.5},
    "duplicate_support_penalty": {"GPP": -0.35, "SPP": -0.4, "TAPER": -0.2},
}

ISOMETRIC_SESSION_SCORE_BAND = {"GPP": 0.08, "SPP": 0.08, "TAPER": 0.05}

_LOADED_EQUIPMENT = {
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

_PUSH_HINTS = {"push", "press", "bench", "dip"}
_PULL_HINTS = {"pull", "row", "chin", "pullup", "pull-up"}
_LOWER_BODY_HINTS = {"squat", "hinge", "deadlift", "rdl", "lunge", "split squat", "step-up", "step up"}
_UNILATERAL_HINTS = {"unilateral", "single_leg", "single leg", "single-leg", "split squat", "step-up", "step up"}
_POWER_HINTS = {
    "explosive",
    "rate_of_force",
    "speed_strength",
    "ballistic",
    "reactive",
    "medicine_ball",
    "med ball",
    "throw",
    "slam",
    "jump",
    "hop",
    "bound",
    "contrast",
    "neural_primer",
}
_LOWER_BODY_JUMP_TAGS = {
    "mech_lower_jump",
    "mech_lower_lateral",
}
_LOWER_BODY_HIP_BALLISTIC_TAGS = {
    "mech_lower_hip_hinge",
    "hip_dominant",
    "posterior_chain",
}
_ROTATIONAL_POWER_TAGS = {"rotational", "mech_trunk_rotation"}
_UPPER_BODY_BALLISTIC_TAGS = {
    "mech_upper_press",
    "upper_body",
}
_SUPPORT_HINTS = {
    "carry",
    "loaded_carry",
    "core",
    "trunk",
    "stability",
    "anti_rotation",
    "anti rotation",
    "breathing",
    "mobility",
    "neck",
    "parasympathetic",
}
_REHAB_HINTS = {"rehab", "therapy", "prehab"}
_CORE_BALANCE_SUPPORT_TAGS = {
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
_ISOMETRIC_HINTS = {"isometric", "iso hold", "iso", "hold"}
_FORCE_ISOMETRIC_HINTS = {
    "pins",
    "pin",
    "% 1rm",
    "%1rm",
    "110%",
    "115%",
    "120%",
    "mid-shin",
    "deadlift isometric",
    "squat hold",
    "split squat hold",
    "overcoming",
    "yielding",
}


def _collect_tags(item: dict[str, Any]) -> set[str]:
    tags = set(normalize_tags(item.get("tags") or []))
    tags.update(normalize_tags(item.get("movement_patterns") or []))
    movement = str(item.get("movement") or "").strip().lower()
    if movement:
        tags.add(movement.replace(" ", "_"))
    return {tag for tag in tags if tag}


def _collect_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("name", "movement", "method", "prescription", "purpose", "notes", "description"):
        value = item.get(key)
        if value:
            parts.append(str(value))
    patterns = item.get("movement_patterns") or []
    if patterns:
        parts.extend(str(pattern) for pattern in patterns if str(pattern).strip())
    return " ".join(parts).lower()


def _has_any_hint(text: str, hints: set[str]) -> bool:
    return any(hint in text for hint in hints)


def _loaded_pattern(tags: set[str], text: str, equipment: set[str]) -> bool:
    if equipment & _LOADED_EQUIPMENT:
        return True
    if "mech_lower_jump" in tags or re.search(r"\b(?:jump|hop|bound)\b", text):
        return False
    if "compound" in tags:
        return True
    if _has_any_hint(text, _LOWER_BODY_HINTS | _PUSH_HINTS | _PULL_HINTS):
        return any(hint in text for hint in {"deadlift", "squat", "press", "row", "pull", "split squat", "step-up", "step up"})
    return False


def classify_strength_item(item: dict[str, Any]) -> dict[str, Any]:
    tags = _collect_tags(item)
    text = _collect_text(item)
    equipment = set(normalize_equipment_list(item.get("equipment") or []))
    is_rehab = _has_any_hint(text, _REHAB_HINTS) or "rehab" in tags
    is_isometric = "isometric" in tags or _has_any_hint(text, _ISOMETRIC_HINTS)
    loaded_pattern = _loaded_pattern(tags, text, equipment)
    lower_body_loaded = loaded_pattern and _has_any_hint(text, _LOWER_BODY_HINTS | {"quad_dominant", "posterior_chain", "hip_dominant"})
    upper_body_push_pull = loaded_pattern and _has_any_hint(text, _PUSH_HINTS | _PULL_HINTS | {"upper_body"})
    unilateral = "unilateral" in tags or _has_any_hint(text, _UNILATERAL_HINTS)
    power_pattern = bool(tags & _POWER_HINTS) or _has_any_hint(text, _POWER_HINTS)
    lower_body_jump = power_pattern and (
        bool(tags & _LOWER_BODY_JUMP_TAGS)
        or bool(re.search(r"\b(?:jump|hop|bound|sprint)\b", text))
    )
    lower_body_olympic = power_pattern and (
        "olympic" in tags
        or bool(
            re.search(
                r"\b(?:power clean|hang clean|clean pull|power snatch|hang snatch|high pull)\b",
                text,
            )
        )
    )
    lower_body_hip_ballistic = (
        power_pattern
        and not lower_body_jump
        and not lower_body_olympic
        and "mech_ballistic" in tags
        and (
            bool(tags & _LOWER_BODY_HIP_BALLISTIC_TAGS)
            or bool(re.search(r"\b(?:kettlebell|kb) swing\b", text))
        )
    )
    lower_body_ballistic = lower_body_jump or lower_body_olympic or lower_body_hip_ballistic
    lower_body_power = lower_body_ballistic
    lower_body_explosive_anchor = lower_body_ballistic
    rotational_power = power_pattern and bool(tags & _ROTATIONAL_POWER_TAGS)
    upper_body_ballistic = power_pattern and (
        bool(tags & _UPPER_BODY_BALLISTIC_TAGS)
        or bool(re.search(r"\b(?:punch|chest pass|push-up)\b", text))
    )
    support_only = bool(tags & _SUPPORT_HINTS) or _has_any_hint(text, _SUPPORT_HINTS)
    core_balance_signal = bool(tags & _CORE_BALANCE_SUPPORT_TAGS)
    force_isometric = is_isometric and (
        loaded_pattern
        or bool(tags & {"posterior_chain", "quad_dominant", "hip_dominant", "push", "pull"})
        or _has_any_hint(text, _FORCE_ISOMETRIC_HINTS)
    )

    if is_rehab:
        quality_class = "rehab_support"
    elif force_isometric:
        quality_class = "anchor_force_isometric"
    elif power_pattern and (
        not support_only
        or lower_body_power
        or rotational_power
        or upper_body_ballistic
    ):
        quality_class = "anchor_power"
    elif loaded_pattern and (lower_body_loaded or upper_body_push_pull or unilateral):
        quality_class = "anchor_loaded"
    elif is_isometric:
        quality_class = "support_isometric"
    else:
        quality_class = "support_accessory"

    # Authored governance is the final fulfilment authority. A loaded movement
    # may still be present for durability or recovery support, but explicit
    # support_only/meaningful_stress metadata prevents its name, tags, or bank
    # membership from turning it back into a strength anchor.
    governed_support = item.get("support_only") is True or item.get("meaningful_stress") is False
    if governed_support and quality_class in ANCHOR_CAPABLE_CLASSES:
        quality_class = "support_isometric" if is_isometric else "support_accessory"
    # The same authority works in the other direction. A heavy sled push, yoke
    # walk or sledgehammer can land in a support class only because a "core" or
    # "carry" token matched; its authored meaningful stress says the session pays
    # for it, so it is never low-cost core/balance support.
    governed_meaningful_stress = item.get("meaningful_stress") is True or item.get("support_only") is False

    core_balance_support = (
        quality_class in SUPPORT_ONLY_CLASSES
        and quality_class != "rehab_support"
        and core_balance_signal
        and not governed_meaningful_stress
    )

    # Region hints come from words such as "pull" and "press", so a sled pull or
    # a rack-pull isometric reads as upper-body pulling. When the authored body
    # category contradicts the hint, the loaded family follows the authored
    # region, and a core-category drill holds no limb strength family.
    authored_category = str(item.get("category") or "").strip().lower()
    if authored_category == "lower_body" and upper_body_push_pull and not lower_body_loaded:
        upper_body_push_pull, lower_body_loaded = False, True
    elif authored_category == "upper_body" and lower_body_loaded and not upper_body_push_pull:
        upper_body_push_pull, lower_body_loaded = True, False
    elif authored_category == "core":
        upper_body_push_pull = lower_body_loaded = False

    base_categories: set[str] = set()
    if lower_body_loaded:
        base_categories.add("lower_body_loaded")
    if upper_body_push_pull:
        base_categories.add("upper_body_push_pull")
    if unilateral:
        base_categories.add("unilateral")
    if lower_body_power:
        base_categories.add("lower_body_power")
        base_categories.add("lower_body_ballistic")
    if lower_body_explosive_anchor:
        base_categories.add("lower_body_explosive_anchor")
    if lower_body_jump:
        base_categories.add("lower_body_jump")
    if lower_body_olympic:
        base_categories.add("lower_body_olympic")
    if lower_body_hip_ballistic:
        base_categories.add("lower_body_hip_ballistic")
    if rotational_power:
        base_categories.add("rotational_power")
    if upper_body_ballistic:
        base_categories.add("upper_body_ballistic")

    return {
        "quality_class": quality_class,
        "anchor_capable": quality_class in ANCHOR_CAPABLE_CLASSES,
        "support_only": quality_class in SUPPORT_ONLY_CLASSES,
        "force_isometric": quality_class == "anchor_force_isometric",
        "loaded_pattern": loaded_pattern,
        "power_pattern": quality_class == "anchor_power",
        "rehab_support": quality_class == "rehab_support",
        "core_balance_support": core_balance_support,
        "base_categories": sorted(base_categories),
    }


# --- Adaptation relevance -----------------------------------------------------
#
# Three facts are kept apart:
#   * anatomical involvement - which body parts an exercise uses (``upper_body``,
#     ``core``, ``posterior_chain`` tags);
#   * adaptation relevance - which quality the exercise's training method can
#     develop, answered here;
#   * delivered adaptation - whether the scheduled, resolved dose actually does,
#     owned by ``goal_preservation``.
# Relevance is read from programming evidence the bank already authors: method,
# the classified quality and loaded pattern, authored body category and trunk
# function tags. A body-part tag never establishes an adaptation on its own, so
# a sled push tagged ``core`` is not trunk training and swimming tagged
# ``upper_body`` is not strength training.
STRENGTH_DEVELOPMENT_METHODS = frozenset({"strength", "contrast"})
POWER_DEVELOPMENT_METHODS = frozenset({"power", "plyometric", "contrast"})
_POWER_QUALITY_TAGS = frozenset(
    {"explosive", "rate_of_force", "plyometric", "ballistic", "elastic", "triple_extension", "horizontal_power"}
)
_SPEED_QUALITY_TAGS = frozenset({"speed", "reactive", "reaction", "acceleration", "max_velocity"})
_TRUNK_FUNCTION_TAGS = frozenset(
    {"anti_rotation", "anti_extension", "anti_lateral_flexion", "trunk_strength", "core_stability", "core_strength"}
)
_LIMB_CATEGORIES = frozenset({"upper_body", "lower_body"})


def strength_adaptation_relevance(item: dict[str, Any], *, prescription_type: str | None = None) -> frozenset[str]:
    """Adaptations this strength-bank item's training method can develop.

    ``strength``: a strength/contrast method on a loaded or force-isometric
    pattern. Sled drags and carries (``strength_conditioning``), heavy implements
    classified as support, and power-method sled sprints do not qualify.
    ``power``: a classified power pattern with a power method or explosive
    quality tags; ``speed`` additionally needs a speed-quality tag.
    ``trunk_strength``: trunk-targeted work - an authored core category or
    movement, or an anti-rotation/extension/lateral-flexion function.
    Raw relevance is capability. When the selector supplies its actual bank
    prescription type, a power-only/timed-conditioning dose cannot back strength.
    The resolved scheduled-day dose still decides final delivery.
    """
    profile = classify_strength_item(item)
    tags = _collect_tags(item)
    method = str(item.get("method") or "").strip().lower()
    quality = profile["quality_class"]
    loaded_categories = set(profile["base_categories"]) & {"lower_body_loaded", "upper_body_push_pull"}
    adaptations: set[str] = set()
    if method in STRENGTH_DEVELOPMENT_METHODS and (
        quality in {"anchor_loaded", "anchor_force_isometric"}
        or (quality == "anchor_power" and loaded_categories)
    ):
        adaptations.add("strength")
    if prescription_type is not None and prescription_type not in {
        "barbell", "general", "core", "contrast", "max_isometric", "isometric",
    }:
        adaptations.discard("strength")
    if quality == "anchor_power" and (method in POWER_DEVELOPMENT_METHODS or tags & _POWER_QUALITY_TAGS):
        adaptations.add("power")
        if tags & _SPEED_QUALITY_TAGS:
            adaptations.add("speed")
    authored_category = str(item.get("category") or "").strip().lower()
    # An anti-rotation tag on a single-arm press records the trunk's involvement
    # in a limb exercise. Outside an authored core category, trunk targeting
    # needs a trunk-function tag on an item that is not a limb exercise. The
    # ``movement`` field is not read: Stage 1 overwrites it with a detected
    # pattern, so an adductor hold tagged "core" would arrive as movement "core".
    if authored_category == "core" or (
        authored_category not in _LIMB_CATEGORIES and tags & _TRUNK_FUNCTION_TAGS
    ):
        adaptations.add("trunk_strength")
    return frozenset(adaptations)


def infer_strength_sessions(exercises: list[dict[str, Any]], num_sessions: int) -> list[dict[str, Any]]:
    session_count = max(1, int(num_sessions or 1))
    if not exercises:
        return [{"session_index": idx + 1, "items": []} for idx in range(session_count)]

    sessions = [{"session_index": idx + 1, "items": [], "positions": []} for idx in range(session_count)]
    for idx, exercise in enumerate(exercises):
        session_idx = idx if idx < session_count else idx % session_count
        sessions[session_idx]["items"].append(exercise)
        sessions[session_idx]["positions"].append(idx)

    for session in sessions:
        anchor_index = None
        for idx, exercise in enumerate(session["items"]):
            if classify_strength_item(exercise)["anchor_capable"]:
                anchor_index = idx
                break
        session["anchor_index"] = anchor_index
    return sessions


def score_band_margin(values: list[float], *, phase: str) -> float:
    cleaned = [float(value) for value in values if value is not None]
    if not cleaned:
        return ISOMETRIC_SESSION_SCORE_BAND.get(phase.upper(), 0.08)
    high = max(cleaned)
    low = min(cleaned)
    spread = max(high - low, 0.0)
    return max(ISOMETRIC_SESSION_SCORE_BAND.get(phase.upper(), 0.08), spread * 0.08)


def session_support_count_before_anchor(session_items: list[dict[str, Any]]) -> int:
    count = 0
    for exercise in session_items:
        profile = classify_strength_item(exercise)
        if profile["anchor_capable"]:
            break
        if profile["support_only"]:
            count += 1
    return count


def session_starts_with_support_only(session_items: list[dict[str, Any]]) -> bool:
    first_two = session_items[:2]
    if not first_two:
        return False
    return all(classify_strength_item(exercise)["support_only"] for exercise in first_two)

def count_support_only(exercises: list[dict[str, Any]]) -> int:
    return sum(1 for exercise in exercises if classify_strength_item(exercise)["support_only"])


def support_budget_cost(
    exercises: list[dict[str, Any]],
    *,
    core_balance_bonus: int = 0,
) -> int:
    """Return support-cap cost while preserving low-cost support work.

    Rehab/prehab never consumes the support cap. When an explicit core/balance
    priority is active, one dedicated core/balance support exercise is also
    free. Anchor-capable compound lifts do not qualify merely because they carry
    trunk-stability mechanism tags.
    """
    remaining_core_balance_bonus = max(0, int(core_balance_bonus or 0))
    cost = 0
    for exercise in exercises:
        profile = classify_strength_item(exercise)
        if profile["rehab_support"]:
            continue
        if not profile["support_only"]:
            continue
        if remaining_core_balance_bonus and profile.get("core_balance_support"):
            remaining_core_balance_bonus -= 1
            continue
        cost += 1
    return cost


def missing_base_categories(
    exercises: list[dict[str, Any]],
    *,
    require_lower_body_explosive_anchor: bool = False,
) -> list[str]:
    present: set[str] = set()
    for exercise in exercises:
        present.update(classify_strength_item(exercise)["base_categories"])
    ordered = ["lower_body_loaded", "upper_body_push_pull", "unilateral"]
    if require_lower_body_explosive_anchor:
        ordered.append("lower_body_explosive_anchor")
    return [category for category in ordered if category not in present]

def strength_quality_adjustment(item: dict[str, Any], *, phase: str) -> tuple[float, dict[str, Any]]:
    profile = classify_strength_item(item)
    quality_class = profile["quality_class"]
    weights = STRENGTH_QUALITY_WEIGHTS
    if quality_class in {"anchor_loaded", "anchor_power", "anchor_force_isometric"}:
        adjustment = weights[quality_class].get(phase.upper(), 0.0)
    elif quality_class == "rehab_support":
        adjustment = weights["rehab_support_penalty"].get(phase.upper(), 0.0)
    else:
        adjustment = weights["support_only_penalty"].get(phase.upper(), 0.0)
    return adjustment, profile
