"""Edit, swap, add and remove exercises on generated or custom training plans.

This is personalisation of an existing prescription, not a workout builder:

* the generated card is never edited and never regenerated; every change is an
  entry in ``plans.exercise_overrides`` (see ``api.services.exercise_overrides``);
* alternatives come from the training catalogue's own metadata, ranked with no
  model call, in the order safety -> training purpose -> movement -> athlete
  relevance -> session cost;
* manual additions and swaps are athlete choices, independent of injury guidance;
* started, completed or past workouts are never changed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from api.contracts.custom_plans import CustomDose
from api.contracts.injury_policy import reconcile_session_prescription
from api.services.custom_plans import dose_block_fields, dose_text
from api.services.exercise_dose import dose_guidance
from api.services.exercise_media import load_media_index
from api.services.exercise_overrides import (
    PROTECTED_BLOCK_TYPES,
    block_identity,
    is_custom_plan,
    session_slot,
    slug,
    stored_overrides,
    editable_day_sessions,
    weekday_token,
)
from api.services.open_plan_timeline import open_plan_spec
from api.services.training_library import cache, preview
from fightcamp.tag_maps import GOAL_NORMALIZER, GOAL_TAG_MAP, STYLE_TAG_MAP, WEAKNESS_NORMALIZER, WEAKNESS_TAG_MAP
from fightcamp.training_context import normalize_athlete_equipment_list, normalize_equipment_list
from fightcamp.sports import SUPPORTED_SPORTS, normalize_sport

logger = logging.getLogger(__name__)

# Blocks an athlete may swap or remove. Coach-led combat work, skill, sparring,
# rehab, nutrition and mindset content stay as prescribed.
EDITABLE_BLOCK_TYPES = frozenset({
    "strength", "strength_speed", "plyometric_power", "speed", "accessory", "conditioning",
})
# Sessions an athlete may add an exercise to. Coach-led sparring, rehab and
# recovery sessions stay as prescribed.
ADDABLE_SESSION_TYPES = frozenset({"strength_power", "conditioning", "mixed", "skill"})
LOCKING_COMPLETION_STATUSES = frozenset({"started", "done", "modified", "skipped"})

_STRENGTH_SOURCES = ("exercise_bank.json",)
_CONDITIONING_SOURCES = ("conditioning_bank.json", "style_conditioning_bank.json")
_SOURCES_BY_BLOCK_TYPE = {
    "strength": _STRENGTH_SOURCES, "strength_speed": _STRENGTH_SOURCES, "accessory": _STRENGTH_SOURCES,
    "plyometric_power": _STRENGTH_SOURCES, "speed": _STRENGTH_SOURCES,
    "conditioning": _CONDITIONING_SOURCES,
}
_SOURCES_BY_SESSION_TYPE = {
    "strength_power": _STRENGTH_SOURCES, "conditioning": _CONDITIONING_SOURCES,
    "mixed": _STRENGTH_SOURCES + _CONDITIONING_SOURCES,
}
_METHOD_FAMILIES = {
    "strength": "strength", "strength_conditioning": "strength", "contrast": "power",
    "power": "power", "plyometric": "power", "primer": "power", "conditioning": "conditioning",
    # Strength-bank prehab support (goblet squat, banded walks). Clinical rehab
    # lives in the rehab bank, which is a different category and never offered.
    "rehab": "strength",
}
# Patterns too generic to establish an equivalent training purpose on their own.
_GENERIC_MOVEMENTS = frozenset({"dynamic", "compound", "core", "mobility", "complex", "isometric", "isometric_hold"})
_COST_FIELDS = ("cns_load", "movement_cost", "impact_cost", "eccentric_cost", "landing_cost",
                "soreness_risk", "lactate_load")
_COST_RANK = {"none": 0, "very_low": 1, "low": 1, "low_moderate": 2, "moderate": 2, "medium": 2,
              "moderate_high": 3, "high": 3, "very_high": 4, "max": 4}
_AMBIENT_EQUIPMENT = frozenset({"bodyweight", "none", "open_space", "wall", "floor", "line", "tape",
                                "line_tape", "space", "stable_support"})
_MOVEMENT_LABELS = {
    "horizontal_push": "horizontal push", "vertical_push": "vertical push", "vertical_jump": "vertical jump",
    "horizontal_jump": "horizontal jump", "lateral_reactive": "lateral reactive", "isometric": "isometric",
}
_CATEGORY_LABELS = {"lower_body": "Lower-body", "upper_body": "Upper-body", "full_body": "Full-body", "core": "Core"}
_ISOMETRIC_MOVEMENTS = frozenset({"isometric", "isometric_hold"})

TOP_LIMIT = 5
ALL_LIMIT = 24


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Sequence):
        return [str(item) for item in value if isinstance(item, str) and item.strip()]
    return []


def _label(value: str) -> str:
    return _MOVEMENT_LABELS.get(value, value.replace("_", " "))


def _parse_day(value: Any, *, field: str = "date") -> date:
    try:
        return date.fromisoformat(_clean(value)[:10])
    except ValueError:
        raise HTTPException(422, f"A valid {field} is required.") from None


def _library_index():
    try:
        return cache.get()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        logger.exception("[exercise_customisation] training catalogue unavailable")
        raise HTTPException(503, "Exercise options are temporarily unavailable.") from None


def _cost(raw: Mapping[str, Any], field: str) -> int | None:
    return _COST_RANK.get(_clean(raw.get(field)).lower()) if raw.get(field) is not None else None


def _total_cost(raw: Mapping[str, Any]) -> int:
    return sum(value for field in _COST_FIELDS if (value := _cost(raw, field)) is not None)


def _tags(raw: Mapping[str, Any]) -> set[str]:
    return {tag.lower() for tag in _strings(raw.get("tags"))}


def _movement_tags(raw: Mapping[str, Any]) -> set[str]:
    return {tag for tag in _tags(raw) if not tag.startswith("mech_")}


def _family(raw: Mapping[str, Any], source: str) -> str | None:
    if source in _CONDITIONING_SOURCES:
        return "conditioning"
    return _METHOD_FAMILIES.get(_clean(raw.get("method")).lower())


# ---------------------------------------------------------------------------
# Athlete context
# ---------------------------------------------------------------------------


class AthleteContext:
    """What UNLXCK already knows about the athlete, reused for every ranking."""

    def __init__(self, intake: Mapping[str, Any]):
        athlete = intake.get("athlete") if isinstance(intake.get("athlete"), Mapping) else {}
        self.equipment = set(normalize_athlete_equipment_list(_strings(intake.get("equipment_access"))))
        goals = [*_strings(intake.get("key_goals")), *_strings(intake.get("primary_goal"))]
        self.goals = {GOAL_NORMALIZER.get(goal.strip(), goal.strip()).lower() for goal in goals}
        self.primary_goal = (GOAL_NORMALIZER.get(_clean(intake.get("primary_goal")), _clean(intake.get("primary_goal"))).lower()
                             if intake.get("primary_goal") else None)
        self.goal_tags = {tag: goal for goal in self.goals for tag in GOAL_TAG_MAP.get(goal, [])}
        weaknesses = [*_strings(intake.get("weak_areas")), *_strings(intake.get("primary_weak_area"))]
        self.weaknesses = {tag for item in weaknesses
                           for tag in WEAKNESS_NORMALIZER.get(item.strip().lower(), [item.strip().lower()])}
        self.weakness_tags = {tag: weakness for weakness in self.weaknesses
                              for tag in WEAKNESS_TAG_MAP.get(weakness, [weakness])}
        styles = [*_strings(athlete.get("technical_style")), *_strings(athlete.get("tactical_style"))]
        self.styles = {style.strip().lower() for style in styles}
        self.sports = {normalize_sport(style) for style in _strings(athlete.get("technical_style"))}
        self.style_tags = {tag for style in self.styles
                           for tag in STYLE_TAG_MAP.get(style, STYLE_TAG_MAP.get(style.replace(" ", "_"), []))}


def _intake_for_plan(store: Any, plan_row: Mapping[str, Any], athlete_id: str) -> Mapping[str, Any]:
    intake_id = _clean(plan_row.get("intake_id"))
    row = store.get_intake(intake_id) if intake_id else None
    row = row or store.get_latest_intake(athlete_id)
    payload = (row or {}).get("intake") if isinstance(row, Mapping) else None
    return payload if isinstance(payload, Mapping) else {}


# ---------------------------------------------------------------------------
# Locating the occurrence
# ---------------------------------------------------------------------------


class Occurrence:
    def __init__(self, *, plan_row, day, week, session, block, date_value, training_day):
        self.plan_row = plan_row
        self.day = day
        self.week = week
        self.session = session
        self.block = block
        self.date = date_value
        self.training_day = training_day

    @property
    def phase(self) -> str:
        from api.services.today_service import _normalized_structured_phase

        return (_normalized_structured_phase(self.day.get("phase_label"))
                or _normalized_structured_phase(self.week.get("phase_label")))

    @property
    def is_open_plan(self) -> bool:
        return open_plan_spec(self.plan_row) is not None and not _clean(self.plan_row.get("fight_date"))


def locate_occurrence(
    store: Any, *, athlete_id: str, athlete_timezone: str | None, plan_id: str,
    day: str, session_id: str, block_id: str | None = None, now: datetime | None = None,
) -> Occurrence:
    """The plan's effective session (and block) on ``day``, or the HTTP error why not.

    Effective means with the athlete's earlier changes applied — the same card
    Today and Plan Detail render, so a stale client is caught by a missing id.
    """
    from api.services.today_service import (
        _iter_mapping_items, _projected_structured_plan, _require_valid_plan_id, resolve_training_day,
    )

    _require_valid_plan_id(plan_id)
    plan_row = store.get_training_plan_for_athlete(plan_id, athlete_id)
    if plan_row is None:
        raise HTTPException(404, "plan not found")
    training_day = resolve_training_day(athlete_timezone, now=now)
    target = _parse_day(day)
    if target < date.fromisoformat(training_day):
        raise HTTPException(409, "Past workouts can't be changed.")
    weeks, context = _projected_structured_plan(plan_row, training_day=target.isoformat())
    if open_plan_spec(plan_row) is not None and context.get("projection_status") != "projected":
        raise HTTPException(409, "This plan's calendar isn't available to change right now.")
    for week in weeks:
        for plan_day in _iter_mapping_items(week.get("days")):
            if _clean(plan_day.get("date"))[:10] != target.isoformat():
                continue
            plan_day = {**plan_day, "sessions": editable_day_sessions(plan_day)}
            session = next((s for s in _iter_mapping_items(plan_day.get("sessions"))
                            if _clean(s.get("session_id")) == session_id), None)
            if session is None:
                break
            block = None
            if block_id is not None:
                block = next((b for b in _iter_mapping_items(session.get("blocks"))
                              if _clean(b.get("block_id")) == block_id), None)
                if block is None:
                    raise HTTPException(409, "That exercise changed. Refresh and try again.")
            return Occurrence(plan_row=plan_row, day=plan_day, week=week, session=session, block=block,
                              date_value=target, training_day=training_day)
    raise HTTPException(409, "That workout changed. Refresh and try again.")


def _occurrence_locked(store: Any, athlete_id: str, plan_id: str, day: date) -> bool:
    rows = store.list_session_completions_from_day(athlete_id, day.isoformat())
    return any(_clean(row.get("training_day"))[:10] == day.isoformat()
               and _clean(row.get("plan_id")) == plan_id
               and _clean(row.get("status")) in LOCKING_COMPLETION_STATUSES
               for row in rows or [])


def require_editable_day(store: Any, occurrence: Occurrence, athlete_id: str) -> None:
    if _occurrence_locked(store, athlete_id, _clean(occurrence.plan_row.get("id")), occurrence.date):
        raise HTTPException(409, "This workout has started. Its exercises stay as prescribed.")


def block_is_editable(block: Mapping[str, Any]) -> bool:
    block_type = _clean(block.get("block_type"))
    change = block.get("athlete_change") if isinstance(block.get("athlete_change"), Mapping) else {}
    if block_type in PROTECTED_BLOCK_TYPES:
        return False
    # Anything the athlete added is theirs to change, whatever its type.
    if block_type not in EDITABLE_BLOCK_TYPES and change.get("action") not in {"add", "edit"} and block.get("prescription_source") != "athlete":
        return False
    # Anything carrying injury-policy ownership is clinical, whatever its type.
    return not any(block.get(field) for field in ("injury_id", "policy_id", "rehab_drill_id", "clinical_review_pin"))


def session_can_add(session: Mapping[str, Any]) -> bool:
    kind = _clean(session.get("session_type"))
    if kind in ADDABLE_SESSION_TYPES:
        return True
    if kind == "sparring":
        from api.contracts.readiness_message import _is_hard_live_contact
        return not _is_hard_live_contact(session, "high")
    return False


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


class SafetyContext:
    """Live injury decisions for the occurrence, computed by the existing system."""

    def __init__(self, store: Any, *, athlete_id: str, occurrence: Occurrence, equipment: Sequence[str]):
        from api.contracts.readiness_message import _injury_text_regions, _resolve_injury_regions
        from api.services.today_service import (
            _entry_mapping_for_readiness, _with_injury_policy, _with_rehab_stage, _with_safe_session_context,
            _with_surface_class, restrictive_injuries,
        )
        from api.services.injury_episode_service import apply_episode_observations, episode_observations

        lister = getattr(store, "list_injury_flags", None)
        try:
            flags = [dict(flag) for flag in (lister(athlete_id, statuses=("open", "monitoring"), limit=500) or [])] \
                if callable(lister) else []
        except Exception:
            # Unknown injury state never yields "safe" alternatives.
            logger.exception("[exercise_customisation] injury flags unavailable")
            raise HTTPException(503, "Your injury guidance couldn't be loaded. Try again.") from None
        injuries = _with_rehab_stage(_with_safe_session_context(_with_surface_class(flags)),
                                     store=store, athlete_id=athlete_id)
        injuries = [apply_episode_observations(row, episode_observations(store, athlete_id, row)) for row in injuries]
        self.session_shell = {key: value for key, value in _entry_mapping_for_readiness(occurrence.session).items()
                              if key != "blocks"}
        injuries = _with_injury_policy(
            injuries, store=store, athlete_id=athlete_id, phase=occurrence.phase, equipment=equipment,
            training_day=occurrence.date.isoformat(), training_session=occurrence.session,
        ) if injuries else []
        # Only now, with episode observations, holds and clinical policy all
        # evaluated, may a "Not limiting me" report stop narrowing the choices
        # (same rule as generation and Today). A row without an id is kept.
        injuries = restrictive_injuries(store, athlete_id, injuries)
        kept_ids = {_clean(row.get("id")) for row in injuries}
        flags = [flag for flag in flags if not _clean(flag.get("id")) or _clean(flag.get("id")) in kept_ids]
        self.injuries = injuries
        self.decisions = [row["rehab_decision"] for row in injuries if isinstance(row.get("rehab_decision"), Mapping)]
        self.plan_id = _clean(occurrence.plan_row.get("id"))
        self.day = occurrence.date.isoformat()
        # Countdown for authored fight-proximity cutoffs. No fight date means no
        # countdown to enforce; an unreadable one fails closed (None).
        fight_date = _clean(occurrence.plan_row.get("fight_date"))
        try:
            self.days_until_fight = (date.fromisoformat(fight_date[:10]) - occurrence.date).days if fight_date else False
        except ValueError:
            self.days_until_fight = None
        regions: set[str] = set()
        for injury in flags:
            if _clean(injury.get("status")).lower() not in {"open", "monitoring"}:
                continue
            if _clean(injury.get("body_area")):
                regions |= _resolve_injury_regions(_clean(injury.get("body_area")))
            regions |= _injury_text_regions(" ".join(_clean(injury.get(k)) for k in ("label", "description")))
        self.injured_regions = regions
        self.injury_texts = [text for injury in flags
                             if (text := " ".join(_clean(injury.get(k)) for k in ("body_area", "description")).strip())]
        # The same text with its reported severity, for the generation guard.
        self.guard_injuries = [f"{_clean(injury.get('severity')) or 'moderate'} {text}"
                               for injury in flags
                               if (text := " ".join(_clean(injury.get(k)) for k in ("body_area", "description")).strip())]
        self.phase = occurrence.phase
        self.has_injuries = bool(flags)

    def contraindicated(self, raw: Mapping[str, Any]) -> bool:
        """Held when inside an authored fight cutoff, or contraindicated.

        A contraindication counts only when it names a currently injured region.

        Region-less notes ("skip when pins are unavailable", blood pressure
        during a cut) are not injury evidence. When no injured region could be
        resolved, any contraindication still counts: unknown never clears.
        """
        from api.contracts.readiness_message import _injury_text_regions

        from fightcamp.late_selector_windows import fight_proximity_allowed

        days = getattr(self, "days_until_fight", False)
        if days is not False and not fight_proximity_allowed(raw, days):
            return True
        from fightcamp.injury_filtering import is_absolute_safety_note

        # Conditional warnings are decided by the generation guard in allows();
        # a body-region match alone never holds an exercise here.
        notes = [note for note in _strings(raw.get("contraindications"))
                 if is_absolute_safety_note("contraindications", note)]
        if not self.has_injuries or not notes:
            return False
        if not self.injured_regions:
            return True
        return any(_injury_text_regions(note) & self.injured_regions for note in notes)

    def planner_excludes(self, raw: Mapping[str, Any]) -> bool:
        """Generation's injury verdict for one bank row, at the reported severity.

        The row is held exactly where the generation guard excludes it: its
        exclusion rules and absolute metadata (avoid_if, active-symptom notes)
        exclude, while flag-level rules and conditional metadata warnings adapt
        by severity and never hold a row on a region match alone.
        """
        from fightcamp.injury_guard import injury_decision

        if not self.injury_texts:
            return False
        guard_injuries = getattr(self, "guard_injuries", self.injury_texts)
        return injury_decision(dict(raw), guard_injuries, getattr(self, "phase", "") or "SPP",
                               "moderate").action == "exclude"

    def allows(self, block: Mapping[str, Any], raw: Mapping[str, Any] | None = None, *,
               prescribed: bool = False) -> bool:
        """Whether ``block`` fits today's injury guidance.

        ``prescribed`` marks the athlete's own planned exercise with a new dose:
        the planner already cleared it, so missing mechanical metadata is not a
        reason to refuse it. Region overlap, the planner's exclusions and the
        live policy still apply.
        """
        from api.contracts.readiness_message import _session_mechanical_load_regions
        # The planner's own exclusion rules: what generation would never have
        # prescribed for these injuries is never offered as a replacement.
        if raw is not None and self.planner_excludes(raw):
            return False
        loaded_regions = _session_mechanical_load_regions({"blocks": [block]})
        if self.has_injuries and not loaded_regions and not prescribed:
            return False
        if loaded_regions & self.injured_regions:
            return False
        probe = {**deepcopy(dict(block)), "block_id": "candidate-probe"}
        snapshot = reconcile_session_prescription(
            {**self.session_shell, "blocks": [probe]}, decisions=self.decisions, plan_id=self.plan_id,
            training_day=self.day, injuries=self.injuries,
        )
        if snapshot is None:
            return True
        if snapshot.get("safety_hold"):
            return False
        blocks = (snapshot.get("session") or {}).get("blocks") or []
        return any(b.get("block_id") == "candidate-probe" and not b.get("_policy_held") for b in blocks)


# ---------------------------------------------------------------------------
# Catalogue candidates
# ---------------------------------------------------------------------------


def _record_for_block(index: Any, block: Mapping[str, Any]) -> dict | None:
    catalogue_id = _clean(block.get("catalogue_id"))
    if catalogue_id and catalogue_id in index.by_id:
        return index.by_id[catalogue_id]
    key = _clean(block.get("exercise_key"))
    if not key:
        return None
    matches = [r for r in index.records if r["summary"]["category"] == "training" and r["entry"]["exercise_key"] == key]
    # One canonical identity only: an ambiguous key is not evidence.
    return matches[0] if len(matches) == 1 else None


def _equipment_available(record: Mapping[str, Any], athlete: AthleteContext) -> bool:
    raw = record["entry"]["raw"]
    tokens = set(normalize_equipment_list(raw.get("equipment")))
    if not tokens - _AMBIENT_EQUIPMENT:
        requirements = record.get("equipment_requirements")
        return not requirements or requirements.get("status") == "reviewed"
    if not (tokens - _AMBIENT_EQUIPMENT) <= athlete.equipment:
        return False
    requirements = record.get("equipment_requirements")
    if requirements and requirements.get("status") != "reviewed":
        return False
    return True


def _equipment_labels(record: Mapping[str, Any]) -> list[str]:
    tokens = [t for t in normalize_equipment_list(record["entry"]["raw"].get("equipment")) if t not in _AMBIENT_EQUIPMENT]
    return [t.replace("_", " ").capitalize() for t in tokens][:2]


def _phase_allows(raw: Mapping[str, Any], phase: str) -> bool:
    phases = {p.upper() for p in _strings(raw.get("phases"))}
    if not phase or phase not in {"GPP", "SPP", "TAPER"}:
        return True
    return not phases or phase in phases


def _sport_allows(record: Mapping[str, Any], athlete: AthleteContext) -> bool:
    """Sport identity is eligibility, not just a ranking bonus (also on write)."""
    from fightcamp.conditioning import is_banned_drill

    entry, raw = record["entry"], record["entry"]["raw"]
    sports = {normalize_sport(s) for s in _strings(raw.get("sports"))}
    tagged = {normalize_sport(t) for t in _tags(raw)} & set(SUPPORTED_SPORTS)
    declared = athlete.sports
    compatible = set(declared)
    if compatible & {"muay_thai", "kickboxing", "karate"}:
        compatible |= {"muay_thai", "kickboxing"}
    if compatible & {"bjj", "wrestling", "grappling"}:
        compatible.add("grappling")
    if "mma" in compatible:
        compatible |= set(SUPPORTED_SPORTS) | {"grappling"}
    # Universal drills suit every sport (and athletes with none declared), so
    # sport identity never excludes them.
    if "universal" in sports:
        return True
    if sports and not sports & compatible:
        return False
    if tagged and not tagged & compatible:
        return False
    sport_specific = bool(sports or tagged or entry.get("content_class") == "technical_coordination")
    if not sport_specific:
        return True
    if not declared:
        return False
    # Audience tags are not techniques. A jab drill shared by boxing and Muay
    # Thai remains valid for a boxer; "keep elbows tucked" is also a cue, not
    # an elbow-strike prescription.
    technique_tags = [tag for tag in _strings(raw.get("tags")) if normalize_sport(tag) not in SUPPORTED_SPORTS]
    details = str(raw.get("modality") or "")
    return any(
        sport not in {"boxing", "kickboxing"} or not is_banned_drill(
            entry["name"], technique_tags, sport, details=details,
            technical_styles=sorted(declared),
        )
        for sport in declared
    )


def _no_more_stress(candidate: Mapping[str, Any], ceiling: Mapping[str, int | None]) -> bool:
    """No cost field rises above the ceiling; unknown never passes a known ceiling."""
    for field, limit in ceiling.items():
        if limit is None:
            continue
        value = _cost(candidate, field)
        if value is None or value > limit:
            return False
    return True


def _carried_effort(effort: Any) -> dict | None:
    if isinstance(effort, Mapping) and effort.get("method") in {"RPE", "RIR", "intent"}:
        return dict(effort)
    return None


def _block_mode(block: Mapping[str, Any]) -> str | None:
    if block.get("rounds") and block.get("work"):
        return "intervals"
    if block.get("sets") and block.get("reps"):
        return "reps"
    if block.get("distance"):
        return "distance"
    if block.get("duration"):
        return "duration"
    return None


def _candidate_mode(record: Mapping[str, Any]) -> tuple[str | None, dict | None]:
    mode, reliable, _ = dose_guidance(record)
    return mode, reliable


def _carry_dose(original: Mapping[str, Any], mode: str) -> dict[str, Any]:
    """Session-level dose that transfers between equivalent movements.

    Never load or intensity text (an absolute weight is exercise-specific), never
    distance, and only within the same dose mode. Effort is carried only as an
    RPE/RIR/intent target, which does not raise the session's stress.
    """
    fields = {"reps": ("sets", "reps", "rest", "tempo"), "intervals": ("rounds", "work", "rest"),
              "duration": ("sets", "duration", "rest"), "distance": ("sets", "distance", "rest"),
              "text": ()}[mode]
    carried = {field: deepcopy(original[field]) for field in fields if original.get(field) not in (None, "", {})}
    effort = _carried_effort(original.get("effort"))
    if effort:
        carried["effort"] = effort
    return carried


def _measured_seconds(value: Any) -> str | None:
    if not isinstance(value, Mapping) or value.get("value") is None:
        return None
    unit = _clean(value.get("unit")).lower()
    amount = value["value"]
    amount = int(amount) if isinstance(amount, float) and amount.is_integer() else amount
    if unit in {"seconds", "second", "sec", "s"}:
        return f"{amount}s"
    if unit in {"minutes", "minute", "min"}:
        return f"{amount} min"
    return f"{amount} {unit}".strip()


def prescription_summary(fields: Mapping[str, Any]) -> str | None:
    parts: list[str] = []
    if fields.get("rounds") and fields.get("work"):
        parts.append(f"{fields['rounds']} × {_measured_seconds(fields['work'])}")
    elif fields.get("sets") and fields.get("reps"):
        parts.append(f"{fields['sets']} × {fields['reps']}")
    elif fields.get("duration"):
        duration = _measured_seconds(fields["duration"])
        parts.append(f"{fields['sets']} × {duration}" if fields.get("sets") else duration)
    elif fields.get("distance"):
        distance = fields["distance"]
        amount = f"{distance['value']:g} {distance['unit']}"
        parts.append(f"{fields['sets']} × {amount}" if fields.get("sets") else amount)
    elif fields.get("prescription_text"):
        return str(fields["prescription_text"])
    effort = fields.get("effort")
    if isinstance(effort, Mapping) and effort.get("value") is not None and effort.get("method") in {"RPE", "RIR"}:
        value = effort["value"]
        value = int(value) if isinstance(value, float) and value.is_integer() else value
        parts.append(f"{effort['method']} {value}")
    # Rest stays on the block; the option line shows the work and the target.
    return " · ".join(part for part in parts if part) or None


def _base_block(record: Mapping[str, Any], *, block_type: str) -> dict[str, Any]:
    """A block for a catalogue exercise, with its own safety metadata and guidance.

    Mechanical metadata is copied only where the bank states it; missing values
    stay missing so the injury guards treat them as unknown.
    """
    entry = record["entry"]
    raw = entry["raw"]
    block: dict[str, Any] = {
        "block_type": block_type, "display_name": entry["name"], "exercise_key": entry["exercise_key"],
        "catalogue_id": entry["catalogue_id"],
    }
    if record["summary"].get("content_type") in {"strength", "power"}:
        block["exercise_type"] = record["summary"]["content_type"]
    for field in ("mechanical_load_regions", "tags", "contact_level", "mechanical_risk_tags"):
        if raw.get(field) is not None:
            block[field] = deepcopy(raw[field])
    if raw.get("impact_level") is not None or raw.get("impact_cost") is not None:
        block["impact_level"] = _clean(raw.get("impact_level") or raw.get("impact_cost"))
    guidance = preview(record).get("guidance", {})
    for target, source in (("coaching_cues", "Cues"), ("stop_rules", "Stop"), ("regression_options", "Regress")):
        if guidance.get(source):
            block[target] = list(guidance[source])
    for target, source in (("purpose", "Why"), ("progression_rule", "Progress")):
        if guidance.get(source):
            block[target] = "\n".join(guidance[source])
    return block


def _snapshot(block: Mapping[str, Any]) -> dict[str, Any]:
    from api.services.exercise_log_service import _PRESCRIBED_FIELDS

    keys = (*_PRESCRIBED_FIELDS, "block_id", "exercise_key", "catalogue_id", "effort", "tempo")
    return {key: deepcopy(block[key]) for key in dict.fromkeys(keys) if block.get(key) not in (None, "", [], {})}


class Candidate:
    def __init__(self, record, score, reasons, fields, dose_required, dose_mode, suggested_dose, block_type=None):
        self.block_type = block_type
        self.record = record
        self.score = score
        self.reasons = reasons
        self.fields = fields
        self.dose_required = dose_required
        self.dose_mode = dose_mode
        self.suggested_dose = suggested_dose

    def payload(self, media: Mapping[str, Any]) -> dict[str, Any]:
        from api.rep_range import progression_context

        entry = self.record["entry"]
        return {
            "catalogue_id": entry["catalogue_id"], "exercise_key": entry["exercise_key"], "name": entry["name"],
            "reason": " · ".join(self.reasons[:2]) or None,
            "equipment": _equipment_labels(self.record) or None,
            "media": media.get(entry["exercise_key"]),
            "prescription": None if self.dose_required else prescription_summary(self.fields),
            "dose_required": self.dose_required, "dose_mode": self.dose_mode,
            "suggested_dose": self.suggested_dose,
            **progression_context({**_base_block(self.record, block_type=self.block_type or "accessory"),
                                   "prescription_source": "athlete"},
                                  None, None, phase="", d_day=None, athlete={}),
        }


def _relevance(raw: Mapping[str, Any], athlete: AthleteContext) -> tuple[int, list[str]]:
    tags = _tags(raw) | {g.lower() for g in _strings(raw.get("goals"))}
    weak = {w.lower() for w in _strings(raw.get("weaknesses"))}
    styles = {s.lower() for s in _strings(raw.get("tactical_styles"))} | tags
    score, reasons = 0, []
    goals = sorted({athlete.goal_tags[t] for t in tags if t in athlete.goal_tags})
    if goals:
        score += min(16, 8 * len(goals))
        reasons.append(f"Builds your {_label(goals[0])} goal")
    weaknesses = sorted({athlete.weakness_tags[t] for t in (tags | weak) if t in athlete.weakness_tags})
    if weaknesses:
        score += min(16, 8 * len(weaknesses))
        reasons.append(f"Targets your {_label(weaknesses[0])}")
    if athlete.style_tags & styles or athlete.styles & styles:
        score += 5
        if not reasons:
            reasons.append("Fits your fighting style")
    return score, reasons


def _purpose_evidence(original: Mapping[str, Any], candidate: Mapping[str, Any], source: str) -> tuple[int, list[str]] | None:
    """Score how far the candidate preserves the original's training purpose.

    Metadata matches rank first. A useful exercise word can also establish a
    related movement, even when the bank uses different pattern/category labels.
    """
    if source in _CONDITIONING_SOURCES:
        same_system = _clean(original.get("system")).lower() == _clean(candidate.get("system")).lower() != ""
        if not same_system:
            return _name_evidence(original, candidate)
        score, reasons = 40, [f"Same {_clean(candidate.get('system'))} energy system"]
        if _clean(original.get("modality")).lower() == _clean(candidate.get("modality")).lower() != "":
            score += 10
        overlap = _movement_tags(original) & _movement_tags(candidate)
        score += min(15, 3 * len(overlap))
        return score, reasons
    movement = _clean(original.get("movement")).lower()
    same_movement = bool(movement) and movement not in _GENERIC_MOVEMENTS and movement == _clean(candidate.get("movement")).lower()
    same_category = _clean(original.get("category")).lower() == _clean(candidate.get("category")).lower() != ""
    same_method = _clean(original.get("method")).lower() == _clean(candidate.get("method")).lower() != ""
    overlap = _movement_tags(original) & _movement_tags(candidate) - {"compound", "rehab_friendly"}
    # A shared pattern label is only evidence within the same body region; across
    # regions ("rotational" core twist vs lateral lunge) it is a coincidence.
    if not ((same_movement and same_category) or (same_category and same_method and len(overlap) >= 2)):
        return _name_evidence(original, candidate)
    score, reasons = 0, []
    category = _CATEGORY_LABELS.get(_clean(candidate.get("category")).lower())
    if same_movement:
        score += 40
        reasons.append(f"Same {_label(movement)} pattern")
    if same_method:
        score += 20
        if not same_movement and category:
            reasons.append(f"{category} {_clean(candidate.get('method')).lower()} focus")
    if same_category:
        score += 15
    if _clean(original.get("type")).lower() == _clean(candidate.get("type")).lower() != "":
        score += 5
    score += min(20, 4 * len(overlap))
    mech = {t for t in _tags(original) if t.startswith("mech_")} & {t for t in _tags(candidate) if t.startswith("mech_")}
    score += min(5, len(mech))
    return score, reasons


def _name_evidence(original: Mapping[str, Any], candidate: Mapping[str, Any]) -> tuple[int, list[str]] | None:
    # Avoid treating shared adjectives (heavy, alternating, bodyweight) as an
    # exercise match, or matching "lunge" inside an unrelated word.
    words = {"lunge", "squat", "deadlift", "hinge", "press", "push", "pull", "row", "run", "sprint",
             "carry", "slam", "throw", "rope", "plank", "jump", "hop", "curl", "raise", "crunch",
             "bridge", "rollout", "rotation", "punch", "shadowboxing", "bike", "cycle"}
    def tokens(raw):
        names = str(raw.get("name") or raw.get("display_name") or "").lower()
        found = set(re.findall(r"[a-z]+", names))
        return found | {t[:-1] for t in found if t.endswith("s")} | ({"run"} if "running" in found else set())
    shared = sorted(tokens(original) & tokens(candidate) & words)
    return (25 + len(shared), [f"Related {shared[0]} movement"]) if shared else None


def _taken_identities(occurrence: Occurrence) -> set[str]:
    from api.services.today_service import _iter_mapping_items

    return {block_identity(b) for s in _iter_mapping_items(occurrence.day.get("sessions"))
            for b in _iter_mapping_items(s.get("blocks"))}


def _readiness_reduced(store: Any, athlete_id: str, occurrence: Occurrence) -> bool:
    if occurrence.date.isoformat() != occurrence.training_day:
        return False
    try:
        checkin = store.get_today_checkin(athlete_id, _clean(occurrence.plan_row.get("id")), occurrence.training_day)
    except Exception:  # noqa: BLE001 - readiness only narrows the options
        logger.exception("[exercise_customisation] readiness unavailable")
        return True
    return _clean((checkin or {}).get("recommendation_state")) in {"modify", "pull_back"}


def swap_candidates(
    store: Any, *, athlete_id: str, occurrence: Occurrence, safety: SafetyContext | None = None,
) -> tuple[list[Candidate], str | None]:
    """Every eligible replacement for the occurrence's block, best first."""
    block = occurrence.block or {}
    index = _library_index()
    original = _record_for_block(index, block)
    sources = _SOURCES_BY_BLOCK_TYPE.get(_clean(block.get("block_type")), ())
    athlete = AthleteContext(_intake_for_plan(store, occurrence.plan_row, athlete_id))

    original_raw = {**(original["entry"]["raw"] if original else block),
                    "name": original["entry"]["name"] if original else block.get("display_name")}
    source = original["entry"]["source"] if original else next(iter(sources), "")
    family = _family(original_raw, source)
    ceiling = {field: _cost(original_raw, field) for field in _COST_FIELDS}
    reduced = _readiness_reduced(store, athlete_id, occurrence)
    taken = _taken_identities(occurrence)
    original_mode = _block_mode(block)
    original_total = _total_cost(original_raw)
    candidates: list[Candidate] = []
    for record in index.records:
        entry = record["entry"]
        raw = entry["raw"]
        if (record["summary"]["category"] != "training" or entry["source"] not in sources
                or not entry["exercise_key"] or (original and entry["catalogue_id"] == original["entry"]["catalogue_id"])):
            continue
        if entry["exercise_key"] in taken or slug(entry["name"]) in taken or entry["exercise_key"] == block_identity(block):
            continue
        if family and _family(raw, entry["source"]) != family:
            continue
        if not _sport_allows(record, athlete) or not _phase_allows(raw, occurrence.phase) or not _equipment_available(record, athlete):
            continue
        stress_ceiling = ceiling if reduced else {k: v for k, v in ceiling.items() if raw.get(k) is not None}
        if not _no_more_stress(raw, stress_ceiling):
            continue
        evidence = _purpose_evidence(original_raw, {**raw, "name": entry["name"]}, source)
        if evidence is None:
            continue
        score, reasons = evidence
        relevance, relevance_reasons = _relevance(raw, athlete)
        score += relevance
        lighter = _total_cost(raw) < original_total
        if lighter:
            score += 6 if reduced else 2
        if record["entry"]["exercise_key"]:
            score += 1
        reasons = [*reasons[:1], *relevance_reasons[:1]]
        if reduced and lighter:
            reasons.insert(0, "Lighter for today's readiness")
        elif lighter and len(reasons) < 2 and original_total - _total_cost(raw) >= 2:
            reasons.append("Less load on the body")
        mode, bank_dose = _candidate_mode(record)
        fields: dict[str, Any] = {}
        dose_required = False
        suggested = None
        if mode and original_mode == mode:
            fields = _carry_dose(block, mode)
        elif bank_dose:
            fields = dose_block_fields(bank_dose)
            suggested = bank_dose
        else:
            dose_required = True
        if not dose_required and not prescription_summary(fields):
            dose_required = True
        if dose_required and suggested is None:
            suggested = dose_guidance(record)[2]
        candidates.append(Candidate(record, score, reasons, fields, dose_required, mode or "reps", suggested))
    candidates.sort(key=lambda c: (-c.score, c.record["entry"]["name"], c.record["entry"]["catalogue_id"]))
    notice = None
    if not candidates:
        notice = "No close match fits your equipment and plan right now."
    return candidates, notice


