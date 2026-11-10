"""Structured training plan schema for Unlxck.

This module defines the machine-readable plan format that replaces the raw AI
text blob. A plan is modelled as a countdown-driven, phase-aware hierarchy:

    plan -> weeks[] -> days[] -> sessions[] -> blocks[]

Design rules baked into the schema:

* Machine-readable prescription (load / effort / tempo / durations) lives in
  dedicated value objects, never inside free strings like ``"85%"``. The
  frontend renders a separate ``display`` / ``display_text`` field instead of
  raw pseudo-code.
* Readiness is self-report only. There are deliberately no biometric fields
  (HRV, CNS%, WHOOP-style recovery scores).
* Weight-cut guidance is represented as warnings requiring qualified
  supervision, never as direct acute-cut instructions.
* The root always carries ``raw_markdown_fallback`` so a failed structured
  generation never leaves the athlete with a blank plan.

Conventions follow ``api/models.py``: Pydantic v2, ``Literal`` aliases for
constrained strings (no ``Enum`` classes), and ``from __future__`` annotations.
"""
from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_serializer

# Bump this whenever the structured shape changes in a backward-incompatible
# way. Stored plans keep the version they were generated with so the renderer
# can branch on it.
SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Constrained-string aliases (Literal, matching api/models.py convention)
# ---------------------------------------------------------------------------

UnitsSystem = Literal["metric", "imperial"]
PlanType = Literal[
    "fight_camp",
    "open_ongoing_system",
    "explosive_athlete",
    "match_week",
    "reintegration",
    "general_performance",
]
PlanStatus = Literal["draft", "active", "completed", "archived"]
EventType = Literal["fight", "match", "trial", "camp", "none"]
Severity = Literal["green", "amber", "red"]
RedFlagWhen = Literal[
    "morning_check_in",
    "pre_session",
    "during_session",
    "post_session",
    "next_day",
]
PhaseLabel = Literal["GPP", "SPP", "TAPER", "FIGHT_WEEK", "REINTEGRATION"]
LoadFocusValue = Literal[
    "low",
    "moderate",
    "high",
    "reduced",
    "peak",
    "build",
    "maintain",
    "unload",
]
WeekType = Literal[
    "build",
    "stabilise",
    "deload",
    "specific_peak",
    "taper",
    "fight_week",
    "reintegration",
]
DayType = Literal[
    "high",
    "moderate",
    "low",
    "recovery",
    "rest",
    "competition",
    "travel",
    "reintegration",
]
# Self-report readiness vocabulary, shared by the today card and the daily
# check-in decision.
ReadinessStatus = Literal["train_as_planned", "modify", "pull_back", "unavailable"]
SessionType = Literal[
    "strength_power",
    "conditioning",
    "skill",
    "sparring",
    "primer",
    "recovery",
    "rehab",
    "fight_or_match",
    "mixed",
]
CompletionStatus = Literal["not_started", "done", "modified", "skipped"]
BlockType = Literal[
    "preparation",
    "mobility_activation",
    "plyometric_power",
    "speed",
    "strength",
    "strength_speed",
    "accessory",
    "conditioning",
    "skill",
    "sparring",
    "cooldown_recovery",
    "nutrition",
    "mindset",
    "rehab",
]
LoadMethod = Literal[
    "percentage",
    "absolute",
    "bodyweight",
    "band",
    "rpe",
    "rir",
    "velocity",
    "relative",
    "other",
]
EffortMethod = Literal[
    "RPE",
    "RIR",
    "intent",
    "velocity",
    "heart_rate_zone",
    "pace",
    "max_effort_percent",
]
RiskLevel = Literal["none", "green", "amber", "red"]
PlanNoteCategory = Literal[
    "weight_cut",
    "injury",
    "nutrition",
    "training",
    "recovery",
    "general",
]


# ---------------------------------------------------------------------------
# Machine-readable value objects (Section O)
# ---------------------------------------------------------------------------


class MeasuredValue(BaseModel):
    """A scalar quantity with an explicit unit (duration, distance, mass...)."""

    value: float
    unit: str


