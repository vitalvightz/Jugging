"""Core Development Block: a trunk development target is 3-4 complementary
trunk exercises in one session a week, never one token plank.

Pipeline tests run the real Stage 1 entry point with a frozen clock and fixed
seed (no model call), reusing the goal/weakness delivery and open-plan fixtures.
"""
from __future__ import annotations

import copy
import json

import pytest

import test_goal_weakness_delivery as delivery
import test_open_plan_development_block as open_plan
from api.exercise_identity import reconcile_exercise_keys
from fightcamp.calendar_context import role_d_day
from fightcamp.goal_preservation import (
    _mark_trunk_development,
    collect_goal_evidence,
    trunk_dose_adequate,
    validate_goal_preservation,
)
from fightcamp.session_composition import (
    TRUNK_BLOCK_GROUP,
    _candidate_records,
    _select_bounded_records,
    _trunk_block_ineligibility,
    trunk_block_members,
    trunk_development_targeted,
    trunk_alternate_records,
)
from fightcamp.stage2_payload import _attach_manifest_label_and_microdose
from fightcamp.strength_session_quality import trunk_function, trunk_load_tier

_brief, _roles, _members, _entry, _BANK = (
    delivery._brief, delivery._roles, delivery._members, delivery._entry, delivery._BANK)

# Extra intakes on the delivery harness: generated on demand, never in its sweeps.
_SCENARIOS = {
    "core_goal": {"goals": ("trunk_strength", "conditioning"), "weak": ("speed",)},
    "core_secondary": {"goals": ("strength", "conditioning"), "weak": ("strength", "trunk_strength")},
    "core_knee": {"goals": ("speed", "conditioning"), "weak": ("trunk_strength",),
                  "guided": [{"area": "Left knee", "severity": "moderate", "trend": "stable",
                              "injury_type": "pain", "timeframe": "last_month"}]},
    "core_high_fatigue": {"goals": ("speed", "conditioning"), "weak": ("trunk_strength",), "fatigue": "high"},
}
delivery._EXPOSURE_FORMS.update(_SCENARIOS)
open_plan._VARIANTS["core_weakness"] = {
    "injuries": "", "guided_injury": None, "guided_injuries": [],
    "weak_areas": ["trunk_strength", "speed"], "primary_weak_area": "trunk_strength",
    "goal_weakness_collision_tags": [],
}
open_plan._VARIANTS["core_weakness_high_fatigue"] = {**open_plan._VARIANTS["core_weakness"], "fatigue_level": "high"}

_DEVELOPMENT = ["king_no_cut", "boxer28_trunk_fullgym", "core_goal", "core_knee"]
_ALL_TRUNK = [*_DEVELOPMENT, "king", "boxer28_trunk_bodyweight", "muaythai28_power_trunk",
              "core_secondary", "core_high_fatigue", "boxer10_trunk_short"]


def _strength_roles(brief):
    return [(week, role) for week, role in _roles(brief) if role.get("category") == "strength"]


def _block(role) -> dict:
    return (role.get("strength_composition_policy") or {}).get("trunk_block") or {}


def _grouped(role) -> list[dict]:
    return [m for m in _members(role) if m.get("component_group") == TRUNK_BLOCK_GROUP]


# ---------------------------------------------------------------------------
# Normal development: 3-4 distinct, complementary members in one session
# ---------------------------------------------------------------------------

def _main(role) -> list[dict]:
    return [m for m in _members(role) if m.get("component_group") != TRUNK_BLOCK_GROUP]


def _capped(role) -> list[dict]:
    """Members the exercise cap governs (everything but the block's additions)."""
    return [m for m in _members(role) if not m.get("exercise_cap_exempt")]


def _tiers_within_budget(names):
    from fightcamp.strength_session_quality import trunk_high_cost
    tiers = [trunk_load_tier(_BANK[name]) for name in names]
    return (tiers.count("rotational_power") <= 1 and tiers.count("loaded") <= 3
            and sum(trunk_high_cost(_BANK[name]) for name in names) <= 1)


