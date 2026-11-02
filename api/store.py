from __future__ import annotations

import copy
import functools
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4
from api.private_media_store import PrivateMediaStoreMixin
from api.community_store import CommunityStoreMixin

import httpx
from fastapi import HTTPException, status
from postgrest.exceptions import APIError as PostgrestAPIError
from storage3.exceptions import StorageApiError
from supabase import Client, ClientOptions, create_client

from .auth import AuthenticatedUser
from .compliance import (
    CODE_DOB_INVALID,
    CODE_UNDER_MINIMUM_AGE,
    HEALTH_CONSENT_VERSION,
    TERMS_VERSION,
    UNDER_MINIMUM_AGE_MESSAGE,
    meets_minimum_age,
    parse_date_of_birth,
)
from .errors import (
    client_request_id_payload_mismatch_error,
    generation_already_in_flight_error,
    generation_daily_limit_error,
    generation_job_not_found_error,
)
from .environment import is_production_environment
from .error_sanitizer import sanitize_error_text
from .generation_config import generation_job_stale_after_seconds, generation_worker_id
from .generation.payloads import _stable_payload_hash
from .generation.time_utils import utc_now_iso as _utc_now_iso
from .json_limits import (
    MAX_CLIENT_JSON_BYTES,
    MAX_JSON_DEPTH,
    MAX_SERVER_JSON_BYTES,
    MAX_STAGE2_PAYLOAD_BYTES,
    json_byte_size,
    validate_json_field,
)
from .models import (
    PlanRequest,
    ProfileUpdateRequest,
    USERNAME_CHANGE_WINDOW_DAYS,
    USERNAME_MAX_CHANGES_PER_WINDOW,
    validate_username,
)
from .schema_requirements import (
    GENERATION_JOB_STAGE2_COST_COLUMNS,
    INTAKES_TABLE,
    PLAN_RUNTIME_REQUIRED_COLUMNS,
)
from .state_machine import (
    ADMIN_REVIEW_PLAN_STATUSES,
    ATHLETE_DISPLAYABLE_PLAN_STATUSES,
    is_generation_job_status,
    is_plan_status,
    require_generation_job_transition,
    require_plan_transition,
)
from .store_performance import CompactGenerationReads
from .read_diagnostics import DiagnosticHttpClient
from .store_protocols import AppStore, RehabExposureWindow
from .structured_block_identity import ensure_structured_block_ids
from .services.effective_structured_plan import ensure_structured_session_ids
from .xp import XpAction
from api.datetimes import parse_utc_datetime as _parse_datetime_utc
from .settings import env_flag, env_float

logger = logging.getLogger(__name__)


def _readiness_history_rows(response: Any) -> list[dict[str, Any]]:
    """A successful SELECT returns a list, including a genuinely empty list.

    Missing/malformed data is not evidence of no workouts, check-ins or injury.
    Callers already handle unavailable reads through their safety boundaries.
    """
    rows = getattr(response, "data", None)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise HTTPException(503, "Readiness history is temporarily unavailable.")
    return rows


def _guard_persisted_json(
    value: Any,
    *,
    field: str,
    max_bytes: int,
    context: str,
) -> None:
    """Reject oversized/too-deep JSON before it is written to Supabase.

    Defense-in-depth backstop for the Pydantic validators: server-assembled
    payloads (request_payload, stage2_payload) never pass through a request
    model, and some callers bypass the HTTP layer entirely. Raises a generic
    413 without echoing payload contents.
    """

    try:
        validate_json_field(
            value,
            field=field,
            max_bytes=max_bytes,
            max_depth=MAX_JSON_DEPTH,
            exc_factory=ValueError,
        )
    except ValueError as exc:
        logger.warning(
            "[store] payload_too_large field=%s max_bytes=%s %s reason=%s",
            field,
            max_bytes,
            context,
            str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"{field} payload too large",
        ) from None


# Exact inputs read by evaluate_profile_compliance; no auth/bootstrap change.
PROFILE_COMPLIANCE_SELECT = (
    "date_of_birth,terms_version,terms_accepted_at,health_data_consent,"
    "health_consent_at,health_consent_withdrawn_at,health_consent_version"
)

PLAN_SUMMARY_SELECT = (
    "id, athlete_id, full_name, fight_date, technical_style, plan_name, status, "
    "stage2_validator_report, pdf_url, created_at"
)
# Execution and logging need the calendar, deterministic fallback and schedule
# metadata, never the Stage-2 prompts/results or admin review text.
TRAINING_PLAN_SELECT = (
    "id,athlete_id,intake_id,fight_date,technical_style,full_name,status,"
    "plan_text,planning_brief,created_at,plan_name,parsing_metadata,"
    "structured_plan,schema_version,stage2_status,stage2_attempt_count,"
    "exercise_overrides,exercise_overrides_revision"
)
GENERATION_JOB_SELECT = "*"
# Admin job-list endpoints never read stage1_result (the largest intermediate
# blob, the full Stage 1 planner output). Listing many rows with select="*"
# loads every stage1_result into memory at once and was OOM-ing the 512MB
# instance, so admin list queries use explicit projections.
GENERATION_JOB_ADMIN_BASE_SELECT = (
    "id, athlete_id, client_request_id, source, status, attempt_count, "
    "heartbeat_at, started_at, completed_at, created_at, updated_at, error, "
    "intake_id, plan_id, progress_milestones"
)
GENERATION_JOB_ADMIN_ACTIVE_SELECT = (
    f"{GENERATION_JOB_ADMIN_BASE_SELECT}, request_payload"
)
GENERATION_JOB_ADMIN_TRIAGE_SELECT = (
    f"{GENERATION_JOB_ADMIN_BASE_SELECT}, request_payload, final_result"
)
GENERATION_JOB_ADMIN_LIST_SELECT = (
    f"{GENERATION_JOB_ADMIN_BASE_SELECT}, request_payload, final_result"
)
# The status-transition guard in update_generation_job only needs the current
# status. It used to re-read the whole row with select="*", which pulls
# request_payload/stage1_result/final_result out of TOAST storage on every
# status write for no reason.
GENERATION_JOB_STATUS_GUARD_SELECT = "id, status"

_TRANSIENT_SUPABASE_ERRORS = (
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ReadTimeout,
)
_STORE_CLIENT_ERRORS = (PostgrestAPIError, httpx.HTTPError)
_STORAGE_CLIENT_ERRORS = (PostgrestAPIError, httpx.HTTPError, StorageApiError)


class LastAdminError(RuntimeError):
    """Raised when a revoke would demote the only remaining admin.

    Guards against accidentally locking everyone out of the admin surface.
    Callers that genuinely intend this must pass ``allow_last_admin=True``.
    """
_TRANSIENT_POSTGREST_SNIPPETS = (
    "connection",
    "connect",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "server disconnected",
    "remote end closed",
    "connection reset",
    "connection terminated",
    "upstream",
    "gateway",
)
# HTTP gateway statuses, matched as whole numbers: SQLSTATE codes such as
# 23503 (foreign_key_violation) contain "503" but are permanent errors.
_TRANSIENT_HTTP_STATUS_PATTERN = re.compile(r"(?<![0-9a-z])50[234](?![0-9a-z])")
_GENERATION_JOB_SCHEMA_SNIPPETS = (
    "schema cache",
    "could not find the table",
    "relation",
    "does not exist",
    "column",
    "generation_jobs",
)
_GENERATION_JOB_CONFLICT_SNIPPETS = (
    "23505",
    "duplicate key value violates unique constraint",
    "generation_jobs_athlete_client_request_key",
    "generation_jobs_one_active_job_per_athlete",
)
_GENERATION_JOB_TERMINAL_CONFLICT_SNIPPETS = (
    "wrong_generation_job_status",
    "stale_generation_job_attempt",
    "stale_generation_job_worker",
    "invalid_terminal_status",
)
_GENERATION_JOB_TERMINAL_MISSING_SNIPPETS = (
    "generation_job_missing",
)
PLAN_RUNTIME_SCHEMA_ERROR_DETAIL = (
    "plans table is missing required runtime columns; apply latest Supabase schema and redeploy"
)
PROFILES_ACTIVE_PLAN_SCHEMA_ERROR_DETAIL = (
    "profiles table is missing active_plan_id; apply latest Supabase schema and redeploy"
)
GENERATION_JOB_ACTIVE_LOCK_ERROR_DETAIL = (
    "generation job active lock is missing; apply latest Supabase migrations and redeploy"
)
# Canonical source lives in api/schema_requirements.py so the live store and the
# deploy-gate runtime schema checker (tools/check_supabase_runtime_schema.py)
# can never drift apart. Re-exported here to preserve the existing import path.
_PLAN_RUNTIME_REQUIRED_COLUMNS_SET = set(PLAN_RUNTIME_REQUIRED_COLUMNS)
_PLAN_RUNTIME_SCHEMA_ERROR_SNIPPETS = (
    "schema cache",
    "column",
    "does not exist",
    "could not find",
)
_PLAN_SCHEMA_MISMATCH_DETAIL = "plans table schema mismatch; apply latest Supabase schema and redeploy"
_PLAN_INVALID_PAYLOAD_DETAIL = "invalid plans payload for table insert"
_VISIBLE_PLAN_STATUSES = {"ready", "publishable_with_flags"}

def _sanitize_error_text(exc: Exception) -> str:
    return sanitize_error_text(exc)


def _visible_plan_text_for_status(result: dict[str, Any], *, status_value: object | None = None) -> str:
    raw_status = result.get("status") if status_value is None else status_value
    status_text = str(raw_status or "").strip().lower()
    if status_text not in _VISIBLE_PLAN_STATUSES:
        return ""
    return str(
        result.get("plan_text")
        or result.get("final_plan_text")
        or result.get("draft_plan_text")
        or ""
    )


GENERATION_JOB_UNAVAILABLE_DETAIL = "generation job service temporarily unavailable"
GENERATION_JOB_SCHEMA_DETAIL = "generation job store is not ready; apply the latest Supabase schema and redeploy"

_is_production_environment = is_production_environment


def _claim_legacy_blank_status_jobs_enabled() -> bool:
    return env_flag("UNLXCK_CLAIM_LEGACY_BLANK_STATUS_JOBS")


def is_effective_admin_profile(profile: Any, store: "AppStore") -> bool:
    if isinstance(profile, dict):
        role = profile.get("role")
        email = profile.get("email")
    else:
        role = getattr(profile, "role", None)
        email = getattr(profile, "email", None)
    return role == "admin" and store.is_admin_email(str(email or ""))


def _signup_date_of_birth(user: AuthenticatedUser) -> str | None:
    """The date of birth declared at signup, when it is usable and 13+.

    Supabase auth metadata is client-writable, so this is only ever a *seed* for
    a profile that has none: :meth:`AppStore._build_profile_payload` prefers an
    already-stored value, and no later read re-imports metadata. An under-13 or
    malformed date returns ``None`` so profile creation still succeeds — the
    account then has no date of birth and cannot pass the compliance gate, which
    is the correct outcome and a far better one than a bootstrap failure that
    leaves an authenticated user with no profile row.
    """
    metadata = getattr(user, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    parsed = parse_date_of_birth(metadata.get("date_of_birth"))
    if parsed is None or not meets_minimum_age(parsed):
        return None
    return parsed.isoformat()


def _signup_full_name(user: AuthenticatedUser) -> str:
    """The name to seed a new profile with, or ``""`` when signup gave none.

    ``AuthenticatedUser.full_name`` falls back to the email address when the
    auth metadata carries no name (see ``api/auth.py``). That fallback is fine
    for display, but seeding a profile with it prefills the intake's name field
    with the athlete's email. Signup no longer asks for a name — intake does —
    so the email fallback seeds an empty name instead. Like the date of birth,
    this is only ever a seed: :meth:`AppStore._build_profile_payload` prefers a
    stored value.
    """
    name = (user.full_name or "").strip()
    if not name or name.lower() == (user.email or "").strip().lower():
        return ""
    return name


def _raise_client_request_payload_mismatch_if_known(job: dict[str, Any], payload_hash: str) -> None:
    existing_hash = job.get("payload_hash")
    if existing_hash and existing_hash != payload_hash:
        raise client_request_id_payload_mismatch_error()


def _encode_structured_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


_ADMIN_SEARCH_RESERVED = re.compile(r'[,()*\\"]+')


def _admin_search_clause(columns: tuple[str, ...], q: str | None) -> str | None:
    """Build a PostgREST ``or()`` expression of case-insensitive ilike matches.

    The raw query is stripped of characters that are structurally significant
    in a PostgREST ``or()`` expression (commas, parentheses, wildcards, quotes,
    backslashes) so a user-supplied search string cannot inject extra
    conditions or break the filter grammar. Returns ``None`` when the sanitized
    term is empty so callers can skip filtering entirely.
    """
    if not q:
        return None
    term = " ".join(_ADMIN_SEARCH_RESERVED.sub(" ", q).split())
    if not term:
        return None
    pattern = f"*{term}*"
    return ",".join(f"{column}.ilike.{pattern}" for column in columns)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            normalized = value.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None
    return None


def _status_transition_error(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _progress_milestones(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _has_milestone_code(milestones: list[Any], code: str) -> bool:
    for entry in milestones:
        if isinstance(entry, dict) and str(entry.get("code") or "") == code:
            return True
    return False


def _latest_generation_job_activity_at(job: dict[str, Any]) -> datetime | None:
    latest: datetime | None = None
    for field in ("heartbeat_at", "updated_at", "started_at", "created_at"):
        parsed = _parse_datetime_utc(job.get(field))
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
    for entry in _progress_milestones(job.get("progress_milestones")):
        if not isinstance(entry, dict):
            continue
        parsed = _parse_datetime_utc(entry.get("at"))
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
    return latest


def _is_active_generation_job_stale_by_latest_activity(
    job: dict[str, Any],
    *,
    stale_after_seconds: int,
) -> bool:
    if str(job.get("status") or "") not in {"queued", "running"}:
        return False
    latest = _latest_generation_job_activity_at(job)
    if latest is None:
        return False
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    else:
        latest = latest.astimezone(timezone.utc)
    return (datetime.now(timezone.utc) - latest).total_seconds() >= max(1, stale_after_seconds)


def _stale_job_reaped_milestones(job: dict[str, Any], *, now_iso: str) -> list[Any]:
    milestones = list(_progress_milestones(job.get("progress_milestones")))
    if not _has_milestone_code(milestones, "stale_job_reaped"):
        milestones.append(
            {
                "code": "stale_job_reaped",
                "label": "Stale job reaped",
                "detail": "Job activity timed out and was failed so a new generation can start.",
                "meta": {},
                "at": now_iso,
            }
        )
    return milestones


def _should_recover_stalled_job(job: dict[str, Any]) -> bool:
    if isinstance(job.get("final_result"), dict):
        return True
    if str(job.get("plan_id") or "").strip():
        return True
    milestones = _progress_milestones(job.get("progress_milestones"))
    for entry in milestones:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or "")
        if code not in {"final_result_persisted", "plan_saved", "generation_job_terminal_status_persisted"}:
            continue
        meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
        if str(meta.get("plan_id") or "").strip():
            return True
    return False


def _plan_id_from_terminal_milestones(job: dict[str, Any]) -> str | None:
    milestones = _progress_milestones(job.get("progress_milestones"))
    for entry in reversed(milestones):
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or "")
        if code not in {"final_result_persisted", "plan_saved", "generation_job_terminal_status_persisted"}:
            continue
        meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
        plan_id = str(meta.get("plan_id") or "").strip()
        if plan_id:
            return plan_id
    return None


def is_pre_start_stale_generation_job(job: dict[str, Any], *, stale_after_seconds: int = 90) -> bool:
    if str(job.get("status") or "") != "running":
        return False
    if _progress_milestones(job.get("progress_milestones")):
        return False
    if job.get("stage1_result") is not None:
        return False
    if job.get("final_result") is not None:
        return False
    if job.get("completed_at") is not None:
        return False

    heartbeat_at = _parse_datetime(job.get("heartbeat_at"))
    started_at = _parse_datetime(job.get("started_at"))
    now = datetime.now(timezone.utc)
    reference_time = heartbeat_at or started_at
    if reference_time is None:
        return False
    return (now - reference_time).total_seconds() >= max(1, stale_after_seconds)


def is_worker_start_stale_generation_job(job: dict[str, Any], *, stale_after_seconds: int = 90) -> bool:
    if str(job.get("status") or "") != "running":
        return False
    if job.get("completed_at") is not None:
        return False
    if job.get("stage1_result") is not None:
        return False
    if job.get("final_result") is not None:
        return False
    milestones = _progress_milestones(job.get("progress_milestones"))
    if len(milestones) != 1:
        return False
    first = milestones[0] if isinstance(milestones[0], dict) else {}
    if str(first.get("code") or "") != "job_loaded":
        return False

    heartbeat_at = _parse_datetime(job.get("heartbeat_at"))
    started_at = _parse_datetime(job.get("started_at"))
    now = datetime.now(timezone.utc)
    reference_time = heartbeat_at or started_at
    if reference_time is None:
        return False
    return (now - reference_time).total_seconds() >= max(1, stale_after_seconds)


def is_job_loaded_stalled_generation_job(job: dict[str, Any], *, stale_after_seconds: int = 90) -> bool:
    if str(job.get("status") or "") != "running":
        return False
    if job.get("completed_at") is not None:
        return False
    if job.get("stage1_result") is not None:
        return False
    if job.get("final_result") is not None:
        return False
    milestones = _progress_milestones(job.get("progress_milestones"))
    if not milestones:
        return False

    saw_job_loaded_at: datetime | None = None
    blocked_codes = {"request_payload_parsed", "profile_update_started", "stage1_planner_starting", "stage1_planner_invoked"}
    for entry in milestones:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or "")
        if code in blocked_codes:
            return False
        if code == "job_loaded":
            parsed = _parse_datetime(entry.get("at"))
            saw_job_loaded_at = parsed or saw_job_loaded_at
    if saw_job_loaded_at is None:
        return False
    age = (datetime.now(timezone.utc) - saw_job_loaded_at).total_seconds()
    return age >= max(1, stale_after_seconds)


def is_startup_stale_generation_job(job: dict[str, Any], *, stale_after_seconds: int = 90) -> bool:
    return is_pre_start_stale_generation_job(
        job,
        stale_after_seconds=stale_after_seconds,
    ) or is_worker_start_stale_generation_job(
        job,
        stale_after_seconds=stale_after_seconds,
    )


def is_stage1_planner_stalled_generation_job(job: dict[str, Any], *, stale_after_seconds: int = 180) -> bool:
    if str(job.get("status") or "") != "running":
        return False
    if job.get("completed_at") is not None or job.get("stage1_result") is not None or job.get("final_result") is not None:
        return False
    milestones = _progress_milestones(job.get("progress_milestones"))
    if not milestones:
        return False
    milestone_invoked_at: datetime | None = None
    saw_planner_finished = False
    for entry in milestones:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or "")
        if code == "stage1_planner_finished":
            saw_planner_finished = True
        if code == "stage1_planner_invoked":
            milestone_invoked_at = _parse_datetime(entry.get("at")) or milestone_invoked_at
    if saw_planner_finished or milestone_invoked_at is None:
        return False
    age = (datetime.now(timezone.utc) - milestone_invoked_at).total_seconds()
    return age >= max(1, stale_after_seconds)


def _stage1_stale_after_seconds_for_reads() -> int:
    raw_value = os.getenv("STAGE1_PLANNER_TIMEOUT_SECONDS")
    if raw_value is None:
        raw_value = os.getenv("APP_STAGE1_PLANNER_TIMEOUT_SECONDS", "600")
    raw_value = raw_value.strip()
    if raw_value in {"", "0", "none", "None", "NONE"}:
        return 600
    try:
        parsed = float(raw_value)
    except ValueError:
        return 600
    if parsed <= 0:
        return 600
    return max(1, int(parsed))


def _generation_startup_max_attempts() -> int:
    raw_value = os.getenv("APP_GENERATION_STARTUP_MAX_ATTEMPTS", "2").strip()
    try:
        parsed = int(float(raw_value))
    except ValueError:
        return 2
    return max(1, parsed)


def _generation_hard_max_runtime_seconds() -> int:
    """Absolute ceiling on a running job's wall-clock age, independent of heartbeat.

    The heartbeat loop refreshes ``heartbeat_at`` on its own timer regardless of
    whether the actual generation work is still progressing, so a downstream hang
    (e.g. a stuck call) can look perpetually "fresh" to every heartbeat-based
    staleness check. This ceiling is keyed off ``started_at`` instead, so a job
    that has simply been running too long gets recovered even with a healthy
    heartbeat.
    """
    raw_value = os.getenv("APP_GENERATION_HARD_MAX_RUNTIME_SECONDS", "900").strip()
    try:
        parsed = int(float(raw_value))
    except (ValueError, OverflowError):
        return 900
    return max(300, parsed)


def generation_job_remaining_runtime_seconds(job: dict[str, Any]) -> float:
    """Wall-clock budget; heartbeat freshness never extends a running attempt."""
    started_at = _parse_datetime(job.get("started_at"))
    age = (datetime.now(timezone.utc) - started_at).total_seconds() if started_at else 0.0
    return max(0.0, _generation_hard_max_runtime_seconds() - max(0.0, age))


def is_generation_job_runtime_exceeded(job: dict[str, Any]) -> bool:
    return (
        str(job.get("status") or "") == "running"
        and generation_job_remaining_runtime_seconds(job) <= 0
    )


DEFAULT_PROFILE_CACHE_TTL_SECONDS = 10.0


def _profile_cache_ttl_seconds() -> float:
    """TTL for the per-process profile cache behind ``ensure_profile``.

    ``0`` disables the cache. Invalid values fall back to the default.
    """
    raw_value = os.getenv("PROFILE_CACHE_TTL_SECONDS")
    if raw_value is None or not raw_value.strip():
        return DEFAULT_PROFILE_CACHE_TTL_SECONDS
    try:
        parsed = float(raw_value.strip())
    except ValueError:
        return DEFAULT_PROFILE_CACHE_TTL_SECONDS
    import math
    if not math.isfinite(parsed) or parsed < 0:
        return DEFAULT_PROFILE_CACHE_TTL_SECONDS
    return parsed


class _ProfileRowCache:
    """Short-lived, per-process cache of profile rows keyed by athlete id.

    Every authenticated request resolves the caller's profile, so without this a
    single screen load re-read the same ``profiles`` row once per API call. The
    TTL is deliberately short: it collapses a burst of requests (login, a
    navigation) into one read while bounding staleness from writes made by other
    processes. Writes made through this store invalidate the entry immediately.
    """

    def __init__(self, ttl_seconds: float, max_size: int = 2000) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self._rows: dict[str, tuple[dict[str, Any], float]] = {}
        self._lock = threading.Lock()

    def get(self, athlete_id: str) -> dict[str, Any] | None:
        if self.ttl_seconds <= 0:
            return None
        with self._lock:
            cached = self._rows.get(athlete_id)
            if cached is None:
                return None
            row, expires_at = cached
            if expires_at <= time.monotonic():
                self._rows.pop(athlete_id, None)
                return None
        return copy.deepcopy(row)

    def put(self, athlete_id: str, row: dict[str, Any]) -> None:
        if self.ttl_seconds <= 0 or not athlete_id or not isinstance(row, dict):
            return
        now = time.monotonic()
        with self._lock:
            if len(self._rows) >= self.max_size:
                self._rows = {
                    key: value for key, value in self._rows.items() if value[1] > now
                }
                if len(self._rows) >= self.max_size:
                    self._rows.clear()
            self._rows[athlete_id] = (copy.deepcopy(row), now + self.ttl_seconds)

    def invalidate(self, athlete_id: str | None = None) -> None:
        with self._lock:
            if athlete_id is None:
                self._rows.clear()
            else:
                self._rows.pop(athlete_id, None)


_PROFILE_CACHE_INIT_LOCK = threading.Lock()