def add_candidates(
    store: Any, *, athlete_id: str, occurrence: Occurrence, safety: SafetyContext | None = None,
) -> tuple[list[Candidate], str | None]:
    """Exercises that suit this session's role, best first."""
    from api.services.today_service import _iter_mapping_items

    session_type = _clean(occurrence.session.get("session_type"))
    sources = _SOURCES_BY_SESSION_TYPE.get(session_type)
    if session_type in {"skill", "sparring"} and session_can_add(occurrence.session):
        sources = tuple({r["entry"]["source"] for r in _library_index().records
                         if r["entry"].get("content_class") == "technical_coordination"})
    if not sources:
        # No ranked suggestions for this kind of work; the library search is open.
        return [], "Search the library to add an exercise."
    index = _library_index()
    athlete = AthleteContext(_intake_for_plan(store, occurrence.plan_row, athlete_id))

    blocks = list(_iter_mapping_items(occurrence.session.get("blocks")))
    existing = [(b, r) for b in blocks if (r := _record_for_block(index, b)) is not None]
    # The session's heaviest known cost bounds an addition: adding work is the
    # athlete's choice, adding a heavier kind of work is not implied by it.
    ceiling: dict[str, int | None] = {}
    for field in _COST_FIELDS:
        values = [v for _, r in existing if (v := _cost(r["entry"]["raw"], field)) is not None]
        ceiling[field] = max(values) if values else (_COST_RANK["moderate"] if field == "cns_load" else None)
    covered = {_clean(r["entry"]["raw"].get("movement")).lower() for _, r in existing}
    taken = _taken_identities(occurrence)
    reduced = _readiness_reduced(store, athlete_id, occurrence)
    candidates: list[Candidate] = []
    for record in index.records:
        entry = record["entry"]
        raw = entry["raw"]
        if record["summary"]["category"] != "training" or entry["source"] not in sources or not entry["exercise_key"]:
            continue
        if entry["exercise_key"] in taken or slug(entry["name"]) in taken:
            continue
        family = "skill" if entry.get("content_class") == "technical_coordination" else _family(raw, entry["source"])
        if family is None:
            continue
        if not _sport_allows(record, athlete) or not _phase_allows(raw, occurrence.phase) or not _equipment_available(record, athlete):
            continue
        if not _no_more_stress(raw, {k: v for k, v in ceiling.items() if v is not None and raw.get(k) is not None}):
            continue
        if reduced and _total_cost(raw) > _COST_RANK["low"] * 3:
            continue
        block_type = "skill" if family == "skill" else "conditioning" if family == "conditioning" else "plyometric_power" if family == "power" else (
            "strength" if "compound" in _tags(raw) else "accessory")
        score, reasons = _relevance(raw, athlete)
        movement = _clean(raw.get("movement")).lower()
        if movement and movement not in covered and movement not in _GENERIC_MOVEMENTS:
            score += 10
            if len(reasons) < 2:
                reasons.append(f"Adds {_label(movement)} work")
        if not reasons:
            # Without real evidence for this athlete it stays out of the top picks.
            score -= 50
            category = _CATEGORY_LABELS.get(_clean(raw.get("category")).lower())
            if category:
                reasons.append(f"{category} {family}")
        score -= _total_cost(raw)
        mode, bank_dose = _candidate_mode(record)
        fields, suggested, dose_required = {}, None, True
        if bank_dose:
            fields, suggested, dose_required = dose_block_fields(bank_dose), bank_dose, False
        else:
            role_match = _role_dose(record, existing, mode)
            if role_match:
                fields, dose_required = role_match, False
        if dose_required:
            suggested = dose_guidance(record)[2]
        candidates.append(Candidate(record, score, reasons, fields, dose_required, mode or "reps", suggested,
                                    block_type=block_type))
    candidates.sort(key=lambda c: (-c.score, c.record["entry"]["name"], c.record["entry"]["catalogue_id"]))
    notice = None
    if not candidates:
        notice = "Nothing suitable to add with your equipment right now."
    return candidates, notice


