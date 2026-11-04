"""Tests for the deterministic-first authority + safety audits (PR-5).

The audits surface coach_gated leakage, computed_support vs structured_plan
conflicts, and duplicate rendered strings as prefixed findings. Findings are
severity-classified: LEAKAGE / CONFLICT / AUDIT_ERROR block the structured card
(``blocked_by_safety_audit`` — the card is discarded and never published);
DUPLICATE findings stay advisory warnings. The audits must never mutate the
plan and never resolve conflicts themselves.
"""
from __future__ import annotations

import copy

from api.structured_plan_generation import (
    build_structured_plan_outcome,
    build_structured_plan_prompt,
)
import json

from api.structured_plan_safety import (
    audit_structured_plan,
    detect_coach_gated_leakage,
    detect_computed_support_conflicts,
    detect_duplicate_rendered_strings,
    strip_coach_gated,
)
from fightcamp.stage2_payload import build_computed_support

from test_structured_plan_models import _valid_plan


def _support(**flags) -> dict:
    base = {"weight": 70, "mental_block": ["confidence"]}
    base.update(flags)
    return build_computed_support(flags=base, phases=["GPP", "SPP", "TAPER"])


def _faithful_source(plan: dict) -> str:
    """Markdown faithful to ``plan`` so the faithfulness gate returns a clean card.

    The gate rejects a countdown-claiming card whose source carries no D-day
    marker, so a placeholder like ``# raw`` would degrade a valid plan to
    ``invalid_fallback_used``. Derive a real countdown source from the plan.
    """
    lines = ["# FIGHT CAMP PLAN", ""]
    for week in plan.get("weeks") or []:
        lines.append(
            f"## Week — SPP ({week.get('countdown_start')} to {week.get('countdown_end')})"
        )
        lines.append("")
        for day in week.get("days") or []:
            lines.append(f"### Day ({day.get('countdown_label') or ''}) — Session")
            for session in day.get("sessions") or []:
                for block in session.get("blocks") or []:
                    name = block.get("display_name")
                    if name:
                        lines.append(f"- {name}")
            lines.append("")
    return "\n".join(lines)


def _plan(nutrition=None, weeks=None) -> dict:
    return {
        "nutrition": nutrition or {"summary": "Eat well.", "daily_focus": "", "weight_cut_warning": None},
        "weeks": weeks or [],
    }


# --- coach_gated leakage ----------------------------------------------------


def test_coach_gated_leakage_detected_when_copied_into_athlete_facing():
    support = _support(weight_cut_risk=True, weight_cut_pct=7.0, fatigue="high")
    gated = support["nutrition"]["by_phase"]["TAPER"]["coach_gated"]["acute_cut_protocol"]
    leaked_text = gated["bicarbonate_g_per_kg"]

    plan = _plan(nutrition={"summary": f"Pre-fight: {leaked_text}", "daily_focus": ""})
    warnings = detect_coach_gated_leakage(plan, support)

    assert any(w.startswith("LEAKAGE") for w in warnings), warnings


def test_no_leakage_when_athlete_facing_is_clean():
    support = _support(weight_cut_risk=True, weight_cut_pct=7.0, fatigue="high")
    plan = _plan(nutrition={"summary": "Hydrate and fuel around sessions.", "daily_focus": ""})
    assert detect_coach_gated_leakage(plan, support) == []


# --- computed_support conflicts --------------------------------------------


def test_macro_conflict_is_flagged():
    support = _support()  # protein envelope ~112-175 g/day for 70kg
    plan = _plan(nutrition={"summary": "Protein: 300 g/day.", "daily_focus": ""})
    warnings = detect_computed_support_conflicts(plan, support)
    assert any("protein_g_per_day" in w and w.startswith("CONFLICT") for w in warnings), warnings


def test_hydration_conflict_is_flagged():
    support = _support()  # hydration ~2100-2800 ml/day for 70kg
    plan = _plan(nutrition={"summary": "Hydration: 500 ml/day.", "daily_focus": ""})
    warnings = detect_computed_support_conflicts(plan, support)
    assert any("hydration_ml_per_day" in w for w in warnings), warnings


def test_weight_cut_risk_conflict_is_flagged():
    support = _support(weight_cut_risk=True, weight_cut_pct=7.0)  # severe band
    plan = _plan(nutrition={
        "summary": "Standard fuelling.",
        "daily_focus": "",
        "weight_cut_warning": {"risk_level": "green", "display_text": "minor", "requires_professional_support": False},
    })
    warnings = detect_computed_support_conflicts(plan, support)
    assert any("weight-cut risk understated" in w for w in warnings), warnings


