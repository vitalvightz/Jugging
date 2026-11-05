"""SupabaseAppStore.upsert_exercise_logs: a batch is one atomic statement."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from api.store import SupabaseAppStore, TRAINING_PLAN_REMOTE_SELECT, TRAINING_PLAN_SELECT

ROWS = [
    {"plan_id": "p", "training_day": "2026-10-07", "block_id": "a", "status": "as_prescribed"},
    {"plan_id": "p", "training_day": "2026-10-07", "block_id": "b", "status": "skipped"},
]


def _store(data):
    client = MagicMock()
    query = client.table.return_value.upsert.return_value
    query.execute.return_value = SimpleNamespace(data=data)
    return SupabaseAppStore(client=client, admin_emails=set()), client


@pytest.mark.parametrize("keep_existing", [False, True])
def test_the_whole_batch_goes_in_one_upsert_request(keep_existing):
    store, client = _store([{**row, "athlete_id": "ath"} for row in ROWS])

    written = store.upsert_exercise_logs("ath", ROWS, keep_existing=keep_existing)

    client.table.assert_called_once_with("exercise_logs")
    client.table.return_value.upsert.assert_called_once_with(
        [{"athlete_id": "ath", **row} for row in ROWS],
        on_conflict="athlete_id,plan_id,training_day,block_id",
        # keep_existing is ON CONFLICT DO NOTHING, decided in the same statement.
        ignore_duplicates=keep_existing,
    )
    assert [row["block_id"] for row in written] == ["a", "b"]


def test_a_short_write_without_keep_existing_is_an_error():
    store, _client = _store([{**ROWS[0], "athlete_id": "ath"}])

    with pytest.raises(HTTPException) as caught:
        store.upsert_exercise_logs("ath", ROWS)

    assert caught.value.status_code == 500


def test_keep_existing_returns_only_the_rows_it_inserted():
    store, _client = _store([{**ROWS[1], "athlete_id": "ath"}])

    assert [row["block_id"] for row in store.upsert_exercise_logs("ath", ROWS, keep_existing=True)] == ["b"]


def test_an_empty_batch_makes_no_request():
    store, client = _store([])

    assert store.upsert_exercise_logs("ath", []) == []
    client.table.assert_not_called()


@pytest.mark.parametrize("method,columns", [
    ("get_plan_identity_for_athlete", "id,athlete_id"),
    # #3199 preserves the local brief key through a computed-field alias.
    ("get_training_plan_for_athlete", TRAINING_PLAN_REMOTE_SELECT),
])
def test_compact_plan_reads_keep_the_owner_scope(method, columns):
    client = MagicMock()
    query = client.table.return_value.select.return_value.eq.return_value.eq.return_value
    query.limit.return_value.execute.return_value = SimpleNamespace(data=[{"id": "p", "athlete_id": "ath"}])
    store = SupabaseAppStore(client=client, admin_emails=set())

    assert getattr(store, method)("p", "ath") == {"id": "p", "athlete_id": "ath"}
    client.table.assert_called_once_with("plans")
    client.table.return_value.select.assert_called_once_with(columns)
    client.table.return_value.select.return_value.eq.assert_called_once_with("id", "p")
    client.table.return_value.select.return_value.eq.return_value.eq.assert_called_once_with("athlete_id", "ath")
    query.limit.assert_called_once_with(1)


def test_training_projection_keeps_fallback_inputs_without_stage2_blobs():
    columns = set(TRAINING_PLAN_SELECT.split(","))
    assert {"planning_brief", "plan_text", "structured_plan", "intake_id", "parsing_metadata"} <= columns
    assert not columns & {"stage2_payload", "stage2_handoff_text", "stage2_retry_text", "draft_plan_text", "final_plan_text"}


def test_failed_identity_read_is_an_outage_not_plan_not_found(monkeypatch):
    import httpx
    client = MagicMock()
    store = SupabaseAppStore(client=client, admin_emails=set())
    monkeypatch.setattr(store, "_select_first", lambda _query: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    monkeypatch.setattr("api.store.time.sleep", lambda _seconds: None)
    with pytest.raises(HTTPException) as caught:
        store.get_plan_identity_for_athlete("p", "ath")
    assert caught.value.status_code == 503


def test_exercise_history_read_is_cross_plan_owner_scoped_and_paginated():
    store, client = _store([])
    query = client.table.return_value.select.return_value.eq.return_value
    query.lt.return_value.order.return_value.order.return_value.order.return_value.range.return_value.execute.return_value = SimpleNamespace(data=[])
    assert store.list_exercise_history("ath", before_day="2026-10-07", limit=20, offset=40) == []
    client.table.return_value.select.return_value.eq.assert_called_once_with("athlete_id", "ath")
    query.lt.assert_called_once_with("training_day", "2026-10-07")
    query.lt.return_value.order.return_value.order.return_value.order.return_value.range.assert_called_once_with(40, 59)
