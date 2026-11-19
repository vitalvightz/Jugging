"""Turn someone's own plan (a PDF or Word file, pasted text, or screenshots)
into a draft.

A PDF or Word file never reaches the server: the browser pulls out plain text
and sends only that. Screenshots arrive re-encoded by the browser as plain
JPEGs (no metadata), are passed to the model once and are never stored. One structured model call reads it into weeks, sessions and
exercises; this module then builds an ordinary ``CustomPlanInput`` that the
person reviews and edits in the plan builder before anything is saved.
Nothing here writes to the database.

What is sent to the model is the plan text and screenshots alone: no name,
email, profile or training history. Both are untrusted data, never instructions.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import threading
import time
from difflib import SequenceMatcher
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field, PrivateAttr, ValidationError

from api.contracts.custom_plans import WEEKDAYS, CustomDose, CustomExerciseSnapshot, CustomPlanInput
from api.services.custom_plans import activity_block_type, dose_optional, session_activity
from api.services.plan_import_check import TIME, SourceCheck, week_heading, written_layout_flags
from api.structured_plan_models import ExercisePrescription
from api.services.plan_import_match import LibraryMatcher, linked
from api.services.plan_import_patch import DOSE_FIELDS, PATCH_INSTRUCTIONS, apply_patch, patch_schema
from api.services.plan_import_reading import (
    DEFAULT_IMPORT_MODEL, EXERCISE_KINDS, INSTRUCTIONS, JPEG_PREFIX, LIST_LABEL, MAX_EXERCISES, MAX_IMAGE_BYTES, MAX_IMAGES,
    MAX_SESSIONS_PER_WEEK, MAX_TEXT_CHARS, MAX_WEEKS, SESSION_TYPES, ReadExercise, ReadPlan, ReadSession, ReadWeek,
    _clean, _int, _number, _numbers, _unit, _written_dose, response_schema, session_days,
)
from api.services.plan_import_references import _session_date, resolve_references

logger = logging.getLogger(__name__)

# -- API contract --

class PlanImportRequest(BaseModel):
    text: str = Field("", max_length=MAX_TEXT_CHARS * 2)
    # Screenshots, re-encoded as JPEG in the browser, as data URLs.
    images: list[str] = Field(default_factory=list, max_length=MAX_IMAGES)
    file_name: str | None = Field(None, max_length=200)
    # The Monday the person's calendar starts on, worked out in their own time zone.
    start_date: date


class ImportFlag(BaseModel):
    """Something in the programme that couldn't be carried into the plan exactly.

    ``where`` names the session (and exercise) so the person can find it in the
    builder; ``message`` says what to check, quoting the coach where it helps."""
    where: str
    message: str
    # Where it is in the draft, so tapping it can open that session or exercise.
    session_id: str | None = None
    exercise_id: str | None = None


class NeedsDay(BaseModel):
    """A session the coach gave no day. The person picks the day before the draft
    opens; until then its sessions sit on their week's Monday as a placeholder
    (a dated plan needs a date) or have no weekday (a weekly plan)."""
    where: str
    session_ids: list[str]


class PlanImportResult(BaseModel):
    # A validated CustomPlanInput, sent as plain JSON: the builder posts it back
    # unchanged, and typing it here would split the request type in the web schema.
    plan: dict[str, Any]
    weeks: int
    sessions: int
    exercises: int
    flags: list[ImportFlag]
    needs_day: list[NeedsDay] = Field(default_factory=list)
    # Exercises linked to the app's library (their demo video comes with them),
    # and the library details the builder shows for each, by exercise id.
    matched: int = 0
    library: dict[str, CustomExerciseSnapshot] = Field(default_factory=dict)
    # Exercises whose numbers didn't match their line: what the second look fixes. Never sent.
    _review: list[dict] = PrivateAttr(default_factory=list)


# -- The model call --

class PlanReader(Protocol):
    def read(self, text: str, images: list[str]) -> ReadPlan: ...

    def patch(self, items: list[dict], timeout: float | None = None) -> list[dict]: ...


# Checks the second look can fix: a dose that doesn't match its own line.
REVIEWABLE = ("Check the dose",)


@dataclass
class OpenAIPlanReader:
    client: Any
    model: str
    max_output_tokens: int
    reasoning_effort: str

    def read(self, text: str, images: list[str]) -> ReadPlan:
        payload = json.dumps({"plan_text": text, "screenshots": len(images)}, ensure_ascii=False)
        content: list[dict[str, Any]] = [{"type": "input_text", "text": payload}]
        content += [{"type": "input_image", "image_url": image, "detail": "high"} for image in images]
        request: dict[str, Any] = {
            "model": self.model,
            "instructions": INSTRUCTIONS,
            "input": [{"role": "user", "content": content}] if images else payload,
            "text": {"format": response_schema()},
            "max_output_tokens": self.max_output_tokens,
            "store": False,
        }
        if self.reasoning_effort:
            request["reasoning"] = {"effort": self.reasoning_effort}
        response = self.client.responses.create(**request)
        output = getattr(response, "output_text", "") or ""
        return ReadPlan.model_validate_json(output)

    def patch(self, items: list[dict], timeout: float | None = None) -> list[dict]:
        """Corrected doses for just these exercises: small, and at low effort, so it's quick."""
        payload = json.dumps({"items": [{k: item[k] for k in ("id", "name", "line", "read", "problems")} for item in items]},
                             ensure_ascii=False)
        client = self.client.with_options(timeout=timeout) if timeout else self.client
        response = client.responses.create(model=self.model, instructions=PATCH_INSTRUCTIONS, input=payload,
                                           text={"format": patch_schema()}, max_output_tokens=4_000, store=False,
                                           reasoning={"effort": "low"})
        return json.loads(getattr(response, "output_text", "") or "{}").get("fixes") or []


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


