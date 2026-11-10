"""Server-owned progression context for a block: its permitted rep range and holds.

The planner resolves a bank range ("2-3x6-10") to one working number ("2x6")
before the plan is written, and that number stays the prescription. This only
recovers the range the prescription was chosen from, so the Today log can
suggest one more rep inside it. Nothing is widened, no dose changes, and no
model call is made: both fields are re-derived from the planning brief on
every read.

``progression_hold`` marks a block whose exposure must not progress at all
(taper, the final fortnight, an injury, a weight cut, a capped or protected
dose); the log then suggests neither a rep nor a weight.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from fightcamp.exercise_progression import progression_prohibited

# "2–3x6–10", "3 x 6-8 per side", "2-4 sets x 6-10 reps". The reps must be a range.
_SETS_X_REPS = re.compile(
    r"(?<![\d.])\d+(?:\s*[-–—]\s*\d+)?\s*(?:sets?\s*)?[x×]\s*(\d+)\s*[-–—]\s*(\d+)(?![\d.])(?!\s*(?:s|sec|secs|seconds?|min|mins|minutes?|m|metres?|meters?|%)\b)",
    re.IGNORECASE,
)
# Choices and compound doses ("6-10 reps or 20-40s", "heavy 2-3 → explosive") abstain.
_AMBIGUOUS = re.compile(r"\bor\b|→|\brounds?\b|\bholds?\b|\bcarries\b|\bthrows?\b|\bRM\b", re.IGNORECASE)


def authored_rep_range(prescription: Any) -> tuple[int, int] | None:
    """The one rep range a bank prescription authorises, or None."""
    text = re.sub(r"\s*\(or\s+[^)]*\)", "", str(prescription or ""), flags=re.I)
    if not text or _AMBIGUOUS.search(text):
        return None
    matches = _SETS_X_REPS.findall(text)
    if len(matches) != 1:
        return None
    low, high = int(matches[0][0]), int(matches[0][1])
    return (low, high) if 0 < low < high <= 30 else None


def _block_reps(block: Mapping[str, Any]) -> int | None:
    reps = block.get("reps")
    if isinstance(reps, bool):
        return None
    if isinstance(reps, int):
        return reps
    if isinstance(reps, str) and reps.strip().isdigit():
        return int(reps.strip())
    return None


def progression_context(
    block: Mapping[str, Any],
    role: Mapping[str, Any] | None,
    assignment: Mapping[str, Any] | None,
    *,
    phase: str,
    d_day: int | None,
    athlete: Mapping[str, Any],
) -> dict[str, Any]:
    """``{"rep_range": [low, high]}``, ``{"progression_hold": True}`` or ``{}``."""
    if role is None or assignment is None or block.get("prescription_source") == "athlete":
        return {}
    if progression_prohibited(dict(role), dict(assignment), phase, d_day, dict(athlete)):
        return {"progression_hold": True}
    base = assignment.get("base_prescription") or assignment.get("prescription")
    effective = assignment.get("effective_prescription")
    if effective and str(effective).strip() != str(base or "").strip():
        # A resolved dose that differs from the bank (a cap, a primer) is fixed.
        from fightcamp.working_dose import working_dose
        if str(effective).strip() not in {working_dose(str(base or "")), working_dose(str(base or ""), development=True)}:
            return {}
    span = authored_rep_range(base)
    reps = _block_reps(block)
    if span is None or reps is None or not span[0] <= reps <= span[1]:
        return {}
    return {"rep_range": [span[0], span[1]]}


def apply_progression_context(block: dict[str, Any], context: Mapping[str, Any]) -> bool:
    """Set exactly the server-owned fields ``context`` names. True when changed."""
    changed = False
    for key in ("rep_range", "progression_hold"):
        value = context.get(key)
        if value is None:
            if key in block:
                del block[key]
                changed = True
        elif block.get(key) != value:
            block[key] = value
            changed = True
    return changed
