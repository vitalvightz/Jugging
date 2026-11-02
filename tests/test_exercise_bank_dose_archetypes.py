"""Regression coverage for exercise-bank dosing archetypes.

The generic prescription templates (``barbell`` / ``ballistic`` / ``isometric``
/ ``core`` / ``general``) are correct for the large majority of the bank, but a
minority of entries have a fundamentally different dosing *unit* or *training
method*: a loaded carry is metres or seconds, a maximal overcoming isometric is
a few seconds of all-out intent, a contrast pair carries two doses, and a
loaded complete-cycle movement is dosed in cycles per side.

These tests pin the archetype routing, the resulting dose semantics, the
authority order (an explicit ``prescription`` always wins), and the fact that
the countdown overlay cannot rewrite a non-``NxM`` dose into ordinary reps.
"""

import json
import re

import pytest

from fightcamp.prescription_resolver import (
    _effective_counts,
    _format_effective_prescription,
    _parse_sets_reps,
)
from fightcamp.strength import (
    DATA_DIR,
    _classify_prescription_type,
    _prescription_templates,
)
from fightcamp.weekly_plan_render import _strength_line

PHASES = ("GPP", "SPP", "TAPER")

BANK = json.loads((DATA_DIR / "exercise_bank.json").read_text(encoding="utf-8"))
BY_NAME = {entry["name"]: entry for entry in BANK}


def _doses(name: str) -> dict[str, str]:
    ptype = _classify_prescription_type(BY_NAME[name])
    return {phase: _prescription_templates(phase)[ptype] for phase in PHASES}


def _under_countdown_cap(dose: str, role_kind: str, *, max_sets: int = 3) -> str:
    """Run one dose through the real countdown overlay."""
    cap = {"max_sets": max_sets, "max_reps": 3, "loaded_allowed": True}
    base_sets, base_reps = _parse_sets_reps(dose)
    sets, reps, loaded = _effective_counts(
        base_sets=base_sets, base_reps=base_reps, role_kind=role_kind,
        strength_cap=cap, base_prescription=dose,
    )
    return _format_effective_prescription(
        base_prescription=dose,
        sets=sets,
        reps=reps,
        rpe_cap="6-7",
        loaded=loaded,
        suppressed_loaded_lift=role_kind in {"anchor", "secondary", "hybrid"} and not loaded,
    )


ROLE_KINDS = ("anchor", "hybrid", "secondary", "power", "support")


# --------------------------------------------------------------------------- #
# Bank-wide invariant
# --------------------------------------------------------------------------- #
def test_every_bank_entry_resolves_to_a_real_non_empty_template():
    for phase in PHASES:
        templates = _prescription_templates(phase)
        for entry in BANK:
            ptype = _classify_prescription_type(entry)
            assert ptype in templates, f"{entry['name']} -> unknown type {ptype!r}"
            assert templates[ptype].strip(), f"{entry['name']} -> empty dose"


# --------------------------------------------------------------------------- #
# Turkish Get-Up: a loaded complete cycle, per side, with a hard ceiling.
# --------------------------------------------------------------------------- #
def test_turkish_get_up_is_a_quality_cycle_not_core_reps():
    assert _classify_prescription_type(BY_NAME["Turkish Get-Up"]) == "quality_cycle"


def test_turkish_get_up_dose_is_per_side_and_within_the_2x2_ceiling():
    doses = _doses("Turkish Get-Up")
    for phase, dose in doses.items():
        assert "per side" in dose, (phase, dose)
        for raw_sets, raw_reps in re.findall(
            r"(\d+(?:[–-]\d+)?) sets? of (\d+(?:[–-]\d+)?)", dose
        ):
            assert max(int(v) for v in re.findall(r"\d+", raw_sets)) <= 2, (phase, dose)
            assert max(int(v) for v in re.findall(r"\d+", raw_reps)) <= 2, (phase, dose)
    assert "hard ceiling" in doses["GPP"]
    assert doses["TAPER"].startswith("1 set of 1 rep per side")


@pytest.mark.parametrize("role_kind", ROLE_KINDS)
def test_countdown_overlay_cannot_strip_per_side_from_a_quality_cycle(role_kind):
    for phase in PHASES:
        dose = _prescription_templates(phase)["quality_cycle"]
        assert "per side" in _under_countdown_cap(dose, role_kind), (phase, role_kind)


