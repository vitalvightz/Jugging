"""Resolve effective physical prescriptions from scheduled-day/context envelopes.

Conditioning consumes closed composition ceilings in the same owner: verified
intervals may repeat, reduce, or restore one withheld round after contact relief.
It never creates calendar capacity or changes work/rest/effort together.

Exercise-bank prescriptions remain useful as the base dose, but scheduled-day
countdown and managed-contact rules are authoritative once a role has been placed
on the calendar. This module produces deterministic ``effective_prescription``
metadata so Stage 2 never has to reconcile conflicting base exercise text and
role-level caps.

Design contract (kept deliberately narrow):

* Countdown shaping still comes from ``apply_late_camp_role_morph``. This module
  never re-derives a countdown band.
* A role marked ``pre_hard_contact_managed_stress`` receives the existing
  D-17..D-14 strength-retention ceiling after placement: 3 x 3 max, RPE 6-7,
  loaded lifting allowed. A stricter countdown cap always wins.
* It is non-destructive: the exercise-bank prescription is preserved as
  ``base_prescription`` while the scheduled-day/context result is stored as
  ``effective_prescription`` and marked as the authoritative render dose.
* Exercise class matters. Anchor (primary loaded) work keeps the most meaningful
  loading the envelope allows; secondary loaded work loses more volume; support /
  trunk / prehab work loses sets but is never forced into low-rep strength reps;
  jumps / throws / neural power work keeps its own neural-quality reps. Loaded
  power/contrast work retains both its loaded-strength and ballistic semantics.
* Pre-hard-contact sessions keep one loaded anchor plus at most one genuinely
  low-cost power/support item; high impact, landing, eccentric or soreness-cost
  secondary work is excluded from the authoritative exercise allow-list.
* Athlete readiness / cut / injury state may only reduce the resolved dose
  further — never raise it above the scheduled-day/context ceiling.
"""

from __future__ import annotations

from .weight_cut import cut_restricts_training_capacity

import re
from typing import Any

from .calendar_context import role_d_day
from .late_camp_role_morph import (
    FULL_STRENGTH_ROLE_KEYS,
    STRENGTH_NEURAL_MORPH_MAX_D,
    late_fight_strength_dose_cap,
    loaded_work_permitted,
)
from .strength_session_quality import (
    ANCHOR_CAPABLE_CLASSES,
    SUPPORT_ONLY_CLASSES,
    classify_strength_item,
)


class MissingLateCampEffectiveStrengthAuthorityError(ValueError):
    """A loaded late-camp role reached the Stage 2 boundary without dose truth."""

    code = "missing_late_camp_effective_strength_authority"

    def __init__(self, details: dict[str, Any]):
        self.details = details
        fields = ", ".join(str(field) for field in details.get("missing_fields", []))
        super().__init__(
            f"{self.code}: role_key={details.get('role_key')!r}, "
            f"week_index={details.get('week_index')!r}, "
            f"session_index={details.get('session_index')!r}, "
            f"scheduled_weekday={details.get('scheduled_weekday')!r}, "
            f"original_countdown={details.get('original_countdown')!r}, "
            f"scheduled_countdown={details.get('scheduled_countdown')!r}, "
            f"resolved_d_day={details.get('resolved_d_day')!r}, "
            f"missing_fields=[{fields}]"
        )


# Movement-role names that mark low-load trunk / prehab / support work even when
# a quality class is unavailable. Mirrors the reservoir/support taxonomy.
_SUPPORT_MOVEMENT_ROLES = frozenset(
    {
        "anti_rotation",
        "trunk",
        "core",
        "mobility",
        "prehab",
        "rehab",
        "strength_support",
    }
)

_PRE_HARD_CONTACT_REASON = "pre_hard_contact_managed_stress"
_PRE_HARD_CONTACT_DOSE_REASON = "pre_hard_contact_strength_retention"
_VERIFIED_LOW_COST_LEVELS = frozenset(
    {"none", "low", "very low", "minimal", "negligible", "not applicable", "n/a"}
)
_PRE_HARD_COST_FIELDS = ("impact_cost", "landing_cost", "eccentric_cost", "soreness_risk")


# "2 x 20 m", "3 x 30s", "4 x 20 km" — the second number carries a distance or
# time unit, so it is NOT a rep count. A goal-preservation microdose is written
# this way ("2 x 20 m @ RPE 7"); reading its 20 metres as 20 reps let the
# countdown overlay rewrite the dose to "2 x 3" and silently drop the unit.
_NON_REP_SECOND_TERM = re.compile(
    r"\s*(?:m|km|s|sec|secs|second|seconds|min|mins|minute|minutes)\b", re.I
)


def _parse_sets_reps(prescription: str) -> tuple[int | None, int | None]:
    text = str(prescription or "")
    for match in re.finditer(r"\b(\d+)\s*[xX×]\s*(\d+)\b", text):
        if _NON_REP_SECOND_TERM.match(text[match.end():]):
            # Distance/time dose: leave it to the non-rep handling, which keeps
            # the unit and reduces the leading count instead.
            continue
        return int(match.group(1)), int(match.group(2))
    return None, None


def _rpe_ceiling(rpe_cap: Any) -> int | None:
    """Return the numeric high end of an RPE cap string like ``"6-7"`` -> 7."""
    values = [int(match) for match in re.findall(r"\d+", str(rpe_cap or ""))]
    return max(values) if values else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _slot_selected(slot: dict[str, Any]) -> dict[str, Any]:
    selected = slot.get("selected")
    return selected if isinstance(selected, dict) else {}


def _slot_quality_class(slot: dict[str, Any]) -> str:
    """Return the quality class the planner already stamped, or ``""``.

    Only explicit slot / selected metadata is used here — classification of a
    bare exercise is done in :func:`_role_kind` as a last resort so an explicit
    ``anchor_capable`` / ``support_only`` flag always wins over a default class.
    """
    selected = _slot_selected(slot)
    for source in (slot, selected):
        quality_class = str(source.get("quality_class") or "").strip()
        if quality_class:
            return quality_class
    return ""


def _has_explicit_strength_intensity(prescription: str) -> bool:
    """Return whether a power-labelled item explicitly carries loaded intensity."""
    text = str(prescription or "")
    return bool(
        re.search(r"\b\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?\s*%", text)
        or re.search(r"\bRPE\s*[:=]?\s*\d+(?:\.\d+)?", text, re.I)
    )


def _is_loaded_power_hybrid(slot: dict[str, Any], *, classified: dict[str, Any] | None = None) -> bool:
    """Identify a ballistic/contrast slot that also contains real loaded work.

    ``anchor_power`` is intentionally broad: it covers pure jumps/throws as well
    as contrast lifts such as ``Heavy RDL → Broad Jump``. The latter must not lose
    their loaded-strength semantics merely because power is the headline quality.
    We require both an explicit loaded structural signal and an explicit working
    intensity; pure neural/ballistic work therefore remains power-only.
    """
    selected = _slot_selected(slot)
    quality_class = _slot_quality_class(slot)
    profile = classified
    if not quality_class and selected:
        profile = profile or classify_strength_item(selected)
        quality_class = str(profile.get("quality_class") or "")
    if quality_class != "anchor_power":
        return False

    base_categories = {
        str(value).strip()
        for value in [
            *(slot.get("base_categories") or []),
            *(selected.get("base_categories") or []),
        ]
        if str(value).strip()
    }
    if not base_categories and selected:
        profile = profile or classify_strength_item(selected)
        base_categories.update(
            str(value).strip()
            for value in profile.get("base_categories") or []
            if str(value).strip()
        )
    loaded_structure = bool(base_categories & {"lower_body_loaded", "upper_body_push_pull"})
    if not loaded_structure and profile:
        loaded_structure = bool(profile.get("loaded_pattern"))
    return loaded_structure and _has_explicit_strength_intensity(selected.get("prescription") or "")


