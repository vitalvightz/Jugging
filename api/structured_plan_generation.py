"""Stage 2 → ``StructuredTrainingPlan`` bridge.

This module connects the existing Stage 2 plan generation to the new structured
plan schema (see ``api/structured_plan_models.py``). It is deliberately additive:
structured generation runs *beside* the legacy raw-text flow and never replaces
it. If structured generation is skipped, fails, or produces invalid JSON, the
raw ``plan_text`` remains the fallback and plan generation is never blocked.

Two concerns live here:

* :func:`build_structured_plan_outcome` — the pure, network-free validation flow
  (validate → one repair retry → raw-markdown fallback) that turns a candidate
  structured payload into a persistable outcome plus an admin/debug status. It
  reuses :func:`safe_parse_structured_plan` and :func:`repair_structured_plan_once`.
* :func:`build_structured_plan_prompt` — the instruction text that tells the
  model what a schema-compatible ``StructuredTrainingPlan`` JSON object must look
  like. The actual model call lives in ``api/stage2_automation.py``.
"""
from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, Callable, Literal, get_args

from fightcamp.injury_awareness import merge_injury_awareness
from fightcamp.session_sequencing import sequence_structured_plan
from fightcamp.fight_date_utils import parse_fight_date
from fightcamp.structured_session_identity import match_sessions_to_roles
from fightcamp.weekly_schedule_view import normalize_weekday as _normalize_weekday

from .state_machine import is_athlete_displayable_plan_status
from .structured_plan_calendar_spine import (
    ROLES_OWNED_ELSEWHERE,
    _effective_dday as spine_effective_dday,
    _parse_dday as spine_parse_dday,
    _resolve_fight_date as spine_resolve_fight_date,
    _role_dday as spine_role_dday,
    reconcile_calendar_spine,
)
from .structured_plan_faithfulness import (
    _source_day_section_lines,
    PRESCRIPTION,
    _day_header_dday,
    check_structured_faithfulness,
    prune_unfaithful_content,
    strip_locked_sessions_for_conversion,
)
from .structured_plan_locked_merge import merge_planner_owned_structured_content
from .minor_safety import MINOR_WEIGHT_CUT_NOTE
from .services.exercise_media import strip_dose_suffix
from .exercise_identity import (
    _name_forms as exercise_name_forms,
    block_exercise_key_for_name,
    normalize_exercise_key,
    reconcile_canonical_names,
    reconcile_exercise_keys,
)
from .structured_plan_safety import (
    audit_structured_plan,
    is_minor_support,
    split_findings,
)
from .structured_plan_models import (
    SCHEMA_VERSION,
    BlockType,
    CompletionStatus,
    DailyCheckIn,
    Day,
    DayType,
    EventType,
    LoadFocusValue,
    PhaseLabel,
    PlanNoteCategory,
    PlanStatus,
    PlanType,
    EffortMethod,
    LoadMethod,
    ReadinessStatus,
    RedFlagWhen,
    RiskLevel,
    SessionType,
    Severity,
    TodayCard,
    UnitsSystem,
    WeekType,
    repair_structured_plan_once,
    safe_parse_structured_plan,
)

# Admin/debug status describing what happened to the structured-plan attempt.
# ``blocked_by_safety_audit`` means the card was schema-valid but carried
# blocking safety findings (LEAKAGE/CONFLICT/AUDIT_ERROR) — it is discarded and
# the plan is held for review, never published on the card's authority.
StructuredPlanStatus = Literal[
    "not_attempted",
    "valid",
    "repair_attempted_valid",
    "invalid_fallback_used",
    "blocked_by_safety_audit",
]

# Biometric / wearable-style keys the structured plan must never carry. Readiness
# is self-report only (no HRV/CNS/WHOOP-style scores — see the schema module). If
# a model hallucinates one of these, it is stripped before validation so it can
# never be persisted.
BANNED_BIOMETRIC_KEYS: frozenset[str] = frozenset(
    {
        "hrv",
        "hrv_score",
        "hrv_ms",
        "cns",
        "cns_recovery",
        "cns_recovery_percent",
        "cns_percent",
        "whoop_recovery",
        "whoop_recovery_score",
        "whoop_score",
        "recovery_score",
        "readiness_score",
        "strain",
        "strain_score",
        "resting_heart_rate",
    }
)

logger = logging.getLogger(__name__)


def _with_athlete_goal_notes(plan: dict, brief: Any) -> dict:
    """Replace goal-gap/deferral prose with the authoritative plain explanation.

    This only owns goal notes; injury context and safety rules are untouched.
    Full diagnostic messages stay in the planning brief and admin reports.
    """
    from fightcamp.goal_preservation import athlete_goal_explanation

    if not isinstance(brief, dict):
        return plan
    entries = (brief.get("open_plan_spec") or {}).get("goal_coverage") or brief.get("goal_preservation") or []
    entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("goal")]
    notes = []
    for note in plan.get("plan_notes") or []:
        if not isinstance(note, dict):
            notes.append(note)
            continue
        label = str(note.get("label") or "").lower()
        text = str(note.get("text") or "").lower()
        goal_note = any(str(e["goal"]).replace("_", " ") in label for e in entries)
        internal = any(token in text for token in ("deterministic", "qualified exercises", "planner repair", "contract_errors"))
        if goal_note and (internal or any(token in label for token in ("deferred", "gap", "missing"))):
            continue
        notes.append(note)
    for entry in entries:
        explanation = athlete_goal_explanation(entry)
        if explanation and not any(isinstance(n, dict) and n.get("text") == explanation for n in notes):
            notes.append({"category": "training", "label": str(entry["goal"]).replace("_", " ").title(),
                          "text": explanation})
    return {**plan, "plan_notes": notes}


def _merge_locked_content(
    plan_dict: dict[str, Any], planning_brief: Any, raw_markdown: str | None = None
) -> dict[str, Any]:
    """Apply Stage 1 locked truth and emit compact, profile-free diagnostics."""
    # The converter never sees server-owned sessions. A day containing only one
    # of them may therefore be absent from its JSON; restore the planner's day
    # spine before projecting the locked card onto its authoritative D-day.
    plan_dict = reconcile_calendar_spine(plan_dict, planning_brief, plan_text=raw_markdown)
    result = merge_planner_owned_structured_content(plan_dict, planning_brief)
    locked_roles = len(result.applied) + len(result.unresolved)
    if locked_roles:
        logger.info(
            "[structured_locked_merge] locked_roles=%d applied=%d unresolved=%d",
            locked_roles,
            len(result.applied),
            len(result.unresolved),
        )
        for application in result.applied:
            logger.info(
                "[structured_locked_merge_detail] day=%s block=%s status=applied",
                application.countdown_label,
                application.block_name,
            )
        for issue in result.unresolved:
            logger.warning(
                "[structured_locked_merge_detail] day=%s block=%s status=unresolved reason=%s",
                issue.countdown_label or "",
                issue.block_name,
                issue.reason,
            )
    # Progress/Easier are verbatim source projections; copy them in code so a
    # paraphrase cannot fail the whole conversion. Coaching recovery still
    # validates them against the programme afterwards.
    from .structured_plan_faithfulness import project_source_adjustments
    return project_source_adjustments(result.plan, raw_markdown or "")


@dataclass
class StructuredPlanOutcome:
    """Result of attempting to produce a validated structured plan.

    ``structured_plan`` is a JSON-ready dict (only on a valid/repaired outcome);
    it is ``None`` whenever the raw ``plan_text`` fallback must be used so an
    invalid payload is never persisted. ``errors``/``status`` feed admin debug.
    """

    status: StructuredPlanStatus
    structured_plan: dict[str, Any] | None = None
    schema_version: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_debug(self) -> dict[str, Any]:
        """Compact admin/debug view persisted alongside the validator report."""
        return {
            "status": self.status,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "schema_version": self.schema_version,
        }


def _plan_has_structured_plan(plan: dict[str, Any]) -> bool:
    value = plan.get("structured_plan")
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return bool(value)
    return True


def _approved_plan_text(plan: dict[str, Any]) -> str:
    """The athlete-facing/final plan text that is the source of truth."""
    return str(plan.get("final_plan_text") or plan.get("plan_text") or "").strip()


def should_attempt_structured_plan(plan: Any, env_enabled: bool) -> bool:
    """Whether to build a ``structured_plan`` for this plan-like object.

    Centralizes the trigger so it is driven by the canonical state machine
    (:func:`api.state_machine.is_athlete_displayable_plan_status`), never by
    hardcoded Stage 2 statuses such as ``stage2_pass``/``admin_review_approved``.
    Returns ``True`` only when ALL of the following hold:

    1. ``env_enabled`` is set (``UNLXCK_STAGE2_STRUCTURED_PLAN=1``).
    2. No ``structured_plan`` is stored yet (idempotent — never overwrites).
    3. An approved/final ``plan_text`` exists to convert (source of truth).
    4. The plan status is athlete-displayable/publishable (``ready`` /
       ``publishable_with_flags``). Blocked, held, medical-gated, review-required,
       and archived states are excluded so nothing is published merely to derive
       structured output.

    Accepts either a Stage 2 result dict or a persisted plan row.
    """

    if not env_enabled:
        return False
    if not isinstance(plan, dict):
        return False
    if _plan_has_structured_plan(plan):
        return False
    if not _approved_plan_text(plan):
        return False
    return is_athlete_displayable_plan_status(plan.get("status"))


# Structured-plan attempt statuses that count as a successfully validated card.
# ``invalid_fallback_used`` / ``not_attempted`` are excluded: they mean the raw
# plan_text fallback is in play, so there is no schema-valid card to trust.
_CLEAN_STRUCTURED_PLAN_STATUSES: frozenset[StructuredPlanStatus] = frozenset(
    {"valid", "repair_attempted_valid"}
)


def has_clean_structured_card(plan: Any) -> bool:
    """True when ``plan`` carries a schema-valid structured card.

    A clean card is a non-empty ``structured_plan`` dict whose recorded attempt
    status validated (``valid`` or ``repair_attempted_valid``). It is used as a
    trust signal: a plan that produced a schema-valid card is treated as
    publishable rather than held for non-safety findings. Accepts either a
    Stage 2 result dict or a persisted plan row (the attempt status lives under
    ``stage2_validator_report.structured_plan.status`` in both).
    """

    if not isinstance(plan, dict):
        return False
    card = plan.get("structured_plan")
    if not isinstance(card, dict) or not card:
        return False
    report = plan.get("stage2_validator_report")
    debug = report.get("structured_plan") if isinstance(report, dict) else None
    status = debug.get("status") if isinstance(debug, dict) else None
    return status in _CLEAN_STRUCTURED_PLAN_STATUSES


def strip_biometric_fields(data: Any) -> tuple[Any, list[str]]:
    """Recursively drop banned biometric keys.

    Returns ``(cleaned, removed_paths)``. The input is not mutated.
    """

    removed: list[str] = []

    def _walk(node: Any, path: str) -> Any:
        if isinstance(node, dict):
            cleaned: dict[Any, Any] = {}
            for key, value in node.items():
                child_path = f"{path}.{key}" if path else str(key)
                if isinstance(key, str) and key.strip().lower() in BANNED_BIOMETRIC_KEYS:
                    removed.append(child_path)
                    continue
                cleaned[key] = _walk(value, child_path)
            return cleaned
        if isinstance(node, list):
            return [_walk(item, f"{path}[{index}]") for index, item in enumerate(node)]
        return node

    return _walk(data, ""), removed


# ---------------------------------------------------------------------------
# Conservative normalization
#
# The structured conversion model occasionally returns the right plan in a
# slightly-wrong shape (enum aliases, loads/rests as strings, countdown labels as
# strings, a non-list daily_check_ins, a missing required meta field). These are
# *formatting* mistakes, not content mistakes. The normalizer below fixes only
# those, using neutral structural defaults. It NEVER invents training content —
# no exercises, sessions, blocks, loads, dates, biometrics, or weight-cut
# instructions — and it never touches raw_markdown_fallback content. If a value
# cannot be coerced safely it is dropped (e.g. an unparseable load → null), and
# the strict Pydantic schema still decides validity afterwards.
# ---------------------------------------------------------------------------

# Allowed enum value sets are derived from the schema's Literal aliases so they
# can never drift from api/structured_plan_models.py.
_PLAN_TYPE_VALUES = frozenset(get_args(PlanType))
_PLAN_STATUS_VALUES = frozenset(get_args(PlanStatus))
_UNITS_VALUES = frozenset(get_args(UnitsSystem))
_EVENT_TYPE_VALUES = frozenset(get_args(EventType))
_SEVERITY_VALUES = frozenset(get_args(Severity))
_RED_FLAG_WHEN_VALUES = frozenset(get_args(RedFlagWhen))
_PHASE_VALUES = frozenset(get_args(PhaseLabel))
_LOAD_FOCUS_VALUES = frozenset(get_args(LoadFocusValue))
_LOAD_METHOD_VALUES = frozenset(get_args(LoadMethod))
_WEEK_TYPE_VALUES = frozenset(get_args(WeekType))
_DAY_TYPE_VALUES = frozenset(get_args(DayType))
_READINESS_VALUES = frozenset(get_args(ReadinessStatus))
_SESSION_TYPE_VALUES = frozenset(get_args(SessionType))
_COMPLETION_VALUES = frozenset(get_args(CompletionStatus))
_BLOCK_TYPE_VALUES = frozenset(get_args(BlockType))
_RISK_LEVEL_VALUES = frozenset(get_args(RiskLevel))
_PLAN_NOTE_CATEGORY_VALUES = frozenset(get_args(PlanNoteCategory))
_EFFORT_METHOD_VALUES = frozenset(get_args(EffortMethod))

_OPEN_PLAN_TYPE = "open_ongoing_system"
_OPEN_PLAN_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Conservative enum aliases for the most common loose values.
_SESSION_TYPE_ALIASES = {
    "strength": "strength_power",
    "power": "strength_power",
    "strength_and_conditioning": "strength_power",
    "s&c": "strength_power",
    "cardio": "conditioning",
    "technical": "skill",
    "spar": "sparring",
    "fight": "fight_or_match",
    "match": "fight_or_match",
    "rest": "recovery",
    "warmup": "primer",
    "warm-up": "primer",
}


def _raw_session_type_is_rehab(value: Any) -> bool:
    """Recognise legacy rehab labels before unknown enums fall back to mixed.

    Earlier raw-plan conversion can emit a session label such as
    ``Rehab-friendly low-load support`` in ``session_type`` rather than the
    structured ``rehab`` enum. This remains deliberately narrow: a canonical
    session type always wins, and only rehab/prehab wording gets this fallback.
    """
    text = _coerce_str(value).strip().lower()
    return bool(re.search(r"\b(?:prehab|rehab)\b", text))


_EFFORT_METHOD_ALIASES = {
    "rpe": "RPE",
    "rir": "RIR",
    "hr_zone": "heart_rate_zone",
    "heart_rate": "heart_rate_zone",
}
# Loose ``load.method`` values the conversion model emits instead of the enum.
# Taper/rehab work is the common source: those blocks are prescribed as "light
# DBs (2-4 kg)" or "band" rather than a clean %1RM, so the model reaches for a
# descriptive method far more often than it does on a strength anchor.
_LOAD_METHOD_ALIASES = {
    "percent": "percentage",
    "pct": "percentage",
    "%": "percentage",
    "1rm": "percentage",
    "percentage_1rm": "percentage",
    "percent_1rm": "percentage",
    "kg": "absolute",
    "kgs": "absolute",
    "lb": "absolute",
    "lbs": "absolute",
    "pound": "absolute",
    "pounds": "absolute",
    "weight": "absolute",
    "external": "absolute",
    "external_load": "absolute",
    "dumbbell": "absolute",
    "dumbbells": "absolute",
    "bw": "bodyweight",
    "body_weight": "bodyweight",
    "body weight": "bodyweight",
    "bodyweight_only": "bodyweight",
    "bands": "band",
    "banded": "band",
    "resistance_band": "band",
    "rpe_scale": "rpe",
    "rir_scale": "rir",
    "qualitative": "other",
    "text": "other",
}

# Unit tokens that identify a load method when the method itself is unreadable.
_LOAD_PERCENT_HINT_RE = re.compile(r"%|percent|1\s*rm", re.I)
_LOAD_BODYWEIGHT_HINT_RE = re.compile(r"bodyweight|body weight|\bbw\b", re.I)
_LOAD_BAND_HINT_RE = re.compile(r"\bbands?\b|banded", re.I)
_LOAD_ABSOLUTE_HINT_RE = re.compile(r"\b(kgs?|lbs?|kilos?|kilograms?|pounds?|dbs?|dumbbells?)\b", re.I)

# A number (or range) that is DIRECTLY attached to a load unit — "2-4 kg", "85%".
# Anchoring on the unit is what stops a rep/set count being misread as a load:
# "2 sets x 8 reps per letter, light DBs (2-4 kg)" must yield 4 kg, never 2.
_LOAD_NUMBER_WITH_UNIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:[-–—]\s*(\d+(?:\.\d+)?))?\s*(?:%|kgs?|lbs?|kilos?|kilograms?|pounds?)",
    re.I,
)

# Default unit per method, used only when the payload carries no usable unit.
# ``absolute`` follows the convention already set by the bare-number branch below.
_LOAD_DEFAULT_UNITS = {
    "percentage": "percent",
    "bodyweight": "bodyweight",
    "band": "band",
    "absolute": "kg",
    "rpe": "rpe",
    "rir": "rir",
    "velocity": "m/s",
    "relative": "relative",
    "other": "other",
}
_BLOCK_TYPE_ALIASES = {
    "warmup": "preparation",
    "warm-up": "preparation",
    "warm_up": "preparation",
    "prep": "preparation",
    "mobility": "mobility_activation",
    "activation": "mobility_activation",
    "plyo": "plyometric_power",
    "plyometrics": "plyometric_power",
    "power": "plyometric_power",
    "cooldown": "cooldown_recovery",
    "cool-down": "cooldown_recovery",
    "cool_down": "cooldown_recovery",
    "recovery": "cooldown_recovery",
}

# --- day_type (intensity badge) classification -----------------------------
#
# day_type is the athlete-facing intensity badge (high / moderate / low / ...).
# The conversion model guesses it from prose and lands on the wrong bucket often;
# worse, the old normalizer silently defaulted *every* unrecognized value (e.g.
# "primer", "light", "easy") to "moderate", which reads as risky one day before a
# fight even when the session is a light primer. Instead we derive the badge
# deterministically from the day's actual content — session types and per-block
# effort (RPE), load (%1RM), and intensity tags — and a day is rated as hard as
# its hardest *real* work block. Categorical days (competition, rest, recovery,
# travel, reintegration) are detected first and never collapsed into an intensity
# bucket. The model's value is only consulted as a last-resort fallback, and even
# then an unreadable day falls to "low" rather than silently "moderate".

# Block intensity tags that read as hard / easy regardless of the numbers.
# "explosive" is deliberately excluded: taper/primer work is often "explosive"
# but low-volume, high-intent and light, so it must not auto-flag a hard day —
# the block's RPE/load decides.
_HIGH_INTENSITY_WORDS = frozenset(
    {"max", "maximal", "near_max", "very_high", "high"}
)
_LOW_INTENSITY_WORDS = frozenset(
    {"none", "very_low", "low", "easy", "light", "primer", "recovery"}
)
# Max-output block types: when a block carries no readable effort/load number,
# their presence still implies a high-CNS day (true power/speed work).
_HIGH_OUTPUT_BLOCK_TYPES = frozenset({"plyometric_power", "speed", "strength_speed"})
# Session types that are intrinsically light when they carry no measurable block.
_LOW_SESSION_TYPES = frozenset({"primer", "recovery", "rehab"})
_INTENSITY_RANK = {"low": 1, "moderate": 2, "high": 3}

