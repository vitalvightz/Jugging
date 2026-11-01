"""Tests for the Stage 2 -> StructuredTrainingPlan generation bridge.

Covers the pure validation/repair flow, the biometric strip, the conservative
normalizer, and the prompt contract. The actual model call is exercised in
``test_stage2_automation.py``; here we test the network-free logic.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from api.structured_plan_generation import (
    BANNED_BIOMETRIC_KEYS,
    _normalize_daily_check_ins,
    _normalize_day,
    _normalize_load,
    _normalize_measured,
    _strip_fallback_from_broken_json,
    bank_conditioning_to_block,
    bank_strength_to_block,
    build_structured_plan_outcome,
    build_structured_plan_prompt,
    normalize_structured_plan_candidate,
    parse_bank_prescription,
    parse_structured_json,
    reconcile_late_fight_week_context,
    should_attempt_structured_plan,
    strip_biometric_fields,
)
from api.structured_plan_models import (
    SCHEMA_VERSION,
    LoadPrescription,
    Nutrition,
    SessionBlock,
    validate_structured_plan,
)

# Reuse the rich valid-plan factory from the schema tests (tests/ is on sys.path).
from test_structured_plan_models import _valid_plan

_DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _first_bank_entry(filename: str) -> dict:
    data = json.loads((_DATA_DIR / filename).read_text(encoding="utf-8"))
    entries = data if isinstance(data, list) else (data.get("items") or data.get("data"))
    return entries[0]


def _invalid_plan():
    """A payload the conservative normalizer cannot salvage.

    The normalizer fills structural defaults for any *dict*, so an unrecoverable
    candidate must be a non-object (here a list). This keeps the repair/fallback
    paths exercisable.
    """
    return ["not", "a", "structured", "plan"]


def _faithful_source(plan: dict) -> str:
    """Markdown faithful to ``plan`` so the faithfulness gate passes.

    The faithfulness gate (part of ``build_structured_plan_outcome``) rejects a
    countdown-claiming card whose source text has no D-day marker. Tests that
    exercise the *outcome machinery* (validation, repair, load normalization,
    biometric stripping, check-in tolerance) still need a faithful source, so
    derive one from the plan: emit each week's countdown bounds and, per day, a
    D-day header followed by every exercise block's display name.
    """
    lines = ["# FIGHT CAMP PLAN", ""]
    for week in plan.get("weeks") or []:
        lines.append(
            f"## Week — SPP ({week.get('countdown_start')} to {week.get('countdown_end')})"
        )
        lines.append("")
        for day in week.get("days") or []:
            label = day.get("countdown_label") or ""
            lines.append(f"### Day ({label}) — Session")
            for session in day.get("sessions") or []:
                for block in session.get("blocks") or []:
                    name = block.get("display_name")
                    if name:
                        lines.append(f"- {name}")
            lines.append("")
    return "\n".join(lines)


# --- build_structured_plan_outcome statuses --------------------------------


def test_none_input_is_not_attempted():
    outcome = build_structured_plan_outcome(None)
    assert outcome.status == "not_attempted"
    assert outcome.structured_plan is None
    assert outcome.schema_version is None


def test_valid_plan_outcome_is_valid_and_carries_schema_version():
    plan = _valid_plan()
    outcome = build_structured_plan_outcome(plan, raw_markdown=_faithful_source(plan))
    assert outcome.status == "valid"
    assert outcome.schema_version == SCHEMA_VERSION
    assert isinstance(outcome.structured_plan, dict)
    assert outcome.structured_plan["schema_version"] == SCHEMA_VERSION
    # Raw markdown fallback is preserved on the structured object.
    assert outcome.structured_plan["raw_markdown_fallback"]
    assert outcome.errors == []


def test_d10_structured_generation_restores_week_context_and_cleans_adjustment_cues():
    plan = _valid_plan()
    week = plan["weeks"][0]
    week["phase_label"] = "GPP"
    week["week_goal"] = None
    week["countdown_start"] = "D-10"
    week["countdown_end"] = "D-5"
    day = week["days"][0]
    day["countdown_label"] = "D-10"
    day["phase_label"] = "GPP"
    block = day["sessions"][0]["blocks"][0]
    block["coaching_cues"] = [
        "Regression /",
        "Stay relaxed through the throw.",
        "Stop: reduce to breathing only if shoulder pain rises.",
    ]
    block["regression_options"] = ["Use a lighter ball."]
    brief = {
        "late_fight_plan_spec": {
            "payload_mode": "pre_fight_compressed_payload",
            "days_out_bucket": "D-10",
        },
        "week_by_week_progression": {
            "weeks": [
                {
                    "week_index": 1,
                    "phase": "TAPER",
                    "stage_label": "Compressed Pre-Fight Week",
                }
            ]
        },
    }

    outcome = build_structured_plan_outcome(
        plan,
        raw_markdown=_faithful_source(plan),
        planning_brief=brief,
    )

    assert outcome.status == "valid"
    structured_week = outcome.structured_plan["weeks"][0]
    assert structured_week["week_goal"] == "Compressed Pre-Fight Week"
    assert structured_week["phase_label"] == "TAPER"
    structured_block = structured_week["days"][0]["sessions"][0]["blocks"][0]
    assert structured_block["coaching_cues"] == ["Stay relaxed through the throw."]
    assert structured_block["regression_options"] == ["Use a lighter ball."]
    # A "Stop:" coaching cue is routed to the dedicated stop_rules field (see the
    # generation contract: "Stop:" / "Stop rule:" -> stop_rules), not folded into
    # progression_rule, which stays absent when the source gives no genuine
    # advancement guidance.
    assert structured_block.get("progression_rule") is None
    assert structured_block["stop_rules"] == [
        "reduce to breathing only if shoulder pain rises."
    ]


def test_read_time_week_context_repairs_each_countdown_week_but_preserves_legacy_titles():
    brief = {
        "late_fight_plan_spec": {
            "payload_mode": "pre_fight_compressed_payload",
            "days_out_bucket": "D-10",
        },
        "week_by_week_progression": {
            "weeks": [
                {
                    "week_index": 1,
                    "phase": "TAPER",
                    "stage_label": "Compressed Pre-Fight Week",
                }
            ]
        },
    }
    broken = {
        "weeks": [
            {
                "week_index": 1,
                "phase_label": "GPP",
                "week_goal": None,
                "countdown_start": "D-10",
                "days": [{"countdown_label": "D-10", "phase_label": "GPP"}],
            },
            {
                "week_index": 2,
                "phase_label": "GPP",
                "week_goal": "",
                "countdown_start": "D-4",
                "days": [{"countdown_label": "D-4", "phase_label": "GPP"}],
            },
        ]
    }

    repaired = reconcile_late_fight_week_context(broken, brief)

    assert [week["week_goal"] for week in repaired["weeks"]] == [
        "Compressed Pre-Fight Week",
        "Sharpness Sessions",
    ]
    assert [week["phase_label"] for week in repaired["weeks"]] == ["TAPER", "TAPER"]

    legacy = {
        "weeks": [
            {
                "week_index": 1,
                "phase_label": "SPP",
                "week_goal": "Power Transfer Touch",
                "countdown_start": "D-10",
                "days": [],
            }
        ]
    }
    assert reconcile_late_fight_week_context(legacy, brief) is legacy


def test_invalid_without_repair_falls_back():
    outcome = build_structured_plan_outcome(_invalid_plan(), raw_markdown="# raw")
    assert outcome.status == "invalid_fallback_used"
    assert outcome.structured_plan is None
    assert outcome.schema_version is None
    assert outcome.errors  # validation errors recorded for admin/debug


def test_repair_retry_succeeds():
    repaired_payload = _valid_plan()
    calls: list[int] = []

    def repair_fn(_broken, _errors):
        calls.append(1)
        return repaired_payload

    outcome = build_structured_plan_outcome(
        _invalid_plan(),
        raw_markdown=_faithful_source(repaired_payload),
        repair_fn=repair_fn,
    )
    assert len(calls) == 1  # exactly one repair attempt
    assert outcome.status == "repair_attempted_valid"
    assert outcome.schema_version == SCHEMA_VERSION
    assert isinstance(outcome.structured_plan, dict)


def test_repair_retry_still_invalid_falls_back():
    def repair_fn(_broken, _errors):
        return ["still", "broken"]  # non-object: normalizer can't salvage it

    outcome = build_structured_plan_outcome(
        _invalid_plan(), raw_markdown="# raw", repair_fn=repair_fn
    )
    assert outcome.status == "invalid_fallback_used"
    assert outcome.structured_plan is None
    assert outcome.errors


def test_repair_not_called_when_first_attempt_is_valid():
    def repair_fn(_broken, _errors):  # pragma: no cover - must not run
        raise AssertionError("repair must not run when first attempt is valid")

    plan = _valid_plan()
    outcome = build_structured_plan_outcome(
        plan, raw_markdown=_faithful_source(plan), repair_fn=repair_fn
    )
    assert outcome.status == "valid"


# --- E. machine-readable load enforcement ----------------------------------


def test_string_only_load_is_normalized_to_object():
    plan = _valid_plan()
    plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["load"] = "85%"
    outcome = build_structured_plan_outcome(plan, raw_markdown=_faithful_source(plan))
    assert outcome.status == "valid"
    load = outcome.structured_plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["load"]
    assert load["method"] == "percentage"
    assert load["value"] == 85
    assert load["unit"] == "percent"


def test_unparseable_string_load_becomes_null():
    plan = _valid_plan()
    plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["load"] = "as hard as possible"
    outcome = build_structured_plan_outcome(plan, raw_markdown=_faithful_source(plan))
    assert outcome.status == "valid"
    assert (
        outcome.structured_plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["load"]
        is None
    )


def test_machine_readable_load_is_accepted():
    good = _valid_plan()
    good["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["load"] = {
        "method": "percentage",
        "value": 85,
        "unit": "percent",
        "ref": "1RM",
        "display": "85% 1RM",
    }
    outcome = build_structured_plan_outcome(good, raw_markdown=_faithful_source(good))
    assert outcome.status == "valid"


# --- F. fake biometric fields are not accepted / not persisted -------------


def test_biometric_fields_are_stripped_before_validation():
    plan = _valid_plan()
    today_card = plan["weeks"][0]["days"][0]["today_card"]
    today_card["hrv_score"] = 88
    today_card["whoop_recovery_score"] = 72
    plan["weeks"][0]["days"][0]["cns_recovery_percent"] = 40
    plan["strain_score"] = 14.2

    outcome = build_structured_plan_outcome(plan, raw_markdown=_faithful_source(plan))
    assert outcome.status == "valid"
    dumped = repr(outcome.structured_plan)
    for banned in ("hrv_score", "whoop_recovery_score", "cns_recovery_percent", "strain_score"):
        assert banned not in dumped


def test_strip_biometric_fields_reports_removed_paths_in_nested_lists():
    data = {
        "weeks": [
            {"days": [{"hrv": 50, "today_card": {"strain_score": 3}}]},
            {"days": [{"keep": True}]},
        ]
    }
    cleaned, removed = strip_biometric_fields(data)
    assert cleaned["weeks"][0]["days"][0] == {"today_card": {}}
    assert cleaned["weeks"][1]["days"][0] == {"keep": True}
    assert "weeks[0].days[0].hrv" in removed
    assert "weeks[0].days[0].today_card.strain_score" in removed


def test_all_banned_keys_are_lowercase_and_stripped_case_insensitively():
    # Guard: every constant is lowercase so the case-insensitive match works.
    assert all(key == key.lower() for key in BANNED_BIOMETRIC_KEYS)
    cleaned, removed = strip_biometric_fields({"HRV_Score": 1, "ok": 2})
    assert cleaned == {"ok": 2}
    assert removed == ["HRV_Score"]


# --- G + format. prompt contract -------------------------------------------


def test_prompt_carries_compact_conversion_and_safe_gap_completion_contract():
    prompt = build_structured_plan_prompt(
        plan_markdown="# Fight Camp\n- squat",
        planning_brief={"main_limiter": "conditioning"}, event_date="2026-06-13",
    )
    for marker in (SCHEMA_VERSION, "weeks[] -> days[] -> sessions[] -> blocks[]",
                   '"method":"percentage"', "Self-report only", "WHOOP",
                   "weight_cut_warning", "coach_gated", "safety trigger",
                   "completion_status", "mindset_anchor", "# Fight Camp", "2026-06-13",
                   "ROOT SHAPE EXAMPLE", "Do not select exercises", "source of truth",
                   "missing presentation", "no word limit", "Preserve source Progress/Progression"):
        assert marker in prompt, marker
    assert "infer only those missing presentation" in prompt
    assert "Never infer sparring/contact prescriptions" in prompt
    assert "volume, load, dates, safety rules" in prompt
    assert "Ignore exercise Progress" not in prompt
    assert len(prompt) < 11_000


def test_repair_prompt_instructs_shape_only_fix_and_includes_skeleton():
    prompt = build_structured_plan_prompt(
        plan_markdown="# plan",
        repair_errors=["weeks.0.load_focus: field required"],
        broken_json='{"weeks": [{}]}',
    )
    assert "Fix ONLY the JSON structure" in prompt
    assert "Do NOT change the training content." in prompt
    # The repair prompt still carries the exact skeleton to anchor the shape.
    assert "ROOT SHAPE EXAMPLE" in prompt


def test_planning_brief_context_is_allowlisted_and_deterministic():
    # The brief can be 100k+ chars of internal Stage 1 machinery. Only the
    # athlete/event/phase context keys are surfaced, the giant internal pools are
    # dropped, and the selection is stable regardless of dict ordering.
    brief = {
        "athlete_snapshot": {"sport": "boxing", "status": "amateur"},
        "phase_strategy": {"GPP": {"weeks": 2}},
        "main_limiter": "conditioning",
        "priority_focus": {"primary_goal": "power"},
        # Giant internal structures that must NOT reach the prompt:
        "candidate_pools": {"GPP": {"strength_slots": list(range(500))}},
        "weekly_role_map": {"weeks": [{"session_roles": list(range(200))}]},
        "stage1_selection_summary": {"noise": "x" * 5000},
        "weekly_stress_map": {"noise": "y" * 5000},
    }
    prompt = build_structured_plan_prompt(plan_markdown="# plan", planning_brief=brief)

    # Context keys present; internal machinery absent.
    assert '"athlete_snapshot"' in prompt
    assert '"phase_strategy"' in prompt
    assert '"main_limiter"' in prompt
    for internal in ("candidate_pools", "stage1_selection_summary", "weekly_stress_map"):
        assert internal not in prompt
    # weekly_role_map lives in the finalizer packet, never in the card context.
    assert '"weekly_role_map"' not in prompt

    # Deterministic: reversing the brief's key order yields an identical prompt.
    reordered = dict(reversed(list(brief.items())))
    assert build_structured_plan_prompt(plan_markdown="# plan", planning_brief=reordered) == prompt


def test_repair_prompt_includes_errors_and_broken_json():
    prompt = build_structured_plan_prompt(
        plan_markdown="# plan",
        repair_errors=["plan_metadata: field required"],
        broken_json='{"weeks": []}',
    )
    assert "failed schema validation" in prompt
    assert "plan_metadata: field required" in prompt
    assert '{"weeks": []}' in prompt
    assert "corrected" in prompt.lower()


def test_prompt_does_not_ask_model_to_echo_plan_into_fallback():
    # The plan body must still appear as the conversion SOURCE, but the model is
    # told to leave raw_markdown_fallback empty (the server backfills it) so we do
    # not pay ~6k output tokens echoing the plan back and risk truncation/drift.
    plan_md = "# Fight Camp\n- Back Squat 3x5\n- Bag work 3x3min"
    prompt = build_structured_plan_prompt(
        plan_markdown=plan_md,
        planning_brief={"main_limiter": "conditioning"},
        event_date="2026-06-13",
    )
    # Source text is still present for conversion fidelity.
    assert plan_md in prompt
    # Skeleton demonstrates the empty fallback, and the old echo instruction is gone.
    assert '"raw_markdown_fallback": ""' in prompt
    assert "preserve this exactly in raw_markdown_fallback" not in prompt
    assert "preserve the original human-readable plan verbatim" not in prompt
    assert 'raw_markdown_fallback to ""' in prompt


def test_repair_prompt_strips_echoed_plan_from_broken_json():
    # A first attempt that copied the whole plan into raw_markdown_fallback must
    # not have that echo re-sent in the repair prompt.
    plan_body = "VERBATIM PLAN BODY " * 400
    broken = json.dumps(
        {"schema_version": "x", "weeks": [{}], "raw_markdown_fallback": plan_body}
    )
    prompt = build_structured_plan_prompt(
        plan_markdown="# plan",
        repair_errors=["weeks.0.load_focus: field required"],
        broken_json=broken,
    )
    # The broken-JSON echo section must not carry the copied plan body.
    assert "VERBATIM PLAN BODY VERBATIM PLAN BODY" not in prompt
    assert '"raw_markdown_fallback": ""' in prompt or '"raw_markdown_fallback":""' in prompt


def test_strip_fallback_from_broken_json_valid_and_unparseable():
    plan = "PLAN " * 500
    # Valid JSON path: parse + blank the field.
    valid = json.dumps({"a": 1, "raw_markdown_fallback": plan})
    out = _strip_fallback_from_broken_json(valid)
    assert plan.strip() not in out
    assert json.loads(out)["raw_markdown_fallback"] == ""
    # Unparseable path (truncated blob): regex still blanks the value.
    truncated = '{"a":1,"raw_markdown_fallback":"' + plan.replace("\n", " ") + '","weeks":[  '
    out2 = _strip_fallback_from_broken_json(truncated)
    assert '"raw_markdown_fallback":""' in out2
    assert "PLAN PLAN PLAN" not in out2
    # Empty input is returned untouched.
    assert _strip_fallback_from_broken_json("") == ""


# --- parse_structured_json --------------------------------------------------


def test_parse_structured_json_plain_object():
    assert parse_structured_json('{"a": 1}') == {"a": 1}


def test_parse_structured_json_recovers_from_prose_wrapper():
    assert parse_structured_json('Here is the plan: {"a": 1} thanks') == {"a": 1}


def test_parse_structured_json_returns_none_on_garbage():
    assert parse_structured_json("not json at all") is None
    assert parse_structured_json("") is None


def test_parse_structured_json_ignores_trailing_prose_with_braces():
    # rfind would over-extend to the stray closing brace in the trailing prose.
    text = 'Plan: {"a": 1} -- done (see notes }below)'
    assert parse_structured_json(text) == {"a": 1}


def test_parse_structured_json_handles_code_fence_and_braces_in_strings():
    text = '```json\n{"display": "85% {1RM}", "value": 85}\n```'
    assert parse_structured_json(text) == {"display": "85% {1RM}", "value": 85}


# --- normalize_structured_plan_candidate (conservative shape fixes) ----------


def _live_malformed_plan() -> dict:
    """A plan in the right shape but with the validation failures seen live."""
    return {
        # schema_version missing entirely
        "plan_metadata": {"title": "Camp"},  # missing sport/plan_type/timezone/status
        "athlete_context": {},  # missing sport_profile
        "event_context": {"event_type": "boxing_match"},  # invalid enum
        "countdown_labels": ["D-28", "D0", "D+1"],  # strings, not objects
        "red_flag_rules": [{"display_text": "Stop if dizzy"}],  # missing required fields
        "weeks": [
            {
                "phase_label": "spp",  # wrong case
                "days": [
                    {
                        "day_type": "HARD",  # invalid enum
                        "today_card": {"headline": "Go"},  # missing readiness/mindset
                        "sessions": [
                            {
                                "session_type": "strength",  # alias -> strength_power
                                "blocks": [
                                    {
                                        "block_type": "warmup",  # alias -> preparation
                                        "display_name": "Skips",
                                        "load": "85%",  # string -> object
                                        "rest": "90s",  # string -> object
                                        "duration": "45 min",  # string -> object
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
        "daily_check_ins": None,  # not a list
        "nutrition": {"summary": "Eat well"},  # incomplete
        "progression_notes": ["note one", "note two"],  # list, not string
        "raw_markdown_fallback": "# Plan\nThe real plan text.",
    }


def test_normalize_recovers_live_style_malformed_plan():
    outcome = build_structured_plan_outcome(_live_malformed_plan(), raw_markdown="# raw")
    assert outcome.status == "valid"
    # The normalized object still validates strictly against the schema.
    validate_structured_plan(outcome.structured_plan)


def test_normalize_fills_missing_plan_metadata_fields():
    plan = normalize_structured_plan_candidate({"plan_metadata": {"title": "Camp"}})
    meta = plan["plan_metadata"]
    assert meta["plan_type"] in {
        "fight_camp",
        "explosive_athlete",
        "match_week",
        "reintegration",
        "general_performance",
    }
    assert meta["status"] == "active"
    assert meta["timezone"] == "UTC"
    assert isinstance(meta["sport"], str)
    assert plan["athlete_context"]["sport_profile"] == ""


def test_normalize_plan_notes_coerces_category_and_drops_empty():
    plan = normalize_structured_plan_candidate(
        {
            "plan_notes": [
                {"category": "Weight_Cut", "label": "Active cut", "text": "~5.7% target."},
                {"category": "bogus", "text": "Stay disciplined."},  # unknown -> general
                {"category": "injury", "text": "   "},  # empty text -> dropped
                "Keep the wound covered.",  # bare string -> general note
                123,  # non-note -> dropped
            ]
        }
    )
    notes = plan["plan_notes"]
    assert [n["category"] for n in notes] == ["weight_cut", "general", "general"]
    assert notes[0] == {"category": "weight_cut", "label": "Active cut", "text": "~5.7% target."}
    assert notes[2]["text"] == "Keep the wound covered."
    # The normalized plan still validates strictly against the schema.
    validate_structured_plan(normalize_structured_plan_candidate({"plan_notes": notes}))


def test_normalize_red_flag_machine_fields_coerce_prose_values():
    plan = normalize_structured_plan_candidate(
        {
            "red_flag_rules": [
                {
                    "rule_id": "power_drop",
                    "when": "during_session",
                    "severity": "amber",
                    "metric": "power_drop_pct",
                    "operator": ">",
                    "threshold": ">20%",  # prose fragment, not a number
                    "applies_to": "neural blocks",  # lone string, not a list
                    "display_text": "Stop if power drops >20% between reps.",
                    "action": "stop_block",
                },
                {
                    "rule_id": "wound",
                    "when": "pre_session",
                    "severity": "amber",
                    "threshold": "re-opens",  # unreadable -> None, not a rejected card
                    "replacement_session_type": "rest",  # alias -> recovery
                    "display_text": "Stop if the wound re-opens or bleeds.",
                    "action": "stop and contact staff",
                },
            ]
        }
    )
    first, second = plan["red_flag_rules"]
    assert first["threshold"] == 20.0
    assert first["applies_to"] == ["neural blocks"]
    assert second["threshold"] is None
    assert second["replacement_session_type"] == "recovery"
    validate_structured_plan(plan)


def test_normalize_block_null_list_fields_become_empty_lists():
    day = _normalize_day(
        {
            "sessions": [
                {
                    "blocks": [
                        {
                            "display_name": "Clean & Press",
                            "regression_options": None,  # explicit null must not reject the card
                            "coaching_cues": None,
                            "substitutions": "med ball scoop toss",
                        }
                    ]
                }
            ]
        }
    )
    block = day["sessions"][0]["blocks"][0]
    assert block["regression_options"] == []
    assert block["coaching_cues"] == []
    assert block["substitutions"] == ["med ball scoop toss"]


def test_normalize_block_drops_reps_that_echo_interval_work_seconds():
    day = _normalize_day(
        {
            "sessions": [
                {
                    "blocks": [
                        {
                            "display_name": "Standing Stance-Set Cue",
                            "reps": 3,
                            "rounds": 2,
                            "work": {"value": 3, "unit": "seconds"},
                            "rest": {"value": 120, "unit": "seconds"},
                        },
                        {
                            "display_name": "Sprint Starts",
                            "reps": 5,
                            "rounds": 2,
                            "work": {"value": 6, "unit": "seconds"},
                        },
                    ]
                }
            ]
        }
    )
    echo, real = day["sessions"][0]["blocks"]
    assert echo["reps"] is None
    assert echo["rounds"] == 2
    assert real["reps"] == 5


def test_missing_identifiers_do_not_reject_an_otherwise_valid_card():
    plan = _live_malformed_plan()
    plan["red_flag_rules"][0].pop("rule_id", None)
    week = plan["weeks"][0]
    week.pop("week_id", None)
    session = week["days"][0]["sessions"][0]
    session.pop("session_id", None)
    session["blocks"][0].pop("block_id", None)

    outcome = build_structured_plan_outcome(plan, raw_markdown="# raw")

    assert outcome.status == "valid"
    assert outcome.structured_plan["red_flag_rules"][0]["rule_id"] is None
    assert outcome.structured_plan["weeks"][0]["week_id"] is None
    assert outcome.structured_plan["weeks"][0]["days"][0]["sessions"][0]["session_id"] is None
    assert (
        outcome.structured_plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0][
            "block_id"
        ]
        is None
    )


def test_normalize_block_red_flags_and_string_effort():
    day = _normalize_day(
        {
            "sessions": [
                {
                    "blocks": [
                        {
                            "display_name": "Jab bursts",
                            "effort": "RPE 7-8",  # bare string -> prescription (upper bound)
                            "red_flags": [
                                {
                                    "rule_id": "tech",
                                    "when": "during_session",
                                    "severity": "amber",
                                    "threshold": "form breaks",  # unreadable -> None
                                    "display_text": "Drop load if technique breaks.",
                                    "action": "reduce_load",
                                }
                            ],
                        },
                        {"display_name": "Shadow", "effort": "easy technical flow"},  # no number -> None
                    ]
                }
            ]
        }
    )
    first, second = day["sessions"][0]["blocks"]
    assert first["effort"] == {"method": "RPE", "value": 8.0, "scale": "1-10"}
    assert first["red_flags"][0]["threshold"] is None
    assert second["effort"] is None


def test_normalize_intent_one_reads_as_full_intent_not_one_of_ten():
    """The live bug: power blocks came back as intent 1 and showed "Intent 1/10"."""
    blocks = [
        {"display_name": "Med-Ball Rotational Slam", "effort": {"method": "intent", "value": 1, "scale": "1-10"}},
        {"display_name": "Barbell Thruster", "effort": {"method": "intent", "value": "1"}},
        {"display_name": "Bound", "effort": {"method": "intent", "value": 0.8}},
        {"display_name": "Sprint", "effort": {"method": "intent", "value": 9, "scale": "1-10"}},
        {"display_name": "Squat", "effort": {"method": "RPE", "value": 1}},
    ]
    day = _normalize_day({"sessions": [{"blocks": blocks}]})
    efforts = [block["effort"] for block in day["sessions"][0]["blocks"]]
    assert efforts == [
        {"method": "intent", "value": "max", "scale": None},
        {"method": "intent", "value": "max", "scale": None},
        {"method": "intent", "value": "80%", "scale": None},
        {"method": "intent", "value": 9, "scale": "1-10"},
        {"method": "RPE", "value": 1},
    ]


def test_intent_dosed_accessory_block_is_relabelled_as_power():
    """The live bug: Lateral Bound-to-Slip, a ballistic drill, was tagged "Accessory"."""
    blocks = [
        {"display_name": "Lateral Bound-to-Slip", "block_type": "accessory", "effort": {"method": "intent", "value": "max"}},
        {"display_name": "Pallof Press", "block_type": "accessory", "effort": {"method": "RPE", "value": 6}},
        {"display_name": "Dead Bug", "block_type": "accessory"},
        {"display_name": "Barbell Thruster", "block_type": "strength", "effort": {"method": "intent", "value": 1}},
    ]
    day = _normalize_day({"sessions": [{"blocks": blocks}]})
    types = [block["block_type"] for block in day["sessions"][0]["blocks"]]
    assert types == ["plyometric_power", "accessory", "accessory", "strength"]


def test_normalize_recovers_invalid_fallback_card_shape():
    """The live failure: string red-flag thresholds + a null block list."""
    plan = _live_malformed_plan()
    plan["red_flag_rules"] = [
        {
            "rule_id": "power_drop",
            "when": "during_session",
            "severity": "amber",
            "threshold": ">20%",
            "display_text": "Stop if power drops >20% between reps.",
            "action": "stop_block",
        }
    ]
    plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["regression_options"] = None
    outcome = build_structured_plan_outcome(plan, raw_markdown="# raw")
    assert outcome.status == "valid"
    validate_structured_plan(outcome.structured_plan)


def test_invalid_block_is_contained_without_discarding_valid_sibling_cards():
    plan = _valid_plan()
    bad_block = copy.deepcopy(plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0])
    bad_block["block_id"] = "bad-block"
    bad_block["display_name"] = "Malformed accessory"
    bad_block["sets"] = {"not": "a count"}
    plan["weeks"][0]["days"][0]["sessions"][0]["blocks"].append(bad_block)
    raw_markdown = _faithful_source(plan)
    plan["raw_markdown_fallback"] = "stale model copy"

    outcome = build_structured_plan_outcome(
        plan,
        raw_markdown=raw_markdown,
    )

    assert outcome.status == "valid"
    assert outcome.structured_plan is not None
    assert outcome.structured_plan["raw_markdown_fallback"] == raw_markdown
    blocks = outcome.structured_plan["weeks"][0]["days"][0]["sessions"][0]["blocks"]
    assert [block["display_name"] for block in blocks] == ["Barbell Back Squat"]
    # The warning names the validation path and message that caused it.
    assert any(
        warning.startswith("schema_salvage: omitted invalid block 'Malformed accessory' (")
        and ".blocks.1.sets:" in warning
        for warning in outcome.warnings
    )


def test_invalid_session_is_contained_without_discarding_valid_day():
    plan = _valid_plan()
    bad_session = copy.deepcopy(plan["weeks"][0]["days"][0]["sessions"][0])
    bad_session["session_id"] = "bad-session"
    bad_session["title"] = "Malformed session"
    bad_session["completion"] = "not an object"
    plan["weeks"][0]["days"][0]["sessions"].append(bad_session)

    outcome = build_structured_plan_outcome(
        plan,
        raw_markdown=_faithful_source(plan),
    )

    assert outcome.status == "valid"
    sessions = outcome.structured_plan["weeks"][0]["days"][0]["sessions"]
    assert [session["title"] for session in sessions] == ["Power Transfer Touch"]
    assert any(
        warning.startswith("schema_salvage: omitted invalid session 'Malformed session' (")
        and ".sessions.1.completion:" in warning
        for warning in outcome.warnings
    )


def test_invalid_day_metadata_is_cleared_without_discarding_the_day():
    # Formerly the whole day was omitted for its incomplete microdose card. The
    # microdose is planner-owned metadata (the spine re-attaches it from the
    # role map), so only that field goes and the day keeps its session.
    plan = _valid_plan()
    plan["weeks"][0]["days"][0]["priority_microdose"] = {"goal": "speed"}

    outcome = build_structured_plan_outcome(
        plan,
        raw_markdown=_faithful_source(plan),
    )

    assert outcome.status == "valid"
    days = outcome.structured_plan["weeks"][0]["days"]
    assert [day["countdown_label"] for day in days] == ["D-15"]
    assert days[0]["priority_microdose"] is None
    assert [s["title"] for s in days[0]["sessions"]] == ["Power Transfer Touch"]
    assert not any("omitted invalid day" in warning for warning in outcome.warnings)


def test_unrepairable_day_without_planner_work_is_still_contained():
    from api.structured_plan_generation import _salvage_invalid_training_nodes

    plan = _valid_plan()
    error = "weeks.0.days.0.today_card.mindset_anchor: Field required"
    salvaged, warnings, refusals = _salvage_invalid_training_nodes(
        plan, [error], raw_markdown="# raw"
    )

    assert refusals == []
    assert salvaged["weeks"][0]["days"] == []
    assert warnings == [f"schema_salvage: omitted invalid day 'D-15' ({error})"]


def test_root_schema_failure_still_uses_whole_plan_fallback():
    outcome = build_structured_plan_outcome(
        _invalid_plan(),
        raw_markdown="# authoritative raw plan",
    )

    assert outcome.status == "invalid_fallback_used"
    assert outcome.structured_plan is None


def test_normalize_invalid_event_type_maps_to_none():
    plan = normalize_structured_plan_candidate(
        {"event_context": {"event_type": "boxing_match", "fight_date": "2026-06-13"}}
    )
    assert plan["event_context"]["event_type"] == "none"
    # A valid event_type is preserved untouched.
    plan2 = normalize_structured_plan_candidate({"event_context": {"event_type": "fight"}})
    assert plan2["event_context"]["event_type"] == "fight"


def test_normalize_countdown_label_strings_become_objects():
    plan = normalize_structured_plan_candidate({"countdown_labels": ["D-28", "D0", "D+1"]})
    labels = plan["countdown_labels"]
    assert labels[0] == {"date": "", "days_to_event": 28, "label": "D-28", "anchor": "event_countdown"}
    assert labels[1]["days_to_event"] == 0
    assert labels[2]["days_to_event"] == -1


# --- day_type intensity classification ---------------------------------------


def _day(day_type, sessions, *, countdown_label=""):
    return {
        "date": "2026-06-13",
        "day_type": day_type,
        "countdown_label": countdown_label,
        "today_card": {"headline": "Go"},
        "sessions": sessions,
    }


def _session(session_type, blocks):
    return {"session_type": session_type, "blocks": blocks}


def test_day_type_d1_light_primer_reads_low_not_moderate():
    # The reported bug: a genuinely light D-1 primer (bodyweight rhythm, RPE 3-4,
    # low-tension Pallof, no heavy work) must not surface as "moderate".
    day = _day(
        "moderate",  # the model's (wrong) guess
        [
            _session(
                "primer",
                [
                    {"block_type": "plyometric_power", "display_name": "Bodyweight rhythm",
                     "effort": {"method": "RPE", "value": 3}},
                    {"block_type": "accessory", "display_name": "Low-tension Pallof",
                     "effort": {"method": "RPE", "value": 4}},
                ],
            )
        ],
        countdown_label="D-1",
    )
    assert _normalize_day(day)["day_type"] == "low"


def test_day_type_unknown_model_value_never_silently_moderate():
    # "primer" is not a valid day_type enum; the old normalizer defaulted it to
    # "moderate". With light content it must read "low".
    day = _day(
        "primer",
        [_session("primer", [{"block_type": "accessory", "intensity": "light",
                               "display_name": "Mobility flow"}])],
    )
    assert _normalize_day(day)["day_type"] == "low"


def test_day_type_heavy_strength_reads_high():
    day = _day(
        "moderate",
        [_session("strength_power", [
            {"block_type": "strength", "display_name": "Back squat",
             "load": {"method": "percentage", "value": 88}},
        ])],
    )
    assert _normalize_day(day)["day_type"] == "high"


def test_day_type_mid_rpe_reads_moderate():
    day = _day(
        "low",
        [_session("conditioning", [
            {"block_type": "conditioning", "display_name": "Intervals",
             "effort": {"method": "RPE", "value": 7}},
        ])],
    )
    assert _normalize_day(day)["day_type"] == "moderate"


def test_normalize_keeps_coach_led_contact_alongside_app_session():
    # A coach-owned sparring day that also carries an app session must keep both:
    # the app session in sessions, and the coach-owned label in coach_led_contact.
    day = _day(
        "moderate",
        [_session("skill", [{"block_type": "skill", "display_name": "Cue card"}])],
        countdown_label="D-11",
    )
    day["today_card"]["coach_led_contact"] = "Coach-led boxing — technical only"
    out = _normalize_day(day)
    assert out["today_card"]["coach_led_contact"] == "Coach-led boxing — technical only"
    assert out["sessions"], "app session must not be dropped"


def test_normalize_folds_empty_coach_led_session_into_contact_field():
    # Some first-pass cards emit coach contact as an empty session plus a real app
    # touch. Normalize that near-miss into the canonical coexisting-day shape.
    day = _day(
        "moderate",
        [
            _session("sparring", []),
            _session("skill", [{"block_type": "skill", "display_name": "Tactical Cue Card"}]),
        ],
        countdown_label="D-11",
    )
    day["sessions"][0]["title"] = "Coach-led boxing - technical only"
    day["sessions"][1]["title"] = "Tactical Cue Card"

    out = _normalize_day(day)

    assert out["today_card"]["coach_led_contact"] == "Coach-led boxing - technical only"
    assert [session["title"] for session in out["sessions"]] == ["Tactical Cue Card"]
    assert out["day_type"] == "moderate"


def test_normalize_drops_blank_coach_led_contact():
    # An empty/whitespace contact must not survive as a blank context block.
    day = _day("moderate", [], countdown_label="D-11")
    day["today_card"]["coach_led_contact"] = "   "
    out = _normalize_day(day)
    assert "coach_led_contact" not in out["today_card"]


def test_day_type_hardest_block_sets_the_day():
    day = _day(
        "low",
        [_session("mixed", [
            {"block_type": "mobility_activation", "display_name": "Warmup",
             "effort": {"method": "RPE", "value": 3}},
            {"block_type": "strength", "display_name": "Power clean",
             "load": {"method": "percentage", "value": 90}},
        ])],
    )
    assert _normalize_day(day)["day_type"] == "high"


def test_day_type_fight_day_is_competition():
    by_countdown = _day("high", [_session("primer", [{"block_type": "preparation",
                                                      "display_name": "Activation"}])],
                        countdown_label="D0")
    assert _normalize_day(by_countdown)["day_type"] == "competition"
    by_session = _day("high", [_session("fight_or_match", [])])
    assert _normalize_day(by_session)["day_type"] == "competition"


def test_day_type_empty_and_recovery_days_are_categorical():
    assert _normalize_day(_day("moderate", []))["day_type"] == "rest"
    recovery = _day("moderate", [_session("recovery", [])])
    assert _normalize_day(recovery)["day_type"] == "recovery"


def test_day_type_plyo_without_numbers_reads_high_but_light_plyo_reads_low():
    # No effort/load number: a true power block implies a hard day.
    blind = _day("moderate", [_session("strength_power", [
        {"block_type": "plyometric_power", "display_name": "Depth jumps"}])])
    assert _normalize_day(blind)["day_type"] == "high"
    # An explicit light RPE overrides the block type.
    light = _day("moderate", [_session("primer", [
        {"block_type": "plyometric_power", "display_name": "Pogos",
         "effort": {"method": "RPE", "value": 4}}])])
    assert _normalize_day(light)["day_type"] == "low"


def test_day_type_empty_sparring_reads_high_not_rest():
    day = _day("moderate", [_session("sparring", [])])
    assert _normalize_day(day)["day_type"] == "high"


def test_recovery_with_recovery_blocks_stays_recovery():
    day = _day("moderate", [_session("recovery", [
        {"block_type": "cooldown_recovery", "intensity": "low"}
    ])])
    assert _normalize_day(day)["day_type"] == "recovery"


def test_rpe_range_7_8_reads_high():
    day = _day("moderate", [_session("conditioning", [
        {"block_type": "conditioning", "effort": {"method": "RPE", "value": "7-8"}}
    ])])
    assert _normalize_day(day)["day_type"] == "high"


def test_day_type_explosive_primer_is_not_auto_high():
    # "explosive" sharp/low-volume primer work at RPE 4 is light, not a hard day.
    day = _day("moderate", [_session("primer", [
        {"block_type": "plyometric_power", "intensity": "explosive",
         "effort": {"method": "RPE", "value": 4}}
    ])])
    assert _normalize_day(day)["day_type"] == "low"


def test_normalize_red_flag_rules_get_required_fields_without_fabricating_id():
    plan = normalize_structured_plan_candidate(
        {"red_flag_rules": [{"display_text": "Stop if dizzy"}]}
    )
    rule = plan["red_flag_rules"][0]
    assert rule["rule_id"] is None
    assert rule["when"] == "morning_check_in"
    assert rule["severity"] == "amber"
    assert rule["action"] == ""


def test_normalize_session_and_block_type_aliases():
    plan = normalize_structured_plan_candidate(
        {
            "weeks": [
                {
                    "days": [
                        {
                            "sessions": [
                                {
                                    "session_type": "strength",
                                    "blocks": [
                                        {"block_type": "warmup", "display_name": "Skips"},
                                        {"block_type": "made_up", "display_name": "Mystery"},
                                    ],
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    )
    session = plan["weeks"][0]["days"][0]["sessions"][0]
    assert session["session_type"] == "strength_power"
    assert session["blocks"][0]["block_type"] == "preparation"
    assert session["blocks"][1]["block_type"] == "accessory"  # unknown -> accessory


def test_normalize_rehab_friendly_raw_session_type_to_rehab():
    plan = normalize_structured_plan_candidate(
        {
            "weeks": [
                {
                    "days": [
                        {"sessions": [{"session_type": "Rehab-friendly low-load support"}]}
                    ]
                }
            ]
        }
    )
    session = plan["weeks"][0]["days"][0]["sessions"][0]
    assert session["session_type"] == "rehab"


def test_normalize_rest_duration_strings_become_objects():
    plan = normalize_structured_plan_candidate(
        {
            "weeks": [
                {
                    "days": [
                        {
                            "sessions": [
                                {
                                    "blocks": [
                                        {"rest": "90s", "duration": "2 min"},
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    )
    block = plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    assert block["rest"] == {"value": 90.0, "unit": "seconds"}
    assert block["duration"] == {"value": 2.0, "unit": "minutes"}


def test_normalize_daily_check_ins_non_list_becomes_empty_list():
    assert normalize_structured_plan_candidate({"daily_check_ins": None})["daily_check_ins"] == []
    assert normalize_structured_plan_candidate({"daily_check_ins": {"x": 1}})["daily_check_ins"] == []


def test_normalize_incomplete_nutrition_gets_structural_defaults():
    nutrition = normalize_structured_plan_candidate({"nutrition": {"summary": "Eat well"}})["nutrition"]
    for key in ("summary", "daily_focus", "training_day_guidance", "fight_week_guidance"):
        assert isinstance(nutrition[key], str)
    assert nutrition["summary"] == "Eat well"


def test_normalize_progression_notes_coerced_to_string():
    assert normalize_structured_plan_candidate({"progression_notes": ["a", "b"]})["progression_notes"] == "a b"
    assert normalize_structured_plan_candidate({"progression_notes": None})["progression_notes"] == ""
    assert normalize_structured_plan_candidate({"progression_notes": {"x": 1}})["progression_notes"] == ""


def test_normalize_preserves_raw_markdown_fallback_and_does_not_invent_content():
    plan = normalize_structured_plan_candidate(_live_malformed_plan())
    assert plan["raw_markdown_fallback"] == "# Plan\nThe real plan text."
    # No weeks/sessions/blocks were invented beyond what the input contained.
    assert len(plan["weeks"]) == 1
    assert len(plan["weeks"][0]["days"][0]["sessions"][0]["blocks"]) == 1


def test_normalize_leaves_non_dict_untouched_so_schema_can_reject():
    # Fallback safety: a non-object payload is not salvageable and must stay invalid.
    assert normalize_structured_plan_candidate(["not", "a", "plan"]) == ["not", "a", "plan"]
    outcome = build_structured_plan_outcome(["not", "a", "plan"], raw_markdown="# raw")
    assert outcome.status == "invalid_fallback_used"
    assert outcome.structured_plan is None


# --- label-typo fix: PRE -> RPE ----------------------------------------------


def _plan_strings(node) -> list[str]:
    """Every string leaf in a normalized plan (for greppable assertions)."""
    out: list[str] = []
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for value in node.values():
            out.extend(_plan_strings(value))
    elif isinstance(node, list):
        for item in node:
            out.extend(_plan_strings(item))
    return out


def test_normalize_fixes_pre_intensity_label_to_rpe():
    plan = normalize_structured_plan_candidate(
        {
            "plan_notes": [{"text": "Hold conditioning at PRE 7–8 all week."}],
            "weeks": [
                {
                    "days": [
                        {
                            "sessions": [
                                {
                                    "blocks": [
                                        {
                                            "progression_rule": "Build from PRE 6 to PRE 8.",
                                            "coaching_cues": ["Keep efforts at PRE 9-10."],
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ],
        }
    )
    blob = " || ".join(_plan_strings(plan))
    # No athlete-facing string carries the PRE-for-RPE typo any more.
    assert "PRE 7" not in blob and "PRE 6" not in blob and "PRE 8" not in blob
    assert "PRE 9" not in blob
    assert "RPE 7–8" in blob
    assert "RPE 6" in blob and "RPE 8" in blob
    assert "RPE 9-10" in blob


def test_normalize_pre_fix_does_not_touch_ordinary_words_or_years():
    plan = normalize_structured_plan_candidate(
        {
            "plan_notes": [
                {"text": "COMPRESSED PRE-FIGHT WEEK begins now."},
                {"text": "Methodology updated PRE 2024 stays as written."},
            ],
        }
    )
    texts = [n["text"] for n in plan["plan_notes"]]
    assert "COMPRESSED PRE-FIGHT WEEK begins now." in texts
    assert "Methodology updated PRE 2024 stays as written." in texts


def test_normalize_pre_fix_never_rewrites_raw_markdown_fallback():
    plan = normalize_structured_plan_candidate(
        {"raw_markdown_fallback": "Conditioning @ PRE 7–8 (verbatim source)."}
    )
    assert plan["raw_markdown_fallback"] == "Conditioning @ PRE 7–8 (verbatim source)."


# --- duplicate safety-warning dedup ------------------------------------------


def test_normalize_drops_plan_note_that_duplicates_a_red_flag():
    plan = normalize_structured_plan_candidate(
        {
            "red_flag_rules": [
                {"display_text": "Stop and report sharp knee pain.", "severity": "red"}
            ],
            "plan_notes": [
                {"category": "injury", "text": "Stop and report sharp knee pain."},
                {"category": "training", "text": "Stay disciplined with sleep."},
            ],
        }
    )
    note_texts = [n["text"] for n in plan["plan_notes"]]
    # The active-note echo of the red flag is dropped; the red flag is the
    # authoritative stop rule and is preserved untouched.
    assert "Stop and report sharp knee pain." not in note_texts
    assert "Stay disciplined with sleep." in note_texts
    assert len(plan["red_flag_rules"]) == 1
    assert plan["red_flag_rules"][0]["display_text"] == "Stop and report sharp knee pain."


def test_normalize_dedupes_repeated_plan_notes_keeping_first():
    plan = normalize_structured_plan_candidate(
        {
            "plan_notes": [
                {"category": "general", "text": "Do not train through dizziness."},
                {"category": "training", "text": "Do not train through dizziness."},
                {"category": "general", "text": "Do not train through dizziness."},
            ]
        }
    )
    assert len(plan["plan_notes"]) == 1
    assert plan["plan_notes"][0]["category"] == "general"


def test_normalize_collapses_identical_red_flag_rules_but_keeps_distinct_ones():
    plan = normalize_structured_plan_candidate(
        {
            "red_flag_rules": [
                {"display_text": "Stop if vision blurs.", "action": "End session.", "severity": "red", "when": "during_session"},
                {"display_text": "Stop if vision blurs.", "action": "End session.", "severity": "red", "when": "during_session"},
                {"display_text": "Pull back if pain exceeds 6/10.", "action": "Reduce load.", "severity": "amber", "when": "during_session"},
            ]
        }
    )
    texts = [r["display_text"] for r in plan["red_flag_rules"]]
    # Byte-for-byte duplicate collapses; the distinct stop rule survives.
    assert texts.count("Stop if vision blurs.") == 1
    assert "Pull back if pain exceeds 6/10." in texts


def test_normalize_keeps_safety_warning_in_at_least_one_place():
    # Even when a warning appears only as an active note (no red flag), it is
    # never removed — dedup only collapses true repeats.
    plan = normalize_structured_plan_candidate(
        {"plan_notes": [{"category": "injury", "text": "Stop and report numbness."}]}
    )
    assert [n["text"] for n in plan["plan_notes"]] == ["Stop and report numbness."]


def test_normalize_dedup_output_still_validates():
    plan_in = _valid_plan()
    plan_in["plan_notes"] = [
        {"category": "general", "label": "Active", "text": "Train as planned."},
        {"category": "general", "text": "Train as planned."},
    ]
    plan_out = normalize_structured_plan_candidate(plan_in)
    assert len(plan_out["plan_notes"]) == 1
    validate_structured_plan(plan_out)


# --- _normalize_measured: per-field default units ----------------------------


def test_normalize_measured_uses_default_unit_for_bare_numbers():
    assert _normalize_measured(90, "seconds") == {"value": 90.0, "unit": "seconds"}
    assert _normalize_measured(45, "minutes") == {"value": 45.0, "unit": "minutes"}
    assert _normalize_measured(400, "meters") == {"value": 400.0, "unit": "meters"}


def test_normalize_measured_parses_plain_numeric_strings_with_default_unit():
    assert _normalize_measured("90", "seconds") == {"value": 90.0, "unit": "seconds"}
    assert _normalize_measured("2.5", "minutes") == {"value": 2.5, "unit": "minutes"}


def test_normalize_measured_unit_is_dimension_aware():
    # "m" is minutes in a time field but meters in a distance field.
    assert _normalize_measured("5 m", "minutes") == {"value": 5.0, "unit": "minutes"}
    assert _normalize_measured("5 m", "meters") == {"value": 5.0, "unit": "meters"}
    assert _normalize_measured("90s", "seconds") == {"value": 90.0, "unit": "seconds"}


def test_normalize_measured_unparseable_returns_none():
    assert _normalize_measured("as long as needed", "seconds") is None
    assert _normalize_measured("", "seconds") is None
    assert _normalize_measured(None, "seconds") is None


def test_normalize_measured_coerces_dict_with_string_range_value():
    """The exact live failure: plan ranges written into the dict form.

    "full recovery 90–120 sec" came back as {"value": "90-120", "unit": "sec"}
    and the untouched pass-through failed MeasuredValue (value must be float),
    rejecting the whole card. A range reads its upper bound (effort convention;
    more rest is safer) and the unit is aliased within the field's dimension.
    """
    assert _normalize_measured({"value": "90-120", "unit": "sec"}, "seconds") == {
        "value": 120.0,
        "unit": "seconds",
    }
    assert _normalize_measured({"value": "5–6", "unit": "s"}, "seconds") == {
        "value": 6.0,
        "unit": "seconds",
    }
    assert _normalize_measured({"value": 45, "unit": "min"}, "minutes") == {
        "value": 45.0,
        "unit": "minutes",
    }


def test_normalize_measured_dict_without_readable_value_is_dropped():
    # No number anywhere → drop the optional field instead of failing the card.
    assert _normalize_measured({"unit": "seconds"}, "seconds") is None
    assert _normalize_measured({"value": None, "unit": "seconds"}, "seconds") is None
    assert _normalize_measured({"value": "as needed", "unit": "seconds"}, "seconds") is None


def test_normalize_measured_dict_reads_value_from_display_text():
    assert _normalize_measured({"unit": "sec", "display": "90-120 sec"}, "seconds") == {
        "value": 120.0,
        "unit": "seconds",
    }


def test_normalize_measured_dict_missing_unit_gets_field_default():
    assert _normalize_measured({"value": 90}, "seconds") == {
        "value": 90.0,
        "unit": "seconds",
    }


def test_normalize_measured_parses_bare_range_strings():
    assert _normalize_measured("90-120 sec", "seconds") == {"value": 120.0, "unit": "seconds"}
    assert _normalize_measured("60–90", "seconds") == {"value": 90.0, "unit": "seconds"}
    assert _normalize_measured("45-60 s", "seconds") == {"value": 60.0, "unit": "seconds"}


def test_block_measured_defaults_follow_bank_conventions():
    block = normalize_structured_plan_candidate(
        {
            "weeks": [
                {
                    "days": [
                        {
                            "sessions": [
                                {
                                    "blocks": [
                                        {"work": 30, "rest": 90, "duration": 12, "distance": 400}
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    )["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    assert block["work"] == {"value": 30.0, "unit": "seconds"}
    assert block["rest"] == {"value": 90.0, "unit": "seconds"}
    assert block["duration"] == {"value": 12.0, "unit": "minutes"}
    assert block["distance"] == {"value": 400.0, "unit": "meters"}


# --- _normalize_load: percentage + optional trailing ref ---------------------


def test_normalize_load_captures_trailing_ref():
    assert _normalize_load("85% 1RM") == {
        "method": "percentage",
        "value": 85.0,
        "unit": "percent",
        "display": "85% 1RM",
        "ref": "1RM",
    }


def test_normalize_load_captures_of_ref():
    load = _normalize_load("85% of 1RM")
    assert load["ref"] == "1RM"
    assert load["value"] == 85.0


def test_normalize_load_range_takes_top_value_with_ref():
    # Bank prescriptions read like "75-85% 1RM"; take the working top end.
    load = _normalize_load("75-85% 1RM")
    assert load["value"] == 85.0
    assert load["ref"] == "1RM"


def test_normalize_load_percent_without_ref_has_no_ref_key():
    load = _normalize_load("85%")
    assert load["method"] == "percentage"
    assert load["value"] == 85.0
    assert "ref" not in load  # group(2) was None and must not be used


def test_normalize_load_bodyweight_and_unparseable():
    assert _normalize_load("bodyweight")["method"] == "bodyweight"
    assert _normalize_load("as hard as possible") is None


# --- _normalize_load: dict form must be coerced, never passed through --------
#
# A load dict used to be returned untouched, so one prose method or one missing
# value on a single taper/rehab block failed the WHOLE card with
# "load.method: Input should be 'percentage'..." / "load.value: Field required".


def test_normalize_load_dict_with_prose_method_is_coerced():
    load = _normalize_load(
        {"method": "light DBs", "unit": "kg", "display": "light DBs (2-4 kg)"}
    )
    LoadPrescription.model_validate(load)
    assert load["method"] == "absolute"
    assert load["value"] == 4.0  # a range reads its working top end
    assert load["unit"] == "kg"


def test_normalize_load_dict_reads_number_from_display_when_value_missing():
    load = _normalize_load({"method": "dumbbell", "display": "2-4 kg"})
    LoadPrescription.model_validate(load)
    assert load["method"] == "absolute"
    assert load["value"] == 4.0


def test_normalize_load_dict_infers_method_from_display_context():
    load = _normalize_load({"display": "85% 1RM"})
    LoadPrescription.model_validate(load)
    assert load["method"] == "percentage"
    assert load["value"] == 85.0
    assert load["unit"] == "percent"


def test_normalize_load_dict_coerces_string_range_value():
    load = _normalize_load({"method": "percentage", "value": "75-85", "unit": "percent"})
    LoadPrescription.model_validate(load)
    assert load["value"] == 85.0


def test_normalize_load_dict_bodyweight_needs_no_number():
    load = _normalize_load({"method": "bodyweight"})
    LoadPrescription.model_validate(load)
    assert load["value"] == 0.0
    assert load["unit"] == "bodyweight"


def test_normalize_load_dict_already_valid_is_preserved():
    load = _normalize_load({"method": "percentage", "value": 88, "unit": "percent"})
    LoadPrescription.model_validate(load)
    assert load["method"] == "percentage"
    assert load["value"] == 88.0


def test_normalize_load_dict_reads_unit_anchored_number_not_rep_counts():
    # The real failing block: the dose names sets/reps BEFORE the load, so an
    # unanchored read would call it "2 kg" off "2 sets". The unit anchors it.
    load = _normalize_load(
        {
            "display": "YTW Raise Sequence (light DBs) - 2 sets x 8 reps per "
            "letter, light DBs (2-4 kg), tempo controlled 2-0-2, rest 45 sec."
        }
    )
    LoadPrescription.model_validate(load)
    assert load["method"] == "absolute"
    assert load["value"] == 4.0
    assert load["unit"] == "kg"


def test_normalize_load_dict_drops_when_no_load_can_be_read():
    # Digits that are NOT a load (a tempo cue, a rep scheme) must never be
    # scraped into a prescription — the optional field is dropped instead.
    assert _normalize_load({"method": "tempo", "display": "controlled 2-0-2"}) is None
    assert _normalize_load({"method": "other", "display": "2 sets x 8 reps"}) is None
    assert _normalize_load({"method": "text", "display": "light effort"}) is None
    assert _normalize_load({"method": "band", "display": "light band"}) is None
    assert _normalize_load({}) is None


def test_unreadable_load_no_longer_fails_the_whole_card():
    # Regression: blocks[3] carrying an unreadable load must not sink the plan.
    candidate = normalize_structured_plan_candidate(
        {
            "weeks": [
                {
                    "days": [
                        {
                            "sessions": [
                                {
                                    "blocks": [
                                        {"display_name": "Trap bar deadlift",
                                         "load": {"method": "percentage", "value": 80, "unit": "percent"}},
                                        {"display_name": "YTW Raise Sequence",
                                         "load": {"method": "light DBs (2-4 kg)"}},
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    )
    blocks = candidate["weeks"][0]["days"][0]["sessions"][0]["blocks"]
    assert blocks[0]["load"]["value"] == 80.0
    # The unreadable one is dropped, not left to fail schema validation.
    assert blocks[1]["load"] is None


# --- weight_cut_warning as a plain string ------------------------------------


def test_normalize_weight_cut_warning_string_is_wrapped():
    nutrition = normalize_structured_plan_candidate(
        {"nutrition": {"weight_cut_warning": "Cut 4% under qualified supervision."}}
    )["nutrition"]
    warning = nutrition["weight_cut_warning"]
    assert warning["risk_level"] == "none"
    assert warning["display_text"] == "Cut 4% under qualified supervision."
    assert warning["requires_professional_support"] is False
    # The wrapped object validates as part of the Nutrition schema.
    Nutrition.model_validate(nutrition)


def test_normalize_weight_cut_warning_empty_string_becomes_none():
    nutrition = normalize_structured_plan_candidate(
        {"nutrition": {"weight_cut_warning": "   "}}
    )["nutrition"]
    assert nutrition["weight_cut_warning"] is None


# --- bank -> StructuredTrainingPlan adapters ---------------------------------


def test_parse_bank_prescription_extracts_sets_reps_load():
    parsed = parse_bank_prescription("3x5 @ 75-85% 1RM")
    assert parsed["sets"] == 3
    assert parsed["reps"] == "5"
    assert parsed["load"]["method"] == "percentage"
    assert parsed["load"]["value"] == 85.0
    assert parsed["load"]["ref"] == "1RM"


def test_parse_bank_prescription_unparseable_is_empty():
    assert parse_bank_prescription("as needed") == {}
    assert parse_bank_prescription(None) == {}


def test_bank_strength_entry_converts_to_valid_block():
    entry = {
        "name": "Barbell Back Squat",
        "method": "strength",
        "category": "lower_body",
        "prescription": "3x5 @ 75-85% 1RM",
        "notes": "Foundational strength builder.",
        "impact_cost": "low",
    }
    block = bank_strength_to_block(entry)
    SessionBlock.model_validate(block)  # respects the schema
    assert block["display_name"] == "Barbell Back Squat"
    assert block["block_type"] == "strength"
    assert block["sets"] == 3
    assert block["load"]["unit"] == "percent"
    assert block["load"]["ref"] == "1RM"


def test_bank_conditioning_entry_converts_to_valid_block():
    entry = {
        "name": "Assault Bike Sprint Intervals",
        "system": "ATP-PCr",
        "work_sec": 10,
        "rest_sec": 50,
        "rounds": 8,
        "total_minutes": 8,
        "rpe": 9,
        "intensity": "max",
        "notes": "Posterior-chain drive without joint pounding.",
        "impact_cost": "low",
    }
    block = bank_conditioning_to_block(entry)
    SessionBlock.model_validate(block)
    assert block["block_type"] == "conditioning"
    assert block["work"] == {"value": 10.0, "unit": "seconds"}
    assert block["rest"] == {"value": 50.0, "unit": "seconds"}
    assert block["duration"] == {"value": 8.0, "unit": "minutes"}
    assert block["rounds"] == 8
    assert block["effort"] == {"method": "RPE", "value": 9, "scale": "1-10"}
    assert block["energy_system"] == "ATP-PCr"
    assert block["intensity"] == "max"


def test_real_bank_entries_convert_to_valid_blocks():
    # Representative live bank entries must convert without error or invention.
    strength = bank_strength_to_block(_first_bank_entry("exercise_bank.json"))
    SessionBlock.model_validate(strength)
    assert strength["display_name"]

    conditioning = bank_conditioning_to_block(_first_bank_entry("conditioning_bank.json"))
    SessionBlock.model_validate(conditioning)
    assert conditioning["block_type"] == "conditioning"
    # work_sec is seconds in the bank; the adapter must keep it in seconds.
    if "work" in conditioning:
        assert conditioning["work"]["unit"] == "seconds"


def test_bank_adapters_do_not_invent_content():
    # An almost-empty entry yields only structural defaults, no fabricated load.
    block = bank_strength_to_block({"name": "Mystery Lift"})
    assert block["display_name"] == "Mystery Lift"
    assert "load" not in block  # no prescription -> no invented load
    assert "sets" not in block


# --- should_attempt_structured_plan: canonical state-machine trigger ---------


def _displayable_plan(status: str) -> dict:
    return {"status": status, "final_plan_text": "# final plan", "structured_plan": None}


def test_should_attempt_for_ready_and_publishable_with_flags():
    assert should_attempt_structured_plan(_displayable_plan("ready"), True) is True
    assert should_attempt_structured_plan(_displayable_plan("publishable_with_flags"), True) is True


def test_should_not_attempt_for_non_displayable_statuses():
    for status in (
        "generated",
        "review_required",
        "held_for_review",
        "triage_blocked",
        "medical_hold",
        "needs_review",
        "restricted_rehab_only",  # safety-gated, intentionally excluded
        "archived",
    ):
        assert should_attempt_structured_plan(_displayable_plan(status), True) is False, status


def test_should_not_attempt_when_env_disabled():
    assert should_attempt_structured_plan(_displayable_plan("ready"), False) is False


def test_should_not_attempt_when_structured_plan_already_present():
    plan = _displayable_plan("ready")
    plan["structured_plan"] = {"schema_version": SCHEMA_VERSION}
    assert should_attempt_structured_plan(plan, True) is False


def test_should_not_attempt_without_final_plan_text():
    plan = _displayable_plan("ready")
    plan["final_plan_text"] = ""
    plan["plan_text"] = ""
    assert should_attempt_structured_plan(plan, True) is False


def test_should_attempt_uses_plan_text_when_final_missing():
    plan = {"status": "ready", "plan_text": "# displayed plan", "structured_plan": None}
    assert should_attempt_structured_plan(plan, True) is True


def test_should_not_attempt_for_non_dict():
    assert should_attempt_structured_plan(None, True) is False
    assert should_attempt_structured_plan("ready", True) is False


# --- PR-1: carry-through of already-supported schema fields ------------------


def test_normalize_preserves_mindset_optional_anchors():
    plan = normalize_structured_plan_candidate(
        {
            "weeks": [
                {
                    "days": [
                        {
                            "today_card": {
                                "mindset_anchor": {
                                    "intent": "Stay sharp",
                                    "focus_cue": "Hands up",
                                    "reset_cue": "Breathe",
                                    "confidence_anchor": "You've banked the work",
                                    "context": "First hard week",
                                }
                            },
                            "sessions": [
                                {
                                    "mindset_anchor": {
                                        "intent": "Move fast",
                                        "focus_cue": "Drive",
                                        "reset_cue": "Reset stance",
                                        "confidence_anchor": "Done this load before",
                                    }
                                }
                            ],
                        }
                    ]
                }
            ]
        }
    )
    day = plan["weeks"][0]["days"][0]
    assert day["today_card"]["mindset_anchor"]["confidence_anchor"] == "You've banked the work"
    assert day["today_card"]["mindset_anchor"]["context"] == "First hard week"
    assert day["sessions"][0]["mindset_anchor"]["confidence_anchor"] == "Done this load before"


def test_normalize_block_carries_coaching_detail():
    block = normalize_structured_plan_candidate(
        {
            "weeks": [
                {
                    "days": [
                        {
                            "sessions": [
                                {
                                    "blocks": [
                                        {
                                            "display_name": "Back Squat",
                                            "coaching_cues": ["Brace hard", "  ", "Knees out"],
                                            "regression_options": ["Goblet squat"],
                                            "substitutions": "Trap-bar deadlift",
                                            "progression_rule": "Add 2.5kg when all reps clean",
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    )["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    assert block["coaching_cues"] == ["Brace hard", "Knees out"]  # blanks dropped
    assert block["regression_options"] == ["Goblet squat"]
    assert block["substitutions"] == ["Trap-bar deadlift"]  # lone string wrapped
    assert block["progression_rule"] == "Add 2.5kg when all reps clean"


def test_full_block_and_mindset_detail_round_trips_through_outcome():
    # _valid_plan already carries coaching_cues/regression_options/substitutions on
    # its block; add the optional mindset anchors and confirm they survive.
    plan = _valid_plan()
    plan["weeks"][0]["days"][0]["sessions"][0]["mindset_anchor"]["confidence_anchor"] = "Anchor X"
    outcome = build_structured_plan_outcome(plan, raw_markdown=_faithful_source(plan))
    assert outcome.status == "valid"
    block = outcome.structured_plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    assert block["coaching_cues"]
    assert block["substitutions"]
    session = outcome.structured_plan["weeks"][0]["days"][0]["sessions"][0]
    assert session["mindset_anchor"]["confidence_anchor"] == "Anchor X"


def test_prompt_requires_block_detail_and_day_mindset():
    prompt = build_structured_plan_prompt(plan_markdown="# plan")
    for needle in (
        '"coaching_cues"',
        '"regression_options"',
        '"substitutions"',
        '"progression_rule"',
        "confidence_anchor",
        "today_card.mindset_anchor",
    ):
        assert needle in prompt, needle
    # Guardrail wording: omit, do not invent.
    assert "invent" in prompt.lower()


def _open_plan_brief(*training_days: str) -> dict:
    return {
        "payload_mode": "open_ongoing_payload",
        "render_mode": "open_ongoing_system",
        "open_plan_spec": {
            "plan_type": "open_ongoing_system",
            "weekly_template": {
                "training_days": list(training_days),
                "hard_sparring_days": [],
                "coach_owned_days": {},
            },
            "development_block": {
                "week_1": "Baseline and technical consistency",
                "week_2": "Small progression",
                "week_3": "Highest controlled week",
                "week_4": "Deload and reassess",
            },
        },
    }


def _open_plan_candidate(*, weekday: str | None) -> dict:
    plan = copy.deepcopy(_valid_plan())
    plan["plan_metadata"]["plan_type"] = "open_ongoing_system"
    plan["event_context"] = {"event_type": "none", "fight_date": None}
    plan["countdown_labels"] = []
    plan["daily_check_ins"] = []
    week = plan["weeks"][0]
    week["start_date"] = ""
    week["end_date"] = ""
    week["countdown_start"] = None
    week["countdown_end"] = None
    day = week["days"][0]
    day["date"] = ""
    day["weekday"] = weekday
    day["countdown_label"] = ""
    return plan


def test_open_plan_prompt_carries_authoritative_weekday_contract():
    prompt = build_structured_plan_prompt(
        plan_markdown="# Open plan",
        planning_brief=_open_plan_brief("Monday", "Tuesday", "Thursday"),
    )

    assert "OPEN ONGOING PLAN CONTRACT" in prompt
    assert 'plan_metadata.plan_type to "open_ongoing_system"' in prompt
    assert '["Mon", "Tue", "Thu"]' in prompt
    assert "Do not emit OFF/rest-only weekdays" in prompt
    assert 'event_context.event_type to "none"' in prompt


def test_open_plan_conversion_prompt_projects_authored_adjustments_only():
    prompt = build_structured_plan_prompt(
        plan_markdown="# Open plan",
        planning_brief=_open_plan_brief("Monday", "Tuesday"),
    )

    assert "OPEN-PLAN DELOAD" in prompt
    assert '"deload_rule"' in prompt
    assert 'Preserve each source "progression_rule" and "deload_rule" verbatim' in prompt
    assert "Missing source adjustment fields stay empty" in prompt
    assert "Never create a progression or Easier note" in " ".join(prompt.split())


def test_block_name_offering_a_choice_keeps_one_exercise_and_swaps_the_other():
    block = _normalized_block(
        {
            "display_name": "Short sprint bounds or low box jumps",
            "sets": 4,
            "reps": "3",
        }
    )
    assert block["display_name"] == "Short sprint bounds"
    assert "Low box jumps" in block["substitutions"]


def test_block_names_that_are_not_a_choice_are_left_alone():
    # A superset is a deliberate pairing, a parenthetical is one movement, and a
    # single-word side ("Bike or rower intervals") would lose the prescription.
    for name in (
        "Trap bar deadlift",
        "Barbell hip thrust + band walk",
        "Lateral raise (front or side)",
        "Bike or rower intervals",
    ):
        block = _normalized_block({"display_name": name})
        assert block["display_name"] == name, name
        assert block.get("substitutions", []) == [], name


def test_week_enumerated_progression_is_split_into_its_step_and_deload():
    """The card shows ONE week and labels it, so the line must not list weeks.

    This is the card from the app: "Week 2 add 1 more burst, week 3 add 1 more
    burst, week 4 keep same low volume." printed in full on every week.
    """
    block = _normalized_block(
        {
            "display_name": "Explosive First-Step Bursts",
            "sets": 4,
            "progression_rule": (
                "Week 2 add 1 more burst, week 3 add 1 more burst, "
                "week 4 keep same low volume."
            ),
        }
    )

    assert block["progression_rule"] == "Add 1 more burst."
    assert block["deload_rule"] == "Keep same low volume."


def test_a_week_label_is_stripped_from_a_deload_rule():
    block = _normalized_block(
        {"display_name": "Trap bar deadlift", "deload_rule": "Week 4 drop to two working sets."}
    )
    assert block["deload_rule"] == "Drop to two working sets."


def test_a_rule_naming_no_week_is_never_rewritten():
    rule = "Add 2.5 kg when all sets feel crisp."
    block = _normalized_block({"display_name": "Trap bar deadlift", "progression_rule": rule})
    assert block["progression_rule"] == rule
    assert "deload_rule" not in block


def test_a_deload_source_label_routes_to_deload_rule():
    block = _normalized_block(
        {
            "display_name": "Plank variation",
            "coaching_cues": ["Deload: hold for 20 sec and stop fresh.", "Brace your middle"],
        }
    )
    # Routed label text is carried verbatim, exactly as purpose/progression are.
    assert block["deload_rule"] == "hold for 20 sec and stop fresh."
    assert block["coaching_cues"] == ["Brace your middle"]


def test_hedge_only_progression_and_deload_lines_are_dropped():
    """The athlete cannot act on "slowly", so the card shows nothing instead.

    The frontend renders no week directive when the rule is absent, so dropping
    here is what guarantees an open card never prints an unactionable line.
    """
    block = _normalized_block(
        {
            "display_name": "Plank variation",
            "sets": 3,
            "progression_rule": "Increase hold time slowly while keeping form.",
            "deload_rule": "Cut the work back a bit.",
        }
    )
    assert "progression_rule" not in block
    assert "deload_rule" not in block


def test_a_real_target_survives_even_when_it_also_hedges():
    """Only lines that hedge INSTEAD of instructing are stripped."""
    for rule in (
        "Go to 3 x 40 sec while the ribs stay down.",  # named target
        "Add 2.5 kg, building slowly.",  # hedge alongside a real number
        "Add the smallest jump on the bar if the bicep stayed quiet.",  # no kg exists
        "Progress to live resistance once timing holds.",  # qualitative skill work
        # A hedge beside a real target is only tempo: the line still says what
        # to move to, so it must survive exactly as the unhedged wording does.
        "Progress to live resistance gradually once timing holds.",
        "Move to controlled partner resistance slowly once timing is stable.",
        "Build into full range gradually.",
        "Progress to live resistance if you can keep timing clean.",
        "Add reps only while it stays pain-free.",  # symptom-led rehab
        "Stays unchanged this block.",  # explicit no-progression
    ):
        block = _normalized_block({"display_name": "Drill", "progression_rule": rule})
        assert block.get("progression_rule") == rule, rule


def test_conditional_swap_is_never_routed_into_progression_rule():
    prompt = build_structured_plan_prompt(plan_markdown="# Plan")
    assert "Never put a conditional swap there" in " ".join(prompt.split())


def test_open_plan_conversion_prompt_forbids_authoring_missing_progress():
    prompt = build_structured_plan_prompt(
        plan_markdown="# Open plan",
        planning_brief=_open_plan_brief("Monday", "Tuesday"),
    )

    assert "Preserve any explicit value or bound" in prompt
    assert "Missing source adjustment fields stay empty" in prompt
    assert "not authoring adjustments" in prompt
    assert "You own the Progress" not in prompt
    assert "author a useful short note" not in prompt


def test_dated_camp_prompt_has_no_open_plan_progression_contract():
    prompt = build_structured_plan_prompt(
        plan_markdown="# Fight camp",
        planning_brief={"athlete_snapshot": {"sport": "boxing"}},
        event_date="2026-06-20",
    )

    assert "OPEN-PLAN DELOAD" not in prompt
    assert "deload_rule" not in prompt


def _normalized_block(block: dict) -> dict:
    return normalize_structured_plan_candidate(
        {"weeks": [{"days": [{"sessions": [{"blocks": [block]}]}]}]}
    )["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]


def test_block_deload_rule_is_trimmed_and_dropped_when_blank():
    kept = _normalized_block(
        {"display_name": "Trap-bar deadlift", "deload_rule": "  Drop to two working sets.  "}
    )
    assert kept["deload_rule"] == "Drop to two working sets."

    blank = _normalized_block({"display_name": "Trap-bar deadlift", "deload_rule": "  "})
    assert "deload_rule" not in blank


def test_open_plan_contract_repairs_schema_valid_card_without_weekday_identity():
    brief = _open_plan_brief("Monday")
    broken = _open_plan_candidate(weekday=None)

    first = build_structured_plan_outcome(broken, planning_brief=brief)

    assert first.status == "invalid_fallback_used"
    assert any("weekday order" in error for error in first.errors)

    fixed = _open_plan_candidate(weekday="Mon")
    repaired = build_structured_plan_outcome(
        broken,
        planning_brief=brief,
        repair_fn=lambda _data, _errors: fixed,
    )

    assert repaired.status == "repair_attempted_valid"
    assert repaired.structured_plan["plan_metadata"]["plan_type"] == "open_ongoing_system"
    assert repaired.structured_plan["weeks"][0]["days"][0]["weekday"] == "Mon"


# --- malformed daily_check_ins tolerance (PR-7) ------------------------------


def _good_check_in() -> dict:
    return {
        "date": "2026-05-29",
        "morning": {"sleep_quality": 4, "overall_readiness": 4, "pain": 2},
        "decision": "train_as_planned",
        "rules_triggered": [],
    }


def test_missing_daily_check_ins_keeps_safe_empty_default():
    # Requirement 1: a missing key stays the model's empty-list default.
    assert _normalize_daily_check_ins(None) == []
    assert _normalize_daily_check_ins({"x": 1}) == []
    assert _normalize_daily_check_ins([]) == []


def test_valid_check_in_is_preserved():
    out = _normalize_daily_check_ins([_good_check_in()])
    assert len(out) == 1
    assert out[0]["date"] == "2026-05-29"
    assert out[0]["morning"]["sleep_quality"] == 4
    assert out[0]["decision"] == "train_as_planned"


def test_malformed_check_in_does_not_cause_invalid_fallback():
    # Requirement 6: malformed daily_check_ins alone must not sink the plan.
    plan = _valid_plan()
    plan["daily_check_ins"] = [
        {"morning": {"sleep_quality": 4}},  # missing date/decision, partial morning
        {"date": "2026-05-30", "decision": "definitely_yes"},  # bad decision, no morning
        _good_check_in(),  # one fully valid entry survives
    ]
    outcome = build_structured_plan_outcome(plan, raw_markdown=_faithful_source(plan))
    assert outcome.status in {"valid", "repair_attempted_valid"}
    validate_structured_plan(outcome.structured_plan)
    surviving = outcome.structured_plan["daily_check_ins"]
    assert len(surviving) == 1
    assert surviving[0]["date"] == "2026-05-29"


def test_entry_missing_date_is_dropped_when_not_inferable():
    # Requirement 5: no date and no resolvable index -> drop.
    entry = _good_check_in()
    entry.pop("date")
    assert _normalize_daily_check_ins([entry]) == []


def test_entry_missing_date_is_inferred_from_unambiguous_index():
    # Requirement 5: infer from week/day index only when it resolves uniquely.
    entry = _good_check_in()
    entry.pop("date")
    entry["week_index"] = 1
    entry["day_index"] = 0
    weeks = [
        {"week_index": 1, "days": [{"date": "2026-06-01"}, {"date": "2026-06-02"}]},
    ]
    out = _normalize_daily_check_ins([entry], weeks)
    assert len(out) == 1
    assert out[0]["date"] == "2026-06-01"


def test_entry_with_ambiguous_index_is_dropped():
    entry = _good_check_in()
    entry.pop("date")
    entry["week_index"] = 1
    entry["day_index"] = 0
    # Two weeks share week_index 1 -> the index does not resolve uniquely.
    weeks = [
        {"week_index": 1, "days": [{"date": "2026-06-01"}]},
        {"week_index": 1, "days": [{"date": "2026-07-01"}]},
    ]
    assert _normalize_daily_check_ins([entry], weeks) == []


def test_entry_missing_morning_is_dropped_no_fake_biometrics():
    # Requirement 3: no neutral default exists for self-report scores -> drop.
    entry = _good_check_in()
    entry.pop("morning")
    assert _normalize_daily_check_ins([entry]) == []


def test_entry_with_incomplete_morning_is_dropped():
    entry = _good_check_in()
    entry["morning"] = {"sleep_quality": 4, "overall_readiness": 3}  # pain missing
    assert _normalize_daily_check_ins([entry]) == []


def test_entry_with_out_of_range_morning_is_dropped():
    entry = _good_check_in()
    entry["morning"]["overall_readiness"] = 9  # out of 1-5
    assert _normalize_daily_check_ins([entry]) == []


def test_numeric_string_scores_are_coerced_not_invented():
    # Coercing "4" -> 4 is a formatting fix, not fabrication.
    entry = _good_check_in()
    entry["morning"] = {"sleep_quality": "4", "overall_readiness": "5", "pain": "0"}
    out = _normalize_daily_check_ins([entry])
    assert len(out) == 1
    assert out[0]["morning"] == {"sleep_quality": 4, "overall_readiness": 5, "pain": 0}


def test_invalid_decision_is_dropped():
    # Requirement 4: an unrecognized decision is dropped, never defaulted.
    entry = _good_check_in()
    entry["decision"] = "feeling_great"
    assert _normalize_daily_check_ins([entry]) == []


def test_obvious_decision_alias_is_mapped_to_valid_enum():
    # Requirement 4: only obvious aliases map to a real enum value.
    for alias, expected in (
        ("Train_As_Planned", "train_as_planned"),
        ("modified", "modify"),
        ("pull back", "pull_back"),
        ("unavailable", "unavailable"),
    ):
        entry = _good_check_in()
        entry["decision"] = alias
        out = _normalize_daily_check_ins([entry])
        assert len(out) == 1, alias
        assert out[0]["decision"] == expected


def test_no_fake_biometric_keys_introduced():
    # The normalizer never adds biometric/readiness scores beyond self-report.
    out = _normalize_daily_check_ins([_good_check_in()])
    morning_keys = set(out[0]["morning"])
    assert morning_keys <= {"sleep_quality", "overall_readiness", "pain", "location", "injury_specific"}
    assert not (morning_keys & BANNED_BIOMETRIC_KEYS)


def test_plan_with_only_malformed_check_ins_still_valid():
    # Requirement 2/6: drop everything -> empty list, plan still validates.
    plan = _valid_plan()
    plan["daily_check_ins"] = [
        {"morning": {"sleep_quality": 4}},
        {"date": "2026-05-30", "decision": "nope"},
    ]
    outcome = build_structured_plan_outcome(plan, raw_markdown=_faithful_source(plan))
    assert outcome.status in {"valid", "repair_attempted_valid"}
    assert outcome.structured_plan["daily_check_ins"] == []
    validate_structured_plan(outcome.structured_plan)


def test_plan_text_fallback_still_works_for_fully_invalid_structure():
    # The existing fallback is untouched when the whole structure is invalid.
    outcome = build_structured_plan_outcome(_invalid_plan(), raw_markdown="# raw")
    assert outcome.status == "invalid_fallback_used"
    assert outcome.structured_plan is None


def test_prompt_requires_valid_or_omitted_check_ins():
    prompt = build_structured_plan_prompt(plan_markdown="# plan")
    assert "daily_check_ins" in prompt
    assert "fully valid or omitted" in prompt
    assert "partial check-in" in prompt


# Source-authority regression: 1 Nov 2026 open plan introduced four Week 4 lifts.
def _open_membership_case():
    from api.structured_plan_faithfulness import check_structured_faithfulness

    plan = _open_plan_candidate(weekday="Sun")
    plan["weeks"][0]["days"][0]["sessions"][0]["title"] = "Strength"
    block = plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    block["display_name"] = "Heavy RDL → Broad Jump"
    block["block_type"] = "strength_speed"
    brief = _open_plan_brief("Sunday")
    source = "Session cards\nSunday - Strength\n- Heavy RDL → Broad Jump: prescribed strength contrast.\n- Sled Push (Speed): 3 x 5 sec.\n"
    return plan, brief, source, check_structured_faithfulness


def test_open_plan_without_countdown_rejects_invented_week_four_exercises():
    plan, brief, source, check = _open_membership_case()
    for index in range(2, 5):
        week = copy.deepcopy(plan["weeks"][0])
        week["week_index"] = index
        plan["weeks"].append(week)
    strength = plan["weeks"][3]["days"][0]["sessions"][0]
    for name in ("Tire Flip", "Landmine Rotational Press", "Woodchopper (Cable)", "Lat Pulldown"):
        block = copy.deepcopy(strength["blocks"][0])
        block["display_name"] = name
        strength["blocks"].append(block)
    violations = check(plan, source, brief)
    assert len([v for v in violations if v.startswith("INTRODUCED: open week 4")]) == 4


def test_open_plan_source_matches_exact_identity_and_dose_title_format():
    plan, brief, source, check = _open_membership_case()
    assert not any(v.startswith("INTRODUCED") for v in check(plan, source, brief))
    block = plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    block["display_name"] = "Sled Push (Heavy)"
    assert any(v.startswith("INTRODUCED") for v in check(plan, source, brief))
    block["display_name"] = "Sled Push (Speed)"
    assert not any(v.startswith("INTRODUCED") for v in check(plan, source, brief))
    plan["weeks"][0]["days"][0]["weekday"] = "Mon"
    assert any(v.startswith("INTRODUCED") for v in check(plan, source, brief))

    plan["weeks"][0]["days"][0]["weekday"] = "Thu"
    plan["weeks"][0]["days"][0]["sessions"][0]["title"] = "Alactic sharpness"
    block["display_name"] = "Short Hill Sprints (5x10s)"
    source += "Thursday - Alactic sharpness (Short hill sprints)\n- Short Hill Sprints (5 x 10 sec): 5 rounds.\n"
    assert not any(v.startswith("INTRODUCED") for v in check(plan, source, brief))


def test_open_plan_planner_owned_four_week_exercise_is_authorised():
    plan, brief, source, check = _open_membership_case()
    brief["open_plan_spec"]["weekly_session_plan"] = {
        "authority": "deterministic_open_development_block",
        "weeks": [{"week": 1, "sessions": [
            {"weekday": "Sunday", "exercises": [{"name": "Tire Flip"}]},
        ]}],
    }
    plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["display_name"] = "Tire Flip"
    assert not any(v.startswith("INTRODUCED") for v in check(plan, source, brief))
    plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["display_name"] = "Lat Pulldown"
    assert any(v.startswith("INTRODUCED") for v in check(plan, source, brief))


def test_open_plan_custom_and_dated_paths_remain_outside_new_gate():
    plan, brief, source, check = _open_membership_case()
    plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["display_name"] = "Tire Flip"
    assert any(v.startswith("INTRODUCED") for v in check(plan, source, brief))
    assert not any(v.startswith("INTRODUCED") for v in check(plan, source, {**brief, "custom_plan": True}))
    assert not any(v.startswith("INTRODUCED") for v in check(plan, source, {}))


def test_open_plan_unapproved_conversion_falls_back_before_publishing():
    plan, brief, source, _ = _open_membership_case()
    plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["display_name"] = "Tire Flip"
    outcome = build_structured_plan_outcome(plan, raw_markdown=source, planning_brief=brief)
    assert outcome.status == "invalid_fallback_used"
    assert any("INTRODUCED: open week" in error for error in outcome.errors)
    assert outcome.structured_plan is None


def test_saved_open_plan_with_unapproved_exercise_is_not_returned_to_today():
    from api.services.effective_structured_plan import resolve_effective_structured_plan

    plan, brief, source, _ = _open_membership_case()
    row = {"structured_plan": plan, "planning_brief": brief, "plan_text": source, "fight_date": None}
    assert resolve_effective_structured_plan(row) is not None
    plan["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["display_name"] = "Tire Flip"
    assert resolve_effective_structured_plan(row) is None
