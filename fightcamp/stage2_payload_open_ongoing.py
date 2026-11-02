from __future__ import annotations

from typing import Any

from .mental_training_policy import mental_training_policy
from .tactical_watch_library import (
    TacticalWatchBankExhausted,
    build_watch_display_text,
    extract_tactical_style,
    select_tactical_watch,
    watch_metadata,
)


_OPEN_PLAN_STRUCTURE = [
    "Immediate Coach Summary",
    "Current Training Rules",
    "Weekly Rhythm",
    "Session Cards",
    "4-Week Development Block",
    "Progression Rules",
    "Priority Hierarchy",
    "Adjustment Rules",
    "Rehab / Red Flags",
    "4-Week Reassessment Gate",
]

_FORBIDDEN_TERMS = [
    "GPP",
    "SPP",
    "TAPER",
    "D-",
    "fight week",
    "fight-day",
    "countdown",
    "taper week",
]


def _uses_open_ongoing_payload(athlete_model: dict[str, Any]) -> bool:
    if not isinstance(athlete_model, dict):
        return False

    days_until_fight = athlete_model.get("days_until_fight")
    fight_date = athlete_model.get("fight_date")
    next_fight_date = athlete_model.get("next_fight_date")

    if isinstance(days_until_fight, int):
        return False
    try:
        int(days_until_fight)
        return False
    except (TypeError, ValueError):
        pass

    if fight_date:
        return False
    if next_fight_date:
        return False

    no_scheduled_fight = athlete_model.get("no_scheduled_fight")
    if isinstance(no_scheduled_fight, bool):
        return no_scheduled_fight

    return True


# An open plan has no camp phase, so every Tactical Watch it renders is drawn
# from the development-oriented (GPP) bank. The phase token itself is internal
# and is stripped before the watch reaches the payload: "GPP" is a forbidden
# athlete-facing term in this render mode.
_OPEN_PLAN_WATCH_PHASE = "GPP"
_OPEN_PLAN_WATCH_WEEKS = 4
_TACTICAL_WATCH_LABEL = "Tactical Focus"


def _open_plan_watch_entry(watch: Any, week: int) -> dict[str, Any]:
    """Athlete-facing Tactical Watch card for one development-block week.

    Only bank content reaches the payload. The internal selection metadata a
    camp role carries (style family, phase, sport ownership, tags) is dropped:
    an open plan must not surface phase vocabulary, and the finalizer has no
    use for selection internals it is forbidden to print.
    """
    metadata = watch_metadata(watch)
    block = dict(metadata.get("tactical_watch") or {})
    key = str(block.pop("key", "") or "")
    for internal in ("style", "phase", "sports", "fallback_reason", "competitive_maturity"):
        block.pop(internal, None)
    # ``context`` is the bank's phase framing ("Early-camp pattern recognition
    # for a counter striker"). An open plan has no camp, so that one line is
    # dropped from both the structured mindset and the rendered body; every
    # other athlete-facing string stays exactly as the bank authored it.
    mindset = dict(block.get("mindset") or {})
    mindset.pop("context", None)
    block["mindset"] = mindset
    display_text = "\n".join(
        line
        for line in build_watch_display_text(watch).split("\n")
        if not line.strip().startswith("Purpose:")
    )
    return {
        "week": week,
        "label": _TACTICAL_WATCH_LABEL,
        "name": block.get("name") or watch.name,
        "duration_min": watch.duration_minutes,
        "zero_physical_load": True,
        # Internal identity, kept for dedupe and telemetry only. Bank keys carry
        # the phase token, which is a forbidden athlete-facing term here.
        "tactical_watch_key": key,
        # The server owns this body exactly as it does in a dated camp; the
        # finalizer reproduces it and never authors or re-doses it.
        "display_text": display_text,
        "tactical_watch": block,
    }


def build_open_plan_tactical_watches(athlete_model: dict[str, Any]) -> list[dict[str, Any]]:
    """Select distinct bank watches at the shared default/prioritised cadence."""
    model = athlete_model if isinstance(athlete_model, dict) else {}
    style = extract_tactical_style(model)
    used_keys: set[str] = set()
    entries: list[dict[str, Any]] = []
    step = mental_training_policy(model).analyse_spacing_days // 7
    for week in range(1, _OPEN_PLAN_WATCH_WEEKS + 1, step):
        try:
            watch = select_tactical_watch(style, _OPEN_PLAN_WATCH_PHASE, used_keys)
        except TacticalWatchBankExhausted:
            # A narrow bank is not a generation failure: render the weeks the
            # bank can actually cover rather than repeating or inventing one.
            break
        used_keys.add(watch.key)
        entries.append(_open_plan_watch_entry(watch, week))
    return entries


