from __future__ import annotations

import logging
import re
from time import perf_counter

from .bout_format import bout_format_metadata
from .coach_review import run_coach_review
from .athlete_model import _derive_competitive_maturity
from .conditioning import generate_conditioning_block, get_technical_footwork_bank
from api.contracts.readiness_message import ReadinessCheckin, ReadinessContext, build_readiness_adjustment
from .injury_location import canonicalize_location
from .mindset_module import get_mindset_by_phase
from .nutrition import generate_nutrition_block
from .plan_pipeline_runtime import (
    PHASES,
    PHASE_COLORS,
    PlanBlocksBundle,
    PlanRuntimeContext,
    ProgressCallback,
    TimingRecorder,
    _apply_muay_thai_filters,
    _emit_progress,
)
from .plan_rendering_utils import sanitize_phase_text
from .rehab_protocols import (
    format_injury_guardrails,
    generate_rehab_protocols,
    generate_support_notes,
)
from .strength import generate_strength_block
from .strength_session_quality import classify_strength_item
from .training_context import TrainingContext, allocate_sessions

def _run_stage1_module(
    *,
    module_name: str,
    started_code: str,
    finished_code: str,
    label_prefix: str,
    logger: logging.Logger,
    progress_callback: ProgressCallback | None,
    record_timing: TimingRecorder,
    timing_label: str,
    fn,
):
    _emit_progress(progress_callback, started_code, f"{label_prefix} started")
    timer_start = perf_counter()
    result = fn()
    elapsed = perf_counter() - timer_start
    record_timing(timing_label, timer_start)
    logger.info("[stage1] module_elapsed module=%s elapsed=%.2f", module_name, elapsed)
    if elapsed > 10.0:
        logger.warning("[stage1] slow_module module=%s elapsed=%.2f", module_name, elapsed)
    _emit_progress(progress_callback, finished_code, f"{label_prefix} finished")
    return result

def _build_phase_mindsets(training_context: TrainingContext) -> dict[str, str]:
    # Compute once; the mental-block classification is invariant across phases.
    base_flags = training_context.to_flags()
    blocks = training_context.mental_block
    if isinstance(blocks, str):
        blocks = [blocks]
    generic = not blocks or blocks[0].lower() == "generic"
    mindset_flags = {"mental_block": ["generic"]} if generic else base_flags

    return {phase: get_mindset_by_phase(phase, mindset_flags) for phase in PHASES}


def _is_rotational_power_anchor(exercise: dict) -> bool:
    """Does this selection count as real rotational-power exposure?

    Matches the definition the selection boost uses: a rotational-power
    *anchor*. Authored governance can leave the rotational base category on an
    item it has demoted to support, and a support twist is not the exposure the
    camp-level gap is about — treating it as such would close the gap and stop
    a genuine rotational throw from ever being favoured.
    """
    profile = classify_strength_item(exercise)
    return bool(profile["anchor_capable"]) and "rotational_power" in profile["base_categories"]