_reader_cache: dict[str, PlanReader | None] = {}


def get_plan_reader() -> PlanReader | None:
    """The configured reader, or None without an API key or SDK.

    ``UNLXCK_PLAN_IMPORT_MODEL`` (default gpt-6-luna); same OPENAI_API_KEY as
    plan generation, whose model and settings are untouched.
    """
    if "default" in _reader_cache:
        return _reader_cache["default"]
    reader: PlanReader | None = None
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if api_key:
        try:
            from openai import OpenAI
        except ImportError:
            OpenAI = None  # noqa: N806
        if OpenAI is not None:
            reader = OpenAIPlanReader(
                client=OpenAI(api_key=api_key, timeout=float(_env_int("UNLXCK_PLAN_IMPORT_TIMEOUT_SECONDS", 90)),
                              max_retries=0),
                model=os.getenv("UNLXCK_PLAN_IMPORT_MODEL", DEFAULT_IMPORT_MODEL).strip() or DEFAULT_IMPORT_MODEL,
                max_output_tokens=_env_int("UNLXCK_PLAN_IMPORT_MAX_OUTPUT_TOKENS", 16_000),
                reasoning_effort=os.getenv("UNLXCK_PLAN_IMPORT_REASONING_EFFORT", "low").strip(),
            )
    _reader_cache["default"] = reader
    return reader


# A day's imports per person. Each one is a model call, so this keeps a stuck
# retry loop or a curious tester from running up cost. Kept in memory: a
# restart only gives everyone a fresh allowance, which is harmless.
_calls: dict[str, list[float]] = {}
_calls_lock = threading.Lock()


def reserve_import(owner_id: str, *, now: float | None = None) -> bool:
    limit = _env_int("UNLXCK_PLAN_IMPORT_DAILY_LIMIT", 10)
    moment = time.time() if now is None else now
    with _calls_lock:
        recent = [at for at in _calls.get(owner_id, []) if moment - at < 86_400]
        if len(recent) >= limit:
            _calls[owner_id] = recent
            return False
        _calls[owner_id] = [*recent, moment]
        return True


def reset_import_limits() -> None:
    with _calls_lock:
        _calls.clear()


# -- Building the draft --
#
# Everything the model read becomes an ordinary CustomPlanInput: the same
# schema, validation, builder and save path as a plan built by hand. Every
# exercise is a manual one, as written. Nothing written is dropped silently:
# a value the schema can't hold is kept as written text or flagged.

def corrections_flags(read: "ReadPlan", flag, source: str = "") -> None:
    """Spelling the reader corrected, listed so the person can see every change.
    A "correction" that changes a number, doesn't look like the written word, or
    (for pasted or file text) quotes a word that isn't in the programme is called
    out on its own."""
    written_text = " ".join(source.split()).lower()
    fixed = []
    for item in read.corrections[:30]:
        written, corrected = _clean(item.written, 80), _clean(item.corrected, 80)
        if not written or not corrected or written == corrected:
            continue
        similar = SequenceMatcher(None, written.lower(), corrected.lower()).ratio() >= 0.6
        if _numbers(written) != _numbers(corrected) or not similar or (written_text and written.lower() not in written_text):
            flag("Spelling", f"Check this change: \u201c{written}\u201d became \u201c{corrected}\u201d.")
        else:
            fixed.append(f"\u201c{written}\u201d \u2192 \u201c{corrected}\u201d")
    if fixed:
        flag("Spelling", "Corrected: " + ", ".join(fixed) + ".")


def _with_read_values(prescription: str | None, item: ReadExercise) -> str | None:
    """A text dose keeps every number read beside it. "4x8-10" read as sets 4 and
    "8-10" becomes "4 × 8-10", never just "8-10" with the sets lost."""
    if not prescription:
        return prescription
    if item.sets and re.match(r"^\s*\d+(?:\.\d+)?\s*[-\u2010-\u2015\u2212]\s*\d", prescription) and not re.search(r"\d\s*[x\u00d7*]\s*\d", prescription, re.I):
        return f"{_number(item.sets)} \u00d7 {prescription}"[:500]
    read_numbers = [n for n in _numbers(_written_dose(item))]
    missing = [n for n in read_numbers if n not in _numbers(prescription)]
    return f"{_written_dose(item)}, {prescription}"[:500] if missing else prescription