@pytest.mark.parametrize("key", _DEVELOPMENT)
def test_development_week_holds_one_complementary_core_block(key):
    """Core as primary weakness (King, full gym, knee) and as primary goal."""
    brief = _brief(key)
    hosts = [(week, role) for week, role in _strength_roles(brief) if _block(role).get("status") == "delivered"]
    assert hosts, key
    by_week = {}
    for week, role in hosts:
        by_week.setdefault(week.get("week_index"), []).append(role)
    assert all(len(roles) == 1 for roles in by_week.values()), "one block per week"
    for week, role in hosts:
        members = _grouped(role)
        assert 3 <= len(members) <= 4, [m["name"] for m in members]
        functions = [m["trunk_function"] for m in members]
        assert len(set(functions)) == len(functions), functions  # no redundant movement
        assert _tiers_within_budget([m["name"] for m in members])
        assert role_d_day(week, role) >= 14 and week.get("phase") != "TAPER"
        # Grouping is presentation only: each member is its own priced, verified
        # bank exercise.
        priced = {p["name"]: p["effective_prescription"] for p in role["effective_strength_prescriptions"]}
        for member in members:
            assert member["slot_id"] and priced.get(member["name"]), member["name"]
            assert "trunk_strength" in delivery.strength_adaptation_relevance(_BANK[member["name"]])
        # The exercise cap governs the MAIN session only; the block is counted
        # on its own, and the full membership count stays accurate.
        policy = role["strength_composition_policy"]
        assert policy["main_exercise_count"] == len(_capped(role))
        assert policy["main_exercise_count"] <= policy["effective_exercise_cap"] + int(policy["standalone_minimum_applied"])
        assert policy["core_block_count"] == len(members)
        assert policy["selected_count"] == len(_members(role))
        # Only block members are ever cap-exempt.
        assert all(m.get("component_group") == TRUNK_BLOCK_GROUP for m in _members(role) if m.get("exercise_cap_exempt"))
        # Contiguous and last, after the session's main work.
        names = [m["name"] for m in _members(role)]
        assert names[-len(members):] == [m["name"] for m in members]


def test_primary_goal_and_weakness_both_target_development():
    for key in ("core_goal", "king_no_cut"):
        assert _entry(_brief(key), "trunk_strength")["initial_state"] == "build"
    assert not trunk_development_targeted({"weak_areas": ["speed"], "key_goals": ["strength"]})


def test_the_block_no_longer_displaces_the_main_lifts():
    """The capped main selection fills its own cap; only block additions sit outside it."""
    for _, host in _strength_roles(_brief("king_no_cut")):
        policy = host.get("strength_composition_policy")
        if not policy:  # late-fight tail roles have their own selector
            continue
        assert len(_capped(host)) <= policy["effective_exercise_cap"] + int(policy["standalone_minimum_applied"])
        exempt = [m for m in _members(host) if m.get("exercise_cap_exempt")]
        assert all(m in _grouped(host) for m in exempt)


def test_every_development_week_including_spp_hosts_a_block():
    """Stage 1's core candidates let SPP weeks host a block too."""
    for key in ("king_no_cut", "boxer28_trunk_fullgym", "core_goal"):
        brief = _brief(key)
        phases = {week.get("phase") for week, role in _strength_roles(brief) if _block(role).get("status") == "delivered"}
        assert {"GPP", "SPP"} <= phases, key
        entry = _entry(brief, "trunk_strength")
        assert (entry["state"], entry["satisfied"], entry["constraints"]) == ("build", True, []), key
        assert len(entry["evidence"]) == len(entry["coverage_requirements"])


def test_spp_core_candidates_come_from_stage1_and_stay_out_of_session_selection():
    brief = _brief("king_no_cut")
    spp = brief["candidate_pools"]["SPP"]
    core = spp["core_development_slots"]
    assert core and all(slot["session_index"] == 0 and slot["role"] == "core_development" for slot in core)
    offered = {o["name"] for slot in spp["strength_slots"] for o in [slot["selected"], *slot["alternates"]]}
    assert not offered & {slot["selected"]["name"] for slot in core}
    names = {slot["selected"]["name"] for slot in core}
    for _, role in _strength_roles(brief):
        for member in _members(role):
            if member["name"] in names:  # only ever as a block member
                assert member.get("component_group") == TRUNK_BLOCK_GROUP and member.get("exercise_cap_exempt")
    # No trunk target: no core candidates at all.
    assert all("core_development_slots" not in pool for pool in _brief("boxer28_strength_fullgym")["candidate_pools"].values())


