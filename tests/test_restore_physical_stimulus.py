"""Saved intake and independent camps must retain real, bank-priced movement."""
from copy import deepcopy
from datetime import date, timedelta
import json

import pytest

from fightcamp.calendar_context import role_d_day
from fightcamp.combat_load_policy import (
    CalendarEvent, LoadClass, contact_load_profile,
    evaluate_candidate_at_position, role_load_profile,
)
from fightcamp.goal_preservation import collect_goal_evidence
from fightcamp.prescription_resolver import apply_effective_strength_prescriptions
from fightcamp.programme_fidelity import programme_fidelity_findings, reconcile_authoritative_programme
from fightcamp.sparring_dose_planner import compute_hard_sparring_plan, hard_sparring_cutoff
from fightcamp.stage2_role_map import enforce_total_session_budget
from fightcamp.stage2_validator import _total_session_budget_warnings
from tools.replay_physical_stimulus import ROOT, SOURCE, cases, replay


@pytest.fixture(scope="module")
def programmes():
    source = json.loads(SOURCE.read_text())
    return {name: replay(intake, include_generation_day=name == "saved_plan")
            for name, intake in cases(source).items() if name != "recorded_boxing_52_day"}


@pytest.fixture(scope="module")
def saved_plan_requiring_speed_repair(programmes):
    # 672ac5a4 restores the typed-cut speed candidate, so production can now
    # satisfy speed during ordinary composition. Create the original missing-
    # role premise explicitly to keep testing restoration and filler removal.
    from fightcamp.goal_preservation import reconcile_goal_preservation

    brief = deepcopy(programmes["saved_plan"][0])
    week, host = next((w, r) for w in brief["weekly_role_map"]["weeks"]
                      for r in w["session_roles"] if role_d_day(w, r) >= 14
                      and any(m["name"] == "Jump-in-Place (Max Frequency)"
                              for m in r.get("selected_exercise_assignments", [])))
    week["session_roles"].remove(host)
    # Preserve the allocator's complete category budget; replacing its candidate
    # reservoir with one role would make the existing D-13 strength touch consume
    # the only slot and test a capacity refusal instead of a repair.
    assert any(r["role_key"] == host["role_key"] for r in week["goal_repair_candidates"])
    day = next(d for d in week["calendar_days"] if d["d_day"] == 17)
    week["session_roles"].append({
        "role_key": "joint_prep", "category": "support_insert",
        "scheduled_day_hint": day["weekday"], "countdown_offset": 17,
        "physical_occupancy_fill": True, "cost_class": "low",
        "governance": {"meaningful_stress": False},
    })
    assert not any("speed_quality" in e["intents"] and 14 <= (e.get("d_day") or 0) <= 17
                   for e in collect_goal_evidence(brief))
    return reconcile_goal_preservation(brief)


def test_saved_intake_replays_the_production_calendar_context(programmes):
    brief, summary = programmes["saved_plan"]
    athlete = brief["athlete_snapshot"]
    assert athlete["days_until_fight"] == 18
    assert athlete["include_generation_day"] is True
    assert athlete["training_frequency"] == 4
    # 62c8fa6d explicitly keeps this flag true for wound-care visibility;
    # surface-only injuries must not add rehab, restriction or compression.
    assert athlete["has_active_injury"] is True
    assert athlete["surface_injury_only"] is True
    assert not athlete["injury_restrictions"]
    assert "injury_management" not in athlete["readiness_flags"]
    assert summary["hard_sparring_cutoff"] == 17
    assert summary["effective_hard_appointments"] == 0
    assert all(c["allows_compatible_sc"] for c in summary["contacts"])


@pytest.mark.parametrize("name", ["saved_plan", "boxing_28_day", "boxing_56_day"])
def test_more_real_physical_stimulus_than_main_without_invented_doses(programmes, name):
    brief, summary = programmes[name]
    baseline = json.loads((ROOT / "docs/physical-stimulus-comparison.json").read_text())["main"]["cases"][name]
    assert summary["sc_sessions"] > baseline["sc_sessions"]
    goals = {g["goal"]: g for g in brief["goal_preservation"]}
    for goal in ("speed", "footwork", "power"):
        assert goals[goal]["satisfied"], goals[goal]
    for goal in ("speed", "footwork"):
        assert goals[goal]["state"] == "build"
        assert all(e["development_quality"] for e in goals[goal]["evidence"])
        assert all(e.get("dose_authority") not in {"scheduled_support_dose", "computed_support.recovery"}
                   for e in goals[goal]["evidence"])
    # Exercise source identity and effective dose survive deterministic rendering.
    rendered = reconcile_authoritative_programme(brief, "# Physical stimulus replay\n")
    assert rendered["findings"] == []
    assert programme_fidelity_findings(brief, rendered["text"]) == []


