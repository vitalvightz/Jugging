from __future__ import annotations

import base64
import binascii
import math
import re
from datetime import date, datetime
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from shared.contracts import shared_message

from .contracts.checkin_decision import (
    ActiveInjury as CheckinActiveInjury,
    Body as CheckinBody,
    CheckinDecisionValue,
    Pain as CheckinPain,
    Phase as CheckinPhase,
    PreviousSession as CheckinPreviousSession,
    Sleep as CheckinSleep,
)
from .contracts.completion import CompletionStatus, LandingSessionState
from .contracts.training_disciplines import SessionType, Sport, discipline_error
from .contracts.achilles_restore_load import AchillesRestoreLoadReview
from .contracts.injury_checkin import (
    MAX_INFECTION_SIGNS,
    BleedingStatus as _BleedingStatus,
    Coverable as _Coverable,
    Drainage as _Drainage,
    FrictionOrContactProblem as _FrictionOrContactProblem,
    SkinIntegrity as _SkinIntegrity,
)
from .contracts.rehab_completion import (
    DURING_ANSWERS as _DURING_ANSWERS,
    LIMIT_ANSWERS as _LIMIT_ANSWERS,
)
from .contracts.rehab_stage import (
    CARE_PATHWAYS as _CARE_TYPES,
    REHAB_STAGES as _REHAB_STAGES,
)
from .json_limits import MAX_CLIENT_JSON_BYTES, MAX_JSON_DEPTH, validate_json_field
from .state_machine import GenerationJobStatus
from .structured_plan_models import StructuredTrainingPlan
from .xp import XpAction

# Role foundation: `athlete` and `admin` are live in private beta. `coach` and
# `gym_owner` are reserved for public beta and are not yet selectable at sign-up
# or assignable to accounts. `gym_owner` (not `gym`) names the person managing a
# gym organisation, since the user account is distinct from the organisation.
UserRole = Literal["athlete", "coach", "gym_owner", "admin"]
GuidedInjurySeverity = Literal["", "low", "moderate", "high"]
AppearanceMode = Literal["dark", "light"]
SexValue = Literal["male", "female"]
DailyActivityLevel = Literal["low", "mixed", "active_job"]
WeighInType = Literal["same_day", "day_before", "informal"]
PhaseOverride = Literal["GPP", "SPP", "TAPER"]
FatigueLevel = Literal["low", "moderate", "high"]
WeightSource = Literal["manual", "latest_bodyweight_log", "imported"]
TrainingRestrictionLevel = Literal["none", "minor", "moderate", "major"]
SleepQuality = Literal["good", "mixed", "poor"]
AppetiteStatus = Literal["normal", "low", "high"]
FoundationStatus = Literal["incomplete", "sufficient", "complete"]
NutritionWorkspaceSource = Literal["default", "draft", "intake"]
FightWeekOverrideBand = Literal["none", "final_day_protocol", "micro_taper_protocol", "mini_taper_protocol"]
# NOTE: "technical" is a legacy internal enum token retained for stored drafts and API compatibility.
# It maps to support_work_days (non-hard training / S&C-compatible slots) in planner and UI flows.
SessionDayType = Literal["hard_spar", "technical", "strength", "conditioning", "recovery", "off"]
SparringDayClass = Literal["primary_hard", "secondary_hard", "managed_hard", "technical", "none"]
EffectiveLoad = Literal["hard", "technical", "reduced", "none"]


_RECORD_PATTERN = re.compile(r"^\d+-\d+(?:-\d+)?$")
_ROUNDS_FORMAT_PATTERN = re.compile(r"^(\d+)\s*[xX]\s*(\d+)$")
# Keep this alias map aligned with web/lib/intake-options.ts so the API accepts
# legacy mild/severe inputs while normalizing to the frontend low/moderate/high vocabulary.
_GUIDED_INJURY_SEVERITY_ALIASES = {
    "": "",
    "low": "low",
    "mild": "low",
    "moderate": "moderate",
    "high": "high",
    "severe": "high",
}
_HARD_SPARRING_DAY_CAP = 4


def _parse_training_days_per_week(value: Any) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("weekly_training_frequency must be numeric")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("weekly_training_frequency must be numeric") from None
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise ValueError("weekly_training_frequency must be a whole number")
    parsed = int(numeric)
    if not 1 <= parsed <= 7:
        raise ValueError("weekly_training_frequency must be between 1 and 7")
    return parsed


# Upper bound on a manually submitted Stage 2 plan body (admin-only path).
MANUAL_STAGE2_MAX_CHARS = 80_000

# Per-element cap for GuidedInjuryInput list fields (enum-ish tokens).
_GUIDED_LIST_ITEM_MAX_CHARS = 64
GUIDED_INJURIES_MAX_ITEMS = 64

ATHLETE_FULL_NAME_MAX_CHARS = 120
RECORD_MAX_CHARS = 40
PROFILE_SHORT_TEXT_MAX_CHARS = 120
PROFILE_TIMEZONE_MAX_CHARS = 100
PROFILE_LOCALE_MAX_CHARS = 35
# Large enough to hold a client-downscaled, JPEG-compressed avatar embedded as a
# base64 ``data:`` URL (the web app shrinks uploads to well under 100 KB before
# sending) while still admitting ordinary ``https://`` links. Kept comfortably
# below ``MAX_REQUEST_BODY_BYTES`` so a legitimate avatar never trips the body
# ceiling.
AVATAR_URL_MAX_CHARS = 256 * 1024
ATHLETE_STYLE_LIST_MAX_ITEMS = 32
ATHLETE_LIST_ITEM_MAX_CHARS = 120
PLAN_LIST_ITEM_MAX_CHARS = 120
EVENT_NAME_MAX_CHARS = 80
PLAN_DAY_ITEM_MAX_CHARS = 80
INJURIES_MAX_CHARS = 2000
TRAINING_PREFERENCE_MAX_CHARS = 1000
MENTAL_BLOCKERS_MAX_CHARS = 1500
PREVIOUS_PLAN_FEEDBACK_MAX_CHARS = 1500
GENERIC_PROFILE_NOTES_MAX_CHARS = 1000

# Durable marker + human-readable warning for a mid-generation profile-refresh
# failure. The generation orchestrator writes the key into
# ``final_result["why_log"]`` (persisted to both the generation job row and the
# plan's ``why_log`` column) when the profile write fails but generation
# continues from the submitted payload; the job/plan response mappers read it
# back so the warning survives progress-milestone eviction and is queryable.
# Kept in lockstep with the web copy in web/lib/profile-refresh-warning.ts.
PROFILE_REFRESH_FAILED_WHY_LOG_KEY = "profile_refresh_failed"
# The web client detects this warning by text and by milestone code
# (shared/api-messages.json).
PROFILE_REFRESH_FAILED_WARNING_CODE, PROFILE_REFRESH_FAILED_WARNING = shared_message("profile_refresh_failed")

_PROFILE_TEXT_LIMITS = {
    "full_name": ATHLETE_FULL_NAME_MAX_CHARS,
    "stance": PROFILE_SHORT_TEXT_MAX_CHARS,
    "professional_status": PROFILE_SHORT_TEXT_MAX_CHARS,
    "record": RECORD_MAX_CHARS,
    "athlete_timezone": PROFILE_TIMEZONE_MAX_CHARS,
    "athlete_locale": PROFILE_LOCALE_MAX_CHARS,
    "avatar_url": AVATAR_URL_MAX_CHARS,
}

_PLAN_TEXT_LIMITS = {
    "injuries": INJURIES_MAX_CHARS,
    "goal_weakness_collision_detail": GENERIC_PROFILE_NOTES_MAX_CHARS,
    "training_preference": TRAINING_PREFERENCE_MAX_CHARS,
    "mindset_challenges": MENTAL_BLOCKERS_MAX_CHARS,
    "notes": PREVIOUS_PLAN_FEEDBACK_MAX_CHARS,
    "primary_goal": PLAN_LIST_ITEM_MAX_CHARS,
    "primary_weak_area": PLAN_LIST_ITEM_MAX_CHARS,
}


def _clean_list(values: list[str] | None) -> list[str]:
    return [str(value).strip() for value in values or [] if str(value).strip()]


_AVATAR_DATA_URL_RE = re.compile(r"^data:image/(jpeg|png|webp|gif);base64,([A-Za-z0-9+/]+=*)$")
_AVATAR_MAGIC_BYTES: dict[str, tuple[bytes, ...]] = {
    "jpeg": (b"\xff\xd8\xff",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "gif": (b"GIF87a", b"GIF89a"),
    "webp": (b"RIFF",),
}


def is_safe_avatar_url(value: str) -> bool:
    """Accept only inert avatar sources: credential-free https links, or a base64
    raster image whose bytes really are the declared format. SVG, ``javascript:``,
    ``data:text/html`` and anything else that could carry script is rejected."""
    match = _AVATAR_DATA_URL_RE.match(value)
    if match:
        kind, payload = match.groups()
        try:
            head = base64.b64decode(payload[:64], validate=False)
        except (binascii.Error, ValueError):
            return False
        if kind == "webp" and head[8:12] != b"WEBP":
            return False
        return head.startswith(_AVATAR_MAGIC_BYTES[kind])
    if value.startswith("data:"):
        return False
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
        and not any(ch.isspace() or ord(ch) < 0x20 for ch in value)
    )


def _clean_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip()


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _validate_list_item_lengths(values: list[str], *, field: str, max_chars: int) -> list[str]:
    for item in values:
        if len(item) > max_chars:
            raise ValueError(f"{field} items must be at most {max_chars} characters long")
    return values


def _validate_detail_string_lengths(values: list[dict[str, str]], *, field: str) -> list[dict[str, str]]:
    for index, detail in enumerate(values):
        if not isinstance(detail, dict):
            raise ValueError(f"{field}[{index}] must be an object")
        for key, item in detail.items():
            if len(str(key)) > PLAN_LIST_ITEM_MAX_CHARS:
                raise ValueError(
                    f"{field}[{index}] keys must be at most {PLAN_LIST_ITEM_MAX_CHARS} characters long"
                )
            if len(str(item)) > GENERIC_PROFILE_NOTES_MAX_CHARS:
                raise ValueError(
                    f"{field}[{index}] values must be at most {GENERIC_PROFILE_NOTES_MAX_CHARS} characters long"
                )
    return values


def _validate_guided_injury_draft(value: Any, *, field: str) -> None:
    if value is None:
        return
    from pydantic import ValidationError
    try:
        GuidedInjuryInput.model_validate(value)
    except (ValueError, ValidationError) as exc:
        raise ValueError(f"onboarding_draft.{field} is invalid: {exc}") from exc


def _validate_onboarding_draft_field_lengths(value: Any) -> Any:
    if not isinstance(value, dict):
        return value

    def check_text(container: dict[str, Any], key: str, max_chars: int) -> None:
        if key in container and container[key] is not None and len(str(container[key]).strip()) > max_chars:
            raise ValueError(f"onboarding_draft.{key} must be at most {max_chars} characters long")

    def check_list(container: dict[str, Any], key: str, max_items: int, max_item_chars: int) -> None:
        if key not in container or container[key] is None:
            return
        raw_items = container[key]
        items = raw_items if isinstance(raw_items, list) else [raw_items]
        cleaned = _clean_list(items)
        if len(cleaned) > max_items:
            raise ValueError(f"onboarding_draft.{key} must contain at most {max_items} items")
        _validate_list_item_lengths(cleaned, field=f"onboarding_draft.{key}", max_chars=max_item_chars)

    for key, max_chars in _PLAN_TEXT_LIMITS.items():
        check_text(value, key, max_chars)
    for key, max_chars in _PROFILE_TEXT_LIMITS.items():
        check_text(value, key, max_chars)
    for key in ("equipment_access", "key_goals", "weak_areas", "goal_weakness_collision_tags"):
        check_list(value, key, 64 if key == "equipment_access" or key == "goal_weakness_collision_tags" else 32, PLAN_LIST_ITEM_MAX_CHARS)
    for key in ("training_availability", "hard_sparring_days", "support_work_days", "technical_skill_days"):
        check_list(value, key, 64, PLAN_DAY_ITEM_MAX_CHARS)
    if isinstance(value.get("goal_weakness_collision_details"), list):
        _validate_detail_string_lengths(
            value["goal_weakness_collision_details"],
            field="onboarding_draft.goal_weakness_collision_details",
        )
    if "guided_injury" in value:
        _validate_guided_injury_draft(value.get("guided_injury"), field="guided_injury")
    if "guided_injuries" in value and value.get("guided_injuries") is not None:
        guided_injuries = value["guided_injuries"]
        if not isinstance(guided_injuries, list):
            raise ValueError("onboarding_draft.guided_injuries must be a list")
        if len(guided_injuries) > GUIDED_INJURIES_MAX_ITEMS:
            raise ValueError(f"onboarding_draft.guided_injuries must contain at most {GUIDED_INJURIES_MAX_ITEMS} items")
        for index, guided in enumerate(guided_injuries):
            _validate_guided_injury_draft(guided, field=f"guided_injuries[{index}]")

    athlete = value.get("athlete")
    if isinstance(athlete, dict):
        for key, max_chars in _PROFILE_TEXT_LIMITS.items():
            check_text(athlete, key, max_chars)
        for key in ("technical_style", "tactical_style"):
            check_list(athlete, key, ATHLETE_STYLE_LIST_MAX_ITEMS, ATHLETE_LIST_ITEM_MAX_CHARS)

    return value


