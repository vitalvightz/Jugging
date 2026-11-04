"""Focused regression coverage for authoritative locked Tactical Watch merging."""
from __future__ import annotations

from copy import deepcopy

import pytest

from api.structured_plan_faithfulness import check_structured_faithfulness
from api.structured_plan_generation import build_structured_plan_outcome
from api.structured_plan_locked_merge import merge_locked_structured_content, merge_planner_owned_structured_content
from api.structured_plan_models import safe_parse_structured_plan

WHY = "Know what happens after the first punches so pocket exchanges stay planned rather than chaotic."
STEPS = [
    "Identify the opponent's most common pocket sequence.",
    "Choose your answer to that sequence.",
    "Choose the finishing shot that best fits the opening.",
    "Decide whether that exchange should end with an exit or a smother.",
]
MINDSET = {
    "intent": "Win the second decision inside the pocket.",
    "focus": "Watch the opponent's response after the first two punches.",
    "reset": "If the exchange loses shape, smother or leave instead of trading blindly.",
    "anchor": "Know the next beat.",
    "context": "SPP pocket planning for a brawler.",
}
PROGRESS = "Rehearse the chosen exchange ending, not just the opening combination."
SOURCE = """D-11 (Tuesday): Tactical Focus
Why: {why}
- Pocket Exchange Map: 10 minutes, tactical review only. No physical load.
  Step 1: {step0}
  Step 2: {step1}
  Step 3: {step2}
  Step 4: {step3}
  Intent: {intent}
  Focus: {focus}
  Reset: {reset}
  Anchor: {anchor}
  Purpose: {context}
  Progress: {progress}
""".format(why=WHY, step0=STEPS[0], step1=STEPS[1], step2=STEPS[2], step3=STEPS[3], progress=PROGRESS, **MINDSET)


def _brief(*, locked=True, day="D-11"):
    return {"weeks": [{"session_roles": [{
        "scheduled_countdown_label": day,
        "countdown_label": "D-99",  # scheduled label has authority
        "display_text": "\n".join(SOURCE.splitlines()[1:]),
        "governance": {
            "selected_drill_locked": locked,
            "selected_drill_name": "Pocket Exchange Map",
            "render_selected_drill_exactly": True,
            "do_not_reselect_or_generalize": True,
        },
        "tactical_watch": {
            "name": "Pocket Exchange Map", "why": WHY, "duration_min": 10,
            "instructions": STEPS, "mindset": MINDSET, "progress": PROGRESS,
        },
    }]}]}


def _plan(*, day="D-11", include=True):
    blocks = [] if not include else [{
        "block_id": "watch-1", "block_type": "mindset",
        "display_name": "Pocket Exchange Map", "duration": {"value": 8, "unit": "minutes"},
        "coaching_cues": ["AI step."], "purpose": "AI purpose.",
        "progression_rule": "AI progress.", "category": "tactical",
        "energy_system": "none", "impact_level": "low",
    }]
    return {"weeks": [{"days": [{"countdown_label": day, "sessions": [{
        "session_type": "mindset", "title": "Tactical Focus", "objective": "AI why.",
        "primary_stressor": "decision-making", "cns_demand": "low",
        "mindset_anchor": {"intent": "Clarify pocket decisions.", "focus_cue": "Read patterns.",
                           "reset_cue": "Reset calmly.", "confidence_anchor": "Control it."},
        "blocks": blocks,
    }]}]}]}


def _merged(plan=None, brief=None):
    return merge_locked_structured_content(plan or _plan(), brief or _brief())


def test_exact_output_is_semantically_unchanged_and_merge_is_idempotent():
    once = _merged().plan
    twice = merge_locked_structured_content(once, _brief()).plan
    assert twice == once
    assert merge_locked_structured_content(deepcopy(once), _brief()).plan == once


@pytest.mark.parametrize("initial", [[], STEPS[:2]])
def test_missing_or_partial_steps_are_replaced_not_appended(initial):
    plan = _plan()
    plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["coaching_cues"] = initial
    assert _merged(plan).plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["coaching_cues"] == STEPS


