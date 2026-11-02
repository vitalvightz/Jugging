"""Stage 2 coaching survives without touching the prescription (mocked model).

SYNTHETIC replay, not validation against a production payload. Stage 1 is
regenerated from an anonymised copy of the power / conditioning / speed boxer
intake (trunk-strength weakness) used in the GPT-6 Luna comparison, at its
original request time. That generation's raw Stage 2 response was never
persisted, so the model output here is the hand-written Luna-style render in
``stage2_luna_style`` (no long dashes in headers, as the prompt demanded),
with deliberately planted invalid lines. No production ids are asserted.
"""
import asyncio
from datetime import datetime
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.models import PlanRequest
from api.stage2_automation import OpenAIStage2Automator
from api.structured_plan_generation import build_structured_plan_outcome
from api.services.effective_structured_plan import resolve_effective_structured_plan
from fightcamp import input_parsing, stage2_planning_brief
from fightcamp.exercise_coaching import easier_rejection
from fightcamp.exercise_coaching_source import coaching_identity, lookup_occurrence, validate_source_coaching
from fightcamp.main import generate_plan_sync
from fightcamp.programme_fidelity import programme_fidelity_findings, reconcile_authoritative_programme
from fightcamp.stage2_payload import STAGE2_FINALIZER_PROMPT, UNLXCK_FINAL_RENDER_CONTRACT
from fightcamp.weekly_plan_render import render_weekly_schedule_section
from stage2_luna_style import AUTHORED, luna_style_response
from support import FakeOpenAIClient

INTAKE = Path(__file__).parent / "fixtures" / "stage2_coaching" / "luna_comparison_intake.json"
VALID = [(key, label, text) for key, fields in AUTHORED.items() for label, text, ok in fields if ok]
INVALID = [(key, label, text) for key, fields in AUTHORED.items() for label, text, ok in fields if not ok]


@pytest.fixture(scope="module")
def stage1():
    intake = json.loads(INTAKE.read_text(encoding="utf8"))
    with pytest.MonkeyPatch.context() as patch:
        now = datetime(2026, 11, 2, 22, 5)
        patch.setattr(input_parsing, "_utc_now", lambda: now)
        patch.setattr(stage2_planning_brief, "_utc_now", lambda: now)
        return generate_plan_sync(PlanRequest(**intake).to_payload())


@pytest.fixture(scope="module")
def brief(stage1):
    return stage1["planning_brief"]


@pytest.fixture(scope="module")
def schedule(brief):
    return render_weekly_schedule_section(planning_brief=brief, blocks=None)


def _occurrence(text, day, name):
    """The detail lines under one exercise on one D-day of a rendered plan."""
    lines, current, inside = [], None, False
    for line in text.splitlines():
        if header := re.match(r"^### D-(\d+)", line):
            current, inside = int(header[1]), False
            continue
        if re.match(r"^\s*- ", line):
            inside = current == day and line.lstrip("- ").startswith(name)
            continue
        if inside and line.startswith("  "):
            lines.append(line.strip())
    return lines


def _programme_lines(text):
    """Week/day headers and exercise lines: the prescription coaching may not touch."""
    body = text[text.index("# Weekly Schedule"):]
    return [line for line in body.splitlines() if re.match(r"^(?:#{2,3} |- )", line)]


@pytest.mark.parametrize("variant", ["canonical", "dashfree", "retitled"])
def test_valid_luna_coaching_survives_reconciliation_at_its_own_occurrence(brief, schedule, variant):
    result = reconcile_authoritative_programme(brief, luna_style_response(schedule, brief, variant=variant))
    for (day, name), label, text in VALID:
        assert f"{label}: {text}" in _occurrence(result["text"], day, name), (variant, day, name, label)
    assert result["findings"] == []


@pytest.mark.parametrize("variant", ["dashfree", "retitled"])
def test_prescription_calendar_and_membership_are_unchanged(brief, schedule, variant):
    result = reconcile_authoritative_programme(brief, luna_style_response(schedule, brief, variant=variant))
    assert _programme_lines(result["text"]) == _programme_lines(schedule)
    assert programme_fidelity_findings(brief, result["text"]) == []