def dose_from(item: ReadExercise, flag=lambda message: None) -> CustomDose:
    """The builder's dose for what was written, in the mode it fits exactly.

    When the written values don't fit one mode (e.g. reps and a distance), the
    whole dose is kept as written text instead of dropping part of it. A value
    the builder can't hold (RPE 15) is flagged and kept in the notes by _exercise."""
    rest = _int(item.rest_seconds, 0, 3600)
    if item.rest_seconds is not None and rest is None:
        flag(f"Rest {_number(item.rest_seconds)}s is too long for the rest field. It's in the notes.")
    rpe = item.rpe if isinstance(item.rpe, (int, float)) and not isinstance(item.rpe, bool) and 1 <= item.rpe <= 10 else None
    if item.rpe is not None and rpe is None:
        flag(f"RPE {_number(item.rpe)} isn't 1-10. It's in the notes.")
    load = _clean(item.load)
    if load and len(load) > 80:
        load = None  # Kept whole in the notes by _exercise.
    tempo = _clean(item.tempo)
    tempo = tempo if tempo and len(tempo) <= 40 else None  # A longer one stays in the notes.
    common = {"rest_seconds": rest, "load_or_intensity": load, "rpe": rpe, "tempo": tempo,
              "per_side": True if item.per_side else None}
    if item.prescribed_sets:
        stages = [stage.model_copy(update={"per_side": "each side"}) if item.per_side and not stage.per_side else stage for stage in item.prescribed_sets]
        return CustomDose(mode="stages", prescribed_sets=stages, **common)
    sets, reps, seconds = _int(item.sets, 1, 100), _int(item.reps, 1, 1000), _int(item.seconds, 1, 36_000)
    rounds, work = _int(item.rounds, 1, 100), _int(item.work_seconds, 1, 36_000)
    distance = float(item.distance) if isinstance(item.distance, (int, float)) and 0 < item.distance <= 100_000 else None
    unit = _unit(item.distance_unit)
    written = {"sets": item.sets, "reps": item.reps, "seconds": item.seconds, "rounds": item.rounds,
               "work_seconds": item.work_seconds, "distance": item.distance}
    present = {key for key, value in written.items() if value is not None}
    prescription = _with_read_values(_clean(item.prescription, 500), item)
    mode: dict | None = None
    if prescription:
        mode = {"mode": "text", "prescription": prescription}
    elif present and present <= {"rounds", "work_seconds"} and (rounds or work) and all(
            v is not None for k, v in (("rounds", rounds), ("work_seconds", work)) if k in present):
        mode = {"mode": "intervals", "rounds": rounds, "work_seconds": work}
    elif present and present <= {"sets", "distance"} and distance and unit and (sets or "sets" not in present):
        mode = {"mode": "distance", "sets": sets, "distance": distance, "distance_unit": unit}
    elif present and present <= {"sets", "reps"} and reps and (sets or "sets" not in present):
        mode = {"mode": "reps", "sets": sets, "reps": reps}
    elif present and present <= {"sets", "seconds"} and seconds and (sets or "sets" not in present):
        mode = {"mode": "duration", "sets": sets, "duration_seconds": seconds}
    elif present:
        # Values that don't fit one mode, or that are out of range: keep the dose as written.
        mode = {"mode": "text", "prescription": _written_dose(item)[:500]}
    if mode:
        try:
            return CustomDose.model_validate({**common, **mode})
        except ValidationError:
            pass
    if not present and not prescription:
        flag("No dose written. Add one or leave it open.")
    try:
        return CustomDose.model_validate(common)
    except ValidationError:
        return CustomDose()


def _lines(values: list[str], *, limit: int = 10, length: int = 500) -> tuple[list[str] | None, list[str]]:
    """Up to ``limit`` lines that fit, and the rest (for the notes)."""
    lines = [line for line in (_clean(value) for value in values) if line]
    fits = [line for line in lines if len(line) <= length][:limit]
    return fits or None, [line for line in lines if line not in fits]


def _explicit_group_type(item: ReadExercise) -> str | None:
    if item.group_type in {"superset", "circuit", "alternating_sets"}:
        return item.group_type
    match = re.match(r"^(?:[A-Za-z]\s*[:—–-]\s*)?(superset|circuit|alternating sets)\b", item.group or "", re.I)
    return match[1].lower().replace(" ", "_") if match else None


def _coded_name(item: ReadExercise) -> tuple[str | None, str]:
    match = re.match(r"^\s*([A-Za-z][1-9][0-9]*)[.):]?\s+(?:[-—–]\s+)?(.+)$", item.name)
    code = item.exercise_code
    if code and not re.fullmatch(r"[A-Za-z][1-9][0-9]*", code):
        code = None
    return (code or match[1], match[2]) if match else (code, item.name)


def _row_prescription(item: ReadExercise) -> ExercisePrescription:
    # Retain numeric cells separately; notation without explicit units stays text.
    return ExercisePrescription(
        sets=item.sets, reps=item.reps,
        duration={"value": item.seconds, "unit": "seconds"} if item.seconds else None,
        rest={"value": item.rest_seconds, "unit": "seconds"} if item.rest_seconds is not None else None,
        work={"value": item.work_seconds, "unit": "seconds"} if item.work_seconds else None,
        rounds=item.rounds,
        distance={"value": item.distance, "unit": item.distance_unit} if item.distance and item.distance_unit else None,
        effort={"method": "RPE", "value": item.rpe} if item.rpe else None,
        per_side="each side" if item.per_side else None,
        source_text=" · ".join(str(v) for v in (
            item.prescription, item.load, item.notes,
            f"Tempo: {item.tempo}" if item.tempo else None,
            f"Equipment: {item.equipment}" if item.equipment else None,
            f"{item.blocks} blocks" if item.blocks else None,
            f"Block rest: {item.block_rest_seconds}s" if item.block_rest_seconds is not None else None,
            " / ".join(item.stages) if item.stages else None,
        ) if v) or None,
    )


def _consolidate_rows(exercises: list[ReadExercise], origins: dict[int, list[int]]) -> list[ReadExercise]:
    rows: list[ReadExercise] = []
    for source in exercises:
        item = source.model_copy(deep=True)
        code, name = _coded_name(item)
        item.exercise_code, item.name = code, name
        item.group_type = _explicit_group_type(item)
        previous = rows[-1] if rows else None
        same_code = previous and previous.exercise_code and code == previous.exercise_code
        continuation = previous and (previous.exercise_code or previous.prescribed_sets) and not name.strip() and (not code or same_code)
        if previous and not item.is_reference and (continuation or (same_code and name == previous.name)) and item.group_type in (None, previous.group_type) and (not item.group or item.group == previous.group):
            origins[id(previous)].append(id(source))
            was_uniform = not previous.prescribed_sets
            if was_uniform:
                first = _row_prescription(previous)
                previous.prescribed_sets = [first] if any(v is not None for v in first.model_dump().values()) else []
            stage = _row_prescription(item)
            previous.prescribed_sets.extend(item.prescribed_sets or ([stage] if any(v is not None for v in stage.model_dump().values()) else []))
            if was_uniform:
                for field in ("sets", "reps", "seconds", "rounds", "work_seconds", "rest_seconds", "distance", "distance_unit", "load", "rpe", "per_side", "prescription", "tempo", "blocks", "block_rest_seconds"):
                    setattr(previous, field, None)
            previous.cues = list(dict.fromkeys([*previous.cues, *item.cues]))
            previous.stop_rules = list(dict.fromkeys([*previous.stop_rules, *item.stop_rules]))
        else:
            rows.append(item)
            origins[id(item)] = [id(source)]
    return rows


