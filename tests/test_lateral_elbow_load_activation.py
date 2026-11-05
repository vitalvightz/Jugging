"""Second profile through shared capture, policy, Today, completion and freeze."""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.contracts.elbow_restore_load_option import ELBOW_LOAD_OPTION as OPTION
from api.contracts.injury_policy import resolve_injury_policy
from api.contracts.lateral_elbow_progression import INPUT, evaluate_elbow_entry, evaluate_elbow_permission
from api.contracts.rehab_assessment import AssessmentContext, LateralElbowProgressionAssessment, read_assessment_input
from api.services.clinical_review_freeze import frozen_review_hold
from api.services.injury_episode_service import InjuryEpisodeObservation, apply_episode_observations, record_episode_observation
from api.services.rehab_completion_service import record_rehab_exposures
from fightcamp.rehab_clinical import load_clinical_policies
from fightcamp.rehab_protocols import get_rehab_bank
from tests.support import FakeStore
from tests.test_achilles_load_activation import DAY, NOW, execute, report_permission, view
from tests.test_rehab_transition_engine import event


def assessment(**changes):
    envelope = {k: changes.pop(k, v) for k, v in dict(side="left", assessor="clinician_physio",
        assessed_at=(NOW - timedelta(hours=2)).isoformat()).items()}
    return LateralElbowProgressionAssessment(**envelope, payload=dict(subtype="lateral", course="chronic",
        safety_screen="clear", pain_irritability="acceptable", elbow_wrist_motion="acceptable",
        grip_task="daily_grip_task", grip_function="acceptable", wrist_extension_task="supported_hand_weight",
        wrist_extension_tolerance="acceptable", option_recommended=True) | changes)


def bundle(*, assessed=True, permission="loading", **changes):
    store, athlete, plan = FakeStore(), str(uuid4()), str(uuid4())
    store.intakes[athlete] = [dict(id=str(uuid4()), athlete_id=athlete, equipment_access=["table"])]
    injury = store.create_injury_flag(athlete, dict(body_area="Left elbow", description="Lateral elbow tendonitis",
        severity="mild", status="monitoring", latest_reported_status="improving"))
    injury.update(side="left", canonical_location="elbow", body_region="elbow", injury_type="tendonitis",
                  created_at="2026-09-01T00:00:00Z", updated_at="2026-09-01T00:00:00Z")
    store.injury_flags[athlete][0].update(injury)
    opening = dict(id=str(uuid4()), athlete_id=athlete, injury_id=injury["id"], injury_episode_id=injury["episode_id"],
        event_type="injury_checkin", created_at="2026-09-01T00:00:00Z",
        payload=dict(explicit_report=True, latest_reported_status="improving"))
    store.injury_episode_events = {opening["id"]: opening}
    policy = next(p for p in load_clinical_policies() if p.policy_id == "elbow_tendonitis")
    restore = next(d for g in get_rehab_bank() for d in g["drills"] if d["id"] == policy.prescriptions[1].drill_id)
    exposure = event(drill=restore, policy_id=policy.policy_id, athlete=athlete, episode=injury["episode_id"],
                     bank_hash=policy.prescriptions[1].bank_hash)
    exposure.update(injury_id=injury["id"], injury_episode_id=injury["episode_id"])
    exposure["event_json"].update(injury_id=injury["id"], body_region="elbow")
    exposure["event_json"]["demand"]["target_regions"] = ["elbow"]
    store.rehab_exposures[exposure["id"]] = exposure
    store.plans[plan] = dict(id=plan, athlete_id=athlete, intake_id=store.intakes[athlete][0]["id"], status="ready",
        created_at="2026-09-01T00:00:00Z", structured_plan={"weeks": [{"phase_label": "GPP", "days": [{"date": DAY,
        "day_type": "strength", "sessions": [dict(session_id="training", session_type="strength", title="Strength",
        blocks=[dict(block_id="training", block_type="strength", display_name="Arm work",
                     mechanical_load_regions=["elbow"], contact_level="none")])]}]}]})
    store.set_active_plan_id(athlete, plan)
    store.upsert_today_checkin(athlete, dict(plan_id=plan, training_day=DAY, recommendation_state="train_as_planned", pain="none", body="good"))
    result = store, None, None, athlete, plan, injury
    report_permission(result, permission)
    if assessed:
        capture(result, assessment(**changes))
    return result