def test_moderate_cut_does_not_force_a_supervision_warning():
    # A routine, moderate-severity cut (~3.5% at D-18) must NOT be flagged as a
    # conflict just because the plan omits a supervision-tier weight_cut_warning.
    # Only escalated (high / severe) cuts require one.
    support = _support(weight_cut_risk=True, weight_cut_pct=3.5, days_until_fight=18)
    plan = _plan(nutrition={"summary": "Fuel around sessions.", "daily_focus": "", "weight_cut_warning": None})
    assert detect_computed_support_conflicts(plan, support) == []


def test_matching_computed_support_produces_no_conflict_warning():
    support = _support(weight_cut_risk=True, weight_cut_pct=7.0)
    plan = _plan(nutrition={
        "summary": "Protein 120-150 g/day; hydration 2200-2700 ml/day.",
        "daily_focus": "",
        "weight_cut_warning": {"risk_level": "red", "display_text": "supervised", "requires_professional_support": True},
    })
    assert detect_computed_support_conflicts(plan, support) == []


def test_no_conflict_when_no_computed_support():
    plan = _plan(nutrition={"summary": "Protein: 300 g/day.", "daily_focus": ""})
    assert detect_computed_support_conflicts(plan, None) == []


def test_per_kg_coefficients_do_not_create_macro_conflict():
    # "3-6 g/kg" is a per-kg coefficient, NOT a daily total — it must never be
    # parsed as 3-6 g/day and flagged against the daily-gram computed envelope.
    support = _support()
    plan = _plan(nutrition={
        "summary": "Carbs: 3-6 g/kg around sessions; protein 1.6-2.2 g/kg.",
        "daily_focus": "Fats 0.8-1.0 g/kg daily intake guidance.",
    })
    assert detect_computed_support_conflicts(plan, support) == []


def test_explicit_daily_total_still_conflicts():
    # A genuine per-day total that contradicts the envelope is still flagged.
    support = _support()
    plan = _plan(nutrition={"summary": "Carbs: 1500 g/day.", "daily_focus": ""})
    warnings = detect_computed_support_conflicts(plan, support)
    assert any("carbs_g_per_day" in w for w in warnings), warnings


# --- duplicates -------------------------------------------------------------


def _day_with_anchors(day_anchor: dict, session_anchor: dict) -> dict:
    return {
        "date": "2026-07-01",
        "today_card": {"headline": "Go", "mindset_anchor": day_anchor},
        "sessions": [{"mindset_anchor": session_anchor}],
    }


def test_duplicate_day_session_mindset_is_detected():
    anchor = {"intent": "Drive forward", "focus_cue": "Sharp hands", "reset_cue": "Breathe"}
    plan = {"weeks": [{"days": [_day_with_anchors(anchor, dict(anchor))]}]}
    warnings = detect_duplicate_rendered_strings(plan)
    assert any("mindset_anchor identical" in w for w in warnings), warnings


def test_distinct_day_session_mindset_is_not_flagged():
    day_anchor = {"intent": "Drive forward", "focus_cue": "Sharp hands", "reset_cue": "Breathe"}
    session_anchor = {"intent": "Stay loose", "focus_cue": "Move feet", "reset_cue": "Exhale"}
    plan = {"weeks": [{"days": [_day_with_anchors(day_anchor, session_anchor)]}]}
    assert detect_duplicate_rendered_strings(plan) == []


# --- audit wiring + invariants ---------------------------------------------


def test_audit_does_not_mutate_inputs():
    support = _support(weight_cut_risk=True, weight_cut_pct=7.0)
    plan = _plan(nutrition={"summary": "Protein: 300 g/day.", "daily_focus": ""})
    plan_before = copy.deepcopy(plan)
    support_before = copy.deepcopy(support)
    audit_structured_plan(plan, support)
    assert plan == plan_before
    assert support == support_before


def test_outcome_surfaces_warnings_without_changing_status():
    # A schema-valid plan with no blocking findings still validates; advisory
    # findings only land in debug warnings.
    support = _support()
    plan = _valid_plan()
    outcome = build_structured_plan_outcome(
        plan, raw_markdown=_faithful_source(plan), computed_support=support
    )
    assert outcome.status == "valid"
    debug = outcome.as_debug()
    assert "warnings" in debug
    assert isinstance(debug["warnings"], list)


