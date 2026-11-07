"""Tests for the structured_plan faithfulness gate.

The structured card is a second LLM conversion of the validated Stage 2
``final_plan_text``. These tests prove the gate rejects a card that introduces
exercises, invents countdown markers, or moves work into the wrong D-day, and
that a faithful (or merely reworded) card passes — so the athlete only ever sees
a card that reflects the validated text, otherwise the raw text fallback.
"""
from __future__ import annotations

import copy

import api.structured_plan_faithfulness as faithfulness
from api.structured_plan_faithfulness import check_structured_faithfulness
from api.structured_plan_faithfulness import repair_locked_tactical_watch_source_text
from api.structured_plan_generation import build_structured_plan_outcome

# A realistic Stage 2 plan source: countdown day headers + named exercises.
# D-32 is an aerobic-support day; D-30 is the strength day that owns the squat
# and the Pallof anti-rotation work.
SOURCE = """# FIGHT CAMP PLAN

## Week 1 — SPP (D-32 → D-30)

### Mon (D-32) — Aerobic Support
- Zone 2 bike ride, 40 minutes easy aerobic flush.

### Wed (D-30) — Strength
- Barbell Back Squat 4x5 at 80 percent.
- Pallof Press 3x10 each side anti-rotation.
"""

LOCKED_WATCH_SOURCE = """D-11 (Tuesday): Tactical Focus
Why: Know what happens after the first punches so pocket exchanges stay planned rather than chaotic.
- Pocket Exchange Map: 10 minutes, tactical review only. No physical load.
  Step 1: Identify the opponent's most common pocket sequence.
  Step 2: Choose your answer to that sequence.
  Step 3: Choose the finishing shot that best fits the opening.
  Step 4: Decide whether that exchange should end with an exit or a smother.
  Intent: Win the second decision inside the pocket.
  Focus: Watch the opponent's response after the first two punches.
  Reset: If the exchange loses shape, smother or leave instead of trading blindly.
  Anchor: Know the next beat.
  Purpose: SPP pocket planning for a brawler.
  Progress: Rehearse the chosen exchange ending, not just the opening combination.
"""

LOCKED_WATCH_BRIEF = {
    "weeks": [{
        "session_roles": [{
            "display_text": "\n".join(LOCKED_WATCH_SOURCE.splitlines()[1:]),
            "preferred_exercise_names": ["Pocket Exchange Map"],
            "governance": {
                "selected_drill_locked": True,
                "selected_drill_name": "Pocket Exchange Map",
            },
        }]
    }]
}


def _locked_watch_card() -> dict:
    return {
        "weeks": [{
            "countdown_start": "D-11",
            "countdown_end": "D-11",
            "days": [{
                "countdown_label": "D-11",
                "sessions": [{
                    "objective": (
                        "Know what happens after the first punches so pocket exchanges "
                        "stay planned rather than chaotic."
                    ),
                    "mindset_anchor": {
                        "intent": "Win the second decision inside the pocket.",
                        "focus_cue": "Watch the opponent's response after the first two punches.",
                        "reset_cue": (
                            "If the exchange loses shape, smother or leave instead of "
                            "trading blindly."
                        ),
                        "confidence_anchor": "Know the next beat.",
                    },
                    "blocks": [{
                        "block_type": "mindset",
                        "display_name": "Pocket Exchange Map",
                        "purpose": "SPP pocket planning for a brawler.",
                        "coaching_cues": [
                            "Identify the opponent's most common pocket sequence.",
                            "Choose your answer to that sequence.",
                            "Choose the finishing shot that best fits the opening.",
                            "Decide whether that exchange should end with an exit or a smother.",
                        ],
                        "progression_rule": (
                            "Rehearse the chosen exchange ending, not just the opening "
                            "combination."
                        ),
                    }],
                }],
            }],
        }],
    }


def test_locked_tactical_watch_exact_content_passes_across_structured_fields():
    assert check_structured_faithfulness(
        _locked_watch_card(), LOCKED_WATCH_SOURCE, LOCKED_WATCH_BRIEF
    ) == []


def test_locked_tactical_watch_missing_one_step_is_rejected():
    plan = _locked_watch_card()
    plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["coaching_cues"].pop(2)
    violations = check_structured_faithfulness(plan, LOCKED_WATCH_SOURCE, LOCKED_WATCH_BRIEF)
    assert any("LOCKED_CONTENT" in item and "Step 3" in item for item in violations)