def test_all_locked_fields_are_restored_and_why_stays_distinct_from_purpose():
    result = _merged()
    session = result.plan["weeks"][0]["days"][0]["sessions"][0]
    block = session["blocks"][0]
    assert session["objective"] == WHY
    assert session["mindset_anchor"] == {
        "intent": MINDSET["intent"], "focus_cue": MINDSET["focus"],
        "reset_cue": MINDSET["reset"], "confidence_anchor": MINDSET["anchor"],
    }
    assert block["purpose"] == MINDSET["context"] != WHY
    assert block["progression_rule"] == PROGRESS
    assert block["duration"] == {"value": 10, "unit": "minutes"}
    assert block["coaching_cues"] == STEPS
    assert len(result.applied) == 1 and result.unresolved == []


def test_ai_enhancement_metadata_survives():
    session = _merged().plan["weeks"][0]["days"][0]["sessions"][0]
    block = session["blocks"][0]
    assert (block["category"], block["energy_system"], block["impact_level"]) == ("tactical", "none", "low")
    assert (session["primary_stressor"], session["cns_demand"]) == ("decision-making", "low")


def test_unlocked_content_is_untouched():
    plan = _plan()
    result = _merged(plan, _brief(locked=False))
    assert result.plan == plan
    assert result.applied == result.unresolved == []


def test_wrong_day_is_unresolved_and_not_moved_then_faithfulness_rejects():
    plan = _plan(day="D-8")
    result = _merged(plan)
    assert result.plan == plan and result.applied == [] and len(result.unresolved) == 1
    assert any("LOCKED_CONTENT" in issue for issue in check_structured_faithfulness(result.plan, SOURCE, _brief()))


def _dated_brief():
    # Fight on 2026-10-01, so D-11 is 2026-09-20.
    return {**_brief(), "fight_date": "2026-10-01"}


def test_wrong_day_stays_unresolved_even_when_the_day_could_be_recreated():
    plan = _plan(day="D-8")
    result = _merged(plan, _dated_brief())
    assert result.plan == plan and result.applied == []
    assert [issue.reason for issue in result.unresolved] == [
        "locked drill is on a different structured day"
    ]


def test_day_split_across_two_rows_is_coalesced_and_repaired():
    # Stage 2 wrote two "D-11" headers; the converter kept one row per header.
    plan = _plan()
    recovery = {"title": "Recovery Reset", "session_type": "recovery", "blocks": []}
    plan["weeks"][0]["days"].append({"countdown_label": "D-11", "sessions": [recovery]})

    result = _merged(plan)

    days = result.plan["weeks"][0]["days"]
    assert len(days) == 1 and result.unresolved == []
    assert [s["title"] for s in days[0]["sessions"]] == ["Tactical Focus", "Recovery Reset"]
    assert check_structured_faithfulness(result.plan, SOURCE, _brief()) == []


def test_dated_row_with_blank_label_is_matched_by_date_and_relabelled():
    plan = _plan()
    day = plan["weeks"][0]["days"][0]
    day["countdown_label"] = ""
    day["date"] = "2026-09-20"

    result = _merged(plan, _dated_brief())

    assert result.unresolved == []
    assert result.plan["weeks"][0]["days"][0]["countdown_label"] == "D-11"
    assert check_structured_faithfulness(result.plan, SOURCE, _dated_brief()) == []


def test_dropped_day_is_recreated_in_countdown_order():
    # The converter dropped D-11 entirely (e.g. schema salvage removed it).
    plan = {"weeks": [{"days": [
        {"countdown_label": "D-12", "phase_label": "SPP", "sessions": []},
        {"countdown_label": "D-10", "phase_label": "SPP", "sessions": []},
    ]}]}

    result = _merged(plan, _dated_brief())

    days = result.plan["weeks"][0]["days"]
    assert [d["countdown_label"] for d in days] == ["D-12", "D-11", "D-10"]
    assert days[1]["date"] == "2026-09-20"
    assert days[1]["phase_label"] == "SPP"
    assert days[1]["sessions"][0]["title"] == "Tactical Focus"
    assert days[1]["sessions"][0]["blocks"][0]["coaching_cues"] == STEPS
    assert result.unresolved == []
    # The neighbouring days are fixture scaffolding absent from SOURCE, so only
    # the locked-content verdict is in scope here.
    assert not [
        issue
        for issue in check_structured_faithfulness(result.plan, SOURCE, _dated_brief())
        if "LOCKED_CONTENT" in issue
    ]


