"""Safety fields must change real decisions, without granting other permissions."""
from copy import deepcopy
import json
import os
import subprocess
import sys

import pytest

from fightcamp import conditioning
from fightcamp.injury_filtering import injury_match_details
from fightcamp.injury_guard import injury_decision, pick_safe_replacement
from fightcamp.injury_location_registry import canonicalize_location_from_registry, get_rehab_location_candidates
from fightcamp.late_selector_windows import fight_proximity_allowed, late_window_allowed
from fightcamp.rehab_selector import select_rehab_candidate
from fightcamp.selection_metadata import normalize_selection_metadata
from fightcamp.session_composition import option_mechanical_risk_tags
from fightcamp.training_catalogue import DATA_DIR, build_catalogue, content_hash
from fightcamp.training_context import normalize_equipment_list
from tests.test_generator_equipment_accuracy import flags, generate, record
from tests.test_rehab_selector import drill, injury
from tools.replay_safety_metadata import ROOT, original_safety_source, source_changes


def without_field(value, field):
    """Compare all programming output while ignoring the source field under test."""
    if isinstance(value, dict):
        return {k: without_field(v, field) for k, v in value.items() if k != field}
    if isinstance(value, (list, tuple)):
        return type(value)(without_field(v, field) for v in value)
    return value


@pytest.mark.parametrize("name,symptoms", [
    ("Jump Rope (Recovery Pace)", "mild shin pain"),  # avoid_if
    ("Split-Step Ankle Snap Pogo", "mild foot pain"),  # avoid_if
    ("Landmine Split-Stance Punch Press", "mild shoulder pain"),  # "...symptoms are active"
])
@pytest.mark.parametrize("phase", ["GPP", "SPP", "TAPER"])
def test_regional_prohibitions_override_mechanical_risk_scores(name, symptoms, phase):
    row = record(name)
    old = {k: v for k, v in row.items() if k not in {"avoid_if", "contraindications"}}
    assert injury_decision(old, [symptoms], phase, "low").action != "exclude"
    decision = injury_decision(row, [symptoms], phase, "low")
    assert decision.action == "exclude"
    assert decision.reason["bucket"] == "safety_metadata"
    assert injury_decision(row, [], phase, "low") == injury_decision(old, [], phase, "low")
    # A restriction on a different region cannot prohibit this exercise.
    assert injury_decision(row, ["mild neck pain"], phase, "low") == injury_decision(old, ["mild neck pain"], phase, "low")


@pytest.mark.parametrize("name,symptoms", [
    ("Punch-Specific Max Isometric Hold", "mild wrist pain"),  # "...if pain spikes..."
    ("Overcoming Split-Squat Isometric", "mild knee pain"),  # "...if irritation increases..."
])
@pytest.mark.parametrize("phase", ["GPP", "SPP", "TAPER"])
def test_conditional_contraindications_warn_instead_of_excluding_on_region(name, symptoms, phase):
    # A response-conditional note is not met by a mild report on its own; it
    # adapts through the severity-weighted score (functional-impact rules).
    row = record(name)
    decision = injury_decision(row, [symptoms], phase, "low")
    assert decision.reason.get("bucket") != "safety_metadata"
    assert decision.action != "exclude"
    rank = {"allow": 0, "modify": 1, "exclude": 2}
    severe = injury_decision(row, [symptoms.replace("mild", "severe")], phase, "low")
    assert rank[severe.action] >= rank[decision.action]


@pytest.mark.parametrize("name,symptoms", [
    ("Landmine Split-Stance Punch Press", "mild shoulder pain"),
])
def test_contraindications_change_generated_work_and_alternate_pools(monkeypatch, name, symptoms):
    row = record(name)
    athlete = flags(name, normalize_equipment_list(row["equipment"]))
    athlete.update(phase="SPP", days_until_fight=18, injuries=[symptoms])
    old = {k: v for k, v in row.items() if k != "contraindications"}
    before, after = generate(monkeypatch, old, athlete), generate(monkeypatch, row, athlete)
    assert name in before[1] and name in before[0]
    assert name not in after[1] and name not in after[0]
    assert name not in {r["name"] for r in after[2]}
    athlete["injuries"] = []
    assert without_field(generate(monkeypatch, old, athlete), "contraindications") == without_field(
        generate(monkeypatch, row, athlete), "contraindications")


