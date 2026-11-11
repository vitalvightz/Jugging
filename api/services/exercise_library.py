"""Complete retained-log summaries; no current plans, guessed keys or evidence writes.

Two sources feed one personal library: prescribed exercise logs and the
optional exercise entries on manual sessions. They share canonical
``key:`` identities only through an exact stored exercise key. A movement the
athlete named themselves stays its own private ``custom:`` identity.
"""
import base64
from datetime import date, datetime, timezone
import json
import re
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError

from api.contracts.exercise_library import (
    ExerciseLibraryResponse, ExercisePerformance, ExercisePerformancesResponse,
    PersonalExercise, RecordedExerciseLoad,
)
from api.models import ExerciseLogActual, ExerciseLogRecord
from api.rep_range import progression_context
from api.services.manual_exercises import entry_actual, entry_identity, values_format
from api.services.session_details import _exercise, _manual_block, _map, _text

_BATCH_SIZE = 500
_KEY = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_EXCLUDED_TYPES = {"rehab", "mindset", "nutrition", "sparring"}
_UNIT_ALIASES = {"sec": "seconds", "s": "seconds", "second": "seconds", "min": "minutes", "minute": "minutes",
                 "m": "meters", "metres": "meters", "meter": "meters", "metre": "meters", "mi": "miles"}


def exercise_identity(row: dict) -> str:
    key = row.get("exercise_key")
    return f"key:{key}" if isinstance(key, str) and len(key) <= 120 and _KEY.fullmatch(key) else f"log:{row['id']}"


def _actual(row: dict) -> ExerciseLogActual | None:
    try:
        if not isinstance(row.get("actual"), dict):
            return None
        for key, value in row["actual"].items():
            if key in {"set_entries", "feel"}:
                continue
            number = value.get("value") if isinstance(value, dict) else value
            if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float))):
                return None
        return ExerciseLogActual.model_validate(row["actual"])
    except ValidationError:
        return None


def _performed(row: dict, actual: ExerciseLogActual | None) -> bool:
    if actual is None or _map(row.get("prescribed")).get("block_type") in _EXCLUDED_TYPES:
        return False
    values = actual.model_dump(exclude_none=True)
    prescribed = _map(row.get("prescribed")) if row.get("status") == "as_prescribed" else {}
    if any(values.get(key, prescribed.get(key)) == 0 for key in ("sets", "reps", "rounds")):
        return False
    if any(_map(values.get(key, prescribed.get(key))).get("value") == 0 for key in ("duration", "work", "distance")):
        return False
    return row.get("status") == "as_prescribed" or (row.get("status") == "modified" and bool(values))


def performed_values(row: dict, actual: ExerciseLogActual) -> ExerciseLogActual:
    """Confirmed numeric prescription fields only; actuals always take priority."""
    values = actual.model_dump(exclude_none=True)
    if row.get("status") != "as_prescribed":
        return actual
    prescribed = _map(row.get("prescribed"))
    for key in ExerciseLogActual.model_fields:
        if actual.set_entries is not None and key in {"sets", "reps", "load", "effort"}:
            continue
        if key in values:
            continue
        value = prescribed.get(key)
        if key in {"sets", "reps", "rounds"}:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                continue
        elif key == "load":
            if _map(value).get("method") not in {None, "absolute"} or _map(value).get("unit") not in {"kg", "lb"}:
                continue
            if isinstance(value.get("value"), bool) or not isinstance(value.get("value"), (int, float)):
                continue
            value = {k: value[k] for k in ("value", "unit") if k in value}
        elif key == "effort":
            if _map(value).get("method") != "RPE":
                continue
            value = {k: value[k] for k in ("method", "value") if k in value}
        else:
            if not isinstance(value, dict):
                continue
            value = {"value": value.get("value"), "unit": _UNIT_ALIASES.get(value.get("unit"), value.get("unit"))}
        try:
            # Validate each fallback independently; never parse a display string.
            checked = ExerciseLogActual.model_validate({key: value}).model_dump(exclude_none=True)
        except ValidationError:
            continue
        values.update(checked)
    return ExerciseLogActual.model_validate(values)