# --- severity classification + publication blocking --------------------------


def test_finding_severity_classification():
    from api.structured_plan_safety import is_blocking_finding, split_findings

    findings = [
        "LEAKAGE: coach_gated text surfaced athlete-facing: 'x'",
        "CONFLICT: protein_g_per_day stated (300.0, 300.0) contradicts computed_support",
        "AUDIT_ERROR: safety audit crashed (ValueError) — treating card as unpublishable",
        "DUPLICATE: session mindset_anchor identical to day mindset_anchor on 2026-07-01",
    ]
    assert [is_blocking_finding(f) for f in findings] == [True, True, True, False]
    blocking, warnings = split_findings(findings)
    assert len(blocking) == 3
    assert warnings == [findings[3]]


def test_leaking_valid_card_is_blocked_and_discarded():
    # A schema-valid card that copies coach_gated dosing into an athlete-facing
    # field must NOT be marked valid: it is blocked and never persisted.
    support = _support(weight_cut_risk=True, weight_cut_pct=7.0, fatigue="high")
    gated = support["nutrition"]["by_phase"]["TAPER"]["coach_gated"]["acute_cut_protocol"]
    plan = _valid_plan()
    plan["nutrition"]["summary"] = f"Pre-fight buffer: {gated['bicarbonate_g_per_kg']}"

    outcome = build_structured_plan_outcome(
        plan, raw_markdown=_faithful_source(plan), computed_support=support
    )

    assert outcome.status == "blocked_by_safety_audit"
    assert outcome.structured_plan is None
    assert any(w.startswith("LEAKAGE") for w in outcome.errors), outcome.errors


def test_conflicting_valid_card_is_blocked():
    support = _support()  # protein envelope ~112-175 g/day for 70kg
    plan = _valid_plan()
    plan["nutrition"]["summary"] = "Protein: 300 g/day."

    outcome = build_structured_plan_outcome(
        plan, raw_markdown=_faithful_source(plan), computed_support=support
    )

    assert outcome.status == "blocked_by_safety_audit"
    assert outcome.structured_plan is None
    assert any(w.startswith("CONFLICT") for w in outcome.errors), outcome.errors


def test_duplicate_only_findings_do_not_block(monkeypatch):
    # DUPLICATE findings are advisory: the card stays valid, findings land in
    # warnings only.
    import api.structured_plan_generation as generation

    monkeypatch.setattr(
        generation,
        "audit_structured_plan",
        lambda *_args, **_kwargs: ["DUPLICATE: today_card.nutrition_summary duplicates plan nutrition.summary on 2026-06-01"],
    )
    plan = _valid_plan()
    outcome = build_structured_plan_outcome(
        plan, raw_markdown=_faithful_source(plan), computed_support=_support()
    )
    assert outcome.status == "valid"
    assert outcome.structured_plan is not None
    assert any(w.startswith("DUPLICATE") for w in outcome.warnings)


def test_audit_crash_is_blocking_not_silently_clean(monkeypatch):
    # A crash inside the audit must not read as "no findings" — the card is in
    # an unknown safety state and must be blocked.
    import api.structured_plan_safety as safety

    def _boom(*_args, **_kwargs):
        raise ValueError("audit exploded")

    monkeypatch.setattr(safety, "detect_duplicate_rendered_strings", _boom)

    findings = safety.audit_structured_plan(_plan(), _support())
    assert findings and findings[0].startswith("AUDIT_ERROR"), findings

    plan = _valid_plan()
    outcome = build_structured_plan_outcome(
        plan, raw_markdown=_faithful_source(plan), computed_support=_support()
    )
    assert outcome.status == "blocked_by_safety_audit"
    assert outcome.structured_plan is None


def test_prompt_keeps_valid_session_anchor_instead_of_omitting():
    # Preserve source anchors without asking the converter to author variations.
    prompt = build_structured_plan_prompt(plan_markdown="PLAN")
    assert "session.mindset_anchor" in prompt
    assert "Do not invent anchors" in prompt
    assert "vary it or omit it" not in prompt


