"""Log what an athlete actually did for one prescribed block today.

A log never edits the plan. It records the block as prescribed (a frozen copy)
beside what was done, keyed on the block's server-owned ``block_id``.

Logging is only open for a session the athlete has started or completed today.
That is deliberate: every gate on training itself (rest day, safety hold,
severe injury, check-in, stale prescription) is enforced once, on the session
start, and a log can never record work the athlete was not cleared to begin.

Optional work (the camp Fight Visualisation) never gets a completion row of its
own when the day starts: it sits outside the day unit. Its blocks can still be
logged once the day is started, so the athlete can say it was done.

Rehab blocks are not logged here. Their dose is recorded by the rehab
completion flow, which owns rehab evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status

from api.optional_sessions import is_optional_session
from api.store import AppStore

from .today_service import (
    _TRAINING_COMPLETION_STATUSES,
    _iter_mapping_items,
    _require_valid_plan_id,
    _structured_day_for_training_day,
    resolve_training_day,
)

# The prescription fields a log freezes. Coaching copy (cues, purpose, stop
# rules) is left out: it is not what the athlete's numbers are compared against.
_PRESCRIBED_FIELDS: tuple[str, ...] = (
    "display_name",
    "block_type",
    "category",
    "sets",
    "reps",
    "rounds",
    "load",
    "duration",
    "work",
    "rest",
    "distance",
    "effort",
    "tempo",
    "intensity",
    "prescription_text",
    "athlete_dose",
    "notes",
    # What the next suggestion is read against.
    "rep_range",
    "progression_hold",
)

_NOT_STARTED_DETAIL = "Start today's session before logging an exercise."
_UNKNOWN_BLOCK_DETAIL = "That exercise is not in today's session. Refresh Today and try again."
_REHAB_DETAIL = "Rehab is logged when you complete the session."


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _prescribed_snapshot(block: Mapping[str, Any]) -> dict[str, Any]:
    return {key: block[key] for key in _PRESCRIBED_FIELDS if block.get(key) not in (None, "", [], {})}


def _active_completions(
    store: AppStore, *, athlete_id: str, plan_id: str, training_day: str
) -> list[dict[str, Any]]:
    """Today's started / completed session records for this plan."""
    rows = store.list_session_completions_from_day(athlete_id, training_day)
    return [
        row
        for row in rows
        if _clean(row.get("training_day"))[:10] == training_day
        and _clean(row.get("plan_id")) == plan_id
        and _clean(row.get("status")) in _TRAINING_COMPLETION_STATUSES
    ]


def _loggable_blocks(
    plan_row: Mapping[str, Any],
    *,
    training_day: str,
    completions: list[dict[str, Any]],
) -> list[tuple[str | None, Mapping[str, Any]]]:
    """``(session_id, block)`` for every block of today's started sessions.

    A session started under live injury guidance was frozen on its completion
    row; that frozen copy is authoritative, even when it contains no blocks.
    The plan card is used only for started sessions without a valid snapshot.
    Optional sessions ride on the started day: starting the day never writes
    their row, so their blocks are open whenever any session is.
    """
    plan_id = _clean(plan_row.get("id"))
    blocks: list[tuple[str | None, Mapping[str, Any]]] = []
    frozen_session_ids: set[str] = set()
    for completion in completions:
        snapshot = completion.get("prescription_snapshot")
        session = snapshot.get("session") if isinstance(snapshot, Mapping) else None
        if not isinstance(session, Mapping) or _clean(snapshot.get("plan_id")) != plan_id:
            continue
        session_id = _clean(session.get("session_id")) or _clean(completion.get("session_id")) or None
        frozen_session_ids.add(session_id or "")
        blocks.extend((block.get("source_session_id") or session_id, block)
                      for block in _iter_mapping_items(session.get("blocks")))

    started_session_ids = {_clean(row.get("session_id")) for row in completions}
    matched = _structured_day_for_training_day(plan_row, training_day)
    if matched is not None:
        day, _week = matched
        for session in _iter_mapping_items(day.get("sessions")):
            session_id = _clean(session.get("session_id"))
            if session_id in frozen_session_ids:
                continue
            if session_id not in started_session_ids and not is_optional_session(session):
                continue
            blocks.extend((session_id, block) for block in _iter_mapping_items(session.get("blocks")))
    return blocks


