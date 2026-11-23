"""Coach chat: who can push, what a push says, who gets file links, and nudges."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from api.services import coach_chat
from api.services.notification_foundation import (
    CONVERSATION_DAILY_CAP, EVENT_DAILY_CAP, NotificationCandidate, _candidate_daily_cap, _candidate_min_spacing,
)
from tests.test_coach_fighter_access import AS_COACH, COACH, FIGHTER, STRANGER
from tests.test_coach_fighter_access import camp  # noqa: F401 - shared pytest fixture

AS_FIGHTER = {"Authorization": "Bearer fighter-token"}
AS_STRANGER = {"Authorization": "Bearer stranger-token"}
CHAT = str(uuid4())


@pytest.fixture
def pushes(monkeypatch):
    sent: list[NotificationCandidate] = []
    monkeypatch.setattr(coach_chat, "dispatch_push_candidate", lambda _store, candidate, **_: sent.append(candidate) or 1)
    return sent


def _message(store, message_id=1, **changes):
    row = {"id": message_id, "chat_id": CHAT, "coach_id": COACH, "athlete_id": FIGHTER, "sender_id": FIGHTER,
           "kind": "text", "body": "My knee felt off on pads", "quote": None, "asset_id": None,
           "created_at": datetime.now(timezone.utc).isoformat(), **changes}
    store.coach_chat_messages[message_id] = row
    return row


def test_a_new_message_pushes_the_other_side_without_its_text(camp, pushes):  # noqa: F811
    client, store, _ = camp
    store.profiles[FIGHTER]["full_name"] = "Leo Martins"
    _message(store)
    response = client.post("/api/chats/messages/1/notify", headers=AS_FIGHTER)
    assert response.status_code == 202, response.text
    assert len(pushes) == 1
    push = pushes[0]
    assert push.profile_id == COACH
    assert push.title == "Leo"
    assert "knee" not in (push.title + push.body).lower()
    assert push.url == f"/coach/chats?with={FIGHTER}"
    assert push.category == "coach_messages" and push.notification_class == "conversation"
    assert push.tag == f"coach-chat-{CHAT}" and push.dedupe_key == "coach-chat:1"


def test_the_coach_side_push_opens_the_fighters_chat(camp, pushes):  # noqa: F811
    client, store, _ = camp
    _message(store, 2, sender_id=COACH, kind="photo", body="")
    assert client.post("/api/chats/messages/2/notify", headers=AS_COACH).status_code == 202
    assert pushes[0].profile_id == FIGHTER
    assert pushes[0].url == f"/chat?with={COACH}"
    assert pushes[0].body == "Sent you a photo."


def test_only_the_sender_can_push_and_only_while_connected(camp, pushes):  # noqa: F811
    client, store, connection = camp
    _message(store)
    assert client.post("/api/chats/messages/1/notify", headers=AS_COACH).status_code == 404
    assert client.post("/api/chats/messages/1/notify", headers=AS_STRANGER).status_code == 404
    assert client.post("/api/chats/messages/99/notify", headers=AS_FIGHTER).status_code == 404
    assert client.post("/api/chats/messages/1/notify").status_code == 401
    connection["status"] = "revoked"
    assert client.post("/api/chats/messages/1/notify", headers=AS_FIGHTER).status_code == 202
    assert pushes == []


def test_an_old_message_is_never_pushed_again(camp, pushes):  # noqa: F811
    client, store, _ = camp
    _message(store, created_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    assert client.post("/api/chats/messages/1/notify", headers=AS_FIGHTER).status_code == 202
    assert pushes == []


@pytest.fixture
def files(camp, monkeypatch):  # noqa: F811
    client, store, connection = camp
    store.attachment_assets["photo-1"] = {"id": "photo-1", "owner_id": FIGHTER, "kind": "photo", "status": "ready",
                                          "ready_path": "f/photo-1/clean.jpg", "width": 800, "height": 600}
    store.attachment_assets["voice-1"] = {"id": "voice-1", "owner_id": COACH, "kind": "voice", "status": "ready",
                                          "ready_path": "c/voice-1/clean.wav", "duration_ms": 4200}
    _message(store, 1, kind="photo", body="", asset_id="photo-1")
    _message(store, 2, sender_id=COACH, kind="voice", body="", asset_id="voice-1")
    _message(store, 3, coach_id=STRANGER, athlete_id=str(uuid4()), kind="photo", asset_id="photo-1")
    signed: list[list[str]] = []
    monkeypatch.setattr(store, "attachment_signed_urls",
                        lambda paths, *, expires_in: signed.append(list(paths)) or {p: f"https://signed/{p}" for p in paths})
    return client, store, connection, signed


def test_participants_get_every_file_link_in_one_signing_call(files):
    client, _store, _connection, signed = files
    response = client.get("/api/chats/files?ids=1,2,3", headers=AS_FIGHTER)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"].startswith("no-store")
    links = {row["message_id"]: row for row in response.json()}
    assert set(links) == {1, 2}
    assert links[1]["thumb_url"] == "https://signed/f/photo-1/thumb.jpg"
    assert links[2]["kind"] == "voice" and links[2]["duration_ms"] == 4200
    assert len(signed) == 1


def test_file_links_follow_the_connection_for_the_coach_only(files):
    client, _store, connection, _signed = files
    assert client.get("/api/chats/files?ids=1,2", headers=AS_STRANGER).json() == []
    connection["status"] = "revoked"
    assert client.get("/api/chats/files?ids=1,2", headers=AS_COACH).json() == []
    # The fighter keeps their history.
    assert len(client.get("/api/chats/files?ids=1,2", headers=AS_FIGHTER).json()) == 2


@pytest.mark.parametrize("ids", ["", "a,b", "0", ",".join(str(n) for n in range(1, 32))])
def test_file_link_requests_are_bounded(files, ids):
    client, *_ = files
    assert client.get(f"/api/chats/files?ids={ids}", headers=AS_FIGHTER).status_code == 422


def test_due_nudges_are_sent_once_with_the_coachs_name(camp, pushes):  # noqa: F811
    _client, store, _ = camp
    store.due_checkin_nudges = [{"nudge_id": "n-1", "coach_id": COACH, "athlete_id": FIGHTER, "coach_name": "Rey Silva",
                                 "timezone": "Europe/London", "training_day": "2026-11-23"}]
    assert coach_chat.run_coach_checkin_nudge_sweep(store) == 1
    push = pushes[0]
    assert push.profile_id == FIGHTER and push.title == "Rey wants your check-in"
    assert push.category == "checkin_reminders" and push.url == "/today#today-checkin"
    assert push.dedupe_key == "coach-checkin:n-1:2026-11-23" and push.training_day == "2026-11-23"
    assert coach_chat.run_coach_checkin_nudge_sweep(store) == 0


def test_a_failed_nudge_never_stops_the_sweep(camp, monkeypatch):  # noqa: F811
    _client, store, _ = camp
    store.due_checkin_nudges = [{"nudge_id": f"n-{n}", "athlete_id": FIGHTER, "coach_name": "", "timezone": "Nowhere/Land",
                                 "training_day": "2026-11-23"} for n in range(2)]
    calls = []

    def flaky(_store, candidate, **_):
        calls.append(candidate)
        if len(calls) == 1:
            raise RuntimeError("push service down")
        return 1
    monkeypatch.setattr(coach_chat, "dispatch_push_candidate", flaky)
    assert coach_chat.run_coach_checkin_nudge_sweep(store) == 1
    assert calls[1].title == "Your coach wants your check-in" and calls[1].timezone_name == "UTC"


def test_chat_pushes_have_their_own_daily_allowance():
    candidate = coach_chat.build_chat_push(
        message={"id": 5, "chat_id": CHAT, "coach_id": COACH, "athlete_id": FIGHTER, "sender_id": COACH, "kind": "text",
                 "body": "", "quote": {"title": "Sparring"}},
        recipient={"id": FIGHTER, "athlete_timezone": ""}, sender_name="", now_utc=datetime.now(timezone.utc))
    assert candidate.body == "Shared a training session with you."
    assert candidate.title == "Your coach"
    assert _candidate_daily_cap(candidate) == CONVERSATION_DAILY_CAP > EVENT_DAILY_CAP
    assert _candidate_min_spacing(candidate) == 1