@pytest.mark.parametrize("key", [k for k in _DEVELOPMENT if k != "core_knee"])
def test_delivered_block_is_the_development_evidence(key):
    brief = _brief(key)
    evidence = [e for e in collect_goal_evidence(brief) if e.get("trunk_development")]
    assert evidence
    for item in evidence:
        assert len(item["trunk_block_functions"]) >= 3 and trunk_dose_adequate(item)
    assert not any(e["goal"] == "trunk_strength" for e in validate_goal_preservation(brief))


def test_block_members_are_dosed_by_the_resolver_at_a_development_point():
    from fightcamp.working_dose import working_dose
    raised = 0
    for _, host in _strength_roles(_brief("king_no_cut")):
        if not _grouped(host):
            continue
        doses = {p["name"]: p for p in host["effective_strength_prescriptions"]}
        grouped = {m["name"] for m in _grouped(host)}
        for name, dose in doses.items():
            if name in grouped:
                assert dose["effective_prescription"] == working_dose(dose["base_prescription"], development=True)
                raised += dose["effective_prescription"] != working_dose(dose["base_prescription"])
            else:  # main work keeps exactly its existing dose
                assert dose["effective_prescription"] == working_dose(dose["base_prescription"])
        if "Side Plank (Long Duration)" in doses:
            assert doses["Side Plank (Long Duration)"]["effective_prescription"].startswith("4 holds x 15s")
    assert raised >= 3


def test_an_injury_keeps_block_doses_at_the_floor_and_says_so():
    brief = _brief("core_knee")
    host = next(role for _, role in _strength_roles(brief) if _grouped(role))
    assert _block(host)["dose_held_by"] == "injury_constraint"
    from fightcamp.working_dose import working_dose
    for dose in host["effective_strength_prescriptions"]:
        assert dose["effective_prescription"] == working_dose(dose["base_prescription"])
    entry = _entry(brief, "trunk_strength")
    assert "injury_constraint" in entry["reason_codes"] and entry["state"] == "maintain"
    assert not any(e["goal"] == "trunk_strength" for e in validate_goal_preservation(brief))


# ---------------------------------------------------------------------------
# Capacity: the block sits beside the capped main session
# ---------------------------------------------------------------------------

def _synthetic(names):
    slots = [{"slot_id": f"s{i}", "session_index": 1, "priority": i, "selected": _BANK[name]}
             for i, name in enumerate(names)]
    return _candidate_records(slots)


_LIFTS = ["Back Squat", "Romanian Deadlift (RDL)", "Z Press", "Push-Up (Weighted)"]
_CORE = ["Plank", "Pallof Press", "Suitcase Carry (Heavy)", "Med-Ball Rotational Slam",
         "Ab Wheel Rollout (Kneeling)", "Bear Hug Carry", "Anti-Rotation Med Ball Slam"]


def test_four_main_exercises_and_a_four_exercise_core_block_coexist():
    records = _synthetic(_LIFTS + _CORE)
    common = dict(role_key="primary_strength_day", cap=5, pressure=0, preserve_trunk_support=True)
    main, _ = _select_bounded_records(records, **common)
    full, _ = _select_bounded_records(records, **common, trunk_block_target=4)
    assert [r["name"] for r in main] == [r["name"] for r in full if r in main]  # main untouched
    assert set(_LIFTS) <= {r["name"] for r in full}
    block = trunk_block_members(full)
    assert len(block) == 4 and len(full) == 8
    assert _tiers_within_budget([r["name"] for r in block])


