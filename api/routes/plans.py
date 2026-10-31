from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import logging
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel

from api.compliance import evaluate_profile_compliance
from api.contracts.training_day import resolve_training_day_str
from api.models import (
    PlanCompletionsResponse,
    PlanDetail,
    PlanRenameRequest,
    PlanSummary,
    ProfileRecord,
    SessionCompletionRecordResponse,
    WeeklySchedule,
)
from api.plan_mappers import (
    _is_admin_archived_hidden_from_athlete,
    _is_archived_plan,
    _is_triage_blocked_plan,
    _lookup_plan_source,
    _map_plan_detail,
    _map_plan_summary,
    _map_weekly_schedule,
)
from api.rehab_labels import resolve_rehab_label_policy
from api.services.exercise_media import attach_exercise_media
from api.services.intake_injury_sync import sync_intake_injuries_for_plan
from api.services.minor_plan_guard import apply_minor_plan_guard
from api.services.plan_safety_copy import clarify_restricted_training_hold
from api.store import AppStore, is_effective_admin_profile
from api.services.active_plan import resolve_active_plan, set_active_plan
from api.services.streaks import reconcile_adherence_streak, reconcile_training_streak
from api.services.plan_intake_injuries import admin_intake_injuries

logger = logging.getLogger(__name__)


class PlanActivationRequest(BaseModel):
    overlap_action: str | None = None


