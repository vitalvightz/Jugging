from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from postgrest.exceptions import APIError

from api.models import ComplianceAcceptanceRequest
from api.store import SupabaseAppStore
from tests.support import _build_client


def test_compliance_selects_only_authenticated_accounts_type(monkeypatch):
    client, store, _ = _build_client()
    calls = []

    def select(athlete_id, account_type):
        calls.append((athlete_id, account_type))
        store.profiles[athlete_id]['role'] = account_type
        return store.profiles[athlete_id]

    monkeypatch.setattr(store, 'select_signup_account_type', select, raising=False)
    response = client.post('/api/me/compliance', headers={'Authorization': 'Bearer athlete-token'}, json={
        'date_of_birth': '1995-01-01', 'accept_terms': True, 'account_type': 'coach',
        'athlete_id': 'another-athlete',
    })
    assert response.status_code == 200, response.text
    assert response.json()['profile']['role'] == 'coach'
    assert calls == [('athlete-1', 'coach')]


def test_signup_cannot_request_admin_or_gym_owner():
    for role in ['admin', 'gym_owner', 'COACH']:
        with pytest.raises(ValueError):
            ComplianceAcceptanceRequest(account_type=role)


def test_existing_compliance_flow_does_not_change_account_type(monkeypatch):
    client, store, _ = _build_client()
    select = MagicMock()
    monkeypatch.setattr(store, 'select_signup_account_type', select, raising=False)
    response = client.post('/api/me/compliance', headers={'Authorization': 'Bearer athlete-token'}, json={
        'date_of_birth': '1995-01-01', 'accept_terms': True,
    })
    assert response.status_code == 200
    select.assert_not_called()


def test_store_uses_service_rpc_and_refreshes_profile():
    store = SupabaseAppStore(client=MagicMock(), admin_emails=set())
    store._require_profile = MagicMock(return_value={'id': 'self', 'role': 'coach'})
    assert store.select_signup_account_type('self', 'coach')['role'] == 'coach'
    store.client.rpc.assert_called_once_with('select_signup_account_type', {'p_athlete_id':'self','p_account_type':'coach'})


def test_store_rejects_staff_choice_and_locked_accounts():
    store = SupabaseAppStore(client=MagicMock(), admin_emails=set())
    with pytest.raises(HTTPException) as invalid:
        store.select_signup_account_type('self', 'admin')
    assert invalid.value.status_code == 422
    store.client.rpc.assert_not_called()
    store.client.rpc.return_value.execute.side_effect = APIError({'code':'42501','message':'locked','details':None,'hint':None})
    with pytest.raises(HTTPException) as locked:
        store.select_signup_account_type('self','coach')
    assert locked.value.status_code == 403


def test_auth_metadata_does_not_grant_a_role():
    from api.auth import AuthenticatedUser
    store = SupabaseAppStore(client=MagicMock(), admin_emails=set())
    user = AuthenticatedUser(user_id='fresh', email='new@example.invalid', full_name='', metadata={'role':'admin','account_type':'coach'})
    assert store._default_role_for(user) == 'athlete'