class LoadPrescription(BaseModel):
    """How much to load a block, kept machine-readable.

    Example::

        {"method": "percentage", "value": 85, "unit": "percent",
         "ref": "1RM", "display": "85% 1RM"}
    """

    method: LoadMethod
    value: float
    unit: str
    ref: str | None = None
    display: str | None = None


class EffortPrescription(BaseModel):
    """Target effort expressed via a named method (RPE, RIR, intent...)."""

    method: EffortMethod
    value: float | str
    scale: str | None = None


class TempoPrescription(BaseModel):
    """Lift tempo phases. Values are seconds or a cue such as ``"X"``."""

    eccentric: int | str | None = None
    pause_bottom: int | str | None = None
    concentric: int | str | None = None
    pause_top: int | str | None = None


class BlockComponent(BaseModel):
    """One movement inside a cluster block, e.g. each half of a contrast pair.

    A heavy lift -> jump pair is one block with one round count and one rest,
    but each movement has its own reps and loading. Server-owned: lifted from
    the planner dose or the exact source line, never authored by the model.
    """

    name: str
    reps: int | str | None = None
    load: LoadPrescription | None = None
    effort: EffortPrescription | None = None


# ---------------------------------------------------------------------------
# Shared anchors / rules
# ---------------------------------------------------------------------------


class MindsetAnchor(BaseModel):
    """Session/day-level psychological framing (Section L)."""

    intent: str
    focus_cue: str
    reset_cue: str
    confidence_anchor: str | None = None
    context: str | None = None


class RedFlagRule(BaseModel):
    """A safety rule (Section F).

    Machine-readable fields (``metric``, ``operator``, ``threshold``,
    ``logic``) are stored separately from ``display_text``. The frontend renders
    ``display_text`` and must never surface raw pseudo-code such as
    ``"achilles_pain >= 6"``.
    """

    # Rendering and safety semantics do not depend on a model-authored id.  A
    # missing identifier must not discard an otherwise valid athlete card.
    rule_id: str | None = None
    metric: str | None = None
    metric_group: str | None = None
    when: RedFlagWhen
    operator: str | None = None
    threshold: float | None = None
    logic: str | None = None
    severity: Severity
    applies_to: list[str] = Field(default_factory=list)
    display_text: str
    action: str
    replacement_session_type: SessionType | None = None
    affected_blocks: list[str] | None = None
    needs_human_review: bool = False


class PlanNote(BaseModel):
    """A short, plan-level "active note" / non-negotiable reminder.

    Carries the header ``Active notes`` and footer reminders (weight cut,
    injury, nutrition, general) that live outside any week so they are not lost
    in the structured view. Weight-cut notes follow the same safety rule as
    :class:`WeightCutWarning`: a risk requiring qualified supervision, never
    direct acute-cut instructions.
    """

    category: PlanNoteCategory = "general"
    label: str | None = None
    text: str


class CountdownLabel(BaseModel):
    """Maps a calendar date to a countdown label (Section E).

    ``label`` examples: ``D-28``, ``D-14``, ``D-7``, ``D-1``, ``D0``, ``D+1``.
    """

    date: str
    days_to_event: int
    label: str
    anchor: str


# ---------------------------------------------------------------------------
# Block / session / day / week hierarchy
# ---------------------------------------------------------------------------


def _omit_none(data: Any, field: str) -> Any:
    """Override provenance exists only where a change applies; elsewhere every
    serialized card keeps exactly the shape it had before the field existed."""
    if isinstance(data, dict) and data.get(field) is None:
        data.pop(field, None)
    return data


