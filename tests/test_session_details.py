"""Read-only History evidence, with deliberate occurrence/provenance collisions."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.services.session_details import session_details, MISSING_DETAILS
from tests.support import _build_client

ATHLETE = "athlete-1"
DAY = "2026-10-10"
PLAN = str(uuid4())


def block(id="squat", **changes):
    return {"block_id": id, "block_type": "strength", "display_name": id.title(),
            "sets": 4, "reps": 6, "rest": {"value": 120, "unit": "seconds"}, **changes}


def occurrence(blocks=None, **changes):
    return {"id": str(uuid4()), "athlete_id": ATHLETE, "plan_id": PLAN, "session_id": "strength-1",
            "training_day": DAY, "status": "done", "completion_origin": "reported",
            "prescription_snapshot": {"plan_id": PLAN, "training_day": DAY,
                "session": {"session_id": "strength-1", "title": "Strength & Power", "blocks": blocks if blocks is not None else [block()]}},
            **changes}


def log(id="squat", **changes):
    return {"athlete_id": ATHLETE, "plan_id": PLAN, "session_id": "strength-1", "training_day": DAY,
            "block_id": id, "prescribed": block(id), "actual": {}, "status": "as_prescribed", **changes}


def store_for(row, logs=()):
    return SimpleNamespace(get_session_completion_by_id=Mock(return_value=row),
        list_session_detail_exercise_logs=Mock(return_value=list(logs)),
        list_rehab_exposures_by_ids=Mock(return_value=[]))


def read(store, row, consent=True):
    return session_details(store, ATHLETE, "unlxck", row["id"], health_consent_granted=consent)


def test_strength_order_explicit_actuals_modification_skip_and_unknown():
    row = occurrence([block(), block("plank", sets=None, reps=None, duration={"value": 45, "unit": "seconds"}),
                      block("row"), block("jump")])
    logs = [log("plank", status="modified", actual={"duration": {"value": 30, "unit": "seconds"}}),
            log(actual={"sets": 3, "reps": 5, "load": {"value": 80, "unit": "kg"},
                        "rest": {"value": 150, "unit": "seconds"}, "effort": {"value": 8}}, status="modified"),
            log("row", status="skipped")]
    result = read(store_for(row, logs), row)
    assert [b.name for b in result.blocks] == ["Squat", "Plank", "Row", "Jump"]
    squat, plank, skipped, unknown = result.blocks
    assert squat.prescribed["sets"] == 4 and squat.actual.sets == 3
    assert squat.actual.reps == 5 and squat.actual.load.value == 80 and squat.actual.load.unit == "kg"
    assert squat.prescribed["rest"]["value"] == 120 and squat.actual.rest.value == 150
    assert squat.actual.effort.value == 8
    assert plank.actual.duration.value == 30 and plank.prescribed["duration"]["value"] == 45
    assert skipped.status == "skipped"
    assert unknown.status is None and unknown.actual.model_dump(exclude_none=True) == {}


def test_empty_actual_does_not_infer_prescribed_completion():
    row = occurrence([block(), block("row"), block("jump")])
    result = read(store_for(row, [log(), log("row", status="modified")]), row)
    assert [b.status for b in result.blocks] == ["as_prescribed", "modified", None]
    assert all(b.actual.sets is None for b in result.blocks)


@pytest.mark.parametrize("field,value", [("athlete_id", "other"), ("plan_id", "other"),
    ("training_day", "2026-10-09"), ("session_id", "evening"), ("session_id", None), ("block_id", "other")])
def test_same_name_and_date_never_cross_identity(field, value):
    row = occurrence()
    result = read(store_for(row, [log(**{field: value}, actual={"sets": 1})]), row)
    assert result.blocks[0].actual.sets is None


def test_aggregate_source_session_id_requires_the_frozen_block_identity():
    row = occurrence([block(source_session_id="morning")])
    store = store_for(row, [log(session_id="morning", actual={"sets": 3}), log("other", session_id="morning")])
    assert read(store, row).blocks[0].actual.sets == 3
    assert store.list_session_detail_exercise_logs.call_args.kwargs["session_ids"] == ["morning", "strength-1"]


def test_duplicate_log_or_block_id_cannot_attach_actuals_twice():
    row = occurrence()
    assert read(store_for(row, [log(), log()]), row).blocks[0].status is None
    row = occurrence([block(), block()])
    assert all(b.status is None for b in read(store_for(row, [log()]), row).blocks)


@pytest.mark.parametrize("performance,state", [("done_as_shown", "quantified"),
    ("changed", "partial_amount_unknown"), ("stopped", "partial_amount_unknown"),
    (None, "performed_amount_unknown"), ("skipped", None)])
def test_rehab_uses_existing_explicit_completion_contract(performance, state):
    row = occurrence([block("calf", block_type="rehab")], rehab_performance=performance)
    result = read(store_for(row), row).blocks[0]
    assert result.status == performance
    assert (result.completed_dose.completion_state if result.completed_dose else None) == state
    if state == "quantified":
        assert result.completed_dose.sets == 4 and result.completed_dose.reps == 6
    elif result.completed_dose:
        assert result.completed_dose.sets is None and result.completed_dose.reps is None


def test_modified_session_can_explicitly_confirm_rehab_and_guidance_is_not_dose():
    row = occurrence([block("calf", block_type="rehab")], status="modified", rehab_performance="done_as_shown")
    assert read(store_for(row), row).blocks[0].completed_dose.sets == 4
    row = occurrence([block("guidance", block_type="rehab", sets=None, reps=None,
        drill_snapshot={"rehab_stage": "calm", "function": "recovery_downregulation"}, instructions=["Rest and comfortable movement."])], rehab_performance="done_as_shown")
    item = read(store_for(row), row).blocks[0]
    assert item.kind == "guidance" and item.completed_dose is None
    assert item.prescribed["prescription_text"] == "Rest and comfortable movement."


def test_legacy_and_deleted_plans_never_read_a_current_plan():
    row = occurrence(prescription_snapshot=None)
    result = read(store_for(row, [log(actual={"sets": 2})]), row)
    assert result.prescription_source == "exercise_logs" and result.blocks[0].actual.sets == 2
    assert read(store_for(row), row).message == MISSING_DETAILS
    row = occurrence(plan_id=None)
    store = store_for(row, [log(actual={"sets": 2})])
    result = read(store, row)
    assert result.prescription_source == "snapshot" and result.blocks[0].status is None
    store.list_session_detail_exercise_logs.assert_not_called()
    row = occurrence(plan_id=None, prescription_snapshot=None)
    assert read(store_for(row), row).message == MISSING_DETAILS


def test_standalone_rehab_history_keeps_its_saved_prescription_without_a_plan():
    row = occurrence([block("rehab", block_type="rehab")], plan_id=None, rehab_performance="done_as_shown")
    row["prescription_snapshot"].update(plan_id=None, rehab_only=True)
    row["prescription_snapshot"]["session"]["session_type"] = "rehab"
    store = store_for(row)
    result = read(store, row)
    assert result.prescription_source == "snapshot"
    assert result.blocks[0].completed_dose.sets == 4
    store.list_session_detail_exercise_logs.assert_not_called()
    row["prescription_snapshot"]["rehab_only"] = False
    assert read(store_for(row), row).message == MISSING_DETAILS


@pytest.mark.parametrize("snapshot", [[], {"session": []}, {"plan_id": "other", "session": {"session_id": "strength-1"}},
    {"plan_id": PLAN, "training_day": "wrong", "session": {"session_id": "strength-1"}}])
def test_malformed_or_mismatched_snapshots_fail_to_missing_details(snapshot):
    row = occurrence(prescription_snapshot=snapshot)
    assert read(store_for(row), row).message == MISSING_DETAILS


def test_current_consent_and_allowlists_hide_health_and_internal_metadata():
    row = occurrence([block(), block("rehab", block_type="rehab", policy_id="secret")],
                     pain_after=3, notes="private", modification_reason="private")
    store = store_for(row, [log(notes="private", reason="pain", actual={"sets": 2})])
    result = read(store, row, False)
    assert result.session.pain_after is None and result.session.notes is None
    assert len(result.blocks) == 1 and result.blocks[0].notes is None and result.blocks[0].reason is None
    store.list_rehab_exposures_by_ids.assert_not_called()
    row["notes"] = "Coach cue [injury_type:secret]"
    assert read(store, row).session.notes == "Coach cue"
    assert "policy_id" not in read(store, row).model_dump_json()


def test_endpoint_owns_occurrence_manual_details_and_no_writes():
    client, store, _ = _build_client()
    row = store.upsert_session_completion(ATHLETE, occurrence())
    before = deepcopy(store.session_completions)
    path = f"/api/training/history/unlxck/{row['id']}"
    assert client.get(path).status_code == 401
    result = client.get(path, headers={"Authorization": "Bearer athlete-token"})
    assert result.status_code == 200 and result.json()["session"]["id"] == row["id"]
    assert store.session_completions == before
    assert client.get(path, headers={"Authorization": "Bearer other-athlete-token"}).status_code in {401, 404}
    from tests.test_api_service_role_authorization import _create_two_athletes
    _create_two_athletes(client, store)
    assert client.get(path, headers={"Authorization": "Bearer athlete-b-token"}).status_code == 404
    manual = store.create_external_training_session(ATHLETE, {"training_day": DAY, "activity": "boxing",
        "duration_minutes": 20, "effort": "moderate", "client_request_id": str(uuid4()), "request_fingerprint": "a" * 64})
    result = client.get(f"/api/training/history/external/{manual['id']}", headers={"Authorization": "Bearer athlete-token"})
    assert result.status_code == 200
    assert result.json()["blocks"] == [] and result.json()["session"]["duration_minutes"] == 20
    assert client.get(f"/api/training/history/external/{manual['id']}", headers={"Authorization": "Bearer athlete-b-token"}).status_code == 404


def test_another_athletes_completion_is_rejected_even_if_store_misbehaves():
    row = occurrence(athlete_id="other")
    with pytest.raises(HTTPException) as error:
        read(store_for(row), row)
    assert error.value.status_code == 404


def rehab_evidence(row, *, partial=False):
    exposure_id, injury, episode, group = [str(uuid4()) for _ in range(4)]
    context = {"version": 1, "athlete_id": ATHLETE, "plan_id": row["plan_id"],
        "session_id": row["session_id"], "training_day": DAY, "session_completion_id": row["id"],
        "injury_id": injury, "injury_episode_id": episode, "response_group_id": group,
        "expected_exposures": [{"exposure_id": exposure_id, "drill_id": "calf_raise", "rehab_occurrence_key": "block:calf"}]}
    row["rehab_response_contexts"] = [context]
    return {"id": exposure_id, "athlete_id": ATHLETE, "event_json": {
        "exposure_id": exposure_id, "injury_id": injury, "injury_episode_id": episode, "response_group_id": group,
        "drill_id": "calf_raise", "body_region": "ankle", "side": "left",
        "demand": {"target_regions": ["ankle"], "load": "low", "impact": "none", "velocity": "low"},
        "dose_completed": {"completion_state": "partial_amount_unknown", "stopped_early": True} if partial
            else {"sets": 4, "reps": 6, "completion_state": "quantified"},
        "response": {"during_response": "worse" if partial else "same", "stopped_due_to_symptoms": partial},
        "occurred_at": DAY + "T12:00:00Z", "provenance": {"source": "athlete_logged_rehab", "recorded_at": DAY + "T12:00:00Z"}}}


@pytest.mark.parametrize("partial", [False, True])
def test_rehab_response_exact_context_and_later_reduction_override(partial):
    row = occurrence([block("calf", block_type="rehab")], rehab_performance="done_as_shown")
    event = rehab_evidence(row, partial=partial)
    store = store_for(row)
    store.list_rehab_exposures_by_ids.return_value = [event]
    item = read(store, row).blocks[0]
    assert item.responses[0].during_response == ("worse" if partial else "same")
    assert item.completed_dose.sets == (None if partial else 4)
    assert item.status == ("changed" if partial else "done_as_shown")
    assert "injury_id" not in read(store, row).model_dump_json()


@pytest.mark.parametrize("part", ["athlete", "completion", "day", "episode", "group", "drill", "event_id", "block"])
def test_rehab_responses_cannot_cross_contexts(part):
    row = occurrence([block("calf", block_type="rehab")])
    event = rehab_evidence(row)
    context = row["rehab_response_contexts"][0]
    if part == "athlete":
        event["athlete_id"] = "other"
    elif part in {"completion", "day"}:
        context["session_completion_id" if part == "completion" else "training_day"] = "wrong"
    elif part == "block":
        context["expected_exposures"][0]["rehab_occurrence_key"] = "block:other"
    else:
        event["event_json"][{"episode": "injury_episode_id", "group": "response_group_id", "drill": "drill_id", "event_id": "exposure_id"}[part]] = str(uuid4()) if part != "drill" else "other_drill"
    store = store_for(row)
    store.list_rehab_exposures_by_ids.return_value = [event]
    assert read(store, row).blocks[0].responses == []


def test_owner_scoped_store_queries_filter_every_identity():
    from unittest.mock import MagicMock
    from api.store import SupabaseAppStore
    client = MagicMock()
    store = SupabaseAppStore(client=client, admin_emails=set())
    store.get_session_completion_by_id(ATHLETE, "completion")
    query = client.table.return_value.select.return_value
    query.eq.assert_called_with("athlete_id", ATHLETE)
    query.eq.return_value.eq.assert_called_with("id", "completion")
    client.reset_mock()
    store.list_session_detail_exercise_logs(ATHLETE, plan_id=PLAN, training_day=DAY, session_ids=["morning"])
    query.eq.assert_called_with("athlete_id", ATHLETE)
    query.eq.return_value.eq.assert_called_with("plan_id", PLAN)
    query.eq.return_value.eq.return_value.eq.assert_called_with("training_day", DAY)
    query.eq.return_value.eq.return_value.eq.return_value.in_.assert_called_with("session_id", ["morning"])


@pytest.mark.parametrize("consent", [True, False])
def test_manual_sparring_reads_only_its_linked_report_and_redacts_health(consent):
    entry_id = str(uuid4())
    row = {"id": entry_id, "athlete_id": ATHLETE, "training_day": DAY, "activity": "sparring",
           "duration_minutes": 40, "effort": "hard", "takeaway": "Keep the jab active"}
    report = {"athlete_id": ATHLETE, "external_session_id": entry_id, "intensity": "hard",
              "rounds_completed": 5, "round_seconds": None, "head_contact": "light", "rocked": False,
              "notes": "Coach feedback", "admin_review_id": "private"}
    store = SimpleNamespace(get_external_training_session=Mock(return_value=row),
                            get_external_session_sparring_log=Mock(return_value=report),
                            list_manual_exercises=Mock(return_value=[]))
    result = session_details(store, ATHLETE, "external", entry_id, health_consent_granted=consent)
    assert result.blocks == [] and result.sparring.rounds_completed == 5
    assert result.sparring.round_seconds is None
    assert result.sparring.notes == ("Coach feedback" if consent else None)
    assert result.sparring.head_contact == ("light" if consent else None)
    assert "admin_review_id" not in result.model_dump_json()
    report["external_session_id"] = str(uuid4())
    assert session_details(store, ATHLETE, "external", entry_id, health_consent_granted=consent).sparring is None


def test_old_snapshot_without_block_id_shows_prescription_without_name_join():
    legacy_block = block()
    del legacy_block["block_id"]
    row = occurrence([legacy_block])
    result = read(store_for(row, [log(actual={"sets": 2})]), row)
    assert result.blocks[0].prescribed["sets"] == 4
    assert result.blocks[0].status is None and result.blocks[0].actual.sets is None


def test_a_run_carries_every_run_through_its_week_not_just_a_history_page():
    client, store, _ = _build_client()
    auth = {"Authorization": "Bearer athlete-token"}

    def add(day, activity="roadwork", **fields):
        return store.create_external_training_session(ATHLETE, {"training_day": day, "activity": activity,
            "duration_minutes": 30, "effort": "easy", "client_request_id": str(uuid4()), "request_fingerprint": "a" * 64, **fields})

    # More runs than one History page (200) reads, the longest one oldest.
    add("2020-01-06", distance_value=40, distance_unit="km")
    for index in range(210):
        add(f"2021-{index // 28 + 1:02d}-{index % 28 + 1:02d}", distance_value=5, distance_unit="km")
    run = add("2026-11-04", distance_value=10, distance_unit="km", duration_seconds=3000, duration_minutes=50, run_type="long")
    add("2026-11-08")  # same week (Sunday): counted
    add("2026-11-09")  # next week: not sent
    add("2026-11-05", activity="boxing")
    removed = add("2026-11-03")
    store.delete_external_training_session(ATHLETE, removed["id"])

    body = client.get(f"/api/training/history/external/{run['id']}", headers=auth).json()
    days = [item["training_day"] for item in body["runs"]]
    assert len(days) == 213 and days[0] == "2020-01-06" and days[-1] == "2026-11-08"
    assert body["runs"][0]["distance_value"] == 40
    assert next(item for item in body["runs"] if item["id"] == run["id"])["run_type"] == "long"

    boxing = add("2026-11-02", activity="boxing")
    assert client.get(f"/api/training/history/external/{boxing['id']}", headers=auth).json()["runs"] is None
