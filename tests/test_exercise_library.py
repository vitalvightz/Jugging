"""Personal exercise index completeness, performed evidence and stable history paging."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import UUID

import pytest
from fastapi import HTTPException

from api.services.exercise_library import exercise_library, exercise_performances
from api.store import SupabaseAppStore
from tests.support import FakeStore, _build_client

ATHLETE = "athlete-1"
NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)


def entry(number=1, key="back-squat", **changes):
    return {"id": str(UUID(int=number)), "athlete_id": ATHLETE, "exercise_key": key, "plan_id": "old-plan",
        "session_id": "strength", "block_id": f"block-{number}", "training_day": "2026-10-10",
        "status": "as_prescribed", "actual": {}, "prescribed": {"display_name": "Back Squat", "block_type": "strength", "sets": 3, "reps": 5},
        "created_at": "2026-10-10T12:00:00+00:00", "updated_at": "2026-10-10T12:00:00+00:00", **changes}


def store_with(*rows):
    store = FakeStore()
    store.exercise_logs = list(rows)
    return store


def library(*rows):
    return exercise_library(store_with(*rows), ATHLETE, now=NOW)


def history(store, identity="key:back-squat", **kwargs):
    return exercise_performances(store, ATHLETE, identity, as_of=NOW.isoformat(), health_consent_granted=True, **kwargs)


def test_chart_values_share_index_eligibility_and_preserve_reported_vs_prescribed():
    rows = [
        entry(1, actual={"load": {"value": 80, "unit": "kg"}}),
        entry(2, prescribed={"reps": 5, "load": {"value": 90, "unit": "kg"}}),
        entry(3, status="modified", actual={"sets": 2}, prescribed={"reps": 5, "load": {"value": 900, "unit": "kg"}}),
        entry(4, status="skipped", actual={"load": {"value": 999, "unit": "kg"}}),
        entry(5, actual={"sets": 0, "load": {"value": 999, "unit": "kg"}}),
        entry(6, prescribed={"reps": "8-12", "load": {"display": "500 kg"}}),
    ]
    values = {item.id: item for item in history(store_with(*rows)).performances}
    assert values[rows[0]["id"]].performed_values.load.value == 80
    assert values[rows[0]["id"]].performed_values.reps == 5
    confirmed = values[rows[1]["id"]]
    assert confirmed.performed_values.load.value == 90
    assert confirmed.block.actual.load is None
    assert values[rows[2]["id"]].performed_values.sets == 2
    assert values[rows[2]["id"]].performed_values.load is None
    assert values[rows[3]["id"]].performed_values is None
    assert values[rows[4]["id"]].performed_values is None
    assert values[rows[5]["id"]].performed_values.load is None
    assert values[rows[5]["id"]].performed_values.reps is None


def test_complete_index_reaches_old_movements_and_best_beyond_first_50_and_1000_logs():
    rows = [entry(i, actual={"load": {"value": 80, "unit": "kg"}}) for i in range(1, 1040)]
    rows[0]["actual"]["load"]["value"] = 150
    rows[0]["training_day"] = "2025-01-01"
    rows.append(entry(2000, key="old-plank", prescribed={"display_name": "Old plank", "duration": {"value": 2, "unit": "minutes"}}))
    store = store_with(*rows)
    original = store.list_exercise_index_page
    # A lower PostgREST row cap must not be mistaken for end-of-history.
    store.list_exercise_index_page = Mock(side_effect=lambda *a, **kw: original(*a, **{**kw, "limit": 73}))
    result = exercise_library(store, ATHLETE, now=NOW)
    assert len(result.exercises) == 2
    squat = next(item for item in result.exercises if item.id == "key:back-squat")
    assert squat.best_loads[0].value == 150
    assert store.list_exercise_index_page.call_count > 14
    assert next(item for item in result.exercises if item.name == "Old plank").latest_values.duration.value == 2


def test_twenty_performances_give_one_canonical_card_and_variations_never_fuzzy_merge():
    rows = [entry(i, prescribed={"display_name": f"Squat wording {i}"}) for i in range(1, 21)]
    rows += [entry(21, key="front-squat"), entry(22, key="back-squat-paused")]
    result = library(*rows)
    assert len(result.exercises) == 3
    assert {item.id for item in result.exercises} == {"key:back-squat", "key:front-squat", "key:back-squat-paused"}


def test_actual_and_explicit_confirmed_loads_only_and_each_unit_has_its_own_maximum():
    result = library(
        entry(1, actual={"load": {"value": 90, "unit": "kg"}}),
        entry(2, prescribed={"display_name": "Squat", "load": {"method": "absolute", "value": 100, "unit": "kg", "display": "100 kg"}}),
        entry(3, status="modified", actual={"sets": 2}, prescribed={"load": {"method": "absolute", "value": 999, "unit": "kg"}}),
        entry(4, status="skipped", actual={"load": {"value": 9999, "unit": "kg"}}),
        entry(5, actual={"load": {"value": 200, "unit": "lb"}}),
        entry(6, prescribed={"load": {"method": "percentage", "value": 95, "unit": "percent", "display": "95% 1RM"}}),
        entry(7, prescribed={"load": {"method": "bodyweight", "value": 120, "unit": "kg"}}),
        entry(8, prescribed={"load": {"display": "250 kg"}}),
    ).exercises[0]
    assert [(load.value, load.unit) for load in result.best_loads] == [(100, "kg"), (200, "lb")]
    assert result.best_loads[0].basis == "confirmed_prescription"
    assert result.best_loads[1].basis == "reported"


def test_a_reported_load_outranks_an_equal_confirmed_prescription_in_either_order():
    prescribed = {"display_name": "Squat", "load": {"method": "absolute", "value": 100, "unit": "kg", "display": "100 kg"}}
    reported = {"load": {"value": 100, "unit": "kg"}}
    for rows in ([entry(1, actual=reported), entry(2, prescribed=prescribed)],
                 [entry(1, prescribed=prescribed), entry(2, actual=reported)]):
        assert [(load.value, load.basis) for load in library(*rows).exercises[0].best_loads] == [(100, "reported")]


@pytest.mark.parametrize("changes", [
    {"status": "skipped"}, {"status": "modified", "actual": {}}, {"status": "modified", "actual": {"sets": None}},
    {"status": "modified", "actual": {"sets": 0, "load": {"value": 100, "unit": "kg"}}},
    {"actual": {"duration": {"value": 0, "unit": "seconds"}}},
    {"actual": {"load": {"value": 100, "unit": "percent"}}}, {"actual": None},
    {"actual": {"load": {"value": True, "unit": "kg"}}}, {"actual": {"load": {"value": "200", "unit": "kg"}}},
    {"prescribed": {"sets": 0, "load": {"value": 100, "unit": "kg", "method": "absolute"}}},
    {"prescribed": {"block_type": "rehab"}}, {"prescribed": {"block_type": "mindset"}},
])
def test_skipped_unknown_unperformed_or_invalid_values_cannot_be_achievements(changes):
    assert library(entry(**changes)).exercises == []


def test_modified_dose_does_not_receive_prescribed_sets_reps_or_duration():
    result = library(entry(status="modified", actual={"load": {"value": 75, "unit": "kg"}},
        prescribed={"sets": 4, "reps": 8, "duration": {"value": 120, "unit": "seconds"}})).exercises[0]
    assert result.latest_values.load.value == 75
    assert result.latest_values.sets is None and result.latest_values.reps is None and result.latest_values.duration is None
    assert not result.latest_confirmed_as_prescribed


def test_bodyweight_isometric_and_intervals_keep_applicable_numeric_evidence():
    result = library(entry(1, key="push-ups", actual={"sets": 3, "reps": 15}),
        entry(2, key="plank", prescribed={"display_name": "Plank", "duration": {"value": 2, "unit": "minutes"}}),
        entry(3, key="sprint-intervals", actual={"rounds": 6, "work": {"value": 30, "unit": "seconds"},
            "rest": {"value": 90, "unit": "seconds"}, "distance": {"value": 100, "unit": "meters"}, "effort": {"value": 8}}))
    values = {item.id: item.latest_values for item in result.exercises}
    assert values["key:push-ups"].reps == 15
    assert values["key:plank"].duration.value == 2
    assert values["key:sprint-intervals"].rounds == 6
    assert all(not item.best_loads for item in result.exercises)


def test_legacy_rows_remain_separate_without_invented_movement_identity_and_orphans_survive():
    rows = [entry(1, key=None), entry(2, key=None, plan_id=None), entry(3, plan_id=None)]
    result = library(*rows)
    assert {item.id for item in result.exercises} == {f"log:{rows[0]['id']}", f"log:{rows[1]['id']}", "key:back-squat"}
    assert sum(item.identity_kind == "legacy" for item in result.exercises) == 2
    assert len(history(store_with(*rows), f"log:{rows[1]['id']}").performances) == 1


def test_paging_same_day_is_stable_after_update_times_change_and_excludes_new_insertions():
    store = store_with(*(entry(i) for i in range(1, 11)))
    seen = []
    page = history(store, limit=3)
    seen += [item.id for item in page.performances]
    for row in store.exercise_logs:
        row["updated_at"] = "2026-10-20T12:00:00+00:00"
    store.exercise_logs.append(entry(20, created_at="2026-10-20T12:00:00+00:00"))
    while page.next_cursor:
        page = history(store, cursor=page.next_cursor, limit=3)
        seen += [item.id for item in page.performances]
    assert len(seen) == len(set(seen)) == 10
    assert seen == [str(UUID(int=i)) for i in range(10, 0, -1)]


def test_lower_server_cap_and_excluded_old_blocks_do_not_cut_off_history():
    rows = [entry(i) for i in range(1, 11)]
    rows[-1]["prescribed"]["block_type"] = "mindset"
    store = store_with(*rows)
    original = store.list_exercise_performances
    store.list_exercise_performances = Mock(side_effect=lambda *a, **kw: original(*a, **{**kw, "limit": min(2, kw["limit"])}))
    page = history(store, limit=3)
    seen = [item.id for item in page.performances]
    while page.next_cursor:
        page = history(store, cursor=page.next_cursor, limit=3)
        seen += [item.id for item in page.performances]
    assert len(seen) == len(set(seen)) == 9


def test_detail_keeps_prescribed_actual_skip_notes_and_consent_separate():
    store = store_with(entry(1, status="modified", actual={"sets": 2, "load": {"value": 75, "unit": "kg"}, "rest": {"value": 180, "unit": "seconds"}},
        reason="pain", notes="Private [injury_type:internal]"), entry(2, status="skipped"))
    result = history(store)
    assert result.performances[0].block.status == "skipped"
    changed = result.performances[1].block
    assert changed.prescribed["sets"] == 3 and changed.actual.sets == 2
    assert changed.notes == "Private" and changed.actual.rest.value == 180
    without = exercise_performances(store, ATHLETE, "key:back-squat", health_consent_granted=False)
    assert without.performances[1].block.reason is None and without.performances[1].block.notes is None


@pytest.mark.parametrize("cursor", ["bad", "[]", "e30", "a" * 25])
def test_bad_cursors_fail_safely(cursor):
    with pytest.raises(HTTPException) as error:
        history(store_with(entry()), cursor=cursor)
    assert error.value.status_code == 422


def test_other_exercises_cursor_is_rejected():
    store = store_with(entry(1), entry(2), entry(3, key="front-squat"))
    cursor = history(store, limit=1).next_cursor
    with pytest.raises(HTTPException) as error:
        history(store, "key:front-squat", cursor=cursor)
    assert error.value.status_code == 422


def test_failed_or_nonadvancing_scan_never_returns_partial_bests():
    store = store_with(entry())
    store.list_exercise_index_page = Mock(return_value=[entry()])
    with pytest.raises(HTTPException) as error:
        exercise_library(store, ATHLETE, now=NOW)
    assert error.value.status_code == 503


def test_owner_scoping_in_api_includes_index_detail_and_legacy_uuid():
    from tests.test_api_service_role_authorization import _create_two_athletes
    client, store, _ = _build_client()
    _create_two_athletes(client, store)
    store.exercise_logs = [entry(1, actual={"load": {"value": 80, "unit": "kg"}}),
        entry(2, athlete_id="athlete-2", actual={"load": {"value": 1000, "unit": "kg"}}),
        entry(3, key=None, athlete_id="athlete-2")]
    before = deepcopy(store.exercise_logs)
    auth = {"Authorization": "Bearer athlete-token"}
    assert client.get("/api/history/exercise-library").status_code == 401
    result = client.get("/api/history/exercise-library", headers=auth)
    assert result.status_code == 200 and result.json()["exercises"][0]["best_loads"][0]["value"] == 80
    result = client.get("/api/history/exercise-performances", params={"exercise": "key:back-squat"}, headers=auth)
    assert result.status_code == 200 and [item["id"] for item in result.json()["performances"]] == [entry()["id"]]
    result = client.get("/api/history/exercise-performances", params={"exercise": f"log:{entry(3)['id']}"}, headers=auth)
    assert result.status_code == 404
    assert store.exercise_logs == before


def test_store_index_is_owner_scoped_compact_keyset_and_fails_closed():
    client = MagicMock()
    query = client.table.return_value.select.return_value.eq.return_value.lte.return_value
    query.gt.return_value.order.return_value.limit.return_value.execute.return_value = SimpleNamespace(data=[])
    store = SupabaseAppStore(client=client, admin_emails=set())
    assert store.list_exercise_index_page(ATHLETE, as_of=NOW.isoformat(), after_id=entry()["id"]) == []
    projection = client.table.return_value.select.call_args.args[0]
    assert "notes" not in projection and "*" not in projection
    client.table.return_value.select.return_value.eq.assert_called_once_with("athlete_id", ATHLETE)
    query.gt.assert_called_once_with("id", entry()["id"])
    query.gt.return_value.order.return_value.limit.return_value.execute.return_value = SimpleNamespace(data=None)
    with pytest.raises(HTTPException):
        store.list_exercise_index_page(ATHLETE, as_of=NOW.isoformat(), after_id=entry()["id"])


@pytest.mark.parametrize("legacy", [False, True])
def test_performance_query_requires_owner_and_exact_key_or_original_log_uuid(legacy):
    client = MagicMock()
    selector = client.table.return_value.select.return_value.eq.return_value.lte.return_value
    query = selector.eq.return_value.or_.return_value
    query.order.return_value.order.return_value.limit.return_value.execute.return_value = SimpleNamespace(data=[])
    store = SupabaseAppStore(client=client, admin_emails=set())
    assert store.list_exercise_performances(ATHLETE, exercise_key=None if legacy else "back-squat",
        log_id=entry()["id"] if legacy else None, as_of=NOW.isoformat(), before_day="2026-10-10", before_id=entry(5)["id"]) == []
    client.table.return_value.select.return_value.eq.assert_called_once_with("athlete_id", ATHLETE)
    selector.eq.assert_called_once_with("id" if legacy else "exercise_key", entry()["id"] if legacy else "back-squat")
    selector.eq.return_value.or_.assert_called_once_with(f"training_day.lt.2026-10-10,and(training_day.eq.2026-10-10,id.lt.{entry(5)['id']})")
    query.order.assert_called_once_with("training_day", desc=True)
    query.order.return_value.order.assert_called_once_with("id", desc=True)


def test_progression_evidence_keeps_reported_effort_separate_from_the_target():
    row = entry(actual={"sets": 3, "reps": 5, "load": {"value": 80, "unit": "kg"}, "feel": "easy"},
                prescribed={"display_name": "Back Squat", "block_type": "strength", "sets": 3, "reps": 5,
                            "effort": {"method": "RPE", "value": 7}, "notes": "private"})
    item = exercise_library(store_with(row), ATHLETE, now=NOW, allow_progression=True).exercises[0]
    assert item.progression_hold is False and item.allow_progression
    assert item.latest_performance.actual.feel == "easy"
    assert item.latest_performance.actual.effort is None
    assert item.latest_performance.prescribed["effort"]["value"] == 7
    assert "notes" not in item.latest_performance.prescribed


def test_a_later_skip_or_pain_prevents_successful_history_from_earning_progression():
    for changes in [{"status": "skipped"}, {"reason": "pain"}]:
        rows = [entry(1, actual={"sets": 3, "reps": 5, "load": {"value": 80, "unit": "kg"}}),
                entry(2, training_day="2026-10-11", **changes)]
        item = exercise_library(store_with(*rows), ATHLETE, now=NOW, allow_progression=True).exercises[0]
        assert item.latest_performance.training_day == "2026-10-11"
        assert item.latest_performance.status == "skipped" or item.progression_hold is True


@pytest.mark.parametrize("tier,injuries,allowed", [("not_checked_in", [], True), ("stop", [], False), ("green", [{"id": "injury"}], False)])
def test_library_reuses_readiness_and_injury_restrictions_without_requiring_a_plan(monkeypatch, tier, injuries, allowed):
    from tests.test_api_service_role_authorization import _create_two_athletes
    import api.routes.today as routes
    client, store, _ = _build_client()
    _create_two_athletes(client, store)
    store.exercise_logs = [entry(actual={"sets": 3, "reps": 5, "load": {"value": 80, "unit": "kg"}, "effort": {"method": "RPE", "value": 6}})]
    view = SimpleNamespace(active_plan={}, today=SimpleNamespace(training_day="2026-10-19", decision_tier=tier), open_injuries=injuries, live_prescription=None)
    monkeypatch.setattr(routes, "build_today_command_view", lambda *a, **k: view)
    response = client.get("/api/history/exercise-library", headers={"Authorization": "Bearer athlete-token"})
    assert response.status_code == 200, response.text
    assert response.json()["exercises"][0]["allow_progression"] is allowed


def test_private_plan_history_uses_the_latest_result_including_skip_and_pain():
    prescribed = {"display_name": "Private Bench", "prescription_source": "athlete", "exercise_type": "strength",
                  "sets": 3, "reps": 10}
    for barrier in ({"status": "skipped"}, {"reason": "pain"}):
        rows = [entry(1, exercise_key=None, prescribed=prescribed,
                      actual={"sets": 3, "reps": 10, "load": {"value": 80, "unit": "kg"}, "feel": "easy"}),
                entry(2, exercise_key=None, prescribed=prescribed, training_day="2026-10-11", **barrier)]
        items = exercise_library(store_with(*rows), ATHLETE, now=NOW, allow_progression=True).exercises
        assert items
        for item in items:
            assert item.latest_performance.training_day == "2026-10-11"
            assert item.latest_performance.status == "skipped" or item.progression_hold is True