def test_explicit_bank_prescription_overrides_the_archetype_dose():
    """The existing per-exercise override reaches the weekly plan renderer."""
    exercise = dict(BY_NAME["Turkish Get-Up"], prescription="1 x 1 per side (coach override)")
    assert _strength_line(exercise, "GPP", forbidden=set()) == (
        "- Turkish Get-Up — 1 x 1 per side (coach override)"
    )


# --------------------------------------------------------------------------- #
# Carries: distance or time, never ordinary reps.
# --------------------------------------------------------------------------- #
CARRY_NAMES = [
    "Farmers Walk (Fat Grip)",
    "Plate Pinch Carry",
    "Farmer Carry (Heavy)",
    "Trap Bar Carry (Frame Neutral)",
    "Yoke Walk",
    "Waiter Walk",
    "Sled Push (Heavy)",
    "Sled Pull Backward",
]


@pytest.mark.parametrize("name", CARRY_NAMES)
def test_carries_route_to_the_carry_archetype(name):
    assert _classify_prescription_type(BY_NAME[name]) == "carry", name


@pytest.mark.parametrize("name", CARRY_NAMES)
def test_carries_are_dosed_in_distance_or_time_not_reps(name):
    for phase, dose in _doses(name).items():
        lowered = dose.lower()
        assert re.search(r"\d\s*m\b", lowered), (phase, dose)
        assert "rep" not in lowered, (phase, dose)
        assert "1rm" not in lowered, (phase, dose)


@pytest.mark.parametrize("role_kind", ROLE_KINDS)
def test_countdown_overlay_never_reads_carry_metres_as_reps(role_kind):
    """"3 x 20 m" would parse as 20 reps, so the carry dose avoids that form."""
    for phase in PHASES:
        dose = _prescription_templates(phase)["carry"]
        assert _parse_sets_reps(dose) == (None, None), (phase, dose)
        assert re.search(r"\d\s*m\b", _under_countdown_cap(dose, role_kind)), (phase, role_kind)


def test_rehab_band_walks_are_not_swept_into_the_carry_archetype():
    """"walk" alone is not a carry signal: these keep ordinary reps."""
    for name in (
        "Banded Lateral Walk",
        "Lateral Resisted Band Walk",
        "Tandem Gait Walk (Heel-to-Toe)",
        "Banded Pallof (Walking)",
    ):
        assert _classify_prescription_type(BY_NAME[name]) != "carry", name


# --------------------------------------------------------------------------- #
# Olympic / loaded power reuses the existing ballistic template.
# --------------------------------------------------------------------------- #
POWER_NAMES = [
    "Hang Power Clean",
    "High Pull",
    "Push Press",
    "Speed Box Squat",
    "Explosive Incline Press",
]


@pytest.mark.parametrize("name", POWER_NAMES)
def test_power_lifts_do_not_inherit_the_barbell_strength_template(name):
    assert _classify_prescription_type(BY_NAME[name]) == "ballistic", name


@pytest.mark.parametrize("name", POWER_NAMES)
def test_power_lift_doses_are_velocity_governed(name):
    for phase, dose in _doses(name).items():
        lowered = dose.lower()
        assert "slow eccentric" not in lowered, (phase, dose)
        assert "tempo" not in lowered, (phase, dose)
        assert "1rm" not in lowered, (phase, dose)
        assert "speed" in lowered or "intent" in lowered, (phase, dose)


def test_power_routing_keys_off_method_not_ballistic_tags():
    """Heavy barbell lifts that also carry ``mech_ballistic`` stay on ``barbell``."""
    for name in ("Cluster Set Trap Bar Deadlift", "Back Squat", "Trap Bar Deadlift", "Front Squat"):
        entry = BY_NAME[name]
        assert _classify_prescription_type(entry) == "barbell", name
    assert "mech_ballistic" in BY_NAME["Cluster Set Trap Bar Deadlift"]["tags"]


def test_loaded_jump_is_velocity_governed_not_percent_1rm_strength_work():
    """A bar-loaded jump stays ballistic: light/moderate load, velocity governs."""
    assert _classify_prescription_type(BY_NAME["Trap Bar Jump"]) == "ballistic"
    for phase, dose in _doses("Trap Bar Jump").items():
        assert "1rm" not in dose.lower(), (phase, dose)
        assert "slow eccentric" not in dose.lower(), (phase, dose)


