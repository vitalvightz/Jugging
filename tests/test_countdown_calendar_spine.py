"""Countdown-aware calendar spine for normal camps.

Covers:
- per-day D-day mapping inside a week,
- the calendar_days metadata block,
- hard-sparring planner converting D-17+ days to technical and capping
  D-21..D-18 to a single effective
  hard exposure,
- no pre-fight session rendering after a Friday fight day.
"""

from fightcamp.fight_date_utils import build_calendar_days, d_day_for_weekday
from fightcamp.sparring_dose_planner import compute_hard_sparring_plan, effective_hard_days


def _week_with_calendar(*, end_d, span, fight_weekday="friday", **overrides):
    week = {
        "phase": overrides.pop("phase", "SPP"),
        "stage_key": overrides.pop("stage_key", "specific_density_build"),
        "week_index": 1,
        "phase_week_index": overrides.pop("phase_week_index", None),
        "phase_week_total": overrides.pop("phase_week_total", None),
        "projected_days_until_fight_start": end_d + span - 1,
        "projected_days_until_fight_end": end_d,
        "span_days": span,
        "fight_weekday": fight_weekday,
        "declared_hard_sparring_days": overrides.pop("hard_days", []),
        "session_roles": overrides.pop("session_roles", []),
    }
    week.update(overrides)
    return week


def _athlete(days_until_fight, *, hard_days, fight_weekday="friday"):
    return {
        "sport": "boxing",
        "fatigue": "low",
        "days_until_fight": days_until_fight,
        "weight_cut_pct": 0.0,
        "weight_cut_risk": False,
        "readiness_flags": [],
        "injuries": [],
        "hard_sparring_days": hard_days,
        "fight_date": None,
        "fight_weekday": fight_weekday,
    }


# ── Calendar spine primitives ─────────────────────────────────────────────────

def test_d_day_for_weekday_friday_fight_resolves_each_weekday():
    # Final week: ends at D-1 (Thursday before Friday fight), span 7.
    assert d_day_for_weekday("thursday", fight_weekday="friday",
                             projected_days_until_fight_end=1, span_days=7) == 1
    assert d_day_for_weekday("friday", fight_weekday="friday",
                             projected_days_until_fight_end=1, span_days=7) == 7
    assert d_day_for_weekday("monday", fight_weekday="friday",
                             projected_days_until_fight_end=1, span_days=7) == 4


def test_d_day_for_weekday_returns_none_when_outside_week_span():
    # Final week with span 3: only the last 3 days before fight are in scope.
    assert d_day_for_weekday("monday", fight_weekday="friday",
                             projected_days_until_fight_end=1, span_days=3) is None
    assert d_day_for_weekday("wednesday", fight_weekday="friday",
                             projected_days_until_fight_end=1, span_days=3) == 2


def test_build_calendar_days_friday_fight_skips_post_fight_weekend():
    # 6-day final week ending at D-1. Because the role map clamps end_d to >= 1
    # for normal-camp weeks, no Saturday/Sunday AFTER fight day can appear here.
    days = build_calendar_days(fight_weekday="friday",
                               projected_days_until_fight_end=1, span_days=6)
    assert all(entry["d_day"] >= 1 for entry in days)
    assert all(entry["is_after_fight_day"] is False for entry in days)
    assert all(entry["is_fight_day"] is False for entry in days)
    # The day immediately before a Friday fight is always Thursday (D-1).
    assert days[-1] == {
        "weekday": "thursday",
        "d_day": 1,
        "is_fight_day": False,
        "is_after_fight_day": False,
    }


def test_build_calendar_days_marks_fight_day_when_d0_in_range():
    days = build_calendar_days(fight_weekday="friday",
                               projected_days_until_fight_end=0, span_days=1)
    assert days == [
        {"weekday": "friday", "d_day": 0, "is_fight_day": True, "is_after_fight_day": False}
    ]


# ── Hard sparring per-day countdown rules ─────────────────────────────────────

