"""Deterministic goal contract at the resolved planner / renderer boundary.

PriorityProfile owns selection and emphasis. The short-camp buckets describe
session framing, not successful adaptations. Only scheduled, effective stimuli
can discharge this contract; neither role names nor LLM prose are evidence.
"""
from __future__ import annotations

from .weight_cut import cut_justifies_goal_deferral

from copy import deepcopy
import re
from typing import Any

from .calendar_context import CalendarLegalityView, build_events, classify_role, role_d_day, week_scope
from .conditioning import _conditioning_structured_profile
from .camp_week_fillers import HANDOFF_UNPLACED_REASON_CODE
from .goal_repair_effective_contact_policy import effective_goal_repair_compression_state
from .late_fight_phase_eligibility import scheduled_phase_for_role
from .planner_context import planner_athlete_model_context
from .prescription_resolver import (
    _role_kind,
    _slot_quality_class_effective,
    _strength_role_slot_groups,
    athlete_dose_state,
)
from .priority_profile import (
    _SELECTED_PRIORITY_TARGET_ALIASES,
    build_priority_profile,
    normalize_priority_values,
)
from .role_labels import athlete_facing_label_for, stamp_role_label
from .tagging import normalize_tag


VERSION = "goal_preservation.v1"
INTENTS = {
    "strength": "meaningful_strength",
    "power": "ballistic_power",
    "speed": "speed_quality",
    "conditioning": "energy_system_training",
    "skill_refinement": "technical_practice",
    "footwork": "footwork_practice",
    "mobility": "mobility_dose",
    "recovery": "recovery_support",
    "weight_cut": "weight_cut_support",
    "trunk_strength": "trunk_training",
}
_ALIASES = {"explosive_power": "power", "conditioning_endurance": "conditioning",
            "endurance": "conditioning", "speed_reaction": "speed"}
_COMPRESSION_REASONS = {
    "high_fatigue": "high_fatigue",
    "active_weight_cut": "weight_cut_pressure",
    "high_pressure_weight_cut": "weight_cut_pressure",
    "aggressive_weight_cut": "weight_cut_pressure",
    "injury_management": "injury_constraint",
    "coach_owned_appointment_capacity": "calendar_capacity",
    "weekly_appointment_capacity": "calendar_capacity",
    "fight_week_override": "fight_proximity",
    "pre_hard_contact_strength_exposure_cap": "pre_hard_contact_managed_stress",
}
# Weekly floor precedence: safety and phase authority already gate everything
# below, then existing exposure, then primary goal, then primary weakness, then
# secondaries. Obligations are repaired in this list order, so when goal and
# weakness genuinely compete for the only safe capacity the goal takes it and
# the weakness records that it found none.
_BUILD_PRIORITIES = {"primary", "primary_weakness"}
# A strength selection inside this window with declared hard sparring is kept
# (never erased at intake) but carried as maintenance and, when no qualifying
# exposure fits, deferred with this cited reason. Same 20-day threshold the
# intake form already applies when it disables a new strength selection.
HARD_SPARRING_STRENGTH_LIMIT_DAYS = 20
# D-13 inward is the finished late-fight tail: its own selector owns membership
# and goal repair never reopens it.
_LATE_TAIL_MAX_D_DAY = 13
_HARD_SPARRING_STRENGTH_LIMIT = "hard_sparring_strength_limit"
_PRIORITY_REASON_CODES = {
    "primary": "primary_goal",
    "primary_weakness": "primary_weakness",
    "secondary": "secondary_goal",
}
# Every official intake weak area resolves deliberately - never by falling off
# the end of a lookup. `web/lib/intake-options.ts::WEAK_AREA_OPTIONS` is the
# source of truth for this list, and `test_weekly_priority_exposure_floor`
# asserts one disposition per option, so a new intake choice cannot be added
# without deciding what services it.
#
# Tier 1: the weekly floor speaks for these directly.
_WEAKNESS_FLOOR_TARGETS = {
    "strength": "strength",
    "power": "power",
    "speed": "speed",
    "footwork": "footwork",
    "mobility": "mobility",
    # The Gas Tank preset pairs a conditioning goal with a gas_tank weakness, so
    # gas_tank is a real floor obligation on the conditioning family: it is
    # recognised, repaired and deferred like any other. It is deliberately NOT
    # microdose-expandable - `_MICRODOSE_GOAL_INTENTS` holds no conditioning goal, so
    # the fallback tier declines it and the conditioning workload-envelope
    # architecture stays the sole authority on conditioning volume.
    "gas_tank": "conditioning",
}

# Tier 2: a canonical subsystem already owns these, and it services them from
# the same weak-area selection this contract reads. Making them floor
# obligations would demand development evidence the coverage ledger has no path
# to observe - a support insert is explicitly not session capacity - so the
# obligation could never be discharged and would block every plan that selects
# them. They are delegated on purpose, and the delegation is recorded.
_WEAKNESS_DELEGATED_SUBSYSTEMS = {
    "balance": "coordination_support_library",
    "coordination": "coordination_support_library",
    "trunk_strength": "session_composition.trunk_support",
    # Defensive: some surfaces submit the option label rather than its value.
    "core_/_trunk_strength": "session_composition.trunk_support",
    "core_trunk_strength": "session_composition.trunk_support",
}
# Delegation names WHO delivers, never that delivery happened. Trunk work now has
# an evidence path (trunk-targeted members at their resolved dose, see
# ``collect_goal_evidence``), so a delegated trunk weakness carries an obligation
# that is checked against the finished schedule like any other. Balance and
# coordination stay delegated without one: support inserts are not session
# capacity and the ledger still has no path to observe them.
_DELEGATED_DELIVERY_OBLIGATIONS = {
    "trunk_strength": "trunk_strength",
    "core_/_trunk_strength": "trunk_strength",
    "core_trunk_strength": "trunk_strength",
}
_SPEED_TAGS = {"speed", "reactive", "reaction", "acceleration", "max_velocity", "speed_reaction"}
_TECHNICAL_TAGS = {"technical", "skill_refinement", "technical_footwork", "footwork", "coordination"}


def _goal(value: Any) -> str:
    token = normalize_tag(str(value or "")) or ""
    return _ALIASES.get(token, token)


def _athlete(brief: dict) -> dict:
    return brief.get("athlete_snapshot") or brief.get("athlete_model") or {}


def primary_weakness_disposition(athlete: dict, focus: dict | None = None) -> dict:
    """How the selected primary weak area is serviced, decided explicitly.

    Every official intake weak area lands in exactly one of these resolutions:

    ``weekly_floor``
        A build obligation on ``target``, carried by the same pipeline the
        primary goal uses.
    ``delegated``
        A canonical subsystem named by ``subsystem`` already services this weak
        area from the same selection, and does it better than a generic
        exposure floor could.
    ``unrecognised``
        Free text that names no known adaptation. It creates no obligation: the
        coverage ledger has no required intent, role match or evidence path for
        it, so an obligation could never be discharged and would block the plan
        forever rather than floor anything.
    ``none``
        No primary weak area was selected.

    Selection vocabulary (``reactive``, ``explosive``, ...) is projected onto
    adaptation families through the same canonical map
    ``selected_priority_targets`` uses, so the weakness is read in the
    vocabulary the profile already owns rather than a second one invented here.
    """
    focus = focus or {}
    profile = build_priority_profile(athlete)
    selected = str(focus.get("primary_weak_area") or profile.primary_weak_area or "")
    raw = normalize_tag(selected) or ""
    if not raw:
        return {"weakness": "", "resolution": "none"}
    record = {"weakness": raw, "selected_label": selected.strip()}
    if raw in _WEAKNESS_DELEGATED_SUBSYSTEMS:
        record = {**record, "resolution": "delegated",
                  "subsystem": _WEAKNESS_DELEGATED_SUBSYSTEMS[raw]}
        obligation = _DELEGATED_DELIVERY_OBLIGATIONS.get(raw)
        if obligation:
            record.update(target=obligation, required_intent=INTENTS[obligation],
                          delivery_verified_by=VERSION)
        return record
    target = _WEAKNESS_FLOOR_TARGETS.get(raw) or _goal(_SELECTED_PRIORITY_TARGET_ALIASES.get(raw, raw))
    if target in INTENTS:
        return {**record, "resolution": "weekly_floor", "target": target,
                "required_intent": INTENTS[target]}
    return {**record, "resolution": "unrecognised"}


def _primary_weakness(athlete: dict, focus: dict | None = None) -> str:
    """The primary weak area as a floor obligation target, or "" when it is none."""
    disposition = primary_weakness_disposition(athlete, focus)
    return disposition.get("target", "") if disposition["resolution"] in {"weekly_floor", "delegated"} else ""


def selected_goals(athlete: dict, focus: dict | None = None) -> list[tuple[str, str]]:
    """Retain every selection, including profiles supplied without PlanInput.

    The primary weak area is a build obligation alongside the primary goal, in
    the weekly floor's order: primary goal, primary weakness, then secondaries.
    A target selected as both is one obligation, satisfied once - the profile
    already treats such a collision as a single canonical priority target.
    """
    focus = focus or {}
    profile = build_priority_profile(athlete)
    primary = _goal(focus.get("primary_goal") or profile.primary_goal or athlete.get("primary_goal"))
    values = [primary, *profile.all_goals,
              *normalize_priority_values(athlete.get("secondary_goals")),
              *normalize_priority_values(focus.get("secondary_goals"))]
    goals = list(dict.fromkeys(_goal(value) for value in values if _goal(value)))
    selections = [(goal, "primary" if goal == primary else "secondary") for goal in goals]
    # Selection scoring and composition already consume secondary weaknesses.
    # Verify delivery too, without promoting them to development obligations or
    # spending another session when a selected goal covers the same quality.
    for value in profile.secondary_weak_areas:
        raw = normalize_tag(value) or ""
        target = (_DELEGATED_DELIVERY_OBLIGATIONS.get(raw)
                  or _WEAKNESS_FLOOR_TARGETS.get(raw)
                  or _goal(_SELECTED_PRIORITY_TARGET_ALIASES.get(raw, raw)))
        if target in INTENTS and target not in {goal for goal, _ in selections}:
            selections.append((target, "secondary"))
    weakness = _primary_weakness(athlete, focus)
    if not weakness or weakness == primary:
        # Merged: one exposure discharges the goal and the weakness together.
        return selections
    # A weakness also named among the goals is still the primary weakness; it is
    # promoted out of the secondary tier rather than carried twice.
    selections = [row for row in selections if row[0] != weakness]
    return [*selections[:1], (weakness, "primary_weakness"), *selections[1:]]


def _hard_sparring_strength_limited(athlete: dict, goal: str) -> bool:
    days = athlete.get("days_until_fight")
    return (goal == "strength" and bool(athlete.get("hard_sparring_days"))
            and isinstance(days, int) and days <= HARD_SPARRING_STRENGTH_LIMIT_DAYS)


def classify_goal_preservation(athlete: dict, focus: dict | None = None) -> list[dict]:
    """Initial obligations, never a claim that the schedule satisfied them."""
    days = athlete.get("days_until_fight")
    readiness = athlete_dose_state(athlete)
    limits = []
    if isinstance(days, int) and days <= 13:
        limits.append("fight_proximity")
    if readiness.get("high_fatigue"):
        limits.append("high_fatigue")
    # Abandoning a requested goal (power, footwork, skill refinement) is a far
    # heavier call than reducing its dose, so it needs a genuinely restrictive
    # cut state — not merely "a cut is active". ``aggressive_weight_cut`` now
    # means high+, which still shapes dose everywhere else, but only a
    # critical/extreme cut removes the planner's obligation to preserve a safe
    # qualifying exposure.
    if cut_justifies_goal_deferral(athlete.get("cut_severity_bucket")):
        limits.append("weight_cut_pressure")
    def _limits(goal: str) -> list[str]:
        return [*limits, _HARD_SPARRING_STRENGTH_LIMIT] if _hard_sparring_strength_limited(athlete, goal) else limits

    return [
        {"goal": goal, "priority": priority,
         # Primary goal and primary weakness are both build obligations: each
         # earns one meaningful exposure per build week. Secondaries stay
         # opportunistic maintenance.
         "state": "build" if priority in _BUILD_PRIORITIES and not _limits(goal) else "maintain",
         "reason_codes": [_PRIORITY_REASON_CODES[priority], *_limits(goal)],
         "required_intent": INTENTS.get(goal, f"selected_goal:{goal}"),
         "evidence": []}
        for goal, priority in selected_goals(athlete, focus)
    ]