_DURATION_UNIT_ALIASES = {
    "s": "seconds",
    "sec": "seconds",
    "secs": "seconds",
    "second": "seconds",
    "seconds": "seconds",
    "m": "minutes",
    "min": "minutes",
    "mins": "minutes",
    "minute": "minutes",
    "minutes": "minutes",
    "h": "hours",
    "hr": "hours",
    "hrs": "hours",
    "hour": "hours",
    "hours": "hours",
}

_COUNTDOWN_RE = re.compile(r"^[Dd]\s*([+-]?\d+)$")
_MEASURED_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]+)\s*$")
# A bare numeric range with an optional unit ("90-120 sec", "5–6"). group(2) is
# the upper bound; group(3) the unit token (may be empty → field default unit).
_MEASURED_RANGE_RE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*[-–—]\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]*)\s*$"
)
# First number or numeric range in a string. group(2) is the upper bound of a
# range ("7-8" → 8) and is ``None`` for a lone number ("7" → group(1) = 7).
_NUMBER_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)(?:\s*[-–—]\s*(\d+(?:\.\d+)?))?")
# Capture a percentage anywhere in the string plus an optional trailing reference
# such as "1RM" or "of 1RM" (bank prescriptions read like "3x5 @ 75-85% 1RM").
# group(2) is optional and may be ``None`` — callers must guard it.
_LOAD_PERCENT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*(?:of\s+)?([A-Za-z0-9]+)?")

# Distance units are mapped separately from duration units because a bare "m"
# means metres for a distance field but minutes for a duration field; the field's
# default_unit selects which alias table applies.
_DISTANCE_UNIT_ALIASES = {
    "m": "meters",
    "meter": "meters",
    "metre": "meters",
    "meters": "meters",
    "metres": "meters",
    "km": "kilometers",
    "mi": "miles",
    "mile": "miles",
    "miles": "miles",
    "yd": "yards",
    "yard": "yards",
    "yards": "yards",
}
_TIME_UNITS = frozenset({"seconds", "minutes", "hours"})


_LEADING_WEEKDAY_RE = re.compile(
    r"^\s*(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)"
    r"\b\s*[.:,\-\u2013\u2014|]\s*",
    re.IGNORECASE,
)


def _strip_leading_weekday(title: str) -> str:
    """Drop a weekday the converter wrote into a session title.

    The day card already names the day ("Monday. Strength anchor" repeats it).
    Only a weekday followed by a separator is removed, never the whole title.
    """
    stripped = _LEADING_WEEKDAY_RE.sub("", title or "", count=1)
    if not stripped.strip():
        return title
    return stripped[:1].upper() + stripped[1:]


def _coerce_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(_coerce_str(item) for item in value).strip()
    if isinstance(value, dict):
        return ""
    return str(value)


def _coerce_nonempty_str(value: Any, default: str) -> str:
    text = _coerce_str(value).strip()
    return text or default


def _coerce_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return default
    return default


def _coerce_optional_int(value: Any) -> int | None:
    """Coerce to ``int`` without inventing a value: return ``None`` when not safe.

    Unlike :func:`_coerce_int` this has no default — a missing, non-numeric, or
    fractional value yields ``None`` so callers can drop the surrounding entry
    rather than substituting a fabricated score.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        return int(number) if number.is_integer() else None
    return None


def _coerce_float(value: Any) -> float | None:
    """Best-effort float, reading the *upper* bound of the first range in a string.

    "RPE 7-8" → 8, "3-4" → 4, "85% 1RM" → 85. Ranges resolve to their upper bound
    because an athlete given a 7-8 block may actually work at 8, so the intensity
    badge should reflect the harder end. Returns ``None`` when no number is
    present so callers can tell "no signal" from a real zero.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = _NUMBER_RANGE_RE.search(value)
        if match:
            try:
                return float(match.group(2) or match.group(1))
            except ValueError:
                return None
    return None


def _enum(value: Any, allowed: frozenset[str], default: str, aliases: dict[str, str] | None = None) -> str:
    if isinstance(value, str):
        candidate = value.strip()
        if candidate in allowed:
            return candidate
        lowered = candidate.lower()
        if lowered in allowed:
            return lowered
        if aliases and lowered in aliases:
            return aliases[lowered]
    return default


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _normalize_countdown_label(item: Any) -> dict[str, Any] | None:
    if isinstance(item, dict):
        out = dict(item)
        label = _coerce_str(out.get("label"))
        out["label"] = label
        out["date"] = _coerce_str(out.get("date"))
        out["anchor"] = _coerce_nonempty_str(out.get("anchor"), "event_countdown")
        if not isinstance(out.get("days_to_event"), int) or isinstance(out.get("days_to_event"), bool):
            out["days_to_event"] = _days_from_label(label)
        return out
    if isinstance(item, str):
        label = item.strip()
        return {
            "date": "",
            "days_to_event": _days_from_label(label),
            "label": label,
            "anchor": "event_countdown",
        }
    return None


def _days_from_label(label: str) -> int:
    match = _COUNTDOWN_RE.match(label.strip())
    if not match:
        return 0
    # "D-28" → 28 days remaining; "D0" → 0; "D+1" → -1 (after the event).
    return -int(match.group(1))


def _load_method_from_context(*parts: Any) -> str:
    """Infer a load method from unit/display text, or ``""`` when unreadable."""
    text = " ".join(_coerce_str(part) for part in parts).strip()
    if not text:
        return ""
    if _LOAD_PERCENT_HINT_RE.search(text):
        return "percentage"
    if _LOAD_BODYWEIGHT_HINT_RE.search(text):
        return "bodyweight"
    if _LOAD_BAND_HINT_RE.search(text):
        return "band"
    if _LOAD_ABSOLUTE_HINT_RE.search(text):
        return "absolute"
    return ""


def _normalize_load_dict(value: dict[str, Any]) -> dict[str, Any] | None:
    """Coerce a model-emitted load object onto ``LoadPrescription``.

    ``LoadPrescription`` requires a valid ``method`` enum, a float ``value`` and
    a ``unit``. The conversion model routinely returns the right prescription in
    a slightly-wrong shape — a prose method ("light dumbbells"), a range value
    ("2-4"), the number present only in ``display`` ("light DBs (2-4 kg)"), or a
    missing unit — and an untouched pass-through failed the WHOLE card on that
    formatting slip. This is the same class of bug ``_normalize_measured`` below
    was hardened against; ``load`` never got the same treatment, so a single
    unreadable taper/rehab load sank every other day in the plan.

    Only formatting is repaired: the method is aliased or inferred from the
    unit/display text, a range reads its upper bound (matching the effort and
    measured conventions), and a method that stays unreadable becomes ``other``
    — which is precisely what that enum member is for. No load is invented: when
    no number can be read the optional field is dropped so the block keeps its
    prose prescription and the card still validates.
    """

    out = dict(value)
    display = _coerce_str(out.get("display")).strip()
    unit = _coerce_str(out.get("unit")).strip()

    raw_method = _coerce_str(out.get("method")).strip().lower()
    method = (
        raw_method
        if raw_method in _LOAD_METHOD_VALUES
        else _LOAD_METHOD_ALIASES.get(raw_method, "")
    )
    if not method:
        # An unreadable method is still recoverable from the numbers' context.
        method = _load_method_from_context(raw_method, unit, display)

    # The number may live on ``value`` or only inside the display text. A display
    # is read ONLY through a unit-anchored match ("2-4 kg", "85%"), so a tempo cue
    # ("controlled 2-0-2") or a rep scheme ("2 sets x 8 reps") can never be
    # mistaken for a load — a range still reads its working top end.
    number = _coerce_float(out.get("value"))
    if number is None:
        match = _LOAD_NUMBER_WITH_UNIT_RE.search(display)
        if match:
            number = float(match.group(2) or match.group(1))

    if number is None:
        # Bodyweight is the one method whose load is defined without a number,
        # matching the string branch below; anything else is dropped unread.
        if method != "bodyweight":
            return None
        number = 0.0

    if not method:
        method = "other"

    out["method"] = method
    out["value"] = number
    out["unit"] = unit or _LOAD_DEFAULT_UNITS.get(method, "other")
    if display:
        out["display"] = display
    else:
        out.pop("display", None)
    ref = _coerce_str(out.get("ref")).strip()
    if ref:
        out["ref"] = ref
    else:
        out.pop("ref", None)
    return out


def _normalize_load(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return _normalize_load_dict(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"method": "absolute", "value": float(value), "unit": "kg", "display": str(value)}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        percent = _LOAD_PERCENT_RE.search(text)
        if percent:
            load = {
                "method": "percentage",
                "value": float(percent.group(1)),
                "unit": "percent",
                "display": text,
            }
            # group(2) is optional, so guard it before use; carry a bank-style
            # reference (e.g. "1RM") through when present.
            ref = (percent.group(2) or "").strip()
            if ref:
                load["ref"] = ref
            return load
        if text.lower() in {"bodyweight", "bw", "body weight", "bodyweight only"}:
            return {"method": "bodyweight", "value": 0, "unit": "bodyweight", "display": "bodyweight"}
    return None


def _alias_measured_unit(raw_unit: Any, default_unit: str) -> str:
    """Alias a unit token within the field's dimension; default when unreadable."""
    text = _coerce_str(raw_unit).strip().lower()
    if not text:
        return default_unit
    if default_unit in _TIME_UNITS:
        return _DURATION_UNIT_ALIASES.get(text, text)
    if default_unit == "meters":
        return _DISTANCE_UNIT_ALIASES.get(text, text)
    return text


def _normalize_measured(value: Any, default_unit: str = "seconds") -> dict[str, Any] | None:
    """Coerce a measured value into ``{"value", "unit"}``.

    ``default_unit`` is used for bare numbers and plain numeric strings so a
    bank-style ``work_sec``/``rest_sec`` int maps to seconds, ``total_minutes``
    to minutes, and a distance to meters. A string carrying its own unit is
    parsed and the unit aliased within the field's dimension (time vs distance).
    Unparseable values return ``None``.
    """

    if value is None:
        return None
    if isinstance(value, dict):
        # The model frequently writes plan ranges into the dict form —
        # {"value": "90-120", "unit": "sec"} or {"unit": "seconds"} with no
        # value at all — and MeasuredValue requires a float ``value``, so an
        # untouched pass-through failed the WHOLE card on a formatting slip
        # (every rest/duration range in the plan text became "Field required").
        # Coerce the number (a range reads its upper bound, matching the effort
        # convention — more rest is safer, and the top of a written range is
        # still what the plan says), alias the unit, and drop the optional
        # field entirely when no number can be read rather than rejecting the card.
        raw_value = value.get("value")
        if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            number = float(raw_value)
        else:
            number = _coerce_float(raw_value)
            if number is None:
                # Some outputs carry the quantity only as display text.
                number = _coerce_float(value.get("display"))
        if number is None:
            return None
        return {
            "value": number,
            "unit": _alias_measured_unit(value.get("unit"), default_unit),
        }
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"value": float(value), "unit": default_unit}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:  # plain numeric string ("90", "2.5") → default unit for the field
            return {"value": float(text), "unit": default_unit}
        except ValueError:
            pass
        match = _MEASURED_RE.match(text)
        if match:
            return {
                "value": float(match.group(1)),
                "unit": _alias_measured_unit(match.group(2), default_unit),
            }
        # A pure range string ("90-120 sec", "5–6") reads its upper bound, the
        # same convention the dict branch and effort coercion use.
        range_match = _MEASURED_RANGE_RE.match(text)
        if range_match:
            return {
                "value": float(range_match.group(2)),
                "unit": _alias_measured_unit(range_match.group(3), default_unit),
            }
    return None


def _normalize_phase(value: Any) -> str:
    if isinstance(value, str):
        upper = value.strip().upper()
        if upper in _PHASE_VALUES:
            return upper
    return "GPP"


def _coerce_str_list(value: Any) -> list[str]:
    """Clean list of non-empty strings; a lone string is wrapped into a list."""
    if isinstance(value, list):
        return [text for text in (_coerce_str(entry).strip() for entry in value) if text]
    text = _coerce_str(value).strip()
    return [text] if text else []


_STOP_RULE_LABEL_RE = re.compile(r"\bstop(?:\s+rule)?\s*:\s*", re.IGNORECASE)
_PURE_STOP_RULE_RE = re.compile(r"^\s*stop(?!-)\b", re.IGNORECASE)
_PROGRAMMING_ONLY_PROGRESSION_RE = re.compile(
    r"^\s*(?:maintain(?:\s+(?:the|this))?\s+dose|keep\s+(?:the\s+)?dose\s+small|"
    r"do\s+not\s+(?:add|increase)\s+(?:sets?|volume)|no\s+(?:set|volume)\s+increase)\b",
    re.IGNORECASE,
)
_POSITIVE_PROGRESSION_RE = re.compile(
    r"\b(?:progress|advance|increase|raise|build|extend|heavier|more\s+resistance|reduce\s+assistance)\b",
    re.IGNORECASE,
)
_NEGATED_PROGRESSION_RE = re.compile(
    r"\b(?:do\s+not|don't|never)\s+(?:add|increase|progress|advance|raise|build|extend)\b",
    re.IGNORECASE,
)


def _strip_stop_rule_label(text: str) -> str:
    return re.sub(r"^\s*stop(?:\s+rule)?\s*:\s*", "", text, flags=re.IGNORECASE).strip()


def _is_programming_only_progression(text: str) -> bool:
    """True for taper/week dose constraints that do not advance the exercise."""
    clean = text.strip()
    if not clean:
        return False
    if _NEGATED_PROGRESSION_RE.search(clean):
        return bool(
            _PROGRAMMING_ONLY_PROGRESSION_RE.search(clean)
            or re.search(r"\b(?:taper|fight[ -]?week|sharpness|freshness)\b", clean, re.IGNORECASE)
        )
    if _POSITIVE_PROGRESSION_RE.search(clean):
        return False
    return bool(_PROGRAMMING_ONLY_PROGRESSION_RE.search(clean))


def _split_progression_and_stop_rules(value: Any) -> tuple[str, list[str]]:
    """Separate a legacy mixed progression string from labelled stop criteria."""
    text = _coerce_str(value).strip()
    if not text:
        return "", []
    match = _STOP_RULE_LABEL_RE.search(text)
    if match:
        progression = text[: match.start()].rstrip(" \t—–-:;,.")
        stop = text[match.end() :].strip()
        stops = [stop] if stop else []
        if progression and _is_programming_only_progression(progression):
            progression = ""
        return progression, stops
    if _PURE_STOP_RULE_RE.search(text):
        return "", [_strip_stop_rule_label(text)]
    if _is_programming_only_progression(text):
        return "", []
    return text, []


