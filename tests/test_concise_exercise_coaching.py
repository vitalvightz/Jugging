"""Recorded October 11/15 cards: concise coaching without dose or safety loss."""
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess

import pytest

from api.structured_plan_coaching import recover_selected_coaching
from api.services.effective_structured_plan import resolve_effective_structured_plan
from fightcamp.coaching_copy import concise_coaching, short_cue
from fightcamp.exercise_coaching import exercise_coaching
from fightcamp.exercise_progression import build_progression_context, occurrence_key


ROOT = Path(__file__).resolve().parents[1]
RECORDS = json.loads((ROOT / "tests/fixtures/concise_coaching/october_11_15.json").read_text())
DOSE = ("display_name", "exercise_key", "sets", "reps", "duration", "work", "rounds", "distance", "load", "rest", "effort", "intensity")


def blocks(card):
    return [b for w in card["weeks"] for d in w["days"] for s in d["sessions"] for b in s["blocks"]]


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["plan_id"][:8])
def test_saved_examples_are_idempotent_preserve_dose_and_retain_every_warning(record):
    original = deepcopy(record["card"])
    after = recover_selected_coaching(original, record["brief"])
    assert original == record["card"]
    assert recover_selected_coaching(after, record["brief"]) == after
    api = resolve_effective_structured_plan({"structured_plan": original, "planning_brief": record["brief"]})
    assert blocks(api) == blocks(after)
    for before, current in zip(blocks(original), blocks(after)):
        assert {k: current.get(k) for k in DOSE} == {k: before.get(k) for k in DOSE}
        assert all(option in before.get("regression_options", []) for option in current.get("regression_options", []))
        assert len(current.get("stop_rules", [])) <= 1
        assert len(current.get("coaching_cues", [])) <= 1
        if before.get("coaching_cues"):
            assert current["coaching_cues"][0] in {before["coaching_cues"][0], short_cue(before["coaching_cues"][0])}
        for warning in before.get("stop_rules", []):
            condition = re.sub(r"^(?:stop\s+)?(?:if|when|for)\s+", "", warning, flags=re.I).rstrip(".")
            assert condition.casefold() in " ".join(current["stop_rules"]).casefold()


def test_actual_band_punch_preserves_authored_easier_short_cue_and_all_four_stop_criteria():
    record = RECORDS[1]
    current = next(b for b in blocks(recover_selected_coaching(record["card"], record["brief"]))
                   if b["display_name"] == "Band-Resisted Punch")
    # The saved card's whole instruction survives; the stance reset is technique, not filler.
    assert current["coaching_cues"] == ["Keep punches balanced; return to guard and reset stance between exchanges"]
    assert len(current["coaching_cues"][0].split()) <= 20  # KS3 sentence cap
    source = next(b for b in blocks(record["card"]) if b["display_name"] == "Band-Resisted Punch")
    assert current["regression_options"] == source["regression_options"]
    assert current["stop_rules"] == [
        "Stop if punch speed worsens, guard return fails, balance deteriorates, or pain appears."]
    assert not current.get("progression_rule")  # actual D-13 taper card
    assert (current["sets"], current["reps"], current["rest"]["value"]) == (1, 2, 120)


def test_fast_fallback_keeps_execution_first_and_exact_dose_in_detail():
    from api.structured_plan_deterministic_fallback import _session
    record = RECORDS[1]
    role = next(r for w in record["brief"]["weekly_role_map"]["weeks"] for r in w["session_roles"]
                if any(a["name"] == "Band-Resisted Punch" for a in r["selected_exercise_assignments"]))
    session = _session(role, 13, athlete=record["brief"]["athlete_snapshot"], phase="TAPER")
    current = next(b for b in session["blocks"] if b["display_name"] == "Band-Resisted Punch")
    assert not re.match(r"\d", current["coaching_cues"][0])
    assert len(current["coaching_cues"][0].split()) <= 7
    assert not current["regression_options"]  # No AI-authored source was supplied.
    assert len(current["stop_rules"]) == 1
    assert not current.get("progression_rule")
    assert (current["sets"], current["reps"], current["rest"]["value"]) == (1, 2, 120)


