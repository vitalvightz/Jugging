"""Intake through effective scheduled doses for a coach-owned combat week."""
import datetime as dt
import json
import logging
from pathlib import Path

import pytest

from fightcamp import input_parsing
from fightcamp.input_parsing import PlanInput
from fightcamp.calendar_context import CalendarLegalityView, build_events, classify_role, role_d_day, week_scope
from fightcamp.combat_load_policy import LoadClass
from fightcamp.goal_preservation import _deferral_constraints, collect_goal_evidence, validate_goal_preservation
from fightcamp.declared_combat_ownership import build_declared_light_combat_role
from fightcamp.stage2_role_map import _boxing_crowded_week_policy_state
from fightcamp.plan_pipeline_blocks import generate_plan_blocks
from fightcamp.plan_pipeline_rendering import build_stage2_outputs
from fightcamp.plan_pipeline_runtime import RenderedPlanBundle, build_runtime_context
from fightcamp.physical_session_frequency import cadence_windows, physical_appointments, PHYSICAL_SUPPORT_ROLE_KEYS


def run_lead(monkeypatch, *, earlier=False, **overrides):
    name = "combat_allocation_earlier.json" if earlier else "combat_allocation_lead.json"
    fixture = json.loads((Path(__file__).parent / "fixtures/goal_preservation" / name).read_text())
    fixture.update(overrides)
    monkeypatch.setattr(input_parsing, "_utc_now", lambda: dt.datetime.fromisoformat(fixture["plan_date"]))
    from fightcamp import stage2_planning_brief
    monkeypatch.setattr(stage2_planning_brief, "_utc_now", lambda: dt.datetime.fromisoformat(fixture["plan_date"]))
    fields = {
        "Full name": "Combat Allocation Regression", "Age": "28", "Weight (kg)": "77",
        "Target Weight (kg)": "77", "Height (cm)": "180", "Stance": "Orthodox",
        "Fighting Style (Technical)": fixture.get("sport", "Boxing"), "Fighting Style (Tactical)": "Pressure Fighter",
        "Professional Status": fixture["status"], "Rounds x Minutes": fixture.get("rounds_format", "10x3"),
        "Weekly Training Frequency": str(fixture["training_frequency"] or ""),
        "Fatigue Level": fixture["fatigue"], "Equipment Access": ", ".join(fixture["equipment"]),
        "Training Availability": ", ".join(fixture["training_days"]),
        "Hard Sparring Days": ", ".join(fixture["hard_sparring_days"]),
        "Support Work Days": ", ".join(fixture["support_work_days"]),
        "What are your key performance goals?": ", ".join(fixture["key_goals"]),
        "Primary goal": fixture["primary_goal"],
        "Where do you feel weakest right now?": ", ".join(fixture["weaknesses"]),
        "Primary weak area": fixture["primary_weak_area"],
        "When is your next fight?": fixture["fight_date"],
        "Are there any parts of your previous plan you hated or loved?": fixture["notes"],
        "Any injuries or areas you need to work around?": fixture.get("injuries", "none"),
    }
    parsed = PlanInput.from_payload({"data": {"fields": [{"label": k, "value": v} for k, v in fields.items()]}})
    logger = logging.getLogger(__name__)
    context = build_runtime_context(plan_input=parsed, random_seed=1, logger=logger)
    blocks = generate_plan_blocks(context=context, logger=logger, record_timing=lambda *args, **kwargs: None)
    rendered = RenderedPlanBundle(fight_plan_text="", coach_notes="", reason_log={}, html="")
    _, brief, _ = build_stage2_outputs(context=context, blocks=blocks, rendered=rendered)
    return parsed, brief


def allocation_summary(brief):
    return {
        "goals": [{k: entry[k] for k in ("goal", "state", "satisfied", "constraints", "missing_windows", "repair_attempts") if k in entry}
                  for entry in brief["goal_preservation"]],
        "weeks": [{
            "week": week["week_index"], "phase": week["phase"],
            "hard_days": week.get("effective_hard_sparring_days"),
            "compression": week.get("intentional_compression"),
            "roles": [{k: role[k] for k in ("role_key", "scheduled_day_hint", "scheduled_d_day", "selected_exercise_assignments", "effective_strength_prescriptions") if k in role}
                      for role in week["session_roles"]],
        } for week in brief["weekly_role_map"]["weeks"]],
    }


