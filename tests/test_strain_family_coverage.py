"""Production strain profiles share the existing resolver and completion path."""
from copy import deepcopy
import json
from pathlib import Path
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
REGIONS = ["hamstring", "calf", "groin", "quads", "biceps", "triceps", "shoulder"]
RESTORE_IDS = {
    "hamstring": "hamstrings_strain_isometric_hamstring_bridge",
    "calf": "calf_strain_double_leg_calf_raises", "groin": "groin_unspecified_isometric_adductor_squeeze_ball_between_knees",
}
DAY = "2026-10-03"
PLAN = str(uuid4())


def injury(region, stage="calm", **changes):
    row = dict(id=str(uuid4()), episode_id=str(uuid4()), athlete_id=str(uuid4()),
               body_region=region, canonical_location=region, body_area=f"Left {region}",
               description=f"{region} strain", side="left", injury_type="strain", severity="mild",
               status="open", created_at="2026-10-01T00:00:00Z", updated_at="2026-10-01T00:00:00Z")
    if stage == "restore":
        row.update(status="monitoring", latest_reported_status="improving", rehab_stage="restore")
    row.update(changes)
    return row


def resolve(row, **kwargs):
    return resolve_injury_policy(row, policies=load_clinical_policies(), bank=get_rehab_bank(), **kwargs)


@pytest.mark.parametrize("region", REGIONS)
@pytest.mark.parametrize("stage", ["calm", "restore"])
def test_correct_region_strain_resolves_reviewed_stage_and_real_completion(region, stage):
    row = injury(region, stage)
    decision = resolve(row)
    assert decision["outcome"] == "prescribed_rehab"
    assert decision["policy_id"] == region + "_strain" and decision["stage"] == stage
    current = decision["prescription"]
    assert current["drill_id"] == (region + "_strain_recovery_support" if stage == "calm"
                                   else RESTORE_IDS.get(region, region + "_strain_reviewed_restore"))
    assert current["bank_hash"] == content_hash(current["drill"])
    assert current["dose"] == {} and current["minimum_gap_days"] == 1
    frozen = reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY)
    assert rehab_allocation_count(frozen["session"]["blocks"]) == 1
    items = session_rehab_items({"id": PLAN}, training_day=DAY, session_id=frozen["session"]["session_id"], prescription=frozen)
    completion = dict(status="done", rehab_performance="done_as_shown", prescription_snapshot=frozen)
    resolutions = resolve_rehab_completion(items, [row], completion=completion)
    assert len(resolutions.eligible) == 1
    event = build_rehab_exposure_event(resolutions.eligible[0], athlete_id=row["athlete_id"], plan_id=PLAN,
                                     session_id=frozen["session"]["session_id"], training_day=DAY,
                                     completion=completion, during="same", limit="no")
    assert event.is_attributable_to(row)
    assert event.dose_completed.completion_state == "performed_amount_unknown"
    assert event.dose_completed.reps is None


@pytest.mark.parametrize("region", REGIONS)
def test_live_identities_have_current_review_and_authorised_edits_keep_source_history(region):
    from tests.rehab_inventory_history import inventory_with_history
    _, historical_ledger = inventory_with_history()
    # #2984 publishes compatible live options; use their current reviews.
    ledger = {r["drill_id"]: r for r in json.loads(
        (ROOT / "data/rehab_metadata_review.json").read_text(encoding="utf-8"))}
    bank = get_rehab_bank()
    policy = next(p for p in load_clinical_policies() if p.region == region)
    assert validate_clinical_bank((policy,), bank) == []
    for prescription in policy.prescriptions:
        record = ledger[prescription.drill_id]
        assert record["review_state"] == "reviewed"
        assert record["source_hash"] == source_hash(drill_id=record["drill_id"], location=record["location"],
            injury_type=record["injury_type"], name=record["name"], notes=record["notes"])
    ledger = {r["drill_id"]: r for r in historical_ledger}
    before = json.loads((ROOT / "docs/strain-family-pre-rollout-audit.json").read_text(encoding="utf-8-sig"))
    for original in before[region]["drills"]:
        record = ledger[original["drill_id"]]
        assert record["review_state"] == "reviewed"
        assert original["source_hash"] in {record["source_hash"],
            *(r["source_hash"] for r in record.get("source_history", []))}
        assert "GPP:" not in record["notes"] and "SPP:" not in record["notes"]


@pytest.mark.parametrize("region", REGIONS)
def test_other_region_cannot_consume_profile_drills_and_stale_content_fails_closed(region):
    policy = next(p for p in load_clinical_policies() if p.region == region)
    other = REGIONS[(REGIONS.index(region) + 1) % len(REGIONS)]
    result = resolve_injury_policy(injury(other), policies=(policy,), bank=get_rehab_bank())
    assert result["prescription"] is None and result["outcome"] == "unsupported_prescription"
    changed = deepcopy(get_rehab_bank())
    drill = next(d for g in changed for d in g["drills"] if d["id"] == policy.prescriptions[0].drill_id)
    drill["notes"] += " Unreviewed change."
    assert resolve_injury_policy(injury(region), policies=(policy,), bank=changed)["prescription"] is None


