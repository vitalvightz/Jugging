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


# --- Fail-closed classification, older plans, and athlete changes -----------------

from api.rep_range import bank_allows_progression, override_progression_hold  # noqa: E402
from api.services.exercise_overrides import apply_exercise_overrides  # noqa: E402


@pytest.mark.parametrize(("exercise", "expected"), [
    ({"exercise_key": "back-squat"}, True),
    ({"name": "Back Squat"}, True),
    ({"display_name": "Feet-Elevated Push-Up"}, True),
    # Jumps, ballistic lifts and speed-intent power work, by tag or by method.
    ({"exercise_key": "single-leg-forward-hops"}, False),
    ({"name": "Barbell Thruster"}, False),
    ({"name": "High Pull"}, False),
    ({"name": "Heavy RDL → Broad Jump"}, False),
    ({"name": "Sled Push"}, False),
    # Not in the strength bank: unclassified, never assumed safe.
    ({"exercise_key": "my-own-invention", "display_name": "My Own Invention"}, None),
    ({}, None),
])
def test_the_exercise_bank_classifies_what_may_progress(exercise, expected):
    assert bank_allows_progression(exercise) is expected


def test_cleared_blocks_say_so_and_everything_else_is_held_or_unknown(healthy):
    plan, brief = healthy
    out = reconcile_exercise_keys(plan, brief)
    for week, block in _strength_blocks(out):
        if block.get("rep_range"):
            assert block["progression_hold"] is False
        if week["phase_label"] == "TAPER":
            assert block["progression_hold"] is True
    assert any(block.get("progression_hold") is False for _, block in _strength_blocks(out))


def test_an_older_brief_without_mechanical_tags_still_holds_jumps(healthy):
    plan, brief = healthy
    old = copy.deepcopy(brief)
    for week in old["weekly_role_map"]["weeks"]:
        for role in week["session_roles"]:
            for assignment in role.get("selected_exercise_assignments") or []:
                assignment.pop("mechanical_risk_tags", None)
    out = reconcile_exercise_keys(plan, old)
    hops = [b for _, b in _strength_blocks(out) if b.get("exercise_key") == "single-leg-forward-hops"]
    assert hops and all(b.get("progression_hold") is True and "rep_range" not in b for b in hops)


def test_a_block_the_planner_cannot_classify_is_left_unknown_not_cleared(healthy):
    plan, brief = healthy
    unknown = copy.deepcopy(brief)
    for week in unknown["weekly_role_map"]["weeks"]:
        for role in week["session_roles"]:
            for assignment in role.get("selected_exercise_assignments") or []:
                if assignment.get("name") == "Back Squat":
                    assignment["name"] = "Mystery Squat Variation"
                    assignment["exercise_key"] = "mystery-squat-variation"
    renamed = copy.deepcopy(plan)
    for _, block in _strength_blocks(renamed):
        if block.get("display_name") == "Back Squat":
            block["display_name"] = "Mystery Squat Variation"
    out = reconcile_exercise_keys(renamed, unknown)
    mystery = [b for w, b in _strength_blocks(out)
               if b.get("exercise_key") == "mystery-squat-variation" and w["phase_label"] in {"GPP", "SPP"}]
    assert mystery and all("progression_hold" not in b and "rep_range" not in b for b in mystery)


_SQUAT = {"block_id": "b1", "block_type": "strength", "display_name": "Back Squat", "exercise_key": "back-squat",
          "sets": 3, "reps": 8}
_GOBLET = {"block_type": "strength", "display_name": "Goblet Squat", "exercise_key": "goblet-squat", "sets": 3, "reps": 8}
_HOPS = {"block_type": "strength", "display_name": "Single-Leg Forward Hops",
         "exercise_key": "single-leg-forward-hops", "sets": 3, "reps": 4}
_UNKNOWN = {"block_type": "strength", "display_name": "My Own Invention", "sets": 3, "reps": 8}


