"""Tests for the structured training plan schema and validation helpers."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
from pydantic import ValidationError

from api.structured_plan_models import (
    SCHEMA_VERSION,
    Session,
    SessionBlock,
    StructuredTrainingPlan,
    _STRICT_UNSUPPORTED_KEYWORDS,
    _is_free_form_object,
    build_strict_structured_plan_schema,
    repair_structured_plan_once,
    safe_parse_structured_plan,
    validate_structured_plan,
)


def test_sc_screenshot_rows_consolidate_at_the_shared_validation_boundary():
    text = (Path(__file__).parent / "fixtures/sc_week5.txt").read_text(encoding="utf-8")
    rows = []
    code = name = None
    for line in text.splitlines()[4:]:
        row_code, row_name, percentage, prescription, weight = [s.strip() for s in line.strip("|").split("|")]
        code, name = row_code or code, row_name or name
        parts = prescription.split("x", 1)
        stage = {"reps": parts[-1]}
        if len(parts) == 2:
            stage["sets"] = int(parts[0])
        if percentage:
            stage["percentage"] = float(percentage.strip("%"))
        if weight:
            stage["source_text"] = f"WT: {weight}"
        rows.append({"block_id": f"row-{len(rows)}", "block_type": "strength",
                     "display_name": f"{code}. {name}", "prescribed_sets": [stage]})
    raw = _session()
    raw.update(title="Day 1 (Total)", blocks=rows)
    session = Session.model_validate(raw)
    assert [b.exercise_code for b in session.blocks] == ["A1", "A2", "B1", "B2", "C1", "C2", "D1"]
    assert [len(b.prescribed_sets) for b in session.blocks] == [8, 1, 6, 1, 3, 1, 1]
    assert all(b.group_type is None for b in session.blocks)
    clean = session.blocks[0]
    assert clean.display_name == "Clean" and clean.exercise_block == "A"
    assert [s.percentage for s in clean.prescribed_sets] == [50, 60, 70, 75, 80, 85, 90, 95]
    assert clean.prescribed_sets[-1].sets == 3 and clean.prescribed_sets[-1].reps == "1"
    assert clean.prescribed_sets[-1].source_text == "WT: 225"
    assert session.blocks[2].prescribed_sets[-1].sets == 7
    assert [s.reps for s in session.blocks[4].prescribed_sets] == ["4e"] * 3
    assert session.blocks[1].prescribed_sets[0].reps == "25 sec"
    assert session.blocks[3].prescribed_sets[0].reps == "2 Ea"
    # The saved form is stable when read/validated again.
    assert Session.model_validate(session.model_dump()).model_dump() == session.model_dump()


def test_legacy_coded_dose_rows_merge_without_losing_load_or_changing_uncoded_work():
    raw = _session()
    raw["blocks"] = [
        {"block_id": str(i), "block_type": "strength", "display_name": "Z12 Clean",
         "reps": 1, "load": {"method": "percentage", "value": p, "unit": "percent", "display": f"{p}%"}}
        for i, p in enumerate([50, 60, 70])
    ]
    raw["blocks"] += [dict(_block(), block_id=f"uncoded-{i}") for i in range(2)]
    session = Session.model_validate(raw)
    assert len(session.blocks) == 3
    assert session.blocks[0].exercise_block == "Z"
    assert [s.load.value for s in session.blocks[0].prescribed_sets] == [50, 60, 70]
    assert session.blocks[0].load is None
    assert session.blocks[1].prescribed_sets == []


@pytest.mark.parametrize("kind", ["superset", "circuit", "alternating_sets"])
def test_explicit_groups_survive_without_merging_different_codes(kind):
    raw = _session()
    raw["blocks"] = [dict(_block(), display_name=f"A{i} Squat", group_type=kind) for i in (1, 2)]
    blocks = Session.model_validate(raw).blocks
    assert len(blocks) == 2
    assert [b.group_type for b in blocks] == [kind, kind]
    assert [b.exercise_code for b in blocks] == ["A1", "A2"]


def test_continuation_without_an_initial_dose_does_not_invent_a_set():
    raw = _session()
    raw["blocks"] = [
        {"block_id": "a", "block_type": "strength", "display_name": "A1 Clean"},
        {"block_id": "b", "block_type": "strength", "display_name": "A1 Clean", "reps": 2},
    ]
    assert len(Session.model_validate(raw).blocks[0].prescribed_sets) == 1


def _walk_schema_nodes(node, path="$"):
    """Yield (path, node) for every dict node in a JSON schema."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk_schema_nodes(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_schema_nodes(value, f"{path}[{index}]")


