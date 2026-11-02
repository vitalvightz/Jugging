"""Block 4 Today/Overview orchestration: connect the pure contracts in
``api/contracts`` to persistence (``api/store.py``).

Everything here is server-authoritative:

* the **training day** is computed from the athlete's timezone (03:00 rollover),
  never supplied by the client;
* the **recommendation** is computed by the readiness-message engine and persisted on the
  check-in row — the client never calculates or supplies it;
* the **command view** and **landing** state are derived from persisted rows.

No saved plan is ever mutated here. Readiness text comes from the deterministic
message engine.
"""

from __future__ import annotations

import json
import logging
import math
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timezone, timedelta
from typing import Any, Callable, Iterator, Mapping, NamedTuple, Sequence

from fastapi import HTTPException, status
from pydantic import ValidationError

from fightcamp.injury_body_region import injury_body_region_context
from fightcamp.session_sequencing import is_support_session as _is_day_support_session
from fightcamp.weekly_schedule_view import normalize_weekday
from fightcamp.rehab_clinical import load_clinical_policies
from fightcamp.rehab_protocols import get_rehab_bank
from fightcamp.surface_wound_safety import sanitize_surface_guidance
from api.contracts.injury_policy import resolve_injury_policy, reconcile_session_prescription, rehab_allocation_count
from api.contracts.clinician_clearance import effective_clinician_clearance
from api.services.injury_episode_service import apply_episode_observations, episode_observations, delayed_rehab_prompts, exposure_rows_with_observations

from api.contracts.command_view import CommandView, RiskWatchItem, build_command_view, make_risk
from api.contracts.completion import (
    TERMINAL_COMPLETION_STATUSES,
    SessionCompletionRecord,
    completion_landing_state,
    completion_status_of,
)
from api.contracts.injury_checkin import (
    MAX_INFECTION_SIGNS,
    DeclaredInjury,
    build_injury_label,
    injury_consequence_tier,
    open_injury_flag_risks,
    reconcile_injury_checkin,
)
from api.contracts.injury_signal import derive_injury_signal
from api.contracts.rehab_exposure import injury_evidence_identity
from api.contracts.landing import LandingDecision, resolve_landing
from api.contracts.rehab_stage import resolve_rehab_stages
from api.contracts.readiness_message import (
    ReadinessAdjustment,
    ReadinessCheckin,
    ReadinessContext,
    active_medical_hold_reasons,
    build_readiness_adjustment,
    classify_injury_surface,
    is_support_session,
    restricts_training,
    surface_wound_medical_review,
)
from api.contracts.training_day import resolve_training_day_str
from api.store import AppStore
from api.services.active_plan import ActivePlanResolution, resolve_active_plan
from api.services.effective_structured_plan import resolve_effective_structured_plan
from api.services.exercise_overrides import apply_exercise_overrides
from api.services.plan_schedule import (
    has_scheduled_day_content,
    parse_iso_date,
    resolve_current_week,
    resolve_today_and_next,
    weekly_schedule_or_none,
)
from api.services.open_plan_timeline import (
    WEEKDAYS as WEEKDAY_TOKENS,
    open_plan_anchor_date,
    open_plan_spec,
    project_open_structured_plan,
)
from api.services.readiness_failsafe import (
    CHECKINS_UNAVAILABLE,
    COMPLETIONS_UNAVAILABLE,
    INJURY_CONTEXT_UNAVAILABLE,
    SPARRING_UNAVAILABLE,
    INTAKE_UNAVAILABLE,
    SESSION_UNAVAILABLE,
    ContextStatusBuilder,
    ReadinessContextStatus,
    apply_context_failsafe,
    build_readiness_signal,
)
from api.datetimes import parse_calendar_date as _parse_structured_date
from api.optional_sessions import is_optional_session

logger = logging.getLogger(__name__)


_CHECKIN_INPUT_FIELDS = (
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
)

TODAY_CHECKIN_WARNING = (
    "You already completed a check-in today. "
    "This response applies to the current active plan only."
)

INTAKE_INJURY_SOURCE = "intake"
INTAKE_CLEARED_VALUES = frozenset({"1", "true", "yes", "y", "cleared", "clear", "resolved", "done"})
GUIDED_INJURY_SEVERITY_MAP = {
    "low": "mild",
    "mild": "mild",
    "moderate": "moderate",
    "high": "severe",
    "severe": "severe",
}
BODY_MAP_ZONE_LABELS = {
    "head": "Head / Neck",
    "l_shoulder": "Left shoulder",
    "r_shoulder": "Right shoulder",
    "chest": "Chest",
    "upper_back": "Upper back",
    "lower_back": "Lower back",
    "core": "Core",
    "l_elbow": "Left elbow",
    "r_elbow": "Right elbow",
    "l_wrist": "Left wrist",
    "r_wrist": "Right wrist",
    "l_hip": "Left hip",
    "r_hip": "Right hip",
    "l_glute": "Left glute",
    "r_glute": "Right glute",
    "l_quad": "Left quad",
    "r_quad": "Right quad",
    "l_ham": "Left hamstring",
    "r_ham": "Right hamstring",
    "l_knee": "Left knee",
    "r_knee": "Right knee",
    "l_shin": "Left shin",
    "r_shin": "Right shin",
    "l_calf": "Left calf",
    "r_calf": "Right calf",
    "l_ankle": "Left ankle",
    "r_ankle": "Right ankle",
}


def _require_valid_plan_id(plan_id: str) -> None:
    """422 on a malformed plan_id so it never reaches the DB as a uuid syntax error."""
    try:
        uuid.UUID(plan_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="plan_id must be a valid UUID",
        ) from exc


def resolve_training_day(athlete_timezone: str | None, *, now: datetime | None = None) -> str:
    """Athlete-local training day (``YYYY-MM-DD``) — the canonical key."""
    return resolve_training_day_str(
        now or datetime.now(timezone.utc),
        athlete_timezone=athlete_timezone,
    )


def _open_plan_has_not_started(plan_row: Mapping[str, Any], training_day: str) -> bool:
    """True while an open plan is waiting for its first block anchor."""

    if open_plan_spec(plan_row) is None:
        return False
    anchor = open_plan_anchor_date(plan_row)
    current = parse_iso_date(training_day)
    return anchor is not None and current is not None and current < anchor


def _dated_plan_has_not_started(plan_row: Mapping[str, Any], training_day: str) -> bool:
    """True on a day before a dated plan's first card day.

    A fight camp generated today usually starts tomorrow. Its weekly template is
    keyed by weekday, so without this boundary today's weekday slot (often an
    empty one) resolves as today's session: the athlete saw a held, untitled
    "No training load" session instead of the plan's first day as next.
    """
    if open_plan_spec(plan_row) is not None:
        return False
    current = parse_iso_date(training_day)
    if current is None:
        return False
    first = min(
        (
            parsed
            for week in _structured_plan_weeks(plan_row, training_day=training_day)
            for day in _iter_mapping_items(week.get("days"))
            if (parsed := parse_iso_date(_clean_text(day.get("date"))[:10])) is not None
        ),
        default=None,
    )
    return first is not None and current < first


def _plan_has_not_started(plan_row: Mapping[str, Any], training_day: str) -> bool:
    """Before the plan's first day, its weekly rhythm schedules nothing today."""
    return _open_plan_has_not_started(plan_row, training_day) or _dated_plan_has_not_started(
        plan_row, training_day
    )


def _readiness_checkin_from(payload: Mapping[str, Any]) -> ReadinessCheckin:
    # A plan-free check-in has no camp phase; the engine reads "" as none.
    return ReadinessCheckin(**{field: payload[field] if field != "phase" else payload[field] or ""
                               for field in _CHECKIN_INPUT_FIELDS})


def _stored_readiness_checkin_from(
    row: Mapping[str, Any],
    *,
    active_injury_override: str | None = None,
) -> ReadinessCheckin:
    defaults = ReadinessCheckin()
    values = {field: row.get(field, getattr(defaults, field)) for field in _CHECKIN_INPUT_FIELDS}
    if active_injury_override:
        values["active_injury"] = active_injury_override
    return ReadinessCheckin(**values)


def _same_day_checkin_warnings(
    store: AppStore,
    *,
    athlete_id: str,
    plan_id: str,
    training_day: str,
    include_current_plan: bool = False,
) -> list[str]:
    rows = store.list_today_checkins_for_day(athlete_id, training_day) or []
    if include_current_plan:
        has_same_day_checkin = bool(rows)
    else:
        has_same_day_checkin = any(str(row.get("plan_id") or "") != plan_id for row in rows)
    return [TODAY_CHECKIN_WARNING] if has_same_day_checkin else []


