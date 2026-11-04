from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import math
import re
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
try:
    from dateutil.tz import gettz
except ImportError:  # pragma: no cover - fallback for minimal runtimes
    def gettz(_: str | None):
        return None

from .guided_injury_display import (
    is_clean_guided_display_location,
    strip_guided_laterality,
)
from .guided_injury_resolver import resolve_guided_injury_entry
from .injury_guard import INJURY_TYPE_SEVERITY, SEVERITY_RANK, normalize_severity
from .injury_negation import remove_negated_phrases
from .injury_registry import SURFACE_TISSUE_TYPES
from .injury_formatting import parse_injuries_and_restrictions, parse_injury_entry
from .normalization import normalize_injury_marker as _normalize_injury_marker
from .normalization import normalize_label as _normalize_label
from .restriction_parsing import ParsedRestriction, parse_restriction_entry


def _normalize_list(field: str | None) -> list[str]:
    return [w.strip().lower() for w in field.split(",") if w.strip()] if field else []


_EMPTY_INJURY_MARKERS = {
    "none",
    "no",
    "no injury",
    "no injuries",
    "n/a",
    "na",
    "nil",
    "none reported",
    "none noted",
    "no issues",
}


_EMPTY_INJURY_MARKERS_NORMALIZED = {
    _normalize_injury_marker(marker) for marker in _EMPTY_INJURY_MARKERS
}


def normalize_injury_text(raw: str | None) -> str:
    if not raw:
        return ""
    cleaned = raw.strip()
    if not cleaned:
        return ""
    normalized_full = _normalize_injury_marker(cleaned)
    if normalized_full in _EMPTY_INJURY_MARKERS_NORMALIZED:
        return ""

    parts = [
        part.strip()
        for part in re.split(r"\s*(?:,|;|/|\+|\n)\s*", cleaned, flags=re.IGNORECASE)
        if part.strip()
    ]
    remaining: list[str] = []
    for part in parts:
        normalized_part = _normalize_injury_marker(part)
        if not normalized_part:
            continue
        if normalized_part in _EMPTY_INJURY_MARKERS_NORMALIZED:
            continue
        remaining.append(part)
    if not remaining:
        return ""
    return ", ".join(remaining)


_CRITICAL_LABEL_ALIASES = {
    _normalize_label("When is your next fight?"): {
        _normalize_label("Fight date"),
        _normalize_label("Next fight date"),
        _normalize_label("Date of next fight"),
        _normalize_label("When is the fight?"),
    },
    _normalize_label("Fighting Style (Technical)"): {
        _normalize_label("Technical style"),
        _normalize_label("Primary fighting style"),
        _normalize_label("Combat style (technical)"),
    },
    _normalize_label("Fighting Style (Tactical)"): {
        _normalize_label("Tactical style"),
        _normalize_label("Fight style (tactical)"),
        _normalize_label("Combat style (tactical)"),
    },
    _normalize_label("Weekly Training Frequency"): {
        _normalize_label("Training Days per Week"),
        _normalize_label("Training frequency"),
        _normalize_label("Sessions per week"),
        _normalize_label("How many times do you train per week"),
    },
    _normalize_label("Training Availability"): {
        _normalize_label("Availability"),
        _normalize_label("Available training days"),
        _normalize_label("Days available to train"),
    },
    _normalize_label("Hard Sparring Days"): {
        _normalize_label("Hard sparring"),
        _normalize_label("Hard sparring days"),
        _normalize_label("Live sparring days"),
    },
    _normalize_label("Support Work Days"): {
        _normalize_label("Support work days"),
        _normalize_label("Light Combat days"),
        _normalize_label("S&C-compatible slots"),
        _normalize_label("Technical days"),
        _normalize_label("Technical skill days"),
        _normalize_label("Lighter skill days"),
        _normalize_label("Technical / lighter skill days"),
    },
    _normalize_label("Any injuries or areas you need to work around?"): {
        _normalize_label("Injuries"),
        _normalize_label("Current injuries"),
        _normalize_label("Injuries or restrictions"),
        _normalize_label("Anything to work around"),
    },
    _normalize_label("Athlete Time Zone"): {
        _normalize_label("Athlete Timezone"),
        _normalize_label("Time Zone"),
        _normalize_label("Timezone"),
        _normalize_label("Athlete UTC Offset"),
        _normalize_label("UTC Offset"),
    },
    _normalize_label("Athlete Locale"): {
        _normalize_label("Locale"),
        _normalize_label("Athlete Region"),
        _normalize_label("Region"),
    },
}
_DATE_ONLY_PATTERN = re.compile(r"^(?:\d{4}[-/]\d{2}[-/]\d{2}|\d{2}/\d{2}/\d{4})$")
_UTC_OFFSET_PATTERN = re.compile(
    r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$",
    re.IGNORECASE,
)
_EXPLICIT_TZ_SUFFIX_PATTERN = re.compile(r"(?:Z|[+-]\d{2}(?::?\d{2})?)$")
_PLATFORM_DEFAULT_TIMEZONE = timezone.utc

ProvenanceSource = Literal["user_supplied", "system_inferred", "defaulted_missing"]


def _field_matches_label(field_label: str, target_label: str) -> bool:
    normalized_target = _normalize_label(target_label)
    normalized_field = _normalize_label(field_label)
    if not normalized_target or not normalized_field:
        return False
    if normalized_field == normalized_target:
        return True
    return normalized_field in _CRITICAL_LABEL_ALIASES.get(normalized_target, set())


def _find_field(label: str, fields: list[dict]) -> dict | None:
    exact_target = label.strip()
    for entry in fields:
        if str(entry.get("label", "")).strip() == exact_target:
            return entry
    for entry in fields:
        if _field_matches_label(entry.get("label", ""), label):
            return entry
    return None


