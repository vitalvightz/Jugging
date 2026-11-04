"""Store round trips per Today build stay flat as a camp gets longer.

A Today build used to read one completion row per remaining session while it
looked for the next session, re-validate the structured card once per helper,
and re-read the plan row the active-plan resolver had just read. The push sweep
then rebuilt the whole view in every dispatcher. These tests pin the flattened
read shape.
"""

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from api.services import fight_camp_notifications
from api.services import intelligent_notifications
from api.services import morning_push
from api.services import session_timing_notifications
from api.services import streak_notifications
from api.services import today_service
from api.services.fight_camp_notifications import FightCampDispatchResult
from api.services import today_readiness_boundary_core
from api.services.today_readiness_boundary import (
    _BuildReadCache,
    build_today_command_view,
    reuse_today_command_views,
)
from support import FakeStore
from api.store import TRAINING_PLAN_SELECT, SupabaseAppStore

ATHLETE = "athlete-1"
PLAN = "11111111-1111-1111-1111-111111111111"
CAMP_START = date(2026, 6, 1)
NOW = datetime(2026, 6, 3, 15, 0, tzinfo=timezone.utc)  # Wednesday of week 1


class CountingStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.reads = Counter()

    def __getattribute__(self, name):
        attr = super().__getattribute__(name)
        if callable(attr) and name.startswith(("get_", "list_")):
            reads = super().__getattribute__("reads")

            def counted(*args, **kwargs):
                reads[name] += 1
                return attr(*args, **kwargs)

            return counted
        return attr


def _camp(weeks: int = 12) -> dict:
    """Two sessions on each of five training days a week."""
    out = []
    for week in range(weeks):
        days = []
        for offset in range(7):
            day = CAMP_START + timedelta(days=week * 7 + offset)
            if offset in (2, 6):
                days.append(
                    {
                        "date": day.isoformat(),
                        "day_type": "rest",
                        "today_card": {"headline": "Rest or active recovery"},
                        "sessions": [],
                    }
                )
                continue
            days.append(
                {
                    "date": day.isoformat(),
                    "day_type": "hard",
                    "today_card": {"headline": "Strength + conditioning"},
                    "sessions": [
                        {
                            "session_id": f"{day.isoformat()}-strength",
                            "session_type": "strength",
                            "title": "Lower body strength",
                            "blocks": [{"name": "Trap bar deadlift", "sets": 3, "reps": "5"}],
                        },
                        {
                            "session_id": f"{day.isoformat()}-conditioning",
                            "session_type": "conditioning",
                            "title": "Aerobic intervals",
                            "blocks": [{"name": "Bike", "sets": 6, "reps": "2 min"}],
                        },
                    ],
                }
            )
        out.append({"phase_label": "GPP", "days": days})
    return {"weeks": out}


def _store_with_camp(athlete_id: str = ATHLETE, plan_id: str = PLAN) -> CountingStore:
    store = CountingStore()
    _add_camp(store, athlete_id=athlete_id, plan_id=plan_id)
    store.reads.clear()
    return store


def _add_camp(store: FakeStore, *, athlete_id: str, plan_id: str, weeks: int = 12) -> None:
    store.plans[plan_id] = {
        "id": plan_id,
        "athlete_id": athlete_id,
        "status": "ready",
        "plan_name": "Camp",
        "fight_date": (CAMP_START + timedelta(weeks=weeks)).isoformat(),
        "created_at": f"{CAMP_START.isoformat()}T00:00:00+00:00",
        "structured_plan": _camp(weeks),
    }
    store.set_active_plan_id(athlete_id, plan_id)


def _log(store: FakeStore, session_id: str, training_day: str, status: str = "done") -> None:
    store.upsert_session_completion(
        ATHLETE,
        {
            "plan_id": PLAN,
            "session_id": session_id,
            "training_day": training_day,
            "status": status,
        },
    )


def _next_session(view) -> dict:
    return view.today.next_session or {}