def _field(label: str, value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        value = _clean_list(value)
    elif value is None:
        value = ""
    return {"label": label, "value": value}


def _validate_record(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized and not _RECORD_PATTERN.fullmatch(normalized):
        raise ValueError("record must use x-x or x-x-x format")
    return normalized


def _validate_rounds_format(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    match = _ROUNDS_FORMAT_PATTERN.fullmatch(normalized)
    if not match:
        raise ValueError("rounds_format must use numeric rounds x minutes format like 3 x 3")
    return f"{int(match[1])} x {int(match[2])}"


class AthleteProfileInput(BaseModel):
    full_name: str = Field(..., max_length=ATHLETE_FULL_NAME_MAX_CHARS)
    sex: SexValue | None = None
    # Derived compatibility value, NOT a fact the athlete supplies. The only
    # persisted source of age truth is ``profiles.date_of_birth``; whatever
    # arrives here is overwritten with that date's current age before the
    # payload is hashed (api/services/generation_request_service.py) and again
    # before the worker uses it (api/generation/orchestrator.py). The field
    # stays on the contract because saved intakes and planner code still read
    # it — treat it as a copy of the profile's age, never as an input.
    age: int | None = None
    weight_kg: float | None = None
    target_weight_kg: float | None = None
    height_cm: int | None = None
    technical_style: list[str] = Field(default_factory=list, max_length=ATHLETE_STYLE_LIST_MAX_ITEMS)
    tactical_style: list[str] = Field(default_factory=list, max_length=ATHLETE_STYLE_LIST_MAX_ITEMS)
    stance: str = Field(default="", max_length=PROFILE_SHORT_TEXT_MAX_CHARS)
    professional_status: str = Field(default="", max_length=PROFILE_SHORT_TEXT_MAX_CHARS)
    record: str = Field(default="", max_length=RECORD_MAX_CHARS)
    athlete_timezone: str = Field(default="", max_length=PROFILE_TIMEZONE_MAX_CHARS)
    athlete_locale: str = Field(default="", max_length=PROFILE_LOCALE_MAX_CHARS)

    @field_validator("full_name", "stance", "professional_status", "record", "athlete_timezone", "athlete_locale", mode="before")
    @classmethod
    def clean_profile_text(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator("technical_style", "tactical_style", mode="before")
    @classmethod
    def clean_style_lists(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return _clean_list([part.strip() for part in value.split(",")])
        if isinstance(value, list):
            return _clean_list(value)
        return _clean_list([value])

    @field_validator("technical_style", "tactical_style", mode="after")
    @classmethod
    def cap_style_list_items(cls, value: list[str], info: ValidationInfo) -> list[str]:
        return _validate_list_item_lengths(value, field=info.field_name or "style", max_chars=ATHLETE_LIST_ITEM_MAX_CHARS)

    @field_validator("record")
    @classmethod
    def validate_record(cls, value: str) -> str:
        return _validate_record(value)

    @field_validator("height_cm", mode="before")
    @classmethod
    def coerce_height_cm(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                return None
            try:
                return int(round(float(normalized)))
            except ValueError:
                raise ValueError("height_cm must be numeric") from None
        if isinstance(value, (int, float)):
            return int(round(float(value)))
        return value


class GuidedInjuryInput(BaseModel):
    # Per-field caps mirror the PlanRequest convention: generous relative to real
    # UI submissions (selections/free-text top out well under these) and present
    # only to bound abuse, complementing the global json_limits guards.
    area: str = Field(default="", max_length=200)
    # UI-only body-map zone key (e.g. "l_shoulder"). Lets the web map stay lit
    # after the athlete rewrites the free-text area; not a planning signal, so it
    # is intentionally omitted from the Stage 1 prompt serialization below.
    zone: str = Field(default="", max_length=64)
    severity: GuidedInjurySeverity = ""
    trend: str = Field(default="", max_length=50)
    avoid: str = Field(default="", max_length=2000)
    notes: str = Field(default="", max_length=4000)
    injury_type: str = Field(default="", max_length=64)
    injury_subtypes: list[str] = Field(default_factory=list, max_length=64)
    surface_type: str = Field(default="", max_length=64)
    timeframe: str = Field(default="", max_length=64)
    cleared: str = Field(default="", max_length=32)
    open_wound: str = Field(default="", max_length=32)
    bleeding_status: str = Field(default="", max_length=64)
    infection_signs: list[str] = Field(default_factory=list, max_length=64)
    impact_related: str = Field(default="", max_length=32)
    sensitive_area: str = Field(default="", max_length=64)

    @field_validator(
        "area",
        "zone",
        "trend",
        "avoid",
        "notes",
        "injury_type",
        "surface_type",
        "timeframe",
        "cleared",
        "open_wound",
        "bleeding_status",
        "impact_related",
        "sensitive_area",
        mode="before",
    )
    @classmethod
    def coerce_guided_text(cls, value: Any) -> str:
        if isinstance(value, bool):
            return "yes" if value else "no"
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("infection_signs", mode="before")
    @classmethod
    def clean_infection_signs(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return _clean_list(value)
        if isinstance(value, str):
            return _clean_list([part.strip() for part in value.split(",")])
        return _clean_list([value])

    @field_validator("injury_subtypes", mode="before")
    @classmethod
    def clean_injury_subtypes(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return _clean_list(value)
        if isinstance(value, str):
            return _clean_list([part.strip() for part in value.split(",")])
        return _clean_list([value])

    @field_validator("infection_signs", "injury_subtypes", mode="after")
    @classmethod
    def cap_list_item_length(cls, value: list[str]) -> list[str]:
        # The list-level ``max_length`` caps the item count, not item size. These
        # are short enum-ish tokens (e.g. ``surface_injury:bruise``, ``pus``), so
        # cap each element too — otherwise a client could send a few megabyte-long
        # strings and slip under the per-field guards.
        for item in value:
            if len(item) > _GUIDED_LIST_ITEM_MAX_CHARS:
                raise ValueError(
                    f"list elements must be at most {_GUIDED_LIST_ITEM_MAX_CHARS} characters long"
                )
        return value

    @field_validator("severity", mode="before")
    @classmethod
    def validate_severity(cls, value: Any) -> GuidedInjurySeverity:
        normalized = str(value or "").strip().lower()
        mapped = _GUIDED_INJURY_SEVERITY_ALIASES.get(normalized)
        if mapped is None:
            raise ValueError("guided injury severity must be one of low, moderate, or high")
        return mapped


class NutritionProfileInput(BaseModel):
    sex: SexValue | None = None
    age: int | None = None
    height_cm: int | None = None
    daily_activity_level: DailyActivityLevel | None = None
    dietary_restrictions: list[str] = Field(default_factory=list)
    food_preferences: list[str] = Field(default_factory=list)
    meals_per_day_preference: int | None = None
    foods_avoided_pre_session: list[str] = Field(default_factory=list)
    foods_avoided_fight_week: list[str] = Field(default_factory=list)
    supplement_use: list[str] = Field(default_factory=list)
    caffeine_use: bool | None = None

    @field_validator(
        "dietary_restrictions",
        "food_preferences",
        "foods_avoided_pre_session",
        "foods_avoided_fight_week",
        "supplement_use",
        mode="before",
    )
    @classmethod
    def clean_list_fields(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return _clean_list([part.strip() for part in value.split(",")])
        if isinstance(value, list):
            return _clean_list(value)
        return _clean_list([value])

    @field_validator("height_cm", mode="before")
    @classmethod
    def coerce_height_cm_value(cls, value: Any) -> Any:
        return AthleteProfileInput.coerce_height_cm(value)

    @field_validator("age", "meals_per_day_preference", mode="before")
    @classmethod
    def coerce_int_fields(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                return None
            try:
                return int(round(float(normalized)))
            except ValueError:
                raise ValueError("value must be numeric") from None
        if isinstance(value, (int, float)):
            return int(round(float(value)))
        return value

    @model_validator(mode="after")
    def enforce_payload_size(self) -> "NutritionProfileInput":
        validate_json_field(
            self.model_dump(mode="json"),
            field="nutrition_profile",
            max_bytes=MAX_CLIENT_JSON_BYTES,
            max_depth=MAX_JSON_DEPTH,
        )
        return self


class NutritionBodyweightLogEntry(BaseModel):
    date: str
    weight_kg: float
    time: str | None = None
    is_fasted: bool | None = None
    notes: str | None = None

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("date is required")
        return normalized

    @field_validator("weight_kg", mode="before")
    @classmethod
    def coerce_weight(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("weight_kg is required")
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                raise ValueError("weight_kg is required")
            try:
                return float(normalized)
            except ValueError:
                raise ValueError("weight_kg must be numeric") from None
        return value


class NutritionReadinessInput(BaseModel):
    sleep_quality: SleepQuality | None = None
    appetite_status: AppetiteStatus | None = None


class NutritionMonitoringInput(BaseModel):
    daily_bodyweight_log: list[NutritionBodyweightLogEntry] = Field(default_factory=list)


class NutritionCoachControlsInput(BaseModel):
    coach_override_enabled: bool = False
    athlete_override_enabled: bool = False
    do_not_reduce_below_calories: int | None = None
    protein_floor_g_per_kg: float | None = None
    fight_week_manual_mode: bool = False
    water_cut_locked_to_manual: bool = False

    @field_validator("do_not_reduce_below_calories", mode="before")
    @classmethod
    def coerce_optional_int(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                return None
            try:
                return int(round(float(normalized)))
            except ValueError:
                raise ValueError("value must be numeric") from None
        if isinstance(value, (int, float)):
            return int(round(float(value)))
        return value

    @field_validator("protein_floor_g_per_kg", mode="before")
    @classmethod
    def coerce_optional_float(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                return None
            try:
                return float(normalized)
            except ValueError:
                raise ValueError("value must be numeric") from None
        return value


class NutritionSandCPreferences(BaseModel):
    equipment_access: list[str] = Field(default_factory=list, max_length=64)
    key_goals: list[str] = Field(default_factory=list, max_length=32)
    primary_goal: str | None = Field(default=None, max_length=PLAN_LIST_ITEM_MAX_CHARS)
    weak_areas: list[str] = Field(default_factory=list, max_length=32)
    primary_weak_area: str | None = Field(default=None, max_length=PLAN_LIST_ITEM_MAX_CHARS)
    goal_weakness_collision_detail: str = Field(default="", max_length=GENERIC_PROFILE_NOTES_MAX_CHARS)
    goal_weakness_collision_tags: list[str] = Field(default_factory=list, max_length=64)
    goal_weakness_collision_details: list[dict[str, str]] = Field(default_factory=list, max_length=64)
    training_preference: str = Field(default="", max_length=TRAINING_PREFERENCE_MAX_CHARS)
    mindset_challenges: str = Field(default="", max_length=MENTAL_BLOCKERS_MAX_CHARS)
    notes: str = Field(default="", max_length=PREVIOUS_PLAN_FEEDBACK_MAX_CHARS)
    random_seed: int | None = None

    @field_validator("equipment_access", "key_goals", "weak_areas", "goal_weakness_collision_tags", mode="before")
    @classmethod
    def clean_array_fields(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return _clean_list([part.strip() for part in value.split(",")])
        if isinstance(value, list):
            return _clean_list(value)
        return _clean_list([value])

    @field_validator("equipment_access", "key_goals", "weak_areas", "goal_weakness_collision_tags", mode="after")
    @classmethod
    def cap_array_field_items(cls, value: list[str], info: ValidationInfo) -> list[str]:
        return _validate_list_item_lengths(value, field=info.field_name or "list", max_chars=PLAN_LIST_ITEM_MAX_CHARS)

    @field_validator("goal_weakness_collision_details", mode="after")
    @classmethod
    def cap_collision_detail_strings(cls, value: list[dict[str, str]]) -> list[dict[str, str]]:
        return _validate_detail_string_lengths(value, field="goal_weakness_collision_details")

    @field_validator("goal_weakness_collision_detail", "training_preference", "mindset_challenges", "notes", mode="before")
    @classmethod
    def clean_preference_text(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator("primary_goal", "primary_weak_area", mode="before")
    @classmethod
    def clean_optional_preference_text(cls, value: Any) -> str | None:
        return _clean_optional_text(value)


class NutritionSharedCampContext(BaseModel):
    fight_date: str = ""
    rounds_format: str = ""
    weigh_in_type: WeighInType | None = None
    weigh_in_time: str | None = None
    current_weight_kg: float | None = None
    current_weight_recorded_at: str | None = None
    current_weight_source: WeightSource | None = None
    target_weight_kg: float | None = None
    target_weight_range_kg: list[float] | None = None
    phase_override: PhaseOverride | None = None
    fatigue_level: FatigueLevel | None = None
    weekly_training_frequency: int | None = None
    training_availability: list[str] = Field(default_factory=list)
    hard_sparring_days: list[str] = Field(default_factory=list)
    support_work_days: list[str] = Field(default_factory=list)
    session_types_by_day: dict[str, SessionDayType] = Field(default_factory=dict)
    injuries: str = Field(default="", max_length=INJURIES_MAX_CHARS)
    guided_injury: GuidedInjuryInput | None = None
    training_restriction_level: TrainingRestrictionLevel | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_support_work_days(cls, value: Any) -> Any:
        if isinstance(value, dict) and "support_work_days" not in value and "technical_skill_days" in value:
            updated = dict(value)
            updated["support_work_days"] = updated.get("technical_skill_days")
            return updated
        return value

    @field_validator(
        "training_availability",
        "hard_sparring_days",
        "support_work_days",
        mode="before",
    )
    @classmethod
    def clean_day_arrays(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return _clean_list([part.strip() for part in value.split(",")])
        if isinstance(value, list):
            return _clean_list(value)
        return _clean_list([value])

    @field_validator("training_availability", "hard_sparring_days", "support_work_days", mode="after")
    @classmethod
    def cap_day_array_items(cls, value: list[str], info: ValidationInfo) -> list[str]:
        return _validate_list_item_lengths(value, field=info.field_name or "day list", max_chars=PLAN_DAY_ITEM_MAX_CHARS)

    @field_validator("injuries", mode="before")
    @classmethod
    def clean_injuries(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator("rounds_format")
    @classmethod
    def validate_rounds_format(cls, value: str) -> str:
        return _validate_rounds_format(value)

    @field_validator("target_weight_range_kg", mode="before")
    @classmethod
    def validate_target_weight_range(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            parts = [part.strip() for part in stripped.split(",") if part.strip()]
        elif isinstance(value, list):
            parts = value
        else:
            raise ValueError("target_weight_range_kg must be a two-value array")

        if len(parts) != 2:
            raise ValueError("target_weight_range_kg must contain [lower, upper]")

        try:
            lower = float(parts[0])
            upper = float(parts[1])
        except (TypeError, ValueError):
            raise ValueError("target_weight_range_kg values must be numeric") from None
        if lower <= 0 or upper <= 0:
            raise ValueError("target_weight_range_kg values must be positive")
        if lower > upper:
            raise ValueError("target_weight_range_kg lower bound must be <= upper bound")
        return [lower, upper]

    @field_validator("current_weight_kg", "target_weight_kg", mode="before")
    @classmethod
    def coerce_optional_weight(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                return None
            try:
                return float(normalized)
            except ValueError:
                raise ValueError("weight value must be numeric") from None
        return value

    @field_validator("weekly_training_frequency", mode="before")
    @classmethod
    def coerce_frequency(cls, value: Any) -> int | None:
        return _parse_training_days_per_week(value)

    @field_validator("session_types_by_day", mode="before")
    @classmethod
    def clean_session_types_by_day(cls, value: Any) -> dict[str, SessionDayType]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("session_types_by_day must be an object")
        cleaned: dict[str, SessionDayType] = {}
        for key, entry in value.items():
            day = str(key or "").strip().lower()
            normalized_entry = str(entry or "").strip().lower()
            if day and normalized_entry:
                cleaned[day] = normalized_entry  # type: ignore[assignment]
        return cleaned


class NutritionBodyweightTrendPoint(BaseModel):
    """One day's scale reading and the smoothed trend through it (an estimate)."""

    date: str
    scale_weight_kg: float
    trend_weight_kg: float


WeightCutRiskBand = Literal["none", "moderate", "high", "severe"]


class NutritionDerivedState(BaseModel):
    days_until_fight: int | None = None
    weight_cut_pct: float = 0.0
    weight_cut_risk: bool = False
    # Canonical athlete-facing band and supervision flag from fightcamp.weight_cut,
    # the same reading the plan's nutrition block uses. Always none/False for a minor.
    weight_cut_risk_band: WeightCutRiskBand = "none"
    weight_cut_supervision_required: bool = False
    aggressive_weight_cut: bool = False
    high_pressure_weight_cut: bool = False
    short_notice: bool = False
    fight_week: bool = False
    readiness_flags: list[str] = Field(default_factory=list)
    fight_week_override_band: FightWeekOverrideBand = "none"
    current_phase_effective: str | None = None
    # Mean of one reading per day over today and the six days before it.
    rolling_7_day_average_weight: float | None = None
    rolling_7_day_weigh_in_days: int = 0
    # kg per week over the last four weeks; None until two weeks of readings exist.
    weekly_pace_kg: float | None = None
    bodyweight_trend: list[NutritionBodyweightTrendPoint] = Field(default_factory=list)
    foundation_status: FoundationStatus = "incomplete"
    missing_required_fields: list[str] = Field(default_factory=list)


class NutritionWorkspaceState(BaseModel):
    athlete_id: str
    source: NutritionWorkspaceSource = "default"
    intake_id: str | None = None
    nutrition_profile: NutritionProfileInput = Field(default_factory=NutritionProfileInput)
    shared_camp_context: NutritionSharedCampContext = Field(default_factory=NutritionSharedCampContext)
    s_and_c_preferences: NutritionSandCPreferences = Field(default_factory=NutritionSandCPreferences)
    nutrition_readiness: NutritionReadinessInput = Field(default_factory=NutritionReadinessInput)
    nutrition_monitoring: NutritionMonitoringInput = Field(default_factory=NutritionMonitoringInput)
    nutrition_coach_controls: NutritionCoachControlsInput = Field(default_factory=NutritionCoachControlsInput)
    derived: NutritionDerivedState = Field(default_factory=NutritionDerivedState)


class NutritionWorkspaceUpdateRequest(BaseModel):
    nutrition_profile: NutritionProfileInput = Field(default_factory=NutritionProfileInput)
    shared_camp_context: NutritionSharedCampContext = Field(default_factory=NutritionSharedCampContext)
    s_and_c_preferences: NutritionSandCPreferences = Field(default_factory=NutritionSandCPreferences)
    nutrition_readiness: NutritionReadinessInput = Field(default_factory=NutritionReadinessInput)
    nutrition_monitoring: NutritionMonitoringInput = Field(default_factory=NutritionMonitoringInput)
    nutrition_coach_controls: NutritionCoachControlsInput = Field(default_factory=NutritionCoachControlsInput)

    @field_validator("shared_camp_context")
    @classmethod
    def validate_weight_context(cls, value: NutritionSharedCampContext) -> NutritionSharedCampContext:
        if value.current_weight_kg is not None and value.current_weight_source is None:
            raise ValueError("current_weight_source is required when current_weight_kg is set")
        if value.current_weight_source == "manual" and not str(value.current_weight_recorded_at or "").strip():
            raise ValueError("current_weight_recorded_at is required when current_weight_source is manual")
        return value


CampTimelineType = Literal["scheduled_fight", "open_camp"]
_DEFAULT_OPEN_CAMP_WEEKS = 12
MAX_OPEN_CAMP_WEEKS = 24


class PlanRequest(BaseModel):
    athlete: AthleteProfileInput
    fight_date: str = ""
    # Optional event/competition name for the scheduled fight (e.g. "London
    # Regionals"). Display-only: shown on Home's fight countdown card.
    event_name: str = Field(default="", max_length=EVENT_NAME_MAX_CHARS)
    no_scheduled_fight: bool = False
    open_camp_weeks: int = _DEFAULT_OPEN_CAMP_WEEKS
    rounds_format: str = ""
    weekly_training_frequency: int | None = None
    fatigue_level: str = ""
    # Free-form text and list fields below carry field-level caps so oversized
    # payloads are rejected by validation before they reach the persistence
    # guards in ``json_limits``. Caps are generous relative to real submissions
    # (UI selections top out well under these) and exist to bound abuse, not to
    # enforce business rules.
    equipment_access: list[str] = Field(default_factory=list, max_length=64)
    training_availability: list[str] = Field(default_factory=list, max_length=64)
    hard_sparring_days: list[str] = Field(default_factory=list, max_length=64)
    support_work_days: list[str] = Field(default_factory=list, max_length=64)
    injuries: str = Field(default="", max_length=INJURIES_MAX_CHARS)
    guided_injury: GuidedInjuryInput | None = None
    guided_injuries: list[GuidedInjuryInput] | None = Field(default=None, max_length=GUIDED_INJURIES_MAX_ITEMS)
    key_goals: list[str] = Field(default_factory=list, max_length=32)
    primary_goal: str | None = Field(default=None, max_length=PLAN_LIST_ITEM_MAX_CHARS)
    weak_areas: list[str] = Field(default_factory=list, max_length=32)
    primary_weak_area: str | None = Field(default=None, max_length=PLAN_LIST_ITEM_MAX_CHARS)
    goal_weakness_collision_detail: str = Field(default="", max_length=GENERIC_PROFILE_NOTES_MAX_CHARS)
    goal_weakness_collision_tags: list[str] = Field(default_factory=list, max_length=64)
    goal_weakness_collision_details: list[dict[str, str]] = Field(default_factory=list, max_length=64)
    reduced_contact_requested: bool = False
    training_preference: str = Field(default="", max_length=TRAINING_PREFERENCE_MAX_CHARS)
    mindset_challenges: str = Field(default="", max_length=MENTAL_BLOCKERS_MAX_CHARS)
    notes: str = Field(default="", max_length=PREVIOUS_PLAN_FEEDBACK_MAX_CHARS)
    random_seed: int | None = None
    intake_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_support_work_days(cls, value: Any) -> Any:
        if isinstance(value, dict) and "support_work_days" not in value and "technical_skill_days" in value:
            updated = dict(value)
            updated["support_work_days"] = updated.get("technical_skill_days")
            return updated
        return value

    @model_validator(mode="before")
    @classmethod
    def infer_no_scheduled_fight_for_legacy_payloads(cls, value: Any) -> Any:
        # Backward compat for payloads that pre-date the ``no_scheduled_fight``
        # flag (PR #1263 shipped open camps via an empty ``fight_date`` alone).
        # When the flag is absent and the date is empty we treat the camp as
        # open. Callers that explicitly send ``no_scheduled_fight: false`` with
        # an empty date are left alone so ``generation_issues()`` can flag the
        # missing date.
        if not isinstance(value, dict):
            return value
        if "no_scheduled_fight" in value:
            return value
        fight_date_val = str(value.get("fight_date") or "").strip()
        if fight_date_val:
            return value
        updated = dict(value)
        updated["no_scheduled_fight"] = True
        return updated

    @field_validator("event_name", mode="before")
    @classmethod
    def clean_event_name(cls, value: Any) -> str:
        if value is None:
            return ""
        return " ".join(str(value).split())

    @field_validator("weekly_training_frequency", mode="before")
    @classmethod
    def validate_weekly_training_frequency(cls, value: Any) -> int | None:
        return _parse_training_days_per_week(value)

    @field_validator("no_scheduled_fight", mode="before")
    @classmethod
    def coerce_no_scheduled_fight(cls, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y", "on"}:
                return True
            if normalized in {"", "false", "0", "no", "n", "off"}:
                return False
        return bool(value)

    @field_validator("open_camp_weeks", mode="before")
    @classmethod
    def coerce_open_camp_weeks(cls, value: Any) -> int:
        if value is None:
            return _DEFAULT_OPEN_CAMP_WEEKS
        if isinstance(value, str):
            normalized = value.strip()
            if not normalized:
                return _DEFAULT_OPEN_CAMP_WEEKS
            try:
                value = float(normalized)
            except ValueError:
                raise ValueError("open_camp_weeks must be numeric") from None
        if isinstance(value, bool):
            raise ValueError("open_camp_weeks must be numeric")
        if isinstance(value, (int, float)):
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ValueError("open_camp_weeks must be numeric")
            parsed = int(round(numeric))
            if parsed < 1 or parsed > MAX_OPEN_CAMP_WEEKS:
                raise ValueError(f"open_camp_weeks must be between 1 and {MAX_OPEN_CAMP_WEEKS}")
            return parsed
        raise ValueError("open_camp_weeks must be numeric")

    @field_validator("equipment_access", "key_goals", "weak_areas", "goal_weakness_collision_tags", mode="before")
    @classmethod
    def clean_array_fields(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return _clean_list([part.strip() for part in value.split(",")])
        if isinstance(value, list):
            return _clean_list(value)
        return _clean_list([value])

    @field_validator("equipment_access", "key_goals", "weak_areas", "goal_weakness_collision_tags", mode="after")
    @classmethod
    def cap_array_field_items(cls, value: list[str], info: ValidationInfo) -> list[str]:
        return _validate_list_item_lengths(value, field=info.field_name or "list", max_chars=PLAN_LIST_ITEM_MAX_CHARS)

    @field_validator("goal_weakness_collision_details", mode="after")
    @classmethod
    def cap_collision_detail_strings(cls, value: list[dict[str, str]]) -> list[dict[str, str]]:
        return _validate_detail_string_lengths(value, field="goal_weakness_collision_details")

    @field_validator("injuries", "goal_weakness_collision_detail", "training_preference", "mindset_challenges", "notes", mode="before")
    @classmethod
    def clean_plan_text(cls, value: Any) -> str:
        return _clean_text(value)

    @field_validator("primary_goal", "primary_weak_area", mode="before")
    @classmethod
    def clean_optional_plan_text(cls, value: Any) -> str | None:
        return _clean_optional_text(value)

    @field_validator("rounds_format")
    @classmethod
    def validate_rounds_format(cls, value: str) -> str:
        return _validate_rounds_format(value)

    @field_validator("training_availability", "hard_sparring_days", "support_work_days", mode="before")
    @classmethod
    def clean_day_arrays(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return _clean_list([part.strip() for part in value.split(",")])
        if isinstance(value, list):
            return _clean_list(value)
        return _clean_list([value])

    @field_validator("training_availability", "hard_sparring_days", "support_work_days", mode="after")
    @classmethod
    def cap_day_array_items(cls, value: list[str], info: ValidationInfo) -> list[str]:
        return _validate_list_item_lengths(value, field=info.field_name or "day list", max_chars=PLAN_DAY_ITEM_MAX_CHARS)

    @field_validator("hard_sparring_days")
    @classmethod
    def validate_hard_sparring_days_cap(cls, value: list[str]) -> list[str]:
        if len(value) > _HARD_SPARRING_DAY_CAP:
            raise ValueError(f"hard sparring days cap is {_HARD_SPARRING_DAY_CAP}; reduce to {_HARD_SPARRING_DAY_CAP} or fewer to generate a plan")
        return value

    @model_validator(mode="after")
    def validate_schedule_days(self) -> "PlanRequest":
        normalized_training_days = {day.strip().lower() for day in self.training_availability if str(day).strip()}
        if self.weekly_training_frequency and normalized_training_days and self.weekly_training_frequency > len(normalized_training_days):
            raise ValueError("weekly_training_frequency cannot exceed selected training_availability days")

        invalid_hard_days = [day for day in self.hard_sparring_days if str(day).strip().lower() not in normalized_training_days]
        if invalid_hard_days:
            raise ValueError(
                f"hard_sparring_days must be included in training_availability: {', '.join(invalid_hard_days)}"
            )

        invalid_support_days = [day for day in self.support_work_days if str(day).strip().lower() not in normalized_training_days]
        if invalid_support_days:
            raise ValueError(
                f"support_work_days must be included in training_availability: {', '.join(invalid_support_days)}"
            )

        support_day_set = {day.strip().lower() for day in self.support_work_days if str(day).strip()}
        overlap_days = [day for day in self.hard_sparring_days if str(day).strip().lower() in support_day_set]
        if overlap_days:
            raise ValueError(
                f"hard_sparring_days and support_work_days must not overlap: {', '.join(overlap_days)}"
            )

        return self

    @property
    def effective_fight_date(self) -> str:
        """The fight date that actually counts, or "" for an open camp.

        An open camp can still carry a stale ``fight_date`` from an earlier
        submission, so every fight-date-driven decision must read this instead of
        the raw field.
        """
        return "" if self.no_scheduled_fight else self.fight_date

    def to_payload(self) -> dict[str, Any]:
        def _guided_injury_payload(guided: GuidedInjuryInput) -> dict[str, Any]:
            # Forward the full structured guided-injury contract. Stage 1
            # (``fightcamp.input_parsing._build_guided_injury``) and the injury
            # triage layer (``fightcamp.injury_triage`` /
            # ``fightcamp.guided_injury_resolver``) consume every field below to
            # classify the injury (e.g. fracture/dislocation/post_surgery via
            # ``injury_type``, surface injuries via ``surface_type``) and to
            # surface medical-safety signals (open wound, bleeding, infection).
            # Dropping any of these silently downgrades triage, so keep this
            # serialization in lockstep with ``GuidedInjuryInput``.
            return {
                "area": guided.area,
                "severity": guided.severity,
                "trend": guided.trend,
                "avoid": guided.avoid,
                "notes": guided.notes,
                "injury_type": guided.injury_type,
                "injury_subtypes": list(guided.injury_subtypes),
                "surface_type": guided.surface_type,
                "timeframe": guided.timeframe,
                "cleared": guided.cleared,
                "open_wound": guided.open_wound,
                "bleeding_status": guided.bleeding_status,
                "infection_signs": list(guided.infection_signs),
                "impact_related": guided.impact_related,
                "sensitive_area": guided.sensitive_area,
            }

        athlete = self.athlete
        normalized_fight_date = self.effective_fight_date
        fields = [
            _field("Full name", athlete.full_name),
            _field("Sex", athlete.sex),
            _field("Age", athlete.age),
            _field("Weight (kg)", athlete.weight_kg),
            _field("Target Weight (kg)", athlete.target_weight_kg),
            _field("Height (cm)", athlete.height_cm),
            _field("Fighting Style (Technical)", athlete.technical_style),
            _field("Fighting Style (Tactical)", athlete.tactical_style),
            _field("Stance", athlete.stance),
            _field("Professional Status", athlete.professional_status),
            _field("Current Record", athlete.record),
            _field("When is your next fight?", normalized_fight_date),
            _field("Athlete Time Zone", athlete.athlete_timezone),
            _field("Rounds x Minutes", self.rounds_format),
            _field("Training Days per Week", self.weekly_training_frequency),
            _field("Fatigue Level", self.fatigue_level),
            _field("Equipment Access", self.equipment_access),
            _field("Training Availability", self.training_availability),
            _field("Hard Sparring Days", self.hard_sparring_days),
            _field("Support Work Days", self.support_work_days),
            _field("Any injuries or areas you need to work around?", self.injuries),
            _field("What are your key performance goals?", self.key_goals),
            _field("Primary goal", self.primary_goal),
            _field("Where do you feel weakest right now?", self.weak_areas),
            _field("Primary weak area", self.primary_weak_area),
            _field("Goal/weak-area collision detail", self.goal_weakness_collision_detail),
            _field("Goal/weak-area collision tags", self.goal_weakness_collision_tags),
            _field("Do you prefer certain training styles?", self.training_preference),
            _field(
                "Do you struggle with any mental blockers or mindset challenges?",
                self.mindset_challenges,
            ),
            _field(
                "Are there any parts of your previous plan you hated or loved?",
                self.notes,
            ),
        ]
        camp_timeline_type: CampTimelineType = (
            "open_camp" if self.no_scheduled_fight else "scheduled_fight"
        )
        payload: dict[str, Any] = {
            "data": {"fields": fields},
            "no_scheduled_fight": self.no_scheduled_fight,
            "open_camp_weeks": self.open_camp_weeks,
            "camp_timeline_type": camp_timeline_type,
            "reduced_contact_requested": self.reduced_contact_requested,
        }
        if self.guided_injuries:
            # The frontend (buildGuidedInjuryFields) always sends BOTH the plural
            # ``guided_injuries`` list and a singular ``guided_injury`` mirror of
            # the first entry, so the plural list must take priority — otherwise
            # additional injuries are silently dropped. Stage 1
            # (fightcamp.input_parsing) consumes the plural key and parses every
            # entry; the singular key is kept for back-compat with callers that
            # only send it.
            payload["guided_injuries"] = [
                _guided_injury_payload(guided) for guided in self.guided_injuries
            ]
            payload["guided_injury"] = _guided_injury_payload(self.guided_injuries[0])
        elif self.guided_injury is not None:
            payload["guided_injury"] = _guided_injury_payload(self.guided_injury)
        if self.random_seed is not None:
            payload["random_seed"] = self.random_seed
        return payload


class ComplianceAcceptanceRequest(BaseModel):
    """Age, Terms and health-data consent submitted by the athlete.

    Deliberately carries *intent*, never evidence: no timestamps and no version
    strings. The server stamps both, so an acceptance cannot be backdated or
    attributed to a document version the athlete never saw. Every field is
    optional so the same endpoint serves first-time acceptance, a later Terms
    re-acceptance, and a standalone consent withdrawal.
    """

    # ISO ``YYYY-MM-DD``. Rejected below 13; the age band is derived from it and
    # never sent by the client.
    date_of_birth: str | None = Field(default=None, max_length=32)
    accept_terms: bool | None = None
    # True grants health-data consent, False withdraws it. Separate from
    # ``accept_terms`` on purpose: bundling the two would make the Article 9
    # consent non-specific and therefore invalid.
    health_data_consent: bool | None = None

    @field_validator("date_of_birth", mode="before")
    @classmethod
    def clean_date_of_birth(cls, value: Any) -> str | None:
        return _clean_optional_text(value)


class TrainingHabitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weekday: int = Field(strict=True, ge=0, le=6)  # Monday = 0
    activity: Literal["sparring", "boxing", "roadwork", "strength", "conditioning", "other"]
    # Optional, as on the manual sessions a routine repeats; absent on older routines.
    sport: Sport | None = None
    session_type: SessionType | None = None
    duration_minutes: int = Field(strict=True, ge=1, le=600)
    state: Literal["confirmed", "dismissed"]
    decided_on: date

    @model_validator(mode="after")
    def check_discipline(self):
        if error := discipline_error(self.activity, self.sport, self.session_type):
            raise ValueError(error)
        return self

    @field_validator("decided_on", mode="before")
    @classmethod
    def validate_decision_day(cls, value):
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError("Use a decision date in YYYY-MM-DD format.")
        return date.fromisoformat(value)


class TrainingHabitPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=True, strict=True)
    decisions: list[TrainingHabitDecision] = Field(default_factory=list, max_length=42)

    @model_validator(mode="after")
    def unique_slots(self):
        slots = [(item.weekday, item.activity, item.sport, item.session_type) for item in self.decisions]
        if len(slots) != len(set(slots)):
            raise ValueError("Use one decision per weekday and kind of session.")
        return self


class ProfileUpdateRequest(BaseModel):
    starting_path: Literal["tracking_first", "plan_first"] | None = None
    training_habits: TrainingHabitPreferences | None = None
    training_habits_revision: int | None = Field(default=None, strict=True, ge=0)

    @model_validator(mode="after")
    def require_habit_revision(self):
        if (self.training_habits is None) != (self.training_habits_revision is None):
            raise ValueError("Send training habits and their current revision together.")
        return self

    full_name: str | None = Field(default=None, max_length=ATHLETE_FULL_NAME_MAX_CHARS)
    technical_style: list[str] | None = Field(default=None, max_length=ATHLETE_STYLE_LIST_MAX_ITEMS)
    tactical_style: list[str] | None = Field(default=None, max_length=ATHLETE_STYLE_LIST_MAX_ITEMS)
    stance: str | None = Field(default=None, max_length=PROFILE_SHORT_TEXT_MAX_CHARS)
    professional_status: str | None = Field(default=None, max_length=PROFILE_SHORT_TEXT_MAX_CHARS)
    record: str | None = Field(default=None, max_length=RECORD_MAX_CHARS)
    athlete_timezone: str | None = Field(default=None, max_length=PROFILE_TIMEZONE_MAX_CHARS)
    athlete_locale: str | None = Field(default=None, max_length=PROFILE_LOCALE_MAX_CHARS)
    appearance_mode: AppearanceMode | None = None
    onboarding_draft: dict[str, Any] | None = None
    avatar_url: str | None = Field(default=None, max_length=AVATAR_URL_MAX_CHARS)
    nutrition_profile: NutritionProfileInput | None = None
    # Acknowledgement of the private trial instructions. The client sends the
    # intent, never a timestamp: the server stamps `private_trial_ack_at` so it
    # cannot be backdated. `false` clears the acknowledgement.
    private_trial_acknowledged: bool | None = None

    @field_validator("full_name", "stance", "professional_status", "record", "athlete_timezone", "athlete_locale", "avatar_url", mode="before")
    @classmethod
    def clean_profile_text(cls, value: Any) -> str | None:
        return _clean_optional_text(value)

    @field_validator("avatar_url")
    @classmethod
    def validate_avatar_url(cls, value: str | None) -> str | None:
        if not value:
            return value
        if not is_safe_avatar_url(value):
            raise ValueError("avatar_url must be an https:// link or a base64 JPEG, PNG, WebP or GIF image")
        return value

    @field_validator("technical_style", "tactical_style", mode="before")
    @classmethod
    def clean_style_lists(cls, value: Any) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            return _clean_list([part.strip() for part in value.split(",")])
        if isinstance(value, list):
            return _clean_list(value)
        return _clean_list([value])

    @field_validator("technical_style", "tactical_style", mode="after")
    @classmethod
    def cap_style_list_items(cls, value: list[str] | None, info: ValidationInfo) -> list[str] | None:
        if value is None:
            return None
        return _validate_list_item_lengths(value, field=info.field_name or "style", max_chars=ATHLETE_LIST_ITEM_MAX_CHARS)

    @field_validator("onboarding_draft")
    @classmethod
    def validate_onboarding_draft_size(cls, value: Any) -> Any:
        validated = validate_json_field(
            value,
            field="onboarding_draft",
            max_bytes=MAX_CLIENT_JSON_BYTES,
            max_depth=MAX_JSON_DEPTH,
        )
        return _validate_onboarding_draft_field_lengths(validated)

    @field_validator("record")
    @classmethod
    def validate_record(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_record(value)


class OnboardingDraftSaveRequest(BaseModel):
    onboarding_draft: dict[str, Any] | None = None
    full_name: str | None = Field(default=None, max_length=ATHLETE_FULL_NAME_MAX_CHARS)
    technical_style: list[str] | None = Field(default=None, max_length=ATHLETE_STYLE_LIST_MAX_ITEMS)
    tactical_style: list[str] | None = Field(default=None, max_length=ATHLETE_STYLE_LIST_MAX_ITEMS)
    stance: str | None = Field(default=None, max_length=PROFILE_SHORT_TEXT_MAX_CHARS)
    professional_status: str | None = Field(default=None, max_length=PROFILE_SHORT_TEXT_MAX_CHARS)
    record: str | None = Field(default=None, max_length=RECORD_MAX_CHARS)
    athlete_timezone: str | None = Field(default=None, max_length=PROFILE_TIMEZONE_MAX_CHARS)

    @field_validator("full_name", "stance", "professional_status", "record", "athlete_timezone", mode="before")
    @classmethod
    def clean_profile_text(cls, value: Any) -> str | None:
        return _clean_optional_text(value)

    @field_validator("technical_style", "tactical_style", mode="before")
    @classmethod
    def clean_style_lists(cls, value: Any) -> list[str] | None:
        if value is None:
            return None
        if isinstance(value, str):
            return _clean_list([part.strip() for part in value.split(",")])
        if isinstance(value, list):
            return _clean_list(value)
        return _clean_list([value])

    @field_validator("technical_style", "tactical_style", mode="after")
    @classmethod
    def cap_style_list_items(cls, value: list[str] | None, info: ValidationInfo) -> list[str] | None:
        if value is None:
            return None
        return _validate_list_item_lengths(value, field=info.field_name or "style", max_chars=ATHLETE_LIST_ITEM_MAX_CHARS)

    @field_validator("onboarding_draft")
    @classmethod
    def validate_onboarding_draft_size(cls, value: Any) -> Any:
        validated = validate_json_field(
            value,
            field="onboarding_draft",
            max_bytes=MAX_CLIENT_JSON_BYTES,
            max_depth=MAX_JSON_DEPTH,
        )
        return _validate_onboarding_draft_field_lengths(validated)

    @field_validator("record")
    @classmethod
    def validate_record(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_record(value)


class OnboardingDraftSaveResponse(BaseModel):
    ok: bool = True
    updated_at: str


class ManualStage2SubmissionRequest(BaseModel):
    # Generous cap relative to a normal generated plan: the automated Stage 2
    # call defaults to 6000 output tokens (~24k chars), so 80k chars leaves ~3x
    # headroom for a hand-written admin submission while preventing an accidental
    # paste from bloating the database or stalling plan rendering. ``max_length``
    # is enforced before the validator runs so oversize input is rejected with a
    # 422 rather than persisted.
    final_plan_text: str = Field(..., max_length=MANUAL_STAGE2_MAX_CHARS)

    @field_validator("final_plan_text")
    @classmethod
    def validate_final_plan_text(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("final_plan_text is required")
        return normalized


class ApproveAndResumeGenerationRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("reason is required")
        return normalized


class PlanRenameRequest(BaseModel):
    plan_name: str = Field(..., max_length=120)

    @field_validator("plan_name", mode="before")
    @classmethod
    def validate_plan_name(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("plan_name is required")
        return normalized


class PlanPermanentDeleteRequest(BaseModel):
    # Optional: archived plans are deleted without typed confirmation. The name
    # is only required when permanently deleting a plan that is not archived.
    confirm_plan_name: str | None = None

    @field_validator("confirm_plan_name")
    @classmethod
    def validate_confirm_plan_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None


PLAN_BULK_PERMANENT_DELETE_MAX = 100


class PlanBulkPermanentDeleteRequest(BaseModel):
    plan_ids: list[str]

    @field_validator("plan_ids")
    @classmethod
    def validate_plan_ids(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        normalized: list[str] = []
        for raw in value or []:
            plan_id = str(raw or "").strip()
            if not plan_id or plan_id in seen:
                continue
            seen.add(plan_id)
            normalized.append(plan_id)
        if not normalized:
            raise ValueError("plan_ids is required")
        # Cap the batch on the deduplicated list so a single request can never
        # fan out into an unbounded number of deletes.
        if len(normalized) > PLAN_BULK_PERMANENT_DELETE_MAX:
            raise ValueError(
                f"plan_ids cannot exceed {PLAN_BULK_PERMANENT_DELETE_MAX} unique ids per request"
            )
        return normalized


class PlanBulkPermanentDeleteResult(BaseModel):
    deleted: list[str]
    skipped: list[dict[str, str]]
    deleted_count: int
    skipped_count: int


USERNAME_MAX_CHANGES_PER_WINDOW = 4
USERNAME_CHANGE_WINDOW_DAYS = 30
USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 24
_USERNAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")


def validate_username(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        raise ValueError("username is required")
    if len(normalized) < USERNAME_MIN_LENGTH or len(normalized) > USERNAME_MAX_LENGTH:
        raise ValueError(
            f"username must be {USERNAME_MIN_LENGTH}-{USERNAME_MAX_LENGTH} characters long"
        )
    if not _USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "username may only contain lowercase letters, digits, dots, dashes, and underscores"
        )
    return normalized


class UsernameChangeRequest(BaseModel):
    username: str

    @field_validator("username")
    @classmethod
    def _validate(cls, value: str) -> str:
        return validate_username(value)


class UsernameRateLimitInfo(BaseModel):
    max_changes_per_window: int = USERNAME_MAX_CHANGES_PER_WINDOW
    window_days: int = USERNAME_CHANGE_WINDOW_DAYS
    remaining: int
    next_available_at: str | None = None


# Browser push endpoints are long opaque URLs; keys are base64url strings. The
# caps are generous versus real payloads and exist only to bound abuse.
PUSH_ENDPOINT_MAX_CHARS = 1024
PUSH_KEY_MAX_CHARS = 256


class PushSubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=PUSH_KEY_MAX_CHARS)
    auth: str = Field(min_length=1, max_length=PUSH_KEY_MAX_CHARS)


class PushSubscribeRequest(BaseModel):
    """A browser PushSubscription plus the device's IANA timezone."""

    endpoint: str = Field(min_length=1, max_length=PUSH_ENDPOINT_MAX_CHARS)
    keys: PushSubscriptionKeys
    timezone: str = Field(default="", max_length=PROFILE_TIMEZONE_MAX_CHARS)

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized.lower().startswith("https://"):
            raise ValueError("push endpoint must be an https URL")
        return normalized

    @field_validator("timezone", mode="before")
    @classmethod
    def _clean_timezone(cls, value: Any) -> str:
        return str(value or "").strip()


class PushUnsubscribeRequest(BaseModel):
    endpoint: str = Field(min_length=1, max_length=PUSH_ENDPOINT_MAX_CHARS)


class PushSettingsResponse(BaseModel):
    """What the client needs to offer push: server readiness + the VAPID public key."""

    enabled: bool
    public_key: str = ""


class ProfileRecord(BaseModel):
    starting_path: Literal["tracking_first", "plan_first"] | None = None
    training_habits: TrainingHabitPreferences = Field(default_factory=TrainingHabitPreferences)
    training_habits_revision: int = 0
    athlete_id: str
    email: str
    username: str | None = None
    username_change_history: list[str] = Field(default_factory=list)
    role: UserRole
    access_status: Literal["pending", "approved"] = "approved"
    full_name: str
    technical_style: list[str] = Field(default_factory=list)
    tactical_style: list[str] = Field(default_factory=list)
    stance: str = ""
    professional_status: str = ""
    record: str = ""
    athlete_timezone: str = ""
    athlete_locale: str = ""
    appearance_mode: AppearanceMode = "dark"
    onboarding_draft: dict[str, Any] | None = None
    avatar_url: str | None = None
    nutrition_profile: NutritionProfileInput = Field(default_factory=NutritionProfileInput)
    # Null until the athlete confirms they read the private trial instructions.
    # The web app gates onboarding on this, so it is part of every /api/me read.
    private_trial_ack_at: str | None = None
    # Compliance evidence. `date_of_birth` is the only input to the age band —
    # `is_minor` and `age_band` below are *derived server-side* on every read, so
    # a client that fabricates them changes nothing. Terms and health-data
    # consent are recorded separately (UK GDPR Art. 9(2)(a) requires the health
    # consent to be specific, separate and withdrawable).
    date_of_birth: str | None = None
    age_band: str = "unknown"
    is_minor: bool = True
    meets_minimum_age: bool = False
    terms_version: str | None = None
    terms_accepted_at: str | None = None
    terms_accepted: bool = False
    health_consent_version: str | None = None
    health_data_consent: bool = False
    health_consent_at: str | None = None
    health_consent_withdrawn_at: str | None = None
    health_consent_granted: bool = False
    created_at: str
    updated_at: str

    @property
    def profile_id(self) -> str:
        """Canonical ``profiles(id)`` value.

        ``athlete_id`` is the legacy response-field name and contains the
        profile primary key for every role, including admins and coaches.
        Server-only features should use this semantic alias when the submitter
        is not necessarily an athlete.
        """

        return self.athlete_id


class PlanSummary(BaseModel):
    custom_plan_id: str | None = None
    plan_id: str
    plan_name: str | None = None
    athlete_id: str
    full_name: str
    fight_date: str = ""
    technical_style: list[str] = Field(default_factory=list)
    created_at: str
    status: str = "generated"
    activation_state: Literal["eligible", "fight_date_passed", "status_ineligible"] = "status_ineligible"
    pdf_url: str | None = None
    review_reason: str | None = None


class ExerciseMedia(BaseModel):
    """A curated demo video for one exercise (public.exercise_media).

    Resolved at read time by api/services/exercise_media.py. It deliberately
    lives beside the structured plan rather than on StructuredBlock: the block
    model is also the Stage-2 model's output schema, and the model must never be
    the source of a video ID.
    """

    provider: Literal["youtube"] = "youtube"
    video_id: str
    start_s: int = 0
    end_s: int | None = None
    source: Literal["curated", "coach"] = "curated"
    # Shown with the player so a third-party demo credits its creator.
    channel_title: str | None = None
    # Frame shape for the player. None = not detected yet: render as landscape.
    orientation: Literal["portrait", "landscape"] | None = None


class PlanOutputs(BaseModel):
    plan_text: str
    pdf_url: str | None = None
    # Structured plan output (schema-first). Optional so legacy raw-text-only
    # plans keep working: when absent the frontend renders `plan_text` as the
    # markdown fallback. Populated once structured generation is available.
    structured_plan: StructuredTrainingPlan | None = None
    schema_version: str | None = None
    # Demo video per block, keyed by the block's exact display_name. Only
    # exercises with a verified video appear; everything else renders cues-only.
    exercise_media: dict[str, ExerciseMedia] = Field(default_factory=dict)


class PlanSafetyState(BaseModel):
    state: Literal["plan_ready", "restricted_rehab_only", "medical_hold", "needs_review"]
    status_chip: str
    header: str
    subtext: str
    stage2_skipped: bool = False
    clinician_clearance_required: bool = False
    matched_high_risk_categories: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    sparring_risk_band: Literal["green", "amber", "red", "black"] | None = None
    next_steps: list[str] = Field(default_factory=list)


class StructuredCardState(BaseModel):
    state: Literal["live", "building", "failed", "not_attempted", "none"] = "none"
    reasons: list[str] = Field(default_factory=list)
    schema_version: str | None = None
    attempt_started_at: str | None = None


class PlanAdvisory(BaseModel):
    kind: Literal["sparring_adjustment"]
    action: Literal["deload", "convert"]
    risk_band: Literal["green", "amber", "red", "black"] | None = None
    phase: str
    week_label: str
    days: list[str] = Field(default_factory=list)
    title: str
    reason: str
    suggestion: str
    replacement: str | None = None
    disclaimer: str


class PlanScheduleContext(BaseModel):
    """Read-only timing projection for Plan Detail.

    This is derived from persisted plan inputs; it is not a second scheduling
    source of truth and does not require a database migration.
    """

    schedule_mode: Literal["event_countdown", "open_recurring", "static_undated"]
    projection_status: Literal["not_required", "projected", "unavailable"]
    # Populated only when an open plan fails to project (projection_status
    # "unavailable"): names the fail-closed guard that tripped so an undated
    # open plan can be diagnosed from one logged field instead of a code trace.
    projection_reason: str | None = None
    anchor_date: str | None = None
    current_training_day: str | None = None
    # Athlete-local creation day of the plan (YYYY-MM-DD), from the same
    # athlete-local basis as ``current_training_day``.
    plan_start_day: str | None = None
    block_number: int | None = None
    current_week_number: int | None = None


class WeeklyDayEntry(BaseModel):
    weekday: Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    title: str = ""
    sparring_day_class: SparringDayClass = "none"
    effective_load: EffectiveLoad = "none"
    status: str = ""
    reason: str = ""
    coach_note: str = ""
    reason_codes: list[str] = Field(default_factory=list)
    d_day: int | None = None
    day_label: str = ""
    weekday_with_label: str = ""
    calendar_date: str | None = None
    is_fight_day: bool = False
    is_after_fight_day: bool = False


class WeeklySchedule(BaseModel):
    plan_id: str
    week_index: int
    week_count: int
    phase: str = ""
    projected_days_until_fight_start: int | None = None
    projected_days_until_fight_end: int | None = None
    day_label: str = ""
    countdown_range: list[int] = Field(default_factory=list)
    week_countdown_label: str = ""
    week_label_with_countdown: str = ""
    days: list[WeeklyDayEntry]


class AdminIntakeInjuries(BaseModel):
    """The injuries an athlete reported at intake, as stored (admin view only)."""

    injuries: str = ""
    guided_injuries: list[dict[str, Any]] = Field(default_factory=list)


class AdminPlanOutputs(BaseModel):
    coach_notes: str = ""
    why_log: dict[str, Any] = Field(default_factory=dict)
    planning_brief: dict[str, Any] | None = None
    stage2_payload: dict[str, Any] | None = None
    parsing_metadata: dict[str, Any] = Field(default_factory=dict)
    stage2_handoff_text: str = ""
    draft_plan_text: str = ""
    final_plan_text: str = ""
    stage2_retry_text: str = ""
    stage2_validator_report: dict[str, Any] = Field(default_factory=dict)
    stage2_status: str = ""
    stage2_attempt_count: int = 0
    # Structured-plan generation debug visibility. ``structured_plan_status`` is
    # one of not_attempted / valid / repair_attempted_valid / invalid_fallback_used.
    # ``structured_plan_errors`` carries validation errors when structured JSON
    # failed; ``structured_schema_version`` is the saved schema version when valid.
    structured_plan_status: str = "not_attempted"
    structured_plan_errors: list[str] = Field(default_factory=list)
    structured_schema_version: str | None = None
    # Filled for plans injury triage held (api/services/plan_intake_injuries.py).
    intake_injuries: AdminIntakeInjuries | None = None


class ActiveInjuryRegion(BaseModel):
    """A body region the athlete is currently injured in, plus its match terms.

    ``terms`` are normalized (lowercase, punctuation collapsed) location synonyms
    and rehab-bank drill names. A rehab block whose text contains any of them is
    targeting this live injury and keeps the "Rehab" label.
    """

    region: str
    terms: list[str] = Field(default_factory=list)


class RehabLabelPolicy(BaseModel):
    """How to label each of a plan's rehab blocks — see api.rehab_labels.

    ``default_mode`` is what a rehab block reads as when it matches no active
    region: "prehab" (the work is prophylactic) once every live injury has been
    localized, "rehab" while an unlocalizable injury is open.
    """

    default_mode: RehabLabelMode = "rehab"
    active_regions: list[ActiveInjuryRegion] = Field(default_factory=list)


class PlanDetail(PlanSummary):
    outputs: PlanOutputs
    safety_state: PlanSafetyState
    structured_card_state: StructuredCardState = Field(default_factory=StructuredCardState)
    advisories: list[PlanAdvisory] = Field(default_factory=list)
    admin_outputs: AdminPlanOutputs | None = None
    plan_source: str | None = None
    schedule_context: PlanScheduleContext | None = None
    # Athlete-safe signal (not gated behind admin_outputs): true when the stored
    # profile could not be refreshed during generation, so this plan was built
    # from the submitted intake and the saved profile may be stale. Derived from
    # the plan row's why_log marker in plan_mappers._map_plan_detail.
    profile_refresh_failed: bool = False
    # Per-region Rehab/Prehab labelling for this plan's rehab blocks. Derived
    # server-side from the athlete's live injury flags (not the intake
    # medical-clearance answer). A rehab block reads "Rehab" only while the body
    # region it targets is actually injured; everything else is "Prehab". See
    # api.rehab_labels.resolve_rehab_label_policy.
    rehab_label_policy: RehabLabelPolicy = Field(default_factory=RehabLabelPolicy)


class ProgressMilestone(BaseModel):
    code: str
    label: str
    detail: str = ""
    at: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class GenerationJobResponse(BaseModel):
    job_id: str
    athlete_id: str
    client_request_id: str
    status: GenerationJobStatus
    created_at: str
    updated_at: str
    started_at: str | None = None
    heartbeat_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    plan_id: str | None = None
    latest_plan_id: str | None = None
    status_url: str | None = None
    message: str | None = None
    progress_milestones: list[ProgressMilestone] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    can_retry: bool = False
    stage2_status: str | None = None
    requires_admin_resume: bool = False
    ready_to_open: bool = False
    # How the job was started (self_serve, quick_build, admin_triage_resume, ...).
    # The athlete's status bar says when an admin resumed a held build.
    source: str | None = None


class GenerationRequestPayloadSummary(BaseModel):
    athlete_name: str = ""
    technical_style: list[str] = Field(default_factory=list)
    fight_date: str = ""
    phase: str = ""
    fight_format: str = ""
    fatigue_level: str = ""
    goals: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    injuries: list[str] = Field(default_factory=list)
    training_availability: str = ""


class AdminGenerationJobDiagnostic(BaseModel):
    job_id: str
    athlete_id: str = ""
    athlete_email: str = ""
    athlete_full_name: str = ""
    intake_id: str | None = None
    status: GenerationJobStatus
    source: str = ""
    created_at: str
    started_at: str | None = None
    heartbeat_at: str | None = None
    completed_at: str | None = None
    client_request_id: str = ""
    retry_of: str | None = None
    error: str | None = None
    stale_reason: str | None = None
    plan_id: str | None = None
    can_retry: bool = False
    stage2_status: str | None = None
    requires_admin_resume: bool = False
    is_stale: bool = False
    profile_unavailable: bool = False
    warnings: list[str] = Field(default_factory=list)
    request_payload_summary: GenerationRequestPayloadSummary = Field(default_factory=GenerationRequestPayloadSummary)


class MeResponse(BaseModel):
    profile: ProfileRecord
    effective_admin: bool = False
    latest_intake: dict[str, Any] | None = None
    latest_plan: PlanSummary | None = None
    plan_count: int = 0
    username_rate_limit: UsernameRateLimitInfo


class AdminAthleteRecord(BaseModel):
    last_meaningful_activity_at: str | None = None
    athlete_id: str
    email: str
    role: UserRole
    access_status: Literal["pending", "approved"] = "approved"
    full_name: str
    technical_style: list[str] = Field(default_factory=list)
    tactical_style: list[str] = Field(default_factory=list)
    stance: str = ""
    professional_status: str = ""
    record: str = ""
    athlete_timezone: str = ""
    athlete_locale: str = ""
    appearance_mode: AppearanceMode = "dark"
    onboarding_draft: dict[str, Any] | None = None
    latest_intake: dict[str, Any] | None = None
    nutrition_profile: NutritionProfileInput = Field(default_factory=NutritionProfileInput)
    created_at: str
    updated_at: str
    plan_count: int = 0
    latest_plan_created_at: str | None = None
    first_usable_plan_at: str | None = None
    time_to_first_usable_plan_seconds: int | None = None


class AdminTimeToFirstPlanSummary(BaseModel):
    """Signup → first usable plan, for athletes who signed up in the window."""

    window_days: int
    signups: int
    with_usable_plan: int
    median_seconds: int | None = None
    p75_seconds: int | None = None


class AdminLatestIntakeUpdateRequest(BaseModel):
    fight_date: str | None = None
    no_scheduled_fight: bool | None = None
    rounds_format: str | None = None
    weekly_training_frequency: int | None = None
    training_availability: list[str] | None = Field(default=None, max_length=64)
    equipment_access: list[str] | None = Field(default=None, max_length=64)
    key_goals: list[str] | None = Field(default=None, max_length=32)
    weak_areas: list[str] | None = Field(default=None, max_length=32)
    injuries: str | None = Field(default=None, max_length=INJURIES_MAX_CHARS)

    @field_validator("injuries", mode="before")
    @classmethod
    def clean_injuries(cls, value: Any) -> str | None:
        return _clean_optional_text(value)

    @field_validator("training_availability", mode="after")
    @classmethod
    def cap_day_items(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _validate_list_item_lengths(_clean_list(value), field="training_availability", max_chars=PLAN_DAY_ITEM_MAX_CHARS)

    @field_validator("equipment_access", "key_goals", "weak_areas", mode="after")
    @classmethod
    def cap_list_items(cls, value: list[str] | None, info: ValidationInfo) -> list[str] | None:
        if value is None:
            return None
        return _validate_list_item_lengths(_clean_list(value), field=info.field_name or "list", max_chars=PLAN_LIST_ITEM_MAX_CHARS)


class AdminPlanSummary(PlanSummary):
    athlete_email: str
    profile_unavailable: bool = False


# ---------------------------------------------------------------------------
# Injury flags, adaptation notes, and the admin review queue.
# See api/routes/daily.py and api/readiness.py.
# ---------------------------------------------------------------------------

ReadinessState = Literal["ready", "caution", "high_fatigue", "injury_flag"]
AdaptationDecisionValue = Literal[
    "keep_plan",
    "reduce_intensity",
    "swap_session",
    "add_recovery",
    "flag_admin_review",
]
InjuryFlagSeverity = Literal["mild", "moderate", "severe"]
# Who owns an injury flag's current severity: the athlete's own choice, or a
# floor the surface (skin) evaluator derived from the structured wound answers.
# Only a system-applied floor may be released automatically.
InjurySeveritySource = Literal["manual", "surface_system"]
InjuryFlagStatus = Literal["open", "monitoring", "resolved"]
# How a rehab block is labelled in the viewer. "rehab" while the body region it
# targets is actively injured; "prehab" once that injury clears and the work is
# purely prophylactic.
RehabLabelMode = Literal["rehab", "prehab"]
InjuryReportedStatus = Literal["ongoing", "improving", "worse", "resolved"]
AdminReviewStatus = Literal["pending", "acknowledged", "resolved"]

# Structured surface (skin) safety vocabulary, re-exported from the check-in
# contract so the API schema and the routing logic can never drift apart.
SkinIntegrity = _SkinIntegrity
BleedingStatus = _BleedingStatus
Drainage = _Drainage
Coverable = _Coverable
FrictionOrContactProblem = _FrictionOrContactProblem
SurfaceInjuryClass = Literal[
    "non_surface",
    "stable_surface",
    "surface_local_restriction",
    "surface_no_contact",
    "surface_medical_review",
]
# Rehabilitation stage vocabulary, derived from the canonical enum rather than
# restated, so the API schema can never drift from fightcamp.rehab_schema.
RehabStage = Literal[_REHAB_STAGES]  # type: ignore[valid-type]
RehabCarePathway = Literal[_CARE_TYPES]  # type: ignore[valid-type]
# The two post-rehab answer vocabularies, derived the same way so the athlete
# can only send back one of the options the server offered.
RehabDuringResponse = Literal[_DURING_ANSWERS]  # type: ignore[valid-type]
RehabLimitResponse = Literal[_LIMIT_ANSWERS]  # type: ignore[valid-type]
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DAILY_NOTE_MAX_CHARS = 2000


def _validate_optional_iso_date(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not _DATE_PATTERN.match(text):
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD)")
    return text


class InjuryFlagCreateRequest(BaseModel):
    body_area: str = Field(default="", max_length=200)
    description: str = Field(min_length=1, max_length=DAILY_NOTE_MAX_CHARS)
    severity: InjuryFlagSeverity = "moderate"

    @field_validator("body_area", "description", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return str(value or "").strip()


class InjuryFlagUpdateRequest(BaseModel):
    status: InjuryFlagStatus


class RehabScheduleRecord(BaseModel):
    state: Literal["due", "recovery_day", "already_completed", "held", "deferred", "unsupported"]
    reason: str
    next_due_day: str | None = None


class RehabPrescriptionSummary(BaseModel):
    model_config = ConfigDict(extra="allow")
    sources: list[str] = Field(default_factory=list)


class InjuryRehabDecisionRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    outcome: str
    summary: str
    reason_codes: list[str] = Field(default_factory=list)
    prescription: RehabPrescriptionSummary | None = None
    schedule: RehabScheduleRecord | None = None
    achilles_load_review: AchillesRestoreLoadReview | None = None


class AthleteClearanceReport(BaseModel):
    model_config = ConfigDict(extra="allow")
    episode_id: str
    scopes: list[str]
    source: Literal["athlete_reported"]
    externally_verified: Literal[False] = False


class InjuryFlagRecord(BaseModel):
    id: str
    athlete_id: str
    plan_id: str | None = None
    source: str = "checkin"
    episode_id: str | None = None
    body_region: str | None = None
    side: Literal["left", "right", "bilateral", "unknown"] = "unknown"
    body_area: str = ""
    description: str
    severity: InjuryFlagSeverity = "moderate"
    # Provenance for ``severity``. ``surface_system`` means the current value is a
    # floor the surface (skin) evaluator applied from the structured wound
    # answers, and ``manual_severity`` carries the athlete's own severity
    # underneath it so a later clean recheck can release the floor. ``None`` reads
    # as manual — a severity the system did not raise is never auto-lowered.
    severity_source: InjurySeveritySource | None = None
    manual_severity: InjuryFlagSeverity | None = None
    status: InjuryFlagStatus = "open"
    latest_reported_status: InjuryReportedStatus = "ongoing"
    # Structured surface (skin) safety answers, recorded by the injury check-in's
    # conditional follow-up. Optional throughout: an injury that never needed the
    # follow-up simply carries none, and the classifier reads a missing answer as
    # "unknown" rather than "clear".
    skin_integrity: SkinIntegrity | None = None
    bleeding_status: BleedingStatus | None = None
    drainage: Drainage | None = None
    infection_signs: list[str] = Field(default_factory=list, max_length=MAX_INFECTION_SIGNS)
    coverable: Coverable | None = None
    friction_or_contact_problem: FrictionOrContactProblem | None = None
    # Canonical surface classification, computed server-side so the UI never
    # re-derives it (see fightcamp.injury_registry.classify_surface_injury).
    surface_class: SurfaceInjuryClass | None = None
    # Resolved rehabilitation stage for THIS injury, derived server-side from the
    # injury record plus the athlete's check-in/session history (see
    # api.contracts.rehab_stage). It is independent of the camp phase: GPP/SPP/
    # TAPER is fight-camp periodisation and cannot advance a rehab stage.
    # ``None`` means either "not resolved" or, on the wound-care pathway, that the
    # musculoskeletal ladder does not apply — ``rehab_care_pathway`` says which.
    rehab_stage: RehabStage | None = None
    # Machine-readable evidence codes behind the stage, never athlete-facing copy.
    rehab_stage_reasons: list[str] = Field(default_factory=list)
    rehab_care_pathway: RehabCarePathway | None = None
    rehab_decision: InjuryRehabDecisionRecord | None = None
    clinician_clearance: AthleteClearanceReport | None = None
    resolved_at: str | None = None
    created_at: str = ""
    updated_at: str = ""


class AdaptationNoteRecord(BaseModel):
    id: str
    athlete_id: str
    plan_id: str | None = None
    checkin_id: str | None = None
    session_log_id: str | None = None
    rule_code: str
    decision: AdaptationDecisionValue
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class ReadinessSummary(BaseModel):
    state: ReadinessState = "ready"
    label: str = "Ready"
    reasons: list[str] = Field(default_factory=list)


class AdminReviewRecord(BaseModel):
    id: str
    athlete_id: str
    athlete_email: str = ""
    athlete_name: str = ""
    adaptation_note_id: str | None = None
    injury_flag_id: str | None = None
    reason: str
    status: AdminReviewStatus = "pending"
    resolution_notes: str = ""
    resolved_by: str = ""
    resolved_at: str | None = None
    created_at: str = ""


class AdminReviewResolveRequest(BaseModel):
    status: Literal["acknowledged", "resolved"] = "resolved"
    resolution_notes: str = Field(default="", max_length=DAILY_NOTE_MAX_CHARS)

    @field_validator("resolution_notes", mode="before")
    @classmethod
    def clean_notes(cls, value: Any) -> str:
        return str(value or "").strip()


# ---------------------------------------------------------------------------
# Block 4 Today/Overview persistence (api/routes/today.py,
# api/services/today_service.py). The categorical Today check-in carries the
# six structured inputs + red-flag safety toggles; the recommendation is always
# computed server-side via api.contracts.checkin_decision.evaluate_checkin().
# Any client-supplied recommendation field is ignored (extra inputs dropped).
# ---------------------------------------------------------------------------


class TodayCheckinRequest(BaseModel):
    # Omitted for an athlete without an active UNLXCK programme. The server
    # decides which applies; a missing plan is never filled in.
    plan_id: str | None = Field(default=None, min_length=1)
    sleep: CheckinSleep
    body: CheckinBody
    pain: CheckinPain
    # A camp phase exists only with a plan, and is never invented without one.
    phase: CheckinPhase | None = None
    active_injury: CheckinActiveInjury = "none"
    previous_session: CheckinPreviousSession = "none"
    sharp_pain: bool = False
    instability: bool = False
    swelling: bool = False
    neurological_symptoms: bool = False
    illness_symptoms: bool = False
    cannot_warm_into_movement: bool = False
    worse_next_day_pain: bool = False

    @model_validator(mode="after")
    def phase_matches_plan(self) -> "TodayCheckinRequest":
        if (self.plan_id is None) != (self.phase is None):
            raise ValueError("a camp phase is sent with a plan_id, and only then")
        return self


class TodayCheckinRecord(BaseModel):
    id: str
    athlete_id: str
    plan_id: str | None = None
    training_day: str
    athlete_timezone: str = ""
    sleep: CheckinSleep
    body: CheckinBody
    pain: CheckinPain
    phase: CheckinPhase | None = None
    active_injury: CheckinActiveInjury = "none"
    previous_session: CheckinPreviousSession = "none"
    sharp_pain: bool = False
    instability: bool = False
    swelling: bool = False
    neurological_symptoms: bool = False
    illness_symptoms: bool = False
    cannot_warm_into_movement: bool = False
    worse_next_day_pain: bool = False
    recommendation_state: CheckinDecisionValue
    recommendation_reason: str = ""
    recommendation_triggers: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class TodayCheckinResponse(BaseModel):
    checkin: TodayCheckinRecord
    training_day: str
    recommendation_state: CheckinDecisionValue
    recommendation_reason: str = ""
    triggers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # Backend-owned typed safety contract (additive; the fields above are
    # unchanged). The frontend keys safety behaviour off these machine-typed
    # fields instead of parsing recommendation prose, so a copy change can never
    # change safety behaviour. See api/services/readiness_failsafe.py.
    #   decision        — train_as_planned | modify | pull_back (same as state).
    #   decision_tier   — clear | caution | stop.
    #   display_state   — ready | modify | hold | unavailable.
    #   reason_codes    — structured codes (context failures first, then triggers).
    #   blocks_training — authoritative "training is blocked" flag.
    decision: str = ""
    decision_tier: str = ""
    display_state: str = ""
    reason_codes: list[str] = Field(default_factory=list)
    title: str = ""
    detail: str = ""
    action: str = ""
    safety: str = ""
    blocks_training: bool = False


class SessionCompletionRequest(BaseModel):
    plan_id: str | None = Field(default=None, min_length=1)
    session_id: str = Field(min_length=1)
    status: CompletionStatus
    prescription_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    rehab_tracking: Literal["injury_checkin"] | None = None
    rehab_performance: Literal["done_as_shown", "changed", "stopped", "skipped"] | None = None
    # Omitted for the normal Today flow (the server resolves the athlete-local
    # training day). A retro-log passes an explicit past day; the service
    # enforces the back-fill window and terminal-status rule.
    training_day: str | None = None
    session_rpe: int | None = Field(default=None, ge=1, le=10)
    pain_after: int | None = Field(default=None, ge=0, le=10)
    # Carries the required explanation for both modified and skipped sessions.
    modification_reason: str = Field(default="", max_length=DAILY_NOTE_MAX_CHARS)
    notes: str = Field(default="", max_length=DAILY_NOTE_MAX_CHARS)

    @field_validator("modification_reason", "notes", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("training_day")
    @classmethod
    def validate_training_day(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            date.fromisoformat(cleaned)
        except ValueError as exc:
            raise ValueError("training_day must be a YYYY-MM-DD date") from exc
        return cleaned


class SessionCompletionRecordResponse(BaseModel):
    id: str
    athlete_id: str
    plan_id: str | None
    session_id: str
    training_day: str
    status: CompletionStatus = "not_started"
    session_rpe: int | None = None
    pain_after: int | None = None
    modification_reason: str = ""
    notes: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
    prescription_snapshot: dict[str, Any] | None = None
    rehab_performance: Literal["done_as_shown", "changed", "stopped", "skipped"] | None = None


class SessionCompletionHistoryResponse(SessionCompletionRecordResponse):
    session_title: str | None = None


SparringIntensity = Literal["light", "medium", "hard"]
SparringPlannedIntensity = Literal["hard", "light", "technical", "contact"]
SparringHeadContact = Literal["none", "light", "heavy"]
SparringLogSource = Literal["contact", "session", "free"]
SPARRING_NOTE_MAX_CHARS = 1000
SPARRING_FOCUS_MAX_CHARS = 140


class SparringLogRequest(BaseModel):
    """A post-sparring entry from the round timer.

    The training day is always the server's athlete-local day; the client never
    back-dates contact. ``planned_intensity`` is what the plan called for,
    ``intensity`` what the athlete says actually happened.
    """

    model_config = ConfigDict(extra="forbid")

    plan_id: str | None = Field(default=None, max_length=64)
    session_id: str | None = Field(default=None, max_length=200)
    source: SparringLogSource
    # The manually logged sparring session this report details. The manual
    # session stays the training record; this adds its contact evidence.
    external_session_id: UUID | None = None
    # Generated once per set of answers, so a retried save cannot write twice.
    client_request_id: UUID | None = None
    # Increases with each new set of answers in one report, so a late older
    # request can never overwrite a newer correction.
    detail_revision: int = Field(default=0, ge=0, le=10_000)
    planned_intensity: SparringPlannedIntensity | None = None
    intensity: SparringIntensity
    rounds_completed: int = Field(ge=0, le=30)
    round_seconds: int | None = Field(default=None, ge=5, le=3600)
    head_contact: SparringHeadContact
    rocked: bool = False
    notes: str = Field(default="", max_length=SPARRING_NOTE_MAX_CHARS)
    # Optional: one thing to work on next time. Never safety or workload data.
    focus: str = Field(default="", max_length=SPARRING_FOCUS_MAX_CHARS)

    @field_validator("notes", "focus", mode="before")
    @classmethod
    def clean_notes(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("plan_id", "session_id", mode="before")
    @classmethod
    def clean_optional_id(cls, value: Any) -> str | None:
        cleaned = str(value or "").strip()
        return cleaned or None

    @model_validator(mode="after")
    def manual_detail_is_unplanned(self) -> "SparringLogRequest":
        # A manual session is not a prescription: it carries no plan, session or
        # planned intensity, so none can be attached to its detail.
        if self.external_session_id is not None and (
            self.source != "free" or self.plan_id or self.session_id or self.planned_intensity
        ):
            raise ValueError("a manual sparring detail must use source 'free' without plan fields")
        return self


class SparringLogRecord(BaseModel):
    id: str
    athlete_id: str
    plan_id: str | None = None
    session_id: str | None = None
    training_day: str
    source: SparringLogSource
    external_session_id: str | None = None
    # The latest applied correction, so a report reopened later continues from it.
    detail_revision: int = 0
    planned_intensity: SparringPlannedIntensity | None = None
    intensity: SparringIntensity
    rounds_completed: int
    round_seconds: int | None = None
    head_contact: SparringHeadContact
    rocked: bool = False
    notes: str = ""
    focus: str = ""
    created_at: str = ""


class TrainingFocusResponse(BaseModel):
    """The athlete's latest recorded sparring focus, for the next relevant session.

    ``focus`` is None when there is nothing recent to show. ``sport`` is the
    sport it was recorded for when known (a manual session's sport), else None.
    """

    focus: str | None = None
    training_day: str | None = None
    sport: str | None = None


class SparringLogResponse(BaseModel):
    log: SparringLogRecord
    # True when the entry raised an admin review (a reported rocked/dropped).
    review_created: bool = False
    # Shown to the athlete as-is when the entry needs a safety message.
    safety_notice: str | None = None


class SparringWindowSummary(BaseModel):
    """Sparring totals over the athlete-local days ending today (inclusive)."""

    days: int
    sessions: int = 0
    rounds: int = 0
    hard_rounds: int = 0
    # Distinct training days with a hard entry: hard sparring is usually capped
    # at one or two days a week, so days (not entries) is what is counted.
    hard_days: int = 0
    heavy_head_contact_sessions: int = 0
    rocked_count: int = 0


class SparringLogHistoryResponse(BaseModel):
    """The athlete's own sparring log, newest first, with recent exposure."""

    logs: list[SparringLogRecord]
    # The athlete-local day the windows end on, for "N days ago" labels.
    current_training_day: str
    last_7_days: SparringWindowSummary
    last_28_days: SparringWindowSummary
    # Most recent days found in the athlete's log, or null when there are none.
    last_hard_day: str | None = None
    last_rocked_day: str | None = None


ExerciseLogStatus = Literal["as_prescribed", "modified", "skipped"]
ExerciseLogReason = Literal["equipment", "fatigue", "pain", "felt_strong"]
EXERCISE_LOG_NOTE_MAX_CHARS = 500
_EXERCISE_LOG_TIME_UNITS = frozenset({"seconds", "minutes"})
_EXERCISE_LOG_MEASURE_UNITS: dict[str, frozenset[str]] = {
    "load": frozenset({"kg", "lb"}),
    "duration": _EXERCISE_LOG_TIME_UNITS,
    "work": _EXERCISE_LOG_TIME_UNITS,
    "rest": _EXERCISE_LOG_TIME_UNITS,
    "distance": frozenset({"meters", "km", "miles"}),
}


class ExerciseLogMeasure(BaseModel):
    """A quantity the athlete reports, in the plan's ``{value, unit}`` shape."""

    model_config = ConfigDict(extra="forbid")

    value: float = Field(ge=0, le=100000)
    unit: str = Field(min_length=1, max_length=16)


class ExerciseLogEffort(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["RPE"] = "RPE"
    value: float = Field(ge=1, le=10)


class ExerciseSetResult(BaseModel):
    """One confirmed set. Missing measurements stay unknown."""

    model_config = ConfigDict(extra="forbid")
    set_number: int | None = Field(default=None, strict=True, ge=1, le=50)
    reps: int | None = Field(default=None, strict=True, ge=1, le=1000)
    load: ExerciseLogMeasure | None = None
    rpe: float | None = Field(default=None, strict=True, ge=1, le=10)

    @field_validator("rpe")
    @classmethod
    def half_step_rpe(cls, value):
        if value is not None and value * 2 != int(value * 2):
            raise ValueError("RPE must be a whole or half number.")
        return value

    @field_validator("load", mode="before")
    @classmethod
    def numeric_load(cls, value):
        if isinstance(value, dict):
            number = value.get("value")
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                raise ValueError("Set load must be a number.")
        return value

    @model_validator(mode="after")
    def valid_load(self):
        if self.load is not None:
            limit = {"kg": 1000, "lb": 2200}.get(self.load.unit)
            if limit is None or not math.isfinite(self.load.value) or self.load.value > limit:
                raise ValueError("Set load must be a valid weight in kg or lb.")
            if round(self.load.value, 2) != self.load.value:
                raise ValueError("Set load allows up to two decimal places.")
        return self


class ExerciseLogActual(BaseModel):
    """What the athlete actually did for one block.

    Same field names and shapes as the block's own prescription, so prescribed
    and actual compare field by field. Every field is optional: the athlete
    reports only what applies to the exercise (a plank has a duration and no
    reps; a lift has sets, reps and the weight on the bar).
    """

    model_config = ConfigDict(extra="forbid")

    sets: int | None = Field(default=None, ge=0, le=50)
    reps: int | None = Field(default=None, ge=0, le=1000)
    rounds: int | None = Field(default=None, ge=0, le=100)
    load: ExerciseLogMeasure | None = None
    duration: ExerciseLogMeasure | None = None
    work: ExerciseLogMeasure | None = None
    rest: ExerciseLogMeasure | None = None
    distance: ExerciseLogMeasure | None = None
    effort: ExerciseLogEffort | None = None
    set_entries: list[ExerciseSetResult] | None = Field(default=None, min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_units(self) -> "ExerciseLogActual":
        for field_name, allowed in _EXERCISE_LOG_MEASURE_UNITS.items():
            measure = getattr(self, field_name)
            if measure is not None and measure.unit not in allowed:
                raise ValueError(f"{field_name}.unit must be one of {sorted(allowed)}")
        if self.set_entries is not None:
            # Rows are authoritative. Never flatten varied sets into a fictitious
            # sets × reps × weight combination for legacy history consumers.
            units = {entry.load.unit for entry in self.set_entries if entry.load is not None}
            if len(units) > 1:
                raise ValueError("Use one load unit for an exercise's sets.")
            rows = self.set_entries
            numbered = any(entry.set_number is not None for entry in rows)
            numbers = [entry.set_number for entry in rows] if numbered else list(range(1, len(rows) + 1))
            if numbered and (None in numbers or numbers != sorted(set(numbers))):
                raise ValueError("Set numbers must be unique and increasing on every row.")
            completed = (self.sets if self.sets is not None else numbers[-1]) if numbered else len(rows)
            if completed < numbers[-1]:
                raise ValueError("Reported set numbers cannot exceed the completed set count.")
            measured = [(number, entry) for number, entry in zip(numbers, rows)
                        if entry.reps is not None or entry.load is not None or entry.rpe is not None]
            # Empty legacy/input rows are unreported work, never measurement records.
            self.sets = completed
            self.set_entries = [entry.model_copy(update={"set_number": number})
                                if numbered or len(measured) != completed else entry for number, entry in measured] or None
            full = len(measured) == completed
            first = measured[0][1] if measured else None
            self.reps = first.reps if full and all(entry.reps == first.reps for _, entry in measured) else None
            self.load = first.load if full and all(entry.load == first.load for _, entry in measured) else None
            if not full or any(entry.rpe is not None for _, entry in measured):
                self.effort = ExerciseLogEffort(value=first.rpe) if full and first.rpe is not None and all(entry.rpe == first.rpe for _, entry in measured) else None
        return self


class ExerciseLogEntry(BaseModel):
    """What was actually done for one prescribed block, named by ``block_id``."""

    model_config = ConfigDict(extra="forbid")

    block_id: str = Field(min_length=1, max_length=200)
    status: ExerciseLogStatus
    actual: ExerciseLogActual = Field(default_factory=ExerciseLogActual)
    reason: ExerciseLogReason | None = None
    notes: str = Field(default="", max_length=EXERCISE_LOG_NOTE_MAX_CHARS)

    @field_validator("notes", mode="before")
    @classmethod
    def clean_notes(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("block_id", mode="before")
    @classmethod
    def clean_block_id(cls, value: Any) -> str:
        return str(value or "").strip()

    @model_validator(mode="after")
    def validate_status_fields(self) -> "ExerciseLogEntry":
        has_actual = bool(self.actual.model_dump(exclude_none=True))
        if self.status == "modified" and not has_actual:
            raise ValueError("a modified exercise must say what was actually done")
        if self.status == "skipped" and has_actual:
            raise ValueError("a skipped exercise has no actual work")
        if self.status == "as_prescribed" and self.reason is not None:
            raise ValueError("a reason explains a change or a skip")
        return self


class ExerciseLogRequest(ExerciseLogEntry):
    """Log what was actually done for one prescribed block today.

    The block is named by its server-owned ``block_id``; the server resolves the
    prescription and the training day itself and never takes either from the
    client. The plan is not changed by a log.
    """

    plan_id: str = Field(min_length=1, max_length=64)

    @field_validator("plan_id", mode="before")
    @classmethod
    def clean_plan_id(cls, value: Any) -> str:
        return str(value or "").strip()


class ExerciseLogBatchRequest(BaseModel):
    """Several blocks of today's session logged at once, e.g. when the session
    timer ends. All entries are accepted or none are."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1, max_length=64)
    entries: list[ExerciseLogEntry] = Field(min_length=1, max_length=60)
    # Leave any block already logged today as it is: what the timer counted
    # never overwrites what the athlete entered by hand.
    keep_existing: bool = False

    @field_validator("plan_id", mode="before")
    @classmethod
    def clean_plan_id(cls, value: Any) -> str:
        return str(value or "").strip()

    @model_validator(mode="after")
    def validate_unique_blocks(self) -> "ExerciseLogBatchRequest":
        block_ids = [entry.block_id for entry in self.entries]
        if len(set(block_ids)) != len(block_ids):
            raise ValueError("each block can be logged once per request")
        return self


class ExerciseLogRecord(BaseModel):
    id: str
    athlete_id: str
    plan_id: str | None = None
    session_id: str | None = None
    block_id: str
    exercise_key: str | None = None
    training_day: str
    status: ExerciseLogStatus
    reason: ExerciseLogReason | None = None
    # The block as it was prescribed when logged, and what was done instead
    # (only the fields the athlete reported are set).
    prescribed: dict[str, Any] = Field(default_factory=dict)
    actual: ExerciseLogActual = Field(default_factory=ExerciseLogActual)
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""


class ExerciseLogResponse(BaseModel):
    log: ExerciseLogRecord


class ExerciseLogBatchResponse(BaseModel):
    logs: list[ExerciseLogRecord]


class ExerciseRecentLoad(BaseModel):
    """The weight last logged for an exercise, carried into the next log."""

    exercise_key: str | None = None
    display_name: str = ""
    load: ExerciseLogMeasure
    training_day: str


class ExerciseHistoryResponse(BaseModel):
    logs: list[ExerciseLogRecord]
    next_offset: int | None = None


class ExerciseLogListResponse(BaseModel):
    """The athlete's exercise logs for one plan on the current training day."""

    training_day: str
    logs: list[ExerciseLogRecord]
    # The latest weight per exercise from earlier days (any plan).
    recent_loads: list[ExerciseRecentLoad] = Field(default_factory=list)
    recent_performances: list[ExerciseLogRecord] = Field(default_factory=list)


class RehabResponsePromptResponse(BaseModel):
    """One injury's post-rehab question, as the athlete is shown it.

    The server sends the questions and the allowed answers together, so the
    client never invents either. ``drill_ids`` names the rehab work that raised
    this prompt; it is informational, and the server re-resolves attribution on
    submit rather than trusting it back.
    """

    injury_id: str
    # Immutable context captured when the server raised this prompt. The client
    # returns it verbatim so a delayed answer cannot cross injury episodes.
    injury_episode_id: UUID
    # "LEFT ANKLE" — the injury addressed by name, so the athlete knows which
    # one they are answering about when more than one is open.
    injury_label: str
    body_region: str
    side: str
    drill_ids: list[str] = Field(default_factory=list)
    during_question: str
    during_options: list[RehabDuringResponse] = Field(default_factory=list)
    limit_question: str
    limit_options: list[RehabLimitResponse] = Field(default_factory=list)
    # Calm-stage guidance was followed rather than exercises performed.
    guidance_only: bool = False


class SessionCompletionResponse(BaseModel):
    completion: SessionCompletionRecordResponse
    completion_status: CompletionStatus
    landing_session_state: LandingSessionState
    # Non-empty only when this session actually contained rehab attributable to
    # a known injury. A normal training session returns none and the athlete is
    # asked nothing extra. General session feedback is unaffected either way.
    rehab_response_prompts: list[RehabResponsePromptResponse] = Field(default_factory=list)


class PendingRehabResponseSetResponse(BaseModel):
    """One exact completion whose injury response is not fully persisted yet."""

    completion_id: UUID
    plan_id: UUID
    session_id: str
    training_day: str
    rehab_response_prompts: list[RehabResponsePromptResponse] = Field(default_factory=list)


class PendingRehabResponsesResponse(BaseModel):
    """Bounded plan-wide pending response contexts plus read completeness."""

    response_sets: list[PendingRehabResponseSetResponse] = Field(default_factory=list)
    history_truncated: bool = False


class RehabResponseAnswer(BaseModel):
    """What the athlete said about one injury after one rehab session."""

    model_config = ConfigDict(extra="forbid")

    injury_id: str = Field(min_length=1)
    injury_episode_id: UUID
    during_response: RehabDuringResponse
    limit_response: RehabLimitResponse


class RehabResponseRequest(BaseModel):
    """The athlete's answers to the prompts one completion raised.

    Carries only the server-issued injury episode context alongside each answer.
    Drill, side and demand remain server-resolved. The episode is not client
    authority: a mismatch with the current server record rejects the entire
    request before any exposure is written.
    """

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    training_day: str | None = None
    answers: list[RehabResponseAnswer] = Field(min_length=1, max_length=20)

    @field_validator("plan_id")
    @classmethod
    def validate_plan_id(cls, value: str) -> str:
        # Rejected here so a malformed id never reaches the database as a uuid
        # syntax error, matching the session-completion path.
        try:
            UUID(value.strip())
        except ValueError as exc:
            raise ValueError("plan_id must be a valid UUID") from exc
        return value.strip()

    @field_validator("training_day")
    @classmethod
    def validate_training_day(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            date.fromisoformat(cleaned)
        except ValueError as exc:
            raise ValueError("training_day must be a YYYY-MM-DD date") from exc
        return cleaned

    @model_validator(mode="after")
    def validate_one_answer_per_injury(self) -> "RehabResponseRequest":
        seen = [answer.injury_id for answer in self.answers]
        if len(set(seen)) != len(seen):
            raise ValueError("each injury may be answered once")
        return self


class RehabResponseResult(BaseModel):
    """Which exposures were appended, named by the identity the server resolved."""

    recorded_exposure_ids: list[str] = Field(default_factory=list)
    recorded_injury_ids: list[str] = Field(default_factory=list)


class PlanCompletionsResponse(BaseModel):
    """Live completion rows for one plan. ``current_training_day`` is the
    server-authoritative athlete-local day the viewer uses to derive missed
    sessions and gate the retro-log window."""

    completions: list[SessionCompletionRecordResponse]
    current_training_day: str


# ---------------------------------------------------------------------------
# Durable account XP
# ---------------------------------------------------------------------------

class XpAwardRecord(BaseModel):
    id: str
    # `XpAction` and the reward amounts live in api/xp.py, which mirrors the
    # award_athlete_xp database function. Retired actions stay in the ledger at
    # 0 XP (daily_login), so the amount floor is 0 rather than 1.
    action: XpAction
    amount: int = Field(ge=0)
    awarded_at: datetime
    calendar_date: date | None = None


class XpAccountState(BaseModel):
    total_xp: int = Field(ge=0)
    last_daily_login_date: date | None = None
    recent_awards: list[XpAwardRecord] = Field(default_factory=list, max_length=20)


class XpAwardResponse(BaseModel):
    state: XpAccountState
    previous_total_xp: int = Field(ge=0)
    awarded: bool
    award: XpAwardRecord | None = None

    @model_validator(mode="after")
    def validate_award_totals(self) -> "XpAwardResponse":
        if self.awarded:
            if self.award is None or self.state.total_xp != self.previous_total_xp + self.award.amount:
                raise ValueError("awarded XP response must include the matching ledger increment")
        elif self.award is not None or self.state.total_xp != self.previous_total_xp:
            raise ValueError("idempotent XP response must preserve the previous total")
        return self


class LandingResponse(BaseModel):
    target: str
    cta: str
    row: int
    reason: str


class TodayInjuryDeclaration(BaseModel):
    """One injury as reported on the Today daily injury check-in.

    ``flag_id`` targets an existing open flag to update; without it the report is
    a new injury and needs a ``body_area`` or ``description``.
    """

    flag_id: str | None = None
    body_area: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=DAILY_NOTE_MAX_CHARS)
    severity: InjuryFlagSeverity | None = None
    status: Literal["ongoing", "improving", "worse", "resolved"] = "ongoing"
    # Optional surface (skin) follow-up, sent only when a known skin injury is
    # marked worse. Existing clients that omit them stay valid.
    skin_integrity: SkinIntegrity | None = None
    bleeding_status: BleedingStatus | None = None
    drainage: Drainage | None = None
    infection_signs: list[str] | None = Field(default=None, max_length=MAX_INFECTION_SIGNS)
    coverable: Coverable | None = None
    friction_or_contact_problem: FrictionOrContactProblem | None = None

    @field_validator("infection_signs", mode="before")
    @classmethod
    def clean_infection_signs(cls, value: Any) -> list[str] | None:
        if value is None:
            # Absent is not malformed. Every surface answer is optional by
            # design — an existing client that posts {flag_id, status}, or one
            # that sends an explicit null for a question it did not ask, stays
            # valid, and the classifier reads the missing answer as "unknown".
            return None
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            # Silently reading a malformed container as "omitted" fails OPEN: the
            # safety classifier would see no infection signs on a wound the
            # client tried to report as infected, and route it as stable skin.
            # A rejected request is the only safe reading of an unparseable
            # safety answer.
            raise ValueError("infection_signs must be a list of strings")
        return [str(item).strip()[:60] for item in value if str(item).strip()]

    @field_validator("flag_id", mode="before")
    @classmethod
    def clean_flag_id(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("body_area", "description", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return str(value or "").strip()


class TodayInjuryCheckinRequest(BaseModel):
    injuries: list[TodayInjuryDeclaration] = Field(default_factory=list, max_length=20)


class TodayInjuryCheckinResponse(BaseModel):
    open_injuries: list[InjuryFlagRecord] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Secure beta feedback
# ---------------------------------------------------------------------------

FeedbackSurface = Literal["plan", "daily_recommendation", "session", "global"]
FeedbackCategory = Literal[
    "plan_usefulness",
    "recommendation_fit",
    "recommendation_safety",
    "session_review",
    "bug_report",
    "feature_request",
    "safety_issue",
    "general_feedback",
]
FeedbackResponseValue = Literal["yes", "no", "unsafe"]
FeedbackPriority = Literal["normal", "safety"]

SessionFeedbackDifficulty = Literal["too_easy", "appropriate", "too_hard"]
SessionFeedbackInstructions = Literal["clear", "unclear"]
SessionFeedbackPlanAccuracy = Literal["felt_right", "something_wrong"]
# Keep in step with beta_feedback_session_id_check in the schema.
SESSION_FEEDBACK_SESSION_ID_MAX_CHARS = 120


class ContextualFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response: FeedbackResponseValue
    reason: str | None = Field(default=None, max_length=64)
    comment: str = Field(default="", max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def clean_feedback_reason(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("comment", mode="before")
    @classmethod
    def clean_feedback_comment(cls, value: Any) -> str:
        return str(value or "").strip()


class GlobalFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Literal["bug_report", "feature_request", "safety_issue", "general_feedback"]
    description: str = Field(default="", max_length=500)
    contact_allowed: bool = False

    @field_validator("description", mode="before")
    @classmethod
    def clean_feedback_description(cls, value: Any) -> str:
        return str(value or "").strip()


class SessionFeedbackRequest(BaseModel):
    """The quick review collected right after a completed session.

    Every question is optional on its own — the prompt must stay short enough
    that testers keep completing sessions — but a submission has to carry at
    least one answer, a comment, or a screenshot to be worth persisting.
    """

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(max_length=64)
    # Bounded so the derived context key — "session:{plan_id}:{session_id}:
    # {training_day}", 56 characters of frame around a UUID plan id and an ISO
    # date — stays inside beta_feedback.context_key's 180-character check.
    session_id: str = Field(max_length=SESSION_FEEDBACK_SESSION_ID_MAX_CHARS)
    training_day: str = Field(default="", max_length=10)
    difficulty: SessionFeedbackDifficulty | None = None
    instructions: SessionFeedbackInstructions | None = None
    plan_accuracy: SessionFeedbackPlanAccuracy | None = None
    comment: str = Field(default="", max_length=500)

    @field_validator("plan_id", "session_id", "training_day", "comment", mode="before")
    @classmethod
    def clean_session_feedback_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("difficulty", "instructions", "plan_accuracy", mode="before")
    @classmethod
    def clean_session_feedback_choice(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    def structured_response(self) -> dict[str, str]:
        """The answered questions only — an unanswered question stays absent."""

        answers = {
            "difficulty": self.difficulty,
            "instructions": self.instructions,
            "plan_accuracy": self.plan_accuracy,
        }
        return {key: value for key, value in answers.items() if value}


class FeedbackRecord(BaseModel):
    id: str
    surface: FeedbackSurface
    category: FeedbackCategory
    response: FeedbackResponseValue | None = None
    reason: str | None = None
    comment: str = ""
    structured_response: dict[str, Any] = Field(default_factory=dict)
    priority: FeedbackPriority
    has_screenshot: bool = False
    created_at: str = ""
    updated_at: str = ""


class AdminFeedbackRecord(FeedbackRecord):
    submitted_by_profile_id: str
    submitter_email: str = ""
    submitter_name: str = ""
    contact_allowed: bool = False
    plan_id: str | None = None
    today_checkin_id: str | None = None
    session_id: str | None = None
    camp_phase: str | None = None
    app_version: str = ""
    page_path: str = ""
    device_context: str = ""
    language: str = ""
    readiness_context: list[str] = Field(default_factory=list)
    injury_context: list[str] = Field(default_factory=list)
    readiness_snapshot: dict[str, Any] = Field(default_factory=dict)
    injury_snapshot: dict[str, Any] = Field(default_factory=dict)
    technical_context: dict[str, Any] = Field(default_factory=dict)
    screenshot_expires_at: str | None = None


class AdminFeedbackScreenshotAccess(BaseModel):
    url: str
    expires_in: int