def _extract_value(field: dict) -> str:
    value = field.get("value")
    if isinstance(value, list):
        if "options" in field:
            selected_ids = {str(item) for item in value}
            return ", ".join(
                [
                    str(opt.get("text", "")).strip()
                    for opt in field["options"]
                    if str(opt.get("id")) in selected_ids and str(opt.get("text", "")).strip()
                ]
            )
        return ", ".join(str(v) for v in value)
    return str(value).strip() if value is not None else ""


def get_value(label: str, fields: list[dict]) -> str:
    field = _find_field(label, fields)
    return _extract_value(field) if field else ""


def _extract_date_value(field: dict) -> str:
    value = field.get("value")
    if isinstance(value, dict):
        for key in ("date", "value", "text", "label"):
            if key in value and value[key] is not None:
                return str(value[key]).strip()
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value).strip() if value is not None else ""


def get_date_value(label: str, fields: list[dict]) -> str:
    field = _find_field(label, fields)
    return _extract_date_value(field) if field else ""


def _parse_fight_datetime(value: str) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _resolve_timezone(value: str | None) -> timezone | ZoneInfo | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if normalized.upper() in {"UTC", "GMT", "Z"}:
        return timezone.utc

    try:
        return ZoneInfo(normalized)
    except ZoneInfoNotFoundError:
        pass

    dateutil_tz = gettz(normalized)
    if dateutil_tz is not None:
        return dateutil_tz

    offset_match = _UTC_OFFSET_PATTERN.match(normalized)
    if not offset_match:
        return None

    sign, hours_text, minutes_text = offset_match.groups()
    hours = int(hours_text)
    minutes = int(minutes_text or "0")
    if hours > 23 or minutes > 59:
        return None

    offset = timedelta(hours=hours, minutes=minutes)
    if sign == "-":
        offset = -offset
    return timezone(offset)


def _business_timezone(athlete_timezone: str | None = None) -> timezone | ZoneInfo:
    resolved = _resolve_timezone(athlete_timezone)
    if resolved is not None:
        return resolved
    return _PLATFORM_DEFAULT_TIMEZONE


def _normalized_timezone_name(tzinfo: timezone | ZoneInfo) -> str:
    if tzinfo == timezone.utc:
        return "UTC"
    if isinstance(tzinfo, ZoneInfo):
        return tzinfo.key
    name = tzinfo.tzname(None)
    return name or "UTC"


def _platform_default_timezone_name() -> str:
    return _normalized_timezone_name(_PLATFORM_DEFAULT_TIMEZONE)


def _athlete_calendar_now(
    athlete_timezone: str | None = None, *, now_utc: datetime | None = None
) -> datetime:
    tzinfo = _business_timezone(athlete_timezone)
    reference_utc = now_utc or _utc_now()
    if reference_utc.tzinfo is None:
        reference_utc = reference_utc.replace(tzinfo=timezone.utc)
    else:
        reference_utc = reference_utc.astimezone(timezone.utc)
    return reference_utc.astimezone(tzinfo).replace(tzinfo=None)


def normalize_days_until_fight(days_until_fight: int | None) -> int | None:
    if not isinstance(days_until_fight, int):
        return None
    return days_until_fight if days_until_fight >= 0 else None


def is_short_notice_days(days_until_fight: int | None) -> bool:
    return isinstance(days_until_fight, int) and 0 <= days_until_fight <= 14


_PLAN_FIELD_LABELS = {
    "full_name": "Full name",
    "age": "Age",
    "weight": "Weight (kg)",
    "target_weight": "Target Weight (kg)",
    "height": "Height (cm)",
    "fighting_style_technical": "Fighting Style (Technical)",
    "fighting_style_tactical": "Fighting Style (Tactical)",
    "stance": "Stance",
    "status": "Professional Status",
    "record": "Current Record",
    "athlete_timezone": "Athlete Time Zone",
    "athlete_locale": "Athlete Locale",
    "rounds_format": "Rounds x Minutes",
    "frequency_raw": "Weekly Training Frequency",
    "fatigue": "Fatigue Level",
    "equipment_access": "Equipment Access",
    "available_days": "Training Availability",
    "hard_sparring_days_raw": "Hard Sparring Days",
    "support_work_days_raw": "Support Work Days",
    "key_goals": "What are your key performance goals?",
    "primary_goal": "Primary goal",
    "weak_areas": "Where do you feel weakest right now?",
    "primary_weak_area": "Primary weak area",
    "goal_weakness_collision_detail": "Goal/weak-area collision detail",
    "goal_weakness_collision_tags": "Goal/weak-area collision tags",
    "goal_weakness_collision_details": "Goal/weak-area collision details",
    "training_preference": "Do you prefer certain training styles?",
    "mental_block": "Do you struggle with any mental blockers or mindset challenges?",
    "notes": "Are there any parts of your previous plan you hated or loved?",
}

_PLAN_FIELD_LABEL_FALLBACKS = {
    "weight": ("Weight",),
    "target_weight": ("Target Weight",),
}


def _extract_fields(data: dict) -> list[dict]:
    fields = data.get("data", {}).get("fields") if isinstance(data, dict) else None
    if not isinstance(fields, list):
        raise ValueError("payload missing required data.fields list")
    return fields


def _get_plan_field_values(fields: list[dict]) -> dict[str, str]:
    values: dict[str, str] = {}
    for name, label in _PLAN_FIELD_LABELS.items():
        value = get_value(label, fields)
        if not value:
            for fallback_label in _PLAN_FIELD_LABEL_FALLBACKS.get(name, ()):
                value = get_value(fallback_label, fields)
                if value:
                    break
        values[name] = value
    return values


