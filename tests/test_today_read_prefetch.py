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


def test_a_save_between_the_validity_check_and_the_insert_is_never_overwritten(monkeypatch):
    """The save lands at the last moment before the view is stored.

    Validity and insertion must be one step: a check-in committing after the
    check but before the insert would otherwise leave the pre-save view for
    XP to reuse for the whole TTL.
    """
    from api.services import today_command_cache as cache

    store = FakeStore()
    real_registry = cache._registry
    armed = {"on": False}

    def registry_with_save(target):
        if armed["on"]:
            armed["on"] = False
            forget_today_command(target, ATHLETE)  # the check-in commits now
        return real_registry(target)

    monkeypatch.setattr(cache, "_registry", registry_with_save)
    with today_command_build(store, athlete_id=ATHLETE, athlete_timezone="UTC") as publish:
        armed["on"] = True
        publish({"view": "pre-save"})

    assert armed["on"] is False  # the save really happened inside publish
    assert recent_today_command(store, athlete_id=ATHLETE, athlete_timezone="UTC") is None
    assert await_today_command(store, athlete_id=ATHLETE, athlete_timezone="UTC") is None


def test_concurrent_saves_never_leave_a_pre_save_view_remembered():
    """Saves racing publishes: whenever a save follows a build's start, that build's view is gone."""
    from api.services.today_command_cache import remember_today_command  # noqa: F401 - same module state

    for _ in range(300):
        store = FakeStore()
        go = threading.Barrier(2)

        def save():
            go.wait()
            forget_today_command(store, ATHLETE)

        saver = threading.Thread(target=save)
        saver.start()
        with today_command_build(store, athlete_id=ATHLETE, athlete_timezone="UTC") as publish:
            go.wait()
            publish({"view": "pre-save"})
        saver.join(5)
        # The save began after the build did, so whichever order they took,
        # the pre-save view must not survive.
        assert recent_today_command(store, athlete_id=ATHLETE, athlete_timezone="UTC") is None


def _no_write_store():
    store = _with_open_flags(_store_with_intake_injury())
    for row in store.intakes[ATHLETE]:
        row["intake"] = {"injuries": ""}  # nothing to seed: the sync only reads
    store.reads.clear()
    return store


def test_the_intake_sync_reads_together_once_with_an_identical_view(monkeypatch):
    store = _no_write_store()
    sequential_store = _no_write_store()
    # Same rows, same ids and timestamps.
    sequential_store.injury_flags = {k: [dict(r) for r in v] for k, v in store.injury_flags.items()}
    with monkeypatch.context() as patch:
        patch.setattr(boundary, "_prefetch_sync_reads", lambda *a, **k: None)
        expected = _view(sequential_store)

    main_thread_reads: list[str] = []
    all_reads: Counter = Counter()
    for name in ("list_injury_flags", "get_intake"):
        # The class method, not the counting wrapper, so each read counts once.
        original = getattr(FakeStore, name).__get__(store)

        def tracked(*args, _name=name, _original=original, **kwargs):
            all_reads[_name] += 1
            if threading.current_thread() is threading.main_thread():
                main_thread_reads.append(_name)
            return _original(*args, **kwargs)

        store.__dict__[name] = tracked
    actual = _view(store)

    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
    # The sync's flag and intake reads left the sequential path entirely…
    assert main_thread_reads == []
    # …and each was still read exactly once.
    assert all_reads["get_intake"] == 1
    assert all_reads["list_injury_flags"] == 2  # open, and open+resolved


def test_a_sync_write_still_reads_the_flags_fresh_afterwards():
    store = _with_open_flags(_store_with_intake_injury())  # intake injury: the sync seeds
    writes = {"n": 0}
    original = store.adopt_or_create_intake_injury_flag

    def adopt(fields):
        writes["n"] += 1
        return original(fields)

    store.adopt_or_create_intake_injury_flag = adopt
    store.reads.clear()
    view = _view(store)

    assert writes["n"] >= 1
    # Prefetched flags predate the write: the sync's final open-flag read and
    # the view's open+resolved read both go back to the store, as before.
    assert store.reads["list_injury_flags"] == 4
    assert [injury["body_area"] for injury in view.open_injuries]


# --- Readiness decision reads -------------------------------------------------
# With a check-in today the view re-evaluates the readiness decision live, and
# that made four reads one after another (head-impact sparring day, recent
# check-ins, both training-history sources): ~4 sequential round trips inside
# today_core in production. They now ride the second prefetch wave.

READINESS_READS = ("latest_sparring_log_day", "list_today_checkins", "list_session_completions",
                   "list_external_training_sessions")


def _camp_store(*, checked_in: bool):
    from test_today_read_amplification import _store_with_camp

    store = _store_with_camp()
    if checked_in:
        store.upsert_today_checkin(ATHLETE, {"plan_id": PLAN, "training_day": "2026-06-03",
                                             "sleep": "good", "body": "fresh", "pain": "none"})
    store.reads.clear()
    return store


def _track_reads(store, names=READINESS_READS):
    main_thread: list[str] = []
    every: Counter = Counter()
    for name in names:
        original = getattr(FakeStore, name).__get__(store)

        def tracked(*args, _name=name, _original=original, **kwargs):
            every[_name] += 1
            if threading.current_thread() is threading.main_thread():
                main_thread.append(_name)
            return _original(*args, **kwargs)

        store.__dict__[name] = tracked
    return main_thread, every


def test_a_checked_in_build_reads_the_decision_inputs_together_once_with_an_identical_view(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(boundary, "_readiness_decision_reads", lambda athlete_id: [])
        expected = _view(_camp_store(checked_in=True))

    store = _camp_store(checked_in=True)
    main_thread, every = _track_reads(store)
    actual = _view(store)

    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
    assert actual.today.recommendation_state != "not_checked_in"  # the decision path really ran
    assert main_thread == []  # none of them is a sequential round trip any more
    assert every["latest_sparring_log_day"] == 1


def test_without_a_check_in_or_open_injury_nothing_extra_is_read():
    store = _camp_store(checked_in=False)
    main_thread, every = _track_reads(store)
    view = _view(store)

    assert view.today.recommendation_state == "not_checked_in"
    assert every["latest_sparring_log_day"] == 0
    assert every["list_external_training_sessions"] == 0


def test_a_failed_prefetched_decision_read_is_read_again_by_the_view():
    expected = _view(_camp_store(checked_in=True))
    store = _camp_store(checked_in=True)
    calls = {"n": 0}
    real = FakeStore.latest_sparring_log_day

    def flaky(self, athlete_id, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # the prefetch wave's read
            raise RuntimeError("sparring read down")
        return real(self, athlete_id, **kwargs)

    store.latest_sparring_log_day = flaky.__get__(store)
    actual = _view(store)

    assert calls["n"] == 2  # the failure was not kept; the view read for itself
    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")


def test_a_decision_read_that_keeps_failing_still_fails_safe(monkeypatch):
    def failing_store():
        store = _camp_store(checked_in=True)
        store.latest_sparring_log_day = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sparring read down"))
        return store

    with monkeypatch.context() as patch:
        patch.setattr(boundary, "_readiness_decision_reads", lambda athlete_id: [])
        without_prefetch = _view(failing_store())
    healthy = _view(_camp_store(checked_in=True))

    actual = _view(failing_store())

    # Exactly as without the prefetch: the missing head-impact history is
    # recorded, never read as "no head impact".
    assert actual.model_dump(mode="json") == without_prefetch.model_dump(mode="json")
    assert actual.model_dump(mode="json") != healthy.model_dump(mode="json")