def _role_kind(slot: dict[str, Any]) -> str:
    """Classify a strength slot's programming role.

    Returns one of ``anchor`` (primary loaded strength / max-force isometric),
    ``hybrid`` (loaded contrast/power work that carries both strength and power),
    ``power`` (pure jumps / throws / olympic / ballistic neural work), ``support``
    (trunk / anti-rotation / prehab / accessory) or ``secondary`` (a loaded lift
    that is not the session's primary anchor). Explicit structural flags win; a
    bare exercise is classified only when it carries no such signal.
    """
    selected = _slot_selected(slot)
    quality_class = _slot_quality_class(slot)
    anchor_flag = bool(slot.get("anchor_capable") or selected.get("anchor_capable"))
    support_flag = bool(slot.get("support_only") or selected.get("support_only"))
    movement = str(slot.get("role") or selected.get("role") or "").strip().lower()

    if quality_class == "anchor_power":
        return "hybrid" if _is_loaded_power_hybrid(slot) else "power"
    if support_flag or quality_class in SUPPORT_ONLY_CLASSES or movement in _SUPPORT_MOVEMENT_ROLES:
        return "support"
    if anchor_flag or quality_class in ANCHOR_CAPABLE_CLASSES:
        return "anchor"

    # No explicit structural signal at all: fall back to classifying the exercise.
    if not quality_class and not anchor_flag and not support_flag and selected:
        classified = classify_strength_item(selected)
        classified_class = str(classified.get("quality_class") or "")
        if classified_class == "anchor_power":
            return "hybrid" if _is_loaded_power_hybrid(slot, classified=classified) else "power"
        if classified.get("support_only") and movement not in {"press", "hinge", "squat", "pull", "row"}:
            return "support"
        if classified.get("anchor_capable"):
            return "anchor"
    return "secondary"


def _slot_quality_class_effective(slot: dict[str, Any]) -> str:
    """Quality class including the classify fallback (for anchor-loaded demotion)."""
    quality_class = _slot_quality_class(slot)
    if quality_class:
        return quality_class
    selected = _slot_selected(slot)
    if selected:
        return str(classify_strength_item(selected).get("quality_class") or "")
    return ""


# Dosing units that are not reps: distance, seconds, a per-side complete cycle,
# or a contrast pair's two components. Only consulted once the "N x M" parse has
# already failed, so an ordinary rep dose is never matched.
_NON_REP_DOSE_PATTERN = re.compile(r"\bper side\b|\d\s*m\b|\d\s*s\b|\u2192", re.IGNORECASE)


def _is_non_rep_dose(prescription: str) -> bool:
    return bool(_NON_REP_DOSE_PATTERN.search(str(prescription or "")))


# A non-rep dose still opens with its own volume count -- "3-5 holds x 10-20s",
# "3-5 carries over 20-40 m", "3-4 rounds: ...", "1-2 sets of 1-2 reps per
# side". That leading count is the countdown band's reduction lever: capping it
# lowers total volume while every unit downstream of it is left untouched.
_LEADING_COUNT_PATTERN = re.compile(r"^(\s*)(\d+)(?:\s*[\u2013-]\s*(\d+))?(?=\s*\D)")

# Only needed so a count reduced to exactly one does not read "1 sets".
_SINGULAR_COUNT_NOUNS = {
    "sets": "set",
    "carries": "carry",
    "rounds": "round",
    "holds": "hold",
    "efforts": "effort",
    "pushes": "push",
}


def _reduce_leading_count(prescription: str, max_count: int) -> str:
    """Cap a non-rep dose's leading volume count. Reduce-only; unit preserved.

    Returns the prescription unchanged when it does not open with a count, or
    when the count is already at or below ``max_count`` -- the countdown band
    may never raise a dose.
    """
    match = _LEADING_COUNT_PATTERN.match(prescription)
    if not match or max_count < 1:
        return prescription
    low = int(match.group(2))
    high = int(match.group(3)) if match.group(3) else low
    new_high = min(high, max_count)
    new_low = min(low, new_high)
    if (new_low, new_high) == (low, high):
        return prescription
    count = f"{new_low}" if new_low == new_high else f"{new_low}\u2013{new_high}"
    tail = prescription[match.end():]
    if new_low == new_high == 1:
        # The noun may carry trailing punctuation ("3-4 rounds: ..."), so match
        # the word itself rather than splitting on whitespace.
        noun_match = re.match(r"(\s*)([A-Za-z]+)", tail)
        if noun_match:
            singular = _SINGULAR_COUNT_NOUNS.get(noun_match.group(2).lower())
            if singular:
                tail = f"{noun_match.group(1)}{singular}{tail[noun_match.end():]}"
    return f"{match.group(1)}{count}{tail}"


def _effective_counts(
    *,
    base_sets: int | None,
    base_reps: int | None,
    role_kind: str,
    strength_cap: dict[str, Any],
    base_prescription: str = "",
) -> tuple[int | None, int | None, bool]:
    """Return ``(effective_sets, effective_reps, loaded)`` for one slot.

    ``loaded`` is False when the resolved prescription carries no loaded strength
    stimulus (either the band forbids loaded lifting, or the exercise is neural /
    support work that is never treated as loaded strength).
    """
    max_sets = _int_or_none(strength_cap.get("max_sets"))
    max_reps = _int_or_none(strength_cap.get("max_reps"))
    # Absent flag (hand-built caps in focused unit tests) means "loaded still
    # allowed" so the anchor/secondary/hybrid maths run as before.
    loaded_allowed = strength_cap.get("loaded_allowed") is not False

    if role_kind in {"anchor", "secondary", "hybrid"}:
        if not loaded_allowed:
            # Band no longer permits loaded strength work: no loaded lift renders.
            return None, None, False

    if role_kind in {"anchor", "hybrid"}:
        # A dose counted in something other than reps -- a carry in metres, a
        # per-side quality cycle, a timed hold, a contrast pair's two components
        # -- keeps its own unit: substituting the cap's reps would replace the
        # whole prescription and drop the pair's explosive half or the get-up's
        # "per side". It is still reduced, through its leading set/round count
        # (see ``_format_effective_prescription``), so the countdown band keeps
        # a real volume lever and is not reduced to allow/forbid. A rep dose the
        # NxM regex merely could not read ("5 sets x 8 reps") is NOT covered
        # here and still takes the numeric cap below.
        non_rep = (
            base_sets is None
            and base_reps is None
            and _is_non_rep_dose(base_prescription)
        )
        sets = (
            min(base_sets, max_sets)
            if base_sets is not None and max_sets is not None
            else (base_sets if base_sets is not None else max_sets)
        )
        if non_rep:
            return sets, None, True
        reps = (
            min(base_reps, max_reps)
            if base_reps is not None and max_reps is not None
            else (base_reps if base_reps is not None else max_reps)
        )
        return sets, reps, True

    if role_kind == "secondary":
        # Secondary loaded work loses a set earlier than the anchor.
        secondary_set_cap = max(
            1,
            (max_sets - 1)
            if isinstance(max_sets, int) and max_sets > 1
            else (max_sets or 1),
        )
        sets = min(base_sets, secondary_set_cap) if base_sets is not None else secondary_set_cap
        if base_reps is None:
            reps = None
        elif isinstance(max_reps, int) and max_reps <= 2:
            reps = min(base_reps, max_reps)
        else:
            # Keep a strength-meaningful rep count rather than collapsing to the
            # anchor's low-rep cap.
            reps = min(base_reps, 5)
        return sets, reps, True

    if role_kind == "power":
        # Jumps / throws / neural power keep their own neural-quality reps; only
        # total volume (sets) tracks the countdown ceiling. Never a loaded lift.
        if isinstance(max_sets, int):
            sets = min(base_sets, max_sets) if base_sets is not None else max_sets
        else:
            sets = base_sets
        return sets, base_reps, False

    # Support / trunk / anti-rotation / prehab: reduce sets, keep reps. Never
    # forced into 2-3 rep strength work, never treated as loaded strength.
    support_set_cap = 2 if not isinstance(max_sets, int) else min(2, max_sets)
    sets = min(base_sets, support_set_cap) if base_sets is not None else support_set_cap
    return sets, base_reps, False




