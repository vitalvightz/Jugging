"""Current medical reports outrank impact labels, clearance and support work."""
from copy import deepcopy

import pytest
from fastapi import HTTPException

from api.contracts.readiness_message import active_medical_hold_reasons
from api.services import today_service
from fightcamp.injury_danger_terms import reported_medical_symptoms
from fightcamp.injury_triage import current_injury_medical_hold
from fightcamp.sparring_readiness import sparring_readiness_flags
from tests.test_clinician_clearance_today import (
    CONTACT, DAY, NOW, context as clearance_context, execute, live_injury,
    report, session, set_sessions, view,
)


@pytest.fixture
def context():
    return clearance_context.__wrapped__()


def medical_report(context, text, *, severity="mild", field="description"):
    context[0].update_injury_flag(context[3]["id"], {
        field: text, "severity": severity, "status": "monitoring",
    })


@pytest.mark.parametrize("text", [
    "worsening headache", "dizziness", "numbness", "vision changes", "neck pain",
    "retinal detachment", "orbital fracture", "cervical spine injury",
    "spinal fracture", "open fracture", "tingling", "severe tibial plateau fracture", "neck nerve pinch",
    "shortness of breath", "chest pain", "pain breathing", "neurological symptoms",
])
@pytest.mark.parametrize("impact", ["limiting", "not_limiting"])
def test_current_medical_reports_stop_today_despite_full_clearance(context, text, impact):
    report(context, CONTACT)
    medical_report(context, f"{text} [training_impact:{impact}]")
    current = view(context)
    assert current.today.decision_tier == "stop"
    assert "medical advice" in current.today.recommendation_reason.lower()
    assert current.live_prescription is None or current.live_prescription["safety_hold"]
    if text in {"neck pain", "chest pain", "pain breathing"}:
        # These reports change the offered rehab session identity. Advisory
        # warnings do not permit logging the superseded session.
        with pytest.raises(HTTPException, match="use the current session"):
            today_service.upsert_session_completion(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW,
                payload=dict(plan_id=context[2], session_id="session-1", status="started"))
        assert context[0].get_session_completion(context[1], "session-1", DAY) is None
        return
    for status in ("started", "done", "modified"):
        row = today_service.upsert_session_completion(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW,
            payload=dict(plan_id=context[2], session_id="session-1", status=status,
                         session_rpe=1, modification_reason="Reduced session"))
        assert row["status"] == status
    assert context[0].get_session_completion(context[1], "session-1", DAY)["status"] == "modified"


@pytest.mark.parametrize("field", ["body_area", "description"])
@pytest.mark.parametrize("checkin", [True, False])
def test_medical_report_holds_without_clearance_or_checkin(context, field, checkin):
    if not checkin:
        context[0].today_checkins[context[1]] = []
    medical_report(context, "dizziness [training_impact:not_limiting]", field=field)
    assert view(context).today.decision_tier == "stop"


def test_no_active_plan_still_exposes_medical_hold(context):
    medical_report(context, "vision changes")
    context[0].plans.clear()
    assert view(context).today.decision_tier == "stop"


@pytest.mark.parametrize("kind", ["mindset", "rehab"])
@pytest.mark.parametrize("text", ["dizzy", "neck nerve pinch"])
def test_medical_hold_is_not_bypassed_by_cognitive_or_reviewed_rehab_work(context, kind, text):
    if kind == "rehab":
        live_injury(context, "chest")
    else:
        set_sessions(context, [session("mindset", "Tactical watch", [dict(
            block_id="watch", block_type="mindset", display_name="Tactical watch",
            mechanical_load_regions=[], contact_level="none")])])
    report(context, CONTACT)
    medical_report(context, f"{text} [training_impact:not_limiting]")
    current = view(context)
    assert current.today.decision_tier == "stop"
    assert current.live_prescription is None or current.live_prescription["safety_hold"]


def test_new_medical_hold_preserves_frozen_acceptance_and_allows_honest_stop(context):
    live_injury(context, "ankle")
    report(context, CONTACT)
    accepted = execute(context, view(context).live_prescription)
    snapshot = deepcopy(accepted["prescription_snapshot"])
    medical_report(context, "dizziness [training_impact:not_limiting]")
    current = view(context)
    live = current.live_prescription
    assert current.today.decision_tier == "stop" and live["safety_hold"]
    assert live["session"] == snapshot["session"] and live["revision"] == snapshot["revision"]
    for status in ("started", "done", "modified"):
        row = execute(context, live, status, session_rpe=1, modification_reason="Reduced session",
                      rehab_performance="changed")
        assert row["status"] == status
    stopped = execute(context, live, "modified", modification_reason="Stopped on dizziness", rehab_performance="stopped")
    assert stopped["prescription_snapshot"] == snapshot
    assert stopped["rehab_performance"] == "stopped"


