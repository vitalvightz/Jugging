from __future__ import annotations

from fightcamp.nutrition import _is_high_pressure_weight_cut as nutrition_high_pressure_cut
from fightcamp.nutrition import compute_nutrition_targets, generate_nutrition_block
from fightcamp.weight_cut import (
    WEIGHT_CUT_INPUTS_MISSING_TARGET,
    compute_weight_cut_pct,
    parse_weight_value,
)


def test_nutrition_high_pressure_weight_cut_detects_multiple_triggers():
    assert not nutrition_high_pressure_cut({"weight_cut_risk": False, "weight_cut_pct": 7.0})
    # Magnitude alone no longer short-circuits this. A 5.1% cut with no
    # countdown is scored at the default far-out distance, where it is a
    # routine cut; the bare ``>= 5.0`` rule used to flag it regardless.
    assert not nutrition_high_pressure_cut({"weight_cut_risk": True, "weight_cut_pct": 5.1})
    assert not nutrition_high_pressure_cut(
        {"weight_cut_risk": True, "weight_cut_pct": 5.1, "fatigue": "low", "days_until_fight": 40}
    )
    # The same cut near the fight is strain-escalated and still flags.
    assert nutrition_high_pressure_cut(
        {"weight_cut_risk": True, "weight_cut_pct": 5.1, "fatigue": "low", "days_until_fight": 6}
    )
    # So does a genuinely large cut at any distance.
    assert nutrition_high_pressure_cut(
        {"weight_cut_risk": True, "weight_cut_pct": 12.0, "fatigue": "low", "days_until_fight": 40}
    )
    assert nutrition_high_pressure_cut(
        {"weight_cut_risk": True, "weight_cut_pct": 2.0, "fatigue": "moderate", "days_until_fight": 40}
    )
    # A low-fatigue, non-aggressive active cut is only high-pressure inside the final
    # two weeks (<=14) — a routine cut at D-21 is not.
    assert not nutrition_high_pressure_cut(
        {"weight_cut_risk": True, "weight_cut_pct": 2.0, "fatigue": "low", "days_until_fight": 21}
    )
    assert not nutrition_high_pressure_cut(
        {"weight_cut_risk": True, "weight_cut_pct": 2.0, "fatigue": "low", "days_until_fight": 15}
    )
    assert nutrition_high_pressure_cut(
        {"weight_cut_risk": True, "weight_cut_pct": 2.0, "fatigue": "low", "days_until_fight": 14}
    )


def test_generate_nutrition_block_includes_phase_fatigue_and_cut_sections():
    block = generate_nutrition_block(
        flags={
            "phase": "SPP",
            "weight": 72,
            "fatigue": "moderate",
            "weight_cut_risk": True,
            "weight_cut_pct": 5.4,
            "days_until_fight": 18,
        }
    )

    assert "Nutrition Module" in block
    assert "**Active Weight-Cut Note:**" in block
    assert "This is a high-pressure cut window" in block
    assert "**SPP Phase Focus:**" in block
    assert "**Moderate Fatigue Adjustments:**" in block
    assert "**Weight Cut Protocol Triggered:**" in block


def test_generate_nutrition_block_handles_taper_high_fatigue_branch():
    block = generate_nutrition_block(
        flags={
            "phase": "TAPER",
            "weight": 66,
            "fatigue": "high",
            "weight_cut_risk": False,
        }
    )

    assert "**Taper Phase Focus:**" in block
    assert "**High Fatigue in Taper:**" in block
    assert "Light electrolyte intake only" in block


def test_generate_nutrition_block_handles_missing_weight():
    # Quick Build never collects body weight, so the athlete model carries
    # weight=None. Regression: 1.7 * None crashed Stage 1 inside the
    # nutrition module. Missing weight must reduce specificity — per-kg
    # guidance only — never fabricate a 70 kg athlete and compute absolute
    # grams/millilitres from it.
    block = generate_nutrition_block(
        flags={
            "phase": "GPP",
            "fatigue": "low",
            "weight": None,
            "weight_cut_risk": False,
            "weight_cut_pct": 0.0,
        }
    )

    assert "Personalisation limited: bodyweight not provided" in block
    assert "Protein intake: 1.7-2.2 g/kg" in block
    assert "exact daily targets need your bodyweight" in block
    # No fabricated absolute targets anywhere (would be 70 kg-derived).
    assert "g/day" not in block
    assert "ml/day" not in block
    assert "119.0" not in block


def test_generate_nutrition_block_never_emits_coach_gated_dosing():
    # The athlete-facing markdown layer must carry risk bands, supervision
    # requirements, and general recovery priorities only. Exact acute-cut and
    # supplement dosing is coach/medical-gated (compute_nutrition_targets
    # nests it under coach_gated) and must never render here.
    # NOTE: generic per-kg fueling coefficients (e.g. "~0.3 g/kg protein" in
    # meal timing) are athlete-safe by the authority model; only the acute-cut
    # and supplement dosing markers below are gated.
    sentinel_tokens = (
        "magnesium", "taurine", "bicarbonate", "mmol", "150%", "refeed",
        "8-12 g/kg", "500-700 mg", "0.6-0.9 l",
    )
    for phase in ("GPP", "SPP", "TAPER"):
        for weight in (70, None):
            block = generate_nutrition_block(
                flags={
                    "phase": phase,
                    "weight": weight,
                    "fatigue": "high",
                    "weight_cut_risk": True,
                    "weight_cut_pct": 6.5,
                    "days_until_fight": 10,
                }
            ).lower()
            for token in sentinel_tokens:
                assert token not in block, f"{token!r} leaked in {phase} block"

    block = generate_nutrition_block(
        flags={
            "phase": "TAPER",
            "weight": 70,
            "fatigue": "high",
            "weight_cut_risk": True,
            "weight_cut_pct": 6.5,
            "days_until_fight": 10,
        }
    )
    assert "**Weight Cut Protocol Triggered:**" in block
    assert "risk band SEVERE" in block
    assert "requires qualified coach/medical supervision" in block


