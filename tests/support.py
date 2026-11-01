from __future__ import annotations

import copy
import math
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from uuid import uuid4

from fastapi import HTTPException, status
from fastapi.testclient import TestClient

from api.app import create_app
from api.auth import AuthenticatedUser
from api.compliance import (
    CODE_DOB_INVALID,
    CODE_UNDER_MINIMUM_AGE,
    HEALTH_CONSENT_VERSION,
    TERMS_VERSION,
    UNDER_MINIMUM_AGE_MESSAGE,
    age_years,
    meets_minimum_age,
    parse_date_of_birth,
)
from api.errors import (
    client_request_id_payload_mismatch_error,
    generation_daily_limit_error,
    generation_job_not_found_error,
)
from api.generation.payloads import _stable_payload_hash
from api.models import (
    PlanRequest,
    ProfileUpdateRequest,
    USERNAME_CHANGE_WINDOW_DAYS,
    USERNAME_MAX_CHANGES_PER_WINDOW,
    validate_username,
)
from api.state_machine import (
    is_generation_job_status,
    is_plan_status,
    require_generation_job_transition,
    require_plan_transition,
)
from api.generation_config import generation_worker_id
from api.schema_requirements import GENERATION_JOB_STAGE2_COST_COLUMNS
from api.store import _signup_date_of_birth, _signup_full_name, _generation_hard_max_runtime_seconds, _generation_startup_max_attempts, is_job_loaded_stalled_generation_job, is_stage1_planner_stalled_generation_job, is_startup_stale_generation_job
from api.store import RehabExposureWindow, TRAINING_PLAN_SELECT
from api.xp import XP_CALENDAR_SCOPED_ACTIONS, XP_REWARD_AMOUNTS, XpAction
from api.services.xp_awards import XP_ABUSE_HARDENING_VERSION
from api.services.notification_foundation import (
    NOTIFICATION_MAX_ATTEMPTS,
    NOTIFICATION_STALE_CLAIM_AFTER,
)
from datetime import timedelta

os.environ.setdefault("APP_GENERATION_SCHEDULER", "fastapi")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_usable_plan_at(plans: list[dict]) -> str | None:
    """Mirrors admin_athlete_rollups.first_usable_plan_at."""
    usable = sorted(
        plan["created_at"] for plan in plans if plan.get("status") in {"ready", "publishable_with_flags"}
    )
    return usable[0] if usable else None


