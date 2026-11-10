import json

from fightcamp.stage2_finalizer_packet import build_stage2_finalizer_packet
from fightcamp.stage2_payload import _closed_membership_render_manifest, build_stage2_handoff_text


def test_locked_manifest_joins_strength_membership_to_authoritative_dose():
    packet = build_stage2_finalizer_packet(
        stage2_payload={
            "athlete_model": {},
            "render_mode": "camp_plan",
            "rewrite_guidance": {"render_guards": {"render_mode": "camp_plan"}},
            "weekly_role_map": {
                "weeks": [{
                    "week_index": 1,
                    "phase": "SPP",
                    "session_roles": [{
                        "role_key": "primary_strength_day",
                        "category": "strength",
                        "selected_exercise_assignments": [{
                            "slot_id": "strength-1",
                            "name": "Trap Bar Deadlift",
                            "source_phase": "SPP",
                            "slot_group": "strength_slots",
                        }],
                        "effective_strength_prescriptions": [{
                            "slot_id": "strength-1",
                            "name": "Trap Bar Deadlift",
                            "effective_prescription": "3 x 3; RPE 6-7",
                        }],
                    }],
                }]
            },
        },
        planning_brief={},
    )

    manifest = _closed_membership_render_manifest(packet)

    assert manifest[0]["selected_count"] == 1
    assert manifest[0]["exercise_lines"] == ["- Trap Bar Deadlift: 3 x 3; RPE 6-7"]
    assert manifest[0]["unresolved"] == []


def test_build_stage2_handoff_text_uses_finalizer_packet_as_single_structured_context():
    planning_brief = {
        "athlete_snapshot": {"sport": "boxing", "status": "amateur"},
        "restrictions": [{"restriction": "heavy_overhead_pressing"}],
        "candidate_pools": {"SPP": {"strength_slots": [{"role": "primary_strength"}]}},
        "omission_ledger": {"SPP": {"removed": ["push press"]}},
        "decision_rules": {
            "selection_rules": ["Prefer strong compliant same-role options first."],
            "render_guards": {
                "has_active_injury": False,
                "suppress_rehab_headings": True,
                "suppress_phase_toolbox_sections": False,
                "render_mode": "camp_plan",
            },
        },
        "weekly_role_map": {
            "weeks": [
                {
                    "week_index": 1,
                    "phase": "SPP",
                    "session_roles": [
                        {
                            "role_key": "primary_strength_day",
                            "category": "strength",
                            "scheduled_day_hint": "tuesday",
                        }
                    ],
                }
            ]
        },
    }

    handoff = build_stage2_handoff_text(
        stage2_payload={
            "athlete_model": {"sport": "boxing"},
            "restrictions": [{"restriction": "heavy_overhead_pressing"}],
            "phase_briefs": {"SPP": {"objective": "fight-specific power"}},
            "candidate_pools": {"SPP": {"strength_slots": [{"role": "primary_strength"}]}},
            "omission_ledger": {"SPP": {"removed": ["push press"]}},
            "rewrite_guidance": {
                "selection_rules": ["Prefer strong compliant same-role options first."],
                "render_guards": {
                    "has_active_injury": False,
                    "suppress_rehab_headings": True,
                    "suppress_phase_toolbox_sections": False,
                    "render_mode": "camp_plan",
                },
            },
        },
        plan_text="Week 1\n- Landmine Press - 4x5",
        coach_notes="Keep this coach-facing note short.",
        planning_brief=planning_brief,
    )

    packet_block = handoff.split("FINALIZER PACKET\n", 1)[1].split("\n\n---\n\n", 1)[0]
    packet_body = packet_block.removeprefix("```json\n").removesuffix("\n```")

    athlete_profile_block = handoff.split("ATHLETE PROFILE\n", 1)[1].split("\n\n---\n\n", 1)[0]
    athlete_profile_body = athlete_profile_block.removeprefix("```json\n").removesuffix("\n```")

    assert "FINALIZER PACKET" in handoff
    assert "ATHLETE PROFILE" in handoff
    assert "UNLXCK FINAL RENDER CONTRACT" in handoff
    assert "D-5 (Tuesday) — Fight-speed primer" in handoff
    assert "GPP — Week 1 (D-X to D-X) — Objective" in handoff
    assert "SPP — Week 2 (D-X to D-X) — Objective" in handoff
    assert "TAPER — Week 3 (D-X to D-X) — Objective" in handoff
    assert "Return only the athlete-facing final plan." in handoff
    assert '"packet_type":"stage2_finalizer_packet"' in handoff
    assert '"render_mode":"camp_plan"' in handoff
    assert '"restrictions":[{"restriction":"heavy_overhead_pressing"}]' in handoff

    assert "COACH NOTES\nKeep this coach-facing note short." in handoff
    assert "STAGE 1 DRAFT PLAN\nWeek 1\n- Landmine Press - 4x5" in handoff

    assert packet_block.startswith("```json\n")
    assert packet_block.endswith("\n```")
    assert packet_body.startswith('{"packet_type":"stage2_finalizer_packet"')

    assert athlete_profile_block.startswith("```json\n")
    assert athlete_profile_block.endswith("\n```")
    assert json.loads(athlete_profile_body) == {}
    assert json.loads(packet_body)["athlete_model"] == planning_brief["athlete_snapshot"]
    assert "ATHLETE PROFILE takes precedence on conflict" in handoff

    # Compact JSON formatting only.
    assert '\n  "' not in packet_body

    # Old bloated handoff must be gone.
    assert "PLANNING BRIEF" not in handoff
    assert '"candidate_pools"' not in handoff
    assert "RESTRICTIONS\n[" not in handoff
    assert "PHASE BRIEFS" not in handoff
    assert "CANDIDATE POOLS\n{" not in handoff
    assert "OMISSION LEDGER\n{" not in handoff
    assert "REWRITE GUIDANCE" not in handoff


