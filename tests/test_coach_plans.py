"""Exercise the real HTTP, compiler, activation, calendar and History paths.

The store double implements only the new persistence boundary; its atomic SQL
and races are tested separately against PostgreSQL.
"""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from tests.test_coach_fighter_access import COACH, FIGHTER, AS_COACH, BASE
from tests.test_coach_fighter_access import camp  # noqa: F401 - shared pytest fixture
from tests.test_custom_open_plans import body
from api.services.today_service import resolve_training_day
from api.services.coach_plans import assignment_view

AS_FIGHTER = {'Authorization': 'Bearer fighter-token'}
ASSIGNMENTS = '/api/custom-plans/assignments'


@pytest.fixture
def plans(camp):  # noqa: F811 - pytest fixture injection
    client, store, connection = camp
    def command(actor, fighter, identity, action, fields=None):
        if connection['status'] != 'accepted' or actor != COACH or fighter != FIGHTER:
            raise HTTPException(409, 'Connection ended')
        row = store.custom_plans.get(identity)
        if action == 'create' and not row:
            row = {**deepcopy(fields), 'id': identity, 'athlete_id': actor, 'fighter_id': fighter,
                   'created_at': datetime.now(timezone.utc).isoformat(), 'updated_at': datetime.now(timezone.utc).isoformat()}
            store.custom_plans[identity] = row
        if not row or row['athlete_id'] != actor or row['fighter_id'] != fighter:
            raise HTTPException(404)
        if action == 'update':
            if row.get('assigned_at'):
                raise HTTPException(409)
            row.update(deepcopy(fields))
        if action == 'assign':
            if not row.get('assigned_at'):
                row['assigned_at'] = datetime.now(timezone.utc).isoformat()
            row['coach_name'] = 'Coach Sam'
        if action == 'retract':
            if row.get('accepted_at') or row.get('declined_at'):
                raise HTTPException(409)
            for plan_id, plan in list(store.plans.items()):
                if plan.get('status') == 'review_required' and (plan.get('planning_brief') or {}).get('coach_assignment_id') == identity:
                    del store.plans[plan_id]
            row.update(assigned_at=None, coach_name=None)
        if action == 'remove':
            answered = row.get('accepted_at') or row.get('declined_at')
            active = row.get('accepted_at') and row.get('published_plan_id') and store.get_active_plan_id(fighter) == row['published_plan_id']
            if not answered or active:
                raise HTTPException(409)
            row.setdefault('coach_removed_at', datetime.now(timezone.utc).isoformat())
        return deepcopy(row)
    def respond(actor, identity, action, **kw):
        row = store.custom_plans[identity]
        if actor != row['fighter_id'] or not row.get('assigned_at'):
            return False
        if action == 'decline':
            if row.get('accepted_at'):
                return False
            row['declined_at'] = datetime.now(timezone.utc).isoformat()
            return True
        if row.get('declined_at') or connection['status'] != 'accepted':
            return False
        if row.get('accepted_at'):
            return row['published_plan_id'] == kw['plan_id']
        if store.get_active_plan_id(actor) != kw['expected_id']:
            return False
        store.plans[kw['plan_id']]['status'] = 'ready'
        row.update(accepted_at=datetime.now(timezone.utc).isoformat(), published_plan_id=kw['plan_id'])
        if kw['replace'] and kw['expected_id']:
            store.plans[kw['expected_id']]['status'] = 'archived'
        store.set_active_plan_id(actor, kw['plan_id'])
        return True
    store.coach_plan_command = command
    store.respond_coach_plan = respond
    store.list_coach_plans = lambda actor, fighter: [deepcopy(r) for r in store.custom_plans.values()
                                                     if r['athlete_id'] == actor and r.get('fighter_id') == fighter and not r.get('coach_removed_at')]
    store.list_plan_assignments = lambda fighter: [deepcopy(r) for r in store.custom_plans.values() if r.get('fighter_id') == fighter and r.get('assigned_at')]
    return client, store, connection


def create(client, prescription=None):
    identity = str(uuid4())
    payload = prescription or body()
    response = client.post(f'{BASE}/plans?request_id={identity}', headers=AS_COACH, json=payload)
    assert response.status_code == 201, response.text
    return response.json(), payload


def send(client, row):
    response = client.post(f"{BASE}/plans/{row['id']}/assign", headers=AS_COACH)
    assert response.status_code == 200, response.text
    return response.json()