@pytest.mark.parametrize("frequency", [6])
def test_final_pipeline_reports_totals_without_trimming_compatible_work(monkeypatch, frequency, sport="boxing"):
    parsed, brief = run_lead(monkeypatch, training_frequency=frequency, sport=sport)
    assert parsed.training_frequency == frequency
    double_days = []
    appointment_days = []
    for week in brief["weekly_role_map"]["weeks"]:
        for days in cadence_windows([(d["d_day"], d["weekday"]) for d in week["calendar_days"]]):
            offsets = {d for d, _ in days}
            # Count rendered standalone appointments independently of the cap
            # helper. A strength card and a coach card on Thursday count twice.
            full = [r for r in week["session_roles"] if role_d_day(week, r) in offsets
                    and r.get("category") in {"strength", "conditioning", "combat", "sparring", "technical", "recovery", "rehab"}
                    and r.get("role_key") not in PHYSICAL_SUPPORT_ROLE_KEYS
                    and not r.get("execution_only") and not r.get("nonphysical")]
            occupied = {role_d_day(week, r) for r in full}
            standalone_support = {role_d_day(week, r) for r in week["session_roles"]
                                  if r.get("role_key") in PHYSICAL_SUPPORT_ROLE_KEYS
                                  and role_d_day(week, r) in offsets - occupied}
            appointment_days.extend(role_d_day(week, r) for r in full)
            appointment_days.extend(standalone_support)
            for day in occupied:
                if sum(role_d_day(week, r) == day for r in full) >= 2:
                    double_days.append(day)
    slots = [(d["d_day"], d["weekday"]) for w in brief["weekly_role_map"]["weeks"] for d in w["calendar_days"]]
    # The weekly allowance is distinct physical training days, not appointments.
    exceeded = any(len(set(appointment_days) & {d for d, _ in days}) > frequency
                   for days in cadence_windows(slots))
    from fightcamp.stage2_validator import _total_session_budget_warnings
    assert bool(_total_session_budget_warnings(brief)) is exceeded
    assert double_days  # The cap must allow legal double days, including at six.
    assert_scheduled_doses_and_contact_safety(brief)


@pytest.mark.parametrize("sport", ["kickboxing", "muay_thai", "mma", "wrestling", "bjj"])
def test_session_reporting_and_shared_load_governance_are_universal(monkeypatch, sport):
    test_final_pipeline_reports_totals_without_trimming_compatible_work(monkeypatch, 6, sport=sport)


def test_appointment_counter_distinguishes_double_days_from_embedded_support():
    roles = [
        {"role_key": "light_combat_day", "category": "combat", "countdown_offset": 20},
        {"role_key": "primary_strength_day", "category": "strength", "countdown_offset": 20},
        {"role_key": "footwork_walkthrough", "category": "recovery", "countdown_offset": 20},
        {"role_key": "mobility_rehab", "category": "rehab", "countdown_offset": 19},
        {"role_key": "tactical_watch", "category": "tactical", "countdown_offset": 19},
    ]
    assert len(physical_appointments(roles)) == 3


def test_budget_retains_host_and_embedded_support_and_reports_overrun():
    from fightcamp.stage2_role_map import enforce_total_session_budget
    coach = {"role_key": "light_combat_day", "category": "technical", "coach_owned": True,
             "scheduled_countdown_label": "D-20"}
    roles = [coach, {"role_key": "secondary_strength_day", "category": "strength", "scheduled_countdown_label": "D-19"},
             {"role_key": "footwork_walkthrough", "category": "support_insert", "scheduled_countdown_label": "D-19"}]
    role_map = {"weeks": [{"phase": "GPP", "session_roles": roles,
                           "calendar_days": [{"d_day": 20, "weekday": "Monday"}, {"d_day": 19, "weekday": "Tuesday"}]}]}
    enforce_total_session_budget(role_map, {"training_frequency": 1})
    assert len(roles) == 3
    assert len(physical_appointments([{**r, "countdown_offset": role_d_day({}, r)} for r in roles])) == 2
    assert any("requested day count exceeded" in flag for flag in role_map["weeks"][0]["coach_note_flags"])


