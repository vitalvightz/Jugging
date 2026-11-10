"""Measurable coaching from resolved exposures, never a new dose authority."""
from __future__ import annotations

from collections import Counter, defaultdict
import math
import re
from typing import Any

from .calendar_context import role_d_day
from .prescription_resolver import athlete_dose_state
from .working_dose import working_dose

_TIME = r"(seconds?|secs?|s|minutes?|mins?|min)\b"
_WORK = ("load", "reps", "sets", "distance", "duration", "rounds", "work")


def effective_exercise_prescription(role: dict, assignment: dict) -> str:
    """Read the same resolved strength/dose authority that renders this card."""
    resolved = [p for p in role.get("effective_strength_prescriptions") or []
                if p.get("name") == assignment.get("name")
                and (not assignment.get("slot_id") or p.get("slot_id") == assignment.get("slot_id"))]
    value = resolved[0].get("effective_prescription") if len(resolved) == 1 else assignment.get("effective_prescription")
    value = value or assignment.get("base_prescription")
    if isinstance(value, dict):
        value = value.get("display") or value.get("dose") or value.get("text")
    return working_dose(str(value or ""))


def measured_dose(text: str, assignment: dict | None = None) -> dict:
    """Parse only unambiguous working quantities; compound/choice doses abstain."""
    text = working_dose(text)
    if not text or re.search(r"\bor\b|→|\+", text):
        return {}
    dose: dict[str, Any] = {}
    if m := re.search(r"(\d+)\s*(?:sets?\s*)?[x×]\s*(\d+)\s*(?:reps?\b|/\s*(?:side|leg)\b)?", text, re.I):
        tail = text[m.end():]
        # NxM seconds/metres is not a repetition count.
        if not re.match(r"\s*(?:s|secs?|seconds?|min|minutes?|m|metres?|meters?)\b", tail, re.I):
            dose.update(sets=int(m[1]), reps=int(m[2]))
    if m := re.search(r"(\d+)\s*(?:holds?\s*)?[x×]\s*(\d+(?:\.\d+)?)\s*" + _TIME, text, re.I):
        dose.update(sets=int(m[1]), duration=_seconds(m[2], m[3]))
    if m := re.search(r"(\d+)\s+(?:carries|sets)\s+(?:over\s+)?(\d+(?:\.\d+)?)\s*m\b", text, re.I):
        dose.update(sets=int(m[1]), distance=float(m[2]))
    elif m := re.search(r"(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*m\b", text, re.I):
        dose.update(sets=int(m[1]), distance=float(m[2]))
    if m := re.search(r"(\d+(?:\.\d+)?)\s*" + _TIME + r"\s*(?:continuous|easy)\b", text, re.I):
        dose["duration"] = _seconds(m[1], m[2])
    if m := re.search(r"(\d+(?:\.\d+)?)\s*" + _TIME + r"\s*(?:work|effort)", text, re.I):
        dose["work"] = _seconds(m[1], m[2])
    if m := re.search(r"(\d+)\s+rounds?\b", text, re.I):
        dose["rounds"] = int(m[1])
    rests = [*re.finditer(r"(?:rest\s*[:=]?\s*)(\d+(?:\.\d+)?)\s*" + _TIME, text, re.I),
             *re.finditer(r"(\d+(?:\.\d+)?)\s*" + _TIME + r"\s+rest\b", text, re.I)]
    if len(rests) == 1:
        dose["rest"] = _seconds(rests[0][1], rests[0][2])
    if m := re.search(r"\bRPE\s*(\d+(?:\.\d+)?)", text, re.I):
        dose["rpe"] = float(m[1])
    elif (assignment or {}).get("rpe") is not None:
        dose["rpe"] = assignment["rpe"]
    if m := re.search(r"(\d+(?:\.\d+)?)\s*(kg|lb|lbs)\b", text, re.I):
        dose["load"] = float(m[1])
        dose["load_unit"] = "lb" if m[2].lower().startswith("lb") else "kg"
    elif m := re.search(r"(\d+(?:\.\d+)?)\s*%\s*1RM\b", text, re.I):
        dose["load"] = float(m[1])
        dose["load_unit"] = "% 1RM"
    dose["per_side"] = bool(re.search(r"/(?:side|leg)|(?:each|per) (?:side|leg|arm)", text, re.I))
    return dose


