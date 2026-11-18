"""Form-check videos on an athlete's own logged work, and what a coach sees."""
from uuid import uuid4

import pytest

from tests.test_coach_fighter_access import AS_COACH, BASE, FIGHTER
from tests.test_coach_fighter_access import camp  # noqa: F401 - shared pytest fixture

AS_FIGHTER = {"Authorization": "Bearer fighter-token"}
URL = "/api/training/attachments"
SIGNED = "https://signed.example/"


def _video(store, owner=FIGHTER, status="verified"):
    asset = {"id": str(uuid4()), "athlete_id": owner, "status": status, "rendition_path": f"{owner}/demo.mp4"}
    store.private_video_assets[asset["id"]] = asset
    return store.create_private_media(owner, {"provider": "uploaded", "asset_id": asset["id"], "start_s": 0,
                                              "end_s": 12, "orientation": "portrait"})["id"]


@pytest.fixture
def logged(camp, monkeypatch):  # noqa: F811 - pytest fixture injection
    client, store, connection = camp
    connection["share_media"] = True
    monkeypatch.setenv("PRIVATE_VIDEO_UPLOADS_ENABLED", "true")
    store.private_media_worker_healthy = lambda: True
    signed: list[str] = []

    def sign(path, *, expires_in):
        signed.append(path)
        return f"{SIGNED}{path}?ttl={expires_in}"
    store.private_video_signed_url = sign
    session = store.create_external_training_session(FIGHTER, {
        "training_day": "2026-11-15", "activity": "strength", "duration_minutes": 40, "effort": "moderate",
        "client_request_id": str(uuid4()), "request_fingerprint": "a" * 64})
    return client, store, connection, session["id"], signed


def _attach(client, media_id, target, **extra):
    return client.post(URL, headers=AS_FIGHTER, json={"media_id": media_id, "source": "external",
                                                      "target_id": target, **extra})


def test_athlete_attaches_lists_plays_and_removes_their_own_video(logged):
    client, store, _, target, signed = logged
    created = _attach(client, _video(store), target)
    assert created.status_code == 201, created.text
    body = created.json()

    listed = client.get(URL, headers=AS_FIGHTER, params={"source": "external", "target_id": target})
    assert [row["id"] for row in listed.json()] == [body["id"]]
    # Neither attaching nor listing hands out a playable link.
    assert body["media"]["url"] is None and listed.json()[0]["media"]["url"] is None
    assert signed == []
    # A different exercise on the same record is a separate list.
    assert client.get(URL, headers=AS_FIGHTER, params={"source": "external", "target_id": target,
                                                       "exercise_ref": "squat"}).json() == []

    played = client.get(f"{URL}/{body['id']}/play", headers=AS_FIGHTER)
    assert played.status_code == 200
    assert played.json()["media"]["url"].startswith(SIGNED) and "ttl=60" in played.json()["media"]["url"]
    assert "no-store" in played.headers["cache-control"]
    assert len(signed) == 1

    assert client.delete(f"{URL}/{body['id']}", headers=AS_FIGHTER).status_code == 204
    assert client.delete(f"{URL}/{body['id']}", headers=AS_FIGHTER).status_code == 204
    assert client.get(f"{URL}/{body['id']}/play", headers=AS_FIGHTER).status_code == 404
    assert client.get(URL, headers=AS_FIGHTER, params={"source": "external", "target_id": target}).json() == []


def test_only_own_verified_videos_on_own_records(logged):
    client, store, _, target, _ = logged
    assert _attach(client, _video(store, owner=str(uuid4())), target).status_code == 404
    assert _attach(client, _video(store, status="processing"), target).status_code == 409
    assert _attach(client, _video(store), str(uuid4())).status_code == 404
    media = _video(store)
    assert _attach(client, media, target).status_code == 201
    assert _attach(client, media, target).status_code == 409
    for _ in range(5):
        assert _attach(client, _video(store), target).status_code == 201
    assert _attach(client, _video(store), target).json()["detail"].startswith("This already has six")
    assert client.get(URL, params={"source": "external", "target_id": target}).status_code == 401


def test_another_athletes_video_cannot_be_played(logged):
    client, store, _, target, signed = logged
    created = _attach(client, _video(store), target).json()
    # The same attachment, asked for by its id under someone else's account.
    row = store.training_attachments[0]
    row["athlete_id"] = str(uuid4())
    assert client.get(f"{URL}/{created['id']}/play", headers=AS_FIGHTER).status_code == 404
    assert signed == []


def test_attaching_fails_closed_when_uploads_are_switched_off(logged, monkeypatch):
    client, store, _, target, _ = logged
    media = _video(store)
    monkeypatch.delenv("PRIVATE_VIDEO_UPLOADS_ENABLED")
    assert _attach(client, media, target).status_code == 503
    monkeypatch.setenv("PRIVATE_VIDEO_UPLOADS_ENABLED", "true")
    store.private_media_worker_healthy = lambda: False
    assert _attach(client, media, target).status_code == 503
    assert store.training_attachments == []


def test_coach_plays_only_with_media_sharing_and_a_live_connection(logged):
    client, store, connection, target, signed = logged
    created = _attach(client, _video(store), target).json()
    path = f"{BASE}/training/attachments"
    params = {"source": "external", "target_id": target}
    listed = client.get(path, headers=AS_COACH, params=params).json()
    assert [row["id"] for row in listed] == [created["id"]] and listed[0]["media"]["url"] is None
    played = client.get(f"{path}/{created['id']}/play", headers=AS_COACH)
    assert played.status_code == 200 and played.json()["media"]["url"].startswith(SIGNED)
    assert "no-store" in played.headers["cache-control"]

    # Consent is read again on every request: turning sharing off stops new links at once.
    connection["share_media"] = False
    assert client.get(path, headers=AS_COACH, params=params).json() == []
    assert client.get(f"{path}/{created['id']}/play", headers=AS_COACH).status_code == 404
    # Injury sharing alone is not media sharing.
    assert connection["share_injuries"] is True
    connection["share_media"] = True
    connection["status"] = "revoked"
    assert client.get(path, headers=AS_COACH, params=params).status_code == 404
    assert client.get(f"{path}/{created['id']}/play", headers=AS_COACH).status_code == 404
    assert len(signed) == 1
