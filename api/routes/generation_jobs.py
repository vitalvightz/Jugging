from __future__ import annotations

import re
import uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, status

from api.errors import generation_job_not_found_error
from api.generation_job_helpers import _job_response, resolve_viewer_role
from api.models import GenerationJobResponse, ProfileRecord
from api.store import AppStore, is_effective_admin_profile
from api.store_performance import compact_status_store

Planner = Callable[[dict[str, Any]], dict[str, Any]]

_FAKE_STORE_JOB_ID_PATTERN = re.compile(r"^job_[0-9a-f]{10}$")


def _validate_generation_job_id(job_id: str) -> None:
    try:
        uuid.UUID(job_id)
        return
    except (ValueError, TypeError, AttributeError):
        if _FAKE_STORE_JOB_ID_PATTERN.fullmatch(str(job_id or "")):
            return
        raise generation_job_not_found_error()


def build_generation_jobs_router(
    *,
    require_profile,
    get_store,
    get_planner,
    get_stage2_automator,
    get_active_generation_tasks,
    get_enable_in_process_generation,
    schedule_generation_job_if_needed,
) -> APIRouter:
    router = APIRouter()

    # Plain ``def`` handlers: FastAPI runs them on its threadpool. As ``async def``
    # only the job read was off the event loop; ``_job_response`` then read the
    # plan (and sometimes the latest plan) from Supabase on the loop itself,
    # stalling every other request's start for those round trips.

    @router.get("/api/generation-jobs/active", response_model=GenerationJobResponse | None)
    def get_active_generation_job(
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> GenerationJobResponse | None:
        job = store.get_visible_active_generation_job_status(profile.athlete_id)
        if not job:
            return None
        viewer_role = resolve_viewer_role(profile, is_admin=is_effective_admin_profile(profile, store))
        return _job_response(
            job,
            store=compact_status_store(store),
            viewer_role=viewer_role,
        )

    @router.get("/api/generation-jobs/latest", response_model=GenerationJobResponse | None)
    def get_latest_generation_job(
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> GenerationJobResponse | None:
        job = store.get_latest_generation_job_status(profile.athlete_id)
        if not job:
            return None
        is_admin = is_effective_admin_profile(profile, store)
        if not is_admin and str(job["athlete_id"]) != profile.athlete_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not allowed")
        return _job_response(
            job,
            store=compact_status_store(store),
            viewer_role=resolve_viewer_role(profile, is_admin=is_admin),
        )

    @router.get("/api/generation-jobs/{job_id}", response_model=GenerationJobResponse)
    def get_generation_job(
        job_id: str,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> GenerationJobResponse:
        _validate_generation_job_id(job_id)
        job = store.get_generation_job_status(job_id)
        if not job:
            raise generation_job_not_found_error()
        is_admin = is_effective_admin_profile(profile, store)
        if not is_admin and str(job["athlete_id"]) != profile.athlete_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not allowed")
        return _job_response(
            job,
            store=compact_status_store(store),
            viewer_role=resolve_viewer_role(profile, is_admin=is_admin),
        )

    return router
