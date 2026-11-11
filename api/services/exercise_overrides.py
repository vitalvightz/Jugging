"""Apply an athlete's exercise swaps, additions and removals to a projected card.

The generated card in ``plans.structured_plan`` is never edited. An athlete's
changes live beside it in ``plans.exercise_overrides`` and are applied here,
after the calendar is projected onto dates, by every reader of the effective
plan: Plan Detail (``api.plan_mappers``), Today, session start and exercise
logging (``api.services.today_service._projected_structured_plan``). One pure
function means every surface shows the same effective workout.

An entry is one change:

* ``action``: ``swap`` (replace one block), ``remove`` (drop one block) or
  ``add`` (append one block to a session).
* ``scope``: ``occurrence`` applies on ``date`` only. ``future`` applies on
  ``date`` and every later date with the same ``weekday`` in an open plan, to
  the same workout (``session_slot``) and the same exercise (``target_key``),
  so a four-week template whose weeks carry different ids still follows it.
* ``original``: the block as prescribed when the change was made (provenance;
  never re-applied).
* ``block``: the athlete's selection, already built and checked server-side.
* ``revoked_at``: set by Undo / Restore. Revoked entries are kept as history.
* ``ends_before``: a recurring change stopped by Restore after it had already
  applied to started or past workouts; those dates keep it.

Days without a calendar date are left alone: a change always names a date, and
an undated card has no occurrence to attach it to. Rehab blocks are protected
here as well as at write time — they are owned by the injury system.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import date
from typing import Any

OVERRIDE_ACTIONS = frozenset({"swap", "remove", "add", "edit"})
OVERRIDE_SCOPES = frozenset({"occurrence", "future"})
OVERRIDE_BLOCK_PREFIX = "ovr-"
WEEKDAY_TOKENS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
PROTECTED_BLOCK_TYPES = frozenset({"rehab"})

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(value: Any) -> str:
    return _SLUG_RE.sub("-", str(value or "").strip().lower()).strip("-")[:80]


def block_identity(block: Mapping[str, Any]) -> str:
    """Which exercise a block is: its canonical key, else its name."""
    return slug(block.get("exercise_key")) or slug(block.get("display_name"))


def session_slot(session: Mapping[str, Any], *, position: int | None = None) -> str:
    """A recurring day slot, independent of week-specific session ids.

    The projected template preserves day membership/order. Include that position
    and the workout identity so two same-name sessions cannot share a slot.
    Without a position, retain the legacy identity for existing stored changes.
    """
    name = slug(session.get("title")) or slug(session.get("session_type"))
    if position is None:
        return name
    return f"v2:{position}:{slug(session.get('session_type'))}:{name}"


def override_block_id(override_id: str) -> str:
    return f"{OVERRIDE_BLOCK_PREFIX}{slug(override_id).replace('-', '')[:16]}"


def weekday_token(day: date) -> str:
    return WEEKDAY_TOKENS[day.weekday()]


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def stored_overrides(plan_row: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = plan_row.get("exercise_overrides")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return [dict(entry) for entry in value if isinstance(entry, Mapping)] if isinstance(value, list) else []


def active_overrides(plan_row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Unrevoked, well-formed entries in the order they were made."""
    entries = []
    for entry in stored_overrides(plan_row):
        if entry.get("revoked_at") or entry.get("action") not in OVERRIDE_ACTIONS:
            continue
        if entry.get("scope") not in OVERRIDE_SCOPES or _parse_date(entry.get("date")) is None:
            continue
        if entry["action"] != "remove" and not isinstance(entry.get("block"), Mapping):
            continue
        entries.append(entry)
    return entries


def is_custom_plan(plan_row: Mapping[str, Any]) -> bool:
    brief = plan_row.get("planning_brief")
    if isinstance(brief, str):
        try:
            brief = json.loads(brief)
        except json.JSONDecodeError:
            brief = None
    return isinstance(brief, Mapping) and bool(brief.get("custom_plan"))


def entry_applies_on(entry: Mapping[str, Any], day_date: date) -> bool:
    start = _parse_date(entry.get("date"))
    if start is None:
        return False
    if entry.get("scope") == "occurrence":
        return day_date == start
    # A recurring change that was later undone keeps the dates it already
    # covered (history) and stops at ``ends_before``.
    end = _parse_date(entry.get("ends_before"))
    if end is not None and day_date >= end:
        return False
    return day_date >= start and weekday_token(day_date) == entry.get("weekday")


def _matching_session(sessions: list[Any], entry: Mapping[str, Any]) -> dict[str, Any] | None:
    by_id = next((s for s in sessions if isinstance(s, dict)
                  and str(s.get("session_id") or "") == str(entry.get("session_id") or "")), None)
    if by_id is not None or entry.get("scope") != "future":
        return by_id
    slot = entry.get("session_slot")
    matches = [s for position, s in enumerate(sessions) if isinstance(s, dict) and slot
               and (session_slot(s, position=position) == slot or session_slot(s) == slot)]
    # Old title-only changes are safe only when that title identifies one session.
    return matches[0] if len(matches) == 1 else None