def _late_fight_phase(brief: dict) -> str:
    """Recover the late-fight phase when the plan spec did not carry one.

    Prefer the earlier weekly allocation's phase when it is unambiguous; taper is
    the canonical late-fight phase otherwise. This deliberately never keys off
    ``candidate_pools`` ordering, which is not phase authority: with several
    active pools that would silently bind late-fight roles to the wrong pool.
    """
    phases = [
        week.get("phase")
        for week in (brief.get("weekly_role_map") or {}).get("weeks") or []
        if week.get("phase")
    ]
    unique = list(dict.fromkeys(phases))
    return unique[0] if len(unique) == 1 else "TAPER"


def _effective_map(brief: dict, *, phase: str | None = None) -> dict:
    # Direct countdown plans render the final visible sequence. Their weekly
    # map is a separate, earlier allocation and must not supply ghost evidence.
    # Still project the visible D-days into calendar_days so downstream
    # deferral checks can prove a real late-countdown/readiness constraint
    # instead of failing merely because the synthetic week had no calendar.
    if "late_fight_session_sequence" in brief:
        spec = brief.get("late_fight_plan_spec") or {}
        # The authoritative phase is the one that produced the late-fight
        # sequence, carried on late_fight_plan_spec.phase (or passed in by the
        # payload before prescription resolution). Never fall back to an
        # arbitrary candidate-pool key as phase authority.
        phase = phase or spec.get("phase") or _late_fight_phase(brief)
        roles = brief["late_fight_session_sequence"]
        suppressed = deepcopy(spec.get("suppressed_roles") or [])
        calendar_days = []
        seen_days: set[int] = set()
        # Project legal D-days from both the visible roles AND the relevant
        # suppressed roles, so _deferral_constraints() can associate a
        # suppression reason with its coverage window even when the only role on
        # a late D-day was the one that was removed.
        for role in [*roles, *suppressed]:
            day = role_d_day({}, role)
            if not isinstance(day, int) or day < 0 or day in seen_days:
                continue
            seen_days.add(day)
            calendar_days.append({
                "d_day": day,
                "is_fight_day": day == 0,
                "is_after_fight_day": False,
            })
        return {"weeks": [{
            "week_index": 1,
            "phase": phase,
            "session_roles": roles,
            "calendar_days": calendar_days,
            "suppressed_roles": suppressed,
        }]}
    return brief.get("weekly_role_map") or {}


def _tags(item: dict) -> set[str]:
    metadata = item.get("selection_metadata") if isinstance(item.get("selection_metadata"), dict) else {}
    values = [
        *(item.get("movement_patterns") or []),
        *(item.get("tags") or []),
        item.get("primary_adaptation"),
        metadata.get("primary_adaptation"),
        *(item.get("secondary_adaptations") or []),
        *(metadata.get("secondary_adaptations") or []),
    ]
    return {_goal(value) for value in values if value}


def _number(value: Any) -> float:
    if isinstance(value, (list, tuple)):
        return min((_number(v) for v in value), default=0)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


# The unit that follows the second number of an "N x M" dose. Metres and
# seconds are not repetitions: "2 x 20 m" is two short carries, not forty reps,
# and "3 x 30 sec" is three timed holds.
_DOSE_UNIT = re.compile(
    r"\s*(?:(?P<sec>s|sec|secs|second|seconds)|(?P<min>min|mins|minute|minutes)"
    r"|(?P<m>m|metre|metres|meter|meters)|(?P<km>km))\b",
    re.I,
)


def dose_volume(text: str) -> dict:
    """``{"sets", "amount", "unit"}`` for a bank/effective dose; unit is reps,
    sec, min or m. Bank dose syntax only, never rendered prose semantics."""
    text = str(text or "")
    # One descriptive word may sit before the unit ("1-2 controlled sets x 15").
    match = re.search(r"(\d+)(?:\s*[-–]\s*\d+)?\s*(?:(?:[a-z]+\s+)?(?:sets?|holds?)\s*)?[x×]\s*(\d+)(?:\s*[-–]\s*\d+)?",
                      text, re.I)
    if match is not None:
        unit = _DOSE_UNIT.match(text[match.end():])
        kind = next((key for key, value in (unit.groupdict() if unit else {}).items() if value), "reps")
        amount = int(match[2]) * (1000 if kind == "km" else 1)
        return {"sets": int(match[1]), "amount": amount, "unit": "m" if kind == "km" else kind}
    # A contrast complex is dosed in rounds: "3-4 rounds: heavy movement
    # 2-3 reps @ 85-92% 1RM -> ...". Each round is one working set.
    match = re.search(r"(\d+)(?:\s*[-–]\s*\d+)?\s*rounds?\b[:;]?[^;]*?\b(\d+)(?:\s*[-–]\s*\d+)?\s*reps?\b", text, re.I)
    if match is not None:
        return {"sets": int(match[1]), "amount": int(match[2]), "unit": "reps"}
    return {"sets": 0, "amount": 0, "unit": "reps"}


def _sets_reps(text: str) -> tuple[int, int]:
    """Sets and repetitions. A distance or time dose has sets but no reps."""
    volume = dose_volume(text)
    return volume["sets"], volume["amount"] if volume["unit"] == "reps" else 0


def _witness_volume(volume: dict) -> dict:
    witness = {"sets": volume["sets"], "reps": volume["amount"] if volume["unit"] == "reps" else 0}
    if volume["unit"] != "reps":
        witness.update(volume_unit=volume["unit"], volume_amount=volume["amount"])
    return witness


def strength_intensity(text: str) -> dict:
    """Minimum declared working intensity, not an exercise-name inference."""
    rpe = re.search(r"(?:RPE\s*[:=]?\s*)(\d+(?:\.\d+)?)(?:\s*[-–]\s*\d+(?:\.\d+)?)?", text, re.I)
    effort = re.search(r"(\d+)(?:\s*[-–]\s*\d+)?/10\s*effort", text, re.I)
    percent = re.search(r"(\d+)(?:\s*[-–]\s*\d+)?\s*%", text)
    if rpe or effort:
        return {"minimum_rpe": float((rpe or effort)[1])}
    if percent:
        return {"minimum_load_percent": float(percent[1])}
    return {}


def _slot_allowed(slot: dict, role: dict, brief: dict) -> bool:
    selected = slot.get("selected") or {}
    if not selected or selected.get("restriction_hits", 0) or selected.get("blocked"):
        return False
    names = role.get("preferred_exercise_names") or []
    if names and selected.get("name") not in names:
        return False
    day = role_d_day({}, role)
    allowed_by_day = (brief.get("late_fight_plan_spec") or {}).get("allowed_exercises_by_day") or {}
    if f"D-{day}" in allowed_by_day and selected.get("name") not in allowed_by_day[f"D-{day}"]:
        return False
    metadata = selected.get("selection_metadata") if isinstance(selected.get("selection_metadata"), dict) else {}
    late_windows = metadata["late_windows"] if "late_windows" in metadata else selected.get("late_windows")
    if isinstance(day, int) and day <= 21 and late_windows:
        from .late_selector_windows import classify_late_selector_window
        if classify_late_selector_window(day) not in late_windows:
            return False
    tags = _tags(selected) | set(selected.get("restriction_tags") or [])
    if tags & set(role.get("blocked_tags") or []):
        return False
    # Use canonical structured restriction keys, not an injury-name heuristic.
    restrictions = brief.get("restrictions") or []
    for restriction in restrictions:
        key = _goal(restriction.get("restriction") or restriction.get("key"))
        if key in {_goal(tag) for tag in selected.get("restriction_tags") or []}:
            return False
    return True


def _strength_stimuli(role: dict, slots: list[dict], brief: dict) -> list[dict]:
    from .session_composition import slot_adaptations

    stimuli = []
    capped = isinstance(role.get("strength_dose_cap"), dict)
    prescriptions = role.get("effective_strength_prescriptions") or []
    envelope = role.get("effective_strength_envelope") or {}
    cap = role.get("strength_dose_cap") or {}
    for slot in slots:
        if not _slot_allowed(slot, role, brief):
            continue
        selected = slot["selected"]
        resolved = next((p for p in prescriptions if p.get("slot_id") == slot.get("slot_id")
                         and p.get("name") == selected.get("name")), None)
        if capped and resolved is None:
            continue  # Missing effective authority cannot fall back to bank dose.
        dose = resolved or {"effective_prescription": selected.get("prescription", ""),
                            "dose_authority": "exercise_bank"}
        volume = dose_volume(str(dose.get("effective_prescription") or ""))
        sets = int(dose.get("effective_max_sets", volume["sets"]))
        volume = {**volume, "sets": sets}
        if volume["unit"] == "reps":
            volume["amount"] = int(dose.get("effective_max_reps", volume["amount"]))
        if sets < 1 or volume["amount"] < 1 or (capped and _number(cap.get("max_sets")) < 1):
            continue
        effective_slot = {**slot, **{key: selected[key] for key in ("quality_class", "anchor_capable", "support_only") if key in selected}}
        kind = _role_kind(effective_slot)
        quality = _slot_quality_class_effective(effective_slot)
        loaded = (
            (kind in {"anchor", "secondary"} and quality == "anchor_loaded")
            or (kind == "hybrid" and quality == "anchor_power")
            or (quality == "anchor_force_isometric" and selected.get("real_strength_maintenance") is True)
        )
        intensity = strength_intensity(str(dose.get("effective_prescription") or ""))
        meaningful_intensity = intensity.get("minimum_rpe", 0) >= 6 or intensity.get("minimum_load_percent", 0) >= 60
        adaptations = slot_adaptations(slot)
        # Strength is lifted for repetitions, or held as a maximal isometric. A
        # distance ("2 x 20 m" carry) is never a strength-development dose, and a
        # member whose own method does not develop strength cannot evidence it.
        strength_volume = volume["unit"] == "reps" or (
            volume["unit"] == "sec" and quality == "anchor_force_isometric")
        intents = []
        if (loaded and strength_volume and "strength" in adaptations and sets >= 2 and meaningful_intensity
                and kind != "support" and cap.get("loaded_allowed") is not False
                and envelope.get("loaded_allowed") is not False
                and dose.get("effective_loaded") is not False):
            intents.append("meaningful_strength")
        if quality == "anchor_power" and kind in {"power", "hybrid"}:
            intents.append("ballistic_power")
            if _tags(selected) & _SPEED_TAGS:
                intents.append("speed_quality")
        # Trunk training is a trunk-TARGETED member at its resolved dose. Trunk
        # involvement (a squat's bracing, a sled push's posture) is not.
        if "trunk_strength" in adaptations:
            intents.append("trunk_training")
        if not intents:
            continue
        stimuli.append({"slot_id": slot.get("slot_id"), "name": selected.get("name"),
                        "intents": intents, "quality_class": quality,
                        "effective_prescription": dose["effective_prescription"],
                        "dose_authority": dose["dose_authority"],
                        **_witness_volume(volume),
                        "development_capable": sets >= 2,
                        **(intensity if "meaningful_strength" in intents else {})})
    return stimuli


# Weekly priority exposure floor - the microdose tier.
#
# ``_restore_goal_roles`` can only restore a WHOLE role from the week's
# ``goal_repair_candidates``. When the session or category cap blocks that, a
# primary goal could go cold for an entire build week even though an existing
# strength session that day could safely host one more already-selected
# exercise. This tier is that fallback.
#
# It never authors an exercise or a dose. The only candidates are the phase's
# Stage 1 ``strength_slots``, which already passed injury, equipment and
# restriction filtering at selection; the dose is whatever the canonical
# ``prescription_resolver`` resolves for the host's scheduled day; and the
# evidence is read back through the ordinary strength-evidence path. When no
# pool candidate survives those owners, the gap is recorded, not filled.
#
# Only goals the strength pool can evidence are floor-eligible. Conditioning
# owns its own system/workload architecture; recovery and weight_cut are
# serviced by deterministic daily support. Moving footwork members retain their
# conditioning-bank authority when composed alongside strength.
_MICRODOSE_GOAL_INTENTS: dict[str, str] = {
    "power": "ballistic_power",
    "speed": "speed_quality",
    "strength": "meaningful_strength",
    "trunk_strength": "trunk_training",
}

