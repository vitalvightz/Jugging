"""Daily readiness for an athlete without an active UNLXCK programme.

The same check-in, engine and fail-safe run with no prescribed session, phase
or intake. Nothing is invented to stand in for them, and missing safety data
can never produce a positive recommendation.
"""

from datetime import date, timedelta
from uuid import uuid4

import pytest

from api.services.today_service import (
    PLAN_FREE_CLEAN_TITLE,
    PLAN_FREE_CLEARANCE_NOTE,
    resolve_training_day,
)
from tests.support import _build_client, withdraw_health_consent

ATHLETE = {"Authorization": "Bearer athlete-token"}
PLAN_ID = "11111111-1111-1111-1111-111111111111"


def _body(**overrides) -> dict:
    return {"sleep": "good", "body": "normal", "pain": "none", **overrides}


def _checkin(client, **overrides):
    return client.post("/api/today/checkin", headers=ATHLETE, json=_body(**overrides))


def _today(client) -> dict:
    resp = client.get("/api/today", headers=ATHLETE)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _seed_plan(store) -> None:
    store.plans[PLAN_ID] = {"id": PLAN_ID, "athlete_id": "athlete-1", "status": "ready",
                            "plan_name": "Camp A", "created_at": "2026-06-01T00:00:00+00:00"}
    store.set_active_plan_id("athlete-1", PLAN_ID)


def _plan_free_rows(store) -> list[dict]:
    return [row for row in store.today_checkins.get("athlete-1", []) if row.get("plan_id") is None]


def test_a_plan_free_check_in_persists_without_a_plan_or_phase():
    client, store, _ = _build_client()

    resp = _checkin(client)

    assert resp.status_code == 201, resp.text
    record = resp.json()["checkin"]
    assert record["plan_id"] is None
    assert record["phase"] is None
    assert record["training_day"] == resolve_training_day(None)
    [row] = _plan_free_rows(store)
    assert row["phase"] is None


def test_reloading_today_restores_the_plan_free_status():
    client, store, _ = _build_client()
    assert _today(client)["today"]["recommendation_state"] == "not_checked_in"

    _checkin(client, sleep="poor")

    today = _today(client)["today"]
    assert today["recommendation_state"] == "modify"
    assert today["decision_tier"] == "modify"
    assert today["recommendation_reason"]


def test_one_plan_free_check_in_per_athlete_day():
    client, store, _ = _build_client()

    _checkin(client)
    _checkin(client, pain="high")

    [row] = _plan_free_rows(store)
    assert row["pain"] == "high"
    assert row["recommendation_state"] == "pull_back"


@pytest.mark.parametrize("payload", [
    _body(phase="GPP"),
    _body(plan_id=PLAN_ID),
])
def test_a_phase_only_travels_with_a_plan(payload):
    client, store, _ = _build_client()
    _seed_plan(store)

    resp = client.post("/api/today/checkin", headers=ATHLETE, json=payload)

    assert resp.status_code == 422
    assert store.today_checkins.get("athlete-1", []) == []


def test_a_stale_client_cannot_check_in_plan_free_over_an_active_plan():
    client, store, _ = _build_client()
    _seed_plan(store)

    resp = _checkin(client)

    assert resp.status_code == 409
    assert store.today_checkins.get("athlete-1", []) == []


def test_a_clean_check_in_says_nothing_about_an_imaginary_session():
    client, store, _ = _build_client()

    body = _checkin(client).json()

    assert body["recommendation_state"] == "train_as_planned"
    lines = body["recommendation_reason"].splitlines()
    assert lines[0] == PLAN_FREE_CLEAN_TITLE
    assert PLAN_FREE_CLEARANCE_NOTE in lines
    text = body["recommendation_reason"].lower()
    assert "planned work" not in text and "full session" not in text
    # A thin evidence base is reported, not hidden.
    assert "sparse_history" in body["triggers"]
    assert "session_unresolved" in body["triggers"]


def test_restrictive_decisions_keep_their_own_copy():
    client, store, _ = _build_client()

    body = _checkin(client, pain="high").json()

    assert body["recommendation_state"] == "pull_back"
    assert body["recommendation_reason"].splitlines()[0] == "Rehab only today."


def test_a_red_flag_stops_training_without_a_plan():
    client, store, _ = _build_client()

    body = _checkin(client, neurological_symptoms=True).json()

    assert body["recommendation_state"] == "pull_back"
    assert body["recommendation_reason"].splitlines()[0] == "No training today."
    assert body["blocks_training"] is True