def editable_day_sessions(day: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Address the existing headline-only technical session by its dated id.

    Today already logs a sessionless training day against its calendar date.
    Materialise that same owner only when an exercise change needs a slot; the
    original persisted day remains untouched.
    """
    sessions = day.get("sessions")
    if isinstance(sessions, list) and sessions:
        return sessions
    card = day.get("today_card") if isinstance(day.get("today_card"), Mapping) else {}
    title = str(card.get("headline") or "").strip()
    if not _parse_date(day.get("date")) or not re.search(r"\b(?:technical|light combat|controlled.*(?:rounds|combat))\b", title, re.I):
        return []
    return [{"session_id": str(day["date"])[:10], "session_type": "skill", "title": title,
             "objective": "", "mindset_anchor": deepcopy(card.get("mindset_anchor") or
                 {"intent": "", "focus_cue": "", "reset_cue": ""}), "blocks": []}]


def _matching_block_index(blocks: list[Any], entry: Mapping[str, Any]) -> int | None:
    target = str(entry.get("block_id") or "")
    for index, block in enumerate(blocks):
        if isinstance(block, Mapping) and target and str(block.get("block_id") or "") == target:
            return index
    if entry.get("scope") != "future":
        return None
    key = entry.get("target_key")
    for index, block in enumerate(blocks):
        # A block another change produced is that change's, never a match by name.
        if (isinstance(block, Mapping) and key and not block.get("athlete_change")
                and block_identity(block) == key):
            return index
    return None


def _provenance(entry: Mapping[str, Any]) -> dict[str, Any]:
    original = entry.get("original") if isinstance(entry.get("original"), Mapping) else {}
    return {
        "override_id": entry.get("id"),
        "action": entry.get("action"),
        "scope": entry.get("scope"),
        "from_date": entry.get("date"),
        **({"original_name": original.get("display_name")} if original.get("display_name") else {}),
    }


def _athlete_block(entry: Mapping[str, Any], *, replaced: Mapping[str, Any] | None, siblings: list[Any]) -> dict[str, Any]:
    """The athlete's block with its progression verdict decided here, on read.

    ``rep_range`` and ``progression_hold`` are server-owned. A stored change
    never carries them forward (an edit is saved as a copy of the block it
    edited, on whatever day that was): the range is dropped, because the
    athlete fixed this dose themselves, and the hold is re-derived from the
    block being replaced or the session being added to.
    """
    from api.rep_range import override_progression_hold

    block = deepcopy(dict(entry["block"]))
    block.pop("rep_range", None)
    block.pop("progression_hold", None)
    if override_progression_hold(block, replaced=replaced, siblings=siblings):
        block["progression_hold"] = True
    return block


def _apply_entry(session: dict[str, Any], entry: Mapping[str, Any]) -> bool:
    blocks = list(session.get("blocks") or [])
    action = entry["action"]
    if action == "add":
        new_id = override_block_id(str(entry.get("id")))
        if any(isinstance(b, Mapping) and b.get("block_id") == new_id for b in blocks):
            return False
        block = _athlete_block(entry, replaced=None, siblings=blocks)
        block["block_id"] = new_id
        block["order_index"] = len(blocks) + 1
        block["athlete_change"] = _provenance(entry)
        session["blocks"] = [*blocks, block]
        return True
    index = _matching_block_index(blocks, entry)
    if index is None:
        return False
    original = blocks[index]
    if str(original.get("block_type") or "") in PROTECTED_BLOCK_TYPES:
        return False
    if action == "remove":
        removed = list(session.get("athlete_removed") or [])
        removed.append({"override_id": entry.get("id"), "display_name": original.get("display_name"),
                        "scope": entry.get("scope")})
        session["athlete_removed"] = removed
        session["blocks"] = blocks[:index] + blocks[index + 1:]
        return True
    block = _athlete_block(entry, replaced=original, siblings=blocks)
    block["block_id"] = override_block_id(str(entry.get("id")))
    if original.get("order_index") is not None:
        block["order_index"] = original.get("order_index")
    block["athlete_change"] = _provenance(entry)
    blocks[index] = block
    session["blocks"] = blocks
    return True


def apply_exercise_overrides(
    plan_row: Mapping[str, Any], structured_plan: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    """The projected card with the athlete's active changes applied.

    Returns the input unchanged (same object) when nothing applies, so callers
    that compare identity to skip re-validation keep doing so. Only the days a
    change touches are copied.
    """
    entries = active_overrides(plan_row)
    if not entries or not isinstance(structured_plan, Mapping):
        return structured_plan
    weeks = structured_plan.get("weeks")
    if not isinstance(weeks, list):
        return structured_plan
    result: dict[str, Any] | None = None
    for week_index, week in enumerate(weeks):
        if not isinstance(week, Mapping) or not isinstance(week.get("days"), list):
            continue
        for day_index, day in enumerate(week["days"]):
            if not isinstance(day, Mapping):
                continue
            day_date = _parse_date(day.get("date"))
            if day_date is None:
                continue
            applicable = [entry for entry in entries if entry_applies_on(entry, day_date)]
            if not applicable:
                continue
            new_day = deepcopy(dict(day))
            sessions = editable_day_sessions(new_day)
            new_day["sessions"] = sessions
            changed = False
            # Recurring changes first, then the one-off changes for this date,
            # so "just this workout" wins over "future workouts" on its day.
            ordered = sorted(applicable, key=lambda e: e.get("scope") == "occurrence")
            for entry in ordered:
                session = _matching_session(sessions, entry)
                if session is not None and _apply_entry(session, entry):
                    changed = True
            if not changed:
                continue
            if result is None:
                result = dict(structured_plan)
                result["weeks"] = list(weeks)
            new_week = result["weeks"][week_index]
            if new_week is week:
                new_week = dict(week)
                new_week["days"] = list(week["days"])
                result["weeks"][week_index] = new_week
            new_week["days"][day_index] = new_day
    return result if result is not None else structured_plan


__all__ = [
    "OVERRIDE_ACTIONS",
    "OVERRIDE_SCOPES",
    "PROTECTED_BLOCK_TYPES",
    "active_overrides",
    "apply_exercise_overrides",
    "block_identity",
    "entry_applies_on",
    "is_custom_plan",
    "override_block_id",
    "session_slot",
    "slug",
    "stored_overrides",
    "weekday_token",
]