@pytest.mark.parametrize("region", REGIONS)
def test_worsening_demotes_to_calm_and_readiness_holds_loading(region):
    decision = resolve(injury(region, "restore", latest_reported_status="worse"))
    assert decision["stage"] == "calm" and decision["prescription"]["drill"]["rehab_stage"] == "calm"
    row = injury(region, "restore")
    decision = resolve(row)
    if decision["prescription"]["is_loading"]:
        assert schedule_rehab(row, decision, training_day=DAY, readiness_decision="pull_back")["state"] == "due"


@pytest.mark.parametrize("region", REGIONS)
@pytest.mark.parametrize("changes", [dict(severity="severe"), dict(severity="high"),
    dict(injury_type="ligament_tear", description="ligament tear"), dict(injury_type="muscle_rupture", description="muscle rupture"),
    dict(rehab_medical_gate=True), dict(description="suspected major tear"),
    dict(description="complete muscle tear"), dict(description="complete tear"),
    dict(description="full-thickness tear"), dict(description="numbness and tingling")])
def test_severe_and_structural_reports_remain_medical_review(region, changes):
    decision = resolve(injury(region, **changes))
    assert decision["outcome"] == "medical_review" and decision["prescription"] is None


@pytest.mark.parametrize("region", REGIONS)
@pytest.mark.parametrize("stage", ["load", "dynamic", "return"])
def test_no_profile_can_enter_non_live_advanced_stage(region, stage):
    decision = resolve(injury(region, "restore", rehab_stage=stage))
    # Persisted or externally supplied stages cannot bypass the baseline ladder.
    assert decision["stage"] in {"calm", "restore"}
    assert decision["prescription"]["drill"]["rehab_stage"] in {"calm", "restore"}
    policy = next(p for p in load_clinical_policies() if p.region == region)
    assert policy.live_stages == ["calm", "restore"] and not any(t.promotable for t in policy.transitions)


@pytest.mark.parametrize("region", REGIONS)
def test_unknown_side_baseline_guidance_is_recordable_but_not_capacity_evidence(region):
    row = injury(region, side="unknown")
    frozen = reconcile_session_prescription(None, decisions=[resolve(row)], plan_id=PLAN, training_day=DAY)
    items = session_rehab_items({"id": PLAN}, training_day=DAY, session_id=frozen["session"]["session_id"], prescription=frozen)
    completion = dict(status="done", prescription_snapshot=frozen)
    result = resolve_rehab_completion(items, [row], completion=completion)
    assert len(result.eligible) == 1
    event = build_rehab_exposure_event(result.eligible[0], athlete_id=row["athlete_id"], plan_id=PLAN,
        session_id=frozen["session"]["session_id"], training_day=DAY, completion=completion, during="worse", limit="stopped")
    assert event.side == "unknown" and event.is_attributable_to(row)
    evidence, ignored = read_exact_events(athlete_id=row["athlete_id"], injury=row,
        exposure_rows=[dict(athlete_id=row["athlete_id"], event_json=event.model_dump(mode="json"))])
    assert evidence == [] and ignored["ignored_side_mismatch"] == 1
    decision = resolve(row, exposures=[dict(athlete_id=row["athlete_id"], event_json=event.model_dump(mode="json"))])
    assert decision["stage"] == "calm"
    # Removing frozen ownership keeps ordinary unknown-side legacy work ineligible.
    legacy = deepcopy(items[0])
    legacy.pop("prescription_policy_id")
    assert not resolve_rehab_completion([legacy], [row], completion=completion).eligible


@pytest.mark.parametrize("region", ["neck", "eye", "jaw", "toe"])
def test_unreviewed_regions_stay_unsupported(region):
    assert resolve(injury(region))["outcome"] == "unsupported_prescription"


@pytest.mark.parametrize("region", ["groin", "quads", "biceps", "triceps", "shoulder"])
def test_unknown_side_restore_uses_only_attributable_calm_fallback(region):
    decision = resolve(injury(region, "restore", side="unknown"))
    assert decision["prescription"]["drill_id"] == f"{region}_strain_recovery_support"
    assert decision["prescription"]["drill"]["laterality_applicability"] == "not_applicable"
    assert decision["prescription"]["drill"]["rehab_stage"] == "calm"


def test_quadriceps_restrictions_cover_existing_mechanical_region_spellings():
    row = injury("quads")
    decision = resolve(row)
    assert {"quads", "quad", "knee"} <= set(decision["restrictions"]["blocked_regions"])
    for region in ("quads", "quad", "knee"):
        session = dict(session_id="training", session_type="strength", blocks=[
            dict(block_id="heavy", block_type="strength", mechanical_load_regions=[region], contact_level="none")])
        frozen = reconcile_session_prescription(session, decisions=[decision], plan_id=PLAN, training_day=DAY)
        assert not any(b.get("block_id") == "heavy" for b in frozen["session"]["blocks"])
