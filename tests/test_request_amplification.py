"""Guards against per-request backend amplification on ordinary app use.

Covers the three read paths that multiplied Supabase round trips on every
screen load: the profile read behind ``require_profile``, activation XP
reconciliation inside ``GET /api/me``, and XP progress rebuilding the Today
command view that ``/api/today`` had just built.
"""

from unittest.mock import MagicMock

from api.auth import AuthenticatedUser
from api.models import ProfileUpdateRequest
from api.routes import xp as xp_routes
from api.services import xp_progress
from api.store import SupabaseAppStore
from support import _build_client, _build_request, finalized_result

ATHLETE_HEADERS = {"Authorization": "Bearer athlete-token"}


def _activated_client():
    client, store, _ = _build_client()
    store.update_profile(
        "athlete-1",
        ProfileUpdateRequest(full_name="Ari Mensah", technical_style=["boxing"]),
    )
    request = _build_request(
        {"athlete": {"full_name": "Ari Mensah", "technical_style": ["boxing"]}}
    )
    intake = store.create_intake("athlete-1", request)
    store.create_plan(
        athlete_id="athlete-1",
        intake_id=intake["id"],
        request=request,
        result=finalized_result(status="ready"),
    )
    # In-memory XP ledger shapes the progress read understands.
    if not isinstance(getattr(store, "xp_awards", None), dict):
        store.xp_awards = {"athlete-1": []}
    if getattr(store, "plan_milestones", None) is None:
        store.plan_milestones = {}
    return client, store


def _recording_award(actions, *, fail_actions=()):
    def award(athlete_id, *, action, idempotency_key, calendar_date=None):
        actions.append(action)
        if action in fail_actions:
            raise RuntimeError(f"{action} unavailable")
        return {"awarded": True, "action": action, "athlete_id": athlete_id}

    return award


def test_get_me_stops_reconciling_activation_xp_once_every_milestone_is_awarded():
    client, store = _activated_client()
    actions = []
    store.award_xp = _recording_award(actions)

    assert client.get("/api/me", headers=ATHLETE_HEADERS).status_code == 200
    assert actions == ["profile_completed", "first_intake_completed", "first_plan_ready"]

    plan_lists = []
    original_list_user_plans = store.list_user_plans

    def counting_list_user_plans(*args, **kwargs):
        plan_lists.append(args)
        return original_list_user_plans(*args, **kwargs)

    store.list_user_plans = counting_list_user_plans
    actions.clear()

    response = client.get("/api/me", headers=ATHLETE_HEADERS)

    assert response.status_code == 200
    assert response.json()["profile"]["athlete_id"] == "athlete-1"
    assert actions == []
    # Only the MeResponse's own plan list remains; the activation lookup is gone.
    assert len(plan_lists) <= 1


def test_get_me_retries_only_the_activation_milestone_that_failed():
    client, store = _activated_client()
    actions = []
    store.award_xp = _recording_award(actions, fail_actions={"profile_completed"})

    assert client.get("/api/me", headers=ATHLETE_HEADERS).status_code == 200
    assert actions == ["profile_completed", "first_intake_completed", "first_plan_ready"]

    actions.clear()
    store.award_xp = _recording_award(actions)
    assert client.get("/api/me", headers=ATHLETE_HEADERS).status_code == 200
    assert actions == ["profile_completed"]

    actions.clear()
    assert client.get("/api/me", headers=ATHLETE_HEADERS).status_code == 200
    assert actions == []


def test_xp_progress_reuses_the_today_view_until_the_athlete_writes(monkeypatch):
    client, _ = _activated_client()
    builds = []
    original_build = xp_progress.build_today_command_view

    def counting_build(*args, **kwargs):
        builds.append(kwargs.get("athlete_id"))
        return original_build(*args, **kwargs)

    monkeypatch.setattr(xp_progress, "build_today_command_view", counting_build)
    monkeypatch.setattr(xp_progress, "get_streak_state", lambda *_a, **_k: {})

    assert client.get("/api/today", headers=ATHLETE_HEADERS).status_code == 200
    assert client.get("/api/xp/progress", headers=ATHLETE_HEADERS).status_code == 200
    assert builds == []

    # Any athlete write drops the reusable view, so XP never reads a pre-write
    # Today state.
    update = client.put(
        "/api/me",
        headers=ATHLETE_HEADERS,
        json={"full_name": "Ari Mensah"},
    )
    assert update.status_code == 200
    assert client.get("/api/xp/progress", headers=ATHLETE_HEADERS).status_code == 200
    assert builds == ["athlete-1"]



