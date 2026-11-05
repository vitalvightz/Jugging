from __future__ import annotations

from copy import deepcopy

import pytest

from fightcamp.pre_hard_contact_strength import (
    PRE_HARD_CONTACT_STRENGTH_CAP_REASON,
    apply_pre_hard_contact_strength_exposure_cap,
)
from fightcamp.prescription_resolver import apply_effective_strength_prescriptions
from fightcamp.late_camp_role_morph import apply_late_camp_role_morph
from fightcamp.stage2_validator import _late_camp_effective_prescription_warnings
from fightcamp.session_composition import compose_normal_strength_assignments, assignment_from_slot, redistribute_pre_hard_power_from_main


def _calendar(*pairs: tuple[str, int]) -> list[dict]:
    return [{"weekday": day, "d_day": d_day} for day, d_day in pairs]


def _strength(day: str, *, session_index: int = 1, strength_session_index: int = 1) -> dict:
    return {
        "category": "strength",
        "role_key": "primary_strength_day" if strength_session_index == 1 else "secondary_strength_day",
        "preferred_pool": "strength_slots",
        "scheduled_day_hint": day,
        "session_index": session_index,
        "strength_session_index": strength_session_index,
    }


def _hard(day: str, *, load: str = "hard", status: str = "hard_as_planned") -> dict:
    return {"day": day, "status": status, "effective_load": load}


def _map(
    *,
    strength_day: str = "Monday",
    strength_d_day: int = 23,
    hard_day: str = "Tuesday",
    hard_d_day: int = 22,
    load: str = "hard",
    extra_roles: list[dict] | None = None,
) -> dict:
    roles = [_strength(strength_day)] + list(extra_roles or [])
    week = {
        "week_index": 1,
        "phase": "GPP",
        "declared_training_days": [strength_day, hard_day, "Thursday"],
        "calendar_days": _calendar(
            (strength_day, strength_d_day),
            (hard_day, hard_d_day),
            ("Thursday", min(strength_d_day, hard_d_day) - 2),
        ),
        "hard_sparring_plan": [
            _hard(
                hard_day,
                load=load,
                status="hard_as_planned" if load == "hard" else "technical_only",
            )
        ],
        "effective_hard_sparring_days": [hard_day] if load == "hard" else [],
        "session_roles": roles,
        "suppressed_roles": [],
    }
    return {"model": "session_role_overlay.v1", "weeks": [week]}


def _anchor_slot(*, prescription: str = "3 x 10 @ 65% 1RM, tempo 3-1-1") -> dict:
    return {
        "slot_id": "gpp-strength-1-anchor",
        "session_index": 1,
        "priority": "primary",
        "role": "hinge",
        "anchor_capable": True,
        "selected": {
            "name": "Romanian Deadlift (RDL)",
            "prescription": prescription,
            "selection_metadata": {
                "eccentric_cost": "high",
                "soreness_risk": "high",
                "impact_cost": "low",
                "landing_cost": "none",
            },
        },
    }


def _high_cost_power_slot() -> dict:
    return {
        "slot_id": "gpp-strength-1-power",
        "session_index": 1,
        "priority": "secondary",
        "role": "jump",
        "quality_class": "anchor_power",
        "selected": {
            "name": "Single-Leg Forward Hops",
            "prescription": "3 x 5",
            "quality_class": "anchor_power",
            "selection_metadata": {
                "impact_cost": "high",
                "landing_cost": "high",
                "eccentric_cost": "moderate",
                "soreness_risk": "moderate",
                # A selected jump is a POWER goal witness even when it is
                # unsuitable for transfer. Without this fixture metadata the
                # test previously (incorrectly) forced the allocator to retain
                # one low-cost power slot solely to witness that goal.
                "primary_adaptation": "power",
            },
        },
    }


def _low_cost_support_slot() -> dict:
    return {
        "slot_id": "gpp-strength-1-support",
        "session_index": 1,
        "priority": "support",
        "role": "anti_rotation",
        "support_only": True,
        "selected": {
            "name": "Anti-Rotation Hold",
            "prescription": "2 x 6",
            "support_only": True,
            "selection_metadata": {
                "impact_cost": "none",
                "landing_cost": "none",
                "eccentric_cost": "low",
                "soreness_risk": "low",
            },
        },
    }


def _candidate_pools(*slots: dict) -> dict:
    return {"GPP": {"strength_slots": list(slots)}}


def _selected_candidate_pools(weekly: dict, *slots: dict) -> dict:
    pools = _candidate_pools(*slots)
    compose_normal_strength_assignments(weekly_role_map=weekly, candidate_pools=pools)
    return pools


def _role(weekly: dict) -> dict:
    return weekly["weeks"][0]["session_roles"][0]


def test_one_day_before_effective_hard_contact_marks_strength_and_reuses_retention_cap() -> None:
    weekly = _map()
    apply_pre_hard_contact_strength_exposure_cap(weekly)
    role = _role(weekly)
    assert role["pre_hard_contact_managed_stress"] is True
    assert role["pre_hard_contact_effective_hard_distance"] == 1

    apply_effective_strength_prescriptions(
        weekly_role_map=weekly,
        candidate_pools=_selected_candidate_pools(weekly, _anchor_slot()),
        athlete_model={"fatigue": "low"},
    )
    role = _role(weekly)
    assert role["strength_dose_cap"] == {
        "max_sets": 3,
        "max_reps": 3,
        "loaded_allowed": True,
    }
    assert role["rpe_cap"] == "6-7"
    assert role["effective_strength_prescriptions"][0]["effective_prescription"] == "3 x 3 @ RPE 6 max"