# --------------------------------------------------------------------------- #
# Maximal overcoming isometrics: seconds of max intent, never reps or long holds.
# --------------------------------------------------------------------------- #
MAX_ISO_NAMES = [
    "Squat Isometric (110% 1RM @ 90°)",
    "Bench Isometric (130% 1RM @ mid-range)",
    "Deadlift Isometric (120% 1RM @ knee height)",
    "Atlas Stone Load Isometric (120% max stone @ lap)",
]


@pytest.mark.parametrize("name", MAX_ISO_NAMES)
def test_over_100_percent_isometrics_are_not_barbell_rep_work(name):
    assert _classify_prescription_type(BY_NAME[name]) == "max_isometric", name


def test_max_isometric_is_a_short_maximal_effort_not_a_long_submaximal_hold():
    max_iso = _prescription_templates("SPP")["max_isometric"].lower()
    support_iso = _prescription_templates("SPP")["isometric"].lower()
    assert max_iso != support_iso
    assert "slow eccentric" not in max_iso
    assert "never dosed as reps" in max_iso
    # Effort duration is single-digit seconds, not a 10-20s endurance hold.
    max_seconds = max(int(v) for v in re.findall(r"(\d+)s", max_iso))
    assert max_seconds <= 6, max_iso


def test_no_isometric_tagged_entry_is_routed_to_a_rep_based_template():
    rep_based = {"barbell", "ballistic", "core", "general", "contrast"}
    offenders = [
        entry["name"]
        for entry in BANK
        if "isometric" in {str(tag).strip().lower() for tag in (entry.get("tags") or [])}
        and _classify_prescription_type(entry) in rep_based
    ]
    assert not offenders, offenders


def test_static_holds_receive_hold_doses_without_reclassifying_dynamic_plank_work():
    for name in ("Bird Dog Hold", "Hollow-Body Hold", "Staggered Stance Hold"):
        assert _classify_prescription_type(BY_NAME[name]) == "support_isometric", name
        for dose in _doses(name).values():
            assert "hold" in dose and "tempo" not in dose
    assert _classify_prescription_type({"name": "Side Plank Row", "tags": ["core"]}) == "core"


# --------------------------------------------------------------------------- #
# Contrast pairs keep both components, in every phase and under a countdown cap.
# --------------------------------------------------------------------------- #
CONTRAST_NAMES = [
    "Back Squat → Box Jump",
    "Heavy RDL → Broad Jump",
    "DB Bench Press → Plyo Push-Up",
    "Landmine Press → Med Ball Chest Pass",
    "Zercher Squat → Sprawl Jump",
]


def test_contrast_class_comes_from_bank_metadata_not_a_name_list():
    from_bank = {e["name"] for e in BANK if str(e.get("method") or "").lower() == "contrast"}
    assert from_bank == set(CONTRAST_NAMES)


@pytest.mark.parametrize("name", CONTRAST_NAMES)
def test_contrast_pairs_route_to_the_contrast_archetype(name):
    assert _classify_prescription_type(BY_NAME[name]) == "contrast", name


@pytest.mark.parametrize("name", CONTRAST_NAMES)
def test_contrast_dose_states_both_components(name):
    for phase, dose in _doses(name).items():
        lowered = dose.lower()
        assert "1rm" in lowered, (phase, dose)          # loaded half, explicit intensity
        assert "explosive movement" in lowered, (phase, dose)  # explosive half
        assert "→" in dose, (phase, dose)               # the pairing itself
        assert "round" in lowered, (phase, dose)


@pytest.mark.parametrize("role_kind", ROLE_KINDS)
def test_countdown_overlay_cannot_delete_the_explosive_half(role_kind):
    for phase in PHASES:
        dose = _prescription_templates(phase)["contrast"]
        assert _parse_sets_reps(dose) == (None, None), (phase, dose)
        out = _under_countdown_cap(dose, role_kind)
        assert "heavy movement" in out and "1RM" in out, (phase, role_kind, out)
        assert "explosive movement" in out and "→" in out, (phase, role_kind, out)