# A microdose attaches only to an existing strength session that already has
# closed membership: only the strength dose resolver can price a strength-slot
# member, and an empty session is not a session to hang development on.
# Sparring, recovery, fight day and zero-cost tactical roles are never hosts:
# hard contact owns its own freshness, recovery-only stays recovery-only, and a
# Tactical Watch day is not physical capacity just because it sits on the
# calendar.
_MICRODOSE_HOST_CATEGORIES = {"strength"}
_MICRODOSE_FORBIDDEN_HOST_CATEGORIES = {"sparring", "recovery", "fight_day", "mobility"}
_MICRODOSE_SOURCE = "phase_pool"


def _microdose_stimuli(role: dict) -> list[dict]:
    """Evidence for a legacy, spec-authored microdose on a previously saved brief.

    A pool-sourced microdose is an ordinary closed strength member, so its
    evidence comes from ``_strength_stimuli`` with the resolver's dose; it must
    not be counted a second time here.
    """
    microdose = role.get("priority_microdose")
    if not isinstance(microdose, dict) or not microdose.get("intents"):
        return []
    if microdose.get("source") == _MICRODOSE_SOURCE:
        return []
    prescription = str(microdose.get("prescription") or "")
    # The witness carries its volume in its own unit, exactly as
    # `_strength_stimuli` does. A sets-only witness cannot be checked against a
    # rendered dose downstream, and "2 x 20 m" is not twenty reps.
    volume = {**dose_volume(prescription), "sets": int(_number(microdose.get("sets")))}
    return [
        {
            "name": microdose.get("name"),
            "intents": list(microdose["intents"]),
            "effective_prescription": prescription,
            "dose_authority": "weekly_priority_exposure_floor",
            **_witness_volume(volume),
            # A spec-authored touch never passed the bank, the resolver or the
            # load checks: it can keep a goal warm, never certify development.
            "development_capable": False,
        }
    ]


def _strength_members(role: dict) -> list[dict]:
    return [
        item for item in role.get("selected_exercise_assignments") or []
        if isinstance(item, dict) and item.get("slot_group") == "strength_slots"
    ]


# Provenance, not rendered text. A declared hard-sparring appointment keeps
# these markers after the contact resolver reduces its effective load, which is
# exactly the case `combat_load_policy` holds strict.
_HARD_SPARRING_PROVENANCE_KEYS = (
    "declared_hard_sparring",
    "hard_sparring_locked",
    "capped_declared_hard_sparring_days",
)


def _carries_hard_sparring_provenance(role: dict) -> bool:
    if str(role.get("role_key") or "").strip().lower() == "hard_sparring_day":
        return True
    if str(role.get("preferred_pool") or "").strip() == "declared_hard_sparring_days":
        return True
    return any(role.get(key) for key in _HARD_SPARRING_PROVENANCE_KEYS)


def _microdose_host_roles(week: dict) -> list[dict]:
    """Existing strength sessions in this week that may host a microdose."""
    hosts = []
    for role in week.get("session_roles") or []:
        if not isinstance(role, dict):
            continue
        category = str(role.get("category") or "").strip().lower()
        if category in _MICRODOSE_FORBIDDEN_HOST_CATEGORIES:
            continue
        if _carries_hard_sparring_provenance(role):
            # Declared hard sparring keeps its strict collision treatment even
            # after the canonical resolver converts its effective contact down
            # to technical-only. Category alone would not catch a converted day
            # if it were ever recategorised, so check provenance directly.
            continue
        if category not in _MICRODOSE_HOST_CATEGORIES:
            continue
        if role.get("priority_microdose") is not None:
            continue
        if (role.get("governance") or {}).get("hard_suppression_reasons"):
            continue
        # A support insert is not a session; it cannot host development.
        if role.get("support_insert_category") or role.get("support_kind"):
            continue
        # An empty session is not a host: nothing was composed there for a
        # touch to hang off, and filling it would author the session.
        if not _strength_members(role):
            continue
        hosts.append(role)
    return hosts


def _member_slot(member: dict, pools: dict, fallback_phase: str, slot_group: str) -> dict | None:
    """The exact Stage 1 slot (or alternate) a final member was assigned from.

    Identity is the member's own slot id, source phase and name. A compatible
    candidate that merely could have filled the session is never a match, and a
    late-fight alternate keeps its ``<slot_id>:alternate:<n>`` identity.
    """
    name = str(member.get("name") or "").strip()
    slot_id = str(member.get("slot_id") or "")
    if not name or not slot_id:
        return None
    phase = str(member.get("source_phase") or member.get("phase") or fallback_phase or "").strip().upper()
    pool = pools.get(phase) if isinstance(pools.get(phase), dict) else {}
    slots = [*(pool.get(slot_group) or []), *((pool.get("late_tail_candidates") or []) if slot_group == "conditioning_slots" else [])]
    base_id, _, alternate = slot_id.partition(":alternate:")
    for slot in slots:
        if not isinstance(slot, dict) or str(slot.get("slot_id") or "") != base_id:
            continue
        option = slot.get("selected") if not alternate else None
        if alternate.isdigit():
            alternates = slot.get("alternates") or []
            option = alternates[int(alternate)] if int(alternate) < len(alternates) else None
        elif member.get("selected_option") is False:
            # Composition substituted an alternate inside the same slot.
            option = next((a for a in slot.get("alternates") or [] if isinstance(a, dict)
                           and str(a.get("name") or "").strip() == name), None)
        if isinstance(option, dict) and str(option.get("name") or "").strip() == name:
            return {**slot, "selected": option}
    return None


def _other_stimuli(role: dict, pools: dict, phase: str, brief: dict) -> list[dict]:
    # Evidence is the role's own final membership. A pool candidate that matches
    # the session's system, but was not assigned to it, trains nothing: swimming
    # on the day must never be credited as the bike drill left in the pool.
    stimuli = []
    category = role.get("category")
    system = role.get("preferred_system")
    for member in role.get("selected_exercise_assignments") or []:
        # Legacy persisted conditioning members may omit their slot group.
        if (not isinstance(member, dict)
                or member.get("slot_group", "conditioning_slots") != "conditioning_slots"):
            continue
        slot = _member_slot(member, pools, phase, "conditioning_slots")
        if slot is None or not _slot_allowed(slot, role, brief):
            continue
        selected = slot["selected"]
        member_system = str(slot.get("role") or system or "").lower()
        # A bank-prescribed moving footwork drill may complement a strength
        # session; coach instructions and mental preparation earn no exposure.
        if (category != "conditioning" and member_system != "technical_footwork"
                and not (member_system == "alactic"
                         and member.get("consolidated_from_system") == "alactic"
                         and member.get("consolidated_from_role_key"))):
            continue
        system = member_system
        # Serialized conditioning dose/cost authority lives under
        # selected.selection_metadata. Preserve compatibility with older payloads
        # that still carry those fields flat, but prefer the canonical metadata.
        metadata = selected.get("selection_metadata") if isinstance(selected.get("selection_metadata"), dict) else {}
        dose_source = dict(selected)
        for field in (
            "work_sec", "rest_sec", "rounds", "total_minutes", "rpe", "rpe_max",
            "impact_cost", "lactate_load", "movement_cost", "duration", "timing", "modality", "tags",
            "distance_m", "distance_segments_m", "recovery_mode",
        ):
            if metadata.get(field) is not None:
                dose_source[field] = metadata[field]
        bank_interval = (_number(dose_source.get("work_sec")), _number(dose_source.get("rounds")))
        # The member's own effective dose wins over the bank dose: composition
        # may partition high-load rounds, and the persisted pool is compacted.
        for field in ("work_sec", "rest_sec", "rounds", "rpe"):
            if member.get(field) is not None:
                dose_source[field] = member[field]
        if member.get("effective_rounds") is not None:
            dose_source["rounds"] = member["effective_rounds"]
        # total_minutes is elapsed block time (work plus between-round rest).
        # The bank's figure describes the bank's own interval, so once the
        # member re-anchors work or rounds (an athlete-round 6 x 3 min of a
        # 6 x 2 min bank drill) it is restated from that effective dose, as
        # config.conditioning_effective_dose does. The prescription is untouched.
        from .config import conditioning_work_is_timed
        effective_work, effective_rounds = _number(dose_source.get("work_sec")), _number(dose_source.get("rounds"))
        if ((effective_work, effective_rounds) != bank_interval and effective_work > 0 and effective_rounds > 0
                and conditioning_work_is_timed(dose_source)):
            dose_source["total_minutes"] = (
                effective_work * effective_rounds
                + max(0.0, effective_rounds - 1) * _number(dose_source.get("rest_sec"))
            ) / 60.0
        # A recovery morph / support flush does not become energy-system work
        # through its old pool identity. Positive work metadata is required.
        if (role.get("late_camp_role_morph") or role.get("counts_toward_conditioning_cap") is False
                or system in (role.get("blocked_systems") or [])):
            continue
        minutes = _number(dose_source.get("total_minutes"))
        work = _number(dose_source.get("work_sec"))
        rounds = _number(dose_source.get("rounds"))
        distance_m = _number(dose_source.get("distance_m"))
        segments = dose_source.get("distance_segments_m") or []
        has_distance_dose = distance_m > 0
        if not (minutes > 0 or (work > 0 and rounds > 0) or has_distance_dose):
            continue
        if "duration_cap_minutes" in role:
            minutes = min(minutes, _number(role["duration_cap_minutes"]))
        if "round_cap" in role:
            rounds = min(rounds, _number(role["round_cap"]))
        intents = []
        from .config import conditioning_dose_active_work_seconds
        active_work = conditioning_dose_active_work_seconds(dose_source) or 0
        if system in {"aerobic", "glycolytic"} and (
            active_work >= 180
            or (metadata.get("standalone_protocol") and active_work > 0)
            # Distance is a genuine prescribed workload, not timed seconds.
            # A short sprint alone is not sufficient aerobic development.
            or (has_distance_dose and distance_m * max(rounds, 1) >= 1600)
        ):
            intents.append("energy_system_training")
        profile = _conditioning_structured_profile(dose_source, system=system)
        if (system in {"alactic", "atp_pcr", "atp-pcr"}
                and len(segments) >= 4
                and all(isinstance(d, (int, float)) and 0 < d <= 100 for d in segments)
                and dose_source.get("recovery_mode") == "walk_back"
                and (profile["rpe"] or 0) >= 8
                and profile["speed_development"]):
            intents.append("speed_quality")
        if system in {"alactic", "atp_pcr", "atp-pcr"} and work > 0 and rounds >= 2 and profile["speed_development"]:
            intents.append("speed_quality")
            # Maximal propulsive bursts can maintain ballistic power regardless
            # of the module that selected them. Require real short work/full
            # rest, multiple efforts, high effort and explosive mechanics;
            # a technical cue or a braking drill cannot earn this intent.
            if profile["rpe"] is not None and profile["rpe"] >= 8 and {"explosive", "mech_ballistic"}.issubset(_tags(selected)):
                intents.append("ballistic_power")
        if _tags(selected) & _TECHNICAL_TAGS:
            intents.append("technical_practice")
        if "footwork" in _tags(selected) or system == "technical_footwork":
            intents.append("footwork_practice")
        if intents:
            stimulus = {"slot_id": member.get("slot_id"), "name": member.get("name"),
                            "intents": intents, "system": system,
                            "dose_authority": "structured_conditioning_dose",
                            "work_sec": work, "rounds": rounds, "total_minutes": minutes,
                            "rest_sec": _number(dose_source.get("rest_sec")),
                            # Full-rest speed work need not create high session
                            # exertion. Easy recovery work still cannot develop
                            # speed, and power keeps its separate maximal-effort gate.
                            "development_capable": "speed_quality" not in intents or profile["rpe"] is None or profile["rpe"] >= 6,
                            "effective_prescription": member.get("effective_prescription")
                            or selected.get("prescription") or ""}
            if not conditioning_work_is_timed(dose_source):
                # Existing rep-dose witnesses/validator already speak sets and
                # reps. Preserve ballistic speed evidence without fake seconds.
                rep = dose_volume(stimulus['effective_prescription'])
                stimulus.update(sets=rep['sets'], reps=rep['amount'])
                for field in ('work_sec', 'rounds', 'total_minutes'):
                    stimulus.pop(field, None)
                if has_distance_dose:
                    stimulus["distance_m"] = distance_m
            stimuli.append(stimulus)
    stimuli += _embedded_trunk_stimuli(role, pools, phase)
    # Locked technical / mobility inserts have an actual deterministic duration.
    duration = _number(role.get("prescribed_duration_min") or role.get("duration_min"))
    support = role.get("support_insert_category") or role.get("support_kind")
    if duration > 0 and not role.get("selected_exercise_assignments"):
        intents = []
        if category == "mobility" or support == "mobility":
            intents.append("mobility_dose")
        if category == "technical" or support in {"technical", "coordination", "technical_footwork"}:
            intents.append("technical_practice")
        if support == "technical_footwork":
            intents.append("footwork_practice")
        if category == "recovery" or support in {"recovery", "recovery_walk"}:
            intents.append("recovery_support")
        if support == "conditioning_maintenance" and duration >= 6:
            intents.append("energy_system_training")
        if intents:
            stimuli.append({"intents": intents, "duration_min": duration,
                            "name": (role.get("preferred_exercise_names") or [role.get("athlete_facing_label") or athlete_facing_label_for(role.get("role_key"))])[0],
                            "effective_prescription": f"{duration:g} minutes",
                            "development_capable": support != "conditioning_maintenance",
                            "dose_authority": "scheduled_support_dose"})
    return stimuli


