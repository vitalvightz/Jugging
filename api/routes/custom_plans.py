"""Owner-scoped saved custom plans, separate from generated plans."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from api.contracts.custom_plans import CustomDoseSuggestion, CustomPlan, CustomPlanInput, CoachPlan
from api.services.custom_plans import plan_fields, publish_custom_plan, source_suggestion
from api.models import PlanSummary
from api.routes.plans import PlanActivationRequest, reconcile_plan_activation
from api.plan_mappers import _map_plan_summary


def build_custom_plans_router(*, require_profile, get_store):
    router = APIRouter(prefix="/api/custom-plans", tags=["custom plans"])

    def owned(store, athlete_id, plan_id):
        row = store.get_custom_plan(athlete_id, str(plan_id))
        if row is None:
            raise HTTPException(404, "Custom plan not found.")
        return row

    @router.get("", response_model=list[CustomPlan])
    def list_custom_plans(profile=Depends(require_profile), store=Depends(get_store)):
        return store.list_custom_plans(profile.athlete_id)

    @router.post("", response_model=CustomPlan, status_code=201)
    def create_custom_plan(body: CustomPlanInput, profile=Depends(require_profile), store=Depends(get_store)):
        return store.create_custom_plan(profile.athlete_id, plan_fields(store, body, athlete_id=profile.athlete_id))

    def assigned(plan_id: UUID, profile=Depends(require_profile), store=Depends(get_store)):
        if profile.role != 'athlete':
            raise HTTPException(404, 'Assignment not found.')
        row = next((r for r in store.list_plan_assignments(profile.athlete_id) if str(r['id']) == str(plan_id)), None)
        if not row:
            raise HTTPException(404, 'Assignment not found.')
        return row

    @router.get('/assignments', response_model=list[CoachPlan])
    def assignments(profile=Depends(require_profile), store=Depends(get_store)):
        from api.services.coach_plans import assignment_views
        if profile.role != 'athlete':
            return []
        return assignment_views(store, store.list_plan_assignments(profile.athlete_id))

    @router.post('/assignments/{plan_id}/decline', response_model=CoachPlan)
    def decline_assignment(row=Depends(assigned), profile=Depends(require_profile), store=Depends(get_store)):
        from api.services.coach_plans import assignment_view
        if not store.respond_coach_plan(profile.athlete_id, row['id'], 'decline'):
            raise HTTPException(409, 'This assignment has already been accepted.')
        return assignment_view(store, {**row, 'declined_at': True})

    @router.post('/assignments/{plan_id}/activate', response_model=PlanSummary)
    def activate_coach_plan(activation: PlanActivationRequest,
                            row=Depends(assigned), profile=Depends(require_profile), store=Depends(get_store)):
        from api.services.coach_plans import activate_assignment
        from api.contracts.training_day import current_training_day
        day = current_training_day(athlete_timezone=profile.athlete_timezone)
        plan = activate_assignment(store, row, profile, overlap_action=activation.overlap_action, training_day=day)
        reconcile_plan_activation(store, profile, str(plan['id']))
        return _map_plan_summary(plan)

    @router.get("/source-suggestion", response_model=CustomDoseSuggestion)
    def suggest(catalogue_id: str, profile=Depends(require_profile)):
        from api.contracts.custom_plans import CustomDose
        from api.services.exercise_dose import dose_guidance
        from api.services.training_library import cache

        suggestion = source_suggestion(catalogue_id)
        record = cache.get().by_id[catalogue_id]
        suggestion.format_dose = CustomDose.model_validate(dose_guidance(record)[2])
        return suggestion

    @router.get("/{plan_id}", response_model=CustomPlan)
    def read_custom_plan(plan_id: UUID, profile=Depends(require_profile), store=Depends(get_store)):
        return owned(store, profile.athlete_id, plan_id)

    @router.post("/{plan_id}/open-plan", response_model=PlanSummary)
    def publish(plan_id: UUID, profile=Depends(require_profile), store=Depends(get_store)):
        row = owned(store, profile.athlete_id, plan_id)
        # Revalidate saved doses and attachments on publication too.
        body = CustomPlanInput.model_validate({
            **{k: row.get(k) for k in ("schedule_mode", "start_date", "end_date") if row.get(k) is not None},
            "name": row["name"], "sessions": [
                {**{k: s[k] for k in ("id", "name", "weekday")}, "scheduled_date": s.get("scheduled_date"), "session_type": s.get("session_type", "mixed"),
                 "exercises": [{k: v for k, v in e.items() if k not in {"snapshot", "incomplete"}}
                               for e in s["exercises"]]} for s in row["sessions"]
            ],
        })
        fields = plan_fields(store, body, row, athlete_id=profile.athlete_id)
        published = publish_custom_plan(store, {**row, **fields}, profile)
        from api.services.active_plan import resolve_active_plan
        from api.contracts.training_day import current_training_day
        resolve_active_plan(
            store, profile.athlete_id,
            current_training_day=current_training_day(athlete_timezone=profile.athlete_timezone),
        )
        from api.services.tracking_xp import reconcile_training_xp

        reconcile_training_xp(store, profile.athlete_id, athlete_timezone=profile.athlete_timezone)
        return _map_plan_summary(published)

    @router.put("/{plan_id}", response_model=CustomPlan)
    def update_custom_plan(plan_id: UUID, body: CustomPlanInput, profile=Depends(require_profile), store=Depends(get_store)):
        previous = owned(store, profile.athlete_id, plan_id)
        row = store.update_custom_plan(profile.athlete_id, str(plan_id), plan_fields(store, body, previous, athlete_id=profile.athlete_id))
        if row is None:
            raise HTTPException(404, "Custom plan not found.")
        return row

    @router.delete("/{plan_id}", status_code=204)
    def delete_custom_plan(plan_id: UUID, profile=Depends(require_profile), store=Depends(get_store)):
        if not store.delete_custom_plan(profile.athlete_id, str(plan_id)):
            raise HTTPException(404, "Custom plan not found.")
        return Response(status_code=204)

    return router
