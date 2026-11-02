"""Late-fight core maintenance: D-13..D-7 keeps a small, low-fatigue trunk
stimulus inside one existing tail strength session a week; D-14+ keeps the
Core Development Block unchanged and D-6 inward carries no core programming.

Pipeline tests run the real Stage 1 entry point with a frozen clock and fixed
seed (no model call), reusing the goal/weakness delivery fixtures.
"""
from __future__ import annotations

import pytest

import test_core_development_block as core
import test_goal_weakness_delivery as delivery
from api.exercise_identity import reconcile_exercise_keys
from fightcamp.calendar_context import role_d_day
from fightcamp.late_camp_role_morph import late_fight_strength_dose_cap
from fightcamp.late_selector_windows import late_window_allowed
from fightcamp.session_composition import (
    LATE_CORE_MAINTENANCE_MINIMUM,
    LATE_CORE_MAINTENANCE_TARGET,
    TRUNK_BLOCK_GROUP,
    late_core_maintenance_additions,
    late_core_maintenance_ineligibility,
)
from fightcamp.strength_session_quality import trunk_function, trunk_high_cost, trunk_load_tier

_brief, _roles, _members, _BANK = delivery._brief, delivery._roles, delivery._members, delivery._BANK
_SCENARIO = "boxer28_trunk_fullgym"  # 28-day boxer, trunk strength as primary weakness
_CORE_CUT = {"goals": ("speed", "conditioning"), "weak": ("trunk_strength",), "weight": "72", "target": "66"}


@pytest.fixture(scope="module")
def without_maintenance():
    """The same plan generated with maintenance switched off (never mutated:
    tests that change it work on a deep copy)."""
    import fightcamp.stage2_payload as payload

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(payload, "_apply_late_core_maintenance", lambda *args, **kwargs: None)
        return delivery._generate(_SCENARIO)["planning_brief"]


def _reapply(brief, host_members):
    """Run maintenance alone on a copy of ``brief`` whose D-13 host holds ``host_members``."""
    import copy
    from fightcamp.planner_context import planner_athlete_model_context
    from fightcamp.stage2_payload import _apply_late_core_maintenance

    brief = copy.deepcopy(brief)
    host = next(r for w, r in _tail_strength(brief) if role_d_day(w, r) == 13)
    host["selected_exercise_assignments"] = copy.deepcopy(host_members)
    tail = [r for _, r in _roles(brief) if r.get("late_fight_tail_owned")]
    token = planner_athlete_model_context.set(brief["athlete_snapshot"])
    try:
        _apply_late_core_maintenance(brief["weekly_role_map"], tail, brief["candidate_pools"], brief["athlete_snapshot"])
    finally:
        planner_athlete_model_context.reset(token)
    return host


def _maintenance(role) -> dict:
    return role.get("late_core_maintenance") or {}


def _maintenance_members(role) -> list[dict]:
    names = set(_maintenance(role).get("members") or [])
    return [m for m in _members(role) if m["name"] in names]


def _tail_strength(brief):
    return [(week, role) for week, role in _roles(brief)
            if role.get("late_fight_tail_owned") and role.get("category") == "strength"]


def _host(brief):
    return next((week, role) for week, role in _tail_strength(brief) if _maintenance(role).get("status") == "delivered")


# ---------------------------------------------------------------------------
# 1. D-14+ development is unchanged
# ---------------------------------------------------------------------------

def test_development_blocks_before_d14_are_unchanged():
    """The 28-day boxer keeps the exact Core Development Blocks it had before."""
    blocks = {role_d_day(week, role): core._block(role)["members"] for week, role in core._strength_roles(_brief(_SCENARIO))
              if core._block(role).get("status") == "delivered"}
    assert blocks == {
        22: ["Banded Pallof (Walking)", "Plank", "Seated Medicine Ball Rotation"],
        19: ["Med-Ball Rotational Slam", "Seated Medicine Ball Rotation", "Slider Body Saw"],
    }
    assert not any("late_core_maintenance" in role for _, role in _roles(_brief(_SCENARIO))
                   if not role.get("late_fight_tail_owned"))


