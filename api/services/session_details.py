"""Historical evidence projection. Never reads a current plan or writes evidence."""
from collections import Counter
from collections.abc import Mapping
from datetime import date, timedelta
import re

from fastapi import HTTPException
from pydantic import ValidationError

from api.contracts.external_training import TrainingHistorySession
from api.contracts.rehab_completion import completed_dose_from_session, is_guidance_only_rehab
from api.contracts.rehab_exposure import ExposureDose
from api.contracts.session_details import SessionDetailBlock, SessionDetailRun, SessionDetails, SessionDetailSparring
from api.models import ExerciseLogActual
from api.services.rehab_completion_service import (
    _canonical_stored_event, _prescribed_dose_from_block, rehab_response_contexts_by_injury,
)
from api.services.manual_exercises import entry_actual
from api.services.training_history import external_occurrence

MISSING_DETAILS = "Detailed exercise records weren't captured for this session."
_DOSE_FIELDS = ("sets", "reps", "rounds", "load", "work", "rest", "duration", "distance",
                "effort", "tempo", "intensity", "prescription_text")


def _map(value):
    return value if isinstance(value, Mapping) else {}


def _rows(value):
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _text(value):
    # Same internal-marker grammar as web/lib/injury-display.stripInternalMarkers.
    return re.sub(r"\s?\[[a-z]+(?:_[a-z]+)+:[^\]]*\]", "", str(value or "")).strip()


def _prescribed(block):
    result = {}
    for key in _DOSE_FIELDS:
        value = block.get(key)
        if isinstance(value, Mapping):
            value = {k: _text(v) if isinstance(v, str) else v for k, v in value.items()
                     if k in {"value", "unit", "method", "display"} and isinstance(v, (str, int, float))}
        elif isinstance(value, str):
            value = _text(value)
        elif not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if value not in (None, "", {}):
            result[key] = value
    return result


def _snapshot_session(row):
    snapshot = _map(row.get("prescription_snapshot"))
    session = _map(snapshot.get("session"))
    standalone_rehab = (row.get("plan_id") is None and snapshot.get("plan_id") is None
                        and snapshot.get("rehab_only") is True and session.get("session_type") == "rehab")
    # Tolerate a legacy null plan ID. Current plan deletion cascades completions.
    if (not session or (not snapshot.get("plan_id") and not standalone_rehab)
            or (row.get("plan_id") and snapshot.get("plan_id") != row["plan_id"])
            or (snapshot.get("training_day") and snapshot["training_day"] != row.get("training_day"))
            or session.get("session_id") != row.get("session_id")):
        return None
    return session


def _summary(row, source, title, consent):
    safe = {key: row.get(key) for key in TrainingHistorySession.model_fields if key in row}
    safe.update(source=source, title=_text(title) or "Unlxck session")
    if not consent:
        for key in ("pain_after", "notes", "modification_reason", "takeaway"):
            safe.pop(key, None)
    else:
        for key in ("notes", "modification_reason", "takeaway"):
            if key in safe:
                safe[key] = _text(safe[key])
    return TrainingHistorySession.model_validate(safe)


def _exercise(block, log, consent):
    actual = ExerciseLogActual()
    if log:
        try:
            actual = ExerciseLogActual.model_validate(_map(log.get("actual")))
        except ValidationError:
            # Corrupt legacy actuals do not turn into a prescription confirmation.
            log = None
    return SessionDetailBlock(
        name=_text(block.get("display_name")) or "Training block",
        prescribed=_prescribed(block), actual=actual,
        status=log.get("status") if log and log.get("status") in {"as_prescribed", "modified", "skipped"} else None,
        reason=log["reason"] if log and log.get("reason") in ({"equipment", "fatigue", "felt_strong", "pain"}
            if consent else {"equipment", "fatigue", "felt_strong"}) else None,
        notes=_text(log.get("notes")) if log and consent else None,
    )


