"""Photos and voice notes: switched on per type, rebuilt before use, owner and consent checked on every read."""
from uuid import uuid4

import pytest

from tests.test_coach_fighter_access import AS_COACH, BASE, COACH, FIGHTER
from tests.test_coach_fighter_access import camp  # noqa: F401 - shared pytest fixture

AS_FIGHTER = {"Authorization": "Bearer fighter-token"}
AS_STRANGER = {"Authorization": "Bearer stranger-token"}
UPLOADS = "/api/attachments/uploads"
URL = "/api/training/attachments"
SIGNED = "https://signed.example/"


@pytest.fixture
def media(camp, monkeypatch):  # noqa: F811 - pytest fixture injection
    client, store, connection = camp
    connection["share_media"] = True
    monkeypatch.setenv("ATTACHMENT_PHOTOS_ENABLED", "true")
    monkeypatch.setenv("ATTACHMENT_VOICE_ENABLED", "true")
    signed: list[str] = []
    processed: list[str] = []

    def sign(path, *, expires_in, download=None):
        signed.append(path)
        return f"{SIGNED}{path}?ttl={expires_in}"

    def process(asset_id, page=None, assemble=False):
        # Stands in for the Edge Function: a "bad" upload is rejected, others rebuilt.
        processed.append(asset_id)
        asset = store.attachment_assets[asset_id]
        if asset["size_bytes"] == 666:
            asset.update(status="rejected", reject_reason="Not a JPEG")
            return {"status": "rejected", "reason": "Not a JPEG"}
        ext = "jpg" if asset["kind"] == "photo" else "wav"
        asset.update(status="ready", ready_path=f"{asset['owner_id']}/{asset_id}/clean.{ext}", width=800, height=600,
                     duration_ms=None if asset["kind"] == "photo" else 12000)
        return {"status": "ready"}
    store.attachment_signed_url = sign
    store.process_attachment = process
    session = store.create_external_training_session(FIGHTER, {
        "training_day": "2026-11-15", "activity": "boxing", "duration_minutes": 40, "effort": "hard",
        "client_request_id": str(uuid4()), "request_fingerprint": "b" * 64})
    return client, store, connection, session["id"], signed, processed


def _upload(client, headers, kind="photo", size=5000):
    reserved = client.post(UPLOADS, headers=headers, json={"kind": kind, "size_bytes": size})
    assert reserved.status_code == 201, reserved.text
    body = reserved.json()
    done = client.post(f"{UPLOADS}/{body['asset_id']}/process", headers=headers)
    assert done.status_code == 200, done.text
    return body, done.json()


def test_athlete_uploads_a_photo_that_is_rebuilt_attached_and_played_by_link_only(media):
    client, store, _, target, signed, _ = media
    permission, result = _upload(client, AS_FIGHTER)
    assert permission["bucket"] == "attachment-intake" and permission["content_type"] == "image/jpeg"
    assert permission["path"] == f"{FIGHTER}/{permission['asset_id']}/source.jpg"
    assert result["status"] == "ready"
    created = client.post(URL, headers=AS_FIGHTER, json={"asset_id": permission["asset_id"], "source": "external",
                                                         "target_id": target})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["kind"] == "photo" and body["author"] == "athlete" and body["removable"] is True
    assert body["file"]["url"] is None and body["file"]["width"] == 800
    listed = client.get(URL, headers=AS_FIGHTER, params={"source": "external", "target_id": target}).json()
    assert [row["file"]["url"] for row in listed] == [None] and signed == []
    played = client.get(f"{URL}/{body['id']}/play", headers=AS_FIGHTER)
    assert played.status_code == 200 and "no-store" in played.headers["cache-control"]
    # Only the rebuilt file is ever signed, never the quarantined original.
    assert signed == [f"{FIGHTER}/{permission['asset_id']}/clean.jpg"]
    assert "ttl=60" in played.json()["file"]["url"]


def test_rejected_or_unfinished_uploads_never_attach(media):
    client, store, _, target, _, _ = media
    _, result = _upload(client, AS_FIGHTER, size=666)
    assert result == {"asset_id": result["asset_id"], "status": "rejected", "reason": "Not a JPEG"}
    assert client.post(URL, headers=AS_FIGHTER, json={"asset_id": result["asset_id"], "source": "external",
                                                      "target_id": target}).status_code == 409
    pending = client.post(UPLOADS, headers=AS_FIGHTER, json={"kind": "photo", "size_bytes": 10}).json()
    assert client.post(URL, headers=AS_FIGHTER, json={"asset_id": pending["asset_id"], "source": "external",
                                                      "target_id": target}).status_code == 409
    assert store.training_attachments == []


def test_nobody_else_can_process_attach_or_play_an_upload(media):
    client, store, _, target, signed, processed = media
    mine, _ = _upload(client, AS_FIGHTER)
    # Another account cannot trigger processing of, or attach, someone else's upload.
    assert client.post(f"{UPLOADS}/{mine['asset_id']}/process", headers=AS_STRANGER).status_code == 404
    assert client.post(URL, headers=AS_STRANGER, json={"asset_id": mine["asset_id"], "source": "external",
                                                       "target_id": target}).status_code == 404
    # Nor can the owner attach to a record that isn't theirs.
    assert client.post(URL, headers=AS_FIGHTER, json={"asset_id": mine["asset_id"], "source": "external",
                                                      "target_id": str(uuid4())}).status_code == 404
    created = client.post(URL, headers=AS_FIGHTER, json={"asset_id": mine["asset_id"], "source": "external",
                                                         "target_id": target}).json()
    assert client.get(f"{URL}/{created['id']}/play", headers=AS_STRANGER).status_code == 404
    assert len(processed) == 1 and signed == []