def _reads_for_camp(weeks: int) -> Counter:
    store = CountingStore()
    _add_camp(store, athlete_id=ATHLETE, plan_id=PLAN, weeks=weeks)
    store.reads.clear()
    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
    assert _next_session(view)["calendar_date"] == "2026-06-04"
    return store.reads


def test_today_reads_completions_once_however_long_the_camp():
    short_camp = _reads_for_camp(weeks=2)
    long_camp = _reads_for_camp(weeks=16)

    # The property under test: nothing Today reads grows with the camp.
    assert long_camp == short_camp
    assert long_camp["get_session_completion"] == 0
    assert long_camp["list_session_completions_from_day"] == 1
    # The injury sync and the build share one active-plan resolution.
    assert long_camp["get_active_plan_id"] == 1
    assert long_camp["get_plan_for_athlete"] == 1


def test_an_athlete_without_a_plan_reads_the_pointer_once():
    store = CountingStore()

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)

    assert view.active_plan == {}
    assert store.reads["get_active_plan_id"] == 1


def test_the_view_resolves_an_unusable_plan_again():
    # Its schedule probe checks the unusable row, which the shared resolution
    # does not carry.
    store = _store_with_camp()
    store.plans[PLAN]["status"] = "archived"

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)

    assert view.active_plan == {}
    assert store.reads["get_active_plan_id"] == 2


def test_the_shared_plan_is_the_one_the_schedule_probe_checks(monkeypatch):
    probed = []
    real_probe = today_readiness_boundary_core._probe_schedule
    monkeypatch.setattr(
        today_readiness_boundary_core,
        "_probe_schedule",
        lambda plan_row, training_day, health: probed.append(plan_row) or real_probe(plan_row, training_day, health),
    )

    build_today_command_view(_store_with_camp(), athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)

    assert [row["id"] for row in probed] == [PLAN]


def test_structured_card_is_validated_once_per_today_build(monkeypatch):
    store = _store_with_camp()
    resolutions = []
    original = today_service.resolve_effective_structured_plan

    def counting_resolve(plan_row, **kwargs):
        resolutions.append(plan_row.get("id"))
        return original(plan_row, **kwargs)

    monkeypatch.setattr(today_service, "resolve_effective_structured_plan", counting_resolve)

    build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)

    assert resolutions == [PLAN]
    # Outside a build nothing is remembered, so a changed card is re-resolved.
    today_service._structured_plan_weeks(store.plans[PLAN])
    today_service._structured_plan_weeks(store.plans[PLAN])
    assert resolutions == [PLAN, PLAN, PLAN]


def test_next_session_skips_days_logged_ahead_from_the_single_read():
    store = _store_with_camp()
    _log(store, "2026-06-04-strength", "2026-06-04")
    _log(store, "2026-06-05-conditioning", "2026-06-05", status="skipped")
    # Not terminal, so Saturday is still the next session.
    _log(store, "2026-06-06-strength", "2026-06-06", status="in_progress")
    store.reads.clear()

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)

    assert _next_session(view)["calendar_date"] == "2026-06-06"
    assert store.reads["get_session_completion"] == 0
    assert store.reads["list_session_completions_from_day"] == 1


