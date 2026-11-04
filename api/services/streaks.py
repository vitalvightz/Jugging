"""Server-authoritative app, training-consistency and plan-adherence streaks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from api.services.active_plan import resolve_active_plan
from api.services.today_service import _structured_session_entry_for_day, resolve_training_day
from api.services.week_progress import COMPLETED_STATUSES, _latest_statuses, _plan_for_training_day
from api.store import AppStore
from api.optional_sessions import is_optional_session


def _rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [dict(row) for row in value if isinstance(row, Mapping)]


def _read_state(store: AppStore, athlete_id: str) -> dict[str, Any]:
    return dict(store.get_athlete_streaks(athlete_id) or {})


def _write_state(store: AppStore, athlete_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    return dict(store.upsert_athlete_streaks(athlete_id, dict(fields)))


def _public_state(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "login": {
            "current": max(0, int(row.get("login_current") or 0)),
            "best": max(0, int(row.get("login_best") or 0)),
            "last_active_date": row.get("login_last_active_date"),
        },
        "adherence": {
            # API key retained for the existing XP-card contract. The values are
            # the athlete-facing Training Streak, not plan adherence.
            "current": max(0, int(row.get("training_current") or 0)),
            "best": max(0, int(row.get("training_best") or 0)),
            "last_qualifying_day": row.get("training_last_qualifying_day"),
        },
        "plan_adherence": {
            "current": max(0, int(row.get("adherence_current") or 0)),
            "best": max(0, int(row.get("adherence_best") or 0)),
            "last_qualifying_day": row.get("adherence_last_qualifying_day"),
        },
    }


def record_daily_activity(
    store: AppStore, *, athlete_id: str, athlete_timezone: str | None, now: datetime | None = None
) -> dict[str, Any]:
    """Record at most one activity row for the server-resolved effective day.

    The store records the day and rebuilds the login streak in one atomic step
    (the ``record_athlete_daily_activity`` RPC) and returns the streak row.
    """
    activity_day = date.fromisoformat(resolve_training_day(athlete_timezone, now=now))
    return _public_state(store.record_daily_activity(athlete_id, activity_day.isoformat()))


def _scheduled_days(plan: Mapping[str, Any], training_day: str) -> list[tuple[date, set[str]]]:
    projected = _plan_for_training_day(plan, training_day)
    structured = projected.get("structured_plan")
    weeks = structured.get("weeks") if isinstance(structured, Mapping) else None
    result: list[tuple[date, set[str]]] = []
    for week in _rows(weeks):
        for day_row in _rows(week.get("days")):
            if str(day_row.get("day_type") or "").lower() == "rest":
                continue
            try:
                scheduled = date.fromisoformat(str(day_row.get("date")))
            except ValueError:
                continue
            ids = {
                str(row.get("session_id") or "").strip()
                for row in _rows(day_row.get("sessions"))
                if not is_optional_session(row)
            }
            ids.discard("")
            if ids:
                result.append((scheduled, ids))
    return sorted(result)


def _training_schedule(plan: Mapping[str, Any], training_day: str) -> dict[date, set[str]]:
    """Expected training days and their canonical app-session identities."""
    projected = _plan_for_training_day(plan, training_day)
    structured = projected.get("structured_plan")
    weeks = structured.get("weeks") if isinstance(structured, Mapping) else None
    result: dict[date, set[str]] = {}
    for week in _rows(weeks):
        for day_row in _rows(week.get("days")):
            try:
                scheduled = date.fromisoformat(str(day_row.get("date")))
            except ValueError:
                continue
            ids = {
                str(session.get("session_id") or "").strip()
                for session in _rows(day_row.get("sessions"))
                if not is_optional_session(session)
            }
            ids.discard("")
            # Sessionless coach/technical work is still expected training. The
            # Today projection owns the distinction between that and true rest.
            if ids or _structured_session_entry_for_day(day_row, week=week) is not None:
                result[scheduled] = ids
    return result


def _all_completions(store: AppStore, athlete_id: str) -> list[dict[str, Any]]:
    return _rows(store.list_session_completions(athlete_id, limit=500))


def _session_logs(store: AppStore, athlete_id: str) -> list[dict[str, Any]]:
    return _rows(store.list_session_logs(athlete_id, limit=500))


def qualifying_training_days(
    store: AppStore,
    *,
    athlete_id: str,
    today: date,
) -> tuple[set[date], set[date]]:
    """Return (completed training days, explicitly skipped expected days).

    Current session completions qualify only when tied to a canonical scheduled
    session, or to a sessionless coach/technical day in that completion's plan.
    Completed legacy/manual ``session_logs`` are authoritative actual training.
    """
    from api.services.open_plan_timeline import open_plan_anchor_date, open_plan_spec
    plans: dict[str, dict[str, Any] | None] = {}
    schedules: dict[tuple[str, int], dict[date, set[str]]] = {}
    qualifying: set[date] = set()
    skipped: set[date] = set()
    for row in _all_completions(store, athlete_id):
        if str(row.get("status") or "") not in {"done", "modified", "skipped"}:
            continue
        try:
            activity_day = date.fromisoformat(str(row.get("training_day")))
        except ValueError:
            continue
        plan_id = str(row.get("plan_id") or "")
        if plan_id not in plans:
            # One projected read per distinct plan: the schedule never needs
            # the Stage-2 payload or prompt text that select("*") drags in.
            plans[plan_id] = store.get_training_plan_for_athlete(plan_id, athlete_id) if plan_id else None
        plan = plans[plan_id]
        anchor = open_plan_anchor_date(plan) if plan and open_plan_spec(plan) else None
        block = max(0, (activity_day - anchor).days // 28) if anchor else 0
        schedule_key = (plan_id, block)
        if schedule_key not in schedules:
            schedules[schedule_key] = _training_schedule(plan, activity_day.isoformat()) if plan else {}
        expected_ids = schedules[schedule_key].get(activity_day)
        session_id = str(row.get("session_id") or "").strip()
        # Before canonical identity was enforced, the supported Today flow used
        # the local day as its key for sessionless coach/technical cards. Accept
        # that narrow historical provenance for streak rebuild only; it remains
        # ineligible for XP and arbitrary ids still fail closed.
        legacy_sessionless = (
            expected_ids == set() and session_id == activity_day.isoformat()
        )
        legitimate = expected_ids is not None and (
            session_id in expected_ids or legacy_sessionless
        )
        if not legitimate:
            continue
        if row.get("status") == "skipped":
            skipped.add(activity_day)
        else:
            qualifying.add(activity_day)
    for row in _session_logs(store, athlete_id):
        if row.get("completed") is not True:
            continue
        try:
            qualifying.add(date.fromisoformat(str(row.get("session_date"))))
        except ValueError:
            continue
    return qualifying, skipped


def reconcile_training_streak(
    store: AppStore, *, athlete_id: str, athlete_timezone: str | None, now: datetime | None = None
) -> dict[str, Any]:
    """Rebuild actual-training consistency without awarding XP.

    Completed training advances once per athlete-local day. Expected training
    missed before today breaks the run. Genuine rest days are absent and neutral;
    an unresolved expected current day is also neutral.
    """
    today = date.fromisoformat(resolve_training_day(athlete_timezone, now=now))
    resolution = resolve_active_plan(store, athlete_id, current_training_day=today)
    plan = resolution.plan
    expected = set(_training_schedule(plan, today.isoformat())) if plan else set()
    qualifying, skipped = qualifying_training_days(
        store, athlete_id=athlete_id, today=today
    )
    current = best = 0
    last: date | None = None
    for activity_day in sorted(expected | qualifying | skipped):
        if activity_day > today:
            continue
        if activity_day in qualifying:
            current += 1
            best = max(best, current)
            last = activity_day
        elif activity_day < today or activity_day in skipped:
            current = 0
            last = None
    row = _write_state(store, athlete_id, {
        "training_current": current,
        "training_best": best,
        "training_last_qualifying_day": last.isoformat() if last else None,
    })
    return _public_state(row)


def reconcile_adherence_streak(
    store: AppStore, *, athlete_id: str, athlete_timezone: str | None, now: datetime | None = None
) -> dict[str, Any]:
    today = date.fromisoformat(resolve_training_day(athlete_timezone, now=now))
    resolution = resolve_active_plan(store, athlete_id, current_training_day=today)
    prior = _read_state(store, athlete_id)
    if resolution.plan is None:
        if resolution.source == "read_failure":
            return _public_state(prior)
        row = _write_state(store, athlete_id, {
            "adherence_current": 0,
            "adherence_best": max(0, int(prior.get("adherence_best") or 0)),
            "adherence_last_qualifying_day": None,
        })
        return _public_state(row)
    plan_id = str(resolution.plan.get("id") or "")
    completions = _rows(store.list_plan_session_completions(athlete_id, plan_id, limit=500))
    current = 0
    last_qualifying: date | None = None
    for scheduled, planned in _scheduled_days(resolution.plan, today.isoformat()):
        if scheduled > today:
            continue
        day_completions = [
            row for row in completions
            if str(row.get("training_day") or "") == scheduled.isoformat()
        ]
        statuses, ambiguous = _latest_statuses(planned=planned, completions=day_completions)
        qualifies = not ambiguous and all(statuses.get(item) in COMPLETED_STATUSES for item in planned)
        skipped = any(statuses.get(item) == "skipped" for item in planned)
        if qualifies:
            current += 1
            last_qualifying = scheduled
        elif scheduled < today or skipped:
            current = 0
            last_qualifying = None
        # An unresolved current day is still in progress and is neutral.
    best = max(int(prior.get("adherence_best") or 0), current)
    row = _write_state(store, athlete_id, {
        "adherence_current": current,
        "adherence_best": best,
        "adherence_last_qualifying_day": last_qualifying.isoformat() if last_qualifying else None,
    })
    return _public_state(row)


def get_streak_state(
    store: AppStore, *, athlete_id: str, athlete_timezone: str | None, now: datetime | None = None
) -> dict[str, Any]:
    """Read persisted streak state without recording activity or writing counters."""
    return _public_state(_read_state(store, athlete_id))


__all__ = ["get_streak_state", "qualifying_training_days", "record_daily_activity", "reconcile_adherence_streak", "reconcile_training_streak"]