def _parse_iso(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _latest_job_activity_at(job: dict) -> datetime | None:
    latest: datetime | None = None
    for field_name in ("heartbeat_at", "updated_at", "started_at", "created_at"):
        parsed = _parse_iso(job.get(field_name))
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
    for milestone in job.get("progress_milestones") or []:
        if not isinstance(milestone, dict):
            continue
        parsed = _parse_iso(milestone.get("at"))
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
    return latest


def _status_transition_error(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _filter_admin_rows(rows: list[dict], q: str | None, columns: tuple[str, ...]) -> list[dict]:
    """Mirror the server-side ``q`` search the real store performs in Postgres:
    a case-insensitive substring match across the given text columns."""
    term = " ".join((q or "").split()).lower()
    if not term:
        return rows
    return [
        row
        for row in rows
        if any(term in str(row.get(column) or "").lower() for column in columns)
    ]


def xp_hardening_ready_payload() -> dict:
    """The rollout payload a fully migrated database returns (see xp_awards)."""
    return {
        "ok": True,
        "version": XP_ABUSE_HARDENING_VERSION,
        "rollout_ready": True,
        "open_plan_scope_ready": True,
    }


@dataclass
class FakeAuthService:
    users_by_token: dict[str, AuthenticatedUser]

    def get_user_from_token(self, token: str) -> AuthenticatedUser:
        user = self.users_by_token.get(token)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
            )
        return user


class FullRowStatusReads:
    """Compact status reads for in-memory test stores.

    SupabaseAppStore reads generation-job and plan status through compact RPCs
    and narrow selects (api/store_performance.py) that fall back to its full-row
    methods. In-memory stores have no compact projection, so these return the
    full rows, and the queue poll never backs off.
    """

    def get_generation_job_status(self, job_id: str) -> dict | None:
        return self.get_generation_job(job_id)

    def get_visible_active_generation_job_status(self, athlete_id: str) -> dict | None:
        return self.get_visible_active_generation_job_for_athlete(athlete_id)

    def get_latest_generation_job_status(self, athlete_id: str) -> dict | None:
        return self.get_latest_generation_job_for_athlete(athlete_id)

    def get_plan_status(self, plan_id: str) -> dict | None:
        return self.get_plan(plan_id)

    def get_latest_plan_status(self, athlete_id: str) -> dict | None:
        return self.get_latest_plan(athlete_id)

    def poll_claimable_generation_jobs(
        self, *, limit: int = 20, stale_after_seconds: int | None = None
    ) -> list[dict]:
        return self.list_claimable_generation_jobs(limit=limit, stale_after_seconds=stale_after_seconds)

    def list_generation_job_recovery_candidates(self, *, limit: int) -> list[dict]:
        return list(self.list_admin_active_generation_jobs(limit=limit))


class InMemoryNotificationLedger:
    """In-memory notification ledger for test stores (see api/store.py).

    Mirrors the durable tables and RPCs production uses: preferences, the
    coalescing evaluation ledger, the delivery claim ledger (dedupe, retry,
    daily cap, spacing, completed-action suppression) and action states. This
    lived inside api/services/notification_foundation.py as a fallback for
    client-less stores; it is test infrastructure, so it lives here now, with
    state kept per store instance.
    """

    def _notification_ledger(self) -> dict:
        state = self.__dict__.get("_notification_ledger_state")
        if state is None:
            state = {"preferences": {}, "deliveries": {}, "evaluations": {}, "actions": set()}
            self.__dict__["_notification_ledger_state"] = state
        return state

    def get_notification_preferences(self, profile_id: str) -> dict | None:
        row = self._notification_ledger()["preferences"].get(profile_id)
        return dict(row) if row is not None else None

    def upsert_notification_preferences(self, profile_id: str, fields: dict) -> dict:
        self._notification_ledger()["preferences"][profile_id] = dict(fields)
        return dict(fields)

    def record_notification_evaluation(self, row: dict, *, min_interval_seconds: int) -> dict:
        bucket = self._notification_ledger()["evaluations"]
        key = (row["profile_id"], row["evaluation_key"])
        reference = _notification_datetime(row["evaluated_at"])
        existing = bucket.get(key)
        if existing is not None:
            last_evaluated = _notification_datetime(existing.get("last_evaluated_at"))
            if (
                min_interval_seconds > 0
                and last_evaluated is not None
                and reference - last_evaluated < timedelta(seconds=min_interval_seconds)
            ):
                return dict(existing)
            existing["evaluated_at"] = row["evaluated_at"]
            existing["last_evaluated_at"] = row["evaluated_at"]
            existing["evaluation_count"] = int(existing.get("evaluation_count") or 0) + 1
            existing["eligible"] = row["eligible"]
            existing["decision"] = row["decision"]
            existing["rejection_reasons"] = list(row["rejection_reasons"])
            if row.get("resulting_delivery_id"):
                existing["resulting_delivery_id"] = row["resulting_delivery_id"]
            return dict(existing)
        stored = {**row, "id": str(uuid4())}
        bucket[key] = stored
        return dict(stored)

    def list_notification_evaluations(
        self, profile_id: str, training_day: str, *, intent: str | None = None
    ) -> list[dict]:
        rows = [
            dict(row)
            for (row_profile_id, _), row in self._notification_ledger()["evaluations"].items()
            if row_profile_id == profile_id
            and str(row.get("training_day") or "") == training_day
            and (not intent or str(row.get("intent") or "") == intent)
        ]
        return sorted(rows, key=lambda row: str(row.get("last_evaluated_at") or ""), reverse=True)

    def has_notification_evaluation_decision(
        self, profile_id: str, *, dedupe_key: str, decision: str
    ) -> bool:
        return any(
            row_profile_id == profile_id
            and str(row.get("dedupe_key") or "") == dedupe_key
            and str(row.get("decision") or "") == decision
            for (row_profile_id, _), row in self._notification_ledger()["evaluations"].items()
        )

    def claim_notification_delivery(self, params: dict, *, now_utc: datetime) -> dict:
        """The decisions of claim_notification_delivery_v2, step for step.

        Source: supabase/migrations/20260813072104_redesign_fight_camp_notifications.sql.
        ``now_utc`` stands in for the database clock.
        """
        ledger = self._notification_ledger()
        bucket = ledger["deliveries"]
        now = now_utc.astimezone(timezone.utc)
        training_day = params["training_day"]
        if _notification_datetime(params["expires_at"]) <= now:
            return {"decision": "outside_due_window"}
        action_key = params.get("action_key")
        if action_key is not None and (
            params["profile_id"],
            action_key,
            training_day,
        ) in ledger["actions"]:
            return {"decision": "user_action_already_done"}
        key = (params["profile_id"], params["dedupe_key"])
        row = bucket.get(key)
        if row is not None:
            attempts = int(row.get("attempt_count") or 0)
            status = str(row.get("status") or "")
            claimed_at = _notification_datetime(row.get("claimed_at"))
            stale = claimed_at is not None and now - claimed_at >= NOTIFICATION_STALE_CLAIM_AFTER
            if not (
                attempts < NOTIFICATION_MAX_ATTEMPTS
                and (status == "failed" or (status == "pending" and stale))
            ):
                return {"decision": "duplicate_dedupe_key"}
            row.update(
                {
                    "notification_type": params["notification_type"],
                    "intent": params["intent"],
                    "variant_id": params.get("variant_id") or None,
                    "status": "pending",
                    "claim_token": str(uuid4()),
                    "claimed_at": now.isoformat(),
                    "attempt_count": attempts + 1,
                    "expires_at": params["expires_at"],
                    "delivered_count": 0,
                    "error_code": None,
                    "sent_at": None,
                    "cancelled_at": None,
                    "cancellation_reason": None,
                }
            )
            return {"decision": "claimed", "delivery": dict(row)}
        active = [
            existing
            for existing in bucket.values()
            if existing.get("profile_id") == params["profile_id"]
            and existing.get("training_day") == training_day
            and existing.get("notification_class") == params["notification_class"]
            and existing.get("status") in {"pending", "sent", "partial"}
        ]
        if len(active) >= max(1, int(params["daily_cap"])):
            return {"decision": "daily_cap"}
        spacing = int(params["min_spacing_minutes"])
        if spacing > 0:
            latest = max(
                (
                    parsed
                    for existing in active
                    if (
                        parsed := _notification_datetime(
                            existing.get("sent_at") or existing.get("claimed_at")
                        )
                    )
                    is not None
                ),
                default=None,
            )
            if latest is not None and latest > now - timedelta(minutes=spacing):
                return {"decision": "cooldown_active"}
        row = {
            "id": str(uuid4()),
            "profile_id": params["profile_id"],
            "notification_type": params["notification_type"],
            "intent": params["intent"],
            "dedupe_key": params["dedupe_key"],
            "training_day": training_day,
            "notification_class": params["notification_class"],
            "variant_id": params.get("variant_id") or None,
            "action_key": params.get("action_key") or None,
            "status": "pending",
            "claim_token": str(uuid4()),
            "claimed_at": now.isoformat(),
            "attempt_count": 1,
            "expires_at": params["expires_at"],
        }
        bucket[key] = row
        return {"decision": "claimed", "delivery": dict(row)}

    def finalize_notification_delivery(
        self,
        delivery_id: str,
        claim_token: str,
        *,
        status: str,
        delivered_count: int,
        error_code: str | None,
    ) -> None:
        for row in self._notification_ledger()["deliveries"].values():
            if row.get("id") == delivery_id and row.get("claim_token") == claim_token:
                row.update(
                    {
                        "status": status,
                        "delivered_count": delivered_count,
                        "error_code": error_code,
                        "sent_at": (
                            datetime.now(timezone.utc).isoformat()
                            if status in {"sent", "partial"}
                            else None
                        ),
                    }
                )
                return

    def get_notification_simulation_state(
        self,
        profile_id: str,
        *,
        dedupe_keys: list,
        training_days: list,
        notification_classes: list,
        action_keys: list,
    ) -> dict:
        """The same five reads SupabaseAppStore makes, with the same filters."""
        ledger = self._notification_ledger()
        deliveries = [
            row for row in ledger["deliveries"].values() if row.get("profile_id") == profile_id
        ]
        evaluations = [
            row
            for row in ledger["evaluations"].values()
            if row.get("profile_id") == profile_id and row.get("decision") == "would_select"
        ]
        return {
            "deliveries": [
                *(dict(row) for row in deliveries if row.get("dedupe_key") in dedupe_keys),
                *(
                    dict(row)
                    for row in deliveries
                    if row.get("training_day") in training_days
                    and row.get("notification_class") in notification_classes
                    and row.get("status") in {"pending", "sent", "partial"}
                ),
            ],
            "evaluations": [
                *(dict(row) for row in evaluations if row.get("dedupe_key") in dedupe_keys),
                *(dict(row) for row in evaluations if row.get("training_day") in training_days),
            ],
            "action_rows": [
                {"profile_id": owner, "action_key": action, "training_day": day}
                for owner, action, day in ledger["actions"]
                if action_keys
                and owner == profile_id
                and action in action_keys
                and day in training_days
            ],
        }

    def invalidate_notification_action(
        self,
        profile_id: str,
        *,
        action_key: str,
        training_day: str,
        completed_at: datetime,
        source_metadata: dict,
    ) -> int:
        ledger = self._notification_ledger()
        ledger["actions"].add((profile_id, action_key, training_day))
        cancelled = 0
        for row in ledger["deliveries"].values():
            if (
                row.get("profile_id") == profile_id
                and row.get("action_key") == action_key
                and row.get("training_day") == training_day
                and row.get("status") in {"pending", "failed"}
            ):
                row.update(
                    {
                        "status": "cancelled",
                        "cancelled_at": completed_at.isoformat(),
                        "cancellation_reason": "user_action_already_done",
                    }
                )
                cancelled += 1
        return cancelled

    def list_notification_deliveries(
        self,
        profile_id: str,
        *,
        intent: str | None = None,
        training_day: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        rows = [
            dict(row)
            for row in self._notification_ledger()["deliveries"].values()
            if row.get("profile_id") == profile_id
            and (not intent or row.get("intent") == intent)
            and (not training_day or row.get("training_day") == training_day)
        ]
        return sorted(
            rows,
            key=lambda row: str(row.get("sent_at") or row.get("claimed_at") or ""),
            reverse=True,
        )[:limit]

    def list_notification_templates(self, intent: str, *, locale: str) -> list[dict]:
        return []


def _notification_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_INJURY_STATUS_RANK = {"resolved": 0, "monitoring": 1, "open": 2}


def _normalized_injury_area(value) -> str:
    # replace(replace(replace(lower(btrim(coalesce(body_area, ''))), '-', '_'), '/', '_'), ' ', '_')
    return str(value or "").strip(" ").lower().replace("-", "_").replace("/", "_").replace(" ", "_")


def _normalized_injury_description(value) -> str:
    # regexp_replace(lower(btrim(coalesce(description, ''))), '\s+', ' ', 'g')
    return re.sub(r"\s+", " ", str(value or "").strip(" ").lower())


class FakeStore(InMemoryNotificationLedger, FullRowStatusReads):
    def __init__(self, admin_emails: set[str] | None = None):
        self.profiles: dict[str, dict] = {}
        self.intakes: dict[str, list[dict]] = {}
        self.plans: dict[str, dict] = {}
        self.active_plan_ids: dict[str, str] = {}
        self.generation_jobs: dict[str, dict] = {}
        self.today_checkins: dict[str, list[dict]] = {}
        self.session_completions: dict[str, list[dict]] = {}
        self.external_training_sessions: dict[str, dict] = {}
        # Mirrors manual_exercise_entries / personal_exercises and their triggers.
        self.manual_exercise_entries: list[dict] = []
        self.personal_exercises: dict[str, dict] = {}
        self.custom_plans: dict[str, dict] = {}
        self.private_media: dict[str, dict] = {}
        self.private_video_assets: dict[str, dict] = {}
        self.session_logs: dict[str, list[dict]] = {}
        self.athlete_streak_rows: dict[str, dict] = {}
        self.daily_activity: dict[str, set[str]] = {}
        self.xp_accounts: dict[str, dict] = {}
        self.xp_awards: dict[str, list[dict]] = {}
        self.plan_milestones: dict[str, list[dict]] = {}
        self.injury_flags: dict[str, list[dict]] = {}
        self.exercise_media: dict[str, dict] = {}
        self.rehab_exposures: dict[str, dict] = {}
        self.injury_episode_events: dict[str, dict] = {}
        self.adaptation_notes: dict[str, list[dict]] = {}
        self.admin_reviews: list[dict] = []
        self.sparring_log_requests: dict[tuple[str, str], str] = {}
        self.sparring_logs: list[dict] = []
        self.exercise_logs: list[dict] = []
        # Simulates the bulk write failing in the database (nothing commits).
        self.fail_next_exercise_log_batch = False
        # Simulates the admin-review insert failing inside record_sparring_log.
        self.fail_sparring_review_insert = False
        self.push_subscriptions: dict[str, dict] = {}
        self.beta_feedback: list[dict] = []
        self.feedback_screenshots: dict[str, tuple[bytes, str]] = {}
        self._feedback_rate_events: dict[tuple[str, str], list[datetime]] = {}
        self.get_admin_athlete_calls = 0
        self.list_admin_athletes_by_ids_calls = 0
        # When True, admin-queue profile enrichment degrades to id-only rows
        # tagged with ``profile_enrichment_failed`` (mirrors a profiles outage).
        self.fail_profile_enrichment = False
        self.admin_emails: set[str] = {
            email.strip().lower() for email in (admin_emails or set()) if email
        }
        self._plan_generation_limit_events: dict[str, list[datetime]] = {}
        self._generation_job_daily_limit_lock = threading.RLock()
        self._xp_lock = threading.RLock()
        self._intake_injury_lock = threading.RLock()

    def validate_runtime_schema(self) -> None:
        return None

    def check_plan_generation_short_window_limit(
        self,
        athlete_id: str,
        max_requests: int,
        window_seconds: float,
    ) -> tuple[bool, int]:
        if max_requests <= 0:
            return True, 0
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=max(1.0, window_seconds))
        bucket = [
            ts for ts in self._plan_generation_limit_events.get(athlete_id, [])
            if ts > cutoff
        ]
        self._plan_generation_limit_events[athlete_id] = bucket
        if len(bucket) >= max_requests:
            retry_after = max(
                1,
                math.ceil(max(1.0, window_seconds) - (now - bucket[0]).total_seconds()),
            )
            return False, retry_after
        bucket.append(now)
        return True, 0

    def is_admin_email(self, email: str) -> bool:
        if not email:
            return False
        normalized = email.strip().lower()
        if normalized in self.admin_emails:
            return True
        # Mirror ensure_profile's @unlxck.test test pattern so existing fixtures
        # do not need to register every admin email explicitly.
        return normalized.endswith("@unlxck.test")

    def _is_admin_email(self, email: str) -> bool:
        return self.is_admin_email(email)

    def _classify_running_job_staleness(self, job: dict, *, stale_after_seconds: int) -> str:
        if str(job.get("status") or "") != "running":
            return "fresh"
        raw_stage1_timeout = os.getenv("APP_STAGE1_PLANNER_TIMEOUT_SECONDS", "240").strip()
        try:
            stage1_stale_after_seconds = max(1, int(float(raw_stage1_timeout)))
        except (TypeError, ValueError):
            stage1_stale_after_seconds = 240
        if raw_stage1_timeout in {"", "0", "none", "None", "NONE"}:
            stage1_stale_after_seconds = 240
        if is_job_loaded_stalled_generation_job(job, stale_after_seconds=stale_after_seconds):
            return "job_loaded_stalled"
        if is_startup_stale_generation_job(job, stale_after_seconds=stale_after_seconds):
            return "startup_stale"
        if is_stage1_planner_stalled_generation_job(job, stale_after_seconds=stage1_stale_after_seconds):
            return "stage1_planner_stalled"
        started_at = _parse_iso(job.get("started_at"))
        if started_at is not None:
            hard_ceiling_age = (datetime.now(timezone.utc) - started_at).total_seconds()
            if hard_ceiling_age >= _generation_hard_max_runtime_seconds():
                return "mid_pipeline_stale"
        reference = _latest_job_activity_at(job)
        if reference is None:
            return "fresh"
        age = (datetime.now(timezone.utc) - reference).total_seconds()
        return "fresh" if age < max(1, stale_after_seconds) else "mid_pipeline_stale"

    def ensure_profile(self, user: AuthenticatedUser) -> dict:
        existing = self.profiles.get(user.user_id)
        if existing:
            existing["updated_at"] = _now()
            return existing
        role = "admin" if self._is_admin_email(user.email) else "athlete"
        profile = {
            "id": user.user_id,
            "email": user.email,
            "username": None,
            "username_change_history": [],
            "role": role,
            "access_status": "approved",
            # Mirrors AppStore._build_profile_payload: only a name declared at
            # signup seeds the profile, never the email fallback.
            "full_name": _signup_full_name(user),
            "technical_style": [],
            "tactical_style": [],
            "stance": "",
            "professional_status": "",
            "record_summary": "",
            "athlete_timezone": "",
            "athlete_locale": "",
            "appearance_mode": "dark",
            "onboarding_draft": None,
            "nutrition_profile": {},
            # Mirrors AppStore._build_profile_payload: the date supplied to the
            # signup form seeds the profile once, and an under-13 or malformed
            # value is dropped rather than stored.
            "date_of_birth": _signup_date_of_birth(user),
            "terms_version": None,
            "terms_accepted_at": None,
            "health_consent_version": None,
            "health_data_consent": False,
            "health_consent_at": None,
            "health_consent_withdrawn_at": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.profiles[user.user_id] = profile
        return profile

    def get_profile(self, athlete_id: str) -> dict | None:
        return self.profiles.get(athlete_id)

    def get_profile_compliance(self, athlete_id: str) -> dict | None:
        from api.store import PROFILE_COMPLIANCE_SELECT
        row = self.get_profile(athlete_id)
        return {key: row[key] for key in PROFILE_COMPLIANCE_SELECT.split(",") if key in row} if row is not None else None

    def record_compliance_acceptance(
        self,
        athlete_id: str,
        *,
        date_of_birth: str | None = None,
        accept_terms: bool | None = None,
        health_data_consent: bool | None = None,
    ) -> dict:
        """Mirrors AppStore.record_compliance_acceptance: server owns the stamps."""
        profile = self.profiles[athlete_id]
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
            profile["date_of_birth"] = parsed.isoformat()
        now = _now()
        if accept_terms:
            profile["terms_version"] = TERMS_VERSION
            profile["terms_accepted_at"] = now
        if health_data_consent is True:
            profile["health_data_consent"] = True
            profile["health_consent_version"] = HEALTH_CONSENT_VERSION
            profile["health_consent_at"] = now
            profile["health_consent_withdrawn_at"] = None
        elif health_data_consent is False:
            profile["health_data_consent"] = False
            profile["health_consent_withdrawn_at"] = now
        profile["updated_at"] = now
        return profile

    def record_onboarding_stage(self, athlete_id: str, *, completed: bool = False) -> None:
        row = self.profiles[athlete_id]
        row.setdefault("onboarding_started_at", datetime.now(timezone.utc).isoformat())
        if completed:
            row.setdefault("onboarding_completed_at", datetime.now(timezone.utc).isoformat())

    def update_profile(self, athlete_id: str, update: ProfileUpdateRequest) -> dict:
        profile = self.profiles[athlete_id]
        if update.training_habits is not None and update.training_habits_revision != profile.get("training_habits_revision", 0):
            raise HTTPException(409, "Routines changed on another device. Refresh and try again.")
        data = update.model_dump(mode="json", exclude_none=True)
        if update.training_habits is not None:
            data["training_habits_revision"] = update.training_habits_revision + 1
        if "record" in data:
            data["record_summary"] = data.pop("record")
        if "private_trial_acknowledged" in data:
            # Mirrors AppStore.update_profile: the client sends the intent and
            # the server owns the timestamp.
            acknowledged = bool(data.pop("private_trial_acknowledged"))
            data["private_trial_ack_at"] = _now() if acknowledged else None
        profile.update(data)
        profile["updated_at"] = _now()
        return profile

    def change_username(self, athlete_id: str, username: str) -> dict:
        profile = self.profiles[athlete_id]
        normalized = validate_username(username)
        current = (profile.get("username") or "").lower() or None
        if normalized == current:
            return profile
        for other_id, other_profile in self.profiles.items():
            if other_id == athlete_id:
                continue
            if (other_profile.get("username") or "").lower() == normalized:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="That username is already taken. Pick another.",
                )
        history_raw = profile.get("username_change_history") or []
        history: list[str] = [str(entry) for entry in history_raw if entry]
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=USERNAME_CHANGE_WINDOW_DAYS)
        recent: list[datetime] = []
        for entry in history:
            try:
                parsed = datetime.fromisoformat(entry.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed >= cutoff:
                recent.append(parsed)
        if len(recent) >= USERNAME_MAX_CHANGES_PER_WINDOW:
            earliest = min(recent)
            next_available = earliest + timedelta(days=USERNAME_CHANGE_WINDOW_DAYS)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"You can change your username up to {USERNAME_MAX_CHANGES_PER_WINDOW} times "
                    f"every {USERNAME_CHANGE_WINDOW_DAYS} days. "
                    f"Next change available {next_available.isoformat()}."
                ),
            )
        profile["username"] = normalized
        profile["username_change_history"] = [entry.isoformat() for entry in recent] + [now.isoformat()]
        profile["updated_at"] = _now()
        return profile

    def get_latest_intake(self, athlete_id: str) -> dict | None:
        items = self.intakes.get(athlete_id, [])
        return items[-1] if items else None

    def get_intake(self, intake_id: str) -> dict | None:
        for items in self.intakes.values():
            for intake in items:
                if intake["id"] == intake_id:
                    return intake
        return None

    def create_intake(self, athlete_id: str, request: PlanRequest) -> dict:
        intake = {
            "id": f"intake_{uuid4().hex[:10]}",
            "athlete_id": athlete_id,
            "fight_date": None if request.no_scheduled_fight else (request.fight_date.strip() or None),
            "technical_style": request.athlete.technical_style,
            "intake": request.model_dump(mode="json"),
            "created_at": _now(),
        }
        self.intakes.setdefault(athlete_id, []).append(intake)
        return intake

    def update_intake(
        self,
        intake_id: str,
        *,
        intake: dict,
        fight_date: str | None,
        technical_style: list[str],
    ) -> dict:
        for athlete_intakes in self.intakes.values():
            for row in athlete_intakes:
                if row["id"] != intake_id:
                    continue
                row["intake"] = intake
                row["fight_date"] = fight_date
                row["technical_style"] = technical_style
                row["updated_at"] = _now()
                return row
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intake not found")

    def create_plan(self, *, athlete_id: str, intake_id: str | None, request: PlanRequest, result: dict, plan_id: str | None = None) -> dict:
        profile = self.profiles[athlete_id]
        plan_id = plan_id or str(uuid4())
        result_status = str(result.get("status") or "generated").strip().lower()
        if not is_plan_status(result_status):
            raise _status_transition_error(f"unknown plan status: {result_status!r}")
        row = {
            "id": plan_id,
            "athlete_id": athlete_id,
            "intake_id": intake_id,
            "fight_date": request.fight_date.strip() or None,
            "technical_style": request.athlete.technical_style,
            "plan_name": result.get("plan_name", ""),
            "status": result_status,
            "plan_text": result.get("plan_text", ""),
            "draft_plan_text": result.get("draft_plan_text", result.get("plan_text", "")),
            "final_plan_text": result.get("final_plan_text", result.get("plan_text", "")),
            "coach_notes": result.get("coach_notes", ""),
            "pdf_url": result.get("pdf_url"),
            "why_log": result.get("why_log", {}),
            "planning_brief": result.get("planning_brief"),
            "stage2_payload": result.get("stage2_payload"),
            "parsing_metadata": result.get("parsing_metadata", {}),
            "stage2_handoff_text": result.get("stage2_handoff_text", ""),
            "stage2_retry_text": result.get("stage2_retry_text", ""),
            "stage2_validator_report": result.get("stage2_validator_report", {}),
            "stage2_status": result.get("stage2_status", ""),
            "stage2_attempt_count": result.get("stage2_attempt_count", 0),
            "structured_plan": result.get("structured_plan"),
            "schema_version": result.get("schema_version"),
            "created_at": _now(),
            "full_name": profile["full_name"],
        }
        self.plans[plan_id] = row
        return row

    def list_user_plans(self, athlete_id: str) -> list[dict]:
        rows = [plan for plan in self.plans.values() if plan["athlete_id"] == athlete_id]
        return sorted(rows, key=lambda row: row["created_at"], reverse=True)

    def get_plan(self, plan_id: str) -> dict | None:
        return self.plans.get(plan_id)

    def get_plan_for_athlete(self, plan_id: str, athlete_id: str) -> dict | None:
        row = self.plans.get(plan_id)
        if not row or str(row.get("athlete_id")) != athlete_id:
            return None
        return row

    def get_latest_plan(self, athlete_id: str) -> dict | None:
        plans = self.list_user_plans(athlete_id)
        return plans[0] if plans else None

    def get_plan_identity_for_athlete(self, plan_id: str, athlete_id: str) -> dict | None:
        row = self.get_plan_for_athlete(plan_id, athlete_id)
        return {"id": row["id"], "athlete_id": row["athlete_id"]} if row else None

    def get_training_plan_for_athlete(self, plan_id: str, athlete_id: str) -> dict | None:
        row = self.get_plan_for_athlete(plan_id, athlete_id)
        # Exercise the actual production projection, including absent fields.
        return {key: row[key] for key in TRAINING_PLAN_SELECT.split(",") if key in row} if row else None

    def compare_and_set_plan_exercise_overrides(
        self, plan_id: str, athlete_id: str, *, expected_revision: int, overrides: list[dict],
    ) -> dict | None:
        row = self.get_plan_for_athlete(plan_id, athlete_id)
        if row is None or int(row.get("exercise_overrides_revision") or 0) != expected_revision:
            return None
        row["exercise_overrides"] = copy.deepcopy(overrides)
        row["exercise_overrides_revision"] = expected_revision + 1
        return row

    def get_active_plan_id(self, athlete_id: str) -> str | None:
        return self.active_plan_ids.get(athlete_id)

    def set_active_plan_id(self, athlete_id: str, plan_id: str) -> None:
        self.active_plan_ids[athlete_id] = plan_id

    def rename_plan(self, plan_id: str, plan_name: str) -> dict:
        row = self.plans.get(plan_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        row["plan_name"] = plan_name
        return row

    def rename_plan_for_athlete(self, plan_id: str, athlete_id: str, plan_name: str) -> dict:
        row = self.get_plan_for_athlete(plan_id, athlete_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        row["plan_name"] = plan_name
        return row

    def archive_plan(self, plan_id: str) -> dict:
        if plan_id not in self.plans:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        row = self.plans[plan_id]
        try:
            row["status"] = require_plan_transition(row.get("status") or "generated", "archived")
        except ValueError as exc:
            raise _status_transition_error(str(exc)) from exc
        return row

    def archive_plan_for_athlete(self, plan_id: str, athlete_id: str) -> dict:
        row = self.get_plan_for_athlete(plan_id, athlete_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        try:
            row["status"] = require_plan_transition(row.get("status") or "generated", "archived")
        except ValueError as exc:
            raise _status_transition_error(str(exc)) from exc
        return row

    def delete_plan(self, plan_id: str) -> None:
        if plan_id not in self.plans:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        del self.plans[plan_id]

    def delete_plan_for_athlete(self, plan_id: str, athlete_id: str) -> None:
        row = self.get_plan_for_athlete(plan_id, athlete_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        del self.plans[plan_id]

    def create_or_get_generation_job(
        self,
        *,
        athlete_id: str,
        client_request_id: str,
        source: str,
        request_payload: dict,
        plan_id: str | None = None,
        intake_id: str | None = None,
        stale_after_seconds: int = 90,
    ) -> dict:
        payload_hash = _stable_payload_hash(request_payload)
        self._fail_stale_active_generation_jobs_for_athlete(
            athlete_id,
            stale_after_seconds=stale_after_seconds,
        )
        for job in self.generation_jobs.values():
            if job["athlete_id"] == athlete_id and job["client_request_id"] == client_request_id:
                if is_startup_stale_generation_job(job, stale_after_seconds=stale_after_seconds):
                    now = _now()
                    reset_changes = {
                        "source": source,
                        "request_payload": request_payload,
                        "payload_hash": payload_hash,
                        "status": "queued",
                        "error": None,
                        "stage1_result": None,
                        "final_result": None,
                        "heartbeat_at": None,
                        "started_at": None,
                        "completed_at": None,
                        "progress_milestones": [],
                        "claimed_by": None,
                        "claimed_at": None,
                        "updated_at": now,
                    }

                    if plan_id is not None:
                        reset_changes["plan_id"] = plan_id
                    if intake_id is not None:
                        reset_changes["intake_id"] = intake_id

                    job.update(reset_changes)
                existing_hash = job.get("payload_hash")
                if existing_hash and str(existing_hash) != payload_hash:
                    raise client_request_id_payload_mismatch_error()
                return dict(job)
        active = self.reconcile_active_generation_job_for_athlete(athlete_id, stale_after_seconds=stale_after_seconds)
        # Mirror SupabaseAppStore.create_or_get_generation_job: only a job that is
        # still queued/running blocks a new request. reconcile_active_generation_job_for_athlete
        # recovers a stale running job to a terminal status (e.g. failed) and returns
        # it; such a job must not be treated as in-flight, otherwise a mid-pipeline
        # stale job would wrongly 409 a new request instead of being superseded.
        if active and str(active.get("status") or "") in {"queued", "running"}:
            if str(active.get("client_request_id") or "") == client_request_id:
                existing_hash = active.get("payload_hash")
                if existing_hash and str(existing_hash) != payload_hash:
                    raise client_request_id_payload_mismatch_error()
                return dict(active)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A generation job is already queued or running for this account.",
            )
        now = _now()
        job_id = f"job_{uuid4().hex[:10]}"
        job = {
            "id": job_id,
            "athlete_id": athlete_id,
            "client_request_id": client_request_id,
            "source": source,
            "request_payload": request_payload,
            "payload_hash": payload_hash,
            "status": "queued",
            "error": None,
            "intake_id": intake_id,
            "stage1_result": None,
            "final_result": None,
            "plan_id": plan_id,
            "attempt_count": 0,
            "heartbeat_at": None,
            "started_at": None,
            "completed_at": None,
            "failed_at": None,
            "claimed_by": None,
            "claimed_at": None,
            "progress_milestones": [],
            "created_at": now,
            "updated_at": now,
        }
        self.generation_jobs[job_id] = job
        return dict(job)

    def _fail_stale_active_generation_jobs_for_athlete(
        self,
        athlete_id: str,
        *,
        stale_after_seconds: int,
        exclude_client_request_id: str | None = None,
    ) -> None:
        cutoff_seconds = max(1, stale_after_seconds)
        now_dt = datetime.now(timezone.utc)
        now = _now()
        for job in self.generation_jobs.values():
            if str(job.get("athlete_id") or "") != athlete_id:
                continue
            if exclude_client_request_id and str(job.get("client_request_id") or "") == exclude_client_request_id:
                continue
            if str(job.get("status") or "") not in {"queued", "running"}:
                continue
        cutoff_seconds = max(1, stale_after_seconds)
        now_dt = datetime.now(timezone.utc)
        now = _now()
        for job in self.generation_jobs.values():
            if str(job.get("athlete_id") or "") != athlete_id:
                continue
            if str(job.get("status") or "") not in {"queued", "running"}:
                continue
            latest = _latest_job_activity_at(job)
            if latest is None:
                continue
            latest = latest.astimezone(timezone.utc)
            if (now_dt - latest).total_seconds() < cutoff_seconds:
                continue
            milestones = list(job.get("progress_milestones") or [])
            if not any(isinstance(item, dict) and item.get("code") == "stale_job_reaped" for item in milestones):
                milestones.append(
                    {
                        "code": "stale_job_reaped",
                        "label": "Stale job reaped",
                        "detail": "Job activity timed out and was failed so a new generation can start.",
                        "meta": {},
                        "at": now,
                    }
                )
            job.update(
                {
                    "status": "failed",
                    "error": "Generation job stalled. Please try again.",
                    "completed_at": now,
                    "failed_at": now,
                    "heartbeat_at": now,
                    "progress_milestones": milestones,
                    "updated_at": now,
                }
            )

    def create_or_get_generation_job_with_daily_limit(
        self,
        *,
        athlete_id: str,
        client_request_id: str,
        source: str,
        request_payload: dict,
        daily_limit: int,
        day_start_iso: str,
        limit_reached_detail: str,
        counted_sources: set[str],
        plan_id: str | None = None,
        intake_id: str | None = None,
        stale_after_seconds: int = 90,
    ) -> dict:
        with self._generation_job_daily_limit_lock:
            payload_hash = _stable_payload_hash(request_payload)
            for job in self.generation_jobs.values():
                if job["athlete_id"] == athlete_id and job["client_request_id"] == client_request_id:
                    existing_hash = job.get("payload_hash")
                    if existing_hash and str(existing_hash) != payload_hash:
                        raise client_request_id_payload_mismatch_error()
                    return dict(job)
            if daily_limit > 0:
                jobs_today = self.count_generation_jobs_for_athlete_since(
                    athlete_id,
                    day_start_iso,
                    sources=counted_sources,
                )
                if jobs_today >= daily_limit:
                    raise generation_daily_limit_error(limit_reached_detail)
            return self.create_or_get_generation_job(
                athlete_id=athlete_id,
                client_request_id=client_request_id,
                source=source,
                request_payload=request_payload,
                plan_id=plan_id,
                intake_id=intake_id,
                stale_after_seconds=stale_after_seconds,
            )

    def get_generation_job(self, job_id: str) -> dict | None:
        job = self.generation_jobs.get(job_id)
        return dict(job) if job else None

    def recover_generation_job_if_stale(self, job: dict | None) -> dict | None:
        if not job or str(job.get("status") or "") != "running":
            return dict(job) if job else None
        live = self.generation_jobs.get(str(job.get("id") or ""))
        if live is None:
            return dict(job)
        staleness = self._classify_running_job_staleness(live, stale_after_seconds=90)
        if staleness == "job_loaded_stalled":
            milestones = list(live.get("progress_milestones") or [])
            now = _now()
            if int(live.get("attempt_count") or 0) < 2:
                milestones.append({"code": "worker_claim_stalled_requeued", "label": "Worker claim stalled", "detail": "Worker loaded the generation job but did not reach request parsing; job was requeued for recovery.", "meta": {}, "at": now})
                live.update({"status": "queued", "error": None, "started_at": None, "heartbeat_at": None, "completed_at": None, "progress_milestones": milestones, "claimed_by": None, "claimed_at": None, "updated_at": now})
            else:
                milestones.append({"code": "worker_claim_stalled_failed", "label": "Worker stalled after loading job", "detail": "Worker loaded the generation job but did not reach request parsing after retry.", "meta": {}, "at": now})
                live.update({"status": "failed", "error": "Generation worker stalled after loading the job.", "completed_at": now, "heartbeat_at": now, "progress_milestones": milestones, "updated_at": now})
        if staleness == "stage1_planner_stalled":
            now = _now()
            milestones = list(live.get("progress_milestones") or [])
            milestones.append(
                {
                    "code": "stage1_planner_timeout",
                    "label": "Stage 1 planner timed out",
                    "detail": "Planner did not return after invocation and the job was failed for recovery.",
                    "meta": {},
                    "at": now,
                }
            )
            live.update(
                {
                    "status": "failed",
                    "error": "Stage 1 planner stalled after planner invocation.",
                    "completed_at": now,
                    "heartbeat_at": now,
                    "progress_milestones": milestones,
                    "updated_at": now,
                }
            )
        return dict(live)

    def get_generation_job_by_client_request_id(self, *, athlete_id: str, client_request_id: str) -> dict | None:
        for job in self.generation_jobs.values():
            if job["athlete_id"] == athlete_id and job["client_request_id"] == client_request_id:
                return dict(job)
        return None

    def get_visible_active_generation_job_for_athlete(self, athlete_id: str) -> dict | None:
        rows = [
            dict(job)
            for job in self.generation_jobs.values()
            if str(job.get("athlete_id") or "") == athlete_id
            and str(job.get("status") or "") in {"queued", "running"}
        ]
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return rows[0] if rows else None

    def reconcile_active_generation_job_for_athlete(
        self,
        athlete_id: str,
        *,
        stale_after_seconds: int = 90,
    ) -> dict | None:
        rows = [
            job
            for job in self.generation_jobs.values()
            if str(job.get("athlete_id") or "") == athlete_id and str(job.get("status") or "") in {"queued", "running"}
        ]
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        for row in rows[:10]:
            if str(row.get("status") or "") == "queued":
                return dict(row)
            staleness = self._classify_running_job_staleness(row, stale_after_seconds=stale_after_seconds)
            if staleness == "fresh":
                return dict(row)
            if staleness == "startup_stale":
                now = _now()
                row.update(
                    {
                        "status": "queued",
                        "error": None,
                        "heartbeat_at": None,
                        "started_at": None,
                        "completed_at": None,
                        "stage1_result": None,
                        "final_result": None,
                        "progress_milestones": [],
                        "claimed_by": None,
                        "claimed_at": None,
                        "updated_at": now,
                    }
                )
            elif staleness == "job_loaded_stalled":
                now = _now()
                milestones = list(row.get("progress_milestones") or [])
                if int(row.get("attempt_count") or 0) < 2:
                    milestones.append({"code": "worker_claim_stalled_requeued", "label": "Worker claim stalled", "detail": "Worker loaded the generation job but did not reach request parsing; job was requeued for recovery.", "meta": {}, "at": now})
                    row.update({"status": "queued", "error": None, "heartbeat_at": None, "started_at": None, "completed_at": None, "progress_milestones": milestones, "claimed_by": None, "claimed_at": None, "updated_at": now})
                else:
                    milestones.append({"code": "worker_claim_stalled_failed", "label": "Worker stalled after loading job", "detail": "Worker loaded the generation job but did not reach request parsing after retry.", "meta": {}, "at": now})
                    row.update({"status": "failed", "error": "Generation worker stalled after loading the job.", "completed_at": now, "heartbeat_at": now, "progress_milestones": milestones, "updated_at": now})
            elif staleness == "stage1_planner_stalled":
                now = _now()
                milestones = list(row.get("progress_milestones") or [])
                milestones.append(
                    {
                        "code": "stage1_planner_timeout",
                        "label": "Stage 1 planner timed out",
                        "detail": "Planner did not return after invocation and the job was failed for recovery.",
                        "meta": {},
                        "at": now,
                    }
                )
                row.update(
                    {
                        "status": "failed",
                        "error": "Stage 1 planner stalled after planner invocation.",
                        "completed_at": now,
                        "heartbeat_at": now,
                        "progress_milestones": milestones,
                        "updated_at": now,
                    }
                )
            else:
                now = _now()
                row.update(
                    {
                        "status": "failed",
                        "error": "Generation job stalled mid-pipeline and was failed for recovery.",
                        "completed_at": now,
                        "heartbeat_at": now,
                        "updated_at": now,
                    }
                )
            return dict(row)
        return None

    def count_generation_jobs_for_athlete_since(
        self,
        athlete_id: str,
        since_timestamp: str,
        *,
        sources: set[str] | None = None,
    ) -> int:
        since = datetime.fromisoformat(since_timestamp.replace("Z", "+00:00"))
        count = 0
        for job in self.generation_jobs.values():
            if job["athlete_id"] != athlete_id:
                continue
            created_at_raw = str(job.get("created_at") or "")
            if not created_at_raw:
                continue
            created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
            if created_at < since:
                continue
            if sources and str(job.get("source") or "") not in sources:
                continue
            count += 1
        return count

    def get_generation_job_by_plan_id(self, plan_id: str) -> dict | None:
        matches = [job for job in self.generation_jobs.values() if str(job.get("plan_id") or "") == plan_id]
        if not matches:
            return None
        matches.sort(key=lambda job: job.get("completed_at") or job.get("updated_at") or "", reverse=True)
        return dict(matches[0])

    def get_latest_generation_job_for_athlete(self, athlete_id: str) -> dict | None:
        rows = [
            dict(job)
            for job in self.generation_jobs.values()
            if str(job.get("athlete_id") or "") == athlete_id
        ]
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return rows[0] if rows else None

    def has_active_generation_job_for_plan(self, plan_id: str) -> bool:
        return any(
            str(job.get("plan_id") or "") == plan_id and str(job.get("status") or "") in {"queued", "running"}
            for job in self.generation_jobs.values()
        )

    def list_generation_jobs_for_athlete(self, athlete_id: str, *, limit: int = 10) -> list[dict]:
        rows = [
            dict(job)
            for job in self.generation_jobs.values()
            if str(job.get("athlete_id") or "") == athlete_id
        ]
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return rows[:limit]

    def _attach_profile_contacts(self, rows: list[dict], *, id_key: str = "athlete_id") -> list[dict]:
        """Mirror AppStore._attach_profile_contacts for admin-queue enrichment.

        Honours ``fail_profile_enrichment`` so tests can exercise the degraded
        path where the queue still renders with id-only rows.
        """
        for row in rows:
            existing = row.get("profiles")
            if isinstance(existing, dict) and (existing.get("email") or existing.get("full_name")):
                continue
            if self.fail_profile_enrichment:
                row["profile_enrichment_failed"] = True
                row["profiles"] = {"email": "", "full_name": ""}
                continue
            profile = self.profiles.get(str(row.get(id_key) or ""), {})
            row["profiles"] = {
                "email": profile.get("email", ""),
                "full_name": profile.get("full_name", ""),
            }
        return rows

    def list_admin_triage_generation_jobs(self, *, limit: int = 50) -> list[dict]:
        protected_statuses = {"triage_blocked", "needs_review", "restricted_rehab_only", "medical_hold"}
        rows = []
        for job in self.generation_jobs.values():
            final_result = job.get("final_result") if isinstance(job.get("final_result"), dict) else {}
            status_value = str(job.get("status") or "").strip().lower()
            plan_id = str(job.get("plan_id") or "").strip()
            triage_status = str(final_result.get("status") or "").strip().lower()
            stage2_status = str(final_result.get("stage2_status") or "").strip().lower()
            if status_value != "review_required" or plan_id or triage_status not in protected_statuses:
                continue
            if stage2_status == "triage_resume_approved":
                continue
            rows.append(dict(job))
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return self._attach_profile_contacts(rows[:limit])

    def list_admin_active_generation_jobs(self, *, limit: int = 50) -> list[dict]:
        rows = []
        for job in self.generation_jobs.values():
            status_value = str(job.get("status") or "").strip().lower()
            if status_value not in {"queued", "running"}:
                continue
            rows.append(dict(job))
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return self._attach_profile_contacts(rows[:limit])

    def list_orphaned_terminal_generation_jobs(self, *, limit: int = 500) -> list[dict]:
        rows: list[dict] = []
        for job in self.generation_jobs.values():
            status_value = str(job.get("status") or "").strip().lower()
            if status_value not in {"completed", "review_required"}:
                continue
            plan_id = str(job.get("plan_id") or "").strip()
            if not plan_id or self.get_plan(plan_id) is None:
                rows.append(
                    {
                        "job_id": str(job.get("id") or ""),
                        "athlete_id": str(job.get("athlete_id") or ""),
                        "status": status_value,
                        "source": str(job.get("source") or ""),
                        "plan_id": plan_id,
                    }
                )
        rows.sort(key=lambda row: row["job_id"], reverse=True)
        return rows[:limit]

    def list_failed_triage_resume_jobs_with_approved_marker(self, *, limit: int = 500) -> list[dict]:
        rows: list[dict] = []
        for job in self.generation_jobs.values():
            status_value = str(job.get("status") or "").strip().lower()
            source_value = str(job.get("source") or "").strip().lower()
            if status_value != "failed" or source_value != "admin_triage_resume":
                continue
            plan_id = str(job.get("plan_id") or "").strip()
            plan_row = self.get_plan(plan_id) if plan_id else None
            if not plan_row:
                continue
            stage2_status = str(plan_row.get("stage2_status") or "").strip().lower()
            if stage2_status != "triage_resume_approved":
                continue
            rows.append(
                {
                    "job_id": str(job.get("id") or ""),
                    "plan_id": str(plan_row.get("id") or ""),
                    "athlete_id": str(job.get("athlete_id") or ""),
                }
            )
        rows.sort(key=lambda row: row["job_id"], reverse=True)
        return rows[:limit]

    def list_claimable_generation_jobs(self, *, limit: int = 20, stale_after_seconds: int = 90) -> list[dict]:
        rows = []
        for job in self.generation_jobs.values():
            status_value = str(job.get("status") or "")
            if status_value == "queued":
                rows.append(dict(job))
                continue
            if status_value != "running":
                continue
            if is_startup_stale_generation_job(job, stale_after_seconds=stale_after_seconds):
                rows.append(dict(job))
        rows.sort(key=lambda row: str(row.get("created_at") or ""))
        return rows[:limit]

    def claim_generation_job_start(self, job_id: str, *, stale_after_seconds: int = 90, worker_id: str | None = None) -> dict | None:
        job = self.generation_jobs.get(job_id)
        if not job:
            return None
        current_status = str(job.get("status") or "").strip().lower() or "queued"
        if current_status not in {"queued", "running"}:
            return None
        if current_status == "running" and not is_startup_stale_generation_job(
            job,
            stale_after_seconds=stale_after_seconds,
        ):
            return None
        # Mirror SupabaseAppStore: the worker reclaim path enforces the same
        # job_loaded retry cap as the read-side recovery so a repeatedly-dying
        # worker cannot re-grab the job past its attempt budget.
        if (
            current_status == "running"
            and int(job.get("attempt_count") or 0) >= _generation_startup_max_attempts()
            and is_job_loaded_stalled_generation_job(job, stale_after_seconds=stale_after_seconds)
        ):
            now_iso = _now()
            milestones = list(job.get("progress_milestones") or [])
            milestones.append(
                {
                    "code": "worker_claim_stalled_failed",
                    "label": "Worker stalled after loading job",
                    "detail": "Worker loaded the generation job but did not reach request parsing after retry.",
                    "meta": {},
                    "at": now_iso,
                }
            )
            job.update(
                {
                    "status": "failed",
                    "error": "Generation worker stalled after loading the job.",
                    "completed_at": now_iso,
                    "heartbeat_at": now_iso,
                    "progress_milestones": milestones,
                    "updated_at": now_iso,
                }
            )
            return None
        now_iso = _now()
        job["status"] = "running"
        job["heartbeat_at"] = now_iso
        job["started_at"] = now_iso if job.get("started_at") is None else job["started_at"]
        job["attempt_count"] = int(job.get("attempt_count") or 0) + 1
        job["claimed_by"] = (worker_id or "").strip() or generation_worker_id()
        job["claimed_at"] = now_iso
        job["error"] = None
        job["completed_at"] = None
        job["progress_milestones"] = [
            {
                "code": "job_loaded",
                "label": "Generation job loaded",
                "detail": "Worker loaded the persisted generation job.",
                "meta": {},
                "at": now_iso,
            }
        ]
        job["updated_at"] = now_iso
        return dict(job)

    def claim_generation_job(self, job_id: str, *, stale_after_seconds: int = 90, worker_id: str | None = None) -> dict | None:
        return self.claim_generation_job_start(job_id, stale_after_seconds=stale_after_seconds, worker_id=worker_id)

    def count_active_generation_jobs(self, *, stale_after_seconds: int = 90) -> int:
        now = datetime.now(timezone.utc)
        count = 0
        for job in self.generation_jobs.values():
            if str(job.get("status") or "") != "running":
                continue
            heartbeat_raw = job.get("heartbeat_at")
            started_raw = job.get("started_at")
            heartbeat = (
                datetime.fromisoformat(str(heartbeat_raw).replace("Z", "+00:00"))
                if isinstance(heartbeat_raw, str) and heartbeat_raw
                else None
            )
            started_at = (
                datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
                if isinstance(started_raw, str) and started_raw
                else None
            )
            last_progress_at = heartbeat or started_at
            if last_progress_at and (now - last_progress_at).total_seconds() < stale_after_seconds:
                count += 1
        return count

    def update_generation_job(self, job_id: str, *, refresh: bool = True, **changes: dict) -> dict:
        # ``refresh`` is control data, not a column. Taking it through
        # **changes would persist {"refresh": False} into the job row and
        # return the row where SupabaseAppStore returns {}, so FakeStore-backed
        # tests would exercise a contract production does not have.
        job = self.generation_jobs.get(job_id)
        if not job:
            raise generation_job_not_found_error()
        payload = dict(changes)
        if "status" in payload:
            next_status = str(payload.get("status") or "").strip().lower()
            if not is_generation_job_status(next_status):
                raise _status_transition_error(f"unknown generation job status: {next_status!r}")
            try:
                payload["status"] = require_generation_job_transition(job.get("status") or "queued", next_status)
            except ValueError as exc:
                raise _status_transition_error(str(exc)) from exc
        job.update(payload)
        job["updated_at"] = _now()
        if not refresh:
            return {}
        return dict(job)

    def _assert_generation_job_terminal_owner(
        self,
        job_id: str,
        *,
        expected_status: str,
        expected_attempt_count: int,
        expected_worker_id: str | None = None,
        enforce_worker_ownership: bool = True,
    ) -> dict:
        job = self.generation_jobs.get(job_id)
        if not job:
            raise generation_job_not_found_error()
        current_status = str(job.get("status") or "")
        if current_status != expected_status:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"wrong_generation_job_status:{job_id} expected {expected_status}, got {current_status or '<null>'}",
            )
        current_attempt = int(job.get("attempt_count") or 0)
        if current_attempt != expected_attempt_count:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"stale_generation_job_attempt:{job_id} expected {expected_attempt_count}, got {current_attempt}",
            )
        # Mirror the SQL RPC ownership guard: enforce only when the job has a
        # recorded owner (rows claimed before the migration have none).
        if enforce_worker_ownership:
            checked_worker_id = (expected_worker_id or "").strip() or generation_worker_id()
            claimed_by = str(job.get("claimed_by") or "")
            if claimed_by and claimed_by != checked_worker_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"stale_generation_job_worker:{job_id} expected {checked_worker_id}, got {claimed_by}",
                )
        return job

    def complete_generation_job(
        self,
        job_id: str,
        *,
        expected_attempt_count: int,
        final_status: str,
        final_result: dict | None = None,
        plan_id: str | None = None,
        error: str | None = None,
        completed_at: str | None = None,
        heartbeat_at: str | None = None,
        expected_status: str = "running",
        expected_worker_id: str | None = None,
        enforce_worker_ownership: bool = True,
    ) -> dict:
        if final_status not in {"completed", "review_required"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"invalid_terminal_status:{final_status}",
            )
        job = self._assert_generation_job_terminal_owner(
            job_id,
            expected_status=expected_status,
            expected_attempt_count=expected_attempt_count,
            expected_worker_id=expected_worker_id,
            enforce_worker_ownership=enforce_worker_ownership,
        )
        now = completed_at or _now()
        job.update(
            {
                "status": final_status,
                "error": error,
                "completed_at": now,
                "failed_at": None,
                "heartbeat_at": heartbeat_at or now,
                "updated_at": _now(),
            }
        )
        if final_result is not None:
            job["final_result"] = final_result
        if plan_id is not None:
            job["plan_id"] = plan_id
        return dict(job)

    def fail_generation_job(
        self,
        job_id: str,
        *,
        expected_attempt_count: int,
        error: str,
        final_result: dict | None = None,
        plan_id: str | None = None,
        progress_milestones: list | None = None,
        failed_at: str | None = None,
        heartbeat_at: str | None = None,
        expected_status: str = "running",
        expected_worker_id: str | None = None,
        enforce_worker_ownership: bool = True,
    ) -> dict:
        job = self._assert_generation_job_terminal_owner(
            job_id,
            expected_status=expected_status,
            expected_attempt_count=expected_attempt_count,
            expected_worker_id=expected_worker_id,
            enforce_worker_ownership=enforce_worker_ownership,
        )
        now = failed_at or _now()
        job.update(
            {
                "status": "failed",
                "error": error or "Generation job failed.",
                "completed_at": now,
                "failed_at": now,
                "heartbeat_at": heartbeat_at or now,
                "updated_at": _now(),
            }
        )
        if final_result is not None:
            job["final_result"] = final_result
        if plan_id is not None:
            job["plan_id"] = plan_id
        if progress_milestones is not None:
            job["progress_milestones"] = progress_milestones
        return dict(job)

    def record_stage2_cost(self, job_id: str, metadata: dict) -> None:
        job = self.generation_jobs.get(job_id)
        if not job or not isinstance(metadata, dict):
            return
        job.update(
            {
                column: metadata[column]
                for column in GENERATION_JOB_STAGE2_COST_COLUMNS
                if column in metadata
            }
        )

    def update_plan_stage2(self, plan_id: str, result: dict) -> dict:
        row = self.plans.get(plan_id)
        if not row:
            return None
        try:
            current_status = row.get("status") or "generated"
            next_status_input = result.get("status") or current_status
            next_status = require_plan_transition(current_status, next_status_input)
        except ValueError as exc:
            raise _status_transition_error(str(exc)) from exc
        row.update(
            {
                "status": next_status,
                "plan_text": result.get("plan_text", row.get("plan_text", "")),
                "draft_plan_text": result.get("draft_plan_text", row.get("draft_plan_text", row.get("plan_text", ""))),
                "final_plan_text": result.get("final_plan_text", row.get("final_plan_text", row.get("plan_text", ""))),
                "pdf_url": result.get("pdf_url"),
                "stage2_retry_text": result.get("stage2_retry_text", ""),
                "stage2_validator_report": result.get("stage2_validator_report", {}),
                "stage2_status": result.get("stage2_status", ""),
                "stage2_attempt_count": result.get("stage2_attempt_count", row.get("stage2_attempt_count", 0)),
            }
        )
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
                row[optional_field] = result.get(optional_field)
        return row

    def update_plan_stage2_if_unchanged(self, plan_id: str, result: dict, expected_snapshot: dict) -> dict:
        row = self.plans.get(plan_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        # Mirror the production guard: lightweight state markers only, never the
        # large text bodies (which PostgREST would push into the request URL).
        guarded_fields = (
            "status",
            "stage2_status",
            "stage2_attempt_count",
        )
        if any(row.get(field) != expected_snapshot.get(field) for field in guarded_fields):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Plan changed while Stage 2 structured processing was running; reload and try again.",
            )
        return self.update_plan_stage2(plan_id, result)

    def update_plan_structured_artifacts(
        self,
        plan_id: str,
        *,
        structured_plan,
        schema_version,
        stage2_validator_report: dict,
        expected_final_plan_text: str | None = None,
    ) -> dict:
        row = self.plans.get(plan_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        # Stale-write guard: when the conversion's source text is supplied, only
        # persist the card if the row's current text still matches. A concurrent
        # edit/reject mid-conversion makes the card stale, so the write is skipped.
        if expected_final_plan_text is not None:
            current_text = str(row.get("final_plan_text") or row.get("plan_text") or "")
            if current_text != str(expected_final_plan_text):
                return row
            if structured_plan is None and row.get("structured_plan") is not None:
                return row
        # Narrow write: only the structured-plan output fields. Status / plan_text
        # / stage2 fields are intentionally left untouched so a concurrent admin
        # action cannot be clobbered by a slow background conversion.
        if structured_plan is not None:
            row["structured_plan"] = structured_plan
            row["schema_version"] = schema_version
        row["stage2_validator_report"] = stage2_validator_report or {}
        return row

    def update_plan_triage_approval(self, plan_id: str, *, why_log: dict, stage2_status: str) -> dict:
        row = self.plans.get(plan_id)
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        row["why_log"] = why_log
        row["stage2_status"] = stage2_status
        return row

    def list_admin_plans(self, *, limit: int = 50, offset: int = 0, q: str | None = None) -> list[dict]:
        rows = [dict(plan) for plan in self.plans.values()]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        rows = _filter_admin_rows(rows, q, ("plan_name", "full_name", "status"))
        return self._attach_profile_contacts(rows[offset:offset + limit])

    def list_admin_review_plans(self, *, limit: int = 100) -> list[dict]:
        review_statuses = {
            "review_required",
            "held_for_review",
            "needs_review",
            "triage_blocked",
            "medical_hold",
            "restricted_rehab_only",
            "publishable_with_flags",
        }
        rows = [
            dict(plan)
            for plan in self.plans.values()
            if str(plan.get("status") or "").strip().lower() in review_statuses
        ]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return self._attach_profile_contacts(rows[:limit])

    def list_plans_missing_structured_plan(self, *, limit: int = 50) -> list[dict]:
        displayable = {"ready", "publishable_with_flags"}
        rows = [
            dict(plan)
            for plan in self.plans.values()
            if str(plan.get("status") or "").strip().lower() in displayable
            and plan.get("structured_plan") is None
        ]
        rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
        return rows[:limit]

    def list_plans_with_orphaned_structured_card_attempt(self, *, limit: int = 25) -> list[dict]:
        from api.structured_card_lifecycle import STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY

        displayable = {"ready", "publishable_with_flags"}
        rows = []
        for plan in self.plans.values():
            if str(plan.get("status") or "").strip().lower() not in displayable:
                continue
            if plan.get("structured_plan") is not None:
                continue
            report = plan.get("stage2_validator_report")
            if not isinstance(report, dict):
                continue
            if not str(report.get(STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY) or "").strip():
                continue
            rows.append(dict(plan))
        rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
        return rows[:limit]

    def list_admin_athletes(self, *, limit: int = 50, offset: int = 0, q: str | None = None) -> list[dict]:
        rows = []
        for profile in self.profiles.values():
            plans = self.list_user_plans(profile["id"])
            rows.append({
                **profile,
                "plan_count": len(plans),
                "latest_plan_created_at": plans[-1]["created_at"] if plans else None,
                "first_usable_plan_at": _first_usable_plan_at(plans),
            })
        rows.sort(key=lambda row: row["updated_at"], reverse=True)
        rows = _filter_admin_rows(
            rows, q, ("email", "full_name", "username", "professional_status", "record_summary")
        )
        return rows[offset:offset + limit]

    def get_admin_athlete(self, athlete_id: str) -> dict | None:
        self.get_admin_athlete_calls += 1
        profile = self.profiles.get(athlete_id)
        if not profile:
            return None
        plans = self.list_user_plans(athlete_id)
        return {
            **profile,
            "plan_count": len(plans),
            "latest_plan_created_at": plans[-1]["created_at"] if plans else None,
            "first_usable_plan_at": _first_usable_plan_at(plans),
        }

    def list_admin_signup_activation(self, *, since: str, limit: int = 5000) -> list[dict]:
        rows = []
        for profile in self.profiles.values():
            if str(profile.get("created_at") or "") < since:
                continue
            rows.append({
                "id": profile["id"],
                "role": profile.get("role"),
                "created_at": profile.get("created_at"),
                "first_usable_plan_at": _first_usable_plan_at(self.list_user_plans(profile["id"])),
            })
        rows.sort(key=lambda row: str(row["created_at"]), reverse=True)
        return rows[:limit]

    def list_admin_athletes_by_ids(self, athlete_ids: list[str]) -> list[dict]:
        self.list_admin_athletes_by_ids_calls += 1
        return [
            athlete
            for athlete_id in athlete_ids
            if (athlete := self.get_admin_athlete_without_counting(athlete_id)) is not None
        ]

    def get_admin_athlete_without_counting(self, athlete_id: str) -> dict | None:
        profile = self.profiles.get(athlete_id)
        if not profile:
            return None
        plans = self.list_user_plans(athlete_id)
        return {
            **profile,
            "plan_count": len(plans),
            "latest_plan_created_at": plans[-1]["created_at"] if plans else None,
            "first_usable_plan_at": _first_usable_plan_at(plans),
        }

    def count_admin_profiles(self) -> int:
        return sum(1 for profile in self.profiles.values() if profile.get("role") == "admin")

    def list_exercise_media(self) -> list[dict]:
        columns = ("exercise_key", "aliases", "video_id", "start_s", "end_s", "source", "channel_title", "made_for_kids", "orientation")
        return [
            {column: row.get(column) for column in columns}
            for _key, row in sorted(self.exercise_media.items())
            if row.get("status") == "ok" and row.get("made_for_kids") is False
        ]

    def list_exercise_media_for_verification(self) -> list[dict]:
        columns = ("exercise_key", "aliases", "video_id", "status")
        return [{column: row.get(column) for column in columns} for _key, row in sorted(self.exercise_media.items())]

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
        row = self.exercise_media.get(exercise_key)
        if row is None:
            return
        row.update(status=status, status_reason=(reason or None) and str(reason)[:200], verified_at=_now())
        if made_for_kids is not None:
            row["made_for_kids"] = made_for_kids
        if title:
            row["title"] = title[:200]
        if channel_title:
            row["channel_title"] = channel_title[:200]
        if orientation in ("portrait", "landscape"):
            row["orientation"] = orientation

    def clear_onboarding_draft(self, athlete_id: str) -> None:
        self.profiles[athlete_id]["onboarding_draft"] = None

    # ------------------------------------------------------------------
    # Today persistence, injury flags, adaptation notes, admin reviews
    # ------------------------------------------------------------------

    def upsert_today_checkin(self, athlete_id: str, fields: dict) -> dict:
        # Mirrors both keys: plan-scoped rows and the one plan-free row per day.
        # (Older fixtures omit a planned row's phase; a plan-free one never has it.)
        if fields.get("plan_id") is None and fields.get("phase") is not None:
            raise ValueError("today_checkins_phase_matches_plan")
        bucket = self.today_checkins.setdefault(athlete_id, [])
        for row in bucket:
            if row.get("plan_id") == fields.get("plan_id") and row["training_day"] == fields["training_day"]:
                row.update(fields)
                row["updated_at"] = _now()
                return dict(row)
        row = {
            "id": str(uuid4()),
            "athlete_id": athlete_id,
            "athlete_timezone": "",
            "active_injury": "none",
            "previous_session": "none",
            "recommendation_reason": "",
            "recommendation_triggers": [],
            **fields,
            "created_at": _now(),
            "updated_at": _now(),
        }
        bucket.append(row)
        return dict(row)

    def get_today_checkin(self, athlete_id: str, plan_id: str | None, training_day: str) -> dict | None:
        for row in self.today_checkins.get(athlete_id, []):
            if row.get("plan_id") == plan_id and row["training_day"] == training_day:
                return dict(row)
        return None

    def list_today_checkins_for_day(self, athlete_id: str, training_day: str) -> list[dict]:
        return [
            dict(row)
            for row in self.today_checkins.get(athlete_id, [])
            if row["training_day"] == training_day
        ]

    def upsert_session_completion(self, athlete_id: str, fields: dict) -> dict:
        bucket = self.session_completions.setdefault(athlete_id, [])
        for row in bucket:
            if row["session_id"] == fields["session_id"] and row["training_day"] == fields["training_day"]:
                if fields.get("completion_origin") == "day_credit" and (
                    row.get("completion_origin") != "day_credit" or row.get("plan_id") != fields["plan_id"]
                ):
                    return dict(row)
                frozen = row.get("prescription_snapshot")
                if frozen and fields.get("prescription_snapshot") not in (None, frozen):
                    raise HTTPException(409, "prescription_revision_conflict")
                row.update(fields)
                if frozen:
                    row["prescription_snapshot"] = frozen
                row["updated_at"] = _now()
                return dict(row)
        row = {
            "id": str(uuid4()),
            "athlete_id": athlete_id,
            "session_rpe": None,
            "pain_after": None,
            "modification_reason": "",
            "notes": "",
            "started_at": None,
            "completed_at": None,
            **fields,
            "created_at": _now(),
            "updated_at": _now(),
        }
        bucket.append(row)
        return dict(row)

    def get_session_completion_by_id(self, athlete_id: str, completion_id: str) -> dict | None:
        return next((row.copy() for row in self.session_completions.get(athlete_id, [])
                     if row.get("athlete_id") == athlete_id and row.get("id") == completion_id), None)

    def list_session_detail_exercise_logs(self, athlete_id: str, *, plan_id: str,
                                         training_day: str, session_ids: list[str]) -> list[dict]:
        return [row for row in self.list_exercise_logs_for_day(athlete_id, plan_id=plan_id, training_day=training_day)
                if row.get("session_id") in session_ids]

    def get_external_session_sparring_log(self, athlete_id: str, entry_id: str) -> dict | None:
        return next((row for row in self.list_sparring_logs(athlete_id)
                     if row.get("external_session_id") == entry_id), None)

    def get_session_completion(self, athlete_id: str, session_id: str, training_day: str) -> dict | None:
        for row in self.session_completions.get(athlete_id, []):
            if row["session_id"] == session_id and row["training_day"] == training_day:
                return dict(row)
        return None

    def initialize_session_completion_rehab_contexts(
        self,
        athlete_id: str,
        *,
        completion_id: str,
        plan_id: str,
        session_id: str,
        training_day: str,
        contexts: list[dict],
    ) -> dict | None:
        for row in self.session_completions.get(athlete_id, []):
            if (
                row["id"] == completion_id
                and row.get("plan_id") == plan_id
                and row["session_id"] == session_id
                and row["training_day"] == training_day
            ):
                if row.get("rehab_response_contexts") is None:
                    row["rehab_response_contexts"] = copy.deepcopy(contexts)
                    row["updated_at"] = _now()
                return copy.deepcopy(row)
        return None

    def community_rpc(self, name: str, params: dict):
        raise HTTPException(503, 'Community storage is unavailable.')

    def private_media_worker_healthy(self) -> bool:
        return False

    def claim_private_media_budget(self, athlete_id: str) -> None:
        if sum(row['athlete_id'] == athlete_id for row in self.private_media.values()) >= 200:
            raise HTTPException(429, 'Video limit reached. Try again later.')

    def reserve_private_video(self, athlete_id: str, size_bytes: int, content_type: str) -> dict:
        # The in-memory store has no isolated processor or storage permissions.
        raise HTTPException(503, 'Video uploads are unavailable.')

    def create_private_media(self, athlete_id: str, fields: dict) -> dict:
        row = {**copy.deepcopy(fields), 'id': str(uuid4()), 'athlete_id': athlete_id, 'created_at': _now()}
        self.private_media[row['id']] = row
        return copy.deepcopy(row)

    def get_private_media(self, athlete_id: str, media_id: str) -> dict | None:
        row = self.private_media.get(media_id)
        return copy.deepcopy(row) if row and row['athlete_id'] == athlete_id else None

    def get_private_media_for_asset(self, athlete_id: str, asset_id: str) -> dict | None:
        return next((copy.deepcopy(row) for row in self.private_media.values()
                     if row['athlete_id'] == athlete_id and row.get('asset_id') == asset_id), None)

    def get_private_video_asset(self, athlete_id: str, asset_id: str) -> dict | None:
        row = self.private_video_assets.get(asset_id)
        return copy.deepcopy(row) if row and row['athlete_id'] == athlete_id else None

    def private_video_signed_url(self, path: str, *, expires_in: int) -> str:
        raise HTTPException(503, 'Private video storage is unavailable.')

    def create_custom_plan(self, athlete_id: str, fields: dict) -> dict:
        row = {**copy.deepcopy(fields), "athlete_id": athlete_id, "id": str(uuid4()),
               "kind": "self_directed", "created_at": _now(), "updated_at": _now()}
        self.custom_plans[row["id"]] = row
        return copy.deepcopy(row)

    def list_custom_plans(self, athlete_id: str) -> list[dict]:
        return [copy.deepcopy(row) for row in self.custom_plans.values() if row["athlete_id"] == athlete_id]

    def get_custom_plan(self, athlete_id: str, plan_id: str) -> dict | None:
        row = self.custom_plans.get(plan_id)
        return copy.deepcopy(row) if row and row["athlete_id"] == athlete_id else None

    def update_custom_plan(self, athlete_id: str, plan_id: str, fields: dict) -> dict | None:
        if not self.get_custom_plan(athlete_id, plan_id):
            return None
        self.custom_plans[plan_id].update(copy.deepcopy(fields), updated_at=_now())
        return self.get_custom_plan(athlete_id, plan_id)

    def delete_custom_plan(self, athlete_id: str, plan_id: str) -> bool:
        if not self.get_custom_plan(athlete_id, plan_id):
            return False
        del self.custom_plans[plan_id]
        return True

    def create_external_training_session(self, athlete_id: str, fields: dict) -> dict:
        existing = next((row for row in self.external_training_sessions.values()
                         if row["athlete_id"] == athlete_id and row["client_request_id"] == fields["client_request_id"]), None)
        if existing:
            return dict(existing)
        row = {**fields, "id": str(uuid4()), "athlete_id": athlete_id,
               "created_at": _now(), "updated_at": _now(), "deleted_at": None}
        self.external_training_sessions[row["id"]] = row
        return dict(row)

    def list_external_training_sessions(self, athlete_id: str, *, limit: int = 200, include_deleted: bool = False,
                                        before_day: str | None = None) -> list[dict]:
        return sorted((dict(row) for row in self.external_training_sessions.values()
                       if row["athlete_id"] == athlete_id and (include_deleted or not row.get("deleted_at"))
                       and (not before_day or row["training_day"] < before_day)),
                      key=lambda row: (row["training_day"], row["created_at"]), reverse=True)[:limit]

    def get_external_training_session(self, athlete_id: str, entry_id: str) -> dict | None:
        row = self.external_training_sessions.get(entry_id)
        if not row or row["athlete_id"] != athlete_id or row.get("deleted_at"):
            return None
        return dict(row)

    def update_external_training_session(self, athlete_id: str, entry_id: str, fields: dict) -> dict | None:
        row = self.external_training_sessions.get(entry_id)
        if not row or row["athlete_id"] != athlete_id or row.get("deleted_at"):
            return None
        row.update(fields)
        row["updated_at"] = _now()
        return dict(row)

    def set_external_training_takeaway(self, athlete_id: str, entry_id: str, takeaway: str) -> dict | None:
        return self.update_external_training_session(athlete_id, entry_id, {"takeaway": takeaway})

    def delete_external_training_session(self, athlete_id: str, entry_id: str) -> bool:
        row = self.external_training_sessions.get(entry_id)
        if not row or row["athlete_id"] != athlete_id:
            return False
        row["deleted_at"] = row.get("deleted_at") or _now()
        self.sync_manual_exercise_parent(entry_id)
        return True

    def sync_manual_exercise_parent(self, entry_id: str) -> None:
        """The sync_manual_exercise_entries trigger: day and tombstone follow the parent."""
        parent = self.external_training_sessions[entry_id]
        for row in self.manual_exercise_entries:
            if row["external_session_id"] == entry_id and row["athlete_id"] == parent["athlete_id"]:
                row["training_day"] = parent["training_day"]
                if parent.get("deleted_at"):
                    row["deleted_at"] = row.get("deleted_at") or parent["deleted_at"]

    def get_personal_exercise(self, athlete_id: str, personal_exercise_id: str) -> dict | None:
        row = self.personal_exercises.get(personal_exercise_id)
        return dict(row) if row and row["athlete_id"] == athlete_id else None

    def ensure_personal_exercise(self, athlete_id: str, name: str, name_key: str) -> dict:
        existing = next((row for row in self.personal_exercises.values()
                         if row["athlete_id"] == athlete_id and row["name_key"] == name_key), None)
        if existing:
            return dict(existing)
        row = {"id": str(uuid4()), "athlete_id": athlete_id, "name": name, "name_key": name_key,
               "created_at": _now(), "updated_at": _now()}
        self.personal_exercises[row["id"]] = row
        return dict(row)

    def get_manual_exercise(self, athlete_id: str, entry_id: str) -> dict | None:
        row = next((row for row in self.manual_exercise_entries
                    if row["athlete_id"] == athlete_id and row["client_entry_id"] == entry_id), None)
        return dict(row) if row else None

    def create_manual_exercise(self, athlete_id: str, fields: dict) -> dict:
        existing = self.get_manual_exercise(athlete_id, fields["client_entry_id"])
        if existing:
            return existing
        parent = self.external_training_sessions.get(fields["external_session_id"])
        if not parent or parent["athlete_id"] != athlete_id or parent.get("deleted_at"):
            raise HTTPException(status_code=500, detail="manual training session not found")
        live = [row for row in self.manual_exercise_entries if row["athlete_id"] == athlete_id
                and row["external_session_id"] == fields["external_session_id"] and not row.get("deleted_at")]
        if len(live) >= 30:
            raise HTTPException(status_code=500, detail="manual exercise limit reached")
        row = {"exercise_key": None, "personal_exercise_id": None, "actual": {}, "notes": "", **fields,
               "id": str(uuid4()), "athlete_id": athlete_id, "training_day": parent["training_day"],
               "revision": 1, "created_at": _now(), "updated_at": _now(), "deleted_at": None}
        self.manual_exercise_entries.append(row)
        return dict(row)

    def update_manual_exercise(self, athlete_id: str, entry_id: str, fields: dict, *, revision: int) -> dict | None:
        row = next((row for row in self.manual_exercise_entries if row["athlete_id"] == athlete_id
                    and row["client_entry_id"] == entry_id and not row.get("deleted_at")
                    and row.get("revision", 1) == revision), None)
        if not row:
            return None
        row.update({key: fields.get(key) for key in (
            "exercise_key", "personal_exercise_id", "display_name", "measurement_format", "actual", "notes")})
        row["revision"] = revision + 1
        row["updated_at"] = _now()
        return dict(row)

    def delete_manual_exercise(self, athlete_id: str, entry_id: str) -> bool:
        row = next((row for row in self.manual_exercise_entries
                    if row["athlete_id"] == athlete_id and row["client_entry_id"] == entry_id), None)
        if not row:
            return False
        row["deleted_at"] = row.get("deleted_at") or _now()
        return True

    def list_manual_exercises(self, athlete_id: str, external_session_id: str) -> list[dict]:
        rows = [dict(row) for row in self.manual_exercise_entries if row["athlete_id"] == athlete_id
                and row["external_session_id"] == external_session_id and not row.get("deleted_at")]
        return sorted(rows, key=lambda row: (row["created_at"], row["id"]))

    def count_manual_exercises(self, athlete_id: str, external_session_ids: list[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.manual_exercise_entries:
            if row["athlete_id"] == athlete_id and row["external_session_id"] in external_session_ids and not row.get("deleted_at"):
                counts[row["external_session_id"]] = counts.get(row["external_session_id"], 0) + 1
        return counts

    def has_exercise_record(self, athlete_id: str, exercise_key: str) -> bool:
        return any(row["athlete_id"] == athlete_id and row.get("exercise_key") == exercise_key for row in self.exercise_logs) or any(
            row["athlete_id"] == athlete_id and row.get("exercise_key") == exercise_key and not row.get("deleted_at")
            for row in self.manual_exercise_entries)

    def list_manual_exercise_index_page(self, athlete_id: str, *, as_of: str, after_id: str | None,
                                        limit: int = 500) -> list[dict]:
        rows = [dict(row) for row in self.manual_exercise_entries if row["athlete_id"] == athlete_id
                and not row.get("deleted_at") and row.get("created_at", "") <= as_of
                and (after_id is None or row["id"] > after_id)]
        return sorted(rows, key=lambda row: row["id"])[:limit]

    def list_manual_exercise_performances(self, athlete_id: str, *, exercise_key: str | None,
                                          personal_exercise_id: str | None, as_of: str, before_day: str | None,
                                          before_id: str | None, limit: int = 31) -> list[dict]:
        rows = [dict(row) for row in self.manual_exercise_entries if row["athlete_id"] == athlete_id
                and not row.get("deleted_at") and row.get("created_at", "") <= as_of
                and (row.get("exercise_key") == exercise_key if exercise_key is not None
                     else row.get("personal_exercise_id") == personal_exercise_id)
                and (before_day is None or (row["training_day"], row["id"]) < (before_day, before_id))]
        return sorted(rows, key=lambda row: (row["training_day"], row["id"]), reverse=True)[:limit]

    def list_session_completions(self, athlete_id: str, *, limit: int = 30, before_day: str | None = None) -> list[dict]:
        rows = sorted(
            (row for row in self.session_completions.get(athlete_id, [])
             if not before_day or row["training_day"] < before_day),
            key=lambda row: row["training_day"],
            reverse=True,
        )
        return [dict(row) for row in rows[:limit]]

    def list_session_completions_from_day(
        self, athlete_id: str, training_day: str, *, limit: int = 200
    ) -> list[dict]:
        rows = sorted(
            (
                row
                for row in self.session_completions.get(athlete_id, [])
                if str(row["training_day"]) >= training_day
            ),
            key=lambda row: row["training_day"],
        )
        return [dict(row) for row in rows[:limit]]
    def list_rehab_schedule_completions(self, athlete_id: str, *, from_day: str) -> list[dict]:
        return [dict(r) for r in self.session_completions.get(athlete_id, []) if r["training_day"] >= from_day]

    def get_rehab_schedule_revision(self, athlete_id: str) -> dict:
        revision = {}
        for key, source in (("exposure_id", self.rehab_exposures), ("event_id", self.injury_episode_events)):
            rows = [r for r in source.values() if r.get("athlete_id") == athlete_id
                    and (key != "event_id" or r.get("event_type") in {"injury_checkin", "delayed_rehab_response", "clinician_clearance_report"})]
            latest = max(rows, key=lambda r: (r.get("created_at", ""), r["id"]), default=None)
            revision[key] = latest["id"] if latest else None
        return revision

    def list_plan_session_completions(
        self, athlete_id: str, plan_id: str, *, limit: int = 500
    ) -> list[dict]:
        rows = sorted(
            (
                row
                for row in self.session_completions.get(athlete_id, [])
                if row.get("plan_id") == plan_id
            ),
            key=lambda row: row["training_day"],
            reverse=True,
        )
        return [dict(row) for row in rows[:limit]]

    def list_session_logs(self, athlete_id: str, *, limit: int = 500) -> list[dict]:
        rows = sorted(
            self.session_logs.get(athlete_id, []),
            key=lambda row: str(row.get("session_date") or ""),
            reverse=True,
        )
        return [dict(row) for row in rows[:limit]]

    # --- Streaks: mirror api/store.py and the record_athlete_daily_activity RPC ---

    def get_athlete_streaks(self, athlete_id: str) -> dict | None:
        row = self.athlete_streak_rows.get(athlete_id)
        return dict(row) if row is not None else None

    def upsert_athlete_streaks(self, athlete_id: str, fields: dict) -> dict:
        row = {**self.athlete_streak_rows.get(athlete_id, {}), **fields, "athlete_id": athlete_id}
        self.athlete_streak_rows[athlete_id] = row
        return dict(row)

    def record_daily_activity(self, athlete_id: str, activity_date: str) -> dict:
        # Like the RPC: record the day, then rebuild the login run ending on it.
        days = self.daily_activity.setdefault(athlete_id, set())
        days.add(activity_date)
        cursor = date.fromisoformat(activity_date)
        current = 0
        while cursor.isoformat() in days:
            current += 1
            cursor -= timedelta(days=1)
        prior = self.athlete_streak_rows.get(athlete_id, {})
        return self.upsert_athlete_streaks(athlete_id, {
            "login_current": current,
            "login_best": max(int(prior.get("login_best") or 0), current),
            "login_last_active_date": max(
                activity_date, str(prior.get("login_last_active_date") or "")
            ),
        })

    def list_daily_activity(self, athlete_id: str) -> list[dict]:
        return [
            {"athlete_id": athlete_id, "activity_date": day}
            for day in sorted(self.daily_activity.get(athlete_id, set()), reverse=True)
        ]

    def list_today_checkins(self, athlete_id: str, *, limit: int = 14) -> list[dict]:
        rows = sorted(
            self.today_checkins.get(athlete_id, []),
            key=lambda row: row["training_day"],
            reverse=True,
        )
        return [dict(row) for row in rows[:limit]]

    # --- XP rollout, progress reads and plan milestones (see api/store.py) ---

    def validate_xp_abuse_hardening(self) -> dict:
        return xp_hardening_ready_payload()

    def reconcile_training_engagement_xp(self, athlete_id: str, *, manual_session_id: str | None = None) -> list:
        # Source validation/concurrency live in PostgreSQL integration tests.
        return []

    def list_unrewarded_contribution_revisions(self, *, after_id: str | None = None, limit: int = 50) -> list[dict]:
        return []

    def reconcile_approved_contribution_xp(self, revision_id: str) -> None:
        return None

    def reconcile_feedback_xp(
        self, athlete_id: str, *, feedback_id: str, target_amount: int
    ) -> dict | None:
        # Eligibility, the daily cap and the 1 -> 3 upgrade live in SQL and are
        # not modelled here; this store awards no feedback XP (tests of those
        # rules use a dedicated store).
        return None

    def get_xp_progress_state(self, athlete_id: str, *, limit: int) -> dict:
        account = self.xp_accounts.get(athlete_id) or {}
        awards = sorted(
            (dict(row) for row in self.xp_awards.get(athlete_id, [])),
            key=lambda row: (str(row.get("awarded_at") or ""), str(row.get("id") or "")),
            reverse=True,
        )
        return {
            "total_xp": account.get("total_xp"),
            "last_daily_login_date": account.get("last_daily_login_date"),
            "recent_awards": [
                {
                    key: row[key]
                    for key in ("id", "action", "amount", "awarded_at", "calendar_date")
                    if row.get(key) is not None
                }
                for row in awards[:limit]
            ],
        }

    def xp_award_exists(
        self,
        athlete_id: str,
        *,
        action: str | None = None,
        idempotency_key: str | None = None,
        calendar_date: str | None = None,
    ) -> bool:
        return any(
            (action is None or str(row.get("action") or "") == action)
            and (idempotency_key is None or str(row.get("idempotency_key") or "") == idempotency_key)
            and (calendar_date is None or str(row.get("calendar_date") or "") == calendar_date)
            for row in self.xp_awards.get(athlete_id, [])
        )

    def list_plan_milestones(self, athlete_id: str, *, limit: int) -> list[dict]:
        rows = sorted(
            (dict(row) for row in self.plan_milestones.get(athlete_id, [])),
            key=lambda row: str(row.get("completed_at") or ""),
            reverse=True,
        )
        return rows[:limit]

    # Milestone recording and week lifecycle are atomic SQL functions; like the
    # feedback reconcile they are not modelled here and record nothing.
    def record_plan_milestone(self, athlete_id: str, **_fields) -> dict | None:
        return None

    def begin_week_lifecycle_reconciliation(
        self, athlete_id: str, *, plan_id: str, week_id: str
    ) -> dict | None:
        return None

    def complete_week_lifecycle_reconciliation(
        self, athlete_id: str, *, plan_id: str, week_id: str
    ) -> dict | None:
        return None

    def award_xp(
        self,
        athlete_id: str,
        *,
        action: XpAction,
        idempotency_key: str,
        calendar_date: str | None = None,
    ) -> dict:
        amount = XP_REWARD_AMOUNTS[action]
        with self._xp_lock:
            account = self.xp_accounts.setdefault(
                athlete_id,
                {
                    "total_xp": 0,
                    "last_daily_login_date": None,
                    "created_at": _now(),
                    "updated_at": _now(),
                },
            )
            previous_total = int(account["total_xp"])
            awards = self.xp_awards.setdefault(athlete_id, [])
            duplicate_key = any(row["idempotency_key"] == idempotency_key for row in awards)
            duplicate_daily = (
                action == "daily_login"
                and account["last_daily_login_date"] is not None
                and calendar_date is not None
                and calendar_date <= account["last_daily_login_date"]
            )
            awarded = not duplicate_key and not duplicate_daily
            award = None
            if awarded:
                award = {
                    "id": str(uuid4()),
                    "athlete_id": athlete_id,
                    "action": action,
                    "amount": amount,
                    "idempotency_key": idempotency_key,
                    # Matches xp_awards_calendar_scope_check: calendar-scoped
                    # actions persist the date, everything else stores null.
                    "calendar_date": (
                        calendar_date
                        if action in XP_CALENDAR_SCOPED_ACTIONS
                        else None
                    ),
                    "awarded_at": _now(),
                }
                awards.append(award)
                account["total_xp"] = previous_total + amount
                if action == "daily_login":
                    account["last_daily_login_date"] = calendar_date
                account["updated_at"] = _now()

            recent = sorted(
                awards,
                key=lambda row: (row["awarded_at"], row["id"]),
                reverse=True,
            )[:20]

            def public_award(row: dict | None) -> dict | None:
                if row is None:
                    return None
                return {
                    key: value
                    for key, value in row.items()
                    if key not in {"athlete_id", "idempotency_key"} and value is not None
                }

            return {
                "state": {
                    "total_xp": account["total_xp"],
                    "last_daily_login_date": account["last_daily_login_date"],
                    "recent_awards": [public_award(row) for row in recent],
                },
                "previous_total_xp": previous_total,
                "awarded": awarded,
                "award": public_award(award),
            }

    def upsert_push_subscription(self, profile_id: str, fields: dict) -> dict:
        endpoint = str(fields.get("endpoint") or "")
        existing = self.push_subscriptions.get(endpoint)
        row = {
            "id": existing["id"] if existing else str(uuid4()),
            "morning_last_sent_day": existing.get("morning_last_sent_day") if existing else None,
            "timezone": "",
            **fields,
            "profile_id": profile_id,
            "created_at": existing["created_at"] if existing else _now(),
            "updated_at": _now(),
        }
        self.push_subscriptions[endpoint] = row
        return dict(row)

    def list_push_subscriptions(self, profile_id: str) -> list[dict]:
        return [
            dict(row)
            for row in self.push_subscriptions.values()
            if row.get("profile_id") == profile_id
        ]

    def delete_push_subscription(self, profile_id: str, endpoint: str) -> None:
        row = self.push_subscriptions.get(endpoint)
        if row and row.get("profile_id") == profile_id:
            del self.push_subscriptions[endpoint]

    def delete_push_subscription_by_endpoint(self, endpoint: str) -> None:
        self.push_subscriptions.pop(endpoint, None)

    def list_all_push_subscriptions(self, *, limit: int = 500, after_id: str | None = None) -> list[dict]:
        rows = sorted(self.push_subscriptions.values(), key=lambda row: row["id"])
        if after_id:
            rows = [row for row in rows if row["id"] > after_id]
        return [dict(row) for row in rows[:limit]]

    def mark_push_subscription_morning_sent(self, subscription_id: str, *, sent_day: str) -> None:
        for row in self.push_subscriptions.values():
            if row.get("id") == subscription_id:
                row["morning_last_sent_day"] = sent_day
                row["updated_at"] = _now()
                return

    def create_injury_flag(self, athlete_id: str, fields: dict) -> dict:
        row = {
            "id": str(uuid4()),
            "athlete_id": athlete_id,
            "plan_id": None,
            "source": "checkin",
            "body_area": "",
            "severity": "moderate",
            "status": "open",
            "resolved_at": None,
            "episode_id": str(uuid4()),
            "body_region": None,
            "side": "unknown",
            **fields,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.injury_flags.setdefault(athlete_id, []).append(row)
        return dict(row)

    def list_injury_flags(
        self, athlete_id: str, *, statuses: tuple = ("open", "monitoring"), limit: int = 20
    ) -> list[dict]:
        rows = [
            dict(row)
            for row in self.injury_flags.get(athlete_id, [])
            if not statuses or row["status"] in statuses
        ]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return rows[:limit]

    def update_injury_flag(self, flag_id: str, fields: dict) -> dict:
        for rows in self.injury_flags.values():
            for row in rows:
                if row["id"] == flag_id:
                    if row.get("status") == "resolved" and fields.get("status") in ("open", "monitoring"):
                        # Mirrors the database's atomic evidence-episode trigger.
                        row["episode_id"] = str(uuid4())
                    row.update(fields)
                    row["updated_at"] = _now()
                    return dict(row)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="injury flag not found")

    def get_injury_flag_for_athlete(self, flag_id: str, athlete_id: str) -> dict | None:
        return next(
            (dict(row) for row in self.injury_flags.get(athlete_id, []) if row["id"] == flag_id),
            None,
        )

    def adopt_or_create_intake_injury_flag(self, params: dict) -> dict | None:
        """In-memory sync_intake_injury_flag.

        Follows the RPC (supabase/migrations/20260804090000_add_intake_injury_source_key.sql,
        20260804093000_preserve_intake_wound_fields.sql and
        20261008001000_intake_injury_identity_across_plans.sql) step by step, with
        one lock standing in for its advisory lock.
        """
        athlete_id = params["athlete_id"]
        plan_id = params["plan_id"]
        source_key = params["source_key"]
        if not str(source_key or "").strip():
            raise ValueError("missing_intake_injury_source_key")
        if not plan_id:
            raise ValueError("missing_intake_injury_plan_id")

        def legacy_duplicate(row: dict) -> bool:
            return (
                str(row.get("plan_id") or "") == plan_id
                and row.get("source") == "intake"
                and row.get("source_key") is None
                and _normalized_injury_area(row.get("body_area"))
                == _normalized_injury_area(params["body_area"])
                and _normalized_injury_description(row.get("description"))
                == _normalized_injury_description(params["description"])
            )

        def resolve_duplicates(rows: list[dict], *, keep_id: str | None) -> None:
            now_iso = _now()
            for row in rows:
                if row["id"] != keep_id:
                    self.update_injury_flag(
                        row["id"],
                        {
                            "source_key": f"{source_key}:legacy-duplicate:{row['id']}",
                            "status": "resolved",
                            "resolved_at": row.get("resolved_at") or now_iso,
                        },
                    )

        identity = str(params.get("intake_identity") or "").strip() or None
        severity = str(params["severity"] or "").strip().lower()

        def description_key(value) -> str:
            return _normalized_injury_description(re.sub(r"\s?\[training_impact:[^\]]*\]", "", str(value or "")))

        with self._intake_injury_lock:
            rows = self.injury_flags.get(athlete_id, [])
            if not any(row.get("source_key") == source_key for row in rows):
                # 20261008001000: carry the same live injury from another plan.
                area = _normalized_injury_area(params["body_area"])
                carried = sorted(
                    (
                        row
                        for row in rows
                        if row.get("source") == "intake"
                        and str(row.get("status") or "").strip().lower() in ("open", "monitoring")
                        and str(row.get("plan_id") or "") != str(plan_id)
                        and (
                            (identity is not None and row.get("intake_identity") == identity)
                            or (
                                row.get("intake_identity") is None
                                and area
                                and _normalized_injury_area(row.get("body_area")) == area
                                and description_key(row.get("description")) == description_key(params["description"])
                            )
                        )
                    ),
                    key=lambda row: (row.get("intake_identity") is None, str(row.get("created_at") or ""), str(row["id"])),
                )
                if carried:
                    row = carried[0]
                    fields = {
                        "plan_id": plan_id,
                        "source_key": source_key,
                        "intake_identity": identity or row.get("intake_identity"),
                        "description": params["description"] if params["description"] is not None else row.get("description"),
                    }
                    if row.get("severity_source") != "surface_system" and severity in ("mild", "moderate", "severe"):
                        fields["severity"] = severity
                    self.update_injury_flag(row["id"], fields)
            flag = next((row for row in rows if row.get("source_key") == source_key), None)
            legacy = [row for row in rows if legacy_duplicate(row)]
            if flag is not None:
                resolve_duplicates(legacy, keep_id=None)
            elif legacy:
                # order by status rank, resolved_at desc nulls last, created_at, id
                ranked = sorted(legacy, key=lambda row: (str(row.get("created_at") or ""), str(row["id"])))
                ranked.sort(
                    key=lambda row: _notification_datetime(row.get("resolved_at")) or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True,
                )
                ranked.sort(key=lambda row: _INJURY_STATUS_RANK.get(str(row.get("status") or "").strip().lower(), 3))
                flag = ranked[0]
                resolve_duplicates(legacy, keep_id=flag["id"])
                self.update_injury_flag(flag["id"], {"source_key": source_key})
            else:
                flag = self.create_injury_flag(
                    athlete_id,
                    {
                        "plan_id": plan_id,
                        "source": "intake",
                        "source_key": source_key,
                        "body_area": params["body_area"] or "",
                        "description": params["description"] or "",
                        "severity": str(params["severity"] or "").strip().lower() or "moderate",
                        "status": str(params["status"] or "").strip().lower() or "open",
                        "resolved_at": params["resolved_at"],
                    },
                )

            # The wound wrapper fills in only what the row lacks.
            wound = {
                key: params[key]
                for key in ("skin_integrity", "bleeding_status", "coverable", "drainage")
                if flag.get(key) is None and params[key] is not None
            }
            signs = params["infection_signs"]
            if not flag.get("infection_signs") and isinstance(signs, list) and signs:
                wound["infection_signs"] = signs
            if flag.get("intake_identity") is None and identity is not None:
                wound["intake_identity"] = identity
            if wound:
                self.update_injury_flag(flag["id"], wound)
            return self.get_injury_flag_for_athlete(flag["id"], athlete_id)

    def create_rehab_exposure(self, athlete_id: str, payload: dict) -> dict:
        exposure_id = payload["exposure_id"]
        existing = self.rehab_exposures.get(exposure_id)
        if existing is not None:
            if existing["athlete_id"] != athlete_id or existing["event_json"] != payload:
                raise HTTPException(status_code=409, detail="exposure id already used")
            return dict(existing)
        row = {"id": exposure_id, "athlete_id": athlete_id, "event_json": payload, "created_at": _now()}
        self.rehab_exposures[exposure_id] = row
        return dict(row)

    def list_rehab_exposures_by_ids(
        self, athlete_id: str, exposure_ids: list[str]
    ) -> list[dict]:
        return [
            copy.deepcopy(row)
            for exposure_id in exposure_ids
            if (row := self.rehab_exposures.get(exposure_id)) is not None
            and row.get("athlete_id") == athlete_id
        ]

    def record_injury_episode_event(self, athlete_id: str, event: dict) -> dict:
        injury = self.get_injury_flag_for_athlete(event["injury_id"], athlete_id)
        if not injury or (event["event_type"] != "delayed_rehab_response" and injury.get("episode_id") != event["injury_episode_id"]):
            raise HTTPException(409, "injury_episode_changed")
        if event["event_type"] == "delayed_rehab_response":
            exposure = self.rehab_exposures.get(event["payload"]["exposure_id"], {})
            raw = exposure.get("event_json") or {}
            if (exposure.get("athlete_id") != athlete_id or raw.get("injury_id") != event["injury_id"]
                    or raw.get("injury_episode_id") != event["injury_episode_id"]):
                raise HTTPException(409, "invalid delayed response")
        if event["id"] in self.injury_episode_events:
            previous = self.injury_episode_events[event["id"]]
            if any(previous.get(key) != event.get(key) for key in ("payload", "event_type", "injury_id", "injury_episode_id")):
                raise HTTPException(409, "episode_event_conflict")
            return copy.deepcopy(previous)
        row = {**copy.deepcopy(event), "athlete_id": athlete_id, "created_at": _now()}
        self.injury_episode_events[event["id"]] = row
        return copy.deepcopy(row)

    def list_injury_episode_events(self, athlete_id: str, *, injury_id: str, injury_episode_id: str) -> list[dict]:
        return [copy.deepcopy(row) for row in self.injury_episode_events.values()
                if row["athlete_id"] == athlete_id and row["injury_id"] == injury_id and row["injury_episode_id"] == injury_episode_id]

    def list_pending_delayed_rehab(self, athlete_id: str, training_day: str) -> list[dict]:
        answered = {row["payload"].get("exposure_id") for row in self.injury_episode_events.values()
                    if row["athlete_id"] == athlete_id and row["event_type"] == "delayed_rehab_response"}
        pending = []
        for row in self.rehab_exposures.values():
            event = row.get("event_json") or {}
            injury = self.get_injury_flag_for_athlete(str(event.get("injury_id")), athlete_id)
            if (row.get("athlete_id") == athlete_id and row["id"] not in answered and injury
                    and str(event.get("occurred_at"))[:10] < (datetime.fromisoformat(training_day) + timedelta(days=1)).date().isoformat()):
                pending.append({**row, **event, "id": row["id"]})
        return pending

    def list_rehab_exposures(
        self,
        athlete_id: str,
        *,
        injury_id: str,
        injury_episode_id: str,
        limit: int = 200,
    ) -> RehabExposureWindow:
        bounded_limit = max(1, min(limit, 500))
        rows = [
            dict(row)
            for row in self.rehab_exposures.values()
            if row.get("athlete_id") == athlete_id
            and str((row.get("event_json") or {}).get("injury_id") or "") == injury_id
            and str((row.get("event_json") or {}).get("injury_episode_id") or "")
            == injury_episode_id
        ]
        rows.sort(
            key=lambda row: (
                str((row.get("event_json") or {}).get("occurred_at") or ""),
                str(row.get("id") or ""),
            ),
            reverse=True,
        )
        history_truncated = len(rows) > bounded_limit
        rows = rows[:bounded_limit]
        rows.sort(
            key=lambda row: (
                str((row.get("event_json") or {}).get("occurred_at") or ""),
                str(row.get("id") or ""),
            )
        )
        return RehabExposureWindow(rows=rows, history_truncated=history_truncated)

    def create_adaptation_note(self, athlete_id: str, fields: dict) -> dict:
        row = {
            "id": str(uuid4()),
            "athlete_id": athlete_id,
            "plan_id": None,
            "checkin_id": None,
            "session_log_id": None,
            "details": {},
            **fields,
            "created_at": _now(),
        }
        self.adaptation_notes.setdefault(athlete_id, []).append(row)
        return dict(row)

    def record_sparring_log(self, athlete_id: str, fields: dict, *, review_reason: str | None) -> dict:
        # Mirrors the single-transaction RPC: nothing is kept unless everything is.
        rocked = bool(fields.get("rocked"))
        if rocked and not (review_reason or "").strip():
            raise ValueError("rocked report requires a review reason")
        if rocked and self.fail_sparring_review_insert:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="review insert failed")
        request_id = fields.get("client_request_id")
        external_id = fields.get("external_session_id")
        revision = int(fields.get("detail_revision") or 0)

        def review_of(log: dict) -> dict | None:
            return next((dict(r) for r in self.admin_reviews if r.get("id") == log.get("admin_review_id")), None)

        # Mirrors sparring_log_requests: every accepted request id is kept.
        if request_id and (athlete_id, request_id) in self.sparring_log_requests:
            replay = next(r for r in self.sparring_logs if r["id"] == self.sparring_log_requests[(athlete_id, request_id)])
            return {"log": dict(replay), "review": review_of(replay), "replayed": True}
        fields = {key: value for key, value in fields.items() if key != "client_request_id"}
        if external_id:
            session = self.get_external_training_session(athlete_id, external_id)
            if not session or session.get("activity") != "sparring":
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="sparring session not found")
            fields["training_day"] = session["training_day"]
            existing = next((r for r in self.sparring_logs if r.get("external_session_id") == external_id), None)
        else:
            existing = None
        was_rocked = bool(existing and existing.get("rocked"))
        if existing:
            if revision > int(existing.get("detail_revision") or 0):
                severity = ("none", "light", "heavy")
                recorded = existing.get("head_contact")
                existing.update({key: fields[key] for key in ("intensity", "rounds_completed", "round_seconds", "head_contact", "notes") if key in fields})
                # Mirrors the RPC: a correction never lowers recorded head contact.
                if recorded in severity and severity.index(existing["head_contact"]) < severity.index(recorded):
                    existing["head_contact"] = recorded
                existing["detail_revision"] = revision
            existing["rocked"] = was_rocked or rocked
            row = existing
        else:
            row = {"id": str(uuid4()), "athlete_id": athlete_id, "created_at": _now(), "admin_review_id": None,
                   "detail_revision": revision, **fields}
            self.sparring_logs.append(row)
        if rocked and not was_rocked:
            review = self.create_admin_review(athlete_id, {"reason": review_reason, "status": "pending"})
            row["admin_review_id"] = review["id"]
        if request_id:
            self.sparring_log_requests[(athlete_id, request_id)] = row["id"]
        return {"log": dict(row), "review": review_of(row), "replayed": False}

    def list_sparring_logs(self, athlete_id: str, *, limit: int = 60, from_day: str | None = None) -> list[dict]:
        rows = [
            dict(row)
            for row in self.sparring_logs
            if row.get("athlete_id") == athlete_id
            and (from_day is None or str(row.get("training_day")) >= from_day)
        ]
        rows.sort(key=lambda row: (str(row.get("training_day")), str(row.get("created_at"))), reverse=True)
        return rows[:limit]

    def latest_sparring_log_day(self, athlete_id: str, *, hard: bool = False, rocked: bool = False) -> str | None:
        days = [
            str(row.get("training_day"))
            for row in self.sparring_logs
            if row.get("athlete_id") == athlete_id
            and (not hard or row.get("intensity") == "hard")
            and (not rocked or row.get("rocked"))
        ]
        return max(days) if days else None

    def upsert_exercise_log(self, athlete_id: str, fields: dict) -> dict:
        # Mirrors the table's occurrence key: one row per plan/day/block.
        key = ("plan_id", "training_day", "block_id")
        for row in self.exercise_logs:
            if row["athlete_id"] == athlete_id and all(row.get(k) == fields.get(k) for k in key):
                row.update({**fields, "updated_at": _now()})
                return dict(row)
        now = _now()
        row = {"id": str(uuid4()), "athlete_id": athlete_id, "created_at": now, "updated_at": now, **fields}
        self.exercise_logs.append(row)
        return dict(row)

    def upsert_exercise_logs(self, athlete_id: str, rows: list[dict], *, keep_existing: bool = False) -> list[dict]:
        # One statement in Postgres: either every row lands or none does. The
        # fake applies the whole batch to a copy and swaps it in only at the end.
        if self.fail_next_exercise_log_batch:
            self.fail_next_exercise_log_batch = False
            raise HTTPException(status_code=500, detail="failed to save exercise logs")
        key = ("plan_id", "training_day", "block_id")
        staged = [dict(row) for row in self.exercise_logs]
        written: list[dict] = []
        for fields in rows:
            match = next(
                (row for row in staged
                 if row["athlete_id"] == athlete_id and all(row.get(k) == fields.get(k) for k in key)),
                None,
            )
            if match is not None:
                if keep_existing:
                    continue  # ON CONFLICT DO NOTHING
                match.update({**fields, "updated_at": _now()})
                written.append(dict(match))
                continue
            now = _now()
            row = {"id": str(uuid4()), "athlete_id": athlete_id, "created_at": now, "updated_at": now, **fields}
            staged.append(row)
            written.append(dict(row))
        self.exercise_logs[:] = staged
        return written

    def list_exercise_logs_for_day(self, athlete_id: str, *, plan_id: str, training_day: str) -> list[dict]:
        return [
            dict(row)
            for row in self.exercise_logs
            if row["athlete_id"] == athlete_id
            and row.get("plan_id") == plan_id
            and str(row.get("training_day")) == training_day
        ]

    def list_exercise_index_page(self, athlete_id: str, *, as_of: str, after_id: str | None,
                                 limit: int = 500) -> list[dict]:
        rows = [dict(row) for row in self.exercise_logs if row["athlete_id"] == athlete_id
                and row.get("status") != "skipped" and row.get("created_at", "") <= as_of
                and (after_id is None or row["id"] > after_id)]
        return sorted(rows, key=lambda row: row["id"])[:limit]

    def list_exercise_performances(self, athlete_id: str, *, exercise_key: str | None, log_id: str | None,
                                   as_of: str, before_day: str | None, before_id: str | None,
                                   limit: int = 31) -> list[dict]:
        rows = [dict(row) for row in self.exercise_logs if row["athlete_id"] == athlete_id
                and row.get("created_at", "") <= as_of
                and (row.get("exercise_key") == exercise_key if exercise_key is not None else row["id"] == log_id)
                and (before_day is None or (row["training_day"], row["id"]) < (before_day, before_id))]
        return sorted(rows, key=lambda row: (row["training_day"], row["id"]), reverse=True)[:limit]

    def list_exercise_history(self, athlete_id: str, *, before_day: str | None = None,
                              limit: int = 50, offset: int = 0) -> list[dict]:
        rows = [dict(row) for row in self.exercise_logs if row["athlete_id"] == athlete_id
                and (before_day is None or row["training_day"] < before_day)]
        rows.sort(key=lambda row: (row["training_day"], row.get("updated_at", ""), row["id"]), reverse=True)
        return rows[offset:offset + limit]

    def list_recent_exercise_loads(self, athlete_id: str, *, before_day: str, limit: int = 200) -> list[dict]:
        rows = [
            dict(row)
            for row in self.exercise_logs
            if row["athlete_id"] == athlete_id
            and str(row.get("training_day")) < before_day
            and isinstance(row.get("actual"), dict)
            and row["actual"].get("load") is not None
        ]
        rows.sort(key=lambda row: str(row.get("training_day")), reverse=True)
        return rows[:limit]

    def create_admin_review(self, athlete_id: str, fields: dict) -> dict:
        row = {
            "id": str(uuid4()),
            "athlete_id": athlete_id,
            "adaptation_note_id": None,
            "injury_flag_id": None,
            "status": "pending",
            "resolution_notes": "",
            "resolved_by": "",
            "resolved_at": None,
            **fields,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.admin_reviews.append(row)
        return dict(row)

    def list_admin_reviews(self, *, status_filter: str | None = "pending", limit: int = 50) -> list[dict]:
        rows = [
            dict(row)
            for row in self.admin_reviews
            if status_filter is None or row["status"] == status_filter
        ]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return rows[:limit]

    def count_pending_admin_reviews_for_athlete(self, athlete_id: str) -> int:
        return sum(
            1
            for row in self.admin_reviews
            if row["athlete_id"] == athlete_id and row["status"] == "pending"
        )

    def resolve_admin_review(self, review_id: str, fields: dict) -> dict:
        for row in self.admin_reviews:
            if row["id"] == review_id:
                row.update(fields)
                row["updated_at"] = _now()
                return dict(row)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="review not found")

    def get_context_feedback(self, profile_id: str, context_key: str) -> dict | None:
        return next(
            (
                dict(row)
                for row in self.beta_feedback
                if row["submitted_by_profile_id"] == profile_id and row["context_key"] == context_key
            ),
            None,
        )

    def get_feedback_plan_for_owner(self, plan_id: str, profile_id: str) -> dict | None:
        return self.get_plan_for_athlete(plan_id, profile_id)

    def get_feedback_active_plan_id(self, profile_id: str) -> str | None:
        return self.get_active_plan_id(profile_id)

    def get_feedback_today_checkin(
        self, profile_id: str, plan_id: str, training_day: str
    ) -> dict | None:
        return self.get_today_checkin(profile_id, plan_id, training_day)

    def list_feedback_injury_flags(self, profile_id: str, *, limit: int = 20) -> list[dict]:
        return self.list_injury_flags(profile_id, limit=limit)

    def get_feedback_intake(self, intake_id: str) -> dict | None:
        return self.get_intake(intake_id)

    def upsert_context_feedback(self, payload: dict) -> dict:
        for row in self.beta_feedback:
            if (
                row["submitted_by_profile_id"] == payload["submitted_by_profile_id"]
                and row["context_key"] == payload["context_key"]
            ):
                row.update(copy.deepcopy(payload))
                row["updated_at"] = _now()
                return dict(row)
        row = {"id": str(uuid4()), **copy.deepcopy(payload), "created_at": _now(), "updated_at": _now()}
        self.beta_feedback.append(row)
        return dict(row)

    def insert_global_feedback(self, payload: dict) -> dict:
        row = {**copy.deepcopy(payload), "created_at": _now(), "updated_at": _now()}
        self.beta_feedback.append(row)
        return dict(row)

    def list_admin_feedback(self, *, limit: int = 50) -> list[dict]:
        rows: list[dict] = []
        for feedback in self.beta_feedback:
            profile = self.profiles.get(str(feedback.get("submitted_by_profile_id") or ""), {})
            rows.append(
                {
                    **copy.deepcopy(feedback),
                    "profiles": {
                        "email": str(profile.get("email") or ""),
                        "full_name": str(profile.get("full_name") or ""),
                    },
                }
            )
        rows.sort(
            key=lambda row: (
                str(row.get("priority") or "normal") == "safety",
                str(row.get("created_at") or ""),
            ),
            reverse=True,
        )
        return rows[: max(1, min(limit, 100))]

    def get_feedback_screenshot_path(self, feedback_id: str) -> str | None:
        row = next((item for item in self.beta_feedback if item.get("id") == feedback_id), None)
        if not row or not row.get("screenshot_path"):
            return None
        expires_at = str(row.get("screenshot_expires_at") or "")
        if expires_at and datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
            return None
        return str(row["screenshot_path"])

    def create_feedback_screenshot_signed_url(self, path: str, *, expires_in: int) -> str:
        return f"https://storage.test/signed/{path}?expires_in={expires_in}"

    def claim_feedback_rate_limit(
        self,
        profile_id: str,
        *,
        report_limit: int,
        screenshot_limit: int,
        has_screenshot: bool,
    ) -> tuple[bool, str | None, int]:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=1)
        with self._generation_job_daily_limit_lock:
            for scope, limit, active in (
                ("global_report", report_limit, True),
                ("screenshot", screenshot_limit, has_screenshot),
            ):
                key = (profile_id, scope)
                events = [event for event in self._feedback_rate_events.get(key, []) if event >= cutoff]
                self._feedback_rate_events[key] = events
                if active and limit > 0 and len(events) >= limit:
                    return False, scope, max(1, math.ceil(3600 - (now - events[0]).total_seconds()))
            if report_limit > 0:
                self._feedback_rate_events.setdefault((profile_id, "global_report"), []).append(now)
            if has_screenshot and screenshot_limit > 0:
                self._feedback_rate_events.setdefault((profile_id, "screenshot"), []).append(now)
        return True, None, 0

    def upload_feedback_screenshot(self, path: str, data: bytes, mime: str) -> None:
        self.feedback_screenshots[path] = (data, mime)

    def delete_feedback_screenshots(self, paths: list[str]) -> None:
        for path in paths:
            self.feedback_screenshots.pop(path, None)

    def list_expired_feedback_screenshots(self, *, limit: int = 100) -> list[dict]:
        now = datetime.now(timezone.utc)
        rows = []
        for row in self.beta_feedback:
            expires = _parse_iso(row.get("screenshot_expires_at"))
            if row.get("screenshot_path") and expires and expires <= now:
                rows.append(dict(row))
        return rows[:limit]

    def list_profile_feedback_screenshots(self, profile_id: str, *, limit: int = 100) -> list[dict]:
        return [
            dict(row)
            for row in self.beta_feedback
            if row.get("submitted_by_profile_id") == profile_id and row.get("screenshot_path")
        ][:limit]

    def clear_feedback_screenshot(self, feedback_id: str, expected_path: str) -> bool:
        for row in self.beta_feedback:
            if row.get("id") == feedback_id and row.get("screenshot_path") == expected_path:
                for field_name in (
                    "screenshot_path",
                    "screenshot_mime",
                    "screenshot_size_bytes",
                    "screenshot_width",
                    "screenshot_height",
                    "screenshot_expires_at",
                ):
                    row[field_name] = None
                row["screenshot_deleted_at"] = _now()
                return True
        return False


