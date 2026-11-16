"""Authored dates must survive drafts, consent and the shared execution paths."""
from copy import deepcopy
from datetime import date, timedelta
from uuid import uuid4

import pytest
from tests.test_coach_plans import plans, camp  # noqa: F401
from pydantic import ValidationError
from api.contracts.custom_plans import CustomPlanInput
from api.services.today_service import resolve_training_day
from api.services.active_plan import get_plan_activation_state, plan_date_range
from tests.test_custom_open_plans import api, AUTH, body, checkin, complete  # noqa: F401 - shared fixture


def dated_body():
    start = date.fromisoformat(resolve_training_day(None))
    original = body(start.strftime('%A'))
    session = original['sessions'][0]
    session['scheduled_date'] = str(start)
    second = deepcopy(session)
    second['id'] = str(uuid4())
    second['exercises'][0]['id'] = str(uuid4())
    second['scheduled_date'] = str(start + timedelta(days=7))
    second['name'] = 'Second week strength'
    second['exercises'][0]['dose']['sets'] = 6
    return {'name': 'Authored fortnight', 'schedule_mode': 'dated', 'start_date': str(start),
            'end_date': str(start+timedelta(days=13)), 'sessions': [session, second]}


def test_dated_draft_publishes_distinct_dates_and_doses_without_repeating(api):  # noqa: F811
    client, store = api
    payload = dated_body()
    created = client.post('/api/custom-plans', headers=AUTH, json=payload)
    assert created.status_code == 201, created.text
    row = created.json()
    assert row['schedule_mode'] == 'dated' and row['start_date'] == payload['start_date']
    published = client.post(f"/api/custom-plans/{row['id']}/open-plan", headers=AUTH)
    assert published.status_code == 200, published.text
    plan = store.plans[published.json()['plan_id']]
    assert 'open_plan_spec' not in plan['planning_brief']
    assert plan['structured_plan']['plan_metadata']['plan_type'] == 'general_performance'
    days = [d for w in plan['structured_plan']['weeks'] for d in w['days']]
    assert len(days) == 14
    sessions = [(d['date'], s) for d in days for s in d['sessions']]
    assert [day for day, _ in sessions] == [s['scheduled_date'] for s in payload['sessions']]
    assert [s['blocks'][0]['sets'] for _, s in sessions] == [3, 6]
    assert len({s['session_id'] for _, s in sessions}) == 2
    bounds = plan_date_range(plan)
    assert str(bounds.start) == payload['start_date'] and str(bounds.end) == payload['end_date']
    assert get_plan_activation_state(plan, current_training_day=payload['end_date']) == 'eligible'
    assert get_plan_activation_state(plan, current_training_day=str(date.fromisoformat(payload['end_date'])+timedelta(days=1))) == 'fight_date_passed'
    retry = client.post(f"/api/custom-plans/{row['id']}/open-plan", headers=AUTH)
    assert retry.json()['plan_id'] == plan['id'] and len(store.plans) == 1
    activation = client.post(f"/api/plans/{plan['id']}/set-active", headers=AUTH, json={})
    assert activation.status_code == 200, activation.text
    checkin(client, plan['id'])
    today = client.get('/api/today', headers=AUTH)
    assert today.status_code == 200, today.text
    assert 'Second week strength' not in today.text
    complete(client, plan['id'], sessions[0][1]['session_id'], 'done')
    history = client.get('/api/today/session-completions', headers=AUTH)
    assert history.status_code == 200 and sessions[0][1]['session_id'] in history.text



@pytest.mark.parametrize('problem', ['outside', 'missing', 'too_long', 'duplicate_ids', 'weekly_dates'])
def test_invalid_calendar_data_cannot_enter_the_draft(problem):
    payload = dated_body()
    if problem == 'outside':
        payload['sessions'][0]['scheduled_date'] = '2000-01-01'
    elif problem == 'missing':
        payload['sessions'][0]['scheduled_date'] = None
    elif problem == 'too_long':
        payload['end_date'] = str(date.fromisoformat(payload['start_date']) + timedelta(days=84))
    elif problem == 'duplicate_ids':
        payload['sessions'][1]['id'] = payload['sessions'][0]['id']
    else:
        payload['schedule_mode'] = 'weekly'
    with pytest.raises(ValidationError):
        CustomPlanInput.model_validate(payload)


def test_weekday_is_derived_from_the_date_and_legacy_defaults_remain_weekly():
    payload = dated_body()
    payload['sessions'][0]['weekday'] = 'Friday'
    parsed = CustomPlanInput.model_validate(payload)
    assert parsed.sessions[0].weekday == date.fromisoformat(payload['start_date']).strftime('%A')
    legacy = CustomPlanInput.model_validate(body())
    assert legacy.schedule_mode == 'weekly' and legacy.start_date is None


def test_coach_dated_assignment_and_revision_keep_the_original_fighter_owned(plans):  # noqa: F811
    from tests.test_coach_plans import create, send, AS_FIGHTER, ASSIGNMENTS
    from tests.test_coach_fighter_access import FIGHTER
    client, store, _ = plans
    original, _ = create(client, dated_body())
    send(client, original)
    active = client.post(f"{ASSIGNMENTS}/{original['id']}/activate", headers=AS_FIGHTER, json={})
    assert active.status_code == 200, active.text
    old_id = active.json()['plan_id']
    before = deepcopy(store.plans[old_id])
    revised_body = dated_body()
    revised_body['name'] = 'Revised fortnight'
    revision, _ = create(client, revised_body)
    send(client, revision)
    assert store.get_active_plan_id(FIGHTER) == old_id
    assert store.plans[old_id] == before
    assert client.post(f"{ASSIGNMENTS}/{revision['id']}/activate", headers=AS_FIGHTER, json={'overlap_action': 'pause'}).status_code == 200
    assert store.get_active_plan_id(FIGHTER) != old_id and store.plans[old_id] == before


def test_weekly_publication_identity_ignores_new_null_calendar_fields(api):  # noqa: F811
    from api.plan_mappers import _map_profile_row
    from api.services.custom_plans import custom_open_plan_result
    _, store = api
    profile = _map_profile_row(store.get_profile('athlete-1'))
    legacy = {**CustomPlanInput.model_validate(body()).model_dump(mode='json'), 'id': str(uuid4())}
    without_dates = deepcopy(legacy)
    for session in without_dates['sessions']:
        session.pop('scheduled_date')
    day = resolve_training_day(None)
    assert custom_open_plan_result(legacy, profile, training_day=day)['plan_id'] == custom_open_plan_result(without_dates, profile, training_day=day)['plan_id']


def test_dated_recovery_days_keep_their_authored_type(api):  # noqa: F811
    from api.plan_mappers import _map_profile_row
    from api.services.custom_plans import custom_open_plan_result
    _, store = api
    payload = dated_body()
    recovery_date = str(date.fromisoformat(payload['start_date']) + timedelta(days=1))
    payload['sessions'].append({'id': str(uuid4()), 'name': 'Easy recovery', 'session_type': 'recovery',
                                'scheduled_date': recovery_date, 'exercises': []})
    draft = {**CustomPlanInput.model_validate(payload).model_dump(mode='json'), 'id': str(uuid4())}
    result = custom_open_plan_result(draft, _map_profile_row(store.get_profile('athlete-1')), training_day=payload['start_date'])
    days = [day for week in result['structured_plan']['weeks'] for day in week['days']]
    recovery = next(day for day in days if day['date'] == recovery_date)
    assert recovery['day_type'] == 'recovery' and recovery['today_card']['headline'] == 'Recovery day'
    assert not recovery['sessions']