def test_each_type_is_off_unless_switched_on_and_fails_closed(media, monkeypatch):
    client, store, _, target, _, processed = media
    ready, _ = _upload(client, AS_FIGHTER)
    monkeypatch.delenv("ATTACHMENT_PHOTOS_ENABLED")
    assert client.post(UPLOADS, headers=AS_FIGHTER, json={"kind": "photo", "size_bytes": 10}).status_code == 503
    assert client.post(URL, headers=AS_FIGHTER, json={"asset_id": ready["asset_id"], "source": "external",
                                                      "target_id": target}).status_code == 503
    assert client.get("/api/attachments/settings", headers=AS_FIGHTER).json() == {
        "photos_available": False, "voice_available": True, "pdfs_available": False}
    # Voice notes are separate, and capped at 2 MB (about a minute of audio).
    assert client.post(UPLOADS, headers=AS_FIGHTER, json={"kind": "voice", "size_bytes": 3_000_000}).status_code == 422
    # PDFs have their own switch, off here.
    assert client.post(UPLOADS, headers=AS_FIGHTER, json={"kind": "pdf", "size_bytes": 10, "page_count": 1}).status_code == 503
    assert client.post(UPLOADS, headers=AS_FIGHTER, json={"kind": "zip", "size_bytes": 10}).status_code == 422
    assert client.post(UPLOADS, headers=AS_FIGHTER, json={"kind": "photo", "size_bytes": 9 * 1024 * 1024}).status_code == 422
    # The processor being down rejects nothing silently and attaches nothing.
    monkeypatch.setenv("ATTACHMENT_PHOTOS_ENABLED", "true")
    store.process_attachment = type(store).process_attachment.__get__(store)
    pending = client.post(UPLOADS, headers=AS_FIGHTER, json={"kind": "photo", "size_bytes": 10}).json()
    assert client.post(f"{UPLOADS}/{pending['asset_id']}/process", headers=AS_FIGHTER).status_code == 503
    assert store.training_attachments == []


def test_coach_voice_feedback_needs_media_sharing_and_the_coach_removes_only_their_own(media):
    client, store, connection, target, signed, _ = media
    path = f"{BASE}/training/attachments"
    params = {"source": "external", "target_id": target}
    athlete_photo, _ = _upload(client, AS_FIGHTER)
    photo = client.post(URL, headers=AS_FIGHTER, json={"asset_id": athlete_photo["asset_id"], **params}).json()
    # Coaches upload voice notes and PDFs, never photos.
    assert client.post(UPLOADS, headers=AS_COACH, json={"kind": "photo", "size_bytes": 10}).status_code == 403
    voice, _ = _upload(client, AS_COACH, kind="voice")
    assert voice["path"].startswith(f"{COACH}/") and voice["content_type"] == "audio/wav"
    created = client.post(path, headers=AS_COACH, json={"asset_id": voice["asset_id"], **params})
    assert created.status_code == 201, created.text
    note = created.json()
    assert note["kind"] == "voice" and note["author"] == "coach" and note["removable"] is True
    assert note["file"]["duration_ms"] == 12000

    coach_view = {row["id"]: row for row in client.get(path, headers=AS_COACH, params=params).json()}
    assert coach_view[photo["id"]]["removable"] is False and coach_view[note["id"]]["removable"] is True
    athlete_view = {row["id"]: row for row in client.get(URL, headers=AS_FIGHTER, params=params).json()}
    assert athlete_view[note["id"]]["author"] == "coach" and athlete_view[note["id"]]["removable"] is True

    # A coach cannot remove the athlete's photo; removing their own note works.
    assert client.delete(f"{path}/{photo['id']}", headers=AS_COACH).status_code == 204
    assert any(row["id"] == photo["id"] for row in store.training_attachments)

    # Media sharing off: nothing listed, nothing played, nothing attached.
    connection["share_media"] = False
    another, _ = _upload(client, AS_COACH, kind="voice")
    assert client.get(path, headers=AS_COACH, params=params).json() == []
    assert client.get(f"{path}/{note['id']}/play", headers=AS_COACH).status_code == 404
    assert client.post(path, headers=AS_COACH, json={"asset_id": another["asset_id"], **params}).status_code == 404
    connection["share_media"] = True
    played = client.get(f"{path}/{note['id']}/play", headers=AS_COACH)
    assert played.status_code == 200 and "no-store" in played.headers["cache-control"]
    # An unconnected coach gets nothing.
    assert client.get(path, headers=AS_STRANGER, params=params).status_code == 404
    assert client.delete(f"{path}/{note['id']}", headers=AS_COACH).status_code == 204
    assert not any(row["id"] == note["id"] for row in store.training_attachments)
    assert len(signed) == 1