def test_core_capacity_never_displaces_a_main_selection():
    """Every cap and pressure: the main session is exactly what it is without a block."""
    records = _king_primary_records()
    for role_key, cap, pressure in (("primary_strength_day", 6, 0), ("secondary_strength_day", 3, 0),
                                    ("primary_strength_day", 4, 1)):
        common = dict(role_key=role_key, cap=cap, pressure=pressure, preserve_trunk_support=True)
        main, _ = _select_bounded_records(records, **common)
        full, _ = _select_bounded_records(records, **common, trunk_block_target=3,
                                          trunk_alternates=trunk_alternate_records(records))
        assert all(r in full for r in main), role_key
        added = [r for r in full if r not in main]
        assert all(r["trunk_targeted"] for r in added) and len(added) <= 3


def _king_primary_records():
    brief = _brief("king_no_cut")
    slots = [s for s in brief["candidate_pools"]["GPP"]["strength_slots"] if (s.get("session_index") or 1) == 1]
    return _candidate_records(slots)


def test_the_role_cap_previously_reduced_core_to_one_movement():
    records = _synthetic(_LIFTS + _CORE)
    common = dict(role_key="primary_strength_day", cap=5, pressure=0, preserve_trunk_support=True)
    before, _ = _select_bounded_records(records, **common)
    assert sum(r["trunk_targeted"] for r in before) == 1
    after, _ = _select_bounded_records(records, **common, trunk_block_target=3)
    assert len(trunk_block_members(after)) == 3 and all(r in after for r in before)


@pytest.mark.parametrize(("target", "expected"), [(4, 4), (3, 3)])
def test_block_size_follows_the_trunk_priority(target, expected):
    """Four for a primary goal, three for a primary weakness beside another goal."""
    records = _synthetic(["Back Squat", "Romanian Deadlift (RDL)", *_CORE[:5], "Z Press"])
    main, _ = _select_bounded_records(records, role_key="primary_strength_day", cap=6, pressure=0,
                                      preserve_trunk_support=True)
    selected, _ = _select_bounded_records(records, role_key="primary_strength_day", cap=6, pressure=0,
                                          preserve_trunk_support=True, trunk_block_target=target)
    members = trunk_block_members(selected)
    assert len(members) == expected and all(r in selected for r in main)
    assert len({trunk_function(r["slot"]["selected"], trunk_targeted=True) for r in members}) == expected


def test_trunk_priority_sets_the_block_target():
    from fightcamp.session_composition import trunk_development_block_target
    assert trunk_development_block_target({"weak_areas": ["speed"], "key_goals": ["strength"]}) == 0
    assert trunk_development_block_target({"weak_areas": ["trunk_strength"], "key_goals": ["speed"]}) == 3
    assert trunk_development_block_target({"weak_areas": [], "key_goals": ["trunk_strength"]}) == 4


# ---------------------------------------------------------------------------
# Load: core is real training load
# ---------------------------------------------------------------------------

def test_load_tiers_separate_stability_loaded_and_rotational_power():
    assert trunk_load_tier(_BANK["Dead Bug (Isometric)"]) == "stability"
    assert trunk_load_tier(_BANK["Bear Hug Carry"]) == "loaded"
    assert trunk_load_tier(_BANK["Suitcase Carry (Heavy)"]) == "loaded"
    assert trunk_load_tier(_BANK["Cable Pallof Press"]) == "loaded"
    assert trunk_load_tier(_BANK["Med-Ball Rotational Slam"]) == "rotational_power"
    assert trunk_load_tier(_BANK["Back Squat"]) == ""


def test_heavy_core_options_are_capped_by_the_block_load_budget():
    """Heavy carries, presses and two slams on offer: one explosive, one high-cost."""
    names = ["Back Squat", "Bear Hug Carry", "Cable Pallof Press", "Barbell Rollout",
             "Med-Ball Rotational Slam", "Woodchopper (Cable)", "Russian Twist (Weighted)", "Plank"]
    selected, _ = _select_bounded_records(_synthetic(names), role_key="primary_strength_day", cap=1,
                                          pressure=0, trunk_block_target=4)
    block = [r["name"] for r in trunk_block_members(selected)]
    assert len(block) >= 3 and _tiers_within_budget(block), block