def test_development_never_draws_on_maintenance_candidates():
    brief = _brief(_SCENARIO)
    maintenance = {slot["selected"]["name"] for pool in brief["candidate_pools"].values()
                   for slot in pool.get("core_development_slots") or [] if slot["role"] == "core_maintenance"}
    assert maintenance
    for week, role in _roles(brief):
        if role_d_day(week, role) is not None and role_d_day(week, role) >= 14:
            assert not maintenance & {m["name"] for m in _members(role)}


# ---------------------------------------------------------------------------
# 2. D-13..D-7: one eligible tail session receives 2-3 complementary members
# ---------------------------------------------------------------------------

def test_week_three_keeps_complementary_low_fatigue_core_maintenance():
    brief = _brief(_SCENARIO)
    week, role = _host(brief)
    d_day = role_d_day(week, role)
    assert 7 <= d_day <= 13 and role["role_key"] == "strength_touch_day"
    members = _maintenance_members(role)
    names = [m["name"] for m in members]
    assert LATE_CORE_MAINTENANCE_MINIMUM <= len(members) <= LATE_CORE_MAINTENANCE_TARGET, names
    functions = [trunk_function(_BANK[name]) for name in names]
    assert len(set(functions)) == len(functions), functions  # complementary, not two planks
    tiers = [trunk_load_tier(_BANK[name]) for name in names]
    assert "rotational_power" not in tiers and tiers.count("loaded") <= 1, tiers
    assert not any(trunk_high_cost(_BANK[name]) for name in names)
    for name in names:
        # Verified bank items the bank itself opts into this countdown window.
        assert "trunk_strength" in delivery.strength_adaptation_relevance(_BANK[name])
        assert late_window_allowed([_BANK[name]], offset=d_day)
    # Dosed by the scheduled-day resolver, inside the countdown cap.
    cap = late_fight_strength_dose_cap(d_day)
    priced = {p["name"]: p for p in role["effective_strength_prescriptions"]}
    for name in names:
        dose = priced[name]
        assert dose["dose_authority"] == "scheduled_countdown_overlay" and dose["effective_prescription"]
        assert dose["effective_max_sets"] <= cap["max_sets"]
    # Only one session a week carries it.
    hosts = [r for w, r in _tail_strength(brief) if w is week and _maintenance(r).get("status") == "delivered"]
    assert hosts == [role]


def test_goal_preservation_is_unchanged_by_maintenance():
    """The late tail is outside every trunk coverage window: maintenance neither
    satisfies nor excuses a development window, and the entry is not enlarged
    (it reaches Stage 2's size-budgeted context through compressed priorities)."""
    entry = delivery._entry(_brief(_SCENARIO), "trunk_strength")
    assert entry["satisfied"] and entry["state"] == "build"
    assert all(e["d_day"] >= 14 for e in entry["evidence"])
    assert "late_core_maintenance" not in entry


def test_maintenance_selection_is_complementary_and_budgeted():
    records = core._synthetic(["Plank", "Bird Dog Hold", "Dead-bug breathing", "Suitcase Carry Holds",
                               "Kettlebell Suitcase Carry Hold", "Med-Ball Rotational Slam"])
    picks = late_core_maintenance_additions(records, loaded_allowed=True)
    names = [r["name"] for r in picks]
    assert 2 <= len(names) <= 3 and "Med-Ball Rotational Slam" not in names
    assert [trunk_load_tier(_BANK[n]) for n in names].count("loaded") <= 1
    assert len({trunk_function(_BANK[n]) for n in names}) == len(names)
    # A no-load countdown day (D-7) admits no resisted member at all.
    unloaded = late_core_maintenance_additions(records, loaded_allowed=False)
    assert unloaded is None or all(trunk_load_tier(_BANK[r["name"]]) == "stability" for r in unloaded)
    # One function is not maintenance of the trunk: nothing is forced.
    assert late_core_maintenance_additions(core._synthetic(["Plank", "Dead-bug breathing"]),
                                           loaded_allowed=True) is None


# ---------------------------------------------------------------------------
# 3. D-6 onward: no core programming
# ---------------------------------------------------------------------------

def _tail_ctx(d_day, *, weekday="monday", hard=("thursday",), **role_overrides):
    week = {"phase": "SPP", "effective_hard_sparring_days": list(hard),
            "calendar_days": [{"d_day": d_day, "weekday": weekday}]}
    role = {"role_key": "strength_touch_day", "category": "strength", "late_fight_tail_owned": True,
            "scheduled_day_hint": weekday, "scheduled_countdown_label": f"D-{d_day}",
            "selected_exercise_assignments": [{"name": "Primer"}], **role_overrides}
    return week, role


