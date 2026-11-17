from api.auth import AuthenticatedUser
from support import _build_client, _build_request, finalized_result
import pytest


@pytest.mark.parametrize('archived', [False, True])
def test_athlete_can_permanently_delete_own_plan(archived):
    client, store, _ = _build_client()
    store.ensure_profile(AuthenticatedUser(user_id='athlete-1', email='ari@example.com', full_name='Ari', metadata={}))
    plan = store.create_plan(athlete_id='athlete-1', intake_id='intake_x', request=_build_request(), result=finalized_result())
    if archived:
        store.archive_plan_for_athlete(plan['id'], 'athlete-1')
    response = client.delete(f"/api/plans/{plan['id']}?permanent=true", headers={'Authorization': 'Bearer athlete-token'})
    assert response.status_code == 204
    assert store.get_plan(plan['id']) is None


def test_athlete_cannot_permanently_delete_coach_assignment():
    client, store, _ = _build_client()
    store.ensure_profile(AuthenticatedUser(user_id='athlete-1', email='ari@example.com', full_name='Ari', metadata={}))
    plan = store.create_plan(athlete_id='athlete-1', intake_id='intake_x', request=_build_request(), result=finalized_result())
    store.plans[plan['id']]['stage2_validator_report'] = {'custom_plan': {'coach_assignment_id': 'coach-assignment'}}
    response = client.delete(f"/api/plans/{plan['id']}?permanent=true", headers={'Authorization': 'Bearer athlete-token'})
    assert response.status_code == 403
    assert store.get_plan(plan['id']) is not None


def test_permanent_delete_is_ownership_scoped():
    client, store, _ = _build_client()
    store.ensure_profile(AuthenticatedUser(user_id='athlete-2', email='other@example.com', full_name='Other', metadata={}))
    plan = store.create_plan(athlete_id='athlete-2', intake_id='intake_x', request=_build_request(), result=finalized_result())
    response = client.delete(f"/api/plans/{plan['id']}?permanent=true", headers={'Authorization': 'Bearer athlete-token'})
    assert response.status_code == 404
    assert store.get_plan(plan['id']) is not None


def test_daily_checkin_store_reads_by_athlete_and_day_only():
    from unittest.mock import MagicMock
    from api.store import SupabaseAppStore
    client = MagicMock()
    query = client.table.return_value.select.return_value
    query.eq.return_value = query
    query.limit.return_value = query
    query.execute.return_value.data = [{'id': 'checkin', 'plan_id': 'previous-plan'}]
    store = SupabaseAppStore(client=client, admin_emails=set())
    assert store.get_today_checkin('athlete', 'new-plan', '2026-11-17')['id'] == 'checkin'
    assert [call.args for call in query.eq.call_args_list] == [('athlete_id', 'athlete'), ('training_day', '2026-11-17')]
