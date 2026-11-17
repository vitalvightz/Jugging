from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.contracts.achilles_progression import AchillesProgressionInput
from api.contracts.rehab_assessment import AchillesProgressionAssessment, AssessmentContext, input_definitions, read_assessment_input

from api.contracts.injury_policy import resolve_injury_policy
from api.contracts.rehab_progression import evaluate_transition
from api.services.injury_episode_service import InjuryEpisodeObservation, apply_episode_observations, record_episode_observation
from fightcamp.injury_triage import current_report_medical_hold_reasons
from fightcamp.rehab_clinical import load_clinical_policies
from fightcamp.rehab_pathways import PathwayTransition
from fightcamp.rehab_protocols import get_rehab_bank
from tests.support import FakeStore
from tests.support import _build_client, withdraw_health_consent
from api.services import today_service

NOW = datetime(2026, 10, 5, 20, tzinfo=timezone.utc)
CHECKPOINTS = frozenset(key for key, (_, protocol) in input_definitions().items() if protocol.profile_id == "achilles_tendonitis")


def assessment(**changes):
    envelope = {k: changes.pop(k, default) for k, default in dict(side="left", assessed_at="2026-10-05T15:00:00Z", assessor="clinician_physio").items()}
    return AchillesProgressionAssessment(**envelope, payload=AchillesProgressionInput(**{**dict(site="insertional", incompatible_pathology="excluded",
        heel_rise_completed=True, heel_rise_mode="single_leg", heel_rise_quality="controlled", heel_rise_repetitions=1,
        heel_rise_assessor_usable=True, loading_task="heel_rise_assessment", loading_performed_at="2026-10-04T15:00:00Z",
        during_symptoms=3.0, delayed_symptoms=4.0, delayed_response_at="2026-10-05T15:00:00Z",
        range_assessed=True, permitted_range="floor_level", resistance="bodyweight",
        range_load_tolerance="tolerated", range_load_assessor_usable=True), **changes}))


@pytest.fixture
def context():
    store, athlete = FakeStore(), str(uuid4())
    injury = store.create_injury_flag(athlete, dict(body_area="Left Achilles", description="Achilles tendonitis",
        severity="mild", status="monitoring"))
    injury.update(side="left", body_region="achilles", created_at="2026-10-01T00:00:00Z")
    store.injury_flags[athlete][0].update(injury)
    return store, athlete, injury


def capture(context, value=None, report_id=None):
    store, athlete, injury = context
    event = record_episode_observation(store, athlete_id=athlete, training_day="2026-10-05",
        observation=InjuryEpisodeObservation(injury_id=injury["id"], injury_episode_id=injury["episode_id"],
            event_type="rehab_progression_assessment", assessment=value or assessment(), report_id=report_id or uuid4()))
    # The fixture replays 5 October; recording must use that clock too.
    event["created_at"] = "2026-10-05T16:00:00Z"
    store.injury_episode_events[event["id"]]["created_at"] = event["created_at"]
    return event


def enriched(context, value=None):
    event = capture(context, value)
    event["created_at"] = "2026-10-05T16:00:00Z"
    return apply_episode_observations(context[2], [event])


def read(row, key="achilles_heel_rise_assessed", **extra):
    return read_assessment_input(key, AssessmentContext.from_injury(row, as_of=NOW, **extra))


def test_real_capture_idempotent_owned_and_readable(context):
    report = uuid4()
    first = capture(context, report_id=report)
    assert capture(context, report_id=report) == first
    assert first["athlete_id"] == context[1]
    assert first["payload"]["externally_verified"] is False
    assert context[0].rehab_exposures == {}
    with pytest.raises(HTTPException):
        capture(context, assessment(site="midportion"), report_id=report)
    row = enriched(context)
    for key in CHECKPOINTS:
        result = read(row, key)
        assert result["status"] == "pass"
        assert result["usable_for_clinical_promotion"] is False
        assert result["assessor"] == "clinician_physio"


