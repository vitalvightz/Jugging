"""One numerical step must be grounded in this exercise's next exposure."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from time import perf_counter

import pytest

from api.structured_plan_coaching import recover_selected_coaching
from api.services.effective_structured_plan import resolve_effective_structured_plan
from fightcamp.exercise_coaching import exercise_coaching
from fightcamp.exercise_progression import build_progression_context, occurrence_key


ROOT = Path(__file__).resolve().parents[1]
DOSE = ("sets", "reps", "rounds", "work", "duration", "distance", "load", "rest", "effort")


def programme(current, future, *, name="Back Squat", athlete=None, phase="GPP", next_day=28):
    a = {"name": name, "exercise_key": name.lower().replace(" ", "-"), "slot_id": "selected",
         "slot_group": "strength_slots", "source_phase": phase, "effective_prescription": current}
    role = {"role_key": "primary_strength_day", "session_index": 1, "category": "strength",
            "athlete_facing_label": "Strength", "scheduled_countdown_label": "D-35",
            "selected_exercise_assignments": [a]}
    next_role = deepcopy(role)
    next_role["scheduled_countdown_label"] = f"D-{next_day}"
    next_role["selected_exercise_assignments"][0]["effective_prescription"] = future
    brief = {"athlete_snapshot": athlete or {}, "weekly_role_map": {"weeks": [
        {"week_index": 1, "phase": phase, "session_roles": [role]},
        {"week_index": 2, "phase": phase, "session_roles": [next_role]},
    ]}}
    return brief, role, a


def coaching(brief, role, assignment, day=35):
    context = build_progression_context(brief)
    return exercise_coaching(assignment, role, phase=assignment["source_phase"], d_day=day,
                             athlete=brief["athlete_snapshot"],
                             progression_context=context.get(occurrence_key(role, assignment, day)))


@pytest.mark.parametrize("current,future,target", [
    ("3 x 8 reps; rest 120 sec; RPE 7", "3 x 9 reps; rest 120 sec; RPE 7", "add 1 rep/set"),
    ("3 x 8 reps; rest 120 sec; RPE 7", "4 x 8 reps; rest 120 sec; RPE 7", "add 1 set"),
    ("20 min continuous; RPE 4", "25 min continuous; RPE 4", "add 5 min"),
    ("3 carries over 20 m; rest 90s", "3 carries over 30 m; rest 90s", "add 10 m"),
    ("5s work, 60s rest x 8 rounds", "5s work, 60s rest x 9 rounds", "add 1 round"),
    ("3 x 8 reps @ 50 kg; rest 120 sec; RPE 7", "3 x 8 reps @ 52 kg; rest 120 sec; RPE 7",
     "add 2 kg"),
    ("3 x 30 sec; rest 90 sec", "3 x 40 sec; rest 90 sec", "add 10 sec"),
])
def test_exact_future_dose_supports_one_measurable_variable(current, future, target):
    brief, role, a = programme(current, future)
    before = deepcopy(brief)
    assert not coaching(brief, role, a).get("progression_rule")
    a["progression_rule"] = f"Next D-28: {target} if controlled and recovered."
    before = deepcopy(brief)
    result = coaching(brief, role, a)
    assert result["progression_rule"] == a["progression_rule"]
    assert target in result["progression_rule"]
    assert len(result["progression_rule"].split()) <= 9
    assert "if controlled and recovered" in result["progression_rule"]
    assert "D-28" in result["progression_rule"]
    assert brief == before


@pytest.mark.parametrize("current,future", [
    ("3 x 8 reps; rest 120 sec", "4 x 9 reps; rest 120 sec"),  # Two growth variables.
    ("3 x 8 reps; rest 120 sec", "3 x 9 reps; rest 60 sec"),  # Rest sacrificed.
    ("3 x 8 reps; rest 120 sec; RPE 6", "3 x 9 reps; rest 120 sec; RPE 8"),
    ("3 x 8 reps; rest 120 sec", "3 x 8 reps @ 40 kg; rest 120 sec"),  # Unknown starting load.
    ("3 x 8 reps @ 50 kg", "3 x 8 reps @ 80 kg"),  # Unsafe load jump.
    ("3 x 8 reps @ 50 kg", "3 x 8 reps @ 52 lb"),  # Unit switch.
    ("3 x 8 reps; rest 120 sec", "3 x 6 reps; rest 120 sec"),  # Reduced dose.
    ("3 x 8 reps; tempo 3-1-3", "3 x 9 reps; tempo 3-0-3"),  # Hidden second variable.
    ("3 x 8 reps; 80% max HR", "3 x 9 reps; 85% max HR"),  # Hidden effort increase.
])
def test_unsupported_or_unsafe_future_step_is_not_offered(current, future):
    brief, role, a = programme(current, future)
    a["progression_rule"] = "Add 1 rep per set."
    assert not coaching(brief, role, a).get("progression_rule")


@pytest.mark.parametrize("athlete", [
    {"fatigue": "high"}, {"readiness_flags": ["high_fatigue"]}, {"injuries": ["knee"]},
    {"injury_restrictions": ["no loaded squats"]}, {"cut_severity_bucket": "moderate"},
    {"readiness_flags": ["aggressive_weight_cut"]},
])
def test_recovery_and_injury_guards_override_numeric_authority(athlete):
    brief, role, a = programme("3 x 8 reps; rest 120 sec", "3 x 9 reps; rest 120 sec", athlete=athlete)
    a["progression_rule"] = "Add 1 rep per set."
    assert not coaching(brief, role, a).get("progression_rule")


def test_a_low_severity_cut_alone_does_not_withhold_supported_progression():
    """An active but routine cut weeks out is not a recovery restriction."""
    brief, role, a = programme("3 x 8 reps; rest 120 sec", "3 x 9 reps; rest 120 sec",
                               athlete={"weight_cut_risk": True, "cut_severity_bucket": "low",
                                        "readiness_flags": ["active_weight_cut"]})
    a["progression_rule"] = "Add 1 rep per set."
    assert coaching(brief, role, a).get("progression_rule") == "Add 1 rep per set."


@pytest.mark.parametrize("phase,next_day", [("TAPER", 28), ("SPP", 14), ("SPP", 13), ("SPP", 6)])
def test_final_fortnight_and_taper_do_not_offer_advancement(phase, next_day):
    brief, role, a = programme("3 x 8 reps", "3 x 9 reps", phase=phase, next_day=next_day)
    a["progression_rule"] = "Add 1 rep per set."
    assert not coaching(brief, role, a).get("progression_rule")


def test_flat_next_exposure_rejects_an_authored_increase():
    brief, role, a = programme("3 x 8 reps; rest 120 sec", "3 x 8 reps; rest 120 sec")
    a["progression_rule"] = "Add 2 reps per set."
    result = coaching(brief, role, a)
    assert not result.get("progression_rule")
    assert len(result["coaching_cues"]) == 1
    assert not any("Add 2 reps" in cue for cue in result["coaching_cues"])


def test_exact_authored_step_is_preserved_and_mismatched_step_is_rejected():
    brief, role, a = programme("3 x 8 reps; rest 120 sec", "3 x 9 reps; rest 120 sec")
    a.update(progression_rule="Add 1 rep per set.", regression_options=["Use 2 x 6 reps; rest 120 sec."],
             stop_rules=["Stop for knee pain."])
    result = coaching(brief, role, a)
    assert result["progression_rule"] == a["progression_rule"]
    assert result["regression_options"] == a["regression_options"]
    assert result["stop_rules"] == a["stop_rules"]
    a["progression_rule"] = "Add 2 reps per set."
    result = coaching(brief, role, a)
    assert not result.get("progression_rule")


@pytest.mark.parametrize("rule", ["Add 1 rep per set; rest 60 sec.", "Add 1 rep per set; RPE 9.",
                                  "Add 1 rep per set @ 80 kg."])
def test_matching_bank_increment_cannot_hide_an_unsafe_second_change(rule):
    brief, role, a = programme("3 x 8 reps; rest 120 sec; RPE 7", "3 x 9 reps; rest 120 sec; RPE 7")
    a["progression_rule"] = rule
    result = coaching(brief, role, a)
    assert not result.get("progression_rule")


def test_unknown_load_never_gets_an_invented_kg_step():
    brief, role, a = programme("3 x 8 reps @ RPE 7", "3 x 8 reps @ RPE 7")
    a["progression_rule"] = "Add 2.5 kg when all reps are complete."
    result = coaching(brief, role, a)
    assert not result.get("progression_rule")
    assert "kg" not in str(result)
    a["progression_rule"] = "Add two kilograms next time."
    assert not coaching(brief, role, a).get("progression_rule")
    assert not any("Add two" in cue for cue in coaching(brief, role, a)["coaching_cues"])


def test_same_day_duplicate_programmes_keep_their_own_future_targets():
    brief, role, a = programme("3 x 8 reps; rest 120 sec", "3 x 9 reps; rest 120 sec")
    a["progression_rule"] = "Next D-28: add 1 rep/set if controlled and recovered."
    for week in brief["weekly_role_map"]["weeks"]:
        other = deepcopy(week["session_roles"][0])
        other["session_index"] = 2
        other["athlete_facing_label"] = "Second strength appointment"
        other["selected_exercise_assignments"][0]["effective_prescription"] = "2 x 6 reps; rest 120 sec"
        other["selected_exercise_assignments"][0].pop("progression_rule", None)
        week["session_roles"].insert(0, other)
    assert "add 1 rep/set" in coaching(brief, role, a)["progression_rule"]
    other = brief["weekly_role_map"]["weeks"][0]["session_roles"][0]
    assert not coaching(brief, other, other["selected_exercise_assignments"][0]).get("progression_rule")


def test_unique_exposure_can_be_reindexed_without_losing_its_next_dose():
    brief, role, a = programme("3 x 8 reps; rest 120 sec", "3 x 9 reps; rest 120 sec")
    a["progression_rule"] = "Next D-28: add 1 rep/set if controlled and recovered."
    brief["weekly_role_map"]["weeks"][1]["session_roles"][0]["session_index"] = 5
    assert "add 1 rep/set" in coaching(brief, role, a)["progression_rule"]


def test_equivalent_unit_and_spacing_formats_keep_supported_progression():
    brief, role, a = programme("3 sets x 8 reps; rest 2 minutes", "3x9 reps; rest 120 sec")
    a["progression_rule"] = "Next D-28: add 1 rep/set if controlled and recovered."
    assert coaching(brief, role, a)["progression_rule"] == a["progression_rule"]


def test_unknown_or_different_current_card_dose_cannot_authorise_an_advance():
    brief, role, a = programme("3 x 8 reps; rest 120 sec", "3 x 9 reps; rest 120 sec")
    card = card_for(brief)
    all_blocks(card)[0]["reps"] = 7
    recovered = recover_selected_coaching(card, brief)
    assert all_blocks(recovered)[0]["reps"] == 7
    assert not all_blocks(recovered)[0].get("progression_rule")


def test_numerical_easier_reduces_only_one_current_work_quantity():
    brief, role, a = programme("3 x 8 reps; rest 120 sec; RPE 7", "3 x 9 reps; rest 120 sec; RPE 7")
    assert not coaching(brief, role, a)["regression_options"]
    a["regression_options"] = ["Reduce sets to 2."]
    result = coaching(brief, role, a)
    assert "sets to 2" in result["regression_options"][0]
    assert result["regression_options"] == ["Reduce sets to 2."]
    assert a["effective_prescription"] == "3 x 8 reps; rest 120 sec; RPE 7"


def card_for(brief):
    from api.structured_plan_deterministic_fallback import _session
    context = build_progression_context(brief)
    return {"schema_version": "1.0", "plan_metadata": {"title": "Numerical coaching"},
            "weeks": [{"week_id": str(i), "week_index": i, "phase_label": w["phase"],
                       "days": [{"date": f"2026-11-{i:02}", "countdown_label": r["scheduled_countdown_label"],
                                 "sessions": [_session(r, int(r["scheduled_countdown_label"][2:]),
                                     phase=w["phase"], progression_context=context)]}
                                for r in w["session_roles"]]}
                      for i, w in enumerate(brief["weekly_role_map"]["weeks"], 1)]}


def all_blocks(card):
    return [b for w in card["weeks"] for d in w["days"] for s in d["sessions"] for b in s["blocks"]]


def test_saved_profile_recovery_and_athlete_edits():
    brief, role, a = programme("3 x 8 reps; rest 120 sec", "3 x 9 reps; rest 120 sec")
    card = card_for(brief)
    block = all_blocks(card)[0]
    block["progression_rule"] = "Once every rep stays controlled, aim for more consistent movement at the same load and dose."
    before = deepcopy(card)
    recovered = recover_selected_coaching(card, brief)
    assert not all_blocks(recovered)[0].get("progression_rule")
    assert all(all_blocks(recovered)[0].get(k) == block.get(k) for k in DOSE)
    assert card == before
    block["athlete_change"] = {"revision": 1}
    block["progression_rule"] = "Athlete's own instruction."
    assert all_blocks(recover_selected_coaching(card, brief))[0] == block


def test_real_athlete_override_is_applied_after_api_coaching_recovery():
    from api.services.exercise_overrides import apply_exercise_overrides
    brief, role, a = programme("3 x 8 reps; rest 120 sec", "3 x 9 reps; rest 120 sec")
    card = card_for(brief)
    session = card["weeks"][0]["days"][0]["sessions"][0]
    original = session["blocks"][0]
    edited = {**original, "sets": 2, "progression_rule": None,
              "coaching_cues": ["Athlete chose this lighter prescription."]}
    row = {"structured_plan": card, "planning_brief": brief, "exercise_overrides": [{
        "id": "athlete-edit", "action": "edit", "scope": "occurrence", "date": "2026-11-01",
        "session_id": session["session_id"], "block_id": original["block_id"], "block": edited,
    }]}
    api = resolve_effective_structured_plan(row)
    assert api is not None
    projected = apply_exercise_overrides(row, api)
    actual = all_blocks(projected)[0]
    assert actual["sets"] == 2 and actual["progression_rule"] is None
    assert actual["coaching_cues"] == edited["coaching_cues"]
    assert actual["athlete_change"]["override_id"] == "athlete-edit"
    assert all_blocks(recover_selected_coaching(projected, brief))[0] == actual


@pytest.mark.skipif(not (ROOT / "web/node_modules/tsx").is_dir(), reason="Requires web npm ci")
def test_backend_read_and_react_preserve_identical_measurable_instructions():
    brief, role, a = programme("3 x 8 reps; rest 120 sec; RPE 7", "3 x 9 reps; rest 120 sec; RPE 7")
    a.update(progression_rule="Add 1 rep per set.", regression_options=["Reduce sets to 2."])
    card = card_for(brief)
    # The simulated AI conversion explicitly supplies these fields; the
    # source-free deterministic builder must not take them from Stage 1.
    all_blocks(card)[0].update({key: a[key] for key in ("progression_rule", "regression_options")})
    recovered = recover_selected_coaching(card, brief)
    assert all_blocks(recovered)[0]["progression_rule"] == all_blocks(card)[0]["progression_rule"]
    expected = [{"id": b["block_id"], "fields": {k: b[k] for k in (
        "coaching_cues", "progression_rule", "regression_options", "stop_rules") if k in b}}
        for b in all_blocks(recovered)]
    result = subprocess.run(["node", "--import", "tsx", "--import", "./test/register-css-stub.mjs",
        "test/assert-exercise-coaching-release.tsx"], cwd=ROOT / "web",
        input=json.dumps({"plan": recovered, "expected": expected}), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_context_and_coaching_do_not_change_source_or_require_a_model_call():
    brief, role, a = programme("3 x 8 reps; rest 120 sec", "3 x 9 reps; rest 120 sec")
    before = deepcopy(brief)
    start = perf_counter()
    for _ in range(100):
        coaching(brief, role, a)
    assert perf_counter() - start < 1
    assert brief == before


REPLAYS = json.loads((ROOT / "tests/fixtures/measurable_coaching/affected_programmes.json").read_text())


def test_model_conversion_cannot_publish_a_number_conflicting_with_next_session():
    from api.structured_plan_generation import build_structured_plan_outcome
    from fightcamp.render_authority import authoritative_render_for_role
    brief, role, a = programme("3 x 8 reps; rest 120 sec; RPE 7", "3 x 9 reps; rest 120 sec; RPE 7")
    card = card_for(brief)
    all_blocks(card)[0]["progression_rule"] = "Add 100 kg next time."
    before_doses = [{k: b.get(k) for k in DOSE} for b in all_blocks(card)]
    context = build_progression_context(brief)
    raw = []
    for week in brief["weekly_role_map"]["weeks"]:
        for source_role in week["session_roles"]:
            day = int(source_role["scheduled_countdown_label"][2:])
            raw.append(f"### D-{day} (Monday) — Strength")
            raw.extend(authoritative_render_for_role(source_role, week=week, d_day=day,
                                                     progression_context=context)["body_lines"])
    outcome = build_structured_plan_outcome(card, raw_markdown="\n".join(raw), planning_brief=brief)
    # The source owns Progress: the converter's invented number never ships.
    assert "100 kg" not in json.dumps(outcome.structured_plan or {})
    assert [{k: b.get(k) for k in DOSE} for b in all_blocks(card)] == before_doses


@pytest.mark.parametrize("record", REPLAYS, ids=lambda r: r["plan_id"][:8])
def test_recorded_old_and_latest_plans_preserve_selection_doses_and_authored_regressions(record):
    before = deepcopy(record["card"])
    recovered = recover_selected_coaching(before, record["brief"])
    assert before == record["card"]
    original = all_blocks(before)
    actual = all_blocks(recovered)
    assert len(actual) == len(original)
    for source, block in zip(original, actual):
        assert block["display_name"] == source["display_name"]
        assert all(block.get(k) == source.get(k) for k in DOSE)
        import re
        for warning in source.get("stop_rules") or []:
            criterion = re.sub(r"^(?:stop(?: the (?:set|effort))?\s+)?(?:if|when|for|on)\s+", "", warning, flags=re.I).rstrip(". ").casefold()
            assert criterion in " ".join(block.get("stop_rules") or []).casefold()
        # Keep the first specific option; recovery no longer appends alternatives.
        from fightcamp.exercise_coaching import is_profile_regression
        if source.get("regression_options") and not all(is_profile_regression(x) for x in source["regression_options"]):
            assert all(option in source["regression_options"] for option in block["regression_options"])
    if record["plan_id"].startswith("741"):
        from fightcamp.exercise_coaching import is_profile_progression
        assert not any(is_profile_progression(b.get("progression_rule", "")) for b in actual)
        assert all(len(b.get("coaching_cues", [])) <= 1 for b in actual)
        rules = [b["progression_rule"] for b in actual if b.get("progression_rule")]
        assert all(rule in str(before) for rule in rules)  # Never manufacture a replacement.
        assert all(option in str(before) for b in actual for option in b.get("regression_options", []))
    api = resolve_effective_structured_plan({"structured_plan": before, "planning_brief": record["brief"]})
    assert api is not None
    assert [b.get("progression_rule") for b in all_blocks(api)] == [b.get("progression_rule") for b in actual]


@pytest.mark.skipif(not (ROOT / "web/node_modules/tsx").is_dir(), reason="Requires web npm ci")
@pytest.mark.parametrize("record", REPLAYS, ids=lambda r: r["plan_id"][:8])
def test_recorded_athlete_instructions_survive_react(record):
    card = recover_selected_coaching(record["card"], record["brief"])
    expected = [{"id": b["block_id"], "fields": {
        k: [s.removeprefix("Cue: ") for s in b[k]] if k == "coaching_cues" else b[k]
        for k in ("coaching_cues", "progression_rule", "regression_options", "stop_rules") if k in b}}
        for b in all_blocks(card)]
    result = subprocess.run(["node", "--import", "tsx", "--import", "./test/register-css-stub.mjs",
        "test/assert-exercise-coaching-release.tsx"], cwd=ROOT / "web",
        input=json.dumps({"plan": card, "expected": expected}), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name,dose,note", [
    ("Back Squat", "3 x 8 reps @ 50 kg; rest 120 sec; RPE 7", "Add 2.5kg."),
    ("Kettlebell Swing", "3 x 4 reps; rest 90 sec; explosive", "Add 1 rep/set."),
    ("Farmer Carry", "3 carries over 20 m; rest 90 sec", "Add 2m."),
    ("Easy Assault Bike", "20 min continuous; RPE 4", "Add 1 min."),
    ("Back Squat", "3 x 8 reps; RPE 9", None),
    ("Back Squat", "3-5 x 8 reps", None),
    ("Unknown coach movement", "3 x 8 reps", None),
])
def test_missing_ai_note_stays_absent_without_changing_work(name, dose, note):
    brief, role, assignment = programme(dose, dose, name=name)
    brief["weekly_role_map"]["weeks"] = brief["weekly_role_map"]["weeks"][:1]
    before = deepcopy(brief)
    result = coaching(brief, role, assignment)
    assert not result.get("progression_rule")  # No authored instruction, regardless of dose.
    assert brief == before


@pytest.mark.parametrize("athlete,phase,day", [({}, "TAPER", 35), ({}, "GPP", 14),
    ({"fatigue": "high"}, "GPP", 35), ({"injuries": ["knee"]}, "GPP", 35)])
def test_missing_progress_stays_hidden_in_restricted_contexts(athlete, phase, day):
    brief, role, assignment = programme("3 x 8 reps", "3 x 8 reps", athlete=athlete, phase=phase)
    brief["weekly_role_map"]["weeks"] = brief["weekly_role_map"]["weeks"][:1]
    assert not coaching(brief, role, assignment, day).get("progression_rule")


def test_unsupported_saved_short_note_is_removed_without_replacement():
    brief, role, assignment = programme("3 x 8 reps @ 100 kg", "3 x 8 reps @ 100 kg")
    card = card_for(brief)
    all_blocks(card)[0]["progression_rule"] = "Add +5kg."
    result = recover_selected_coaching(card, brief)
    assert not all_blocks(result)[0].get("progression_rule")
    assert recover_selected_coaching(result, brief) == result
