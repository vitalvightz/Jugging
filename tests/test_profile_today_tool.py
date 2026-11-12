"""tools/profile_today.py stays read-only and never prints athlete content."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

import api.store
from test_today_read_amplification import ATHLETE, _store_with_intake_injury

TOOL = Path(__file__).resolve().parents[1] / "tools" / "profile_today.py"
SECRET_NAME = "Sentinel Camp Name"


def _run(monkeypatch, capsys, store, *extra) -> dict:
    import fastapi.dependencies.utils as dependency_utils
    import fastapi.routing as routing
    import httpx

    from api.services import today_reads

    # Everything the tool patches is restored after each test.
    monkeypatch.setattr(httpx.Client, "send", httpx.Client.send)
    monkeypatch.setattr(routing, "run_in_threadpool", routing.run_in_threadpool)
    monkeypatch.setattr(dependency_utils, "run_in_threadpool", dependency_utils.run_in_threadpool)
    monkeypatch.setattr(today_reads._PREFETCH_POOL, "submit", today_reads._PREFETCH_POOL.submit)
    monkeypatch.setattr(api.store.SupabaseAppStore, "from_env", classmethod(lambda cls: store))
    import logging

    monkeypatch.setattr(logging.root.manager, "disable", logging.root.manager.disable)
    args = list(extra) or ["--now", "2026-06-03T15:00:00Z"]
    monkeypatch.setattr(sys, "argv", ["profile_today.py", "--athlete", ATHLETE, "--warm-runs", "1", *args])
    runpy.run_path(str(TOOL), run_name="__main__")
    out = capsys.readouterr().out
    assert SECRET_NAME not in out  # content never leaves the server
    return json.loads(out)


def test_reports_stages_store_calls_and_field_sizes(monkeypatch, capsys):
    store = _store_with_intake_injury()
    next(iter(store.plans.values()))["plan_name"] = SECRET_NAME
    report = _run(monkeypatch, capsys, store)

    assert report["response_bytes"] > 0 and report["response_fields"]
    assert "today_core" in report["warm"]["median_stages"]
    assert report["store_calls_one_warm_build"]
    assert report["cold_and_warm_responses_identical"] is True


def test_writes_are_refused_and_reported(monkeypatch, capsys):
    store = _store_with_intake_injury()  # intake injury: the sync would seed a flag
    flags_before = json.dumps(store.injury_flags, sort_keys=True, default=str)
    report = _run(monkeypatch, capsys, store)

    assert report["refused_writes"].get("adopt_or_create_intake_injury_flag")
    assert json.dumps(store.injury_flags, sort_keys=True, default=str) == flags_before


def test_http_guard_refuses_writes_before_they_are_sent(monkeypatch):
    import httpx

    tool = runpy.run_path(str(TOOL))  # loads the module without running main()
    sent: list[str] = []
    transport = httpx.MockTransport(lambda request: (sent.append(f"{request.method} {request.url.path}"),
                                                     httpx.Response(200, json=[]))[1])
    monkeypatch.setattr(httpx.Client, "send", httpx.Client.send)  # restored after the test
    refused = tool["install_http_read_only_guard"]()
    client = httpx.Client(transport=transport, base_url="https://db.example")

    client.get("/rest/v1/plans?id=eq.1")
    client.post("/rest/v1/rpc/pending_delayed_rehab", json={})
    for method, path in (("POST", "/rest/v1/today_checkins"), ("PATCH", "/rest/v1/plans"),
                         ("DELETE", "/rest/v1/injury_flags"), ("POST", "/rest/v1/rpc/sync_intake_injury_flag")):
        with pytest.raises(RuntimeError, match="read-only"):
            client.request(method, path, json={})

    assert sent == ["GET /rest/v1/plans", "POST /rest/v1/rpc/pending_delayed_rehab"]
    assert refused == {"POST today_checkins": 1, "PATCH plans": 1, "DELETE injury_flags": 1,
                       "POST rpc/sync_intake_injury_flag": 1}


def test_a_real_store_write_is_refused_before_any_request_leaves(monkeypatch):
    import httpx

    monkeypatch.setenv("SUPABASE_URL", "https://db.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-key-for-tests")
    monkeypatch.setattr(httpx.Client, "send", httpx.Client.send)
    tool = runpy.run_path(str(TOOL))
    refused = tool["install_http_read_only_guard"]()
    store = api.store.SupabaseAppStore.from_env()

    with pytest.raises(Exception):
        store.upsert_today_checkin(ATHLETE, {"training_day": "2026-06-03", "readiness": "ready"})
    with pytest.raises(Exception):
        store.adopt_or_create_intake_injury_flag({"athlete_id": ATHLETE, "plan_id": "p", "source_key": "k"})

    assert sum(refused.values()) >= 2
    assert all(label.split()[0] in {"POST", "PATCH", "PUT", "DELETE"} for label in refused)


@pytest.mark.parametrize("target", ["calendar", "history"])
def test_route_targets_serve_through_the_app_read_only(monkeypatch, capsys, target):
    from api.auth import AuthenticatedUser

    store = _store_with_intake_injury()
    store.ensure_profile(AuthenticatedUser(user_id=ATHLETE, email="ari@example.com", full_name="Ari", metadata={}))
    next(iter(store.plans.values()))["plan_name"] = SECRET_NAME
    report = _run(monkeypatch, capsys, store, "--target", target)

    assert report["status_codes"] == [200]
    assert report["refused_writes"] == {} and report["refused_http_requests"] == {}
    assert report["store_call_timeline_one_warm_request"]
    assert report["profile_top_cumulative_repo"]


def test_route_targets_do_not_build_the_production_app_on_import(monkeypatch, capsys):
    """With credentials set, importing api.app builds a second app and runs startup
    validation (an RPC). The profiler must take only create_app from it."""
    from api.auth import AuthenticatedUser

    store = _store_with_intake_injury()
    store.ensure_profile(AuthenticatedUser(user_id=ATHLETE, email="ari@example.com", full_name="Ari", metadata={}))
    built = {"validate_runtime_schema": 0}
    store.validate_runtime_schema = lambda: built.__setitem__("validate_runtime_schema", built["validate_runtime_schema"] + 1)
    monkeypatch.setenv("SUPABASE_URL", "https://db.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-key-for-tests")
    monkeypatch.delitem(sys.modules, "api.app", raising=False)  # import fresh, as on the server; restored after

    report = _run(monkeypatch, capsys, store, "--target", "calendar")

    assert report["status_codes"] == [200]
    assert built["validate_runtime_schema"] == 0  # no production app was built on import
    assert report["refused_http_requests"] == {}
    import os
    assert os.environ["SUPABASE_URL"] == "https://db.invalid"  # credentials restored after the import