def test_draft_assign_preview_activate_history_and_supersede(plans):
    client, store, _ = plans
    day = resolve_training_day(None)
    prescription = body()
    prescription.update(schedule_mode='dated', start_date=day, end_date=day)
    prescription['sessions'] = [{**prescription['sessions'][0], 'scheduled_date': day}]
    draft, payload = create(client, prescription)
    assert draft['assignment_status'] == 'draft'
    assert client.get(ASSIGNMENTS, headers=AS_FIGHTER).json() == []
    payload['name'] = 'Fight preparation'
    assert client.put(f"{BASE}/plans/{draft['id']}", headers=AS_COACH, json=payload).status_code == 200
    row = send(client, draft)
    assert row['assignment_status'] == 'pending' and row['coach_name'] == 'Coach Sam'
    assert not store.plans and store.get_active_plan_id(FIGHTER) is None
    assert send(client, row)['assigned_at'] == row['assigned_at']
    preview = client.get(ASSIGNMENTS, headers=AS_FIGHTER).json()[0]
    assert preview['sessions'] == row['sessions']
    assert client.put(f"{BASE}/plans/{row['id']}", headers=AS_COACH, json=payload).status_code == 409
    assert client.get(f"/api/custom-plans/{row['id']}", headers=AS_COACH).status_code == 404
    result = client.post(f"{ASSIGNMENTS}/{row['id']}/activate", headers=AS_FIGHTER, json={})
    assert result.status_code == 200, result.text
    plan_id = result.json()['plan_id']
    assert result.json()['custom_plan_id'] is None
    # Coach assignments are structured-only custom plans. They skip Stage 2 and
    # expose the same content source as athlete-authored plans without granting
    # access to the coach's editable draft.
    detail = client.get(f'/api/plans/{plan_id}', headers=AS_FIGHTER)
    assert detail.status_code == 200, detail.text
    detail = detail.json()
    assert detail['status'] == 'ready'
    assert detail['plan_source'] == 'custom' and detail['custom_plan_id'] is None
    assert detail['outputs']['plan_text'] == ''
    assert detail['outputs']['structured_plan']['plan_metadata']['prescription_source'] == 'athlete'
    assert detail['structured_card_state']['state'] == 'live'
    assert store.plans[plan_id]['stage2_status'] == 'not_required'
    assert not store.generation_jobs
    assert store.get_active_plan_id(FIGHTER) == plan_id
    assert assignment_view(store, store.custom_plans[row['id']])['assignment_status'] == 'active'
    assert client.post(f"{ASSIGNMENTS}/{row['id']}/decline", headers=AS_FIGHTER).status_code == 409
    # The normal projected calendar and History use the very same plan/session.
    schedule = client.get(f'{BASE}/training/plan?include_past=true', headers=AS_COACH).json()
    assert schedule and all(item['plan_id'] == plan_id for item in schedule)
    session = schedule[0]
    assert session['day'] == day
    checkin = client.post('/api/today/checkin', headers=AS_FIGHTER, json={
        'plan_id': plan_id, 'sleep': 'good', 'body': 'normal', 'pain': 'none', 'phase': 'GPP',
    })
    assert checkin.status_code == 201, checkin.text
    completion = {'plan_id': plan_id, 'session_id': session['id']}
    started = client.post('/api/today/session-completion', headers=AS_FIGHTER, json={**completion, 'status': 'started'})
    assert started.status_code == 201, started.text
    logged = client.post('/api/today/exercise-log', headers=AS_FIGHTER, json={
        'plan_id': plan_id, 'block_id': session['blocks'][0]['feedback_ref'], 'status': 'as_prescribed',
    })
    assert logged.status_code == 201, logged.text
    for _ in range(2):  # Repeat the save as if the first response was lost.
        completed = client.post('/api/today/session-completion', headers=AS_FIGHTER,
                                json={**completion, 'status': 'done', 'session_rpe': 6})
        assert completed.status_code == 201, completed.text
    history = client.get(f'{BASE}/training/history', headers=AS_COACH)
    assert history.status_code == 200, history.text
    records = [s for s in history.json()['sessions'] if s['plan_id'] == plan_id]
    assert len(records) == 1 and records[0]['status'] == 'done' and records[0]['session_rpe'] == 6
    details = client.get(f"{BASE}/training/history/unlxck/{records[0]['id']}", headers=AS_COACH)
    assert details.status_code == 200, details.text
    assert details.json()['blocks'][0]['status'] == 'as_prescribed'
    other, _ = create(client)
    send(client, other)
    assert client.post(f"{ASSIGNMENTS}/{other['id']}/activate", headers=AS_FIGHTER, json={'overlap_action': 'pause'}).status_code == 200
    assert store.plans[plan_id]['status'] == 'ready'
    assignments = client.get(ASSIGNMENTS, headers=AS_FIGHTER).json()
    assert next(item for item in assignments if item['id'] == row['id'])['assignment_status'] == 'superseded'
    # An old activation retry must not switch back after the next assignment.
    current = store.get_active_plan_id(FIGHTER)
    assert client.post(f"{ASSIGNMENTS}/{row['id']}/activate", headers=AS_FIGHTER, json={}).status_code == 200
    assert store.get_active_plan_id(FIGHTER) == current


