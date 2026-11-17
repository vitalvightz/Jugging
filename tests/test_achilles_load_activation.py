"""Simple reported permissions through real Today, completion and exposure services."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.contracts.achilles_restore_load_pilot import ACHILLES_LOAD_OPTION as OPTION
from api.services import today_service
from api.services.rehab_completion_service import record_rehab_exposures
from tests.support import FakeStore
from tests.test_achilles_progression_inputs import NOW
from tests.test_achilles_shadow_pilot import shadow_bundle

DAY = NOW.date().isoformat()


def activation_bundle(*, site="midportion", level="loading", scopes=None):
    template, _, request, _ = shadow_bundle()
    store = FakeStore()
    athlete, plan = str(request.athlete_id), str(uuid4())
    store.intakes[athlete] = [dict(id=str(uuid4()), athlete_id=athlete, equipment_access=["stable_support"])]
    injury = store.create_injury_flag(athlete, template.snapshot["injury"])
    injury.update(created_at="2026-09-01T00:00:00Z", updated_at="2026-09-01T00:00:00Z",
                  description=f"Left Achilles tendonitis [achilles_site:{site}]")
    store.injury_flags[athlete][0].update(injury)
    opening = deepcopy(template.snapshot["events"][0])
    store.injury_episode_events[opening["id"]] = opening
    for exposure in template.snapshot["exposures"]:
        store.rehab_exposures[exposure["id"]] = deepcopy(exposure)
    store.plans[plan] = dict(id=plan, athlete_id=athlete, intake_id=store.intakes[athlete][0]["id"], status="ready", created_at="2026-09-01T00:00:00Z",
        structured_plan={"weeks":[{"phase_label":"GPP","days":[{"date":DAY,"day_type":"strength","sessions":[
            dict(session_id="original-session",session_type="strength",title="Strength",blocks=[
                dict(block_id="training",block_type="strength",display_name="Calf work",mechanical_load_regions=["achilles"],contact_level="none")])]}]}]})
    store.set_active_plan_id(athlete, plan)
    store.upsert_today_checkin(athlete, dict(plan_id=plan,training_day=DAY,recommendation_state="train_as_planned",pain="none",body="good"))
    bundle = store, None, None, athlete, plan, injury
    report_permission(bundle, level, scopes=scopes)
    return bundle


def report_permission(bundle, level, *, scopes=None):
    from api.services.injury_episode_service import InjuryEpisodeObservation, record_episode_observation
    values = dict(injury_id=bundle[5]["id"], injury_episode_id=bundle[5]["episode_id"],
                  event_type="clinician_clearance_report", scopes=scopes or ["rehab"])
    if level is not None:
        values["rehabilitation_permission"] = dict(schema_version=1, level=level)
    event = record_episode_observation(bundle[0], athlete_id=bundle[3],
        observation=InjuryEpisodeObservation(**values), training_day=DAY)
    # Preserve call order in replay: UUID ordering must not choose between two
    # advice updates artificially recorded at exactly the same instant.
    event["created_at"] = (NOW-timedelta(minutes=1)+timedelta(microseconds=len(bundle[0].injury_episode_events))).isoformat()
    bundle[0].injury_episode_events[event["id"]] = event
    return event


def view(bundle):
    return today_service.build_today_command_view(bundle[0],athlete_id=bundle[3],athlete_timezone="UTC",now=NOW)


def execute(bundle,live,status="started",**changes):
    return today_service.upsert_session_completion(bundle[0],athlete_id=bundle[3],athlete_timezone="UTC",now=NOW,
        payload=dict(plan_id=bundle[4],session_id=live["session"]["session_id"],status=status,prescription_revision=live["revision"],**changes))


def test_reported_permission_to_today_accept_complete_without_assessment_or_admin():
    bundle = activation_bundle()
    current = view(bundle)
    assert not any(e["event_type"] in {"rehab_progression_assessment", "clinical_progression_review"} for e in bundle[0].injury_episode_events.values())
    assert current.open_injuries[0]["rehab_decision"]["stage"] == "load"
    live = current.live_prescription
    assert live and not live["safety_hold"]
    block = live["session"]["blocks"][0]
    assert block["rehab_drill_id"] == OPTION.drill_id
    assert block["dose"] == dict(sets=1,reps=10)
    assert block["instructions"] == OPTION.instructions and block["range_choice"] == "floor_level"
    assert block["resistance"] == dict(mode="bodyweight",kg=None)
    assert block["frequency"] == "daily" and block["minimum_gap_days"] == 1
    assert "clinical_review_pin" not in block and "reviewed_prescription" not in block
    execute(bundle, live)
    done = execute(bundle, live, "done", rehab_performance="done_as_shown")
    events = record_rehab_exposures(bundle[0], athlete_id=bundle[3], plan_row=bundle[0].plans[bundle[4]],
        training_day=DAY, session_id=live["session"]["session_id"], completion=done,
        answers={bundle[5]["id"]:dict(injury_episode_id=bundle[5]["episode_id"],during_response="same",limit_response=None)})
    assert len(events) == 1
    assert events[0].dose_completed.sets == 1 and events[0].dose_completed.reps == 10
    again = view(bundle).live_prescription
    assert not again or all(b.get("rehab_drill_id") != OPTION.drill_id for b in again["session"]["blocks"])


@pytest.mark.parametrize("site", ["insertional", "unknown"])
@pytest.mark.parametrize("level", [None, "not_cleared", "gentle_recovery", "loading", "sport_specific"])
def test_unsupported_presentations_never_unlock(site, level):
    bundle = activation_bundle(site=site, level=level)
    assert view(bundle).open_injuries[0]["rehab_decision"]["stage"] != "load"


@pytest.mark.parametrize("level", [None, "not_cleared", "gentle_recovery"])
def test_training_scope_does_not_supply_rehab_permission(level):
    bundle = activation_bundle(level=level, scopes=["rehab","training","contact"])
    assert view(bundle).open_injuries[0]["rehab_decision"]["stage"] == "restore"


def test_sport_permission_opens_only_existing_load_option():
    decision = view(activation_bundle(level="sport_specific")).open_injuries[0]["rehab_decision"]
    assert decision["stage"] == "load"
    assert decision["progression"]["next_transition"]["to_stage"] == "dynamic"
    assert not decision["progression"]["next_transition"]["target_stage_live"]


@pytest.mark.parametrize("mutation", ["downgrade", "withdraw", "episode", "worse", "medical", "restriction", "support"])
def test_future_work_holds_and_saved_history_is_not_rewritten(mutation):
    bundle = activation_bundle()
    live = view(bundle).live_prescription
    saved = bundle[0].upsert_session_completion(bundle[3], dict(plan_id=bundle[4], session_id=live["session"]["session_id"],
        training_day=DAY,status="not_started",prescription_snapshot=deepcopy(live)))
    snapshot = deepcopy(saved["prescription_snapshot"])
    if mutation in {"downgrade", "withdraw"}:
        for event in bundle[0].injury_episode_events.values():
            if event["event_type"] == "clinician_clearance_report":
                event["payload"]["rehabilitation_permission"]["level"] = "gentle_recovery" if mutation == "downgrade" else "not_cleared"
    elif mutation == "episode":
        bundle[0].injury_flags[bundle[3]][0]["episode_id"] = str(uuid4())
    elif mutation == "support":
        bundle[0].intakes[bundle[3]][0]["equipment_access"] = []
    else:
        bundle[0].injury_flags[bundle[3]][0][{"worse":"latest_reported_status","medical":"medical_hold","restriction":"restriction_hold"}[mutation]] = "worse" if mutation == "worse" else True
    held = view(bundle).live_prescription
    if held and held.get("frozen"):
        assert held["safety_hold"]
    if mutation in {"withdraw", "episode"}:
        # Withdrawn authority / episode identity still requires a fresh session.
        with pytest.raises(HTTPException):
            execute(bundle, live)
    else:
        assert execute(bundle, live)["status"] == "started"
    assert bundle[0].session_completions[bundle[3]][0]["prescription_snapshot"] == snapshot


@pytest.mark.parametrize("status,performance", [("done","done_as_shown"),("modified","done_as_shown"),("modified","changed"),("skipped",None),("done","skipped")])
def test_simple_completion_records_actual_work_only(status, performance):
    from api.services.rehab_completion_service import build_rehab_response_contexts, record_reported_permission_rehab
    bundle = activation_bundle()
    live = view(bundle).live_prescription
    done = execute(bundle, live, status, rehab_performance=performance, modification_reason="Rehab changed" if status != "done" else "")
    _, contexts = build_rehab_response_contexts(bundle[0],athlete_id=bundle[3],plan_row=bundle[0].plans[bundle[4]],training_day=DAY,session_id=live["session"]["session_id"],completion=done)
    done["rehab_response_contexts"] = contexts
    events = record_reported_permission_rehab(bundle[0], athlete_id=bundle[3],plan_row=bundle[0].plans[bundle[4]],completion=done)
    if status == "skipped" or performance == "skipped":
        assert not events
    else:
        assert len(events) == 1 and events[0].response.during_response == "not_reported"
        if performance == "done_as_shown":
            # Rehab confirmed as shown counts even when another block was skipped.
            assert events[0].dose_completed.reps == 10
        else:
            assert events[0].dose_completed.reps is None


@pytest.mark.parametrize("status", ["started", "skipped"])
def test_done_as_shown_still_needs_finished_work(status):
    bundle = activation_bundle()
    live = view(bundle).live_prescription
    with pytest.raises(HTTPException) as exc:
        execute(bundle, live, status, rehab_performance="done_as_shown", modification_reason="Ran out of time")
    assert exc.value.status_code == 422


def test_started_and_completed_snapshots_survive_permission_change():
    from api.services.clinical_review_freeze import frozen_review_hold
    bundle = activation_bundle()
    live = view(bundle).live_prescription
    saved = deepcopy(execute(bundle, live)["prescription_snapshot"])
    for event in bundle[0].injury_episode_events.values():
        if event["event_type"] == "clinician_clearance_report":
            event["payload"]["rehabilitation_permission"]["level"] = "not_cleared"
    assert not frozen_review_hold(bundle[0],bundle[3],saved,work_state="started",as_of=NOW)
    assert not frozen_review_hold(bundle[0],bundle[3],saved,work_state="completed",as_of=NOW)
    view(bundle)
    assert bundle[0].session_completions[bundle[3]][0]["prescription_snapshot"] == saved


def test_no_permission_can_override_another_severe_injury():
    bundle = activation_bundle()
    live = view(bundle).live_prescription
    saved = deepcopy(execute(bundle,live)["prescription_snapshot"])
    bundle[0].create_injury_flag(bundle[3],dict(body_area="Wrist",description="Wrist strain",severity="severe",status="open"))
    held = view(bundle).live_prescription
    assert held and held["safety_hold"]
    assert bundle[0].session_completions[bundle[3]][0]["prescription_snapshot"] == saved


@pytest.mark.parametrize("changes", [{"mandatory_restrictions": []}, {"duration_seconds": 600}, {"weight": 10}, {"coaching_cues": ["Use a step"]}])
def test_saved_load_cannot_change_reviewed_work(changes):
    from api.services.clinical_review_freeze import frozen_review_hold
    bundle = activation_bundle()
    saved = deepcopy(view(bundle).live_prescription)
    saved["session"]["blocks"][0].update(changes)
    assert frozen_review_hold(bundle[0], bundle[3], saved, work_state="unstarted", as_of=NOW)


def test_unavailable_permission_inputs_hold_future_work(monkeypatch):
    from api.services.clinical_review_freeze import frozen_review_hold
    bundle = activation_bundle()
    saved = deepcopy(view(bundle).live_prescription)
    def unavailable(*args, **kwargs):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(bundle[0], "get_injury_flag_for_athlete", unavailable)
    assert frozen_review_hold(bundle[0], bundle[3], saved, work_state="unstarted", as_of=NOW)
