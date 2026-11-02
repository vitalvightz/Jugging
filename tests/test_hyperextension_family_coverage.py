"""Hyperextension profiles preserve the existing safety and ownership contracts."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tests.support import cover

from api.contracts.injury_policy import reconcile_session_prescription, resolve_injury_policy
from api.contracts.rehab_completion import build_rehab_exposure_event, resolve_rehab_completion
from api.contracts.rehab_evidence import read_exact_events
from api.contracts.rehab_schedule import schedule_rehab
from api.services.rehab_completion_service import session_rehab_items
from fightcamp.rehab_clinical import content_hash, load_clinical_policies, validate_clinical_bank
from fightcamp.rehab_protocols import get_rehab_bank
from tests.test_tendon_family_coverage import DAY, PLAN, injury
from tools.rehab_metadata_review_lib import source_hash
from tools.seed_hyperextension_family import PROFILES

ROOT = Path(__file__).resolve().parents[1]
REGIONS = ["toe", "fingers", "elbow", "wrist", "hand", "shoulder"]
PAIRS = [(r, "calm") for r in REGIONS]


def read(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def resolve(row, **kwargs):
    return resolve_injury_policy(row, policies=load_clinical_policies(), bank=get_rehab_bank(), **kwargs)


@pytest.mark.parametrize("region,stage", PAIRS)
def test_exact_identity_reviewed_baseline_and_real_completion(region, stage):
    row = injury(region, "hyperextension", stage)
    decision = resolve(row)
    expected = "calm"
    assert decision["outcome"] == "prescribed_rehab"
    assert decision["policy_id"] == f"{region}_hyperextension" and decision["stage"] == expected
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


@pytest.mark.parametrize("region,kind", cover(REGIONS, ["pain", "soreness", "tightness", "stiffness", "swelling", "tendonitis", "instability", "strain", "sprain", "impingement", "unspecified"]))
def test_other_types_never_consume_hyperextension_profiles(region, kind):
    policy = next(p for p in load_clinical_policies() if p.policy_id == f"{region}_hyperextension")
    assert resolve_injury_policy(injury(region, kind), policies=[policy], bank=get_rehab_bank())["prescription"] is None


@pytest.mark.parametrize("region", REGIONS)
def test_cross_region_stale_content_and_worsening(region):
    policy = next(p for p in load_clinical_policies() if p.policy_id == f"{region}_hyperextension")
    other = "hand" if region != "hand" else "wrist"
    assert resolve_injury_policy(injury(other, "hyperextension"), policies=[policy], bank=get_rehab_bank())["prescription"] is None
    bank = deepcopy(get_rehab_bank())
    drill = next(d for g in bank for d in g["drills"] if d["id"] == policy.prescriptions[0].drill_id)
    drill["notes"] += " Unreviewed change."
    assert resolve_injury_policy(injury(region, "hyperextension"), policies=[policy], bank=bank)["prescription"] is None
    worse = resolve(injury(region, "hyperextension", "restore", latest_reported_status="worse"))
    assert worse["stage"] == "calm" and worse["prescription"]["drill"]["rehab_stage"] == "calm"


@pytest.mark.parametrize("region,changes", cover(REGIONS, [dict(severity="severe"), dict(severity="high"), dict(injury_type="fracture"),
    dict(injury_type="dislocation"), dict(injury_type="tendon_rupture"), dict(description="suspected tendon rupture"),
    dict(description="complete ligament tear"), dict(description="full-thickness rotator cuff tear"),
    dict(description="major structural tear"), dict(injury_type="post_surgery"),
    dict(description="post-surgery repair"), dict(description="numbness and tingling"), dict(rehab_medical_gate=True),
    *[dict(description=text) for text in ["recurrent giving way", "joint locking", "suspected unstable joint",
        "vascular symptoms", "hand cold and blue", "major swelling", "obvious deformity",
        "unable to bear weight", "unable to use the joint", "significant functional loss",
        "neurological change", "head trauma", "joint feels unstable", "joint locks", "blue foot",
        "cannot put any weight on it", "major muscle tear", "subluxation", "ligament rupture"]]]))
def test_structural_high_and_neurological_reports_stay_medical(region, changes):
    decision = resolve(injury(region, "hyperextension", **changes))
    assert decision["outcome"] == "medical_review" and decision["prescription"] is None


@pytest.mark.parametrize("region", REGIONS)
@pytest.mark.parametrize("stage", ["load", "dynamic", "return"])
def test_persisted_advanced_stage_and_dormant_content_cannot_open_ladder(region, stage):
    decision = resolve(injury(region, "hyperextension", "restore", rehab_stage=stage))
    assert decision["stage"] == "calm"
    assert decision["prescription"]["drill"]["rehab_stage"] == "calm"
    policy = next(p for p in load_clinical_policies() if p.policy_id == f"{region}_hyperextension")
    assert not any(t.promotable for t in policy.transitions)
    assert set(policy.live_stages) <= {"calm", "restore"}
    # Reviewing demand/function is insufficient: identities outside a profile
    # remain inventory even if they are given advanced metadata.
    bank = deepcopy(get_rehab_bank())
    active_ids = {p.drill_id for p in policy.prescriptions}
    for group in bank:
        if group["location"] == region and group["type"] == "hyperextension":
            for drill in group["drills"]:
                if drill["id"] not in active_ids:
                    drill.update(rehab_stage=stage, function="control", load="low", impact="none", velocity="low",
                                 equipment=[], target_regions=[region], target_tissues=[f"{region} joint region"],
                                 laterality_applicability="side_specific", contraction_type="mixed",
                                 sport_specificity="general_rehab", contact_level="none")
    base = "calm"
    assert resolve_injury_policy(injury(region, "hyperextension", base), policies=[policy], bank=bank)["prescription"]["drill_id"] in active_ids


@pytest.mark.parametrize("region", REGIONS)
def test_unknown_side_recordable_without_capacity_and_ownership_cannot_cross_episode(region):
    row = injury(region, "hyperextension", "calm", side="unknown")
    decision = resolve(row)
    assert decision["prescription"]["drill_id"] == f"{region}_hyperextension_recovery_support"
    frozen = reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY)
    items = session_rehab_items({"id": PLAN}, training_day=DAY, session_id=frozen["session"]["session_id"], prescription=frozen)
    completion = dict(status="done", prescription_snapshot=frozen)
    eligible = resolve_rehab_completion(items, [row], completion=completion).eligible
    assert len(eligible) == 1
    assert not resolve_rehab_completion(items, [injury(region, "hyperextension", side="unknown")], completion=completion).eligible
    event = build_rehab_exposure_event(eligible[0], athlete_id=row["athlete_id"], plan_id=PLAN,
        session_id=frozen["session"]["session_id"], training_day=DAY, completion=completion, during="same", limit="no")
    evidence, ignored = read_exact_events(athlete_id=row["athlete_id"], injury=row,
        exposure_rows=[dict(athlete_id=row["athlete_id"], event_json=event.model_dump(mode="json"))])
    assert event.side == "unknown" and not evidence and ignored["ignored_side_mismatch"] == 1


@pytest.mark.parametrize("region", ["groin", "glute", "obliques", "lower back", "upper back", "chest", "biceps", "triceps", "forearm", "neck", "jaw", "face", "unspecified", "knee", "ankle", "hip"])
def test_unprofiled_and_absent_regions_do_not_inherit_joint_work(region):
    assert resolve(injury(region, "hyperextension"))["prescription"] is None


@pytest.mark.parametrize("region", REGIONS)
def test_restore_is_explicitly_closed_despite_improvement(region):
    decision = resolve(injury(region, "hyperextension", "restore"))
    assert decision["outcome"] == "unsupported_prescription"
    assert decision["prescription"] is None and decision["reason_codes"] == ["stage_not_activated"]


@pytest.mark.parametrize("region", REGIONS)
def test_clearance_camp_and_time_do_not_establish_stability_or_open_restore(region):
    for stage in ("calm", "restore"):
        row = injury(region, "hyperextension", stage, created_at="2024-01-01T00:00:00Z")
        row["clinician_clearance"] = dict(episode_id=row["episode_id"], scopes=["rehab", "training", "contact"])
        for phase in ("GPP", "SPP", "TAPER"):
            decision = resolve(row, phase=phase, readiness_decision="train_as_planned")
            assert decision["stage"] == stage
            if stage == "calm":
                assert decision["prescription"]["drill_id"] == f"{region}_hyperextension_recovery_support"
            else:
                assert decision["prescription"] is None and decision["reason_codes"] == ["stage_not_activated"]


@pytest.mark.parametrize("region", REGIONS)
def test_new_instability_holds_a_frozen_prescription_despite_clearance(region):
    row = injury(region, "hyperextension")
    decision = resolve(row)
    frozen = reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY, injuries=[row])
    row.update(description="recurrent giving way", clinician_clearance=dict(
        episode_id=row["episode_id"], scopes=["rehab", "training", "contact"]))
    current = resolve(row)
    assert current["outcome"] == "medical_review"
    held = reconcile_session_prescription(None, decisions=[current], plan_id=PLAN, training_day=DAY,
                                         frozen=frozen, injuries=[row])
    assert held["safety_hold"]


@pytest.mark.parametrize("kind", ["strain", "sprain", "instability", "tendonitis", "impingement"])
def test_additional_trauma_screen_does_not_change_previous_family_stage_rules(kind):
    from api.contracts.rehab_stage import resolve_rehab_stage
    # These phrases were not canonical urgent tokens before this rollout.
    # Only the hyperextension presentation receives the additional screen.
    for text in ("recurrent giving way", "joint locking", "vascular symptoms", "major swelling"):
        assert not resolve_rehab_stage(injury("wrist", kind, description=text)).medical_gate


@pytest.mark.parametrize("region", REGIONS)
def test_tolerated_exposures_and_complete_history_do_not_open_missing_transition(region):
    stage = "calm"
    row = injury(region, "hyperextension", stage)
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
    audit = read("docs/hyperextension-family-bank-audit.json")
    assert len(audit) == 42 and len({r["region"] for r in audit}) == 17
    for row in audit:
        assert row["source_hash"] == source_hash(drill_id=row["drill_id"], location=row["bank_location"], injury_type="hyperextension", name=row["name"], notes=row["notes"])
    hashes = read("tests/fixtures/rehab_bank_before_hyperextension_hashes.json")
    from tests.rehab_inventory_history import inventory_with_history
    historical_bank, historical_ledger = inventory_with_history()
    bank = {d["id"]: d for g in historical_bank for d in g["drills"]}
    assert hashes.keys() <= bank.keys()
    family_ids = {row["drill_id"] for row in audit}
    assert {identity for identity in family_ids if content_hash(bank[identity]) != hashes[identity]} == set()
    current_family = {d["id"] for g in get_rehab_bank() if g["type"] == "hyperextension" for d in g["drills"]}
    assert current_family - hashes.keys() == {f"{r}_hyperextension_recovery_support" for r in REGIONS}
    ledger = {r["drill_id"]: r for r in historical_ledger}
    active = {p.drill_id for policy in load_clinical_policies() if policy.injury_type == "hyperextension" for p in policy.prescriptions}
    assert len(active) == 6
    for row in audit:
        current = ledger[row["drill_id"]]
        assert current["review_state"] == "needs_review" and current["source_hash"] == row["source_hash"]
        assert row["drill_id"] not in active
    policies = tuple(p for p in load_clinical_policies() if p.injury_type == "hyperextension")
    assert validate_clinical_bank(policies, get_rehab_bank()) == []


def test_seed_bank_review_and_profiles_are_byte_idempotent(tmp_path, monkeypatch):
    from tools import seed_hyperextension_family
    from tools.generate_rehab_metadata_review import build_ledger
    folder = tmp_path / "data"
    folder.mkdir()
    names = ["rehab_bank.json", "rehab_metadata_review.json", "rehab_pathways.json"]
    expected = {name: (ROOT / "data" / name).read_bytes() for name in names}
    for name, data in expected.items():
        (folder / name).write_bytes(data)
    monkeypatch.setattr(seed_hyperextension_family, "ROOT", tmp_path)
    seed_hyperextension_family.main()
    seed_hyperextension_family.main()
    assert {name: (folder / name).read_bytes() for name in names} == expected
    assert build_ledger(json.loads(expected["rehab_bank.json"]), prior=json.loads(expected["rehab_metadata_review.json"])) == json.loads(expected["rehab_metadata_review.json"])
    assert set(PROFILES) == set(REGIONS)
