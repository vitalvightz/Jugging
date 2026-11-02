from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from shared.contracts import load_shared_contract, require_object, require_string, require_string_list

from .config import CONDITIONING_PER_DAY, STRENGTH_PER_DAY
from .weight_cut import WEIGHT_CUT_INPUTS_KNOWN

EQUIP_ALIASES = {
    "punching bag": "heavy_bag",
    "punching_bag": "heavy_bag",
    "heavy bag": "heavy_bag",
    "pull up bar": "pullup_bar",
    "pull-up bar": "pullup_bar",
    "pullup bar": "pullup_bar",
    "pull_up_bar": "pullup_bar",
    "med balls": "medicine_ball",
    "med ball": "medicine_ball",
    "medicine balls": "medicine_ball",
    "medicine ball": "medicine_ball",
    "band": "bands",
    "banded": "bands",
    "mini_band": "bands",
    "mini band": "bands",
    "mini bands": "bands",
    "resistance_band": "bands",
    "resistance_bands": "bands",
    "resistance bands": "bands",
    "resistance band": "bands",
    "plates": "plate",
    "weight plate": "plate",
    "weight_plate": "plate",
    "kettlebells": "kettlebell",
    "plyo box": "box",
    "plyo_box": "box",
    "plyometric box": "box",
    "plyometric_box": "box",
    "jump box": "box",
    "jump_box": "box",
    "weighted vest": "weight_vest",
    "weighted_vest": "weight_vest",
    "weight belt": "weight_belt",
    "weighted belt": "weight_belt",
    "weighted_belt": "weight_belt",
    "dip belt": "weight_belt",
    "dip_belt": "weight_belt",
    "stability ball": "swiss_ball",
    "stability_ball": "swiss_ball",
    "cable machine": "cable",
    "cable_machine": "cable",
    "sandbags": "sandbag",
    "battle rope": "battle_ropes",
    "battle ropes": "battle_ropes",
    "air bike": "assault_bike",
    "air_bike": "assault_bike",
    "echo bike": "assault_bike",
    "echo_bike": "assault_bike",
    "assault bike": "assault_bike",
    "bike erg": "assault_bike",
    "bike_erg": "assault_bike",
    "ski erg": "ski_erg",
    "ski_erg": "ski_erg",
    "skierg": "ski_erg",
    "rowing machine": "rower",
    "rowing_machine": "rower",
    "concept2 rower": "rower",
    "skipping rope": "jump_rope",
    "skip rope": "jump_rope",
    "speed rope": "jump_rope",
    "hurdle": "hurdles",
    "mini hurdle": "hurdles",
    "mini hurdles": "hurdles",
    "agility hurdles": "hurdles",
}

# --- Canonical combat pad/mitt capability -------------------------------
# Thai pads, focus mitts and partner mitts are separate names for the same
# athlete-facing capability: handheld striking pads held by a partner. The
# banks use all of these spellings, while intake only ever stored one, so
# eligibility (``required_equipment <= athlete_equipment``) used to fail for
# drills that happened to spell it differently. They all collapse to
# ``pads``. Deliberately NOT aliased: heavy/double-end bags, wall pads,
# kick shields, body protectors and rehab foam pads - none of those are
# handheld striking pads, and "pad" on its own is a squeeze pad in the
# strength bank, not a striking pad.
#
# The spellings live in shared/equipment-aliases.json, which the intake form
# (web/lib/intake-options.ts) reads too, so a profile saved with any of them
# keeps its pads when the athlete re-saves it.
_PAD_CONTRACT = require_object(load_shared_contract("equipment-aliases.json"), "pads", source="Equipment aliases")

# Canonical token for handheld combat striking pads/mitts. Exposed so callers
# (and tests) do not have to re-spell it.
PADS = require_string(_PAD_CONTRACT, "canonical", source="Equipment aliases pads")
EQUIP_ALIASES.update(
    {alias: PADS for alias in (*require_string_list(_PAD_CONTRACT, "aliases", source="Equipment aliases pads"), PADS)}
)


# Product decision: every athlete can provide a floor line or tape, a mat or
# training surface, something to drape a towel over and a squeeze pad, so these
# never gate eligibility (rows: Tandem Gait Walk, Side Hop-to-Stabilize,
# Tape-Assisted Plyo Taps, Towel Hang, Adductor Squeeze Isometric and every
# mat/mats row). Dropped here so generation, the editor and the Stage 2
# validator agree.
NOT_REQUIRED_EQUIPMENT = frozenset({"mat", "mats", "line", "tape", "line_tape", "bar", "pad"})


def _split_items(value):
    if isinstance(value, str):
        return re.split(r"\s*(?:,|/|\+| and )\s*", value)
    return [value]