@pytest.mark.parametrize('action', ['pause', 'replace'])
def test_preserves_current_plan_until_explicit_overlap_choice(plans, action):
    client, store, _ = plans
    first, _ = create(client)
    send(client, first)
    initial = client.post(f"{ASSIGNMENTS}/{first['id']}/activate", headers=AS_FIGHTER, json={}).json()['plan_id']
    second, _ = create(client)
    send(client, second)
    assert store.get_active_plan_id(FIGHTER) == initial
    conflict = client.post(f"{ASSIGNMENTS}/{second['id']}/activate", headers=AS_FIGHTER, json={})
    assert conflict.status_code == 409 and conflict.json()['detail']['code'] == 'active_plan_overlap'
    assert store.get_active_plan_id(FIGHTER) == initial and len(store.plans) == 1
    result = client.post(f"{ASSIGNMENTS}/{second['id']}/activate", headers=AS_FIGHTER, json={'overlap_action': action})
    assert result.status_code == 200, result.text
    assert store.plans[initial]['status'] == ('archived' if action == 'replace' else 'ready')


def test_decline_revoke_permissions_and_no_cross_fighter_data(plans):
    client, store, connection = plans
    row, payload = create(client)
    send(client, row)
    for token in ['coach-token', 'stranger-token', 'admin-token', 'athlete-token']:
        for action in ['activate', 'decline']:
            assert client.post(f"{ASSIGNMENTS}/{row['id']}/{action}", headers={'Authorization': f'Bearer {token}'}, json={}).status_code == 404
    for action in ['get', 'put']:
        call = getattr(client, action)
        extra = {'json': payload} if action == 'put' else {}
        assert call(f"{BASE}/plans/{row['id']}", headers={'Authorization': 'Bearer stranger-token'}, **extra).status_code == 404
    assert client.post(f"{ASSIGNMENTS}/{row['id']}/decline", headers=AS_FIGHTER).status_code == 200
    assert client.post(f"{ASSIGNMENTS}/{row['id']}/decline", headers=AS_FIGHTER).status_code == 200
    assert client.post(f"{ASSIGNMENTS}/{row['id']}/activate", headers=AS_FIGHTER, json={}).status_code == 409
    assert not store.plans
    pending, _ = create(client)
    send(client, pending)
    accepted, _ = create(client)
    send(client, accepted)
    assert client.post(f"{ASSIGNMENTS}/{accepted['id']}/activate", headers=AS_FIGHTER, json={}).status_code == 200
    before = deepcopy(store.plans)
    connection['status'] = 'revoked'
    assert client.post(f'{BASE}/plans?request_id={uuid4()}', headers=AS_COACH, json=payload).status_code == 404
    assert client.post(f"{BASE}/plans/{pending['id']}/assign", headers=AS_COACH).status_code == 404
    assert client.get(f'{BASE}/plans', headers=AS_COACH).status_code == 404
    assert client.post(f"{ASSIGNMENTS}/{pending['id']}/activate", headers=AS_FIGHTER, json={}).status_code == 409
    assert store.plans == before


def test_incomplete_and_private_attachments_are_rejected(plans):
    client, _, _ = plans
    row, _ = create(client, {'name': 'Draft', 'sessions': []})
    assert client.post(f"{BASE}/plans/{row['id']}/assign", headers=AS_COACH).status_code == 422
    payload = body()
    payload['sessions'][0]['exercises'][0]['media_id'] = str(uuid4())
    assert client.post(f'{BASE}/plans?request_id={uuid4()}', headers=AS_COACH, json=payload).status_code == 422