def test_impossible_coach_schedule_is_flagged_without_deleting_coach_sessions():
    from fightcamp.stage2_role_map import enforce_total_session_budget
    from fightcamp.stage2_validator import _total_session_budget_warnings
    roles = [{"role_key": "light_combat_day", "category": "technical", "coach_owned": True,
              "scheduled_countdown_label": f"D-{d}"} for d in range(20, 13, -1)]
    role_map = {"weeks": [{"phase": "SPP", "session_roles": roles,
                           "calendar_days": [{"d_day": d} for d in range(20, 13, -1)]}]}
    enforce_total_session_budget(role_map, {"training_frequency": 6})
    assert len(roles) == 7
    errors = _total_session_budget_warnings({"athlete_snapshot": {"training_frequency": 6}, "weekly_role_map": role_map})
    assert errors[0]["code"] == "total_session_budget_exceeded"
    assert errors[0]["severity"] == "warning"


def test_historical_bank_replay_accepts_only_the_reviewed_round_edit():
    from tools.replay_combat_round_metadata import pre_combat_round_source
    source = (Path(__file__).resolve().parents[1] / "data/style_conditioning_bank.json").read_bytes()
    original = json.loads(pre_combat_round_source("data/style_conditioning_bank.json", source))
    pressure = next(r for r in original if r["name"] == "Pressure Combination Rounds")
    assert "round_based" not in pressure
    assert (pressure["rounds"], pressure["work_sec"], pressure["rest_sec"]) == (4, 120, 60)
    with pytest.raises(ValueError, match="Unreviewed"):
        pre_combat_round_source("data/style_conditioning_bank.json", source.replace(b'"round_based": true', b'"round_based": false'))


def assert_scheduled_doses_and_contact_safety(brief):
    role_map = brief["weekly_role_map"]
    for ordinal, week in enumerate(role_map["weeks"]):
        for role in week["session_roles"]:
            day = role_d_day(week, role)
            if role.get("category") not in {"strength", "conditioning"} or day is None:
                continue
            events = build_events(role_map, exclude_role=role)
            if day <= 13:
                # This PR changes normal allocation only. Finished-tail occupancy
                # stays with its owner; check its effective hard-contact protection
                # here and its actual doses below, alongside the existing tail suite.
                events = [event for event in events if event.profile.load_class == LoadClass.HARD_CONTACT]
            view = CalendarLegalityView(events=tuple(events), scope=week_scope(week, ordinal))
            profile = classify_role(role)
            assert profile is not None
            assert view.decision_for_profile(profile, day).allowed
            for member in role.get("selected_exercise_assignments") or []:
                assert member.get("slot_id") and member.get("source_phase")
                assert member.get("effective_prescription") or any(
                    dose.get("slot_id") == member["slot_id"] and dose.get("effective_prescription")
                    for dose in role.get("effective_strength_prescriptions") or []
                )