def _invalidates_profile_cache(*, all_profiles: bool = False):
    """Drop the cached profile row once a profile-writing store method returns.

    Invalidation runs after the write (success or failure) so a concurrent read
    cannot repopulate the cache with the pre-write row after it was cleared.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            try:
                return fn(self, *args, **kwargs)
            finally:
                if all_profiles:
                    self.invalidate_cached_profile()
                else:
                    athlete_id = kwargs.get("athlete_id", args[0] if args else None)
                    self.invalidate_cached_profile(str(athlete_id) if athlete_id else None)

        return wrapper

    return decorator


def _job_loaded_milestone(now_iso: str) -> dict[str, Any]:
    return {
        "code": "job_loaded",
        "label": "Generation job loaded",
        "detail": "Worker loaded the persisted generation job.",
        "meta": {},
        "at": now_iso,
    }


@dataclass
class SupabaseAppStore(CommunityStoreMixin, PrivateMediaStoreMixin, CompactGenerationReads):
    client: Client
    admin_emails: set[str]

    @classmethod
    def from_env(cls) -> "SupabaseAppStore":
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
        admin_emails = {
            email.strip().lower()
            for email in os.getenv("UNLXCK_ADMIN_EMAILS", "").split(",")
            if email.strip()
        }
        logger.info(
            "[store] initializing supabase store has_url=%s has_service_role_key=%s admin_emails_count=%s",
            bool(url),
            bool(key),
            len(admin_emails),
        )
        # Disable HTTP/2 to avoid RemoteProtocolError (GOAWAY frames) when
        # Supabase terminates a multiplexed connection after several streams.
        # HTTP/1.1 uses a simple request-per-connection model that is immune
        # to this class of failure.
        #
        # Explicit timeouts avoid httpx's short default read timeout (5s)
        # causing false ReadTimeout outages on Supabase reads (e.g.
        # ensure_profile and generation_jobs polling) during transient
        # latency spikes. Overridable via env for ops tuning.
        read_timeout = env_float("SUPABASE_HTTP_TIMEOUT_SECONDS", 20.0, positive=True)
        connect_timeout = env_float("SUPABASE_HTTP_CONNECT_TIMEOUT_SECONDS", 10.0, positive=True)
        http_client = DiagnosticHttpClient(
            http2=False,
            timeout=httpx.Timeout(
                read_timeout,
                connect=connect_timeout,
            ),
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=20,
                keepalive_expiry=30.0,
            ),
        )
        return cls(create_client(url, key, options=ClientOptions(httpx_client=http_client)), admin_emails)

    def is_admin_email(self, email: str) -> bool:
        if not email:
            return False
        return email.strip().lower() in self.admin_emails

    def _profile_cache(self) -> _ProfileRowCache:
        # Created lazily (not as a dataclass field) so stores built with
        # object.__new__ in tests still get one.
        cache = self.__dict__.get("_profile_row_cache")
        if cache is None:
            with _PROFILE_CACHE_INIT_LOCK:
                cache = self.__dict__.get("_profile_row_cache")
                if cache is None:
                    cache = _ProfileRowCache(_profile_cache_ttl_seconds())
                    self.__dict__["_profile_row_cache"] = cache
        return cache

    def invalidate_cached_profile(self, athlete_id: str | None = None) -> None:
        """Drop one athlete's cached profile row, or every row when ``None``."""
        self._profile_cache().invalidate(athlete_id)

    def _select_first(self, query) -> dict[str, Any] | None:
        response = query.limit(1).execute()
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    def _classify_running_job_staleness(
        self,
        job: dict[str, Any],
        *,
        stale_after_seconds: int,
        stage1_stale_after_seconds: int | None = None,
    ) -> str:
        if str(job.get("status") or "") != "running":
            return "fresh"
        if is_job_loaded_stalled_generation_job(job, stale_after_seconds=stale_after_seconds):
            return "job_loaded_stalled"
        if is_startup_stale_generation_job(job, stale_after_seconds=stale_after_seconds):
            return "startup_stale"
        stage1_threshold = stale_after_seconds if stage1_stale_after_seconds is None else stage1_stale_after_seconds
        if is_stage1_planner_stalled_generation_job(job, stale_after_seconds=stage1_threshold):
            return "stage1_planner_stalled"
        started_at = _parse_datetime(job.get("started_at"))
        now = datetime.now(timezone.utc)
        if is_generation_job_runtime_exceeded(job):
            # A healthy heartbeat only proves the heartbeat loop is alive, not
            # that the generation work itself is progressing. Past the hard
            # ceiling we recover the job regardless of heartbeat freshness.
            return "mid_pipeline_stale"
        heartbeat_at = _parse_datetime(job.get("heartbeat_at"))
        reference_time = heartbeat_at or started_at
        if reference_time is None:
            return "fresh"
        if (now - reference_time).total_seconds() < max(1, stale_after_seconds):
            return "fresh"
        return "mid_pipeline_stale"

    def _fail_stale_active_generation_jobs_for_athlete(
        self,
        athlete_id: str,
        *,
        stale_after_seconds: int,
        exclude_client_request_id: str | None = None,
    ) -> None:
        try:
            query = self.client.table("generation_jobs") \
                .select(GENERATION_JOB_SELECT) \
                .eq("athlete_id", athlete_id) \
                .in_("status", ["queued", "running"])
            if exclude_client_request_id:
                query = query.neq("client_request_id", exclude_client_request_id)
            response = self._run_with_transient_retry(
                operation=f"fail_stale_active_generation_jobs_for_athlete:select athlete_id={athlete_id}",
                fn=lambda: query.order("created_at", desc=True).limit(25).execute(),
            )
            rows = [row for row in (getattr(response, "data", None) or []) if isinstance(row, dict)]
            for row in rows:
                if not _is_active_generation_job_stale_by_latest_activity(
                    row,
                    stale_after_seconds=stale_after_seconds,
                ):
                    continue
                now_iso = _utc_now_iso()
                self._run_with_transient_retry(
                    operation="fail_stale_active_generation_jobs_for_athlete:update",
                    fn=lambda row=row, now_iso=now_iso: self.client.table("generation_jobs")
                    .update(
                        {
                            "status": "failed",
                            "error": "Generation job stalled. Please try again.",
                            "completed_at": now_iso,
                            "failed_at": now_iso,
                            "heartbeat_at": now_iso,
                            "progress_milestones": _stale_job_reaped_milestones(row, now_iso=now_iso),
                        }
                    )
                    .eq("id", str(row.get("id") or ""))
                    .in_("status", ["queued", "running"])
                    .execute(),
                )
        except _STORE_CLIENT_ERRORS as exc:
            if self._is_transient_store_error(exc):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=GENERATION_JOB_UNAVAILABLE_DETAIL,
                ) from exc
            if self._is_generation_job_schema_error(exc):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=GENERATION_JOB_SCHEMA_DETAIL,
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to reap stale generation jobs",
            ) from exc

    def _log_profile_event(self, *, operation: str, user: AuthenticatedUser, **fields: Any) -> None:
        details = " ".join(f"{key}=%r" % value for key, value in sorted(fields.items()))
        suffix = f" {details}" if details else ""
        logger.info(
            "[store] profile:%s athlete_id=%s%s",
            operation,
            user.user_id,
            suffix,
            extra={
                "athlete_id": user.user_id,
                "auth_event": f"profile_{operation}",
                "status": fields.get("status") or "ok",
            },
        )

    def _is_transient_profile_error(self, exc: Exception) -> bool:
        return self._is_transient_store_error(exc)

    def _is_transient_store_error(self, exc: Exception) -> bool:
        if isinstance(exc, _TRANSIENT_SUPABASE_ERRORS):
            return True
        if isinstance(exc, PostgrestAPIError):
            text = " ".join(
                str(part)
                for part in (exc.code, exc.message, exc.hint, exc.details)
                if part
            ).lower()
            return (any(snippet in text for snippet in _TRANSIENT_POSTGREST_SNIPPETS)
                    or _TRANSIENT_HTTP_STATUS_PATTERN.search(text) is not None)
        return False

    def _is_generation_job_schema_error(self, exc: Exception) -> bool:
        if not isinstance(exc, PostgrestAPIError):
            return False
        text = " ".join(
            str(part)
            for part in (exc.code, exc.message, exc.hint, exc.details)
            if part
        ).lower()
        has_generation_job_context = "generation_jobs" in text
        has_schema_mismatch_signal = any(snippet in text for snippet in _GENERATION_JOB_SCHEMA_SNIPPETS)
        return has_generation_job_context and has_schema_mismatch_signal

    def _is_generation_job_conflict_error(self, exc: Exception) -> bool:
        if not isinstance(exc, PostgrestAPIError):
            return False
        text = " ".join(
            str(part)
            for part in (exc.code, exc.message, exc.hint, exc.details)
            if part
        ).lower()
        return any(snippet in text for snippet in _GENERATION_JOB_CONFLICT_SNIPPETS)

    def _is_generation_job_terminal_conflict_error(self, exc: Exception) -> bool:
        if not isinstance(exc, PostgrestAPIError):
            return False
        text = " ".join(
            str(part)
            for part in (exc.code, exc.message, exc.hint, exc.details)
            if part
        ).lower()
        return any(snippet in text for snippet in _GENERATION_JOB_TERMINAL_CONFLICT_SNIPPETS)

    def _is_generation_job_terminal_missing_error(self, exc: Exception) -> bool:
        if not isinstance(exc, PostgrestAPIError):
            return False
        text = " ".join(
            str(part)
            for part in (exc.code, exc.message, exc.hint, exc.details)
            if part
        ).lower()
        return any(snippet in text for snippet in _GENERATION_JOB_TERMINAL_MISSING_SNIPPETS)


    def _is_profiles_active_plan_schema_error(self, exc: Exception) -> bool:
        if not isinstance(exc, PostgrestAPIError):
            return False
        text = " ".join(
            str(part)
            for part in (exc.code, exc.message, exc.hint, exc.details)
            if part
        ).lower()
        return (
            "profiles" in text
            and "active_plan_id" in text
            and any(snippet in text for snippet in _PLAN_RUNTIME_SCHEMA_ERROR_SNIPPETS)
        )

    def _is_plan_schema_column_error(self, exc: Exception) -> bool:
        return self._missing_plan_column(exc) in _PLAN_RUNTIME_REQUIRED_COLUMNS_SET

    @staticmethod
    def _missing_plan_column(exc: Exception) -> str | None:
        # Constraint/payload errors can mention both a runtime column and plans.
        # Only undefined-column codes and the actual column in the message count;
        # details/hints may contain athlete data or merely suggest another column.
        if not isinstance(exc, PostgrestAPIError):
            return None
        code = str(exc.code or "").upper()
        message = str(exc.message or "")
        if code == "PGRST204":
            match = re.search(
                r"Could not find the '([^']+)' column of '(?:public\.)?plans' in the schema cache",
                message, re.IGNORECASE,
            )
        elif code == "42703":
            match = re.search(
                r'column (?:["\']([^"\']+)["\'] of relation "plans"|(?:public\.)?plans\."?(\w+)"?) does not exist',
                message, re.IGNORECASE,
            )
        else:
            return None
        return next((part.lower() for part in match.groups() if part), None) if match else None

    def _legacy_plan_schema_fallback_enabled(self) -> bool:
        flag_set = env_flag("UNLXCK_ALLOW_LEGACY_PLAN_SCHEMA_FALLBACK")
        if not flag_set:
            return False
        if _is_production_environment():
            logger.error(
                "[store] legacy_plan_schema_fallback:blocked_in_production "
                "UNLXCK_ALLOW_LEGACY_PLAN_SCHEMA_FALLBACK is ignored in production; "
                "apply the latest Supabase schema and redeploy"
            )
            return False
        logger.warning(
            "[store] Legacy plan schema fallback is enabled. Runtime columns may be "
            "dropped. Do not use in production."
        )
        return True

    def _log_create_plan_postgrest_error(
        self,
        *,
        athlete_id: str,
        intake_id: str,
        payload: dict[str, Any],
        exc: PostgrestAPIError,
    ) -> None:
        message = str(exc.message or "")
        if str(exc.code or "").startswith("22"):
            # Invalid-input messages may echo a submitted UUID/date/JSON value.
            message = re.sub(r":\s*.*", ": [redacted input]", message, flags=re.DOTALL)
        logger.error(
            "[store] create_plan:postgrest_error athlete_id=%s intake_id=%s error_type=%s code=%s message=%s details=%s hint=%s payload_keys=%s",
            athlete_id,
            intake_id,
            type(exc).__name__,
            getattr(exc, "code", None),
            sanitize_error_text(message),
            # PostgreSQL constraint details include the complete failing row or
            # foreign-key values. Preserve presence, never log those values.
            "[redacted database detail]" if exc.details else None,
            sanitize_error_text(exc.hint) if self._missing_plan_column(exc) and exc.hint else (
                "[redacted database hint]" if exc.hint else None
            ),
            sorted(payload.keys()),
        )

    def _create_plan_error_detail(self, exc: PostgrestAPIError) -> str:
        code = str(getattr(exc, "code", "") or "").lower()
        if self._missing_plan_column(exc):
            return "missing plans column; apply latest Supabase schema and redeploy"
        if code.startswith("22") or code == "pgrst102":
            return _PLAN_INVALID_PAYLOAD_DETAIL
        return "plan persistence failed"

    def _validate_generation_job_active_lock(self) -> None:
        try:
            response = self.client.rpc("validate_generation_job_active_lock").execute()
        except _STORE_CLIENT_ERRORS as exc:
            logger.exception("[store] validate_runtime_schema:active_job_lock_check_failed")
            raise RuntimeError(GENERATION_JOB_ACTIVE_LOCK_ERROR_DETAIL) from exc

        lock_present = bool(response.data)

        if not lock_present:
            logger.error("[store] validate_runtime_schema:active_job_lock_missing")
            raise RuntimeError(GENERATION_JOB_ACTIVE_LOCK_ERROR_DETAIL)

    def validate_runtime_schema(self) -> None:
        legacy_fallback_enabled = self._legacy_plan_schema_fallback_enabled()
        logger.info(
            "[store] validate_runtime_schema:start legacy_fallback_enabled=%s",
            legacy_fallback_enabled,
        )
        try:
            (
                self.client.table("plans")
                .select(",".join(PLAN_RUNTIME_REQUIRED_COLUMNS))
                .limit(1)
                .execute()
            )
        except PostgrestAPIError as exc:
            if not self._is_plan_schema_column_error(exc):
                raise
            if legacy_fallback_enabled:
                logger.warning(
                    "[store] validate_runtime_schema:legacy_fallback_enabled; continuing despite schema mismatch"
                )
            else:
                logger.exception("[store] validate_runtime_schema:schema_mismatch")
                raise RuntimeError(PLAN_RUNTIME_SCHEMA_ERROR_DETAIL) from exc
        except httpx.HTTPError as exc:
            logger.exception("[store] validate_runtime_schema:plan_schema_check_failed")
            raise RuntimeError("store service temporarily unavailable") from exc

        self._validate_generation_job_active_lock()
        logger.info("[store] validate_runtime_schema:ok")

    def _raise_operation_http_error(
        self,
        *,
        operation: str,
        detail: str,
        exc: Exception,
    ) -> None:
        if self._is_transient_store_error(exc):
            logger.warning(
                "[store] %s:transient_failure error_type=%s",
                operation,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="store service temporarily unavailable",
            ) from exc
        logger.exception("[store] %s:exception", operation)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=detail,
        ) from exc

    def _run_with_transient_retry(
        self,
        *,
        operation: str,
        fn: Callable[[], Any],
        attempts: int = 3,
        backoff_seconds: float = 0.25,
    ) -> Any:
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except _STORE_CLIENT_ERRORS as exc:
                transient = self._is_transient_store_error(exc)
                logger.warning(
                    "[store] %s:failure attempt=%s transient=%s error_type=%s error=%s",
                    operation,
                    attempt,
                    transient,
                    type(exc).__name__,
                    exc,
                )
                if not transient or attempt >= attempts:
                    raise
                time.sleep(backoff_seconds * attempt)

        raise RuntimeError(f"{operation} exhausted retries")

    def _lookup_generation_job_by_client_request_id(
        self,
        *,
        athlete_id: str,
        client_request_id: str,
    ) -> dict[str, Any] | None:
        return self._select_first(
            self.client.table("generation_jobs")
            .select(GENERATION_JOB_SELECT)
            .eq("athlete_id", athlete_id)
            .eq("client_request_id", client_request_id)
            .order("created_at", desc=True)
        )

    def _read_generation_job(self, job_id: str) -> dict[str, Any] | None:
        return self._select_first(
            self.client.table("generation_jobs").select(GENERATION_JOB_SELECT).eq("id", job_id)
        )

    def _read_generation_job_status_guard(self, job_id: str) -> dict[str, Any] | None:
        """Status-only read for the update_generation_job transition guard."""
        return self._select_first(
            self.client.table("generation_jobs")
            .select(GENERATION_JOB_STATUS_GUARD_SELECT)
            .eq("id", job_id)
        )

    def _get_profile_by_id(self, athlete_id: str) -> dict[str, Any] | None:
        return self._select_first(self.client.table("profiles").select("*").eq("id", athlete_id))

    def _get_profile_contacts_by_ids(self, athlete_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch ``{email, full_name}`` for a batch of athlete ids.

        Used only for best-effort admin-queue enrichment, so it selects the two
        display columns rather than the full profile row. Raises on transient
        failures so :meth:`_attach_profile_contacts` can degrade gracefully.
        """
        if not athlete_ids:
            return {}
        response = self._run_with_transient_retry(
            operation=f"get_profile_contacts_by_ids count={len(athlete_ids)}",
            fn=lambda: self.client.table("profiles")
            .select("id, email, full_name")
            .in_("id", athlete_ids)
            .execute(),
        )
        contacts: dict[str, dict[str, Any]] = {}
        for row in (response.data or []):
            if isinstance(row, dict) and row.get("id"):
                contacts[str(row["id"])] = {
                    "email": str(row.get("email") or ""),
                    "full_name": str(row.get("full_name") or ""),
                }
        return contacts

    def _attach_profile_contacts(
        self, rows: list[dict[str, Any]], *, id_key: str = "athlete_id"
    ) -> list[dict[str, Any]]:
        """Best-effort attach athlete email/name under ``row['profiles']``.

        Profile enrichment is decoupled from the core admin-queue queries so a
        transient profiles outage degrades to id-only rows instead of failing the
        whole queue. When the lookup fails, each unenriched row is tagged with
        ``profile_enrichment_failed`` so the API/UI can surface a single
        "Profile unavailable" warning while still rendering review/resume actions.
        """
        if not rows:
            return rows
        athlete_ids = sorted(
            {
                str(row.get(id_key))
                for row in rows
                if str(row.get(id_key) or "").strip()
            }
        )
        if not athlete_ids:
            return rows
        try:
            contacts = self._get_profile_contacts_by_ids(athlete_ids)
        except _STORE_CLIENT_ERRORS as exc:
            logger.warning(
                "[store] admin_profile_enrichment:degraded count=%d transient=%s error_type=%s",
                len(athlete_ids),
                self._is_transient_store_error(exc),
                type(exc).__name__,
            )
            for row in rows:
                existing = row.get("profiles")
                if not isinstance(existing, dict) or not (
                    existing.get("email") or existing.get("full_name")
                ):
                    row["profile_enrichment_failed"] = True
                    row["profiles"] = {"email": "", "full_name": ""}
            return rows
        for row in rows:
            existing = row.get("profiles")
            if isinstance(existing, dict) and (
                existing.get("email") or existing.get("full_name")
            ):
                continue
            row["profiles"] = contacts.get(
                str(row.get(id_key) or ""), {"email": "", "full_name": ""}
            )
        return rows

    def get_profile(self, athlete_id: str) -> dict[str, Any] | None:
        """One profile row by id, or ``None``.

        Server-side callers that need a profile fact (the age band, a consent
        state) without a request-scoped ``ProfileRecord`` — the generation
        worker, for instance — read it through here.
        """
        return self._get_profile_by_id(athlete_id)

    def get_profile_compliance(self, athlete_id: str) -> dict[str, Any] | None:
        """Fresh compliance facts only; intentionally no separate cache."""
        return self._select_first(
            self.client.table("profiles").select(PROFILE_COMPLIANCE_SELECT).eq("id", athlete_id)
        )

    @_invalidates_profile_cache()
    def record_compliance_acceptance(
        self,
        athlete_id: str,
        *,
        date_of_birth: str | None = None,
        accept_terms: bool | None = None,
        health_data_consent: bool | None = None,
    ) -> dict[str, Any]:
        """Persist age, Terms acceptance and health-data consent.

        The caller supplies intent only. Every timestamp and version string is
        written here, from the server clock and the constants currently in
        force, so the stored record is auditable evidence rather than a client
        assertion. Withdrawal is recorded as its own timestamp instead of
        clearing the grant: "consented on X, withdrew on Y" is the fact the
        retention policy needs, and erasing it would destroy the audit trail.
        """
        fields: dict[str, Any] = {}
        if date_of_birth is not None:
            parsed = parse_date_of_birth(date_of_birth)
            if parsed is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "code": CODE_DOB_INVALID,
                        "message": "Enter your date of birth as a valid date.",
                    },
                )
            if not meets_minimum_age(parsed):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "code": CODE_UNDER_MINIMUM_AGE,
                        "message": UNDER_MINIMUM_AGE_MESSAGE,
                    },
                )
            fields["date_of_birth"] = parsed.isoformat()

        now = datetime.now(timezone.utc).isoformat()
        if accept_terms:
            fields["terms_version"] = TERMS_VERSION
            fields["terms_accepted_at"] = now
        if health_data_consent is True:
            fields["health_data_consent"] = True
            fields["health_consent_version"] = HEALTH_CONSENT_VERSION
            fields["health_consent_at"] = now
            # A fresh grant supersedes any earlier withdrawal. The withdrawal
            # timestamp is cleared rather than left behind, because a stale
            # withdrawal newer than the grant would read as "still withdrawn".
            fields["health_consent_withdrawn_at"] = None
        elif health_data_consent is False:
            fields["health_data_consent"] = False
            fields["health_consent_withdrawn_at"] = now

        if not fields:
            return self._require_profile(athlete_id)

        try:
            logger.info(
                "[store] compliance:record athlete_id=%s fields=%s",
                athlete_id,
                sorted(fields.keys()),
            )
            self.client.table("profiles").update(fields).eq("id", athlete_id).execute()
            return self._require_profile(athlete_id)
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"record_compliance_acceptance athlete_id={athlete_id}",
                detail="failed to record consent",
                exc=exc,
            )

    def _build_profile_payload(
        self,
        *,
        user: AuthenticatedUser,
        existing: dict[str, Any] | None,
    ) -> dict[str, Any]:
        existing = existing or {}
        return {
            "id": user.user_id,
            "email": user.email,
            "username": existing.get("username"),
            "username_change_history": existing.get("username_change_history") or [],
            "full_name": existing.get("full_name") or _signup_full_name(user),
            "role": existing.get("role") or self._default_role_for(user),
            # New accounts get in straight away: there is no admin approval
            # queue between signing up and building a first plan.
            "access_status": "approved",
            "technical_style": existing.get("technical_style") or [],
            "tactical_style": existing.get("tactical_style") or [],
            "stance": existing.get("stance") or "",
            "professional_status": existing.get("professional_status") or "",
            "record_summary": existing.get("record_summary") or "",
            "athlete_timezone": existing.get("athlete_timezone") or "",
            "athlete_locale": existing.get("athlete_locale") or "",
            "appearance_mode": existing.get("appearance_mode") or "dark",
            "onboarding_draft": existing.get("onboarding_draft"),
            "avatar_url": existing.get("avatar_url"),
            "private_trial_ack_at": existing.get("private_trial_ack_at"),
            # Seeded once, from the date the athlete supplied to the signup form
            # (Supabase auth user metadata). An already-stored value always wins,
            # so re-authenticating with edited metadata cannot move an account
            # out of the under-18 band. Anything under 13 is dropped rather than
            # written: the DB trigger would reject the row and strand the
            # account with no profile at all.
            "date_of_birth": existing.get("date_of_birth")
            or _signup_date_of_birth(user),
        }

    def _upsert_profile_with_retry(
        self,
        *,
        user: AuthenticatedUser,
        payload: dict[str, Any],
        attempts: int = 3,
        backoff_seconds: float = 0.25,
    ) -> None:
        for attempt in range(1, attempts + 1):
            try:
                self._log_profile_event(operation="upsert_attempt", user=user, attempt=attempt)
                self.client.table("profiles").upsert(payload, on_conflict="id").execute()
                self._log_profile_event(operation="upsert_success", user=user, attempt=attempt)
                return
            except _STORE_CLIENT_ERRORS as exc:
                transient = self._is_transient_profile_error(exc)
                logger.warning(
                    "[store] profile:upsert_failure athlete_id=%s attempt=%s transient=%s error_type=%s error=%s error_code=%s",
                    user.user_id,
                    attempt,
                    transient,
                    type(exc).__name__,
                    _sanitize_error_text(exc),
                    "profile_upsert_failure",
                    extra={
                        "athlete_id": user.user_id,
                        "auth_event": "profile_upsert_failure",
                        "status": "failure",
                        "error_code": "profile_upsert_failure",
                    },
                )
                if not transient or attempt >= attempts:
                    raise
                time.sleep(backoff_seconds * attempt)

    def _require_profile(self, athlete_id: str) -> dict[str, Any]:
        profile = self._get_profile_by_id(athlete_id)
        if not profile:
            logger.warning("[store] profile not found athlete_id=%s", athlete_id)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="profile not found")
        return profile

    def _default_role_for(self, user: AuthenticatedUser) -> str:
        if self.is_admin_email(user.email):
            return "admin"
        return "athlete"

    def ensure_profile(self, user: AuthenticatedUser) -> dict[str, Any]:
        try:
            cached = self._profile_cache().get(user.user_id)
            if cached:
                return cached
            self._log_profile_event(operation="ensure_start", user=user)
            existing = self._run_with_transient_retry(
                operation=f"ensure_profile:read athlete_id={user.user_id}",
                fn=lambda: self._get_profile_by_id(user.user_id),
            )
            if existing:
                self._log_profile_event(
                    operation="ensure_existing",
                    user=user,
                    role=existing.get("role"),
                )
                self._profile_cache().put(user.user_id, existing)
                return existing

            payload = self._build_profile_payload(user=user, existing=None)
            try:
                self._upsert_profile_with_retry(user=user, payload=payload)
            except _STORE_CLIENT_ERRORS as exc:
                logger.exception(
                    "[store] profile:ensure_upsert_exception athlete_id=%s error_type=%s error_code=%s",
                    user.user_id,
                    type(exc).__name__,
                    "profile_ensure_upsert_exception",
                    extra={
                        "athlete_id": user.user_id,
                        "auth_event": "profile_ensure_upsert_exception",
                        "status": "failure",
                        "error_code": "profile_ensure_upsert_exception",
                    },
                )
                fallback = self._run_with_transient_retry(
                    operation=f"ensure_profile:fallback_read athlete_id={user.user_id}",
                    fn=lambda: self._get_profile_by_id(user.user_id),
                )
                if fallback:
                    self._log_profile_event(operation="ensure_fallback_read_success", user=user)
                    self._profile_cache().put(user.user_id, fallback)
                    return fallback
                if self._is_transient_profile_error(exc):
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="profile service temporarily unavailable",
                    ) from exc
                raise

            profile = self._run_with_transient_retry(
                operation=f"ensure_profile:post_upsert_read athlete_id={user.user_id}",
                fn=lambda: self._require_profile(user.user_id),
            )
            self._log_profile_event(operation="ensure_created", user=user, role=profile.get("role"))
            self._profile_cache().put(user.user_id, profile)
            return profile
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            logger.exception(
                "[store] ensure_profile:exception athlete_id=%s error_type=%s error_code=%s",
                user.user_id,
                type(exc).__name__,
                "ensure_profile_exception",
                extra={
                    "athlete_id": user.user_id,
                    "auth_event": "ensure_profile_exception",
                    "status": "failure",
                    "error_code": "ensure_profile_exception",
                },
            )
            if self._is_transient_profile_error(exc):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="profile service temporarily unavailable",
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to ensure profile",
            ) from exc

    @_invalidates_profile_cache()
    def record_onboarding_stage(self, athlete_id: str, *, completed: bool = False) -> None:
        try:
            self.client.rpc("record_onboarding_stage", {"p_athlete_id": athlete_id, "p_completed": completed}).execute()
        except _STORE_CLIENT_ERRORS:
            logger.warning("Onboarding workflow timestamp could not be recorded")

    def update_profile(self, athlete_id: str, update: ProfileUpdateRequest) -> dict[str, Any]:
        try:
            fields = update.model_dump(mode="json", exclude_none=True)
            if "record" in fields:
                fields["record_summary"] = fields.pop("record")
            if "private_trial_acknowledged" in fields:
                # The client sends the intent; the server owns the timestamp so
                # an acknowledgement can never be backdated from the browser.
                acknowledged = bool(fields.pop("private_trial_acknowledged"))
                fields["private_trial_ack_at"] = (
                    datetime.now(timezone.utc).isoformat() if acknowledged else None
                )
            for json_field in ("onboarding_draft", "nutrition_profile", "training_habits"):
                if json_field in fields:
                    _guard_persisted_json(
                        fields[json_field],
                        field=json_field,
                        max_bytes=MAX_CLIENT_JSON_BYTES,
                        context=f"athlete_id={athlete_id}",
                    )
            if not fields:
                logger.info("[store] update_profile:no_fields athlete_id=%s", athlete_id)
                return self._require_profile(athlete_id)

            logger.info("[store] update_profile:start athlete_id=%s fields=%s", athlete_id, sorted(fields.keys()))
            query = self.client.table("profiles").update({
                **fields,
                **({"training_habits_revision": update.training_habits_revision + 1} if update.training_habits is not None else {}),
            }).eq("id", athlete_id)
            if update.training_habits is not None:
                query = query.eq("training_habits_revision", update.training_habits_revision)
            response = query.execute()
            if update.training_habits is not None and not getattr(response, "data", None):
                raise HTTPException(409, "Routines changed on another device. Refresh and try again.")
            profile = self._require_profile(athlete_id)
            logger.info("[store] update_profile:success athlete_id=%s", athlete_id)
            return profile
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"update_profile athlete_id={athlete_id}",
                detail="failed to update profile",
                exc=exc,
            )

    # ------------------------------------------------------------------
    # Admin role management
    #
    # UNLXCK_ADMIN_EMAILS seeds a profile's role the first time the profile is
    # created (see _default_role_for), and effective admin access also requires
    # allowlist membership on each backend decision. Removing an email from the
    # env var does not demote the existing database role, though, so grants and
    # revocations after first sign-in must still go through here, which updates
    # profiles.role and records the change in public.admin_role_audit. The
    # service-role key bypasses the prevent_self_role_escalation trigger, so the
    # backend is the only sanctioned path for role changes.
    # ------------------------------------------------------------------

    def _get_profile_by_email(self, email: str) -> dict[str, Any] | None:
        normalized = (email or "").strip().lower()
        if not normalized:
            return None
        # Fast path: the indexed unique column, which is lowercase for any
        # profile created after email normalization landed.
        row = self._select_first(
            self.client.table("profiles").select("*").eq("email", normalized)
        )
        if row:
            return row
        # Fallback for legacy rows stored with mixed-case emails. ilike is
        # case-insensitive; `_`/`%` in a local part are treated as wildcards, so
        # the result is a superset that we narrow with an exact lowercase match.
        response = (
            self.client.table("profiles").select("*").ilike("email", normalized).execute()
        )
        for candidate in getattr(response, "data", None) or []:
            if str(candidate.get("email") or "").strip().lower() == normalized:
                return candidate
        return None

    def list_admin_profiles(self) -> list[dict[str, Any]]:
        response = self._run_with_transient_retry(
            operation="list_admin_profiles",
            fn=lambda: (
                self.client.table("profiles")
                .select("id,email,role")
                .eq("role", "admin")
                .execute()
            ),
        )
        return list(getattr(response, "data", None) or [])

    def count_admin_profiles(self) -> int:
        return len(self.list_admin_profiles())

    @_invalidates_profile_cache(all_profiles=True)
    def set_profile_role(
        self,
        *,
        email: str,
        new_role: str,
        actor: str,
        reason: str | None = None,
        allow_last_admin: bool = False,
    ) -> dict[str, Any]:
        """Grant or revoke admin by email; records an audit row on any change.

        Returns a summary dict with ``changed=False`` when the profile is
        already in the requested role (no write, no audit row). Raises
        ``ValueError`` for an unsupported role, ``LookupError`` when no profile
        matches the email, ``LastAdminError`` when revoking would leave zero
        admins (unless ``allow_last_admin=True``), and ``RuntimeError`` when
        the atomic role+audit transaction fails — in that case neither the
        role change nor the audit row was committed.
        """
        normalized_role = (new_role or "").strip().lower()
        if normalized_role not in {"admin", "athlete"}:
            raise ValueError(f"unsupported role {new_role!r}; expected 'admin' or 'athlete'")
        profile = self._get_profile_by_email(email)
        if not profile:
            raise LookupError(f"no profile found for email {email!r}")

        athlete_id = str(profile["id"])
        previous_role = str(profile.get("role") or "athlete")
        target_email = str(profile.get("email") or email).strip().lower()
        summary = {
            "athlete_id": athlete_id,
            "email": target_email,
            "previous_role": previous_role,
            "new_role": normalized_role,
        }
        if previous_role == normalized_role:
            logger.info(
                "[admin] role:unchanged email=%s role=%s actor=%s",
                target_email, normalized_role, actor,
            )
            return {**summary, "changed": False}

        # Lockout guard: refuse to demote the only remaining admin unless the
        # caller explicitly opts in.
        if previous_role == "admin" and normalized_role != "admin" and not allow_last_admin:
            if self.count_admin_profiles() <= 1:
                raise LastAdminError(
                    f"refusing to revoke {target_email}: they are the only admin. "
                    "Pass allow_last_admin=True to override."
                )

        # Atomic RPC: the role update and the audit insert commit in one
        # database transaction, so a role change can never land without its
        # audit record (and vice versa). The expected previous role is
        # CAS-checked against the locked row so a concurrent change cannot
        # produce a misleading audit trail.
        action = "promote" if normalized_role == "admin" else "revoke"
        try:
            response = self.client.rpc(
                "set_profile_role_with_audit",
                {
                    "p_athlete_id": athlete_id,
                    "p_new_role": normalized_role,
                    "p_actor": actor,
                    "p_expected_previous_role": previous_role,
                    "p_reason": reason,
                    "p_target_email": target_email,
                },
            ).execute()
        except _STORE_CLIENT_ERRORS as exc:
            logger.error(
                "[admin] role:change_rolled_back action=%s email=%s actor=%s error_type=%s error=%s",
                action, target_email, actor, type(exc).__name__, _sanitize_error_text(exc),
            )
            raise RuntimeError(
                f"admin role change for {target_email} was rolled back "
                f"(role and audit write commit together): {_sanitize_error_text(exc)}"
            ) from exc
        result = response.data
        if isinstance(result, list):
            result = result[0] if result else {}
        if not isinstance(result, dict):
            result = {}

        authoritative_email = str(result.get("email") or target_email)
        authoritative_previous_role = str(result.get("previous_role") or previous_role)
        authoritative_new_role = str(result.get("new_role") or normalized_role)
        authoritative_action = str(result.get("action") or action)
        logger.warning(
            "[admin] role:changed action=%s email=%s previous_role=%s new_role=%s actor=%s",
            authoritative_action,
            authoritative_email,
            authoritative_previous_role,
            authoritative_new_role,
            actor,
        )
        return {
            "athlete_id": athlete_id,
            "email": authoritative_email,
            "previous_role": authoritative_previous_role,
            "new_role": authoritative_new_role,
            "changed": result.get("changed", True),
            "action": authoritative_action,
        }

    @_invalidates_profile_cache()
    def change_username(self, athlete_id: str, username: str) -> dict[str, Any]:
        try:
            profile = self._require_profile(athlete_id)
            current_username = (profile.get("username") or "").strip().lower() or None
            normalized = validate_username(username)

            if normalized == current_username:
                logger.info(
                    "[store] change_username:noop athlete_id=%s username unchanged",
                    athlete_id,
                )
                return profile

            try:
                logger.info("[store] change_username:start athlete_id=%s", athlete_id)
                self.client.rpc(
                    "change_profile_username",
                    {
                        "p_profile_id": athlete_id,
                        "p_username": normalized,
                    },
                ).execute()
            except PostgrestAPIError as exc:
                message = " ".join(
                    str(part).lower()
                    for part in (
                        getattr(exc, "message", None),
                        getattr(exc, "details", None),
                        getattr(exc, "hint", None),
                        getattr(exc, "code", None),
                        str(exc),
                    )
                    if part
                )
                if "duplicate" in message or "23505" in message or "unique" in message:
                    logger.info(
                        "[store] change_username:duplicate athlete_id=%s username=%s",
                        athlete_id,
                        normalized,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="That username is already taken. Pick another.",
                    ) from exc
                if "username_rate_limit_exceeded" in message:
                    next_available: str | None = None
                    rate_limit_text = str(getattr(exc, "message", "") or "")
                    match = re.search(r"username_rate_limit_exceeded:([0-9T:\-+.\sZ]+)", rate_limit_text)
                    if match:
                        next_available = match.group(1).strip()
                    logger.info(
                        "[store] change_username:rate_limited athlete_id=%s",
                        athlete_id,
                    )
                    detail = (
                        f"You can change your username up to {USERNAME_MAX_CHANGES_PER_WINDOW} times "
                        f"every {USERNAME_CHANGE_WINDOW_DAYS} days."
                    )
                    if next_available:
                        detail = f"{detail} Next change available {next_available}."
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail=detail,
                    ) from exc
                raise

            profile = self._require_profile(athlete_id)
            logger.info("[store] change_username:success athlete_id=%s", athlete_id)
            return profile
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"change_username athlete_id={athlete_id}",
                detail="failed to change username",
                exc=exc,
            )

    def get_latest_intake(self, athlete_id: str) -> dict[str, Any] | None:
        return self._select_first(
            self.client.table("athlete_intakes")
            .select("*")
            .eq("athlete_id", athlete_id)
            .order("created_at", desc=True)
        )

    def get_intake(self, intake_id: str) -> dict[str, Any] | None:
        return self._select_first(self.client.table("athlete_intakes").select("*").eq("id", intake_id))

    def create_intake(self, athlete_id: str, request: PlanRequest) -> dict[str, Any]:
        payload = {
            "athlete_id": athlete_id,
            "fight_date": request.effective_fight_date.strip() or None,
            "technical_style": request.athlete.technical_style,
            "intake": request.model_dump(mode="json"),
        }
        try:
            logger.info(
                "[store] create_intake:start athlete_id=%s fight_date=%s technical_style=%s",
                athlete_id,
                request.fight_date,
                request.athlete.technical_style,
            )
            response = self.client.table("athlete_intakes").insert(payload).execute()
            rows = getattr(response, "data", None) or []
            if not rows:
                logger.error(
                    "[store] create_intake:no_rows athlete_id=%s response=%r",
                    athlete_id,
                    response,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="failed to persist intake",
                )
            logger.info(
                "[store] create_intake:success athlete_id=%s intake_id=%s",
                athlete_id,
                rows[0].get("id"),
            )
            return rows[0]
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            logger.exception("[store] create_intake:exception athlete_id=%s", athlete_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="create_intake failed",
            ) from exc

    def update_intake(
        self,
        intake_id: str,
        *,
        intake: dict[str, Any],
        fight_date: str | None,
        technical_style: list[str],
    ) -> dict[str, Any]:
        payload = {
            "intake": intake,
            "fight_date": (fight_date or "").strip() or None,
            "technical_style": technical_style,
        }
        try:
            response = self.client.table("athlete_intakes").update(payload).eq("id", intake_id).execute()
            rows = getattr(response, "data", None) or []
            if not rows:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intake not found")
            return rows[0]
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"update_intake intake_id={intake_id}",
                detail="failed to update intake",
                exc=exc,
            )

    def create_plan(
        self,
        *,
        athlete_id: str,
        intake_id: str | None,
        request: PlanRequest,
        result: dict[str, Any],
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        from .structured_plan_coaching import validate_final_coaching
        result = validate_final_coaching(result)
        result_status = str(result.get("status") or "generated").strip().lower()
        if not is_plan_status(result_status):
            raise _status_transition_error(f"unknown plan status: {result_status!r}")
        visible_plan_text = _visible_plan_text_for_status(result, status_value=result_status)
        plan_id = plan_id or str(uuid4())
        payload = {
            "id": plan_id,
            "athlete_id": athlete_id,
            "intake_id": intake_id,
            "fight_date": request.fight_date.strip() or None,
            "technical_style": request.athlete.technical_style,
            "full_name": request.athlete.full_name,
            "plan_name": result.get("plan_name", ""),
            "status": result_status,
            "plan_text": visible_plan_text,
            "draft_plan_text": result.get("draft_plan_text", result.get("plan_text", "")),
            "final_plan_text": result.get("final_plan_text", result.get("plan_text", "")),
            "coach_notes": result.get("coach_notes", ""),
            "pdf_url": result.get("pdf_url"),
            "why_log": result.get("why_log", {}),
            "planning_brief": _encode_structured_text(result.get("planning_brief")),
            "stage2_payload": result.get("stage2_payload"),
            "stage2_handoff_text": result.get("stage2_handoff_text", ""),
            "stage2_retry_text": result.get("stage2_retry_text", ""),
            "stage2_validator_report": result.get("stage2_validator_report", {}),
            "stage2_status": result.get("stage2_status", ""),
            "stage2_attempt_count": result.get("stage2_attempt_count", 0),
            # Custom plans have no intake parsing result. Explicit NULL bypasses
            # the database's {} default and violates its NOT NULL constraint.
            "parsing_metadata": result.get("parsing_metadata") if result.get("parsing_metadata") is not None else {},
            # Structured plan is written only when structured generation produced
            # a validated object; otherwise it stays NULL and the raw plan_text is
            # the fallback. schema_version mirrors the stored structured plan.
            # Every block is saved with a server-owned block_id: exercise logs
            # key on it, so it is never left to the model.
            "structured_plan": ensure_structured_block_ids(
                ensure_structured_session_ids(result.get("structured_plan"), plan_id=plan_id)
            ),
            "schema_version": result.get("schema_version"),
        }

        _guard_persisted_json(
            payload.get("stage2_payload"),
            field="stage2_payload",
            max_bytes=MAX_STAGE2_PAYLOAD_BYTES,
            context=f"athlete_id={athlete_id} intake_id={intake_id}",
        )
        logger.info(
            "[store] create_plan:stage2_payload_size athlete_id=%s intake_id=%s bytes=%s max_bytes=%s",
            athlete_id,
            intake_id,
            json_byte_size(payload.get("stage2_payload")),
            MAX_STAGE2_PAYLOAD_BYTES,
        )
        _guard_persisted_json(
            payload.get("structured_plan"),
            field="structured_plan",
            max_bytes=MAX_SERVER_JSON_BYTES,
            context=f"athlete_id={athlete_id} intake_id={intake_id}",
        )

        def _insert_plan(insert_payload: dict[str, Any]) -> dict[str, Any]:
            response = self.client.table("plans").insert(insert_payload).execute()
            rows = getattr(response, "data", None) or []
            if not rows:
                logger.error(
                    "[store] create_plan:no_rows athlete_id=%s intake_id=%s response_type=%s response_repr=%r",
                    athlete_id,
                    intake_id,
                    type(response).__name__,
                    response,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="failed to persist plan",
                )
            return rows[0]

        try:
            logger.info(
                "[store] create_plan:start athlete_id=%s intake_id=%s status=%s stage2_status=%s",
                athlete_id,
                intake_id,
                payload["status"],
                payload["stage2_status"],
            )
            try:
                row = _insert_plan(payload)
            except PostgrestAPIError as exc:
                if not self._is_plan_schema_column_error(exc):
                    raise
                self._log_create_plan_postgrest_error(
                    athlete_id=athlete_id, intake_id=intake_id, payload=payload, exc=exc,
                )
                if not self._legacy_plan_schema_fallback_enabled():
                    logger.error(
                        "[store] create_plan:schema_mismatch athlete_id=%s intake_id=%s",
                        athlete_id,
                        intake_id,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=PLAN_RUNTIME_SCHEMA_ERROR_DETAIL,
                    ) from exc
                compatibility_payload = {
                    key: value
                    for key, value in payload.items()
                    if key not in _PLAN_RUNTIME_REQUIRED_COLUMNS_SET
                }
                logger.warning(
                    "[store] create_plan:legacy_schema_fallback athlete_id=%s intake_id=%s dropped_columns=%s",
                    athlete_id,
                    intake_id,
                    sorted(_PLAN_RUNTIME_REQUIRED_COLUMNS_SET),
                )
                row = _insert_plan(compatibility_payload)
            logger.info(
                "[store] create_plan:success athlete_id=%s intake_id=%s plan_id=%s",
                athlete_id,
                intake_id,
                row.get("id"),
            )
            return row
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            if isinstance(exc, PostgrestAPIError):
                self._log_create_plan_postgrest_error(
                    athlete_id=athlete_id,
                    intake_id=intake_id,
                    payload=payload,
                    exc=exc,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=self._create_plan_error_detail(exc),
                ) from exc
            logger.exception("[store] create_plan:exception athlete_id=%s intake_id=%s", athlete_id, intake_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="plan persistence failed",
            ) from exc

    def list_user_plans(self, athlete_id: str) -> list[dict[str, Any]]:
        try:
            response = self._run_with_transient_retry(
                operation="list_user_plans",
                fn=lambda: (
                    self.client.table("plans")
                    .select(PLAN_SUMMARY_SELECT)
                    .eq("athlete_id", athlete_id)
                    .order("created_at", desc=True)
                    .execute()
                ),
            )
            return getattr(response, "data", None) or []
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation="list_user_plans",
                detail="failed to list plans",
                exc=exc,
            )

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        try:
            return self._run_with_transient_retry(
                operation=f"get_plan plan_id={plan_id}",
                fn=lambda: self._select_first(self.client.table("plans").select("*").eq("id", plan_id)),
            )
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"get_plan plan_id={plan_id}",
                detail="failed to read plan",
                exc=exc,
            )

    def get_plan_for_athlete(self, plan_id: str, athlete_id: str) -> dict[str, Any] | None:
        """Read a plan scoped to its owner.

        Scoping the query by both ``id`` and ``athlete_id`` means a plan owned by
        another athlete is indistinguishable from a missing one (returns
        ``None``), so callers cannot accidentally leak or mutate it.
        """
        try:
            return self._run_with_transient_retry(
                operation=f"get_plan_for_athlete plan_id={plan_id} athlete_id={athlete_id}",
                fn=lambda: self._select_first(
                    self.client.table("plans")
                    .select("*")
                    .eq("id", plan_id)
                    .eq("athlete_id", athlete_id)
                ),
            )
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"get_plan_for_athlete plan_id={plan_id} athlete_id={athlete_id}",
                detail="failed to read plan",
                exc=exc,
            )

    def _owned_plan_projection(
        self, plan_id: str, athlete_id: str, *, columns: str, operation: str
    ) -> dict[str, Any] | None:
        try:
            return self._run_with_transient_retry(
                operation=operation,
                fn=lambda: self._select_first(
                    self.client.table("plans").select(columns)
                    .eq("id", plan_id).eq("athlete_id", athlete_id)
                ),
            )
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(operation=operation, detail="failed to read plan", exc=exc)

    def get_plan_identity_for_athlete(self, plan_id: str, athlete_id: str) -> dict[str, Any] | None:
        """Owner-scoped existence check without fetching any planner content."""
        return self._owned_plan_projection(
            plan_id, athlete_id, columns="id,athlete_id", operation="get_plan_identity_for_athlete"
        )

    def get_training_plan_for_athlete(self, plan_id: str, athlete_id: str) -> dict[str, Any] | None:
        """The execution calendar and its unchanged deterministic fallback inputs."""
        return self._owned_plan_projection(
            plan_id, athlete_id, columns=TRAINING_PLAN_SELECT, operation="get_training_plan_for_athlete"
        )

    def get_latest_plan(self, athlete_id: str) -> dict[str, Any] | None:
        try:
            return self._run_with_transient_retry(
                operation=f"get_latest_plan athlete_id={athlete_id}",
                fn=lambda: self._select_first(
                    self.client.table("plans")
                    .select("*")
                    .eq("athlete_id", athlete_id)
                    .order("created_at", desc=True)
                ),
            )
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"get_latest_plan athlete_id={athlete_id}",
                detail="failed to read latest plan",
                exc=exc,
            )

    def get_active_plan_id(self, athlete_id: str) -> str | None:
        try:
            row = self._run_with_transient_retry(
                operation=f"get_active_plan_id athlete_id={athlete_id}",
                fn=lambda: self._select_first(
                    self.client.table("profiles").select("active_plan_id").eq("id", athlete_id)
                ),
            )
            return (str(row.get("active_plan_id") or "").strip() or None) if row else None
        except _STORE_CLIENT_ERRORS as exc:
            if self._is_profiles_active_plan_schema_error(exc):
                logger.exception("[store] get_active_plan_id:schema_mismatch athlete_id=%s", athlete_id)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=PROFILES_ACTIVE_PLAN_SCHEMA_ERROR_DETAIL,
                ) from exc
            self._raise_operation_http_error(
                operation=f"get_active_plan_id athlete_id={athlete_id}",
                detail="failed to read active plan",
                exc=exc,
            )

    @_invalidates_profile_cache()
    def set_active_plan_id(self, athlete_id: str, plan_id: str) -> None:
        try:
            self._run_with_transient_retry(
                operation=f"set_active_plan_id athlete_id={athlete_id}",
                fn=lambda: self.client.table("profiles").update({"active_plan_id": plan_id}).eq("id", athlete_id).execute(),
            )
        except _STORE_CLIENT_ERRORS as exc:
            if self._is_profiles_active_plan_schema_error(exc):
                logger.exception("[store] set_active_plan_id:schema_mismatch athlete_id=%s", athlete_id)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=PROFILES_ACTIVE_PLAN_SCHEMA_ERROR_DETAIL,
                ) from exc
            self._raise_operation_http_error(
                operation=f"set_active_plan_id athlete_id={athlete_id}",
                detail="failed to set active plan",
                exc=exc,
            )


    @_invalidates_profile_cache()
    def try_set_active_plan_id(
        self, athlete_id: str, plan_id: str, expected_id: str | None,
        current_training_day: date | str | None = None,
    ) -> bool:
        """Compare the pointer, eligible candidate and sole-plan count atomically."""
        try:
            response = self._run_with_transient_retry(
                operation=f"try_set_active_plan_id athlete_id={athlete_id}",
                fn=lambda: self.client.rpc("try_activate_only_unarchived_plan", {
                    "p_athlete_id": athlete_id, "p_plan_id": plan_id,
                    "p_expected_id": expected_id,
                    "p_training_day": str(current_training_day or date.today()),
                }).execute(),
            )
            return getattr(response, "data", None) is True
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"try_set_active_plan_id athlete_id={athlete_id}",
                detail="failed to set active plan",
                exc=exc,
            )


    def create_or_get_generation_job(
        self,
        *,
        athlete_id: str,
        client_request_id: str,
        source: str,
        request_payload: dict[str, Any],
        plan_id: str | None = None,
        intake_id: str | None = None,
        stale_after_seconds: int = 90,
    ) -> dict[str, Any]:
        last_error: Exception | None = None

        _guard_persisted_json(
            request_payload,
            field="request_payload",
            max_bytes=MAX_SERVER_JSON_BYTES,
            context=f"athlete_id={athlete_id} client_request_id={client_request_id}",
        )
        payload_hash = _stable_payload_hash(request_payload)
        self._fail_stale_active_generation_jobs_for_athlete(
            athlete_id,
            stale_after_seconds=stale_after_seconds,
            exclude_client_request_id=client_request_id,
        )

        try:
            existing = self._run_with_transient_retry(
                operation="create_or_get_generation_job:lookup_existing",
                fn=lambda: self._lookup_generation_job_by_client_request_id(
                    athlete_id=athlete_id,
                    client_request_id=client_request_id,
                ),
            )
        except _STORE_CLIENT_ERRORS as exc:
            if not self._is_transient_store_error(exc):
                if self._is_generation_job_schema_error(exc):
                    logger.exception(
                        "[store] create_or_get_generation_job:schema_mismatch athlete_id=%s client_request_id=%s",
                        athlete_id,
                        client_request_id,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=GENERATION_JOB_SCHEMA_DETAIL,
                    ) from exc
                logger.exception(
                    "[store] create_or_get_generation_job:lookup_exception athlete_id=%s client_request_id=%s",
                    athlete_id,
                    client_request_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="failed to read generation job",
                ) from exc
            last_error = exc
            existing = None
        if existing:
            if is_startup_stale_generation_job(existing, stale_after_seconds=stale_after_seconds):
                reset_payload = {
                    "status": "queued",
                    "source": (source or "").strip() or "self_serve",
                    "request_payload": request_payload,
                    "payload_hash": payload_hash,
                    "error": None,
                    "heartbeat_at": None,
                    "started_at": None,
                    "completed_at": None,
                    "stage1_result": None,
                    "final_result": None,
                    "progress_milestones": [],
                    # Back to the queue means back to unowned; the next claim
                    # records the new owner.
                    "claimed_by": None,
                    "claimed_at": None,
                }

                if plan_id is not None:
                    reset_payload["plan_id"] = plan_id
                if intake_id is not None:
                    reset_payload["intake_id"] = intake_id

                self._run_with_transient_retry(
                    operation="create_or_get_generation_job:reset_startup_stale",
                    fn=lambda: self.client.table("generation_jobs")
                    .update(reset_payload)
                    .eq("id", str(existing["id"]))
                    .eq("status", "running")
                    .execute(),
                )
                refreshed = self.get_generation_job(str(existing["id"]))
                if refreshed:
                    return refreshed
            _raise_client_request_payload_mismatch_if_known(existing, payload_hash)
            return existing
        active_job = self.reconcile_active_generation_job_for_athlete(
            athlete_id,
            stale_after_seconds=stale_after_seconds,
        )
        if active_job and str(active_job.get("status") or "") in {"queued", "running"}:
            if str(active_job.get("client_request_id") or "") == client_request_id:
                _raise_client_request_payload_mismatch_if_known(active_job, payload_hash)
                return active_job
            raise generation_already_in_flight_error()

        payload = {
            "athlete_id": athlete_id,
            "client_request_id": client_request_id,
            "source": (source or "").strip() or "self_serve",
            "request_payload": request_payload,
            "payload_hash": payload_hash,
            "status": "queued",
            "attempt_count": 0,
            "heartbeat_at": None,
            "started_at": None,
            "completed_at": None,
            "error": None,
            "intake_id": intake_id,
            "stage1_result": None,
            "final_result": None,
            "plan_id": plan_id,
        }
        try:
            response = self._run_with_transient_retry(
                operation="create_or_get_generation_job:insert",
                fn=lambda: self.client.table("generation_jobs").insert(payload).execute(),
            )
            rows = getattr(response, "data", None) or []
            if rows:
                return rows[0]
        except _STORE_CLIENT_ERRORS as exc:
            last_error = exc
            if self._is_generation_job_conflict_error(exc):
                logger.info(
                    "[store] create_or_get_generation_job:insert_conflict athlete_id=%s client_request_id=%s",
                    athlete_id,
                    client_request_id,
                )
            else:
                logger.exception(
                    "[store] create_or_get_generation_job:insert_exception athlete_id=%s client_request_id=%s",
                    athlete_id,
                    client_request_id,
                )

        try:
            existing = self._run_with_transient_retry(
                operation="create_or_get_generation_job:lookup_after_insert",
                fn=lambda: self._lookup_generation_job_by_client_request_id(
                    athlete_id=athlete_id,
                    client_request_id=client_request_id,
                ),
            )
        except _STORE_CLIENT_ERRORS as exc:
            if not self._is_transient_store_error(exc):
                if self._is_generation_job_schema_error(exc):
                    logger.exception(
                        "[store] create_or_get_generation_job:schema_mismatch_after_insert athlete_id=%s client_request_id=%s",
                        athlete_id,
                        client_request_id,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=GENERATION_JOB_SCHEMA_DETAIL,
                    ) from exc
                logger.exception(
                    "[store] create_or_get_generation_job:lookup_after_insert_exception athlete_id=%s client_request_id=%s",
                    athlete_id,
                    client_request_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="failed to read generation job",
                ) from exc
            last_error = exc
            existing = None
        if existing:
            _raise_client_request_payload_mismatch_if_known(existing, payload_hash)
            return existing
        active_job = self.reconcile_active_generation_job_for_athlete(
            athlete_id,
            stale_after_seconds=stale_after_seconds,
        )
        if active_job and str(active_job.get("status") or "") in {"queued", "running"}:
            if str(active_job.get("client_request_id") or "") == client_request_id:
                _raise_client_request_payload_mismatch_if_known(active_job, payload_hash)
                return active_job
            raise generation_already_in_flight_error()
        if last_error and self._is_transient_store_error(last_error):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=GENERATION_JOB_UNAVAILABLE_DETAIL,
            ) from last_error
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to persist generation job",
        )

    def create_or_get_generation_job_with_daily_limit(
        self,
        *,
        athlete_id: str,
        client_request_id: str,
        source: str,
        request_payload: dict[str, Any],
        daily_limit: int,
        day_start_iso: str,
        limit_reached_detail: str,
        counted_sources: set[str],
        plan_id: str | None = None,
        intake_id: str | None = None,
        stale_after_seconds: int = 90,
    ) -> dict[str, Any]:
        if daily_limit <= 0:
            return self.create_or_get_generation_job(
                athlete_id=athlete_id,
                client_request_id=client_request_id,
                source=source,
                request_payload=request_payload,
                plan_id=plan_id,
                intake_id=intake_id,
                stale_after_seconds=stale_after_seconds,
            )

        _guard_persisted_json(
            request_payload,
            field="request_payload",
            max_bytes=MAX_SERVER_JSON_BYTES,
            context=f"athlete_id={athlete_id} client_request_id={client_request_id}",
        )
        payload_hash = _stable_payload_hash(request_payload)
        self._fail_stale_active_generation_jobs_for_athlete(
            athlete_id,
            stale_after_seconds=stale_after_seconds,
            exclude_client_request_id=client_request_id,
        )

        # Recover stale `running` jobs before the atomic RPC runs. The RPC's
        # in-flight guard checks `status in ('queued', 'running')` purely at the
        # SQL level and has no staleness awareness, so without this a stale
        # `running` row left by a crashed worker would raise
        # `generation_job_in_flight` and permanently block new generation
        # requests. This mirrors the recovery that create_or_get_generation_job
        # performs via reconcile_active_generation_job_for_athlete; the requeue/fail
        # mutations land before the RPC re-checks in-flight state atomically.
        self.reconcile_active_generation_job_for_athlete(
            athlete_id,
            stale_after_seconds=stale_after_seconds,
        )

        try:
            response = self._run_with_transient_retry(
                operation=f"create_or_get_generation_job_with_daily_limit athlete_id={athlete_id}",
                fn=lambda: self.client.rpc(
                    "create_generation_job_with_daily_limit",
                    {
                        "p_athlete_id": athlete_id,
                        "p_client_request_id": client_request_id,
                        "p_source": source,
                        "p_request_payload": request_payload,
                        "p_payload_hash": payload_hash,
                        "p_daily_limit": daily_limit,
                        "p_day_start": day_start_iso,
                        "p_counted_sources": sorted(counted_sources),
                        "p_plan_id": plan_id,
                        "p_intake_id": intake_id,
                    },
                ).execute(),
            )
        except _STORE_CLIENT_ERRORS as exc:
            error_text = _sanitize_error_text(exc)
            if "generation_job_in_flight" in error_text:
                raise generation_already_in_flight_error() from exc
            if self._is_generation_job_schema_error(exc):
                logger.exception(
                    "[store] create_or_get_generation_job_with_daily_limit:schema_mismatch athlete_id=%s client_request_id=%s",
                    athlete_id,
                    client_request_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=GENERATION_JOB_SCHEMA_DETAIL,
                ) from exc
            if self._is_generation_job_conflict_error(exc):
                raise generation_already_in_flight_error() from exc
            self._raise_operation_http_error(
                operation=f"create_or_get_generation_job_with_daily_limit athlete_id={athlete_id}",
                detail="failed to persist generation job",
                exc=exc,
            )

        payload = getattr(response, "data", None)
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="invalid daily generation limit response",
            )
        if payload.get("limit_exceeded") is True:
            raise generation_daily_limit_error(limit_reached_detail)
        job = payload.get("job")
        if not isinstance(job, dict):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="invalid daily generation limit response",
            )
        _raise_client_request_payload_mismatch_if_known(job, payload_hash)
        return job

    def count_generation_jobs_for_athlete_since(
        self,
        athlete_id: str,
        since_timestamp: str,
        *,
        sources: set[str] | None = None,
    ) -> int:
        try:
            query = self.client.table("generation_jobs").select("id", count="exact").eq("athlete_id", athlete_id).gte("created_at", since_timestamp).limit(0)
            if sources:
                query = query.in_("source", sorted(sources))
            response = self._run_with_transient_retry(
                operation=f"count_generation_jobs_for_athlete_since athlete_id={athlete_id}",
                fn=lambda: query.execute(),
            )
            count = getattr(response, "count", None)
            return int(count or 0)
        except _STORE_CLIENT_ERRORS as exc:
            if self._is_generation_job_schema_error(exc):
                logger.exception(
                    "[store] count_generation_jobs_for_athlete_since:schema_mismatch athlete_id=%s",
                    athlete_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=GENERATION_JOB_SCHEMA_DETAIL,
                ) from exc
            self._raise_operation_http_error(
                operation=f"count_generation_jobs_for_athlete_since athlete_id={athlete_id}",
                detail="failed to count generation jobs",
                exc=exc,
            )

    def check_plan_generation_short_window_limit(
        self,
        athlete_id: str,
        max_requests: int,
        window_seconds: float,
    ) -> tuple[bool, int]:
        try:
            response = self._run_with_transient_retry(
                operation=f"check_plan_generation_short_window_limit athlete_id={athlete_id}",
                fn=lambda: self.client.rpc(
                    "check_plan_generation_short_window_limit",
                    {
                        "p_athlete_id": athlete_id,
                        "p_max_requests": max_requests,
                        "p_window_seconds": window_seconds,
                    },
                ).execute(),
            )
            payload = getattr(response, "data", None)
            if isinstance(payload, list):
                payload = payload[0] if payload else {}
            if not isinstance(payload, dict):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="invalid short-window rate limit response",
                )
            allowed = payload.get("allowed")
            retry_after_seconds = payload.get("retry_after_seconds")
            if not isinstance(allowed, bool) or not isinstance(retry_after_seconds, int):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="invalid short-window rate limit response",
                )
            return allowed, max(0, retry_after_seconds)
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"check_plan_generation_short_window_limit athlete_id={athlete_id}",
                detail="failed to enforce short-window rate limit",
                exc=exc,
            )

    def get_generation_job(self, job_id: str) -> dict[str, Any] | None:
        """Pure read. Never mutates job state.

        Stale ``running`` jobs are recovered separately via
        :meth:`recover_generation_job_if_stale` so that callers performing a
        plain lookup (including read-after-write refreshes) cannot trigger
        hidden status changes.
        """
        try:
            return self._run_with_transient_retry(
                operation="get_generation_job:select",
                fn=lambda: self._read_generation_job(job_id),
            )
        except _STORE_CLIENT_ERRORS as exc:
            if self._is_transient_store_error(exc):
                logger.warning(
                    "[store] get_generation_job:transient_failure job_id=%s error_type=%s",
                    job_id,
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=GENERATION_JOB_UNAVAILABLE_DETAIL,
                ) from exc
            if self._is_generation_job_schema_error(exc):
                logger.exception("[store] get_generation_job:schema_mismatch job_id=%s", job_id)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=GENERATION_JOB_SCHEMA_DETAIL,
                ) from exc
            logger.exception("[store] get_generation_job:exception job_id=%s", job_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to load generation job",
            ) from exc

    def recover_generation_job_if_stale(self, job: dict[str, Any] | None) -> dict[str, Any] | None:
        """Requeue, fail, or recover a stale ``running`` job and return it refreshed.

        This is the explicit, mutating counterpart to :meth:`get_generation_job`.
        Non-running jobs (and ``None``) are returned unchanged.
        """
        if not job or str(job.get("status") or "") != "running":
            return job
        job_id = str(job.get("id") or "")
        try:
            staleness = self._classify_running_job_staleness(
                job,
                stale_after_seconds=generation_job_stale_after_seconds(),
                stage1_stale_after_seconds=_stage1_stale_after_seconds_for_reads(),
            )
            if staleness == "job_loaded_stalled":
                attempt_count = int(job.get("attempt_count") or 0)
                now_iso = _utc_now_iso()
                milestones = _progress_milestones(job.get("progress_milestones"))
                if attempt_count < _generation_startup_max_attempts():
                    milestones.append(
                        {
                            "code": "worker_claim_stalled_requeued",
                            "label": "Worker claim stalled",
                            "detail": "Worker loaded the generation job but did not reach request parsing; job was requeued for recovery.",
                            "meta": {},
                            "at": now_iso,
                        }
                    )
                    self._run_with_transient_retry(
                        operation="recover_generation_job_if_stale:requeue_job_loaded_stalled",
                        fn=lambda: self.client.table("generation_jobs")
                        .update(
                            {
                                "status": "queued",
                                "error": None,
                                "started_at": None,
                                "heartbeat_at": None,
                                "completed_at": None,
                                "progress_milestones": milestones,
                                # Back to the queue means back to unowned; the
                                # next claim records the new owner.
                                "claimed_by": None,
                                "claimed_at": None,
                            }
                        )
                        .eq("id", str(job.get("id") or ""))
                        .eq("status", "running")
                        .execute(),
                    )
                else:
                    milestones.append(
                        {
                            "code": "worker_claim_stalled_failed",
                            "label": "Worker stalled after loading job",
                            "detail": "Worker loaded the generation job but did not reach request parsing after retry.",
                            "meta": {},
                            "at": now_iso,
                        }
                    )
                    self._run_with_transient_retry(
                        operation="recover_generation_job_if_stale:fail_job_loaded_stalled",
                        fn=lambda: self.client.table("generation_jobs")
                        .update(
                            {
                                "status": "failed",
                                "error": "Generation worker stalled after loading the job.",
                                "completed_at": now_iso,
                                "heartbeat_at": now_iso,
                                "progress_milestones": milestones,
                            }
                        )
                        .eq("id", str(job.get("id") or ""))
                        .eq("status", "running")
                        .execute(),
                    )
                return self._read_generation_job(job_id)
            if staleness == "stage1_planner_stalled":
                now_iso = _utc_now_iso()
                milestones = _progress_milestones(job.get("progress_milestones"))
                if not _has_milestone_code(milestones, "stage1_planner_timeout"):
                    milestones.append(
                        {
                            "code": "stage1_planner_timeout",
                            "label": "Stage 1 planner timed out",
                            "detail": "Planner did not return after invocation and the job was failed for recovery.",
                            "meta": {},
                            "at": now_iso,
                        }
                    )
                recovered_plan_id = _plan_id_from_terminal_milestones(job)
                recovered_status = "review_required" if _should_recover_stalled_job(job) else "failed"
                if recovered_status == "review_required":
                    milestones.append(
                        {
                            "code": "stalled_job_recovered",
                            "label": "Stalled job recovered",
                            "detail": "Recovered as review required because usable output was already persisted.",
                            "meta": {"recovery_reason": "persisted_output"},
                            "at": now_iso,
                        }
                    )
                self._run_with_transient_retry(
                    operation="recover_generation_job_if_stale:resolve_stage1_stalled",
                    fn=lambda: self.client.table("generation_jobs")
                    .update(
                        {
                            "status": recovered_status,
                            "error": None if recovered_status == "review_required" else "Stage 1 planner stalled after planner invocation.",
                            "completed_at": now_iso,
                            "heartbeat_at": now_iso,
                            "progress_milestones": milestones,
                            "plan_id": recovered_plan_id or str(job.get("plan_id") or "") or None,
                        }
                    )
                    .eq("id", str(job.get("id") or ""))
                    .eq("status", "running")
                    .execute(),
                )
                return self._read_generation_job(job_id)
            return job
        except _STORE_CLIENT_ERRORS as exc:
            if self._is_transient_store_error(exc):
                logger.warning(
                    "[store] recover_generation_job_if_stale:transient_failure job_id=%s error_type=%s",
                    job_id,
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=GENERATION_JOB_UNAVAILABLE_DETAIL,
                ) from exc
            if self._is_generation_job_schema_error(exc):
                logger.exception("[store] recover_generation_job_if_stale:schema_mismatch job_id=%s", job_id)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=GENERATION_JOB_SCHEMA_DETAIL,
                ) from exc
            logger.exception("[store] recover_generation_job_if_stale:exception job_id=%s", job_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to recover generation job",
            ) from exc

    def get_generation_job_by_client_request_id(self, *, athlete_id: str, client_request_id: str) -> dict[str, Any] | None:
        try:
            return self._run_with_transient_retry(
                operation=f"get_generation_job_by_client_request_id athlete_id={athlete_id}",
                fn=lambda: self._lookup_generation_job_by_client_request_id(
                    athlete_id=athlete_id,
                    client_request_id=client_request_id,
                ),
            )
        except _STORE_CLIENT_ERRORS as exc:
            if self._is_transient_store_error(exc):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=GENERATION_JOB_UNAVAILABLE_DETAIL,
                ) from exc
            if self._is_generation_job_schema_error(exc):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=GENERATION_JOB_SCHEMA_DETAIL,
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to load generation job",
            ) from exc

    def get_visible_active_generation_job_for_athlete(self, athlete_id: str) -> dict[str, Any] | None:
        """Pure read of the latest queued/running generation job for polling endpoints."""
        try:
            response = self._run_with_transient_retry(
                operation=f"get_visible_active_generation_job_for_athlete athlete_id={athlete_id}",
                fn=lambda: self.client.table("generation_jobs")
                .select(GENERATION_JOB_SELECT)
                .eq("athlete_id", athlete_id)
                .in_("status", ["queued", "running"])
                .order("created_at", desc=True)
                .limit(1)
                .execute(),
            )
            rows = [row for row in (getattr(response, "data", None) or []) if isinstance(row, dict)]
            return rows[0] if rows else None
        except _STORE_CLIENT_ERRORS as exc:
            if self._is_transient_store_error(exc):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=GENERATION_JOB_UNAVAILABLE_DETAIL,
                ) from exc
            if self._is_generation_job_schema_error(exc):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=GENERATION_JOB_SCHEMA_DETAIL,
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to load generation job",
            ) from exc

    def reconcile_active_generation_job_for_athlete(
        self,
        athlete_id: str,
        *,
        stale_after_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        """Reconcile (requeue/fail/recover) the athlete's stale ``running`` jobs.

        This is an explicit **write/control** path — it mutates job state — and
        must only be called from write/reconciliation flows (job creation,
        retry, daily-limit create). Polling/read endpoints must use
        :meth:`get_visible_active_generation_job_for_athlete` or
        :meth:`get_generation_job`, which never mutate. It returns the active
        queued/running job (after recovery) or a job recovered to a terminal
        state so callers can decide whether a new request is in flight.
        """
        if stale_after_seconds is None:
            stale_after_seconds = generation_job_stale_after_seconds()
        try:
            response = self._run_with_transient_retry(
                operation=f"reconcile_active_generation_job_for_athlete athlete_id={athlete_id}",
                fn=lambda: self.client.table("generation_jobs")
                .select(GENERATION_JOB_SELECT)
                .eq("athlete_id", athlete_id)
                .in_("status", ["queued", "running"])
                .order("created_at", desc=True)
                .limit(10)
                .execute(),
            )
            rows = [row for row in (getattr(response, "data", None) or []) if isinstance(row, dict)]
            for row in rows:
                if str(row.get("status") or "") == "queued":
                    return row
                if str(row.get("status") or "") != "running":
                    continue
                staleness = self._classify_running_job_staleness(
                    row,
                    stale_after_seconds=stale_after_seconds,
                    stage1_stale_after_seconds=_stage1_stale_after_seconds_for_reads(),
                )
                if staleness == "fresh":
                    return row
                if staleness == "startup_stale":
                    self._run_with_transient_retry(
                        operation="reconcile_active_generation_job_for_athlete:reset_startup_stale",
                        fn=lambda: self.client.table("generation_jobs")
                        .update(
                            {
                                "status": "queued",
                                "error": None,
                                "heartbeat_at": None,
                                "started_at": None,
                                "completed_at": None,
                                "stage1_result": None,
                                "final_result": None,
                                "progress_milestones": [],
                            }
                        )
                        .eq("id", str(row.get("id") or ""))
                        .eq("status", "running")
                        .execute(),
                    )
                elif staleness == "job_loaded_stalled":
                    attempt_count = int(row.get("attempt_count") or 0)
                    now_iso = _utc_now_iso()
                    milestones = _progress_milestones(row.get("progress_milestones"))
                    if attempt_count < _generation_startup_max_attempts():
                        milestones.append(
                            {
                                "code": "worker_claim_stalled_requeued",
                                "label": "Worker claim stalled",
                                "detail": "Worker loaded the generation job but did not reach request parsing; job was requeued for recovery.",
                                "meta": {},
                                "at": now_iso,
                            }
                        )
                        self._run_with_transient_retry(
                            operation="reconcile_active_generation_job_for_athlete:requeue_job_loaded_stalled",
                            fn=lambda: self.client.table("generation_jobs")
                            .update(
                                {
                                    "status": "queued",
                                    "error": None,
                                    "heartbeat_at": None,
                                    "started_at": None,
                                    "completed_at": None,
                                    "progress_milestones": milestones,
                                }
                            )
                            .eq("id", str(row.get("id") or ""))
                            .eq("status", "running")
                            .execute(),
                        )
                    else:
                        milestones.append(
                            {
                                "code": "worker_claim_stalled_failed",
                                "label": "Worker stalled after loading job",
                                "detail": "Worker loaded the generation job but did not reach request parsing after retry.",
                                "meta": {},
                                "at": now_iso,
                            }
                        )
                        self._run_with_transient_retry(
                            operation="reconcile_active_generation_job_for_athlete:fail_job_loaded_stalled",
                            fn=lambda: self.client.table("generation_jobs")
                            .update(
                                {
                                    "status": "failed",
                                    "error": "Generation worker stalled after loading the job.",
                                    "completed_at": now_iso,
                                    "heartbeat_at": now_iso,
                                    "progress_milestones": milestones,
                                }
                            )
                            .eq("id", str(row.get("id") or ""))
                            .eq("status", "running")
                            .execute(),
                        )
                elif staleness == "stage1_planner_stalled":
                    now_iso = _utc_now_iso()
                    milestones = _progress_milestones(row.get("progress_milestones"))
                    if not _has_milestone_code(milestones, "stage1_planner_timeout"):
                        milestones.append(
                            {
                                "code": "stage1_planner_timeout",
                                "label": "Stage 1 planner timed out",
                                "detail": "Planner did not return after invocation and the job was failed for recovery.",
                                "meta": {},
                            "at": now_iso,
                            }
                        )
                    recovered_plan_id = _plan_id_from_terminal_milestones(row)
                    recovered_status = "review_required" if _should_recover_stalled_job(row) else "failed"
                    if recovered_status == "review_required":
                        milestones.append(
                            {
                                "code": "stalled_job_recovered",
                                "label": "Stalled job recovered",
                                "detail": "Recovered as review required because usable output was already persisted.",
                                "meta": {"recovery_reason": "persisted_output"},
                                "at": now_iso,
                            }
                        )
                    self._run_with_transient_retry(
                        operation="reconcile_active_generation_job_for_athlete:resolve_stage1_stalled",
                        fn=lambda: self.client.table("generation_jobs")
                        .update(
                            {
                                "status": recovered_status,
                                "error": None if recovered_status == "review_required" else "Stage 1 planner stalled after planner invocation.",
                                "completed_at": now_iso,
                                "heartbeat_at": now_iso,
                                "progress_milestones": milestones,
                                "plan_id": recovered_plan_id or str(row.get("plan_id") or "") or None,
                            }
                        )
                        .eq("id", str(row.get("id") or ""))
                        .eq("status", "running")
                        .execute(),
                    )
                else:
                    now_iso = _utc_now_iso()
                    milestones = _progress_milestones(row.get("progress_milestones"))
                    recovered_plan_id = _plan_id_from_terminal_milestones(row)
                    recovered_status = "review_required" if _should_recover_stalled_job(row) else "failed"
                    if recovered_status == "review_required":
                        milestones.append(
                            {
                                "code": "stalled_job_recovered",
                                "label": "Stalled job recovered",
                                "detail": "Recovered as review required because usable output was already persisted.",
                                "meta": {"recovery_reason": "persisted_output"},
                                "at": now_iso,
                            }
                        )
                    self._run_with_transient_retry(
                        operation="reconcile_active_generation_job_for_athlete:resolve_mid_pipeline_stale",
                        fn=lambda: self.client.table("generation_jobs")
                        .update(
                            {
                                "status": recovered_status,
                                "error": None if recovered_status == "review_required" else "Generation job stalled mid-pipeline and was failed for recovery.",
                                "completed_at": now_iso,
                                "heartbeat_at": now_iso,
                                "progress_milestones": milestones,
                                "plan_id": recovered_plan_id or str(row.get("plan_id") or "") or None,
                            }
                        )
                        .eq("id", str(row.get("id") or ""))
                        .eq("status", "running")
                        .execute(),
                    )
                refreshed = self.get_generation_job(str(row.get("id") or ""))
                if refreshed and str(refreshed.get("status") or "") in {"queued", "failed", "review_required"}:
                    return refreshed
            return None
        except _STORE_CLIENT_ERRORS as exc:
            if self._is_transient_store_error(exc):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=GENERATION_JOB_UNAVAILABLE_DETAIL,
                ) from exc
            if self._is_generation_job_schema_error(exc):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=GENERATION_JOB_SCHEMA_DETAIL,
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to load generation job",
            ) from exc

    def get_generation_job_by_plan_id(self, plan_id: str) -> dict[str, Any] | None:
        # Soft-fails to None so plan detail loads even if generation_jobs is unavailable —
        # the plan_source field is non-critical (banner-only).
        try:
            return self._select_first(
                self.client.table("generation_jobs")
                .select(GENERATION_JOB_SELECT)
                .eq("plan_id", plan_id)
                .order("completed_at", desc=True)
            )
        except _STORE_CLIENT_ERRORS:
            logger.exception("[store] get_generation_job_by_plan_id:exception plan_id=%s", plan_id)
            return None

    def get_latest_generation_job_for_athlete(self, athlete_id: str) -> dict[str, Any] | None:
        try:
            response = self._run_with_transient_retry(
                operation=f"get_latest_generation_job_for_athlete athlete_id={athlete_id}",
                fn=lambda: self.client.table("generation_jobs")
                .select(GENERATION_JOB_SELECT)
                .eq("athlete_id", athlete_id)
                .order("created_at", desc=True)
                .limit(1)
                .execute(),
            )
            rows = response.data or []
            if not rows:
                return None
            row = rows[0]
            return row if isinstance(row, dict) else None
        except _STORE_CLIENT_ERRORS as exc:
            if self._is_transient_store_error(exc):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=GENERATION_JOB_UNAVAILABLE_DETAIL,
                ) from exc
            if self._is_generation_job_schema_error(exc):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=GENERATION_JOB_SCHEMA_DETAIL,
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to load generation job",
            ) from exc

    def has_active_generation_job_for_plan(self, plan_id: str) -> bool:
        try:
            response = self._run_with_transient_retry(
                operation=f"has_active_generation_job_for_plan plan_id={plan_id}",
                fn=lambda: self.client.table("generation_jobs")
                .select("id")
                .eq("plan_id", plan_id)
                .in_("status", ["queued", "running"])
                .limit(1)
                .execute(),
            )
            return bool(response.data)
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"has_active_generation_job_for_plan plan_id={plan_id}",
                detail="failed to check active generation jobs",
                exc=exc,
            )

    def list_generation_jobs_for_athlete(self, athlete_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        try:
            response = self._run_with_transient_retry(
                operation=f"list_generation_jobs_for_athlete athlete_id={athlete_id}",
                fn=lambda: self.client.table("generation_jobs")
                .select(GENERATION_JOB_ADMIN_LIST_SELECT)
                .eq("athlete_id", athlete_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute(),
            )
            return [row for row in (response.data or []) if isinstance(row, dict)]
        except _STORE_CLIENT_ERRORS as exc:
            if self._is_generation_job_schema_error(exc):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=GENERATION_JOB_SCHEMA_DETAIL,
                ) from exc
            self._raise_operation_http_error(
                operation=f"list_generation_jobs_for_athlete athlete_id={athlete_id}",
                detail="failed to list generation jobs",
                exc=exc,
            )
        return []

    def list_admin_triage_generation_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        try:
            response = self._run_with_transient_retry(
                operation=f"list_admin_triage_generation_jobs limit={limit}",
                fn=lambda: self.client.table("generation_jobs")
                .select(GENERATION_JOB_ADMIN_TRIAGE_SELECT)
                .eq("status", "review_required")
                .is_("plan_id", "null")
                .order("created_at", desc=True)
                .limit(limit)
                .execute(),
            )
            rows = [row for row in (response.data or []) if isinstance(row, dict)]
            return self._attach_profile_contacts(rows)
        except _STORE_CLIENT_ERRORS as exc:
            if self._is_generation_job_schema_error(exc):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=GENERATION_JOB_SCHEMA_DETAIL,
                ) from exc
            self._raise_operation_http_error(
                operation=f"list_admin_triage_generation_jobs limit={limit}",
                detail="failed to list triage generation jobs",
                exc=exc,
            )
        return []

    def list_admin_active_generation_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        try:
            response = self._run_with_transient_retry(
                operation=f"list_admin_active_generation_jobs limit={limit}",
                fn=lambda: self.client.table("generation_jobs")
                .select(GENERATION_JOB_ADMIN_ACTIVE_SELECT)
                .in_("status", ["queued", "running"])
                .order("created_at", desc=True)
                .limit(limit)
                .execute(),
            )
            rows = [row for row in (response.data or []) if isinstance(row, dict)]
            return self._attach_profile_contacts(rows)
        except _STORE_CLIENT_ERRORS as exc:
            if self._is_generation_job_schema_error(exc):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=GENERATION_JOB_SCHEMA_DETAIL,
                ) from exc
            self._raise_operation_http_error(
                operation=f"list_admin_active_generation_jobs limit={limit}",
                detail="failed to list active generation jobs",
                exc=exc,
            )
        return []

    def list_orphaned_terminal_generation_jobs(self, *, limit: int = 500) -> list[dict[str, Any]]:
        try:
            response = self._run_with_transient_retry(
                operation=f"list_orphaned_terminal_generation_jobs limit={limit}",
                fn=lambda: self.client.table("generation_jobs")
                .select("id, athlete_id, status, source, plan_id, plans!left(id)")
                .in_("status", ["completed", "review_required"])
                .is_("plans.id", "null")
                .order("updated_at", desc=True)
                .limit(limit)
                .execute(),
            )
            rows = [row for row in (response.data or []) if isinstance(row, dict)]
            orphaned: list[dict[str, Any]] = []
            for row in rows:
                plan_ref = row.get("plans")
                has_plan = isinstance(plan_ref, dict) and bool(str(plan_ref.get("id") or "").strip())
                if not has_plan:
                    orphaned.append(
                        {
                            "job_id": str(row.get("id") or ""),
                            "athlete_id": str(row.get("athlete_id") or ""),
                            "status": str(row.get("status") or ""),
                            "source": str(row.get("source") or ""),
                            "plan_id": str(row.get("plan_id") or ""),
                        }
                    )
            return orphaned
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"list_orphaned_terminal_generation_jobs limit={limit}",
                detail="failed to list orphaned terminal generation jobs",
                exc=exc,
            )
        return []

    def list_failed_triage_resume_jobs_with_approved_marker(self, *, limit: int = 500) -> list[dict[str, Any]]:
        try:
            response = self._run_with_transient_retry(
                operation=f"list_failed_triage_resume_jobs_with_approved_marker limit={limit}",
                fn=lambda: self.client.table("generation_jobs")
                .select("id, athlete_id, status, source, plan_id, plans!inner(id,stage2_status)")
                .eq("status", "failed")
                .eq("source", "admin_triage_resume")
                .eq("plans.stage2_status", "triage_resume_approved")
                .order("updated_at", desc=True)
                .limit(limit)
                .execute(),
            )
            rows = [row for row in (response.data or []) if isinstance(row, dict)]
            findings: list[dict[str, Any]] = []
            for row in rows:
                linked_plan = row.get("plans") if isinstance(row.get("plans"), dict) else {}
                findings.append(
                    {
                        "job_id": str(row.get("id") or ""),
                        "plan_id": str(linked_plan.get("id") or row.get("plan_id") or ""),
                        "athlete_id": str(row.get("athlete_id") or ""),
                    }
                )
            return findings
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"list_failed_triage_resume_jobs_with_approved_marker limit={limit}",
                detail="failed to list failed triage resume jobs with approved marker",
                exc=exc,
            )
        return []

    def list_claimable_generation_jobs(self, *, limit: int = 20, stale_after_seconds: int | None = None) -> list[dict[str, Any]]:
        if stale_after_seconds is None:
            stale_after_seconds = generation_job_stale_after_seconds()
        try:
            cutoff_iso = (
                datetime.now(timezone.utc) - timedelta(seconds=max(1, stale_after_seconds))
            ).isoformat()
            queued_response = self._run_with_transient_retry(
                operation="list_claimable_generation_jobs:select_queued",
                fn=lambda: self.client.table("generation_jobs")
                .select(GENERATION_JOB_SELECT)
                .eq("status", "queued")
                .order("created_at", desc=False)
                .limit(limit)
                .execute(),
            )
            legacy_status_responses: list[Any] = []
            if _claim_legacy_blank_status_jobs_enabled():
                legacy_status_responses = [
                    self._run_with_transient_retry(
                        operation="list_claimable_generation_jobs:select_null_status",
                        fn=lambda: self.client.table("generation_jobs")
                        .select(GENERATION_JOB_SELECT)
                        .is_("status", "null")
                        .order("created_at", desc=False)
                        .limit(limit)
                        .execute(),
                    ),
                    self._run_with_transient_retry(
                        operation="list_claimable_generation_jobs:select_blank_status",
                        fn=lambda: self.client.table("generation_jobs")
                        .select(GENERATION_JOB_SELECT)
                        .eq("status", "")
                        .order("created_at", desc=False)
                        .limit(limit)
                        .execute(),
                    ),
                ]
            stale_heartbeat_response = self._run_with_transient_retry(
                operation="list_claimable_generation_jobs:select_running_stale_heartbeat",
                fn=lambda: self.client.table("generation_jobs")
                .select(GENERATION_JOB_SELECT)
                .eq("status", "running")
                .lte("heartbeat_at", cutoff_iso)
                .order("created_at", desc=False)
                .limit(limit)
                .execute(),
            )
            stale_without_heartbeat_response = self._run_with_transient_retry(
                operation="list_claimable_generation_jobs:select_running_stale_started",
                fn=lambda: self.client.table("generation_jobs")
                .select(GENERATION_JOB_SELECT)
                .eq("status", "running")
                .is_("heartbeat_at", "null")
                .lte("started_at", cutoff_iso)
                .order("created_at", desc=False)
                .limit(limit)
                .execute(),
            )

            merged_rows: dict[str, dict[str, Any]] = {}

            # Queued jobs are always claimable. Legacy blank/null status scans
            # are opt-in only; the migration normalizes old rows and the hot
            # worker loop should stay on indexed canonical statuses.
            for response in (queued_response, *legacy_status_responses):
                for row in response.data or []:
                    if not isinstance(row, dict):
                        continue
                    row_id = str(row.get("id") or "")
                    if not row_id:
                        continue
                    merged_rows[row_id] = dict(row)

            # Running jobs are only claimable if they are startup-stale.
            for response in (stale_heartbeat_response, stale_without_heartbeat_response):
                for row in response.data or []:
                    if not isinstance(row, dict):
                        continue
                    row_id = str(row.get("id") or "")
                    if not row_id:
                        continue
                    if not is_startup_stale_generation_job(
                        row,
                        stale_after_seconds=stale_after_seconds,
                    ):
                        continue
                    merged_rows[row_id] = dict(row)
            return sorted(merged_rows.values(), key=lambda row: str(row.get("created_at") or ""))[:limit]
        except _STORE_CLIENT_ERRORS as exc:
            if self._is_transient_store_error(exc):
                logger.warning(
                    "[store] list_claimable_generation_jobs:transient_failure error_type=%s",
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=GENERATION_JOB_UNAVAILABLE_DETAIL,
                ) from exc
            if self._is_generation_job_schema_error(exc):
                logger.exception("[store] list_claimable_generation_jobs:schema_mismatch")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=GENERATION_JOB_SCHEMA_DETAIL,
                ) from exc
            logger.exception("[store] list_claimable_generation_jobs:exception")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to list generation jobs",
            ) from exc

    def _fail_stalled_job_loaded_job(
        self, job_id: str, job: dict[str, Any], now_iso: str
    ) -> None:
        """Mark a ``job_loaded``-stalled job failed once its retry budget is spent.

        Mirrors the read-side recovery in ``recover_generation_job_if_stale`` so the
        terminal status, error, and milestone are identical regardless of which path
        observes the exhausted attempt budget.
        """
        milestones = _progress_milestones(job.get("progress_milestones"))
        milestones.append(
            {
                "code": "worker_claim_stalled_failed",
                "label": "Worker stalled after loading job",
                "detail": "Worker loaded the generation job but did not reach request parsing after retry.",
                "meta": {},
                "at": now_iso,
            }
        )
        # Optimistic lock: only fail the exact row we read. Guarding on
        # attempt_count blocks a concurrent worker that re-claimed the job (the
        # claim bumps attempt_count), and guarding on heartbeat_at blocks a job
        # whose heartbeat advanced after our read — i.e. a worker that is in fact
        # alive. Either change means the row moved on, so the update no-ops and the
        # next pass re-evaluates instead of clobbering live state.
        expected_attempt_count = int(job.get("attempt_count") or 0)

        self.fail_generation_job(
            job_id,
            expected_status="running",
            expected_attempt_count=expected_attempt_count,
            error="Generation worker stalled after loading the job.",
            progress_milestones=milestones,
            failed_at=now_iso,
            heartbeat_at=now_iso,
            # The stalled job is owned by a dead worker, not this process; the
            # status + attempt_count guards above are the protection here.
            enforce_worker_ownership=False,
        )

    def claim_generation_job_start(self, job_id: str, *, stale_after_seconds: int | None = None, worker_id: str | None = None) -> dict[str, Any] | None:
        if stale_after_seconds is None:
            stale_after_seconds = generation_job_stale_after_seconds()
        try:
            job = self.get_generation_job(job_id)
            if not job:
                return None

            current_status = str(job.get("status") or "").strip().lower() or "queued"
            current_attempt_count = int(job.get("attempt_count") or 0)
            now_iso = _utc_now_iso()

            if current_status not in {"queued", "running"}:
                return None
            if current_status == "running" and not is_startup_stale_generation_job(
                job,
                stale_after_seconds=stale_after_seconds,
            ):
                return None
            # The worker reclaims startup-stale ``running`` jobs directly through this
            # path. Jobs stalled at ``job_loaded`` (claimed, then dead before request
            # parsing) are retry-capped by the read-side recovery; enforce the same cap
            # here so a repeatedly-dying worker cannot re-grab the job forever, bumping
            # attempt_count without bound. Once the budget is spent, fail the job
            # instead of reclaiming it.
            if (
                current_status == "running"
                and current_attempt_count >= _generation_startup_max_attempts()
                and is_job_loaded_stalled_generation_job(
                    job,
                    stale_after_seconds=stale_after_seconds,
                )
            ):
                self._fail_stalled_job_loaded_job(job_id, job, now_iso)
                return None
            try:
                require_generation_job_transition(current_status, "running")
            except ValueError:
                return None

            # Atomic claim RPC: the status/attempt guards and the
            # running/ownership write happen in one statement, so two workers
            # racing for the same row cannot both win. A null result means the
            # row moved on (claimed elsewhere or finished) — not an error.
            payload = {
                "p_job_id": job_id,
                "p_worker_id": (worker_id or "").strip() or generation_worker_id(),
                "p_expected_status": current_status,
                "p_expected_attempt_count": current_attempt_count,
                "p_progress_milestones": [_job_loaded_milestone(now_iso)],
                "p_claimed_at": now_iso,
            }
            response = self._run_with_transient_retry(
                operation="claim_generation_job_start:rpc",
                fn=lambda: self.client.rpc("claim_generation_job", payload).execute(),
            )
            return self._terminal_generation_job_rpc_result(response)
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            if self._is_transient_store_error(exc):
                logger.warning(
                    "[store] claim_generation_job_start:transient_failure job_id=%s error_type=%s",
                    job_id,
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=GENERATION_JOB_UNAVAILABLE_DETAIL,
                ) from exc
            logger.exception("[store] claim_generation_job_start:exception job_id=%s", job_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to claim generation job",
            ) from exc

    def claim_generation_job(self, job_id: str, *, stale_after_seconds: int | None = None, worker_id: str | None = None) -> dict[str, Any] | None:
        return self.claim_generation_job_start(job_id, stale_after_seconds=stale_after_seconds, worker_id=worker_id)

    def count_active_generation_jobs(self, *, stale_after_seconds: int | None = None) -> int:
        if stale_after_seconds is None:
            stale_after_seconds = generation_job_stale_after_seconds()
        try:
            cutoff_iso = (
                datetime.now(timezone.utc) - timedelta(seconds=max(1, stale_after_seconds))
            ).isoformat()
            response = self._run_with_transient_retry(
                operation="count_active_generation_jobs:select_running",
                fn=lambda: self.client.table("generation_jobs")
                .select("id", count="exact")
                .eq("status", "running")
                .or_(f"heartbeat_at.gt.{cutoff_iso},and(heartbeat_at.is.null,started_at.gt.{cutoff_iso})")
                .execute(),
            )
            count_value = getattr(response, "count", None)
            return int(count_value or 0)
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            if self._is_transient_store_error(exc):
                logger.warning(
                    "[store] count_active_generation_jobs:transient_failure error_type=%s",
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=GENERATION_JOB_UNAVAILABLE_DETAIL,
                ) from exc
            if self._is_generation_job_schema_error(exc):
                logger.exception("[store] count_active_generation_jobs:schema_mismatch")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=GENERATION_JOB_SCHEMA_DETAIL,
                ) from exc
            logger.exception("[store] count_active_generation_jobs:exception")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to count active generation jobs",
            ) from exc

    def _terminal_generation_job_rpc_result(self, response: Any) -> dict[str, Any] | None:
        data = getattr(response, "data", None)
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return None

    def _handle_terminal_generation_job_error(
        self,
        *,
        exc: Exception,
        operation: str,
        job_id: str,
    ) -> None:
        if self._is_transient_store_error(exc):
            logger.warning(
                "[store] %s:transient_failure job_id=%s error_type=%s",
                operation,
                job_id,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=GENERATION_JOB_UNAVAILABLE_DETAIL,
            ) from exc
        if self._is_generation_job_terminal_missing_error(exc):
            raise generation_job_not_found_error() from exc
        if self._is_generation_job_terminal_conflict_error(exc):
            raise _status_transition_error(_sanitize_error_text(exc)) from exc
        if self._is_generation_job_schema_error(exc):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=GENERATION_JOB_SCHEMA_DETAIL,
            ) from exc

    def complete_generation_job(
        self,
        job_id: str,
        *,
        expected_attempt_count: int,
        final_status: str,
        final_result: dict[str, Any] | None = None,
        plan_id: str | None = None,
        error: str | None = None,
        completed_at: str | None = None,
        heartbeat_at: str | None = None,
        expected_status: str = "running",
        expected_worker_id: str | None = None,
        enforce_worker_ownership: bool = True,
    ) -> dict[str, Any]:
        next_status = str(final_status or "").strip().lower()
        if next_status not in {"completed", "review_required"}:
            raise _status_transition_error(f"invalid terminal generation job status: {final_status!r}")
        try:
            payload = {
                "p_job_id": job_id,
                "p_expected_status": expected_status,
                "p_expected_attempt_count": int(expected_attempt_count),
                "p_final_status": next_status,
                "p_final_result": final_result,
                "p_plan_id": plan_id,
                "p_error": error,
                "p_completed_at": completed_at,
                "p_heartbeat_at": heartbeat_at,
                "p_expected_worker_id": (
                    ((expected_worker_id or "").strip() or generation_worker_id())
                    if enforce_worker_ownership
                    else None
                ),
            }
            response = self._run_with_transient_retry(
                operation="complete_generation_job:rpc",
                fn=lambda: self.client.rpc("complete_generation_job", payload).execute(),
            )
            updated = self._terminal_generation_job_rpc_result(response)
            if not updated:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="generation job completion returned no row",
                )
            return updated
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._handle_terminal_generation_job_error(
                exc=exc,
                operation="complete_generation_job",
                job_id=job_id,
            )
            logger.exception("[store] complete_generation_job:exception job_id=%s", job_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to complete generation job",
            ) from exc

    def fail_generation_job(
        self,
        job_id: str,
        *,
        expected_attempt_count: int,
        error: str,
        final_result: dict[str, Any] | None = None,
        plan_id: str | None = None,
        progress_milestones: list[Any] | None = None,
        failed_at: str | None = None,
        heartbeat_at: str | None = None,
        expected_status: str = "running",
        expected_worker_id: str | None = None,
        enforce_worker_ownership: bool = True,
    ) -> dict[str, Any]:
        try:
            payload = {
                "p_job_id": job_id,
                "p_expected_status": expected_status,
                "p_expected_attempt_count": int(expected_attempt_count),
                "p_error": str(error or "Generation job failed."),
                "p_final_result": final_result,
                "p_plan_id": plan_id,
                "p_progress_milestones": progress_milestones,
                "p_failed_at": failed_at,
                "p_heartbeat_at": heartbeat_at,
                "p_expected_worker_id": (
                    ((expected_worker_id or "").strip() or generation_worker_id())
                    if enforce_worker_ownership
                    else None
                ),
            }
            response = self._run_with_transient_retry(
                operation="fail_generation_job:rpc",
                fn=lambda: self.client.rpc("fail_generation_job", payload).execute(),
            )
            updated = self._terminal_generation_job_rpc_result(response)
            if not updated:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="generation job failure returned no row",
                )
            return updated
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._handle_terminal_generation_job_error(
                exc=exc,
                operation="fail_generation_job",
                job_id=job_id,
            )
            logger.exception("[store] fail_generation_job:exception job_id=%s", job_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to fail generation job",
            ) from exc

    def update_generation_job(
        self, job_id: str, *, refresh: bool = True, **changes: Any
    ) -> dict[str, Any]:
        """Patch a generation job row.

        ``refresh=False`` skips the read-back and returns ``{}``. High-frequency
        writers (the progress-milestone recorder and the heartbeat loop) discard
        the returned row, and the read-back is a ``select="*"`` that pulls every
        TOASTed blob on the row (request_payload/stage1_result/final_result).
        Doing that after every write turned one progress event into a PATCH plus
        a multi-megabyte GET, which is what saturated the database on
        2026-09-15. Callers that actually use the updated row keep the default.
        """
        try:
            payload = dict(changes)
            if "status" in payload:
                next_status = str(payload.get("status") or "").strip().lower()
                if not is_generation_job_status(next_status):
                    raise _status_transition_error(f"unknown generation job status: {next_status!r}")
                existing = self._read_generation_job_status_guard(job_id)
                if not existing:
                    raise generation_job_not_found_error()
                try:
                    payload["status"] = require_generation_job_transition(existing.get("status") or "queued", next_status)
                except ValueError as exc:
                    raise _status_transition_error(str(exc)) from exc
            self._run_with_transient_retry(
                operation="update_generation_job:update",
                fn=lambda: self.client.table("generation_jobs").update(payload).eq("id", job_id).execute(),
            )
            if not refresh:
                return {}
            updated = self.get_generation_job(job_id)
            if not updated:
                raise generation_job_not_found_error()
            return updated
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            if self._is_transient_store_error(exc):
                logger.warning(
                    "[store] update_generation_job:transient_failure job_id=%s error_type=%s",
                    job_id,
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=GENERATION_JOB_UNAVAILABLE_DETAIL,
                ) from exc
            logger.exception("[store] update_generation_job:exception job_id=%s", job_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to update generation job",
            ) from exc

    def record_stage2_cost(self, job_id: str, metadata: dict[str, Any]) -> None:
        """Best-effort persistence of Stage 2 token/cost telemetry to generation_jobs.

        Telemetry only — this NEVER raises. The canonical generation outcome
        (status, plan, final_result) is already persisted via
        update_generation_job before this is called, so a schema gap (migration
        not yet applied) or a transient store error here must not fail or roll
        back the job. Only the known Stage 2 cost columns are written; any stray
        keys in ``metadata`` are dropped so the payload can never carry plan text
        or other unrelated fields into the database.
        """
        if not isinstance(metadata, dict):
            return
        payload = {
            column: metadata[column]
            for column in GENERATION_JOB_STAGE2_COST_COLUMNS
            if column in metadata
        }
        if not payload:
            return
        try:
            self.client.table("generation_jobs").update(payload).eq("id", job_id).execute()
            logger.info(
                "[store] record_stage2_cost:ok job_id=%s model=%s total_tokens=%s estimated_cost_usd=%s",
                job_id,
                payload.get("stage2_model"),
                payload.get("stage2_total_tokens"),
                payload.get("stage2_estimated_cost_usd"),
            )
        except Exception as exc:  # noqa: BLE001 - telemetry write must never break generation
            logger.warning(
                "[store] record_stage2_cost:skipped job_id=%s error_type=%s error=%s",
                job_id,
                type(exc).__name__,
                _sanitize_error_text(exc),
            )

    def rename_plan(self, plan_id: str, plan_name: str) -> dict[str, Any]:
        try:
            logger.info("[store] rename_plan:start plan_id=%s", plan_id)
            self.client.table("plans").update({"plan_name": plan_name}).eq("id", plan_id).execute()
            updated = self.get_plan(plan_id)
            if not updated:
                logger.warning("[store] rename_plan:plan_missing_after_update plan_id=%s", plan_id)
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="plan not found",
                )
            logger.info("[store] rename_plan:success plan_id=%s", plan_id)
            return updated
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"rename_plan plan_id={plan_id}",
                detail="failed to rename plan",
                exc=exc,
            )

    def rename_plan_for_athlete(self, plan_id: str, athlete_id: str, plan_name: str) -> dict[str, Any]:
        """Rename a plan only if it belongs to ``athlete_id``.

        The update is scoped by both ``id`` and ``athlete_id`` so another
        athlete's plan is untouched; the scoped re-read then yields ``None`` and
        we surface a 404 rather than leaking that the plan exists.
        """
        try:
            logger.info("[store] rename_plan_for_athlete:start plan_id=%s athlete_id=%s", plan_id, athlete_id)
            self.client.table("plans").update({"plan_name": plan_name}).eq("id", plan_id).eq(
                "athlete_id", athlete_id
            ).execute()
            updated = self.get_plan_for_athlete(plan_id, athlete_id)
            if not updated:
                logger.warning(
                    "[store] rename_plan_for_athlete:not_found plan_id=%s athlete_id=%s", plan_id, athlete_id
                )
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="plan not found",
                )
            logger.info("[store] rename_plan_for_athlete:success plan_id=%s athlete_id=%s", plan_id, athlete_id)
            return updated
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"rename_plan_for_athlete plan_id={plan_id} athlete_id={athlete_id}",
                detail="failed to rename plan",
                exc=exc,
            )

    def compare_and_set_plan_exercise_overrides(
        self, plan_id: str, athlete_id: str, *, expected_revision: int, overrides: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Replace the athlete's exercise overrides only if nobody else wrote first.

        Owner-scoped and conditional on ``exercise_overrides_revision``: the
        update matches no row when another write landed since the caller read
        the plan, and ``None`` tells the caller to re-read and decide. The
        generated card itself is never part of this write.
        """
        operation = f"compare_and_set_plan_exercise_overrides plan_id={plan_id} athlete_id={athlete_id}"
        try:
            response = (
                self.client.table("plans")
                .update({"exercise_overrides": overrides, "exercise_overrides_revision": expected_revision + 1})
                .eq("id", plan_id).eq("athlete_id", athlete_id)
                .eq("exercise_overrides_revision", expected_revision)
                .execute()
            )
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(operation=operation, detail="failed to save exercise change", exc=exc)
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    def archive_plan(self, plan_id: str) -> dict[str, Any]:
        try:
            existing = self.get_plan(plan_id)
            if not existing:
                logger.warning("[store] archive_plan:not_found plan_id=%s", plan_id)
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="plan not found",
                )
            try:
                next_status = require_plan_transition(existing.get("status") or "generated", "archived")
            except ValueError as exc:
                raise _status_transition_error(str(exc)) from exc
            logger.info("[store] archive_plan:start plan_id=%s", plan_id)
            self.client.table("plans").update({"status": next_status}).eq("id", plan_id).execute()
            updated = self.get_plan(plan_id)
            if not updated:
                logger.warning("[store] archive_plan:plan_missing_after_update plan_id=%s", plan_id)
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="plan not found",
                )
            logger.info("[store] archive_plan:success plan_id=%s", plan_id)
            return updated
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"archive_plan plan_id={plan_id}",
                detail="failed to archive plan",
                exc=exc,
            )

    def archive_plan_for_athlete(self, plan_id: str, athlete_id: str) -> dict[str, Any]:
        """Archive a plan only if it belongs to ``athlete_id`` (404 otherwise)."""
        try:
            existing = self.get_plan_for_athlete(plan_id, athlete_id)
            if not existing:
                logger.warning(
                    "[store] archive_plan_for_athlete:not_found plan_id=%s athlete_id=%s", plan_id, athlete_id
                )
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="plan not found",
                )
            try:
                next_status = require_plan_transition(existing.get("status") or "generated", "archived")
            except ValueError as exc:
                raise _status_transition_error(str(exc)) from exc
            logger.info("[store] archive_plan_for_athlete:start plan_id=%s athlete_id=%s", plan_id, athlete_id)
            self.client.table("plans").update({"status": next_status}).eq("id", plan_id).eq(
                "athlete_id", athlete_id
            ).execute()
            updated = self.get_plan_for_athlete(plan_id, athlete_id)
            if not updated:
                logger.warning(
                    "[store] archive_plan_for_athlete:plan_missing_after_update plan_id=%s athlete_id=%s",
                    plan_id,
                    athlete_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="plan not found",
                )
            logger.info("[store] archive_plan_for_athlete:success plan_id=%s athlete_id=%s", plan_id, athlete_id)
            return updated
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"archive_plan_for_athlete plan_id={plan_id} athlete_id={athlete_id}",
                detail="failed to archive plan",
                exc=exc,
            )

    def delete_plan(self, plan_id: str) -> None:
        try:
            existing = self.get_plan(plan_id)
            if not existing:
                logger.warning("[store] delete_plan:not_found plan_id=%s", plan_id)
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="plan not found",
                )
            logger.info("[store] delete_plan:start plan_id=%s", plan_id)
            self.client.table("plans").delete().eq("id", plan_id).execute()
            logger.info("[store] delete_plan:success plan_id=%s", plan_id)
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"delete_plan plan_id={plan_id}",
                detail="failed to delete plan",
                exc=exc,
            )

    def delete_plan_for_athlete(self, plan_id: str, athlete_id: str) -> None:
        """Delete a plan only if it belongs to ``athlete_id`` (404 otherwise)."""
        try:
            existing = self.get_plan_for_athlete(plan_id, athlete_id)
            if not existing:
                logger.warning(
                    "[store] delete_plan_for_athlete:not_found plan_id=%s athlete_id=%s", plan_id, athlete_id
                )
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="plan not found",
                )
            logger.info("[store] delete_plan_for_athlete:start plan_id=%s athlete_id=%s", plan_id, athlete_id)
            self.client.table("plans").delete().eq("id", plan_id).eq("athlete_id", athlete_id).execute()
            logger.info("[store] delete_plan_for_athlete:success plan_id=%s athlete_id=%s", plan_id, athlete_id)
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"delete_plan_for_athlete plan_id={plan_id} athlete_id={athlete_id}",
                detail="failed to delete plan",
                exc=exc,
            )

    def _build_plan_stage2_payload(
        self, existing: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        from .structured_plan_coaching import validate_final_coaching
        result = validate_final_coaching({**result,
            "planning_brief": result.get("planning_brief", existing.get("planning_brief"))})
        try:
            current_status = existing.get("status") or "generated"
            next_status_input = result.get("status") or current_status
            next_status = require_plan_transition(current_status, next_status_input)
        except ValueError as exc:
            raise _status_transition_error(str(exc)) from exc
        visible_plan_text = _visible_plan_text_for_status(result, status_value=next_status)
        payload = {
            "status": next_status,
            "plan_text": visible_plan_text,
            "draft_plan_text": result.get("draft_plan_text", result.get("plan_text", "")),
            "final_plan_text": result.get("final_plan_text", result.get("plan_text", "")),
            "pdf_url": result.get("pdf_url"),
            "stage2_retry_text": result.get("stage2_retry_text", ""),
            "stage2_validator_report": result.get("stage2_validator_report", {}),
            "stage2_status": result.get("stage2_status", ""),
            "stage2_attempt_count": result.get("stage2_attempt_count", 0),
        }
        for optional_field in (
            "coach_notes",
            "why_log",
            "planning_brief",
            "stage2_payload",
            "parsing_metadata",
            "stage2_handoff_text",
            "structured_plan",
            "schema_version",
        ):
            if optional_field in result:
                value = result.get(optional_field)
                if optional_field == "planning_brief":
                    value = _encode_structured_text(value)
                elif optional_field == "structured_plan":
                    value = ensure_structured_block_ids(
                        ensure_structured_session_ids(value, plan_id=existing.get("id"))
                    )
                payload[optional_field] = value
        if "stage2_payload" in payload:
            _guard_persisted_json(
                payload.get("stage2_payload"),
                field="stage2_payload",
                max_bytes=MAX_STAGE2_PAYLOAD_BYTES,
                context=f"plan_id={existing.get('id') or ''}",
            )
        if "structured_plan" in payload:
            _guard_persisted_json(
                payload.get("structured_plan"),
                field="structured_plan",
                max_bytes=MAX_SERVER_JSON_BYTES,
                context=f"plan_id={existing.get('id') or ''}",
            )
        return payload

    def update_plan_stage2(self, plan_id: str, result: dict[str, Any]) -> dict[str, Any]:
        existing = self.get_plan(plan_id)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="plan not found",
            )
        payload = self._build_plan_stage2_payload(existing, result)
        try:
            logger.info("[store] update_plan_stage2:start plan_id=%s status=%s", plan_id, payload["status"])
            self.client.table("plans").update(payload).eq("id", plan_id).execute()
            updated = self.get_plan(plan_id)
            if not updated:
                logger.warning("[store] update_plan_stage2:plan_missing_after_update plan_id=%s", plan_id)
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="plan not found",
                )
            logger.info("[store] update_plan_stage2:success plan_id=%s", plan_id)
            return updated
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"update_plan_stage2 plan_id={plan_id}",
                detail="failed to update plan stage 2",
                exc=exc,
            )

    def update_plan_stage2_if_unchanged(
        self, plan_id: str, result: dict[str, Any], expected_snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        if not expected_snapshot:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        payload = self._build_plan_stage2_payload(expected_snapshot, result)
        # Guard only on lightweight state markers, never on the multi-KB text
        # bodies. PostgREST serializes `.eq()` filters into the request URL, so
        # filtering on plan_text/draft_plan_text/final_plan_text/stage2_retry_text
        # pushes the entire plan into the query string and trips PostgREST's URL
        # length limit, which returns a bare "400 Bad Request" (plain text, not
        # JSON) and surfaces as an opaque APIError. Every write that mutates plan
        # text also advances one of these markers (status transition,
        # stage2_status, or stage2_attempt_count), so a concurrent change is still
        # detected without inflating the URL.
        guarded_fields = (
            "status",
            "stage2_status",
            "stage2_attempt_count",
        )
        try:
            logger.info(
                "[store] update_plan_stage2_if_unchanged:start plan_id=%s status=%s",
                plan_id,
                payload["status"],
            )
            query = self.client.table("plans").update(payload).eq("id", plan_id)
            for field in guarded_fields:
                expected_value = expected_snapshot.get(field)
                if expected_value is None:
                    query = query.is_(field, "null")
                else:
                    query = query.eq(field, expected_value)
            response = query.execute()
            data = getattr(response, "data", None) or []
            if not data:
                if not self.get_plan(plan_id):
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Plan changed while Stage 2 structured processing was running; reload and try again.",
                )
            updated = dict(data[0])
            logger.info("[store] update_plan_stage2_if_unchanged:success plan_id=%s", plan_id)
            return updated
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"update_plan_stage2_if_unchanged plan_id={plan_id}",
                detail="failed to conditionally update plan stage 2",
                exc=exc,
            )

    def update_plan_structured_artifacts(
        self,
        plan_id: str,
        *,
        structured_plan: dict[str, Any] | None,
        schema_version: str | None,
        stage2_validator_report: dict[str, Any],
        expected_final_plan_text: str | None = None,
    ) -> dict[str, Any]:
        """Persist only the structured-plan output columns for a plan.

        Narrow companion to :meth:`update_plan_stage2`. It writes exactly three
        fields — ``structured_plan``, ``schema_version`` and
        ``stage2_validator_report`` — and nothing else. This is what makes the
        best-effort background structured conversion safe: a slow conversion that
        started from an earlier read of the row can never clobber newer
        ``status`` / ``plan_text`` / ``draft_plan_text`` / ``final_plan_text`` /
        ``stage2_retry_text`` / ``stage2_status`` / ``stage2_attempt_count`` state
        written by a concurrent admin action (reject, archive, rename, manual
        Stage 2 edit) in the meantime. No status transition is enforced because
        the plan's lifecycle status is intentionally left untouched.

        When ``expected_final_plan_text`` is supplied it is the plan text the
        structured card was converted from. The card is only persisted if the
        row's current ``final_plan_text`` (falling back to ``plan_text``) still
        matches it. If the text changed during the async conversion/backfill the
        card is now a *stale* projection of superseded text, so the write is
        skipped and the existing row (raw-markdown fallback) is left intact.
        """

        current = None
        if expected_final_plan_text is not None:
            current = self.get_plan(plan_id)
            if not current:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="plan not found",
                )
            current_text = str(current.get("final_plan_text") or current.get("plan_text") or "")
            if current_text != str(expected_final_plan_text):
                logger.info(
                    "[store] update_plan_structured_artifacts:stale_text_skip plan_id=%s",
                    plan_id,
                )
                return current
            if structured_plan is None and current.get("structured_plan") is not None:
                logger.info(
                    "[store] update_plan_structured_artifacts:existing_card_skip plan_id=%s",
                    plan_id,
                )
                return current

        payload = {"stage2_validator_report": stage2_validator_report or {}}
        if structured_plan is not None:
            from .structured_plan_coaching import validate_final_coaching
            # Backfills use the same row already read by the stale-text guard.
            # Direct callers need its authority before numerical coaching ships.
            current = current if current is not None else self.get_plan(plan_id)
            current = current if isinstance(current, dict) else {}
            checked = validate_final_coaching({**current, "structured_plan": structured_plan})
            structured_plan = checked.get("structured_plan")
            findings = (checked.get("stage2_validator_report") or {}).get("exercise_coaching")
            if findings:
                payload["stage2_validator_report"] = {**payload["stage2_validator_report"], "exercise_coaching": findings}
            payload["structured_plan"] = ensure_structured_block_ids(
                ensure_structured_session_ids(structured_plan, plan_id=plan_id)
            )
            payload["schema_version"] = schema_version
        _guard_persisted_json(
            payload.get("structured_plan"),
            field="structured_plan",
            max_bytes=MAX_SERVER_JSON_BYTES,
            context=f"plan_id={plan_id}",
        )
        try:
            logger.info("[store] update_plan_structured_artifacts:start plan_id=%s", plan_id)
            self.client.table("plans").update(payload).eq("id", plan_id).execute()
            updated = self.get_plan(plan_id)
            if not updated:
                logger.warning(
                    "[store] update_plan_structured_artifacts:plan_missing_after_update plan_id=%s",
                    plan_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="plan not found",
                )
            logger.info("[store] update_plan_structured_artifacts:success plan_id=%s", plan_id)
            return updated
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"update_plan_structured_artifacts plan_id={plan_id}",
                detail="failed to update plan structured output",
                exc=exc,
            )

    def update_plan_triage_approval(
        self,
        plan_id: str,
        *,
        why_log: dict[str, Any],
        stage2_status: str,
    ) -> dict[str, Any]:
        payload = {"why_log": why_log, "stage2_status": stage2_status}
        try:
            logger.info("[store] update_plan_triage_approval:start plan_id=%s", plan_id)
            self.client.table("plans").update(payload).eq("id", plan_id).execute()
            updated = self.get_plan(plan_id)
            if not updated:
                logger.warning("[store] update_plan_triage_approval:plan_missing_after_update plan_id=%s", plan_id)
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="plan not found",
                )
            logger.info("[store] update_plan_triage_approval:success plan_id=%s", plan_id)
            return updated
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"update_plan_triage_approval plan_id={plan_id}",
                detail="failed to update triage approval",
                exc=exc,
            )

    def list_admin_plans(
        self, *, limit: int = 50, offset: int = 0, q: str | None = None
    ) -> list[dict[str, Any]]:
        query = self.client.table("plans").select(
            "id, athlete_id, full_name, fight_date, technical_style, plan_name, status, "
            "stage2_validator_report, pdf_url, created_at"
        )
        clause = _admin_search_clause(("plan_name", "full_name", "status"), q)
        if clause:
            query = query.or_(clause)
        response = (
            query.order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        rows = [row for row in (getattr(response, "data", None) or []) if isinstance(row, dict)]
        return self._attach_profile_contacts(rows)

    def list_admin_review_plans(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """List plans that are held/blocked and awaiting an admin decision.

        Filters on the persisted ``plans.status`` column (the held/review
        statuses) so a paused plan stays visible in the review queue. Profile
        enrichment is best-effort: a profiles outage degrades the rows to
        id-only instead of hiding the held plan.
        """
        response = self._run_with_transient_retry(
            operation=f"list_admin_review_plans limit={limit}",
            fn=lambda: self.client.table("plans")
            .select(
                "id, athlete_id, full_name, fight_date, technical_style, plan_name, status, "
                "stage2_validator_report, pdf_url, created_at"
            )
            .in_("status", list(ADMIN_REVIEW_PLAN_STATUSES))
            .order("created_at", desc=True)
            .limit(limit)
            .execute(),
        )
        rows = [row for row in (getattr(response, "data", None) or []) if isinstance(row, dict)]
        return self._attach_profile_contacts(rows)

    def list_plans_missing_structured_plan(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Athlete-displayable plans that have no structured card yet (backfill).

        Selects ``ready``/``publishable_with_flags`` rows where ``structured_plan``
        is NULL so the structured-plan backfill can re-attempt conversion for plans
        generated/approved before structured generation was enabled. ``plan_text``
        presence is enforced downstream by the conversion trigger, so a row with no
        approved text simply short-circuits there.
        """
        response = self._run_with_transient_retry(
            operation=f"list_plans_missing_structured_plan limit={limit}",
            fn=lambda: self.client.table("plans")
            .select("id, status, created_at")
            .in_("status", list(ATHLETE_DISPLAYABLE_PLAN_STATUSES))
            .is_("structured_plan", "null")
            .order("created_at", desc=True)
            .limit(limit)
            .execute(),
        )
        return [row for row in (getattr(response, "data", None) or []) if isinstance(row, dict)]

    def list_plans_with_orphaned_structured_card_attempt(
        self, *, limit: int = 25
    ) -> list[dict[str, Any]]:
        """Displayable plans with an in-flight card marker but no saved card.

        A structured-card conversion stamps a durable
        ``stage2_validator_report.structured_card_attempt_started_at`` marker that
        is cleared only on a terminal outcome. When the process running the
        background build dies mid-flight (e.g. a deploy swap terminates the web
        instance), the plan is left carrying that marker with ``structured_plan``
        still NULL — the admin UI shows "building" and then degrades to "failed".
        This finds those orphaned rows so the startup self-heal can re-queue the
        single deferred conversion. Marker age is intentionally not filtered: a
        stale orphan is exactly the stuck state we want to recover.
        """
        response = self._run_with_transient_retry(
            operation=f"list_plans_with_orphaned_structured_card_attempt limit={limit}",
            fn=lambda: self.client.table("plans")
            .select("id, status, created_at")
            .in_("status", list(ATHLETE_DISPLAYABLE_PLAN_STATUSES))
            .is_("structured_plan", "null")
            .filter(
                "stage2_validator_report->>structured_card_attempt_started_at",
                "not.is",
                "null",
            )
            .order("created_at", desc=True)
            .limit(limit)
            .execute(),
        )
        return [row for row in (getattr(response, "data", None) or []) if isinstance(row, dict)]

    def list_admin_athletes(
        self, *, limit: int = 50, offset: int = 0, q: str | None = None
    ) -> list[dict[str, Any]]:
        query = self.client.table("admin_athlete_rollups").select("*")
        clause = _admin_search_clause(
            ("email", "full_name", "username", "professional_status", "record_summary"), q
        )
        if clause:
            query = query.or_(clause)
        response = (
            query.order("updated_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        return getattr(response, "data", None) or []

    def list_admin_signup_activation(self, *, since: str, limit: int = 5000) -> list[dict[str, Any]]:
        """Signup and first-usable-plan timestamps for accounts created since ``since``."""
        response = (
            self.client.table("admin_athlete_rollups")
            .select("id,role,created_at,first_usable_plan_at")
            .gte("created_at", since)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return getattr(response, "data", None) or []

    def record_injury_episode_event(self, athlete_id: str, event: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.rpc("record_injury_episode_event", {"p_athlete_id": athlete_id, "p_event": event}).execute()
            data = getattr(response, "data", None)
            return data[0] if isinstance(data, list) and data else data
        except _STORE_CLIENT_ERRORS as exc:
            if any(code in str(exc) for code in ("23514", "23505")):
                raise HTTPException(status_code=409, detail="The injury response changed. Refresh Today.") from exc
            self._raise_operation_http_error(operation="record_injury_episode_event", detail="failed to record injury response", exc=exc)

    def get_clinical_review_capture_context(self, athlete_id: str, injury_id: str, episode_id: str) -> dict[str, Any]:
        """One complete locked snapshot, private to the service capture boundary."""
        try:
            response = self.client.rpc("clinical_review_capture_context", {
                "p_athlete_id": athlete_id, "p_injury_id": injury_id, "p_episode_id": episode_id}).execute()
            return response.data
        except _STORE_CLIENT_ERRORS as exc:
            if any(code in str(exc) for code in ("23514", "23503")):
                raise HTTPException(409, "The target injury episode changed.") from exc
            self._raise_operation_http_error(operation="clinical_review_capture_context", detail="failed to read clinical review context", exc=exc)

    def record_clinical_review_event(self, athlete_id: str, recorder_id: str, context: dict,
                                    event: dict, supersession: dict | None) -> dict[str, Any]:
        try:
            response = self.client.rpc("record_clinical_review_event", {
                "p_athlete_id": athlete_id, "p_recorder_id": recorder_id, "p_context": context,
                "p_event": event, "p_supersession": supersession}).execute()
            data = response.data
            return data[0] if isinstance(data, list) and data else data
        except _STORE_CLIENT_ERRORS as exc:
            if any(code in str(exc) for code in ("23514", "23505", "22023")):
                raise HTTPException(409, "Clinical review context or request changed. Refresh the evidence packet.") from exc
            if "42501" in str(exc):
                raise HTTPException(403, "Clinical review recorder or target access is no longer allowed.") from exc
            self._raise_operation_http_error(operation="record_clinical_review_event", detail="failed to record clinical review", exc=exc)

    def list_injury_episode_events(self, athlete_id: str, *, injury_id: str, injury_episode_id: str) -> list[dict[str, Any]]:
        from api.contracts.rehab_assessment import AssessmentHistory
        # Keyset pagination keeps older concerns visible even while new reports
        # arrive. Never treat a PostgREST default page as complete episode history.
        rows, before = [], None
        while True:
            query = (self.client.table("injury_episode_events").select("*").eq("athlete_id", athlete_id)
                     .eq("injury_id", injury_id).eq("injury_episode_id", injury_episode_id)
                     .order("created_at", desc=True).order("id", desc=True).limit(200))
            if before:
                stamp, identity = before
                query = query.or_(f"created_at.lt.{stamp},and(created_at.eq.{stamp},id.lt.{identity})")
            page = getattr(query.execute(), "data", None) or []
            rows.extend(page)
            if not page:
                return AssessmentHistory(rows, history_complete=True)
            before = (page[-1]["created_at"], page[-1]["id"])

    def list_pending_delayed_rehab(self, athlete_id: str, training_day: str) -> list[dict[str, Any]]:
        response = self.client.rpc("pending_delayed_rehab", {"p_athlete_id": athlete_id, "p_training_day": training_day}).execute()
        return getattr(response, "data", None) or []

    def get_admin_athlete(self, athlete_id: str) -> dict[str, Any] | None:
        return self._select_first(
            self.client.table("admin_athlete_rollups").select("*").eq("id", athlete_id)
        )

    def list_admin_athletes_by_ids(self, athlete_ids: list[str]) -> list[dict[str, Any]]:
        if not athlete_ids:
            return []
        response = (
            self.client.table("admin_athlete_rollups")
            .select("*")
            .in_("id", athlete_ids)
            .execute()
        )
        return getattr(response, "data", None) or []

    @_invalidates_profile_cache()
    def clear_onboarding_draft(self, athlete_id: str) -> None:
        try:
            logger.info("[store] clear_onboarding_draft:start athlete_id=%s", athlete_id)
            self.client.table("profiles").update({"onboarding_draft": None}).eq("id", athlete_id).execute()
            logger.info("[store] clear_onboarding_draft:success athlete_id=%s", athlete_id)
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"clear_onboarding_draft athlete_id={athlete_id}",
                detail="failed to clear onboarding draft",
                exc=exc,
            )

    # ------------------------------------------------------------------
    # Injury flags, adaptation notes, admin reviews (api/routes/daily.py)
    # ------------------------------------------------------------------

    def _insert_row(self, table: str, payload: dict[str, Any], *, operation: str) -> dict[str, Any]:
        try:
            response = self.client.table(table).insert(payload).execute()
            rows = getattr(response, "data", None) or []
            if not rows:
                logger.error("[store] %s:no_rows response=%r", operation, response)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"failed to persist {table} row",
                )
            return rows[0]
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=operation,
                detail=f"failed to persist {table} row",
                exc=exc,
            )

    def upsert_today_checkin(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        payload = {"athlete_id": athlete_id, **fields}
        try:
            if fields.get("plan_id") is None:
                # NULL plan_ids never conflict on the plan-scoped key, so a
                # plan-free row upserts on its own partial key via the RPC.
                response = self.client.rpc(
                    "upsert_plan_free_today_checkin",
                    {"p_athlete_id": athlete_id, "p_fields": fields},
                ).execute()
            else:
                response = (
                    self.client.table("today_checkins")
                    .upsert(payload, on_conflict="athlete_id,plan_id,training_day")
                    .execute()
                )
            rows = getattr(response, "data", None) or []
            # A function returning one composite row arrives as an object.
            if isinstance(rows, dict):
                rows = [rows]
            if not rows:
                logger.error("[store] upsert_today_checkin:no_rows athlete_id=%s", athlete_id)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="failed to persist Today check-in",
                )
            return rows[0]
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"upsert_today_checkin athlete_id={athlete_id}",
                detail="failed to persist Today check-in",
                exc=exc,
            )

    def get_today_checkin(
        self, athlete_id: str, plan_id: str | None, training_day: str
    ) -> dict[str, Any] | None:
        """The day's check-in for ``plan_id``, or the plan-free one for ``None``."""
        query = self.client.table("today_checkins").select("*").eq("athlete_id", athlete_id)
        query = query.is_("plan_id", "null") if plan_id is None else query.eq("plan_id", plan_id)
        return self._select_first(query.eq("training_day", training_day))

    def list_today_checkins_for_day(
        self, athlete_id: str, training_day: str
    ) -> list[dict[str, Any]]:
        response = (
            self.client.table("today_checkins")
            .select("*")
            .eq("athlete_id", athlete_id)
            .eq("training_day", training_day)
            .order("updated_at", desc=True)
            .execute()
        )
        return getattr(response, "data", None) or []

    def upsert_session_completion(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        payload = {**fields, "athlete_id": athlete_id}
        try:
            if fields.get("completion_origin") == "day_credit":
                # Insert if absent, then update only credit. Both statements
                # are safe if an independent report wins between them; a
                # read/check followed by an unconditional upsert would race.
                self.client.table("session_completions").upsert(
                    payload, on_conflict="athlete_id,session_id,training_day", ignore_duplicates=True,
                ).execute()
                response = (self.client.table("session_completions").update(payload)
                    .eq("athlete_id", athlete_id).eq("session_id", fields["session_id"])
                    .eq("training_day", fields["training_day"]).eq("plan_id", fields["plan_id"])
                    .eq("completion_origin", "day_credit").execute())
                if not (getattr(response, "data", None) or []):
                    winner = self.get_session_completion(athlete_id, fields["session_id"], fields["training_day"])
                    if winner:
                        return winner
            else:
                response = (
                    self.client.table("session_completions")
                    .upsert(payload, on_conflict="athlete_id,session_id,training_day")
                    .execute()
                )
            rows = getattr(response, "data", None) or []
            if not rows:
                logger.error("[store] upsert_session_completion:no_rows athlete_id=%s", athlete_id)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="failed to persist session completion",
                )
            return rows[0]
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            if any(code in str(exc) for code in ("prescription_revision_conflict", "rehab_daily_allocation_conflict", "rehab_exposure_cannot_be_reset", "clinical_review_pin_invalidated_hold")):
                raise HTTPException(status_code=409, detail="Your session prescription changed. Refresh Today before starting.") from exc
            self._raise_operation_http_error(
                operation=f"upsert_session_completion athlete_id={athlete_id}",
                detail="failed to persist session completion",
                exc=exc,
            )

    def get_session_completion_by_id(self, athlete_id: str, completion_id: str) -> dict[str, Any] | None:
        return self._select_first(self.client.table("session_completions").select("*")
            .eq("athlete_id", athlete_id).eq("id", completion_id))

    def update_session_completion_fields(self, athlete_id: str, completion_id: str,
                                         fields: dict[str, Any]) -> dict[str, Any] | None:
        """History's limited edit: RPE, pain after and notes only."""
        response = (self.client.table("session_completions")
            .update({key: fields.get(key) for key in ("session_rpe", "pain_after", "notes")})
            .eq("athlete_id", athlete_id).eq("id", completion_id).execute())
        return (getattr(response, "data", None) or [None])[0]

    def reset_session_completion(self, athlete_id: str, completion_id: str) -> dict[str, Any] | None:
        """Remove a finished plan session from History: the row stays, marked not started."""
        response = (self.client.table("session_completions")
            .update({"status": "not_started", "started_at": None, "completed_at": None, "session_rpe": None,
                     "pain_after": None, "modification_reason": "", "notes": ""})
            .eq("athlete_id", athlete_id).eq("id", completion_id).execute())
        return (getattr(response, "data", None) or [None])[0]

    def list_session_detail_exercise_logs(self, athlete_id: str, *, plan_id: str,
                                         training_day: str, session_ids: list[str]) -> list[dict[str, Any]]:
        response = (self.client.table("exercise_logs").select("*")
            .eq("athlete_id", athlete_id).eq("plan_id", plan_id).eq("training_day", training_day)
            .in_("session_id", session_ids).order("created_at").execute())
        return getattr(response, "data", None) or []

    def get_external_session_sparring_log(self, athlete_id: str, entry_id: str) -> dict[str, Any] | None:
        return self._select_first(self.client.table("sparring_logs").select(
            "athlete_id,external_session_id,intensity,rounds_completed,round_seconds,head_contact,rocked,notes")
            .eq("athlete_id", athlete_id).eq("external_session_id", entry_id))

    def get_session_completion(
        self, athlete_id: str, session_id: str, training_day: str
    ) -> dict[str, Any] | None:
        return self._select_first(
            self.client.table("session_completions")
            .select("*")
            .eq("athlete_id", athlete_id)
            .eq("session_id", session_id)
            .eq("training_day", training_day)
        )

    def initialize_session_completion_rehab_contexts(
        self,
        athlete_id: str,
        *,
        completion_id: str,
        plan_id: str | None,
        session_id: str,
        training_day: str,
        contexts: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Set immutable response context once on the exact completion row."""
        query = (
            self.client.table("session_completions")
            .update({"rehab_response_contexts": contexts})
            .eq("id", completion_id)
            .eq("athlete_id", athlete_id)
            .eq("session_id", session_id)
            .eq("training_day", training_day)
            .is_("rehab_response_contexts", "null")
        )
        query = query.is_("plan_id", "null") if plan_id is None else query.eq("plan_id", plan_id)
        response = query.execute()
        rows = getattr(response, "data", None) or []
        if rows:
            return rows[0]
        # A concurrent identical completion may have initialized it first.
        # Return the exact row so the caller can use that immutable winner.
        return self.get_session_completion(athlete_id, session_id, training_day)

    def create_custom_plan(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        payload = {key: fields[key] for key in ("name", "sessions", "incomplete")}
        response = self.client.table("custom_training_plans").insert({**payload, "athlete_id": athlete_id}).execute()
        rows = getattr(response, "data", None) or []
        if not rows:
            raise HTTPException(500, "Failed to save custom plan.")
        return rows[0]

    def list_custom_plans(self, athlete_id: str) -> list[dict[str, Any]]:
        response = (self.client.table("custom_training_plans").select("*")
                    .eq("athlete_id", athlete_id).order("updated_at", desc=True).execute())
        return getattr(response, "data", None) or []

    def get_custom_plan(self, athlete_id: str, plan_id: str) -> dict[str, Any] | None:
        return self._select_first(self.client.table("custom_training_plans").select("*")
                                  .eq("athlete_id", athlete_id).eq("id", plan_id))

    def update_custom_plan(self, athlete_id: str, plan_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        response = (self.client.table("custom_training_plans")
                    .update({key: fields[key] for key in ("name", "sessions", "incomplete")})
                    .eq("athlete_id", athlete_id).eq("id", plan_id).execute())
        return (getattr(response, "data", None) or [None])[0]

    def delete_custom_plan(self, athlete_id: str, plan_id: str) -> bool:
        response = (self.client.table("custom_training_plans").delete()
                    .eq("athlete_id", athlete_id).eq("id", plan_id).execute())
        return bool(getattr(response, "data", None))

    def create_external_training_session(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        # ON CONFLICT DO NOTHING is atomic. A retry cannot overwrite an edit or
        # resurrect a deleted occurrence, even when it crosses local midnight.
        self.client.table("external_training_sessions").upsert(
            {**fields, "athlete_id": athlete_id},
            on_conflict="athlete_id,client_request_id", ignore_duplicates=True,
        ).execute()
        row = self._select_first(self.client.table("external_training_sessions").select("*")
            .eq("athlete_id", athlete_id).eq("client_request_id", fields["client_request_id"]))
        if not row:
            raise HTTPException(500, "Failed to save training session.")
        return row

    def list_external_training_sessions(self, athlete_id: str, *, limit: int = 200, include_deleted: bool = False,
                                        before_day: str | None = None) -> list[dict[str, Any]]:
        query = self.client.table("external_training_sessions").select("*").eq("athlete_id", athlete_id)
        if not include_deleted:
            query = query.is_("deleted_at", "null")
        if before_day:
            query = query.lt("training_day", before_day)
        response = query.order("training_day", desc=True).order("created_at", desc=True).limit(limit).execute()
        return _readiness_history_rows(response)

    def get_external_training_session(self, athlete_id: str, entry_id: str) -> dict[str, Any] | None:
        """One live manual session, owner-scoped."""
        return self._select_first(self.client.table("external_training_sessions").select("*")
            .eq("athlete_id", athlete_id).eq("id", entry_id).is_("deleted_at", "null"))

    def update_external_training_session(self, athlete_id: str, entry_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        response = (self.client.table("external_training_sessions")
            .update({key: fields.get(key) for key in ("activity", "duration_minutes", "effort", "sport", "session_type",
                                                      "distance_value", "distance_unit", "duration_seconds")})
            .eq("athlete_id", athlete_id).eq("id", entry_id).is_("deleted_at", "null").execute())
        return (getattr(response, "data", None) or [None])[0]

    def set_external_training_takeaway(self, athlete_id: str, entry_id: str, takeaway: str) -> dict[str, Any] | None:
        # Its own write: a training edit or a retried create never touches it.
        response = (self.client.table("external_training_sessions").update({"takeaway": takeaway})
            .eq("athlete_id", athlete_id).eq("id", entry_id).is_("deleted_at", "null").execute())
        return (getattr(response, "data", None) or [None])[0]

    def delete_external_training_session(self, athlete_id: str, entry_id: str) -> bool:
        query = self.client.table("external_training_sessions").select("*").eq("athlete_id", athlete_id).eq("id", entry_id)
        row = self._select_first(query)
        if not row:
            return False
        if row.get("deleted_at"):
            return True
        deleted_at = datetime.now(timezone.utc).isoformat()
        response = (self.client.table("external_training_sessions")
            .update({"deleted_at": deleted_at})
            .eq("athlete_id", athlete_id).eq("id", entry_id).execute())
        # The database trigger already tombstones the session's exercise
        # entries; this repeats it so no visible result outlives its session.
        (self.client.table("manual_exercise_entries").update({"deleted_at": deleted_at})
            .eq("athlete_id", athlete_id).eq("external_session_id", entry_id).is_("deleted_at", "null").execute())
        return bool(getattr(response, "data", None))

    def get_personal_exercise(self, athlete_id: str, personal_exercise_id: str) -> dict[str, Any] | None:
        return self._select_first(self.client.table("personal_exercises").select("*")
            .eq("athlete_id", athlete_id).eq("id", personal_exercise_id))

    def ensure_personal_exercise(self, athlete_id: str, name: str, name_key: str) -> dict[str, Any]:
        # One private identity per athlete and name; a repeat returns it.
        self.client.table("personal_exercises").upsert(
            {"athlete_id": athlete_id, "name": name, "name_key": name_key},
            on_conflict="athlete_id,name_key", ignore_duplicates=True,
        ).execute()
        row = self._select_first(self.client.table("personal_exercises").select("*")
            .eq("athlete_id", athlete_id).eq("name_key", name_key))
        if not row:
            raise HTTPException(500, "Failed to save exercise.")
        return row

    def get_manual_exercise(self, athlete_id: str, entry_id: str) -> dict[str, Any] | None:
        """By the client's retry key, tombstones included."""
        return self._select_first(self.client.table("manual_exercise_entries").select("*")
            .eq("athlete_id", athlete_id).eq("client_entry_id", entry_id))

    def create_manual_exercise(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        # ON CONFLICT DO NOTHING: a retry cannot add a second occurrence or
        # resurrect a deleted one. The trigger stamps the parent's day.
        self.client.table("manual_exercise_entries").upsert(
            {**fields, "athlete_id": athlete_id},
            on_conflict="athlete_id,client_entry_id", ignore_duplicates=True,
        ).execute()
        row = self.get_manual_exercise(athlete_id, fields["client_entry_id"])
        if not row:
            raise HTTPException(500, "Failed to save exercise.")
        return row

    def update_manual_exercise(self, athlete_id: str, entry_id: str, fields: dict[str, Any], *,
                               revision: int) -> dict[str, Any] | None:
        """Compare-and-set: applies only while ``revision`` is current, then bumps it.
        None when the entry moved on, was deleted or never existed."""
        response = (self.client.table("manual_exercise_entries")
            .update({**{key: fields.get(key) for key in (
                "exercise_key", "personal_exercise_id", "display_name", "measurement_format", "actual", "notes")},
                "revision": revision + 1})
            .eq("athlete_id", athlete_id).eq("client_entry_id", entry_id).eq("revision", revision)
            .is_("deleted_at", "null").execute())
        return (getattr(response, "data", None) or [None])[0]

    def delete_manual_exercise(self, athlete_id: str, entry_id: str) -> bool:
        row = self.get_manual_exercise(athlete_id, entry_id)
        if not row:
            return False
        if row.get("deleted_at"):
            return True
        response = (self.client.table("manual_exercise_entries")
            .update({"deleted_at": datetime.now(timezone.utc).isoformat()})
            .eq("athlete_id", athlete_id).eq("client_entry_id", entry_id).execute())
        return bool(getattr(response, "data", None))

    def list_manual_exercises(self, athlete_id: str, external_session_id: str) -> list[dict[str, Any]]:
        response = (self.client.table("manual_exercise_entries").select("*")
            .eq("athlete_id", athlete_id).eq("external_session_id", external_session_id).is_("deleted_at", "null")
            .order("created_at").order("id").limit(100).execute())
        return _readiness_history_rows(response)

    def count_manual_exercises(self, athlete_id: str, external_session_ids: list[str]) -> dict[str, int]:
        if not external_session_ids:
            return {}
        response = (self.client.table("manual_exercise_entries").select("external_session_id")
            .eq("athlete_id", athlete_id).in_("external_session_id", external_session_ids)
            .is_("deleted_at", "null").limit(len(external_session_ids) * 31).execute())
        counts: dict[str, int] = {}
        for row in _readiness_history_rows(response):
            counts[str(row["external_session_id"])] = counts.get(str(row["external_session_id"]), 0) + 1
        return counts

    def has_exercise_record(self, athlete_id: str, exercise_key: str) -> bool:
        for table, live in (("exercise_logs", False), ("manual_exercise_entries", True)):
            query = self.client.table(table).select("id").eq("athlete_id", athlete_id).eq("exercise_key", exercise_key)
            if live:
                query = query.is_("deleted_at", "null")
            if _readiness_history_rows(query.limit(1).execute()):
                return True
        return False

    def list_manual_exercise_index_page(self, athlete_id: str, *, as_of: str, after_id: str | None,
                                        limit: int = 500) -> list[dict[str, Any]]:
        query = (self.client.table("manual_exercise_entries")
            .select("id,athlete_id,external_session_id,exercise_key,personal_exercise_id,display_name,"
                    "measurement_format,training_day,actual,deleted_at")
            .eq("athlete_id", athlete_id).is_("deleted_at", "null").lte("created_at", as_of))
        if after_id:
            query = query.gt("id", after_id)
        return _readiness_history_rows(query.order("id").limit(limit).execute())

    def list_manual_exercise_performances(self, athlete_id: str, *, exercise_key: str | None,
                                          personal_exercise_id: str | None, as_of: str, before_day: str | None,
                                          before_id: str | None, limit: int = 31) -> list[dict[str, Any]]:
        query = (self.client.table("manual_exercise_entries").select("*")
            .eq("athlete_id", athlete_id).is_("deleted_at", "null").lte("created_at", as_of))
        query = (query.eq("exercise_key", exercise_key) if exercise_key is not None
                 else query.eq("personal_exercise_id", personal_exercise_id))
        if before_day and before_id:
            query = query.or_(f"training_day.lt.{before_day},and(training_day.eq.{before_day},id.lt.{before_id})")
        return _readiness_history_rows(query.order("training_day", desc=True).order("id", desc=True).limit(limit).execute())

    def list_session_completions(
        self, athlete_id: str, *, limit: int = 30, before_day: str | None = None
    ) -> list[dict[str, Any]]:
        """Recent completions (newest training day first) for the derived signal.

        ``before_day`` pages History back past the newest ``limit`` rows."""
        query = (
            self.client.table("session_completions")
            .select("*")
            .eq("athlete_id", athlete_id)
        )
        if before_day:
            query = query.lt("training_day", before_day)
        response = query.order("training_day", desc=True).limit(limit).execute()
        return _readiness_history_rows(response)

    def list_session_completions_from_day(
        self, athlete_id: str, training_day: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Completions on or after ``training_day`` in one read (served by
        ``session_completions_athlete_day_idx``), for Today's lookahead."""
        response = (
            self.client.table("session_completions")
            .select("*")
            .eq("athlete_id", athlete_id)
            .gte("training_day", training_day)
            .order("training_day")
            .limit(limit)
            .execute()
        )
        return getattr(response, "data", None) or []

    def list_plan_session_completions(
        self, athlete_id: str, plan_id: str, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        """All completions for one plan (newest training day first) so the plan
        viewer can colour every session card from real logging."""
        response = (
            self.client.table("session_completions")
            .select("*")
            .eq("athlete_id", athlete_id)
            .eq("plan_id", plan_id)
            .order("training_day", desc=True)
            .limit(limit)
            .execute()
        )
        return getattr(response, "data", None) or []

    def list_rehab_schedule_completions(self, athlete_id: str, *, from_day: str) -> list[dict[str, Any]]:
        """All recent accepted snapshots across plans, paged to avoid truncation."""
        rows, offset = [], 0
        while True:
            response = (self.client.table("session_completions")
                .select("id,athlete_id,plan_id,session_id,training_day,status,prescription_snapshot,created_at,updated_at")
                .eq("athlete_id", athlete_id).gte("training_day", from_day)
                .order("training_day", desc=True).order("id")
                .range(offset, offset + 499).execute())
            page = getattr(response, "data", None) or []
            rows.extend(page)
            if len(page) < 500:
                return rows
            offset += 500

    def get_rehab_schedule_revision(self, athlete_id: str) -> dict[str, Any]:
        revision = {}
        for key, table in (("exposure_id", "rehab_exposures"), ("event_id", "injury_episode_events")):
            response = (self.client.table(table).select("id").eq("athlete_id", athlete_id)
                        .order("created_at", desc=True).order("id", desc=True))
            if table == "injury_episode_events":
                response = response.in_("event_type", ["injury_checkin", "delayed_rehab_response", "clinician_clearance_report",
                    "rehab_progression_assessment", "clinical_progression_review", "clinical_progression_review_lifecycle"])
            response = response.limit(1).execute()
            rows = getattr(response, "data", None) or []
            revision[key] = rows[0]["id"] if rows else None
        return revision

    def list_session_logs(self, athlete_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        """Legacy/manual training history; this table has no current write API."""
        response = (
            self.client.table("session_logs")
            .select("id,athlete_id,plan_id,session_date,session_type,completed,created_at,updated_at")
            .eq("athlete_id", athlete_id)
            .order("session_date", desc=True)
            .limit(limit)
            .execute()
        )
        return getattr(response, "data", None) or []

    def get_athlete_streaks(self, athlete_id: str) -> dict[str, Any] | None:
        return self._select_first(
            self.client.table("athlete_streaks").select("*").eq("athlete_id", athlete_id)
        )

    def upsert_athlete_streaks(
        self, athlete_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        response = (
            self.client.table("athlete_streaks")
            .upsert({"athlete_id": athlete_id, **fields}, on_conflict="athlete_id")
            .execute()
        )
        rows = getattr(response, "data", None) or []
        if not rows:
            raise RuntimeError("failed to persist athlete streak")
        return rows[0]

    def record_daily_activity(
        self, athlete_id: str, activity_date: str
    ) -> dict[str, Any]:
        response = self.client.rpc(
            "record_athlete_daily_activity",
            {"p_athlete_id": athlete_id, "p_activity_date": activity_date},
        ).execute()
        rows = getattr(response, "data", None) or []
        if not rows:
            raise RuntimeError("failed to record athlete activity")
        return rows[0]

    def list_daily_activity(self, athlete_id: str) -> list[dict[str, Any]]:
        response = (
            self.client.table("athlete_daily_activity")
            .select("athlete_id,activity_date")
            .eq("athlete_id", athlete_id)
            .order("activity_date", desc=True)
            .execute()
        )
        return getattr(response, "data", None) or []

    # --- Account XP and plan milestones (moved verbatim from the XP services) ---

    def reconcile_training_engagement_xp(self, athlete_id: str, *, manual_session_id: str | None = None) -> Any:
        response = self.client.rpc("reconcile_training_engagement_xp", {
            "p_athlete_id": athlete_id, "p_manual_session_id": manual_session_id,
        }).execute()
        return getattr(response, "data", None)

    def list_unrewarded_contribution_revisions(self, *, after_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        response = self.client.rpc("list_unrewarded_contribution_revisions", {
            "p_after_id": after_id, "p_limit": limit,
        }).execute()
        return self._mapping_rows(getattr(response, "data", None))

    def reconcile_approved_contribution_xp(self, revision_id: str) -> Any:
        response = self.client.rpc("reconcile_approved_contribution_xp", {
            "p_revision_id": revision_id,
        }).execute()
        return getattr(response, "data", None)

    @staticmethod
    def _mapping_rows(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [dict(row) for row in value if isinstance(row, dict)]

    @staticmethod
    def _rpc_mapping(response: Any) -> dict[str, Any] | None:
        payload = getattr(response, "data", None)
        if isinstance(payload, list):
            payload = payload[0] if payload else None
        return dict(payload) if isinstance(payload, dict) else None

    def validate_xp_abuse_hardening(self) -> Any:
        """Raw payload of the rollout check; the XP service decides if it is current."""
        return getattr(self.client.rpc("validate_xp_abuse_hardening").execute(), "data", None)

    def reconcile_feedback_xp(
        self, athlete_id: str, *, feedback_id: str, target_amount: int
    ) -> dict[str, Any] | None:
        response = self.client.rpc(
            "reconcile_feedback_xp",
            {
                "p_athlete_id": athlete_id,
                "p_feedback_id": feedback_id,
                "p_target_amount": target_amount,
            },
        ).execute()
        return self._rpc_mapping(response)

    def get_xp_progress_state(self, athlete_id: str, *, limit: int) -> dict[str, Any]:
        account_response = (
            self.client.table("xp_accounts")
            .select("total_xp,last_daily_login_date")
            .eq("athlete_id", athlete_id)
            .limit(1)
            .execute()
        )
        account_rows = self._mapping_rows(getattr(account_response, "data", None))
        account = account_rows[0] if account_rows else {}
        awards_response = (
            self.client.table("xp_awards")
            .select("id,action,amount,awarded_at,calendar_date")
            .eq("athlete_id", athlete_id)
            .order("awarded_at", desc=True)
            .order("id", desc=True)
            .limit(limit)
            .execute()
        )
        return {
            "total_xp": account.get("total_xp"),
            "last_daily_login_date": account.get("last_daily_login_date"),
            "recent_awards": self._mapping_rows(getattr(awards_response, "data", None)),
        }

    def xp_award_exists(
        self,
        athlete_id: str,
        *,
        action: str | None = None,
        idempotency_key: str | None = None,
        calendar_date: str | None = None,
    ) -> bool:
        query = self.client.table("xp_awards").select("id").eq("athlete_id", athlete_id)
        if action is not None:
            query = query.eq("action", action)
        if idempotency_key is not None:
            query = query.eq("idempotency_key", idempotency_key)
        if calendar_date is not None:
            query = query.eq("calendar_date", calendar_date)
        return bool(self._mapping_rows(getattr(query.limit(1).execute(), "data", None)))

    def list_plan_milestones(self, athlete_id: str, *, limit: int) -> list[dict[str, Any]]:
        response = (
            self.client.table("plan_milestones")
            .select("id,plan_id,milestone_type,milestone_key,phase_label,metadata,completed_at")
            .eq("athlete_id", athlete_id)
            .order("completed_at", desc=True)
            .limit(limit)
            .execute()
        )
        return self._mapping_rows(getattr(response, "data", None))

    def record_plan_milestone(
        self,
        athlete_id: str,
        *,
        plan_id: str,
        milestone_type: str,
        milestone_key: str,
        phase_label: str | None,
        metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        response = self.client.rpc(
            "record_plan_milestone",
            {
                "p_athlete_id": athlete_id,
                "p_plan_id": plan_id,
                "p_milestone_type": milestone_type,
                "p_milestone_key": milestone_key,
                "p_phase_label": phase_label,
                "p_metadata": dict(metadata),
            },
        ).execute()
        return self._rpc_mapping(response)

    def begin_week_lifecycle_reconciliation(
        self, athlete_id: str, *, plan_id: str, week_id: str
    ) -> dict[str, Any] | None:
        response = self.client.rpc(
            "begin_week_lifecycle_reconciliation",
            {"p_athlete_id": athlete_id, "p_plan_id": plan_id, "p_week_id": week_id},
        ).execute()
        return self._rpc_mapping(response)

    def complete_week_lifecycle_reconciliation(
        self, athlete_id: str, *, plan_id: str, week_id: str
    ) -> dict[str, Any] | None:
        response = self.client.rpc(
            "complete_week_lifecycle_reconciliation",
            {"p_athlete_id": athlete_id, "p_plan_id": plan_id, "p_week_id": week_id},
        ).execute()
        return self._rpc_mapping(response)

    # --- Notification ledger (moved verbatim from notification_foundation) ---

    @staticmethod
    def _notification_rows(response: Any) -> list[dict[str, Any]]:
        payload = getattr(response, "data", None)
        if isinstance(payload, dict):
            return [payload]
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        return []

    def get_notification_preferences(self, profile_id: str) -> dict[str, Any] | None:
        response = (
            self.client.table("notification_preferences")
            .select("*")
            .eq("profile_id", profile_id)
            .limit(1)
            .execute()
        )
        rows = self._notification_rows(response)
        return rows[0] if rows else None

    def upsert_notification_preferences(
        self, profile_id: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        response = (
            self.client.table("notification_preferences")
            .upsert({"profile_id": profile_id, **fields}, on_conflict="profile_id")
            .execute()
        )
        rows = self._notification_rows(response)
        return rows[0] if rows else None

    def record_notification_evaluation(
        self, row: dict[str, Any], *, min_interval_seconds: int
    ) -> dict[str, Any] | None:
        response = self.client.rpc(
            "record_notification_evaluation",
            {
                "p_profile_id": row["profile_id"],
                "p_training_day": row["training_day"],
                "p_intent": row["intent"],
                "p_notification_type": row["notification_type"] or "",
                "p_category": row["category"] or "",
                "p_evaluated_at": row["evaluated_at"],
                "p_scheduled_for": row["scheduled_for"],
                "p_timing_source": row["timing_source"] or "",
                "p_timing_confidence": row["timing_confidence"] or "",
                "p_eligible": row["eligible"],
                "p_decision": row["decision"],
                "p_rejection_reasons": row["rejection_reasons"],
                "p_priority": row["priority"],
                "p_dedupe_key": row["dedupe_key"] or "",
                "p_variant_id": row["variant_id"] or "",
                "p_source_event_metadata": row["source_event_metadata"],
                "p_resulting_delivery_id": row["resulting_delivery_id"],
                "p_evaluation_key": row["evaluation_key"],
                "p_min_interval_seconds": min_interval_seconds,
            },
        ).execute()
        rows = self._notification_rows(response)
        return rows[0] if rows else None

    def list_notification_evaluations(
        self, profile_id: str, training_day: str, *, intent: str | None = None
    ) -> list[dict[str, Any]]:
        query = (
            self.client.table("notification_evaluations")
            .select("*")
            .eq("profile_id", profile_id)
            .eq("training_day", training_day)
        )
        if intent:
            query = query.eq("intent", intent)
        response = query.order("last_evaluated_at", desc=True).limit(500).execute()
        return self._notification_rows(response)

    def has_notification_evaluation_decision(
        self, profile_id: str, *, dedupe_key: str, decision: str
    ) -> bool:
        response = (
            self.client.table("notification_evaluations")
            .select("id")
            .eq("profile_id", profile_id)
            .eq("dedupe_key", dedupe_key)
            .eq("decision", decision)
            .limit(1)
            .execute()
        )
        return bool(self._notification_rows(response))

    def claim_notification_delivery(self, params: dict[str, Any], *, now_utc: datetime) -> Any:
        """Claim through ``claim_notification_delivery_v2``; returns its raw payload.

        ``now_utc`` is unused here: the database clock is authoritative.
        """
        response = self.client.rpc(
            "claim_notification_delivery_v2",
            {f"p_{key}": value for key, value in params.items()},
        ).execute()
        return getattr(response, "data", None)

    def finalize_notification_delivery(
        self,
        delivery_id: str,
        claim_token: str,
        *,
        status: str,
        delivered_count: int,
        error_code: str | None,
    ) -> None:
        self.client.rpc(
            "finalize_notification_delivery",
            {
                "p_delivery_id": delivery_id,
                "p_claim_token": claim_token,
                "p_status": status,
                "p_delivered_count": delivered_count,
                "p_error_code": error_code,
            },
        ).execute()

    def get_notification_simulation_state(
        self,
        profile_id: str,
        *,
        dedupe_keys: list[str],
        training_days: list[str],
        notification_classes: list[str],
        action_keys: list[str],
    ) -> dict[str, list[dict[str, Any]]]:
        delivery_by_dedupe = self._notification_rows(
            self.client.table("notification_deliveries")
            .select("*")
            .eq("profile_id", profile_id)
            .in_("dedupe_key", dedupe_keys)
            .execute()
        )
        active_deliveries = self._notification_rows(
            self.client.table("notification_deliveries")
            .select("*")
            .eq("profile_id", profile_id)
            .in_("training_day", training_days)
            .in_("notification_class", notification_classes)
            .in_("status", ["pending", "sent", "partial"])
            .execute()
        )
        evaluations_by_dedupe = self._notification_rows(
            self.client.table("notification_evaluations")
            .select("*")
            .eq("profile_id", profile_id)
            .eq("decision", "would_select")
            .in_("dedupe_key", dedupe_keys)
            .execute()
        )
        active_evaluations = self._notification_rows(
            self.client.table("notification_evaluations")
            .select("*")
            .eq("profile_id", profile_id)
            .eq("decision", "would_select")
            .in_("training_day", training_days)
            .execute()
        )
        action_rows: list[dict[str, Any]] = []
        if action_keys:
            action_rows = self._notification_rows(
                self.client.table("notification_action_states")
                .select("profile_id,action_key,training_day")
                .eq("profile_id", profile_id)
                .in_("action_key", action_keys)
                .in_("training_day", training_days)
                .execute()
            )
        return {
            "deliveries": [*delivery_by_dedupe, *active_deliveries],
            "evaluations": [*evaluations_by_dedupe, *active_evaluations],
            "action_rows": action_rows,
        }

    def invalidate_notification_action(
        self,
        profile_id: str,
        *,
        action_key: str,
        training_day: str,
        completed_at: datetime,
        source_metadata: dict[str, Any],
    ) -> int:
        response = self.client.rpc(
            "invalidate_notification_action",
            {
                "p_profile_id": profile_id,
                "p_action_key": action_key,
                "p_training_day": training_day,
                "p_completed_at": completed_at.isoformat(),
                "p_source_metadata": dict(source_metadata),
            },
        ).execute()
        return int(getattr(response, "data", 0) or 0)

    def list_notification_deliveries(
        self,
        profile_id: str,
        *,
        intent: str | None = None,
        training_day: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        query = self.client.table("notification_deliveries").select("*").eq("profile_id", profile_id)
        if intent:
            query = query.eq("intent", intent)
        if training_day:
            query = query.eq("training_day", training_day)
        response = query.order("claimed_at", desc=True).limit(limit).execute()
        return self._notification_rows(response)

    def list_notification_templates(self, intent: str, *, locale: str) -> list[dict[str, Any]]:
        response = (
            self.client.table("notification_templates")
            .select("*")
            .eq("intent", intent)
            .eq("locale", locale)
            .eq("active", True)
            .order("variant_id")
            .execute()
        )
        return getattr(response, "data", None) or []

    def list_today_checkins(
        self, athlete_id: str, *, limit: int = 14
    ) -> list[dict[str, Any]]:
        """Recent check-ins (newest training day first) for the derived signal."""
        response = (
            self.client.table("today_checkins")
            .select("*")
            .eq("athlete_id", athlete_id)
            .order("training_day", desc=True)
            .limit(limit)
            .execute()
        )
        return _readiness_history_rows(response)

    def award_xp(
        self,
        athlete_id: str,
        *,
        action: XpAction,
        idempotency_key: str,
        calendar_date: str | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._run_with_transient_retry(
                operation=f"award_xp athlete_id={athlete_id} action={action}",
                fn=lambda: self.client.rpc(
                    "award_athlete_xp",
                    {
                        "p_athlete_id": athlete_id,
                        "p_action": action,
                        "p_idempotency_key": idempotency_key,
                        "p_calendar_date": calendar_date,
                    },
                ).execute(),
            )
            payload = getattr(response, "data", None)
            if isinstance(payload, list):
                payload = payload[0] if payload else None
            if not isinstance(payload, dict):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="XP service temporarily unavailable",
                )
            return payload
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"award_xp athlete_id={athlete_id} action={action}",
                detail="failed to persist XP award",
                exc=exc,
            )

    def create_injury_flag(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return self._insert_row(
            "injury_flags",
            {"athlete_id": athlete_id, **fields},
            operation=f"create_injury_flag athlete_id={athlete_id}",
        )

    def list_injury_flags(
        self, athlete_id: str, *, statuses: tuple[str, ...] = ("open", "monitoring"), limit: int = 20
    ) -> list[dict[str, Any]]:
        query = (
            self.client.table("injury_flags")
            .select("*")
            .eq("athlete_id", athlete_id)
        )
        if statuses:
            query = query.in_("status", list(statuses))
        response = query.order("created_at", desc=True).limit(limit).execute()
        return _readiness_history_rows(response)

    def update_injury_flag(self, flag_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.table("injury_flags").update(fields).eq("id", flag_id).execute()
            rows = getattr(response, "data", None) or []
            if not rows:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="injury flag not found")
            return rows[0]
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"update_injury_flag flag_id={flag_id}",
                detail="failed to update injury flag",
                exc=exc,
            )

    def get_injury_flag_for_athlete(self, flag_id: str, athlete_id: str) -> dict[str, Any] | None:
        response = (
            self.client.table("injury_flags")
            .select("*")
            .eq("id", flag_id)
            .eq("athlete_id", athlete_id)
            .limit(1)
            .execute()
        )
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    def adopt_or_create_intake_injury_flag(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """Adopt a legacy intake injury row or insert one, keyed by ``source_key``.

        One transaction-scoped RPC reads, adopts, dedupes and inserts, then
        fills in wound-safety fields the row lacks. ``params`` are the RPC's
        arguments without their ``p_`` prefix.
        """
        response = self.client.rpc(
            "sync_intake_injury_flag",
            {f"p_{key}": value for key, value in params.items()},
        ).execute()
        data = response.data
        if isinstance(data, dict):
            return dict(data)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return dict(data[0])
        return None

    def create_rehab_exposure(self, athlete_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Use the sole database write path; the RPC is immutable/idempotent."""
        try:
            response = self.client.rpc(
                "record_rehab_exposure",
                {"p_athlete_id": athlete_id, "p_event": payload},
            ).execute()
            row = getattr(response, "data", None)
            if isinstance(row, list):
                row = row[0] if row else None
            if not isinstance(row, dict):
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="rehab exposure unavailable")
            return row
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"create_rehab_exposure athlete_id={athlete_id}",
                detail="failed to persist rehab exposure",
                exc=exc,
            )

    def list_rehab_exposures_by_ids(
        self, athlete_id: str, exposure_ids: list[str]
    ) -> list[dict[str, Any]]:
        if not exposure_ids:
            return []
        response = (
            self.client.table("rehab_exposures")
            .select("id,athlete_id,event_json")
            .eq("athlete_id", athlete_id)
            .in_("id", exposure_ids)
            .execute()
        )
        return getattr(response, "data", None) or []

    def list_rehab_exposures(
        self,
        athlete_id: str,
        *,
        injury_id: str,
        injury_episode_id: str,
        limit: int = 200,
    ) -> RehabExposureWindow:
        """Read newest episode evidence and expose any bounded-history gap."""
        bounded_limit = max(1, min(limit, 500))
        response = (
            self.client.table("rehab_exposures")
            .select("id,athlete_id,event_json,occurred_at,created_at")
            .eq("athlete_id", athlete_id)
            .eq("injury_id", injury_id)
            .eq("injury_episode_id", injury_episode_id)
            .order("occurred_at", desc=True)
            # One sentinel row makes truncation explicit; the window size is
            # not allowed to become an implicit clinical-clearance threshold.
            .limit(bounded_limit + 1)
            .execute()
        )
        rows = getattr(response, "data", None) or []
        history_truncated = len(rows) > bounded_limit
        rows = rows[:bounded_limit]
        rows.sort(
            key=lambda row: (
                str(row.get("occurred_at") or ""),
                str(row.get("id") or ""),
            )
        )
        return RehabExposureWindow(rows=rows, history_truncated=history_truncated)

    def create_adaptation_note(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return self._insert_row(
            "adaptation_notes",
            {"athlete_id": athlete_id, **fields},
            operation=f"create_adaptation_note athlete_id={athlete_id}",
        )

    def upsert_push_subscription(self, profile_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        payload = {"profile_id": profile_id, **fields}
        try:
            response = (
                self.client.table("push_subscriptions")
                # An endpoint identifies one browser install. Re-subscribing (or a
                # different account subscribing on a shared device) replaces the
                # row so pushes only ever reach the endpoint's current owner.
                .upsert(payload, on_conflict="endpoint")
                .execute()
            )
            rows = getattr(response, "data", None) or []
            if not rows:
                logger.error("[store] upsert_push_subscription:no_rows profile_id=%s", profile_id)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="failed to persist push subscription",
                )
            return rows[0]
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"upsert_push_subscription profile_id={profile_id}",
                detail="failed to persist push subscription",
                exc=exc,
            )

    def list_push_subscriptions(self, profile_id: str) -> list[dict[str, Any]]:
        response = (
            self.client.table("push_subscriptions")
            .select("*")
            .eq("profile_id", profile_id)
            .execute()
        )
        return getattr(response, "data", None) or []

    def delete_push_subscription(self, profile_id: str, endpoint: str) -> None:
        try:
            (
                self.client.table("push_subscriptions")
                .delete()
                .eq("profile_id", profile_id)
                .eq("endpoint", endpoint)
                .execute()
            )
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"delete_push_subscription profile_id={profile_id}",
                detail="failed to remove push subscription",
                exc=exc,
            )

    def delete_push_subscription_by_endpoint(self, endpoint: str) -> None:
        """Prune a dead endpoint (push service returned 404/410). Best-effort."""
        (
            self.client.table("push_subscriptions")
            .delete()
            .eq("endpoint", endpoint)
            .execute()
        )

    def list_all_push_subscriptions(
        self, *, limit: int = 500, after_id: str | None = None
    ) -> list[dict[str, Any]]:
        """One id-ordered page of subscriptions; ``after_id`` is the keyset cursor.

        Keyset (rather than offset) pagination so the morning sweep can walk an
        arbitrarily large table without skipping rows when it deletes dead
        endpoints mid-walk.
        """
        query = self.client.table("push_subscriptions").select("*")
        if after_id:
            query = query.gt("id", after_id)
        response = query.order("id", desc=False).limit(limit).execute()
        return getattr(response, "data", None) or []

    def mark_push_subscription_morning_sent(
        self, subscription_id: str, *, sent_day: str
    ) -> None:
        (
            self.client.table("push_subscriptions")
            .update({"morning_last_sent_day": sent_day})
            .eq("id", subscription_id)
            .execute()
        )

    def record_sparring_log(
        self, athlete_id: str, fields: dict[str, Any], *, review_reason: str | None
    ) -> dict[str, Any]:
        """Insert a sparring log and, for a rocked report, its admin review in
        one transaction (public.record_sparring_log). Returns {"log", "review"}.
        """
        operation = f"record_sparring_log athlete_id={athlete_id}"
        try:
            response = self._run_with_transient_retry(
                operation=operation,
                fn=lambda: self.client.rpc(
                    "record_sparring_log",
                    {"p_athlete_id": athlete_id, "p_log": fields, "p_review_reason": review_reason},
                ).execute(),
            )
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=operation,
                detail="failed to save sparring log",
                exc=exc,
            )
        payload = getattr(response, "data", None)
        if isinstance(payload, list):
            payload = payload[0] if payload else None
        if not isinstance(payload, dict) or not isinstance(payload.get("log"), dict):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Sparring log service temporarily unavailable",
            )
        return payload

    def set_sparring_log_focus(self, athlete_id: str, log_id: str, focus: str) -> dict[str, Any] | None:
        """Owner-scoped write of the optional focus only; never the safety fields."""
        response = (self.client.table("sparring_logs").update({"focus": focus})
            .eq("athlete_id", athlete_id).eq("id", log_id).execute())
        return (getattr(response, "data", None) or [None])[0]

    def list_sparring_logs(
        self, athlete_id: str, *, limit: int = 60, from_day: str | None = None
    ) -> list[dict[str, Any]]:
        """The athlete's sparring logs, newest training day first (served by
        ``sparring_logs_athlete_day_idx``), optionally only on/after ``from_day``."""
        query = self.client.table("sparring_logs").select("*").eq("athlete_id", athlete_id)
        if from_day:
            query = query.gte("training_day", from_day)
        response = (
            query.order("training_day", desc=True)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return getattr(response, "data", None) or []

    def latest_sparring_log_day(
        self, athlete_id: str, *, hard: bool = False, rocked: bool = False
    ) -> str | None:
        """The most recent training day with a hard (or rocked) entry, if any."""
        query = self.client.table("sparring_logs").select("training_day").eq("athlete_id", athlete_id)
        if hard:
            query = query.eq("intensity", "hard")
        if rocked:
            query = query.eq("rocked", True)
        response = query.order("training_day", desc=True).limit(1).execute()
        rows = getattr(response, "data", None) or []
        return str(rows[0]["training_day"])[:10] if rows else None

    # -- Exercise logs (public.exercise_logs) ---------------------------------
    # What the athlete actually did for one prescribed block on one training
    # day. Written by POST /api/today/exercise-log; the plan is never edited.

    def upsert_exercise_log(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Write the one log for ``(plan_id, training_day, block_id)``; a second
        save for the same block corrects it in place."""
        operation = f"upsert_exercise_log athlete_id={athlete_id}"
        payload = {"athlete_id": athlete_id, **fields}
        try:
            response = (
                self.client.table("exercise_logs")
                .upsert(payload, on_conflict="athlete_id,plan_id,training_day,block_id")
                .execute()
            )
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=operation,
                detail="failed to save exercise log",
                exc=exc,
            )
        rows = getattr(response, "data", None) or []
        if not rows:
            logger.error("[store] upsert_exercise_log:no_rows athlete_id=%s", athlete_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to save exercise log",
            )
        return rows[0]

    def upsert_exercise_logs(
        self, athlete_id: str, rows: list[dict[str, Any]], *, keep_existing: bool = False
    ) -> list[dict[str, Any]]:
        """Write several logs in ONE request, so they commit together or not at all.

        PostgREST runs a bulk upsert as a single ``INSERT ... ON CONFLICT``
        statement, which Postgres commits atomically: a failure on any row
        writes none of them. With ``keep_existing`` the conflict clause is
        ``DO NOTHING``, so a block already logged keeps its row (decided by the
        database at write time, not by an earlier read); only new rows come back.
        """
        if not rows:
            return []
        operation = f"upsert_exercise_logs athlete_id={athlete_id} count={len(rows)}"
        payload = [{"athlete_id": athlete_id, **fields} for fields in rows]
        try:
            response = (
                self.client.table("exercise_logs")
                .upsert(
                    payload,
                    on_conflict="athlete_id,plan_id,training_day,block_id",
                    ignore_duplicates=keep_existing,
                )
                .execute()
            )
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=operation,
                detail="failed to save exercise logs",
                exc=exc,
            )
        written = getattr(response, "data", None) or []
        if not keep_existing and len(written) != len(rows):
            logger.error(
                "[store] upsert_exercise_logs:row_mismatch athlete_id=%s expected=%s got=%s",
                athlete_id,
                len(rows),
                len(written),
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to save exercise logs",
            )
        return written

    def list_exercise_logs_for_day(
        self, athlete_id: str, *, plan_id: str, training_day: str
    ) -> list[dict[str, Any]]:
        """One plan's logs on one training day (served by the occurrence key)."""
        response = (
            self.client.table("exercise_logs")
            .select("*")
            .eq("athlete_id", athlete_id)
            .eq("plan_id", plan_id)
            .eq("training_day", training_day)
            .order("created_at")
            .execute()
        )
        return getattr(response, "data", None) or []

    def list_exercise_index_page(self, athlete_id: str, *, as_of: str, after_id: str | None,
                                 limit: int = 500) -> list[dict[str, Any]]:
        query = (self.client.table("exercise_logs")
            .select("id,athlete_id,exercise_key,status,training_day,prescribed,actual")
            .eq("athlete_id", athlete_id).neq("status", "skipped").lte("created_at", as_of))
        if after_id:
            query = query.gt("id", after_id)
        return _readiness_history_rows(query.order("id").limit(limit).execute())

    def list_exercise_performances(self, athlete_id: str, *, exercise_key: str | None, log_id: str | None,
                                   as_of: str, before_day: str | None, before_id: str | None,
                                   limit: int = 31) -> list[dict[str, Any]]:
        query = self.client.table("exercise_logs").select("*").eq("athlete_id", athlete_id).lte("created_at", as_of)
        query = query.eq("exercise_key", exercise_key) if exercise_key is not None else query.eq("id", log_id)
        if before_day and before_id:
            query = query.or_(f"training_day.lt.{before_day},and(training_day.eq.{before_day},id.lt.{before_id})")
        return _readiness_history_rows(query.order("training_day", desc=True).order("id", desc=True).limit(limit).execute())

    def list_exercise_history(
        self, athlete_id: str, *, before_day: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Athlete-owned history across plans, ordered consistently for paging."""
        query = self.client.table("exercise_logs").select("*").eq("athlete_id", athlete_id)
        if before_day:
            query = query.lt("training_day", before_day)
        response = (query.order("training_day", desc=True).order("updated_at", desc=True)
                    .order("id", desc=True).range(offset, offset + limit - 1).execute())
        return getattr(response, "data", None) or []

    def list_recent_exercise_loads(
        self, athlete_id: str, *, before_day: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        """The athlete's latest logs that recorded a weight, newest day first,
        from before ``before_day`` (served by ``exercise_logs_athlete_day_idx``)."""
        response = (
            self.client.table("exercise_logs")
            .select("exercise_key,prescribed,actual,training_day")
            .eq("athlete_id", athlete_id)
            .lt("training_day", before_day)
            .or_("actual->load.not.is.null,actual->set_entries.not.is.null")
            .order("training_day", desc=True)
            .limit(limit)
            .execute()
        )
        return getattr(response, "data", None) or []

    # -- Exercise demo videos (public.exercise_media) -------------------------
    # Read by api/services/exercise_media.py on plan reads (cached in-process)
    # and written only by tools/exercise_media.py and the worker's daily check.

    _EXERCISE_MEDIA_SERVED_COLUMNS = (
        "exercise_key,aliases,video_id,start_s,end_s,source,channel_title,made_for_kids,orientation"
    )
    # PostgREST caps a single read (1000 rows by default), so reads page until
    # a short page comes back.
    _EXERCISE_MEDIA_PAGE_SIZE = 1000

    def _exercise_media_page(self, *, columns: str, served_only: bool, offset: int) -> Any:
        query = self.client.table("exercise_media").select(columns)
        if served_only:
            query = query.eq("status", "ok")
        # Stable order: pages don't overlap, and when two rows claim the same
        # alias the same row wins on every load.
        return (
            query.order("exercise_key")
            .range(offset, offset + self._EXERCISE_MEDIA_PAGE_SIZE - 1)
            .execute()
        )

    def _list_exercise_media_rows(self, *, columns: str, served_only: bool, operation: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            response = self._run_with_transient_retry(
                operation=f"{operation} offset={offset}",
                fn=lambda offset=offset: self._exercise_media_page(
                    columns=columns, served_only=served_only, offset=offset
                ),
            )
            batch = [row for row in (getattr(response, "data", None) or []) if isinstance(row, dict)]
            rows.extend(batch)
            if len(batch) < self._EXERCISE_MEDIA_PAGE_SIZE:
                return rows
            offset += self._EXERCISE_MEDIA_PAGE_SIZE

    def list_exercise_media(self) -> list[dict[str, Any]]:
        """Every curator-approved video currently checked as available to serve."""
        return self._list_exercise_media_rows(
            columns=self._EXERCISE_MEDIA_SERVED_COLUMNS,
            served_only=True,
            operation="list_exercise_media",
        )

    def list_exercise_media_for_verification(self) -> list[dict[str, Any]]:
        """Every row, unavailable ones included, so a video that recovers is served again."""
        return self._list_exercise_media_rows(
            columns="exercise_key,aliases,video_id,status",
            served_only=False,
            operation="list_exercise_media_for_verification",
        )

    def update_exercise_media_status(
        self,
        exercise_key: str,
        *,
        status: str,
        reason: str | None,
        made_for_kids: bool | None = None,
        title: str | None = None,
        channel_title: str | None = None,
        orientation: str | None = None,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        payload: dict[str, Any] = {
            "status": status,
            "status_reason": (reason or None) and str(reason)[:200],
            "verified_at": now_iso,
            "updated_at": now_iso,
        }
        # A deleted or private video reports no metadata; keep what was last seen.
        if made_for_kids is not None:
            payload["made_for_kids"] = made_for_kids
        if title:
            payload["title"] = title[:200]
        if channel_title:
            payload["channel_title"] = channel_title[:200]
        # No dimensions reported: never null out an orientation already stored.
        if orientation in ("portrait", "landscape"):
            payload["orientation"] = orientation
        self._run_with_transient_retry(
            operation=f"update_exercise_media_status key={exercise_key}",
            fn=lambda: self.client.table("exercise_media")
            .update(payload)
            .eq("exercise_key", exercise_key)
            .execute(),
        )

    def upsert_exercise_media(self, row: dict[str, Any]) -> None:
        payload = {**row, "updated_at": datetime.now(timezone.utc).isoformat()}
        self._run_with_transient_retry(
            operation=f"upsert_exercise_media key={row.get('exercise_key')}",
            fn=lambda: self.client.table("exercise_media")
            .upsert(payload, on_conflict="exercise_key")
            .execute(),
        )

    def create_admin_review(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return self._insert_row(
            "admin_reviews",
            {"athlete_id": athlete_id, **fields},
            operation=f"create_admin_review athlete_id={athlete_id}",
        )

    def list_admin_reviews(self, *, status_filter: str | None = "pending", limit: int = 50) -> list[dict[str, Any]]:
        query = self.client.table("admin_reviews").select("*")
        if status_filter:
            query = query.eq("status", status_filter)
        response = query.order("created_at", desc=True).limit(limit).execute()
        return getattr(response, "data", None) or []

    def count_pending_admin_reviews_for_athlete(self, athlete_id: str) -> int:
        response = (
            self.client.table("admin_reviews")
            .select("id", count="exact")
            .eq("athlete_id", athlete_id)
            .eq("status", "pending")
            .limit(0)
            .execute()
        )
        count = getattr(response, "count", None)
        return int(count or 0)

    def resolve_admin_review(self, review_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.table("admin_reviews").update(fields).eq("id", review_id).execute()
            rows = getattr(response, "data", None) or []
            if not rows:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="review not found")
            return rows[0]
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_operation_http_error(
                operation=f"resolve_admin_review review_id={review_id}",
                detail="failed to resolve review",
                exc=exc,
            )

    # ------------------------------------------------------------------
    # Secure beta feedback
    # ------------------------------------------------------------------

    def _raise_feedback_store_error(self, exc: Exception, *, detail: str) -> None:
        transient = self._is_transient_store_error(exc)
        logger.error(
            "[feedback_store] failure error_code=feedback_store_failure error_class=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
                if transient
                else status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            detail="store service temporarily unavailable" if transient else detail,
        ) from exc

    def get_context_feedback(self, profile_id: str, context_key: str) -> dict[str, Any] | None:
        try:
            return self._select_first(
                self.client.table("beta_feedback")
                .select("*")
                .eq("submitted_by_profile_id", profile_id)
                .eq("context_key", context_key)
            )
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_feedback_store_error(exc, detail="failed to read feedback")

    def get_feedback_plan_for_owner(self, plan_id: str, profile_id: str) -> dict[str, Any] | None:
        try:
            return self._select_first(
                self.client.table("plans").select("*").eq("id", plan_id).eq("athlete_id", profile_id)
            )
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_feedback_store_error(exc, detail="failed to read feedback context")

    def get_feedback_active_plan_id(self, profile_id: str) -> str | None:
        try:
            row = self._select_first(
                self.client.table("profiles").select("active_plan_id").eq("id", profile_id)
            )
            return str(row.get("active_plan_id") or "").strip() or None if row else None
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_feedback_store_error(exc, detail="failed to read feedback context")

    def get_feedback_today_checkin(
        self, profile_id: str, plan_id: str, training_day: str
    ) -> dict[str, Any] | None:
        try:
            return self._select_first(
                self.client.table("today_checkins")
                .select("*")
                .eq("athlete_id", profile_id)
                .eq("plan_id", plan_id)
                .eq("training_day", training_day)
            )
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_feedback_store_error(exc, detail="failed to read feedback context")

    def list_feedback_injury_flags(self, profile_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        try:
            response = (
                self.client.table("injury_flags")
                .select("*")
                .eq("athlete_id", profile_id)
                .in_("status", ["open", "monitoring"])
                .order("created_at", desc=True)
                .limit(max(1, min(limit, 20)))
                .execute()
            )
            return getattr(response, "data", None) or []
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_feedback_store_error(exc, detail="failed to read feedback context")

    def get_feedback_intake(self, intake_id: str) -> dict[str, Any] | None:
        try:
            return self._select_first(
                self.client.table(INTAKES_TABLE).select("*").eq("id", intake_id)
            )
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_feedback_store_error(exc, detail="failed to read feedback context")

    def upsert_context_feedback(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = (
                self.client.table("beta_feedback")
                .upsert(payload, on_conflict="submitted_by_profile_id,context_key")
                .execute()
            )
            rows = getattr(response, "data", None) or []
            if not rows:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="failed to persist feedback",
                )
            return rows[0]
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_feedback_store_error(exc, detail="failed to persist feedback")

    def insert_global_feedback(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.table("beta_feedback").insert(payload).execute()
            rows = getattr(response, "data", None) or []
            if not rows:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="failed to persist feedback",
                )
            return rows[0]
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_feedback_store_error(exc, detail="failed to persist feedback")

    def list_admin_feedback(self, *, limit: int = 50) -> list[dict[str, Any]]:
        try:
            response = (
                self.client.table("beta_feedback")
                .select(
                    "id,submitted_by_profile_id,surface,category,response,reason,comment,"
                    "structured_response,contact_allowed,priority,plan_id,today_checkin_id,"
                    "session_id,camp_phase,app_version,"
                    "readiness_snapshot,injury_snapshot,technical_context,"
                    "screenshot_path,screenshot_expires_at,created_at,updated_at,"
                    "profiles!beta_feedback_submitted_by_profile_id_fkey(email,full_name)"
                )
                .order("created_at", desc=True)
                .limit(100)
                .execute()
            )
            rows = getattr(response, "data", None) or []
            rows.sort(
                key=lambda row: (
                    str(row.get("priority") or "normal") == "safety",
                    str(row.get("created_at") or ""),
                ),
                reverse=True,
            )
            return rows[: max(1, min(limit, 100))]
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_feedback_store_error(exc, detail="failed to read admin feedback")

    def get_feedback_screenshot_path(self, feedback_id: str) -> str | None:
        try:
            row = self._select_first(
                self.client.table("beta_feedback")
                .select("screenshot_path")
                .eq("id", feedback_id)
                .gt("screenshot_expires_at", datetime.now(timezone.utc).isoformat())
            )
            return str(row.get("screenshot_path") or "").strip() or None if row else None
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_feedback_store_error(exc, detail="failed to read feedback screenshot")

    def create_feedback_screenshot_signed_url(self, path: str, *, expires_in: int) -> str:
        try:
            response = self.client.storage.from_("feedback-screenshots").create_signed_url(
                path,
                expires_in,
            )
            url = str(response.get("signedURL") or response.get("signedUrl") or "").strip()
            if not url:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="feedback screenshot unavailable",
                )
            return url
        except HTTPException:
            raise
        except _STORAGE_CLIENT_ERRORS as exc:
            self._raise_feedback_store_error(exc, detail="failed to open feedback screenshot")

    def claim_feedback_rate_limit(
        self,
        profile_id: str,
        *,
        report_limit: int,
        screenshot_limit: int,
        has_screenshot: bool,
    ) -> tuple[bool, str | None, int]:
        try:
            response = self.client.rpc(
                "claim_beta_feedback_rate_limit",
                {
                    "p_submitted_by_profile_id": profile_id,
                    "p_report_limit": report_limit,
                    "p_screenshot_limit": screenshot_limit,
                    "p_window_seconds": 3600,
                    "p_has_screenshot": has_screenshot,
                },
            ).execute()
            rows = getattr(response, "data", None) or []
            if not rows:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="feedback rate limit unavailable",
                )
            row = rows[0]
            return bool(row.get("allowed")), row.get("blocked_scope"), int(row.get("retry_after_seconds") or 0)
        except HTTPException:
            raise
        except _STORE_CLIENT_ERRORS as exc:
            self._raise_feedback_store_error(exc, detail="feedback rate limit unavailable")

    def upload_feedback_screenshot(self, path: str, data: bytes, mime: str) -> None:
        try:
            self.client.storage.from_("feedback-screenshots").upload(
                path,
                data,
                {"content-type": mime, "upsert": "false"},
            )
        except _STORAGE_CLIENT_ERRORS as exc:
            self._raise_feedback_store_error(exc, detail="failed to upload screenshot")

    def delete_feedback_screenshots(self, paths: list[str]) -> None:
        if not paths:
            return
        try:
            self.client.storage.from_("feedback-screenshots").remove(paths)
        except _STORAGE_CLIENT_ERRORS as exc:
            self._raise_feedback_store_error(exc, detail="failed to delete screenshot")

    def list_expired_feedback_screenshots(self, *, limit: int = 100) -> list[dict[str, Any]]:
        response = (
            self.client.table("beta_feedback")
            .select("id,screenshot_path,screenshot_expires_at")
            .not_.is_("screenshot_path", "null")
            .is_("screenshot_deleted_at", "null")
            .lte("screenshot_expires_at", datetime.now(timezone.utc).isoformat())
            .order("screenshot_expires_at")
            .limit(max(1, min(limit, 500)))
            .execute()
        )
        return getattr(response, "data", None) or []

    def list_profile_feedback_screenshots(self, profile_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        response = (
            self.client.table("beta_feedback")
            .select("id,screenshot_path,screenshot_expires_at")
            .eq("submitted_by_profile_id", profile_id)
            .not_.is_("screenshot_path", "null")
            .order("created_at")
            .limit(max(1, min(limit, 500)))
            .execute()
        )
        return getattr(response, "data", None) or []

    def clear_feedback_screenshot(self, feedback_id: str, expected_path: str) -> bool:
        response = (
            self.client.table("beta_feedback")
            .update(
                {
                    "screenshot_path": None,
                    "screenshot_mime": None,
                    "screenshot_size_bytes": None,
                    "screenshot_width": None,
                    "screenshot_height": None,
                    "screenshot_expires_at": None,
                    "screenshot_deleted_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("id", feedback_id)
            .eq("screenshot_path", expected_path)
            .execute()
        )
        return bool(getattr(response, "data", None) or [])