# These match concrete recovery durations, not work intervals or footwork cues.
# Both "1min rest" and "rest 60 sec" occur in the conditioning bank.
_PRIMER_REST_AFTER = re.compile(
    r"(?P<low>\d+(?:\.\d+)?)(?:\s*[-–]\s*(?P<high>\d+(?:\.\d+)?))?\s*"
    r"(?P<unit>seconds?|secs?|s|minutes?|mins?|m)\s+"
    r"(?P<label>rest|recovery|off)\b", re.I,
)
_PRIMER_REST_BEFORE = re.compile(
    r"\b(?P<label>rest|recovery|off)\s*[:=]?\s*"
    r"(?P<low>\d+(?:\.\d+)?)(?:\s*[-–]\s*(?P<high>\d+(?:\.\d+)?))?\s*"
    r"(?P<unit>seconds?|secs?|s|minutes?|mins?|m)\b", re.I,
)
def _primer_rest_with_floor(prescription: str, minimum_sec: float) -> str:
    """Increase short rest; preserve longer bank recovery without changing work."""
    found = False

    def rest_after(match: re.Match[str]) -> str:
        nonlocal found
        found = True
        unit = match.group("unit").lower()
        low = float(match.group("low")) * (60 if unit.startswith("m") else 1)
        if low >= minimum_sec:
            return match.group(0)
        return f"{minimum_sec:g} sec {match.group('label')}"

    def rest_before(match: re.Match[str]) -> str:
        nonlocal found
        found = True
        unit = match.group("unit").lower()
        low = float(match.group("low")) * (60 if unit.startswith("m") else 1)
        if low >= minimum_sec:
            return match.group(0)
        return f"{match.group('label')} {minimum_sec:g} sec"

    result = _PRIMER_REST_AFTER.sub(rest_after, prescription)
    result = _PRIMER_REST_BEFORE.sub(rest_before, result)
    if not found:
        result = f"{result.rstrip(' ;,')}; rest {minimum_sec:g} sec"
    return result


def resolve_late_primer_conditioning_dose(
    prescription: str, *, d_day: int | None, alactic: bool,
    complement: bool = False,
) -> str:
    """Price alactic primer work against its actual scheduled countdown window.

    Effort count and recovery floor come from canonical Style Taper policy.
    Rep-based throws retain reps, not fight-round units. Existing RPE remains
    completely unchanged; technical and mobility work never inherit this floor.
    """
    dose = _reduce_leading_count(prescription, 1) if complement else prescription
    if not dose or not alactic:
        return dose
    from .style_taper_governance import (
        ALACTIC_MAX_ROUNDS_BY_WINDOW,
        ALACTIC_MIN_REST_SEC_BY_WINDOW,
        style_taper_window_for_days,
    )

    window = style_taper_window_for_days(d_day)
    if window is None:
        return dose
    dose = _reduce_leading_count(dose, ALACTIC_MAX_ROUNDS_BY_WINDOW[window])
    return _primer_rest_with_floor(dose, ALACTIC_MIN_REST_SEC_BY_WINDOW[window])



_NO_LOADED_LIFTING = "No loaded lifting: neural/primer, readiness or mobility only"


def _format_effective_prescription(
    *,
    base_prescription: str,
    sets: int | None,
    reps: int | None,
    rpe_cap: str | None,
    loaded: bool,
    suppressed_loaded_lift: bool = False,
    athlete_risk: bool = False,
    exercise_name: str = "",
) -> str:
    # Unit-preserving doses still honour the context effort ceiling. Previously
    # a support/carry dose kept RPE 6-8 while its resolved metadata claimed 7.
    if rpe_cap:
        effort = re.search(r"\bRPE\s*[:=]?\s*(\d+(?:\.\d+)?)(?:\s*[-–]\s*(\d+(?:\.\d+)?))?", base_prescription, re.I)
        ceiling = _rpe_ceiling(rpe_cap)
        if effort and ceiling is not None and float(effort[2] or effort[1]) > ceiling:
            base_prescription = base_prescription[:effort.start()] + f"RPE {rpe_cap} max" + base_prescription[effort.end():]
    if sets is None or reps is None:
        # Only a loaded lift forbidden by the countdown band is suppressed.
        if suppressed_loaded_lift:
            return _NO_LOADED_LIFTING
        # Timed isometrics, carries, per-side cycles and contrast pairs keep
        # their own unit rather than being rewritten as reps -- but the band's
        # set ceiling still applies, to their leading volume count. This is the
        # one reduction lever these doses have; without it they would be immune
        # to everything except an outright no-loaded-lifting band.
        if isinstance(sets, int) and _is_non_rep_dose(base_prescription):
            return _reduce_leading_count(base_prescription, sets)
        # Throws, primers and other support work with no recognised unit retain
        # their bank prescription verbatim.
        return base_prescription
    if sets == 0 or reps == 0:
        return _NO_LOADED_LIFTING
    dose = f"{sets} x {reps}"
    # The trap-bar bank prescribes %1RM. Its original 85-90% load exceeds the
    # late-camp ceiling, so keep that measurable method at a reduced target
    # instead of replacing the load entirely with an RPE-only instruction.
    percent = re.search(r"(\d+(?:\.\d+)?)\s*(?:[-\u2013]\s*\d+(?:\.\d+)?)?\s*%\s*1\s*RM", base_prescription, re.I)
    if loaded and exercise_name.casefold() == "trap bar deadlift" and percent and rpe_cap:
        target = min(float(percent.group(1)), 75 if athlete_risk or (_rpe_ceiling(rpe_cap) or 10) <= 6 else 80)
        target_text = f"{target:g}"
        return f"{dose} @ {target_text}% 1RM (established max only); stop if bar speed slows"
    if not loaded and re.search(r"\bat max speed\b", base_prescription, re.I):
        rest = re.search(r"full rest\s+\d+\s*[-\u2013]\s*(\d+)\s*s\b", base_prescription, re.I)
        return f"{dose} at max speed" + (f"; rest {rest.group(1)}s" if rest else "; full rest")
    if rpe_cap:
        dose += f" @ RPE {rpe_cap} max"
    return dose


def _athlete_dose_reduction(athlete_state: dict[str, Any] | None) -> int:
    """Return an additional set reduction (0 or 1) from athlete readiness state.

    Athlete state may only ever REDUCE the context-resolved dose, never raise it.
    A single bounded step keeps the reduction monotonic: any active risk signal
    reduces by one set; a higher-risk profile is therefore always the same or
    lower than a lower-risk one under the same envelope, never higher.
    """
    if not isinstance(athlete_state, dict):
        return 0
    if any(
        bool(athlete_state.get(flag))
        for flag in ("high_fatigue", "aggressive_weight_cut", "recent_contact_load", "injury_restricted")
    ):
        return 1
    return 0