def test_build_stage2_handoff_text_late_fight_excludes_candidate_pools_and_phase_briefs():
    handoff = build_stage2_handoff_text(
        stage2_payload={
            "payload_mode": "bridge_compression_payload",
            "athlete_model": {
                "sport": "boxing",
                "days_until_fight": 17,
                "fight_date": "2026-05-13",
                "has_active_injury": False,
            },
            "candidate_pools": {
                "SPP": {"toolbox": ["Should not be exposed"]},
                "GPP": {"toolbox": ["Should not be exposed"]},
                "TAPER": {"toolbox": ["Should not be exposed"]},
            },
            "phase_briefs": {
                "SPP": {"objective": "Should not be exposed in late-fight handoff"}
            },
            "rewrite_guidance": {
                "render_guards": {
                    "has_active_injury": False,
                    "suppress_rehab_headings": True,
                    "suppress_phase_toolbox_sections": True,
                    "render_mode": "late_fight_countdown_only",
                }
            },
        },
        plan_text="D-0\n- Fight day protocol",
        planning_brief={
            "athlete_snapshot": {
                "sport": "boxing",
                "days_until_fight": 17,
                "fight_date": "2026-05-13",
                "has_active_injury": False,
            },
            "decision_rules": {
                "render_guards": {
                    "has_active_injury": False,
                    "suppress_rehab_headings": True,
                    "suppress_phase_toolbox_sections": True,
                    "render_mode": "late_fight_countdown_only",
                }
            },
            "weekly_role_map": {
                "weeks": [
                    {
                        "week_index": 1,
                        "phase": "TAPER",
                        "session_roles": [
                            {
                                "role_key": "fight_day_protocol",
                                "scheduled_day_hint": "wednesday",
                                "display_text": "Fight day protocol — follow coach warm-up and fight protocol; no additional app S&C.",
                            }
                        ],
                    }
                ]
            },
        },
    )

    assert "FINALIZER PACKET" in handoff
    assert '"render_mode":"late_fight_countdown_only"' in handoff
    assert '"packet_type":"stage2_finalizer_packet"' in handoff

    # The finalizer must not see internal menus.
    assert '"candidate_pools"' not in handoff
    assert '"phase_briefs"' not in handoff
    assert "Should not be exposed" not in handoff
    assert "SPP toolbox" in handoff  # allowed only as forbidden-output label
    assert "key drills to keep in your toolbox" in handoff  # allowed only as forbidden-output label

    # No old full planning brief section.
    assert "PLANNING BRIEF" not in handoff