def test_qualified_only_twins_collapse_to_one_repaired_block():
    plan = _plan(include=False)
    day = plan["weeks"][0]["days"][0]
    day["sessions"][0]["title"] = "Condition and Tactical Focus"
    day["sessions"][0]["blocks"] = [
        {"display_name": "Lateral Shuffle + Strike Drills"},
        {"display_name": "Pocket Exchange Map mental rehearsal"},
    ]
    day["sessions"].append({
        "title": "Tactical Focus: Pocket Exchange Map",
        "blocks": [{"display_name": "Pocket Exchange Map mental rehearsal"}],
    })

    result = _merged(plan)

    assert result.unresolved == []
    blocks = [
        block["display_name"]
        for session in result.plan["weeks"][0]["days"][0]["sessions"]
        for block in session["blocks"]
    ]
    assert blocks.count("Pocket Exchange Map") == 1
    assert "Pocket Exchange Map mental rehearsal" not in blocks
    assert "Lateral Shuffle + Strike Drills" in blocks
    assert check_structured_faithfulness(result.plan, SOURCE, _brief()) == []


def test_missing_block_is_restored_from_authoritative_role():
    plan = _plan(include=False)
    result = _merged(plan)
    block = result.plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    assert block["display_name"] == "Pocket Exchange Map"
    assert block["coaching_cues"] == STEPS
    assert result.unresolved == []
    assert check_structured_faithfulness(result.plan, SOURCE, _brief()) == []


def test_drill_titled_session_is_reused_as_the_single_tactical_watch():
    plan = _plan(include=False)
    session = plan["weeks"][0]["days"][0]["sessions"][0]
    session["title"] = "Pocket Exchange Map"

    result = _merged(plan)

    sessions = result.plan["weeks"][0]["days"][0]["sessions"]
    assert [session["title"] for session in sessions] == ["Tactical Focus"]
    assert sessions[0]["blocks"][0]["display_name"] == "Pocket Exchange Map"
    assert result.unresolved == []
    assert check_structured_faithfulness(result.plan, SOURCE, _brief()) == []


def test_tactical_watch_keeps_its_fixed_session_title_not_a_role_alias():
    plan = _plan(include=False)
    plan["weeks"][0]["days"][0]["sessions"][0]["title"] = "Pocket Exchange Map"
    brief = _brief()
    brief["weeks"][0]["session_roles"][0]["athlete_facing_label"] = "Pressure review"

    result = _merged(plan, brief)

    assert result.plan["weeks"][0]["days"][0]["sessions"][0]["title"] == (
        "Tactical Focus"
    )


def test_stale_drill_titled_shell_is_collapsed_into_existing_tactical_watch():
    plan = _plan()
    plan["weeks"][0]["days"][0]["sessions"].insert(
        0,
        {
            "session_type": "skill",
            "title": "Pocket Exchange Map",
            "completion_status": "not_started",
            "objective": WHY,
            "mindset_anchor": {},
            "blocks": [],
        },
    )

    result = _merged(plan)

    sessions = result.plan["weeks"][0]["days"][0]["sessions"]
    assert [session["title"] for session in sessions] == ["Tactical Focus"]
    assert sessions[0]["blocks"][0]["display_name"] == "Pocket Exchange Map"
    assert result.unresolved == []
    assert merge_locked_structured_content(result.plan, _brief()).plan == result.plan


def test_qualified_tactical_focus_shell_is_collapsed():
    plan = _plan()
    plan["weeks"][0]["days"][0]["sessions"].insert(0, {
        "title": "Tactical Focus — Pocket Exchange Map",
        "completion_status": "not_started",
        "blocks": [],
    })

    result = _merged(plan)
    sessions = result.plan["weeks"][0]["days"][0]["sessions"]
    assert [session["title"] for session in sessions] == ["Tactical Focus"]
    assert merge_locked_structured_content(result.plan, _brief()).plan == result.plan


