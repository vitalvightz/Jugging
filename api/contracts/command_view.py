"""Normalized command-view read model (Block 4 §6 risk-watch + §7 command view).

Overview MUST NOT parse raw ``structured_plan``. It consumes this normalized,
derived read model instead, so Overview stays cheap to render and resilient to
``structured_plan`` schema changes.

The command view is **derived** state — built from the persisted plan, the
latest valid recommendation, completion records, and session data. It is not a
new source of truth. Missing inputs degrade to a clean empty state rather than
crashing.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, Field, PrivateAttr

from .completion import CompletionStatus, completion_status_of
from .readiness_message import (
    ConfidenceBand,
    confidence_band,
    confidence_note,
    context_labels,
    decision_sources,
    safety_checks,
    trigger_labels,
)
from .recommendation import RecommendationState, resolve_recommendation_state

# ---------------------------------------------------------------------------
# Risk watch (§6)
# ---------------------------------------------------------------------------

RiskCategory = Literal[
    "stop_red_flag",
    "active_injury_worse",
    "high_pain",
    "weight_cut",
    "fatigue",
    "reminder",
]

RiskTimeframe = Literal[
    "today",
    "last_session",
    "recent_sessions",
    "active",
]

# Lower number = higher priority (rendered first). See §6 priority order.
RISK_PRIORITY: dict[str, int] = {
    "stop_red_flag": 1,
    "active_injury_worse": 2,
    "high_pain": 3,
    "weight_cut": 4,
    "fatigue": 6,
    "reminder": 7,
}

# Default presentation per category: (icon, label, tone). Meaning never relies on
# colour alone — every risk carries icon + label + text + tone (§6 encoding).
_RISK_PRESENTATION: dict[str, tuple[str, str, str]] = {
    "stop_red_flag": ("octagon-x", "Stop", "stop"),
    "active_injury_worse": ("bandage", "Injury worsening", "stop"),
    "high_pain": ("alert-triangle", "High pain", "warning"),
    "weight_cut": ("scale", "Weight cut", "warning"),
    "fatigue": ("battery-low", "Fatigue", "caution"),
    "reminder": ("info", "Reminder", "info"),
}

VISIBLE_RISK_LIMIT = 2

_ACTIVE_INJURY_STATUSES = frozenset({"open", "monitoring"})
_SURFACE_CLASSES = frozenset(
    {
        "stable_surface",
        "surface_local_restriction",
        "surface_no_contact",
        "surface_medical_review",
    }
)


class RiskWatchItem(BaseModel):
    category: RiskCategory
    priority: int
    icon: str
    label: str
    text: str = ""
    tone: str
    timeframe: RiskTimeframe | None = None


def make_risk(
    category: RiskCategory,
    *,
    text: str = "",
    icon: str | None = None,
    label: str | None = None,
    tone: str | None = None,
    timeframe: RiskTimeframe | None = None,
) -> RiskWatchItem:
    """Build a risk-watch item with sensible per-category defaults."""
    default_icon, default_label, default_tone = _RISK_PRESENTATION[category]
    return RiskWatchItem(
        category=category,
        priority=RISK_PRIORITY[category],
        icon=icon or default_icon,
        label=label or default_label,
        text=text,
        tone=tone or default_tone,
        timeframe=timeframe,
    )


def _coerce_risk(risk: RiskWatchItem | Mapping[str, Any]) -> RiskWatchItem:
    if isinstance(risk, RiskWatchItem):
        return risk
    category = str(risk.get("category") or "reminder")
    if category not in RISK_PRIORITY:
        category = "reminder"
    return make_risk(
        category,  # type: ignore[arg-type]
        text=str(risk.get("text") or ""),
        icon=risk.get("icon"),
        label=risk.get("label"),
        tone=risk.get("tone"),
        timeframe=risk.get("timeframe"),
    )


def sort_risk_watch(
    risks: Sequence[RiskWatchItem | Mapping[str, Any]],
) -> list[RiskWatchItem]:
    """Order risks by priority (stable within a priority band)."""
    items = [_coerce_risk(r) for r in risks]
    return sorted(items, key=lambda item: item.priority)


def visible_risk_watch(
    risks: Sequence[RiskWatchItem],
    *,
    limit: int = VISIBLE_RISK_LIMIT,
) -> tuple[list[RiskWatchItem], int]:
    """Split an ordered risk list into (visible, overflow_count) for "+N more"."""
    visible = list(risks[:limit])
    overflow = max(0, len(risks) - len(visible))
    return visible, overflow


# ---------------------------------------------------------------------------
# Command view (§7)
# ---------------------------------------------------------------------------


class QuickAction(BaseModel):
    """A declarative navigation action — a route, never an inline mutation."""

    id: str
    label: str
    route: str


# ---------------------------------------------------------------------------
# Decision tier (STOP / PULL BACK / MODIFY / GREEN) — single authoritative source
#
# The Today banner and the risk-watch footer used to be derived independently on
# the client (the banner by substring-parsing the reason, the footer from the risk
# categories), so they could disagree — e.g. a PULL BACK banner over a STOP footer.
# This tier is computed once here from the recommendation + risks + injuries, and
# both surfaces render from it, so they can never contradict.
# ---------------------------------------------------------------------------

DecisionTier = Literal["stop", "pull_back", "modify", "green", "not_checked_in"]


class PrimarySafetyNotice(BaseModel):
    """Current, actionable safety guidance that can outrank session preview copy.

    This is deliberately separate from ``decision_tier``: a wound can need care
    now without blocking a future session. The server owns the notice and its
    priority so every client gets the same message hierarchy.
    """

    code: Literal[
        "skin_care",
        "skin_local_protection",
        "skin_no_contact",
        "skin_medical_review",
        "head_impact_no_contact",
    ]
    # Empty for a head-impact notice, which comes from a sparring report.
    injury_id: str
    chip: Literal["SKIN CARE", "CHECK"]
    title: str
    detail: str
    action: str
    tone: Literal["amber", "red"]

_TIER_RANK: dict[str, int] = {
    "not_checked_in": 0,
    "green": 1,
    "modify": 2,
    "pull_back": 3,
    "stop": 4,
}

# Stop-level risk categories: when present, the day is at minimum a STOP. NOTE:
# `stop_red_flag` is deliberately excluded — it is emitted for EVERY pull_back (it
# echoes the recommendation), so clamping on it would force a plain PULL BACK to
# read as STOP. `active_injury_worse` only fires for a severe / worse injury hold.
_STOP_RISK_CATEGORIES = frozenset({"active_injury_worse"})

# A pull_back recommendation is a hard STOP (rehab-only / no-training / injury-hold)
# rather than a plain pull-back when its first line (the title) is one of these, or
# its reason carries one of the specific stop markers. The markers are deliberately
# narrow — phrases that ONLY appear in stop copy — so a plain pull-back's generic
# "seek medical advice" safety line never falsely reads as a STOP. Every backend
# stop path already carries a distinguishing title; the markers are a backstop.
_STOP_REASON_TITLES = frozenset({"no training today", "rehab only today", "session blocked"})
_STOP_REASON_MARKERS = (
    "red flag",
    "injury is worse",
    "was reported worse",
    "pain is high",
    "head, neck, or nerve",
)


def _active_severe_injury_present(open_injuries: Sequence[Mapping[str, Any]] | None) -> bool:
    for injury in open_injuries or []:
        if (
            str(injury.get("severity") or "").lower() == "severe"
            and str(injury.get("status") or "").lower() in {"open", "monitoring"}
        ):
            return True
    return False


def resolve_decision_tier(
    *,
    recommendation_state: str,
    recommendation_reason: str | None,
    risks: Sequence[RiskWatchItem] | None = None,
    open_injuries: Sequence[Mapping[str, Any]] | None = None,
    injury_hold_exempt: bool = False,
) -> DecisionTier:
    """The single authoritative decision tier the whole Today UI renders from.

    Never weaker than the strongest risk in the footer, so the banner and footer
    cannot contradict. A severe active injury is always a STOP (injury hold), even
    before a check-in — unless ``injury_hold_exempt`` is set, which happens when
    today's scheduled session is a low-cost support / filler (mental cue, breathing
    or mobility reset) that the injury hold does not apply to.
    """
    if not injury_hold_exempt and _active_severe_injury_present(open_injuries):
        return "stop"

    state = str(recommendation_state or "not_checked_in")
    if state == "pull_back":
        reason = str(recommendation_reason or "")
        title = reason.splitlines()[0].strip().lower().rstrip(".!?") if reason else ""
        lowered = reason.lower()
        if title in _STOP_REASON_TITLES or any(marker in lowered for marker in _STOP_REASON_MARKERS):
            tier: DecisionTier = "stop"
        else:
            tier = "pull_back"
    elif state == "modify":
        tier = "modify"
    elif state == "train_as_planned":
        tier = "green"
    else:
        tier = "not_checked_in"

    # The tier can never be weaker than the strongest risk shown in the footer,
    # unless today's session is exempt from the injury hold (a safe filler).
    if not injury_hold_exempt and any((r.category in _STOP_RISK_CATEGORIES) for r in (risks or [])):
        if _TIER_RANK["stop"] > _TIER_RANK[tier]:
            tier = "stop"
    return tier


class CommandViewToday(BaseModel):
    training_day: str
    recommendation_state: RecommendationState = "not_checked_in"
    recommendation_reason: str | None = None
    # Authoritative decision tier (STOP/PULL BACK/MODIFY/GREEN). Both the banner and
    # the risk-watch footer render from this so they cannot disagree.
    decision_tier: DecisionTier = "not_checked_in"
    # True when today's scheduled session is a low-cost support / filler that an
    # injury hold does not apply to, so the UI must not block it for an injury.
    injury_hold_exempt: bool = False
    # The explanation, split by the role each part played. Computed on the
    # backend from the engine's own trigger codes (like decision_tier) so it can
    # never drift from the decision it explains. All empty until check-in.
    #
    # Triggers are what changed about the ATHLETE and set the decision. Context
    # is the camp around it, which only changes how cautious that decision is.
    # Holding them apart is what stops "Fight week" reading as a peer of
    # "High pain".
    recommendation_trigger_labels: list[str] = Field(default_factory=list)
    recommendation_context_labels: list[str] = Field(default_factory=list)
    # Safety checks are the third role: things the engine ASSESSED, which mostly
    # changed nothing. A stable skin injury lives here — never in the trigger
    # list — so the card can show it was considered without implying it reduced
    # the session. Structured ({code, label, result, result_label}) so the UI
    # never reads prose to tell a check from a cause.
    recommendation_safety_checks: list[dict[str, str]] = Field(default_factory=list)
    recommendation_sources: list[str] = Field(default_factory=list)
    # How much data the decision rests on, and what it was missing. This is data
    # completeness, NOT predictive accuracy — see readiness_message.
    #
    # ``None`` when the decision carries no trigger codes to judge it by, which
    # covers both "no decision yet" and a recommendation stored before the engine
    # recorded triggers. Deliberately not defaulted to "high": absent evidence is
    # not evidence of completeness, and asserting a band there would put a
    # confident claim on the one decision nothing is known about.
    recommendation_confidence: ConfidenceBand | None = None
    recommendation_confidence_note: str = ""
    # A current safety instruction is a separate authority from the scheduled
    # session. It may lead the UI while ``session_scope == "next"`` without
    # implying that the future session has been blocked or modified.
    primary_safety_notice: PrimarySafetyNotice | None = None
    warnings: list[str] = Field(default_factory=list)
    next_session: dict[str, Any] = Field(default_factory=dict)
    session_scope: Literal["today", "next", "none"] = "none"
    session_label: str = ""
    completion_status: CompletionStatus = "not_started"


class CommandView(BaseModel):
    active_plan: dict[str, Any] = Field(default_factory=dict)
    today: CommandViewToday
    risk_watch: list[RiskWatchItem] = Field(default_factory=list)
    # Open/monitoring injury_flags, normalized for the Today injury check-in to
    # prefill against (never the raw plan). Empty when nothing is being tracked.
    open_injuries: list[dict[str, Any]] = Field(default_factory=list)
    live_prescription: dict[str, Any] | None = None
    # The one answer to "is today's session blocked?" ({held, reason, source}).
    # The web and the completion gate both read it; nothing else re-derives it.
    session_hold: dict[str, Any] | None = None
    # Existing ExerciseMedia values are attached by the presentation service.
    exercise_media: dict[str, Any] = Field(default_factory=dict)
    effective_clinician_clearance: dict[str, Any] | None = None
    delayed_rehab_prompts: list[dict[str, Any]] = Field(default_factory=list)
    week_summary: dict[str, Any] = Field(default_factory=dict)
    quick_actions: list[QuickAction] = Field(default_factory=list)
    # The stop tier came from an injury hold, not a readiness red flag.
    _injury_hold_stop: bool = PrivateAttr(default=False)


def session_is_today(view: Any) -> bool:
    """Whether the session the command view carries is scheduled for today.

    ``build_today_command_view`` is the only place session timing is derived —
    it owns rest days, multi-session days, completed sessions and the roll to
    tomorrow. Everything downstream (XP, notifications, the app) asks this
    instead of re-deriving the answer from dates, relations or plan rows, so a
    surface can never disagree with Today about what day a session falls on.

    Accepts anything command-view shaped so the loosely-typed callers share this
    one definition rather than keeping a second copy of the comparison.
    """
    today = getattr(view, "today", None)
    return str(getattr(today, "session_scope", "") or "") == "today"


_PLAN_IDENTITY_FIELDS = ("status", "phase", "fight_date", "camp_type", "event_name", "target_weight_kg")


def _plan_identity(plan: Mapping[str, Any] | None) -> dict[str, Any]:
    """Minimal plan identity/summary — never the full structured plan."""
    if not plan:
        return {}
    identity: dict[str, Any] = {
        field: plan[field]
        for field in _PLAN_IDENTITY_FIELDS
        if plan.get(field) not in (None, "")
    }
    plan_id = str(plan.get("id") or plan.get("plan_id") or "").strip()
    if plan_id:
        identity["id"] = plan_id
    plan_name = str(plan.get("name") or plan.get("plan_name") or "").strip()
    if plan_name:
        identity["name"] = plan_name
    # A plan with no recognisable identity fields is treated as "no plan".
    return identity


def _quick_actions(active_plan_id: str | None) -> list[QuickAction]:
    if not active_plan_id:
        return [
            QuickAction(
                id="complete_intake",
                label="Complete Intake / Create Plan",
                route="/intake",
            )
        ]
    return [
        QuickAction(id="open_today", label="Open Today", route="/today"),
        QuickAction(id="view_plan", label="View Plan", route=f"/plans/{active_plan_id}"),
    ]

def _as_iso(day: date | str) -> str:
    if hasattr(day, "date"):
        return day.date().isoformat()  # type: ignore[union-attr]
    if isinstance(day, date):
        return day.isoformat()
    return str(day or "").strip()


def _injury_name(injury: Mapping[str, Any]) -> str:
    """Use the server-normalized injury label, else build the same clean label.

    The stored description carries internal markers such as
    ``[training_impact:not_limiting]``; it is never shown as-is.
    """
    raw = str(injury.get("label") or "").strip()
    if not raw:
        from api.contracts.injury_checkin import build_injury_label

        raw = build_injury_label(injury.get("body_area"), injury.get("description")).strip() or "Injury"
    return raw[:1].upper() + raw[1:]


def _active_injury_rows(
    open_injuries: Sequence[Mapping[str, Any]] | None,
) -> list[Mapping[str, Any]]:
    return [
        injury
        for injury in (open_injuries or [])
        if str(injury.get("status") or "").strip().lower() in _ACTIVE_INJURY_STATUSES
    ]


_SURFACE_NOTICE_PRIORITY = {
    "stable_surface": 1,
    "surface_local_restriction": 2,
    "surface_no_contact": 3,
    "surface_medical_review": 4,
}


def _natural_injury_name(label: str) -> str:
    first_word = label.split(maxsplit=1)[0]
    if len(first_word) > 1 and first_word.isupper():
        return label
    return label[:1].lower() + label[1:]


def _primary_safety_notice(
    open_injuries: Sequence[Mapping[str, Any]] | None,
    *,
    head_impact_today: bool = False,
) -> PrimarySafetyNotice | None:
    """Return the strongest current safety instruction, if one exists.

    Session timing is intentionally absent from this selector. A future-session
    preview is planning information; an active wound instruction is current
    safety information and therefore remains eligible to lead the command card.
    A head-impact report from today outranks every skin instruction, so no
    earlier positive message stays in the lead after it. It is a no-contact
    instruction, never a diagnosis or a clearance.
    """
    if head_impact_today:
        return PrimarySafetyNotice(
            code="head_impact_no_contact",
            injury_id="",
            chip="CHECK",
            title="No more contact today",
            action="You reported being rocked or dropped. No more contact today, and tell your coach.",
            detail=(
                "If you get a headache, dizziness, nausea, confusion, memory problems or "
                "blurred vision, stop training and get checked by a medical professional."
            ),
            tone="red",
        )
    surface_injuries = [
        injury
        for injury in _active_injury_rows(open_injuries)
        if str(injury.get("surface_class") or "") in _SURFACE_NOTICE_PRIORITY
    ]
    if not surface_injuries:
        return None

    injury = max(
        surface_injuries,
        key=lambda item: _SURFACE_NOTICE_PRIORITY[
            str(item.get("surface_class") or "stable_surface")
        ],
    )
    surface_class = str(injury.get("surface_class") or "stable_surface")
    injury_id = str(injury.get("id") or "").strip()
    label = _injury_name(injury)
    natural_label = _natural_injury_name(label)

    if surface_class == "surface_medical_review":
        return PrimarySafetyNotice(
            code="skin_medical_review",
            injury_id=injury_id,
            chip="CHECK",
            title="Check before training",
            action=f"Get the {natural_label} checked before training.",
            detail="Keep it clean and covered, and keep contact or rubbing off it.",
            tone="red",
        )
    if surface_class == "surface_no_contact":
        return PrimarySafetyNotice(
            code="skin_no_contact",
            injury_id=injury_id,
            chip="SKIN CARE",
            title="Contact restriction",
            action=(
                f"Keep contact off the {natural_label} until the skin is closed and coverable."
            ),
            detail="Keep it clean and covered while it heals.",
            tone="amber",
        )
    if surface_class == "surface_local_restriction":
        return PrimarySafetyNotice(
            code="skin_local_protection",
            injury_id=injury_id,
            chip="SKIN CARE",
            title="Protect the area",
            action=f"Protect the {natural_label} from rubbing or contact.",
            detail="Keep it clean and covered while it heals.",
            tone="amber",
        )
    return PrimarySafetyNotice(
        code="skin_care",
        injury_id=injury_id,
        chip="SKIN CARE",
        title="Skin care active",
        action=f"Keep the {natural_label} clean and covered.",
        detail="Stop if it opens, reopens, bleeds, or rubs.",
        tone="amber",
    )


def _specific_trigger_labels(
    triggers: Sequence[str],
    open_injuries: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    """Replace generic injury contributors with the exact tracked injuries.

    The engine still owns the structured trigger codes. This presentation layer
    joins those codes to the already-normalized injury records, so the client is
    told *which* injury contributed and whether it is worsening, severe, needs
    review, or is relevant to hard work. It never parses recommendation prose.
    """
    base_labels = list(trigger_labels(triggers))
    if not base_labels:
        return []

    codes = {str(trigger).strip() for trigger in triggers}
    active_by_id = {
        str(injury.get("id") or "").strip(): injury
        for injury in _active_injury_rows(open_injuries)
        if str(injury.get("id") or "").strip()
    }
    replacements: dict[str, list[str]] = {}

    if "active_injury_worse" in codes:
        responsible_ids = [
            code.split(":", 1)[1]
            for prefix in ("injury_hold:", "active_injury_worse:")
            for code in triggers
            if str(code).startswith(prefix) and str(code).split(":", 1)[1]
        ]
        candidates = [
            active_by_id[flag_id]
            for flag_id in dict.fromkeys(responsible_ids)
            if flag_id in active_by_id
        ]
        if candidates:
            replacements["Injury getting worse"] = [
                (
                    f"{_injury_name(injury)} — needs medical review"
                    if str(injury.get("surface_class") or "") == "surface_medical_review"
                    else f"{_injury_name(injury)} — getting worse"
                    if str(injury.get("latest_reported_status") or "").strip().lower()
                    == "worse"
                    else f"{_injury_name(injury)} — severe"
                )
                for injury in candidates
            ]

    if {"active_injury_restriction", "tracked_injury_high_risk_session"} & codes:
        responsible_ids = [
            code.split(":", 1)[1]
            for prefix in (
                "active_injury_restriction:",
                "tracked_injury_high_risk_session:",
            )
            for code in triggers
            if str(code).startswith(prefix) and str(code).split(":", 1)[1]
        ]
        load_relevant = [
            active_by_id[flag_id]
            for flag_id in dict.fromkeys(responsible_ids)
            if flag_id in active_by_id
        ]
        reason = (
            "active for hard session"
            if "tracked_injury_high_risk_session" in codes
            else "restricts today's training"
        )
        if load_relevant:
            replacements["Active injury"] = [
                f"{_injury_name(injury)} — {reason}" for injury in load_relevant
            ]

    if "surface_injury_medical_review" in codes:
        responsible_ids = [
            code.split(":", 1)[1]
            for code in triggers
            if str(code).startswith("surface_injury_medical_review:")
            and str(code).split(":", 1)[1]
        ]
        surface_reviews = [
            active_by_id[flag_id]
            for flag_id in dict.fromkeys(responsible_ids)
            if flag_id in active_by_id
        ]
        if surface_reviews:
            replacements["Skin injury needs review"] = [
                f"{_injury_name(injury)} — needs medical review"
                for injury in surface_reviews
            ]

    resolved: list[str] = []
    for label in base_labels:
        specifics = replacements.get(label) or [label]
        for specific in specifics:
            if specific not in resolved:
                resolved.append(specific)
            if len(resolved) >= 3:
                return resolved
    return resolved


def build_command_view(
    *,
    current_training_day: date | str,
    plan: Mapping[str, Any] | None = None,
    recommendation: Mapping[str, Any] | None = None,
    completion: Mapping[str, Any] | None = None,
    next_session: Mapping[str, Any] | None = None,
    session_scope: Literal["today", "next", "none"] | None = None,
    warnings: Sequence[str] | None = None,
    risks: Sequence[RiskWatchItem | Mapping[str, Any]] | None = None,
    open_injuries: Sequence[Mapping[str, Any]] | None = None,
    week_summary: Mapping[str, Any] | None = None,
    injury_hold_exempt: bool = False,
    head_impact_today: bool = False,
) -> CommandView:
    """Assemble the normalized command view from derived inputs.

    Every argument is optional/nullable: a missing plan yields the empty state
    with an Intake CTA, a missing/expired recommendation yields
    ``not_checked_in``, and missing session/plan data yields clean empty objects
    rather than reaching into ``structured_plan``.
    """
    training_day = _as_iso(current_training_day)
    active_plan = _plan_identity(plan)

    rec_view = resolve_recommendation_state(
        recommendation, current_training_day=training_day
    )
    resolved_session_scope: Literal["today", "next", "none"] = (
        session_scope if session_scope is not None else ("next" if next_session else "none")
    )
    session_label = {
        "today": "Today's session",
        "next": "Next session",
        "none": "",
    }[resolved_session_scope]

    sorted_risks = sort_risk_watch(risks or [])
    today = CommandViewToday(
        training_day=training_day,
        recommendation_state=rec_view.state,
        recommendation_reason=rec_view.reason,
        decision_tier=resolve_decision_tier(
            recommendation_state=rec_view.state,
            recommendation_reason=rec_view.reason,
            risks=sorted_risks,
            open_injuries=open_injuries,
            injury_hold_exempt=injury_hold_exempt,
        ),
        injury_hold_exempt=injury_hold_exempt,
        recommendation_trigger_labels=_specific_trigger_labels(
            rec_view.triggers, open_injuries
        ),
        recommendation_context_labels=list(context_labels(rec_view.triggers)),
        recommendation_safety_checks=[dict(check) for check in safety_checks(rec_view.triggers)],
        # Sources come from the decision's own trigger codes. An open injury is
        # named only when it ACTED — being tracked is not being used, and a
        # stable skin injury stays in the safety checks where it belongs.
        recommendation_sources=(
            list(decision_sources(rec_view.triggers)) if rec_view.triggers else []
        ),
        recommendation_confidence=(
            confidence_band(rec_view.triggers) if rec_view.triggers else None
        ),
        recommendation_confidence_note=confidence_note(rec_view.triggers),
        primary_safety_notice=_primary_safety_notice(open_injuries, head_impact_today=head_impact_today),
        warnings=[str(warning) for warning in (warnings or []) if str(warning).strip()],
        next_session=dict(next_session) if next_session else {},
        session_scope=resolved_session_scope,
        session_label=session_label,
        completion_status=completion_status_of(completion),
    )

    return CommandView(
        active_plan=active_plan,
        today=today,
        risk_watch=sorted_risks,
        open_injuries=[dict(injury) for injury in (open_injuries or [])],
        week_summary=dict(week_summary) if week_summary else {},
        quick_actions=_quick_actions(active_plan.get("id")),
    )