@pytest.mark.parametrize("text", [
    "no dizziness", "not dizzy", "denies numbness", "no neck pain",
    "without vision changes", "no worsening headache", "no retinal detachment",
    "without neurological symptoms", "no chest pain", "no shortness of breath",
    "not neck pain", "without worsening headache", "not light headed", "not light-headed",
    "without light headedness", "no headaches are getting worse",
])
def test_negated_medical_reports_do_not_create_holds(text):
    assert not active_medical_hold_reasons([dict(status="open", body_area="Other", description=text, severity="mild")])


def test_positive_symptom_after_negation_and_nonlimiting_label_is_still_current():
    assert reported_medical_symptoms("No dizziness yesterday but dizzy today [training_impact:not_limiting]") == ("dizziness",)


def test_resolved_medical_report_does_not_block_safe_tactical_work(context):
    set_sessions(context, [session("mindset", "Tactical watch", [])])
    medical_report(context, "dizziness")
    context[0].update_injury_flag(context[3]["id"], {"status": "resolved"})
    assert view(context).today.decision_tier == "green"


@pytest.mark.parametrize("text,severity", [
    ("retinal detachment", "mild"), ("cervical spine injury", "mild"),
    ("spinal fracture", "mild"), ("tingling", "mild"), ("tibial plateau fracture", "severe"),
    ("shortness of breath", "mild"), ("chest pain", "mild"), ("pain breathing", "mild"),
])
def test_existing_serious_triage_flags_reach_execution_and_contact(text, severity):
    assert current_injury_medical_hold("Other", text, severity)
    flags = sparring_readiness_flags({"as_of": DAY, "active_injuries": [dict(
        body_area="Other", description=text, severity=severity, status="monitoring")]})
    assert "medical_contact_restriction" in flags


def test_bare_dizziness_is_execution_hold_without_concussion_diagnosis():
    assert reported_medical_symptoms("dizziness") == ("dizziness",)
    assert not current_injury_medical_hold("Other", "dizziness", "mild")
    flags = sparring_readiness_flags({"as_of": DAY, "active_injuries": [dict(body_area="Dizziness", status="open")]})
    assert "medical_contact_restriction" in flags and "suspected_concussion" not in flags


def test_real_injury_checkin_refreshes_hold_and_resolution_releases_it(context):
    report(context, CONTACT)
    result = today_service.submit_today_injury_checkin(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW,
        payload={"injuries": [{"flag_id": context[3]["id"], "body_area": "Other",
                              "description": "dizziness [training_impact:not_limiting]", "severity": "mild", "status": "improving"}]})
    assert result["recommendation"]["recommendation_state"] == "pull_back"
    assert view(context).today.decision_tier == "stop"
    today_service.submit_today_injury_checkin(context[0], athlete_id=context[1], athlete_timezone="UTC", now=NOW,
        payload={"injuries": [{"flag_id": context[3]["id"], "status": "resolved"}]})
    assert view(context).today.decision_tier == "green"


@pytest.mark.parametrize("fields", [
    {"triage_category": "retinal_detachment_or_eye_trauma"}, {"injury_type": "acute_nerve_issue"},
    {"flags": ["numbness"]}, {"flags": ["urgent_fracture"]}, {"consequence": "neuro"},
    {"injury_type": "tibial_plateau_fracture", "severity": "severe"},
    {"label": "headaches are getting worse"}, {"label": "orbital fracture"},
    {"body_area": "neck", "description": "pain"}, {"body_area": "dizziness"},
])
@pytest.mark.parametrize("status", ["open", "monitoring", None, "resolved"])
def test_stored_or_split_medical_evidence_has_the_same_gate_in_today_and_sparring(fields, status):
    injury = {"severity": "mild", **fields}
    if status is not None:
        injury["status"] = status
    expected = status != "resolved"
    assert bool(active_medical_hold_reasons([injury])) == expected
    flags = sparring_readiness_flags({"as_of": DAY, "active_injuries": [injury]})
    assert ("medical_contact_restriction" in flags) == expected


@pytest.mark.parametrize("text", ["headaches getting worse", "headaches are getting worse", "headaches worsening"])
def test_plural_worsening_headache_variants_stop_today(context, text):
    medical_report(context, text)
    assert view(context).today.decision_tier == "stop"
