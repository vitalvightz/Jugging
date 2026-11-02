"""What an inferred training focus may say and how it is matched to upcoming work.

The movement-pattern vocabulary lives in shared/training-focus-patterns.json,
which web/lib/training-focus.ts reads too, so the pattern grounded here is the
one Today matches against.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

_PATH = Path(__file__).resolve().parents[2] / "shared" / "training-focus-patterns.json"


def _load() -> list[tuple[str, tuple[str, ...]]]:
    try:
        patterns = json.loads(_PATH.read_text(encoding="utf-8"))["patterns"]
    except FileNotFoundError as exc:
        raise RuntimeError(f"Training focus patterns are missing at {_PATH}. "
                           "Ensure shared/training-focus-patterns.json is packaged.") from exc
    loaded = [(item["id"], tuple(item["phrases"])) for item in patterns]
    if len({pattern for pattern, _ in loaded}) != len(loaded):
        raise RuntimeError("Training focus pattern ids must be unique")
    return loaded


PATTERNS = _load()
PATTERN_IDS = frozenset(pattern for pattern, _ in PATTERNS)

FocusScope = Literal["specific_exercise", "movement_pattern", "general_quality", "sport_technique"]
FocusCategory = Literal["strength", "conditioning", "technique", "sparring", "mobility"]
FocusSourceKind = Literal["sparring_log", "manual_takeaway", "plan_completion"]

FOCUS_MAX_CHARS = 90
_NON_WORD = re.compile(r"[^a-z0-9]+")


def words(value: Any) -> str:
    """Lowercase, accent-free words joined by single spaces ("Push-Ups" -> "push ups")."""
    text = unicodedata.normalize("NFKD", str(value or "").lower()).encode("ascii", "ignore").decode("ascii")
    return _NON_WORD.sub(" ", text.replace("&", " and ")).strip()


def contains_words(haystack: str, needle: str) -> bool:
    """``needle`` appears in ``haystack`` as whole words (both already ``words()``)."""
    return bool(needle) and f" {needle} " in f" {haystack} "


def movement_pattern(name: Any) -> str | None:
    """The first pattern whose phrase is in the exercise name, else None."""
    text = words(name)
    for pattern, phrases in PATTERNS:
        if any(contains_words(text, phrase) for phrase in phrases):
            return pattern
    return None


class InferredFocus(BaseModel):
    """The model's structured answer. ``focus`` None means no suggestion."""

    model_config = ConfigDict(extra="forbid")

    focus: str | None = None
    scope: FocusScope | None = None
    category: FocusCategory | None = None
    sport: str | None = None
    exercise_targets: list[str] = Field(default_factory=list, max_length=3)
    movement_patterns: list[str] = Field(default_factory=list, max_length=3)


def response_schema() -> dict[str, Any]:
    """Strict JSON schema for the Responses API (every field required, nullables typed)."""
    nullable_string = {"type": ["string", "null"]}
    return {
        "type": "json_schema",
        "name": "training_focus",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["focus", "scope", "category", "sport", "exercise_targets", "movement_patterns"],
            "properties": {
                "focus": nullable_string,
                "scope": {"type": ["string", "null"], "enum": [*FocusScope.__args__, None]},
                "category": {"type": ["string", "null"], "enum": [*FocusCategory.__args__, None]},
                "sport": nullable_string,
                "exercise_targets": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                "movement_patterns": {"type": "array", "items": {"type": "string", "enum": sorted(PATTERN_IDS)},
                                      "maxItems": 3},
            },
        },
    }


# Health and medical language: no training cue is generated from it, in the
# note or in the model's answer. Deliberately broad; a missed cue is cheap.
_HEALTH = re.compile(
    r"\b(pain\w*|hurt\w*|injur\w*|sore\w*|ach(e|es|ing|ed)|concuss\w*|dizz\w*|headache\w*|nause\w*|vomit\w*|"
    r"black(ed)? ?out|knocked (out|down)|rocked|ko|numb\w*|tingl\w*|swell\w*|swollen|sprain\w*|strain(ed)?|"
    r"tweak\w*|pulled (a|my)|bleed\w*|blood\w*|bruis\w*|fractur\w*|broken|tendon\w*|tendin\w*|ligament\w*|"
    r"acl|mcl|physio\w*|doctor\w*|medic\w*|rehab\w*|flare\w*|cramp\w*|blurr\w*|seeing stars|faint\w*|"
    r"sick|ill|fever\w*|chest tight\w*|can'?t breathe|migraine\w*|stiff neck|whiplash)\b",
    re.IGNORECASE,
)
# A cue is technique or attention only. It never changes the prescribed work.
_WORKLOAD = re.compile(
    r"\d|\b(increase\w*|decrease\w*|reduce\w*|add(ing)? (weight|load|sets?|reps?|rounds?|volume)|heavier|lighter|"
    r"more (sets?|reps?|rounds?|weight|load|volume)|fewer|extra|skip\w*|deload\w*|push through|max(imal|imum)?|"
    r"failure|ignore\w*|instead of|replace\w*|swap\w*|rest (day|longer))\b",
    re.IGNORECASE,
)


def mentions_health(text: str) -> bool:
    return bool(_HEALTH.search(text or ""))


def grounded_focus(inferred: InferredFocus, *, exercise_names: list[str], patterns: set[str],
                   categories: set[str], sports: set[str]) -> dict[str, Any] | None:
    """The model's answer kept only when every match field is grounded in the
    noted day's context; None otherwise (no suggestion beats a wrong one)."""
    text = " ".join((inferred.focus or "").split())
    if not text or len(text) > FOCUS_MAX_CHARS or inferred.scope is None or inferred.category is None:
        return None
    if mentions_health(text) or _WORKLOAD.search(text):
        return None
    if inferred.category not in categories:
        return None
    names = [words(name) for name in exercise_names if words(name)]
    # A target is an exercise in the context, or its family as written there
    # ("split squat" of "Bulgarian Split Squat"): never a name the day lacks.
    targets = []
    for target in inferred.exercise_targets:
        key = words(target)
        if not key or not any(contains_words(name, key) for name in names):
            return None
        targets.append(key)
    chosen = list(dict.fromkeys(inferred.movement_patterns))
    if any(pattern not in patterns for pattern in chosen):
        return None
    sport = inferred.sport if inferred.sport in sports else None
    if inferred.sport and sport is None:
        return None
    if inferred.scope == "specific_exercise":
        # Matched on the exercise only: a specific problem is never broadened.
        if not targets:
            return None
        chosen = []
    elif inferred.scope in {"movement_pattern", "general_quality"}:
        # Transfer must name where it applies; a bare "strength" never matches.
        if not chosen:
            return None
    elif inferred.scope == "sport_technique":
        if inferred.category not in {"technique", "sparring"}:
            return None
        targets, chosen = [], []
    return {"focus": text, "scope": inferred.scope, "category": inferred.category, "sport": sport,
            "exercise_targets": list(dict.fromkeys(targets)), "movement_patterns": chosen}