def _added_block_type(record: Mapping[str, Any]) -> str:
    entry = record["entry"]
    family = _family(entry["raw"], entry["source"])
    if family == "conditioning":
        return "conditioning"
    if family == "power":
        return "plyometric_power"
    if family == "strength":
        return "strength" if "compound" in _tags(entry["raw"]) else "accessory"
    return "skill" if entry.get("content_class") == "technical_coordination" else "accessory"


def _library_candidate(
    record: Mapping[str, Any], *, occurrence: Occurrence, athlete: AthleteContext, safety: SafetyContext | None,
    existing: list, taken: set[str],
) -> Candidate | str | None:
    """One exercise the athlete picked from the library, or why it can't be added.

    An explicit pick is the athlete's choice: purpose, phase, equipment and
    session-fit rankings and injury guidance do not restrict it. Identity
    rules still apply (training category, not already in this workout).
    """
    entry = record["entry"]
    raw = entry["raw"]
    if record["summary"]["category"] != "training" or not entry["exercise_key"] or not _sport_allows(record, athlete):
        return None
    if entry["exercise_key"] in taken or slug(entry["name"]) in taken:
        return None
    block_type = _added_block_type(record)
    _, reasons = _relevance(raw, athlete)
    if not reasons:
        context = [part for part in _clean(record["summary"].get("context")).split(" · ") if part][:2]
        if context:
            reasons = [" · ".join(context)]
    mode, bank_dose = _candidate_mode(record)
    fields, suggested, dose_required = {}, None, True
    if bank_dose:
        fields, suggested, dose_required = dose_block_fields(bank_dose), bank_dose, False
    elif (role_match := _role_dose(record, existing, mode)):
        fields, dose_required = role_match, False
    if dose_required:
        suggested = dose_guidance(record)[2]
    return Candidate(record, 0, reasons, fields, dose_required, mode or "reps", suggested, block_type=block_type)


