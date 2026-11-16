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


def test_shipped_policies_are_sourced_active_and_self_paced():
    policies = load_clinical_policies()
    assert {p.region for p in policies} == {"chest", "ankle"}
    assert all(p.activation == "live" and p.status == "active" for p in policies)
    assert all(p.dose is None and p.sources for policy in policies for p in policy.prescriptions)
    with pytest.raises(ValidationError):
        ClinicalPolicy.model_validate({**policies[0].model_dump(), "content_hash": "a" * 64})


@pytest.mark.parametrize("side", ["left", "unknown"])
def test_chest_guidance_retains_episode_feedback_without_inventing_laterality(side):
    from fightcamp.rehab_protocols import get_rehab_bank
    row = dict(id=str(uuid4()), episode_id=str(uuid4()), athlete_id=ATHLETE, body_area="Chest", description="Chest strain",
               body_region="chest", canonical_location="chest", injury_type="strain", severity="mild", status="open", side=side)
    decision = resolve_injury_policy(row, policies=load_clinical_policies(), bank=get_rehab_bank(), phase="TAPER")
    snapshot = reconcile_session_prescription(None, decisions=[decision], plan_id=PLAN, training_day=DAY)
    completion = dict(status="done", prescription_snapshot=snapshot, rehab_performance="done_as_shown")
    items = session_rehab_items({"id": PLAN}, training_day=DAY, session_id=f"rehab-{DAY}", prescription=snapshot)
    candidates = resolve_rehab_completion(items, [row], completion=completion).eligible
    assert len(candidates) == 1 and candidates[0].side == side
    event = build_rehab_exposure_event(candidates[0], athlete_id=ATHLETE, plan_id=PLAN, session_id=f"rehab-{DAY}",
        training_day=DAY, completion=completion, during="same", limit="no")
    assert event.is_attributable_to(row)
    assert event.dose_completed.completion_state == "performed_amount_unknown"


def test_phase_changes_bounded_dose_not_eligibility(reviewed):
    results = [decide(reviewed, phase=phase) for phase in ("GPP", "SPP", "TAPER")]
    assert all(result["outcome"] == "prescribed_rehab" for result in results)
    assert len({r["prescription"]["drill_id"] for r in results}) == 1
    assert results[-1]["prescription"]["dose"]["sets"] == 1
    assert results[0]["prescription"]["dose"]["sets"] == 2


def test_readiness_requires_reviewed_reductions_and_preserves_camp_ceiling(reviewed):
    policy, bank, injury = reviewed
    assert decide(reviewed, readiness_decision="pull_back")["prescription"] is None
    raw = policy.model_dump()
    raw["prescriptions"][0]["readiness_doses"] = {"pull_back": {"sets": 2, "reps": 2}}
    reduced = reviewed_policy(raw)
    result = resolve_injury_policy(injury, policies=(reduced,), bank=bank, phase="TAPER", readiness_decision="pull_back")
    assert result["prescription"]["dose"] == {"sets": 1, "reps": 2}
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


def test_retired_policy_holds_work_instead_of_resuming_legacy_prescriptions(reviewed):
    policy, bank, injury = reviewed
    retired = ClinicalPolicy.model_validate({**policy.model_dump(), "status": "retired", "activation": "shadow"})
    decision = resolve_injury_policy(injury, policies=(retired,), bank=bank)
    assert decision["outcome"] == "medical_review" and decision["prescription"] is None
    assert reconcile_session_prescription({"session_id": "s", "blocks": []}, decisions=[decision], plan_id=PLAN, training_day=DAY)["safety_hold"]


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
    assert snapshot["session"]["session_id"] == f"rehab-{DAY}"
    assert snapshot["session"]["blocks"][0]["injury_episode_id"] == decision["injury_episode_id"]
    unknown = {"session_id": "s-1", "blocks": [{"block_id": "b-1", "block_type": "strength"}]}
    original = deepcopy(unknown)
    held = reconcile_session_prescription(unknown, decisions=[decision], plan_id=PLAN, training_day=DAY)
    assert not held["safety_hold"] and held["changes"][0]["action"] == "held"
    assert held["rehab_only"] and held["held_session_id"] == "s-1"
    assert all(block.get("policy_id") for block in held["session"]["blocks"])
    assert unknown == original


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


def test_latest_clearance_report_replaces_older_broader_scope(reviewed):
    injury = reviewed[2]
    events = [{"id": "old", "injury_id": injury["id"], "injury_episode_id": injury["episode_id"],
               "event_type": "clinician_clearance_report", "created_at": "2026-09-29T12:00:00Z",
               "payload": {"scopes": ["rehab", "training", "contact"]}},
              {"id": "new", "injury_id": injury["id"], "injury_episode_id": injury["episode_id"],
               "event_type": "clinician_clearance_report", "created_at": "2026-09-30T12:00:00Z",
               "payload": {"scopes": ["rehab", "training"]}}]
    for ordered in (events, list(reversed(events))):
        report = apply_episode_observations(injury, ordered)["clinician_clearance"]
        assert report["scopes"] == ["rehab", "training"]
        assert set(report["scope_reported_at"].values()) == {"2026-09-30T12:00:00Z"}


