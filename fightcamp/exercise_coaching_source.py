"""Session-scoped projection and safety checks for final AI coaching text.

This module only reads or rejects authored lines. It never creates instructions
or changes programme prescriptions.
"""
from __future__ import annotations

import re

from .calendar_context import role_d_day
from .coaching_copy import is_null_progression
from .exercise_coaching import easier_rejection, clinical_coaching_rejection
from .exercise_progression import build_progression_context, occurrence_key, progress_rejection
from .structured_session_identity import identity_tokens
from .mental_training_policy import mental_category

# Day headers: "D-27 (Tuesday) — Strength". The finalizer prompt forbids long
# dashes in athlete text, so a compliant model writes "D-27 (Tuesday): Strength"
# instead; both name the same appointment and must parse identically.
_DAY = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:D-(\d+)\s*\([^)]*\)|[^\n]*?\(D-(\d+)\))(?:\*\*)?"
                  r"\s*(?:[—–:|]|\s-\s|-)\s*(?:\*\*)?(.+?)(?:\*\*)?\s*$", re.I)
_EXERCISE = re.compile(r"^\s*[-*•]\s+(.+?)\s*(?:\s[—–]\s|:\s)(.+)$")
_LABEL = re.compile(r"^\s*(?:\*\*)?(How|Cue|Coaching cues|Easier|Regression|Progress|Progression|Stop|Stop rule|"
                    r"Swap|Swaps|Substitution|Substitutions|Purpose)(?:\*\*)?\s*:\s*(?:\*\*)?\s*(.+)$", re.I)
_FIELDS = {"how": "execution_instructions", "cue": "coaching_cues", "coaching cues": "coaching_cues",
           "easier": "regression_options", "regression": "regression_options",
           "progress": "progression_rule", "progression": "progression_rule",
           "stop": "stop_rules", "stop rule": "stop_rules",
           "swap": "substitutions", "swaps": "substitutions", "substitution": "substitutions",
           "substitutions": "substitutions",
           "purpose": "purpose"}
_LISTS = {"coaching_cues", "regression_options", "stop_rules", "substitutions"}


def coaching_identity(value):
    # A trailing plural ("Kettlebell Swings") names the same exercise.
    return tuple(sorted({re.sub(r"(?<=[a-z]{3})s$", "", token) for token in identity_tokens(value)}))


def lookup_occurrence(index: dict, key: tuple, peers):
    """The one indexed value for an exact day/session/exercise occurrence.

    A retitled session ("Strength" -> "Power and Trunk Strength") still names
    the same appointment when its exercise occurs exactly once on that day in
    both the source and the index. Never crosses a day; never guesses between
    two same-day sessions holding the same exercise.
    """
    if key in index:
        # Two identical occurrences on the other side cannot both own one block.
        return index[key] if sum(1 for peer in peers if peer == key) <= 1 else None
    day, _, name = key
    if day is None or not name:
        return None
    same = [k for k in index if k[0] == day and k[2] == name]
    if len(same) != 1 or same[0] in peers:
        return None  # Ambiguous, or that exact key belongs to another session.
    if sum(1 for k in peers if k[0] == day and k[2] == name) != 1:
        return None
    return index[same[0]]


def coaching_occurrences(text: str):
    """Yield exact day/session/exercise occurrences with labelled source lines."""
    day = title = None
    occurrence = None
    for line in str(text or "").splitlines():
        if header := _DAY.match(line):
            if occurrence:
                yield occurrence
            day, title = int(header[1] or header[2]), coaching_identity(header[3])
            occurrence = None
        elif re.match(r"^\s*#{1,6}\s", line):
            if occurrence:
                yield occurrence
            day = title = occurrence = None
        elif exercise := _EXERCISE.match(line):
            if occurrence:
                yield occurrence
            occurrence = {"key": (day, title, coaching_identity(exercise[1])), "fields": {}, "lines": []}
        elif occurrence and (label := _LABEL.match(line)):
            field = _FIELDS[label[1].lower()]
            if field == "progression_rule" and is_null_progression(label[2]):
                continue  # A stated absence is not a Progress instruction.
            if field in _LISTS:
                occurrence["fields"].setdefault(field, []).append(label[2])
            else:
                occurrence["fields"][field] = label[2]
            occurrence["lines"].append(line)
    if occurrence:
        yield occurrence


