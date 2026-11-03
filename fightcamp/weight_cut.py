from __future__ import annotations

import math
import re


def parse_weight_value(raw: object) -> float:
    """Parse weight-like values from numeric or string input."""
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)

    text = str(raw).strip()
    if not text:
        return 0.0
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return 0.0
    try:
        return float(match.group(0))
    except ValueError:
        return 0.0


def parse_optional_weight_value(raw: object) -> float | None:
    """Usable weight in kg, or ``None`` when it was never provided.

    ``parse_weight_value`` collapses missing, blank, and unparseable input to
    ``0.0``, which is indistinguishable from a real zero. Weight-cut logic must
    keep that distinction: a blank target weight is *unknown*, not "cutting to
    zero". Anything that does not parse to a positive number is treated as not
    provided.
    """
    value = parse_weight_value(raw)
    return value if value > 0.0 else None


# Whether both sides of the cut calculation were actually collected. Blank or
# unparseable input yields a ``missing_*`` status rather than a silent zero, so
# downstream consumers can say "no target weight set" instead of inventing a
# 100% cut (blank target) or silently dropping a declared target (blank current).
WEIGHT_CUT_INPUTS_KNOWN = "known"
WEIGHT_CUT_INPUTS_MISSING_TARGET = "missing_target_weight"
WEIGHT_CUT_INPUTS_MISSING_CURRENT = "missing_current_weight"
WEIGHT_CUT_INPUTS_MISSING_BOTH = "missing_both_weights"

WEIGHT_CUT_INPUTS_UNKNOWN_STATUSES = frozenset(
    {
        WEIGHT_CUT_INPUTS_MISSING_TARGET,
        WEIGHT_CUT_INPUTS_MISSING_CURRENT,
        WEIGHT_CUT_INPUTS_MISSING_BOTH,
    }
)


def weight_cut_input_status(current_weight: object, target_weight: object) -> str:
    """Which side(s) of the cut calculation the athlete actually provided."""
    current = parse_optional_weight_value(current_weight)
    target = parse_optional_weight_value(target_weight)
    if current is None and target is None:
        return WEIGHT_CUT_INPUTS_MISSING_BOTH
    if current is None:
        return WEIGHT_CUT_INPUTS_MISSING_CURRENT
    if target is None:
        return WEIGHT_CUT_INPUTS_MISSING_TARGET
    return WEIGHT_CUT_INPUTS_KNOWN


def weight_cut_inputs_known(current_weight: object, target_weight: object) -> bool:
    """Whether both current and target weight are usable numbers."""
    return weight_cut_input_status(current_weight, target_weight) == WEIGHT_CUT_INPUTS_KNOWN


def compute_weight_cut_pct(current_weight: object, target_weight: object) -> float:
    """
    Return active cut percentage as body-mass delta:
      (current - target) / current * 100
    Clamped at zero and rounded to one decimal.

    Returns ``0.0`` when either weight is missing. A blank target weight used to
    parse as ``0.0`` and produce a 100% cut, which saturated every downstream
    severity gate (``extreme`` bucket, supervision required, hard-sparring
    blocks) for an athlete who simply left the optional field empty. Absent
    input means the cut is *unknown*, not maximal — callers that need to tell
    "no cut" apart from "no data" should use :func:`weight_cut_input_status`.
    """
    current = parse_optional_weight_value(current_weight)
    target = parse_optional_weight_value(target_weight)
    if current is None or target is None:
        return 0.0
    if current < 1.0:
        return 0.0
    return round(max(0.0, (current - target) / current * 100.0), 1)


# Strain score at which a cut escalates to "high" (supervision, alarm copy).
HEALTH_ESCALATION_SCORE = 35.0
# A cut this large is severe at any days-out.
CUT_MAGNITUDE_FLOOR_PCT = 6.0


def compute_cut_severity_score(weight_cut_pct: object, days_until_fight: object) -> float:
    """
    Deterministic active-cut severity score (0-100):
      3.2 * (cut_pct^1.15) * (1 + 1.8 * exp(-days_out / 15))
    """
    try:
        cut_pct = float(weight_cut_pct or 0.0)
    except (TypeError, ValueError):
        cut_pct = 0.0
    try:
        days_out = int(days_until_fight)
    except (TypeError, ValueError):
        days_out = 35

    cut_pct = max(0.0, cut_pct)
    days_out = max(0, days_out)
    raw_score = 3.2 * (cut_pct ** 1.15) * (1.0 + 1.8 * math.exp(-days_out / 15.0))
    return round(min(100.0, max(0.0, raw_score)), 1)


