"""Dose-aware endurance evidence shared by calendar and final-output checks."""

from __future__ import annotations

import re
from collections.abc import Mapping

from .bank_authority import original_bank_entries
from .config import conditioning_dose_minutes

UNSAFE_STACKING_CODE = "substantial_endurance_strength_conflict"

# Continuous sessions of this size are training appointments, even at easy
# effort. Distance thresholds avoid inventing completion times. Short recovery
# flushes and interval strength/power work do not satisfy this contract.
_DISTANCE_FLOORS = {"run": 3000, "swim": 1000, "cycle": 10000, "row": 5000}
_MODALITIES = {
    "run": r"\b(?:run|running|jog|jogging)\b",
    "swim": r"\b(?:swim|swimming)\b",
    "cycle": r"\b(?:cycle|cycling|bike|biking|ride|riding)\b",
    "row": r"\b(?:row|rowing|rower|erg)\b",
}
_DISTANCE_METERS = {
    **dict.fromkeys(("m", "meter", "meters", "metre", "metres"), 1),
    **dict.fromkeys(("km", "kilometer", "kilometers", "kilometre", "kilometres"), 1000),
    **dict.fromkeys(("mi", "mile", "miles"), 1609.344),
    **dict.fromkeys(("yd", "yard", "yards"), 0.9144),
}
_DURATION_MINUTES = {
    **dict.fromkeys(("s", "sec", "secs", "second", "seconds"), 1 / 60),
    **dict.fromkeys(("m", "min", "mins", "minute", "minutes"), 1),
    **dict.fromkeys(("h", "hr", "hrs", "hour", "hours"), 60),
}
_DISTANCE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(" + "|".join(map(re.escape, _DISTANCE_METERS)) + r")\b",
    re.I,
)


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def substantial_endurance(item: Mapping) -> bool:
    """Use authored classification and current dose, with bank lookup for renders."""
    name = str(item.get("name") or item.get("display_name") or "").strip().rstrip(".")
    rows = original_bank_entries({"name": name, "slot_group": "conditioning_slots"})
    metadata = dict(rows[0]) if rows else {}
    metadata.update({k: v for k, v in item.items() if v is not None})
    text = " ".join(
        str(metadata.get(k) or "")
        for k in ("modality", "name", "display_name", "category", "primary_stressor", "context_modality")
    ).replace("_", " ")
    modality = next(
        (key for key, pattern in _MODALITIES.items() if re.search(pattern, text, re.I)),
        None,
    )
    classification = str(
        metadata.get("block_type") or metadata.get("category") or metadata.get("session_type") or ""
    ).lower()
    if modality is None and classification not in {"conditioning", "cardio", "aerobic", "endurance"}:
        return False
    dose = str(
        item.get("effective_prescription")
        or item.get("prescription")
        or item.get("base_prescription")
        or item.get("duration")
        or item.get("timing")
        or ""
    )
    # A rendered/realised shorter dose takes precedence over the bank dose.
    if dose or any(
        item.get(k) is not None for k in ("total_minutes", "duration_min", "distance_m", "work_sec")
    ):
        for key in (
            "total_minutes",
            "duration_min",
            "distance_m",
            "duration",
            "timing",
            "rounds",
            "effective_rounds",
            "work_sec",
            "rest_sec",
        ):
            if key not in item:
                metadata.pop(key, None)
        if dose:
            metadata["duration"] = dose
    duration = str(metadata.get("duration") or metadata.get("timing") or "")
    if re.search(
        r"\d\s*[x×]|\b(?:intervals?|rounds?|repeats?|emom|tabata|circuit)\b",
        duration,
        re.I,
    ):
        return False
    rounds = metadata.get("effective_rounds") or metadata.get("rounds") or 1
    if _number(rounds) > 1:
        return False
    minutes = conditioning_dose_minutes(metadata)
    if minutes is None and _number(metadata.get("work_sec")) > 0:
        minutes = _number(metadata["work_sec"]) / 60
    if minutes is None and (
        hours := re.search(r"\b(\d+(?:\.\d+)?)\s*(?:h|hours?)\b", duration, re.I)
    ):
        minutes = float(hours[1]) * 60
    distance = _number(metadata.get("distance_m"))
    if not distance and (match := _DISTANCE_RE.search(duration)):
        distance = float(match[1]) * _DISTANCE_METERS[match[2].lower()]
    return bool(
        (minutes is not None and minutes >= 30)
        or (modality is not None and distance >= _DISTANCE_FLOORS[modality])
    )