def test_two_days_before_effective_hard_contact_does_not_trigger() -> None:
    weekly = _map(strength_day="Sunday", strength_d_day=24, hard_d_day=22)
    before = deepcopy(weekly)
    apply_pre_hard_contact_strength_exposure_cap(weekly)
    assert weekly == before

    apply_effective_strength_prescriptions(
        weekly_role_map=weekly,
        candidate_pools=_selected_candidate_pools(weekly, _anchor_slot()),
        athlete_model={"fatigue": "low"},
    )
    role = _role(weekly)
    assert "pre_hard_contact_managed_stress" not in role
    # The pre-hard-contact cap did not trigger, so the selected bank dose is
    # carried forward unchanged: exercise-bank authority, no scheduled-day
    # overlay and no dose cap.
    assert "strength_dose_cap" not in role
    presc = role["effective_strength_prescriptions"][0]
    assert presc["effective_prescription"] == "3 x 10 @ 65% 1RM, tempo 3-1-1"
    assert presc["dose_authority"] == "exercise_bank"


def test_declared_contact_resolved_to_technical_does_not_trigger() -> None:
    weekly = _map(load="technical")
    before = deepcopy(weekly)
    apply_pre_hard_contact_strength_exposure_cap(weekly)
    assert weekly == before


def test_pre_hard_week_keeps_one_strength_role_and_records_suppression() -> None:
    secondary = _strength("Thursday", session_index=2, strength_session_index=2)
    weekly = _map(extra_roles=[secondary])
    apply_pre_hard_contact_strength_exposure_cap(weekly)

    week = weekly["weeks"][0]
    strength_roles = [role for role in week["session_roles"] if role.get("category") == "strength"]
    assert len(strength_roles) == 1
    assert strength_roles[0]["role_key"] == "primary_strength_day"
    assert strength_roles[0]["pre_hard_contact_managed_stress"] is True

    suppressed = [row for row in week["suppressed_roles"] if row.get("category") == "strength"]
    assert len(suppressed) == 1
    assert suppressed[0]["compression_reason_codes"] == [PRE_HARD_CONTACT_STRENGTH_CAP_REASON]
    assert suppressed[0]["governance"]["hard_suppression_reasons"]
    assert PRE_HARD_CONTACT_STRENGTH_CAP_REASON in week["intentional_compression"]["reason_codes"]


def test_pre_hard_retention_cap_stacks_with_existing_high_fatigue_reduction() -> None:
    weekly = _map()
    apply_pre_hard_contact_strength_exposure_cap(weekly)
    apply_effective_strength_prescriptions(
        weekly_role_map=weekly,
        candidate_pools=_selected_candidate_pools(weekly, _anchor_slot()),
        athlete_model={"fatigue": "high"},
    )
    assert _role(weekly)["effective_strength_prescriptions"][0]["effective_prescription"] == "2 x 3 @ RPE 6 max"


def test_stricter_existing_countdown_cap_wins_over_pre_hard_retention_cap() -> None:
    weekly = _map()
    apply_pre_hard_contact_strength_exposure_cap(weekly)
    role = _role(weekly)
    role.update(
        {
            "strength_dose_cap": {"max_sets": 2, "max_reps": 3, "loaded_allowed": True},
            "rpe_cap": "6-7",
            "dose_adjustment_reason": "late_camp_reduced_strength_maintenance",
        }
    )
    apply_effective_strength_prescriptions(
        weekly_role_map=weekly,
        candidate_pools=_selected_candidate_pools(weekly, _anchor_slot()),
        athlete_model={"fatigue": "low"},
    )
    assert _role(weekly)["effective_strength_prescriptions"][0]["effective_prescription"] == "2 x 3 @ RPE 6 max"
    assert _role(weekly)["dose_adjustment_reason"] == "late_camp_reduced_strength_maintenance"


def test_pre_hard_allow_list_drops_high_impact_power_but_keeps_anchor() -> None:
    weekly = _map()
    apply_pre_hard_contact_strength_exposure_cap(weekly)
    apply_effective_strength_prescriptions(
        weekly_role_map=weekly,
        candidate_pools=_selected_candidate_pools(weekly, _anchor_slot(), _high_cost_power_slot()),
        athlete_model={"fatigue": "low"},
    )
    role = _role(weekly)
    names = [item["name"] for item in role["effective_strength_prescriptions"]]
    assert names == ["Romanian Deadlift (RDL)"]
    assert [item["name"] for item in role["selected_exercise_assignments"]] == [
        "Romanian Deadlift (RDL)"
    ]
    envelope = role["effective_strength_envelope"]
    assert envelope["complete_exercise_allow_list"] is True
    assert envelope["allowed_exercise_names"] == ["Romanian Deadlift (RDL)"]
    assert envelope["forbid_slow_eccentric_emphasis"] is True


def test_pre_hard_allow_list_permits_one_low_cost_support_item() -> None:
    weekly = _map()
    apply_pre_hard_contact_strength_exposure_cap(weekly)
    apply_effective_strength_prescriptions(
        weekly_role_map=weekly,
        candidate_pools=_selected_candidate_pools(weekly, _anchor_slot(), _low_cost_support_slot()),
        athlete_model={"fatigue": "low"},
    )
    names = [item["name"] for item in _role(weekly)["effective_strength_prescriptions"]]
    assert names == ["Romanian Deadlift (RDL)", "Anti-Rotation Hold"]


def test_clean_week_without_next_day_hard_contact_is_unchanged() -> None:
    weekly = _map(strength_day="Thursday", strength_d_day=20, hard_day="Tuesday", hard_d_day=22)
    before = deepcopy(weekly)
    apply_pre_hard_contact_strength_exposure_cap(weekly)
    assert weekly == before


def test_goal_deferral_contract_recognises_pre_hard_strength_cap() -> None:
    from fightcamp.goal_preservation import _deferral_constraints

    brief = {
        "athlete_snapshot": {"days_until_fight": 24},
        "weekly_role_map": {
            "weeks": [
                {
                    "week_index": 1,
                    "calendar_days": [{"weekday": "Monday", "d_day": 23}],
                    "session_roles": [],
                    "suppressed_roles": [
                        {
                            "category": "strength",
                            "role_key": "secondary_strength_day",
                            "compression_reason_codes": [
                                PRE_HARD_CONTACT_STRENGTH_CAP_REASON
                            ],
                        }
                    ],
                }
            ]
        },
    }
    missing = [{"min_d_day": 20, "max_d_day": 24}]
    constraints = _deferral_constraints({"goal": "strength"}, brief, missing)
    assert constraints
    assert constraints[0]["reason_code"] == "pre_hard_contact_managed_stress"
    assert constraints[0]["source_reason_code"] == PRE_HARD_CONTACT_STRENGTH_CAP_REASON



