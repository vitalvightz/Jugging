"""Reconcile rendered training against existing calendar and dose authorities.

This module never places a session, selects an exercise or authors a dose.
"""
from collections import Counter
import re

from .lead_summary import render_lead_summary
from .weekly_plan_render import render_weekly_schedule_section
from .calendar_context import role_d_day
from .render_authority import authoritative_render_for_role

_DAY = re.compile(r"^\s*(?:#{1,6}\s*)?D-(\d+)\s*\((\w+)\)\s*[—–-]\s*(.+)$", re.I)
_WEEK = re.compile(r"^\s*(?:#{1,6}\s*)?(?:GPP|SPP|TAPER|Week)\b.*\bWeek\s*\d+|^\s*#{1,6}\s*Week\s*\d+", re.I)
_PURPOSE = re.compile(r"^\s*Purpose\s*:", re.I)
_SUPPORT = re.compile(r"^\s*#{1,2}\s+(?:Nutrition|Recovery|Mindset|Rehab|Mobility|Coach Notes)\b", re.I)


def _blocks(text: str) -> list[tuple]:
    blocks = []
    current = None
    for line in str(text or "").splitlines():
        match = _DAY.match(line)
        if match:
            if current:
                blocks.append(current)
            current = [int(match[1]), match[2].lower(), match[3].strip().lower(), []]
        elif _WEEK.match(line) or _SUPPORT.match(line):
            if current:
                blocks.append(current)
            current = None
        elif current is not None and line.strip():
            # Coaching wording is AI-owned; programme identity and dose remain exact.
            if not re.match(r"^\s*(?:How|Cue|Coaching cues|Easier|Regression|Progress|Progression|Stop|Stop rule|"
                            r"Swap|Swaps|Substitution)\s*:", line, re.I):
                current[3].append(line.strip())
    if current:
        blocks.append(current)
    return [(d, weekday, label, tuple(body)) for d, weekday, label, body in blocks]


def programme_fidelity_findings(planning_brief: dict, text: str) -> list[dict]:
    expected = render_weekly_schedule_section(planning_brief=planning_brief, blocks=None)
    if not expected:
        return []  # Separate direct-countdown/open programme owner.
    required_blocks = _blocks(expected)
    # Purpose is coaching where the authority writes none; where the planner
    # wrote one (tactical/mental sessions) it stays part of the exact body.
    planner_purpose = {block[:3] for block in required_blocks if any(_PURPOSE.match(line) for line in block[3])}
    actual_blocks = [block if block[:3] in planner_purpose else
                     (*block[:3], tuple(line for line in block[3] if not _PURPOSE.match(line)))
                     for block in _blocks(text)]
    required = Counter(required_blocks)
    actual = Counter(actual_blocks)
    findings = []
    for week in (planning_brief.get("weekly_role_map") or {}).get("weeks") or []:
        for role in week.get("session_roles") or []:
            day = role_d_day(week, role)
            if day is not None and role.get("render_mandatory") is not False and role.get("category") in {"strength", "conditioning"} and "selected_exercise_assignments" in role:
                if authoritative_render_for_role(role, week=week, d_day=day, athlete_snapshot=planning_brief.get("athlete_snapshot")) is None:
                    findings.append({"code": "authoritative_programme_unpriced_session", "severity": "blocker", "confidence": "high",
                                     "scheduled_d_day": day, "message": "Scheduled S&C has no complete selected membership and effective dose."})
    if required == actual and required_blocks != actual_blocks:
        findings.append({"code": "authoritative_programme_order_changed", "severity": "blocker", "confidence": "high",
                         "message": "Appointment execution order differs from the authoritative programme."})
    required_weeks = Counter(line.strip() for line in expected.splitlines() if _WEEK.match(line))
    actual_weeks = Counter(line.strip() for line in text.splitlines() if _WEEK.match(line))
    if required_weeks != actual_weeks:
        findings.append({"code": "authoritative_programme_phase_changed", "severity": "blocker", "confidence": "high",
                         "message": "Week phase or countdown boundaries differ from the authoritative programme."})
    for block, count in (required - actual).items():
        findings.append({"code": "authoritative_programme_missing_or_changed", "severity": "blocker",
                         "confidence": "high", "scheduled_d_day": block[0], "weekday": block[1],
                         "message": "Scheduled appointment, selected membership or effective prescription differs from its authority.",
                         "expected_label": block[2], "count": count})
    for block, count in (actual - required).items():
        findings.append({"code": "authoritative_programme_extra_or_changed", "severity": "blocker",
                         "confidence": "high", "scheduled_d_day": block[0], "weekday": block[1],
                         "message": "Rendered appointment or prescription is not in the authoritative programme.",
                         "rendered_label": block[2], "count": count})
    return findings