def test_block_members_count_in_the_sessions_realised_load():
    from fightcamp.session_composition import realised_role_stress
    host = next(role for _, role in _strength_roles(_brief("boxer28_trunk_fullgym")) if _grouped(role))
    without = {**host, "selected_exercise_assignments": _main(host)}
    full_stress, main_stress = realised_role_stress(host), realised_role_stress(without)
    assert "trunk" in {region.value for region in full_stress.regions}
    assert set(main_stress.regions) <= set(full_stress.regions)
    assert full_stress.systemic >= main_stress.systemic
    assert full_stress.neural_mechanical >= main_stress.neural_mechanical
    # The calendar stamp the realised-load governor reads includes the block.
    assert "trunk" in host["calendar_stress"]["regions"]


# ---------------------------------------------------------------------------
# Maintenance, readiness, taper, sparring, equipment
# ---------------------------------------------------------------------------

def test_secondary_trunk_target_stays_maintenance():
    brief = _brief("core_secondary")
    entry = _entry(brief, "trunk_strength")
    assert (entry["priority"], entry["initial_state"], entry["state"]) == ("secondary", "maintain", "maintain")
    assert entry["satisfied"]
    for _, role in _strength_roles(brief):
        # No block is reserved; ordinary Stage 1 fill alone decides any extra trunk item.
        assert not _block(role) and not _grouped(role)


def test_high_fatigue_turns_core_development_into_maintenance():
    brief = _brief("core_high_fatigue")
    entry = _entry(brief, "trunk_strength")
    assert entry["state"] == "maintain" and "high_fatigue" in entry["reason_codes"]
    assert not any(_grouped(role) for _, role in _strength_roles(brief))
    assert not any(e["goal"] == "trunk_strength" for e in validate_goal_preservation(brief))


def test_a_weight_cut_records_readiness_as_the_reason_no_block_was_built():
    brief = _brief("king")
    blocks = [_block(role) for _, role in _strength_roles(brief) if _block(role)]
    assert blocks and all(b["status"] == "not_eligible" and b["reason_code"] == "readiness_capacity"
                          and "weight_cut_pressure" in b["pressure_sources"] for b in blocks)
    entry = _entry(brief, "trunk_strength")
    assert entry["state"] == "maintain" and entry["satisfied"]
    assert {c["reason_code"] for c in entry["constraints"]} == {"weight_cut_pressure"}
    assert not any(e["goal"] == "trunk_strength" for e in validate_goal_preservation(brief))


def test_an_injury_keeps_the_block_inside_injury_filtered_candidates():
    brief = _brief("core_knee")
    pool_names = {option.get("name") for pool in brief["candidate_pools"].values()
                  for slot in [*(pool.get("strength_slots") or []), *(pool.get("core_development_slots") or [])]
                  for option in [slot.get("selected") or {}, *(slot.get("alternates") or [])]}
    grouped = [m["name"] for _, role in _strength_roles(brief) for m in _grouped(role)]
    assert grouped and set(grouped) <= pool_names


def _ctx(**overrides):
    week = {"phase": "GPP", "effective_hard_sparring_days": ["thursday"],
            "calendar_days": [{"d_day": 20, "weekday": "wednesday"}]}
    role = {"role_key": "primary_strength_day", "category": "strength", "scheduled_day_hint": "Monday",
            "scheduled_countdown_label": "D-20"}
    ctx = {"week": week, "role": role, "phase": "GPP", "pressure_state": {}, "exercise_count_pressure": 0,
           "microdose_only": False}
    for key, value in overrides.items():
        (role if key in {"scheduled_day_hint", "pre_hard_contact_managed_stress"} else ctx)[key] = value
    return ctx


def test_block_host_eligibility_protects_taper_sparring_and_readiness():
    assert _trunk_block_ineligibility(_ctx()) is None
    assert _trunk_block_ineligibility(_ctx(phase="TAPER"))["reason_code"] == "taper_phase"
    assert _trunk_block_ineligibility(_ctx(scheduled_day_hint="Wednesday")) == {
        "reason_code": "hard_sparring_priority", "source": "hard_sparring_adjacent"}
    assert _trunk_block_ineligibility(_ctx(scheduled_day_hint="Thursday"))["reason_code"] == "hard_sparring_priority"
    assert _trunk_block_ineligibility(_ctx(scheduled_day_hint="Friday")) is None  # the day after is not protected
    assert _trunk_block_ineligibility(_ctx(pre_hard_contact_managed_stress=True))["reason_code"] == "hard_sparring_priority"
    pressured = _trunk_block_ineligibility(_ctx(exercise_count_pressure=2, pressure_state={"fatigue_pressure": 2}))
    assert pressured == {"reason_code": "readiness_capacity", "pressure_sources": ["high_fatigue"]}


