"""Exercise execution guidance, independent of dose and calendar authority.

Movement profiles supply execution and stop guidance only. The final-session
LLM owns Progress and Easier; recovery never synthesizes those adjustments.
Unknown movements keep their authored guidance without invented load targets.
"""
from __future__ import annotations

import re
from typing import Any
from .exercise_instructions import exercise_instructions
from .coaching_copy import concise_coaching, is_null_progression
from .exercise_progression import (
    normalise_measures, progress_rejection, progression_prohibited,
)


# Cue, easier execution, legacy quality rule, stop criterion. The third value
# only identifies #2880's persisted wording for migration; it is never emitted
# as Progress. More specific matches precede movement families.
_PROFILES = (
    (r"swimming|freestyle", (
        "Keep the stroke relaxed and breathe regularly; avoid breath holds.",
        "Slow the stroke and relax the pull while keeping breathing comfortable.",
        "Keep the same swim dose; aim for an even stroke rhythm from start to finish.",
        "End the swim if shoulder pain, dizziness or breathing difficulty appears.",
    )),
    (r"easy assault bike|nasal jog|aerobic", (
        "Keep a relaxed rhythm and breathing comfortable throughout the aerobic work.",
        "Ease the pace or resistance until breathing settles; do not chase the effort target.",
        "At the same duration and effort ceiling, aim for a steadier pace without breathing strain.",
        "End the work if dizziness, chest discomfort or unusual breathlessness appears.",
    )),
    (r"cossack", (
        "Sit into the working hip with the heel grounded and knee tracking the toes.",
        "Use a shallower pain-free depth; keep the heel grounded.",
        "Once every rep is stable and pain-free, improve control into the same depth at the prescribed dose.",
        "End the set if knee or groin pain appears or the heel lifts.",
    )),
    (r"single-leg trx.*hold", (
        "Keep the pelvis level and use the straps to steady the single-leg position.",
        "Take more support through the straps without extending the hold.",
        "Once the holds stay level and pain-free, aim for less sway at the same hold dose.",
        "End the hold if joint pain appears or the pelvis drops despite strap support.",
    )),
    (r"staggered stance hold|standing stance", (
        "Set a balanced stance with soft knees and ribs stacked over the pelvis.",
        "Use a more comfortable stance width and keep the position relaxed.",
        "Once the stance stays steady, aim for even pressure through both feet at the same dose.",
        "End the hold if pain appears or you cannot keep a balanced stance.",
    )),
    (r"lateral bound", (
        "Land quietly, control the knee and reset the stance before the next bound.",
        "Shorten the bound to a distance you can land and balance cleanly.",
        "Once every landing is controlled, aim for a cleaner stance reset at the same bound distance and dose.",
        "End the set if knee or ankle pain appears or landings become unstable.",
    )),
    (r"jump|hop|bound", (
        "Stick each landing with the knee tracking the toes; reset before the next take-off.",
        "Reduce jump distance or height until each landing is quiet and stable.",
        "Once every landing is stable, aim for more repeatable take-offs at the same height, distance and dose.",
        "End the set if pain appears, jump quality drops or you cannot stick the landing.",
    )),
    (r"sled push.*speed", (
        "Drive the sled with quick steps and a braced trunk; keep each acceleration crisp.",
        "Use less sled resistance so steps stay quick without changing the work or rest.",
        "Once all accelerations stay crisp, aim for more even step speed at the same sled load and dose.",
        "End the effort if speed drops, the trunk collapses or pain appears.",
    )),
    (r"sled pull|sled drag|carry", (
        "Keep the trunk braced and steps steady; move the load without twisting or jerking.",
        "Reduce the carried or sled load until steps are steady.",
        "Once every length is controlled, aim for a smoother unbroken rhythm at the same load and distance.",
        "End the effort if pain appears or you cannot keep the trunk and steps controlled.",
    )),
    (r"thruster|push press|swing", (
        "Drive through the legs and keep the trunk braced; finish each rep without grinding.",
        "Use a lighter load so the same movement stays crisp and controlled.",
        "Once all reps stay crisp, aim for more consistent rep speed at the same load and dose.",
        "End the set if speed drops, trunk control fails or pain appears.",
    )),
    (r"bag burst|heavy bag|boxing.*burst|pocket frame|pull-counter|entry.*exit|punch|shadowbox", (
        "Keep punches balanced, return to guard and reset the stance between exchanges.",
        "Ease punch force and simplify the exchange until balance and guard return are clean.",
        "Once each exchange stays balanced, aim for a cleaner guard return at the same effort ceiling and dose.",
        "End the effort if punch speed, guard return or balance deteriorates, or pain appears.",
    )),
    (r"first.step|acceleration|sprint", (
        "Push off a stable stance and regain balance before the next effort.",
        "Ease acceleration until you can brake and reset under control.",
        "Once every reset is balanced, aim for more consistent first-step timing at the same dose.",
        "End the effort if speed drops, braking becomes uncontrolled or pain appears.",
    )),
    (r"throw|med.*ball", (
        "Drive the throw from the legs and trunk; reset your balance before the next release.",
        "Use a lighter ball and reduce throw force until the release stays controlled.",
        "Once every release is balanced, aim for a more consistent throw at the same ball weight and dose.",
        "End the set if throw speed drops, balance is lost or shoulder or back pain appears.",
    )),
    (r"squat|deadlift|\brdl\b|\brow\b|\bpress\b|pull.up", (
        "Keep the trunk braced and the movement controlled; avoid grinding repetitions.",
        "Use a lighter load or more assistance through the same pain-free movement.",
        "Once every rep stays controlled, aim for more consistent movement at the same load and dose.",
        "End the set if pain appears or you lose trunk or joint control.",
    )),
)


