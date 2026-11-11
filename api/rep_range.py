"""Server-owned progression context for a block: its permitted rep range and holds.

The planner resolves a bank range ("2-3x6-10") to one working number ("2x6")
before the plan is written, and that number stays the prescription. This only
recovers the range the prescription was chosen from, so the Today log can
suggest one more rep inside it. Nothing is widened, no dose changes, and no
model call is made: progression fields are re-derived from the planning brief on
every read.

``progression_hold`` is the server's verdict on whether the exposure may
progress. ``False`` means positively cleared: the block was matched to its
planner assignment, nothing prohibits progression, and the exercise bank
classifies the movement as ordinary strength work. ``True`` means it must not
progress (taper, the final fortnight, an injury, a capacity-restricting weight
cut, a capped or protected dose, or a jumping / ballistic / power movement).
Absent means the server has no verdict (an exercise it could not match or the
bank does not know); automatic increases require explicit clearance.
``bodyweight_progression`` carries bank eligibility separately from authored load.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Mapping

from fightcamp.config import DATA_DIR
from fightcamp.exercise_identity import normalize_exercise_key
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


# Jumps, hops, throws, ballistic lifts and speed-intent power work progress on
# movement quality and planned difficulty, never on reps or bar weight,
# whatever block type the plan gave them.
_REACTIVE_TAGS = frozenset({"mech_ballistic", "mech_reactive", "mech_lower_jump", "mech_landing_impact"})
# Bank methods that are ordinary rep-and-load work. "rehab" in the strength
# bank is prehab support (goblet squat, banded walks), not clinical rehab.
_PROGRESSIVE_METHODS = frozenset({"strength", "strength_conditioning", "rehab"})


@lru_cache(maxsize=1)
def _bank_classes() -> tuple[dict[str, bool | None], frozenset[str]]:
    """Progression verdicts and unweighted movement keys from one bank read."""
    rows = json.loads((DATA_DIR / "exercise_bank.json").read_text(encoding="utf-8"))
    classes: dict[str, bool | None] = {}
    bodyweight: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        key = normalize_exercise_key(row.get("name"))
        if not key:
            continue
        progressive = (
            str(row.get("method") or "").strip().lower() in _PROGRESSIVE_METHODS
            and not _REACTIVE_TAGS.intersection(row.get("mechanical_risk_tags") or ())
            and not _REACTIVE_TAGS.intersection(row.get("tags") or ())
        )
        classes[key] = progressive if classes.get(key, progressive) == progressive else None
        equipment = row.get("equipment")
        if equipment == "bodyweight" or (equipment in ("pullup_bar", "towel")
                and ("pull-up" in key or "chin-up" in key) and not key.startswith("weighted-")):
            bodyweight.add(key)
    return classes, frozenset(bodyweight)


def bank_allows_progression(exercise: Mapping[str, Any]) -> bool | None:
    """The exercise bank's verdict for a block or assignment.

    True: ordinary strength work. False: jumping, ballistic or power work.
    None: not in the strength bank, so unclassified.
    """
    classes, _ = _bank_classes()
    for key in (normalize_exercise_key(exercise.get("exercise_key")),
                normalize_exercise_key(exercise.get("name")),
                normalize_exercise_key(exercise.get("display_name"))):
        if key and key in classes:
            return classes[key]
    return None


def bank_uses_bodyweight(exercise: Mapping[str, Any]) -> bool:
    """Eligible unweighted strength movements; explicit authored loads always win."""
    classes, keys = _bank_classes()
    for field in ("exercise_key", "name", "display_name"):
        key = normalize_exercise_key(exercise.get(field))
        if key in classes:
            return classes[key] is True and key in keys
    return False


def progression_context(
    block: Mapping[str, Any],
    role: Mapping[str, Any] | None,
    assignment: Mapping[str, Any] | None,
    *,
    phase: str,
    d_day: int | None,
    athlete: Mapping[str, Any],
) -> dict[str, Any]:
    """The block's ``progression_hold`` verdict and, when cleared, its ``rep_range``.

    ``{}`` (unknown) when the block could not be matched to its planner
    assignment or the bank does not know the movement.
    """
    if (bank_allows_progression(block) is False or block.get("exercise_type") in {"power", "other"}
            or _REACTIVE_TAGS.intersection(block.get("tags") or ())
            or _REACTIVE_TAGS.intersection(block.get("mechanical_risk_tags") or ())):
        return {"progression_hold": True}
    if block.get("prescription_source") == "athlete":
        if block.get("progression_hold") is True or progression_prohibited(dict(role or {}), dict(assignment or {}), phase, d_day, dict(athlete)):
            return {"progression_hold": True}
        if bank_allows_progression(block) is not True and block.get("exercise_type") not in {"strength", "bodyweight"}:
            return {}
        context: dict[str, Any] = {"progression_hold": False}
        if not block.get("load") and (bank_uses_bodyweight(block) or block.get("exercise_type") == "bodyweight"):
            context["bodyweight_progression"] = True
        return context
    if role is None or assignment is None:
        return {}
    if progression_prohibited(dict(role), dict(assignment), phase, d_day, dict(athlete)):
        return {"progression_hold": True}
    # The bank is the authority, so briefs written before assignments carried
    # mechanical tags are protected too; the assignment's own tags still count.
    allowed = bank_allows_progression(assignment)
    if allowed is False or _REACTIVE_TAGS.intersection(assignment.get("mechanical_risk_tags") or ()):
        return {"progression_hold": True}
    if allowed is None:
        return {}
    cleared: dict[str, Any] = {"progression_hold": False}
    base = assignment.get("base_prescription") or assignment.get("prescription")
    effective = assignment.get("effective_prescription")
    if effective and str(effective).strip() != str(base or "").strip():
        # A resolved dose that differs from the bank (a cap, a primer) is fixed.
        from fightcamp.working_dose import working_dose
        if str(effective).strip() not in {working_dose(str(base or "")), working_dose(str(base or ""), development=True)}:
            return {"progression_hold": True}
    # Movement eligibility is metadata, never a rewrite of the authored load.
    if not block.get("load") and bank_uses_bodyweight(block):
        cleared["bodyweight_progression"] = True
    span = authored_rep_range(base)
    reps = _block_reps(block)
    if span is None or reps is None or not span[0] <= reps <= span[1]:
        return cleared
    return {**cleared, "rep_range": [span[0], span[1]]}


def apply_progression_context(block: dict[str, Any], context: Mapping[str, Any]) -> bool:
    """Set exactly the server-owned fields ``context`` names. True when changed."""
    changed = False
    for key in ("rep_range", "progression_hold", "bodyweight_progression"):
        value = context.get(key)
        if value is None:
            if key in block:
                del block[key]
                changed = True
        elif block.get(key) != value:
            block[key] = value
            changed = True
    return changed
