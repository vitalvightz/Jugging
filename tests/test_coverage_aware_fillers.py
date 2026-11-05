from __future__ import annotations

from fightcamp.calendar_context import sequence_legality
from fightcamp.camp_week_fillers import apply_camp_week_fillers
from fightcamp.gap_fill_inserts import (
    LOW_COST_AEROBIC_INSERTS,
    build_target_coverage_state,
    select_gap_fill_insert,
)
from fightcamp.priority_profile import (
    PRIMARY_GOAL_WEIGHT,
    PRIMARY_WEAKNESS_WEIGHT,
    SECONDARY_GOAL_WEIGHT,
    SECONDARY_WEAKNESS_WEIGHT,
)


def _athlete(**overrides):
    athlete = {
        "sport": "boxing",
        "status": "professional",
        "training_days": ["monday", "tuesday", "wednesday", "thursday", "friday"],
        "hard_sparring_days": [],
        "fatigue": "low",
        "fatigue_level": "low",
        "readiness_flags": [],
        "weight_cut_risk": False,
        "weight_cut_pct": 0.0,
        "weaknesses": [],
        "key_goals": [],
        "injuries": [],
        "parsed_injuries": [],
        "guided_injury": None,
        "injury_restrictions": [],
        "days_until_fight": 21,
        "plan_creation_weekday": "monday",
    }
    athlete.update(overrides)
    return athlete


def _meaningful_role(role_key: str, *targets: str) -> dict:
    role = {
        "role_key": role_key,
        "category": "conditioning",
        "governance": {"meaningful_stress": True},
    }
    if targets:
        role["coverage_targets"] = list(targets)
    return role


def _supports(role: dict, target: str) -> bool:
    return float((role.get("support_target_capabilities") or {}).get(target, 0.0)) > 0


def _fight_dated_spp_week(
    *,
    main_d_day: int = 14,
    filler_d_day: int = 12,
    main_role: dict | None = None,
) -> dict:
    role = dict(main_role) if main_role is not None else {
        "role_key": "primary_strength_day",
        "category": "strength",
        "governance": {"meaningful_stress": True},
    }
    role.setdefault("scheduled_day_hint", "Monday")
    role.setdefault("countdown_offset", main_d_day)
    return {
        "phase": "SPP",
        "session_roles": [role],
        "calendar_days": [
            {"weekday": "monday", "d_day": main_d_day},
            {"weekday": "wednesday", "d_day": filler_d_day},
        ],
        "declared_training_days": ["Monday", "Wednesday"],
        "intentionally_unused_days": [{"day": "Wednesday", "role": "off_day"}],
    }


def _discretionary_roles(week: dict) -> list[dict]:
    return [
        role
        for role in week["session_roles"]
        if role.get("camp_week_filler") and role.get("role_key") != "tactical_watch"
    ]


def test_undercovered_primary_weakness_beats_generic_after_goal_is_covered():
    athlete = _athlete(
        weaknesses=["footwork"],
        primary_weak_area="footwork",
        key_goals=["speed"],
        primary_goal="speed",
    )

    insert = select_gap_fill_insert(
        athlete,
        12,
        scheduled_roles=[_meaningful_role("alactic_speed_day")],
    )

    assert insert is not None
    assert _supports(insert, "footwork")


def test_real_technical_footwork_role_prevents_redundant_filler():
    athlete = _athlete(
        weaknesses=["footwork"],
        primary_weak_area="footwork",
    )
    production_role = {
        "role_key": "primary_conditioning_day",
        "category": "conditioning",
        "preferred_system": "technical_footwork",
        "governance": {"meaningful_stress": True},
    }

    states = build_target_coverage_state(athlete, [production_role])
    insert = select_gap_fill_insert(
        athlete,
        12,
        scheduled_roles=[production_role],
    )

    assert states[0].target == "footwork"
    assert states[0].meaningful_coverage is True
    assert states[0].remaining_need == 0.0
    assert insert is not None
    assert not _supports(insert, "footwork")