def test_existing_strength_session_carries_speed_and_moving_footwork(saved_plan_requiring_speed_repair):
    brief = saved_plan_requiring_speed_repair
    evidence = collect_goal_evidence(brief)
    speed = next(e for e in evidence if e.get("name") == "Jump-in-Place (Max Frequency)" and e["d_day"] >= 14)
    footwork = next(e for e in evidence if e.get("name") == "Step-Back Pivot Reset" and e["d_day"] >= 14)
    assert speed["role_key"] == footwork["role_key"]
    assert speed["d_day"] == footwork["d_day"]
    assert speed["effective_prescription"] == "3 x 2 at max speed; rest 120s"
    assert footwork["rounds"] == 2 and footwork["work_sec"] == 75 and footwork["rest_sec"] == 60
    assert not any(e.get("name") == "Boxer stance weight-shift hold" and "footwork_practice" in e["intents"] for e in evidence)
    assert ":alternate:" in speed["slot_id"]
    repair = next(g for g in brief["goal_preservation"] if g["goal"] == "speed")["repair_attempts"]
    assert any("joint_prep" in row.get("displaced_fillers", []) for row in repair)
    assert not any(r["role_key"] == "joint_prep" and role_d_day(w, r) == speed["d_day"]
                   for w in brief["weekly_role_map"]["weeks"] for r in w["session_roles"])


def test_appointment_overruns_are_visible_and_never_trim_selected_work(programmes):
    brief, summary = programmes["saved_plan"]
    from fightcamp.goal_preservation import _effective_map
    role_map = deepcopy(_effective_map(brief))
    before = deepcopy(role_map)
    enforce_total_session_budget(role_map, brief["athlete_snapshot"], brief["candidate_pools"])
    for old, new in zip(before["weeks"], role_map["weeks"]):
        assert old["session_roles"] == new["session_roles"]
        assert old.get("suppressed_roles") == new.get("suppressed_roles")
    from fightcamp.physical_session_frequency import appointment_budget_exceeded
    day_overrun = appointment_budget_exceeded(role_map, brief["athlete_snapshot"]["training_frequency"])
    assert bool(_total_session_budget_warnings(brief)) is day_overrun
    assert any("requested day count exceeded" in flag for w in role_map["weeks"]
               for flag in w.get("coach_note_flags", [])) is day_overrun
    # Historical summary's appointment total is a separate metric.
    assert summary["count_exceeded"] is True


def test_low_risk_short_camp_retains_power_on_a_converted_technical_day(programmes):
    brief, summary = programmes["low_risk_10_day"]
    assert summary["hard_sparring_cutoff"] == 14
    assert summary["sc_sessions"] >= 3
    assert all(g["satisfied"] for g in brief["goal_preservation"])
    technical_days = {c["d_day"] for c in summary["contacts"] if c["effective_load"] == "technical_contact"}
    strength_days = {r["d_day"] for r in summary["roles"] if r["category"] == "strength" and r["exercises"]}
    assert strength_days & technical_days
    # A short camp keeps maintenance, never fabricates development in the tail.
    assert not any(e["development_quality"] and "speed_quality" in e["intents"] for e in collect_goal_evidence(brief))


@pytest.mark.parametrize("fatigue,cutoff", [("low", 14), ("high", 17)])
def test_conversion_uses_the_resolved_cutoff_and_preserves_medical_blocks(fatigue, cutoff):
    athlete = {"sport": "boxing", "fatigue": fatigue, "days_until_fight": 30,
               "fight_date": "2026-11-01", "readiness_flags": []}
    assert hard_sparring_cutoff(athlete) == cutoff
    weekday = (date(2026, 11, 1) - timedelta(days=cutoff)).strftime("%A")
    week = {"week_index": 1, "phase": "SPP", "stage_key": "specific_density_build",
            "declared_hard_sparring_days": [weekday], "fight_weekday": "sunday", "span_days": 1,
            "projected_days_until_fight_start": cutoff, "projected_days_until_fight_end": cutoff}
    resolved = compute_hard_sparring_plan(week=week, athlete_snapshot=athlete)[0]
    assert resolved["effective_load"] == "technical"
    technical = contact_load_profile(resolved)
    strength = role_load_profile({"category": "strength", "role_key": "primary_strength_day"})
    assert evaluate_candidate_at_position(strength, candidate_position=0, events=[CalendarEvent(0, technical)]).allowed
    assert evaluate_candidate_at_position(technical, candidate_position=0, events=[CalendarEvent(0, strength)]).allowed
    medical = compute_hard_sparring_plan(week=week, athlete_snapshot={**athlete, "readiness_flags": ["medical_contact_restriction"]})[0]
    assert medical["effective_load"] == "none"
    assert not contact_load_profile(medical).allows_light_combat_stack


@pytest.mark.parametrize("factory", [contact_load_profile, role_load_profile])
def test_high_stress_technical_work_still_blocks_meaningful_sc(factory):
    technical = factory({"role_key": "light_combat_day", "effective_load": "technical",
                         "calendar_stress": {"systemic": 3, "neural_mechanical": 3}})
    strength = role_load_profile({"category": "strength", "role_key": "primary_strength_day"})
    decision = evaluate_candidate_at_position(strength, candidate_position=0, events=[CalendarEvent(0, technical)])
    assert not decision.allowed
    assert technical.load_class is LoadClass.TECHNICAL_CONTACT


