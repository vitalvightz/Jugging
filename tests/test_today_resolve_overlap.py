"""Today's reads after the active-plan resolution go out in one wave when the sync cannot write.

Production builds made the plan pointer and plan row reads, then the intake
sync's wave, then the view's first wave, then its episode and readiness wave:
five round trips one after another. The reads that decide the later waves (the
latest intake, the sync's flags and the day's check-ins) now run while the plan
resolves, and when the sync provably writes nothing the view's reads join the
sync's wave. A build whose sync may write keeps the earlier order, so no view
row read before a write is ever served after it.
"""

from __future__ import annotations

import copy
import threading
from collections import Counter

import pytest

from api.services import today_readiness_boundary as boundary
from api.services.active_plan import ActivePlanResolution
from api.services.today_reads import _BuildReadCache
from support import FakeStore
from test_today_read_amplification import ATHLETE, NOW, PLAN, _store_with_camp, _store_with_intake_injury
from test_today_read_prefetch import _view, _with_open_flags

DAY = "2026-06-03"
VIEW_ONLY_READS = ("get_profile", "list_sparring_logs", "list_pending_delayed_rehab",
                   "list_session_completions_from_day", "list_rehab_schedule_completions")


def _checked_in(store, plan_id=PLAN):
    store.upsert_today_checkin(ATHLETE, {"plan_id": plan_id, "training_day": DAY,
                                         "sleep": "good", "body": "fresh", "pain": "none"})
    return store


def _intake_without_injuries(store):
    store.intakes.setdefault(ATHLETE, []).append(
        {"id": "intake-1", "athlete_id": ATHLETE, "intake": {"injuries": ""},
         "created_at": "2026-05-30T00:00:00+00:00"})
    store.plans[PLAN]["intake_id"] = "intake-1"
    return store


def _with_try_set_active_plan_id(store):
    def try_set_active_plan_id(athlete_id, plan_id, expected_id, current_training_day=None):
        # Compare-and-set, as the production RPC.
        if store.get_active_plan_id(athlete_id) != expected_id:
            return False
        remaining = [p for p in store.list_user_plans(athlete_id) if p.get("status") != "archived"]
        if len(remaining) != 1 or remaining[0]["id"] != plan_id:
            return False
        store.set_active_plan_id(athlete_id, plan_id)
        return True

    store.try_set_active_plan_id = try_set_active_plan_id
    return store


SCENARIOS = {
    "no write, open injuries, checked in": lambda: _checked_in(_with_open_flags(_intake_without_injuries(_store_with_camp()))),
    "no write, no check-in": lambda: _with_open_flags(_intake_without_injuries(_store_with_camp())),
    "no intake on the plan": lambda: _checked_in(_with_open_flags(_store_with_camp())),
    "sync writes (intake injury)": lambda: _checked_in(_with_open_flags(_store_with_intake_injury())),
    "check-in for another plan only": lambda: _checked_in(_with_open_flags(_intake_without_injuries(_store_with_camp())),
                                                          plan_id="another-plan"),
}


def _twin(make):
    """Two stores with the same rows, ids and timestamps."""
    store = make()
    twin = make()
    for name in ("injury_flags", "today_checkins", "intakes", "plans"):
        if hasattr(store, name):
            setattr(twin, name, copy.deepcopy(getattr(store, name)))
    store.reads.clear()
    twin.reads.clear()
    return store, twin


def _no_prefetch(patch):
    for name in ("_begin_athlete_reads", "_prefetch_sync_reads", "_prefetch_view_reads"):
        patch.setattr(boundary, name, (lambda *a, **k: (lambda: None)) if name == "_begin_athlete_reads" else (lambda *a, **k: None))


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_the_view_is_identical_to_reading_everything_in_sequence(monkeypatch, scenario):
    store, sequential = _twin(SCENARIOS[scenario])
    with monkeypatch.context() as patch:
        _no_prefetch(patch)
        expected = _view(sequential)

    actual = _view(store)

    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")


def _waves_and_main_thread_reads(monkeypatch, store):
    """Prefetch waves that read something, and reads made one after another."""
    waves: list[list[str]] = []
    original = _BuildReadCache.begin_prefetch

    def counting(self, calls):
        calls = list(calls)
        before = Counter(store.reads)
        finish = original(self, calls)

        def done():
            finish()
            issued = Counter(store.reads) - before
            if issued:
                waves.append(sorted(issued))
        return done

    monkeypatch.setattr(_BuildReadCache, "begin_prefetch", counting)
    main_thread: list[str] = []
    for name in ("get_active_plan_id", "get_training_plan_for_athlete", *VIEW_ONLY_READS, "get_today_checkin",
                 "list_injury_episode_events", "list_today_checkins", "latest_sparring_log_day"):
        reader = getattr(FakeStore, name).__get__(store)

        def tracked(*args, _name=name, _reader=reader, **kwargs):
            if threading.current_thread() is threading.main_thread():
                main_thread.append(_name)
            return _reader(*args, **kwargs)

        store.__dict__[name] = tracked
    return waves, main_thread


