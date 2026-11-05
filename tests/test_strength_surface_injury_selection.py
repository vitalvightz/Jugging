"""A later regional keyword pass cannot undo the shared surface-injury policy."""
from copy import deepcopy

import pytest

from fightcamp import strength
from fightcamp.injury_guard import injury_decision

JUMP = "Jump-in-Place (Max Frequency)"


@pytest.mark.parametrize("injuries,allowed", [
    ([], True),
    (["left bicep cut"], True),
    (["moderate left elbow pain"], False),
    (["left bicep cut", "moderate left elbow pain"], False),
    (["left bicep infected cut"], False),
], ids=["healthy", "surface-cut", "joint-pain", "mixed-injuries", "surface-red-flag"])
def test_final_strength_selection_preserves_surface_policy_with_a_safe_fallback(monkeypatch, injuries, allowed):
    # Keep real bank mechanics and real injury rules. A second real candidate
    # exposes replacement: a one-row bank can re-add the removed jump later.
    names = {JUMP, "Adductor Squeeze Isometric"}
    bank = [deepcopy(row) for row in strength.get_exercise_bank() if row["name"] in names]
    assert {row["name"] for row in bank} == names
    jump = next(row for row in bank if row["name"] == JUMP)
    if injuries == ["left bicep cut"]:
        verdict = injury_decision(jump, injuries, "SPP", "low")
        assert verdict.action == "allow"
        assert verdict.reason["disposition"] == "allow_with_skin_precautions"
    monkeypatch.setattr(strength, "get_exercise_bank", lambda: deepcopy(bank))
    monkeypatch.setattr(strength, "allocate_sessions", lambda *args, **kwargs: {"strength": 1})
    monkeypatch.setattr(strength, "calculate_exercise_numbers", lambda *args, **kwargs: {"strength": 1})
    monkeypatch.setattr(strength, "strength_quality_adjustment", lambda row, phase=None: (0.0, strength.classify_strength_item(row)))
    monkeypatch.setattr(strength, "score_exercise", lambda **kwargs: (
        100.0 if "mech_ballistic" in kwargs["exercise_tags"] else 10.0,
        {"reason_codes": []},
    ))
    result = strength.generate_strength_block(flags=dict(
        phase="SPP", fatigue="low", fight_format="boxing", sport="boxing",
        equipment=["bodyweight", "pad"], key_goals=["speed"], random_seed=7,
        training_days=["Monday"], training_frequency=1, days_available=1,
        days_until_fight=18, injuries=injuries,
    ), weaknesses=[])
    selected = {row["name"] for row in result["exercises"]}
    assert (JUMP in selected) is allowed
    assert selected <= names