def reconcile_authoritative_programme(planning_brief: dict, text: str) -> dict:
    from .exercise_coaching_source import source_coaching, validate_source_coaching
    original = text
    text, coaching_findings = validate_source_coaching(planning_brief, text)
    schedule = render_weekly_schedule_section(planning_brief=planning_brief, blocks=None)
    if not schedule:
        return {"text": text, "applied": text != original, "findings": [],
                "coaching_rejections": coaching_findings}
    from .exercise_coaching_source import (
        _DAY, _EXERCISE, _FIELDS, _LABEL, coaching_identity, coaching_occurrences, lookup_occurrence)
    authored = source_coaching(text)
    # Authoritative occurrences in the rebuilt schedule. An authored line may
    # replace bank coaching, never planner-decided Purpose text.
    scheduled = list(coaching_occurrences(schedule))
    peers = [item["key"] for item in scheduled]
    scheduled_fields = {item["key"]: item["fields"] for item in scheduled}
    # Replace only labelled display metadata at an exact occurrence. Never move
    # coaching to another day, appointment or exercise when correcting a dose.
    merged, pending = [], None
    day = title = None
    for line in schedule.splitlines():
        if header := _DAY.match(line):
            pending = None
            day, title = int(header[1] or header[2]), coaching_identity(header[3])
        elif exercise := _EXERCISE.match(line):
            key = (day, title, coaching_identity(exercise[1]))
            pending = lookup_occurrence(authored, key, peers)
            merged.append(line)
            if pending:
                own = scheduled_fields.get(key) or {}
                merged.extend(raw for raw in pending["lines"]
                              if not ("purpose" in own and _FIELDS[_LABEL.match(raw)[1].lower()] == "purpose"))
            continue
        elif line.startswith("#"):
            pending = None
        label = _LABEL.match(line)
        if label:
            field = _FIELDS[label[1].lower()]
            if field in {"progression_rule", "regression_options"} or (
                    pending and field in pending["fields"] and field != "purpose"):
                continue
        merged.append(line)
    schedule = "\n".join(merged)
    lines = str(text or "").splitlines()
    start = next((i for i, line in enumerate(lines) if _WEEK.match(line) or _DAY.match(line)), None)
    if start is None:
        # No parseable programme is not permission to retain guessed workouts.
        start = len(lines)
    end = next((i for i in range(start + 1, len(lines)) if _SUPPORT.match(lines[i])), len(lines))
    lead = render_lead_summary(planning_brief)
    athlete = planning_brief.get("athlete_snapshot") or {}
    frequency = athlete.get("training_frequency")
    facts = f"Requested weekly total: {frequency} standalone training sessions, including coach sessions." if frequency else ""
    weight_status = str(athlete.get("weight_cut_status") or "")
    if weight_status and weight_status != "known":
        facts += " Weight information is incomplete; no individual weight-cut target is assumed."
    prefix = "\n\n".join(p for p in ("# Fight Camp Plan", lead, facts) if p)
    corrected = "\n\n".join(p for p in (prefix, schedule, "\n".join(lines[end:]).strip()) if p)
    return {"text": corrected, "applied": corrected != original,
            "coaching_rejections": coaching_findings,
            "findings": programme_fidelity_findings(planning_brief, corrected),
            "corrected_finding_count": len(programme_fidelity_findings(planning_brief, text)),
            "authority": "scheduled_roles_and_resolved_prescriptions", "model_call_used": False}