class SessionBlock(BaseModel):
    """An executable unit inside a session (Section N)."""

    # Optional presentation identity.  The frontend already uses a positional
    # key when this is absent; training content must not fail validation for it.
    block_id: str | None = None
    block_type: BlockType
    contact_level: Literal["none", "controlled", "full", "unknown"] | None = None
    mechanical_load_regions: list[str] | None = None
    tags: list[str] | None = None
    display_name: str
    category: str | None = None
    order_index: int | None = None
    duration: MeasuredValue | None = None
    sets: int | None = None
    reps: int | str | None = None
    load: LoadPrescription | None = None
    effort: EffortPrescription | None = None
    tempo: TempoPrescription | None = None
    rest: MeasuredValue | None = None
    # Qualitative rest the dose states without a number ("Full rest between
    # holds"). Only set when ``rest`` is absent; a value is never invented.
    rest_note: str | None = None
    work: MeasuredValue | None = None
    distance: MeasuredValue | None = None
    rounds: int | None = None
    # Cluster structure (contrast pairs): each movement's own reps and load,
    # sharing the block's ``rounds`` and ``rest``. Absent for single movements.
    components: list[BlockComponent] | None = None
    intensity: str | None = None
    energy_system: str | None = None
    impact_level: str | None = None
    purpose: str | None = None
    why_today: str | None = None
    coaching_cues: list[str] = Field(default_factory=list)
    execution_instructions: str | None = None
    regression_options: list[str] = Field(default_factory=list)
    progression_rule: str | None = None
    # Open/ongoing plans only: the block's own week-4 deload or reassessment
    # instruction. Dated camps prescribe their taper week by week, so they
    # leave this unset.
    deload_rule: str | None = None
    stop_rules: list[str] = Field(default_factory=list)
    substitutions: list[str] = Field(default_factory=list)
    red_flags: list[RedFlagRule] = Field(default_factory=list)
    # The canonical rehab-bank drill id, on rehab blocks only. It is what lets a
    # completed session be attributed to a specific injury without matching on
    # display_name, which Stage 2 rewrites. Server-verified against the day's own
    # candidate pool after generation (see reconcile_rehab_drill_ids): an id that
    # does not appear there is cleared, and a cleared id means the completion
    # gate refuses the drill rather than guessing which injury it was for.
    rehab_drill_id: str | None = None
    # Stable identity of the physical exercise this block prescribes, as a
    # normalized slug in the exercise-media taxonomy ("tempo-shadowboxing").
    # display_name is presentation copy that Stage 2 may rewrite into a dose
    # ("3 x 2 min easy rounds"); this field is what demo videos resolve on.
    # Server-owned: stamped from planner/bank data by reconcile_exercise_keys,
    # never taken from the model. None on legacy plans and on blocks with no
    # deterministic identity, which fall back to display_name matching.
    exercise_key: str | None = None
    # Server-owned progression context, re-derived from the planning brief on
    # every read (api/rep_range.py). ``reps`` stays the prescription; rep_range
    # is the bank range it was chosen from, and progression_hold marks a block
    # that must not progress (taper, final fortnight, injury, weight cut, cap).
    rep_range: list[int] | None = Field(default=None, min_length=2, max_length=2)
    progression_hold: bool | None = None
    # Athlete-authored prescriptions retain their exact text and dose alongside
    # the shared numeric fields. No guessed load, coaching or movement metadata.
    prescription_source: Literal["athlete"] | None = None
    prescription_text: str | None = None
    athlete_dose: dict[str, Any] | None = None
    catalogue_id: str | None = None
    private_media_id: str | None = None
    community_revision_id: str | None = None
    source_session_id: str | None = None
    notes: str | None = None
    # Server-owned provenance of an athlete's swap or added exercise, applied
    # from plans.exercise_overrides on read (api/services/exercise_overrides.py).
    # Never accepted from a generated card: the resolver strips it.
    athlete_change: dict[str, Any] | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_athlete_change(self, handler):
        data = handler(self)
        for field in ("athlete_change", "rest_note", "components", "rep_range", "progression_hold"):
            data = _omit_none(data, field)
        return data


class Completion(BaseModel):
    """Post-session completion log (Section P)."""

    session_rpe: float | None = Field(default=None, ge=0, le=10)
    pain_after_session: int | None = Field(default=None, ge=0, le=10)
    performed_duration: MeasuredValue | None = None
    modification_reason: str | None = None
    notes: str | None = None
    completed_at: str | None = None