@pytest.mark.parametrize("d_day,reason", [(6, "fight_proximity"), (5, "fight_proximity"), (1, "fight_proximity"),
                                          (0, "fight_proximity"), (14, "development_window"),
                                          (21, "development_window")])
def test_maintenance_window_is_d13_to_d7(d_day, reason):
    week, role = _tail_ctx(d_day)
    assert late_core_maintenance_ineligibility(week, role, {}) == {"reason_code": reason}


def test_an_undated_session_is_reported_as_unscheduled():
    week, role = _tail_ctx(10)
    week["calendar_days"] = []
    role.pop("scheduled_countdown_label")
    role.pop("scheduled_day_hint")
    assert late_core_maintenance_ineligibility(week, role, {}) == {"reason_code": "unscheduled"}


@pytest.mark.parametrize("d_day", [13, 10, 7])
def test_maintenance_window_admits_d13_through_d7(d_day):
    week, role = _tail_ctx(d_day)
    assert late_core_maintenance_ineligibility(week, role, {}) is None


def test_no_core_work_inside_d6():
    for key in (_SCENARIO, "core_goal", "king_no_cut"):
        for week, role in _roles(_brief(key)):
            d_day = role_d_day(week, role)
            if d_day is not None and d_day <= 6:
                assert not _maintenance_members(role), key
                assert _maintenance(role).get("status") in {None, "not_eligible"}


# ---------------------------------------------------------------------------
# 4. Hard sparring, injury, fatigue and weight-cut pressure suppress it
# ---------------------------------------------------------------------------

def test_hard_sparring_today_or_tomorrow_suppresses_maintenance():
    for weekday in ("thursday", "wednesday"):
        week, role = _tail_ctx(10, weekday=weekday)
        assert late_core_maintenance_ineligibility(week, role, {}) == {
            "reason_code": "hard_sparring_priority", "source": "hard_sparring_adjacent"}


@pytest.mark.parametrize("pressure,source", [
    ({"fatigue_pressure": 2}, "high_fatigue"),
    ({"cut_pressure": 1}, "weight_cut_pressure"),
    ({"injury_pressure": 1}, "injury_constraint"),
])
def test_readiness_pressure_suppresses_maintenance(pressure, source):
    week, role = _tail_ctx(12)
    assert late_core_maintenance_ineligibility(week, role, pressure) == {
        "reason_code": "readiness_capacity", "pressure_sources": [source]}


def test_coach_owned_or_empty_sessions_never_host():
    for overrides in ({"coach_owned": True}, {"selected_exercise_assignments": []}):
        week, role = _tail_ctx(12, **overrides)
        assert late_core_maintenance_ineligibility(week, role, {})["reason_code"] == "no_eligible_host"


@pytest.mark.parametrize("key,source", [("core_high_fatigue", "high_fatigue"), ("core_cut", "weight_cut_pressure")])
def test_pipeline_records_why_maintenance_was_withheld(key, source, monkeypatch):
    monkeypatch.setitem(delivery._EXPOSURE_FORMS, "core_cut", _CORE_CUT)
    brief = _brief(key)
    outcomes = [_maintenance(role) for week, role in _tail_strength(brief) if 7 <= role_d_day(week, role) <= 13]
    assert outcomes and all(o["status"] == "not_eligible" for o in outcomes), outcomes
    assert any(source in (o.get("pressure_sources") or []) for o in outcomes), outcomes
    assert not any(_maintenance_members(role) for _, role in _tail_strength(brief))


# ---------------------------------------------------------------------------
# 5. No duplicates, new sessions, cap breaches or extra stress
# ---------------------------------------------------------------------------