def test_today_completion_comes_from_the_single_read():
    store = _store_with_camp()
    _log(store, "2026-06-03-strength", "2026-06-02", status="done")  # yesterday: ignored
    _log(store, "2026-06-04-strength", "2026-06-04", status="modified")
    store.reads.clear()

    now = datetime(2026, 6, 4, 15, 0, tzinfo=timezone.utc)
    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=now)

    assert view.today.completion_status == "modified"
    assert store.reads["get_session_completion"] == 0


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("started", [False, True])
def test_rehab_occurrence_uses_reader_timestamps_and_stable_priority(monkeypatch, fallback, reverse, started):
    day = "2026-10-02"
    status = "started" if started else "done"
    rows = [dict(id=identity, athlete_id=ATHLETE, plan_id=PLAN, session_id=identity, training_day=day,
                 status=state, created_at=created, updated_at=updated,
                 prescription_snapshot={"session": {"session_type": "rehab"}})
            for identity, state, created, updated in [
                ("z-old", status, day + "T07:00:00Z", day + "T08:00:00Z"),
                ("b-fresh", status, day + "T07:00:00Z", day + "T10:00:00Z"),
                ("c-fresh", status, day + "T10:00:00Z", None),
                ("a-terminal", "done", day + "T07:00:00Z", day + "T12:00:00Z"),
            ]]
    if reverse:
        rows.reverse()
    # Honour the real store's SELECT projection; otherwise fake full rows hide
    # missing timestamp columns in the paged fallback reader.
    selected = ["*"]
    query = MagicMock()
    def select(columns):
        selected[0] = columns
        return query
    query.select.side_effect = select
    for method in ("eq", "gte", "order", "limit", "range"):
        getattr(query, method).return_value = query
    query.execute.side_effect = lambda: SimpleNamespace(data=[
        dict(row) if selected[0] == "*" else {key: value for key, value in row.items() if key in selected[0].split(",")}
        for row in rows])
    client = MagicMock()
    client.table.return_value = query
    store = SupabaseAppStore(client=client, admin_emails=set())
    monkeypatch.setattr(today_service, "_UPCOMING_COMPLETION_READ_LIMIT", 1 if fallback else 200)
    completion = today_service._UpcomingCompletions(store, athlete_id=ATHLETE, from_day=day).rehab_completion(PLAN, day)
    assert completion["session_id"] == ("c-fresh" if started else "a-terminal")
    if fallback:
        assert {"created_at", "updated_at"} <= set(selected[0].split(","))


def _record_exact_read_days(store: FakeStore) -> list[str]:
    days: list[str] = []
    exact_read = store.get_session_completion

    def recording(athlete_id, session_id, training_day):
        days.append(training_day)
        return exact_read(athlete_id, session_id, training_day)

    store.get_session_completion = recording
    return days


def test_exactly_the_limit_of_rows_is_still_a_complete_read(monkeypatch):
    monkeypatch.setattr(today_service, "_UPCOMING_COMPLETION_READ_LIMIT", 2)
    store = _store_with_camp()
    _log(store, "2026-06-04-strength", "2026-06-04")
    _log(store, "2026-06-05-strength", "2026-06-05")
    exact_read_days = _record_exact_read_days(store)

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)

    assert _next_session(view)["calendar_date"] == "2026-06-06"
    assert exact_read_days == []


def test_capped_read_still_answers_every_day_before_its_last(monkeypatch):
    monkeypatch.setattr(today_service, "_UPCOMING_COMPLETION_READ_LIMIT", 2)
    store = _store_with_camp()
    _log(store, "2026-06-04-strength", "2026-06-04")
    _log(store, "2026-06-05-strength", "2026-06-05")
    _log(store, "2026-06-06-strength", "2026-06-06", status="in_progress")
    exact_read_days = _record_exact_read_days(store)

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)

    # The read stopped inside 6 June, so only that day needs exact reads; the
    # logged 4th and 5th are still skipped from the read itself.
    assert _next_session(view)["calendar_date"] == "2026-06-06"
    assert exact_read_days and set(exact_read_days) == {"2026-06-06"}


def test_capped_read_out_of_order_falls_back_to_exact_reads(monkeypatch):
    monkeypatch.setattr(today_service, "_UPCOMING_COMPLETION_READ_LIMIT", 1)
    store = _store_with_camp()
    _log(store, "2026-06-04-strength", "2026-06-04")
    _log(store, "2026-06-05-strength", "2026-06-05")
    store.list_session_completions_from_day = lambda *_a, **_k: [
        {"session_id": "2026-06-05-strength", "training_day": "2026-06-05", "status": "done"},
        {"session_id": "2026-06-04-strength", "training_day": "2026-06-04", "status": "done"},
    ]
    exact_read_days = _record_exact_read_days(store)

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)

    assert _next_session(view)["calendar_date"] == "2026-06-06"
    assert "2026-06-04" in exact_read_days