def test_strict_schema_is_structurally_openai_compliant():
    # The generated schema must satisfy OpenAI strict mode's structural rules:
    # every object closes additionalProperties and requires all of its
    # properties, no strict-forbidden keywords survive, and no free-form object
    # remains. (Live acceptance is validated separately in staging.)
    schema = build_strict_structured_plan_schema()
    for path, node in _walk_schema_nodes(schema):
        for banned in _STRICT_UNSUPPORTED_KEYWORDS:
            assert banned not in node, f"{banned} survived at {path}"
        assert not _is_free_form_object(node), f"free-form object at {path}"
        props = node.get("properties")
        if isinstance(props, dict):
            assert node.get("additionalProperties") is False, f"open object at {path}"
            assert set(node.get("required", [])) == set(props), f"required!=all props at {path}"


def test_strict_schema_reduces_server_injected_field_to_null():
    # deterministic_support is injected server-side (never model-generated) and is
    # a free-form dict, which strict mode forbids: it must collapse to null-only.
    schema = build_strict_structured_plan_schema()
    det = schema["properties"]["deterministic_support"]
    variants = det.get("anyOf", [det])
    assert all(v.get("type") == "null" for v in variants)
    # The root still requires every property (strict mode).
    assert set(schema["required"]) == set(schema["properties"])


def _block() -> dict:
    return {
        "block_id": "blk-1",
        "block_type": "strength",
        "display_name": "Barbell Back Squat",
        "order_index": 1,
        "sets": 4,
        "reps": "4-6",
        "load": {
            "method": "percentage",
            "value": 85,
            "unit": "percent",
            "ref": "1RM",
            "display": "85% 1RM",
        },
        "effort": {"method": "RPE", "value": 7, "scale": "1-10"},
        "tempo": {"eccentric": 3, "pause_bottom": 1, "concentric": "X", "pause_top": 0},
        "rest": {"value": 180, "unit": "seconds"},
        "purpose": "Build general force without provoking posterior soreness.",
        "coaching_cues": ["Controlled eccentric", "Knee-driven ascent"],
        "regression_options": ["Goblet squat"],
        "substitutions": ["Trap-bar deadlift"],
        "red_flags": [_red_flag_rule()],
    }


def _red_flag_rule() -> dict:
    return {
        "rule_id": "achilles-pain-high",
        "metric": "achilles_pain",
        "metric_group": "posterior_leg",
        "when": "morning_check_in",
        "operator": ">=",
        "threshold": 6,
        "logic": "achilles_pain >= 6",
        "severity": "red",
        "applies_to": ["session:strength_power", "block:strength"],
        "display_text": "Sharp pain behind the leg at 6/10 or higher — stop and report.",
        "action": "Replace heavy hinge with isometric holds and notify coach.",
        "replacement_session_type": "rehab",
        "affected_blocks": ["blk-1"],
        "needs_human_review": True,
    }


def _mindset() -> dict:
    return {
        "intent": "Move fast, stay relaxed.",
        "focus_cue": "Drive the floor away.",
        "reset_cue": "Breathe out, reset stance.",
        "confidence_anchor": "You have done this rep at this load before.",
    }


def _session() -> dict:
    return {
        "session_id": "ses-1",
        "session_type": "strength_power",
        "title": "Power Transfer Touch",
        "objective": "Raise usable punch speed with one focused power exposure.",
        "planned_duration": {"value": 45, "unit": "minutes"},
        "primary_stressor": "neural",
        "cns_demand": "moderate",
        "impact_level": "low",
        "completion_status": "not_started",
        "mindset_anchor": _mindset(),
        "blocks": [_block()],
    }


def _day() -> dict:
    return {
        "date": "2026-05-29",
        "day_type": "high",
        "countdown_label": "D-15",
        "phase_label": "SPP",
        "today_card": {
            "headline": "Power day — keep it crisp.",
            "readiness_status": "train_as_planned",
            "primary_warning": None,
            "nutrition_summary": "Carbs and fluids around the session.",
            "weight_cut_warning": None,
            "mindset_anchor": _mindset(),
        },
        "sessions": [_session()],
    }


