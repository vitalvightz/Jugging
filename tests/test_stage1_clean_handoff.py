"""Stage 2 must receive a clean Stage 1 plan.

Production (Nov 2026): a trunk-strength athlete had a Kettlebell Windmill
embedded beside a 30-minute continuous Easy Assault Bike, with a range dose
("1-2 controlled sets x 3-5 slow reps"). The structured-card audit rejected the
whole card (substantial_endurance_strength_conflict) and the app showed an
empty Full prescription. These tests sweep real intakes through Stage 1 and
assert the handoff invariants, plus the guard's own repair contract.
"""
from __future__ import annotations

import copy
import datetime as dt
import re
from collections import Counter

import pytest

from api.models import PlanRequest
from fightcamp.goal_preservation import _effective_map
from fightcamp.main import generate_plan_sync
from fightcamp.session_workload import UNSAFE_STACKING_CODE, substantial_endurance
from fightcamp.stage1_handoff_integrity import HANDOFF_STACKING_REMOVAL, enforce_handoff_integrity
from planner_clock import pin_planner_clock

_NOW = dt.datetime(2026, 11, 3, 15, 28)
_BASE = {
    "athlete": {
        "age": 23, "sex": None, "record": "", "stance": "", "full_name": "Handoff Test",
        "height_cm": None, "weight_kg": None, "athlete_locale": "",
        "tactical_style": ["distance_striker"], "technical_style": ["boxing"],
        "athlete_timezone": "Europe/London", "target_weight_kg": None, "professional_status": "",
    },
    "injuries": "", "key_goals": ["conditioning", "speed"], "event_name": "Test",
    "fight_date": "2026-11-30", "weak_areas": ["trunk_strength", "footwork"],
    "primary_goal": "conditioning", "fatigue_level": "", "guided_injury": None,
    "rounds_format": "3 x 3", "guided_injuries": [], "open_camp_weeks": 12,
    "equipment_access": [
        "bands", "heavy_bag", "partner", "medicine_ball", "barbell", "kettlebells", "assault_bike",
        "dumbbells", "trap_bar", "bench", "landmine", "battle_ropes", "treadmill", "hurdles",
        "jump_rope", "pullup_bar", "sandbag", "towel", "swiss_ball", "foam_roller",
    ],
    "primary_weak_area": "trunk_strength", "support_work_days": ["Wednesday"],
    "hard_sparring_days": ["Friday"], "mindset_challenges": "", "no_scheduled_fight": False,
    "training_preference": "",
    "training_availability": ["Tuesday", "Thursday", "Friday", "Wednesday", "Saturday", "Monday"],
    "reduced_contact_requested": False, "weekly_training_frequency": 6,
    "goal_weakness_collision_tags": [], "goal_weakness_collision_detail": "",
    "goal_weakness_collision_details": [],
}
_CASES = {
    "production_trunk_primary": {},
    "high_fatigue": {"fatigue_level": "high"},
    "eight_week_camp": {"fight_date": (_NOW.date() + dt.timedelta(weeks=8)).isoformat()},
    "footwork_primary": {"primary_weak_area": "footwork", "weak_areas": ["footwork", "trunk_strength"]},
    "strength_goal": {"key_goals": ["strength", "power"], "primary_goal": "strength"},
}
_RANGE_DOSE = re.compile(r"^\s*\d+\s*-\s*\d+\s+controlled sets", re.I)


@pytest.fixture(scope="module", params=sorted(_CASES))
def brief(request) -> dict:
    intake = copy.deepcopy(_BASE)
    intake.update(_CASES[request.param])
    payload = PlanRequest(**intake).to_payload()
    payload["random_seed"] = 518647419
    mp = pytest.MonkeyPatch()
    pin_planner_clock(mp, _NOW)
    try:
        return generate_plan_sync(payload)["planning_brief"]
    finally:
        mp.undo()


