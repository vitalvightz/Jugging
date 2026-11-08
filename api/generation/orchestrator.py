"""Top-level generation job orchestrator.

``run_generation_job`` claims a queued job, runs Stage 1 (subprocess planner),
optionally runs Stage 2 finalization, and persists the terminal outcome. The
heavy lifting lives in the sibling modules (admin_linkage, persistence,
stage1_runner, stage2_runner, triage, milestones, heartbeat); this module wires
them together and owns the exception-to-job-status failure handling.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import traceback
from contextlib import suppress
from typing import Any, Callable

from fastapi import HTTPException, status

from ..compliance import evaluate_profile_compliance, profile_age_years
from ..error_sanitizer import sanitize_error_text
from ..generation_health import (
    NON_HEALTH_GENERATION_MODE,
    NON_HEALTH_GENERATION_MODE_KEY,
    build_non_health_generation_payload,
    non_health_planner_payload,
)
from ..minor_safety import minor_safe_stage1_payload
from ..services.rehab_stage_snapshot import annotate_payload_with_rehab_stage
from ..services.sparring_readiness_snapshot import annotate_payload_with_sparring_readiness
from ..models import (
    PROFILE_REFRESH_FAILED_WARNING as _PROFILE_REFRESH_FAILED_WARNING,
    PROFILE_REFRESH_FAILED_WARNING_CODE as _PROFILE_REFRESH_FAILED_WARNING_CODE,
    PROFILE_REFRESH_FAILED_WHY_LOG_KEY as _PROFILE_REFRESH_FAILED_WHY_LOG_KEY,
    ProfileUpdateRequest,
)
from ..stage2_automation import (
    Stage2AutomationError,
    Stage2AutomationUnavailableError,
    Stage2Automator,
)
from ..store import AppStore, generation_job_remaining_runtime_seconds
from .admin_linkage import (
    validate_admin_latest_intake_linkage,
    validate_admin_triage_resume_linkage,
)
from .errors import AdminLatestIntakeLinkageError, TriageResumeMissingPlanError
from .heartbeat import heartbeat_generation_job
from .milestones import build_progress_recorder
from .payloads import parse_plan_request
from .persistence import persist_plan_and_finalize, persist_triage_review_required
from .stage1_runner import Stage1PlannerError, run_stage1_planner
from .stage2_runner import (
    _OPENAI_QUOTA_ADMIN_ERROR,
    finalize_stage2_with_timeout,
    is_openai_quota_error,
    retry_structured_card_before_release,
)
from .timeouts import _stage1_planner_timeout_seconds
from .time_utils import utc_now_iso
from .triage import _is_triage_skipped_final_result, should_skip_stage2
from .types import Planner, ProgressCallback

logger = logging.getLogger(__name__)
_TRIAGE_RESUME_OVERRIDE_KEY = "_triage_resume_override"


def _athlete_is_minor(store: AppStore, athlete_id: str) -> bool:
    """Whether the athlete is under 18, read from the stored profile.

    Fails safe: an unreadable profile is treated as a minor, because guessing
    "adult" would hand an unverified account exactly the weight-cut guidance the
    child policy prohibits. The cost of guessing wrong the other way is a plan
    without cut guidance, which every athlete can still train from.
    """
    try:
        profile_row = store.get_profile(athlete_id)
    except Exception as exc:  # noqa: BLE001 - safeguard must not depend on a clean read
        logger.error(
            "[jobs] generation:minor_check_failed athlete_id=%s exc_type=%s error=%s",
            athlete_id,
            type(exc).__name__,
            sanitize_error_text(exc),
        )
        return True
    if not profile_row:
        return True
    return evaluate_profile_compliance(profile_row).is_minor


# Accounts whose plans may schedule training on the generation day itself. The
# normal pathway starts the camp the day after generation.
_GENERATION_DAY_INCLUDED_EMAILS = frozenset({"vitalvightz@gmail.com", "jjjjjjj@hotmail.com"})


def _athlete_includes_generation_day(store: AppStore, athlete_id: str) -> bool:
    try:
        profile_row = store.get_profile(athlete_id)
    except Exception:  # noqa: BLE001 - optional override; default pathway on failure
        return False
    if not isinstance(profile_row, dict):
        return False
    email = str(profile_row.get("email") or "").strip().lower()
    return email in _GENERATION_DAY_INCLUDED_EMAILS


def _athlete_saved_timezone(store: AppStore, athlete_id: str) -> str:
    """The athlete's saved profile timezone, or ``""`` when none is set yet.

    The route already applies it to fresh requests; the worker re-applies it for
    queued jobs, retries and replayed drafts so the profile refresh below never
    tries to overwrite a saved zone (which the database guard rejects shortly
    after training or XP activity).
    """
    try:
        profile_row = store.get_profile(athlete_id)
    except Exception as exc:  # noqa: BLE001 - fall back to the payload's zone
        logger.warning(
            "[jobs] generation:saved_timezone_read_failed athlete_id=%s exc_type=%s error=%s",
            athlete_id,
            type(exc).__name__,
            sanitize_error_text(exc),
        )
        return ""
    return str((profile_row or {}).get("athlete_timezone") or "").strip()


def _athlete_canonical_age(store: AppStore, athlete_id: str) -> int | None:
    """The athlete's current DOB-derived age, read from the stored profile.

    The request route already normalises the age before a job is created, so on
    a freshly submitted request this re-derivation is a no-op. It is not
    redundant: the worker also runs payloads the route never normalised — jobs
    queued before this rule existed, admin retries, generations replayed from an
    old intake, and stale onboarding drafts — and for those the stored age can
    be years out of date or simply fabricated.

    Fails to ``None`` rather than to the payload's age, for the same reason the
    minor check above fails safe: a profile we cannot read is a profile we
    cannot vouch for, and the submitted age is exactly the value this rule
    exists to distrust.
    """
    try:
        profile_row = store.get_profile(athlete_id)
    except Exception as exc:  # noqa: BLE001 - safeguard must not depend on a clean read
        logger.error(
            "[jobs] generation:canonical_age_check_failed athlete_id=%s exc_type=%s error=%s",
            athlete_id,
            type(exc).__name__,
            sanitize_error_text(exc),
        )
        return None
    if not profile_row:
        return None
    return profile_age_years(profile_row)


def _mark_profile_refresh_failed(final_result: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``final_result`` with the durable profile-refresh marker set.

    Idempotent and defensive: preserves any existing ``why_log`` contents and only adds
    the marker. Never mutates the input in place so callers can keep their own binding.
    """
    if not isinstance(final_result, dict):
        return final_result
    existing_why_log = final_result.get("why_log")
    why_log = dict(existing_why_log) if isinstance(existing_why_log, dict) else {}
    why_log[_PROFILE_REFRESH_FAILED_WHY_LOG_KEY] = {
        "at": utc_now_iso(),
        "detail": _PROFILE_REFRESH_FAILED_WARNING,
    }
    return {**final_result, "why_log": why_log}


