from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock

import pytest

from api.services.streaks import (
    get_streak_state,
    reconcile_adherence_streak,
    reconcile_training_streak,
    record_daily_activity,
)


class Store:
    def __init__(self):
        self.athlete_streaks = {}
        self.athlete_daily_activity = set()
        self.completions = []
        self.plans = []
        self.active_plan_id = None
        self.session_logs = []
        self.lock = Lock()

    def record_daily_activity(self, athlete_id, activity_date):
        with self.lock:
            self.athlete_daily_activity.add((athlete_id, activity_date))
            cursor = datetime.fromisoformat(activity_date).date()
            current = 0
            while (athlete_id, cursor.isoformat()) in self.athlete_daily_activity:
                current += 1
                cursor = cursor.fromordinal(cursor.toordinal() - 1)
            prior = self.athlete_streaks.get(athlete_id, {})
            self.athlete_streaks[athlete_id] = {
                "athlete_id": athlete_id,
                **prior,
                "login_current": current,
                "login_best": max(int(prior.get("login_best") or 0), current),
                "login_last_active_date": max(
                    day for owner, day in self.athlete_daily_activity if owner == athlete_id
                ),
            }
            return dict(self.athlete_streaks[athlete_id])

    def get_athlete_streaks(self, athlete_id):
        row = self.athlete_streaks.get(athlete_id)
        return dict(row) if row is not None else None

    def upsert_athlete_streaks(self, athlete_id, fields):
        with self.lock:
            self.athlete_streaks[athlete_id] = {
                **self.athlete_streaks.get(athlete_id, {}),
                **fields,
                "athlete_id": athlete_id,
            }
            return dict(self.athlete_streaks[athlete_id])

    def list_user_plans(self, athlete_id):
        return [row for row in self.plans if row["athlete_id"] == athlete_id]

    def get_active_plan_id(self, athlete_id):
        return self.active_plan_id

    def get_plan_for_athlete(self, plan_id, athlete_id):
        return next((row for row in self.plans if row["id"] == plan_id and row["athlete_id"] == athlete_id), None)

    def get_training_plan_for_athlete(self, plan_id, athlete_id):
        # The real projection, so a streak that needs a column it leaves out fails here.
        from api.store import TRAINING_PLAN_SELECT

        row = self.get_plan_for_athlete(plan_id, athlete_id)
        return {key: row[key] for key in TRAINING_PLAN_SELECT.split(",") if key in row} if row else None

    def list_plan_session_completions(self, athlete_id, plan_id, *, limit=500):
        return [row for row in self.completions if row["plan_id"] == plan_id][:limit]

    def list_session_completions(self, athlete_id, *, limit=500):
        return self.completions[:limit]

    def list_session_logs(self, athlete_id, *, limit=500):
        return self.session_logs[:limit]


def instant(day, hour=12):
    return datetime(2026, 8, day, hour, tzinfo=timezone.utc)


def plan(days, *, plan_id="plan-1", status="ready"):
    return {
        "id": plan_id,
        "athlete_id": "athlete-1",
        "status": status,
        "created_at": "2026-08-01T00:00:00Z",
        "structured_plan": {"weeks": [{"start_date": "2026-08-17", "end_date": "2026-08-23", "days": days}]},
    }


def training_day(day, session_id, day_type="high"):
    return {"date": f"2026-08-{day:02d}", "day_type": day_type, "sessions": [] if day_type == "rest" else [{"session_id": session_id}]}


def completion(day, session_id, status="done", plan_id="plan-1"):
    return {"plan_id": plan_id, "training_day": f"2026-08-{day:02d}", "session_id": session_id, "status": status, "updated_at": f"2026-08-{day:02d}T12:00:00Z"}


def test_same_day_retries_devices_and_concurrency_are_one_activity_day():
    store = Store()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: record_daily_activity(store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(18)), range(20)))
    assert {result["login"]["current"] for result in results} == {1}
    assert store.athlete_daily_activity == {("athlete-1", "2026-08-18")}


def test_login_next_day_gap_reset_and_best_is_retained():
    store = Store()
    for day in (16, 17, 18):
        record_daily_activity(store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(day))
    assert record_daily_activity(store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(20))["login"] == {"current": 1, "best": 3, "last_active_date": "2026-08-20"}