def test_an_unreadable_injury_history_never_clears_training(monkeypatch):
    client, store, _ = _build_client()

    def broken(*args, **kwargs):
        raise RuntimeError("injury store down")

    monkeypatch.setattr(store, "list_injury_flags", broken)
    body = _checkin(client).json()

    assert body["recommendation_state"] == "pull_back"
    assert body["blocks_training"] is True


def test_an_unreadable_sparring_history_never_clears_training(monkeypatch):
    client, store, _ = _build_client()

    def broken(*args, **kwargs):
        raise RuntimeError("sparring store down")

    monkeypatch.setattr(store, "latest_sparring_log_day", broken)
    body = _checkin(client).json()

    assert body["recommendation_state"] != "train_as_planned"
    assert "sparring_unavailable" in body["reason_codes"]


@pytest.mark.parametrize("days_ago, expected", [(0, "pull_back"), (1, "pull_back"), (2, "train_as_planned")])
def test_a_recent_head_impact_report_rules_out_contact(days_ago, expected):
    client, store, _ = _build_client()
    today = date.fromisoformat(resolve_training_day(None))
    store.sparring_logs.append({"id": str(uuid4()), "athlete_id": "athlete-1", "plan_id": None,
                                "training_day": (today - timedelta(days=days_ago)).isoformat(),
                                "source": "free", "intensity": "hard", "rounds_completed": 4,
                                "head_contact": "heavy", "rocked": True, "notes": "", "created_at": ""})

    body = _checkin(client).json()

    assert body["recommendation_state"] == expected
    if expected == "pull_back":
        assert body["recommendation_reason"].splitlines()[0] == "No contact today."
        assert "recent_head_impact" in body["triggers"]


def test_the_head_impact_rule_also_protects_planned_athletes():
    client, store, _ = _build_client()
    _seed_plan(store)
    store.sparring_logs.append({"id": str(uuid4()), "athlete_id": "athlete-1", "plan_id": None,
                                "training_day": resolve_training_day(None), "source": "free",
                                "intensity": "hard", "rounds_completed": 4, "head_contact": "heavy",
                                "rocked": True, "notes": "", "created_at": ""})

    body = client.post("/api/today/checkin", headers=ATHLETE,
                       json=_body(plan_id=PLAN_ID, phase="GPP")).json()

    assert body["recommendation_state"] == "pull_back"
    assert "recent_head_impact" in body["triggers"]


def test_an_injury_reported_after_a_clean_check_in_refreshes_it():
    client, store, _ = _build_client()
    assert _checkin(client).json()["recommendation_state"] == "train_as_planned"

    resp = client.post("/api/today/injury-checkin", headers=ATHLETE, json={"injuries": [{
        "body_area": "left knee", "description": "sprain [training_impact:cant_train]", "severity": "severe",
    }]})

    assert resp.status_code == 201, resp.text
    [row] = _plan_free_rows(store)
    assert row["recommendation_state"] == "pull_back"
    assert _today(client)["today"]["decision_tier"] in {"pull_back", "stop"}


def test_creating_a_plan_later_reuses_daily_check_in():
    client, store, _ = _build_client()
    _checkin(client, sleep="poor")

    _seed_plan(store)
    today = _today(client)["today"]

    assert today["recommendation_state"] != "not_checked_in"
    assert len(_plan_free_rows(store)) == 1
    planned = client.post("/api/today/checkin", headers=ATHLETE, json=_body(plan_id=PLAN_ID, phase="GPP"))
    assert planned.status_code == 201
    assert len(_plan_free_rows(store)) == 0
    assert len(store.today_checkins["athlete-1"]) == 1


def test_no_health_data_consent_means_no_plan_free_collection():
    client, store, _ = _build_client()
    withdraw_health_consent(store)

    resp = _checkin(client)

    assert resp.status_code == 403
    assert store.today_checkins.get("athlete-1", []) == []


# -- Every Today read re-evaluates the plan-free check-in ----------------------
# The stored recommendation is a check-in-time snapshot. As on the planned path,
# each read evaluates the stored answers against the latest evidence, without
# rewriting the check-in.