@pytest.mark.parametrize("site,status", [("midportion", "pass"), ("insertional", "pass"), ("unknown", "unknown")])
def test_site_is_explicit_and_never_inferred(context, site, status):
    row = enriched(context, assessment(site=site))
    assert read(row, "achilles_site_assessed")["status"] == status
    assert row["progression_assessments"][0]["payload"]["assessment"]["payload"]["site"] == site


@pytest.mark.parametrize("field,value", [("athlete_id", "other"), ("injury_id", "other"),
    ("injury_episode_id", "other")])
def test_other_ownership_never_satisfies(context, field, value):
    row = enriched(context)
    row["progression_assessments"][0][field] = value
    assert read(row)["status"] == "unknown"


@pytest.mark.parametrize("change", [dict(side="right"), dict(side="bilateral"), dict(body_region="ankle", body_area="Ankle", description="Ankle tendonitis"),
    dict(injury_type="sprain"), dict(body_region="elbow", body_area="Elbow", description="Elbow tendonitis")])
def test_exact_side_and_type_no_bilateral_widening(context, change):
    row = enriched(context)
    row.update(change)
    assert read(row)["status"] == "unknown"


@pytest.mark.parametrize("assessor,heel,range_status", [("unknown", "unknown", "unknown"),
    ("self_reported", "unknown", "unknown"), ("coach_observed", "pass", "unknown"), ("clinician_physio", "pass", "pass")])
def test_sources_not_equivalent(context, assessor, heel, range_status):
    row = enriched(context, assessment(assessor=assessor))
    assert read(row)["status"] == heel
    assert read(row, "achilles_range_load_assessed")["status"] == range_status
    assert not read(row)["externally_verified"]


@pytest.mark.parametrize("changes,key,status", [
    (dict(heel_rise_completed=None, heel_rise_mode="unknown", heel_rise_quality="unknown", heel_rise_repetitions=None, heel_rise_assessor_usable=None), "achilles_heel_rise_assessed", "unknown"),
    (dict(heel_rise_quality="unable", heel_rise_assessor_usable=False), "achilles_heel_rise_assessed", "fail"),
    (dict(delayed_symptoms=None, delayed_response_at=None), "achilles_loading_response_assessed", "unknown"),
    (dict(range_load_tolerance="not_tolerated"), "achilles_range_load_assessed", "fail"),
    (dict(incompatible_pathology="not_assessed"), "achilles_range_load_assessed", "unknown"),
    (dict(side="unknown"), "achilles_heel_rise_assessed", "unknown"),
])
def test_unknown_and_failed_observations(context, changes, key, status):
    if changes.get("side") == "unknown":
        context[2]["side"] = "unknown"
        context[0].injury_flags[context[1]][0]["side"] = "unknown"
    assert read(enriched(context, assessment(**changes)), key)["status"] == status


def test_stale_future_and_newer_unknown_do_not_reuse_old_success(context):
    row = enriched(context)
    assert read(row, setback_at=datetime(2026, 10, 5, 15, tzinfo=timezone.utc))["status"] == "unknown"
    assert read(row, setback_at=datetime(2026, 10, 4, 16, tzinfo=timezone.utc))["status"] == "unknown"
    row["assessment_episode_started_at"] = NOW
    assert read(row)["status"] == "unknown"
    row.pop("assessment_episode_started_at")
    row["progression_assessments"][0]["created_at"] = "2026-10-04T16:00:00Z"
    assert read(row)["status"] == "unknown"
    old = enriched(context)
    newer = deepcopy(old["progression_assessments"][0])
    newer["id"], newer["created_at"] = "new", "2026-10-05T17:00:00Z"
    newer["payload"]["assessment"]["payload"].update(heel_rise_quality="unknown", heel_rise_assessor_usable=None)
    old["progression_assessments"].append(newer)
    assert read(old)["status"] == "unknown"