def _resolve_logging_day(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
    plan_id: str,
    now: datetime | None,
) -> tuple[str, list[tuple[str | None, Mapping[str, Any]]]]:
    """``(training_day, loggable blocks)`` once the plan and the day's start are checked."""
    _require_valid_plan_id(plan_id)
    # Service-role write: plan ownership is enforced here, not by RLS.
    plan_row = store.get_training_plan_for_athlete(plan_id, athlete_id)
    if plan_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")

    training_day = resolve_training_day(athlete_timezone, now=now)
    completions = _active_completions(
        store, athlete_id=athlete_id, plan_id=plan_id, training_day=training_day
    )
    if not completions:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_NOT_STARTED_DETAIL)
    return training_day, _loggable_blocks(plan_row, training_day=training_day, completions=completions)


def _log_fields(
    entry: Mapping[str, Any],
    *,
    plan_id: str,
    training_day: str,
    blocks: list[tuple[str | None, Mapping[str, Any]]],
    health_consent_granted: bool,
) -> dict[str, Any]:
    """The row to upsert for one entry, or the HTTP error that rejects it."""
    block_id = _clean(entry.get("block_id"))
    match = next(
        ((session_id, block) for session_id, block in blocks if _clean(block.get("block_id")) == block_id),
        None,
    )
    if match is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_UNKNOWN_BLOCK_DETAIL)
    session_id, block = match
    if _clean(block.get("block_type")) == "rehab":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_REHAB_DETAIL)

    reason = entry.get("reason")
    if reason == "pain" and not health_consent_granted:
        # Pain is health data. Without consent the training log is kept and the
        # health field is dropped, as session completion does with pain_after.
        reason = None
    actual = entry.get("actual")
    return {
        "plan_id": plan_id,
        "session_id": session_id,
        "block_id": block_id,
        "exercise_key": _clean(block.get("exercise_key")) or None,
        "training_day": training_day,
        "status": entry.get("status"),
        "reason": reason,
        "prescribed": _prescribed_snapshot(block),
        "actual": dict(actual) if isinstance(actual, Mapping) else {},
        # Free text can contain health data, regardless of the selected reason.
        "notes": _clean(entry.get("notes")) if health_consent_granted else "",
    }


