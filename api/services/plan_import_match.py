"""Match imported exercises to the app's exercise library.

An exercise the coach wrote that is *the same exercise* as a library one is
linked to it, so its demo video, cues and history come with it straight away.
Only a safe match links: a wrong demo video is worse than none.

  * ``same``  - the same name, or the same words in another order / plural
                ("Goblet Squats" = "goblet squat"). Linked quietly.
  * ``close`` - at least 95% alike in spelling and carrying the same numbers.
                Linked, and flagged so the person confirms it.
  * anything else, or two library exercises equally good: left as written.

Numbers must agree: "Front Squat Isometric (105% 1RM)" is never "(115% 1RM)".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Mapping

from api.contracts.custom_plans import CustomExerciseSnapshot
from api.services.training_library import preview, text_key

CLOSE_RATIO = 0.95
_FILLER = {"the", "a", "an", "of", "with", "and"}


def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def _words(name: str) -> tuple[str, ...]:
    return tuple(sorted(_singular(w) for w in text_key(name).split() if w not in _FILLER))


def _numbers(name: str) -> tuple[str, ...]:
    return tuple(sorted(re.findall(r"\d+", name)))


@dataclass(frozen=True)
class Match:
    record: dict
    kind: str  # "same" | "close"

    @property
    def catalogue_id(self) -> str:
        return self.record["summary"]["catalogue_id"]

    @property
    def name(self) -> str:
        return self.record["entry"]["name"]


class LibraryMatcher:
    def __init__(self, records, media: Mapping[str, Any] | None = None):
        self.media = media or {}
        self.by_text: dict[str, list[dict]] = {}
        self.by_words: dict[tuple[str, ...], list[dict]] = {}
        self.names: list[tuple[str, dict]] = []
        for record in records:
            if record["summary"]["category"] != "training":
                continue
            name = record["entry"]["name"]
            self.by_text.setdefault(text_key(name), []).append(record)
            self.by_words.setdefault(_words(name), []).append(record)
            self.names.append((text_key(name), record))

    def _one(self, candidates: list[dict]) -> dict | None:
        """The single unambiguous record; a duplicate name resolves only when exactly one has a video."""
        unique = {r["summary"]["catalogue_id"]: r for r in candidates}
        if len(unique) == 1:
            return next(iter(unique.values()))
        with_video = [r for r in unique.values() if self.media.get(r["entry"]["exercise_key"])]
        return with_video[0] if len(with_video) == 1 else None

    def match(self, name: str) -> Match | None:
        key = text_key(name)
        if not key:
            return None
        for table, lookup in ((self.by_text, key), (self.by_words, _words(name))):
            found = table.get(lookup)
            if found:
                record = self._one(found)
                return Match(record, "same") if record else None
        best: list[tuple[float, dict]] = []
        for other, record in self.names:
            if abs(len(other) - len(key)) > max(2, len(key) // 10):
                continue
            ratio = SequenceMatcher(None, key, other).ratio()
            if ratio >= CLOSE_RATIO and _numbers(name) == _numbers(record["entry"]["name"]):
                best.append((ratio, record))
        if not best:
            return None
        best.sort(key=lambda pair: -pair[0])
        if len({r["summary"]["catalogue_id"] for _, r in best}) > 1:
            return None
        return Match(best[0][1], "close")

    def snapshot(self, match: Match) -> dict:
        data = preview(match.record)
        data["media"] = self.media.get(match.record["entry"]["exercise_key"])
        return CustomExerciseSnapshot.model_validate(data).model_dump(mode="json")


def linked(built: dict, match: Match) -> dict | None:
    """The library form of an exercise built as written. Everything the coach
    wrote stays: the dose as is, and the notes, equipment and cues as cue lines.
    None when it wouldn't fit the library form (then it stays as written)."""
    cues = list(built.get("coaching_cues") or [])
    extra = [line for line in (built.get("notes") or "").split("\n") if line.strip()]
    if built.get("equipment"):
        extra.append(f"Equipment: {built['equipment']}")
    cues += extra
    if len(cues) > 10 or any(len(line) > 500 for line in cues):
        return None
    return {"id": built["id"], "origin": "catalogue", "catalogue_id": match.catalogue_id, "dose": built["dose"],
            "coaching_cues": cues or None, "stop_rules": built.get("stop_rules") or None}
