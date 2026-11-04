from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from postgrest.exceptions import APIError as PostgrestAPIError

from fightcamp.logging_utils import bind_log_context, clear_log_context, configure_logging

from . import read_diagnostics, server_timing
from .auth import AuthService, SupabaseAuthService
from .environment import (
    apply_production_environment_defaults,
    is_production_environment,
    should_default_to_production,
)
from .dependencies import (
    Planner,
    get_active_generation_tasks,
    get_enable_in_process_generation,
    get_optional_stage2_automator,
    get_planner,
    get_store,
    require_admin,
    require_plan_row,
    require_profile,
)
from .request_body_guard import RequestBodySizeLimitMiddleware, normalize_request_path
from .models import (
    GenerationJobResponse,
    NutritionWorkspaceUpdateRequest,
    PlanRequest,
    ProfileRecord,
    ProfileUpdateRequest,
)
from .generation.lazy_scheduler import schedule_generation_job_if_needed
from .store import AppStore, SupabaseAppStore
from .sentry_config import init_sentry
from .services.generation_request_service import generate_plan_for_current_user
from .services.today_command_cache import forget_today_command
from .services.generation_retry_service import (
    cancel_generation_job as cancel_generation_job_service,
    retry_generation_job as retry_generation_job_service,
)
from .json_limits import MAX_REQUEST_BODY_BYTES
from .cors_config import (
    get_cors_origins as get_cors_origins,
    get_cors_origin_regex as get_cors_origin_regex,
    validate_production_cors_config as validate_production_cors_config,
)
from .plan_mappers import (
    _map_profile_row,
)
from .generation_job_helpers import (
    _triage_job_has_resume_approval as _triage_job_has_resume_approval,
    _triage_plan_has_resume_approval as _triage_plan_has_resume_approval,
)
from .routes.product_analytics import build_product_analytics_router
from .routes.admin import build_admin_router
from .routes.training_library import build_training_library_router
from .routes.custom_plans import build_custom_plans_router
from .routes.exercise_changes import build_exercise_changes_router
from .routes.private_media import build_private_media_router
from .routes.community_exercises import build_community_router
from .routes import (
    build_daily_router,
    build_feedback_router,
    build_generation_jobs_router,
    build_nutrition_router,
    build_plans_router,
    build_profile_router,
    build_push_router,
    build_today_router,
    build_xp_router,
)
from .settings import env_csv, env_flag, env_float, env_int

if TYPE_CHECKING:
    from .stage2_automation import Stage2Automator

logger = logging.getLogger(__name__)

init_sentry()


def _fastapi_documentation_options() -> dict[str, str | None]:
    if is_production_environment():
        return {
            "docs_url": None,
            "redoc_url": None,
            "openapi_url": None,
        }

    return {
        "docs_url": "/docs",
        "redoc_url": "/redoc",
        "openapi_url": "/openapi.json",
    }


def _admin_max_concurrent_requests() -> int:
    return env_int("APP_ADMIN_MAX_CONCURRENT_REQUESTS", 2, minimum=1)


def _rss_warn_mb() -> float:
    return env_float("APP_RSS_WARN_MB", 450.0, minimum=1.0)


def _rss_mb() -> float | None:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as file:
            for line in file:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 2)
    except Exception:
        return None
    return None


def _validate_session_type_consistency(workspace: NutritionWorkspaceUpdateRequest) -> None:
    training_days = {day.strip().lower() for day in workspace.shared_camp_context.training_availability if str(day).strip()}
    hard_days = {day.strip().lower() for day in workspace.shared_camp_context.hard_sparring_days if str(day).strip()}
    support_days = {day.strip().lower() for day in workspace.shared_camp_context.support_work_days if str(day).strip()}

    for day, session_type in workspace.shared_camp_context.session_types_by_day.items():
        normalized_day = str(day or "").strip().lower()
        if session_type == "hard_spar" and normalized_day not in hard_days:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"session_types_by_day.{day} must also be included in hard_sparring_days",
            )
        if session_type == "technical" and normalized_day not in support_days:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"session_types_by_day.{day} must also be included in support_work_days",
            )
        if session_type != "off" and normalized_day not in training_days:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"session_types_by_day.{day} must also be included in training_availability",
            )


