"""Coach drafts use the athlete builder/compiler and the same activation rules."""
from fastapi import HTTPException
from api.contracts.custom_plans import CustomPlanInput
from api.services.active_plan import set_active_plan
from api.services.custom_plans import plan_fields, publish_custom_plan, custom_open_plan_result


def coach_plan_fields(store, body, previous=None, *, coach_id):
    # Private attachments belong to the author and cannot be transferred to a
    # fighter. Public catalogue/community revisions and manual work are shared.
    for session in body.sessions:
        for exercise in session.exercises:
            if getattr(exercise, 'media_id', None) or getattr(exercise, 'community_id', None):
                raise HTTPException(422, 'Use a public exercise or remove private attachments before sharing a plan.')
    return plan_fields(store, body, previous, athlete_id=coach_id)


def saved_input(row):
    return CustomPlanInput.model_validate({
        **{k: row.get(k) for k in ('schedule_mode', 'start_date', 'end_date') if row.get(k) is not None},
        'name': row['name'], 'sessions': [
            {**{k: s[k] for k in ('id', 'name', 'weekday')}, 'scheduled_date': s.get('scheduled_date'), 'session_type': s.get('session_type', 'mixed'), 'notes': s.get('notes'), 'programme_week': s.get('programme_week'),
             'exercises': [{k: v for k, v in e.items() if k not in {'snapshot', 'incomplete'}} for e in s['exercises']]}
            for s in row['sessions']],
    })


def assignment_view(store, row):
    active = store.get_active_plan_id(row['fighter_id']) if row.get('accepted_at') else None
    return _with_status(row, active)


def assignment_views(store, rows):
    """``assignment_view`` for a list, reading each fighter's active plan once.

    Each accepted row needs its fighter's active-plan pointer. A list is one
    fighter's assignments, so reading it per row repeated the same round trip.
    """
    pointers = {}
    views = []
    for row in rows:
        active = None
        if row.get('accepted_at'):
            if row['fighter_id'] not in pointers:
                pointers[row['fighter_id']] = store.get_active_plan_id(row['fighter_id'])
            active = pointers[row['fighter_id']]
        views.append(_with_status(row, active))
    return views


def _with_status(row, active):
    return {**row, 'assignment_status': (
        'active' if row.get('accepted_at') and row.get('published_plan_id') and str(active) == str(row.get('published_plan_id')) else
        'superseded' if row.get('accepted_at') else 'declined' if row.get('declined_at') else
        'pending' if row.get('assigned_at') else 'draft')}


def activate_assignment(store, row, profile, *, overlap_action, training_day):
    if row.get('declined_at'):
        raise HTTPException(409, 'This assignment was declined.')
    if row.get('accepted_at'):
        accepted = store.get_plan_for_athlete(row['published_plan_id'], profile.athlete_id) if row.get('published_plan_id') else None
        if not accepted:
            raise HTTPException(404, 'The accepted training plan has been removed.')
        return accepted
    connection = store.get_accepted_coach_connection(row['athlete_id'], profile.athlete_id)
    if not connection or connection.get('status') != 'accepted':
        raise HTTPException(409, 'This coach connection has ended.')
    # An unaccepted executable row is ineligible for ALL normal activation paths,
    # including the sole-plan fallback. Only the transaction below releases it.
    from api.compliance_guards import require_onboarding_compliance
    require_onboarding_compliance(profile)
    result = custom_open_plan_result(row, profile, training_day=str(training_day))
    candidate = {**result, 'id': result['plan_id'], 'athlete_id': profile.athlete_id,
                 'fight_date': None, 'created_at': str(training_day)}
    expected = store.get_active_plan_id(profile.athlete_id)

    class ConsentActivation:
        def __getattr__(self, name):
            return getattr(store, name)

        def get_plan_for_athlete(self, plan_id, athlete_id):
            return candidate if plan_id == candidate['id'] else store.get_plan_for_athlete(plan_id, athlete_id)

        def set_active_plan_id(self, athlete_id, plan_id):
            publish_custom_plan(store, row, profile, awaiting_assignment=True)
            if not store.respond_coach_plan(athlete_id, row['id'], 'activate', plan_id=plan_id,
                                            expected_id=expected, replace=overlap_action == 'replace'):
                raise HTTPException(409, 'The assignment or active plan changed. Refresh and try again.')

        def archive_plan_for_athlete(self, plan_id, athlete_id):
            # The consent transaction already archived the previous plan.
            return store.get_plan_for_athlete(plan_id, athlete_id)

    set_active_plan(ConsentActivation(), profile.athlete_id, candidate['id'],
                           overlap_action=overlap_action, current_training_day=training_day)
    return store.get_plan_for_athlete(candidate['id'], profile.athlete_id)
