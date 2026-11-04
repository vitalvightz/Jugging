"""Deterministic doses must reach Stage 2 and be recognised on the way back.

Every failure these cover was observed in production, not hypothesised:

* Every conditioning bank entry serialised to an EMPTY prescription, because the
  serializer read ``timing``/``rest``/``load`` while the banks author their dose
  in ``duration``. Stage 2 was handed scheduled drills with no dose and had to
  invent one.
* ``_goal_witness_dose_matches`` demanded a time unit from every witness holding
  ``work_sec``/``rounds``, so a correctly rendered rep-shaped drill
  ("4 x 5/side") read as a missing witness.
* ``_parse_sets_reps`` read "2 x 20 m" as 20 reps, letting the countdown overlay
  rewrite a carry microdose and drop its metres.
* The deterministic card fallback published internal placement rationale as the
  athlete-facing objective.
"""

import json

import pytest

from fightcamp.role_labels import athlete_facing_label_for
from fightcamp.prescription_resolver import (
    _parse_sets_reps,
    resolve_strength_slot_prescription,
)
from fightcamp.stage2_payload import _serialize_conditioning_option
from fightcamp.stage2_validator import _goal_witness_dose_matches
from fightcamp.strength import DATA_DIR

_CONDITIONING_BANKS = (
    "conditioning_bank.json",
    "style_conditioning_bank.json",
    "technical_footwork_bank.json",
)


def _bank(name: str) -> list[dict]:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Conditioning drills must carry their authored dose into Stage 2.
# --------------------------------------------------------------------------- #
def test_no_conditioning_entry_serialises_to_an_empty_prescription():
    missing = []
    for bank_name in _CONDITIONING_BANKS:
        for drill in _bank(bank_name):
            option = _serialize_conditioning_option(drill, "aerobic", "why")
            if not str(option.get("prescription") or "").strip():
                missing.append(f"{bank_name}:{drill.get('name')}")
    assert not missing, f"{len(missing)} drills reach Stage 2 with no dose: {missing[:10]}"


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Alternating Bounds", "4x5/side, 2min rest"),
        ("Nasal Shadowboxing Flow (Gas Tank)", "12-20min continuous"),
    ],
)
def test_authored_duration_is_carried_verbatim(name, expected):
    drill = next(d for d in _bank("conditioning_bank.json") if d.get("name") == name)
    assert _serialize_conditioning_option(drill, "aerobic", "why")["prescription"] == expected


def test_explicit_timing_still_wins_over_duration():
    """The existing timing/rest/load contract is unchanged where it is authored."""
    drill = {"name": "X", "timing": "8 x 8s", "rest": "90s", "duration": "ignored"}
    assert _serialize_conditioning_option(drill, "alactic", "why")["prescription"] == "8 x 8s | 90s"


# --------------------------------------------------------------------------- #
# A rep-shaped authored dose must be recognised when rendered rep-shaped.
# --------------------------------------------------------------------------- #
REP_WITNESS = {
    "name": "Alternating Bounds",
    "rounds": 4,
    "work_sec": 5,
    "rest_sec": 120,
    "effective_prescription": "4x5/side, 2min rest",
}
TIMED_WITNESS = {
    "name": "Range Reset Intervals",
    "rounds": 6,
    "work_sec": 45,
    "rest_sec": 45,
    "effective_prescription": "45s work / 45s rest x 6 rounds",
}


@pytest.mark.parametrize(
    "rendered",
    [
        "4 x 5/side; work ~5 s per rep; rest 120 s between sets; RPE 6-7 max.",
        "4 x 5/side, 2 min rest. 5 sec work bursts. RPE 6-7.",
        "4 x 5/side; 2 min rest between sets.",
        "4 x 5/side, rest 2:00.",
        "5 x 6/side, rest 2:00.",  # above the authored dose is still a match
    ],
)
def test_rep_shaped_witness_matches_a_rep_shaped_rendering(rendered):
    assert _goal_witness_dose_matches(REP_WITNESS, rendered)


@pytest.mark.parametrize("rendered", ["3 x 5/side, rest 2:00.", "4 x 3/side, rest 2:00."])
def test_rep_shaped_witness_still_rejects_under_dosing(rendered):
    """The matcher is corrected, not weakened."""
    assert not _goal_witness_dose_matches(REP_WITNESS, rendered)


