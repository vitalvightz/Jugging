"""Regional tendon profiles use shared safety, scheduling and completion."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from api.contracts.injury_policy import rehab_allocation_count, reconcile_session_prescription, resolve_injury_policy
from api.contracts.rehab_completion import build_rehab_exposure_event, resolve_rehab_completion
from api.contracts.rehab_evidence import read_exact_events
from api.contracts.rehab_schedule import schedule_rehab
from api.services.rehab_completion_service import session_rehab_items
from fightcamp.rehab_clinical import content_hash, load_clinical_policies, validate_clinical_bank
from fightcamp.rehab_protocols import get_rehab_bank
from tools.rehab_metadata_review_lib import source_hash

ROOT = Path(__file__).resolve().parents[1]
REPAIRED = {"achilles_tendonitis_eccentric_calf_drops_on_step", "shoulder_tendonitis_banded_scaption_holds",
    "bicep_tendonitis_incline_db_curl_eccentric_focus", "forearm_tendonitis_eccentric_wrist_extensions",
    "elbow_tendonitis_eccentric_reverse_wrist_curls", "wrist_tendonitis_eccentric_wrist_flexion_with_dumbbell",
    "wrist_tendonitis_pronation_supination_twists"}
REGIONS = ["achilles", "shoulder", "biceps", "forearm", "elbow", "wrist", "hand", "fingers"]
PAIRS = [(region, "tendonitis") for region in REGIONS]
DAY = "2026-10-03"
PLAN = str(uuid4())


def injury(region, kind, stage="calm", **changes):
    row = dict(id=str(uuid4()), episode_id=str(uuid4()), athlete_id=str(uuid4()),
               body_region=region, canonical_location=region, body_area=f"Left {region}",
               description=f"{region} {kind}", side="left", injury_type=kind, severity="mild",
               status="open", created_at="2026-10-01T00:00:00Z", updated_at="2026-10-01T00:00:00Z")
    if stage == "restore":
        row.update(status="monitoring", latest_reported_status="improving", rehab_stage="restore")
    row.update(changes)
    return row


def resolve(row, **kwargs):
    return resolve_injury_policy(row, policies=load_clinical_policies(), bank=get_rehab_bank(), **kwargs)


@pytest.mark.parametrize("region,kind", PAIRS)
@pytest.mark.parametrize("stage", ["calm", "restore"])
def test_region_and_exact_type_resolve_a_reviewed_prescription_and_real_completion(region, kind, stage):
    row = injury(region, kind, stage)
    decision = resolve(row)
    assert decision["outcome"] == "prescribed_rehab"
    assert decision["policy_id"] == f"{region}_{kind}" and decision["stage"] == stage
    prescription = decision["prescription"]
    # #2984 shares compatible published bank options across live profiles.
    restore_ids = {
        "wrist": "wrist_tendonitis_pronation_supination_twists",
        "shoulder": "shoulder_impingement_scapular_wall_slides_with_chin_tuck",
        "elbow": "elbow_unspecified_pronation_supination_with_dowel",
    }
    assert prescription["drill_id"] == (
        restore_ids.get(region, f"{region}_{kind}_reviewed_restore")
        if stage == "restore" else f"{region}_{kind}_recovery_support"
    )
    if stage == "restore" and region in {"shoulder", "elbow"}:
        assert kind in prescription["drill"]["compatible_injury_types"]
    assert prescription["bank_hash"] == content_hash(prescription["drill"])
    assert prescription["dose"] == {} and prescription["minimum_gap_days"] == 1
    frozen = reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY)
    assert rehab_allocation_count(frozen["session"]["blocks"]) == 1
    items = session_rehab_items({"id": PLAN}, training_day=DAY, session_id=frozen["session"]["session_id"], prescription=frozen)
    completion = dict(status="done", rehab_performance="done_as_shown", prescription_snapshot=frozen)
    resolutions = resolve_rehab_completion(items, [row], completion=completion)
    assert len(resolutions.eligible) == 1
    event = build_rehab_exposure_event(resolutions.eligible[0], athlete_id=row["athlete_id"], plan_id=PLAN,
        session_id=frozen["session"]["session_id"], training_day=DAY, completion=completion, during="same", limit="no")
    assert event.is_attributable_to(row)
    assert event.dose_completed.completion_state == "performed_amount_unknown"
    assert event.dose_completed.reps is None


@pytest.mark.parametrize("region,kind", PAIRS)
def test_reviewed_hashes_match_and_original_sources_survive_authorised_repairs(region, kind):
    ledger = {r["drill_id"]: r for r in json.loads((ROOT / "data/rehab_metadata_review.json").read_text(encoding="utf-8"))}
    policy = next(p for p in load_clinical_policies() if (p.region, p.injury_type) == (region, kind))
    assert validate_clinical_bank((policy,), get_rehab_bank()) == []
    for p in policy.prescriptions:
        record = ledger[p.drill_id]
        assert record["review_state"] == "reviewed"
        assert record["source_hash"] == source_hash(drill_id=record["drill_id"], location=record["location"],
            injury_type=record["injury_type"], name=record["name"], notes=record["notes"])
    before = json.loads((ROOT / "docs/tendon-family-bank-audit.json").read_text(encoding="utf-8"))
    for original in (r for r in before if r["region"] == region and r["drill_id"] in REPAIRED):
        record = ledger[original["drill_id"]]
        assert record["review_state"] == "reviewed"
        assert original["source_hash"] in {record["source_hash"], *(r["source_hash"] for r in record.get("source_history", []))}
        assert "GPP:" not in record["notes"] and "SPP:" not in record["notes"]


@pytest.mark.parametrize("region,kind", PAIRS)
def test_cross_region_or_type_and_unreviewed_content_fail_closed(region, kind):
    policy = next(p for p in load_clinical_policies() if (p.region, p.injury_type) == (region, kind))
    for other in [injury("elbow" if region != "elbow" else "ankle", kind),
                  injury(region, "strain")]:
        assert resolve_injury_policy(other, policies=(policy,), bank=get_rehab_bank())["prescription"] is None
    changed = deepcopy(get_rehab_bank())
    drill = next(d for g in changed for d in g["drills"] if d["id"] == policy.prescriptions[0].drill_id)
    drill["notes"] += " Unreviewed change."
    assert resolve_injury_policy(injury(region, kind), policies=(policy,), bank=changed)["prescription"] is None


@pytest.mark.parametrize("region,kind", PAIRS)
def test_worsening_returns_to_protection(region, kind):
    decision = resolve(injury(region, kind, "restore", latest_reported_status="worse"))
    assert decision["stage"] == "calm" and decision["prescription"]["drill"]["rehab_stage"] == "calm"


@pytest.mark.parametrize("region", REGIONS)
def test_restore_pull_back_uses_real_loading_hold_semantics(region):
    row = injury(region, "tendonitis", "restore")
    decision = resolve(row)
    assert decision["prescription"]["is_loading"] is (region == "achilles")
    scheduled = schedule_rehab(row, decision, training_day=DAY, readiness_decision="pull_back")
    # #2993: RESTORE remains available on pull-back days, even if loading.
    assert decision["stage"] == "restore"
    assert scheduled["state"] == "due"
    assert schedule_rehab(row, decision, training_day=DAY, readiness_decision="stop")["state"] == "held"


def test_achilles_tendon_loading_function_cannot_bypass_live_stage_gates():
    row = injury("achilles", "tendonitis", "restore")
    decision = resolve(row)
    assert decision["stage"] == "restore"
    assert decision["prescription"]["drill_id"] == "achilles_tendonitis_reviewed_restore"
    assert decision["prescription"]["drill"]["function"] == "tendon_loading"
    assert decision["prescription"]["drill"]["rehab_stage"] == "restore"
    assert schedule_rehab(row, decision, training_day=DAY)["state"] == "due"
    policy = next(p for p in load_clinical_policies() if p.policy_id == "achilles_tendonitis")
    assert policy.live_stages == ["calm", "restore", "load"]
    assert policy.transitions[0].promotable and not any(t.promotable for t in policy.transitions[1:])
    for stage in ("load", "dynamic", "return"):
        injected = resolve({**row, "rehab_stage": stage})
        assert injected["stage"] == "calm"
        assert injected["prescription"]["drill_id"] == "achilles_tendonitis_recovery_support"
        assert not injected["prescription"]["is_loading"]


def test_wrist_baseline_groups_cover_all_camp_phases_and_bicep_alias_resolves():
    from fightcamp.injury_location_registry import canonicalize_location_from_registry
    from fightcamp.rehab_schema import split_phase_progression
    for group in get_rehab_bank():
        if group["location"] == "wrist" and group["type"] == "tendonitis" and any(
                d["rehab_stage"] in {"calm", "restore"} for d in group["drills"]):
            assert split_phase_progression(group["phase_progression"]) == ["GPP", "SPP", "TAPER"]
        if group["location"] == "bicep" and group["type"] == "tendonitis":
            assert all(d["target_regions"] == ["bicep"] for d in group["drills"])
    for region in ("bicep", "biceps"):
        for stage in ("calm", "restore"):
            row = injury(region, "tendonitis", stage,
                         canonical_location=canonicalize_location_from_registry(region))
            assert resolve(row)["policy_id"] == "biceps_tendonitis"


@pytest.mark.parametrize("region,kind", PAIRS)
@pytest.mark.parametrize("changes", [dict(severity="severe"), dict(description="acute major tendon tear"),
    dict(injury_type="tendon_rupture_or_avulsion", description="tendon rupture"), dict(description="suspected tendon rupture"),
    dict(description="suspected fracture"), dict(description="dislocated joint"), dict(description="subluxation"),
    dict(description="numbness and tingling"), dict(rehab_medical_gate=True),
    dict(injury_type="post_surgery", description="post surgery")])
def test_serious_structural_and_neurological_cases_require_medical_review(region, kind, changes):
    decision = resolve(injury(region, kind, **changes))
    assert decision["outcome"] == "medical_review" and decision["prescription"] is None


@pytest.mark.parametrize("region,kind", PAIRS)
@pytest.mark.parametrize("stage", ["load", "dynamic", "return"])
def test_external_stage_cannot_open_an_advanced_rung(region, kind, stage):
    decision = resolve(injury(region, kind, "restore", rehab_stage=stage))
    assert decision["stage"] in {"calm", "restore"}
    assert decision["prescription"]["drill"]["rehab_stage"] in {"calm", "restore"}
    policy = next(p for p in load_clinical_policies() if (p.region, p.injury_type) == (region, kind))
    if policy.policy_id in {"achilles_tendonitis", "elbow_tendonitis", "shoulder_tendonitis"}:
        assert policy.live_stages == ["calm", "restore", "load"]
        assert policy.transitions[0].promotable and not any(t.promotable for t in policy.transitions[1:])
    else:
        assert policy.live_stages == ["calm", "restore"] and not any(t.promotable for t in policy.transitions)


@pytest.mark.parametrize("region,kind", PAIRS)
def test_unknown_side_uses_recordable_protection_without_capacity_credit(region, kind):
    row = injury(region, kind, "restore", side="unknown")
    decision = resolve(row)
    assert decision["prescription"]["drill_id"] == f"{region}_{kind}_recovery_support"
    frozen = reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY)
    items = session_rehab_items({"id": PLAN}, training_day=DAY, session_id=frozen["session"]["session_id"], prescription=frozen)
    completion = dict(status="done", prescription_snapshot=frozen)
    eligible = resolve_rehab_completion(items, [row], completion=completion).eligible
    assert len(eligible) == 1
    event = build_rehab_exposure_event(eligible[0], athlete_id=row["athlete_id"], plan_id=PLAN,
        session_id=frozen["session"]["session_id"], training_day=DAY, completion=completion, during="same", limit="no")
    assert event.side == "unknown" and event.is_attributable_to(row)
    evidence, ignored = read_exact_events(athlete_id=row["athlete_id"], injury=row,
        exposure_rows=[dict(athlete_id=row["athlete_id"], event_json=event.model_dump(mode="json"))])
    assert evidence == [] and ignored["ignored_side_mismatch"] == 1


@pytest.mark.parametrize("region", ["foot", "heel", "shin", "groin", "obliques", "lower back",
    "upper back", "chest", "triceps", "neck", "jaw", "unspecified", "knee"])
def test_unprofiled_bank_regions_and_absent_knee_do_not_inherit_tendon_work(region):
    decision = resolve(injury(region, "tendonitis"))
    assert decision["prescription"] is None


@pytest.mark.parametrize("region", REGIONS)
@pytest.mark.parametrize("changes", [dict(severity="high"), dict(injury_type="tendon_rupture"),
    dict(description="complete tendon tear"), dict(description="post-surgery tendon repair"),
    dict(description="neurological symptoms numbness")])
def test_tendon_specific_structural_and_high_severity_gates(region, changes):
    decision = resolve(injury(region, "tendonitis", **changes))
    assert decision["outcome"] == "medical_review" and decision["prescription"] is None


@pytest.mark.parametrize("region", REGIONS)
def test_unrelated_pain_does_not_become_tendonitis(region):
    decision = resolve(injury(region, "pain"))
    assert decision["injury_type"] == "pain"
    if decision["prescription"] is not None:
        assert decision["policy_id"] == f"{region}_pain"
        policy = next(p for p in load_clinical_policies() if p.policy_id == decision["policy_id"])
        assert policy.pathway_family == "nonspecific_msk_symptoms"
        assert decision["prescription"]["drill_id"] in {p.drill_id for p in policy.prescriptions}


def test_inventory_repaired_id_history_and_tendon_content_preservation():
    audit = json.loads((ROOT / "docs/tendon-family-bank-audit.json").read_text(encoding="utf-8"))
    assert len(audit) == 60 and len({r["region"] for r in audit}) == 20
    for original in audit:
        assert original["source_hash"] == source_hash(drill_id=original["drill_id"],
            location=original["bank_location"], injury_type="tendonitis",
            name=original["name"], notes=original["notes"])
    assert all(r["review_state"] == "needs_review" and r["rehab_stage"] is None for r in audit)
    hashes = json.loads((ROOT / "tests/fixtures/rehab_bank_before_tendon_hashes.json").read_text(encoding="utf-8"))
    from tests.rehab_inventory_history import inventory_with_history
    historical_bank, historical_ledger = inventory_with_history()
    bank = {d["id"]: d for g in historical_bank for d in g["drills"]}
    assert hashes.keys() <= bank.keys()
    # Later family rollouts preserve their own pre-rollout fixtures. Keep the
    # tendon assertions exact without freezing unrelated inventory forever.
    tendon_ids = {d["id"] for g in historical_bank if g["type"] == "tendonitis" for d in g["drills"]}
    assert {identity for identity in hashes if identity in tendon_ids and content_hash(bank[identity]) != hashes[identity]} == REPAIRED
    assert len(tendon_ids - hashes.keys()) == 15
    ledger = {r["drill_id"]: r for r in historical_ledger}
    active = {p.drill_id for policy in load_clinical_policies() for p in policy.prescriptions}
    assert {r["drill_id"] for r in audit} & active == {"wrist_tendonitis_pronation_supination_twists",
        "achilles_tendonitis_eccentric_calf_drops_on_step"}
    for record in audit:
        current = ledger[record["drill_id"]]
        if record["drill_id"] in REPAIRED:
            assert current["review_state"] == "reviewed"
            assert record["source_hash"] in {r["source_hash"] for r in current["source_history"]}
        else:
            assert current["review_state"] == "needs_review"
            assert current["source_hash"] == record["source_hash"]


def test_dormant_reviewed_load_work_is_not_a_prescription():
    ledger = json.loads((ROOT / "data/rehab_metadata_review.json").read_text(encoding="utf-8"))
    load_ids = {r["drill_id"] for r in ledger if r["injury_type"] == "tendonitis"
                and r["review_state"] == "reviewed" and r["proposed"]["rehab_stage"] == "load"}
    assert len(load_ids) == 8
    policies = [p for p in load_clinical_policies() if p.injury_type == "tendonitis"]
    assert load_ids & {r.drill_id for p in policies for r in p.prescriptions} == {"achilles_tendonitis_eccentric_calf_drops_on_step", "elbow_tendonitis_supported_hand_weight_wrist_extension", "shoulder_tendonitis_wall_push_up"}
    assert all(p.live_stages == ["calm", "restore"] and not any(t.promotable for t in p.transitions)
        for p in policies if p.policy_id not in {"achilles_tendonitis", "elbow_tendonitis", "shoulder_tendonitis"})


@pytest.mark.parametrize("region", REGIONS)
def test_positive_reports_and_complete_history_cannot_replace_missing_clinical_inputs(region):
    row = injury(region, "tendonitis", "restore")
    decision = resolve(row)
    frozen = reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY)
    items = session_rehab_items({"id": PLAN}, training_day=DAY, session_id=frozen["session"]["session_id"], prescription=frozen)
    completion = dict(status="done", prescription_snapshot=frozen)
    eligible = resolve_rehab_completion(items, [row], completion=completion).eligible[0]
    event = build_rehab_exposure_event(eligible, athlete_id=row["athlete_id"], plan_id=PLAN,
        session_id=frozen["session"]["session_id"], training_day=DAY, completion=completion, during="better", limit="no")
    raw = event.model_dump(mode="json")
    raw["response"]["next_day_response"] = "same"
    reports = []
    for _ in range(20):
        observation = deepcopy(raw)
        observation.update(exposure_id=str(uuid4()), response_group_id=str(uuid4()))
        reports.append(dict(athlete_id=row["athlete_id"], event_json=observation))
    for truncated in (False, True):
        assert resolve(row, exposures=reports, history_truncated=truncated)["stage"] == "restore"
    negative = deepcopy(reports[-1])
    negative["event_json"]["response"]["next_day_response"] = "worse"
    assert resolve(row, exposures=[negative])["stage"] == "calm"


def test_bank_and_review_seed_are_byte_idempotent(tmp_path, monkeypatch):
    from tools import seed_tendon_family
    from tools.generate_rehab_metadata_review import build_ledger
    folder = tmp_path / "data"
    folder.mkdir()
    names = ["rehab_bank.json", "rehab_metadata_review.json", "rehab_pathways.json", "rehab_bank_duplicate_debt.json"]
    expected = {}
    for name in names:
        expected[name] = (ROOT / "data" / name).read_bytes()
        (folder / name).write_bytes(expected[name])
    monkeypatch.setattr(seed_tendon_family, "ROOT", tmp_path)
    seed_tendon_family.main()
    seed_tendon_family.main()
    assert {name: (folder / name).read_bytes() for name in names} == expected
    bank = json.loads(expected["rehab_bank.json"])
    ledger = json.loads(expected["rehab_metadata_review.json"])
    assert build_ledger(bank, prior=ledger) == ledger


def test_vocabulary_audit_passes():
    result = subprocess.run([sys.executable, str(ROOT / "tools/audit_injury_vocabulary.py")],
                            capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


