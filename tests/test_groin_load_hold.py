"""Closed groin pilot: normal RESTORE evidence must not activate dormant LOAD.

These tests pin the deliberately unactivated outcome. They do not simulate a
clinical approval, test muscle strength, or assert new subtype discrimination.
"""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from api.contracts.injury_policy import reported_load_option, resolve_injury_policy
from api.contracts.rehab_progression import evaluate_transition
from api.services.injury_episode_service import (
    InjuryEpisodeObservation, apply_episode_observations, exposure_rows_with_observations,
    record_episode_observation,
)
from api.services.rehab_completion_service import (
    build_rehab_response_contexts, record_reported_permission_rehab,
)
from fightcamp.rehab_clinical import content_hash, load_clinical_policies
from fightcamp.rehab_protocols import get_rehab_bank
from tests.support import FakeStore
from tests.test_achilles_load_activation import DAY, NOW, execute, report_permission, view
from tests.test_rehab_transition_engine import event

ROOT = Path(__file__).resolve().parents[1]
POLICY = next(p for p in load_clinical_policies() if p.policy_id == "groin_strain")
RESTORE = "groin_strain_side_lying_hip_adduction"
CABLE = "groin_strain_standing_cable_adduction"
# Metadata-contract tests intentionally mutate then replace the cached bank.
# Keep this historical expectation independent of their discarded object graph.
DRILL = deepcopy(next(d for g in get_rehab_bank() for d in g["drills"] if d["id"] == RESTORE))


def bundle(*, description="strain. Clinician diagnosed uncomplicated acute adductor muscle strain",
           permission="loading", severity="moderate", equipment=("cable_machine",)):
    store, athlete, plan = FakeStore(), str(uuid4()), str(uuid4())
    store.intakes[athlete] = [dict(id=str(uuid4()), athlete_id=athlete, equipment_access=list(equipment))]
    injury = store.create_injury_flag(athlete, dict(body_area="Left groin", description=description,
        severity=severity, status="monitoring", latest_reported_status="improving"))
    from api.contracts.rehab_exposure import injury_evidence_identity
    injury.update(**injury_evidence_identity(injury["body_area"], injury["description"]),
                  created_at="2026-09-01T00:00:00Z", updated_at="2026-09-01T00:00:00Z")
    store.injury_flags[athlete][0].update(injury)
    opening = dict(id=str(uuid4()), athlete_id=athlete, injury_id=injury["id"], injury_episode_id=injury["episode_id"],
        event_type="injury_checkin", created_at="2026-09-01T00:00:00Z",
        payload=dict(explicit_report=True, latest_reported_status="improving"))
    store.injury_episode_events = {opening["id"]: opening}
    exposure = event(drill=DRILL, policy_id=POLICY.policy_id, athlete=athlete, episode=injury["episode_id"],
                     bank_hash=POLICY.prescriptions[1].bank_hash)
    exposure.update(injury_id=injury["id"], injury_episode_id=injury["episode_id"])
    exposure["event_json"].update(injury_id=injury["id"], body_region="groin")
    exposure["event_json"]["demand"]["target_regions"] = ["groin"]
    store.rehab_exposures[exposure["id"]] = exposure
    store.plans[plan] = dict(id=plan, athlete_id=athlete, intake_id=store.intakes[athlete][0]["id"], status="ready",
        created_at="2026-09-01T00:00:00Z", structured_plan={"weeks": [{"phase_label": "GPP", "days": [{"date": DAY,
        "day_type": "strength", "sessions": [dict(session_id="training", session_type="strength", title="Strength",
        blocks=[dict(block_id="training", block_type="strength", display_name="Leg work",
                     mechanical_load_regions=["groin"], contact_level="none")])]}]}]})
    store.set_active_plan_id(athlete, plan)
    store.upsert_today_checkin(athlete, dict(plan_id=plan, training_day=DAY,
        recommendation_state="train_as_planned", pain="none", body="good"))
    result = store, None, None, athlete, plan, injury
    report_permission(result, permission)
    return result