def _week() -> dict:
    return {
        "week_id": "wk-1",
        "week_index": 1,
        "phase_label": "SPP",
        "week_goal": "Concentrate fight-specific power transfer.",
        "start_date": "2026-05-25",
        "end_date": "2026-05-31",
        "countdown_start": "D-19",
        "countdown_end": "D-13",
        "load_focus": {
            "volume": "reduced",
            "intensity": "high",
            "specificity": "high",
            "fatigue_target": "reduced",
        },
        "progression": {
            "week_type": "specific_peak",
            "planned_change_from_previous": "Trim accessory volume, hold intensity.",
        },
        "days": [_day()],
    }


def _daily_check_in() -> dict:
    return {
        "date": "2026-05-29",
        "morning": {
            "sleep_quality": 4,
            "overall_readiness": 4,
            "pain": 2,
            "location": "behind right knee",
            "injury_specific": {"knee_giving_way": False},
        },
        "decision": "train_as_planned",
        "rules_triggered": [],
    }


def _nutrition() -> dict:
    return {
        "summary": "Fuel around main sessions, protect freshness during the cut.",
        "daily_focus": "Prioritise protein and carbs spread across the day.",
        "training_day_guidance": "Carbs and fluids before and after key sessions.",
        "fight_week_guidance": "Follow coach/medical guidance for the final cut.",
        "weight_cut_warning": {
            "risk_level": "amber",
            "display_text": "5.7% cut — manage with qualified supervision.",
            "requires_professional_support": True,
        },
    }


def _valid_plan() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "plan_metadata": {
            "title": "Fight Camp — 8 Weeks",
            "sport": "boxing",
            "plan_type": "fight_camp",
            "timezone": "Europe/London",
            "status": "active",
            "units": "metric",
        },
        "athlete_context": {
            "sport_profile": "amateur boxer, orthodox",
            "style_profile": "pressure boxer",
            "experience_level": "intermediate",
            "sex": "male",
            "age": 24,
            "body_mass": {"value": 71.5, "unit": "kg"},
            "weight_class": "welterweight",
            "injury_status": "minor",
            "known_issues": ["sore behind the legs"],
            "equipment_access": ["barbell", "bands", "assault_bike"],
            "constraints": ["no heavy eccentric hamstring loading"],
        },
        "event_context": {
            "fight_date": "2026-06-13",
            "weigh_in_date": "2026-06-12",
            "event_type": "fight",
            "ruleset": "amateur boxing",
        },
        "countdown_labels": [
            {"date": "2026-05-16", "days_to_event": 28, "label": "D-28", "anchor": "fight"},
            {"date": "2026-06-13", "days_to_event": 0, "label": "D0", "anchor": "fight"},
        ],
        "red_flag_rules": [_red_flag_rule()],
        "weeks": [_week()],
        "daily_check_ins": [_daily_check_in()],
        "nutrition": _nutrition(),
        "progression_notes": "Reduce optional work as the cut progresses.",
        "raw_markdown_fallback": "# Fight Camp\n...",
    }


# A. Valid structured fight-camp plan validates successfully.
def test_valid_fight_camp_plan_validates():
    plan = validate_structured_plan(_valid_plan())
    assert isinstance(plan, StructuredTrainingPlan)
    assert plan.plan_metadata.plan_type == "fight_camp"
    assert plan.schema_version == SCHEMA_VERSION


def test_open_ongoing_plan_type_validates():
    data = _valid_plan()
    data["plan_metadata"]["plan_type"] = "open_ongoing_system"

    plan = validate_structured_plan(data)

    assert plan.plan_metadata.plan_type == "open_ongoing_system"


# A2. coach_led_contact on today_card survives validation (it carries the
# coach-owned label for a sparring day that also has an app session).
def test_today_card_coach_led_contact_survives_validation():
    data = _valid_plan()
    data["weeks"][0]["days"][0]["today_card"]["coach_led_contact"] = (
        "Coach-led boxing — technical only"
    )
    plan = validate_structured_plan(data)
    today_card = plan.weeks[0].days[0].today_card
    assert today_card.coach_led_contact == "Coach-led boxing — technical only"