@pytest.mark.parametrize("field", ["suspected_rupture", "marked_weakness", "traumatic_loss_of_function", "clinician_restriction", "incompatible_pathology"])
def test_safety_hold_wins_and_reassuring_report_cannot_clear(context, field):
    row = enriched(context, assessment(**{field: "suspected" if field == "incompatible_pathology" else True}))
    assert current_report_medical_hold_reasons(row)
    assert all(read(row, key)["status"] == "fail" for key in CHECKPOINTS)
    good = enriched(context)["progression_assessments"][0]
    good["created_at"] = "2026-10-05T18:00:00Z"
    row = apply_episode_observations(context[2], [*row["progression_assessments"], good])
    assert current_report_medical_hold_reasons(row)
    decision = resolve_injury_policy(row, policies=load_clinical_policies(), bank=get_rehab_bank())
    assert decision["outcome"] == "medical_review"
    assert decision["prescription"] is None


@pytest.mark.parametrize("irrelevant", [dict(clinician_clearance={"scopes": ["rehab", "training", "contact"]}),
    dict(camp_phase="SPP"), dict(days_since_injury=100), dict(completed_sessions=100),
    dict(latest_reported_status="improving", next_day_response="same"), dict(heel_rise_completed=True)])
def test_no_proxy_for_real_observation(context, irrelevant):
    row = {**context[2], **irrelevant}
    assert all(read(row, key)["status"] == "unknown" for key in CHECKPOINTS)


def test_transition_engine_reads_capture_but_production_load_stays_closed(context):
    row = enriched(context)
    policy = next(p for p in load_clinical_policies() if p.policy_id == "achilles_tendonitis")
    transition = PathwayTransition(from_stage="restore", to_stage="load", closed_reason="test input shell",
        requirements=[dict(requirement_id=k, kind="input_availability", checkpoint=k,
                           basis="data_sufficiency", description="Synthetic availability fixture") for k in sorted(CHECKPOINTS)])
    evaluated = evaluate_transition(transition, policy=policy, injury=row, exposures=[])
    assert evaluated["status"] == "closed"
    assert all(r["status"] == "pass" for r in evaluated["requirements"])
    before = resolve_injury_policy(context[2], policies=load_clinical_policies(), bank=get_rehab_bank())
    after = resolve_injury_policy(row, policies=load_clinical_policies(), bank=get_rehab_bank())
    assert after["stage"] in {"calm", "restore"}
    assert after["prescription"] == before["prescription"]
    assert after["progression"] == before["progression"]
    assert set(after["assessment_inputs"]) == CHECKPOINTS
    assert policy.transitions[0].promotable and "load" in policy.live_stages
    assert after["stage"] != "load"  # Availability cannot replace trusted approval.


def test_capture_rejects_cross_side_episode_owner_and_future(context):
    for value in (assessment(side="right"), assessment(assessed_at="2099-01-01T00:00:00Z")):
        with pytest.raises(HTTPException):
            capture(context, value)
    for athlete, episode in [(str(uuid4()), context[2]["episode_id"]), (context[1], str(uuid4()))]:
        with pytest.raises(HTTPException):
            record_episode_observation(context[0], athlete_id=athlete, training_day="2026-10-05",
                observation=InjuryEpisodeObservation(injury_id=context[2]["id"], injury_episode_id=episode,
                    event_type="rehab_progression_assessment", assessment=assessment()))


@pytest.mark.parametrize("changes", [dict(during_symptoms="same"), dict(during_symptoms=True),
    dict(assessed_at="2026-10-05"), dict(delayed_response_at="2026-10-03T12:00:00Z"),
    dict(heel_rise_completed=False), dict(range_assessed=False), dict(externally_verified=True)])
def test_malformed_or_fabricated_measurements_rejected(changes):
    with pytest.raises(ValidationError):
        assessment(**changes)


