from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from api.bodyweight_trend import daily_readings, seven_day_average, smoothed_trend, weekly_pace
from api.datetimes import parse_calendar_date
from fightcamp.camp_phases import calculate_phase_weeks
from fightcamp.input_parsing import _athlete_calendar_now, _compute_days_until_fight, is_short_notice_days
from fightcamp.mindset_module import classify_mental_block
from fightcamp.sports import planning_format
from fightcamp.stage2_payload import _derive_readiness_flags, _is_high_pressure_weight_cut
from fightcamp.stage2_payload_late_fight import _fight_week_override_band
from fightcamp.weight_cut import (
    compute_weight_cut_pct,
    escalation_cut_pct,
    weight_cut_risk_band,
    weight_cut_supervision_required,
)

from .models import (
    AdminAthleteRecord,
    GuidedInjuryInput,
    NutritionBodyweightLogEntry,
    NutritionCoachControlsInput,
    NutritionDerivedState,
    NutritionMonitoringInput,
    NutritionProfileInput,
    NutritionReadinessInput,
    NutritionSandCPreferences,
    NutritionSharedCampContext,
    NutritionWorkspaceState,
    NutritionWorkspaceUpdateRequest,
    ProfileRecord,
)

_DAY_ORDER = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


def _coerce_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clean_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, list):
        parts = value
    else:
        parts = [value]
    cleaned: list[str] = []
    for part in parts:
        normalized = str(part or "").strip()
        if normalized:
            cleaned.append(normalized)
    return cleaned


def _coerce_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    normalized = str(value).strip()
    if not normalized:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(round(float(value)))
    normalized = str(value).strip()
    if not normalized:
        return None
    try:
        return int(round(float(normalized)))
    except ValueError:
        return None


