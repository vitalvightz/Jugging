"""Ensure service-role writes stay owner scoped and retries never overwrite."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from api.store import SupabaseAppStore


def store_query():
    client = MagicMock()
    query = client.table.return_value
    for method in ("select", "eq", "is_", "order", "limit", "upsert", "update"):
        getattr(query, method).return_value = query
    query.execute.return_value = SimpleNamespace(data=[{"id": "entry", "athlete_id": "owner"}])
    return SupabaseAppStore(client=client, admin_emails=set()), query


def test_atomic_insert_does_not_overwrite_an_edit_or_trust_client_owner():
    store, query = store_query()
    fields = {"client_request_id": "request", "athlete_id": "attacker", "duration_minutes": 60}
    assert store.create_external_training_session("owner", fields)["id"] == "entry"
    query.upsert.assert_called_once_with({**fields, "athlete_id": "owner"},
        on_conflict="athlete_id,client_request_id", ignore_duplicates=True)
    assert ("athlete_id", "owner") in [call.args for call in query.eq.call_args_list]


def test_update_whitelist_cannot_move_date_owner_or_idempotency_key():
    store, query = store_query()
    fields = {"activity": "boxing", "duration_minutes": 45, "effort": "easy", "sport": "boxing", "session_type": "pads",
              "distance_value": None, "distance_unit": None, "duration_seconds": None,
              "athlete_id": "other", "training_day": "2027-01-01", "client_request_id": "changed", "takeaway": "lost"}
    store.update_external_training_session("owner", "entry", fields)
    query.update.assert_called_once_with({key: fields[key] for key in ("activity", "duration_minutes", "effort", "sport", "session_type",
                                                                         "distance_value", "distance_unit", "duration_seconds")})
    assert [call.args for call in query.eq.call_args_list] == [("athlete_id", "owner"), ("id", "entry")]
    query.is_.assert_called_once_with("deleted_at", "null")


def test_update_without_discipline_clears_it_instead_of_keeping_a_stale_pair():
    # An edit replaces the whole classification: a stored sport/session type
    # can never survive next to an activity it no longer maps to.
    store, query = store_query()
    store.update_external_training_session("owner", "entry", {"activity": "roadwork", "duration_minutes": 30, "effort": "easy"})
    query.update.assert_called_once_with({"activity": "roadwork", "duration_minutes": 30, "effort": "easy",
                                          "sport": None, "session_type": None,
                                          "distance_value": None, "distance_unit": None, "duration_seconds": None})


def test_list_excludes_tombstones_and_other_owners():
    store, query = store_query()
    store.list_external_training_sessions("owner", limit=20)
    query.eq.assert_called_once_with("athlete_id", "owner")
    query.is_.assert_called_once_with("deleted_at", "null")
    query.limit.assert_called_once_with(20)


def test_internal_refresh_can_see_tombstones_without_losing_owner_scope():
    store, query = store_query()
    store.list_external_training_sessions("owner", include_deleted=True)
    query.eq.assert_called_once_with("athlete_id", "owner")
    query.is_.assert_not_called()


def test_delete_is_scoped_even_with_privileged_client():
    store, query = store_query()
    assert store.delete_external_training_session("owner", "entry")
    # Session lookup, session tombstone, then the same owner's exercise entries.
    assert [call.args for call in query.eq.call_args_list] == [("athlete_id", "owner"), ("id", "entry")] * 2 + [
        ("athlete_id", "owner"), ("external_session_id", "entry")]
    assert [set(call.args[0]) for call in query.update.call_args_list] == [{"deleted_at"}, {"deleted_at"}]


def manual_store():
    store, query = store_query()
    for method in ("in_", "lte", "gt", "or_", "delete"):
        getattr(query, method).return_value = query
    return store, query


def test_manual_exercise_insert_is_atomic_owner_scoped_and_ignores_client_owner():
    store, query = manual_store()
    query.execute.return_value = SimpleNamespace(data=[{"id": "row", "athlete_id": "owner", "client_entry_id": "entry"}])
    fields = {"client_entry_id": "entry", "external_session_id": "session", "athlete_id": "attacker"}
    store.create_manual_exercise("owner", fields)
    query.upsert.assert_called_once_with({**fields, "athlete_id": "owner"},
        on_conflict="athlete_id,client_entry_id", ignore_duplicates=True)
    assert ("athlete_id", "owner") in [call.args for call in query.eq.call_args_list]


def test_manual_exercise_update_whitelist_cannot_move_session_day_owner_or_undelete():
    store, query = manual_store()
    fields = {"display_name": "Back Squat", "measurement_format": "strength", "actual": {}, "notes": "",
              "exercise_key": "back-squat", "personal_exercise_id": None, "athlete_id": "other",
              "external_session_id": "elsewhere", "training_day": "2020-01-01", "deleted_at": None}
    store.update_manual_exercise("owner", "entry", fields, revision=4)
    assert query.update.call_args.args[0] == {**{key: fields[key] for key in (
        "exercise_key", "personal_exercise_id", "display_name", "measurement_format", "actual", "notes")}, "revision": 5}
    # Compare-and-set: only the revision the edit was based on can be replaced.
    assert [call.args for call in query.eq.call_args_list] == [
        ("athlete_id", "owner"), ("client_entry_id", "entry"), ("revision", 4)]
    query.is_.assert_called_once_with("deleted_at", "null")


def test_manual_exercise_reads_are_owner_scoped_and_hide_tombstones():
    store, query = manual_store()
    store.list_manual_exercises("owner", "session")
    store.list_manual_exercise_index_page("owner", as_of="2026-10-19T00:00:00+00:00", after_id=None)
    store.list_manual_exercise_performances("owner", exercise_key="back-squat", personal_exercise_id=None,
        as_of="2026-10-19T00:00:00+00:00", before_day=None, before_id=None)
    query.execute.return_value = SimpleNamespace(data=[{"external_session_id": "session"}] * 2)
    assert store.count_manual_exercises("owner", ["session"]) == {"session": 2}
    owners = [call.args for call in query.eq.call_args_list if call.args[0] == "athlete_id"]
    assert owners == [("athlete_id", "owner")] * 4
    assert query.is_.call_count == 4
    assert all(call.args == ("deleted_at", "null") for call in query.is_.call_args_list)
    assert store.count_manual_exercises("owner", []) == {}