def test_stage2_handoff_gives_finalizer_exercise_selection_authority():
    handoff = build_stage2_handoff_text(
        stage2_payload={"athlete_model": {"sport": "boxing"}},
        plan_text="# Stage 1 Draft\n- Strength",
        planning_brief={"athlete_snapshot": {"sport": "boxing"}},
    )

    assert "Deterministic selected_exercise_assignments" in handoff
    assert "use only those exercises" in handoff
    assert "Do not add, restore, or substitute candidates" in handoff
    assert "Draft text is candidate material" in handoff
    assert "has an authoritative volume but no rest or effort, state a conservative value" in handoff
    assert "Missing rest or effort is not an unresolved-dose conflict" in handoff
    assert "session_count_summary.reduced_from_planned" in handoff
    assert "Lead notes come first" in handoff
    assert "Taper means reduce volume, not remove sharpness" in handoff
    assert "Use selected_plan inside FINALIZER PACKET as the session source of truth" not in handoff


def test_build_stage2_handoff_text_carries_surgical_voice_rules():
    handoff = build_stage2_handoff_text(
        stage2_payload={},
        plan_text="Week 1\n- Landmine Press - 4x5",
        planning_brief={"athlete_snapshot": {"sport": "boxing"}},
    )

    assert "decisive" in handoff.lower() or "gym-realistic" in handoff.lower(), (
        "Handoff should convey a decisive, gym-realistic coach voice"
    )
    assert "make the call" in handoff or "corrective" in handoff.lower(), (
        "Handoff should instruct making a clear call on corrections"
    )
    assert "practical options" in handoff or "two options" in handoff.lower(), (
        "Handoff should limit optionality for the model"
    )
    assert "focus on" in handoff or "ensure" in handoff or "motivation" in handoff.lower(), (
        "Handoff should contain anti-filler coaching voice directives"
    )
    assert "fatigue" in handoff.lower() and "optionality" in handoff.lower(), (
        "Handoff should address fatigue → reduce optionality"
    )
    assert "injury" in handoff.lower() and ("constraints" in handoff.lower() or "stop rules" in handoff.lower()), (
        "Handoff should address injury → lead with constraints"
    )

def test_build_stage2_handoff_text_preserves_countdown_calendar_fields_in_weekly_role_map():
    handoff = build_stage2_handoff_text(
        stage2_payload={"athlete_model": {"sport": "boxing"}},
        plan_text="Week 1",
        planning_brief={
            "athlete_snapshot": {"sport": "boxing"},
            "weekly_role_map": {
                "weeks": [
                    {
                        "week_index": 1,
                        "phase": "SPP",
                        "projected_days_until_fight_start": 36,
                        "projected_days_until_fight_end": 30,
                        "countdown_range": [36, 30],
                        "calendar_days": [{"weekday": "Tue", "d_day": 36}],
                        "declared_training_days": ["Tue"],
                        "declared_hard_sparring_days": ["Wed"],
                        "declared_support_work_days": ["Thu"],
                        "effective_hard_sparring_days": ["Wed"],
                        "final_week_sparring_cap": {"active": False},
                        "coach_note_flags": ["keep_easy"],
                        "intentional_compression": {"active": False},
                        "intentionally_unused_days": ["Sun"],
                        "session_roles": [],
                    }
                ]
            },
        },
    )

    assert '"countdown_range":[36,30]' in handoff
    assert '"calendar_days":[{"weekday":"Tue","d_day":36}]' in handoff
    assert '"projected_days_until_fight_start":36' in handoff