def athlete_dose_state(athlete_model: dict[str, Any] | None) -> dict[str, bool]:
    """Derive the reduce-only athlete readiness signals the resolver consumes."""
    if not isinstance(athlete_model, dict):
        return {}
    readiness_flags = {
        str(flag).strip().lower()
        for flag in (athlete_model.get("readiness_flags") or [])
        if str(flag).strip()
    }
    fatigue = str(athlete_model.get("fatigue") or "").strip().lower()
    cut_bucket = str(athlete_model.get("cut_severity_bucket") or "").strip().lower()
    injuries = athlete_model.get("injuries") or athlete_model.get("parsed_injuries") or []
    # ``surface_injury_only`` is the athlete model's canonical verdict that the
    # whole injury picture is stable train-through skin (a blister or graze).
    # That verdict already withholds ``injury_management``; reading the raw list
    # here would turn a covered blister back into global strength pressure.
    # Local wound precautions stay with the exercise-level injury filters.
    if athlete_model.get("surface_injury_only") is True:
        injuries = []
    return {
        "high_fatigue": fatigue == "high" or "high_fatigue" in readiness_flags,
        # Consume the resolved cut state; never re-derive severity from a raw
        # percentage here. The ``>= 5.0`` clause was an independent severity
        # system that overrode the canonical bucket for any 5%+ cut regardless
        # of how far out the fight was.
        "aggressive_weight_cut": (
            "aggressive_weight_cut" in readiness_flags
            or cut_restricts_training_capacity(cut_bucket)
        ),
        "injury_restricted": bool(injuries) or "injury_management" in readiness_flags,
        # Reserved for genuine athlete-level recent-contact state. Scheduled
        # next-day hard contact is a separate planner/context envelope and must
        # not masquerade as readiness state.
        "recent_contact_load": False,
    }


def apply_effective_conditioning_prescriptions(
    *, weekly_role_map: dict, athlete_model: dict,
) -> dict:
    """Resolve repeat/reduce/rebuild decisions inside closed membership.

    The composition dose is the ceiling, never a starting point for an
    unbounded weekly ramp. Only known timed intervals can change, only rounds
    change, and a rebuild requires an observed reduction in resolved contact
    load plus explicitly low fatigue. No completion/adaptation data is assumed.
    Fixed coach sessions, countdown membership and rest/effort stay with their
    existing owners. Repeated calls resolve from the same ceiling (idempotent).
    """
    from .conditioning_boxing import conditioning_load_context
    from .config import conditioning_work_is_timed
    from .exercise_progression import measured_dose

    state = athlete_dose_state(athlete_model)
    fatigue = str(athlete_model.get("fatigue") or "").lower()
    restricted = any(state.values()) or bool(athlete_model.get("injury_restrictions"))
    cut = str(athlete_model.get("cut_severity_bucket") or "none").lower()
    history: dict[tuple, dict] = {}
    exposures = []
    for week in weekly_role_map.get("weeks") or []:
        load = conditioning_load_context(athlete_model, week)
        week["conditioning_load_context"] = load
        for role in week.get("session_roles") or []:
            if (role.get("category") != "conditioning" or role.get("coach_owned")
                    or role.get("late_fight_tail_owned")):
                continue
            day = role_d_day(week, role)
            if day is not None:
                exposures.append((day, week, role, load))
    for day, week, role, load in sorted(exposures, key=lambda e: -e[0]):
        policy = role.get("conditioning_composition_policy") or {}
        total_work = 0.0
        changed = False
        for member in role.get("selected_exercise_assignments") or []:
            if member.get("slot_group") != "conditioning_slots":
                continue
            # A calendar week may straddle a phase boundary. Composition has
            # already resolved this member's scheduled-day phase.
            phase = str(member.get("source_phase") or role.get("phase") or week.get("phase") or "").upper()
            ceiling = member.setdefault("conditioning_dose_ceiling", {
                key: member.get(key) for key in
                ("effective_prescription", "effective_rounds", "rounds", "work_sec", "rest_sec")
            })
            text = str(ceiling.get("effective_prescription") or "")
            rounds = _int_or_none(ceiling.get("effective_rounds") or ceiling.get("rounds"))
            work = ceiling.get("work_sec")
            rest = ceiling.get("rest_sec")
            key = (phase, role.get("role_key"), member.get("exercise_key") or member.get("name"))
            previous = history.get(key)
            if previous and previous['day'] <= day:
                previous = None
            decision, reason = "maintain", "repeat_authorised_dose"
            selected_rounds = rounds
            # Match only the round count, leaving every other character of the
            # authorised prescription (pace, rest, effort, technique) intact.
            leading = re.match(r"^(\d+)\s*x\s*\d+(?:\.\d+)?\s*(?:s|sec|seconds|min|minutes)\b", text, re.I)
            trailing = re.search(r"\bx\s*(\d+)\s+rounds?\b", text, re.I)
            count = leading or trailing
            measured = measured_dose(text, member)
            timed = (count and rounds and int(count[1]) == rounds
                     and isinstance(work, (int, float)) and work > 0
                     and isinstance(rest, (int, float)) and rest >= 0
                     and measured.get('work', measured.get('duration')) == work
                     and measured.get('rest') == rest
                     and conditioning_work_is_timed({"duration": text})
                     and not re.search(r"\bor\b|\+|\(|→", text))
            locked = (phase == "TAPER" or day <= 14 or role.get("late_camp_role_morph")
                      or member.get("support_only") or role.get("allowed_on_recovery_day"))
            hard = (role.get("preferred_system") == "glycolytic"
                    or (member.get("rpe") or 0) >= 8)
            pressure = restricted or fatigue == "moderate" or cut not in {"none", "low"}
            contact_pressure = hard and (load["hard_exposures"] >= 2 or policy.get("hard_sparring_adjacent"))
            if timed and not locked:
                if pressure or contact_pressure:
                    selected_rounds = max(1, rounds - 1)
                    decision, reason = "reduce", "recovery_pressure" if pressure else "sparring_load"
                    if selected_rounds == rounds:
                        decision, reason = "maintain", "minimum_authorised_round_count"
                elif previous and previous["rounds"] < rounds:
                    selected_rounds = previous["rounds"]
                    # A planned load reduction is evidence for restoring ONE
                    # previously withheld round, not evidence of adaptation.
                    if (fatigue == "low" and load["hard_exposures"] < previous["hard_exposures"]
                            and load["coach_conditioning_intensity"] != "unknown"
                            and previous["work"] == work and previous["rest"] == rest
                            and previous["rpe"] == member.get("rpe")):
                        selected_rounds += 1
                        decision, reason = "increase", "restore_one_round_after_contact_relief"
                    else:
                        reason = "hold_until_recovery_and_load_justify_change"
                if (previous and decision == "reduce" and selected_rounds == previous["rounds"]
                        and previous["work"] == work and previous["rest"] == rest
                        and previous["rpe"] == member.get("rpe")):
                    decision, reason = "maintain", "repeat_reduced_dose_for_recovery"
                if selected_rounds != rounds:
                    start, end = count.span(1)
                    member["effective_prescription"] = text[:start] + str(selected_rounds) + text[end:]
                    member["effective_rounds"] = selected_rounds
                    member["rounds"] = selected_rounds
                    changed = True
                else:
                    member.update(ceiling)
                history[key] = {"rounds": selected_rounds, "work": work, "rest": rest,
                                "rpe": member.get("rpe"), "hard_exposures": load["hard_exposures"], 'day': day}
                total_work += work * selected_rounds
            else:
                reason = "countdown_or_support_dose" if locked else "unverified_interval_units_or_structure"
                if timed:
                    total_work += work * rounds
            member["conditioning_decision"] = {"action": decision, "reason": reason, "variable": "rounds" if decision in {"increase", "reduce"} else None}
        # A safe reduction deliberately lowers the composition obligation.
        # Never conceal a pre-existing underfill or credit unknown work.
        if changed and policy.get("conditioning_workload_met") is True:
            base_target = policy.setdefault("unadjusted_target_active_work_seconds", policy.get("conditioning_target_active_work_seconds"))
            policy["conditioning_target_active_work_seconds"] = min(base_target, total_work)
            policy["conditioning_active_work_seconds"] = total_work
            policy["dose_adjustment_reason"] = "authorised_conditioning_reduction"
    return weekly_role_map