def _build_open_plan_tactical_watch_spec(athlete_model: dict[str, Any]) -> dict[str, Any]:
    """The plan's complete Tactical Watch set, plus how many weeks it covers.

    A narrow style/sport bank can run out before week 4, so the rotation is not
    guaranteed to be four entries long. ``weeks_covered`` states the real length
    alongside the rotation so the render contract can be driven by what exists
    rather than by an assumed four-week shape.
    """
    rotation = build_open_plan_tactical_watches(athlete_model)
    return {
        "label": _TACTICAL_WATCH_LABEL,
        "placement": "session_cards",
        "cadence": f"one every {mental_training_policy(athlete_model).analyse_spacing_days} days of the 4-week development block",
        "attach_to_existing_training": True,
        "zero_physical_load": True,
        "weeks_covered": len(rotation),
        "weekly_rotation": rotation,
    }


def build_open_ongoing_payload(*, athlete_model: dict[str, Any]) -> dict[str, Any]:
    return {
        "payload_mode": "open_ongoing_payload",
        "render_mode": "open_ongoing_system",
        "open_plan_spec": {
            "plan_type": "open_ongoing_system",
            "structure": list(_OPEN_PLAN_STRUCTURE),
            "forbidden_terms": list(_FORBIDDEN_TERMS),
            "render_rules": [
                "Render a renewable open training system, not a fight camp.",
                "Use the exact section order from open_plan_spec.structure.",
                "Render one Weekly Rhythm section only.",
                "Render app-owned days as session cards using Objective/Main work/Fallback/Rehab / mobility/Coach note/Stop rule.",
                "Name one exercise per main-work line. A choice such as \"A or B\" is never the exercise name: pick one and put the alternative on that line's Easier/Swap line. Only a deliberate superset or contrast pair may pair two movements, written as \"A + B\" with the dose for the pair.",
                "Give each programmed exercise that advances in the block a Progress: line in its own units, stepped from its own dose, using the variable that suits the work. Omit the line where the work should not advance, and never cite a load the plan has not prescribed.",
                "Never number the weeks inside a Progress: line. The athlete sees one week at a time, so write the single step (\"Progress: add one more burst\") and put the block's easy week on its own Deload: line.",
                "Do not render GPP/SPP/TAPER headings, countdown labels, D-day labels, fight-week rules, or fight-day protocol.",
                "Use 4-Week Development Block + 4-Week Reassessment Gate instead of fixed phase blocks.",
                "Preserve safety, medical stop rules, weight-cut adjustments, and fatigue adjustments.",
                "Do not expose internal scoring, candidate pools, raw tags, or unused options.",
                "tactical_watch.weekly_rotation is the complete Tactical Watch set for this plan. Render only the entries it contains and never invent, repeat or fill a week it does not cover.",
                "Render the first weekly_rotation entry as its own session card inside Session Cards, reproducing its display_text exactly.",
                "Name each remaining weekly_rotation entry against its own week inside the 4-Week Development Block so the athlete can see what changes each week.",
                "Attach each Tactical Watch to an existing physical training day in its selected week; never create a standalone mental day or fill calendar space. Maximum one mental task per day.",
                "A Tactical Watch is zero physical load: it never replaces a training session and never counts toward weekly training frequency.",
                "Never print Tactical Watch keys, style families, phase tokens, or sport ownership.",
            ],
            "weekly_template": {
                "training_days": athlete_model.get("training_days") or [],
                "hard_sparring_days": athlete_model.get("hard_sparring_days") or [],
                "support_work_days": athlete_model.get("support_work_days") or [],
                "coach_owned_days": {
                    "technical_skill_days": athlete_model.get("technical_skill_days") or [],
                    "hard_sparring_days": athlete_model.get("hard_sparring_days") or [],
                    "support_work_days": athlete_model.get("support_work_days") or [],
                },
            },
            "development_block": {
                "week_1": "Baseline and technical consistency",
                "week_2": "Small progression",
                "week_3": "Highest controlled week",
                "week_4": "Deload and reassess",
            },
            "tactical_watch": _build_open_plan_tactical_watch_spec(athlete_model),
            "priority_hierarchy": [
                "Protect restrictions and injury constraints first",
                "Preserve declared hard sparring and contact schedule",
                "Keep one main adaptation focus + one limiter focus",
                "Use support work only after anchor quality is protected",
            ],
            "adjustment_rules": [
                "If symptoms or red flags rise, reduce optional conditioning first.",
                "If fatigue stays high, trim volume before trimming key anchor quality.",
                "If weight-cut pressure rises, preserve recovery margin and remove low-priority extras.",
            ],
        },
    }
