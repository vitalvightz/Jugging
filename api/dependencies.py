"""FastAPI dependencies shared by every router: app state, auth and plan access.

Each reads what ``create_app`` put on ``app.state``, so routers import these
directly instead of receiving them from ``create_app``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import server_timing
from .auth import AuthService, AuthenticatedUser, is_auth_api_error
from .models import ProfileRecord
from .plan_mappers import _is_admin_archived_hidden_from_athlete, _map_profile_row
from .store import AppStore, is_effective_admin_profile

logger = logging.getLogger(__name__)

Planner = Callable[[dict[str, Any]], dict[str, Any]]

security = HTTPBearer(auto_error=False)


def get_store(request: Request) -> AppStore:
    return request.app.state.store

def get_auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service

def get_planner(request: Request) -> Planner:
    return request.app.state.planner

def get_optional_stage2_automator(request: Request) -> Any | None:
    return request.app.state.stage2_automator

def get_required_stage2_automator(request: Request) -> Any:
    # Build the automator on first use and cache it on app.state. This keeps
    # startup free of the OpenAI Stage 2 import while preserving admin
    # structured-card generation, which runs in the web process.
    automator = request.app.state.stage2_automator
    if automator is None:
        from .stage2_automation import build_default_stage2_automator

        automator = build_default_stage2_automator()
        request.app.state.stage2_automator = automator
    return automator

def get_active_generation_tasks(request: Request) -> set[str]:
    return request.app.state.active_generation_tasks

def get_enable_in_process_generation(request: Request) -> bool:
    return bool(request.app.state.enable_in_process_generation)

def require_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    auth: AuthService = Depends(get_auth_service),
) -> AuthenticatedUser:
    request_id = getattr(request.state, "request_id", "")
    if credentials is None or credentials.scheme.lower() != "bearer":
        logger.warning(
            "[auth] missing_or_invalid_bearer_token request_id=%s auth_event=%s status=%s error_code=%s",
            request_id,
            "missing_or_invalid_bearer_token",
            "failure",
            "authentication_required",
            extra={
                "request_id": request_id,
                "auth_event": "missing_or_invalid_bearer_token",
                "status": "failure",
                "error_code": "authentication_required",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
        )
    try:
        user = auth.get_user_from_token(credentials.credentials)
        logger.debug(
            "[auth] token_resolved request_id=%s athlete_id=%s auth_event=%s status=%s",
            request_id,
            user.user_id,
            "token_resolved",
            "success",
            extra={
                "request_id": request_id,
                "athlete_id": user.user_id,
                "auth_event": "token_resolved",
                "status": "success",
            },
        )
        return user
    except HTTPException as exc:
        logger.warning(
            "[auth] token_resolution_http_error request_id=%s status=%s auth_event=%s error_code=%s",
            request_id,
            exc.status_code,
            "token_resolution_http_error",
            "auth_http_error",
            extra={
                "request_id": request_id,
                "auth_event": "token_resolution_http_error",
                "status": exc.status_code,
                "error_code": "auth_http_error",
            },
        )
        raise
    except Exception as exc:
        if is_auth_api_error(exc):
            logger.warning(
                "[auth] token_resolution_invalid_token request_id=%s auth_event=%s status=%s error_code=%s error_class=%s",
                request_id,
                "token_resolution_invalid_token",
                status.HTTP_401_UNAUTHORIZED,
                "invalid_authentication_token",
                exc.__class__.__module__ + "." + exc.__class__.__name__,
                extra={
                    "request_id": request_id,
                    "auth_event": "token_resolution_invalid_token",
                    "status": status.HTTP_401_UNAUTHORIZED,
                    "error_code": "invalid_authentication_token",
                },
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid authentication token",
            ) from exc
        logger.exception(
            "[auth] token_resolution_failed request_id=%s auth_event=%s status=%s error_code=%s",
            request_id,
            "token_resolution_failed",
            "failure",
            "auth_resolution_failed",
            extra={
                "request_id": request_id,
                "auth_event": "token_resolution_failed",
                "status": "failure",
                "error_code": "auth_resolution_failed",
            },
        )
        raise

def require_profile(
    request: Request,
    user: AuthenticatedUser = Depends(require_user),
    store: AppStore = Depends(get_store),
) -> ProfileRecord:
    request_id = getattr(request.state, "request_id", "")
    try:
        with server_timing.measure("profile"):
            profile = _map_profile_row(store.ensure_profile(user))
        request.state.athlete_id = profile.athlete_id
        logger.debug(
            "[auth] profile_resolved request_id=%s athlete_id=%s auth_event=%s status=%s",
            request_id,
            profile.athlete_id,
            "profile_resolved",
            "success",
            extra={
                "request_id": request_id,
                "athlete_id": profile.athlete_id,
                "auth_event": "profile_resolved",
                "status": "success",
            },
        )
        return profile
    except HTTPException as exc:
        logger.warning(
            "[auth] profile_resolution_http_error request_id=%s athlete_id=%s status=%s auth_event=%s error_code=%s",
            request_id,
            user.user_id,
            exc.status_code,
            "profile_resolution_http_error",
            "profile_http_error",
            extra={
                "request_id": request_id,
                "athlete_id": user.user_id,
                "auth_event": "profile_resolution_http_error",
                "status": exc.status_code,
                "error_code": "profile_http_error",
            },
        )
        raise
    except Exception:
        logger.exception(
            "[auth] profile_resolution_failed request_id=%s athlete_id=%s auth_event=%s status=%s error_code=%s",
            request_id,
            user.user_id,
            "profile_resolution_failed",
            "failure",
            "profile_resolution_failed",
            extra={
                "request_id": request_id,
                "athlete_id": user.user_id,
                "auth_event": "profile_resolution_failed",
                "status": "failure",
                "error_code": "profile_resolution_failed",
            },
        )
        raise

def require_admin(
    profile: ProfileRecord = Depends(require_profile),
    store: AppStore = Depends(get_store),
) -> ProfileRecord:
    email_allowlisted = store.is_admin_email(profile.email)
    if not is_effective_admin_profile(profile, store):
        logger.warning(
            "[auth] admin_access_denied athlete_id=%s role=%s email_allowlisted=%s",
            profile.athlete_id,
            profile.role,
            email_allowlisted,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin access required")
    return profile

def require_plan_row(
    plan_id: str,
    profile: ProfileRecord = Depends(require_profile),
    store: AppStore = Depends(get_store),
) -> dict[str, Any]:
    try:
        uuid.UUID(plan_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
    plan_row = store.get_plan(plan_id)
    if not plan_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
    is_admin = is_effective_admin_profile(profile, store)
    if not is_admin and str(plan_row["athlete_id"]) != profile.athlete_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not allowed")
    if not is_admin and _is_admin_archived_hidden_from_athlete(plan_row):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
    return plan_row