@pytest.mark.parametrize("earlier", [False, True], ids=["newer-lead", "earlier-lead"])
def test_both_saved_lead_constraints_retain_real_normal_power_and_explain_taper(monkeypatch, earlier):
    parsed, brief = run_lead(monkeypatch, earlier=earlier)
    assert parsed.training_frequency == 6  # Both saved intakes actually supplied six.
    assert brief["athlete_snapshot"]["plan_creation_weekday"] == "tuesday"
    evidence = collect_goal_evidence(brief)
    power = [e for e in evidence if "ballistic_power" in e["intents"] and e["d_day"] >= 14]
    # The six-session budget includes four coach appointments. Maintenance
    # must satisfy its actual fourteen-day windows, not add a GPP appointment
    # merely to satisfy a phase-label expectation.
    assert power
    # Power may be authored as repetitions or genuine full-rest timed bursts;
    # metres/seconds must not be relabelled as reps to satisfy the witness.
    assert all(e["effective_prescription"] and (
        (e.get("sets", 0) >= 1 and e.get("volume_amount", e.get("reps", 0)) >= 1
         and e.get("volume_unit", "reps") in {"reps", "sec"})
        or (e.get("rounds", 0) >= 2 and 0 < e.get("work_sec", 0) <= 10 and e.get("rest_sec", 0) >= 60)
    ) for e in power)
    assert any("energy_system_training" in e["intents"] for e in evidence)
    power_goal = next(e for e in brief["goal_preservation"] if e["goal"] == "power")
    assert power_goal["satisfied"], power_goal
    assert power_goal["missing_coverage"] == []
    assert any(2 <= e["d_day"] <= 15 for e in power_goal["evidence"])
    assert validate_goal_preservation(brief) == []
    if not earlier:
        workload = brief["athlete_snapshot"]["training_workload_context"]
        assert workload["reported_weekly_sessions"] == {"minimum": 8, "maximum": 9, "source": "training_notes"}
        assert workload["double_days_reported"] and "structured_frequency_conflict" not in workload
    assert_scheduled_doses_and_contact_safety(brief)
    for week in brief["weekly_role_map"]["weeks"]:
        normal_days = {d["weekday"] for d in week["calendar_days"] if d["d_day"] > 13}
        for day in {"thursday", "friday"} & normal_days:
            assert any(r["role_key"] == "light_combat_day" and r["scheduled_day_hint"].lower() == day
                       and r["coach_owned"] for r in week["session_roles"])


@pytest.mark.parametrize("frequency,expected", [(6, 6), (None, 6)])
def test_six_training_days_retain_legal_double_day_work_at_effective_doses(monkeypatch, frequency, expected):
    parsed, brief = run_lead(monkeypatch, training_frequency=frequency)
    assert parsed.training_frequency == expected
    assert_scheduled_doses_and_contact_safety(brief)
    evidence = collect_goal_evidence(brief)
    assert any("ballistic_power" in e["intents"] and e["d_day"] >= 14 for e in evidence)
    assert any("energy_system_training" in e["intents"] and e["d_day"] >= 14 for e in evidence)
    speed = next(e for e in brief["goal_preservation"] if e["goal"] == "speed")
    assert speed["satisfied"], speed
    assert validate_goal_preservation(brief) == []
    assert any(
        role_d_day(week, app) == role_d_day(week, coach)
        for week in brief["weekly_role_map"]["weeks"]
        for coach in week["session_roles"] if coach["role_key"] == "light_combat_day"
        for app in week["session_roles"] if app.get("category") == "conditioning"
        and app.get("selected_exercise_assignments")
    )


@pytest.mark.parametrize("case,overrides", [
    ("high-fatigue", {"fatigue": "high"}),
    ("injury", {"injuries": "moderate right knee pain, avoid jumping"}),
    ("three-contacts", {"hard_sparring_days": ["Monday", "Wednesday", "Saturday"]}),
    ("professional-low-load", {"status": "professional", "training_frequency": 6, "fatigue": "low",
                               "hard_sparring_days": [], "support_work_days": ["Tuesday", "Thursday", "Friday"]}),
    ("fight-week", {"plan_date": "2026-11-30"}),
])
def test_realistic_readiness_and_taper_restrictions_still_govern_the_schedule(monkeypatch, case, overrides):
    _, brief = run_lead(monkeypatch, **overrides)
    assert_scheduled_doses_and_contact_safety(brief)
    weeks = brief["weekly_role_map"]["weeks"]
    if case == "high-fatigue":
        assert any("high_fatigue" in (w.get("intentional_compression") or {}).get("reason_codes", []) for w in weeks)
    elif case == "injury":
        assert brief["restrictions"]
        assert brief["athlete_snapshot"]["injuries"]
        assert not any("jump" in member["name"].lower() or "bound" in member["name"].lower()
                       for w in weeks for r in w["session_roles"] for member in r.get("selected_exercise_assignments") or [])
    elif case == "three-contacts":
        assert any(len(w.get("effective_hard_sparring_days") or []) >= 3 for w in weeks)
        assert any(w.get("suppressed_roles") for w in weeks)
    elif case == "professional-low-load":
        assert all(not w.get("effective_hard_sparring_days") for w in weeks)
        assert any("ballistic_power" in e["intents"] for e in collect_goal_evidence(brief))
        assert validate_goal_preservation(brief) == []
    else:
        assert all(not w.get("effective_hard_sparring_days") for w in weeks)
        assert not any(e.get("development_quality") for e in collect_goal_evidence(brief) if e.get("d_day"))
        for w in weeks:
            for r in w["session_roles"]:
                for dose in r.get("effective_strength_prescriptions") or []:
                    assert dose.get("effective_max_sets", 1) <= 2