def test_d14_cutoff_converts_hard_sparring_for_a_normal_risk_athlete():
    # 4-week camp, week 2 covers D-20..D-14. Friday fight -> Monday D-18 and
    # Wednesday D-16 are ahead of the D-14 cutoff and stay hard; Friday is D-14,
    # the first converted day.
    week = _week_with_calendar(
        end_d=14, span=7, fight_weekday="friday",
        hard_days=["Monday", "Wednesday", "Friday"],
    )
    plan = compute_hard_sparring_plan(
        week=week,
        athlete_snapshot=_athlete(20, hard_days=["Monday", "Wednesday", "Friday"]),
    )
    by_day = {entry["day"]: entry for entry in plan}

    assert by_day["Monday"]["d_day"] == 18
    assert by_day["Wednesday"]["d_day"] == 16
    assert by_day["Friday"]["d_day"] == 14
    for day in ("Monday", "Wednesday"):
        assert by_day[day]["effective_load"] == "hard"
    assert by_day["Friday"]["status"] == "convert_to_technical_suggested"
    assert by_day["Friday"]["effective_load"] == "technical"
    assert "d14_hard_sparring_ban" in by_day["Friday"]["reason_codes"]


def test_d17_cutoff_converts_hard_sparring_for_an_elevated_risk_athlete():
    # Elevated risk (here: high fatigue) brings the cutoff forward to D-17, so
    # Wednesday (D-16) and Friday (D-14) convert while Monday (D-18) stays hard.
    week = _week_with_calendar(
        end_d=14, span=7, fight_weekday="friday",
        hard_days=["Monday", "Wednesday", "Friday"],
    )
    athlete = _athlete(20, hard_days=["Monday", "Wednesday", "Friday"])
    athlete["readiness_flags"] = ["high_fatigue"]
    plan = compute_hard_sparring_plan(week=week, athlete_snapshot=athlete)
    by_day = {entry["day"]: entry for entry in plan}

    for day in ("Wednesday", "Friday"):
        assert by_day[day]["status"] == "convert_to_technical_suggested"
        assert by_day[day]["effective_load"] == "technical"
        assert "d17_hard_sparring_ban" in by_day[day]["reason_codes"]

    assert by_day["Wednesday"]["d_day"] == 16
    assert by_day["Monday"]["d_day"] == 18
    assert by_day["Monday"]["effective_load"] == "hard"


def test_d18_remains_last_allowed_hard_spar_day():
    # Friday fight, week of D-24..D-18 — Monday is D-18. Only one declared
    # hard day inside the cap band → it stays hard.
    week = _week_with_calendar(
        end_d=18, span=7, fight_weekday="friday",
        hard_days=["Monday"],
    )
    plan = compute_hard_sparring_plan(
        week=week,
        athlete_snapshot=_athlete(24, hard_days=["Monday"]),
    )
    assert plan[0]["day"] == "Monday"
    assert plan[0]["d_day"] == 18
    assert plan[0]["effective_load"] == "hard"


def test_d21_d18_window_keeps_declared_hard_days_as_coach_owned_locks():
    # Week ends at D-18, span 4, days D-18..D-21 — fully inside the D-18+
    # band. Friday fight → D-18 = Monday, D-21 = Friday. Declared hard
    # sparring days at D-18 or further out are coach-owned combat locks:
    # the app never caps or deloads them.
    week = _week_with_calendar(
        end_d=18, span=4, fight_weekday="friday",
        hard_days=["Friday", "Monday"],
    )
    plan = compute_hard_sparring_plan(
        week=week,
        athlete_snapshot=_athlete(24, hard_days=["Friday", "Monday"]),
    )
    statuses = {entry["day"]: entry["status"] for entry in plan}

    hard_count = sum(1 for status in statuses.values() if status == "hard_as_planned")
    assert hard_count == 2, statuses


def test_normal_week_outside_d21_keeps_hard_sparring_as_declared():
    # 4-week camp, week 1 covers D-27..D-21. With Friday fight, every weekday is >=22.
    week = _week_with_calendar(
        end_d=21, span=7, fight_weekday="friday",
        hard_days=["Tuesday", "Thursday"],
    )
    plan = compute_hard_sparring_plan(
        week=week,
        athlete_snapshot=_athlete(27, hard_days=["Tuesday", "Thursday"]),
    )
    assert all(entry["status"] == "hard_as_planned" for entry in plan)
    assert all(entry["d_day"] >= 22 for entry in plan)


# ── Normal-camp role-map integration: 4-week and 12-week ──────────────────────

