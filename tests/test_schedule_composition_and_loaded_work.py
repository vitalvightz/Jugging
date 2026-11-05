"""F09: membership composed before the final calendar; F10: two meanings of "loaded".

Pipeline tests run the real Stage 1 entry point with a frozen clock and a fixed
seed; they make no model call.
"""
from __future__ import annotations

import collections
import copy
import datetime as dt
import json

import pytest

from fightcamp import input_parsing
from fightcamp.calendar_context import role_d_day
from fightcamp.late_camp_role_morph import loaded_work_permitted
from fightcamp.main import generate_plan_sync
from fightcamp.planner_authority_integrity import planner_authority_findings
from fightcamp.planner_context import planner_athlete_model_context
from fightcamp.prescription_resolver import apply_effective_strength_prescriptions
from fightcamp import session_composition
from fightcamp.calendar_integrity import _stamp_relocation
from fightcamp.combat_load_policy import PlacementDirective
from fightcamp.physical_session_frequency import is_physical_session_role
from fightcamp.session_composition import (
    _scheduled_phase_for_role,
    compose_normal_strength_assignments,
    reconcile_relocated_strength,
    stale_strength_roles,
)

_NOW = dt.datetime(2026, 1, 4, 12)
_FULL_GYM = ("barbell", "dumbbells", "kettlebells", "medicine_ball", "heavy_bag", "bodyweight",
             "pull_up_bar", "bands", "bench", "skipping_rope")
_SCENARIOS = {
    # Audit reproductions.
    "mma21_two_strength_on_d17": {"sport": "mma", "goals": ("strength", "speed"), "hard": ("Tuesday",),
                                  "target": "68"},
    # Unseeded: this scenario pins the no-move case, and a seed decides between
    # equally rated strength picks, some of which legitimately need relocating.
    "boxer56_novice_revalidation_move": {"days": 56, "frequency": 3, "target": "68",
                                         "goals": ("strength", "conditioning"), "seed": None},
    "boxer70_power_only_session_held": {"days": 70, "goals": ("strength", "power"), "target": "68",
                                        "weak": ("trunk_strength", "gas_tank"),
                                        "equipment": ("barbell", "dumbbells", "kettlebells", "sled", "yoke", "sandbag",
                                                      "bodyweight", "bands", "cable", "medicine_ball")},
    "muaythai28_cut_kb_swing_held": {"days": 28, "sport": "muay thai", "goals": ("power", "conditioning"),
                                     "weak": ("balance", "trunk_strength"), "frequency": 5, "weight": "70",
                                     "hard": ("Wednesday", "Saturday")},
    "boxer56_strength_on_glycolytic_day": {"days": 56},
    # Sport / camp sweep.
    "boxer21_injured_foot": {"guided": [{"area": "Right foot", "severity": "low", "trend": "stable",
                                         "injury_type": "pain", "timeframe": "last_week"}]},
    "wrestling42": {"days": 42, "sport": "wrestling", "goals": ("strength", "conditioning"),
                    "weak": ("gas_tank",), "hard": ("Tuesday", "Thursday"), "target": "68"},
    "kickboxing35": {"days": 35, "sport": "kickboxing", "goals": ("speed", "power"), "target": "68"},
    "bjj35_shoulder": {"days": 35, "sport": "bjj", "goals": ("strength", "mobility"), "target": "68",
                       "hard": ("Tuesday", "Thursday"),
                       "guided": [{"area": "Right shoulder", "severity": "low", "trend": "improving",
                                   "injury_type": "pain", "timeframe": "last_month"}]},
    "boxer35_bodyweight_freq5": {"days": 35, "equipment": ("bodyweight",), "frequency": 5,
                                 "goals": ("strength", "power"), "target": "68"},
    "kick10_late": {"days": 10, "sport": "kickboxing", "goals": ("speed",)},
}


