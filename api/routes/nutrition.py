from __future__ import annotations

from api.contracts.training_day import current_training_day
from fastapi import APIRouter, Depends, HTTPException, status

from api.compliance_guards import require_health_feature_access
from api.models import NutritionWorkspaceState, NutritionWorkspaceUpdateRequest, ProfileRecord, ProfileUpdateRequest
from api.nutrition_workspace import (
    build_nutrition_workspace,
    future_weigh_ins,
    latest_calendar_day,
    merge_workspace_into_payload,
    normalize_nutrition_update_request,
    sync_current_weight_with_log,
)
from api.plan_mappers import _map_admin_athlete
from api.services.active_plan import resolve_active_plan
from api.store import AppStore


def _sync_weight_from_log(*, update, previous_log, today):
    """Reject new future-dated weights, then let the newest logged weight set camp weight."""
    if future_weigh_ins(update=update, previous_log=previous_log, today=today):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A weight can't be dated in the future.",
        )
    return sync_current_weight_with_log(update=update, previous_log=previous_log, today=today)


def _active_plan_fight_date(store: AppStore, profile: ProfileRecord) -> str | None:
    plan = resolve_active_plan(
        store,
        profile.athlete_id,
        current_training_day=current_training_day(athlete_timezone=profile.athlete_timezone),
    ).plan
    return str(plan.get("fight_date") or "") if plan else None