def _seconds(value: str, unit: str) -> float:
    return float(value) * (60 if unit.lower().startswith("min") else 1)


def progression_prohibited(role: dict, assignment: dict, phase: str, day: int | None, athlete: dict) -> bool:
    state = athlete_dose_state(athlete)
    policy = role.get("strength_composition_policy") or {}
    return bool(
        phase.upper() == "TAPER" or (day is not None and day <= 14)
        or any(state.values()) or athlete.get("injury_restrictions")
        or (athlete.get("has_active_injury") and not athlete.get("surface_injury_only"))
        # A weight cut blocks progression by its graded severity (moderate and
        # above, an aggressive-cut flag via athlete_dose_state, or the role's cut
        # pressure below), never merely because a cut exists: a low-severity
        # cut weeks out does not freeze every lift.
        or str(athlete.get("cut_severity_bucket") or "none").lower() not in {"none", "low"}
        or assignment.get("slot_group") == "priority_microdose" or assignment.get("support_only")
        or role.get("pre_hard_contact_managed_stress") or role.get("goal_strength_retention")
        or role.get("strength_dose_cap") or policy.get("injury_restricted")
        or (policy.get("fatigue_pressure") or 0) > 0 or (policy.get("cut_pressure") or 0) > 0
    )


def occurrence_key(role: dict, assignment: dict, day: int | None) -> tuple:
    return day, role.get("role_key"), role.get("session_index"), assignment.get("slot_id"), assignment.get("name")


def build_progression_context(brief: dict) -> dict[tuple, dict]:
    """One sorted lookup per programme; same-day duplicates are never 'next'."""
    groups = defaultdict(list)
    athlete = brief.get("athlete_snapshot") or {}
    for week in (brief.get("weekly_role_map") or {}).get("weeks") or []:
        for role in week.get("session_roles") or []:
            day = role_d_day(week, role)
            if day is None or role.get("render_mandatory") is False:
                continue
            for assignment in role.get("selected_exercise_assignments") or []:
                if assignment.get("slot_group") not in {"strength_slots", "conditioning_slots", "priority_microdose"}:
                    continue
                identity = assignment.get("exercise_key") or str(assignment.get("name") or "").casefold()
                # Different same-day appointments keep different programming purposes.
                group = identity, assignment.get("slot_group"), role.get("role_key")
                phase = str(week.get("phase") or week.get("phase_label") or "")
                prescription = effective_exercise_prescription(role, assignment)
                groups[group].append({"day": day, "phase": phase, "role": role, "assignment": assignment,
                                      "prescription": prescription, "dose": measured_dose(prescription, assignment),
                                      "blocked": progression_prohibited(role, assignment, phase, day, athlete)})
    timelines = []
    for exposures in groups.values():
        if any(count > 1 for count in Counter(e["day"] for e in exposures).values()):
            # Two appointments share this role/exercise: retain the full context.
            sessions = defaultdict(list)
            for exposure in exposures:
                sessions[exposure["role"].get("session_index")].append(exposure)
            timelines.extend(sessions.values())
        else:
            # A unique appointment may be reindexed as weekly composition changes.
            timelines.append(exposures)
    context = {}
    for exposures in timelines:
        exposures.sort(key=lambda e: -e["day"])
        for i, current in enumerate(exposures):
            later = [e for e in exposures[i+1:] if e["day"] < current["day"]]
            # Multiple occurrences in the same future appointment are ambiguous.
            next_exposure = later[0] if later and sum(e["day"] == later[0]["day"] for e in later) == 1 else None
            context[occurrence_key(current["role"], current["assignment"], current["day"])] = {
                "current": current, "next": next_exposure,
            }
    return context