def decide(b, **kwargs):
    row = apply_episode_observations(b[0].injury_flags[b[3]][0], list(b[0].injury_episode_events.values()), as_of=NOW)
    return resolve_injury_policy(row, policies=[POLICY], bank=kwargs.pop("bank", get_rehab_bank()),
        equipment=kwargs.pop("equipment", ["cable_machine"]), exposures=list(b[0].rehab_exposures.values()),
        as_of=NOW, **kwargs)


def assert_closed(decision):
    assert decision["stage"] not in {"load", "dynamic", "return"}
    prescription = decision.get("prescription")
    assert not prescription or prescription["drill_id"] in {RESTORE, "groin_strain_recovery_support"}


def test_even_apparently_ready_athlete_has_no_clinical_transition_or_load_prescription():
    b = bundle()
    current = view(b)
    decision = current.open_injuries[0]["rehab_decision"]
    assert decision["stage"] == "restore" and decision["prescription"]["drill_id"] == RESTORE
    transition = decision["progression"]["next_transition"]
    assert not transition["target_stage_live"]
    assert transition["reason_codes"] == ["no_clinical_criteria_declared"]
    # A real reviewed exposure and both responses can satisfy the safety
    # baseline. That still cannot manufacture a clinical readiness criterion.
    assert all(r["status"] == "pass" for r in transition["requirements"])
    assert POLICY.version == 1 and POLICY.live_stages == ["calm", "restore"]
    assert not any(t.promotable for t in POLICY.transitions)
    assert reported_load_option("groin_strain") is None
    assert current.live_prescription["session"]["blocks"][0]["rehab_drill_id"] == RESTORE


@pytest.mark.parametrize("description", [
    "strain", "strain. Not sure which tissue", "strain. Conflicting diagnoses",
    "strain. Iliopsoas injury", "strain. Hip flexor injury", "strain. Suspected hernia",
    "strain. Inguinal-related pain", "strain. Hip joint pain", "strain. Labral injury",
    "strain. Pubic-related pain", "strain. Tendon injury", "strain. Suspected avulsion",
    "strain. Significant tear", "strain. Unexplained loss of function", "strain. Numbness",
    "strain. Chronic recurring groin pain",
])
def test_unknown_nonadductor_and_conflicting_presentations_cannot_gain_load(description):
    # Closure, not new diagnostic routing. Baseline indication is not asserted.
    assert_closed(decide(bundle(description=description)))


@pytest.mark.parametrize("permission", [None, "not_cleared", "gentle_recovery", "loading", "sport_specific"])
def test_rehab_and_contact_permission_never_activates_an_unreviewed_option(permission):
    b = bundle(permission=permission)
    report_permission(b, permission, scopes=["rehab", "training", "contact"])
    assert_closed(decide(b))


@pytest.mark.parametrize("mutation", ["wrong_athlete", "wrong_episode", "wrong_side", "wrong_injury",
    "missing_work", "modified_work", "unreviewed_work", "unknown_demand", "unknown_during", "unknown_next_day",
    "worse_during", "worse_next_day", "truncated_history", "medical_hold", "restriction_hold", "worse",
    "severe", "unknown_severity", "missing_equipment"])