def _log_hard_session(client) -> dict:
    resp = client.post("/api/training/sessions", headers=ATHLETE, json={
        "activity": "sparring", "duration_minutes": 60, "effort": "hard", "client_request_id": str(uuid4()),
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_workload_logged_after_the_check_in_is_reflected_on_reload():
    client, store, _ = _build_client()
    _checkin(client, sleep="poor")
    assert "Heavy recent load" not in _today(client)["today"]["recommendation_trigger_labels"]
    stored = dict(_plan_free_rows(store)[0])

    _log_hard_session(client)
    _log_hard_session(client)

    today = _today(client)["today"]
    assert "Heavy recent load" in today["recommendation_trigger_labels"]
    # Read-only: the morning check-in is never rewritten by a read.
    assert _plan_free_rows(store)[0]["recommendation_triggers"] == stored["recommendation_triggers"]
    assert _plan_free_rows(store)[0]["updated_at"] == stored["updated_at"]


def test_removed_workload_is_reflected_on_reload_too():
    client, store, _ = _build_client()
    _checkin(client, sleep="poor")
    first = _log_hard_session(client)
    _log_hard_session(client)
    assert "Heavy recent load" in _today(client)["today"]["recommendation_trigger_labels"]

    assert client.delete(f"/api/training/sessions/{first['id']}", headers=ATHLETE).status_code == 204

    assert "Heavy recent load" not in _today(client)["today"]["recommendation_trigger_labels"]


@pytest.mark.parametrize("method, expected_tier", [
    ("list_injury_flags", "pull_back"),
    ("list_external_training_sessions", "modify"),
    ("list_session_completions", "modify"),
    ("latest_sparring_log_day", "modify"),
])
def test_a_safety_read_failing_after_a_green_check_in_never_shows_green(monkeypatch, method, expected_tier):
    client, store, _ = _build_client()
    assert _checkin(client).json()["recommendation_state"] == "train_as_planned"
    assert _today(client)["today"]["decision_tier"] == "green"

    def broken(*args, **kwargs):
        raise RuntimeError(f"{method} down")

    monkeypatch.setattr(store, method, broken)
    resp = client.get("/api/today", headers=ATHLETE)

    if resp.status_code == 200:
        today = resp.json()["today"]
        assert today["recommendation_state"] != "train_as_planned"
        assert today["decision_tier"] in {expected_tier, "pull_back", "stop"}
    else:
        # Failing closed by making Today unavailable is also never green.
        assert resp.status_code >= 500


def test_a_head_impact_reported_after_a_green_check_in_takes_over_on_reload():
    client, store, _ = _build_client()
    assert _checkin(client).json()["recommendation_state"] == "train_as_planned"

    session = client.post("/api/training/sessions", headers=ATHLETE, json={
        "activity": "sparring", "duration_minutes": 30, "effort": "moderate", "client_request_id": str(uuid4()),
    }).json()
    client.post("/api/today/sparring-log", headers=ATHLETE, json={
        "source": "free", "external_session_id": session["id"], "intensity": "medium", "rounds_completed": 3,
        "head_contact": "heavy", "rocked": True, "client_request_id": str(uuid4()), "detail_revision": 1,
    })

    today = _today(client)["today"]
    assert today["recommendation_state"] == "pull_back"
    assert "Head impact reported" in today["recommendation_trigger_labels"]
    assert today["primary_safety_notice"]["code"] == "head_impact_no_contact"


def test_a_medical_hold_still_outranks_the_morning_check_in():
    client, store, _ = _build_client()
    assert _checkin(client).json()["recommendation_state"] == "train_as_planned"

    client.post("/api/today/injury-checkin", headers=ATHLETE, json={"injuries": [{
        "body_area": "head", "description": "dizziness after a clash of heads", "severity": "severe",
    }]})

    today = _today(client)["today"]
    assert today["recommendation_state"] == "pull_back"
    assert today["decision_tier"] in {"pull_back", "stop"}


@pytest.mark.parametrize("body_area", ["", "core"])
def test_plan_free_injury_names_never_show_internal_markers(body_area):
    # A tracking-only athlete's worsening injury is named in Today's trigger,
    # injury list and copy by its clean label, never the stored description.
    client, store, _ = _build_client()
    store.create_injury_flag("athlete-1", {"body_area": body_area, "description": "Bruise [training_impact:not_limiting]",
                                           "severity": "moderate", "latest_reported_status": "worse"})
    assert _checkin(client, active_injury="worse").status_code in (200, 201)
    resp = client.get("/api/today", headers=ATHLETE)
    assert resp.status_code == 200, resp.text
    today = resp.json()
    # Only the raw description (read by the injury editor) keeps its markers.
    for injury in today["open_injuries"]:
        injury.pop("description", None)
    assert "[training_impact" not in str(today)
    name = "Core bruise" if body_area else "Bruise"
    assert today["today"]["recommendation_trigger_labels"][0] == f"{name} — getting worse"
    assert f"The {name.lower()} injury is worse." in today["today"]["recommendation_reason"]
    assert [injury["label"] for injury in today["open_injuries"]] == [name]