def test_explicit_technical_stress_survives_composition_without_app_exercises():
    from fightcamp.session_composition import _refresh_stress_stamps
    role = {"role_key": "light_combat_day", "category": "technical",
            "calendar_stress": {"systemic": 3, "neural_mechanical": 3}}
    role_map = {"weeks": [{"session_roles": [role]}]}
    _refresh_stress_stamps(role_map)
    assert role["calendar_stress"] == {"systemic": 3, "neural_mechanical": 3}


def test_retained_recovery_bank_members_receive_canonical_doses(programmes):
    _, summary = programmes["saved_plan"]
    recovery = [r for r in summary["roles"] if r["category"] == "recovery" and r["exercises"]]
    assert recovery
    assert all(e["prescription"] for r in recovery for e in r["exercises"])


def test_maintenance_repair_uses_the_same_canonical_dose_as_existing_work(saved_plan_requiring_speed_repair):
    brief = saved_plan_requiring_speed_repair
    role_map = deepcopy(brief["weekly_role_map"])
    role = next(r for w in role_map["weeks"] for r in w["session_roles"]
                if r.get("goal_preservation_repair") and role_d_day(w, r) >= 14)
    before = deepcopy(role["effective_strength_prescriptions"])
    role["goal_preservation_repair"]["mode"] = "maintenance"
    apply_effective_strength_prescriptions(weekly_role_map=role_map, candidate_pools=brief["candidate_pools"],
                                          athlete_model=brief["athlete_snapshot"])
    assert role["effective_strength_prescriptions"] == before
    assert role["dose_adjustment_reason"] != "goal_preservation_reduced_maintenance"


@pytest.mark.parametrize("name", ["saved_plan", "boxing_28_day", "boxing_56_day"])
def test_goal_repair_never_stacks_two_meaningful_strength_sessions(programmes, name):
    from fightcamp.combat_load_policy import role_load_profile, LoadClass
    from fightcamp.stage2_validator import _same_day_strength_ownership_warnings

    brief, _ = programmes[name]
    assert not _same_day_strength_ownership_warnings(brief)
    for week in brief["weekly_role_map"]["weeks"]:
        by_day = {}
        for role in week.get("session_roles") or []:
            profile = role_load_profile(role)
            if (profile is not None and profile.load_class is LoadClass.MEANINGFUL_STRENGTH
                    and (day := role_d_day(week, role)) is not None):
                by_day.setdefault(day, []).append(role)
        assert all(len(roles) == 1 for roles in by_day.values())
    goals = {item["goal"]: item for item in brief["goal_preservation"]}
    assert goals["speed"]["satisfied"]
    assert goals["footwork"]["satisfied"]


def test_validator_detects_exact_d26_double_strength_and_repeated_footwork():
    from fightcamp.stage2_validator import _same_day_strength_ownership_warnings

    roles = [
        {"role_key": "primary_strength_day", "category": "strength",
         "scheduled_countdown_label": "D-26",
         "selected_exercise_assignments": [
             {"name": "Sled Push (Board)", "exercise_key": "sled-push-board"},
             {"name": "Step-Back Pivot Reset", "exercise_key": "step-back-pivot-reset"},
         ]},
        {"role_key": "secondary_strength_day", "category": "strength",
         "scheduled_countdown_label": "D-26",
         "selected_exercise_assignments": [
             {"name": "Step-Back Pivot Reset", "exercise_key": "step-back-pivot-reset"},
             {"name": "Back Squat", "exercise_key": "back-squat"},
         ]},
    ]
    brief = {"weekly_role_map": {"weeks": [{"session_roles": roles}]}}
    warnings = _same_day_strength_ownership_warnings(brief)
    assert all(warning["severity"] == "warning" for warning in warnings)
    assert {warning["code"] for warning in warnings} == {
        "duplicate_meaningful_strength_day", "duplicate_physical_exercise_day",
    }
    roles[1]["scheduled_countdown_label"] = "D-19"
    assert _same_day_strength_ownership_warnings(brief) == []


def test_guided_surface_cut_reaches_the_movement_guard_with_its_type(programmes):
    # The guided card's phrase is only its area. Without the resolved type the
    # guard read "left bicep" as a moderate elbow injury and excluded jumps.
    from fightcamp.injury_guard import injury_decision

    brief, _ = programmes["saved_plan"]
    injuries = brief["athlete_snapshot"]["injuries"]
    assert injuries == ["left bicep cut"]
    jump = next(row for row in json.loads((ROOT / "data/exercise_bank.json").read_text())
                if row["name"] == "Jump-in-Place (Max Frequency)")
    assert injury_decision(jump, injuries, "SPP", "low").action == "allow"
    assert injury_decision(jump, ["left bicep"], "SPP", "low").action == "exclude"