# B. Multi-week plan validates: weeks[] -> days[] -> sessions[] -> blocks[].
def test_multi_week_plan_validates():
    data = _valid_plan()
    second_week = copy.deepcopy(_week())
    second_week["week_id"] = "wk-2"
    second_week["week_index"] = 2
    second_week["phase_label"] = "TAPER"
    data["weeks"].append(second_week)

    plan = validate_structured_plan(data)

    assert len(plan.weeks) == 2
    assert [w.phase_label for w in plan.weeks] == ["SPP", "TAPER"]
    week = plan.weeks[0]
    assert week.days[0].sessions[0].blocks[0].display_name == "Barbell Back Squat"


def test_schema_supports_all_required_phases():
    phases = {"GPP", "SPP", "TAPER", "FIGHT_WEEK", "REINTEGRATION"}
    for phase in phases:
        data = _valid_plan()
        data["weeks"][0]["phase_label"] = phase
        data["weeks"][0]["days"][0]["phase_label"] = phase
        plan = validate_structured_plan(data)
        assert plan.weeks[0].phase_label == phase


# C. Invalid string-only load should fail; structured load object works.
def test_string_only_load_is_rejected():
    data = _valid_plan()
    data["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["load"] = "85%"
    with pytest.raises(ValidationError):
        validate_structured_plan(data)


def test_structured_load_object_is_accepted():
    plan = validate_structured_plan(_valid_plan())
    load = plan.weeks[0].days[0].sessions[0].blocks[0].load
    assert load is not None
    assert load.method == "percentage"
    assert load.value == 85
    assert load.display == "85% 1RM"


# D. Completion status enum works.
@pytest.mark.parametrize("status", ["not_started", "done", "modified", "skipped"])
def test_completion_status_enum_values(status):
    data = _valid_plan()
    data["weeks"][0]["days"][0]["sessions"][0]["completion_status"] = status
    plan = validate_structured_plan(data)
    assert plan.weeks[0].days[0].sessions[0].completion_status == status


def test_invalid_completion_status_rejected():
    data = _valid_plan()
    data["weeks"][0]["days"][0]["sessions"][0]["completion_status"] = "finished"
    with pytest.raises(ValidationError):
        validate_structured_plan(data)


def test_completion_log_records_outcome():
    data = _valid_plan()
    data["weeks"][0]["days"][0]["sessions"][0]["completion"] = {
        "session_rpe": 7.5,
        "pain_after_session": 3,
        "performed_duration": {"value": 40, "unit": "minutes"},
        "modification_reason": "Cut last accessory block.",
        "notes": "Felt sharp.",
        "completed_at": "2026-05-29T18:30:00Z",
    }
    plan = validate_structured_plan(data)
    completion = plan.weeks[0].days[0].sessions[0].completion
    assert completion is not None
    assert completion.session_rpe == 7.5
    assert completion.pain_after_session == 3


# E. Red flag rule supports machine fields, display_text, action.
def test_red_flag_rule_separates_machine_and_display():
    plan = validate_structured_plan(_valid_plan())
    rule = plan.red_flag_rules[0]
    assert rule.metric == "achilles_pain"
    assert rule.operator == ">="
    assert rule.threshold == 6
    assert rule.severity == "red"
    # Display text is human-readable, not the raw machine logic expression.
    assert rule.display_text
    assert rule.logic not in rule.display_text
    assert rule.action
    assert rule.replacement_session_type == "rehab"
    assert rule.needs_human_review is True


# F. Daily check-in supports sleep_quality, overall_readiness, pain, decision.
def test_daily_check_in_self_report_fields():
    plan = validate_structured_plan(_valid_plan())
    check_in = plan.daily_check_ins[0]
    assert check_in.morning.sleep_quality == 4
    assert check_in.morning.overall_readiness == 4
    assert check_in.morning.pain == 2
    assert check_in.decision == "train_as_planned"


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("sleep_quality", 0),
        ("sleep_quality", 6),
        ("overall_readiness", 6),
        ("pain", 11),
        ("pain", -1),
    ],
)
def test_morning_check_in_bounds_enforced(field, bad_value):
    data = _valid_plan()
    data["daily_check_ins"][0]["morning"][field] = bad_value
    with pytest.raises(ValidationError):
        validate_structured_plan(data)


