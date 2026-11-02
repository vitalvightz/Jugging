"""Compact Stage 2 finalizer packet.

This module converts the full internal Stage 2 payload into a small LLM-facing
packet. The full payload remains useful for debugging and tests, but the LLM
should not receive internal candidate pools, phase toolbox menus, or unused
rehab options.

Purpose:
- reduce LLM prompt bloat
- prevent GPP/SPP/TAPER toolbox leakage
- prevent rehab/prehab leakage when no active injury exists
- keep finalizer focused on selected sessions and render rules
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .fight_day_override import FIGHT_DAY_PROTOCOL_TEXT
from .stage2_render_guards import _all_active_injuries_surface_only, _render_guard_flags
from .combat_render_authority import (
    CANONICAL_HARD_SPARRING_BAN_LABEL,
    CANONICAL_HARD_SPARRING_LABEL,
    CANONICAL_HARD_SPARRING_NOTE,
    CANONICAL_TECHNICAL_ONLY_NOTE,
)


_ATHLETE_KEYS = (
    "sport",
    "status",
    "record",
    "rounds_format",
    "camp_length_weeks",
    "days_until_fight",
    "fight_date",
    "next_fight_date",
    "fatigue",
    "stance",
    "age",
    "weight_cut_risk",
    "weight_cut_pct",
    "weight_cut_status",
    "cut_severity_score",
    "cut_severity_bucket",
    "technical_styles",
    "tactical_styles",
    "weaknesses",
    "key_goals",
    "equipment",
    "training_frequency",
    "training_days",
    "hard_sparring_days",
    "support_work_days",
    "technical_skill_days",
    "short_notice",
    "plan_creation_weekday",
    "readiness_flags",
    "has_active_injury",
    "injuries_raw_text",
    "parsed_injuries",
    "guided_injury",
    "injury_restrictions",
)


_FORBIDDEN_TOOLBOX_LABELS = [
    "GPP toolbox",
    "SPP toolbox",
    "TAPER toolbox",
    "key drills to keep in your toolbox",
    "available options",
    "phase reference menu",
    "SPP tools",
    "GPP tools",
    "TAPER tools",
]


_FORBIDDEN_REHAB_LABELS = [
    "Rehab",
    "Injury Rehab",
    "Brief Rehab",
    "Prepare / brief rehab",
    "Prehab",
    "Rehab / Mobility",
]


# Writing rules that STAGE2_FINALIZER_PROMPT / UNLXCK_FINAL_RENDER_CONTRACT already
# state in prose. They stay in ``rewrite_guidance`` for the deterministic payload
# and its consumers, but ``build_stage2_finalizer_packet`` strips them from the
# packet the model receives: shipping a second, slightly reworded copy of a rule
# inside JSON costs ~4.4k prompt chars and leaves the model reconciling two
# versions of the same instruction (e.g. prose "The app must not prescribe" vs the
# packet's "The app does not prescribe"). One authoritative copy reads better.
# ``test_finalizer_packet_writing_rules.py`` fails loudly if any entry here stops
# matching a live rule in ``rewrite_guidance["writing_rules"]``.
FINALIZER_PROMPT_COVERED_WRITING_RULES: frozenset[str] = frozenset({
    "Keep the final plan athlete-facing and clean.",
    "Do not start corrective lines with generic openers such as 'focus on', 'ensure', 'make sure', or 'it's important to'; start with the action.",
    "Use autonomy-supportive phrasing only within real guardrails; if choice is safe and useful, offer at most two practical options, and only when both options are safe and materially equivalent for the day's goal.",
    "Do not use generic motivation such as 'stay consistent', 'trust the process', 'push yourself', or 'you've got this'.",
    "Do not use empty safety lines such as 'listen to your body', 'be careful', or 'avoid overtraining' unless they are followed by a concrete rule, symptom trigger, or plan change.",
    "Keep every active week present and structurally complete, including late-camp weeks.",
    "For boxer weeks, keep the default rhythm of support strength, low-damage conditioning, recovery, primary strength, then the main phase-specific conditioning stressor unless a stronger planning rule forces a change.",
    "Do not echo Primary, Fallback, Drill, or option-menu labels across most session lines.",
    "Treat declared hard sparring days in weekly_role_map as immutable hard_sparring_day slots except when final_week_sparring_cap.active is true. In final taper weeks, final_week_sparring_cap overrides the coach-declared hard-day lock: render at most one effective hard sparring day, and do not present capped_declared_hard_sparring_days as sparring.",
    "Hard sparring days are the athlete's own combat locks (run in their gym, with or without a coach). The app does not prescribe or lead the sparring itself, and it must respect resolved safety, readiness, and calendar restrictions on every declared hard sparring day. Only for a resolved hard-as-planned day render the label '" + CANONICAL_HARD_SPARRING_LABEL + "' (or the equivalent sport-specific label such as 'MMA — hard sparring / controlled hard contact') followed by exactly one short note: '" + CANONICAL_HARD_SPARRING_NOTE + "' From D-14 normally, or D-17 with elevated risk, hard sparring is converted to technical work: render '" + CANONICAL_HARD_SPARRING_BAN_LABEL + "' (or sport-equivalent) — the same applies whenever the day carries reason code 'd14_hard_sparring_ban' or 'd17_hard_sparring_ban' — followed by exactly one short note: '" + CANONICAL_TECHNICAL_ONLY_NOTE + "' A technical-only day must never carry the hard-sparring note. A blocked/none contact status overrides all declarations and dates: no contact or sparring; surface medical evaluation/clearance guidance and do not restore contact. Do not output round counts, time-x-rounds formulas, intensity targets, dose, RPE, work:rest, or any sparring template wording (e.g. never '6-8 x 3-min rounds at set intensity', 'X rounds technical sparring', 'live rounds at moderate intensity'). Do not narrate intent, do not add a 'why today' line, do not list focus areas, do not suggest pad/bag/clinch volume — the athlete owns that contact work. Never schedule programmed S&C alongside effective hard or reduced contact. Technical-only days may carry compatible S&C already selected by the planner. Anything more than the label plus that one note is a violation of this rule.",
    "If the athlete has more available days than planned sessions, leave the spare days off or clearly optional rather than rendering another full session.",
    "If weekly_role_map marks intentional_compression.active, keep that smaller week on purpose and do not restore the suppressed standalone role.",
    "If weekly_role_map.intentional_compression.policy is boxing_crowded_week, keep hard sparring as the week owner, then one anchor, then at most one low-load support day.",
    "In boxing crowded weeks, do not turn anchor days or recovery/support days into multi-stressor sessions by adding glycolytic, transfer, or extra sharpness work.",
    "If a target-weight constraint is present, explicitly acknowledge that it changes recovery and training tolerance in the athlete-facing plan.",
    "Never state 'weight cut none active' or 'recovery tolerance is standard' when readiness flags or weight_cut_pct indicate an active cut.",
    "If the cut is high-pressure, include one short summary-level note plus one support-level note; do not bury it only in the athlete profile or nutrition numbers.",
    "If fatigue is high or fight-week pressure is active, reduce optionality and make the directive plain.",
    "If injury management is active, lead with constraints, substitutions, or stop rules instead of optional language.",
    "If a target-weight constraint is present, keep the language shorter, safety-first, and non-negotiable about recovery margin.",
})


def _compact_dict(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: source.get(key) for key in keys if key in source}


def _compact_athlete_model(athlete_model: dict[str, Any]) -> dict[str, Any]:
    return _compact_dict(athlete_model or {}, _ATHLETE_KEYS)


def _compact_restrictions(restrictions: Any) -> list[dict[str, Any]]:
    if not isinstance(restrictions, list):
        return []
    compact: list[dict[str, Any]] = []
    for entry in restrictions:
        if not isinstance(entry, dict):
            continue
        compact.append(
            {
                key: entry.get(key)
                for key in (
                    "restriction",
                    "region",
                    "side",
                    "strength",
                    "source_phrase",
                    "blocked_patterns",
                    "mechanical_equivalents",
                )
                if entry.get(key) not in (None, "", [])
            }
        )
    return compact


# Stage 1's own decision-hierarchy declaration. Every role carries the same
# constant block (only a handful of distinct values across a whole camp), no
# render rule or contract text names any of these fields, and the finalizer has
# no use for which internal driver won a Stage 1 argument. They are dropped from
# the packet for the same reason selection_rule / placement_rule are, below --
# the planning brief and the role map keep them for validation and audit. The
# governance fields the finalizer does act on (selected_drill_locked, main_job,
# support_cap, forbidden_secondary_stressors, suppression_rules, locked_day,
# late_fight_payload, ...) are preserved untouched.
_INTERNAL_GOVERNANCE_FIELDS = (
    "authority",
    "execution_only",
    "governed_by",
    "cannot_override",
    "resolved_authority",
)


def _compact_governance(governance: Any) -> Any:
    if not isinstance(governance, dict):
        return governance
    return {
        key: value
        for key, value in governance.items()
        if key not in _INTERNAL_GOVERNANCE_FIELDS
    }


# Fields the progression object republishes verbatim from the same week's entry in
# weekly_role_map. Measured on a real 8-week camp, both are byte-identical in
# EVERY week, so the finalizer received one calendar twice and had to decide which
# copy was authoritative. weekly_role_map is that authority, so the copies are
# dropped from the progression view here -- at the handoff boundary only. Stage 1
# keeps the whole object for planning, validators, diagnostics and persistence.
#
# Deliberately NOT listed: hard_sparring_plan and intentionally_unused_days. They
# match the role map in most weeks but not all (7/8 and 6/8 measured), so removing
# them would silently drop real per-week information.
_PROGRESSION_FIELDS_OWNED_BY_ROLE_MAP = ("calendar_days", "intentional_compression")


def _compact_week_progression(progression: Any) -> Any:
    """Drop the calendar fields weekly_role_map already owns."""
    if not isinstance(progression, dict):
        return progression
    weeks = progression.get("weeks")
    if not isinstance(weeks, list):
        return progression
    return {
        **progression,
        "weeks": [
            {
                key: value
                for key, value in week.items()
                if key not in _PROGRESSION_FIELDS_OWNED_BY_ROLE_MAP
            }
            if isinstance(week, dict)
            else week
            for week in weeks
        ],
    }


# Per-exercise bookkeeping that names where Stage 1 found a slot. No finalizer
# prompt, render contract or rule names any of them, and membership is already
# closed by selected_exercise_assignments, so they only add tokens between the
# model and the fields it must act on (name and effective_prescription). They stay
# on the rich Stage 1 object for validators, diagnostics and persistence.
_ASSIGNMENT_PROVENANCE_FIELDS = (
    "slot_group",
    "source_phase",
    "source_session_index",
    "dose_authority",
    # Authored mechanical vocabulary the deterministic planner uses to measure a
    # composed session's realised cross-day cost. No finalizer rule names it and
    # the model must not act on it, so it stays on the rich Stage 1 object.
    "mechanical_risk_tags",
    # Core Development Block grouping is planner/UI metadata. Stage 2 renders
    # block members as ordinary closed exercises and never sees the grouping.
    "component_group",
    "trunk_function",
    "exercise_cap_exempt",
)


def _compact_prescribed_items(
    items: Any, *, effective_by_slot: dict[str, str]
) -> Any:
    """Drop provenance, and the base dose the finalizer must not choose from.

    ``base_prescription`` is the raw exercise-bank dose, never the authorised
    one. It is dropped where it is character-identical to the authoritative
    ``effective_prescription`` for the same slot, and also wherever the item has
    an effective dose that the scheduled-day strength resolver does not own.

    The distinction is which packet rule defends the pairing. A slot present in
    ``effective_by_slot`` belongs to a role carrying
    ``effective_strength_prescriptions``, and an explicit packet rule tells the
    finalizer to render that entry's ``effective_prescription`` and never its
    ``base_prescription`` — so a capped strength dose keeps both, and the raw
    bank dose stays visible as provenance.

    An item with no resolver-owned dose has no such rule. In practice that is an
    embedded support item: a ``strength_slots`` assignment composed into a
    *conditioning* role, which never carries ``effective_strength_prescriptions``
    and so falls outside the strength-scoped rule entirely. Shipping its capped
    ``effective_prescription`` next to the uncapped bank dose left the finalizer
    two competing doses with nothing to separate them, and it could render the
    bank dose. Only the authorised dose is sent.

    Provenance is unaffected: ``base_prescription`` stays on the rich Stage 1
    object, in the validator's view and in persistence.
    """
    if not isinstance(items, list):
        return items
    compact: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            compact.append(item)
            continue
        trimmed = {
            key: value
            for key, value in item.items()
            if key not in _ASSIGNMENT_PROVENANCE_FIELDS
        }
        base = trimmed.get("base_prescription")
        slot_id = str(trimmed.get("slot_id") or "")
        resolver_owned = slot_id in effective_by_slot
        effective = trimmed.get("effective_prescription") or effective_by_slot.get(slot_id)
        if base and effective and (base == effective or not resolver_owned):
            trimmed.pop("base_prescription", None)
        compact.append(trimmed)
    return compact


def _drops_locked_display_text(role: dict[str, Any]) -> bool:
    """True when this role's body is written by the server, not the finalizer.

    A ``selected_drill_locked`` role (in practice the deterministic Fight Tactical
    Watch) is rendered from its own ``tactical_watch`` object by the structured
    locked merge, by the deterministic fallback and by the source repair. Shipping
    the full multi-line script to a model that must not author it spends attention
    on content it cannot change. The drill's identity still reaches the finalizer
    via ``governance.selected_drill_name`` and ``preferred_exercise_names``, which
    is what it needs to plan the surrounding day.

    Stage 1 keeps ``display_text`` on the rich role: the faithfulness gate reads it
    from the planning brief, not from this packet.
    """
    governance = role.get("governance")
    return isinstance(governance, dict) and governance.get("selected_drill_locked") is True


def _compact_role(role: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "session_index",
        # Planner-decided within-day order (fightcamp.session_sequencing). Distinct
        # from session_index, which is planning identity, not execution order.
        "execution_order",
        "category",
        "role_key",
        "scheduled_day_hint",
        # preferred_tags stays: the tactical-identity boundary sanitizes it before
        # the handoff (a competing declared style is stripped), so it is delivered
        # deliberately rather than incidentally.
        "preferred_tags",
        "preferred_exercise_names",
        "anchor",
        # NOTE: selection_rule / placement_rule / day_assignment_reason are
        # deliberately NOT surfaced to the finalizer. They are internal Stage 1
        # selection/placement rationale (which slot was chosen and why), not
        # athlete-facing content, and no render instruction references them. The
        # finalizer treats Stage 1 as candidates and owns its own placement, with
        # the authoritative day signal coming from scheduled_day_hint +
        # calendar_days; these strings were highly repetitive (a handful of unique
        # values stamped across every role) and only bloated the prompt.
        "coach_owned",
        "display_text",
        "prescribed_duration_min",
        "athlete_facing_label",
        "camp_week_filler",
        "mandatory_tactical_watch",
        "mandatory_fight_visualization",
        "optional_fight_visualization",
        "weekly_requirement",
        "camp_phase",
        "stress_class",
        "cost_class",
        "support_insert_category",
        "support_insert_cost_category",
        "governance",
        "countdown_label",
        "scheduled_countdown_label",
        "countdown_display_label",
        "countdown_weekday",
        "real_weekday",
        "countdown_offset",
        "placement_basis",

        # Sparring dose truth from the planner / role map
        "hard_sparring_status",
        "hard_sparring_class",
        "hard_sparring_reason_codes",
        "hard_sparring_reason",
        "coach_note_flags",
        "coach_note",
        "replacement_role_key",
        "downgraded_from_role_key",
        "locked_day",

        # Gas-tank / recovery-day upgrade flags
        "gas_tank_recovery_touch",
        "allowed_on_recovery_day",
        "recovery_compatible",
        "converted_from_unused_day",
        "original_role_key",
        "original_unused_day_role",

        # Dedicated recovery/mobility support markers
        "is_dedicated_recovery_mobility_day",
        "priority_recovery_touch",
        "support_kind",
        "counts_toward_conditioning_cap",
        "counts_toward_exercise_cap",
        "counts_toward_strength_cap",

        # Safety filters for low-aerobic recovery work
        "blocked_systems",
        "blocked_intensities",
        "blocked_tags",

        # Deterministic late-camp strength dose truth. The resolver makes the
        # scheduled-day effective prescription authoritative so the finalizer
        # never renders the raw exercise-bank dose over a countdown-shaped role.
        # base/effective/authority/reason live inside effective_strength_prescriptions;
        # effective_strength_envelope is the numeric ceiling the validator enforces.
        "strength_dose_cap",
        "rpe_cap",
        "selected_exercise_assignments",
        "effective_strength_prescriptions",
        "effective_strength_envelope",
        "strength_session_index",
        "scheduled_d_day",
        "dose_adjustment_reason",
    )
    # ``selected_exercise_assignments=[]`` is an explicit closed-membership
    # sentinel.  Dropping it would make an intentionally empty role appear open
    # to the finalizer, incorrectly authorising downstream exercise selection.
    compact = {
        key: role.get(key)
        for key in keep
        if role.get(key) not in (None, "", [])
        or (
            key == "selected_exercise_assignments"
            and key in role
            and role.get(key) == []
        )
    }
    effective_by_slot = {
        str(item.get("slot_id") or ""): str(item.get("effective_prescription") or "")
        for item in role.get("effective_strength_prescriptions") or []
        if isinstance(item, dict) and item.get("effective_prescription")
    }
    for key in ("selected_exercise_assignments", "effective_strength_prescriptions"):
        if key in compact:
            compact[key] = _compact_prescribed_items(
                compact[key], effective_by_slot=effective_by_slot
            )
    if _drops_locked_display_text(role):
        compact.pop("display_text", None)
    if "governance" in compact:
        governance = _compact_governance(compact["governance"])
        if governance:
            compact["governance"] = governance
        else:
            compact.pop("governance")
    return compact


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [value]


def _int_value(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _planned_weekly_count(week: dict[str, Any], athlete_model: dict[str, Any]) -> int:
    for key in ("weekly_training_frequency", "training_frequency", "weekly_sessions"):
        if key in athlete_model:
            count = _int_value(athlete_model.get(key), -1)
            if count >= 0:
                return count
    declared_training_days = _as_list(week.get("declared_training_days"))
    if declared_training_days:
        return len(declared_training_days)
    counts = week.get("session_counts")
    if isinstance(counts, dict):
        return sum(_int_value(counts.get(key), 0) for key in ("strength", "conditioning", "recovery", "technical"))
    return 0


def _is_coach_owned_role(role: dict[str, Any]) -> bool:
    role_key = str(role.get("role_key") or "").strip().lower()
    category = str(role.get("category") or "").strip().lower()
    return bool(role.get("coach_owned")) or category == "sparring" or role_key == "hard_sparring_day"


def _collect_reason_codes(week: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    for collection_key in ("session_roles", "suppressed_roles", "hard_sparring_plan"):
        for entry in _as_list(week.get(collection_key)):
            if not isinstance(entry, dict):
                continue
            for code_key in ("reason_codes", "hard_sparring_reason_codes", "compression_reason_codes"):
                codes.update(str(code).strip() for code in _as_list(entry.get(code_key)) if str(code).strip())
    compression = week.get("intentional_compression")
    if isinstance(compression, dict):
        codes.update(str(code).strip() for code in _as_list(compression.get("reason_codes")) if str(code).strip())
        reason = str(compression.get("reason") or "").strip()
        if reason:
            codes.add(reason)
    fight_day_override = week.get("fight_day_override")
    if isinstance(fight_day_override, dict) and fight_day_override.get("active"):
        codes.add("fight_day_override")
    return codes


def _has_active_weight_cut(athlete_model: dict[str, Any]) -> bool:
    if athlete_model.get("weight_cut_risk"):
        return True
    if str(athlete_model.get("cut_severity_bucket") or "").strip().lower() not in {"", "none", "low"}:
        return True
    try:
        return float(athlete_model.get("weight_cut_pct") or 0) > 0
    except (TypeError, ValueError):
        return False


def _has_active_injury(athlete_model: dict[str, Any]) -> bool:
    return bool(
        athlete_model.get("has_active_injury")
        or _as_list(athlete_model.get("parsed_injuries"))
        or _as_list(athlete_model.get("injury_restrictions"))
        or str(athlete_model.get("injuries_raw_text") or "").strip()
    )


def _session_count_summary(week: dict[str, Any], athlete_model: dict[str, Any]) -> dict[str, Any]:
    roles = [role for role in _as_list(week.get("session_roles")) if isinstance(role, dict)]
    coach_owned_count = sum(1 for role in roles if _is_coach_owned_role(role))
    app_owned_count = max(0, len(roles) - coach_owned_count)
    planned_count = _planned_weekly_count(week, athlete_model)
    rendered_total = len(roles)
    reduced = planned_count > 0 and rendered_total < planned_count
    reason_codes = _collect_reason_codes(week)
    compression = week.get("intentional_compression")

    reasons: list[str] = []
    if str(week.get("phase") or "").strip().upper() == "TAPER":
        reasons.append("taper")
    if (_has_active_weight_cut(athlete_model) and str(athlete_model.get("cut_severity_bucket") or "").strip().lower() not in {"", "none", "low", "moderate"}) or reason_codes & {"active_weight_cut", "high_pressure_weight_cut", "weight_cut_high_suppress_hard_work", "weight_cut_unsafe_block"}:
        reasons.append("weight_cut")
    if "d17_hard_sparring_ban" in reason_codes:
        reasons.append("d17_technical_only_rule")
    elif "d14_hard_sparring_ban" in reason_codes:
        reasons.append("d14_technical_only_rule")
    # Explain actual removals, not injury presence: a mild symptom must not
    # be reintroduced here as a global workload restriction in athlete prose.
    if "injury_management" in reason_codes and not _all_active_injuries_surface_only(athlete_model):
        reasons.append("injury_management")
    if "coach_owned_appointment_capacity" in reason_codes:
        reasons.append("coach_owned_appointment_capacity")
    if coach_owned_count or week.get("hard_sparring_plan") or week.get("effective_hard_sparring_days"):
        reasons.append("coach_led_contact_load")
    if "fight_week_override" in reason_codes:
        reasons.append("fight_week_override")
    if isinstance(compression, dict) and compression.get("active"):
        reasons.append("intentional_compression")
    if "no_eligible_exercise_membership" in reason_codes:
        reasons.append("no_eligible_exercise")

    reason_labels = {
        "taper": "Taper trims volume while preserving sharpness.",
        "weight_cut": "Target-weight pressure tightens recovery tolerance.",
        "d17_technical_only_rule": "Elevated risk brings hard-sparring conversion forward to D-17.",
        "d14_technical_only_rule": "The D-14 hard-sparring cutoff moves contact work to technical-only.",
        "injury_management": "Injury management removes or compresses risky standalone work.",
        "coach_led_contact_load": "Hard sparring / contact work owns part of the weekly load.",
        "coach_owned_appointment_capacity": "Your coached sessions count toward your weekly training schedule.",
        "fight_week_override": "Fight-week override caps app-owned work.",
        "intentional_compression": "Planner marked this as intentional compression.",
        "no_eligible_exercise": "A session with no eligible exercise was removed rather than filled.",
    }

    summary = {
        "planned_weekly_count": planned_count,
        "rendered_total_count": rendered_total,
        "rendered_app_owned_count": app_owned_count,
        "coach_owned_count": coach_owned_count,
        "reduced_from_planned": reduced,
        "reduction_reasons": [reason_labels[reason] for reason in dict.fromkeys(reasons)] if reduced else [],
        "reason_codes": sorted(reason_codes),
    }
    return {key: value for key, value in summary.items() if value not in (None, "", [])}


def _server_owned_locked_role(role: dict[str, Any]) -> bool:
    return str(role.get("role_key") or "").strip().lower() in {
        "tactical_watch", "fight_visualization"
    }


def _compact_weekly_role_map(weekly_role_map: Any, athlete_model: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(weekly_role_map, dict):
        return {}

    athlete_model = athlete_model or {}
    compact_weeks: list[dict[str, Any]] = []
    for week in weekly_role_map.get("weeks", []) or []:
        if not isinstance(week, dict):
            continue

        compact_weeks.append(
            {
                key: value
                for key, value in {
                    "week_index": week.get("week_index"),
                    "phase": week.get("phase"),
                    "label": week.get("label"),
                    "session_roles": [
                        _compact_role(role)
                        for role in (week.get("session_roles") or [])
                        if isinstance(role, dict) and not _server_owned_locked_role(role)
                    ],
                    "suppressed_roles": [
                        _compact_role(role)
                        for role in (week.get("suppressed_roles") or [])
                        if isinstance(role, dict) and not _server_owned_locked_role(role)
                    ],
                    "fight_day_override": week.get("fight_day_override"),
                    "projected_days_until_fight_start": week.get("projected_days_until_fight_start"),
                    "projected_days_until_fight_end": week.get("projected_days_until_fight_end"),
                    "countdown_range": week.get("countdown_range"),
                    "calendar_days": week.get("calendar_days"),
                    "intentionally_unused_days": week.get("intentionally_unused_days"),
                    "declared_training_days": week.get("declared_training_days"),
                    "declared_hard_sparring_days": week.get("declared_hard_sparring_days"),
                    "declared_support_work_days": week.get("declared_support_work_days"),
                    "hard_sparring_plan": [
                        dict(entry)
                        for entry in week.get("hard_sparring_plan")
                        if isinstance(entry, dict)
                    ] if isinstance(week.get("hard_sparring_plan"), list) else None,
                    "effective_hard_sparring_days": week.get("effective_hard_sparring_days"),
                    "final_week_sparring_cap": week.get("final_week_sparring_cap"),
                    "coach_note_flags": week.get("coach_note_flags"),
                    "intentional_compression": week.get("intentional_compression"),
                    "session_count_summary": _session_count_summary(week, athlete_model),
                }.items()
                if value not in (None, "", [])
            }
        )

    return {
        key: value
        for key, value in {
            "weeks": compact_weeks,
            "fight_day_override": weekly_role_map.get("fight_day_override"),
        }.items()
        if value not in (None, "", [])
    }


# Internal late-fight scaffolding the finalizer never needs. The LLM already
# gets the countdown session list via selected_plan.session_sequence
# (visible_session_sequence is a byte-identical copy of it), and allocator /
# role_budget / permission_policy are Stage 1 allocation internals that no render
# instruction references. The one referenced cap (max_active_roles) lives at the
# spec top level and is preserved; only these internal-only keys are stripped.
_LATE_FIGHT_SPEC_INTERNAL_KEYS = (
    "visible_session_sequence",
    "session_sequence",
    "sessions",
    "allocator",
    "role_budget",
    "permission_policy",
)


def _compact_late_fight_plan_spec(late_fight_plan_spec: Any) -> Any:
    """Return the LLM-facing late-fight spec without internal allocation scaffolding.

    Non-mutating: builds a new dict so the source spec (still read by the Stage 1
    pipeline and the Stage 2 validator) is untouched. ``countdown_mode_sequence``
    is preserved because the handoff's countdown-continuation map reads it.
    """

    if not isinstance(late_fight_plan_spec, dict):
        return late_fight_plan_spec
    return {
        key: value
        for key, value in late_fight_plan_spec.items()
        if key not in _LATE_FIGHT_SPEC_INTERNAL_KEYS
    }


def _compact_session_sequence(stage2_payload: dict[str, Any]) -> list[dict[str, Any]]:
    plan_spec = stage2_payload.get("late_fight_plan_spec") or {}
    if isinstance(plan_spec, dict):
        value = plan_spec.get("visible_session_sequence")
        if isinstance(value, list):
            return [entry for entry in value if isinstance(entry, dict) and not _server_owned_locked_role(entry)]

    for key in (
        "late_fight_session_sequence",
        "session_sequence",
        "countdown_sessions",
    ):
        value = stage2_payload.get(key)
        if isinstance(value, list):
            return [entry for entry in value if isinstance(entry, dict) and not _server_owned_locked_role(entry)]

    if isinstance(plan_spec, dict):
        value = plan_spec.get("session_sequence") or plan_spec.get("sessions")
        if isinstance(value, list):
            return [entry for entry in value if isinstance(entry, dict) and not _server_owned_locked_role(entry)]

    return []


def _render_guards(stage2_payload: dict[str, Any]) -> dict[str, Any]:
    athlete_model = stage2_payload.get("athlete_model") or {}
    rewrite_guidance = stage2_payload.get("rewrite_guidance") or {}
    guards = rewrite_guidance.get("render_guards")

    if isinstance(guards, dict) and guards:
        return guards

    return _render_guard_flags(
        athlete_model=athlete_model,
        payload_mode=str(stage2_payload.get("payload_mode") or ""),
        days_until_fight=athlete_model.get("days_until_fight"),
    )


def build_stage2_finalizer_packet(
    *,
    stage2_payload: dict[str, Any],
    planning_brief: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the compact LLM-facing Stage 2 packet.

    This function must not mutate the original Stage 2 payload.
    It intentionally excludes full candidate pools and internal scoring data.

    planning_brief is optional but preferred when available because it can carry
    richer late-fight/session sequencing data than the raw Stage 2 payload.
    """

    source = planning_brief if isinstance(planning_brief, dict) else stage2_payload

    athlete_model = (
        source.get("athlete_snapshot")
        or source.get("athlete_model")
        or stage2_payload.get("athlete_model")
        or {}
    )
    rewrite_guidance = (
        source.get("decision_rules")
        or source.get("rewrite_guidance")
        or stage2_payload.get("rewrite_guidance")
        or {}
    )

    guards = _render_guards(
        {
            **stage2_payload,
            "athlete_model": athlete_model,
            "rewrite_guidance": rewrite_guidance,
            "payload_mode": (
                source.get("payload_mode")
                or source.get("effective_stage2_mode")
                or stage2_payload.get("payload_mode")
                or stage2_payload.get("effective_stage2_mode")
                or ""
            ),
        }
    )
    render_mode = guards.get("render_mode") or "camp_plan"

    weekly_role_map = source.get("weekly_role_map") or stage2_payload.get("weekly_role_map")
    late_fight_plan_spec = _compact_late_fight_plan_spec(
        source.get("late_fight_plan_spec")
        or stage2_payload.get("late_fight_plan_spec")
        or {}
    )
    open_plan_spec = (
        source.get("open_plan_spec")
        or stage2_payload.get("open_plan_spec")
        or {}
    )

    # Resolve goal-preservation entries and their contract version from the same
    # fallback chain, so version and data always travel together. Open/ongoing
    # payloads skip reconciliation and carry the entries only in
    # compressed_priorities (with no top-level version); stamp the current
    # contract when the supplying container lacks one, rather than emitting
    # entries alongside a None version.
    from .goal_preservation import VERSION as _GOAL_PRESERVATION_VERSION

    _goal_snapshot = source.get("athlete_snapshot") or source.get("athlete_model") or {}
    goal_preservation_entries: list = []
    goal_preservation_version = source.get("goal_preservation_version")
    for _goal_source in (
        source,
        source.get("compressed_priorities") or {},
        _goal_snapshot.get("compressed_priorities") or {},
        stage2_payload,
    ):
        _entries = _goal_source.get("goal_preservation")
        if _entries:
            goal_preservation_entries = deepcopy(_entries)
            # Core Development Block evidence detail is planner-only metadata.
            for _entry in goal_preservation_entries:
                for _item in (_entry.get("evidence") or []) if isinstance(_entry, dict) else []:
                    if isinstance(_item, dict):
                        for _key in ("trunk_function", "trunk_load_tier", "trunk_development", "trunk_block_functions"):
                            _item.pop(_key, None)
            goal_preservation_version = (
                _goal_source.get("goal_preservation_version") or _GOAL_PRESERVATION_VERSION
            )
            break

    packet = {
        "packet_type": "stage2_finalizer_packet",
        "packet_version": 1,
        "render_mode": render_mode,
        "athlete_model": _compact_athlete_model(athlete_model),
        "render_guards": guards,
        "hard_rules": [
            "selected_plan.goal_preservation is the deterministic final decision for every selected athlete goal, overriding generic Build wording in priority_focus.main_focus. Render build, maintain and explicit deferrals faithfully. Explain deferred goals and their constraint in Lead notes using plain language; never claim a deferred goal is being trained. Do not reconsider these states from role names or draft prose.",
            "Each goal_preservation evidence entry with a name is a required selected stimulus on its recorded D-day. Preserve that exercise and its effective prescription/dose. Do not substitute a power, rehab, balance or primer exercise for meaningful_strength. These witnesses override general freedom to reselect exercises. Missing evidence never authorises an invented exercise or training claim.",
            "Render only athlete-facing plan content.",
            "Do not expose candidate pools, scoring logic, internal menus, or unused options.",
            "weekly_role_map.weeks[*].calendar_days is the only authority for weekday and D-day labels.",
            "Do not infer D-days from weekday order.",
            "Do not invent D-days from the fight date manually.",
            "Only render session_roles whose scheduled_day_hint exists in that week's calendar_days.",
            "When one calendar day carries more than one session role, render them in ascending execution_order. The deterministic planner decided that order from each session's purpose (prepare, prime, skill, contact, strength, conditioning, restore, review); explain it if useful, but never reorder it.",
            "Use session_count_summary to explain reduced weeks; do not restore suppressed roles to match the athlete's planned weekly frequency.",
            "Stage 1 draft exercise text is candidate material only. For a role with selected_exercise_assignments, those assignments are the deterministic planner's complete session membership: render every assigned exercise and do not add candidates, alternates, substitutes, or other S&C exercises. Use each assignment's effective_prescription when present; it is the dose authority. Final exercise rendering must obey weekly_role_map role, count, day ownership, restrictions, and taper rules first.",
            "Prescribe one exact working value for every round count, set count, rep count, duration, hold, rest interval, load and RPE. For support roles, prescribed_duration_min is the chosen duration; render it instead of the duration_min bounds. When an authorised effective prescription gives a range, choose one value inside it using the athlete's fight format, goals, phase, days to fight, fatigue, injury restrictions and session purpose; do not simply repeat the range or always choose the same endpoint. Preserve every already-exact value and all hard safety caps. Do not turn coach-owned sparring into an app dose. An optional easier alternative or safety limit may remain conditional, but the primary work must never leave a numeric range for the athlete to decide.",
            "For a conditioning role with selected_exercise_assignments, render one separate bullet for every assignment: `- exact assignment name: one exact working dose within effective_prescription`. A round_based assignment already has the athlete's exact fight-round duration in effective_prescription; preserve it. Do not merge assignments into a single conditioning block, promote one assignment to the only primary drill, turn another assignment into an optional fallback, or replace it with a Stage 1 draft/candidate exercise.",
            "For every role with selected_exercise_assignments, closed membership overrides all Stage 2 prompt, writing-rule, decision-rule, anchor-standard, safe-strong, goal-support, accessory, equipment-replacement, and substitution guidance, including Rules 4, 5, 6A, 7, and 8. Those rules may change wording or reduce dose only within the selected set. If a hard restriction makes a selected exercise illegal, remove/hold that selected exercise and leave the gap; never choose a downstream replacement. Exercise selection must return upstream to deterministic composition.",
            # Late-camp effective strength dose is authoritative over the bank dose.
            "If a session role carries effective_strength_prescriptions, each entry's effective_prescription is the authoritative dose or dose bound for that exercise on that day. Render one exact value within any bound, never the base_prescription, and never a dose above it. Its base_prescription is the original exercise-bank dose kept only for provenance — do not render it as the prescription.",
            "Preserve the effective prescription's exertion method: a %1RM load stays %1RM, and max-speed jump/throw work stays a speed-and-quality cue. Do not replace either with generic RPE.",
            "Write each exercise's coaching lines as RULE 6A describes; a Swap is a suggestion, not an added exercise. Deterministic reconciliation preserves your valid lines and rejects unsupported adjustments; it does not write replacements.",
            "A selected_exercise_assignments entry may carry coaching_notes: the authored exercise-bank guidance for that exercise. Use it as source-backed execution evidence — choose the one or two most relevant cues (technique, load, or a stop/quality rule) and phrase them in your own words. Do not dump every note verbatim or repeat generic advice. coaching_notes is never a dose: it can never override effective_prescription, effective_strength_envelope, restrictions, taper rules, or closed membership.",
            "If a session role carries effective_strength_envelope, treat it as a hard ceiling: do not render more sets than effective_strength_envelope.max_sets, more reps than max_reps (for the loaded anchor/secondary lifts), or a higher RPE than rpe_cap_high. If effective_strength_envelope.loaded_allowed is false, render no loaded strength lifting on that day — neural/primer, readiness, or mobility work only.",
            "If effective_strength_envelope.complete_exercise_allow_list is true, effective_strength_envelope.allowed_exercise_names is the complete S&C exercise allow-list selected by the deterministic planner for that role. Render only those named exercises; do not restore omitted candidates, alternates, substitutes, add another loaded lift, or invent another strength, power, plyometric, trunk, or support exercise for that session. An individually legal dose does not make an unselected exercise legal. If effective_strength_envelope.forbid_slow_eccentric_emphasis is true, do not restore a slow/tempo eccentric prescription from the base exercise-bank text.",
            "If selected_plan.session_sequence is present, render every entry in selected_plan.session_sequence as its own athlete-facing countdown card.",
            "Each selected_plan.session_sequence entry with scheduled_countdown_label/countdown_display_label must appear as a visible D-X header in the final output.",
            "Do not omit selected support, recovery, freshness, mobility, reset, or technical roles because they are low stress or short duration.",
            # Locked-drill contract, reduced to context. Stage 1 selects the
            # activity AND the server now writes its body into every athlete-facing
            # surface: merge_locked_structured_content builds the card from the
            # deterministic tactical_watch object, the deterministic fallback
            # rebuilds it when Stage 2 fails, and the source repair inserts it into
            # the plan text. The finalizer used to be told to reproduce that body
            # verbatim, so the whole multi-line script had to be shipped to it; it
            # no longer authors any of it. What remains is the one thing only the
            # finalizer can get wrong: the day must exist and must stay free of
            # adaptive S&C.
            "Tactical Focus and Fight Visualisation are server-owned. Their roles are omitted from this handoff; do not create substitute or generic sessions for them. The server inserts their exact cards after conversion. Keep the calendar days available for that insertion.",
            "Do not collapse a selected countdown session into Lead notes, another day, movement prep, mobility finisher, rationale, or a generic note. It must keep its own D-X card.",
            "If selected_plan.session_sequence contains D-3 fight_week_freshness_day, render a D-3 freshness/reset card even when it is support-class and low RPE.",
            "Render selected countdown cards in descending countdown order, then append D-0 last.",
            "If a calendar day has is_fight_day=true, render fight_day_protocol and no other app-led training. Any governed visualisation is inserted by the server afterward.",
            "If a calendar day has is_after_fight_day=true, render no app-led training.",
            "If a weekday is not present in calendar_days, do not render it.",
            "Do not render any session after D-0 unless a post-fight recovery mode is explicitly active.",
            "D-0 renders as fight-day protocol with no other model-authored training; the server inserts any governed visualisation afterward.",
            "If late_fight_plan_spec is present, append a terminal D-0 fight-day protocol block after the final active countdown day. D-0 is not an app training session, does not count toward max_active_roles, and must be the final athlete-facing block.",
            "Do not append Coach note, Final coach notes, summary, nutrition, recovery, or any footer after D-0. Put summary notes in Lead notes before the first week or omit them.",
            f"Fight-day protocol text: {FIGHT_DAY_PROTOCOL_TEXT}",
            "Declared hard-sparring/contact days override app S&C unless the contact work is light or cancelled.",
            "For late_fight_plan_spec.allowed_exercises_by_day, each countdown day may render only those listed exercise names plus generic breathing, mobility/reset, shadowboxing/technical cues, hard-sparring/contact session labels, and rehab/prehab band resets except on D-1, where all band work is blocked.",
            "Preserve the priority hierarchy from priority_focus. Do not treat all goals and weak areas equally. Primary goal and primary weak area shape emphasis; secondary selections support without taking over.",
            "If priority_focus.goal_weakness_collisions is non-empty, treat overlap as valid athlete intent. Do not remove it or overcorrect it. Use priority_focus.collision_detail when present to clarify the limiter.",
            "If priority_focus.collision_details contains multiple entries, preserve each clarification. Do not collapse all overlaps into the first detail. Use each detail to sharpen the relevant training emphasis.",
            "Use priority_focus.derived_clarification_tags as internal emphasis signals when preserving the plan's intent. These tags clarify the kind of adaptation the athlete meant, but they do not override hard safety, schedule, injury, phase, or recovery constraints.",
            "Do not expose derived_clarification_tags or raw scoring/reason-code labels directly in athlete-facing text.",
            "Use parsed_injuries and guided_source_injury_subtypes as injury context only. Do not override parsed injury_type or invent diagnoses from subtype tags.",
            "Small mobility prep, reset, or warm-up that appears inside another session does not satisfy or replace a dedicated recovery/mobility day. A role flagged is_dedicated_recovery_mobility_day is a session-level recovery tool and must not be suppressed because mobility already appears as prep elsewhere in the week.",
        ],
        "forbidden_output": {
            "phase_toolbox_labels": list(_FORBIDDEN_TOOLBOX_LABELS),
            "rehab_labels_when_no_active_injury": (
                list(_FORBIDDEN_REHAB_LABELS)
                if guards.get("suppress_rehab_headings")
                else []
            ),
        },
        "restrictions": _compact_restrictions(
            source.get("restrictions") or stage2_payload.get("restrictions")
        ),
        "selected_plan": {
            "goal_preservation_version": goal_preservation_version,
            "goal_preservation": goal_preservation_entries,
            "session_sequence": _compact_session_sequence(source)
            or _compact_session_sequence(stage2_payload),
            "weekly_role_map": _compact_weekly_role_map(weekly_role_map, athlete_model),
            "late_fight_plan_spec": late_fight_plan_spec,
            "open_plan_spec": open_plan_spec,
            "fight_week_override": (
                source.get("fight_week_override")
                or stage2_payload.get("fight_week_override")
                or {}
            ),
            "week_by_week_progression": _compact_week_progression(
                source.get("week_by_week_progression")
                or stage2_payload.get("week_by_week_progression")
                or {}
            ),
            "priority_focus": (
                source.get("priority_focus")
                or stage2_payload.get("priority_focus")
                or {}
            ),
        },
        # Strip the rules STAGE2_FINALIZER_PROMPT already states in prose. The
        # prompt and the packet used to carry the same ~20 rules in two slightly
        # different wordings; the model reads both and has to reconcile them.
        # Only the state-dependent rules the prose cannot know (render guards,
        # triage, declared tactical styles) still travel in the packet.
        "writing_rules": [
            rule
            for rule in ((rewrite_guidance or {}).get("writing_rules") or [])
            if rule not in FINALIZER_PROMPT_COVERED_WRITING_RULES
        ],
    }

    # Only dated camp mode needs compact phase context.
    if render_mode == "camp_plan":
        packet["phase_briefs"] = (
            source.get("phase_briefs")
            or stage2_payload.get("phase_briefs")
            or {}
        )

    return packet