def test_unusable_ranged_read_keeps_exact_reads():
    class UnusableRangeStore(FakeStore):
        def list_session_completions_from_day(self, *args, **kwargs):
            return None

    store = UnusableRangeStore()
    _add_camp(store, athlete_id=ATHLETE, plan_id=PLAN)
    _log(store, "2026-06-04-conditioning", "2026-06-04")

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)

    assert _next_session(view)["calendar_date"] == "2026-06-05"


def test_reused_views_are_built_once_and_handed_out_as_copies(monkeypatch):
    store = _store_with_camp()
    builds = []
    original = today_readiness_boundary_core._today_service.build_today_command_view

    def counting_build(*args, **kwargs):
        builds.append(kwargs.get("athlete_id"))
        return original(*args, **kwargs)

    monkeypatch.setattr(
        today_readiness_boundary_core._today_service, "build_today_command_view", counting_build
    )

    with reuse_today_command_views():
        first = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
        first.today.warnings.append("changed by one consumer")
        second = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
        # A different instant is a different view.
        build_today_command_view(
            store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW + timedelta(hours=1)
        )

    assert builds == [ATHLETE, ATHLETE]
    assert second is not first
    assert "changed by one consumer" not in second.today.warnings

    # Outside the scope every call builds.
    build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
    assert builds == [ATHLETE, ATHLETE, ATHLETE]


@pytest.fixture
def push_configured(monkeypatch):
    monkeypatch.setenv("UNLXCK_VAPID_PRIVATE_KEY", "private")
    monkeypatch.setenv("UNLXCK_VAPID_PUBLIC_KEY", "public")


def _subscribed_store(athletes: int) -> FakeStore:
    store = FakeStore()
    for index in range(athletes):
        athlete_id = f"athlete-{index}"
        _add_camp(store, athlete_id=athlete_id, plan_id=f"{index:08d}-1111-1111-1111-111111111111")
        store.upsert_push_subscription(
            athlete_id,
            {
                "endpoint": f"https://push.example/{index}",
                "p256dh": "p256dh-key",
                "auth": "auth-key",
                "timezone": "UTC",
            },
        )
    return store


def test_push_sweep_builds_each_athletes_today_once(monkeypatch, push_configured):
    store = _subscribed_store(3)
    # Never reach the push transport: record would-be sends instead.
    sends = []
    monkeypatch.setattr("pywebpush.webpush", lambda **kwargs: sends.append(kwargs))

    builds = []
    original_build = today_readiness_boundary_core._today_service.build_today_command_view

    def counting_build(*args, **kwargs):
        builds.append(kwargs.get("athlete_id"))
        return original_build(*args, **kwargs)

    monkeypatch.setattr(
        today_readiness_boundary_core._today_service, "build_today_command_view", counting_build
    )
    # Each dispatcher module holds its own reference to the boundary builder;
    # count how many of them ask for a view, to prove the reuse is exercised.
    view_requests = Counter()
    for module in (
        fight_camp_notifications,
        session_timing_notifications,
        streak_notifications,
        intelligent_notifications,
    ):
        consumer = module.build_today_command_view

        def requesting(*args, _consumer=consumer, **kwargs):
            view_requests[kwargs.get("athlete_id")] += 1
            return _consumer(*args, **kwargs)

        monkeypatch.setattr(module, "build_today_command_view", requesting)

    # Inside the morning window, so every dispatcher evaluates each athlete.
    morning_push.run_morning_push_sweep(
        store, now_utc=datetime(2026, 6, 3, 7, 30, tzinfo=timezone.utc)
    )

    athletes = ["athlete-0", "athlete-1", "athlete-2"]
    assert all(view_requests[athlete] >= 2 for athlete in athletes)
    assert sorted(builds) == athletes