def test_maintenance_adds_members_only_inside_the_existing_session(without_maintenance):
    """Same calendar with and without maintenance. The host keeps its lead and
    any meaningful work in order; only support-only primer complements may give
    way (recorded), and its realised stress is no higher."""
    with_maintenance, without = _brief(_SCENARIO), without_maintenance

    def calendar(brief):
        return [(w.get("week_index"), role_d_day(w, r), r.get("role_key")) for w, r in _roles(brief)]

    assert calendar(with_maintenance) == calendar(without)
    week, host = _host(with_maintenance)
    before = next(r for w, r in _roles(without)
                  if (w.get("week_index"), role_d_day(w, r), r.get("role_key"))
                  == (week.get("week_index"), role_d_day(week, host), host.get("role_key")))
    original = _members(before)
    added = [m["name"] for m in _maintenance_members(host)]
    kept = [m["name"] for m in _members(host) if m["name"] not in added]
    displaced = [m for m in original if m["name"] not in kept]
    assert [m["name"] for m in _members(host)] == kept + added  # original order, core last
    assert kept[0] == original[0]["name"]  # the primer's lead stays
    assert all(m.get("primer_complement") and m.get("support_only") is True for m in displaced)
    assert [m["name"] for m in displaced] == _maintenance(host).get("displaced_primer_complements", [])
    assert all(m["name"] in kept for m in original if not m.get("support_only"))  # meaningful work stays
    # No duplicate exercise anywhere on that day.
    day = [m["name"] for w, r in _roles(with_maintenance) if w is week and role_d_day(w, r) == role_d_day(week, host)
           for m in _members(r)]
    assert len(day) == len(set(day))
    # The composed session stays at or below its own realised load.
    for axis in ("systemic", "neural_mechanical"):
        assert host["calendar_stress"][axis] <= before["calendar_stress"][axis]


@pytest.mark.parametrize("key", [_SCENARIO, "king_no_cut", "core_goal", "muaythai28_power_trunk"])
def test_every_late_primer_session_respects_the_member_cap(key):
    from fightcamp.stage2_payload import LATE_PRIMER_MAX_MEMBERS, LATE_PRIMER_ROLE_KEYS

    for _, role in _roles(_brief(key)):
        if role.get("late_fight_tail_owned") and role.get("role_key") in LATE_PRIMER_ROLE_KEYS:
            assert len(_members(role)) <= LATE_PRIMER_MAX_MEMBERS, (key, [m["name"] for m in _members(role)])


def test_maintenance_is_withheld_when_only_the_cap_could_make_room(without_maintenance):
    """A full primer with no support-only complement to stand in for keeps every
    member; maintenance is withheld with the cap as its reason."""
    from fightcamp.stage2_payload import LATE_PRIMER_MAX_MEMBERS

    original = next(r for w, r in _tail_strength(without_maintenance) if role_d_day(w, r) == 13)
    full = [{**m, "meaningful_stress": True} for m in _members(original)]
    assert len(full) == LATE_PRIMER_MAX_MEMBERS
    host = _reapply(without_maintenance, full)
    assert host["selected_exercise_assignments"] == full
    assert _maintenance(host) == {"intent": "maintenance", "status": "constrained",
                                  "reason_code": "primer_member_cap"}


def _trunk_assignment(brief, name):
    from fightcamp.session_composition import assignment_from_slot

    for phase, pool in brief["candidate_pools"].items():
        for slot in [*(pool.get("strength_slots") or []), *(pool.get("core_development_slots") or [])]:
            if slot["selected"]["name"] == name:
                return {**assignment_from_slot(phase, "strength_slots", slot), "phase": phase}
    raise AssertionError(name)


def test_trunk_work_the_session_already_holds_spends_the_maintenance_budget(without_maintenance):
    """A ballistic rotational trunk lead already exceeds the maintenance budget:
    no core is added on top of it, and the reason names the load limit."""
    original = next(r for w, r in _tail_strength(without_maintenance) if role_d_day(w, r) == 13)
    members = [_trunk_assignment(without_maintenance, "Med-Ball Rotational Slam"), *_members(original)[:1]]
    host = _reapply(without_maintenance, members)
    assert [m["name"] for m in _members(host)] == [m["name"] for m in members]
    assert _maintenance(host) == {"intent": "maintenance", "status": "constrained",
                                  "reason_code": "trunk_load_limits"}