def record_exercise_log(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
    payload: Mapping[str, Any],
    health_consent_granted: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate and upsert one exercise log for the server's training day."""
    plan_id = _clean(payload.get("plan_id"))
    training_day, blocks = _resolve_logging_day(
        store, athlete_id=athlete_id, athlete_timezone=athlete_timezone, plan_id=plan_id, now=now
    )
    fields = _log_fields(
        payload,
        plan_id=plan_id,
        training_day=training_day,
        blocks=blocks,
        health_consent_granted=health_consent_granted,
    )
    return store.upsert_exercise_log(athlete_id, fields)


def record_exercise_logs(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
    plan_id: str,
    entries: list[Mapping[str, Any]],
    health_consent_granted: bool,
    keep_existing: bool = False,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Validate every entry, then write them all in one atomic statement.

    Validation runs first, so an invalid block writes nothing; the write itself
    is a single bulk upsert, so a database failure writes nothing either.

    With ``keep_existing`` a block already logged today keeps its log (the
    database skips it on conflict) and is returned as stored.
    """
    plan_id = _clean(plan_id)
    training_day, blocks = _resolve_logging_day(
        store, athlete_id=athlete_id, athlete_timezone=athlete_timezone, plan_id=plan_id, now=now
    )
    rows = [
        _log_fields(
            entry,
            plan_id=plan_id,
            training_day=training_day,
            blocks=blocks,
            health_consent_granted=health_consent_granted,
        )
        for entry in entries
    ]
    written = store.upsert_exercise_logs(athlete_id, rows, keep_existing=keep_existing)
    if keep_existing:
        # Kept rows are not returned by the write: read the day back so every
        # entry is answered with what is now stored.
        stored = {
            _clean(row.get("block_id")): row
            for row in store.list_exercise_logs_for_day(athlete_id, plan_id=plan_id, training_day=training_day)
        }
    else:
        stored = {_clean(row.get("block_id")): row for row in written}
    missing = [fields["block_id"] for fields in rows if fields["block_id"] not in stored]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="failed to save exercise logs"
        )
    return [stored[fields["block_id"]] for fields in rows]


def recent_exercise_performances(store: AppStore, *, athlete_id: str, before_day: str) -> list[dict[str, Any]]:
    return exercise_history_context(store, athlete_id=athlete_id, before_day=before_day)[0]


# Today reads at most this many of the athlete's newest earlier logs, so the
# cost of every Today load stays flat as history grows. Two pages reach well
# past the movements a camp rotates through; one older than that is not recalled.
_HISTORY_PAGE_ROWS = 500
_HISTORY_SCAN_ROWS = 1000


def exercise_history_context(store: AppStore, *, athlete_id: str, before_day: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Latest occurrence and latest reported weight per movement, across plans.

    A bounded, paged read of the newest history retains less frequent movements
    and loads omitted on a later log. A latest skip or pain report prevents
    recalling an older weight.
    """
    latest: dict[str, dict[str, Any]] = {}
    weights: dict[str, dict[str, Any]] = {}
    offset = 0
    while True:
        rows = store.list_exercise_history(athlete_id, before_day=before_day, limit=_HISTORY_PAGE_ROWS, offset=offset)
        for row in rows:
            prescribed = row.get("prescribed") or {}
            key = _clean(row.get("exercise_key"))
            name = _clean(prescribed.get("display_name")).casefold()
            identity = f"key:{key}" if key else f"name:{name}"
            if identity == "name:":
                continue
            latest.setdefault(identity, row)
            if identity not in weights:
                candidate = recent_exercise_loads(store, athlete_id=athlete_id, before_day=before_day, performances=[row])
                if candidate:
                    weights[identity] = candidate[0]
        offset += len(rows)
        if len(rows) < _HISTORY_PAGE_ROWS or offset >= _HISTORY_SCAN_ROWS:
            loads = [weight for identity, weight in weights.items()
                     if latest[identity].get("status") != "skipped" and latest[identity].get("reason") != "pain"]
            return list(latest.values()), loads


def recent_exercise_loads(
    store: AppStore, *, athlete_id: str, before_day: str, performances: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """The latest weight logged for each exercise before ``before_day``.

    Keyed by the planner's ``exercise_key`` when the log has one, otherwise by
    the exercise's name, so the next "done" can carry the weight forward.
    """
    seen: set[str] = set()
    loads: list[dict[str, Any]] = []
    rows = performances if performances is not None else store.list_recent_exercise_loads(athlete_id, before_day=before_day)
    for row in rows:
        if row.get("status") == "skipped" or row.get("reason") == "pain":
            continue
        actual = row.get("actual") if isinstance(row.get("actual"), Mapping) else {}
        load = actual.get("load")
        # A varied workout has no single load. Carry only its last measured set
        # as a suggestion, never as the load for every historical set.
        if isinstance(actual.get("set_entries"), list):
            load = next((entry.get("load") for entry in reversed(actual["set_entries"])
                         if isinstance(entry, Mapping) and entry.get("load") is not None), None)
        # Only a well-formed weight is carried forward; anything else is skipped.
        if (
            not isinstance(load, Mapping)
            or not isinstance(load.get("value"), (int, float))
            or isinstance(load.get("value"), bool)
            or load["value"] <= 0
            or not _clean(load.get("unit"))
        ):
            continue
        prescribed = row.get("prescribed") if isinstance(row.get("prescribed"), Mapping) else {}
        exercise_key = _clean(row.get("exercise_key")) or None
        display_name = _clean(prescribed.get("display_name"))
        identity = f"key:{exercise_key}" if exercise_key else f"name:{display_name.lower()}"
        if identity in seen or identity == "name:":
            continue
        seen.add(identity)
        loads.append(
            {
                "exercise_key": exercise_key,
                "display_name": display_name,
                "load": {"value": load.get("value"), "unit": load.get("unit")},
                "training_day": str(row.get("training_day") or "")[:10],
            }
        )
    return loads


def list_exercise_logs_for_today(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
    plan_id: str,
    now: datetime | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """``(training_day, logs)`` for one of the athlete's plans, today only."""
    _require_valid_plan_id(plan_id)
    if store.get_plan_identity_for_athlete(plan_id, athlete_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
    training_day = resolve_training_day(athlete_timezone, now=now)
    rows = store.list_exercise_logs_for_day(athlete_id, plan_id=plan_id, training_day=training_day)
    return training_day, rows


__all__ = [
    "list_exercise_logs_for_today",
    "recent_exercise_loads",
    "record_exercise_log",
    "record_exercise_logs",
]
