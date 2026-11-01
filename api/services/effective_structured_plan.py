"""Resolve the one effective structured calendar for a persisted plan row."""

from __future__ import annotations

import json
from copy import deepcopy
import logging
import re
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import date
from typing import Any

from api.structured_block_identity import ensure_structured_block_ids
from api.structured_plan_deterministic_fallback import build_deterministic_structured_plan
from api.structured_plan_models import safe_parse_structured_plan
from api.structured_plan_sparring_reconcile import surface_coach_led_contact

logger = logging.getLogger(__name__)


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, Mapping) else None
    return None


def _has_usable_dated_calendar(plan: Mapping[str, Any]) -> bool:
    weeks = plan.get("weeks")
    if not isinstance(weeks, Sequence) or isinstance(weeks, (str, bytes, bytearray)):
        return False
    for week in weeks:
        if not isinstance(week, Mapping):
            continue
        days = week.get("days")
        if not isinstance(days, Sequence) or isinstance(days, (str, bytes, bytearray)):
            continue
        for day in days:
            if not isinstance(day, Mapping):
                continue
            try:
                date.fromisoformat(str(day.get("date") or "")[:10])
            except ValueError:
                continue
            return True
    return False


def _has_usable_open_weekly_calendar(
    plan_row: Mapping[str, Any], plan: Mapping[str, Any]
) -> bool:
    """Recognise the older renewable cards whose rows are weekday templates."""

    if str(plan_row.get("fight_date") or "").strip():
        return False
    weeks = plan.get("weeks")
    if not isinstance(weeks, Sequence) or not weeks:
        return False
    valid_weekdays = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    for week in weeks:
        if not isinstance(week, Mapping):
            return False
        days = week.get("days")
        if not isinstance(days, Sequence) or len(days) < 2:
            return False
        if any(
            not isinstance(day, Mapping)
            or str(day.get("weekday") or "").strip().lower()[:3] not in valid_weekdays
            for day in days
        ):
            return False
    return True


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(value: Any) -> str:
    return _SLUG_RE.sub("-", str(value or "").strip().lower()).strip("-")[:32].strip("-")


def _rows(container: Any, key: str) -> list[Any]:
    """A saved card's nested rows, or ``[]`` when malformed (never iterate a scalar)."""
    rows = container.get(key) if isinstance(container, Mapping) else None
    return rows if isinstance(rows, list) else []


def _session_rows(day: Any) -> list[Any]:
    return _rows(day, "sessions")


_NESTED_ROWS = ("weeks", "days", "sessions", "blocks")


def _without_scalar_rows(plan: dict[str, Any]) -> dict[str, Any]:
    """Replace a stored non-list weeks/days/sessions/blocks value with ``[]``.

    Every reader iterates these as lists; a malformed card (a number or flag
    where rows belong) then degrades to an empty level instead of crashing.
    The stored card is never mutated.
    """
    def malformed(node: Any, depth: int) -> bool:
        if depth == len(_NESTED_ROWS) or not isinstance(node, Mapping):
            return False
        rows = node.get(_NESTED_ROWS[depth])
        if rows is None:
            return False
        if not isinstance(rows, list):
            return True
        return any(malformed(row, depth + 1) for row in rows)

    def repair(node: Any, depth: int) -> None:
        if depth == len(_NESTED_ROWS) or not isinstance(node, dict):
            return
        key = _NESTED_ROWS[depth]
        if key in node and node[key] is not None and not isinstance(node[key], list):
            node[key] = []
        for row in node.get(key) or []:
            repair(row, depth + 1)

    if not malformed(plan, 0):
        return plan
    plan = deepcopy(plan)
    repair(plan, 0)
    return plan


def _needs_session_ids(plan: Mapping[str, Any]) -> bool:
    weeks = plan.get("weeks")
    if not isinstance(weeks, list):
        return False
    return any(
        isinstance(session, Mapping) and not str(session.get("session_id") or "").strip()
        for week in weeks
        if isinstance(week, Mapping)
        for day in (week.get("days") if isinstance(week.get("days"), list) else [])
        for session in _session_rows(day)
    )