def source_coaching(text: str) -> dict:
    groups = {}
    for item in coaching_occurrences(text):
        groups.setdefault(item["key"], []).append(item)
    return {key: values[0] for key, values in groups.items() if len(values) == 1}


def validate_source_coaching(brief: dict, text: str) -> tuple[str, list[dict]]:
    """Reject unsafe source lines before any structured or markdown fallback.

    Missing instructions remain missing. Reports name rejected fields/reasons,
    without replacing the AI's coaching or adjusting the planned dose.
    """
    context = build_progression_context(brief)
    selected = {}
    nonphysical = set()
    for week in (brief.get("weekly_role_map") or {}).get("weeks") or []:
        for role in week.get("session_roles") or []:
            day = role_d_day(week, role)
            if mental_category(role):
                nonphysical.add((day, coaching_identity(role.get("athlete_facing_label"))))
            for assignment in role.get("selected_exercise_assignments") or []:
                key = day, coaching_identity(role.get("athlete_facing_label")), coaching_identity(assignment.get("name"))
                selected.setdefault(key, []).append((role, assignment, context.get(occurrence_key(role, assignment, day))))
            if not role.get("selected_exercise_assignments") and not mental_category(role):
                # Older recovery roles carry their selected movement/dose as
                # display_text. It is existing prescription evidence, not a new
                # exercise choice or permission for numerical advancement.
                for raw in str(role.get("display_text") or "").splitlines():
                    match = _EXERCISE.match(raw) or _EXERCISE.match("- " + raw)
                    if match:
                        assignment = {"name": match[1], "effective_prescription": match[2]}
                        key = day, coaching_identity(role.get("athlete_facing_label")), coaching_identity(match[1])
                        selected.setdefault(key, []).append((role, assignment, None))
    # Direct late-fight briefs keep their sessions in a flat countdown sequence.
    for role in brief.get("late_fight_session_sequence") or []:
        day = role_d_day({}, role) if isinstance(role, dict) else None
        if isinstance(day, int) and mental_category(role):
            nonphysical.add((day, coaching_identity(role.get("athlete_facing_label"))))
    athlete = brief.get("athlete_snapshot") or {}
    peers = [item["key"] for item in coaching_occurrences(text)]
    day = title = name = None
    result, findings = [], []
    for line in str(text or "").splitlines():
        if header := _DAY.match(line):
            day, title, name = int(header[1] or header[2]), coaching_identity(header[3]), None
        elif re.match(r"^\s*#{1,6}\s", line):
            day = title = name = None
        elif exercise := _EXERCISE.match(line):
            name = coaching_identity(exercise[1])
        label = _LABEL.match(line)
        if label and (day, title) not in nonphysical and _FIELDS[label[1].lower()] in {"progression_rule", "regression_options"}:
            candidates = lookup_occurrence(selected, (day, title, name), peers) or []
            role, assignment, current = candidates[0] if len(candidates) == 1 else ({}, {}, None)
            field = _FIELDS[label[1].lower()]
            reason = (progress_rejection(label[2], current) if field == "progression_rule" else
                      easier_rejection(label[2], assignment, role, athlete, brief=brief))
            reason = reason or clinical_coaching_rejection(label[2], brief)
            if reason:
                findings.append({"code": "exercise_coaching_rejected", "field": field,
                                 "scheduled_d_day": day, "reason": reason})
                continue
        result.append(line)
    validated_text = text if len(result) == len(str(text or "").splitlines()) else "\n".join(result)
    for item in coaching_occurrences(validated_text):
        if lookup_occurrence(selected, item["key"], peers) and not item["fields"].get("regression_options"):
            findings.append({"code": "exercise_coaching_missing", "field": "regression_options",
                             "scheduled_d_day": item["key"][0], "reason": "missing_ai_authored_easier"})
    return validated_text, findings