def _extract_goal_weakness_collision_details(fields: list[dict]) -> list[dict[str, str]]:
    field = _find_field("Goal/weak-area collision details", fields)
    if not isinstance(field, dict):
        return []

    raw_value = field.get("value")
    parsed_value: object = raw_value
    if isinstance(raw_value, str):
        normalized = raw_value.strip()
        if not normalized:
            return []
        try:
            parsed_value = json.loads(normalized)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    if not isinstance(parsed_value, list):
        return []

    sanitized: list[dict[str, str]] = []
    for entry in parsed_value:
        if not isinstance(entry, dict):
            continue
        tag = str(entry.get("tag", "")).strip()
        label = str(entry.get("label", "")).strip()
        detail = str(entry.get("detail", "")).strip()
        if tag or detail:
            sanitized.append({"tag": tag, "label": label, "detail": detail})
    return sanitized


@dataclass(frozen=True)
class GuidedInjury:
    area: str = ""
    severity: str = ""
    trend: str = ""
    avoid: str = ""
    notes: str = ""
    injury_type: str = ""
    injury_subtypes: list[str] = field(default_factory=list)
    surface_type: str = ""
    timeframe: str = ""
    cleared: str = ""
    open_wound: str = ""
    bleeding_status: str = ""
    infection_signs: list[str] = field(default_factory=list)
    impact_related: str = ""
    sensitive_area: str = ""
    #: Ephemeral live rehab context resolved server-side at generation time and
    #: attached to the payload just before planning (never persisted into intake
    #: — intake is not the source of truth for rehab stage). Carries this exact
    #: injury episode's ``rehab_stage`` plus the identity the fightcamp selector
    #: keys on (athlete_id, injury_id, episode_id, side) and its exposure trail.
    #: Empty for a plan generated with no resolved stage, which reads downstream
    #: as "stage not resolved" — exactly the pre-stage-aware behaviour.
    rehab_generation_context: dict[str, object] = field(default_factory=dict)

    def has_content(self) -> bool:
        return any(
            [
                self.area,
                self.severity,
                self.trend,
                self.avoid,
                self.notes,
                self.injury_type,
                self.injury_subtypes,
                self.surface_type,
                self.timeframe,
                self.cleared,
                self.open_wound,
                self.bleeding_status,
                self.infection_signs,
                self.impact_related,
                self.sensitive_area,
            ]
        )