def _payload(key):
    kw = {"days": 21, "sport": "boxing", "goals": ("speed", "conditioning"), "weak": ("trunk_strength",),
          "frequency": 4, "equipment": _FULL_GYM, "hard": ("Thursday",), "weight": "68", "target": "65",
          "guided": None, "seed": 7, **_SCENARIOS[key]}
    fields = {
        "Full name": "Regression", "Age": "24", "Weight (kg)": kw["weight"], "Target Weight (kg)": kw["target"],
        "Height (cm)": "175", "Stance": "Orthodox", "Fighting Style (Technical)": kw["sport"],
        "Professional Status": "amateur", "Current Record": "0-0", "Rounds x Minutes": "3x3",
        "Weekly Training Frequency": str(kw["frequency"]), "Fatigue Level": "low",
        "Equipment Access": ", ".join(kw["equipment"]),
        "Training Availability": "Monday, Tuesday, Wednesday, Thursday, Friday, Saturday",
        "Hard Sparring Days": ", ".join(kw["hard"]),
        "What are your key performance goals?": ", ".join(kw["goals"]), "Primary goal": kw["goals"][0],
        "Where do you feel weakest right now?": ", ".join(kw["weak"]), "Primary weak area": kw["weak"][0],
        "When is your next fight?": (_NOW.date() + dt.timedelta(days=kw["days"])).isoformat(),
        "Athlete Time Zone": "UTC",
    }
    data = {"data": {"fields": [{"label": k, "value": v} for k, v in fields.items()]}, "random_seed": kw["seed"]}
    if kw["guided"]:
        data["guided_injuries"] = kw["guided"]
    return data


def _generate(key):
    mp = pytest.MonkeyPatch()
    mp.setattr(input_parsing, "_utc_now", lambda: _NOW)
    try:
        return generate_plan_sync(_payload(key))["planning_brief"]
    finally:
        mp.undo()


_CACHE: dict[str, dict] = {}


def _plan(key):
    if key not in _CACHE:
        _CACHE[key] = _generate(key)
    return _CACHE[key]


def _roles(brief):
    if "late_fight_session_sequence" in brief:
        return [({}, r) for r in brief["late_fight_session_sequence"]]
    return [(w, r) for w in brief["weekly_role_map"]["weeks"] for r in w["session_roles"]]


def _normal_strength(week):
    return [r for r in week["session_roles"] if r.get("category") == "strength" and not r.get("late_fight_tail_owned")]


def _later_additions(role):
    """Exercises legitimately added after composition: an applied standalone
    minimum and a goal-preservation microdose."""
    standalone = (role.get("strength_composition_policy") or {}).get("standalone_minimum") or {}
    return {standalone.get("exercise") if standalone.get("status") == "applied" else None,
            (role.get("priority_microdose") or {}).get("name")}


# ---------------------------------------------------------------------------
# F09 - membership agrees with the final calendar
# ---------------------------------------------------------------------------

_NORMAL = sorted(k for k in _SCENARIOS if k != "kick10_late")


@pytest.mark.parametrize("key", _NORMAL)
def test_shipped_membership_is_what_the_composer_gives_the_final_calendar(key):
    brief = _plan(key)
    fresh = copy.deepcopy(brief["weekly_role_map"])
    token = planner_athlete_model_context.set(brief["athlete_snapshot"])
    try:
        compose_normal_strength_assignments(weekly_role_map=fresh, candidate_pools=brief["candidate_pools"])
        apply_effective_strength_prescriptions(weekly_role_map=fresh, candidate_pools=brief["candidate_pools"],
                                               athlete_model=brief["athlete_snapshot"])
    finally:
        planner_athlete_model_context.reset(token)
    for week, fresh_week in zip(brief["weekly_role_map"]["weeks"], fresh["weeks"]):
        for role, fresh_role in zip(_normal_strength(week), _normal_strength(fresh_week)):
            extra = _later_additions(role)
            shipped = [m["name"] for m in role["selected_exercise_assignments"] if m["name"] not in extra]
            final = [m["name"] for m in fresh_role.get("selected_exercise_assignments") or [] if m["name"] not in extra]
            if role.get("priority_microdose"):
                # A microdose host keeps only what the floor left beside it.
                assert set(shipped) <= set(final), (role["role_key"], role_d_day(week, role))
            else:
                assert shipped == final, (role["role_key"], role_d_day(week, role))