class Session(BaseModel):
    """A single training session within a day (Section M)."""

    # Completion writes still require a real id, but the read-only plan card can
    # safely render without one.  Do not replace missing identity with a generic
    # value that can collide with another session.
    session_id: str | None = None
    session_type: SessionType
    prescription_source: Literal["athlete"] | None = None
    title: str
    objective: str
    planned_duration: MeasuredValue | None = None
    primary_stressor: str | None = None
    cns_demand: str | None = None
    impact_level: str | None = None
    completion_status: CompletionStatus = "not_started"
    mindset_anchor: MindsetAnchor
    blocks: list[SessionBlock] = Field(default_factory=list)
    completion: Completion | None = None
    # 1-based position of this session within its day. Server-owned: set by
    # fightcamp.session_sequencing after the day's membership is final (never
    # model-generated), and the day's ``sessions`` list is already in this
    # order. Distinct from any planning identity such as ``session_index``.
    execution_order: int | None = None
    # Server-owned: True only for work offered but never required (the optional
    # camp Fight Visualisation). Set by structured_plan_locked_merge, which clears
    # any model-supplied value; read through api.optional_sessions.
    optional: bool | None = None
    # Server-owned: exercises the athlete removed from this occurrence, so the
    # card can offer to restore them. Applied on read, like athlete_change.
    athlete_removed: list[dict[str, Any]] | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_athlete_removed(self, handler):
        return _omit_none(handler(self), "athlete_removed")


class TodayCard(BaseModel):
    """The athlete-facing summary shown at the top of a day (Section K).

    Readiness is self-report only — no biometric scores.
    """

    headline: str
    readiness_status: ReadinessStatus
    mindset_anchor: MindsetAnchor
    primary_warning: str | None = None
    nutrition_summary: str | None = None
    weight_cut_warning: str | None = None
    # Coach-owned contact (declared / downgraded sparring) that coexists with the
    # day's app sessions. Rendered as a context line above the session cards so a
    # sparring day that also carries a low-load app touch shows both, not one.
    coach_led_contact: str | None = None
    # Server-owned: deterministic explanation for a reduced-dose contact day,
    # stamped by structured_plan_sparring_reconcile from the planner's reason codes.
    contact_reason: str | None = None


class PriorityMicrodose(BaseModel):
    """A small planner-owned dose attached to its host calendar day."""

    goal: str
    name: str
    prescription: str


class Day(BaseModel):
    """A calendar day in the plan (Section J)."""

    date: str
    # Renewable open plans have no event countdown, so weekday is their stable
    # template identity. Optional for legacy/datestamped cards; the read adapter
    # validates and fills it before an open plan is shown to an athlete.
    weekday: Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] | None = None
    day_type: DayType
    countdown_label: str
    phase_label: PhaseLabel
    # Stage 1 structural ownership. ``phase_label`` remains the athlete-facing
    # day phase; these fields preserve the planning block that assigned the day
    # when Mon-Sun display weeks straddle a phase boundary.
    planning_week_index: int | None = None
    planning_week_phase: PhaseLabel | None = None
    planning_day_role_keys: list[str] = Field(default_factory=list)
    priority_microdose: PriorityMicrodose | None = None
    today_card: TodayCard
    sessions: list[Session] = Field(default_factory=list)


class LoadFocus(BaseModel):
    """Week-level load dial settings (Section H)."""

    volume: LoadFocusValue
    intensity: LoadFocusValue
    specificity: LoadFocusValue
    fatigue_target: LoadFocusValue


class Progression(BaseModel):
    """How a week relates to the previous one (Section I)."""

    week_type: WeekType
    planned_change_from_previous: str


class Week(BaseModel):
    """A training week (Section G)."""

    # Display identity is optional; week order is already explicit in weeks[].
    week_id: str | None = None
    week_index: int
    phase_label: PhaseLabel
    week_goal: str
    start_date: str
    end_date: str
    countdown_start: str | None = None
    countdown_end: str | None = None
    # Display weeks are Mon-Sun groups. They may contain days from more than one
    # Stage 1 planning week/phase, so keep that ownership separate.
    display_week_kind: str | None = None
    planning_week_indices: list[int] = Field(default_factory=list)
    phase_coverage: list[str] = Field(default_factory=list)
    load_focus: LoadFocus
    progression: Progression
    days: list[Day] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Daily self-report check-in (Section Q)
