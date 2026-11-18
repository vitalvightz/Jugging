"""Apply a coach's written progression to a copied session.

When a later week is written as "Same as Week 1" plus a change, only changes
that are exact arithmetic on a dose are applied:

  * sets   - "Cut 1 set from every exercise", "add 2 sets", "one set fewer"
  * rounds - "Increase volume to 5 rounds", "Peak at 6 rounds", "Do 3 sharp rounds"

Everything else ("drop lift weights by 15%", "increase reps if easy") is kept
in the notes for the person to apply. A change that names a specific exercise
("Keep neck work light (1 x 15s)") is never applied to every exercise, and a
change that would give a dose below one is not applied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_COUNT = r"(\d+|one|two|three|a|an)"
_WORDS = {"one": 1, "a": 1, "an": 1, "two": 2, "three": 3}
_FEWER = r"(?:cut|drop|remove|reduce|subtract|minus|take off|lose)"
_MORE = r"(?:add|increase|plus)"

_SETS_DOWN = [re.compile(rf"\b{_FEWER}\s+(?:by\s+)?{_COUNT}\s+sets?\b", re.I),
              re.compile(rf"\b{_COUNT}\s+sets?\s+(?:fewer|less)\b", re.I)]
_SETS_UP = [re.compile(rf"\b{_MORE}\s+(?:by\s+)?{_COUNT}\s+(?:extra\s+|more\s+)?sets?\b", re.I),
            re.compile(rf"\b{_COUNT}\s+(?:extra|more)\s+sets?\b", re.I)]
# "to 5 rounds", "at 6 rounds", "Do 3 sharp rounds": the new total is written.
_ROUNDS_TO = re.compile(r"\b(?:to|at|do|complete|for)\s+(\d+)\s+(?:[a-z]+\s+){0,2}rounds?\b", re.I)


def _count(word: str) -> int:
    return int(word) if word.isdigit() else _WORDS[word.lower()]


def _one(patterns, text: str) -> int | None:
    found = {_count(m.group(1)) for p in patterns for m in p.finditer(text)}
    return found.pop() if len(found) == 1 else None


@dataclass(frozen=True)
class Progression:
    sets_delta: int | None = None
    rounds: int | None = None

    def __bool__(self) -> bool:
        return self.sets_delta is not None or self.rounds is not None


def read_progression(change: str | None) -> Progression:
    """The exact dose changes written in ``change``; ambiguous or conflicting ones are left out."""
    if not change:
        return Progression()
    down, up = _one(_SETS_DOWN, change), _one(_SETS_UP, change)
    delta = None if (down and up) else (-down if down else up)
    rounds = {int(m.group(1)) for m in _ROUNDS_TO.finditer(change)}
    return Progression(delta, rounds.pop() if len(rounds) == 1 else None)


def apply_progression(exercise, progression: Progression) -> list[str]:
    """Apply ``progression`` to a copied exercise in place; returns what changed, as text."""
    applied = []
    if progression.sets_delta and exercise.sets:
        new = exercise.sets + progression.sets_delta
        if new >= 1:
            applied.append(f"sets {exercise.sets} → {new}")
            exercise.sets = new
    if progression.rounds and exercise.rounds and exercise.rounds != progression.rounds:
        applied.append(f"rounds {exercise.rounds} → {progression.rounds}")
        exercise.rounds = progression.rounds
    return applied