def test_missing_or_adverse_information_cannot_open_load(mutation):
    b = bundle()
    row = b[0].injury_flags[b[3]][0]
    exposure = next(iter(b[0].rehab_exposures.values()))
    payload = exposure["event_json"]
    options = {}
    if mutation.startswith("wrong_"):
        field = {"wrong_athlete": "athlete_id", "wrong_episode": "injury_episode_id",
                 "wrong_side": "side", "wrong_injury": "injury_id"}[mutation]
        value = "right" if field == "side" else str(uuid4())
        exposure[field] = value
        if field != "athlete_id":
            payload[field] = value
    elif mutation == "missing_work":
        b[0].rehab_exposures.clear()
    elif mutation == "modified_work":
        payload["dose_completed"].update(completion_state="partial_unknown", stopped_early=True)
    elif mutation == "unreviewed_work":
        payload["provenance"]["bank_hash"] = "0" * 64
    elif mutation == "unknown_demand":
        payload["demand"]["load"] = "unknown"
    elif mutation in {"unknown_during", "unknown_next_day", "worse_during", "worse_next_day"}:
        field = "during_response" if mutation.endswith("during") else "next_day_response"
        payload["response"][field] = "worse" if mutation.startswith("worse") else "not_reported"
    elif mutation == "truncated_history":
        options["history_truncated"] = True
    elif mutation in {"medical_hold", "restriction_hold"}:
        row[mutation] = True
    elif mutation == "worse":
        row["latest_reported_status"] = "worse"
    elif mutation in {"severe", "unknown_severity"}:
        row["severity"] = "severe" if mutation == "severe" else "unknown"
    elif mutation == "missing_equipment":
        options["equipment"] = []
    assert_closed(decide(b, **options))


def test_missing_or_altered_bank_content_cannot_schedule_cable():
    b = bundle()
    for remove, alter in [(CABLE, False), (RESTORE, False), (RESTORE, True)]:
        bank = deepcopy(get_rehab_bank())
        for group in bank:
            if alter:
                for drill in group["drills"]:
                    if drill["id"] == remove:
                        drill["notes"] += " Unreviewed change."
            else:
                group["drills"] = [d for d in group["drills"] if d["id"] != remove]
        assert_closed(decide(b, bank=bank))


@pytest.mark.parametrize("target", ["load", "dynamic", "return"])
def test_all_advanced_stages_remain_unavailable(target):
    b = bundle(permission="sport_specific")
    row = b[0].injury_flags[b[3]][0]
    transition = next(t for t in POLICY.transitions if t.to_stage == target)
    result = evaluate_transition(transition, policy=POLICY, injury=row,
        exposures=list(b[0].rehab_exposures.values()), as_of=NOW)
    assert result["status"] != "met" and result["target_stage_live"] is False


@pytest.mark.parametrize("other,kind", [("Right hip", "strain"), ("Right groin", "strain"),
    ("Pelvis", "pain"), ("Left knee", "sprain"), ("Right ankle", "sprain"), ("Right wrist", "contusion")])
def test_relevant_and_unrelated_other_injuries_never_borrow_a_groin_load_option(other, kind):
    b = bundle()
    b[0].create_injury_flag(b[3], dict(body_area=other, description=kind, severity="mild", status="open"))
    current = view(b)
    decision = next(r for r in current.open_injuries if r["id"] == b[5]["id"])["rehab_decision"]
    assert_closed(decision)
    # No new blanket multi-injury veto was introduced into baseline selection.
    assert decision["prescription"]["drill_id"] == RESTORE


