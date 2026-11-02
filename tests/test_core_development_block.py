"""Core Development Block: a trunk development target is 3-4 complementary
trunk exercises in one session a week, never one token plank.

Pipeline tests run the real Stage 1 entry point with a frozen clock and fixed
seed (no model call), reusing the goal/weakness delivery and open-plan fixtures.
"""
from __future__ import annotations

import copy

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
from fightcamp.strength_session_quality import trunk_function

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
        assert role_d_day(week, role) >= 14 and week.get("phase") != "TAPER"
        # Grouping is presentation only: each member is its own priced, verified
        # bank exercise inside the role's existing cap.
        priced = {p["name"]: p["effective_prescription"] for p in role["effective_strength_prescriptions"]}
        for member in members:
            assert member["slot_id"] and priced.get(member["name"]), member["name"]
            assert "trunk_strength" in delivery.strength_adaptation_relevance(_BANK[member["name"]])
        policy = role["strength_composition_policy"]
        assert len(_members(role)) <= policy["effective_exercise_cap"] + int(policy.get("standalone_minimum_applied", False))
        # Contiguous and last, after the session's main work.
        names = [m["name"] for m in _members(role)]
        assert names[-len(members):] == [m["name"] for m in members]


def test_primary_goal_and_weakness_both_target_development():
    for key in ("core_goal", "king_no_cut"):
        assert _entry(_brief(key), "trunk_strength")["initial_state"] == "build"
    assert not trunk_development_targeted({"weak_areas": ["speed"], "key_goals": ["strength"]})


def test_a_development_block_keeps_the_sessions_main_work():
    """The block replaces accessories, never the athlete's required lift."""
    for week, role in _strength_roles(_brief("king_no_cut")):
        if _block(role).get("status") == "delivered":
            assert any(m not in _grouped(role) for m in _members(role))
            assert any("strength" in delivery.strength_adaptation_relevance(_BANK.get(m["name"], {}))
                       or "power" in delivery.strength_adaptation_relevance(_BANK.get(m["name"], {}))
                       or "speed" in delivery.strength_adaptation_relevance(_BANK.get(m["name"], {}))
                       for m in _members(role) if m not in _grouped(role))


@pytest.mark.parametrize("key", _DEVELOPMENT)
def test_delivered_block_is_the_development_evidence(key):
    brief = _brief(key)
    evidence = [e for e in collect_goal_evidence(brief) if e.get("trunk_development")]
    assert evidence
    for item in evidence:
        assert len(item["trunk_block_functions"]) >= 3 and trunk_dose_adequate(item)
    assert not any(e["goal"] == "trunk_strength" for e in validate_goal_preservation(brief))


# ---------------------------------------------------------------------------
# Caps: the selector that used to stop at one trunk member
# ---------------------------------------------------------------------------

def _king_primary_records():
    brief = _brief("king_no_cut")
    slots = [s for s in brief["candidate_pools"]["GPP"]["strength_slots"] if (s.get("session_index") or 1) == 1]
    return _candidate_records(slots)


def test_the_role_cap_previously_reduced_core_to_one_movement():
    records = _king_primary_records()
    common = dict(role_key="primary_strength_day", cap=6, pressure=0, preserve_trunk_support=True)
    before, _ = _select_bounded_records(records, **common)
    assert sum(r["trunk_targeted"] for r in before) == 1
    after, dropped = _select_bounded_records(records, **common, trunk_block_target=4,
                                             trunk_alternates=trunk_alternate_records(records))
    members = trunk_block_members(after)
    assert len(members) >= 3 and len(after) <= 6  # same cap, not a cap increase
    # A slot whose alternate joined the block is one substitution, not two members.
    base_ids = [str(r["slot"]["slot_id"]).partition(":alternate:")[0] for r in after]
    assert len(base_ids) == len(set(base_ids))
    # The session's required strength group is still covered.
    assert any(r["families"] & {"lower_strength", "upper_strength"} for r in after)


