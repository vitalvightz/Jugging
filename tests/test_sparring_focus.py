"""The optional sparring focus and the "Last training focus" read."""
from datetime import date, timedelta
from uuid import uuid4

from api.services.today_service import resolve_training_day
from tests.support import _build_client, withdraw_health_consent
from tests.test_api_sparring_log import ATHLETE, _manual_detail, _manual_session, _post, _seed_plan


def focus(client):
    response = client.get("/api/today/training-focus", headers=ATHLETE)
    assert response.status_code == 200, response.text
    return response.json()


def test_no_focus_before_any_sparring_or_when_left_blank():
    client, store, _ = _build_client()
    assert focus(client)["focus"] is None
    _seed_plan(store)
    assert _post(client).status_code == 201
    assert focus(client)["focus"] is None


def test_timer_sparring_focus_is_saved_and_read_back():
    client, store, _ = _build_client()
    _seed_plan(store)
    response = _post(client, focus="  Keep my right hand up after jabbing  ")
    assert response.status_code == 201, response.text
    assert response.json()["log"]["focus"] == "Keep my right hand up after jabbing"
    assert focus(client) == {"focus": "Keep my right hand up after jabbing",
                             "training_day": resolve_training_day(None), "sport": None}


def test_focus_write_never_goes_through_the_safety_rpc_fields():
    client, store, _ = _build_client()
    _seed_plan(store)
    _post(client, focus="Exit on an angle", rocked=True)
    # The rocked review is still raised; the focus sits beside it.
    assert len(store.admin_reviews) == 1
    assert store.sparring_logs[0]["focus"] == "Exit on an angle"


def test_focus_is_capped():
    client, store, _ = _build_client()
    _seed_plan(store)
    assert _post(client, focus="x" * 141).status_code == 422


def test_newest_focus_wins_and_a_correction_updates_it():
    client, _, _ = _build_client()
    session = _manual_session(client)
    first = _manual_detail(client, session["id"], client_request_id=str(uuid4()), detail_revision=1, focus="Jab first")
    assert first.status_code == 201, first.text
    corrected = _manual_detail(client, session["id"], client_request_id=str(uuid4()), detail_revision=2, focus="Double the jab")
    assert corrected.json()["log"]["focus"] == "Double the jab"
    assert focus(client)["focus"] == "Double the jab"


def test_a_late_older_correction_does_not_overwrite_a_newer_focus():
    client, _, _ = _build_client()
    session = _manual_session(client)
    _manual_detail(client, session["id"], client_request_id=str(uuid4()), detail_revision=2, focus="Newer")
    late = _manual_detail(client, session["id"], client_request_id=str(uuid4()), detail_revision=1, focus="Older")
    assert late.status_code == 201
    assert focus(client)["focus"] == "Newer"


def test_deleting_the_manual_session_removes_its_focus():
    client, _, _ = _build_client()
    session = _manual_session(client)
    _manual_detail(client, session["id"], client_request_id=str(uuid4()), detail_revision=1, focus="Slip and counter")
    assert focus(client)["focus"] == "Slip and counter"
    assert client.delete(f"/api/training/sessions/{session['id']}", headers=ATHLETE).status_code == 204
    assert focus(client)["focus"] is None


def test_stale_focus_is_not_shown():
    client, store, _ = _build_client()
    old_day = (date.fromisoformat(resolve_training_day(None)) - timedelta(days=28)).isoformat()
    store.sparring_logs.append({"id": str(uuid4()), "athlete_id": "athlete-1", "training_day": old_day, "source": "free",
                                "intensity": "hard", "rounds_completed": 3, "head_contact": "none", "rocked": False,
                                "notes": "", "focus": "Old news", "created_at": f"{old_day}T10:00:00+00:00"})
    assert focus(client)["focus"] is None


def test_manual_focus_carries_its_sport():
    client, _, _ = _build_client()
    response = client.post("/api/training/sessions", headers=ATHLETE, json={
        "activity": "sparring", "sport": "muay_thai", "session_type": "sparring", "duration_minutes": 45,
        "effort": "hard", "client_request_id": str(uuid4())})
    assert response.status_code == 201, response.text
    _manual_detail(client, response.json()["id"], client_request_id=str(uuid4()), detail_revision=1, focus="Check the low kick")
    assert focus(client)["sport"] == "muay_thai"


def test_focus_is_hidden_without_health_consent():
    client, store, _ = _build_client()
    _seed_plan(store)
    _post(client, focus="Hands up")
    withdraw_health_consent(store)
    assert focus(client)["focus"] is None