def cut_health_bucket(score: object) -> str:
    """Map the severity score to buckets for PHYSIOLOGICAL STRAIN.

    One score, two scales, because a cut poses two different questions:

    * strain (this function, stricter, original calibration) -> "how hard is
      this cut on the body right now?" It drives health-risk escalation
      (warnings, supervision, stop-and-report) *and* load shaping (volume, RPE
      ceilings, glycolytic exposure, strength dose, sparring dose). A 5% cut six
      days out is strain-high and must keep being treated as such.
    * capacity (:func:`cut_severity_bucket`, recalibrated) -> "does this cut
      justify deleting training days?" The same 5% cut six days out does not; it
      warrants a reduced dose, not a blank week.

    Keeping them apart is what lets the planner reduce dose *before* deleting
    calendar: recalibrating capacity severity must never quietly relax medical
    escalation or load shaping, so strain keeps its own thresholds.
    """
    try:
        value = float(score or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    if value < 5.0:
        return "none"
    if value < 15.0:
        return "low"
    if value < HEALTH_ESCALATION_SCORE:
        return "moderate"
    if value < 55.0:
        return "high"
    if value < 85.0:
        return "critical"
    return "extreme"


def cut_severity_bucket(score: object) -> str:
    """Map the severity score to buckets for TRAINING-PRESSURE decisions.

    Boundaries (none <10, low <25, moderate <50, high <75, critical <95,
    extreme >=95) are calibrated against combat-sport evidence rather than
    arbitrary conservatism. At the boundaries this puts the moderate/high line
    at roughly 8.8% at D-28, 7.2% at D-16 and 4.6% at D-1, and the high/critical
    line at 12.5% / 10.2% / 6.6% respectively.

    Sanity check against the evidence base: UFC data on 616 athletes recorded
    average mass above class of ~6.7% at 72h, ~5.7% at 48h and ~4.4% at 24h
    before weigh-in, and the 2025 ISSN combat-sport position stand treats ~2-4%
    acute water loss in the final 24h as a practice seen in appropriately
    supervised professional contexts. The previous boundaries placed
    moderate/high at 5.3% at D-16 and 3.4% at D-1, i.e. they classified the
    *median* competitive cut as "high" and deleted training for it. These
    boundaries keep a routine cut in ``moderate`` and reserve ``high`` and above
    for cuts that genuinely exceed normal competitive practice.

    This is a calibration of *planner* severity, not an endorsement of
    aggressive dehydration: health-risk escalation keeps its own magnitude floor
    (see :func:`weight_cut_risk_band` / :func:`weight_cut_supervision_required`).
    """
    try:
        value = float(score or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    if value < 10.0:
        return "none"
    if value < 25.0:
        return "low"
    if value < 50.0:
        return "moderate"
    if value < 75.0:
        return "high"
    if value < 95.0:
        return "critical"
    return "extreme"


# Ordinal ordering of the severity buckets (higher index = more severe). Used to
# decide, from the deterministic smart score, when a cut is serious enough to
# warrant supervision / stop-and-report / red-flag copy versus a calm note.
_SEVERITY_ORDER = ("none", "low", "moderate", "high", "critical", "extreme")


def cut_severity_rank(bucket: object) -> int:
    """Ordinal rank of a severity bucket (0 = none, higher = more severe)."""
    key = str(bucket or "none").strip().lower()
    try:
        return _SEVERITY_ORDER.index(key)
    except ValueError:
        return 0


def cut_warnings_escalate(bucket: object) -> bool:
    """Whether a cut is *worse than moderate* (high / critical / extreme).

    This is the single gate for alarm-tier weight-cut copy. Only an escalated
    cut warrants supervision, stop-and-report red flags, or "seek qualified
    support" language. A ``none`` / ``low`` / ``moderate`` cut does NOT — it gets
    a brief, calm active note (with light precautions at moderate) instead of
    the plan shouting medical warnings at the athlete.
    """
    return cut_severity_rank(bucket) >= _SEVERITY_ORDER.index("high")


def weight_cut_risk_band(
    active: object,
    cut_pct: object,
    days_until_fight: object = None,
) -> str:
    """Athlete-facing weight-cut band, derived from the smart severity score.

    Returns one of ``none`` / ``moderate`` / ``high`` / ``severe``:

    * inactive cut -> ``none``
    * ``cut_pct >= 6`` -> ``severe`` (magnitude floor for a medically serious
      acute cut, independent of days-out)
    * smart bucket critical/extreme -> ``severe``
    * smart bucket high -> ``high``
    * any other active cut -> ``moderate`` (active, but not escalated)

    Crucially this does NOT promote a routine active cut to ``high`` just because
    a fight is near or the athlete is tired — the smart score already folds in
    days-out. Only a genuinely high/critical/extreme cut earns alarm-tier
    handling downstream.
    """
    if not active:
        return "none"
    try:
        pct = float(cut_pct or 0.0)
    except (TypeError, ValueError):
        pct = 0.0
    try:
        days = int(float(days_until_fight)) if days_until_fight is not None else None
    except (TypeError, ValueError):
        days = None
    if pct >= CUT_MAGNITUDE_FLOOR_PCT:
        return "severe"
    rank = cut_severity_rank(
        cut_health_bucket(compute_cut_severity_score(pct, days))
    )
    if rank >= _SEVERITY_ORDER.index("critical"):
        return "severe"
    if rank >= _SEVERITY_ORDER.index("high"):
        return "high"
    return "moderate"


def weight_cut_supervision_required(
    active: object,
    cut_pct: object,
    days_until_fight: object = None,
) -> bool:
    """Whether a cut is serious enough to flag qualified supervision.

    True only for a magnitude-heavy cut (``>= 6%``) or a smart bucket that is
    worse than moderate. A moderate-or-lower cut never trips the supervision
    flag, so the plan stops recommending medical oversight for routine cuts.
    """
    if not active:
        return False
    try:
        pct = float(cut_pct or 0.0)
    except (TypeError, ValueError):
        pct = 0.0
    try:
        days = int(float(days_until_fight)) if days_until_fight is not None else None
    except (TypeError, ValueError):
        days = None
    if pct >= CUT_MAGNITUDE_FLOOR_PCT:
        return True
    return cut_warnings_escalate(
        cut_health_bucket(compute_cut_severity_score(pct, days))
    )


def escalation_cut_pct(days_until_fight: object) -> float:
    """The largest cut % that stays below strain escalation at this days-out.

    The closed-form inverse of :func:`compute_cut_severity_score` at
    ``HEALTH_ESCALATION_SCORE``, capped at the magnitude floor, so it reads the
    same scale as :func:`weight_cut_risk_band` rather than adding a threshold.
    A cut at or under this percentage is not escalated.
    """
    try:
        days_out = max(0, int(days_until_fight))
    except (TypeError, ValueError):
        days_out = 35
    pressure = 3.2 * (1.0 + 1.8 * math.exp(-days_out / 15.0))
    return min(CUT_MAGNITUDE_FLOOR_PCT, (HEALTH_ESCALATION_SCORE / pressure) ** (1 / 1.15))


def is_high_pressure_weight_cut(flags: dict) -> bool:
    """Whether an active cut is under enough pressure to soften load and density.

    Reads the flat ``weight_cut_risk`` / ``weight_cut_pct`` / ``fatigue`` /
    ``days_until_fight`` shape used by the Stage 1 nutrition and recovery
    blocks. ``athlete_model._is_high_pressure_weight_cut`` answers the same
    question from the readiness-flag shape; keep the two in step.

    This softens load and density, so it reads the STRAIN scale. It used to
    short-circuit on a bare ``weight_cut_pct >= 5.0``, which was a third
    severity system: days-out blind, it treated 5% at D-40 exactly like 5% on
    fight day and contradicted the canonical score both scales derive from.

    A low-fatigue, non-strained active cut only counts as high-pressure inside
    the final two weeks (<=14). A strain-escalated cut and moderate+ fatigue stay
    high-pressure at any distance via the clauses above.
    """
    if not flags.get("weight_cut_risk", False):
        return False
    try:
        cut_pct = float(flags.get("weight_cut_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        cut_pct = 0.0
    if cut_warnings_escalate(
        cut_health_bucket(
            compute_cut_severity_score(cut_pct, flags.get("days_until_fight"))
        )
    ):
        return True
    fatigue = str(flags.get("fatigue", "")).strip().lower()
    days_until_fight = flags.get("days_until_fight")
    return fatigue in {"moderate", "high"} or (
        isinstance(days_until_fight, int) and days_until_fight <= 14
    )


# ---------------------------------------------------------------------------
# Cut TRAINING PRESSURE (distinct from cut HEALTH RISK)
# ---------------------------------------------------------------------------
# Health risk answers "does this cut warrant a warning, supervision, or medical
# escalation?" and is owned by ``weight_cut_risk_band`` /
# ``weight_cut_supervision_required``, which keep their own >=6% magnitude floor
# because a medically serious acute cut is serious regardless of days-out.
#
# Training pressure answers a different question: "does this cut justify
# *deleting training*?" A health caution must not silently delete physical
# sessions, so training consequences are resolved here and nowhere else.
#
# The intended hierarchy is dose-before-calendar:
#   none/low  - no training consequence at all
#   moderate  - preserve training frequency; adjust volume / density / RPE
#               ceiling / glycolytic dose / recovery emphasis only
#   high      - still preserve usable days; shed expensive work first
#               (volume, soreness cost, glycolytic density, eccentric and
#               collision load) before removing anything
#   critical  - may remove one meaningful stress exposure
#   extreme   - may suppress high-risk training and escalate on safety

_CUT_TRAINING_COMPRESSION_POINTS = {
    "none": 0,
    "low": 0,
    "moderate": 0,
    "high": 1,
    "critical": 2,
    "extreme": 2,
}


def cut_training_compression_points(bucket: object) -> int:
    """Weekly *calendar* slots a cut may remove, as one canonical decision.

    This is the single authority for cut-driven session-count removal. No other
    module may independently subtract weekly capacity for the same cut: stacking
    a second removal on top of this (an extra late-camp overlay, a raw-percentage
    rule, a proximity point that the severity score already folded in) is what
    turned a routine 5.3% cut into a three-slot deletion.

    Moderate deliberately returns 0. A moderate cut still shapes training, but it
    does so through dose, density and RPE ceilings, never by emptying the
    calendar.
    """
    key = str(bucket or "none").strip().lower()
    return _CUT_TRAINING_COMPRESSION_POINTS.get(key, 0)


def cut_restricts_training_capacity(bucket: object) -> bool:
    """Whether a cut is restrictive enough to justify removing training capacity."""
    return cut_training_compression_points(bucket) > 0


def cut_justifies_goal_deferral(bucket: object) -> bool:
    """Whether a cut is restrictive enough to abandon a requested goal.

    Deliberately far stricter than "an active cut exists". Deferring power,
    footwork or skill refinement requires a genuinely restrictive cut state, not
    a routine one - at moderate and below a safe qualifying exposure can almost
    always be preserved at reduced dose instead.
    """
    return cut_severity_rank(bucket) >= _SEVERITY_ORDER.index("critical")


_VALID_BUCKETS = frozenset(_SEVERITY_ORDER)


def resolve_cut_health_bucket(source: object) -> str:
    """Resolve the STRAIN bucket from an athlete model / flags mapping.

    Dose and load consumers must never silently read the capacity bucket in
    place of the strain bucket. The two disagree by design — a 5.3% cut at D-16
    is capacity ``moderate`` but strain ``high`` — so falling back from a
    missing strain field straight to ``cut_severity_bucket`` would treat real
    physical strain as milder than it is, the exact inverse of the stacked
    penalties this module exists to prevent.

    Precedence, strictest first:

    1. an explicit ``cut_health_bucket``
    2. the strain reading of a stamped ``cut_severity_score``
    3. the strain reading of a score recomputed from ``weight_cut_pct`` and
       ``days_until_fight``
    4. only then a legacy ``cut_severity_bucket``, for snapshots that predate
       the split and carry nothing else

    Returns ``""`` when nothing is resolvable, so callers keep their own
    defaults.
    """
    if not isinstance(source, dict):
        return ""

    explicit = str(source.get("cut_health_bucket") or "").strip().lower()
    if explicit in _VALID_BUCKETS:
        return explicit

    raw_score = source.get("cut_severity_score")
    if raw_score is not None:
        try:
            return cut_health_bucket(float(raw_score))
        except (TypeError, ValueError):
            pass

    raw_pct = source.get("weight_cut_pct")
    if raw_pct is not None:
        try:
            pct = float(raw_pct)
        except (TypeError, ValueError):
            pct = None
        if pct is not None:
            return cut_health_bucket(
                compute_cut_severity_score(pct, source.get("days_until_fight"))
            )

    legacy = str(source.get("cut_severity_bucket") or "").strip().lower()
    return legacy if legacy in _VALID_BUCKETS else ""
