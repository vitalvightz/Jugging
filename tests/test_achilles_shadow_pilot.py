"""Real production criterion/option through the existing trusted capture channel."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.contracts.achilles_restore_load_pilot import (
    ACHILLES_LOAD_OPTION as OPTION, CLINICAL_SCOPE, CRITERION_ID, JUDGMENTS, PILOT_SOURCES,
    AchillesRestoreLoadInterpretationV1, evaluate_achilles_interpretation,
)
from api.contracts.clinical_progression_review import CLINICAL_REVIEW_REGISTRY, UNSUPPORTED_REVIEW_TRUST
from api.contracts.clinical_review_capture import ClinicalReviewCaptureRequest, ClinicalReviewLifecycleRequest
from api.contracts.clinical_review_validity import evaluate_clinical_review, evaluate_frozen_review
from api.contracts.clinician_clearance import effective_clinician_clearance
from api.contracts.injury_policy import resolve_injury_policy
from api.contracts.rehab_assessment import AssessmentContext, assessment_payload, read_assessment_input
from api.contracts.achilles_restore_load import REQUIRED_INPUTS
from api.contracts.rehab_progression import evaluate_transition, resolve_reviewed_progression
from api.contracts.reviewed_prescription import ReviewedPrescriptionSelection, materialised_selection_hash
from api.services.clinical_review_capture_service import (
    build_review_context, hydrate_review_input, prepare_review_packet, record_clinical_review, record_review_lifecycle,
)
from api.services.injury_episode_service import apply_episode_observations
from fightcamp.rehab_clinical import content_hash, load_clinical_policies, compose_policy, load_pathway_catalog
from fightcamp.rehab_protocols import get_rehab_bank
from fightcamp.rehab_pathways import TransitionRequirement
from tests.clinical_capture_fixtures import capture_bundle
from tests.test_achilles_progression_inputs import NOW, assessment
from tests.test_rehab_transition_engine import event as exposure_event


def shadow_bundle(*, side="left", **changes):
    store, recorder, original, _ = capture_bundle()
    injury = store.snapshot["injury"]
    injury.update(canonical_location="achilles", body_region="achilles", injury_type="tendonitis",
                  side=side, body_area=f"{side.title()} Achilles", description=f"{side.title()} Achilles tendonitis")
    # Preserve the pre-activation shadow contract. The live athlete workflow
    # now uses self-reported permissions; it does not register trusted reviews
    # as a gate. These tests continue to exercise the retained private machinery.
    import json
    from pathlib import Path
    historical = json.loads((Path(__file__).parents[1] / "tools/rehab_achilles_activation_inventory_baseline.json").read_text(encoding="utf-8"))["historical_profile"]
    policy = compose_policy(load_pathway_catalog(), historical)
    bank = get_rehab_bank()
    value = assessment(**dict(side=side, site="midportion", suspected_rupture=False, marked_weakness=False,
                             traumatic_loss_of_function=False, clinician_restriction=False) | changes)
    observed = dict(id=str(uuid4()), athlete_id=injury["athlete_id"], injury_id=injury["id"],
        injury_episode_id=injury["episode_id"], event_type="rehab_progression_assessment",
        created_at="2026-10-05T16:00:00Z", payload=assessment_payload(value, injury, as_of=NOW))
    store.snapshot["events"].append(observed)
    restore = next(d for g in bank for d in g["drills"] if d["id"] == policy.prescriptions[1].drill_id)
    exposure = exposure_event(drill=restore, side=side, policy_id=policy.policy_id, bank_hash=policy.prescriptions[1].bank_hash)
    exposure.update(injury_id=injury["id"], injury_episode_id=injury["episode_id"])
    exposure["event_json"]["body_region"] = "achilles"
    exposure["event_json"]["demand"]["target_regions"] = ["achilles"]
    store.snapshot["exposures"] = [exposure]
    definition = CLINICAL_REVIEW_REGISTRY.current(CRITERION_ID)
    context = build_review_context(store.snapshot, definition=definition, policy=policy, bank=bank, as_of=NOW)
    selected = ReviewedPrescriptionSelection(option_id=OPTION.option_id, option_version=OPTION.option_version,
        option_hash=OPTION.option_hash, drill_id=OPTION.drill_id, bank_hash=OPTION.bank_hash,
        range_choice=OPTION.range_choices[0], resistance=dict(mode="bodyweight"), dose=OPTION.dose_choices[0],
        cadence=OPTION.cadence_choices[0], restrictions=tuple(sorted(OPTION.mandatory_restrictions)),
        materialised_prescription_hash="0"*64)
    selected = selected.model_copy(update={"materialised_prescription_hash": materialised_selection_hash(OPTION, selected)})
    interpretation = AchillesRestoreLoadInterpretationV1(subtype="midportion", assessment_event_id=observed["id"],
        assessment_content_hash=content_hash(observed["payload"]), selected_prescription_hash=selected.materialised_prescription_hash,
        **dict.fromkeys(JUDGMENTS, "supported"))
    statement = original.statement.model_dump(mode="json") | dict(clinical_scope=CLINICAL_SCOPE,
        reviewed_at="2026-10-05T17:00:00Z", confirmed_at="2026-10-05T18:00:00Z",
        reviewed_packet_revision=context.current_packet.packet_revision,
        interpretation=interpretation.model_dump(mode="json"),
        selection=selected.model_dump(mode="json", include={
            "option_id", "option_version", "range_choice", "resistance", "dose", "cadence", "restrictions"}))
    request = ClinicalReviewCaptureRequest(request_id=uuid4(), athlete_id=injury["athlete_id"], injury_id=injury["id"],
        injury_episode_id=injury["episode_id"], criterion_id=CRITERION_ID, criterion_version=1, statement=statement)
    return store, recorder, request, dict(policies=(policy,), bank=bank, as_of=NOW)


def write(bundle):
    store, recorder, request, kwargs = bundle
    return record_clinical_review(store, recorder=recorder, request=request, **kwargs)


def replay(bundle):
    store, _, request, kwargs = bundle
    supplied = hydrate_review_input(store.snapshot, criterion_id=request.criterion_id, criterion_version=1, **kwargs)
    result = evaluate_clinical_review(supplied.context, supplied.reviews, as_of=kwargs["as_of"],
        lifecycle=supplied.lifecycle, registry=supplied.registry, trust=supplied.trust)
    return supplied, result


def engine(bundle, supplied):
    store, _, _, kwargs = bundle
    injury = apply_episode_observations(store.snapshot["injury"], store.snapshot["events"], as_of=kwargs["as_of"])
    policy = shadow_policy(kwargs["policies"][0])
    return evaluate_transition(policy.transitions[0], policy=policy, injury=injury,
        exposures=store.snapshot["exposures"], as_of=kwargs["as_of"], clinical_review_input=supplied), injury


def shadow_policy(policy):
    """Isolated engine fixture: compiled production rule, unchanged live policy."""
    transition = policy.transitions[0].model_copy(update={
        "closed_reason":"Shadow pilot only; production LOAD remains disabled.",
        "requirements":[*policy.transitions[0].requirements,
            *(TransitionRequirement(requirement_id=k, kind="input_availability", basis="data_sufficiency",
                checkpoint=k, description="Data sufficiency only.") for k in REQUIRED_INPUTS),
            TransitionRequirement(requirement_id=CRITERION_ID, kind="functional_checkpoint", basis="clinical",
                checkpoint=CRITERION_ID, description="Trusted exact Achilles interpretation and bounded work.",
                sources=list(PILOT_SOURCES))]})
    return policy.model_copy(update={"transitions":[transition,*policy.transitions[1:]], "live_stages":["calm","restore"]})


def test_production_shadow_complete_chain_pass_stays_restore_and_never_schedules_load():
    bundle = shadow_bundle()
    store, _, request, kwargs = bundle
    result = write(bundle)
    assert store.writes == 1 and str(result.review_id) == store.snapshot["events"][-1]["id"]
    supplied, evaluation = replay(bundle)
    assert evaluation.validity == "valid" and evaluation.trusted and evaluation.prescription_valid
    assert evaluation.criterion_status == "pass"
    transition, injury = engine(bundle, supplied)
    assert all(r["status"] == "pass" for r in transition["requirements"])
    assert transition["status"] == "closed" and not transition["target_stage_live"]
    assert all(read_assessment_input(k, AssessmentContext.from_injury(injury, as_of=NOW))["status"] == "pass"
               for k in REQUIRED_INPUTS)
    pilot = shadow_policy(kwargs["policies"][0])
    resolved = resolve_reviewed_progression(injury, base_stage="restore", policy=pilot,
        exposures=store.snapshot["exposures"], as_of=NOW, clinical_review_inputs={CRITERION_ID: supplied})
    assert resolved["stage"] == "restore"
    live_resolved = resolve_reviewed_progression(injury, base_stage="restore", policy=kwargs["policies"][0],
        exposures=store.snapshot["exposures"], as_of=NOW, clinical_review_inputs={CRITERION_ID:supplied})
    assert live_resolved["stage"] == "restore" and not live_resolved["next_transition"]["target_stage_live"]
    decision = resolve_injury_policy(injury, policies=(pilot,), bank=kwargs["bank"],
        exposures=store.snapshot["exposures"], as_of=NOW, clinical_review_inputs={CRITERION_ID: supplied})
    assert decision["stage"] == "restore" and decision["prescription"]["drill_id"] != OPTION.drill_id
    assert effective_clinician_clearance([injury]) is None
    assert evaluation.pin.criterion_version == evaluation.pin.option_version == evaluation.pin.selection_version == 1


@pytest.mark.parametrize("side,status", [("right", "pass"), ("bilateral", "unknown")])
def test_one_exact_side_only(side, status):
    bundle = shadow_bundle(side=side)
    write(bundle)
    assert replay(bundle)[1].criterion_status == status


def test_today_with_real_trusted_review_schedules_only_restore(monkeypatch):
    import json
    from api.services import today_service
    bundle = shadow_bundle()
    write(bundle)
    store, _, request, kwargs = bundle
    monkeypatch.setattr(today_service, "load_clinical_policies", lambda: (shadow_policy(kwargs["policies"][0]),))
    monkeypatch.setattr(today_service, "get_rehab_bank", lambda: kwargs["bank"])
    row = today_service._with_injury_policy([store.snapshot["injury"]], store=store,
        athlete_id=str(request.athlete_id), as_of=NOW)[0]
    decision = row["rehab_decision"]
    assert decision["stage"] == "restore" and decision["prescription"]["drill_id"] != OPTION.drill_id
    clinical = next(r for r in decision["progression"]["next_transition"]["requirements"]
                    if r["requirement_id"] == CRITERION_ID)
    assert clinical["status"] == "pass"
    assert request.statement.statement_text not in json.dumps(row, default=str)
    assert request.statement.confirmation_reference not in json.dumps(row, default=str)
    assert request.statement.qualification_reference not in json.dumps(row, default=str)


@pytest.mark.parametrize("scopes", [["rehab","training"], ["rehab","training","contact"]])
def test_clearance_alone_cannot_supply_review_and_approval_does_not_expand_clearance(scopes):
    bundle = shadow_bundle()
    store = bundle[0]
    injury = store.snapshot["injury"]
    injury["clinician_clearance"] = dict(episode_id=injury["episode_id"], scopes=scopes)
    # Clearance is independent. Updating the packet requires a new clinical decision.
    context = build_review_context(store.snapshot, definition=CLINICAL_REVIEW_REGISTRY.current(CRITERION_ID),
        policy=bundle[3]["policies"][0], bank=bundle[3]["bank"], as_of=NOW)
    raw = bundle[2].model_dump(mode="json")
    raw["statement"]["reviewed_packet_revision"] = context.current_packet.packet_revision
    bundle = (*bundle[:2], ClinicalReviewCaptureRequest.model_validate(raw), bundle[3])
    supplied, evaluated = replay(bundle)
    assert evaluated.criterion_status == "unknown"
    before = effective_clinician_clearance([injury])
    write(bundle)
    assert replay(bundle)[1].criterion_status == "pass"
    assert effective_clinician_clearance([injury]) == before


@pytest.mark.parametrize("field,value", [("rehab_medical_gate",True), ("latest_reported_status","worse"),
    ("episode_id", "replaced-episode"), ("side","right"), ("status","resolved")])
def test_engine_own_safety_context_dominates_valid_shadow_pass(field, value):
    bundle = shadow_bundle()
    write(bundle)
    supplied, evaluation = replay(bundle)
    assert evaluation.criterion_status == "pass"
    bundle[0].snapshot["injury"][field] = value
    transition, _ = engine(bundle, supplied)
    clinical = next(r for r in transition["requirements"] if r["requirement_id"] == CRITERION_ID)
    assert clinical["status"] != "pass" and transition["status"] == "closed"


@pytest.mark.parametrize("field", JUDGMENTS)
@pytest.mark.parametrize("value,status", [("not_supported", "fail"), ("unknown", "unknown")])
def test_each_clinical_judgment_matters_in_persisted_approved_review(field, value, status):
    bundle = shadow_bundle()
    raw = bundle[2].model_dump(mode="json")
    raw["statement"]["interpretation"][field] = value
    bundle = (*bundle[:2], ClinicalReviewCaptureRequest.model_validate(raw), bundle[3])
    write(bundle)
    assert replay(bundle)[1].criterion_status == status


@pytest.mark.parametrize("changes", [
    dict(site="unknown"), dict(site="insertional"), dict(suspected_rupture=True), dict(marked_weakness=True),
    dict(traumatic_loss_of_function=True), dict(incompatible_pathology="suspected"), dict(clinician_restriction=True),
    dict(suspected_rupture=None), dict(incompatible_pathology="unknown"),
    dict(heel_rise_completed=None, heel_rise_mode="unknown", heel_rise_quality="unknown",
         heel_rise_repetitions=None, heel_rise_assessor_usable=None),
    dict(heel_rise_repetitions=None), dict(heel_rise_quality="unable", heel_rise_assessor_usable=False),
    dict(during_symptoms=None), dict(delayed_symptoms=None, delayed_response_at=None),
    dict(range_assessed=None, permitted_range="unknown", range_load_assessor_usable=None),
    dict(permitted_range="clinician_limited"), dict(resistance="external", resistance_kg=1.0),
    dict(range_load_tolerance="not_tolerated"), dict(assessor="self_reported"), dict(assessor="coach_observed"),
])
def test_actual_observation_cannot_be_overridden_by_all_positive_judgments(changes):
    bundle = shadow_bundle(**changes)
    try:
        write(bundle)
    except HTTPException as exc:
        assert exc.status_code == 409 and bundle[0].writes == 0
    else:
        assert replay(bundle)[1].criterion_status in {"unknown", "fail"}


@pytest.mark.parametrize("field,value", [
    ("assessment_event_id", "old-observation"), ("assessment_content_hash", "0"*64),
    ("selected_prescription_hash", "0"*64), ("schema_version", 2), ("subtype", "insertional"),
])
def test_interpretation_exact_binding_and_version(field, value):
    bundle = shadow_bundle()
    raw = bundle[2].model_dump(mode="json")
    raw["statement"]["interpretation"][field] = value
    bundle = (*bundle[:2], ClinicalReviewCaptureRequest.model_validate(raw), bundle[3])
    write(bundle)
    assert replay(bundle)[1].criterion_status == "unknown"


@pytest.mark.parametrize("payload", [{}, {"trusted":True}, {"subtype":"midportion", "function_adequate":True}])
def test_no_arbitrary_pass_payload(payload):
    if payload:
        with pytest.raises(ValidationError):
            AchillesRestoreLoadInterpretationV1.model_validate(payload)
    else:
        assert evaluate_achilles_interpretation(AchillesRestoreLoadInterpretationV1()).status == "unknown"


@pytest.mark.parametrize("mutation", ["option", "option_version", "dose", "range", "resistance", "cadence", "restriction"])
def test_capture_rejects_unreviewed_selection(mutation):
    bundle = shadow_bundle()
    raw = bundle[2].model_dump(mode="json")
    selected = raw["statement"]["selection"]
    if mutation == "option":
        selected["option_id"] = "arbitrary_drill"
    elif mutation == "option_version":
        selected["option_version"] = 2
    elif mutation == "dose":
        selected["dose"]["reps"] = 11
    elif mutation == "range":
        selected["range_choice"] = "below_floor"
    elif mutation == "resistance":
        selected["resistance"] = dict(mode="external_kg", kg=1.0)
    elif mutation == "cadence":
        selected["cadence"]["minimum_gap_days"] = 2
    else:
        selected["restrictions"].pop()
    bundle = (*bundle[:2], ClinicalReviewCaptureRequest.model_validate(raw), bundle[3])
    with pytest.raises(HTTPException):
        write(bundle)
    assert bundle[0].writes == 0


def test_shadow_capture_cannot_bind_an_undeclared_live_target():
    bundle = shadow_bundle()
    current = bundle[3]["policies"][0]
    undeclared = current.transitions[0].model_copy(update={"requirements":
        [r for r in current.transitions[0].requirements if r.checkpoint != CRITERION_ID]})
    bundle[3]["policies"] = (current.model_copy(update={"live_stages":["calm", "restore", "load"], "transitions":[undeclared,*current.transitions[1:]]}),)
    with pytest.raises(HTTPException, match="clinical criterion does not match this transition"):
        write(bundle)
    assert bundle[0].writes == 0


@pytest.mark.parametrize("change", ["policy", "bank", "option"])
def test_packet_change_before_capture_requires_a_new_clinician_decision(change):
    bundle = shadow_bundle()
    kwargs = bundle[3]
    if change == "policy":
        kwargs["policies"] = (kwargs["policies"][0].model_copy(update={"content_hash":"0"*64}),)
    elif change == "bank":
        kwargs["bank"] = deepcopy(kwargs["bank"])
        drill = next(d for g in kwargs["bank"] for d in g["drills"] if d["id"] == OPTION.drill_id)
        drill["notes"] += " changed"
    else:
        from api.contracts.clinical_progression_review import CriterionReviewRegistry
        definition = CLINICAL_REVIEW_REGISTRY.current(CRITERION_ID)
        kwargs["registry"] = CriterionReviewRegistry((replace(definition,
            options=(OPTION.model_copy(update={"option_version":2}),)),))
    with pytest.raises(HTTPException, match="reviewed evidence packet changed"):
        write(bundle)
    assert bundle[0].writes == 0


@pytest.mark.parametrize("mutation", ["athlete_claim", "untrusted", "episode", "side", "criterion_version", "policy",
    "bank", "setback", "assessment", "medical", "restriction", "history", "closed_episode", "expiry",
    "revoke", "supersede", "packet", "changed_option", "changed_schema"])
def test_invalidation_dominates_production_criterion_and_frozen_pin(mutation):
    bundle = shadow_bundle()
    write(bundle)
    store, recorder, request, kwargs = bundle
    saved = deepcopy(store.snapshot["events"][-1])
    supplied, old = replay(bundle)
    now = NOW + timedelta(hours=1)
    kwargs["as_of"] = now
    if mutation in {"revoke", "supersede"}:
        action = ClinicalReviewLifecycleRequest(request_id=uuid4(), athlete_id=request.athlete_id,
            injury_id=request.injury_id, injury_episode_id=request.injury_episode_id, review_id=old.review_id,
            action="revoke", effective_at=NOW, confirmation_reference="withdrawal", reason="Clinician withdrew approval")
        record_review_lifecycle(store, recorder=recorder, request=action, as_of=NOW)
        supplied, result = replay(bundle)
        if mutation == "supersede":
            raw = request.model_dump(mode="json")
            raw.update(request_id=str(uuid4()), supersedes_review_id=old.review_id)
            raw["statement"]["decision"] = "deferred"
            replacement = ClinicalReviewCaptureRequest.model_validate(raw)
            record_clinical_review(store, recorder=recorder, request=replacement, **kwargs)
            supplied, result = replay(bundle)
    elif mutation in {"setback", "assessment", "medical"}:
        e = deepcopy(store.snapshot["events"][1])
        e.update(id=str(uuid4()), created_at=(NOW+timedelta(minutes=1)).isoformat())
        if mutation == "setback":
            e.update(event_type="injury_checkin", payload=dict(latest_reported_status="worse"))
        elif mutation == "medical":
            e["payload"]["assessment"]["payload"]["suspected_rupture"] = True
            e["payload"]["medical_concern"] = True
        store.snapshot["events"].append(e)
        supplied, result = replay(bundle)
    elif mutation in {"policy", "bank", "restriction", "closed_episode", "changed_option", "changed_schema"}:
        if mutation == "policy":
            kwargs["policies"] = (kwargs["policies"][0].model_copy(update={"content_hash":"0"*64}),)
        elif mutation == "bank":
            kwargs["bank"] = deepcopy(kwargs["bank"])
            d = next(d for g in kwargs["bank"] for d in g["drills"] if d["id"] == OPTION.drill_id)
            d["notes"] += " changed"
        elif mutation in {"changed_option", "changed_schema"}:
            from api.contracts.clinical_progression_review import CriterionReviewRegistry
            definition = CLINICAL_REVIEW_REGISTRY.current(CRITERION_ID)
            if mutation == "changed_option":
                definition = replace(definition, options=(OPTION.model_copy(update={"option_version":2}),))
            else:
                class RevisedInterpretation(AchillesRestoreLoadInterpretationV1):
                    additional_judgment: str = "unknown"
                definition = replace(definition, payload_type=RevisedInterpretation)
            kwargs["registry"] = CriterionReviewRegistry((definition,))
        else:
            store.snapshot["injury"].update({"restriction_hold":True} if mutation == "restriction" else {"status":"resolved"})
        supplied, result = replay(bundle)
    else:
        context = supplied.context
        reviews = deepcopy(supplied.reviews)
        if mutation == "untrusted":
            supplied = replace(supplied, trust=UNSUPPORTED_REVIEW_TRUST)
        elif mutation in {"episode", "side", "criterion_version"}:
            reviews[0][{"episode":"injury_episode_id", "side":"side", "criterion_version":"criterion_version"}[mutation]] = {
                "episode":str(uuid4()), "side":"right", "criterion_version":2}[mutation]
        elif mutation == "athlete_claim":
            reviews[0]["provenance"]["source"] = "athlete_reported"
        elif mutation == "expiry":
            reviews[0].update(valid_until=NOW.isoformat(), expiry_reason="Clinician specified reassessment")
        elif mutation == "history":
            context = context.model_copy(update={"history_complete":False})
        else:
            context = context.model_copy(update={"current_packet":context.current_packet.model_copy(update={"packet_revision":"0"*64})})
        supplied = replace(supplied, context=context, reviews=reviews)
        result = evaluate_clinical_review(context, reviews, as_of=now, registry=supplied.registry, trust=supplied.trust)
    assert result.criterion_status != "pass"
    assert evaluate_frozen_review(old.pin, result, work_state="unstarted", as_of=now).safety_hold
    assert not evaluate_frozen_review(old.pin, result, work_state="completed", as_of=now).safety_hold
    assert saved in store.snapshot["events"]


def test_packet_exposes_exact_relevant_episode_and_current_bounded_work():
    store, recorder, request, kwargs = shadow_bundle()
    packet = prepare_review_packet(store, recorder=recorder, athlete_id=request.athlete_id,
        injury_id=request.injury_id, injury_episode_id=request.injury_episode_id,
        criterion_id=CRITERION_ID, criterion_version=1, **kwargs)
    assert packet["injury"] == store.snapshot["injury"]
    assert packet["exposures"] == store.snapshot["exposures"]
    assert packet["reviewed_options"] == [OPTION.model_dump(mode="json") | {"option_hash":OPTION.option_hash}]
    assert packet["interpretation_schema"] == AchillesRestoreLoadInterpretationV1.model_json_schema()
    assert packet["context"]["assessment_events"][0]["payload"]["assessment"]["payload"]["delayed_symptoms"] == 4.0
    for key in ("policy_hash", "policy_version", "bank", "medical_hold", "restriction_hold", "safety_history"):
        assert key in packet["context"]


def test_bank_identity_display_mechanics_and_all_live_inventory_unchanged():
    policies = load_clinical_policies()
    bank = get_rehab_bank()
    drill = next(d for g in bank for d in g["drills"] if d["id"] == OPTION.drill_id)
    assert drill["name"] == "Floor-level controlled Achilles lowering"
    assert drill["notes"] == OPTION.instructions and content_hash(drill) == OPTION.bank_hash
    assert len(policies) == 64
    # #2984/#2986 intentionally expanded published rehab to 130 exercises.
    # Preserve the identity/mechanics checks and account for real bank IDs.
    published_ids = {rx.drill_id for p in policies for rx in p.prescriptions}
    assert len(published_ids) == 130
    assert published_ids <= {d["id"] for group in bank for d in group["drills"]}
    assert all(set(p.live_stages) <= {"calm", "restore"} for p in policies if p.policy_id not in {OPTION.profile_id, "elbow_tendonitis", "ankle_sprain", "shoulder_tendonitis"})
    assert [(p.policy_id,t.key) for p in policies for t in p.transitions if t.promotable] == [("ankle_sprain","restore->load"), (OPTION.profile_id,"restore->load"), ("shoulder_tendonitis","restore->load"), ("elbow_tendonitis","restore->load")]
    assert OPTION.drill_id in {rx.drill_id for p in policies for rx in p.prescriptions}
    definition = CLINICAL_REVIEW_REGISTRY.current(CRITERION_ID)
    assert definition.profile_ids == frozenset({"achilles_tendonitis"}) and definition.requires_prescription
    assert set(CLINICAL_REVIEW_REGISTRY._definitions) == {(CRITERION_ID,1)}