def exercise_library(store, athlete_id: str, *, now: datetime | None = None, allow_progression: bool = False, held_keys: set[str] | None = None) -> ExerciseLibraryResponse:
    as_of = (now or datetime.now(timezone.utc)).isoformat()
    summaries: dict[str, PersonalExercise] = {}
    performances: dict[str, tuple[tuple[str, str], ExerciseLogRecord]] = {}
    latest: dict[str, tuple[str, str]] = {}
    after_id = None
    while True:
        rows = store.list_exercise_index_page(athlete_id, as_of=as_of, after_id=after_id, limit=_BATCH_SIZE)
        if not rows:
            break
        ids = [str(row.get("id") or "") for row in rows]
        if ids != sorted(set(ids)) or not all(ids) or (after_id and ids[0] <= after_id):
            raise HTTPException(503, "Exercise history is temporarily unavailable.")
        for row in rows:
            if row.get("athlete_id") != athlete_id:
                continue
            actual = _actual(row)
            identity = exercise_identity(row)
            order = (row["training_day"], row["id"])
            if (row.get("status") in {"as_prescribed", "modified", "skipped"} and actual is not None
                    and _map(row.get("prescribed")).get("block_type") not in _EXCLUDED_TYPES
                    and (identity not in performances or order > performances[identity][0])):
                snapshot = {k: v for k, v in _map(row.get("prescribed")).items() if k in {
                    "display_name", "block_type", "prescription_source", "sets", "reps", "rep_range",
                    "load", "effort", "progression_hold", "exercise_type"}}
                performances[identity] = (order, ExerciseLogRecord(**{
                    **row, "block_id": row.get("block_id") or "", "notes": "",
                    "prescribed": snapshot, "actual": actual}))
                # Private plan movements use the logger's existing exact-name identity.
                if not row.get("exercise_key") and snapshot.get("prescription_source") == "athlete":
                    name_identity = "athlete-name:" + str(snapshot.get("display_name") or "").strip().casefold()
                    if name_identity not in performances or order > performances[name_identity][0]:
                        performances[name_identity] = performances[identity]
            if not _performed(row, actual):
                continue
            values = performed_values(row, actual)
            _record(summaries, latest, identity, order, values,
                name=_text(_map(row.get("prescribed")).get("display_name")) or (row.get("exercise_key") or "Legacy exercise record"),
                source="plan", kind="canonical" if identity.startswith("key:") else "legacy",
                confirmed=row["status"] == "as_prescribed",
                basis="reported" if actual.load is not None or any(entry.load is not None for entry in actual.set_entries or []) else "confirmed_prescription")
        after_id = ids[-1]
    manual_formats: dict[str, tuple[tuple[str, str], str]] = {}
    for row in _manual_index_rows(store, athlete_id, as_of):
        values = entry_actual(row)
        if values is None:
            continue
        identity = entry_identity(row)
        day = str(row["training_day"])[:10]
        order = (day, str(row["id"]))
        if identity not in performances or order > performances[identity][0]:
            performances[identity] = (order, ExerciseLogRecord(id=str(row["id"]), athlete_id=athlete_id,
                block_id="", exercise_key=row.get("exercise_key"), training_day=day,
                status="modified", prescribed={"display_name": row["display_name"]}, actual=values))
        _record(summaries, latest, identity, order, values, name=row["display_name"], source="manual",
                kind="canonical" if identity.startswith("key:") else "personal", confirmed=False, basis="reported")
        if identity not in manual_formats or order > manual_formats[identity][0]:
            manual_formats[identity] = (order, row["measurement_format"])
    for identity, summary in summaries.items():
        summary.latest_performance = performances.get(identity, (None, None))[1]
        if (identity.startswith("log:") and summary.latest_performance
                and summary.latest_performance.prescribed.get("prescription_source") == "athlete"):
            summary.latest_performance = performances.get("athlete-name:" + summary.name.strip().casefold(),
                                                         (None, summary.latest_performance))[1]
        # A saved private movement has the athlete's explicitly chosen recording type.
        # Catalogue layouts alone cannot override unknown or protected bank metadata.
        manual_type = manual_formats.get(identity, (None, None))[1] if identity.startswith("custom:") else None
        authored_type = (summary.latest_performance.prescribed.get("exercise_type")
                         if summary.latest_performance else None)
        context = progression_context({"exercise_key": identity[4:] if identity.startswith("key:") else None,
            "display_name": summary.name, "prescription_source": "athlete",
            "exercise_type": authored_type or (manual_type if manual_type in {"strength", "bodyweight"} else None),
            "progression_hold": (summary.latest_performance.prescribed.get("progression_hold")
                                 if summary.latest_performance else None)},
            None, None, phase="", d_day=None, athlete={})
        summary.progression_hold = context.get("progression_hold")
        summary.bodyweight_progression = context.get("bodyweight_progression")
        summary.allow_progression = allow_progression
        if identity[4:] in (held_keys or set()) or (summary.latest_performance and summary.latest_performance.reason == "pain"):
            summary.progression_hold = True
        if summary.latest_performance and summary.latest_performance.reason == "pain":
            summary.latest_performance = summary.latest_performance.model_copy(update={"reason": None})
        summary.best_loads.sort(key=lambda entry: entry.unit)
        # The athlete's own last layout for this movement, else what the record shows.
        summary.log_format = manual_formats[identity][1] if identity in manual_formats else values_format(summary.latest_values)
    return ExerciseLibraryResponse(exercises=sorted(summaries.values(), key=lambda item: (item.name.casefold(), item.id)), as_of=as_of)


