"""Today reads issued together, and Today builds shared with XP progress.

Production Today builds made 31-40 strictly sequential Data API calls; the view
is unchanged by fetching the same reads concurrently up front.
"""

from __future__ import annotations

import threading
from collections import Counter

import pytest

from api.services import today_readiness_boundary as boundary
from api.services import today_readiness_boundary_core as core
from api.services.today_command_cache import (
    await_today_command,
    forget_today_command,
    recent_today_command,
    today_command_build,
)
from api.services.today_readiness_boundary import build_today_command_view
from api.services.today_reads import _BuildReadCache
from support import FakeStore, _build_client
from test_today_read_amplification import ATHLETE, NOW, PLAN, _store_with_intake_injury


def _with_open_flags(store):
    for area in ("knee", "lower back"):
        flag = store.create_injury_flag(ATHLETE, {"body_area": area, "description": f"{area} pain",
                                                  "severity": "mild", "status": "open"})
        store.injury_flags[ATHLETE][-1]["episode_id"] = f"episode-{flag['id']}"
    return store


def _view(store):
    return build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)


def test_prefetched_view_is_identical_and_reads_each_input_once(monkeypatch):
    prefetched = _with_open_flags(_store_with_intake_injury())
    sequential = _with_open_flags(_store_with_intake_injury())
    # Same rows, same ids: copy the injury state across.
    sequential.injury_flags = {k: [dict(r) for r in v] for k, v in prefetched.injury_flags.items()}

    expected_store = sequential
    with monkeypatch.context() as patch:
        patch.setattr(boundary, "_prefetch_view_reads", lambda *a, **k: None)
        expected_store.reads.clear()
        expected = _view(expected_store)
        sequential_reads = Counter(expected_store.reads)

    prefetched.reads.clear()
    actual = _view(prefetched)

    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
    # Each open flag's episode history was read by several consumers; now once.
    flags = [f for f in prefetched.list_injury_flags(ATHLETE) if f.get("episode_id")]
    assert prefetched.reads["list_injury_episode_events"] == len(flags)
    assert sequential_reads["list_injury_episode_events"] >= len(flags)
    # A read this path does not use may be fetched in a concurrent wave; it
    # costs one parallel request, never another sequential round trip.


def test_a_failed_prefetch_is_not_kept_and_the_consumer_reads_for_itself():
    store = _store_with_intake_injury()
    calls = {"n": 0}
    real = FakeStore.list_sparring_logs

    def flaky(self, athlete_id, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # the prefetch wave's read
            raise RuntimeError("sparring read down")
        return real(self, athlete_id, **kwargs)

    store.list_sparring_logs = flaky.__get__(store)
    _view(store)

    assert calls["n"] == 2  # the view repeated the read rather than reuse a failure


def test_prefetch_skips_reads_already_held_and_unknown_methods():
    store = _store_with_intake_injury()
    flags_before = len(store.injury_flags.get(ATHLETE, []))
    cache = _BuildReadCache(store)
    cache.get_profile(ATHLETE)
    store.reads.clear()
    cache.prefetch([("get_profile", (ATHLETE,), {}), ("create_injury_flag", (ATHLETE, {}), {})])
    assert store.reads["get_profile"] == 0
    assert len(store.injury_flags.get(ATHLETE, [])) == flags_before  # a write is never issued by prefetch


def test_checkin_save_is_unchanged_by_shared_pre_write_reads(monkeypatch):
    from api.models import TodayCheckinRequest
    payload = TodayCheckinRequest(plan_id=PLAN, sleep="good", body="normal", pain="none", phase="GPP").model_dump()
    fields = ("recommendation_state", "recommendation_reason", "readiness_signal", "training_day", "warnings")

    first = _with_open_flags(_store_with_intake_injury())
    baseline = core._today_service.submit_today_checkin(
        first, athlete_id=ATHLETE, athlete_timezone="UTC", payload=payload, now=NOW)
    second = _with_open_flags(_store_with_intake_injury())
    second.injury_flags = {k: [dict(r) for r in v] for k, v in first.injury_flags.items()}
    second.reads.clear()
    saved = core.submit_today_checkin(second, athlete_id=ATHLETE, athlete_timezone="UTC", payload=payload, now=NOW)

    assert {f: saved.get(f) for f in fields} == {f: baseline.get(f) for f in fields}
    assert second.get_today_checkin(ATHLETE, PLAN, saved["training_day"])["id"] == saved["id"]


def test_xp_waits_for_the_running_today_build_instead_of_building_another():
    store = FakeStore()
    started, release, result = threading.Event(), threading.Event(), {}

    def today_request():
        with today_command_build(store, athlete_id=ATHLETE, athlete_timezone="UTC") as publish:
            started.set()
            release.wait(5)
            publish({"view": "fresh"})

    worker = threading.Thread(target=today_request)
    worker.start()
    assert started.wait(5)
    waiter = threading.Thread(target=lambda: result.update(
        view=await_today_command(store, athlete_id=ATHLETE, athlete_timezone="UTC")))
    waiter.start()
    release.set()
    worker.join(5)
    waiter.join(5)
    assert result["view"] == {"view": "fresh"}


def test_a_write_during_the_build_keeps_that_view_from_anyone():
    store = FakeStore()
    with today_command_build(store, athlete_id=ATHLETE, athlete_timezone="UTC") as publish:
        forget_today_command(store, ATHLETE)  # the athlete saved while Today was building
        publish({"view": "pre-write"})
    assert recent_today_command(store, athlete_id=ATHLETE, athlete_timezone="UTC") is None
    assert await_today_command(store, athlete_id=ATHLETE, athlete_timezone="UTC") is None


def test_another_timezone_never_joins_a_build():
    store = FakeStore()
    with today_command_build(store, athlete_id=ATHLETE, athlete_timezone="UTC") as publish:
        assert await_today_command(store, athlete_id=ATHLETE, athlete_timezone="Europe/London") is None
        publish({"view": "utc"})


@pytest.mark.parametrize("neutral", [True, False])
def test_analytics_events_keep_the_reusable_today_view(neutral):
    client, store, _ = _build_client(enable_in_process_generation=False)
    headers = {"Authorization": "Bearer athlete-token"}
    from api.services.today_command_cache import remember_today_command
    remember_today_command(store, athlete_id="athlete-1", athlete_timezone=None, view={"v": 1})
    if neutral:
        client.post("/api/analytics/events", headers=headers,
                    json={"event_name": "page_viewed", "feature": "today", "request_id": "00000000-0000-4000-8000-000000000001"})
    else:
        client.post("/api/injury-flags", headers=headers, json={"body_area": "knee", "description": "knee pain"})
    kept = recent_today_command(store, athlete_id="athlete-1", athlete_timezone=None) is not None
    assert kept is neutral