def test_locked_tactical_watch_step_moved_to_another_day_is_rejected():
    source = LOCKED_WATCH_SOURCE + """
D-10 (Wednesday): Recovery
- Easy mobility: 10 minutes.
"""
    plan = _locked_watch_card()
    watch_cues = plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0][
        "coaching_cues"
    ]
    moved_step = watch_cues.pop(2)
    plan["weeks"][0]["days"].append(
        {
            "countdown_label": "D-10",
            "sessions": [
                {
                    "objective": "Recover well.",
                    "blocks": [
                        {
                            "block_type": "mobility",
                            "display_name": "Easy mobility",
                            "coaching_cues": [moved_step],
                        }
                    ],
                }
            ],
        }
    )

    violations = check_structured_faithfulness(plan, source, LOCKED_WATCH_BRIEF)

    assert any("LOCKED_CONTENT" in item and "Step 3" in item for item in violations)


def test_locked_tactical_watch_missing_all_steps_and_progress_is_rejected():
    block = _locked_watch_card()["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    block["coaching_cues"] = []
    block["progression_rule"] = None
    violations = check_structured_faithfulness(
        {
            "weeks": [{
                "countdown_start": "D-11",
                "countdown_end": "D-11",
                "days": [{"countdown_label": "D-11", "sessions": [{"blocks": [block]}]}],
            }]
        },
        LOCKED_WATCH_SOURCE,
        LOCKED_WATCH_BRIEF,
    )
    labels = ("Step 1", "Step 2", "Step 3", "Step 4", "Progress")
    assert all(any(label in item for item in violations) for label in labels)


def test_locked_tactical_watch_rewritten_mindset_is_rejected():
    plan = _locked_watch_card()
    plan["weeks"][0]["days"][0]["sessions"][0]["mindset_anchor"]["intent"] = (
        "Clarify pocket decisions."
    )
    violations = check_structured_faithfulness(plan, LOCKED_WATCH_SOURCE, LOCKED_WATCH_BRIEF)
    assert any("Intent" in item for item in violations)


def test_locked_tactical_watch_missing_progress_is_rejected():
    plan = _locked_watch_card()
    plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["progression_rule"] = None
    violations = check_structured_faithfulness(plan, LOCKED_WATCH_SOURCE, LOCKED_WATCH_BRIEF)
    assert any("Progress" in item for item in violations)


OVERLAY_DANGER_CUE = "Name the opponent danger cue that tells you to protect, delay or reset."
OVERLAY_ROUND_PHASE = "Choose the round phase where this cue is most likely to matter."


def _body_attack_brief(day: str = "D-17") -> dict:
    return {
        "weeks": [{
            "session_roles": [{
                "scheduled_countdown_label": day,
                "countdown_label": "D-99",
                "display_text": "\n".join(
                    [
                        "Why: Find safe body attacks that reward pressure without making the entry predictable.",
                        "- Body Attack Opportunity: 10 minutes, tactical review only. No physical load.",
                        "  Step 1: Identify the clearest body opening the opponent gives away.",
                        "  Step 2: Choose the punch or feint that creates that opening.",
                        "  Step 3: Choose the body target you will attack.",
                        "  Step 4: Choose the head follow-up that punishes their reaction.",
                        f"  Step 5: {OVERLAY_DANGER_CUE}",
                        f"  Step 6: {OVERLAY_ROUND_PHASE}",
                        "  Intent: Attack the body when the position earns it.",
                        "  Focus: Watch for high guards, separated elbows and upright posture.",
                        "  Reset: If the body line closes, go back upstairs rather than forcing it.",
                        "  Anchor: Open it before you take it.",
                        "  Purpose: SPP body-attack selection for a pressure fighter.",
                        "  Progress: Use the body attack only after the chosen setup appears in technical work.",
                    ]
                ),
                "preferred_exercise_names": ["Body Attack Opportunity"],
                "mandatory_tactical_watch": True,
                "governance": {
                    "selected_drill_locked": True,
                    "selected_drill_name": "Body Attack Opportunity",
                    "render_selected_drill_exactly": True,
                    "experience_overlay_locked": True,
                    "mandatory": True,
                },
                "tactical_watch": {
                    "name": "Body Attack Opportunity",
                    "why": "Find safe body attacks that reward pressure without making the entry predictable.",
                    "duration_min": 10,
                    "instructions": [
                        "Identify the clearest body opening the opponent gives away.",
                        "Choose the punch or feint that creates that opening.",
                        "Choose the body target you will attack.",
                        "Choose the head follow-up that punishes their reaction.",
                        OVERLAY_DANGER_CUE,
                        OVERLAY_ROUND_PHASE,
                    ],
                    "mindset": {
                        "intent": "Attack the body when the position earns it.",
                        "focus": "Watch for high guards, separated elbows and upright posture.",
                        "reset": "If the body line closes, go back upstairs rather than forcing it.",
                        "anchor": "Open it before you take it.",
                        "context": "SPP body-attack selection for a pressure fighter.",
                    },
                    "progress": "Use the body attack only after the chosen setup appears in technical work.",
                },
            }]
        }]
    }


def _body_attack_card() -> dict:
    return {
        "weeks": [{
            "countdown_start": "D-17",
            "countdown_end": "D-9",
            "days": [{
                "countdown_label": "D-17",
                "sessions": [{
                    "title": "Tactical Focus",
                    "objective": "Find safe body attacks that reward pressure without making the entry predictable.",
                    "mindset_anchor": {
                        "intent": "Attack the body when the position earns it.",
                        "focus_cue": "Watch for high guards, separated elbows and upright posture.",
                        "reset_cue": "If the body line closes, go back upstairs rather than forcing it.",
                        "confidence_anchor": "Open it before you take it.",
                    },
                    "blocks": [{
                        "block_type": "mindset",
                        "display_name": "Body Attack Opportunity",
                        "coaching_cues": [
                            "Identify the clearest body opening the opponent gives away.",
                            "Choose the punch or feint that creates that opening.",
                            "Choose the body target you will attack.",
                            "Choose the head follow-up that punishes their reaction.",
                            OVERLAY_DANGER_CUE,
                            OVERLAY_ROUND_PHASE,
                        ],
                        "purpose": "SPP body-attack selection for a pressure fighter.",
                        "progression_rule": (
                            "Use the body attack only after the chosen setup appears in technical work."
                        ),
                    }],
                }],
            }],
        }]
    }


def test_repeated_overlay_elsewhere_does_not_create_locked_step_requirements():
    source = f"""D-17 (Monday) - Strength
- Sled Push (Speed). 3 x 2 pushes.

D-9 (Tuesday) - Tactical Focus
Why: Know what happens after the first punches so pocket exchanges stay planned.
- Pocket Exchange Map: 10 minutes, tactical review only. No physical load.
  Step 1: Identify the opponent's most common pocket sequence.
  Step 2: Choose your answer to that sequence.
  Step 5: {OVERLAY_DANGER_CUE}
  Step 6: {OVERLAY_ROUND_PHASE}
"""

    # Stage 2 authored no Tactical Watch prose on D-17, and it was never its job
    # to: the locked role owns the drill and the deterministic merge projects it
    # into the card. The card carries the authoritative content, so there is no
    # violation -- the requirement is checked against the role, not the prose.
    assert check_structured_faithfulness(
        _body_attack_card(),
        source,
        _body_attack_brief(),
    ) == []

    # The requirement did not disappear, it moved to the real owner: a card that
    # drops the locked block still fails, even though the prose is unchanged.
    stripped = copy.deepcopy(_body_attack_card())
    stripped["weeks"][0]["days"][0]["sessions"][0]["blocks"] = []
    assert check_structured_faithfulness(stripped, source, _body_attack_brief()) == [
        "LOCKED_CONTENT: 'Body Attack Opportunity' lost required source content: "
        "selected drill name, Why, Step 1, Step 2, Step 3, Step 4, Step 5, Step 6, "
        "Intent, Focus, Reset, Anchor, Purpose, Progress"
    ]


def test_locked_requirements_are_scoped_to_the_correct_drill_section():
    source = f"""D-17 (Monday) - Tactical Focus
Why: Find safe body attacks that reward pressure without making the entry predictable.
- Body Attack Opportunity: 10 minutes, tactical review only. No physical load.
  Step 1: Identify the clearest body opening the opponent gives away.
  Step 2: Choose the punch or feint that creates that opening.
  Step 3: Choose the body target you will attack.
  Step 4: Choose the head follow-up that punishes their reaction.
  Intent: Attack the body when the position earns it.
  Focus: Watch for high guards, separated elbows and upright posture.
  Reset: If the body line closes, go back upstairs rather than forcing it.
  Anchor: Open it before you take it.
  Purpose: SPP body-attack selection for a pressure fighter.
  Progress: Use the body attack only after the chosen setup appears in technical work.

D-9 (Tuesday) - Tactical Focus
Why: Know what happens after the first punches so pocket exchanges stay planned.
- Pocket Exchange Map: 10 minutes, tactical review only. No physical load.
  Step 5: {OVERLAY_DANGER_CUE}
  Step 6: {OVERLAY_ROUND_PHASE}
"""
    plan = _body_attack_card()
    plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["coaching_cues"] = [
        cue
        for cue in plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["coaching_cues"]
        if cue not in {OVERLAY_DANGER_CUE, OVERLAY_ROUND_PHASE}
    ]

    assert check_structured_faithfulness(plan, source, _body_attack_brief()) == []


def test_missing_locked_tactical_watch_source_is_repaired_idempotently():
    source = """D-17 (Monday) - Strength
- Sled Push (Speed). 3 x 2 pushes.

D-16 (Tuesday) - Technical-only combat
Technical-only contact today - no hard sparring.

D-9 (Tuesday) - Tactical Focus
Why: Know what happens after the first punches so pocket exchanges stay planned.
- Pocket Exchange Map: 10 minutes, tactical review only. No physical load.
"""
    brief = _body_attack_brief()

    repaired = repair_locked_tactical_watch_source_text(source, brief)
    repaired_twice = repair_locked_tactical_watch_source_text(
        repaired.source_markdown,
        brief,
    )

    assert repaired.unresolved == []
    assert repaired.applied == ["D-17: Body Attack Opportunity"]
    assert repaired_twice.source_markdown == repaired.source_markdown
    assert repaired_twice.applied == []
    assert repaired.source_markdown.count("Body Attack Opportunity") == 1
    assert check_structured_faithfulness(
        _body_attack_card(),
        repaired.source_markdown,
        brief,
    ) == []


def test_missing_locked_tactical_watch_repair_allows_contiguous_multi_session_day():
    source = """D-17 (Monday) - Strength
- Sled Push (Speed). 3 x 2 pushes.

D-17 (Monday) - Technical Sparring
- Technical sparring: 4 x 3 min, coach-led only.

D-16 (Tuesday) - Technical-only combat
Technical-only contact today - no hard sparring.
"""
    repaired = repair_locked_tactical_watch_source_text(source, _body_attack_brief())

    assert repaired.unresolved == []
    assert repaired.applied == ["D-17: Body Attack Opportunity"]
    assert "D-17 (Monday) — Tactical Focus" in repaired.source_markdown
    assert repaired.source_markdown.index("Technical Sparring") < repaired.source_markdown.index(
        "Tactical Focus"
    )
    assert repaired.source_markdown.index("Tactical Focus") < repaired.source_markdown.index(
        "D-16 (Tuesday)"
    )


def test_missing_locked_tactical_watch_repair_rejects_split_duplicate_day():
    source = """D-17 (Monday) - Strength
- Sled Push (Speed). 3 x 2 pushes.

D-16 (Tuesday) - Technical-only combat
Technical-only contact today - no hard sparring.

D-17 (Monday) - Technical Sparring
- Technical sparring: 4 x 3 min, coach-led only.
"""
    repaired = repair_locked_tactical_watch_source_text(source, _body_attack_brief())

    assert repaired.source_markdown == source
    assert repaired.applied == []
    assert repaired.unresolved[0].reason == "authoritative day split across multiple calendar groups"


def test_missing_locked_tactical_watch_repair_rejects_inactive_role():
    brief = _body_attack_brief()
    brief["weeks"][0]["session_roles"][0]["active"] = False
    source = """D-17 (Monday) - Strength
- Sled Push (Speed). 3 x 2 pushes.
"""

    repaired = repair_locked_tactical_watch_source_text(source, brief)

    assert repaired.source_markdown == source
    assert repaired.applied == []
    assert repaired.unresolved[0].reason == "locked role is inactive"


def test_missing_locked_tactical_watch_repair_rejects_conflicting_watch_identity():
    brief = _body_attack_brief()
    brief["weeks"][0]["session_roles"][0]["tactical_watch"]["name"] = "Pressure Route Scan"
    source = """D-17 (Monday) - Strength
- Sled Push (Speed). 3 x 2 pushes.
"""

    repaired = repair_locked_tactical_watch_source_text(source, brief)

    assert repaired.source_markdown == source
    assert repaired.applied == []
    assert repaired.unresolved[0].reason == "tactical_watch name conflicts with locked drill"


def test_unlocked_role_does_not_enable_locked_content_invariant():
    role = {
        **LOCKED_WATCH_BRIEF["weeks"][0]["session_roles"][0],
        "governance": {"selected_drill_locked": False},
    }
    brief = {"session_roles": [role]}
    assert check_structured_faithfulness(_locked_watch_card(), LOCKED_WATCH_SOURCE, brief) == []


def test_locked_content_rejection_uses_existing_text_fallback_path():
    from test_structured_plan_models import _valid_plan

    plan = _valid_plan()
    week = plan["weeks"][0]
    week["countdown_start"] = week["countdown_end"] = "D-11"
    day = week["days"][0]
    day["countdown_label"] = "D-11"
    session = day["sessions"][0]
    faithful = _locked_watch_card()["weeks"][0]["days"][0]["sessions"][0]
    session["objective"] = faithful["objective"]
    session["mindset_anchor"] = faithful["mindset_anchor"]
    session["blocks"] = [faithful["blocks"][0]]
    session["blocks"][0].update({"block_id": "watch-1", "coaching_cues": []})

    outcome = build_structured_plan_outcome(
        plan,
        raw_markdown=LOCKED_WATCH_SOURCE,
        planning_brief=LOCKED_WATCH_BRIEF,
    )

    assert outcome.status == "invalid_fallback_used"
    assert outcome.structured_plan is None
    assert any("LOCKED_CONTENT" in error and "Step 1" in error for error in outcome.errors)


def _plan(days: list[tuple[str, list[tuple[str, str]]]]) -> dict:
    """Minimal structured-plan dict: ``[(countdown_label, [(block_type, name)])]``."""
    return {
        "weeks": [
            {
                "countdown_start": "D-32",
                "countdown_end": "D-30",
                "days": [
                    {
                        "countdown_label": label,
                        "sessions": [
                            {
                                "blocks": [
                                    {"block_type": block_type, "display_name": name}
                                    for block_type, name in blocks
                                ]
                            }
                        ],
                    }
                    for label, blocks in days
                ],
            }
        ]
    }


_FAITHFUL = _plan(
    [
        ("D-32", [("conditioning", "Zone 2 Bike Ride")]),
        ("D-30", [("strength", "Barbell Back Squat"), ("accessory", "Pallof Press")]),
    ]
)


# --- faithful / reworded cards pass ----------------------------------------


def test_faithful_card_has_no_violations():
    assert check_structured_faithfulness(_FAITHFUL, SOURCE) == []


def test_reworded_exercise_passes():
    # "Barbell Back Squat" -> "Heavy Back Squats": still shares the squat token
    # (inflection-tolerant), so a rewording is not treated as fabrication.
    plan = _plan(
        [
            ("D-32", [("conditioning", "Zone 2 Bike Ride")]),
            ("D-30", [("strength", "Heavy Back Squats"), ("accessory", "Pallof Press")]),
        ]
    )
    assert check_structured_faithfulness(plan, SOURCE) == []


def test_card_with_countdown_against_sourceless_text_is_unverifiable():
    # Card-first hard gate: a card that claims a countdown structure cannot be
    # proven faithful against source text with no D-day marker, so it is rejected
    # as unverifiable rather than skipped.
    violations = check_structured_faithfulness(_FAITHFUL, "# raw")
    assert violations
    assert any(v.startswith("COUNTDOWN") for v in violations)


def test_no_countdown_claim_and_no_source_returns_clean():
    # A degenerate card making no countdown claim has nothing to project, so the
    # schema gate remains the only authority and the faithfulness gate stays out
    # of it. An empty plan likewise has no basis to judge.
    assert check_structured_faithfulness({"weeks": []}, "# raw") == []
    assert check_structured_faithfulness({}, SOURCE) == []


def test_open_plan_rejects_explicit_prescription_fields_dropped_by_card_conversion():
    source = """Session Card — Strength
- Trap-Bar Romanian Deadlift. 3 sets x 6 reps @ RPE 7. Rest 90-120 sec.
  Cue: Brace before each rep and drive through the floor.
"""
    plan = {
        "weeks": [
            {
                "days": [
                    {
                        "sessions": [
                            {
                                "blocks": [
                                    {
                                        "block_type": "strength",
                                        "display_name": "Trap-Bar Romanian Deadlift",
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
    }

    violations = check_structured_faithfulness(plan, source)

    assert any("dropped explicit source rest" in item for item in violations)
    assert any("dropped explicit source effort" in item for item in violations)
    assert any("dropped explicit source cue" in item for item in violations)


def test_open_plan_accepts_preserved_explicit_prescription_fields():
    source = """Session Card — Strength
- Trap-Bar Romanian Deadlift. 3 sets x 6 reps @ RPE 7. Rest 90-120 sec.
  Cue: Brace before each rep and drive through the floor.
"""
    plan = {
        "weeks": [
            {
                "days": [
                    {
                        "sessions": [
                            {
                                "blocks": [
                                    {
                                        "block_type": "strength",
                                        "display_name": "Trap-Bar Romanian Deadlift",
                                        "sets": 3,
                                        "reps": 6,
                                        "rest": {"value": 120, "unit": "seconds"},
                                        "effort": {"method": "RPE", "value": 7},
                                        "coaching_cues": [
                                            "Brace before each rep and drive through the floor."
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
    }

    assert check_structured_faithfulness(plan, source) == []


def test_missing_prescription_is_recovered_without_discarding_source_backed_card():
    from test_structured_plan_models import _valid_plan

    plan = _valid_plan()
    block = plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    block["display_name"] = "Trap bar deadlift"
    # Only omissions are under test; retained dose fields must match the source.
    block["sets"] = 3
    block["reps"] = 4
    # This omission fixture has no authored adjustment in its source.
    block.pop("progression_rule", None)
    block["regression_options"] = []
    block.pop("rest")
    block.pop("effort")
    block["coaching_cues"] = ["Drive through your legs and hips, keep a tall chest."]
    source = """## Week 1 (D-19 to D-13)
### Tue (D-15) — Power Transfer Touch
- Trap bar deadlift: 3 sets x 4 reps @ RPE 7. Rest 2-3 minutes.
  Cue: Drive through your legs and hips, keep a tall chest.
"""

    outcome = build_structured_plan_outcome(plan, raw_markdown=source)

    assert outcome.status == "valid"
    assert outcome.structured_plan is not None
    assert outcome.errors == []
    assert not any("dropped explicit source rest" in warning for warning in outcome.warnings)
    published = outcome.structured_plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    assert published["effort"]["value"] == 7
    assert published["rest_note"] == "Rest 2-3 minutes"
    assert not any("dropped explicit source effort" in warning for warning in outcome.warnings)
    assert not any("dropped explicit source cue" in warning for warning in outcome.warnings)


def test_prescription_gate_still_matches_after_choice_title_is_split():
    source = """Session Card — Power
- Short sprint bounds or low box jumps — 2-3 sets x 4 reps, RPE 6-7, rest 60-90 sec.
  Cue: Land quietly and reset before each rep.
"""
    plan = {
        "weeks": [
            {
                "days": [
                    {
                        "sessions": [
                            {
                                "blocks": [
                                    {
                                        "block_type": "plyometric_power",
                                        "display_name": "Short sprint bounds",
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
    }

    violations = check_structured_faithfulness(plan, source)
    assert any("dropped explicit source rest" in item for item in violations)
    assert any("dropped explicit source effort" in item for item in violations)
    assert any("dropped explicit source cue" in item for item in violations)


# --- introduced exercises are rejected -------------------------------------


def test_introduced_exercise_is_rejected():
    plan = _plan(
        [
            ("D-32", [("conditioning", "Zone 2 Bike Ride")]),
            ("D-30", [("strength", "Nordic Hamstring Curl")]),  # never in source
        ]
    )
    violations = check_structured_faithfulness(plan, SOURCE)
    assert any(v.startswith("INTRODUCED") for v in violations), violations


def test_introduced_exercise_falls_back_via_outcome():
    # The schema-valid card is downgraded to invalid_fallback_used so plan_text
    # (the validated source) is what the athlete sees.
    from test_structured_plan_models import _valid_plan

    plan = _valid_plan()
    # _valid_plan's exercise ("Barbell Back Squat" / "D-15") shares nothing with
    # this fabricated-only source, so the gate must reject it.
    source = (
        "# FIGHT CAMP PLAN\n\n### Mon (D-15) — Conditioning\n"
        "- Assault bike intervals, ten rounds.\n"
    )
    outcome = build_structured_plan_outcome(plan, raw_markdown=source)
    assert outcome.status == "invalid_fallback_used"
    assert outcome.structured_plan is None
    assert any("faithfulness" in err for err in outcome.errors)


def test_coach_led_day_stays_minimal():
    # A coach-led day in the source carries no app-owned S&C exercise; a card
    # that injects an app-owned strength block onto it is rejected because that
    # exercise is absent from the source.
    source = (
        "# FIGHT CAMP PLAN\n\n## Week 1 — SPP (D-20 → D-18)\n\n"
        "### Mon (D-20) — Coach-led sparring\n"
        "- Coach-led technical sparring. No app-owned strength work today.\n\n"
        "### Wed (D-18) — Strength\n- Trap-bar deadlift 4x3.\n"
    )
    plan = _plan(
        [
            ("D-20", [("strength", "Barbell Hip Thrust")]),  # app-owned, not in source
        ]
    )
    # Re-point the week countdown to the coach-led window.
    plan["weeks"][0]["countdown_start"] = "D-20"
    plan["weeks"][0]["countdown_end"] = "D-18"
    violations = check_structured_faithfulness(plan, source)
    assert any(v.startswith("INTRODUCED") for v in violations), violations


# --- misplaced work across D-days is rejected ------------------------------


def test_pallof_moved_to_wrong_dday_is_rejected():
    # The source assigns Pallof to D-30; placing it on D-32 is a boundary break.
    plan = _plan(
        [
            ("D-32", [("accessory", "Pallof Press")]),  # source puts this in D-30
            ("D-30", [("strength", "Barbell Back Squat")]),
        ]
    )
    violations = check_structured_faithfulness(plan, SOURCE)
    assert any(v.startswith("MISPLACED") for v in violations), violations


def test_strength_kept_in_correct_dday_passes():
    # Same exercises, correct days — no misplacement flagged.
    assert check_structured_faithfulness(_FAITHFUL, SOURCE) == []


# --- invented countdown markers are rejected -------------------------------


def test_new_countdown_marker_is_rejected():
    plan = _plan(
        [
            ("D-99", [("strength", "Barbell Back Squat")]),  # D-99 not in source
        ]
    )
    violations = check_structured_faithfulness(plan, SOURCE)
    assert any(v.startswith("COUNTDOWN") for v in violations), violations


# --- real generated plan formats -------------------------------------------

# The actual Stage 2 output leads with the countdown ("D-32 (Wednesday) — ...")
# and also nests it inside markdown headings ("### Mon (D-30) — ..."). Week
# headers ("GPP — Week 1 (D-33 to D-27)") must not be read as training days.
REAL_SOURCE = """GPP — Week 1 (D-33 to D-27)

D-32 (Wednesday) — Aerobic support
- Zone 2 bike ride, 40 minutes easy aerobic flush.

D-30 (Friday) — Strength
- Barbell Back Squat 4x5 at 80 percent.
- Pallof Press 3x10 each side anti-rotation.
"""

REAL_SOURCE_HEADINGS = """## GPP — Week 1 (D-33 to D-27)

### Wed (D-32) — Aerobic support
- Zone 2 bike ride, 40 minutes easy aerobic flush.

### Fri (D-30) — Strength
- Barbell Back Squat 4x5 at 80 percent.
- Pallof Press 3x10 each side anti-rotation.
"""


def test_real_leading_dday_format_pallof_misplaced_is_rejected():
    # Source assigns Pallof to D-30; the card placing it on D-32 must be MISPLACED
    # even though the day headers use the leading "D-32 (Wednesday) —" format.
    plan = _plan(
        [
            ("D-32", [("accessory", "Pallof Press")]),
            ("D-30", [("strength", "Barbell Back Squat")]),
        ]
    )
    violations = check_structured_faithfulness(plan, REAL_SOURCE)
    assert any(v.startswith("MISPLACED") for v in violations), violations


def test_real_markdown_heading_format_pallof_misplaced_is_rejected():
    plan = _plan(
        [
            ("D-32", [("accessory", "Pallof Press")]),
            ("D-30", [("strength", "Barbell Back Squat")]),
        ]
    )
    violations = check_structured_faithfulness(plan, REAL_SOURCE_HEADINGS)
    assert any(v.startswith("MISPLACED") for v in violations), violations


def test_real_format_faithful_card_passes():
    # Pallof kept on its source day (D-30) — no misplacement, no fabrication.
    plan = _plan(
        [
            ("D-32", [("conditioning", "Zone 2 Bike Ride")]),
            ("D-30", [("strength", "Barbell Back Squat"), ("accessory", "Pallof Press")]),
        ]
    )
    assert check_structured_faithfulness(plan, REAL_SOURCE) == []


def test_tactical_watch_note_label_is_not_treated_as_misplaced_exercise():
    source = """# FIGHT CAMP PLAN

D-17 (Friday) - Tactical Focus
- Watch: 8-12 min.
Purpose: identify opponent rhythm.
Output: write 3 fight cues only.

D-9 (Saturday) - Tactical Focus
- Watch: 8-12 min.
Purpose: identify bait reactions and exits.
Output: write 3 fight cues only.
"""
    plan = _plan(
        [
            ("D-9", [("conditioning", "Watch + note")]),
        ]
    )
    plan["weeks"][0]["countdown_start"] = "D-17"
    plan["weeks"][0]["countdown_end"] = "D-9"

    assert check_structured_faithfulness(plan, source) == []


def test_week_header_is_not_parsed_as_a_training_day():
    # "GPP — Week 1 (D-33 to D-27)" must not create a D-33/D-27 section that
    # captures the following day's exercises. Pallof belongs to D-30 only, so the
    # source token index must map it to {30}, making the misplacement check fire.
    sections = faithfulness._source_day_sections(REAL_SOURCE)
    assert set(sections) == {32, 30}
    token_days = faithfulness._source_token_days(sections)
    assert token_days.get("pallof") == {30}


def test_countdown_leading_fight_week_titles_remain_distinct_day_sections():
    """A D-day heading may legitimately include ``fight-week`` in its title."""
    source = """D-11 (Wednesday) — Light Combat / Technical
Your declared light-combat / technical session. Keep it as scheduled.

D-9 (Friday) — Fight-week freshness
- Band face pull, light: 2 x 12 reps, controlled tempo.

D-4 (Wednesday) — Light Combat / Technical
Your declared light-combat / technical session. Keep it as scheduled.

D-2 (Friday) — Fight-week freshness
- Band pull-apart, low volume: 2 x 12 controlled reps.
"""

    sections = faithfulness._source_day_sections(source)
    assert set(sections) == {11, 9, 4, 2}
    token_days = faithfulness._source_token_days(sections)
    assert token_days.get("face") == {9}
    assert token_days.get("apart") == {2}


# --- fail-closed on internal error -----------------------------------------


def test_internal_error_fails_closed(monkeypatch):
    # If the checker itself crashes it must reject the card (return a violation),
    # never silently allow an unverified card through.
    def _boom(*_args, **_kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(faithfulness, "_check", _boom)
    violations = check_structured_faithfulness(_FAITHFUL, SOURCE)
    assert violations and violations[0].startswith("INTERNAL")


def test_internal_error_falls_back_via_outcome(monkeypatch):
    from test_structured_plan_models import _valid_plan

    monkeypatch.setattr(
        "api.structured_plan_generation.check_structured_faithfulness",
        lambda *_a, **_k: ["INTERNAL: boom"],
    )
    outcome = build_structured_plan_outcome(_valid_plan(), raw_markdown="### Mon (D-15) — x")
    assert outcome.status == "invalid_fallback_used"
    assert outcome.structured_plan is None


def test_exact_source_title_wins_over_an_earlier_choice_line():
    """A plan can carry both lines as separate exercises.

    "Short sprint bounds or low box jumps" is only this block's source when the
    plan has no exact "Short sprint bounds" line of its own. When both exist the
    choice line appears first, so a first-match sweep would hand this block the
    other exercise's 2-3 sets / RPE 6-7 / 60-90 sec.
    """
    source = (
        "- Short sprint bounds or low box jumps - 2-3 sets, RPE 6-7, rest 60-90 sec\n"
        "- Short sprint bounds - 4 sets, RPE 8, rest 120 sec\n"
    )

    segment = faithfulness._source_block_segment(source, "Short sprint bounds")

    assert "4 sets" in segment
    assert "RPE 8" in segment
    assert "2-3 sets" not in segment


def test_choice_line_is_still_the_source_when_no_exact_title_exists():
    source = "- Short sprint bounds or low box jumps - 2-3 sets, RPE 6-7, rest 60-90 sec\n"

    segment = faithfulness._source_block_segment(source, "Short sprint bounds")

    assert "2-3 sets" in segment


# --- calendar-only days (spine rest rows, server-assembled sessions) --------
#
# The calendar spine runs before this gate (so a locked card has its D-day to
# land on) and writes every countdown day. Stage 2 text rarely names rest days,
# so holding those rows to the source's markers rejected nearly every card.


def _rest_row(label: str, headline: str = "") -> dict:
    return {
        "countdown_label": label,
        "day_type": "rest",
        "today_card": {"headline": headline},
        "sessions": [],
    }


def test_spine_rest_days_absent_from_source_are_not_countdown_violations():
    plan = copy.deepcopy(_FAITHFUL)
    week = plan["weeks"][0]
    week["countdown_start"], week["countdown_end"] = "D-34", "D-29"
    week["days"] = [
        _rest_row("D-34"),
        _rest_row("D-33", "Rest day"),
        week["days"][0],
        _rest_row("D-31"),
        week["days"][1],
        _rest_row("D-29"),
    ]
    assert check_structured_faithfulness(plan, SOURCE) == []


def test_server_assembled_only_day_absent_from_source_passes():
    plan = copy.deepcopy(_FAITHFUL)
    plan["weeks"][0]["days"].append(
        {
            "countdown_label": "D-29",
            "today_card": {"headline": ""},
            "sessions": [
                {
                    "session_id": "deterministic-29-joint_prep-0",
                    "blocks": [{"block_type": "mobility_activation", "display_name": "Neck CARs"}],
                }
            ],
        }
    )
    assert check_structured_faithfulness(plan, SOURCE) == []


def test_model_content_on_a_day_absent_from_source_is_still_rejected():
    plan = copy.deepcopy(_FAITHFUL)
    plan["weeks"][0]["days"].append(_rest_row("D-29", "Hard sparring"))
    violations = check_structured_faithfulness(plan, SOURCE)
    assert any("'D-29'" in v for v in violations), violations

    plan = _plan([("D-99", [("strength", "Barbell Back Squat")])])
    plan["weeks"][0]["days"][0]["today_card"] = {"headline": ""}
    violations = check_structured_faithfulness(plan, SOURCE)
    assert any(v.startswith("COUNTDOWN") and "'D-99'" in v for v in violations), violations


def test_undated_salvage_prunes_only_rejected_week_membership():
    brief = {"open_plan_spec": {"plan_type": "open_ongoing_system",
        "weekly_session_plan": {"authority": "deterministic_open_development_block",
            "weeks": [{"week": 1, "sessions": [{"weekday": "Wednesday",
                "exercises": [{"name": "Lat Pulldown"}, {"name": "Woodchopper (Cable)"}]}]}]}}}
    blocks = [{"block_type": "strength", "display_name": name}
              for name in ["Lat Pulldown", "Woodchopper (Cable)", "Easy Jog Flush"]]
    plan = {"weeks": [{"week_index": 1, "days": [{"weekday": "Wed",
        "sessions": [{"title": "Strength", "blocks": blocks}]}]}]}
    original = copy.deepcopy(plan)
    pruned, removed = faithfulness.prune_unfaithful_content(plan, "Undated plan", brief)
    assert pruned is not None
    assert [b["display_name"] for b in pruned["weeks"][0]["days"][0]["sessions"][0]["blocks"]] == [
        "Lat Pulldown", "Woodchopper (Cable)"]
    assert len(removed) == 1
    assert "Easy Jog Flush" in removed[0]
    assert plan == original
    assert not faithfulness._open_plan_membership_violations(pruned, "Undated plan", brief)