def test_contrast_pair_never_collapses_to_a_generic_single_exercise_dose():
    generic = {
        _prescription_templates(phase)[key]
        for phase in PHASES
        for key in ("barbell", "ballistic", "core", "general")
    }
    for name in CONTRAST_NAMES:
        for dose in _doses(name).values():
            assert dose not in generic, (name, dose)


# --------------------------------------------------------------------------- #
# A no-loaded-lifting band still suppresses loaded work (safety lever intact).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", ["contrast", "carry", "quality_cycle"])
def test_no_loaded_band_still_suppresses_loaded_work(key):
    cap = {"max_sets": 3, "max_reps": 3, "loaded_allowed": False}
    dose = _prescription_templates("SPP")[key]
    base_sets, base_reps = _parse_sets_reps(dose)
    sets, reps, loaded = _effective_counts(
        base_sets=base_sets, base_reps=base_reps, role_kind="anchor",
        strength_cap=cap, base_prescription=dose,
    )
    out = _format_effective_prescription(
        base_prescription=dose, sets=sets, reps=reps, rpe_cap="6-7",
        loaded=loaded, suppressed_loaded_lift=True,
    )
    assert out.startswith("No loaded lifting"), out


@pytest.mark.parametrize(
    "dose",
    [
        "5 sets x 8 reps @ RPE 8",   # longhand the NxM regex cannot read
        "4 x 3 @ RPE 7",
        "3 x 6",
    ],
)
def test_rep_doses_the_regex_cannot_read_are_still_capped(dose):
    """Parse failure is NOT a licence to skip the cap.

    Only a dose counted in something other than reps (metres, seconds, per
    side, a contrast pair) is preserved verbatim. An ordinary rep dose written
    longhand must still take the countdown ceiling.
    """
    assert _under_countdown_cap(dose, "anchor") == "3 x 3 @ RPE 6-7 max"


@pytest.mark.parametrize("key", ["isometric", "max_isometric", "carry", "quality_cycle", "contrast"])
def test_non_rep_doses_are_still_reduced_by_the_countdown_cap(key):
    """Keeping the unit must not make a dose immune to volume reduction.

    The band's set ceiling applies to the dose's own leading volume count
    (holds / efforts / carries / rounds / sets), so the countdown keeps a real
    lever instead of collapsing to allow-or-forbid.
    """
    base = _prescription_templates("SPP")[key]
    tightened = [_under_countdown_cap(base, "anchor", max_sets=n) for n in (3, 2, 1)]
    leading = [int(re.match(r"\s*(\d+)", dose).group(1)) for dose in tightened]
    assert leading == sorted(leading, reverse=True) or len(set(leading)) > 1, tightened
    assert leading[-1] == 1, tightened
    # The unit itself survives every reduction: everything after the leading
    # count is byte-identical apart from singularising the count noun.
    for dose in tightened:
        assert dose.split(None, 1)[1].replace("set ", "sets ", 1).replace(
            "carry ", "carries ", 1
        ).replace("hold ", "holds ", 1).replace("effort ", "efforts ", 1).replace(
            "round:", "rounds:", 1
        ) == base.split(None, 1)[1], (base, dose)


@pytest.mark.parametrize("key", ["isometric", "max_isometric", "carry", "quality_cycle", "contrast"])
def test_countdown_cap_never_raises_a_non_rep_dose(key):
    base = _prescription_templates("SPP")[key]
    assert _under_countdown_cap(base, "anchor", max_sets=99) == base


def test_late_camp_isometric_remains_reducible():
    """Trap-Bar Pin Pull Isometric is allowed out to D-7 and must still taper."""
    assert _classify_prescription_type(BY_NAME["Trap-Bar Pin Pull Isometric"]) == "isometric"
    base = _prescription_templates("TAPER")["isometric"]
    assert _under_countdown_cap(base, "anchor", max_sets=1).startswith("1 hold ")


def test_barbell_thruster_is_not_prescribed_as_a_heavy_strength_lift():
    """A ballistic squat-to-press, corrected in classification, not in the bank."""
    entry = BY_NAME["Barbell Thruster"]
    assert entry["method"] == "strength", "bank selection semantics must be untouched"
    assert _classify_prescription_type(entry) == "ballistic"
    for phase, dose in _doses("Barbell Thruster").items():
        assert "slow eccentric" not in dose.lower(), (phase, dose)
        assert "1RM" not in dose, (phase, dose)