def numerical_guidance(context: dict | None, dose: dict, prescription: str) -> dict:
    """One measurable advance to an already authorised future prescription."""
    fields: dict[str, Any] = {}
    if not context or context["current"]["blocked"]:
        return fields
    future = context.get("next")
    if not future or future["blocked"] or future["phase"] != context["current"]["phase"]:
        return fields
    target = future["dose"]
    if not dose or not target or dose.get("per_side") != target.get("per_side"):
        return fields
    if dose != context["current"]["dose"]:
        return fields  # A legacy card differs from exact dose authority; do not promise advancement.
    current_shape, current_numbers = _prescription_signature(prescription)
    future_shape, future_numbers = _prescription_signature(future["prescription"])
    if current_shape != future_shape:
        return fields  # A changed intent, tempo grammar or movement qualifier is not one work step.
    changes = [key for key in _WORK if dose.get(key) != target.get(key)]
    if not changes and dose == target and current_numbers == future_numbers:
        fields["maintenance_cue"] = f"Next D-{future['day']}: repeat {future['prescription'].rstrip('.')}."
        return fields
    # No unknown starting value, unit switch, simultaneous growth or reduced rest.
    if (len(changes) != 1 or not isinstance(dose.get(changes[0]), (int, float))
            or not isinstance(target.get(changes[0]), (int, float))):
        return fields
    if (len(current_numbers) != len(future_numbers)
            or sum(a != b for a, b in zip(current_numbers, future_numbers)) != 1):
        return fields  # Do not hide an extra change to tempo, HR percentage or another unparsed quantity.
    variable = changes[0]
    if (target[variable] <= dose[variable] or dose.get("load_unit") != target.get("load_unit")
            or (dose.get("rest") is not None and (target.get("rest") or 0) < dose["rest"])
            or dose.get("rpe") != target.get("rpe")):
        return fields
    if variable == "load":
        # Same conservative limit as the guarded load suggestion in exercise-log.ts.
        limit = 5 if dose.get("load_unit") == "% 1RM" else dose[variable] * 0.05
        if target[variable] - dose[variable] > limit + 1e-9:
            return fields
    if context["current"]["role"].get("category") == "conditioning":
        label = "rounds" if variable == "sets" else _label(variable, dose)
        fields["progression_rule"] = (
            f"Next D-{future['day']}: {label} to {_value(variable, target)} if recovered."
        )
    else:
        delta = {**dose, variable: target[variable] - dose[variable]}
        noun = {"reps": " reps/side" if dose.get("per_side") else " reps/set",
                "sets": " sets", "rounds": " rounds"}.get(variable, "")
        amount = _value(variable, delta)
        if delta[variable] == 1:
            noun = noun.replace(" reps", " rep").replace(" sets", " set").replace(" rounds", " round")
        fields["progression_rule"] = (
            f"Next D-{future['day']}: add {amount}{noun} if controlled and recovered."
        )
    fields["variable"] = variable
    fields["from"] = dose[variable]
    fields["to"] = target[variable]
    return fields


def _prescription_signature(text: str) -> tuple[str, tuple]:
    text = normalise_measures(text).lower().replace("×", "x")
    text = re.sub(r"(\d+(?:\.\d+)?)\s*" + _TIME,
                  lambda m: f"{_seconds(m[1], m[2]):g} sec", text, flags=re.I)
    text = re.sub(r"\bsets?\s*(?=x)", "", text)
    text = re.sub(r"\b(reps?|rounds?|holds?|carries)\b",
                  lambda m: {"rep": "reps", "round": "rounds", "hold": "holds"}.get(m[0], m[0]), text)
    numbers = tuple(float(n) for n in re.findall(r"\d+(?:\.\d+)?", text))
    shape = re.sub(r"\d+(?:\.\d+)?", "#", text)
    return re.sub(r"[\s,;:.@]", "", shape), numbers


def _label(variable: str, dose: dict) -> str:
    return {"duration": "duration", "work": "work time", "distance": "distance per carry",
            "reps": "reps per side" if dose.get("per_side") else "reps per set"}.get(variable, variable)


def _value(variable: str, dose: dict) -> str:
    value = dose[variable]
    if variable in {"duration", "work", "rest"}:
        return f"{value/60:g} min" if value >= 60 and value % 60 == 0 else f"{value:g} sec"
    suffix = {"distance": " m", "load": " " + dose.get("load_unit", "")}.get(variable, "")
    return f"{value:g}{suffix}"


def numerical_easier(dose: dict) -> str | None:
    """Reduce a single known work quantity; never shorten rest or infer a load."""
    for variable in ("sets", "rounds", "reps", "duration", "work", "distance"):
        value = dose.get(variable)
        if not isinstance(value, (int, float)) or value <= 1:
            continue
        reduced = value - 1 if variable in {"sets", "rounds", "reps"} else max(1, math.floor(value * 0.8))
        target = {**dose, variable: reduced}
        return f"Reduce {_label(variable, dose)} to {_value(variable, target)}."
    return None