def test_hard_sparring_adjacency_is_recorded_on_the_fight_camp():
    blocks = [_block(role) for _, role in _strength_roles(_brief("muaythai28_power_trunk"))]
    assert any(b.get("reason_code") == "hard_sparring_priority" for b in blocks)


def test_no_taper_or_late_tail_session_carries_a_block():
    for key in _ALL_TRUNK:
        for week, role in _roles(_brief(key)):
            if _grouped(role):
                assert week.get("phase") in {"GPP", "SPP"} and role_d_day(week, role) >= 14, key


def test_limited_equipment_records_why_the_block_was_not_delivered():
    brief = _brief("boxer28_trunk_bodyweight")
    blocks = [_block(role) for _, role in _strength_roles(brief) if _block(role)]
    assert any(b["status"] == "constrained" and b["reason_code"] == "insufficient_complementary_candidates"
               for b in blocks)
    entry = _entry(brief, "trunk_strength")
    assert entry["state"] == "maintain" and entry["satisfied"]
    assert "insufficient_complementary_candidates" in entry["reason_codes"]
    assert not any(e["goal"] == "trunk_strength" for e in validate_goal_preservation(brief))


@pytest.mark.parametrize("key", _ALL_TRUNK)
def test_every_trunk_plan_validates(key):
    assert not any(e["goal"] == "trunk_strength" for e in validate_goal_preservation(_brief(key)))


def test_unrelated_programmes_carry_no_core_block():
    for key in ("boxer28_strength_fullgym", "mma42_strength_power", "boxer28_knee_injury", "vito_b"):
        for _, role in _roles(_brief(key)):
            assert not _block(role) and not _grouped(role), key


# ---------------------------------------------------------------------------
# Validation: a token trunk exercise is not development
# ---------------------------------------------------------------------------

def _stimulus(function, sets, amount, unit="reps", tier="stability"):
    item = {"intents": ["trunk_training"], "trunk_function": function, "trunk_load_tier": tier, "sets": sets,
            "reps": amount if unit == "reps" else 0}
    if unit != "reps":
        item.update(volume_unit=unit, volume_amount=amount)
    return item


def test_token_or_redundant_trunk_work_earns_no_development():
    single = [_stimulus("anti_extension", 3, 10, "sec")]
    redundant = [_stimulus("anti_extension", 3, 20, "sec"), _stimulus("anti_extension", 3, 30, "sec"),
                 _stimulus("anti_extension", 3, 8, tier="loaded")]
    negligible = [_stimulus("anti_extension", 3, 20, "sec"), _stimulus("anti_rotation", 3, 8, tier="loaded"),
                  _stimulus("loaded_carry", 1, 15, "m", tier="loaded")]  # one 15 m carry
    # Three distinct functions at the dose FLOOR (2 x 6, 3 x 10 s): token work.
    floor = [_stimulus("anti_extension", 3, 10, "sec", tier="loaded"), _stimulus("anti_rotation", 2, 6, tier="loaded"),
             _stimulus("rotation", 2, 6)]
    # Three adequately dosed, distinct functions, but all unloaded bodyweight.
    unresisted = [_stimulus("anti_extension", 4, 15, "sec"), _stimulus("anti_rotation", 3, 8),
                  _stimulus("anti_lateral_flexion", 2, 20, "sec")]
    for stimuli in (single, redundant, negligible, floor, unresisted):
        _mark_trunk_development(stimuli)
        assert not any(s["trunk_development"] for s in stimuli)
    block = [_stimulus("anti_extension", 4, 15, "sec"), _stimulus("anti_rotation", 3, 8, tier="loaded"),
             _stimulus("loaded_carry", 4, 30, "m", tier="loaded"),
             _stimulus("rotational_power", 4, 2, tier="rotational_power")]
    _mark_trunk_development(block)
    assert all(s["trunk_development"] for s in block)