def _embedded_trunk_stimuli(role: dict, pools: dict, phase: str) -> list[dict]:
    """Low-load trunk support composed into a conditioning session.

    It is real trunk exposure, but at a support dose by construction, so it can
    maintain a trunk priority and never counts as development. The member must
    still resolve to a trunk-targeted pool slot: a legacy membership that
    embedded a heavy sled push as "trunk support" earns nothing.
    """
    from .session_composition import slot_adaptations

    if role.get("category") != "conditioning":
        return []
    stimuli = []
    for member in role.get("selected_exercise_assignments") or []:
        if not isinstance(member, dict) or member.get("support_dose_category") != "low_load_trunk":
            continue
        slot = _member_slot(member, pools, phase, "strength_slots")
        if slot is None or "trunk_strength" not in slot_adaptations(slot):
            continue
        prescription = str(member.get("effective_prescription") or "")
        volume = dose_volume(prescription)
        if volume["sets"] < 1 or volume["amount"] < 1:
            continue
        stimuli.append({"slot_id": member.get("slot_id"), "name": member.get("name"),
                        "intents": ["trunk_training"], "effective_prescription": prescription,
                        "dose_authority": "embedded_trunk_support", **_witness_volume(volume),
                        "development_capable": False})
    return stimuli


def collect_goal_evidence(brief: dict) -> list[dict]:
    """Recompute from live schedule and effective prescriptions, ignoring caches."""
    role_map = _effective_map(brief)
    pools = brief.get("candidate_pools") or {}
    strength = {id(role): slots for _, role, slots in _strength_role_slot_groups(
        weekly_role_map=role_map, candidate_pools=pools)}
    evidence = []
    for week in role_map.get("weeks") or []:
        for index, role in enumerate(week.get("session_roles") or [], 1):
            day = role_d_day(week, role)
            if day is None or day <= 0 or role.get("render_mandatory") is False:
                continue
            if "late_fight_session_sequence" not in brief and not any(
                d.get("d_day") == day and not d.get("is_after_fight_day") and not d.get("is_fight_day")
                for d in week.get("calendar_days") or []
            ):
                continue
            if (role.get("governance") or {}).get("hard_suppression_reasons"):
                continue
            phase = scheduled_phase_for_role(role, athlete_model=_athlete(brief), spec_phase=week.get("phase")) or week.get("phase")
            stimuli = _strength_stimuli(role, strength.get(id(role), []), brief)
            stimuli += _other_stimuli(role, pools, phase, brief)
            stimuli += _microdose_stimuli(role)
            for stimulus in stimuli:
                # D-1 protocol can support readiness; never training adaptations.
                if day <= 1:
                    stimulus["intents"] = [v for v in stimulus["intents"] if v in {"recovery_support", "mobility_dose"}]
                if not stimulus["intents"]:
                    continue
                evidence.append({"week_index": week.get("week_index"), "phase": phase,
                                 "session_index": role.get("session_index", index),
                                 "role_key": role.get("role_key"), "d_day": day,
                                 "development_quality": day > 13 and phase != "TAPER" and stimulus.get("development_capable", True),
                                 **stimulus})
    # These selected goals are serviced by deterministic daily support, not S&C
    # slots. Do not count these records toward conditioning or strength.
    support = brief.get("computed_support") or {}
    for phase, nutrition in (support.get("nutrition", {}).get("by_phase") or {}).items():
        if (nutrition.get("weight_cut") or {}).get("active") and nutrition.get("protein_g_per_day"):
            evidence.append({"phase": phase, "intents": ["weight_cut_support"],
                             "dose_authority": "computed_support.nutrition", "development_quality": True,
                             "risk_band": nutrition["weight_cut"].get("risk_band")})
    return evidence


