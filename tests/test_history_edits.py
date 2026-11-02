"""History's limited edits to plan sessions, and run distance/time on manual work."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from api.contracts.external_training import ExternalTrainingFields
from tests.support import _build_client, withdraw_health_consent

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
                          json=run(distance_value=3.1, distance_unit="miles", duration_minutes=27, duration_seconds=26 * 60 + 5))
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert (body["distance_value"], body["distance_unit"], body["duration_seconds"], body["duration_minutes"]) == (3.1, "miles", 1565, 27)


@pytest.mark.parametrize("changes", [
    {"activity": "strength"},
    {"distance_unit": None},
    {"distance_value": 201},
    {"distance_unit": "miles", "distance_value": 126},
    {"duration_minutes": 24},
    {"duration_seconds": 0},
    {"activity": "strength", "distance_value": None, "distance_unit": None},
    {"distance_value": 0.001},
])
def test_invalid_run_detail_is_rejected(changes):
    with pytest.raises(ValidationError):
        ExternalTrainingFields(**run(**changes))


def test_sessions_without_run_detail_still_validate():
    assert ExternalTrainingFields(activity="sparring", duration_minutes=60, effort="hard").distance_value is None


def test_removal_clears_the_sessions_exercise_results_but_keeps_the_completion_id(api):
    client, store = api
    row = completion(store)
    keep = {"athlete_id": "athlete-1", "plan_id": row["plan_id"], "session_id": "other", "training_day": DAY, "block_id": "b2"}
    store.exercise_logs.extend([
        {"athlete_id": "athlete-1", "plan_id": row["plan_id"], "session_id": "s-strength", "training_day": DAY, "block_id": "b1"},
        keep,
    ])
    assert client.delete(f"/api/training/history/unlxck/{row['id']}", headers=AUTH).status_code == 204
    # Only the removed session's results go; other sessions that day stay.
    assert store.exercise_logs == [keep]
    # Redoing it writes to the same row, and XP is keyed by that id, so no second award.
    redone = store.upsert_session_completion("athlete-1", {"plan_id": row["plan_id"], "session_id": "s-strength",
                                                          "training_day": DAY, "status": "done"})
    assert redone["id"] == row["id"]


def test_a_refused_removal_changes_nothing(api):
    # The database refuses (e.g. the rehab reset guard): nothing is half-done.
    client, store = api
    row = completion(store)
    log = {"athlete_id": "athlete-1", "plan_id": row["plan_id"], "session_id": "s-strength", "training_day": DAY, "block_id": "b1"}
    store.exercise_logs.append(log)
    store.fail_history_reset = True
    assert client.delete(f"/api/training/history/unlxck/{row['id']}", headers=AUTH).status_code == 409
    assert store.exercise_logs == [log]
    assert store.get_session_completion_by_id("athlete-1", row["id"])["status"] == "done"
    assert any(item["id"] == row["id"] for item in history_rows(client))


def test_removal_goes_through_the_single_atomic_store_call(api, monkeypatch):
    client, store = api
    row = completion(store)
    calls = []
    real = store.reset_history_plan_session
    monkeypatch.setattr(store, "reset_history_plan_session",
                        lambda athlete_id, completion_id: calls.append(completion_id) or real(athlete_id, completion_id))
    assert client.delete(f"/api/training/history/unlxck/{row['id']}", headers=AUTH).status_code == 204
    assert calls == [row["id"]]


def test_partial_plan_session_edit_keeps_the_other_fields(api):
    client, store = api
    row = completion(store)
    response = client.patch(f"/api/training/history/unlxck/{row['id']}", headers=AUTH, json={"session_rpe": 9})
    assert response.status_code == 200, response.text
    stored = store.get_session_completion_by_id("athlete-1", row["id"])
    assert (stored["session_rpe"], stored["pain_after"], stored["notes"]) == (9, 1, "felt ok")


def test_plan_session_edit_works_without_health_consent_but_keeps_pain(api):
    client, store = api
    row = completion(store)
    withdraw_health_consent(store)
    response = client.patch(f"/api/training/history/unlxck/{row['id']}", headers=AUTH,
                            json={"session_rpe": 6, "pain_after": 9, "notes": "ok"})
    assert response.status_code == 200, response.text
    stored = store.get_session_completion_by_id("athlete-1", row["id"])
    assert (stored["session_rpe"], stored["pain_after"], stored["notes"]) == (6, 1, "ok")


@pytest.mark.parametrize("block", [{"block_type": "rehab"}, {"block_type": "strength", "policy_id": "p"},
                                   {"block_type": "strength", "clinical_review_pin": {}}])
def test_a_saved_rehab_prescription_locks_the_session(api, block):
    client, store = api
    row = completion(store, prescription_snapshot={"session": {"blocks": [block]}})
    assert next(item for item in history_rows(client) if item["id"] == row["id"])["history_editable"] is False
    assert client.delete(f"/api/training/history/unlxck/{row['id']}", headers=AUTH).status_code == 409