def test_qualified_bank_block_is_replaced_by_exact_block():
    plan = _plan()
    plan["weeks"][0]["days"][0]["sessions"][0]["blocks"].insert(0, {
        "display_name": "Pocket Exchange Map mental rehearsal",
        "duration": {"value": 6, "unit": "minutes"},
        "coaching_cues": ["AI paraphrase."],
    })

    result = _merged(plan)
    blocks = result.plan["weeks"][0]["days"][0]["sessions"][0]["blocks"]
    assert [block["display_name"] for block in blocks] == ["Pocket Exchange Map"]
    assert blocks[0]["duration"] == {"value": 10, "unit": "minutes"}


def test_completed_drill_titled_session_is_preserved():
    plan = _plan()
    plan["weeks"][0]["days"][0]["sessions"].append(
        {
            "session_type": "skill",
            "title": "Pocket Exchange Map",
            "completion_status": "completed",
            "blocks": [],
        }
    )

    result = _merged(plan)

    assert [session["title"] for session in result.plan["weeks"][0]["days"][0]["sessions"]] == [
        "Tactical Focus",
        "Pocket Exchange Map",
    ]


def test_locked_block_in_wrong_same_day_session_moves_without_overwriting_combat():
    plan = _plan()
    plan["weeks"][0]["days"][0]["sessions"][0]["title"] = "Technical-only combat"

    result = _merged(plan)

    sessions = result.plan["weeks"][0]["days"][0]["sessions"]
    assert [session["title"] for session in sessions] == [
        "Technical-only combat",
        "Tactical Focus",
    ]
    assert sessions[0]["blocks"] == []
    assert sessions[1]["blocks"][0]["display_name"] == "Pocket Exchange Map"
    assert result.unresolved == []
    assert check_structured_faithfulness(result.plan, SOURCE, _brief()) == []


def test_omitted_tactical_watch_session_is_created_without_another_model_call():
    plan = {"weeks": [{"days": [{"countdown_label": "D-11", "sessions": []}]}]}

    result = _merged(plan)

    session = result.plan["weeks"][0]["days"][0]["sessions"][0]
    assert session["session_type"] == "skill"
    assert session["title"] == "Tactical Focus"
    assert session["blocks"][0]["display_name"] == "Pocket Exchange Map"
    assert result.unresolved == []
    assert check_structured_faithfulness(result.plan, SOURCE, _brief()) == []


def test_ambiguous_renamed_mindset_block_is_preserved():
    plan = _plan()
    session = plan["weeks"][0]["days"][0]["sessions"][0]
    session["blocks"][0]["display_name"] = "Pocket review"

    result = _merged(plan)

    blocks = result.plan["weeks"][0]["days"][0]["sessions"][0]["blocks"]
    assert [block["display_name"] for block in blocks] == [
        "Pocket review",
        "Pocket Exchange Map",
    ]
    assert blocks[0]["coaching_cues"] == ["AI step."]
    assert blocks[1]["coaching_cues"] == STEPS
    assert check_structured_faithfulness(result.plan, SOURCE, _brief()) == []


def test_unrelated_mindset_block_is_preserved_when_locked_block_is_missing():
    plan = _plan()
    session = plan["weeks"][0]["days"][0]["sessions"][0]
    unrelated = session["blocks"][0]
    unrelated.update(
        {
            "display_name": "Fight-night breathing reset",
            "coaching_cues": ["Use a slow exhale."],
            "purpose": "Settle pre-fight tension.",
        }
    )
    original = deepcopy(unrelated)

    result = _merged(plan)

    blocks = result.plan["weeks"][0]["days"][0]["sessions"][0]["blocks"]
    assert blocks[0] == original
    assert blocks[1]["display_name"] == "Pocket Exchange Map"
    assert blocks[1]["coaching_cues"] == STEPS
    assert result.unresolved == []