def _record(summaries, latest, identity, order, values, *, name, source, kind, confirmed, basis):
    day = order[0]
    if identity not in summaries:
        summaries[identity] = PersonalExercise(id=identity, name="Exercise", identity_kind=kind,
            latest_day=day, latest_values=values, latest_confirmed_as_prescribed=False)
    summary = summaries[identity]
    if identity not in latest or order > latest[identity]:
        latest[identity] = order
        summary.name, summary.latest_day, summary.latest_values = name, day, values
        summary.latest_confirmed_as_prescribed, summary.latest_source = confirmed, source
    loads = [entry.load for entry in values.set_entries if entry.load is not None] if values.set_entries else [values.load]
    for load in loads:
        if load is None or load.value <= 0:
            continue
        before = next((entry for entry in summary.best_loads if entry.unit == load.unit), None)
        # A reported weight outranks an equal one only inferred from a confirmed prescription.
        if before is None or load.value > before.value or (
                load.value == before.value and basis == "reported" and before.basis != "reported"):
            summary.best_loads = [entry for entry in summary.best_loads if entry.unit != load.unit] + [RecordedExerciseLoad(
                value=load.value, unit=load.unit, training_day=day, basis=basis)]


def _manual_index_rows(store, athlete_id: str, as_of: str):
    """Every live entry once, by immutable ID; a malformed page fails the whole index."""
    after_id = None
    while True:
        rows = store.list_manual_exercise_index_page(athlete_id, as_of=as_of, after_id=after_id, limit=_BATCH_SIZE)
        if not rows:
            return
        ids = [str(row.get("id") or "") for row in rows]
        if ids != sorted(set(ids)) or not all(ids) or (after_id and ids[0] <= after_id):
            raise HTTPException(503, "Exercise history is temporarily unavailable.")
        for row in rows:
            if row.get("athlete_id") == athlete_id and not row.get("deleted_at") and (row.get("exercise_key") or row.get("personal_exercise_id")):
                yield row
        after_id = ids[-1]


def _selector(identity: str) -> tuple[str | None, str | None, str | None]:
    """(exercise key, legacy log ID, personal exercise ID); exactly one is set."""
    if identity.startswith("key:") and len(identity) <= 124 and _KEY.fullmatch(identity[4:]):
        return identity[4:], None, None
    try:
        if identity.startswith("log:"):
            return None, str(UUID(identity[4:])), None
        if identity.startswith("custom:"):
            return None, None, str(UUID(identity[7:]))
    except ValueError:
        pass
    raise HTTPException(422, "Invalid exercise record.")