def _checked_recent_today_checkins(
    store: AppStore, athlete_id: str, *, limit: int = 4
) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(rows, ok)``. ``ok`` is False only when the read RAISED — a
    genuinely empty history is a healthy ``ok=True`` case. A raised read must
    not look like "no history"."""
    try:
        return list(store.list_today_checkins(athlete_id, limit=limit) or []), True
    except Exception:
        logger.exception("[today] recent_checkins_read_failed athlete_id=%s", athlete_id)
        return [], False


def _checked_today_checkin(
    store: AppStore, athlete_id: str, plan_id: str | None, training_day: str
) -> dict[str, Any] | None:
    """Today's stored check-in row, or ``None``. Never raises.

    Read for context only (the rehab-stage resolver's "current day"), so a
    failure degrades to "no check-in today" rather than failing the caller.
    """
    if not plan_id:
        return None
    try:
        return store.get_today_checkin(athlete_id, plan_id, training_day)
    except Exception:
        logger.exception("[today] today_checkin_read_failed athlete_id=%s", athlete_id)
        return None


def _checked_recent_session_completions(
    store: AppStore, athlete_id: str, *, limit: int = 3
) -> tuple[list[dict[str, Any]], bool]:
    try:
        return list(store.list_session_completions(athlete_id, limit=limit) or []), True
    except Exception:
        logger.exception("[today] recent_completions_read_failed athlete_id=%s", athlete_id)
        return [], False


def _entry_mapping_for_readiness(entry: Any) -> dict[str, Any]:
    if entry is None:
        return {}
    if isinstance(entry, Mapping):
        return dict(entry)
    keys = (
        "session_id",
        "title",
        "label",
        "status",
        "reason",
        "coach_note",
        "effective_load",
        "primary_focus",
        "emphasis",
        "weekday",
        "calendar_date",
    )
    return {key: getattr(entry, key) for key in keys if getattr(entry, key, None) not in (None, "")}


def _resolve_today_session_entry(plan_row: Mapping[str, Any], training_day: str) -> dict[str, Any]:
    """Resolve today's session mapping for readiness. MAY RAISE on malformed plan
    data — callers decide whether that is a best-effort skip or a degraded-context
    signal. An empty ``{}`` means "no scheduled session today" (a rest day), which
    is a normal, non-failure result."""
    structured_today = _structured_today(plan_row, training_day)
    if structured_today.entry:
        return dict(structured_today.entry)
    # The card has a row for today and it schedules nothing: a rest day, which is
    # the normal empty result — not a reason to fall back to the weekly template.
    if structured_today.is_rest_day:
        return {}

    # A renewable plan created Fri-Sun starts on the coming Monday. Its weekly
    # template still contains the current weekday, but that recurring rhythm is
    # not live before the anchor and must never become today's session.
    if _plan_has_not_started(plan_row, training_day):
        return {}

    training_date = parse_iso_date(training_day)
    if training_date is None:
        return {}
    _week_index, week = resolve_current_week(plan_row, today=training_date)
    today_entry, _next_entry = resolve_today_and_next(week, today=training_date)
    if not has_scheduled_day_content(today_entry):
        return {}
    return _entry_mapping_for_readiness(today_entry)


def _today_session_for_readiness(plan_row: Mapping[str, Any], training_day: str) -> dict[str, Any]:
    """Best-effort today's-session resolution (display/completion callers). A
    malformed plan degrades to ``{}`` and never crashes the caller."""
    try:
        return _resolve_today_session_entry(plan_row, training_day)
    except Exception:
        return {}


def _checked_today_session_for_readiness(
    plan_row: Mapping[str, Any], training_day: str
) -> tuple[dict[str, Any], bool]:
    """Readiness-path today's-session resolution. Returns ``(entry, ok)`` where
    ``ok=False`` marks a resolution FAILURE (degraded context) so a session whose
    risk we cannot classify never silently reads as a safe/rest day."""
    try:
        return _resolve_today_session_entry(plan_row, training_day), True
    except Exception:
        logger.exception("[today] session_resolution_failed training_day=%s", training_day)
        return {}, False


def _checked_intake_payload_for_readiness(
    store: AppStore, plan_row: Mapping[str, Any]
) -> tuple[Mapping[str, Any], bool]:
    """Return ``(intake_payload, ok)``. A missing ``intake_id`` is a normal
    ``ok=True`` empty result; only a RAISED read marks intake context
    unavailable (degraded)."""
    intake_id = str(plan_row.get("intake_id") or "").strip()
    if not intake_id:
        return {}, True
    try:
        return _intake_payload_from_row(store.get_intake(intake_id)), True
    except Exception:
        logger.exception("[today] intake_read_failed intake_id=%s", intake_id)
        return {}, False


def _checked_open_injury_flags(
    store: AppStore, athlete_id: str
) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(open_flags, ok)``. Injury state is the most safety-critical
    input, so a RAISED read is a hard signal (``ok=False``) — we cannot rule out a
    severe injury, and an empty list must not be assumed."""
    try:
        flags = store.list_injury_flags(athlete_id, statuses=("open", "monitoring"))
        return [dict(flag) for flag in (flags or [])], True
    except Exception:
        logger.exception("[today] injury_flags_read_failed athlete_id=%s", athlete_id)
        return [], False


# Surface classes the surface evaluator routes itself (contact restriction /
# local protection). A worse report on one of these must NOT be escalated into
# the blanket "active injury worse" stop.
_SURFACE_ROUTED_CLASSES = frozenset(
    {"stable_surface", "surface_local_restriction", "surface_no_contact"}
)

_INJURY_SEVERITY_RANK = {"mild": 0, "moderate": 1, "severe": 2}
_SURFACE_SEVERITY_FLOOR = {
    "stable_surface": "mild",
    "surface_local_restriction": "moderate",
    "surface_no_contact": "moderate",
    "surface_medical_review": "severe",
}


def _stronger_severity(*values: str) -> str:
    return max(values, key=lambda value: _INJURY_SEVERITY_RANK.get(value, -1))


def _sync_surface_severity_from_checkin(
    declared: Sequence[DeclaredInjury],
    open_flags: Sequence[Mapping[str, Any]],
) -> tuple[list[DeclaredInjury], dict[str, dict[str, Any]]]:
    """Apply — and release — a server-owned severity floor from wound answers.

    The follow-up answers already determine the canonical surface class. Reuse
    that result instead of trusting the browser to choose a matching severity:
    local/open-wound restrictions are at least moderate, while infection,
    uncontrolled bleeding, drainage, or another medical-review result is severe.

    The floor is not one-way. Every write records who owns the stored severity
    (``severity_source``) and, while a floor is in force, the athlete's own value
    underneath it (``manual_severity``). A later recheck is therefore evaluated
    against the athlete's severity, not against the floor the system last
    applied: clean answers drop an infected wound back to what the athlete
    actually reported, while a severity the athlete chose is never lowered by the
    system. Returns the declarations with their effective severity plus the
    provenance fields to persist, keyed by flag id — provenance is server-owned
    and deliberately absent from the client contract.
    """
    current_by_id = {
        str(flag.get("id")): dict(flag)
        for flag in open_flags
        if str(flag.get("id") or "").strip()
    }
    synced: list[DeclaredInjury] = []
    provenance: dict[str, dict[str, Any]] = {}
    for injury in declared:
        surface_fields = injury.surface_safety_fields()
        if injury.status != "worse" and not surface_fields:
            # No wound evidence in this report. A floor is only ever released by
            # answers that say the wound is better, never by silence.
            synced.append(injury)
            continue

        current = current_by_id.get(str(injury.flag_id or ""), {})
        # The conditional surface follow-up updates an existing tracked wound.
        # A brand-new API declaration has no prior severity to synchronize and
        # keeps the normal create/default behavior.
        if not current:
            synced.append(injury)
            continue
        candidate = {
            **current,
            **surface_fields,
            "latest_reported_status": injury.status,
            # Classify the wound answers themselves. A previously auto-raised
            # severe value would otherwise force medical review before the now-
            # clean answers are considered, making recovery impossible.
            "severity": "mild",
        }
        if injury.body_area.strip():
            candidate["body_area"] = injury.body_area.strip()
        if injury.description.strip():
            candidate["description"] = injury.description.strip()
        try:
            surface_class = classify_injury_surface(candidate)
        except Exception:
            logger.exception("[today] surface_severity_classification_failed")
            synced.append(injury)
            continue

        current_severity = str(current.get("severity") or "mild").strip().lower()
        system_owned = (
            str(current.get("severity_source") or "").strip().lower() == "surface_system"
        )
        stored_manual = str(current.get("manual_severity") or "").strip().lower()
        if injury.severity is not None:
            # An explicit severity on this report is the athlete choosing one,
            # which replaces whatever floor was in force.
            athlete_severity = str(injury.severity).strip().lower()
        elif system_owned:
            # The stored value is the system's floor, so the athlete's own
            # severity is the one preserved underneath it.
            athlete_severity = stored_manual or "mild"
        else:
            athlete_severity = current_severity

        # A wound that no longer classifies as surface tissue has no floor to
        # contribute, so the athlete's severity simply stands.
        floor = _SURFACE_SEVERITY_FLOOR.get(surface_class, athlete_severity)
        target = _stronger_severity(athlete_severity, floor)
        synced.append(injury.model_copy(update={"severity": target}))
        if injury.flag_id:
            provenance[injury.flag_id] = (
                {"severity_source": "surface_system", "manual_severity": athlete_severity}
                if target != athlete_severity
                else {"severity_source": "manual", "manual_severity": None}
            )
    return synced, provenance


def _with_surface_class(injuries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Stamp each open injury with its canonical surface classification.

    Computed once, server-side, so the Today UI can ask the right follow-up for a
    skin injury without re-deriving the rules (and so nothing has to parse the
    injury text client-side). A classification failure degrades to ``None`` —
    which the UI reads as "not a skin injury", i.e. existing behaviour.
    """
    rows: list[dict[str, Any]] = []
    for injury in injuries or []:
        row = dict(injury)
        try:
            row["surface_class"] = classify_injury_surface(row)
        except Exception:
            logger.exception("[today] surface_injury_classification_failed")
            row["surface_class"] = None
        rows.append(row)
    return rows


#: How much history the rehab-stage resolver is given. Wide enough for the
#: evidence counts to mean something, bounded so a Today read stays cheap.
_REHAB_STAGE_HISTORY_LIMIT = 14

def _with_rehab_stage(
    injuries: Sequence[Mapping[str, Any]],
    *,
    store: AppStore,
    athlete_id: str,
    current_checkin: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Stamp each open injury with its own resolved rehabilitation stage.

    Rehab stage is per injury and derived from that injury's record plus the
    athlete's check-in and session history — never from the camp phase, which is
    not an input to the resolver at all. Computed server-side for the same reason
    ``surface_class`` is: so no other layer re-derives it.

    Degrades to no stamp on any failure. A missing stage reads as "not resolved"
    everywhere downstream, which is the same thing every consumer already does
    with PR1's not-yet-migrated metadata.
    """
    rows = [apply_episode_observations(dict(injury), episode_observations(store, athlete_id, injury))
            for injury in injuries or []]
    if not rows:
        return rows

    checkins, _checkins_ok = _checked_recent_today_checkins(
        store, athlete_id, limit=_REHAB_STAGE_HISTORY_LIMIT
    )
    completions, _completions_ok = _checked_recent_session_completions(
        store, athlete_id, limit=_REHAB_STAGE_HISTORY_LIMIT
    )
    try:
        decisions = resolve_rehab_stages(
            rows,
            current_checkin=current_checkin,
            previous_checkins=checkins,
            session_completions=completions,
        )
    except Exception:
        logger.exception("[today] rehab_stage_resolution_failed athlete_id=%s", athlete_id)
        return rows

    for row in rows:
        decision = decisions.get(str(row.get("id") or ""))
        if decision is None:
            continue
        row["rehab_stage"] = decision.stage
        row["rehab_stage_reasons"] = list(decision.reasons)
        row["rehab_care_pathway"] = decision.care_pathway
        row["rehab_medical_gate"] = decision.medical_gate
    return rows


def _log_next_transition(row: Mapping[str, Any]) -> None:
    """Operational diagnostics for the next declared stage transition (no reads)."""
    evaluation = ((row.get("rehab_decision") or {}).get("progression") or {}).get("next_transition")
    if not evaluation:
        return
    logger.info("[today] rehab_next_transition %s", json.dumps({
        "injury_id": str(row.get("id") or ""), "episode_id": str(row.get("episode_id") or ""),
        "transition": f"{evaluation['from_stage']}->{evaluation['to_stage']}", "status": evaluation["status"],
        "reason_codes": evaluation["reason_codes"], "missing_inputs": evaluation["missing_inputs"],
    }, sort_keys=True, separators=(",", ":")))


def _rehab_equipment_from_intake(intake: Mapping[str, Any]) -> list[str]:
    """Exact reported props, shared by Today and generation. Never infer equipment."""
    return list(dict.fromkeys(value for field in ("equipment", "equipment_access")
        for value in (intake.get(field) if isinstance(intake.get(field), (list, tuple)) else ())
        if isinstance(value, str)))


_ANSWER_LABELS = {"better": "Better", "same": "Same", "worse": "Worse", "not_sure": "Not sure"}
_CHECKIN_LABELS = {"improving": "Better", "ongoing": "Same", "worse": "Worse"}


def injury_recent_log(exposures, observations, decision: Mapping[str, Any], bank: Sequence[Mapping[str, Any]],
                      *, limit: int = 6) -> list[dict[str, Any]]:
    """The athlete's recent rehab and injury answers for this episode, newest first.

    Read-only, built from rows Today already loaded. ``counts`` says whether a
    rehab entry is evidence for the current progression check.
    """
    names = {d.get("id"): d.get("name") for group in bank for d in group.get("drills", [])}
    next_transition = (decision.get("progression") or {}).get("next_transition") or {}
    evidence = {str(i) for r in next_transition.get("requirements", []) for i in r.get("evidence_ids", [])}
    entries = []
    for row in exposures:
        raw = row.get("event_json") or row
        response = raw.get("response") or {}
        stamp = str((raw.get("provenance") or {}).get("recorded_at") or row.get("created_at") or raw.get("occurred_at") or "")
        drill = raw.get("drill_id") or raw.get("rehab_drill_id")
        entries.append({"at": stamp, "date": stamp[:10], "kind": "rehab",
                        "label": names.get(drill) or str(drill or "Rehab").replace("_", " ").capitalize(),
                        "felt": _ANSWER_LABELS.get(response.get("during_response"), "not answered"),
                        "next_day": _ANSWER_LABELS.get(response.get("next_day_response"), "waiting"),
                        "counts": (str(raw.get("exposure_id") or row.get("id")) in evidence) if next_transition else None})
    for event in observations:
        status = (event.get("payload") or {}).get("latest_reported_status")
        if (event.get("event_type") == "injury_checkin" and (event.get("payload") or {}).get("explicit_report") is True
                and status in _CHECKIN_LABELS):
            stamp = str(event.get("created_at") or "")
            entries.append({"at": stamp, "date": stamp[:10], "kind": "checkin", "label": _CHECKIN_LABELS[status]})
    return sorted(entries, key=lambda e: e["at"], reverse=True)[:limit]


def _with_injury_policy(injuries, *, store, athlete_id, phase="", current_checkin=None, equipment=(), readiness_decision=None,
                        training_day=None, training_session=None, observations_by_injury=None, as_of=None, athlete_timezone=None):
    if not injuries:
        return []
    policies, bank = load_clinical_policies(), get_rehab_bank()
    as_of = as_of or datetime.now(timezone.utc)
    from api.contracts.rehab_schedule import schedule_rehab
    completions = store.list_rehab_schedule_completions(athlete_id, from_day=(parse_iso_date(training_day) - timedelta(days=14)).isoformat()) if training_day else []
    rows = []
    for injury in injuries:
        observations = (observations_by_injury[str(injury["id"])] if observations_by_injury is not None
                        else episode_observations(store, athlete_id, injury))
        row = apply_episode_observations(injury, observations, as_of=as_of)
        from api.contracts.ankle_restore_load import seated_ankle_multi_injury_hold
        from api.contracts.shoulder_restore_load import wall_shoulder_multi_injury_hold
        if seated_ankle_multi_injury_hold(row, injuries) or wall_shoulder_multi_injury_hold(row, injuries):
            row["restriction_hold"] = True
        exposures = []
        history_truncated = False
        reader = getattr(store, "list_rehab_exposures", None)
        if callable(reader) and injury.get("episode_id"):
            window = reader(athlete_id, injury_id=str(injury["id"]), injury_episode_id=str(injury["episode_id"]))
            exposures = list(window.rows)
            history_truncated = window.history_truncated
        exposures = exposure_rows_with_observations(exposures, observations, athlete_timezone)
        # Only compiled criteria can request hydration. Athlete payloads and
        # generic clearance cannot grant review trust or activate LOAD.
        from api.contracts.clinical_progression_review import CLINICAL_REVIEW_REGISTRY
        from api.contracts.rehab_assessment import assessment_identity
        from api.services.clinical_review_capture_service import hydrate_review_input
        review_inputs = {}
        identity = assessment_identity(injury)
        for policy in policies:
            if (policy.region, policy.injury_type) != identity:
                continue
            for transition in policy.transitions:
                for requirement in transition.requirements:
                    definition = CLINICAL_REVIEW_REGISTRY.current(requirement.checkpoint)
                    if not definition or policy.policy_id not in definition.profile_ids:
                        continue
                    try:
                        review_inputs[definition.criterion_id] = hydrate_review_input(
                            store.get_clinical_review_capture_context(athlete_id, str(injury["id"]), str(injury["episode_id"])),
                            criterion_id=definition.criterion_id, criterion_version=definition.version,
                            as_of=as_of, registry=CLINICAL_REVIEW_REGISTRY, policies=policies, bank=bank)
                    except (ValueError, HTTPException) as exc:
                        logger.info("[today] clinical_review_unavailable criterion=%s category=%s",
                            definition.criterion_id, type(exc).__name__)
                        continue
                    except Exception as exc:  # noqa: BLE001 - unavailable authority fails closed
                        # Missing/unavailable private history cannot satisfy a
                        # clinical requirement or interrupt conservative rehab.
                        logger.error("[today] clinical_review_hydration_failed criterion=%s category=%s",
                            definition.criterion_id, type(exc).__name__)
                        continue
        row["rehab_decision"] = resolve_injury_policy(row, policies=policies, bank=bank, phase=phase,
                                                     exposures=exposures, current_checkin=current_checkin, equipment=equipment,
            history_truncated=history_truncated, readiness_decision=readiness_decision, as_of=as_of,
            clinical_review_inputs=review_inputs)
        if training_day:
            row["rehab_decision"]["schedule"] = schedule_rehab(row, row["rehab_decision"], training_day=training_day,
                completions=completions, exposures=exposures, readiness_decision=readiness_decision, training_session=training_session)
            # A recovering/held loading routine may have a due conservative
            # alternative. Nothing is added to the one-routine daily allocation.
            excluded, first = [], row["rehab_decision"]
            while row["rehab_decision"].get("prescription") and row["rehab_decision"]["schedule"]["state"] in {"recovery_day", "held", "deferred"}:
                if (row["rehab_decision"]["prescription"].get("clinical_review_pin")
                        or row["rehab_decision"]["prescription"].get("required_rehabilitation_level")):
                    break
                excluded.append(row["rehab_decision"]["prescription"]["drill_id"])
                alternative = resolve_injury_policy(row, policies=policies, bank=bank, phase=phase, exposures=exposures,
                    current_checkin=current_checkin, equipment=equipment, history_truncated=history_truncated,
                    readiness_decision=readiness_decision, excluded_drill_ids=excluded, as_of=as_of)
                if not alternative.get("prescription"):
                    row["rehab_decision"] = first
                    break
                alternative["schedule"] = schedule_rehab(row, alternative, training_day=training_day, completions=completions,
                    exposures=exposures, readiness_decision=readiness_decision, training_session=training_session)
                row["rehab_decision"] = alternative
                if alternative["schedule"]["state"] == "due":
                    alternative["schedule"]["reason"] = first["schedule"]["reason"] + " A lighter baseline routine is due."
        if row["rehab_decision"].get("activation") == "live":
            row["rehab_stage"] = row["rehab_decision"]["stage"]
        row["rehab_decision"]["recent_log"] = injury_recent_log(exposures, observations, row["rehab_decision"], bank)
        _log_next_transition(row)
        rows.append(row)
    if training_day:
        limit = 1 if "sparring" in str((training_session or {}).get("session_type") or "").lower() else 2
        # A completed standalone rehab occurrence must not relax the ceiling
        # already accepted for a sparring day when the plan card falls forward.
        limit = min([limit] + [c["prescription_snapshot"]["allocation_limit"] for c in completions
                    if c.get("training_day") == training_day and c.get("status") in {"started", "done", "modified"}
                    and (c.get("prescription_snapshot") or {}).get("allocation_limit") in (1, 2)])
        used = sum(rehab_allocation_count(
            (c.get("prescription_snapshot") or {}).get("session", {}).get("blocks", []), include_held=True)
            for c in completions
            if c.get("training_day") == training_day and c.get("status") in {"started", "done", "modified"})
        slots = max(0, limit - used)
        for row in sorted(rows, key=lambda r: str(r.get("id"))):
            decision = row["rehab_decision"]
            if decision["schedule"]["state"] == "due" and decision.get("activation") == "live":
                if slots:
                    slots -= 1
                else:
                    decision["schedule"] = {"state": "deferred", "reason": "Today's rehab allocation is full.",
                        "next_due_day": (parse_iso_date(training_day) + timedelta(days=1)).isoformat()}
    return rows


def _with_safe_session_context(
    injuries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Attach backend-owned anatomy and consequence fields for Today.

    The browser must never maintain a second injury synonym parser. Every body
    phrase is resolved by the existing backend injury system, then its canonical
    location is grouped for safe-session loading decisions.

    The broad group goes in ``load_region``. ``body_region`` stays the stored
    location (``ankle``, not ``lower_limb``): rehab exposure events are matched
    against it later in the same build.
    """
    rows: list[dict[str, Any]] = []
    for injury in injuries or []:
        row = dict(injury)
        try:
            context = injury_body_region_context(
                row.get("body_area"), row.get("description")
            )
            row["canonical_location"] = context["canonical_location"]
            row["region_group"] = context["region_group"]
            row["load_region"] = context["body_region"]
            row["consequence"] = injury_consequence_tier(
                row.get("body_area"),
                row.get("description"),
                severity=row.get("severity"),
            )
        except Exception:
            logger.exception("[today] safe_session_injury_classification_failed")
            row["canonical_location"] = None
            row["region_group"] = "unknown"
            row["load_region"] = "unknown"
            # Fail closed: the client treats an unknown structural consequence as
            # rest-only, so a classifier failure can never re-enable loaded work.
            row["consequence"] = "structural"
        rows.append(row)
    return rows


def _load_relevant_worse_injury(injuries: Sequence[Mapping[str, Any]]) -> bool:
    """True when an injury reported worse is one the generic stop still owns.

    A worsening skin injury is deliberately excluded: it routes through the
    surface evaluator (contact restriction), so marking a blister worse can no
    longer turn the whole day into rehab-only.
    """
    for injury in injuries or []:
        if str(injury.get("latest_reported_status") or "").strip().lower() != "worse":
            continue
        if str(injury.get("status") or "").strip().lower() not in {"open", "monitoring"}:
            continue
        try:
            surface_class = classify_injury_surface(injury)
        except Exception:
            logger.exception("[today] surface_injury_classification_failed")
            surface_class = "non_surface"
        if surface_class not in _SURFACE_ROUTED_CLASSES:
            return True
    return False


def _checked_with_injury_consequence(
    injuries: Sequence[Mapping[str, Any]],
) -> tuple[tuple[dict[str, Any], ...], bool]:
    """Attach the coarse ``consequence`` tier to each open injury.

    Returns ``(enriched, ok)``. ``ok=False`` when the classifier RAISED for a
    present injury — we then cannot grade its consequence (head/neck vs. tendon),
    which is safety-critical, so the caller treats the context as unavailable
    rather than silently scoring the injury as minor.
    """
    enriched: list[dict[str, Any]] = []
    ok = True
    for injury in injuries or []:
        row = dict(injury)
        if "consequence" not in row:
            try:
                row["consequence"] = injury_consequence_tier(
                    row.get("body_area"),
                    row.get("description"),
                    severity=row.get("severity"),
                )
            except Exception:
                logger.exception("[today] injury_consequence_classification_failed")
                row["consequence"] = None
                ok = False
        enriched.append(row)
    return tuple(enriched), ok


def _checked_last_head_impact_day(store: AppStore, athlete_id: str) -> tuple[str | None, bool]:
    """``(day, ok)`` for the latest rocked/dropped sparring report. A raised
    read is ``ok=False``: it must not look like "no head impact"."""
    try:
        day = store.latest_sparring_log_day(athlete_id, rocked=True)
        return (str(day)[:10] if day else None), True
    except Exception:
        logger.exception("[today] sparring_read_failed athlete_id=%s", athlete_id)
        return None, False


def restrictive_injuries(
    store: AppStore,
    athlete_id: str,
    injuries: Sequence[Mapping[str, Any]],
    *,
    context_ok: bool = True,
) -> list[Mapping[str, Any]]:
    """Injuries that may restrict training, after every medical check.

    A "Not limiting me" report is dropped only once its episode observations,
    medical holds and clinical restrictions have been evaluated and none
    applies. Incomplete injury context never clears an injury.
    """
    if not context_ok:
        return list(injuries)
    kept: list[Mapping[str, Any]] = []
    for injury in injuries:
        if restricts_training(injury):
            kept.append(injury)
            continue
        try:
            observed = apply_episode_observations(dict(injury), episode_observations(store, athlete_id, injury))
        except Exception:
            logger.exception("[today] injury_observations_unavailable")
            kept.append(injury)
            continue
        if restricts_training(observed):
            kept.append(observed)
    return kept


def _readiness_context_and_status(
    store: AppStore,
    *,
    athlete_id: str,
    plan_row: Mapping[str, Any] | None,
    training_day: str,
    phase: str,
    open_injuries: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[ReadinessContext, ReadinessContextStatus]:
    """Assemble the readiness context AND track which safety reads failed.

    Every failed safety-critical read contributes a structured reason code so the
    fail-safe floor (see ``apply_context_failsafe``) can keep a missing read from
    ever being interpreted as readiness. When ``open_injuries`` is supplied by the
    caller (already read upstream) the flag read is skipped, but the consequence
    classification is still verified.
    """
    builder = ContextStatusBuilder()

    if open_injuries is not None:
        resolved_injuries: list[dict[str, Any]] = [dict(flag) for flag in open_injuries]
    else:
        resolved_injuries, injuries_ok = _checked_open_injury_flags(store, athlete_id)
        if not injuries_ok:
            builder.add(INJURY_CONTEXT_UNAVAILABLE)
        try:
            resolved_injuries = [apply_episode_observations(row, episode_observations(store, athlete_id, row))
                                 for row in resolved_injuries]
        except Exception:
            logger.exception("[today] injury_clearance_context_unavailable")
            builder.add(INJURY_CONTEXT_UNAVAILABLE)

    # Attach the coarse consequence tier so the readiness engine can scale the
    # decision by injury TYPE (head/neck, structural, rib, tendon, joint), not
    # severity alone. A classification failure for a present injury is treated as
    # unavailable context (conservative), never as a minor injury.
    enriched_injuries, classify_ok = _checked_with_injury_consequence(resolved_injuries)
    if not classify_ok:
        builder.add(INJURY_CONTEXT_UNAVAILABLE)

    if plan_row is None:
        # No programme is a known fact, not a failed read: there is no
        # prescribed session to grade and no camp intake. The engine treats the
        # missing session as unresolved exposure, never as a safe rest day.
        today_session: Mapping[str, Any] = {}
        intake: Mapping[str, Any] = {}
    else:
        today_session, session_ok = _checked_today_session_for_readiness(plan_row, training_day)
        if not session_ok:
            builder.add(SESSION_UNAVAILABLE)

        intake, intake_ok = _checked_intake_payload_for_readiness(store, plan_row)
        if not intake_ok:
            builder.add(INTAKE_UNAVAILABLE)

    last_head_impact_day, sparring_ok = _checked_last_head_impact_day(store, athlete_id)
    if not sparring_ok:
        builder.add(SPARRING_UNAVAILABLE)

    recent_checkins, checkins_ok = _checked_recent_today_checkins(store, athlete_id)
    if not checkins_ok:
        builder.add(CHECKINS_UNAVAILABLE)

    # Session-level work only. Keep rehab/XP completion reads prescribed-only.
    from .training_history import training_history
    try:
        recent_sessions = training_history(store, athlete_id)
        completions_ok = True
        from fightcamp.completed_workload import has_unverified_completions
        if has_unverified_completions(recent_sessions, training_day):
            completions_ok = False
    except Exception:
        logger.exception("[today] training_history_read_failed")
        recent_sessions, completions_ok = [], False
    if not completions_ok:
        builder.add(COMPLETIONS_UNAVAILABLE)

    context = ReadinessContext(
        training_day=training_day,
        phase=phase,
        today_session=today_session,
        active_plan=plan_row,
        intake=intake,
        open_injuries=restrictive_injuries(store, athlete_id, enriched_injuries, context_ok=classify_ok),
        recent_checkins=recent_checkins,
        recent_sessions=recent_sessions,
        last_head_impact_day=last_head_impact_day,
    )
    return context, builder.build()


def _readiness_decision_with_failsafe(
    store: AppStore,
    *,
    checkin: ReadinessCheckin,
    athlete_id: str,
    plan_row: Mapping[str, Any] | None,
    training_day: str,
    phase: str,
    open_injuries: Sequence[Mapping[str, Any]] | None = None,
    session_override: Mapping[str, Any] | None = None,
) -> tuple[ReadinessAdjustment, ReadinessContextStatus]:
    """Compute the readiness decision and floor it by context completeness.

    This is the single safety chokepoint: it assembles the status-tracked
    context, runs the deterministic engine, then applies the fail-safe floor so a
    degraded/unavailable context can never yield ``train_as_planned``.
    """
    context, status = _readiness_context_and_status(
        store,
        athlete_id=athlete_id,
        plan_row=plan_row,
        training_day=training_day,
        phase=phase,
        open_injuries=open_injuries,
    )
    if session_override is not None:
        from dataclasses import replace
        context = replace(context, today_session=session_override)
    adjustment = build_readiness_adjustment(checkin, context)
    adjustment = apply_context_failsafe(adjustment, status)
    return adjustment, status


def _recommendation_fields_from_decision(
    *,
    checkin_row: Mapping[str, Any],
    decision: ReadinessAdjustment,
) -> dict[str, Any]:
    defaults = ReadinessCheckin()
    fields: dict[str, Any] = {
        "plan_id": checkin_row.get("plan_id"),
        "training_day": checkin_row.get("training_day"),
        "athlete_timezone": checkin_row.get("athlete_timezone") or "",
        "recommendation_state": decision.decision,
        "recommendation_reason": decision.message,
        "recommendation_triggers": list(decision.triggers),
    }
    for field in _CHECKIN_INPUT_FIELDS:
        fields[field] = checkin_row.get(field, getattr(defaults, field))
    if fields["plan_id"] is None:
        fields["phase"] = None
    return fields


def _refresh_today_recommendation_after_injury_change(
    store: AppStore,
    *,
    athlete_id: str,
    plan_row: Mapping[str, Any] | None,
    training_day: str,
    open_injuries: Sequence[Mapping[str, Any]],
    injury_reported_worse: bool,
) -> dict[str, Any] | None:
    """Re-evaluate today's stored check-in after an injury change. ``plan_row``
    None re-evaluates the plan-free check-in, so its green cannot go stale."""
    plan_id = str((plan_row or {}).get("id") or "").strip()
    if plan_row is not None and not plan_id:
        return None
    today_checkin = store.get_today_checkin(athlete_id, plan_id or None, training_day)
    if not today_checkin:
        return None

    # Escalate the readiness recommendation when the injury is reported worse OR
    # an active severe injury is present — a severe injury added as "ongoing" must
    # still pull training back, not sit at the daily "load reduced" copy. Routing
    # it through the same "worse" path keeps the engine's copy ("Rehab only
    # today.") consistent across both entry points.
    has_active_severe = _active_severe_injury(open_injuries) is not None
    # The injury answer is re-read from the injuries as they are now, so a later
    # Better clears an earlier Worse.
    today_checkin = {**today_checkin, "active_injury": derived_active_injury(open_injuries)}
    active_injury_override = "worse" if (injury_reported_worse or has_active_severe) else None
    decision, context_status = _readiness_decision_with_failsafe(
        store,
        checkin=_stored_readiness_checkin_from(
            today_checkin, active_injury_override=active_injury_override
        ),
        athlete_id=athlete_id,
        plan_row=plan_row,
        training_day=training_day,
        phase=str(today_checkin.get("phase") or (plan_row or {}).get("phase") or ""),
        open_injuries=[apply_episode_observations(row, episode_observations(store, athlete_id, row))
                       for row in open_injuries],
    )
    if plan_row is None:
        decision = _plan_free_wording(decision)
    refreshed = dict(
        store.upsert_today_checkin(
            athlete_id,
            _recommendation_fields_from_decision(checkin_row=today_checkin, decision=decision),
        )
    )
    refreshed["readiness_signal"] = build_readiness_signal(decision, context_status).to_dict()
    return refreshed


_INJURY_RANK = {"none": 0, "stable": 1, "worse": 2}
_SESSION_RANK = {"none": 0, "normal": 1, "very_hard": 2}


def derived_active_injury(open_injuries: Sequence[Mapping[str, Any]]) -> str:
    """The check-in's injury answer, read from the tracked injuries.

    Skin injuries route through the surface evaluator, so they never count here.
    """
    from api.contracts.readiness_message import _surface_assessment

    tracked = [i for i in open_injuries if str(i.get("status") or "").lower() in {"open", "monitoring"}
               and not _surface_assessment(i).is_surface]
    if _active_severe_injury(tracked) is not None or any(
            str(i.get("latest_reported_status") or "").lower() == "worse" for i in tracked):
        return "worse"
    return "stable" if tracked else "none"


def derived_previous_session(recent_sessions: Sequence[Mapping[str, Any]], training_day: str) -> str:
    """The check-in's last-session answer, read from logged sessions.

    Sessions logged on the previous training day or earlier today. Any one of
    them meeting the recent-workload demanding test makes it "very_hard"
    (conservative: a hard yesterday is not hidden by an easy morning).
    """
    from fightcamp.completed_workload import completed_workload

    window = completed_workload(recent_sessions, training_day, days=2, limit=None)
    if window["demanding_sessions"]:
        return "very_hard"
    return "normal" if window["sessions"] else "none"


def _with_derived_checkin_answers(store: AppStore, *, athlete_id: str, training_day: str,
                                  payload: Mapping[str, Any]) -> dict[str, Any]:
    """Fill active_injury / previous_session from tracked data. A client value
    (older app) only ever escalates, so it can never weaken the derived answer."""
    derived = dict(payload)
    try:
        injury = derived_active_injury(_open_injury_flags(store, athlete_id))
    except Exception:
        # The readiness failsafe owns an unreadable injury list (conservative hold).
        logger.exception("[today] derived_active_injury_failed")
        injury = "none"
    try:
        from .training_history import training_history
        session = derived_previous_session(training_history(store, athlete_id), training_day)
    except Exception:
        logger.exception("[today] derived_previous_session_failed")
        session = "none"
    client_injury = str(payload.get("active_injury") or "none")
    client_session = str(payload.get("previous_session") or "none")
    derived["active_injury"] = max(injury, client_injury, key=lambda v: _INJURY_RANK.get(v, 0))
    derived["previous_session"] = max(session, client_session, key=lambda v: _SESSION_RANK.get(v, 0))
    return derived


def submit_today_checkin(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
    payload: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Persist a Today check-in and its server-evaluated recommendation.

    ``payload`` carries ``plan_id`` plus the categorical inputs/safety flags.
    Any client-supplied recommendation field is ignored — the recommendation is
    always recomputed here. Returns the persisted row (recommendation included).

    Without ``plan_id`` the check-in is plan-free: the same engine and fail-safe
    run with no prescribed session, phase or intake, and nothing is invented to
    stand in for them. That path is only taken once the server confirms there is
    no active plan, so a stale client can never record a plan-free check-in for
    an athlete whose programme should own the day.
    """
    # Injury and last-session answers come from tracked injuries and logged
    # sessions, never a second question that could contradict them.
    payload = _with_derived_checkin_answers(store, athlete_id=athlete_id, payload=payload,
                                            training_day=resolve_training_day(athlete_timezone, now=now))
    plan_id = str(payload.get("plan_id") or "").strip()
    if not plan_id:
        return _submit_plan_free_checkin(
            store, athlete_id=athlete_id, athlete_timezone=athlete_timezone, payload=payload, now=now
        )
    _require_valid_plan_id(plan_id)

    # Writes are service-role (RLS does not gate them here), so the backend must
    # prove the plan belongs to the caller before persisting anything.
    plan_row = store.get_training_plan_for_athlete(plan_id, athlete_id)
    if plan_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")

    training_day = resolve_training_day(athlete_timezone, now=now)
    warnings = _same_day_checkin_warnings(
        store,
        athlete_id=athlete_id,
        plan_id=plan_id,
        training_day=training_day,
        include_current_plan=True,
    )
    decision, context_status = _readiness_decision_with_failsafe(
        store,
        checkin=_readiness_checkin_from(payload),
        athlete_id=athlete_id,
        plan_row=plan_row,
        training_day=training_day,
        phase=str(payload.get("phase") or plan_row.get("phase") or ""),
    )

    fields: dict[str, Any] = {
        "plan_id": plan_id,
        "training_day": training_day,
        "athlete_timezone": athlete_timezone or "",
        # Server-computed recommendation — never trust a client-supplied value.
        # ``decision`` is already floored by the context fail-safe, so a failed
        # safety read cannot persist as ``train_as_planned``.
        "recommendation_state": decision.decision,
        "recommendation_reason": decision.message,
        "recommendation_triggers": list(decision.triggers),
    }
    for field in _CHECKIN_INPUT_FIELDS:
        fields[field] = payload[field]

    row = dict(store.upsert_today_checkin(athlete_id, fields))
    row["warnings"] = warnings
    # Backend-owned typed safety signal (additive; see readiness_failsafe).
    row["readiness_signal"] = build_readiness_signal(decision, context_status).to_dict()
    return row


# The clean-day copy written for a prescribed session ("Full session.",
# "Run the planned work…") would describe a session that does not exist. Only
# those titles are reworded; every restrictive decision keeps its own copy.
_PLANNED_CLEAN_TITLES = frozenset({"Full session.", "Sharp work only."})
PLAN_FREE_CLEAN_TITLE = "No warning signs today."
PLAN_FREE_CLEAN_ACTION = "Train as your coach plans and stop anything that hurts."
PLAN_FREE_CLEARANCE_NOTE = "This check-in is not medical clearance for sparring or contact."


def _plan_free_wording(adjustment: ReadinessAdjustment) -> ReadinessAdjustment:
    """The same decision, worded for an athlete with no prescribed session."""
    from dataclasses import replace

    if adjustment.decision != "train_as_planned":
        return adjustment
    title, action = adjustment.title, adjustment.action
    if title in _PLANNED_CLEAN_TITLES:
        title, action = PLAN_FREE_CLEAN_TITLE, PLAN_FREE_CLEAN_ACTION
    return replace(adjustment, title=title, action=action,
                   safety=adjustment.safety or PLAN_FREE_CLEARANCE_NOTE)


def _submit_plan_free_checkin(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
    payload: Mapping[str, Any],
    now: datetime | None,
) -> dict[str, Any]:
    if payload.get("phase"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="a camp phase needs a plan")
    training_day = resolve_training_day(athlete_timezone, now=now)
    # A failed read raises (503) rather than reading as "no plan".
    if resolve_active_plan(store, athlete_id, current_training_day=training_day).plan:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Your plan is active. Refresh Today before checking in.")
    decision, context_status = _readiness_decision_with_failsafe(
        store,
        checkin=_readiness_checkin_from(payload),
        athlete_id=athlete_id,
        plan_row=None,
        training_day=training_day,
        phase="",
    )
    decision = _plan_free_wording(decision)
    fields: dict[str, Any] = {
        "plan_id": None,
        "training_day": training_day,
        "athlete_timezone": athlete_timezone or "",
        "recommendation_state": decision.decision,
        "recommendation_reason": decision.message,
        "recommendation_triggers": list(decision.triggers),
    }
    for field in _CHECKIN_INPUT_FIELDS:
        fields[field] = payload.get(field) if field != "phase" else None
    row = dict(store.upsert_today_checkin(athlete_id, fields))
    row["warnings"] = []
    row["readiness_signal"] = build_readiness_signal(decision, context_status).to_dict()
    return row


def _recommendation_mapping(checkin: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Adapt a persisted check-in row to the recommendation contract shape."""
    if not checkin:
        return None
    return {
        "training_day": checkin.get("training_day"),
        "decision": checkin.get("recommendation_state"),
        "reason": checkin.get("recommendation_reason"),
        "triggers": checkin.get("recommendation_triggers") or [],
    }


# Session-completion transitions that mean "I trained (or am training) this".
# A severe injury blocks these; skipping / reverting to not-started stays allowed.
_TRAINING_COMPLETION_STATUSES: frozenset[str] = frozenset({"started", "done", "modified"})


def _is_stopped_started_session(existing, payload):
    """Only an owned, accepted occurrence may bypass execution reads to stop."""
    return bool(existing.get("prescription_snapshot") and existing.get("started_at")
                and existing.get("status") == "started"
                and str(existing.get("plan_id")) == str(payload.get("plan_id"))
                and str(existing.get("session_id")) == str(payload.get("session_id"))
                and payload.get("status") == "modified" and payload.get("rehab_performance") == "stopped"
                and str(payload.get("modification_reason") or "").strip())


def _active_severe_injury(
    open_flags: Sequence[Mapping[str, Any]] | None,
) -> Mapping[str, Any] | None:
    """The first active (open/monitoring) SEVERE injury flag, or None.

    Severity-driven, not day-status driven: a severe injury is still severe while
    it is easing (monitoring), so only clearing it (resolved) drops it out. This
    is the single source of truth shared by the completion guard and the command
    view's injury-hold recommendation, matching the Today/Overview UI.
    """
    for flag in open_flags or []:
        if (
            str(flag.get("severity") or "") == "severe"
            and str(flag.get("status") or "") in {"open", "monitoring"}
        ):
            return flag
    return None


def _active_severe_non_surface_injury(
    open_flags: Sequence[Mapping[str, Any]] | None,
) -> Mapping[str, Any] | None:
    """First severe active injury not owned by the surface-wound pathway."""
    for flag in open_flags or []:
        if (
            str(flag.get("severity") or "") != "severe"
            or str(flag.get("status") or "") not in {"open", "monitoring"}
        ):
            continue
        try:
            if classify_injury_surface(flag) == "non_surface":
                return flag
        except Exception:
            # Classification failure must not let a severe injury lose priority.
            logger.exception("[today] severe_injury_surface_classification_failed")
            return flag
    return None


def _severe_injury_recommendation(
    injury: Mapping[str, Any], training_day: str
) -> dict[str, Any]:
    """A hard-block (pull_back) recommendation that supersedes the daily readiness
    copy when a severe injury is active. The stored daily check-in is untouched
    (it stays in history); this only reshapes the live command view so every
    consumer — not just the Today UI — sees the injury hold as authoritative."""
    from fightcamp.injury_awareness import injury_stop_phrase

    label = injury.get("label") or build_injury_label(
        injury.get("body_area"), injury.get("description")
    )
    reason = "\n".join(
        [
            "Session blocked",
            f"{injury_stop_phrase(dict(injury), label)}. This is not a load-reduced session.",
            "Clear it before training.",
        ]
    )
    flag_id = str(injury.get("id") or "").strip()
    triggers = ["injury_hold"]
    if flag_id:
        triggers.append(f"injury_hold:{flag_id}")
    triggers.append("active_injury_worse")
    return {
        "decision": "pull_back",
        "reason": reason,
        "training_day": training_day,
        # The hold is injury-driven and supersedes the daily readiness copy, so the
        # card names the injury as its contributor. ``injury_hold`` marks it as a
        # recommendation that does NOT rest on today's check-in — it fires whether
        # or not one exists, so the card must not claim a check-in it never read.
        "triggers": triggers,
    }


def _surface_medical_review_recommendation(
    injury: Mapping[str, Any], training_day: str
) -> dict[str, Any] | None:
    """Visible wound guidance even when there is no session or daily check-in.

    Driven by the wound's own answers rather than its stored class, for the same
    reason the readiness engine is: the surface severity floor raises an infected
    wound to severe, and a severe wound classifies as medical review whatever its
    answers say. Reading the answers keeps this in step with the readiness
    pathway — a wound severe for some other reason keeps the severe-injury hold.
    """
    try:
        review_reason = surface_wound_medical_review(injury)
    except Exception:
        logger.exception("[today] surface_review_classification_failed")
        return None
    if review_reason is None:
        return None

    label = injury.get("label") or build_injury_label(
        injury.get("body_area"), injury.get("description")
    )
    natural_label = str(label or "wound").strip().lower()
    if review_reason == "infection_signs":
        reason = f"Your {natural_label} is showing infection signs."
        safety = "Seek medical advice for spreading redness, pus, swelling, or fever."
    elif review_reason == "uncontrolled_bleeding":
        reason = f"Your {natural_label} is bleeding and not under control."
        safety = "Get bleeding controlled and seek medical advice before training."
    else:
        reason = f"Your {natural_label} needs checking before you train through it."
        safety = "Seek medical advice for spreading redness, pus, drainage, or fever."
    flag_id = str(injury.get("id") or "").strip()
    triggers = ["surface_injury_medical_review"]
    if flag_id:
        triggers.append(f"surface_injury_medical_review:{flag_id}")
    triggers.append("safety_check:surface_injury:medical_review")
    return {
        "decision": "pull_back",
        "reason": "\n".join(
            (
                "Get this checked.",
                reason,
                "Keep it clean and covered, and keep direct contact or rubbing off that area.",
                safety,
            )
        ),
        "training_day": training_day,
        "triggers": triggers,
    }


# Triggers that mark a pull-back as a GENERIC injury hold — one driven by "an
# injury is active/worse" with no injury-specific guidance of its own. A red flag
# stop is never generic, and neither is a pull-back the surface pathway already
# owns, so both keep their copy.
_GENERIC_INJURY_PULL_BACK_TRIGGERS = frozenset(
    {"active_injury_worse", "active_injury_restriction", "injury_hold"}
)


def _is_generic_injury_pull_back(recommendation: Mapping[str, Any] | None) -> bool:
    triggers = {
        str(trigger).split(":", 1)[0]
        for trigger in (recommendation or {}).get("triggers") or []
    }
    if "red_flag" in triggers:
        return False
    return bool(triggers & _GENERIC_INJURY_PULL_BACK_TRIGGERS)


def _completion_session_is_support(
    plan_row: Mapping[str, Any], training_day: str, session_id: str
) -> bool:
    """True when the session being completed is today's low-cost support / filler
    session (so an injury hold does not block logging it). Structured-plan aware;
    conservatively False when the day's session cannot be resolved."""
    entry = _today_session_for_readiness(plan_row, training_day)
    if not entry:
        return False
    entry_id = _session_id_for_entry(entry)
    if session_id and entry_id and str(entry_id) != str(session_id):
        return False
    return is_support_session(entry)


# How many days back a session may still be logged after the fact. Mirrors
# RETRO_LOG_WINDOW_DAYS in web/lib/camp-map.ts.
RETRO_LOG_WINDOW_DAYS = 7


def _validate_retro_log_day(
    plan_row: Mapping[str, Any],
    *,
    requested_day: str,
    today: str,
    status_value: str,
    session_id: str,
) -> None:
    """Reject an explicit past ``training_day`` outside the back-fill contract.

    Retro logs must be terminal (a past session has no start/resume lifecycle),
    inside the 7-day window, and — for structured plans — target a session that
    was actually scheduled on that day.
    """
    try:
        requested = date.fromisoformat(requested_day)
        today_date = date.fromisoformat(today)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="training_day must be a YYYY-MM-DD date",
        )
    if requested > today_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="training_day cannot be in the future",
        )
    if (today_date - requested).days > RETRO_LOG_WINDOW_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Sessions can only be back-filled within {RETRO_LOG_WINDOW_DAYS} days.",
        )
    if status_value not in TERMINAL_COMPLETION_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A past session can only be logged as done, modified, or skipped.",
        )

    # Structured plans know exactly which sessions each day carried; the
    # requested day+session must match one. Legacy plans without structured
    # weeks stay permissive, matching the normal Today flow.
    weeks = _structured_plan_weeks(plan_row, training_day=requested_day)
    if not weeks:
        return
    for week in weeks:
        for day in _iter_mapping_items(week.get("days")):
            if _clean_text(day.get("date"))[:10] != requested_day:
                continue
            explicit_ids = {
                _clean_text(candidate.get("session_id"))
                for candidate in _iter_mapping_items(day.get("sessions"))
                if _clean_text(candidate.get("session_id")) and not (
                    is_optional_session(candidate) and candidate.get("prescription_source") == "athlete"
                    and candidate.get("session_type") == "recovery"
                )
            }
            entry = _structured_session_entry_for_day(day, week=week)
            entry_id = _clean_text(entry.get("session_id")) if entry else ""
            if session_id in explicit_ids or (entry_id and session_id == entry_id):
                return
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="That session is not scheduled on the requested day.",
            )
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="The requested day is not part of this plan.",
    )