def test_build_stage2_handoff_text_includes_priority_focus_guidance_without_raw_reason_codes():
    handoff = build_stage2_handoff_text(
        stage2_payload={"athlete_model": {"sport": "boxing"}},
        plan_text="Week 1",
        planning_brief={
            "athlete_snapshot": {"sport": "boxing"},
            "priority_focus": {
                "primary_goal": "power",
                "primary_weak_area": "power",
                "secondary_goals": ["conditioning"],
                "secondary_weak_areas": ["gas_tank"],
                "goal_weakness_collisions": ["power"],
                "collision_detail": "Power drops when tired",
                "collision_details": [
                    {"tag": "power", "label": "Power", "detail": "Power drops when tired"},
                    {"tag": "conditioning", "label": "Conditioning", "detail": "Late-round fatigue"},
                ],
                "derived_clarification_tags": ["explosive", "rate_of_force"],
            },
        },
    )

    # The priority-hierarchy doctrine is carried by the finalizer packet's
    # hard_rules (canonical, always sent) rather than a duplicate prose section.
    lowered = handoff.lower()
    assert "preserve the priority hierarchy from priority_focus" in lowered
    assert "primary goal and primary weak area shape emphasis" in lowered
    assert "preserve each clarification" in lowered
    assert "derived_clarification_tags" in handoff
    # The collision detail value still reaches the model via the priority_focus
    # data block, and multi-entry collision_details are preserved there.
    assert "Power drops when tired" in handoff
    assert '"collision_detail":"Power drops when tired"' in handoff
    # Raw internal reason-code labels must never surface in the handoff.
    assert "priority_primary_goal_match" not in handoff
    assert "priority_collision_goal_weakness" not in handoff
    assert "priority_clarification_tag_match" not in handoff


def test_build_stage2_handoff_text_keeps_structured_collision_details_in_packet():
    handoff = build_stage2_handoff_text(
        stage2_payload={"athlete_model": {"sport": "boxing"}},
        plan_text="Week 1",
        planning_brief={
            "athlete_snapshot": {"sport": "boxing"},
            "priority_focus": {
                "primary_goal": "power",
                "primary_weak_area": "power",
                "goal_weakness_collisions": ["power", "conditioning"],
                "collision_detail": "Power drops when tired",
                "collision_details": [
                    {"tag": "power", "label": "Power", "detail": "Power drops when tired"},
                    {"tag": "conditioning", "label": "Conditioning", "detail": "Late-round fatigue"},
                ],
                "derived_clarification_tags": ["explosive", "rate_of_force"],
            },
        },
    )

    assert '"collision_details":[{"tag":"power","label":"Power","detail":"Power drops when tired"},{"tag":"conditioning","label":"Conditioning","detail":"Late-round fatigue"}]' in handoff
    assert '"derived_clarification_tags":["explosive","rate_of_force"]' in handoff


def _kickboxing_three_week_camp() -> tuple[dict, dict]:
    """The latest three-week Kickboxing camp: GPP D-21..15, SPP D-14..7, TAPER D-6..0."""
    render_guards = {
        "has_active_injury": False,
        "suppress_rehab_headings": True,
        "suppress_phase_toolbox_sections": False,
        "render_mode": "camp_plan",
    }
    weeks = [
        {"week_index": 1, "phase": "GPP", "session_roles": [
            {"role_key": "primary_strength_day", "category": "strength", "scheduled_day_hint": "monday"}
        ]},
        {"week_index": 2, "phase": "SPP", "session_roles": [
            {"role_key": "fight_pace_repeatability_day", "category": "conditioning", "scheduled_day_hint": "monday"}
        ]},
        {"week_index": 3, "phase": "TAPER", "session_roles": [
            {"role_key": "fight_week_freshness_day", "category": "recovery", "scheduled_day_hint": "monday"}
        ]},
    ]
    stage2_payload = {
        "athlete_model": {"sport": "kickboxing", "days_until_fight": 21},
        "rewrite_guidance": {"render_guards": dict(render_guards)},
    }
    planning_brief = {
        "athlete_snapshot": {"sport": "kickboxing", "days_until_fight": 21},
        "decision_rules": {"render_guards": dict(render_guards)},
        "weekly_role_map": {"weeks": weeks},
    }
    return stage2_payload, planning_brief