@pytest.mark.parametrize(
    "rendered, expected",
    [
        ("6 rounds x 45 seconds, rest 45 sec", True),
        ("6 rounds x 30 seconds, rest 45 sec", False),  # under work_sec
        ("4 x 5/side, rest 2:00.", False),              # rep-shaped ≠ timed witness
    ],
)
def test_timed_witness_keeps_strict_timed_matching(rendered, expected):
    assert _goal_witness_dose_matches(TIMED_WITNESS, rendered) is expected


# --------------------------------------------------------------------------- #
# A distance/time dose is never read as reps.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "dose, expected",
    [
        ("2 x 20 m @ RPE 7", (None, None)),
        ("2 x 20 km", (None, None)),
        ("3 x 30s hold", (None, None)),
        ("3 x 45 seconds", (None, None)),
        ("4 x 3 @ RPE 7", (4, 3)),
        ("3x8-12 @ 60-75% 1RM", (3, 8)),
        ("4 x 6 reps", (4, 6)),
    ],
)
def test_parse_sets_reps_ignores_a_unit_bearing_second_term(dose, expected):
    assert _parse_sets_reps(dose) == expected


def test_carry_dose_keeps_its_unit_through_the_countdown_overlay():
    """A distance-dosed carry slot keeps its metres when the countdown band applies."""
    role = {
        "role_key": "primary_strength_day",
        "category": "strength",
        "selected_exercise_assignments": [],
        "scheduled_d_day": 16,
        "strength_dose_cap": {"max_sets": 3, "max_reps": 3, "loaded_allowed": True},
        "rpe_cap": "6-7",
    }
    slot = {
        "slot_id": "spp_strength_carry",
        "priority": 1,
        "session_index": 1,
        "quality_class": "anchor_loaded",
        "anchor_capable": True,
        "selected": {
            "name": "Loaded Carry Touch",
            "prescription": "2 x 20 m @ RPE 7",
            "quality_class": "anchor_loaded",
            "anchor_capable": True,
        },
    }
    resolved = resolve_strength_slot_prescription(role=role, slot=slot, athlete_state={})
    assert resolved["effective_prescription"] == "2 x 20 m @ RPE 7"

    # Still reduce-only: a tighter band lowers the carry count, keeps the unit.
    role["strength_dose_cap"] = {"max_sets": 1, "max_reps": 3, "loaded_allowed": True}
    tightened = resolve_strength_slot_prescription(role=role, slot=slot, athlete_state={})
    assert tightened["effective_prescription"] == "1 x 20 m @ RPE 7"


# --------------------------------------------------------------------------- #
# Internal placement rationale is never the athlete-facing objective.
# --------------------------------------------------------------------------- #
def test_deterministic_card_objective_is_not_internal_placement_rationale():
    from api.structured_plan_deterministic_fallback import _session

    role = {
        "role_key": "primary_strength_day",
        "category": "strength",
        "athlete_facing_label": "Primary Strength",
        "session_index": 1,
        "day_assignment_reason": (
            "Use the lowest-load day immediately before the primary strength anchor."
        ),
        "selected_exercise_assignments": [
            {"name": "Trap Bar Deadlift", "effective_prescription": "4 x 3 @ RPE 7"}
        ],
    }
    session = _session(role, 20)
    assert session is not None
    assert "lowest-load" not in session["objective"]
    assert "anchor" not in session["objective"].lower()
    # The canonical athlete-facing role label, not planner rationale.
    assert session["objective"] == athlete_facing_label_for("primary_strength_day")
    assert session["objective"] == session["title"]
    # The internal field itself is preserved for audit.
    assert role["day_assignment_reason"]


# --------------------------------------------------------------------------- #
# Render integrity: an aside must not become a second athlete-facing card.
# --------------------------------------------------------------------------- #
def test_unprescribed_duplicate_block_is_collapsed_into_the_prescribed_one():
    """Stage 2's explanatory aside became a second, empty exercise card.

    The plan text carried one real prescription plus a later
    "Note: Loaded Carry Touch is a scheduled microdose ..."; conversion turned
    the aside into a second block with the same name and no dose.
    """
    from api.structured_plan_generation import _normalize_session

    session = _normalize_session({
        "session_id": "s1",
        "blocks": [
            {"block_id": "b1", "display_name": "Loaded Carry Touch", "sets": 2,
             "distance": {"value": 20, "unit": "meters"},
             "coaching_cues": ["tall posture"]},
            {"block_id": "b2", "display_name": "Trap Bar Deadlift", "sets": 4, "reps": 3},
            {"block_id": "b3", "display_name": "Loaded Carry Touch",
             "coaching_cues": ["Scheduled microdose for strength exposure."]},
        ],
    })
    names = [b["display_name"] for b in session["blocks"]]
    assert names.count("Loaded Carry Touch") == 1, names
    carry = next(b for b in session["blocks"] if b["display_name"] == "Loaded Carry Touch")
    assert carry["sets"] == 2  # the prescribed block survives, not the empty one
    # The aside's coaching detail is kept, not discarded.
    assert "Scheduled microdose for strength exposure." in carry["coaching_cues"]