# ---------------------------------------------------------------------------


class MorningCheckIn(BaseModel):
    """3-tap morning self-report. Self-report only, no biometrics."""

    sleep_quality: int = Field(ge=1, le=5)
    overall_readiness: int = Field(ge=1, le=5)
    pain: int = Field(ge=0, le=10)
    location: str | None = None
    injury_specific: dict[str, Any] | None = None


class DailyCheckIn(BaseModel):
    """A dated check-in plus the resulting decision (Section Q)."""

    date: str
    morning: MorningCheckIn
    decision: ReadinessStatus
    rules_triggered: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Nutrition (Section R)
# ---------------------------------------------------------------------------


class WeightCutWarning(BaseModel):
    """Weight-cut risk flag.

    Safety rule: this represents the *risk*, never direct acute-cut
    instructions (no dehydration/sauna/water-loading/sodium protocols). Anything
    aggressive must route through ``requires_professional_support``.
    """

    risk_level: RiskLevel
    display_text: str
    requires_professional_support: bool = False


class Nutrition(BaseModel):
    """Plan-level nutrition guidance (Section R)."""

    summary: str
    daily_focus: str
    training_day_guidance: str
    fight_week_guidance: str
    weight_cut_warning: WeightCutWarning | None = None


# ---------------------------------------------------------------------------
# Plan-level context (Sections B, C, D)
# ---------------------------------------------------------------------------


class PlanMetadata(BaseModel):
    """Top-level plan metadata (Section B)."""

    plan_id: str | None = None
    title: str
    sport: str
    plan_type: PlanType
    prescription_source: Literal["athlete"] | None = None
    timezone: str
    status: PlanStatus
    created_at: str | None = None
    created_by: str | None = None
    units: UnitsSystem = "metric"


class AthleteContext(BaseModel):
    """Athlete profile snapshot used to generate the plan (Section C)."""

    athlete_id: str | None = None
    sport_profile: str
    style_profile: str | None = None
    experience_level: str | None = None
    sex: str | None = None
    age: int | None = None
    body_mass: MeasuredValue | None = None
    weight_class: str | None = None
    injury_status: str | None = None
    known_issues: list[str] = Field(default_factory=list)
    equipment_access: list[str] = Field(default_factory=list)
    constraints: list[str] | None = None


class EventContext(BaseModel):
    """The event the plan counts down to (Section D)."""

    fight_date: str | None = None
    match_date: str | None = None
    weigh_in_date: str | None = None
    event_type: EventType | None = None
    ruleset: str | None = None


# ---------------------------------------------------------------------------
# Root object (Section A)
# ---------------------------------------------------------------------------


class StructuredTrainingPlan(BaseModel):
    """Root structured plan (Section A).

    Always carries ``raw_markdown_fallback`` so the athlete sees *something*
    even if structured rendering is unavailable.
    """

    schema_version: str = SCHEMA_VERSION
    plan_metadata: PlanMetadata
    athlete_context: AthleteContext
    event_context: EventContext | None = None
    countdown_labels: list[CountdownLabel] = Field(default_factory=list)
    red_flag_rules: list[RedFlagRule] = Field(default_factory=list)
    # Short plan-level reminders from the header "Active notes" / footer notes
    # (weight cut, injury, nutrition, general). Optional so legacy plans and the
    # plan_text fallback keep working.
    plan_notes: list[PlanNote] = Field(default_factory=list)
    weeks: list[Week] = Field(default_factory=list)
    daily_check_ins: list[DailyCheckIn] = Field(default_factory=list)
    nutrition: Nutrition
    progression_notes: str = ""
    # Athlete-safe projection of Stage 1's deterministic computed_support
    # (macros / hydration / fuel timing / weight-cut risk band per phase, plus
    # recovery sleep/fatigue/phase-focus per phase). Injected deterministically
    # during conversion — never model-generated — and ALWAYS coach_gated-free.
    # Optional so legacy plans and the plan_text fallback keep working.
    deterministic_support: dict[str, Any] | None = None
    raw_markdown_fallback: str = ""