def _requirements(entry: dict, brief: dict) -> list[dict]:
    """Strength continuity in eligible weeks; other maintenance every 14 days.

    Partial development windows share the same requirement. Final fight-week
    strength is excluded by its canonical loaded-work cutoff. Multiple lifts on
    one day cannot pay for multiple windows.
    """
    if entry["goal"] in {"recovery", "weight_cut"}:
        return [{"min_d_day": None, "max_d_day": None}]
    days = _athlete(brief).get("days_until_fight")
    if not isinstance(days, int):
        return [{"min_d_day": None, "max_d_day": None}]
    # Recurring promises belong to the actual normal-camp weeks, not a
    # camp-wide bucket. Two exposures in the opening week cannot discharge the
    # next pre-tail week. D-13 inward remains the finished tail's maintenance
    # / primer decision, and TAPER never creates a mandatory loaded exposure.
    horizon_floor = int(_athlete(brief).get("open_block_horizon_days") or 0)
    calendar = {d["d_day"] for week in _effective_map(brief).get("weeks") or []
                for d in week.get("calendar_days") or [] if isinstance(d.get("d_day"), int)}
    horizon = _plannable_horizon(brief, days)
    complete_calendar = horizon is not None and set(range(max(14, horizon_floor), horizon + 1)) <= calendar
    if complete_calendar and (entry["goal"] == "strength" or entry["state"] == "build"):
        weekly_windows = []
        for week in _effective_map(brief).get("weeks") or []:
            trainable = [d["d_day"] for d in week.get("calendar_days") or []
                         if isinstance(d.get("d_day"), int) and d["d_day"] <= days
                         and not d.get("is_fight_day") and not d.get("is_after_fight_day")]
            eligible = [day for day in trainable if day >= max(14, horizon_floor)]
            if eligible and str(week.get("phase") or "").upper() != "TAPER":
                window = {"min_d_day": min(eligible), "max_d_day": max(eligible)}
                # A programme week that straddles the D-14 cutoff leaves only a
                # boundary fragment (often D-14 alone) eligible. That sliver is
                # not a development week of its own: it extends the adjacent
                # earlier window, as the cadence path's earliest window absorbs
                # its remainder. A majority-eligible week keeps its own promise.
                if (weekly_windows and len(eligible) < len(trainable)
                        and 2 * len(eligible) < len(trainable)
                        and weekly_windows[-1]["min_d_day"] == window["max_d_day"] + 1):
                    weekly_windows[-1]["min_d_day"] = window["min_d_day"]
                else:
                    weekly_windows.append(window)
        if weekly_windows:
            return weekly_windows
    # Direct late-countdown plans retain their own maintenance window and
    # causal readiness evidence. Weekly reconciliation never reopens that tail.
    normal_camp = days > 13 and "late_fight_session_sequence" not in brief
    cutoff = 14 if entry["state"] == "build" or (normal_camp and entry["goal"] == "strength") else (8 if entry["goal"] == "strength" else 2)
    # An open development block is planned clear of any fight: its calendar
    # starts at the horizon, so nothing nearer than that is a plannable window.
    cutoff = max(cutoff, horizon_floor)
    width = 7 if entry["state"] == "build" or entry["goal"] == "strength" else 14
    span = max(1, days - cutoff + 1)
    # Windows are anchored at the cutoff and counted outward, so any remainder
    # falls at the far end - the opening days of camp. Rounding up turned that
    # remainder into its own requirement, demanding a full development-week
    # exposure from a sliver that can be a single day (a 21-day camp yields a
    # D-21..D-21 window). The earliest window absorbs the remainder instead, so
    # every requirement covers a real development week.
    count = max(1, span // width)
    windows = [
        {
            "min_d_day": cutoff + index * width,
            "max_d_day": days
            if index == count - 1
            else min(days, cutoff + (index + 1) * width - 1),
        }
        for index in range(count)
    ]
    # The camp's first trainable day is the calendar's, not the intake's
    # days-out: a camp created at D-14 starts training at D-13. A window is
    # clamped to that day, and one with no plannable day left is marked so a
    # gap there is attributed to the calendar rather than left unexplained.
    horizon = _plannable_horizon(brief, days)
    if horizon is not None:
        for window in windows:
            if window["max_d_day"] > horizon:
                window["max_d_day"] = horizon
            if window["min_d_day"] > window["max_d_day"]:
                window["unplannable"] = True
    return windows


def _plannable_horizon(brief: dict, days: int) -> int | None:
    """The earliest countdown day the final calendar can train on.

    Only the creation day itself is ever removed: a calendar that does not hold
    D-<days> starts the next day. A partial calendar never shortens a camp.
    """
    calendar = {
        day["d_day"]
        for week in _effective_map(brief).get("weeks") or []
        for day in week.get("calendar_days") or []
        if isinstance(day.get("d_day"), int)
    }
    if not calendar:
        return None
    return days if days in calendar else days - 1


def _coverage(entry: dict, brief: dict, evidence: list[dict]) -> tuple[list[dict], list[dict]]:
    matching = [e for e in evidence if entry["required_intent"] in e["intents"]
                and (entry["state"] != "build" or e.get("development_quality"))]
    missing, witnesses = [], []
    for window in _requirements(entry, brief):
        witness = next((e for e in matching if window["min_d_day"] is None or
                       (e.get("d_day") is not None and window["min_d_day"] <= e["d_day"] <= window["max_d_day"])), None)
        if witness is None:
            missing.append(window)
        elif witness not in witnesses:
            witnesses.append(witness)
    return witnesses, missing


def athlete_goal_explanation(entry: dict) -> str:
    """Plain, factual plan context; never expose the internal repair workflow."""
    goal = str(entry.get("goal") or "training").replace("_", " ")
    if entry.get("satisfied") is True:
        return ""
    if entry.get("state") != "defer":
        return f"This plan does not yet include work for your {goal} goal."
    reasons = set(entry.get("reason_codes") or []) | {
        item.get("reason_code") for item in entry.get("constraints") or [] if isinstance(item, dict)
    }
    if "calendar_capacity" in reasons:
        return f"Your current training schedule leaves no room for separate {goal} work this block."
    if "injury_constraint" in reasons:
        return f"{goal.capitalize()} work is on hold while your current symptoms are managed."
    if "high_fatigue" in reasons:
        return f"{goal.capitalize()} work is on hold to allow recovery."
    return f"{goal.capitalize()} work is not scheduled in this block."


def _role_matches_goal(role: dict, goal: str) -> bool:
    category = role.get("category")
    if goal in {"strength", "power", "trunk_strength"}:
        return category == "strength"
    if goal == "speed":
        return category == "strength" or role.get("preferred_system") in {"alactic", "atp_pcr"}
    if goal == "conditioning":
        return category == "conditioning" and role.get("preferred_system") in {"aerobic", "glycolytic", "alactic"}
    if goal == "footwork":
        return category == "technical" or (category == "conditioning" and role.get("preferred_system") == "technical_footwork")
    return category == {"mobility": "mobility", "recovery": "recovery", "skill_refinement": "technical"}.get(goal)


def _realised_load_not_increased(before: Any, after: Any) -> bool:
    """True when adding the member leaves the host's measured cross-day load intact.

    Both values come from ``session_composition.realised_role_stress``, the
    measurement the calendar's realised-load revalidation already judged the
    host on. An added member that raises either axis, or touches a new body
    region, would change a load that pass will not see again, so it is refused.
    """
    if after is None:
        return before is None
    if before is None:
        return False
    return (
        after.systemic <= before.systemic
        and after.neural_mechanical <= before.neural_mechanical
        and set(after.regions) <= set(before.regions)
    )


def _attach_goal_microdose(brief: dict, ordinal: int, entry: dict) -> dict | None:
    """Weekly exposure floor: one more already-selected exercise on an existing session.

    Reached only when ``_restore_goal_roles`` has already established that a
    whole role cannot be restored inside the week's session/category budget.
    Adds no session, no training day and no frequency, and never authors an
    exercise or a dose: the candidate is a Stage 1 strength-pool slot (already
    injury-, equipment- and restriction-filtered), it must fit the host's
    composed exercise cap and leave the host's measured load unchanged, and its
    dose is the one ``prescription_resolver`` resolves for the host's day.

    Returns an audit record, or None when the goal is not floor-eligible at all.
    """
    from .prescription_resolver import apply_effective_strength_prescriptions
    from .session_composition import (
        _scheduled_phase_for_role,
        assignment_from_slot,
        realised_role_stress,
    )

    if entry["goal"] not in _MICRODOSE_GOAL_INTENTS and entry["goal"] not in {"footwork", "mobility"}:
        # Conditioning and support-serviced goals are owned elsewhere.
        return None
    if entry["state"] != "build":
        # A maintenance obligation has a 14-day window; it does not need a
        # weekly floor and must not spend capacity as if it did.
        return None

    week = brief["weekly_role_map"]["weeks"][ordinal]
    if str(week.get("phase") or "").upper() == "TAPER":
        return {"week_index": week.get("week_index"), "result": "floor_not_applied",
                "reason_codes": ["taper_no_developmental_floor"]}

    # Already covered somewhere in this week: the floor is a floor, not a bonus.
    week_days = {
        day.get("d_day")
        for day in week.get("calendar_days") or []
        if isinstance(day.get("d_day"), int)
    }
    covered = any(
        e.get("d_day") in week_days
        and entry["required_intent"] in e["intents"]
        and e.get("development_quality")
        for e in collect_goal_evidence(brief)
    )
    if covered:
        return {"week_index": week.get("week_index"), "result": "floor_already_met",
                "reason_codes": ["existing_weekly_exposure"]}

    if entry["goal"] not in _MICRODOSE_GOAL_INTENTS:
        # No selected pool family can evidence this goal; inventing a drill to
        # cover it is exactly what this tier must not do.
        return {"week_index": week.get("week_index"), "goal": entry["goal"],
                "result": "floor_no_safe_candidate",
                "reason_codes": ["no_phase_pool_candidate_for_goal"]}

    hosts = _microdose_host_roles(week)
    refusals: list[str] = []
    protected_days = {str(d.get("day") or "").lower() for d in week.get("intentionally_unused_days") or []}
    pools = brief.get("candidate_pools") or {}
    for host in hosts:
        host_day = role_d_day(week, host)
        if not isinstance(host_day, int) or host_day <= 13:
            # Reuse the established developmental cutoff: no new development
            # inside the taper / fight-week tail.
            continue
        if str(host.get("scheduled_day_hint") or "").lower() in protected_days:
            # A truly unused / rest / recovery day stays protected.
            continue
        cap = (host.get("strength_composition_policy") or {}).get("effective_exercise_cap")
        if isinstance(cap, int) and len(_strength_members(host)) >= cap:
            # The composition owner already sized this session for the
            # athlete's readiness; the floor does not overrule it.
            refusals.append("host_at_composed_exercise_cap")
            continue
        phase = _scheduled_phase_for_role(week, host) or str(week.get("phase") or "").strip().upper()
        pool = pools.get(phase) if isinstance(pools, dict) else None
        slots = [slot for slot in (pool or {}).get("strength_slots") or [] if isinstance(slot, dict)]
        members = {
            (str(item.get("slot_id") or ""), str(item.get("name") or ""))
            for item in _strength_members(host)
        }
        host_index = (week.get("session_roles") or []).index(host)
        candidates = [
            slot for slot in slots
            if (str(slot.get("slot_id") or ""), str((slot.get("selected") or {}).get("name") or "")) not in members
            and _slot_allowed(slot, host, brief)
        ]
        if not candidates:
            refusals.append("no_eligible_phase_pool_candidate")
            continue
        _, missing_before = _coverage(entry, brief, collect_goal_evidence(brief))
        before_stress = realised_role_stress(host)
        for slot in candidates:
            assignment = assignment_from_slot(phase, "strength_slots", slot)
            if not assignment:
                continue
            assignment["priority_microdose_goal"] = entry["goal"]
            trial = deepcopy(brief)
            trial_host = trial["weekly_role_map"]["weeks"][ordinal]["session_roles"][host_index]
            trial_host["selected_exercise_assignments"] = [
                *(trial_host.get("selected_exercise_assignments") or []), assignment,
            ]
            # The canonical resolver prices the new member for this exact day
            # (countdown caps, pre-hard-contact pruning, readiness reductions).
            apply_effective_strength_prescriptions(
                weekly_role_map=trial["weekly_role_map"],
                candidate_pools=trial.get("candidate_pools") or {},
                athlete_model=_athlete(trial),
            )
            resolved = next(
                (item for item in trial_host.get("effective_strength_prescriptions") or []
                 if item.get("slot_id") == assignment.get("slot_id") and item.get("name") == assignment["name"]),
                None,
            )
            if resolved is None or not any(
                item.get("slot_id") == assignment.get("slot_id") for item in trial_host.get("selected_exercise_assignments") or []
            ):
                refusals.append("resolver_did_not_admit_candidate")
                continue
            if not _realised_load_not_increased(before_stress, realised_role_stress(trial_host)):
                refusals.append("host_realised_load_would_increase")
                continue
            trial_evidence = collect_goal_evidence(trial)
            _, missing_after = _coverage(entry, trial, trial_evidence)
            retained = {(e.get("d_day"), e.get("name"), tuple(e["intents"])) for e in trial_evidence}
            # Same no-regression guard the role repair uses: a floor exposure may
            # not pay for this goal by erasing another stimulus.
            if len(missing_after) >= len(missing_before) or any(
                (e.get("d_day"), e.get("name"), tuple(e["intents"])) not in retained
                for e in collect_goal_evidence(brief)
            ):
                refusals.append("candidate_adds_no_coverage")
                continue
            sets, _ = _sets_reps(str(resolved.get("effective_prescription") or ""))
            trial_host["priority_microdose"] = {
                "goal": entry["goal"],
                "name": assignment["name"],
                "slot_id": assignment.get("slot_id"),
                "prescription": resolved.get("effective_prescription"),
                "sets": int(resolved.get("effective_max_sets", sets) or sets),
                "intents": [entry["required_intent"]],
                "dose_authority": resolved.get("dose_authority"),
                "source": _MICRODOSE_SOURCE,
                "authority": VERSION,
            }
            brief["weekly_role_map"] = trial["weekly_role_map"]
            return {"week_index": week.get("week_index"), "d_day": host_day,
                    "role_key": host.get("role_key"), "goal": entry["goal"],
                    "exercise": assignment["name"],
                    "result": "microdose_attached", "reason_codes": ["weekly_priority_exposure_floor"]}

    # An earlier obligation in the same week may already hold the only session
    # that could have hosted this one - `_microdose_host_roles` excludes a role
    # that already carries a microdose, and stacking a second touch on it is not
    # this tier's call to make. Say that plainly instead of reporting it as "no
    # compatible session": the capacity existed, a higher-precedence priority
    # spent it, and this obligation waits for another legal host rather than
    # forcing more work onto the week.
    taken = [
        role.get("priority_microdose", {}).get("goal")
        for role in week.get("session_roles") or []
        if isinstance(role, dict) and isinstance(role.get("priority_microdose"), dict)
    ]
    if any(goal and goal != entry["goal"] for goal in taken):
        return {"week_index": week.get("week_index"), "goal": entry["goal"],
                "result": "floor_no_safe_capacity",
                "reason_codes": ["priority_capacity_spent"],
                "held_by": [goal for goal in taken if goal and goal != entry["goal"]]}
    if refusals:
        # Hosts existed, but no already-selected pool exercise survived the
        # injury/equipment/restriction-filtered pool, the composed cap, the dose
        # resolver and the host's measured load. Safety outranks coverage: the
        # gap is recorded, never filled with an invented exercise.
        return {"week_index": week.get("week_index"), "goal": entry["goal"],
                "result": "floor_no_safe_candidate",
                "reason_codes": sorted(set(refusals))}
    return {"week_index": week.get("week_index"), "result": "floor_no_safe_host",
            "reason_codes": ["no_compatible_existing_session"]}


def _coverage_regresses(before: dict, after: dict) -> bool:
    """One no-regression verdict for every bounded goal-repair trial.

    Compare the actual missing windows, not just their count: gaining one week
    cannot pay for losing a different week. Read each trial's evidence once.
    """
    before_evidence, after_evidence = collect_goal_evidence(before), collect_goal_evidence(after)
    for goal in classify_goal_preservation(_athlete(before), before.get("priority_focus")):
        previous = {tuple(sorted(w.items())) for w in _coverage(goal, before, before_evidence)[1]}
        current = {tuple(sorted(w.items())) for w in _coverage(goal, after, after_evidence)[1]}
        if current - previous:
            return True
    return False


def _recompose_existing_strength_for_goal(brief: dict, entry: dict) -> dict | None:
    """Try an existing dated strength appointment before inventing a second one.

    Selection remains owned by the exercise composer and dose resolver. Reuse
    already-filtered pools within the existing exercise cap, then accept the
    trial only when it meets a missing goal window without losing another goal,
    relocating sessions or violating the shared calendar.
    """
    if (entry.get("goal") not in {"speed", "power", "strength"}
            or (entry.get("state") != "build" and entry.get("goal") != "strength")):
        return None
    from .prescription_resolver import apply_effective_strength_prescriptions
    from .session_composition import (
        apply_realised_load_calendar_revalidation,
        compose_normal_strength_assignments,
        compose_normal_conditioning_assignments,
    )
    role_map = brief.get("weekly_role_map") or {}
    _, missing = _coverage(entry, brief, collect_goal_evidence(brief))
    if not missing:
        return None
    for ordinal, week in enumerate(role_map.get("weeks") or []):
        for index, host in enumerate(week.get("session_roles") or []):
            day = role_d_day(week, host)
            if (host.get("category") != "strength" or not isinstance(day, int) or day <= 13
                    or not any(w["min_d_day"] is None or w["min_d_day"] <= day <= w["max_d_day"] for w in missing)
                    or (host.get("governance") or {}).get("hard_suppression_reasons")):
                continue
            trial = deepcopy(brief)
            trial_map = trial["weekly_role_map"]
            trial_week = trial_map["weeks"][ordinal]
            trial_host = trial_week["session_roles"][index]
            original_members = [(m.get("slot_id"), m.get("name"))
                                for m in trial_host.get("selected_exercise_assignments") or []]
            compose_normal_strength_assignments(
                weekly_role_map=trial_map,
                candidate_pools=trial.get("candidate_pools") or {},
                only_roles={id(trial_host)}, allow_relocation=False,
            )
            next_members = [(m.get("slot_id"), m.get("name"))
                            for m in trial_host.get("selected_exercise_assignments") or []]
            if original_members == next_members:
                continue
            apply_effective_strength_prescriptions(
                weekly_role_map=trial_map, candidate_pools=trial.get("candidate_pools") or {},
                athlete_model=_athlete(trial),
            )
            # Re-evaluate realised cost, not just the pre-selection role label.
            apply_realised_load_calendar_revalidation(
                trial_map, only_roles={id(trial_host)},
                redose_callback=lambda rm: apply_effective_strength_prescriptions(
                    weekly_role_map=rm, candidate_pools=trial.get("candidate_pools") or {},
                    athlete_model=_athlete(trial)),
                recompose_strength_callback=lambda rm, only_roles: compose_normal_strength_assignments(
                    weekly_role_map=rm, candidate_pools=trial.get("candidate_pools") or {},
                    only_roles=only_roles, allow_relocation=False),
                recompose_conditioning_callback=lambda rm, only_roles: compose_normal_conditioning_assignments(
                    weekly_role_map=rm, candidate_pools=trial.get("candidate_pools") or {},
                    only_roles=only_roles),
            )
            if (not any(role is trial_host for role in trial_week.get("session_roles") or [])
                    or role_d_day(trial_week, trial_host) != day):
                continue
            profile = classify_role(trial_host)
            view = CalendarLegalityView(
                events=tuple(build_events(trial_map, exclude_role=trial_host)),
                scope=week_scope(trial_week, ordinal),
            )
            verdict = view.decision_for_profile(profile, day) if profile is not None else None
            if verdict is None or not verdict.allowed:
                continue
            trial_evidence = collect_goal_evidence(trial)
            _, still_missing = _coverage(entry, trial, trial_evidence)
            if len(still_missing) >= len(missing):
                continue
            if _coverage_regresses(brief, trial):
                continue
            brief["weekly_role_map"] = trial_map
            return {"week_index": week.get("week_index"), "d_day": day,
                    "role_key": host.get("role_key"), "goal": entry["goal"],
                    "result": "recomposed_existing_strength",
                    "reason_codes": ["use_existing_session_before_extra_appointment"],
                    "original_exercises": [name for _, name in original_members],
                    "selected_exercises": [name for _, name in next_members]}
    return None


def _restore_goal_roles(brief: dict, entry: dict, *, enforce_session_budget: bool = False) -> list[dict]:
    """Try retained planner candidates within their original week and budget.

    Intentional compression and finished tails are immutable. No new exercise,
    role family, dose, extra session capacity or contact rule is invented here.
    New maintenance retries enforce the requested appointment budget; the
    existing build-repair reporting policy remains unchanged.
    """
    from .late_camp_role_morph import _apply_late_camp_role_morph_once
    from .pre_hard_contact_strength import apply_pre_hard_contact_strength_exposure_cap
    from .physical_session_frequency import appointment_budget_exceeded, requested_training_frequency
    from .prescription_resolver import apply_effective_strength_prescriptions
    from .session_composition import (
        apply_realised_load_calendar_revalidation,
        compose_normal_conditioning_assignments,
        compose_normal_strength_assignments,
    )

    from .stage2_payload import _is_displaceable_occupancy_filler

    audit = []
    if "late_fight_session_sequence" in brief:
        return audit
    role_map = brief.get("weekly_role_map") or {}
    _, uncovered = _coverage(entry, brief, collect_goal_evidence(brief))
    if not uncovered:
        return audit
    recomposed = _recompose_existing_strength_for_goal(brief, entry)
    if recomposed is not None:
        # A long camp can require more than one development window. Repair the
        # next uncovered window too; do not stop after the first valid host.
        _, remaining = _coverage(entry, brief, collect_goal_evidence(brief))
        return [recomposed, *_restore_goal_roles(brief, entry, enforce_session_budget=enforce_session_budget)] if remaining else [recomposed]
    for ordinal, week in enumerate(role_map.get("weeks") or []):
        # A repair belongs to an uncovered cadence window. Re-composing weeks
        # that already met the goal adds no coverage and wastes bounded trials.
        calendar = [d.get("d_day") for d in week.get("calendar_days") or []]
        if not any(window["min_d_day"] is None or any(
            isinstance(day, int) and window["min_d_day"] <= day <= window["max_d_day"]
            for day in calendar) for window in uncovered):
            continue
        candidates = week.get("goal_repair_candidates") or []
        for candidate in candidates:
            identity = (candidate.get("role_key"), candidate.get("strength_session_index"))
            # A closed conditioning role can carry genuine ballistic power.
            # Judge its live effective evidence, not its module/category label.
            live_intent = any(e.get("week_index") == week.get("week_index")
                              and e.get("role_key") == candidate.get("role_key")
                              and entry["required_intent"] in e["intents"]
                              for e in collect_goal_evidence(brief))
            if not _role_matches_goal(candidate, entry["goal"]) and not live_intent:
                continue
            live = next((r for r in week.get("session_roles") or []
                         if (r.get("role_key"), r.get("strength_session_index")) == identity
                         and role_d_day(week, r) is not None), None)
            live_qualifies = live is not None and any(
                e.get("week_index") == week.get("week_index") and e.get("role_key") == live.get("role_key")
                and e.get("d_day") == role_d_day(week, live) and entry["required_intent"] in e["intents"]
                and (entry["state"] != "build" or e.get("development_quality"))
                for e in collect_goal_evidence(brief))
            if live_qualifies and any(window["min_d_day"] is None or
                    window["min_d_day"] <= role_d_day(week, live) <= window["max_d_day"] for window in uncovered):
                continue
            suppressed = [r for r in week.get("suppressed_roles") or [] if r.get("role_key") == candidate.get("role_key")]
            # The D-14/D-13 ownership handoff is not reclaimable capacity. A role
            # it removed failed normal placement outright and its window now
            # belongs to the finished late-fight tail, so restoring it here would
            # re-create the stale session the handoff just resolved. This is a
            # narrow, explicitly named authority — not readiness compression, and
            # it deliberately does not widen any goal-deferral rule.
            if any(r.get("reason_code") == HANDOFF_UNPLACED_REASON_CODE for r in suppressed):
                audit.append({"week_index": week.get("week_index"), "role_key": candidate.get("role_key"),
                              "result": "authority_preserved",
                              "reason_codes": [HANDOFF_UNPLACED_REASON_CODE]})
                continue
            compression, compression_codes = effective_goal_repair_compression_state(week, suppressed, athlete_model=_athlete(brief))
            if compression_codes or compression.get("active") or any((r.get("governance") or {}).get("hard_suppression_reasons") for r in suppressed):
                audit.append({"week_index": week.get("week_index"), "role_key": candidate.get("role_key"),
                              "result": "authority_preserved", "reason_codes": sorted(set(compression_codes))})
                continue
            # Original category allocation still owns standalone role capacity.
            # Overall appointment discrepancies are reported after repair.
            roles = week.get("session_roles") or []
            unresolved = next((r for r in roles
                if (r.get("role_key"), r.get("strength_session_index")) == identity
                and role_d_day(week, r) is None), None)
            # A retained dayless role has already passed composition and dose
            # resolution. Use that actual low-cost profile, not its pre-composed
            # reservoir copy's exclusive-slot promise, when testing a double day.
            repair_source = live or unresolved or candidate
            if (repair_source.get("governance") or {}).get("hard_suppression_reasons"):
                continue
            original_cap = sum(r.get("category") == candidate.get("category") for r in candidates)
            # A recovery slot converted by the allocator into an aerobic flush
            # keeps its original budget ownership. Charging it again as a new
            # conditioning slot stranded an already-approved alactic double day.
            current = sum(r.get("original_category", r.get("category")) == candidate.get("category")
                          and role_d_day(week, r) is not None for r in roles)
            if live is not None:
                current -= 1  # Relocation consumes its existing category slot.
            if current >= original_cap:
                audit.append({"week_index": week.get("week_index"), "result": "session_cap", "reason_codes": ["calendar_capacity"]})
                continue
            declared = {str(d).lower() for d in week.get("declared_training_days") or []}
            protected_days = {str(d.get("day") or "").lower() for d in week.get("intentionally_unused_days") or []}
            # Real unmet goals take precedence over placeholders inserted only
            # to fill an empty day. Reuse the late-primer owner's displacement
            # definition; prescribed recovery and medical holds are not fillers.
            filler_days = {role_d_day(week, r) for r in roles if _is_displaceable_occupancy_filler(r)}
            days = sorted(week.get("calendar_days") or [], key=lambda day: day.get("d_day") not in filler_days)
            for day in days:
                d_day = day.get("d_day")
                if (not isinstance(d_day, int) or d_day <= 13 or str(day.get("weekday")).lower() not in declared
                        or str(day.get("weekday")).lower() in protected_days):
                    continue
                if not any(window["min_d_day"] is None or window["min_d_day"] <= d_day <= window["max_d_day"] for window in uncovered):
                    continue
                # Judge the resolved work on the existing calendar, including
                # compatible technical doubles. Report appointment totals separately.
                displaced = [i for i, r in enumerate(roles)
                             if role_d_day(week, r) == d_day and _is_displaceable_occupancy_filler(r)]
                placement_map = {**role_map, "weeks": [
                    {**w, "session_roles": [r for i, r in enumerate(w.get("session_roles") or [])
                                            if r is not live and (w is not week or i not in displaced)]}
                    for w in role_map.get("weeks") or []]}
                view = CalendarLegalityView(events=tuple(build_events(placement_map)), scope=week_scope(week, ordinal))
                decision = view.decision_for_role(repair_source, d_day)
                swap = None
                if live is not None and decision is not None and not decision.allowed:
                    # A cadence gap may need two already-approved S&C sessions
                    # exchanged, rather than another session added. Contact locks,
                    # finished tails and hard suppression remain immutable.
                    occupants = [r for r in roles if r is not live and role_d_day(week, r) == d_day
                                 and r.get("category") in {"strength", "conditioning"}
                                 and not r.get("late_fight_tail_owned")
                                 and not (r.get("governance") or {}).get("hard_suppression_reasons")]
                    if len(occupants) == 1:
                        swap = occupants[0]
                        swap_view = CalendarLegalityView(events=tuple(event for event in build_events(placement_map)
                            if event.position != -d_day or event.profile != classify_role(swap)), scope=week_scope(week, ordinal))
                        decision = swap_view.decision_for_role(repair_source, d_day)
                if decision is None or not decision.allowed:
                    audit.append({"week_index": week.get("week_index"), "d_day": d_day,
                                  "result": "calendar_forbidden", "reason_codes": [decision.reason_code] if decision else []})
                    continue
                trial = deepcopy(brief)
                trial_week = trial["weekly_role_map"]["weeks"][ordinal]
                # Re-place an unresolved approved role instead of duplicating it.
                trial_week["session_roles"] = [r for i, r in enumerate(trial_week["session_roles"])
                    if i not in displaced and (r.get("role_key"), r.get("strength_session_index")) != identity]
                if swap is not None:
                    swap_identity = (swap.get("role_key"), swap.get("strength_session_index"))
                    trial_swap = next(r for r in trial_week["session_roles"]
                        if (r.get("role_key"), r.get("strength_session_index")) == swap_identity)
                    source_day = role_d_day(week, live)
                    source_weekday = next(d["weekday"] for d in week["calendar_days"] if d.get("d_day") == source_day)
                    trial_swap.update(scheduled_day_hint=str(source_weekday).title(), scheduled_countdown_label=f"D-{source_day}",
                                      scheduled_d_day=source_day, countdown_offset=source_day, countdown_label=f"D-{source_day}")
                restored = deepcopy(repair_source)
                restored.update(scheduled_day_hint=str(day["weekday"]).title(), scheduled_countdown_label=f"D-{d_day}",
                                scheduled_d_day=d_day, countdown_offset=d_day, countdown_label=f"D-{d_day}",
                                session_index=max((r.get("session_index", 0) for r in roles), default=0) + 1)
                restored["goal_preservation_repair"] = {"goal": entry["goal"], "authority": VERSION,
                    "mode": "maintenance" if entry["state"] == "maintain" else "build"}
                trial_week["session_roles"].append(restored)
                changed_roles = {id(restored)} | ({id(trial_swap)} if swap is not None else set())
                compose_normal_strength_assignments(
                    weekly_role_map=trial["weekly_role_map"],
                    candidate_pools=trial.get("candidate_pools") or {},
                    only_roles=changed_roles, allow_relocation=False,
                )
                # The finished calendar already passed the placement governor.
                # Reprice the trial without reopening unrelated graded searches;
                # the realised-load governor below judges its changed sessions.
                _apply_late_camp_role_morph_once(trial["weekly_role_map"])
                apply_pre_hard_contact_strength_exposure_cap(trial["weekly_role_map"])
                compose_normal_conditioning_assignments(
                    weekly_role_map=trial["weekly_role_map"],
                    candidate_pools=trial.get("candidate_pools") or {},
                    only_roles=changed_roles,
                )
                apply_effective_strength_prescriptions(weekly_role_map=trial["weekly_role_map"],
                                                      candidate_pools=trial.get("candidate_pools") or {}, athlete_model=_athlete(trial))
                # Price the proposed work before checking whether a redundant
                # appointment can fund it. The new session may meaningfully
                # deliver another quality as well (e.g. strength AND power).
                frequency = requested_training_frequency(_athlete(trial))
                traded_role = None
                over_target = frequency and appointment_budget_exceeded({"weeks": [trial_week]}, frequency)
                if over_target:
                    conditioning = [r for r in trial_week["session_roles"]
                        if r.get("category") == "conditioning" and not r.get("late_fight_tail_owned")
                        and not r.get("coach_owned") and not r.get("declared_combat_ownership")
                        and not (r.get("governance") or {}).get("hard_suppression_reasons")]
                    if len(conditioning) > 1:
                        for funding_role in reversed(conditioning):
                            funding_index = trial_week["session_roles"].index(funding_role)
                            trial_week["session_roles"].remove(funding_role)
                            if (not appointment_budget_exceeded({"weeks": [trial_week]}, frequency)
                                    and not _coverage_regresses(brief, trial)):
                                traded_role = funding_role
                                break
                            trial_week["session_roles"].insert(funding_index, funding_role)
                    # Repair may reuse capacity, never manufacture another
                    # appointment to satisfy a goal.
                    if enforce_session_budget and appointment_budget_exceeded({"weeks": [trial_week]}, frequency):
                        audit.append({"week_index": week.get("week_index"), "d_day": d_day,
                                      "result": "session_cap", "reason_codes": ["calendar_capacity"]})
                        continue
                # A trial must face the same realised-load governor as the main
                # pipeline, after membership and effective doses are knowable.
                apply_realised_load_calendar_revalidation(
                    trial["weekly_role_map"],
                    only_roles={id(role)
                        for nearby_week in trial["weekly_role_map"]["weeks"]
                        for role in nearby_week.get("session_roles") or []
                        if role_d_day(nearby_week, role) is not None
                        and abs(role_d_day(nearby_week, role) - d_day) <= 7},
                    redose_callback=lambda role_map: apply_effective_strength_prescriptions(
                        weekly_role_map=role_map, candidate_pools=trial.get("candidate_pools") or {},
                        athlete_model=_athlete(trial)),
                    recompose_conditioning_callback=lambda role_map, only_roles: compose_normal_conditioning_assignments(
                        weekly_role_map=role_map, candidate_pools=trial.get("candidate_pools") or {}, only_roles=only_roles),
                    recompose_strength_callback=lambda role_map, only_roles: compose_normal_strength_assignments(
                        weekly_role_map=role_map, candidate_pools=trial.get("candidate_pools") or {},
                        only_roles=only_roles, allow_relocation=False),
                )
                if not any(role is restored for role in trial_week["session_roles"]):
                    continue
                final_day = role_d_day(trial_week, restored)
                final_profile = classify_role(restored)
                final_view = CalendarLegalityView(
                    events=tuple(build_events(trial["weekly_role_map"], exclude_role=restored)),
                    scope=week_scope(trial_week, ordinal),
                )
                final_decision = final_view.decision_for_profile(final_profile, final_day) if final_profile and final_day else None
                if final_decision is None or not final_decision.allowed:
                    # Preserve the shared calendar verdict; a soft preference
                    # is not a second categorical prohibition.
                    audit.append({"week_index": week.get("week_index"), "d_day": final_day,
                                  "result": "calendar_forbidden",
                                  "realised_stress": restored.get("calendar_stress"),
                                  "effective_doses": [p.get("effective_prescription") for p in restored.get("effective_strength_prescriptions") or []],
                                  "reason_codes": [final_decision.reason_code] if final_decision else []})
                    continue
                _, missing_before = _coverage(entry, brief, collect_goal_evidence(brief))
                _, missing_after = _coverage(entry, trial, collect_goal_evidence(trial))
                # Protect actual selected-goal coverage below, rather than every
                # incidental exercise identity. Day-specific recomposition and
                # maintenance reductions can legitimately change those members.
                if len(missing_after) >= len(missing_before):
                    audit.append({"week_index": week.get("week_index"), "d_day": final_day,
                                  "result": "trial_rejected", "reason_codes": ["no_coverage_gain"]})
                    continue
                if _coverage_regresses(brief, trial):
                    audit.append({"week_index": week.get("week_index"), "d_day": final_day,
                                  "result": "trial_rejected", "reason_codes": ["selected_goal_coverage_regressed"]})
                    continue
                trial_week["suppressed_roles"] = [r for r in trial_week.get("suppressed_roles") or [] if r.get("role_key") != candidate.get("role_key")]
                if traded_role is not None:
                    trial_week["suppressed_roles"].append({**traded_role,
                        "reason_code": "selected_goal_capacity_trade", "replaced_by_goal": entry["goal"]})
                brief["weekly_role_map"] = trial["weekly_role_map"]
                # The goal_repair reservoir holds pre-finalization role copies, so
                # a restored role reaches the finalizer without the athlete-facing
                # label surviving roles get from stamp_weekly_role_map_labels. Give
                # it the same final decoration now (after the morph has run so any
                # morph-owned label wins), rather than appending a stale copy.
                stamp_role_label(restored)
                audit.append({"week_index": week.get("week_index"), "d_day": d_day,
                              "result": "relocated" if live is not None else "restored", "reason_codes": [],
                              "displaced_fillers": [roles[i].get("role_key") for i in displaced],
                              **({"appointment_tradeoff": "compatible_work_above_requested_target"} if over_target and traded_role is None else {}),
                              **({"capacity_trade": traded_role.get("role_key")} if traded_role else {})})
                return audit + _restore_goal_roles(brief, entry, enforce_session_budget=enforce_session_budget) if missing_after else audit
        # Reaching here means no candidate could be restored into this week -
        # for ANY reason: no candidate matched the goal, the week held its
        # authority (compression, hard suppression, contact rules), the session
        # or category budget was full, the calendar forbade every day, or the
        # trial regressed another stimulus. A restore returns above, so this is
        # the single "the full repair did not happen" exit.
        #
        # Whichever reason it was, the weekly requirement may still be missing
        # and the week may still hold a session that can safely carry the
        # smallest exposure. Gating this on one specific failure reason made the
        # fallback unreachable for every other one, including a week that has no
        # repair candidates at all and never enters the loop above.
        floor_audit = _attach_goal_microdose(brief, ordinal, entry)
        if floor_audit is not None:
            audit.append(floor_audit)
    return audit


def _repair_refusal_reasons(week: dict, goal: str, window: dict, *, athlete_model: dict | None = None) -> list[dict]:
    """Live compression codes that stop this week's goal candidates being restored.

    Mirrors the refusal in ``_restore_goal_roles`` exactly (same candidates, same
    ``effective_goal_repair_compression_state``), so repair and deferral cannot
    disagree about whether a compressed week is higher authority.
    """
    reasons: list[dict] = []
    existing = {(r.get("role_key"), r.get("strength_session_index")) for r in week.get("session_roles") or []
                if role_d_day(week, r) is not None}
    for candidate in week.get("goal_repair_candidates") or []:
        identity = (candidate.get("role_key"), candidate.get("strength_session_index"))
        if identity in existing or not _role_matches_goal(candidate, goal):
            continue
        suppressed = [r for r in week.get("suppressed_roles") or [] if r.get("role_key") == candidate.get("role_key")]
        _, codes = effective_goal_repair_compression_state(week, suppressed, athlete_model=athlete_model)
        for code in sorted(set(codes)):
            reason = {"reason_code": _COMPRESSION_REASONS.get(code, "planner_compression"),
                      "authority": "planner_compression", "source_reason_code": code,
                      "week_index": week.get("week_index"), "role_key": candidate.get("role_key"),
                      "coverage_window": window}
            if reason not in reasons:
                reasons.append(reason)
    return reasons


def _trunk_capacity_reasons(weeks: list[dict], window: dict) -> list[dict]:
    """Live composition refusals that kept trunk work out of this window.

    Each normal strength session in the window must carry composition's own causal
    finding (``trunk_support.skip_reason == "readiness_capacity"``): trunk work
    would have fitted at pressure 0. One readiness-limited session does not
    excuse another that simply held no trunk work. Conditioning's explicit
    contact-adjacency refusal and finished-tail ownership remain their own
    authorities; this reader never changes their membership.
    """
    sessions = []
    protected = []
    for week in weeks:
        for role in week.get("session_roles") or []:
            day = role_d_day(week, role)
            if role.get("category") not in {"strength", "conditioning"} or not isinstance(day, int):
                continue
            if window["min_d_day"] is not None and not window["min_d_day"] <= day <= window["max_d_day"]:
                continue
            receipt = {"week_index": week.get("week_index"), "role_key": role.get("role_key"),
                       "d_day": day, "coverage_window": window}
            if role.get("late_fight_tail_owned") and day <= _LATE_TAIL_MAX_D_DAY:
                protected.append({**receipt, "reason_code": "fight_proximity", "authority": "late_fight_tail_ownership"})
                continue
            if role.get("category") == "conditioning":
                policy = role.get("conditioning_composition_policy") or {}
                if (policy.get("embedded_trunk_support_skip_reason") == "hard_sparring_adjacent"
                        and policy.get("hard_sparring_adjacent") is True):
                    sessions.append((week, day, role, {"skip_reason": "readiness_capacity",
                        "pressure_sources": ["hard_sparring_priority"],
                        "authority": "session_composition.embedded_trunk_support"}))
                    continue
                return []  # An unexplained conditioning host cannot be hidden by another receipt.
            outcome = (role.get("strength_composition_policy") or {}).get("trunk_support") or {}
            sessions.append((week, day, role, outcome))
    if not sessions or any(outcome.get("skip_reason") != "readiness_capacity" for *_, outcome in sessions):
        return []
    return protected + [{"reason_code": code, "authority": outcome.get("authority", "session_composition.trunk_support"),
             "week_index": week.get("week_index"), "role_key": role.get("role_key"), "d_day": day,
             "coverage_window": window}
            for week, day, role, outcome in sessions for code in outcome.get("pressure_sources") or []]


def _deferral_constraints(entry: dict, brief: dict, missing: list[dict]) -> list[dict]:
    """Every uncovered window needs a live, causal higher-authority reason."""
    if not missing:
        return []
    days = _athlete(brief).get("days_until_fight")
    if _hard_sparring_strength_limited(_athlete(brief), entry["goal"]):
        return [{"reason_code": _HARD_SPARRING_STRENGTH_LIMIT, "authority": "hard_sparring_strength_policy",
                 "days_until_fight": days, "hard_sparring_days": list(_athlete(brief).get("hard_sparring_days") or [])}]
    if isinstance(days, int) and (days <= 1 or (entry["goal"] == "strength" and days <= 7)):
        return [{"reason_code": "fight_proximity", "authority": "late_fight_strength_dose_cap" if entry["goal"] == "strength" else "fight_day_protocol", "days_until_fight": days}]
    reasons = []
    for window in missing:
        window_reasons = []
        if window.get("unplannable"):
            # The calendar opens after this window closes: the camp itself
            # starts inside fight proximity, so no session can exist there.
            reasons.append({"reason_code": "fight_proximity", "authority": "plan_calendar",
                            "days_until_fight": days, "coverage_window": window})
            continue
        for week in _effective_map(brief).get("weeks") or []:
            calendar = [d.get("d_day") for d in week.get("calendar_days") or [] if isinstance(d.get("d_day"), int)]
            if window["min_d_day"] is not None and not any(window["min_d_day"] <= d <= window["max_d_day"] for d in calendar):
                continue
            for row in week.get("suppressed_roles") or []:
                if not _role_matches_goal(row, entry["goal"]):
                    continue
                _, live_codes = effective_goal_repair_compression_state(week, [row], athlete_model=_athlete(brief))
                for code in live_codes:
                    if code in _COMPRESSION_REASONS:
                        window_reasons.append({"reason_code": _COMPRESSION_REASONS[code], "authority": "planner_compression",
                            "source_reason_code": code, "week_index": week.get("week_index"), "role_key": row.get("role_key"),
                            "coverage_window": window})
                if row.get("calendar_integrity") and row.get("reason_code"):
                    window_reasons.append({"reason_code": "calendar_capacity", "authority": "final_calendar_integrity",
                        "source_reason_code": row["reason_code"], "week_index": week.get("week_index"), "role_key": row.get("role_key"),
                        "coverage_window": window})
            # The same compression authority _restore_goal_roles honours when it
            # refuses to reopen this week's retained candidate. Without it, a
            # compressed week (e.g. crowded-week `low_session_budget_high_combat_load`)
            # blocked the repair yet left the gap unexplained, so the goal stayed
            # an unsatisfiable `build` blocker instead of a cited deferral.
            window_reasons.extend(_repair_refusal_reasons(week, entry["goal"], window, athlete_model=_athlete(brief)))
            if entry["goal"] == "strength":
                readiness = athlete_dose_state(_athlete(brief))
                for role in week.get("session_roles") or []:
                    cap = role.get("strength_dose_cap") or {}
                    if _number(cap.get("max_sets")) < 2:
                        continue
                    # A reduce-only readiness override can turn a legitimate
                    # two-set retention exposure into one set. That is causal
                    # evidence; merely having an injury flag is not.
                    if any(p.get("effective_loaded") and p.get("effective_max_sets") == 1
                           for p in role.get("effective_strength_prescriptions") or []):
                        for flag, code in (("high_fatigue", "high_fatigue"), ("aggressive_weight_cut", "weight_cut_pressure"), ("injury_restricted", "injury_constraint")):
                            if readiness.get(flag):
                                window_reasons.append({"reason_code": code, "authority": "effective_strength_prescriptions",
                                    "week_index": week.get("week_index"), "role_key": role.get("role_key"), "coverage_window": window})
        if entry["goal"] == "trunk_strength":
            window_reasons.extend(_trunk_capacity_reasons(_effective_map(brief).get("weeks") or [], window))
        if (not window_reasons and entry["state"] != "build" and window["max_d_day"] is not None
                and window["max_d_day"] <= _LATE_TAIL_MAX_D_DAY):
            # Maintenance owed wholly inside D-13..D-1, with no more specific
            # cause: those days belong to the late-fight tail, whose
            # freshness-first selector owns membership and which goal repair
            # may not reopen. That ownership is why nothing was placed.
            window_reasons.append({"reason_code": "fight_proximity", "authority": "late_fight_tail_ownership",
                                   "days_until_fight": days, "coverage_window": window})
        if not window_reasons:
            return []  # One explained gap must not conceal another unexplained gap.
        # The same suppressed role or compressed week can be read from more
        # than one path; one cause is one constraint.
        unique: dict[str, dict] = {}
        for reason in window_reasons:
            unique.setdefault(_constraint_key(reason), reason)
        reasons.extend(unique.values())
    return reasons


def _constraint_key(reason: dict):
    return repr(sorted(reason.items()))



def refresh_goal_evidence_after_composition(brief: dict) -> dict:
    """Refresh recorded witnesses after a dose-preserving, post-repair consolidation.

    This reads actual delivered exercise membership. It never repairs goals,
    changes selected exercises, redoses work or changes the original goal state.
    """
    entries = brief.get("goal_preservation") or []
    if not entries:
        return brief
    evidence = collect_goal_evidence(brief)
    for entry in entries:
        effective_entry = (
            {**entry, "state": entry.get("initial_state", entry.get("state"))}
            if entry.get("state") == "defer" else entry
        )
        matching, missing = _coverage(effective_entry, brief, evidence)
        entry.update(evidence=matching, missing_coverage=missing,
                     coverage_requirements=_requirements(effective_entry, brief),
                     satisfied=not missing and entry.get("state") != "defer")
    brief["compressed_priorities"]["goal_preservation"] = entries
    _athlete(brief)["compressed_priorities"]["goal_preservation"] = entries
    return brief


def reconcile_goal_preservation(brief: dict) -> dict:
    # Repair runs after the Stage 1 builder's context has closed. Its existing
    # composition/phase owners still need the same athlete priorities and state.
    token = planner_athlete_model_context.set(_athlete(brief) if isinstance(brief, dict) else None)
    try:
        return _reconcile_goal_preservation(brief)
    finally:
        planner_athlete_model_context.reset(token)


def _reconcile_goal_preservation(brief: dict) -> dict:
    """Repair, then decide; unresolved obligations remain blocking obligations."""
    from .prescription_resolver import apply_effective_conditioning_prescriptions
    apply_effective_conditioning_prescriptions(
        weekly_role_map=brief.get("weekly_role_map") or {}, athlete_model=_athlete(brief),
    )
    entries = classify_goal_preservation(_athlete(brief), brief.get("priority_focus"))
    for entry in entries:
        initial_state = entry["state"]
        matching, missing = _coverage(entry, brief, collect_goal_evidence(brief))
        audit = _restore_goal_roles(brief, entry) if missing else []
        if audit:
            apply_effective_conditioning_prescriptions(
                weekly_role_map=brief.get("weekly_role_map") or {}, athlete_model=_athlete(brief),
            )
        matching, missing = _coverage(entry, brief, collect_goal_evidence(brief))
        constraints = _deferral_constraints(entry, brief, missing) if missing else []
        if missing and constraints:
            # Prefer retaining a maintenance promise over deferring a primary.
            maintained = {**entry, "state": "maintain"}
            maintenance_evidence, maintenance_missing = _coverage(maintained, brief, collect_goal_evidence(brief))
            if entry["state"] == "build" and maintenance_missing:
                # A legal maintenance exposure may survive where development
                # cannot. Try the same closed-candidate/calendar owners before
                # accepting a complete loss of the selected adaptation.
                audit.extend(_restore_goal_roles(brief, maintained, enforce_session_budget=True))
                maintenance_evidence, maintenance_missing = _coverage(maintained, brief, collect_goal_evidence(brief))
                matching, missing = _coverage(entry, brief, collect_goal_evidence(brief))
                constraints = _deferral_constraints(entry, brief, missing) if missing else []
            if missing and constraints:
                if entry["state"] == "build" and not maintenance_missing:
                    entry["state"] = "maintain"
                    matching, missing = maintenance_evidence, []
                else:
                    entry["state"] = "defer"
            entry["reason_codes"] = list(dict.fromkeys([*entry["reason_codes"], *(c["reason_code"] for c in constraints)]))
        entry.update(initial_state=initial_state, evidence=matching, constraints=constraints, repair_attempts=audit,
                     coverage_requirements=_requirements({**entry, "state": initial_state} if entry["state"] == "defer" else entry, brief), missing_coverage=missing,
                     satisfied=not missing and entry["state"] != "defer")
    # Later goal repairs may legally relocate an earlier goal's witnesses.
    # Persist evidence from the final calendar, never its pre-repair day cache.
    final_evidence = collect_goal_evidence(brief)
    for entry in entries:
        coverage_entry = {**entry, "state": entry["initial_state"]} if entry["state"] == "defer" else entry
        matching, missing = _coverage(coverage_entry, brief, final_evidence)
        entry.update(evidence=matching, missing_coverage=missing,
                     coverage_requirements=_requirements(coverage_entry, brief),
                     satisfied=not missing and entry["state"] != "defer")
    brief["goal_preservation_version"] = VERSION
    brief["goal_preservation"] = entries
    # A weak area serviced elsewhere must still be auditable here: without this
    # record, "delegated to a canonical subsystem" and "silently dropped" look
    # identical from the outside.
    brief["primary_weakness_disposition"] = primary_weakness_disposition(
        _athlete(brief), brief.get("priority_focus")
    )
    compressed = deepcopy(brief.get("compressed_priorities") or _athlete(brief).get("compressed_priorities") or {})
    compressed["goal_preservation"] = entries
    brief["compressed_priorities"] = compressed
    _athlete(brief)["compressed_priorities"] = compressed
    # Correct legacy per-role success claims AFTER effective dose resolution.
    evidence = collect_goal_evidence(brief)
    summary = {"checked": 0, "satisfied": 0, "unsatisfied": 0, "unsatisfied_roles": []}
    for week in _effective_map(brief).get("weeks") or []:
        for role in week.get("session_roles") or []:
            validation = role.get("intent_validation") or {}
            if validation.get("intent") == "meaningful_strength":
                survives = any(e.get("week_index") == week.get("week_index") and e.get("d_day") == role_d_day(week, role)
                               and e.get("role_key") == role.get("role_key") and "meaningful_strength" in e["intents"] for e in evidence)
                role["intent_validation"] = {**validation, "satisfied": survives, "authority": "post_prescription_goal_reconciliation"}
                if not survives:
                    role["intent_validation"]["reason_code"] = "effective_stimulus_lost"
                else:
                    role["intent_validation"].pop("reason_code", None)
                    role["intent_validation"].pop("reason", None)
            if role.get("intent_validation"):
                summary["checked"] += 1
                if role["intent_validation"].get("satisfied"):
                    summary["satisfied"] += 1
                else:
                    summary["unsatisfied"] += 1
                    summary["unsatisfied_roles"].append({"role_key": role.get("role_key"),
                        "intent": role["intent_validation"].get("intent"), "scheduled_d_day": role_d_day(week, role),
                        "reason_code": role["intent_validation"].get("reason_code")})
    if brief.get("weekly_role_map"):
        brief["weekly_role_map"]["post_morph_intent_validation"] = summary
    return brief


def validate_goal_preservation(brief: dict) -> list[dict]:
    """Fail closed on missing, forged or stale states. Never repair in validation."""
    goals = selected_goals(_athlete(brief), brief.get("priority_focus"))
    entries = brief.get("goal_preservation")
    # Legacy stored briefs stay readable; any dated planner with selected goals
    # is required to carry a contract when submitted for publication.
    if not goals:
        return []
    if brief.get("payload_variant") == "open_ongoing_stage2_payload" and not brief.get("goal_preservation_version"):
        spec = brief.get("open_plan_spec") or {}
        coverage = spec.get("goal_coverage") or []
        errors = []
        for goal, priority in goals:
            rows = [row for row in coverage if row.get("goal") == goal]
            row = rows[0] if len(rows) == 1 else {}
            # The canonical contract is evaluated before the weekday projection,
            # while causal restrictions and effective doses are still available.
            # A valid deferral intentionally has no satisfied training exposure.
            valid = (len(rows) == 1 and row.get("priority") == priority
                     and row.get("contract_errors") == [])
            if row.get("state") == "defer":
                valid = valid and bool(row.get("constraints"))
            else:
                valid = (valid and row.get("state") in {"build", "maintain"}
                         and bool(row.get("satisfied")) and bool(row.get("evidence")))
            if valid and row.get("state") != "defer":
                sessions = [(week.get("week"), session)
                            for week in (spec.get("weekly_session_plan") or {}).get("weeks") or []
                            for session in week.get("sessions") or []]
                valid = all(any(
                    evidence.get("week") == week and evidence.get("weekday") == session.get("weekday")
                    and any(exercise.get("name") == evidence.get("name") for exercise in session.get("exercises") or [])
                    for week, session in sessions) for evidence in row["evidence"])
            if not valid:
                errors.append({"code": "goal_preservation_failed", "goal": goal,
                               "message": "Open plan lacks justified deterministic goal coverage.",
                               "severity": "blocker", "confidence": "high"})
        return errors
    # A resolved dated payload that carries goal-preservation entries must also
    # carry the current contract version before publication validation. Without
    # this, a brief with valid-looking states/evidence but a missing or stale
    # version would slip stale contract semantics through. A brief with no entries
    # at all is not "resolved": it falls through to the per-goal checks below,
    # which report the missing contract for each selected goal.
    if (brief.get("payload_variant") != "open_ongoing_stage2_payload"
            and isinstance(entries, list) and entries
            and brief.get("goal_preservation_version") != VERSION):
        return [{
            "code": "goal_preservation_contract_stale",
            "requirement": "goal_preservation_version",
            "message": "Resolved dated payload must carry the current goal-preservation contract version.",
            "expected_version": VERSION,
            "actual_version": brief.get("goal_preservation_version"),
            "severity": "blocker",
            "confidence": "high",
        }]
    errors = []
    evidence = collect_goal_evidence(brief)
    initial = {e["goal"]: e for e in classify_goal_preservation(_athlete(brief), brief.get("priority_focus"))}
    for goal, priority in goals:
        rows = [e for e in entries or [] if isinstance(e, dict) and e.get("goal") == goal] if isinstance(entries, list) else []
        message = ""
        if len(rows) != 1:
            message = "Selected goal must have exactly one deterministic final state."
        else:
            entry = rows[0]
            if entry.get("state") not in {"build", "maintain", "defer"} or entry.get("required_intent") != INTENTS.get(goal, f"selected_goal:{goal}") or entry.get("priority") != priority:
                message = "Goal state or semantic requirement is invalid."
            elif entry.get("state") == "defer":
                obligation = {**entry, "state": "maintain"}
                _, missing = _coverage(obligation, brief, evidence)
                _, original_missing = _coverage(initial[goal], brief, evidence)
                justified = _deferral_constraints(initial[goal], brief, original_missing)
                if not missing or not justified or entry.get("constraints") != justified or not all(c["reason_code"] in (entry.get("reason_codes") or []) for c in justified):
                    message = "Deferred goal lacks a current, causal higher-authority reason."
            else:
                current, missing = _coverage(entry, brief, evidence)
                if missing or not current:
                    message = "Selected goal has no qualifying effective coverage; deterministic repair or justified deferral is required."
                elif entry.get("evidence") != current:
                    message = "Goal evidence is stale relative to the final schedule/prescription."
                elif entry["state"] == "maintain" and initial[goal]["state"] == "build":
                    _, build_missing = _coverage(initial[goal], brief, evidence)
                    constraints = _deferral_constraints(initial[goal], brief, build_missing)
                    if not constraints or entry.get("constraints") != constraints:
                        message = "Primary goal was downgraded without a higher-authority constraint."
        if message:
            errors.append({"code": "goal_preservation_failed", "goal": goal, "requirement": goal,
                           "message": message, "severity": "blocker", "confidence": "high"})
    return errors