def _coerce_guided_text(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return ""
    return str(value).strip()


def _coerce_guided_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    coerced = _coerce_guided_text(value)
    return [coerced] if coerced else []


def _build_guided_injury(raw_value: dict[str, object]) -> GuidedInjury:
    return GuidedInjury(
        area=_coerce_guided_text(raw_value.get("area")),
        severity=_coerce_guided_text(raw_value.get("severity")),
        trend=_coerce_guided_text(raw_value.get("trend")),
        avoid=_coerce_guided_text(raw_value.get("avoid")),
        notes=_coerce_guided_text(raw_value.get("notes")),
        injury_type=_coerce_guided_text(raw_value.get("injury_type")),
        injury_subtypes=_coerce_guided_list(raw_value.get("injury_subtypes")),
        surface_type=_coerce_guided_text(raw_value.get("surface_type")),
        timeframe=_coerce_guided_text(raw_value.get("timeframe")),
        cleared=_coerce_guided_text(raw_value.get("cleared")),
        open_wound=_coerce_guided_text(raw_value.get("open_wound")),
        bleeding_status=_coerce_guided_text(raw_value.get("bleeding_status")),
        infection_signs=_coerce_guided_list(raw_value.get("infection_signs")),
        impact_related=_coerce_guided_text(raw_value.get("impact_related")),
        sensitive_area=_coerce_guided_text(raw_value.get("sensitive_area")),
        rehab_generation_context=_coerce_rehab_generation_context(raw_value),
    )


def _coerce_rehab_generation_context(raw_value: dict[str, object]) -> dict[str, object]:
    """Read the server-attached ephemeral rehab context off a guided-injury dict.

    The generation boundary (``api.services.rehab_stage_snapshot``) stamps a
    ``rehab_generation_context`` mapping onto each guided injury it can resolve a
    live stage for. Nothing else writes it, so an absent or malformed value
    simply means "no live stage resolved" and yields an empty context.
    """
    context = raw_value.get("rehab_generation_context")
    if not isinstance(context, dict):
        return {}
    stage = str(context.get("rehab_stage") or "").strip().lower()
    if not stage:
        return {}
    exposures = context.get("rehab_exposures")
    equipment = context.get("available_equipment")
    return {
        "rehab_stage": stage,
        "injury_id": str(context.get("injury_id") or "") or None,
        "episode_id": str(context.get("episode_id") or "") or None,
        "athlete_id": str(context.get("athlete_id") or "") or None,
        "rehab_care_pathway": str(context.get("rehab_care_pathway") or "") or None,
        "rehab_exposures": list(exposures) if isinstance(exposures, (list, tuple)) else [],
        "available_equipment": list(equipment)
        if isinstance(equipment, (list, tuple))
        else None,
        "policy_injury": context.get("policy_injury") if isinstance(context.get("policy_injury"), dict) else None,
        "rehab_history_truncated": context.get("rehab_history_truncated", False) is True,
    }


_GUIDED_TRIGGER_PREFIX = re.compile(
    r"^(?:avoid|limit|reduce|no|dont|don't|do not|cannot|can't|cant|restricted|restriction)\b",
    re.IGNORECASE,
)
_GUIDED_SEVERITY_MAP = {
    "low": "low",
    "mild": "low",
    "moderate": "moderate",
    "high": "high",
    "severe": "high",
}
_GUIDED_STRUCTURAL_NOTE_PATTERN = re.compile(
    r"\b(?:acl|tear|rupture|reconstruction|dislocation|fracture|concussion)\b",
    re.IGNORECASE,
)
def _extract_guided_injury(data: dict) -> GuidedInjury | None:
    if not isinstance(data, dict):
        return None

    raw_value = data.get("guided_injury")
    if not isinstance(raw_value, dict):
        nested_data = data.get("data")
        if isinstance(nested_data, dict):
            raw_value = nested_data.get("guided_injury")
    if not isinstance(raw_value, dict):
        return None

    guided = _build_guided_injury(raw_value)
    return guided if guided.has_content() else None


def _extract_guided_injuries(data: dict) -> list[GuidedInjury]:
    if not isinstance(data, dict):
        return []

    raw_value = data.get("guided_injuries")
    if not isinstance(raw_value, list):
        nested_data = data.get("data")
        if isinstance(nested_data, dict):
            raw_value = nested_data.get("guided_injuries")
    if not isinstance(raw_value, list):
        return []

    injuries: list[GuidedInjury] = []
    for entry in raw_value:
        if not isinstance(entry, dict):
            continue
        guided = _build_guided_injury(entry)
        if guided.has_content():
            injuries.append(guided)
    return injuries


def _parse_guided_injury(guided_injury: GuidedInjury) -> tuple[list[dict[str, str | None]], list[ParsedRestriction]]:
    injuries: list[dict[str, str | None]] = []
    restrictions: list[ParsedRestriction] = []
    from .injury_awareness import reported_training_impact
    training_impact = reported_training_impact({"notes": guided_injury.notes})
    is_not_limiting = training_impact == "not_limiting"

    if guided_injury.area:
        injury_entry = parse_injury_entry(guided_injury.area)
        if injury_entry is None:
            injury_entry = {
                "injury_type": "unspecified",
                "canonical_location": None,
                "side": None,
                "laterality": None,
                "original_phrase": guided_injury.area,
            }

        injury_entry = resolve_guided_injury_entry(guided_injury, injury_entry)

        # The area-only parser can leave an unspecified rehab type after the
        # guided notes resolve a reviewed pilot profile. Keep its canonical
        # type at the episode selector boundary; applicability is still checked
        # by the owned clinical policy, never inferred from these notes alone.
        if ((injury_entry.get("canonical_location"), injury_entry.get("injury_type"))
                in {("elbow", "tendonitis"), ("ankle", "sprain"), ("shoulder", "tendonitis"), ("groin", "strain")}
                and injury_entry.get("rehab_type") in (None, "", "unspecified")):
            injury_entry["rehab_type"] = injury_entry["injury_type"]

        laterality = injury_entry.get("laterality") or injury_entry.get("side")
        display_location = strip_guided_laterality(guided_injury.area, laterality)
        if display_location and is_clean_guided_display_location(guided_injury.area, injury_entry):
            injury_entry["display_location"] = display_location

        injury_entry["guided_source_injury_type"] = guided_injury.injury_type
        injury_entry["guided_source_injury_subtypes"] = list(guided_injury.injury_subtypes)
        injury_entry["guided_source_area"] = guided_injury.area

        raw_guided_severity = guided_injury.severity.strip().lower()
        mapped_severity = _GUIDED_SEVERITY_MAP.get(raw_guided_severity)
        if mapped_severity:
            injury_entry["severity"] = mapped_severity
            injury_entry["severity_source"] = "guided_card"
            injury_entry["severity_evidence"] = [f"guided severity: {mapped_severity}"]
        if guided_injury.trend:
            injury_entry["trend"] = guided_injury.trend.strip().lower()
        if guided_injury.avoid:
            injury_entry["avoid"] = guided_injury.avoid.strip().lower()

        if guided_injury.notes:
            injury_entry["notes"] = guided_injury.notes
            if training_impact:
                injury_entry["training_impact"] = training_impact
            if is_not_limiting:
                # Keep the athlete's explicit onboarding impact choice attached
                # to the parsed record.  The approved-resume runtime can then
                # remove only this non-limiting injury from planning without
                # changing the preserved intake used by Today.
                injury_entry["training_impact"] = "not_limiting"
                # The athlete's own area wording, used only for the
                # awareness note (fightcamp.injury_awareness).
                injury_entry["guided_area"] = guided_injury.area
            if _GUIDED_STRUCTURAL_NOTE_PATTERN.search(guided_injury.notes):
                injury_entry["original_phrase"] = f"{guided_injury.area}. Notes: {guided_injury.notes}"
        _apply_rehab_generation_context(injury_entry, guided_injury.rehab_generation_context)
        injuries.append(injury_entry)

    # A surface/skin injury's "avoid" is wound-care guidance (e.g. "avoid
    # friction on the wound"), not a training-load restriction. Promoting it to
    # a hard restriction makes the Stage-2 validator flag the plan's own
    # wound-care references as violations and falsely hold the plan, so we keep
    # it as injury guidance (already stored on injury_entry["avoid"]) instead.
    is_surface_injury = (
        str(guided_injury.injury_type or "").strip().lower() == "surface_injury"
        or bool(str(guided_injury.surface_type or "").strip())
        or any(
            str(entry.get("injury_type") or "").strip().lower() in SURFACE_TISSUE_TYPES
            or str(entry.get("rehab_type") or "").strip().lower() in SURFACE_TISSUE_TYPES
            for entry in injuries
        )
    )

    if guided_injury.avoid and not is_surface_injury:
        restriction_phrase = guided_injury.avoid
        if not _GUIDED_TRIGGER_PREFIX.match(restriction_phrase):
            restriction_phrase = f"avoid {restriction_phrase}"
        restriction = parse_restriction_entry(restriction_phrase)
        if restriction is not None:
            if restriction.get("region") is None and injuries:
                restriction["region"] = injuries[0].get("canonical_location")
            if is_not_limiting:
                restriction["training_impact"] = "not_limiting"
            restrictions.append(restriction)

    return injuries, restrictions


def _apply_rehab_generation_context(
    injury_entry: dict[str, object], context: dict[str, object]
) -> None:
    """Stamp one injury episode's live rehab context onto its parsed entry.

    Keeps the stage, the identity the fightcamp rehab selector keys on
    (``injury_id``, ``episode_id``, ``athlete_id``, ``side``) and the exposure
    trail travelling together as one atomic unit, so no downstream merge can
    pair one episode's evidence with another episode's stage.
    """
    if not context:
        return
    injury_entry["rehab_stage"] = context.get("rehab_stage")
    for source_key, entry_key in (
        ("injury_id", "injury_id"),
        ("episode_id", "episode_id"),
        ("athlete_id", "athlete_id"),
        ("rehab_care_pathway", "rehab_care_pathway"),
        ("available_equipment", "available_equipment"),
        ("policy_injury", "policy_injury"),
        ("rehab_history_truncated", "rehab_history_truncated"),
    ):
        value = context.get(source_key)
        if value is not None:
            injury_entry[entry_key] = value
    exposures = context.get("rehab_exposures")
    if isinstance(exposures, (list, tuple)):
        injury_entry["rehab_exposures"] = list(exposures)


def _parse_guided_injuries(
    guided_injuries: list[GuidedInjury],
) -> tuple[list[dict[str, str | None]], list[ParsedRestriction]]:
    parsed_injuries: list[dict[str, str | None]] = []
    parsed_restrictions: list[ParsedRestriction] = []
    for guided_injury in guided_injuries:
        injuries, restrictions = _parse_guided_injury(guided_injury)
        parsed_injuries.extend(injuries)
        parsed_restrictions.extend(restrictions)
    return parsed_injuries, parsed_restrictions


def _phrase_present(haystack: str, needle: str) -> bool:
    """Whether *needle* appears in *haystack* as a whole phrase.

    Word-boundary aware so a short term like "hip"/"arm"/"ear" is not matched
    inside an unrelated word ("chipped", "warm", "forearm").
    """
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def _attach_severity_provenance(
    parsed_injuries: list[dict[str, str | None | list[str]]],
) -> list[dict[str, str | None | list[str]]]:
    def _joined_text(injury: dict[str, str | None | list[str]]) -> str:
        parts = [
            str(injury.get("original_phrase") or ""),
            str(injury.get("notes") or ""),
            str(injury.get("avoid") or ""),
        ]
        # Dedupe parts: guided formatting often folds the notes into
        # original_phrase, so adding notes again double-counts the text (and can
        # split a negated phrase apart). Skip a part only when it already appears
        # as a whole phrase earlier — a word-boundary check so a short term like
        # "hip" is not treated as a duplicate because it sits inside "chipped".
        kept: list[str] = []
        accumulated = ""
        for part in parts:
            cleaned = part.strip()
            if cleaned:
                # Use word boundaries to prevent false-positive substring matches
                # (e.g., "hip" matching inside "chipped", or "arm" inside "warm").
                pattern = rf"\b{re.escape(cleaned.lower())}\b"
                if not re.search(pattern, accumulated.lower()):
                    kept.append(cleaned)
                    accumulated = f"{accumulated} {cleaned}"
        joined = " ".join(kept)
        # Strip negated content so denials ("no fracture") never escalate
        # severity off the negated structural noun.
        return remove_negated_phrases(joined) if joined else joined

    def _severity_rank(value: str) -> int:
        return SEVERITY_RANK.get(value, SEVERITY_RANK["moderate"])

    for injury in parsed_injuries:
        if not isinstance(injury, dict):
            continue
        guided_raw = str(injury.get("severity") or "").strip().lower()
        guided_severity = _GUIDED_SEVERITY_MAP.get(guided_raw)

        text_severity, text_hits = normalize_severity(_joined_text(injury))
        text_has_signal = bool(text_hits)
        injury_type = str(injury.get("injury_type") or "").strip().lower()

        if guided_severity and text_has_signal and _severity_rank(text_severity) > _severity_rank(guided_severity):
            injury["severity"] = text_severity
            injury["severity_source"] = "text_escalation"
            injury["severity_evidence"] = [
                f"guided severity: {guided_severity}",
                f"text severity: {text_severity}",
                *text_hits,
            ]
            continue

        if guided_severity:
            injury["severity"] = guided_severity
            injury["severity_source"] = "guided_card"
            injury["severity_evidence"] = [f"guided severity: {guided_severity}"]
            continue

        default_type_severity = _GUIDED_SEVERITY_MAP.get(INJURY_TYPE_SEVERITY.get(injury_type, ""))
        # "unspecified" (and empty) injury types carry no real severity signal,
        # so they must fall through to the explicit fallback default rather than
        # borrowing a default-type severity.
        if injury_type in ("", "unspecified"):
            default_type_severity = None

        if text_has_signal:
            # Benign qualifiers like "minor"/"slight" escalate nothing and must
            # not pull a recognised injury type below its default floor: "minor
            # swelling" stays at the swelling default (moderate), not low.
            if default_type_severity and _severity_rank(text_severity) < _severity_rank(default_type_severity):
                injury["severity"] = default_type_severity
                injury["severity_source"] = "injury_type_default"
                injury["severity_evidence"] = [f"injury type default: {injury_type}"]
                continue
            injury["severity"] = text_severity
            injury["severity_source"] = "text_detected"
            injury["severity_evidence"] = [f"text severity: {text_severity}", *text_hits]
            continue

        if default_type_severity:
            injury["severity"] = default_type_severity
            injury["severity_source"] = "injury_type_default"
            injury["severity_evidence"] = [f"injury type default: {injury_type}"]
            continue

        injury["severity"] = "moderate"
        injury["severity_source"] = "fallback_default"
        injury["severity_evidence"] = ["fallback default: moderate"]
    return parsed_injuries


def _compute_days_until_fight(
    raw_value: str,
    fight_date: datetime,
    *,
    athlete_timezone: str | None = None,
    now_utc: datetime | None = None,
) -> int | None:
    business_tz = _business_timezone(athlete_timezone)
    reference = _athlete_calendar_now(athlete_timezone, now_utc=now_utc)
    fight_local_date = _fight_local_date(raw_value, fight_date, business_tz)
    raw_days = (fight_local_date - reference.date()).days
    return normalize_days_until_fight(raw_days)


def _has_explicit_timezone(raw_value: str) -> bool:
    return bool(_EXPLICIT_TZ_SUFFIX_PATTERN.search((raw_value or "").strip()))


def _fight_local_date(
    raw_value: str,
    fight_date: datetime,
    business_timezone: timezone | ZoneInfo,
) -> datetime.date:
    raw = (raw_value or "").strip()
    if _DATE_ONLY_PATTERN.match(raw):
        return fight_date.date()
    if _has_explicit_timezone(raw):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(business_timezone).date()
        except ValueError:
            pass
    return fight_date.date()


CampTimelineType = Literal["scheduled_fight", "open_camp"]
DEFAULT_OPEN_CAMP_WEEKS = 12
MAX_OPEN_CAMP_WEEKS = 24

# Keep these tables in sync with ``api.models.PlanRequest.coerce_no_scheduled_fight``
# so a payload coerced once by PlanRequest still coerces the same way when it
# reaches ``PlanInput.from_payload`` (and so the legacy/no-PlanRequest path
# behaves identically to the API path).
_NO_SCHEDULED_FIGHT_TRUE_TOKENS = {"true", "1", "yes", "y", "on"}
_NO_SCHEDULED_FIGHT_FALSE_TOKENS = {"", "false", "0", "no", "n", "off"}


def parse_int_or_none(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            parsed = float(normalized)
        except ValueError:
            return None
        return int(parsed) if math.isfinite(parsed) and parsed.is_integer() else None
    return None


def parse_float_or_none(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            parsed = float(normalized)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _coerce_bool_flag(value: object) -> bool:
    """Boolean from the loose truthy/falsy shapes an API payload can carry."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _NO_SCHEDULED_FIGHT_TRUE_TOKENS:
            return True
        if normalized in _NO_SCHEDULED_FIGHT_FALSE_TOKENS:
            return False
    return bool(value)


def _coerce_no_scheduled_fight(value: object) -> bool:
    return _coerce_bool_flag(value)


def _coerce_open_camp_weeks(value: object) -> int:
    if value is None:
        return DEFAULT_OPEN_CAMP_WEEKS
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return DEFAULT_OPEN_CAMP_WEEKS
        try:
            value = int(round(float(normalized)))
        except ValueError:
            raise ValueError("open_camp_weeks must be numeric") from None
    if isinstance(value, (int, float)):
        return max(1, min(int(round(float(value))), MAX_OPEN_CAMP_WEEKS))
    return DEFAULT_OPEN_CAMP_WEEKS


def _training_workload_from_notes(notes: str) -> dict[str, Any]:
    """Extract explicit current cadence only; numbers never imply intensity.

    This is intake-owned context. A structured requested frequency wins over a
    conflicting description of existing training; the conflict stays inspectable.
    """
    text = str(notes or "").lower()
    pattern = r"\b(\d{1,2})(?:\s*[-–—/]\s*(\d{1,2}))?\s*(?:sessions?|times|workouts?)\s*(?:a|per|each|every)?\s*(?:week(?:ly)?|/\s*week)\b"
    reports = []
    for match in re.finditer(pattern, text):
        prefix = text[max(0, match.start() - 45):match.start()]
        if re.search(r"\b(?:used to|want to|would like|hope to|cannot|can't|don't|do not)\b[^.!?]*$", prefix):
            continue
        low, high = int(match[1]), int(match[2] or match[1])
        if 1 <= low <= high <= 14:
            reports.append((low, high))
    context: dict[str, Any] = {}
    if len(set(reports)) == 1:
        low, high = reports[0]
        context["reported_weekly_sessions"] = {"minimum": low, "maximum": high, "source": "training_notes"}
    double = re.search(r"\b(?:double (?:days|sessions)|two[- ]a[- ]days|twice (?:a|per) day)\b", text)
    if double and not re.search(r"\b(?:no|not|without|never|can't|cannot)\s+(?:do\s+)?$", text[max(0, double.start() - 20):double.start()]):
        context["double_days_reported"] = True
    # Neither a weekday, duration nor intensity is supplied by these statements.
    return context


@dataclass(frozen=True)
class PlanInput:
    full_name: str
    age: str
    weight: str
    target_weight: str
    height: str
    fighting_style_technical: str
    fighting_style_tactical: str
    stance: str
    status: str
    record: str
    next_fight_date: str
    athlete_timezone: str
    athlete_locale: str
    rounds_format: str
    frequency_raw: str
    fatigue: str
    equipment_access: str
    available_days: str
    hard_sparring_days_raw: str
    support_work_days_raw: str
    injuries: str
    guided_injury: GuidedInjury | None
    parsed_injuries: list[dict[str, str | None]]
    restrictions: list[ParsedRestriction]
    key_goals: str
    primary_goal: str
    weak_areas: str
    primary_weak_area: str
    training_preference: str
    mental_block: str
    notes: str
    training_days: list[str]
    hard_sparring_days: list[str]
    support_work_days: list[str]
    training_frequency: int
    weeks_out: int | str
    days_until_fight: int | None
    goal_weakness_collision_detail: str | None = None
    goal_weakness_collision_tags: list[str] = field(default_factory=list)
    goal_weakness_collision_details: list[dict[str, str]] = field(default_factory=list)
    guided_injuries: list[GuidedInjury] = field(default_factory=list)
    parsing_metadata: dict[str, dict[str, str]] = field(default_factory=dict)
    training_workload_context: dict[str, Any] = field(default_factory=dict)
    # Explicit camp-timeline plumbing. ``no_scheduled_fight`` is the external API
    # flag (mirrors the frontend ``noScheduledFight`` checkbox); the internal
    # ``camp_timeline_type`` token is derived in ``from_payload`` so downstream
    # planning code never has to re-infer from empty strings.
    no_scheduled_fight: bool = False
    camp_timeline_type: CampTimelineType = "scheduled_fight"
    open_camp_weeks: int = DEFAULT_OPEN_CAMP_WEEKS
    # Under-18 safeguard. Set by the backend from the profile's stored date of
    # birth — never from an intake answer, and never from an ``Age`` field the
    # athlete typed. When true the pipeline must not produce weight-cut,
    # dehydration or water-cut guidance (docs/children-age-appropriate-use-policy.md).
    is_minor: bool = False
    sparring_readiness: dict = field(default_factory=dict)
    # Account-level override set by the backend (never from intake answers):
    # plan from the generation day itself instead of starting the camp tomorrow.
    include_generation_day: bool = False

    @classmethod
    def from_payload(cls, data: dict) -> "PlanInput":
        def _metadata(source: ProvenanceSource, reason: str | None = None) -> dict[str, str]:
            metadata = {"source": source}
            if reason:
                metadata["reason"] = reason
            return metadata

        fields = _extract_fields(data)
        values = _get_plan_field_values(fields)

        raw_available_days = values["available_days"]
        raw_frequency = values["frequency_raw"]
        training_workload_context = _training_workload_from_notes(values["notes"])
        raw_athlete_timezone = values["athlete_timezone"].strip()

        # Normalization step (safe, non-planning transforms only).
        next_fight_date = get_date_value("When is your next fight?", fields)
        injuries = normalize_injury_text(
            get_value("Any injuries or areas you need to work around?", fields)
        )
        raw_training_impact = ""
        guided_injuries = _extract_guided_injuries(data)
        if guided_injuries:
            guided_injury = guided_injuries[0]
            parsed_injuries, parsed_restrictions = _parse_guided_injuries(guided_injuries)
        else:
            guided_injury = _extract_guided_injury(data)
            if guided_injury is not None:
                guided_injuries = [guided_injury]
                parsed_injuries, parsed_restrictions = _parse_guided_injury(guided_injury)
            else:
                guided_injuries = []
                from .injury_awareness import reported_training_impact
                impact = raw_training_impact = reported_training_impact({"notes": injuries})
                symptom_text = re.sub(r"\s*\[training_impact:[^\]]*\]", "", injuries or "", flags=re.I)
                parsed_injuries, parsed_restrictions = parse_injuries_and_restrictions(symptom_text)
                if impact:
                    for entry in parsed_injuries:
                        entry["training_impact"] = impact
                        entry["notes"] = f"[training_impact:{impact}]"
        parsed_injuries = _attach_severity_provenance(parsed_injuries)
        if raw_training_impact:
            # Match Camp Setup's compatibility bands only after clinical text
            # and injury-type severity are resolved. Never soften that evidence.
            reported_band = {"not_limiting": "low", "limiting": "moderate", "cant_train": "high"}[raw_training_impact]
            bands = {"low": 0, "moderate": 1, "high": 2}
            for entry in parsed_injuries:
                if bands[reported_band] > bands.get(str(entry.get("severity") or "moderate"), 1):
                    entry["severity"] = reported_band
                    entry["severity_source"] = "athlete_training_impact"
                    entry["severity_evidence"] = [*(entry.get("severity_evidence") or []),
                                                  f"training impact: {raw_training_impact}"]
        training_days = [d.strip() for d in raw_available_days.split(",") if d.strip()]
        hard_sparring_days = [
            d.strip() for d in values["hard_sparring_days_raw"].split(",") if d.strip()
        ]
        support_work_days = [
            d.strip() for d in values["support_work_days_raw"].split(",") if d.strip()
        ]
        key_goals_list = [goal.strip() for goal in values["key_goals"].split(",") if goal.strip()]
        weak_areas_list = [area.strip() for area in values["weak_areas"].split(",") if area.strip()]
        primary_goal = values["primary_goal"].strip()
        primary_weak_area = values["primary_weak_area"].strip()
        goal_weakness_collision_detail = values["goal_weakness_collision_detail"].strip()
        goal_weakness_collision_tags = [
            tag.strip()
            for tag in values["goal_weakness_collision_tags"].split(",")
            if tag.strip()
        ]
        if not primary_goal or primary_goal not in key_goals_list:
            primary_goal = key_goals_list[0] if key_goals_list else ""
        if not primary_weak_area or primary_weak_area not in weak_areas_list:
            primary_weak_area = weak_areas_list[0] if weak_areas_list else ""
        goal_weakness_collision_details = _extract_goal_weakness_collision_details(fields)

        # Validation step (planning-critical contract).
        if next_fight_date:
            fight_date = _parse_fight_datetime(next_fight_date)
            if fight_date is None:
                raise ValueError(f"invalid fight date format: {next_fight_date}")
        else:
            fight_date = None

        if raw_frequency.strip():
            try:
                training_frequency = int(raw_frequency)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid Weekly Training Frequency: expected integer, got {raw_frequency!r}"
                ) from exc
            if not 1 <= training_frequency <= 7:
                raise ValueError(f"invalid Weekly Training Frequency: expected integer 1..7, got {training_frequency}")
            if training_days and training_frequency > len(training_days):
                raise ValueError(
                    "invalid Weekly Training Frequency: cannot exceed selected Training Availability days "
                    f"({len(training_days)}), got {training_frequency}"
                )
            training_frequency_metadata = _metadata("user_supplied")
        else:
            # Reported sessions in notes are workload context, not training days.
            training_frequency = len(training_days)
            training_frequency_metadata = _metadata(
                "system_inferred",
                "weekly_training_frequency_missing_inferred_from_training_availability",
            )
        training_workload_context["frequency_source"] = training_frequency_metadata["source"]

        available_days_metadata = (
            _metadata("user_supplied")
            if raw_available_days.strip()
            else _metadata("defaulted_missing", "training_availability_missing")
        )

        fallback_timezone_name = _platform_default_timezone_name()
        if not raw_athlete_timezone:
            effective_athlete_timezone = fallback_timezone_name
            athlete_timezone_metadata = _metadata("defaulted_missing", "athlete_timezone_missing")
        elif _resolve_timezone(raw_athlete_timezone) is None:
            effective_athlete_timezone = fallback_timezone_name
            athlete_timezone_metadata = _metadata(
                "defaulted_missing",
                "invalid_athlete_timezone_fallback_to_platform_default",
            )
        else:
            effective_athlete_timezone = raw_athlete_timezone
            athlete_timezone_metadata = _metadata("user_supplied")

        include_generation_day = (
            _coerce_bool_flag(data.get("include_generation_day"))
            if isinstance(data, dict)
            else False
        )

        weeks_out: int | str = "N/A"
        days_until_fight = None
        if fight_date:
            # The camp calendar starts the day after the reference date, so
            # anchoring one day earlier makes the generation day plannable.
            days_until_fight = _compute_days_until_fight(
                next_fight_date,
                fight_date,
                athlete_timezone=effective_athlete_timezone,
                now_utc=(_utc_now() - timedelta(days=1)) if include_generation_day else None,
            )
            weeks_out = max(1, days_until_fight // 7) if days_until_fight is not None else "N/A"

        # Resolve explicit camp-timeline plumbing. ``no_scheduled_fight`` is the
        # external API flag; ``camp_timeline_type`` is the derived internal
        # token. Coercion must match ``api.models.PlanRequest`` so that a
        # payload round-tripping through PlanRequest -> PlanInput keeps the
        # same parse semantics as a payload built directly here.
        if isinstance(data, dict) and "no_scheduled_fight" in data:
            no_scheduled_fight = _coerce_no_scheduled_fight(data.get("no_scheduled_fight"))
        else:
            # Backward compat for payloads predating the flag (PR #1263): an
            # absent flag plus an empty next_fight_date implies open camp.
            no_scheduled_fight = not bool(next_fight_date)

        is_minor = (
            _coerce_bool_flag(data.get("is_minor"))
            if isinstance(data, dict)
            else False
        )

        open_camp_weeks_raw = data.get("open_camp_weeks") if isinstance(data, dict) else None
        open_camp_weeks = _coerce_open_camp_weeks(open_camp_weeks_raw)

        camp_timeline_type: CampTimelineType = "open_camp" if no_scheduled_fight else "scheduled_fight"

        normalized_values = {
            **values,
            "athlete_timezone": effective_athlete_timezone,
            "key_goals": ", ".join(key_goals_list),
            "primary_goal": primary_goal,
            "weak_areas": ", ".join(weak_areas_list),
            "primary_weak_area": primary_weak_area,
            "goal_weakness_collision_detail": goal_weakness_collision_detail,
            "goal_weakness_collision_tags": goal_weakness_collision_tags,
            "goal_weakness_collision_details": goal_weakness_collision_details,
        }

        return cls(
            **normalized_values,
            sparring_readiness={
                **(data.get("_sparring_readiness") if isinstance(data.get("_sparring_readiness"), dict) else {}),
                "reduced_contact_requested": data.get("reduced_contact_requested") is True,
            },
            next_fight_date=next_fight_date,
            injuries=injuries,
            guided_injury=guided_injury,
            guided_injuries=guided_injuries,
            parsed_injuries=parsed_injuries,
            restrictions=parsed_restrictions,
            training_days=training_days,
            hard_sparring_days=hard_sparring_days,
            support_work_days=support_work_days,
            training_frequency=training_frequency,
            training_workload_context=training_workload_context,
            weeks_out=weeks_out,
            days_until_fight=days_until_fight,
            no_scheduled_fight=no_scheduled_fight,
            camp_timeline_type=camp_timeline_type,
            open_camp_weeks=open_camp_weeks,
            is_minor=is_minor,
            include_generation_day=include_generation_day,
            parsing_metadata={
                "training_frequency": training_frequency_metadata,
                "available_days": available_days_metadata,
                "athlete_timezone": athlete_timezone_metadata,
                "goal_weakness_collision_detail": (
                    _metadata("user_supplied")
                    if goal_weakness_collision_detail
                    else _metadata("defaulted_missing", "goal_weakness_collision_detail_missing")
                ),
                "goal_weakness_collision_tags": (
                    _metadata("user_supplied")
                    if goal_weakness_collision_tags
                    else _metadata("defaulted_missing", "goal_weakness_collision_tags_missing")
                ),
            },
        )

    @property
    def tech_styles(self) -> list[str]:
        return _normalize_list(self.fighting_style_technical)

    @property
    def tactical_styles(self) -> list[str]:
        return _normalize_list(self.fighting_style_tactical)

    def generation_issues(self) -> list[str]:
        # ``camp_timeline_type == "open_camp"`` is the explicit no-fight branch
        # (driven by ``no_scheduled_fight``). For that branch the empty
        # ``next_fight_date`` is expected; downstream phase/days-out logic falls
        # back to ``open_camp_weeks`` (default 8). Scheduled fights still
        # require a date.
        issues: list[str] = []
        if not self.fighting_style_technical.strip():
            issues.append("missing_fighting_style_technical")
        if self.camp_timeline_type == "scheduled_fight" and not self.next_fight_date.strip():
            issues.append("missing_next_fight_date")
        elif (
            self.camp_timeline_type == "scheduled_fight"
            and self.days_until_fight is None
        ):
            # A scheduled fight with a date but no computed days-out means the
            # date resolved to the past (``normalize_days_until_fight`` clamps
            # negatives to ``None``). Generating a camp for a fight that has
            # already happened would run with ``weeks_out == "N/A"`` and break
            # downstream phase logic, so block it here instead.
            issues.append("invalid_next_fight_date")
        if not self.training_days:
            issues.append("missing_training_availability")
        if self.training_frequency < 1:
            issues.append("invalid_training_frequency")
        return issues