def test_floor_and_embedded_doses_are_not_development_doses():
    assert not trunk_dose_adequate(_stimulus("anti_rotation", 1, 15, "sec"))  # 1-2 x 15-20 s embedded hold
    assert not trunk_dose_adequate(_stimulus("anti_extension", 3, 10, "sec"))  # isometric floor
    assert not trunk_dose_adequate(_stimulus("rotation", 2, 6))  # core-template floor
    assert trunk_dose_adequate(_stimulus("anti_extension", 4, 15, "sec"))  # development point
    assert trunk_dose_adequate(_stimulus("rotation", 3, 8))
    assert trunk_dose_adequate(_stimulus("rotational_power", 4, 2, tier="rotational_power"))


def test_a_brief_claiming_development_from_one_trunk_exercise_is_rejected():
    brief = copy.deepcopy(_brief("king_no_cut"))
    for _, role in _strength_roles(brief):
        grouped = {m["name"] for m in _grouped(role)}
        if grouped:
            keep = next(iter(grouped))
            role["selected_exercise_assignments"] = [m for m in role["selected_exercise_assignments"]
                                                     if m["name"] not in grouped or m["name"] == keep]
    entry = _entry(brief, "trunk_strength")
    entry.update(state="build", constraints=[], satisfied=True)
    assert any(e["goal"] == "trunk_strength" for e in validate_goal_preservation(brief))


def test_carry_doses_are_read_as_distance():
    from fightcamp.goal_preservation import dose_volume
    assert dose_volume("3 carries over 20 m, heavy but unbroken; rest 90s.") == {"sets": 3, "amount": 20, "unit": "m"}
    assert dose_volume("2-4 carries over 15-30 m") == {"sets": 2, "amount": 15, "unit": "m"}


def test_trunk_functions_come_from_the_exercise_not_loose_tags():
    expected = {"Plank": "anti_extension", "Pallof Press": "anti_rotation",
                "Side Plank (Long Duration)": "anti_lateral_flexion", "Suitcase Carry (Heavy)": "loaded_carry",
                "Med-Ball Rotational Slam": "rotational_power", "Ab Wheel Rollout (Kneeling)": "anti_extension",
                "Russian Twist (Weighted)": "rotation"}
    for name, function in expected.items():
        assert trunk_function(_BANK[name]) == function, name
    assert trunk_function(_BANK["Back Squat"]) == ""


# ---------------------------------------------------------------------------
# Open plans
# ---------------------------------------------------------------------------

def test_open_plan_builds_a_grouped_core_block_every_week():
    spec = open_plan._spec("core_weakness")
    coverage = next(c for c in spec["goal_coverage"] if c["goal"] == "trunk_strength")
    assert (coverage["state"], coverage["satisfied"], coverage["contract_errors"]) == ("build", True, [])
    for week in spec["weekly_session_plan"]["weeks"]:
        # Planner-only: the open-plan projection carries ordinary exercises,
        # each with its own dose, and no grouping signal for Stage 2.
        for session in week["sessions"]:
            for exercise in session.get("exercises") or []:
                assert "group" not in exercise and exercise["prescription"]


def test_open_plan_high_fatigue_keeps_core_as_maintenance():
    spec = open_plan._spec("core_weakness_high_fatigue")
    coverage = next(c for c in spec["goal_coverage"] if c["goal"] == "trunk_strength")
    assert coverage["state"] == "maintain" and coverage["contract_errors"] == []
    assert not any(e.get("group") for week in spec["weekly_session_plan"]["weeks"]
                   for s in week["sessions"] for e in s.get("exercises") or [])


# ---------------------------------------------------------------------------
# Rendering and logging
# ---------------------------------------------------------------------------

def _host(brief):
    return next((week, role) for week, role in _strength_roles(brief) if _grouped(role))


def test_stage2_manifest_carries_no_group():
    _, role = _host(_brief("king_no_cut"))
    entry = {}
    _attach_manifest_label_and_microdose(entry, role)
    assert "exercise_groups" not in entry