def build_nutrition_router(
    *,
    require_profile,
    require_admin,
    get_store,
    validate_schedule_consistency,
    validate_session_type_consistency,
    update_profile_with_nutrition_fallback,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/nutrition/current", response_model=NutritionWorkspaceState)
    def get_nutrition_current(
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> NutritionWorkspaceState:
        latest_intake = store.get_latest_intake(profile.athlete_id)
        return build_nutrition_workspace(
            profile=profile,
            latest_intake_row=latest_intake,
            active_plan_fight_date=_active_plan_fight_date(store, profile),
        )

    @router.put("/api/nutrition/current", response_model=NutritionWorkspaceState)
    def update_nutrition_current(
        update: NutritionWorkspaceUpdateRequest,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> NutritionWorkspaceState:
        # Bodyweight, target weight, appetite and supplement use are health-
        # inference data (docs/data-map-processor-register.md), so writing them
        # needs current explicit consent. The GET above is left ungated: reading
        # back data already collected lawfully is how withdrawal degrades safely
        # instead of locking an athlete out of their own record.
        require_health_feature_access(profile)
        if profile.is_minor:
            # Same data-minimisation rule as the intake: no weight-cut feature
            # for an under-18, so no target weight to collect for it. Applied to
            # the validated model rather than the raw body so the shape stays
            # exactly what the rest of this handler expects.
            update = update.model_copy(
                update={
                    "shared_camp_context": update.shared_camp_context.model_copy(
                        update={"target_weight_kg": None, "target_weight_range_kg": None}
                    )
                }
            )
        latest_intake = store.get_latest_intake(profile.athlete_id)
        active_plan_fight_date = _active_plan_fight_date(store, profile)
        current_workspace = build_nutrition_workspace(
            profile=profile, latest_intake_row=latest_intake,
            active_plan_fight_date=active_plan_fight_date,
        )
        update = update.model_copy(update={"nutrition_coach_controls": current_workspace.nutrition_coach_controls})
        normalized_update = normalize_nutrition_update_request(
            update=update,
            existing_shared_camp_context=current_workspace.shared_camp_context,
        )
        validate_schedule_consistency(normalized_update)
        validate_session_type_consistency(normalized_update)
        normalized_update, weight_from_log = _sync_weight_from_log(
            update=normalized_update,
            previous_log=current_workspace.nutrition_monitoring.daily_bodyweight_log,
            today=latest_calendar_day(),
        )

        merged_payload = merge_workspace_into_payload(
            base_payload=(
                profile.onboarding_draft
                if current_workspace.source == "draft" and isinstance(profile.onboarding_draft, dict)
                else latest_intake.get("intake")
                if current_workspace.source == "intake" and isinstance(latest_intake, dict)
                else {}
            ),
            workspace=normalized_update,
            profile=profile,
            weight_from_log=weight_from_log,
        )

        if current_workspace.source == "intake" and current_workspace.intake_id:
            updated_profile = update_profile_with_nutrition_fallback(
                store=store,
                athlete_id=profile.athlete_id,
                update=ProfileUpdateRequest(nutrition_profile=normalized_update.nutrition_profile),
            )
            store.update_intake(
                current_workspace.intake_id,
                intake=merged_payload,
                fight_date=normalized_update.shared_camp_context.fight_date or None,
                technical_style=list(merged_payload.get("athlete", {}).get("technical_style") or updated_profile.technical_style),
            )
            refreshed_intake = store.get_latest_intake(profile.athlete_id)
            return build_nutrition_workspace(
                profile=updated_profile, latest_intake_row=refreshed_intake,
                active_plan_fight_date=active_plan_fight_date,
            )

        updated_profile = update_profile_with_nutrition_fallback(
            store=store,
            athlete_id=profile.athlete_id,
            update=ProfileUpdateRequest(
                nutrition_profile=normalized_update.nutrition_profile,
                onboarding_draft=merged_payload,
            ),
        )
        refreshed_intake = store.get_latest_intake(profile.athlete_id)
        return build_nutrition_workspace(
            profile=updated_profile, latest_intake_row=refreshed_intake,
            active_plan_fight_date=active_plan_fight_date,
        )

    @router.get("/api/admin/athletes/{athlete_id}/nutrition/current", response_model=NutritionWorkspaceState)
    def get_admin_athlete_nutrition_current(
        athlete_id: str,
        _: ProfileRecord = Depends(require_admin),
        store: AppStore = Depends(get_store),
    ) -> NutritionWorkspaceState:
        row = store.get_admin_athlete(athlete_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="athlete not found")
        latest_intake = store.get_latest_intake(athlete_id)
        athlete = _map_admin_athlete(row, latest_intake=latest_intake)
        return build_nutrition_workspace(profile=athlete, latest_intake_row=latest_intake)

    @router.put("/api/admin/athletes/{athlete_id}/nutrition/current", response_model=NutritionWorkspaceState)
    def update_admin_athlete_nutrition_current(
        athlete_id: str,
        update: NutritionWorkspaceUpdateRequest,
        _: ProfileRecord = Depends(require_admin),
        store: AppStore = Depends(get_store),
    ) -> NutritionWorkspaceState:
        row = store.get_admin_athlete(athlete_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="athlete not found")

        latest_intake = store.get_latest_intake(athlete_id)
        athlete = _map_admin_athlete(row, latest_intake=latest_intake)
        current_workspace = build_nutrition_workspace(profile=athlete, latest_intake_row=latest_intake)
        if "nutrition_coach_controls" not in update.model_fields_set:
            update = update.model_copy(update={"nutrition_coach_controls": current_workspace.nutrition_coach_controls})
        normalized_update = normalize_nutrition_update_request(
            update=update,
            existing_shared_camp_context=current_workspace.shared_camp_context,
        )
        validate_schedule_consistency(normalized_update)
        validate_session_type_consistency(normalized_update)
        normalized_update, weight_from_log = _sync_weight_from_log(
            update=normalized_update,
            previous_log=current_workspace.nutrition_monitoring.daily_bodyweight_log,
            today=latest_calendar_day(),
        )

        merged_payload = merge_workspace_into_payload(
            base_payload=(
                athlete.onboarding_draft
                if current_workspace.source == "draft" and isinstance(athlete.onboarding_draft, dict)
                else latest_intake.get("intake")
                if current_workspace.source == "intake" and isinstance(latest_intake, dict)
                else {}
            ),
            workspace=normalized_update,
            profile=athlete,
            weight_from_log=weight_from_log,
        )

        if current_workspace.source == "intake" and current_workspace.intake_id:
            updated_profile = update_profile_with_nutrition_fallback(
                store=store,
                athlete_id=athlete_id,
                update=ProfileUpdateRequest(nutrition_profile=normalized_update.nutrition_profile),
            )
            store.update_intake(
                current_workspace.intake_id,
                intake=merged_payload,
                fight_date=normalized_update.shared_camp_context.fight_date or None,
                technical_style=list(merged_payload.get("athlete", {}).get("technical_style") or updated_profile.technical_style),
            )
            refreshed_intake = store.get_latest_intake(athlete_id)
            return build_nutrition_workspace(profile=updated_profile, latest_intake_row=refreshed_intake)

        updated_profile = update_profile_with_nutrition_fallback(
            store=store,
            athlete_id=athlete_id,
            update=ProfileUpdateRequest(
                nutrition_profile=normalized_update.nutrition_profile,
                onboarding_draft=merged_payload,
            ),
        )
        refreshed_intake = store.get_latest_intake(athlete_id)
        return build_nutrition_workspace(profile=updated_profile, latest_intake_row=refreshed_intake)

    return router
