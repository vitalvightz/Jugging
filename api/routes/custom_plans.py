"""Owner-scoped saved custom plans, separate from generated plans."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from api.contracts.custom_plans import CustomDoseSuggestion, CustomPlan, CustomPlanInput
from api.services.custom_plans import plan_fields, publish_custom_plan, source_suggestion
from api.models import PlanSummary
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
            "name": row["name"], "sessions": [
                {**{k: s[k] for k in ("id", "name", "weekday")}, "session_type": s.get("session_type", "mixed"),
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
