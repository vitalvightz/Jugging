"""Admin API routes: plan review, generation-job triage and athlete management."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from api.contracts.clinical_review_capture import (
    ClinicalReviewCaptureRequest, ClinicalReviewCaptureResult, ClinicalReviewLifecycleRequest,
)
from api.services.clinical_review_capture_service import (
    prepare_review_packet, record_clinical_review, record_review_lifecycle,
)

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import ValidationError


from ..dependencies import (
    Planner,
    get_active_generation_tasks,
    get_enable_in_process_generation,
    get_optional_stage2_automator,
    get_planner,
    get_required_stage2_automator,
    get_store,
    require_admin,
)
from ..errors import (
    generation_already_in_flight_error,
    generation_job_not_cancellable_error,
    generation_job_not_found_error,
)
from ..models import (
    ApproveAndResumeGenerationRequest,
    AdminGenerationJobDiagnostic,
    AdminAthleteRecord,
    AdminLatestIntakeUpdateRequest,
    AdminPlanSummary,
    AdminTimeToFirstPlanSummary,
    GenerationJobResponse,
    ManualStage2SubmissionRequest,
    PlanBulkPermanentDeleteRequest,
    PlanBulkPermanentDeleteResult,
    PlanDetail,
    PlanPermanentDeleteRequest,
    PlanRequest,
    ProfileRecord,
)
from ..performance_focus import validate_performance_focus_selections
from ..generation.lazy_scheduler import schedule_generation_job_if_needed
from ..generation.time_utils import utc_now_iso
from ..store import AppStore, is_startup_stale_generation_job
from ..services.admin_stage2_service import (
    approve_review_required_plan as approve_review_required_plan_service,
    backfill_structured_plans as backfill_structured_plans_service,
    list_structured_plan_backfill_candidates as list_structured_plan_backfill_candidates_service,
    prewarm_structured_plan as prewarm_structured_plan_service,
    prepare_structured_plan_rebuild as prepare_structured_plan_rebuild_service,
    run_structured_plan_post_processing as run_structured_plan_post_processing_service,
    should_prewarm_review_plan_row,
    submit_manual_stage2 as submit_manual_stage2_service,
)
from ..services.triage_resume_service import (
    approve_and_resume_job_triage,
    approve_and_resume_plan_triage,
)
from ..cors_config import (
    get_cors_origins as get_cors_origins,
    get_cors_origin_regex as get_cors_origin_regex,
    validate_production_cors_config as validate_production_cors_config,
)
from ..plan_mappers import (
    _is_archived_plan,
    _lookup_plan_source,
    _map_plan_detail,
    _map_admin_plan_summary,
    _map_admin_athlete,
)
from ..generation_job_helpers import (
    _normalized_client_request_id,
    _job_response,
    _job_final_result_triage_status,
    _is_stale_job,
    _generation_job_stale_after_seconds,
    _find_blocking_generation_job_for_athlete,
    _stable_payload_signature,
    _admin_generation_job_diagnostic,
    _triage_job_has_resume_approval as _triage_job_has_resume_approval,
    _triage_plan_has_resume_approval as _triage_plan_has_resume_approval,
)
from ..services.onboarding_metrics import summarize_time_to_first_usable_plan
from ..services.plan_intake_injuries import admin_intake_injuries
from ..settings import env_flag

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def _admin_structured_prewarm_enabled() -> bool:
    return env_flag("APP_ADMIN_STRUCTURED_PREWARM_ENABLED")


def _admin_rejected_result(plan_row: dict[str, Any]) -> dict[str, Any]:
    held_text = str(plan_row.get("final_plan_text") or plan_row.get("draft_plan_text") or plan_row.get("plan_text") or "").strip()
    if not held_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No saved Stage 2 or draft text is available to keep in review.",
        )
    validator_report = plan_row.get("stage2_validator_report") if isinstance(plan_row.get("stage2_validator_report"), dict) else {}
    if not validator_report.get("errors") and not validator_report.get("blocking_warnings"):
        validator_report = {
            **validator_report,
            "warnings": list(validator_report.get("warnings") or []),
            "blocking_warnings": [
                {
                    "code": "admin_review_rejected",
                    "message": "Admin returned this plan to review.",
                    "severity": "blocker",
                }
            ],
        }
    return {
        "status": "review_required",
        "plan_text": "",
        "draft_plan_text": str(plan_row.get("draft_plan_text") or plan_row.get("plan_text") or ""),
        "final_plan_text": held_text,
        "pdf_url": None,
        "stage2_retry_text": str(plan_row.get("stage2_retry_text") or ""),
        "stage2_validator_report": validator_report,
        "stage2_status": "admin_review_rejected",
        "stage2_attempt_count": int(plan_row.get("stage2_attempt_count") or 0),
    }


def _admin_archived_result(plan_row: dict[str, Any]) -> dict[str, Any]:
    archived_text = str(plan_row.get("final_plan_text") or plan_row.get("draft_plan_text") or plan_row.get("plan_text") or "").strip()
    why_log = plan_row.get("why_log") if isinstance(plan_row.get("why_log"), dict) else {}
    return {
        "status": "archived",
        "plan_text": "",
        "draft_plan_text": str(plan_row.get("draft_plan_text") or plan_row.get("plan_text") or ""),
        "final_plan_text": archived_text,
        "why_log": {**why_log, "admin_archived_hidden_from_athlete": True},
        "pdf_url": None,
        "stage2_retry_text": str(plan_row.get("stage2_retry_text") or ""),
        "stage2_validator_report": plan_row.get("stage2_validator_report") or {},
        "stage2_status": "admin_archived",
        "stage2_attempt_count": int(plan_row.get("stage2_attempt_count") or 0),
    }



def build_admin_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/athletes/{athlete_id}/injuries/{injury_id}/episodes/{episode_id}/clinical-review-packet")
    def clinical_review_packet(athlete_id: uuid.UUID, injury_id: uuid.UUID, episode_id: uuid.UUID, criterion_id: str,
                               criterion_version: int = Query(..., ge=1),
                               recorder: ProfileRecord = Depends(require_admin), store: AppStore = Depends(get_store)):
        return prepare_review_packet(store, recorder=recorder, athlete_id=athlete_id, injury_id=injury_id,
            injury_episode_id=episode_id, criterion_id=criterion_id, criterion_version=criterion_version)

    @router.post("/api/admin/clinical-progression-reviews", response_model=ClinicalReviewCaptureResult)
    def capture_clinical_review(body: ClinicalReviewCaptureRequest,
                                 recorder: ProfileRecord = Depends(require_admin), store: AppStore = Depends(get_store)):
        return record_clinical_review(store, recorder=recorder, request=body)

    @router.post("/api/admin/clinical-progression-reviews/lifecycle", response_model=ClinicalReviewCaptureResult)
    def capture_clinical_review_lifecycle(body: ClinicalReviewLifecycleRequest,
                                           recorder: ProfileRecord = Depends(require_admin), store: AppStore = Depends(get_store)):
        return record_review_lifecycle(store, recorder=recorder, request=body)

    @router.get("/api/admin/plans", response_model=list[AdminPlanSummary])
    def list_admin_plans(
        _: ProfileRecord = Depends(require_admin),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        q: str | None = Query(None, max_length=200),
        store: AppStore = Depends(get_store),
    ) -> list[AdminPlanSummary]:
        return [
            _map_admin_plan_summary(row)
            for row in store.list_admin_plans(limit=limit, offset=offset, q=q)
        ]

    @router.get("/api/admin/plans/review", response_model=list[AdminPlanSummary])
    def list_admin_review_plans(
        request: Request,
        background_tasks: BackgroundTasks,
        _: ProfileRecord = Depends(require_admin),
        limit: int = Query(20, ge=1, le=50),
        store: AppStore = Depends(get_store),
    ) -> list[AdminPlanSummary]:
        # Held/blocked plans awaiting an admin decision. Kept separate from the
        # general plan history so a paused plan stays visible even when profile
        # enrichment is degraded.
        rows = store.list_admin_review_plans(limit=limit)
        # Pre-warm is opt-in because it pulls Stage 2 into the web process.
        if _admin_structured_prewarm_enabled():
            stage2 = get_required_stage2_automator(request)
            for row in rows:
                if should_prewarm_review_plan_row(row):
                    background_tasks.add_task(
                        prewarm_structured_plan_service,
                        plan_id=str(row.get("id") or ""),
                        store=store,
                        stage2=stage2,
                    )
        return [_map_admin_plan_summary(row) for row in rows]

    @router.get("/api/admin/generation-jobs/triage", response_model=list[AdminGenerationJobDiagnostic])
    def list_admin_triage_generation_jobs(
        _: ProfileRecord = Depends(require_admin),
        limit: int = Query(20, ge=1, le=50),
        store: AppStore = Depends(get_store),
    ) -> list[AdminGenerationJobDiagnostic]:
        stale_after_seconds = _generation_job_stale_after_seconds()
        diagnostics = [
            _admin_generation_job_diagnostic(job, stale_after_seconds=stale_after_seconds)
            for job in store.list_admin_triage_generation_jobs(limit=limit)
        ]
        return [job for job in diagnostics if job.requires_admin_resume][:limit]

    @router.get("/api/admin/generation-jobs/active", response_model=list[AdminGenerationJobDiagnostic])
    def list_admin_active_generation_jobs(
        _: ProfileRecord = Depends(require_admin),
        limit: int = Query(20, ge=1, le=50),
        store: AppStore = Depends(get_store),
    ) -> list[AdminGenerationJobDiagnostic]:
        stale_after_seconds = _generation_job_stale_after_seconds()
        return [
            _admin_generation_job_diagnostic(job, stale_after_seconds=stale_after_seconds)
            for job in store.list_admin_active_generation_jobs(limit=limit)
        ]

    @router.delete("/api/admin/generation-jobs/{job_id}", response_model=GenerationJobResponse)
    def cancel_admin_generation_job(
        job_id: str,
        _: ProfileRecord = Depends(require_admin),
        store: AppStore = Depends(get_store),
        active_tasks: set[str] = Depends(get_active_generation_tasks),
    ) -> GenerationJobResponse:
        try:
            uuid.UUID(job_id)
        except (ValueError, TypeError, AttributeError):
            if not str(job_id or "").startswith("job_"):
                raise generation_job_not_found_error()

        job = store.get_generation_job(job_id)
        if not job:
            raise generation_job_not_found_error()

        job_status = str(job.get("status") or "").strip().lower()
        if job_status not in {"queued", "running"}:
            raise generation_job_not_cancellable_error(
                "Only queued or running generation jobs can be cancelled."
            )

        now_iso = utc_now_iso()
        updated = store.update_generation_job(
            job_id,
            status="failed",
            error="Generation cancelled by admin.",
            completed_at=now_iso,
            failed_at=now_iso,
            heartbeat_at=now_iso,
        )
        active_tasks.discard(job_id)
        return _job_response(updated, store=store, viewer_role="admin")

    @router.post("/api/admin/generation-jobs/{job_id}/reject", response_model=GenerationJobResponse)
    def reject_triage_generation_job(
        job_id: str,
        profile: ProfileRecord = Depends(require_admin),
        store: AppStore = Depends(get_store),
    ) -> GenerationJobResponse:
        job = store.get_generation_job(job_id)
        if not job:
            raise generation_job_not_found_error()
        if (
            job.get("status") != "review_required"
            or job.get("plan_id")
            or not _job_final_result_triage_status(job)
            or _triage_job_has_resume_approval(job)
        ):
            raise HTTPException(status_code=409, detail="Only unapproved suspended builds can be rejected.")

        now_iso = utc_now_iso()
        final_result = dict(job.get("final_result") or {})
        why_log = dict(final_result.get("why_log") or {})
        why_log["admin_rejection"] = {"rejected_by": profile.athlete_id, "rejected_at": now_iso}
        final_result.update(status="failed", stage2_status="admin_review_rejected", why_log=why_log)
        updated = store.update_generation_job(
            job_id, status="failed", final_result=final_result,
            error="Build rejected by admin.", completed_at=now_iso, failed_at=now_iso,
        )
        return _job_response(updated, store=store, viewer_role="admin")

    @router.post("/api/admin/plans/{plan_id}/manual-stage2", response_model=PlanDetail)
    async def submit_manual_stage2(
        plan_id: str,
        submission: ManualStage2SubmissionRequest,
        _: ProfileRecord = Depends(require_admin),
        store: AppStore = Depends(get_store),
        stage2: Any = Depends(get_required_stage2_automator),
    ) -> PlanDetail:
        return await submit_manual_stage2_service(
            plan_id=plan_id,
            final_plan_text=submission.final_plan_text,
            store=store,
            stage2=stage2,
        )

    @router.post("/api/admin/plans/{plan_id}/approve", response_model=PlanDetail)
    async def approve_review_required_plan(
        plan_id: str,
        background_tasks: BackgroundTasks,
        _: ProfileRecord = Depends(require_admin),
        store: AppStore = Depends(get_store),
        stage2: Any = Depends(get_required_stage2_automator),
    ) -> PlanDetail:
        # Approval is a fast DB-only release so the admin click returns well
        # within the frontend/proxy timeout. Any structured-plan conversion runs
        # after the response is sent, leaving the raw markdown fallback live in
        # the meantime.
        detail = await approve_review_required_plan_service(
            plan_id=plan_id,
            store=store,
            stage2=stage2,
        )
        background_tasks.add_task(
            run_structured_plan_post_processing_service,
            plan_id=plan_id,
            store=store,
            stage2=stage2,
            continue_existing_attempt=True,
        )
        return detail

    @router.post("/api/admin/plans/{plan_id}/structured-plan/rebuild", status_code=202)
    async def rebuild_structured_plan(
        plan_id: str,
        background_tasks: BackgroundTasks,
        _: ProfileRecord = Depends(require_admin),
        store: AppStore = Depends(get_store),
        stage2: Any = Depends(get_required_stage2_automator),
    ) -> dict[str, Any]:
        decision = await prepare_structured_plan_rebuild_service(plan_id=plan_id, store=store)
        if decision["queued"]:
            background_tasks.add_task(
                run_structured_plan_post_processing_service,
                plan_id=plan_id,
                store=store,
                stage2=stage2,
                continue_existing_attempt=True,
                rebuild=True,
            )
        return decision

    @router.post("/api/admin/plans/structured-plan/backfill", status_code=202)
    async def backfill_structured_plans(
        background_tasks: BackgroundTasks,
        _: ProfileRecord = Depends(require_admin),
        store: AppStore = Depends(get_store),
        stage2: Any = Depends(get_required_stage2_automator),
        limit: int = Query(default=25, ge=1, le=200),
    ) -> dict[str, Any]:
        # Find displayable plans with no structured card, then convert them in the
        # background (one model call each) so the admin request returns immediately
        # — the same fast-response/background-conversion contract as approval. Cards
        # appear on the affected plans as each conversion lands.
        plan_ids = await list_structured_plan_backfill_candidates_service(store=store, limit=limit)
        background_tasks.add_task(
            backfill_structured_plans_service,
            store=store,
            stage2=stage2,
            plan_ids=plan_ids,
        )
        return {"queued": len(plan_ids), "plan_ids": plan_ids}

    @router.post("/api/admin/plans/{plan_id}/approve-and-resume-generation", response_model=GenerationJobResponse, status_code=202)
    async def approve_and_resume_generation(
        request: Request,
        plan_id: str,
        approval: ApproveAndResumeGenerationRequest,
        background_tasks: BackgroundTasks,
        profile: ProfileRecord = Depends(require_admin),
        store: AppStore = Depends(get_store),
        planner_fn: Planner = Depends(get_planner),
        stage2: Any = Depends(get_optional_stage2_automator),
        active_tasks: set[str] = Depends(get_active_generation_tasks),
        enable_in_process_generation: bool = Depends(get_enable_in_process_generation),
    ) -> GenerationJobResponse:
        return await approve_and_resume_plan_triage(
            plan_id=plan_id,
            approval=approval,
            background_tasks=background_tasks,
            profile=profile,
            store=store,
            planner_fn=planner_fn,
            stage2=stage2,
            active_tasks=active_tasks,
            enable_in_process_generation=enable_in_process_generation,
        )

    @router.post(
        "/api/admin/generation-jobs/{job_id}/approve-and-resume-generation",
        response_model=GenerationJobResponse,
        status_code=202,
    )
    async def approve_and_resume_generation_from_job(
        request: Request,
        job_id: str,
        approval: ApproveAndResumeGenerationRequest,
        background_tasks: BackgroundTasks,
        profile: ProfileRecord = Depends(require_admin),
        store: AppStore = Depends(get_store),
        planner_fn: Planner = Depends(get_planner),
        stage2: Any = Depends(get_optional_stage2_automator),
        active_tasks: set[str] = Depends(get_active_generation_tasks),
        enable_in_process_generation: bool = Depends(get_enable_in_process_generation),
    ) -> GenerationJobResponse:
        return await approve_and_resume_job_triage(
            job_id=job_id,
            approval=approval,
            background_tasks=background_tasks,
            profile=profile,
            store=store,
            planner_fn=planner_fn,
            stage2=stage2,
            active_tasks=active_tasks,
            enable_in_process_generation=enable_in_process_generation,
        )

    @router.post("/api/admin/plans/{plan_id}/reject", response_model=PlanDetail)
    def reject_approved_plan(
        plan_id: str,
        _: ProfileRecord = Depends(require_admin),
        store: AppStore = Depends(get_store),
    ) -> PlanDetail:
        plan_row = store.get_plan(plan_id)
        if not plan_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")

        updated = store.update_plan_stage2(
            plan_id,
            _admin_rejected_result(plan_row),
        )
        return _map_plan_detail(
            updated,
            include_admin=True,
            intake_injuries=admin_intake_injuries(store, updated),
            plan_source=_lookup_plan_source(store, plan_id),
        )

    @router.post("/api/admin/plans/{plan_id}/archive", response_model=PlanDetail)
    def archive_plan(
        plan_id: str,
        _: ProfileRecord = Depends(require_admin),
        store: AppStore = Depends(get_store),
    ) -> PlanDetail:
        plan_row = store.get_plan(plan_id)
        if not plan_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")

        if store.has_active_generation_job_for_plan(plan_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Plan has an active generation job. Cancel or wait before archiving.",
            )

        updated = store.update_plan_stage2(
            plan_id,
            _admin_archived_result(plan_row),
        )
        return _map_plan_detail(
            updated,
            include_admin=True,
            intake_injuries=admin_intake_injuries(store, updated),
            plan_source=_lookup_plan_source(store, plan_id),
        )

    @router.delete(
        "/api/admin/plans/{plan_id}/permanent",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def permanent_delete_plan(
        plan_id: str,
        request_body: PlanPermanentDeleteRequest | None = Body(default=None),
        _: ProfileRecord = Depends(require_admin),
        store: AppStore = Depends(get_store),
    ) -> Response:
        try:
            uuid.UUID(plan_id)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        plan_row = store.get_plan(plan_id)
        if not plan_row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        if store.has_active_generation_job_for_plan(plan_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Plan has an active generation job. Cancel or wait before deleting.",
            )
        # Archived plans are already retired from the athlete's view, so they can
        # be permanently deleted without retyping the plan name. Live plans still
        # require the typed confirmation to guard against accidental deletion.
        if not _is_archived_plan(plan_row):
            expected = str(plan_row.get("plan_name") or "").strip()
            if not expected:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Plan has no name. Rename it before permanent deletion.",
                )
            confirm = request_body.confirm_plan_name if request_body else None
            if confirm != expected:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Confirmation does not match the plan name.",
                )
        store.delete_plan(plan_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/api/admin/plans/bulk-permanent-delete",
        response_model=PlanBulkPermanentDeleteResult,
    )
    def bulk_permanent_delete_plans(
        request_body: PlanBulkPermanentDeleteRequest,
        _: ProfileRecord = Depends(require_admin),
        store: AppStore = Depends(get_store),
    ) -> PlanBulkPermanentDeleteResult:
        # Bulk deletion is intentionally restricted to already-archived plans so a
        # single confirmation never wipes a live plan. Non-archived ids are
        # reported back as skipped rather than failing the whole batch.
        deleted: list[str] = []
        skipped: list[dict[str, str]] = []
        for plan_id in request_body.plan_ids:
            try:
                uuid.UUID(plan_id)
            except (ValueError, AttributeError):
                skipped.append({"plan_id": plan_id, "reason": "not_found"})
                continue
            plan_row = store.get_plan(plan_id)
            if not plan_row:
                skipped.append({"plan_id": plan_id, "reason": "not_found"})
                continue
            if not _is_archived_plan(plan_row):
                skipped.append({"plan_id": plan_id, "reason": "not_archived"})
                continue
            if store.has_active_generation_job_for_plan(plan_id):
                skipped.append({"plan_id": plan_id, "reason": "active_generation_job"})
                continue
            store.delete_plan(plan_id)
            deleted.append(plan_id)
        return PlanBulkPermanentDeleteResult(
            deleted=deleted,
            skipped=skipped,
            deleted_count=len(deleted),
            skipped_count=len(skipped),
        )

    @router.get("/api/admin/athletes", response_model=list[AdminAthleteRecord])
    def list_admin_athletes(
        _: ProfileRecord = Depends(require_admin),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        q: str | None = Query(None, max_length=200),
        store: AppStore = Depends(get_store),
    ) -> list[AdminAthleteRecord]:
        return [
            _map_admin_athlete(row)
            for row in store.list_admin_athletes(limit=limit, offset=offset, q=q)
        ]

    @router.get("/api/admin/metrics/time-to-first-plan", response_model=AdminTimeToFirstPlanSummary)
    def get_admin_time_to_first_plan(
        _: ProfileRecord = Depends(require_admin),
        days: int = Query(30, ge=1, le=365),
        store: AppStore = Depends(get_store),
    ) -> AdminTimeToFirstPlanSummary:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        rows = store.list_admin_signup_activation(since=since.isoformat())
        return AdminTimeToFirstPlanSummary(
            **summarize_time_to_first_usable_plan(rows, window_days=days, since=since)
        )

    @router.get("/api/admin/athletes/{athlete_id}", response_model=AdminAthleteRecord)
    def get_admin_athlete(
        athlete_id: str,
        _: ProfileRecord = Depends(require_admin),
        store: AppStore = Depends(get_store),
    ) -> AdminAthleteRecord:
        row = store.get_admin_athlete(athlete_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="athlete not found")
        latest_intake = store.get_latest_intake(athlete_id)
        return _map_admin_athlete(row, latest_intake=latest_intake)

    @router.get("/api/admin/athletes/{athlete_id}/generation-jobs", response_model=list[AdminGenerationJobDiagnostic])
    def list_admin_athlete_generation_jobs(
        athlete_id: str,
        _: ProfileRecord = Depends(require_admin),
        limit: int = Query(10, ge=1, le=50),
        store: AppStore = Depends(get_store),
    ) -> list[AdminGenerationJobDiagnostic]:
        row = store.get_admin_athlete(athlete_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="athlete not found")
        jobs = store.list_generation_jobs_for_athlete(athlete_id, limit=limit)
        stale_after_seconds = _generation_job_stale_after_seconds()
        return [_admin_generation_job_diagnostic(job, stale_after_seconds=stale_after_seconds) for job in jobs]

    @router.get("/api/admin/diagnostics/state-integrity")
    def get_admin_state_integrity_diagnostics(
        _: ProfileRecord = Depends(require_admin),
        limit: int = Query(500, ge=1, le=5000),
        store: AppStore = Depends(get_store),
    ) -> dict[str, Any]:
        orphaned_terminal_jobs = store.list_orphaned_terminal_generation_jobs(limit=limit)
        failed_resume_with_approved_marker = store.list_failed_triage_resume_jobs_with_approved_marker(limit=limit)

        return {
            "limit": limit,
            "orphaned_terminal_jobs": orphaned_terminal_jobs,
            "failed_resume_with_approved_marker": failed_resume_with_approved_marker,
            "orphaned_terminal_job_count": len(orphaned_terminal_jobs),
            "failed_resume_with_approved_marker_count": len(failed_resume_with_approved_marker),
        }

    @router.post("/api/admin/athletes/{athlete_id}/plans/generate-from-latest-intake", response_model=GenerationJobResponse, status_code=202)
    async def generate_admin_athlete_plan_from_latest_intake(
        request: Request,
        athlete_id: str,
        background_tasks: BackgroundTasks,
        _: ProfileRecord = Depends(require_admin),
        store: AppStore = Depends(get_store),
        planner_fn: Planner = Depends(get_planner),
        stage2: Any = Depends(get_optional_stage2_automator),
        active_tasks: set[str] = Depends(get_active_generation_tasks),
        enable_in_process_generation: bool = Depends(get_enable_in_process_generation),
    ) -> GenerationJobResponse:
        row = await asyncio.to_thread(store.get_admin_athlete, athlete_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="athlete not found")
        latest_intake = await asyncio.to_thread(store.get_latest_intake, athlete_id)
        if not latest_intake or not isinstance(latest_intake.get("intake"), dict):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="latest intake not found for athlete",
            )
        latest_intake_athlete_id = str(latest_intake.get("athlete_id") or "").strip()
        latest_intake_id = str(latest_intake.get("id") or "").strip() or None
        if latest_intake_athlete_id != athlete_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="latest intake belongs to a different athlete",
            )
        if not latest_intake_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="latest intake is missing id",
            )
        try:
            request_body = PlanRequest.model_validate(latest_intake["intake"])
        except ValidationError as exc:
            logger.warning(
                "[admin] generate_from_latest_intake:invalid_intake athlete_id=%s error_code=%s validation_error_count=%s",
                athlete_id,
                "invalid_intake",
                len(exc.errors()),
                extra={
                    "athlete_id": athlete_id,
                    "status": status.HTTP_409_CONFLICT,
                    "error_code": "invalid_intake",
                },
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="latest intake is invalid and cannot be used for generation",
            ) from exc
        focus_validation = validate_performance_focus_selections(
            request_body.effective_fight_date,
            key_goals=request_body.key_goals,
            weak_areas=request_body.weak_areas,
            time_zone=request_body.athlete.athlete_timezone,
        )
        if focus_validation.is_over_cap:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=focus_validation.error_message or "Too many focus selections for this camp.",
            )
        client_request_id = _normalized_client_request_id(
            request.headers.get("X-Client-Request-Id"),
            "cli",
        )
        stale_after_seconds = _generation_job_stale_after_seconds()
        request_payload = request_body.model_dump(mode="json")
        existing_job = await asyncio.to_thread(
            store.get_generation_job_by_client_request_id,
            athlete_id=athlete_id,
            client_request_id=client_request_id,
        )
        if existing_job:
            existing_source = str(existing_job.get("source") or "").strip()
            existing_intake_id = str(existing_job.get("intake_id") or "").strip() or None
            existing_payload = existing_job.get("request_payload")
            has_safe_linkage = (
                existing_source == "admin_latest_intake"
                and existing_intake_id == latest_intake_id
                and isinstance(existing_payload, dict)
                and _stable_payload_signature(existing_payload) == _stable_payload_signature(request_payload)
            )
            is_startup_stale = is_startup_stale_generation_job(existing_job, stale_after_seconds=stale_after_seconds)
            if not has_safe_linkage and not is_startup_stale:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="unsafe existing admin generation job linkage",
                )
            if is_startup_stale:
                existing_job = await asyncio.to_thread(
                    store.create_or_get_generation_job,
                    athlete_id=athlete_id,
                    client_request_id=client_request_id,
                    source="admin_latest_intake",
                    request_payload=request_payload,
                    intake_id=latest_intake_id,
                    stale_after_seconds=stale_after_seconds,
                )
                existing_payload_after_reset = existing_job.get("request_payload")
                if (
                    str(existing_job.get("source") or "").strip() != "admin_latest_intake"
                    or str(existing_job.get("intake_id") or "").strip() != (latest_intake_id or "")
                    or not isinstance(existing_payload_after_reset, dict)
                    or _stable_payload_signature(existing_payload_after_reset) != _stable_payload_signature(request_payload)
                ):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="unsafe existing admin generation job linkage",
                    )
            job = await schedule_generation_job_if_needed(
                job=existing_job,
                background_tasks=background_tasks,
                store=store,
                planner_fn=planner_fn,
                stage2=stage2,
                active_tasks=active_tasks,
                enable_in_process_generation=enable_in_process_generation,
                stale_job_checker=_is_stale_job,
                stale_after_seconds=stale_after_seconds,
            )
            return await asyncio.to_thread(_job_response, job, store=store, viewer_role="admin")
        blocking_job = await asyncio.to_thread(
            _find_blocking_generation_job_for_athlete,
            store=store,
            athlete_id=athlete_id,
            stale_after_seconds=stale_after_seconds,
        )
        if blocking_job:
            raise generation_already_in_flight_error()
        job = await asyncio.to_thread(
            store.create_or_get_generation_job,
            athlete_id=athlete_id,
            client_request_id=client_request_id,
            source="admin_latest_intake",
            request_payload=request_payload,
            intake_id=latest_intake_id,
            stale_after_seconds=stale_after_seconds,
        )
        job = await schedule_generation_job_if_needed(
            job=job,
            background_tasks=background_tasks,
            store=store,
            planner_fn=planner_fn,
            stage2=stage2,
            active_tasks=active_tasks,
            enable_in_process_generation=enable_in_process_generation,
            stale_job_checker=_is_stale_job,
            stale_after_seconds=stale_after_seconds,
        )
        return await asyncio.to_thread(_job_response, job, store=store, viewer_role="admin")

    @router.patch("/api/admin/athletes/{athlete_id}/latest-intake", response_model=AdminAthleteRecord)
    def update_admin_athlete_latest_intake(
        athlete_id: str,
        update: AdminLatestIntakeUpdateRequest,
        _: ProfileRecord = Depends(require_admin),
        store: AppStore = Depends(get_store),
    ) -> AdminAthleteRecord:
        row = store.get_admin_athlete(athlete_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="athlete not found")
        latest_intake = store.get_latest_intake(athlete_id)
        if not latest_intake or not isinstance(latest_intake.get("intake"), dict):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="latest intake not found for athlete")
        if str(latest_intake.get("athlete_id") or "").strip() != athlete_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="latest intake belongs to a different athlete")
        latest_intake_id = str(latest_intake.get("id") or "").strip()
        if not latest_intake_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="latest intake is missing id")
        merged = dict(latest_intake["intake"])
        for field in ("fight_date", "no_scheduled_fight", "rounds_format", "weekly_training_frequency", "training_availability", "equipment_access", "key_goals", "weak_areas", "injuries"):
            if field in update.model_fields_set:
                merged[field] = getattr(update, field)
        try:
            request_body = PlanRequest.model_validate(merged)
        except ValidationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors()) from exc
        focus_validation = validate_performance_focus_selections(
            request_body.effective_fight_date,
            key_goals=request_body.key_goals,
            weak_areas=request_body.weak_areas,
            time_zone=request_body.athlete.athlete_timezone,
        )
        if focus_validation.is_over_cap:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=focus_validation.error_message or "Too many focus selections for this camp.")
        if request_body.weekly_training_frequency and request_body.weekly_training_frequency > len(request_body.training_availability):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="weekly_training_frequency cannot exceed selected training_availability days")
        refreshed = store.update_intake(
            latest_intake_id,
            intake=request_body.model_dump(mode="json"),
            fight_date=request_body.effective_fight_date.strip() or None,
            technical_style=list(request_body.athlete.technical_style),
        )
        return _map_admin_athlete(row, latest_intake=refreshed)

    return router