class FakeOpenAIStream:
    """Minimal stand-in for the OpenAI SDK's AsyncResponseStreamManager: entering
    the context resolves (or raises) like ``responses.create`` would, and
    ``get_final_response`` returns the accumulated response object."""

    def __init__(self, response_coro) -> None:
        self._coro = response_coro
        self._response: object | None = None

    async def __aenter__(self) -> "FakeOpenAIStream":
        self._response = await self._coro
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def get_final_response(self) -> object:
        return self._response


class FakeOpenAIResponses:
    def __init__(self, outputs: list[object]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    async def create(self, **request: object) -> object:
        self.calls.append(request)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output

    def stream(self, **request: object) -> FakeOpenAIStream:
        # Delegate through create() so tests that override .create keep working.
        return FakeOpenAIStream(self.create(**request))


class FakeOpenAIClient:
    def __init__(self, outputs: list[object]) -> None:
        self.responses = FakeOpenAIResponses(outputs)


@dataclass
class FakeStage2Automator:
    result: dict | None = None
    error: Exception | None = None
    calls: list[dict] = field(default_factory=list)
    # Tests pass a zero-arg callable when each invocation should produce a fresh
    # finalized payload (e.g. distinct ids per call). When set, it takes
    # precedence over the static ``result``.
    result_factory: Callable[[], dict] | None = None

    async def finalize(self, *, stage1_result: dict, log_context: dict | None = None) -> dict:
        self.calls.append(stage1_result)
        if self.error:
            raise self.error
        overlay = self.result_factory() if self.result_factory is not None else (self.result or {})
        return {**stage1_result, **overlay}


def _build_request(overrides: dict | None = None) -> PlanRequest:
    payload = {
        "athlete": {
            "full_name": "Ari Mensah",
            # Derived from the default athlete's date of birth rather than
            # hard-coded, because the generation route overwrites `age` with
            # exactly this value. A fixed number would leave the default payload
            # differing from the one the route stores, and any test comparing a
            # hand-seeded job against a posted one would fail on the hash alone.
            "age": age_years(DEFAULT_ADULT_DATE_OF_BIRTH),
            "weight_kg": 72.5,
            "target_weight_kg": 70.0,
            "height_cm": 178,
            "technical_style": ["boxing"],
            "tactical_style": ["pressure_fighter"],
            "professional_status": "amateur",
            "record": "5-1",
            "athlete_timezone": "Europe/London",
        },
        "fight_date": "2099-04-18",
        "weekly_training_frequency": 4,
        "training_availability": ["Monday", "Tuesday", "Thursday", "Saturday"],
        "hard_sparring_days": ["Tuesday", "Saturday"],
        "technical_skill_days": ["Monday"],
        "equipment_access": ["barbell", "heavy_bag"],
        "key_goals": ["power", "conditioning"],
        "weak_areas": ["gas_tank"],
        "injuries": "mild left shoulder irritation",
        "rounds_format": "3 x 3",
        "fatigue_level": "moderate",
    }
    if overrides:
        merged = copy.deepcopy(overrides)
        athlete_overrides = merged.pop("athlete", None)
        if athlete_overrides:
            payload["athlete"].update(athlete_overrides)
        payload.update(merged)
        availability_override = merged.get("training_availability")
        if isinstance(availability_override, list):
            normalized_days = {
                str(day).strip().lower()
                for day in availability_override
                if str(day).strip()
            }
            for key in ["hard_sparring_days", "technical_skill_days", "support_work_days"]:
                if key not in merged and key in payload:
                    payload[key] = [
                        day
                        for day in payload[key]
                        if str(day).strip().lower() in normalized_days
                    ]
    return PlanRequest.model_validate(payload)


def stage1_result() -> dict:
    return {
        "plan_text": "# Stage 1 Draft",
        "coach_notes": "### Coach Review",
        "pdf_url": "https://example.com/stage1.pdf",
        "why_log": {"strength": {}},
        "stage2_payload": {"ok": True},
        "planning_brief": {"schema_version": "planning_brief.v1", "main_limiter": "conditioning"},
        "stage2_handoff_text": "handoff",
    }


def finalized_result(**overrides: object) -> dict:
    base = {
        **stage1_result(),
        "status": "ready",
        "plan_text": "# Final Plan",
        "draft_plan_text": "# Stage 1 Draft",
        "final_plan_text": "# Final Plan",
        "pdf_url": None,
        "stage2_status": "stage2_pass",
        "stage2_validator_report": {"errors": [], "warnings": []},
        "stage2_retry_text": "",
        "stage2_attempt_count": 1,
    }
    return {**base, **overrides}


def advisory_planning_brief(
    *,
    phase: str = "TAPER",
    stage_key: str = "fight_week_survival_rhythm",
    days_until_fight: int = 6,
    fatigue: str = "low",
    readiness_flags: list[str] | None = None,
    injuries: list[str] | None = None,
    weight_cut_pct: float = 0.0,
    hard_sparring_days: list[str] | None = None,
) -> dict:
    hard_days = hard_sparring_days or ["Tuesday", "Thursday"]
    return {
        "schema_version": "planning_brief.v1",
        "athlete_snapshot": {
            "sport": "boxing",
            "days_until_fight": days_until_fight,
            "fatigue": fatigue,
            "short_notice": days_until_fight <= 14,
            "readiness_flags": readiness_flags or [],
            "injuries": injuries or [],
            "weight_cut_pct": weight_cut_pct,
            "hard_sparring_days": hard_days,
            "technical_skill_days": ["Monday"],
        },
        "weekly_role_map": {
            "weeks": [
                {
                    "phase": phase,
                    "week_index": 1,
                    "phase_week_index": 1,
                    "phase_week_total": 1,
                    "stage_key": stage_key,
                    "declared_hard_sparring_days": hard_days,
                    "declared_technical_skill_days": ["Monday"],
                    "declared_training_days": ["Monday", "Tuesday", "Thursday", "Saturday"],
                    "session_roles": [],
                    "suppressed_roles": [],
                }
            ]
        },
    }


def _presentation_ready_plan(*, heading: str, support_note: str) -> str:
    return (
        f"## {heading}\n"
        "### Week 1\n"
        "#### Strength\n"
        "- Trap Bar Deadlift - 4x3\n"
        "#### Recovery\n"
        f"- {support_note}\n"
        "#### Fight-pace conditioning\n"
        "- Bag Rounds - 5 x 3 min\n"
    )


def _review_required_result(*, final_plan_text: str, warning_code: str) -> dict:
    warning = {"code": warning_code, "severity": "blocker"}
    return finalized_result(
        status="review_required",
        plan_text="",
        final_plan_text=final_plan_text,
        stage2_status="stage2_failed",
        stage2_retry_text="repair prompt",
        stage2_validator_report={"errors": [], "warnings": [warning], "blocking_warnings": [warning]},
        stage2_attempt_count=2,
    )


@dataclass(frozen=True)
class SystemScenario:
    key: str
    request_overrides: dict
    automator_result: dict
    expected_status: str
    expected_review_code: str | None
    expected_resolution: str | None
    support_marker: str


SYSTEM_SCENARIOS = [
    SystemScenario(
        key="high_fatigue",
        request_overrides={
            "fatigue_level": "high",
            "weekly_training_frequency": 5,
            "training_availability": ["Monday", "Tuesday", "Wednesday", "Thursday", "Saturday"],
        },
        automator_result=finalized_result(
            plan_text=_presentation_ready_plan(
                heading="PHASE 2: SPP",
                support_note="High fatigue this week, so keep the recovery day obvious and trim optional density first.",
            ),
            final_plan_text=_presentation_ready_plan(
                heading="PHASE 2: SPP",
                support_note="High fatigue this week, so keep the recovery day obvious and trim optional density first.",
            ),
        ),
        expected_status="ready",
        expected_review_code=None,
        expected_resolution=None,
        support_marker="High fatigue this week",
    ),
    SystemScenario(
        key="messy_injury_input",
        request_overrides={
            "injuries": "none / right shoulder cranky after pads + left wrist sore on hooks",
            "equipment_access": ["bands", "heavy_bag", "bodyweight"],
        },
        automator_result=finalized_result(
            plan_text=_presentation_ready_plan(
                heading="PHASE 1: GPP",
                support_note="Shoulder and wrist management stay in the week, but the main session remains decisive.",
            ),
            final_plan_text=_presentation_ready_plan(
                heading="PHASE 1: GPP",
                support_note="Shoulder and wrist management stay in the week, but the main session remains decisive.",
            ),
        ),
        expected_status="ready",
        expected_review_code=None,
        expected_resolution=None,
        support_marker="Shoulder and wrist management",
    ),
    SystemScenario(
        key="severe_cut_pressure",
        request_overrides={
            "athlete": {"weight_kg": 72.0, "target_weight_kg": 66.0},
            "fatigue_level": "moderate",
            "fight_date": "2099-04-05",
        },
        automator_result=finalized_result(
            plan_text=(
                "## Camp Summary\n"
                "- Active weight-cut stress is part of this camp, so protect freshness and avoid optional fatigue.\n"
                "## Nutrition\n"
                "- Prioritize carbs, fluids, and sodium around key sessions to preserve strength expression and conditioning tolerance.\n"
            ),
            final_plan_text=(
                "## Camp Summary\n"
                "- Active weight-cut stress is part of this camp, so protect freshness and avoid optional fatigue.\n"
                "## Nutrition\n"
                "- Prioritize carbs, fluids, and sodium around key sessions to preserve strength expression and conditioning tolerance.\n"
            ),
        ),
        expected_status="ready",
        expected_review_code=None,
        expected_resolution=None,
        support_marker="Active weight-cut stress",
    ),
    SystemScenario(
        key="limited_equipment_hold",
        request_overrides={
            "equipment_access": ["bands", "bodyweight"],
            "weekly_training_frequency": 3,
            "training_availability": ["Tuesday", "Thursday", "Saturday"],
        },
        automator_result=_review_required_result(
            final_plan_text="## PHASE 2: SPP\n- Heavy Bag Sprint Rounds - 6 x 15 sec",
            warning_code="equipment_incongruent_selection",
        ),
        expected_status="review_required",
        expected_review_code="equipment_incongruent_selection",
        expected_resolution="approve",
        support_marker="Heavy Bag Sprint Rounds",
    ),
    SystemScenario(
        key="short_notice_contradictory",
        request_overrides={
            "fight_date": "2099-03-24",
            "weekly_training_frequency": 2,
            "training_availability": ["Monday", "Wednesday"],
            "equipment_access": ["assault_bike", "bands", "bodyweight"],
        },
        automator_result=_review_required_result(
            final_plan_text="## PHASE 3: TAPER\n### Week 5\n#### Strength\n- Dead Bug - 2x8",
            warning_code="late_camp_session_incomplete",
        ),
        expected_status="review_required",
        expected_review_code="late_camp_session_incomplete",
        expected_resolution="manual_stage2",
        support_marker="Dead Bug - 2x8",
    ),
]


def _planner(payload: dict) -> dict:
    return stage1_result()


def _empty_plan_planner(payload: dict, *, progress_callback=None) -> dict:
    """Module-level planner that returns an empty plan result.

    Tests that pipe a planner through the generation subprocess need a
    picklable callable; in-test ``lambda`` planners raise AttributeError when
    the subprocess tries to pickle them.
    """
    return {"plan_text": ""}


def _start_generation(client: TestClient, request: PlanRequest | None = None) -> tuple[dict, dict]:
    response = client.post(
        "/api/plans/generate",
        headers={"Authorization": "Bearer athlete-token"},
        json=(request or _build_request()).model_dump(mode="json"),
    )
    assert response.status_code == 202
    job_body = response.json()
    job_response = client.get(
        f"/api/generation-jobs/{job_body['job_id']}",
        headers={"Authorization": "Bearer athlete-token"},
    )
    assert job_response.status_code == 200
    return job_body, job_response.json()


DEFAULT_ATHLETE_USER = AuthenticatedUser(
    user_id="athlete-1",
    email="ari@example.com",
    full_name="Ari Mensah",
    metadata={},
)
DEFAULT_ADMIN_USER = AuthenticatedUser(
    user_id="admin-1",
    email="ops@unlxck.test",
    full_name="Ops Admin",
    metadata={},
)


# The default test athlete is an onboarded adult: old enough for the standard
# flow, Terms accepted, health-data consent given. That is the state a real
# athlete reaches at the end of signup, so it is the right baseline for every
# test that is about something else. Tests that exercise the compliance gates
# clear these explicitly (see ``clear_compliance`` / ``withdraw_health_consent``).
DEFAULT_ADULT_DATE_OF_BIRTH = "1996-05-04"


def grant_default_compliance(
    store: "FakeStore",
    athlete_id: str = DEFAULT_ATHLETE_USER.user_id,
    *,
    date_of_birth: str = DEFAULT_ADULT_DATE_OF_BIRTH,
) -> dict:
    """Put a profile in the fully consented state signup produces."""
    return store.record_compliance_acceptance(
        athlete_id,
        date_of_birth=date_of_birth,
        accept_terms=True,
        health_data_consent=True,
    )


def clear_compliance(store: "FakeStore", athlete_id: str = DEFAULT_ATHLETE_USER.user_id) -> dict:
    """Reset a profile to the pre-consent state a brand-new account is in."""
    profile = store.profiles[athlete_id]
    profile.update(
        {
            "date_of_birth": None,
            "terms_version": None,
            "terms_accepted_at": None,
            "health_consent_version": None,
            "health_data_consent": False,
            "health_consent_at": None,
            "health_consent_withdrawn_at": None,
        }
    )
    return profile


def withdraw_health_consent(
    store: "FakeStore", athlete_id: str = DEFAULT_ATHLETE_USER.user_id
) -> dict:
    return store.record_compliance_acceptance(athlete_id, health_data_consent=False)


def seed_default_profiles(store: "FakeStore") -> None:
    """Ensure the default athlete and admin profiles exist in ``store``.

    Mirrors production where ``require_profile`` calls ``ensure_profile`` on the
    first authenticated request. Tests that build a ``FakeStore`` directly and
    seed intakes/plans without going through ``_build_client`` should call this
    to satisfy the profile-row prerequisite of admin/plan endpoints.

    The athlete is left fully consented, matching an account that finished
    signup — otherwise every health-dependent endpoint in the suite would 403 on
    a gate that is not what the test is checking.
    """
    store.ensure_profile(DEFAULT_ATHLETE_USER)
    store.ensure_profile(DEFAULT_ADMIN_USER)
    grant_default_compliance(store)


def _build_client(
    automator: FakeStage2Automator | None = None,
    *,
    enable_in_process_generation: bool = True,
) -> tuple[TestClient, FakeStore, FakeStage2Automator]:
    athlete = AuthenticatedUser(
        user_id="athlete-1",
        email="ari@example.com",
        full_name="Ari Mensah",
        metadata={},
    )
    admin = AuthenticatedUser(
        user_id="admin-1",
        email="ops@unlxck.test",
        full_name="Ops Admin",
        metadata={},
    )
    store = FakeStore()
    # Mirror production: every authenticated request ensures the caller's profile
    # via require_profile -> ensure_profile. Seed both up front so tests that
    # don't make a request first (e.g. seeding store state directly) still see
    # the profiles their endpoints expect.
    seed_default_profiles(store)
    stage2 = automator or FakeStage2Automator(result=finalized_result())
    client = TestClient(
        create_app(
            store=store,
            auth_service=FakeAuthService({"athlete-token": athlete, "admin-token": admin}),
            planner=_planner,
            stage2_automator=stage2,
            enable_in_process_generation=enable_in_process_generation,
        )
    )
    return client, store, stage2