def authored_progression_matches(rule: str, guidance: dict, target: dict) -> bool:
    """Keep precise bank wording only when its one numerical step is supported."""
    rule = normalise_measures(rule)
    variable = guidance.get("variable")
    if not variable:
        return False
    proposed = measured_dose(rule)
    if any(proposed[key] != target.get(key) for key in ("rest", "rpe", "load_unit") if key in proposed):
        return False
    additions = re.findall(r"\badd\s+(\d+(?:\.\d+)?)\s*(kg|lb|reps?|sets?|rounds?|m|min(?:utes?)?|sec(?:onds?)?)\b", rule, re.I)
    if additions:
        if len(additions) != 1:
            return False
        amount, unit = additions[0]
        field = {"rep": "reps", "reps": "reps", "set": "sets", "sets": "sets",
                 "round": "rounds", "rounds": "rounds", "m": "distance", "kg": "load", "lb": "load"}.get(unit.lower())
        if unit.lower().startswith(("min", "sec")):
            field = variable if variable in {"duration", "work"} else None
            amount = str(float(amount) * (60 if unit.lower().startswith("min") else 1))
        # "Add 5kg" is a delta, not a new 5kg absolute load. Still inspect any
        # separate absolute prescription so a second change cannot hide in it.
        remaining = re.sub(r"\badd\s+\d+(?:\.\d+)?\s*(?:kg|lb|reps?|sets?|rounds?|m|min(?:utes?)?|sec(?:onds?)?)\b",
                           "", rule, count=1, flags=re.I)
        additional = measured_dose(remaining)
        return (field == variable and float(amount) == guidance["to"] - guidance["from"]
                and all(additional[key] == target.get(key) for key in (*_WORK, "rest", "rpe", "load_unit") if key in additional))
    if variable not in proposed:
        if variable in {"duration", "work"} and (m := re.search(r"\bto\s+(\d+(?:\.\d+)?)\s*" + _TIME, rule, re.I)):
            proposed[variable] = _seconds(m[1], m[2])
        else:
            return False
    return all(proposed[key] == target.get(key) for key in _WORK if key in proposed)


def normalise_measures(text: str) -> str:
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
             "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
             "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
             "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100}
    text = re.sub(r"\b(" + "|".join(words) + r")\b", lambda m: str(words[m[1].lower()]), text, flags=re.I)
    text = re.sub(r"\bmet(?:re|er)s?\b", "m", text, flags=re.I)
    text = re.sub(r"\bkilograms?\b", "kg", text, flags=re.I)
    text = re.sub(r"\bpounds?\b", "lb", text, flags=re.I)
    return re.sub(r"\bmore\s+(?=reps?\b|sets?\b|rounds?\b)", "", text, flags=re.I)


