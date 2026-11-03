"""Production regression: power goal, trunk-strength weakness. Monday D-21
carries six strength exercises; Tuesday D-20 was chosen as the Core
Development Block host, then adjacent-strength recovery found substantial
overlapping loading with Monday and stripped Tuesday's block. The week must
re-rule its host on the final composition instead of leaving Monday (and
every other session) marked ``hosted_elsewhere`` with no block anywhere."""
from __future__ import annotations

import test_goal_weakness_delivery as delivery
from fightcamp.session_composition import (
    TRUNK_BLOCK_GROUP,
    _ADJACENT_STRENGTH_RECOVERY_REASON,
    _apply_adjacent_strength_recovery,
    _candidate_records,
    _select_bounded_records,
    trunk_block_members,
)
from fightcamp.strength_session_quality import trunk_function

_BANK = delivery._BANK
_CORE = ["Plank", "Pallof Press", "Suitcase Carry (Heavy)", "Russian Twist (Weighted)",
         "Ab Wheel Rollout (Kneeling)", "Bear Hug Carry", "Copenhagen Side Plank"]
_MONDAY = ["Trap Bar Jump", "Back Squat", "Romanian Deadlift (RDL)", "Z Press",
           "Push-Up (Weighted)", "Plank"]


def _records(names, index):
    return _candidate_records([{"slot_id": f"{index}-{i}", "session_index": index, "priority": i,
                                "selected": _BANK[name]} for i, name in enumerate(names)])


def _entry(week, d_day, role_key, main, core, cap, block_target):
    records = _records(main, d_day)
    core_records = _records(core, 100 + d_day)
    selected, _ = _select_bounded_records(
        records, role_key=role_key, cap=cap, pressure=0, preserve_trunk_support=True,
        preserve_adaptation="power", trunk_block_target=block_target,
        trunk_alternates=core_records if block_target else ())
    role = {"category": "strength", "role_key": role_key, "scheduled_d_day": d_day,
            "strength_composition_policy": {"trunk_block": (
                {"intent": "development", "status": "delivered"} if block_target
                else {"intent": "development", "status": "hosted_elsewhere"})}}
    week["session_roles"].append(role)
    return {"week": week, "role": role, "phase": "SPP", "records": records, "core_records": core_records,
            "role_key": role_key, "effective_cap": cap, "pressure": 0, "selected_records": selected,
            "preserve_trunk_support": True, "preserve_adaptation": "power", "trunk_development": True,
            "trunk_block_size": 3, "trunk_block_target": block_target,
            "trunk_block_additions": [r for r in selected if r["name"] in core]}


def _week(third_core, monday_ineligible=False):
    week = {"session_roles": []}
    entries = [
        _entry(week, 21, "primary_strength_day", _MONDAY, [], 6, 0),
        _entry(week, 20, "secondary_strength_day", ["Back Squat", "Push-Up (Weighted)"], _CORE, 3, 3),
        _entry(week, 17, "secondary_strength_day", ["Push-Up (Weighted)"], third_core, 3, 0),
    ]
    if monday_ineligible:
        entries[0]["role"]["strength_composition_policy"]["trunk_block"] = {
            "intent": "development", "status": "not_eligible", "reason_code": "hard_sparring_priority"}
    _apply_adjacent_strength_recovery({"weeks": [week]}, entries, allow_relocation=False)
    return entries


def _block(entry):
    return entry["role"]["strength_composition_policy"]["trunk_block"]


def test_yielded_block_moves_to_the_remaining_eligible_session():
    monday, tuesday, friday = _week(_CORE)
    assert _block(tuesday)["reason_code"] == _ADJACENT_STRENGTH_RECOVERY_REASON
    assert not any(a.get("component_group") for a in tuesday["role"]["selected_exercise_assignments"])
    block = _block(friday)
    assert block["status"] == "delivered" and block["rehosted_from_d_day"] == 20
    grouped = [a for a in friday["role"]["selected_exercise_assignments"]
               if a.get("component_group") == TRUNK_BLOCK_GROUP]
    names = [a["name"] for a in grouped]
    assert 3 <= len(names) <= 4 and len(set(names)) == len(names)
    assert set(names) <= set(_CORE)
    assert len({trunk_function(_BANK[n]) for n in names}) == len(names)
    # Main work is untouched; the block sits only in its own capacity.
    assert "Push-Up (Weighted)" in {a["name"] for a in friday["role"]["selected_exercise_assignments"]}
    # One host per week: Monday's deferral is now true.
    assert _block(monday)["status"] == "hosted_elsewhere"


def test_without_another_eligible_host_the_real_reason_is_recorded():
    entries = _week([], monday_ineligible=True)
    statuses = [_block(e) for e in entries]
    assert all(s["status"] != "hosted_elsewhere" for s in statuses)
    assert not any(s["status"] == "delivered" for s in statuses)
    assert all(s.get("reason_code") for s in statuses)
    friday = entries[2]
    assert _block(friday)["status"] == "constrained"
    assert not trunk_block_members(friday["selected_records"])
    assert [a["name"] for a in friday["role"].get("selected_exercise_assignments") or []] in ([], ["Push-Up (Weighted)"])