@pytest.mark.parametrize("block_type", ["nutrition", "mindset"])
def test_nonphysical_support_does_not_require_exercise_loading_metadata(reviewed, block_type):
    block = {"block_id": "b", "block_type": block_type}
    result = reconcile_session_prescription({"session_id": "s", "blocks": [block]}, decisions=[decide(reviewed)], plan_id=PLAN, training_day=DAY)
    assert not result["safety_hold"] and not result["rehab_only"]
    assert result["session"]["blocks"][0] == block


def test_no_contact_removes_coach_contact_without_holding_unaffected_fitness(reviewed):
    session = {"session_id": "s", "coach_led_contact": "Hard sparring", "blocks": [
        {"block_id": "b", "block_type": "strength", "mechanical_load_regions": ["shoulder"]}]}
    result = reconcile_session_prescription(session, decisions=[decide(reviewed)], plan_id=PLAN, training_day=DAY)
    assert not result["safety_hold"] and not result["rehab_only"]
    assert "coach_led_contact" not in result["session"]
    assert result["changes"][0]["action"] == "contact_held"


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


def test_held_training_still_offers_separate_phase_rehab(reviewed):
    decision = decide(reviewed, readiness_decision="pull_back")
    assert decision["outcome"] == "prescribed_rehab"
    assert not decision["loading_hold"]
    snapshot = reconcile_session_prescription(
        {"session_id": "strength", "session_type": "strength", "blocks": [
            {"block_id": "safe-strength", "block_type": "strength", "mechanical_load_regions": ["shoulder"]}]},
        decisions=[decision], plan_id=PLAN, training_day=DAY, training_held=True,
    )
    assert snapshot["rehab_only"] and not snapshot["safety_hold"]
    assert snapshot["session"]["session_id"] == f"rehab-{DAY}"
    assert all(block["block_type"] == "rehab" for block in snapshot["session"]["blocks"])


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
    items = session_rehab_items(plan, training_day=DAY, session_id=f"rehab-{DAY}", prescription=snapshot)
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
    payload = {"plan_id": PLAN, "session_id": f"rehab-{DAY}", "status": "started", "prescription_revision": "a" * 64}
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
    assert "Test control drill" in text and "1 x 4 reps" in text
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
    setback = {"injury_id": injury["id"], "injury_episode_id": injury["episode_id"], "event_type": "injury_checkin",
               "created_at": "2026-10-01T00:00:00Z", "payload": {"latest_reported_status": "worse"}}
    before = {**saved, "created_at": "2026-09-30T12:00:00Z"}
    assert "clinician_clearance" not in apply_episode_observations(injury, [before, setback])
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
    event = {"injury_id": injury["id"], "injury_episode_id": injury["episode_id"], "body_region": "ankle", "occurred_at": "2026-09-28T12:00:00Z", "response": {"next_day_response": "not_yet_known"}}
    store.rehab_exposures[exposure_id] = {"id": exposure_id, "athlete_id": ATHLETE, "event_json": event}
    assert delayed_rehab_prompts(store, ATHLETE, DAY)
    observation = InjuryEpisodeObservation(injury_id=injury["id"], injury_episode_id=injury["episode_id"], event_type="delayed_rehab_response", exposure_id=exposure_id, response="same")
    record_episode_observation(store, athlete_id=ATHLETE, observation=observation, training_day=DAY)
    assert not delayed_rehab_prompts(store, ATHLETE, DAY)
    assert event["response"]["next_day_response"] == "not_yet_known"
    record_episode_observation(store, athlete_id=ATHLETE, observation=observation, training_day=DAY)
    with pytest.raises(HTTPException):
        record_episode_observation(store, athlete_id=ATHLETE, observation=observation.model_copy(update={"response": "worse"}), training_day=DAY)


@pytest.mark.parametrize("report_limit", [False, True])
def test_active_chest_rest_day_direct_completion_and_feedback_through_api(report_limit):
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
    body = dict(plan_id=PLAN, session_id=live["session"]["session_id"], prescription_revision=live["revision"])
    completed = client.post("/api/today/session-completion", headers=headers, json={**body, "status": "done", "rehab_performance": "done_as_shown"})
    assert completed.status_code == 201, completed.text
    assert completed.json()["rehab_response_prompts"][0]["injury_id"] == row["id"]
    answer = dict(injury_id=row["id"], injury_episode_id=row["episode_id"], during_response="same")
    if report_limit:
        answer["limit_response"] = "no"
    answered = client.post("/api/today/rehab-responses", headers=headers, json={"plan_id": PLAN, "session_id": body["session_id"],
        "answers": [answer]})
    assert answered.status_code == 201, answered.text
    event = next(iter(store.rehab_exposures.values()))["event_json"]
    assert event["side"] == "unknown" and event["dose_completed"]["completion_state"] == "performed_amount_unknown"
    assert event["response"]["during_response"] == "same"
    assert event["response"].get("stopped_due_to_symptoms") is (False if report_limit else None)
    assert event["response"].get("next_day_response") != "same"
    assert client.get("/api/today", headers=headers).json()["open_injuries"][0]["rehab_decision"]["schedule"]["state"] == "already_completed"
