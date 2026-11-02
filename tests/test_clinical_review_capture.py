from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.contracts.clinical_progression_review import CLINICAL_REVIEW_REGISTRY, CriterionReviewRegistry
from api.contracts.clinical_review_capture import ClinicalReviewCaptureRequest, ClinicalReviewLifecycleRequest
from api.contracts.clinical_review_validity import evaluate_clinical_review
from api.contracts.rehab_progression import evaluate_transition, resolve_reviewed_progression
from api.services.clinical_review_capture_service import (
    hydrate_review_input, prepare_review_packet, record_clinical_review, record_review_lifecycle,
)
from api.services.clinical_review_freeze import frozen_review_hold, pin_reviewed_work
from api.services.clinical_review_trust import PersistedClinicalReviewTrust
from api.services.injury_episode_service import InjuryEpisodeObservation
from tests.clinical_capture_fixtures import capture_bundle
from tests.clinical_review_fixtures import NOW


def write(store, recorder, request, kwargs):
    return record_clinical_review(store, recorder=recorder, request=request, **kwargs)


def replay(store, request, kwargs):
    supplied = hydrate_review_input(store.snapshot, criterion_id=request.criterion_id,
        criterion_version=request.criterion_version, **kwargs)
    return supplied, evaluate_clinical_review(supplied.context, supplied.reviews, as_of=kwargs["as_of"],
        lifecycle=supplied.lifecycle, registry=supplied.registry, trust=supplied.trust)


@pytest.mark.parametrize("second", [False, True])
def test_real_channel_server_envelope_replay_and_engine(second):
    store, recorder, request, kwargs = capture_bundle(second)
    result = write(store, recorder, request, kwargs)
    row = store.snapshot["events"][-1]
    assert row["payload"]["clinical_author"]["author_id"] != recorder.athlete_id
    assert row["payload"]["provenance"]["recorder"] == row["payload"]["provenance"]["verifier"]
    assert row["payload"]["provenance"]["recorder"]["actor_id"] == recorder.athlete_id
    assert str(result.review_id) == row["id"]
    supplied, evaluated = replay(store, request, kwargs)
    assert evaluated.trusted and evaluated.validity == "valid" and evaluated.criterion_status == "pass"
    policy = kwargs["policies"][0]
    engine = evaluate_transition(policy.transitions[0], policy=policy, injury=store.snapshot["injury"],
        exposures=[], as_of=NOW, clinical_review_input=supplied)
    checkpoint = next(r for r in engine["requirements"] if r["requirement_id"] == "test_shared_review")
    assert checkpoint["status"] == "pass" and engine["status"] == "blocked" and not engine["target_stage_live"]
    resolved = resolve_reviewed_progression(store.snapshot["injury"], base_stage="restore", policy=policy,
        exposures=[], as_of=NOW, clinical_review_inputs={request.criterion_id: supplied})
    assert resolved["stage"] == "restore"
    assert CLINICAL_REVIEW_REGISTRY.get(request.criterion_id, 1) is None


@pytest.mark.parametrize("field", ["externally_verified", "trusted", "verified_clinician", "profile_id", "side",
    "policy_hash", "bank_hash", "clinical_checkpoint", "stage", "provenance", "verifier"])
def test_request_rejects_client_authority_and_derived_fields(field):
    _, _, request, _ = capture_bundle()
    for path in (None, "statement"):
        raw = request.model_dump(mode="json")
        (raw if path is None else raw[path])[field] = True
        with pytest.raises(ValidationError):
            ClinicalReviewCaptureRequest.model_validate(raw)


def test_athlete_observation_rejects_both_private_event_types():
    _, _, request, _ = capture_bundle()
    for event in ("clinical_progression_review", "clinical_progression_review_lifecycle"):
        with pytest.raises(ValidationError):
            InjuryEpisodeObservation.model_validate(dict(injury_id=request.injury_id,
                injury_episode_id=request.injury_episode_id, event_type=event))


@pytest.mark.parametrize("role,email", [("athlete", "operator@example.test"), ("admin", "outsider@example.test"), ("coach", "operator@example.test")])
def test_no_service_write_before_admin_authorisation(role, email):
    store, recorder, request, kwargs = capture_bundle()
    recorder.role, recorder.email = role, email
    with pytest.raises(HTTPException) as err:
        write(store, recorder, request, kwargs)
    assert err.value.status_code == 403 and store.writes == 0


