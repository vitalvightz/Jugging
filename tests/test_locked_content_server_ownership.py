"""Deterministic locked content survives without Stage 2 reproducing it.

The invariant: input- or rule-owned content (declared hard sparring, declared
light technical combat, Tactical Focus) may inform Stage 2, but its
survival must never depend on Stage 2 echoing it back. These tests drive the
real generation path, not helper stubs.
"""

from __future__ import annotations

import datetime

import pytest

from api.structured_plan_faithfulness import (
    _authoritative_locked_day,
    _locked_roles,
    check_structured_faithfulness,
)
from api.structured_plan_locked_merge import merge_locked_structured_content
from api.structured_plan_sparring_reconcile import (
    _deterministic_contact_days,
    reconcile_coach_led_sparring_days,
)
from support import _build_request

DECLARED_LIGHT_COMBAT_WEEKDAY = "Wednesday"


@pytest.fixture(scope="module")
def camp_brief() -> dict:
    """An 8-week camp with a declared Wednesday light-combat day."""
    generate_plan_sync = pytest.importorskip("fightcamp.main").generate_plan_sync
    fight_date = (datetime.date.today() + datetime.timedelta(days=56)).isoformat()
    request = _build_request(
        {
            "fight_date": fight_date,
            "support_work_days": [DECLARED_LIGHT_COMBAT_WEEKDAY],
            "training_availability": [
                "Monday", "Tuesday", "Wednesday", "Thursday", "Saturday",
            ],
            "weekly_training_frequency": 5,
        }
    ).to_payload()
    request["random_seed"] = 3
    return generate_plan_sync(request)["planning_brief"]


def _light_combat_ddays(planning_brief: dict) -> set[int]:
    days: set[int] = set()
    for week in (planning_brief.get("weekly_role_map") or {}).get("weeks") or []:
        for role in week.get("session_roles") or []:
            if str(role.get("role_key") or "").strip() != "light_combat_day":
                continue
            label = role.get("scheduled_countdown_label") or role.get("countdown_label")
            if isinstance(label, str) and label.upper().startswith("D-"):
                days.add(int(label.split("-")[1]))
    return days


def _stage2_plan_without_locked_content(ddays: list[int]) -> dict:
    """A structured plan where Stage 2 authored only its own S&C work."""
    return {
        "weeks": [
            {
                "week_index": 1,
                "phase": "GPP",
                "days": [
                    {
                        "countdown_label": f"D-{dday}",
                        "date": "",
                        "sessions": [
                            {
                                "session_id": f"session-{dday}",
                                "session_type": "strength",
                                "title": "Aerobic support",
                                "objective": "Easy aerobic work",
                                "blocks": [
                                    {
                                        "block_id": f"block-{dday}",
                                        "block_type": "exercise",
                                        "display_name": "Bike",
                                        "duration": {"value": 30, "unit": "minutes"},
                                    }
                                ],
                            }
                        ],
                    }
                    for dday in ddays
                ],
            }
        ]
    }


def test_declared_light_combat_survives_every_applicable_week(camp_brief):
    declared = _light_combat_ddays(camp_brief)
    assert declared, "fixture must declare light-combat days across the camp"

    contacts = _deterministic_contact_days(camp_brief)
    assert declared <= {contact.d_day for contact in contacts}

    contact_ddays = sorted(
        {contact.d_day for contact in contacts if contact.d_day is not None}, reverse=True
    )
    plan = _stage2_plan_without_locked_content(contact_ddays)
    reconcile_coach_led_sparring_days(plan, camp_brief)

    by_dday = {
        int(day["countdown_label"].split("-")[1]): day
        for week in plan["weeks"]
        for day in week["days"]
    }
    for dday in sorted(declared, reverse=True):
        card = by_dday[dday].get("today_card") or {}
        # Stage 2 never wrote "Light Combat / Technical" anywhere, and the day it
        # did author (aerobic support) does not delete the declared combat load.
        assert str(card.get("coach_led_contact") or "").strip(), f"D-{dday} lost declared combat"
        assert by_dday[dday]["sessions"], f"D-{dday} lost its compatible app work"


def test_declared_hard_sparring_survives_without_stage2_recreating_it(camp_brief):
    contacts = _deterministic_contact_days(camp_brief)
    hard = {contact.d_day for contact in contacts if contact.load == "hard"}
    assert hard, "fixture must declare hard sparring"

    plan = _stage2_plan_without_locked_content(sorted(hard, reverse=True))
    reconcile_coach_led_sparring_days(plan, camp_brief)
    by_dday = {
        int(day["countdown_label"].split("-")[1]): day
        for week in plan["weeks"]
        for day in week["days"]
    }
    for dday in sorted(hard, reverse=True):
        card = by_dday[dday].get("today_card") or {}
        assert str(card.get("headline") or card.get("coach_led_contact") or "").strip()


def test_tactical_watch_survives_stage2_returning_no_watch_prose(camp_brief):
    roles = _locked_roles(camp_brief)
    assert roles, "fixture must produce deterministic Tactical Watch roles"
    ddays = sorted(
        {
            day
            for role in roles
            if (day := _authoritative_locked_day(role)) is not None
        },
        reverse=True,
    )

    # Stage 2 mentions the days but authors no Tactical Watch content at all.
    source_markdown = "\n".join(
        f"### D-{dday}\n\n- Conditioning: Bike 30 min\n" for dday in ddays
    )
    merged = merge_locked_structured_content(
        _stage2_plan_without_locked_content(ddays), camp_brief
    )
    assert not merged.unresolved
    assert len(merged.applied) == len(roles)

    watch_blocks = [
        block
        for week in merged.plan["weeks"]
        for day in week["days"]
        for session in day["sessions"]
        for block in session.get("blocks") or []
        if block.get("block_type") == "mindset"
    ]
    assert len(watch_blocks) == len(roles)

    # The exact deterministic names survive, and the omission from model prose
    # raises no locked_tactical_watch_missing_from_stage2 failure.
    deterministic_names = {
        str((role.get("governance") or {}).get("selected_drill_name") or "")
        for role in roles
    } - {""}
    assert deterministic_names <= {str(block.get("display_name")) for block in watch_blocks}
    assert check_structured_faithfulness(merged.plan, source_markdown, camp_brief) == []


def test_locked_content_still_fails_when_the_payload_really_lacks_it(camp_brief):
    """The requirement moved to the real owner; it did not disappear."""
    roles = _locked_roles(camp_brief)
    ddays = sorted(
        {
            day
            for role in roles
            if (day := _authoritative_locked_day(role)) is not None
        },
        reverse=True,
    )
    source_markdown = "\n".join(f"### D-{dday}\n\n- Conditioning: Bike 30 min\n" for dday in ddays)
    unmerged = _stage2_plan_without_locked_content(ddays)

    violations = check_structured_faithfulness(unmerged, source_markdown, camp_brief)
    # The optional camp Fight Visualisation is locked for exact rendering but
    # never required, so only the mandatory roles can fail the plan.
    required = [role for role in roles if not role.get("optional_fight_visualization")]
    assert len(required) < len(roles), "fixture should include optional camp sessions"
    assert len(violations) == len(required)
    assert all("lost required source content" in violation for violation in violations)
