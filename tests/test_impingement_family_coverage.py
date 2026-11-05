"""Impingement profiles preserve the existing safety and ownership contracts."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from api.contracts.injury_policy import reconcile_session_prescription, resolve_injury_policy
from api.contracts.rehab_completion import build_rehab_exposure_event, resolve_rehab_completion
from api.contracts.rehab_evidence import read_exact_events
from api.contracts.rehab_schedule import schedule_rehab
from api.services.rehab_completion_service import session_rehab_items
from fightcamp.rehab_clinical import content_hash, load_clinical_policies, validate_clinical_bank
from fightcamp.rehab_protocols import get_rehab_bank
from tests.test_tendon_family_coverage import DAY, PLAN, injury
from tools.rehab_metadata_review_lib import source_hash
from tools.seed_impingement_family import PROFILES, REPAIRED

ROOT = Path(__file__).resolve().parents[1]
REGIONS = ["shoulder", "hip", "ankle", "elbow", "wrist"]
PAIRS = [(r, s) for r in REGIONS for s in (["calm"] if r == "wrist" else ["calm", "restore"])]


def read(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def resolve(row, **kwargs):
    return resolve_injury_policy(row, policies=load_clinical_policies(), bank=get_rehab_bank(), **kwargs)


@pytest.mark.parametrize("region,stage", PAIRS)
def test_exact_identity_reviewed_baseline_and_real_completion(region, stage):
    row = injury(region, "impingement", stage)
    decision = resolve(row)
    expected = "calm" if region == "wrist" else stage
    assert decision["outcome"] == "prescribed_rehab"
    assert decision["policy_id"] == f"{region}_impingement" and decision["stage"] == expected
    prescription = decision["prescription"]
    assert prescription["drill"]["rehab_stage"] == expected
    ledger = {r["drill_id"]: r for r in read("data/rehab_metadata_review.json")}
    assert ledger[prescription["drill_id"]]["review_state"] == "reviewed"
    assert prescription["bank_hash"] == content_hash(prescription["drill"])
    assert prescription["dose"] == {} and not prescription["is_loading"]
    assert schedule_rehab(row, decision, training_day=DAY, readiness_decision="pull_back")["state"] == "due"
    frozen = reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY)
    items = session_rehab_items({"id": PLAN}, training_day=DAY, session_id=frozen["session"]["session_id"], prescription=frozen)
    completion = dict(status="done", prescription_snapshot=frozen)
    eligible = resolve_rehab_completion(items, [row], completion=completion).eligible
    assert len(eligible) == 1
    event = build_rehab_exposure_event(eligible[0], athlete_id=row["athlete_id"], plan_id=PLAN,
        session_id=frozen["session"]["session_id"], training_day=DAY, completion=completion, during="same", limit="no")
    assert event.is_attributable_to(row) and event.dose_completed.completion_state == "performed_amount_unknown"


@pytest.mark.parametrize("region", REGIONS)
@pytest.mark.parametrize("kind", ["pain", "soreness", "tightness", "stiffness", "swelling", "tendonitis", "instability", "strain", "sprain"])
def test_other_types_never_consume_impingement_profiles(region, kind):
    policy = next(p for p in load_clinical_policies() if p.policy_id == f"{region}_impingement")
    assert resolve_injury_policy(injury(region, kind), policies=[policy], bank=get_rehab_bank())["prescription"] is None


@pytest.mark.parametrize("region", REGIONS)
def test_cross_region_stale_content_and_worsening(region):
    policy = next(p for p in load_clinical_policies() if p.policy_id == f"{region}_impingement")
    other = "hip" if region != "hip" else "shoulder"
    assert resolve_injury_policy(injury(other, "impingement"), policies=[policy], bank=get_rehab_bank())["prescription"] is None
    bank = deepcopy(get_rehab_bank())
    drill = next(d for g in bank for d in g["drills"] if d["id"] == policy.prescriptions[0].drill_id)
    drill["notes"] += " Unreviewed change."
    assert resolve_injury_policy(injury(region, "impingement"), policies=[policy], bank=bank)["prescription"] is None
    worse = resolve(injury(region, "impingement", "restore", latest_reported_status="worse"))
    assert worse["stage"] == "calm" and worse["prescription"]["drill"]["rehab_stage"] == "calm"


@pytest.mark.parametrize("region", REGIONS)
@pytest.mark.parametrize("changes", [dict(severity="severe"), dict(severity="high"), dict(injury_type="fracture"),
    dict(injury_type="dislocation"), dict(injury_type="tendon_rupture"), dict(description="suspected tendon rupture"),
    dict(description="complete ligament tear"), dict(description="full-thickness rotator cuff tear"),
    dict(description="major structural tear"), dict(injury_type="post_surgery"),
    dict(description="post-surgery repair"), dict(description="numbness and tingling"), dict(rehab_medical_gate=True)])
def test_structural_high_and_neurological_reports_stay_medical(region, changes):
    decision = resolve(injury(region, "impingement", **changes))
    assert decision["outcome"] == "medical_review" and decision["prescription"] is None


@pytest.mark.parametrize("region", REGIONS)
@pytest.mark.parametrize("stage", ["load", "dynamic", "return"])
def test_persisted_advanced_stage_and_dormant_content_cannot_open_ladder(region, stage):
    decision = resolve(injury(region, "impingement", "restore", rehab_stage=stage))
    assert decision["stage"] == "calm"
    assert decision["prescription"]["drill"]["rehab_stage"] == "calm"
    policy = next(p for p in load_clinical_policies() if p.policy_id == f"{region}_impingement")
    assert not any(t.promotable for t in policy.transitions)
    assert set(policy.live_stages) <= {"calm", "restore"}
    # Reviewing demand/function is insufficient: identities outside a profile
    # remain inventory even if they are given advanced metadata.
    bank = deepcopy(get_rehab_bank())
    active_ids = {p.drill_id for p in policy.prescriptions}
    for group in bank:
        if group["location"] == region and group["type"] == "impingement":
            for drill in group["drills"]:
                if drill["id"] not in active_ids:
                    drill.update(rehab_stage=stage, function="control", load="low", impact="none", velocity="low")
    base = "calm" if region == "wrist" else "restore"
    assert resolve_injury_policy(injury(region, "impingement", base), policies=[policy], bank=bank)["prescription"]["drill_id"] in active_ids


@pytest.mark.parametrize("region", REGIONS)
def test_unknown_side_recordable_without_capacity_and_ownership_cannot_cross_episode(region):
    row = injury(region, "impingement", "calm" if region == "wrist" else "restore", side="unknown")
    decision = resolve(row)
    assert decision["prescription"]["drill_id"] == f"{region}_impingement_recovery_support"
    frozen = reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY)
    items = session_rehab_items({"id": PLAN}, training_day=DAY, session_id=frozen["session"]["session_id"], prescription=frozen)
    completion = dict(status="done", prescription_snapshot=frozen)
    eligible = resolve_rehab_completion(items, [row], completion=completion).eligible
    assert len(eligible) == 1
    assert not resolve_rehab_completion(items, [injury(region, "impingement", side="unknown")], completion=completion).eligible
    event = build_rehab_exposure_event(eligible[0], athlete_id=row["athlete_id"], plan_id=PLAN,
        session_id=frozen["session"]["session_id"], training_day=DAY, completion=completion, during="same", limit="no")
    evidence, ignored = read_exact_events(athlete_id=row["athlete_id"], injury=row,
        exposure_rows=[dict(athlete_id=row["athlete_id"], event_json=event.model_dump(mode="json"))])
    assert event.side == "unknown" and not evidence and ignored["ignored_side_mismatch"] == 1


@pytest.mark.parametrize("region", ["groin", "glute", "obliques", "lower back", "upper back", "chest", "biceps", "triceps", "forearm", "hand", "fingers", "neck", "jaw", "face", "unspecified", "knee"])
def test_unprofiled_and_absent_regions_do_not_inherit_joint_work(region):
    assert resolve(injury(region, "impingement"))["prescription"] is None


def test_wrist_restore_is_explicitly_closed_despite_improvement():
    decision = resolve(injury("wrist", "impingement", "restore"))
    assert decision["outcome"] == "unsupported_prescription"
    assert decision["prescription"] is None and decision["reason_codes"] == ["stage_not_activated"]


@pytest.mark.parametrize("region", REGIONS)
def test_tolerated_exposures_and_complete_history_do_not_open_missing_transition(region):
    stage = "calm" if region == "wrist" else "restore"
    row = injury(region, "impingement", stage)
    decision = resolve(row)
    frozen = reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY)
    items = session_rehab_items({"id": PLAN}, training_day=DAY, session_id=frozen["session"]["session_id"], prescription=frozen)
    completion = dict(status="done", prescription_snapshot=frozen)
    candidate = resolve_rehab_completion(items, [row], completion=completion).eligible[0]
    event = build_rehab_exposure_event(candidate, athlete_id=row["athlete_id"], plan_id=PLAN,
        session_id=frozen["session"]["session_id"], training_day=DAY, completion=completion, during="better", limit="no")
    raw = event.model_dump(mode="json")
    raw["response"]["next_day_response"] = "same"
    reports = [dict(athlete_id=row["athlete_id"], event_json=raw)]
    for truncated in (False, True):
        assert resolve(row, exposures=reports, history_truncated=truncated)["stage"] == stage
    negative = deepcopy(reports)
    negative[0]["event_json"]["response"]["next_day_response"] = "worse"
    assert resolve(row, exposures=negative)["stage"] == "calm"


def test_original_audit_hashes_ids_and_review_history_preserved():
    audit = read("docs/impingement-family-bank-audit.json")
    assert len(audit) == 48 and len({r["region"] for r in audit}) == 20
    for row in audit:
        assert row["source_hash"] == source_hash(drill_id=row["drill_id"], location=row["bank_location"], injury_type="impingement", name=row["name"], notes=row["notes"])
    hashes = read("tests/fixtures/rehab_bank_before_impingement_hashes.json")
    from tests.rehab_inventory_history import inventory_with_history
    historical_bank, historical_ledger = inventory_with_history()
    bank = {d["id"]: d for g in historical_bank for d in g["drills"]}
    assert hashes.keys() <= bank.keys()
    family_ids = {row["drill_id"] for row in audit}
    assert {identity for identity in family_ids if content_hash(bank[identity]) != hashes[identity]} == REPAIRED
    current_family = {d["id"] for g in get_rehab_bank() if g["type"] == "impingement" for d in g["drills"]}
    assert len(current_family - hashes.keys()) == 6
    ledger = {r["drill_id"]: r for r in historical_ledger}
    active = {p.drill_id for policy in load_clinical_policies() if policy.injury_type == "impingement" for p in policy.prescriptions}
    assert len(active) == 9
    for row in audit:
        current = ledger[row["drill_id"]]
        if row["drill_id"] in REPAIRED:
            assert current["review_state"] == "reviewed"
            assert row["source_hash"] in {h["source_hash"] for h in current["source_history"]}
        else:
            assert current["review_state"] == "needs_review" and current["source_hash"] == row["source_hash"]
            assert row["drill_id"] not in active
    policies = tuple(p for p in load_clinical_policies() if p.injury_type == "impingement")
    assert validate_clinical_bank(policies, get_rehab_bank()) == []


def test_seed_bank_review_and_profiles_are_byte_idempotent(tmp_path, monkeypatch):
    from tools import seed_impingement_family
    from tools.generate_rehab_metadata_review import build_ledger
    folder = tmp_path / "data"
    folder.mkdir()
    names = ["rehab_bank.json", "rehab_metadata_review.json", "rehab_pathways.json"]
    from tools.rehab_metadata_review_lib import before_bank_publication
    # #2984 pins migration replay to its historical bank. Reconstruct only
    # hash-matched reviewed publication deltas; unrelated drift still fails.
    kinds = {"rehab_bank.json": "drills", "rehab_metadata_review.json": "reviews",
             "rehab_pathways.json": "profiles"}
    expected = {name: (json.dumps(before_bank_publication(read(f"data/{name}"), kinds[name]),
                                 indent=2, ensure_ascii=False) + "\n").encode("utf-8") for name in names}
    for name, data in expected.items():
        (folder / name).write_bytes(data)
    monkeypatch.setattr(seed_impingement_family, "ROOT", tmp_path)
    seed_impingement_family.main()
    seed_impingement_family.main()
    assert {name: (folder / name).read_bytes() for name in names} == expected
    assert build_ledger(json.loads(expected["rehab_bank.json"]), prior=json.loads(expected["rehab_metadata_review.json"])) == json.loads(expected["rehab_metadata_review.json"])
    assert set(PROFILES) == set(REGIONS)
