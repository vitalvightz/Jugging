"""Synthetic versioned fixtures test software activation, not clinical approval."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.contracts.injury_policy import reconcile_session_prescription, resolve_injury_policy
from api.contracts.rehab_completion import completed_dose_from_session, resolve_rehab_completion, build_rehab_exposure_event
from api.services.injury_episode_service import (
    InjuryEpisodeObservation, apply_episode_observations, delayed_rehab_prompts, record_episode_observation,
)
from api.services.rehab_completion_service import session_rehab_items
from api.services import today_service
from fightcamp.rehab_clinical import ClinicalPolicy, content_hash, policy_review_hash, load_clinical_policies, validate_clinical_bank
from tests.support import FakeStore, _build_client, withdraw_health_consent

ATHLETE = str(uuid4())
PLAN = str(uuid4())
DAY = "2026-09-30"
NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def reviewed_policy(raw):
    draft = ClinicalPolicy.model_validate({**raw, "status": "draft", "activation": "shadow", "content_hash": None})
    return ClinicalPolicy.model_validate({**draft.model_dump(), "status": "active", "activation": raw.get("activation", "live"), "content_hash": policy_review_hash(draft)})


@pytest.fixture(autouse=True)
def isolate_synthetic_bank_index(monkeypatch):
    # Synthetic generation banks must not populate the shipped bank's id cache.
    monkeypatch.setattr("fightcamp.rehab_protocols._REHAB_DRILLS_BY_ID_CACHE", None)


@pytest.fixture
def reviewed():
    drill = {"id": "ankle_test_reviewed_control", "name": "Test control drill", "rehab_stage": "restore",
             "target_regions": ["ankle"], "target_tissues": ["test tissue"], "laterality_applicability": "side_specific",
             "care_pathway": "msk", "allowed_severities": ["low"], "function": "control", "equipment": [],
             "load": "low", "impact": "none", "velocity": "low"}
    bank = [{"location": "ankle", "type": "sprain", "phases": ["GPP"], "drills": [drill]}]
    policy = reviewed_policy({
        "policy_id": "test-only", "version": 1, "region": "ankle", "injury_type": "sprain",
        "activation": "live", "evidence_sources": ["test fixture"], "blocked_regions": ["ankle"],
        "prescriptions": [{"drill_id": drill["id"], "bank_hash": content_hash(drill), "stage": "restore",
                           "instructions": "Synthetic test instructions", "dose": {"sets": 2, "reps": 4},
                           "camp_doses": {"TAPER": {"sets": 1, "reps": 4}}, "allowed_severities": ["low"],
                           "stop_when": ["Synthetic test stop rule"], "frequency": "daily", "sources": ["test fixture"]}],
    })
    injury = {"id": str(uuid4()), "episode_id": str(uuid4()), "athlete_id": ATHLETE,
              "body_area": "Left ankle", "body_region": "ankle", "canonical_location": "ankle", "injury_type": "sprain",
              "description": "Left ankle sprain", "severity": "mild", "status": "monitoring",
              "latest_reported_status": "improving", "side": "left", "rehab_stage": "restore",
              "created_at": "2026-09-28T12:00:00Z", "updated_at": "2026-09-28T12:00:00Z"}
    return policy, bank, injury


def decide(reviewed, **kwargs):
    policy, bank, injury = reviewed
    return resolve_injury_policy(injury, policies=(policy,), bank=bank, **kwargs)


def test_original_shipped_policies_are_sourced_active_and_self_paced(tmp_path):
    # #2980/#2984/#2986 added source-dosed content. Retain the exact original
    # self-paced rollout checks against their hash-checked publication history.
    import json
    from pathlib import Path
    from fightcamp.rehab_protocols import get_rehab_bank
    from tools.rehab_metadata_review_lib import before_bank_publication
    source = Path(__file__).resolve().parents[1] / "data/rehab_pathways.json"
    raw = before_bank_publication(json.loads(source.read_text()), "profiles")
    dated_bank = before_bank_publication(get_rehab_bank(), "drills")
    target = tmp_path / "original-pathways.json"
    target.write_text(json.dumps(raw))
    policies = load_clinical_policies(target, bank=dated_bank)
    assert {p.region for p in policies} == {"chest", "ankle", "hamstring", "calf", "groin", "quads", "biceps", "triceps", "shoulder",
                                          "knee", "toe", "wrist", "elbow", "hand", "fingers", "achilles", "forearm", "hip", "heel", "shin", "neck", "lower back"}
    assert all(p.activation == "live" and p.status == "active" for p in policies)
    assert all(p.sources and (p.dose is None if not p.required_rehabilitation_level else
        policy.policy_id in {"achilles_tendonitis", "elbow_tendonitis", "ankle_sprain", "shoulder_tendonitis"} and p.stage == "load"
        and p.clinical_criterion is None and p.required_rehabilitation_level == "loading"
        and p.dose.model_dump(exclude_none=True) == {"sets":1,"reps":8 if policy.policy_id == "shoulder_tendonitis" else 10})
        for policy in policies for p in policy.prescriptions)
    from fightcamp.rehab_schema import CONTRACT_FIELDS, PAIN_CEILING_UNRESTRICTED
    bank = {d["id"]: d for group in dated_bank for d in group["drills"]}
    for policy in policies:
        assert policy.version == {"chest_strain": 3, "ankle_sprain": 6, "achilles_tendonitis":3, "elbow_tendonitis":4, "shoulder_tendonitis":3}.get(policy.policy_id, 1)
        for prescription in policy.prescriptions:
            drill = bank[prescription.drill_id]
            if policy.policy_id == "shoulder_tendonitis" and prescription.stage == "load":
                assert drill["dose"] == {"sets": 1, "reps": 8} and drill["pain_ceiling"] is None
            elif policy.policy_id in {"chest_strain", "ankle_sprain"}:
                assert drill["pain_ceiling"] == PAIN_CEILING_UNRESTRICTED
                assert all(drill.get(field) is not None for field in CONTRACT_FIELDS)
            else:
                # New baselines make no claim about numerical pain ceilings or dose.
                assert drill["pain_ceiling"] is None and drill["dose"] is None
    with pytest.raises(ValidationError):
        ClinicalPolicy.model_validate({**policies[0].model_dump(), "content_hash": "a" * 64})


def test_live_source_dosed_publication_preserves_authored_dose_and_integrity():
    from fightcamp.rehab_protocols import get_rehab_bank
    from fightcamp.rehab_publication import bank_prescription
    bank = get_rehab_bank()
    policies = load_clinical_policies()
    assert validate_clinical_bank(policies, bank) == []
    published = {d["id"]: (g, d) for g in bank for d in g["drills"] if d.get("prescription")}
    source_dosed = []
    for policy in policies:
        for prescription in policy.prescriptions:
            if prescription.drill_id not in published:
                continue
            group, drill = published[prescription.drill_id]
            projected, reasons = bank_prescription(group, drill)
            assert reasons == [] and projected is not None
            assert prescription.dose == projected.dose
            assert prescription.bank_hash == content_hash(drill)
            assert prescription.instructions == drill["notes"]
            if prescription.dose and prescription.stage == "restore":
                source_dosed.append(prescription.drill_id)
    assert "knee_pain_seated_knee_straightening_hold" in source_dosed
    assert "wrist_pain_side_to_side_wrist_bend" in source_dosed


@pytest.mark.parametrize("side", ["left", "unknown", "", "  ", None])
def test_chest_guidance_retains_episode_feedback_without_inventing_laterality(side):
    from fightcamp.rehab_protocols import get_rehab_bank
    row = dict(id=str(uuid4()), episode_id=str(uuid4()), athlete_id=ATHLETE, body_area="Chest", description="Chest strain",
               body_region="chest", canonical_location="chest", injury_type="strain", severity="mild", status="open", side=side)
    decision = resolve_injury_policy(row, policies=load_clinical_policies(), bank=get_rehab_bank(), phase="TAPER")
    snapshot = reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY)
    completion = dict(status="done", prescription_snapshot=snapshot, rehab_performance="done_as_shown")
    items = session_rehab_items({"id": PLAN}, training_day=DAY, session_id=snapshot["session"]["session_id"], prescription=snapshot)
    candidates = resolve_rehab_completion(items, [row], completion=completion).eligible
    assert len(candidates) == 1 and candidates[0].side == (str(side or "").strip() or "unknown")
    event = build_rehab_exposure_event(candidates[0], athlete_id=ATHLETE, plan_id=PLAN, session_id=snapshot["session"]["session_id"],
        training_day=DAY, completion=completion, during="same", limit="no")
    assert event.is_attributable_to(row)
    assert event.dose_completed.completion_state == "performed_amount_unknown"


def test_phase_preserves_approved_dose_and_eligibility(reviewed):
    results = [decide(reviewed, phase=phase) for phase in ("GPP", "SPP", "TAPER")]
    assert all(result["outcome"] == "prescribed_rehab" for result in results)
    assert len({r["prescription"]["drill_id"] for r in results}) == 1
    # #2984: fight-camp periodisation cannot change the approved dose.
    assert all(r["prescription"]["dose"] == {"sets": 2, "reps": 4} for r in results)


def test_readiness_preserves_restore_dose_and_validates_legacy_context_bounds(reviewed):
    policy, bank, injury = reviewed
    # #2993: CALM/RESTORE neither require a pull-back dose nor change dose.
    assert decide(reviewed, readiness_decision="pull_back")["prescription"]["dose"] == {"sets": 2, "reps": 4}
    raw = policy.model_dump()
    raw["prescriptions"][0]["readiness_doses"] = {"pull_back": {"sets": 2, "reps": 2}}
    reduced = reviewed_policy(raw)
    result = resolve_injury_policy(injury, policies=(reduced,), bank=bank, phase="TAPER", readiness_decision="pull_back")
    assert result["prescription"]["dose"] == {"sets": 2, "reps": 4}
    assert result["stage"] == "restore"
    raw["prescriptions"][0]["readiness_doses"]["pull_back"]["reps"] = 5
    with pytest.raises(ValidationError, match="context dose may only reduce"):
        reviewed_policy(raw)


def test_stale_review_and_missing_provenance_fail_closed(reviewed):
    policy, bank, _injury = reviewed
    bank[0]["drills"][0]["load"] = "high"
    assert validate_clinical_bank((policy,), bank)
    assert decide(reviewed)["prescription"] is None
    with pytest.raises(ValidationError):
        ClinicalPolicy.model_validate({**policy.model_dump(), "clinician": ""})
    raw = policy.model_dump()
    raw["prescriptions"][0]["camp_doses"]["TAPER"]["reps"] = 100
    with pytest.raises(ValidationError):
        ClinicalPolicy.model_validate(raw)
    changed = policy.model_dump()
    changed["prescriptions"][0]["dose"]["reps"] = 5
    with pytest.raises(ValidationError, match="policy content hash is missing or stale"):
        ClinicalPolicy.model_validate(changed)


def test_retired_content_is_unavailable_and_frozen_work_cannot_resume(reviewed, monkeypatch):
    policy, bank, injury = reviewed
    active = resolve_injury_policy(injury, policies=(policy,), bank=bank)
    frozen = reconcile_session_prescription(None, decisions=[active], plan_id=PLAN, training_day=DAY)
    retired = ClinicalPolicy.model_validate({**policy.model_dump(), "status": "retired", "activation": "shadow"})
    decision = resolve_injury_policy(injury, policies=(retired,), bank=bank)
    assert decision["outcome"] == "unsupported_prescription" and decision["prescription"] is None
    session = {"session_id": "s", "session_type": "strength", "blocks": [
        {"block_id": "training", "block_type": "strength", "mechanical_load_regions": ["ankle"], "contact_level": "none"}]}
    assert not reconcile_session_prescription(session, decisions=[decision], plan_id=PLAN, training_day=DAY)["safety_hold"]
    assert reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY,
        frozen=frozen)["safety_hold"]
    from fightcamp import rehab_clinical, rehab_protocols
    monkeypatch.setattr(rehab_clinical, "load_clinical_policies", lambda: (retired,))
    monkeypatch.setattr(rehab_protocols, "get_rehab_bank", lambda: bank)
    text, _ = rehab_protocols.generate_rehab_protocols(injury_string="", exercise_data=[], current_phase="GPP", parsed_entries=[injury])
    assert decision["summary"] in text
    assert "No suitable rehab drill" not in text and "Test control drill" not in text


def test_urgent_missing_and_shadow_outcomes_are_explicit(reviewed):
    policy, bank, injury = reviewed
    injury["severity"] = "severe"
    assert decide(reviewed)["outcome"] == "medical_review"
    missing = {**injury, "severity": "mild", "body_area": "", "body_region": None, "canonical_location": None, "injury_type": "", "description": ""}
    assert resolve_injury_policy(missing, policies=(policy,), bank=bank)["outcome"] == "missing_information"
    shadow = ClinicalPolicy.model_validate({**policy.model_dump(), "activation": "shadow"})
    injury["severity"] = "mild"
    decision = resolve_injury_policy(injury, policies=(shadow,), bank=bank)
    assert decision["outcome"] != "prescribed_rehab"
    assert reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY) is None


def test_new_injury_gets_rehab_on_rest_day_and_unknown_training_is_held(reviewed):
    decision = decide(reviewed)
    snapshot = reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY)
    assert snapshot["session"]["session_id"].startswith(f"rehab-{DAY}-")
    assert snapshot["session"]["blocks"][0]["injury_episode_id"] == decision["injury_episode_id"]
    # Nothing planned was held, so rest-day rehab is additional work, not a hold.
    assert "training_hold_reason" not in snapshot
    unknown = {"session_id": "s-1", "blocks": [{"block_id": "b-1", "block_type": "strength"}]}
    original = deepcopy(unknown)
    held = reconcile_session_prescription(unknown, decisions=[decision], plan_id=PLAN, training_day=DAY)
    assert not held["safety_hold"] and held["changes"][0]["action"] == "held"
    assert held["rehab_only"] and held["held_session_id"] == "s-1"
    assert held["training_hold_reason"]
    assert all(block.get("policy_id") for block in held["session"]["blocks"])
    assert unknown == original


def test_rehab_added_to_training_goes_before_the_cool_down(reviewed):
    decision = decide(reviewed)
    session = {"session_id": "s-1", "session_type": "strength_power", "blocks": [
        {"block_id": "warm", "block_type": "mobility_activation", "mechanical_load_regions": [], "tags": [], "contact_level": "none"},
        {"block_id": "press", "block_type": "strength", "mechanical_load_regions": ["chest"], "tags": [], "contact_level": "none"},
        {"block_id": "cool", "block_type": "cooldown_recovery", "mechanical_load_regions": [], "tags": [], "contact_level": "none"}]}
    live = reconcile_session_prescription(session, decisions=[decision], plan_id=PLAN, training_day=DAY)
    assert not live["safety_hold"] and not live["rehab_only"] and "training_hold_reason" not in live
    kinds = [block["block_type"] for block in live["session"]["blocks"]]
    assert kinds[:2] == ["mobility_activation", "strength"] and kinds[-1] == "cooldown_recovery"
    assert set(kinds[2:-1]) == {"rehab"}
    # No cool-down: rehab closes the session.
    session["blocks"] = session["blocks"][:2]
    live = reconcile_session_prescription(session, decisions=[decision], plan_id=PLAN, training_day=DAY)
    assert live["session"]["blocks"][-1]["block_type"] == "rehab"


def test_added_rehab_note_keeps_the_days_verdict_and_action():
    from api.services.today_service import _with_added_rehab_note
    assert _with_added_rehab_note("All clear today.", before_cooldown=True) == (
        "All clear today. Your reviewed rehab is added to today's session, before the cool-down.")
    assert _with_added_rehab_note("Keep it light\nSleep was short.\nCut the volume.", before_cooldown=False) == (
        "Keep it light\nSleep was short. Your reviewed rehab is added to today's session.\nCut the volume.")


def test_substitution_requires_same_role_and_workload(reviewed):
    block = {"block_id": "b-1", "role": "control", "dose": {"sets": 1, "reps": 2},
             "mechanical_load_regions": ["ankle"], "contact_level": "none"}
    alternate = {**block, "mechanical_load_regions": ["shoulder"]}
    block["alternates"] = [alternate]
    snapshot = reconcile_session_prescription({"session_id": "s", "blocks": [block]}, decisions=[decide(reviewed)], plan_id=PLAN, training_day=DAY)
    assert not snapshot["safety_hold"]
    assert snapshot["changes"][0]["action"] == "substituted"
    alternate["dose"] = {"sets": 10, "reps": 2}
    assert reconcile_session_prescription({"blocks": [block]}, decisions=[decide(reviewed)], plan_id=PLAN, training_day=DAY)["rehab_only"]


def test_unknown_or_malformed_alternate_demands_cannot_claim_safety(reviewed):
    for demands in (["unknown"], [{"region": "shoulder"}], {"region": "shoulder"}):
        block = {"block_id": "b", "role": "control", "dose": {"reps": 2}, "mechanical_load_regions": ["ankle"], "contact_level": "none"}
        block["alternates"] = [{**block, "mechanical_load_regions": demands}]
        snapshot = reconcile_session_prescription({"session_id": "s", "blocks": [block]}, decisions=[decide(reviewed)], plan_id=PLAN, training_day=DAY)
        assert snapshot["changes"][0]["action"] == "held"


def test_frozen_content_stays_fixed_but_new_episode_or_gate_stops_it(reviewed):
    decision = decide(reviewed)
    frozen = reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY)
    before = deepcopy(frozen)
    changed = {**decision, "injury_episode_id": str(uuid4())}
    result = reconcile_session_prescription(None, decisions=[changed], plan_id=PLAN, training_day=DAY, frozen=frozen)
    assert result["session"] == frozen["session"] and result["safety_hold"]
    assert frozen == before


def test_snapshot_dose_and_attribution_survive_plan_and_bank_changes(reviewed):
    decision = decide(reviewed)
    snapshot = reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY)
    plan = {"id": PLAN, "structured_plan": {"weeks": []}}
    reviewed[1][0]["drills"][0]["name"] = "Changed current bank"
    items = session_rehab_items(plan, training_day=DAY, session_id=snapshot["session"]["session_id"], prescription=snapshot)
    assert items[0]["name"] == "Test control drill"
    completion = {"status": "done", "prescription_snapshot": snapshot, "rehab_performance": "done_as_shown"}
    resolution = resolve_rehab_completion(items, [reviewed[2], {**reviewed[2], "id": str(uuid4()), "episode_id": str(uuid4())}], completion=completion)
    assert len(resolution.eligible) == 1
    assert completed_dose_from_session(completion, prescribed=items[0]["prescribed_dose"]).completion_state == "quantified"
    completion["rehab_performance"] = "changed"
    assert completed_dose_from_session(completion, prescribed=items[0]["prescribed_dose"]).reps is None
    assert not resolve_rehab_completion(items, [{**reviewed[2], "episode_id": str(uuid4())}], completion=completion).eligible


def test_today_start_rejects_stale_revision_and_freezes_server_content(reviewed, monkeypatch):
    policy, bank, injury = reviewed
    store = FakeStore()
    store.injury_flags[ATHLETE] = [injury]
    store.plans[PLAN] = {"id": PLAN, "athlete_id": ATHLETE, "status": "ready", "created_at": "2026-09-01T00:00:00Z",
                         "structured_plan": {"weeks": [{"phase_label": "GPP", "days": [{"date": DAY, "day_type": "rest", "sessions": []}]}]}}
    store.set_active_plan_id(ATHLETE, PLAN)
    store.upsert_today_checkin(ATHLETE, {"plan_id": PLAN, "training_day": DAY, "recommendation_state": "train_as_planned", "pain": "none", "body": "good"})
    monkeypatch.setattr(today_service, "load_clinical_policies", lambda: (policy,))
    monkeypatch.setattr(today_service, "get_rehab_bank", lambda: bank)
    view = today_service.build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
    assert view.live_prescription and view.today.session_scope == "today"
    assert view.live_prescription["readiness_context"]["id"] == store.get_today_checkin(ATHLETE, PLAN, DAY)["id"]
    payload = {"plan_id": PLAN, "session_id": view.live_prescription["session"]["session_id"], "status": "started", "prescription_revision": "a" * 64}
    with pytest.raises(HTTPException) as failure:
        today_service.upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="UTC", payload=payload, now=NOW)
    assert failure.value.status_code == 409
    payload["prescription_revision"] = view.live_prescription["revision"]
    row = today_service.upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="UTC", payload=payload, now=NOW)
    assert row["prescription_snapshot"]["revision"] == payload["prescription_revision"]
    injury["latest_reported_status"] = "worse"
    held = today_service.build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
    assert held.live_prescription["safety_hold"]
    stopped = today_service.upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="UTC",
        payload={**payload, "status": "modified", "rehab_performance": "stopped", "modification_reason": "Stopped when the injury worsened."}, now=NOW)
    assert stopped["rehab_performance"] == "stopped" and stopped["status"] == "modified"


def test_policy_activation_preserves_a_preexisting_started_session(reviewed, monkeypatch):
    policy, bank, injury = reviewed
    store = FakeStore()
    store.injury_flags[ATHLETE] = [injury]
    session = {"session_id": "old-strength", "title": "Original strength", "session_type": "strength",
               "blocks": [{"block_id": "original", "block_type": "strength", "display_name": "Original work"}]}
    store.plans[PLAN] = {"id": PLAN, "athlete_id": ATHLETE, "status": "ready", "created_at": "2026-09-01T00:00:00Z",
                         "structured_plan": {"weeks": [{"phase_label": "GPP", "days": [{"date": DAY, "day_type": "strength", "sessions": [session]}]}]}}
    store.set_active_plan_id(ATHLETE, PLAN)
    store.upsert_today_checkin(ATHLETE, {"plan_id": PLAN, "training_day": DAY, "recommendation_state": "train_as_planned", "pain": "none"})
    store.upsert_session_completion(ATHLETE, {"plan_id": PLAN, "session_id": "old-strength", "training_day": DAY,
                                            "status": "started", "started_at": NOW.isoformat()})
    monkeypatch.setattr(today_service, "load_clinical_policies", lambda: (policy,))
    monkeypatch.setattr(today_service, "get_rehab_bank", lambda: bank)
    view = today_service.build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
    assert view.live_prescription["safety_hold"]
    assert view.live_prescription["session"]["session_id"] == "old-strength"
    assert view.live_prescription["session"]["blocks"] == session["blocks"]
    stopped = today_service.upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW,
        payload={"plan_id": PLAN, "session_id": "old-strength", "status": "modified", "rehab_performance": "stopped",
                 "modification_reason": "Stopped under the server injury hold."})
    assert stopped["rehab_performance"] == "stopped" and not stopped.get("prescription_snapshot")


def test_live_policy_fails_closed_when_injury_history_cannot_be_read(reviewed, monkeypatch):
    store = FakeStore()
    def fail_read(*args, **kwargs):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(store, "list_injury_flags", fail_read)
    monkeypatch.setattr(today_service, "load_clinical_policies", lambda: (reviewed[0],))
    with pytest.raises(HTTPException) as failure:
        today_service._open_injury_flags(store, ATHLETE)
    assert failure.value.status_code == 503


def test_generation_uses_reviewed_policy_in_taper_and_stage2_does_not_expand_alternates(reviewed, monkeypatch):
    from fightcamp import rehab_clinical, rehab_protocols
    from fightcamp.stage2_payload import _build_rehab_slots
    policy, bank, injury = reviewed
    monkeypatch.setattr(rehab_clinical, "load_clinical_policies", lambda: (policy,))
    monkeypatch.setattr(rehab_protocols, "get_rehab_bank", lambda: bank)
    text, _seen = rehab_protocols.generate_rehab_protocols(injury_string="", exercise_data=[], current_phase="TAPER", parsed_entries=[injury])
    # #2984: generation preserves the approved dose in TAPER, too.
    assert "Test control drill" in text and "2 x 4 reps" in text
    assert rehab_protocols.rehab_drill_options_for_phase("sprain", "ankle", "TAPER") == []
    slots = _build_rehab_slots(text, "TAPER")
    assert slots and slots[0]["selected"]["rehab_drill_id"] == bank[0]["drills"][0]["id"]
    assert not slots[0]["alternates"]


def test_generation_never_adds_unreviewed_set_count(reviewed, monkeypatch):
    from fightcamp import rehab_clinical, rehab_protocols
    policy, bank, injury = reviewed
    raw = policy.model_dump()
    raw["prescriptions"][0]["dose"] = {"reps": 4}
    raw["prescriptions"][0]["camp_doses"] = {}
    policy = reviewed_policy(raw)
    monkeypatch.setattr(rehab_clinical, "load_clinical_policies", lambda: (policy,))
    monkeypatch.setattr(rehab_protocols, "get_rehab_bank", lambda: bank)
    option = rehab_protocols._reviewed_episode_option(injury, "ankle", "GPP")
    assert "4 reps" in option["line"][1] and "1 x" not in option["line"][1]


def test_clearance_is_quick_self_reported_episode_specific_and_owned(reviewed):
    store = FakeStore()
    injury = reviewed[2]
    store.injury_flags[ATHLETE] = [injury]
    observation = InjuryEpisodeObservation(injury_id=injury["id"], injury_episode_id=injury["episode_id"], event_type="clinician_clearance_report", scopes=["rehab"])
    saved = record_episode_observation(store, athlete_id=ATHLETE, observation=observation, training_day=DAY)
    assert not saved["payload"]["externally_verified"]
    assert record_episode_observation(store, athlete_id=ATHLETE, observation=observation, training_day=DAY)["id"] == saved["id"]
    assert apply_episode_observations(injury, [saved])["clinician_clearance"]["scopes"] == ["rehab"]
    setback = {"athlete_id": ATHLETE, "injury_id": injury["id"], "injury_episode_id": injury["episode_id"], "event_type": "injury_checkin",
               "created_at": "2026-10-01T00:00:00Z", "payload": {"latest_reported_status": "worse"}}
    before = {**saved, "created_at": "2026-09-30T12:00:00Z"}
    assert apply_episode_observations(injury, [before, setback])["clinician_clearance"]["scopes"] == ["rehab"]
    fresh = record_episode_observation(store, athlete_id=ATHLETE, observation=observation.model_copy(update={"report_id": uuid4()}), training_day="2026-10-02")
    assert fresh["id"] != saved["id"]
    fresh["created_at"] = "2026-10-02T12:00:00Z"
    assert apply_episode_observations(injury, [before, setback, fresh])["clinician_clearance"]["scopes"] == ["rehab"]
    assert "clinician_clearance" not in apply_episode_observations({**injury, "episode_id": str(uuid4())}, [saved])
    with pytest.raises(HTTPException) as failure:
        record_episode_observation(store, athlete_id=str(uuid4()), observation=observation, training_day=DAY)
    assert failure.value.status_code == 404


def test_episode_report_endpoint_requires_consent_and_keeps_retries_idempotent(reviewed):
    client, store, _ = _build_client()
    injury = {**reviewed[2], "athlete_id": "athlete-1"}
    store.injury_flags["athlete-1"] = [injury]
    body = {"injury_id": injury["id"], "injury_episode_id": injury["episode_id"],
            "event_type": "clinician_clearance_report", "scopes": ["rehab"], "report_id": str(uuid4())}
    headers = {"Authorization": "Bearer athlete-token"}
    first = client.post("/api/today/injury-episode-observation", headers=headers, json=body)
    assert first.status_code == 200
    assert first.json()["payload"]["externally_verified"] is False
    retry = client.post("/api/today/injury-episode-observation", headers=headers, json=body)
    assert retry.json()["id"] == first.json()["id"]
    withdraw_health_consent(store)
    refused = client.post("/api/today/injury-episode-observation", headers=headers, json={**body, "report_id": str(uuid4())})
    assert refused.status_code == 403
    assert len(store.injury_episode_events) == 1


def test_delayed_response_is_separate_durable_and_idempotent(reviewed):
    store = FakeStore()
    injury = reviewed[2]
    store.injury_flags[ATHLETE] = [injury]
    exposure_id = str(uuid4())
    event = {"injury_id": injury["id"], "injury_episode_id": injury["episode_id"], "body_region": "ankle", "occurred_at": "2026-09-29T12:00:00Z", "response": {"next_day_response": "not_yet_known"}}
    store.rehab_exposures[exposure_id] = {"id": exposure_id, "athlete_id": ATHLETE, "event_json": event}
    assert delayed_rehab_prompts(store, ATHLETE, DAY)
    observation = InjuryEpisodeObservation(injury_id=injury["id"], injury_episode_id=injury["episode_id"], event_type="delayed_rehab_response", exposure_id=exposure_id, response="same")
    record_episode_observation(store, athlete_id=ATHLETE, observation=observation, training_day=DAY)
    assert not delayed_rehab_prompts(store, ATHLETE, DAY)
    assert event["response"]["next_day_response"] == "not_yet_known"
    record_episode_observation(store, athlete_id=ATHLETE, observation=observation, training_day=DAY)
    with pytest.raises(HTTPException):
        record_episode_observation(store, athlete_id=ATHLETE, observation=observation.model_copy(update={"response": "worse"}), training_day=DAY)


@pytest.mark.parametrize("occurred,shown", [
    ("2026-09-30T00:00:00Z", False),  # same day: the day after has not come yet
    ("2026-09-29T00:00:00Z", True),   # the day after: asked
    ("2026-09-28T00:00:00Z", False),  # older: no longer offered
])
def test_next_day_question_is_offered_on_the_following_day_only(reviewed, occurred, shown):
    store = FakeStore()
    injury = reviewed[2]
    store.injury_flags[ATHLETE] = [injury]
    exposure_id = str(uuid4())
    event = {"injury_id": injury["id"], "injury_episode_id": injury["episode_id"], "body_region": "ankle",
             "occurred_at": occurred, "response": {"next_day_response": "not_yet_known"}}
    store.rehab_exposures[exposure_id] = {"id": exposure_id, "athlete_id": ATHLETE, "event_json": event}
    assert bool(delayed_rehab_prompts(store, ATHLETE, DAY, "Europe/London")) == shown


def test_active_chest_pilot_rest_day_start_completion_and_feedback_through_api():
    client, store, _ = _build_client()
    headers = {"Authorization": "Bearer athlete-token"}
    day = client.get("/api/today", headers=headers).json()["today"]["training_day"]
    store.plans[PLAN] = dict(id=PLAN, athlete_id="athlete-1", status="ready", created_at="2026-09-01T00:00:00Z",
        structured_plan={"weeks": [{"phase_label": "TAPER", "days": [{"date": day, "day_type": "rest", "sessions": []}]}]})
    store.set_active_plan_id("athlete-1", PLAN)
    store.upsert_today_checkin("athlete-1", dict(plan_id=PLAN, training_day=day, recommendation_state="train_as_planned", pain="none"))
    row = store.create_injury_flag("athlete-1", dict(body_area="Chest", description="Chest strain", severity="mild", status="open", body_region="chest", side="unknown"))
    view = client.get("/api/today", headers=headers).json()
    live = view["live_prescription"]
    assert live and view["open_injuries"][0]["rehab_decision"]["schedule"]["state"] == "due"
    body = dict(plan_id=PLAN, session_id=live["session"]["session_id"], status="started", prescription_revision=live["revision"])
    assert client.post("/api/today/session-completion", headers=headers, json=body).status_code == 201
    completed = client.post("/api/today/session-completion", headers=headers, json={**body, "status": "done", "rehab_performance": "done_as_shown"})
    assert completed.status_code == 201, completed.text
    assert completed.json()["rehab_response_prompts"][0]["injury_id"] == row["id"]
    answered = client.post("/api/today/rehab-responses", headers=headers, json={"plan_id": PLAN, "session_id": body["session_id"],
        "answers": [dict(injury_id=row["id"], injury_episode_id=row["episode_id"], during_response="same", limit_response="no")]})
    assert answered.status_code == 201, answered.text
    event = next(iter(store.rehab_exposures.values()))["event_json"]
    assert event["side"] == "unknown" and event["dose_completed"]["completion_state"] == "performed_amount_unknown"
    assert client.get("/api/today", headers=headers).json()["open_injuries"][0]["rehab_decision"]["schedule"]["state"] == "already_completed"


@pytest.mark.parametrize("sibling_region", ["shoulder", "ankle"])
@pytest.mark.parametrize("load", ["high", {"method": "percentage", "value": 85, "unit": "percent", "ref": "1RM"}])
def test_live_rehab_checks_the_whole_training_day_and_owns_one_snapshot(reviewed, monkeypatch, sibling_region, load):
    policy, bank, injury = reviewed
    store = FakeStore()
    store.injury_flags[ATHLETE] = [injury]
    def session(identity, region):
        return {"session_id": identity, "title": identity, "session_type": "strength", "blocks": [
            {"block_id": identity, "block_type": "strength", "display_name": identity,
             "mechanical_load_regions": [region], "contact_level": "none", "load": load}]}
    store.plans[PLAN] = {"id": PLAN, "athlete_id": ATHLETE, "status": "ready", "created_at": "2026-09-01T00:00:00Z",
        "structured_plan": {"weeks": [{"phase_label": "GPP", "days": [{"date": DAY, "day_type": "strength",
            "sessions": [session("primary", "shoulder"), session("sibling", sibling_region)]}]}]}}
    store.set_active_plan_id(ATHLETE, PLAN)
    store.upsert_today_checkin(ATHLETE, {"plan_id": PLAN, "training_day": DAY,
        "recommendation_state": "train_as_planned", "pain": "none", "body": "good"})
    monkeypatch.setattr(today_service, "load_clinical_policies", lambda: (policy,))
    monkeypatch.setattr(today_service, "get_rehab_bank", lambda: bank)
    view = today_service.build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
    live = view.live_prescription
    if sibling_region == "ankle":
        # Demanding sibling work blocks loading even when the primary is safe.
        assert live["safety_hold"] or live["session"]["session_type"] == "rehab"
        return
    assert {b["block_id"] for b in live["session"]["blocks"]} >= {"primary", "sibling"}
    assert not live["safety_hold"]
    payload = {"plan_id": PLAN, "session_id": "primary", "status": "started", "prescription_revision": live["revision"]}
    today_service.upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="UTC", payload=payload, now=NOW)
    assert store.get_session_completion(ATHLETE, "primary", DAY)["prescription_snapshot"]
    assert not store.get_session_completion(ATHLETE, "sibling", DAY).get("prescription_snapshot")
    resumed = today_service.build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
    assert resumed.live_prescription["frozen"] and not resumed.live_prescription["safety_hold"]
    today_service.upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="UTC",
        payload={**payload, "status": "done", "rehab_performance": "done_as_shown"}, now=NOW)
    assert all(row["status"] == "done" for row in store.session_completions[ATHLETE])
    assert sum(bool(row.get("prescription_snapshot")) for row in store.session_completions[ATHLETE]) == 1


@pytest.mark.parametrize("field,value", [("region", "ankel"), ("injury_type", "sprian")])
def test_policy_rejects_noncanonical_identity(reviewed, field, value):
    raw = reviewed[0].model_dump()
    raw[field] = value
    with pytest.raises(ValidationError, match="unknown policy region or injury type"):
        ClinicalPolicy.model_validate(raw)


@pytest.mark.parametrize("field,value", [("instructions", "  "), ("stop_when", ["Valid rule", "  "])])
def test_prescription_rejects_blank_safety_guidance(reviewed, field, value):
    from fightcamp.rehab_clinical import ClinicalPrescription
    raw = reviewed[0].prescriptions[0].model_dump()
    raw[field] = value
    with pytest.raises(ValidationError, match="must not be blank"):
        ClinicalPrescription.model_validate(raw)


def test_snapshot_identity_mismatch_is_rejected_and_legacy_id_falls_back(reviewed):
    decision = decide(reviewed)
    snapshot = reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY)
    snapshot["session"]["blocks"][0]["drill_snapshot"]["id"] = "wrong_drill"
    with pytest.raises(HTTPException, match="rehab_snapshot_identity_mismatch"):
        session_rehab_items({"id": PLAN}, training_day=DAY, session_id=snapshot["session"]["session_id"], prescription=snapshot)
    from fightcamp.rehab_protocols import get_rehab_bank
    drill = next(d for g in get_rehab_bank() for d in g["drills"] if d["id"] == "ankle_sprain_heel_lowering")
    block = snapshot["session"]["blocks"][0]
    block.pop("drill_snapshot")
    block["rehab_drill_id"] = drill["id"]
    items = session_rehab_items({"id": PLAN}, training_day=DAY, session_id=snapshot["session"]["session_id"], prescription=snapshot)
    assert items[0]["id"] == drill["id"] and items[0]["prescribed_dose"] == {"sets": 2, "reps": 4}


@pytest.mark.parametrize("targets", [[], ["ankle"], ["shoulder"]])
def test_legacy_rehab_is_retained_or_explicitly_held(reviewed, targets):
    block = dict(block_id="legacy", block_type="rehab", target_regions=targets, rehab_drill_id="legacy_drill")
    snapshot = reconcile_session_prescription({"session_id": "s", "blocks": [block]}, decisions=[decide(reviewed)], plan_id=PLAN, training_day=DAY)
    assert any(b["block_id"] == "legacy" for b in snapshot["session"]["blocks"]) or any(
        c.get("block_id") == "legacy" and c["action"] == "held" for c in snapshot["changes"])


def test_exact_pilot_identity_replaces_only_due_work(reviewed):
    decision = decide(reviewed)
    snapshot = reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY)
    block = snapshot["session"]["blocks"][0]
    repeated = reconcile_session_prescription(snapshot["session"], decisions=[decision], plan_id=PLAN, training_day=DAY)
    assert len(repeated["session"]["blocks"]) == 1
    assert repeated["changes"][0]["action"] == "replaced"
    decision["schedule"] = {"state": "recovery_day"}
    held = reconcile_session_prescription(snapshot["session"], decisions=[decision], plan_id=PLAN, training_day=DAY)
    assert held["safety_hold"] and held["session"]["blocks"][0]["_policy_held"]
    assert held["changes"][0]["block_id"] == block["block_id"]


def test_resolved_pilot_episode_cannot_be_programmed_again(reviewed, monkeypatch):
    from fightcamp import rehab_clinical, rehab_protocols
    policy, bank, row = reviewed
    monkeypatch.setattr(rehab_clinical, "load_clinical_policies", lambda: (policy,))
    monkeypatch.setattr(rehab_protocols, "get_rehab_bank", lambda: bank)
    episode = rehab_protocols._episode_context({**row, "status": "resolved"})
    result = rehab_protocols._reviewed_episode_option(episode, "ankle", "GPP")
    assert result["line"] is None and result["decision"]["outcome"] == "no_rehab_indicated"


@pytest.mark.parametrize("session_type", ["strength", "sparring"])
def test_replacing_assigned_rehab_keeps_its_slot_before_new_work(reviewed, session_type):
    current = decide(reviewed)
    snapshot = reconcile_session_prescription(None, decisions=[current], plan_id=PLAN, training_day=DAY)
    session = snapshot["session"]
    session["session_type"] = session_type
    if session_type == "strength":
        session["blocks"].append(dict(block_id="legacy", block_type="rehab", mechanical_load_regions=["shoulder"],
                                      tags=[], contact_level="none"))
    additional = deepcopy(current)
    additional["injury_id"] = "00000000-0000-4000-8000-000000000000"  # sorts before current
    additional["injury_episode_id"] = str(uuid4())
    result = reconcile_session_prescription(session, decisions=[additional, current], plan_id=PLAN, training_day=DAY)
    blocks = result["session"]["blocks"]
    assert len(blocks) == result["allocation_limit"]
    assert sum(b.get("injury_id") == current["injury_id"] for b in blocks) == 1
    assert not any(b.get("injury_id") == additional["injury_id"] for b in blocks)
    assert any(c.get("injury_id") == additional["injury_id"] and c["action"] == "deferred" for c in result["changes"])


def test_shared_drill_names_do_not_allocate_another_episode(monkeypatch):
    from fightcamp import rehab_protocols
    monkeypatch.setattr(rehab_protocols, "_reviewed_episode_option", lambda *args: {"line": ("Shared routine", "Self-paced"), "decision": {}})
    episodes = [dict(injury_id="left", side="left", injury_type="sprain"), dict(injury_id="right", side="right", injury_type="sprain")]
    outcomes = []
    selected = rehab_protocols._select_rehab_drills_per_episode(episodes=episodes, loc="ankle", loc_candidates=["ankle"],
        current_phase="GPP", day_type="strength", drill_limit=1, outcomes=outcomes)
    assert len(selected) == 1 and [o["outcome"] for o in outcomes] == ["available", "deferred"]


@pytest.mark.parametrize("timezone,occurred,day,allowed", [
    ("Pacific/Honolulu", "2026-10-01T05:00:00Z", "2026-10-01", True),
    ("Pacific/Auckland", "2026-09-30T18:00:00Z", "2026-10-01", False),
    # Logged rehab is stamped at midnight UTC on its own training day: that
    # names the day, so the next-day question waits for the following day
    # even where midnight UTC falls before the local rollover.
    ("Europe/London", "2026-10-01T00:00:00Z", "2026-10-01", False),
    ("Europe/London", "2026-10-01T00:00:00Z", "2026-10-02", True),
    ("Pacific/Honolulu", "2026-10-01T00:00:00Z", "2026-10-01", False),
])
def test_delayed_feedback_uses_athlete_training_day_and_survives_reopening(reviewed, timezone, occurred, day, allowed):
    store = FakeStore()
    row = reviewed[2]
    old_episode = row["episode_id"]
    store.injury_flags[ATHLETE] = [{**row, "episode_id": str(uuid4())}]
    exposure = str(uuid4())
    store.rehab_exposures[exposure] = dict(id=exposure, athlete_id=ATHLETE, created_at=occurred,
        event_json=dict(injury_id=row["id"], injury_episode_id=old_episode, body_region="ankle", occurred_at=occurred,
                        response={"next_day_response": "not_yet_known"}))
    prompts = delayed_rehab_prompts(store, ATHLETE, day, timezone)
    assert bool(prompts) == allowed
    observation = InjuryEpisodeObservation(injury_id=row["id"], injury_episode_id=old_episode,
        event_type="delayed_rehab_response", exposure_id=exposure, response="same")
    if allowed:
        saved = record_episode_observation(store, athlete_id=ATHLETE, observation=observation, training_day=day, athlete_timezone=timezone)
        assert saved["injury_episode_id"] == old_episode
        assert apply_episode_observations(store.injury_flags[ATHLETE][0], [saved])["latest_reported_at"] is None
    else:
        with pytest.raises(HTTPException) as failure:
            record_episode_observation(store, athlete_id=ATHLETE, observation=observation, training_day=day, athlete_timezone=timezone)
        assert failure.value.status_code == 409


@pytest.mark.parametrize("started,reason", [(False, "Stopped"), (True, "")])
def test_client_stopped_value_cannot_bypass_a_server_hold(reviewed, monkeypatch, started, reason):
    policy, bank, injury = reviewed
    store = FakeStore()
    store.injury_flags[ATHLETE] = [{**injury, "severity": "severe"}]
    session = dict(session_id="s", session_type="strength", blocks=[dict(block_id="b", block_type="strength")])
    store.plans[PLAN] = dict(id=PLAN, athlete_id=ATHLETE, status="ready", created_at="2026-09-01T00:00:00Z",
        structured_plan={"weeks": [{"phase_label": "GPP", "days": [{"date": DAY, "day_type": "high", "sessions": [session]}]}]})
    store.set_active_plan_id(ATHLETE, PLAN)
    if started:
        store.upsert_session_completion(ATHLETE, dict(plan_id=PLAN, session_id="s", training_day=DAY, status="started", started_at=NOW.isoformat()))
    monkeypatch.setattr(today_service, "load_clinical_policies", lambda: (policy,))
    monkeypatch.setattr(today_service, "get_rehab_bank", lambda: bank)
    with pytest.raises(HTTPException) as failure:
        today_service.upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW,
            payload=dict(plan_id=PLAN, session_id="s", status="modified", rehab_performance="stopped", modification_reason=reason))
    assert failure.value.status_code == 409


def test_rehab_done_requires_an_explicit_performance_choice(reviewed, monkeypatch):
    policy, bank, injury = reviewed
    store = FakeStore()
    store.injury_flags[ATHLETE] = [injury]
    store.plans[PLAN] = dict(id=PLAN, athlete_id=ATHLETE, status="ready", created_at="2026-09-01T00:00:00Z",
        structured_plan={"weeks": [{"phase_label": "GPP", "days": [{"date": DAY, "day_type": "rest", "sessions": []}]}]})
    store.set_active_plan_id(ATHLETE, PLAN)
    store.upsert_today_checkin(ATHLETE, dict(plan_id=PLAN, training_day=DAY, recommendation_state="train_as_planned", pain="none"))
    monkeypatch.setattr(today_service, "load_clinical_policies", lambda: (policy,))
    monkeypatch.setattr(today_service, "get_rehab_bank", lambda: bank)
    view = today_service.build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
    payload = dict(plan_id=PLAN, session_id=view.live_prescription["session"]["session_id"], status="started", prescription_revision=view.live_prescription["revision"])
    today_service.upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW, payload=payload)
    with pytest.raises(HTTPException) as failure:
        today_service.upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW, payload={**payload, "status": "done"})
    assert failure.value.status_code == 422
    assert store.get_session_completion(ATHLETE, payload["session_id"], DAY)["status"] == "started"
    done = today_service.upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW,
        payload={**payload, "status": "done", "rehab_performance": "done_as_shown"})
    retry = today_service.upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW,
        payload={**payload, "status": "done"})
    assert retry["rehab_performance"] == "done_as_shown"
    assert retry["completed_at"] == done["completed_at"]


def test_skip_survives_raw_noncritical_rehab_read_failure(reviewed, monkeypatch):
    store = FakeStore()
    store.plans[PLAN] = dict(id=PLAN, athlete_id=ATHLETE, status="ready", created_at="2026-09-01T00:00:00Z",
        structured_plan={"weeks": [{"days": [{"date": DAY, "day_type": "strength", "sessions": [dict(session_id="s", blocks=[])]}]}]})
    monkeypatch.setattr(today_service, "build_today_command_view", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("rehab read unavailable")))
    row = today_service.upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW,
        payload=dict(plan_id=PLAN, session_id="s", status="skipped", modification_reason="Resting today"))
    assert row["status"] == "skipped" and not row.get("prescription_snapshot")


def test_rehab_replacement_does_not_borrow_held_training_coaching(reviewed):
    session = {"session_id": "s", "session_type": "strength", "title": "Strength",
               "objective": "Build structural strength", "coach_note": "Lift heavy",
               "mindset_anchor": {"focus": "Power"}, "planned_duration": {"value": 60, "unit": "minutes"},
               "coach_led_contact": "Hard sparring", "effective_load": "high",
               "blocks": [{"block_id": "b", "block_type": "strength"}]}
    snapshot = reconcile_session_prescription(session, decisions=[decide(reviewed)], plan_id=PLAN, training_day=DAY)
    assert snapshot["rehab_only"]
    replacement = snapshot["session"]
    assert "strength" not in replacement.get("objective", "").lower()
    assert not replacement.get("mindset_anchor") and not replacement.get("coach_led_contact")
    assert not replacement.get("planned_duration")
    assert replacement["effective_load"] == "reduced"


def test_today_explains_the_rehab_replacement_instead_of_stored_green(reviewed, monkeypatch):
    policy, bank, injury = reviewed
    store = FakeStore()
    store.injury_flags[ATHLETE] = [injury]
    store.plans[PLAN] = {"id": PLAN, "athlete_id": ATHLETE, "status": "ready", "created_at": "2026-09-01T00:00:00Z",
        "structured_plan": {"weeks": [{"phase_label": "GPP", "days": [{"date": DAY, "day_type": "moderate",
            "sessions": [{"session_id": "strength", "session_type": "strength", "blocks": [{"block_id": "b", "block_type": "strength"}]}]}]}]}}
    store.set_active_plan_id(ATHLETE, PLAN)
    store.upsert_today_checkin(ATHLETE, {"plan_id": PLAN, "training_day": DAY, "recommendation_state": "train_as_planned", "pain": "none"})
    monkeypatch.setattr(today_service, "load_clinical_policies", lambda: (policy,))
    monkeypatch.setattr(today_service, "get_rehab_bank", lambda: bank)
    view = today_service.build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
    assert view.live_prescription["rehab_only"] and not view.live_prescription["safety_hold"]
    assert view.today.decision_tier == "modify" and view.today.recommendation_state == "modify"
    assert "loading details" in view.today.recommendation_reason
    assert store.get_today_checkin(ATHLETE, PLAN, DAY)["recommendation_state"] == "train_as_planned"
    live = view.live_prescription
    started = today_service.upsert_session_completion(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW,
        payload={"plan_id": PLAN, "session_id": live["session"]["session_id"], "status": "started", "prescription_revision": live["revision"]})
    assert started["prescription_snapshot"]["session"]["session_type"] == "rehab"


@pytest.mark.parametrize("block_type", [
    "preparation", "mobility_activation", "plyometric_power", "speed", "strength",
    "strength_speed", "accessory", "conditioning", "cooldown_recovery", "nutrition", "mindset",
])
def test_no_contact_alone_does_not_hold_fitness_or_support_without_contact_metadata(reviewed, block_type):
    decision = decide(reviewed)
    decision["restrictions"] = {"blocked_regions": [], "blocked_tags": [], "contact_limit": "none"}
    block = {"block_id": "b", "block_type": block_type, "display_name": "Work"}
    session = {"session_id": "s", "session_type": "strength", "blocks": [block]}
    snapshot = reconcile_session_prescription(session, decisions=[decision], plan_id=PLAN, training_day=DAY)
    assert snapshot["session"]["session_id"] == "s"
    assert snapshot["session"]["blocks"][0] == block
    assert not snapshot["safety_hold"] and not snapshot["rehab_only"]
    assert not snapshot["changes"]


@pytest.mark.parametrize("block_type", ["strength", "conditioning", "preparation", "mobility_activation", "cooldown_recovery"])
@pytest.mark.parametrize("contact", ["controlled", "full", "unknown", {}, 1])
def test_no_contact_still_holds_explicit_contact_or_unknown_demands(reviewed, contact, block_type):
    decision = decide(reviewed)
    decision["restrictions"] = {"blocked_regions": [], "blocked_tags": [], "contact_limit": "none"}
    block = {"block_id": "b", "block_type": block_type, "contact_level": contact}
    snapshot = reconcile_session_prescription({"session_id": "s", "blocks": [block]}, decisions=[decision], plan_id=PLAN, training_day=DAY)
    assert snapshot["rehab_only"] and snapshot["changes"][0]["action"] == "held"


@pytest.mark.parametrize("regions, held", [(["shoulder"], False), (["ankle"], True), (None, True)])
@pytest.mark.parametrize("block_type", ["strength", "conditioning", "plyometric_power", "speed", "accessory", "mobility_activation", "preparation", "cooldown_recovery"])
def test_non_contact_default_preserves_injury_loading_guard(reviewed, regions, held, block_type):
    block = {"block_id": "b", "block_type": block_type, "mechanical_load_regions": regions}
    snapshot = reconcile_session_prescription({"session_id": "s", "blocks": [block]}, decisions=[decide(reviewed)], plan_id=PLAN, training_day=DAY)
    assert snapshot["rehab_only"] is held
    assert bool(snapshot["changes"]) is held


@pytest.mark.parametrize("block_type", ["skill", "sparring", "unknown", None])
@pytest.mark.parametrize("contact", ["controlled", "full", None])
def test_no_contact_does_not_infer_partner_or_unknown_work_is_contact_free(reviewed, contact, block_type):
    decision = decide(reviewed)
    decision["restrictions"] = {"blocked_regions": [], "blocked_tags": [], "contact_limit": "none"}
    block = {"block_id": "b", "block_type": block_type, "contact_level": contact}
    snapshot = reconcile_session_prescription({"session_id": "s", "session_type": "sparring", "blocks": [block]}, decisions=[decision], plan_id=PLAN, training_day=DAY)
    assert snapshot["rehab_only"]


def test_explicit_solo_skill_remains_available_under_no_contact(reviewed):
    decision = decide(reviewed)
    decision["restrictions"] = {"blocked_regions": [], "blocked_tags": [], "contact_limit": "none"}
    block = {"block_id": "b", "block_type": "skill", "contact_level": "none"}
    snapshot = reconcile_session_prescription({"session_id": "s", "blocks": [block]}, decisions=[decision], plan_id=PLAN, training_day=DAY)
    assert not snapshot["rehab_only"] and snapshot["session"]["blocks"][0] == block


def test_equivalent_injury_row_order_keeps_the_same_prescription_revision(reviewed, monkeypatch):
    policy, bank, injury = reviewed
    second = {**injury, "id": str(uuid4()), "episode_id": str(uuid4())}
    store = FakeStore()
    store.injury_flags[ATHLETE] = [injury, second]
    store.plans[PLAN] = {"id": PLAN, "athlete_id": ATHLETE, "status": "ready", "created_at": "2026-09-01T00:00:00Z",
        "structured_plan": {"weeks": [{"phase_label": "GPP", "days": [{"date": DAY, "day_type": "rest", "sessions": []}]}]}}
    store.set_active_plan_id(ATHLETE, PLAN)
    store.upsert_today_checkin(ATHLETE, {"plan_id": PLAN, "training_day": DAY, "recommendation_state": "train_as_planned", "pain": "none"})
    monkeypatch.setattr(today_service, "load_clinical_policies", lambda: (policy,))
    monkeypatch.setattr(today_service, "get_rehab_bank", lambda: bank)
    first = today_service.build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
    store.injury_flags[ATHLETE].reverse()
    refreshed = today_service.build_today_command_view(store, athlete_id=ATHLETE, athlete_timezone="UTC", now=NOW)
    assert first.live_prescription["revision"] == refreshed.live_prescription["revision"]
