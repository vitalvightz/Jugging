"""Seated lateral-sprain starter through the normal owned athlete workflow."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest

from api.contracts.ankle_restore_load_option import ANKLE_LOAD_OPTION as OPTION
from api.contracts.injury_policy import resolve_injury_policy
from api.services.clinical_review_freeze import frozen_review_hold
from api.services.injury_episode_service import InjuryEpisodeObservation, apply_episode_observations, record_episode_observation
from api.services.rehab_completion_service import record_rehab_exposures
from fightcamp.rehab_clinical import load_clinical_policies
from fightcamp.rehab_protocols import get_rehab_bank
from tests.support import FakeStore
from tests.test_achilles_load_activation import DAY, NOW, execute, report_permission, view
from tests.test_rehab_transition_engine import event


def bundle(*, scope="uncomplicated_lateral", permission="loading", severity="mild"):
    store, athlete, plan = FakeStore(), str(uuid4()), str(uuid4())
    store.intakes[athlete] = [dict(id=str(uuid4()), athlete_id=athlete, equipment_access=["chair", "stable_support"])]
    injury = store.create_injury_flag(athlete, dict(body_area="Left ankle", description=f"sprain [ankle_scope:{scope}]",
        severity=severity, status="monitoring", latest_reported_status="improving"))
    injury.update(side="left", canonical_location="ankle", body_region="ankle", injury_type="sprain",
                  created_at="2026-09-01T00:00:00Z", updated_at="2026-09-01T00:00:00Z")
    store.injury_flags[athlete][0].update(injury)
    opening = dict(id=str(uuid4()), athlete_id=athlete, injury_id=injury["id"], injury_episode_id=injury["episode_id"],
        event_type="injury_checkin", created_at="2026-09-01T00:00:00Z",
        payload=dict(explicit_report=True, latest_reported_status="improving"))
    store.injury_episode_events = {opening["id"]: opening}
    policy = next(p for p in load_clinical_policies() if p.policy_id == "ankle_sprain")
    for number, prescription in enumerate(p for p in policy.prescriptions if p.stage == "restore"):
        drill = next(d for g in get_rehab_bank() for d in g["drills"] if d["id"] == prescription.drill_id)
        exposure = event(number + 1, drill=drill, policy_id=policy.policy_id, athlete=athlete,
                         episode=injury["episode_id"], bank_hash=prescription.bank_hash)
        exposure.update(injury_id=injury["id"], injury_episode_id=injury["episode_id"])
        exposure["event_json"].update(injury_id=injury["id"])
        store.rehab_exposures[exposure["id"]] = exposure
    store.plans[plan] = dict(id=plan, athlete_id=athlete, intake_id=store.intakes[athlete][0]["id"], status="ready",
        created_at="2026-09-01T00:00:00Z", structured_plan={"weeks": [{"phase_label": "GPP", "days": [{"date": DAY,
        "day_type": "strength", "sessions": [dict(session_id="training", session_type="strength", title="Strength",
        blocks=[dict(block_id="training", block_type="strength", display_name="Leg work",
                     mechanical_load_regions=["ankle"], contact_level="none")])]}]}]})
    store.set_active_plan_id(athlete, plan)
    store.upsert_today_checkin(athlete, dict(plan_id=plan, training_day=DAY, recommendation_state="train_as_planned", pain="none", body="good"))
    result = store, None, None, athlete, plan, injury
    report_permission(result, permission)
    return result


@pytest.mark.parametrize("scope", ["unknown", "", "uncomplicated_lateral] [ankle_scope:unknown"])
def test_unknown_or_conflicting_scope_does_not_qualify(scope):
    decision = view(bundle(scope=scope)).open_injuries[0]["rehab_decision"]
    assert decision["stage"] == "restore"
    assert "ankle_uncomplicated_lateral_not_reported" in decision["progression"]["next_transition"]["reason_codes"]


def test_other_sprain_type_uses_existing_recovery_monitoring():
    decision = view(bundle(scope="other")).open_injuries[0]["rehab_decision"]
    assert decision["outcome"] == "unsupported_prescription"
    assert not decision["prescription"] and "progression" not in decision


def test_other_lower_limb_episode_cannot_borrow_bilateral_loading_permission():
    from api.services.rehab_stage_snapshot import resolve_open_injury_rehab_context
    b = bundle()
    b[0].create_injury_flag(b[3], dict(body_area="Right ankle", description="sprain", severity="moderate", status="open"))
    current = view(b)
    ankle = next(row for row in current.open_injuries if row["id"] == b[5]["id"])
    assert ankle["rehab_decision"]["stage"] != "load"
    context = next(row for row in resolve_open_injury_rehab_context(b[0], b[3]).values() if row["injury_id"] == b[5]["id"])
    assert context["policy_injury"]["restriction_hold"] is True


@pytest.mark.parametrize("area", ["Right ankle", "Right foot", "Right calf", "Right heel", "Right Achilles", "Right toe"])
def test_new_lower_limb_injury_blocks_saved_unstarted_load(area):
    from fastapi import HTTPException
    b = bundle()
    live = view(b).live_prescription
    assert live["session"]["blocks"][0]["rehab_drill_id"] == OPTION.drill_id
    saved = b[0].upsert_session_completion(b[3], dict(plan_id=b[4], session_id=live["session"]["session_id"],
        training_day=DAY, status="not_started", prescription_snapshot=deepcopy(live)))
    snapshot = deepcopy(saved["prescription_snapshot"])
    assert not frozen_review_hold(b[0], b[3], snapshot, work_state="unstarted", as_of=NOW)
    b[0].create_injury_flag(b[3], dict(body_area=area, description="strain", severity="mild", status="open"))
    assert frozen_review_hold(b[0], b[3], snapshot, work_state="unstarted", as_of=NOW)
    if area == "Right calf":
        with pytest.raises(HTTPException):
            execute(b, live)
        assert b[0].session_completions[b[3]][0]["status"] == "not_started"
    else:
        assert execute(b, live)["status"] == "started"
    assert b[0].session_completions[b[3]][0]["prescription_snapshot"] == snapshot
    assert not frozen_review_hold(b[0], b[3], snapshot, work_state="started", as_of=NOW)
    assert not frozen_review_hold(b[0], b[3], snapshot, work_state="completed", as_of=NOW)
    assert snapshot == saved["prescription_snapshot"]


@pytest.mark.parametrize("status,other_athlete", [("resolved", False), ("open", True)])
def test_resolved_or_other_athlete_leg_injury_does_not_hold_saved_load(status, other_athlete):
    b = bundle()
    snapshot = deepcopy(view(b).live_prescription)
    b[0].create_injury_flag(str(uuid4()) if other_athlete else b[3],
        dict(body_area="Right ankle", description="sprain", severity="mild", status=status))
    assert not frozen_review_hold(b[0], b[3], snapshot, work_state="unstarted", as_of=NOW)


@pytest.mark.parametrize("status,work_state", [("started", "started"), ("done", "completed")])
def test_second_leg_injury_preserves_started_and_completed_records(status, work_state):
    b = bundle()
    live = view(b).live_prescription
    record = execute(b, live)
    if status == "done":
        record = execute(b, live, "done", rehab_performance="done_as_shown")
    historical = deepcopy(b[0].session_completions[b[3]][0])
    b[0].create_injury_flag(b[3], dict(body_area="Right foot", description="strain", severity="mild", status="monitoring"))
    assert not frozen_review_hold(b[0], b[3], record["prescription_snapshot"], work_state=work_state, as_of=NOW)
    view(b)
    assert b[0].session_completions[b[3]][0] == historical


@pytest.mark.parametrize("detail", ["medial", "deltoid", "high ankle", "syndesmotic", "suspected fracture",
    "structural injury", "unstable", "persistent instability", "recurrent sprain", "giving way", "unable to bear weight",
    "bony tenderness", "deformity", "numbness", "cold foot"])
def test_unsupported_concerns_veto_reassuring_permission_and_completion(detail):
    b = bundle()
    b[0].injury_flags[b[3]][0]["description"] += f" {detail}"
    assert view(b).open_injuries[0]["rehab_decision"]["stage"] != "load"


@pytest.mark.parametrize("permission", [None, "not_cleared", "gentle_recovery"])
def test_contact_permission_is_not_loading_permission(permission):
    b = bundle(permission=permission)
    report_permission(b, permission, scopes=["rehab", "training", "contact"])
    assert view(b).open_injuries[0]["rehab_decision"]["stage"] == "restore"


@pytest.mark.parametrize("severity", ["moderate", "severe", "unknown"])
def test_severity_is_not_inferred_from_subtype(severity):
    assert view(bundle(severity=severity)).open_injuries[0]["rehab_decision"]["stage"] != "load"


@pytest.mark.parametrize("field", ["athlete_id", "injury_id", "injury_episode_id", "side"])
def test_wrong_exposure_ownership_cannot_supply_progression(field):
    b = bundle()
    for row in b[0].rehab_exposures.values():
        row[field] = "right" if field == "side" else str(uuid4())
        row["event_json"][field] = row[field]
    decision = view(b).open_injuries[0]["rehab_decision"]
    assert decision["stage"] == "restore"
    assert "entry_basis" not in decision["progression"]
    assert decision["progression"]["evidence_ids"] == []  # foreign work never certifies tolerance


@pytest.mark.parametrize("field,value", [("during_response", "not_reported"), ("next_day_response", "not_reported"),
    ("during_response", "worse"), ("next_day_response", "worse")])
def test_missing_or_worsening_response_cannot_pass(field, value):
    b = bundle()
    for row in b[0].rehab_exposures.values():
        row["event_json"]["response"][field] = value
    assert view(b).open_injuries[0]["rehab_decision"]["stage"] != "load"


def test_reported_permission_does_not_advance_stage_without_prior_work():
    b = bundle()
    b[0].rehab_exposures.clear()
    assert view(b).open_injuries[0]["rehab_decision"]["stage"] == "restore"


@pytest.mark.parametrize("mutation", ["permission", "episode", "worse", "medical", "restriction", "equipment", "setback", "response", "history"])
def test_frozen_future_rechecks_preserve_started_and_completed_history(mutation):
    b = bundle()
    live = deepcopy(view(b).live_prescription)
    assert live
    row = b[0].injury_flags[b[3]][0]
    if mutation == "permission":
        report_permission(b, "not_cleared")["created_at"] = NOW.isoformat()
    elif mutation == "episode":
        row["episode_id"] = str(uuid4())
    elif mutation == "worse":
        row["latest_reported_status"] = "worse"
    elif mutation in {"medical", "restriction"}:
        row[f"{mutation}_hold"] = True
    elif mutation == "equipment":
        b[0].intakes[b[3]][0]["equipment_access"] = []
    elif mutation in {"setback", "response"}:
        for exposure in b[0].rehab_exposures.values():
            exposure["event_json"]["response"]["next_day_response"] = "worse" if mutation == "setback" else "not_reported"
    elif mutation == "history":
        from api.contracts.rehab_assessment import AssessmentHistory
        b[0].list_injury_episode_events = lambda *args, **kwargs: AssessmentHistory(list(b[0].injury_episode_events.values()), history_complete=False)
    original = deepcopy(live)
    assert frozen_review_hold(b[0], b[3], live, work_state="unstarted", as_of=NOW)
    assert live == original
    assert not frozen_review_hold(b[0], b[3], live, work_state="completed", as_of=NOW)
    if mutation != "episode":
        assert not frozen_review_hold(b[0], b[3], live, work_state="started", as_of=NOW)


def test_other_profiles_and_ankle_baselines_unchanged():
    import json
    from fightcamp.rehab_clinical import content_hash
    current = json.loads(open("data/rehab_pathways.json", encoding="utf-8").read())
    from tools.rehab_metadata_review_lib import before_reported_permission_entry
    current = before_reported_permission_entry(current)
    manifest = json.loads(open("tools/rehab_ankle_activation_inventory_baseline.json", encoding="utf-8").read())
    shoulder = json.loads(open("tools/rehab_shoulder_activation_inventory_baseline.json", encoding="utf-8").read())
    for index, profile in enumerate(current["profiles"]):
        if profile["policy_id"] == "shoulder_tendonitis":
            assert content_hash(profile) == shoulder["activated_profile_sha256"]
            current["profiles"][index] = shoulder["historical_profile"]
    assert {p["policy_id"]: content_hash(p) for p in current["profiles"] if p["policy_id"] != "ankle_sprain"} == manifest["unchanged_profiles_sha256"]
    before = manifest["historical_profile"]
    after = next(p for p in current["profiles"] if p["policy_id"] == "ankle_sprain")
    assert after["prescriptions"][:3] == before["prescriptions"]
    assert after["stage_bundles"] == before["stage_bundles"]


def test_real_capture_today_start_complete_and_daily_allocation():
    b = bundle()
    current = view(b)
    assert current.open_injuries[0]["rehab_decision"]["stage"] == "load"
    live = current.live_prescription
    assert live and not live["safety_hold"]
    block = live["session"]["blocks"][0]
    assert block["rehab_drill_id"] == OPTION.drill_id
    assert block["exercise_key"] == OPTION.drill_id.replace("_", "-")
    assert block["dose"] == dict(sets=1, reps=10)
    assert block["instructions"] == OPTION.instructions
    assert block["range_choice"] == OPTION.range_choices[0]
    assert block["resistance"] == dict(mode="bodyweight", kg=None)
    assert block["frequency"] == "daily" and block["minimum_gap_days"] == 1
    assert "clinical_review_pin" not in block
    assert not any(e["event_type"] == "clinical_progression_review" for e in b[0].injury_episode_events.values())
    execute(b, live)
    done = execute(b, live, "done", rehab_performance="done_as_shown")
    events = record_rehab_exposures(b[0], athlete_id=b[3], plan_row=b[0].plans[b[4]], training_day=DAY,
        session_id=live["session"]["session_id"], completion=done,
        answers={b[5]["id"]: dict(injury_episode_id=b[5]["episode_id"], during_response="same", limit_response=None)})
    assert len(events) == 1 and events[0].dose_completed.reps == 10
    assert events[0].body_region == "ankle" and events[0].side == "left"
    next_live = view(b).live_prescription
    assert not next_live or all(v.get("rehab_drill_id") != OPTION.drill_id for v in next_live["session"]["blocks"])




@pytest.mark.parametrize("performance,eligible", [("done_as_shown", True), ("changed", False), ("skipped", False)])
def test_real_restore_completion_and_existing_checkins_supply_consumer_progression(performance, eligible):
    from api.services.injury_episode_service import exposure_rows_with_observations
    from api.services.rehab_completion_service import build_rehab_response_contexts, record_reported_permission_rehab
    b = bundle()
    b[0].rehab_exposures.clear()
    report_permission(b, "gentle_recovery")
    current = view(b)
    assert current.open_injuries[0]["rehab_decision"]["stage"] == "restore"
    live = current.live_prescription
    execute(b, live)
    completion = execute(b, live, "done", rehab_performance=performance, rehab_tracking="injury_checkin")
    # Match the existing session-completion route's follow-up capture.
    _, contexts = build_rehab_response_contexts(b[0], athlete_id=b[3], plan_row=b[0].plans[b[4]],
        training_day=DAY, session_id=completion["session_id"], completion=completion)
    persisted = b[0].initialize_session_completion_rehab_contexts(b[3], completion_id=completion["id"],
        plan_id=b[4], session_id=completion["session_id"], training_day=DAY, contexts=contexts)
    completion.update(persisted)
    record_reported_permission_rehab(b[0], athlete_id=b[3], plan_row=b[0].plans[b[4]], completion=completion)
    if performance == "skipped":
        assert not b[0].rehab_exposures
        assert view(b).open_injuries[0]["rehab_decision"]["stage"] != "load"
        return
    for row in b[0].rehab_exposures.values():
        row["created_at"] = NOW.isoformat()
        assert row["event_json"]["response"]["during_response"] == "not_reported"
    assert view(b).open_injuries[0]["rehab_decision"]["stage"] != "load"
    checkin = dict(id=str(uuid4()), athlete_id=b[3], injury_id=b[5]["id"], injury_episode_id=b[5]["episode_id"],
        event_type="injury_checkin", created_at=(NOW + timedelta(minutes=1)).isoformat(),
        payload=dict(explicit_report=True, latest_reported_status="ongoing"))
    b[0].injury_episode_events[checkin["id"]] = checkin
    # The existing single injury tap addresses every pending drill exposure.
    for row in b[0].rehab_exposures.values():
        observation = InjuryEpisodeObservation(injury_id=b[5]["id"], injury_episode_id=b[5]["episode_id"],
            event_type="delayed_rehab_response", exposure_id=row["id"], response="same")
        delayed = record_episode_observation(b[0], athlete_id=b[3], observation=observation,
            training_day=(NOW + timedelta(days=1)).date().isoformat())
        delayed["created_at"] = (NOW + timedelta(days=1)).isoformat()
        b[0].injury_episode_events[delayed["id"]] = delayed
    advice = report_permission(b, "loading")
    advice["created_at"] = (NOW + timedelta(days=1)).isoformat()
    b[0].injury_episode_events[advice["id"]] = advice
    events = list(b[0].injury_episode_events.values())
    injury = apply_episode_observations(b[0].injury_flags[b[3]][0], events, as_of=NOW + timedelta(days=1))
    decision = resolve_injury_policy(injury, policies=load_clinical_policies(), bank=get_rehab_bank(),
        equipment=["chair", "stable_support"], exposures=exposure_rows_with_observations(list(b[0].rehab_exposures.values()), events),
        as_of=NOW + timedelta(days=1))
    assert decision["stage"] == ("load" if eligible else "restore")
    assert "entry_basis" not in decision["progression"]
    assert not any(e["event_type"] == "rehab_progression_assessment" for e in events)




def test_generation_uses_same_owned_gate_and_option():
    from api.services.rehab_stage_snapshot import resolve_open_injury_rehab_context
    from fightcamp.input_parsing import _coerce_rehab_generation_context, _apply_rehab_generation_context
    from fightcamp.rehab_protocols import _episode_context, _reviewed_episode_option
    b = bundle()
    contexts = resolve_open_injury_rehab_context(b[0], b[3])
    assert len(contexts) == 1
    raw = next(iter(contexts.values()))
    raw["available_equipment"] = ["chair", "stable_support"]
    parsed = _coerce_rehab_generation_context(dict(rehab_generation_context=raw))
    entry = dict(injury_type="sprain", severity="mild", laterality="left")
    _apply_rehab_generation_context(entry, parsed)
    generated = _reviewed_episode_option(_episode_context(entry), "ankle", "GPP")
    assert generated["decision"]["stage"] == "load" and generated["drill"]["id"] == OPTION.drill_id
    assert generated["decision"]["prescription"]["dose"] == dict(sets=1, reps=10)
    row = apply_episode_observations(b[0].injury_flags[b[3]][0], list(b[0].injury_episode_events.values()), as_of=NOW)
    assert resolve_injury_policy(row, policies=load_clinical_policies(), bank=get_rehab_bank(),
        equipment=["chair", "stable_support"], exposures=list(b[0].rehab_exposures.values()), as_of=NOW)["stage"] == "load"




def test_multi_injury_and_started_history_remain_authoritative():
    b = bundle()
    live = view(b).live_prescription
    saved = deepcopy(execute(b, live)["prescription_snapshot"])
    report_permission(b, "not_cleared")
    assert not frozen_review_hold(b[0], b[3], saved, work_state="started", as_of=NOW)
    b[0].create_injury_flag(b[3], dict(body_area="Wrist", description="Wrist strain", severity="severe", status="open"))
    assert view(b).live_prescription["safety_hold"]
    assert b[0].session_completions[b[3]][0]["prescription_snapshot"] == saved




@pytest.mark.parametrize("field", ["id", "episode_id", "athlete_id", "side", "injury_type"])
def test_generation_cannot_mix_owned_observations_with_another_episode(field):
    from api.services.rehab_stage_snapshot import resolve_open_injury_rehab_context
    from fightcamp.input_parsing import _coerce_rehab_generation_context, _apply_rehab_generation_context
    from fightcamp.rehab_protocols import _episode_context, _reviewed_episode_option
    b = bundle()
    raw = next(iter(resolve_open_injury_rehab_context(b[0], b[3]).values()))
    raw["policy_injury"][field] = "another"
    entry = dict(injury_type="sprain", severity="mild", laterality="left")
    _apply_rehab_generation_context(entry, _coerce_rehab_generation_context(dict(rehab_generation_context=raw)))
    generated = _reviewed_episode_option(_episode_context(entry), "ankle", "GPP")
    assert generated["decision"]["stage"] != "load"



@pytest.mark.parametrize("scope,severity,permission,eligible", [("uncomplicated_lateral", "mild", "loading", True),
    ("unknown", "mild", "loading", False), ("other", "mild", "loading", False),
    ("uncomplicated_lateral", "severe", "loading", False), ("uncomplicated_lateral", "mild", "not_cleared", False)])
def test_full_guided_intake_generation(scope, severity, permission, eligible):
    from api.services.rehab_stage_snapshot import annotate_payload_with_rehab_stage
    from fightcamp.input_parsing import _extract_guided_injuries, _parse_guided_injuries
    from fightcamp.rehab_protocols import generate_rehab_protocols
    b = bundle(scope=scope, permission=permission)
    payload = annotate_payload_with_rehab_stage(dict(guided_injuries=[dict(
        area="Left ankle", severity=severity, trend="improving", injury_type="tendon_ligament",
        notes="Ankle sprain", timeframe="two_to_six_weeks")]), store=b[0], athlete_id=b[3])
    entries, _ = _parse_guided_injuries(_extract_guided_injuries(payload))
    block, _ = generate_rehab_protocols(injury_string="left ankle sprain", exercise_data=[], current_phase="GPP", parsed_entries=entries)
    assert ("Seated controlled ankle heel raises" in block) is eligible
    assert "Foam Pad" not in block and "Banded Ankle Circles" not in block
    if eligible:
        assert "1 x 10 reps" in block and OPTION.instructions in block


def test_no_dynamic_return_and_missing_reviewed_content_or_chair_cannot_schedule():
    b = bundle(permission="sport_specific")
    row = apply_episode_observations(b[0].injury_flags[b[3]][0], list(b[0].injury_episode_events.values()), as_of=NOW)
    policy = next(p for p in load_clinical_policies() if p.policy_id == "ankle_sprain")
    assert policy.live_stages == ["calm", "restore", "load"]
    assert not any(t.promotable for t in policy.transitions[1:])
    for equipment, bank in [([], get_rehab_bank()), (["chair"], [{**g, "drills": [d for d in g["drills"] if d["id"] != OPTION.drill_id]} for g in get_rehab_bank()])]:
        decision = resolve_injury_policy(row, policies=[policy], bank=bank, equipment=equipment,
            exposures=list(b[0].rehab_exposures.values()), as_of=NOW)
        assert not decision["prescription"] or decision["prescription"]["drill_id"] != OPTION.drill_id