def _generate_strength_blocks(
    context: PlanRuntimeContext,
    *,
    progress_callback: ProgressCallback | None = None,
) -> tuple[dict[str, dict | None], dict[str, list[dict]]]:
    strength_blocks: dict[str, dict | None] = {phase: None for phase in PHASES}
    strength_reason_log: dict[str, list[dict]] = {}
    previous_names: list[str] = []
    previous_movements: set[str] = set()
    # Camp-level rotational-power coverage. Rotational power is a distinct power
    # family, so a camp whose development phases never touch it has a hole the
    # per-phase rules cannot see. Once any phase selects a rotational-power
    # anchor the gap closes and later phases score normally.
    rotational_power_seen = False
    # Compute once per request; spread into per-phase flags dict below.
    base_flags = context.training_context.to_flags()
    logger = logging.getLogger(__name__)

    def _emit_strength_substep(code: str, label: str) -> None:
        _emit_progress(progress_callback, code, label)

    for phase in PHASES:
        if not context.phase_active(phase):
            continue
        flags = {
            **base_flags,
            "phase": phase,
            "random_seed": context.random_seed,
            "restrictions": context.training_context.injury_restrictions,
            "ignore_restrictions": context.selection_ignore_restrictions,
            "strength_substep_callback": _emit_strength_substep,
        }
        if previous_names:
            flags["prev_exercises"] = previous_names
            flags["recent_exercises"] = sorted(previous_movements)
        if not rotational_power_seen:
            flags["rotational_power_camp_gap"] = True
        phase_step = f"phase_{phase.lower()}"
        _emit_strength_substep(f"stage1_strength_{phase_step}_started", f"Stage 1 strength {phase} started")
        phase_started = perf_counter()
        try:
            block = generate_strength_block(
                flags=flags,
                weaknesses=context.training_context.weaknesses,
            )
        finally:
            phase_elapsed = perf_counter() - phase_started
            logger.info("[stage1] strength_substep_elapsed step=%s elapsed=%.2f", phase_step, phase_elapsed)
            if phase_elapsed > 5.0:
                logger.warning("[stage1] slow_strength_substep step=%s elapsed=%.2f", phase_step, phase_elapsed)
        _emit_strength_substep(f"stage1_strength_{phase_step}_finished", f"Stage 1 strength {phase} finished")
        strength_blocks[phase] = block
        strength_reason_log[phase] = block.get("why_log", [])
        phase_names = [exercise["name"] for exercise in block.get("exercises", []) if exercise.get("name")]
        phase_movements = {
            exercise["movement"]
            for exercise in block.get("exercises", [])
            if exercise.get("movement")
        }
        previous_names = list(dict.fromkeys([*previous_names, *phase_names]))
        previous_movements |= phase_movements
        if not rotational_power_seen:
            rotational_power_seen = any(
                _is_rotational_power_anchor(exercise)
                for exercise in block.get("exercises", [])
            )

    return strength_blocks, strength_reason_log