def test_avoid_if_cannot_reenter_conditioning_alternate_pool(monkeypatch):
    row = record("Jump Rope (Recovery Pace)")
    athlete = flags(row["name"], ["jump_rope"])
    athlete.update(phase="GPP", days_until_fight=35, injuries=["mild shin pain"])
    old = {k: v for k, v in row.items() if k != "avoid_if"}
    assert row["name"] in generate(monkeypatch, old, athlete)[0]
    after = generate(monkeypatch, row, athlete)
    assert row["name"] not in after[0] | after[1]


def test_replacement_rechecks_prohibitions_and_mechanical_loading():
    prohibited = {"name": "Control drill", "tags": [], "avoid_if": ["wrist"]}
    mechanically_unsafe = {"name": "Heavy bench press", "tags": ["wrist_loaded_extension"]}
    safe = {"name": "Gentle breathing", "tags": ["recovery"]}
    replacement, decision = pick_safe_replacement(
        prohibited, [prohibited, mechanically_unsafe, safe],
        {"injuries": ["severe wrist pain"], "phase": "SPP", "fatigue": "low"},
    )
    assert replacement == safe and decision.action == "allow"


def test_cache_does_not_clear_changed_metadata_or_dictionary_injuries():
    row = {"id": "cache-probe", "name": "Control drill", "tags": []}
    restriction = [{"canonical_location": "wrist", "severity": "low"}]
    assert injury_decision(row, restriction, "GPP", "low").action == "allow"
    row["contraindications"] = ["avoid if wrist symptoms are active"]
    assert injury_decision(row, restriction, "GPP", "low").action == "exclude"
    assert injury_match_details(row, ["wrist"])
    row["contraindications"] = None
    assert injury_decision(row, restriction, "GPP", "low").action == "allow"
    assert not injury_match_details(row, ["wrist"])


@pytest.mark.parametrize("field", ["avoid_if", "contraindications"])
def test_null_metadata_never_overrides_existing_injury_exclusions(field):
    row = {"name": "Bench Press", "tags": ["mech_horizontal_push"], field: None}
    assert injury_decision(row, ["severe shoulder pain"], "TAPER", "high").action == "exclude"


def test_equipment_and_non_anatomical_notes_do_not_create_injury_evidence():
    row = {"name": "Control drill", "tags": [], "contraindications": [
        "skip when pins or trap bar are unavailable", "avoid if aggressive brace work spikes blood pressure during the cut"]}
    assert injury_decision(row, ["mild knee pain"], "GPP", "low").action == "allow"


@pytest.mark.parametrize("name", [
    "5-10-15 Ladder (Box Jumps/Push-Ups/KB Swings)", "EMOM: 5 Squat Cleans + 5 Burpees",
])
@pytest.mark.parametrize("days", [20, 21, 22, 35])
def test_countdown_changes_real_conditioning_selection(monkeypatch, name, days):
    row = record(name)
    athlete = flags(name, normalize_equipment_list(row["equipment"]))
    athlete.update(phase="SPP", days_until_fight=days, key_goals=["conditioning"], weaknesses=["gas_tank"])
    old = {k: v for k, v in row.items() if k != "block_if_fight_within_days"}
    before, after = generate(monkeypatch, old, athlete), generate(monkeypatch, row, athlete)
    assert name in before[0] & before[1]
    if days <= 21:
        assert name not in after[0] | after[1]
    else:
        assert without_field(before, "block_if_fight_within_days") == without_field(after, "block_if_fight_within_days")


@pytest.mark.parametrize("name", ["Pad EMOM (5x5 format)", "5-10-15 Ladder (Box Jumps/Push-Ups/KB Swings)",
                                  "EMOM: 5 Squat Cleans + 5 Burpees"])
