"""The AI cue a saved note produced: read back right after the save, and shown
beside its note in History (sessions and sparring)."""
from __future__ import annotations

from uuid import uuid4

from api.routes import today as today_routes
from tests.support import withdraw_health_consent
from tests.test_training_focus_inference import (  # noqa: F401 - fixture
    AUTH, SPLIT_SQUAT, api, edit_notes, lower_body_completion, manual_session, save_takeaway, sparring_detail,
)

BOXING = {"focus": "Turn your hips through each hook.", "scope": "sport_technique", "category": "sparring",
          "sport": "boxing", "exercise_targets": [], "movement_patterns": []}


def read_cue(client, kind, source_id):
    response = client.get(f"/api/training/focus-cues/{kind}/{source_id}", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def history(client):
    response = client.get("/api/training/history?limit=50", headers=AUTH)
    assert response.status_code == 200, response.text
    return {row["id"]: row for row in response.json()["sessions"]}


def test_a_saved_note_reads_back_its_cue(api):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    cue = read_cue(client, "plan_completion", row["id"])
    assert cue["status"] == "ready" and cue["focus"] == SPLIT_SQUAT["focus"] and cue["id"]


def test_a_note_without_an_inference_yet_reads_pending(api):
    client, _, _ = api
    assert read_cue(client, "manual_takeaway", uuid4()) == {"status": "pending", "id": None, "focus": None}


def test_no_suggestion_health_and_dismissed_read_none(api):
    client, store, fake = api
    fake.answer = None
    vague = lower_body_completion(store, session_id="s-vague")
    edit_notes(client, vague["id"], "Good session.")
    assert read_cue(client, "plan_completion", vague["id"])["status"] == "none"

    hurt = lower_body_completion(store, session_id="s-hurt")
    edit_notes(client, hurt["id"], "Knee pain on split squats.")
    assert read_cue(client, "plan_completion", hurt["id"]) == {"status": "none", "id": None, "focus": None}

    fake.answer = SPLIT_SQUAT
    kept = lower_body_completion(store, session_id="s-kept")
    edit_notes(client, kept["id"], "Balance on split squats was poor.")
    cue = read_cue(client, "plan_completion", kept["id"])
    assert client.delete(f"/api/today/training-focus/{cue['id']}", headers=AUTH).status_code == 204
    assert read_cue(client, "plan_completion", kept["id"])["status"] == "none"


def test_cues_are_only_read_with_health_consent(api):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    withdraw_health_consent(store)
    assert read_cue(client, "plan_completion", row["id"])["status"] == "none"
    assert history(client)[str(row["id"])]["focus_cue"] is None


def test_history_shows_each_note_cue_beside_it(api):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    completion = lower_body_completion(store)
    edit_notes(client, completion["id"], "Balance on split squats was poor.")
    fake.answer = {"focus": "Keep your hips square on each lunge.", "scope": "specific_exercise",
                   "category": "strength", "sport": None, "exercise_targets": ["walking lunge"], "movement_patterns": []}
    manual = manual_session(client)
    client.put(f"/api/training/sessions/{manual['id']}/exercises/{uuid4()}", headers=AUTH,
               json={"exercise": {"name": "Walking Lunge"}, "format": "strength", "actual": {"sets": 3, "reps": 10}})
    save_takeaway(client, manual["id"], "Hips kept twisting on the lunges.")
    plain = manual_session(client)

    rows = history(client)
    assert rows[str(completion["id"])]["focus_cue"] == SPLIT_SQUAT["focus"]
    assert rows[manual["id"]]["focus_cue"] == "Keep your hips square on each lunge."
    assert rows[plain["id"]]["focus_cue"] is None


def test_sparring_history_shows_the_cue_from_its_notes(api):
    client, _, fake = api
    fake.answer = BOXING
    session = manual_session(client, activity="sparring", sport="boxing", session_type="sparring", effort="hard")
    log = sparring_detail(client, session["id"], notes="Hooks were all arm, no power.")
    assert read_cue(client, "sparring_log", log["id"])["focus"] == BOXING["focus"]
    response = client.get("/api/today/sparring-logs", headers=AUTH)
    assert response.status_code == 200, response.text
    [shown] = [item for item in response.json()["logs"] if item["id"] == log["id"]]
    assert shown["focus_cue"] == BOXING["focus"]


def test_edited_or_cleared_plan_note_never_shows_an_old_cue(api, monkeypatch):
    client, store, fake = api
    fake.answer = SPLIT_SQUAT
    row = lower_body_completion(store)
    edit_notes(client, row["id"], "Balance on split squats was poor.")
    assert read_cue(client, "plan_completion", row["id"])["status"] == "ready"

    # Simulate the window between saving the edit and its background inference.
    monkeypatch.setattr(today_routes, "schedule_focus_inference", lambda *args, **kwargs: None)
    edit_notes(client, row["id"], "Balance felt better today.")
    assert read_cue(client, "plan_completion", row["id"])["status"] == "pending"
    assert history(client)[str(row["id"])]["focus_cue"] is None

    edit_notes(client, row["id"], "")
    assert read_cue(client, "plan_completion", row["id"])["status"] == "none"
    assert history(client)[str(row["id"])]["focus_cue"] is None


def test_edited_or_cleared_manual_takeaway_never_shows_an_old_cue(api, monkeypatch):
    client, store, fake = api
    fake.answer = {"focus": "Keep your hips square on each lunge.", "scope": "specific_exercise",
                   "category": "strength", "sport": None, "exercise_targets": ["walking lunge"], "movement_patterns": []}
    manual = manual_session(client)
    client.put(f"/api/training/sessions/{manual['id']}/exercises/{uuid4()}", headers=AUTH,
               json={"exercise": {"name": "Walking Lunge"}, "format": "strength", "actual": {"sets": 3, "reps": 10}})
    save_takeaway(client, manual["id"], "Hips kept twisting on lunges.")
    assert read_cue(client, "manual_takeaway", manual["id"])["status"] == "ready"

    monkeypatch.setattr(today_routes, "schedule_focus_inference", lambda *args, **kwargs: None)
    save_takeaway(client, manual["id"], "Hips were steady this session.")
    assert read_cue(client, "manual_takeaway", manual["id"])["status"] == "pending"
    assert history(client)[manual["id"]]["focus_cue"] is None

    save_takeaway(client, manual["id"], "")
    assert read_cue(client, "manual_takeaway", manual["id"])["status"] == "none"
    assert history(client)[manual["id"]]["focus_cue"] is None


def test_edited_or_cleared_sparring_notes_never_show_an_old_cue(api, monkeypatch):
    client, store, fake = api
    fake.answer = BOXING
    session = manual_session(client, activity="sparring", sport="boxing", session_type="sparring", effort="hard")
    log = sparring_detail(client, session["id"], notes="Hooks were all arm, no power.")
    assert read_cue(client, "sparring_log", log["id"])["status"] == "ready"

    def shown():
        response = client.get("/api/today/sparring-logs", headers=AUTH)
        assert response.status_code == 200, response.text
        return next(item for item in response.json()["logs"] if item["id"] == log["id"])

    monkeypatch.setattr(today_routes, "schedule_focus_inference", lambda *args, **kwargs: None)
    edited = sparring_detail(client, session["id"], notes="Hooks were much cleaner.", detail_revision=2)
    assert edited["id"] == log["id"]
    assert read_cue(client, "sparring_log", log["id"])["status"] == "pending"
    assert shown()["focus_cue"] is None

    sparring_detail(client, session["id"], notes="", detail_revision=3)
    assert read_cue(client, "sparring_log", log["id"])["status"] == "none"
    assert shown()["focus_cue"] is None