@pytest.mark.parametrize("key", _NORMAL)
def test_every_strength_role_was_composed_and_dosed_for_the_day_it_is_on(key):
    """Covers composer-, governor- and revalidation-moved roles alike (the
    boxer56/70 and kickboxing35 camps include composer relocations)."""
    brief = _plan(key)
    assert stale_strength_roles(brief["weekly_role_map"]) == set()
    for week in brief["weekly_role_map"]["weeks"]:
        for role in _normal_strength(week):
            envelope = role.get("effective_strength_envelope") or {}
            if "scheduled_d_day" in envelope:
                assert envelope["scheduled_d_day"] == role_d_day(week, role)


def _physical_days(week):
    return {role_d_day(week, r) for r in week["session_roles"] if is_physical_session_role(r)}


def _moved_copy(key):
    """A finished map with one strength role moved to a free day of its week
    after its membership was chosen, as any mover would leave it."""
    brief = _plan(key)
    role_map = copy.deepcopy(brief["weekly_role_map"])
    for week in role_map["weeks"]:
        # This is a controlled mover fixture. More compatible appointments now
        # survive production allocation; reserve room for the two forced moves
        # without changing the resolved coach/contact calendar.
        week["session_roles"] = [r for r in week["session_roles"]
                                 if r.get("category") not in {"conditioning", "recovery", "support_insert"}]
        busy = _physical_days(week)
        free = [d for d in week["calendar_days"] if d["d_day"] not in busy and d["d_day"] > 14]
        strength = _normal_strength(week)
        if strength and len(free) >= 3:
            role = strength[0]
            _stamp_relocation(role, weekday=free[0]["weekday"], d_day=free[0]["d_day"],
                              reason_code="test_move", directive=PlacementDirective.ALLOW)
            return brief, role_map, week, role
    raise AssertionError("no movable strength role")


def _with_athlete(brief, fn):
    token = planner_athlete_model_context.set(brief["athlete_snapshot"])
    try:
        return fn()
    finally:
        planner_athlete_model_context.reset(token)


def test_a_role_moved_after_composition_is_recomposed_for_its_destination():
    brief, role_map, week, role = _moved_copy("boxer56_novice_revalidation_move")
    assert stale_strength_roles(role_map) == {id(role)}
    _with_athlete(brief, lambda: reconcile_relocated_strength(
        role_map, candidate_pools=brief["candidate_pools"], replace_callback=lambda m: None))
    assert stale_strength_roles(role_map) == set()
    assert role["strength_composition_policy"]["composed_for_d_day"] == role_d_day(week, role)


def test_a_second_move_is_reconciled_without_another_round(monkeypatch):
    """Recomposition relocates (forced here), the governor re-run moves the role
    again; the bounded final pass composes it for where it ended up and moves
    nothing further."""
    brief, role_map, week, role = _moved_copy("boxer56_novice_revalidation_move")
    real = session_composition.compose_normal_strength_assignments
    calls = []

    def compose(**kwargs):
        calls.append(kwargs.get("allow_relocation", True))
        out = real(**kwargs)
        if len(calls) == 1:  # the composer's own mover relocates it
            day = next(d for d in week["calendar_days"] if d["d_day"] not in _physical_days(week) and d["d_day"] > 14)
            _stamp_relocation(role, weekday=day["weekday"], d_day=day["d_day"], reason_code="mover", directive=PlacementDirective.ALLOW)
        return out

    composed_for = role_d_day(week, role)

    def governor(role_map):  # and the governor moves it once more, to a new day
        governor.calls += 1
        taken = _physical_days(week) | {composed_for}
        day = next(d for d in week["calendar_days"] if d["d_day"] not in taken and d["d_day"] > 14)
        _stamp_relocation(role, weekday=day["weekday"], d_day=day["d_day"], reason_code="governor", directive=PlacementDirective.ALLOW)
    governor.calls = 0

    monkeypatch.setattr(session_composition, "compose_normal_strength_assignments", compose)
    _with_athlete(brief, lambda: reconcile_relocated_strength(
        role_map, candidate_pools=brief["candidate_pools"], replace_callback=governor))
    final_day = role_d_day(week, role)
    assert final_day != composed_for
    assert governor.calls == 1 and calls == [True, False]
    assert stale_strength_roles(role_map) == set()
    assert role["strength_composition_policy"]["composed_for_d_day"] == final_day