@pytest.mark.parametrize("options", [
    ["Reduce reps per set to 1", "Or: Reduce reps per set to 1 instead of 2; keep rests unchanged."],
    ["Use a lighter band.", "Reduce reps per set to 1"],
    ["Do one rep per set.", "Reduce repetitions from 2 to 1."],
])
def test_authored_easier_options_remain_unchanged_without_appended_copy(options):
    actual = concise_coaching({"regression_options": options})
    assert actual["regression_options"] == options  # Never select/rewrite the AI's alternatives.
    assert concise_coaching(actual) == actual


def test_overlapping_stops_combine_without_losing_distinct_safety_action():
    assert concise_coaching({"stop_rules": ["If pain appears", "Stop if pain appears"]})["stop_rules"] == ["Stop if pain appears."]
    fields = concise_coaching({"stop_rules": ["If pain appears", "Stop if pain appears", "If balance deteriorates"]})
    assert fields["stop_rules"] == ["Stop if pain appears or balance deteriorates."]
    fields = concise_coaching({"stop_rules": ["Stop if dizzy; seek medical help for chest pain.", "If balance deteriorates"]})
    assert fields["stop_rules"] == [
        "Stop if dizzy; seek medical help for chest pain. Stop if balance deteriorates."]


def test_short_cues_do_not_cut_conditional_or_negated_safety_instructions():
    cue = "Keep your wrist neutral and do not continue if sharp pain appears."
    assert short_cue(cue) == cue  # safety is never cut


@pytest.mark.parametrize("cue", [
    "Set up like fight stance and explode on the first beat.",
    "Keep your front foot planted and control the lowering phase.",
    "Rotate through your hips and return to guard after every combination.",
])
def test_complete_cues_within_the_ks3_cap_are_kept_whole(cue):
    assert short_cue(cue) == cue
    assert short_cue(f"Cue: {cue}") == cue
    assert concise_coaching({"coaching_cues": [cue]})["coaching_cues"] == [cue]


def test_long_validated_prose_is_never_clipped_to_a_fragment():
    prose = ("Set up in your fight stance with your weight centred over both feet, explode on the first "
             "beat and recover your guard straight away on every single rep.")
    assert len(prose.split()) > 20
    assert short_cue(prose) == prose
    assert concise_coaching({"coaching_cues": [prose]})["coaching_cues"] == [prose]


def test_known_bank_cues_still_use_their_authored_compact_wording():
    from fightcamp.coaching_copy import _COMPACT_COPY
    long_form, compact = next(iter(_COMPACT_COPY.items()))
    assert short_cue(long_form) == compact


def test_labelled_and_unlabelled_versions_of_one_cue_do_not_multiply():
    fields = concise_coaching({"coaching_cues": ["Cue: Keep the elbow tucked.", "Keep the elbow tucked."]})
    assert fields["coaching_cues"] == ["Keep the elbow tucked."]
    assert concise_coaching(fields) == fields


def test_standard_movement_profiles_keep_cues_and_stop_rules_concise():
    from fightcamp.exercise_coaching import _PROFILES
    for _, (cue, easier, _, stop) in _PROFILES:
        fields = concise_coaching({"coaching_cues": [cue, cue],
                                   "regression_options": [easier], "stop_rules": [stop]})
        for key in ("coaching_cues", "stop_rules"):
            assert len(fields[key]) == 1
            assert len(fields[key][0].split()) <= 9, fields[key]
        assert concise_coaching(fields) == fields


def test_secondary_and_embedded_safety_cues_survive_single_cue_selection():
    fields = concise_coaching({"coaching_cues": [
        "Brace first. Stop if dizzy.", "Stop for sharp pain.", "Brace first."]})
    assert fields["coaching_cues"] == ["Brace first"]
    assert "dizzy" in " ".join(fields["stop_rules"])
    assert "sharp pain" in " ".join(fields["stop_rules"])
    assert concise_coaching(fields) == fields


def test_coach_fields_win_over_fallback_and_keep_full_technical_detail():
    assignment = {"name": "Band-Resisted Punch", "slot_group": "strength_slots",
                  "effective_prescription": "1 x 2 reps; rest 120 sec",
                  "coaching_cues": ["Keep the elbow below shoulder height; return to guard before resetting."],
                  "regression_options": ["Use the yellow rehabilitation band."],
                  "stop_rules": ["Stop for shoulder pain."]}
    actual = exercise_coaching(assignment, {}, phase="TAPER", d_day=13)
    # The coach's full technical instruction wins over the fallback, unshortened.
    assert actual["coaching_cues"] == assignment["coaching_cues"]
    assert actual["regression_options"] == assignment["regression_options"]
    assert actual["stop_rules"] == assignment["stop_rules"]
    assert not actual.get("progression_rule")