def test_two_genuinely_prescribed_blocks_are_not_silently_collapsed():
    """A real duplicate stays visible to duplicate detection."""
    from api.structured_plan_generation import _normalize_session

    session = _normalize_session({
        "session_id": "s2",
        "blocks": [
            {"block_id": "b1", "display_name": "Easy Assault Bike",
             "duration": {"value": 20, "unit": "minutes"}},
            {"block_id": "b2", "display_name": "Easy Assault Bike",
             "duration": {"value": 10, "unit": "minutes"}},
        ],
    })
    assert len(session["blocks"]) == 2



def test_identical_tactical_blocks_are_collapsed_without_changing_distinct_work():
    from api.structured_plan_generation import _normalize_session

    tactical = {"display_name": "Opponent Pattern Scan", "block_type": "mindset",
                "duration": {"value": 10, "unit": "minutes"},
                "coaching_cues": ["Identify repeated patterns."]}
    session = _normalize_session({"blocks": [
        {"block_id": "a", **tactical},
        {"block_id": "b", **tactical},
        {"block_id": "c", **tactical, "duration": {"value": 5, "unit": "minutes"}},
        {"block_id": "d", "display_name": "Plank", "block_type": "strength", "sets": 2, "reps": 6},
        {"block_id": "e", "display_name": "Plank", "block_type": "strength", "sets": 2, "reps": 6},
    ]})
    assert [block["block_id"] for block in session["blocks"]] == ["a", "c", "d", "e"]


# --------------------------------------------------------------------------- #
# Deterministic conditioning dose must survive Stage 2 verbatim.
# --------------------------------------------------------------------------- #
def test_conditioning_dose_drift_is_detected_now_that_expected_is_populated():
    """The drift check was dead code while every expected dose was empty.

    Double-End Bag Circuit is authored "5x2min rounds with 30s transitions";
    Stage 2 rendered 5 x 3 min, a 50% increase in work duration.
    """
    from fightcamp.stage2_validator import _conditioning_dose_within_bounds

    drill = next(
        d for d in _bank("conditioning_bank.json") if d.get("name") == "Double-End Bag Circuit"
    )
    expected = _serialize_conditioning_option(drill, "glycolytic", "why")["prescription"]
    assert expected, "an empty expected dose silently disables the drift check"

    faithful, _, _, _ = _conditioning_dose_within_bounds(
        expected, "- Double-End Bag Circuit: 5 x 2 min, 60s rest"
    )
    assert faithful

    drifted, violations, _, _ = _conditioning_dose_within_bounds(
        expected, "- Double-End Bag Circuit: 5 x 3 min, 60s rest"
    )
    assert not drifted
    assert violations


# --------------------------------------------------------------------------- #
# Annotation labels are never read as exercise names.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "line",
    [
        "Purpose: build aerobic base over 20 min",
        "- Purpose: build aerobic base over 20 min",
        "  - Easier: drop to 3 sets",
        "* Stop: sharp pain, end the set",
        "- **Why today:** keep speed sharp",
        "- Regression/stop: halve the rounds",
    ],
)
def test_bulleted_annotation_lines_are_not_exercises(line):
    """A bulleted annotation escaped the check and was reported as an exercise
    whose name was the label itself ("Purpose", "Easier")."""
    from fightcamp.stage2_validator import (
        _late_fight_line_is_annotation_or_task,
        _late_fight_line_is_exercise_like,
    )

    assert _late_fight_line_is_annotation_or_task(line)
    assert not _late_fight_line_is_exercise_like(line)


@pytest.mark.parametrize(
    "line",
    [
        "- Trap Bar Deadlift: 4 x 3 @ RPE 7",
        "- Easy Assault Bike — 20 min easy",
        "- Alternating Bounds: 4 x 5/side, 2 min rest",
    ],
)
def test_real_exercise_lines_are_still_exercises(line):
    from fightcamp.stage2_validator import (
        _late_fight_line_is_annotation_or_task,
        _late_fight_line_is_exercise_like,
    )

    assert not _late_fight_line_is_annotation_or_task(line)
    assert _late_fight_line_is_exercise_like(line)