@pytest.mark.parametrize("key", _NORMAL)
def test_membership_comes_from_the_scheduled_phase(key):
    brief = _plan(key)
    for week in brief["weekly_role_map"]["weeks"]:
        for role in _normal_strength(week):
            phase = _scheduled_phase_for_role(week, role) or week.get("phase")
            assert {m.get("source_phase") for m in role["selected_exercise_assignments"]} <= {phase}


@pytest.mark.parametrize("key", _NORMAL)
def test_no_strength_stacking_or_duplicated_exercise(key):
    brief = _plan(key)
    for week in brief["weekly_role_map"]["weeks"]:
        strength = _normal_strength(week)
        days = collections.Counter(role_d_day(week, r) for r in strength)
        assert all(n == 1 for n in days.values()), days
        names = collections.Counter(m["name"] for r in strength for m in r["selected_exercise_assignments"])
        assert all(n == 1 for n in names.values()), names


def test_the_d17_stack_is_resolved_onto_a_free_day():
    brief = _plan("mma21_two_strength_on_d17")
    week = brief["weekly_role_map"]["weeks"][0]
    strength_days = sorted(role_d_day(week, r) for r in _normal_strength(week))
    assert len(strength_days) == len(set(strength_days)) == 2


def test_revalidation_does_not_force_a_move_when_final_doses_are_legal():
    """With the requested three-session budget, surplus secondary SPP sessions
    are absent. Every surviving dose is legal; do not manufacture a relocation.
    Actual mover/recomposition paths are exercised by the preceding tests.
    """
    from fightcamp.calendar_context import CalendarLegalityView, build_events, week_scope
    from fightcamp.physical_session_frequency import appointment_budget_exceeded
    brief = _plan("boxer56_novice_revalidation_move")
    actions = (brief["weekly_role_map"].get("realised_load_revalidation") or {}).get("actions") or []
    assert actions == []
    day_overrun = appointment_budget_exceeded(brief["weekly_role_map"], 3)
    assert any("requested day count exceeded" in flag for w in brief["weekly_role_map"]["weeks"]
               for flag in w.get("coach_note_flags", [])) == day_overrun
    assert all(g["satisfied"] for g in brief["goal_preservation"])
    for index, week in enumerate(brief["weekly_role_map"]["weeks"]):
        for role in _normal_strength(week):
            day = role_d_day(week, role)
            view = CalendarLegalityView(events=tuple(build_events(brief["weekly_role_map"], exclude_role=role)),
                                        scope=week_scope(week, index))
            assert view.decision_for_role(role, day).directive is PlacementDirective.ALLOW
            assert role["strength_composition_policy"]["composed_for_d_day"] == day
            assert role.get("effective_strength_prescriptions")


def test_strength_no_longer_lands_on_the_glycolytic_day_week_after_week():
    brief = _plan("boxer56_strength_on_glycolytic_day")
    shared = [
        role_d_day(w, r) for w in brief["weekly_role_map"]["weeks"] for r in _normal_strength(w)
        if any(x.get("preferred_system") == "glycolytic" and role_d_day(w, x) == role_d_day(w, r)
               for x in w["session_roles"])
    ]
    assert shared == []