def capture(b, value, report_id=None):
    observation = InjuryEpisodeObservation(injury_id=b[5]["id"], injury_episode_id=b[5]["episode_id"],
        event_type="rehab_progression_assessment", assessment=value, report_id=report_id or uuid4())
    row = record_episode_observation(b[0], athlete_id=b[3], observation=observation, training_day=DAY)
    row["created_at"] = (NOW - timedelta(hours=1)).isoformat()
    b[0].injury_episode_events[row["id"]] = row
    return row


def context(b, **kwargs):
    injury = apply_episode_observations(b[0].injury_flags[b[3]][0], list(b[0].injury_episode_events.values()), as_of=NOW)
    return AssessmentContext.from_injury(injury, as_of=NOW, **kwargs)


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
    assert events[0].body_region == "elbow" and events[0].side == "left"
    next_live = view(b).live_prescription
    assert not next_live or all(v.get("rehab_drill_id") != OPTION.drill_id for v in next_live["session"]["blocks"])


@pytest.mark.parametrize("changes", [dict(subtype="unknown"), dict(subtype="medial"), dict(subtype="posterior"),
    dict(grip_function="unknown"), dict(grip_function="not_acceptable"), dict(elbow_wrist_motion="unknown"),
    dict(pain_irritability="not_acceptable"), dict(wrist_extension_tolerance="unknown"),
    dict(wrist_extension_tolerance="not_acceptable"), dict(course="acute_traumatic"), dict(safety_screen="concern"),
    dict(option_recommended=None), dict(option_recommended=False), dict(assessor="self_reported")])
def test_unconfirmed_or_unsatisfactory_clinician_function_never_promotes(changes):
    b = bundle(**changes)
    assert evaluate_elbow_entry(context(b))["status"] != "pass"
    assert view(b).open_injuries[0]["rehab_decision"]["stage"] != "load"


def test_recorded_is_not_clinically_satisfactory():
    b = bundle(grip_function="not_acceptable")
    assert read_assessment_input(INPUT, context(b))["status"] == "pass"
    assert evaluate_elbow_entry(context(b))["status"] == "fail"


def test_old_good_assessment_recorded_again_cannot_supersede_later_bad_function():
    b = bundle(grip_function="not_acceptable")
    row = capture(b, assessment(assessed_at=(NOW - timedelta(days=1)).isoformat()))
    row["created_at"] = (NOW - timedelta(minutes=10)).isoformat()
    assert evaluate_elbow_entry(context(b))["reason_code"] == "elbow_later_unsatisfactory_function"


@pytest.mark.parametrize("permission", [None, "not_cleared", "gentle_recovery"])
def test_function_does_not_replace_separate_rehab_permission(permission):
    assert view(bundle(permission=permission)).open_injuries[0]["rehab_decision"]["stage"] == "restore"


def test_permission_alone_and_completion_alone_do_not_supply_function():
    b = bundle(assessed=False)
    assert view(b).open_injuries[0]["rehab_decision"]["stage"] == "restore"
    report_permission(b, "sport_specific", scopes=["rehab", "training", "contact"])
    assert view(b).open_injuries[0]["rehab_decision"]["stage"] == "restore"


@pytest.mark.parametrize("permission", ["loading", "sport_specific"])
def test_permission_and_completion_cannot_guess_elbow_location(permission):
    b = bundle(assessed=False, permission=permission)
    current = view(b)
    decision = current.open_injuries[0]["rehab_decision"]
    assert decision["stage"] == "restore"
    transition = decision["progression"]["next_transition"]
    assert transition["status"] == "blocked"
    assert "elbow_lateral_applicability_not_confirmed" in transition["reason_codes"]
    assert all(row["event_type"] != "rehab_progression_assessment" for row in b[0].injury_episode_events.values())
    # Free-text "lateral" is not a replacement for the explicit location answer.
    assert next(iter(b[0].rehab_exposures.values()))


def consumer_bundle(*, site="lateral", **kwargs):
    b = bundle(assessed=False, **kwargs)
    b[0].injury_flags[b[3]][0]["description"] += f" [elbow_site:{site}]"
    return b


@pytest.mark.parametrize("permission", ["loading", "sport_specific"])
def test_new_consumer_reaches_load_without_assessment_or_fake_function(permission):
    b = consumer_bundle(permission=permission)
    current = view(b)
    assert current.open_injuries[0]["rehab_decision"]["stage"] == "load"
    assert current.live_prescription["session"]["blocks"][0]["rehab_drill_id"] == OPTION.drill_id
    assert evaluate_elbow_permission(context(b))["status"] == "pass"
    assert read_assessment_input(INPUT, context(b))["status"] == "unknown"
    assert not any(e["event_type"] in {"rehab_progression_assessment", "clinical_progression_review"}
                   for e in b[0].injury_episode_events.values())
    execute(b, current.live_prescription)
    execute(b, current.live_prescription, "done", rehab_performance="done_as_shown")