def test_schema_valid_card_with_omitted_watch_is_repaired_and_persistable():
    from test_structured_plan_models import _valid_plan

    plan = _valid_plan()
    week = plan["weeks"][0]
    week["countdown_start"] = week["countdown_end"] = "D-11"
    day = week["days"][0]
    day["countdown_label"] = "D-11"
    day["sessions"] = []

    outcome = build_structured_plan_outcome(
        plan,
        raw_markdown=SOURCE,
        planning_brief=_brief(),
    )

    assert outcome.status == "valid"
    assert outcome.structured_plan is not None
    sessions = outcome.structured_plan["weeks"][0]["days"][0]["sessions"]
    assert sessions[0]["title"] == "Tactical Focus"
    assert sessions[0]["blocks"][0]["coaching_cues"] == STEPS
    assert safe_parse_structured_plan(
        outcome.structured_plan, raw_markdown=SOURCE
    ).ok


def test_previous_pocket_exchange_failure_is_repaired_before_faithfulness():
    result = _merged()
    assert check_structured_faithfulness(result.plan, SOURCE, _brief()) == []


def test_open_plan_range_map_is_restored_from_weekly_rotation():
    why = "Establish the ranges where you can score without staying available for the return."
    steps = [
        "Mark the range where you can score while remaining difficult to reach.",
        "Mark the range where the opponent becomes most dangerous.",
        "Choose the strike that best controls the space between you.",
        "Write one distance rule to test in your next technical session.",
    ]
    mindset = {
        "intent": "Control the space before increasing output.",
        "focus": "Notice where each fighter can land without overreaching.",
        "reset": "Return to the last range where you could see and react clearly.",
        "anchor": "Make them cross your range before they can attack.",
    }
    progress = "Test the distance rule in technical work and keep it only if it holds up."
    brief = {
        "open_plan_spec": {
            "tactical_watch": {
                "weekly_rotation": [
                    {
                        "week": 1,
                        "name": "Range Map",
                        "tactical_watch": {
                            "name": "Range Map",
                            "why": why,
                            "duration_min": 10,
                            "instructions": steps,
                            "mindset": mindset,
                            "progress": progress,
                        },
                    }
                ]
            }
        }
    }
    plan = {
        "weeks": [
            {
                "days": [
                    {
                        "weekday": "Wed",
                        "sessions": [
                            {
                                "session_type": "recovery",
                                "title": "Tactical Focus: Range Map",
                                "objective": "Establish where you can score without staying open.",
                                "mindset_anchor": {
                                    "intent": "Control space before raising output.",
                                    "focus_cue": "Notice where you can score safely.",
                                    "reset_cue": "Return to the last clear range if confused.",
                                    "confidence_anchor": "A simple distance rule reduces risk.",
                                },
                                "blocks": [
                                    {
                                        "block_type": "mindset",
                                        "display_name": "Range Map",
                                        "duration": {"value": 10, "unit": "minutes"},
                                        "coaching_cues": [
                                            "Mark the scoring range while keeping out of easy return fire."
                                        ],
                                        "purpose": "Establish where you can score without staying open.",
                                        "progression_rule": "Test the chosen distance rule during technical rounds.",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ]
    }

    result = merge_locked_structured_content(plan, brief)
    session = result.plan["weeks"][0]["days"][0]["sessions"][0]
    block = session["blocks"][0]

    assert session["objective"] == why
    assert session["mindset_anchor"] == {
        "intent": mindset["intent"],
        "focus_cue": mindset["focus"],
        "reset_cue": mindset["reset"],
        "confidence_anchor": mindset["anchor"],
    }
    assert block["coaching_cues"] == steps
    assert block["progression_rule"] == progress
    assert block["purpose"] == why
    assert block["duration"] == {"value": 10, "unit": "minutes"}
    assert session["session_type"] == "skill"
    assert merge_locked_structured_content(result.plan, brief).plan == result.plan


def _same_day_tactical_duplicate_plan():
    """Amy's Week 1/3 shape: one physical drill also copied into Tactical Focus."""
    weeks = []
    for week in range(1, 5):
        drill = {
            "block_id": f"physical-{week}",
            "exercise_key": "lateral-shuffle-strike-drills",
            "display_name": "Lateral Shuffle + Strike Drills",
            "block_type": "conditioning",
            "rounds": 3,
            "work": {"value": 5, "unit": "minutes"},
            "rest": {"value": 60, "unit": "seconds"},
            "effort": {"method": "RPE", "value": 5, "scale": "1-10"},
            "purpose": "Keep footwork and pressure rhythm with low hip load.",
            "execution_instructions": "Short lateral shuffles with light strikes.",
            "coaching_cues": ["Small steps", "3 x 5min rounds"],
            "why_today": f"Week {week} support",
        }
        sessions = [{"title": "Rehab-friendly low-load support", "session_type": "rehab",
                     "blocks": [drill]}]
        if week in (1, 3):
            copied = deepcopy(drill)
            copied.update({
                "block_id": f"tactical-copy-{week}",
                "coaching_cues": ["Small steps", "Keep hands high"],
                "why_today": "Tactical framing",
            })
            sessions.append({
                "title": "Tactical Focus",
                "session_type": "skill",
                "blocks": [
                    {"block_id": f"watch-{week}", "block_type": "mindset",
                     "display_name": "Safe Entry Builder", "duration": {"value": 10, "unit": "minutes"}},
                    copied,
                ],
            })
        weeks.append({"days": [{"weekday": "Tue", "sessions": sessions}]})
    return {"weeks": weeks}


def test_tactical_merge_removes_amys_week_1_and_3_duplicate_without_losing_coaching():
    original = _same_day_tactical_duplicate_plan()
    merged = merge_planner_owned_structured_content(original, {}).plan
    for number, week in enumerate(merged["weeks"], 1):
        sessions = week["days"][0]["sessions"]
        drill = sessions[0]["blocks"][0]
        assert drill["block_id"] == f"physical-{number}"
        if number in (1, 3):
            assert [b["display_name"] for b in sessions[1]["blocks"]] == ["Safe Entry Builder"]
            assert drill["coaching_cues"] == ["Small steps", "3 x 5min rounds", "Keep hands high"]
        else:
            assert len(sessions) == 1
    assert len(original["weeks"][0]["days"][0]["sessions"][1]["blocks"]) == 2
    assert merge_planner_owned_structured_content(merged, {}).plan == merged


@pytest.mark.parametrize("changed", [
    {"rounds": 4},
    {"exercise_key": "another-drill"},
    {"purpose": "Different purpose and work"},
])
def test_tactical_merge_keeps_distinct_prescriptions_and_exercises(changed):
    plan = _same_day_tactical_duplicate_plan()
    plan["weeks"][0]["days"][0]["sessions"][1]["blocks"][1].update(changed)
    merged = merge_planner_owned_structured_content(plan, {}).plan
    assert len(merged["weeks"][0]["days"][0]["sessions"][1]["blocks"]) == 2


def test_tactical_merge_does_not_deduplicate_two_physical_sessions():
    plan = _same_day_tactical_duplicate_plan()
    plan["weeks"][0]["days"][0]["sessions"][1]["title"] = "Additional technical session"
    merged = merge_planner_owned_structured_content(plan, {}).plan
    assert len(merged["weeks"][0]["days"][0]["sessions"][1]["blocks"]) == 2


def test_tactical_merge_does_not_remove_orphan_or_unanchored_exercises():
    plan = _same_day_tactical_duplicate_plan()
    first_day = plan["weeks"][0]["days"][0]
    first_day["sessions"][0]["blocks"] = []
    third_day = plan["weeks"][2]["days"][0]
    third_day["sessions"][1]["blocks"].pop(0)
    merged = merge_planner_owned_structured_content(plan, {}).plan
    assert len(merged["weeks"][0]["days"][0]["sessions"][1]["blocks"]) == 2
    assert len(merged["weeks"][2]["days"][0]["sessions"][1]["blocks"]) == 1


def test_tactical_merge_does_not_change_custom_plans():
    plan = _same_day_tactical_duplicate_plan()
    merged = merge_planner_owned_structured_content(plan, {"custom_plan": True}).plan
    assert len(merged["weeks"][0]["days"][0]["sessions"][1]["blocks"]) == 2