def role_has_substantial_endurance(role: Mapping) -> bool:
    return any(
        substantial_endurance(item)
        for item in role.get("selected_exercise_assignments") or []
        if isinstance(item, Mapping)
    )


def stacking_finding(profiles: list, *, day: str) -> list[dict]:
    """Ask the canonical same-day policy; adapters supply representation only."""
    from .combat_load_policy import evaluate_candidate_at_position, CalendarEvent

    for index, profile in enumerate(profiles):
        decision = evaluate_candidate_at_position(
            profile,
            candidate_position=0,
            events=[
                CalendarEvent(0, other)
                for i, other in enumerate(profiles)
                if i != index
            ],
        )
        if decision.reason_code == UNSAFE_STACKING_CODE:
            return [
                {
                    "code": UNSAFE_STACKING_CODE,
                    "severity": "blocker",
                    "confidence": "high",
                    "blocking": True,
                    "day": day,
                    "message": decision.reason,
                }
            ]
    return []


def _rendered_profiles(label: str, items: list[dict]) -> list:
    from .combat_load_policy import CalendarLoadProfile, LoadClass, DayOccupancy

    profiles = []
    strength = bool(re.search(r"\b(?:strength|s&c)\b", label, re.I)) or any(
        item.get("block_type") in {"strength", "strength_speed"} for item in items
    )
    if not strength:
        strength = (
            sum(
                bool(
                    original_bank_entries(
                        {
                            "name": str(item.get("name") or "").rstrip("."),
                            "slot_group": "strength_slots",
                        }
                    )
                )
                for item in items
            )
            >= 3
        )
    # As with an uncapped planner strength role, an unproven "touch" title
    # cannot waive meaningful strength ownership.
    if strength:
        profiles.append(
            CalendarLoadProfile(LoadClass.MEANINGFUL_STRENGTH, DayOccupancy.PHYSICAL)
        )
    if any(substantial_endurance(item) for item in items):
        profiles.append(
            CalendarLoadProfile(
                LoadClass.LOW_LOAD_AEROBIC,
                DayOccupancy.PHYSICAL,
                substantial_endurance=True,
            )
        )
    return profiles


_REALIZED_DOSE_FIELDS = (
    "effective_prescription", "prescription", "base_prescription", "duration",
    "timing", "total_minutes", "duration_min", "distance_m", "work_sec",
)


def _has_current_dose(record: Mapping) -> bool:
    for field in (*_REALIZED_DOSE_FIELDS, "prescription_text", "work", "distance"):
        value = record.get(field)
        if isinstance(value, str):
            if value.strip():
                return True
        elif isinstance(value, Mapping):
            if value.get("value") is not None:
                return True
        elif value is not None:
            return True
    return False


def _structured_workload_item(record: Mapping) -> dict:
    """Keep authored metadata while translating quantities into shared units."""
    item = {key: value for key, value in record.items() if value is not None}
    item["name"] = record.get("display_name") or record.get("name") or record.get("title")
    if text := str(record.get("prescription_text") or "").strip():
        item["effective_prescription"] = text
    duration = record.get("duration") or record.get("work") or record.get("planned_duration")
    for target, measured, factors in (
        ("duration_min", duration, _DURATION_MINUTES),
        ("distance_m", record.get("distance"), _DISTANCE_METERS),
    ):
        if not isinstance(measured, Mapping):
            continue
        value = _number(measured.get("value"))
        unit = str(measured.get("unit") or "").strip().lower()
        factor = factors.get(unit)
        if factor is not None:
            item[target] = value * factor
        if target == "duration_min":
            # The quantity field disambiguates "m": duration means minutes,
            # whereas the same alias in a distance quantity means metres.
            item["duration"] = f"{value * factor:g} min" if factor is not None else f"{value:g} {unit}"
    return item