def test_unsafe_unverifiable_unauthorised_and_wrong_day_lines_are_rejected(brief, schedule):
    result = reconcile_authoritative_programme(brief, luna_style_response(schedule, brief, variant="dashfree"))
    for _, _, text in INVALID:
        assert text not in result["text"]
    reasons = {(f["field"], f["reason"]) for f in result["coaching_rejections"]
               if f["code"] == "exercise_coaching_rejected"}
    assert ("regression_options", "stop_condition_override") in reasons
    assert any(field == "progression_rule" for field, _ in reasons)
    # The scheduled exercise keeps its own rest/dose and no Progress appears.
    assert not any(line.startswith("Progress:") for line in _occurrence(result["text"], 21, "Kettlebell Swing"))


@pytest.mark.parametrize("day,name,text", [
    (21, "Bulgarian Split Squat", "Use bodyweight and hold support for balance."),
    (26, "Heavy Bag Power Intervals", "Reduce punch force while maintaining the prescribed work and rest."),
    (14, "Med-Ball Rotational Slam", "Use a lighter available medicine ball."),
    (26, "Burpee Broad Jumps", "Step back instead of jumping and land softly."),
])
def test_ordinary_regressions_are_not_second_guessed(brief, day, name, text):
    role, a = _assignment(brief, day, name)
    assert easier_rejection(text, a, role, brief["athlete_snapshot"], brief=brief) is None


@pytest.mark.parametrize("text,reason", [
    ("Push through the burn for the last round.", "stop_condition_override"),
    ("Use 45 sec work.", "increased_easier_dose"),
    ("Use 40 kg.", "unknown_easier_dose"),
])
def test_unsafe_or_over_dose_regressions_stay_rejected(brief, text, reason):
    role, a = _assignment(brief, 26, "Heavy Bag Power Intervals")
    assert easier_rejection(text, a, role, brief["athlete_snapshot"], brief=brief) == reason


@pytest.mark.parametrize("line,annotation", [
    ("Swap: Goblet Squat.", True), ("**Swap:** Goblet Squat.", True), ("**Easier**: Use bodyweight.", True),
    ("- Goblet Squat: 3 x 5", False),
])
def test_late_camp_allow_list_treats_coaching_labels_as_text(line, annotation):
    from fightcamp.stage2_validator import _late_fight_line_is_annotation_or_task
    assert _late_fight_line_is_annotation_or_task(line) is annotation


def test_bold_and_plural_labels_are_coaching_not_programme_body(brief, schedule):
    raw = luna_style_response(schedule, brief, variant="dashfree")
    raw = raw.replace("  Swap: Cossack Squat", "  **Substitutions:** Cossack Squat").replace(
        "  Purpose: Single-leg", "  **Purpose:** Single-leg")
    result = reconcile_authoritative_programme(brief, raw)
    assert "**Substitutions:** Cossack Squat at the same dose if the split stance will not settle." in result["text"]
    assert result["findings"] == [] and programme_fidelity_findings(brief, result["text"]) == []
    card = resolve_effective_structured_plan({"planning_brief": brief, "plan_text": result["text"]})
    assert _card_block(card, 21, "Bulgarian Split Squat")["substitutions"] == [
        "Cossack Squat at the same dose if the split stance will not settle."]


def test_identical_duplicate_occurrences_never_share_one_block():
    key = (21, ("strength",), ("swing",))
    assert lookup_occurrence({key: "coaching"}, key, [key, key]) is None
    assert lookup_occurrence({key: "coaching"}, key, [key]) == "coaching"