def _exercise(item: ReadExercise, flag) -> dict | None:
    code, read_name = _coded_name(item)
    full_name = _clean(LIST_LABEL.sub("", read_name, count=1)) or _clean(read_name)
    if not full_name:
        written = _written_dose(item) or _clean(item.prescription) or _clean(item.notes, 80)
        if written:
            flag(f"Skipped an exercise with no name: \u201c{written}\u201d.")
        return None
    name = full_name[:100]
    # A warm-up, cooldown, stretch or mobility line is done, not dosed: no dose is not a gap.
    # In a timed section ("Core Routine (15 mins)") or a warm-up, cooldown or
    # mobility section, an exercise with no dose of its own is done within that
    # section: say so, without giving it a time of its own.
    section = _clean(item.group)
    if section and (TIME.search(section) or dose_optional(section)) and not re.search(r"\brounds?\b", section, re.I) \
            and not _written_dose(item) and not _clean(item.prescription):
        item = item.model_copy(update={"prescription": f"Within {section}"[:500]})
    block_type = EXERCISE_KINDS.get(item.kind or "") or activity_block_type(full_name)
    dose = dose_from(item, (lambda message: None) if session_activity(full_name, block_type) or (dose_optional(full_name, block_type) and not _written_dose(item)) else flag)
    cues, extra_cues = _lines(item.cues)
    stops, extra_stops = _lines(item.stop_rules)
    equipment = _clean(item.equipment)
    # Anything that didn't fit its own field is kept, whole, in the notes.
    notes = [_clean(item.notes)]
    if name != full_name:
        notes.append(f"Full name: {full_name}")
    load = _clean(item.load)
    if load and len(load) > 80:
        notes.append(f"Load: {load}")
    if equipment and len(equipment) > 120:
        notes.append(f"Equipment: {equipment}")
        equipment = None
    if item.rpe is not None and dose.rpe is None:
        notes.append(f"RPE as written: {_number(item.rpe)}")
    if _clean(item.tempo) and not dose.tempo:
        notes.append(f"Tempo: {_clean(item.tempo)}")
    if item.rest_seconds is not None and dose.rest_seconds is None:
        notes.append(f"Rest as written: {_number(item.rest_seconds)}s")
    notes += [f"Cue: {line}" for line in extra_cues] + [f"Stop rule: {line}" for line in extra_stops]
    # What a dose can't hold is kept as written and flagged, never flattened.
    blocks = _int(item.blocks, 2, 100)
    if blocks:
        between = _int(item.block_rest_seconds, 1, 3600)
        notes.append(f"Repeat: {blocks} blocks of these rounds" + (f", {_duration(between)} rest between blocks" if between else ""))
        flag(f"Written as {blocks} blocks of {_number(item.rounds) + ' rounds' if item.rounds else 'rounds'}"
             + (f" with {_duration(between)} rest between blocks" if between else "")
             + ". The dose is one block; the repeat is in the notes.")
    stages = [line for line in (_clean(value, 300) for value in item.stages) if line]
    if stages:
        notes.append("Each round, in order:\n" + "\n".join(stages))
        flag(f"{len(stages)} timed {'part' if len(stages) == 1 else 'parts'} per round, listed in the notes.")
    group = _clean(item.group, 200)
    if group:
        notes.append(f"Part of: {group}")
    text = "\n".join(line for line in notes if line) or None
    if text and len(text) > 2000:
        flag("Notes were longer than 2,000 characters and were shortened.")
        text = text[:2000]
    return {"id": str(uuid4()), "origin": "manual", "block_type": block_type, "name": name,
            **({"exercise_code": code} if code else {}),
            **({"group_type": item.group_type} if item.group_type in {"superset", "circuit", "alternating_sets"} else {}), "equipment": equipment, "notes": text,
            "coaching_cues": cues, "stop_rules": stops, "dose": dose.model_dump(exclude_none=True)}


def _duration(seconds: int) -> str:
    return f"{seconds // 60} min" if seconds % 60 == 0 else f"{seconds}s"


# "Day 12", "DAY 3 - Pads", "Day 30:" -- a programme's own day number, not a weekday.
DAY_NUMBER = re.compile(r"^\s*day\s*(\d{1,3})(?!\d)", re.I)


def _numbered_days(weeks: list[ReadWeek]) -> list[tuple[ReadSession, int]] | None:
    """Sessions of a sequential daily programme ("30-Day Challenge": Day 1 ... Day 30),
    each with its day number, when every session is numbered once, none has a written
    weekday or date, no week repeats, and the numbering runs past one week. Day 1-5
    with no weekdays stays a weekly template whose days the person picks."""
    items = [s for w in weeks for s in w.sessions]
    if not items or any(max(1, int(w.repeat or 1)) > 1 for w in weeks):
        return None
    numbered = []
    for item in items:
        match = DAY_NUMBER.match(item.name or "")
        if not match or session_days(item) or item.date or int(match[1]) < 1:
            return None
        numbered.append((item, int(match[1])))
    numbers = [n for _, n in numbered]
    return numbered if len(set(numbers)) == len(numbers) and max(numbers) > 7 else None