def _validate_schedule_consistency(workspace: NutritionWorkspaceUpdateRequest) -> None:
    shared = workspace.shared_camp_context
    training_days = [day for day in shared.training_availability if str(day).strip()]
    normalized_training_days = {day.strip().lower() for day in training_days}
    if shared.weekly_training_frequency and len(training_days) and shared.weekly_training_frequency > len(training_days):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="weekly_training_frequency cannot exceed selected training_availability days",
        )

    invalid_hard_days = [day for day in shared.hard_sparring_days if str(day).strip().lower() not in normalized_training_days]
    if invalid_hard_days:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"hard_sparring_days must be included in training_availability: {', '.join(invalid_hard_days)}",
        )

    invalid_support_days = [day for day in shared.support_work_days if str(day).strip().lower() not in normalized_training_days]
    if invalid_support_days:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"support_work_days must be included in training_availability: {', '.join(invalid_support_days)}",
        )

    overlap = sorted(
        {
            hard_day
            for hard_day in shared.hard_sparring_days
            if str(hard_day).strip().lower() in {day.strip().lower() for day in shared.support_work_days if str(day).strip()}
        }
    )
    if overlap:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"A day cannot be both hard_sparring and support_work: {', '.join(overlap)}",
        )


def _update_profile_with_nutrition_fallback(
    *,
    store: AppStore,
    athlete_id: str,
    update: ProfileUpdateRequest,
) -> ProfileRecord:
    try:
        return _map_profile_row(store.update_profile(athlete_id, update))
    except HTTPException as exc:
        should_retry_without_profile = (
            update.nutrition_profile is not None
            and exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        if not should_retry_without_profile:
            raise
        logger.warning(
            "[nutrition] retrying profile update without nutrition_profile athlete_id=%s status=%s detail=%s",
            athlete_id,
            exc.status_code,
            exc.detail,
        )
        fallback_update = update.model_copy(update={"nutrition_profile": None})
        return _map_profile_row(store.update_profile(athlete_id, fallback_update))


def is_in_process_generation_enabled() -> bool:
    # Same flag and rule as api.generation.scheduler; defined here so the web
    # process does not import the generation runtime to read it.
    return env_flag("UNLXCK_ENABLE_IN_PROCESS_GENERATION")


def _plan_generate_rate_limit_requests() -> int:
    return env_int("APP_PLAN_GENERATE_RATE_LIMIT", 5, minimum=0)


def _plan_generate_rate_limit_window_seconds() -> float:
    return env_float("APP_PLAN_GENERATE_RATE_LIMIT_WINDOW_SECONDS", 60.0, minimum=1.0)


def _plan_generate_daily_limit_per_user() -> int:
    return env_int("APP_PLAN_GENERATE_DAILY_LIMIT_PER_USER", 5, minimum=0)


def _daily_generation_cap_exempt_emails() -> frozenset[str]:
    return frozenset(env_csv("APP_DAILY_GENERATION_CAP_EXEMPT_EMAILS", lower=True))


# Always exempt from the daily generation cap, on top of the env-configured list.
_ALWAYS_DAILY_GENERATION_CAP_EXEMPT_EMAILS = frozenset({"vitalvightz@gmail.com", "jjjjjjj@hotmail.com"})


def _is_exempt_from_daily_generation_cap(email: str) -> bool:
    normalized = email.strip().lower()
    return (
        normalized in _ALWAYS_DAILY_GENERATION_CAP_EXEMPT_EMAILS
        or normalized in _daily_generation_cap_exempt_emails()
    )


def _default_planner(
    payload: dict[str, Any],
    *,
    progress_callback=None,
) -> dict[str, Any]:
    # Imported lazily: stage1_runner pulls fightcamp.main (the heavy planner
    # side), which the web service must not load unless in-process generation is
    # deliberately enabled.
    from .generation.stage1_runner import default_planner

    return default_planner(payload, progress_callback=progress_callback)


def _noop_planner(
    payload: dict[str, Any],
    *,
    progress_callback=None,
) -> dict[str, Any]:
    return {}


def _health_payload(*, mode_label: str) -> dict[str, str | bool]:
    return {
        "ok": True,
        "app": "unlxck-fight-camp-api",
        "mode": mode_label,
    }


def _log_admin_count_on_startup(store: AppStore) -> None:
    """Log the database admin count at startup so operators can spot drift.

    Runtime admin access also requires UNLXCK_ADMIN_EMAILS membership in
    require_admin, but profiles.role still needs explicit promotion/revocation.
    Surfacing the database count makes accidental lockout or lingering roles
    visible in the boot logs. Best-effort: never block startup on this.
    """
    try:
        admin_count = store.count_admin_profiles()
    except Exception as exc:  # pragma: no cover - diagnostics must not block boot
        logger.warning("[admin] startup_admin_count_failed error_type=%s", type(exc).__name__)
        return
    if admin_count == 0:
        logger.warning(
            "[admin] startup_admin_count=0 — no admin profiles exist; the admin "
            "surface is inaccessible. Seed one via UNLXCK_ADMIN_EMAILS (first "
            "sign-in) or tools/manage_admin.py."
        )
    else:
        logger.info("[admin] startup_admin_count=%s", admin_count)


async def _self_heal_structured_cards_on_startup(store: AppStore) -> None:
    """Detached startup task: recover card builds orphaned by a prior restart.

    Best-effort — never blocks readiness and never crashes the process. See
    :func:`api.services.admin_stage2_service.self_heal_orphaned_structured_cards`.
    """
    try:
        from .services.admin_stage2_service import self_heal_orphaned_structured_cards

        await self_heal_orphaned_structured_cards(store=store)
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - startup recovery must not crash boot
        logger.exception("[stage2] structured-card self-heal task failed")


_SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Mutations that cannot change the Today command view, so they keep the reusable
# Today build (see api.services.today_command_cache). Anything not listed here
# drops it, so a missing entry costs a rebuild, never a stale view.
_TODAY_NEUTRAL_MUTATION_PREFIXES = (
    "/api/xp/",
    "/api/feedback/",
    "/api/push/",
    "/api/generation-jobs/",
)
# Product analytics only inserts product_events, which no Today input reads; the
# web app posts one per page view, and each used to discard the reusable view.
_TODAY_NEUTRAL_MUTATION_PATHS = frozenset({"/api/me/username", "/api/onboarding/draft", "/api/analytics/events"})


def _is_today_neutral_mutation(path: str) -> bool:
    normalized = path.rstrip("/") or "/"
    return normalized in _TODAY_NEUTRAL_MUTATION_PATHS or normalized.startswith(
        _TODAY_NEUTRAL_MUTATION_PREFIXES
    )


def create_app(
    *,
    store: AppStore,
    auth_service: AuthService,
    planner: Planner = _default_planner,
    stage2_automator: "Stage2Automator | None" = None,
    mode_label: str = "supabase-authenticated",
    enable_in_process_generation: bool = True,
) -> FastAPI:
    configure_logging()

    @asynccontextmanager
    async def _app_lifespan(app_instance: FastAPI):
        # Bank priming loads the strength/conditioning/rehab exercise banks into
        # memory for the Stage 1 planner. That is worker-side work; a web service
        # that only creates jobs neither needs the banks resident nor should pay
        # the fightcamp.plan_pipeline import to get prime_plan_banks. Skip it (and
        # keep the import lazy) unless in-process generation is enabled.
        if enable_in_process_generation:
            from fightcamp.plan_pipeline import prime_plan_banks

            await asyncio.to_thread(prime_plan_banks, logger=logger)
        await asyncio.to_thread(_log_admin_count_on_startup, store)
        # Recover structured-card builds orphaned by a prior deploy/restart. Runs
        # detached so it never blocks readiness; it queries first and only builds
        # the Stage 2 automator when there is actually orphaned work, so a clean
        # startup pays nothing.
        heal_task = asyncio.create_task(_self_heal_structured_cards_on_startup(store))
        app_instance.state.structured_card_self_heal_task = heal_task
        try:
            yield
        finally:
            heal_task.cancel()

    app = FastAPI(
        title="UNLXCK Fight Camp API",
        version="0.2.0",
        description="Authenticated athlete-first application API around the fight camp planner.",
        lifespan=_app_lifespan,
        **_fastapi_documentation_options(),
    )
    app.state.store = store
    app.state.auth_service = auth_service
    app.state.planner = planner
    # Do not build the Stage 2 automator at startup — constructing it imports the
    # OpenAI Stage 2 surface (fightcamp.stage2_pipeline). It is built lazily on
    # first use by get_required_stage2_automator so the web service starts light while
    # web-side admin structured-card work still gets a real automator on demand.
    app.state.stage2_automator = stage2_automator
    app.state.mode_label = mode_label
    app.state.enable_in_process_generation = enable_in_process_generation
    app.state.active_generation_tasks = set()
    app.state.admin_request_semaphore = asyncio.Semaphore(_admin_max_concurrent_requests())
    app.state.rss_warn_mb = _rss_warn_mb()
    cors_origins = get_cors_origins()
    cors_regex = get_cors_origin_regex()
    validate_production_cors_config(cors_origins, cors_regex)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=cors_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Hard ceiling on the actual number of body bytes received, enforced as the
    # body streams in. This backstops the Content-Length check below, which a
    # chunked or mislabelled request can slip past.
    feedback_multipart_limit = 5 * 1024 * 1024 + 64 * 1024
    app.add_middleware(
        RequestBodySizeLimitMiddleware,
        max_body_bytes=MAX_REQUEST_BODY_BYTES,
        path_limits={"/api/feedback/global": feedback_multipart_limit},
    )

    async def enforce_request_body_size(request: Request, call_next):
        # Reject obviously oversized requests up front (via the declared
        # Content-Length) before they are buffered, parsed, or routed. This is a
        # coarse, cheap DoS guard; RequestBodySizeLimitMiddleware enforces the
        # same ceiling against the bytes actually received for requests that
        # understate or omit Content-Length, and per-field caps and json_limits
        # provide finer-grained validation once the body is parsed.
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = -1
            request_path = normalize_request_path(request.url.path)
            request_limit = (
                feedback_multipart_limit
                if request_path == "/api/feedback/global"
                else MAX_REQUEST_BODY_BYTES
            )
            if declared > request_limit:
                logger.warning(
                    "[http] request:body_too_large method=%s path=%s content_length=%s limit=%s",
                    request.method,
                    request.url.path,
                    content_length,
                    request_limit,
                )
                return JSONResponse(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    content={"detail": "request body too large", "code": "request_body_too_large"},
                )
        return await call_next(request)

    async def limit_admin_concurrency(request: Request, call_next):
        if request.url.path.startswith("/api/admin/"):
            semaphore = getattr(request.app.state, "admin_request_semaphore", None)
            if semaphore is not None:
                async with semaphore:
                    return await call_next(request)

        return await call_next(request)
        
    async def invalidate_athlete_read_caches(request: Request, call_next):
        # The Today view reused by XP progress must never answer a read that
        # follows an athlete write that could change Today. Writes default to
        # dropping it; only paths known not to touch Today keep it. The profile
        # cache is not handled here: the store methods that write profiles
        # invalidate it themselves.
        try:
            return await call_next(request)
        finally:
            if request.method not in _SAFE_HTTP_METHODS and not _is_today_neutral_mutation(
                request.url.path
            ):
                athlete_id = getattr(request.state, "athlete_id", None)
                if athlete_id:
                    try:
                        forget_today_command(request.app.state.store, athlete_id)
                    except Exception:
                        # Cache maintenance must not turn a committed training
                        # write into a failed save. GET /api/today always rebuilds
                        # from authoritative history, independent of this cache.
                        logger.exception("[http] today_cache_invalidation_failed athlete_id=%s", athlete_id)

    async def log_requests(request: Request, call_next):
        request_id = str(uuid.uuid4())[:8]
        request.state.request_id = request_id
        started = time.perf_counter()
        bind_log_context(request_id=request_id, method=request.method, path=request.url.path)

        logger.info(
            "[http] request:start request_id=%s method=%s path=%s has_query=%s",
            request_id,
            request.method,
            request.url.path,
            bool(request.url.query),
            extra={
                "request_id": request_id,
                "status": "started",
            },
        )

        try:
            response = await call_next(request)
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "[http] request:complete request_id=%s method=%s path=%s status=%s duration_ms=%s",
                request_id,
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                extra={
                    "request_id": request_id,
                    "status": response.status_code,
                },
            )
            rss_mb = _rss_mb()
            if rss_mb is not None and rss_mb >= request.app.state.rss_warn_mb:
                logger.warning(
                    "[memory] high_rss request_id=%s method=%s path=%s rss_mb=%s",
                    request_id,
                    request.method,
                    request.url.path,
                    rss_mb,
                    extra={
                        "request_id": request_id,
                        "status": response.status_code,
                        "rss_mb": rss_mb,
                    },
                )
            return response
        except HTTPException as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.warning(
                "[http] request:http_exception request_id=%s method=%s path=%s status=%s duration_ms=%s error_code=%s",
                request_id,
                request.method,
                request.url.path,
                exc.status_code,
                duration_ms,
                "http_exception",
                extra={
                    "request_id": request_id,
                    "status": exc.status_code,
                    "error_code": "http_exception",
                },
            )
            exc_content: dict[str, Any] = {
                "detail": exc.detail,
                "request_id": request_id,
            }
            error_code = getattr(exc, "code", None)
            if error_code:
                exc_content["code"] = error_code
            return JSONResponse(
                status_code=exc.status_code,
                content=exc_content,
                headers={**(exc.headers or {}), "X-Request-ID": request_id},
            )
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "[http] request:exception request_id=%s method=%s path=%s duration_ms=%s error_code=%s",
                request_id,
                request.method,
                request.url.path,
                duration_ms,
                "unhandled_exception",
                extra={
                    "request_id": request_id,
                    "status": status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "error_code": "unhandled_exception",
                },
            )
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "detail": "Internal server error",
                    "request_id": request_id,
                },
                headers={"X-Request-ID": request_id},
            )
        finally:
            clear_log_context()

    # One middleware layer instead of four stacked ``@app.middleware`` layers
    # (each wraps every request in its own task and response stream). The
    # steps keep their order, outermost first: request logging, athlete read
    # cache invalidation, admin concurrency, declared body size.
    @app.middleware("http")
    async def handle_request(request: Request, call_next):
        async def check_body_size(req: Request):
            return await enforce_request_body_size(req, call_next)

        async def limit_admin(req: Request):
            return await limit_admin_concurrency(req, check_body_size)

        async def invalidate_caches(req: Request):
            return await invalidate_athlete_read_caches(req, limit_admin)

        timings, timing_token = server_timing.begin()
        request_started = time.perf_counter()
        try:
            response = await _dispatch_with_diagnostics(request, invalidate_caches)
        finally:
            server_timing.end(timing_token)
        timings.add("app", (time.perf_counter() - request_started) * 1000)
        response.headers["Server-Timing"] = timings.header_value()
        # Cross-origin callers (the web app) can only read Server-Timing in
        # DevTools / PerformanceResourceTiming when the origin is allowed. Only
        # origins CORS already accepts are echoed.
        origin = request.headers.get("origin")
        if origin and response.headers.get("access-control-allow-origin") == origin:
            response.headers["Timing-Allow-Origin"] = origin
        return response

    async def _dispatch_with_diagnostics(request: Request, invalidate_caches):
        # Off by default in every environment. Enable only for controlled captures.
        if os.getenv("UNLXCK_READ_DIAGNOSTICS") != "1":
            return await log_requests(request, invalidate_caches)
        diagnostics, token = read_diagnostics.begin()
        started = time.perf_counter()
        try:
            return await log_requests(request, invalidate_caches)
        finally:
            read_diagnostics.end(token)
            route = request.scope.get("route")
            logger.info("[read-audit] %s", json.dumps({
                "route": getattr(route, "path", "unmatched"),
                "method": request.method,
                "backend_ms": round((time.perf_counter() - started) * 1000, 3),
                **diagnostics.snapshot(),
            }, separators=(",", ":")))

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "")
        content: dict[str, Any] = {"detail": exc.detail}
        error_code = getattr(exc, "code", None)
        if error_code:
            content["code"] = error_code
        if request_id:
            content["request_id"] = request_id
        headers = {**(exc.headers or {})}
        if request_id:
            headers["X-Request-ID"] = request_id
        return JSONResponse(status_code=exc.status_code, content=content, headers=headers)

    @app.get("/", include_in_schema=False)
    def root(request: Request) -> dict[str, str | bool]:
        return _health_payload(mode_label=str(request.app.state.mode_label))

    @app.head("/", include_in_schema=False)
    def root_head() -> None:
        return None

    @app.get("/health")
    def health(request: Request) -> dict[str, str | bool]:
        return _health_payload(mode_label=str(request.app.state.mode_label))

    if env_flag("ENABLE_SENTRY_DEBUG_ROUTE"):
        @app.get("/sentry-debug", include_in_schema=False)
        def sentry_debug() -> None:
            raise Exception("Sentry backend test error")

    app.include_router(
        build_profile_router(
            require_profile=require_profile,
            get_store=get_store,
        )
    )
    app.include_router(
        build_nutrition_router(
            require_profile=require_profile,
            require_admin=require_admin,
            get_store=get_store,
            validate_schedule_consistency=_validate_schedule_consistency,
            validate_session_type_consistency=_validate_session_type_consistency,
            update_profile_with_nutrition_fallback=_update_profile_with_nutrition_fallback,
        )
    )
    app.include_router(
        build_daily_router(
            require_profile=require_profile,
            require_admin=require_admin,
            get_store=get_store,
        )
    )
    app.include_router(
        build_today_router(
            require_profile=require_profile,
            get_store=get_store,
        )
    )
    app.include_router(build_training_library_router(require_profile=require_profile, get_store=get_store))
    app.include_router(build_custom_plans_router(require_profile=require_profile, get_store=get_store))
    app.include_router(build_exercise_changes_router(require_profile=require_profile, get_store=get_store))
    app.include_router(build_private_media_router(require_profile=require_profile, get_store=get_store))
    app.include_router(build_community_router(require_profile=require_profile, require_admin=require_admin, get_store=get_store))
    app.include_router(
        build_xp_router(
            require_profile=require_profile,
            get_store=get_store,
        )
    )
    app.include_router(
        build_feedback_router(
            require_profile=require_profile,
            require_admin=require_admin,
            get_store=get_store,
        )
    )
    app.include_router(
        build_push_router(
            require_profile=require_profile,
            require_admin=require_admin,
            get_store=get_store,
        )
    )

    @app.post("/api/plans/generate", response_model=GenerationJobResponse, status_code=202)
    async def generate_current_user_plan(
        request: Request,
        request_body: PlanRequest,
        background_tasks: BackgroundTasks,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
        planner_fn: Planner = Depends(get_planner),
        stage2: Any = Depends(get_optional_stage2_automator),
        active_tasks: set[str] = Depends(get_active_generation_tasks),
        enable_in_process_generation: bool = Depends(get_enable_in_process_generation),
    ) -> GenerationJobResponse:
        return await generate_plan_for_current_user(
            request=request,
            request_body=request_body,
            background_tasks=background_tasks,
            profile=profile,
            store=store,
            planner_fn=planner_fn,
            stage2=stage2,
            active_tasks=active_tasks,
            enable_in_process_generation=enable_in_process_generation,
            schedule_generation_job_if_needed=schedule_generation_job_if_needed,
            plan_generate_rate_limit_requests=_plan_generate_rate_limit_requests,
            plan_generate_rate_limit_window_seconds=_plan_generate_rate_limit_window_seconds,
            plan_generate_daily_limit_per_user=_plan_generate_daily_limit_per_user,
            is_exempt_from_daily_generation_cap=_is_exempt_from_daily_generation_cap,
        )

    app.include_router(
        build_generation_jobs_router(
            require_profile=require_profile,
            get_store=get_store,
            get_planner=get_planner,
            get_stage2_automator=get_optional_stage2_automator,
            get_active_generation_tasks=get_active_generation_tasks,
            get_enable_in_process_generation=get_enable_in_process_generation,
            schedule_generation_job_if_needed=schedule_generation_job_if_needed,
        )
    )

    @app.post("/api/generation-jobs/{job_id}/retry", response_model=GenerationJobResponse, status_code=202)
    async def retry_generation_job(
        request: Request,
        job_id: str,
        background_tasks: BackgroundTasks,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
        planner_fn: Planner = Depends(get_planner),
        stage2: Any = Depends(get_optional_stage2_automator),
        active_tasks: set[str] = Depends(get_active_generation_tasks),
        enable_in_process_generation: bool = Depends(get_enable_in_process_generation),
    ) -> GenerationJobResponse:
        return await retry_generation_job_service(
            request=request,
            job_id=job_id,
            background_tasks=background_tasks,
            profile=profile,
            store=store,
            planner_fn=planner_fn,
            stage2=stage2,
            active_tasks=active_tasks,
            enable_in_process_generation=enable_in_process_generation,
            schedule_generation_job_if_needed=schedule_generation_job_if_needed,
            plan_generate_daily_limit_per_user=_plan_generate_daily_limit_per_user,
            is_exempt_from_daily_generation_cap=_is_exempt_from_daily_generation_cap,
        )

    @app.post("/api/generation-jobs/{job_id}/cancel", response_model=GenerationJobResponse)
    async def cancel_generation_job(
        job_id: str,
        profile: ProfileRecord = Depends(require_profile),
        store: AppStore = Depends(get_store),
    ) -> GenerationJobResponse:
        return await cancel_generation_job_service(
            job_id=job_id,
            profile=profile,
            store=store,
        )

    app.include_router(
        build_plans_router(
            require_profile=require_profile,
            require_plan_row=require_plan_row,
            get_store=get_store,
        )
    )

    app.include_router(build_admin_router())
    app.include_router(build_product_analytics_router())
    from .services.product_friction import install_product_friction
    install_product_friction(app)

    return app