def test_candidate_ranking_follows_canonical_priority_weights():
    athlete = _athlete(
        weaknesses=["mobility", "conditioning"],
        primary_weak_area="mobility",
        key_goals=["footwork", "speed"],
        primary_goal="footwork",
    )
    states = build_target_coverage_state(athlete)

    assert [(state.target, state.priority_weight) for state in states] == [
        ("mobility", PRIMARY_WEAKNESS_WEIGHT),
        ("footwork", PRIMARY_GOAL_WEIGHT),
        ("conditioning", SECONDARY_WEAKNESS_WEIGHT),
        ("speed", SECONDARY_GOAL_WEIGHT),
    ]

    covered: list[dict] = []
    expected_targets = ("mobility", "footwork", "conditioning", "speed")
    for target in expected_targets:
        insert = select_gap_fill_insert(athlete, 12, scheduled_roles=covered)
        assert insert is not None
        assert _supports(insert, target)
        covered.append(_meaningful_role(f"covered_{target}", target))


def test_strength_and_power_are_not_fake_repaired_by_low_cost_fillers():
    for target in ("strength", "power"):
        athlete = _athlete(
            weaknesses=[target],
            primary_weak_area=target,
        )
        states = build_target_coverage_state(athlete)
        insert = select_gap_fill_insert(athlete, 12)

        assert states[0].target == target
        assert states[0].remaining_need == 1.0
        assert states[0].low_cost_addressable is False
        assert insert is not None
        assert target not in (insert.get("support_target_capabilities") or {})


def test_undercovered_conditioning_can_receive_honest_aerobic_maintenance():
    athlete = _athlete(
        key_goals=["conditioning"],
        primary_goal="conditioning",
    )

    insert = select_gap_fill_insert(athlete, 12)

    assert insert is not None
    assert insert["role_key"] in LOW_COST_AEROBIC_INSERTS
    assert insert["governance"]["meaningful_stress"] is False
    assert _supports(insert, "conditioning")


def test_alactic_speed_role_does_not_falsely_close_conditioning_need():
    athlete = _athlete(
        key_goals=["conditioning"],
        primary_goal="conditioning",
    )
    alactic_role = {
        "role_key": "primary_conditioning_day",
        "category": "conditioning",
        "preferred_system": "alactic",
        "governance": {"meaningful_stress": True},
    }

    state = build_target_coverage_state(athlete, [alactic_role])[0]
    insert = select_gap_fill_insert(
        athlete,
        12,
        scheduled_roles=[alactic_role],
    )

    assert state.target == "conditioning"
    assert state.meaningful_coverage is False
    assert state.remaining_need == 1.0
    assert insert is not None
    assert insert["role_key"] in LOW_COST_AEROBIC_INSERTS
    assert _supports(insert, "conditioning")


def test_shared_collision_legality_vetoes_highest_priority_target():
    athlete = _athlete(
        weaknesses=["footwork"],
        primary_weak_area="footwork",
    )
    legality = sequence_legality([], resolved_contacts=[(12, "hard")])

    insert = select_gap_fill_insert(
        athlete,
        12,
        on_hard_sparring_day=True,
        legality=legality,
    )

    assert insert is not None
    assert not _supports(insert, "footwork")
    assert legality.role_is_forbidden(insert, 12) is False


def test_coverage_ignores_target_words_in_display_text():
    athlete = _athlete(
        weaknesses=["footwork"],
        primary_weak_area="footwork",
    )
    unrelated = {
        "role_key": "recovery_reset_day",
        "category": "recovery",
        "display_text": "Footwork appears here only as arbitrary renderer copy.",
    }

    states = build_target_coverage_state(athlete, [unrelated])
    insert = select_gap_fill_insert(athlete, 12, scheduled_roles=[unrelated])

    assert states[0].meaningful_coverage is False
    assert states[0].remaining_need == 1.0
    assert insert is not None
    assert _supports(insert, "footwork")


def test_previously_inserted_filler_updates_coverage_and_avoids_repetition():
    athlete = _athlete(
        weaknesses=["footwork"],
        primary_weak_area="footwork",
    )
    first = select_gap_fill_insert(athlete, 12)
    assert first is not None and _supports(first, "footwork")

    second = select_gap_fill_insert(athlete, 11, scheduled_roles=[first])

    assert second is not None
    assert not _supports(second, "footwork")
    state = build_target_coverage_state(athlete, [first])[0]
    assert state.meaningful_coverage is False
    assert state.support_coverage == 1.0
    assert state.remaining_need == 0.0


def test_normal_camp_does_not_let_secondary_coordination_bypass_primary_footwork():
    athlete = _athlete(
        weaknesses=["footwork", "coordination"],
        primary_weak_area="footwork",
        technical_styles=["boxing"],
        tactical_styles=["distance_striker"],
        equipment=[],
    )
    week = _fight_dated_spp_week()

    apply_camp_week_fillers({"weeks": [week]}, athlete)

    discretionary = _discretionary_roles(week)
    assert len(discretionary) == 1
    assert discretionary[0]["role_key"] != "coordination_support"
    assert _supports(discretionary[0], "footwork")


