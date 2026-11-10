"""The permitted rep range and progression holds, on real generated plans."""
from __future__ import annotations

import copy
import datetime

import pytest

from api.exercise_identity import reconcile_exercise_keys, reconcile_progression_context
from api.rep_range import authored_rep_range
from api.services.exercise_log_service import _prescribed_snapshot
from api.structured_plan_deterministic_fallback import build_deterministic_structured_plan
from api.structured_plan_models import safe_parse_structured_plan
from support import _build_request

_HEALTHY = {"injuries": "", "fatigue_level": "low", "athlete": {"weight_kg": 70.0, "target_weight_kg": 70.0}}


def _camp(overrides: dict) -> tuple[dict, dict]:
    generate_plan_sync = pytest.importorskip("fightcamp.main").generate_plan_sync
    fight_date = (datetime.date.today() + datetime.timedelta(days=70)).isoformat()
    request = _build_request({**_HEALTHY, "fight_date": fight_date, **overrides}).to_payload()
    request["random_seed"] = 3
    brief = generate_plan_sync(request)["planning_brief"]
    return build_deterministic_structured_plan(brief), brief


def _strength_blocks(plan: dict):
    for week in plan["weeks"]:
        for day in week["days"]:
            for session in day.get("sessions") or []:
                for block in session.get("blocks") or []:
                    if block.get("block_type") in {"strength", "accessory"}:
                        yield week, block


@pytest.fixture(scope="module")
def healthy():
    return _camp({})


@pytest.mark.parametrize(("text", "expected"), [
    ("2–3x6–10 @ RPE 6–7, keep reps crisp.", (6, 10)),
    ("3–5x4–6 reps (or 6–10 throws) at crisp intent; rest 60–90s.", (4, 6)),
    ("3 x 6-8 per side @ RPE 6-8; rest 75s.", (6, 8)),
    ("3x8-12 @ 60-75% 1RM with slow eccentrics, tempo 3-1-1.", (8, 12)),
    # A choice, a compound dose, a hold, a fixed count or seconds abstain.
    ("2-4 sets x 6-10 reps or 20-40s tempo (3-1-3), RPE 6-8.", None),
    ("3–4 rounds: heavy movement 2–3 reps @ 85–92% 1RM → explosive movement 2–3 reps", None),
    ("3–5 holds x 10–20s @ 7–9/10 effort; full rest between holds.", None),
    ("2 x 20s hold @ RPE 6; rest 60s.", None),
    ("3 x 8", None),
    ("3 x 20-30s", None),
])
def test_only_one_unambiguous_authored_rep_range_is_read(text, expected):
    assert authored_rep_range(text) == expected


def test_a_healthy_camp_exposes_ranges_without_changing_the_prescription(healthy):
    plan, brief = healthy
    out = reconcile_exercise_keys(plan, brief)
    assert safe_parse_structured_plan(out).ok
    ranged = [(week, block) for week, block in _strength_blocks(out) if block.get("rep_range")]
    assert ranged, "a healthy GPP/SPP camp should carry permitted rep ranges"
    before = {id(b): b for _, b in _strength_blocks(plan)}
    for week, block in ranged:
        low, high = block["rep_range"]
        assert low <= block["reps"] <= high and low < high
        assert week["phase_label"] in {"GPP", "SPP"}
    # The prescription itself is untouched.
    def strip(p):
        return [{k: v for k, v in b.items() if k not in {"rep_range", "progression_hold"}} for _, b in _strength_blocks(p)]
    assert strip(out) == strip(plan)
    assert len(before) == len(list(_strength_blocks(out)))


def test_taper_is_held(healthy):
    plan, brief = healthy
    out = reconcile_progression_context(plan, brief)
    taper = [block for week, block in _strength_blocks(out) if week["phase_label"] == "TAPER"]
    assert taper and all(block.get("progression_hold") is True and "rep_range" not in block for block in taper)


@pytest.mark.parametrize("overrides", [
    {"athlete": {"weight_kg": 76.0, "target_weight_kg": 70.0}},
    {"injuries": "mild left shoulder irritation"},
], ids=["weight_cut", "injury"])
def test_a_weight_cut_or_injury_holds_every_strength_block(overrides):
    plan, brief = _camp(overrides)
    out = reconcile_progression_context(plan, brief)
    blocks = [block for _, block in _strength_blocks(out)]
    assert blocks and all(block.get("progression_hold") is True and "rep_range" not in block for block in blocks)


def test_read_time_context_keeps_identity_and_is_idempotent(healthy):
    plan, brief = healthy
    keyed = reconcile_exercise_keys(plan, brief)
    stale = copy.deepcopy(keyed)
    for _, block in _strength_blocks(stale):
        block["rep_range"] = [1, 30]  # a model or an old read cannot claim a range
        block["exercise_key"] = block.get("exercise_key")
    again = reconcile_progression_context(stale, brief)
    assert again == keyed
    assert reconcile_progression_context(again, brief) is again


def test_the_log_freezes_the_range_it_was_read_against(healthy):
    plan, brief = healthy
    block = next(b for _, b in _strength_blocks(reconcile_exercise_keys(plan, brief)) if b.get("rep_range"))
    assert _prescribed_snapshot(block)["rep_range"] == block["rep_range"]


def _with_athlete(brief: dict, **snapshot) -> dict:
    out = copy.deepcopy(brief)
    out["athlete_snapshot"] = {**(out.get("athlete_snapshot") or {}), **snapshot}
    return out


def test_a_routine_weight_cut_weeks_out_does_not_freeze_progression(healthy):
    plan, brief = healthy
    routine = _with_athlete(brief, weight_cut_risk=True, cut_severity_bucket="low",
                            readiness_flags=["active_weight_cut"])
    blocks = [block for week, block in _strength_blocks(reconcile_exercise_keys(plan, routine))
              if week["phase_label"] in {"GPP", "SPP"}]
    assert any(block.get("rep_range") for block in blocks)


@pytest.mark.parametrize("snapshot", [
    {"weight_cut_risk": True, "cut_severity_bucket": "moderate"},
    {"weight_cut_risk": True, "cut_severity_bucket": "high"},
    {"weight_cut_risk": True, "cut_severity_bucket": "critical"},
    {"weight_cut_risk": True, "cut_severity_bucket": "low", "readiness_flags": ["aggressive_weight_cut"]},
    {"fatigue": "high"},
], ids=["moderate_cut", "high_cut", "critical_cut", "flagged_aggressive_cut", "high_fatigue"])
def test_a_capacity_restricting_cut_or_high_fatigue_still_holds_everything(healthy, snapshot):
    plan, brief = healthy
    out = reconcile_exercise_keys(plan, _with_athlete(brief, **snapshot))
    blocks = [block for _, block in _strength_blocks(out)]
    assert blocks and all(block.get("progression_hold") is True and "rep_range" not in block for block in blocks)


def test_jumps_and_ballistic_lifts_are_held_even_when_labelled_strength(healthy):
    plan, brief = healthy
    out = reconcile_exercise_keys(plan, brief)
    hops = [block for _, block in _strength_blocks(out) if block.get("exercise_key") == "single-leg-forward-hops"]
    assert hops, "the generated camp should place the plyometric as a strength block"
    assert all(block["block_type"] == "strength" for block in hops)
    assert all(block.get("progression_hold") is True and "rep_range" not in block for block in hops)
    # An ordinary lift beside it keeps its range.
    squats = [block for week, block in _strength_blocks(out)
              if block.get("exercise_key") == "back-squat" and week["phase_label"] in {"GPP", "SPP"}]
    assert squats and all(block.get("rep_range") for block in squats)
