"""Regional ligament profiles use shared safety, scheduling and completion."""
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import pytest

from tests.support import cover

from api.contracts.injury_policy import rehab_allocation_count, reconcile_session_prescription, resolve_injury_policy
from api.contracts.rehab_completion import build_rehab_exposure_event, resolve_rehab_completion
from api.contracts.rehab_evidence import read_exact_events
from api.contracts.rehab_schedule import schedule_rehab
from api.services.rehab_completion_service import session_rehab_items
from fightcamp.rehab_clinical import content_hash, load_clinical_policies, validate_clinical_bank
from fightcamp.rehab_protocols import get_rehab_bank
from fightcamp.rehab_protocols import rehab_drill_options_for_phase
from tools.rehab_metadata_review_lib import source_hash

ROOT = Path(__file__).resolve().parents[1]
PAIRS = [("ankle", "instability"), ("knee", "instability"), ("toe", "sprain"),
         ("wrist", "sprain"), ("elbow", "sprain"), ("shoulder", "sprain"),
         ("shoulder", "instability"), ("hand", "sprain"), ("fingers", "sprain")]
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
    assert prescription["drill_id"] == f"{region}_{kind}_" + ("recovery_support" if stage == "calm" else "reviewed_restore")
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
    from tests.rehab_inventory_history import inventory_with_history
    _, historical_ledger = inventory_with_history()
    ledger = {r["drill_id"]: r for r in historical_ledger}
    policy = next(p for p in load_clinical_policies() if (p.region, p.injury_type) == (region, kind))
    assert validate_clinical_bank((policy,), get_rehab_bank()) == []
    current = {r["drill_id"]: r for r in json.loads((ROOT / "data/rehab_metadata_review.json").read_text())}
    for p in policy.prescriptions:
        # Live prescriptions are checked against the current ledger; the dated audit below uses history.
        record = current[p.drill_id]
        assert record["review_state"] == "reviewed"
        assert record["source_hash"] == source_hash(drill_id=record["drill_id"], location=record["location"],
            injury_type=record["injury_type"], name=record["name"], notes=record["notes"])
    before = json.loads((ROOT / "docs/sprain-family-bank-audit.json").read_text())
    for original in (r for r in before if (r["region"], r["injury_type"]) == (region, kind)):
        record = ledger[original["drill_id"]]
        assert record["review_state"] == "reviewed"
        assert original["source_hash"] in {record["source_hash"], *(r["source_hash"] for r in record.get("source_history", []))}
        assert "GPP:" not in record["notes"] and "SPP:" not in record["notes"]


@pytest.mark.parametrize("region,kind", PAIRS)
def test_cross_region_or_type_and_unreviewed_content_fail_closed(region, kind):
    policy = next(p for p in load_clinical_policies() if (p.region, p.injury_type) == (region, kind))
    for other in [injury("elbow" if region != "elbow" else "ankle", kind),
                  injury(region, "instability" if kind == "sprain" else "sprain")]:
        assert resolve_injury_policy(other, policies=(policy,), bank=get_rehab_bank())["prescription"] is None
    changed = deepcopy(get_rehab_bank())
    drill = next(d for g in changed for d in g["drills"] if d["id"] == policy.prescriptions[0].drill_id)
    drill["notes"] += " Unreviewed change."
    assert resolve_injury_policy(injury(region, kind), policies=(policy,), bank=changed)["prescription"] is None


@pytest.mark.parametrize("region,kind", PAIRS)
def test_worsening_returns_to_protection_and_readiness_holds_loaded_restore(region, kind):
    decision = resolve(injury(region, kind, "restore", latest_reported_status="worse"))
    assert decision["stage"] == "calm" and decision["prescription"]["drill"]["rehab_stage"] == "calm"
    row = injury(region, kind, "restore")
    decision = resolve(row)
    if decision["prescription"]["is_loading"]:
        assert schedule_rehab(row, decision, training_day=DAY, readiness_decision="pull_back")["state"] == "due"


@pytest.mark.parametrize("region,kind,changes", cover(PAIRS, [dict(severity="severe"), dict(description="grade 3 sprain"),
    dict(injury_type="ligament_tear", description="torn ligament"), dict(description="ACL tear"),
    dict(description="suspected fracture"), dict(description="dislocated joint"), dict(description="subluxation"),
    dict(description="numbness and tingling"), dict(rehab_medical_gate=True),
    dict(injury_type="post_surgery", description="post surgery")]))
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


@pytest.mark.parametrize("region,kind", [("knee", "sprain"), ("wrist", "instability"), ("hand", "instability"),
    ("fingers", "instability"), ("foot", "instability"), ("chest", "sprain"), ("lower back", "sprain"),
    ("biceps", "sprain"), ("jaw", "sprain"), ("eye", "instability"), ("face", "instability")])
def test_missing_or_uncertain_diagnoses_do_not_inherit_another_profile(region, kind):
    decision = resolve(injury(region, kind))
    assert decision["prescription"] is None and decision["outcome"] == "unsupported_prescription"


def test_whole_family_audit_covers_the_historical_bank():
    audit = json.loads((ROOT / "docs/sprain-family-bank-audit.json").read_text())
    assert len(audit) == 117 and len({r["bank_location"] for r in audit}) == 25
    from tests.rehab_inventory_history import inventory_with_history
    historical_bank, _ = inventory_with_history()
    bank_ids = {d["id"] for g in historical_bank for d in g["drills"]}
    assert {r["drill_id"] for r in audit} <= bank_ids


def test_existing_ankle_bundle_and_new_wrist_prescription_are_two_allocations():
    decisions = [resolve(injury("ankle", "sprain", "restore")), resolve(injury("wrist", "sprain", "restore"))]
    frozen = reconcile_session_prescription(None, decisions=decisions, plan_id=PLAN, training_day=DAY)
    blocks = frozen["session"]["blocks"]
    assert rehab_allocation_count(blocks) == 2
    assert len(blocks) == 3


@pytest.mark.parametrize("region", ["knee", "wrist", "shoulder"])
def test_profiles_preserve_other_inventory_and_require_episode_selection(region):
    before = json.loads((ROOT / "tests/fixtures/rehab_bank_before_nonspecific_hashes.json").read_text())
    originals = {identity for identity in before if identity.startswith(f"{region}_pain_")}
    current = {d["id"] for g in get_rehab_bank() if g["type"] == "pain" and g["location"] == region for d in g["drills"]}
    from tests.rehab_inventory_history import inventory_with_history
    historical_bank, _ = inventory_with_history()
    preserved = {d['id'] for g in historical_bank if g['type'] == 'pain' and g['location'] == region for d in g['drills']}
    assert originals and originals <= preserved
    assert current <= preserved
    # Pain now has its own reviewed profile; neither active type can bypass
    # current episode/stage selection through the legacy bank-only lookup.
    assert rehab_drill_options_for_phase("pain", region, "GPP", limit=6) == []
    kind = "instability" if region == "knee" else "sprain"
    assert rehab_drill_options_for_phase(kind, region, "GPP", limit=6) == []