def test_plan_text_fallback_still_works():
    # No computed_support, unsalvageable payload -> fallback status unchanged.
    outcome = build_structured_plan_outcome(["not", "a", "plan"], raw_markdown="# raw")
    assert outcome.status == "invalid_fallback_used"
    assert outcome.structured_plan is None
    assert outcome.warnings == []
    # And a skipped attempt is still skipped.
    assert build_structured_plan_outcome(None).status == "not_attempted"


# --- athlete-safe projection (PR-6) ----------------------------------------


def test_strip_coach_gated_is_recursive_and_total():
    payload = {
        "a": 1,
        "coach_gated": {"dose": "x"},
        "nested": {"coach_gated": ["y"], "keep": [{"coach_gated": 1, "ok": 2}]},
    }
    cleaned = strip_coach_gated(payload)
    assert "coach_gated" not in json.dumps(cleaned)
    assert cleaned["nested"]["keep"][0] == {"ok": 2}


def test_outcome_no_longer_injects_deterministic_support_or_coach_gated():
    support = _support(weight_cut_risk=True, weight_cut_pct=7.0, fatigue="high")
    source_plan = _valid_plan()
    # Align the card's stated risk with the computed severe band — understating
    # it is a blocking CONFLICT (covered elsewhere).
    source_plan["nutrition"]["weight_cut_warning"]["risk_level"] = "red"
    outcome = build_structured_plan_outcome(
        source_plan, raw_markdown=_faithful_source(source_plan), computed_support=support
    )
    assert outcome.status == "valid"
    plan = outcome.structured_plan
    # Nutrition/recovery are Stage 1-only and never projected onto the card.
    assert plan.get("deterministic_support") is None
    blob = json.dumps(plan).lower()
    assert "coach_gated" not in blob
    assert "bicarbonate" not in blob


def test_outcome_without_computed_support_has_no_deterministic_support():
    plan = _valid_plan()
    outcome = build_structured_plan_outcome(plan, raw_markdown=_faithful_source(plan))
    assert outcome.status == "valid"
    assert outcome.structured_plan.get("deterministic_support") is None


def test_athlete_facing_strings_includes_stop_rules():
    from api.structured_plan_safety import athlete_facing_strings

    plan = {"weeks": [{"days": [{"sessions": [{"blocks": [{"stop_rules": ["Stop if punch speed collapses"]}]}]}]}]}
    assert "Stop if punch speed collapses" in athlete_facing_strings(plan)


def test_normalization_humanizes_contact_without_changing_machine_role_keys():
    from api.structured_plan_generation import normalize_structured_plan_candidate

    plan = _valid_plan()
    day = plan["weeks"][0]["days"][0]
    day["planning_day_role_keys"] = ["technical_combat"]
    day["today_card"]["headline"] = "technical_combat"
    day["today_card"]["coach_led_contact"] = "technical_combat"

    normalized = normalize_structured_plan_candidate(plan)
    rendered_day = normalized["weeks"][0]["days"][0]
    assert rendered_day["today_card"]["headline"] == "Technical Combat"
    assert rendered_day["today_card"]["coach_led_contact"] == "Technical Combat"
    assert rendered_day["planning_day_role_keys"] == ["technical_combat"]
    assert day["today_card"]["coach_led_contact"] == "technical_combat"


def test_cosmetic_identifiers_do_not_block_structured_card_release():
    plan = _valid_plan()
    plan["nutrition"]["summary"] = "opaque_internal_mode"
    plan["nutrition"]["daily_focus"] = "stop_and_modify"
    plan["weeks"][0]["days"][0]["today_card"]["primary_warning"] = "stop_and_report"
    plan["weeks"][0]["days"][0]["today_card"]["nutrition_summary"] = "stop_and_monitor"

    outcome = build_structured_plan_outcome(
        plan, raw_markdown=_faithful_source(plan), computed_support=_support()
    )
    assert outcome.status == "valid", outcome.errors
    assert outcome.structured_plan is not None
    assert not any("internal athlete-facing display label" in error for error in outcome.errors)


def test_known_internal_contact_identifier_is_normalized_before_release():
    plan = _valid_plan()
    plan["weeks"][0]["days"][0]["today_card"]["coach_led_contact"] = "technical_combat"
    outcome = build_structured_plan_outcome(
        plan, raw_markdown=_faithful_source(plan), computed_support=_support()
    )
    assert outcome.status == "valid", outcome.errors
    assert outcome.structured_plan["weeks"][0]["days"][0]["today_card"]["coach_led_contact"] == "Technical Combat"