def test_final_integrity_relocates_pre_hard_strength_when_allow_destination_exists() -> None:
    weekly = _map()
    apply_late_camp_role_morph(weekly)
    role = _role(weekly)
    assert role["scheduled_day_hint"] == "Thursday"
    assert "pre_hard_contact_managed_stress" not in role


def test_pre_hard_policy_applies_after_final_integrity_when_no_allow_destination_exists() -> None:
    weekly = _map()
    week = weekly["weeks"][0]
    week["declared_training_days"] = ["Monday", "Tuesday"]
    week["calendar_days"] = _calendar(("Monday", 23), ("Tuesday", 22))
    apply_late_camp_role_morph(weekly)
    role = _role(weekly)
    assert role["scheduled_day_hint"] == "Monday"
    assert role["pre_hard_contact_managed_stress"] is True
    assert role["pre_hard_contact_effective_hard_distance"] == 1


def test_normal_pre_hard_policy_does_not_take_ownership_inside_d13() -> None:
    weekly = _map(strength_d_day=13, hard_d_day=12)
    before = deepcopy(weekly)
    apply_pre_hard_contact_strength_exposure_cap(weekly)
    assert weekly == before


def test_unknown_cost_support_is_not_treated_as_verified_low_cost() -> None:
    weekly = _map()
    apply_pre_hard_contact_strength_exposure_cap(weekly)
    unknown_support = _low_cost_support_slot()
    unknown_support["selected"].pop("selection_metadata", None)
    apply_effective_strength_prescriptions(
        weekly_role_map=weekly,
        candidate_pools=_selected_candidate_pools(weekly, _anchor_slot(), unknown_support),
        athlete_model={"fatigue": "low"},
    )
    role = _role(weekly)
    names = [item["name"] for item in role["effective_strength_prescriptions"]]
    assert names == ["Romanian Deadlift (RDL)"]
    assert [item["name"] for item in role["selected_exercise_assignments"]] == [
        "Romanian Deadlift (RDL)"
    ]


def test_repair_like_second_strength_role_is_recompressed_by_final_morph() -> None:
    weekly = _map()
    week = weekly["weeks"][0]
    week["declared_training_days"] = ["Monday", "Tuesday"]
    week["calendar_days"] = _calendar(("Monday", 23), ("Tuesday", 22))

    apply_late_camp_role_morph(weekly)
    assert _role(weekly)["pre_hard_contact_managed_stress"] is True

    # Simulate a retained goal-repair candidate being appended after the first
    # resolved pass. The repair trial calls the same morph again.
    week["session_roles"].append(
        _strength("Monday", session_index=2, strength_session_index=2)
    )
    apply_late_camp_role_morph(weekly)

    strength_roles = [
        role for role in week["session_roles"] if role.get("category") == "strength"
    ]
    assert len(strength_roles) == 1
    assert strength_roles[0]["role_key"] == "primary_strength_day"
    assert strength_roles[0]["pre_hard_contact_managed_stress"] is True
    policy = week["pre_hard_contact_strength_policy"]
    assert policy["active"] is True
    assert policy["max_meaningful_strength_exposures"] == 1


