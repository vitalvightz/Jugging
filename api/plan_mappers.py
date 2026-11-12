"""Profile/plan response mappers extracted from api.app (PR2: pure helpers)."""
from __future__ import annotations

import copy
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from fastapi import HTTPException, status

from fightcamp.stage2_policy import (
    admin_review_blocking_findings,
    apply_stage2_release_policy,
)
from fightcamp.sparring_advisories import build_plan_advisories
from fightcamp.weekly_schedule_view import extract_weekly_schedule

from .compliance import evaluate_profile_compliance
from .contracts.training_day import current_training_day

from .models import (
    AdminIntakeInjuries,
    PROFILE_REFRESH_FAILED_WHY_LOG_KEY,
    AdminAthleteRecord,
    AdminPlanOutputs,
    AdminPlanSummary,
    MeResponse,
    PlanDetail,
    PlanOutputs,
    PlanScheduleContext,
    PlanSafetyState,
    PlanSummary,
    ProfileRecord,
    RehabLabelPolicy,
    StructuredCardState,
    USERNAME_CHANGE_WINDOW_DAYS,
    USERNAME_MAX_CHANGES_PER_WINDOW,
    UsernameRateLimitInfo,
    WeeklySchedule,
)
from .store import AppStore, is_effective_admin_profile
from .structured_card_lifecycle import (
    STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY,
    STRUCTURED_CARD_BUILD_STALE_AFTER,
    parse_structured_card_attempt_started_at,
)
from .exercise_identity import reconcile_exercise_keys
from .structured_plan_models import StructuredTrainingPlan, safe_parse_structured_plan
from .structured_plan_calendar_spine import reconcile_calendar_spine
from .structured_plan_sparring_reconcile import surface_coach_led_contact
from .structured_plan_generation import (
    reconcile_late_fight_week_context,
    reconcile_rehab_drill_ids,
)
from .structured_plan_locked_merge import merge_planner_owned_structured_content
from .services.effective_structured_plan import resolve_effective_structured_plan
from .services.onboarding_metrics import time_to_first_usable_plan_seconds
from .services.exercise_overrides import apply_exercise_overrides
from .services.open_plan_timeline import project_open_structured_plan
from .services.active_plan import get_plan_activation_state

logger = logging.getLogger(__name__)

_CLEAN_STRUCTURED_CARD_STATUSES = frozenset({"valid", "repair_attempted_valid"})
_FAILED_STRUCTURED_CARD_STATUSES = frozenset(
    {"invalid_fallback_used", "blocked_by_safety_audit"}
)
_STALE_STRUCTURED_CARD_REASON = "Enhanced card build did not complete."
_MISSING_SAVED_CARD_REASON = "Saved enhanced card is unavailable."

_REVIEW_CODE_LABELS = {
    "calendar_spine_fight_day_protocol_violation": "fight-day protocol timing needs review",
    "equipment_incongruent_selection": "selected equipment does not match the programmed work",
    "late_camp_session_incomplete": "late-camp session detail is incomplete",
    "missing_required_element": "required plan elements are missing",
    "restriction_violation": "training restrictions were violated",
    "true_internal_system_leak": "blocked release content was detected",
}


def _build_me_response(profile: ProfileRecord, store: AppStore) -> MeResponse:
    latest_intake = store.get_latest_intake(profile.athlete_id)
    plans = _visible_plans_for_athlete(store.list_user_plans(profile.athlete_id))
    training_day = current_training_day(athlete_timezone=profile.athlete_timezone)
    latest_plan = _map_plan_summary(plans[0], current_training_day=training_day) if plans else None
    return MeResponse(
        profile=profile,
        effective_admin=is_effective_admin_profile(profile, store),
        latest_intake=latest_intake.get("intake") if latest_intake else None,
        latest_plan=latest_plan,
        plan_count=len(plans),
        username_rate_limit=_username_rate_limit_info(profile.username_change_history),
    )