def _completion_timestamps(
    existing: Mapping[str, Any], status_value: str, now_iso: str
) -> tuple[str | None, str | None]:
    """started_at / completed_at for a status transition. Both are preserved once
    set (idempotent re-saves keep the original time) and cleared when the status
    moves back to a state that should not have them."""
    started_at = (
        existing.get("started_at") or now_iso
        if status_value in {"started", "done", "modified"}
        else None
    )
    completed_at = (
        existing.get("completed_at") or now_iso
        if status_value in {"done", "modified"}
        else None
    )
    return started_at, completed_at


def _day_session_ids(plan_row: Mapping[str, Any], training_day: str) -> list[str]:
    """Every addressable session the card schedules on ``training_day``, in order."""
    ids: list[str] = []
    for entry in _structured_today(plan_row, training_day).entries:
        session_id = _session_id_for_entry(entry)
        if session_id and session_id not in ids:
            ids.append(session_id)
    return ids


def upsert_session_completion(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
    payload: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate (via the completion contract) and upsert a session completion.

    The server computes ``training_day`` and stamps ``started_at`` /
    ``completed_at`` from the status transition; the contract's field rules are
    enforced before anything is written. An explicit past ``training_day`` in
    the payload is a retro-log (7-day back-fill window, terminal statuses only,
    session must be scheduled on that day).
    """
    plan_id = str(payload.get("plan_id") or "").strip() or None
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="session_id is required",
        )
    if plan_id:
        _require_valid_plan_id(plan_id)

    # Service-role write: enforce plan ownership at the backend (RLS won't here).
    plan_row = store.get_training_plan_for_athlete(plan_id, athlete_id) if plan_id else {}
    if plan_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")

    today = resolve_training_day(athlete_timezone, now=now)
    now_iso = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    status_value = str(payload.get("status") or "not_started")

    requested_day = str(payload.get("training_day") or "").strip()
    is_retro_log = bool(requested_day) and requested_day != today
    if is_retro_log and not plan_id:
        raise HTTPException(409, "Standalone rehab can only be logged for today.")
    if is_retro_log:
        _validate_retro_log_day(
            plan_row,
            requested_day=requested_day,
            today=today,
            status_value=status_value,
            session_id=session_id,
        )
    training_day = requested_day or today
    existing = store.get_session_completion(athlete_id, session_id, training_day) or {}
    if existing.get("completion_origin") == "day_credit":
        # Credit is not an accepted/performed occurrence. Direct confirmation
        # needs fresh actual timestamps, safety gates and its own rehab context.
        existing = {key: existing[key] for key in ("id", "plan_id", "session_id", "training_day") if key in existing}
    accepted_stop = _is_stopped_started_session(existing, payload)
    stop_requested = bool(existing.get("plan_id") == plan_id and existing.get("started_at")
                          and existing.get("status") == "started" and status_value == "modified"
                          and payload.get("rehab_performance") == "stopped"
                          and str(payload.get("modification_reason") or "").strip())

    # The same pre-start boundary that frames Today is enforced on the write.
    # A stale client or forged request cannot log a future open-plan session
    # against today's training-day record.
    if (
        not is_retro_log
        and status_value != "not_started"
        and _open_plan_has_not_started(plan_row, training_day)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This plan has not started yet, so no session can be logged today.",
        )

    # A day the plan card schedules nothing on has no session to log. The UI does
    # not offer it, but the UI is not the guard: without this, a direct call —
    # or a client running against a stale card — writes a completion for a
    # session that never existed, and that record then drives "Resume" on a rest
    # day. ``not_started`` stays allowed so an already-written record can be
    # cleared.
    command = None
    if not is_retro_log and (status_value in _TRAINING_COMPLETION_STATUSES or not plan_id) and not accepted_stop:
        from .today_readiness_boundary import build_today_command_view as build_current_command
        command = build_current_command(store, athlete_id=athlete_id, athlete_timezone=athlete_timezone, now=now)
    live = command.live_prescription if command and command.active_plan.get("id") == plan_id else None
    if command and command.active_plan.get("id") != plan_id and status_value in _TRAINING_COMPLETION_STATUSES:
        if any(injury.get("rehab_decision", {}).get("activation") in {"live", "retired"}
               or injury.get("clinician_clearance") for injury in command.open_injuries):
            raise HTTPException(409, "Activate this plan to apply your current injury guidance before training.")
    standalone = live is not None and live["session"].get("session_id") == session_id
    saved_occurrence = existing.get("prescription_snapshot") or {}
    saved_standalone = saved_occurrence.get("plan_id") == plan_id and saved_occurrence.get("session", {}).get("session_id") == session_id
    if not plan_id:
        if not re.fullmatch(rf"rehab-{re.escape(training_day)}-[a-f0-9]{{24}}", session_id):
            raise HTTPException(422, "A current standalone rehab session is required.")
        if not standalone and not saved_standalone:
            raise HTTPException(409, "Refresh Today to use the current rehab session.")
        if saved_standalone and existing.get("plan_id") is not None:
            raise HTTPException(409, "session_completion_plan_mismatch")
    severe = None
    if not is_retro_log and status_value in _TRAINING_COMPLETION_STATUSES and not accepted_stop and not _completion_session_is_support(plan_row, training_day, session_id):
        severe = _active_severe_injury(_open_injury_flags(store, athlete_id))
    # The exception records stopping already-started work under a server-owned
    # hold. A client enum alone cannot bypass training or freshness gates.
    stopped_started_session = accepted_stop or bool(stop_requested and (severe is not None or
        (standalone and live.get("safety_hold"))))
    # Declining standalone rehab executes nothing and earns no credit. Its
    # current episode may be unreadable, so accept the rehab identity shape
    # without manufacturing a prescription snapshot or permission to train.
    declined_rehab = status_value == "skipped" and bool(re.fullmatch(
        rf"rehab-{re.escape(training_day)}(?:-[a-f0-9]{{24}})?", session_id))
    if status_value != "not_started" and _structured_today(plan_row, training_day).is_rest_day and not (standalone or saved_standalone or declined_rehab):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That day is a rest day in your plan, so there is no session to log.",
        )

    # Server-side safety hold: an active severe injury blocks actually training
    # this session (start / done / modified) — not just in the UI. Skipping or
    # reverting to not-started stays allowed so the athlete can still log that
    # they backed off. Mirrors the Today/Overview injury hold. A low-cost support /
    # filler session (mental cue, breathing/mobility reset) is exempt — it is the
    # safe work the hold itself prescribes. Retro-logs are exempt too: they
    # record training that already happened, so today's injury cannot block them.
    if (
        not is_retro_log
        and status_value in _TRAINING_COMPLETION_STATUSES
        and not _completion_session_is_support(plan_row, training_day, session_id)
        and not stopped_started_session
    ):
        if severe is not None:
            label = severe.get("label") or build_injury_label(
                severe.get("body_area"), severe.get("description")
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Blocked by an active severe injury ({label}). "
                    "Clear it before training this session."
                ),
            )

    if not is_retro_log and status_value in _TRAINING_COMPLETION_STATUSES and not stopped_started_session and command:
        # A support exemption only covers injury-driven load restrictions. It
        # cannot override a current red-flag STOP. Apply current readiness even
        # when an unsupported injury has no live prescription to carry a hold.
        # The same rule Today shows (session_hold); nothing here re-derives it.
        if command.today.decision_tier == "stop" and command._injury_hold_stop:
            raise HTTPException(409, "This session is on hold for the current injury guidance.")
        hold = session_hold(command.today.decision_tier, live, session_id=session_id,
                            check_in_possible=check_in_possible(store, athlete_id))
        if hold["held"]:
            raise HTTPException(409, hold["reason"])

    existing_plan_id = str(existing.get("plan_id") or "")
    if existing_plan_id and existing_plan_id != plan_id:
        # A completion row is one immutable plan/session/day occurrence. Letting
        # the upsert rebind that identity to another plan would also let old
        # rehab work inherit whatever injury episode is open at the time of the
        # rebind. A genuinely different occurrence needs a different completion
        # identity instead.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="session_completion_plan_mismatch",
        )

    frozen = existing.get("prescription_snapshot")
    if frozen and sanitize_surface_guidance(frozen) != frozen and status_value in _TRAINING_COMPLETION_STATUSES and not stop_requested:
        raise HTTPException(409, "Saved surface guidance was withdrawn. Refresh Today before training.")
    if not is_retro_log and status_value in _TRAINING_COMPLETION_STATUSES and str(existing.get("status")) not in {"done", "modified"}:
        if command and not live and any(injury.get("rehab_decision", {}).get("activation") in {"live", "retired"} for injury in command.open_injuries):
            raise HTTPException(409, "Refresh Today before training: no current session matches this request.")
        if live:
            if not standalone:
                raise HTTPException(409, "Refresh Today to use the current session.")
            if live.get("safety_hold") and not stopped_started_session:
                raise HTTPException(409, "This session is on hold for the current injury guidance.")
            if not frozen and not stopped_started_session and payload.get("prescription_revision") != live.get("revision"):
                raise HTTPException(409, "Your session prescription changed. Refresh Today before starting.")
            if not frozen and not stopped_started_session:
                frozen = live
    performance = payload.get("rehab_performance") or existing.get("rehab_performance")
    if frozen and status_value in _TRAINING_COMPLETION_STATUSES and not stop_requested and not accepted_stop and str(existing.get("status")) not in {"done", "modified"}:
        from api.services.clinical_review_freeze import frozen_review_hold
        if frozen_review_hold(store, athlete_id, frozen,
                work_state="started" if existing.get("status") == "started" else "unstarted",
                as_of=now or datetime.now(timezone.utc)):
            raise HTTPException(409, "The clinical review or prescribed work changed. Stop and refresh Today.")
    # The rehab answer is about the rehab block. A session saved as modified
    # because a different block was skipped can still have its rehab done as shown.
    if performance == "done_as_shown" and (not frozen or status_value not in {"done", "modified"}):
        raise HTTPException(422, "Confirm rehab as shown only for a completed, saved prescription.")
    if (status_value == "done" and frozen and any(b.get("block_type") == "rehab" for b in frozen.get("session", {}).get("blocks", []))
            and performance not in {"done_as_shown", "changed", "stopped", "skipped"}):
        raise HTTPException(422, "Choose Completed, Modified or Skipped for rehab.")

    # Stamp timestamps from the transition. started/done/modified carry
    # started_at; done/modified carry completed_at. Both are preserved once set
    # (idempotent re-saves keep the original time) and cleared when the status
    # moves back to a state that should not have them.
    started_at, completed_at = _completion_timestamps(existing, status_value, now_iso)

    modification_reason = str(payload.get("modification_reason") or "")
    notes = str(payload.get("notes") or "")

    # Enforce the completion contract (started_at / completed_at / reason rules)
    # before writing — surface a 422 rather than persisting an invalid record.
    try:
        SessionCompletionRecord(
            user_id=athlete_id,
            plan_id=plan_id,
            session_id=session_id,
            training_day=training_day,
            status=status_value,  # type: ignore[arg-type]
            session_rpe=payload.get("session_rpe"),
            pain_after=payload.get("pain_after"),
            modification_reason=modification_reason,
            notes=notes,
            started_at=started_at,
            completed_at=completed_at,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid session completion: {exc.errors()[0]['msg']}",
        ) from exc

    fields = {
        "completion_origin": "reported",
        "plan_id": plan_id,
        "session_id": session_id,
        "training_day": training_day,
        "status": status_value,
        "session_rpe": payload.get("session_rpe"),
        "pain_after": payload.get("pain_after"),
        "modification_reason": modification_reason,
        "notes": notes,
        "started_at": started_at,
        "completed_at": completed_at,
    }
    if frozen:
        fields["prescription_snapshot"] = frozen
        fields["rehab_performance"] = performance or existing.get("rehab_performance")
    elif stopped_started_session:
        fields["rehab_performance"] = "stopped"
    elif performance in {"changed", "stopped", "skipped"}:
        fields["rehab_performance"] = performance
    row = store.upsert_session_completion(athlete_id, fields)
    # Keep day-level progress without inventing sibling actuals. Only the
    # explicitly submitted row is execution evidence; sibling progress credit
    # cannot overwrite reported or legacy occurrences.
    day_session_ids = _day_session_ids(plan_row, training_day)
    held_session_ids = {str(change["session_id"]) for change in (frozen or {}).get("changes", [])
                        if change.get("action") == "held" and change.get("session_id")}
    if session_id in day_session_ids and not stopped_started_session:
        for sibling_id in day_session_ids:
            if sibling_id == session_id or sibling_id in held_session_ids:
                continue
            sibling = store.get_session_completion(athlete_id, sibling_id, training_day) or {}
            sibling_plan_id = str(sibling.get("plan_id") or "")
            if sibling_plan_id and sibling_plan_id != plan_id:
                continue
            if sibling and sibling.get("completion_origin") != "day_credit":
                continue
            sibling_started_at, sibling_completed_at = _completion_timestamps(
                sibling, status_value, now_iso
            )
            store.upsert_session_completion(
                athlete_id,
                {
                    "plan_id": plan_id,
                    "session_id": sibling_id,
                    "training_day": training_day,
                    "status": status_value,
                    "completion_origin": "day_credit",
                    "session_rpe": None,
                    "pain_after": None,
                    "notes": "",
                    "modification_reason": "Day progress credit" if status_value in {"modified", "skipped"} else "",
                    "started_at": sibling_started_at,
                    "completed_at": sibling_completed_at,
                },
            )
    # Response-only transition marker. It is deliberately added after the
    # database write so it can never become part of the completion schema. The
    # rehab-response capture path uses it to initialize durable context exactly
    # once, rather than retroactively resolving an old completion against a
    # newly opened injury episode.
    row["_entered_completed_status"] = (
        status_value in {"done", "modified"}
        and str(existing.get("status") or "not_started") not in {"done", "modified"}
    )
    from api.plan_mappers import _decode_structured_text
    if (_decode_structured_text(plan_row.get("planning_brief")) or {}).get("custom_plan"):
        # All scheduled sessions share the existing day-level completion. A
        # retry submitted through a sibling must not claim another XP award.
        row["_day_credit_key"] = f"{athlete_id}:{training_day}"
    return row


def submit_today_injury_checkin(
    store: AppStore,
    *,
    athlete_id: str,
    payload: Mapping[str, Any],
    athlete_timezone: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reconcile a day's declared injuries against the athlete's open flags.

    Validates each declaration via the ``DeclaredInjury`` contract, then applies
    the deterministic create/update plan: new injuries open a flag, easing ones
    move to ``monitoring``, resolved ones close (stamping ``resolved_at``), and a
    reopened flag clears its ``resolved_at`` and the database rotates its
    evidence episode. Foreign ``flag_id``s can't be updated — only ids belonging
    to the athlete are honoured. Returns the
    refreshed open-injury list and the exact existing flag ids successfully
    updated by this request.
    """
    raw_injuries = payload.get("injuries") or []
    try:
        declared = [DeclaredInjury(**dict(item)) for item in raw_injuries]
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid injury check-in: {exc.errors()[0]['msg']}",
        ) from exc

    all_flags = list(store.list_injury_flags(athlete_id, statuses=("open", "monitoring", "resolved")) or [])
    open_flags = [flag for flag in all_flags if flag.get("status") in ("open", "monitoring")]
    resolved_flag_ids = [str(flag.get("id")) for flag in all_flags if flag.get("status") == "resolved" and flag.get("id")]
    open_flag_ids = [str(flag.get("id")) for flag in open_flags if flag.get("id")]
    declared, severity_provenance = _sync_surface_severity_from_checkin(declared, open_flags)
    try:
        plan = reconcile_injury_checkin(
            declared=declared,
            open_flag_ids=open_flag_ids,
            resolved_flag_ids=resolved_flag_ids,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid injury check-in: {str(exc)}",
        ) from exc

    now_iso = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    training_day = resolve_training_day(athlete_timezone, now=now)
    active_plan_row = resolve_active_plan(
        store,
        athlete_id,
        current_training_day=training_day,
    ).plan
    plan_id = str(active_plan_row.get("id") or "").strip() if active_plan_row else None

    for fields in plan.creates:
        identity = injury_evidence_identity(str(fields.get("body_area") or ""), str(fields.get("description") or ""))
        store.create_injury_flag(athlete_id, {**fields, **identity, "plan_id": plan_id})

    for update in plan.updates:
        fields = dict(update.fields)
        # Stamp resolution time on close; clear it when a flag is reopened so a
        # later "it came back" report doesn't keep a stale resolved_at.
        fields["resolved_at"] = now_iso if fields.get("status") == "resolved" else None
        # Server-owned severity provenance, written alongside the severity it
        # describes so the pair can never drift (a floor without the athlete's
        # value under it could not be released).
        fields.update(severity_provenance.get(update.flag_id, {}))
        store.update_injury_flag(update.flag_id, fields)

    open_after = _with_rehab_stage(
        _with_surface_class(
            store.list_injury_flags(athlete_id, statuses=("open", "monitoring")) or []
        ),
        store=store,
        athlete_id=athlete_id,
        current_checkin=_checked_today_checkin(store, athlete_id, plan_id, training_day),
    )
    # Only a load-relevant injury reported worse escalates the day. A worsening
    # skin injury is routed by the surface evaluator instead (see
    # ``_load_relevant_worse_injury``).
    open_after = _with_injury_policy(_with_safe_session_context(open_after), store=store, athlete_id=athlete_id)
    injury_reported_worse = _load_relevant_worse_injury(open_after)
    # With or without a plan: a plan-free check-in is re-evaluated the same way.
    refreshed_recommendation = _refresh_today_recommendation_after_injury_change(
        store,
        athlete_id=athlete_id,
        plan_row=active_plan_row or None,
        training_day=training_day,
        open_injuries=open_after,
        injury_reported_worse=injury_reported_worse,
    )
    return {
        "open_injuries": open_after,
        "recommendation": refreshed_recommendation,
        "updated_injury_ids": [update.flag_id for update in plan.updates],
    }