def test_authenticated_api_owns_capture_and_requires_health_consent(context):
    client, store, _ = _build_client()
    injury = {**context[2], "athlete_id": "athlete-1"}
    store.injury_flags["athlete-1"] = [injury]
    body = dict(injury_id=injury["id"], injury_episode_id=injury["episode_id"],
                event_type="rehab_progression_assessment", report_id=str(uuid4()), assessment=assessment().model_dump(mode="json"))
    url = "/api/today/injury-episode-observation"
    assert client.post(url, json=body).status_code == 401
    headers = {"Authorization": "Bearer athlete-token"}
    first = client.post(url, json=body, headers=headers)
    assert first.status_code == 200
    assert client.post(url, json=body, headers=headers).json() == first.json()
    assert client.post(url, json={**body, "assessment": {**body["assessment"], "side": "right"}}, headers=headers).status_code == 409
    assert client.post(url, json={**body, "athlete_id": str(uuid4())}, headers=headers).status_code == 422
    withdraw_health_consent(store)
    assert client.post(url, json={**body, "report_id": str(uuid4())}, headers=headers).status_code == 403
    assert len(store.injury_episode_events) == 1


@pytest.mark.parametrize("frozen", [False, True])
def test_today_concern_holds_training_and_preserves_other_episode(context, frozen):
    store, athlete, injury = context
    plan = str(uuid4())
    store.plans[plan] = dict(id=plan, athlete_id=athlete, status="ready", created_at="2026-09-01T00:00:00Z",
        structured_plan={"weeks": [{"phase_label": "GPP", "days": [{"date": "2026-10-05", "day_type": "strength", "sessions": [
            dict(session_id="strength-1", session_type="strength", title="Strength", blocks=[dict(block_id="block-1", block_type="strength", display_name="Strength", mechanical_load_regions=["shoulder"], contact_level="none")])]}]}]})
    store.set_active_plan_id(athlete, plan)
    store.upsert_today_checkin(athlete, dict(plan_id=plan, training_day="2026-10-05", recommendation_state="train_as_planned", pain="none", body="good"))
    other = store.create_injury_flag(athlete, dict(body_area="Right elbow", description="Elbow strain", severity="mild", status="open"))
    snapshot = None
    if frozen:
        snapshot = dict(plan_id=plan, training_day="2026-10-05", revision="a" * 64,
            session=deepcopy(store.plans[plan]["structured_plan"]["weeks"][0]["days"][0]["sessions"][0]))
        store.upsert_session_completion(athlete, dict(plan_id=plan, session_id="strength-1", training_day="2026-10-05",
            status="started", prescription_snapshot=snapshot))
    capture(context, assessment(suspected_rupture=True))
    clearance = InjuryEpisodeObservation(injury_id=injury["id"], injury_episode_id=injury["episode_id"],
        event_type="clinician_clearance_report", scopes=["rehab", "training", "contact"])
    record_episode_observation(store, athlete_id=athlete, observation=clearance, training_day="2026-10-05")
    current = today_service.build_today_command_view(store, athlete_id=athlete, athlete_timezone="UTC", now=NOW)
    by_id = {r["id"]: r for r in current.open_injuries}
    assert by_id[injury["id"]]["rehab_decision"]["outcome"] == "medical_review"
    assert "assessment_inputs" not in by_id[other["id"]]["rehab_decision"]
    assert current.today.recommendation_state == "pull_back"
    assert current.live_prescription is None or current.live_prescription["safety_hold"]
    if frozen:
        assert current.live_prescription["frozen"]
        assert current.live_prescription["session"] == snapshot["session"]
    row = today_service.upsert_session_completion(store, athlete_id=athlete, athlete_timezone="UTC", now=NOW,
        payload=dict(plan_id=plan, session_id="strength-1", status="done" if frozen else "started"))
    assert row["status"] == ("done" if frozen else "started")


def test_truncated_or_forged_provenance_is_unknown(context):
    row = enriched(context)
    assert read(row, history_truncated=True)["status"] == "unknown"
    row["progression_assessments"][0]["payload"]["externally_verified"] = True
    assert read(row)["status"] == "unknown"