def _build_runtime_app() -> FastAPI:
    if should_default_to_production():
        apply_production_environment_defaults()

    enable_in_process_generation = is_in_process_generation_enabled()
    logger.info(
        "[app] build_runtime_app:start has_supabase_url=%s has_service_role_key=%s in_process_generation=%s",
        bool(os.getenv("SUPABASE_URL")),
        bool(os.getenv("SUPABASE_SERVICE_ROLE_KEY")),
        enable_in_process_generation,
    )

    if not (os.getenv("SUPABASE_URL", "").strip() and os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()):
        logger.warning("[app] build_runtime_app:missing_supabase_config")
        return _build_startup_failure_app("missing supabase configuration")
    logger.info("[app] build_runtime_app:using_supabase_mode")
    store = SupabaseAppStore.from_env()
    store.validate_runtime_schema()
    return create_app(
        store=store,
        auth_service=SupabaseAuthService.from_env(),
        mode_label="supabase-authenticated",
        enable_in_process_generation=enable_in_process_generation,
    )


def _build_startup_failure_app(detail: str = "service temporarily unavailable") -> FastAPI:
    app = FastAPI(
        title="UNLXCK Fight Camp API",
        version="0.2.0",
        **_fastapi_documentation_options(),
    )

    def _failure_response() -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "ok": False,
                "app": "unlxck-fight-camp-api",
                "detail": detail,
            },
        )

    @app.get("/", include_in_schema=False)
    def root() -> JSONResponse:
        return _failure_response()

    @app.head("/", include_in_schema=False)
    def root_head() -> Response:
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    @app.get("/health")
    def health() -> JSONResponse:
        return _failure_response()

    return app


try:
    app = _build_runtime_app()
except ValueError:
    # Invalid runtime configuration (e.g. malformed CORS origins).
    logger.exception("[app] runtime_app_build_failed:invalid_config")
    app = _build_startup_failure_app("application startup failed")
except PostgrestAPIError:
    # Backend/database connectivity or quota failure during startup. Listed
    # before the broad ``except Exception`` below — PostgrestAPIError subclasses
    # Exception, so a later clause would never be reached.
    logger.exception("[app] runtime_app_build_failed:postgrest")
    app = _build_startup_failure_app("service temporarily unavailable")
except Exception:
    logger.exception("[app] runtime_app_build_failed")
    app = _build_startup_failure_app("service temporarily unavailable")