def _coerce_optional_str(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _coerce_guided_injury(value: Any) -> GuidedInjuryInput | None:
    if value is None:
        return None
    try:
        guided = GuidedInjuryInput.model_validate(value)
    except Exception:
        return None
    if not any(
        [
            guided.area.strip(),
            guided.severity.strip(),
            guided.trend.strip(),
            guided.avoid.strip(),
            guided.notes.strip(),
        ]
    ):
        return None
    return guided


def _coerce_bodyweight_log(value: Any) -> list[NutritionBodyweightLogEntry]:
    if not isinstance(value, list):
        return []
    entries: list[NutritionBodyweightLogEntry] = []
    for entry in value:
        try:
            entries.append(NutritionBodyweightLogEntry.model_validate(entry))
        except Exception:
            continue
    return entries


def _infer_weight_source(
    *,
    explicit_source: str | None,
    current_weight_kg: float | None,
    current_weight_recorded_at: str | None,
    daily_bodyweight_log: list[NutritionBodyweightLogEntry],
) -> str | None:
    if explicit_source:
        return explicit_source
    if current_weight_kg is None:
        return None
    if daily_bodyweight_log:
        latest = _latest_bodyweight_log_entry(daily_bodyweight_log)
        if latest and abs(float(latest.weight_kg) - float(current_weight_kg)) < 0.01:
            return "latest_bodyweight_log"
    if current_weight_recorded_at:
        return "manual"
    return "imported"


def _latest_bodyweight_log_entry(
    entries: list[NutritionBodyweightLogEntry],
    *,
    on_or_before: date | None = None,
) -> NutritionBodyweightLogEntry | None:
    if on_or_before is not None:
        entries = [entry for entry in entries if not _is_future_weigh_in(entry, on_or_before)]
    if not entries:
        return None

    def _sort_key(entry: NutritionBodyweightLogEntry) -> tuple[str, str]:
        return (entry.date, entry.time or "")

    return sorted(entries, key=_sort_key)[-1]


def athlete_today(profile: ProfileRecord | AdminAthleteRecord) -> date:
    """The athlete's calendar day, the same reading days-to-fight uses."""
    return _athlete_calendar_now(profile.athlete_timezone or None).date()


# The furthest-ahead timezone. A weigh-in dated after the day already reached
# there cannot have happened anywhere, whatever timezone the athlete is in.
_EARLIEST_ZONE = timezone(timedelta(hours=14))


def latest_calendar_day(now_utc: datetime | None = None) -> date:
    """The newest calendar day that has begun anywhere on Earth.

    Future-dated weigh-ins are judged against this rather than the profile
    timezone, so a fighter who has travelled east of home is not rejected.
    """
    return (now_utc or datetime.now(timezone.utc)).astimezone(_EARLIEST_ZONE).date()


def _is_future_weigh_in(entry: NutritionBodyweightLogEntry, today: date) -> bool:
    day = parse_calendar_date(entry.date)
    return day is not None and day > today


def future_weigh_ins(
    *,
    update: NutritionWorkspaceUpdateRequest,
    previous_log: list[NutritionBodyweightLogEntry],
    today: date,
) -> list[NutritionBodyweightLogEntry]:
    """Weigh-ins this update adds or changes that are dated after today.

    Entries already stored are left alone, so one saved before this check
    existed cannot block every later save; it is ignored for camp weight instead.
    """
    unchanged = [entry.model_dump(mode="json") for entry in previous_log]
    added: list[NutritionBodyweightLogEntry] = []
    for entry in update.nutrition_monitoring.daily_bodyweight_log:
        dumped = entry.model_dump(mode="json")
        if dumped in unchanged:
            unchanged.remove(dumped)
        elif _is_future_weigh_in(entry, today):
            added.append(entry)
    return added


def _same_weigh_in(left: NutritionBodyweightLogEntry | None, right: NutritionBodyweightLogEntry | None) -> bool:
    if left is None or right is None:
        return left is right
    return (left.date, left.time or "", round(left.weight_kg, 2)) == (right.date, right.time or "", round(right.weight_kg, 2))


def sync_current_weight_with_log(
    *,
    update: NutritionWorkspaceUpdateRequest,
    previous_log: list[NutritionBodyweightLogEntry],
    today: date,
) -> tuple[NutritionWorkspaceUpdateRequest, bool]:
    """Make the newest weigh-in the camp's current weight when it changes.

    Adding a newer weigh-in, or editing or deleting the newest one, moves the
    current weight to the newest weigh-in left in the log. Edits to older
    entries leave it alone, so a weight entered later in Camp Setup is not
    overwritten by tidying history. An emptied log keeps the last current weight.
    A weigh-in dated after ``today`` never counts as the newest.
    Returns the update and whether the current weight now comes from the log.
    """
    before = _latest_bodyweight_log_entry(previous_log, on_or_before=today)
    after = _latest_bodyweight_log_entry(update.nutrition_monitoring.daily_bodyweight_log, on_or_before=today)
    if after is None or _same_weigh_in(before, after):
        return update, False
    shared = update.shared_camp_context.model_copy(
        update={
            "current_weight_kg": round(float(after.weight_kg), 1),
            "current_weight_source": "latest_bodyweight_log",
            "current_weight_recorded_at": f"{after.date}T{after.time}" if after.time else after.date,
        }
    )
    return update.model_copy(update={"shared_camp_context": shared}), True


def _merge_unique_days(*day_lists: list[str]) -> list[str]:
    merged: list[str] = []
    for day_list in day_lists:
        for day in day_list:
            if day and day not in merged:
                merged.append(day)
    return merged


def _derive_restriction_level(
    explicit_level: str | None,
    guided_injury: GuidedInjuryInput | None,
) -> str | None:
    if explicit_level:
        return explicit_level
    severity = str(guided_injury.severity if guided_injury else "").strip().lower()
    if severity == "low":
        return "minor"
    if severity == "moderate":
        return "moderate"
    if severity == "high":
        return "major"
    return None


def _derive_session_types(
    *,
    explicit_value: dict[str, Any],
    training_availability: list[str],
    hard_sparring_days: list[str],
    support_work_days: list[str],
) -> dict[str, str]:
    cleaned = {
        str(day or "").strip().lower(): str(session_type or "").strip().lower()
        for day, session_type in explicit_value.items()
        if str(day or "").strip() and str(session_type or "").strip()
    }

    derived: dict[str, str] = {}
    training_days = {day.strip().lower() for day in training_availability if str(day).strip()}
    hard_days = {day.strip().lower() for day in hard_sparring_days if str(day).strip()}
    support_days = {day.strip().lower() for day in support_work_days if str(day).strip()}
    for day, session_type in cleaned.items():
        if day not in training_days:
            continue
        if session_type == "hard_spar" and day not in hard_days:
            continue
        if session_type == "technical" and day not in support_days:
            continue
        derived[day] = session_type
    for day in training_availability:
        normalized_day = str(day or "").strip().lower()
        if normalized_day in hard_days:
            derived[normalized_day] = "hard_spar"
        elif normalized_day in support_days:
            derived[normalized_day] = "technical"
    return derived


def normalize_nutrition_update_request(
    *,
    update: NutritionWorkspaceUpdateRequest,
    existing_shared_camp_context: NutritionSharedCampContext | None = None,
) -> NutritionWorkspaceUpdateRequest:
    payload = update.model_dump(mode="json")
    shared_payload = _coerce_dict(payload.get("shared_camp_context"))
    existing_training_availability = (
        list(existing_shared_camp_context.training_availability) if existing_shared_camp_context else []
    )
    existing_session_types = (
        dict(existing_shared_camp_context.session_types_by_day) if existing_shared_camp_context else {}
    )
    hard_sparring_days = _clean_list(shared_payload.get("hard_sparring_days"))
    support_work_days = _clean_list(shared_payload.get("support_work_days") or shared_payload.get("technical_skill_days"))
    training_availability = _merge_unique_days(
        _clean_list(shared_payload.get("training_availability")),
        existing_training_availability,
        hard_sparring_days,
        support_work_days,
    )
    shared_payload["training_availability"] = training_availability
    shared_payload["session_types_by_day"] = _derive_session_types(
        explicit_value=shared_payload.get("session_types_by_day") or existing_session_types,
        training_availability=training_availability,
        hard_sparring_days=hard_sparring_days,
        support_work_days=support_work_days,
    )
    payload["shared_camp_context"] = shared_payload
    return NutritionWorkspaceUpdateRequest.model_validate(payload)


def _has_active_workspace_data(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    if any(
        key in payload
        for key in (
            "shared_camp_context",
            "nutrition_readiness",
            "nutrition_monitoring",
            "nutrition_coach_controls",
            "s_and_c_preferences",
        )
    ):
        return True
    athlete = _coerce_dict(payload.get("athlete"))
    return any(
        [
            athlete.get("weight_kg") is not None,
            athlete.get("target_weight_kg") is not None,
            athlete.get("age") is not None,
            athlete.get("height_cm") is not None,
            payload.get("fight_date"),
            payload.get("training_availability"),
            payload.get("weekly_training_frequency"),
            payload.get("fatigue_level"),
        ]
    )


def _profile_defaults(profile: ProfileRecord | AdminAthleteRecord) -> NutritionProfileInput:
    try:
        return NutritionProfileInput.model_validate(profile.nutrition_profile.model_dump(mode="json"))
    except Exception:
        return NutritionProfileInput()


def _build_nutrition_profile(
    *,
    profile: ProfileRecord | AdminAthleteRecord,
    raw_payload: dict[str, Any],
) -> NutritionProfileInput:
    defaults = _profile_defaults(profile)
    raw_athlete = _coerce_dict(raw_payload.get("athlete"))
    athlete_sex = _coerce_optional_str(raw_athlete.get("sex"))
    athlete_age = _coerce_optional_int(raw_athlete.get("age"))
    athlete_height_cm = _coerce_optional_int(raw_athlete.get("height_cm"))
    return NutritionProfileInput.model_validate(
        {
            **defaults.model_dump(mode="json"),
            "sex": athlete_sex or defaults.sex,
            "age": athlete_age if athlete_age is not None else defaults.age,
            "height_cm": athlete_height_cm if athlete_height_cm is not None else defaults.height_cm,
        }
    )


def _build_shared_camp_context(
    raw_payload: dict[str, Any],
    monitoring: NutritionMonitoringInput,
) -> NutritionSharedCampContext:
    raw_shared = _coerce_dict(raw_payload.get("shared_camp_context"))
    raw_athlete = _coerce_dict(raw_payload.get("athlete"))
    explicit_source = _coerce_optional_str(raw_shared.get("current_weight_source"))
    explicit_recorded_at = _coerce_optional_str(raw_shared.get("current_weight_recorded_at"))
    athlete_current_weight_kg = _coerce_optional_float(raw_athlete.get("weight_kg"))
    athlete_target_weight_kg = _coerce_optional_float(raw_athlete.get("target_weight_kg"))
    current_weight_kg = athlete_current_weight_kg
    if current_weight_kg is None:
        current_weight_kg = _coerce_optional_float(raw_shared.get("current_weight_kg"))
    target_weight_kg = athlete_target_weight_kg
    if target_weight_kg is None:
        target_weight_kg = _coerce_optional_float(raw_shared.get("target_weight_kg"))

    recorded_frequency = raw_shared.get("weekly_training_frequency")
    if recorded_frequency is None:
        recorded_frequency = raw_payload.get("weekly_training_frequency")
    try:
        training_days = NutritionSharedCampContext(weekly_training_frequency=recorded_frequency).weekly_training_frequency
    except ValidationError:
        # Older session-based records cannot be converted into training days without user input.
        # Only the read model is left unset; the saved intake is unchanged.
        training_days = None

    shared = NutritionSharedCampContext.model_validate(
        {
            "fight_date": _coerce_optional_str(raw_shared.get("fight_date"))
            or _coerce_optional_str(raw_payload.get("fight_date"))
            or "",
            "rounds_format": _coerce_optional_str(raw_shared.get("rounds_format"))
            or _coerce_optional_str(raw_payload.get("rounds_format"))
            or "3 x 3",
            "weigh_in_type": _coerce_optional_str(raw_shared.get("weigh_in_type")),
            "weigh_in_time": _coerce_optional_str(raw_shared.get("weigh_in_time")),
            "current_weight_kg": current_weight_kg,
            "current_weight_recorded_at": explicit_recorded_at,
            "current_weight_source": explicit_source,
            "target_weight_kg": target_weight_kg,
            "target_weight_range_kg": raw_shared.get("target_weight_range_kg"),
            "phase_override": _coerce_optional_str(raw_shared.get("phase_override")),
            "fatigue_level": _coerce_optional_str(raw_shared.get("fatigue_level"))
            or _coerce_optional_str(raw_payload.get("fatigue_level")),
            "weekly_training_frequency": training_days,
            "training_availability": raw_shared.get("training_availability")
            or raw_payload.get("training_availability")
            or [],
            "hard_sparring_days": raw_shared.get("hard_sparring_days")
            or raw_payload.get("hard_sparring_days")
            or [],
            "support_work_days": raw_shared.get("support_work_days")
            or raw_payload.get("support_work_days")
            or raw_shared.get("technical_skill_days")
            or raw_payload.get("technical_skill_days")
            or [],
            "session_types_by_day": raw_shared.get("session_types_by_day") or {},
            "injuries": _coerce_optional_str(raw_shared.get("injuries"))
            or _coerce_optional_str(raw_payload.get("injuries"))
            or "",
            "guided_injury": raw_shared.get("guided_injury") or raw_payload.get("guided_injury"),
            "training_restriction_level": _coerce_optional_str(raw_shared.get("training_restriction_level")),
        }
    )

    shared.training_availability = _merge_unique_days(
        shared.training_availability,
        shared.hard_sparring_days,
        shared.support_work_days,
    )
    shared.session_types_by_day = _derive_session_types(
        explicit_value=raw_shared.get("session_types_by_day") or {},
        training_availability=shared.training_availability,
        hard_sparring_days=shared.hard_sparring_days,
        support_work_days=shared.support_work_days,
    )
    shared.training_restriction_level = _derive_restriction_level(
        shared.training_restriction_level,
        shared.guided_injury,
    )

    inferred_source = _infer_weight_source(
        explicit_source=shared.current_weight_source,
        current_weight_kg=shared.current_weight_kg,
        current_weight_recorded_at=shared.current_weight_recorded_at,
        daily_bodyweight_log=monitoring.daily_bodyweight_log,
    )
    shared.current_weight_source = inferred_source  # type: ignore[assignment]

    if shared.current_weight_source == "latest_bodyweight_log" and not shared.current_weight_recorded_at:
        latest_entry = _latest_bodyweight_log_entry(monitoring.daily_bodyweight_log)
        if latest_entry:
            shared.current_weight_recorded_at = f"{latest_entry.date}T{latest_entry.time}" if latest_entry.time else latest_entry.date

    return shared


def _build_s_and_c_preferences(raw_payload: dict[str, Any]) -> NutritionSandCPreferences:
    raw_section = _coerce_dict(raw_payload.get("s_and_c_preferences"))
    return NutritionSandCPreferences.model_validate(
        {
            "equipment_access": raw_section.get("equipment_access") or raw_payload.get("equipment_access") or [],
            "key_goals": raw_section.get("key_goals") or raw_payload.get("key_goals") or [],
            "weak_areas": raw_section.get("weak_areas") or raw_payload.get("weak_areas") or [],
            "training_preference": _coerce_optional_str(raw_section.get("training_preference"))
            or _coerce_optional_str(raw_payload.get("training_preference"))
            or "",
            "mindset_challenges": _coerce_optional_str(raw_section.get("mindset_challenges"))
            or _coerce_optional_str(raw_payload.get("mindset_challenges"))
            or "",
            "notes": _coerce_optional_str(raw_section.get("notes"))
            or _coerce_optional_str(raw_payload.get("notes"))
            or "",
            "random_seed": raw_section.get("random_seed")
            if raw_section.get("random_seed") is not None
            else raw_payload.get("random_seed"),
        }
    )


def _build_readiness(raw_payload: dict[str, Any]) -> NutritionReadinessInput:
    return NutritionReadinessInput.model_validate(_coerce_dict(raw_payload.get("nutrition_readiness")))


def _build_monitoring(raw_payload: dict[str, Any]) -> NutritionMonitoringInput:
    raw_section = _coerce_dict(raw_payload.get("nutrition_monitoring"))
    return NutritionMonitoringInput(
        daily_bodyweight_log=_coerce_bodyweight_log(raw_section.get("daily_bodyweight_log")),
    )


def _build_coach_controls(raw_payload: dict[str, Any]) -> NutritionCoachControlsInput:
    raw_section = _coerce_dict(raw_payload.get("nutrition_coach_controls"))
    return NutritionCoachControlsInput.model_validate(raw_section)


def _compute_days_out(fight_date: str, athlete_timezone: str) -> int | None:
    normalized = str(fight_date or "").strip()
    if not normalized:
        return None
    try:
        fight_dt = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        try:
            fight_dt = datetime.fromisoformat(f"{normalized}T00:00:00")
        except ValueError:
            return None
    return _compute_days_until_fight(normalized, fight_dt, athlete_timezone=athlete_timezone or None)


def _resolve_effective_phase(
    *,
    phase_override: str | None,
    days_until_fight: int | None,
    current_weight_kg: float | None,
    target_weight_kg: float | None,
    fatigue_level: str | None,
    technical_style: list[str],
    tactical_style: list[str],
    professional_status: str,
    mindset_challenges: str,
) -> str | None:
    if phase_override:
        return phase_override

    if days_until_fight is None:
        return None

    primary_technical = technical_style[0].strip().lower() if technical_style else ""
    mapped_format = planning_format(primary_technical, fallback=None)
    if mapped_format:
        weight_cut_pct = compute_weight_cut_pct(current_weight_kg or 0.0, target_weight_kg or 0.0)
        weight_cut_risk = weight_cut_pct >= 3.0
        mental_block = classify_mental_block(mindset_challenges or "")
        phase_weeks = calculate_phase_weeks(
            max(1, days_until_fight // 7),
            mapped_format,
            tactical_style,
            professional_status,
            fatigue_level,
            weight_cut_risk,
            mental_block,
            weight_cut_pct,
            days_until_fight,
        )
        phase_days = phase_weeks.get("days") or {}
        taper_days = int(phase_days.get("TAPER") or 0)
        spp_days = int(phase_days.get("SPP") or 0)
        if days_until_fight <= taper_days:
            return "TAPER"
        if days_until_fight <= taper_days + spp_days:
            return "SPP"
        return "GPP"

    if days_until_fight <= 7:
        return "Fight week"
    if days_until_fight <= 14:
        return "TAPER"
    if days_until_fight <= 35:
        return "SPP"
    return "GPP"


def _missing_required_fields(
    *,
    nutrition_profile: NutritionProfileInput,
    shared_camp_context: NutritionSharedCampContext,
) -> list[str]:
    missing: list[str] = []
    if nutrition_profile.sex is None:
        missing.append("sex")
    if nutrition_profile.age is None:
        missing.append("age")
    if nutrition_profile.height_cm is None:
        missing.append("height_cm")
    if shared_camp_context.current_weight_kg is None:
        missing.append("current_weight_kg")
    if shared_camp_context.target_weight_kg is None:
        missing.append("target_weight_kg")
    if not shared_camp_context.fight_date:
        missing.append("fight_date")
    if shared_camp_context.weigh_in_type is None:
        missing.append("weigh_in_type")
    if shared_camp_context.weekly_training_frequency is None:
        missing.append("weekly_training_frequency")
    if shared_camp_context.fatigue_level is None:
        missing.append("fatigue_level")
    return missing


def _foundation_status(
    *,
    missing_required_fields: list[str],
    readiness: NutritionReadinessInput,
    monitoring: NutritionMonitoringInput,
) -> str:
    if missing_required_fields:
        return "incomplete"
    if readiness.sleep_quality and monitoring.daily_bodyweight_log:
        return "complete"
    return "sufficient"


def _weight_cut_status(
    *,
    current_weight_kg: float | None,
    target_weight_kg: float | None,
    days_until_fight: int | None,
    risk_band: str,
) -> tuple[str, float | None, float | None]:
    """(status, kg to target, kg behind) for an adult's dated camp.

    Reads the canonical risk band, recomputed from today's camp weight and
    days-out, so a lighter weigh-in moves the status back. "behind" says how
    much lighter the camp weight would need to be for the remaining cut to sit
    at :func:`fightcamp.weight_cut.escalation_cut_pct`. Never a prescription.
    """
    if current_weight_kg is None or target_weight_kg is None or days_until_fight is None or days_until_fight < 0:
        return "none", None, None
    to_target = round(max(0.0, current_weight_kg - target_weight_kg), 1)
    if current_weight_kg <= target_weight_kg:
        return "on_target", 0.0, None
    if risk_band == "severe":
        return "too_aggressive", to_target, None
    if risk_band == "high":
        on_pace_weight = target_weight_kg / (1 - escalation_cut_pct(days_until_fight) / 100)
        return "behind", to_target, max(0.1, round(current_weight_kg - on_pace_weight, 1))
    return "on_track", to_target, None


def _workspace_payload(
    onboarding_draft: Any,
    latest_intake_row: dict[str, Any] | None,
) -> tuple[str, str | None, dict[str, Any]]:
    """(source, intake id, raw payload) the workspace reads: an active
    onboarding draft first, else the latest intake."""
    draft_payload = onboarding_draft if isinstance(onboarding_draft, dict) else None
    if _has_active_workspace_data(draft_payload):
        return "draft", None, draft_payload or {}
    if isinstance(latest_intake_row, dict) and isinstance(latest_intake_row.get("intake"), dict):
        return "intake", str(latest_intake_row.get("id") or "") or None, latest_intake_row["intake"]
    return "default", None, {}


def current_target_weight_kg(
    *,
    onboarding_draft: Any,
    latest_intake_row: dict[str, Any] | None,
) -> float | None:
    """The athlete's current target weight: the one the weight tracker shows,
    from the same payload and precedence as :func:`build_nutrition_workspace`.
    No age check here: callers gate it for under-18s."""
    _, _, raw_payload = _workspace_payload(onboarding_draft, latest_intake_row)

    def _read(section: str) -> float | None:
        raw = _coerce_dict(raw_payload.get(section)).get("target_weight_kg")
        # A boolean is not a weight (float(True) would read as 1 kg).
        return None if isinstance(raw, bool) else _coerce_optional_float(raw)

    target = _read("athlete")
    if target is None:
        target = _read("shared_camp_context")
    return target if target is not None and math.isfinite(target) and target > 0 else None


def build_nutrition_workspace(
    *,
    profile: ProfileRecord | AdminAthleteRecord,
    latest_intake_row: dict[str, Any] | None = None,
) -> NutritionWorkspaceState:
    source, intake_id, raw_payload = _workspace_payload(profile.onboarding_draft, latest_intake_row)

    monitoring = _build_monitoring(raw_payload)
    shared_camp_context = _build_shared_camp_context(raw_payload, monitoring)
    nutrition_profile = _build_nutrition_profile(profile=profile, raw_payload=raw_payload)
    s_and_c_preferences = _build_s_and_c_preferences(raw_payload)
    readiness = _build_readiness(raw_payload)
    coach_controls = _build_coach_controls(raw_payload)

    raw_athlete = _coerce_dict(raw_payload.get("athlete"))
    technical_style = _clean_list(raw_athlete.get("technical_style") or profile.technical_style)
    tactical_style = _clean_list(raw_athlete.get("tactical_style") or profile.tactical_style)
    professional_status = _coerce_optional_str(raw_athlete.get("professional_status")) or profile.professional_status
    days_until_fight = _compute_days_out(shared_camp_context.fight_date, profile.athlete_timezone)
    weight_cut_pct = compute_weight_cut_pct(shared_camp_context.current_weight_kg or 0.0, shared_camp_context.target_weight_kg or 0.0)
    weight_cut_risk = weight_cut_pct >= 3.0
    is_minor = bool(getattr(profile, "is_minor", False))
    risk_band = "none" if is_minor else weight_cut_risk_band(weight_cut_risk, weight_cut_pct, days_until_fight)
    cut_status, kg_to_target, behind_kg = (
        ("none", None, None)
        if is_minor
        else _weight_cut_status(
            current_weight_kg=shared_camp_context.current_weight_kg,
            target_weight_kg=shared_camp_context.target_weight_kg,
            days_until_fight=days_until_fight,
            risk_band=risk_band,
        )
    )
    reference_day = athlete_today(profile)
    # Same future-date rule as camp weight: a day that has begun nowhere is ignored.
    readings = [reading for reading in daily_readings(monitoring.daily_bodyweight_log) if reading[0] <= latest_calendar_day()]
    average_weight, average_days = seven_day_average(readings, reference_day)
    injuries_for_flags = []
    if shared_camp_context.injuries.strip():
        injuries_for_flags.append(shared_camp_context.injuries.strip())
    if shared_camp_context.guided_injury and shared_camp_context.guided_injury.area.strip():
        injuries_for_flags.append(shared_camp_context.guided_injury.area.strip())
    short_notice = is_short_notice_days(days_until_fight)
    readiness_flags = _derive_readiness_flags(
        fatigue=shared_camp_context.fatigue_level or "",
        weight_cut_risk=weight_cut_risk,
        weight_cut_pct=weight_cut_pct,
        injuries=injuries_for_flags,
        short_notice=short_notice,
        days_until_fight=days_until_fight,
    )
    athlete_model = {
        "readiness_flags": readiness_flags,
        "weight_cut_risk": weight_cut_risk,
        "fatigue": shared_camp_context.fatigue_level or "",
        "days_until_fight": days_until_fight,
    }
    effective_phase = _resolve_effective_phase(
        phase_override=shared_camp_context.phase_override,
        days_until_fight=days_until_fight,
        current_weight_kg=shared_camp_context.current_weight_kg,
        target_weight_kg=shared_camp_context.target_weight_kg,
        fatigue_level=shared_camp_context.fatigue_level,
        technical_style=technical_style,
        tactical_style=tactical_style,
        professional_status=professional_status,
        mindset_challenges=s_and_c_preferences.mindset_challenges,
    )
    missing_required_fields = _missing_required_fields(
        nutrition_profile=nutrition_profile,
        shared_camp_context=shared_camp_context,
    )

    derived = NutritionDerivedState(
        days_until_fight=days_until_fight,
        weight_cut_pct=weight_cut_pct,
        weight_cut_risk=weight_cut_risk,
        # Canonical readiness flag, not a raw percentage: athlete_model derives
        # aggressive_weight_cut from the capacity severity bucket, so repeating
        # a bare >= 5.0 here contradicted it for any cut far from fight day.
        aggressive_weight_cut="aggressive_weight_cut" in (readiness_flags or []),
        high_pressure_weight_cut=_is_high_pressure_weight_cut(athlete_model=athlete_model),
        short_notice=short_notice,
        fight_week=isinstance(days_until_fight, int) and 0 <= days_until_fight <= 7,
        readiness_flags=readiness_flags,
        fight_week_override_band=_fight_week_override_band(days_until_fight),
        current_phase_effective=effective_phase,
        weight_cut_risk_band=risk_band,
        weight_cut_supervision_required=(
            False if is_minor else weight_cut_supervision_required(weight_cut_risk, weight_cut_pct, days_until_fight)
        ),
        weight_cut_status=cut_status,
        kg_to_target=kg_to_target,
        weight_cut_behind_kg=behind_kg,
        rolling_7_day_average_weight=average_weight,
        rolling_7_day_weigh_in_days=average_days,
        weekly_pace_kg=weekly_pace(readings, reference_day),
        bodyweight_trend=smoothed_trend(readings),
        foundation_status=_foundation_status(
            missing_required_fields=missing_required_fields,
            readiness=readiness,
            monitoring=monitoring,
        ),
        missing_required_fields=missing_required_fields,
    )

    return NutritionWorkspaceState(
        athlete_id=profile.athlete_id,
        source=source,
        intake_id=intake_id,
        nutrition_profile=nutrition_profile,
        shared_camp_context=shared_camp_context,
        s_and_c_preferences=s_and_c_preferences,
        nutrition_readiness=readiness,
        nutrition_monitoring=monitoring,
        nutrition_coach_controls=coach_controls,
        derived=derived,
    )


def merge_workspace_into_payload(
    *,
    base_payload: dict[str, Any] | None,
    workspace: NutritionWorkspaceUpdateRequest,
    profile: ProfileRecord | AdminAthleteRecord,
    weight_from_log: bool = False,
) -> dict[str, Any]:
    """Fold a workspace update into the stored intake or draft payload.

    The athlete's saved weight normally wins over the workspace copy; with
    ``weight_from_log`` the workspace's current weight (the newest weigh-in, see
    :func:`sync_current_weight_with_log`) replaces it, so camp calculations,
    Camp Setup and the next plan read the same weight as the tracker.
    """
    merged = dict(base_payload or {})
    raw_athlete = _coerce_dict(merged.get("athlete"))
    shared = workspace.shared_camp_context
    s_and_c_preferences = workspace.s_and_c_preferences
    athlete_sex = _coerce_optional_str(raw_athlete.get("sex"))
    athlete_age = _coerce_optional_int(raw_athlete.get("age"))
    athlete_weight_kg = _coerce_optional_float(raw_athlete.get("weight_kg"))
    athlete_target_weight_kg = _coerce_optional_float(raw_athlete.get("target_weight_kg"))
    athlete_height_cm = _coerce_optional_int(raw_athlete.get("height_cm"))
    base_shared = _coerce_dict(merged.get("shared_camp_context"))
    training_availability = _merge_unique_days(
        list(shared.training_availability),
        _clean_list(base_shared.get("training_availability") or merged.get("training_availability")),
        list(shared.hard_sparring_days),
        list(shared.support_work_days),
    )

    athlete_payload = {
        **raw_athlete,
        "full_name": _coerce_optional_str(raw_athlete.get("full_name")) or profile.full_name,
        "sex": athlete_sex or workspace.nutrition_profile.sex,
        "age": athlete_age if athlete_age is not None else workspace.nutrition_profile.age,
        "weight_kg": (
            shared.current_weight_kg
            if weight_from_log and shared.current_weight_kg is not None
            else athlete_weight_kg if athlete_weight_kg is not None else shared.current_weight_kg
        ),
        # The newest target wins: a target saved from the workspace (weight
        # tracker, Camp Setup) replaces the one the intake started with.
        "target_weight_kg": shared.target_weight_kg if shared.target_weight_kg is not None else athlete_target_weight_kg,
        "height_cm": athlete_height_cm if athlete_height_cm is not None else workspace.nutrition_profile.height_cm,
        "technical_style": raw_athlete.get("technical_style") or list(profile.technical_style),
        "tactical_style": raw_athlete.get("tactical_style") or list(profile.tactical_style),
        "stance": _coerce_optional_str(raw_athlete.get("stance")) or profile.stance,
        "professional_status": _coerce_optional_str(raw_athlete.get("professional_status")) or profile.professional_status,
        "record": _coerce_optional_str(raw_athlete.get("record")) or profile.record,
        "athlete_timezone": _coerce_optional_str(raw_athlete.get("athlete_timezone")) or profile.athlete_timezone,
        "athlete_locale": _coerce_optional_str(raw_athlete.get("athlete_locale")) or profile.athlete_locale,
    }

    session_types_by_day = {
        day: session_type
        for day, session_type in _derive_session_types(
            explicit_value=base_shared.get("session_types_by_day") or workspace.shared_camp_context.session_types_by_day,
            training_availability=training_availability,
            hard_sparring_days=shared.hard_sparring_days,
            support_work_days=shared.support_work_days,
        ).items()
        if day and session_type
    }
    shared_payload = {
        **shared.model_dump(mode="json"),
        "current_weight_kg": athlete_payload.get("weight_kg"),
        "target_weight_kg": athlete_payload.get("target_weight_kg"),
        "training_availability": training_availability,
        "session_types_by_day": session_types_by_day,
    }

    merged.update(
        {
            "athlete": athlete_payload,
            "fight_date": shared.fight_date,
            "rounds_format": shared.rounds_format,
            "weekly_training_frequency": shared.weekly_training_frequency,
            "fatigue_level": shared.fatigue_level,
            "equipment_access": list(s_and_c_preferences.equipment_access),
            "training_availability": training_availability,
            "hard_sparring_days": list(shared.hard_sparring_days),
            "support_work_days": list(shared.support_work_days),
            "injuries": shared.injuries,
            "guided_injury": shared.guided_injury.model_dump(mode="json") if shared.guided_injury else None,
            "key_goals": list(s_and_c_preferences.key_goals),
            "weak_areas": list(s_and_c_preferences.weak_areas),
            "training_preference": s_and_c_preferences.training_preference,
            "mindset_challenges": s_and_c_preferences.mindset_challenges,
            "notes": s_and_c_preferences.notes,
            "random_seed": s_and_c_preferences.random_seed,
            "shared_camp_context": shared_payload,
            "s_and_c_preferences": s_and_c_preferences.model_dump(mode="json"),
            "nutrition_readiness": workspace.nutrition_readiness.model_dump(mode="json"),
            "nutrition_monitoring": workspace.nutrition_monitoring.model_dump(mode="json"),
            "nutrition_coach_controls": workspace.nutrition_coach_controls.model_dump(mode="json"),
        }
    )
    return merged

