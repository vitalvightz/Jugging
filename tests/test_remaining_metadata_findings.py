"""Programming consequences of the remaining-findings review, using live rows."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from fightcamp import conditioning, training_context
from fightcamp.training_catalogue import build_catalogue

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def catalogue():
    return {entry["catalogue_id"]: entry for entry in build_catalogue()}


def _style_result(monkeypatch, row, equipment, **overrides):
    monkeypatch.setattr(conditioning, "get_conditioning_bank", lambda: [])
    monkeypatch.setattr(conditioning, "get_style_conditioning_bank", lambda: [deepcopy(row)])
    monkeypatch.setattr(conditioning, "get_coordination_bank", lambda: [])
    monkeypatch.setattr(conditioning, "_load_bank", lambda *a, **k: [])
    flags = dict(phase="SPP", fatigue="low", sport="bjj", fight_format="bjj",
                 style_technical=["bjj"], style_tactical=["submission_hunter"],
                 key_goals=["conditioning"], weaknesses=["gas_tank"],
                 equipment=equipment, injuries=[], restrictions=[], random_seed=7,
                 training_frequency=3, days_available=3, days_until_fight=35)
    flags.update(overrides)
    return conditioning.generate_conditioning_block(flags)


def _candidates(result):
    return {c["drill"]["name"] for role, values in result[5].items()
            if not role.startswith("__") for c in values}


def _canonical_equipment(value):
    if isinstance(value, dict):
        return {k: training_context.normalize_equipment_list(v) if k == "equipment" else _canonical_equipment(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_equipment(v) for v in value]
    return value


def test_resistance_band_alias_repairs_real_style_candidate_eligibility(monkeypatch, catalogue):
    row = next(e["raw"] for e in catalogue.values() if e["name"] == "Limb Collector")
    with monkeypatch.context() as old:
        old.delitem(training_context.EQUIP_ALIASES, "resistance_bands")
        before = _style_result(old, row, ["bands"])
        compatible_before = _style_result(old, {**row, "equipment": ["bands"]}, ["bands"])
    after = _style_result(monkeypatch, row, ["bands"])
    assert row["name"] not in _candidates(before)
    assert row["name"] in _candidates(after)
    # This is solely an alias repair: complete generator output, score, dose,
    # selected fallback and ordered reservoir equal the existing canonical input.
    assert _canonical_equipment(after) == _canonical_equipment(compatible_before)


@pytest.mark.parametrize("equipment", [[], ["bodyweight"], ["battle_ropes"]])
def test_band_requirement_is_not_removed(monkeypatch, catalogue, equipment):
    row = next(e["raw"] for e in catalogue.values() if e["name"] == "Limb Collector")
    assert row["name"] not in _candidates(_style_result(monkeypatch, row, equipment))


@pytest.mark.parametrize("overrides", [
    {"phase": "GPP"}, {"phase": "TAPER", "days_until_fight": 7},
    {"days_until_fight": 1}, {"style_technical": ["boxing"], "sport": "boxing", "fight_format": "boxing"},
])
def test_band_alias_cannot_bypass_independent_gates(monkeypatch, catalogue, overrides):
    row = next(e["raw"] for e in catalogue.values() if e["name"] == "Limb Collector")
    assert row["name"] not in _candidates(_style_result(monkeypatch, row, ["bands"], **overrides))



def test_band_alias_keeps_explicit_clinical_restrictions(monkeypatch, catalogue):
    from fightcamp.restriction_parsing import parse_restriction_entry

    row = next(e["raw"] for e in catalogue.values() if e["name"] == "Limb Collector")
    result = _style_result(monkeypatch, row, ["bands"],
                           restrictions=[parse_restriction_entry("avoid Limb Collector")])
    assert row["name"] not in _candidates(result)
    assert row["name"] not in result[1]


@pytest.mark.parametrize("token", ["resistance_band", "resistance_bands", "resistance bands", "bands"])
def test_saved_band_spellings_resolve_to_same_capability(token):
    assert training_context.normalize_equipment_list([token]) == ["bands"]
    assert training_context.normalize_athlete_equipment_list([token]) == ["bands", "bodyweight"]


def test_equipment_quantity_and_suitability_distinctions_survive():
    for token in ("dumbbell", "dumbbells", "pads"):
        assert training_context.normalize_equipment_list([token]) == [token]
    # Product decision: mats, lines, tape and squeeze pads never gate eligibility.
    for token in ("mat", "mats", "pad"):
        assert training_context.normalize_equipment_list([token]) == []


def test_review_covers_exactly_the_original_74_findings():
    original = json.loads((ROOT / "tools/tag_retirement_review.json").read_text())
    report = json.loads((ROOT / "tools/remaining_metadata_review.json").read_text())
    expected = {f["issue_id"] for f in original["findings"] if f["classification"] == "requires_investigation"}
    actual = report["findings"]
    assert len(actual) == len(expected) == 74
    assert {f["issue_id"] for f in actual} == expected
    for finding in actual:
        assert finding["classification"] in {"confirmed_defect", "intentional_distinction", "cosmetic_inconsistency", "unresolved", "already_resolved"}
        assert finding["priority"] in {"P0", "P1", "P2", "P3"}
        assert finding["evidence"] and finding["pathways"] and finding["disposition"]
        for reference in finding["consumers"]:
            assert (ROOT / reference.split(":", 1)[0]).is_file(), reference


@pytest.mark.parametrize("phase,days,fatigue", [
    ("GPP", 35, "low"), ("SPP", 35, "low"), ("SPP", 35, "high"),
    ("SPP", 18, "low"), ("TAPER", 7, "moderate"), ("TAPER", 1, "high"),
])
@pytest.mark.parametrize("name", ["Bicep Slicer Drill", "Limb Collector", "Kneebar Khaos", "Toe Hold Torment"])
def test_each_band_row_matches_existing_canonical_setup_in_every_context(monkeypatch, catalogue, name, phase, days, fatigue):
    row = next(e["raw"] for e in catalogue.values() if e["name"] == name)
    # Compare complete outputs with only the source spelling changed. Existing
    # alactic-structure, fatigue, sport and countdown restrictions still own dose
    # and selection; not every correctly equipped row must become eligible.
    overrides = dict(phase=phase, days_until_fight=days, fatigue=fatigue)
    result = _style_result(monkeypatch, row, ["bands"], **overrides)
    canonical = _style_result(monkeypatch, {**row, "equipment": ["bands"]}, ["bands"], **overrides)
    assert _canonical_equipment(result) == _canonical_equipment(canonical)


@pytest.mark.parametrize("name", ["Bicep Slicer Drill", "Limb Collector", "Kneebar Khaos", "Toe Hold Torment"])
def test_editor_equipment_gate_accepts_existing_band_access(monkeypatch, catalogue, name):
    from types import SimpleNamespace
    from api.services.exercise_customisation import _equipment_available

    entry = next(e for e in catalogue.values() if e["name"] == name)
    record = {"entry": entry}
    athlete = SimpleNamespace(equipment={"bands"})
    with monkeypatch.context() as old:
        old.delitem(training_context.EQUIP_ALIASES, "resistance_bands")
        assert not _equipment_available(record, athlete)
    assert _equipment_available(record, athlete)
    assert not _equipment_available(record, SimpleNamespace(equipment=set()))
    assert not _equipment_available(record, SimpleNamespace(equipment={"battle_ropes"}))
