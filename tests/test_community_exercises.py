"""API boundaries complement the real PostgreSQL transition/RLS checks."""

from copy import deepcopy
from unittest.mock import MagicMock
from uuid import uuid4
import pytest
from fastapi import HTTPException
from api.services import community_exercises as service
from api.contracts.private_media import YouTubeClipInput
from api.store import SupabaseAppStore
from tests.support import _build_client

AUTH = {"Authorization": "Bearer athlete-token"}
ADMIN = {"Authorization": "Bearer admin-token"}
BASE = "/api/community-exercises"


def row():
    identity = str(uuid4())
    return {
        "id": str(uuid4()),
        "revision_id": identity,
        "name": "Community drill",
        "equipment": "Bag",
        "notes": "Hands up",
        "media_id": None,
        "status": "private",
        "created_at": "2026-10-11T20:00:00Z",
        "published": False,
        "verified": False,
        "confirmed": False,
        "contributor": None,
        "raw_media": None,
    }


@pytest.fixture
def api():
    client, store, _ = _build_client()
    record = row()
    calls = []

    def rpc(name, params):
        calls.append((name, deepcopy(params)))
        if name == "community_list":
            return {"entries": [record], "total": 1, "next_offset": None}
        if (
            name == "community_command"
            or name == "community_owned_revision"
            or name == "community_dismiss_report"
        ):
            return record["revision_id"]
        return deepcopy(record)

    store.community_rpc = rpc
    return client, store, record, calls


@pytest.mark.parametrize("path", ["", "/mine", "/review", "/history", "/revisions/00000000-0000-0000-0000-00000000a03c"])
def test_community_requires_authentication(api, path):
    client, _, _, calls = api
    assert client.get(BASE + path).status_code == 401
    assert not calls


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("get", "/review", None),
        ("get", "/history", None),
        ("get", "/review/00000000-0000-0000-0000-00000000a03d", None),
        ("put", "/review/00000000-0000-0000-0000-00000000a03e", {"name": "Edited"}),
        (
            "post",
            "/review/00000000-0000-0000-0000-00000000a03f",
            {"action": "approve", "final_confirmation": True},
        ),
    ],
)
def test_non_admin_cannot_review_or_publish(api, method, path, payload):
    client, _, _, calls = api
    kwargs = {"headers": AUTH}
    if payload is not None:
        kwargs["json"] = payload
    assert getattr(client, method)(BASE + path, **kwargs).status_code == 403
    assert not calls


def test_draft_identity_and_actor_are_server_owned(api):
    client, _, record, calls = api
    assert (
        client.post(
            BASE + "/drafts",
            headers=AUTH,
            json={"name": "Mine", "creator_id": "forged"},
        ).status_code
        == 422
    )
    response = client.post(BASE + "/drafts", headers=AUTH, json={"name": "Mine"})
    assert response.status_code == 201
    assert response.json()["prescription_allowed"] is False
    assert calls[0][1]["p_actor_id"] != "forged"
    assert "creator_id" not in calls[0][1]["p_payload"]
    assert "raw_media" not in response.json()
    assert response.json()["revision_id"] == record["revision_id"]


