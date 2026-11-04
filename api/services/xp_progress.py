"""Read-only athlete XP progress, opportunities and milestone presentation.

The interface must never invent earning opportunities in the browser. This
module derives them from persisted profile/intake/plan/check-in/completion state
and the same Today/week contracts that own the rest of the product.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from api.contracts.command_view import session_is_today
from api.services.today_command_cache import recent_today_command
from api.services.today_readiness_boundary import build_today_command_view
from api.services.today_service import resolve_training_day
from api.services.week_progress import evaluate_week_completion, find_week_for_training_day
from api.services.streaks import get_streak_state
from api.services.xp_awards import profile_activation_complete
from api.store import AppStore

logger = logging.getLogger(__name__)

RECENT_AWARDS_LIMIT = 20
MILESTONES_LIMIT = 50
COMPLETED_SESSION_STATUSES = frozenset({"done", "modified"})
RecordReadStatus = Literal["found", "not_found", "unavailable"]


@dataclass(frozen=True)
class _RecordRead:
    """Result of an optional-record read without collapsing failure into absence."""

    status: RecordReadStatus
    value: Mapping[str, Any] | None = None


def _rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [dict(row) for row in value if isinstance(row, Mapping)]


def _read_xp_state(store: AppStore, athlete_id: str) -> dict[str, Any]:
    state = store.get_xp_progress_state(athlete_id, limit=RECENT_AWARDS_LIMIT)
    if not isinstance(state, Mapping):
        raise RuntimeError("XP progress store returned no state")
    return {
        "total_xp": max(0, int(state.get("total_xp") or 0)),
        "last_daily_login_date": state.get("last_daily_login_date"),
        "recent_awards": _rows(state.get("recent_awards")),
    }


def _list_milestones(store: AppStore, athlete_id: str) -> list[dict[str, Any]]:
    return _rows(store.list_plan_milestones(athlete_id, limit=MILESTONES_LIMIT))


def _award_exists(
    store: AppStore,
    athlete_id: str,
    *,
    action: str | None = None,
    idempotency_key: str | None = None,
    calendar_date: str | None = None,
) -> bool:
    return bool(
        store.xp_award_exists(
            athlete_id,
            action=action,
            idempotency_key=idempotency_key,
            calendar_date=calendar_date,
        )
    )


def _optional_record_read(row: object, *, source: str, athlete_id: str) -> _RecordRead:
    if row is None:
        return _RecordRead(status="not_found")
    if isinstance(row, Mapping):
        return _RecordRead(status="found", value=dict(row))
    logger.error(
        "[xp] malformed %s progress read athlete_id=%s type=%s",
        source,
        athlete_id,
        type(row).__name__,
    )
    return _RecordRead(status="unavailable")


def _safe_latest_intake(store: AppStore, athlete_id: str) -> _RecordRead:
    try:
        return _optional_record_read(
            store.get_latest_intake(athlete_id),
            source="intake",
            athlete_id=athlete_id,
        )
    except Exception:  # noqa: BLE001 - activation opportunities fail closed
        logger.exception("[xp] intake progress read failed athlete_id=%s", athlete_id)
        return _RecordRead(status="unavailable")


def _safe_latest_plan(store: AppStore, athlete_id: str) -> _RecordRead:
    try:
        return _optional_record_read(
            store.get_latest_plan_status(athlete_id),
            source="plan",
            athlete_id=athlete_id,
        )
    except Exception:  # noqa: BLE001 - activation opportunities fail closed
        logger.exception("[xp] plan progress read failed athlete_id=%s", athlete_id)
        return _RecordRead(status="unavailable")


def _today_command(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
) -> object | None:
    recent = recent_today_command(
        store,
        athlete_id=athlete_id,
        athlete_timezone=athlete_timezone,
    )
    if recent is not None:
        return recent
    try:
        return build_today_command_view(
            store,
            athlete_id=athlete_id,
            athlete_timezone=athlete_timezone,
        )
    except Exception:  # noqa: BLE001 - opportunities fail closed
        logger.exception("[xp] Today progress read failed athlete_id=%s", athlete_id)
        return None


def _active_plan(
    store: AppStore,
    *,
    athlete_id: str,
    command: object | None,
) -> Mapping[str, Any] | None:
    command_plan = getattr(command, "active_plan", None)
    plan_id = (
        str(command_plan.get("id") or "").strip()
        if isinstance(command_plan, Mapping)
        else ""
    )
    if not plan_id:
        return None
    try:
        # Progress only reads the calendar, never Stage-2 blobs.
        row = store.get_training_plan_for_athlete(plan_id, athlete_id)
        return row if isinstance(row, Mapping) else None
    except Exception:  # noqa: BLE001 - progress fails closed
        logger.exception(
            "[xp] active plan progress read failed athlete_id=%s plan_id=%s",
            athlete_id,
            plan_id,
        )
        return None


def _current_week(
    store: AppStore,
    *,
    athlete_id: str,
    plan: Mapping[str, Any] | None,
    training_day: str,
) -> dict[str, Any] | None:
    if not plan:
        return None
    week = find_week_for_training_day(plan, training_day)
    if not isinstance(week, Mapping):
        return None
    plan_id = str(plan.get("id") or "").strip()
    week_id = str(week.get("week_id") or "").strip()
    start_date = str(week.get("start_date") or "").strip()
    end_date = str(week.get("end_date") or "").strip()
    if not plan_id or not week_id or not start_date or not end_date:
        return None
    try:
        completions = store.list_plan_session_completions(
            athlete_id,
            plan_id,
            limit=500,
        )
    except Exception:  # noqa: BLE001 - progress fails closed
        logger.exception(
            "[xp] week completion read failed athlete_id=%s plan_id=%s",
            athlete_id,
            plan_id,
        )
        return None
    relevant = [
        row
        for row in _rows(completions)
        if start_date <= str(row.get("training_day") or "") <= end_date
    ]
    evaluation = evaluate_week_completion(week=week, completions=relevant)
    planned = max(0, int(evaluation.get("planned") or 0))
    skipped = len(evaluation.get("skipped_session_ids") or [])
    resolved = max(0, int(evaluation.get("resolved") or 0))
    completed = max(0, min(planned, resolved - skipped))
    week_xp_earned = _award_exists(
        store,
        athlete_id,
        action="full_training_week_completed",
        calendar_date=start_date,
    )
    return {
        "plan_id": plan_id,
        "week_id": week_id,
        "week_index": week.get("week_index"),
        "phase_label": str(week.get("phase_label") or "").strip(),
        "start_date": start_date,
        "end_date": end_date,
        "completed_sessions": completed,
        "planned_sessions": planned,
        "remaining_sessions": max(0, planned - completed),
        "complete": bool(evaluation.get("complete")),
        "week_xp_earned": week_xp_earned,
    }


def _milestone_view(row: Mapping[str, Any]) -> dict[str, Any] | None:
    milestone_type = str(row.get("milestone_type") or "").strip()
    if milestone_type == "phase_completed":
        phase_label = str(row.get("phase_label") or "Training").strip()
        display_label = f"{phase_label} phase complete"
    elif milestone_type == "plan_completed":
        display_label = "Plan complete"
    elif milestone_type == "camp_completed":
        display_label = "Fight camp complete"
    else:
        return None
    return {
        "id": str(row.get("id") or "").strip(),
        "plan_id": str(row.get("plan_id") or "").strip(),
        "milestone_type": milestone_type,
        "phase_label": row.get("phase_label"),
        "completed_at": row.get("completed_at"),
        "display_label": display_label,
    }


def _opportunity(
    *,
    code: str,
    label: str,
    xp: int,
    href: str,
    priority: int,
) -> dict[str, Any]:
    return {
        "code": code,
        "label": label,
        "xp": xp,
        "href": href,
        "priority": priority,
    }


def _opportunities(
    store: AppStore,
    *,
    athlete_id: str,
    profile: object,
    latest_intake: _RecordRead,
    latest_plan: _RecordRead,
    command: object | None,
    current_week: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    choices: list[dict[str, Any]] = []

    if (
        not _award_exists(store, athlete_id, action="profile_completed")
        and not profile_activation_complete(profile)
    ):
        choices.append(
            _opportunity(
                code="complete_profile",
                label="Complete your athlete profile",
                xp=25,
                href="/settings",
                priority=50,
            )
        )
    if (
        latest_intake.status == "not_found"
        and not _award_exists(store, athlete_id, action="first_intake_completed")
    ):
        choices.append(
            _opportunity(
                code="complete_first_intake",
                label="Complete Camp Setup",
                xp=50,
                href="/onboarding",
                priority=60,
            )
        )
    if (
        latest_plan.status == "not_found"
        and not _award_exists(store, athlete_id, action="first_plan_ready")
    ):
        choices.append(
            _opportunity(
                code="build_first_plan",
                label="Build your first training plan",
                xp=100,
                href="/onboarding",
                priority=70,
            )
        )

    today = getattr(command, "today", None)
    training_day = str(getattr(today, "training_day", "") or "").strip()
    recommendation_state = str(
        getattr(today, "recommendation_state", "") or ""
    ).strip()
    if recommendation_state == "not_checked_in":
        first_checkin_earned = _award_exists(
            store,
            athlete_id,
            action="first_checkin_completed",
        )
        choices.append(
            _opportunity(
                code="complete_today_checkin",
                label="Complete today's check-in",
                xp=10 if first_checkin_earned else 35,
                href="/today",
                priority=10,
            )
        )

    open_injuries = getattr(command, "open_injuries", None)
    if (
        training_day
        and isinstance(open_injuries, Sequence)
        and any(isinstance(injury, Mapping) for injury in open_injuries)
        and not _award_exists(
            store,
            athlete_id,
            action="injury_update_completed",
            calendar_date=training_day,
        )
    ):
        choices.append(
            _opportunity(
                code="update_active_injury",
                label="Update today's active injury",
                xp=10,
                href="/today",
                priority=20,
            )
        )

    next_session = getattr(today, "next_session", None)
    completion_status = str(getattr(today, "completion_status", "") or "")
    decision_tier = str(getattr(today, "decision_tier", "") or "")
    # Session timing comes from the command view, never from a second reading of
    # the plan here — the XP "next action" and Today must name the same session.
    if (
        session_is_today(command)
        and isinstance(next_session, Mapping)
        and bool(next_session)
        and completion_status not in COMPLETED_SESSION_STATUSES
        and decision_tier != "stop"
    ):
        choices.append(
            _opportunity(
                code="complete_today_session",
                label=(
                    "Complete today's modified session"
                    if decision_tier == "modify"
                    else "Complete today's session"
                ),
                xp=75,
                href="/today",
                priority=30,
            )
        )

    if (
        current_week
        and int(current_week.get("planned_sessions") or 0) > 0
        and not bool(current_week.get("complete"))
        and not bool(current_week.get("week_xp_earned"))
    ):
        choices.append(
            _opportunity(
                code="complete_training_week",
                label="Complete this training week",
                xp=100,
                href="/progress",
                priority=40,
            )
        )

    choices.sort(key=lambda item: (int(item["priority"]), str(item["code"])))
    return choices[:3]


def build_xp_progress(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
    profile: object,
) -> dict[str, Any]:
    """Build the athlete-scoped, read-only XP and streak payload."""

    state = _read_xp_state(store, athlete_id)
    latest_intake = _safe_latest_intake(store, athlete_id)
    latest_plan = _safe_latest_plan(store, athlete_id)
    command = _today_command(
        store,
        athlete_id=athlete_id,
        athlete_timezone=athlete_timezone,
    )
    training_day = str(
        getattr(getattr(command, "today", None), "training_day", "")
        or resolve_training_day(athlete_timezone)
    )
    plan = _active_plan(store, athlete_id=athlete_id, command=command)
    current_week = _current_week(
        store,
        athlete_id=athlete_id,
        plan=plan,
        training_day=training_day,
    )
    milestones = [
        view
        for row in _list_milestones(store, athlete_id)
        if (view := _milestone_view(row)) is not None
    ]
    opportunities = _opportunities(
        store,
        athlete_id=athlete_id,
        profile=profile,
        latest_intake=latest_intake,
        latest_plan=latest_plan,
        command=command,
        current_week=current_week,
    )
    return {
        "state": state,
        "streaks": get_streak_state(
            store,
            athlete_id=athlete_id,
            athlete_timezone=athlete_timezone,
        ),
        "opportunities": opportunities,
        "current_week": current_week,
        "major_milestones": milestones,
    }


__all__ = ["build_xp_progress"]
