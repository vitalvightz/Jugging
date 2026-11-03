"""Footwork as the primary weakness: the open block varies the movement pattern.

Reproduces a production open plan (boxing distance striker, skill refinement
primary goal, footwork primary weakness, no status/record) whose 4-week block
repeated Step-Back Pivot Reset on Monday and Saturday every week while the bank
held other relevant drills. Checks the final weekly_session_plan, not just
candidate eligibility.
"""
from __future__ import annotations

import copy
import datetime as dt
from collections import Counter

import pytest

from api.models import PlanRequest
from fightcamp.conditioning import get_technical_footwork_bank
from fightcamp.main import generate_plan_sync
from planner_clock import pin_planner_clock

_INTAKE = {
    "notes": "",
    "athlete": {
        "age": 23, "sex": None, "record": "", "stance": "", "full_name": "Footwork Test",
        "height_cm": None, "weight_kg": None, "athlete_locale": "",
        "tactical_style": ["distance_striker"], "technical_style": ["boxing"],
        "athlete_timezone": "Europe/London", "target_weight_kg": None, "professional_status": "",
    },
    "injuries": "Hamstring is kinda tight [training_impact:not_limiting]",
    "key_goals": ["skill_refinement", "power", "speed"],
    "fight_date": "", "weak_areas": ["footwork"], "primary_goal": "skill_refinement",
    "fatigue_level": "", "guided_injury": None, "rounds_format": "3 x 3", "guided_injuries": [],
    "open_camp_weeks": 12,
    "equipment_access": [
        "dumbbells", "kettlebells", "barbell", "trap_bar", "plate", "bench", "cable", "landmine",
        "assault_bike", "rower", "treadmill", "sled", "battle_ropes", "box", "agility_ladder",
        "hurdles", "jump_rope", "medicine_ball", "sandbag", "bands", "trx", "pullup_bar",
        "swiss_ball", "heavy_bag", "pads", "foam_roller", "towel",
    ],
    "primary_weak_area": "footwork", "support_work_days": ["Monday", "Tuesday"],
    "hard_sparring_days": ["Thursday", "Friday"], "mindset_challenges": "",
    "no_scheduled_fight": True, "training_preference": "",
    "training_availability": ["Monday", "Saturday", "Thursday", "Friday", "Wednesday", "Tuesday"],
    "reduced_contact_requested": False, "weekly_training_frequency": 5,
    "goal_weakness_collision_tags": [], "goal_weakness_collision_detail": "",
    "goal_weakness_collision_details": [],
}
_FOOTWORK = {drill["name"] for drill in get_technical_footwork_bank()}


@pytest.fixture(scope="module")
def brief() -> dict:
    payload = PlanRequest(**copy.deepcopy(_INTAKE)).to_payload()
    payload["random_seed"] = 814959391
    mp = pytest.MonkeyPatch()
    pin_planner_clock(mp, dt.datetime(2026, 11, 3, 14, 22))
    try:
        return generate_plan_sync(payload)["planning_brief"]
    finally:
        mp.undo()


def _weekly_footwork(brief: dict) -> list[list[str]]:
    weeks = brief["open_plan_spec"]["weekly_session_plan"]["weeks"]
    return [
        [exercise["name"] for session in week["sessions"]
         for exercise in session.get("exercises") or [] if exercise["name"] in _FOOTWORK]
        for week in weeks
    ]


def test_footwork_slots_carry_ranked_same_role_alternates(brief):
    slots = [slot for slot in brief["candidate_pools"]["GPP"]["conditioning_slots"]
             if slot.get("role") == "technical_footwork"]
    assert slots
    names = {slot["selected"]["name"] for slot in slots}
    names |= {alt["name"] for slot in slots for alt in slot.get("alternates") or []}
    assert "Foot Feint and Reposition" in names
    # Unknown maturity: the high-complexity advanced drills are not pushed in.
    assert not names & {"Double Pivot Chain", "Shift-Step Angle Creation"}


def test_no_footwork_drill_repeats_within_a_week(brief):
    for week in _weekly_footwork(brief):
        assert week, "footwork primary weakness must keep a footwork exposure every week"
        repeats = [name for name, count in Counter(week).items() if count > 1]
        assert not repeats, f"repeated within the week: {repeats}"


def test_variety_does_not_reduce_footwork_exposures(brief):
    # Before the fix this plan carried three footwork exposures a week (two
    # distinct drills); variety must not come from dropping exposures.
    assert all(len(week) >= 3 for week in _weekly_footwork(brief))
    assert all(len(set(week)) >= 3 for week in _weekly_footwork(brief))


def test_pattern_is_stable_across_the_block_so_it_can_progress(brief):
    weeks = [sorted(week) for week in _weekly_footwork(brief)]
    assert all(week == weeks[0] for week in weeks)


# High fatigue removes the speed-dose slot, so the energy-system drills fill the
# conditioning cap and the best-effort footwork insert used to be skipped.
def _plan(**over) -> dict:
    intake = copy.deepcopy(_INTAKE)
    intake.update(over)
    payload = PlanRequest(**intake).to_payload()
    payload["random_seed"] = 814959391
    mp = pytest.MonkeyPatch()
    pin_planner_clock(mp, dt.datetime(2026, 11, 3, 14, 22))
    try:
        return generate_plan_sync(payload)["planning_brief"]
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def high_fatigue_brief() -> dict:
    return _plan(fatigue_level="high")


def _footwork_slots(brief: dict) -> list[dict]:
    return [slot for slot in brief["candidate_pools"]["GPP"]["conditioning_slots"]
            if slot.get("role") == "technical_footwork"]


def test_primary_footwork_survives_high_fatigue_capacity(high_fatigue_brief):
    assert _footwork_slots(high_fatigue_brief)
    weeks = _weekly_footwork(high_fatigue_brief)
    assert all(weeks), "footwork primary weakness keeps a footwork exposure every week"
    for week in weeks:
        assert len(week) == len(set(week))


def test_footwork_swap_replaces_a_surplus_drill_and_keeps_every_system(caplog):
    with caplog.at_level("INFO", logger="fightcamp.conditioning"):
        brief = _plan(fatigue_level="high")
    displaced = [r.getMessage() for r in caplog.records if "technical_footwork_displaced" in r.getMessage()]
    # One swap per source phase: footwork replaced a drill, it did not add one.
    assert {m.split("phase=")[1].split()[0] for m in displaced} == {"GPP", "SPP"}
    systems = {slot.get("role") for slot in brief["candidate_pools"]["GPP"]["conditioning_slots"]}
    assert {"aerobic", "glycolytic", "alactic"} <= systems


def test_secondary_footwork_stays_best_effort_under_high_fatigue():
    brief = _plan(fatigue_level="high", weak_areas=["speed", "footwork"], primary_weak_area="speed")
    assert not _footwork_slots(brief)