def test_consent_cannot_be_omitted_or_forged(api):
    client, _, record, calls = api
    url = f"{BASE}/revisions/{record['revision_id']}/submit"
    for payload in (
        {},
        {"share_rights": True},
        {"share_rights": False, "attribution_consent": True},
        {"share_rights": True, "attribution_consent": True, "username": "forged"},
    ):
        assert client.post(url, headers=AUTH, json=payload).status_code == 422
    assert not calls
    assert (
        client.post(
            url, headers=AUTH, json={"share_rights": True, "attribution_consent": True}
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "action",
    ["approve", "confirm", "reject", "request_changes", "remove", "dismiss_report"],
)
def test_review_requires_explicit_confirmation_or_reason(api, action):
    client, _, record, calls = api
    assert (
        client.post(
            f"{BASE}/review/{record['revision_id']}",
            headers=ADMIN,
            json={"action": action},
        ).status_code
        == 422
    )
    assert not calls


def test_uploaded_preview_uses_sanitized_short_lived_url_only(api):
    client, store, record, _ = api
    asset = {
        "status": "verified",
        "rendition_path": "owner/asset/demo.mp4",
        "sha256": "proof",
        "processor_version": "isolated-v1",
    }
    media = {
        "id": str(uuid4()),
        "provider": "uploaded",
        "asset_id": str(uuid4()),
        "start_s": 0,
        "end_s": 12,
        "asset": asset,
    }
    record.update(media_id=media["id"], raw_media=media)
    store.private_video_signed_url = MagicMock(
        return_value="https://storage.example/signed"
    )
    response = client.get(f"{BASE}/review/{record['revision_id']}", headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["media"]["url"] == "https://storage.example/signed"
    store.private_video_signed_url.assert_called_once_with(
        asset["rendition_path"], expires_in=60
    )
    assert "raw_media" not in response.json()
    assert "rendition_path" not in str(response.json())
    asset["status"] = "processing"
    assert service.present(store, record, preview=True).media is None
    with pytest.raises(HTTPException):
        service.verify_media(record)


def test_review_youtube_reuses_server_validation_and_never_trusts_verification_from_client(
    api, monkeypatch
):
    client, _, record, calls = api
    raw = {
        "id": str(uuid4()),
        "provider": "youtube",
        "video_id": "abcdefghijk",
        "start_s": 10,
        "end_s": 25,
    }
    record.update(media_id=raw["id"], raw_media=raw, status="in_review")
    checks = []

    def validate(body):
        assert isinstance(body, YouTubeClipInput)
        checks.append(body)
        return {"video_id": "abcdefghijk"}

    monkeypatch.setattr(service, "validate_clip", validate)
    url = f"{BASE}/review/{record['revision_id']}"
    assert (
        client.post(
            url, headers=ADMIN, json={"action": "verify", "youtube_verified": True}
        ).status_code
        == 422
    )
    assert client.post(url, headers=ADMIN, json={"action": "verify"}).status_code == 200
    assert checks[0].start_s == 10 and checks[0].end_s == 25
    payload = next(p["p_payload"] for name, p in calls if name == "community_command")
    assert (
        payload["verified_media_id"] == raw["id"]
        and payload["youtube_verified"] is True
    )


def test_present_sanitizes_profile_image_and_uses_only_live_profile_values(api):
    _, store, record, _ = api
    record["contributor"] = {"username": "actual", "avatar_url": "javascript:alert(1)"}
    assert service.present(store, record).contributor.avatar_url is None
    record["contributor"]["avatar_url"] = "https://profile.example/image.png"
    assert service.present(store, record).contributor.username == "actual"


def test_community_store_carries_authenticated_actor_and_rejects_unknown_operations():
    client = MagicMock()
    client.rpc.return_value.execute.return_value.data = []
    store = SupabaseAppStore(client=client, admin_emails=set())
    store.community_rpc("community_list", {"p_actor_id": "actor", "p_scope": "public"})
    client.rpc.assert_called_once_with(
        "community_list", {"p_actor_id": "actor", "p_scope": "public"}
    )
    with pytest.raises(ValueError):
        store.community_rpc("finish_private_video", {})


def test_community_plan_reference_is_separate_and_withdrawal_preserves_saved_snapshot(
    api,
):
    client, store, record, _ = api
    record.update(status="published", published=True)
    body = {
        "name": "My week",
        "sessions": [
            {
                "id": str(uuid4()),
                "name": "Practice",
                "weekday": "Monday",
                "exercises": [
                    {
                        "id": str(uuid4()),
                        "origin": "manual",
                        "name": record["name"],
                        "community_revision_id": record["revision_id"],
                        "dose": {"mode": "reps", "sets": 3, "reps": 5},
                    }
                ],
            }
        ],
    }
    response = client.post("/api/custom-plans", headers=AUTH, json=body)
    assert response.status_code == 201, response.text
    saved = response.json()
    exercise = saved["sessions"][0]["exercises"][0]
    assert exercise["community_revision_id"] == record["revision_id"]
    assert exercise["media_id"] is None and "catalogue_id" not in exercise
    record.update(published=False)
    assert client.post("/api/custom-plans", headers=AUTH, json=body).status_code == 409
    assert (
        client.put(
            "/api/custom-plans/" + saved["id"], headers=AUTH, json=body
        ).status_code
        == 200
    )
    # Older clients that omit the new identity keep their saved revision.
    del body["sessions"][0]["exercises"][0]["community_revision_id"]
    result = client.put("/api/custom-plans/" + saved["id"], headers=AUTH, json=body)
    assert result.status_code == 200
    assert (
        result.json()["sessions"][0]["exercises"][0]["community_revision_id"]
        == record["revision_id"]
    )
    assert not store.plans


def test_report_dismissal_is_admin_only_and_requires_report_and_reason(api):
    client, _, record, calls = api
    path = f"{BASE}/review/{record['revision_id']}"
    report = str(uuid4())
    body = {"action": "dismiss_report", "reason": "False report", "report_id": report}
    assert client.post(path, headers=AUTH, json=body).status_code == 403
    assert (
        client.post(
            path, headers=ADMIN, json={"action": "dismiss_report", "reason": "Reason"}
        ).status_code
        == 422
    )
    assert (
        client.post(path, headers=ADMIN, json={**body, "reason": "  "}).status_code
        == 422
    )
    assert not calls
    assert client.post(path, headers=ADMIN, json=body).status_code == 200
    assert calls[0][0] == "community_dismiss_report"
    assert calls[0][1]["p_report_id"] == report
    assert calls[0][1]["p_reason"] == "False report"

def test_admin_history_is_separate_from_active_queue(api):
    client, _, _, calls = api
    response = client.get(BASE + "/history", headers=ADMIN)
    assert response.status_code == 200
    assert calls[-1][0] == "community_list"
    assert calls[-1][1]["p_scope"] == "history"


def test_rejection_sends_reason_to_authoritative_transition(api):
    client, _, record, calls = api
    response = client.post(
        f"{BASE}/review/{record['revision_id']}",
        headers=ADMIN,
        json={"action": "reject", "reason": "Incorrect demonstration"},
    )
    assert response.status_code == 200
    assert calls[-1][0] == "community_detail"
    command_calls = [data for name, data in calls if name == "community_command"]
    assert len(command_calls) == 1
    assert command_calls[0]["p_action"] == "reject"
    assert command_calls[0]["p_payload"]["reason"] == "Incorrect demonstration"