def test_structured_blocks_are_tagged_by_server_identity_and_stay_separate():
    brief = _brief("king_no_cut")
    week, role = _host(brief)
    day = next(d for d in week["calendar_days"] if d["d_day"] == role_d_day(week, role))
    blocks = [{"block_type": "strength", "display_name": m["name"]} for m in _members(role)]
    blocks.append({"block_type": "accessory", "display_name": "Neck Bridge", "tags": ["core_development"]})
    plan = {"weeks": [{"days": [{"date": day.get("date"), "countdown_label": f"D-{day['d_day']}",
                                 "sessions": [{"title": role.get("athlete_facing_label") or "Strength",
                                               "blocks": blocks}]}]}]}
    out = reconcile_exercise_keys(plan, brief)
    rendered = out["weeks"][0]["days"][0]["sessions"][0]["blocks"]
    grouped = {m["name"] for m in _grouped(role)}
    for block in rendered[:-1]:
        assert ("core_development" in (block.get("tags") or [])) == (block["display_name"] in grouped)
        assert block.get("exercise_key")  # each member keeps its own identity for logging
    assert not rendered[-1].get("tags")  # a model-written tag is never trusted
    assert len(rendered) == len(blocks)


def test_a_block_yields_when_recovery_must_cut_the_hosts_main_work():
    """power_trunk: the SPP host sits the day after a heavy ballistic session.
    Its block yields to recovery, so goal repair can still move that session
    with its main lifts intact and the strength goal stays covered."""
    from scripts.audit_training_intelligence import generate_scenario

    brief = generate_scenario("power_trunk")["brief"]
    assert validate_goal_preservation(brief) == []
    neural = next(role for _, role in _strength_roles(brief) if role["role_key"] == "neural_plus_strength_day")
    assert {"Barbell Thruster", "DB Bench Press → Plyo Push-Up"} <= {m["name"] for m in _members(neural)}
    assert not _grouped(neural)
    strength = _entry(brief, "strength")
    assert strength["satisfied"]


_STAGE2_MARKERS = ("core_development", "Core Development", "exercise_groups", "component_group",
                   "trunk_function", "exercise_cap_exempt", "trunk_block", "trunk_load_tier")


def _model_response(text):
    from types import SimpleNamespace
    return SimpleNamespace(id="r", output_text=text,
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2))


def _assert_no_marker(calls):
    sent = json.dumps([call.get("input") for call in calls], default=str)
    for marker in _STAGE2_MARKERS:
        assert marker not in sent, marker


def test_stage2_requests_carry_no_core_development_signal(monkeypatch):
    """Planner-only: neither AI call sees the block as a group. Covers the
    written plan (first_pass) and the structured card (structured_first and
    its repair), the call suspected in the #3163 900 s runtime-cap failures."""
    import asyncio
    from support import FakeOpenAIClient
    from api.stage2_automation import OpenAIStage2Automator

    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "1")
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_REPAIR", "1")
    stage1 = delivery._generate("king_no_cut")
    brief = stage1["planning_brief"]
    host = next(role for _, role in _strength_roles(brief) if _grouped(role))
    plan_text = stage1.get("plan_text") or ""

    # Written plan: the real finalize path.
    client = FakeOpenAIClient([_model_response(plan_text)] * 6)
    asyncio.run(OpenAIStage2Automator(client=client, model="m").finalize(stage1_result=stage1))
    assert client.responses.calls
    _assert_no_marker(client.responses.calls)

    # Structured card: first pass returns no valid card, forcing the repair call.
    client = FakeOpenAIClient([_model_response("{}"), _model_response("{}")])
    automator = OpenAIStage2Automator(client=client, model="m")
    rendered = plan_text + "\n" + "\n".join(f"- {m['name']}" for m in _members(host))
    asyncio.run(automator._generate_structured_outcome(
        final_plan_text=rendered, planning_brief=brief, source="test", costs=[]))
    assert len(client.responses.calls) == 2  # structured_first + structured_repair
    _assert_no_marker(client.responses.calls)