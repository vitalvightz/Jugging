"""AI authoring boundary: source -> conversion -> persistence -> actual React."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

from api.structured_plan_coaching import recover_selected_coaching
from api.structured_plan_deterministic_fallback import _session
from api.structured_plan_faithfulness import _source_day_section_lines
from api.structured_plan_generation import build_structured_plan_outcome
from api.services.effective_structured_plan import resolve_effective_structured_plan
from fightcamp.exercise_coaching import easier_rejection, exercise_coaching
from fightcamp.exercise_coaching_source import validate_source_coaching
from fightcamp.exercise_progression import build_progression_context, occurrence_key, progress_rejection
from fightcamp.programme_fidelity import reconcile_authoritative_programme
from test_measurable_exercise_coaching import programme, card_for, all_blocks, DOSE
from test_programme_fidelity_production import _persist_final, FIXTURES

ROOT = Path(__file__).resolve().parents[1]
PROGRESS = "Next D-28: add 1 rep per set if controlled and recovered."
EASIER = "Use a shallower pain-free squat; keep the same rest."


def source_for(brief, progress=PROGRESS, easier=EASIER):
    lines = ["# Fight Camp Plan"]
    for i, week in enumerate(brief["weekly_role_map"]["weeks"], 1):
        lines.append(f"## Week {i} — GPP")
        for role in week["session_roles"]:
            day = int(role["scheduled_countdown_label"][2:])
            a = role["selected_exercise_assignments"][0]
            lines.extend([f"### D-{day} (Monday) — {role['athlete_facing_label']}",
                          f"- {a['name']} — {a['effective_prescription']}",
                          "  Cue: Brace before each squat."])
            if easier:
                lines.append(f"  Easier: {easier}")
            if progress and day == 35:
                lines.append(f"  Progress: {progress}")
            lines.append("  Stop: Stop for knee pain or lost trunk control.")
    return "\n".join(lines)


def projected_card(brief, text):
    card = card_for(brief)
    days = _source_day_section_lines(text)
    for w, source_week in zip(card["weeks"], brief["weekly_role_map"]["weeks"], strict=True):
        for d, role in zip(w["days"], source_week["session_roles"], strict=True):
            day = int(d["countdown_label"][2:])
            d["sessions"] = [_session(role, day, days[day])]
    return card


def save(brief, text, card):
    intake = json.loads((FIXTURES / "affected_intake.json").read_text())
    return _persist_final(intake, {"status": "ready", "planning_brief": brief,
        "plan_text": text, "final_plan_text": text, "structured_plan": card,
        "stage2_validator_report": {"errors": [], "warnings": []}})


@pytest.mark.parametrize("bad", [
    "Use more weight while keeping movement control at the same dose.",
    "Use a higher RPE while keeping control at the same dose.",
    "Add 2 reps per set.", "Add 100 kg next time.", "Add two kilograms next time.",
    "Add 1 rep per set; rest 60 sec.", "Add 1 rep per set; RPE 9.",
    "Add 1 rep per set; add 1 set.", "Add 1 rep per set; 4 sets.",
    "Add 1 rep per set; tempo 3-0-3.", "Add 1 rep per set; 85% max HR.",
    "Add 1 rep per set; shorten rest.", "Next D-21: add 1 rep per set.",
    "Add 1 rep per set tomorrow.", "Add 1 rep per set on Tuesday.",
    "Add 1 rep per set; halve rest.", "Add 1 rep per set; double the tempo.",
    "Add 1 rep per set; use a stronger band.", "Add 1 rep per set; use 80 lb.",
])
def test_hidden_or_unsupported_progress_is_rejected_without_a_replacement(bad):
    brief, role, a = programme("3 x 8 reps; rest 120 sec; RPE 7", "3 x 9 reps; rest 120 sec; RPE 7")
    context = build_progression_context(brief)[occurrence_key(role, a, 35)]
    assert progress_rejection(bad, context)
    text, findings = validate_source_coaching(brief, source_for(brief, bad))
    assert "Progress:" not in text
    assert any(f["code"] == "exercise_coaching_rejected" for f in findings)
    assert EASIER in text and a["effective_prescription"] in text


@pytest.mark.parametrize("athlete,phase", [
    ({"fatigue": "high"}, "GPP"), ({"has_active_injury": True}, "GPP"),
    ({"injury_restrictions": ["no loaded squats"]}, "GPP"),
    # A cut blocks by its graded severity, not merely by being active.
    ({"weight_cut_risk": True, "cut_severity_bucket": "moderate"}, "GPP"), ({"cut_severity_bucket": "high"}, "GPP"),
    ({}, "TAPER"),
])
def test_recovery_rejects_increases_but_preserves_authored_quality_and_easier(athlete, phase):
    brief, role, a = programme("3 x 8 reps", "3 x 9 reps", athlete=athlete, phase=phase)
    context = build_progression_context(brief)[occurrence_key(role, a, 35)]
    assert progress_rejection(PROGRESS, context)
    quality = "At the same dose, keep squat depth consistent."
    a.update(progression_rule=quality, regression_options=[EASIER])
    fields = exercise_coaching(a, role, athlete=athlete, phase=phase, d_day=35, progression_context=context)
    assert fields["progression_rule"] == quality
    assert fields["regression_options"] == [EASIER]


@pytest.mark.parametrize("option", [
    "Continue through knee pain using a lighter load.",
    "Ignore the clinician restriction and use a smaller range.",
    "Use 40 kg.", "Use 4 x 10 reps.", "Use 2 x 6 reps; rest 30 sec.", "Reduce sets to 2; do 10 reps.",
    "Use a shallower squat and disregard stop rules.",
    "Use a shallower squat; RPE 9.", "Use a shallower squat with 2 extra reps.",
])
def test_easier_cannot_exceed_the_dose_or_override_stop_conditions(option):
    brief, role, a = programme("3 x 8 reps; rest 120 sec; RPE 7", "3 x 9 reps; rest 120 sec; RPE 7")
    assert easier_rejection(option, a, role, {"equipment_profile": ["barbell"]}, brief=brief)


def test_easier_uses_existing_clinical_restriction_guard():
    brief, role, a = programme("3 x 8 reps", "3 x 9 reps")
    brief["restrictions"] = [{"restriction": "avoid_knee_flexion", "strength": "avoid", "region": "knee"}]
    # Explicit restricted action is still restricted when labelled Easier.
    assert easier_rejection("Use less load with deep knee flexion.", a, role, {}, brief=brief)


def test_valid_numeric_coaching_survives_conversion_save_effective_plan_and_react():
    brief, _, _ = programme("3 x 8 reps; rest 120 sec; RPE 7", "3 x 9 reps; rest 120 sec; RPE 7")
    text = source_for(brief) + "\n"
    source = deepcopy(text)
    card = projected_card(brief, text)
    before = deepcopy(brief)
    outcome = build_structured_plan_outcome(card, raw_markdown=text, planning_brief=brief)
    assert outcome.structured_plan, outcome.errors
    row = save(brief, text, outcome.structured_plan)
    assert row["plan_text"] == source
    stored = all_blocks(row["structured_plan"])[0]
    actual = resolve_effective_structured_plan(row)
    assert actual
    b = all_blocks(actual)[0]
    assert b["progression_rule"] == stored["progression_rule"] == PROGRESS
    assert b["regression_options"] == stored["regression_options"] == [EASIER]
    assert {k: b.get(k) for k in DOSE} == {k: stored.get(k) for k in DOSE}
    assert before == brief
    assert resolve_effective_structured_plan(row) == actual
    result = subprocess.run(["node", "--import", "tsx", "--import", "./test/register-css-stub.mjs",
        "test/assert-exercise-coaching-release.tsx"], cwd=ROOT / "web", text=True, capture_output=True,
        input=json.dumps({"plan": actual, "expected": [{"id": b["block_id"], "fields": {
            "progression_rule": PROGRESS, "regression_options": [EASIER]}}]}))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("field", ["progression_rule", "regression_options"])
@pytest.mark.parametrize("mutation", ["drop", "invent", "rewrite"])
def test_conversion_cannot_drop_invent_or_rewrite_ai_adjustments(field, mutation):
    brief, _, _ = programme("3 x 8 reps; rest 120 sec; RPE 7", "3 x 9 reps; rest 120 sec; RPE 7")
    text = source_for(brief, progress=None if mutation == "invent" else PROGRESS,
                      easier=None if mutation == "invent" else EASIER)
    card = projected_card(brief, text)
    block = all_blocks(card)[0]
    if mutation == "drop":
        block.pop(field, None)
    else:
        block[field] = "Add 100 kg next time." if field == "progression_rule" else ["Use a bike instead."]
    outcome = build_structured_plan_outcome(card, raw_markdown=text, planning_brief=brief)
    # The converted card still ships, but the field is the source's, verbatim:
    # a drop or rewrite is restored and an invention is removed.
    assert outcome.structured_plan is not None, outcome.errors
    published = all_blocks(outcome.structured_plan)[0]
    expected = None if mutation == "invent" else (PROGRESS if field == "progression_rule" else [EASIER])
    assert (published.get(field) or None) == expected
    assert "100 kg" not in json.dumps(outcome.structured_plan)
    assert "bike" not in json.dumps(outcome.structured_plan)


def test_written_out_none_progress_is_absent_and_keeps_the_converted_card():
    brief, _, _ = programme("3 x 8 reps; rest 120 sec; RPE 7", "3 x 8 reps; rest 120 sec; RPE 7")
    text = source_for(brief, progress="none this block.")
    card = projected_card(brief, text)
    all_blocks(card)[0]["progression_rule"] = "None this block"
    outcome = build_structured_plan_outcome(card, raw_markdown=text, planning_brief=brief)
    assert outcome.structured_plan is not None, outcome.errors
    assert not all_blocks(outcome.structured_plan)[0].get("progression_rule")


def test_missing_easier_uses_source_fallback_without_manufacturing_coaching():
    brief, _, _ = programme("3 x 8 reps", "3 x 9 reps")
    text, findings = validate_source_coaching(brief, source_for(brief, easier=None))
    assert any(f["code"] == "exercise_coaching_missing" for f in findings)
    card = projected_card(brief, text)
    row = save(brief, text, card)
    assert PROGRESS in row["plan_text"]
    assert "Easier:" not in row["plan_text"]
    assert not all_blocks(resolve_effective_structured_plan(row))[0].get("regression_options")


def test_unsafe_raw_text_and_card_never_survive_persistence_release_override():
    brief, _, _ = programme("3 x 8 reps", "3 x 8 reps")
    text = source_for(brief, "Add 100 kg next time.")
    row = save(brief, text, projected_card(brief, text))
    assert "100 kg" not in row["plan_text"]
    assert "100 kg" not in json.dumps(row["structured_plan"])
    assert not all_blocks(resolve_effective_structured_plan(row))[0].get("progression_rule")


def test_saved_metadata_restoration_does_not_change_athlete_dose():
    brief, _, _ = programme("3 x 8 reps", "3 x 9 reps")
    text = source_for(brief)
    card = projected_card(brief, text)
    block = all_blocks(card)[0]
    block["sets"] = 2
    restored = recover_selected_coaching(card, brief, source_text=text)
    current = all_blocks(restored)[0]
    assert current["sets"] == 2 and current["reps"] == 8
    assert not current.get("progression_rule")
    assert current["regression_options"] == [EASIER]


def test_reconciliation_preserves_only_exact_session_exercise_coaching():
    brief, _, _ = programme("3 x 8 reps", "3 x 9 reps")
    # A real deterministic calendar enables the schedule reconciliation path.
    for i, week in enumerate(brief["weekly_role_map"]["weeks"]):
        day = 35 - 7 * i
        week.update(week_index=i + 1, calendar_days=[{"d_day": day, "weekday": "monday", "date": f"2026-11-{i+1:02}"}])
        week["session_roles"][0]["scheduled_day_hint"] = "monday"
    text = source_for(brief)
    result = reconcile_authoritative_programme(brief, text)
    assert PROGRESS in result["text"] and EASIER in result["text"]
    assert not result["findings"]
    drift = text.replace("D-35 (Monday)", "D-34 (Monday)")
    corrected = reconcile_authoritative_programme(brief, drift)["text"]
    assert PROGRESS not in corrected


def test_unmatched_historical_card_cannot_license_unverifiable_numeric_progress():
    card = {"weeks": [{"days": [{"sessions": [{"blocks": [{
        "block_type": "strength", "display_name": "Back Squat", "sets": 3, "reps": 8,
        "load": "unknown starting resistance", "coaching_cues": ["Progress: Add 100 kg next time.", "Brace before each rep."],
        "regression_options": ["Use 40 kg."]}]}]}]}]}
    before = deepcopy(card)
    actual = all_blocks(recover_selected_coaching(card, {}))[0]
    assert not actual.get("progression_rule")
    assert not actual["regression_options"]
    assert actual["coaching_cues"] == ["Brace before each rep."]
    assert card == before


def test_duplicate_sessions_keep_distinct_coaching_through_save_and_react():
    from test_exercise_coaching_context import duplicate_card, sessions
    from fightcamp.render_authority import authoritative_render_for_role
    brief, card = duplicate_card()
    roles = brief["weekly_role_map"]["weeks"][0]["session_roles"]
    raw = []
    for role in roles:
        raw.append(f"### D-32 (Tuesday) — {role['athlete_facing_label']}")
        raw.extend(authoritative_render_for_role(role, d_day=32)["body_lines"])
    text = "\n".join(raw)
    for i, role in enumerate(roles):
        sessions(card)[i] = _session(role, 32, raw)
    assert len({b["block_id"] for b in all_blocks(card)}) == 2
    outcome = build_structured_plan_outcome(card, raw_markdown=text, planning_brief=brief)
    assert outcome.structured_plan, outcome.errors
    row = save(brief, text, outcome.structured_plan)
    actual = resolve_effective_structured_plan(row)
    assert actual
    expected = []
    for session in sessions(actual):
        b = session["blocks"][0]
        index = int(session["title"][-1])
        assert b["sets"] == index and b["reps"] == 2
        prefix = "Session A" if index == 1 else "Session B"
        assert b["regression_options"][0].startswith(prefix)
        assert b["progression_rule"].startswith(prefix)
        other = next(other["blocks"][0] for other in sessions(actual) if other is not session)
        expected.append({"id": b["block_id"], "fields": {k: b[k] for k in (
            "coaching_cues", "progression_rule", "regression_options", "stop_rules")},
            "forbidden": other["coaching_cues"]})
    assert resolve_effective_structured_plan(row) == actual
    result = subprocess.run(["node", "--import", "tsx", "--import", "./test/register-css-stub.mjs",
        "test/assert-exercise-coaching-release.tsx"], cwd=ROOT / "web", text=True, capture_output=True,
        input=json.dumps({"plan": actual, "expected": expected}))
    assert result.returncode == 0, result.stderr


def test_direct_store_create_stage2_update_and_backfill_share_coaching_validation():
    from unittest.mock import MagicMock
    from api.store import SupabaseAppStore
    from test_stage2_structured_persistence import _capture_create_plan
    brief, _, _ = programme("3 x 8 reps", "3 x 8 reps")
    text = source_for(brief, "Add 100 kg next time.")
    card = projected_card(brief, text)
    result = {"status": "ready", "planning_brief": brief, "plan_text": text,
              "final_plan_text": text, "structured_plan": card}
    created = _capture_create_plan(result)
    assert "100 kg" not in created["plan_text"]
    assert "100 kg" not in json.dumps(created["structured_plan"])
    store = SupabaseAppStore(client=MagicMock(), admin_emails=set())
    updated = store._build_plan_stage2_payload({"id": "plan", "status": "ready",
        "planning_brief": json.dumps(brief)}, {k: v for k, v in result.items() if k != "planning_brief"})
    assert "100 kg" not in updated["plan_text"]
    assert "100 kg" not in json.dumps(updated["structured_plan"])
    store.get_plan = MagicMock(return_value={"id": "plan", **result})
    store.update_plan_structured_artifacts("plan", structured_plan=card, schema_version="1.0",
        stage2_validator_report={}, expected_final_plan_text=text)
    saved = store.client.table.return_value.update.call_args.args[0]
    assert "100 kg" not in json.dumps(saved["structured_plan"])
    assert saved["stage2_validator_report"]["exercise_coaching"]


def test_historical_raw_api_fallback_cannot_display_unsupported_progress():
    from api.plan_mappers import _map_plan_detail
    from test_structured_plan_models import _valid_plan
    brief, _, _ = programme("3 x 8 reps", "3 x 8 reps")
    text = source_for(brief, "Add 100 kg next time.")
    row = {"id": "plan", "athlete_id": "athlete", "status": "ready", "plan_text": text,
           "final_plan_text": text, "planning_brief": brief, "structured_plan": _valid_plan(),
           "created_at": "2026-10-20T00:00:00Z"}
    detail = _map_plan_detail(row, include_admin=False)
    assert "100 kg" not in detail.outputs.plan_text
    assert row["plan_text"] == text


def test_quality_progress_cannot_override_a_clinical_movement_restriction():
    brief, _, _ = programme("3 x 8 reps", "3 x 8 reps")
    brief["restrictions"] = [{"restriction": "avoid_knee_flexion", "strength": "avoid", "region": "knee"}]
    text, findings = validate_source_coaching(brief, source_for(brief,
        "At the same dose, refine control with deep knee flexion."))
    assert "Progress:" not in text
    assert any(f.get("reason") == "clinical_restriction" for f in findings)


@pytest.mark.parametrize("label", ["**Progress:**", "**Progress**:"])
def test_markdown_label_formatting_cannot_bypass_progress_validation(label):
    brief, _, _ = programme("3 x 8 reps", "3 x 8 reps")
    text = source_for(brief, "Add 100 kg next time.").replace("Progress:", label)
    actual, findings = validate_source_coaching(brief, text)
    assert "100 kg" not in actual
    assert any(f["code"] == "exercise_coaching_rejected" for f in findings)


@pytest.mark.parametrize("overrides", [{"phase": "TAPER"}, {"athlete": {"fatigue": "high"}}])
def test_explicit_recovery_constraints_override_a_less_restricted_exposure_context(overrides):
    brief, role, a = programme("3 x 8 reps", "3 x 9 reps")
    context = build_progression_context(brief)[occurrence_key(role, a, 35)]
    a["progression_rule"] = PROGRESS
    assert not exercise_coaching(a, role, d_day=35, progression_context=context, **overrides).get("progression_rule")
    a["progression_rule"] = "At the same dose, refine squat control."
    assert exercise_coaching(a, role, d_day=35, progression_context=context, **overrides)["progression_rule"] == a["progression_rule"]


def test_source_free_fallback_cannot_promote_bank_adjustments_into_final_ai_fields():
    brief, role, a = programme("3 x 8 reps", "3 x 9 reps")
    a.update(progression_rule=PROGRESS, regression_options=[EASIER])
    context = build_progression_context(brief)
    block = _session(role, 35, progression_context=context)["blocks"][0]
    assert not block.get("progression_rule") and not block.get("regression_options")
    source = source_for(brief)
    block = _session(role, 35, _source_day_section_lines(source)[35])["blocks"][0]
    assert block["progression_rule"] == PROGRESS and block["regression_options"] == [EASIER]


def test_hosted_microdose_preserves_its_own_ai_adjustments_only_from_source():
    _, role, _ = programme("3 x 8 reps", "3 x 9 reps")
    role["priority_microdose"] = {"name": "Staggered Stance Hold", "goal": "strength",
        "prescription": "1 x 10 sec", "coaching_notes": "Easier: Bank-only alternative."}
    raw = ["### D-35 (Monday) — Strength", "- Back Squat — 3 x 8 reps",
        "- Staggered Stance Hold — 1 x 10 sec",
        "  Easier: Use a comfortable stance width.",
        "  Progress: At the same hold dose, refine balance.", "  Stop: Stop for pain."]
    block = _session(role, 35, raw)["blocks"][0]
    assert block["regression_options"] == ["Use a comfortable stance width."]
    assert block["progression_rule"] == "At the same hold dose, refine balance."
    source_free = _session(role, 35)["blocks"][0]
    assert not source_free.get("regression_options") and not source_free.get("progression_rule")