def ensure_structured_session_ids(
    plan: Any, *, plan_id: Any = None
) -> Any:
    """Give every structured session a stable id, deriving the missing ones.

    Completion, the session timer, streaks and week progress all key on
    ``session_id``. The converter is told identifiers are optional and usually
    emits null, which left most model-written sessions impossible to start or
    log ("completion is unavailable") and invisible to week progress. The
    derived id is a pure function of the stored card, so every reader of the
    same plan agrees on it without a write:
    ``ses-<plan>-<date or week/day slot>-<title>``, with a numeric suffix for a
    repeated title on the same day. The plan prefix keeps a regenerated plan's
    sessions from colliding with the old plan's completion rows on the same day.
    Existing ids are never changed. The input is not mutated.
    """
    if not isinstance(plan, Mapping):
        return plan
    if not _needs_session_ids(plan):
        return dict(plan)
    # Copy only the week -> day -> session path being written; blocks and every
    # other nested value stay shared with the (possibly cached) stored card.
    result = dict(plan)
    result["weeks"] = weeks = [
        dict(week) if isinstance(week, Mapping) else week for week in plan.get("weeks") or []
    ]
    plan_part = _slug(plan_id)[:8]
    for week_index, week in enumerate(weeks):
        if not isinstance(week, dict) or not isinstance(week.get("days"), list):
            continue
        week["days"] = days = [dict(day) if isinstance(day, Mapping) else day for day in week["days"]]
        for day_index, day in enumerate(days):
            if not isinstance(day, dict):
                continue
            sessions = [
                dict(session) if isinstance(session, Mapping) else session
                for session in _session_rows(day)
            ]
            if isinstance(day.get("sessions"), list):
                day["sessions"] = sessions
            taken = {
                str(session.get("session_id") or "").strip()
                for session in sessions
                if isinstance(session, Mapping)
            }
            date_part = str(day.get("date") or "").strip()[:10]
            try:
                date.fromisoformat(date_part)
            except ValueError:
                date_part = f"w{week_index + 1}d{day_index + 1}"
            for session_index, session in enumerate(sessions):
                if not isinstance(session, dict) or str(session.get("session_id") or "").strip():
                    continue
                title = _slug(session.get("title")) or _slug(session.get("session_type")) or str(
                    session_index + 1
                )
                base = "-".join(part for part in ("ses", plan_part, date_part, title) if part)
                candidate = base
                suffix = 2
                while candidate in taken:
                    candidate = f"{base}-{suffix}"
                    suffix += 1
                taken.add(candidate)
                session["session_id"] = candidate
    return result


def resolve_effective_structured_plan(
    plan_row: Mapping[str, Any], *, raw_markdown: str | None = None
) -> dict[str, Any] | None:
    """Return the valid stored card, or rebuild it from deterministic planner truth.

    Missing and malformed inputs fail closed. In particular, this resolver does
    not derive a calendar from broad recurring metadata. Every session in the
    returned card carries a ``session_id`` (see ensure_structured_session_ids),
    and every non-rehab block a ``block_id`` unique within its day (see
    ensure_structured_block_ids), which is what exercise logs key on.
    """
    resolved = _resolve_effective_structured_plan(plan_row, raw_markdown=raw_markdown)
    if resolved is None:
        return None
    resolved = _without_scalar_rows(resolved)
    # These fields are server-owned by custom publication, never by a model.
    # A generated card cannot claim athlete authority or private attachments.
    brief = _mapping(plan_row.get("planning_brief")) or {}
    if not plan_row.get("fight_date") and (brief.get("open_plan_spec") or {}).get("weekly_session_plan"):
        from api.structured_plan_locked_merge import merge_planner_owned_structured_content

        # Preserve the ids older id-less templates already derived on read,
        # before their one source week is expanded into four actual weeks.
        resolved = ensure_structured_session_ids(resolved, plan_id=plan_row.get("id"))
        resolved = merge_planner_owned_structured_content(resolved, dict(brief)).plan
    from api.structured_plan_coaching import recover_selected_coaching
    resolved = _without_athlete_change_claims(resolved)
    resolved = recover_selected_coaching(resolved, dict(brief),
        source_text=raw_markdown or str(plan_row.get("plan_text") or ""))
    if not brief.get("custom_plan"):
        fields = ("prescription_source", "prescription_text", "athlete_dose",
                  "private_media_id", "community_revision_id", "source_session_id", "catalogue_id")
        sessions = [session for week in _rows(resolved, "weeks") if isinstance(week, Mapping)
                    for day in _rows(week, "days") for session in _session_rows(day) if isinstance(session, Mapping)]
        nodes = [resolved.get("plan_metadata") or {}, *sessions,
                 *(block for session in sessions for block in _rows(session, "blocks") if isinstance(block, Mapping))]
        if any(node.get(field) is not None for node in nodes if isinstance(node, Mapping) for field in fields):
            resolved = deepcopy(resolved)
            if isinstance(resolved.get("plan_metadata"), dict):
                resolved["plan_metadata"].pop("prescription_source", None)
            for week in _rows(resolved, "weeks"):
                if not isinstance(week, Mapping):
                    continue
                for day in _rows(week, "days"):
                    for session in _session_rows(day):
                        if not isinstance(session, dict):
                            continue
                        session.pop("prescription_source", None)
                        for block in _rows(session, "blocks"):
                            if not isinstance(block, dict):
                                continue
                            for field in fields:
                                block.pop(field, None)
    # Reconcile old saved cards on read so Plan, Today, logging and adherence
    # consume the same server-owned optional flag without rewriting stored plans.
    from api.optional_sessions import mark_optional_support_inserts

    resolved = deepcopy(resolved)
    mark_optional_support_inserts(resolved, brief)
    resolved = _with_coach_led_contact(resolved, brief)
    resolved = ensure_structured_session_ids(resolved, plan_id=plan_row.get("id"))
    return ensure_structured_block_ids(resolved)