def test_push_sweep_stops_before_the_next_athlete_on_shutdown(monkeypatch, push_configured):
    store = _subscribed_store(3)
    evaluated = []

    def fake_fight_camp(_store, *, profile_id, **_kwargs):
        evaluated.append(profile_id)
        return FightCampDispatchResult(delivered_count=0, candidate_count=1)

    monkeypatch.setattr(morning_push, "dispatch_fight_camp_notifications", fake_fight_camp)

    morning_push.run_morning_push_sweep(
        store,
        now_utc=datetime(2026, 6, 3, 7, 30, tzinfo=timezone.utc),
        should_stop=lambda: len(evaluated) >= 1,
    )

    assert len(evaluated) == 1


def _store_with_intake_injury() -> CountingStore:
    store = _store_with_camp()
    store.intakes.setdefault(ATHLETE, []).append(
        {"id": "intake-1", "athlete_id": ATHLETE, "intake": {"injuries": "left ankle sprain"},
         "created_at": "2026-05-30T00:00:00+00:00"}
    )
    store.plans[PLAN]["intake_id"] = "intake-1"
    build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
    store.reads.clear()
    return store


def test_the_sync_and_the_view_share_the_intake_and_open_flag_reads():
    store = _store_with_intake_injury()

    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)

    assert [injury["body_area"] for injury in view.open_injuries]
    assert store.reads["get_intake"] == 1
    # Sync: open, open+resolved, open after its upsert. View: open+resolved
    # after that upsert (the sync's was read before it). The view's open-flag
    # read is the sync's last one.
    assert store.reads["list_injury_flags"] == 4


def test_a_failed_shared_read_is_not_kept_so_the_view_reads_again():
    store = _store_with_intake_injury()
    calls = {"n": 0}
    real = FakeStore.list_injury_flags

    def flaky(self, athlete_id, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:  # the sync's last open-flag read
            raise RuntimeError("flag read down")
        return real(self, athlete_id, **kwargs)

    store.list_injury_flags = flaky.__get__(store)
    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)

    assert calls["n"] == 5  # the view read the open flags itself
    assert [injury["body_area"] for injury in view.open_injuries]


def test_readiness_history_is_shared_only_within_one_build_and_same_limit():
    store = _store_with_intake_injury()
    build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
    assert store.reads["list_today_checkins"] == 1
    store.reads.clear()
    build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
    assert store.reads["list_today_checkins"] == 1


def test_injury_update_uses_the_plan_already_read_by_active_plan_resolution():
    store = _store_with_intake_injury()
    flag_id = store.list_injury_flags(ATHLETE)[0]["id"]
    store.reads.clear()

    result = today_service.submit_today_injury_checkin(
        store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW,
        payload={"injuries": [{"flag_id": flag_id, "status": "resolved"}]},
    )

    assert result["updated_injury_ids"] == [flag_id]
    assert result["open_injuries"] == []
    assert store.reads["get_plan_for_athlete"] == 1
    assert store.reads["get_training_plan_for_athlete"] == 0


def test_shared_history_retries_failures_and_invalidates_after_a_write():
    store = _store_with_camp()
    real_read = store.list_today_checkins
    read = MagicMock(side_effect=[RuntimeError("temporarily down"), [{"id": "before"}], [{"id": "older"}]])
    store.list_today_checkins = read
    cache = _BuildReadCache(store)
    with pytest.raises(RuntimeError):
        cache.list_today_checkins(ATHLETE, limit=14)
    first = cache.list_today_checkins(ATHLETE, limit=14)
    first[0]["id"] = "mutated copy"
    assert cache.list_today_checkins(ATHLETE, limit=14) == [{"id": "before"}]
    assert read.call_count == 2
    assert cache.list_today_checkins(ATHLETE, limit=28) == [{"id": "older"}]
    assert read.call_count == 3
    store.list_today_checkins = real_read
    cache.upsert_today_checkin(ATHLETE, {"plan_id": PLAN, "training_day": "2026-06-03"})
    assert cache.list_today_checkins(ATHLETE, limit=14)[0]["plan_id"] == PLAN


