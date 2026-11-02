"""One canonical structured plan built from deterministic planner state alone.

When Stage 2 fails there is no valid ``structured_plan`` column, and the athlete
client would otherwise rebuild its calendar from ``plan_text`` (see
``web/lib/plan-text-adapter.ts``). That hands the model authority over whether a
declared sparring day, a declared light-combat day or a Tactical Watch *exists* —
on the one path where the model has already proven unreliable.

This module builds the fallback from Stage 1's own resolved state instead:
selected session roles, their selected exercise assignments and authoritative
effective prescriptions. It owns no calendar and no classification of its own —
each kind of locked content is assembled by the module that already owns it:

    role sessions        -> here, from weekly_role_map.session_roles
    declared contact     -> reconcile_coach_led_sparring_days
    Tactical Watch       -> merge_locked_structured_content
    calendar continuity  -> reconcile_calendar_spine

Roles owned by one of those assemblers are deliberately NOT turned into sessions
here, so a day never carries two representations of the same role.

Identity is ``(d_day, role_key, session_index)``: distinct roles that share a day
(conditioning + light combat + Tactical Watch on one Wednesday) all survive.
Deterministic ownership protects the individual role, never the whole day —
existing collision rules still decide what may share a day, and this module
reads their verdict rather than re-deciding it.
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from fightcamp.coaching_copy import is_null_progression
from fightcamp.role_labels import athlete_facing_label_for
from fightcamp.session_sequencing import sequence_structured_plan

from fightcamp.exercise_identity import support_insert_exercise_key

from .exercise_identity import block_exercise_key_for_name, normalize_exercise_key
from .structured_plan_calendar_spine import (
    ROLES_OWNED_ELSEWHERE,
    reconcile_calendar_spine,
    reconcile_priority_microdose_representations,
)
from .structured_plan_faithfulness import (
    _day_header_dday,
    _source_block_segment,
    _source_day_section_lines,
)
from .structured_plan_locked_merge import merge_planner_owned_structured_content

logger = logging.getLogger(__name__)

# Roles another deterministic assembler already renders. Building sessions for
# them here would put the same role on the day twice. Defined with the calendar
# spine, which applies the same exclusion when it restores a dropped scheduled
# role, so the two paths can never drift apart.
_ROLES_OWNED_ELSEWHERE = ROLES_OWNED_ELSEWHERE

_SESSION_TYPE_BY_CATEGORY = {
    "strength": "strength_power",
    "conditioning": "conditioning",
    "recovery": "recovery",
    "rehab": "rehab",
    "technical": "skill",
    "skill": "skill",
    "sparring": "sparring",
    "mindset": "skill",
    "support_insert": "recovery",
}
_BLOCK_TYPE_BY_CATEGORY = {
    "strength": "strength",
    "conditioning": "conditioning",
    "recovery": "cooldown_recovery",
    "rehab": "rehab",
    "technical": "skill",
    "skill": "skill",
    "sparring": "sparring",
    "mindset": "mindset",
    "support_insert": "mobility_activation",
}

# A support insert is LOW-COST work, not recovery work. Mapping the whole
# category to ``recovery`` published a technical footwork drill and a joint-prep
# reset to the athlete under a RECOVERY tag. The planner already carries the
# real semantics on the role (``support_insert_category`` from
# fightcamp/gap_fill_inserts._INSERT_META, with ``support_insert_cost_category``
# behind it), so read those rather than guessing from the title.
#
# Every value is an existing canonical SessionType: this schema is also the
# strict Stage 2 response schema, so a new value would change what the model is
# required to emit. "rehab" is the schema's home for mobility/movement-quality
# work — the web renderer already groups rehab/prehab/mobility together and the
# per-region policy renames it "Prehab" when nothing is injured. Tactical and
# mental inserts are skill work that happens to carry no physical load; their
# zero-load status is decided by the shared identity rule (#2570), never by the
# session type.
_SESSION_TYPE_BY_SUPPORT_CATEGORY = {
    "technical": "skill",
    "technical_footwork": "skill",
    "footwork": "skill",
    "coordination": "skill",
    "tactical": "skill",
    "mental": "skill",
    "conditioning_maintenance": "conditioning",
    "low_cost_aerobic": "conditioning",
    "recovery": "recovery",
    "recovery_walk": "recovery",
    "low_cost_recovery": "recovery",
    "mobility": "rehab",
    "movement_quality": "rehab",
    # Cost categories are the fallback vocabulary for an older or partial role
    # that carries no `support_insert_category`. Every value gap_fill_inserts
    # ._cost_category can return is mapped, so such a role can never fall
    # through to the legacy support_insert -> "recovery" default: "physical"
    # says the work moves the athlete without saying which quality it trains
    # ("mixed" is the schema's honest unspecified-physical value), and
    # "zero_cost" is only ever tactical or mental work.
    "physical": "mixed",
    "zero_cost": "skill",
}
_BLOCK_TYPE_BY_SUPPORT_CATEGORY = {
    "technical": "skill",
    "technical_footwork": "skill",
    "footwork": "skill",
    "coordination": "skill",
    "tactical": "mindset",
    "mental": "mindset",
    "conditioning_maintenance": "conditioning",
    "low_cost_aerobic": "conditioning",
    "recovery": "cooldown_recovery",
    "recovery_walk": "cooldown_recovery",
    "low_cost_recovery": "cooldown_recovery",
    "mobility": "mobility_activation",
    "movement_quality": "mobility_activation",
    "physical": "accessory",
    "zero_cost": "mindset",
}


def _support_semantics(role: dict[str, Any], table: dict[str, str]) -> str | None:
    """Look ``role`` up in ``table`` by category, then by cost category.

    A role whose ``support_insert_category`` is unknown to the table (a new
    planner category this module has not learned yet) still resolves through its
    cost category rather than silently falling back to recovery.
    """
    for key in ("support_insert_category", "support_insert_cost_category"):
        value = str(role.get(key) or "").strip().lower()
        if value and value in table:
            return table[value]
    return None


def _session_type(role: dict[str, Any]) -> str:
    category = _category(role)
    if category == "support_insert":
        support = _support_semantics(role, _SESSION_TYPE_BY_SUPPORT_CATEGORY)
        if support:
            return support
        # A support insert with no usable planner semantics at all is still not
        # recovery work; "mixed" claims only that it is a session.
        return "mixed"
    return _SESSION_TYPE_BY_CATEGORY.get(category, "mixed")


def _block_type(role: dict[str, Any]) -> str:
    category = _category(role)
    if category == "support_insert":
        support = _support_semantics(role, _BLOCK_TYPE_BY_SUPPORT_CATEGORY)
        if support:
            return support
    return _BLOCK_TYPE_BY_CATEGORY.get(category, "accessory")


def _dday(role: dict[str, Any]) -> int | None:
    raw = role.get("scheduled_d_day")
    if isinstance(raw, int) and raw >= 0:
        return raw
    for key in ("scheduled_countdown_label", "countdown_label"):
        label = str(role.get(key) or "").strip().upper()
        if label.startswith("D-"):
            try:
                return int(label[2:])
            except ValueError:
                continue
    return None


def _category(role: dict[str, Any]) -> str:
    return str(role.get("category") or "").strip().lower()


def _effective_prescription(role: dict[str, Any], assignment: dict[str, Any]) -> str:
    """The planner's authoritative dose for one assignment.

    ``effective_strength_prescriptions`` is the resolved authority (it carries any
    late-camp dose cap); the assignment's own effective dose, then its bank
    dose, are the fallbacks.
    """
    slot_id = str(assignment.get("slot_id") or "").strip()
    name = str(assignment.get("name") or "").strip()
    for resolved in role.get("effective_strength_prescriptions") or []:
        if not isinstance(resolved, dict):
            continue
        if (slot_id and str(resolved.get("slot_id") or "").strip() == slot_id) or (
            name and str(resolved.get("name") or "").strip() == name
        ):
            prescription = str(
                resolved.get("effective_prescription")
                or resolved.get("base_prescription")
                or ""
            ).strip()
            if prescription:
                return prescription
    return str(
        assignment.get("effective_prescription")
        or assignment.get("base_prescription")
        or ""
    ).strip()


# ---------------------------------------------------------------------------
# Exact working doses.
#
# Stage 1 keeps a bank dose as authorised bounds ("20-30min continuous") and
# Stage 2 is required to prescribe one exact value inside them (the validator's
# ``ambiguous_working_dose`` rule). This fallback only exists when Stage 2 did
# not produce a usable card, so the value the athlete was actually given lives
# in the plan text. That text is read first; the bounds are only resolved here
# when the text carries no line for the exercise, and then by the same rule the
# athlete copy follows: workload and intensity take the lower bound, rest takes
# the upper bound.
# ---------------------------------------------------------------------------

# A bank's "4–6x2–5" shorthand glues the reps range to its multiplier, so an
# "x" directly before the number still starts a range.
_DOSE_RANGE_RE = re.compile(
    r"(?:(?<=[x×X])|(?<![\w.]))"
    r"(?P<low>\d+(?:\.\d+)?)\s*[-–—]\s*(?P<high>\d+(?:\.\d+)?)(?![\d.])"
)
_DOSE_UNIT_AFTER_RE = re.compile(
    r"^\s*(?:[x×]|/\s*10\b|%|sets?\b|reps?\b|rounds?\b|holds?\b|bursts?\b|"
    r"kg\b|kgs\b|lbs?\b|s\b|sec\b|secs\b|seconds?\b|min\b|mins\b|minutes?\b|m\b)",
    re.IGNORECASE,
)
_DOSE_LABEL_BEFORE_RE = re.compile(
    r"\b(?:RPE|RIR|sets?|reps?|rounds?|work|rest|recovery|reset|duration|load)\s*[:=]?\s*$",
    re.IGNORECASE,
)
_REST_BEFORE_RE = re.compile(r"\b(?:rest|recovery|reset|RIR)\s*[:=]?\s*$", re.IGNORECASE)
_REST_AFTER_RE = re.compile(
    r"^\s*(?:s|sec|secs|seconds?|min|mins|minutes?)\s+(?:rest|recovery|reset)\b",
    re.IGNORECASE,
)


def _exact_working_dose(prescription: str) -> str:
    """One exact value for every unit-bearing range in a planner dose.

    Numbers that are not a dose (a ``D-21 to D-8`` window, a name) are left as
    written: a range is only resolved when a dose unit follows it or a dose
    label precedes it.
    """
    source = str(prescription or "")

    def choose(match: re.Match[str]) -> str:
        head = source[max(0, match.start() - 28) : match.start()]
        tail = source[match.end() :]
        if not (_DOSE_UNIT_AFTER_RE.match(tail) or _DOSE_LABEL_BEFORE_RE.search(head)):
            return match.group(0)
        is_rest = bool(_REST_BEFORE_RE.search(head) or _REST_AFTER_RE.match(tail))
        return match.group("high" if is_rest else "low")

    return _DOSE_RANGE_RE.sub(choose, source)


# ---------------------------------------------------------------------------
# The plan text's own line for a selected exercise.
#
# Stage 2 writes each exercise in the planner's session-body grammar:
#
#     Why: <rationale>
#     - <Exercise>: <clause>; <clause>; ...
#       Cue: ... / Purpose: ... / Easier: ... / Stop: ...
#
# The exercise is matched by its exact title inside its own D-day only, so a
# same-named drill on another day never lends this block its dose.
# ---------------------------------------------------------------------------

_UNIT_NAMES = {
    "s": "seconds",
    "sec": "seconds",
    "secs": "seconds",
    "second": "seconds",
    "seconds": "seconds",
    "min": "minutes",
    "mins": "minutes",
    "minute": "minutes",
    "minutes": "minutes",
}
_TIME_UNIT = r"(?P<unit>s|secs?|seconds?|mins?|minutes?)"
_NUMBER = r"(?P<value>\d+(?:\.\d+)?)"
_DURATION_CLAUSE_RE = re.compile(rf"(?:duration\s*:?\s*)?{_NUMBER}\s*{_TIME_UNIT}", re.IGNORECASE)
_REST_CLAUSE_RES = (
    re.compile(rf"(?:full\s+)?rest\s*:?\s*{_NUMBER}\s*{_TIME_UNIT}", re.IGNORECASE),
    re.compile(rf"{_NUMBER}\s*{_TIME_UNIT}\s+rest", re.IGNORECASE),
)
_EFFORT_CLAUSE_RE = re.compile(
    r"(?P<method>RPE|RIR)\s*(?P<value>\d+(?:\.\d+)?(?:\s*[-–—]\s*\d+(?:\.\d+)?)?)",
    re.IGNORECASE,
)
_SETS_REPS_CLAUSE_RE = re.compile(
    r"(?P<sets>\d+)\s*(?:sets?\s*)?[x×]\s*(?P<reps>\d+)\s*(?:reps?)?", re.IGNORECASE
)
_SETS_TIME_CLAUSE_RE = re.compile(
    rf"(?P<sets>\d+)\s*(?:sets?|holds?)\s*[x×]\s*{_NUMBER}\s*{_TIME_UNIT}", re.IGNORECASE
)


def _number(text: str) -> int | float:
    value = float(text)
    return int(value) if value.is_integer() else value


def _measured(match: re.Match[str]) -> dict[str, Any]:
    return {
        "value": _number(match.group("value")),
        "unit": _UNIT_NAMES[match.group("unit").lower()],
    }


# A cluster block names its movements in order ("Heavy RDL → Broad Jump").
_CLUSTER_NAME_SEPARATOR_RE = re.compile(r"\s*(?:→|->)\s*")
_ROUNDS_RE = re.compile(r"\b(\d+)\s*rounds?\b", re.IGNORECASE)
_COMPONENT_REPS_RE = re.compile(r"\b(\d+)\s*reps?\b", re.IGNORECASE)
_COMPONENT_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*1rm\b", re.IGNORECASE)
_COMPONENT_ABSOLUTE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|lbs?)\b", re.IGNORECASE)
_COMPONENT_INTENT_RE = re.compile(r"\bmax(?:imal)?\s+(intent|speed|height|velocity|effort)\b", re.IGNORECASE)
_FULL_REST_RE = re.compile(r"\bfull\s+rest\b(?:\s+between\s+[a-z ]+?)?(?=\s*(?:[;,.]|$))", re.IGNORECASE)
_EFFORT_OUT_OF_TEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*10\s*effort\b", re.IGNORECASE)


def _percent_load(match: re.Match[str]) -> dict[str, Any]:
    value = _number(match[1])
    return {"method": "percentage", "value": value, "unit": "percent", "ref": "1RM",
            "display": f"{value}% 1RM"}


def _absolute_load(match: re.Match[str]) -> dict[str, Any]:
    value = _number(match[1])
    unit = "lb" if match[2].lower().startswith("lb") else "kg"
    return {"method": "absolute", "value": value, "unit": unit, "display": f"{value} {unit}"}


def _cluster_fields(name: str | None, dose: str) -> dict[str, Any]:
    """Rounds and per-movement components for a two-movement cluster block.

    Reads both grammars that carry a contrast pair: the planner's
    "3 rounds: heavy movement 2 reps @ 85% 1RM → explosive movement 2 reps at
    max intent; rest 4 min per round." and the plan text's "3 rounds; RDL 2 reps
    @ 85% 1RM, then Broad Jump 2 reps; rest 4 min between rounds.". Each
    component takes the reps, load and intent written in its own span of the
    dose. Anything ambiguous (not exactly two named movements, not exactly two
    rep counts, no round count) lifts nothing, so a card never guesses.
    """
    parts = [part.strip() for part in _CLUSTER_NAME_SEPARATOR_RE.split(str(name or "")) if part.strip()]
    if len(parts) != 2:
        return {}
    rounds = _ROUNDS_RE.search(dose)
    reps = list(_COMPONENT_REPS_RE.finditer(dose))
    if rounds is None or len(reps) != 2:
        return {}
    clause_end = dose.find(";", reps[1].end())
    spans = (dose[reps[0].start():reps[1].start()],
             dose[reps[1].start():clause_end if clause_end >= 0 else len(dose)])
    components: list[dict[str, Any]] = []
    for component_name, rep, span in zip(parts, reps, spans, strict=True):
        component: dict[str, Any] = {"name": component_name, "reps": int(rep[1])}
        if percent := _COMPONENT_PERCENT_RE.search(span):
            component["load"] = _percent_load(percent)
        elif absolute := _COMPONENT_ABSOLUTE_RE.search(span):
            component["load"] = _absolute_load(absolute)
        if rpe := _EFFORT_CLAUSE_RE.search(span):
            component["effort"] = {"method": rpe.group("method").upper(),
                                   "value": _number(rpe.group("value")) if re.fullmatch(r"[\d.]+", rpe.group("value")) else rpe.group("value"),
                                   "scale": "1-10" if rpe.group("method").upper() == "RPE" else None}
        elif intent := _COMPONENT_INTENT_RE.search(span):
            quality = intent[1].lower()
            component["effort"] = {"method": "intent", "value": "max" if quality == "intent" else f"max {quality}"}
        components.append(component)
    return {"rounds": int(rounds[1]), "components": components}


def _lift_dose_fields(dose: str, name: str | None = None) -> tuple[dict[str, Any], str]:
    """Structured fields for the whole clauses the card renders as stats.

    Only a clause that is *entirely* one quantity is lifted ("duration 20 min",
    "rest 120 sec", "RPE 6", "2 sets x 2 reps"); every other clause keeps the
    text's own wording for execution instructions. A value the
    text wrote is never changed. "3 holds x 10 sec" is timed work: its count
    becomes ``sets`` and its time ``duration``.
    """
    fields: dict[str, Any] = {}
    # The deterministic programme uses compact lifting and interval notation.
    # Lift its exact numbers without discarding the accompanying intent/cues.
    reps = re.match(r"\s*(\d+)\s*(?:sets?\s*)?[x×]\s*(\d+)\b(?!\s*(?:s|secs?|seconds?|mins?|minutes?)\b)", dose, re.I)
    # Final text also spells intervals as "3 rounds of 60s work". Keep the
    # round count and per-round work separate, just as for "3 rounds x 60s".
    timed = re.match(
        r"\s*(\d+)\s*(?:rounds?\s+of\s+|(?:sets?|holds?|rounds?)?\s*[x×]\s*)"
        + _NUMBER + r"\s*" + _TIME_UNIT + r"\b", dose, re.I,
    )
    interval = re.match(r"\s*(\d+(?:\.\d+)?)\s*(s|secs?|seconds?|mins?|minutes?)\s+(?:work|effort)\s*[,/]\s*"
                        r"(\d+(?:\.\d+)?)\s*(s|secs?|seconds?|mins?|minutes?)\s+rest\s*[x×]\s*(\d+)\s+rounds?", dose, re.I)
    if reps:
        fields.update(sets=int(reps[1]), reps=int(reps[2]))
    elif timed:
        if re.search(r"\brounds?\b", dose, re.I):
            fields.update(rounds=int(timed[1]), work=_measured(timed))
        else:
            fields.update(sets=int(timed[1]), duration=_measured(timed))
    elif interval:
        fields.update(rounds=int(interval[5]),
                      work={"value": _number(interval[1]), "unit": _UNIT_NAMES[interval[2].lower()]},
                      rest={"value": _number(interval[3]), "unit": _UNIT_NAMES[interval[4].lower()]})
    elif lead := re.match(r"\s*" + _NUMBER + r"\s*" + _TIME_UNIT + r"\b(?!\s*(?:rest|recovery|between|[x×]))", dose, re.I):
        # One continuous bout: "30min continuous", "6 minutes, RPE 4 max."
        fields["duration"] = _measured(lead)
    kept: list[str] = []
    for raw_clause in re.split(r"\s*;\s*", dose.strip().rstrip(".").strip()):
        clause = raw_clause.strip().rstrip(".").strip()
        if not clause:
            continue
        rest = next(
            (m for pattern in _REST_CLAUSE_RES if (m := pattern.fullmatch(clause))), None
        )
        if rest:
            fields["rest"] = _measured(rest)
            continue
        if match := _DURATION_CLAUSE_RE.fullmatch(clause):
            fields["duration"] = _measured(match)
            continue
        if "effort" not in fields and (match := _EFFORT_CLAUSE_RE.fullmatch(clause)):
            raw_value = re.sub(r"\s*[-–—]\s*", "-", match.group("value"))
            fields["effort"] = {
                "method": match.group("method").upper(),
                "value": _number(raw_value) if "-" not in raw_value else raw_value,
                "scale": "1-10" if match.group("method").upper() == "RPE" else None,
            }
            continue
        if match := _SETS_REPS_CLAUSE_RE.fullmatch(clause):
            fields["sets"] = int(match.group("sets"))
            fields["reps"] = int(match.group("reps"))
            continue
        if (
            match := _SETS_TIME_CLAUSE_RE.fullmatch(clause)
        ):
            fields["sets"] = int(match.group("sets"))
            fields["duration"] = _measured(match)
            continue
        kept.append(clause)
    if "rest" not in fields:
        # Compact planner notation writes rest as a sub-clause of the work:
        # "6x3 reps, 2min rest", "1x5s with 120s rest". Lift it only when the
        # dose states exactly one rest, so the card never guesses between two.
        rests = [match for pattern in _REST_CLAUSE_RES for match in pattern.finditer(dose)]
        if len(rests) == 1 and len(re.findall(r"\brest\b", dose, re.IGNORECASE)) == 1:
            fields["rest"] = _measured(rests[0])
    if "rest" not in fields and not fields.get("rest_note"):
        # Preserve an explicit rest range as text; never lose it because the
        # scalar schema cannot express it. Recovery instructions stay intact.
        ranged_rest = re.search(
            r"\brest\s*:?\s*\d+(?:\.\d+)?\s*[-–—]\s*\d+(?:\.\d+)?\s*(?:seconds?|secs?|s|minutes?|mins?)\b"
            r"|\b\d+(?:\.\d+)?\s*[-–—]\s*\d+(?:\.\d+)?\s*(?:seconds?|secs?|s|minutes?|mins?)\s+rest\b",
            dose, re.I,
        )
        if ranged_rest and len(re.findall(r"\brest\b", dose, re.I)) == 1:
            fields["rest_note"] = ranged_rest[0]
    if "effort" not in fields and (effort := _EFFORT_CLAUSE_RE.search(dose)):
        value = re.sub(r"\s*[-–—]\s*", "-", effort.group("value"))
        fields["effort"] = {"method": effort.group("method").upper(),
                            "value": _number(value) if "-" not in value else value,
                            "scale": "1-10" if effort.group("method").upper() == "RPE" else None}
    carry = re.match(r"\s*(\d+)\s+carries\s+over\s+(\d+(?:\.\d+)?)\s*m\b", dose, re.I)
    if carry:
        fields.update(sets=int(carry[1]), distance={"value": _number(carry[2]), "unit": "meters"})
    if "effort" not in fields and (out_of_ten := _EFFORT_OUT_OF_TEN_RE.search(dose)):
        # "@ 7/10 effort" is the planner's spelling of RPE 7.
        fields["effort"] = {"method": "RPE", "value": _number(out_of_ten[1]), "scale": "1-10"}
    if "rest" not in fields and (full_rest := _FULL_REST_RE.search(dose)):
        # Qualitative rest stays words; it is never turned into a number.
        note = re.sub(r"\s+", " ", full_rest.group(0)).strip()
        fields["rest_note"] = note[:1].upper() + note[1:]
    if percent := _COMPONENT_PERCENT_RE.search(dose):
        fields["load"] = _percent_load(percent)
    if absolute := _COMPONENT_ABSOLUTE_RE.search(dose):
        fields["load"] = _absolute_load(absolute)
    fields.update(_cluster_fields(name, dose))
    return fields, "; ".join(kept)


def _source_title_re(name: str) -> re.Pattern[str] | None:
    """The exercise title as ``_source_block_segment`` matches it, dose excluded.

    Also accepts the title as one option of a source choice ("Short sprint
    bounds or low box jumps — ..."), which that helper falls back to.
    """
    tokens = re.findall(r"[a-z0-9]+", str(name or "").casefold())
    if not tokens:
        return None
    # Closing brackets end a parenthetical name ("Pivot-and-Strike (Rotation
    # Focus)"), so they may stand between its last word and the delimiter.
    title = r"[\s\W]+".join(re.escape(token) for token in tokens) + r"[)\]]*"
    return re.compile(
        rf"^\s*(?:[-*•]\s*)?{title}(?:\s+or\s+[^.:,—–]+)?\s*(?:[.:,—–]|\s-\s)", re.IGNORECASE
    )


def _source_session_why(section_lines: list[str] | None, names: list[str]) -> str:
    """The text's ``Why:`` for the session holding the first of ``names``.

    Only the lines between that exercise and its own day header are read, so a
    second session on the same D-day never lends this one its rationale.
    """
    if not section_lines:
        return ""
    for name in names:
        title_re = _source_title_re(name)
        if title_re is None:
            continue
        index = next(
            (i for i, line in enumerate(section_lines) if title_re.match(line)), None
        )
        if index is None:
            continue
        for previous in reversed(section_lines[:index]):
            why = _WHY_LINE_RE.match(previous)
            if why:
                return why.group("text").strip()
            if _day_header_dday(previous) is not None:
                break
        return ""
    return ""


def _source_block(
    section_lines: list[str] | None, name: str
) -> dict[str, Any] | None:
    """The fields the plan text prescribes for ``name`` on its own D-day.

    ``None`` when the day carries no exact line for this exercise, so the
    caller keeps the planner's dose instead of borrowing another drill's.
    """
    title_re = _source_title_re(name)
    if not section_lines or title_re is None:
        return None
    segment = _source_block_segment("\n".join(section_lines), name)
    first, _, details = segment.partition("\n")
    title = title_re.match(first)
    if title is None:
        return None
    dose = first[title.end() :].strip()
    parsed = _parse_display_text(f"- {name}: {dose}\n{details}")
    fields, _ = _lift_dose_fields(parsed.activity_dose, name)
    return {
        **fields,
        "purpose": parsed.purpose or None,
        "execution_instructions": _preserve_unrepresented_dose(parsed.activity_dose, parsed.execution_instructions, name),
        "coaching_cues": list(parsed.cues),
        "regression_options": list(parsed.regressions),
        "substitutions": list(parsed.substitutions),
        "progression_rule": parsed.progression or None,
        "stop_rules": list(parsed.stop_rules),
    }


_DOSE_WORD_RE = re.compile(
    r"^(?:\d+(?:\.\d+)?(?:[x×]\d+(?:\.\d+)?)?(?:s|secs?|seconds?|mins?|minutes?|m|kg|lbs?|reps?|sets?|%)?|"
    r"[x×@]|rpe|rir|sets?|reps?|rounds?|work|rest|reset|recovery|between|with|of|and|per|each|side|"
    r"technical|continuous|easy|low|max|total|carries|over|s|secs?|seconds?|mins?|minutes?|m)$",
    re.I,
)


def _dose_qualifiers(text: str) -> str:
    """The coaching words in a dose once its numbers are shown as stats.

    "2x6 @ RPE 6, keep reps crisp" -> "keep reps crisp"; a clause made only of
    quantities the card already displays ("60 sec between sets") is dropped.
    """
    kept = []
    for clause in re.split(r"\s*[,;|]\s*", str(text or "")):
        clause = clause.strip().rstrip(".").strip()
        words = [w for w in re.split(r"[\s/()+:]+", clause.lower()) if w]
        if words and not all(_DOSE_WORD_RE.match(word) for word in words):
            # Drop the leading quantity the stats show: "3 x 2 at max speed".
            tokens = clause.split()
            while tokens and re.fullmatch(r"[\d.]+[x×]?[\d.]*\w{0,7}|[x×@]|at|sets?|reps?", tokens[0].lower()):
                tokens.pop(0)
            rest = " ".join(tokens)
            kept.append(rest if rest and not rest.lower().startswith("or ") else clause)
    return ", ".join(kept)


def _preserve_unrepresented_dose(dose: str, instructions: str | None, name: str | None = None) -> str | None:
    """Keep compound working quantities outside disposable coaching cues.

    A heavy lift -> jump pair is not one repetition statistic. The original
    sequence, loading and rest must survive cue compaction without inventing
    a single set/rep interpretation for its different components.
    """
    lifted, remainder = _lift_dose_fields(dose, name)
    if lifted.get("components"):
        # The cluster is shown as rounds + one row per movement; repeating the
        # whole compound sentence under "How" would only duplicate it.
        return instructions or None
    compound = "→" in dose or re.search(r"\brounds?\s*:", dose, re.I)
    # A dose the card already shows as Sets/Duration/Rest/Effort is not an
    # execution instruction; repeating it under "How" only duplicates the stats.
    # Keep it only when it is compound or nothing could be shown as a stat.
    working = dose if compound else (remainder if not lifted else _dose_qualifiers(remainder))
    if working and working not in str(instructions or ""):
        return "\n".join(part for part in (working, instructions) if part)
    return instructions or None


# The weekly priority exposure floor carries the same microdose both as planner
# metadata and in the host's closed selected membership.  Render that member
# through this labelled block, and skip its mirrored assignment below, so the
# fallback has one athlete-facing representation rather than two.
_MICRODOSE_BLOCK_TYPE_BY_GOAL = {
    "power": "plyometric_power",
    "speed": "speed",
    "strength": "strength",
    "footwork": "skill",
    "mobility": "mobility_activation",
}


def _microdose_block(role: dict[str, Any], d_day: int, role_key: str,
                     source_lines: list[str] | None = None) -> dict[str, Any] | None:
    """The host's priority microdose, as subordinate work inside its session.

    Never its own session or card: it is rendered as the first block of the host
    the floor chose, and the host keeps its own identity, title and session type.
    """
    microdose = role.get("priority_microdose")
    if not isinstance(microdose, dict):
        return None
    name = str(microdose.get("name") or "").strip()
    if not name:
        return None
    goal = str(microdose.get("goal") or "").strip().lower()
    prescription = str(microdose.get("prescription") or "").strip()
    label = f"{goal.replace('_', ' ').title()} microdose" if goal else "Priority microdose"
    from fightcamp.exercise_coaching import exercise_coaching
    coaching = exercise_coaching({"name": name, "slot_group": "priority_microdose",
                                  "coaching_notes": microdose.get("coaching_notes"),
                                  "effective_prescription": prescription}, role, d_day=d_day)
    coaching.pop("progression_rule", None)
    coaching["regression_options"] = []
    source = _source_block(source_lines, name)
    if source:
        for key in ("execution_instructions", "coaching_cues", "progression_rule", "regression_options", "stop_rules"):
            if source.get(key):
                coaching[key] = source[key]
    return {
        "block_id": f"deterministic-{d_day}-{role_key}-{role.get('session_index', 0)}-microdose",
        "block_type": _MICRODOSE_BLOCK_TYPE_BY_GOAL.get(goal, "accessory"),
        # Preserve the selected exercise name for closed-membership checks and
        # identity resolution. The priority label belongs in its purpose.
        "display_name": name,
        "exercise_key": block_exercise_key_for_name(name),
        "order_index": 0,
        **coaching,
        "coaching_cues": coaching["coaching_cues"] + ([prescription] if prescription else []),
        "purpose": label,
        "substitutions": [],
    }


def _blocks(
    role: dict[str, Any],
    d_day: int,
    role_key: str,
    source_lines: list[str] | None = None,
    athlete: dict[str, Any] | None = None,
    phase: str = "",
    progression_context: dict | None = None,
) -> list[dict[str, Any]]:
    block_type = _block_type(role)
    blocks: list[dict[str, Any]] = []
    microdose = role.get("priority_microdose")
    microdose_name = (
        str(microdose.get("name") or "").strip().casefold()
        if isinstance(microdose, dict)
        else ""
    )
    microdose_slot = (
        f"priority_microdose::{microdose.get('goal')}"
        if isinstance(microdose, dict)
        else ""
    )
    for index, assignment in enumerate(role.get("selected_exercise_assignments") or []):
        if not isinstance(assignment, dict):
            continue
        name = str(assignment.get("name") or "").strip()
        if not name:
            continue
        if isinstance(microdose, dict) and (
            assignment.get("slot_group") == "priority_microdose"
            or str(assignment.get("slot_id") or "") == microdose_slot
            or name.casefold() == microdose_name
        ):
            continue
        block: dict[str, Any] = {
            "block_id": f"deterministic-{d_day}-{role_key}-{role.get('session_index', 0)}-{index}",
            "block_type": block_type,
            "display_name": name,
            # The assignment name is the canonical bank exercise.
            "exercise_key": block_exercise_key_for_name(name),
            "order_index": index,
            "regression_options": [],
            "substitutions": [],
        }
        source = _source_block(source_lines, name)
        if source is not None:
            # The plan text is what the athlete was given: its exact dose and
            # its own Cue / Purpose / Easier / Stop lines are the card.
            block.update({key: value for key, value in source.items() if value not in (None, [])})
            block.setdefault("coaching_cues", [])
        else:
            # Keep the exact dose in statistics and coaching detail, with the
            # execution cue first rather than repeating numbers as Key cue.
            prescription = _exact_working_dose(_effective_prescription(role, assignment))
            block["coaching_cues"] = []
            if prescription:
                # The same exact numbers as stats, so timers and logging run
                # on the planner's dose; the verbatim cue stays alongside.
                fields, _ = _lift_dose_fields(prescription, name)
                block.update(fields)
            from fightcamp.exercise_coaching import exercise_coaching
            from fightcamp.exercise_progression import occurrence_key
            coaching = exercise_coaching(assignment, role, phase=phase, d_day=d_day, athlete=athlete,
                                         progression_context=(progression_context or {}).get(
                                             occurrence_key(role, assignment, d_day)))
            # Stage 1 bank notes are execution evidence, not final-session AI
            # adjustments. Without final source text, both fields remain absent.
            coaching.pop("progression_rule", None)
            coaching["regression_options"] = []
            block["coaching_cues"].extend(coaching.pop("coaching_cues"))
            if prescription and not block.get("components"):
                # A cluster's dose is fully shown as rounds + component rows.
                block["coaching_cues"].append(prescription)
            block.update(coaching)
            block["execution_instructions"] = _preserve_unrepresented_dose(
                prescription, block.get("execution_instructions"), name)
        blocks.append(block)
    # Only ever attached to a host that already renders. A role with no selected
    # exercise renders no session at all here, and a microdose must not be the
    # thing that brings one into existence - that would be a new session.
    microdose_block = _microdose_block(role, d_day, role_key, source_lines) if blocks else None
    if microdose_block is not None:
        for block in blocks:
            block["order_index"] = int(block.get("order_index") or 0) + 1
        blocks.insert(0, microdose_block)
    return blocks


def _support_instruction(role: dict[str, Any]) -> str:
    """The role's own athlete-facing instruction, verbatim, or ``""``.

    Support inserts (joint prep, breathing reset, footwork walkthrough,
    visualisation) carry their whole prescription as one banked sentence in
    ``display_text`` and never populate ``selected_exercise_assignments``. That is
    real scheduled content, so it is preserved as the session's instruction — it
    is not turned into an invented exercise with an invented dose.
    """
    for key in ("display_text", "athlete_facing_text", "prescription"):
        text = str(role.get(key) or "").strip()
        if text:
            return text
    return ""


# ---------------------------------------------------------------------------
# The planner's own athlete-facing session-body grammar.
#
# Every banked insert renders the SAME shape (build_watch_display_text,
# build_visualization_display_text, build_coordination_display_text and
# gap_fill_inserts._apply_bank_footwork all document it):
#
#     Why: <rationale>                  unbulleted lead line, optional
#     - <Name>: <dose>                  one bulleted activity heading
#       <Label>: <detail>               indented labelled lines owned by it
#
# This is a fixed, machine-generated grammar, so it is parsed exactly — not
# guessed at. Copy that does not match it (the one-line instruction most
# _INSERT_META entries carry) is left alone as instruction, never reshaped.
# ---------------------------------------------------------------------------

_WHY_LINE_RE = re.compile(r"^\s*why\s*:\s*(?P<text>.+)$", re.IGNORECASE)
_ACTIVITY_LINE_RE = re.compile(r"^\s*[-*•]\s*(?P<body>.+)$")
_DETAIL_LINE_RE = re.compile(r"^\s*(?:\*\*)?(?P<label>[A-Za-z][A-Za-z /-]{0,28})(?:\*\*)?\s*:\s*(?:\*\*)?\s*(?P<text>.+)$")

#: Detail labels that already have a home in the structured block schema. Every
#: other planner label (Cue, Cue Method, Side / Stance, Step N, Intent, Focus,
#: Reset, Anchor, Rule, Pre-bout, Rest…) keeps its own wording as a coaching cue.
_STOP_LABELS = {"quality stop", "stop", "stop rule"}
_REGRESSION_LABELS = {"easier", "regress", "regression"}
_PROGRESSION_LABELS = {"progress", "progression"}
_PURPOSE_LABELS = {"purpose"}
_SUBSTITUTION_LABELS = {"swap", "swaps", "substitution"}
_WHY_LABELS = {"why", "why today"}

#: Detail labels Stage 2 may write INLINE after an exercise's dose instead of
#: on their own indented lines ("- Push-Up: 2 sets x 6 reps; RPE 6. Cue: ...
#: Purpose: ... Stop: ..."). Only these known labels, and only at a sentence
#: boundary, start a new detail line, so ordinary prose containing a colon is
#: never split.
_INLINE_DETAIL_LABELS = sorted(
    {"cue", "how", *_STOP_LABELS, *_REGRESSION_LABELS, *_PROGRESSION_LABELS, *_PURPOSE_LABELS,
     *_SUBSTITUTION_LABELS},
    key=len,
    reverse=True,
)
_INLINE_DETAIL_RE = re.compile(
    r"(?<=[.;!?])\s+(?=(?:" + "|".join(re.escape(label) for label in _INLINE_DETAIL_LABELS) + r")\s*:)",
    re.IGNORECASE,
)


def _explode_inline_details(line: str) -> list[str]:
    """One line per labelled detail, whether the text wrote them inline or not."""
    return [part.strip() for part in _INLINE_DETAIL_RE.split(line) if part.strip()]


@dataclass
class _ParsedDisplayText:
    """What the planner's session body says, split by what it means."""

    why: str = ""
    instruction: str = ""
    activity_name: str = ""
    activity_dose: str = ""
    purpose: str = ""
    execution_instructions: str = ""
    cues: list[str] = field(default_factory=list)
    stop_rules: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    substitutions: list[str] = field(default_factory=list)
    progression: str = ""


def _parse_display_text(display_text: str) -> _ParsedDisplayText:
    """Split planner-authored copy into its labelled parts, wording preserved."""
    parsed = _ParsedDisplayText()
    lines = [
        part
        for line in str(display_text or "").splitlines()
        if line.strip()
        for part in _explode_inline_details(line.rstrip())
    ]
    if not lines:
        return parsed

    instruction_lines: list[str] = []
    seen_activity = False
    for line in lines:
        if not seen_activity:
            why = _WHY_LINE_RE.match(line)
            if why and not parsed.why:
                parsed.why = why.group("text").strip()
                continue
        activity = _ACTIVITY_LINE_RE.match(line)
        if activity and not seen_activity:
            seen_activity = True
            body = activity.group("body").strip()
            name, separator, dose = body.partition(":")
            if separator and name.strip():
                parsed.activity_name = name.strip()
                parsed.activity_dose = dose.strip()
            else:
                parsed.activity_name = body
            continue
        detail = _DETAIL_LINE_RE.match(line)
        if seen_activity and detail:
            label = detail.group("label").strip().lower()
            text = detail.group("text").strip()
            if label == "how":
                parsed.execution_instructions = text
            elif label in _STOP_LABELS:
                parsed.stop_rules.append(text)
            elif label in _REGRESSION_LABELS:
                parsed.regressions.append(text)
            elif label in _SUBSTITUTION_LABELS:
                parsed.substitutions.append(text)
            elif label in _PROGRESSION_LABELS:
                # "Progress: none this block" states that nothing advances.
                if not parsed.progression and not is_null_progression(text):
                    parsed.progression = text
            elif label in _PURPOSE_LABELS and not parsed.purpose:
                parsed.purpose = text
            elif label in _WHY_LABELS and not parsed.why:
                parsed.why = text
            else:
                # Keep the planner's own label with its text: "Cue Method: …"
                # reads as coaching, and dropping the label would lose meaning.
                parsed.cues.append(line.strip())
            continue
        if seen_activity:
            parsed.cues.append(line.strip())
            continue
        instruction_lines.append(line.strip())

    parsed.instruction = " ".join(instruction_lines).strip()
    return parsed


def _parsed_block(
    parsed: _ParsedDisplayText, role: dict[str, Any], d_day: int, role_key: str
) -> dict[str, Any] | None:
    """The planner's bulleted activity as one block, or ``None`` when it has none."""
    if not parsed.activity_name:
        return None
    # The dose is a planner sentence ("2 sets x 4 clean reactions each
    # direction, full stance reset between reps."), not a parsed set/rep
    # structure. It is surfaced verbatim as the block's leading cue rather than
    # split into numbers this module would have to invent.
    lifted, _ = _lift_dose_fields(parsed.activity_dose) if parsed.activity_dose else ({}, "")
    # Numbers the stats show are not repeated as a cue; only the coaching
    # wording around them stays ("6 minutes, RPE 4 max." leaves nothing).
    lead = _dose_qualifiers(parsed.activity_dose) if lifted else parsed.activity_dose
    cues = [lead] if lead else []
    cues.extend(parsed.cues)
    block = {
        **lifted,
        "block_id": f"deterministic-{d_day}-{role_key or 'role'}-display",
        "block_type": _block_type(role),
        "display_name": parsed.activity_name,
        "order_index": 0,
        "purpose": parsed.purpose or None,
        "execution_instructions": parsed.execution_instructions or None,
        "why_today": parsed.why or None,
        "coaching_cues": cues,
        "regression_options": list(parsed.regressions),
        "substitutions": list(parsed.substitutions),
        "progression_rule": parsed.progression or None,
        "stop_rules": list(parsed.stop_rules),
    }
    exercise_key = _insert_exercise_key(role)
    if exercise_key:
        block["exercise_key"] = exercise_key
    return block


def _insert_exercise_key(role: dict[str, Any]) -> str | None:
    """The movement identity Stage 1 assigned this support insert, if any.

    The planner stamps ``exercise_key`` on an insert that is one known physical
    movement ("Shadowboxing Aerobic Flow" is tempo shadowboxing); older briefs
    predate the stamp, so the same deterministic table is consulted for them.
    Inserts without one (joint prep, breathing, visualisation) return ``None``.
    """
    stamped = normalize_exercise_key(role.get("exercise_key"))
    if stamped:
        return stamped
    return support_insert_exercise_key(role.get("role_key"), role.get("athlete_facing_label"))


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def _insert_movement_block(
    role: dict[str, Any], d_day: int, role_key: str, title: str, instruction: str
) -> dict[str, Any] | None:
    """One block for a support insert that IS one known movement, else ``None``.

    Without a block the insert has nothing to carry its ``exercise_key``, so the
    card prints the banked sentence as bare text and the demo never resolves.
    The movement and its identity are the planner's; the dose is the banked
    copy verbatim. Only clauses of its first sentence that are entirely one
    quantity ("60 sec rest", "RPE 3") are lifted into stats; every other word
    stays, in order, as the block's cues, so nothing is invented or dropped.
    """
    exercise_key = _insert_exercise_key(role)
    if not exercise_key or not instruction:
        return None
    first, *more = _SENTENCE_SPLIT_RE.split(instruction.strip(), maxsplit=1)
    fields, remainder = _lift_dose_fields(re.sub(r"\s*,\s*", "; ", first.strip()))
    cues = [remainder] if remainder else []
    cues.extend(sentence.strip() for sentence in more if sentence.strip())
    return {
        "block_id": f"deterministic-{d_day}-{role_key or 'role'}-movement",
        "block_type": _block_type(role),
        "display_name": title,
        "exercise_key": exercise_key,
        "order_index": 0,
        "coaching_cues": cues,
        "regression_options": [],
        "substitutions": [],
        **fields,
    }


def _source_lines_for_session(
    lines: list[str] | None, title: str, *, require_title: bool = False,
    block_name: str | None = None,
) -> list[str] | None:
    """Scope repeated D-day exercise names to their own source session."""
    if not lines:
        return lines
    from fightcamp.structured_session_identity import identity_tokens
    starts = [i for i, line in enumerate(lines) if _day_header_dday(line) is not None]
    if len(starts) <= 1 and not require_title:
        return lines
    sections = [lines[start:end] for start, end in zip(starts, [*starts[1:], len(lines)])]
    wanted = identity_tokens(title)
    matched = [section for section in sections
               if wanted and identity_tokens(re.split(r"[—–]|\s-\s", section[0], maxsplit=1)[-1]) == wanted]
    if not matched and block_name and not require_title:
        # Canonical session titles may differ from finalizer wording. An exact
        # exercise occurring in just one session on this day is still provable.
        matched = [section for section in sections
                   if _source_block_segment("\n".join(section), block_name)]
    return matched[0] if len(matched) == 1 else None


def _source_instruction_for_role(role: dict[str, Any], lines: list[str] | None) -> str:
    title = athlete_facing_label_for(role.get("role_key"), fallback=role.get("athlete_facing_label"))
    section = _source_lines_for_session(lines, str(title or ""), require_title=True)
    return "\n".join(line for line in section or [] if _day_header_dday(line) is None).strip()


def _session(
    role: dict[str, Any], d_day: int, source_lines: list[str] | None = None,
    athlete: dict[str, Any] | None = None, phase: str = "",
    progression_context: dict | None = None,
) -> dict[str, Any] | None:
    role_key = str(role.get("role_key") or "").strip()
    title = athlete_facing_label_for(
        role_key, fallback=str(role.get("athlete_facing_label") or "").strip() or None
    )
    # The appointment's own label disambiguates duplicate role/exercise pairs.
    source_title = str(role.get("athlete_facing_label") or title or "")
    source_lines = _source_lines_for_session(source_lines, source_title)
    if source_lines:
        title = source_title
    blocks = _blocks(role, d_day, role_key or "role", source_lines, athlete, phase, progression_context)
    instruction = _support_instruction(role)
    if not blocks and not instruction and source_lines:
        # An open role's approved text is its instruction; an exact source
        # heading prevents borrowing another same-day card.
        instruction = _source_instruction_for_role(role, source_lines)
    parsed = _parse_display_text(instruction) if instruction and not blocks else None
    if parsed is not None:
        display_block = _parsed_block(parsed, role, d_day, role_key)
        if display_block is not None:
            blocks = [display_block]
    if not blocks and not instruction:
        # A role with neither a selected exercise nor athlete-facing copy has
        # nothing deterministic to render; inventing a session here would be the
        # fallback making things up.
        return None
    session_index = role.get("session_index")
    suffix = session_index if isinstance(session_index, int) else 0
    if not blocks and parsed is not None and not parsed.activity_name:
        # One-sentence copy for a known movement: give it a block so the
        # planner's movement identity (and its demo) reaches the card.
        movement = _insert_movement_block(
            role, d_day, role_key, str(title or "Session"), parsed.instruction
        )
        if movement is not None:
            blocks = [movement]
            # The prescription now lives on the block; the session keeps only
            # its rationale, or its title (which the card does not repeat).
            parsed = _ParsedDisplayText(why=parsed.why)
    # ``day_assignment_reason`` is internal Stage 1 placement rationale
    # ("Declared hard sparring day is fixed in the weekly role map", "Use the
    # lowest-load day immediately before the primary strength anchor"). The
    # finalizer packet already withholds it as non-athlete-facing content; using
    # it here published that same internal reasoning straight to the athlete as
    # the card's objective. It stays on the role for audit; the athlete sees the
    # athlete-facing label instead.
    #
    # Otherwise the objective is the session's RATIONALE when the planner wrote
    # one ("Why: read the opponent's exit lane…"), and the instruction itself
    # only when that instruction is the whole session — a blockless card whose
    # single line IS the prescription. Putting instructional copy behind the
    # renderer's "Why" kicker told the athlete that "Neck CARs, shoulder CARs,
    # wrist circles…" was a rationale.
    #
    # A session built from selected exercises takes the plan text's own "Why:"
    # for that session, so the card does not repeat its title as a rationale.
    objective = str(title or "Session")
    if parsed is not None:
        if parsed.why and blocks:
            objective = parsed.why
        elif parsed.instruction:
            objective = parsed.instruction
        elif parsed.why:
            objective = parsed.why
    elif blocks:
        names = [
            str(assignment.get("name") or "").strip()
            for assignment in role.get("selected_exercise_assignments") or []
            if isinstance(assignment, dict)
        ]
        objective = _source_session_why(source_lines, [n for n in names if n]) or objective
    from fightcamp.mental_training_policy import mental_category
    duration = role.get("prescribed_duration_min") or role.get("duration_min")
    if isinstance(duration, (list, tuple)):
        duration = duration[-1] if duration else None
    mental_duration = (
        {"planned_duration": {"value": duration, "unit": "minutes"}}
        if mental_category(role) and isinstance(duration, (int, float)) else {}
    )
    return {
        **mental_duration,
        "session_id": f"deterministic-{d_day}-{role_key}-{suffix}",
        "session_type": _session_type(role),
        "title": title or "Session",
        "objective": objective,
        "completion_status": "not_started",
        "mindset_anchor": {"intent": "", "focus_cue": "", "reset_cue": ""},
        "blocks": blocks,
    }


def reconcile_open_development_sessions(
    plan: dict[str, Any], planning_brief: Any,
) -> dict[str, Any]:
    """Project saved open-block membership through the existing role converter.

    The structured week supplies presentation and stable ids; the deterministic
    block supplies each week's membership, placement and dose. Legacy templates
    without that block retain their existing projection path.
    """
    if not isinstance(planning_brief, dict) or planning_brief.get("custom_plan"):
        return plan
    spec = planning_brief.get("open_plan_spec") or {}
    programmed = spec.get("weekly_session_plan") or {}
    decisions = programmed.get("weeks")
    if (spec.get("plan_type") != "open_ongoing_system"
            or programmed.get("authority") != "deterministic_open_development_block"
            or not isinstance(decisions, list) or len(decisions) != 4):
        return plan
    # Keep legacy and dated-camp reads outside the open planner's import graph,
    # including historical source replays that intentionally load older owners.
    from .services.open_plan_timeline import (
        WEEKDAYS, _coach_owned_days, _expand_single_template_week, _ordered_weekdays, _resolve_template_days,
    )
    from fightcamp.weekly_schedule_view import normalize_weekday
    from fightcamp.stage2_payload import OPEN_BLOCK_HORIZON_DAYS, OPEN_BLOCK_WEEKS

    training_days = _ordered_weekdays((spec.get("weekly_template") or {}).get("training_days"))
    coach_days = set(_coach_owned_days(spec.get("weekly_template") or {}))
    weeks = copy.deepcopy(plan.get("weeks") or [])
    for week in weeks:
        days = _resolve_template_days(week.get("days"), training_days)
        if days is None:
            return plan
        week["days"] = days
    weeks = _expand_single_template_week(weeks, spec)
    if weeks is None:
        return plan

    # Keep the existing tactical placement, but only in its prescribed rotation
    # weeks. Its bank-owned content is restored by the existing locked merger.
    rotation = (spec.get("tactical_watch") or {}).get("weekly_rotation") or []
    watch_names = {entry.get("name") for entry in rotation}
    watch_templates = [
        (day["weekday"], session)
        for week in weeks for day in week["days"] for session in day.get("sessions") or []
        if any(block.get("display_name") in watch_names for block in session.get("blocks") or [])
    ]
    dose_fields = ("sets", "reps", "rounds", "work", "rest", "rest_note", "duration", "distance", "load",
                   "tempo", "effort", "intensity", "components")
    for position, (week, decision) in enumerate(zip(weeks, decisions, strict=True), start=1):
        if not isinstance(decision, dict) or not isinstance(decision.get("sessions"), list):
            return plan
        by_day = {day["weekday"]: day for day in week["days"]}
        previous_by_day = {day["weekday"]: list(day.get("sessions") or []) for day in week["days"]}
        for day in week["days"]:
            day["sessions"] = []
        for index, item in enumerate(decision["sessions"]):
            weekday = normalize_weekday(item.get("weekday"))
            if weekday not in training_days or weekday not in by_day:
                return plan
            # Coach-led combat is represented by the existing contact owner,
            # never by an executable session with a fabricated exercise.
            if item.get("coach_owned") or item.get("category") == "sparring":
                continue
            exercises = item.get("exercises") or []
            names = [exercise["name"] for exercise in exercises]
            candidates = [session for session in previous_by_day[weekday]
                          if session.get("title") == item.get("label")]
            if not candidates:
                candidates = [session for session in previous_by_day[weekday]
                              if names and [block.get("display_name") for block in session.get("blocks") or []] == names]
            old = candidates[0] if len(candidates) == 1 else None
            if old is not None:
                previous_by_day[weekday].remove(old)
            role = {
                **item, "athlete_facing_label": item.get("label"), "session_index": index,
                "display_text": item.get("display_text") or (item.get("label") if not exercises else ""),
                "selected_exercise_assignments": [
                    {**exercise, "effective_prescription": exercise.get("prescription")}
                    for exercise in exercises
                ],
            }
            # Use the same far-from-fight context as the open planner; this
            # converter argument also informs existing exercise coaching.
            block_day = OPEN_BLOCK_HORIZON_DAYS + OPEN_BLOCK_WEEKS * 7 - (position - 1) * 7 - WEEKDAYS.index(weekday)
            session = _session(role, block_day,
                               athlete=planning_brief.get("athlete_snapshot"), phase="GPP")
            if session is None:
                continue
            if old is not None:
                session = {**copy.deepcopy(old), "blocks": session["blocks"]}
            session["title"] = item.get("label") or session["title"]
            session["execution_order"] = item.get("execution_order")
            for block in session["blocks"]:
                original = next((source for source in (old or {}).get("blocks") or []
                                 if source.get("display_name") == block["display_name"]), None)
                if original is not None:
                    # Preserve exercise-specific Progress/Easier/Stop copy. Only
                    # the prescribed dose comes from this week's decision.
                    dose = {key: block.get(key) for key in dose_fields}
                    block.update(copy.deepcopy(original))
                    block.update(dose)
                    prescription = next((exercise.get("prescription") for exercise in exercises
                                         if exercise.get("name") == block["display_name"]), "")
                    if block.get("components"):
                        # A cluster's dose is its rounds + component rows; the
                        # sentence form is not repeated as a coaching cue.
                        block["coaching_cues"] = [cue for cue in block.get("coaching_cues") or []
                                                  if cue != prescription]
                    elif prescription and prescription not in (block.get("coaching_cues") or []):
                        block["coaching_cues"] = [*(block.get("coaching_cues") or []), prescription]
            by_day[weekday]["sessions"].append(session)
        for entry in rotation:
            if entry.get("week") != position or not watch_templates:
                continue
            weekday, template = next(
                ((day, session) for day, session in watch_templates if any(
                    block.get("display_name") == entry.get("name") for block in session.get("blocks") or []
                )), watch_templates[0],
            )
            watch = copy.deepcopy(template)
            watch["title"] = entry.get("label") or watch["title"]
            watch["blocks"][0]["display_name"] = entry["name"]
            by_day[weekday]["sessions"].append(watch)
        for day in week["days"]:
            if day["sessions"]:
                day["today_card"]["headline"] = day["sessions"][0]["title"]
                if day.get("day_type") == "rest":
                    day["day_type"] = "moderate"
            elif (day["weekday"] in training_days and day.get("day_type") != "recovery"
                  and day["weekday"] not in coach_days
                  and not day["today_card"].get("coach_led_contact")):
                day["today_card"]["headline"] = "No session planned"
                day["day_type"] = "rest"
    return {**plan, "weeks": weeks}


def build_deterministic_structured_plan(
    planning_brief: Any, plan_text: str | None = None
) -> dict[str, Any] | None:
    """Assemble the canonical fallback plan, or ``None`` when not applicable.

    ``plan_text`` is the plan the athlete was given. The planner still decides
    which days and exercises exist; the text supplies each selected exercise's
    exact dose and coaching lines where it carries that exercise on that day.

    Never raises: an unusable brief returns ``None`` so the caller keeps whatever
    behaviour it had before.
    """
    try:
        return _build(planning_brief, plan_text)
    except Exception:  # a fallback that raises is worse than no fallback
        logger.exception("[deterministic_fallback] assembly failed")
        return None


def _build(planning_brief: Any, plan_text: str | None = None) -> dict[str, Any] | None:
    if not isinstance(planning_brief, dict):
        return None
    role_map = planning_brief.get("weekly_role_map")
    if not isinstance(role_map, dict):
        return None
    weeks = role_map.get("weeks")
    if not isinstance(weeks, list) or not weeks:
        return None
    sequence = planning_brief.get("late_fight_session_sequence")
    if isinstance(sequence, list):
        # Direct countdowns finish composition after their weekly allocation.
        # Use that final membership/dose while preserving the calendar spans and
        # contact safety metadata owned by the earlier allocation.
        role_map = copy.deepcopy(role_map)
        weeks = role_map["weeks"]
        for week in weeks:
            span = week.get("countdown_span") or {}
            start, end = span.get("start_day"), span.get("end_day")
            if isinstance(start, int) and isinstance(end, int):
                week["session_roles"] = [copy.deepcopy(role) for role in sequence
                                         if (day := _dday(role)) is not None and end <= day <= start]
            elif len(weeks) == 1:
                week["session_roles"] = copy.deepcopy(sequence)
        planning_brief = {**planning_brief, "weekly_role_map": role_map}

    # Identity is (d_day, role_key, session_index): two distinct roles on one day
    # are two sessions, and the same role is never emitted twice.
    sessions_by_dday: dict[int, list[dict[str, Any]]] = {}
    seen: set[tuple[int, str, Any]] = set()
    from fightcamp.exercise_coaching_source import validate_source_coaching
    plan_text, _ = validate_source_coaching(planning_brief, plan_text or "")
    source_days = _source_day_section_lines(plan_text) if isinstance(plan_text, str) else {}
    from fightcamp.exercise_progression import build_progression_context
    progression_context = build_progression_context(planning_brief)
    for week in weeks:
        if not isinstance(week, dict):
            continue
        for role in week.get("session_roles") or []:
            if not isinstance(role, dict):
                continue
            role_key = str(role.get("role_key") or "").strip()
            if role_key in _ROLES_OWNED_ELSEWHERE:
                continue
            d_day = _dday(role)
            if d_day is None:
                continue
            identity = (d_day, role_key, role.get("session_index"))
            if identity in seen:
                continue
            session = _session(role, d_day, source_days.get(d_day),
                               planning_brief.get("athlete_snapshot"),
                               str(week.get("phase") or week.get("phase_label") or ""), progression_context)
            if session is None:
                continue
            seen.add(identity)
            sessions_by_dday.setdefault(d_day, []).append(session)

    # The spine builds every calendar day in its own schema-valid shape, so the
    # fallback never hand-rolls a day: the sessions are attached onto the days it
    # produced. This is also why a Tactical Watch on a day with no S&C still has
    # a day to be placed on.
    plan = reconcile_calendar_spine({"weeks": []}, planning_brief)
    if not isinstance(plan, dict):
        return None
    days = [
        day
        for week in plan.get("weeks") or []
        for day in week.get("days") or []
        if isinstance(day, dict)
    ]
    if not days:
        return None
    for day in days:
        d_day = _label_dday(day.get("countdown_label"))
        sessions = sessions_by_dday.get(d_day) if d_day is not None else None
        if sessions:
            day["sessions"] = sessions
            if day.get("day_type") == "rest" and any(
                session.get("session_type") in {"strength_power", "conditioning", "primer"} for session in sessions
            ):
                # The spine created an empty rest shell before selected work
                # existed. Attaching that work must also clear the rest marker.
                day["day_type"] = "moderate"

    # Assemble every locked session first, then reconcile contact against the
    # finished day. A Tactical Watch can be merged onto a declared contact day;
    # the final reconcile must see that session so it writes coach_led_contact
    # instead of leaving the contact only in a headline the renderer will hide.
    plan = merge_planner_owned_structured_content(plan, planning_brief).plan

    # The first spine pass intentionally ran before sessions existed.  Re-run
    # the same final-day invariant now that fallback and locked sessions are all
    # present, removing a day-level card whenever its session block survived.
    plan = reconcile_priority_microdose_representations(plan, planning_brief)

    # Sessions entered each day in role-map order and locked cards were
    # appended after them; neither is an execution order. Sequence the finished
    # days by intent (fightcamp.session_sequencing) now that membership is final.
    plan = sequence_structured_plan(plan, planning_brief)

    weeks_out = plan.get("weeks") if isinstance(plan, dict) else None
    if not isinstance(weeks_out, list) or not weeks_out:
        return None
    strategy = planning_brief.get("phase_strategy") or {}
    for week in weeks_out:
        if not week.get("week_goal"):
            week["week_goal"] = str((strategy.get(week.get("phase_label")) or {}).get("visible_objective") or "")
    plan.update(_plan_envelope(planning_brief))
    from .structured_plan_coaching import recover_selected_coaching
    return recover_selected_coaching(plan, planning_brief, progression_context, source_text=plan_text or "")


def _label_dday(label: Any) -> int | None:
    text = str(label or "").strip().upper()
    if not text.startswith("D-"):
        return None
    try:
        return int(text[2:])
    except ValueError:
        return None


def _plan_envelope(planning_brief: dict[str, Any]) -> dict[str, Any]:
    """Schema-required plan-level sections, from deterministic state only.

    Nothing here is invented: fields with no deterministic source stay empty
    rather than carrying text the planner never produced. In particular the
    nutrition section is left blank -- the athlete-safe numbers already travel
    on ``deterministic_support``, and this fallback must not author guidance.
    """
    athlete = planning_brief.get("athlete_model")
    if not isinstance(athlete, dict) or not athlete:
        athlete = planning_brief.get("athlete_snapshot")
    athlete = athlete if isinstance(athlete, dict) else {}
    sport = str(athlete.get("sport") or athlete.get("sport_profile") or "").strip()
    return {
        "plan_metadata": {
            "title": "Fight camp plan",
            "sport": sport or "combat_sports",
            "plan_type": "fight_camp",
            "timezone": str(athlete.get("athlete_timezone") or "UTC").strip() or "UTC",
            "status": "active",
        },
        "athlete_context": {
            "sport_profile": sport or "combat_sports",
            "equipment_access": [
                str(item)
                for item in athlete.get("equipment") or []
                if str(item).strip()
            ],
        },
        "nutrition": {
            "summary": "",
            "daily_focus": "",
            "training_day_guidance": "",
            "fight_week_guidance": "",
        },
    }