def build_plans_router(*, require_profile, require_plan_row, get_store) -> APIRouter:
    router = APIRouter()
    # Kept in the factory signature for compatibility with create_app wiring.
    # Athlete plan reads below are deliberately scoped by owner instead of using
    # the legacy raw-id dependency, so another athlete's UUID resolves as 404.
    _ = require_plan_row

    def _read_plan_for_viewer(
        plan_id: str,
        *,
        profile: ProfileRecord,
        store: AppStore,
    ) -> dict[str, Any]:
        try:
            uuid.UUID(plan_id)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")

        is_admin = is_effective_admin_profile(profile, store)
        if is_admin:
            plan_row = store.get_plan(plan_id)
        else:
            plan_row = store.get_plan_for_athlete(plan_id, profile.athlete_id)

        # Athletes keep read-only access to their own archived plans (history
        # preview); only plans an admin archived as hidden disappear entirely.
        # This mirrors require_plan_row in api/app.py.
        if not plan_row or (not is_admin and _is_admin_archived_hidden_from_athlete(plan_row)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        return plan_row

    def _rehab_policy_for_plan(
        plan_row: dict[str, Any],
        *,
        profile: ProfileRecord,
        store: AppStore,
        training_day: str,
    ):
        """Synchronize active-plan intake injuries before resolving labels.

        This also repairs already-generated plans when Plan is opened directly,
        without requiring the athlete to visit Today first. Archived and inactive
        plans remain read-only and cannot re-seed historical injuries.
        """
        owner_id = str(plan_row.get("athlete_id") or profile.athlete_id)
        if not evaluate_profile_compliance(profile).health_consent_granted:
            return resolve_rehab_label_policy(store, athlete_id=owner_id)
        try:
            active_plan = resolve_active_plan(
                store,
                owner_id,
                current_training_day=training_day,
            ).plan
            if active_plan and str(active_plan.get("id") or "") == str(
                plan_row.get("id") or ""
            ):
                # resolve_active_plan read this plan with get_plan_for_athlete,
                # so active_plan is already the full owner-scoped row.
                sync_intake_injuries_for_plan(
                    store,
                    athlete_id=owner_id,
                    plan_row=active_plan,
                )
        except Exception:
            # Rehab policy reads have always been best-effort. A synchronization
            # failure must not make the saved plan unavailable.
            pass
        return resolve_rehab_label_policy(store, athlete_id=owner_id)

    @router.get("/api/plans/latest", response_model=PlanDetail)
    def get_latest_plan(
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> PlanDetail:
        training_day = resolve_training_day_str(
            datetime.now(timezone.utc), athlete_timezone=profile.athlete_timezone
        )
        plan_row = resolve_active_plan(
            store,
            profile.athlete_id,
            current_training_day=training_day,
        ).plan
        if not plan_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        is_admin = is_effective_admin_profile(profile, store)
        detail = _map_plan_detail(
            plan_row,
            include_admin=is_admin,
            intake_injuries=admin_intake_injuries(store, plan_row) if is_admin else None,
            plan_source=_lookup_plan_source(store, str(plan_row.get("id") or "")),
            current_training_day=training_day,
            rehab_label_policy=_rehab_policy_for_plan(
                plan_row,
                profile=profile,
                store=store,
                training_day=training_day,
            ),
        )
        return attach_exercise_media(
            apply_minor_plan_guard(
                clarify_restricted_training_hold(detail),
                # The viewer's own age band, not the plan owner's: these routes serve
                # an athlete their own plan. An admin reviewing someone else's plan
                # must see it unscrubbed — they are reviewing what was generated, and
                # an admin has no date of birth on file, which would otherwise
                # fail-safe to "minor" and quietly redact their review view.
                is_minor=profile.role == "athlete" and profile.is_minor,
            ),
            store,
        )

    @router.get("/api/plans/latest/weekly-schedule", response_model=WeeklySchedule)
    def get_latest_weekly_schedule(
        week_index: int = Query(0, ge=0),
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> WeeklySchedule:
        training_day = resolve_training_day_str(
            datetime.now(timezone.utc), athlete_timezone=profile.athlete_timezone
        )
        plan_row = resolve_active_plan(
            store,
            profile.athlete_id,
            current_training_day=training_day,
        ).plan
        if not plan_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        return _map_weekly_schedule(plan_row, week_index=week_index)

    @router.get("/api/plans/active", response_model=PlanSummary)
    def get_active_plan(
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> PlanSummary:
        training_day = resolve_training_day_str(
            datetime.now(timezone.utc), athlete_timezone=profile.athlete_timezone
        )
        plan_row = resolve_active_plan(
            store,
            profile.athlete_id,
            current_training_day=training_day,
        ).plan
        if not plan_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        return _map_plan_summary(plan_row, current_training_day=training_day)

    @router.get("/api/plans", response_model=list[PlanSummary])
    def list_plans(
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> list[PlanSummary]:
        training_day = resolve_training_day_str(
            datetime.now(timezone.utc), athlete_timezone=profile.athlete_timezone
        )
        rows = store.list_user_plans(profile.athlete_id)
        if not is_effective_admin_profile(profile, store):
            rows = [row for row in rows if not _is_triage_blocked_plan(row)]
        return [_map_plan_summary(row, current_training_day=training_day) for row in rows]

    @router.get("/api/plans/{plan_id}", response_model=PlanDetail)
    def get_plan(
        plan_id: str,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> PlanDetail:
        plan_row = _read_plan_for_viewer(plan_id, profile=profile, store=store)
        is_admin = is_effective_admin_profile(profile, store)
        training_day = resolve_training_day_str(
            datetime.now(timezone.utc), athlete_timezone=profile.athlete_timezone
        )
        detail = _map_plan_detail(
            plan_row,
            include_admin=is_admin,
            intake_injuries=admin_intake_injuries(store, plan_row) if is_admin else None,
            plan_source=_lookup_plan_source(store, str(plan_row.get("id") or "")),
            current_training_day=training_day,
            rehab_label_policy=_rehab_policy_for_plan(
                plan_row,
                profile=profile,
                store=store,
                training_day=training_day,
            ),
        )
        return attach_exercise_media(
            apply_minor_plan_guard(
                clarify_restricted_training_hold(detail),
                # The viewer's own age band, not the plan owner's: these routes serve
                # an athlete their own plan. An admin reviewing someone else's plan
                # must see it unscrubbed — they are reviewing what was generated, and
                # an admin has no date of birth on file, which would otherwise
                # fail-safe to "minor" and quietly redact their review view.
                is_minor=profile.role == "athlete" and profile.is_minor,
            ),
            store,
        )

    @router.get("/api/plans/{plan_id}/completions", response_model=PlanCompletionsResponse)
    def get_plan_completions(
        plan_id: str,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> PlanCompletionsResponse:
        plan_row = _read_plan_for_viewer(plan_id, profile=profile, store=store)
        # Completions are athlete-owned rows; even an admin viewing another
        # athlete's plan sees that athlete's logging only via admin surfaces,
        # so this endpoint always reads the caller's own rows.
        rows = store.list_plan_session_completions(
            profile.athlete_id, str(plan_row.get("id") or "")
        )
        return PlanCompletionsResponse(
            completions=[SessionCompletionRecordResponse(**row) for row in rows],
            current_training_day=resolve_training_day_str(
                datetime.now(timezone.utc), athlete_timezone=profile.athlete_timezone
            ),
        )

    @router.get("/api/plans/{plan_id}/weekly-schedule", response_model=WeeklySchedule)
    def get_plan_weekly_schedule(
        plan_id: str,
        week_index: int = Query(0, ge=0),
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> WeeklySchedule:
        plan_row = _read_plan_for_viewer(plan_id, profile=profile, store=store)
        if not is_effective_admin_profile(profile, store) and _is_archived_plan(plan_row):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="weekly schedule not found")
        return _map_weekly_schedule(plan_row, week_index=week_index)

    @router.post("/api/plans/{plan_id}/set-active", response_model=PlanSummary)
    def set_active_user_plan(
        plan_id: str,
        activation: PlanActivationRequest | None = Body(default=None),
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> PlanSummary:
        try:
            uuid.UUID(plan_id)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        training_day = resolve_training_day_str(
            datetime.now(timezone.utc), athlete_timezone=profile.athlete_timezone
        )
        plan_row = set_active_plan(
            store,
            profile.athlete_id,
            plan_id,
            overlap_action=activation.overlap_action if activation else None,
            current_training_day=training_day,
        )
        # Activation can restore an existing plan whose completion history was
        # already present. Rebuild the persisted adherence view from that
        # history; this reconciliation is deliberately XP-free.
        try:
            reconcile_training_streak(
                store,
                athlete_id=profile.athlete_id,
                athlete_timezone=profile.athlete_timezone,
            )
        except Exception:  # noqa: BLE001 - the active pointer remains authoritative
            logger.exception(
                "[streak] activation training reconciliation failed athlete_id=%s plan_id=%s",
                profile.athlete_id,
                plan_id,
            )
        try:
            reconcile_adherence_streak(
                store,
                athlete_id=profile.athlete_id,
                athlete_timezone=profile.athlete_timezone,
            )
        except Exception:  # noqa: BLE001 - the active pointer remains authoritative
            logger.exception(
                "[streak] activation reconciliation failed athlete_id=%s plan_id=%s",
                profile.athlete_id,
                plan_id,
            )
        return _map_plan_summary(plan_row, current_training_day=training_day)

    @router.patch("/api/plans/{plan_id}", response_model=PlanDetail)
    @router.patch("/api/plans/{plan_id}/name", response_model=PlanDetail)
    def rename_plan(
        plan_id: str,
        update: PlanRenameRequest,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> PlanDetail:
        try:
            uuid.UUID(plan_id)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        is_admin = is_effective_admin_profile(profile, store)
        if is_admin:
            plan_row = store.get_plan(plan_id)
            if not plan_row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
            updated = store.rename_plan(plan_id, update.plan_name)
        else:
            # Ownership is enforced by the athlete-scoped store methods: a plan
            # owned by someone else reads back as missing (404).
            plan_row = store.get_plan_for_athlete(plan_id, profile.athlete_id)
            if not plan_row or _is_archived_plan(plan_row):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
            updated = store.rename_plan_for_athlete(plan_id, profile.athlete_id, update.plan_name)
        training_day = resolve_training_day_str(
            datetime.now(timezone.utc), athlete_timezone=profile.athlete_timezone
        )
        detail = _map_plan_detail(
            updated,
            include_admin=is_admin,
            intake_injuries=admin_intake_injuries(store, updated) if is_admin else None,
            plan_source=_lookup_plan_source(store, plan_id),
            current_training_day=training_day,
            rehab_label_policy=_rehab_policy_for_plan(
                updated,
                profile=profile,
                store=store,
                training_day=training_day,
            ),
        )
        return attach_exercise_media(
            apply_minor_plan_guard(
                clarify_restricted_training_hold(detail),
                # The viewer's own age band, not the plan owner's: these routes serve
                # an athlete their own plan. An admin reviewing someone else's plan
                # must see it unscrubbed — they are reviewing what was generated, and
                # an admin has no date of birth on file, which would otherwise
                # fail-safe to "minor" and quietly redact their review view.
                is_minor=profile.role == "athlete" and profile.is_minor,
            ),
            store,
        )

    @router.delete("/api/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
    def archive_user_plan(
        plan_id: str,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> Response:
        try:
            uuid.UUID(plan_id)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        # Ownership is enforced by the athlete-scoped store methods for
        # non-admins; admins intentionally operate on the raw plan id.
        is_admin = is_effective_admin_profile(profile, store)
        if is_admin:
            plan_row = store.get_plan(plan_id)
        else:
            plan_row = store.get_plan_for_athlete(plan_id, profile.athlete_id)
        if not plan_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        if store.has_active_generation_job_for_plan(plan_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Plan has an active generation job. Cancel or wait before archiving.",
            )
        if _is_archived_plan(plan_row):
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        if is_admin:
            store.archive_plan(plan_id)
        else:
            store.archive_plan_for_athlete(plan_id, profile.athlete_id)
            resolve_active_plan(
                store,
                profile.athlete_id,
                current_training_day=resolve_training_day_str(
                    datetime.now(timezone.utc), athlete_timezone=profile.athlete_timezone
                ),
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