def test_normal_camp_4_week_emits_calendar_metadata_and_bans_d15():
    from fightcamp.stage2_role_map import _build_weekly_role_map

    athlete_model = {
        "sport": "boxing",
        "status": "amateur",
        "rounds_format": "3x3",
        "training_days": ["monday", "tuesday", "wednesday", "thursday", "friday"],
        "hard_sparring_days": ["monday", "wednesday", "friday"],
        "fatigue": "low",
        "weight_cut_pct": 0.0,
        "weight_cut_risk": False,
        "readiness_flags": [],
        "injuries": [],
        "fight_date": "2026-05-29",  # Friday
        "days_until_fight": 28,
    }
    progression = {
        "weeks": [
            {"week_index": 1, "phase": "GPP", "stage_key": "general_capacity",
             "phase_week_index": 1, "phase_week_total": 1, "span_days": 7,
             "session_counts": {"strength": 2, "conditioning": 2, "recovery": 1}},
            {"week_index": 2, "phase": "SPP", "stage_key": "specific_density_build",
             "phase_week_index": 1, "phase_week_total": 2, "span_days": 7,
             "session_counts": {"strength": 2, "conditioning": 2, "recovery": 1}},
            {"week_index": 3, "phase": "SPP", "stage_key": "specific_density_build",
             "phase_week_index": 2, "phase_week_total": 2, "span_days": 7,
             "session_counts": {"strength": 2, "conditioning": 2, "recovery": 1}},
            {"week_index": 4, "phase": "TAPER", "stage_key": "taper_sharpen",
             "phase_week_index": 1, "phase_week_total": 1, "span_days": 7,
             "session_counts": {"strength": 1, "conditioning": 1, "recovery": 1}},
        ]
    }
    role_map = _build_weekly_role_map(
        athlete_model, progression, {"key": "general_fight_readiness"}
    )

    weeks = role_map["weeks"]
    assert len(weeks) == 4
    for week in weeks:
        assert week["calendar_days"], "every week needs a calendar spine"
        assert week["countdown_range"], "every week needs a countdown range"
        assert week["projected_days_until_fight_end"] >= 0
        # No pre-fight day appears AFTER the fight day (Friday).
        assert all(day["is_after_fight_day"] is False for day in week["calendar_days"])

    # Week 3 ends at D-7 → all days fall inside the D-17 ban → all converted.
    week_3_plan = weeks[2]["hard_sparring_plan"]
    assert all(entry["effective_load"] != "hard" for entry in week_3_plan)


def test_open_camp_sunday_monday_contact_is_deloaded_across_week_boundaries():
    from fightcamp.calendar_integrity import apply_final_calendar_integrity
    from fightcamp.stage2_role_map import _build_weekly_role_map

    athlete = {
        **_athlete(84, hard_days=["Monday", "Sunday"]),
        "plan_creation_weekday": "sunday",
        "open_block_horizon_days": 63,
        "training_days": ["monday", "sunday"],
    }
    progression = {"weeks": [
        {"week_index": index, "phase": "GPP", "stage_key": "general_capacity",
         "span_days": 7, "session_counts": {}}
        for index in range(1, 4)
    ]}
    role_map = _build_weekly_role_map(athlete, progression, {})
    contacts = [entry for week in role_map["weeks"] for entry in week["hard_sparring_plan"]]
    hard_dates = {entry["d_day"] for entry in contacts if entry["effective_load"] == "hard"}

    assert len(contacts) == 6
    assert len(hard_dates) == 4
    assert all(d_day + 1 not in hard_dates for d_day in hard_dates)
    assert all(
        entry["effective_load"] == "reduced" and "consecutive_hard_days" in entry["reason_codes"]
        for entry in contacts if entry["d_day"] not in hard_dates
    )
    apply_final_calendar_integrity(role_map)


def test_normal_camp_12_week_attaches_calendar_to_every_week():
    from fightcamp.stage2_role_map import _build_weekly_role_map

    athlete_model = {
        "sport": "boxing",
        "status": "amateur",
        "rounds_format": "3x3",
        "training_days": ["monday", "tuesday", "wednesday", "thursday", "friday"],
        "hard_sparring_days": ["tuesday", "thursday"],
        "fatigue": "low",
        "weight_cut_pct": 0.0,
        "weight_cut_risk": False,
        "readiness_flags": [],
        "injuries": [],
        "fight_date": "2026-07-24",  # Friday
        "days_until_fight": 84,
    }
    weeks_input = []
    for idx in range(1, 13):
        if idx <= 4:
            phase, stage = "GPP", "general_capacity"
            pwi, pwt = idx, 4
        elif idx <= 10:
            phase, stage = "SPP", "specific_density_build"
            pwi, pwt = idx - 4, 6
        else:
            phase, stage = "TAPER", "taper_sharpen"
            pwi, pwt = idx - 10, 2
        weeks_input.append({
            "week_index": idx, "phase": phase, "stage_key": stage,
            "phase_week_index": pwi, "phase_week_total": pwt, "span_days": 7,
            "session_counts": {"strength": 2, "conditioning": 2, "recovery": 1},
        })
    role_map = _build_weekly_role_map(
        athlete_model, {"weeks": weeks_input}, {"key": "general_fight_readiness"}
    )

    weeks = role_map["weeks"]
    assert len(weeks) == 12
    all_d_days = [day["d_day"] for week in weeks for day in week["calendar_days"]]
    # The countdown spine should cover D-0 (the fight day) through at least
    # D-83 — the whole camp anchored so the final week ends on the fight.
    assert min(all_d_days) == 0
    assert max(all_d_days) >= 83

    # Week 12 ends on the fight day → its declared Tue/Thu carry no hard load.
    final_plan = weeks[-1]["hard_sparring_plan"]
    assert all(entry["effective_load"] != "hard" for entry in final_plan)