@pytest.mark.parametrize("site", ["unknown", "other", "lateral] [elbow_site:unknown"])
def test_consumer_unknown_other_or_ambiguous_location_stays_closed(site):
    assert view(consumer_bundle(site=site)).open_injuries[0]["rehab_decision"]["stage"] == "restore"


@pytest.mark.parametrize("description", ["Medial elbow tendonitis", "Posterior elbow tendonitis", "Traumatic elbow tendonitis", "Elbow tendonitis with tingling"])
def test_consumer_location_cannot_override_conflicting_injury_details(description):
    b = consumer_bundle()
    b[0].injury_flags[b[3]][0]["description"] = description + " [elbow_site:lateral]"
    assert view(b).open_injuries[0]["rehab_decision"]["stage"] != "load"


@pytest.mark.parametrize("permission", [None, "gentle_recovery", "not_cleared"])
def test_consumer_contact_scope_does_not_replace_loading_permission(permission):
    b = consumer_bundle(permission=permission)
    report_permission(b, permission, scopes=["rehab", "training", "contact"])
    assert view(b).open_injuries[0]["rehab_decision"]["stage"] == "restore"


@pytest.mark.parametrize("field", ["during_response", "next_day_response"])
@pytest.mark.parametrize("answer", ["not_reported", "worse"])
def test_consumer_completion_and_better_do_not_override_missing_or_worse_response(field, answer):
    b = consumer_bundle()
    next(iter(b[0].rehab_exposures.values()))["event_json"]["response"][field] = answer
    assert view(b).open_injuries[0]["rehab_decision"]["stage"] != "load"


@pytest.mark.parametrize("mutation", ["location", "permission", "worse", "medical", "restriction", "side", "history", "equipment", "no_work"])
def test_consumer_changes_invalidate_unstarted_work_preserving_started_and_completed(mutation):
    b = consumer_bundle()
    live = deepcopy(view(b).live_prescription)
    assert not frozen_review_hold(b[0], b[3], live, work_state="unstarted", as_of=NOW)
    row = b[0].injury_flags[b[3]][0]
    if mutation == "location":
        row["description"] = row["description"].replace("elbow_site:lateral", "elbow_site:unknown")
    elif mutation == "permission":
        report_permission(b, "not_cleared")
    elif mutation == "worse":
        row["latest_reported_status"] = "worse"
    elif mutation in {"medical", "restriction"}:
        row[f"{mutation}_hold"] = True
    elif mutation == "side":
        row["side"] = "right"
    elif mutation == "history":
        from api.contracts.rehab_assessment import AssessmentHistory
        b[0].list_injury_episode_events = lambda *args, **kwargs: AssessmentHistory(list(b[0].injury_episode_events.values()), history_complete=False)
    elif mutation == "equipment":
        b[0].intakes[b[3]][0]["equipment_access"] = []
    elif mutation == "no_work":
        b[0].rehab_exposures.clear()
    original = deepcopy(live)
    # #3003: permission cannot replace progression evidence. Removing the
    # completed work invalidates unstarted LOAD just like the other mutations.
    assert frozen_review_hold(b[0], b[3], live, work_state="unstarted", as_of=NOW)
    assert not frozen_review_hold(b[0], b[3], live, work_state="started", as_of=NOW)
    assert not frozen_review_hold(b[0], b[3], live, work_state="completed", as_of=NOW)
    assert live == original


@pytest.mark.parametrize("changes", [dict(grip_function="not_acceptable"), dict(safety_screen="concern"),
    dict(elbow_wrist_motion="not_acceptable"), dict(wrist_extension_tolerance="not_acceptable"),
    dict(option_recommended=False), dict(subtype="medial"), dict(course="acute_traumatic")])
def test_consumer_location_and_permission_never_erase_known_adverse_observations(changes):
    b = consumer_bundle()
    capture(b, assessment(**changes))
    capture(b, assessment())
    assert evaluate_elbow_permission(context(b))["status"] == "fail"
    assert view(b).open_injuries[0]["rehab_decision"]["stage"] != "load"