async def run_generation_job(
    *,
    job_id: str,
    store: AppStore,
    planner_fn: Planner,
    stage2: Stage2Automator,
    active_tasks: set[str],
) -> None:
    t_start = time.perf_counter()
    stop_event = asyncio.Event()
    heartbeat_task: asyncio.Task[None] | None = None
    progress_flush: Callable[[], None] | None = None
    athlete_id = "unknown"
    progress_callback: ProgressCallback | None = None
    stage1_timed_out = threading.Event()
    # Set by the heartbeat loop the moment it notices the job has moved off
    # "running" underneath us (a manual cancel, or the hard-runtime-ceiling
    # recovery) — checked between stages so the run actually stops burning
    # CPU/API calls on the next expensive step instead of only having its
    # final persistence rejected.
    cancelled = threading.Event()
    seen_milestone_codes: set[str] = set()
    claimed_attempt_count: int | None = None
    profile_refresh_failed = False

    def _safe_error_and_frame(exc: Exception) -> tuple[str, traceback.FrameSummary | None]:
        tb = traceback.extract_tb(exc.__traceback__)
        frame = tb[-1] if tb else None
        return sanitize_error_text(exc), frame

    def _emit_milestone(code: str, label: str, detail: str = "", **meta: Any) -> None:
        if progress_callback is None:
            return
        if code == "stage1_planner_timeout" and code in seen_milestone_codes:
            return
        try:
            progress_callback(code, label, detail, meta)
            seen_milestone_codes.add(code)
        except Exception as exc:
            safe_error, frame = _safe_error_and_frame(exc)
            logger.error(
                "[jobs] generation:milestone_emit_failed athlete_id=%s job_id=%s code=%s exc_type=%s error=%s location=%s:%s:%s",
                athlete_id,
                job_id,
                code,
                type(exc).__name__,
                safe_error,
                frame.filename if frame else "",
                frame.lineno if frame else "",
                frame.name if frame else "",
            )

    async def _fail_claimed_job(error: str, *, now_iso: str | None = None) -> None:
        failed_at = now_iso or utc_now_iso()
        if claimed_attempt_count is None:
            with suppress(Exception):
                await asyncio.to_thread(
                    store.update_generation_job,
                    job_id,
                    status="failed",
                    error=error,
                    completed_at=failed_at,
                    failed_at=failed_at,
                    heartbeat_at=failed_at,
                )
            return
        with suppress(Exception):
            await asyncio.to_thread(
                store.fail_generation_job,
                job_id,
                expected_attempt_count=claimed_attempt_count,
                error=error,
                failed_at=failed_at,
                heartbeat_at=failed_at,
            )

    async def _touch_heartbeat() -> None:
        with suppress(Exception):
            await asyncio.to_thread(
                store.update_generation_job,
                job_id,
                heartbeat_at=utc_now_iso(),
            )

    async def _to_thread_with_heartbeat(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        await _touch_heartbeat()
        result = await asyncio.to_thread(fn, *args, **kwargs)
        await _touch_heartbeat()
        return result

    async def _ensure_admin_resume_plan_exists(linked_plan_id: str | None) -> None:
        if job_source != "admin_triage_resume":
            return
        # resume-from-job (no legacy plan_id) is valid: a real plan row
        # will be created after Stage 2 succeeds. Only validate when the
        # resume was started against a legacy plan row.
        if not linked_plan_id:
            if resume_from_job_only:
                return
            raise TriageResumeMissingPlanError(
                "admin triage resume job lost its linked plan_id; refusing to continue"
            )
        linked = await _to_thread_with_heartbeat(store.get_plan, linked_plan_id)
        if not linked:
            raise TriageResumeMissingPlanError(
                "admin triage resume job linked plan was deleted while generation was running"
            )

    try:
        # Claim the job for processing. This implements the worker-claim model
        # introduced in #1417 while preserving Main's heartbeat-on-read safety.
        try:
            job = await asyncio.to_thread(store.claim_generation_job_start, job_id)
        except HTTPException as exc:
            if exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
                # Transient claim unavailability (store temporarily overloaded).
                # Leave the job queued so a later worker pass can retry it,
                # instead of failing it like a permanent error. Mirrors the
                # scheduler's 503 handling for the capacity-count call.
                logger.warning(
                    "[jobs] generation:claim_unavailable_transient job_id=%s detail=%s",
                    job_id,
                    exc.detail,
                )
                return
            raise
        if not job:
            logger.warning("[jobs] generation:claim_unavailable job_id=%s", job_id)
            return
        claimed_attempt_count = int(job.get("attempt_count") or 0)
        # Freeze the remaining wall-clock budget on a monotonic clock so
        # Stage 1, finalization and the card retry all spend the same budget.
        runtime_deadline = time.monotonic() + generation_job_remaining_runtime_seconds(job)
        logger.info(
            "[jobs] worker:job_loaded athlete_id=%s job_id=%s source=%s status=%s attempt_count=%s",
            job.get("athlete_id"),
            job_id,
            job.get("source"),
            job.get("status"),
            claimed_attempt_count,
        )

        # Use persisted milestones as the initial state for the progress recorder.
        persisted_milestones = job.get("progress_milestones")
        initial_milestones = persisted_milestones if isinstance(persisted_milestones, list) else []
        for milestone in initial_milestones:
            if isinstance(milestone, dict):
                milestone_code = str(milestone.get("code") or "").strip()
                if milestone_code:
                    seen_milestone_codes.add(milestone_code)
        _, progress_callback, progress_flush = build_progress_recorder(
            job_id=job_id,
            store=store,
            initial_milestones=initial_milestones,
            should_persist=lambda: not stage1_timed_out.is_set() and not cancelled.is_set(),
        )
        heartbeat_task = asyncio.create_task(
            heartbeat_generation_job(job_id, store, stop_event, on_cancelled=cancelled.set)
        )

        athlete_id = str(job["athlete_id"])
        raw_request_payload = job.get("request_payload") or {}
        payload_keys = sorted(str(key) for key in raw_request_payload.keys()) if isinstance(raw_request_payload, dict) else []
        logger.info(
            "[jobs] worker:payload_raw athlete_id=%s job_id=%s job_keys=%s request_payload_type=%s request_payload_keys=%s",
            athlete_id,
            job_id,
            sorted(str(key) for key in job.keys()),
            type(raw_request_payload).__name__,
            payload_keys,
        )

        non_health_mode = (
            isinstance(raw_request_payload, dict)
            and raw_request_payload.get(NON_HEALTH_GENERATION_MODE_KEY) == NON_HEALTH_GENERATION_MODE
        )
        current_profile = await _to_thread_with_heartbeat(store.get_profile, athlete_id)
        if (
            isinstance(current_profile, dict)
            and current_profile.get("role") == "athlete"
            and bool(current_profile.get("health_consent_withdrawn_at"))
            and not evaluate_profile_compliance(current_profile).health_consent_granted
            and not non_health_mode
        ):
            # Consent can be withdrawn while a job is queued. Never let a
            # previously accepted health-bearing job cross the worker boundary
            # after that point.
            raise ValueError("health consent is no longer active for this generation job")
        if non_health_mode:
            # Re-validate the stored job at the worker boundary. This prevents
            # a modified job row from bypassing the API sanitiser.
            raw_request_payload = build_non_health_generation_payload(raw_request_payload)

        # Triaging override information: track approval flag and any allowed modes.
        triage_resume_override_approved = False
        triage_override_allowed_modes: list[Any] = []
        if isinstance(raw_request_payload, dict):
            triage_override = raw_request_payload.get(_TRIAGE_RESUME_OVERRIDE_KEY)
            if isinstance(triage_override, dict):
                triage_resume_override_approved = triage_override.get("approved") is True
                modes = triage_override.get("allowed_modes")
                if isinstance(modes, list):
                    triage_override_allowed_modes = list(modes)

        # Normalize source and linked ids early for admin resume validation.
        job_source = str(job.get("source") or "").strip().lower()
        plan_id = str(job.get("plan_id") or "").strip() or None
        intake_id = str(job.get("intake_id") or "").strip() or None
        # Track whether this resume was started without a legacy plan_id —
        # in that case the resume runs against the generation job/intake
        # directly, and a real plan is created only when Stage 2 succeeds.
        resume_from_job_only = job_source == "admin_triage_resume" and not plan_id

        admin_resume_plan_row = await validate_admin_triage_resume_linkage(
            job_source=job_source,
            athlete_id=athlete_id,
            plan_id=plan_id,
            intake_id=intake_id,
            store=store,
            to_thread_with_heartbeat=_to_thread_with_heartbeat,
            emit_milestone=_emit_milestone,
        )

        await validate_admin_latest_intake_linkage(
            job_source=job_source,
            athlete_id=athlete_id,
            intake_id=intake_id,
            raw_request_payload=raw_request_payload,
            store=store,
            to_thread_with_heartbeat=_to_thread_with_heartbeat,
        )

        await _touch_heartbeat()
        logger.info("[jobs] worker:before_request_parse athlete_id=%s job_id=%s", athlete_id, job_id)
        _emit_milestone(
            "request_payload_parse_started",
            "Request payload parse started",
            "Validating the stored intake payload.",
        )
        try:
            request_body = parse_plan_request(raw_request_payload)
        except Exception as exc:
            safe_error, frame = _safe_error_and_frame(exc)
            logger.error(
                "[jobs] worker:request_parse_failed athlete_id=%s job_id=%s exc_type=%s error=%s location=%s:%s:%s",
                athlete_id,
                job_id,
                type(exc).__name__,
                safe_error,
                frame.filename if frame else "",
                frame.lineno if frame else "",
                frame.name if frame else "",
            )
            _emit_milestone(
                "request_payload_parse_failed",
                "Request payload parse failed",
                "Stored intake payload failed request validation.",
                failed=True,
            )
            await _fail_claimed_job(f"request_parse_failed: {safe_error}")
            return
        # Second boundary for the canonical age, after the route's own override.
        # Everything downstream of this line — the refreshed profile draft, the
        # intake row created below, the Stage 1 planner payload, Stage 2 and the
        # persisted plan — reads `request_body.athlete.age`, so replacing it here
        # is what makes a historical payload carrying `age: 25` unable to
        # contradict a profile that resolves to 15 today. The minor fail-safe
        # further down is unchanged and still reads the profile independently.
        canonical_age = await _to_thread_with_heartbeat(
            _athlete_canonical_age, store, athlete_id
        )
        if request_body.athlete.age != canonical_age:
            logger.info(
                "[jobs] generation:age_normalised_from_profile athlete_id=%s job_id=%s "
                "stored_age_present=%s",
                athlete_id,
                job_id,
                request_body.athlete.age is not None,
            )
            request_body.athlete.age = canonical_age
        saved_timezone = await _to_thread_with_heartbeat(
            _athlete_saved_timezone, store, athlete_id
        )
        if saved_timezone:
            request_body.athlete.athlete_timezone = saved_timezone

        await _touch_heartbeat()
        _emit_milestone(
            "request_payload_parsed",
            "Request payload parsed",
            "Stored intake payload passed request validation.",
        )
        logger.info("[jobs] worker:after_request_parse athlete_id=%s job_id=%s", athlete_id, job_id)
        logger.info(
            "[jobs] generation:start athlete_id=%s job_id=%s source=%s job_plan_id=%s job_intake_id=%s "
            "override_present=%s override_approved=%s override_allowed_modes=%s",
            athlete_id,
            job_id,
            job.get("source"),
            job.get("plan_id"),
            job.get("intake_id"),
            isinstance(raw_request_payload, dict)
            and _TRIAGE_RESUME_OVERRIDE_KEY in raw_request_payload,
            triage_resume_override_approved,
            triage_override_allowed_modes,
        )

        try:
            _emit_milestone(
                "profile_update_started",
                "Profile update started",
                "Refreshing athlete profile fields from the request payload.",
            )
            await _to_thread_with_heartbeat(
                store.update_profile,
                athlete_id,
                ProfileUpdateRequest(
                    full_name=request_body.athlete.full_name,
                    technical_style=request_body.athlete.technical_style,
                    tactical_style=request_body.athlete.tactical_style,
                    stance=request_body.athlete.stance,
                    professional_status=request_body.athlete.professional_status,
                    record=request_body.athlete.record,
                    athlete_timezone=request_body.athlete.athlete_timezone,
                    athlete_locale=request_body.athlete.athlete_locale,
                    onboarding_draft=request_body.model_dump(mode="json"),
                ),
            )
            _emit_milestone(
                "profile_update_finished",
                "Profile update finished",
                "Athlete profile fields were refreshed.",
            )
        except Exception as exc:
            profile_refresh_failed = True
            safe_error, frame = _safe_error_and_frame(exc)
            logger.error(
                "[jobs] generation:update_profile_failed athlete_id=%s job_id=%s exc_type=%s error=%s location=%s:%s:%s",
                athlete_id,
                job_id,
                type(exc).__name__,
                safe_error,
                frame.filename if frame else "",
                frame.lineno if frame else "",
                frame.name if frame else "",
            )
            _emit_milestone(
                "profile_update_finished",
                "Profile update finished",
                "Profile refresh failed; generation is continuing with the stored payload.",
                failed=True,
            )
            _emit_milestone(
                _PROFILE_REFRESH_FAILED_WARNING_CODE,
                "Job warning",
                _PROFILE_REFRESH_FAILED_WARNING,
                warning=True,
            )
        if not intake_id:
            intake = await _to_thread_with_heartbeat(store.create_intake, athlete_id, request_body)
            intake_id = str(intake["id"])
            job = await _to_thread_with_heartbeat(
                store.update_generation_job,
                job_id,
                intake_id=intake_id,
                heartbeat_at=utc_now_iso(),
            )

        if cancelled.is_set():
            logger.info("[jobs] generation:cancelled_before_stage1 athlete_id=%s job_id=%s", athlete_id, job_id)
            return

        stage1_result = job.get("stage1_result")
        if not isinstance(stage1_result, dict):
            planner_payload = request_body.to_payload()
            if non_health_mode:
                planner_payload = non_health_planner_payload(planner_payload)
            # The age band comes from the stored profile, never from the intake
            # payload the client submitted. A minor's payload has its cut inputs
            # stripped before the planner ever sees them, so no weight-cut,
            # dehydration or water-cut guidance can be generated in the first
            # place (docs/children-age-appropriate-use-policy.md).
            if await _to_thread_with_heartbeat(_athlete_is_minor, store, athlete_id):
                planner_payload = minor_safe_stage1_payload(planner_payload)
                logger.info(
                    "[jobs] generation:minor_weight_cut_guard_applied athlete_id=%s job_id=%s",
                    athlete_id,
                    job_id,
                )
            if await _to_thread_with_heartbeat(_athlete_includes_generation_day, store, athlete_id):
                planner_payload["include_generation_day"] = True
            if isinstance(raw_request_payload, dict):
                triage_override = raw_request_payload.get(_TRIAGE_RESUME_OVERRIDE_KEY)
                if isinstance(triage_override, dict):
                    planner_payload[_TRIAGE_RESUME_OVERRIDE_KEY] = triage_override
            # Standing admin approval for resumable triage: continue in this
            # job instead of parking the athlete until an admin is online.
            # Stage 1 still evaluates injuries and refuses medical_hold.
            planner_payload.setdefault(
                _TRIAGE_RESUME_OVERRIDE_KEY,
                {
                    "approved": True,
                    "allowed_modes": ["needs_review", "restricted_rehab_only"],
                    "approval_source": "automatic_admin_policy",
                    "reason": "Standing admin approval for generation triage.",
                },
            )
            # Resolve each injury's CURRENT rehab stage server-side and stamp it
            # onto the payload as ephemeral generation context, so Stage 1 selects
            # stage-safe rehab drills from today's tissue tolerance rather than
            # from the intake it was created with. Best-effort: a failure leaves
            # the payload untouched and generation proceeds stage-unaware.
            planner_payload = await _to_thread_with_heartbeat(
                annotate_payload_with_rehab_stage,
                planner_payload,
                store=store,
                athlete_id=athlete_id,
            )
            if not non_health_mode:
                planner_payload = await _to_thread_with_heartbeat(
                    annotate_payload_with_sparring_readiness,
                    planner_payload,
                    store=store,
                    athlete_id=athlete_id,
                    athlete_timezone=request_body.athlete.athlete_timezone or "",
                )
            _emit_milestone(
                "stage1_planner_starting",
                "Stage 1 planner starting",
                "Starting the deterministic planner pass.",
            )
            await _touch_heartbeat()
            _emit_milestone(
                "stage1_planner_invoked",
                "Stage 1 planner invoked",
                "Planner process was invoked and is waiting for its first result.",
            )
            stage1_timeout_seconds = _stage1_planner_timeout_seconds()
            try:
                stage1_result = await run_stage1_planner(
                    planner_fn,
                    planner_payload,
                    progress_callback=progress_callback,
                    timeout_seconds=stage1_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                safe_error, frame = _safe_error_and_frame(exc)
                logger.error(
                    "[jobs] generation:stage1_timeout athlete_id=%s job_id=%s exc_type=%s error=%s location=%s:%s:%s",
                    athlete_id,
                    job_id,
                    type(exc).__name__,
                    safe_error,
                    frame.filename if frame else "",
                    frame.lineno if frame else "",
                    frame.name if frame else "",
                )
                now_iso = utc_now_iso()
                _emit_milestone(
                    "stage1_planner_timeout",
                    "Stage 1 planner timed out",
                    "Planner did not return after invocation and the job was failed for recovery.",
                    timestamp=now_iso,
                    failed=True,
                )
                stage1_timed_out.set()
                await _fail_claimed_job(
                    "Stage 1 planner timed out before producing a result.",
                    now_iso=now_iso,
                )
                return
            _emit_milestone(
                "stage1_planner_finished",
                "Stage 1 planner finished",
                "Planner returned a Stage 1 result.",
            )
            await _touch_heartbeat()
            if stage1_result.get("status") == "invalid_input":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "message": stage1_result.get("error", "invalid planning input"),
                        "missing_fields": stage1_result.get("missing_fields", []),
                    },
                )
            _stage1_why_log = stage1_result.get("why_log") if isinstance(stage1_result.get("why_log"), dict) else {}
            _injury_triage = _stage1_why_log.get("injury_triage") if isinstance(_stage1_why_log.get("injury_triage"), dict) else {}
            _override_marker = (
                _stage1_why_log.get("injury_triage_resume_override")
                if isinstance(_stage1_why_log.get("injury_triage_resume_override"), dict)
                else None
            )
            if _override_marker and _override_marker.get("approval_source") == "automatic_admin_policy":
                _emit_milestone(
                    "triage_auto_approved",
                    "Triage automatically approved",
                    "Standing admin approval applied; continuing plan generation.",
                    triage_mode=_override_marker.get("triage_mode"),
                )
            logger.info(
                "[jobs] generation:stage1_done athlete_id=%s job_id=%s stage1_status=%s "
                "triage_mode=%s should_block_stage2=%s override_applied=%s",
                athlete_id,
                job_id,
                stage1_result.get("status"),
                _injury_triage.get("mode"),
                _injury_triage.get("should_block_stage2"),
                bool(_override_marker and _override_marker.get("bypassed_blocking") is True),
            )
            _emit_milestone(
                "stage1_result_persist_started",
                "Stage 1 result persist started",
                "Saving Stage 1 planner result to the generation job.",
            )
            job = await _to_thread_with_heartbeat(
                store.update_generation_job,
                job_id,
                stage1_result=stage1_result,
                heartbeat_at=utc_now_iso(),
            )
            _emit_milestone(
                "stage1_result_persisted",
                "Stage 1 result persisted",
                "Stage 1 planner result was saved to the generation job.",
            )
            await _ensure_admin_resume_plan_exists(plan_id)

        if cancelled.is_set():
            logger.info("[jobs] generation:cancelled_before_stage2 athlete_id=%s job_id=%s", athlete_id, job_id)
            return

        final_result = job.get("final_result")
        if not isinstance(final_result, dict):
            await _ensure_admin_resume_plan_exists(plan_id)
            if should_skip_stage2(stage1_result, allow_triage_resume_override=triage_resume_override_approved):
                _emit_milestone(
                    "stage2_skipped",
                    "Stage 2 skipped",
                    "Triage routing held the plan at Stage 1, so no AI finalization was needed.",
                )
                final_result = {**stage1_result, "full_name": request_body.athlete.full_name}
            else:
                _emit_milestone(
                    "stage2_drafting",
                    "Stage 2 finalizer drafting",
                    "Sending the planning brief to the AI finalizer.",
                )
                await _touch_heartbeat()
                _emit_milestone(
                    "stage2_model_call_started",
                    "Stage 2 model call started",
                    "AI finalizer request started.",
                    attempt_count=claimed_attempt_count,
                    remaining_runtime_seconds=max(0.0, runtime_deadline - time.monotonic()),
                )
                # The throttle can otherwise leave the durable row at Stage 1
                # throughout a hung provider call. Flush before awaiting it.
                if progress_flush is not None:
                    await asyncio.to_thread(progress_flush)
                stage1_result = {
                    **stage1_result,
                    "_generation_source": str(job.get("source") or ""),
                }
                finalized_result = await finalize_stage2_with_timeout(
                    stage2=stage2,
                    stage1_result=stage1_result,
                    log_context={"job_id": job_id, "athlete_id": athlete_id},
                    remaining_runtime_seconds=max(0.0, runtime_deadline - time.monotonic()),
                )
                await _touch_heartbeat()
                final_result = {**finalized_result, "full_name": request_body.athlete.full_name}
                _emit_milestone(
                    "stage2_model_response_received",
                    "Stage 2 model response received",
                    "AI finalizer returned a response.",
                )
                _emit_milestone(
                    "stage2_response_parse_started",
                    "Stage 2 response parsing started",
                    "Preparing finalizer output for validation and persistence.",
                )
                _emit_milestone(
                    "stage2_response_parsed",
                    "Stage 2 response parsed",
                    "Finalizer output was parsed.",
                )
                _emit_milestone(
                    "stage2_result_ready",
                    "Stage 2 result ready",
                    "Finalizer result returned; saving the release state.",
                )
                finalized_status = str(final_result.get("status") or "").strip().lower()
                if finalized_status == "ready":
                    _emit_milestone(
                        "stage2_validated",
                        "Stage 2 finalizer complete",
                        "Validator passed. Final coach-voice plan ready for handoff.",
                    )
                elif finalized_status == "publishable_with_flags":
                    _emit_milestone(
                        "stage2_flagged",
                        "Stage 2 finalizer complete (flagged)",
                        "Finalizer output released with validator flags recorded for admin audit.",
                    )
                else:
                    _emit_milestone(
                        "stage2_review_required",
                        "Stage 2 needs review",
                        "Finalizer returned a status that is not athlete-displayable.",
                    )
        # Triage-blocked Stage 1 outcomes are protected review states, not
        # plans. They live exclusively on the generation job — no plan row
        # is created or updated. The admin "Approve & Resume" flow drives
        # the next generation from the job; if Stage 2 then succeeds, the
        # resume runtime branch persists a real plan row at that point.
        triage_skipped = _is_triage_skipped_final_result(final_result)
        plan_id = plan_id or (str(job.get("plan_id") or "") or None)

        # Persist the durable profile-refresh-failed marker into the result that is
        # about to be written, so it lands in both the generation job's final_result
        # and (on the plan-success path) the plan row's why_log column. Applied once
        # here so every terminal branch below carries it.
        if profile_refresh_failed:
            final_result = _mark_profile_refresh_failed(final_result)

        if triage_skipped:
            await persist_triage_review_required(
                job_id=job_id,
                athlete_id=athlete_id,
                plan_id=plan_id,
                expected_attempt_count=claimed_attempt_count or 0,
                job_source=job_source,
                final_result=final_result,
                admin_resume_plan_row=admin_resume_plan_row,
                store=store,
                emit_milestone=_emit_milestone,
                to_thread_with_heartbeat=_to_thread_with_heartbeat,
                t_start=t_start,
            )
            return

        # The job's completion is what moves the athlete off the generation
        # screen, so a failed inline enhanced card gets its retry here, before
        # the plan row is released, never after it.
        final_result = await retry_structured_card_before_release(
            stage2=stage2,
            final_result=final_result,
            emit_milestone=_emit_milestone,
            log_context={"job_id": job_id, "athlete_id": athlete_id},
            remaining_runtime_seconds=max(0.0, runtime_deadline - time.monotonic()),
        )
        if time.monotonic() >= runtime_deadline:
            raise asyncio.TimeoutError
        await _touch_heartbeat()

        await persist_plan_and_finalize(
            job=job,
            job_id=job_id,
            athlete_id=athlete_id,
            plan_id=plan_id,
            intake_id=intake_id,
            job_source=job_source,
            resume_from_job_only=resume_from_job_only,
            admin_resume_plan_row=admin_resume_plan_row,
            final_result=final_result,
            request_body=request_body,
            store=store,
            emit_milestone=_emit_milestone,
            to_thread_with_heartbeat=_to_thread_with_heartbeat,
            t_start=t_start,
        )
    # Stage 2 technical failures are terminal for this generation attempt.
    # Stage 1 is planner input only and is never promoted to an athlete-facing plan.
    except asyncio.TimeoutError as exc:
        safe_error, frame = _safe_error_and_frame(exc)
        logger.error(
            "[jobs] generation:stage2_timeout athlete_id=%s job_id=%s exc_type=%s error=%s location=%s:%s:%s",
            athlete_id,
            job_id,
            type(exc).__name__,
            safe_error,
            frame.filename if frame else "",
            frame.lineno if frame else "",
            frame.name if frame else "",
        )
        now_iso = utc_now_iso()
        _emit_milestone(
            "stage2_finalizer_timeout",
            "Stage 2 finalizer timed out",
            "Stage 2 finalizer timed out and the job was failed for recovery.",
            timestamp=now_iso,
            failed=True,
        )
        await _fail_claimed_job("Stage 2 finalizer timed out.", now_iso=now_iso)
    except Stage2AutomationUnavailableError as exc:
        safe_error, frame = _safe_error_and_frame(exc)
        logger.warning(
            "[jobs] generation:stage2_unavailable athlete_id=%s job_id=%s exc_type=%s error=%s location=%s:%s:%s",
            athlete_id,
            job_id,
            type(exc).__name__,
            safe_error,
            frame.filename if frame else "",
            frame.lineno if frame else "",
            frame.name if frame else "",
        )
        await _fail_claimed_job(safe_error)
    except Stage2AutomationError as exc:
        safe_error, frame = _safe_error_and_frame(exc)
        logger.error(
            "[jobs] generation:stage2_failed athlete_id=%s job_id=%s exc_type=%s error=%s location=%s:%s:%s",
            athlete_id,
            job_id,
            type(exc).__name__,
            safe_error,
            frame.filename if frame else "",
            frame.lineno if frame else "",
            frame.name if frame else "",
        )
        resolved_error = _OPENAI_QUOTA_ADMIN_ERROR if is_openai_quota_error(exc) else safe_error
        await _fail_claimed_job(resolved_error)
        # Record whatever token/cost was captured before the failure so a failed
        # Stage 2 attempt is still auditable. Best-effort: never re-raise.
        stage2_cost = getattr(exc, "stage2_cost", None)
        if isinstance(stage2_cost, dict) and stage2_cost:
            with suppress(Exception):
                await asyncio.to_thread(store.record_stage2_cost, job_id, stage2_cost)
    except HTTPException as exc:
        detail = sanitize_error_text(exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail))
        logger.warning("[jobs] generation:http_error athlete_id=%s job_id=%s detail=%s", athlete_id, job_id, detail)
        await _fail_claimed_job(detail)
    except TriageResumeMissingPlanError as exc:
        safe_error, frame = _safe_error_and_frame(exc)
        logger.error(
            "[jobs] generation:resume_missing_plan_failure athlete_id=%s job_id=%s exc_type=%s error=%s location=%s:%s:%s",
            athlete_id,
            job_id,
            type(exc).__name__,
            safe_error,
            frame.filename if frame else "",
            frame.lineno if frame else "",
            frame.name if frame else "",
        )
        await _fail_claimed_job(safe_error)
    except AdminLatestIntakeLinkageError as exc:
        safe_error, frame = _safe_error_and_frame(exc)
        logger.error(
            "[jobs] generation:admin_latest_intake_linkage_failure athlete_id=%s job_id=%s exc_type=%s error=%s location=%s:%s:%s",
            athlete_id,
            job_id,
            type(exc).__name__,
            safe_error,
            frame.filename if frame else "",
            frame.lineno if frame else "",
            frame.name if frame else "",
        )
        await _fail_claimed_job(safe_error)
    except Exception as exc:
        safe_error, frame = _safe_error_and_frame(exc)
        logger.error(
            "[jobs] generation:unhandled_exception athlete_id=%s job_id=%s exc_type=%s error=%s location=%s:%s:%s child_traceback_present=%s",
            athlete_id,
            job_id,
            type(exc).__name__,
            safe_error,
            frame.filename if frame else "",
            frame.lineno if frame else "",
            frame.name if frame else "",
            isinstance(exc, Stage1PlannerError) and bool(exc.child_traceback),
        )
        await _fail_claimed_job("Plan generation failed unexpectedly. Check server logs with the request ID.")
    finally:
        stop_event.set()
        if progress_flush is not None:
            # Writes are throttled, so the closing milestones of the run are
            # usually still pending here. Never let the job end without them.
            with suppress(Exception):
                await asyncio.to_thread(progress_flush)
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        active_tasks.discard(job_id)
