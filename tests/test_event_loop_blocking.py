"""Store reads never run on the event loop, where they stall every request's start.

The API is one process: while a coroutine blocks on a Supabase round trip, no
other request can even begin (Server-Timing ``queue`` read 700–900ms in
production while ``/api/generation-jobs/*`` looked up a plan on the loop).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock

from support import _build_client
from test_reactivation_email import client_for
from test_today_read_amplification import _add_camp, PLAN

H = {"Authorization": "Bearer athlete-token"}
JOB_ID = "33333333-3333-3333-3333-333333333333"


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _watch(store, names):
    on_loop: list[str] = []
    for name in names:
        original = getattr(store, name)

        def watched(*args, _name=name, _original=original, **kwargs):
            if _on_event_loop():
                on_loop.append(_name)
            return _original(*args, **kwargs)

        setattr(store, name, watched)
    return on_loop


def _completed_job(store, athlete: str) -> None:
    store.generation_jobs[JOB_ID] = {
        "id": JOB_ID, "athlete_id": athlete, "client_request_id": "c1", "source": "athlete",
        "status": "completed", "error": None, "intake_id": None, "stage1_result": None, "final_result": None,
        "plan_id": PLAN, "attempt_count": 1, "progress_milestones": [], "created_at": "2026-11-02T00:00:00+00:00",
        "updated_at": "2026-11-02T00:10:00+00:00", "completed_at": "2026-11-02T00:10:00+00:00",
        "started_at": "2026-11-02T00:00:00+00:00", "heartbeat_at": None, "failed_at": None, "claimed_by": None,
        "claimed_at": None, "request_payload": {}, "payload_hash": "h",
    }


def test_generation_job_routes_read_the_store_off_the_event_loop():
    client, store, _ = _build_client()
    me = client.get("/api/me", headers=H).json()
    athlete = (me.get("profile") or {}).get("athlete_id") or me["athlete_id"]
    _add_camp(store, athlete_id=athlete, plan_id=PLAN, weeks=2)
    _completed_job(store, athlete)
    on_loop = _watch(store, [name for name in ("get_plan", "get_plan_status", "get_latest_plan",
                                               "get_latest_generation_job_status", "get_generation_job_status",
                                               "get_visible_active_generation_job_status") if hasattr(store, name)])

    latest = client.get("/api/generation-jobs/latest", headers=H)
    by_id = client.get(f"/api/generation-jobs/{JOB_ID}", headers=H)
    active = client.get("/api/generation-jobs/active", headers=H)

    assert latest.status_code == by_id.status_code == active.status_code == 200
    # The completed job still resolves its plan, exactly as before…
    assert latest.json()["plan_id"] == by_id.json()["plan_id"] == PLAN
    # …but every store read ran on a worker thread.
    assert on_loop == []


def _signed(key: bytes, body: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = base64.b64encode(hmac.new(key, f"evt.{timestamp}.{body}".encode(), hashlib.sha256).digest()).decode()
    return {"svix-id": "evt", "svix-timestamp": timestamp, "svix-signature": "v1," + signature}


def test_delivery_webhook_writes_off_the_event_loop_with_unchanged_responses(monkeypatch):
    key = b"test-signing-key"
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", "whsec_" + base64.b64encode(key).decode())
    store = MagicMock()
    loop_calls: list[bool] = []
    store.client.table.side_effect = lambda *a, **k: (loop_calls.append(_on_event_loop()), store.client.table.return_value)[1]
    client = client_for(store)

    body = json.dumps({"type": "email.delivered", "data": {"email_id": "one-message"}})
    assert client.post("/api/reactivation/webhook", content=body, headers=_signed(key, body)).status_code == 200
    assert "delivered_at" in store.client.table.return_value.update.call_args.args[0]
    assert loop_calls and not any(loop_calls)

    # A malformed event is still a 400, and a send awaiting finalization a 503.
    bad = json.dumps({"type": "email.delivered", "data": {}})
    assert client.post("/api/reactivation/webhook", content=bad, headers=_signed(key, bad)).status_code == 400
    assert client.post("/api/reactivation/webhook", content="{", headers=_signed(key, "{")).status_code == 400
    store.client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = []
    pending = store.client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute
    pending.side_effect = [MagicMock(data=[]), MagicMock(data=[{"profile_id": "p"}])]
    assert client.post("/api/reactivation/webhook", content=body, headers=_signed(key, body)).status_code == 503
