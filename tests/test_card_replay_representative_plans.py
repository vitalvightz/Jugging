"""Representative athletes: the card an athlete is served executes the planner's programme.

A rejected AI card is no longer repaired or re-converted; the athlete is served
the validated deterministic card. These replays prove that card carries the
authorised programme exactly, for the affected boxer and the athlete shapes
that have broken before, and that the validators reject a converter that
changes a dose or drops a selected exercise before it can reach the athlete.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
from pathlib import Path

import pytest

from api.models import PlanRequest
from api.structured_plan_deterministic_fallback import _ROLES_OWNED_ELSEWHERE, build_deterministic_structured_plan
from api.structured_plan_generation import build_structured_plan_outcome
from api.structured_plan_models import safe_parse_structured_plan
from fightcamp import input_parsing, stage2_planning_brief
from fightcamp.combat_load_policy import LoadClass, role_load_class
from fightcamp.main import generate_plan_sync
from fightcamp.programme_fidelity import reconcile_authoritative_programme
from support import _build_request

FIXTURES = Path(__file__).parent / "fixtures" / "programme_fidelity"
_NOW = dt.datetime(2026, 10, 14, 3, 40)


def _days_out(days: int) -> str:
    return (_NOW.date() + dt.timedelta(days=days)).isoformat()


def _affected_boxer() -> dict:
    intake = json.loads((FIXTURES / "affected_intake.json").read_text(encoding="utf8"))
    intake["random_seed"] = 1
    return PlanRequest(**intake).to_payload()


def _request(**overrides) -> dict:
    payload = _build_request(overrides).to_payload()
    payload["random_seed"] = 3
    return payload


SCENARIOS = {
    "affected_boxer": _affected_boxer,
    "novice_three_sessions": lambda: _request(
        fight_date=_days_out(49),
        weekly_training_frequency=3,
        training_availability=["Monday", "Wednesday", "Saturday"],
        hard_sparring_days=["Saturday"],
        technical_skill_days=[],
        athlete={"professional_status": "novice", "record": "0-0"},
    ),
    "high_frequency_double_days": lambda: _request(
        fight_date=_days_out(56),
        weekly_training_frequency=6,
        training_availability=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
        hard_sparring_days=["Tuesday", "Saturday"],
        support_work_days=["Thursday"],
        notes="I train 8-9 sessions a week; double days before and after work are fine.",
    ),
    "high_fatigue_with_injury": lambda: _request(
        fight_date=_days_out(42),
        fatigue_level="high",
        injuries="right knee patellar tendon pain, worse on jumping",
    ),
    "kickboxing": lambda: _request(athlete={"technical_style": ["kickboxing"]}, fight_date=_days_out(49)),
    "muay_thai": lambda: _request(athlete={"technical_style": ["muay_thai"]}, fight_date=_days_out(49)),
    "mma": lambda: _request(athlete={"technical_style": ["mma"]}, fight_date=_days_out(49)),
    "mental_priority": lambda: _request(key_goals=["mental preparation"], fight_date=_days_out(49)),
    "short_camp": lambda: _request(fight_date=_days_out(18)),
    "fight_week": lambda: _request(fight_date=_days_out(6)),
}


@pytest.fixture(scope="module", params=sorted(SCENARIOS))
def replay(request):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(input_parsing, "_utc_now", lambda: _NOW)
        patch.setattr(stage2_planning_brief, "_utc_now", lambda: _NOW)
        stage1 = generate_plan_sync(SCENARIOS[request.param]())
    brief = stage1["planning_brief"]
    card = build_deterministic_structured_plan(brief)
    assert card is not None, request.param
    return request.param, brief, card


def _label_dday(label) -> int | None:
    match = re.search(r"D-\s*(\d+)", str(label or ""))
    return int(match.group(1)) if match else None


def _card_days(card: dict) -> dict[int, dict]:
    return {
        _label_dday(day.get("countdown_label")): day
        for week in card.get("weeks") or []
        for day in week.get("days") or []
        if _label_dday(day.get("countdown_label")) is not None
    }


def _scheduled_roles(brief: dict):
    roles = brief.get("late_fight_session_sequence")
    if not isinstance(roles, list):
        roles = [role for week in brief["weekly_role_map"]["weeks"] for role in week.get("session_roles") or []]
    for role in roles:
        d_day = _label_dday(role.get("scheduled_countdown_label") or role.get("countdown_label"))
        if d_day is not None:
            yield d_day, role


def _blocks(day: dict) -> list[dict]:
    return [block for session in day.get("sessions") or [] for block in session.get("blocks") or []]


def _seconds(measure) -> float | None:
    if not isinstance(measure, dict) or measure.get("value") is None:
        return None
    return float(measure["value"]) * (60 if str(measure.get("unit")).startswith("min") else 1)


def test_card_is_schema_valid(replay):
    name, _, card = replay
    assert safe_parse_structured_plan(card).ok, name


def test_every_selected_exercise_is_on_its_day_with_its_exact_dose(replay):
    name, brief, card = replay
    days = _card_days(card)
    for d_day, role in _scheduled_roles(brief):
        if d_day == 0 or str(role.get("role_key")) in _ROLES_OWNED_ELSEWHERE:
            continue
        for assignment in role.get("selected_exercise_assignments") or []:
            blocks = [b for b in _blocks(days[d_day]) if b.get("display_name") == assignment["name"]]
            assert blocks, f"{name}: D-{d_day} lost {assignment['name']!r}"
            block = blocks[0]
            dose = str(assignment.get("effective_prescription") or assignment.get("base_prescription") or "")
            stated = {
                "rest": re.search(r"\brest\b", dose, re.I),
                "work": re.search(r"\d\s*(?:s|secs?|seconds?|mins?|minutes?)\s+(?:work|effort)\b", dose, re.I),
            }
            for field, key in (("rest", "rest_sec"), ("work", "work_sec")):
                # Compare where the dose states the value; bank metadata the
                # dose does not mention is not part of the athlete's instruction.
                if assignment.get(key) and stated[field]:
                    assert _seconds(block.get(field)) == float(assignment[key]), (
                        f"{name}: D-{d_day} {assignment['name']!r} {field} != {assignment[key]}s ({dose!r})"
                    )
            # A reps range ("3x8-12") stays text: the schema stores one number.
            sets_reps = re.match(r"\s*(\d+)\s*(?:sets?\s*)?[x×]\s*(\d+)\b(?!\s*(?:s|sec|min|[-–]))", dose, re.I)
            if sets_reps:
                assert (block.get("sets"), block.get("reps")) == (int(sets_reps[1]), int(sets_reps[2])), (
                    f"{name}: D-{d_day} {assignment['name']!r} sets/reps != {dose!r}"
                )


def test_every_coach_appointment_is_visible_once_and_never_zero_load(replay):
    name, brief, card = replay
    days = _card_days(card)
    contact_ddays = {
        d_day for d_day, role in _scheduled_roles(brief)
        if role.get("role_key") == "hard_sparring_day" and d_day > 0
    }
    for d_day in contact_ddays:
        day = days[d_day]
        card_view = day.get("today_card") or {}
        if day.get("sessions"):
            # A session day carries its contact on the dedicated field only.
            contact = card_view.get("coach_led_contact")
            physical = [
                s for s in day["sessions"]
                if s.get("session_type") in {"strength_power", "conditioning", "primer"}
            ]
            assert contact or physical, f"{name}: D-{d_day} contact hidden behind zero-load sessions"
        else:
            assert card_view.get("headline"), f"{name}: D-{d_day} contact day has no headline"
        session_ids = [s.get("session_id") for s in day.get("sessions") or []]
        assert len(session_ids) == len(set(session_ids)), f"{name}: D-{d_day} duplicate appointment"


def test_scheduled_zero_load_companions_survive(replay):
    name, brief, card = replay
    days = _card_days(card)
    for d_day, role in _scheduled_roles(brief):
        if d_day == 0 or role_load_class(role) is not LoadClass.ZERO_LOAD:
            continue
        if str(role.get("role_key")) in _ROLES_OWNED_ELSEWHERE:
            continue
        label = str(role.get("athlete_facing_label") or "").casefold()
        titles = " ".join(
            str(s.get("title") or "") + " " + " ".join(str(b.get("display_name") or "") for b in s.get("blocks") or [])
            for s in days[d_day].get("sessions") or []
        ).casefold()
        assert label and label in titles, f"{name}: D-{d_day} lost scheduled {role.get('athlete_facing_label')!r}"


# ---------------------------------------------------------------------------
# A converter that changes executable instructions is rejected before release.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def boxer_card_and_text():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(input_parsing, "_utc_now", lambda: _NOW)
        patch.setattr(stage2_planning_brief, "_utc_now", lambda: _NOW)
        brief = generate_plan_sync(_affected_boxer())["planning_brief"]
    raw = (FIXTURES / "production_render.txt").read_text(encoding="utf8")
    text = reconcile_authoritative_programme(brief, raw)["text"]
    return brief, build_deterministic_structured_plan(brief, plan_text=text), text


def _validate(brief, card, text):
    return build_structured_plan_outcome(
        copy.deepcopy(card),
        raw_markdown=text,
        computed_support=brief.get("computed_support"),
        planning_brief=brief,
        allow_salvage=True,
    )


def test_faithful_card_for_the_affected_boxer_validates(boxer_card_and_text):
    brief, card, text = boxer_card_and_text
    outcome = _validate(brief, card, text)
    assert outcome.status == "valid", outcome.errors
    # The one rest the source states in words ("full rest") is now carried on
    # the card's instructions, so no explicit source rest is dropped.
    assert [w for w in outcome.warnings if "dropped explicit source rest" in w] == []
    # #3101/#3104 intentionally vary ranked strength membership. Keep the
    # original qualitative-rest regression on its recorded authority rather
    # than requiring today's generated plan to select that same exercise.
    recorded = json.loads((FIXTURES / "recorded_authority.json").read_text(encoding="utf8"))
    original_text = (FIXTURES / "production_render.txt").read_text(encoding="utf8")
    original_card = build_deterministic_structured_plan(recorded, plan_text=original_text)
    assert original_card is not None
    holds = [b for week in original_card["weeks"] for day in week["days"] for s in day.get("sessions") or []
             for b in s.get("blocks") or [] if b.get("display_name") == "Single-Leg TRX Suspension Hold"]
    assert holds and all("full rest" in str(b.get("execution_instructions") or "") for b in holds)


def test_converter_shortening_bag_rest_from_60_to_20_seconds_is_rejected(boxer_card_and_text):
    brief, card, text = boxer_card_and_text
    tampered = copy.deepcopy(card)
    bag = next(
        block
        for week in tampered["weeks"] for day in week["days"] for block in _blocks(day)
        if block.get("display_name") == "Max-Power Bag Burst"
    )
    assert _seconds(bag["rest"]) == 60
    bag["rest"] = {"value": 20, "unit": "seconds"}

    outcome = _validate(brief, tampered, text)

    assert outcome.structured_plan is None
    assert any("PRESCRIPTION_CHANGED" in error and "Max-Power Bag Burst" in error for error in outcome.errors)


def test_converter_dropping_a_selected_sled_exercise_is_rejected(boxer_card_and_text):
    brief, card, text = boxer_card_and_text
    tampered = copy.deepcopy(card)
    session = next(
        session
        for week in tampered["weeks"] for day in week["days"] if day.get("countdown_label") == "D-44"
        for session in day["sessions"]
        if any("Sled" in str(b.get("display_name")) for b in session.get("blocks") or [])
    )
    session["blocks"] = [b for b in session["blocks"] if "Sled" not in str(b.get("display_name"))]

    outcome = _validate(brief, tampered, text)

    assert outcome.structured_plan is None
    assert any("Sled Pull Forward (Harness)" in error and "missing" in error for error in outcome.errors)


def test_final_mental_calendar_is_compact_attached_and_within_frequency(replay):
    from collections import Counter
    from fightcamp.mental_training_policy import mental_category, mental_training_policy, calendar_week_bucket
    from fightcamp.physical_session_frequency import is_physical_session_role

    name, brief, card = replay
    model = brief["athlete_snapshot"]
    policy = mental_training_policy(model)
    scheduled = list(_scheduled_roles(brief))
    mental = [(d, r) for d, r in scheduled if mental_category(r)]
    physical_days = {d for d, r in scheduled if is_physical_session_role(r)}
    assert all(d in physical_days for d, _ in mental), name
    assert all(n == 1 for n in Counter(d for d, _ in mental).values()), name
    fight_date = dt.date.fromisoformat(str(model["fight_date"])[:10]) if model.get("fight_date") else None
    weekday = fight_date.weekday() if fight_date else None
    visual = [(d, r) for d, r in mental if mental_category(r) == "visualise"]
    assert all(n <= policy.visualisations_per_week for n in Counter(
        calendar_week_bucket(d, weekday) for d, _ in visual).values()), name
    analyses = sorted(d for d, r in mental if mental_category(r) == "analyse")
    assert all(b-a >= policy.analyse_spacing_days for a, b in zip(analyses, analyses[1:])), name
    contacts = {d for d, r in scheduled if r.get("category") == "sparring" or r.get("role_key") == "hard_sparring_day"}
    reflections = [d for d, r in mental if mental_category(r) == "reflect"]
    assert set(reflections) <= contacts, name
    assert all(n <= 1 for n in Counter(calendar_week_bucket(d, weekday) for d in reflections).values()), name
    for d, r in mental:
        if d <= 6:
            assert mental_category(r) == "visualise", name
            assert r.get("prescribed_duration_min", 0) <= 3, name
    assert sum(d <= 6 for d, _ in mental) <= policy.visualisations_per_week, name
    # The served deterministic card cannot restore old mental-only appointments.
    days = _card_days(card)
    for d, r in mental:
        assert days[d].get("sessions"), (name, d, r["role_key"])
