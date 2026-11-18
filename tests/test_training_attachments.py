"""Form-check videos on an athlete's own logged work, and what a coach sees."""
from uuid import uuid4

import pytest

from tests.test_coach_fighter_access import AS_COACH, BASE, FIGHTER
from tests.test_coach_fighter_access import camp  # noqa: F401 - shared pytest fixture

AS_FIGHTER = {"Authorization": "Bearer fighter-token"}
URL = "/api/training/attachments"


def _video(store, owner=FIGHTER, status="verified"):
    asset = {"id": str(uuid4()), "athlete_id": owner, "status": status, "rendition_path": f"{owner}/demo.mp4"}
    store.private_video_assets[asset["id"]] = asset
    return store.create_private_media(owner, {"provider": "uploaded", "asset_id": asset["id"], "start_s": 0,
                                              "end_s": 12, "orientation": "portrait"})["id"]


@pytest.fixture
def logged(camp):  # noqa: F811 - pytest fixture injection
    client, store, connection = camp
    store.private_video_signed_url = lambda path, *, expires_in: f"https://signed.example/{path}?ttl={expires_in}"
    session = store.create_external_training_session(FIGHTER, {
        "training_day": "2026-11-15", "activity": "strength", "duration_minutes": 40, "effort": "moderate",
        "client_request_id": str(uuid4()), "request_fingerprint": "a" * 64})
    return client, store, connection, session["id"]


def _attach(client, media_id, target, **extra):
    return client.post(URL, headers=AS_FIGHTER, json={"media_id": media_id, "source": "external",
                                                      "target_id": target, **extra})


def test_athlete_attaches_lists_and_removes_their_own_video(logged):
    client, store, _, target = logged
    created = _attach(client, _video(store), target)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["media"]["url"].startswith("https://signed.example/") and "ttl=60" in body["media"]["url"]

    listed = client.get(URL, headers=AS_FIGHTER, params={"source": "external", "target_id": target})
    assert [row["id"] for row in listed.json()] == [body["id"]]
    # A different exercise on the same record is a separate list.
    assert client.get(URL, headers=AS_FIGHTER, params={"source": "external", "target_id": target,
                                                       "exercise_ref": "squat"}).json() == []

    assert client.delete(f"{URL}/{body['id']}", headers=AS_FIGHTER).status_code == 204
    assert client.delete(f"{URL}/{body['id']}", headers=AS_FIGHTER).status_code == 204
    assert client.get(URL, headers=AS_FIGHTER, params={"source": "external", "target_id": target}).json() == []


def test_only_own_verified_videos_on_own_records(logged):
    client, store, _, target = logged
    assert _attach(client, _video(store, owner=str(uuid4())), target).status_code == 404
    assert _attach(client, _video(store, status="processing"), target).status_code == 409
    assert _attach(client, _video(store), str(uuid4())).status_code == 404
    media = _video(store)
    assert _attach(client, media, target).status_code == 201
    assert _attach(client, media, target).status_code == 409
    for _ in range(5):
        assert _attach(client, _video(store), target).status_code == 201
    assert _attach(client, _video(store), target).json()["detail"].startswith("This already has six videos")
    assert client.get(URL, params={"source": "external", "target_id": target}).status_code == 401


def test_coach_sees_videos_only_with_injury_sharing_and_a_live_connection(logged):
    client, store, connection, target = logged
    created = _attach(client, _video(store), target).json()
    path = f"{BASE}/training/attachments"
    params = {"source": "external", "target_id": target}
    assert [row["id"] for row in client.get(path, headers=AS_COACH, params=params).json()] == [created["id"]]

    connection["share_injuries"] = False
    assert client.get(path, headers=AS_COACH, params=params).json() == []
    connection["share_injuries"] = True
    connection["status"] = "revoked"
    assert client.get(path, headers=AS_COACH, params=params).status_code == 404