@pytest.mark.parametrize("field,value", [("access_status", "pending"), ("health_data_consent", False),
    ("health_consent_at", None), ("health_consent_version", "old"), ("health_consent_withdrawn_at", "2026-09-01T00:00:00Z"),
    ("date_of_birth", None), ("date_of_birth", "2020-01-01"), ("terms_version", "old"), ("role", "admin")])
def test_target_compliance_not_recorders_own_consent(field, value):
    store, recorder, request, kwargs = capture_bundle()
    store.snapshot["profile"][field] = value
    with pytest.raises(HTTPException):
        write(store, recorder, request, kwargs)
    assert store.writes == 0


@pytest.mark.parametrize("field,value", [("athlete_id", "00000000-0000-0000-0000-00000000a05f"), ("id", "00000000-0000-0000-0000-00000000a060"),
    ("episode_id", "00000000-0000-0000-0000-00000000a061"), ("status", "resolved"), ("side", "right"), ("side", "unknown"),
    ("canonical_location", "elbow"), ("injury_type", "tendonitis")])
def test_exact_owned_current_known_injury_binding(field, value):
    store, recorder, request, kwargs = capture_bundle()
    store.snapshot["injury"][field] = value
    with pytest.raises((HTTPException, ValueError)):
        write(store, recorder, request, kwargs)
    assert store.writes == 0


@pytest.mark.parametrize("change", ["author_admin", "author_athlete", "author_admin_hex", "author_athlete_braces",
    "scope", "interpretation", "future", "missing_option", "arbitrary_dose", "packet"])
def test_source_actor_scope_payload_time_and_bounded_selection(change):
    store, recorder, request, kwargs = capture_bundle()
    raw = request.model_dump(mode="json")
    s = raw["statement"]
    if change.startswith("author_"):
        s["author_reference"] = recorder.athlete_id if "admin" in change else str(request.athlete_id)
        if change.endswith("_hex"):
            s["author_reference"] = s["author_reference"].replace('-','')
        elif change.endswith("_braces"):
            s["author_reference"] = '{'+s["author_reference"]+'}'
    elif change == "scope":
        s["clinical_scope"] = "unqualified_scope"
    elif change == "interpretation":
        s["interpretation"]["trusted"] = True
    elif change == "future":
        s["confirmed_at"] = (NOW + timedelta(hours=1)).isoformat()
    elif change == "missing_option":
        s["selection"] = None
    elif change == "arbitrary_dose":
        s["selection"]["dose"]["sets"] = 999
    else:
        s["reviewed_packet_revision"] = "0" * 64
    with pytest.raises(HTTPException):
        write(store, recorder, ClinicalReviewCaptureRequest.model_validate(raw), kwargs)
    assert store.writes == 0


def test_idempotency_exact_payload_changed_retry_and_withdrawal():
    store, recorder, request, kwargs = capture_bundle()
    first = write(store, recorder, request, kwargs)
    assert write(store, recorder, request, kwargs) == first and store.writes == 1
    store.snapshot["injury"]["status"] = "resolved"
    assert write(store,recorder,request,kwargs) == first and store.writes == 1
    store.snapshot["injury"]["status"] = "monitoring"
    changed = request.model_copy(update={"statement": request.statement.model_copy(update={"rationale": "Changed"})})
    with pytest.raises(HTTPException, match="409"):
        write(store, recorder, changed, kwargs)
    store.snapshot["profile"]["health_data_consent"] = False
    with pytest.raises(HTTPException):
        write(store, recorder, request, kwargs)


def test_server_packet_is_deterministic_and_does_not_advance_with_clock():
    store, recorder, request, kwargs = capture_bundle()
    first = prepare_review_packet(store, recorder=recorder, athlete_id=request.athlete_id, injury_id=request.injury_id,
        injury_episode_id=request.injury_episode_id, criterion_id=request.criterion_id, criterion_version=1, **kwargs)
    later = dict(kwargs, as_of=NOW+timedelta(hours=1))
    second = prepare_review_packet(store, recorder=recorder, athlete_id=request.athlete_id, injury_id=request.injury_id,
        injury_episode_id=request.injury_episode_id, criterion_id=request.criterion_id, criterion_version=1, **later)
    assert first["context"]["current_packet"] == second["context"]["current_packet"]
    assert "profile" not in first and all(e["event_type"] != "clinical_progression_review" for e in first["observations"])


