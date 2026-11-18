"""Pushes between a coach and a connected fighter."""
from uuid import uuid4

import pytest

from api.auth import AuthenticatedUser
from api.services import coach_notifications
from tests.support import DEFAULT_ATHLETE_USER, _build_client, grant_default_compliance
from tests.test_api_sparring_log import _post, _seed_plan
from tests.test_coach_fighter_access import AS_COACH, BASE, COACH, FIGHTER
from tests.test_coach_fighter_access import camp  # noqa: F401 - shared pytest fixture
from tests.test_coach_plans import ASSIGNMENTS, create, plans, send  # noqa: F401 - shared pytest fixture
from tests.test_custom_open_plans import body

AS_FIGHTER = {"Authorization": "Bearer fighter-token"}


@pytest.fixture
def pushes(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(coach_notifications, "send_coach_message_notification",
                        lambda _store, **kwargs: sent.append(kwargs) or 1)
    return sent


def _feedback(store, **changes):
    row = {"id": str(uuid4()), "athlete_id": FIGHTER, "coach_id": COACH, "coach_name": "Sam Okafor",
           "source": "sparring", "target_id": str(uuid4()), "exercise_ref": "", "note": "Hands up on the exit.",
           "created_at": "2026-11-17T10:00:00+00:00", "updated_at": "2026-11-17T10:00:00+00:00", **changes}
    store.coach_feedback.append(row)
    return row


def test_saved_feedback_pushes_the_fighter_without_the_note_text(camp, pushes):  # noqa: F811
    client, store, _ = camp
    note = _feedback(store)
    response = client.post(f"{BASE}/feedback/notify", headers=AS_COACH,
                           json={"source": "sparring", "target_id": note["target_id"]})
    assert response.status_code == 202, response.text
    assert len(pushes) == 1
    push = pushes[0]
    assert push["athlete_id"] == FIGHTER and push["url"] == "/history"
    assert push["title"] == "Feedback from Sam"
    assert "Hands up" not in push["title"] + push["body"]
    # An edit is a new message; a retry of the same save is the same one.
    assert push["message_id"] == f"feedback:{note['id']}:{note['updated_at']}"


def test_feedback_notice_needs_this_coaches_own_note_and_a_live_connection(camp, pushes):  # noqa: F811
    client, store, connection = camp
    other = _feedback(store, coach_id=str(uuid4()))
    assert client.post(f"{BASE}/feedback/notify", headers=AS_COACH,
                       json={"source": "sparring", "target_id": other["target_id"]}).status_code == 404
    mine = _feedback(store)
    connection["status"] = "revoked"
    assert client.post(f"{BASE}/feedback/notify", headers=AS_COACH,
                       json={"source": "sparring", "target_id": mine["target_id"]}).status_code == 404
    assert client.post(f"{BASE}/feedback/notify", headers=AS_FIGHTER,
                       json={"source": "sparring", "target_id": mine["target_id"]}).status_code == 404
    assert pushes == []


def test_sending_a_plan_pushes_the_fighter_once(plans, pushes):  # noqa: F811
    client, _store, _ = plans
    row, _ = create(client)
    send(client, row)
    assert [(p["athlete_id"], p["url"]) for p in pushes] == [(FIGHTER, "/plans")]
    assert pushes[0]["title"] == "Coach sent you a plan"
    send(client, row)
    assert len(pushes) == 1


def test_fighter_answers_push_the_coach(plans, pushes):  # noqa: F811
    client, store, _ = plans
    declined, _ = create(client)
    send(client, declined)
    accepted, _ = create(client, body())
    send(client, accepted)
    pushes.clear()

    assert client.post(f"{ASSIGNMENTS}/{declined['id']}/decline", headers=AS_FIGHTER).status_code == 200
    response = client.post(f"{ASSIGNMENTS}/{accepted['id']}/activate", headers=AS_FIGHTER,
                           json={"overlap_action": "replace"})
    assert response.status_code == 200, response.text
    assert [(p["athlete_id"], p["message_id"], p["url"]) for p in pushes] == [
        (COACH, f"plan-declined:{declined['id']}", f"/coach/fighters/{FIGHTER}"),
        (COACH, f"plan-accepted:{accepted['id']}", f"/coach/fighters/{FIGHTER}"),
    ]
    assert pushes[0]["title"].endswith("declined your plan")


@pytest.fixture
def sparring():
    client, store, _ = _build_client()
    store.ensure_profile(DEFAULT_ATHLETE_USER)
    grant_default_compliance(store)
    coaches = []
    for share in (True, False):
        coach = AuthenticatedUser(user_id=str(uuid4()), email=f"{uuid4()}@example.com", full_name="Coach", metadata={})
        store.ensure_profile(coach)
        store.profiles[coach.user_id]["role"] = "coach"
        store.coach_connections.append({"id": str(uuid4()), "coach_id": coach.user_id, "athlete_id": "athlete-1",
                                        "status": "accepted", "share_injuries": share})
        coaches.append(coach.user_id)
    _seed_plan(store)
    return client, store, coaches


@pytest.mark.parametrize("report", [{"rocked": True}, {"head_contact": "heavy"}])
def test_head_contact_pushes_only_coaches_who_see_injury_status(sparring, pushes, report):
    client, _store, (sharing, _not_sharing) = sparring
    response = _post(client, **report)
    assert response.status_code == 201, response.text
    assert [(p["athlete_id"], p["urgent"], p["url"]) for p in pushes] == [(sharing, True, "/coach/fighters/athlete-1")]
    assert pushes[0]["title"] == "Check in with Ari"
    assert "rocked" not in pushes[0]["body"].lower()


def test_ordinary_sparring_pushes_no_one(sparring, pushes):
    client, _store, _ = sparring
    assert _post(client).status_code == 201
    assert pushes == []