def test_login_activity_write_keeps_the_today_view_and_the_profile_cache(monkeypatch):
    """GET /api/today -> POST /api/xp/activity -> GET /api/xp/progress.

    Recording app activity touches neither Today nor the profile, so it must not
    throw away the Today build XP is about to reuse or the cached profile row.
    """

    client, store = _activated_client()
    builds = []
    original_build = xp_progress.build_today_command_view

    def counting_build(*args, **kwargs):
        builds.append(kwargs.get("athlete_id"))
        return original_build(*args, **kwargs)

    monkeypatch.setattr(xp_progress, "build_today_command_view", counting_build)
    monkeypatch.setattr(xp_progress, "get_streak_state", lambda *_a, **_k: {})
    monkeypatch.setattr(xp_routes, "record_daily_activity", lambda *_a, **_k: {})
    monkeypatch.setattr(xp_routes, "claim_daily_login_reward", lambda *_a, **_k: None)
    profile_invalidations = []
    store.invalidate_cached_profile = lambda athlete_id=None: profile_invalidations.append(
        athlete_id
    )

    assert client.get("/api/today", headers=ATHLETE_HEADERS).status_code == 200
    activity = client.post("/api/xp/activity", headers=ATHLETE_HEADERS)
    assert activity.status_code == 200
    assert client.get("/api/xp/progress", headers=ATHLETE_HEADERS).status_code == 200

    assert builds == []
    assert profile_invalidations == []


def test_profile_cache_survives_requests_that_do_not_write_the_profile(monkeypatch):
    store, reads = _supabase_store_with_profile_reads(monkeypatch)

    store.ensure_profile(USER)  # GET /api/me resolves the caller
    store.ensure_profile(USER)  # POST /api/xp/activity resolves the caller
    store.ensure_profile(USER)  # GET /api/xp/progress resolves the caller

    assert reads == ["athlete-1"]


def test_xp_progress_without_a_recent_today_view_builds_its_own(monkeypatch):
    client, _ = _activated_client()
    builds = []
    original_build = xp_progress.build_today_command_view

    def counting_build(*args, **kwargs):
        builds.append(kwargs.get("athlete_id"))
        return original_build(*args, **kwargs)

    monkeypatch.setattr(xp_progress, "build_today_command_view", counting_build)
    monkeypatch.setattr(xp_progress, "get_streak_state", lambda *_a, **_k: {})

    assert client.get("/api/xp/progress", headers=ATHLETE_HEADERS).status_code == 200
    assert builds == ["athlete-1"]


