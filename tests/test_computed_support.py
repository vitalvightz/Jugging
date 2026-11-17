"""Tests for the Stage 1 computed-support carry-through (PR-4).

Stage 1 computes nutrition/mindset numbers deterministically. These tests pin
that those numbers are emitted as structured data and bundled into the planning
brief, with acute weight-cut / supplement dosing kept under a coach-gated
sub-section. Nutrition is Stage 1-only: the Stage 1 -> structured_plan
conversion prompt carries the mindset and weight-cut risk bands, never the
nutrition guidance.
"""

import asyncio
import json

from fightcamp.main import generate_plan
from fightcamp.mindset_module import compute_mindset_plan
from fightcamp.nutrition import compute_nutrition_targets
from fightcamp.stage2_payload import build_computed_support

from api.structured_plan_generation import build_structured_plan_prompt


def test_nutrition_targets_match_generator_formulas():
    targets = compute_nutrition_targets(flags={"weight": 80, "phase": "GPP"})
    # 1.6-2.0 g/kg protein in GPP -> exact computed numbers, not invented.
    assert targets["protein_g_per_day"] == {
        "min": 128.0,
        "max": 160.0,
        "per_kg": [1.6, 2.0],
        "note": None,
    }
    assert targets["carbs_g_per_day"]["min"] == 400.0  # 5 * 80
    assert targets["hydration_ml_per_day"]["min"] == 2400.0  # 30 * 80
    assert targets["fuel_timing"]["post"].startswith("within 1h")
    assert targets["weight_cut"]["active"] is False
    # No cut / no high fatigue -> nothing coach-gated.
    assert "coach_gated" not in targets


def test_missing_weight_yields_relative_only_targets():
    # Quick Build never collects bodyweight. Unknown weight must reduce
    # specificity: keep per-kg coefficients, mark the module
    # personalisation_limited, and never invent a 70 kg athlete or absolute
    # grams/millilitres.
    for phase in ("GPP", "SPP", "TAPER"):
        targets = compute_nutrition_targets(flags={"weight": None, "phase": phase})
        assert targets["weight_kg"] is None, phase
        assert targets["personalisation_limited"] is True, phase
        assert targets["personalisation_limited_reason"] == "missing_bodyweight"
        assert "provide bodyweight" in json.dumps(targets).lower()
        for macro in ("protein_g_per_day", "carbs_g_per_day", "fats_g_per_day",
                      "hydration_ml_per_day"):
            assert targets[macro]["min"] is None, (phase, macro)
            assert targets[macro]["max"] is None, (phase, macro)
    # Relative coefficients are retained so guidance stays useful.
    gpp = compute_nutrition_targets(flags={"weight": None, "phase": "GPP"})
    assert gpp["protein_g_per_day"]["per_kg"] == [1.6, 2.0]
    assert gpp["hydration_ml_per_day"]["per_kg_l"] == [0.03, 0.04]
    # A known weight keeps exact targets and is explicitly not limited.
    known = compute_nutrition_targets(flags={"weight": 80, "phase": "GPP"})
    assert known["personalisation_limited"] is False
    assert known["weight_kg"] == 80.0


def test_taper_macros_use_consistent_schema():
    # TAPER macros must use the same machine-readable shape as other phases:
    # every macro carries min/max/per_kg/note (even when some are None).
    targets = compute_nutrition_targets(flags={"weight": 70, "phase": "TAPER"})
    expected_keys = {"min", "max", "per_kg", "note"}
    for macro in ("carbs_g_per_day", "protein_g_per_day", "fats_g_per_day"):
        assert set(targets[macro]) == expected_keys, macro
    assert targets["carbs_g_per_day"] == {
        "min": None,
        "max": 350.0,  # 5 * 70
        "per_kg": [None, 5],
        "note": "reduce in days before weigh-in",
    }
    assert targets["fats_g_per_day"] == {
        "min": None,
        "max": None,
        "per_kg": None,
        "note": "moderate (~20% calories); reduce fiber 1-2 days out",
    }


def test_nutrition_acute_cut_and_supplements_are_coach_gated():
    targets = compute_nutrition_targets(
        flags={"weight": 70, "phase": "TAPER", "fatigue": "high",
               "weight_cut_risk": True, "weight_cut_pct": 7.0},
    )
    assert targets["weight_cut"] == {
        "active": True,
        "risk_band": "severe",
        "supervision_required": True,
    }
    gated = targets["coach_gated"]
    # Exact dosing lives only under coach_gated, never at the athlete-facing top.
    assert "acute_cut_protocol" in gated
    assert "bicarbonate_g_per_kg" in gated["acute_cut_protocol"]
    assert "high_fatigue_supplements" in gated
    top_level_blob = json.dumps({k: v for k, v in targets.items() if k != "coach_gated"})
    assert "bicarbonate" not in top_level_blob
    assert "magnesium" not in top_level_blob


