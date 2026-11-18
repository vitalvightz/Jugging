"""Progress photos (the athlete's own, on their profile) and the coach's
media panel on a fighter: consent-gated, and only ever records that exist."""
from tests.test_coach_fighter_access import AS_COACH, BASE, COACH, FIGHTER
from tests.test_photo_voice_attachments import AS_FIGHTER, URL, _upload
from tests.test_photo_voice_attachments import media  # noqa: F401 - shared pytest fixture
from tests.test_coach_fighter_access import camp  # noqa: F401 - shared pytest fixture

MEDIA = f"{BASE}/media"


def _progress(client, store, asset_id, target=FIGHTER):
    return client.post(URL, headers=AS_FIGHTER, json={"asset_id": asset_id, "source": "progress", "target_id": target})


def test_athlete_adds_progress_photos_to_their_own_profile_only(media):  # noqa: F811
    client, store, *_ = media
    photo, _ = _upload(client, AS_FIGHTER)
    assert _progress(client, store, photo["asset_id"], target=COACH).status_code == 404
    created = _progress(client, store, photo["asset_id"])
    assert created.status_code == 201, created.text
    listed = client.get(URL, headers=AS_FIGHTER, params={"source": "progress", "target_id": FIGHTER})
    assert [row["id"] for row in listed.json()] == [created.json()["id"]]
    # Asking for another person's progress returns nothing, never an error that confirms they exist.
    assert client.get(URL, headers=AS_FIGHTER, params={"source": "progress", "target_id": COACH}).json() == []
    voice, _ = _upload(client, AS_FIGHTER, kind="voice")
    assert _progress(client, store, voice["asset_id"]).status_code == 404


def test_coach_sees_progress_photos_and_recent_files_only_while_media_is_shared(media):  # noqa: F811
    client, store, connection, session, *_ = media
    photo, _ = _upload(client, AS_FIGHTER)
    _progress(client, store, photo["asset_id"])
    on_session, _ = _upload(client, AS_FIGHTER)
    client.post(URL, headers=AS_FIGHTER, json={"asset_id": on_session["asset_id"], "source": "external",
                                               "target_id": session})
    shown = client.get(MEDIA, headers=AS_COACH)
    assert shown.status_code == 200 and "no-store" in shown.headers["cache-control"]
    body = shown.json()
    assert body["share_media"] is True and body["progress_count"] == 1
    assert [(t["source"], t["target_id"]) for t in body["recent"]] == [("external", session)]
    progress = client.get(f"{BASE}/training/attachments", headers=AS_COACH,
                          params={"source": "progress", "target_id": FIGHTER})
    assert len(progress.json()) == 1

    connection["share_media"] = False
    assert client.get(MEDIA, headers=AS_COACH).json() == {"share_media": False, "progress_count": 0, "recent": []}
    assert client.get(f"{BASE}/training/attachments", headers=AS_COACH,
                      params={"source": "progress", "target_id": FIGHTER}).json() == []


def test_coach_cannot_post_to_progress(media):  # noqa: F811
    client, *_ = media
    voice, _ = _upload(client, AS_COACH, kind="voice")
    response = client.post(f"{BASE}/training/attachments", headers=AS_COACH,
                           json={"asset_id": voice["asset_id"], "source": "progress", "target_id": FIGHTER})
    assert response.status_code == 422