def _manual_block(row, consent):
    """Only what the athlete entered; no prescription to compare against."""
    return SessionDetailBlock(name=_text(row.get("display_name")) or "Exercise", source="manual",
        format=row.get("measurement_format"), actual=entry_actual(row) or ExerciseLogActual(),
        notes=(_text(row.get("notes")) or None) if consent else None)


def _rehab_responses(store, athlete_id, row):
    contexts = rehab_response_contexts_by_injury(row, athlete_id=athlete_id)
    expected = [(context, item) for context in contexts.values()
                for item in _rows(context.get("expected_exposures")) if item.get("exposure_id")]
    ids = list({item["exposure_id"] for _, item in expected})
    stored = store.list_rehab_exposures_by_ids(athlete_id, ids) if ids else []
    responses = {}
    for context, item in expected:
        matches = [event for event in stored if event.get("id") == item["exposure_id"]
                   and event.get("athlete_id") == athlete_id]
        if len(matches) != 1:
            continue
        try:
            event = _canonical_stored_event(matches[0], context)
        except (HTTPException, ValidationError):
            continue
        if event.drill_id != item.get("drill_id") or str(event.exposure_id) != item["exposure_id"]:
            continue
        key = item.get("rehab_occurrence_key")
        responses.setdefault(key, []).append(event)
    return responses


def _run_history(store, athlete_id: str, training_day: str) -> list[SessionDetailRun]:
    """Every live run through the Sunday of ``training_day``'s week, oldest first."""
    day = date.fromisoformat(training_day[:10])
    week_end = (day + timedelta(days=6 - day.weekday())).isoformat()
    rows = store.list_external_runs(athlete_id, through_day=week_end)
    return [SessionDetailRun(**{key: row.get(key) for key in SessionDetailRun.model_fields}
                             | {"id": str(row["id"]), "training_day": str(row["training_day"])[:10],
                                "created_at": str(row["created_at"]) if row.get("created_at") else None})
            for row in rows if row.get("athlete_id") == athlete_id and row.get("activity") == "roadwork"
            and not row.get("deleted_at") and str(row["training_day"])[:10] <= week_end]


