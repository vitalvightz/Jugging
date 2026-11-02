"""History's limited edits to plan sessions, and run distance/time on manual work."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from api.contracts.external_training import ExternalTrainingFields
from tests.support import _build_client

AUTH = {"Authorization": "Bearer athlete-token"}
DAY = "2026-10-10"


@pytest.fixture
def api():
    client, store, _ = _build_client()
    return client, store


def completion(store, **changes):
    fields = {"plan_id": str(uuid4()), "session_id": "s-strength", "training_day": DAY, "status": "done",
              "completion_origin": "reported", "session_rpe": 7, "pain_after": 1, "notes": "felt ok",
              "completed_at": f"{DAY}T10:00:00+00:00", **changes}
    return store.upsert_session_completion("athlete-1", fields)


def history_rows(client):
    response = client.get("/api/training/history", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()["sessions"]


def test_plan_session_edit_changes_only_rpe_pain_and_notes(api):
    client, store = api
    row = completion(store)
    assert next(item for item in history_rows(client) if item["id"] == row["id"])["history_editable"] is True
    response = client.patch(f"/api/training/history/unlxck/{row['id']}", headers=AUTH,
                            json={"session_rpe": 9, "pain_after": 0, "notes": "  harder than planned "})
    assert response.status_code == 200, response.text
    assert response.json()["session_rpe"] == 9 and response.json()["notes"] == "harder than planned"
    stored = store.get_session_completion_by_id("athlete-1", row["id"])
    assert stored["status"] == "done" and stored["pain_after"] == 0 and stored["plan_id"] == row["plan_id"]


def test_plan_session_edit_rejects_other_fields(api):
    client, store = api
    row = completion(store)
    response = client.patch(f"/api/training/history/unlxck/{row['id']}", headers=AUTH, json={"status": "skipped"})
    assert response.status_code == 422


def test_plan_session_delete_marks_not_done_and_leaves_history(api):
    client, store = api
    row = completion(store)
    assert client.delete(f"/api/training/history/unlxck/{row['id']}", headers=AUTH).status_code == 204
    stored = store.get_session_completion_by_id("athlete-1", row["id"])
    assert stored["status"] == "not_started" and stored["completed_at"] is None and stored["session_rpe"] is None
    assert all(item["id"] != row["id"] for item in history_rows(client))
    # A retried delete after a lost response is still a success.
    assert client.delete(f"/api/training/history/unlxck/{row['id']}", headers=AUTH).status_code == 204


@pytest.mark.parametrize("changes", [
    {"completion_origin": "day_credit"},
    {"completion_origin": None},
    {"rehab_response_contexts": [{"injury_id": "i"}]},
    {"rehab_performance": "completed"},
])
def test_protected_plan_sessions_cannot_change(api, changes):
    client, store = api
    row = completion(store, **changes)
    assert client.patch(f"/api/training/history/unlxck/{row['id']}", headers=AUTH, json={"session_rpe": 5}).status_code == 409
    assert client.delete(f"/api/training/history/unlxck/{row['id']}", headers=AUTH).status_code == 409


def test_unknown_or_foreign_plan_session_is_not_found(api):
    client, store = api
    other = store.upsert_session_completion("athlete-2", {"plan_id": str(uuid4()), "session_id": "s", "training_day": DAY,
                                                         "status": "done", "completion_origin": "reported"})
    for completion_id in (str(uuid4()), other["id"]):
        assert client.patch(f"/api/training/history/unlxck/{completion_id}", headers=AUTH, json={}).status_code == 404
        assert client.delete(f"/api/training/history/unlxck/{completion_id}", headers=AUTH).status_code == 404


def run(**changes):
    return {"activity": "roadwork", "duration_minutes": 25, "effort": "moderate",
            "distance_value": 5, "distance_unit": "km", "duration_seconds": 24 * 60 + 35, **changes}


def test_run_distance_and_time_are_logged_and_edited(api):
    client, _ = api
    response = client.post("/api/training/sessions", headers=AUTH, json={**run(), "client_request_id": str(uuid4())})
    assert response.status_code == 201, response.text
    row = response.json()
    assert (row["distance_value"], row["distance_unit"], row["duration_seconds"]) == (5, "km", 1475)
    edited = client.patch(f"/api/training/sessions/{row['id']}", headers=AUTH,
                          json=run(distance_value=3.1, distance_unit="miles"))
    assert edited.status_code == 200, edited.text
    assert (edited.json()["distance_value"], edited.json()["distance_unit"]) == (3.1, "miles")


@pytest.mark.parametrize("changes", [
    {"activity": "strength"},
    {"distance_unit": None},
    {"distance_value": 201},
    {"distance_unit": "miles", "distance_value": 126},
    {"duration_minutes": 24},
    {"duration_seconds": 0},
])
def test_invalid_run_detail_is_rejected(changes):
    with pytest.raises(ValidationError):
        ExternalTrainingFields(**run(**changes))


def test_sessions_without_run_detail_still_validate():
    assert ExternalTrainingFields(activity="sparring", duration_minutes=60, effort="hard").distance_value is None