def exercise_coaching(
    assignment: dict[str, Any], role: dict[str, Any], *,
    phase: str = "", d_day: int | None = None,
    athlete: dict[str, Any] | None = None,
    progression_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return concise coaching for this selected exercise and athlete context.

    Taper, final fortnight, active injury, high fatigue and weight-cut risk
    prohibit advancement; authored quality guidance may keep the same dose. Easier never asks the athlete
    to push through a stop criterion. It never rewrites the prescribed dose.
    """
    athlete = athlete or {}
    name = str(assignment.get("name") or "")
    fields: dict[str, Any] = {}
    explanation = exercise_instructions(assignment)
    if explanation:
        fields["execution_instructions"] = explanation
    for key in ("coaching_cues", "regression_options", "stop_rules"):
        value = assignment.get(key)
        fields[key] = ([value] if isinstance(value, str) and value.strip() else
                       list(value) if isinstance(value, list) else [])
    if isinstance(assignment.get("progression_rule"), str) and assignment["progression_rule"].strip():
        fields["progression_rule"] = assignment["progression_rule"]
    # Keep authored safety criteria in the safety field, rather than burying
    # "Stop..." halfway through a cue. Notes remain evidence, never a new dose.
    notes = str(assignment.get("coaching_notes") or "").strip()
    labels = {"cue": "coaching_cues", "coaching cues": "coaching_cues",
              "how": "execution_instructions",
              "easier": "regression_options", "regression": "regression_options",
              "progress": "progression_rule", "progression": "progression_rule",
              "stop": "stop_rules", "stop rule": "stop_rules"}
    for line in notes.splitlines():
        labelled = re.match(r"^\s*([A-Za-z ]+)\s*:\s*(.+)$", line)
        if labelled and labelled[1].lower() in labels:
            key = labels[labelled[1].lower()]
            if key == "progression_rule" and is_null_progression(labelled[2]):
                continue
            if key in {"progression_rule", "execution_instructions"}:
                fields.setdefault(key, labelled[2])
            elif labelled[2] not in fields[key]:
                fields[key].append(labelled[2])
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", line.strip()):
            if sentence:
                embedded_stop = re.search(r"\bstop\s+(?:the\b|if\b|on\b|when\b|for\b|immediately\b).*$", sentence, re.I)
                if embedded_stop:
                    stop = embedded_stop[0]
                    if stop not in fields["stop_rules"]:
                        fields["stop_rules"].append(stop)
                    sentence = sentence[:embedded_stop.start()].strip(" ;,:")
                    if not sentence:
                        continue
                key = "stop_rules" if re.match(r"stop\b|end\b", sentence, re.I) else "coaching_cues"
                if sentence not in fields[key]:
                    fields[key].append(sentence)
    if assignment.get("slot_group") not in {"strength_slots", "conditioning_slots", "priority_microdose"}:
        return concise_coaching(fields)
    # Bank notes describe execution, not a second sets/reps/load authority.
    # An old "3-5 sets" cue must not contradict a resolved two-set session.
    # Keep movement cues and safety instructions; the existing profile below
    # supplies execution guidance when a dose-only note was all the bank had.
    fields["coaching_cues"] = [cue for cue in fields["coaching_cues"] if not has_measurable_step(cue)]
    phase = str(phase or role.get("phase") or assignment.get("source_phase") or "").upper()
    authored_progress = fields.pop("progression_rule", None)
    if progression_context and progression_prohibited(role, assignment, phase, d_day, athlete):
        progression_context = {**progression_context,
                               "current": {**progression_context["current"], "blocked": True}}
    profile = next((p for pattern, p in _PROFILES if re.search(pattern, name, re.I)), None)
    if profile is not None:
        cue, easier, _, stop = profile
        if not fields["coaching_cues"]:
            fields["coaching_cues"] = [cue]
        if not fields["stop_rules"]:
            fields["stop_rules"] = [stop]
    # Progress and Easier belong to the final-session LLM. Carry explicit source
    # instructions, but never manufacture an increment or a reduced duration.
    if authored_progress and not is_profile_progression(authored_progress) and not progress_rejection(authored_progress, progression_context):
        fields["progression_rule"] = authored_progress
    fields["regression_options"] = [text for text in fields["regression_options"]
                                    if not easier_rejection(text, assignment, role, athlete)]
    return concise_coaching(fields)


def is_profile_progression(text: str) -> bool:
    """Identify only #2880's exact generated quality rules, never athlete prose."""
    return text.strip() in {profile[2] for _, profile in _PROFILES}


def is_profile_regression(text: str) -> bool:
    return text.strip() in {profile[1] for _, profile in _PROFILES}


def is_profile_cue(text: str) -> bool:
    return text.removeprefix("Cue: ").strip() in {profile[0] for _, profile in _PROFILES}


def has_measurable_step(text: str) -> bool:
    return bool(re.search(r"\d\s*(?:kg\b|lb\b|%|[x×]\s*\d|sets?\b|reps?\b|rounds?\b|m\b|min\b|minutes?\b|sec\b|seconds?\b)|RPE\s*\d", normalise_measures(text), re.I))


def is_execution_quality_instruction(text: str) -> bool:
    return not has_measurable_step(text) and not re.search(
        r"\b(?:add|extend|heavier|stronger)\b|\bincrease\s+(?:load|weight|volume|reps|sets|rounds|speed|effort|intensity)|"
        r"\b(?:advance|progress)\s+to\b|\bshorten\s+(?:the\s+)?rest", text, re.I)


def coaching_lines(fields: dict[str, Any]) -> list[str]:
    lines = [f"  How: {fields['execution_instructions']}"] if fields.get("execution_instructions") else []
    lines.extend(f"  Cue: {cue}" for cue in fields.get("coaching_cues") or [])
    lines.extend(f"  Easier: {text}" for text in fields.get("regression_options") or [])
    if fields.get("progression_rule"):
        lines.append(f"  Progress: {fields['progression_rule']}")
    for text in fields.get("stop_rules") or []:
        trigger = re.sub(r"^Stop(?: rule)?:\s*", "", text, flags=re.I)
        lines.append(f"  Stop: {trigger}")
    return lines


def easier_rejection(text: str, assignment: dict, role: dict, athlete: dict, *, brief: dict | None = None, current_dose: dict | None = None) -> str | None:
    """Reject only an Easier line that is unsafe or contradicts the dose.

    Easier is optional AI advice, never an edit to the scheduled prescription.
    Its wording is the model's; only stop-rule overrides, clinical restrictions
    and numbers above the prescribed dose are rejected.
    """
    from .exercise_progression import effective_exercise_prescription, measured_dose
    value = normalise_measures(text)
    positive = re.sub(r"\b(?:do not|don't|never)\s+(?:continue|push|work)\b[^.;]*", "", value, flags=re.I)
    if re.search(r"\b(?:ignore|disregard|override|push through|work through)\b|\bcontinue\b.*\b(?:pain|dizz|symptom)|\bdespite\b.*\b(?:pain|stop|restriction)", positive, re.I):
        return "stop_condition_override"
    if clinical_coaching_rejection(text, brief or {}):
        return "clinical_restriction"
    if re.search(r"\d", value):
        current = current_dose if current_dose is not None else measured_dose(effective_exercise_prescription(role, assignment), assignment)
        proposed = measured_dose(value)
        # Explicit reductions such as "Reduce sets to 2" are not full doses.
        for label, field in (("sets?", "sets"), ("reps?(?: per set)?|repetitions?", "reps"), ("rounds?", "rounds")):
            if m := re.search(r"(?:" + label + r")\s+(?:to\s+)(\d+)\b", value, re.I):
                proposed[field] = int(m[1])
        if m := re.search(r"\b(?:use|to)\s+(\d+(?:\.\d+)?)\s*(sec(?:onds?)?|s|min(?:utes?)?)\b", value, re.I):
            field = "work" if "work" in current else "duration"
            proposed[field] = float(m[1]) * (60 if m[2].lower().startswith("min") else 1)
        if not any(k in proposed for k in ("sets", "reps", "rounds", "load", "duration", "work", "distance")):
            return "unverifiable_easier_dose"
        for key, quantity in proposed.items():
            if key == "per_side":
                continue
            if key == "load_unit":
                if quantity != current.get(key):
                    return "unknown_easier_load"
            elif not isinstance(quantity, (int, float)) or key not in current:
                return "unknown_easier_dose"
            elif (quantity < current[key] if key == "rest" else quantity > current[key]) or quantity <= 0:
                return "increased_easier_dose"
        # Every quantity must be accounted for, including a second instruction
        # the whole-dose parser does not otherwise recognise.
        residual = re.sub(r"\d+\s*(?:sets?\s*)?[x×]\s*\d+", "", value, flags=re.I)
        residual = re.sub(r"(?:sets?|reps?(?: per set)?|repetitions?|rounds?)\s+to\s+\d+", "", residual, flags=re.I)
        residual = re.sub(r"(?:rest\s*[:=]?|RPE\s*)\d+(?:\.\d+)?\s*(?:s|sec|seconds?|minutes?|min)?\b", "", residual, flags=re.I)
        residual = re.sub(r"(?:instead of|from)\s+(\d+)",
                          lambda m: "" if float(m[1]) in current.values() else m[0], residual, flags=re.I)
        def checked_quantity(match):
            number, unit = float(match[1]), match[2].lower()
            key = {"kg": "load", "lb": "load", "reps": "reps", "rep": "reps",
                   "sets": "sets", "set": "sets", "round": "rounds", "rounds": "rounds", "m": "distance"}.get(unit)
            if unit.startswith(("sec", "min")) or unit == "s":
                key = "work" if "work" in proposed else "duration"
                number *= 60 if unit.startswith("min") else 1
            return "" if key and proposed.get(key) == number else match[0]
        residual = re.sub(r"(\d+(?:\.\d+)?)\s*(kg|lb|reps?|sets?|rounds?|m|min(?:utes?)?|sec(?:onds?)?|s)\b",
                          checked_quantity, residual, flags=re.I)
        if re.search(r"\d", residual):
            return "unverified_secondary_easier_change"
        # Tempo/percentages/ranges are not measured by this parser.
        if re.search(r"tempo|%|\d\s*[-–]\s*\d|\bby\s+\d", value, re.I):
            return "unverifiable_easier_change"
    return None


def clinical_coaching_rejection(text: str, brief: dict) -> str | None:
    """Reuse clinical restrictions for both Progress and Easier wording."""
    if brief.get("restrictions"):
        from .stage2_validator import _find_restricted_hits
        if _find_restricted_hits(brief, [text]):
            return "clinical_restriction"
    return None