def library_add_candidates(
    store: Any, *, athlete_id: str, occurrence: Occurrence, query: str, safety: SafetyContext | None = None,
) -> tuple[list[Candidate], str | None]:
    """The whole training library, searched with the library's own ranking."""
    from api.services.today_service import _iter_mapping_items
    from api.services.training_library import search

    index = _library_index()
    athlete = AthleteContext(_intake_for_plan(store, occurrence.plan_row, athlete_id))

    blocks = list(_iter_mapping_items(occurrence.session.get("blocks")))
    existing = [(b, r) for b in blocks if (r := _record_for_block(index, b)) is not None]
    taken = _taken_identities(occurrence)
    results = search(index, q=query, category="training", limit=50)["entries"]
    candidates: list[Candidate] = []
    for summary in results:
        record = index.by_id.get(summary["catalogue_id"])
        if record is None:
            continue
        candidate = _library_candidate(record, occurrence=occurrence, athlete=athlete, safety=safety,
                                       existing=existing, taken=taken)
        if isinstance(candidate, Candidate):
            candidates.append(candidate)
    notice = None
    if not candidates:
        notice = "No library exercise matches that search."
    return candidates, notice


def library_pick(
    store: Any, *, athlete_id: str, occurrence: Occurrence, catalogue_id: str, safety: SafetyContext | None,
) -> Candidate | None:
    from api.services.today_service import _iter_mapping_items

    index = _library_index()
    record = index.by_id.get(catalogue_id)
    if record is None:
        return None
    athlete = AthleteContext(_intake_for_plan(store, occurrence.plan_row, athlete_id))
    blocks = list(_iter_mapping_items(occurrence.session.get("blocks")))
    existing = [(b, r) for b in blocks if (r := _record_for_block(index, b)) is not None]
    candidate = _library_candidate(record, occurrence=occurrence, athlete=athlete, safety=safety,
                                   existing=existing, taken=_taken_identities(occurrence))
    return candidate if isinstance(candidate, Candidate) else None


