"""SupabaseAppStore.reset_history_plan_session: one RPC, owner-scoped, errors mapped."""
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from postgrest.exceptions import APIError

from api.store import SupabaseAppStore


def store_with(rpc_result=None, error=None):
    store = SupabaseAppStore.__new__(SupabaseAppStore)
    store.client = MagicMock()
    call = store.client.rpc.return_value
    if error:
        call.execute.side_effect = error
    else:
        call.execute.return_value = MagicMock(data=rpc_result)
    return store


def test_calls_the_atomic_function_once_with_owner_and_id():
    store = store_with({"id": "c1", "status": "not_started"})
    assert store.reset_history_plan_session("owner", "c1") == {"id": "c1", "status": "not_started"}
    store.client.rpc.assert_called_once_with("reset_history_plan_session", {"p_athlete_id": "owner", "p_completion_id": "c1"})
    store.client.table.assert_not_called()


def test_not_found_is_none():
    assert store_with(None).reset_history_plan_session("owner", "c1") is None


def test_the_rehab_reset_guard_is_a_conflict():
    error = APIError({"message": "rehab_exposure_cannot_be_reset", "code": "23514"})
    with pytest.raises(HTTPException) as caught:
        store_with(error=error).reset_history_plan_session("owner", "c1")
    assert caught.value.status_code == 409