def test_normal_camp_coordination_wins_when_it_is_the_highest_remaining_priority():
    athlete = _athlete(
        weaknesses=["coordination", "footwork"],
        primary_weak_area="coordination",
        technical_styles=["boxing"],
        tactical_styles=["distance_striker"],
        equipment=[],
    )
    week = _fight_dated_spp_week()

    apply_camp_week_fillers({"weeks": [week]}, athlete)

    discretionary = _discretionary_roles(week)
    # The week now also keeps its free day occupied (physical frequency is
    # preserved), so there can be a second filler. What matters here is that the
    # highest remaining priority, coordination, is placed first.
    assert 1 <= len(discretionary) <= 2
    assert discretionary[0]["role_key"] == "coordination_support"
    assert _supports(discretionary[0], "coordination")


def test_live_wrapper_previous_filler_reduces_next_week_remaining_need():
    athlete = _athlete(
        days_until_fight=28,
        weaknesses=["footwork"],
        primary_weak_area="footwork",
    )
    week_one = _fight_dated_spp_week(main_d_day=28, filler_d_day=26)
    week_two = _fight_dated_spp_week(main_d_day=21, filler_d_day=19)

    apply_camp_week_fillers({"weeks": [week_one, week_two]}, athlete)

    first = _discretionary_roles(week_one)
    second = _discretionary_roles(week_two)
    assert len(first) == 1
    # 979085b0 (physical-frequency preservation), refined by #3106:
    # a breathing insert cannot occupy the week's required physical day.
    # Keep exactly one physical fallback as well as the zero-load reset.
    assert len(second) == 2
    assert {role["role_key"] for role in second} == {"joint_prep", "breathing_reset"}
    assert _supports(first[0], "footwork")
    assert all(not _supports(role, "footwork") for role in second)


def test_live_wrapper_counts_meaningful_coverage_from_other_scheduled_week():
    athlete = _athlete(
        days_until_fight=28,
        # Keep an unmet target: physical-frequency policy still fills the slot
        # when every selected target is covered, using a safe fallback.
        weaknesses=["footwork", "mobility"],
        primary_weak_area="footwork",
    )
    week_one = _fight_dated_spp_week(main_d_day=28, filler_d_day=26)
    technical_footwork = {
        "role_key": "primary_conditioning_day",
        "category": "conditioning",
        "preferred_system": "technical_footwork",
        "scheduled_day_hint": "Monday",
        "countdown_offset": 21,
        "governance": {"meaningful_stress": True},
    }
    week_two = _fight_dated_spp_week(
        main_d_day=21,
        filler_d_day=19,
        main_role=technical_footwork,
    )

    apply_camp_week_fillers({"weeks": [week_one, week_two]}, athlete)

    first = _discretionary_roles(week_one)
    assert len(first) == 1
    assert not _supports(first[0], "footwork")
    assert _supports(first[0], "mobility")


def test_normal_and_late_paths_share_target_aware_selection():
    athlete = _athlete(
        weaknesses=["footwork"],
        primary_weak_area="footwork",
        key_goals=["speed"],
        primary_goal="speed",
    )
    speed_role = {
        **_meaningful_role("alactic_speed_day"),
        "scheduled_day_hint": "Monday",
    }
    week = {
        "phase": "SPP",
        "session_roles": [speed_role],
        "calendar_days": [
            {"weekday": "monday", "d_day": 14},
            {"weekday": "wednesday", "d_day": 12},
        ],
        "declared_training_days": ["Monday", "Wednesday"],
        "intentionally_unused_days": [{"day": "Wednesday", "role": "off_day"}],
    }

    apply_camp_week_fillers({"weeks": [week]}, athlete)
    watches = [
        role
        for role in week["session_roles"]
        if role.get("role_key") == "tactical_watch"
    ]
    normal_insert = next(
        role
        for role in week["session_roles"]
        if role.get("camp_week_filler") and role.get("role_key") != "tactical_watch"
    )
    late_insert = select_gap_fill_insert(
        athlete,
        12,
        scheduled_roles=[speed_role],
    )

    assert late_insert is not None
    assert len(watches) == 1
    assert watches[0]["mandatory_tactical_watch"] is True
    assert _supports(normal_insert, "footwork")
    assert _supports(late_insert, "footwork")