def _role(record: Mapping[str, Any]) -> str:
    raw = record["entry"]["raw"]
    if _clean(raw.get("movement")).lower() in _ISOMETRIC_MOVEMENTS:
        return "isometric"
    family = _family(raw, record["entry"]["source"])
    if family == "strength":
        return "main" if "compound" in _tags(raw) and _cost(raw, "cns_load") not in (None, 0, 1) else "accessory"
    return family or "other"


def _role_dose(record: Mapping[str, Any], existing: list, mode: str | None) -> dict[str, Any] | None:
    """A dose from a block in this session with the same role, never a neighbour by position."""
    role = _role(record)
    for block, other in existing:
        if _role(other) == role and mode and _block_mode(block) == mode:
            fields = _carry_dose(block, mode)
            if prescription_summary(fields):
                return fields
    return None


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------


def _scopes(occurrence: Occurrence) -> list[dict[str, str]]:
    scopes = [{"value": "occurrence", "label": "Just this workout"}]
    if occurrence.is_open_plan:
        scopes.append({"value": "future", "label": f"Every {occurrence.date.strftime('%A')}"})
    return scopes


def exercise_options(
    store: Any, *, athlete_id: str, athlete_timezone: str | None, plan_id: str, day: str, session_id: str,
    block_id: str, show_all: bool = False, now: datetime | None = None,
) -> dict[str, Any]:
    occurrence = locate_occurrence(store, athlete_id=athlete_id, athlete_timezone=athlete_timezone,
                                   plan_id=plan_id, day=day, session_id=session_id, block_id=block_id, now=now)
    require_editable_day(store, occurrence, athlete_id)
    if not block_is_editable(occurrence.block or {}):
        raise HTTPException(409, "This exercise stays as prescribed.")
    candidates, notice = swap_candidates(store, athlete_id=athlete_id, occurrence=occurrence)
    media = load_media_index(store)
    limit = ALL_LIMIT if show_all else TOP_LIMIT
    return {
        "alternatives": [c.payload(media) for c in candidates[:limit]],
        "more_available": len(candidates) > limit,
        "notice": notice,
        "scopes": _scopes(occurrence),
    }