def test_dated_multiweek_camp_handoff_requires_phase_week_spine():
    stage2_payload, planning_brief = _kickboxing_three_week_camp()
    handoff = build_stage2_handoff_text(
        stage2_payload=stage2_payload,
        plan_text="GPP — Week 1\n- Slow-Lowered Pull-Up - 3x5",
        planning_brief=planning_brief,
    )

    # Stage 1 owns the calendar spine and the camp renders in camp_plan mode.
    assert '"render_mode":"camp_plan"' in handoff

    # The contract now decisively requires the phase/week spine for camp_plan
    # and scopes countdown-led output to late_fight_countdown_only only.
    assert "camp_plan (a dated multi-week camp): you MUST render the Stage 1 phase/week spine" in handoff
    assert "the final week's D-days (including D-0) stay inside the TAPER week" in handoff
    assert "late_fight_countdown_only (a short-notice camp" in handoff

    # The old, un-scoped instruction that let a multi-week camp flatten into a
    # bare countdown list must be gone.
    assert "Late-fight plans must use D-X countdown headers." not in handoff

    # RULE 13's no-week-header directive is scoped to late_fight_countdown_only.
    # In camp_plan mode suppress_phase_toolbox_sections is false, so the whole
    # countdown-scaffolding paragraph is now gated out of the prompt rather than
    # sent with a carve-out excusing camp_plan from it. Assert the stronger
    # property: the directive is absent entirely, and the positive phase/week
    # spine requirement above is what the model is left with.
    assert "do not emit phase scaffolding" not in handoff
    assert "This no-week-header rule does not apply to camp_plan" not in handoff
    assert "Output is countdown-led. Lead every active day" not in handoff
    # RULE 13's session-title and safety label discipline is never gated: it
    # applies in every mode and must survive.
    assert "RULE 13 — LATE-FIGHT LABEL DISCIPLINE" in handoff
    assert "Do not expose internal role keys" in handoff
    assert 'Never use the word "Glycolytic" in D-7 or tighter windows.' in handoff

    # The fight-week tail is handled by the countdown-continuation map, which
    # keeps the earlier camp days on the phase/week spine rather than flattening
    # the whole camp into a countdown.
    assert "Do not convert the earlier camp days into a late-fight countdown." in handoff

    # Late-camp session rules remain intact: the D-13..D-0 tail still maps to the
    # existing fight-week payload contracts inside the one camp spine.
    assert "COUNTDOWN CONTINUATION MAP" in handoff
    assert "pre_fight_compressed_payload" in handoff
    assert "fight_day_protocol_payload" in handoff


def test_late_fight_countdown_only_handoff_stays_countdown_led():
    # A short-notice camp entirely inside the late-fight window keeps countdown-led
    # rendering; the phase/week spine requirement is scoped to camp_plan only.
    stage2_payload, planning_brief = _kickboxing_three_week_camp()
    stage2_payload["athlete_model"]["days_until_fight"] = 6
    stage2_payload["rewrite_guidance"]["render_guards"]["render_mode"] = "late_fight_countdown_only"
    stage2_payload["rewrite_guidance"]["render_guards"]["suppress_phase_toolbox_sections"] = True
    planning_brief["decision_rules"]["render_guards"]["render_mode"] = "late_fight_countdown_only"
    planning_brief["decision_rules"]["render_guards"]["suppress_phase_toolbox_sections"] = True

    handoff = build_stage2_handoff_text(
        stage2_payload=stage2_payload,
        plan_text="D-6\n- Freshness reset",
        planning_brief=planning_brief,
    )

    assert '"render_mode":"late_fight_countdown_only"' in handoff
    assert "late_fight_countdown_only (a short-notice camp" in handoff
    assert "countdown-led with no week or phase headers" in handoff


