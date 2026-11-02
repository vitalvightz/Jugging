"""Pins how athlete-built sessions reach the web, for the "Last training focus".

The web decides relevance from these exact fields (web/lib/training-focus.ts).
This compiles a real custom plan and checks it against the fixture the web
tests read, so neither side can drift without the other noticing.
"""
import json
from pathlib import Path
from uuid import uuid4

from api.plan_mappers import _map_profile_row
from api.services.custom_plans import custom_open_plan_result
from tests.test_custom_open_plans import api, body  # noqa: F401 - fixture

FIXTURE = Path(__file__).resolve().parents[1] / "web/test/fixtures/custom-plan-focus-sessions.json"


def _week() -> dict:
    sessions = body("Monday")["sessions"][:1] + [
        {"id": str(uuid4()), "name": "Light Technical", "weekday": "Tuesday", "session_type": "skill", "exercises": [
            {"id": str(uuid4()), "origin": "manual", "name": "Pad rounds",
             "dose": {"mode": "intervals", "rounds": 6, "work_seconds": 180, "rest_seconds": 60}}]},
        {"id": str(uuid4()), "name": "Hard Sparring", "weekday": "Wednesday", "session_type": "sparring", "exercises": []},
    ]
    return {"name": "Fighter week", "sessions": sessions, "id": str(uuid4())}


def _shape(result: dict) -> list[dict]:
    """Only what the focus rules read, per session, in weekday order."""
    shaped = []
    for day in result["structured_plan"]["weeks"][0]["days"]:
        for session in day["sessions"]:
            shaped.append({
                "title": session["title"],
                "session_type": session["session_type"],
                "prescription_source": session.get("prescription_source"),
                "blocks": [{"block_type": block["block_type"]} for block in session.get("blocks") or []],
                "coach_led_contact": (day.get("today_card") or {}).get("coach_led_contact"),
            })
    return shaped


def test_athlete_built_sessions_match_the_web_focus_fixture(api):  # noqa: F811
    _, store = api
    profile = _map_profile_row(store.profiles["athlete-1"])
    result = custom_open_plan_result(_week(), profile, training_day="2026-10-12")
    assert _shape(result) == json.loads(FIXTURE.read_text(encoding="utf-8"))