def test_coach_gated_dosing_never_leaks_to_athlete_facing_fields():
    """Exact acute-cut + supplement dosing must live ONLY under coach_gated.

    Decision #2: athletes may see macros, hydration, fuel timing, weight-cut
    risk band, and the supervision warning — never exact dehydration / sauna /
    sodium / bicarbonate / supplement dosing. This proves those values do not
    appear in any athlete-facing field of the computed support.
    """
    flags = {
        "weight": 70, "phase": "TAPER", "fatigue": "high",
        "weight_cut_risk": True, "weight_cut_pct": 7.0, "age": 38,
    }
    nutrition = compute_nutrition_targets(flags=flags)

    # Exact acute-cut + supplement dosing that is coach/medical-gated only.
    sensitive = [
        "bicarbonate", "magnesium", "taurine", "mmol", "150%",
        "g_per_kg", "refeed",
    ]

    assert "coach_gated" in nutrition, "expected gated dosing for this scenario"
    athlete_facing = {k: v for k, v in nutrition.items() if k != "coach_gated"}
    blob = json.dumps(athlete_facing).lower()
    for token in sensitive:
        assert token.lower() not in blob, f"{token!r} leaked into athlete-facing fields"
    # The athlete-facing weight-cut summary is risk band + supervision only.
    assert set(nutrition["weight_cut"]) == {"active", "risk_band", "supervision_required"}

    # And via the full bundle the same holds for every active phase.
    support = build_computed_support(flags=flags, phases=["GPP", "SPP", "TAPER"])
    for phase_block in support["nutrition"]["by_phase"].values():
        athlete_facing = {k: v for k, v in phase_block.items() if k != "coach_gated"}
        blob = json.dumps(athlete_facing).lower()
        for token in sensitive:
            assert token.lower() not in blob, f"{token!r} leaked in bundle"


def test_mindset_plan_carries_phase_cues():
    plan = compute_mindset_plan({"mental_block": ["confidence"]})
    assert plan["primary_blocks"] == ["confidence"]
    assert set(plan["phase_cues"]) == {"GPP", "SPP", "TAPER"}
    assert set(plan["by_phase"]) == {"GPP", "SPP", "TAPER"}


def test_mindset_plan_carries_verbatim_athlete_note():
    plan = compute_mindset_plan(
        {
            "mental_block": ["confidence"],
            "mental_block_raw": "  I freeze up when I get caught early  ",
        }
    )
    # The athlete's own words are preserved (trimmed) so Stage 2 can personalise
    # the mindset_anchor instead of emitting only the generic phase cue.
    assert plan["athlete_note"] == "I freeze up when I get caught early"


def test_mindset_plan_athlete_note_defaults_to_empty():
    plan = compute_mindset_plan({"mental_block": ["confidence"]})
    assert plan["athlete_note"] == ""


def test_build_computed_support_only_includes_active_phases():
    support = build_computed_support(
        flags={"weight": 75, "mental_block": []}, phases=["GPP", "GPP"]
    )
    assert support["schema_version"] == "computed_support.v1"
    assert list(support["nutrition"]["by_phase"]) == ["GPP"]  # de-duped
    assert "recovery" not in support


def test_prompt_carries_computed_support_untruncated():
    # A planning brief big enough that the legacy 6000-char cap would clip the
    # computed support if it were serialized together with the rest.
    filler = {f"ctx_{i}": "x" * 200 for i in range(60)}
    support = build_computed_support(
        flags={"weight": 70, "phase": "GPP", "mental_block": ["confidence"]},
        phases=["GPP", "SPP", "TAPER"],
    )
    brief = {"schema_version": "planning_brief.v1", "computed_support": support, **filler}

    prompt = build_structured_plan_prompt(plan_markdown="PLAN", planning_brief=brief)

    assert "STAGE 1 COMPUTED SUPPORT" in prompt
    # The mindset block survives untruncated.
    assert json.dumps(support["mindset"], ensure_ascii=False, separators=(",", ":")) in prompt
    # Nutrition guidance never reaches Stage 2; only the weight-cut risk bands do.
    assert '"nutrition":{"by_phase"' not in prompt
    assert "protein_g_per_day" not in prompt
    assert '"weight_cut":{"by_phase"' in prompt
    # And the rest of the brief is still capped (filler partially clipped).
    assert "PLANNING BRIEF" in prompt


def test_prompt_without_computed_support_omits_the_injected_section():
    brief = {"schema_version": "planning_brief.v1", "fight_demands": {"sport": "mma"}}
    prompt = build_structured_plan_prompt(plan_markdown="PLAN", planning_brief=brief)
    assert "PLANNING BRIEF" in prompt
    # No serialized computed_support section is injected when none is present.
    # (The static authority rules may mention the phrase, so key off the data.)
    assert "computed_support.v1" not in prompt


def test_real_generation_attaches_computed_support_to_planning_brief():
    from support import _build_request

    result = asyncio.run(generate_plan(_build_request().to_payload()))
    support = result["planning_brief"]["computed_support"]

    assert support["schema_version"] == "computed_support.v1"
    assert support["nutrition"]["by_phase"], "nutrition numbers must be carried"
    assert "recovery" not in support
    assert "phase_cues" in support["mindset"]
