"""Last deterministic check on Stage 1 membership before it reaches Stage 2.

Stage 2 renders the planner's closed sessions and the structured-card audit
then re-checks them. When Stage 1 hands over a day the audit forbids, the
model is asked to render something the card will reject: the plan ships as
raw text with no card. This guard runs on the final weekly role map and makes
the handoff clean by construction.

It only ever removes OPTIONAL embedded support (``embedded_support: True``,
e.g. the low-load trunk drill composition tucks into a conditioning session).
Programmed work is never deleted here; a conflict that removing optional
support cannot resolve is recorded as a finding and logged, so it is visible
instead of surfacing later as a blocked card.
"""
from __future__ import annotations

import logging
from typing import Any

from .calendar_context import role_d_day
from .session_workload import UNSAFE_STACKING_CODE, substantial_endurance

logger = logging.getLogger(__name__)

HANDOFF_STACKING_REMOVAL = "handoff_removed_embedded_support_on_endurance_day"


def _day_key(week_index: int, week: dict[str, Any], role: dict[str, Any]) -> tuple:
    d_day = role_d_day(week, role)
    if d_day is not None:
        return (week_index, "d", d_day)
    weekday = str(role.get("scheduled_day_hint") or role.get("real_weekday") or "").strip().lower()
    return (week_index, "w", weekday or id(role))


def _members(role: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in role.get("selected_exercise_assignments") or [] if isinstance(m, dict)]


def _is_strength_member(member: dict[str, Any]) -> bool:
    return str(member.get("slot_group") or "") == "strength_slots"


def _has_substantial_endurance(member: dict[str, Any]) -> bool:
    if _is_strength_member(member):
        return False
    try:
        return substantial_endurance(member)
    except Exception:  # never let a measurement quirk break the handoff
        logger.exception("[handoff] endurance measurement failed for %s", member.get("name"))
        return False


def enforce_handoff_integrity(weekly_role_map: dict[str, Any]) -> list[dict[str, Any]]:
    """Repair what can be repaired safely; return findings for what cannot.

    Rule (mirrors ``session_workload.structured_stacking_findings``): a day that
    holds substantial continuous endurance must not also hold strength-slot
    work. Optional embedded support on such a day is removed; any remaining
    programmed strength member is reported, never silently dropped.
    """
    findings: list[dict[str, Any]] = []
    if not isinstance(weekly_role_map, dict):
        return findings
    for week_index, week in enumerate(weekly_role_map.get("weeks") or []):
        if not isinstance(week, dict):
            continue
        days: dict[tuple, list[dict[str, Any]]] = {}
        for role in week.get("session_roles") or []:
            if isinstance(role, dict):
                days.setdefault(_day_key(week_index, week, role), []).append(role)
        for key, roles in days.items():
            endurance = [m["name"] for r in roles for m in _members(r) if _has_substantial_endurance(m)]
            if not endurance:
                continue
            for role in roles:
                members = _members(role)
                removed = [m for m in members if _is_strength_member(m) and m.get("embedded_support")]
                if removed:
                    role["selected_exercise_assignments"] = [m for m in members if m not in removed]
                    audit = role.setdefault("handoff_integrity", [])
                    audit.append({
                        "code": HANDOFF_STACKING_REMOVAL,
                        "removed": [m.get("name") for m in removed],
                        "endurance": endurance,
                    })
                    logger.info(
                        "[handoff] removed embedded support %s from %s (endurance day: %s)",
                        [m.get("name") for m in removed], role.get("role_key"), endurance,
                    )
            remaining = [m.get("name") for r in roles for m in _members(r) if _is_strength_member(m)]
            if remaining:
                finding = {
                    "code": UNSAFE_STACKING_CODE,
                    "week_index": week_index,
                    "day": key[2],
                    "endurance": endurance,
                    "strength": remaining,
                }
                findings.append(finding)
                logger.warning("[handoff] unresolved same-day endurance + strength: %s", finding)
    if findings:
        weekly_role_map.setdefault("handoff_integrity_findings", []).extend(findings)
    return findings