@pytest.mark.parametrize(("target", "expected"), [(4, 4), (3, 3)])
def test_block_size_follows_the_trunk_priority(target, expected):
    """Four for a primary goal, three for a primary weakness beside another goal."""
    names = ["Back Squat", "Romanian Deadlift (RDL)", "Plank", "Pallof Press", "Ab Wheel Rollout (Kneeling)",
             "Suitcase Carry (Heavy)", "Med-Ball Rotational Slam", "Z Press"]
    slots = [{"slot_id": f"s{i}", "session_index": 1, "priority": i, "selected": _BANK[name]}
             for i, name in enumerate(names)]
    records = _candidate_records(slots)
    selected, _ = _select_bounded_records(records, role_key="primary_strength_day", cap=6, pressure=0,
                                          preserve_trunk_support=True, trunk_block_target=target)
    members = trunk_block_members(selected)
    assert len(members) == expected and len(selected) <= 6
    assert any(r["name"] in {"Back Squat", "Romanian Deadlift (RDL)"} for r in selected)  # main lift kept
    assert len({trunk_function(r["slot"]["selected"], trunk_targeted=True) for r in members}) == expected


def test_trunk_priority_sets_the_block_target():
    from fightcamp.session_composition import trunk_development_block_target
    assert trunk_development_block_target({"weak_areas": ["speed"], "key_goals": ["strength"]}) == 0


def test_a_block_never_exceeds_a_small_cap():
    records = _king_primary_records()
    selected, _ = _select_bounded_records(records, role_key="secondary_strength_day", cap=3, pressure=0,
                                          preserve_trunk_support=True, trunk_block_target=4)
    assert len(selected) <= 3


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
                  for slot in pool.get("strength_slots") or []
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

def _stimulus(function, sets, amount, unit="reps"):
    item = {"intents": ["trunk_training"], "trunk_function": function, "sets": sets,
            "reps": amount if unit == "reps" else 0}
    if unit != "reps":
        item.update(volume_unit=unit, volume_amount=amount)
    return item


def test_token_or_redundant_trunk_work_earns_no_development():
    single = [_stimulus("anti_extension", 3, 10, "sec")]
    redundant = [_stimulus("anti_extension", 3, 20, "sec"), _stimulus("anti_extension", 3, 30, "sec"),
                 _stimulus("anti_extension", 3, 8)]
    negligible = [_stimulus("anti_extension", 3, 20, "sec"), _stimulus("anti_rotation", 3, 8),
                  _stimulus("loaded_carry", 1, 15, "m")]  # one 15 m carry
    for stimuli in (single, redundant, negligible):
        _mark_trunk_development(stimuli)
        assert not any(s["trunk_development"] for s in stimuli)
    block = [_stimulus("anti_extension", 3, 20, "sec"), _stimulus("anti_rotation", 3, 8),
             _stimulus("loaded_carry", 3, 20, "m"), _stimulus("rotational_power", 3, 4)]
    _mark_trunk_development(block)
    assert all(s["trunk_development"] for s in block)


def test_embedded_hold_dose_is_not_a_development_dose():
    assert not trunk_dose_adequate(_stimulus("anti_rotation", 1, 15, "sec"))  # 1-2 x 15-20 s embedded hold
    assert not trunk_dose_adequate(_stimulus("anti_extension", 3, 5, "sec"))
    assert trunk_dose_adequate(_stimulus("anti_extension", 3, 10, "sec"))


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
        grouped = [[e["name"] for e in s.get("exercises") or [] if e.get("group") == TRUNK_BLOCK_GROUP]
                   for s in week["sessions"]]
        hosts = [names for names in grouped if names]
        assert len(hosts) == 1 and 3 <= len(hosts[0]) <= 4, week["week"]
        session = next(s for s in week["sessions"] if any(e.get("group") for e in s.get("exercises") or []))
        assert all(e["prescription"] for e in session["exercises"])  # each member keeps its own dose


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


def test_stage2_manifest_names_the_group_without_merging_members():
    _, role = _host(_brief("king_no_cut"))
    entry = {}
    _attach_manifest_label_and_microdose(entry, role)
    assert entry["exercise_groups"] == {TRUNK_BLOCK_GROUP: [m["name"] for m in _grouped(role)]}


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