def _merge_numeric_cap(existing: Any, incoming: Any) -> int | None:
    left = _int_or_none(existing)
    right = _int_or_none(incoming)
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def _merge_rpe_cap(existing: Any, incoming: Any) -> str:
    existing_text = str(existing or "").strip()
    incoming_text = str(incoming or "").strip()
    if not existing_text:
        return incoming_text
    if not incoming_text:
        return existing_text
    existing_high = _rpe_ceiling(existing_text)
    incoming_high = _rpe_ceiling(incoming_text)
    if existing_high is None:
        return incoming_text
    if incoming_high is None:
        return existing_text
    return existing_text if existing_high <= incoming_high else incoming_text


def _apply_pre_hard_contact_cap(role: dict[str, Any]) -> None:
    """Merge the shared D17-D14 retention ceiling into an affected role."""
    if role.get("pre_hard_contact_managed_stress") is not True:
        return
    retention = late_fight_strength_dose_cap(17) or {}
    existing = role.get("strength_dose_cap")
    existing = dict(existing) if isinstance(existing, dict) else {}
    merged = {
        "max_sets": _merge_numeric_cap(existing.get("max_sets"), retention.get("max_sets")),
        "max_reps": _merge_numeric_cap(existing.get("max_reps"), retention.get("max_reps")),
        "loaded_allowed": (
            existing.get("loaded_allowed") is not False
            and retention.get("loaded_allowed") is not False
        ),
    }
    role["strength_dose_cap"] = {key: value for key, value in merged.items() if value is not None}
    role["rpe_cap"] = _merge_rpe_cap(role.get("rpe_cap"), retention.get("rpe_cap") or "6-7")
    if not str(role.get("dose_adjustment_reason") or "").strip():
        role["dose_adjustment_reason"] = _PRE_HARD_CONTACT_DOSE_REASON
    role["pre_hard_contact_dose_adjustment"] = True
    if role.get("pre_hard_contact_microdose_only") is True:
        role["strength_dose_cap"] = {
            "max_sets": _merge_numeric_cap(role["strength_dose_cap"].get("max_sets"), 2),
            "max_reps": _merge_numeric_cap(role["strength_dose_cap"].get("max_reps"), 6),
            "loaded_allowed": False,
        }
        role["rpe_cap"] = _merge_rpe_cap(role.get("rpe_cap"), "6")
        role["dose_adjustment_reason"] = "pre_hard_contact_redistributed_low_cost_work"


def _selection_metadata(slot: dict[str, Any]) -> dict[str, Any]:
    selected = _slot_selected(slot)
    metadata = selected.get("selection_metadata")
    if isinstance(metadata, dict):
        return metadata
    metadata = slot.get("selection_metadata")
    return metadata if isinstance(metadata, dict) else {}


def _cost_value(slot: dict[str, Any], field: str) -> str:
    metadata = _selection_metadata(slot)
    selected = _slot_selected(slot)
    value = metadata.get(field)
    if value in (None, ""):
        value = selected.get(field)
    if value in (None, ""):
        value = slot.get(field)
    return str(value or "").strip().lower().replace("-", "_")


def has_verified_low_cost(
    slot: dict[str, Any], *, fields: tuple[str, ...]
) -> bool:
    """Require affirmative low/none values from the existing cost metadata."""
    values = [
        _cost_value(slot, field).replace("_", " ")
        for field in fields
    ]
    return all(value and value in _VERIFIED_LOW_COST_LEVELS for value in values)


def _pre_hard_verified_low_cost(slot: dict[str, Any]) -> bool:
    """Require affirmative low/none cost metadata for the optional second item.

    Low impact and little eccentric loading do not make a heavy implement low
    cost. A support-classified item whose authored governance states meaningful
    stress (a heavy sled push, yoke walk or loaded carry) is refused here.
    """
    selected = _slot_selected(slot)
    if _role_kind(slot) == "support" and (
        selected.get("meaningful_stress") is True or selected.get("support_only") is False
    ):
        return False
    return has_verified_low_cost(slot, fields=_PRE_HARD_COST_FIELDS)


def _pre_hard_transfer_eligible(slot: dict[str, Any]) -> bool:
    """Allow light, controllable exercises before sparring; refuse high cost.

    Unlike the independent meaningful-strength gate this evaluates only an
    already selected EXERCISE to be moved, not a newly added exposure. An
    anti-rotation anchor can move as capped light technique (never heavy force
    work); a meaningful power exercise is not rejected solely for its label.
    """
    selected = _slot_selected(slot)
    kind = _role_kind(slot)
    metadata = _selection_metadata(slot)
    # Stage 1 may stamp risk tags in the selected option, its provenance
    # metadata, or the slot. Reading only one location misses some source
    # flags. Semantic tags like anti_rotation are relevant for a controlled
    # trunk exception, but cannot override any high-load veto.
    tags = {
        str(tag).strip().lower()
        for tag in (
            *(slot.get("mechanical_risk_tags") or []),
            *(slot.get("tags") or []),
            *(selected.get("mechanical_risk_tags") or []),
            *(selected.get("tags") or []),
            *(metadata.get("mechanical_risk_tags") or []),
        )
        if str(tag).strip()
    }
    controlled_trunk = (
        kind == "anchor"
        and "anti_rotation" in tags
        and "mech_trunk_stability" in tags
    )
    if kind not in {"power", "support"} and not controlled_trunk:
        return False
    if tags & {"mech_cns_high", "mech_systemic_fatigue", "mech_axial_heavy"}:
        return False
    # Mechanical tags alone are not an exhaustive load classification.
    # The bank's explicit CNS, systemic, contact and movement-cost fields are
    # authoritative even if a corresponding mech_* tag is absent.
    if any(_cost_value(slot, field) in {"high", "very_high", "very high", "extreme"}
           for field in ("cns_load", "movement_cost", "contact_cost", "lactate_load")):
        return False
    # Landing or impact must be low/none; moderate soreness/eccentric cost can
    # survive a SMALL, dose-limited transfer. High or unverified costs cannot.
    costs = {
        field: _cost_value(slot, field).replace(" ", "_")
        for field in _PRE_HARD_COST_FIELDS
    }
    verified_low = {
        level.replace(" ", "_") for level in _VERIFIED_LOW_COST_LEVELS
    }
    if any(not value for value in costs.values()):
        return False
    if costs["impact_cost"] not in verified_low:
        return False
    if costs["landing_cost"] not in verified_low:
        return False
    if any(value not in verified_low | {"moderate"} for value in costs.values()):
        return False
    # Ensure a loaded trunk exercise has a parseable, non-maximal dose and can
    # genuinely be downscaled to RPE 6 technique before hard contact.
    if controlled_trunk:
        dose = str(selected.get("prescription") or "")
        if "%" in dose or not re.search(r"\bRPE\b", dose, re.I):
            return False
    return True