def _generate_conditioning_blocks(context: PlanRuntimeContext, *, progress_callback: ProgressCallback | None = None) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    conditioning_blocks: dict[str, dict] = {}
    conditioning_reason_log: dict[str, list[dict]] = {}
    # Compute once per request; spread into per-phase flags dict below.
    base_flags = context.training_context.to_flags()

    # Use the existing Today decision engine for recent external work. Only
    # conditioning selection in the first active phase receives this snapshot;
    # phase lengths, strength, contact permissions and coach sessions are intact.
    history = context.training_context.sparring_readiness.get("training_history", {})
    workload_adjustment = None
    first_phase = next((phase for phase in PHASES if context.phase_active(phase)), None)
    current_checkins = [row for row in history.get("checkins", []) if row.get("training_day") == history.get("as_of")]
    if history.get("available") and any(row.get("source") == "external" for row in history.get("sessions", [])) and current_checkins:
        latest = max(current_checkins, key=lambda row: row.get("updated_at", ""))
        checkin = ReadinessCheckin(**{key: value for key, value in latest.items() if key in ReadinessCheckin.__dataclass_fields__})
        workload_adjustment = build_readiness_adjustment(checkin, ReadinessContext(
            training_day=history.get("as_of", ""), phase=first_phase or checkin.phase,
            today_session={"session_type": "conditioning", "title": "Conditioning"},
            recent_sessions=history.get("sessions", []), recent_checkins=history.get("checkins", []),
        ))

    logger = logging.getLogger(__name__)
    conditioning_started = perf_counter()
    # Status + record -> competitive maturity, the same derivation the Stage 2
    # athlete model uses. Technical footwork uses it only to rank drills of a
    # fitting complexity; it never changes dose, eligibility or session count.
    competitive_maturity = _derive_competitive_maturity(
        context.training_context.status, context.plan_input.record
    )["competitive_maturity"]
    technical_footwork_names = {
        drill.get("name") for drill in get_technical_footwork_bank()
    }
    # Technical footwork already placed in an earlier phase, so later phases can
    # vary it (GPP/SPP) or keep it familiar (taper).
    prior_technical_footwork: list[str] = []

    for phase in PHASES:
        if not context.phase_active(phase):
            continue
        phase_code = f"stage1_conditioning_phase_{phase.lower()}"
        _emit_progress(progress_callback, f"{phase_code}_started", f"Stage 1 conditioning {phase} started")
        phase_started = perf_counter()
        phase_flags = {**base_flags}
        # Reuse the existing moderate-fatigue conditioning selector only when
        # the Today engine finds repeated hard work AND poor current readiness.
        if (phase == first_phase and workload_adjustment
                and workload_adjustment.decision in {"modify", "pull_back"}
                and "recent_hard_load_plus_poor_today" in workload_adjustment.triggers
                and base_flags.get("fatigue") in {"low", "none"}):
            phase_flags["fatigue"] = "moderate"
        (
            block_text,
            names,
            reasons,
            grouped_drills,
            missing_systems,
            candidate_reservoir,
        ) = generate_conditioning_block(
            {
                **phase_flags,
                "phase": phase,
                "sport": context.canonical_sport,
                # Round-based conditioning is dosed at the athlete's own round
                # length, so selection must measure the workload it will render.
                "rounds_format": context.plan_input.rounds_format,
                "random_seed": context.random_seed,
                "competitive_maturity": competitive_maturity,
                "prior_technical_footwork": list(prior_technical_footwork),
                "time_to_fight_days": context.plan_input.days_until_fight,
                "weeks_out": context.plan_input.weeks_out,
                "restrictions": context.training_context.injury_restrictions,
                "ignore_restrictions": context.selection_ignore_restrictions,
                "conditioning_substep_callback": lambda code, label: _emit_progress(progress_callback, code, label),
            }
        )
        for name in names:
            if name in technical_footwork_names and name not in prior_technical_footwork:
                prior_technical_footwork.append(name)
        phase_elapsed = perf_counter() - phase_started
        logger.info("[stage1] conditioning_phase_elapsed phase=%s elapsed=%.2f", phase.lower(), phase_elapsed)
        if phase_elapsed > 10.0:
            logger.warning("[stage1] slow_conditioning_phase phase=%s elapsed=%.2f", phase.lower(), phase_elapsed)
        _emit_progress(progress_callback, f"{phase_code}_finished", f"Stage 1 conditioning {phase} finished")
        render_metadata = {
            "num_sessions": allocate_sessions(context.training_context.training_frequency, phase).get("conditioning", 1),
            "diagnostic_context": {
                "phase": phase,
                "sport": context.canonical_sport,
                "time_to_fight_days": context.plan_input.days_until_fight,
                "days_until_fight": context.plan_input.days_until_fight,
                "weeks_out": context.plan_input.weeks_out,
                "fatigue_level": phase_flags.get("fatigue", context.training_context.fatigue),
                "injuries": context.training_context.injuries,
                "fight_format": context.training_context.fight_format,
                "bout_format": bout_format_metadata(context.plan_input.rounds_format),
            },
            "sport": context.canonical_sport,
            # Preserve the canonical stance in conditioning metadata so any
            # re-render (coach review, late-fight) resolves the same
            # technical-footwork side/stance instruction as the first render.
            "stance": context.training_context.stance,
        }
        conditioning_reason_log[phase] = reasons
        conditioning_blocks[phase] = {
            "block": block_text,
            "names": names,
            "why_log": reasons,
            "grouped_drills": grouped_drills,
            "missing_systems": missing_systems,
            "candidate_reservoir": candidate_reservoir,
            # Canonical bout-format demand facts, available to later consumers
            # without reparsing the intake string. Observability only.
            "bout_format": bout_format_metadata(context.plan_input.rounds_format),
            "phase_color": PHASE_COLORS[phase],
            "num_sessions": render_metadata.get("num_sessions", 1),
            "diagnostic_context": render_metadata.get("diagnostic_context", {}),
            "sport": render_metadata.get("sport"),
            "stance": render_metadata.get("stance"),
        }

    total_elapsed = perf_counter() - conditioning_started
    if total_elapsed > 30.0:
        logger.warning("[stage1] slow_conditioning_total elapsed=%.2f", total_elapsed)
    return conditioning_blocks, conditioning_reason_log