# ---------------------------------------------------------------------------
# Strict json_schema generation for OpenAI structured outputs (opt-in)
# ---------------------------------------------------------------------------
#
# OpenAI's strict json_schema mode guarantees the model returns an object that
# conforms to a schema (not merely syntactically valid JSON), which lets the
# structured-card call retire its repair retry. Strict mode imposes hard
# requirements the raw Pydantic schema does not meet, so the schema is
# transformed:
#   * every object sets additionalProperties: false and lists ALL properties in
#     `required`. Pydantic already emits Optional fields as nullable
#     (anyOf with {"type": "null"}), so the model can emit null for them; fields
#     with a non-null default (str="", list=[]) simply emit their empty value.
#   * keywords strict mode rejects (default, format, numeric/length/item bounds,
#     pattern, …) are stripped.
#   * free-form dict[str, Any] objects (which strict mode forbids entirely) are
#     reduced to null-only. Our two such fields are both Optional:
#     deterministic_support is injected server-side after conversion (never
#     model-generated), and MorningCheckIn.injury_specific degrades to null.
#
# IMPORTANT: this is verified here only for STRUCTURAL strict-compliance
# (all-required, additionalProperties:false, no banned keywords, no free-form
# objects). Whether the live OpenAI endpoint accepts the schema, and whether the
# model output round-trips cleanly through validation, MUST be confirmed in
# staging with a real API key before the schema mode is enabled in production.

_STRICT_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "default",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "multipleOf",
    }
)


def _is_free_form_object(node: Any) -> bool:
    """Whether ``node`` is a schema for an object with no declared shape.

    Strict mode forbids free-form objects. A proper object (has ``properties``)
    is never free-form; a ``dict[str, Any]`` (``additionalProperties: true``) or
    a bare ``{"type": "object"}`` with no properties is.
    """

    if not isinstance(node, dict) or node.get("properties"):
        return False
    additional = node.get("additionalProperties")
    if additional is True or isinstance(additional, dict):
        return True
    return node.get("type") == "object" and additional is None


def _strictify_schema_node(node: Any) -> Any:
    """Recursively rewrite a JSON schema node to satisfy OpenAI strict mode."""

    if isinstance(node, list):
        return [_strictify_schema_node(item) for item in node]
    if not isinstance(node, dict):
        return node

    # Drop free-form-object variants from unions (they cannot survive strict
    # mode). Our free-form fields are all Optional, so this leaves the null
    # variant and the field becomes null-only.
    if isinstance(node.get("anyOf"), list):
        variants = [v for v in node["anyOf"] if not _is_free_form_object(v)]
        node = {**node, "anyOf": variants or [{"type": "null"}]}

    cleaned: dict[str, Any] = {}
    for key, value in node.items():
        if key in _STRICT_UNSUPPORTED_KEYWORDS:
            continue
        cleaned[key] = _strictify_schema_node(value)

    # A standalone free-form object (not reachable via the anyOf handling above)
    # has no expressible strict shape; force it to null.
    if _is_free_form_object(cleaned):
        return {"type": "null"}

    properties = cleaned.get("properties")
    if isinstance(properties, dict):
        cleaned["required"] = list(properties.keys())
        cleaned["additionalProperties"] = False

    return cleaned


_STRICT_SCHEMA_CACHE: dict[str, Any] | None = None


def build_strict_structured_plan_schema() -> dict[str, Any]:
    """Return the OpenAI strict-mode json_schema for StructuredTrainingPlan.

    Cached (the transform is pure). See the module comment above for the strict
    requirements enforced and the staging-validation caveat.
    """

    global _STRICT_SCHEMA_CACHE
    if _STRICT_SCHEMA_CACHE is None:
        raw = StructuredTrainingPlan.model_json_schema()
        _STRICT_SCHEMA_CACHE = _strictify_schema_node(copy.deepcopy(raw))
    return copy.deepcopy(_STRICT_SCHEMA_CACHE)