def _supabase_store_with_profile_reads(monkeypatch, ttl: str | None = None):
    if ttl is None:
        monkeypatch.delenv("PROFILE_CACHE_TTL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("PROFILE_CACHE_TTL_SECONDS", ttl)
    store = SupabaseAppStore(client=MagicMock(), admin_emails=set())
    reads = []

    def read_profile(athlete_id):
        reads.append(athlete_id)
        return {"id": athlete_id, "role": "athlete", "full_name": f"read {len(reads)}"}

    store._get_profile_by_id = read_profile
    return store, reads


USER = AuthenticatedUser(
    user_id="athlete-1",
    email="ari@example.com",
    full_name="Ari Mensah",
    metadata={},
)


def test_ensure_profile_serves_a_burst_of_requests_from_one_read(monkeypatch):
    store, reads = _supabase_store_with_profile_reads(monkeypatch)

    first = store.ensure_profile(USER)
    second = store.ensure_profile(USER)

    assert reads == ["athlete-1"]
    assert first == second
    # Callers get their own copy; mutating it cannot poison the cache.
    second["role"] = "admin"
    assert store.ensure_profile(USER)["role"] == "athlete"


def test_profile_writes_through_the_store_invalidate_the_cached_row(monkeypatch):
    store, reads = _supabase_store_with_profile_reads(monkeypatch)

    store.ensure_profile(USER)
    store.clear_onboarding_draft("athlete-1")
    refreshed = store.ensure_profile(USER)

    assert reads == ["athlete-1", "athlete-1"]
    assert refreshed["full_name"] == "read 2"


def test_explicit_invalidation_and_zero_ttl_disable_profile_reuse(monkeypatch):
    store, reads = _supabase_store_with_profile_reads(monkeypatch)
    store.ensure_profile(USER)
    store.invalidate_cached_profile("athlete-1")
    store.ensure_profile(USER)
    assert len(reads) == 2

    uncached, uncached_reads = _supabase_store_with_profile_reads(monkeypatch, ttl="0")
    uncached.ensure_profile(USER)
    uncached.ensure_profile(USER)
    assert len(uncached_reads) == 2


def test_newly_created_profile_is_cached_for_the_following_requests(monkeypatch):
    monkeypatch.delenv("PROFILE_CACHE_TTL_SECONDS", raising=False)
    store = SupabaseAppStore(client=MagicMock(), admin_emails=set())
    created = {"id": "athlete-1", "role": "athlete", "full_name": "Ari Mensah"}
    reads = []

    def read_profile(athlete_id):
        reads.append(athlete_id)
        # First read: no row yet. After the upsert the row exists.
        return None if len(reads) == 1 else dict(created)

    store._get_profile_by_id = read_profile
    store._require_profile = lambda athlete_id: read_profile(athlete_id)
    store._upsert_profile_with_retry = lambda **_kwargs: None

    assert store.ensure_profile(USER)["id"] == "athlete-1"
    assert store.ensure_profile(USER)["id"] == "athlete-1"
    assert len(reads) == 2


def test_fallback_read_after_a_failed_upsert_is_cached(monkeypatch):
    import httpx

    monkeypatch.delenv("PROFILE_CACHE_TTL_SECONDS", raising=False)
    store = SupabaseAppStore(client=MagicMock(), admin_emails=set())
    reads = []

    def read_profile(athlete_id):
        reads.append(athlete_id)
        if len(reads) == 1:
            return None
        return {"id": athlete_id, "role": "athlete"}

    def failing_upsert(**_kwargs):
        raise httpx.ConnectError("upsert raced")

    store._get_profile_by_id = read_profile
    store._upsert_profile_with_retry = failing_upsert

    assert store.ensure_profile(USER)["id"] == "athlete-1"
    assert store.ensure_profile(USER)["id"] == "athlete-1"
    assert len(reads) == 2


def _concurrent_ensure(store, callers: int):
    import threading

    results, errors = [None] * callers, []
    start = threading.Barrier(callers)

    def run(index):
        start.wait()
        try:
            results[index] = store.ensure_profile(USER)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(callers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    return results, errors


def _slow_profile_reads(store, *, fail_first=False, during_read=None):
    import threading
    import time

    reads, lock = [], threading.Lock()

    def read_profile(athlete_id):
        with lock:
            reads.append(athlete_id)
            count = len(reads)
        time.sleep(0.15)  # held open so the burst arrives while it is in flight
        if during_read and count == 1:
            during_read()
        if fail_first and count == 1:
            raise RuntimeError("profile read down")
        return {"id": athlete_id, "role": "athlete", "full_name": f"read {count}"}

    store._get_profile_by_id = read_profile
    store._is_transient_store_error = lambda exc: False
    return reads


def test_a_cold_cache_launch_burst_shares_one_profile_read(monkeypatch):
    store, _ = _supabase_store_with_profile_reads(monkeypatch)
    reads = _slow_profile_reads(store)

    results, errors = _concurrent_ensure(store, 6)

    assert errors == []
    assert reads == ["athlete-1"]
    assert all(result["full_name"] == "read 1" for result in results)
    # Every caller still owns its row.
    results[0]["role"] = "admin"
    assert results[1]["role"] == "athlete"


def test_a_failed_shared_read_is_not_handed_to_waiters(monkeypatch):
    from api.store import _STORE_CLIENT_ERRORS

    store, _ = _supabase_store_with_profile_reads(monkeypatch)
    reads = _slow_profile_reads(store, fail_first=True)
    monkeypatch.setattr("api.store._STORE_CLIENT_ERRORS", (*_STORE_CLIENT_ERRORS, RuntimeError))

    results, errors = _concurrent_ensure(store, 3)

    # The leader's caller sees its failure; each waiter read for itself.
    assert len(errors) == 1
    assert len(reads) == 3
    assert sorted(result["full_name"] for result in results if result) == ["read 2", "read 3"]


def test_a_write_during_the_shared_read_makes_waiters_read_again(monkeypatch):
    store, _ = _supabase_store_with_profile_reads(monkeypatch)
    reads = _slow_profile_reads(store, during_read=lambda: store.invalidate_cached_profile("athlete-1"))

    results, errors = _concurrent_ensure(store, 3)

    assert errors == []
    assert len(reads) >= 2  # waiters never reuse a read that overlapped a write
    waiter_rows = [result for result in results if result["full_name"] != "read 1"]
    assert len(waiter_rows) == 2


def test_zero_ttl_also_switches_off_shared_reads(monkeypatch):
    store, _ = _supabase_store_with_profile_reads(monkeypatch, ttl="0")
    reads = _slow_profile_reads(store)

    _concurrent_ensure(store, 3)

    assert len(reads) == 3
