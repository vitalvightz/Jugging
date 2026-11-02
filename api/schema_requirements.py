"""Centralized definition of the Supabase schema the backend depends on.

This module is intentionally dependency-light (standard library only) so it can
be imported by:

* ``api.store`` (the live runtime store) to share required column constants, and
* ``tools/check_supabase_runtime_schema.py`` (the deploy-gate checker), and
* the unit tests, *without* pulling in ``supabase``/``fastapi`` or requiring
  live credentials.

It contains two things:

1. The canonical *requirements* (tables, columns, functions, indexes, RLS).
2. Pure comparison helpers that diff those requirements against a catalog
   snapshot (a plain ``dict``). The helpers never touch the network and never
   read user row data, which keeps them trivially unit-testable and safe to use
   as a production deploy gate.

Note on the intakes table: the application stores athlete intake records in the
``public.athlete_intakes`` table (see ``api/store.py``). There is no table named
``intakes``; ``athlete_intakes`` is the canonical name the backend depends on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Canonical table names
# ---------------------------------------------------------------------------

INTAKES_TABLE = "athlete_intakes"

REQUIRED_TABLES: tuple[str, ...] = (
    "product_events", "product_analytics_preferences", "product_analytics_excluded_emails", "product_analytics_rollout",
    "profiles",
    "plans",
    "generation_jobs",
    INTAKES_TABLE,
    "plan_generation_rate_limits",
    # Accountability trail for admin role changes (api/store.py::set_profile_role).
    # Required so the deploy gate catches an environment where the migration has
    # not been applied and admin changes would silently lose their audit record.
    "admin_role_audit",
    # Live athlete daily tracking (api/routes/daily.py + api/store.py). The
    # dashboard, check-in, session-log, and admin review-queue endpoints all
    # read/write these tables, so a missing migration must fail the deploy gate.
    "daily_checkins",
    "session_logs",
    "injury_flags",
    "adaptation_notes",
    "admin_reviews",
    # Block 4 Today/Overview persistence (api/routes/today.py + api/store.py):
    # athlete-local categorical check-ins with the server-evaluated
    # recommendation, and per-session completion records. A missing migration
    # must fail the deploy gate just like the daily-tracking tables above.
    "today_checkins",
    "session_completions",
    # Post-sparring entries from the round timer (POST /api/today/sparring-log).
    # A missing migration must fail the gate: the endpoint writes this table
    # and, for rocked reports, its admin review in one RPC.
    "sparring_logs",
    # Accepted sparring request ids, so a retried report never writes twice.
    "sparring_log_requests",
    # What the athlete actually did per prescribed block (POST
    # /api/today/exercise-log). A missing migration must fail the gate: the
    # in-session log would otherwise fail on every save.
    "exercise_logs",
    "external_training_sessions",
    # Optional exercise results on a manual session and the athlete's private
    # movement names (PUT /api/training/sessions/{id}/exercises/{entry_id}).
    "manual_exercise_entries",
    "personal_exercises",
    "custom_training_plans",
    "private_video_assets",
    "private_exercise_media",
    "private_media_budget",
    "private_media_worker_health",
    "community_exercises", "community_exercise_revisions", "community_exercise_audit", "community_exercise_reports",
    "rehab_exposures",
    "injury_episode_events",
    # Durable XP aggregate + immutable award ledger. Award writes are atomic
    # through public.award_athlete_xp and never browser-controlled.
    "xp_accounts",
    "xp_awards",
    # Secure beta feedback and its durable, per-profile abuse controls.
    "beta_feedback",
    "beta_feedback_rate_limits",
    "notification_preferences",
    "notification_deliveries",
    "notification_templates",
    "notification_action_states",
    "notification_evaluations",
)

# ---------------------------------------------------------------------------
# Required columns
# ---------------------------------------------------------------------------

# The Stage 1/Stage 2 plan runtime columns. This is the canonical source of
# truth shared with ``api.store`` (imported there as PLAN_RUNTIME_REQUIRED_COLUMNS)
# so the live store and the deploy-gate checker can never drift apart.
PLAN_RUNTIME_REQUIRED_COLUMNS: tuple[str, ...] = (
    "draft_plan_text",
    "final_plan_text",
    "planning_brief",
    "stage2_payload",
    "stage2_handoff_text",
    "stage2_retry_text",
    "stage2_validator_report",
    "stage2_status",
    "stage2_attempt_count",
    "parsing_metadata",
    # Schema-first structured plan (additive, nullable). Written beside the raw
    # plan_text when structured generation succeeds; legacy rows leave them NULL
    # and the create_plan legacy-schema fallback drops them when absent.
    "structured_plan",
    "schema_version",
)

REQUIRED_PLANS_COLUMNS: tuple[str, ...] = (
    "id",
    "athlete_id",
    "intake_id",
    "plan_text",
    *PLAN_RUNTIME_REQUIRED_COLUMNS,
    # Non-runtime columns the backend reads/writes in create_plan() and
    # list_user_plans() (PLAN_SUMMARY_SELECT) — see api/store.py.
    "fight_date",
    "technical_style",
    "full_name",
    "plan_name",
    # Athlete-directed exercise swaps/adds/removals beside the generated card
    # (api/services/exercise_overrides.py). Read on every training-plan read.
    "exercise_overrides",
    "exercise_overrides_revision",
    "coach_notes",
    "pdf_url",
    "why_log",
    "status",
    "created_at",
)

# Stage 2 token/cost telemetry written to generation_jobs after finalization
# (see api/store.py::record_stage2_cost). These let an admin/dev audit cost per
# athlete/job straight from the database instead of scraping logs. All columns
# are nullable: a row predating the feature, or a Stage 2 call where the OpenAI
# response omitted usage, simply leaves them NULL. The runtime write is
# best-effort and degrades gracefully if the columns are absent, but they are
# listed here so the deploy gate (tools/check_supabase_runtime_schema.py)
# guarantees the migration is applied in production where the audit is relied on.
GENERATION_JOB_STAGE2_COST_COLUMNS: tuple[str, ...] = (
    "stage2_model",
    "stage2_input_tokens",
    "stage2_output_tokens",
    "stage2_total_tokens",
    "stage2_estimated_cost_usd",
    "stage2_attempt_count",
    "stage2_response_id",
    "stage2_cost_recorded_at",
)

REQUIRED_GENERATION_JOBS_COLUMNS: tuple[str, ...] = (
    "id",
    "athlete_id",
    "client_request_id",
    "source",
    "status",
    "attempt_count",
    "heartbeat_at",
    "started_at",
    "completed_at",
    "failed_at",
    # Worker ownership for the running attempt (claim_generation_job writes
    # them; the terminal RPCs enforce them).
    "claimed_by",
    "claimed_at",
    "created_at",
    "updated_at",
    "error",
    "intake_id",
    "plan_id",
    "progress_milestones",
    "request_payload",
    "payload_hash",
    "stage1_result",
    "final_result",
    *GENERATION_JOB_STAGE2_COST_COLUMNS,
)

REQUIRED_PROFILES_COLUMNS: tuple[str, ...] = (
    "analytics_excluded", "onboarding_started_at", "onboarding_completed_at",
    "starting_path",
    "training_habits",
    "training_habits_revision",
    "id",
    "email",
    "full_name",
    "role",
    "access_status",
    "username",
    "username_change_history",
    "avatar_url",
    "onboarding_draft",
    "created_at",
    "updated_at",
    # Additional columns written by profile bootstrap in
    # _build_profile_payload() — see api/store.py.
    "technical_style",
    "tactical_style",
    "stance",
    "professional_status",
    "record_summary",
    "athlete_timezone",
    "athlete_locale",
    "active_plan_id",
    "appearance_mode",
    "private_trial_ack_at",
    # Compliance evidence: age band source, Terms acceptance and the separate
    # health-data consent. Missing columns here mean the consent gate cannot be
    # evaluated, so the runtime check must fail loudly rather than treat an
    # absent column as "not consented" for every athlete.
    "date_of_birth",
    "terms_version",
    "terms_accepted_at",
    "health_consent_version",
    "health_data_consent",
    "health_consent_at",
    "health_consent_withdrawn_at",
)

REQUIRED_ADMIN_ROLE_AUDIT_COLUMNS: tuple[str, ...] = (
    "id",
    "target_athlete_id",
    "target_email",
    "previous_role",
    "new_role",
    "action",
    "actor",
    "reason",
    "created_at",
)

REQUIRED_DAILY_CHECKINS_COLUMNS: tuple[str, ...] = (
    "id",
    "athlete_id",
    "checkin_date",
    "readiness",
    "fatigue",
    "soreness",
    "sleep_quality",
    "sleep_hours",
    "injury_note",
    "notes",
    "readiness_state",
    "created_at",
    "updated_at",
)

REQUIRED_SESSION_LOGS_COLUMNS: tuple[str, ...] = (
    "id",
    "athlete_id",
    "plan_id",
    "session_date",
    "session_type",
    "completed",
    "rpe",
    "duration_minutes",
    "notes",
    "created_at",
    "updated_at",
)

REQUIRED_INJURY_FLAGS_COLUMNS: tuple[str, ...] = (
    "id",
    "athlete_id",
    "plan_id",
    "source",
    "body_area",
    "description",
    "episode_id", "body_region", "side",
    "severity",
    "severity_source",
    "manual_severity",
    "status",
    "latest_reported_status",
    "resolved_at",
    "created_at",
    "updated_at",
)

REQUIRED_ADAPTATION_NOTES_COLUMNS: tuple[str, ...] = (
    "id",
    "athlete_id",
    "plan_id",
    "checkin_id",
    "session_log_id",
    "rule_code",
    "decision",
    "summary",
    "details",
    "created_at",
)

REQUIRED_ADMIN_REVIEWS_COLUMNS: tuple[str, ...] = (
    "id",
    "athlete_id",
    "adaptation_note_id",
    "injury_flag_id",
    "reason",
    "status",
    "resolution_notes",
    "resolved_by",
    "resolved_at",
    "created_at",
    "updated_at",
)

# Block 4 Today check-in: athlete-local categorical inputs + safety flags plus
# the server-evaluated recommendation persisted on the same row (the
# recommendation is 1:1 with the check-in, so source_checkin_id is this row id).
REQUIRED_TODAY_CHECKINS_COLUMNS: tuple[str, ...] = (
    "id",
    "athlete_id",
    "plan_id",
    "training_day",
    "athlete_timezone",
    "sleep",
    "body",
    "pain",
    "phase",
    "active_injury",
    "previous_session",
    "sharp_pain",
    "instability",
    "swelling",
    "neurological_symptoms",
    "illness_symptoms",
    "cannot_warm_into_movement",
    "worse_next_day_pain",
    "recommendation_state",
    "recommendation_reason",
    "recommendation_triggers",
    "created_at",
    "updated_at",
)

REQUIRED_SESSION_COMPLETIONS_COLUMNS: tuple[str, ...] = (
    "completion_origin",
    "id",
    "athlete_id",
    "plan_id",
    "session_id",
    "training_day",
    "status",
    "session_rpe",
    "pain_after",
    "modification_reason",
    "notes",
    "started_at",
    "completed_at",
    "rehab_response_contexts",
    "prescription_snapshot", "rehab_performance",
    "created_at",
    "updated_at",
)

REQUIRED_SPARRING_LOGS_COLUMNS: tuple[str, ...] = (
    "id",
    "athlete_id",
    "plan_id",
    "session_id",
    "training_day",
    "source",
    "planned_intensity",
    "intensity",
    "rounds_completed",
    "round_seconds",
    "head_contact",
    "rocked",
    "notes",
    "created_at",
    # Manual-session link and ordered corrections (record_sparring_log RPC).
    "external_session_id",
    "detail_revision",
    "admin_review_id",
    # "One thing to work on next time" (plain update after the RPC).
    "focus",
)

REQUIRED_EXERCISE_LOGS_COLUMNS: tuple[str, ...] = (
    "id",
    "athlete_id",
    "plan_id",
    "session_id",
    "block_id",
    "exercise_key",
    "training_day",
    "status",
    "reason",
    "prescribed",
    "actual",
    "notes",
    "created_at",
    "updated_at",
)

REQUIRED_EXTERNAL_TRAINING_COLUMNS: tuple[str, ...] = (
    "id", "athlete_id", "client_request_id", "request_fingerprint", "training_day",
    "activity", "sport", "session_type", "duration_minutes", "effort", "takeaway", "distance_value", "distance_unit", "duration_seconds",
    "deleted_at", "created_at", "updated_at",
)

REQUIRED_MANUAL_EXERCISE_ENTRIES_COLUMNS: tuple[str, ...] = (
    "id", "athlete_id", "external_session_id", "client_entry_id", "training_day", "exercise_key",
    "personal_exercise_id", "display_name", "measurement_format", "actual", "notes", "revision", "deleted_at",
    "created_at", "updated_at",
)

REQUIRED_XP_ACCOUNTS_COLUMNS: tuple[str, ...] = (
    "athlete_id",
    "total_xp",
    "last_daily_login_date",
    "created_at",
    "updated_at",
)

REQUIRED_XP_AWARDS_COLUMNS: tuple[str, ...] = (
    "id",
    "athlete_id",
    "action",
    "amount",
    "idempotency_key",
    "calendar_date",
    "awarded_at",
)

REQUIRED_BETA_FEEDBACK_COLUMNS: tuple[str, ...] = (
    "id",
    "submitted_by_profile_id",
    "context_key",
    "surface",
    "category",
    "response",
    "reason",
    "comment",
    "structured_response",
    "contact_allowed",
    "priority",
    "plan_id",
    "today_checkin_id",
    "session_id",
    "camp_phase",
    "readiness_snapshot",
    "injury_snapshot",
    "app_version",
    "technical_context",
    "screenshot_path",
    "screenshot_mime",
    "screenshot_size_bytes",
    "screenshot_width",
    "screenshot_height",
    "screenshot_expires_at",
    "screenshot_deleted_at",
    "created_at",
    "updated_at",
)

REQUIRED_BETA_FEEDBACK_RATE_LIMIT_COLUMNS: tuple[str, ...] = (
    "id",
    "submitted_by_profile_id",
    "scope",
    "created_at",
)

REQUIRED_NOTIFICATION_PREFERENCES_COLUMNS: tuple[str, ...] = (
    "profile_id", "push_enabled", "session_reminders", "checkin_reminders",
    "injury_followups", "plan_update_alerts", "progress_milestones",
    "coach_messages", "quiet_hours_enabled", "quiet_hours_start",
    "quiet_hours_end", "preferred_training_time", "created_at", "updated_at",
)

REQUIRED_NOTIFICATION_DELIVERIES_COLUMNS: tuple[str, ...] = (
    "id", "profile_id", "notification_type", "intent", "category", "priority",
    "title", "body", "url", "tag", "dedupe_key", "expires_at", "status",
    "claim_token", "claimed_at", "attempt_count", "delivered_count", "error_code",
    "sent_at", "training_day", "scheduled_for", "timing_source",
    "timing_confidence", "variant_id", "source_event_metadata", "action_key",
    "notification_class", "respect_quiet_hours", "merged_intents", "cancelled_at",
    "cancellation_reason", "created_at", "updated_at",
)

REQUIRED_NOTIFICATION_TEMPLATES_COLUMNS: tuple[str, ...] = (
    "intent", "variant_id", "title_template", "body_template", "locale",
    "template_version", "active", "selection_weight", "minimum_timing_confidence",
    "created_at", "updated_at",
)

REQUIRED_NOTIFICATION_ACTION_STATES_COLUMNS: tuple[str, ...] = (
    "profile_id", "action_key", "training_day", "completed_at", "source_metadata",
    "created_at", "updated_at",
)

REQUIRED_NOTIFICATION_EVALUATIONS_COLUMNS: tuple[str, ...] = (
    "id", "profile_id", "training_day", "intent", "notification_type", "category",
    "evaluated_at", "first_evaluated_at", "last_evaluated_at", "evaluation_count",
    "scheduled_for", "timing_source", "timing_confidence", "eligible", "decision",
    "rejection_reasons", "priority", "dedupe_key", "variant_id",
    "source_event_metadata", "resulting_delivery_id", "evaluation_key", "created_at",
    "updated_at",
)

# Map of table -> required columns, used by the checker.
REQUIRED_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "product_analytics_excluded_emails": ("email",),
    "product_events": ("id", "athlete_id", "event_name", "feature", "request_id", "flow_id", "created_at"),
    "product_analytics_rollout": ("singleton", "started_at"),
    "product_analytics_preferences": ("athlete_id", "enabled", "consent_version", "updated_at"),
    "product_activity": ("id", "athlete_id", "occurred_at", "feature", "action", "meaningful", "flow_id"),
    "admin_athlete_rollups": ("last_meaningful_activity_at",),
    "injury_episode_events": ("id", "athlete_id", "injury_id", "injury_episode_id", "event_type", "payload", "created_at", "clinical_capture"),
    "rehab_exposures": ("id", "athlete_id", "injury_id", "injury_episode_id", "drill_id", "body_region", "side",
                        "demand", "prescribed_dose", "completed_dose", "response", "event_json", "response_group_id",
                        "evidence_source", "occurred_at", "recorded_at", "created_at"),
    "plans": REQUIRED_PLANS_COLUMNS,
    "generation_jobs": REQUIRED_GENERATION_JOBS_COLUMNS,
    "profiles": REQUIRED_PROFILES_COLUMNS,
    "admin_role_audit": REQUIRED_ADMIN_ROLE_AUDIT_COLUMNS,
    "daily_checkins": REQUIRED_DAILY_CHECKINS_COLUMNS,
    "session_logs": REQUIRED_SESSION_LOGS_COLUMNS,
    "injury_flags": REQUIRED_INJURY_FLAGS_COLUMNS,
    "adaptation_notes": REQUIRED_ADAPTATION_NOTES_COLUMNS,
    "admin_reviews": REQUIRED_ADMIN_REVIEWS_COLUMNS,
    "today_checkins": REQUIRED_TODAY_CHECKINS_COLUMNS,
    "session_completions": REQUIRED_SESSION_COMPLETIONS_COLUMNS,
    "sparring_logs": REQUIRED_SPARRING_LOGS_COLUMNS,
    "sparring_log_requests": ("athlete_id", "client_request_id", "sparring_log_id", "created_at"),
    "exercise_logs": REQUIRED_EXERCISE_LOGS_COLUMNS,
    "external_training_sessions": REQUIRED_EXTERNAL_TRAINING_COLUMNS,
    "manual_exercise_entries": REQUIRED_MANUAL_EXERCISE_ENTRIES_COLUMNS,
    "personal_exercises": ("id", "athlete_id", "name", "name_key", "created_at", "updated_at"),
    "custom_training_plans": ("id", "athlete_id", "name", "kind", "sessions", "incomplete", "created_at", "updated_at"),
    "private_video_assets": ("id", "athlete_id", "status", "size_bytes", "content_type", "intake_path", "rendition_path", "duration_seconds", "width", "height", "sha256", "processor_version", "upload_expires_at", "processed_at", "created_at", "updated_at"),
    "private_exercise_media": ("id", "athlete_id", "provider", "video_id", "asset_id", "start_s", "end_s", "channel_title", "orientation", "made_for_kids", "created_at"),
    "private_media_budget": ("athlete_id", "upload", "created_at"),
    "private_media_worker_health": ("id", "healthy_at"),
    "community_exercises": ("id", "creator_id", "current_revision_id", "published_revision_id", "attribution_consent", "consent_at", "withdrawn_at", "created_at"),
    "community_exercise_revisions": ("id", "exercise_id", "name", "equipment", "notes", "media_id", "status", "submitted_at", "verified_at", "verified_by", "verified_media_id", "verified_proof", "confirmed_at", "confirmed_by", "decision_reason", "created_at", "changes_requested_at"),
    "community_exercise_audit": ("id", "exercise_id", "revision_id", "actor_id", "action", "details", "created_at"),
    "community_exercise_reports": ("id", "revision_id", "reporter_id", "reason", "status", "created_at"),
    "xp_accounts": REQUIRED_XP_ACCOUNTS_COLUMNS,
    "xp_awards": REQUIRED_XP_AWARDS_COLUMNS,
    "beta_feedback": REQUIRED_BETA_FEEDBACK_COLUMNS,
    "beta_feedback_rate_limits": REQUIRED_BETA_FEEDBACK_RATE_LIMIT_COLUMNS,
    "notification_preferences": REQUIRED_NOTIFICATION_PREFERENCES_COLUMNS,
    "notification_deliveries": REQUIRED_NOTIFICATION_DELIVERIES_COLUMNS,
    "notification_templates": REQUIRED_NOTIFICATION_TEMPLATES_COLUMNS,
    "notification_action_states": REQUIRED_NOTIFICATION_ACTION_STATES_COLUMNS,
    "notification_evaluations": REQUIRED_NOTIFICATION_EVALUATIONS_COLUMNS,
}

# ---------------------------------------------------------------------------
# Required functions / RPCs
# ---------------------------------------------------------------------------

# Stored schema-qualified for clear reporting; comparison is done on the bare
# proname within the ``public`` schema.
REQUIRED_FUNCTIONS: tuple[str, ...] = (
    "public.product_analytics_facts", "public.ingest_product_event", "public.prune_product_events",
    "public.record_onboarding_stage", "public.set_product_analytics_preference", "public.product_analytics_retention_health",
    "private.prevent_client_product_analytics_profile_writes",
    "public.change_profile_username",
    "public.try_parse_timestamptz",
    "public.check_plan_generation_short_window_limit",
    "public.create_generation_job_with_daily_limit",
    "public.claim_generation_job",
    "public.complete_generation_job",
    "public.fail_generation_job",
    # Internal trigger/policy helpers moved to the private schema by
    # 20260726162809_harden_internal_functions_and_search_paths.sql.
    "private.prevent_self_role_escalation",
    "private.prevent_username_policy_bypass",
    # Atomic role change + audit write (api/store.py::set_profile_role); a
    # missing function would silently break the only sanctioned role-change
    # path, so the deploy gate must catch it.
    "public.set_profile_role_with_audit",
    "private.is_admin",
    # Invoked during AppStore.validate_runtime_schema() at backend startup
    # (api/store.py); a missing lock RPC must fail this check too.
    "public.validate_generation_job_active_lock",
    "public.claim_beta_feedback_rate_limit",
    "public.claim_private_media_budget",
    "public.reserve_private_video",
    "public.claim_private_video",
    "public.finish_private_video",
    "public.private_video_cleanup_candidates",
    "public.private_demo_orphan_objects",
    "public.cleanup_private_youtube_previews",
    "public.validate_custom_plan_private_media",
    "public.community_revision_payload", "public.community_command", "public.community_detail",
    "public.community_list", "public.community_media_retained", "public.community_dismiss_report", "public.community_correction_window", "public.community_owned_revision",
    "public.community_revision_available", "public.community_creator_deleted",
    "public.award_athlete_xp",
    "public.reconcile_training_engagement_xp",
    "public.reconcile_approved_contribution_xp",
    "public.list_unrewarded_contribution_revisions",
    "public.enforce_tracking_xp_source",
    "public.xp_open_plan_session_weeks",
    "public.claim_notification_delivery_v2",
    "public.record_notification_evaluation",
    "public.invalidate_notification_action",
    # Atomic sparring log + rocked/dropped admin review (api/store.py::record_sparring_log).
    "public.record_sparring_log",
    # Plan-free daily check-in upsert (api/store.py::upsert_today_checkin).
    "public.upsert_plan_free_today_checkin",
    "public.record_rehab_exposure",
    "public.record_injury_episode_event",
    "public.clinical_review_capture_context",
    "public.record_clinical_review_event",
    "public.preserve_clinical_review_history",
    "public.verify_frozen_clinical_review",
    "public.pending_delayed_rehab",
    "public.preserve_started_prescription",
    "public.capture_injury_episode_change",
    "public.lock_injury_prescription_context",
)

# ---------------------------------------------------------------------------
# Required indexes / constraints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexRequirement:
    """A structural requirement satisfied by any one of ``accepted_names``.

    A requirement is met when *any* of ``accepted_names`` is present among the
    database's indexes or constraints. Several requirements can legitimately be
    served by either an index or a uniqueness constraint, so the checker unions
    both name sources before testing.
    """

    label: str
    accepted_names: tuple[str, ...]

    def is_satisfied(self, present_names: Iterable[str]) -> bool:
        present = set(present_names)
        return any(name in present for name in self.accepted_names)


INDEX_REQUIREMENTS: tuple[IndexRequirement, ...] = (
    IndexRequirement(label="product event retry uniqueness", accepted_names=("product_events_athlete_id_event_name_request_id_key",)),
    IndexRequirement(label="product event daily deduplication", accepted_names=("product_event_discovery_daily",)),
    IndexRequirement(label="product event athlete timeline", accepted_names=("product_events_athlete_time",)),
    IndexRequirement(label="product event expiry", accepted_names=("product_events_retention",)),
    IndexRequirement(label="one delayed response per exposure", accepted_names=("injury_episode_events_delayed_once_idx",)),
    IndexRequirement(
        label="generation_jobs active job uniqueness/lock",
        accepted_names=("generation_jobs_one_active_job_per_athlete",),
    ),
    IndexRequirement(
        label="generation_jobs athlete/client request uniqueness",
        accepted_names=(
            "generation_jobs_athlete_client_request_uidx",
            "generation_jobs_athlete_client_request_key",
        ),
    ),
    IndexRequirement(
        label="plan_generation_rate_limits athlete/created index",
        accepted_names=("plan_generation_rate_limits_athlete_created_idx",),
    ),
    IndexRequirement(
        label="profiles username uniqueness",
        accepted_names=("profiles_username_key", "profiles_username_idx"),
    ),
    IndexRequirement(
        label="profiles active_plan_id index",
        accepted_names=("profiles_active_plan_id_idx",),
    ),
    # Legacy: one check-in per athlete per day. The daily_checkins table is
    # retained for its historical rows but no longer has a read/write path —
    # the Today surface owns check-ins now. The index stays asserted so the
    # gate keeps matching the deployed schema.
    IndexRequirement(
        label="daily_checkins athlete/date uniqueness",
        accepted_names=("daily_checkins_athlete_date_key",),
    ),
    # One Today check-in per athlete/plan/training-day; the store's upsert path
    # (api/store.py::upsert_today_checkin) depends on this conflict target.
    IndexRequirement(
        label="today_checkins athlete/plan/training-day uniqueness",
        accepted_names=("today_checkins_athlete_plan_day_key",),
    ),
    # One plan-free check-in per athlete-day: the plan-scoped key above treats
    # NULL plan_ids as distinct, so the upsert RPC targets this partial key.
    IndexRequirement(
        label="today_checkins plan-free athlete/training-day uniqueness",
        accepted_names=("today_checkins_athlete_plan_free_day_key",),
    ),
    # One completion per athlete/session/training-day; the store's upsert path
    # (api/store.py::upsert_session_completion) depends on this conflict target.
    IndexRequirement(
        label="session_completions athlete/session/training-day uniqueness",
        accepted_names=("session_completions_athlete_session_day_key",),
    ),
    IndexRequirement(
        label="sparring_logs athlete/training-day index",
        accepted_names=("sparring_logs_athlete_day_idx",),
    ),
    IndexRequirement(
        # One detailed report per manual sparring session.
        label="sparring_logs manual-session uniqueness",
        accepted_names=("sparring_logs_external_session_key",),
    ),
    IndexRequirement(
        # Every accepted request id: a retried report is answered from here.
        label="sparring_log_requests athlete/request uniqueness",
        accepted_names=("sparring_log_requests_pkey",),
    ),
    IndexRequirement(
        # The upsert's ON CONFLICT target: one log per plan/day/block.
        label="exercise_logs plan/day/block uniqueness",
        accepted_names=("exercise_logs_occurrence_key",),
    ),
    IndexRequirement(
        label="xp awards athlete/idempotency uniqueness",
        accepted_names=("xp_awards_athlete_idempotency_key",),
    ),
    IndexRequirement(label="external training request uniqueness", accepted_names=("external_training_sessions_request_key",)),
    IndexRequirement(label="external training athlete/day index", accepted_names=("external_training_sessions_athlete_day_idx",)),
    # ON CONFLICT target: a retried exercise save never adds a second occurrence.
    IndexRequirement(label="manual exercise retry uniqueness", accepted_names=("manual_exercise_entries_request_key",)),
    IndexRequirement(label="personal exercise name uniqueness", accepted_names=("personal_exercises_name_key",)),
    IndexRequirement(label="custom plans athlete index", accepted_names=("custom_training_plans_athlete_updated_idx",)),
    IndexRequirement(label="community review queue index", accepted_names=("community_revision_queue_idx",)),
    IndexRequirement(label="community ownership index", accepted_names=("community_exercises_creator_idx",)),
    IndexRequirement(label="private video owner index", accepted_names=("private_video_assets_owner_created_idx",)),
    IndexRequirement(label="private media owner index", accepted_names=("private_exercise_media_owner_idx",)),
    IndexRequirement(label="private media quota index", accepted_names=("private_media_budget_owner_created_idx",)),
    IndexRequirement(
        label="xp awards one daily login per account calendar date",
        accepted_names=("xp_awards_one_daily_login_per_calendar_date",),
    ),
    IndexRequirement(
        label="beta_feedback submitter/context uniqueness",
        accepted_names=("beta_feedback_submitter_context_key",),
    ),
    IndexRequirement(
        label="beta_feedback rate-limit claim index",
        accepted_names=("beta_feedback_rate_limits_claim_idx",),
    ),
    IndexRequirement(
        label="beta_feedback screenshot expiry index",
        accepted_names=("beta_feedback_screenshot_expiry_idx",),
    ),
    IndexRequirement(
        label="notification delivery profile/day/class index",
        accepted_names=("notification_deliveries_profile_day_class_idx",),
    ),
    IndexRequirement(
        label="notification evaluation diagnostic index",
        accepted_names=("notification_evaluations_diagnostic_idx",),
    ),
)

# ---------------------------------------------------------------------------
# RLS requirements
# ---------------------------------------------------------------------------

RLS_REQUIRED_TABLES: tuple[str, ...] = (
    "product_events", "product_analytics_preferences", "product_analytics_excluded_emails", "product_analytics_rollout",
    "private_video_assets",
    "private_exercise_media",
    "private_media_budget",
    "private_media_worker_health",
    "community_exercises", "community_exercise_revisions", "community_exercise_audit", "community_exercise_reports",
    "custom_training_plans",
    "manual_exercise_entries", "personal_exercises",
    "rehab_exposures", "injury_episode_events",
    "profiles",
    "plans",
    INTAKES_TABLE,
    "generation_jobs",
    "plan_generation_rate_limits",
    "admin_role_audit",
    "daily_checkins",
    "session_logs",
    "injury_flags",
    "adaptation_notes",
    "admin_reviews",
    "today_checkins",
    "session_completions",
    "sparring_logs",
    "sparring_log_requests",
    "exercise_logs",
    "external_training_sessions",
    "xp_accounts",
    "xp_awards",
    "beta_feedback",
    "beta_feedback_rate_limits",
    "notification_preferences",
    "notification_deliveries",
    "notification_templates",
    "notification_action_states",
    "notification_evaluations",
)


# ---------------------------------------------------------------------------
# Introspection snapshot + comparison
# ---------------------------------------------------------------------------


class SchemaIntrospectionError(ValueError):
    """Raised when the catalog snapshot payload is malformed."""


@dataclass(frozen=True)
class SchemaIntrospection:
    """A normalized, network-free snapshot of the live database catalog.

    All names are bare object names within the ``public`` schema, except
    ``private_functions`` (bare names within ``private``). No row data is
    ever captured here — only catalog metadata (table/column/function/index
    names and per-table RLS flags).
    """

    tables: frozenset[str]
    columns_by_table: Mapping[str, frozenset[str]]
    functions: frozenset[str]
    index_constraint_names: frozenset[str]
    rls_by_table: Mapping[str, bool]
    private_functions: frozenset[str] = frozenset()

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "SchemaIntrospection":
        """Build a snapshot from the ``runtime_schema_introspection`` RPC payload."""
        if not isinstance(payload, Mapping):
            raise SchemaIntrospectionError("introspection payload must be an object")

        def _str_set(key: str) -> frozenset[str]:
            raw = payload.get(key) or []
            if not isinstance(raw, (list, tuple)):
                raise SchemaIntrospectionError(f"'{key}' must be a list")
            return frozenset(str(item) for item in raw if item is not None)

        columns_raw = payload.get("columns") or {}
        if not isinstance(columns_raw, Mapping):
            raise SchemaIntrospectionError("'columns' must be an object")
        columns_by_table: dict[str, frozenset[str]] = {}
        for table, cols in columns_raw.items():
            if cols is not None and not isinstance(cols, (list, tuple)):
                raise SchemaIntrospectionError(f"columns for table '{table}' must be a list")
            columns_by_table[str(table)] = frozenset(
                str(c) for c in (cols or []) if c is not None
            )

        rls_raw = payload.get("rls") or {}
        if not isinstance(rls_raw, Mapping):
            raise SchemaIntrospectionError("'rls' must be an object")
        rls_by_table = {str(table): bool(enabled) for table, enabled in rls_raw.items()}

        # Indexes and constraints are unioned: a requirement may be served by
        # either an index or a uniqueness constraint of the same logical intent.
        index_constraint_names = _str_set("indexes") | _str_set("constraints")

        return cls(
            tables=_str_set("tables"),
            columns_by_table=columns_by_table,
            functions=_str_set("functions"),
            index_constraint_names=index_constraint_names,
            rls_by_table=rls_by_table,
            private_functions=_str_set("private_functions"),
        )


@dataclass
class SchemaCheckResult:
    """Outcome of comparing requirements against an introspection snapshot."""

    missing_tables: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    missing_functions: list[str] = field(default_factory=list)
    missing_indexes: list[str] = field(default_factory=list)
    rls_issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.missing_tables
            or self.missing_columns
            or self.missing_functions
            or self.missing_indexes
            or self.rls_issues
        )

    def format_report(self) -> str:
        if self.ok:
            return "✅ Supabase runtime schema check passed."

        lines = ["❌ Supabase runtime schema check failed."]
        sections = (
            ("Missing tables:", self.missing_tables),
            ("Missing columns:", self.missing_columns),
            ("Missing functions:", self.missing_functions),
            ("Missing indexes/constraints:", self.missing_indexes),
            ("RLS issues:", self.rls_issues),
        )
        for header, items in sections:
            if not items:
                continue
            lines.append(header)
            lines.extend(f"- {item}" for item in items)
        return "\n".join(lines)


# --- individual diff helpers (small + unit-testable) -----------------------


def find_missing_tables(present_tables: Iterable[str]) -> list[str]:
    present = set(present_tables)
    return [table for table in REQUIRED_TABLES if table not in present]


def find_missing_columns(columns_by_table: Mapping[str, Iterable[str]]) -> list[str]:
    """Return ``table.column`` entries for every required-but-absent column.

    A required table that is entirely absent reports each of its columns as
    missing so the operator gets a complete picture in a single run.
    """
    missing: list[str] = []
    for table, required in REQUIRED_COLUMNS.items():
        present = set(columns_by_table.get(table, ()) or ())
        missing.extend(f"{table}.{column}" for column in required if column not in present)
    return missing


def find_missing_functions(
    present_functions: Iterable[str],
    private_functions: Iterable[str] = (),
) -> list[str]:
    present_by_schema = {
        "public": set(present_functions),
        "private": set(private_functions),
    }
    missing: list[str] = []
    for qualified in REQUIRED_FUNCTIONS:
        schema, bare = qualified.split(".", 1)
        if bare not in present_by_schema.get(schema, set()):
            missing.append(qualified)
    return missing


def find_missing_index_constraints(present_names: Iterable[str]) -> list[str]:
    present = set(present_names)
    return [req.label for req in INDEX_REQUIREMENTS if not req.is_satisfied(present)]


def find_rls_issues(rls_by_table: Mapping[str, bool]) -> list[str]:
    issues: list[str] = []
    for table in RLS_REQUIRED_TABLES:
        if not rls_by_table.get(table, False):
            issues.append(f"{table} RLS is not enabled")
    return issues


def evaluate_schema(introspection: SchemaIntrospection) -> SchemaCheckResult:
    """Compare a normalized snapshot against all requirements."""
    return SchemaCheckResult(
        missing_tables=find_missing_tables(introspection.tables),
        missing_columns=find_missing_columns(introspection.columns_by_table),
        missing_functions=find_missing_functions(
            introspection.functions, introspection.private_functions
        ),
        missing_indexes=find_missing_index_constraints(introspection.index_constraint_names),
        rls_issues=find_rls_issues(introspection.rls_by_table),
    )


def evaluate_payload(payload: Mapping[str, object]) -> SchemaCheckResult:
    """Convenience: parse a raw RPC payload and evaluate it in one step."""
    return evaluate_schema(SchemaIntrospection.from_payload(payload))


__all__: Sequence[str] = (
    "INTAKES_TABLE",
    "REQUIRED_TABLES",
    "PLAN_RUNTIME_REQUIRED_COLUMNS",
    "REQUIRED_PLANS_COLUMNS",
    "GENERATION_JOB_STAGE2_COST_COLUMNS",
    "REQUIRED_GENERATION_JOBS_COLUMNS",
    "REQUIRED_PROFILES_COLUMNS",
    "REQUIRED_COLUMNS",
    "REQUIRED_FUNCTIONS",
    "IndexRequirement",
    "INDEX_REQUIREMENTS",
    "RLS_REQUIRED_TABLES",
    "SchemaIntrospection",
    "SchemaIntrospectionError",
    "SchemaCheckResult",
    "find_missing_tables",
    "find_missing_columns",
    "find_missing_functions",
    "find_missing_index_constraints",
    "find_rls_issues",
    "evaluate_schema",
    "evaluate_payload",
)
