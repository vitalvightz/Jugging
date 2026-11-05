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
    profile_id, plan_id = str(uuid4()), str(uuid4())
    monkeypatch.setenv("RESEND_API_KEY", "fake-resend-key")
    monkeypatch.setenv("PLAN_READY_FROM_EMAIL", "UNLXCK <notifications@unlxck.test>")
    monkeypatch.setenv("UNLXCK_REACTIVATION_EMAIL_ENABLED", "1")
    monkeypatch.setenv("UNLXCK_REACTIVATION_UNSUBSCRIBE_SECRET", "test-secret")
    monkeypatch.setenv("UNLXCK_REACTIVATION_UNSUBSCRIBE_URL", "https://api.unlxck.test/api/reactivation/unsubscribe")

    store = MagicMock()
    store.client.rpc.return_value.execute.return_value.data = [{
        "profile_id": profile_id, "plan_id": plan_id,
        "email": "fighter@example.test", "first_name": "Sam",
    }]
    store.client.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {"enabled": True}
    store.client.table.return_value.select.return_value.eq.return_value.gte.return_value.limit.return_value.execute.return_value.data = []
    observed = {}

    def fake_post(url, **kwargs):
        observed.update(url=url, **kwargs)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(reactivation_email.httpx, "post", fake_post)
    assert reactivation_email.run_reactivation_email_sweep(store) == 1
    assert observed["url"] == "https://api.resend.com/emails"
    assert observed["json"]["to"] == ["fighter@example.test"]
    assert "Unsubscribe" in observed["json"]["html"]
    assert f"/plans/{plan_id}" in observed["json"]["text"]
    assert observed["headers"]["Idempotency-Key"] == f"reactivation-{profile_id}"


def test_never_sends_when_consent_withdrawn(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "fake-key")
    monkeypatch.setenv("PLAN_READY_FROM_EMAIL", "UNLXCK <notifications@unlxck.test>")
    monkeypatch.setenv("UNLXCK_REACTIVATION_EMAIL_ENABLED", "1")
    monkeypatch.setenv("UNLXCK_REACTIVATION_UNSUBSCRIBE_SECRET", "test-secret")
    monkeypatch.setenv("UNLXCK_REACTIVATION_UNSUBSCRIBE_URL", "https://api.unlxck.test/api/reactivation/unsubscribe")
    store = MagicMock()
    store.client.rpc.return_value.execute.return_value.data = [{
        "profile_id": str(uuid4()), "plan_id": str(uuid4()), "email": "a@example.test",
    }]
    store.client.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data = {"enabled": False}
    monkeypatch.setattr(reactivation_email.httpx, "post", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("sent")))
    assert reactivation_email.run_reactivation_email_sweep(store) == 0