def test_october_11_style_progress_requires_an_actual_authorised_future_exposure():
    # The recorded Jump Squat has no approved next exposure. Its old
    # numerical promise must not be resurrected merely because it reads well.
    record = RECORDS[0]
    after = recover_selected_coaching(record["card"], record["brief"])
    assert not next(b for b in blocks(after) if b["display_name"] == "Jump Squat").get("progression_rule")
    # Controlled future-authority extension using that actual selected movement.
    role = next(r for w in record["brief"]["weekly_role_map"]["weeks"] for r in w["session_roles"]
                if any(a["name"] == "Jump Squat" for a in r["selected_exercise_assignments"]))
    role = deepcopy(role)
    assignment = next(a for a in role["selected_exercise_assignments"] if a["name"] == "Jump Squat")
    role["selected_exercise_assignments"] = [assignment]
    role.pop("effective_strength_prescriptions", None)
    role.pop("strength_composition_policy", None)  # healthy controlled fixture, not a production change
    role["scheduled_countdown_label"] = "D-18"
    assignment["effective_prescription"] = "4 x 3 reps at max speed; rest 90 sec"
    future = deepcopy(role)
    future["scheduled_countdown_label"] = "D-15"
    future["selected_exercise_assignments"][0]["effective_prescription"] = "4 x 4 reps at max speed; rest 90 sec"
    brief = {"weekly_role_map": {"weeks": [{"phase": "SPP", "session_roles": [role, future]}]}}
    context = build_progression_context(brief)[occurrence_key(role, assignment, 18)]
    assert not exercise_coaching(assignment, role, phase="SPP", d_day=18, progression_context=context).get("progression_rule")
    assignment["progression_rule"] = "Next time add one rep per set (4 x 4)."
    current = exercise_coaching(assignment, role, phase="SPP", d_day=18, progression_context=context)
    assert current["progression_rule"] == assignment["progression_rule"]
    # The saved coach wording survives unchanged when this future dose supports it.
    from api.structured_plan_deterministic_fallback import _session
    matched_card = {"weeks": [{"days": [{"countdown_label": "D-18", "sessions": [_session(role, 18)]}]}]}
    blocks(matched_card)[0]["progression_rule"] = assignment["progression_rule"]
    recovered = recover_selected_coaching(matched_card, brief)
    jump = next(b for b in blocks(recovered) if b["display_name"] == "Jump Squat")
    assert jump["progression_rule"] == "Next time add one rep per set (4 x 4)."
    assert recover_selected_coaching(recovered, brief) == recovered
    future["scheduled_countdown_label"] = "D-14"
    context = build_progression_context(brief)[occurrence_key(role, assignment, 18)]
    assert not exercise_coaching(assignment, role, d_day=18, progression_context=context).get("progression_rule")


@pytest.mark.skipif(not (ROOT / "web/node_modules/tsx").is_dir(), reason="Requires web npm ci")
@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["plan_id"][:8])
def test_actual_saved_instructions_survive_expanded_react_cards(record):
    card = recover_selected_coaching(record["card"], record["brief"])
    expected = [{"id": b["block_id"], "fields": {k: b[k] for k in (
        "coaching_cues", "regression_options", "progression_rule", "stop_rules") if k in b}} for b in blocks(card)]
    result = subprocess.run(["node", "--import", "tsx", "--import", "./test/register-css-stub.mjs",
        "test/assert-exercise-coaching-release.tsx"], cwd=ROOT / "web",
        input=json.dumps({"plan": card, "expected": expected}), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_dose_first_legacy_coaching_does_not_displace_execution():
    fields = concise_coaching({"coaching_cues": [
        "5s work, 60s rest x 8 rounds", "Cue: Return to guard.", "Stop for pain."]})
    assert fields["coaching_cues"] == ["Return to guard."]
    assert "pain" in " ".join(fields["stop_rules"])