@pytest.mark.parametrize("mutation", ["assessment", "setback", "clearance", "medical", "restriction", "exposure", "policy", "bank", "version"])
def test_later_state_invalidates_persisted_trust_without_rewriting_history(mutation):
    store, recorder, request, kwargs = capture_bundle()
    write(store, recorder, request, kwargs)
    saved = deepcopy(store.snapshot["events"][-1])
    kwargs = dict(kwargs, as_of=NOW+timedelta(hours=1))
    if mutation in {"assessment", "setback", "clearance", "medical"}:
        typ = {"assessment":"rehab_progression_assessment", "setback":"injury_checkin",
               "clearance":"clinician_clearance_report", "medical":"rehab_progression_assessment"}[mutation]
        payload = {"latest_reported_status":"worse"} if mutation == "setback" else (
            {"scopes":["rehab"]} if mutation == "clearance" else {"medical_concern":mutation == "medical"})
        store.snapshot["events"].append(dict(id=str(uuid4()), athlete_id=str(request.athlete_id), injury_id=str(request.injury_id),
            injury_episode_id=str(request.injury_episode_id), created_at=(NOW+timedelta(minutes=1)).isoformat(), event_type=typ, payload=payload))
    elif mutation == "restriction":
        store.snapshot["injury"]["restriction_hold"] = True
    elif mutation == "exposure":
        store.snapshot["exposures"].append(dict(id=str(uuid4()), athlete_id=str(request.athlete_id), injury_id=str(request.injury_id),
            injury_episode_id=str(request.injury_episode_id), created_at=(NOW+timedelta(minutes=1)).isoformat(), event_json={}))
    elif mutation == "policy":
        kwargs["policies"] = (kwargs["policies"][0].model_copy(update={"content_hash":"0"*64}),)
    elif mutation == "bank":
        kwargs["bank"] = deepcopy(kwargs["bank"])
        kwargs["bank"][0]["drills"][0]["mechanics"] = "changed"
    else:
        old = kwargs["registry"].current(request.criterion_id)
        newer = replace(old, version=2, options=(), requires_prescription=False)
        kwargs["registry"] = CriterionReviewRegistry((old,newer))
        with pytest.raises(HTTPException):
            replay(store, request, kwargs)
        return
    _, evaluation = replay(store, request, kwargs)
    assert evaluation.validity == "invalid" and evaluation.criterion_status != "pass"
    assert saved in store.snapshot["events"]


@pytest.mark.parametrize("field", ["clinical_author", "provenance", "decision", "selected_prescription"])
def test_private_persisted_attestation_is_bound_to_exact_envelope(field):
    store, recorder, request, kwargs = capture_bundle()
    write(store, recorder, request, kwargs)
    row = deepcopy(store.snapshot["events"][-1])
    row["payload"][field] = "forged"
    assert not PersistedClinicalReviewTrust.from_server_history([row]).review_hashes


def test_revoke_and_atomic_replacement_append_immutable_lifecycle():
    store, recorder, request, kwargs = capture_bundle()
    first = write(store, recorder, request, kwargs)
    action = ClinicalReviewLifecycleRequest(request_id=uuid4(), athlete_id=request.athlete_id, injury_id=request.injury_id,
        injury_episode_id=request.injury_episode_id, review_id=first.review_id, action="revoke", effective_at=NOW,
        confirmation_reference="withdrawal-confirmation", reason="Clinician withdrew the decision")
    result = record_review_lifecycle(store, recorder=recorder, request=action, as_of=NOW)
    assert record_review_lifecycle(store, recorder=recorder, request=action, as_of=NOW) == result
    assert replay(store, request, kwargs)[1].validity == "invalid"
    later = dict(kwargs, as_of=NOW+timedelta(minutes=1))
    replacement = request.model_copy(update={"request_id":uuid4(), "supersedes_review_id":first.review_id})
    write(store, recorder, replacement, later)
    assert replay(store, replacement, later)[1].validity == "valid"
    assert len([e for e in store.snapshot["events"] if e["event_type"] == "clinical_progression_review"]) == 2


def test_frozen_pins_hold_future_work_preserve_started_completed_and_mismatch():
    store, recorder, request, kwargs = capture_bundle()
    write(store, recorder, request, kwargs)
    _, evaluation = replay(store, request, kwargs)
    definition = kwargs["registry"].current(request.criterion_id)
    selection = kwargs["registry"].parse(store.snapshot["events"][-1]["payload"]).selected_prescription
    block = pin_reviewed_work(dict(injury_id=str(request.injury_id),injury_episode_id=str(request.injury_episode_id)), evaluation,
                              option=definition.options[0], selection=selection)
    frozen = dict(session=dict(blocks=[block]))
    saved = deepcopy(frozen)
    assert not frozen_review_hold(store, str(request.athlete_id), frozen, work_state="unstarted", **kwargs)
    mismatch = deepcopy(frozen)
    mismatch["session"]["blocks"][0]["clinical_review_pin"]["option_version"] = 2
    assert frozen_review_hold(store, str(request.athlete_id), mismatch, work_state="unstarted", **kwargs)
    store.snapshot["events"][-1]["clinical_capture"]["envelope_hash"] = "0"*64
    for state in ("unstarted", "started"):
        assert frozen_review_hold(store, str(request.athlete_id), frozen, work_state=state, **kwargs)
    assert not frozen_review_hold(store, str(request.athlete_id), frozen, work_state="completed", **kwargs)
    assert frozen == saved