def session_details(store, athlete_id, source, entry_id, *, health_consent_granted):
    consent = health_consent_granted
    row = (store.get_external_training_session(athlete_id, entry_id) if source == "external"
           else store.get_session_completion_by_id(athlete_id, entry_id))
    if (not row or row.get("athlete_id") != athlete_id or row.get("deleted_at")
            or (source == "unlxck" and (row.get("status") not in {"done", "modified", "skipped"}
                                       or row.get("completion_origin") == "day_credit"))):
        raise HTTPException(404, "Training session not found.")
    if source == "external":
        occurrence = external_occurrence(row)
        sparring = store.get_external_session_sparring_log(athlete_id, entry_id) if row.get("activity") == "sparring" else None
        safe_sparring = None
        if sparring and sparring.get("athlete_id") == athlete_id and sparring.get("external_session_id") == entry_id:
            safe_sparring = SessionDetailSparring(**{
                key: (_text(sparring.get(key)) if key == "notes" else sparring.get(key))
                for key in SessionDetailSparring.model_fields
                if key in sparring and (consent or key not in {"head_contact", "rocked", "notes"})})
        entries = [entry for entry in store.list_manual_exercises(athlete_id, entry_id)
                   if entry.get("athlete_id") == athlete_id and str(entry.get("external_session_id")) == entry_id
                   and not entry.get("deleted_at")]
        summary = _summary({**occurrence, "exercise_count": len(entries)}, source, occurrence["title"], consent)
        runs = _run_history(store, athlete_id, str(row["training_day"])) if row.get("activity") == "roadwork" else None
        return SessionDetails(session=summary, blocks=[_manual_block(entry, consent) for entry in entries],
                              prescription_source="manual", sparring=safe_sparring, runs=runs)

    session = _snapshot_session(row)
    blocks = _rows(session.get("blocks")) if session else []
    session_ids = {row.get("session_id")} | {b.get("source_session_id") for b in blocks if b.get("source_session_id")}
    logs = store.list_session_detail_exercise_logs(athlete_id, plan_id=row["plan_id"],
        training_day=row["training_day"], session_ids=sorted(s for s in session_ids if s)) if row.get("plan_id") and row.get("session_id") else []
    # Defend the service boundary as well as the owner-scoped database query.
    logs = [log for log in logs if log.get("athlete_id") == athlete_id and log.get("plan_id") == row.get("plan_id")
            and log.get("training_day") == row.get("training_day") and log.get("session_id") in session_ids
            and log.get("block_id")]
    counts = Counter((log["session_id"], log["block_id"]) for log in logs)
    indexed = {(log["session_id"], log["block_id"]): log for log in logs
               if counts[(log["session_id"], log["block_id"])] == 1}
    result = []
    responses = _rehab_responses(store, athlete_id, row) if consent and any(b.get("block_type") == "rehab" for b in blocks) else {}
    block_counts = Counter(b.get("block_id") for b in blocks if b.get("block_id"))
    seen_blocks = set()
    for block in blocks:
        block_id = block.get("block_id")
        if block_id and block_id in seen_blocks:
            continue
        if block_id:
            seen_blocks.add(block_id)
        if block.get("block_type") == "rehab":
            if not consent:
                continue
            guidance = is_guidance_only_rehab(_map(block.get("drill_snapshot")) or block)
            performance = row.get("rehab_performance")
            dose = None if guidance or performance == "skipped" or row["status"] == "skipped" else completed_dose_from_session(
                row, prescribed=_prescribed_dose_from_block(block))
            evidence = responses.get(f"block:{block.get('block_id')}", []) if block_counts[block.get("block_id")] == 1 else []
            # A later reduced/stopped answer qualifies the initial done-as-shown
            # confirmation. Never display the full dose against contrary evidence.
            if evidence and dose is not None:
                amounts = [event.dose_completed for event in evidence]
                partial = next((amount for amount in amounts if amount.completion_state == "partial_amount_unknown"), None)
                dose = partial or (amounts[0] if all(amount == amounts[0] for amount in amounts)
                                   else ExposureDose(completion_state="performed_amount_unknown"))
                if dose.completion_state != "quantified" and performance == "done_as_shown":
                    performance = "changed"
            prescribed = _prescribed(block)
            if guidance and not prescribed.get("prescription_text"):
                instructions = block.get("instructions")
                text = " ".join(_text(item) for item in instructions if isinstance(item, str)) if isinstance(instructions, list) else _text(instructions)
                if text or block.get("notes"):
                    prescribed["prescription_text"] = text or _text(block["notes"])
            result.append(SessionDetailBlock(name=_text(block.get("display_name")) or "Rehab guidance",
                kind="guidance" if guidance else "rehab", prescribed=prescribed,
                status="skipped" if row["status"] == "skipped" else performance if performance in {"done_as_shown", "changed", "stopped", "skipped"} else None,
                completed_dose=dose, responses=[event.response for event in evidence]))
        else:
            key = (block.get("source_session_id") or row.get("session_id"), block.get("block_id"))
            log = indexed.get(key) if block_counts[block.get("block_id")] == 1 else None
            result.append(_exercise(block, log, consent))
    if session is None:
        result = [_exercise(_map(log.get("prescribed")), log, consent) for log in indexed.values()
                  if _map(log.get("prescribed")).get("block_type") != "rehab"]
    return SessionDetails(session=_summary(row, source, session.get("title") if session else None, consent),
        blocks=result, prescription_source="snapshot" if session else "exercise_logs" if result else "unavailable",
        message=("Health details are hidden while health-data consent is off." if not consent and any(b.get("block_type") == "rehab" for b in blocks)
                 else None if result else MISSING_DETAILS))