def _pre_hard_allowed_slots(
    owned_slots: list[dict[str, Any]], *, microdose_only: bool = False
) -> list[dict[str, Any]]:
    """One loaded anchor and low-cost support, or 2-3 transferred items."""
    if microdose_only:
        # Composition has already chosen 2-3 existing exercise identities.
        # Re-check the same eligibility before the dose resolver may publish.
        return [
            slot for slot in sorted(owned_slots, key=_slot_priority)
            if _pre_hard_transfer_eligible(slot)
        ][:3]
    allowed: list[dict[str, Any]] = []
    loaded_anchor_used = False
    additional_used = False
    for slot in sorted(owned_slots, key=_slot_priority):
        kind = _role_kind(slot)
        if kind in {"anchor", "hybrid", "secondary"}:
            if loaded_anchor_used:
                continue
            # A true secondary slot cannot become the one anchor when a higher
            # priority anchor was absent from the candidate group.
            if kind == "secondary":
                continue
            allowed.append(slot)
            loaded_anchor_used = True
            continue
        if kind in {"power", "support"}:
            if additional_used or not _pre_hard_verified_low_cost(slot):
                continue
            allowed.append(slot)
            additional_used = True
    return allowed


def resolve_strength_slot_prescription(
    *,
    role: dict[str, Any],
    slot: dict[str, Any],
    athlete_state: dict[str, Any] | None = None,
    force_kind: str | None = None,
) -> dict[str, Any]:
    """Return deterministic effective-prescription metadata for one strength slot.

    ``force_kind`` lets the caller override the per-slot dose treatment (used to
    demote a later loaded lift in a multi-lift session to ``secondary``) without
    erasing the slot's original semantic class.
    """
    selected = _slot_selected(slot)
    from .working_dose import working_dose
    base_prescription = str(selected.get("prescription") or "").strip()
    cap = role.get("strength_dose_cap") if isinstance(role.get("strength_dose_cap"), dict) else None
    if not cap or not base_prescription:
        return {
            "base_prescription": base_prescription,
            "effective_prescription": working_dose(base_prescription),
            "dose_authority": "exercise_bank",
        }

    semantic_kind = _role_kind(slot)
    kind = force_kind or semantic_kind
    microdose_only = role.get("pre_hard_contact_microdose_only") is True
    priced_base = (
        working_dose(str(slot.get("redistributed_dose_ceiling") or base_prescription))
        if microdose_only else base_prescription
    )
    base_sets, base_reps = _parse_sets_reps(priced_base)
    sets, reps, loaded = _effective_counts(
        base_sets=base_sets,
        base_reps=base_reps,
        role_kind=kind,
        strength_cap=cap,
        base_prescription=priced_base,
    )
    if microdose_only:
        # An unpriceable or loaded member cannot fall back to its
        # unrestricted bank prescription before protected hard sparring.
        if kind not in {"power", "support"} or not isinstance(sets, int) or not isinstance(reps, int):
            return {
                "base_prescription": base_prescription,
                "effective_prescription": "",
                "dose_authority": "scheduled_countdown_overlay",
                "dose_adjustment_reason": "pre_hard_contact_unpriceable_microdose",
                "effective_loaded": False,
                "strength_dose_cap": dict(cap),
            }
        sets = min(sets, 2)
        reps = min(reps, 2 if kind == "power" else 6)
        loaded = False

    # Readiness / cut / injury state can only pull the dose down further.
    reduction = _athlete_dose_reduction(athlete_state)
    if reduction and loaded and isinstance(sets, int) and sets > 1:
        sets = max(1, sets - reduction)
    # A complementary member of a countdown primer session adds a different
    # stimulus, not more volume: it is held to a single set so a three-exercise
    # primer never multiplies the session's work.
    if slot.get("primer_complement") and isinstance(sets, int) and sets > 1:
        sets = 1

    rpe_cap = str(role.get("rpe_cap") or "").strip() or None
    effective = _format_effective_prescription(
        base_prescription=priced_base,
        sets=sets,
        reps=reps,
        rpe_cap=rpe_cap,
        loaded=loaded,
        suppressed_loaded_lift=kind in {"anchor", "secondary", "hybrid"} and not loaded,
        athlete_risk=bool(reduction),
        exercise_name=str(selected.get("name") or ""),
    )
    # ``dose_role_kind`` is the existing persisted validator-facing vocabulary.
    # Loaded-power hybrid is an internal semantic used to resolve the right dose;
    # downstream truth stays expressed through the canonical slot plus the
    # existing anchor/secondary/power/support kind and effective-loaded fields.
    persisted_kind = "anchor" if semantic_kind == "hybrid" and kind == "hybrid" else kind
    result = {
        "base_prescription": base_prescription,
        "effective_prescription": working_dose(effective),
        "dose_authority": "scheduled_countdown_overlay",
        "dose_role_kind": persisted_kind,
        "dose_adjustment_reason": role.get("dose_adjustment_reason"),
        "effective_loaded": bool(loaded),
        "strength_dose_cap": dict(cap),
    }
    if isinstance(sets, int):
        result["effective_max_sets"] = sets
    if isinstance(reps, int):
        result["effective_max_reps"] = reps
    rpe_high = _rpe_ceiling(role.get("rpe_cap"))
    if rpe_high is not None:
        result["effective_rpe_cap"] = rpe_high
    return result


def _strength_slots_for_phase(candidate_pools: dict[str, Any], phase: str) -> list[dict[str, Any]]:
    phase_pool = candidate_pools.get(phase)
    if not isinstance(phase_pool, dict):
        return []
    slots = phase_pool.get("strength_slots")
    if not isinstance(slots, list):
        return []
    return [slot for slot in slots if isinstance(slot, dict)]


def _assignment_strength_slot(
    candidate_pools: dict[str, Any], source_phase: str, assignment: dict[str, Any]
) -> dict[str, Any] | None:
    """The exact Stage 1 slot (or alternate) a strength assignment came from.

    The late-fight selector may assign a slot's alternate under the identity
    ``<slot_id>:alternate:<n>``; that alternate is dosed as its own slot rather
    than left without an effective prescription.
    """
    slot_id = str(assignment.get("slot_id") or "")
    name = str(assignment.get("name") or "")
    base_id, _, alternate = slot_id.partition(":alternate:")
    for slot in _strength_slots_for_phase(candidate_pools, source_phase):
        if str(slot.get("slot_id") or "") != base_id:
            continue
        if not alternate:
            return slot if str(_slot_selected(slot).get("name") or "") == name else None
        alternates = slot.get("alternates") or []
        option = alternates[int(alternate)] if alternate.isdigit() and int(alternate) < len(alternates) else None
        if isinstance(option, dict) and str(option.get("name") or "") == name:
            return {"slot_id": slot_id, "selected": option, "alternates": []}
        return None
    return None


def _is_strength_role(role: dict[str, Any]) -> bool:
    role_key = str(role.get("role_key") or "").strip().lower()
    return (
        str(role.get("category") or "").strip().lower() == "strength"
        or role_key in FULL_STRENGTH_ROLE_KEYS
        or str(role.get("preferred_pool") or "").strip().lower() == "strength_slots"
    )