@pytest.mark.parametrize("performance", ["done_as_shown", "changed", "skipped"])
def test_normal_restore_completion_responses_and_next_day_never_manufacture_load(performance):
    b = bundle()
    b[0].rehab_exposures.clear()
    live = view(b).live_prescription
    execute(b, live)
    completion = execute(b, live, "done", rehab_performance=performance, rehab_tracking="injury_checkin")
    _, contexts = build_rehab_response_contexts(b[0], athlete_id=b[3], plan_row=b[0].plans[b[4]],
        training_day=DAY, session_id=completion["session_id"], completion=completion)
    completion.update(b[0].initialize_session_completion_rehab_contexts(b[3], completion_id=completion["id"],
        plan_id=b[4], session_id=completion["session_id"], training_day=DAY, contexts=contexts))
    record_reported_permission_rehab(b[0], athlete_id=b[3], plan_row=b[0].plans[b[4]], completion=completion)
    if performance == "skipped":
        assert not b[0].rehab_exposures
        return
    for exposure in b[0].rehab_exposures.values():
        exposure["created_at"] = NOW.isoformat()
        assert exposure["event_json"]["response"]["during_response"] == "not_reported"
        assert exposure["event_json"]["drill_id"] == RESTORE
        assert exposure["event_json"]["provenance"]["bank_hash"] == content_hash(DRILL)
    checkin = dict(id=str(uuid4()), athlete_id=b[3], injury_id=b[5]["id"], injury_episode_id=b[5]["episode_id"],
        event_type="injury_checkin", created_at=(NOW + timedelta(minutes=1)).isoformat(),
        payload=dict(explicit_report=True, latest_reported_status="improving"))
    b[0].injury_episode_events[checkin["id"]] = checkin
    for exposure in b[0].rehab_exposures.values():
        response = record_episode_observation(b[0], athlete_id=b[3],
            observation=InjuryEpisodeObservation(injury_id=b[5]["id"], injury_episode_id=b[5]["episode_id"],
                event_type="delayed_rehab_response", exposure_id=exposure["id"], response="same"),
            training_day=(NOW + timedelta(days=1)).date().isoformat())
        response["created_at"] = (NOW + timedelta(days=1)).isoformat()
        b[0].injury_episode_events[response["id"]] = response
    events = list(b[0].injury_episode_events.values())
    row = apply_episode_observations(b[0].injury_flags[b[3]][0], events, as_of=NOW + timedelta(days=1))
    decision = resolve_injury_policy(row, policies=[POLICY], bank=get_rehab_bank(), equipment=["cable_machine"],
        exposures=exposure_rows_with_observations(list(b[0].rehab_exposures.values()), events), as_of=NOW + timedelta(days=1))
    assert_closed(decision)
    assert decision["stage"] == "restore"
    assert not any(e["event_type"] in {"rehab_progression_assessment", "clinical_progression_review"} for e in events)
    again = view(b).live_prescription
    assert not again or all(block.get("rehab_drill_id") not in {RESTORE, CABLE} for block in again["session"]["blocks"])


@pytest.mark.parametrize("phase", ["GPP", "SPP", "TAPER"])
def test_today_and_full_guided_generation_keep_restore_and_exclude_dormant_cable(phase):
    from api.services.rehab_stage_snapshot import annotate_payload_with_rehab_stage
    from fightcamp.input_parsing import _extract_guided_injuries, _parse_guided_injuries
    from fightcamp.rehab_protocols import generate_rehab_protocols
    b = bundle()
    guided = dict(area="Left groin", severity="moderate", trend="improving", injury_type="tendon_ligament",
                  injury_subtypes=["strain"], timeframe="two_to_six_weeks", notes=b[5]["description"])
    payload = annotate_payload_with_rehab_stage(dict(guided_injuries=[guided]), store=b[0], athlete_id=b[3])
    entries, _ = _parse_guided_injuries(_extract_guided_injuries(payload))
    assert entries[0]["injury_type"] == entries[0]["rehab_type"] == "strain"
    block, _ = generate_rehab_protocols(injury_string="left groin strain", exercise_data=[], current_phase=phase, parsed_entries=entries)
    assert DRILL["name"] in block and DRILL["notes"] in block
    assert "Cable" not in block and "Copenhagen" not in block
    assert view(b).open_injuries[0]["rehab_decision"]["prescription"]["drill_id"] == RESTORE


@pytest.mark.parametrize("side", [None, "unknown", "bilateral", "right"])
def test_missing_or_different_injury_side_cannot_borrow_left_restore(side):
    b = bundle()
    b[0].injury_flags[b[3]][0].update(side=side)
    assert_closed(decide(b))