def test_late_fight_preserves_safe_conditioning_and_exposes_real_day_labels():
    from fightcamp.stage2_role_map import _build_weekly_role_map
    from fightcamp.weekly_schedule_view import extract_weekly_schedule

    athlete_model = {
        "sport": "boxing",
        "status": "amateur",
        "rounds_format": "3x3",
        "training_days": ["monday", "tuesday", "wednesday", "thursday", "friday"],
        "hard_sparring_days": ["tuesday", "thursday"],
        "key_goals": ["conditioning_endurance"],
        "weaknesses": ["gas_tank"],
        "fatigue": "moderate",
        "weight_cut_pct": 0.0,
        "weight_cut_risk": False,
        "readiness_flags": [],
        "injuries": [],
        "fight_date": "2026-05-10",
        "days_until_fight": 6,
    }
    progression = {
        "weeks": [
            {"week_index": 1, "phase": "TAPER", "stage_key": "taper_sharpen",
             "phase_week_index": 1, "phase_week_total": 1, "span_days": 6,
             "session_counts": {"strength": 1, "conditioning": 0, "recovery": 1}},
        ]
    }
    role_map = _build_weekly_role_map(
        athlete_model, progression, {"key": "general_fight_readiness"}
    )
    week = role_map["weeks"][0]
    conditioning_roles = [r for r in week["session_roles"] if r.get("category") == "conditioning"]
    assert conditioning_roles, "conditioning/endurance signal should create at least one conditioning role before compression"
    assert all(r.get("preferred_system") != "glycolytic" for r in conditioning_roles)

    schedule = extract_weekly_schedule({"weekly_role_map": role_map}, week_index=0)
    assert schedule is not None
    assert isinstance(schedule.get("projected_days_until_fight_end"), int)
    assert schedule.get("projected_days_until_fight_end") <= 6


def test_recovery_day_can_become_low_aerobic_gas_tank_when_gas_tank_is_limiter():
    from fightcamp.stage2_role_map import _build_weekly_role_map

    athlete_model = {
        "sport": "boxing",
        "status": "amateur",
        "rounds_format": "3x3",
        "training_days": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"],
        "hard_sparring_days": ["monday", "wednesday", "friday"],
        "key_goals": ["conditioning_endurance"],
        "weaknesses": ["gas_tank"],
        # Low fatigue: moderate fatigue on top of three hard sparring days now
        # blocks the gas-tank conversion (readiness gating), so the limiter case
        # is exercised for an athlete who is actually fresh enough to take it.
        "fatigue": "low",
        "weight_cut_pct": 3.0,
        "weight_cut_risk": True,
        "readiness_flags": [],
        "injuries": [],
        "fight_date": "2026-05-29",
        "days_until_fight": 28,
    }

    progression = {
        "weeks": [
            {
                "week_index": 1,
                "phase": "GPP",
                "stage_key": "general_capacity",
                "phase_week_index": 1,
                "phase_week_total": 1,
                "span_days": 7,
                "session_counts": {
                    "strength": 1,
                    "conditioning": 0,
                    "recovery": 2,
                },
            }
        ]
    }

    role_map = _build_weekly_role_map(
        athlete_model,
        progression,
        {"key": "general_fight_readiness"},
    )

    week = role_map["weeks"][0]
    gas_tank_roles = [
        role for role in week["session_roles"]
        if role.get("role_key") == "recovery_aerobic_gas_tank_day"
    ]

    assert gas_tank_roles
    assert all(role.get("preferred_system") == "aerobic" for role in gas_tank_roles)
    assert all(role.get("gas_tank_recovery_touch") is True for role in gas_tank_roles)