def test_when_the_sync_cannot_write_every_view_read_rides_one_wave_after_the_plan(monkeypatch):
    store = SCENARIOS["no write, open injuries, checked in"]()
    store.reads.clear()
    waves, main_thread = _waves_and_main_thread_reads(monkeypatch, store)

    view = _view(store)

    assert view.open_injuries and view.today.recommendation_state != "not_checked_in"
    # Only the plan's pointer and row are read one after another.
    assert main_thread == ["get_active_plan_id", "get_training_plan_for_athlete"]
    # One wave beside the resolution, one after it; the view's own prefetch finds everything held.
    assert len(waves) == 2
    for name in (*VIEW_ONLY_READS, "get_today_checkin", "list_injury_episode_events", "list_external_training_sessions"):
        assert name in waves[1]
    # And nothing was read twice.
    assert store.reads["list_injury_episode_events"] == len([f for f in view.open_injuries])
    assert store.reads["get_profile"] == 1
    assert store.reads["list_injury_flags"] == 2


def test_when_the_sync_may_write_the_view_reads_wait_until_after_it(monkeypatch):
    store = SCENARIOS["sync writes (intake injury)"]()
    store.reads.clear()
    events: list[str] = []
    adopt = store.adopt_or_create_intake_injury_flag
    store.adopt_or_create_intake_injury_flag = lambda fields: (events.append("write"), adopt(fields))[1]
    profile = FakeStore.get_profile.__get__(store)
    store.__dict__["get_profile"] = lambda *a, **k: (events.append("view read"), profile(*a, **k))[1]

    _view(store)

    assert "write" in events
    # The view's rows are all read after the sync's write, as before.
    assert events.index("view read") > events.index("write")
    assert store.reads["get_profile"] == events.count("view read")


@pytest.mark.parametrize("intake, plan_intake_id, expected", [
    ({"injuries": ""}, "intake-1", True),
    ({"injuries": "left ankle sprain"}, "intake-1", False),
    ({"injuries": ""}, "older-intake", False),  # the latest intake is not the plan's
    ({"injuries": ""}, "", True),  # the sync falls back to the latest intake
])
def test_the_sync_counts_as_writing_unless_its_intake_lists_no_injury(intake, plan_intake_id, expected):
    store = _store_with_camp()
    store.intakes.setdefault(ATHLETE, []).append(
        {"id": "intake-1", "athlete_id": ATHLETE, "intake": intake, "created_at": "2026-05-30T00:00:00+00:00"})
    plan = {**store.plans[PLAN], "intake_id": plan_intake_id}
    resolution = ActivePlanResolution(plan=plan, source="explicit")

    assert boundary._sync_cannot_write(_BuildReadCache(store), athlete_id=ATHLETE, active_plan=resolution) is expected


def test_an_unknown_plan_or_a_failed_intake_read_counts_as_writing():
    store = _store_with_camp()
    cache = _BuildReadCache(store)
    assert boundary._sync_cannot_write(cache, athlete_id=ATHLETE, active_plan=None) is False

    store.get_latest_intake = lambda athlete_id: (_ for _ in ()).throw(RuntimeError("intake read down"))
    resolution = ActivePlanResolution(plan=store.plans[PLAN], source="explicit")
    assert boundary._sync_cannot_write(_BuildReadCache(store), athlete_id=ATHLETE, active_plan=resolution) is False


def test_a_write_while_a_wave_is_in_flight_discards_the_whole_wave():
    store = _store_with_camp()
    cache = _BuildReadCache(store)
    release = threading.Event()
    profile = FakeStore.get_profile.__get__(store)
    store.__dict__["get_profile"] = lambda *a, **k: (release.wait(5), profile(*a, **k))[1]

    finish = cache.begin_prefetch([("get_profile", (ATHLETE,), {})])
    cache.set_active_plan_id(ATHLETE, PLAN)  # a write through the cache, e.g. auto-activation
    release.set()
    finish()

    reads = store.reads["get_profile"]
    cache.get_profile(ATHLETE)
    assert store.reads["get_profile"] == reads + 1  # read again, not served from before the write


def test_auto_activation_during_the_resolution_leaves_the_view_unchanged(monkeypatch):
    def make():
        store = _with_try_set_active_plan_id(_checked_in(_with_open_flags(_intake_without_injuries(_store_with_camp()))))
        store.set_active_plan_id(ATHLETE, None)  # no pointer: the only plan is activated
        return store

    store, sequential = _twin(make)
    _with_try_set_active_plan_id(sequential)
    with monkeypatch.context() as patch:
        _no_prefetch(patch)
        expected = _view(sequential)

    actual = _view(store)

    assert store.get_active_plan_id(ATHLETE) == PLAN
    assert actual.model_dump(mode="json") == expected.model_dump(mode="json")
