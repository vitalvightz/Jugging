"""Recover missing coaching on saved generated cards without changing doses."""
from copy import deepcopy
from typing import Any
import re

from fightcamp.calendar_context import role_d_day
from fightcamp.exercise_instructions import nested_rows, recover_execution_instructions
from fightcamp.coaching_copy import concise_coaching, is_null_progression
from fightcamp.exercise_coaching import (
    exercise_coaching, is_profile_progression, is_profile_cue, easier_rejection, clinical_coaching_rejection,
)
from fightcamp.exercise_progression import (
    build_progression_context, occurrence_key, progress_rejection, effective_exercise_prescription, structured_dose, measured_dose,
)
from fightcamp.structured_session_identity import deterministic_session_id, identity_tokens


def _matching_assignment(candidates, session, day):
    """Never borrow context from a second occurrence of the same exercise."""
    session_id = str(session.get("session_id") or "").lower()
    exact = [c for c in candidates if session_id == deterministic_session_id(c[0], day)]
    if len(exact) == 1:
        return exact[0]
    title = identity_tokens(session.get("title"))
    exact = [c for c in candidates if title and title == identity_tokens(c[0].get("athlete_facing_label"))]
    if len(exact) == 1:
        return exact[0]
    # Converter ids sometimes retain the role key; use it only when unique.
    exact = [c for c in candidates if c[0].get("role_key")
             and str(c[0]["role_key"]).lower() in session_id]
    return exact[0] if len(exact) == 1 else None


def _current_card_dose(block: dict) -> dict:
    # Compound/legacy cards retain their exact working dose as execution text.
    # Explicit edited statistic fields always take precedence over that metadata.
    return {**measured_dose(str(block.get("execution_instructions") or "")), **structured_dose(block)}


def _dose_matches(block: dict, role: dict, assignment: dict) -> bool:
    from .structured_plan_deterministic_fallback import _EFFORT_CLAUSE_RE, _lift_dose_fields
    from .structured_plan_faithfulness import _prescription_value
    prescription = effective_exercise_prescription(role, assignment)
    fields, _ = _lift_dose_fields(prescription)
    # Presentation-only lifts (qualitative rest, cluster rows, "7/10 effort"
    # read as RPE) are not part of this quantitative authority comparison.
    fields = {key: value for key, value in fields.items() if key not in ("rest_note", "components")}
    if "effort" in fields and not _EFFORT_CLAUSE_RE.search(prescription):
        fields.pop("effort")
    for key in ("sets", "reps", "rounds", "duration", "work", "distance", "load", "rest", "effort"):
        if block.get(key) not in (None, "", {}) and key not in fields:
            return False  # Extra current quantities are not covered by programme authority.
    if block.get("tempo"):
        tempo = re.search(r"tempo\s+(\d+)[-–](\d+)[-–](\d+|X)", prescription, re.I)
        if not tempo or tuple(str(block["tempo"].get(key)).upper() for key in (
                "eccentric", "pause_bottom", "concentric")) != tuple(value.upper() for value in tempo.groups()):
            return False
    return bool(fields) and all(_prescription_value(block.get(k)) == _prescription_value(v)
                                for k, v in fields.items())


def _recover_programming(block: dict, role: dict, assignment: dict, context: dict | None,
                         athlete: dict, brief: dict, source: dict | None = None) -> None:
    matches = _dose_matches(block, role, assignment)
    if source is not None:
        for key in ("progression_rule", "regression_options", "substitutions"):
            if source.get(key):
                block[key] = deepcopy(source[key])
            else:
                block.pop(key, None)
    rule = str(block.get("progression_rule") or "")
    if rule and (is_profile_progression(rule) or progress_rejection(rule, context, dose_matches=matches) or clinical_coaching_rejection(rule, brief)):
        block.pop("progression_rule", None)
    block["regression_options"] = [text for text in block.get("regression_options") or []
        if not easier_rejection(text, assignment, role, athlete, brief=brief,
                                current_dose=None if matches else _current_card_dose(block))]