# ---------------------------------------------------------------------------
# Validation + safe-parse helpers (Section 3)
# ---------------------------------------------------------------------------


class StructuredPlanParseResult(BaseModel):
    """Outcome of a non-throwing parse attempt.

    Exactly one of ``plan`` (on success) or ``raw_markdown_fallback`` (on
    failure) is the thing the caller should hand to the frontend. ``errors``
    carries human-readable validation messages for logging/debugging.
    """

    ok: bool
    plan: StructuredTrainingPlan | None = None
    raw_markdown_fallback: str | None = None
    errors: list[str] = Field(default_factory=list)


def validate_structured_plan(data: Any) -> StructuredTrainingPlan:
    """Strictly validate ``data`` into a :class:`StructuredTrainingPlan`.

    Raises :class:`pydantic.ValidationError` on failure. Use this when a caller
    wants the error to propagate; use :func:`safe_parse_structured_plan` in the
    generation flow where crashing is not acceptable.
    """

    return StructuredTrainingPlan.model_validate(data)


def _format_validation_errors(error: ValidationError) -> list[str]:
    """Render a ValidationError into compact ``loc: message`` strings."""

    messages: list[str] = []
    for item in error.errors():
        loc = ".".join(str(part) for part in item.get("loc", ()))
        msg = item.get("msg", "invalid value")
        messages.append(f"{loc}: {msg}" if loc else msg)
    return messages


def safe_parse_structured_plan(
    raw_data: Any,
    raw_markdown: str | None = None,
) -> StructuredPlanParseResult:
    """Validate structured JSON without ever crashing the generation flow.

    * On success, returns ``ok=True`` with the parsed plan. If ``raw_markdown``
      is supplied and the plan did not already carry one, it is stored on the
      plan so the fallback is always present.
    * On failure, returns ``ok=False`` with the raw markdown preserved and the
      validation errors exposed for logging.
    """

    try:
        plan = StructuredTrainingPlan.model_validate(raw_data)
    except ValidationError as error:
        return StructuredPlanParseResult(
            ok=False,
            plan=None,
            raw_markdown_fallback=raw_markdown,
            errors=_format_validation_errors(error),
        )
    except Exception as error:  # defensive: never crash generation
        return StructuredPlanParseResult(
            ok=False,
            plan=None,
            raw_markdown_fallback=raw_markdown,
            errors=[f"unexpected error: {error}"],
        )

    if raw_markdown and not plan.raw_markdown_fallback:
        plan = plan.model_copy(update={"raw_markdown_fallback": raw_markdown})

    return StructuredPlanParseResult(ok=True, plan=plan, errors=[])


def repair_structured_plan_once(
    raw_data: Any,
    *,
    repair_fn: Any | None = None,
    raw_markdown: str | None = None,
) -> StructuredPlanParseResult:
    """First attempt -> validate -> one repair retry -> raw markdown fallback.

    This is a deliberately small placeholder for the eventual LLM-repair loop.
    The flow is:

    1. Try to parse ``raw_data``.
    2. If it validates, return it.
    3. Otherwise, if a ``repair_fn(raw_data, errors) -> raw_data`` callable is
       provided, run it exactly once and re-validate the result.
    4. If repair is absent, raises, or still fails validation, fall back to the
       preserved ``raw_markdown``.

    ``repair_fn`` is intentionally untyped/optional so this hook can be wired
    into the real generation architecture later without forcing it now.
    """

    first = safe_parse_structured_plan(raw_data, raw_markdown=raw_markdown)
    if first.ok or repair_fn is None:
        return first

    try:
        repaired = repair_fn(raw_data, first.errors)
    except Exception as error:  # repair itself failed -> keep the fallback
        return StructuredPlanParseResult(
            ok=False,
            plan=None,
            raw_markdown_fallback=raw_markdown,
            errors=first.errors + [f"repair failed: {error}"],
        )

    second = safe_parse_structured_plan(repaired, raw_markdown=raw_markdown)
    if not second.ok:
        # Surface both attempts' errors for debugging.
        second = second.model_copy(
            update={"errors": first.errors + second.errors}
        )
    return second