def normalize_equipment_list(raw):
    """Return a list of canonical equipment tokens."""
    parts: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            parts.extend(_split_items(item))
    elif isinstance(raw, str):
        parts.extend(_split_items(raw))
    else:
        return []

    normalized: list[str] = []
    for part in parts:
        key = part.lower().strip()
        if key in {"med balls / bands", "med balls/bands"}:
            for token in ("medicine_ball", "bands"):
                if token not in normalized:
                    normalized.append(token)
            continue
        key = EQUIP_ALIASES.get(key, key).replace(" ", "_")
        # Re-check after whitespace collapsing so "Focus Mitts" and
        # "focus_mitts" land on the same canonical token.
        key = EQUIP_ALIASES.get(key, key)
        if key in NOT_REQUIRED_EQUIPMENT:
            continue
        if key and key not in normalized:
            normalized.append(key)
    return normalized


def normalize_athlete_equipment_list(raw):
    """Return athlete equipment access with implicit bodyweight availability."""
    normalized = normalize_equipment_list(raw)
    # Intake only offers "dumbbells"; owning them means owning one, so the
    # single-dumbbell rows (supported rehab curls/wrist work) are available.
    if "dumbbells" in normalized and "dumbbell" not in normalized:
        normalized.append("dumbbell")
    if "bodyweight" not in normalized:
        normalized.append("bodyweight")
    return normalized


# ✅ Correct constant definition (not a function)
known_equipment = [
    "barbell", "dumbbell", "dumbbells", "kettlebell", "sled", "medicine_ball",
    "trap_bar", "bands", "cable", "box", "weight_vest", "landmine",
    "towel", "partner", "bench", "trx", "pullup_bar", "plate",
    "swiss_ball", "heavy_bag", "pads", "neck_harness", "log",
    "tire", "atlas_stone", "water_jug", "bulgarian_bag", "sandbag",
    "treadmill", "rower", "agility_ladder", "battle_ropes", "sledgehammer",
    "climbing_rope", "bosu_ball", "foam_roller", "assault_bike",
    "stationary_bike", "step_mill", "recumbent_bike", "arm_ergometer",
    "elliptical", "pool", "bodyweight", "battle_rope", "kettlebells",
    "weight_belt", "jump_rope", "hurdles"
]


@dataclass(frozen=True)
class TrainingContext:
    fatigue: str
    training_frequency: int
    days_available: int
    training_days: list[str]
    injuries: list[str]
    style_technical: list[str]
    style_tactical: list[str]
    weaknesses: list[str]
    equipment: list[str]
    weight_cut_risk: bool
    weight_cut_pct: float
    fight_format: str
    status: str
    key_goals: list[str]
    training_preference: str
    mental_block: list[str] | str
    age: int | None
    weight: float | None
    prev_exercises: list[str]
    recent_exercises: list[str]
    phase_weeks: dict
    days_until_fight: int | None
    primary_goal: str = field(default="", kw_only=True)
    primary_weak_area: str = field(default="", kw_only=True)
    # Existing intake/profile stance identity. Optional for backward-compatible
    # callers; technical footwork falls back to bilateral/neutral cueing when it
    # is unavailable.
    stance: str = field(default="", kw_only=True)
    # Whether current/target weight were both collected. ``weight_cut_pct`` is
    # 0.0 when either is missing, so this is the only way downstream consumers
    # can tell "no cut" apart from "no cut data".
    weight_cut_status: str = WEIGHT_CUT_INPUTS_KNOWN
    # Under-18 safeguard, derived server-side from the profile's date of birth.
    # ``weight_cut_risk`` is already forced false for a minor upstream; this
    # carries the *reason* through to the flags so athlete-facing blocks can say
    # why cut guidance is absent instead of silently omitting it.
    is_minor: bool = False
    # Plan from the generation day itself (see ``PlanInput.include_generation_day``).
    include_generation_day: bool = False
    sparring_readiness: dict = field(default_factory=dict)
    training_split: dict[str, Any] = field(default_factory=dict)
    training_workload_context: dict[str, Any] = field(default_factory=dict)
    hard_sparring_days: list[str] = field(default_factory=list)
    support_work_days: list[str] = field(default_factory=list)
    technical_skill_days: list[str] = field(default_factory=list)  # legacy fallback
    athlete_timezone: str = ""
    next_fight_date: str = ""
    injuries_raw_text: str = ""
    # The athlete's verbatim mental/mindset note from Stage 1, preserved
    # alongside the classified ``mental_block`` buckets so Stage 2 can
    # personalise the mindset_anchor from the athlete's own words (mirrors
    # ``injuries_raw_text``).
    mental_block_raw: str = ""
    parsed_injuries: list[dict[str, Any]] = field(default_factory=list)
    guided_injury: dict[str, Any] | None = None
    guided_injuries: list[dict[str, Any]] = field(default_factory=list)
    injury_restrictions: list[dict[str, Any]] = field(default_factory=list)
    triage_summary: dict[str, Any] = field(default_factory=dict)
    # Display-only: parsed injuries the athlete marked as not limiting and that
    # were removed from every planning path above. Read only by
    # ``fightcamp.injury_awareness`` to keep the injury note and re-injury stop
    # rule on the plan; never consulted for selection, loading or scoring.
    non_limiting_injuries: list[dict[str, Any]] = field(default_factory=list)

    def to_flags(self) -> dict:
        return asdict(self)