def test_consumer_generation_uses_same_eligibility_without_assessment():
    from api.services.rehab_stage_snapshot import resolve_open_injury_rehab_context
    from fightcamp.input_parsing import _coerce_rehab_generation_context, _apply_rehab_generation_context
    from fightcamp.rehab_protocols import _episode_context, _reviewed_episode_option
    b = consumer_bundle()
    raw = next(iter(resolve_open_injury_rehab_context(b[0], b[3]).values()))
    raw["available_equipment"] = ["table"]
    entry = dict(injury_type="tendonitis", severity="mild", laterality="left")
    _apply_rehab_generation_context(entry, _coerce_rehab_generation_context(dict(rehab_generation_context=raw)))
    assert _reviewed_episode_option(_episode_context(entry), "elbow", "GPP")["decision"]["stage"] == "load"


@pytest.mark.parametrize("performance,eligible", [("done_as_shown", True), ("changed", False), ("skipped", False)])
def test_real_restore_completion_and_existing_checkins_supply_consumer_progression(performance, eligible):
    from api.services.injury_episode_service import exposure_rows_with_observations
    from api.services.rehab_completion_service import build_rehab_response_contexts, record_reported_permission_rehab
    b = consumer_bundle()
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
    row = next(iter(b[0].rehab_exposures.values()))
    row["created_at"] = NOW.isoformat()
    assert row["event_json"]["response"]["during_response"] == "not_reported"
    assert view(b).open_injuries[0]["rehab_decision"]["stage"] != "load"
    checkin = dict(id=str(uuid4()), athlete_id=b[3], injury_id=b[5]["id"], injury_episode_id=b[5]["episode_id"],
        event_type="injury_checkin", created_at=(NOW + timedelta(minutes=1)).isoformat(),
        payload=dict(explicit_report=True, latest_reported_status="ongoing"))
    b[0].injury_episode_events[checkin["id"]] = checkin
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
        equipment=["table"], exposures=exposure_rows_with_observations(list(b[0].rehab_exposures.values()), events),
        as_of=NOW + timedelta(days=1))
    assert decision["stage"] == ("load" if eligible else "restore")
    assert "entry_basis" not in decision["progression"]
    assert not any(e["event_type"] == "rehab_progression_assessment" for e in events)


@pytest.mark.parametrize("field", ["athlete_id", "injury_id", "injury_episode_id"])
def test_wrong_ownership_fails_closed(field):
    b = bundle()
    for e in b[0].injury_episode_events.values():
        if e["event_type"] == "rehab_progression_assessment":
            e[field] = str(uuid4())
    assert evaluate_elbow_entry(context(b))["status"] == "unknown"


@pytest.mark.parametrize("mutation", ["wrong_side", "future", "episode", "history", "worse", "medical", "restriction", "no_function", "equipment", "permission", "setback"])
def test_future_frozen_work_invalidated_without_rewriting_history(mutation):
    b = bundle()
    live = deepcopy(view(b).live_prescription)
    row = b[0].injury_flags[b[3]][0]
    observed = next(e for e in b[0].injury_episode_events.values() if e["event_type"] == "rehab_progression_assessment")
    if mutation == "wrong_side":
        observed["payload"]["assessment"]["side"] = "right"
    elif mutation == "future":
        observed["payload"]["assessment"]["assessed_at"] = (NOW + timedelta(days=1)).isoformat()
    elif mutation == "episode":
        row["episode_id"] = str(uuid4())
    elif mutation == "history":
        from api.contracts.rehab_assessment import AssessmentHistory
        b[0].list_injury_episode_events = lambda *args, **kwargs: AssessmentHistory(list(b[0].injury_episode_events.values()), history_complete=False)
    elif mutation == "worse":
        row["latest_reported_status"] = "worse"
    elif mutation in {"medical", "restriction"}:
        row[f"{mutation}_hold"] = True
    elif mutation == "no_function":
        observed["payload"]["assessment"]["payload"]["grip_function"] = "unknown"
    elif mutation == "equipment":
        b[0].intakes[b[3]][0]["equipment_access"] = []
    elif mutation == "permission":
        for e in b[0].injury_episode_events.values():
            if e["event_type"] == "clinician_clearance_report":
                e["payload"]["rehabilitation_permission"]["level"] = "not_cleared"
    elif mutation == "setback":
        exposure = next(iter(b[0].rehab_exposures.values()))
        exposure["event_json"]["response"]["during_response"] = "worse"
        exposure["response_recorded_at"] = (NOW - timedelta(minutes=30)).isoformat()
    original = deepcopy(live)
    assert frozen_review_hold(b[0], b[3], live, work_state="unstarted", as_of=NOW)
    assert live == original
    assert not frozen_review_hold(b[0], b[3], live, work_state="completed", as_of=NOW)