def recover_selected_coaching(plan: dict[str, Any], brief: dict[str, Any],
                              progression_context: dict | None = None, *, source_text: str = "") -> dict[str, Any]:
    if brief.get("custom_plan"):
        return plan
    plan = recover_execution_instructions(plan)
    from fightcamp.exercise_coaching_source import (
        source_coaching, validate_source_coaching, coaching_identity, lookup_occurrence)
    validated_text, _ = validate_source_coaching(brief, source_text)
    authored = source_coaching(validated_text)
    card_keys = [
        (int(str(day.get("countdown_label"))[2:]) if str(day.get("countdown_label") or "")[2:].isdigit() else None,
         coaching_identity(session.get("title")), coaching_identity(block.get("display_name")))
        for week in nested_rows(plan, "weeks") for day in nested_rows(week, "days") if isinstance(day, dict)
        for session in nested_rows(day, "sessions") if isinstance(session, dict)
        for block in nested_rows(session, "blocks") if isinstance(block, dict)]
    selected = {}
    for week in (brief.get("weekly_role_map") or {}).get("weeks") or []:
        for role in week.get("session_roles") or []:
            day = role_d_day(week, role)
            if day is None:
                continue
            for assignment in role.get("selected_exercise_assignments") or []:
                name = str(assignment.get("name") or "").casefold()
                key = str(assignment.get("exercise_key") or "")
                candidate = (role, week, assignment)
                for identity in {name, key} - {""}:
                    selected.setdefault((day, identity), []).append(candidate)
    if progression_context is None:
        progression_context = build_progression_context(brief)
    # Never mutate the persisted object, which other services may still hold.
    recovered = deepcopy(plan)
    from .structured_plan_faithfulness import project_source_adjustments
    recovered = project_source_adjustments(recovered, validated_text)
    # Malformed saved weeks/days/sessions/blocks carry no coaching to recover.
    for week in nested_rows(recovered, "weeks"):
        for day in nested_rows(week, "days"):
            if not isinstance(day, dict):
                continue
            label = str(day.get("countdown_label") or "")
            offset = int(label[2:]) if label.startswith("D-") and label[2:].isdigit() else None
            for session in nested_rows(day, "sessions"):
                for block in nested_rows(session, "blocks"):
                    if not isinstance(block, dict) or block.get("athlete_change") or block.get("prescription_source") == "athlete":
                        continue
                    # Old converters put labelled adjustments in the cue list.
                    # Route before validation so React cannot resurrect a rejected rule.
                    cues = []
                    for cue in block.get("coaching_cues") or []:
                        match = re.match(r"^\s*(Progress(?:ion)?|Easier|Regression)\s*:\s*(.+)$", cue, re.I)
                        if not match:
                            cues.append(cue)
                        elif match[1].lower().startswith("progress"):
                            block.setdefault("progression_rule", match[2])
                        else:
                            block.setdefault("regression_options", []).append(match[2])
                    if block.get("coaching_cues"):
                        block["coaching_cues"] = cues
                    if is_null_progression(block.get("progression_rule")):
                        block.pop("progression_rule", None)  # "none this block" is no rule.
                    candidates = selected.get((offset, str(block.get("exercise_key") or ""))) or selected.get(
                        (offset, str(block.get("display_name") or "").casefold())
                    )
                    match = _matching_assignment(candidates or [], session, offset)
                    if match is None:
                        if block.get("block_type") in {"strength", "strength_speed", "plyometric_power", "speed", "accessory", "conditioning", "rehab"}:
                            if progress_rejection(str(block.get("progression_rule") or ""), None) or clinical_coaching_rejection(str(block.get("progression_rule") or ""), brief):
                                block.pop("progression_rule", None)
                            if block.get("regression_options"):
                                block["regression_options"] = [text for text in block["regression_options"]
                                    if not easier_rejection(text, {"name": block.get("display_name")}, {},
                                        brief.get("athlete_snapshot") or {}, brief=brief, current_dose=_current_card_dose(block))]
                        continue  # Ambiguous context cannot author or license a number.
                    role, source_week, assignment = match
                    programming_context = progression_context.get(occurrence_key(role, assignment, offset))
                    fields = exercise_coaching(
                        assignment, role,
                        progression_context=programming_context,
                        phase=str(source_week.get("phase") or source_week.get("phase_label") or ""),
                        d_day=offset, athlete=brief.get("athlete_snapshot"),
                    )
                    authored_cue = assignment.get("coaching_cues") or re.search(
                        r"^\s*(?:Cue|Coaching cues)\s*:", str(assignment.get("coaching_notes") or ""), re.I | re.M)
                    if authored_cue and all(is_profile_cue(cue) for cue in block.get("coaching_cues") or []):
                        block["coaching_cues"] = deepcopy(fields["coaching_cues"])
                    for field, value in (fields or {}).items():
                        if field in {"progression_rule", "regression_options"}:
                            continue  # Adjustment notes are owned by the final-session LLM.
                        if value and not block.get(field):
                            block[field] = deepcopy(value)
                    source = lookup_occurrence(authored, (offset, coaching_identity(session.get("title")),
                                                          coaching_identity(block.get("display_name"))), card_keys)
                    _recover_programming(block, role, assignment, programming_context,
                                         brief.get("athlete_snapshot") or {}, brief,
                                         source["fields"] if source else None)
                    compact = concise_coaching({k: block.get(k) for k in (
                        "coaching_cues", "regression_options", "stop_rules")})
                    block.update(compact)
    return recovered


def validate_final_coaching(result: dict[str, Any]) -> dict[str, Any]:
    """Apply the same rejection rules to saved text and saved cards."""
    from fightcamp.exercise_coaching_source import validate_source_coaching
    brief = result.get("planning_brief") or {}
    if isinstance(brief, str):
        import json
        try:
            brief = json.loads(brief)
        except (TypeError, ValueError):
            brief = {}
    if not isinstance(brief, dict):
        brief = {}
    if brief.get("custom_plan"):
        return result
    source = str(result.get("final_plan_text") or result.get("plan_text") or "")
    text, findings = validate_source_coaching(brief, source)
    validated = {**result}
    for key in ("final_plan_text", "plan_text"):
        if result.get(key):
            validated[key], _ = validate_source_coaching(brief, str(result[key]))
    if isinstance(result.get("structured_plan"), dict):
        from .services.effective_structured_plan import _without_athlete_change_claims
        validated["structured_plan"] = recover_selected_coaching(
            _without_athlete_change_claims(result["structured_plan"]), brief, source_text=text)
        if validated["structured_plan"].get("raw_markdown_fallback"):
            validated["structured_plan"]["raw_markdown_fallback"], _ = validate_source_coaching(
                brief, str(validated["structured_plan"]["raw_markdown_fallback"]))
    if findings:
        validated["stage2_validator_report"] = {**(result.get("stage2_validator_report") or {}),
                                                "exercise_coaching": findings}
    return validated