@pytest.mark.parametrize("notes,frequency,expected,reported,double", [
    ("I do about 8/ 9 sessions a week some are double days", None, 6, (8, 9), True),
    ("I train 8–9 times weekly, including double days", None, 6, (8, 9), True),
    ("I train 9 sessions per week", 6, 6, (9, 9), False),
    ("I work 8-9 hours. No double days", None, 6, None, False),
    ("I used to do 9 sessions per week", None, 6, None, False),
    ("I want to do 9 sessions per week", None, 6, None, False),
    ("", None, 6, None, False),
])
def test_note_cadence_is_explicit_and_availability_remains_a_distinct_fact(notes, frequency, expected, reported, double):
    fields = {"Training Availability": "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday",
              "Weekly Training Frequency": str(frequency or ""),
              "Are there any parts of your previous plan you hated or loved?": notes}
    parsed = PlanInput.from_payload({"data": {"fields": [{"label": k, "value": v} for k, v in fields.items()]}})
    assert parsed.training_frequency == expected
    assert len(parsed.training_days) == 6
    context = parsed.training_workload_context
    assert bool(context.get("double_days_reported")) == double
    if reported:
        assert (context["reported_weekly_sessions"]["minimum"], context["reported_weekly_sessions"]["maximum"]) == reported
    else:
        assert "reported_weekly_sessions" not in context
        assert parsed.parsing_metadata["training_frequency"]["source"] == "system_inferred"


@pytest.mark.parametrize("effective_count,active", [(0, False), (1, False), (2, False), (3, True)])
def test_crowded_policy_uses_resolved_contacts_after_declared_three_are_reduced(effective_count, active):
    athlete = {"sport": "boxing", "days_until_fight": 35, "fatigue": "moderate",
               "training_days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
               "hard_sparring_days": ["Monday", "Wednesday", "Saturday"]}
    state = _boxing_crowded_week_policy_state({"effective_hard_sparring_days": athlete["hard_sparring_days"][:effective_count]}, athlete)
    assert state["hard_spar_count"] == effective_count
    assert state["active"] == active


@pytest.mark.parametrize("technical_friday", [False, True])
def test_mixed_taper_window_never_uses_tail_ownership_to_excuse_missing_normal_work(technical_friday):
    week = {"week_index": 1, "phase": "SPP", "declared_training_days": ["Friday", "Saturday"],
            "calendar_days": [{"weekday": "friday", "d_day": 15}, {"weekday": "saturday", "d_day": 14}],
            "hard_sparring_plan": [{"day": "Saturday", "status": "convert_to_technical_suggested", "effective_load": "technical"}],
            "session_roles": [build_declared_light_combat_role("Friday", calendar_stress={"systemic": 2, "neural_mechanical": 2})] if technical_friday else [],
            "goal_repair_candidates": [{"category": "strength", "role_key": "primary_strength_day", "strength_session_index": 1}]}
    brief = {"athlete_snapshot": {"days_until_fight": 35}, "weekly_role_map": {"weeks": [week]}}
    constraints = _deferral_constraints({"goal": "power", "state": "maintain"}, brief, [{"min_d_day": 2, "max_d_day": 15}])
    assert not constraints  # D-14/15 are still owned by normal planning.
