"""The persistence interface, one Protocol per area.

``AppStore`` is the union the app, worker and routes depend on. A service that
touches one area can depend on that area's Protocol instead, and a test double
for it implements only that area. ``SupabaseAppStore`` (api/store.py) is the
production implementation; ``tests/support.FakeStore`` is the in-memory one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .auth import AuthenticatedUser
from .models import PlanRequest, ProfileUpdateRequest
from .xp import XpAction


@dataclass(frozen=True)
class RehabExposureWindow:
    """Newest bounded evidence rows plus whether older episode history exists."""

    rows: list[dict[str, Any]]
    history_truncated: bool


class ProfileStore(Protocol):
    def validate_runtime_schema(self) -> None: ...

    def is_admin_email(self, email: str) -> bool: ...

    def ensure_profile(self, user: AuthenticatedUser) -> dict[str, Any]: ...

    def get_profile(self, athlete_id: str) -> dict[str, Any] | None: ...

    def get_profile_compliance(self, athlete_id: str) -> dict[str, Any] | None: ...

    def record_onboarding_stage(self, athlete_id: str, *, completed: bool = False) -> None: ...

    def update_profile(self, athlete_id: str, update: ProfileUpdateRequest) -> dict[str, Any]: ...

    def record_compliance_acceptance(
        self,
        athlete_id: str,
        *,
        date_of_birth: str | None = None,
        accept_terms: bool | None = None,
        health_data_consent: bool | None = None,
    ) -> dict[str, Any]: ...

    def change_username(self, athlete_id: str, username: str) -> dict[str, Any]: ...

    def clear_onboarding_draft(self, athlete_id: str) -> None: ...

    def count_admin_profiles(self) -> int: ...


class IntakeStore(Protocol):
    def get_latest_intake(self, athlete_id: str) -> dict[str, Any] | None: ...

    def get_intake(self, intake_id: str) -> dict[str, Any] | None: ...

    def create_intake(self, athlete_id: str, request: PlanRequest) -> dict[str, Any]: ...

    def update_intake(
        self,
        intake_id: str,
        *,
        intake: dict[str, Any],
        fight_date: str | None,
        technical_style: list[str],
    ) -> dict[str, Any]: ...


class PlanStore(Protocol):
    def create_plan(
        self,
        *,
        athlete_id: str,
        intake_id: str | None,
        request: PlanRequest,
        result: dict[str, Any],
        plan_id: str | None = None,
    ) -> dict[str, Any]: ...

    def list_user_plans(self, athlete_id: str) -> list[dict[str, Any]]: ...

    def get_plan(self, plan_id: str) -> dict[str, Any] | None: ...

    def get_plan_for_athlete(self, plan_id: str, athlete_id: str) -> dict[str, Any] | None: ...

    def get_plan_identity_for_athlete(self, plan_id: str, athlete_id: str) -> dict[str, Any] | None: ...

    def get_training_plan_for_athlete(self, plan_id: str, athlete_id: str) -> dict[str, Any] | None: ...

    def get_latest_plan(self, athlete_id: str) -> dict[str, Any] | None: ...

    # ``id,status,stage2_status,intake_id`` only (api/store_performance.py).
    # In-memory stores have no compact projection and return their full rows.
    def get_plan_status(self, plan_id: str) -> dict[str, Any] | None: ...

    def get_latest_plan_status(self, athlete_id: str) -> dict[str, Any] | None: ...

    def get_active_plan_id(self, athlete_id: str) -> str | None: ...

    def set_active_plan_id(self, athlete_id: str, plan_id: str) -> None: ...

    def rename_plan(self, plan_id: str, plan_name: str) -> dict[str, Any]: ...

    def rename_plan_for_athlete(self, plan_id: str, athlete_id: str, plan_name: str) -> dict[str, Any]: ...

    def archive_plan(self, plan_id: str) -> dict[str, Any]: ...

    def archive_plan_for_athlete(self, plan_id: str, athlete_id: str) -> dict[str, Any]: ...

    def delete_plan(self, plan_id: str) -> None: ...

    def delete_plan_for_athlete(self, plan_id: str, athlete_id: str) -> None: ...

    def update_plan_stage2(self, plan_id: str, result: dict[str, Any]) -> dict[str, Any]: ...

    def update_plan_stage2_if_unchanged(
        self, plan_id: str, result: dict[str, Any], expected_snapshot: dict[str, Any]
    ) -> dict[str, Any]: ...

    def update_plan_structured_artifacts(
        self,
        plan_id: str,
        *,
        structured_plan: dict[str, Any] | None,
        schema_version: str | None,
        stage2_validator_report: dict[str, Any],
        expected_final_plan_text: str | None = None,
    ) -> dict[str, Any]: ...

    def update_plan_triage_approval(self, plan_id: str, *, why_log: dict[str, Any], stage2_status: str) -> dict[str, Any]: ...


class GenerationJobStore(Protocol):
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
    ) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...

    def count_generation_jobs_for_athlete_since(
        self,
        athlete_id: str,
        since_timestamp: str,
        *,
        sources: set[str] | None = None,
    ) -> int: ...

    def check_plan_generation_short_window_limit(
        self,
        athlete_id: str,
        max_requests: int,
        window_seconds: float,
    ) -> tuple[bool, int]: ...

    def get_generation_job(self, job_id: str) -> dict[str, Any] | None: ...

    def recover_generation_job_if_stale(self, job: dict[str, Any] | None) -> dict[str, Any] | None: ...

    def get_generation_job_by_client_request_id(self, *, athlete_id: str, client_request_id: str) -> dict[str, Any] | None: ...

    def get_visible_active_generation_job_for_athlete(self, athlete_id: str) -> dict[str, Any] | None: ...

    def reconcile_active_generation_job_for_athlete(
        self,
        athlete_id: str,
        *,
        stale_after_seconds: int | None = None,
    ) -> dict[str, Any] | None: ...

    def get_latest_generation_job_for_athlete(self, athlete_id: str) -> dict[str, Any] | None: ...

    def get_generation_job_by_plan_id(self, plan_id: str) -> dict[str, Any] | None: ...

    def has_active_generation_job_for_plan(self, plan_id: str) -> bool: ...

    def list_generation_jobs_for_athlete(self, athlete_id: str, *, limit: int = 10) -> list[dict[str, Any]]: ...

    def list_admin_active_generation_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]: ...

    def list_admin_triage_generation_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]: ...

    def list_orphaned_terminal_generation_jobs(self, *, limit: int = 500) -> list[dict[str, Any]]: ...

    def list_failed_triage_resume_jobs_with_approved_marker(self, *, limit: int = 500) -> list[dict[str, Any]]: ...

    def list_claimable_generation_jobs(self, *, limit: int = 20, stale_after_seconds: int | None = None) -> list[dict[str, Any]]: ...

    # Compact reads for polling, heartbeats and the worker (api/store_performance.py).
    # In-memory stores have no compact projection and return their full rows.
    def get_generation_job_status(self, job_id: str) -> dict[str, Any] | None: ...

    def get_visible_active_generation_job_status(self, athlete_id: str) -> dict[str, Any] | None: ...

    def get_latest_generation_job_status(self, athlete_id: str) -> dict[str, Any] | None: ...

    def poll_claimable_generation_jobs(
        self, *, limit: int = 20, stale_after_seconds: int | None = None
    ) -> list[dict[str, Any]]: ...

    def list_generation_job_recovery_candidates(self, *, limit: int) -> list[dict[str, Any]]: ...

    def claim_generation_job_start(self, job_id: str, *, stale_after_seconds: int | None = None, worker_id: str | None = None) -> dict[str, Any] | None: ...

    def claim_generation_job(self, job_id: str, *, stale_after_seconds: int | None = None, worker_id: str | None = None) -> dict[str, Any] | None: ...

    def count_active_generation_jobs(self, *, stale_after_seconds: int | None = None) -> int: ...

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
    ) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...

    def update_generation_job(
        self, job_id: str, *, refresh: bool = True, **changes: Any
    ) -> dict[str, Any]: ...

    def record_stage2_cost(self, job_id: str, metadata: dict[str, Any]) -> None: ...


class AdminStore(Protocol):
    def list_admin_plans(
        self, *, limit: int = 50, offset: int = 0, q: str | None = None
    ) -> list[dict[str, Any]]: ...

    def list_admin_review_plans(self, *, limit: int = 100) -> list[dict[str, Any]]: ...

    def list_plans_missing_structured_plan(self, *, limit: int = 50) -> list[dict[str, Any]]: ...

    def list_plans_with_orphaned_structured_card_attempt(
        self, *, limit: int = 25
    ) -> list[dict[str, Any]]: ...

    def list_admin_athletes(
        self, *, limit: int = 50, offset: int = 0, q: str | None = None
    ) -> list[dict[str, Any]]: ...

    def get_admin_athlete(self, athlete_id: str) -> dict[str, Any] | None: ...

    def list_admin_athletes_by_ids(self, athlete_ids: list[str]) -> list[dict[str, Any]]: ...

    def list_admin_signup_activation(self, *, since: str, limit: int = 5000) -> list[dict[str, Any]]: ...

    def create_admin_review(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...

    def list_admin_reviews(self, *, status_filter: str | None = "pending", limit: int = 50) -> list[dict[str, Any]]: ...

    def count_pending_admin_reviews_for_athlete(self, athlete_id: str) -> int: ...

    def resolve_admin_review(self, review_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...


class TodayStore(Protocol):
    """Check-ins and session completions (api/routes/today.py)."""

    def upsert_today_checkin(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...

    def get_today_checkin(
        self, athlete_id: str, plan_id: str | None, training_day: str
    ) -> dict[str, Any] | None: ...

    def list_today_checkins_for_day(
        self, athlete_id: str, training_day: str
    ) -> list[dict[str, Any]]: ...

    def list_today_checkins(
        self, athlete_id: str, *, limit: int = 14
    ) -> list[dict[str, Any]]: ...

    def upsert_session_completion(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...

    def get_session_completion_by_id(self, athlete_id: str, completion_id: str) -> dict[str, Any] | None: ...

    def list_session_detail_exercise_logs(self, athlete_id: str, *, plan_id: str,
                                         training_day: str, session_ids: list[str]) -> list[dict[str, Any]]: ...

    def get_external_session_sparring_log(self, athlete_id: str, entry_id: str) -> dict[str, Any] | None: ...

    def get_session_completion(
        self, athlete_id: str, session_id: str, training_day: str
    ) -> dict[str, Any] | None: ...

    def initialize_session_completion_rehab_contexts(
        self,
        athlete_id: str,
        *,
        completion_id: str,
        plan_id: str,
        session_id: str,
        training_day: str,
        contexts: list[dict[str, Any]],
    ) -> dict[str, Any] | None: ...

    def create_custom_plan(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...

    def list_custom_plans(self, athlete_id: str) -> list[dict[str, Any]]: ...

    def get_custom_plan(self, athlete_id: str, plan_id: str) -> dict[str, Any] | None: ...

    def update_custom_plan(self, athlete_id: str, plan_id: str, fields: dict[str, Any]) -> dict[str, Any] | None: ...

    def delete_custom_plan(self, athlete_id: str, plan_id: str) -> bool: ...

    def create_external_training_session(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...

    def list_external_training_sessions(self, athlete_id: str, *, limit: int = 200, include_deleted: bool = False,
                                        before_day: str | None = None) -> list[dict[str, Any]]: ...

    def get_external_training_session(self, athlete_id: str, entry_id: str) -> dict[str, Any] | None: ...

    def update_external_training_session(self, athlete_id: str, entry_id: str, fields: dict[str, Any]) -> dict[str, Any] | None: ...

    def set_external_training_takeaway(self, athlete_id: str, entry_id: str, takeaway: str) -> dict[str, Any] | None: ...

    def delete_external_training_session(self, athlete_id: str, entry_id: str) -> bool: ...

    def get_personal_exercise(self, athlete_id: str, personal_exercise_id: str) -> dict[str, Any] | None: ...

    def ensure_personal_exercise(self, athlete_id: str, name: str, name_key: str) -> dict[str, Any]: ...

    def get_manual_exercise(self, athlete_id: str, entry_id: str) -> dict[str, Any] | None: ...

    def create_manual_exercise(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...

    def update_manual_exercise(self, athlete_id: str, entry_id: str, fields: dict[str, Any], *,
                               revision: int) -> dict[str, Any] | None: ...

    def delete_manual_exercise(self, athlete_id: str, entry_id: str) -> bool: ...

    def list_manual_exercises(self, athlete_id: str, external_session_id: str) -> list[dict[str, Any]]: ...

    def count_manual_exercises(self, athlete_id: str, external_session_ids: list[str]) -> dict[str, int]: ...

    def has_exercise_record(self, athlete_id: str, exercise_key: str) -> bool: ...

    def list_manual_exercise_index_page(self, athlete_id: str, *, as_of: str, after_id: str | None,
                                        limit: int = 500) -> list[dict[str, Any]]: ...

    def list_manual_exercise_performances(self, athlete_id: str, *, exercise_key: str | None,
                                          personal_exercise_id: str | None, as_of: str, before_day: str | None,
                                          before_id: str | None, limit: int = 31) -> list[dict[str, Any]]: ...

    def list_session_completions(
        self, athlete_id: str, *, limit: int = 30, before_day: str | None = None
    ) -> list[dict[str, Any]]: ...

    def list_session_completions_from_day(
        self, athlete_id: str, training_day: str, *, limit: int = 200
    ) -> list[dict[str, Any]]: ...

    def list_rehab_schedule_completions(self, athlete_id: str, *, from_day: str) -> list[dict[str, Any]]: ...

    def get_rehab_schedule_revision(self, athlete_id: str) -> dict[str, Any]: ...

    def list_plan_session_completions(
        self, athlete_id: str, plan_id: str, *, limit: int = 500
    ) -> list[dict[str, Any]]: ...

    def list_session_logs(self, athlete_id: str, *, limit: int = 500) -> list[dict[str, Any]]: ...


class StreakStore(Protocol):
    """Server-authoritative athlete streaks (api/services/streaks.py)."""

    def get_athlete_streaks(self, athlete_id: str) -> dict[str, Any] | None: ...

    def upsert_athlete_streaks(
        self, athlete_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]: ...

    def record_daily_activity(
        self, athlete_id: str, activity_date: str
    ) -> dict[str, Any]: ...

    def list_daily_activity(self, athlete_id: str) -> list[dict[str, Any]]: ...


class XpStore(Protocol):
    """Durable, server-awarded XP, plan milestones and week lifecycle."""

    def reconcile_training_engagement_xp(self, athlete_id: str, *, manual_session_id: str | None = None) -> Any: ...

    def list_unrewarded_contribution_revisions(self, *, after_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]: ...

    def reconcile_approved_contribution_xp(self, revision_id: str) -> Any: ...

    def award_xp(
        self,
        athlete_id: str,
        *,
        action: XpAction,
        idempotency_key: str,
        calendar_date: str | None = None,
    ) -> dict[str, Any]: ...

    def validate_xp_abuse_hardening(self) -> Any: ...

    def reconcile_feedback_xp(
        self, athlete_id: str, *, feedback_id: str, target_amount: int
    ) -> dict[str, Any] | None: ...

    def get_xp_progress_state(self, athlete_id: str, *, limit: int) -> dict[str, Any]: ...

    def xp_award_exists(
        self,
        athlete_id: str,
        *,
        action: str | None = None,
        idempotency_key: str | None = None,
        calendar_date: str | None = None,
    ) -> bool: ...

    def list_plan_milestones(self, athlete_id: str, *, limit: int) -> list[dict[str, Any]]: ...

    def record_plan_milestone(
        self,
        athlete_id: str,
        *,
        plan_id: str,
        milestone_type: str,
        milestone_key: str,
        phase_label: str | None,
        metadata: dict[str, Any],
    ) -> dict[str, Any] | None: ...

    def begin_week_lifecycle_reconciliation(
        self, athlete_id: str, *, plan_id: str, week_id: str
    ) -> dict[str, Any] | None: ...

    def complete_week_lifecycle_reconciliation(
        self, athlete_id: str, *, plan_id: str, week_id: str
    ) -> dict[str, Any] | None: ...


class NotificationStore(Protocol):
    """The notification ledger (api/services/notification_foundation.py)."""

    def get_notification_preferences(self, profile_id: str) -> dict[str, Any] | None: ...

    def upsert_notification_preferences(
        self, profile_id: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None: ...

    def record_notification_evaluation(
        self, row: dict[str, Any], *, min_interval_seconds: int
    ) -> dict[str, Any] | None: ...

    def list_notification_evaluations(
        self, profile_id: str, training_day: str, *, intent: str | None = None
    ) -> list[dict[str, Any]]: ...

    def has_notification_evaluation_decision(
        self, profile_id: str, *, dedupe_key: str, decision: str
    ) -> bool: ...

    def claim_notification_delivery(
        self, params: dict[str, Any], *, now_utc: datetime
    ) -> Any: ...

    def finalize_notification_delivery(
        self,
        delivery_id: str,
        claim_token: str,
        *,
        status: str,
        delivered_count: int,
        error_code: str | None,
    ) -> None: ...

    def get_notification_simulation_state(
        self,
        profile_id: str,
        *,
        dedupe_keys: list[str],
        training_days: list[str],
        notification_classes: list[str],
        action_keys: list[str],
    ) -> dict[str, list[dict[str, Any]]]: ...

    def invalidate_notification_action(
        self,
        profile_id: str,
        *,
        action_key: str,
        training_day: str,
        completed_at: datetime,
        source_metadata: dict[str, Any],
    ) -> int: ...

    def list_notification_deliveries(
        self,
        profile_id: str,
        *,
        intent: str | None = None,
        training_day: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]: ...

    def list_notification_templates(self, intent: str, *, locale: str) -> list[dict[str, Any]]: ...


class PushSubscriptionStore(Protocol):
    """Web push subscriptions (api/routes/push.py, push notification services)."""

    def upsert_push_subscription(self, profile_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...

    def list_push_subscriptions(self, profile_id: str) -> list[dict[str, Any]]: ...

    def delete_push_subscription(self, profile_id: str, endpoint: str) -> None: ...

    def delete_push_subscription_by_endpoint(self, endpoint: str) -> None: ...

    def list_all_push_subscriptions(
        self, *, limit: int = 500, after_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    def mark_push_subscription_morning_sent(
        self, subscription_id: str, *, sent_day: str
    ) -> None: ...


class InjuryStore(Protocol):
    """Injury flags, rehab exposure evidence and athlete-reported training events."""

    def create_injury_flag(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...

    def list_injury_flags(
        self, athlete_id: str, *, statuses: tuple[str, ...] = ("open", "monitoring"), limit: int = 20
    ) -> list[dict[str, Any]]: ...

    def update_injury_flag(self, flag_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...

    def get_injury_flag_for_athlete(self, flag_id: str, athlete_id: str) -> dict[str, Any] | None: ...

    # Atomic adopt-or-insert of an intake injury (api/services/intake_injury_sync.py).
    def adopt_or_create_intake_injury_flag(self, params: dict[str, Any]) -> dict[str, Any] | None: ...

    def create_rehab_exposure(self, athlete_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    def record_injury_episode_event(self, athlete_id: str, event: dict[str, Any]) -> dict[str, Any]: ...

    def list_injury_episode_events(self, athlete_id: str, *, injury_id: str, injury_episode_id: str) -> list[dict[str, Any]]: ...

    def list_pending_delayed_rehab(self, athlete_id: str, training_day: str) -> list[dict[str, Any]]: ...

    def list_rehab_exposures_by_ids(
        self, athlete_id: str, exposure_ids: list[str]
    ) -> list[dict[str, Any]]: ...

    def list_rehab_exposures(
        self,
        athlete_id: str,
        *,
        injury_id: str,
        injury_episode_id: str,
        limit: int = 200,
    ) -> RehabExposureWindow: ...

    def create_adaptation_note(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...

    def record_sparring_log(
        self, athlete_id: str, fields: dict[str, Any], *, review_reason: str | None
    ) -> dict[str, Any]: ...

    def list_sparring_logs(
        self, athlete_id: str, *, limit: int = 60, from_day: str | None = None
    ) -> list[dict[str, Any]]: ...

    def latest_sparring_log_day(
        self, athlete_id: str, *, hard: bool = False, rocked: bool = False
    ) -> str | None: ...

    def upsert_exercise_log(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...

    def upsert_exercise_logs(
        self, athlete_id: str, rows: list[dict[str, Any]], *, keep_existing: bool = False
    ) -> list[dict[str, Any]]: ...

    def list_exercise_logs_for_day(
        self, athlete_id: str, *, plan_id: str, training_day: str
    ) -> list[dict[str, Any]]: ...

    def list_exercise_index_page(self, athlete_id: str, *, as_of: str, after_id: str | None,
                                 limit: int = 500) -> list[dict[str, Any]]: ...

    def list_exercise_performances(self, athlete_id: str, *, exercise_key: str | None, log_id: str | None,
                                   as_of: str, before_day: str | None, before_id: str | None,
                                   limit: int = 31) -> list[dict[str, Any]]: ...

    def list_exercise_history(
        self, athlete_id: str, *, before_day: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]: ...

    def list_recent_exercise_loads(
        self, athlete_id: str, *, before_day: str, limit: int = 200
    ) -> list[dict[str, Any]]: ...


class FeedbackStore(Protocol):
    """Secure beta feedback (api/routes/feedback.py)."""

    def get_context_feedback(self, profile_id: str, context_key: str) -> dict[str, Any] | None: ...

    def get_feedback_plan_for_owner(self, plan_id: str, profile_id: str) -> dict[str, Any] | None: ...

    def get_feedback_active_plan_id(self, profile_id: str) -> str | None: ...

    def get_feedback_today_checkin(
        self, profile_id: str, plan_id: str, training_day: str
    ) -> dict[str, Any] | None: ...

    def list_feedback_injury_flags(self, profile_id: str, *, limit: int = 20) -> list[dict[str, Any]]: ...

    def get_feedback_intake(self, intake_id: str) -> dict[str, Any] | None: ...

    def upsert_context_feedback(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def insert_global_feedback(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def list_admin_feedback(self, *, limit: int = 50) -> list[dict[str, Any]]: ...

    def get_feedback_screenshot_path(self, feedback_id: str) -> str | None: ...

    def create_feedback_screenshot_signed_url(self, path: str, *, expires_in: int) -> str: ...

    def claim_feedback_rate_limit(
        self,
        profile_id: str,
        *,
        report_limit: int,
        screenshot_limit: int,
        has_screenshot: bool,
    ) -> tuple[bool, str | None, int]: ...

    def upload_feedback_screenshot(self, path: str, data: bytes, mime: str) -> None: ...

    def delete_feedback_screenshots(self, paths: list[str]) -> None: ...

    def list_expired_feedback_screenshots(self, *, limit: int = 100) -> list[dict[str, Any]]: ...

    def list_profile_feedback_screenshots(self, profile_id: str, *, limit: int = 100) -> list[dict[str, Any]]: ...

    def clear_feedback_screenshot(self, feedback_id: str, expected_path: str) -> bool: ...


class ExerciseMediaStore(Protocol):
    """Exercise demo videos (api/services/exercise_media.py)."""

    def list_exercise_media(self) -> list[dict[str, Any]]: ...

    def list_exercise_media_for_verification(self) -> list[dict[str, Any]]: ...

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
    ) -> None: ...


class CommunityStore(Protocol):
    def community_rpc(self, name: str, params: dict[str, Any]) -> Any: ...


class PrivateMediaStore(Protocol):
    def private_media_worker_healthy(self) -> bool: ...
    def claim_private_media_budget(self, athlete_id: str) -> None: ...
    def reserve_private_video(self, athlete_id: str, size_bytes: int, content_type: str) -> dict[str, Any]: ...
    def create_private_media(self, athlete_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...
    def get_private_media(self, athlete_id: str, media_id: str) -> dict[str, Any] | None: ...
    def get_private_media_for_asset(self, athlete_id: str, asset_id: str) -> dict[str, Any] | None: ...
    def get_private_video_asset(self, athlete_id: str, asset_id: str) -> dict[str, Any] | None: ...
    def private_video_signed_url(self, path: str, *, expires_in: int) -> str: ...


class AppStore(
    ProfileStore,
    IntakeStore,
    PlanStore,
    GenerationJobStore,
    AdminStore,
    TodayStore,
    StreakStore,
    XpStore,
    NotificationStore,
    PushSubscriptionStore,
    InjuryStore,
    FeedbackStore,
    ExerciseMediaStore,
    PrivateMediaStore,
    CommunityStore,
    Protocol,
):
    """Everything the app, worker and routes persist."""