def test_timezone_boundary_and_invalid_timezone_fallback():
    store = Store()
    # 02:59 local remains the previous effective day; invalid zones safely use UTC.
    result = record_daily_activity(store, athlete_id="athlete-1", athlete_timezone="Europe/London", now=datetime(2026, 8, 18, 1, 59, tzinfo=timezone.utc))
    assert result["login"]["last_active_date"] == "2026-08-17"
    result = record_daily_activity(store, athlete_id="other", athlete_timezone="Not/AZone", now=instant(18))
    assert result["login"]["last_active_date"] == "2026-08-18"


@pytest.mark.parametrize("status", ["done", "modified"])
def test_done_and_safety_modified_qualify_without_duplicate_increment(status):
    store = Store()
    store.active_plan_id = "plan-1"
    store.plans = [plan([training_day(17, "a"), training_day(18, "b")])]
    store.completions = [completion(17, "a", status), completion(17, "a", status), completion(18, "b", status)]
    state = reconcile_adherence_streak(store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(18))
    assert state["plan_adherence"]["current"] == 2


def test_rest_is_neutral_but_missed_or_skipped_prescribed_work_breaks():
    store = Store()
    store.active_plan_id = "plan-1"
    store.plans = [plan([training_day(16, "a"), training_day(17, "rest", "rest"), training_day(18, "b"), training_day(19, "c")])]
    store.completions = [completion(16, "a"), completion(18, "b", "skipped"), completion(19, "c")]
    state = reconcile_adherence_streak(store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(19))
    assert state["plan_adherence"]["current"] == 1
    assert state["plan_adherence"]["best"] == 1


def test_inactive_plan_cannot_progress_or_farm_a_second_track():
    store = Store()
    active = plan([training_day(18, "active")])
    inactive = plan([training_day(18, "inactive")], plan_id="plan-2", status="archived")
    store.active_plan_id = "plan-1"
    store.plans = [active, inactive]
    store.completions = [completion(18, "inactive", plan_id="plan-2")]
    state = reconcile_adherence_streak(store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(18))
    assert state["plan_adherence"]["current"] == 0


def test_null_active_plan_clears_current_progress_without_using_saved_plan():
    store = Store()
    store.athlete_streaks["athlete-1"] = {"adherence_current": 4, "adherence_best": 6}
    store.plans = [plan([training_day(18, "saved")])]
    store.completions = [completion(18, "saved")]

    state = reconcile_adherence_streak(store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(18))

    assert state["plan_adherence"]["current"] == 0
    assert state["plan_adherence"]["best"] == 6


def test_restored_active_plan_reconciles_existing_completion_history():
    store = Store()
    store.plans = [plan([training_day(17, "a"), training_day(18, "b")])]
    store.completions = [completion(17, "a"), completion(18, "b", "modified")]
    assert reconcile_adherence_streak(
        store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(18)
    )["plan_adherence"]["current"] == 0

    store.active_plan_id = "plan-1"
    state = reconcile_adherence_streak(store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(18))

    assert state["plan_adherence"]["current"] == 2


def test_read_failure_fails_closed_and_best_survives_reset():
    store = Store()
    store.athlete_streaks["athlete-1"] = {"adherence_current": 3, "adherence_best": 5}
    store.active_plan_id = "plan-1"
    store.plans = [plan([training_day(17, "a")])]
    with pytest.raises(RuntimeError):
        store.list_plan_session_completions = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable"))
        reconcile_adherence_streak(store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(18))
    assert store.athlete_streaks["athlete-1"]["adherence_current"] == 3

    store.list_plan_session_completions = lambda *args, **kwargs: []
    state = reconcile_adherence_streak(store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(18))
    assert state["plan_adherence"]["current"] == 0
    assert state["plan_adherence"]["best"] == 5


def test_streak_state_read_does_not_record_or_reconcile_activity():
    store = Store()
    store.athlete_streaks["athlete-1"] = {
        "login_current": 4,
        "login_best": 7,
        "login_last_active_date": "2026-08-17",
    }
    before_activity = set(store.athlete_daily_activity)
    before_state = dict(store.athlete_streaks["athlete-1"])

    state = get_streak_state(
        store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(18)
    )

    assert state["login"]["current"] == 4
    assert store.athlete_daily_activity == before_activity
    assert store.athlete_streaks["athlete-1"] == before_state