def test_removed_accepted_plan_remains_superseded_and_cannot_be_resurrected(plans):
    client, store, _ = plans
    row, _ = create(client)
    send(client, row)
    response = client.post(f"{ASSIGNMENTS}/{row['id']}/activate", headers=AS_FIGHTER, json={})
    assert response.status_code == 200
    store.plans.pop(response.json()['plan_id'])
    store.custom_plans[row['id']]['published_plan_id'] = None
    store.profiles[FIGHTER]['active_plan_id'] = None
    assert client.get(ASSIGNMENTS, headers=AS_FIGHTER).json()[0]['assignment_status'] == 'superseded'
    assert client.post(f"{ASSIGNMENTS}/{row['id']}/activate", headers=AS_FIGHTER, json={}).status_code == 404
    assert not store.plans


def test_interrupted_consent_leaves_an_inert_plan_and_retry_reuses_it(plans):
    client, store, _ = plans
    row, _ = create(client)
    send(client, row)
    respond = store.respond_coach_plan
    store.respond_coach_plan = lambda *args, **kw: False
    assert client.post(f"{ASSIGNMENTS}/{row['id']}/activate", headers=AS_FIGHTER, json={}).status_code == 409
    plan = next(iter(store.plans.values()))
    assert plan['status'] == 'review_required' and store.get_active_plan_id(FIGHTER) is None
    assert client.get('/api/plans', headers=AS_FIGHTER).json() == []
    assert client.get('/api/plans/active', headers=AS_FIGHTER).status_code == 404
    assert client.post(f"/api/plans/{plan['id']}/set-active", headers=AS_FIGHTER, json={}).status_code == 409
    store.respond_coach_plan = respond
    result = client.post(f"{ASSIGNMENTS}/{row['id']}/activate", headers=AS_FIGHTER, json={})
    assert result.status_code == 200, result.text
    assert result.json()['plan_id'] == plan['id'] and len(store.plans) == 1


def test_failed_activation_does_not_map_unaccepted_assignment_as_released(plans):
    client, store, _ = plans
    row, _ = create(client)
    send(client, row)
    store.respond_coach_plan = lambda *args, **kw: False
    response = client.post(f"{ASSIGNMENTS}/{row['id']}/activate", headers=AS_FIGHTER, json={})
    assert response.status_code == 409
    plan = next(iter(store.plans.values()))
    assert plan['status'] == 'review_required'
    assert not store.custom_plans[row['id']].get('accepted_at')

    detail = client.get(f"/api/plans/{plan['id']}", headers=AS_FIGHTER)
    assert detail.status_code == 200, detail.text
    assert detail.json()['status'] == 'review_required'
    assert detail.json()['activation_state'] == 'status_ineligible'
    assert detail.json()['structured_card_state']['state'] == 'live'


def test_coach_deletes_only_unsent_drafts(plans):
    client, store, _ = plans
    draft, _ = create(client)
    response = client.delete(f"{BASE}/plans/{draft['id']}", headers=AS_COACH)
    assert response.status_code == 204, response.text
    assert draft['id'] not in store.custom_plans
    assert client.delete(f"{BASE}/plans/{draft['id']}", headers=AS_COACH).status_code == 404
    sent = send(client, create(client)[0])
    assert client.delete(f"{BASE}/plans/{sent['id']}", headers=AS_COACH).status_code == 409
    assert sent['id'] in store.custom_plans
    other, _ = create(client)
    assert client.delete(f"{BASE}/plans/{other['id']}", headers=AS_FIGHTER).status_code in (403, 404)
    assert other['id'] in store.custom_plans