def test_push_press_intent_is_explosive_across_implements():
    assert _classify_prescription_type(BY_NAME["Landmine Push Press"]) == "ballistic"
    assert _classify_prescription_type(BY_NAME["Band-Resisted Push Press"]) == "ballistic"


def test_parseable_doses_are_still_capped_exactly_as_before():
    """The resolver fix must not change behaviour for ordinary NxM doses."""
    assert _under_countdown_cap(_prescription_templates("SPP")["barbell"], "anchor") == "3 x 3 @ RPE 6-7 max"
    assert _under_countdown_cap(_prescription_templates("SPP")["ballistic"], "anchor") == "3 x 2 @ RPE 6-7 max"


# --------------------------------------------------------------------------- #
# Taper reduces volume without abandoning intensity.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", ["contrast", "carry", "quality_cycle"])
def test_taper_is_lower_volume_than_gpp(key):
    gpp = _prescription_templates("GPP")[key]
    taper = _prescription_templates("TAPER")[key]
    assert gpp != taper
    gpp_top = max(int(v) for v in re.findall(r"\d+", gpp.split("@")[0]))
    taper_top = max(int(v) for v in re.findall(r"\d+", taper.split("@")[0]))
    assert taper_top <= gpp_top, (gpp, taper)


# --------------------------------------------------------------------------- #
# Conditioning drills: work time, never sets x reps.
# --------------------------------------------------------------------------- #
TIMED_ARCHETYPES = ("timed_interval", "combat_round", "tabata")


def test_every_bank_conditioning_drill_is_dosed_in_time_not_reps():
    """"3 sets x 4 reps" of jump rope was the production failure."""
    for entry in BANK:
        if entry.get("method") != "conditioning":
            continue
        ptype = _classify_prescription_type(entry)
        assert ptype in TIMED_ARCHETYPES + ("carry",), (entry["name"], ptype)
        for phase in PHASES:
            dose = _prescription_templates(phase)[ptype]
            assert _parse_sets_reps(dose) == (None, None), (entry["name"], phase, dose)
            assert "reps" not in dose, (entry["name"], phase, dose)


def test_weighted_vest_jump_rope_is_named_and_dosed_as_rope_work():
    assert "Rope Jumping (Weighted Vest)" not in BY_NAME
    entry = BY_NAME["Jump Rope (Weighted Vest)"]
    assert _classify_prescription_type(entry) == "timed_interval"
    assert _doses("Jump Rope (Weighted Vest)")["GPP"].startswith("3–5 rounds of 60–90s")


def test_named_round_structures_are_kept():
    assert _classify_prescription_type(BY_NAME["3-Minute Heavy Bag Interval"]) == "combat_round"
    assert _classify_prescription_type(BY_NAME["3-Minute Round (Striking + Takedowns)"]) == "combat_round"
    assert _classify_prescription_type(BY_NAME["Tabata Sprints (Treadmill/Row)"]) == "tabata"
    assert _classify_prescription_type(BY_NAME["Burpee-to-Punch (Tabata)"]) == "tabata"
    for dose in _doses("3-Minute Heavy Bag Interval").values():
        assert "3 min" in dose, dose
    for dose in _doses("Burpee-to-Punch (Tabata)").values():
        assert "20s" in dose and "10s rest" in dose, dose


def test_conditioning_carry_stays_a_carry():
    assert _classify_prescription_type(BY_NAME["Death by Carry (Sandbag/Sled)"]) == "carry"


@pytest.mark.parametrize("key", TIMED_ARCHETYPES)
@pytest.mark.parametrize("role_kind", ROLE_KINDS)
def test_countdown_cap_reduces_rounds_and_keeps_the_seconds(key, role_kind):
    for phase in PHASES:
        base = _prescription_templates(phase)[key]
        capped = _under_countdown_cap(base, role_kind, max_sets=1)
        assert capped.startswith("1 round of "), (phase, role_kind, capped)
        assert capped.split(" of ", 1)[1] == base.split(" of ", 1)[1], (base, capped)
        assert _under_countdown_cap(base, "anchor", max_sets=99) == base
