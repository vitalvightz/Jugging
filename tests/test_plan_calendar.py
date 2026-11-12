"""Home's fight countdown reads only the plan's dated spine, not the full detail.

The countdown uses week dates, phases and countdown labels (web/lib/fight-countdown.ts).
Home used to download the whole plan detail for them: ~1.7 MB for one athlete.
"""

from __future__ import annotations

import copy
import json

from support import _build_client
from test_structured_plan_models import _valid_plan

H = {"Authorization": "Bearer athlete-token"}
PLAN_ID = "11111111-2222-3333-4444-555555555555"


def _athlete(client) -> str:
    me = client.get("/api/me", headers=H).json()
    return (me.get("profile") or {}).get("athlete_id") or me["athlete_id"]


def _seed(store, athlete_id: str, plan_id: str = PLAN_ID) -> dict:
    plan = copy.deepcopy(_valid_plan())
    week = plan["weeks"][0]
    # Several sessions of content per day, which the countdown never reads.
    session = week["days"][0]["sessions"][0]
    session["blocks"] = [{**copy.deepcopy(session["blocks"][0]), "block_id": f"b-{i}"} for i in range(8)]
    week["days"][0]["countdown_label"] = "D-20"
    store.plans[plan_id] = {
        "id": plan_id, "athlete_id": athlete_id, "status": "ready", "plan_name": "Camp",
        "created_at": "2026-06-01T00:00:00+00:00", "fight_date": "2026-12-01", "structured_plan": plan,
    }
    return plan


def test_calendar_is_the_detail_plans_spine_and_much_smaller():
    client, store, _ = _build_client()
    _seed(store, _athlete(client))

    detail = client.get(f"/api/plans/{PLAN_ID}", headers=H)
    calendar = client.get(f"/api/plans/{PLAN_ID}/calendar", headers=H)

    assert detail.status_code == calendar.status_code == 200
    full = detail.json()["outputs"]["structured_plan"]
    spine = calendar.json()["structured_plan"]
    assert calendar.json()["plan_id"] == PLAN_ID
    assert len(spine["weeks"]) == len(full["weeks"])
    for full_week, week in zip(full["weeks"], spine["weeks"]):
        for key, value in week.items():
            if key != "days":
                assert full_week[key] == value
        assert {"week_index", "start_date", "end_date", "phase_label"} <= set(week)
        assert [day["date"] for day in week["days"]] == [day["date"] for day in full_week["days"]]
        for full_day, day in zip(full_week["days"], week["days"]):
            assert set(day) <= {"date", "phase_label", "countdown_label"}
            assert all(full_day[key] == value for key, value in day.items())
    assert spine["weeks"][0]["days"][0]["countdown_label"] == "D-20"
    assert "sessions" not in json.dumps(spine)
    assert len(calendar.content) * 5 < len(detail.content)


def test_calendar_is_refused_exactly_where_the_detail_is():
    client, store, _ = _build_client()
    _seed(store, "someone-else")

    assert client.get(f"/api/plans/{PLAN_ID}", headers=H).status_code == 404
    assert client.get(f"/api/plans/{PLAN_ID}/calendar", headers=H).status_code == 404


def test_a_plan_without_a_structured_card_has_no_calendar():
    client, store, _ = _build_client()
    _seed(store, _athlete(client))
    store.plans[PLAN_ID]["structured_plan"] = None

    response = client.get(f"/api/plans/{PLAN_ID}/calendar", headers=H)

    assert response.status_code == 200
    assert response.json()["structured_plan"] is None