def test_saved_card_swaps_survive_as_text(brief):
    from api.structured_plan_coaching import recover_selected_coaching
    swaps = ["Sled push at the same dose.", "Any squat variation you like."]
    card = {"weeks": [{"days": [{"countdown_label": "D-5", "sessions": [{"title": "Unscheduled", "blocks": [
        {"block_type": "strength", "display_name": "Goblet Squat", "sets": 3, "reps": 6, "substitutions": swaps}]}]}]}]}
    block = recover_selected_coaching(card, brief)["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    assert block["substitutions"] == swaps and (block["sets"], block["reps"]) == (3, 6)


def test_malformed_saved_rows_do_not_crash_recovery(brief):
    from api.structured_plan_coaching import recover_selected_coaching
    card = {"weeks": [{"days": [{"countdown_label": "D-21", "sessions": ["bad", {"title": "S", "blocks": ["bad", None]}]}]}]}
    assert recover_selected_coaching(card, brief)


def test_source_cue_and_stop_win_for_every_label_form():
    from api.structured_plan_faithfulness import project_source_adjustments
    source = OPEN_SOURCE.format(sep=" — ").replace("  Cue:", "  **Coaching cues:**").replace("  Stop:", "  Stop rule:")
    plan = {"weeks": [{"week_index": 1, "days": [{"weekday": "Monday", "countdown_label": "", "sessions": [
        {"title": "Strength", "blocks": [{"block_type": "strength", "display_name": "Goblet Squat", "sets": 3,
         "reps": 6, "coaching_cues": ["Squat."], "stop_rules": ["Stop if tired."]}]}]}]}]}
    block = project_source_adjustments(plan, source)["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    assert "sit down between your hips" in block["coaching_cues"][0]
    assert any("knees cave" in rule for rule in block["stop_rules"])


def test_same_day_duplicate_exercise_is_never_guessed():
    # Two same-day sessions hold the same exercise; a retitled source session
    # cannot prove which one it means, so neither receives its coaching.
    index = {(21, ("strength",), ("swing",)): "a", (21, ("power",), ("swing",)): "b"}
    assert lookup_occurrence(index, (21, ("renamed",), ("swing",)), [(21, ("renamed",), ("swing",))]) is None
    one = {(21, ("renamed",), ("swing",)): "coaching"}
    assert lookup_occurrence(one, (21, ("strength",), ("swing",)), list(index)) is None
    assert lookup_occurrence(one, (21, ("strength",), ("swing",)), [(21, ("strength",), ("swing",))]) == "coaching"
    assert lookup_occurrence(one, (20, ("strength",), ("swing",)), [(20, ("strength",), ("swing",))]) is None
    assert coaching_identity("Kettlebell Swings") == coaching_identity("Kettlebell Swing")


def test_dash_free_headers_validate_against_the_scheduled_assignment(brief, schedule):
    text, findings = validate_source_coaching(brief, luna_style_response(schedule, brief, variant="dashfree"))
    assert "Easier: Use bodyweight and hold support for balance." in text
    assert all(f["scheduled_d_day"] is not None for f in findings)


def test_structured_cards_keep_every_approved_field(brief, schedule):
    final = reconcile_authoritative_programme(brief, luna_style_response(schedule, brief, variant="dashfree"))["text"]
    card = resolve_effective_structured_plan({"planning_brief": brief, "plan_text": final})
    block = _card_block(card, 21, "Bulgarian Split Squat")
    assert block["regression_options"] == ["Use bodyweight and hold support for balance."]
    assert block["substitutions"] == ["Cossack Squat at the same dose if the split stance will not settle."]
    assert block["coaching_cues"] == ["Keep your front foot planted and control the lowering phase."]
    assert "lower until the back knee nearly touches the floor" in block["execution_instructions"]
    assert block["purpose"] == "Single-leg strength that keeps your stance solid when you push off."
    assert block["stop_rules"] and not block.get("progression_rule")
    assert block["display_name"] == "Bulgarian Split Squat"
    swing = _card_block(card, 21, "Kettlebell Swing")
    assert not swing.get("progression_rule")
    assert _card_block(card, 14, "Barbell Thruster")["substitutions"] == [
        "Goblet Squat with a kettlebell at the same dose.", "Sled push at the same dose."]


def test_converter_cannot_drop_invent_or_rewrite_swap_and_cue(brief, schedule):
    final = reconcile_authoritative_programme(brief, luna_style_response(schedule, brief, variant="dashfree"))["text"]
    card = resolve_effective_structured_plan({"planning_brief": brief, "plan_text": final})
    block = _card_block(card, 21, "Bulgarian Split Squat")
    block["substitutions"] = []
    block["coaching_cues"] = ["Brace your trunk."]
    _card_block(card, 21, "Battle Rope Slams")["substitutions"] = ["Sled sprints instead."]
    outcome = build_structured_plan_outcome(card, raw_markdown=final, planning_brief=brief)
    assert outcome.structured_plan is not None, outcome.errors
    published = _card_block(outcome.structured_plan, 21, "Bulgarian Split Squat")
    assert published["substitutions"] == ["Cossack Squat at the same dose if the split stance will not settle."]
    assert published["coaching_cues"] == ["Keep your front foot planted and control the lowering phase."]
    assert not _card_block(outcome.structured_plan, 21, "Battle Rope Slams").get("substitutions")


def test_finalize_with_mocked_luna_keeps_coaching_and_passes_release(stage1, schedule, monkeypatch):
    monkeypatch.setenv("UNLXCK_STAGE2_STRUCTURED_PLAN", "0")
    raw = luna_style_response(schedule, stage1["planning_brief"], variant="dashfree")
    client = FakeOpenAIClient([SimpleNamespace(id="luna_style", output_text=raw,
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2))])
    final = asyncio.run(OpenAIStage2Automator(client=client, model="gpt-6-luna").finalize(stage1_result=stage1))
    text = final["final_plan_text"]
    assert "Easier: Reduce punch force while maintaining the prescribed work and rest." in text
    assert "Swap: Cossack Squat at the same dose if the split stance will not settle." in text
    assert programme_fidelity_findings(stage1["planning_brief"], text) == []
    report = final["stage2_validator_report"]
    codes = {str(item.get("code")) for item in report.get("errors") or [] if isinstance(item, dict)}
    # Only the existing "no Easier authored for this exercise" flag remains;
    # no fidelity, allow-list or rejection blocker, and release is unchanged.
    assert codes <= {"exercise_coaching_missing"}
    assert report["programme_reconciliation"]["findings"] == []
    assert report["release_decision"] == "publish_with_flags" and report["is_athlete_releasable"]


def test_prompt_states_the_coaching_job_without_word_caps():
    prompt = STAGE2_FINALIZER_PROMPT + UNLXCK_FINAL_RENDER_CONTRACT
    assert "expert S&C coach completing an approved programme" in prompt
    for removed in ("at most 9 words", "at most seven words", "Progress: none this block", "Coach call:"):
        assert removed not in prompt
    assert "concise, personalised coaching" in prompt
    assert "one specific, practical modification" in prompt
    assert "structural week and day headers" in prompt  # Header separator is not a style violation.


OPEN_SOURCE = """# Session Cards

## Monday{sep}Strength
- Goblet Squat: 3 sets x 6 reps; rest 90 sec; RPE 7
  Cue: Keep your chest tall and sit down between your hips.
  Easier: Use a lighter kettlebell.
  Stop: Stop if your knees cave.

# 4-Week Development Block
Week 2 small step.
"""


@pytest.mark.parametrize("sep", [" — ", ": "])
def test_open_plan_card_cannot_recycle_easier_as_progress(sep):
    from api.structured_plan_faithfulness import project_source_adjustments
    plan = {"weeks": [{"week_index": 1, "days": [{"weekday": "Monday", "countdown_label": "", "sessions": [
        {"title": "Strength", "blocks": [{"block_type": "strength", "display_name": "Goblet Squat", "sets": 3,
         "reps": 6, "progression_rule": "Use a lighter kettlebell.", "regression_options": [],
         "coaching_cues": ["Squat."]}]}]}]}]}
    block = project_source_adjustments(plan, OPEN_SOURCE.format(sep=sep))["weeks"][0]["days"][0]["sessions"][0]["blocks"][0]
    assert not block.get("progression_rule")  # No source Progress line: none on the card.
    assert block["regression_options"] == ["Use a lighter kettlebell."]
    assert "sit down between your hips" in block["coaching_cues"][0]


def test_open_prompt_has_no_word_caps_or_forced_progress():
    from fightcamp.stage2_payload import _OPEN_ONGOING_RENDER_MODE_INSTRUCTIONS as prompt
    from fightcamp.stage2_payload_open_ongoing import build_open_ongoing_payload
    rules = " ".join(build_open_ongoing_payload(athlete_model={})["open_plan_spec"]["render_rules"])
    for text in (prompt, rules):
        assert "none this block" not in text and "9 words" not in text
    assert "concise, personalised coaching" in prompt


def _assignment(brief, day, name):
    for week in brief["weekly_role_map"]["weeks"]:
        for role in week["session_roles"]:
            if role.get("scheduled_countdown_label") == f"D-{day}":
                for assignment in role.get("selected_exercise_assignments") or []:
                    if assignment["name"] == name:
                        return role, assignment
    raise AssertionError((day, name))


def _card_block(card, day, name):
    for week in card["weeks"]:
        for d in week["days"]:
            if d.get("countdown_label") == f"D-{day}":
                for session in d["sessions"]:
                    for block in session["blocks"]:
                        if block.get("display_name") == name:
                            return block
    raise AssertionError((day, name))