def _days(brief: dict):
    for week in _effective_map(brief)["weeks"]:
        days: dict[str, list[dict]] = {}
        for role in week["session_roles"]:
            days.setdefault(str(role.get("scheduled_countdown_label")), []).append(role)
        yield from days.items()


def test_no_day_pairs_substantial_endurance_with_strength(brief):
    for day, roles in _days(brief):
        members = [m for r in roles for m in r.get("selected_exercise_assignments") or []]
        endurance = [m["name"] for m in members
                     if m.get("slot_group") != "strength_slots" and substantial_endurance(m)]
        strength = [m["name"] for m in members if m.get("slot_group") == "strength_slots"]
        assert not (endurance and strength), (day, endurance, strength)


def test_every_member_has_one_exact_dose(brief):
    for day, roles in _days(brief):
        for role in roles:
            priced = {p.get("name") for p in role.get("effective_strength_prescriptions") or []
                      if p.get("effective_prescription")}
            for member in role.get("selected_exercise_assignments") or []:
                dose = str(member.get("effective_prescription") or "")
                assert dose.strip() or member.get("name") in priced or member.get("display_text"), (
                    day, role.get("role_key"), member.get("name"))
                assert not _RANGE_DOSE.match(dose), (day, member.get("name"), dose)


def test_no_session_holds_two_members_from_one_original_slot(brief):
    for day, roles in _days(brief):
        for role in roles:
            origins = Counter(str(m.get("slot_id") or "").partition(":alternate:")[0]
                              for m in role.get("selected_exercise_assignments") or [] if m.get("slot_id"))
            assert all(count == 1 for count in origins.values()), (day, role.get("role_key"), origins)


def test_handoff_has_no_unresolved_integrity_findings(brief):
    assert not _effective_map(brief).get("handoff_integrity_findings")


# --- the guard's own contract -------------------------------------------------

def _bike():
    return {"name": "Easy Assault Bike", "slot_group": "conditioning_slots",
            "effective_prescription": "30min continuous", "modality": "aerobic_bike"}


def _windmill(**extra):
    return {"name": "Kettlebell Windmill", "slot_group": "strength_slots",
            "effective_prescription": "2 sets x 5 slow reps; rest 60 sec", **extra}


def _map(*roles):
    return {"weeks": [{"session_roles": list(roles)}]}


def test_guard_removes_embedded_support_from_an_endurance_day():
    role = {"role_key": "aerobic_base_day", "scheduled_countdown_label": "D-26",
            "selected_exercise_assignments": [_bike(), _windmill(embedded_support=True)]}
    findings = enforce_handoff_integrity(_map(role))
    assert findings == []
    assert [m["name"] for m in role["selected_exercise_assignments"]] == ["Easy Assault Bike"]
    assert role["handoff_integrity"][0]["code"] == HANDOFF_STACKING_REMOVAL


def test_guard_never_deletes_programmed_strength_it_reports_it():
    bike_day = {"role_key": "aerobic_base_day", "scheduled_countdown_label": "D-26",
                "selected_exercise_assignments": [_bike()]}
    strength_day = {"role_key": "primary_strength_day", "scheduled_countdown_label": "D-26",
                    "selected_exercise_assignments": [_windmill()]}
    role_map = _map(bike_day, strength_day)
    findings = enforce_handoff_integrity(role_map)
    assert strength_day["selected_exercise_assignments"][0]["name"] == "Kettlebell Windmill"
    assert findings and findings[0]["code"] == UNSAFE_STACKING_CODE
    assert role_map["handoff_integrity_findings"] == findings


def test_guard_leaves_short_or_interval_conditioning_alone():
    role = {"role_key": "controlled_repeatability_day", "scheduled_countdown_label": "D-21",
            "selected_exercise_assignments": [
                {"name": "Heavy Bag Power Intervals", "slot_group": "conditioning_slots",
                 "effective_prescription": "8 x 30 sec work; 90 sec rest"},
                _windmill(embedded_support=True)]}
    assert enforce_handoff_integrity(_map(role)) == []
    assert len(role["selected_exercise_assignments"]) == 2