def _first_active_phase(phase_weeks: dict) -> str:
    return next((phase for phase in PHASES if phase_weeks.get(phase, 0) > 0 or phase_weeks.get("days", {}).get(phase, 0) >= 1), "GPP")


def _build_phase_support_block(context: PlanRuntimeContext, builder) -> str:
    active_phases = [phase for phase in PHASES if context.phase_active(phase)]
    sections: list[str] = []

    for phase in active_phases:
        block = builder(phase).strip()
        if not block:
            continue
        if len(active_phases) == 1:
            sections.append(block)
        else:
            sections.extend([f"### {phase}", block])

    return "\n\n".join(section for section in sections if section)


def _normalize_guided_injury_type(value: str | None) -> str:
    normalized = " ".join(str(value or "").strip().lower().replace("_", " ").replace("/", " ").split())
    if not normalized:
        return ""
    if "instability" in normalized or "giving way" in normalized:
        return "instability"
    return normalized


def _guided_area_matches_location(area: str, canonical_location: str) -> bool:
    """Whether a guided card's free-text *area* refers to *canonical_location*.

    Synonym- and token-aware: it canonicalizes the whole area and each word/2-word
    span ("hip flexor strain" -> "hip_flexor"/"hip") rather than doing a raw
    substring test, so "hip" matches the area "left hip" but never matches inside
    an unrelated word like "whiplash".
    """
    if not area or not canonical_location:
        return False
    if canonicalize_location(area) == canonical_location:
        return True
    tokens = re.findall(r"[a-z]+", area.lower())
    for span in (1, 2):
        for start in range(len(tokens) - span + 1):
            phrase = " ".join(tokens[start : start + span])
            if canonicalize_location(phrase) == canonical_location:
                return True
    return False


def _build_rehab_injury_string(context: PlanRuntimeContext) -> str:
    parsed_entries = context.training_context.parsed_injuries or []
    if not parsed_entries:
        return context.injuries_only_text

    # An explicit guided-card injury type (e.g. "instability / giving way") is a
    # user selection and outranks a type re-derived from the free-text phrase —
    # but only for the entry the card actually describes (matched by area), so a
    # first card's type is not smeared across unrelated injuries.
    context_guided_type = ""
    context_guided_area = ""
    guided_source = getattr(context.plan_input, "guided_injury", None)
    if guided_source is None:
        guided_list = getattr(context.plan_input, "guided_injuries", None) or []
        guided_source = guided_list[0] if guided_list else None
    if guided_source is not None:
        context_guided_type = _normalize_guided_injury_type(getattr(guided_source, "injury_type", None))
        context_guided_area = str(getattr(guided_source, "area", "") or "").strip().lower()

    phrases: list[str] = []
    for entry in parsed_entries:
        canonical_location = str(entry.get("canonical_location") or "").strip().lower()
        display_location = str(entry.get("display_location") or "").strip().lower()
        laterality = str(entry.get("laterality") or entry.get("side") or "").strip().lower()

        location = display_location or canonical_location
        if laterality and location and not location.startswith(f"{laterality} "):
            location = f"{laterality} {location}"
        elif not location:
            location = laterality

        raw_context = " ".join(
            str(part or "")
            for part in (
                entry.get("original_phrase"),
                entry.get("notes"),
                entry.get("display_location"),
                entry.get("canonical_location"),
            )
        ).lower()
        knee_movement_language = any(
            token in raw_context
            for token in (
                "went back",
                "bent back",
                "locked back",
                "overextend",
                "overextended",
                "hyperextend",
                "hyperextended",
            )
        )
        is_knee = canonical_location == "knee" or "knee" in display_location

        injury_type = _normalize_guided_injury_type(entry.get("injury_type"))
        guided_type = _normalize_guided_injury_type(entry.get("guided_source_injury_type") or entry.get("guided_injury_type"))
        if (
            not guided_type
            and context_guided_type
            and canonical_location
            and _guided_area_matches_location(context_guided_area, canonical_location)
        ):
            guided_type = context_guided_type
        if guided_type and injury_type in {"", "sprain", "unspecified", "pain", "soreness", "tightness", "stiffness"}:
            injury_type = guided_type
        if is_knee and knee_movement_language and injury_type in {"", "sprain", "unspecified", "pain", "soreness", "tightness", "stiffness"}:
            injury_type = "hyperextension"

        severity = str(entry.get("severity") or "").strip().lower()
        trend = str(entry.get("trend") or "").strip().lower()

        components = [part for part in (location, injury_type, severity, trend) if part]
        if components:
            phrases.append(" ".join(components))

    return "; ".join(phrases) if phrases else context.injuries_only_text