def _session_id_for_entry(entry: Any) -> str | None:
    """A stable session id for a derived weekly-schedule day entry.

    Structured plan entries use only their explicit session identity. Calendar
    dates and weekday labels describe when a structured session happens; using
    either as its id creates completion rows that cannot match the prescription.
    Legacy weekly-schedule entries retain their historical date/weekday key.
    """
    if entry is None:
        return None
    if isinstance(entry, Mapping):
        explicit_session_id = entry.get("session_id")
        if entry.get("_structured_session"):
            return str(explicit_session_id) if explicit_session_id else None
        calendar_date = entry.get("calendar_date")
        weekday = entry.get("weekday", "")
    else:
        explicit_session_id = getattr(entry, "session_id", None)
        calendar_date = getattr(entry, "calendar_date", None)
        weekday = getattr(entry, "weekday", "")
    if explicit_session_id:
        return str(explicit_session_id)
    if calendar_date:
        return str(calendar_date)
    return str(weekday) or None


def _iter_mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _structured_effective_load(day_type: Any) -> str:
    normalized = _clean_text(day_type).lower()
    if normalized in {"hard", "high"}:
        return "hard"
    if normalized in {"reduced", "low", "recovery"}:
        return "reduced"
    if normalized in {"rest", "off", "none", "travel"}:
        return "none"
    if normalized in {"competition"}:
        return "hard"
    return "technical"