@pytest.mark.parametrize("description,kind", [("Elbow tightness", "tightness"), ("Medial elbow tendonitis", "tendonitis"),
    ("Posterior elbow tendonitis", "tendonitis"), ("Elbow tendonitis with nerve symptoms", "tendonitis")])
def test_other_presentations_cannot_borrow_lateral_observations(description, kind):
    b = bundle()
    row = b[0].injury_flags[b[3]][0]
    row.update(description=description, injury_type=kind)
    assert view(b).open_injuries[0]["rehab_decision"]["stage"] != "load"


def test_missing_responses_and_history_hold_below_permission_ceiling():
    for field in ("during_response", "next_day_response"):
        b = bundle()
        next(iter(b[0].rehab_exposures.values()))["event_json"]["response"][field] = "not_reported"
        assert view(b).open_injuries[0]["rehab_decision"]["stage"] == "restore"
    b = bundle()
    assert view(b).open_injuries[0]["rehab_decision"]["stage"] == "load"
    b[0].rehab_exposures.clear()
    # #3003: reported permission is a ceiling, never progression evidence.
    decision = view(b).open_injuries[0]["rehab_decision"]
    assert decision["stage"] == "restore"
    assert "no_reviewed_stage_exposure" in decision["progression"]["next_transition"]["reason_codes"]


def test_no_dynamic_return_or_training_grant():
    b = bundle(permission="sport_specific")
    v = view(b)
    d = v.open_injuries[0]["rehab_decision"]
    assert d["stage"] == "load" and not d["progression"]["next_transition"]["target_stage_live"]
    assert v.effective_clinician_clearance["level"] == "rehab_only"
    assert next(p for p in load_clinical_policies() if p.policy_id == "elbow_tendonitis").live_stages == ["calm", "restore", "load"]


def test_capture_side_episode_timestamps_idempotency_and_no_fake_verification():
    b = bundle(assessed=False)
    identity = uuid4()
    first = capture(b, assessment(), identity)
    assert capture(b, assessment(), identity) == first
    assert first["payload"]["source"] == "athlete_reported" and first["payload"]["externally_verified"] is False
    for change in (dict(side="right"), dict(assessed_at="2020-01-01T00:00:00Z"), dict(assessed_at="2099-01-01T00:00:00Z")):
        with pytest.raises(HTTPException):
            capture(b, assessment(**change))


def test_generation_uses_same_owned_gate_and_option():
    from api.services.rehab_stage_snapshot import resolve_open_injury_rehab_context
    from fightcamp.input_parsing import _coerce_rehab_generation_context, _apply_rehab_generation_context
    from fightcamp.rehab_protocols import _episode_context, _reviewed_episode_option
    b = bundle()
    contexts = resolve_open_injury_rehab_context(b[0], b[3])
    assert len(contexts) == 1
    raw = next(iter(contexts.values()))
    raw["available_equipment"] = ["table"]
    parsed = _coerce_rehab_generation_context(dict(rehab_generation_context=raw))
    entry = dict(injury_type="tendonitis", severity="mild", laterality="left")
    _apply_rehab_generation_context(entry, parsed)
    generated = _reviewed_episode_option(_episode_context(entry), "elbow", "GPP")
    assert generated["decision"]["stage"] == "load" and generated["drill"]["id"] == OPTION.drill_id
    assert generated["decision"]["prescription"]["dose"] == dict(sets=1, reps=10)
    row = context(b).injury
    assert resolve_injury_policy(row, policies=load_clinical_policies(), bank=get_rehab_bank(),
        equipment=["table"], exposures=list(b[0].rehab_exposures.values()), as_of=NOW)["stage"] == "load"


@pytest.mark.parametrize("changes,severity,eligible", [(dict(), "mild", True),
    (dict(permission="not_cleared"), "mild", False), (dict(subtype="unknown"), "mild", False),
    (dict(subtype="medial"), "mild", False), (dict(grip_function="not_acceptable"), "mild", False),
    (dict(), "severe", False)])