def test_held_trunk_functions_are_not_repeated():
    held = core._synthetic(["Bird Dog Hold"])
    records = core._synthetic(["Plank", "Controlled Bird-Dog", "Dead-bug breathing", "Suitcase Carry Holds"])
    picks = late_core_maintenance_additions(records, loaded_allowed=True, held=held)
    functions = [trunk_function(_BANK[r["name"]]) for r in picks]
    assert "anti_rotation" not in functions and len(set(functions)) == len(functions)
    # A held resisted member spends the single loaded allowance.
    loaded_held = core._synthetic(["Suitcase Carry Holds"])
    picks = late_core_maintenance_additions(records, loaded_allowed=True, held=loaded_held) or []
    assert all(trunk_load_tier(_BANK[r["name"]]) == "stability" for r in picks)
    # Ballistic rotational work already in the session: nothing is added.
    assert late_core_maintenance_additions(records, loaded_allowed=True,
                                           held=core._synthetic(["Med-Ball Rotational Slam"])) is None


# ---------------------------------------------------------------------------
# 6. Stage 2 receives no grouping signal
# ---------------------------------------------------------------------------

def test_maintenance_members_carry_no_grouping():
    _, role = _host(_brief(_SCENARIO))
    for member in _maintenance_members(role):
        assert not {"component_group", "trunk_function", "exercise_cap_exempt"} & set(member)


def test_stage2_requests_carry_no_maintenance_or_grouping_signal(monkeypatch):
    import asyncio
    from support import FakeOpenAIClient
    from api.stage2_automation import OpenAIStage2Automator

    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_REPAIR", "1")
    stage1 = delivery._generate(_SCENARIO)
    brief = stage1["planning_brief"]
    _, host = _host(brief)
    plan_text = stage1.get("plan_text") or ""
    markers = (*core._STAGE2_MARKERS, "late_core_maintenance", "core_maintenance")

    def assert_clean(calls):
        sent = core.json.dumps([call.get("input") for call in calls], default=str)
        for marker in markers:
            assert marker not in sent, marker
        # The members themselves reach the model as ordinary closed exercises.
        for member in _maintenance_members(host):
            assert member["name"] in sent

    client = FakeOpenAIClient([core._model_response(plan_text)] * 6)
    asyncio.run(OpenAIStage2Automator(client=client, model="m").finalize(stage1_result=stage1))
    assert client.responses.calls
    assert_clean(client.responses.calls)

    client = FakeOpenAIClient([core._model_response("{}"), core._model_response("{}")])
    rendered = plan_text + "\n" + "\n".join(f"- {m['name']}" for m in _members(host))
    asyncio.run(OpenAIStage2Automator(client=client, model="m")._generate_structured_outcome(
        final_plan_text=rendered, planning_brief=brief, source="test", costs=[]))
    assert len(client.responses.calls) == 2
    assert_clean(client.responses.calls)


# ---------------------------------------------------------------------------
# 7. Identity, logging and rendering stay intact
# ---------------------------------------------------------------------------

def test_maintenance_members_keep_identity_and_render_as_ordinary_exercises():
    brief = _brief(_SCENARIO)
    week, role = _host(brief)
    for member in _maintenance_members(role):
        assert member["exercise_key"] and member["slot_id"] and member["source_phase"]
    day = next(d for d in week["calendar_days"] if d["d_day"] == role_d_day(week, role))
    blocks = [{"block_type": "strength", "display_name": m["name"]} for m in _members(role)]
    plan = {"weeks": [{"days": [{"date": day.get("date"), "countdown_label": f"D-{day['d_day']}",
                                 "sessions": [{"title": role.get("athlete_facing_label") or "Strength",
                                               "blocks": blocks}]}]}]}
    rendered = reconcile_exercise_keys(plan, brief)["weeks"][0]["days"][0]["sessions"][0]["blocks"]
    assert len(rendered) == len(blocks)
    keys = {m["name"]: m["exercise_key"] for m in _members(role)}
    for block in rendered:
        assert block.get("exercise_key") == keys[block["display_name"]]  # loggable by its own identity
        assert TRUNK_BLOCK_GROUP not in (block.get("tags") or [])


def test_unrelated_programmes_carry_no_maintenance():
    brief = _brief("boxer28_strength_fullgym")
    assert not any("late_core_maintenance" in role for _, role in _roles(brief))
    assert all(slot["role"] != "core_maintenance" for pool in brief["candidate_pools"].values()
               for slot in pool.get("core_development_slots") or [])