# G. Raw fallback works when structured validation fails.
def test_safe_parse_preserves_raw_markdown_on_failure():
    result = safe_parse_structured_plan({"plan_metadata": {}}, raw_markdown="# fallback plan")
    assert result.ok is False
    assert result.plan is None
    assert result.raw_markdown_fallback == "# fallback plan"
    assert result.errors  # validation errors exposed for logging


def test_safe_parse_succeeds_and_injects_raw_markdown():
    data = _valid_plan()
    data["raw_markdown_fallback"] = ""
    result = safe_parse_structured_plan(data, raw_markdown="# generated markdown")
    assert result.ok is True
    assert result.plan is not None
    assert result.plan.raw_markdown_fallback == "# generated markdown"


def test_root_requires_raw_markdown_fallback_field():
    plan = validate_structured_plan(_valid_plan())
    assert plan.raw_markdown_fallback == "# Fight Camp\n..."


# Repair-retry hook placeholder.
def test_repair_hook_recovers_with_repair_fn():
    broken = _valid_plan()
    broken["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["load"] = "85%"

    def repair_fn(raw, errors):
        fixed = copy.deepcopy(raw)
        fixed["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]["load"] = {
            "method": "percentage",
            "value": 85,
            "unit": "percent",
            "ref": "1RM",
            "display": "85% 1RM",
        }
        return fixed

    result = repair_structured_plan_once(broken, repair_fn=repair_fn, raw_markdown="# fb")
    assert result.ok is True
    assert result.plan is not None


def test_repair_hook_falls_back_without_repair_fn():
    broken = {"nope": True}
    result = repair_structured_plan_once(broken, raw_markdown="# fb")
    assert result.ok is False
    assert result.raw_markdown_fallback == "# fb"
    assert result.errors


def test_repair_hook_keeps_fallback_when_repair_raises():
    broken = {"nope": True}

    def repair_fn(raw, errors):
        raise RuntimeError("model unavailable")

    result = repair_structured_plan_once(broken, repair_fn=repair_fn, raw_markdown="# fb")
    assert result.ok is False
    assert result.raw_markdown_fallback == "# fb"
    assert any("repair failed" in err for err in result.errors)


# H. Existing raw-only saved plan compatibility remains intact (mapper layer).
def test_legacy_plan_row_maps_without_structured_plan():
    from api.plan_mappers import _decode_structured_plan

    structured, version = _decode_structured_plan(None, raw_markdown="# legacy text")
    assert structured is None
    assert version is None


def test_structured_plan_row_decodes_when_present():
    from api.plan_mappers import _decode_structured_plan

    structured, version = _decode_structured_plan(_valid_plan(), raw_markdown="# md")
    assert structured is not None
    assert version == SCHEMA_VERSION


def test_malformed_structured_plan_column_warns_and_falls_back(caplog):
    # A stored structured payload that no longer parses must surface a warning
    # (not silently drop to the markdown fallback) so a regression is visible.
    from api.plan_mappers import _decode_structured_plan

    malformed = {"schema_version": "x", "weeks": "not-a-list"}
    with caplog.at_level("WARNING", logger="api.plan_mappers"):
        structured, version = _decode_structured_plan(malformed, raw_markdown="# md")
    assert structured is None
    assert version is None
    assert any("structured_plan column failed to parse" in r.message for r in caplog.records)


@pytest.mark.parametrize("contact", ["none", "controlled", "full", "unknown"])
def test_block_preserves_explicit_contact_level(contact):
    block = SessionBlock.model_validate({"block_id": "b", "block_type": "conditioning",
                                        "display_name": "Conditioning", "contact_level": contact})
    assert block.model_dump()["contact_level"] == contact


def test_block_preserves_supplied_load_regions_and_movement_tags():
    block = SessionBlock.model_validate({"block_id": "b", "block_type": "strength", "display_name": "Work",
                                        "mechanical_load_regions": ["chest"], "tags": ["horizontal_push"]})
    assert block.model_dump()["mechanical_load_regions"] == ["chest"]
    assert block.model_dump()["tags"] == ["horizontal_push"]
    unknown = SessionBlock.model_validate({"block_id": "b", "block_type": "strength", "display_name": "Work"})
    assert unknown.mechanical_load_regions is None and unknown.tags is None
