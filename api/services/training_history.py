"""One session-level history for Plan and adaptive consumers; no exercise rows."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json

from fastapi import HTTPException

from api.contracts.completion import HistoryCompletionEdit
from api.contracts.external_training import ExternalTrainingCreate, ExternalTrainingFields, ExternalTrainingTakeaway
from api.contracts.training_day import resolve_training_day_str
from api.contracts.training_disciplines import training_title


def external_occurrence(row: dict) -> dict:
    return {**row, "source": "external", "status": "done", "plan_id": None,
            "session_id": f"external-{row['id']}", "title": training_title(row)}


def training_history(store, athlete_id: str, *, limit: int = 200, prescribed: list[dict] | None = None, external: list[dict] | None = None, include_skipped: bool = False) -> list[dict]:
    """Combine occurrences; callers supplying preloaded rows retain read health.

    An explicit empty list skips a source read; it does not prove availability.
    """
    if prescribed is None:
        prescribed = store.list_session_completions(athlete_id, limit=limit)
    if external is None:
        external = store.list_external_training_sessions(athlete_id, limit=limit)
    return _combine(athlete_id, prescribed, external, include_skipped)


def training_history_page(store, athlete_id: str, *, limit: int, before_day: str | None = None,
                          include_skipped: bool = False, manual_only: bool = False) -> tuple[list[dict], str | None]:
    """One History page of whole training days, newest first, and the cursor for the next.

    Each source reads ``limit`` rows before ``before_day``. When a source fills its
    read, its oldest day may be cut short, so that day moves to the next page. A
    day that alone fills a read is read in full instead, so no row is skipped.
    """
    def prescribed_read(*, limit: int, before_day: str | None) -> list[dict]:
        if manual_only:
            return []
        return store.list_session_completions(athlete_id, limit=limit, before_day=before_day)

    def external_read(*, limit: int, before_day: str | None) -> list[dict]:
        return store.list_external_training_sessions(athlete_id, limit=limit, before_day=before_day)

    prescribed = prescribed_read(limit=limit, before_day=before_day)
    external = external_read(limit=limit, before_day=before_day)
    full = [source for source in (prescribed, external) if len(source) >= limit]
    if not full:
        return _combine(athlete_id, prescribed, external, include_skipped), None
    # The newest day any full source reached may be missing rows, so it and
    # everything older is left to the next page, which reads before ``oldest``.
    cutoff = max(str(source[-1]["training_day"]) for source in full)
    rows = _combine(athlete_id, prescribed, external, include_skipped)
    kept = [row for row in rows if str(row["training_day"]) > cutoff]
    if kept:
        return kept, min(str(row["training_day"]) for row in kept)
    # One day fills a whole read: return all of that day, and continue before it.
    day_rows = _combine(athlete_id, _whole_day(prescribed_read, cutoff, limit),
                        _whole_day(external_read, cutoff, limit), include_skipped)
    return day_rows, cutoff


def _whole_day(read, day: str, limit: int) -> list[dict]:
    """Every row of ``day`` from one source, newest-day-first reads widened until
    the read is short or reaches an older day."""
    after = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    size = max(limit, 1) * 2
    while True:
        rows = read(limit=size, before_day=after)
        if len(rows) < size or str(rows[-1]["training_day"]) < day:
            return [row for row in rows if str(row["training_day"]) == day]
        size *= 2


def _combine(athlete_id: str, prescribed: list[dict], external: list[dict], include_skipped: bool) -> list[dict]:
    rows = [
        {**row, "source": "unlxck", "completion_origin": row.get("completion_origin")} for row in prescribed
        if row.get("athlete_id") == athlete_id and row.get("status") in ({"done", "modified", "skipped"} if include_skipped else {"done", "modified"})
        and row.get("completion_origin") != "day_credit"
    ] + [external_occurrence(row) for row in external
         if row.get("athlete_id") == athlete_id and not row.get("deleted_at")]
    # IDs are scoped to their source. Name/date matches are distinct sessions.
    unique = {(row["source"], row.get("id") or row.get("session_id"), row.get("plan_id"), row["training_day"]): row for row in rows}
    return sorted(unique.values(), key=lambda row: (
        str(row["training_day"]), str(row.get("completed_at") or row.get("created_at") or ""),
        str(row.get("id") or "")), reverse=True)


def create_external_training(store, athlete_id: str, athlete_timezone: str | None,
                             body: ExternalTrainingCreate, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    current_day = resolve_training_day_str(now, athlete_timezone=athlete_timezone)
    training_day = body.training_day.isoformat() if body.training_day else current_day
    if training_day > current_day:
        raise HTTPException(422, "Training date cannot be after your current training day.")
    # Preserve existing request fingerprints when the optional date, sport or
    # session type is omitted.
    fields = body.model_dump(mode="json", exclude={"client_request_id"}, exclude_none=True)
    fingerprint = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
    row = store.create_external_training_session(athlete_id, {
        **fields, "client_request_id": str(body.client_request_id), "request_fingerprint": fingerprint,
        "training_day": training_day,
    })
    if row.get("deleted_at"):
        raise HTTPException(410, "This session was deleted. Log new work with a new request ID.")
    if row["request_fingerprint"] != fingerprint:
        raise HTTPException(409, "This request ID was already used for another session.")
    from api.services.tracking_xp import reconcile_training_xp

    reconcile_training_xp(store, athlete_id, manual_session_id=str(row["id"]), athlete_timezone=athlete_timezone)
    return external_occurrence(row)


def update_external_training(store, athlete_id: str, entry_id: str, body: ExternalTrainingFields) -> dict:
    row = store.update_external_training_session(athlete_id, entry_id, body.model_dump())
    if row is None:
        raise HTTPException(404, "Training session not found.")
    return external_occurrence(row)


def set_external_training_takeaway(store, athlete_id: str, entry_id: str, body: ExternalTrainingTakeaway) -> dict:
    row = store.set_external_training_takeaway(athlete_id, entry_id, body.takeaway)
    if row is None:
        raise HTTPException(404, "Training session not found.")
    return external_occurrence(row)


def delete_external_training(store, athlete_id: str, entry_id: str) -> None:
    # Keep a tombstone so a late retry of the initial POST cannot recreate it.
    if not store.delete_external_training_session(athlete_id, entry_id):
        raise HTTPException(404, "Training session not found.")


def plan_completion_history_editable(row: dict) -> bool:
    """A finished plan session History may edit or remove.

    Day credit is not a performed session, legacy rows have no verified
    identity, and rehab work is clinical evidence, so those stay as logged.
    """
    return bool(row.get("plan_id")) and row.get("status") in {"done", "modified"} \
        and row.get("completion_origin") == "reported" \
        and not row.get("rehab_response_contexts") and not row.get("rehab_performance") \
        and not _frozen_rehab_blocks(row)


def _frozen_rehab_blocks(row: dict) -> bool:
    """A saved prescription holding rehab work (the database refuses to reset those)."""
    snapshot = row.get("prescription_snapshot")
    session = snapshot.get("session") if isinstance(snapshot, dict) else None
    blocks = session.get("blocks") if isinstance(session, dict) else None
    return any(isinstance(block, dict) and (block.get("block_type") == "rehab" or "policy_id" in block
                                            or "clinical_review_pin" in block) for block in blocks or [])


def _editable_plan_completion(store, athlete_id: str, completion_id: str) -> dict:
    row = store.get_session_completion_by_id(athlete_id, completion_id)
    if not row or row.get("status") not in {"done", "modified"}:
        raise HTTPException(404, "Training session not found.")
    if not plan_completion_history_editable(row):
        raise HTTPException(409, "This session can't be changed from History.")
    return row


def edit_plan_completion(store, athlete_id: str, completion_id: str, body: HistoryCompletionEdit, *,
                         health_consent_granted: bool) -> dict:
    _editable_plan_completion(store, athlete_id, completion_id)
    # Only the fields sent change; an RPE-only edit keeps notes and pain.
    fields = body.model_dump(exclude_unset=True)
    if not health_consent_granted:
        # Pain is health data: without consent the stored value is kept as is.
        fields.pop("pain_after", None)
    updated = store.update_session_completion_fields(athlete_id, completion_id, fields)
    if updated is None:
        raise HTTPException(404, "Training session not found.")
    return updated


def delete_plan_completion(store, athlete_id: str, completion_id: str) -> dict | None:
    """Mark a finished plan session not done; None when it already was (a retried delete)."""
    row = store.get_session_completion_by_id(athlete_id, completion_id)
    if row and row.get("status") == "not_started":
        return None
    _editable_plan_completion(store, athlete_id, completion_id)
    # One transaction removes its exercise results with it (a redo starts
    # clean, progression never reads removed results) or changes nothing. The
    # row and its id, which keys earned XP, stay: a redo cannot award twice.
    updated = store.reset_history_plan_session(athlete_id, completion_id)
    if updated is None:
        raise HTTPException(404, "Training session not found.")
    return updated