def build_draft(read: ReadPlan, start: date, source: str | None = None, matcher: LibraryMatcher | None = None) -> PlanImportResult:
    """The Custom Build draft for what was read. ``source`` is the programme text,
    used to double-check names and numbers: "" for screenshots (nothing to check
    against, which is flagged), None to skip the checks."""
    day_one = start
    start = start - timedelta(days=start.weekday())
    flags: list[ImportFlag] = []
    flagged: set[tuple] = set()
    flag_context = None
    library: dict[str, dict] = {}

    def flag(where: str, message: str):
        # A repeated week repeats its flags; each is listed once.
        key = (where, message, flag_context)
        if key not in flagged:
            flagged.add(key)
            flags.append(ImportFlag(where=where, message=message))

    # A rest week the programme writes keeps its place wherever it is, first
    # week included, so every week keeps its date. An empty week that isn't
    # written as rest is not something the coach wrote, and is left out.
    read = read.model_copy(deep=True, update={"imported_on": start})
    source_weeks = [week_heading(line) for line in (source or "").splitlines() if week_heading(line)]
    if any(numbers != {-1} for numbers in source_weeks):
        source_weeks = [numbers for numbers in source_weeks if numbers != {-1}]
    if len(source_weeks) == len(read.weeks):
        for week, numbers in zip(read.weeks, source_weeks):
            if week.week_index is None and min(numbers) > 0:
                week.week_index = min(numbers)
    if len(source_weeks) > len(read.weeks) and any(w.repeat > 1 for w in read.weeks):
        flag("Plan", "Separate written weeks were read as a repeated template. Check each week's exercises and doses.")
    for week_index, week in [*enumerate(read.weeks), (-1, ReadWeek(sessions=read.every_week))]:
        for session_index, raw_session in enumerate(week.sessions):
            raw_session._source_position = (week_index, session_index)
            for exercise_index, raw in enumerate(raw_session.exercises):
                raw._source_index = exercise_index
    checker = SourceCheck(source, read.corrections) if source and source.strip() else None
    source_messages: dict[tuple[int, int, int], list[str]] = {}
    source_missing: dict[tuple[int, int], list[str]] = {}

    def check_source(original: ReadPlan) -> None:
        # Check source rows before references copy them. A corrected original
        # must be the one copied into later sessions, with the same source identity.
        checker.plan = original
        for raw_session in [s for w in original.weeks for s in w.sessions] + original.every_week:
            checker.session = raw_session.name
            checker.context(raw_session)
            for raw in raw_session.exercises:
                if raw.is_reference or not (raw.name.strip() or raw.prescribed_sets):
                    continue
                occurrence = (*raw_session._source_position, raw._source_index)
                messages: list[str] = []
                mark = len(checker.failed)
                checker.check(raw, messages.append)
                source_messages[occurrence] = messages
                for failed in checker.failed[mark:]:
                    failed["occurrence"] = occurrence
            source_missing[raw_session._source_position] = checker.missing_exercises(raw_session)

    read = resolve_references(read, lambda where, message: flag(where, message), before_copy=check_source if checker else None)
    if checker:
        for wi, heading in checker.missing_sessions(read):
            flag(f"Week {read.weeks[wi].week_index or wi + 1}",
                 f"Possible missing session: {heading}. Compare with the source.")
    written_weeks = [week for week in read.weeks if week.sessions or week.rest]
    every_week = read.every_week
    if not any(week.sessions for week in written_weeks) and every_week:
        # Only a weekly schedule was written: that schedule is the week.
        written_weeks, every_week = [ReadWeek(sessions=every_week)], []
    if not any(week.sessions for week in written_weeks):
        raise HTTPException(422, "No training sessions were found in that programme.")
    # Each written week, repeated as written. A repeated week is flagged once,
    # under its week range ("Weeks 1-3"), not once per copy.
    runs = [(week, max(1, int(week.repeat or 1))) for week in written_weeks]
    expanded = [week.model_copy(update={"week_index": week.week_index + offset}) if week.week_index else week
                for week, count in runs for offset in range(count)]
    if len(expanded) > MAX_WEEKS:
        flag("Plan", f"The programme runs {len(expanded)} weeks. Plans hold up to {MAX_WEEKS}, so weeks {MAX_WEEKS + 1}-{len(expanded)} weren't brought in.")
        expanded = expanded[:MAX_WEEKS]
    labels: list[str] = []
    for week, count in runs:
        first_position = len(labels) + 1
        last_position = min(first_position + count - 1, len(expanded))
        if first_position > last_position:
            break
        first_number = week.week_index or first_position
        last = first_number + last_position - first_position
        labels += [f"Week {first_number}" if first_number == last else f"Weeks {first_number}-{last}"] * (last_position - first_position + 1)
        if not week.sessions:
            flag(labels[-1], "Written as a rest week, so it has no sessions.")
    name = _clean(read.name, 100) or "Imported plan"
    dated = len(expanded) > 1 or any(s.date or _session_date(s, read) for w in written_weeks for s in w.sessions)
    # Pasted or file text can be checked line by line; screenshots can't.

    def slots(week: ReadWeek, label: str) -> list[tuple[ReadSession, str | None, str]]:
        """The week's sessions, one per written day ("Tue/Thu/Sat" is three), then
        each every-week session on its days that the week's own sessions leave
        free, so an overview line never doubles a session written out in full.
        The same session written twice on one day is brought in once."""
        own = [(item, day, label) for item in week.sessions for day in (session_days(item) or [None])]
        taken = {day for _, day, _ in own if day}
        # An every-week session's checks are listed once, not once per week.
        recurring = [(item, day, "Every week \u00b7 " if label else "") for item in every_week
                     for day in session_days(item) if day not in taken]
        kept, seen = [], set()
        for item, day, where in own + recurring:
            key = (day, (_clean(item.name) or "").lower(), item.session_type)
            if day and key in seen:
                flag(f"{where}{_clean(item.name) or 'Session'}", f"Written twice on {day}, so it was brought in once.")
                continue
            seen.add(key)
            kept.append((item, day, where))
        return kept

    for item in every_week:
        if not session_days(item):
            flag("Every week", f"“{_clean(item.name, 80) or 'A session'}” has no day, so it wasn't added.")

    consolidated_rows: dict[int, list[ReadExercise]] = {}
    row_origins: dict[int, list[int]] = {}
    row_flags: dict[int, list[ImportFlag]] = {}

    def session(item: ReadSession, index: int, label: str, scheduled: date | None, day: str | None = None, programme_week: int | None = None) -> dict:
        nonlocal flag_context
        session_mark = len(flags)
        kind = item.session_type if item.session_type in SESSION_TYPES else "mixed"
        title = _clean(item.name) or ("Rest" if kind == "rest" else f"Session {index + 1}")
        where = f"{label}{title}"
        session_id = str(uuid4())
        session_where.setdefault(where, session_id)
        for missing in source_missing.get(item._source_position, []):
            flag(where, f"Possible missing exercise: {missing}. Compare with the source.")
        notes = _clean(item.instructions)
        if source is not None and not item.exercises and kind not in {"rest", "recovery"}:
            flag(where, "No exercises were read for this session. Compare it with the source.")
        if notes and len(notes) > 2000:
            flag(where, "The session note was longer than 2,000 characters and was shortened.")
        if checker:
            for exercise_index, raw in enumerate(item.exercises):
                if (raw.name.strip() or raw.prescribed_sets) and not raw.copied:
                    flag_context = (*item._source_position, raw._source_index)
                    mark = len(flags)
                    for message in source_messages.get((*item._source_position, raw._source_index), []):
                        flag(f"{where} · {_clean(raw.name, 60)}", message)
                    row_flags.setdefault(id(raw), []).extend(flags[mark:])
        if id(item) not in consolidated_rows:
            consolidated_rows[id(item)] = _consolidate_rows(item.exercises, row_origins)
        item = item.model_copy(update={"exercises": consolidated_rows[id(item)]})
        if len(item.exercises) > MAX_EXERCISES:
            flag(where, f"Only the first {MAX_EXERCISES} of {len(item.exercises)} exercises were brought in.")
        exercises = []
        for group in dict.fromkeys(_clean(e.group, 200) for e in item.exercises[:MAX_EXERCISES]):
            if group and re.search(r"\brounds?\b", group, re.I):
                flag(where, f"Circuit \u201c{group}\u201d: its rounds are each exercise's sets.")
        for exercise in item.exercises[:MAX_EXERCISES]:
            flag_context = (*item._source_position, exercise._source_index)
            def say(message, e=exercise, where=where):
                flag(f"{where} \u00b7 {_clean(e.name, 60)}" if _clean(e.name) else where, message)

            # Numbers are checked against the written line first, so one the line
            # settles is fixed before the exercise is built.
            mark = len(flags)
            # A copy made from a reference was flagged where it was first written.
            built = _exercise(exercise, (lambda message: None) if exercise.copied else say)
            if built and matcher:
                found = matcher.match(built["name"])
                swapped = linked(built, found) if found else None
                if swapped:
                    if found.kind == "close":
                        say(f"Matched to the library exercise \u201c{found.name}\u201d. Check it's the same exercise.")
                    library[swapped["id"]] = matcher.snapshot(found)
                    built = swapped
            if built:
                exercises.append(built)
                original_flags = [raised for original in row_origins[id(exercise)] for raised in row_flags.get(original, [])]
                for raised in [*flags[mark:], *original_flags]:
                    raised.exercise_id = raised.exercise_id or built["id"]
        for raised in flags[session_mark:]:
            raised.session_id = raised.session_id or session_id
        flag_context = None
        return {"id": session_id, "name": title[:100], **({"programme_week": programme_week} if programme_week else {}), "weekday": None if scheduled else day,
                "scheduled_date": scheduled.isoformat() if scheduled else None, "session_type": kind,
                "notes": notes[:2000] if notes else None, "exercises": exercises}

    # Sessions with no written day, grouped so a repeated week asks once.
    unplaced: dict[int, NeedsDay] = {}

    def needs_day(item: ReadSession, where: str, built: dict):
        unplaced.setdefault(id(item), NeedsDay(where=where, session_ids=[])).session_ids.append(built["id"])

    sessions: list[dict] = []
    session_where: dict[str, str] = {}
    numbered = _numbered_days(written_weeks)
    if numbered:
        # A sequential daily programme: Day N is the (N-1)th day after Day 1, so
        # Day 8 is in the next calendar week, never back on Day 1's weekday.
        try:
            day_one = date.fromisoformat(read.start_date) if read.start_date else day_one
        except ValueError:
            pass
        numbered.sort(key=lambda pair: pair[1])
        last = numbered[-1][1]
        kept = [(item, n) for item, n in numbered if n <= 7 * MAX_WEEKS]
        if len(kept) < len(numbered):
            flag("Plan", f"The programme runs {last} days. Plans hold up to {7 * MAX_WEEKS}, so days "
                         f"{7 * MAX_WEEKS + 1}-{last} weren't brought in.")
        found = {n for _, n in kept}
        missing = [n for n in range(1, kept[-1][1] + 1) if n not in found]
        if missing:
            flag("Plan", f"Read {len(found)} of {kept[-1][1]} days. Day{'s' if len(missing) > 1 else ''} "
                         f"{', '.join(map(str, missing))} weren't found. Compare with the source.")
        for index, (item, n) in enumerate(kept):
            sessions.append(session(item, index, "", day_one + timedelta(days=n - 1), programme_week=(n - 1) // 7 + 1))
        for item in every_week:
            flag("Every week", f"\u201c{_clean(item.name, 80) or 'A session'}\u201d is written for weekdays, but this programme runs by numbered days, so it wasn't added.")
        expanded = [ReadWeek()] * ((kept[-1][1] - 1) // 7 + 1)
        body = {"name": name, "schedule_mode": "dated", "start_date": day_one.isoformat(),
                "end_date": (day_one + timedelta(days=kept[-1][1] - 1)).isoformat(), "sessions": sessions}
    elif not dated:
        # One week, repeating: the builder's weekly plan. A session without a
        # written day stays unplaced until the person picks its day.
        items = slots(expanded[0], "")
        if len(items) > MAX_SESSIONS_PER_WEEK:
            flag("Plan", f"Only the first {MAX_SESSIONS_PER_WEEK} of {len(items)} sessions in the week were brought in.")
            items = items[:MAX_SESSIONS_PER_WEEK]
        for index, (item, day, where) in enumerate(items):
            sessions.append(session(item, index, where, None, day, expanded[0].week_index))
            if not day:
                needs_day(item, sessions[-1]["name"], sessions[-1])
        body = {"name": name, "schedule_mode": "weekly", "sessions": sessions}
    else:
        stated = None
        try:
            stated = date.fromisoformat(read.start_date) if read.start_date else None
        except ValueError:
            stated = None
        # No start date, but the sessions carry dates: the first one starts the plan.
        if stated is None:
            dated_sessions = [d for d in (_session_date(s, read) for w in read.weeks for s in w.sessions) if d]
            stated = min(dated_sessions) if dated_sessions else None
        if stated and stated >= start - timedelta(days=7):
            first = stated - timedelta(days=stated.weekday())
        else:
            first = start
            flag("Plan", f"No start date written, so it starts Monday {first.day} {first.strftime('%b')}.")
        for number, week in enumerate(expanded):
            items = slots(week, f"{labels[number]} \u00b7 ") if week.sessions else []
            if len(items) > MAX_SESSIONS_PER_WEEK:
                flag(labels[number], f"Only the first {MAX_SESSIONS_PER_WEEK} of {len(items)} sessions were brought in.")
                items = items[:MAX_SESSIONS_PER_WEEK]
            monday = first + timedelta(days=7 * number)
            once = max(1, int(week.repeat or 1)) == 1
            for index, (item, day, where) in enumerate(items):
                # A written calendar date places the session (a programme week can run
                # Wednesday to Monday); otherwise its weekday in this week. No day
                # written: never guessed. It waits on Monday until the person picks its day.
                programme_start = (stated or first) + timedelta(days=7 * number)
                written = _session_date(item, read, programme_start) if once else None
                if written and not (first <= written < first + timedelta(days=7 * MAX_WEEKS)) or (written and day and written.strftime("%A") != day):
                    written = None
                fallback = programme_start + timedelta(days=(WEEKDAYS.index(day) - programme_start.weekday()) % 7) if day else monday
                sessions.append(session(item, index, where, written or fallback, programme_week=week.week_index))
                if not day:
                    needs_day(item, f"{labels[number]} \u00b7 {sessions[-1]['name']}", sessions[-1])
        body = {"name": name, "schedule_mode": "dated", "start_date": first.isoformat(),
                "end_date": max([first + timedelta(days=7 * len(expanded) - 1)]
                                + [(lambda d: d + timedelta(days=6 - d.weekday()))(date.fromisoformat(x["scheduled_date"])) for x in sessions]).isoformat(),
                "sessions": sessions}
    if source and source.strip():
        # A session still waiting for its day isn't placed on one yet.
        waiting = {sid for group in unplaced.values() for sid in group.session_ids}
        placed = {s["weekday"] if s.get("weekday") else WEEKDAYS[date.fromisoformat(s["scheduled_date"]).weekday()]
                  for s in sessions if s["id"] not in waiting and (s.get("weekday") or s.get("scheduled_date"))}
        written_layout_flags(source, len(expanded), placed, flag, {w.week_index for w in expanded if w.week_index})
    corrections_flags(read, flag, source or "")
    if source is not None and not source.strip():
        flag("Plan", "Read from screenshots, so doses weren't double-checked. Compare them with your screenshots.")
    for line in read.skipped[:10]:
        if _clean(line):
            flag("Not placed", f"\u201c{_clean(line, 300)}\u201d")
    try:
        plan = CustomPlanInput.model_validate(body)
    except ValidationError as exc:
        logger.warning("plan import draft failed validation: %s", exc.errors()[:3])
        raise HTTPException(422, "That programme couldn't be read. Try a simpler file, or build it by hand.") from exc
    for raised in flags:
        # A flag on a session, or one of its exercises, opens that session.
        raised.session_id = raised.session_id or next((sid for where, sid in session_where.items()
                                  if raised.where == where or raised.where.startswith(f"{where} \u00b7 ")), None)
        if raised.exercise_id and not raised.session_id:
            raised.exercise_id = None
    result = PlanImportResult(plan=plan.model_dump(mode="json"), weeks=len(expanded), sessions=len(plan.sessions),
                              exercises=sum(len(s.exercises) for s in plan.sessions), flags=flags,
                              needs_day=list(unplaced.values()), matched=len(library), library=library)
    result._review = [{**item, "id": n} for n, item in enumerate(checker.failed if checker else [])]
    return result


def library_matcher(store) -> LibraryMatcher | None:
    """The exercise library with its demo videos, or None: matching is a bonus, never a reason to fail an import."""
    try:
        from api.services.exercise_media import load_media_index
        from api.services.training_library import cache
        return LibraryMatcher(cache.get().records, load_media_index(store) if store is not None else {})
    except Exception:  # noqa: BLE001
        logger.warning("exercise library unavailable for plan import matching", exc_info=True)
        return None


def checked_images(images: list[str]) -> list[str]:
    """Only small baseline JPEG data URLs; anything else is refused, never forwarded."""
    checked = []
    for image in images:
        if not isinstance(image, str) or not image.startswith(JPEG_PREFIX):
            raise HTTPException(422, "Screenshots must be images. Try again.")
        try:
            data = base64.b64decode(image[len(JPEG_PREFIX):], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(422, "A screenshot couldn't be read. Try again.") from exc
        if len(data) > MAX_IMAGE_BYTES or not data.startswith(b"\xff\xd8\xff") or not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
            raise HTTPException(422, "A screenshot couldn't be read. Try again.")
        checked.append(JPEG_PREFIX + base64.b64encode(data).decode("ascii"))
    return checked


def import_plan(owner_id: str, body: PlanImportRequest, *, reader: PlanReader | None, store=None) -> PlanImportResult:
    if reader is None:
        raise HTTPException(503, "Plan import isn't available right now.")
    images = checked_images(body.images)
    text = "\n".join(line.rstrip() for line in body.text.replace("\r", "").split("\n")).strip()
    if not images and len(" ".join(text.split())) < 20:
        raise HTTPException(422, "That has almost no text. Scanned plans can't be read as a file; add screenshots of the pages instead.")
    shortened = len(text) > MAX_TEXT_CHARS
    text = text[:MAX_TEXT_CHARS]
    if not reserve_import(owner_id):
        raise HTTPException(429, "You've imported several plans today. Try again tomorrow.", headers={"Retry-After": "3600"})
    try:
        started = time.monotonic()
        read = reader.read(text, images)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - the model or network; never leak details
        logger.warning("plan import model call failed: %s", type(exc).__name__)
        raise HTTPException(502, "That plan couldn't be read just now. Try again in a minute.") from exc
    matcher = library_matcher(store)
    result = build_draft(read, body.start_date, text, matcher)
    # The second look only runs in time the request has left: a slow first read
    # returns as it is rather than holding the connection past the host's limit.
    left = _env_int("UNLXCK_PLAN_IMPORT_BUDGET_SECONDS", 55) - (time.monotonic() - started)
    if left >= SECOND_LOOK_MIN_SECONDS:
        result = _second_look(reader, read, result, lambda again: build_draft(again, body.start_date, text, matcher), left)
    if shortened:
        result.flags.insert(0, ImportFlag(where="Plan", message=f"The programme was longer than {MAX_TEXT_CHARS:,} characters, so only the first part was read."))
    return result


def _reviewable(result: PlanImportResult) -> list[ImportFlag]:
    return [f for f in result.flags if f.message.startswith(REVIEWABLE)]


# Below this many seconds left, a second look can't finish in time: it is skipped.
SECOND_LOOK_MIN_SECONDS = 10
# The most exercises sent for a fix at once: keeps the call small and quick.
MAX_PATCH_ITEMS = 30


def _second_look(reader: PlanReader, read: ReadPlan, result: PlanImportResult, build,
                 timeout: float | None = None) -> PlanImportResult:
    """When doses don't match their own written line, one small, quick call fixes
    just those exercises (see plan_import_patch), and the code puts the fixed
    doses back. Part of the same import (no extra allowance used). Kept only if it
    keeps every session and exercise, fails fewer checks and adds no other flags;
    otherwise the first reading stands. Screenshots have no line to check against."""
    items = result._review[:MAX_PATCH_ITEMS]
    checks = _reviewable(result)
    if not items or not checks:
        return result
    try:
        fixes = reader.patch(items, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - a failed second look keeps the first reading
        logger.info("plan import second look failed: %s", type(exc).__name__)
        return result
    # Verify each change independently. A good correction must never conceal a
    # second correction that still has unsupported values or deleted work.
    for item in items:
        try:
            matching = [f for f in fixes if isinstance(f, dict) and f.get("id") == item["id"]]
            if len(matching) != 1 or not any(matching[0].get(f) for f in DOSE_FIELDS):
                continue
            candidate = apply_patch(read, [item], fixes)
            again = build(candidate)
        except Exception:  # noqa: BLE001 - invalid provider fields leave the original intact
            continue
        unresolved = any(tuple(f.get("occurrence", ())) == tuple(item["occurrence"]) for f in again._review)
        if (not unresolved and _coverage(again) == _coverage(result)
                and len(_reviewable(again)) < len(_reviewable(result))
                and {(f.where, f.message) for f in again.flags if not f.message.startswith(REVIEWABLE)}
                <= {(f.where, f.message) for f in result.flags if not f.message.startswith(REVIEWABLE)}):
            read, result = candidate, again
    return result


def _coverage(result: PlanImportResult) -> tuple[int, int]:
    """Sessions and exercises brought in. A second reading never wins by dropping
    a hard-to-read exercise or session: it must keep at least as many of each."""
    return result.sessions, result.exercises