def _packet_with_undosed_member():
    return build_stage2_finalizer_packet(
        stage2_payload={
            "athlete_model": {},
            "render_mode": "camp_plan",
            "rewrite_guidance": {"render_guards": {"render_mode": "camp_plan"}},
            "weekly_role_map": {
                "weeks": [{
                    "week_index": 1,
                    "phase": "SPP",
                    "session_roles": [{
                        "role_key": "primary_strength_day",
                        "category": "strength",
                        "selected_exercise_assignments": [
                            {
                                "slot_id": "strength-1",
                                "name": "Trap Bar Deadlift",
                                "source_phase": "SPP",
                                "slot_group": "strength_slots",
                            },
                            {
                                "slot_id": "strength-2",
                                "name": "Med Ball Scoop Toss",
                                "source_phase": "SPP",
                                "slot_group": "strength_slots",
                            },
                        ],
                        "effective_strength_prescriptions": [{
                            "slot_id": "strength-1",
                            "name": "Trap Bar Deadlift",
                            "effective_prescription": "3 x 3; RPE 6-7",
                        }],
                    }],
                }]
            },
        },
        planning_brief={},
    )


def test_undosed_closed_member_is_rendered_for_finalizer_dosing():
    """A scheduled exercise the planner did not dose must still reach the render.

    Membership is closed, dosing is not. Dropping the line silently deleted a
    scheduled exercise while selected_count still demanded it, so the model was
    told to render N members and handed N-1 lines.
    """
    manifest = _closed_membership_render_manifest(_packet_with_undosed_member())[0]

    assert manifest["selected_count"] == 2
    # Every scheduled member gets a line; the count the prompt calls mandatory
    # is actually satisfiable.
    assert len(manifest["exercise_lines"]) == manifest["selected_count"]
    assert manifest["exercise_lines"] == [
        "- Trap Bar Deadlift: 3 x 3; RPE 6-7",
        "- Med Ball Scoop Toss: DOSE_UNRESOLVED",
    ]
    assert manifest["unresolved"] == [{
        "index": 1,
        "name": "Med Ball Scoop Toss",
        "reason": "missing_effective_prescription",
        "action": "finalizer_prescribes_from_athlete_context",
    }]


def test_unnamed_assignment_is_still_dropped_and_reported():
    """A nameless assignment has nothing to render or dose; it stays unresolved."""
    packet = build_stage2_finalizer_packet(
        stage2_payload={
            "athlete_model": {},
            "render_mode": "camp_plan",
            "rewrite_guidance": {"render_guards": {"render_mode": "camp_plan"}},
            "weekly_role_map": {
                "weeks": [{
                    "week_index": 1,
                    "phase": "SPP",
                    "session_roles": [{
                        "role_key": "primary_strength_day",
                        "category": "strength",
                        "selected_exercise_assignments": [{"slot_id": "strength-1"}],
                    }],
                }]
            },
        },
        planning_brief={},
    )

    manifest = _closed_membership_render_manifest(packet)[0]

    assert manifest["exercise_lines"] == []
    assert manifest["unresolved"] == [
        {"index": 0, "name": "", "reason": "missing_name"}
    ]


def test_finalizer_instructions_authorise_dosing_but_not_membership_change():
    """The prompt must license dose authorship without loosening membership."""
    from fightcamp.stage2_payload import STAGE2_FINALIZER_PROMPT

    assert "DOSE_UNRESOLVED" in STAGE2_FINALIZER_PROMPT
    # Dosing is licensed...
    assert "you must prescribe one" in STAGE2_FINALIZER_PROMPT
    # ...and membership is still closed.
    assert "never add, restore or substitute an exercise" in STAGE2_FINALIZER_PROMPT