def test_finish_session_shares_preflight_reads_and_plan_projection(monkeypatch):
    from api.services.today_readiness_boundary import upsert_session_completion
    from api.models import TodayCheckinRequest

    store = _store_with_camp()
    now = NOW + timedelta(days=1)  # A scheduled training day.
    today_service.submit_today_checkin(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=now,
        payload=TodayCheckinRequest(plan_id=PLAN, sleep="good", body="normal", pain="none", phase="GPP").model_dump())
    view = build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=now)
    payload = {"plan_id": PLAN, "session_id": view.today.next_session["session_id"], "status": "done"}
    if view.live_prescription:
        payload["prescription_revision"] = view.live_prescription["revision"]
    resolutions = []
    original = today_service.resolve_effective_structured_plan

    def resolve(row, **kwargs):
        resolutions.append(row.get("id"))
        return original(row, **kwargs)

    monkeypatch.setattr(today_service, "resolve_effective_structured_plan", resolve)
    store.reads.clear()
    result = upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="UTC", payload=payload, now=now)
    assert result["status"] == "done"
    assert store.reads["get_training_plan_for_athlete"] == 1
    # The owned row and Today command's private copy each resolve at most once.
    assert len(resolutions) <= 2
    assert store.reads["list_injury_flags"] == 3


def test_injury_mutation_reads_the_checkin_once_and_observes_the_written_flags():
    from api.services.today_readiness_boundary import submit_today_injury_checkin

    store = _store_with_intake_injury()
    flag_id = store.list_injury_flags(ATHLETE)[0]["id"]
    store.reads.clear()
    result = submit_today_injury_checkin(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW,
        payload={"injuries": [{"flag_id": flag_id, "status": "resolved"}]})
    assert result["open_injuries"] == []
    assert result["updated_injury_ids"] == [flag_id]
    assert store.reads["get_today_checkin"] == 1
    assert store.reads["get_training_plan_for_athlete"] == 1
    # A later request must read again, including its newly written flags.
    store.reads.clear()
    reopened = submit_today_injury_checkin(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW,
        payload={"injuries": [{"flag_id": flag_id, "status": "ongoing"}]})
    assert [row["id"] for row in reopened["open_injuries"]] == [flag_id]
    assert store.reads["get_today_checkin"] == 1


def test_mutation_cache_keeps_exact_ownership_scopes_and_invalidates_even_failed_writes():
    store = _store_with_camp()
    cache = _BuildReadCache(store)
    first = cache.get_training_plan_for_athlete(PLAN, ATHLETE)
    first["structured_plan"]["weeks"].clear()
    assert cache.get_plan_for_athlete(PLAN, ATHLETE)["structured_plan"]["weeks"]
    assert store.reads["get_training_plan_for_athlete"] == 1
    assert cache.get_training_plan_for_athlete(PLAN, "another-athlete") is None
    store.update_injury_flag = MagicMock(side_effect=RuntimeError("write unavailable"))
    with pytest.raises(RuntimeError):
        cache.update_injury_flag("flag", {"status": "resolved"})
    assert cache.get_training_plan_for_athlete(PLAN, ATHLETE)
    assert store.reads["get_training_plan_for_athlete"] == 3


class _ProjectionOnlyStore(CountingStore):
    """Serves the projection without going through the full-row read, so a
    count of ``get_plan_for_athlete`` means a real ``select("*")`` on plans."""

    def get_training_plan_for_athlete(self, plan_id, athlete_id):
        row = self.plans.get(plan_id)
        if not row or str(row.get("athlete_id")) != athlete_id:
            return None
        return {key: row[key] for key in TRAINING_PLAN_SELECT.split(",") if key in row}