def test_coach_retracts_a_pending_plan_back_to_an_editable_draft(plans):
    client, store, _ = plans
    sent = send(client, create(client)[0])
    assert [row['id'] for row in client.get(ASSIGNMENTS, headers=AS_FIGHTER).json()] == [sent['id']]
    response = client.post(f"{BASE}/plans/{sent['id']}/retract", headers=AS_COACH)
    assert response.status_code == 200, response.text
    assert response.json()['assignment_status'] == 'draft' and response.json()['assigned_at'] is None
    # Gone from the fighter's list, and it can no longer be accepted or declined.
    assert client.get(ASSIGNMENTS, headers=AS_FIGHTER).json() == []
    assert client.post(f"{ASSIGNMENTS}/{sent['id']}/activate", headers=AS_FIGHTER, json={}).status_code in (404, 409)
    assert client.post(f"{ASSIGNMENTS}/{sent['id']}/decline", headers=AS_FIGHTER).status_code in (404, 409)
    assert store.get_active_plan_id(FIGHTER) is None
    # A retry is harmless; the draft is editable, deletable and can be sent again.
    assert client.post(f"{BASE}/plans/{sent['id']}/retract", headers=AS_COACH).status_code == 200
    assert client.put(f"{BASE}/plans/{sent['id']}", headers=AS_COACH, json=body()).status_code == 200
    assert send(client, sent)['assignment_status'] == 'pending'
    assert client.post(f"{BASE}/plans/{sent['id']}/retract", headers=AS_COACH).status_code == 200
    assert client.delete(f"{BASE}/plans/{sent['id']}", headers=AS_COACH).status_code == 204
    assert sent['id'] not in store.custom_plans


def test_an_answered_plan_cannot_be_retracted(plans):
    client, store, _ = plans
    accepted = send(client, create(client)[0])
    assert client.post(f"{ASSIGNMENTS}/{accepted['id']}/activate", headers=AS_FIGHTER, json={}).status_code == 200
    declined = send(client, create(client)[0])
    assert client.post(f"{ASSIGNMENTS}/{declined['id']}/decline", headers=AS_FIGHTER).status_code == 200
    for row in (accepted, declined):
        assert client.post(f"{BASE}/plans/{row['id']}/retract", headers=AS_COACH).status_code == 409
        assert store.custom_plans[row['id']]['assigned_at']
    # The fighter keeps the plan they accepted.
    assert store.get_active_plan_id(FIGHTER) == store.custom_plans[accepted['id']]['published_plan_id']
    # Only the coach who wrote it, with a live connection, can retract.
    pending = send(client, create(client)[0])
    assert client.post(f"{BASE}/plans/{pending['id']}/retract", headers=AS_FIGHTER).status_code in (403, 404)
    assert store.custom_plans[pending['id']]['assigned_at']


def test_coach_removes_answered_plans_from_their_list_but_never_the_active_one(plans):
    client, store, _ = plans
    first = send(client, create(client)[0])
    assert client.post(f"{ASSIGNMENTS}/{first['id']}/activate", headers=AS_FIGHTER, json={}).status_code == 200
    declined = send(client, create(client)[0])
    assert client.post(f"{ASSIGNMENTS}/{declined['id']}/decline", headers=AS_FIGHTER).status_code == 200
    pending = send(client, create(client)[0])

    def coach_list():
        return {row['id']: row['assignment_status'] for row in client.get(f"{BASE}/plans", headers=AS_COACH).json()}

    assert coach_list() == {first['id']: 'active', declined['id']: 'declined', pending['id']: 'pending'}
    # The plan the fighter is training on, and one they have not answered, stay.
    assert client.delete(f"{BASE}/plans/{first['id']}", headers=AS_COACH).status_code == 409
    assert client.delete(f"{BASE}/plans/{pending['id']}", headers=AS_COACH).status_code == 409
    active_before = store.get_active_plan_id(FIGHTER)
    # A declined plan leaves the coach's list; the fighter's record is untouched.
    assert client.delete(f"{BASE}/plans/{declined['id']}", headers=AS_COACH).status_code == 204
    assert declined['id'] not in coach_list() and store.custom_plans[declined['id']]['declined_at']
    assert declined['id'] in [row['id'] for row in client.get(ASSIGNMENTS, headers=AS_FIGHTER).json()]
    assert client.delete(f"{BASE}/plans/{declined['id']}", headers=AS_COACH).status_code == 404
    # Once the fighter moves to another plan, the old one is superseded and removable.
    second = send(client, create(client)[0])
    assert client.post(f"{ASSIGNMENTS}/{second['id']}/activate", headers=AS_FIGHTER, json={'overlap_action': 'replace'}).status_code == 200
    assert coach_list()[first['id']] == 'superseded'
    assert client.delete(f"{BASE}/plans/{first['id']}", headers=AS_COACH).status_code == 204
    assert first['id'] not in coach_list()
    row = store.custom_plans[first['id']]
    assert row['accepted_at'] and row['published_plan_id'] in store.plans
    assert store.get_active_plan_id(FIGHTER) != active_before
    assert store.get_active_plan_id(FIGHTER) == store.custom_plans[second['id']]['published_plan_id']