def _changed(action: str, new_block: dict | None, *, original: dict, extra: list[dict] | None = None) -> dict:
    """The session after one athlete change, through the real override reader."""
    session = {"session_id": "s1", "session_type": "strength_power", "title": "Strength",
               "blocks": [copy.deepcopy(original), *copy.deepcopy(extra or [])]}
    plan = {"weeks": [{"days": [{"date": "2026-11-12", "sessions": [session]}]}]}
    entry = {"id": "ovr1", "action": action, "scope": "occurrence", "date": "2026-11-12", "session_id": "s1",
             "block_id": original["block_id"], "original": original}
    if new_block is not None:
        entry["block"] = new_block
    out = apply_exercise_overrides({"exercise_overrides": [entry]}, plan)
    return out["weeks"][0]["days"][0]["sessions"][0]


def test_bank_fixture_names_exist():
    assert bank_allows_progression(_GOBLET) is True and bank_allows_progression(_HOPS) is False


@pytest.mark.parametrize(("original_hold", "new_block", "expected"), [
    (False, _GOBLET, None),     # nothing held: no verdict, as for any unclassified block
    (True, _GOBLET, True),      # an existing hold survives the swap
    (None, _GOBLET, None),
    (False, _HOPS, True),       # ballistic by the bank
    (True, _UNKNOWN, True),     # a hold survives even onto an exercise the bank does not know
    (False, _UNKNOWN, None),    # unknown stays unknown
], ids=["cleared", "held_original", "unknown_original", "ballistic_swap", "held_unclassified", "unclassified_swap"])
def test_a_swap_keeps_holds_and_checks_the_bank(original_hold, new_block, expected):
    original = {**_SQUAT, "rep_range": [8, 12]}
    if original_hold is not None:
        original["progression_hold"] = original_hold
    swapped = _changed("swap", new_block, original=original)["blocks"][0]
    assert swapped["display_name"] == new_block["display_name"]
    assert swapped.get("progression_hold") is expected
    assert "rep_range" not in swapped  # the athlete's own choice of dose is fixed


def test_a_stored_change_cannot_carry_its_own_verdict_or_range():
    """An edit is saved as a copy of the block as it was that day; neither field is trusted on read."""
    stale = {**_SQUAT, "reps": 10, "rep_range": [8, 12], "progression_hold": False}
    edited = _changed("edit", stale, original={**_SQUAT, "progression_hold": True})["blocks"][0]
    assert edited["reps"] == 10 and edited["progression_hold"] is True and "rep_range" not in edited
    # And a stale hold saved on a taper day does not follow the edit into a cleared week.
    freed = _changed("edit", {**stale, "progression_hold": True}, original={**_SQUAT, "progression_hold": False})["blocks"][0]
    assert "progression_hold" not in freed and "rep_range" not in freed


def test_an_added_exercise_takes_the_sessions_hold_and_the_banks_class():
    cleared = {**_SQUAT, "progression_hold": False}
    held = {**_SQUAT, "progression_hold": True}

    def added(new_block, *originals):
        session = {"session_id": "s1", "session_type": "strength_power", "title": "Strength", "blocks": copy.deepcopy(list(originals))}
        plan = {"weeks": [{"days": [{"date": "2026-11-12", "sessions": [session]}]}]}
        entry = {"id": "ovr2", "action": "add", "scope": "occurrence", "date": "2026-11-12", "session_id": "s1", "block": new_block}
        return apply_exercise_overrides({"exercise_overrides": [entry]}, plan)["weeks"][0]["days"][0]["sessions"][0]["blocks"][-1]

    assert added(_GOBLET, held).get("progression_hold") is True       # taper, cut, injury: the session is held
    assert added(_UNKNOWN, held).get("progression_hold") is True
    assert added(_HOPS, cleared).get("progression_hold") is True      # ballistic by the bank
    assert "progression_hold" not in added(_GOBLET, cleared)
    assert "progression_hold" not in added(_GOBLET, _SQUAT)
    assert "progression_hold" not in added(_UNKNOWN, cleared)
    # One held jump beside a cleared lift does not hold the whole session.
    assert "progression_hold" not in added(_GOBLET, cleared, {**_HOPS, "block_id": "h", "progression_hold": True})
    # A claim on the added block itself counts for nothing.
    assert added({**_GOBLET, "progression_hold": False, "rep_range": [1, 30]}, held)["progression_hold"] is True


def test_another_athlete_change_is_not_evidence_about_the_session():
    sibling = {**_GOBLET, "block_id": "x", "progression_hold": True, "athlete_change": {"action": "add"}}
    assert override_progression_hold(_GOBLET, replaced=None, siblings=[sibling]) is None