# Same inflection rule as the work-headline allowlist in plan_schedule.py and
# the web classifier's SPARRING_RE: the trailing ``\b`` closes the whole
# alternation, so a bare ``spar`` stem matched only the exact word and a
# blockless "Hard sparring" session produced no contact label at all.
_STRUCTURED_COACH_CONTACT_RE = re.compile(
    r"\b(coach(?:ing|ed)?|spar(?:r(?:ing|ed)|s)?|technical[\s-]+only|"
    r"no\s+hard\s+sparring|boxing|pad\s?work|pads|mitts?)\b",
    re.I,
)


def _structured_session_blocks(session: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return _iter_mapping_items(session.get("blocks"))


def _structured_contact_label_from_session(session: Mapping[str, Any]) -> str:
    text = " ".join(
        part
        for part in (
            _clean_text(session.get("title")),
            _clean_text(session.get("objective")),
            _clean_text(session.get("session_type")),
        )
        if part
    )
    if not text or not _STRUCTURED_COACH_CONTACT_RE.search(text):
        return ""
    return _clean_text(session.get("title")) or _clean_text(session.get("objective")) or text


def _select_structured_primary_session(sessions: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Choose the app-owned session summary when coach contact coexists.

    The structured card can carry a coach-owned contact session and a real app
    session on the same day. Overview/Today only have one compact session slot,
    so prefer the first session with executable blocks; the structured blocks UI
    still renders every session from the full card.

    Sessions are listed in execution order (fightcamp.session_sequencing), so
    joint prep or a tactical card can lead the day. Support-only work is passed
    over when the day also holds main training, keeping the slot on that work.
    """
    # Optional work is offered beside the day, never the day itself.
    sessions = [session for session in sessions if not is_optional_session(session)]
    if not sessions:
        return None
    with_blocks = [session for session in sessions if _structured_session_blocks(session)]
    main = [session for session in with_blocks if not _is_day_support_session(session)]
    if main:
        return main[0]
    if with_blocks:
        return with_blocks[0]
    return sessions[0]


# One Today build asks several helpers about the same plan card, and resolving
# the effective card re-validates all of it. Inside a build the plan row is only
# read, so the resolved card and each day's projection are computed once. Keys
# hold the row itself so an id() is never matched against a different row.
_STRUCTURED_PROJECTION_MEMO: ContextVar[dict[tuple[Any, ...], tuple[Mapping[str, Any], Any]] | None] = (
    ContextVar("today_structured_projection_memo", default=None)
)


@contextmanager
def structured_projection_memo() -> Iterator[None]:
    """Resolve each plan card once for the duration of one Today build."""
    if _STRUCTURED_PROJECTION_MEMO.get() is not None:
        yield
        return
    token = _STRUCTURED_PROJECTION_MEMO.set({})
    try:
        yield
    finally:
        _STRUCTURED_PROJECTION_MEMO.reset(token)


def _memoized(key: tuple[Any, ...], plan_row: Mapping[str, Any], compute: Callable[[], Any]) -> Any:
    memo = _STRUCTURED_PROJECTION_MEMO.get()
    if memo is None:
        return compute()
    cached = memo.get(key)
    if cached is not None and cached[0] is plan_row:
        return cached[1]
    value = compute()
    memo[key] = (plan_row, value)
    return value


def _projected_structured_plan(
    plan_row: Mapping[str, Any], *, training_day: str | None = None
) -> tuple[list[Mapping[str, Any]], Mapping[str, Any]]:
    def _project() -> tuple[list[Mapping[str, Any]], Mapping[str, Any]]:
        structured_plan = _memoized(
            ("effective", id(plan_row)),
            plan_row,
            lambda: resolve_effective_structured_plan(plan_row),
        )
        if structured_plan is None:
            return [], {}
        projected, context = project_open_structured_plan(
            plan_row,
            structured_plan,
            current_training_day=training_day,
        )
        # The athlete's swaps/adds/removals, on dated days only. Every Today,
        # start and logging read goes through here, so all of them see the same
        # effective workout as Plan Detail.
        projected = apply_exercise_overrides(plan_row, projected) or projected
        return _iter_mapping_items(projected.get("weeks")), context

    return _memoized(("projection", id(plan_row), training_day), plan_row, _project)


def _structured_plan_weeks(
    plan_row: Mapping[str, Any], *, training_day: str | None = None
) -> list[Mapping[str, Any]]:
    return _projected_structured_plan(plan_row, training_day=training_day)[0]


def _normalized_structured_phase(value: Any) -> str:
    phase = _clean_text(value).upper().replace(" ", "_")
    if phase in {"GPP", "SPP", "TAPER", "REINTEGRATION"}:
        return phase
    for candidate in ("REINTEGRATION", "TAPER", "SPP", "GPP"):
        if candidate in phase:
            return candidate
    return ""


def _structured_phase_for_day(plan_row: Mapping[str, Any], training_day: str) -> str:
    for week in _structured_plan_weeks(plan_row, training_day=training_day):
        if not isinstance(week, Mapping):
            continue
        for day in _iter_mapping_items(week.get("days")):
            if not isinstance(day, Mapping):
                continue
            day_date = _clean_text(day.get("date"))[:10]
            if day_date == training_day:
                return _normalized_structured_phase(day.get("phase_label")) or _normalized_structured_phase(
                    week.get("phase_label")
                )
    return ""


def _structured_session_entry_for_day(
    day: Mapping[str, Any],
    *,
    week: Mapping[str, Any],
    session_override: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    day_date = _clean_text(day.get("date"))[:10]
    if not day_date:
        return None
    if (session_override is not None and is_optional_session(session_override)
            and session_override.get("prescription_source") == "athlete"
            and session_override.get("session_type") == "recovery"):
        return None
    sessions = _iter_mapping_items(day.get("sessions"))
    if (sessions and str(day.get("day_type")) in {"rest", "recovery"}
            and all(s.get("prescription_source") == "athlete" for s in sessions)
            and not any(not is_optional_session(s) for s in sessions)):
        return None
    today_card = day.get("today_card") if isinstance(day.get("today_card"), Mapping) else {}
    coach_led_contact = _clean_text(today_card.get("coach_led_contact"))
    if not coach_led_contact:
        for candidate in sessions:
            if _structured_session_blocks(candidate):
                continue
            coach_led_contact = _structured_contact_label_from_session(candidate)
            if coach_led_contact:
                break

    # ``session_override`` names one session on a multi-session day (see
    # _structured_day_session_entries). Without it the day's primary session is
    # summarized, which is the single-session behaviour every caller had before.
    first_session = (
        session_override
        if session_override is not None
        else _select_structured_primary_session(sessions)
    )
    if first_session is not None:
        session = dict(first_session)
    else:
        headline = _clean_text(today_card.get("headline"))
        if not headline:
            return None
        session = {
            "session_type": "scheduled_session",
            "title": headline,
            "objective": _clean_text(today_card.get("primary_warning")),
        }

    title = (
        _clean_text(session.get("title"))
        or _clean_text(today_card.get("headline"))
        or _clean_text(session.get("session_type"))
        or "Today's session"
    )
    objective = _clean_text(session.get("objective")) or _clean_text(today_card.get("headline"))
    session_id = _clean_text(session.get("session_id"))
    weekday = ""
    try:
        weekday = datetime.strptime(day_date, "%Y-%m-%d").strftime("%A")
    except ValueError:
        weekday = ""

    # The day-level load comes from day_type, but a rest/travel day_type must not
    # zero out a day that actually schedules work: recovery fillers (breathing,
    # mobility, mental cues) are routinely placed on "rest" days, and mapping the
    # whole entry to "none" made has_scheduled_day_content() drop them from Today.
    effective_load = _structured_effective_load(day.get("day_type"))
    if effective_load == "none":
        effective_load = "reduced"

    entry = {
        **session,
        "_structured_session": True,
        "calendar_date": day_date,
        "weekday": weekday,
        "weekday_with_label": _clean_text(day.get("countdown_label")) or weekday,
        "day_label": _clean_text(day.get("countdown_label")),
        "title": title,
        "status": _clean_text(session.get("session_type")) or "scheduled_session",
        "coach_note": objective,
        "effective_load": effective_load,
        "phase": _normalized_structured_phase(day.get("phase_label"))
        or _normalized_structured_phase(week.get("phase_label")),
        **({"coach_led_contact": coach_led_contact} if coach_led_contact else {}),
    }
    if session_id:
        entry["session_id"] = session_id
    # A day with no session objects is only a session when its headline names
    # real work. "Rest or active recovery" is a rest day even though it says
    # "recovery", while "Rhythm flush" is the low-cost support work Today should
    # surface. has_scheduled_day_content (api/services/plan_schedule.py) owns
    # that rule for the whole service, so this defers to it rather than keeping
    # a second vocabulary that answers the same question differently.
    if first_session is None and not has_scheduled_day_content(entry):
        return None
    if first_session is None:
        # Deterministic calendar rows can prescribe a headline-only support
        # session. Its dated day is the canonical legacy identity; this keeps it
        # completable without consulting the recurring weekly metadata.
        entry["session_id"] = day_date
    return entry


def _open_plan_week_position(
    plan_row: Mapping[str, Any],
    *,
    week_count: int,
    training_date: date | None,
    context: Mapping[str, Any],
) -> int | None:
    """0-based index of the renewable block week containing today.

    Mirrors the app (``resolveOpenPlanWeekNumber`` in web/lib/camp-map.ts): the
    projection's own week number wins, otherwise the week is counted from the
    anchor (first Monday on or after the plan was created), wrapping every block
    so it renews indefinitely.
    """
    if week_count <= 0:
        return None
    explicit = context.get("current_week_number")
    if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit >= 1:
        return min(explicit, week_count) - 1
    anchor = _parse_structured_date(context.get("anchor_date")) or open_plan_anchor_date(plan_row)
    if anchor is None or training_date is None:
        return None
    elapsed = (training_date - anchor).days
    if elapsed < 0:
        return 0
    return (elapsed // 7) % week_count


def _structured_day_for_training_day(
    plan_row: Mapping[str, Any], training_day: str
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    """Today's row in the plan card as ``(day, week)``, matched as the app matches it.

    Calendar date first. An open / renewable plan's card carries a weekly rhythm
    rather than live dates, and its date projection is unavailable whenever the
    card and the saved weekly template disagree, so today's weekday inside the
    current block week is the fallback — the same rule the app resolves the
    blocks it renders with. Without it the server sees no card for today and
    falls back to the template's generic guess, which is how a plan REST day
    ended up presented as a startable session.
    """
    # Before the first open-plan anchor, a same-weekday row belongs to the
    # upcoming block (for example Sat 8 Aug viewed on Sat 1 Aug), not today.
    if _open_plan_has_not_started(plan_row, training_day):
        return None

    weeks, context = _projected_structured_plan(plan_row, training_day=training_day)
    for week in weeks:
        for day in _iter_mapping_items(week.get("days")):
            if _clean_text(day.get("date"))[:10] == training_day:
                return day, week

    if not weeks or open_plan_spec(plan_row) is None:
        return None
    training_date = _parse_structured_date(training_day)
    if training_date is None:
        return None
    target_weekday = WEEKDAY_TOKENS[training_date.weekday()]
    preferred = _open_plan_week_position(
        plan_row,
        week_count=len(weeks),
        training_date=training_date,
        context=context,
    )
    # The block's current week owns the match; the other weeks are the fallback,
    # so an open plan still resolves when no anchor is available (every week of
    # a block shares the same weekly rhythm).
    order = [preferred] if preferred is not None else []
    order.extend(index for index in range(len(weeks)) if index != preferred)
    for index in order:
        week = weeks[index]
        for day in _iter_mapping_items(week.get("days")):
            if normalize_weekday(day.get("weekday")) == target_weekday:
                return day, week
    return None


class _StructuredToday(NamedTuple):
    """What the plan card says about today.

    ``entries`` is every session the card schedules today, in the order Today
    offers them, and is populated only for a dated row (an entry has to be keyed
    on a calendar date). ``entry`` is the first of them — the day's primary
    session — which is what callers that only have one session slot want.
    ``is_rest_day`` means the card has a row for today and that row schedules no
    work — the one thing a weekday-only row can still answer, and the answer the
    intake weekly template must not override.
    """

    entries: list[dict[str, Any]]
    is_rest_day: bool

    @property
    def entry(self) -> dict[str, Any] | None:
        return self.entries[0] if self.entries else None


def _structured_today(plan_row: Mapping[str, Any], training_day: str) -> _StructuredToday:
    matched = _structured_day_for_training_day(plan_row, training_day)
    if matched is None:
        return _StructuredToday([], False)
    day, week = matched
    entries = _structured_day_session_entries(day, week=week)
    if entries:
        return _StructuredToday(entries, False)
    # A weekday-only row carries no date to key a session on, so it is asked the
    # same question with today's date standing in for the missing one: does this
    # row schedule work at all? Only its answer to that is used — a row with work
    # still resolves through the weekly schedule, so session identity is unchanged.
    dated = _structured_session_entry_for_day({**day, "date": training_day}, week=week)
    return _StructuredToday([], dated is None)


def _structured_today_session_entry(plan_row: Mapping[str, Any], training_day: str) -> dict[str, Any] | None:
    return _structured_today(plan_row, training_day).entry


def _structured_day_session_entries(
    day: Mapping[str, Any], *, week: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Every session the card schedules on ``day``, in the order Today offers them.

    The day's primary session leads; the remaining sessions follow in card
    order. The day is logged as one unit, so the full list is what a single
    completion is written to and what "is this day logged" is asked of.
    """
    primary = _structured_session_entry_for_day(day, week=week)
    entries = [primary] if primary is not None else []
    seen = {_session_id_for_entry(primary)} if primary is not None else set()
    for session in _iter_mapping_items(day.get("sessions")):
        # Optional work is not part of the day unit: no shared log is written
        # to it and it never decides whether the day is done.
        if is_optional_session(session):
            continue
        entry = _structured_session_entry_for_day(day, week=week, session_override=session)
        if entry is None:
            continue
        session_id = _session_id_for_entry(entry)
        # An unidentified session cannot carry a durable completion, so it would
        # reopen forever. Only addressable sessions join the rotation.
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)
        entries.append(entry)
    return entries


def _session_entry_is_complete(
    entry: Mapping[str, Any] | None,
    *,
    is_complete: Callable[[str, str], bool] | None,
    calendar_date: str | None = None,
) -> bool:
    """Whether ``entry`` already has a terminal completion on its own day."""
    if entry is None or is_complete is None:
        return False
    session_id = _session_id_for_entry(entry)
    if not session_id:
        return False
    day = calendar_date or _clean_text(entry.get("calendar_date"))[:10]
    if not day:
        return False
    return bool(is_complete(session_id, day))


def _structured_next_session_entry(
    plan_row: Mapping[str, Any],
    training_day: str,
    *,
    is_complete: Callable[[str, str], bool] | None = None,
) -> dict[str, Any] | None:
    training_date = _parse_structured_date(training_day)
    if training_date is None:
        return None
    future_days: list[tuple[date, str, Mapping[str, Any], Mapping[str, Any]]] = []
    for week in _structured_plan_weeks(plan_row, training_day=training_day):
        for day in _iter_mapping_items(week.get("days")):
            day_date = _clean_text(day.get("date"))[:10]
            parsed_day_date = _parse_structured_date(day_date)
            if parsed_day_date is None or parsed_day_date <= training_date:
                continue
            future_days.append((parsed_day_date, day_date, day, week))
    # Earliest day first (a stable sort keeps card order on a shared date), and
    # stop at the first day that qualifies: every completion check is a store
    # read, so checking the whole remaining camp cost one read per future session.
    future_days.sort(key=lambda item: item[0])
    for _parsed_day_date, day_date, day, week in future_days:
        # A future day is one unit like today: it offers its primary session,
        # and a day already logged ahead of time never becomes "next".
        entries = [
            entry
            for entry in _structured_day_session_entries(day, week=week)
            if has_scheduled_day_content(entry)
        ]
        if not entries or any(
            _session_entry_is_complete(entry, is_complete=is_complete, calendar_date=day_date)
            for entry in entries
        ):
            continue
        return entries[0]
    return None


def _entry_calendar_date(entry: Any) -> date | None:
    if entry is None:
        return None
    calendar_date = (
        entry.get("calendar_date")
        if isinstance(entry, Mapping)
        else getattr(entry, "calendar_date", None)
    )
    return _parse_structured_date(calendar_date)


def _prefer_earlier_structured_next_entry(target_entry: Any, structured_next_entry: dict[str, Any] | None) -> Any:
    if structured_next_entry is None:
        return target_entry
    if target_entry is None:
        return structured_next_entry
    target_date = _entry_calendar_date(target_entry)
    structured_date = _entry_calendar_date(structured_next_entry)
    if structured_date is None:
        return target_entry
    if target_date is None or structured_date <= target_date:
        return structured_next_entry
    return target_entry


def _scan_forward_for_next_training(
    plan_row: Mapping[str, Any],
    *,
    week: Any,
    week_index: int,
    training_date: Any,
) -> Any:
    """Find the next training day in a later schedule week.

    The within-week resolver (``resolve_today_and_next``) only looks at the
    remaining days of the *current* week. Taper and late-camp weeks frequently
    carry just one or two training days, so on every non-training day the rest of
    the week is empty and the genuine next session sits in a future week. Without
    crossing the week boundary the Overview "Next session" card reports
    "No session found" even though training is still scheduled. This scan walks
    subsequent weeks (in order) and returns the first day that carries training,
    so the card always reflects the next real session while one exists.
    """
    if week is None:
        return None
    week_count = int(getattr(week, "week_count", 0) or 0)
    for index in range(week_index + 1, week_count):
        later_week = weekly_schedule_or_none(plan_row, week_index=index)
        if later_week is None:
            continue
        dated_candidates = []
        undated_candidates = []
        for entry in getattr(later_week, "days", []) or []:
            if not has_scheduled_day_content(entry):
                continue
            calendar_date = (
                entry.get("calendar_date")
                if isinstance(entry, Mapping)
                else getattr(entry, "calendar_date", None)
            )
            entry_date = parse_iso_date(calendar_date) if calendar_date else None
            # Dated plans: never surface a session on/before today. Undated plans
            # (weekday-only fallback) can't be compared, so any later-week
            # training day is the next session.
            if entry_date is not None and training_date is not None and entry_date <= training_date:
                continue
            if entry_date is not None:
                dated_candidates.append((entry_date, entry))
            else:
                undated_candidates.append(entry)
        if dated_candidates:
            dated_candidates.sort(key=lambda item: item[0])
            return dated_candidates[0][1]
        if undated_candidates:
            return undated_candidates[0]
    return None


def _next_session_payload(entry: Any, session_id: str | None, *, relation: str | None = None) -> dict[str, Any]:
    if entry is None:
        return {}
    data = entry.model_dump() if hasattr(entry, "model_dump") else dict(entry)
    data.pop("_structured_session", None)
    if session_id:
        data["session_id"] = session_id
    if relation:
        data["session_relation"] = relation
    return data


def _risks_from_checkin(checkin: Mapping[str, Any] | None):
    """Minimal, prioritized risk-watch derived from the live check-in."""
    if not checkin:
        return []
    risks = []
    state = str(checkin.get("recommendation_state") or "")
    if state == "pull_back":
        risks.append(
            make_risk(
                "stop_red_flag",
                text="Recommendation: pull back today.",
                timeframe="today",
            )
        )
    if str(checkin.get("active_injury")) == "worse":
        risks.append(
            make_risk(
                "active_injury_worse",
                text="Active injury reported as worse during today's check-in.",
                timeframe="today",
            )
        )
    if str(checkin.get("pain")) == "high":
        risks.append(
            make_risk(
                "high_pain",
                text="High pain reported during today's check-in.",
                timeframe="today",
            )
        )
    # Taper deliberately produces no risk row. It fired on phase alone, so every
    # athlete in taper carried a permanent caution-toned entry with a "!" icon,
    # counted in Overview's "N active warnings" and able to render as the day's
    # "strongest signal". Being in taper is the plan working, not a risk. It now
    # appears as CONTEXT on the decision card, where it explains the call without
    # being mistaken for a problem.
    if str(checkin.get("sleep")) == "poor" or str(checkin.get("body")) == "flat":
        risks.append(
            make_risk(
                "fatigue",
                text="Fatigue reported during today's check-in.",
                timeframe="today",
            )
        )
    return risks


def _history_injury_risks(
    store: AppStore,
    *,
    athlete_id: str,
    training_day: str,
    current_phase: str | None = None,
) -> list[RiskWatchItem]:
    """Derive an injury-risk item from logged post-session pain history.

    A transient read failure must never crash Overview; the derived signal is
    simply skipped.
    """
    try:
        completions = store.list_session_completions(athlete_id) or []
    except Exception:
        return []
    return derive_injury_signal(
        completions=completions,
        current_training_day=training_day,
        current_phase=current_phase,
    )


def _open_injury_flags(store: AppStore, athlete_id: str) -> list[dict[str, Any]]:
    """Open/monitoring injury flags for this athlete, defensively.

    Resilient to a read failure — Overview must never crash because the injury
    list could not be loaded.
    """
    lister = getattr(store, "list_injury_flags", None)
    if not callable(lister):
        return []
    try:
        flags = lister(athlete_id, statuses=("open", "monitoring"), limit=500)
        return [dict(flag) for flag in (flags or [])]
    except Exception:
        if any(policy.activation == "live" for policy in load_clinical_policies()):
            raise HTTPException(503, "Current injury guidance could not be loaded. Refresh before training.")
        return []


def _normalized_injury_key(value: object) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _injury_dedupe_keys(flag: Mapping[str, Any]) -> set[str]:
    return {
        key
        for key in (
            _normalized_injury_key(flag.get("body_area")),
            _normalized_injury_key(flag.get("description")),
        )
        if key
    }


def _is_truthy_cleared(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in INTAKE_CLEARED_VALUES


def _guided_injury_has_content(injury: Mapping[str, Any]) -> bool:
    return any(
        str(injury.get(field) or "").strip()
        for field in (
            "area",
            "zone",
            "notes",
            "avoid",
            "injury_type",
            "surface_type",
            "timeframe",
        )
    )


def _body_area_from_guided_injury(injury: Mapping[str, Any]) -> str:
    area = str(injury.get("area") or "").strip()
    if area:
        return area
    zone = str(injury.get("zone") or "").strip()
    return BODY_MAP_ZONE_LABELS.get(zone, zone.replace("_", " ").strip())


def _flag_severity_from_guided_injury(injury: Mapping[str, Any]) -> str:
    raw = str(injury.get("severity") or "").strip().lower()
    return GUIDED_INJURY_SEVERITY_MAP.get(raw, "moderate")


def _flag_status_from_guided_injury(injury: Mapping[str, Any]) -> str:
    return "monitoring" if str(injury.get("trend") or "").strip().lower() == "improving" else "open"


def _details_already_include_body_area(body_area: str, details: str) -> bool:
    body_key = _normalized_injury_key(body_area)
    details_key = _normalized_injury_key(details)
    return bool(body_key and (details_key == body_key or details_key.startswith(f"{body_key} ")))


def _humanized_guided_token(value: object) -> str:
    return " ".join(str(value or "").replace("_", " ").split())


def _guided_subtype_word(subtype: object) -> str:
    """The athlete-facing condition word inside a taxonomy subtype token.

    Subtypes are stored as ``family:specific`` (``surface_injury:blister``). Only
    the specific half is a word an athlete recognises; the family is the routing
    key, and it is already implied by the word it qualifies.
    """
    text = str(subtype or "").strip()
    if not text:
        return ""
    return _humanized_guided_token(text.rsplit(":", 1)[-1]) or _humanized_guided_token(text)


def _format_guided_injury_description(body_area: str, injury: Mapping[str, Any]) -> str:
    """Athlete-facing description for a flag bootstrapped from guided intake.

    The description is rendered on the injury card and in history, so it carries
    the condition and the athlete's own notes — never the taxonomy plumbing.
    A blister reads "Right shoulder: blister", not "Right shoulder: blister.
    surface injury. surface injury:blister". Nothing the injury scorer routes on
    is lost: the specific condition word is what it reads, and the structured
    wound answers travel in their own columns.
    """
    parts: list[str] = []
    surface_type = _humanized_guided_token(injury.get("surface_type"))
    injury_type = _humanized_guided_token(injury.get("injury_type"))
    raw_subtypes = injury.get("injury_subtypes")
    subtype_words = (
        [word for item in raw_subtypes if (word := _guided_subtype_word(item))]
        if isinstance(raw_subtypes, list)
        else []
    )
    if surface_type:
        parts.append(surface_type)
    # ``surface_injury`` is the family a wound is routed by, and its specific word
    # (blister / graze / cut) says the same thing in the athlete's language. Keep
    # the family only when nothing more specific is available to say it.
    # Casefolded: the token is an enum, but nothing guarantees the casing it was
    # stored in, and a "Surface_Injury" that misses this comparison leaks the
    # family straight onto the card.
    if injury_type and not (
        injury_type.casefold() == "surface injury" and (surface_type or subtype_words)
    ):
        parts.append(injury_type)
    # ``timeframe`` is deliberately absent: it is a structured enum about WHEN
    # the injury happened ("last month", "one to three months", "old cleared"),
    # not what it is, and it read as planner vocabulary in the middle of the
    # athlete's own words. The intake payload still carries it.
    parts.extend(subtype_words)
    for field in ("notes", "avoid"):
        value = str(injury.get(field) or "").strip()
        if value:
            parts.append(value)
    # Case-insensitive dedupe: a surface type and its subtype word are the same
    # fact stored twice, and repeating it reads as noise.
    seen: set[str] = set()
    deduped: list[str] = []
    for part in parts:
        key = part.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(part)
    details = ". ".join(deduped)
    if body_area and details and _details_already_include_body_area(body_area, details):
        return details
    if body_area and details:
        return f"{body_area}: {details}"
    return body_area or details


# Guided intake asks the surface-safety questions in its own vocabulary. The
# canonical classifier reads the daily check-in's vocabulary. Without this
# translation the bootstrapped flag carries no structured wound state at all, and
# an open, infected or uncontrolled-bleeding intake cut is classified as
# `stable_surface` in Today — a wound that triaged as needing review at intake
# silently becomes "no session change".
_GUIDED_OPEN_WOUND_TO_SKIN_INTEGRITY = {
    "yes": "open",
    "true": "open",
    "open": "open",
    "burst": "open",
    "no": "intact",
    "false": "intact",
    "closed": "intact",
    "intact": "intact",
    "not_sure": "unknown",
    "unsure": "unknown",
    "unknown": "unknown",
}
_GUIDED_BLEEDING_TO_CANONICAL = {
    "wont_stop": "uncontrolled",
    "won't_stop": "uncontrolled",
    "uncontrolled": "uncontrolled",
    "a_little": "controlled",
    "controlled": "controlled",
    "none": "none",
    "no": "none",
    "stopped": "none",
}
# Matches the injury_flags column constraint. An oversized list would fail the
# insert, and the bootstrap swallows write errors — so an over-long answer would
# silently drop the whole wound instead of just the surplus signs.
_GUIDED_EMPTY_ANSWERS = frozenset({"", "none", "no", "nil", "n/a", "na", "unknown", "unsure", "not_sure"})


def _guided_answer(injury: Mapping[str, Any], field: str) -> str:
    value = injury.get(field)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _guided_surface_safety_fields(injury: Mapping[str, Any]) -> dict[str, object]:
    """Canonical surface-safety columns carried over from a guided intake injury.

    Only answers the athlete actually gave are written: an unanswered question
    stays absent so the classifier reads it as "unknown" rather than "clear".
    """
    fields: dict[str, object] = {}

    skin_integrity = _GUIDED_OPEN_WOUND_TO_SKIN_INTEGRITY.get(
        _guided_answer(injury, "skin_integrity")
    ) or _GUIDED_OPEN_WOUND_TO_SKIN_INTEGRITY.get(_guided_answer(injury, "open_wound"))
    if skin_integrity:
        fields["skin_integrity"] = skin_integrity

    bleeding = _GUIDED_BLEEDING_TO_CANONICAL.get(_guided_answer(injury, "bleeding_status"))
    if bleeding:
        fields["bleeding_status"] = bleeding

    raw_signs = injury.get("infection_signs")
    if isinstance(raw_signs, str):
        raw_signs = [raw_signs]
    if isinstance(raw_signs, (list, tuple, set, frozenset)):
        signs: list[str] = []
        for item in raw_signs:
            token = str(item or "").strip().lower().replace("-", "_").replace(" ", "_")
            if token and token not in _GUIDED_EMPTY_ANSWERS and token not in signs:
                signs.append(token)
        if signs:
            fields["infection_signs"] = signs[:MAX_INFECTION_SIGNS]

    # Guided intake does not ask whether the wound can be kept covered today, so
    # this is only carried when a caller supplied it.
    coverable = _guided_answer(injury, "coverable")
    if coverable in {"yes", "no", "unknown"}:
        fields["coverable"] = coverable

    drainage = _guided_answer(injury, "drainage")
    if drainage in {"none", "present", "unknown"}:
        fields["drainage"] = drainage

    return fields


def _guided_intake_injury_candidate(
    injury: Mapping[str, Any],
    *,
    plan_id: str,
) -> dict[str, object] | None:
    if _is_truthy_cleared(injury.get("cleared")):
        return None
    body_area = _body_area_from_guided_injury(injury)
    description = _format_guided_injury_description(body_area, injury)
    if not (body_area or description):
        return None
    return {
        "source": INTAKE_INJURY_SOURCE,
        "plan_id": plan_id,
        "body_area": body_area,
        "description": description or body_area,
        "severity": _flag_severity_from_guided_injury(injury),
        "status": _flag_status_from_guided_injury(injury),
        # The wound state triage already collected. Without it the canonical
        # classifier sees an unanswered wound and routes it as stable skin.
        **_guided_surface_safety_fields(injury),
    }


_INTAKE_MARKER_RE = re.compile(r"\s*\[[a-z_]+:[^\]]*\]", re.IGNORECASE)
_NEGATED_PHRASE_RE = re.compile(r"^(?:no|not|never|without|zero)\b", re.IGNORECASE)
# "no knee pain" denies a symptom; "no deep squats" is a real restriction.
_NEGATED_SYMPTOM_RE = re.compile(
    r"^(?:no|not|never|without|zero)\b.*\b(?:pain|injur\w*|issues?|problems?|sore\w*|tight\w*|niggles?)\b",
    re.IGNORECASE,
)
_TRAINING_IMPACT_MARKER_RE = re.compile(
    r"\[training_impact:(?:not_limiting|limiting|cant_train)\]", re.IGNORECASE
)


def _legacy_intake_injury_candidates(injuries_text: object, *, plan_id: str) -> list[dict[str, object]]:
    """Injury flags for free-text intake (quick build, onboarding without the guided card).

    The text is parsed into one flag per injury, each with its body area and
    condition ("Hip" / "tightness"), the shape guided intake produces. The
    stored training-impact marker is metadata: it rides on the description,
    where the impact readers look for it, and never becomes part of the area.
    Free text carries one impact choice for the whole field, so every injury
    parsed from it shares that choice.
    Text the parser cannot place keeps a single flag over the cleaned text.
    """
    text = str(injuries_text or "").strip()
    impact_match = _TRAINING_IMPACT_MARKER_RE.search(text)
    impact_marker = impact_match.group(0).lower() if impact_match else ""
    cleaned = " ".join(_INTAKE_MARKER_RE.sub(" ", text).split())
    if (
        not re.search(r"[a-z]", cleaned, re.IGNORECASE)
        or cleaned.lower() in {"none", "n/a", "na", "no", "no injuries"}
    ):
        return []

    def candidate(body_area: str, condition: str) -> dict[str, object]:
        return {
            "source": INTAKE_INJURY_SOURCE,
            "plan_id": plan_id,
            "body_area": body_area,
            "description": " ".join(part for part in (condition, impact_marker) if part),
            "severity": "moderate",
            "status": "open",
        }

    from fightcamp.injury_formatting import parse_injuries_and_restrictions

    try:
        parsed, _restrictions = parse_injuries_and_restrictions(cleaned)
    except Exception:
        logger.exception("[today_service] free-text intake injury parse failed")
        parsed, _restrictions = [], []

    candidates: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    negated_only = bool(parsed)
    for entry in parsed:
        # "no knee pain" names a body part the athlete does not have trouble with.
        if _NEGATED_PHRASE_RE.match(str(entry.get("original_phrase") or "").strip()):
            continue
        negated_only = False
        location = _humanized_guided_token(entry.get("canonical_location"))
        if not location:
            continue
        side = _humanized_guided_token(entry.get("side") or entry.get("laterality"))
        if side and side.lower() not in location.lower():
            location = f"{side} {location}"
        body_area = location[:1].upper() + location[1:]
        # The athlete's own words after the area ("bruise after sparring"),
        # falling back to the parsed condition when the phrase is only the area.
        phrase = str(entry.get("original_phrase") or "").strip()
        start = cleaned.lower().find(phrase.lower()) if phrase else -1
        if start >= 0:
            phrase = cleaned[start:start + len(phrase)]
        bare_location = re.escape(_humanized_guided_token(entry.get("canonical_location")))
        area_prefix = rf"^(?:(?:left|right|both)\s+)?{bare_location}s?\b[\s:,.-]*"
        condition = re.sub(area_prefix, "", phrase, flags=re.IGNORECASE).strip()
        if not condition:
            condition = _humanized_guided_token(entry.get("injury_type")) or "pain"
        # One injury per area and condition type: "hip tightness, tight hip" is one hip.
        key = (body_area.lower(), str(entry.get("injury_type") or condition).lower())
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate(body_area, condition))
    if candidates:
        return candidates
    if negated_only:
        return []
    # A constraint ("tight hips, avoid deep squats") can still name the area.
    restrictions = [
        restriction
        for restriction in _restrictions
        if isinstance(restriction, Mapping)
        and not _NEGATED_SYMPTOM_RE.match(str(restriction.get("original_phrase") or "").strip())
    ]
    if _restrictions and not restrictions:
        return []
    regions = {
        region
        for restriction in restrictions
        if (region := _humanized_guided_token(restriction.get("region")))
    }
    if len(regions) == 1:
        region = regions.pop()
        return [candidate(region[:1].upper() + region[1:], cleaned)]
    return [candidate(cleaned[:120], cleaned)]


def _intake_payload_from_row(row: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not row:
        return {}
    payload = row.get("intake")
    return payload if isinstance(payload, Mapping) else row


def _intake_row_for_plan(
    store: AppStore,
    *,
    athlete_id: str,
    plan_row: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    intake_id = str(plan_row.get("intake_id") or "").strip()
    if intake_id:
        row = store.get_intake(intake_id)
        if row:
            return row
    return store.get_latest_intake(athlete_id)


def _intake_injury_candidates(
    intake_payload: Mapping[str, Any],
    *,
    plan_id: str,
) -> list[dict[str, object]]:
    guided_injuries = intake_payload.get("guided_injuries")
    if isinstance(guided_injuries, list):
        guided_items = [
            injury
            for injury in guided_injuries
            if isinstance(injury, Mapping) and _guided_injury_has_content(injury)
        ]
        guided_candidates = [
            _guided_intake_injury_candidate(injury, plan_id=plan_id)
            for injury in guided_items
        ]
        if guided_items:
            return [candidate for candidate in guided_candidates if candidate is not None]

    guided_injury = intake_payload.get("guided_injury")
    if isinstance(guided_injury, Mapping) and _guided_injury_has_content(guided_injury):
        candidate = _guided_intake_injury_candidate(guided_injury, plan_id=plan_id)
        return [candidate] if candidate else []

    return _legacy_intake_injury_candidates(intake_payload.get("injuries"), plan_id=plan_id)


def _ensure_intake_injury_flags(
    store: AppStore,
    *,
    athlete_id: str,
    plan_row: Mapping[str, Any],
    open_flags: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Seed real injury flags from active-plan intake once, then return open flags."""
    plan_id = str(plan_row.get("id") or "").strip()
    if not plan_id:
        return open_flags
    intake_payload = _intake_payload_from_row(
        _intake_row_for_plan(store, athlete_id=athlete_id, plan_row=plan_row)
    )
    if not intake_payload:
        return open_flags

    # Dedupe against resolved flags too, not just open/monitoring ones. When an
    # athlete clears an intake-seeded injury its flag moves to ``resolved``; if we
    # only looked at the still-open set the next Today load would re-create the
    # flag from the unchanged intake payload and the cleared injury would
    # reappear. Including ``resolved`` keeps a cleared injury cleared.
    try:
        dedupe_flags = [
            dict(flag)
            for flag in (
                store.list_injury_flags(athlete_id, statuses=("open", "monitoring", "resolved"), limit=500)
                or []
            )
        ]
    except Exception:
        dedupe_flags = list(open_flags)

    seen_keys: set[str] = set()
    for flag in dedupe_flags:
        seen_keys.update(_injury_dedupe_keys(flag))

    seeded = list(open_flags)
    for candidate in _intake_injury_candidates(intake_payload, plan_id=plan_id):
        candidate_keys = _injury_dedupe_keys(candidate)
        if not candidate_keys or candidate_keys & seen_keys:
            continue
        try:
            created = dict(store.create_injury_flag(athlete_id, candidate))
        except Exception:
            # Today should still load if the best-effort intake bootstrap write
            # hits a transient store/schema issue.
            continue
        seeded.insert(0, created)
        seen_keys.update(candidate_keys)
    return seeded


def _merge_risks(*risk_lists: list[RiskWatchItem]) -> list[RiskWatchItem]:
    """Concatenate risk lists, keeping the first item seen per category.

    Same-day check-in risks are passed first, so they win over a derived signal
    in the same category (the fresh self-report beats the logged-history echo).
    """
    seen: set[str] = set()
    merged: list[RiskWatchItem] = []
    for risk_list in risk_lists:
        for risk in risk_list:
            if risk.category in seen:
                continue
            seen.add(risk.category)
            merged.append(risk)
    return merged


def _fight_card_fields_from_intake(intake: Mapping[str, Any]) -> dict[str, Any]:
    """Event name and target fight weight from the plan's own intake, for Home's
    fight countdown card. Display-only; absent values are simply omitted."""
    fields: dict[str, Any] = {}
    event_name = " ".join(str(intake.get("event_name") or "").split())
    if event_name:
        fields["event_name"] = event_name
    athlete = intake.get("athlete")
    raw_target = athlete.get("target_weight_kg") if isinstance(athlete, Mapping) else None
    try:
        target = float(raw_target) if raw_target is not None and not isinstance(raw_target, bool) else None
    except (TypeError, ValueError):
        target = None
    if target is not None and math.isfinite(target) and target > 0:
        fields["target_weight_kg"] = target
    return fields


def _plan_with_resolved_phase(
    plan_row: Mapping[str, Any],
    week: Any,
    *,
    structured_phase: str = "",
) -> dict[str, Any]:
    """Use the current schedule week as the command-view phase authority."""
    plan = dict(plan_row)
    resolved_phase = structured_phase or str(getattr(week, "phase", "") or "").strip()
    if resolved_phase:
        plan["phase"] = resolved_phase
    return plan


# Rows on or after today are the current day's log plus anything logged ahead,
# so this bound is not reached in practice. One extra row is requested so that
# exactly this many rows is still known to be the whole range.
_UPCOMING_COMPLETION_READ_LIMIT = 200


class _UpcomingCompletions:
    """Completion rows from ``from_day`` onward, read at most once per Today build.

    Today asks whether a session is logged for each of today's sessions and for
    every future day it passes while finding the next session. Completions are
    unique per (athlete, session, training day), so one ranged read answers all
    of those questions. A day before ``from_day``, or a ranged read that did not
    return a list, falls back to the exact per-session read.

    A read that hit the limit is cut somewhere inside its last training day.
    Rows arrive in training-day order, so every earlier day is complete and is
    still answered from the read; only the last day read and later days fall
    back to exact reads. Rows out of order drop the whole read to exact reads.
    """

    def __init__(self, store: AppStore, *, athlete_id: str, from_day: str) -> None:
        self._store = store
        self._athlete_id = athlete_id
        self._from_day = from_day
        self._rows: dict[tuple[str, str], Mapping[str, Any]] | None = None
        # Exclusive upper bound of the days the read covers; None = all of them.
        self._covered_before: str | None = None
        self._loaded = False

    def _upcoming_rows(self) -> dict[tuple[str, str], Mapping[str, Any]] | None:
        if self._loaded:
            return self._rows
        self._loaded = True
        rows = self._store.list_session_completions_from_day(
            self._athlete_id, self._from_day, limit=_UPCOMING_COMPLETION_READ_LIMIT + 1
        )
        if not isinstance(rows, list):
            return None
        rows = [row for row in rows if isinstance(row, Mapping)]
        days = [str(row.get("training_day") or "")[:10] for row in rows]
        if len(rows) > _UPCOMING_COMPLETION_READ_LIMIT:
            if days != sorted(days):
                return None
            self._covered_before = days[-1]
        self._rows = {
            (str(row.get("session_id") or ""), day): row for row, day in zip(rows, days)
        }
        return self._rows

    def get(self, session_id: str, training_day: str) -> dict[str, Any] | None:
        if len(training_day) == 10 and training_day >= self._from_day:
            rows = self._upcoming_rows()
            if rows is not None and (
                self._covered_before is None or training_day < self._covered_before
            ):
                row = rows.get((session_id, training_day))
                return dict(row) if row is not None else None
        return self._store.get_session_completion(self._athlete_id, session_id, training_day)

    def rehab_completion(self, plan_id: str, training_day: str) -> dict[str, Any] | None:
        rows = self._upcoming_rows()
        if rows is None or (self._covered_before is not None and training_day >= self._covered_before):
            candidates = self._store.list_rehab_schedule_completions(self._athlete_id, from_day=training_day)
        else:
            candidates = rows.values()
        accepted = [row for row in candidates if row.get("training_day") == training_day
                    and row.get("plan_id") == plan_id and row.get("status") in {"started", *TERMINAL_COMPLETION_STATUSES}
                    and (row.get("prescription_snapshot") or {}).get("session", {}).get("session_type") == "rehab"]
        # Resume accepted work first; terminal records still describe the logged
        # day, but cannot claim a newly offered allocation with a different id.
        return dict(max(accepted, key=lambda row: (row.get("status") == "started",
            str(row.get("updated_at") or row.get("created_at") or ""), str(row.get("session_id"))))) if accepted else None


def _head_impact_reported(store: AppStore, athlete_id: str, training_day: str) -> bool:
    """Whether a sparring report for this athlete-local day says rocked/dropped.

    The report itself is the record; this only keeps Today's lead message
    consistent with it. A failed read leaves Today as it was before the report
    was readable here: it never adds clearance.
    """
    try:
        rows = store.list_sparring_logs(athlete_id, limit=50, from_day=training_day)
    except Exception as exc:  # noqa: BLE001 - logged; Today must still render
        logger.warning("[today] sparring read failed athlete_id=%s error_type=%s", athlete_id, type(exc).__name__)
        return False
    return any(bool(row.get("rocked")) and str(row.get("training_day") or "")[:10] == training_day for row in rows)


def check_in_possible(store: AppStore, athlete_id: str) -> bool:
    """A check-in is health data: without consent it cannot be given, so it is never required."""
    try:
        from api.compliance import evaluate_profile_compliance
        profile = store.get_profile_compliance(athlete_id)
        return profile is None or evaluate_profile_compliance(profile).health_consent_granted
    except Exception:
        logger.exception("[today] check_in_consent_read_failed")
        return True


def apply_readiness_hold(live: dict, decision_tier: str, *, check_in_possible: bool = True) -> bool:
    """Fold today's verdict into the reconciled session. True when it added the hold."""
    holds = (decision_tier == "stop" or (decision_tier == "not_checked_in" and check_in_possible)
             or (decision_tier == "pull_back" and not live.get("rehab_only")))
    if live["safety_hold"] or not holds:
        return False
    live["safety_hold"] = True
    live["safety_hold_reason"] = ("Complete today's check-in before starting." if decision_tier == "not_checked_in"
        else "Today's pull-back guidance holds your planned training." if decision_tier == "pull_back"
        else "Follow today's stop guidance before training.")
    return True


def _apply_rehab_session_hold(injuries, live):
    """A routine held by the final session verdict is not accessible due work."""
    if not live or not live.get("safety_hold"):
        return
    for injury in injuries:
        schedule = (injury.get("rehab_decision") or {}).get("schedule")
        if schedule and schedule["state"] == "due":
            schedule.update(state="held", reason=live.get("safety_hold_reason")
                            or "Rehab is on hold for the current injury guidance.")


def session_hold(decision_tier: str, live: dict | None, *, session_id: str | None = None,
                 readiness_held: bool = False, check_in_possible: bool = True) -> dict[str, Any]:
    """The single rule for whether a session today is blocked, and why.

    ``session_id`` names the session being acted on; ``None`` means the session
    Today shows. On a pull-back day only the offered reviewed rehab may run.
    """
    targeted = bool(live and (session_id is None or live["session"].get("session_id") == session_id))
    offered_rehab = targeted and live["session"].get("session_type") == "rehab" and not live.get("safety_hold")
    if (targeted and live.get("safety_hold") and not readiness_held
            and live.get("training_hold_source") != "readiness"):
        return {"held": True, "reason": live.get("safety_hold_reason") or "This session is on hold for the current injury guidance.",
                "source": "injury"}
    if decision_tier == "stop":
        return {"held": True, "reason": "Training is stopped by today's current safety guidance.", "source": "readiness"}
    if decision_tier == "not_checked_in" and check_in_possible:
        return {"held": True, "reason": "Complete today's check-in before starting.", "source": "check_in"}
    if decision_tier == "pull_back" and not offered_rehab:
        return {"held": True, "reason": "Today's pull-back guidance holds your planned training.", "source": "readiness"}
    return {"held": False, "reason": None, "source": None}


def build_today_command_view(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
    now: datetime | None = None,
    active_plan: ActivePlanResolution | None = None,
) -> CommandView:
    """Assemble the normalized command view from persisted state.

    Degrades gracefully: no active plan → empty view with the Intake CTA; a
    missing/unparseable structured plan → empty ``next_session`` (no crash).

    ``active_plan`` is a resolution the caller already made for the same
    training day (the Today boundary shares its own with the intake sync).
    """
    with structured_projection_memo():
        return _build_today_command_view(
            store,
            athlete_id=athlete_id,
            athlete_timezone=athlete_timezone,
            now=now,
            active_plan=active_plan,
        )


def _with_added_rehab_note(reason: str | None, *, before_cooldown: bool) -> str:
    """Say on the day's guidance that reviewed rehab was added to the session.

    The reason is title / detail / action lines (one line is the detail alone);
    the note joins the detail so the day's verdict and action are unchanged.
    """
    note = ("Your reviewed rehab is added to today's session, before the cool-down." if before_cooldown
            else "Your reviewed rehab is added to today's session.")
    lines = [line for line in (reason or "").split("\n") if line.strip()]
    if not lines:
        return note
    at = 1 if len(lines) >= 2 else 0
    lines[at] = f"{lines[at].rstrip()} {note}"
    return "\n".join(lines)


def _build_today_command_view(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
    now: datetime | None,
    active_plan: ActivePlanResolution | None = None,
) -> CommandView:
    training_day = resolve_training_day(athlete_timezone, now=now)
    assessment_as_of = now or datetime.now(timezone.utc)
    if assessment_as_of.tzinfo is None:
        assessment_as_of = assessment_as_of.replace(tzinfo=timezone.utc)
    if active_plan is None:
        # The resolver reads the pointed-to plan through get_plan_for_athlete, so
        # this is already the full owner-scoped row; no second read is needed.
        active_plan = resolve_active_plan(
            store,
            athlete_id,
            current_training_day=training_day,
        )
    plan_row = active_plan.plan

    if not plan_row:
        plan_free_checkin = store.get_today_checkin(athlete_id, None, training_day)
        injuries = _with_rehab_stage(_with_safe_session_context(_with_surface_class(_open_injury_flags(store, athlete_id))),
                                    store=store, athlete_id=athlete_id, current_checkin=plan_free_checkin)
        # Same clean, athlete-facing names as the planned path: the stored
        # description carries internal markers and is never displayed as-is.
        for injury in injuries:
            injury["label"] = build_injury_label(injury.get("body_area"), injury.get("description"))
        # The plan-free check-in is read like a planned one: a failed read
        # raises (Today unavailable) rather than looking like "not checked in".
        recommendation = None
        if plan_free_checkin or active_medical_hold_reasons(injuries):
            from dataclasses import replace

            # As on the planned path, the saved recommendation is a check-in-time
            # snapshot: every read re-evaluates the stored answers against the
            # current injuries, workload, sparring and safety reads, with no plan
            # context. Read-only: the check-in row is never rewritten here. A
            # current medical hold or worsening injury leads through the same
            # engine, and failed reads are floored by the same fail-safe.
            decision, _ = _readiness_decision_with_failsafe(
                store,
                checkin=replace(_stored_readiness_checkin_from(plan_free_checkin or {}), phase=""),
                athlete_id=athlete_id,
                plan_row=None,
                training_day=training_day,
                phase="",
                open_injuries=injuries,
            )
            decision = _plan_free_wording(decision)
            recommendation = {"decision": decision.decision, "reason": decision.message,
                              "triggers": list(decision.triggers), "training_day": training_day}
        injuries = _with_injury_policy(injuries, store=store, athlete_id=athlete_id,
            current_checkin=plan_free_checkin, training_day=training_day, as_of=assessment_as_of,
            readiness_decision=(recommendation or {}).get("decision") or "not_checked_in",
            athlete_timezone=athlete_timezone,
            equipment=_rehab_equipment_from_intake(store.get_latest_intake(athlete_id) or {}))
        occurrences = store.list_rehab_schedule_completions(athlete_id, from_day=training_day)
        started = next((c for c in occurrences if c.get("plan_id") is None
            and c.get("training_day") == training_day and c.get("status") == "started"
            and c.get("prescription_snapshot")), None)
        live = reconcile_session_prescription(None, decisions=[i["rehab_decision"] for i in injuries],
            plan_id=None, training_day=training_day, injuries=injuries,
            frozen=(started or {}).get("prescription_snapshot"))
        if live and not started:
            from fightcamp.rehab_clinical import content_hash
            live["session"]["completion_status"] = "not_started"
            live["injury_context"] = [{"id": row["id"], "episode_id": row.get("episode_id"), "updated_at": row.get("updated_at")}
                for row in sorted(injuries, key=lambda row: str(row["id"]))]
            live["evidence_context"] = store.get_rehab_schedule_revision(athlete_id)
            live["readiness_context"] = {"id": plan_free_checkin["id"], "updated_at": plan_free_checkin.get("updated_at")} if plan_free_checkin else None
            live["revision"] = content_hash({k: v for k, v in live.items() if k != "revision"})
        view = build_command_view(current_training_day=training_day, plan=None, open_injuries=injuries,
                                  head_impact_today=_head_impact_reported(store, athlete_id, training_day),
                                  recommendation=recommendation,
                                  risks=_risks_from_checkin(plan_free_checkin))
        if live:
            apply_readiness_hold(live, view.today.decision_tier, check_in_possible=check_in_possible(store, athlete_id))
            live["session"]["completion_status"] = completion_status_of(started)
        _apply_rehab_session_hold(injuries, live)
        view.live_prescription = live
        view.effective_clinician_clearance = effective_clinician_clearance(injuries)
        view.delayed_rehab_prompts = delayed_rehab_prompts(store, athlete_id, training_day, athlete_timezone)
        return view

    plan_id = str(plan_row.get("id") or "")

    # Fetch the check-in once and reuse it for both the recommendation and the
    # risk watch (avoids a redundant DB roundtrip).
    today_checkin = store.get_today_checkin(athlete_id, plan_id, training_day)
    recommendation = _recommendation_mapping(today_checkin)
    warnings = _same_day_checkin_warnings(
        store,
        athlete_id=athlete_id,
        plan_id=plan_id,
        training_day=training_day,
    )

    # Derive today's/next session from the persisted plan's weekly schedule.
    today_entry = next_entry = None
    week = None
    week_index = 0
    training_date = parse_iso_date(training_day)
    if training_date is not None:
        try:
            week_index, week = resolve_current_week(plan_row, today=training_date)
            today_entry, next_entry = resolve_today_and_next(week, today=training_date)
            if _plan_has_not_started(plan_row, training_day):
                today_entry = None
        except Exception:
            # Malformed plan data must never crash Overview.
            today_entry = next_entry = None
            week = None

    completions = _UpcomingCompletions(store, athlete_id=athlete_id, from_day=training_day)

    def _session_is_complete(session_id: str, calendar_date: str) -> bool:
        return (
            completion_status_of(completions.get(session_id, calendar_date))
            in TERMINAL_COMPLETION_STATUSES
        )

    # The plan card owns whether today is a rest day. The intake weekly template
    # calls every configured training weekday a session ("Fri training"), so
    # without this it resurrects a session on a day the athlete's own card reads
    # "Rest or active recovery" — and Today then offers to start it.
    structured_today = _structured_today(plan_row, training_day)
    # A training day is one session to the athlete, however many sessions the
    # card splits it into: one log ordinarily finishes the day, including legacy
    # logs before fan-out. An accepted partial support projection is the explicit
    # exception: its held source owners remain outstanding.
    today_candidates = (
        []
        if structured_today.is_rest_day
        else (structured_today.entries or ([today_entry] if today_entry is not None else []))
    )
    source_allocation_ceiling = 1 if any("sparring" in str(_entry_mapping_for_readiness(entry).get("session_type") or "").lower()
                                        for entry in today_candidates) else 2
    # A partial accepted support session explicitly holds other source owners.
    # Its terminal log cannot stand in for completion of those held sessions.
    partial_support_ownership = any(
        change.get("action") == "held" and change.get("session_id")
        for entry in today_candidates
        for change in ((completions.get(_session_id_for_entry(entry), training_day) or {}).get("prescription_snapshot") or {}).get("changes", [])
    )
    if partial_support_ownership:
        outstanding_candidates = [entry for entry in today_candidates if not _session_entry_is_complete(
            entry, is_complete=_session_is_complete, calendar_date=training_day)]
        if outstanding_candidates:
            today_candidates = outstanding_candidates
        today_candidates.sort(key=lambda entry: completion_status_of(
            completions.get(_session_id_for_entry(entry), training_day)) != "started")
    today_logged_entry = next(
        (
            entry
            for entry in today_candidates
            if _session_entry_is_complete(
                entry, is_complete=_session_is_complete, calendar_date=training_day
            )
        ),
        None,
    )
    today_session_entry = (
        today_candidates[0] if today_candidates and today_logged_entry is None else None
    )
    # Custom days execute as one combined workout through the existing
    # completion fan-out. Name every required session so the summary describes
    # the same unit as the Enhanced Cards and the completion action.
    from api.plan_mappers import _decode_structured_text
    is_custom_plan = bool((_decode_structured_text(plan_row.get("planning_brief")) or {}).get("custom_plan"))
    if is_custom_plan and today_session_entry is not None and len(today_candidates) > 1:
        today_session_entry = {
            **_entry_mapping_for_readiness(today_session_entry),
            "title": " / ".join(
                str(_entry_mapping_for_readiness(entry).get("title") or "Session")
                for entry in today_candidates
            ),
        }
    has_today_session = has_scheduled_day_content(today_session_entry)
    today_session_id = _session_id_for_entry(today_session_entry) if has_today_session else None
    rehab_occurrence = completions.rehab_completion(plan_id, training_day)
    today_completion = (
        completions.get(today_session_id, training_day)
        if today_session_id
        else rehab_occurrence or completions.get(f"rehab-{training_day}", training_day)
    )
    if today_completion and today_completion.get("plan_id") != plan_id:
        today_completion = None
    if not (today_completion or {}).get("prescription_snapshot"):
        rehab_completion = rehab_occurrence or completions.get(f"rehab-{training_day}", training_day)
        # Accepted work in progress owns the current session. A terminal rehab
        # occurrence owns only its allocations, never outstanding camp work.
        if (((rehab_completion or {}).get("prescription_snapshot") or {}).get("plan_id") == plan_id
                and (not has_today_session or completion_status_of(rehab_completion) == "started")):
            today_completion = rehab_completion
            today_session_entry = today_completion["prescription_snapshot"]["session"]
            has_today_session = True
            today_session_id = _session_id_for_entry(today_session_entry)
    if not has_today_session and ((today_completion or {}).get("prescription_snapshot") or {}).get("plan_id") == plan_id:
        today_session_entry = today_completion["prescription_snapshot"]["session"]
        has_today_session = True
        today_session_id = _session_id_for_entry(today_session_entry)
    today_is_complete = completion_status_of(today_completion) in TERMINAL_COMPLETION_STATUSES
    target_entry = today_session_entry if has_today_session and not today_is_complete else next_entry
    if target_entry is None and week is not None:
        # No training left in the current week — look ahead so the "Next session"
        # card surfaces the upcoming session instead of "No session found".
        try:
            target_entry = _scan_forward_for_next_training(
                plan_row,
                week=week,
                week_index=week_index,
                training_date=training_date,
            )
        except Exception:
            target_entry = None
    if target_entry is not today_session_entry or today_session_entry is None:
        target_entry = _prefer_earlier_structured_next_entry(
            target_entry,
            _structured_next_session_entry(
                plan_row, training_day, is_complete=_session_is_complete
            ),
        )
    session_relation = (
        "today"
        if target_entry is not None and target_entry is today_session_entry
        else ("next" if target_entry is not None else None)
    )
    session_id = _session_id_for_entry(target_entry)
    # completion_status always describes TODAY (notifications and XP read it as
    # "has this athlete finished training today"), never the card on screen — a
    # fall-forward to tomorrow still reports today's session as done. With work
    # still outstanding today it reports that session, so a day whose first of
    # two sessions is logged does not read as finished.
    accepted_rehab = ((today_completion or {}).get("prescription_snapshot") or {}).get("session", {}).get("session_type") == "rehab"
    today_completion_entry = today_session_entry if accepted_rehab else today_logged_entry or today_session_entry
    today_completion_id = (
        _session_id_for_entry(today_completion_entry)
        if has_scheduled_day_content(today_completion_entry)
        else None
    )
    today_completion = (
        completions.get(today_completion_id, training_day)
        if today_completion_id
        else today_completion
    )
    # The logged day owner is resolved above, after the next-session scan.
    # Recompute before reading its frozen prescription: a completed occurrence
    # must not resume and replace the next target with the raw weekly fallback.
    today_is_complete = completion_status_of(today_completion) in TERMINAL_COMPLETION_STATUSES

    structured_phase = _structured_phase_for_day(plan_row, training_day)
    resolved_plan = _plan_with_resolved_phase(
        plan_row,
        week,
        structured_phase=structured_phase,
    )

    open_injuries = _with_rehab_stage(
        _with_safe_session_context(
            _with_surface_class(
                _ensure_intake_injury_flags(
                    store,
                    athlete_id=athlete_id,
                    plan_row=plan_row,
                    open_flags=_open_injury_flags(store, athlete_id),
                )
            )
        ),
        store=store,
        athlete_id=athlete_id,
        current_checkin=today_checkin,
    )
    # Attach a clean, athlete-facing label derived from the injury synonym logic
    # so the reminder text and the check-in card render the same normalized name
    # ("Left wrist tightness") instead of raw stored words.
    for injury in open_injuries:
        injury["label"] = build_injury_label(injury.get("body_area"), injury.get("description"))

    observations_by_injury = {str(row["id"]): episode_observations(store, athlete_id, row) for row in open_injuries}
    open_injuries = [apply_episode_observations(row, observations_by_injury[str(row["id"])], as_of=assessment_as_of) for row in open_injuries]
    support_projection = None
    support_owner_ids = set()
    ceiling = effective_clinician_clearance(open_injuries)
    if ceiling and ceiling["level"] != "train_contact" and completion_status_of(today_completion) != "started":
        from api.contracts.readiness_message import _is_non_physical_mapping, _session_has_contact
        injured_regions = {row.get("canonical_location") for row in open_injuries}
        support_entries = []
        other_entries = []
        for candidate in today_candidates:
            app_entry = _entry_mapping_for_readiness(candidate)
            # Coach-led contact is day context, not the watch/mobility's work.
            app_entry.pop("coach_led_contact", None)
            blocks = app_entry.get("blocks") or []
            safe_demands = bool(blocks) and all(_is_non_physical_mapping(block) or (
                all(isinstance(region, str) and region.strip() for region in injured_regions)
                and isinstance(block.get("mechanical_load_regions"), list)
                and not set(block["mechanical_load_regions"]) & injured_regions
                and block.get("contact_level") == "none") for block in blocks)
            if is_support_session(app_entry) and safe_demands and not _session_has_contact(app_entry):
                support_entries.append(app_entry)
            else:
                other_entries.append(app_entry)
        # Do not discard otherwise-permitted ordinary non-contact camp under
        # train/no-contact. This exception offers independently safe support.
        if support_entries and not any(entry.get("prescription_source") == "athlete" for entry in today_candidates) and (ceiling["level"] == "rehab_only" or all(_session_has_contact(row) for row in other_entries)):
            support_projection = {**support_entries[0], "blocks": [block for row in support_entries for block in row["blocks"]]}
            support_owner_ids = {str(row["session_id"]) for row in support_entries}
    if completion_status_of(today_completion) == "started":
        accepted_session = (today_completion.get("prescription_snapshot") or {}).get("session")
        if accepted_session and is_support_session(accepted_session):
            # Readiness follows the already accepted support, not its held
            # coach-led sibling. Reconciliation still freezes and gates it.
            support_projection = accepted_session
    if today_checkin or active_medical_hold_reasons(open_injuries):
        # The saved recommendation is a check-in-time snapshot, not a live
        # workload decision. Every authoritative read evaluates the current
        # check-in against BOTH history sources, even when the history is now
        # empty (edits/deletions) or its read failed. The canonical boundary
        # shares successful reads within this build and floors failed reads.
        # This cheap deterministic evaluation writes neither the check-in nor
        # accepted/frozen prescriptions; no invalidation or refresh write can
        # race a successful training write or overwrite a newer evaluation.
        decision, _ = _readiness_decision_with_failsafe(
            store, checkin=_stored_readiness_checkin_from(today_checkin or {}), athlete_id=athlete_id,
            plan_row=plan_row, training_day=training_day,
            phase=str((today_checkin or {}).get("phase") or resolved_plan.get("phase") or ""), open_injuries=open_injuries,
            session_override=support_projection)
        recommendation = {"decision": decision.decision, "reason": decision.message,
                          "triggers": list(decision.triggers), "training_day": training_day}

    # A severe active injury is the highest-priority constraint for the day: it
    # supersedes the daily readiness recommendation with a hard pull-back so the
    # live command view is authoritative (the stored daily check-in stays in
    # history). This catches a severe injury carried in from intake / a prior day
    # that never went through the injury check-in refresh. It only ESCALATES —
    # when the recommendation is already a pull-back (e.g. the readiness engine
    # produced "Rehab only today." on injury report) we keep that richer copy.
    # A low-cost support / filler session (mental cue card, breathing/mobility reset)
    # is the safe work an injury STOP itself prescribes, so the injury hold does not
    # apply to it. Current medical red flags still block all work. Exempt today's
    # scheduled filler from the severe-injury override, the decision tier, and the
    # completion guard.
    # has_today_session is already limited to a session that is still
    # outstanding, so a completed one can no longer reach this.
    today_is_support_filler = has_today_session and is_support_session(
        support_projection or _entry_mapping_for_readiness(today_session_entry)
    )
    severe_injury = _active_severe_injury(open_injuries)
    severe_non_surface_injury = _active_severe_non_surface_injury(open_injuries)
    current_decision = str((recommendation or {}).get("decision") or "")
    surface_review_recommendation = next(
        (
            review
            for injury in open_injuries
            if (review := _surface_medical_review_recommendation(injury, training_day))
            is not None
        ),
        None,
    )
    if (
        severe_non_surface_injury is not None
        and current_decision != "pull_back"
        and not today_is_support_filler
    ):
        recommendation = _severe_injury_recommendation(
            severe_non_surface_injury, training_day
        )
        if surface_review_recommendation is not None:
            recommendation["triggers"] = list(
                dict.fromkeys(
                    [
                        *recommendation["triggers"],
                        "safety_check:surface_injury:medical_review",
                    ]
                )
            )
        current_decision = "pull_back"
    elif surface_review_recommendation is not None and (
        current_decision != "pull_back"
        # A pull-back is normally left alone — it is already the stronger call and
        # usually carries richer copy. A GENERIC injury pull-back over a wound is
        # the exception: it says "rehab only" and nothing about keeping the wound
        # clean, covered and out of contact. With no severe non-surface injury to
        # explain it, the wound-specific guidance is what the athlete needs, so it
        # replaces the generic copy rather than losing to it.
        or (
            severe_non_surface_injury is None
            and _is_generic_injury_pull_back(recommendation)
        )
    ):
        recommendation = surface_review_recommendation
        current_decision = "pull_back"
    if severe_injury is not None and current_decision != "pull_back" and not today_is_support_filler:
        recommendation = _severe_injury_recommendation(severe_injury, training_day)

    intake, _intake_ok = _checked_intake_payload_for_readiness(store, plan_row)
    equipment = _rehab_equipment_from_intake(intake)
    resolved_plan.update(_fight_card_fields_from_intake(intake))
    # Today logs the entire training day. Reconcile every scheduled block,
    # preserving the primary completion identity and checking sibling demands.
    training_session = _entry_mapping_for_readiness(today_session_entry) if today_session_entry else None
    if training_session and len(today_candidates) > 1:
        training_session["blocks"] = [
            dict(block) for entry in today_candidates
            for block in _entry_mapping_for_readiness(entry).get("blocks", [])
        ]
        if any("sparring" in str(_entry_mapping_for_readiness(entry).get("session_type") or "").lower()
               for entry in today_candidates):
            training_session["session_type"] = "sparring"
        from api.contracts.readiness_message import _session_has_contact
        contact_entry = next((entry for entry in today_candidates
                              if _session_has_contact({**_entry_mapping_for_readiness(entry), "blocks": []})
                              or _session_has_contact(_entry_mapping_for_readiness(entry))), None)
        if contact_entry:
            training_session["coach_led_contact"] = contact_entry.get("coach_led_contact") or contact_entry.get("title") or "Contact work"
    open_injuries = _with_injury_policy(open_injuries, store=store, athlete_id=athlete_id,
                                       phase=str(resolved_plan.get("phase") or ""), current_checkin=today_checkin, equipment=equipment,
                                       readiness_decision=(recommendation or {}).get("decision") or "not_checked_in", training_day=training_day,
                                       training_session=training_session, observations_by_injury=observations_by_injury, as_of=assessment_as_of, athlete_timezone=athlete_timezone)
    decisions = [injury["rehab_decision"] for injury in open_injuries]
    frozen = None if today_is_complete else (today_completion or {}).get("prescription_snapshot")
    live = reconcile_session_prescription(
        (support_projection or training_session) if not today_is_complete else None,
        decisions=decisions, plan_id=plan_id, training_day=training_day, frozen=frozen,
        injuries=open_injuries,
        allocation_ceiling=source_allocation_ceiling if support_projection or partial_support_ownership else None,
    )
    if live and not frozen and (today_completion or {}).get("status") == "started":
        # Activation must not rewrite work that started before snapshots existed.
        live["session"] = _entry_mapping_for_readiness(today_session_entry)
        live["safety_hold"] = True
        live["safety_hold_reason"] = "This session started before the current injury guidance. Stop and review your next session."
    if live and frozen:
        from api.services.clinical_review_freeze import frozen_review_hold
        if frozen_review_hold(store, athlete_id, frozen,
                work_state="started" if (today_completion or {}).get("status") == "started" else "unstarted",
                as_of=assessment_as_of):
            live["safety_hold"] = True
            live["safety_hold_reason"] = "The clinical review for this saved work changed. Stop and review your next session."
    if live:
        for change in live.get("changes", []):
            if change.get("action") == "deferred":
                for injury in open_injuries:
                    if injury["id"] == change.get("injury_id"):
                        injury["rehab_decision"]["summary"] = "Your rehab is matched, but this session's rehab allocation is full. Keep to your current restrictions."
                        injury["rehab_decision"]["scheduled_today"] = False
                        injury["rehab_decision"]["schedule"] = {"state": "deferred", "reason": "Today's rehab allocation is full.", "next_due_day": (parse_iso_date(training_day) + timedelta(days=1)).isoformat()}
        if not frozen:
            from fightcamp.rehab_clinical import content_hash
            from fightcamp.rehab_protocols import rehab_drill_by_id
            from copy import deepcopy
            previous_limits = [(row.get("prescription_snapshot") or {}).get("allocation_limit")
                               for row in (today_completion, rehab_occurrence) if row]
            live["allocation_limit"] = min([live["allocation_limit"]] + [limit for limit in previous_limits if limit in (1, 2)])
            if support_projection:
                # Keep the original day ceiling and record held source owners
                # in the accepted snapshot so support completion cannot log them.
                live["changes"].extend({"session_id": str(row["session_id"]), "action": "held",
                    "reason": "clinician_clearance_ceiling"} for row in today_candidates
                    if str(row["session_id"]) not in support_owner_ids)
            live["injury_context"] = [{"id": row["id"], "episode_id": row.get("episode_id"), "updated_at": row.get("updated_at")}
                                      for row in sorted(open_injuries, key=lambda row: str(row["id"]))]
            live["evidence_context"] = store.get_rehab_schedule_revision(athlete_id)
            for block in live["session"].get("blocks", []):
                if block.get("block_type") == "rehab" and not block.get("drill_snapshot"):
                    drill = rehab_drill_by_id(str(block.get("rehab_drill_id") or ""))
                    if drill:
                        block["drill_snapshot"] = deepcopy(drill)
            live["revision"] = content_hash({k: v for k, v in live.items() if k != "revision"})
        today_session_entry = live["session"]
        target_entry = today_session_entry
        session_id = _session_id_for_entry(target_entry)
        session_relation = "today"
        if not today_completion or today_completion.get("session_id") != session_id:
            today_completion = completions.get(session_id, training_day)
        if completion_status_of(today_completion) in TERMINAL_COMPLETION_STATUSES:
            live = None
            target_entry, session_relation = next_entry, "next" if next_entry else None
            session_id = _session_id_for_entry(target_entry)

    target_entry = sanitize_surface_guidance(target_entry)
    today_completion = sanitize_surface_guidance(today_completion)
    view = build_command_view(
        current_training_day=training_day,
        plan=resolved_plan,
        recommendation=recommendation,
        head_impact_today=_head_impact_reported(store, athlete_id, training_day),
        injury_hold_exempt=today_is_support_filler,
        completion=today_completion,
        next_session=sanitize_surface_guidance(_next_session_payload(target_entry, session_id, relation=session_relation)),
        session_scope=session_relation or "none",
        warnings=warnings,
        # Risk precedence per category, freshest signal first: today's check-in,
        # then tracked open injuries, then the derived post-session pain history.
        # Together they keep the badge live whether the athlete reported today,
        # is carrying an open injury, or just logged painful sessions.
        risks=_merge_risks(
            _risks_from_checkin(today_checkin),
            open_injury_flag_risks(open_injuries),
            _history_injury_risks(
                store,
                athlete_id=athlete_id,
                training_day=training_day,
                current_phase=str(resolved_plan.get("phase") or ""),
            ),
        ),
        open_injuries=open_injuries,
    )
    # Only a missing check-in needs to know whether one can be given (consent).
    can_check_in = view.today.decision_tier != "not_checked_in" or check_in_possible(store, athlete_id)
    if live:
        policy_changed_guidance = False
        readiness_held = apply_readiness_hold(live, view.today.decision_tier, check_in_possible=can_check_in)
        # Describe the actual prescription without weakening stronger readiness.
        if live["safety_hold"] and view.today.decision_tier in {"green", "modify"}:
            policy_changed_guidance = True
            view._injury_hold_stop = True
            view.today.recommendation_state = "pull_back"
            view.today.decision_tier = "stop"
            view.today.recommendation_reason = "Session blocked\n" + live.get("safety_hold_reason", "This session is on hold for the current injury guidance.") + "\nDo not start this session. Review your injury guidance."
        # Rehab added beside finished or absent training changes nothing: only a
        # held planned session (it carries a hold reason) adjusts the guidance.
        elif not live["safety_hold"] and view.today.decision_tier in {"green", "modify"} and (
                (live.get("rehab_only") and live.get("training_hold_reason")) or live.get("changes")):
            policy_changed_guidance = True
            view.today.recommendation_state = "modify"
            view.today.decision_tier = "modify"
            if live.get("rehab_only"):
                view.today.recommendation_reason = "Reviewed rehab today\n" + live.get("training_hold_reason", "Your planned training is held by the current injury restrictions.") + "\nFollow only the reviewed rehab shown today."
            else:
                view.today.recommendation_reason = "Session adjusted\nCurrent injury restrictions changed today's work.\nFollow the adjusted session shown and its current injury restrictions."
        session_blocks = live["session"].get("blocks", [])
        rehab_at = [i for i, block in enumerate(session_blocks) if block.get("block_type") == "rehab"
                    and block.get("policy_id") and not block.get("_policy_held")]
        if (rehab_at and not live["safety_hold"] and not live.get("rehab_only")
                and view.today.decision_tier in {"green", "modify"}):
            view.today.recommendation_reason = _with_added_rehab_note(view.today.recommendation_reason,
                before_cooldown=any(block.get("block_type") == "cooldown_recovery" for block in session_blocks[rehab_at[-1]:]))
        if policy_changed_guidance:
            view.today.recommendation_trigger_labels = list(dict.fromkeys([*view.today.recommendation_trigger_labels, "Current injury restrictions"]))
            view.today.recommendation_sources = list(dict.fromkeys([*view.today.recommendation_sources, "your tracked injuries"]))
        if not frozen:
            from fightcamp.rehab_clinical import content_hash
            live["readiness_context"] = {"id": today_checkin["id"], "updated_at": today_checkin.get("updated_at")} if today_checkin else None
            live["revision"] = content_hash({k: v for k, v in live.items() if k != "revision"})
    view.live_prescription = live
    _apply_rehab_session_hold(open_injuries, live)
    if view.today.session_scope == "today":
        view.session_hold = session_hold(view.today.decision_tier, live, check_in_possible=can_check_in,
                                         readiness_held=bool(live and readiness_held))
    if live:
        # Presentation only: never part of the frozen prescription/revision.
        from .exercise_media import load_media_index, resolve_session_exercise_media, with_athlete_change_media
        from api.plan_mappers import _decode_structured_text
        custom = _decode_structured_text(plan_row.get("planning_brief"))
        if custom and custom.get("custom_plan"):
            view.exercise_media = with_athlete_change_media(
                custom["custom_plan"].get("exercise_media"), (live.get("session") or {}).get("blocks") or [], store)
        else:
            view.exercise_media = resolve_session_exercise_media(live.get("session") or {}, load_media_index(store))
    view.effective_clinician_clearance = effective_clinician_clearance(open_injuries)
    view.delayed_rehab_prompts = delayed_rehab_prompts(store, athlete_id, training_day, athlete_timezone)
    return view


def resolve_today_landing(
    store: AppStore,
    *,
    athlete_id: str,
    athlete_timezone: str | None,
    has_interacted: bool,
    now: datetime | None = None,
) -> LandingDecision:
    """Resolve the landing decision from persisted state (see ``resolve_landing``)."""
    training_day = resolve_training_day(athlete_timezone, now=now)
    plan_row = resolve_active_plan(
        store,
        athlete_id,
        current_training_day=training_day,
    ).plan
    has_active_plan = bool(plan_row)

    session_state = "none"
    checked_in_today = False
    if plan_row:
        plan_id = str(plan_row.get("id") or "")
        checkin = store.get_today_checkin(athlete_id, plan_id, training_day)
        checked_in_today = checkin is not None

        today_entry = next_entry = None
        training_date = parse_iso_date(training_day)
        if training_date is not None:
            try:
                _week_index, week = resolve_current_week(plan_row, today=training_date)
                today_entry, next_entry = resolve_today_and_next(week, today=training_date)
                if _plan_has_not_started(plan_row, training_day):
                    today_entry = None
            except Exception:
                today_entry = next_entry = None
        # Same resolution Today uses, for the same reason: a rest day has no
        # session, so a completion record left on one (written before this was
        # enforced, or against a since-changed plan) must not land the athlete on
        # "Resume". Otherwise the card the athlete is looking at and the landing
        # decision would answer "is there a session today?" differently.
        structured_today = _structured_today(plan_row, training_day)
        session_id = (
            None
            if structured_today.is_rest_day
            else _session_id_for_entry(structured_today.entry or today_entry or next_entry)
        )
        if session_id:
            completion = store.get_session_completion(athlete_id, session_id, training_day)
            session_state = completion_landing_state(completion_status_of(completion))

    return resolve_landing(
        has_active_plan=has_active_plan,
        has_interacted=has_interacted,
        session_state=session_state,
        checked_in_today=checked_in_today,
    )