def test_full_guided_intake_to_generation_uses_owned_elbow_policy(changes, severity, eligible):
    from api.services.rehab_stage_snapshot import annotate_payload_with_rehab_stage
    from fightcamp.input_parsing import _extract_guided_injuries, _parse_guided_injuries
    from fightcamp.rehab_protocols import generate_rehab_protocols

    b = bundle(**changes)
    payload = annotate_payload_with_rehab_stage(dict(guided_injuries=[dict(
        area="Left elbow", severity=severity, trend="improving", injury_type="tendon_ligament",
        notes="Lateral elbow tendonitis", timeframe="three_plus_months")]), store=b[0], athlete_id=b[3])
    entries, _ = _parse_guided_injuries(_extract_guided_injuries(payload))
    block, _ = generate_rehab_protocols(injury_string="left elbow tendonitis",
        exercise_data=[], current_phase="GPP", parsed_entries=entries)
    assert ("Supported hand-weight wrist extension" in block) is eligible
    assert "Arm Bar Stretch" not in block
    if eligible:
        assert "1 x 10 reps" in block and OPTION.instructions in block


def test_multi_injury_and_started_history_remain_authoritative():
    b = bundle()
    live = view(b).live_prescription
    saved = deepcopy(execute(b, live)["prescription_snapshot"])
    report_permission(b, "not_cleared")
    assert not frozen_review_hold(b[0], b[3], saved, work_state="started", as_of=NOW)
    b[0].create_injury_flag(b[3], dict(body_area="Wrist", description="Wrist strain", severity="severe", status="open"))
    assert view(b).live_prescription["safety_hold"]
    assert b[0].session_completions[b[3]][0]["prescription_snapshot"] == saved


def test_rehab_youtube_media_is_exact_identity_decoration_not_prescription():
    from api.services.exercise_media import reset_media_index_cache
    b = bundle()
    initial = view(b).live_prescription
    b[0].list_exercise_media = lambda: [dict(exercise_key=OPTION.drill_id,
        video_id="hQgFixeXdZo", source="curated", made_for_kids=False, start_s=5, end_s=20)]
    reset_media_index_cache()
    try:
        decorated = view(b)
        assert decorated.exercise_media["exercise:elbow-tendonitis-supported-hand-weight-wrist-extension"].video_id == "hQgFixeXdZo"
        assert decorated.live_prescription == initial
        assert all("video_id" not in block for block in decorated.live_prescription["session"]["blocks"])
    finally:
        reset_media_index_cache()


@pytest.mark.parametrize("field", ["id", "episode_id", "athlete_id", "side", "injury_type"])
def test_generation_cannot_mix_owned_observations_with_another_episode(field):
    from api.services.rehab_stage_snapshot import resolve_open_injury_rehab_context
    from fightcamp.input_parsing import _coerce_rehab_generation_context, _apply_rehab_generation_context
    from fightcamp.rehab_protocols import _episode_context, _reviewed_episode_option
    b = bundle()
    raw = next(iter(resolve_open_injury_rehab_context(b[0], b[3]).values()))
    raw["policy_injury"][field] = "another"
    entry = dict(injury_type="tendonitis", severity="mild", laterality="left")
    _apply_rehab_generation_context(entry, _coerce_rehab_generation_context(dict(rehab_generation_context=raw)))
    generated = _reviewed_episode_option(_episode_context(entry), "elbow", "GPP")
    assert generated["decision"]["stage"] != "load"


def test_unstarted_work_rechecks_current_stage_evidence_not_just_function():
    b = bundle()
    live = deepcopy(view(b).live_prescription)
    exposure = next(iter(b[0].rehab_exposures.values()))
    exposure["event_json"]["response"]["next_day_response"] = "not_reported"
    assert evaluate_elbow_entry(context(b))["status"] == "pass"
    assert frozen_review_hold(b[0], b[3], live, work_state="unstarted", as_of=NOW)
    assert not frozen_review_hold(b[0], b[3], live, work_state="started", as_of=NOW)


def test_elbow_activation_preserves_both_existing_baselines_and_achilles_profile():
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "tools/rehab_elbow_activation_inventory_baseline.json").read_text(encoding="utf-8"))
    from tools.rehab_metadata_review_lib import before_reported_permission_entry
    profiles = before_reported_permission_entry(json.loads((root / "data/rehab_pathways.json").read_text(encoding="utf-8")))["profiles"]
    elbow = next(p for p in profiles if p["policy_id"] == "elbow_tendonitis")
    assert elbow["prescriptions"][:2] == manifest["historical_profile"]["prescriptions"]
    assert next(p for p in profiles if p["policy_id"] == "achilles_tendonitis")["content_hash"] == "5ff715a474424088cf1549c6b38cc3f1fa51b25b7da118ec1ca767f330d55a1a"
