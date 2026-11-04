"""Programming consequences of the post-#2959 goal, weakness and equipment fixes.

Two defects, both reproduced on real bank rows and complete programmes:

* Boxing aerobic ranking read goals and weak areas as symptom text. Only
  injuries and restrictions describe a current limitation. The "footwork" weak area (an intake option) contains "foot", and
  the legacy "Speed" goal normalizes to "reactive", an impact keyword. A
  healthy boxer was ranked as if lower-limb impact had to be avoided.
* Strength rows that spell out ``open_space`` were unreachable for every
  athlete: intake never offers it, although the conditioning bank prescribes
  hill sprints and sprint ladders with no equipment at all.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re

import pytest

from fightcamp import strength
from fightcamp.conditioning import get_conditioning_bank
from fightcamp.conditioning_boxing import _boxing_aerobic_context_flags, _boxing_aerobic_priority_adjustment
from fightcamp.injury_guard import injury_decision
from fightcamp.main import generate_plan_sync
from fightcamp.stage2_payload import _serialize_strength_option
from tests.planner_clock import pin_planner_clock

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/tag_retirement_plans.json"
NOW = datetime(2026, 10, 11, 9, 0)

REACHABLE_SPACE_ROWS = ("Sprint Acceleration (10-20m)", "Carioca Sprint", "Lateral Shuffle Sprint")
HELD_BACK_SPACE_ROWS = ("Backpedal-to-Sprint Transition", "Single-Leg Bounding")
# Conditioning sprints every athlete can already receive (no equipment).
REACHABLE_CONDITIONING_SPRINTS = ("Hill Sprint Repeats", "Short Hill Sprints (5x10s)",
                                  "Descending Sprint Ladder (80-60-40m)")


def _context(**overrides):
    kwargs = dict(injuries=[], weaknesses=[], goals=[], restrictions=[], equipment_access_set=set())
    kwargs.update(overrides)
    return _boxing_aerobic_context_flags(**kwargs)


# --- Goals and weak areas are not symptom text ------------------------------

@pytest.mark.parametrize("overrides", [
    {"weaknesses": ["footwork"]},
    {"weaknesses": ["footwork", "gas_tank", "coordination", "trunk_strength"]},
    {"goals": ["reactive"]},  # the legacy "Speed" / "Speed / Reaction" goal
    {"goals": ["shoulders", "hip", "quad_dominant"]},  # legacy regional goals
])
def test_performance_selections_do_not_read_as_tissue_state(overrides):
    assert _context(**overrides) == _context()


@pytest.mark.parametrize("weakness", ["knee stability", "knees", "hip mobility", "adductor groin", "shoulders",
                                      "sore hamstrings"])
def test_weak_areas_are_improvement_targets_not_limitations(weakness):
    # Performance weakness != functional limitation != medical restriction.
    assert _context(weaknesses=[weakness]) == _context()


@pytest.mark.parametrize("injury,flag", [
    ("ankle sprain", "lower_limb_unload_desirable"),
    ("footwork drills hurt my foot", "lower_limb_unload_desirable"),
    ("reactive landing pain", "impact_tolerance_reduced"),
    ("shin splints", "impact_tolerance_reduced"),
    ("tendinopathy flare", "active_tissue_irritation"),
    ("shoulder impingement", "upper_body_swim_sensitive"),
])
def test_injury_text_matching_is_unchanged(injury, flag):
    # Injury text keeps substring matching; only performance selections changed.
    assert _context(injuries=[injury])[flag]


def test_impact_restriction_still_reduces_impact_tolerance():
    assert _context(restrictions=[{"key": "high_impact"}])["impact_tolerance_reduced"]


@pytest.mark.parametrize("name", ["5K Steady Run", "Nasal Jog", "Shadowboxing (Light Pace)"])
def test_real_aerobic_rows_rank_the_same_for_footwork_and_speed_athletes(name):
    drill = next(d for d in get_conditioning_bank() if d.get("name") == name)
    common = dict(injuries=[], restrictions=[], equipment_access_set={"heavy_bag"})
    clean = _boxing_aerobic_priority_adjustment(drill, weaknesses=[], goals=["conditioning"], **common)
    assert _boxing_aerobic_priority_adjustment(drill, weaknesses=["footwork"], goals=["conditioning"], **common) == clean
    assert _boxing_aerobic_priority_adjustment(drill, weaknesses=[], goals=["reactive"], **common) == clean
    injured = dict(common, injuries=["left ankle sprain"])
    assert _boxing_aerobic_priority_adjustment(drill, weaknesses=[], goals=[], **injured) < clean


# --- Complete programmes -----------------------------------------------------

def _payload(case, **fields):
    plans = json.loads(FIXTURE.read_text())["plans"]
    payload = deepcopy(next(p for p in plans if p["case"] == case)["payload"])
    for field in payload["data"]["fields"]:
        if field["label"] in fields:
            field["value"] = fields[field["label"]]
    return payload


def _plan(monkeypatch, case, **fields):
    pin_planner_clock(monkeypatch, NOW)
    return generate_plan_sync(_payload(case, **fields))


def _session_drills(plan):
    return re.findall(r"^\s*- \*\*(.+?)\*\*", plan["plan_text"], re.M)


def _top_exercises(plan):
    return [block.split("; ") for block in re.findall(r"\*\*Top Exercises:\*\* (.*)", plan["plan_text"])]


GOALS = "What are your key performance goals?"
WEAK = "Where do you feel weakest right now?"


@pytest.mark.parametrize("label", ["Speed", "Speed / Reaction"])
def test_legacy_speed_label_selects_the_same_programme_as_the_intake_value(monkeypatch, label):
    web = _plan(monkeypatch, "boxing_gpp_power_conditioning", **{GOALS: ["speed", "power", "conditioning"]})
    legacy = _plan(monkeypatch, "boxing_gpp_power_conditioning", **{GOALS: [label, "power", "conditioning"]})
    # On Main the legacy label swapped 5K Steady Run for two heavy-bag drills.
    assert "5K Steady Run" in _session_drills(web)
    assert _session_drills(legacy) == _session_drills(web)
    assert _top_exercises(legacy) == _top_exercises(web)


def test_footwork_weak_area_keeps_the_boxer_aerobic_running_preference(monkeypatch):
    footwork = _plan(monkeypatch, "boxing_gpp_power_conditioning", **{WEAK: ["footwork"]})
    balance = _plan(monkeypatch, "boxing_gpp_power_conditioning", **{WEAK: ["balance"]})
    # On Main the footwork athlete lost every run as if they had a foot problem.
    assert _session_drills(footwork).count("5K Steady Run") == _session_drills(balance).count("5K Steady Run") == 2


# --- Running space is not apparatus ------------------------------------------

def _strength_bank():
    return {row["name"]: row for row in strength.get_exercise_bank()}


@pytest.mark.parametrize("name", REACHABLE_SPACE_ROWS)
def test_space_only_rows_need_no_declared_equipment(name):
    assert strength._required_strength_equipment(_strength_bank()[name]) == []


@pytest.mark.parametrize("name", HELD_BACK_SPACE_ROWS)
def test_held_back_rows_keep_their_requirement(name):
    assert strength._required_strength_equipment(_strength_bank()[name]) == ["open_space"]


def test_other_strength_requirements_are_untouched():
    for row in strength.get_exercise_bank():
        if "open_space" not in str(row.get("equipment")):
            assert strength._required_strength_equipment(row) == strength.normalize_equipment_list(row.get("equipment", []))


_REGIONS = ("ankle", "foot", "toe", "knee", "hamstring", "achilles", "calf", "hip", "groin", "shin",
            "quad", "lower back", "hip flexor", "adductor")
_STRICTNESS = {"allow": 0, "modify": 1, "exclude": 2}


def _laxer_than_reachable_sprints(row):
    analogues = [d for d in get_conditioning_bank() if d.get("name") in REACHABLE_CONDITIONING_SPRINTS]
    assert len(analogues) == len(REACHABLE_CONDITIONING_SPRINTS)
    laxer = []
    for severity in ("low", "moderate", "high"):
        for region in _REGIONS:
            injury = [{"region": region, "severity": severity, "original_phrase": f"left {region}"}]
            own = _STRICTNESS[injury_decision(row, injury, "SPP", "low").action]
            floor = min(_STRICTNESS[injury_decision(a, injury, "SPP", "low").action] for a in analogues)
            if own < floor:
                laxer.append((severity, region))
    return laxer


@pytest.mark.parametrize("name", REACHABLE_SPACE_ROWS)
def test_newly_reachable_rows_are_gated_at_least_as_strictly_as_existing_sprints(name):
    assert _laxer_than_reachable_sprints(_strength_bank()[name]) == []


@pytest.mark.parametrize("name", HELD_BACK_SPACE_ROWS)
def test_held_back_rows_would_create_new_injured_exposure(name):
    # Remove a row from the hold only once its injury metadata passes the test above.
    assert ("high", "ankle") in _laxer_than_reachable_sprints(_strength_bank()[name]) or \
        ("high", "achilles") in _laxer_than_reachable_sprints(_strength_bank()[name])


def test_stage2_option_matches_stage1_reachability():
    bank = _strength_bank()
    for name in REACHABLE_SPACE_ROWS:
        assert _serialize_strength_option(bank[name], "test", None)["universally_available"] is True
    for name in HELD_BACK_SPACE_ROWS:
        assert _serialize_strength_option(bank[name], "test", None)["universally_available"] is False


def _strength_flags(**overrides):
    flags = dict(phase="SPP", fatigue="low", style_technical=["boxing"], style_tactical=["pressure_fighter"],
                 key_goals=["speed", "power"], primary_goal="speed", weaknesses=[], equipment=["barbell", "heavy_bag"],
                 injuries=[], training_frequency=4, days_available=4, training_days=["Monday", "Tuesday", "Thursday", "Saturday"],
                 days_until_fight=35, fight_format="boxing", random_seed=7)
    flags.update(overrides)
    return flags


def _strength_names(flags):
    result = strength.generate_strength_block(flags=flags)
    return {ex["name"] for ex in result.get("exercises", [])}


def test_speed_primary_boxer_receives_acceleration_work_in_spp():
    names = _strength_names(_strength_flags())
    assert "Sprint Acceleration (10-20m)" in names
    assert not names & set(HELD_BACK_SPACE_ROWS)


@pytest.mark.parametrize("overrides", [
    {"phase": "GPP"},
    {"injuries": [{"region": "hamstring", "severity": "moderate", "original_phrase": "left hamstring"}]},
    {"injuries": [{"region": "foot", "severity": "high", "original_phrase": "right foot"}]},
])
def test_existing_phase_and_injury_gates_still_hold_sprints_out(overrides):
    assert not _strength_names(_strength_flags(**overrides)) & set(REACHABLE_SPACE_ROWS)