# ── The ban must fail closed when the calendar cannot place a day ─────────────
#
# ``_per_day_d_days`` needs a fight weekday to resolve each declared day. Without
# one it returned an empty map and the whole per-day authority no-opped, so every
# declared hard day stayed hard — right through the final week to D-0. A safety
# rule that silently switches itself off is worse than one that is occasionally
# over-conservative, so an unplaceable day now falls back to the week's own
# countdown window.

def test_ban_applies_when_fight_weekday_cannot_be_resolved():
    # No fight weekday (no fight date and no plan-creation weekday), week window
    # D-20..D-14. Nothing can be placed on the calendar, but the window reaches
    # into the ban, so every declared day converts instead of staying hard.
    week = _week_with_calendar(
        end_d=14, span=7, fight_weekday=None,
        hard_days=["Tuesday", "Saturday"],
    )
    plan = compute_hard_sparring_plan(
        week=week,
        athlete_snapshot=_athlete(42, hard_days=["Tuesday", "Saturday"]),
    )

    assert [entry["effective_load"] for entry in plan] == ["technical", "technical"]
    for entry in plan:
        assert entry["status"] == "convert_to_technical_suggested"
        assert "d14_hard_sparring_ban" in entry["reason_codes"]
        # Flagged so the guess is visible rather than passing as calendar truth.
        assert "unresolved_countdown_day" in entry["reason_codes"]


def test_unplaceable_days_outside_the_ban_window_stay_hard():
    # The fallback must not over-reach: a week that never approaches D-17 keeps
    # its declared hard sparring even with no resolvable calendar.
    week = _week_with_calendar(
        end_d=35, span=7, fight_weekday=None,
        hard_days=["Tuesday", "Saturday"],
    )
    plan = compute_hard_sparring_plan(
        week=week,
        athlete_snapshot=_athlete(42, hard_days=["Tuesday", "Saturday"]),
    )

    assert [entry["effective_load"] for entry in plan] == ["hard", "hard"]
    for entry in plan:
        assert entry["reason_codes"] == []


def test_resolvable_week_keeps_precise_per_day_verdicts():
    # With a fight weekday the per-day calendar still decides each day on its own
    # D-day — the fallback must not coarsen a week it can actually resolve.
    week = _week_with_calendar(
        end_d=14, span=7, fight_weekday="friday",
        hard_days=["Tuesday", "Saturday"],
    )
    plan = compute_hard_sparring_plan(
        week=week,
        athlete_snapshot=_athlete(20, hard_days=["Tuesday", "Saturday"]),
    )
    by_day = {entry["day"]: entry for entry in plan}

    assert by_day["Saturday"]["d_day"] == 20
    assert by_day["Saturday"]["effective_load"] == "hard"
    assert by_day["Tuesday"]["d_day"] == 17
    # D-17 is ahead of the normal D-14 cutoff, so it stays hard...
    assert by_day["Tuesday"]["effective_load"] == "hard"
    assert all("unresolved_countdown_day" not in e["reason_codes"] for e in plan)

    # ...and converts for an elevated-risk athlete, whose cutoff is D-17.
    elevated = _athlete(20, hard_days=["Tuesday", "Saturday"])
    elevated["readiness_flags"] = ["high_fatigue"]
    elevated_by_day = {
        entry["day"]: entry
        for entry in compute_hard_sparring_plan(week=week, athlete_snapshot=elevated)
    }
    assert elevated_by_day["Saturday"]["effective_load"] == "hard"
    assert elevated_by_day["Tuesday"]["effective_load"] == "technical"


def test_declared_day_outside_a_short_span_converts_inside_the_ban():
    # A two-day week (D-15..D-14, Thu-Fri off a Friday fight) contains neither
    # declared day, so neither can be placed. They used to stay hard and count as
    # the week's effective hard sparring; inside the ban window they must not.
    #
    # days_until_fight is the camp start (42), deliberately outside the 0..17
    # camp-level override — otherwise that override would convert these days on
    # its own and the per-day fallback under test would never be exercised.
    week = _week_with_calendar(
        end_d=14, span=2, fight_weekday="friday",
        hard_days=["Tuesday", "Saturday"],
    )
    plan = compute_hard_sparring_plan(
        week=week,
        athlete_snapshot=_athlete(42, hard_days=["Tuesday", "Saturday"]),
    )

    assert all(entry["effective_load"] == "technical" for entry in plan)
    assert effective_hard_days(plan) == []