def candidate_role_capacity(training_frequency: int) -> int:
    """Role-slot capacity for a requested frequency — NOT a physical-session count.

    The historical ``+1`` in ``allocate_sessions`` is a *candidate depth* rule,
    not a calendar rule. Exercise selection sizes its pools from these counts
    (``calculate_exercise_numbers``, ``strength.num_strength_sessions``,
    ``conditioning.num_conditioning_sessions``, ``late_fight_dosage_policy``),
    and the extra slot keeps a frequency-N athlete from bottoming out on
    candidate variety once governance suppresses a role. Removing it would thin
    every downstream bank, so it stays here and the calendar-facing count is
    capped separately (``stage2_planning_brief._cap_session_counts_to_frequency``).
    """
    return max(1, min(int(training_frequency) + 1, 6))


def physical_session_target(training_frequency: int, viable_training_days: int) -> int:
    """The number of physical sessions a week should hold.

    ``training_frequency`` is the athlete's requested count; ``viable_training_days``
    is how many declared training opportunities the calendar actually still has
    (fight day is not one). Safety may reduce the result further, but only with a
    recorded reason — see ``fightcamp.physical_session_frequency``.
    """
    try:
        requested = int(training_frequency)
    except (TypeError, ValueError):
        return max(0, int(viable_training_days))
    return max(0, min(requested, int(viable_training_days)))


def allocate_sessions(training_frequency: int, phase: str = "GPP") -> dict:
    """Return weekly *candidate role* counts for a frequency and phase.

    These are role opportunities, not the athlete-facing physical-session
    frequency: the table is indexed by ``candidate_role_capacity`` (frequency + 1,
    capped at 6) on purpose. Callers that build the visible weekly calendar must
    cap the total back to the requested frequency.
    """
    freq = candidate_role_capacity(training_frequency)
    phase = phase.upper()

    plan = {
        1: {
            "GPP": {"strength": 1, "conditioning": 0, "recovery": 0},
            "SPP": {"strength": 0, "conditioning": 1, "recovery": 0},
            "TAPER": {"strength": 0, "conditioning": 1, "recovery": 0},
        },
        2: {
            "GPP": {"strength": 1, "conditioning": 1, "recovery": 0},
            "SPP": {"strength": 1, "conditioning": 1, "recovery": 0},
            "TAPER": {"strength": 0, "conditioning": 1, "recovery": 1},
        },
        3: {
            "GPP": {"strength": 1, "conditioning": 1, "recovery": 1},
            "SPP": {"strength": 1, "conditioning": 2, "recovery": 0},
            "TAPER": {"strength": 1, "conditioning": 1, "recovery": 1},
        },
        4: {
            "GPP": {"strength": 2, "conditioning": 1, "recovery": 1},
            "SPP": {"strength": 1, "conditioning": 2, "recovery": 1},
            "TAPER": {"strength": 1, "conditioning": 2, "recovery": 1},
        },
        5: {
            "GPP": {"strength": 2, "conditioning": 2, "recovery": 1},
            "SPP": {"strength": 2, "conditioning": 2, "recovery": 1},
            "TAPER": {"strength": 1, "conditioning": 3, "recovery": 1},
        },
        6: {
            "GPP": {"strength": 2, "conditioning": 3, "recovery": 1},
            "SPP": {"strength": 2, "conditioning": 3, "recovery": 1},
            "TAPER": {"strength": 1, "conditioning": 4, "recovery": 1},
        },
    }

    return plan.get(freq, plan[6]).get(phase, {"strength": 1, "conditioning": 1, "recovery": 1})


def calculate_exercise_numbers(training_frequency: int, phase: str) -> dict:
    """Return recommended exercise counts for each block type.

    The result multiplies allocated session counts from ``allocate_sessions`` by
    phase-specific exercise targets. Recovery days are implied by sessions not
    scheduled for strength or conditioning.
    """

    sessions = allocate_sessions(training_frequency, phase)
    phase = phase.upper()

    return {
        "strength": STRENGTH_PER_DAY.get(phase, 0) * sessions.get("strength", 0),
        "conditioning": CONDITIONING_PER_DAY.get(phase, 0) * sessions.get(
            "conditioning", 0
        ),
    }