@pytest.mark.parametrize("state", ["not_started", "started", "done"])
def test_existing_frozen_baseline_history_is_preserved_after_a_setback(state):
    b = bundle()
    live = view(b).live_prescription
    if state == "not_started":
        b[0].upsert_session_completion(b[3], dict(plan_id=b[4], session_id=live["session"]["session_id"],
            training_day=DAY, status=state, prescription_snapshot=deepcopy(live)))
    else:
        execute(b, live)
        if state == "done":
            execute(b, live, "done", rehab_performance="done_as_shown")
    before = deepcopy(b[0].session_completions[b[3]])
    b[0].injury_flags[b[3]][0]["latest_reported_status"] = "worse"
    current = view(b)
    assert_closed(current.open_injuries[0]["rehab_decision"])
    assert current.open_injuries[0]["rehab_decision"]["stage"] == "calm"
    assert b[0].session_completions[b[3]] == before
    if state == "not_started":
        assert not current.live_prescription or current.live_prescription["safety_hold"]


def test_all_profiles_inventory_provenance_and_athlete_interactions_remain_unchanged():
    manifest = json.loads((ROOT / "tools/rehab_groin_activation_inventory_baseline.json").read_text())
    assert manifest["outcome"] == "load_closed" and manifest["selected_load_exercise"] is None
    followups = json.loads((ROOT / "tests/fixtures/rehab_groin_reviewed_followups.json").read_text())
    for path, expected in manifest["unchanged_files_sha256"].items():
        source = (ROOT / path).read_bytes()
        if path in followups:
            # Explicitly reviewed later report/UI changes, pinned byte-for-byte.
            # The fixture cites every intervening commit, including #2980,
            # #2986, #3003 and the Today UI PRs through #3078.
            revision = followups[path]
            assert revision["historical_sha256"] == expected and revision["commits"]
            expected = revision["approved_sha256"]
        if path in {"data/rehab_bank.json", "data/rehab_metadata_review.json"}:
            from tools.rehab_metadata_review_lib import before_bank_publication
            kind = "drills" if path.endswith("rehab_bank.json") else "reviews"
            source = (json.dumps(before_bank_publication(json.loads(source), kind), indent=2, ensure_ascii=False) + "\n").encode()
        if path == "data/rehab_pathways.json":
            from tools.rehab_metadata_review_lib import before_reported_permission_entry
            source = (json.dumps(before_reported_permission_entry(json.loads(source)), indent=2, ensure_ascii=False) + "\n").encode()
        if path == "tools/rehab_advanced_candidate_decisions.json":
            # Only the completion service's explicitly reviewed runtime
            # fingerprint changed; preserve all dated clinical judgments.
            # #2976 changed this provenance fingerprint only. Later runtime
            # edits must not masquerade as the dated review's recorded source.
            current = b"377d536920e8844fd25788d608f374be60d88a2b2e10ad1d602c8fa7355273cb"
            original = b"0695b8dac6aa854e2ebc855d625a80e699e74a3ab80ec86c3228e204fdfaeaf0"
            assert source.count(current) == 1
            source = source.replace(current, original)
        assert hashlib.sha256(source).hexdigest() == expected, path
    from tools.rehab_metadata_review_lib import before_reported_permission_entry
    profiles = before_reported_permission_entry(json.loads((ROOT / "data/rehab_pathways.json").read_text()))["profiles"]
    assert len(profiles) == 64
    assert {p["policy_id"]: content_hash(p) for p in profiles} == manifest["unchanged_profiles_sha256"]
    from tools.rehab_metadata_review_lib import before_bank_publication
    bank = before_bank_publication(json.loads((ROOT / "data/rehab_bank.json").read_text()), "drills")
    ledger = before_bank_publication(json.loads((ROOT / "data/rehab_metadata_review.json").read_text()), "reviews")
    assert content_hash({d["id"]: content_hash(d) for g in bank for d in g["drills"]}) == manifest["bank_before_sha256"]
    assert content_hash({r["drill_id"]: content_hash(r) for r in ledger}) == manifest["ledger_before_sha256"]