def structured_stacking_findings(plan: dict) -> list[dict]:
    findings = []
    for wi, week in enumerate(plan.get("weeks") or []):
        for di, day in enumerate(week.get("days") or []):
            profiles = []
            for session in day.get("sessions") or []:
                items = []
                session_item = _structured_workload_item(session)
                if session.get("session_type") == "conditioning":
                    session_item["context_modality"] = " ".join(
                        str(session.get(key) or "")
                        for key in ("modality", "title", "objective", "primary_stressor")
                    )
                session_type = str(session.get("session_type") or "").strip().lower()
                session_endurance = session_type in {"conditioning", "cardio", "aerobic", "endurance"} or (
                    session_type in {"", "other", "unknown"} and bool(session.get("modality"))
                )
                for block in session.get("blocks") or []:
                    item = _structured_workload_item(block)
                    for field in ("modality", "category", "primary_stressor", "session_type", "rounds", "effective_rounds", "context_modality"):
                        if field not in item and field in session_item:
                            item[field] = session_item[field]
                    # A session budget is fallback evidence, never a replacement
                    # for a shortened exercise or a per-round interval dose.
                    if (session_endurance and item.get("block_type") in {None, "conditioning", "other"}
                            and not _has_current_dose(block)):
                        for field in _REALIZED_DOSE_FIELDS:
                            if field not in item and field in session_item:
                                item[field] = session_item[field]
                    items.append(item)
                if not items and session_endurance:
                    items.append(session_item)
                label = str(session.get("title") or "")
                if session.get("session_type") == "strength_power":
                    label += " strength"
                profiles.extend(_rendered_profiles(label, items))
            findings.extend(
                stacking_finding(
                    profiles,
                    day=str(
                        day.get("weekday") or day.get("countdown_label") or f"{wi}:{di}"
                    ),
                )
            )
    return findings


_WEEKDAYS = r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
_RAW_DAY = re.compile(
    rf"^\s*(?:#{{1,6}}\s*|[-*]\s*)?(?:D-(\d+)\s*\(({_WEEKDAYS})\)|({_WEEKDAYS}))\s*(?:[—–:-]|\(D-(\d+)\))\s*(.*)$",
    re.I,
)


def raw_stacking_findings(text: str) -> list[dict]:
    """Read actual day cards, independently of an optional weekly role map."""
    days: dict[tuple, list] = {}
    week = 0
    current = None
    for raw in str(text or "").splitlines():
        line = raw.replace("**", "")
        if re.match(
            r"^\s*(?:#{1,6}\s*)?(?:(?:GPP|SPP|TAPER).*\b)?Week\s+\d+", line, re.I
        ):
            week += 1
            current = None
        elif match := _RAW_DAY.match(line):
            key = (match[1] or match[4] or week, (match[2] or match[3]).lower())
            current = {"label": match[5], "items": []}
            days.setdefault(key, []).append(current)
        elif re.match(
            r"^\s*(?:#{1,6}\s*)?(?:4-Week|Progression Rules|Priority Hierarchy|Adjustment Rules|Rehab /|Session Cards|Weekly Rhythm)\b",
            line,
            re.I,
        ):
            current = None
        elif current is not None:
            if re.match(
                r"^\s*(?:How|Cue|Easier|Swap|Progress|Stop|Why|Coach note|Fallback)\s*:",
                line,
                re.I,
            ):
                continue
            if dose := re.match(r"^\s*Dose\s*:\s*(.*)", line, re.I):
                if current["items"]:
                    current["items"][-1]["prescription"] = dose[1]
            elif exercise := re.match(
                r"^\s*[-*•]\s+(.+?)(?:\s+[—–]\s+|:\s+|$)(.*)", line
            ):
                current["items"].append(
                    {"name": exercise[1].rstrip("."), "prescription": exercise[2]}
                )
    findings = []
    for key, sessions in days.items():
        profiles = [
            p for s in sessions for p in _rendered_profiles(s["label"], s["items"])
        ]
        findings.extend(stacking_finding(profiles, day=f"{key[0]}:{key[1]}"))
    return findings
