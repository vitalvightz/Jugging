"""History's independent reads run together, with identical results and errors."""

from __future__ import annotations

import threading
import time

import pytest

from api.routes import today as today_routes
from api.services import training_history
from api.services.today_reads import run_reads_concurrently
from support import _build_client
from test_today_read_amplification import _add_camp, PLAN

H = {"Authorization": "Bearer athlete-token"}
PLAN_B = "22222222-2222-2222-2222-222222222222"


def _sequential(*reads):
    return [read() for read in reads]


def test_results_keep_argument_order():
    assert run_reads_concurrently(lambda: (time.sleep(0.05), "a")[1], lambda: "b") == ["a", "b"]


def test_the_first_error_in_argument_order_is_raised_after_every_read_finished():
    finished = []

    def fails(label):
        def read():
            time.sleep(0.02 if label == "first" else 0)
            finished.append(label)
            raise RuntimeError(label)
        return read

    with pytest.raises(RuntimeError, match="first"):
        run_reads_concurrently(fails("first"), fails("second"), lambda: finished.append("ok"))
    assert sorted(finished) == ["first", "ok", "second"]


def _history_client():
    client, store, _ = _build_client()
    me = client.get("/api/me", headers=H).json()
    athlete = (me.get("profile") or {}).get("athlete_id") or me["athlete_id"]
    for plan_id in (PLAN, PLAN_B):
        _add_camp(store, athlete_id=athlete, plan_id=plan_id, weeks=2)
        day = next(d for d in store.plans[plan_id]["structured_plan"]["weeks"][0]["days"] if d.get("sessions"))
        store.upsert_session_completion(athlete, {"plan_id": plan_id, "session_id": day["sessions"][0]["session_id"],
                                                  "training_day": day["date"], "status": "done"})
    store.create_external_training_session(athlete, {"training_day": "2026-06-05", "activity": "boxing",
                                                      "duration_minutes": 30, "client_request_id": "r1"})
    return client, store


def test_history_is_identical_to_reading_one_after_another(monkeypatch):
    client, _ = _history_client()
    url = "/api/training/history?include_skipped=true&limit=30"
    concurrent = client.get(url, headers=H).json()
    monkeypatch.setattr(today_routes, "run_reads_concurrently", _sequential)
    monkeypatch.setattr(training_history, "run_reads_concurrently", _sequential)
    sequential = client.get(url, headers=H).json()

    assert concurrent == sequential
    assert {row["title"] for row in concurrent["sessions"] if row["source"] == "unlxck"} == {"Lower body strength"}


def test_history_reads_overlap_instead_of_queueing(monkeypatch):
    client, store = _history_client()
    active, peak = {"n": 0}, {"n": 0}
    lock = threading.Lock()
    names = ("list_session_completions", "list_external_training_sessions",
             "get_training_plan_for_athlete", "count_manual_exercises")
    for name in names:
        original = getattr(store, name)

        def slow(*args, _original=original, **kwargs):
            with lock:
                active["n"] += 1
                peak["n"] = max(peak["n"], active["n"])
            time.sleep(0.05)
            with lock:
                active["n"] -= 1
            return _original(*args, **kwargs)

        setattr(store, name, slow)

    response = client.get("/api/training/history?include_skipped=true&limit=30", headers=H)

    assert response.status_code == 200
    assert peak["n"] >= 2  # independent reads were in flight together