def test_real_achilles_is_unregistered_and_cannot_use_admin_writer():
    store, recorder, request, kwargs = capture_bundle()
    kwargs["registry"] = CLINICAL_REVIEW_REGISTRY
    request = request.model_copy(update={"criterion_id":"achilles_restore_load_review_v1"})
    with pytest.raises(HTTPException, match="not registered"):
        write(store, recorder, request, kwargs)
    assert store.writes == 0


@pytest.mark.parametrize("role,email,status", [("athlete","operator@example.test",403),
    ("admin","outsider@example.test",403), ("admin","operator@example.test",422)])
def test_admin_json_route_uses_real_authorisation_dependency(role,email,status):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.dependencies import get_store,require_profile
    from api.routes.admin import build_admin_router
    store,recorder,request,_ = capture_bundle()
    recorder.role,recorder.email = role,email
    app = FastAPI()
    app.include_router(build_admin_router())
    app.dependency_overrides[get_store] = lambda:store
    app.dependency_overrides[require_profile] = lambda:recorder
    # Synthetic criteria are never registered for an authorised operator.
    response = TestClient(app).post('/api/admin/clinical-progression-reviews',json=request.model_dump(mode="json"))
    assert response.status_code == status and store.writes == 0


@pytest.mark.parametrize("decision,criterion_status", [("not_approved","fail"),("deferred","unknown")])
def test_denial_and_deferment_are_stored_exactly_without_a_prescription(decision,criterion_status):
    store,recorder,request,kwargs = capture_bundle()
    request = request.model_copy(update={"statement":request.statement.model_copy(update={"decision":decision,"selection":None})})
    write(store,recorder,request,kwargs)
    _,evaluated = replay(store,request,kwargs)
    assert evaluated.validity == "valid" and evaluated.criterion_status == criterion_status and evaluated.pin is None


def test_today_uses_raw_snapshot_for_enriched_injury_and_keeps_private_source_hidden(monkeypatch):
    import json
    import api.contracts.clinical_progression_review as contracts
    from api.services import today_service
    store,recorder,request,kwargs = capture_bundle()
    write(store,recorder,request,kwargs)
    monkeypatch.setattr(contracts,"CLINICAL_REVIEW_REGISTRY",kwargs["registry"])
    monkeypatch.setattr(today_service,"load_clinical_policies",lambda:kwargs["policies"])
    monkeypatch.setattr(today_service,"get_rehab_bank",lambda:kwargs["bank"])
    enriched = dict(store.snapshot["injury"],rehab_stage_reasons=["derived_ui_enrichment"])
    rows = today_service._with_injury_policy([enriched],store=store,athlete_id=str(request.athlete_id),as_of=NOW)
    checkpoint = next(r for r in rows[0]["rehab_decision"]["progression"]["next_transition"]["requirements"]
                      if r["requirement_id"] == "test_shared_review")
    assert checkpoint["status"] == "pass"
    assert rows[0]["rehab_decision"]["stage"] == "restore"
    assert "test-independent-confirmation" not in json.dumps(rows,default=str)
    assert "test-professional-registration" not in json.dumps(rows,default=str)
    assert request.statement.statement_text not in json.dumps(rows,default=str)


def test_exact_statement_utf8_hash_and_changed_private_text_cannot_inherit_trust():
    from hashlib import sha256
    store,recorder,request,kwargs = capture_bundle()
    text = "  Exact confirmed statement\nUnicode: é  "
    request = request.model_copy(update={"statement":request.statement.model_copy(update={"statement_text":text})})
    write(store,recorder,request,kwargs)
    row = store.snapshot["events"][-1]
    assert row["clinical_capture"]["statement_text"] == text
    assert row["payload"]["provenance"]["statement_hash"] == sha256(text.encode('utf-8')).hexdigest()
    row["clinical_capture"]["statement_text"] = "changed"
    assert not PersistedClinicalReviewTrust.from_server_history([row]).review_hashes
