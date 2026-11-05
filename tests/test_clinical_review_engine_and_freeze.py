"""Optional shadow integration, independent clearance and immutable frozen pins."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from api.contracts.achilles_restore_load import CRITERION_ID as ACHILLES_V1
from api.contracts.clinical_progression_review import CriterionReviewRegistry
from api.contracts.clinical_review_validity import ReviewReason, evaluate_frozen_review
from api.contracts.clinician_clearance import effective_clinician_clearance
from api.contracts.injury_policy import reconcile_session_prescription
from api.contracts.rehab_assessment import instant
from api.contracts.rehab_progression import CAPTURED_FUNCTIONAL_CHECKPOINTS, evaluate_transition
from fightcamp.rehab_clinical import ClinicalPolicy, load_clinical_policies, policy_review_hash
from fightcamp.rehab_pathways import TransitionRequirement
from tests.clinical_review_fixtures import (
    NOW, SyntheticAttestations, bundle, changed_review, evaluate, lifecycle_change, protocol, selection_for,
)
from tests.test_achilles_restore_load_review import policy as achilles_policy, review_transition as achilles_transition
from tests.test_rehab_transition_engine import clinical, event, injury, policy


def engine_bundle(second=False, *, live=("calm", "restore", "load")):
    supplied = bundle(second)
    checkpoint = clinical("test_shared_review", "functional_checkpoint", checkpoint=supplied.context.criterion_id)
    current = policy(live=live)
    # Replace the synthetic clinical requirement in memory; the shipped catalog
    # never declares these test-only checkpoints.
    transition = current.transitions[0].model_copy(update={"requirements": [
        *(r for r in current.transitions[0].requirements if r.basis != "clinical"),
        TransitionRequirement.model_validate(checkpoint)]})
    draft = current.model_copy(update={"transitions": [transition, *current.transitions[1:]]})
    current = ClinicalPolicy.model_validate(draft.model_dump() | {"content_hash": policy_review_hash(draft)})
    subject = injury()
    definition = protocol(second)
    option = definition.options[0].model_copy(update={"profile_id": current.policy_id})
    definition = replace(definition, profile_ids=frozenset({current.policy_id}), options=(option,))
    registry = CriterionReviewRegistry((definition,))
    context = supplied.context.model_copy(update={"profile_id": current.policy_id, "policy_version": current.version,
        "policy_hash": current.content_hash, "athlete_id": subject["athlete_id"], "injury_id": subject["id"],
        "injury_episode_id": subject["episode_id"], "episode_started_at": instant(subject["created_at"]),
        "bank": (supplied.context.bank[0].model_copy(update={"drill_id": option.drill_id, "bank_hash": option.bank_hash}),)})
    supplied = replace(supplied, context=context, registry=registry)
    supplied = changed_review(supplied, profile_id=context.profile_id, policy_hash=context.policy_hash,
        policy_version=context.policy_version, athlete_id=context.athlete_id, injury_id=context.injury_id,
        injury_episode_id=context.injury_episode_id, selected_prescription=selection_for(option))
    return supplied, current, subject


def engine(supplied, current, subject, exposures=(), **kwargs):
    return evaluate_transition(current.transitions[0], policy=current, injury=subject, exposures=list(exposures),
        as_of=NOW, clinical_review_input=supplied, **kwargs)


@pytest.mark.parametrize("second", [False, True])
def test_shared_review_passes_only_one_requirement_engine_keeps_other_gates(second):
    supplied, current, subject = engine_bundle(second)
    empty = engine(supplied, current, subject)
    checkpoint = next(r for r in empty["requirements"] if r["requirement_id"] == "test_shared_review")
    assert checkpoint["status"] == "pass" and checkpoint["clinical_review"]["validity"] == "valid"
    assert empty["status"] == "blocked" and "no_reviewed_stage_exposure" in empty["reason_codes"]
    completed = engine(supplied, current, subject, (event(),))
    assert completed["status"] == "met" and completed["target_stage_live"]
    assert CAPTURED_FUNCTIONAL_CHECKPOINTS == frozenset({
        "achilles_restore_load_clinical_review_v1", "achilles_midportion_reported_load_permission_v1",
        "lateral_elbow_restore_load_v1", "lateral_elbow_reported_load_permission_v2", "ankle_lateral_reported_seated_load_v1", "shoulder_cuff_reported_wall_load_v1",
    })
    elbow = next(p for p in load_clinical_policies() if p.policy_id == "elbow_tendonitis")
    checkpoints = [r.checkpoint for r in elbow.transitions[0].requirements if r.kind == "functional_checkpoint"]
    # Compatibility reader remains registered, but the consumer pathway must
    # use the permission criterion rather than require the removed worksheet.
    assert checkpoints == ["lateral_elbow_reported_load_permission_v2"]


def test_approval_cannot_activate_a_shadow_target():
    supplied, current, subject = engine_bundle(live=("calm", "restore"))
    result = engine(supplied, current, subject, (event(),))
    assert result["status"] == "met" and result["target_stage_live"] is False
    assert result["reason_codes"] == ["target_stage_not_activated"]


@pytest.mark.parametrize("change", ["profile", "side", "episode", "athlete", "medical", "setback", "closed", "history"])
def test_engine_checks_its_own_context_and_does_not_trust_shadow_subject(change):
    supplied, current, subject = engine_bundle()
    kwargs = {}
    if change == "profile":
        supplied = replace(supplied, context=supplied.context.model_copy(update={"profile_id": "another-profile"}))
    elif change in {"side", "episode", "athlete"}:
        subject[{"side": "side", "episode": "episode_id", "athlete": "athlete_id"}[change]] = "right" if change == "side" else "another"
    elif change == "medical":
        subject["rehab_medical_gate"] = True
    elif change == "setback":
        subject["latest_reported_status"] = "worse"
    elif change == "closed":
        subject["status"] = "closed"
    else:
        kwargs["history_truncated"] = True
    result = engine(supplied, current, subject, (event(),), **kwargs)
    checkpoint = next(r for r in result["requirements"] if r["requirement_id"] == "test_shared_review")
    assert checkpoint["status"] != "pass" and checkpoint["reason_code"] == ReviewReason.ENGINE_CONTEXT.value
    assert result["status"] == "blocked"


def test_raw_newest_malformed_review_is_reported_by_engine():
    supplied, current, subject = engine_bundle()
    raw = supplied.reviews[0].model_dump(mode="json") | {"interpretation": {"incomplete": True}}
    result = engine(replace(supplied, reviews=(raw,)), current, subject, (event(),))
    assert result["status"] == "blocked" and "clinical_review_malformed" in result["reason_codes"]
    with pytest.raises(ValueError, match="explicit as_of"):
        evaluate_transition(current.transitions[0], policy=current, injury=subject, exposures=[], clinical_review_input=supplied)


def test_training_clearance_never_supplies_review_pass():
    supplied, current, subject = engine_bundle()
    subject["clinician_clearance"] = dict(episode_id=subject["episode_id"], scopes=["rehab", "training", "contact"])
    result = engine(replace(supplied, reviews=()), current, subject, (event(),))
    assert "clinical_review_missing" in result["reason_codes"] and result["status"] == "blocked"


@pytest.mark.parametrize("scopes", [None, ["rehab"], ["rehab", "training"]])
def test_clinical_approval_never_creates_clearance_or_overrides_its_ceiling(scopes):
    supplied, current, subject = engine_bundle()
    if scopes is not None:
        subject["clinician_clearance"] = dict(episode_id=subject["episode_id"], scopes=scopes)
    before = deepcopy(subject)
    assert engine(supplied, current, subject, (event(),))["status"] == "met"
    clearance = effective_clinician_clearance([subject])
    assert clearance is None if scopes is None else clearance["scopes"] == scopes
    assert subject == before
    if scopes is not None:
        saved = reconcile_session_prescription(dict(session_id="test-contact", session_type="sparring", title="Sparring", blocks=[]),
            decisions=[], plan_id="test-plan", training_day="2026-10-02", injuries=[subject])
        assert saved["safety_hold"]


def test_achilles_v1_has_priority_over_any_shadow_shared_review_binding():
    supplied = bundle()
    subject = dict(athlete_id="test-athlete", id="test-injury", episode_id="test-episode", side="left",
        canonical_location="achilles", injury_type="tendonitis", created_at="2026-10-01T00:00:00Z", status="monitoring")
    supplied = replace(supplied, context=supplied.context.model_copy(update={"criterion_id": ACHILLES_V1}))
    result = evaluate_transition(achilles_transition(), policy=achilles_policy(), injury=subject, exposures=[],
        as_of=NOW, clinical_review_input=supplied)
    assert result["status"] == "blocked" and result["requirements"][0]["status"] == "unknown"
    assert result["requirements"][0]["clinical_review"]["promotion_allowed"] is False
    assert result["status"] == "blocked"  # Legacy athlete-report criterion still cannot approve LOAD.


@pytest.mark.parametrize("invalidating", ["revocation", "setback", "selection_changed"])
@pytest.mark.parametrize("state", ["unstarted", "started", "completed"])
def test_frozen_work_held_without_rewriting_history(invalidating, state):
    supplied = bundle()
    approval = evaluate(supplied)
    pin = approval.pin
    history = dict(pin=pin.model_dump(mode="json"), dose_completed=dict(reps=3), exposure_id="test-exposure")
    before = deepcopy(history)
    if invalidating == "revocation":
        change = lifecycle_change(supplied)
        supplied = replace(supplied, lifecycle=(change,), trust=SyntheticAttestations.attest(supplied.reviews, (change,)))
    elif invalidating == "setback":
        from api.contracts.clinical_review_validity import ReviewSafetyEvent
        context = supplied.context
        setback = ReviewSafetyEvent(event_id="test-setback", **{k: getattr(context, k) for k in (
            "athlete_id", "injury_id", "injury_episode_id", "side")}, kind="setback", occurred_at=NOW, recorded_at=NOW)
        supplied = replace(supplied, context=context.model_copy(update={"safety_history": (setback,)}))
    else:
        option = protocol().options[0]
        from api.contracts.reviewed_prescription import materialised_selection_hash
        selected = supplied.reviews[0].selected_prescription.model_copy(update={"dose": option.dose_choices[1]})
        selected = selected.model_copy(update={"materialised_prescription_hash": materialised_selection_hash(option, selected)})
        supplied = changed_review(supplied, review_id="changed-immutable-review", selected_prescription=selected)
        assert evaluate(supplied).criterion_status == "pass"
    result = evaluate_frozen_review(pin, evaluate(supplied), work_state=state, as_of=NOW)
    assert result.can_accept is False
    assert result.safety_hold is (state != "completed")
    assert result.history_immutable is (state != "unstarted")
    assert history == before and pin == approval.pin


def test_current_frozen_pin_accepts_only_unstarted_work():
    approval = evaluate()
    assert evaluate_frozen_review(approval.pin, approval, work_state="unstarted", as_of=NOW).can_accept
    assert not evaluate_frozen_review(approval.pin, approval, work_state="started", as_of=NOW).can_accept
    with pytest.raises(ValueError, match="same explicit aware as_of"):
        evaluate_frozen_review(approval.pin, approval, work_state="unstarted", as_of=NOW + timedelta(seconds=1))


def test_production_catalog_remains_calm_restore_only():
    policies = load_clinical_policies()
    assert len(policies) == 64
    # #2984/#2986 published 130 distinct exercises; shared drills appear in
    # multiple profile prescriptions. Keep both real row and identity counts.
    assert sum(len(p.prescriptions) for p in policies) == 139
    assert len({rx.drill_id for p in policies for rx in p.prescriptions}) == 130
    assert [(p.policy_id,t.key) for p in policies for t in p.transitions if t.promotable] == [("ankle_sprain","restore->load"), ("achilles_tendonitis","restore->load"), ("shoulder_tendonitis","restore->load"), ("elbow_tendonitis","restore->load")]
    assert all(set(p.live_stages) <= {"calm", "restore"} for p in policies if p.policy_id not in {"achilles_tendonitis", "elbow_tendonitis", "ankle_sprain", "shoulder_tendonitis"})
    assert {stage for p in policies for stage in p.live_stages} == {"calm", "restore", "load"}