def test_complete_pre_hard_allow_list_blocks_removed_exercise_in_render() -> None:
    brief = {
        "weekly_role_map": {
            "weeks": [
                {
                    "week_index": 1,
                    "session_roles": [
                        {
                            "category": "strength",
                            "role_key": "primary_strength_day",
                            "effective_strength_envelope": {
                                "scheduled_d_day": 23,
                                "loaded_allowed": True,
                                "rpe_cap_high": 7,
                                "max_sets": 3,
                                "max_reps": 3,
                                "complete_exercise_allow_list": True,
                                "allowed_exercise_names": ["Romanian Deadlift (RDL)"],
                                "dose_adjustment_reason": "pre_hard_contact_strength_retention",
                            },
                            "effective_strength_prescriptions": [
                                {
                                    "name": "Romanian Deadlift (RDL)",
                                    "dose_role_kind": "anchor",
                                    "effective_loaded": True,
                                    "effective_max_sets": 3,
                                    "effective_max_reps": 3,
                                    "effective_rpe_cap": 7,
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    }
    rendered = "\n".join(
        [
            "D-23 (Monday) — Strength",
            "- Romanian Deadlift (RDL): 3 x 3 @ RPE 6",
            "- Single-Leg Forward Hops: 3 x 5",
        ]
    )
    warnings = _late_camp_effective_prescription_warnings(brief, rendered)
    allow_list_findings = [
        item
        for item in warnings
        if "exercise_allow_list" in (item.get("violation_dimensions") or [])
    ]
    assert len(allow_list_findings) == 1
    assert allow_list_findings[0]["code"] == "late_camp_effective_prescription_exceeded"
    assert allow_list_findings[0]["rendered_exercise"] == "Single-Leg Forward Hops"


# PR 2: only split a dense main session when at least TWO safe exercises
# can move. Exercise identities and aggregate weekly membership are conserved.
def _secondary_power_geometry() -> dict:
    primary = _strength("Saturday", session_index=1, strength_session_index=1)
    secondary = _strength("Monday", session_index=2, strength_session_index=2)
    week = {
        "week_index": 1,
        "phase": "GPP",
        "declared_training_days": ["Monday", "Tuesday", "Thursday", "Saturday"],
        "calendar_days": _calendar(
            ("Monday", 23), ("Tuesday", 22), ("Thursday", 20), ("Saturday", 18)
        ),
        "hard_sparring_plan": [_hard("Tuesday"), _hard("Thursday")],
        "effective_hard_sparring_days": ["Tuesday", "Thursday"],
        "session_roles": [primary, secondary],
        "suppressed_roles": [],
    }
    return {"model": "session_role_overlay.v1", "weeks": [week]}


def _lower_power_slot(name: str, suffix: int, *, cost: str = "low", with_cost: bool = True) -> dict:
    metadata = {"primary_adaptation": "power"}
    if with_cost:
        metadata.update(
            impact_cost=cost, landing_cost="none", eccentric_cost="low",
            soreness_risk="low",
        )
    return {
        "slot_id": f"gpp-lower-power-{suffix}",
        "session_index": 1,
        "role": "lower_power",
        "quality_class": "anchor_power",
        "base_categories": ["lower_body_power"],
        "selected": {
            "name": name,
            "quality_class": "anchor_power",
            "prescription": "4 x 3",
            "mechanical_risk_tags": ["mech_lower_power", "mech_ballistic"],
            "selection_metadata": metadata,
        },
    }


def _transfer_slots(cost: str = "low", with_cost: bool = True) -> tuple[dict, ...]:
    return (
        _anchor_slot(),
        _high_cost_power_slot(),
        _low_cost_support_slot(),
        _lower_power_slot("Low-Impact Band Drive", 1, cost=cost, with_cost=with_cost),
        _lower_power_slot("Low-Impact Step Push", 2, cost=cost, with_cost=with_cost),
    )


def _seed_main(weekly: dict, *slots: dict) -> None:
    main = weekly["weeks"][0]["session_roles"][0]
    main["selected_exercise_assignments"] = [
        assignment_from_slot("GPP", "strength_slots", slot) for slot in slots
    ]


def test_redistribution_keeps_the_closed_core_component_on_its_host() -> None:
    weekly = _secondary_power_geometry()
    slots = _transfer_slots()
    _seed_main(weekly, *slots)
    main = weekly["weeks"][0]["session_roles"][0]
    # All transferable-looking members belong to one existing three-member
    # core component. Peeling off two destroys its complementary coverage.
    for member in main["selected_exercise_assignments"][2:]:
        member["component_group"] = "core_development"
    before = deepcopy(main["selected_exercise_assignments"])
    athlete = {"primary_goal": "power", "fatigue": "low"}
    apply_pre_hard_contact_strength_exposure_cap(weekly, athlete)
    redistribute_pre_hard_power_from_main(
        weekly_role_map=weekly, candidate_pools=_candidate_pools(*slots), athlete_model=athlete,
    )
    assert main["selected_exercise_assignments"] == before
    assert not any(r.get("pre_hard_contact_microdose_only")
                   for r in weekly["weeks"][0]["session_roles"])


def test_dense_main_rebalances_two_same_region_exercises_without_more_weekly_work() -> None:
    weekly = _secondary_power_geometry()
    athlete = {"primary_goal": "power", "fatigue": "low", "cut_severity_bucket": "none"}
    slots = _transfer_slots()
    _seed_main(weekly, *slots)
    apply_pre_hard_contact_strength_exposure_cap(weekly, athlete)
    roles = weekly["weeks"][0]["session_roles"]
    assert [r["scheduled_day_hint"] for r in roles] == ["Saturday", "Monday"]
    before = [a["name"] for a in roles[0]["selected_exercise_assignments"]]

    redistribute_pre_hard_power_from_main(
        weekly_role_map=weekly,
        candidate_pools=_candidate_pools(*slots),
        athlete_model=athlete,
    )
    main, monday = roles
    after = [a["name"] for a in main["selected_exercise_assignments"]]
    transferred = [a["name"] for a in monday["selected_exercise_assignments"]]
    assert set(transferred) == {"Low-Impact Band Drive", "Low-Impact Step Push"}
    assert sorted(before) == sorted(after + transferred)
    assert len(after) == 3
    assert weekly["weeks"][0]["pre_hard_power_redistribution"]["weekly_exercise_count_delta"] == 0

    redistribute_pre_hard_power_from_main(
        weekly_role_map=weekly,
        candidate_pools=_candidate_pools(*slots),
        athlete_model=athlete,
    )
    assert [a["name"] for a in main["selected_exercise_assignments"]] == after
    assert [a["name"] for a in monday["selected_exercise_assignments"]] == transferred

    apply_effective_strength_prescriptions(
        weekly_role_map=weekly, candidate_pools=_candidate_pools(*slots),
        athlete_model=athlete,
    )
    resolved = monday["effective_strength_prescriptions"]
    assert len(resolved) == 2
    assert all(item["effective_prescription"].startswith("2 x 2") for item in resolved)
    assert all(item["effective_loaded"] is False for item in resolved)
    assert monday["strength_dose_cap"]["loaded_allowed"] is False
    assert not set(transferred) & {
        item["name"] for item in main["effective_strength_prescriptions"]
    }


def test_six_exercise_day_can_split_three_but_never_four() -> None:
    weekly = _secondary_power_geometry()
    athlete = {"primary_goal": "power", "fatigue": "low"}
    extra = _lower_power_slot("Low-Impact Band Acceleration", 3)
    slots = (*_transfer_slots(), extra)
    _seed_main(weekly, *slots)
    apply_pre_hard_contact_strength_exposure_cap(weekly, athlete)
    redistribute_pre_hard_power_from_main(
        weekly_role_map=weekly,
        candidate_pools=_candidate_pools(*slots),
        athlete_model=athlete,
    )
    main, monday = weekly["weeks"][0]["session_roles"]
    assert len(main["selected_exercise_assignments"]) == 3
    assert len(monday["selected_exercise_assignments"]) == 3
    assert all(
        "Low-Impact" in item["name"]
        for item in monday["selected_exercise_assignments"]
    )


@pytest.mark.parametrize(
    "athlete",
    [
        {"primary_goal": "power", "fatigue": "high"},
        {"primary_goal": "power", "cut_severity_bucket": "high"},
        {"primary_goal": "power", "injuries": ["knee pain"]},
        {"primary_goal": "power", "readiness_flags": ["injury_management"]},
    ],
)
def test_pre_contact_second_strength_remains_suppressed_when_constrained(athlete) -> None:
    weekly = _secondary_power_geometry()
    apply_pre_hard_contact_strength_exposure_cap(weekly, athlete)
    assert [r["scheduled_day_hint"] for r in weekly["weeks"][0]["session_roles"]] == ["Saturday"]


@pytest.mark.parametrize("cost,with_cost", [("high", True), ("low", False)])
def test_fewer_than_two_safe_transfers_keeps_entire_main_and_no_monday(cost, with_cost) -> None:
    weekly = _secondary_power_geometry()
    slots = _transfer_slots(cost=cost, with_cost=with_cost)
    _seed_main(weekly, *slots)
    main = weekly["weeks"][0]["session_roles"][0]
    before = [m["name"] for m in main["selected_exercise_assignments"]]
    athlete = {"primary_goal": "power", "fatigue": "low"}
    apply_pre_hard_contact_strength_exposure_cap(weekly, athlete)
    redistribute_pre_hard_power_from_main(
        weekly_role_map=weekly,
        candidate_pools=_candidate_pools(*slots),
        athlete_model=athlete,
    )
    assert [r["scheduled_day_hint"] for r in weekly["weeks"][0]["session_roles"]] == ["Saturday"]
    assert [m["name"] for m in main["selected_exercise_assignments"]] == before
    assert weekly["weeks"][0]["pre_hard_power_redistribution"]["status"] == "skipped"


def test_no_split_when_main_not_dense() -> None:
    weekly = _secondary_power_geometry()
    slots = _transfer_slots()[:4]
    _seed_main(weekly, *slots)
    athlete = {"fatigue": "low"}
    apply_pre_hard_contact_strength_exposure_cap(weekly, athlete)
    redistribute_pre_hard_power_from_main(
        weekly_role_map=weekly,
        candidate_pools=_candidate_pools(*slots),
        athlete_model=athlete,
    )
    assert len(weekly["weeks"][0]["session_roles"]) == 1
    assert len(weekly["weeks"][0]["session_roles"][0]["selected_exercise_assignments"]) == 4


def test_split_does_not_promote_unverified_secondary_before_composition() -> None:
    from fightcamp.stage2_role_map import _non_spar_role_priority_rank

    secondary = {"category": "strength", "role_key": "secondary_strength_day"}
    fresh = {"primary_goal": "strength", "fatigue": "low", "injuries": []}
    assert _non_spar_role_priority_rank(secondary, "GPP", True, False, athlete_model=fresh) == 2
    assert _non_spar_role_priority_rank(secondary, "GPP", False, False, athlete_model=fresh) == 2
    assert _non_spar_role_priority_rank(
        secondary, "GPP", True, False, athlete_model={**fresh, "fatigue": "high"}
    ) == 2


def _composed_dense_week_without_secondary() -> tuple[dict, tuple[dict, ...]]:
    """The early role-budget phase removed the secondary BEFORE composition."""
    weekly = _secondary_power_geometry()
    week = weekly["weeks"][0]
    secondary = week["session_roles"].pop()
    week["suppressed_roles"].append({
        "category": "strength",
        "role_key": secondary["role_key"],
        "compression_reason_codes": ["spar_first_cap"],
    })
    slots = _transfer_slots()
    _seed_main(weekly, *slots)
    return weekly, slots


def test_role_budget_can_reserve_pre_spar_receiver_after_secondary_was_dropped() -> None:
    from fightcamp.stage2_role_map import reserve_dense_day_receiver

    weekly, slots = _composed_dense_week_without_secondary()
    athlete = {
        "training_days": ["Monday", "Tuesday", "Thursday", "Saturday"],
        "hard_sparring_days": ["Tuesday", "Thursday"],
        "training_frequency": 4,
        "primary_goal": "power",
        "fatigue": "low",
        "cut_severity_bucket": "none",
    }
    # The past compression decision no longer kills a possible transfer:
    # composition made source density visible after budget selection.
    reserve_dense_day_receiver(weekly, athlete)
    assert len(weekly["weeks"][0]["session_roles"]) == 2
    assert weekly["weeks"][0]["session_roles"][1]["scheduled_day_hint"] == "Monday"
    apply_pre_hard_contact_strength_exposure_cap(weekly, athlete)
    redistribute_pre_hard_power_from_main(
        weekly_role_map=weekly, candidate_pools=_candidate_pools(*slots),
        athlete_model=athlete,
    )
    main, monday = weekly["weeks"][0]["session_roles"]
    assert len(main["selected_exercise_assignments"]) == 3
    assert len(monday["selected_exercise_assignments"]) == 2
    assert weekly["weeks"][0]["pre_hard_power_redistribution"]["status"] == "transferred"


def test_post_composition_reservation_does_not_invent_capacity() -> None:
    from fightcamp.stage2_role_map import reserve_dense_day_receiver

    weekly, _ = _composed_dense_week_without_secondary()
    athlete = {
        "training_days": ["Monday", "Tuesday", "Thursday", "Saturday"],
        "hard_sparring_days": ["Tuesday", "Thursday"],
        "training_frequency": 1,
        "primary_goal": "power",
        "fatigue": "low",
    }
    reserve_dense_day_receiver(weekly, athlete)
    assert len(weekly["weeks"][0]["session_roles"]) == 1
    assert "dense_day_receiver_proposal" not in weekly["weeks"][0]


def test_post_composition_reservation_requires_five_actual_selected_exercises() -> None:
    from fightcamp.stage2_role_map import reserve_dense_day_receiver

    weekly, _ = _composed_dense_week_without_secondary()
    weekly["weeks"][0]["session_roles"][0]["selected_exercise_assignments"] = (
        weekly["weeks"][0]["session_roles"][0]["selected_exercise_assignments"][:4]
    )
    athlete = {
        "training_days": ["Monday", "Tuesday", "Thursday", "Saturday"],
        "hard_sparring_days": ["Tuesday", "Thursday"],
        "training_frequency": 4,
        "fatigue": "low",
    }
    reserve_dense_day_receiver(weekly, athlete)
    assert len(weekly["weeks"][0]["session_roles"]) == 1


def test_post_composition_reservation_refuses_injury_management() -> None:
    from fightcamp.stage2_role_map import reserve_dense_day_receiver

    weekly, _ = _composed_dense_week_without_secondary()
    athlete = {
        "training_days": ["Monday", "Tuesday", "Thursday", "Saturday"],
        "hard_sparring_days": ["Tuesday", "Thursday"],
        "training_frequency": 4,
        "fatigue": "low",
        "injuries": ["Achilles pain"],
    }
    reserve_dense_day_receiver(weekly, athlete)
    assert len(weekly["weeks"][0]["session_roles"]) == 1


def test_split_restores_original_main_when_final_governor_moves_receiver() -> None:
    from fightcamp.stage2_role_map import reserve_dense_day_receiver
    from fightcamp.session_composition import rollback_invalid_dense_day_redistributions

    weekly, slots = _composed_dense_week_without_secondary()
    athlete = {
        "training_days": ["Monday", "Tuesday", "Thursday", "Saturday"],
        "hard_sparring_days": ["Tuesday", "Thursday"],
        "training_frequency": 4,
        "primary_goal": "power",
        "fatigue": "low",
    }
    original = [
        dict(item) for item in weekly["weeks"][0]["session_roles"][0]["selected_exercise_assignments"]
    ]
    reserve_dense_day_receiver(weekly, athlete)
    apply_pre_hard_contact_strength_exposure_cap(weekly, athlete)
    redistribute_pre_hard_power_from_main(
        weekly_role_map=weekly, candidate_pools=_candidate_pools(*slots),
        athlete_model=athlete,
    )
    week = weekly["weeks"][0]
    assert week["pre_hard_power_redistribution"]["status"] == "transferred"
    week["session_roles"][1]["scheduled_d_day"] = 21
    week["session_roles"][1]["scheduled_countdown_label"] = "D-21"
    assert rollback_invalid_dense_day_redistributions(
        weekly, athlete_model=athlete
    )
    assert [item["name"] for item in week["session_roles"][0]["selected_exercise_assignments"]] == [
        item["name"] for item in original
    ]
    assert len(week["session_roles"]) == 1
    assert week["pre_hard_power_redistribution"]["status"] == "rolled_back"

def test_meaningful_power_and_capped_trunk_anchor_move_as_two_existing_exercises() -> None:
    """Saved-plan regression: a meaningful med-ball exercise is movable.

    A moderate-eccentric anti-rotation exercise can be reduced to technique,
    but high-soreness/high-impact lower-body exercises stay on the main day.
    The same rule applies to future plans; no athlete/weekday names are encoded
    in the production allocator.
    """
    weekly = _secondary_power_geometry()
    athlete = {"primary_goal": "power", "fatigue": "low"}
    med_ball = {
        "slot_id": "gpp-meaningful-medball",
        "session_index": 1,
        "role": "core",
        "quality_class": "anchor_power",
        "selected": {
            "name": "Med-Ball Rotational Slam",
            "quality_class": "anchor_power",
            "prescription": "4 x 2 reps at max speed; full rest 120s.",
            "meaningful_stress": True,
            "support_only": False,
            "mechanical_risk_tags": [
                "mech_ballistic", "mech_trunk_rotation", "mech_trunk_stability",
            ],
            "selection_metadata": {
                "impact_cost": "low", "landing_cost": "none",
                "eccentric_cost": "low", "soreness_risk": "low",
            },
        },
    }
    anti_rotation = {
        "slot_id": "gpp-meaningful-trunk-anchor",
        "session_index": 1,
        "role": "horizontal_push",
        "quality_class": "anchor_loaded",
        "selected": {
            "name": "Landmine Anti-Rotation Press",
            "quality_class": "anchor_loaded",
            "prescription": "2-4 sets x 6-10 reps; RPE 6-8.",
            "meaningful_stress": True,
            "support_only": False,
            "mechanical_risk_tags": [
                "anti_rotation", "loaded_rotation",
                "mech_trunk_stability", "mech_upper_press",
            ],
            "selection_metadata": {
                "impact_cost": "low", "landing_cost": "none",
                "eccentric_cost": "moderate", "soreness_risk": "low",
            },
        },
    }
    high = _high_cost_power_slot()
    high2 = deepcopy(high)
    high2["slot_id"] = "gpp-high-jump-2"
    high2["selected"]["name"] = "Single-Leg Depth Drop"
    high3 = deepcopy(high)
    high3["slot_id"] = "gpp-high-jump-3"
    high3["selected"]["name"] = "Trap Bar Jump"
    slots = (_anchor_slot(), med_ball, high, high2, anti_rotation, high3)
    _seed_main(weekly, *slots)
    main = weekly["weeks"][0]["session_roles"][0]
    before = [item["name"] for item in main["selected_exercise_assignments"]]
    apply_pre_hard_contact_strength_exposure_cap(weekly, athlete)
    redistribute_pre_hard_power_from_main(
        weekly_role_map=weekly, candidate_pools=_candidate_pools(*slots),
        athlete_model=athlete,
    )
    main, receiver = weekly["weeks"][0]["session_roles"]
    moved = [item["name"] for item in receiver["selected_exercise_assignments"]]
    kept = [item["name"] for item in main["selected_exercise_assignments"]]
    assert set(moved) == {"Med-Ball Rotational Slam", "Landmine Anti-Rotation Press"}
    assert sorted(before) == sorted(moved + kept)
    assert len(kept) == 4 and len(moved) == 2
    assert weekly["weeks"][0]["pre_hard_power_redistribution"]["weekly_exercise_count_delta"] == 0

    apply_effective_strength_prescriptions(
        weekly_role_map=weekly, candidate_pools=_candidate_pools(*slots),
        athlete_model=athlete,
    )
    prescriptions = {
        p["name"]: p for p in receiver["effective_strength_prescriptions"]
    }
    assert set(prescriptions) == set(moved)
    assert prescriptions["Med-Ball Rotational Slam"]["effective_prescription"].startswith("2 x 2")
    assert prescriptions["Landmine Anti-Rotation Press"]["effective_prescription"].startswith("2 x 6")
    assert all(p["effective_loaded"] is False for p in prescriptions.values())


def test_transfer_still_blocks_high_cost_and_cns_even_with_meaningful_power() -> None:
    from fightcamp.prescription_resolver import _pre_hard_transfer_eligible

    moderate = _lower_power_slot("Low-Cost Power", 9, cost="low")
    moderate["selected"]["meaningful_stress"] = True
    moderate["selected"]["selection_metadata"]["eccentric_cost"] = "moderate"
    assert _pre_hard_transfer_eligible(moderate)
    for field in ("impact_cost", "landing_cost", "eccentric_cost", "soreness_risk"):
        bad = deepcopy(moderate)
        bad["selected"]["selection_metadata"][field] = "high"
        assert not _pre_hard_transfer_eligible(bad)
    cns = deepcopy(moderate)
    cns["selected"]["mechanical_risk_tags"] = ["mech_cns_high"]
    assert not _pre_hard_transfer_eligible(cns)




@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Med-Ball Rotational Slam", True),
        ("Landmine Push Press", True),
        ("Med Ball Scoop Toss", False),  # CNS high, even when landing is none
        ("Anti-Rotation Med Ball Slam", False),  # CNS high
        ("DB Clean & Press", False),  # CNS and movement cost high
        ("Sled Push", False),  # movement_cost high but impact low
        ("Battle Rope Slams", False),  # systemic fatigue and movement cost high
        ("Trap Bar Jump", False),  # landing / soreness high
        ("Speed Box Squat", False),  # cns high, missing mech_cns_high tag
        ("Single-Leg Forward Hops", False),  # landing high
        ("Slow-Lowered Pull-Up", False),  # unknown eccentric cost
    ],
)
def test_bank_tag_audit_drives_shared_pre_contact_transfer_policy(
    name: str, expected: bool,
) -> None:
    """Real exercise bank regression, not a hand-picked plan or mocked cost."""
    import json
    from pathlib import Path

    from fightcamp.prescription_resolver import _pre_hard_transfer_eligible

    path = Path(__file__).resolve().parents[1] / "data" / "exercise_bank.json"
    bank = json.loads(path.read_text(encoding="utf-8"))
    record = next(item for item in bank if item["name"] == name)
    # Strength roles already arrive with Stage 1's classified source slot.
    slot = {
        "slot_id": "audited-" + name.lower().replace(" ", "-"),
        "quality_class": "anchor_power",
        "role": record.get("movement"),
        "selected": {
            **record,
            "selection_metadata": {
                field: record.get(field)
                for field in (
                    "impact_cost", "landing_cost", "eccentric_cost",
                    "soreness_risk", "movement_cost", "cns_load",
                    "contact_cost", "lactate_load",
                )
            },
        },
    }
    assert _pre_hard_transfer_eligible(slot) is expected


def test_transfer_respects_explicit_cns_without_mechanical_tag() -> None:
    from fightcamp.prescription_resolver import _pre_hard_transfer_eligible

    slot = _lower_power_slot("No CNS tag but high explicit load", 16)
    slot["selected"]["selection_metadata"]["cns_load"] = "high"
    assert not _pre_hard_transfer_eligible(slot)
    slot["selected"]["selection_metadata"]["cns_load"] = "moderate"
    slot["selected"]["selection_metadata"]["movement_cost"] = "high"
    assert not _pre_hard_transfer_eligible(slot)
    slot["selected"]["selection_metadata"]["movement_cost"] = "moderate"
    assert _pre_hard_transfer_eligible(slot)
    # A high-risk tag held only in Stage 1's provenance must still veto.
    slot["selected"]["selection_metadata"]["mechanical_risk_tags"] = ["mech_cns_high"]
    assert not _pre_hard_transfer_eligible(slot)
    slot["selected"]["selection_metadata"]["mechanical_risk_tags"] = []
    slot["selected"]["selection_metadata"]["impact_cost"] = "very_low"
    slot["selected"]["selection_metadata"]["landing_cost"] = "not applicable"
    assert _pre_hard_transfer_eligible(slot)


def test_transfer_timed_isometric_cannot_be_invented_as_reps() -> None:
    weekly = _secondary_power_geometry()
    slots = list(_transfer_slots())
    slots[-1] = deepcopy(slots[-1])
    slots[-1]["selected"]["name"] = "Timed Trunk Brace"
    slots[-1]["selected"]["prescription"] = "2 x 20s hold"
    slots[-1]["slot_id"] = "timed-brace"
    _seed_main(weekly, *slots)
    athlete = {"primary_goal": "power", "fatigue": "low"}
    apply_pre_hard_contact_strength_exposure_cap(weekly, athlete)
    redistribute_pre_hard_power_from_main(
        weekly_role_map=weekly,
        candidate_pools=_candidate_pools(*slots), athlete_model=athlete,
    )
    # May still choose two OTHER eligible exercises, but a timed hold cannot
    # be reinterpreted as twenty full repetitions by the transfer parser.
    week = weekly["weeks"][0]
    receivers = [
        r for r in week["session_roles"]
        if r.get("pre_hard_contact_microdose_only")
    ]
    assert all(
        a["name"] != "Timed Trunk Brace"
        for r in receivers
        for a in r.get("selected_exercise_assignments", [])
    )


def test_any_declared_pre_hard_weekday_can_receive_not_only_monday() -> None:
    from fightcamp.stage2_role_map import reserve_dense_day_receiver

    weekly, slots = _composed_dense_week_without_secondary()
    week = weekly["weeks"][0]
    week["calendar_days"].append({"weekday": "Wednesday", "d_day": 21})
    # The resolved Tuesday is technical, leaving Wednesday eligible before
    # Thursday. Two effective hard contacts still protect the narrow gap.
    week["hard_sparring_plan"][0] = _hard("Tuesday", load="technical", status="convert_to_technical_suggested")
    week["declared_training_days"] = ["Wednesday", "Tuesday", "Thursday", "Saturday"]
    athlete = {
        "training_days": ["Wednesday", "Tuesday", "Thursday", "Saturday"],
        "hard_sparring_days": ["Tuesday", "Thursday"],
        "training_frequency": 5,
        "primary_goal": "power", "fatigue": "low", "cut_severity_bucket": "none",
    }
    reserve_dense_day_receiver(weekly, athlete)
    assert [r["scheduled_day_hint"] for r in week["session_roles"]] == ["Saturday", "Wednesday"]
    apply_pre_hard_contact_strength_exposure_cap(weekly, athlete)
    redistribute_pre_hard_power_from_main(
        weekly_role_map=weekly, candidate_pools=_candidate_pools(*slots),
        athlete_model=athlete,
    )
    assert len(week["session_roles"]) == 2
    main, wednesday = week["session_roles"]
    assert len(main["selected_exercise_assignments"]) == 3
    assert len(wednesday["selected_exercise_assignments"]) == 2
    assert week["pre_hard_power_redistribution"]["status"] == "transferred"


def test_dense_receiver_uses_its_seven_day_window_in_eight_day_phase_week():
    from fightcamp.stage2_role_map import reserve_dense_day_receiver

    weekly, _ = _composed_dense_week_without_secondary()
    week = weekly["weeks"][0]
    week["calendar_days"] = _calendar(
        ("Saturday", 25), ("Sunday", 24), ("Monday", 23), ("Tuesday", 22),
        ("Wednesday", 21), ("Thursday", 20), ("Friday", 19), ("Saturday", 18),
    )
    week["session_roles"].extend(
        {"role_key": "light_combat_day", "category": "technical", "countdown_offset": d}
        for d in (22, 21, 20, 19)
    )
    # Five days across the phase span, but only four in D-25..D-19.
    # The dense donor at D-18 belongs to the next cadence window.
    reserve_dense_day_receiver(weekly, {
        "training_days": ["Monday", "Tuesday", "Thursday", "Saturday"],
        "hard_sparring_days": ["Tuesday", "Thursday"],
        "training_frequency": 5, "primary_goal": "power", "fatigue": "low",
        "cut_severity_bucket": "none",
    })
    receivers = [r for r in week["session_roles"] if r.get("dense_day_receiver_provisional")]
    assert len(receivers) == 1
    assert receivers[0]["scheduled_d_day"] == 23


def test_composition_leaves_pre_hard_receivers_for_conserving_redistribution() -> None:
    """A provisional microdose must not become an independently composed token."""
    weekly = _secondary_power_geometry()
    slots = list(_transfer_slots())
    _seed_main(weekly, *slots)
    athlete = {"primary_goal": "power", "fatigue": "low"}
    apply_pre_hard_contact_strength_exposure_cap(weekly, athlete)
    receiver = weekly["weeks"][0]["session_roles"][1]
    assert receiver["pre_hard_contact_microdose_only"] is True
    provisional = deepcopy(slots[-1])
    provisional.update(slot_id="provisional-independent-power", session_index=2)
    provisional["selected"]["name"] = "Independent provisional power"
    pools = _candidate_pools(*slots, provisional)
    original_names = {a["name"] for a in weekly["weeks"][0]["session_roles"][0]["selected_exercise_assignments"]}
    compose_normal_strength_assignments(
        weekly_role_map=weekly, candidate_pools=pools,
        only_roles={id(receiver)}, allow_relocation=False,
    )
    assert receiver.get("selected_exercise_assignments") == []
    redistribute_pre_hard_power_from_main(
        weekly_role_map=weekly, candidate_pools=pools, athlete_model=athlete,
    )
    assert 2 <= len(receiver["selected_exercise_assignments"]) <= 3
    assert all(a["redistribution_reason"] == "pre_hard_session_rebalance"
               for a in receiver["selected_exercise_assignments"])
    after = [a["name"] for r in weekly["weeks"][0]["session_roles"]
             if r.get("category") == "strength" for a in r.get("selected_exercise_assignments", [])]
    assert len(after) == len(set(after)) == len(original_names)
    assert set(after) == original_names
    apply_effective_strength_prescriptions(weekly_role_map=weekly, candidate_pools=pools, athlete_model=athlete)
    before = deepcopy(receiver["selected_exercise_assignments"])
    compose_normal_strength_assignments(
        weekly_role_map=weekly, candidate_pools=pools,
        only_roles={id(receiver)}, allow_relocation=False,
    )
    assert receiver["selected_exercise_assignments"] == before


def test_transfer_records_calendar_day_before_prescriptions_stamp_it() -> None:
    from fightcamp.calendar_context import role_d_day
    from fightcamp.session_composition import rollback_invalid_dense_day_redistributions

    weekly = _secondary_power_geometry()
    slots = _transfer_slots()
    _seed_main(weekly, *slots)
    athlete = {"primary_goal": "power", "fatigue": "low"}
    apply_pre_hard_contact_strength_exposure_cap(weekly, athlete)
    week = weekly["weeks"][0]
    receiver = next(r for r in week["session_roles"] if r.get("pre_hard_contact_microdose_only"))
    receiver.pop("scheduled_d_day", None)
    expected_day = role_d_day(week, receiver)
    assert expected_day is not None
    pools = _candidate_pools(*slots)
    redistribute_pre_hard_power_from_main(weekly_role_map=weekly, candidate_pools=pools, athlete_model=athlete)
    assert week["pre_hard_power_redistribution"]["receiver_original_d_day"] == expected_day
    apply_effective_strength_prescriptions(weekly_role_map=weekly, candidate_pools=pools, athlete_model=athlete)
    before = deepcopy(receiver["selected_exercise_assignments"])
    assert not rollback_invalid_dense_day_redistributions(weekly, athlete_model=athlete)
    assert receiver["selected_exercise_assignments"] == before
