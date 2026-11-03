"""Technical footwork ranking: competitive maturity and cross-phase variety.

Both are ranking nudges only. Eligibility (phase, taper complexity, reactive
level, cue source, injury) and drill dose are unchanged, and an athlete with
unknown maturity and no earlier-phase footwork ranks exactly as before.
"""
from __future__ import annotations

import pytest

from fightcamp import conditioning
from fightcamp.conditioning import (
    _technical_footwork_selection_reasons,
    select_technical_footwork_candidates,
)


def _flags(**over) -> dict:
    flags = {
        "phase": "SPP",
        "fatigue": "low",
        "sport": "boxing",
        "fight_format": "boxing",
        "style_tactical": ["distance_striker"],
        "style_technical": ["boxing"],
        "equipment": ["partner"],
        "key_goals": ["footwork"],
        "weaknesses": [],
    }
    flags.update(over)
    return flags


def _ranked(**over) -> list[str]:
    return [d["name"] for d in select_technical_footwork_candidates(_flags(**over), set(), [])]


def test_unknown_maturity_and_no_history_keeps_existing_order():
    assert _ranked() == _ranked(competitive_maturity="unknown_competitive_maturity")
    assert _ranked() == _ranked(competitive_maturity="early_pro", prior_technical_footwork=[])


@pytest.mark.parametrize("maturity", ["experienced_amateur", "developing_pro", "established_pro"])
def test_experienced_athlete_ranks_low_complexity_drill_lower(maturity):
    baseline = _ranked(phase="GPP")
    experienced = _ranked(phase="GPP", competitive_maturity=maturity)
    drill = "Stance Reset Line Drill"
    assert experienced.index(drill) > baseline.index(drill)
    # Still eligible: it is a ranking nudge, not an exclusion.
    assert set(experienced) == set(baseline)


def test_experienced_pressure_fighter_keeps_high_complexity_drill_on_top():
    ranked = _ranked(style_tactical=["pressure_fighter"], competitive_maturity="established_pro")
    assert ranked[0] == "Pressure Step-Cut Reset"


def test_novice_pressure_fighter_prefers_lower_complexity_ring_cut():
    baseline = _ranked(style_tactical=["pressure_fighter"])
    novice = _ranked(style_tactical=["pressure_fighter"], competitive_maturity="novice_amateur")
    assert baseline[0] == "Pressure Step-Cut Reset"
    assert novice[0] == "Ring Cut-Off Walkdown"


def test_maturity_never_overrides_tactical_relevance():
    # A counter striker's relevant moderate drills must stay above the
    # pressure-oriented high-complexity drill even for an established pro.
    ranked = _ranked(style_tactical=["counter_striker"], competitive_maturity="established_pro")
    assert ranked.index("Step-Back Pivot Reset") < ranked.index("Pressure Step-Cut Reset")


def test_taper_still_excludes_high_complexity_for_experienced_athlete():
    ranked = _ranked(
        phase="TAPER", style_tactical=["pressure_fighter"], competitive_maturity="established_pro"
    )
    assert "Pressure Step-Cut Reset" not in ranked


def test_drill_used_in_earlier_phase_is_deprioritised_in_spp():
    first = _ranked(style_tactical=["counter_striker"])[0]
    later = _ranked(style_tactical=["counter_striker"], prior_technical_footwork=[first])
    assert later[0] != first
    assert first in later  # still a fallback candidate


def test_taper_prefers_drill_already_used_in_camp():
    baseline = _ranked(phase="TAPER", style_tactical=["counter_striker"])
    familiar = baseline[1]
    ranked = _ranked(
        phase="TAPER", style_tactical=["counter_striker"], prior_technical_footwork=[familiar]
    )
    assert ranked[0] == familiar


def test_selection_reasons_report_ranking_adjustments():
    drill = conditioning.get_technical_footwork_bank()
    by_name = {d["name"]: d for d in drill}
    reasons = _technical_footwork_selection_reasons(
        _flags(
            phase="GPP",
            competitive_maturity="established_pro",
            prior_technical_footwork=["Stance Reset Line Drill"],
        ),
        by_name["Stance Reset Line Drill"],
    )
    assert "technical_footwork_complexity_fit:experienced:low" in reasons["reason_codes"]
    assert "technical_footwork_repeat_deprioritised" in reasons["reason_codes"]


ADVANCED_BOXING_DRILLS = {
    "Double Pivot Chain",
    "Shift-Step Angle Creation",
    "Foot Feint and Reposition",
}


def _camp(style: str, maturity: str = "") -> list[str]:
    prior: list[str] = []
    for phase in ("GPP", "SPP", "TAPER"):
        prior.append(
            _ranked(
                phase=phase,
                style_tactical=[style],
                competitive_maturity=maturity,
                prior_technical_footwork=list(prior),
            )[0]
        )
    return prior


def test_advanced_boxing_drills_are_in_the_bank_with_the_safety_contract():
    bank = {d["name"]: d for d in conditioning.get_technical_footwork_bank()}
    assert ADVANCED_BOXING_DRILLS <= set(bank)
    for name in ADVANCED_BOXING_DRILLS:
        drill = bank[name]
        assert "boxing" in drill["tags"]
        assert drill["modality"] == "technical_footwork"
        assert drill["movement_cost"] in {"low", "moderate"}
        assert drill["rpe"] <= 5
        assert "mech_lower_limb_weight_bearing" in drill["mechanical_risk_tags"]
        assert drill["late_windows"]
    assert bank["Double Pivot Chain"]["technical_complexity"] == "high"
    assert bank["Shift-Step Angle Creation"]["technical_complexity"] == "high"
    assert bank["Shift-Step Angle Creation"]["side_rule"] == "alternate_stances"


@pytest.mark.parametrize(
    ("style", "drill"),
    [("counter_striker", "Double Pivot Chain"), ("pressure_fighter", "Shift-Step Angle Creation")],
)
def test_established_pro_reaches_the_advanced_drill_for_their_style(style, drill):
    assert drill in _camp(style, "established_pro")


@pytest.mark.parametrize("style", ["counter_striker", "pressure_fighter", "distance_striker"])
def test_unknown_and_novice_camps_do_not_default_to_new_advanced_drills(style):
    for maturity in ("", "novice_amateur"):
        assert not set(_camp(style, maturity)) & ADVANCED_BOXING_DRILLS


def test_new_high_complexity_drills_never_reach_the_taper():
    for style in ("counter_striker", "pressure_fighter"):
        ranked = _ranked(phase="TAPER", style_tactical=[style], competitive_maturity="established_pro")
        assert "Double Pivot Chain" not in ranked
        assert "Shift-Step Angle Creation" not in ranked


def test_foot_feint_is_a_taper_safe_candidate_for_counter_strikers():
    ranked = _ranked(phase="TAPER", style_tactical=["counter_striker"])
    assert "Foot Feint and Reposition" in ranked