def _dedupe_stop_rules(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = _strip_stop_rule_label(value)
        key = text.casefold().strip().rstrip(".")
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


_BLOCK_DETAIL_LABEL_RE = re.compile(
    r"^\s*(purpose|why\s+today|easier|regress(?:ion)?|progress(?:ion)?|"
    r"deload|stop(?:\s+rule)?|swaps?|substitutions?|cue)\s*:\s*(.*)$",
    re.IGNORECASE,
)


def _dedupe_text_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = _coerce_str(value).strip()
        key = re.sub(r"\s+", " ", text).casefold().rstrip(" .")
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _normalize_mindset(value: Any) -> dict[str, Any]:
    out = dict(value) if isinstance(value, dict) else {}
    out["intent"] = _coerce_str(out.get("intent"))
    out["focus_cue"] = _coerce_str(out.get("focus_cue"))
    out["reset_cue"] = _coerce_str(out.get("reset_cue"))
    # Optional anchors: keep when the source provides them, else leave unset (None).
    for optional_key in ("confidence_anchor", "context"):
        if out.get(optional_key) is not None:
            out[optional_key] = _coerce_str(out.get(optional_key))
    return out


def _normalize_fractional_intent(effort: dict[str, Any]) -> None:
    """Rewrite an intent written as a fraction of full effort into words.

    The model writes ``{"method": "intent", "value": 1}`` meaning full intent;
    the card reads a bare number as a 10-point scale, so a power block showed
    "Intent 1/10". A value in (0, 1] becomes "max" (1) or a percentage.
    """
    raw = effort.get("value")
    if isinstance(raw, bool):
        return
    if isinstance(raw, str):
        if not re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", raw):
            return
        raw = float(raw)
    if not isinstance(raw, (int, float)) or not 0 < raw <= 1:
        return
    effort["value"] = "max" if raw == 1 else f"{round(raw * 100)}%"
    effort["scale"] = None


def _normalize_effort(value: Any) -> dict[str, Any] | None:
    """Effort as an ``EffortPrescription`` dict; tolerate a bare "RPE 7-8" string.

    A dict keeps its value (the schema allows float or str) and only has its
    method aliased onto the enum. A bare string or number becomes an RPE/RIR
    prescription when a number can be read from it; anything unreadable becomes
    None (the field is optional) instead of failing the card.
    """
    if isinstance(value, dict):
        out = dict(value)
        out["method"] = _enum(out.get("method"), _EFFORT_METHOD_VALUES, "RPE", _EFFORT_METHOD_ALIASES)
        if not isinstance(out.get("value"), (int, float, str)) or isinstance(out.get("value"), bool):
            number = _coerce_float(out.get("value"))
            if number is None:
                return None
            out["value"] = number
        if out.get("scale") is not None:
            out["scale"] = _coerce_str(out.get("scale")).strip() or None
        if out["method"] == "intent":
            _normalize_fractional_intent(out)
        return out
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return {"method": "RPE", "value": float(value), "scale": "1-10"}
    if isinstance(value, str):
        number = _coerce_float(value)
        if number is None:
            return None
        method = "RIR" if re.search(r"\brir\b", value, re.I) else "RPE"
        return {"method": method, "value": number, "scale": "1-10" if method == "RPE" else None}
    return None


_TEMPO_PHASES = ("eccentric", "pause_bottom", "concentric", "pause_top")
_TEMPO_CHAIN_RE = re.compile(
    r"\s*([0-9xX])\s*[-:/ ]?\s*([0-9xX])\s*[-:/ ]?\s*([0-9xX])(?:\s*[-:/ ]?\s*([0-9xX]))?\s*"
)


def _tempo_phase(value: Any) -> int | str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = _coerce_str(value).strip()
    if not text:
        return None
    return int(text) if text.isdigit() else ("X" if text.casefold() == "x" else text)


def _normalize_tempo(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    """``(tempo, cue)``: the schema's phase object, or the source wording as a cue.

    The converter sometimes writes tempo as a chain ("3-1-1-0", "31X0") or as
    prose ("slow 3 sec lower"). A string there failed the whole card on one
    field, so a chain becomes phases and any other wording stays on the block
    as a "Tempo:" cue rather than being dropped.
    """
    if value is None:
        return None, None
    if isinstance(value, dict):
        phases = {key: _tempo_phase(value.get(key)) for key in _TEMPO_PHASES}
        phases = {key: phase for key, phase in phases.items() if phase is not None}
        return (phases or None), None
    if isinstance(value, (list, tuple)):
        items = [_tempo_phase(item) for item in value][: len(_TEMPO_PHASES)]
        phases = {key: item for key, item in zip(_TEMPO_PHASES, items) if item is not None}
        return (phases or None), None
    text = _coerce_str(value).strip()
    if not text:
        return None, None
    chain = _TEMPO_CHAIN_RE.fullmatch(text)
    if chain:
        phases = {
            key: _tempo_phase(item)
            for key, item in zip(_TEMPO_PHASES, chain.groups())
            if item is not None
        }
        return phases, None
    return None, f"Tempo: {text}"


def _reps_echo_interval_work(block: Mapping[str, Any]) -> bool:
    """True when a rounds x work block's bare rep count only repeats the work seconds.

    The converter reads "2x3s" as rounds 2, work 3 sec and, wrongly, reps 3.
    That rep count is not a volume and renders as a meaningless "Volume 3".
    """
    rounds = block.get("rounds")
    sets = block.get("sets")
    work = block.get("work")
    reps = block.get("reps")
    if isinstance(rounds, bool) or not isinstance(rounds, (int, float)) or rounds <= 0:
        return False
    if isinstance(sets, (int, float)) and not isinstance(sets, bool) and sets > 0:
        return False
    if not isinstance(work, Mapping) or str(work.get("unit") or "").strip().lower() not in {
        "s", "sec", "secs", "second", "seconds"
    }:
        return False
    work_value = work.get("value")
    if isinstance(work_value, bool) or not isinstance(work_value, (int, float)) or work_value <= 0:
        return False
    if isinstance(reps, bool):
        return False
    if isinstance(reps, (int, float)):
        reps_value: float | None = float(reps)
    elif isinstance(reps, str) and re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", reps):
        reps_value = float(reps)
    else:
        reps_value = None
    return reps_value is not None and reps_value == float(work_value)


def _normalize_block(value: Any) -> dict[str, Any]:
    out = dict(value) if isinstance(value, dict) else {}
    # Identifier metadata is not athlete-facing content.  Preserve a useful id,
    # but leave it absent/null instead of fabricating the same collision-prone
    # fallback for every block or rejecting the whole card.
    out["block_id"] = _coerce_str(out.get("block_id")).strip() or None
    out["block_type"] = _enum(out.get("block_type"), _BLOCK_TYPE_VALUES, "accessory", _BLOCK_TYPE_ALIASES)
    out["display_name"] = _coerce_str(out.get("display_name"))
    if "load" in out:
        out["load"] = _normalize_load(out.get("load"))
    # Per-field default units follow the bank conventions: work/rest in seconds
    # (work_sec/rest_sec), duration in minutes (total_minutes), distance in meters.
    for measured_key, default_unit in (
        ("rest", "seconds"),
        ("work", "seconds"),
        ("distance", "meters"),
        ("duration", "minutes"),
    ):
        if measured_key in out:
            out[measured_key] = _normalize_measured(out.get(measured_key), default_unit)
    if _reps_echo_interval_work(out):
        out["reps"] = None
    if "tempo" in out:
        tempo, tempo_cue = _normalize_tempo(out.get("tempo"))
        out["tempo"] = tempo
        if tempo_cue:
            out["coaching_cues"] = [*_coerce_str_list(out.get("coaching_cues")), tempo_cue]
    # Carry coaching detail through, tolerating a single string instead of a
    # list. An explicit null must also become [] — the schema fields are
    # non-optional lists, so a passed-through None rejects the whole card.
    for list_key in ("coaching_cues", "regression_options", "substitutions", "stop_rules"):
        if list_key in out:
            out[list_key] = _coerce_str_list(out.get(list_key))
    for text_key in ("purpose", "why_today", "deload_rule"):
        if text_key in out:
            text_value = _coerce_str(out.get(text_key)).strip()
            if text_value:
                out[text_key] = text_value
            else:
                out.pop(text_key, None)

    # Converter/source prose can leak labelled rationale and adjustment lines into
    # coaching_cues. Route each label to its semantic field so the red cue bullets
    # remain strictly about how to execute the exercise.
    cues = _coerce_str_list(out.get("coaching_cues"))
    cleaned_cues: list[str] = []
    stop_cues: list[str] = []
    regression_cues: list[str] = []
    substitution_cues: list[str] = []
    progression_cues: list[str] = []
    for cue in cues:
        if re.fullmatch(
            r"\s*(?:progress(?:ion)?\s*/\s*)?regress(?:ion)?\s*(?:/\s*)?\s*",
            cue,
            re.IGNORECASE,
        ):
            continue
        labelled = _BLOCK_DETAIL_LABEL_RE.match(cue)
        if labelled:
            label = re.sub(r"\s+", " ", labelled.group(1).strip().casefold())
            detail = labelled.group(2).strip()
            if not detail:
                continue
            if label == "purpose":
                out.setdefault("purpose", detail)
            elif label == "why today":
                out.setdefault("why_today", detail)
            elif label in {"easier", "regress", "regression"}:
                regression_cues.append(detail)
            elif label in {"progress", "progression"}:
                progression_cues.append(detail)
            elif label == "deload":
                out.setdefault("deload_rule", detail)
            elif label.startswith("stop"):
                stop_cues.append(detail)
            elif label == "cue":
                cleaned_cues.append(detail)
            else:
                substitution_cues.append(detail)
            continue
        if re.match(r"^\s*stop(?!-)", cue, re.IGNORECASE):
            stop_cues.append(cue.strip())
            continue
        cleaned_cues.append(cue)

    out["coaching_cues"] = _dedupe_text_values(cleaned_cues)
    out["regression_options"] = _dedupe_text_values(
        _coerce_str_list(out.get("regression_options")) + regression_cues
    )
    out["substitutions"] = _dedupe_text_values(
        _coerce_str_list(out.get("substitutions")) + substitution_cues
    )
    if "red_flags" in out:
        out["red_flags"] = [_normalize_red_flag(rule) for rule in _as_dict_list(out.get("red_flags"))]
    if "effort" in out:
        out["effort"] = _normalize_effort(out.get("effort"))
    # Accessory work is never dosed by intent: a block prescribed "at max
    # speed" is a jump, bound or throw the model filed under the enum's
    # default bucket, so the card would tag a power drill "Accessory".
    effort = out.get("effort")
    if out["block_type"] == "accessory" and isinstance(effort, dict) and effort.get("method") == "intent":
        out["block_type"] = "plyometric_power"

    progression_rule = ""
    embedded_stops: list[str] = []
    progression_candidates = [
        _coerce_str(out.get("progression_rule")).strip(),
        *progression_cues,
    ]
    for candidate in progression_candidates:
        if not candidate:
            continue
        candidate_progression, candidate_stops = _split_progression_and_stop_rules(candidate)
        embedded_stops.extend(candidate_stops)
        if candidate_progression and not progression_rule:
            progression_rule = candidate_progression
    stop_rules = _dedupe_stop_rules(
        _coerce_str_list(out.get("stop_rules")) + stop_cues + embedded_stops
    )
    if progression_rule:
        progression_rule, enumerated_deload = _split_week_enumerated_rule(progression_rule)
        if enumerated_deload and not _coerce_str(out.get("deload_rule")).strip():
            out["deload_rule"] = enumerated_deload
    if "deload_rule" in out:
        # "Week 4 keep the sets light" -> "Keep the sets light."
        deload_clauses = _week_enumerated_clauses(_coerce_str(out.get("deload_rule")))
        if deload_clauses:
            out["deload_rule"] = next(
                (deload_clauses[week] for week in sorted(deload_clauses) if week >= 4),
                next(iter(deload_clauses.values())),
            )
    if progression_rule and not _is_hedge_only_adjustment(progression_rule):
        out["progression_rule"] = progression_rule
    else:
        out.pop("progression_rule", None)
    if _is_hedge_only_adjustment(out.get("deload_rule")):
        out.pop("deload_rule", None)
    if stop_rules or "stop_rules" in out:
        out["stop_rules"] = stop_rules

    # A card names the exercise the athlete is doing. Source prose sometimes
    # offers a choice instead ("Short sprint bounds or low box jumps"), which
    # leaves the athlete deciding mid-session and makes the dose ambiguous. Keep
    # the first option as the exercise and demote the alternative to a swap, so
    # nothing is lost and no card asks a question. A superset ("A + B") is a
    # deliberate pairing and is never split.
    name, alternative = _split_alternative_display_name(out.get("display_name"))
    if alternative:
        out["display_name"] = name
        out["substitutions"] = _dedupe_text_values(
            _coerce_str_list(out.get("substitutions")) + [alternative]
        )
    # "Assault Bike - 25 min": the dose belongs in the prescription fields, which
    # the card already shows. Left in the name it repeats on screen and the name
    # no longer matches the exercise's demo video. Only cut it when the block
    # carries its own prescription, so the dose is never lost.
    if _block_has_prescription(out):
        out["display_name"] = strip_dose_suffix(out.get("display_name"))
    return out


# "Increase hold time slowly", "Add small load each week" — an instruction whose
# only magnitude is a hedge. The athlete cannot act on it: the card already shows
# 3 x 30 sec, so the line owed them "3 x 40 sec". A hedge alongside a real target
# is fine ("Add 2.5 kg, building slowly"), and so is a progression that is
# genuinely qualitative ("Progress to live resistance once timing holds") or a
# minimal step the plan cannot number ("add the smallest jump on the bar") — this
# only strips lines that hedge INSTEAD of instructing. \bsmall\b deliberately does
# not match "smallest".
_HEDGE_MAGNITUDE_RE = re.compile(
    r"\b(?:small|slight(?:ly)?|slow(?:ly)?|gradual(?:ly)?|a bit|a little|"
    r"marginal(?:ly)?|modest(?:ly)?)\b",
    re.IGNORECASE,
)
# "Week 2 add 1 more burst, week 3 add 1 more burst, week 4 keep same low
# volume." The card shows ONE week and already labels it, so an enumeration of
# the whole block reads as other weeks' work on every card. Split it: the
# progression week's step becomes the progression, and the week-4 clause becomes
# the deload the card shows on its own deload week.
_WEEK_CLAUSE_RE = re.compile(
    r"\bweeks?\s*(\d+)(?:\s*(?:and|&|/|,|\+|to|-)\s*(\d+))?\b\s*[:,.\-–—]?\s*",
    re.IGNORECASE,
)


def _week_enumerated_clauses(text: str) -> dict[int, str]:
    """``{2: "add 1 more burst", 4: "keep same low volume"}`` from one line."""
    matches = list(_WEEK_CLAUSE_RE.finditer(text))
    if not matches:
        return {}
    clauses: dict[int, str] = {}
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        clause = text[match.end() : end].strip().strip(",;-–— ").strip()
        if not clause:
            continue
        clause = clause[:1].upper() + clause[1:]
        if not clause.endswith("."):
            clause += "."
        for group in (match.group(1), match.group(2)):
            if group is None:
                continue
            try:
                week = int(group)
            except ValueError:
                continue
            clauses.setdefault(week, clause)
    return clauses


def _split_week_enumerated_rule(value: Any) -> tuple[str, str]:
    """``(progression, deload)`` for a rule that spells out the block's weeks.

    Returns the text unchanged as the progression when it names no week, so a
    normal rule is never rewritten. The deload half is empty unless the line
    actually carries a week-4 (or later) clause.
    """
    text = _coerce_str(value).strip()
    if not text:
        return "", ""
    clauses = _week_enumerated_clauses(text)
    if not clauses:
        return text, ""
    deload = next((clauses[week] for week in sorted(clauses) if week >= 4), "")
    progression = next(
        (clauses[week] for week in sorted(clauses) if 2 <= week <= 3),
        "",
    )
    if not progression:
        # Only a deload clause was spelled out ("Week 4 keep it light"): there is
        # no progression step left to show, and inventing one is not this
        # module's job.
        return "", deload
    return progression, deload


_HAS_NUMBER_RE = re.compile(r"\d")
# A qualitative progression names what it moves TO: "Progress to live
# resistance", "Move onto controlled partner resistance", "Build into full
# range". That is a real target, so the hedge beside it is only tempo — the
# line still tells the athlete what to do this week and must survive. Without
# such a target, a hedge is all the line has ("Increase hold time slowly"),
# and it goes. An infinitive ("...to keep form") reads as a target here and
# keeps the line: a false keep costs a vague line, a false delete costs the
# athlete their whole instruction.
_PROGRESSION_TARGET_RE = re.compile(r"\b(?:to|into|onto)\s+\w", re.IGNORECASE)


def _is_hedge_only_adjustment(value: Any) -> bool:
    """True when a progression/deload line hedges INSTEAD of naming a target."""
    text = _coerce_str(value).strip()
    if not text:
        return False
    if _HAS_NUMBER_RE.search(text) or _PROGRESSION_TARGET_RE.search(text):
        return False
    return bool(_HEDGE_MAGNITUDE_RE.search(text))


# " or " joining two multi-word movements is a choice between exercises. Single
# words are left alone: in "Bike or rower intervals" the trailing noun belongs
# to both sides, so splitting would lose the prescription.
_ALTERNATIVE_NAME_RE = re.compile(
    r"^(?P<first>[^()]*?\S(?:\s+\S+)+?)\s+or\s+(?P<second>\S+(?:\s+\S+)+)$",
    re.IGNORECASE,
)


def _split_alternative_display_name(value: Any) -> tuple[str, str]:
    """``("A or B") -> ("A", "B")``; ``("", "")`` when the name is a single exercise."""
    name = _coerce_str(value).strip()
    if not name or "+" in name:
        return name, ""
    match = _ALTERNATIVE_NAME_RE.match(name)
    if not match:
        return name, ""
    first = match.group("first").strip(" ,;-")
    second = match.group("second").strip(" ,;-")
    if not first or not second:
        return name, ""
    return first, second[:1].upper() + second[1:]


# Fields that make a block an actual prescribed exercise rather than a mention.
_BLOCK_PRESCRIPTION_KEYS = ("sets", "reps", "work", "rest", "duration", "distance", "load")


def _block_has_prescription(block: Mapping[str, Any]) -> bool:
    return any(block.get(key) not in (None, "", [], {}) for key in _BLOCK_PRESCRIPTION_KEYS)


def _block_identity(block: Mapping[str, Any]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(block.get("display_name") or "").casefold()).strip()


def _collapse_unprescribed_duplicate_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop a duplicate block that only *mentions* an exercise already prescribed.

    Stage 2 sometimes follows a prescribed line with an explanatory aside
    ("Note: Loaded Carry Touch is a scheduled microdose ..."). Converted to a
    card that becomes a second block with the same name and no dose, so the
    athlete sees the same exercise twice — once real, once empty.

    A dose-less duplicate is merged into its prescribed host. Prescribed physical
    blocks stay distinct, while otherwise-identical mindset blocks from repeated
    tactical copy retain only the first block.
    """
    prescribed: dict[str, dict[str, Any]] = {}
    for block in blocks:
        identity = _block_identity(block)
        if identity and _block_has_prescription(block):
            prescribed.setdefault(identity, block)
    kept: list[dict[str, Any]] = []
    for block in blocks:
        identity = _block_identity(block)
        host = prescribed.get(identity)
        if host is not None and block is not host and not _block_has_prescription(block):
            for list_key in ("coaching_cues", "regression_options", "substitutions", "stop_rules"):
                merged = _coerce_str_list(host.get(list_key)) + _coerce_str_list(block.get(list_key))
                if merged:
                    host[list_key] = _dedupe_text_values(merged)
            for text_key in ("purpose", "why_today"):
                if not str(host.get(text_key) or "").strip() and str(block.get(text_key) or "").strip():
                    host[text_key] = block[text_key]
            continue
        if block.get("block_type") == "mindset" and any(
            {key: value for key, value in prior.items() if key != "block_id"}
            == {key: value for key, value in block.items() if key != "block_id"}
            for prior in kept
        ):
            continue
        kept.append(block)
    return kept


def _normalize_session(value: Any) -> dict[str, Any]:
    out = dict(value) if isinstance(value, dict) else {}
    out["session_id"] = _coerce_str(out.get("session_id")).strip() or None
    raw_session_type = out.get("session_type")
    normalized_session_type = _enum(
        raw_session_type, _SESSION_TYPE_VALUES, "mixed", _SESSION_TYPE_ALIASES
    )
    out["session_type"] = (
        "rehab"
        if normalized_session_type == "mixed" and _raw_session_type_is_rehab(raw_session_type)
        else normalized_session_type
    )
    out["title"] = _strip_leading_weekday(_coerce_str(out.get("title")))
    out["objective"] = _coerce_str(out.get("objective"))
    out["mindset_anchor"] = _normalize_mindset(out.get("mindset_anchor"))
    if "completion_status" in out:
        out["completion_status"] = _enum(out.get("completion_status"), _COMPLETION_VALUES, "not_started")
    if out.get("planned_duration") is not None:
        out["planned_duration"] = _normalize_measured(out.get("planned_duration"), "minutes")
    out["blocks"] = _collapse_unprescribed_duplicate_blocks(
        [_normalize_block(block) for block in _as_dict_list(out.get("blocks"))]
    )
    return out


def _coerce_optional_text(value: Any) -> str | None:
    """Optional athlete-facing copy as a string, or ``None``.

    The converter sometimes mirrors a plan-level object into a day-level text
    slot (``nutrition.weight_cut_warning`` is an object; ``today_card``'s is a
    string). Keep the object's own display copy when it has one; anything else
    is dropped rather than failing the whole day.
    """
    if isinstance(value, Mapping):
        value = value.get("display_text") or value.get("text")
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item).strip() for item in value if isinstance(item, str) and item.strip())
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _normalize_today_card(value: Any) -> dict[str, Any]:
    out = dict(value) if isinstance(value, dict) else {}
    out["headline"] = _coerce_str(out.get("headline"))
    if out["headline"].strip().casefold() == "technical_combat":
        out["headline"] = "Technical Combat"
    out["readiness_status"] = _enum(out.get("readiness_status"), _READINESS_VALUES, "train_as_planned")
    out["mindset_anchor"] = _normalize_mindset(out.get("mindset_anchor"))
    for key in ("primary_warning", "nutrition_summary", "weight_cut_warning"):
        if key in out:
            out[key] = _coerce_optional_text(out.get(key))
    # Coach-owned contact that coexists with the day's app sessions — keep only a
    # non-empty string so the renderer never shows a blank contact block.
    contact = _coerce_str(out.get("coach_led_contact")).strip()
    if contact:
        out["coach_led_contact"] = "Technical Combat" if contact.casefold() == "technical_combat" else contact
    else:
        out.pop("coach_led_contact", None)
    reason = _coerce_str(out.get("contact_reason")).strip()
    if reason:
        out["contact_reason"] = reason
    else:
        out.pop("contact_reason", None)
    return out


_COACH_LED_CONTACT_RE = re.compile(
    r"\b(coach|spar|technical\s+only|no\s+hard\s+sparring|boxing|pad\s?work|pads|mitts?"
    r"|light\s+(?:technical\s+)?combat)\b",
    re.I,
)


def _coach_led_contact_label(session: dict[str, Any]) -> str | None:
    """A contact-only session label, or None when this is real app work."""
    if _as_list(session.get("blocks")):
        return None
    text = " ".join(
        part
        for part in (
            _coerce_str(session.get("title")).strip(),
            _coerce_str(session.get("objective")).strip(),
            _coerce_str(session.get("session_type")).strip(),
        )
        if part
    )
    if not text or not _COACH_LED_CONTACT_RE.search(text):
        return None
    return _coerce_str(session.get("title")).strip() or _coerce_str(session.get("objective")).strip() or text


def _fold_coach_led_sessions_into_today_card(day: dict[str, Any]) -> None:
    """Normalize same-day contact + app work into the renderer's canonical shape."""
    sessions = [session for session in _as_list(day.get("sessions")) if isinstance(session, dict)]
    if len(sessions) < 2:
        return
    app_sessions: list[dict[str, Any]] = []
    contact_labels: list[str] = []
    for session in sessions:
        label = _coach_led_contact_label(session)
        if label:
            contact_labels.append(label)
        else:
            app_sessions.append(session)
    if not contact_labels or not app_sessions:
        return
    card = day.get("today_card")
    if not isinstance(card, dict):
        card = _normalize_today_card(card)
        day["today_card"] = card
    if not _coerce_str(card.get("coach_led_contact")).strip():
        label = contact_labels[0]
        card["coach_led_contact"] = "Technical Combat" if label.casefold() == "technical_combat" else label
    day["sessions"] = app_sessions


def _coach_led_contact_intensity(day: dict[str, Any]) -> str | None:
    card = day.get("today_card")
    if not isinstance(card, dict):
        return None
    text = _coerce_str(card.get("coach_led_contact")).strip().lower()
    if not text:
        return None
    if "technical" in text or "no hard" in text:
        return "moderate"
    if "reduced" in text or "deload" in text:
        return "low"
    if re.search(r"\bspar", text):
        return "high"
    if re.search(r"\b(coach|boxing|pad\s?work|pads|mitts?)\b", text):
        return "moderate"
    return None


def _block_intensity(block: Any) -> str | None:
    """Rate a single block "high" / "moderate" / "low", or ``None`` when silent.

    Reads, in priority order, the explicit intensity tag, the RPE effort, the
    %1RM load, and finally the block type. Explicit effort/load always wins over
    block type, so a light primer plyo (RPE 3–4) reads "low" rather than being
    flagged "high" just for being plyometric.
    """
    if not isinstance(block, dict):
        return None
    tag = _coerce_str(block.get("intensity")).strip().lower()
    effort = block.get("effort")
    rpe = None
    if isinstance(effort, dict) and str(effort.get("method") or "").upper() == "RPE":
        rpe = _coerce_float(effort.get("value"))
    if rpe is None and "rpe" in tag:  # an RPE buried in the tag ("rpe 3-4")
        rpe = _coerce_float(tag)
    pct = None
    load = block.get("load")
    if isinstance(load, dict) and str(load.get("method") or "") == "percentage":
        pct = _coerce_float(load.get("value"))

    # Explicit hard signals.
    if tag in _HIGH_INTENSITY_WORDS:
        return "high"
    if rpe is not None and rpe >= 8:
        return "high"
    if pct is not None and pct >= 85:
        return "high"

    # Explicit easy/moderate signals in priority order.
    if tag in _LOW_INTENSITY_WORDS:
        return "low"
    if rpe is not None:
        return "low" if rpe <= 5 else "moderate"
    if pct is not None:
        return "low" if pct < 70 else "moderate"
    if tag in {"moderate", "medium"}:
        return "moderate"
    # No effort/load number at all: a true power/speed block implies a hard day.
    if str(block.get("block_type") or "") in _HIGH_OUTPUT_BLOCK_TYPES:
        return "high"
    return None


def _session_intensity(session: Any) -> str | None:
    """Rate a session by its hardest block, falling back to the session type."""
    if not isinstance(session, dict):
        return None
    levels = [lvl for lvl in (_block_intensity(b) for b in _as_list(session.get("blocks"))) if lvl]
    if levels:
        return max(levels, key=_INTENSITY_RANK.__getitem__)
    stype = str(session.get("session_type") or "")
    if stype in _LOW_SESSION_TYPES:
        return "low"
    if stype == "sparring":
        return "high"
    return None


def _classify_day_type(day: dict[str, Any], fallback: str) -> str:
    """Derive the day's intensity badge from its content (overrides the model).

    ``fallback`` is the model's own value already coerced onto a valid enum (or
    ``""`` when it was missing/unrecognized). It is consulted only when the day
    carries no readable intensity signal at all.
    """
    sessions = [s for s in _as_list(day.get("sessions")) if isinstance(s, dict)]
    session_types = {str(s.get("session_type") or "") for s in sessions}

    # Competition / fight day is categorical, anchored on the countdown or a
    # fight session — never an intensity bucket.
    if _countdown_distance(day.get("countdown_label")) == 0 or "fight_or_match" in session_types:
        return "competition"
    # travel / reintegration are model-only states content cannot contradict.
    if fallback in {"travel", "reintegration"}:
        return fallback
    # A purely recovery day stays "recovery" even when its mobility/breathing/
    # cooldown work is broken into blocks — it is categorical, not an intensity.
    if session_types and session_types <= {"recovery"}:
        return "recovery"

    # The day is as hard as its hardest session. _session_intensity reads each
    # session by its hardest block and, for a session with no readable block,
    # falls back to its type (e.g. an empty coach-led sparring day is still
    # "high", never "rest").
    levels = [lvl for lvl in (_session_intensity(s) for s in sessions) if lvl]
    contact_level = _coach_led_contact_intensity(day)
    if contact_level:
        levels.append(contact_level)
    if levels:
        return max(levels, key=_INTENSITY_RANK.__getitem__)

    # No sessions / no readable signal at all: an empty day is "rest", otherwise
    # keep a valid model intensity guess, else default low (never silently
    # "moderate").
    if not sessions:
        return fallback if fallback in {"rest", "recovery"} else "rest"
    if fallback in {"high", "moderate", "low", "recovery", "rest"}:
        return fallback
    return "low"


def _countdown_distance(label: Any) -> int | None:
    """Parse the days-to-event from a countdown label (``D-1`` → 1, ``D0`` → 0)."""
    match = _COUNTDOWN_RE.match(_coerce_str(label).strip())
    if not match:
        return None
    try:
        return abs(int(match.group(1)))
    except ValueError:
        return None


def _normalize_priority_microdose(value: Any) -> dict[str, str] | None:
    """A complete planner microdose card, or ``None``.

    The microdose is planner-owned: the calendar spine re-attaches it from the
    weekly role map, so a partial converter copy is dropped, never repaired.
    """
    if not isinstance(value, Mapping):
        return None
    dose = {key: _coerce_str(value.get(key)).strip() for key in ("goal", "name", "prescription")}
    return dose if all(dose.values()) else None


def _normalize_day(value: Any) -> dict[str, Any]:
    out = dict(value) if isinstance(value, dict) else {}
    out["date"] = _coerce_str(out.get("date"))
    if "weekday" in out:
        # The source headers spell weekdays out ("D-23 (Friday)") and the model
        # mirrors them; the schema stores the short form.
        out["weekday"] = _normalize_weekday(out.get("weekday"))
    if "priority_microdose" in out:
        out["priority_microdose"] = _normalize_priority_microdose(out.get("priority_microdose"))
    out["countdown_label"] = _coerce_str(out.get("countdown_label"))
    out["phase_label"] = _normalize_phase(out.get("phase_label"))
    out["planning_week_index"] = _coerce_optional_int(out.get("planning_week_index"))
    planning_phase = _coerce_str(out.get("planning_week_phase")).strip().upper()
    out["planning_week_phase"] = _normalize_phase(planning_phase) if planning_phase else None
    out["planning_day_role_keys"] = _coerce_str_list(out.get("planning_day_role_keys"))
    out["today_card"] = _normalize_today_card(out.get("today_card"))
    out["sessions"] = [_normalize_session(session) for session in _as_dict_list(out.get("sessions"))]
    _fold_coach_led_sessions_into_today_card(out)
    # Derive the intensity badge from the now-normalized content; the model's own
    # value is only a last-resort fallback (see _classify_day_type).
    out["day_type"] = _classify_day_type(out, _enum(out.get("day_type"), _DAY_TYPE_VALUES, ""))
    return out


def _normalize_load_focus(value: Any) -> dict[str, Any]:
    out = dict(value) if isinstance(value, dict) else {}
    for key in ("volume", "intensity", "specificity", "fatigue_target"):
        out[key] = _enum(out.get(key), _LOAD_FOCUS_VALUES, "moderate")
    return out


def _normalize_progression(value: Any) -> dict[str, Any]:
    out = dict(value) if isinstance(value, dict) else {}
    out["week_type"] = _enum(out.get("week_type"), _WEEK_TYPE_VALUES, "build")
    out["planned_change_from_previous"] = _coerce_str(out.get("planned_change_from_previous"))
    return out


def _normalize_week(value: Any) -> dict[str, Any]:
    out = dict(value) if isinstance(value, dict) else {}
    out["week_id"] = _coerce_str(out.get("week_id")).strip() or None
    out["week_index"] = _coerce_int(out.get("week_index"), 0)
    out["phase_label"] = _normalize_phase(out.get("phase_label"))
    out["week_goal"] = _coerce_str(out.get("week_goal"))
    out["start_date"] = _coerce_str(out.get("start_date"))
    out["end_date"] = _coerce_str(out.get("end_date"))
    out["display_week_kind"] = _coerce_str(out.get("display_week_kind")).strip() or None
    out["planning_week_indices"] = [
        item
        for item in (_coerce_optional_int(v) for v in _as_list(out.get("planning_week_indices")))
        if item is not None
    ]
    out["phase_coverage"] = _coerce_str_list(out.get("phase_coverage"))
    out["load_focus"] = _normalize_load_focus(out.get("load_focus"))
    out["progression"] = _normalize_progression(out.get("progression"))
    out["days"] = [_normalize_day(day) for day in _as_dict_list(out.get("days"))]
    return out


def _normalize_plan_note(value: Any) -> dict[str, Any] | None:
    """Coerce a plan-level note, or ``None`` when it carries no text.

    Keeps only formatting fixes: the category is aliased onto a valid enum value
    (defaulting to ``general``), and label/text are coerced to strings. A note
    with no usable text is dropped rather than fabricated.
    """
    if isinstance(value, str):
        text = value.strip()
        return {"category": "general", "text": text} if text else None
    if not isinstance(value, dict):
        return None
    text = _coerce_str(value.get("text")).strip()
    if not text:
        return None
    out: dict[str, Any] = {
        "category": _enum(value.get("category"), _PLAN_NOTE_CATEGORY_VALUES, "general", {"weight cut": "weight_cut", "weight-cut": "weight_cut"}),
        "text": text,
    }
    label = _coerce_str(value.get("label")).strip()
    if label:
        out["label"] = label
    return out


def _normalize_plan_notes(value: Any) -> list[dict[str, Any]]:
    return [
        note
        for note in (_normalize_plan_note(item) for item in _as_list(value))
        if note is not None
    ]


def _normalize_red_flag(value: Any) -> dict[str, Any]:
    out = dict(value) if isinstance(value, dict) else {}
    out["rule_id"] = _coerce_str(out.get("rule_id")).strip() or None
    out["when"] = _enum(out.get("when"), _RED_FLAG_WHEN_VALUES, "morning_check_in")
    out["severity"] = _enum(out.get("severity"), _SEVERITY_VALUES, "amber")
    out["display_text"] = _coerce_str(out.get("display_text"))
    out["action"] = _coerce_str(out.get("action"))
    # Machine fields arrive as prose fragments more often than schema shapes
    # ("threshold": ">20%", "applies_to": "all sessions"); the display_text is the
    # athlete-facing rule, so a threshold that can't be read becomes None rather
    # than failing the whole card.
    if "threshold" in out:
        out["threshold"] = _coerce_float(out.get("threshold"))
    for optional_key in ("metric", "metric_group", "operator", "logic"):
        if out.get(optional_key) is not None:
            out[optional_key] = _coerce_str(out.get(optional_key)).strip() or None
    if "applies_to" in out:
        out["applies_to"] = _coerce_str_list(out.get("applies_to"))
    if out.get("replacement_session_type") is not None:
        replacement = _enum(
            out.get("replacement_session_type"), _SESSION_TYPE_VALUES, "", _SESSION_TYPE_ALIASES
        )
        out["replacement_session_type"] = replacement or None
    return out


def _normalize_plan_metadata(value: Any) -> dict[str, Any]:
    out = dict(value) if isinstance(value, dict) else {}
    out["title"] = _coerce_nonempty_str(out.get("title"), "Training Plan")
    out["sport"] = _coerce_str(out.get("sport"))
    out["plan_type"] = _enum(out.get("plan_type"), _PLAN_TYPE_VALUES, "general_performance")
    out["timezone"] = _coerce_nonempty_str(out.get("timezone"), "UTC")
    out["status"] = _enum(out.get("status"), _PLAN_STATUS_VALUES, "active")
    out["units"] = _enum(out.get("units"), _UNITS_VALUES, "metric")
    return out


def _normalize_athlete_context(value: Any) -> dict[str, Any]:
    out = dict(value) if isinstance(value, dict) else {}
    out["sport_profile"] = _coerce_str(out.get("sport_profile"))
    for list_key in ("known_issues", "equipment_access"):
        if list_key in out:
            out[list_key] = _coerce_str_list(out.get(list_key))
    return out


def _normalize_event_context(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    out = dict(value)
    if out.get("event_type") is not None:
        out["event_type"] = _enum(out.get("event_type"), _EVENT_TYPE_VALUES, "none")
    return out


def _normalize_nutrition(value: Any) -> dict[str, Any]:
    out = dict(value) if isinstance(value, dict) else {}
    for key in ("summary", "daily_focus", "training_day_guidance", "fight_week_guidance"):
        out[key] = _coerce_str(out.get(key))
    warning = out.get("weight_cut_warning")
    if isinstance(warning, dict):
        warning = dict(warning)
        warning["risk_level"] = _enum(warning.get("risk_level"), _RISK_LEVEL_VALUES, "none")
        warning["display_text"] = _coerce_str(warning.get("display_text"))
        out["weight_cut_warning"] = warning
    elif isinstance(warning, str):
        # Some conversions return the warning as a plain sentence. Wrap it in the
        # schema object with a neutral risk level; the text is preserved verbatim.
        text = warning.strip()
        out["weight_cut_warning"] = (
            {"risk_level": "none", "display_text": text, "requires_professional_support": False}
            if text
            else None
        )
    return out


# Decision aliases: only obvious, unambiguous synonyms map onto an established
# ReadinessStatus value. A readiness decision is self-report, so anything not
# matched here is dropped — never coerced into a fabricated default.
_DECISION_ALIASES = {
    "train": "train_as_planned",
    "as_planned": "train_as_planned",
    "train_as_plan": "train_as_planned",
    "train_normally": "train_as_planned",
    "modified": "modify",
    "modify_session": "modify",
    "pullback": "pull_back",
    "pull-back": "pull_back",
    "pull back": "pull_back",
    "unavailable": "unavailable",
}


def _normalize_checkin_decision(value: Any) -> str | None:
    """Resolve a check-in decision to a valid ``ReadinessStatus``, or ``None``.

    Accepts an exact enum value, a case-only variant, or one of a few obvious
    aliases. An unrecognized decision returns ``None`` so the caller drops the
    whole check-in — a readiness call is never invented.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if candidate in _READINESS_VALUES:
        return candidate
    lowered = candidate.lower()
    if lowered in _READINESS_VALUES:
        return lowered
    return _DECISION_ALIASES.get(lowered)


def _normalize_checkin_morning(value: Any) -> dict[str, Any] | None:
    """Coerce a morning self-report, or ``None`` when it cannot be trusted.

    ``sleep_quality``/``overall_readiness``/``pain`` are required self-report
    scores with NO neutral default in the schema. A missing or out-of-range score
    drops the whole check-in rather than fabricating a biometric/readiness value.
    Numeric strings ("4") are coerced because that is a formatting fix, not
    invention. Optional self-report context is carried through verbatim.
    """
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {}
    for key, low, high in (
        ("sleep_quality", 1, 5),
        ("overall_readiness", 1, 5),
        ("pain", 0, 10),
    ):
        score = _coerce_optional_int(value.get(key))
        if score is None or not (low <= score <= high):
            return None
        out[key] = score
    if value.get("location") is not None:
        out["location"] = _coerce_str(value.get("location"))
    if isinstance(value.get("injury_specific"), dict):
        out["injury_specific"] = value.get("injury_specific")
    return out


def _normalize_checkin_date(item: dict[str, Any], weeks: list[dict[str, Any]]) -> str | None:
    """Resolve a check-in's date, inferring from indices only when unambiguous.

    A present non-empty ``date`` string is used as-is. Otherwise, when the
    check-in carries integer ``week_index``/``day_index`` that resolve to exactly
    one dated day in the (already-normalized) ``weeks``, that date is used.
    Anything ambiguous or unresolved returns ``None`` and the caller drops the
    check-in — a date is never invented.
    """
    date = _coerce_str(item.get("date")).strip()
    if date:
        return date
    week_index = _coerce_optional_int(item.get("week_index"))
    day_index = _coerce_optional_int(item.get("day_index"))
    if week_index is None or day_index is None:
        return None
    resolved: list[str] = []
    for week in weeks:
        if not isinstance(week, dict):
            continue
        if _coerce_optional_int(week.get("week_index")) != week_index:
            continue
        days = week.get("days")
        if not isinstance(days, list) or not (0 <= day_index < len(days)):
            continue
        day = days[day_index]
        if isinstance(day, dict):
            day_date = _coerce_str(day.get("date")).strip()
            if day_date:
                resolved.append(day_date)
    # Unambiguous only: exactly one matching dated day.
    if len(resolved) == 1:
        return resolved[0]
    return None


def _normalize_daily_check_ins(
    value: Any, weeks: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Drop malformed daily check-ins so they cannot fail the whole plan (PR-7).

    A partial or garbled daily check-in must never sink an otherwise-valid
    structured plan. Each entry is conservatively repaired — numeric-string scores
    coerced, obvious decision aliases mapped, date inferred only when unambiguous —
    then validated against :class:`DailyCheckIn`. Anything that cannot be made
    trustworthy WITHOUT inventing self-report content (sleep, pain, readiness,
    decision, or date) is dropped. A non-list input, or a list with no salvageable
    entries, becomes an empty list (the model's own default).
    """
    if not isinstance(value, list):
        return []
    weeks = weeks if isinstance(weeks, list) else []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        date = _normalize_checkin_date(item, weeks)
        morning = _normalize_checkin_morning(item.get("morning"))
        decision = _normalize_checkin_decision(item.get("decision"))
        if date is None or morning is None or decision is None:
            continue
        candidate = {
            "date": date,
            "morning": morning,
            "decision": decision,
            "rules_triggered": _coerce_str_list(item.get("rules_triggered")),
        }
        try:  # final guarantee: only schema-valid entries survive
            DailyCheckIn.model_validate(candidate)
        except Exception:
            continue
        out.append(candidate)
    return out


# Intensity-label typo fix. Coaches read effort as ``RPE`` (rate of perceived
# exertion); models occasionally emit ``PRE`` instead. Only an all-caps ``PRE``
# immediately preceding a value on the RPE scale (1-10, optionally a range like
# ``7–8``) is corrected, so ordinary words (``pre-fight``, ``PRE-FIGHT WEEK``)
# and unrelated numbers (``PRE 2024``) are never touched.
_RPE_LABEL_TYPO_RE = re.compile(r"\bPRE\b(?=\s*(?:10|[1-9])\b)")

# Keys whose verbatim text must never be rewritten by cosmetic label fixes.
_LABEL_FIX_SKIP_KEYS = frozenset({"raw_markdown_fallback"})


def _fix_label_typos(node: Any) -> Any:
    """Recursively correct intensity-label typos (``PRE`` → ``RPE``) in text.

    A pure formatting fix applied to every string leaf except the verbatim
    ``raw_markdown_fallback``. The regex is deliberately narrow so it only
    rewrites the effort label, never coaching content.
    """
    if isinstance(node, dict):
        return {
            key: value if key in _LABEL_FIX_SKIP_KEYS else _fix_label_typos(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_fix_label_typos(item) for item in node]
    if isinstance(node, str):
        return _RPE_LABEL_TYPO_RE.sub("RPE", node)
    return node


def _normalize_safety_text(text: Any) -> str:
    """Lowercase, collapse whitespace, drop trailing punctuation — for dedup."""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return cleaned.rstrip(".!;: ").strip()


def _dedupe_plan_safety_text(plan: dict[str, Any]) -> dict[str, Any]:
    """Drop repeated athlete-facing warning/constraint text (PR-QC).

    Stop/warning language tends to echo across active notes, red flags, and
    final notes. The authoritative place for a stop rule is the red-flag rule,
    so this keeps the strongest copy there and removes only exact duplicates:

    * collapses red-flag rules that are byte-for-byte identical (same text,
      action, severity, and trigger) — never distinct or session-specific rules;
    * drops a plan note whose text repeats an earlier plan note or an existing
      red-flag ``display_text``.

    Mutates and returns ``plan``. No safety rule is ever removed outright — the
    warning always survives in at least one place.
    """
    rules = plan.get("red_flag_rules")
    if isinstance(rules, list):
        seen_rules: set[tuple] = set()
        deduped_rules: list[Any] = []
        for rule in rules:
            if not isinstance(rule, dict):
                deduped_rules.append(rule)
                continue
            display = _normalize_safety_text(rule.get("display_text"))
            key = (
                display,
                _normalize_safety_text(rule.get("action")),
                rule.get("severity"),
                rule.get("when"),
            )
            if display and key in seen_rules:
                continue
            seen_rules.add(key)
            deduped_rules.append(rule)
        plan["red_flag_rules"] = deduped_rules
        rules = deduped_rules

    red_flag_texts = {
        _normalize_safety_text(rule.get("display_text"))
        for rule in (rules or [])
        if isinstance(rule, dict) and _normalize_safety_text(rule.get("display_text"))
    }

    notes = plan.get("plan_notes")
    if isinstance(notes, list):
        seen_notes: set[str] = set()
        deduped_notes: list[Any] = []
        for note in notes:
            if not isinstance(note, dict):
                deduped_notes.append(note)
                continue
            norm = _normalize_safety_text(note.get("text"))
            if norm and (norm in seen_notes or norm in red_flag_texts):
                continue
            if norm:
                seen_notes.add(norm)
            deduped_notes.append(note)
        plan["plan_notes"] = deduped_notes

    return plan


def normalize_structured_plan_candidate(data: Any) -> Any:
    """Conservatively coerce a model's near-miss structured JSON into schema shape.

    Only fixes obvious *formatting* mistakes (enum aliases, string loads/rests,
    string countdown labels, non-list ``daily_check_ins``, non-string
    ``progression_notes``, missing required meta fields filled with neutral
    structural defaults, intensity-label typos, and duplicated warning text). It
    never invents training content and never alters ``raw_markdown_fallback``
    text. Non-dict input is returned unchanged so the strict schema can reject
    it. Never raises.
    """

    if not isinstance(data, dict):
        return data

    plan = copy.deepcopy(data)

    if not isinstance(plan.get("schema_version"), str) or not plan.get("schema_version"):
        plan["schema_version"] = SCHEMA_VERSION
    plan["plan_metadata"] = _normalize_plan_metadata(plan.get("plan_metadata"))
    plan["athlete_context"] = _normalize_athlete_context(plan.get("athlete_context"))
    if plan.get("event_context") is not None:
        plan["event_context"] = _normalize_event_context(plan.get("event_context"))
    plan["countdown_labels"] = [
        label
        for label in (_normalize_countdown_label(item) for item in _as_list(plan.get("countdown_labels")))
        if label is not None
    ]
    plan["red_flag_rules"] = [_normalize_red_flag(rule) for rule in _as_dict_list(plan.get("red_flag_rules"))]
    plan["plan_notes"] = _normalize_plan_notes(plan.get("plan_notes"))
    plan["weeks"] = [_normalize_week(week) for week in _as_dict_list(plan.get("weeks"))]
    # daily_check_ins must be a list of fully-valid entries; a non-list, or a
    # partial/garbled entry, must never fail the whole plan. Malformed entries are
    # dropped conservatively (no fabricated scores/decisions/dates), leaving [] if
    # none survive — matching the model's empty-list default.
    plan["daily_check_ins"] = _normalize_daily_check_ins(
        plan.get("daily_check_ins"), plan.get("weeks")
    )
    plan["nutrition"] = _normalize_nutrition(plan.get("nutrition"))
    plan["progression_notes"] = _coerce_str(plan.get("progression_notes"))
    plan["raw_markdown_fallback"] = _coerce_str(plan.get("raw_markdown_fallback"))
    # Cosmetic label fix first (so dedup compares corrected text), then collapse
    # repeated athlete-facing warnings. Both are formatting-only and never touch
    # raw_markdown_fallback or drop a safety rule outright.
    plan = _fix_label_typos(plan)
    plan = _dedupe_plan_safety_text(plan)
    return plan


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


_SOURCE_BULLET_RE = re.compile(r"^[-*•]\s+(?P<title>.+?)(?::|\s[—–-]\s|$)")
_SOURCE_WHY_RE = re.compile(r"^\s*why\s*:\s*(?P<why>.+)$", re.I)
_HEADER_TITLE_RE = re.compile(r"[—–]\s*(?P<title>[^—–]+?)\s*$")
_COUNTDOWN_NUMBER_RE = re.compile(r"D-\s*(\d+)", re.I)
# Server-owned sessions (Tactical Watch, deterministic support) are placed by
# the planner, never by the converter, and are never regrouped.
_SERVER_OWNED_SESSION_PREFIXES = ("locked-", "deterministic-")


def _name_tokens(value: Any) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", strip_dose_suffix(_coerce_str(value)).casefold()))


def _source_session_groups(raw_markdown: str) -> dict[int, list[dict[str, Any]]]:
    """Per D-day, each source session: its header title, Why line and bullet titles."""
    groups: dict[int, list[dict[str, Any]]] = {}
    current: dict[str, Any] | None = None
    for line in str(raw_markdown or "").splitlines():
        day = _day_header_dday(line)
        if day is not None:
            title_match = _HEADER_TITLE_RE.search(line)
            current = {
                "title": title_match.group("title").strip() if title_match else "",
                "why": "",
                "bullets": [],
            }
            groups.setdefault(day, []).append(current)
            continue
        if current is None:
            continue
        bullet = _SOURCE_BULLET_RE.match(line)
        if bullet:
            current["bullets"].append(_name_tokens(bullet.group("title")))
            continue
        why = _SOURCE_WHY_RE.match(line)
        if why and not current["why"]:
            current["why"] = why.group("why").strip()
    return groups


def _owning_source_group(session: dict[str, Any], groups: list[dict[str, Any]]) -> int | None:
    """Index of the one source session holding every block of ``session``."""
    blocks = [block for block in _as_list(session.get("blocks")) if isinstance(block, dict)]
    if not blocks:
        return None
    owners: set[int] = set()
    for block in blocks:
        tokens = _name_tokens(block.get("display_name"))
        if not tokens:
            return None
        matches = {
            index
            for index, group in enumerate(groups)
            if any(tokens <= bullet or bullet <= tokens for bullet in group["bullets"] if bullet)
        }
        if len(matches) != 1:
            return None
        owners |= matches
    return owners.pop() if len(owners) == 1 else None


def regroup_split_sessions(plan: Any, raw_markdown: str) -> Any:
    """Merge sessions the converter split out of ONE source session.

    The plan text lists a session as a day header with its exercises bulleted
    beneath it. The converter sometimes emits each bullet as its own session
    (a strength day becomes four one-exercise cards, each with its own title
    and Why). This puts them back: every converter session whose blocks all sit
    under the same source header is folded into the first of them, which takes
    the header's title and Why line. Anything that cannot be matched to exactly
    one header is left as it is.
    """
    groups_by_day = _source_session_groups(raw_markdown)
    if not isinstance(plan, dict) or not groups_by_day:
        return plan
    plan = copy.deepcopy(plan)
    for week in _as_list(plan.get("weeks")):
        for day in _as_list(week.get("days") if isinstance(week, dict) else None):
            if not isinstance(day, dict):
                continue
            match = _COUNTDOWN_NUMBER_RE.search(_coerce_str(day.get("countdown_label")))
            groups = groups_by_day.get(int(match.group(1))) if match else None
            sessions = day.get("sessions")
            if not groups or not isinstance(sessions, list) or len(sessions) < 2:
                continue
            merged: list[Any] = []
            first_by_group: dict[int, dict[str, Any]] = {}
            for session in sessions:
                session_id = _coerce_str(session.get("session_id") if isinstance(session, dict) else "")
                owner = (
                    None
                    if not isinstance(session, dict) or session_id.startswith(_SERVER_OWNED_SESSION_PREFIXES)
                    else _owning_source_group(session, groups)
                )
                if owner is None:
                    merged.append(session)
                    continue
                first = first_by_group.get(owner)
                if first is None:
                    first_by_group[owner] = session
                    merged.append(session)
                    continue
                first["blocks"] = _as_list(first.get("blocks")) + _as_list(session.get("blocks"))
                if _coerce_str(first.get("session_type")) != _coerce_str(session.get("session_type")):
                    first["session_type"] = "mixed"
                first["_regrouped_from"] = owner
            if len(merged) == len(sessions):
                continue
            for owner, first in first_by_group.items():
                if first.pop("_regrouped_from", None) is None:
                    continue
                group = groups[owner]
                if group["title"]:
                    first["title"] = group["title"]
                if group["why"]:
                    first["objective"] = group["why"]
            day["sessions"] = merged
    return plan


def _strip_and_normalize(data: Any) -> Any:
    """Strip banned biometric keys then conservatively normalize. Never raises."""
    stripped, _removed = strip_biometric_fields(data)
    try:
        return normalize_structured_plan_candidate(stripped)
    except Exception:  # normalization must never break the fallback flow
        return stripped


def _progression_week_for_countdown(
    progression_weeks: list[dict[str, Any]],
    *,
    week_index: int,
    countdown_start: int | None,
) -> dict[str, Any] | None:
    """Resolve the deterministic progression entry that owns a rendered week."""

    if countdown_start is not None:
        for entry in progression_weeks:
            span = entry.get("countdown_span")
            if not isinstance(span, dict):
                continue
            try:
                start_day = int(span.get("start_day"))
                end_day = int(span.get("end_day"))
            except (TypeError, ValueError):
                continue
            upper, lower = max(start_day, end_day), min(start_day, end_day)
            if lower <= countdown_start <= upper:
                return entry
    for entry in progression_weeks:
        try:
            entry_week_index = int(entry.get("week_index") or 0)
        except (TypeError, ValueError):
            continue
        if entry_week_index == week_index:
            return entry
    return None


def _late_fight_stage_label_for_countdown(countdown_start: int | None) -> str | None:
    if countdown_start is None or not 0 <= countdown_start <= 21:
        return None
    if countdown_start >= 14:
        return "Bridge Compression Week"
    if countdown_start >= 8:
        return "Compressed Pre-Fight Week"
    if countdown_start == 7:
        return "Sharpness Week"
    if countdown_start >= 5:
        return "Sharpness & Freshness Window"
    if countdown_start >= 2:
        return "Sharpness Sessions"
    if countdown_start == 1:
        return "Primer Day"
    return "Fight-Day Protocol"


def _normalized_drill_name(value: Any) -> str:
    """A display name reduced to comparable form: lowercase alphanumeric words."""
    return " ".join(re.findall(r"[a-z0-9]+", _coerce_str(value).lower()))


def _brief_rehab_drill_ids(planning_brief: Any) -> dict[str, str | None]:
    """Map each rehab option's display name to its canonical bank id.

    Built from the plan's *own* candidate pools, so it only ever contains drills
    that were actually offered for this plan. A name claimed by two different
    bank ids maps to ``None``: that name cannot identify a drill here, and
    picking one of the two would invent the attribution.
    """
    pools = planning_brief.get("candidate_pools") if isinstance(planning_brief, dict) else None
    if not isinstance(pools, dict):
        return {}
    by_name: dict[str, str | None] = {}
    for pool in pools.values():
        if not isinstance(pool, dict):
            continue
        for slot in _as_list(pool.get("rehab_slots")):
            if not isinstance(slot, dict):
                continue
            options = [slot.get("selected"), *_as_list(slot.get("alternates"))]
            for option in options:
                if not isinstance(option, dict):
                    continue
                drill_id = _coerce_str(option.get("rehab_drill_id")).strip()
                name = _normalized_drill_name(option.get("name"))
                if not drill_id or not name:
                    continue
                if name in by_name and by_name[name] != drill_id:
                    by_name[name] = None
                else:
                    by_name.setdefault(name, drill_id)
    return by_name


def reconcile_rehab_drill_ids(
    structured_plan: Any,
    planning_brief: Any,
) -> Any:
    """Stamp the canonical rehab-bank id onto every rehab block it identifies.

    This is the one place display names are allowed anywhere near rehab
    identity, and it is deliberately here rather than at completion time. The
    markdown→JSON conversion never sees ``candidate_pools`` (the prompt drops
    them), so the model cannot carry the id itself; the server resolves it once,
    against this plan's own small option set, and stores the result. Everything
    downstream — the completion gate, the exposure event — reads the stored id
    and never looks at a name again.

    Anything that does not resolve to exactly one id is left ``None``. The
    completion gate then reports ``not_rehab_work`` and writes no evidence,
    which is the correct outcome: an unidentifiable drill is not proof that a
    particular tissue did particular work.
    """
    if not isinstance(structured_plan, dict) or not isinstance(planning_brief, dict):
        return structured_plan
    by_name = _brief_rehab_drill_ids(planning_brief)
    if not by_name:
        return structured_plan

    repaired = copy.deepcopy(structured_plan)
    changed = False
    for week in _as_list(repaired.get("weeks")):
        if not isinstance(week, dict):
            continue
        for day in _as_list(week.get("days")):
            if not isinstance(day, dict):
                continue
            for session in _as_list(day.get("sessions")):
                if not isinstance(session, dict):
                    continue
                for block in _as_list(session.get("blocks")):
                    if not isinstance(block, dict):
                        continue
                    if _coerce_str(block.get("block_type")).strip() != "rehab":
                        # Only rehab blocks carry rehab identity. A strength
                        # block is never an injury exposure, whatever it is
                        # named.
                        if block.get("rehab_drill_id") is not None:
                            block["rehab_drill_id"] = None
                            changed = True
                        continue
                    resolved = by_name.get(_normalized_drill_name(block.get("display_name")))
                    if block.get("rehab_drill_id") != resolved:
                        block["rehab_drill_id"] = resolved
                        changed = True
    return repaired if changed else structured_plan


def reconcile_late_fight_week_context(
    structured_plan: Any,
    planning_brief: Any,
) -> Any:
    """Fill missing late-fight week display context from deterministic Stage 2.

    Countdown-led Stage 2 text intentionally suppresses week scaffolding. The
    structured viewer still needs a short week goal and phase for its overview
    and week rail, so use ``week_by_week_progression`` as the source of truth.
    Existing non-empty values are preserved.
    """

    if not isinstance(structured_plan, dict) or not isinstance(planning_brief, dict):
        return structured_plan
    if isinstance(planning_brief.get("open_plan_spec"), dict):
        return structured_plan
    progression = planning_brief.get("week_by_week_progression")
    progression_weeks = [
        entry
        for entry in _as_list(progression.get("weeks") if isinstance(progression, dict) else None)
        if isinstance(entry, dict)
    ]
    late_fight_spec = planning_brief.get("late_fight_plan_spec")
    payload_mode = (
        _coerce_str(late_fight_spec.get("payload_mode")).strip()
        if isinstance(late_fight_spec, dict)
        else ""
    )
    has_late_fight_progression = any(
        _coerce_str(entry.get("stage_label")).strip() for entry in progression_weeks
    )
    plan_weeks = structured_plan.get("weeks")
    if (
        not isinstance(plan_weeks, list)
        or (
            not has_late_fight_progression
            and payload_mode in {"", "camp_payload"}
        )
    ):
        return structured_plan

    repaired = copy.deepcopy(structured_plan)
    changed = False
    fallback_phase = next(
        (
            _coerce_str(entry.get("phase")).strip().upper()
            for entry in progression_weeks
            if _coerce_str(entry.get("phase")).strip().upper() in _PHASE_VALUES
        ),
        "TAPER",
    )
    for position, week in enumerate(repaired.get("weeks") or [], start=1):
        if not isinstance(week, dict):
            continue
        distances = [
            distance
            for distance in (
                _countdown_distance(week.get("countdown_start")),
                *(
                    _countdown_distance(day.get("countdown_label"))
                    for day in _as_list(week.get("days"))
                    if isinstance(day, dict)
                ),
            )
            if distance is not None
        ]
        countdown_start = max(distances) if distances else None
        try:
            week_index = int(week.get("week_index") or position)
        except (TypeError, ValueError):
            week_index = position
        source = _progression_week_for_countdown(
            progression_weeks,
            week_index=week_index,
            countdown_start=countdown_start,
        )

        goal_missing = not _coerce_str(week.get("week_goal")).strip()
        stage_label = (
            _coerce_str(source.get("stage_label")).strip()
            if source is not None
            else ""
        ) or _late_fight_stage_label_for_countdown(countdown_start)
        if goal_missing and stage_label:
            week["week_goal"] = stage_label
            changed = True

        phase = (
            _coerce_str(source.get("phase")).strip().upper()
            if source is not None
            else fallback_phase
        )
        current_phase = _coerce_str(week.get("phase_label")).strip().upper()
        # A blank goal is the signature of the broken countdown conversion. In
        # that case a normalized GPP fallback is not authoritative, so replace it
        # with Stage 2's phase. Otherwise preserve every valid saved value.
        if phase in _PHASE_VALUES and (
            current_phase not in _PHASE_VALUES or (goal_missing and current_phase == "GPP")
        ):
            week["phase_label"] = phase
            changed = True
            for day in _as_list(week.get("days")):
                if not isinstance(day, dict):
                    continue
                day_phase = _coerce_str(day.get("phase_label")).strip().upper()
                if day_phase not in _PHASE_VALUES or day_phase == "GPP":
                    day["phase_label"] = phase

    return repaired if changed else structured_plan


# ---------------------------------------------------------------------------
# Bank → StructuredTrainingPlan adapters
#
# These map the project's existing bank conventions (see fightcamp/bank_schema.py
# and data/*_bank.json) into StructuredTrainingPlan value objects, so the
# structured renderer can consume bank-derived training metadata without a new
# naming scheme. They are conservative readers, not generators: they only
# translate fields already present on a bank entry and never invent content.
#
# Bank conventions translated here:
#   * strength/exercise: ``prescription`` string like "3x5 @ 75-85% 1RM"
#     (sets x reps @ %load ref), ``method``, ``category``, ``equipment`` (str).
#   * conditioning: ``work_sec``/``rest_sec`` (int seconds), ``total_minutes``
#     (minutes), ``rounds`` (int), ``rpe`` (int), ``intensity`` (str),
#     ``system`` (energy system), ``equipment`` (list).
# ---------------------------------------------------------------------------

_PRESCRIPTION_SETS_REPS_RE = re.compile(r"([0-9]+)\s*[xX×]\s*([0-9]+(?:\s*[-–]\s*[0-9]+)?)")


def parse_bank_prescription(prescription: Any) -> dict[str, Any]:
    """Parse a bank ``prescription`` string into structured block fields.

    "3x5 @ 75-85% 1RM" → {"sets": 3, "reps": "5", "load": {percentage 85, ref 1RM}}.
    Returns only the fields it can read; an unparseable input yields ``{}``.
    """

    if not isinstance(prescription, str) or not prescription.strip():
        return {}
    text = prescription.strip()
    out: dict[str, Any] = {}
    sets_reps = _PRESCRIPTION_SETS_REPS_RE.search(text)
    if sets_reps:
        out["sets"] = int(sets_reps.group(1))
        out["reps"] = sets_reps.group(2).replace(" ", "")
    load = _normalize_load(text)
    if load is not None:
        out["load"] = load
    return out


def _slug(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return cleaned or fallback


def bank_strength_to_block(entry: dict[str, Any]) -> dict[str, Any]:
    """Adapter: an exercise/strength bank entry → a SessionBlock-shaped dict.

    Reads name/method/category/prescription/notes; the result is passed through
    :func:`_normalize_block` so it always satisfies the schema.
    """

    entry = entry if isinstance(entry, dict) else {}
    name = _coerce_str(entry.get("name"))
    block: dict[str, Any] = {
        "block_id": _slug(name, "block"),
        "block_type": _enum(entry.get("method"), _BLOCK_TYPE_VALUES, "strength", _BLOCK_TYPE_ALIASES),
        "display_name": name,
        "exercise_key": block_exercise_key_for_name(name),
    }
    category = _coerce_str(entry.get("category"))
    if category:
        block["category"] = category
    notes = _coerce_str(entry.get("notes"))
    if notes:
        block["purpose"] = notes
    impact = _coerce_str(entry.get("impact_cost"))
    if impact:
        block["impact_level"] = impact
    block.update(parse_bank_prescription(entry.get("prescription")))
    return _normalize_block(block)


def bank_conditioning_to_block(entry: dict[str, Any]) -> dict[str, Any]:
    """Adapter: a conditioning bank entry → a SessionBlock-shaped dict.

    Maps work_sec/rest_sec → work/rest (seconds), total_minutes → duration
    (minutes), rounds, rpe → effort (RPE), system → energy_system, and intensity.
    The result is passed through :func:`_normalize_block`.
    """

    entry = entry if isinstance(entry, dict) else {}
    name = _coerce_str(entry.get("name"))
    block: dict[str, Any] = {
        "block_id": _slug(name, "conditioning"),
        "block_type": "conditioning",
        "display_name": name,
        "exercise_key": block_exercise_key_for_name(name),
    }
    if entry.get("work_sec") is not None:
        block["work"] = entry.get("work_sec")
    if entry.get("rest_sec") is not None:
        block["rest"] = entry.get("rest_sec")
    if entry.get("total_minutes") is not None:
        block["duration"] = entry.get("total_minutes")
    if isinstance(entry.get("rounds"), int) and not isinstance(entry.get("rounds"), bool):
        block["rounds"] = entry.get("rounds")
    rpe = entry.get("rpe")
    if isinstance(rpe, (int, float)) and not isinstance(rpe, bool):
        block["effort"] = {"method": "RPE", "value": rpe, "scale": "1-10"}
    system = _coerce_str(entry.get("system"))
    if system:
        block["energy_system"] = system
    intensity = _coerce_str(entry.get("intensity"))
    if intensity:
        block["intensity"] = intensity
    notes = _coerce_str(entry.get("notes"))
    if notes:
        block["purpose"] = notes
    impact = _coerce_str(entry.get("impact_cost"))
    if impact:
        block["impact_level"] = impact
    return _normalize_block(block)


def _conversion_support(computed_support: Any) -> Any:
    """computed_support as the conversion sees it: no nutrition guidance.

    Nutrition is Stage 1-only, so macros, hydration, fuel timing and the
    coach-gated dosing never reach Stage 2. Only each phase's weight-cut risk
    band survives, because the weight-cut severity gate is a safety rule.
    """
    if not isinstance(computed_support, dict):
        return computed_support
    support = {
        key: value
        for key, value in computed_support.items()
        if key not in {"nutrition", "athlete_facing_note"}
    }
    by_phase = (computed_support.get("nutrition") or {}).get("by_phase") or {}
    weight_cut_by_phase = {
        phase: entry["weight_cut"]
        for phase, entry in by_phase.items()
        if isinstance(entry, dict) and isinstance(entry.get("weight_cut"), dict)
    }
    if weight_cut_by_phase:
        support["weight_cut"] = {"by_phase": weight_cut_by_phase}
    return support


# Under-18 cards: the conversion model never sees the refusal note or any
# weight-cut vocabulary, so it has nothing to reword into wording the safety
# audit would (rightly) block. The note is added back verbatim, in code, once
# the card has passed the audit.
_MINOR_CONVERSION_RULE = (
    "UNDER-18 ATHLETE: never mention weight cuts, cutting weight, making weight, "
    "dehydration, rehydration, water cuts or loading, sauna, sweat suits, "
    "diuretics or refeeds anywhere in the JSON. Set nutrition.weight_cut_warning "
    "to null. Weight-cut guidance is added separately by the server."
)


_MINOR_CONVERSION_DROPPED_KEYS = frozenset({"coach_gated", "athlete_facing_note", "weight_cut"})


def _minor_conversion_markdown(plan_markdown: str) -> str:
    """Plan markdown with the under-18 refusal note removed for conversion."""
    return "\n".join(
        line for line in plan_markdown.splitlines() if MINOR_WEIGHT_CUT_NOTE not in line
    )


def _minor_conversion_support(node: Any) -> Any:
    """computed_support without the refusal note or any cut sections.

    ``coach_gated`` is never athlete-facing and holds no cut for a minor (Stage 1
    computes none), ``athlete_facing_note`` only exists to warn about it, and
    ``weight_cut`` only records that the cut is blocked — none of it is anything
    the conversion needs, and all of it is cut wording it could echo.
    """
    if isinstance(node, dict):
        return {
            key: _minor_conversion_support(value)
            for key, value in node.items()
            if key not in _MINOR_CONVERSION_DROPPED_KEYS
            and value != MINOR_WEIGHT_CUT_NOTE
        }
    if isinstance(node, list):
        return [_minor_conversion_support(item) for item in node]
    return node


def _with_minor_weight_cut_note(plan_dict: dict[str, Any]) -> dict[str, Any]:
    """Add the verbatim refusal note to a minor card's nutrition summary and notes.

    The plan page no longer shows nutrition, so the note also goes into
    plan_notes, which the header renders.
    """
    nutrition = plan_dict.get("nutrition")
    if isinstance(nutrition, dict):
        summary = str(nutrition.get("summary") or "").strip()
        if MINOR_WEIGHT_CUT_NOTE not in summary:
            note_summary = f"{summary} {MINOR_WEIGHT_CUT_NOTE}".strip()
            plan_dict = {**plan_dict, "nutrition": {**nutrition, "summary": note_summary}}
    notes = plan_dict.get("plan_notes")
    notes = list(notes) if isinstance(notes, list) else []
    if not any(
        isinstance(note, dict) and MINOR_WEIGHT_CUT_NOTE in str(note.get("text") or "")
        for note in notes
    ):
        notes.append({"category": "weight_cut", "label": None, "text": MINOR_WEIGHT_CUT_NOTE})
        plan_dict = {**plan_dict, "plan_notes": notes}
    return plan_dict


def _open_plan_spec_from_brief(planning_brief: Any) -> dict[str, Any] | None:
    if not isinstance(planning_brief, dict):
        return None
    spec = planning_brief.get("open_plan_spec")
    if not isinstance(spec, dict) or spec.get("plan_type") != _OPEN_PLAN_TYPE:
        return None
    return spec


def _open_plan_training_weekdays(spec: dict[str, Any]) -> list[str]:
    template = spec.get("weekly_template")
    template = template if isinstance(template, dict) else {}
    raw_days = template.get("training_days")
    if not isinstance(raw_days, (list, tuple, set)):
        return []
    normalized = {
        day
        for value in raw_days
        if (day := _normalize_weekday(value))
    }
    return [day for day in _OPEN_PLAN_WEEKDAYS if day in normalized]


def _open_plan_contract_errors(
    plan_dict: dict[str, Any], planning_brief: Any
) -> list[str]:
    """Validate the non-schema invariants that make an open plan schedulable.

    The generic structured schema deliberately supports dated fight camps and
    renewable plans.  It cannot express conditional rules such as "open plans
    must use weekday identities and no countdown".  Enforce those rules here so
    a schema-valid but unschedulable card triggers the existing single repair
    attempt instead of being published as an empty timeline.
    """

    spec = _open_plan_spec_from_brief(planning_brief)
    if spec is None:
        return []

    errors: list[str] = []
    expected_weekdays = _open_plan_training_weekdays(spec)
    if not expected_weekdays:
        return ["open_plan_contract: weekly_template.training_days is missing"]

    metadata = plan_dict.get("plan_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    if metadata.get("plan_type") != _OPEN_PLAN_TYPE:
        errors.append(
            f'open_plan_contract: plan_metadata.plan_type must be "{_OPEN_PLAN_TYPE}"'
        )

    event_context = plan_dict.get("event_context")
    event_context = event_context if isinstance(event_context, dict) else {}
    if str(event_context.get("event_type") or "none").strip().lower() != "none":
        errors.append('open_plan_contract: event_context.event_type must be "none"')
    if any(
        str(event_context.get(key) or "").strip()
        for key in ("fight_date", "match_date", "weigh_in_date")
    ):
        errors.append("open_plan_contract: renewable plans cannot carry event dates")
    if plan_dict.get("countdown_labels"):
        errors.append("open_plan_contract: countdown_labels must be empty")

    weeks = plan_dict.get("weeks")
    if not isinstance(weeks, list) or len(weeks) not in {1, 4}:
        errors.append("open_plan_contract: weeks must contain one reusable template or four weeks")
        return errors

    for position, week in enumerate(weeks, start=1):
        if not isinstance(week, dict):
            errors.append(f"open_plan_contract: week {position} must be an object")
            continue
        if int(week.get("week_index") or 0) != position:
            errors.append(f"open_plan_contract: week {position} has the wrong week_index")
        if any(
            str(week.get(key) or "").strip()
            for key in ("start_date", "end_date", "countdown_start", "countdown_end")
        ):
            errors.append(f"open_plan_contract: week {position} must remain undated")

        days = week.get("days")
        if not isinstance(days, list) or len(days) != len(expected_weekdays):
            errors.append(
                f"open_plan_contract: week {position} days must match "
                f"{expected_weekdays!r} exactly"
            )
            continue

        actual_weekdays = [
            _normalize_weekday(day.get("weekday"))
            if isinstance(day, dict)
            else None
            for day in days
        ]
        if actual_weekdays != expected_weekdays:
            errors.append(
                f"open_plan_contract: week {position} weekday order must be "
                f"{expected_weekdays!r}, got {actual_weekdays!r}"
            )
        for day_position, day in enumerate(days, start=1):
            if not isinstance(day, dict):
                continue
            if str(day.get("date") or "").strip() or str(day.get("countdown_label") or "").strip():
                errors.append(
                    f"open_plan_contract: week {position} day {day_position} must remain undated"
                )

    return errors


_TRAINING_NODE_ERROR_RE = re.compile(
    r"^weeks\.(\d+)\.days\.(\d+)"
    r"(?:\.sessions\.(\d+)(?:\.blocks\.(\d+))?)?(?:\.|:)"
)
_DAY_FIELD_ERROR_RE = re.compile(r"^weeks\.\d+\.days\.\d+\.([A-Za-z_]+)(?:\.([A-Za-z_]+))?")
# Session types that carry planner-owned strength / conditioning work.
_PLANNER_SC_SESSION_TYPES = frozenset({"strength_power", "conditioning"})


def _optional_field_default(model: type[Any], name: str | None) -> tuple[bool, Any]:
    """``(True, default)`` when ``name`` is an optional field of ``model``."""
    info = model.model_fields.get(name or "")
    if info is None or info.is_required():
        return False, None
    return True, info.get_default(call_default_factory=True)


def _clear_invalid_day_metadata(day: dict[str, Any], error: str) -> str | None:
    """Reset the optional day/today-card field ``error`` names; its path, or ``None``.

    Day metadata (weekday, planner microdose, warnings copy) is presentation:
    an invalid value there is a reason to drop that value, never the day's
    sessions with it. Required fields are left for whole-day containment.
    """
    match = _DAY_FIELD_ERROR_RE.match(error)
    if not match:
        return None
    field_name, child = match.groups()
    if field_name == "today_card":
        card = day.get("today_card")
        clearable, default = _optional_field_default(TodayCard, child)
        if not clearable or not isinstance(card, dict):
            return None
        card[child] = default
        return f"today_card.{child}"
    if field_name == "sessions":
        return None
    clearable, default = _optional_field_default(Day, field_name)
    if not clearable:
        return None
    day[field_name] = default
    return field_name


def _planner_sc_roles_by_dday(planning_brief: Any) -> dict[int, list[dict[str, Any]]]:
    """Stage 1's exercise-backed (strength / conditioning) roles per D-day."""
    role_map = planning_brief.get("weekly_role_map") if isinstance(planning_brief, dict) else None
    roles_by_dday: dict[int, list[dict[str, Any]]] = {}
    if not isinstance(role_map, dict):
        return roles_by_dday
    for week in role_map.get("weeks") or []:
        if not isinstance(week, dict):
            continue
        for role in week.get("session_roles") or []:
            if not isinstance(role, dict) or not role.get("selected_exercise_assignments"):
                continue
            if str(role.get("role_key") or "").strip() in ROLES_OWNED_ELSEWHERE:
                continue
            d_day = spine_role_dday(week, role)
            if d_day is not None:
                roles_by_dday.setdefault(d_day, []).append(role)
    return roles_by_dday


def _planner_owned_session_indexes(
    day: dict[str, Any],
    roles_by_dday: dict[int, list[dict[str, Any]]],
    fight_date: Any,
) -> set[int]:
    """Indexes of the day's sessions that carry a Stage 1 S&C role.

    A session is planner-owned when it represents one of the day's roles under
    the shared identity rule, or when it is strength/conditioning work on a day
    Stage 1 scheduled strength/conditioning for (the converter often retitles
    "Primary strength" as plain "Strength").
    """
    if not roles_by_dday:
        return set()
    d_day = spine_parse_dday(day.get("countdown_label"))
    if d_day is None and fight_date is not None:
        d_day = spine_effective_dday(day, fight_date)
    roles = roles_by_dday.get(d_day) if d_day is not None else None
    sessions = day.get("sessions")
    if not roles or not isinstance(sessions, list):
        return set()
    owned = set(match_sessions_to_roles(sessions, roles, d_day))
    owned.update(
        index
        for index, session in enumerate(sessions)
        if isinstance(session, dict)
        and str(session.get("session_type") or "") in _PLANNER_SC_SESSION_TYPES
    )
    return owned


def _role_session_type(role: dict[str, Any]) -> str:
    # Deferred: the deterministic fallback imports the spine, which this module
    # also imports; keep the import graph acyclic at load time.
    from .structured_plan_deterministic_fallback import _session_type

    return _session_type(role)


def planner_sc_coverage_losses(
    plan_dict: Any, planning_brief: Any, raw_markdown: str
) -> list[str]:
    """Stage 1 S&C roles the source rendered but the structured card lost.

    A role counts when Stage 1 scheduled it with selected exercises, it is
    strength/conditioning work, and the source text names one of its exercises
    in that D-day's section (what Stage 2 actually rendered is what conversion
    must preserve; a role the text never rendered is a separate Stage 2
    underfill, not a conversion loss).

    It is represented when one of its own exercises is on that day's card (by
    the server-stamped ``exercise_key`` or the exact exercise name), whatever
    session holds it: the converter may fold two roles into one session
    ("Strength" carrying the fight-pace conditioning too), or title and type a
    primer after its exercise. Failing that, a session matching the role under
    the shared identity rule, or an otherwise-unclaimed session of the same
    session type, also represents it.
    """
    if isinstance(plan_dict, dict) and isinstance(planning_brief, dict):
        programmed = (planning_brief.get("open_plan_spec") or {}).get("weekly_session_plan") or {}
        if programmed.get("authority") == "deterministic_open_development_block":
            losses = []
            expected_weeks = {week.get("week"): week for week in programmed.get("weeks") or []}
            for position, week in enumerate(plan_dict.get("weeks") or [], 1):
                expected = expected_weeks.get(week.get("week_index") or position, {})
                for session in expected.get("sessions") or []:
                    weekday = _normalize_weekday(session.get("weekday"))
                    actual_names = set()
                    for day in week.get("days") or []:
                        if _normalize_weekday(day.get("weekday")) == weekday:
                            for actual in day.get("sessions") or []:
                                for block in actual.get("blocks") or []:
                                    actual_names.update(exercise_name_forms(block.get("display_name")))
                    for exercise in session.get("exercises") or []:
                        if not actual_names.intersection(exercise_name_forms(exercise.get("name"))):
                            losses.append(f"planner_coverage: open week {position} {weekday} lost {exercise.get('name')}")
            return losses
    roles_by_dday = _planner_sc_roles_by_dday(planning_brief)
    if not isinstance(plan_dict, dict) or not isinstance(planning_brief, dict) or not raw_markdown.strip():
        return []
    sections = _source_day_section_lines(raw_markdown)
    fight_date = parse_fight_date(spine_resolve_fight_date(planning_brief))
    sessions_by_dday: dict[int, list[Any]] = {}
    exercises_by_dday: dict[int, set[str]] = {}
    for week in plan_dict.get("weeks") or []:
        if not isinstance(week, dict):
            continue
        for day in week.get("days") or []:
            if not isinstance(day, dict):
                continue
            d_day = spine_parse_dday(day.get("countdown_label"))
            if d_day is None and fight_date is not None:
                d_day = spine_effective_dday(day, fight_date)
            sessions = day.get("sessions")
            if d_day is not None and isinstance(sessions, list):
                sessions_by_dday.setdefault(d_day, []).extend(sessions)
                identities = exercises_by_dday.setdefault(d_day, set())
                for session in sessions:
                    for block in session.get("blocks") or [] if isinstance(session, dict) else []:
                        if not isinstance(block, dict):
                            continue
                        key = normalize_exercise_key(block.get("exercise_key"))
                        if key:
                            identities.add(key)
                        identities.update(exercise_name_forms(block.get("display_name")))

    losses: list[str] = []
    for d_day in sorted(roles_by_dday, reverse=True):
        source = " ".join(sections.get(d_day, [])).casefold()
        required = []
        for role in roles_by_dday[d_day]:
            if _role_session_type(role) not in _PLANNER_SC_SESSION_TYPES:
                continue
            names = [
                str(assignment.get("name") or "").strip().casefold()
                for assignment in role.get("selected_exercise_assignments") or []
                if isinstance(assignment, dict)
            ]
            if any(name and name in source for name in names):
                required.append(role)
        on_card = exercises_by_dday.get(d_day, set())
        # Role/type coverage is insufficient: one retained exercise cannot stand
        # in for the other closed assignments on the same session card.
        for role in required:
            missing = [str(assignment.get("name") or "")
                for assignment in role.get("selected_exercise_assignments") or []
                if isinstance(assignment, dict) and str(assignment.get("name") or "").casefold() in source
                and (normalize_exercise_key(assignment.get("exercise_key"))
                     or normalize_exercise_key(assignment.get("name"))) not in on_card]
            if missing:
                losses.append(f"planner_coverage: D-{d_day} {str(role.get('role_key') or 'role')!r} "
                              f"selected exercises {missing!r} are missing from the structured card")
        required = [
            role
            for role in required
            if not any(
                (normalize_exercise_key(assignment.get("exercise_key")) or normalize_exercise_key(assignment.get("name")))
                in on_card
                for assignment in role.get("selected_exercise_assignments") or []
                if isinstance(assignment, dict)
            )
        ]
        if not required:
            continue
        sessions = sessions_by_dday.get(d_day, [])
        matched = match_sessions_to_roles(sessions, required, d_day)
        claimed = set(matched)
        represented = {id(role) for role in matched.values()}
        for role in required:
            if id(role) in represented:
                continue
            session_type = _role_session_type(role)
            index = next(
                (
                    i
                    for i, session in enumerate(sessions)
                    if i not in claimed
                    and isinstance(session, dict)
                    and session.get("session_type") == session_type
                ),
                None,
            )
            if index is not None:
                claimed.add(index)
                continue
            losses.append(
                f"planner_coverage: D-{d_day} {str(role.get('role_key') or 'role')!r} "
                f"({session_type}) is rendered in the source but missing from the "
                "structured card"
            )
    # Open, instruction-only roles are real scheduled work too. Exercise-only
    # coverage missed an approved D-3 freshness session because it had no bank
    # assignments. Require its exact source heading and its own card identity.
    from .structured_plan_deterministic_fallback import _source_instruction_for_role
    for week in (planning_brief.get("weekly_role_map") or {}).get("weeks") or []:
        if not isinstance(week, dict):
            continue
        for role in week.get("session_roles") or []:
            if (not isinstance(role, dict) or role.get("selected_exercise_assignments")
                    or role.get("role_key") in ROLES_OWNED_ELSEWHERE):
                continue
            d_day = spine_role_dday(week, role)
            if d_day is None or not _source_instruction_for_role(role, sections.get(d_day)):
                continue
            if not match_sessions_to_roles(sessions_by_dday.get(d_day, []), [role], d_day):
                losses.append(f"planner_coverage: D-{d_day} {role.get('role_key')!r} "
                              "instruction-only session is missing from the structured card")
    return losses


def _salvage_invalid_training_nodes(
    candidate: Any,
    errors: list[str],
    *,
    raw_markdown: str,
    planning_brief: Any = None,
) -> tuple[Any, list[str], list[str]]:
    """Contain the smallest invalid training nodes while retaining valid siblings.

    Returns ``(salvaged, warnings, refusals)``. The raw source must be present
    before containment is allowed. It remains embedded in the saved plan, so the
    omitted card/day is never the only copy of the prescription. Root metadata,
    calendar/week structure, safety rules, nutrition and other plan-wide
    failures remain fatal; this helper handles only validation locations inside
    days/sessions/blocks.

    Containment is smallest-first: an invalid optional day field is cleared, a
    block is dropped from its session, a session from its day, and a day only
    when a required day field cannot be repaired. Each warning names the
    validation path and message that caused it.

    Structured conversion must never cost the athlete Stage 1 strength or
    conditioning work. When containment would remove a session (or a day
    holding one) that carries a planner-owned S&C role, salvage is refused and
    ``refusals`` says why: the card then fails validation and the existing
    repair / deterministic Stage 1 fallback builds it instead.
    """
    if not raw_markdown.strip() or not isinstance(candidate, dict):
        return candidate, [], []

    block_targets: dict[tuple[int, int, int, int], list[str]] = {}
    session_targets: dict[tuple[int, int, int], list[str]] = {}
    day_errors: dict[tuple[int, int], list[str]] = {}
    for error in errors:
        match = _TRAINING_NODE_ERROR_RE.match(error)
        if not match:
            continue
        week_index, day_index, session_index, block_index = (
            int(value) if value is not None else None for value in match.groups()
        )
        if session_index is None:
            day_errors.setdefault((week_index, day_index), []).append(error)
        elif block_index is None:
            session_targets.setdefault((week_index, day_index, session_index), []).append(error)
        else:
            block_targets.setdefault(
                (week_index, day_index, session_index, block_index), []
            ).append(error)

    if not (block_targets or session_targets or day_errors):
        return candidate, [], []

    salvaged = copy.deepcopy(candidate)
    salvaged["raw_markdown_fallback"] = raw_markdown
    warnings: list[str] = []
    refusals: list[str] = []
    weeks = salvaged.get("weeks")
    if not isinstance(weeks, list):
        return candidate, [], []

    def _day_at(week_index: int, day_index: int) -> dict[str, Any] | None:
        if week_index < 0 or week_index >= len(weeks) or not isinstance(weeks[week_index], dict):
            return None
        days = weeks[week_index].get("days")
        if not isinstance(days, list) or day_index < 0 or day_index >= len(days):
            return None
        return days[day_index] if isinstance(days[day_index], dict) else None

    def _day_label(day: Any) -> str:
        if not isinstance(day, dict):
            return "day"
        return str(day.get("countdown_label") or day.get("date") or "day")

    def _reason(node_errors: list[str]) -> str:
        return "; ".join(node_errors[:3]) + (" ..." if len(node_errors) > 3 else "")

    roles_by_dday = _planner_sc_roles_by_dday(planning_brief)
    fight_date = None
    if roles_by_dday and isinstance(planning_brief, dict):
        fight_date = parse_fight_date(spine_resolve_fight_date(planning_brief))
    # Ownership is judged on the day as the converter wrote it, before any
    # sibling removal shifts indexes.
    owned_cache: dict[tuple[int, int], set[int]] = {}

    def _owned(week_index: int, day_index: int) -> set[int]:
        key = (week_index, day_index)
        if key not in owned_cache:
            day = _day_at(week_index, day_index)
            owned_cache[key] = (
                _planner_owned_session_indexes(day, roles_by_dday, fight_date) if day else set()
            )
        return owned_cache[key]

    # Day metadata first: clear what is optional, keep the day.
    day_targets: dict[tuple[int, int], list[str]] = {}
    for (week_index, day_index), node_errors in day_errors.items():
        day = _day_at(week_index, day_index)
        if day is None:
            continue
        unrepaired = []
        for error in node_errors:
            cleared = _clear_invalid_day_metadata(day, error)
            if cleared is None:
                unrepaired.append(error)
            else:
                warnings.append(
                    f"schema_salvage: cleared invalid day field {cleared!r} "
                    f"on {_day_label(day)!r} ({error})"
                )
        if unrepaired:
            day_targets[(week_index, day_index)] = unrepaired
            _owned(week_index, day_index)

    for week_index, day_index, session_index in session_targets:
        if (week_index, day_index) in day_targets:
            continue
        if session_index in _owned(week_index, day_index):
            day = _day_at(week_index, day_index)
            sessions = day.get("sessions") if day else None
            session = sessions[session_index] if isinstance(sessions, list) and session_index < len(sessions) else None
            title = str(session.get("title") or "session") if isinstance(session, dict) else "session"
            refusals.append(
                f"schema_salvage_refused: session {title!r} on {_day_label(day)!r} carries "
                f"planner-owned strength/conditioning work "
                f"({_reason(session_targets[(week_index, day_index, session_index)])})"
            )
    for (week_index, day_index), node_errors in day_targets.items():
        if _owned(week_index, day_index):
            refusals.append(
                f"schema_salvage_refused: day {_day_label(_day_at(week_index, day_index))!r} "
                f"carries planner-owned strength/conditioning work ({_reason(node_errors)})"
            )
    if refusals:
        return candidate, [], refusals

    # Deepest nodes first. Parent targets then remove any still-invalid parent;
    # descending indexes keep sibling positions stable during each removal.
    for key in sorted(block_targets, reverse=True):
        week_index, day_index, session_index, block_index = key
        if (week_index, day_index) in day_targets or (
            week_index,
            day_index,
            session_index,
        ) in session_targets:
            continue
        day = _day_at(week_index, day_index)
        sessions = day.get("sessions") if day else None
        if not isinstance(sessions, list) or not (0 <= session_index < len(sessions)):
            continue
        session = sessions[session_index]
        blocks = session.get("blocks") if isinstance(session, dict) else None
        if not isinstance(blocks, list) or not (0 <= block_index < len(blocks)):
            continue
        block = blocks.pop(block_index)
        name = str(block.get("display_name") or "block") if isinstance(block, dict) else "block"
        warnings.append(
            f"schema_salvage: omitted invalid block {name!r} ({_reason(block_targets[key])})"
        )

    for key in sorted(session_targets, reverse=True):
        week_index, day_index, session_index = key
        if (week_index, day_index) in day_targets:
            continue
        day = _day_at(week_index, day_index)
        sessions = day.get("sessions") if day else None
        if not isinstance(sessions, list) or not (0 <= session_index < len(sessions)):
            continue
        session = sessions.pop(session_index)
        title = str(session.get("title") or "session") if isinstance(session, dict) else "session"
        warnings.append(
            f"schema_salvage: omitted invalid session {title!r} ({_reason(session_targets[key])})"
        )

    for key in sorted(day_targets, reverse=True):
        week_index, day_index = key
        if not (0 <= week_index < len(weeks)) or not isinstance(weeks[week_index], dict):
            continue
        days = weeks[week_index].get("days")
        if not isinstance(days, list) or not (0 <= day_index < len(days)):
            continue
        day = days.pop(day_index)
        warnings.append(
            f"schema_salvage: omitted invalid day {_day_label(day)!r} ({_reason(day_targets[key])})"
        )

    return salvaged, warnings, []


def build_structured_plan_outcome(
    raw_data: Any,
    *,
    raw_markdown: str = "",
    repair_fn: Callable[[Any, list[str]], Any] | None = None,
    computed_support: dict[str, Any] | None = None,
    planning_brief: dict[str, Any] | None = None,
    allow_salvage: bool = True,
) -> StructuredPlanOutcome:
    """Validate a candidate structured payload into a persistable outcome.

    Flow (matching the task contract):

    1. ``raw_data is None`` → ``not_attempted`` (structured generation skipped).
    2. Strip banned biometric keys, then validate.
    3. Valid → ``valid``.
    4. Invalid and no ``repair_fn`` → ``invalid_fallback_used``.
    5. Invalid with ``repair_fn`` → run exactly one repair retry via
       :func:`repair_structured_plan_once`; success → ``repair_attempted_valid``,
       otherwise ``invalid_fallback_used``.

    A schema-valid card is then safety-audited: blocking findings
    (LEAKAGE/CONFLICT/AUDIT_ERROR) yield ``blocked_by_safety_audit`` with the
    card discarded, so a leaking or contradictory card can never be persisted or
    trusted for publication. DUPLICATE findings stay advisory warnings.

    Faithfulness findings remain blocking except ``PRESCRIPTION`` omissions.
    The structured schema can only store scalar measured values, so an exact raw
    range such as ``Rest 2-3 minutes`` may be intentionally absent from the
    converted object. The persisted plan embeds the verbatim source and the
    renderer deterministically restores those explicit ranges; rejecting the
    entire card would replace a recoverable field with a lossy fallback UI.

    Salvage is the last step before giving up: when the only remaining blockers
    are content the source does not back, that content is pruned
    (:func:`prune_unfaithful_content`) and the rest of the card ships, with each
    removal recorded as a ``salvaged:`` warning. A caller that will spend a model
    repair next passes ``allow_salvage=False`` so a complete repaired card is
    preferred to a pruned first pass.

    Never raises: a malformed payload degrades to ``invalid_fallback_used`` so the
    raw ``plan_text`` flow keeps working.
    """

    def _split_faithfulness(
        findings: list[str],
    ) -> tuple[list[str], list[str]]:
        blocking: list[str] = []
        advisory: list[str] = []
        for issue in findings:
            target = advisory if issue.startswith(f"{PRESCRIPTION}:") else blocking
            target.append(f"faithfulness: {issue}")
        return blocking, advisory

    def _audited_outcome(
        status: StructuredPlanStatus,
        plan_dict: dict[str, Any],
        schema_version: str | None,
        faithfulness_warnings: list[str] | None = None,
    ) -> StructuredPlanOutcome:
        # Every card that can be published passes through here, after the
        # locked merge and any salvage pruning, so its day membership is final:
        # order each day's sessions and each session's blocks for execution.
        plan_dict = regroup_split_sessions(plan_dict, raw_markdown)
        # Identity is the planner's: a block tied to a Stage 1 exercise by its
        # server-stamped key takes that exercise's name, and its session the
        # role's label. After faithfulness (which judges what the model wrote)
        # and regrouping (which matches the model's names to the text).
        plan_dict = reconcile_canonical_names(plan_dict, planning_brief)
        plan_dict = sequence_structured_plan(plan_dict, planning_brief)
        plan_dict = _with_athlete_goal_notes(plan_dict, planning_brief)
        # Conversion cannot turn a quality cue or an ungrounded number into a
        # programming instruction. Use the same deterministic recovery as reads.
        from .structured_plan_coaching import recover_selected_coaching
        plan_dict = recover_selected_coaching(plan_dict, planning_brief or {}, source_text=raw_markdown)
        # Hard invariant for every candidate that can ship (first pass,
        # schema-salvaged, faithfulness-pruned or model-repaired): conversion
        # never costs Stage 1 strength/conditioning the source rendered. A card
        # that lost one falls back to the deterministic Stage 1 card instead.
        coverage_losses = planner_sc_coverage_losses(plan_dict, planning_brief, raw_markdown)
        if coverage_losses:
            return StructuredPlanOutcome(
                status="invalid_fallback_used",
                errors=coverage_losses,
                warnings=list(faithfulness_warnings or []),
            )
        blocking, advisory = split_findings(audit_structured_plan(plan_dict, computed_support))
        warnings = list(dict.fromkeys([*(faithfulness_warnings or []), *advisory]))
        if blocking:
            return StructuredPlanOutcome(
                status="blocked_by_safety_audit",
                errors=blocking,
                warnings=warnings,
            )
        if is_minor_support(computed_support):
            plan_dict = _with_minor_weight_cut_note(plan_dict)
        # Non-limiting injuries are removed from planning; their note and
        # re-injury stop rule are added back verbatim, in code, after the audit
        # (the conversion prompt never carries them).
        plan_dict = merge_injury_awareness(
            plan_dict,
            computed_support.get("injury_awareness") if isinstance(computed_support, dict) else None,
        )
        return StructuredPlanOutcome(
            status=status,
            structured_plan=plan_dict,
            schema_version=schema_version,
            warnings=warnings,
        )

    def _salvaged(
        candidates: list[tuple[StructuredPlanStatus, dict[str, Any], str | None]],
        failed: StructuredPlanOutcome,
    ) -> StructuredPlanOutcome:
        if not allow_salvage:
            return failed
        for status, candidate, schema_version in candidates:
            pruned, removed = prune_unfaithful_content(candidate, raw_markdown, planning_brief)
            if pruned is None or not removed:
                continue
            if not safe_parse_structured_plan(pruned, raw_markdown=raw_markdown or None).ok:
                continue
            errors, advisory = _split_faithfulness(
                check_structured_faithfulness(pruned, raw_markdown, planning_brief)
            )
            errors.extend(_open_plan_contract_errors(pruned, planning_brief))
            if errors:
                continue
            return _audited_outcome(
                status,
                pruned,
                schema_version,
                [*(f"salvaged: {item}" for item in removed), *advisory],
            )
        return failed

    if raw_data is None:
        return StructuredPlanOutcome(status="not_attempted")

    first_candidate: list[tuple[StructuredPlanStatus, dict[str, Any], str | None]] = []
    cleaned = _strip_and_normalize(
        reconcile_exercise_keys(
            reconcile_rehab_drill_ids(
                reconcile_late_fight_week_context(raw_data, planning_brief),
                planning_brief,
            ),
            planning_brief,
        )
    )

    first = safe_parse_structured_plan(cleaned, raw_markdown=raw_markdown or None)
    salvage_warnings: list[str] = []
    salvage_refusals: list[str] = []
    if not first.ok:
        salvaged, candidate_warnings, salvage_refusals = _salvage_invalid_training_nodes(
            cleaned,
            list(first.errors),
            raw_markdown=raw_markdown,
            planning_brief=planning_brief,
        )
        if candidate_warnings:
            salvaged_parse = safe_parse_structured_plan(
                salvaged, raw_markdown=raw_markdown or None
            )
            if salvaged_parse.ok:
                cleaned = salvaged
                first = salvaged_parse
                salvage_warnings = candidate_warnings
    first_errors = [*first.errors, *salvage_refusals]
    if first.ok and first.plan is not None:
        plan_dict = first.plan.model_dump(mode="json")
        plan_dict = _merge_locked_content(plan_dict, planning_brief, raw_markdown)
        unfaithful = check_structured_faithfulness(plan_dict, raw_markdown, planning_brief)
        first_errors, first_warnings = _split_faithfulness(unfaithful)
        first_warnings = [*salvage_warnings, *first_warnings]
        first_errors.extend(_open_plan_contract_errors(plan_dict, planning_brief))
        if not first_errors:
            return _audited_outcome(
                "valid", plan_dict, first.plan.schema_version, first_warnings
            )
        first_candidate = [("valid", plan_dict, first.plan.schema_version)]

    if repair_fn is None:
        return _salvaged(
            first_candidate,
            StructuredPlanOutcome(status="invalid_fallback_used", errors=first_errors),
        )

    # Strip biometric keys + conservatively normalize anything the repair attempt
    # introduces too, mirroring the first-pass treatment.
    def _clean_repair(data: Any, errors: list[str]) -> Any:
        return _strip_and_normalize(repair_fn(data, errors))

    if first.ok and first.plan is not None:
        # The generic schema can be valid while the open-plan scheduling contract
        # is not.  In that case repair_structured_plan_once would return the
        # original unchanged object without calling repair_fn, so invoke the same
        # single repair explicitly with the contract/faithfulness errors.
        try:
            repaired_data = _clean_repair(cleaned, first_errors)
            repaired = safe_parse_structured_plan(
                repaired_data, raw_markdown=raw_markdown or None
            )
        except Exception as exc:  # noqa: BLE001 - raw fallback must always survive
            return _salvaged(
                first_candidate,
                StructuredPlanOutcome(
                    status="invalid_fallback_used",
                    errors=[f"structured repair failed: {type(exc).__name__}"],
                ),
            )
    else:
        repaired = repair_structured_plan_once(
            cleaned, repair_fn=_clean_repair, raw_markdown=raw_markdown or None
        )
    if repaired.ok and repaired.plan is not None:
        plan_dict = repaired.plan.model_dump(mode="json")
        plan_dict = _merge_locked_content(plan_dict, planning_brief, raw_markdown)
        unfaithful = check_structured_faithfulness(plan_dict, raw_markdown, planning_brief)
        repaired_errors, repaired_warnings = _split_faithfulness(unfaithful)
        repaired_errors.extend(_open_plan_contract_errors(plan_dict, planning_brief))
        if repaired_errors:
            return _salvaged(
                [
                    ("repair_attempted_valid", plan_dict, repaired.plan.schema_version),
                    *first_candidate,
                ],
                StructuredPlanOutcome(status="invalid_fallback_used", errors=repaired_errors),
            )
        return _audited_outcome(
            "repair_attempted_valid",
            plan_dict,
            repaired.plan.schema_version,
            repaired_warnings,
        )
    return _salvaged(
        first_candidate,
        StructuredPlanOutcome(status="invalid_fallback_used", errors=list(repaired.errors)),
    )


def _extract_json_object(text: str) -> str | None:
    """Return the first balanced top-level ``{...}`` object in ``text``.

    Uses a brace-counting scan so trailing/leading prose — or stray braces in a
    commentary wrapper or a ```json fence — does not produce a truncated or
    over-wide span the way ``find``/``rfind`` would. String literals are honoured
    so braces inside JSON string values are not mistaken for structure. Returns
    ``None`` when no complete object is present.
    """

    depth = 0
    start: int | None = None
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                return text[start : index + 1]
    return None


def parse_structured_json(text: str) -> Any:
    """Parse model output into JSON, tolerating a leading/trailing prose wrapper.

    Returns ``None`` when no JSON object can be located, so callers treat it as a
    skipped attempt rather than crashing.
    """

    if not text:
        return None
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Fall back to the first balanced ``{...}`` object if the model wrapped the
    # JSON in commentary or a code fence despite instructions.
    candidate = _extract_json_object(stripped)
    if candidate is None:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


# Matches a JSON string value for raw_markdown_fallback, honouring backslash
# escapes so a plan body full of quotes/newlines is captured as one value.
_RAW_FALLBACK_VALUE_RE = re.compile(
    r'("raw_markdown_fallback"\s*:\s*)"(?:[^"\\]|\\.)*"', re.DOTALL
)


def _strip_fallback_from_broken_json(broken_json: str) -> str:
    """Blank ``raw_markdown_fallback`` in a prior attempt before re-embedding it.

    The repair prompt echoes the previous (invalid) JSON back to the model. If
    that attempt copied the whole plan into ``raw_markdown_fallback`` we do not
    want to pay to send it again — and we never want the plan echo, only its
    structural shape, to guide the repair. Parses when possible and blanks the
    field; otherwise falls back to a targeted regex so a genuinely broken blob
    is still trimmed. Never raises.
    """

    if not broken_json:
        return broken_json
    parsed = parse_structured_json(broken_json)
    if isinstance(parsed, dict):
        if parsed.get("raw_markdown_fallback"):
            parsed["raw_markdown_fallback"] = ""
        try:
            return json.dumps(parsed, ensure_ascii=False)
        except (TypeError, ValueError):
            pass
    return _RAW_FALLBACK_VALUE_RE.sub(r'\1""', broken_json)


# One conversion contract. The finalizer owns programming and adjustments;
# conversion may complete missing rest, effort and one execution cue only.
_STRUCTURED_PLAN_RULES = f"""\
Convert the approved plan below into ONE StructuredTrainingPlan JSON object.
Output ONLY JSON, with no wrapper, markdown or commentary.

AUTHORITY
- The plan is the source of truth. Preserve every exercise, session, assigned
  day, fixed dose, rest period, effort method, restriction and safety trigger.
  Do not select exercises, rewrite supplied coaching, invent dates, Easier
  options, progression or future workouts. Preserve source Progress/Progression
  verbatim in progression_rule, separate from weekly development and deload.
- For an EXISTING physical exercise only, infer only those missing presentation
  fields: rest, effort and one concise execution cue. Use the supplied athlete
  context, goals and weaknesses, session purpose, sport, phase/fight proximity,
  fatigue, injury restrictions, equipment and movement demands. Use conservative
  current evidence-based sport-science: adequate recovery for quality/power,
  density only when intended, effort within every supplied safety/dose cap.
  Preserve any explicit value or bound; never replace max intent with RPE.
  Never infer sparring/contact prescriptions or fill locked mental drills.
  This completion cannot change membership, volume, load, dates, safety rules
  or adjustments. Other missing source fields stay empty.
- Server-owned locked sessions are restored in code. Do not recreate omitted
  locked drills. The server also assigns identifiers and execution order;
  use a source id when available, otherwise omit ids and execution_order.
- Set schema_version to "{SCHEMA_VERSION}" and raw_markdown_fallback to "".
  The server restores the original text. Do not echo it in any other field.

STRUCTURE
- Root shape: plan_metadata, athlete_context, event_context, countdown_labels,
  red_flag_rules, plan_notes, weeks, daily_check_ins, nutrition,
  progression_notes, raw_markdown_fallback, schema_version. No top-level plan.
- Training hierarchy: weeks[] -> days[] -> sessions[] -> blocks[]. Preserve
  source order, every dated day, every session and every listed exercise.
- A header such as D-18 (Wednesday) — Power starts a session on that day.
  Two headers with the same D-day mean two sessions on ONE day. A Why: line
  is its objective. A bullet is one block; indented/labelled detail belongs
  to that bullet, never a new exercise. A tactical drill's Step N lines are
  ordered coaching_cues on ONE skill/mindset block, not separate blocks.
- Use source dates/countdowns when stated. Known fight dates use D-N/D0/D+N.
  An OPEN ONGOING PLAN CONTRACT overrides dated-camp fields and examples.
- phase_label: GPP|SPP|TAPER|FIGHT_WEEK|REINTEGRATION.
  session_type: strength_power|conditioning|skill|sparring|primer|recovery|
  rehab|fight_or_match|mixed.
  block_type: preparation|strength|strength_speed|plyometric_power|speed|
  accessory|conditioning|skill|sparring|rehab|mobility_activation|
  cooldown_recovery|mindset|nutrition. Jumps/throws are power, not accessory lifting;
  tactical watch/visualisation is mindset/skill, never physical conditioning.
- Keep declared contact even without S&C: sessions=[] and a headline naming
  Hard sparring or Technical-only combat. If that day ALSO has prescribed work,
  keep the sessions and put the contact label in today_card.coach_led_contact.
  Empty days without contact use day_type=rest and No planned session.
- session completion_status defaults to not_started. Preserve supplied mindset
  in session.mindset_anchor and today_card.mindset_anchor, including intent,
  focus_cue, reset_cue, confidence_anchor and context. Do not invent anchors.

PRESCRIPTION SHAPES
- sets/rounds: positive integers. reps: count or exact source qualifier such
  as "6 per side" or "AMRAP"; never put time, distance or combined chains here.
- duration/work/rest/distance: {{"value": number, "unit": string}}. Time units:
  seconds/minutes/hours. Distance: meters/kilometers/miles/yards. A timed hold
  uses sets + duration; intervals use rounds + work + rest. Keep rest separate.
- Keep explicit ranges verbatim in execution_instructions; scalar working
  fields use lower work/intensity and longer rest, never outside the source.
  Example: 3 x 8; rest 90 sec -> sets=3,reps=8,rest={{"value":90,"unit":"seconds"}}.
- load: {{"method":"percentage","value":85,"unit":"percent","ref":"1RM",
  "display":"85% 1RM"}}, or method=absolute with a stated kg/lb value.
  Other methods: bodyweight|band|rpe|rir|velocity|relative|other. No numeric
  load in the source means null; do not manufacture kg or percentages.
- effort: {{"method":"RPE","value":7,"scale":"1-10"}}. Other methods:
  RIR|intent|velocity|heart_rate_zone|pace|max_effort_percent. Intent is a word
  ("max", "explosive"), never a number. Preserve the source method.
- tempo uses eccentric/pause_bottom/concentric/pause_top, with seconds or X.
- Route labelled source text verbatim: How -> execution_instructions;
  Cue -> coaching_cues; Purpose -> purpose; Why today -> why_today;
  Easier/Regression -> regression_options; Swap -> substitutions;
  Progress/Progression -> progression_rule;
  Stop/Stop rule -> stop_rules. You are not authoring adjustments. Never put
  a conditional swap there in progression_rule. Preserve all conditions and
  safety detail; no word limit may shorten an approved instruction.
- coaching_cues, regression_options, substitutions, stop_rules and applies_to
  are arrays ([] when empty). Other optional fields can be omitted or null.
  Do not emit unused null/default keys for every exercise.

SAFETY AND SUPPORT
- Self-report only: no HRV, CNS scores, WHOOP, strain or wearable readiness.
- daily_check_ins must be fully valid or omitted ([]): only actual dated
  self-reports with date, morning.sleep_quality (1-5), overall_readiness (1-5),
  pain (0-10), and decision=train_as_planned|modify|pull_back|unavailable.
  Never invent scores or emit a partial check-in.
- red_flag_rules contain stop/modify/report rules only. Separate display_text
  from machine metric/operator/threshold/logic. threshold is a number; omit it
  for nonnumeric triggers. Never lose safety thresholds while shortening copy.
- Medical advice, activity guidance and precautions belong once in injury plan_notes or red_flag_rules, never in executable session blocks. Do not duplicate the same warning across these fields.
- plan_notes holds stated plan-level context outside weeks. category:
  weight_cut|injury|nutrition|training|recovery|general. No duplicate warnings.
- Computed support owns risk bands and mindset classification. Weight-cut
  none/moderate: calm context only, warning risk_level=none, professional
  support=false; high: amber; severe: red with professional support=true.
  Never expose coach_gated content, acute-cut instructions or supplement doses.
- Nutrition advice is server-owned: keep summary, daily_focus,
  training_day_guidance and fight_week_guidance empty. Preserve only the
  computed weight_cut_warning when applicable.
- Use short source-backed titles/objectives in plain language. Do not duplicate
  coaching across fields. Except for the three permitted missing presentation
  fields above, omit unsupported optional detail rather than invent it.
"""


# Compact, valid skeleton showing the exact root shape and every nested object
# the schema requires. "..." marks free text to fill from the source plan.
_ROOT_SKELETON = f"""\
ROOT SHAPE EXAMPLE (replace example values; omit unsupported optional detail):
{{
  "schema_version": "{SCHEMA_VERSION}",
  "plan_metadata": {{"title": "...", "sport": "...", "plan_type": "fight_camp", "timezone": "Europe/London", "status": "active", "units": "metric"}},
  "athlete_context": {{"sport_profile": "..."}},
  "event_context": {{"event_type": "fight", "fight_date": "YYYY-MM-DD"}},
  "countdown_labels": [{{"date": "YYYY-MM-DD", "days_to_event": 28, "label": "D-28", "anchor": "fight"}}],
  "red_flag_rules": [{{"rule_id": "...", "when": "morning_check_in", "severity": "amber", "display_text": "...", "action": "..."}}],
  "plan_notes": [{{"category": "weight_cut", "label": "Active weight cut", "text": "..."}}],
  "weeks": [
    {{
      "week_id": "wk-1", "week_index": 1, "phase_label": "SPP", "week_goal": "<short label, max 6 words>",
      "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD",
      "load_focus": {{"volume": "moderate", "intensity": "high", "specificity": "high", "fatigue_target": "reduced"}},
      "progression": {{"week_type": "build", "planned_change_from_previous": "..."}},
      "days": [
        {{
          "date": "YYYY-MM-DD", "weekday": "Mon", "day_type": "high", "countdown_label": "D-15", "phase_label": "SPP",
          "today_card": {{"headline": "...", "readiness_status": "train_as_planned", "coach_led_contact": "", "mindset_anchor": {{"intent": "...", "focus_cue": "...", "reset_cue": "...", "confidence_anchor": "...", "context": "..."}}}},
          "sessions": [
            {{
              "session_id": "ses-1", "session_type": "strength_power", "title": "...", "objective": "...",
              "completion_status": "not_started",
              "mindset_anchor": {{"intent": "...", "focus_cue": "...", "reset_cue": "...", "confidence_anchor": "...", "context": "..."}},
              "blocks": [
                {{
                  "block_id": "blk-1", "block_type": "strength", "display_name": "...", "sets": 4, "reps": 4,
                  "load": {{"method": "percentage", "value": 85, "unit": "percent", "ref": "1RM", "display": "85% 1RM"}},
                  "rest": {{"value": 180, "unit": "seconds"}}, "duration": null,
                  "purpose": "...", "why_today": "...", "coaching_cues": ["..."],
                  "regression_options": ["..."], "substitutions": ["..."],
                  "execution_instructions": "...", "progression_rule": "...", "stop_rules": ["..."]
                }}
              ]
            }}
          ]
        }}
      ]
    }}
  ],
  "daily_check_ins": [],
  "nutrition": {{"summary": "...", "daily_focus": "...", "training_day_guidance": "...", "fight_week_guidance": "...", "weight_cut_warning": {{"risk_level": "amber", "display_text": "...", "requires_professional_support": false}}}},
  "progression_notes": "...",
  "raw_markdown_fallback": ""
}}"""


# Athlete/event/phase context keys the structured conversion can use as soft
# context. Everything else in the brief (candidate_pools, weekly_role_map,
# stage1_selection_summary, weekly_stress_map, late_fight_plan_spec, …) is
# internal Stage 1 machinery the conversion never needs — the plan markdown and
# computed_support already carry the authoritative content — so it is left out.
_BRIEF_CONTEXT_KEYS = (
    "athlete_snapshot",
    "archetype_summary",
    "fight_demands",
    "fight_week_override",
    "phase_strategy",
    "main_limiter",
    "limiter_profile",
    "priority_focus",
    "compressed_priorities",
    "main_risks",
)

# Soft context only: include complete fields within this budget. Never cut JSON
# mid-value; the approved programme and all explicit prescriptions stay in full.
_BRIEF_CONTEXT_CHAR_CAP = 8000


def _select_brief_context(planning_brief: Any) -> dict[str, Any]:
    """Pick the athlete/event/phase context keys from a (possibly huge) brief.

    Deterministic allowlist: returns the present :data:`_BRIEF_CONTEXT_KEYS` in a
    fixed order so the serialized context does not depend on dict ordering, and
    the large internal Stage 1 structures never reach the prompt. Non-dict input
    yields ``{}``.
    """

    if not isinstance(planning_brief, dict):
        return {}
    selected: dict[str, Any] = {}
    for key in _BRIEF_CONTEXT_KEYS:
        if key not in planning_brief or planning_brief[key] in (None, "", [], {}):
            continue
        candidate = {**selected, key: planning_brief[key]}
        try:
            if len(json.dumps(candidate, ensure_ascii=False)) <= _BRIEF_CONTEXT_CHAR_CAP:
                selected = candidate
        except (TypeError, ValueError):
            continue
    return selected


def _open_plan_prompt_contract(planning_brief: Any) -> str:
    spec = _open_plan_spec_from_brief(planning_brief)
    if spec is None:
        return ""

    weekdays = _open_plan_training_weekdays(spec)
    # The plan text already contains the programme. Repeating the full spec
    # (four weeks, exercise bank prose, render_rules and tactical libraries)
    # made the converter reason over two overlapping instructions and doses.
    programmed_weeks = (spec.get("weekly_session_plan") or {}).get("weeks") or []
    calendar = {
        "plan_type": _OPEN_PLAN_TYPE,
        "training_weekdays": weekdays,
        "week_count": len(programmed_weeks) or 1,
    }
    try:
        spec_json = json.dumps(calendar, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        spec_json = "{}"

    return f"""\
OPEN ONGOING PLAN CONTRACT (authoritative; overrides the fight-camp example
values in the generic root skeleton):
- This is a renewable open training system, NOT a dated fight camp.
- Set plan_metadata.plan_type to "{_OPEN_PLAN_TYPE}".
- Set event_context.event_type to "none" and all event dates to null/empty.
- Set countdown_labels to [] and every day countdown_label to "".
- Keep week start/end/countdown fields empty; the server projects real dates.
- When weekly_session_plan provides four weeks, emit those four weeks with
  their actual sessions. Identical baseline exercise prescriptions are valid.
  Otherwise emit one reusable template week.
- Every emitted week.days array MUST contain exactly these training weekdays in
  this order: {json.dumps(weekdays)}. Set each day.weekday to that exact short
  value. Do not emit OFF/rest-only weekdays and do not infer extra training days.
- Preserve the source session spine. The 4-week development rules describe how
  the same spine progresses; do not invent additional exercises to fill weeks.
- The generic schema still requires phase_label; use the source value when
  stated, otherwise use "GPP" as a neutral internal value. Never surface phase
  labels as open-plan headings.

OPEN-PLAN DELOAD
- Preserve each source "progression_rule" and "deload_rule" verbatim. Never create a
  progression or Easier note from exercise type or a generic week policy.
- Missing source adjustment fields stay empty. Rest, effort and execution-cue
  completion still follow the same physical-exercise limits above.

AUTHORITATIVE open_plan_spec:
{spec_json}"""


def build_structured_plan_prompt(
    *,
    plan_markdown: str,
    planning_brief: dict[str, Any] | None = None,
    event_date: str = "",
    repair_errors: list[str] | None = None,
    broken_json: str | None = None,
) -> str:
    """Build the model prompt for turning a markdown plan into structured JSON.

    When ``repair_errors``/``broken_json`` are supplied the prompt asks the model
    to fix a previous invalid attempt (the single repair retry).
    """

    plan_markdown = strip_locked_sessions_for_conversion(plan_markdown, planning_brief)
    is_minor = isinstance(planning_brief, dict) and is_minor_support(
        planning_brief.get("computed_support")
    )
    if is_minor:
        plan_markdown = _minor_conversion_markdown(plan_markdown)
    sections: list[str] = [_STRUCTURED_PLAN_RULES, _ROOT_SKELETON]
    if is_minor:
        sections.append(_MINOR_CONVERSION_RULE)
    open_plan_contract = _open_plan_prompt_contract(planning_brief)
    if open_plan_contract:
        sections.append(open_plan_contract)

    if event_date:
        sections.append(f"EVENT/FIGHT DATE: {event_date}")

    if planning_brief:
        # Carry Stage 1's own computed mindset classification and weight-cut
        # risk bands through UNTRUNCATED so the conversion does not re-derive them from
        # compressed prose. The rest of the brief stays capped (it is broad
        # context, not source-of-truth numbers).
        computed_support = None
        if isinstance(planning_brief, dict) and "computed_support" in planning_brief:
            computed_support = planning_brief.get("computed_support")
            if isinstance(computed_support, dict) and "injury_awareness" in computed_support:
                computed_support = {
                    key: value
                    for key, value in computed_support.items()
                    if key != "injury_awareness"
                }
            computed_support = _conversion_support(computed_support)
            if is_minor:
                computed_support = _minor_conversion_support(computed_support)

        # Select only the athlete/event/phase context keys. The full brief is
        # huge (100k+ chars: candidate_pools, weekly_role_map, selection
        # summaries) and a blind slice fed the conversion ~6k chars of whichever
        # giant internal structure happened to serialize first — nondeterministic
        # noise, not the context this section claims. An explicit allowlist is
        # deterministic and drops the internal pools entirely.
        brief_context = _select_brief_context(planning_brief)
        try:
            brief_json = json.dumps(brief_context, ensure_ascii=False)
        except (TypeError, ValueError):
            brief_json = ""
        if not brief_json:
            brief_json = "{}"
        sections.append(
            "PLANNING BRIEF (context for athlete/event/phases — do not copy "
            "verbatim):\n" + brief_json
        )

        if computed_support:
            try:
                support_json = json.dumps(computed_support, ensure_ascii=False)
            except (TypeError, ValueError):
                support_json = ""
            if support_json and is_minor:
                sections.append(
                    "STAGE 1 COMPUTED SUPPORT (authoritative mindset "
                    "classification — use these exact values; do not invent "
                    "or round differently):\n" + support_json
                )
            elif support_json:
                sections.append(
                    "STAGE 1 COMPUTED SUPPORT (authoritative weight-cut risk "
                    "bands and mindset classification — use these exact values; "
                    "do not invent or round differently):\n" + support_json
                )

    sections.append(
        "ORIGINAL PLAN (this is the source to convert; do NOT copy it into "
        "raw_markdown_fallback — leave that field \"\"):\n"
        + plan_markdown
    )

    if repair_errors:
        sections.append(
            "Your previous JSON failed schema validation with these errors:\n"
            + "\n".join(f"- {err}" for err in repair_errors)
        )
    if broken_json:
        sections.append(
            "Previous invalid JSON to correct (return a fully valid object):\n"
            + _strip_fallback_from_broken_json(broken_json)[:12000]
        )

    if repair_errors:
        sections.append(
            "Fix ONLY the JSON structure/shape so it matches the root skeleton and "
            "passes the schema. Do NOT change the training content."
        )

    sections.append(
        "Return the corrected StructuredTrainingPlan JSON object now."
        if repair_errors
        else "Return the StructuredTrainingPlan JSON object now."
    )

    return "\n\n".join(sections)