def test_explicit_countdowns_override_dated_window_permissions(monkeypatch, name):
    from fightcamp import stage2_payload

    row = record(name)
    limit = row["block_if_fight_within_days"]
    # An opt-in must never override the same row's explicit countdown block.
    row["late_windows"] = ["all"]
    for day in (0, 1, limit - 1, limit):
        assert not late_window_allowed([row], offset=day)
    assert late_window_allowed([row], offset=limit + 1)
    # The dated assignment consults original bank authority even when an
    # option's serialized metadata claims unrestricted late-window permission.
    monkeypatch.setattr(stage2_payload, "original_bank_entries", lambda _: [row])
    slot = {"selected": {"name": name, "selection_metadata": {"late_windows": ["all"]}}}
    assert not stage2_payload._slot_late_window_allowed("SPP", "conditioning_slots", slot, offset=limit)
    assert stage2_payload._slot_late_window_allowed("SPP", "conditioning_slots", slot, offset=limit + 1)
    assert not fight_proximity_allowed(row, None)
    assert not conditioning._evaluate_conditioning_late_window(
        row, system=row["system"], window=None, bridge_rules=None, days_until_fight=limit + 1,
    )["blocked"]
    assert conditioning._evaluate_conditioning_late_window(
        row, system=row["system"], window=None, bridge_rules=None, days_until_fight=limit,
    )["blocked"]


def test_null_countdown_does_not_grant_late_window_authority():
    row = {"block_if_fight_within_days": None}
    assert not late_window_allowed([row], offset=7)
    assert not fight_proximity_allowed({"block_if_fight_within_days": "unknown"}, 7)
    assert not fight_proximity_allowed({"block_if_fight_within_days": 21}, -1)


def test_stage_selector_does_not_advance_from_null_stage_or_missing_region():
    result = select_rehab_candidate(injury=injury(), rehab_stage="restore", candidates=[
        drill("advanced", stage="load"), drill("unknown_stage", stage="unreviewed"),
        drill("missing_region", target_regions=None), drill("baseline", stage=None),
    ])
    assert result.selected_drill_id == "baseline"
    assert {r.drill_id for r in result.rejected_candidates} == {"advanced", "unknown_stage", "missing_region"}
    # Compatibility with an unmigrated baseline drill does not activate LOAD.
    assert select_rehab_candidate(injury=injury(), rehab_stage="load", candidates=[drill("baseline", stage=None)]).selected_drill_id is None


def test_duplicate_corrections_preserve_all_identities_and_every_distinct_risk():
    after = {r["catalogue_id"]: r for r in build_catalogue()}
    changes = source_changes()
    assert len(changes) == 14
    source = "data/style_taper_conditioning.json"
    raw = (ROOT / source).read_bytes()
    old_rows = json.loads(original_safety_source(source, raw))
    new_rows = json.loads(raw)
    changed = {c["after_sha256"]: c for c in changes}
    for old, new in zip(old_rows, new_rows):
        if old == new:
            continue
        change = changed[content_hash(new)]
        assert content_hash(old) == change["before_sha256"]
        assert {**new, **change["before"]} == old
        assert new["mechanical_risk_tags"] == list(dict.fromkeys(old["mechanical_risk_tags"]))
        assert normalize_selection_metadata(old) == normalize_selection_metadata(new)
        assert option_mechanical_risk_tags(old) == option_mechanical_risk_tags(new) == set(new["mechanical_risk_tags"])
        entry = after[change["catalogue_id"]]
        assert entry["row_sha256"] == change["after_sha256"]
        assert entry["source_id"] == change["catalogue_id"].split(":", 1)[1]
        for symptoms in ("ankle sprain", "knee pain", "lower back strain", "shoulder pain", "wrist pain"):
            assert injury_match_details(deepcopy(old), [symptoms]) == injury_match_details(deepcopy(new), [symptoms])
            assert injury_decision(deepcopy(old), [symptoms], "TAPER", "high") == injury_decision(deepcopy(new), [symptoms], "TAPER", "high")
    # Unknown edits must still fail frozen replay rather than refreshing hashes.
    tampered = raw.replace(changes[0]["name"].encode(), b"Unreviewed drill", 1)
    with pytest.raises(ValueError, match="Safety source history drift"):
        original_safety_source(source, tampered)