def addition_options(
    store: Any, *, athlete_id: str, athlete_timezone: str | None, plan_id: str, day: str, session_id: str,
    show_all: bool = False, query: str = "", now: datetime | None = None,
) -> dict[str, Any]:
    occurrence = locate_occurrence(store, athlete_id=athlete_id, athlete_timezone=athlete_timezone,
                                   plan_id=plan_id, day=day, session_id=session_id, now=now)
    require_editable_day(store, occurrence, athlete_id)
    if not session_can_add(occurrence.session):
        raise HTTPException(409, "Exercises can't be added to this session.")
    if query.strip():
        candidates, notice = library_add_candidates(store, athlete_id=athlete_id, occurrence=occurrence,
                                                    query=query.strip())
        show_all = True
    else:
        candidates, notice = add_candidates(store, athlete_id=athlete_id, occurrence=occurrence)
    media = load_media_index(store)
    limit = ALL_LIMIT if show_all else TOP_LIMIT
    return {
        "alternatives": [c.payload(media) for c in candidates[:limit]],
        "more_available": len(candidates) > limit,
        "notice": notice,
        "scopes": _scopes(occurrence),
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _write(store: Any, plan_row: Mapping[str, Any], athlete_id: str, mutate) -> list[dict[str, Any]]:
    """Read-modify-write with compare-and-swap; one retry on a concurrent write."""
    plan_id = _clean(plan_row.get("id"))
    row = plan_row
    for _ in range(2):
        entries = stored_overrides(row)
        updated = mutate(deepcopy(entries))
        if updated is None:
            return entries
        saved = store.compare_and_set_plan_exercise_overrides(
            plan_id, athlete_id, expected_revision=int(row.get("exercise_overrides_revision") or 0),
            overrides=updated,
        )
        if saved is not None:
            return updated
        row = store.get_training_plan_for_athlete(plan_id, athlete_id)
        if row is None:
            raise HTTPException(404, "plan not found")
    raise HTTPException(409, "Your plan changed on another device. Refresh and try again.")


_MAIN_WORK = {"strength": "strength", "strength_speed": "power", "plyometric_power": "power", "speed": "speed",
              "conditioning": "conditioning"}


def _removal_notice(occurrence: Occurrence, athlete: AthleteContext) -> str | None:
    """Why removing this block changes the session materially, if it does.

    Material means: it is the session's only main work of its kind, or the only
    work covering one of the athlete's goals. Accessories never ask.
    """
    from api.services.today_service import _iter_mapping_items

    block = occurrence.block or {}
    blocks = list(_iter_mapping_items(occurrence.session.get("blocks")))
    block_type = _clean(block.get("block_type"))
    word = _MAIN_WORK.get(block_type)
    if word and sum(_clean(b.get("block_type")) == block_type for b in blocks) == 1:
        return f"This is the only {word} work in this session. Remove it anyway?"
    index = _library_index()
    record = _record_for_block(index, block)
    if record is None:
        return None
    goals = [g for g in dict.fromkeys([athlete.primary_goal, *sorted(athlete.goals)]) if g]
    tags = _tags(record["entry"]["raw"])
    for goal in goals:
        goal_tags = set(GOAL_TAG_MAP.get(goal, []))
        if not goal_tags & tags:
            continue
        others = [r for b in blocks if b is not block and (r := _record_for_block(index, b)) is not None
                  and goal_tags & _tags(r["entry"]["raw"])]
        if not others:
            return f"This is today's main work for your {_label(goal)} goal. Remove it anyway?"
    return None


def _root_target(entries: list[dict[str, Any]], block: Mapping[str, Any], scope: str) -> tuple[str, str]:
    """``(block_id, target_key)`` a new change should follow.

    A recurring change made on a one-off replacement follows the exercise the
    plan prescribes there, so it still applies on the weeks without that one-off.
    """
    change = block.get("athlete_change") if isinstance(block.get("athlete_change"), Mapping) else None
    if change and scope == "future" and change.get("scope") == "occurrence":
        source = next((e for e in entries if e.get("id") == change.get("override_id")), None)
        if source:
            return _clean(source.get("block_id")), _clean(source.get("target_key"))
    return _clean(block.get("block_id")), block_identity(block)


def apply_change(
    store: Any, *, athlete_id: str, athlete_timezone: str | None, plan_id: str, payload: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate and record one swap / add / remove. Idempotent on ``client_request_id``."""
    change_id = _clean(payload.get("client_request_id"))
    action = payload.get("action")
    scope = payload.get("scope") or "occurrence"
    plan_row = store.get_training_plan_for_athlete(plan_id, athlete_id)
    if plan_row is None:
        raise HTTPException(404, "plan not found")
    existing = next((e for e in stored_overrides(plan_row) if e.get("id") == change_id), None)
    if existing is not None:
        return {**_change_result(existing), "session": effective_session(
            store, athlete_id=athlete_id, athlete_timezone=athlete_timezone, plan_id=plan_id,
            day=_clean(existing.get("date")), session_id=_clean(existing.get("session_id")), now=now)}
    occurrence = locate_occurrence(
        store, athlete_id=athlete_id, athlete_timezone=athlete_timezone, plan_id=plan_id,
        day=payload.get("date"), session_id=_clean(payload.get("session_id")),
        block_id=None if action == "add" else _clean(payload.get("block_id")), now=now,
    )
    require_editable_day(store, occurrence, athlete_id)
    if scope == "future" and not occurrence.is_open_plan:
        raise HTTPException(422, "Future changes are available on open plans only.")
    athlete = AthleteContext(_intake_for_plan(store, occurrence.plan_row, athlete_id))
    now_iso = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    entry: dict[str, Any] = {
        "id": change_id, "action": action, "scope": scope, "date": occurrence.date.isoformat(),
        "weekday": weekday_token(occurrence.date), "session_id": _clean(occurrence.session.get("session_id")),
        "session_slot": session_slot(occurrence.session, position=occurrence.day["sessions"].index(occurrence.session)),
        "created_at": now_iso, "revoked_at": None,
    }
    if action in {"swap", "remove", "edit"}:
        block = occurrence.block or {}
        if not block_is_editable(block):
            raise HTTPException(409, "This exercise stays as prescribed.")
        target_block_id, target_key = _root_target(stored_overrides(occurrence.plan_row), block, scope)
        entry.update(block_id=target_block_id, target_key=target_key, original=_snapshot(block))
    if action == "remove":
        from api.services.today_service import _iter_mapping_items

        remaining = [b for b in _iter_mapping_items(occurrence.session.get("blocks"))
                     if _clean(b.get("block_id")) != _clean((occurrence.block or {}).get("block_id"))]
        technical = _clean(occurrence.session.get("session_type")) in {"skill", "sparring"} and session_can_add(occurrence.session)
        if not remaining and not technical:
            raise HTTPException(409, "Keep at least one exercise. Swap it, or skip the session instead.")
        if not payload.get("confirmed"):
            notice = _removal_notice(occurrence, athlete)
            if notice:
                raise HTTPException(409, {"code": "confirm_remove", "message": notice})
    elif action == "edit":
        new_block = deepcopy(dict(occurrence.block or {}))
        if payload.get("dose") is not None:
            dumped = _validated_dose(payload["dose"])
            # A changed prescription invalidates the old AI promise. The athlete
            # may supply their own instruction below; never regenerate one.
            new_block.pop("progression_rule", None)
            new_block["regression_options"] = []
            new_block = _edited_dose(new_block, dumped)
            # A deload (same exercise, nothing raised) never needs clearance.
            # Anything heavier or not comparable is validated against today's
            # injury guidance, scoped to the regions the exercise loads.
            if not _dose_not_heavier(occurrence.block or {}, new_block):
                safety = SafetyContext(store, athlete_id=athlete_id, occurrence=occurrence,
                                       equipment=sorted(athlete.equipment))
                record = _record_for_block(_library_index(), new_block)
                raw = record["entry"]["raw"] if record else new_block
                probe = {**{field: deepcopy(raw[field]) for field in ("mechanical_load_regions", "mechanical_risk_tags")
                            if raw.get(field) is not None and new_block.get(field) is None}, **new_block}
                if safety.contraindicated(raw) or not safety.allows(probe, raw, prescribed=True):
                    raise HTTPException(409, "That prescription doesn't fit your current injury guidance.")
        for field in ("coaching_cues", "stop_rules", "regression_options"):
            if payload.get(field) is not None:
                values = payload[field]
                if any(not isinstance(value, str) or len(value.strip()) > 500 for value in values):
                    raise HTTPException(422, "Keep each cue and stop rule under 500 characters.")
                new_block[field] = [value.strip() for value in values if value.strip()]
        if payload.get("progression_rule") is not None:
            # An empty rule clears it, like an emptied cue list.
            new_block["progression_rule"] = _clean(payload["progression_rule"]) or None
        entry["block"] = new_block
    else:
        safety = None
        if action == "swap":
            candidates, _ = swap_candidates(store, athlete_id=athlete_id, occurrence=occurrence, safety=safety)
        else:
            if not session_can_add(occurrence.session):
                raise HTTPException(409, "Exercises can't be added to this session.")
            candidates, _ = add_candidates(store, athlete_id=athlete_id, occurrence=occurrence, safety=safety)
        catalogue_id = _clean(payload.get("catalogue_id"))
        chosen = next((c for c in candidates if c.record["entry"]["catalogue_id"] == catalogue_id), None)
        if chosen is None and action == "add":
            # An explicit library choice is independent of injury guidance.
            chosen = library_pick(store, athlete_id=athlete_id, occurrence=occurrence,
                                  catalogue_id=catalogue_id, safety=safety)
        if chosen is None:
            # Reject unavailable catalogue identities.
            raise HTTPException(409, "That exercise isn't available for this workout anymore. Choose another.")
        fields = chosen.fields
        dose = payload.get("dose")
        if dose is not None:
            dumped = _validated_dose(dose)
            fields = _dose_fields(dumped)
        elif chosen.dose_required:
            raise HTTPException(422, "Set a dose for this exercise first.")
        block_type = (_clean((occurrence.block or {}).get("block_type")) if action == "swap"
                      else chosen.block_type or "accessory")
        new_block = {**_base_block(chosen.record, block_type=block_type), **deepcopy(fields)}
        if is_custom_plan(plan_row):
            new_block["prescription_source"] = "athlete"
            new_block["source_session_id"] = entry["session_id"]
        if action == "add":
            entry.update(block_id=None, target_key=None, original=None)
        entry["block"] = new_block
        entry["reason"] = " · ".join(chosen.reasons[:2]) or None

    def mutate(entries: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        if any(e.get("id") == change_id for e in entries):
            return None
        return [*entries, entry]

    saved = _write(store, occurrence.plan_row, athlete_id, mutate)
    result = _change_result(next(e for e in saved if e.get("id") == change_id))
    result["session"] = effective_session(store, athlete_id=athlete_id, athlete_timezone=athlete_timezone,
                                          plan_id=plan_id, day=occurrence.date.isoformat(),
                                          session_id=entry["session_id"], now=now)
    return result


def _dose_mode(block: Mapping[str, Any]) -> str:
    saved = block.get("athlete_dose") if isinstance(block.get("athlete_dose"), Mapping) else {}
    return saved.get("mode") or (
        "intervals" if block.get("rounds") and block.get("work") else
        "distance" if block.get("distance") else "duration" if block.get("duration") else
        "reps" if block.get("sets") and block.get("reps") else "text")


def _edited_dose(original: dict, dose: dict) -> dict:
    """Keep prescription metadata that the editor cannot represent unchanged."""
    old_dose = original.get("athlete_dose") or {}
    old_mode = _dose_mode(original)
    fields = _dose_fields(dose)
    result = deepcopy(original)
    for field in ("sets", "reps", "rounds", "duration", "distance", "work", "prescription_text", "athlete_dose"):
        result.pop(field, None)
    if old_mode != dose["mode"]:
        for field in ("rest", "load", "intensity", "tempo"):
            result.pop(field, None)
    # RIR, percentage and other effort methods are not an empty RPE target.
    # Only an explicitly supplied RPE replaces the existing effort method.
    if dose.get("rpe") is None:
        fields.pop("effort", None)
    if old_mode == dose["mode"]:
        old_load = original.get("load") or {}
        if dose.get("load_or_intensity") == (old_load.get("display") or original.get("intensity")):
            fields.pop("load", None)
            fields.pop("intensity", None)
        elif dose.get("load_or_intensity"):
            result.pop("load", None)
            result.pop("intensity", None)
        if dose.get("tempo") and dose.get("tempo") != old_dose.get("tempo"):
            result.pop("tempo", None)
        for field in ("duration", "work", "rest"):
            old = original.get(field) or {}
            new = fields.get(field) or {}
            factor = 60 if str(old.get("unit", "")).lower().startswith("min") else 1
            if isinstance(old.get("value"), (int, float)) and old["value"] * factor == new.get("value"):
                fields[field] = deepcopy(old)
    result.update(fields)
    return result


_DISTANCE_METRES = {"m": 1.0, "km": 1000.0, "mi": 1609.344, "yd": 0.9144}


def _magnitude(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    numbers = re.findall(r"\d+(?:\.\d+)?", value) if isinstance(value, str) else []
    # A range such as "8-10" is as heavy as its top end.
    return max(float(n) for n in numbers) if numbers else None


def _dose_magnitudes(block: Mapping[str, Any]) -> dict[str, float]:
    """Comparable dose quantities, normalised to one unit each."""
    out: dict[str, float] = {}
    for field in ("sets", "reps", "rounds"):
        if (value := _magnitude(block.get(field))) is not None:
            out[field] = value
    for field in ("duration", "work"):
        if (value := _seconds(block.get(field))) is not None:
            out[field] = value
    spec = block.get("distance")
    if isinstance(spec, Mapping) and (value := _magnitude(spec.get("value"))) is not None:
        factor = _DISTANCE_METRES.get(str(spec.get("unit", "")).lower())
        if factor:
            out["distance"] = value * factor
    load = block.get("load")
    if isinstance(load, Mapping) and load.get("method") == "absolute" and (value := _magnitude(load.get("value"))) is not None:
        unit = str(load.get("unit", "")).lower()
        if unit in {"kg", "lb", "lbs"}:
            out["load"] = value * (0.45359237 if unit.startswith("lb") else 1)
    effort = block.get("effort")
    if isinstance(effort, Mapping) and effort.get("method") == "RPE" and (value := _magnitude(effort.get("value"))) is not None:
        out["rpe"] = value
    return out


_NO_ADDED_LOAD = {"", "bodyweight", "body weight", "bw", "unloaded", "no load", "none"}


def _intensity(block: Mapping[str, Any]) -> str:
    text = _clean(block.get("intensity")).lower()
    # Bodyweight adds nothing, so it reads the same as no stated intensity.
    return "" if text in _NO_ADDED_LOAD else text


def _seconds(spec: Any) -> float | None:
    if not isinstance(spec, Mapping) or (value := _magnitude(spec.get("value"))) is None:
        return None
    unit = str(spec.get("unit", "")).lower()
    return value * (3600 if unit.startswith("h") else 60 if unit.startswith("min") else 1)


def _dose_extra(block: Mapping[str, Any], field: str) -> Any:
    saved = block.get("athlete_dose") if isinstance(block.get("athlete_dose"), Mapping) else {}
    value = block.get(field)
    return saved.get(field) if value is None else value


def _dose_not_heavier(original: Mapping[str, Any], edited: Mapping[str, Any]) -> bool:
    """True when the edit only keeps or lowers the dose of the same exercise.

    The prescription mode must be unchanged and every quantity the edit states
    comparable to the original and no higher. Free text, a new dimension, a
    changed effort/intensity that cannot be compared numerically, shorter rest,
    a changed tempo or a switch to per-side work is not provably lighter.
    """
    mode = _dose_mode(edited)
    if mode != _dose_mode(original):
        return False
    if mode == "text" and _clean(edited.get("prescription_text")) != _clean(original.get("prescription_text")):
        return False
    rest_before, rest_after = _seconds(original.get("rest")), _seconds(edited.get("rest"))
    if rest_after is not None and (rest_before is None or rest_after < rest_before):
        return False
    if _dose_extra(edited, "tempo") and _dose_extra(edited, "tempo") != _dose_extra(original, "tempo"):
        return False
    if _dose_extra(edited, "per_side") and not _dose_extra(original, "per_side"):
        return False
    before, after = _dose_magnitudes(original), _dose_magnitudes(edited)
    if any(field not in before or value > before[field] for field, value in after.items()):
        return False
    if _intensity(edited) != _intensity(original) and "load" not in after:
        return False
    if edited.get("effort") != original.get("effort") and "rpe" not in after:
        return False
    return True


def _validated_dose(dose: Mapping[str, Any]) -> dict:
    from api.services.custom_plans import dose_incomplete

    try:
        dumped = CustomDose.model_validate(dose).model_dump(mode="json", exclude_none=True)
    except ValueError:
        raise HTTPException(422, "Check the dose, weight, rest and effort.") from None
    if dose_incomplete(dumped):
        raise HTTPException(422, "Finish the dose before saving it.")
    return dumped


def _dose_fields(dose: dict) -> dict:
    fields = dose_block_fields(dose)
    fields.update(prescription_source="athlete", athlete_dose=deepcopy(dose), prescription_text=dose_text(dose))
    return fields


def effective_session(
    store: Any, *, athlete_id: str, athlete_timezone: str | None, plan_id: str, day: str, session_id: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The occurrence's session as every surface now renders it, for an instant card update."""
    from api.structured_plan_models import Session

    try:
        occurrence = locate_occurrence(store, athlete_id=athlete_id, athlete_timezone=athlete_timezone,
                                       plan_id=plan_id, day=day, session_id=session_id, now=now)
    except HTTPException:
        return None
    try:
        return Session.model_validate(occurrence.session).model_dump(mode="json")
    except ValueError:
        return None


def _change_result(entry: Mapping[str, Any]) -> dict[str, Any]:
    block = entry.get("block") if isinstance(entry.get("block"), Mapping) else {}
    original = entry.get("original") if isinstance(entry.get("original"), Mapping) else {}
    return {
        "id": entry.get("id"), "action": entry.get("action"), "scope": entry.get("scope"),
        "date": entry.get("date"), "name": block.get("display_name") or original.get("display_name"),
        "original_name": original.get("display_name"), "revoked": bool(entry.get("revoked_at")),
    }


def undo_change(
    store: Any, *, athlete_id: str, athlete_timezone: str | None, plan_id: str, change_id: str,
    from_day: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Undo or restore one change without touching any started or past workout.

    A one-off change is revoked. A recurring change is revoked only if it has
    not yet applied to a past or started workout; otherwise it ends before the
    first date that can still change, so history keeps what was prescribed.
    """
    from api.services.today_service import _require_valid_plan_id, resolve_training_day

    _require_valid_plan_id(plan_id)
    plan_row = store.get_training_plan_for_athlete(plan_id, athlete_id)
    if plan_row is None:
        raise HTTPException(404, "plan not found")
    entry = next((e for e in stored_overrides(plan_row) if e.get("id") == change_id), None)
    if entry is None:
        raise HTTPException(404, "That change no longer exists.")
    if entry.get("revoked_at"):
        return _change_result(entry)
    today = date.fromisoformat(resolve_training_day(athlete_timezone, now=now))
    start = _parse_day(entry.get("date"))
    first_open = today if not _occurrence_locked(store, athlete_id, plan_id, today) else today + timedelta(days=1)
    now_iso = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    if entry.get("scope") == "occurrence":
        if start < first_open:
            raise HTTPException(409, "This workout has started. Its exercises stay as logged.")
        end = None
    else:
        requested = _parse_day(from_day, field="restore date") if from_day else start
        end = max(requested, first_open, start)
    if end is not None and end > start:
        update = {"ends_before": end.isoformat()}
    else:
        update = {"revoked_at": now_iso}

    def mutate(entries: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        target = next((e for e in entries if e.get("id") == change_id), None)
        if target is None or target.get("revoked_at"):
            return None
        target.update(update)
        return entries

    saved = _write(store, plan_row, athlete_id, mutate)
    target_day = (_parse_day(from_day, field="restore date") if from_day else start)
    return {**_change_result(next(e for e in saved if e.get("id") == change_id)), "revoked": True,
            "session": effective_session(store, athlete_id=athlete_id, athlete_timezone=athlete_timezone,
                                         plan_id=plan_id, day=max(target_day, first_open).isoformat(),
                                         session_id=_clean(entry.get("session_id")), now=now)}


__all__ = [
    "addition_options",
    "apply_change",
    "block_is_editable",
    "exercise_options",
    "prescription_summary",
    "undo_change",
]
