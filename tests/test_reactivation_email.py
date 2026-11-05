"""The reminder stays disabled unless opted in and explicitly switched on."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import httpx

from api.services import reactivation_email


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("UNLXCK_REACTIVATION_EMAIL_ENABLED", raising=False)
    store = MagicMock()
    assert reactivation_email.run_reactivation_email_sweep(store) == 0
    store.client.rpc.assert_not_called()


def test_unsubscribe_token_is_scoped_and_tamper_proof(monkeypatch):
    monkeypatch.setenv("UNLXCK_REACTIVATION_UNSUBSCRIBE_SECRET", "test-secret")
    profile_id = str(uuid4())
    token = reactivation_email.unsubscribe_token(profile_id)
    assert reactivation_email.verify_unsubscribe_token(token) == profile_id
    assert reactivation_email.verify_unsubscribe_token(token[:-1] + ("0" if token[-1] != "0" else "1")) is None
    assert reactivation_email.verify_unsubscribe_token("invalid") is None


def test_opted_in_candidate_uses_resend_with_unsubscribe(monkeypatch):
    profile_id = str(uuid4())
    monkeypatch.setenv("RESEND_API_KEY", "fake-resend-key")
    monkeypatch.setenv("PLAN_READY_FROM_EMAIL", "UNLXCK <notifications@unlxck.test>")
    monkeypatch.setenv("UNLXCK_REACTIVATION_EMAIL_ENABLED", "1")
    monkeypatch.setenv("UNLXCK_REACTIVATION_UNSUBSCRIBE_SECRET", "test-secret")
    monkeypatch.setenv("UNLXCK_REACTIVATION_UNSUBSCRIBE_URL", "https://api.unlxck.test/api/reactivation/unsubscribe")

    store = MagicMock()
    store.client.rpc.return_value.execute.return_value.data = [{
        "profile_id": profile_id, "attempts": 1,
        "email": "fighter@example.test", "first_name": "Sam",
    }]
    store.client.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {"enabled": True}
    store.client.table.return_value.select.return_value.eq.return_value.gte.return_value.limit.return_value.execute.return_value.data = []
    monkeypatch.setattr(reactivation_email.time, "sleep", lambda _: None)
    observed = {}

    def fake_post(url, **kwargs):
        observed.update(url=url, **kwargs)
        return httpx.Response(200, json={"id": "test-message"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(reactivation_email.httpx, "post", fake_post)
    assert reactivation_email.run_reactivation_email_sweep(store) == 1
    assert observed["url"] == "https://api.resend.com/emails"
    assert observed["json"]["to"] == ["fighter@example.test"]
    assert "Unsubscribe" in observed["json"]["html"]
    assert "https://app.unlxck.com/dashboard" in observed["json"]["text"]
    assert observed["json"]["subject"] == "Your progress doesn't stop here"
    assert "GET BACK TO TRAINING" in observed["json"]["html"]
    assert "TRAINING / CONTINUE" in observed["json"]["html"]
    assert "background-color:#0a0a0b" in observed["json"]["html"]
    assert "{{" not in observed["json"]["html"]
    assert observed["headers"]["Idempotency-Key"] == f"reactivation-{profile_id}"


def test_never_sends_when_consent_withdrawn(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "fake-key")
    monkeypatch.setenv("PLAN_READY_FROM_EMAIL", "UNLXCK <notifications@unlxck.test>")
    monkeypatch.setenv("UNLXCK_REACTIVATION_EMAIL_ENABLED", "1")
    monkeypatch.setenv("UNLXCK_REACTIVATION_UNSUBSCRIBE_SECRET", "test-secret")
    monkeypatch.setenv("UNLXCK_REACTIVATION_UNSUBSCRIBE_URL", "https://api.unlxck.test/api/reactivation/unsubscribe")
    store = MagicMock()
    store.client.rpc.return_value.execute.return_value.data = [{
        "profile_id": str(uuid4()), "attempts": 1, "email": "a@example.test",
    }]
    store.client.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {"enabled": False}
    monkeypatch.setattr(reactivation_email.httpx, "post", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("sent")))
    assert reactivation_email.run_reactivation_email_sweep(store) == 0


import pytest
from types import SimpleNamespace
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from api.routes.reactivation import build_reactivation_router


@pytest.mark.parametrize("response,expected", [(429, "failed"), (500, "failed"), (400, "skipped"), (401, "skipped")])
def test_resend_failure_classification(monkeypatch, response, expected):
    monkeypatch.setenv("RESEND_API_KEY", "fake")
    monkeypatch.setattr(reactivation_email.httpx, "post", lambda url, **kw: httpx.Response(
        response, headers={"Retry-After": "120"}, request=httpx.Request("POST", url)))
    status, _, delay = reactivation_email._send(str(uuid4()), {})
    assert status == expected
    if response == 429:
        assert delay == 120


def test_timeout_reuses_identical_key_and_body(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "fake")
    calls = []
    def post(url, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise httpx.ReadTimeout("timeout")
        return httpx.Response(200, json={"id": "one-message"}, request=httpx.Request("POST", url))
    monkeypatch.setattr(reactivation_email.httpx, "post", post)
    profile_id = str(uuid4())
    payload = {"text": "frozen"}
    assert reactivation_email._send(profile_id, payload)[0] == "failed"
    assert reactivation_email._send(profile_id, payload)[0] == "sent"
    assert calls[0] == calls[1]


def client_for(store, *, authenticated=True):
    def profile():
        if not authenticated:
            raise HTTPException(401)
        return SimpleNamespace(athlete_id="00000000-0000-4000-8000-000000000001")
    app = FastAPI()
    app.include_router(build_reactivation_router(require_profile=profile, get_store=lambda: store))
    return TestClient(app)


@pytest.mark.parametrize("birthday,status", [(None,403), ("2015-01-01",403), ("1990-01-01",200)])
def test_preference_adult_only(birthday, status):
    store = MagicMock()
    store.get_profile.return_value = {"role": "athlete", "date_of_birth": birthday}
    response = client_for(store).put("/api/reactivation/preferences", json={"enabled": True})
    assert response.status_code == status
    if status == 403:
        store.client.table.assert_not_called()


def test_preference_requires_authentication():
    store = MagicMock()
    client = client_for(store, authenticated=False)
    assert client.get("/api/reactivation/preferences").status_code == 401
    assert client.put("/api/reactivation/preferences", json={"enabled": True}).status_code == 401
    store.client.table.assert_not_called()


def test_unsubscribe_get_does_not_mutate_and_post_is_repeatable(monkeypatch):
    monkeypatch.setenv("UNLXCK_REACTIVATION_UNSUBSCRIBE_SECRET", "test-secret")
    store = MagicMock()
    client = client_for(store)
    token = reactivation_email.unsubscribe_token(str(uuid4()))
    assert client.get("/api/reactivation/unsubscribe", params={"token": token}).status_code == 200
    store.client.table.assert_not_called()
    for _ in range(2):
        assert client.post("/api/reactivation/unsubscribe", params={"token": token}).status_code == 200
    assert store.client.table.return_value.upsert.call_args.args[0]["enabled"] is False
    assert client.post("/api/reactivation/unsubscribe", params={"token": "bad"}).status_code == 400


def test_webhook_signature_and_delivery(monkeypatch):
    import base64, hashlib, hmac, json, time
    key = b"test-signing-key"
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", "whsec_" + base64.b64encode(key).decode())
    store = MagicMock()
    client = client_for(store)
    body = json.dumps({"type": "email.delivered", "data": {"email_id": "one-message"}})
    timestamp = str(int(time.time()))
    signature = base64.b64encode(hmac.new(key, f"evt.{timestamp}.{body}".encode(), hashlib.sha256).digest()).decode()
    headers = {"svix-id": "evt", "svix-timestamp": timestamp, "svix-signature": "v1," + signature}
    assert client.post("/api/reactivation/webhook", content=body).status_code == 401
    store.client.table.assert_not_called()
    assert client.post("/api/reactivation/webhook", content=body, headers=headers).status_code == 200
    assert "delivered_at" in store.client.table.return_value.update.call_args.args[0]
    headers["svix-timestamp"] = "1"
    assert client.post("/api/reactivation/webhook", content=body, headers=headers).status_code == 401