def _strength_role_slot_groups(
    *,
    weekly_role_map: dict[str, Any],
    candidate_pools: dict[str, Any],
):
    """Yield each scheduled strength role with the candidate slots it owns."""
    for week in weekly_role_map.get("weeks", []) or []:
        if not isinstance(week, dict):
            continue
        for role in week.get("session_roles") or []:
            if not isinstance(role, dict) or not _is_strength_role(role):
                continue
            assignments = role.get("selected_exercise_assignments")
            if isinstance(assignments, list):
                owned_slots = []
                for assignment in assignments:
                    if not isinstance(assignment, dict) or assignment.get("slot_group") != "strength_slots":
                        continue
                    # The selector owns the source phase.  A spliced D-13 role
                    # may sit in an SPP calendar week while its exact selected
                    # slot came from another phase; never rediscover it from the
                    # containing week's phase.
                    source_phase = str(assignment.get("source_phase") or "").strip().upper()
                    if not source_phase:
                        continue
                    match = _assignment_strength_slot(candidate_pools, source_phase, assignment)
                    if match is not None:
                        if assignment.get("primer_complement"):
                            match = {**match, "primer_complement": True}
                        # Composition approved a reduce-only rep variant for
                        # this exact transferred exercise. Carry the bounded
                        # selection through the existing dose owner rather
                        # than reparsing an ambiguous bank text (e.g.
                        # "2-4 sets x 6-10 reps or 20-40s tempo").
                        if role.get("pre_hard_contact_microdose_only") is True and assignment.get("redistributed_dose_ceiling"):
                            match = {**match, "redistributed_dose_ceiling": assignment["redistributed_dose_ceiling"]}
                        owned_slots.append(match)
            else:
                # Candidate grouping is retained for diagnostics only.  It must
                # never become athlete-facing prescription authority.
                owned_slots = []
            yield week, role, owned_slots