# Columns that make a plan row megabytes wide. None of them is read on the Today path.
_HEAVY_PLAN_COLUMNS = {"stage2_payload", "stage2_handoff_text", "stage2_result", "stage2_validator_report"}


def test_today_build_never_reads_the_full_plan_row():
    store = _ProjectionOnlyStore()
    _add_camp(store, athlete_id=ATHLETE, plan_id=PLAN, weeks=4)
    store.plans[PLAN].update({column: "x" * 100_000 for column in _HEAVY_PLAN_COLUMNS})
    store.reads.clear()

    build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)

    assert store.reads["get_plan_for_athlete"] == 0
    assert store.reads["get_training_plan_for_athlete"] == 1


def test_the_training_projection_leaves_out_the_stage2_blobs():
    assert not _HEAVY_PLAN_COLUMNS & set(TRAINING_PLAN_SELECT.split(","))


def test_progress_side_reads_use_the_training_projection():
    from api.services import streaks, training_focus_inference, week_progress, xp_progress

    store = _ProjectionOnlyStore()
    _add_camp(store, athlete_id=ATHLETE, plan_id=PLAN, weeks=4)
    _log(store, session_id="2026-06-01-strength", training_day="2026-06-01")
    store.reads.clear()

    streaks.qualifying_training_days(store, athlete_id=ATHLETE, today=NOW.date())
    training_focus_inference._plan_sport(store, ATHLETE, PLAN)
    xp_progress._active_plan(store, athlete_id=ATHLETE, command=SimpleNamespace(active_plan={"id": PLAN}))
    week_progress.try_award_completed_week_for_completion(
        store, athlete_id=ATHLETE, athlete_timezone="UTC",
        completion={"plan_id": PLAN, "training_day": "2026-06-01"},
    )

    assert store.reads["get_plan_for_athlete"] == 0
    assert store.reads["get_training_plan_for_athlete"] >= 3


_CONSENT = {
    "date_of_birth": "1995-01-01", "terms_version": "v1", "terms_accepted_at": "2026-01-01T00:00:00+00:00",
    "health_data_consent": True, "health_consent_at": "2026-01-01T00:00:00+00:00",
    "health_consent_withdrawn_at": None, "health_consent_version": "v1",
}


class _SeparateProfileReadsStore(_ProjectionOnlyStore):
    """Counts the consent read as its own round trip, as production does."""

    def get_profile_compliance(self, athlete_id):
        row = self.profiles.get(athlete_id)
        return {key: row[key] for key in _CONSENT if key in row} if row else None


def test_today_build_takes_consent_from_the_profile_row_it_already_read():
    store = _SeparateProfileReadsStore()
    _add_camp(store, athlete_id=ATHLETE, plan_id=PLAN, weeks=4)
    store.profiles[ATHLETE] = {"id": ATHLETE, "onboarding_draft": {}, **_CONSENT}
    store.reads.clear()

    build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)

    assert store.reads["get_profile"] == 1
    assert store.reads["get_profile_compliance"] == 0


def test_consent_is_read_again_after_a_write_and_without_a_full_row():
    store = _SeparateProfileReadsStore()
    store.profiles[ATHLETE] = {"id": ATHLETE, **_CONSENT}
    cache = _BuildReadCache(store)

    # No full row yet: the real consent read runs.
    assert cache.get_profile_compliance(ATHLETE)["health_data_consent"] is True
    assert store.reads["get_profile_compliance"] == 1

    cache.get_profile(ATHLETE)
    assert cache.get_profile_compliance(ATHLETE)["health_data_consent"] is True
    assert store.reads["get_profile_compliance"] == 1

    # A write in the same operation drops the shared row; consent is re-read.
    store.profiles[ATHLETE]["health_data_consent"] = False
    cache.set_active_plan_id(ATHLETE, PLAN)
    assert cache.get_profile_compliance(ATHLETE)["health_data_consent"] is False
    assert store.reads["get_profile_compliance"] == 2