def test_training_streak_counts_completed_and_modified_once_per_day():
    store = Store()
    store.active_plan_id = "plan-1"
    store.plans = [plan([training_day(17, "a"), training_day(18, "b")])]
    store.completions = [
        completion(17, "a"),
        completion(17, "a"),  # replay/duplicate is still one training day
        completion(18, "b", "modified"),
    ]

    state = reconcile_training_streak(
        store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(18)
    )

    assert state["adherence"] == {
        "current": 2,
        "best": 2,
        "last_qualifying_day": "2026-08-18",
    }


def test_skipped_or_started_does_not_count_and_a_missed_expected_day_breaks():
    store = Store()
    store.active_plan_id = "plan-1"
    store.plans = [plan([training_day(16, "a"), training_day(17, "b"), training_day(18, "c")])]
    store.completions = [
        completion(16, "a"),
        completion(17, "b", "started"),
        completion(18, "c", "skipped"),
    ]

    state = reconcile_training_streak(
        store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(18)
    )

    assert state["adherence"]["current"] == 0
    assert state["adherence"]["best"] == 1


def test_completed_manual_training_counts_but_incomplete_manual_log_does_not():
    store = Store()
    store.session_logs = [
        {"session_date": "2026-08-17", "session_type": "gym", "completed": True},
        {"session_date": "2026-08-18", "session_type": "sparring", "completed": False},
    ]

    state = reconcile_training_streak(
        store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(18)
    )

    assert state["adherence"]["current"] == 1
    assert state["adherence"]["last_qualifying_day"] == "2026-08-17"


def test_sessionless_technical_combat_on_rest_label_counts_but_arbitrary_plan_id_does_not():
    store = Store()
    store.active_plan_id = "plan-1"
    technical = {
        "date": "2026-08-20",
        "day_type": "rest",
        "sessions": [],
        "today_card": {"headline": "Technical-only combat"},
    }
    store.plans = [plan([technical])]
    store.completions = [completion(20, "2026-08-20")]

    state = reconcile_training_streak(
        store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(20)
    )
    assert state["adherence"]["current"] == 1

    store.completions = [completion(20, "arbitrary-id")]
    state = reconcile_training_streak(
        store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(20)
    )
    assert state["adherence"]["current"] == 0

    store.completions = [completion(20, "fake", plan_id="unknown-plan")]
    state = reconcile_training_streak(
        store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(20)
    )
    assert state["adherence"]["current"] == 0


def test_genuine_rest_day_is_neutral_and_historical_rebuild_is_idempotent():
    store = Store()
    store.active_plan_id = "plan-1"
    store.plans = [plan([
        training_day(16, "a"),
        training_day(17, "rest", "rest"),
        training_day(18, "b"),
    ])]
    store.completions = [completion(16, "a"), completion(18, "b")]

    first = reconcile_training_streak(
        store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(18)
    )
    second = reconcile_training_streak(
        store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(18)
    )

    assert first["adherence"] == second["adherence"]
    assert second["adherence"]["current"] == 2


def test_training_rebuild_uses_the_same_0300_local_day_boundary_as_today():
    store = Store()
    store.active_plan_id = "plan-1"
    store.plans = [plan([training_day(17, "a")])]
    store.completions = [completion(17, "a")]

    state = reconcile_training_streak(
        store,
        athlete_id="athlete-1",
        athlete_timezone="UTC",
        now=datetime(2026, 8, 18, 2, 59, tzinfo=timezone.utc),
    )

    assert state["adherence"]["last_qualifying_day"] == "2026-08-17"


def test_training_reconciliation_never_changes_login_streak():
    store = Store()
    store.athlete_streaks["athlete-1"] = {
        "login_current": 4,
        "login_best": 7,
        "login_last_active_date": "2026-08-18",
    }
    store.session_logs = [{"session_date": "2026-08-18", "completed": True}]

    state = reconcile_training_streak(
        store, athlete_id="athlete-1", athlete_timezone="UTC", now=instant(18)
    )

    assert state["login"] == {"current": 4, "best": 7, "last_active_date": "2026-08-18"}