def _loaded_candidate_names(owned_slots: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for slot in owned_slots:
        selected = _slot_selected(slot)
        if (
            _role_kind(slot) not in {"anchor", "secondary", "hybrid"}
            or not str(selected.get("prescription") or "").strip()
        ):
            continue
        name = str(selected.get("name") or slot.get("slot_id") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def assert_late_camp_effective_strength_authority(
    *,
    weekly_role_map: dict[str, Any],
    candidate_pools: dict[str, Any],
) -> None:
    """Block Stage 2 when a loaded countdown role lacks scheduled-day authority."""
    if not isinstance(weekly_role_map, dict) or not isinstance(candidate_pools, dict):
        return

    for week, role, owned_slots in _strength_role_slot_groups(
        weekly_role_map=weekly_role_map,
        candidate_pools=candidate_pools,
    ):
        d_day = role_d_day(week, role)
        loaded_names = _loaded_candidate_names(owned_slots)
        if (
            d_day is None
            or not 0 <= d_day <= STRENGTH_NEURAL_MORPH_MAX_D
            or not loaded_names
        ):
            continue

        missing: list[str] = []
        cap = role.get("strength_dose_cap")
        if not isinstance(cap, dict):
            missing.append("strength_dose_cap")
        elif not isinstance(cap.get("loaded_allowed"), bool):
            missing.append("strength_dose_cap.loaded_allowed")

        if _int_or_none(role.get("scheduled_d_day")) != d_day:
            missing.append("scheduled_d_day")
        if not str(role.get("dose_adjustment_reason") or "").strip():
            missing.append("dose_adjustment_reason")
        if _rpe_ceiling(role.get("rpe_cap")) is None:
            missing.append("rpe_cap")

        prescriptions = role.get("effective_strength_prescriptions")
        if not isinstance(prescriptions, list) or not prescriptions:
            missing.append("effective_strength_prescriptions")
            prescriptions = []
        by_name = {
            str(item.get("name") or "").strip(): item
            for item in prescriptions
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        }
        for name in loaded_names:
            item = by_name.get(name)
            if not item:
                missing.append(f"effective_strength_prescriptions[{name}]")
                continue
            if not str(item.get("effective_prescription") or "").strip():
                missing.append(f"effective_strength_prescriptions[{name}].effective_prescription")
            if item.get("dose_authority") != "scheduled_countdown_overlay":
                missing.append(f"effective_strength_prescriptions[{name}].dose_authority")
            if not isinstance(item.get("effective_loaded"), bool):
                missing.append(f"effective_strength_prescriptions[{name}].effective_loaded")

        envelope = role.get("effective_strength_envelope")
        if not isinstance(envelope, dict):
            missing.append("effective_strength_envelope")
        else:
            if _int_or_none(envelope.get("scheduled_d_day")) != d_day:
                missing.append("effective_strength_envelope.scheduled_d_day")
            if not isinstance(envelope.get("loaded_allowed"), bool):
                missing.append("effective_strength_envelope.loaded_allowed")
            if _int_or_none(envelope.get("rpe_cap_high")) is None:
                missing.append("effective_strength_envelope.rpe_cap_high")

        if missing:
            raise MissingLateCampEffectiveStrengthAuthorityError(
                {
                    "role_key": role.get("role_key"),
                    "week_index": week.get("week_index"),
                    "session_index": role.get("session_index"),
                    "scheduled_weekday": role.get("scheduled_day_hint") or role.get("real_weekday"),
                    "original_countdown": role.get("countdown_label"),
                    "scheduled_countdown": role.get("scheduled_countdown_label"),
                    "resolved_d_day": d_day,
                    "loaded_exercises": loaded_names,
                    "missing_fields": missing,
                }
            )


_PRIORITY_ORDER = {
    "critical": 0,
    "anchor": 0,
    "primary": 0,
    "high": 1,
    "secondary": 1,
    "medium": 2,
    "support": 2,
    "power": 2,
    "ballistic": 2,
    "low": 3,
}


def _slot_priority(slot: dict[str, Any]) -> tuple[int, int]:
    raw_priority = slot.get("priority")
    priority = _int_or_none(raw_priority)
    if priority is None:
        priority = _PRIORITY_ORDER.get(str(raw_priority or "").strip().lower())
    session_index = _int_or_none(slot.get("session_index")) or 1
    return (priority if priority is not None else 10_000, session_index)


def _build_role_envelope(
    role: dict[str, Any],
    resolved: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Summarise the loaded-strength ceiling for the finalizer/validator."""
    loaded_entries = [item for item in resolved if item.get("dose_role_kind") in {"anchor", "secondary"}]
    loaded_names = [item.get("name") for item in loaded_entries if item.get("name")]
    # Whether the DAY permits loaded work is the countdown cap's decision alone.
    # Which lifts in this session are loaded is a separate fact, carried per
    # exercise as ``effective_loaded`` and in ``loaded_exercise_names``. Folding
    # the second into this flag made a session of power/support work read as
    # "no loaded work allowed" on a day that allows it, which the release check
    # then held.
    loaded_allowed = loaded_work_permitted(role)

    max_sets = None
    max_reps = None
    for item in loaded_entries:
        if not item.get("effective_loaded"):
            continue
        item_sets = item.get("effective_max_sets")
        item_reps = item.get("effective_max_reps")
        if isinstance(item_sets, int):
            max_sets = item_sets if max_sets is None else max(max_sets, item_sets)
        if isinstance(item_reps, int):
            max_reps = item_reps if max_reps is None else max(max_reps, item_reps)

    selected_names = [
        str(assignment.get("name") or "").strip()
        for assignment in (role.get("selected_exercise_assignments") or [])
        if isinstance(assignment, dict) and str(assignment.get("name") or "").strip()
    ]
    envelope = {
        "scheduled_d_day": role.get("scheduled_d_day"),
        "dose_adjustment_reason": role.get("dose_adjustment_reason"),
        "loaded_allowed": bool(loaded_allowed),
        "rpe_cap_high": _rpe_ceiling(role.get("rpe_cap")),
        "loaded_exercise_names": loaded_names,
        # Composition and dose are separate authorities.  This list is derived
        # only from the deterministic selector, never from the candidate pool or
        # from whichever entries happened to resolve a dose successfully.
        "allowed_exercise_names": list(dict.fromkeys(selected_names)),
        "complete_exercise_allow_list": True,
    }
    if role.get("pre_hard_contact_managed_stress") is True:
        envelope.update(
            {
                "pre_hard_contact_managed_stress": True,
                "pre_hard_contact_reason_code": role.get("pre_hard_contact_reason_code") or _PRE_HARD_CONTACT_REASON,
                "max_meaningful_strength_exposures": 1,
                "pre_hard_contact_microdose_only": role.get("pre_hard_contact_microdose_only") is True,
                "max_loaded_anchors": 0 if role.get("pre_hard_contact_microdose_only") is True else 1,
                "max_additional_low_cost_items": 3 if role.get("pre_hard_contact_microdose_only") is True else 1,
                # The pre-contact policy may reduce dose/volume, but it must not
                # reopen or independently redefine selected membership.
                "allowed_exercise_names": list(dict.fromkeys(selected_names)),
                "complete_exercise_allow_list": True,
                "forbid_slow_eccentric_emphasis": True,
            }
        )
    if isinstance(max_sets, int):
        envelope["max_sets"] = max_sets
    if isinstance(max_reps, int):
        envelope["max_reps"] = max_reps
    return envelope


def apply_effective_strength_prescriptions(
    *,
    weekly_role_map: dict[str, Any],
    candidate_pools: dict[str, Any],
    athlete_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach resolved strength prescriptions to role metadata for Stage 2.

    Runs after ``apply_late_camp_role_morph``. Countdown roles therefore already
    carry their scheduled-day cap. Normal-camp roles marked by the canonical
    pre-hard-contact role-budget policy receive the same D17-D14 retention ceiling
    here, after the late-camp morph has finished, so a D-18+ cap is never cleared
    as stale countdown metadata.
    """
    if not isinstance(weekly_role_map, dict) or not isinstance(candidate_pools, dict):
        return weekly_role_map

    athlete_state = athlete_dose_state(athlete_model)

    for week, role, owned_slots in _strength_role_slot_groups(
        weekly_role_map=weekly_role_map,
        candidate_pools=candidate_pools,
    ):
        _apply_pre_hard_contact_cap(role)
        assignments = role.get("selected_exercise_assignments")
        slots_for_resolution = owned_slots
        if isinstance(assignments, list) and role.get("pre_hard_contact_managed_stress") is True:
            # #2435 is a deterministic composition decision, not merely a dose
            # filter. Once it removes a high-cost secondary/power item, that item
            # must also disappear from the authoritative selected membership so
            # PR2 cannot re-authorise it via the closed allow-list.
            slots_for_resolution = _pre_hard_allowed_slots(
                owned_slots,
                microdose_only=role.get("pre_hard_contact_microdose_only") is True,
            )
            surviving_strength_keys = {
                (str(slot.get("slot_id") or ""), str(_slot_selected(slot).get("name") or ""))
                for slot in slots_for_resolution
            }
            assignments = [
                assignment
                for assignment in assignments
                if isinstance(assignment, dict)
                and (
                    assignment.get("slot_group") != "strength_slots"
                    or (
                        str(assignment.get("slot_id") or ""),
                        str(assignment.get("name") or ""),
                    ) in surviving_strength_keys
                )
            ]
            role["selected_exercise_assignments"] = assignments

        if isinstance(assignments, list):
            # Even a normal-camp role with no countdown dose overlay has closed
            # composition once the selector has written this field.  Reuse the
            # existing complete allow-list envelope instead of introducing a
            # second validator contract.
            envelope = role.get("effective_strength_envelope")
            if not isinstance(envelope, dict):
                envelope = {}
            selected_names = [
                str(assignment.get("name") or "").strip()
                for assignment in assignments
                if isinstance(assignment, dict) and str(assignment.get("name") or "").strip()
            ]
            envelope.update(
                allowed_exercise_names=list(dict.fromkeys(selected_names)),
                complete_exercise_allow_list=True,
            )
            if isinstance(role.get("strength_dose_cap"), dict):
                # A capped day states its permission even when no strength lift
                # resolved a dose; a missing flag read as "allowed" on a no-load day.
                envelope["loaded_allowed"] = loaded_work_permitted(role)
            scheduled_d_day = role_d_day(week, role)
            if scheduled_d_day is not None:
                role["scheduled_d_day"] = scheduled_d_day
                envelope["scheduled_d_day"] = scheduled_d_day
            role["effective_strength_envelope"] = envelope
        # A closed-membership strength role always carries its selected bank
        # dose forward, whether or not a scheduled-day cap applies.  When a cap
        # exists ``resolve_strength_slot_prescription`` reshapes the dose to the
        # ceiling; with no cap it returns the exercise-bank dose unchanged.  The
        # scheduled-day metadata below stays cap-only so a normal-camp role is
        # never labelled as a countdown day.
        has_dose_cap = isinstance(role.get("strength_dose_cap"), dict)
        if has_dose_cap:
            scheduled_d_day = role_d_day(week, role)
            if scheduled_d_day is not None:
                role["scheduled_d_day"] = scheduled_d_day

        if not isinstance(assignments, list):
            continue

        # Demote every anchor-capable loaded lift after the highest-priority one
        # to ``secondary`` so later loaded work loses more volume. Loaded-power
        # hybrids compete for the same loaded-anchor budget; pure neural power
        # remains outside it. Ordered by planner slot priority.
        anchor_loaded_used = False
        resolved: list[dict[str, Any]] = []
        for slot in sorted(slots_for_resolution, key=_slot_priority):
            kind = _role_kind(slot)
            force_kind = None
            if (
                role.get("pre_hard_contact_microdose_only") is True
                and kind == "anchor"
                and _pre_hard_transfer_eligible(slot)
            ):
                # Capped trunk-control technique, not a second loaded anchor.
                force_kind = "support"
            if force_kind is None and kind in {"anchor", "hybrid"} and _slot_quality_class_effective(slot) != "anchor_force_isometric":
                if anchor_loaded_used:
                    force_kind = "secondary"
                else:
                    anchor_loaded_used = True
            item = resolve_strength_slot_prescription(
                role=role,
                slot=slot,
                athlete_state=athlete_state,
                force_kind=force_kind,
            )
            if not item.get("effective_prescription"):
                continue
            entry = {
                "slot_id": slot.get("slot_id"),
                "name": (_slot_selected(slot).get("name") if _slot_selected(slot) else None),
                **item,
            }
            resolved.append(entry)

        # A rejected effective dose is a rejected member, not permission to
        # leave an unpriced bank prescription in the closed session. In
        # particular an ambiguous pre-contact hold must never survive on its
        # unrestricted base dose. Empty sessions are removed by composition's
        # existing final suppression pass.
        priced_keys = {(p.get("slot_id"), p.get("name")) for p in resolved}
        role["selected_exercise_assignments"] = [a for a in assignments
            if isinstance(a, dict) and (a.get("slot_group") != "strength_slots"
                                       or (a.get("slot_id"), a.get("name")) in priced_keys)]
        role["effective_strength_prescriptions"] = resolved
        names = [a.get("name") for a in role["selected_exercise_assignments"] if a.get("name")]
        role["effective_strength_envelope"]["allowed_exercise_names"] = list(dict.fromkeys(names))
        policy = role.get("strength_composition_policy")
        if isinstance(policy, dict):
            policy.update(selected_count=len(names), selected_names=names)
        # The numeric loaded-strength ceiling envelope is a scheduled-day cap
        # artefact.  A normal-camp role has no cap to summarise and keeps the
        # complete-allow-list envelope already built above.
        if has_dose_cap:
            envelope = _build_role_envelope(role, resolved)
            if envelope:
                role["effective_strength_envelope"] = envelope
    return weekly_role_map