def progress_rejection(rule: str, context: dict | None, *, dose_matches: bool = True) -> str | None:
    """Validate authored Progress. Never return an athlete-facing replacement.

    Quantities must describe the next resolved exposure's single safe change.
    Unparsed quantities/changes abstain rather than guessing from keywords.
    Quality-only advice can remain in recovery contexts at the same workload.
    """
    text = normalise_measures(re.sub(r"^\s*Progress(?:ion)?\s*:\s*", "", rule, flags=re.I)).strip()
    if not text:
        return None
    if re.search(r"\b(?:ignore|disregard|override|push through|work through)\b|\bcontinue\b.*\b(?:pain|dizz|symptom)", text, re.I):
        return "stop_condition_override"
    quality_text = re.sub(r"\b(?:do not|don't|never)\s+(?:add|increase|extend|raise|shorten)\b[^.;]*", "", text, flags=re.I)
    numbers = re.sub(r"\bD-\d+\b", "", quality_text, flags=re.I)
    change = re.search(r"\b(?:add|increase|extend|heavier|stronger|faster|extra|harder|higher|additional|advance|progress|raise|shorten|reduce|decrease|bump)\b", quality_text, re.I)
    if re.search(r"\bmore\s+(?:load|weight|resistance|reps|sets|rounds|effort|speed|distance|duration|force|intensity)\b", quality_text, re.I):
        change = True
    if not re.search(r"\d", numbers) and not change:
        # Restrict unmeasurable prose to an explicit quality/maintenance intent.
        if re.search(r"\b(?:control|controlled|balance|balanced|quality|technique|consistent|consistency|smooth|steadier|rhythm|refine|repeat|same|maintain|none)\b", text, re.I):
            return None
        return "unverifiable_progress"
    if not context or not dose_matches:
        return "missing_or_changed_dose_authority"
    current, future = context["current"], context.get("next")
    guidance = numerical_guidance(context, current["dose"], current["prescription"])
    if not future or "variable" not in guidance:
        return "unsupported_future_exposure"
    dates = [int(n) for n in re.findall(r"\bD-(\d+)\b", text, re.I)]
    if any(day != future["day"] for day in dates):
        return "unauthorised_future_session"
    if re.search(r"\b(?:tomorrow|tonight|daily|twice|another workout|extra session|next week)\b", text, re.I):
        return "unverified_future_session"
    weekdays = re.findall(r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", text, re.I)
    if any(day.lower() != str(future["role"].get("scheduled_day_hint") or "").lower() for day in weekdays):
        return "unauthorised_future_session"
    if not authored_progression_matches(text, guidance, future["dose"]):
        return "proposed_dose_mismatch"
    # Account for every numeric claim. The legacy matcher parses work, rest,
    # RPE and load, but cannot verify tempo, HR%, ranges or additional targets.
    residual = re.sub(r"\bD-\d+\b", "", text, flags=re.I)
    residual = re.sub(r"\badd\s+\d+(?:\.\d+)?\s*(?:kg|lb|reps?|sets?|rounds?|m|min(?:utes?)?|sec(?:onds?)?)\b", "", residual, flags=re.I)
    residual = re.sub(r"(?:increase\s+to\s+|progress\s+to\s+)?\d+\s*(?:sets?\s*)?[x×]\s*\d+\s*(?:reps?)?", "", residual, flags=re.I)
    residual = re.sub(r"(?:rest\s*[:=]?|RPE\s*)\d+(?:\.\d+)?\s*(?:s|sec|seconds?|minutes?|min)?\b", "", residual, flags=re.I)
    if re.search(r"\badd\b.*\b(?:add|increase|extend|shorten|reduce|raise|advance)\b", text, re.I):
        return "unverified_secondary_change"
    def checked_quantity(match):
        amount, unit = float(match[1]), match[2].lower()
        field = {"kg": "load", "lb": "load", "rep": "reps", "reps": "reps", "set": "sets",
                 "sets": "sets", "round": "rounds", "rounds": "rounds", "m": "distance"}.get(unit)
        if unit.startswith(("sec", "min")):
            field = guidance["variable"] if guidance["variable"] in {"duration", "work"} else None
            amount *= 60 if unit.startswith("min") else 1
        return "" if field and amount == future["dose"].get(field) else match[0]
    residual = re.sub(r"(\d+(?:\.\d+)?)\s*(kg|lb|reps?|sets?|rounds?|m|min(?:utes?)?|sec(?:onds?)?)\b",
                      checked_quantity, residual, flags=re.I)
    if re.search(r"\d|%|tempo|shorten|less rest|faster|stronger|heavier|lighter|double|triple|halve|cut|compress|increase|extend|reduce|decrease|raise|equipment|dumbbell|kettlebell|barbell|band|sled|bike|box|ball|pool|machine", residual, re.I):
        return "unverified_secondary_change"
    return None


def structured_dose(block: dict) -> dict:
    """Known current quantities for validation, never athlete-facing copy."""
    dose = {key: block[key] for key in ("sets", "reps", "rounds")
            if isinstance(block.get(key), (int, float))}
    for key in ("duration", "work", "distance", "rest"):
        measured = block.get(key)
        if not isinstance(measured, dict) or not isinstance(measured.get("value"), (int, float)):
            continue
        unit = str(measured.get("unit") or "").lower()
        if key == "distance" and unit in {"m", "meters", "metres"}:
            dose[key] = measured["value"]
        elif key != "distance" and unit in {"s", "sec", "seconds", "min", "minutes"}:
            dose[key] = measured["value"] * (60 if unit in {"min", "minutes"} else 1)
    effort = block.get("effort") if isinstance(block.get("effort"), dict) else {}
    if str(effort.get("method") or "").upper() == "RPE" and isinstance(effort.get("value"), (int, float)):
        dose["rpe"] = effort["value"]
    load = block.get("load") if isinstance(block.get("load"), dict) else {}
    if isinstance(load.get("value"), (int, float)) and load.get("unit") in {"kg", "lb", "lbs"}:
        dose.update(load=load["value"], load_unit="lb" if load["unit"].startswith("lb") else "kg")
    return dose