def _chart_values(row: dict) -> ExerciseLogActual | None:
    actual = _actual(row)
    return performed_values(row, actual) if _performed(row, actual) else None


def _cutoff(value: str) -> str:
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        raise ValueError("missing timezone")
    return instant.isoformat()


def _ordered(read, keep, *, before_day, before_id, limit: int) -> list[dict]:
    """Up to ``limit + 1`` kept rows after the cursor, newest first.

    Do not mistake a lower server row cap or excluded rows for the end of this
    movement's history. Stop at one valid sentinel or an empty read.
    """
    rows: list[dict] = []
    while len(rows) <= limit:
        batch = read(before_day=before_day, before_id=before_id, limit=limit + 1 - len(rows))
        if not batch:
            break
        order = [(str(row.get("training_day") or "")[:10], str(row.get("id") or "")) for row in batch]
        if (order != sorted(set(order), reverse=True) or not all(day and id for day, id in order)
                or (before_day and order[0] >= (before_day, before_id))):
            raise HTTPException(503, "Exercise history is temporarily unavailable.")
        rows.extend(row for row in batch if keep(row))
        before_day, before_id = order[-1]
    return rows


def exercise_performances(store, athlete_id: str, identity: str, *, as_of: str | None = None,
                          cursor: str | None = None, limit: int = 30, health_consent_granted: bool) -> ExercisePerformancesResponse:
    key, log_id, personal_id = _selector(identity)
    before_day = before_id = None
    try:
        cutoff = _cutoff(as_of) if as_of else datetime.now(timezone.utc).isoformat()
        if cursor:
            data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if data["exercise"] != identity:
                raise ValueError("wrong exercise")
            cutoff = _cutoff(data["as_of"])
            before_day, before_id = date.fromisoformat(data["day"]).isoformat(), str(UUID(data["id"]))
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise HTTPException(422, "Invalid exercise history cursor.") from None
    rows: list[tuple[str, dict]] = []
    if personal_id is None:
        rows += [("plan", row) for row in _ordered(
            lambda **page: store.list_exercise_performances(athlete_id, exercise_key=key, log_id=log_id, as_of=cutoff, **page),
            lambda row: (row.get("athlete_id") == athlete_id and exercise_identity(row) == identity
                         and _map(row.get("prescribed")).get("block_type") not in _EXCLUDED_TYPES),
            before_day=before_day, before_id=before_id, limit=limit)]
    if log_id is None:
        rows += [("manual", row) for row in _ordered(
            lambda **page: store.list_manual_exercise_performances(athlete_id, exercise_key=key,
                personal_exercise_id=personal_id, as_of=cutoff, **page),
            lambda row: (row.get("athlete_id") == athlete_id and not row.get("deleted_at")
                         and entry_identity(row) == identity),
            before_day=before_day, before_id=before_id, limit=limit)]
    # One keyset across both sources: (training_day, id) is immutable and unique.
    rows.sort(key=lambda item: (str(item[1]["training_day"])[:10], str(item[1]["id"])), reverse=True)
    if not rows and not cursor:
        raise HTTPException(404, "Exercise history not found.")
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1][1]
        next_cursor = base64.urlsafe_b64encode(json.dumps({"exercise": identity, "as_of": cutoff,
            "day": str(last["training_day"])[:10], "id": last["id"]}).encode()).decode().rstrip("=")
    performances = []
    for source, row in page:
        if source == "plan":
            performances.append(ExercisePerformance(id=row["id"], training_day=row["training_day"],
                block=_exercise(_map(row.get("prescribed")), row, health_consent_granted), performed_values=_chart_values(row)))
        else:
            values = entry_actual(row)
            performances.append(ExercisePerformance(id=str(row["id"]), training_day=str(row["training_day"])[:10],
                block=_manual_block(row, health_consent_granted), performed_values=values))
    return ExercisePerformancesResponse(performances=performances, next_cursor=next_cursor)
