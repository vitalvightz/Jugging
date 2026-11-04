"""Authoritative full-training-week detection and XP award orchestration."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any

from api.services.open_plan_timeline import open_plan_anchor_date, open_plan_spec, project_open_structured_plan
from api.services.effective_structured_plan import resolve_effective_structured_plan
from api.services.progress_notifications import dispatch_progress_award_notification  # noqa: F401
from api.services.xp_awards import ensure_xp_abuse_hardening
from api.store import AppStore
from api.optional_sessions import is_optional_session
from api.contracts.completion import TERMINAL_COMPLETION_STATUSES as RESOLVED_STATUSES

logger = logging.getLogger(__name__)

COMPLETED_STATUSES = frozenset({"done", "modified"})
_TIMESTAMP_FIELDS = ("updated_at", "completed_at", "created_at")


def _parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_timestamp(row: Mapping[str, Any]) -> datetime | None:
    for field in _TIMESTAMP_FIELDS:
        parsed = _parse_timestamp(row.get(field))
        if parsed is not None:
            return parsed
    return None


def _mapping_rows(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _weeks(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    structured = resolve_effective_structured_plan(plan)
    if structured is None:
        return []
    return _mapping_rows(structured.get("weeks"))


def _plan_for_training_day(
    plan: Mapping[str, Any],
    training_day: str,
) -> Mapping[str, Any]:
    """Project renewable open-plan templates onto the requested calendar block.

    Dated plans read their resolved card unchanged. Projection is
    fail-closed: when the saved open template cannot be reconciled safely, the
    original undated plan remains in place and no week can be awarded.
    """

    structured = resolve_effective_structured_plan(plan)
    if structured is None:
        return plan
    projected, context = project_open_structured_plan(
        plan,
        structured,
        current_training_day=training_day,
    )
    projection_status = str(context.get("projection_status") or "")
    if projection_status == "projected":
        chosen = projected
    elif projection_status == "not_required":
        # A dated camp needs no projection, but it still reads the resolved
        # card: that card carries the derived session ids Today completes
        # against, and the raw row would never match those completions.
        chosen = structured
    else:
        return plan
    normalized = dict(plan)
    normalized["structured_plan"] = chosen
    return normalized


def _planned_session_ids(week: Mapping[str, Any]) -> set[str]:
    planned: set[str] = set()
    for day in _mapping_rows(week.get("days")):
        if str(day.get("day_type") or "").strip().lower() == "rest":
            continue
        for session in _mapping_rows(day.get("sessions")):
            # Optional work is offered, not planned: skipping it misses nothing.
            if is_optional_session(session):
                continue
            session_id = str(session.get("session_id") or "").strip()
            if session_id:
                planned.add(session_id)
    return planned


def _latest_statuses(
    *,
    planned: set[str],
    completions: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], set[str]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in completions:
        session_id = str(row.get("session_id") or "").strip()
        status = str(row.get("status") or "").strip().lower()
        if session_id in planned and status:
            grouped.setdefault(session_id, []).append(row)

    latest_by_session: dict[str, str] = {}
    ambiguous: set[str] = set()

    for session_id, rows in grouped.items():
        status_rows = [
            (str(row.get("status") or "").strip().lower(), _row_timestamp(row))
            for row in rows
            if str(row.get("status") or "").strip()
        ]
        statuses = {status for status, _ in status_rows}
        if len(statuses) == 1:
            latest_by_session[session_id] = next(iter(statuses))
            continue

        if any(timestamp is None for _, timestamp in status_rows):
            ambiguous.add(session_id)
            continue

        latest_timestamp = max(
            timestamp for _, timestamp in status_rows if timestamp is not None
        )
        latest_statuses = {
            status
            for status, timestamp in status_rows
            if timestamp == latest_timestamp
        }
        if len(latest_statuses) == 1:
            latest_by_session[session_id] = next(iter(latest_statuses))
        else:
            ambiguous.add(session_id)

    return latest_by_session, ambiguous


def _find_dated_week(
    plan: Mapping[str, Any],
    target: date,
) -> Mapping[str, Any] | None:
    for week in _weeks(plan):
        start = _parse_date(week.get("start_date"))
        end = _parse_date(week.get("end_date"))
        if start is not None and end is not None and start <= target <= end:
            return week
    return None


def find_week_for_training_day(
    plan: Mapping[str, Any],
    training_day: str,
) -> Mapping[str, Any] | None:
    target = _parse_date(training_day)
    if target is None:
        return None
    effective_plan = _plan_for_training_day(plan, training_day)
    return _find_dated_week(effective_plan, target)


def evaluate_week_completion(
    *,
    week: Mapping[str, Any],
    completions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    planned = _planned_session_ids(week)
    if not planned:
        return {
            "complete": False,
            "reason": "no_planned_sessions",
            "planned": 0,
            "resolved": 0,
            "ambiguous_session_ids": [],
        }

    latest_by_session, ambiguous = _latest_statuses(
        planned=planned,
        completions=completions,
    )
    unresolved = sorted(
        session_id
        for session_id in planned
        if latest_by_session.get(session_id) not in RESOLVED_STATUSES
    )
    unauthorised_skips = sorted(
        session_id
        for session_id in planned
        if latest_by_session.get(session_id) == "skipped"
    )
    complete = all(
        latest_by_session.get(session_id) in COMPLETED_STATUSES
        for session_id in planned
    )
    return {
        "complete": complete,
        "reason": "complete" if complete else "unresolved_or_skipped",
        "planned": len(planned),
        "resolved": len(planned) - len(unresolved),
        "unresolved_session_ids": unresolved,
        "skipped_session_ids": unauthorised_skips,
        "ambiguous_session_ids": sorted(ambiguous),
    }


def _reconcile_completed_week_lifecycle(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str,
    plan: Mapping[str, Any],
    week: Mapping[str, Any],
    completions: Sequence[Mapping[str, Any]],
    week_award_result: Mapping[str, Any] | None = None,
    week_key: str | None = None,
) -> None:
    """Best-effort durable repair pass, independent of the weekly XP result."""

    plan_id = str(plan.get("id") or "").strip()
    week_id = str(week.get("week_id") or "").strip()
    try:
        from api.services.plan_milestones import (
            reconcile_plan_milestones_after_completed_week,
        )

        reconcile_plan_milestones_after_completed_week(
            store,
            athlete_id=athlete_id,
            athlete_timezone=athlete_timezone,
            plan=plan,
            completed_week=week,
            completions=completions,
            week_award_result=week_award_result,
            week_key=week_key,
        )
    except Exception:  # noqa: BLE001 - repair must never break week/session logging
        logger.exception(
            "[xp] lifecycle reconciliation failed athlete_id=%s plan_id=%s week_id=%s",
            athlete_id,
            plan_id,
            week_id,
        )


def award_completed_week(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str,
    plan: Mapping[str, Any],
    training_day: str,
) -> dict[str, Any] | None:
    """Confirm a week, idempotently award XP, then reconcile lifecycle."""

    target = _parse_date(training_day)
    if target is None:
        return None
    effective_plan = _plan_for_training_day(plan, training_day)
    week = _find_dated_week(effective_plan, target)
    if week is None:
        return None
    plan_id = str(effective_plan.get("id") or "").strip()
    week_id = str(week.get("week_id") or "").strip()
    start_date = str(week.get("start_date") or "").strip()
    end_date = str(week.get("end_date") or "").strip()
    if not plan_id or not week_id or not start_date or not end_date:
        return None

    completions = store.list_plan_session_completions(
        athlete_id,
        plan_id,
        limit=500,
    )
    relevant = [
        row
        for row in completions
        if start_date <= str(row.get("training_day") or "") <= end_date
    ]
    result = evaluate_week_completion(week=week, completions=relevant)
    if not result["complete"]:
        return None

    try:
        ensure_xp_abuse_hardening(store)
    except Exception:  # noqa: BLE001 - completed sessions remain persisted
        logger.exception(
            "[xp] week and lifecycle awards disabled because hardening is unavailable athlete_id=%s plan_id=%s",
            athlete_id,
            plan_id,
        )
        return None

    source_key = f"{plan_id}:{week_id}"
    anchor = open_plan_anchor_date(plan) if open_plan_spec(plan) is not None else None
    if anchor is not None and (target - anchor).days >= 28:
        # Preserve already-awarded first-block keys. Later renewable blocks
        # need their actual week date so a prior block cannot consume the bonus.
        source_key = f"{source_key}:{start_date}"
    normalized: dict[str, Any] | None = None
    try:
        award = store.award_xp(
            athlete_id,
            action="full_training_week_completed",
            idempotency_key=f"full-week:{source_key}",
            calendar_date=start_date,
        )
        if isinstance(award, Mapping):
            normalized = dict(award)
    except Exception:  # noqa: BLE001 - weekly XP must not block lifecycle repair
        logger.exception(
            "[xp] full week award failed athlete_id=%s plan_id=%s week_id=%s",
            athlete_id,
            plan_id,
            week_id,
        )

    _reconcile_completed_week_lifecycle(
        store,
        athlete_id=athlete_id,
        athlete_timezone=athlete_timezone,
        plan=effective_plan,
        week=week,
        completions=completions,
        week_award_result=normalized,
        week_key=source_key,
    )
    return normalized


def try_award_completed_week_for_completion(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str,
    completion: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Best-effort wrapper covering plan lookup, evaluation, XP and repair."""

    plan_id = str(completion.get("plan_id") or "").strip()
    training_day = str(completion.get("training_day") or "").strip()
    if not plan_id or not training_day:
        return None

    try:
        plan = store.get_training_plan_for_athlete(plan_id, athlete_id)
        if not isinstance(plan, Mapping):
            return None
        return award_completed_week(
            store,
            athlete_id=athlete_id,
            athlete_timezone=athlete_timezone,
            plan=plan,
            training_day=training_day,
        )
    except Exception:  # noqa: BLE001 - week work must never break session logging
        logger.exception(
            "[xp] week evaluation failed athlete_id=%s plan_id=%s training_day=%s",
            athlete_id,
            plan_id,
            training_day,
        )
        return None


__all__ = [
    "award_completed_week",
    "evaluate_week_completion",
    "find_week_for_training_day",
    "try_award_completed_week_for_completion",
]