def _decode_structured_text(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            return {"raw": stripped}
        return decoded if isinstance(decoded, dict) else {"raw": decoded}
    return {"raw": value}


def _map_profile_row(row: dict[str, Any]) -> ProfileRecord:
    raw_username = row.get("username")
    history_raw = row.get("username_change_history") or []
    username_history: list[str] = [str(entry) for entry in history_raw if entry]
    # Derived here, on every read, from the stored date of birth and consent
    # timestamps. Nothing downstream re-derives an age band or a consent verdict
    # from client input, so this is the single place the answer comes from.
    compliance = evaluate_profile_compliance(row)
    return ProfileRecord(
        starting_path=row.get("starting_path", "plan_first"),
        training_habits=row.get("training_habits") or {},
        training_habits_revision=row.get("training_habits_revision") or 0,
        athlete_id=str(row["id"]),
        email=str(row.get("email") or ""),
        username=str(raw_username) if raw_username else None,
        username_change_history=username_history,
        role=str(row.get("role") or "athlete"),
        access_status=str(row.get("access_status") or "approved"),
        full_name=str(row.get("full_name") or ""),
        technical_style=list(row.get("technical_style") or []),
        tactical_style=list(row.get("tactical_style") or []),
        stance=str(row.get("stance") or ""),
        professional_status=str(row.get("professional_status") or ""),
        record=str(row.get("record_summary") or ""),
        athlete_timezone=str(row.get("athlete_timezone") or ""),
        athlete_locale=str(row.get("athlete_locale") or ""),
        appearance_mode=str(row.get("appearance_mode") or "dark"),
        onboarding_draft=row.get("onboarding_draft"),
        avatar_url=row.get("avatar_url") or None,
        nutrition_profile=row.get("nutrition_profile") or {},
        private_trial_ack_at=str(row.get("private_trial_ack_at") or "") or None,
        date_of_birth=compliance.date_of_birth,
        age_band=compliance.age_band,
        is_minor=compliance.is_minor,
        meets_minimum_age=compliance.meets_minimum_age,
        terms_version=str(row.get("terms_version") or "") or None,
        terms_accepted_at=str(row.get("terms_accepted_at") or "") or None,
        terms_accepted=compliance.terms_accepted,
        health_consent_version=str(row.get("health_consent_version") or "") or None,
        health_data_consent=row.get("health_data_consent") is True,
        health_consent_at=str(row.get("health_consent_at") or "") or None,
        health_consent_withdrawn_at=str(row.get("health_consent_withdrawn_at") or "") or None,
        health_consent_granted=compliance.health_consent_granted,
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
    )


def _username_rate_limit_info(history: list[str]) -> UsernameRateLimitInfo:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=USERNAME_CHANGE_WINDOW_DAYS)
    recent: list[datetime] = []
    for entry in history:
        try:
            parsed = datetime.fromisoformat(str(entry).replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if parsed >= cutoff:
            recent.append(parsed)
    remaining = max(0, USERNAME_MAX_CHANGES_PER_WINDOW - len(recent))
    next_available_at: str | None = None
    if remaining == 0 and recent:
        next_available_at = (min(recent) + timedelta(days=USERNAME_CHANGE_WINDOW_DAYS)).isoformat()
    return UsernameRateLimitInfo(
        remaining=remaining,
        next_available_at=next_available_at,
    )


def _review_finding_label(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    code = str(item.get("code") or "").strip()
    if not code:
        return None
    return _REVIEW_CODE_LABELS.get(code) or code.replace("_", " ")


def _review_finding_labels(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    labels: list[str] = []
    seen: set[str] = set()
    for item in items:
        label = _review_finding_label(item)
        if not label or label in seen:
            continue
        seen.add(label)
        labels.append(label)
    return labels


def _finding_list(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _format_review_reason(report: dict[str, Any], *, normalized_status: str) -> str | None:
    if normalized_status != "held_for_review":
        return None
    if not report:
        return (
            "Admin review is required before release because validation did not "
            "return a publishable report."
        )

    error_labels = _review_finding_labels(report.get("errors"))
    if error_labels:
        return (
            "Admin review is required before release because Stage 2 validation "
            f"found errors: {', '.join(error_labels[:3])}."
        )

    blocking_labels = _review_finding_labels(
        [*_finding_list(report.get("blocking_warnings")), *admin_review_blocking_findings(report)]
    )
    if blocking_labels:
        return (
            "Admin review is required before release because Stage 2 validation "
            f"found blocking issues: {', '.join(blocking_labels[:3])}."
        )

    return "Admin review is required before this plan can be released to the athlete."


def _map_plan_summary(
    row: dict[str, Any],
    *,
    current_training_day: date | str | None = None,
) -> PlanSummary:
    raw_status = str(row.get("status") or "generated")
    normalized_status = raw_status
    report = row.get("stage2_validator_report") if isinstance(row.get("stage2_validator_report"), dict) else {}
    if raw_status == "review_required":
        report_exists = bool(report)
        if not report_exists:
            normalized_status = "held_for_review"
        else:
            policy_report = apply_stage2_release_policy(report)
            has_errors = bool(policy_report.get("errors"))
            has_blocking = policy_report.get("release_decision") == "hold"
            # A malformed report cannot clear a plan that was already held. The
            # release policy is observational at generation time, but this is a
            # stored review_required decision being re-derived: if the report is
            # unreadable we cannot show that the hold is resolved, so it stands.
            has_unreadable_report = bool(policy_report.get("release_policy_malformed_fields"))
            has_review_flags = bool(report.get("warnings") or report.get("review_flags"))
            if has_errors or has_blocking or has_unreadable_report:
                normalized_status = "held_for_review"
            elif has_review_flags:
                normalized_status = "publishable_with_flags"
            else:
                normalized_status = "ready"
    custom = report.get("custom_plan")
    return PlanSummary(
        custom_plan_id=custom.get("id") if isinstance(custom, dict) else None,
        plan_id=str(row["id"]),
        plan_name=(str(row["plan_name"]).strip() if row.get("plan_name") is not None else None) or None,
        athlete_id=str(row["athlete_id"]),
        full_name=str(row.get("full_name") or ""),
        fight_date=str(row.get("fight_date") or ""),
        technical_style=list(row.get("technical_style") or []),
        created_at=str(row.get("created_at") or ""),
        status=normalized_status,
        activation_state=get_plan_activation_state(
            row,
            current_training_day=current_training_day,
        ),
        pdf_url=row.get("pdf_url"),
        review_reason=_format_review_reason(report, normalized_status=normalized_status),
    )


def _is_archived_plan(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    return str(row.get("status") or "").strip().lower() == "archived"


def _is_admin_archived_hidden_from_athlete(row: dict[str, Any] | None) -> bool:
    """Archived AND explicitly hidden by an admin (why_log marker).

    A plain athlete-archived plan stays readable as a history preview; only the
    admin bulk-archive path stamps this marker to remove it from the athlete
    view entirely.
    """
    if not _is_archived_plan(row):
        return False
    why_log = row.get("why_log") if isinstance(row.get("why_log"), dict) else {}
    return bool(why_log.get("admin_archived_hidden_from_athlete"))


def _is_triage_blocked_plan(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    return str(row.get("status") or "").strip().lower() == "triage_blocked"


def _visible_plans_for_athlete(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Triage-blocked outcomes are screening decisions, not plans — they must
    # not surface in the athlete's archive or "latest plan" snapshot. Admin
    # endpoints bypass this filter so the ops team can still review and
    # approve-and-resume blocked attempts.
    return [
        row
        for row in rows
        if not _is_archived_plan(row) and not _is_triage_blocked_plan(row)
    ]


def _admin_draft_text(row: dict[str, Any]) -> str:
    return str(row.get("draft_plan_text") or row.get("plan_text") or "")


def _admin_final_text(row: dict[str, Any]) -> str:
    return str(row.get("final_plan_text") or row.get("plan_text") or "")


def _map_plan_safety_state(row: dict[str, Any]) -> PlanSafetyState:
    triage = {}
    why_log = row.get("why_log")
    if isinstance(why_log, dict):
        triage = why_log.get("injury_triage") or {}
    if not isinstance(triage, dict):
        triage = {}

    mode = str(triage.get("mode") or "")
    triage_blocked = str(row.get("status") or "").strip().lower() == "triage_blocked"
    stage2_was_skipped = bool(triage.get("should_block_stage2")) or triage_blocked
    if mode == "medical_hold":
        return PlanSafetyState(
            state="medical_hold",
            status_chip="MEDICAL HOLD",
            header="Medical hold: no training plan generated",
            subtext=(
                "Urgent neurological or medical red-flag signals were detected. "
                "Planning was intentionally blocked before normal generation."
            ),
            stage2_skipped=stage2_was_skipped,
            clinician_clearance_required=bool(triage.get("clinician_clearance_required", True)),
            matched_high_risk_categories=list(triage.get("matched_high_risk_categories") or []),
            red_flags=list(triage.get("red_flags") or []),
            sparring_risk_band=triage.get("sparring_risk_band"),
            next_steps=[
                "Seek appropriate medical review before training guidance.",
                "Update the intake after clearance.",
                "Regenerate only when medically cleared.",
            ],
        )
    if mode == "restricted_rehab_only":
        return PlanSafetyState(
            state="restricted_rehab_only",
            status_chip="RESTRICTED REHAB ONLY",
            header="Planning paused: clinician clearance required",
            subtext=(
                "Serious structural injury signals were detected. "
                "Normal fight-camp generation is paused to avoid unsafe loading recommendations."
            ),
            stage2_skipped=stage2_was_skipped,
            clinician_clearance_required=bool(triage.get("clinician_clearance_required", True)),
            matched_high_risk_categories=list(triage.get("matched_high_risk_categories") or []),
            red_flags=list(triage.get("red_flags") or []),
            sparring_risk_band=triage.get("sparring_risk_band"),
            next_steps=[
                "Review injury details and current restrictions.",
                "Update the intake after clinician clearance.",
                "Regenerate normal planning only when safe.",
            ],
        )
    if mode == "needs_review":
        return PlanSafetyState(
            state="needs_review",
            status_chip="NEEDS REVIEW",
            header="Safety review required before planning",
            subtext=(
                "Guided injury severity/trend combinations triggered a conservative safety gate. "
                "Automatic planning is paused pending coach/admin review."
            ),
            stage2_skipped=stage2_was_skipped,
            clinician_clearance_required=bool(triage.get("clinician_clearance_required", False)),
            matched_high_risk_categories=list(triage.get("matched_high_risk_categories") or []),
            red_flags=list(triage.get("red_flags") or []),
            sparring_risk_band=triage.get("sparring_risk_band"),
            next_steps=[
                "Review guided injury severity/trend details.",
                "Clarify diagnosis progression and restrictions.",
                "Approve before rerunning full planning.",
            ],
        )

    return PlanSafetyState(
        state="plan_ready",
        status_chip="PLAN READY",
        header="Plan ready",
        subtext="Normal planning completed.",
        stage2_skipped=False,
        clinician_clearance_required=False,
        matched_high_risk_categories=[],
        red_flags=[],
        sparring_risk_band=None,
        next_steps=[],
    )


_ALLOWED_PLAN_SOURCES: frozenset[str] = frozenset({"quick_build", "self_serve"})


def _lookup_plan_source(store: AppStore, plan_id: str) -> str | None:
    job = store.get_generation_job_by_plan_id(plan_id)
    if not isinstance(job, dict):
        return None
    raw = job.get("source")
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    return value if value in _ALLOWED_PLAN_SOURCES else None


def _structured_card_reason_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("message", "reason", "detail", "error", "code"):
            text = _structured_card_reason_text(value.get(key))
            if text:
                return text
        try:
            return json.dumps(value, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return str(value)
    text = str(value).strip()
    return text or None


def _structured_card_reason_list(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple)) else [value]
    reasons: list[str] = []
    for item in values:
        text = _structured_card_reason_text(item)
        if text and text not in reasons:
            reasons.append(text)
    return reasons


def _merge_structured_card_reasons(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for reason in group:
            if reason not in merged:
                merged.append(reason)
    return merged


def _derive_structured_card_state(
    row: dict[str, Any],
    *,
    structured_plan: StructuredTrainingPlan | None,
    structured_schema_version: str | None,
    now: datetime | None = None,
) -> StructuredCardState:
    report = row.get("stage2_validator_report")
    report = report if isinstance(report, dict) else {}
    debug = report.get("structured_plan")
    debug = debug if isinstance(debug, dict) else {}
    debug_status = str(debug.get("status") or "").strip()

    error_reasons = _structured_card_reason_list(debug.get("errors"))
    warning_reasons = _structured_card_reason_list(debug.get("warnings"))
    recorded_reasons = _structured_card_reason_list(debug.get("reason"))
    reasons = _merge_structured_card_reasons(
        error_reasons,
        warning_reasons,
        recorded_reasons,
    )

    raw_schema_version = structured_schema_version or debug.get("schema_version")
    schema_version = (
        str(raw_schema_version).strip() if raw_schema_version is not None else None
    ) or None

    raw_attempt_started_at = report.get(STRUCTURED_CARD_ATTEMPT_STARTED_AT_KEY)
    attempt_at = parse_structured_card_attempt_started_at(raw_attempt_started_at)
    attempt_started_at: str | None = None
    if attempt_at is not None:
        attempt_started_at = (
            raw_attempt_started_at.strip()
            if isinstance(raw_attempt_started_at, str)
            else attempt_at.isoformat()
        )

    if structured_plan is not None and debug_status in _CLEAN_STRUCTURED_CARD_STATUSES:
        return StructuredCardState(
            state="live",
            reasons=reasons,
            schema_version=schema_version,
            attempt_started_at=attempt_started_at,
        )

    if attempt_at is not None:
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        else:
            current_time = current_time.astimezone(timezone.utc)
        if current_time - attempt_at < STRUCTURED_CARD_BUILD_STALE_AFTER:
            return StructuredCardState(
                state="building",
                reasons=[],
                schema_version=schema_version,
                attempt_started_at=attempt_started_at,
            )
        stale_reasons = _merge_structured_card_reasons(
            [_STALE_STRUCTURED_CARD_REASON],
            reasons,
        )
        if debug_status in _CLEAN_STRUCTURED_CARD_STATUSES:
            stale_reasons = _merge_structured_card_reasons(
                stale_reasons,
                [_MISSING_SAVED_CARD_REASON],
            )
        return StructuredCardState(
            state="failed",
            reasons=stale_reasons,
            schema_version=schema_version,
            attempt_started_at=attempt_started_at,
        )

    if debug_status in _CLEAN_STRUCTURED_CARD_STATUSES:
        return StructuredCardState(
            state="failed",
            reasons=_merge_structured_card_reasons(
                [_MISSING_SAVED_CARD_REASON],
                reasons,
            ),
            schema_version=schema_version,
        )

    if debug_status in _FAILED_STRUCTURED_CARD_STATUSES:
        if not reasons:
            reasons = [
                "Enhanced card was blocked by the safety audit."
                if debug_status == "blocked_by_safety_audit"
                else "Enhanced card validation failed."
            ]
        return StructuredCardState(
            state="failed",
            reasons=reasons,
            schema_version=schema_version,
        )

    if debug_status == "not_attempted":
        return StructuredCardState(
            state="failed" if error_reasons else "not_attempted",
            reasons=reasons,
            schema_version=schema_version,
        )

    return StructuredCardState(
        state="none",
        reasons=reasons,
        schema_version=schema_version,
    )


def _map_plan_detail(
    row: dict[str, Any],
    *,
    include_admin: bool,
    plan_source: str | None = None,
    current_training_day: date | str | None = None,
    rehab_label_policy: RehabLabelPolicy | None = None,
    intake_injuries: AdminIntakeInjuries | None = None,
) -> PlanDetail:
    summary = _map_plan_summary(row, current_training_day=current_training_day)
    planning_brief = _decode_structured_text(row.get("planning_brief"))
    raw_stage2_payload = row.get("stage2_payload")
    # No fallback to stage2_payload["input_parsing_metadata"]: Stage 1 sets it and
    # the top-level parsing_metadata from the same plan_input.parsing_metadata in
    # the same result (fightcamp/main.py), so the fallback could only ever repeat
    # the value the column already holds — and when the column is falsy, so is it.
    # Rows predating the column predate stage2_payload too (both were added by
    # 20260427120000_stabilize_generation_runtime.sql, neither backfilled), so
    # there is no legacy era where the fallback supplied anything either.
    parsing_metadata = row.get("parsing_metadata") or {}
    display_plan_text = str(row.get("plan_text") or "")
    if not display_plan_text and summary.status == "archived" and not include_admin:
        display_plan_text = str(row.get("final_plan_text") or row.get("draft_plan_text") or "")
    is_legacy_review_required = str(row.get("status") or "").strip().lower() == "review_required"
    if (
        not display_plan_text
        and is_legacy_review_required
        and summary.status in {"ready", "publishable_with_flags"}
    ):
        display_plan_text = str(row.get("final_plan_text") or "")
    if not (planning_brief or {}).get("custom_plan"):
        from fightcamp.exercise_coaching_source import validate_source_coaching
        display_plan_text, _ = validate_source_coaching(planning_brief or {}, display_plan_text)
    effective_structured_plan = resolve_effective_structured_plan(
        row,
        raw_markdown=display_plan_text,
    )
    structured_plan, structured_schema_version = _decode_structured_plan(
        effective_structured_plan,
        raw_markdown=display_plan_text,
    )
    structured_payload = (
        structured_plan.model_dump(mode="json") if structured_plan is not None else {}
    )
    locked_payload = merge_planner_owned_structured_content(
        structured_payload,
        planning_brief,
    ).plan
    if structured_plan is not None and locked_payload != structured_payload:
        locked_result = safe_parse_structured_plan(
            locked_payload,
            raw_markdown=display_plan_text or None,
        )
        if locked_result.ok and locked_result.plan is not None:
            structured_plan = locked_result.plan
            structured_payload = locked_payload
    reconciled_payload = reconcile_exercise_keys(
        reconcile_rehab_drill_ids(
            reconcile_late_fight_week_context(
                structured_payload,
                planning_brief,
            ),
            planning_brief,
        ),
        planning_brief,
    )
    if structured_plan is not None and reconciled_payload != structured_payload:
        reconciled_result = safe_parse_structured_plan(
            reconciled_payload,
            raw_markdown=display_plan_text or None,
        )
        if reconciled_result.ok and reconciled_result.plan is not None:
            structured_plan = reconciled_result.plan
            structured_payload = reconciled_payload
    # Restore any no-session calendar days a dated camp dropped, so week
    # boundaries / counts / phases render from the authoritative countdown spine
    # rather than only the days that carried a session. A no-op (returns the same
    # object) for open plans, undated plans, and calendars already continuous.
    spine_payload = reconcile_calendar_spine(structured_payload, planning_brief)
    if structured_plan is not None and spine_payload is not structured_payload:
        spine_result = safe_parse_structured_plan(
            spine_payload,
            raw_markdown=display_plan_text or None,
        )
        if spine_result.ok and spine_result.plan is not None:
            structured_plan = spine_result.plan
            structured_payload = spine_payload
    # The spine can restore a scheduled session onto a contact day; the
    # contact must then live on coach_led_contact or the session hides it.
    if structured_plan is not None:
        contact_payload = copy.deepcopy(structured_payload)
        if surface_coach_led_contact(contact_payload, planning_brief):
            contact_result = safe_parse_structured_plan(
                contact_payload,
                raw_markdown=display_plan_text or None,
            )
            if contact_result.ok and contact_result.plan is not None:
                structured_plan = contact_result.plan
                structured_payload = contact_payload
    projected_payload, raw_schedule_context = project_open_structured_plan(
        row,
        structured_payload,
        current_training_day=current_training_day or datetime.now(timezone.utc).date(),
    )
    # The athlete's exercise changes, applied to dated days exactly as Today
    # applies them (api/services/exercise_overrides.py).
    projected_payload = apply_exercise_overrides(row, projected_payload) or projected_payload
    if (
        raw_schedule_context.get("schedule_mode") == "open_recurring"
        and raw_schedule_context.get("projection_status") == "unavailable"
    ):
        # An open plan that failed to project renders undated ("WEEK N · weekday")
        # in the Plan tab. The reason names the fail-closed guard that tripped so
        # the cause is one log line instead of a payload-shape trace.
        logger.warning(
            "open plan projection unavailable: plan_id=%s reason=%s",
            row.get("id"),
            raw_schedule_context.get("projection_reason"),
        )
    if structured_plan is not None and projected_payload != structured_payload:
        projected_result = safe_parse_structured_plan(
            projected_payload,
            raw_markdown=display_plan_text or None,
        )
        if projected_result.ok and projected_result.plan is not None:
            structured_plan = projected_result.plan
    report_dict = row.get("stage2_validator_report")
    report_dict = report_dict if isinstance(report_dict, dict) else {}
    structured_debug = report_dict.get("structured_plan")
    structured_debug = structured_debug if isinstance(structured_debug, dict) else {}
    return PlanDetail(
        **summary.model_dump(mode="json"),
        outputs=PlanOutputs(
            exercise_media=(planning_brief or {}).get("custom_plan", {}).get("exercise_media") or {},
            plan_text=display_plan_text,
            pdf_url=row.get("pdf_url"),
            structured_plan=structured_plan,
            schema_version=structured_schema_version,
        ),
        safety_state=_map_plan_safety_state(row),
        structured_card_state=_derive_structured_card_state(
            row,
            structured_plan=structured_plan,
            structured_schema_version=structured_schema_version,
        ),
        advisories=build_plan_advisories(planning_brief=planning_brief),
        plan_source="custom" if (planning_brief or {}).get("custom_plan") else plan_source,
        schedule_context=PlanScheduleContext(**raw_schedule_context),
        profile_refresh_failed=bool(
            (row.get("why_log") if isinstance(row.get("why_log"), dict) else {}).get(
                PROFILE_REFRESH_FAILED_WHY_LOG_KEY
            )
        ),
        rehab_label_policy=rehab_label_policy or RehabLabelPolicy(),
        admin_outputs=(
            AdminPlanOutputs(
                coach_notes=str(row.get("coach_notes") or ""),
                why_log=row.get("why_log") or {},
                intake_injuries=intake_injuries,
                planning_brief=planning_brief,
                stage2_payload=raw_stage2_payload,
                parsing_metadata=parsing_metadata if isinstance(parsing_metadata, dict) else {},
                stage2_handoff_text=str(row.get("stage2_handoff_text") or ""),
                draft_plan_text=_admin_draft_text(row),
                final_plan_text=_admin_final_text(row),
                stage2_retry_text=str(row.get("stage2_retry_text") or ""),
                stage2_validator_report=row.get("stage2_validator_report") or {},
                stage2_status=str(row.get("stage2_status") or "legacy"),
                stage2_attempt_count=int(row.get("stage2_attempt_count") or 0),
                structured_plan_status=str(structured_debug.get("status") or "not_attempted"),
                structured_plan_errors=[str(err) for err in (structured_debug.get("errors") or [])],
                structured_schema_version=(
                    structured_schema_version or structured_debug.get("schema_version")
                ),
            )
            if include_admin
            else None
        ),
    )


def _decode_structured_plan(
    value: Any,
    *,
    raw_markdown: str = "",
) -> tuple[StructuredTrainingPlan | None, str | None]:
    """Best-effort decode of a stored structured plan column.

    Legacy plans have no structured payload, so this returns ``(None, None)``
    for missing/blank values. Malformed structured data never breaks the
    response: it is dropped and the raw markdown fallback still flows through
    ``plan_text``.
    """

    decoded = _decode_structured_text(value)
    # `_decode_structured_text` wraps non-JSON strings as {"raw": ...}; that is
    # not a structured plan, so treat it (and empty values) as "no structured".
    if not decoded or (len(decoded) == 1 and "raw" in decoded):
        return None, None
    result = safe_parse_structured_plan(decoded, raw_markdown=raw_markdown or None)
    if not result.ok or result.plan is None:
        # The column carried a structured payload that no longer parses against
        # the schema. Surface it instead of silently dropping to the markdown
        # fallback, so a stored-card regression is visible rather than invisible.
        logger.warning(
            "structured_plan column failed to parse; falling back to markdown (errors=%s)",
            "; ".join(result.errors) if getattr(result, "errors", None) else "unknown",
        )
        return None, None
    return result.plan, result.plan.schema_version


def _map_weekly_schedule(row: dict[str, Any], *, week_index: int) -> WeeklySchedule:
    planning_brief = _decode_structured_text(row.get("planning_brief"))
    schedule = extract_weekly_schedule(
        planning_brief,
        week_index=week_index,
        fight_date=row.get("fight_date"),
    )
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="weekly schedule not found")
    return WeeklySchedule(plan_id=str(row["id"]), **schedule)


def _map_admin_plan_summary(row: dict[str, Any]) -> AdminPlanSummary:
    profile = row.get("profiles") or {}
    summary = _map_plan_summary(row)
    return AdminPlanSummary(
        **summary.model_dump(mode="json"),
        athlete_email=str(profile.get("email") or ""),
        profile_unavailable=bool(row.get("profile_enrichment_failed")),
    )


def _map_admin_athlete(row: dict[str, Any], latest_intake: dict[str, Any] | None = None) -> AdminAthleteRecord:
    onboarding_draft = row.get("onboarding_draft")
    return AdminAthleteRecord(
        athlete_id=str(row["id"]),
        email=str(row.get("email") or ""),
        role=str(row.get("role") or "athlete"),
        access_status=str(row.get("access_status") or "approved"),
        full_name=str(row.get("full_name") or ""),
        technical_style=list(row.get("technical_style") or []),
        tactical_style=list(row.get("tactical_style") or []),
        stance=str(row.get("stance") or ""),
        professional_status=str(row.get("professional_status") or ""),
        record=str(row.get("record") or row.get("record_summary") or ""),
        athlete_timezone=str(row.get("athlete_timezone") or ""),
        athlete_locale=str(row.get("athlete_locale") or ""),
        appearance_mode=str(row.get("appearance_mode") or "dark"),
        onboarding_draft=onboarding_draft if isinstance(onboarding_draft, dict) else None,
        latest_intake=latest_intake.get("intake") if isinstance(latest_intake, dict) else None,
        nutrition_profile=row.get("nutrition_profile") or {},
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
        plan_count=int(row.get("plan_count") or 0),
        latest_plan_created_at=row.get("latest_plan_created_at"),
        last_meaningful_activity_at=row.get("last_meaningful_activity_at"),
        first_usable_plan_at=row.get("first_usable_plan_at"),
        time_to_first_usable_plan_seconds=time_to_first_usable_plan_seconds(row),
    )


# What Home's fight countdown reads (web/lib/fight-countdown.ts through
# getWeeks/getDays/resolvedWeekPhase in web/lib/structured-plan.ts).
_CALENDAR_WEEK_FIELDS = (
    "week_id", "week_index", "start_date", "end_date", "phase_label", "countdown_start", "countdown_end",
)
_CALENDAR_DAY_FIELDS = ("date", "phase_label", "countdown_label")


def plan_calendar_spine(structured_plan: Mapping[str, Any]) -> dict[str, Any]:
    """The plan's weeks and days with only their calendar fields, in order."""
    weeks = []
    for week in structured_plan.get("weeks") or []:
        if not isinstance(week, Mapping):
            continue
        spine = {key: week[key] for key in _CALENDAR_WEEK_FIELDS if key in week}
        spine["days"] = [
            {key: day[key] for key in _CALENDAR_DAY_FIELDS if key in day}
            for day in (week.get("days") or [])
            if isinstance(day, Mapping)
        ]
        weeks.append(spine)
    return {"weeks": weeks}