def test_generation_is_deterministic():
    first = _plan("mma21_two_strength_on_d17")
    again = _generate("mma21_two_strength_on_d17")
    assert json.dumps(first["weekly_role_map"], sort_keys=True, default=str) == json.dumps(
        again["weekly_role_map"], sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# F10 - one meaning of "loaded"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", sorted(_SCENARIOS))
def test_no_plan_is_held_for_loaded_work_and_every_envelope_matches_its_cap(key):
    brief = _plan(key)
    assert not [f for f in planner_authority_findings(brief) if f["code"] == "selected_loaded_exercise_forbidden"]
    for _, role in _roles(brief):
        envelope = role.get("effective_strength_envelope")
        if isinstance(envelope, dict) and isinstance(role.get("strength_dose_cap"), dict):
            assert envelope["loaded_allowed"] is loaded_work_permitted(role)


def test_a_power_only_session_on_a_loaded_day_is_not_told_to_drop_loading():
    # #3101/#3104 changed ranked membership. Keep this specific power-only
    # regression by choosing the real published Thruster alternate explicitly,
    # then run the actual dose resolver on the generated calendar and caps.
    brief = copy.deepcopy(_plan("boxer70_power_only_session_held"))
    slot = next(s for s in brief["candidate_pools"]["SPP"]["strength_slots"]
                if any(a["name"] == "Barbell Thruster" for a in s["alternates"]))
    alternate = next(a for a in slot["alternates"] if a["name"] == "Barbell Thruster")
    slot_id = f'{slot["slot_id"]}:alternate:{slot["alternates"].index(alternate)}'
    for _, role in _roles(brief):
        if role["role_key"] == "neural_plus_strength_day":
            role["selected_exercise_assignments"] = [{
                "slot_id": slot_id, "name": alternate["name"], "source_phase": "SPP",
                "slot_group": "strength_slots", "base_prescription": alternate["prescription"],
            }]
    apply_effective_strength_prescriptions(
        weekly_role_map=brief["weekly_role_map"], candidate_pools=brief["candidate_pools"],
        athlete_model=brief["athlete_snapshot"],
    )
    roles = [r for _, r in _roles(brief) if r["role_key"] == "neural_plus_strength_day"
             and "Barbell Thruster" in {m["name"] for m in r["selected_exercise_assignments"]}]
    assert roles
    for role in roles:
        assert loaded_work_permitted(role)
        assert role["effective_strength_envelope"].get("loaded_allowed") is not False
        assert role["effective_strength_prescriptions"][0]["name"] == "Barbell Thruster"
        assert role["effective_strength_prescriptions"][0]["effective_prescription"]


def test_a_capped_no_load_day_states_its_permission():
    brief = _plan("boxer21_injured_foot")
    tail = [r for _, r in _roles(brief) if r.get("late_fight_tail_owned") and isinstance(r.get("strength_dose_cap"), dict)
            and r["strength_dose_cap"]["loaded_allowed"] is False and isinstance(r.get("effective_strength_envelope"), dict)]
    assert tail and all(r["effective_strength_envelope"]["loaded_allowed"] is False for r in tail)


def test_loaded_work_permission_reads_the_cap():
    assert loaded_work_permitted({}) is True
    assert loaded_work_permitted({"strength_dose_cap": {"loaded_allowed": True}}) is True
    assert loaded_work_permitted({"strength_dose_cap": {"loaded_allowed": False}}) is False


def _single_role_brief(name, *, cap_allows, envelope_allows=None):
    role = {"role_key": "neural_plus_strength_day", "category": "strength", "scheduled_countdown_label": "D-10",
            "strength_dose_cap": {"max_sets": 2, "max_reps": 3, "loaded_allowed": cap_allows},
            "selected_exercise_assignments": [{"name": name, "slot_group": "strength_slots", "source_phase": "SPP"}]}
    if envelope_allows is not None:
        role["effective_strength_envelope"] = {"loaded_allowed": envelope_allows}
    return {"athlete_snapshot": {"phase_weeks": {"days": {"SPP": 26}}},
            "weekly_role_map": {"weeks": [{"phase": "SPP", "session_roles": [role]}]}}


def _codes(brief):
    return {f["code"] for f in planner_authority_findings(brief)}


@pytest.mark.parametrize("name", ["Barbell Thruster", "Back Squat"])
def test_externally_loaded_work_is_blocked_only_where_the_day_forbids_it(name):
    assert "selected_loaded_exercise_forbidden" in _codes(_single_role_brief(name, cap_allows=False))
    assert "selected_loaded_exercise_forbidden" not in _codes(_single_role_brief(name, cap_allows=True))


def test_a_forbidding_envelope_without_a_forbidding_cap_still_blocks():
    """Missing or contradictory evidence is never read as permission."""
    brief = _single_role_brief("Barbell Thruster", cap_allows=True, envelope_allows=False)
    assert "selected_loaded_exercise_forbidden" in _codes(brief)


def test_bodyweight_power_is_not_external_load():
    assert "selected_loaded_exercise_forbidden" not in _codes(_single_role_brief("Clap Push-Up", cap_allows=False))


def test_a_strength_member_without_load_metadata_is_held_on_a_no_load_day():
    """Unknown load is not zero load."""
    assert "selected_loaded_exercise_forbidden" in _codes(_single_role_brief("Unknown Lift", cap_allows=False))
    assert "selected_loaded_exercise_forbidden" not in _codes(_single_role_brief("Unknown Lift", cap_allows=True))