def test_parse_weight_value_returns_zero_for_blank_or_invalid_inputs():
    assert parse_weight_value(None) == 0.0
    assert parse_weight_value("") == 0.0
    assert parse_weight_value("no number here") == 0.0


def test_compute_weight_cut_pct_clamps_negative_and_zero_current_weight():
    assert compute_weight_cut_pct(0, 65) == 0.0
    assert compute_weight_cut_pct("64 kg", "66 kg") == 0.0
    assert compute_weight_cut_pct("70kg", "66kg") == 5.7


def test_nutrition_targets_report_unknown_cut_when_target_weight_missing():
    targets = compute_nutrition_targets(
        flags={
            "weight": 88.0,
            "phase": "GPP",
            "fatigue": "low",
            "weight_cut_risk": False,
            "weight_cut_pct": 0.0,
            "weight_cut_status": WEIGHT_CUT_INPUTS_MISSING_TARGET,
            "days_until_fight": 42,
        }
    )

    # Not "no active cut" — the data to decide that was never collected.
    assert targets["weight_cut"]["known"] is False
    assert targets["weight_cut"]["unknown_reason"] == WEIGHT_CUT_INPUTS_MISSING_TARGET
    assert "No target weight set" in targets["weight_cut"]["note"]
    # An unknown cut never escalates or demands supervision.
    assert targets["weight_cut"]["active"] is False
    assert targets["weight_cut"]["risk_band"] == "none"
    assert targets["weight_cut"]["supervision_required"] is False
    assert "coach_gated" not in targets


def test_nutrition_targets_mark_cut_known_when_both_weights_present():
    targets = compute_nutrition_targets(
        flags={
            "weight": 88.0,
            "phase": "GPP",
            "fatigue": "low",
            "weight_cut_risk": True,
            "weight_cut_pct": 3.5,
            "days_until_fight": 42,
        }
    )

    # A known cut keeps the tight athlete-facing shape — the unknown-state keys
    # are added only when there is genuinely no cut data.
    assert set(targets["weight_cut"]) == {"active", "risk_band", "supervision_required"}
    assert targets["weight_cut"].get("known", True) is True


def test_escalation_cut_pct_is_the_strain_scale_boundary_inverted():
    from fightcamp.weight_cut import (
        CUT_MAGNITUDE_FLOOR_PCT,
        HEALTH_ESCALATION_SCORE,
        compute_cut_severity_score,
        escalation_cut_pct,
        weight_cut_risk_band,
    )

    for days in (1, 7, 14, 21):
        pct = escalation_cut_pct(days)
        assert pct < CUT_MAGNITUDE_FLOOR_PCT
        assert abs(compute_cut_severity_score(pct, days) - HEALTH_ESCALATION_SCORE) <= 0.1
        assert weight_cut_risk_band(True, pct - 0.05, days) == "moderate"
        assert weight_cut_risk_band(True, pct + 0.05, days) == "high"
    # Closer to the fight, less of the cut can remain before it escalates.
    assert escalation_cut_pct(1) < escalation_cut_pct(7) < escalation_cut_pct(21)
    # Far out, the magnitude floor is the limit.
    assert escalation_cut_pct(28) == CUT_MAGNITUDE_FLOOR_PCT
    assert escalation_cut_pct(60) == CUT_MAGNITUDE_FLOOR_PCT


def test_weight_cut_status_reads_the_canonical_band():
    from api.nutrition_workspace import _weight_cut_status
    from fightcamp.weight_cut import compute_weight_cut_pct, weight_cut_risk_band

    def status(current, target, days):
        pct = compute_weight_cut_pct(current, target)
        return _weight_cut_status(
            current_weight_kg=current,
            target_weight_kg=target,
            days_until_fight=days,
            risk_band=weight_cut_risk_band(pct >= 3.0, pct, days),
        )

    assert status(None, 70.0, 10) == ("none", None, None)
    assert status(72.0, None, 10) == ("none", None, None)
    assert status(72.0, 70.0, None) == ("none", None, None)
    assert status(72.0, 70.0, -1) == ("none", None, None)
    assert status(69.8, 70.0, 10) == ("on_target", 0.0, None)
    assert status(72.5, 70.0, 19) == ("on_track", 2.5, None)
    # 4.5% with a week to go is above the 4.15% the strain scale allows at D-7.
    state, to_target, behind = status(73.3, 70.0, 7)
    assert (state, to_target) == ("behind", 3.3)
    assert behind == round(73.3 - 70.0 / (1 - 0.0415), 1)
    # A lighter weigh-in recovers the status.
    assert status(72.9, 70.0, 7)[0] == "on_track"
    assert status(80.0, 74.0, 21) == ("too_aggressive", 6.0, None)