def _with_coach_led_contact(plan: dict[str, Any], brief: Mapping[str, Any]) -> dict[str, Any]:
    """Every surface reads a contact day's sparring from ``coach_led_contact``.

    Cards saved before the contact invariant held can carry hard sparring only
    in a headline their own sessions shadow; Today, progress and the plan view
    would then show the day as zero load. Surfaced on a copy, never in place.
    """
    if not isinstance(brief, dict) or not brief.get("weekly_role_map"):
        return plan
    candidate = deepcopy(plan)
    return candidate if surface_coach_led_contact(candidate, brief) else plan


def _without_athlete_change_claims(plan: dict[str, Any]) -> dict[str, Any]:
    """Drop override provenance a stored card claims for itself.

    ``athlete_change`` / ``athlete_removed`` mark changes applied from
    plans.exercise_overrides after projection. A stored card (generated or
    custom) never carries them legitimately, so a model cannot fake an
    athlete-directed change or hide one.
    """
    sessions = [session for week in _rows(plan, "weeks") if isinstance(week, Mapping)
                for day in _rows(week, "days") for session in _session_rows(day) if isinstance(session, Mapping)]
    if not any("athlete_removed" in session or any(
            isinstance(block, Mapping) and "athlete_change" in block for block in _rows(session, "blocks"))
            for session in sessions):
        return plan
    plan = deepcopy(plan)
    for week in _rows(plan, "weeks"):
        for day in _rows(week, "days"):
            for session in _session_rows(day):
                if not isinstance(session, dict):
                    continue
                session.pop("athlete_removed", None)
                for block in _rows(session, "blocks"):
                    if isinstance(block, dict):
                        block.pop("athlete_change", None)
    return plan


def _resolve_effective_structured_plan(
    plan_row: Mapping[str, Any], *, raw_markdown: str | None = None
) -> dict[str, Any] | None:

    legacy_open_calendar: dict[str, Any] | None = None
    stored = _mapping(plan_row.get("structured_plan"))
    if stored:
        from api.structured_plan_calendar_spine import reconcile_calendar_spine

        brief = _mapping(plan_row.get("planning_brief")) or {}
        if not brief.get("custom_plan"):
            stored = reconcile_calendar_spine(dict(stored), dict(brief),
                plan_text=raw_markdown or str(plan_row.get("plan_text") or ""))
        parsed = safe_parse_structured_plan(stored, raw_markdown=raw_markdown or None)
        if parsed.ok and parsed.plan is not None:
            # Old Open Plan cards predate the undated exercise-membership gate.
            # Never let a schema-valid but invented workout reach Plan/Today/logs.
            spec = brief.get("open_plan_spec") or {}
            if not brief.get("custom_plan") and isinstance(spec, dict) and spec.get("plan_type") == "open_ongoing_system":
                from api.structured_plan_faithfulness import INTRODUCED, INTERNAL, check_structured_faithfulness

                findings = check_structured_faithfulness(
                    dict(stored), raw_markdown or str(plan_row.get("plan_text") or ""), dict(brief),
                )
                unsafe = [finding for finding in findings if finding.startswith((f"{INTRODUCED}: open week", f"{INTERNAL}:"))]
                if unsafe:
                    logger.error(
                        "saved Open Plan failed source membership; using verified fallback plan_id=%s findings=%s",
                        plan_row.get("id"), unsafe,
                    )
                else:
                    return dict(stored)
            else:
                return dict(stored)
        # Some older cards predate the current strict display schema but still
        # carry the dated ``weeks`` calendar consumed by session services. Keep
        # their established behaviour; absence of that calendar is what permits
        # deterministic reconstruction.
        if _has_usable_dated_calendar(stored):
            return dict(stored)
        if _has_usable_open_weekly_calendar(plan_row, stored):
            # Try planner reconstruction first. This is retained only for old
            # renewable plans whose canonical cards are weekday templates.
            legacy_open_calendar = dict(stored)
        logger.warning(
            "stored structured_plan failed validation; trying deterministic fallback plan_id=%s",
            plan_row.get("id"),
        )

    planning_brief = _mapping(plan_row.get("planning_brief"))
    # Every surface (plan view, Today, progress) reads the same athlete-visible
    # plan text, so the rebuilt card carries the doses the athlete was given
    # wherever it renders. A held plan's unreleased text is never read here.
    plan_text = raw_markdown or str(plan_row.get("plan_text") or "")
    fallback = build_deterministic_structured_plan(planning_brief, plan_text=plan_text or None)
    if fallback is None:
        return legacy_open_calendar
    parsed = safe_parse_structured_plan(fallback, raw_markdown=raw_markdown or None)
    if not parsed.ok or parsed.plan is None:
        logger.warning(
            "deterministic structured fallback failed validation plan_id=%s",
            plan_row.get("id"),
        )
        return None
    logger.info("deterministic effective structured plan assembled plan_id=%s", plan_row.get("id"))
    return fallback


__all__ = ["ensure_structured_session_ids", "resolve_effective_structured_plan"]