def _infer_rehab_day_type(*, phase: str) -> str | None:
    """Infer rehab day type only when reliable session context exists.

    Current phase-level rehab generation has no per-session allocation context,
    so return None to avoid pretending day-specific intent in output.
    """
    _ = phase
    return None


def _generate_rehab_support_bundle(
    context: PlanRuntimeContext,
    rehab_injury_string: str,
) -> tuple[dict[str, str], dict[str, str], str, bool, str, str]:
    rehab_blocks = {phase: "" for phase in PHASES}

    if context.phase_active("GPP"):
        rehab_blocks["GPP"], _ = generate_rehab_protocols(
            injury_string=rehab_injury_string,
            exercise_data=context.exercise_bank,
            current_phase="GPP",
            parsed_entries=context.training_context.parsed_injuries,
            day_type=_infer_rehab_day_type(phase="GPP"),
        )
        if rehab_blocks["GPP"].strip().startswith("**Red Flag Detected**"):
            rehab_blocks["SPP"] = rehab_blocks["GPP"]
            rehab_blocks["TAPER"] = rehab_blocks["GPP"]

    if not rehab_blocks["GPP"].strip().startswith("**Red Flag Detected**"):
        for phase in ("SPP", "TAPER"):
            if context.phase_active(phase):
                rehab_blocks[phase], _ = generate_rehab_protocols(
                    injury_string=rehab_injury_string,
                    exercise_data=context.exercise_bank,
                    current_phase=phase,
                    parsed_entries=context.training_context.parsed_injuries,
                    day_type=_infer_rehab_day_type(phase=phase),
                )

    guardrails = {
        phase: format_injury_guardrails(
            phase,
            context.training_context.injuries_raw_text,
            context.training_context.injury_restrictions,
            parsed_entries=context.training_context.parsed_injuries,
        )
        for phase in PHASES
    }
    has_injuries = bool(
        context.injuries_only_text
        or context.training_context.parsed_injuries
        or context.training_context.injury_restrictions
    )
    current_phase = _first_active_phase(context.phase_weeks)
    # Compute once; captured as a default argument by each builder lambda so
    # the dict is not re-created for every active phase.
    base_flags = context.training_context.to_flags()
    nutrition_block = _build_phase_support_block(
        context,
        lambda phase, bf=base_flags: generate_nutrition_block(flags={**bf, "phase": phase}),
    )
    support_notes = generate_support_notes(rehab_injury_string) if has_injuries else ""

    if context.apply_muay_thai_filters:
        rehab_blocks = {
            phase: _apply_muay_thai_filters(text, allow_grappling=False)
            for phase, text in rehab_blocks.items()
        }
        guardrails = {
            phase: _apply_muay_thai_filters(text, allow_grappling=False)
            for phase, text in guardrails.items()
        }
        support_notes = _apply_muay_thai_filters(support_notes, allow_grappling=False)

    rehab_blocks = {
        phase: sanitize_phase_text(text, context.sanitize_labels)
        for phase, text in rehab_blocks.items()
    }
    guardrails = {
        phase: sanitize_phase_text(text, context.sanitize_labels)
        for phase, text in guardrails.items()
    }
    support_notes = sanitize_phase_text(support_notes, context.sanitize_labels) if support_notes else ""

    return rehab_blocks, guardrails, support_notes, has_injuries, current_phase, nutrition_block