def test_upper_back_source_aliases_remain_compatible_without_rewriting_exposures():
    bank = json.loads((DATA_DIR / "rehab_bank.json").read_bytes())
    rows = [d for g in bank for d in g["drills"] if d.get("target_regions") in (["upper back"], ["upper_back"])]
    assert len(rows) == 52
    assert sum(d["target_regions"] == ["upper back"] for d in rows) == 32
    assert canonicalize_location_from_registry("upper_back") == "upper back"
    assert set(get_rehab_location_candidates("upper back")) >= {"upper back", "upper_back"}
    for spelling in ("upper back", "upper_back"):
        result = select_rehab_candidate(
            injury=injury(body_region=spelling, body_region_aliases=get_rehab_location_candidates(spelling), injury_type="unspecified"),
            rehab_stage="restore", candidates=rows,
        )
        assert result.eligible_candidate_count == 52
        assert result.selected_drill_id


def test_historical_upper_back_exposures_keep_exact_identity():
    from api.contracts.rehab_evidence import read_exact_events
    from tests.test_rehab_transition_engine import ATHLETE, event, injury as episode

    row = event()
    raw = row["event_json"]
    raw.update(body_region="upper_back")
    raw["demand"]["target_regions"] = ["upper_back"]
    stored = deepcopy(row)
    exact, ignored = read_exact_events(athlete_id=ATHLETE, injury=episode(body_region="upper_back"), exposure_rows=[row])
    assert len(exact) == 1 and not ignored
    aliased, ignored = read_exact_events(athlete_id=ATHLETE, injury=episode(body_region="upper back"), exposure_rows=[row])
    assert not aliased and ignored["ignored_region_mismatch"] == 1
    assert row == stored


@pytest.mark.parametrize("field,value", [("dose", None), ("dose", {"sets": 99, "reps": 99}),
                                         ("pain_ceiling", None), ("pain_ceiling", 0), ("rehab_stage", None)])
def test_unreviewed_bank_edits_cannot_change_a_live_prescription(field, value):
    from api.contracts.injury_policy import resolve_injury_policy
    from fightcamp.rehab_protocols import get_rehab_bank
    from tests.test_rehab_prescription_bundles import fixture

    policy, episode, before = fixture()
    assert before["prescription"]
    bank = deepcopy(get_rehab_bank())
    identity = policy.prescriptions[0].drill_id
    row = next(d for group in bank for d in group["drills"] if d["id"] == identity)
    assert row.get(field) != value
    row[field] = value
    after = resolve_injury_policy(episode, policies=(policy,), bank=bank)
    assert after["prescription"] is None
    assert after["reason_codes"] == ["rehab_policy_stale_or_incomplete"]
    assert after["restrictions"] == before["restrictions"]


def test_complete_programmes_preserve_selection_dose_periodisation_and_personalisation(tmp_path):
    evidence = json.loads((ROOT / "tests/fixtures/safety_metadata_programmes_current.json").read_bytes())
    target = tmp_path / "plans.json"
    subprocess.run([sys.executable, "tools/canonical_metadata_plan_probe.py",
                    "tests/fixtures/tag_retirement_plans.json", str(target)], cwd=ROOT,
                   env={**os.environ, "PYTHONHASHSEED": "0"}, check=True, capture_output=True, timeout=300)
    actual = json.loads(target.read_bytes())
    assert len(actual) == 6
    for plan, expected in zip(actual, evidence["plans"]):
        assert plan["case"] == expected["case"]
        assert content_hash(plan["plan_text"]) == expected["plan_text_sha256"]
        assert content_hash(plan["stage2_payload"]) == expected["stage2_payload_sha256"]