def _names_from_grouped(grouped: dict[str, list[dict]]) -> list[str]:
    return [
        drill.get("name")
        for drills in grouped.values()
        for drill in drills
        if drill.get("name")
    ]


def _apply_substitution_log(reason_log: dict[str, list[dict]], substitutions: list[dict], module: str) -> None:
    for substitution in substitutions:
        if substitution["module"] != module:
            continue
        phase_key = substitution["phase"]
        logs = reason_log.get(phase_key, [])
        logs = [entry for entry in logs if entry.get("name") != substitution["old"]]
        if substitution.get("new"):
            logs.append(
                {
                    "name": substitution["new"],
                    "reasons": {},
                    "explanation": "coach safety substitution",
                }
            )
        reason_log[phase_key] = logs


def generate_plan_blocks(
    *,
    context: PlanRuntimeContext,
    record_timing: TimingRecorder,
    logger: logging.Logger,
    progress_callback: ProgressCallback | None = None,
) -> PlanBlocksBundle:
    timer_start = perf_counter()
    phase_mindsets = _build_phase_mindsets(context.training_context)
    record_timing("mindset", timer_start)

    # Derived once per request: both the rehab/support bundle and the coach
    # review read the same normalised injury phrase list.
    rehab_injury_string = _build_rehab_injury_string(context)

    logger.info(
        "[stage] selection_ignore_restrictions=%s restrictions_present=%s restrictions_count=%d",
        context.selection_ignore_restrictions,
        bool(context.training_context.injury_restrictions),
        len(context.training_context.injury_restrictions or []),
    )

    strength_blocks, strength_reason_log = _run_stage1_module(
        module_name="strength",
        started_code="stage1_strength_block_started",
        finished_code="stage1_strength_block_finished",
        label_prefix="Stage 1 strength block",
        logger=logger,
        progress_callback=progress_callback,
        record_timing=record_timing,
        timing_label="strength",
        fn=lambda: _generate_strength_blocks(
            context,
            progress_callback=progress_callback,
        ),
    )
    strength_count = sum(
        len(strength_reason_log.get(phase, []) or []) for phase in PHASES
    )
    _emit_progress(
        progress_callback,
        "strength_scored",
        "Strength candidates scored",
        f"Selected {strength_count} strength exercise(s) across active phases.",
        count=strength_count,
    )

    conditioning_blocks, conditioning_reason_log = _run_stage1_module(
        module_name="conditioning",
        started_code="stage1_conditioning_block_started",
        finished_code="stage1_conditioning_block_finished",
        label_prefix="Stage 1 conditioning block",
        logger=logger,
        progress_callback=progress_callback,
        record_timing=record_timing,
        timing_label="conditioning",
        fn=lambda: _generate_conditioning_blocks(context, progress_callback=progress_callback),
    )
    conditioning_count = sum(
        len(conditioning_reason_log.get(phase, []) or []) for phase in PHASES
    )
    _emit_progress(
        progress_callback,
        "conditioning_scored",
        "Conditioning drills scored",
        f"Selected {conditioning_count} conditioning drill(s) across active phases.",
        count=conditioning_count,
    )

    (
        rehab_blocks,
        guardrails,
        support_notes,
        has_injuries,
        current_phase,
        nutrition_block,
    ) = _run_stage1_module(
        module_name="rehab_support_bundle",
        started_code="stage1_rehab_block_started",
        finished_code="stage1_rehab_block_finished",
        label_prefix="Stage 1 rehab block",
        logger=logger,
        progress_callback=progress_callback,
        record_timing=record_timing,
        timing_label="rehab_support_bundle",
        fn=lambda: _generate_rehab_support_bundle(context, rehab_injury_string),
    )
    if has_injuries:
        rehab_detail = "Rehab protocols and injury guardrails added to every active phase."
    else:
        rehab_detail = "No injury guardrails needed. Nutrition cues added."
    _emit_progress(
        progress_callback,
        "rehab_support_built",
        "Rehab & support drafted",
        rehab_detail,
        has_injuries=has_injuries,
    )

    _emit_progress(progress_callback, "stage1_mobility_block_started", "Stage 1 mobility block started")
    _emit_progress(progress_callback, "stage1_mobility_block_finished", "Stage 1 mobility block finished")
    _emit_progress(progress_callback, "stage1_nutrition_block_started", "Stage 1 nutrition block started")
    _emit_progress(progress_callback, "stage1_nutrition_block_finished", "Stage 1 nutrition block finished")
    _emit_progress(progress_callback, "stage1_weekly_schedule_started", "Stage 1 weekly schedule started")
    _emit_progress(progress_callback, "stage1_weekly_schedule_finished", "Stage 1 weekly schedule finished")

    coach_review_notes, strength_blocks, conditioning_blocks, substitutions = _run_stage1_module(
        module_name="coach_notes",
        started_code="stage1_coach_notes_started",
        finished_code="stage1_coach_notes_finished",
        label_prefix="Stage 1 coach notes",
        logger=logger,
        progress_callback=progress_callback,
        record_timing=record_timing,
        timing_label="coach_review",
        fn=lambda: run_coach_review(
            injury_string=rehab_injury_string,
            phase=current_phase,
            training_context=context.training_context.to_flags(),
            parsed_injury_entries=context.training_context.parsed_injuries,
            exercise_bank=context.exercise_bank,
            conditioning_banks=[context.conditioning_bank, context.style_conditioning_bank],
            strength_blocks=strength_blocks,
            conditioning_blocks=conditioning_blocks,
        ),
    )
    swap_count = len(substitutions or [])
    if swap_count:
        coach_detail = f"Coach review applied {swap_count} safety substitution(s)."
    else:
        coach_detail = "Coach review found no exercises to swap."
    _emit_progress(
        progress_callback,
        "coach_review_done",
        "Coach review pass complete",
        coach_detail,
        swap_count=swap_count,
    )

    _apply_substitution_log(strength_reason_log, substitutions, "Strength")
    _apply_substitution_log(conditioning_reason_log, substitutions, "Conditioning")

    for phase in PHASES:
        if strength_blocks.get(phase):
            strength_blocks[phase]["why_log"] = strength_reason_log.get(phase, [])
        if conditioning_blocks.get(phase):
            conditioning_blocks[phase]["why_log"] = conditioning_reason_log.get(phase, [])

    strength_names = {
        phase: [exercise["name"] for exercise in strength_blocks[phase].get("exercises", []) if exercise.get("name")]
        if strength_blocks.get(phase)
        else []
        for phase in PHASES
    }
    conditioning_names = {
        phase: _names_from_grouped(conditioning_blocks[phase].get("grouped_drills", {}))
        if conditioning_blocks.get(phase)
        else []
        for phase in PHASES
    }

    return PlanBlocksBundle(
        phase_mindsets=phase_mindsets,
        strength_blocks=strength_blocks,
        conditioning_blocks=conditioning_blocks,
        rehab_blocks=rehab_blocks,
        guardrails=guardrails,
        nutrition_block=nutrition_block,
        has_injuries=has_injuries,
        support_notes=support_notes,
        strength_reason_log=strength_reason_log,
        conditioning_reason_log=conditioning_reason_log,
        strength_names=strength_names,
        conditioning_names=conditioning_names,
        coach_review_notes=coach_review_notes,
        current_phase=current_phase,
    )
